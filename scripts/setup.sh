#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

INSTALL_DIR=/opt/polykybd-ctnd

# Resolve the real user even when the script is run via sudo
CTND_USER="${SUDO_USER:-$USER}"
CTND_HOME=$(getent passwd "$CTND_USER" | cut -d: -f6)

echo "=== PolyKybd CTND Setup (user: $CTND_USER) ==="

# Guard: must be run from the repository root
if [[ ! -f station/ui/app.py ]]; then
    echo "Error: run this script from the polykybd-ctnd repository root." >&2
    exit 1
fi

# Detect chromium package and binary name.
# Raspberry Pi OS Bookworm (Debian 12) renamed the package from
# 'chromium-browser' to 'chromium'.
if apt-cache show chromium &>/dev/null 2>&1; then
    CHROMIUM_PKG=chromium
    CHROMIUM_BIN=chromium
else
    CHROMIUM_PKG=chromium-browser
    CHROMIUM_BIN=chromium-browser
fi
echo "Chromium package: $CHROMIUM_PKG"

# System packages
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  uhubctl \
  libhidapi-hidraw0 libhidapi-libusb0 \
  "$CHROMIUM_PKG"

# Allow the user to access GPIO and USB without root
sudo usermod -aG gpio,plugdev "$CTND_USER"

# udev rule for HID access without root
# Update idVendor to match your actual QMK VID
sudo tee /etc/udev/rules.d/99-polykybd-hid.rules > /dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="4b50", MODE="0666", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules

# Install application
sudo mkdir -p "$INSTALL_DIR/firmware"
sudo cp -r . "$INSTALL_DIR"
sudo chown -R "$CTND_USER:$CTND_USER" "$INSTALL_DIR"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# Install systemd service files, substituting the actual username, home
# directory, and chromium binary for the 'pi' placeholders in the templates.
sed "s|User=pi|User=$CTND_USER|g; s|/home/pi|$CTND_HOME|g" \
  systemd/polykybd-ctnd.service \
  | sudo tee /etc/systemd/system/polykybd-ctnd.service > /dev/null

sed "s|User=pi|User=$CTND_USER|g; s|/home/pi|$CTND_HOME|g; s|chromium-browser|$CHROMIUM_BIN|g" \
  systemd/polykybd-kiosk.service \
  | sudo tee /etc/systemd/system/polykybd-kiosk.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable polykybd-ctnd.service polykybd-kiosk.service

echo ""
echo "=== GitHub Actions Runner ==="
echo "1. Go to: https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new"
echo "2. Select: Linux / ARM64"
echo "3. Download, configure, and when prompted for labels enter: polykybd-ctnd"
echo "4. Install as a service: sudo ./svc.sh install && sudo ./svc.sh start"
echo ""
echo "Done. Reboot to start all services."
