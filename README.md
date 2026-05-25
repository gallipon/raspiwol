# raspiwol

Raspberry Pi 3B を会社の有線 LAN に設置し、MQTT (Beebotte) 経由で Wake-on-LAN マジックパケットを送信するデーモン。

```
スマホ / PC → Beebotte MQTT → Raspberry Pi 3B（有線LAN）→ WOL        → 対象PC
                                                          → GPIO 電源ボタン → 対象PC
```

---

## 必要なもの

| 項目 | 内容 |
|---|---|
| ハードウェア | Raspberry Pi 3B（有線 Ethernet 使用） |
| OS | Raspberry Pi OS Lite (Bookworm 推奨) |
| MQTT | Beebotte アカウント・チャンネル |
| 電源 | USB-C ACアダプター（PCの電源に依存しないもの） |
| GPIO 電源ボタン（任意） | 1kΩ 抵抗、ジャンパーワイヤー、ブレッドボード |

---

## 初期セットアップ

### 1. Pi OS を SD カードに書き込む

Raspberry Pi Imager で以下を設定してから書き込む:
- OS: Raspberry Pi OS Lite (64-bit)
- ホスト名: `raspi3b`
- SSH: 有効
- ユーザー名・パスワード: 任意（例: `gallipon`）
- WiFi: 設定しない（有線のみ使用）

### 2. このリポジトリを Pi に転送する

Pi と同じ LAN にいる PC から:

```bash
scp -r raspiwol/ USER@raspi3b.local:/home/USER/
```

### 3. setup.sh を実行する

Pi に SSH して:

```bash
cd /home/USER/raspiwol
sudo bash setup.sh
```

対話式で以下を入力する:

| 項目 | 例（会社環境） |
|---|---|
| 静的 IP | `192.168.11.46` |
| サブネット prefix | `24` |
| ゲートウェイ | `192.168.11.1` |
| DNS | （空欄でゲートウェイを使用） |
| Beebotte トークン | `token_XXXXXXXXXXXX` |
| コマンドトピック | `raspi3b/wol` |
| ログトピック | `raspi3b/log` |
| update URL | `https://raw.githubusercontent.com/gallipon/raspiwol/main/raspiwol.py` |

### 4. overlayroot パッケージをインストールする

setup.sh 完了後、**reboot 前に**インターネット接続がある状態で:

```bash
sudo apt install -y overlayroot
```

> setup.sh 内の `raspi-config nonint do_overlayfs 0` は overlayroot パッケージが必要。
> インターネット接続がない状態で実行すると apt が失敗するため、手動でインストールする。

### 5. cmdline.txt に overlayroot=tmpfs を追記する

```bash
sudo bash -c 'echo "overlayroot=tmpfs $(cat /boot/firmware/cmdline.txt)" > /boot/firmware/cmdline.txt'
cat /boot/firmware/cmdline.txt  # 確認
```

### 6. 再起動する

```bash
sudo reboot
```

### 7. 動作確認

```bash
# overlayfs が有効か確認
mount | grep overlay

# サービスログ確認
sudo journalctl -u raspiwol -f
```

Beebotte から `{"data": "status"}` を送信して応答が返れば完了。

---

## MQTT コマンド一覧

Beebotte のチャンネルトピック（`raspi3b/wol`）に以下の JSON を送信する:

| data フィールド | 動作 |
|---|---|
| `"desktopmuk"` | WOL パケット送信（会社デスクトップ） |
| `"macmini"` | WOL パケット送信（Mac Mini） |
| `"pwrbtn"` | GPIO 電源ボタン 短押し 0.2秒（電源ON） |
| `"pwrbtn_long"` | GPIO 電源ボタン 長押し 5秒（強制電源OFF） |
| `"pwrbtn_10s"` | GPIO 電源ボタン 10秒押し（完全強制電源OFF） |
| `"status"` | IP・稼働時間・デバイス一覧を `raspi3b/log` に返信 |
| `"update"` | GitHub から最新の raspiwol.py を取得してサービス再起動 |
| `"reboot"` | Pi を再起動 |

送信フォーマット:
```json
{"data": "status"}
```

応答は `raspi3b/log` トピックに届く。

---

## GPIO 電源ボタン接続（任意）

WOL が効かない場合（完全シャットダウン時など）の補完として、Raspberry Pi の GPIO をマザーボードの PWR_SW ヘッダに接続して電源ボタンを物理的に操作できる。

### 必要部品

- 1kΩ 抵抗 × 1
- ジャンパーワイヤー（メス-メス、メス-オス）
- ブレッドボード（小型可）

### 配線

```
マザボ PWR_SW ─── 分岐 ┬── 元の電源ボタン（そのまま）
                        └── [1kΩ抵抗] ── Pi GPIO17 (物理ピン 11)

マザボ GND ────── 分岐 ┬── 元の電源ボタン（そのまま）
                        └──────────────── Pi GND    (物理ピン 6)
```

抵抗はブレッドボード上に挿し、ジャンパーワイヤーで橋渡しする。GND 側は抵抗不要。

> **注意**: 対象 PC の PWR_SW スタンバイ電圧が 3.3V であることを確認すること。  
> Z690 など現代のマザーボードはほぼ 3.3V。5V の場合は Pi GPIO が破損するおそれがある。

### GPIO ピン番号の変更

デフォルトは BCM17（物理ピン 11）。`raspiwol.ini` の `[gpio]` セクションで変更できる:

```ini
[gpio]
pwr_pin = 27  ; BCM 番号で指定
```

---

## コードの更新方法

1. `raspiwol.py` を編集
2. GitHub に push:
   ```bash
   git add raspiwol.py
   git commit -m "変更内容"
   git push
   ```
3. Beebotte から `{"data": "update"}` を送信

Pi が自動でダウンロード・サービス再起動する。SSH 不要。

---

## 設定ファイル（Pi のみ・Git 管理外）

`/boot/firmware/raspiwol.ini`:

```ini
[mqtt]
host = mqtt.beebotte.com
port = 1883
token = token_XXXXXXXXXXXX
topic_cmd = raspi3b/wol
topic_log = raspi3b/log

[update]
url = https://raw.githubusercontent.com/gallipon/raspiwol/main/raspiwol.py

[gpio]
; 電源ボタン用 GPIO ピン番号（BCM 番号、デフォルト 17 = 物理ピン 11）
pwr_pin = 17
```

> `raspiwol.ini` はトークンを含むため Git に含めない（`.gitignore` 済み）。

---

## デバイスリスト

`/boot/firmware/raspiwol_devices.csv`（`devices.csv` から Pi にコピーされる）:

```
# name, mac
desktopmuk, d8:bb:c1:df:91:36
macmini, c8:2a:14:55:b0:65
```

デバイスを追加する場合は CSV を編集して Pi の `/boot/firmware/raspiwol_devices.csv` に上書きコピーする（overlayfs 環境では `/boot/firmware` は書き込み可）。

---

## トラブルシューティング

### MQTT に接続しない

```bash
# 接続状態確認
ss -tn | grep 1883

# サービスログ確認
sudo journalctl -u raspiwol -n 50
```

接続が確立していない場合はインターネット疎通を確認:
```bash
ping -c 3 8.8.8.8
```

### /boot/firmware が read-only になっている

```bash
sudo mount -o remount,rw /boot/firmware
```

fstab に `ro` が残っている場合は削除:
```bash
sudo sed -i 's|defaults,ro|defaults|' /etc/fstab
sudo systemctl daemon-reload
```

### overlayfs が有効にならない

```bash
# overlayroot パッケージの確認
dpkg -l overlayroot

# cmdline.txt の確認
cat /boot/firmware/cmdline.txt
# 先頭に overlayroot=tmpfs があること

# 手動追記（まだない場合）
sudo bash -c 'echo "overlayroot=tmpfs $(cat /boot/firmware/cmdline.txt)" > /boot/firmware/cmdline.txt'
```

### SSH 接続先

| 環境 | コマンド |
|---|---|
| 自宅（mDNS） | `ssh gallipon@raspi3b.local` |
| 会社（VPN 経由） | `ssh gallipon@192.168.11.46` |
