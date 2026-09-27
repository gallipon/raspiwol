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
"""
import ctypes
import datetime
import glob
import json
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

# Claude Code activity detection.  Every session appends to its own transcript
# under ~/.claude/projects/, so the newest mtime across all of them is an
# all-sessions OR that needs no coordination: unlike a shared busy flag, one
# session finishing cannot clear another session's hold.  The ** is required --
# subagent transcripts sit one level deeper, and those long unattended runs are
# exactly what this is meant to protect.
CC_GLOB         = os.path.expanduser("~/.claude/projects/**/*.jsonl")
CC_STALE_MIN    = 20    # a transcript touched within this window == working.
                        # Generous on purpose: this is only ever evaluated after
                        # IDLE_MIN minutes of no physical input, so a fresh
                        # transcript then means "nobody here, but still running".
CC_MAX_HOLD_MIN = 180   # stop holding after this long, so a tab left looping
                        # forever cannot keep the PC awake indefinitely

# Deferred sleep ("sleep_in <min>") -- the office-day counterpart of Slack "終了".
DEFER_DEFAULT_MIN = 10    # delay used when no minutes are given
DEFER_IDLE_MIN    = 5     # after the delay, also require this much idle time
DEFER_MAX_MIN     = 240   # a reservation older than this is dropped (safety:
                          # a forgotten one must not suspend the PC next morning)
DEFER_CHECK_SEC   = 20    # how often the deferred loop evaluates

autopilot_on = True   # cached switch state; default on (automation enabled)

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


def cc_active():
    """True while any Claude Code session has been working recently.

    Freshness alone decides.  Nothing is written or deleted here, so a session
    that crashes cannot leave a flag stuck on: its transcript simply stops
    growing and goes stale within CC_STALE_MIN.  CC_MAX_HOLD_MIN covers the
    opposite failure -- a tab looping forever that would never let the PC sleep.

    Callers should evaluate this LAST, after the idle/weekday/cutoff checks, so
    the directory scan only runs when a suspend is otherwise imminent.
    """
    global _cc_hold_since, _cc_hold_capped
    try:
        files = glob.glob(CC_GLOB, recursive=True)
    except OSError as e:
        print("cc_active: scan failed: " + str(e), file=sys.stderr)
        return False
    if not files:
        # Claude Code moved its transcripts, or is not installed for this user.
        print("cc_active: no transcripts under " + CC_GLOB +
              " -- detection is not working", file=sys.stderr)
        return False

    newest      = 0.0
    newest_file = ""
    for f in files:
        try:
            m = os.path.getmtime(f)
        except OSError:
            continue        # a session removed or rotated it mid-scan
        if m > newest:
            newest, newest_file = m, f

    now = time.time()
    if now - newest > CC_STALE_MIN * 60:
        _cc_hold_since  = 0.0
        _cc_hold_capped = False
        return False

    if not _cc_hold_since:
        _cc_hold_since = now
        # Name the project so an unexpected "it never slept" is traceable.
        print("cc_active: %s active -- holding off sleep"
              % os.path.basename(os.path.dirname(newest_file)))

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
    try:
        arr = bbt_read(AUTO_RESOURCE, 1)
        if arr:
            autopilot_on = str(arr[0].get("data", "off")).strip().lower() == "on"
        print("autopilot initial state: " + ("on" if autopilot_on else "off"))
    except Exception as e:
        autopilot_on = False   # fail safe: never auto-sleep on a read failure
        print("autopilot read failed (fail-safe off): " + str(e), file=sys.stderr)


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


def main():
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
