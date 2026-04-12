#!/usr/bin/env bash
# raspiwol セットアップスクリプト
# Pi OS Lite (Bullseye/Bookworm) に SSH して root で実行:
#   sudo bash setup.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: root で実行してください: sudo bash $0" >&2
    exit 1
fi

# ── ブートパス検出 ────────────────────────────────────────────────────────────
if [ -d /boot/firmware ]; then
    BOOT_DIR=/boot/firmware   # Bookworm
else
    BOOT_DIR=/boot            # Bullseye
fi
echo "Boot dir: $BOOT_DIR"

# ── ユーザー確認 ──────────────────────────────────────────────────────────────
PI_USER=${SUDO_USER:-pi}
echo "実行ユーザー: $PI_USER"

# ── ネットワーク設定の入力 ────────────────────────────────────────────────────
echo ""
echo "=== 静的 IP 設定 ==="
read -rp "Pi の静的 IP アドレス (例: 192.168.1.50): " STATIC_IP
read -rp "サブネットマスク prefix (例: 24): " PREFIX
read -rp "デフォルトゲートウェイ (例: 192.168.1.1): " GATEWAY
read -rp "DNS サーバー (空欄でゲートウェイを使用): " DNS_SERVER
DNS_SERVER="${DNS_SERVER:-$GATEWAY}"

# ── MQTT 設定の入力 ──────────────────────────────────────────────────────────
echo ""
echo "=== Beebotte MQTT 設定 ==="
read -rp "Beebotte トークン (token_XXXX): " MQTT_TOKEN
read -rp "コマンドトピック (例: mychannel/command): " TOPIC_CMD
read -rp "ログトピック (例: mychannel/log): " TOPIC_LOG
read -rp "update URL (GitHub raw URL、後で設定する場合は空欄): " UPDATE_URL

# ── 依存パッケージ ────────────────────────────────────────────────────────────
echo ""
echo "=== パッケージインストール ==="
apt-get update -qq
apt-get install -y python3-paho-mqtt

# ── ファイルコピー ────────────────────────────────────────────────────────────
echo ""
echo "=== ファイルコピー → $BOOT_DIR ==="
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

cp "$SCRIPT_DIR/raspiwol.py"   "$BOOT_DIR/raspiwol.py"
cp "$SCRIPT_DIR/devices.csv"   "$BOOT_DIR/raspiwol_devices.csv"
chmod 644 "$BOOT_DIR/raspiwol.py" "$BOOT_DIR/raspiwol_devices.csv"

# ── 設定ファイル生成 ──────────────────────────────────────────────────────────
cat > "$BOOT_DIR/raspiwol.ini" << EOF
[mqtt]
host = mqtt.beebotte.com
port = 1883
token = $MQTT_TOKEN
topic_cmd = $TOPIC_CMD
topic_log = $TOPIC_LOG

[update]
url = $UPDATE_URL
EOF
chmod 600 "$BOOT_DIR/raspiwol.ini"
echo "設定ファイル: $BOOT_DIR/raspiwol.ini"

# ── システム書き込み削減 ──────────────────────────────────────────────────────
echo ""
echo "=== SD 書き込み削減 ==="

# swap 無効化
if systemctl is-enabled --quiet dphys-swapfile 2>/dev/null; then
    dphys-swapfile swapoff || true
    dphys-swapfile uninstall || true
    systemctl disable dphys-swapfile
    echo "swap: 無効化"
fi

# journald を揮発性（RAM のみ）に
sed -i 's/^#*Storage=.*/Storage=volatile/' /etc/systemd/journald.conf
grep -q '^Storage=' /etc/systemd/journald.conf || echo 'Storage=volatile' >> /etc/systemd/journald.conf
echo "journald: volatile に設定"

# /tmp と /var/log を tmpfs に（overlayfs と重複するが harmless）
FSTAB_MARKER="# raspiwol tmpfs"
if ! grep -q "$FSTAB_MARKER" /etc/fstab; then
    cat >> /etc/fstab << EOF

$FSTAB_MARKER
tmpfs /tmp     tmpfs defaults,noatime,nosuid,nodev,size=64m 0 0
tmpfs /var/log tmpfs defaults,noatime,nosuid,nodev,size=32m 0 0
EOF
    echo "fstab: tmpfs エントリ追加"
fi

# ── systemd サービス ──────────────────────────────────────────────────────────
echo ""
echo "=== systemd サービス設定 ==="
cp "$SCRIPT_DIR/raspiwol.service" /etc/systemd/system/raspiwol.service

# サービスファイルのユーザー名を実際のユーザーに書き換え
sed -i "s/^User=pi$/User=${PI_USER}/" /etc/systemd/system/raspiwol.service

systemctl daemon-reload
systemctl enable raspiwol
echo "raspiwol.service: 有効化"

# ── overlayfs 有効化 ──────────────────────────────────────────────────────────
echo ""
echo "=== overlayfs (read-only root) 有効化 ==="
if command -v raspi-config &>/dev/null; then
    raspi-config nonint do_overlayfs 0
    echo "overlayfs: 有効化 (次回起動から有効)"
else
    echo "WARNING: raspi-config が見つかりません。overlayfs を手動で設定してください。"
fi

# ── 静的 IP 設定（最後：nmcli con up で SSH が切れるため他の作業の後に実行）──
echo ""
echo "=== 静的 IP 設定 ==="
if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    # Bookworm: NetworkManager
    CONN=$(nmcli -t -f NAME,DEVICE con show | grep eth0 | head -1 | cut -d: -f1)
    if [ -z "$CONN" ]; then
        CONN="Wired connection 1"
    fi
    nmcli con mod "$CONN" \
        ipv4.method manual \
        ipv4.addresses "${STATIC_IP}/${PREFIX}" \
        ipv4.gateway "$GATEWAY" \
        ipv4.dns "$DNS_SERVER 8.8.8.8"
    # con up は切断を伴うため reboot に委ねる（con mod だけで次回起動時に反映）
    echo "NetworkManager: 静的 IP 設定完了 ($STATIC_IP/$PREFIX) ← 再起動後に有効"
elif [ -f /etc/dhcpcd.conf ]; then
    # Bullseye: dhcpcd
    cat >> /etc/dhcpcd.conf << EOF

interface eth0
static ip_address=${STATIC_IP}/${PREFIX}
static routers=${GATEWAY}
static domain_name_servers=${DNS_SERVER} 8.8.8.8
EOF
    echo "dhcpcd: 静的 IP 設定完了 ($STATIC_IP/$PREFIX) ← 再起動後に有効"
else
    echo "WARNING: ネットワーク設定方式を特定できませんでした。手動で設定してください。"
fi

# ── 完了 ──────────────────────────────────────────────────────────────────────
echo ""
echo "========================================"
echo "セットアップ完了"
echo "========================================"
echo "静的 IP : $STATIC_IP/$PREFIX (GW: $GATEWAY)"
echo "Boot dir: $BOOT_DIR"
echo "Config  : $BOOT_DIR/raspiwol.ini"
echo ""
echo "次のステップ:"
echo "  1. sudo reboot  ← overlayfs を有効にして再起動"
echo "  2. SSH 再接続後: sudo journalctl -u raspiwol -f  ← ログ確認"
echo "  3. Beebotte から \"status\" コマンドで動作確認"
echo ""
echo "更新時: Beebotte から \"update\" コマンドを送信"
echo "  (config.ini の update.url に GitHub raw URL を設定しておくこと)"
