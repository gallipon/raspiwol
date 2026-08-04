#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""退勤コマンド: PC 常駐エージェント (pcsleep_agent.py) にスリープを指示する。

出社日は Slack に「終了」を書かないので、帰る前にこれを叩いて予約する。
既定 10 分後 + 直前 5 分アイドルでスリープ（作業を再開したら自動で延期）。

  python pcsleep_cmd.py           # 10 分後（既定）
  python pcsleep_cmd.py 20        # 20 分後
  python pcsleep_cmd.py now       # 即時スリープ
  python pcsleep_cmd.py cancel    # 予約取消

BEEBOTTE_TOKEN 環境変数が必要（エージェントと同じトークン）。
raspi_cmd.py と違い raspi3b/pcsleep へ publish する（Pi ではなく PC が受け取る）。
"""
import json
import os
import sys

import paho.mqtt.client as mqtt

TOKEN = os.environ.get("BEEBOTTE_TOKEN", "")
TOPIC = "raspi3b/pcsleep"
DEFAULT_MIN = 10

if not TOKEN:
    print("Error: 環境変数 BEEBOTTE_TOKEN を設定してください", file=sys.stderr)
    sys.exit(1)

arg = (sys.argv[1] if len(sys.argv) > 1 else str(DEFAULT_MIN)).strip().lower()
if arg == "now":
    cmd = "sleep"
elif arg == "cancel":
    cmd = "cancel"
else:
    try:
        minutes = int(arg)
    except ValueError:
        print("Usage: pcsleep_cmd.py [分 | now | cancel]", file=sys.stderr)
        sys.exit(1)
    cmd = "sleep_in %d" % minutes

client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="pcsleep_cli",
)
client.username_pw_set(TOKEN)
client.connect("mqtt.beebotte.com", 1883, keepalive=30)
client.loop_start()

info = client.publish(TOPIC, json.dumps({"data": cmd}))
info.wait_for_publish(timeout=10)   # QoS0: 送信完了までプロセスを落とさない

client.loop_stop()
client.disconnect()
print("→ %s: %s" % (TOPIC, cmd))
