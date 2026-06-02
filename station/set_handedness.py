# SPDX-License-Identifier: GPL-2.0-only
"""Set the keyboard's handedness (EE_HANDS) over Raw HID — cmd 25 (0x19).

Handedness lives in each half's EEPROM and decides which side a half *displays*
as (the splash "POLY KYBD" for left vs "SPLIT 72" for right, and the status
OLED). It is independent of the HIL master/slave role, which is fixed at compile
time (``POLYKYBD_HIL=left|right``) — so a freshly-provisioned or mismatched board
can correctly run as master yet show the wrong side. This fixes that without a
reflash.

Mirrors PolyKybdHost's ``PolyKybd.set_handedness()``: the SET_HANDEDNESS command
goes to the **master (USB) half** — payload byte 0 ⇒ master is left — which
stores its own handedness, pushes the opposite to the slave over the split link,
then both reboot to apply (~10 s, no replug). The firmware ACKs ``"P\\x19."``
*before* it soft-resets, so the reply is best-effort: USB drops as the half
reboots and a missing reply is normal, not a failure.

    python -m station.set_handedness left    # USB/master half = left  (slave = right)
    python -m station.set_handedness right   # USB/master half = right (slave = left)
"""
import sys
from typing import Callable

from .hid import RawHID

POLY_CHANNEL       = ord("P")   # 0x50
CMD_SET_HANDEDNESS = 0x19       # case 25 in keyboards/handwired/polykybd/hid_com.c
ACK                = ord(".")


def set_handedness(master_is_left: bool, raw: "RawHID | None" = None,
                   log: Callable[[str], None] = print) -> bool:
    """Tell the master (USB) half which side it is, fixing a half that shows the
    wrong side. ``master_is_left=True`` ⇒ the connected/master half is the left
    half (firmware: ``master_is_left = data[HID_DATA_IDX] == 0``).

    Returns True on a clean send. The keyboard reboots to apply, so a missing or
    non-ACK reply is treated as success (the half drops USB mid-reply).
    """
    raw = raw or RawHID()
    log(f"[handedness] setting USB (master) half = {'LEFT' if master_is_left else 'RIGHT'} "
        f"(slave = {'RIGHT' if master_is_left else 'LEFT'}); the keyboard reboots to apply")
    payload = bytes([POLY_CHANNEL, CMD_SET_HANDEDNESS, 0 if master_is_left else 1])
    try:
        resp = raw.send(payload, timeout_ms=500)
    except Exception as exc:
        # The half drops USB as it soft-resets — expected, not a failure.
        log(f"[handedness] command sent; keyboard rebooting ({exc})")
        return True
    if (resp and len(resp) >= 3 and resp[0] == POLY_CHANNEL
            and resp[1] == CMD_SET_HANDEDNESS and resp[2] == ACK):
        log("[handedness] ACK — keyboard rebooting to apply the new side")
    else:
        log(f"[handedness] command sent; keyboard rebooting (reply: {resp!r})")
    return True


if __name__ == "__main__":
    arg = sys.argv[1].lower() if len(sys.argv) == 2 else ""
    if arg not in ("left", "right"):
        print("usage: python -m station.set_handedness <left|right>\n"
              "  left  = the USB-connected (master) half is the LEFT half\n"
              "  right = the USB-connected (master) half is the RIGHT half",
              file=sys.stderr)
        sys.exit(2)
    sys.exit(0 if set_handedness(arg == "left") else 1)
