#!/usr/bin/env python3
"""raspiwol - Raspberry Pi Wake-on-LAN daemon via Beebotte MQTT"""

import configparser
import csv
import json
import socket
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import paho.mqtt.client as mqtt

# systemd 配下では stdout がブロックバッファになり journalctl にログが出ない/遅延する。
# 行バッファ化して print() を即時 journald に流す（デバッグ可視性のため）。
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

# /boot/firmware (Bookworm) or /boot (Bullseye)
BOOT_DIR = Path("/boot/firmware") if Path("/boot/firmware").exists() else Path("/boot")
CONFIG_FILE = BOOT_DIR / "raspiwol.ini"
DEVICES_FILE = BOOT_DIR / "raspiwol_devices.csv"
SCRIPT_DEST = BOOT_DIR / "raspiwol.py"

cfg = configparser.ConfigParser()
cfg.read(CONFIG_FILE)

MQTT_HOST  = cfg.get("mqtt", "host",      fallback="mqtt.beebotte.com")
MQTT_PORT  = cfg.getint("mqtt", "port",   fallback=1883)
MQTT_TOKEN = cfg.get("mqtt", "token",     fallback="")
TOPIC_CMD  = cfg.get("mqtt", "topic_cmd", fallback="")
TOPIC_LOG  = cfg.get("mqtt", "topic_log", fallback="")
UPDATE_URL          = cfg.get("update", "url",          fallback="")
UPDATE_GITHUB_TOKEN = cfg.get("update", "github_token", fallback="")
PWR_GPIO   = cfg.getint("gpio", "pwr_pin", fallback=17)
# 短押し(pwrbtn)の長さ。長いと PC が S3 に入った後も押下が続き「同じ押下で起こし返す」
# (powercfg /lastwake=電源ボタン)現象が起きる。S3 に入る前に離せるよう短く（既定 0.1s）。
# 短すぎて押下が認識されない場合は raspiwol.ini の [gpio] short_sec で微調整。
PWR_SHORT  = cfg.getfloat("gpio", "short_sec", fallback=0.1)

# Beebotte REST API（ダッシュボードが read するステータスの永続化に使用）
API_BASE           = "https://api.beebotte.com/v1/data"
CHANNEL            = TOPIC_CMD.split("/")[0] if "/" in TOPIC_CMD else ""
POWER_RESOURCE     = "power"       # ※Beebotte チャンネルに事前に作成しておくこと
AUTOPILOT_RESOURCE = "autopilot"   # ※同上。自動Wake/Sleep のマスタ ON/OFF スイッチ
WAKE_TARGET = cfg.get("wake", "target", fallback="officepc")  # 朝Wake の対象デバイス

# name (lowercase) → mac
devices: dict[str, str] = {}
try:
    with open(DEVICES_FILE, newline="") as f:
        for row in csv.reader(f):
            if row and not row[0].startswith("#") and len(row) >= 2:
                devices[row[0].strip().lower()] = row[1].strip()
except FileNotFoundError:
    print(f"WARNING: {DEVICES_FILE} not found", file=sys.stderr)


# ── GPIO 電源ボタン ───────────────────────────────────────────────────────────

try:
    import RPi.GPIO as GPIO
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PWR_GPIO, GPIO.IN)
    _gpio_ok = True
except Exception:
    _gpio_ok = False

_press_lock = threading.Lock()


def _do_press(duration: float):
    try:
        GPIO.setup(PWR_GPIO, GPIO.OUT, initial=GPIO.LOW)
        time.sleep(duration)
    except Exception as e:
        try:
            pub(f"pwrbtn_error: {e}")
        except Exception:
            print(f"pwrbtn_error: {e}", file=sys.stderr)
    finally:
        try:
            GPIO.setup(PWR_GPIO, GPIO.IN)
        except Exception:
            pass
        _press_lock.release()


def press_power_button(duration: float) -> str:
    if not _gpio_ok:
        return "unavailable"
    if not _press_lock.acquire(blocking=False):
        return "busy"
    threading.Thread(target=_do_press, args=(duration,), daemon=True).start()
    return "ok"


# ── WOL ──────────────────────────────────────────────────────────────────────

def _broadcasts() -> list[str]:
    """全ての up な IPv4 インターフェースの broadcast を返す（255.255.255.255 含む）"""
    addrs = ["255.255.255.255"]
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            f = line.split()
            if "brd" in f:                      # 各 IF の実際の broadcast を採用（/23 等もOK）
                brd = f[f.index("brd") + 1]
                if brd not in addrs:
                    addrs.append(brd)
    except Exception as e:
        print(f"_broadcasts error: {e}", file=sys.stderr)
    return addrs


def send_wol(mac: str) -> bool:
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        magic = b"\xff" * 6 + mac_bytes * 16
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for addr in _broadcasts():
                s.sendto(magic, (addr, 9))
        return True
    except Exception as e:
        print(f"WOL error: {e}", file=sys.stderr)
        return False


# ── status ───────────────────────────────────────────────────────────────────

def local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return "unknown"


def uptime_str() -> str:
    try:
        secs = float(open("/proc/uptime").read().split()[0])
        h, rem = divmod(int(secs), 3600)
        return f"{h}h{rem // 60:02d}m"
    except Exception:
        return "unknown"


def _valid_ipv4(s: str) -> bool:
    parts = s.split(".")
    return len(parts) == 4 and all(
        p.isdigit() and 0 <= int(p) <= 255 for p in parts
    )


def ping_host(ip: str) -> bool:
    """対象 IP に ICMP echo を1発投げ、応答すれば True（起動・稼働中の判定）"""
    try:
        r = subprocess.run(
            ["ping", "-c", "1", "-W", "1", ip],
            capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def _interfaces() -> list[str]:
    """インターネット上りの物理 IF 名（eth0, wlan0 等）。tailscale0 等の
    仮想/トンネル IF はそこから外部到達できず疎通指標にならないので除外する。"""
    skip = ("tailscale", "wg", "tun", "tap", "docker", "veth", "br-")
    names: list[str] = []
    try:
        out = subprocess.run(
            ["ip", "-o", "-4", "addr", "show", "scope", "global"],
            capture_output=True, text=True,
        ).stdout
        for line in out.splitlines():
            f = line.split()
            if len(f) >= 2 and f[1] not in names and not f[1].startswith(skip):
                names.append(f[1])
    except Exception as e:
        print(f"_interfaces error: {e}", file=sys.stderr)
    return names


def ping_via(ifname: str, ip: str = "8.8.8.8") -> bool:
    """指定インターフェース経由で外部へ ICMP echo（経路ごとの疎通を切り分ける）。
    2発投げ1発でも応答すれば up。1発・1秒だと省電力で寝た WiFi(wlan0)の起床に
    間に合わず誤って down と出るため（初回パケットが遅延）、起床のもたつきを吸収する。"""
    try:
        r = subprocess.run(
            ["ping", "-I", ifname, "-c", "2", "-W", "2", ip],
            capture_output=True,
        )
        return r.returncode == 0
    except Exception:
        return False


def tailscale_state() -> str | None:
    """Tailscale バックエンドの状態（running/needslogin/stopped 等）を小文字で返す。
    out-of-band 救済経路の健全性指標。ping ではなく接続状態で見るのが正しい
    （tailscale0 は外部到達できず ping は無意味）。未導入なら None（netcheck で項目を出さない）。"""
    try:
        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5,
        )
    except FileNotFoundError:
        return None
    except Exception:
        return "down"
    try:
        return json.loads(r.stdout).get("BackendState", "?").lower()
    except Exception:
        return "down"


# ── /boot/firmware の一時的な書き込み許可 ─────────────────────────────────────

def remount_boot(rw: bool):
    mode = "rw" if rw else "ro"
    subprocess.run(["mount", "-o", f"remount,{mode}", str(BOOT_DIR)],
                   capture_output=True)


# ── MQTT ─────────────────────────────────────────────────────────────────────

mq = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="raspiwol",
)
mq.username_pw_set(MQTT_TOKEN)
mq.will_set(TOPIC_LOG, json.dumps({"data": "offline"}), retain=True)


def pub(msg: str):
    mq.publish(TOPIC_LOG, json.dumps({"data": msg}))
    print(f"PUB: {msg}")


# api.beebotte.com への HTTPS が CERTIFICATE_VERIFY_FAILED（unable to get local issuer
# certificate）になるため検証を無効化（curl -k 相当）。原因は未確定で、Pi の CA バンドルが
# 古い or Beebotte サーバが中間証明書を送っていない可能性（GitHub への HTTPS は通る）。
# ステータスを書くだけの内部用途なので無効化を許容。ca-certificates 更新で正攻法に直せるかも。
_SSL_NOVERIFY = ssl.create_default_context()
_SSL_NOVERIFY.check_hostname = False
_SSL_NOVERIFY.verify_mode = ssl.CERT_NONE


def bbt_write(resource: str, value: str):
    """Beebotte REST API で値を永続化（ダッシュボードが GET read で取得する）"""
    if not CHANNEL or not MQTT_TOKEN:
        return
    try:
        req = urllib.request.Request(
            f"{API_BASE}/write/{CHANNEL}/{resource}",
            data=json.dumps({"data": value}).encode(),
            method="POST",
        )
        req.add_header("X-Auth-Token", MQTT_TOKEN)
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=5, context=_SSL_NOVERIFY)
    except Exception as e:
        print(f"bbt_write error: {e}", file=sys.stderr)


def bbt_read(resource: str, default: str = "") -> str:
    """Beebotte REST API で最新値を1件読む（取得失敗時は default を返す）。"""
    if not CHANNEL or not MQTT_TOKEN:
        return default
    try:
        req = urllib.request.Request(f"{API_BASE}/read/{CHANNEL}/{resource}?limit=1")
        req.add_header("X-Auth-Token", MQTT_TOKEN)
        with urllib.request.urlopen(req, timeout=5, context=_SSL_NOVERIFY) as r:
            arr = json.loads(r.read())
        if arr:
            return str(arr[0].get("data", default))
    except Exception as e:
        print(f"bbt_read error: {e}", file=sys.stderr)
    return default


# ── 平日朝の自動 Wake（systemd timer から `raspiwol.py wake` で呼ばれる）─────────

# 内閣府の祝日 CSV（公式・国民の振替休日も含む）。pip 依存を避けるため urllib で取得。
HOLIDAY_CSV_URL = "https://www8.cao.go.jp/chosei/shukujitsu/syukujitsu.csv"


def _jp_holidays() -> set:
    """内閣府 CSV から祝日（＋日曜振替休日）の日付集合を返す。取得失敗時は空集合。"""
    import datetime
    hols: set = set()
    try:
        req = urllib.request.Request(HOLIDAY_CSV_URL)
        with urllib.request.urlopen(req, timeout=8, context=_SSL_NOVERIFY) as r:
            text = r.read().decode("cp932", errors="replace")   # CSV は Shift_JIS
        for line in text.splitlines():
            parts = line.split(",")[0].strip().split("/")        # 先頭列 "YYYY/M/D"
            if len(parts) == 3 and all(p.isdigit() for p in parts):
                y, m, day = map(int, parts)
                hols.add(datetime.date(y, m, day))
    except Exception as e:
        print(f"_jp_holidays fetch error: {e}", file=sys.stderr)
    return hols


def is_workday(d=None) -> bool:
    """出勤日なら True。平日(月〜金)かつ祝日でない。会社固有ルール「土曜に祝日が
    被ると翌月曜が振替休」も考慮する。年末年始は対象外（autopilot OFF 運用で対応）。
    祝日 CSV の取得に失敗した時は平日を出勤扱い（起こしすぎても寝かせ手段がある安全側）。"""
    import datetime
    if d is None:
        d = datetime.date.today()
    if d.weekday() >= 5:                       # 土日
        return False
    hols = _jp_holidays()
    if not hols:                               # 取得失敗 → 祝日判定できないので平日は出勤扱い
        return True
    if d in hols:                              # 祝日＋日曜振替（CSV に含まれる）
        return False
    if d.weekday() == 0 and (d - datetime.timedelta(days=2)) in hols:
        return False                           # ★会社ルール: 土曜の祝日→翌月曜休み
    return True


def run_scheduled_wake():
    """出勤日 かつ autopilot ON のときだけ WAKE_TARGET へ WOL を送る。"""
    if not is_workday():
        print("scheduled_wake: 非出勤日のためスキップ")
        return
    if bbt_read(AUTOPILOT_RESOURCE, default="on").strip().lower() == "off":
        print("scheduled_wake: autopilot OFF のためスキップ")
        return
    mac = devices.get(WAKE_TARGET)
    if not mac:
        print(f"scheduled_wake: 対象 '{WAKE_TARGET}' が devices に無い", file=sys.stderr)
        return
    ok = send_wol(mac)
    print(f"scheduled_wake: WOL {WAKE_TARGET} ({mac}) {'ok' if ok else 'fail'}")


# ── 深夜 nightwatch（systemd timer から `raspiwol.py nightwatch` で呼ばれる）────────

# 通知済みフラグ（RAM 上 /tmp。overlayfs に書かない。再起動で消えるため mtime で有効期限判定）。
_NIGHTWATCH_FLAG = Path("/tmp/raspiwol_nightwatch.flag")


def run_nightwatch():
    """深夜に対象 PC が起動中なら ntfy.sh でスマホへ通知する（自動スリープはしない）。"""
    import datetime

    # 1. ini の [nightwatch] から設定を読む。どちらか未設定なら無効
    ntfy_topic = cfg.get("nightwatch", "ntfy_topic", fallback="").strip()
    target_ip  = cfg.get("nightwatch", "target_ip",  fallback="").strip()
    if not ntfy_topic or not target_ip:
        print("nightwatch: not configured")
        return

    # 2. autopilot チェック。"off" なら skip。read 失敗時は default "on"（=続行）。
    #    Beebotte が死んでいる時こそ pcsleep_agent も止まっている可能性があるため。
    if bbt_read(AUTOPILOT_RESOURCE, default="on").strip().lower() == "off":
        print("nightwatch: autopilot=off, skip")
        return

    # 3. ping で PC 状態確認（-c2 -W2：1発ロス耐性。ping_host は -c1 のため直接呼ばない）
    try:
        r = subprocess.run(
            ["ping", "-c", "2", "-W", "2", target_ip],
            capture_output=True,
        )
        pc_up = r.returncode == 0
    except Exception:
        pc_up = False

    if not pc_up:
        print("nightwatch: pc=down, ok")
        return

    # 4. 通知済みフラグ確認（12時間以内に通知済みなら一晩1回だけ）
    if _NIGHTWATCH_FLAG.exists():
        age_sec = time.time() - _NIGHTWATCH_FLAG.stat().st_mtime
        if age_sec < 12 * 3600:
            print("nightwatch: already notified")
            return

    # 5. ntfy.sh へ通知（証明書検証は通常どおり有効。CERT_NONE は Beebotte 専用の回避策）
    now_str = datetime.datetime.now().strftime("%H:%M")
    body = f"⚠️ PC がまだ起きています ({now_str}) — pcsleep_agent の死亡かも".encode("utf-8")
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{ntfy_topic}",
            data=body,
            method="POST",
        )
        req.add_header("Title", "raspiwol nightwatch")   # ASCII のみ
        req.add_header("Tags", "warning")
        req.add_header("Priority", "high")
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"nightwatch: ntfy error: {e}", file=sys.stderr)
        return

    # 6. フラグを touch して "一晩1回" を保証
    _NIGHTWATCH_FLAG.touch()
    print("nightwatch: pc=up, notified")


def handle(data: str):
    cmd = data.strip().lower()

    # デバイス名で WOL
    if cmd in devices:
        mac = devices[cmd]
        ok = send_wol(mac)
        pub(f"{'wol_ok' if ok else 'wol_fail'}: {cmd} ({mac})")
        return

    # MAC アドレス直接指定
    if len(cmd.replace(":", "").replace("-", "")) == 12:
        ok = send_wol(cmd)
        pub(f"{'wol_ok' if ok else 'wol_fail'}: {cmd}")
        return

    if cmd == "status":
        pub(f"ip={local_ip()} uptime={uptime_str()} devices={list(devices.keys())}")
        return

    # ping <ip>: 対象PCの電源/稼働状態を確認（ダッシュボードのステータス表示用）
    if cmd.startswith("ping"):
        parts = cmd.split()
        if len(parts) >= 2 and _valid_ipv4(parts[1]):
            ip = parts[1]
            state = "up" if ping_host(ip) else "down"
            pub(f"ping: {ip} {state}")
            bbt_write(POWER_RESOURCE, state)   # ダッシュボード read 用に永続化
        else:
            pub("ping: bad_ip")
        return

    # netcheck: 各インターフェース経由で外部疎通を確認（eth0/wlan0 のどちらが死んだか切り分け）
    # ＋ out-of-band 救済経路 Tailscale の接続状態（導入時のみ tailscale=running 等を付加）
    if cmd == "netcheck":
        parts = [f"{i}={'up' if ping_via(i) else 'down'}" for i in _interfaces()]
        ts = tailscale_state()
        if ts is not None:
            parts.append(f"tailscale={ts}")
        pub(f"netcheck: {' '.join(parts) or 'no interfaces'}")
        return

    if cmd == "reboot":
        pub("rebooting")
        time.sleep(1)
        subprocess.run(["reboot"])
        return

    if cmd == "shutdown":
        pub("shutting down")
        time.sleep(1)
        subprocess.run(["shutdown", "-h", "now"])
        return

    if cmd == "pwrbtn":
        r = press_power_button(PWR_SHORT)
        pub({"ok": f"pwrbtn: short ({PWR_SHORT}s)", "busy": "pwrbtn: busy", "unavailable": "pwrbtn: gpio unavailable"}[r])
        return

    if cmd == "pwrbtn_long":
        r = press_power_button(5.0)
        pub({"ok": "pwrbtn: long (5s)", "busy": "pwrbtn: busy", "unavailable": "pwrbtn: gpio unavailable"}[r])
        return

    if cmd == "pwrbtn_10s":
        r = press_power_button(10.0)
        pub({"ok": "pwrbtn: 10s", "busy": "pwrbtn: busy", "unavailable": "pwrbtn: gpio unavailable"}[r])
        return

    if cmd == "update":
        if not UPDATE_URL:
            pub("update_url not set in raspiwol.ini")
            return
        pub("update: downloading")
        tmp = SCRIPT_DEST.with_suffix(".tmp")
        try:
            remount_boot(rw=True)
            req = urllib.request.Request(UPDATE_URL)
            if UPDATE_GITHUB_TOKEN:
                req.add_header("Authorization", f"token {UPDATE_GITHUB_TOKEN}")
            with urllib.request.urlopen(req) as resp:
                tmp.write_bytes(resp.read())
            tmp.rename(SCRIPT_DEST)
            remount_boot(rw=False)
            pub("update: saved, restarting service")
            subprocess.Popen(["systemctl", "restart", "raspiwol"])
        except Exception as e:
            pub(f"update_fail: {e}")
            tmp.unlink(missing_ok=True)
            remount_boot(rw=False)
        return

    pub(f"unknown_command: {cmd}")


def on_connect(client, userdata, connect_flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(TOPIC_CMD)
        pub(f"connected ip={local_ip()}")
    else:
        print(f"MQTT connect failed: {reason_code}", file=sys.stderr)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        data = payload.get("data", "")
    except Exception:
        data = msg.payload.decode(errors="replace")
    if isinstance(data, str) and data:
        handle(data)


mq.on_connect = on_connect
mq.on_message = on_message


def _setup_signal_handler():
    def _on_sigterm(signum, frame):
        if _gpio_ok:
            try:
                GPIO.setup(PWR_GPIO, GPIO.IN)
                GPIO.cleanup()
            except Exception:
                pass
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)


def main():
    # systemd timer から `raspiwol.py wake` で呼ばれる平日朝の自動 Wake（単発実行）。
    if len(sys.argv) > 1 and sys.argv[1] == "wake":
        run_scheduled_wake()
        return
    # systemd timer から `raspiwol.py nightwatch` で呼ばれる深夜監視（単発実行）。
    if len(sys.argv) > 1 and sys.argv[1] == "nightwatch":
        run_nightwatch()
        return
    _setup_signal_handler()
    print(f"raspiwol start | devices={list(devices.keys())} | ip={local_ip()}")
    while True:
        try:
            mq.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            mq.loop_forever()
        except Exception as e:
            print(f"MQTT error: {e}, retry 30s", file=sys.stderr)
            time.sleep(30)


if __name__ == "__main__":
    main()
