# raspiwol

会社の有線 LAN に常設した Raspberry Pi 3B を踏み台に、**外出先から会社 PC を電源管理する**リモート運用システム。Wake-on-LAN による起動を軸に、確実なスリープ（PC 常駐エージェント）、スマホ/PC 用の Web ダッシュボード、平日スケジュール＋勤怠 Slack 連動の自動 Wake/Sleep、ネットワーク冗長化と out-of-band 復旧経路までを、**現地に行けるのは月数回**という制約のもとで「壊れない・遠隔で直せる」ことを最優先に設計している。

> マイコン版（Wio Terminal / M5Stack Core）: [m5stackwol](https://github.com/gallipon/m5stackwol)

---

## 主な設計判断（Design highlights）

- **SD カードを守る read-only root（overlayfs）** — 書き込みをすべて RAM に逃がし、摩耗ゼロ・電源断でも壊れない。24時間運用×遠隔保守への最適化。
- **資格情報を持たせないスリープ設計** — PC を寝かせるのは「PC 自身」。Pi に SSH 鍵もパスワードも置かず、トークンが漏れても侵入経路にならない。
- **デュアルホーム＋自動フェイルオーバー** — eth0（有線・主）と wlan0（WiFi・予備）でインターネット経路を二重化。安定した有線を生命線に。
- **out-of-band 復旧経路（Tailscale）** — eth0/VPN が落ちても tailnet 経由で Pi に到達。会社本網は晒さない設定で導入。
- **イベント駆動＋スケジュールの自動化** — 平日朝の自動 Wake、終業後の idle スリープ、勤怠 Slack 連動を、1つの**マスタースイッチ（autopilot）**で統括。
- **会社網フレンドリー** — 非標準ポートが塞がれた環境前提に、ダッシュボードは **Beebotte REST API（HTTPS 443）** だけで動く1枚もの。
- **SSH 不要のリモート更新** — MQTT `update` で GitHub から自己更新。overlayfs 中でも永続化できる。

---

## アーキテクチャ

```
                ┌─────────────────────────── Beebotte (MQTT 1883 / REST 443) ───────────────────────────┐
                │                                                                                        │
 [スマホ/PC]    │   [VPS: Apache+HTTPS]                 [Raspberry Pi 3B / 会社有線LAN]        [対象PC]   │
   dashboard ───┤── dashboard.html ──pub/read──┐   ┌── raspiwol.py (daemon) ──WOL─────────────→ officepc
   (REST 443)   │                              ├───┤      ├ GPIO 電源ボタン ─────────────────→ (PWR_SW)
                │   slack_sleep_poll.php        │   │      ├ status / ping / netcheck
   勤怠 Slack ──┼── (cron, User Token) ──pub────┘   │      └ scheduled wake (平日 08:50 timer)
   「終了」     │                                   │
                │   pcsleep_agent.py (PC 常駐) ──sub─┘   ← "sleep" 受信で SetSuspendState
                │      └ idle/終業後 自動スリープ（autopilot 連動）
                └────────────────────────────────────────────────────────────────────────────────────────┘
        master switch: Beebotte "autopilot"（dashboard が write、Pi/PC が参照）
        out-of-band: Pi に Tailscale（eth0 障害時も wlan0 経由で SSH 復旧）
```

| コンポーネント | 役割 | 動作環境 |
|---|---|---|
| `raspiwol.py` | MQTT デーモン本体（WOL・GPIO・診断・自己更新・平日朝 Wake） | Raspberry Pi 3B |
| `dashboard.html` | 電源ステータス＋Wake/Sleep＋autopilot トグル（REST 直叩き・1枚もの） | VPS（静的配信） |
| `pcsleep_agent.py` | PC 常駐。`sleep` 受信で SetSuspendState＋終業後 idle 自動スリープ | 対象 PC（Windows） |
| `slack_sleep_poll.php` | 勤怠 Slack の「終了」投稿を拾って `sleep` を publish | VPS（cron） |
| `raspiwol-wake.{service,timer}` | 平日 08:50 に `raspiwol.py wake` を起動 | Raspberry Pi 3B |

---

## なぜ overlayfs（read-only root）か

この Pi は会社の有線 LAN に常時接続し、24時間つけっぱなしで運用する。一方で**現地に行けるのは月に数回**しかないため、「SD カードが突然壊れてリモートで何もできなくなる」事態を避けることが最優先になる。

**SD カード（フラッシュメモリ）は書き込み回数に寿命がある。** ログやテンポラリファイルへの書き込みが 24時間続くと、いずれ書き込み限界に達してファイルシステムが壊れ、起動不能になり得る。加えて AC 電源で常時稼働させる都合上、**不意の停電・電源断で書き込み中の SD が破損する**リスクもある。

そこで root (`/`) を **overlayfs（tmpfs を上位レイヤにした read-only 構成）** にしている:

- 起動後の書き込みはすべて **RAM 上**に乗り、SD には一切書かれない → **書き込み摩耗ゼロ・電源断でも破損しない**
- 代償として、稼働中に行った変更は**再起動で消える**（揮発）
- 永続させたいもの（サービスコード・設定・デバイス一覧）は **`/boot/firmware`（FAT32・overlayfs 対象外）** に置く。普段は read-only、更新時だけ一時的に rw へ remount して書き込む（`update` コマンドや手動更新が自動で行う）
- OS 設定（NetworkManager・systemd ユニット等）を永続させたい時は `overlayroot-chroot` でベース層（read-only 下層）を直接編集する

結果として、**通常運用では SD への書き込みが発生しない**ため、カード寿命と停電耐性を最大化しつつ、コードはリモート（MQTT `update`）で更新できる構成になっている。

---

## リモートダッシュボード（dashboard.html）

ブラウザから **Beebotte REST API（HTTPS 443）** に直接 fetch する1枚もの（外部ライブラリ不使用）。電源ステータス表示、Wake/Sleep ボタン、自動運転（autopilot）トグルを提供する。

- **なぜ REST か**：会社網は非標準ポート（MQTT 1883/8883、WebSocket 8084 等）を塞いでおり、ブラウザからの MQTT-over-WS が通らない。Beebotte REST は **CORS 全開（`Access-Control-Allow-Origin: *`）かつ 443 のみ**で通るため、会社 PC でもスマホでも確実に動く（`file://` でも動作）。
- **ステータス**：ページが `ping <PC_IP>` を publish → Pi が ping して結果を `power` リソースへ REST write → ページが read して 🟢ON / ⚫OFF 表示。前面タブかつフォーカス時のみポーリングし、Beebotte の無料メッセージ枠を節約。
- **トークンの渡し方**：URL フラグメント（`dashboard.html#token_xxx`）。フラグメントはサーバへ送信されないため、VPS のアクセスログにも Referer にも残らない。
- **デプロイ**：自前 VPS（Apache2 + Let's Encrypt HTTPS）の推測しにくいパス＋Basic 認証。ローカル PC が OFF でも iPad/Android から操作できる。

---

## 確実な PC スリープ（方式2：資格情報を Pi に置かない）

当初は GPIO 電源ボタンの短押しで寝かせていたが、**S3 移行中に同じ押下が「起床トリガ」と誤認され、寝た直後に起き返す**現象が判明（`powercfg /lastwake` = 電源ボタン）。押下を短くしても安定しなかった。

そこで **GPIO を使わず、PC 自身に `SetSuspendState` を実行させる方式2**へ移行した。

- `pcsleep_agent.py`（PC 常駐）が Beebotte の `pcsleep` リソースを購読し、`"sleep"` 受信で `SetSuspendState(0,0,0)` を実行（スリープ。WOL 復帰可）。対話セッションで確実に動く。
- **セキュリティ設計**：Pi に PC の資格情報（SSH 鍵・パスワード）を**一切置かない**。PC は Beebotte へ**アウトバウンド接続するだけ**で、できるのは「自分を寝かせる」ことのみ。トークンが漏れても侵入経路にならない（Pi から PC へ SSH する方式1より乗っ取り耐性が高い）。
- **自動起動**：Windows タスクスケジューラ「ログオン時」＋「ユーザーがログオン中のみ実行」（対話セッション必須のため）。`pythonw.exe` で窓なし常駐。

---

## ネットワーク冗長化と Tailscale out-of-band 復旧

Pi を eth0（会社有線）と wlan0（ゲスト WiFi）の**両方に接続**し、インターネット経路を冗長化している。

| IF | 役割 | metric |
|---|---|---|
| eth0（有線） | **主**：PC 制御(WOL/ping)＋インターネット | 50 |
| wlan0（WiFi） | **予備**：インターネットのバックアップ | 600 |

- eth0 が落ちれば NetworkManager がメトリック差で wlan0 へ自動フェイルオーバー。**有線を生命線**にしたのは、ゲスト WiFi が「繋がっているのにインターネットだけ死ぬ」故障を起こしやすいため。
- 予備リンクは即応すべきなので **wlan0 の WiFi 省電力を off** にしている（起床遅延で疎通判定が揺れるのを防ぐ）。
- NetworkManager 設定は overlayfs（RAM）上にあるため、`overlayroot-chroot` でベース層に焼いて永続化している。

**out-of-band 復旧（Tailscale）**：VPN は eth0 セグメントにしか繋がっておらず、eth0 が物理的に落ちると SSH できなくなる。そこで Pi に Tailscale を入れ、**tailnet 経由で eth0/VPN 非依存の到達経路**を確保した。

- `--advertise-routes=`（空）で**サブネットルーティングを無効化**し、会社本網を tailnet に晒さない。
- 認証済み状態をベース層へ焼いて reboot 後も同一ノードで自動復帰（auth key ローテーション不要）。
- MagicDNS は `--accept-dns=false` で無効化（有効だと resolv.conf を乗っ取られ、公開名の解決ができなくなる落とし穴がある）。

---

## 平日 自動 Wake/Sleep ＋ マスタースイッチ

平日の朝に PC を自動起動し、終業後にアイドルで自動スリープする。ON/OFF は Beebotte の **`autopilot` リソース（dashboard トグル＝単一の真実）**で一括制御する。

- **朝 Wake（Pi）**：systemd timer（平日 08:50）→ `raspiwol.py wake`。出勤日判定に通り、かつ autopilot が ON のときだけ WOL を送る。
  - **祝日判定は内閣府の祝日 CSV**（公式・国民の振替休日込み）を取得して行う＝**外部ライブラリ依存なし**。
  - **会社固有ルール**：土曜に祝日が被ると翌月曜が振替休（祝日法には無いので独自に計算）。
  - 年末年始など不定期の休みは autopilot を OFF にして対応（＝休暇・出張時も一括停止できる）。
- **idle Sleep（PC）**：`pcsleep_agent.py` が 平日・終業時刻(19:00)以降・無操作30分（`GetLastInputInfo`）で自動スリープ。autopilot ON のときだけ。
- **手動/Slack の Sleep は autopilot に関係なく常に有効**（明示的な操作は尊重する）。

---

## Slack 連携スリープ（不可視）

勤怠チャンネルへ自分が「終了」を含む投稿をしたら、それを拾って PC をスリープさせる。

- 勤怠チャンネルは全員が見るため、**Bot をチャンネルに参加させない**設計にした（Bot 参加は「追加」通知とメンバー表示が出てしまう）。
- 代わりに **自分の User トークンで `conversations.history` を読むだけ**（チャンネルには何も表示されない）。VPS の cron が定期ポーリングし、自分の新規「終了」投稿を見つけたら `pcsleep` へ `"sleep"` を publish する。
- 読み取り専用スコープ（`channels:history`）のみ。Beebotte 経由なので、ここでも「できるのは PC を寝かせることだけ」という設計が保たれる。
- 認証情報（Slack/Beebotte トークン等）は本体から分離し、gitignore 済みの `slack_sleep_config.php` に置く（テンプレートは `slack_sleep_config.example.php`）。`.php` なので Apache 上では実行されソース＝トークンは配信されない。

---

## 診断・運用

| コマンド | 用途 |
|---|---|
| `status` | IP・稼働時間・デバイス一覧を返す |
| `ping <ip>` | 対象 IP の死活確認（ダッシュボードの電源ステータス） |
| `netcheck` | 各物理 IF(eth0/wlan0)経由の外部疎通＋Tailscale 接続状態を一度に確認（どの経路が死んだかの切り分け） |
| `update` | GitHub から最新の `raspiwol.py` を取得してサービス再起動 |

> **運用知見**：`update` は GitHub raw 取得のため、push 直後は CDN が旧版を数分キャッシュする。急ぐ時は `/tmp` 経由の SSH 手動配置（CDN 非経由・即時）を使う。

---

## ドキュメント

| ドキュメント | 内容 |
|---|---|
| **[SETUP.md](./SETUP.md)** | 必要機材・Pi コアの初期セットアップ・設定ファイル（`raspiwol.ini` / デバイス CSV） |
| **[OPERATIONS.md](./OPERATIONS.md)** | MQTT コマンド一覧・GPIO 電源ボタン配線・更新方法・トラブルシュート |
