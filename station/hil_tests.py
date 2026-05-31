# SPDX-License-Identifier: GPL-2.0-only
"""Concrete HIL test cases for the PolyKybd test/deploy station.

Each test is a dict ``{"name": str, "fn": callable}``. ``fn`` receives
``(raw_hid: RawHID, log: Callable[[str], None])`` and returns a bool — True for
pass, False for fail. Exceptions are caught by the runner and reported as a
failure, so a test may also simply assert.

Run them via the CLI::

    python -m station.test_runner --left left_hil.uf2 --right right_hil.uf2

See ``docs/FUTURE_TESTS.md`` for the planned-but-not-yet-implemented backlog.
"""
from typing import Callable

from .hid import RawHID, enumerate_raw_interfaces

# --- Raw HID protocol constants (mirror keyboards/handwired/polykybd/hid_com.c) ---
# A command report is: data[0] = 'P' (channel marker), data[1] = command id.
# Responses echo "P<id><status>...", where status is '.' (ACK) / '!' (NACK),
# and GET_ID uses '*' instead of '.' on the first exchange after a fresh boot.
POLY_CHANNEL = ord("P")  # 0x50
CMD_GET_ID = 6           # case 6 in raw_hid_receive(): device identity string
ACK = ord(".")
NACK = ord("!")
FRESH_BOOT = ord("*")    # GET_ID status byte when the firmware just (re)booted


def test_single_master_enumerates(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Exactly one half presents a Raw HID interface (the master).

    This is the headline assertion for the POLYKYBD_HIL firmware build. On the
    rig both halves are USB-powered, so stock VBUS-based master detection makes
    *both* enumerate as master. The HIL build forces left=master/right=slave by
    handedness and has the slave usb_disconnect(), leaving a single Raw HID
    interface. Zero interfaces = neither half came up as master (or USB/flash
    failed); two = the force-master override did not take effect.
    """
    interfaces = enumerate_raw_interfaces()
    count = len(interfaces)
    for d in interfaces:
        # manufacturer_string / product_string help eyeball which half answered
        log(f"  raw iface: path={d['path']!r} product={d.get('product_string')!r}")
    log(f"raw HID interfaces present: {count} (expected exactly 1)")
    if count == 0:
        log("  FAIL: no master enumerated — check flash, USB cabling, EE_HANDS marker")
    elif count > 1:
        log("  FAIL: multiple masters — POLYKYBD_HIL force-master override not effective")
    return count == 1


def test_get_id(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The master answers GET_ID with a well-formed Split72 identity string.

    Sends ``P\\x06`` and expects a reply that echoes the channel + command id,
    carries an ACK ('.' ) or fresh-boot ('*') status byte, and names the board.
    Proves the surviving master is not just enumerated but actually servicing
    the Raw HID command dispatcher.
    """
    response = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
    log(f"GET_ID response: {response!r}")
    if response is None:
        log("  FAIL: no response to GET_ID")
        return False
    if response[0] != POLY_CHANNEL or response[1] != CMD_GET_ID:
        log(f"  FAIL: bad header {response[0]:#04x} {response[1]:#04x}, want 'P' 0x06")
        return False
    if response[2] not in (ACK, FRESH_BOOT):
        log(f"  FAIL: status byte {response[2]:#04x} is neither ACK '.' nor '*'")
        return False
    # Identity string follows the 3-byte header; firmware sends "...Split72 <ver>..."
    identity = bytes(response[3:]).split(b"\x00", 1)[0].decode("utf-8", "replace")
    log(f"  identity: {identity!r}")
    if "Split72" not in identity:
        log("  FAIL: identity string does not contain 'Split72'")
        return False
    return True


# Ordered so the cheap "is there exactly one master?" structural check runs
# before the command-level probe — if enumeration is wrong, GET_ID's failure
# would otherwise be the confusing first symptom.
TESTS = [
    {"name": "single master enumerates", "fn": test_single_master_enumerates},
    {"name": "raw HID GET_ID", "fn": test_get_id},
]
