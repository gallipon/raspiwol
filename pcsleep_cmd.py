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

raspi3b/pcsleep_req へ REST で write（永続）する。エージェントはこれを REST で
ポーリングするため、Beebotte の MQTT が落ちていても届く（2026-09-22 の障害で
MQTT publish 方式は全滅した）。pip 依存も無くなった（paho 不要）。
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

TOKEN    = os.environ.get("BEEBOTTE_TOKEN", "")
CHANNEL  = "raspi3b"
RESOURCE = "pcsleep_req"
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
