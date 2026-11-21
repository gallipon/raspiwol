#!/usr/bin/env python3
"""raspiwol コマンド送信ツール: python3 raspi_cmd.py <command>

送り先（2026-11-21〜、Beebotte から自前ブローカーへの移行中）:
- RASPI_MQTT_PASS があれば自前ブローカー（VPS の Mosquitto、wss、ユーザー cli）
- 無ければ Beebotte（BEEBOTTE_TOKEN）
どちらか一方にだけ送る（Pi は両方を購読しているので、両方へ送ると二度実行される）。
"""

import json
import os
import sys
import time
import paho.mqtt.client as mqtt

TOKEN    = os.environ.get("BEEBOTTE_TOKEN", "")
MQ_PASS  = os.environ.get("RASPI_MQTT_PASS", "")
MQ_HOST  = os.environ.get("RASPI_MQTT_HOST", "gallipon.f5.si")
if not TOKEN and not MQ_PASS:
    print("Error: 環境変数 RASPI_MQTT_PASS か BEEBOTTE_TOKEN を設定してください")
    print("  export RASPI_MQTT_PASS=...        # Mac/Linux（自前ブローカー）")
    print("  $env:RASPI_MQTT_PASS='...'        # PowerShell")
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
    if msg.retain or not msg.payload:
        return                      # 接続前のログ（retained）は今回の返事ではない
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


if MQ_PASS:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="raspi_cli-%d" % os.getpid(),
        transport="websockets",
    )
    client.ws_set_options(path="/mqtt")
    client.tls_set()
    client.username_pw_set("cli", MQ_PASS)
    broker = (MQ_HOST, 443)
else:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="raspi_cli",
    )
    client.username_pw_set(TOKEN)
    broker = ("mqtt.beebotte.com", 1883)
client.on_connect    = on_connect
client.on_subscribe  = on_subscribe
client.on_message    = on_message

client.connect(*broker, keepalive=60)
client.loop_start()

deadline = time.time() + TIMEOUT
while not done and time.time() < deadline:
    time.sleep(0.1)

client.loop_stop()
client.disconnect()
