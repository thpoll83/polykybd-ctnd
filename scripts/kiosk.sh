#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
# Manual kiosk launch (use the systemd service instead when possible)
export DISPLAY=:0
chromium-browser \
  --kiosk \
  --noerrdialogs \
  --disable-infobars \
  --app=http://localhost:5000
