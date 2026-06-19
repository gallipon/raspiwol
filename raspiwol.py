#!/usr/bin/env python3
"""raspiwol - Raspberry Pi Wake-on-LAN daemon via Beebotte MQTT"""

import configparser
import csv
import json
import socket
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import paho.mqtt.client as mqtt

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

# Beebotte REST API（ダッシュボードが read するステータスの永続化に使用）
API_BASE       = "https://api.beebotte.com/v1/data"
CHANNEL        = TOPIC_CMD.split("/")[0] if "/" in TOPIC_CMD else ""
POWER_RESOURCE = "power"   # ※Beebotte チャンネルに事前に作成しておくこと

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
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"bbt_write error: {e}", file=sys.stderr)


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
        r = press_power_button(0.2)
        pub({"ok": "pwrbtn: short (0.2s)", "busy": "pwrbtn: busy", "unavailable": "pwrbtn: gpio unavailable"}[r])
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
