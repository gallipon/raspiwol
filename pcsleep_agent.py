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


def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe(TOPIC)
    client.subscribe(AUTO_TOPIC)
    print("connected; subscribed " + TOPIC + ", " + AUTO_TOPIC)


def on_message(client, userdata, msg):
    global autopilot_on
    try:
        data = json.loads(msg.payload).get("data", "")
    except Exception:
        data = msg.payload.decode(errors="replace")
    val = str(data).strip().lower()

    if msg.topic == AUTO_TOPIC:
        autopilot_on = (val == "on")
        print("autopilot -> " + ("on" if autopilot_on else "off"))
        return

    if val == "sleep":   # dashboard / Slack: always honored (explicit intent)
        print("sleep command received -> suspending")
        sleep_pc()


client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="pcsleep_agent",
)
client.username_pw_set(TOKEN)
client.on_connect = on_connect
client.on_message = on_message


def main():
    read_autopilot()
    threading.Thread(target=autopilot_loop, daemon=True).start()
    while True:
        try:
            client.connect("mqtt.beebotte.com", 1883, keepalive=60)
            # If the connection drops on suspend/resume, loop_forever returns
            # and we reconnect.
            client.loop_forever()
        except Exception as e:
            print("mqtt error: " + str(e) + ", retry 30s", file=sys.stderr)
            time.sleep(30)


if __name__ == "__main__":
    main()
