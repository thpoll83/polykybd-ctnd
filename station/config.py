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

HID_CONSOLE_USAGE_PAGE = _c["qmk"]["console_usage_page"]
HID_CONSOLE_USAGE      = _c["qmk"]["console_usage"]
HID_RAW_USAGE_PAGE     = _c["qmk"]["raw_usage_page"]
HID_RAW_USAGE          = _c["qmk"]["raw_usage"]

MASS_STORAGE_LABEL = _c["qmk"]["mass_storage_label"]

# Web UI
UI_HOST = _c["ui"]["host"]
UI_PORT = _c["ui"]["port"]

# Firmware directory (relative to repo root)
FIRMWARE_DIR = str(_root / "firmware")
