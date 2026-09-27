#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""緊急用 WOL: Beebotte を経由せず、この Pi から直接マジックパケットを送る。

Beebotte（MQTT / REST）が使えないときの Wake 手段。Tailscale か VPN で Pi に
SSH できれば PC を起こせる。

  python3 wol_manual.py                 # 既定 desktopmuk
  python3 wol_manual.py d8:bb:c1:df:91:36
  python3 wol_manual.py macmini         # devices.csv の名前でも可
"""
import csv
import os
import socket
import struct
import sys

DEVICES = "/boot/firmware/raspiwol_devices.csv"
DEFAULT = "desktopmuk"
PORTS = (9, 7)


def load_devices():
    out = {}
    try:
        with open(DEVICES, newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 2 and not row[0].strip().startswith("#"):
                    out[row[0].strip().lower()] = row[1].strip()
    except OSError as e:
        print("devices.csv 読み込み失敗: %s" % e, file=sys.stderr)
    return out


def broadcasts():
    """各インターフェースのブロードキャストアドレスを集める（ip コマンド）。"""
    addrs = ["255.255.255.255"]
    for line in os.popen("ip -o -4 addr show").read().splitlines():
        parts = line.split()
        if "brd" in parts:
            addrs.append(parts[parts.index("brd") + 1])
    seen, uniq = set(), []
    for a in addrs:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return uniq


def magic(mac):
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("MAC の形式が不正: %s" % mac)
    return b"\xff" * 6 + raw * 16


arg = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT).strip()
mac = arg if ":" in arg or "-" in arg else load_devices().get(arg.lower(), "")
if not mac:
    print("MAC が特定できません: %s" % arg, file=sys.stderr)
    sys.exit(1)

pkt = magic(mac)
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
sent = 0
for addr in broadcasts():
    for port in PORTS:
        try:
            s.sendto(pkt, (addr, port))
            sent += 1
            print("sent -> %s:%d" % (addr, port))
        except OSError as e:
            print("failed -> %s:%d (%s)" % (addr, port, e), file=sys.stderr)
s.close()
print("WOL %s (%s): %d packets" % (arg, mac, sent))
sys.exit(0 if sent else 1)
