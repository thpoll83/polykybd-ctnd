# SPDX-License-Identifier: GPL-2.0-only
from pathlib import Path
import ipaddress
import logging
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
GPIO_INVERTED     = bool(_c["gpio"].get("inverted", True))

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
_ui = _c.get("ui", {})
UI_PORT = _ui.get("port", 5000)


def _is_loopback(h):
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


# SECURITY (HIL-1): the control UI is unauthenticated and exposes flashing,
# USB-power, GPIO and runner-control. The kiosk only ever loads
# http://localhost:5000, so default to loopback and require an explicit
# `ui.allow_lan: true` opt-in to bind a network interface. This also protects
# already-deployed rigs whose gitignored config.yaml still says "0.0.0.0":
# without allow_lan we override the bind to loopback (and warn) rather than
# silently exposing the box on the LAN.
ALLOW_LAN = bool(_ui.get("allow_lan", False))
_requested_host = _ui.get("host", "127.0.0.1")
if ALLOW_LAN:
    UI_HOST = _requested_host
else:
    if not _is_loopback(_requested_host):
        logging.getLogger("polykybd-ctnd").warning(
            "ui.host=%r would expose the unauthenticated control UI on the "
            "network; binding 127.0.0.1 instead (set ui.allow_lan: true to "
            "override).", _requested_host)
    UI_HOST = "127.0.0.1"

# Browser origins allowed to open the SocketIO control channel. Scoped to the
# kiosk's localhost origin unless LAN access is opted in — blocks a drive-by /
# DNS-rebind page in the operator's browser from driving the rig.
UI_CORS_ORIGINS = "*" if ALLOW_LAN else [
    f"http://localhost:{UI_PORT}",
    f"http://127.0.0.1:{UI_PORT}",
]

# GitHub Actions CI status (optional)
GITHUB_REPO  = _c.get("github", {}).get("repo", "")
GITHUB_TOKEN = _c.get("github", {}).get("token", "")
# Labels the HIL job requires (must match `runs-on:` in qmk-test.yml). Used by the
# runner-status badge and the on-screen diagnostics.
RUNNER_LABELS = _c.get("github", {}).get("runner_labels") or ["self-hosted", "polykybd-ctnd"]

# Firmware directory (relative to repo root)
FIRMWARE_DIR = str(_root / "firmware")

# Self-update: the git branch the deployed station tracks. The polykybd-update
# timer fetches this branch and fast-forwards + restarts when it gains commits
# (see scripts/self-update.sh); the UI "Update" badge/button reflect the same.
UPDATE_BRANCH = _c.get("update", {}).get("branch") or "main"
