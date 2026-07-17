# セットアップ

設計の概要は [README](./README.md) を参照。このドキュメントは Pi コアの導入手順・必要機材・設定ファイルをまとめる。

---

## 必要なもの

| 項目 | 内容 |
|---|---|
| ハードウェア | Raspberry Pi 3B（有線 Ethernet 使用） |
| OS | Raspberry Pi OS Lite (Bookworm 以降) |
| MQTT | Beebotte アカウント・チャンネル |
| 電源 | USB-C ACアダプター（PCの電源に依存しないもの） |
| GPIO 電源ボタン（任意） | 1kΩ 抵抗、ジャンパーワイヤー、ブレッドボード |
| 拡張（任意） | VPS（ダッシュボード/Slack 連携）、Tailscale アカウント |

---

## 初期セットアップ（Pi コア）

### 1. Pi OS を SD カードに書き込む

Raspberry Pi Imager で以下を設定してから書き込む:
- OS: Raspberry Pi OS Lite (64-bit) / ホスト名: `raspi3b` / SSH: 有効 / WiFi: 任意

### 2. このリポジトリを Pi に転送して setup.sh を実行する

```bash
scp -r raspiwol/ USER@raspi3b.local:/home/USER/
ssh USER@raspi3b.local
cd /home/USER/raspiwol && sudo bash setup.sh
```

対話式で 静的 IP / ゲートウェイ / Beebotte トークン / トピック / update URL を入力する。

### 3. overlayroot を入れて read-only 化する

```bash
sudo apt install -y overlayroot                                   # reboot 前に・ネット接続ありで
sudo bash -c 'echo "overlayroot=tmpfs $(cat /boot/firmware/cmdline.txt)" > /boot/firmware/cmdline.txt'
sudo reboot
```

### 4. 動作確認

```bash
mount | grep overlay            # overlayfs が有効か
sudo journalctl -u raspiwol -f  # サービスログ
```

Beebotte から `{"data": "status"}` を送って応答が返れば完了。

### 拡張コンポーネントの導入（任意）

| 機能 | 概要 |
|---|---|
| ダッシュボード | `dashboard.html` を VPS に配置（Basic 認証推奨）。Beebotte に `power`・`autopilot`・**`agent`** リソースを作成 |
| PC スリープ | `pcsleep_agent.py` を PC に常駐（タスクスケジューラ・`pythonw`）。`BEEBOTTE_TOKEN` を環境変数に |
| 自動 Wake | `raspiwol-wake.{service,timer}` を `/etc/systemd/system/` に置き timer を enable（overlayfs なら base 層へ永続化） |
| nightwatch | `raspiwol-nightwatch.{service,timer}` を同様に配置・enable。`raspiwol.ini` の `[nightwatch]` に `ntfy_topic`（ntfy.sh トピック名）と `target_ip` を記入。スマホに ntfy アプリを入れてトピックを購読する |
| Slack スリープ | `slack_sleep_poll.php` ＋ `slack_sleep_config.php`（example をコピーして記入）を VPS に配置し cron 登録。Slack の User Token（`channels:history`）が必要 |
| **ゾンビ対策ウォッチドッグ** | `pcsleep_agent.py` がゾンビ検出時に `os._exit(1)` で終了するため、タスクスケジューラで 5分ごとに「プロセスが居なければ再起動」するウォッチドッグタスクを追加する（下記コマンド例参照） |

**Beebotte `agent` リソースの作成**：Beebotte コンソール → チャンネル `raspi3b` → リソース追加 → 名前 `agent`（`power`・`autopilot` と同手順）。未作成の場合 dashboard の Agent 表示は「⚫ 不明」になり GPIO ボタンは表示されない。

**ウォッチドッグタスクの作成例（PowerShell・管理者権限で実行）**：

ポイントは**「pcsleep_agent プロセスが不在のときだけ再起動する」ガード付きアクション**にすること。
起動自体は既存の `pcsleep_agent` タスク（対話セッション／ログオン時トリガー）を `Start-ScheduledTask` で
呼び直す＝起動方法の単一ソースを保つ。単純に pythonw を毎回起動する定義にすると 5分ごとに多重起動し、
同一 client_id で MQTT 上互いを蹴り合うので不可。

```powershell
# プロセス不在なら既存の pcsleep_agent タスクを起動する（ガード付き）
$guard = "if (-not (Get-CimInstance Win32_Process -Filter ""Name LIKE 'python%'"" | " +
         "Where-Object { $_.CommandLine -like '*pcsleep_agent*' })) { " +
         "Start-ScheduledTask -TaskName pcsleep_agent }"

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
               -Argument ("-NoProfile -WindowStyle Hidden -Command """ + $guard + """")
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
               -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive

$definition = New-ScheduledTask -Action $action -Trigger $trigger `
                  -Settings $settings -Principal $principal `
                  -Description "pcsleep_agent watchdog: start it if the process is absent"
Register-ScheduledTask -TaskName "pcsleep_agent_watchdog" -InputObject $definition
```

> `os._exit(1)` でプロセスが消えると、次回 5分スキャンでガードが不在を検知して `pcsleep_agent` タスクを
> 起動し直す。プロセスが生きている間はガードが弾くので二重起動しない。

---

## 設定ファイル（Pi のみ・Git 管理外）

`/boot/firmware/raspiwol.ini`（トークンを含むため `.gitignore` 済み。テンプレートは `config.ini.example`）:

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
pwr_pin = 17        ; BCM 番号（既定 17 = 物理ピン 11）

[wake]
target = officepc ; 平日朝の自動 Wake 対象（raspiwol_devices.csv の名前）
```

デバイスリスト `/boot/firmware/raspiwol_devices.csv`（`devices.csv` 由来）:

```
# name, mac
officepc, 00:11:22:33:44:55
macmini, 00:11:22:33:44:66
```
