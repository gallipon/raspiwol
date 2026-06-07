#!/usr/bin/env python3
"""raspiwol コマンド送信ツール: python3 raspi_cmd.py <command>"""

import json
import os
import sys
import time
import paho.mqtt.client as mqtt

TOKEN = os.environ.get("BEEBOTTE_TOKEN", "")
if not TOKEN:
    print("Error: 環境変数 BEEBOTTE_TOKEN を設定してください")
    print("  export BEEBOTTE_TOKEN=token_XXXX  # Mac/Linux")
    print("  $env:BEEBOTTE_TOKEN='token_XXXX'  # PowerShell")
    sys.exit(1)

TOPIC_CMD = "raspi3b/wol"
TOPIC_LOG = "raspi3b/log"
TIMEOUT   = 60  # 秒（update は再起動待ちがあるため長めに）

cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
done = False


def on_connect(client, userdata, flags, reason_code, properties):
    client.subscribe(TOPIC_LOG)


def on_subscribe(client, userdata, mid, reason_codes, properties):
    client.publish(TOPIC_CMD, json.dumps({"data": cmd}))


def on_message(client, userdata, msg):
    global done
    payload = json.loads(msg.payload)
    data = payload.get("data", "")
    if data == "offline":
        return
    print(data)
    if cmd == "update":
        # connected が来たら更新完了
        if isinstance(data, str) and data.startswith("connected"):
            done = True
    elif cmd not in ("pwrbtn_long", "pwrbtn_10s"):
        done = True


client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="raspi_cli",
)
client.username_pw_set(TOKEN)
client.on_connect    = on_connect
client.on_subscribe  = on_subscribe
client.on_message    = on_message

client.connect("mqtt.beebotte.com", 1883, keepalive=60)
client.loop_start()

deadline = time.time() + TIMEOUT
while not done and time.time() < deadline:
    time.sleep(0.1)

client.loop_stop()
client.disconnect()
