#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC sleep agent (design 2: no PC credentials stored on the Pi).

Subscribes to Beebotte raspi3b/pcsleep and, when it receives data == "sleep",
suspends this PC. The dashboard's Sleep button and the Slack "owari" webhook
both publish "sleep" to that resource.

It also accepts a deferred sleep, "sleep_in <minutes>" (dashboard 退勤 button /
pcsleep_cmd.py), for office days when no Slack "終了" is posted: after the delay
elapses it waits until the user has also been idle for DEFER_IDLE_MIN minutes,
then suspends.  Late input postpones the suspend instead of cancelling it, so
the PC never sleeps under your hands but always sleeps once you have left.
"cancel" clears a pending reservation.

It also runs a local auto-sleep loop: on weekdays, at/after the work-end hour,
if the user has been idle long enough, it suspends the PC -- but only while the
"autopilot" master switch (raspi3b/autopilot) is on. The switch is read once at
startup (REST) and then tracked live (MQTT subscribe). The Slack/dashboard
"sleep" command is always honored regardless of the switch (explicit intent).

Both automatic paths (the idle auto-sleep and the deferred sleep) additionally
hold off while any Claude Code session is still working, since GetLastInputInfo
cannot see an unattended agent run and would suspend the PC mid-task. The
explicit "sleep" command ignores this, just as it ignores the switch.

Commands arrive over two paths.  Writers (dashboard, VPS Slack poller,
pcsleep_cmd.py) WRITE their command to the persisted resource raspi3b/pcsleep_req
and this agent polls it over REST every POLL_SEC.  MQTT delivery of that same
write only nudges the poller, so a working broker keeps the response instant
while REST alone is enough to function.  This exists because on 2026-09-22
Beebotte's broker refused every new MQTT connection for days while its REST API
kept working, which left all sleep paths dead.  The legacy raspi3b/pcsleep topic
is still honored over MQTT for older senders.

Zombie resilience: on sleep/resume the MQTT TCP connection can become half-open
(the OS thinks it is connected but Beebotte has already dropped it). A watchdog
monitor loop detects this via self-echo heartbeats (ZOMBIE_ECHO_CHECK) and
reconnects or exits cleanly so an external watchdog task can restart the process.

- The Pi, SSH and keys are NOT involved. This PC only makes an outbound
  connection to Beebotte. The only thing it can do is suspend itself, so a
  leaked token is not an intrusion path.
- Runs inside the user session so SetSuspendState and idle detection work
  reliably (Task Scheduler: "At log on" / "Run only when user is logged on").

Setup (Windows cmd):
  pip install paho-mqtt
  set BEEBOTTE_TOKEN=token_XXXX
  python pcsleep_agent.py

Autostart (Task Scheduler):
  - Trigger: At log on
  - Action: pythonw.exe <full path to this file>   (pythonw = no console window)
  - "Run only when user is logged on"
  - Put BEEBOTTE_TOKEN in the user env vars:  setx BEEBOTTE_TOKEN token_XXXX
  - Optional, self-hosted broker (parallel run with Beebotte):
      setx PCSLEEP_MQTT_PASS <password of the "agent" user>
"""
import ctypes
import datetime
import glob
import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

TOKEN    = os.environ.get("BEEBOTTE_TOKEN", "")
CHANNEL  = "raspi3b"
RESOURCE = "pcsleep"
TOPIC    = CHANNEL + "/" + RESOURCE

# REST command queue: senders write here (persisted), we poll it.  This is the
# path that survives an MQTT outage; see the module docstring.
REQ_RESOURCE = "pcsleep_req"
REQ_TOPIC    = CHANNEL + "/" + REQ_RESOURCE
POLL_SEC     = 20     # how often we poll the queue over REST
CMD_MAX_AGE_SEC  = 300    # ignore a queued command older than this.  One written
                          # while the PC slept must not suspend it again right
                          # after a WOL wake.
AUTO_REFRESH_SEC = 300    # re-read the autopilot switch this often, since MQTT
                          # (the live path for switch changes) may be down

# Master on/off switch (Beebotte resource shared with the dashboard and the Pi).
AUTO_RESOURCE = "autopilot"
AUTO_TOPIC    = CHANNEL + "/" + AUTO_RESOURCE

# Heartbeat / zombie-detection resource (agent liveness, read by the dashboard).
AGENT_RESOURCE   = "agent"
AGENT_TOPIC      = CHANNEL + "/" + AGENT_RESOURCE

# Self-hosted broker (VPS Mosquitto behind Apache), run alongside Beebotte while
# we migrate off it (Beebotte's certificate expired on 2026-11-17 and was not
# renewed).  Enabled only when PCSLEEP_MQTT_PASS is set, so an agent without it
# behaves exactly as before.  Nothing on this path may disturb the Beebotte one.
#   raspi3b/pcsleep_req  retained, {"data": cmd, "ts": ms}  (dashboard2 / Slack)
#   raspi3b/autopilot    retained, {"data": "on"|"off"}     -- wins over Beebotte
#                        once it has been received
#   raspi3b/agent        retained heartbeat, {"data": epoch_sec}
MQ_HOST     = os.environ.get("PCSLEEP_MQTT_HOST", "gallipon.f5.si")
MQ_PATH     = "/mqtt"
MQ_USER     = "agent"
MQ_PASS     = os.environ.get("PCSLEEP_MQTT_PASS", "")
MQ_KEEPALIVE_SEC = 60     # Apache's proxy timeout is 300 s, well above this

# Connection tuning.
KEEPALIVE_SEC    = 20     # short keepalive speeds up half-open detection by the broker

# Heartbeat sent to Beebotte so the dashboard can read agent liveness.
HEARTBEAT_SEC    = 60     # how often we publish a heartbeat

# Zombie detection via self-echo: Beebotte echoes our own publish back to us.
# If is_connected() is True but we have not received ANY message for this long,
# the receive path is probably stuck (zombie).
ECHO_STALE_SEC   = 200    # seconds without any inbound message -> zombie suspect
ZOMBIE_ECHO_CHECK = True  # set False if your Beebotte plan does not echo own publishes

# Monitor loop cadence.
MONITOR_SEC      = 15     # how often the watchdog checks connection health

# Auto-sleep policy (constants -- tweak freely).
CUTOFF_HOUR = 19    # only auto-sleep at/after this hour (work end = 19:00)
IDLE_MIN    = 60    # required minutes with no keyboard/mouse input
                    # NOTE: GetLastInputInfo tracks physical keyboard/mouse only,
                    # so long background jobs do NOT count as activity. Claude
                    # Code runs are covered separately by cc_active(); other
                    # unattended jobs (builds, downloads) still are not.
CHECK_SEC   = 60    # how often the auto-sleep loop evaluates
COOLDOWN_SEC = 300  # grace after an auto-sleep/resume before considering again

# Claude Code activity detection.  ClaudeTabs' state-hook.js keeps one file per
# session, rewritten from Claude Code's hooks: UserPromptSubmit -> "working",
# Stop -> "done", a permission prompt -> "blocked", StopFailure -> "failed",
# SessionEnd -> file removed.  Any "working" file means a turn is in progress,
# however long it has gone without writing to its transcript.  One file per
# session also makes it an all-sessions OR: one session finishing cannot clear
# another's hold.  "blocked" does not hold -- it is waiting for a human.
#
# Transcript mtimes are NOT used: every resumed claude.exe re-stamps its own
# transcript's mtime once an hour without writing, so with ~10 tabs restored
# after a reboot some file was always "fresh" and the PC never slept (2026-10-14).
CC_STATE_DIR    = os.path.expanduser("~/claude_logs/claude-park/state")
CC_PROJECTS     = os.path.expanduser("~/.claude/projects")
CC_TAIL_BYTES   = 256 * 1024   # how much of a transcript's end is read to spot
                               # an Esc interrupt (see _cc_interrupted)
CC_MAX_HOLD_MIN = 180   # stop holding after this long, so a tab left looping
                        # forever cannot keep the PC awake indefinitely

# Deferred sleep ("sleep_in <min>") -- the office-day counterpart of Slack "終了".
DEFER_DEFAULT_MIN = 10    # delay used when no minutes are given
DEFER_IDLE_MIN    = 5     # after the delay, also require this much idle time
DEFER_MAX_MIN     = 240   # a reservation older than this is dropped (safety:
                          # a forgotten one must not suspend the PC next morning)
DEFER_CHECK_SEC   = 20    # how often the deferred loop evaluates

autopilot_on = True   # cached switch state; default on (automation enabled)
# True once the self-hosted broker has delivered the switch.  From then on its
# value is the truth and Beebotte's (REST refresh or MQTT) is ignored.
auto_from_mq = False

# Self-hosted broker command guard, separate from cmd_watermark: its ts comes
# from the sender's clock, not Beebotte's.  Seeded with this process's start
# time, so anything sent before we started is history (the REST queue's backlog
# rule) no matter when or how often the broker hands the retained copy over,
# while a command sent during a later disconnect is still executed on reconnect.
mq_watermark = time.time() * 1000.0
MQ_FUTURE_SEC = 600    # a ts further ahead than this is rejected: it would block
                       # every later command until the clock caught up

# Guards autopilot_on / auto_from_mq: a Beebotte REST read can take seconds and
# must not overwrite a value the self-hosted broker delivered meanwhile.
auto_lock = threading.Lock()

# Highest command timestamp (ms, Beebotte's own) already handled.  0 = not yet
# initialised; poll_loop seeds it from the queue so the backlog is skipped.
cmd_watermark = 0.0
# Set by on_message so an MQTT-delivered write polls immediately instead of
# waiting out POLL_SEC.  The poller stays the only executor, so a command can
# never run twice.
poll_now = threading.Event()

# Pending deferred sleep. defer_due = epoch when the delay elapses (0 = none),
# defer_expire = epoch when the reservation is dropped unfired.
# Plain float assignment is atomic under the GIL -- no lock needed.
defer_due    = 0.0
defer_expire = 0.0

# last_rx: wall-clock time of the most recent inbound MQTT message.
# Updated in on_message (any topic) and on_connect.  Read in the monitor loop.
# Plain float assignment is atomic under the GIL -- no lock needed.
last_rx = time.time()

# _cc_hold_since: epoch when the current Claude Code hold started (0 = none).
# _cc_hold_capped: True once CC_MAX_HOLD_MIN was reached, so the message that
# announces giving up is printed once per hold instead of every cycle.
_cc_hold_since  = 0.0
_cc_hold_capped = False

if not TOKEN:
    print("Error: set the BEEBOTTE_TOKEN environment variable", file=sys.stderr)
    sys.exit(1)


def sleep_pc():
    # SetSuspendState(Hibernate=0 -> sleep, Force=0, WakeupEventsDisabled=0).
    # WakeupEventsDisabled=0 lets the PC resume from WOL (magic packet).
    ctypes.windll.powrprof.SetSuspendState(0, 0, 0)


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """Seconds since the last local keyboard/mouse input (GetLastInputInfo)."""
    lii = _LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
    tick = ctypes.windll.kernel32.GetTickCount()          # 32-bit DWORD
    return ((tick - lii.dwTime) & 0xFFFFFFFF) / 1000.0     # mask handles wrap


def _boot_epoch():
    """Wall-clock time of the last boot (GetTickCount64 = ms since boot)."""
    f = ctypes.windll.kernel32.GetTickCount64
    f.restype = ctypes.c_ulonglong
    return time.time() - f() / 1000.0


def _cc_interrupted(sid):
    """True if the session's newest conversation line is an Esc interrupt.

    Claude Code does not fire the Stop hook when the user interrupts a turn, so
    the state file stays "working" although nothing runs.  The interrupt shows
    up in the transcript as a user line "[Request interrupted by user...]".  A
    missing transcript returns False (trust the hook).
    """
    paths = glob.glob(os.path.join(glob.escape(CC_PROJECTS), "*", sid + ".jsonl"))
    if not paths:
        return False
    try:
        size = os.path.getsize(paths[0])
        with open(paths[0], "rb") as fh:
            fh.seek(max(0, size - CC_TAIL_BYTES))
            lines = fh.read().split(b"\n")
    except OSError:
        return False
    if size > CC_TAIL_BYTES:
        lines = lines[1:]        # first line is cut mid-way
    for raw in reversed(lines):
        if b'"type":"user"' not in raw and b'"type":"assistant"' not in raw:
            continue             # system lines (away recap etc.) say nothing
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if rec.get("type") not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        if rec["type"] == "assistant":
            return False
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        else:
            text = ""
        return text.lstrip().startswith("[Request interrupted by user")
    return False


def _cc_working_session():
    """Session id of a turn in progress, "" if none (see CC_STATE_DIR)."""
    try:
        names = os.listdir(CC_STATE_DIR)
    except OSError:
        # ClaudeTabs' hook is not installed: do not hold.  Holding on a guess is
        # what kept the PC awake all night, so a missing signal means "idle".
        return ""
    boot = _boot_epoch()
    for name in names:
        if not name.endswith(".json"):
            continue             # *.tmp is mid-rename
        try:
            with open(os.path.join(CC_STATE_DIR, name), encoding="utf-8") as fh:
                st = json.load(fh)
        except (OSError, ValueError):
            continue
        if st.get("state") != "working":
            continue
        if st.get("ts", 0) / 1000.0 < boot:
            continue             # left over from before a reboot or crash
        sid = name[:-5]
        if _cc_interrupted(sid):
            continue
        return sid
    return ""


def cc_active():
    """True while any Claude Code session has a turn in progress.

    Nothing is written or deleted here.  A session that dies mid-turn leaves its
    state file on "working"; a reboot clears that (ts older than boot), and
    CC_MAX_HOLD_MIN covers both that and a tab looping forever that would never
    let the PC sleep.

    Callers should evaluate this LAST, after the idle/weekday/cutoff checks, so
    the directory scan only runs when a suspend is otherwise imminent.
    """
    global _cc_hold_since, _cc_hold_capped
    sid = _cc_working_session()
    now = time.time()
    if not sid:
        _cc_hold_since  = 0.0
        _cc_hold_capped = False
        return False

    if not _cc_hold_since:
        _cc_hold_since = now
        # Name the session so an unexpected "it never slept" is traceable.
        print("cc_active: session %s working -- holding off sleep" % sid)

    if now - _cc_hold_since >= CC_MAX_HOLD_MIN * 60:
        if not _cc_hold_capped:
            _cc_hold_capped = True
            print("cc_active: held %d min, cap reached -- allowing sleep"
                  % CC_MAX_HOLD_MIN, file=sys.stderr)
        return False
    return True


def bbt_read(resource, limit=1):
    """REST read of a persisted resource; returns Beebotte's list of records.

    api.beebotte.com serves an incomplete certificate chain, so Python's
    OpenSSL rejects it (CERTIFICATE_VERIFY_FAILED) even though curl/browsers
    pass via AIA fetching.  We disable verification here -- same workaround as
    the Pi's bbt_write.  Without it the autopilot read always failed and fell
    back to "on", which once caused an unexpected auto-sleep while it was off.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        "https://api.beebotte.com/v1/data/read/%s/%s?limit=%d"
        % (CHANNEL, resource, limit))
    req.add_header("X-Auth-Token", TOKEN)
    with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
        return json.loads(r.read())


def read_autopilot():
    """Refresh the cached switch state from Beebotte.

    On failure we fail SAFE = off: a read error must never enable auto-sleep.
    """
    global autopilot_on
    if auto_from_mq:
        return             # the self-hosted broker owns the switch now
    try:
        arr = bbt_read(AUTO_RESOURCE, 1)
        new = (str(arr[0].get("data", "off")).strip().lower() == "on") if arr else None
        err = None
    except Exception as e:
        new, err = False, e    # fail safe: never auto-sleep on a read failure
    with auto_lock:
        if auto_from_mq:
            return         # the broker spoke while we were reading -- it wins
        if new is not None:
            autopilot_on = new
    if err is not None:
        print("autopilot read failed (fail-safe off): " + str(err), file=sys.stderr)
    else:
        print("autopilot initial state: " + ("on" if autopilot_on else "off"))


def autopilot_loop():
    """Weekday + after work-end + idle -> suspend, while the switch is on."""
    while True:
        try:
            now = datetime.datetime.now()
            if (autopilot_on
                    and now.weekday() < 5
                    and now.hour >= CUTOFF_HOUR
                    and idle_seconds() >= IDLE_MIN * 60
                    and not cc_active()):   # last: only scans when about to sleep
                print("auto-sleep: weekday after %02d:00 and idle %dmin -> suspend"
                      % (CUTOFF_HOUR, IDLE_MIN))
                sleep_pc()
                # On resume, give a grace period. If the user resumed by input,
                # idle is already reset so this just avoids a tight loop after a
                # non-input (e.g. network) resume.
                time.sleep(COOLDOWN_SEC)
        except Exception as e:
            print("autopilot loop error: " + str(e), file=sys.stderr)
        time.sleep(CHECK_SEC)


def arm_defer(minutes):
    """Arm (or re-arm) a deferred sleep `minutes` from now."""
    global defer_due, defer_expire
    now = time.time()
    defer_due    = now + minutes * 60
    defer_expire = now + DEFER_MAX_MIN * 60
    print("deferred sleep armed: %d min (idle %d min required after that)"
          % (minutes, DEFER_IDLE_MIN))


def cancel_defer(reason):
    global defer_due, defer_expire
    if defer_due:
        print("deferred sleep cancelled (%s)" % reason)
    defer_due = defer_expire = 0.0


def defer_loop():
    """Fire a pending deferred sleep once the delay AND the idle time are met.

    Input during the countdown POSTPONES the suspend (it does not cancel it):
    we keep waiting until idle_seconds() reaches DEFER_IDLE_MIN, so resuming
    work after pressing 退勤 never sleeps the PC mid-keystroke, while walking
    away always ends in a suspend.  Unlike autopilot_loop this ignores the
    autopilot switch and the weekday/cutoff rules -- it is an explicit request.
    """
    while True:
        time.sleep(DEFER_CHECK_SEC)
        try:
            if not defer_due:
                continue
            now = time.time()
            if now > defer_expire:
                cancel_defer("expired after %d min unfired" % DEFER_MAX_MIN)
                continue
            if now < defer_due:
                continue
            idle = idle_seconds()
            if idle < DEFER_IDLE_MIN * 60:
                continue   # still in use -- postpone, re-check next cycle
            if cc_active():
                continue   # agent run in flight -- postpone (DEFER_MAX_MIN caps it)
            print("deferred sleep: delay elapsed and idle %.0f min -> suspend"
                  % (idle / 60))
            cancel_defer("fired")
            sleep_pc()
            time.sleep(COOLDOWN_SEC)
        except Exception as e:
            print("defer loop error: " + str(e), file=sys.stderr)


def bbt_write(resource, value):
    """REST write (persisted) -- the fallback when MQTT is unavailable."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        "https://api.beebotte.com/v1/data/write/%s/%s" % (CHANNEL, resource),
        data=json.dumps({"data": value}).encode(),
        headers={"X-Auth-Token": TOKEN, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=5, context=ctx) as r:
        r.read()


def heartbeat_loop(client):
    """Publish a timestamped heartbeat to AGENT_TOPIC every HEARTBEAT_SEC.

    The dashboard reads this resource via REST to display agent liveness.
    write:True instructs Beebotte to persist the value (required for REST read).

    Falls back to a REST write while MQTT is unusable -- otherwise an outage
    makes the agent look dead even though it is serving REST commands, which is
    exactly when its liveness matters most.  The MQTT publish also doubles as
    the self-echo the monitor loop uses, so it stays the preferred path.
    """
    while True:
        time.sleep(HEARTBEAT_SEC)
        try:
            payload = json.dumps({"data": int(time.time()), "write": True})
            rc = client.publish(AGENT_TOPIC, payload)
            if rc.rc == mqtt.MQTT_ERR_SUCCESS and client.is_connected():
                continue
            print("heartbeat over MQTT unavailable (rc=%d) -- writing via REST"
                  % rc.rc, file=sys.stderr)
        except Exception as e:
            print("heartbeat error: " + str(e), file=sys.stderr)
        try:
            bbt_write(AGENT_RESOURCE, int(time.time()))
        except Exception as e:
            print("heartbeat REST write failed: " + str(e), file=sys.stderr)


# ── MQTT callbacks ────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties):
    global last_rx
    last_rx = time.time()   # reset stale timer on successful (re)connect
    client.subscribe(TOPIC)
    client.subscribe(REQ_TOPIC)
    client.subscribe(AUTO_TOPIC)
    client.subscribe(AGENT_TOPIC)   # subscribe to own topic for self-echo detection
    print("connected; subscribed %s, %s, %s, %s"
          % (TOPIC, REQ_TOPIC, AUTO_TOPIC, AGENT_TOPIC))


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print("disconnected (rc=%s); monitor loop will reconnect" % reason_code,
          file=sys.stderr)


def handle_command(val):
    """Act on one command string, whichever path delivered it."""
    if val == "sleep":   # dashboard / Slack: always honored (explicit intent)
        print("sleep command received -> suspending")
        cancel_defer("superseded by immediate sleep")
        sleep_pc()
        return

    if val == "cancel":                       # clear a pending reservation
        cancel_defer("cancel command received")
        return

    if val.startswith("sleep_in"):            # "sleep_in" | "sleep_in 20"
        arg = val[len("sleep_in"):].strip()
        try:
            minutes = int(arg) if arg else DEFER_DEFAULT_MIN
        except ValueError:
            print("bad sleep_in argument: " + arg, file=sys.stderr)
            return
        if not 0 < minutes <= DEFER_MAX_MIN:
            print("sleep_in out of range: %d" % minutes, file=sys.stderr)
            return
        arm_defer(minutes)
        return

    print("unknown command ignored: " + val, file=sys.stderr)


def poll_loop():
    """Poll the REST command queue; the only executor of queued commands.

    MQTT delivery of a queue write merely sets poll_now, so a command is acted
    on exactly once no matter how many paths carried it.  The watermark is
    advanced BEFORE acting because a suspend blocks inside handle_command until
    the PC resumes -- on resume the same entry must not fire again.
    """
    global cmd_watermark

    # Seed the watermark: whatever is already queued at startup is history.
    while not cmd_watermark:
        try:
            arr = bbt_read(REQ_RESOURCE, 1)
            cmd_watermark = float(arr[0]["ts"]) if arr else time.time() * 1000.0
            print("command queue ready (watermark %.0f)" % cmd_watermark)
        except Exception as e:
            print("command queue unavailable (%s) -- create the '%s' resource in "
                  "the Beebotte console; retrying" % (e, REQ_RESOURCE),
                  file=sys.stderr)
            time.sleep(POLL_SEC)

    last_auto = time.time()
    while True:
        poll_now.wait(POLL_SEC)
        poll_now.clear()
        try:
            if time.time() - last_auto >= AUTO_REFRESH_SEC:
                read_autopilot()     # MQTT may be down, so refresh it here too
                last_auto = time.time()
            entries = bbt_read(REQ_RESOURCE, 5)
        except Exception as e:
            print("command poll failed: " + str(e), file=sys.stderr)
            continue

        for rec in sorted(entries, key=lambda x: x.get("ts", 0)):
            ts = float(rec.get("ts", 0))
            if ts <= cmd_watermark:
                continue
            cmd_watermark = ts
            val = str(rec.get("data", "")).strip().lower()
            age = time.time() - ts / 1000.0
            if age > CMD_MAX_AGE_SEC:
                print("stale command ignored (%d min old): %s" % (age / 60, val))
                continue
            print("command via REST: " + val)
            handle_command(val)


def on_message(client, userdata, msg):
    global autopilot_on, last_rx
    last_rx = time.time()   # any inbound message keeps the zombie timer alive

    try:
        data = json.loads(msg.payload).get("data", "")
    except Exception:
        data = msg.payload.decode(errors="replace")
    val = str(data).strip().lower()

    # Self-echo from our own heartbeat publish -- just a liveness ping, ignore.
    if msg.topic == AGENT_TOPIC:
        return

    if msg.topic == AUTO_TOPIC:
        with auto_lock:
            if auto_from_mq:
                return     # the self-hosted broker owns the switch now
            autopilot_on = (val == "on")
        print("autopilot -> " + ("on" if autopilot_on else "off"))
        return

    # A queue write also arrives here: poll right away instead of executing, so
    # the poller's watermark stays the single guard against double execution.
    if msg.topic == REQ_TOPIC:
        poll_now.set()
        return

    handle_command(val)   # legacy raspi3b/pcsleep senders


# ── MQTT client setup ─────────────────────────────────────────────────────────

client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="pcsleep_agent",
)
client.username_pw_set(TOKEN)
client.reconnect_delay_set(min_delay=1, max_delay=30)
client.on_connect    = on_connect
client.on_disconnect = on_disconnect
client.on_message    = on_message


def monitor_loop(client):
    """Main watchdog: checks connectivity and zombie state every MONITOR_SEC.

    Zombie detection strategy (when ZOMBIE_ECHO_CHECK is True):
      Beebotte echoes every publish back to subscribers of the same topic.
      We subscribe to AGENT_TOPIC (our own heartbeat topic), so every
      successful heartbeat should produce an echo within a few seconds.
      If is_connected() is True but last_rx is stale for ECHO_STALE_SEC,
      the receive path is stuck.  We try one reconnect; if that does not
      produce traffic within another ECHO_STALE_SEC window, we call
      os._exit(1) so the watchdog task scheduler can restart the process.

    Fallback when ZOMBIE_ECHO_CHECK is False:
      Only the is_connected() check is active; no zombie detection.
      This is safe for environments where Beebotte does not echo own publishes.
    """
    reconnect_attempted_at = 0.0   # wall time of last reconnect attempt

    while True:
        time.sleep(MONITOR_SEC)
        try:
            if not client.is_connected():
                print("monitor: not connected, reconnecting...", file=sys.stderr)
                try:
                    client.reconnect()
                    reconnect_attempted_at = time.time()
                except Exception as e:
                    print("monitor: reconnect failed: " + str(e), file=sys.stderr)
                continue   # re-evaluate after next sleep cycle

            if not ZOMBIE_ECHO_CHECK:
                continue   # self-echo check disabled; only is_connected() matters

            stale = time.time() - last_rx
            if stale < ECHO_STALE_SEC:
                continue   # traffic is flowing, healthy

            # Receive path looks stale.
            since_reconnect = time.time() - reconnect_attempted_at
            if since_reconnect > ECHO_STALE_SEC:
                # First time noticing stale (or long enough after last attempt).
                print("monitor: zombie suspect (no rx for %.0fs), reconnecting..."
                      % stale, file=sys.stderr)
                try:
                    client.reconnect()
                    reconnect_attempted_at = time.time()
                except Exception as e:
                    print("monitor: reconnect failed: " + str(e), file=sys.stderr)
            else:
                # We already tried a reconnect but traffic still has not resumed.
                print("monitor: zombie confirmed (no rx %.0fs after reconnect), "
                      "exiting for watchdog restart" % stale, file=sys.stderr)
                os._exit(1)

        except Exception as e:
            print("monitor loop error: " + str(e), file=sys.stderr)


# ── Self-hosted broker (parallel run) ─────────────────────────────────────────
# paho's own loop reconnects this client; the Beebotte monitor/zombie logic does
# not touch it.  Every callback swallows its own errors so a fault here cannot
# take the Beebotte path down with it.

MQ_REQ_TOPIC  = REQ_TOPIC
MQ_AUTO_TOPIC = AUTO_TOPIC
MQ_AGENT_TOPIC = AGENT_TOPIC


def _mq_suspend():
    try:
        sleep_pc()
    except Exception as e:
        print("mq: suspend failed: " + str(e), file=sys.stderr)


def mq_on_connect(client, userdata, flags, reason_code, properties):
    try:
        if reason_code.is_failure:
            print("mq: connect refused (%s)" % reason_code, file=sys.stderr)
            return
        client.subscribe([(MQ_REQ_TOPIC, 1), (MQ_AUTO_TOPIC, 1)])
        print("mq: connected to %s; subscribed %s, %s" % (MQ_HOST, MQ_REQ_TOPIC, MQ_AUTO_TOPIC))
    except Exception as e:
        print("mq: on_connect error: " + str(e), file=sys.stderr)


def mq_on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print("mq: disconnected (rc=%s); paho will reconnect" % reason_code, file=sys.stderr)


def mq_on_message(client, userdata, msg):
    global autopilot_on, auto_from_mq, mq_watermark
    try:
        if not msg.payload:
            return                                   # retained copy deleted
        body = json.loads(msg.payload)
        val  = str(body.get("data", "")).strip().lower()

        if msg.topic == MQ_AUTO_TOPIC:
            with auto_lock:
                autopilot_on = (val == "on")
                took_over = not auto_from_mq
                auto_from_mq = True
            if took_over:
                print("mq: taking over the autopilot switch from Beebotte")
            print("autopilot -> %s (mq)" % ("on" if autopilot_on else "off"))
            return

        if msg.topic != MQ_REQ_TOPIC:
            return
        ts = float(body.get("ts", 0))
        if not math.isfinite(ts) or ts > (time.time() + MQ_FUTURE_SEC) * 1000.0:
            print("mq: command with bad ts ignored: %r %s" % (body.get("ts"), val),
                  file=sys.stderr)
            return
        if ts <= mq_watermark:
            return                     # sent before we started, or already handled
        mq_watermark = ts              # advance BEFORE acting, see poll_loop
        age = time.time() - ts / 1000.0
        if age > CMD_MAX_AGE_SEC:
            print("mq: stale command ignored (%d min old): %s" % (age / 60, val))
            return
        print("command via mq: " + val)
        if val == "sleep":
            # Clear any reservation here, in arrival order; only the suspend itself
            # (it blocks until resume) runs off paho's thread, so a sleep_in that
            # arrives after this sleep cannot be cancelled by a late worker.
            print("sleep command received -> suspending")
            cancel_defer("superseded by immediate sleep")
            threading.Thread(target=_mq_suspend, daemon=True).start()
        else:
            handle_command(val)        # sleep_in / cancel: quick, keep arrival order
    except Exception as e:
        print("mq: message error: " + str(e), file=sys.stderr)


def mq_heartbeat_loop(client):
    while True:
        time.sleep(HEARTBEAT_SEC)
        try:
            client.publish(MQ_AGENT_TOPIC, json.dumps({"data": int(time.time())}),
                           qos=0, retain=True)
        except Exception as e:
            print("mq: heartbeat error: " + str(e), file=sys.stderr)


def start_mq():
    """Start the self-hosted broker client, or do nothing if it is not configured."""
    if not MQ_PASS:
        print("mq: PCSLEEP_MQTT_PASS not set -- self-hosted broker disabled")
        return
    try:
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id="pcsleep_agent", transport="websockets")
        c.ws_set_options(path=MQ_PATH)
        c.tls_set()                                  # Let's Encrypt, verified
        c.username_pw_set(MQ_USER, MQ_PASS)
        c.reconnect_delay_set(min_delay=1, max_delay=60)
        c.on_connect, c.on_disconnect, c.on_message = (
            mq_on_connect, mq_on_disconnect, mq_on_message)
        c.connect_async(MQ_HOST, 443, keepalive=MQ_KEEPALIVE_SEC)
        c.loop_start()                               # retries on its own if the VPS is down
        threading.Thread(target=mq_heartbeat_loop, args=(c,), daemon=True).start()
    except Exception as e:
        print("mq: start failed (Beebotte path unaffected): " + str(e), file=sys.stderr)


def main():
    start_mq()
    read_autopilot()
    threading.Thread(target=autopilot_loop, daemon=True).start()
    threading.Thread(target=defer_loop, daemon=True).start()
    threading.Thread(target=heartbeat_loop, args=(client,), daemon=True).start()
    # Started before the connect loop below: with the broker refusing
    # connections, REST polling is the only working path and must not wait.
    threading.Thread(target=poll_loop, daemon=True).start()

    # Initial connect; the monitor loop keeps it alive from here on.
    while True:
        try:
            client.connect("mqtt.beebotte.com", 1883, keepalive=KEEPALIVE_SEC)
            break
        except Exception as e:
            print("initial connect failed: " + str(e) + ", retry 30s",
                  file=sys.stderr)
            time.sleep(30)

    client.loop_start()
    monitor_loop(client)   # blocks; exits via os._exit on confirmed zombie


if __name__ == "__main__":
    main()
