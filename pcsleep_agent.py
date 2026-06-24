#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PC sleep agent (design 2: no PC credentials stored on the Pi).

Subscribes to Beebotte raspi3b/pcsleep and, when it receives data == "sleep",
suspends this PC. The dashboard's Sleep button publishes to that resource.

- The Pi, SSH and keys are NOT involved. This PC only makes an outbound
  connection to Beebotte. The only thing it can do is suspend itself, so a
  leaked token is not an intrusion path.
- Runs inside the user session so SetSuspendState works reliably
  (Task Scheduler: "At log on" / "Run only when user is logged on").

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
import json
import os
import sys
import time

import paho.mqtt.client as mqtt

TOKEN    = os.environ.get("BEEBOTTE_TOKEN", "")
CHANNEL  = "raspi3b"
RESOURCE = "pcsleep"
TOPIC    = CHANNEL + "/" + RESOURCE

if not TOKEN:
    print("Error: set the BEEBOTTE_TOKEN environment variable", file=sys.stderr)
    sys.exit(1)


def sleep_pc():
    # SetSuspendState(Hibernate=0 -> sleep, Force=0, WakeupEventsDisabled=0).
    # WakeupEventsDisabled=0 lets the PC resume from WOL (magic packet).
    ctypes.windll.powrprof.SetSuspendState(0, 0, 0)


def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe(TOPIC)
    print("connected; subscribed " + TOPIC)


def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload).get("data", "")
    except Exception:
        data = msg.payload.decode(errors="replace")
    if str(data).strip().lower() == "sleep":
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
