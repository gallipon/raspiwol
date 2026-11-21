#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""退勤コマンド: PC 常駐エージェント (pcsleep_agent.py) にスリープを指示する。

出社日は Slack に「終了」を書かないので、帰る前にこれを叩いて予約する。
既定 10 分後 + 直前 5 分アイドルでスリープ（作業を再開したら自動で延期）。

  python pcsleep_cmd.py           # 10 分後（既定）
  python pcsleep_cmd.py 20        # 20 分後
  python pcsleep_cmd.py now       # 即時スリープ
  python pcsleep_cmd.py cancel    # 予約取消

送り先は2つ（2026-11-21〜、Beebotte から自前ブローカーへの移行中）:
- PCSLEEP_BYE_PASS があれば、自前ブローカー（VPS の Mosquitto、wss、ユーザー bye）へ
  retained・QoS1 で送る。paho が要る（エージェントと同じ PC なので入っている）
- 無いとき、または自前ブローカーへ送れなかったときは、Beebotte の raspi3b/pcsleep_req へ
  REST で write（永続）する。BEEBOTTE_TOKEN が要る
エージェントはブローカーごとに一度だけ実行するので、仮に両方へ届いても予約がかけ直されるだけ。
"""
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request

TOKEN    = os.environ.get("BEEBOTTE_TOKEN", "")
BYE_PASS = os.environ.get("PCSLEEP_BYE_PASS", "")
MQ_HOST  = os.environ.get("PCSLEEP_MQTT_HOST", "gallipon.f5.si")
CHANNEL  = "raspi3b"
RESOURCE = "pcsleep_req"
DEFAULT_MIN = 10

if not TOKEN and not BYE_PASS:
    print("Error: 環境変数 PCSLEEP_BYE_PASS か BEEBOTTE_TOKEN を設定してください", file=sys.stderr)
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


def send_mq():
    """自前ブローカーへ送る。ブローカーの受け取り（PUBACK）まで確かめられたら True。"""
    try:
        import paho.mqtt.client as mqtt
        c = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                        client_id="pcsleep_cmd-%d" % os.getpid(), transport="websockets")
        c.ws_set_options(path="/mqtt")
        c.tls_set()
        c.username_pw_set("bye", BYE_PASS)
        c.connect(MQ_HOST, 443, keepalive=30)
        c.loop_start()
        info = c.publish("%s/%s" % (CHANNEL, RESOURCE),
                         json.dumps({"data": cmd, "ts": int(time.time() * 1000)}),
                         qos=1, retain=True)
        info.wait_for_publish(10)
        ok = info.is_published()
        c.disconnect()
        c.loop_stop()
        return ok
    except Exception as e:
        print("自前ブローカーへ送れませんでした: %s" % e, file=sys.stderr)
        return False


if BYE_PASS:
    if send_mq():
        print("→ %s/%s: %s (mq)" % (CHANNEL, RESOURCE, cmd))
        sys.exit(0)
    if not TOKEN:
        print("Error: 送信に失敗しました（BEEBOTTE_TOKEN が無いので送り直せません）", file=sys.stderr)
        sys.exit(1)
    print("Beebotte へ送り直します", file=sys.stderr)

# api.beebotte.com は中間証明書を送ってこないため OpenSSL の検証が通らない
# （curl やブラウザは AIA で補完するので成功する）。エージェント側と同じ回避策。
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

req = urllib.request.Request(
    "https://api.beebotte.com/v1/data/write/%s/%s" % (CHANNEL, RESOURCE),
    data=json.dumps({"data": cmd}).encode(),
    headers={"X-Auth-Token": TOKEN, "Content-Type": "application/json"},
    method="POST")
try:
    with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
        body = r.read().decode(errors="replace").strip()
except urllib.error.HTTPError as e:
    detail = e.read().decode(errors="replace").strip()
    print("Error: write 失敗 HTTP %d %s" % (e.code, detail), file=sys.stderr)
    if e.code == 404:
        print("Beebotte コンソールで %s/%s リソースを作成してください"
              % (CHANNEL, RESOURCE), file=sys.stderr)
    sys.exit(1)
except OSError as e:
    print("Error: write 失敗 %s" % e, file=sys.stderr)
    sys.exit(1)

print("→ %s/%s: %s (%s)" % (CHANNEL, RESOURCE, cmd, body))
