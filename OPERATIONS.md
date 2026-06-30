# 運用・リファレンス

設計の概要は [README](./README.md)、導入手順は [SETUP](./SETUP.md) を参照。このドキュメントは MQTT コマンド一覧・GPIO 配線・更新方法・トラブルシュートをまとめる。

---

## MQTT コマンド一覧

Beebotte のコマンドトピック（`raspi3b/wol`）に `{"data": "<コマンド>"}` を送信する。応答は `raspi3b/log` に届く。

| data | 動作 |
|---|---|
| `"officepc"` / `"macmini"` | WOL パケット送信（デバイス名は `raspiwol_devices.csv`） |
| `"pwrbtn"` | GPIO 電源ボタン 短押し（既定 0.1 秒・電源ON 用） |
| `"pwrbtn_long"` | GPIO 電源ボタン 長押し 5秒（強制電源OFF） |
| `"pwrbtn_10s"` | GPIO 電源ボタン 10秒押し（完全強制電源OFF） |
| `"status"` | IP・稼働時間・デバイス一覧を返信 |
| `"ping <ip>"` | 指定 IP の死活確認（ダッシュボードの電源ステータス用） |
| `"netcheck"` | 各 IF の外部疎通＋Tailscale 状態（`eth0=up wlan0=up tailscale=running`） |
| `"update"` | GitHub から最新コードを取得してサービス再起動 |
| `"reboot"` / `"shutdown"` | Pi を再起動 / シャットダウン |

スリープ系は別リソースに送る：Web/Slack は `pcsleep` へ `"sleep"`（PC エージェントが実行）、自動運転の ON/OFF は `autopilot` へ `"on"/"off"`。

---

## GPIO 電源ボタン接続（任意）

WOL が効かない完全シャットダウン時の補完として、Pi の GPIO をマザーボードの PWR_SW ヘッダに接続し電源ボタンを物理操作できる。

```
マザボ PWR_SW ─── 分岐 ┬── 元の電源ボタン（そのまま）
                        └── [1kΩ抵抗] ── Pi GPIO17 (物理ピン 11)
マザボ GND ────── 分岐 ┬── 元の電源ボタン（そのまま）
                        └──────────────── Pi GND    (物理ピン 6)
```

> **注意**: 対象 PC の PWR_SW スタンバイ電圧が 3.3V であることを確認すること（5V だと Pi GPIO が破損するおそれ）。GPIO ピンは `raspiwol.ini` の `[gpio] pwr_pin`（BCM 番号）で変更可。

---

## コードの更新方法

1. `raspiwol.py` を編集して GitHub に push
2. Beebotte から `{"data": "update"}` を送信 → Pi が自動でダウンロード・サービス再起動（SSH 不要）

> push から数分は CDN キャッシュで旧版が返ることがある。即時反映したい時は SSH で `/boot/firmware/raspiwol.py` を手動配置する。

---

## トラブルシューティング

### MQTT に接続しない
```bash
ss -tn | grep 1883
sudo journalctl -u raspiwol -n 50
ping -c 3 8.8.8.8
getent hosts mqtt.beebotte.com    # 名前解決の確認（Tailscale MagicDNS で壊れていないか）
```

### /boot/firmware が read-only
```bash
sudo mount -o remount,rw /boot/firmware
```

### overlayfs が有効にならない
```bash
dpkg -l overlayroot
cat /boot/firmware/cmdline.txt    # 先頭に overlayroot=tmpfs があること
```

### SSH 接続先
| 環境 | コマンド |
|---|---|
| 同一 LAN（mDNS） | `ssh USER@raspi3b.local` |
| リモート（VPN 経由） | `ssh USER@<静的IP>` |
| eth0 障害時など | `ssh USER@<Tailscale の 100.x>`（out-of-band） |
