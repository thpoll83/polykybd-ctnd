# SPDX-License-Identifier: GPL-2.0-only

# GPIO pin assignments (BCM numbering)
LEFT_RUN_PIN      = 17
LEFT_BOOTSEL_PIN  = 18
RIGHT_RUN_PIN     = 22
RIGHT_BOOTSEL_PIN = 23

# uhubctl — RPi4 built-in USB hub
# Find your hub location by running: uhubctl
USB_HUB_LOCATION = "1-1"
LEFT_USB_PORT    = 1
RIGHT_USB_PORT   = 2

# QMK HID identifiers
# Update with actual values from keyboards/polykbd/config.h
QMK_VENDOR_ID  = 0x4B50   # placeholder — check your QMK config
QMK_PRODUCT_ID = 0x0001   # placeholder

# HID usage pages (QMK standard)
HID_CONSOLE_USAGE_PAGE = 0xFF31
HID_CONSOLE_USAGE      = 0x0074
HID_RAW_USAGE_PAGE     = 0xFF60
HID_RAW_USAGE          = 0x0061

# Mass storage bootloader label
MASS_STORAGE_LABEL = "RPI-RP2"

# Web UI
UI_HOST = "0.0.0.0"
UI_PORT = 5000

# Directory where built UF2 files are placed by CI.
# Resolved relative to this file so it works regardless of install location.
from pathlib import Path as _Path
FIRMWARE_DIR = str(_Path(__file__).parent.parent / "firmware")
