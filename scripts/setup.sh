#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

INSTALL_DIR=/opt/polykybd-ctnd

echo "=== PolyKybd CTND Setup ==="

# System packages
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  uhubctl \
  libhidapi-hidraw0 libhidapi-libusb0 \
  chromium-browser

# Allow pi user to access GPIO and USB
sudo usermod -aG gpio,plugdev pi

# udev rule for HID access without root
# Update idVendor to match your actual QMK VID
sudo tee /etc/udev/rules.d/99-polykybd-hid.rules > /dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="4b50", MODE="0666", GROUP="plugdev"
EOF
sudo udevadm control --reload-rules

# Install application
sudo mkdir -p "$INSTALL_DIR/firmware"
sudo cp -r . "$INSTALL_DIR"
sudo chown -R pi:pi "$INSTALL_DIR"

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# Systemd services
sudo cp systemd/polykybd-ctnd.service   /etc/systemd/system/
sudo cp systemd/polykybd-kiosk.service  /etc/systemd/system/
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
