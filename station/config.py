# SPDX-License-Identifier: GPL-2.0-only
from pathlib import Path
import yaml

_root    = Path(__file__).parent.parent
_cfg     = _root / "config" / "config.yaml"
_example = _root / "config" / "config.yaml.example"

with open(_cfg if _cfg.exists() else _example) as _f:
    _c = yaml.safe_load(_f)

# GPIO pin assignments (BCM numbering)
LEFT_RUN_PIN      = _c["gpio"]["left_run"]
LEFT_BOOTSEL_PIN  = _c["gpio"]["left_bootsel"]
RIGHT_RUN_PIN     = _c["gpio"]["right_run"]
RIGHT_BOOTSEL_PIN = _c["gpio"]["right_bootsel"]

# uhubctl
USB_HUB_LOCATION = _c["usb"]["hub_location"]
LEFT_USB_PORT    = _c["usb"]["left_port"]
RIGHT_USB_PORT   = _c["usb"]["right_port"]

# QMK HID identifiers
QMK_VENDOR_ID  = _c["qmk"]["vendor_id"]
QMK_PRODUCT_ID = _c["qmk"]["product_id"]

# HID usage pages (QMK standard — not user-configurable)
HID_CONSOLE_USAGE_PAGE = 0xFF31
HID_CONSOLE_USAGE      = 0x0074
HID_RAW_USAGE_PAGE     = 0xFF60
HID_RAW_USAGE          = 0x0061

# Mass storage bootloader label
MASS_STORAGE_LABEL = "RPI-RP2"

# Web UI
UI_HOST = _c["ui"]["host"]
UI_PORT = _c["ui"]["port"]

# Firmware directory (relative to repo root)
FIRMWARE_DIR = str(_root / "firmware")
