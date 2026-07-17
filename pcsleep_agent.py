#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC sleep agent (design 2: no PC credentials stored on the Pi).

Subscribes to Beebotte raspi3b/pcsleep and, when it receives data == "sleep",
suspends this PC. The dashboard's Sleep button and the Slack "owari" webhook
both publish "sleep" to that resource.

It also runs a local auto-sleep loop: on weekdays, at/after the work-end hour,
if the user has been idle long enough, it suspends the PC -- but only while the
"autopilot" master switch (raspi3b/autopilot) is on. The switch is read once at
startup (REST) and then tracked live (MQTT subscribe). The Slack/dashboard
"sleep" command is always honored regardless of the switch (explicit intent).

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
                    # NOTE: GetLastInputInfo tracks physical keyboard/mouse only.
                    # Long background jobs (builds, Claude Code runs, downloads)
                    # do NOT count as activity -- an unattended long run can be
                    # suspended once past IDLE_MIN. Slack "終了" is the main path.
CHECK_SEC   = 60    # how often the auto-sleep loop evaluates
COOLDOWN_SEC = 300  # grace after an auto-sleep/resume before considering again

autopilot_on = True   # cached switch state; default on (automation enabled)

# last_rx: wall-clock time of the most recent inbound MQTT message.
# Updated in on_message (any topic) and on_connect.  Read in the monitor loop.
# Plain float assignment is atomic under the GIL -- no lock needed.
last_rx = time.time()

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


def read_autopilot():
    """Read the current switch state once via Beebotte REST (default: on)."""
    global autopilot_on
    try:
        req = urllib.request.Request(
            "https://api.beebotte.com/v1/data/read/%s/%s?limit=1"
            % (CHANNEL, AUTO_RESOURCE))
        req.add_header("X-Auth-Token", TOKEN)
        with urllib.request.urlopen(req, timeout=5) as r:
            arr = json.loads(r.read())
        if arr:
            autopilot_on = str(arr[0].get("data", "on")).strip().lower() == "on"
        print("autopilot initial state: " + ("on" if autopilot_on else "off"))
    except Exception as e:
        print("autopilot read failed (default on): " + str(e), file=sys.stderr)


def autopilot_loop():
    """Weekday + after work-end + idle -> suspend, while the switch is on."""
    while True:
        try:
            now = datetime.datetime.now()
            if (autopilot_on
                    and now.weekday() < 5
                    and now.hour >= CUTOFF_HOUR
                    and idle_seconds() >= IDLE_MIN * 60):
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


def heartbeat_loop(client):
    """Publish a timestamped heartbeat to AGENT_TOPIC every HEARTBEAT_SEC.

    The dashboard reads this resource via REST to display agent liveness.
    write:True instructs Beebotte to persist the value (required for REST read).
    """
    while True:
        time.sleep(HEARTBEAT_SEC)
        try:
            payload = json.dumps({"data": int(time.time()), "write": True})
            rc = client.publish(AGENT_TOPIC, payload)
            if rc.rc != mqtt.MQTT_ERR_SUCCESS:
                print("heartbeat publish failed rc=%d" % rc.rc, file=sys.stderr)
        except Exception as e:
            print("heartbeat error: " + str(e), file=sys.stderr)
            # The monitor loop will detect the disconnect and reconnect.


# ── MQTT callbacks ────────────────────────────────────────────────────────────

def on_connect(client, userdata, flags, reason_code, properties):
    global last_rx
    last_rx = time.time()   # reset stale timer on successful (re)connect
    client.subscribe(TOPIC)
    client.subscribe(AUTO_TOPIC)
    client.subscribe(AGENT_TOPIC)   # subscribe to own topic for self-echo detection
    print("connected; subscribed %s, %s, %s" % (TOPIC, AUTO_TOPIC, AGENT_TOPIC))


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    print("disconnected (rc=%s); monitor loop will reconnect" % reason_code,
          file=sys.stderr)


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

    if val == "sleep":   # dashboard / Slack: always honored (explicit intent)
        print("sleep command received -> suspending")
        sleep_pc()


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
    threading.Thread(target=heartbeat_loop, args=(client,), daemon=True).start()

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
