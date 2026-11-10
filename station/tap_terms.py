"""Cmd 44 (protocol v29+): the tap-hold terms. Request packing and reply parsing,
shared by the HIL test and the reboot-persistence check, and unit-tested offline.

Mirrors qmk_firmware keyboards/polykybd/base/tap_terms.h and hid_cmd_44:
  request  data[2] op (0 query, 1 set), data[3..4] tapping term ms,
           data[5..6] Flow Tap term ms, little-endian; 0xFFFF keeps a term.
  reply    data[3..4] tapping term, data[5..6] Flow Tap term, data[7] flags
           (bit 0: Flow Tap is compiled in). ACK and NACK replies both carry them.
  v30      request data[7] Auto Shift op (0 keep, 1 off, 2 on), data[8..9] Auto
           Shift timeout ms LE (0 keeps it). Reply flag bit 1: Auto Shift compiled
           in, bit 2: Auto Shift on; data[8..9] the timeout.
"""

CMD_TAP_TERMS = 44
TAP_TERMS_MIN_PROTOCOL = 29

POLY_CHANNEL = ord("P")
KEEP = 0xFFFF
FLAG_FLOW_TAP = 0x01

STEP_MS = 10
TAPPING_MIN_MS, TAPPING_MAX_MS = 100, 500
FLOW_TAP_MIN_MS, FLOW_TAP_MAX_MS = 0, 300
DEFAULT_TAPPING_MS, DEFAULT_FLOW_TAP_MS = 200, 150

AUTO_SHIFT_MIN_PROTOCOL = 30
AUTO_SHIFT_MIN_MS, AUTO_SHIFT_MAX_MS = 100, 500
DEFAULT_AUTO_SHIFT_MS = 180
AS_KEEP, AS_OFF, AS_ON = 0, 1, 2
AS_MS_KEEP = 0
FLAG_AUTO_SHIFT = 0x02
FLAG_AUTO_SHIFT_ON = 0x04


def query_request() -> bytes:
    return bytes([POLY_CHANNEL, CMD_TAP_TERMS, 0, 0, 0, 0, 0])


def set_request(tapping_ms: int = KEEP, flow_tap_ms: int = KEEP) -> bytes:
    return bytes([POLY_CHANNEL, CMD_TAP_TERMS, 1,
                  tapping_ms & 0xFF, (tapping_ms >> 8) & 0xFF,
                  flow_tap_ms & 0xFF, (flow_tap_ms >> 8) & 0xFF])


def auto_shift_request(op: int = AS_KEEP, ms: int = AS_MS_KEEP,
                       tapping_ms: int = KEEP, flow_tap_ms: int = KEEP) -> bytes:
    """A v30 set: the terms (KEEP by default) plus the Auto Shift fields."""
    return set_request(tapping_ms, flow_tap_ms) + bytes([op & 0xFF, ms & 0xFF, (ms >> 8) & 0xFF])


def parse_auto_shift(resp):
    """(status byte, compiled in, on, timeout ms), or None when the reply is not a
    cmd 44 reply long enough to carry them."""
    if not resp or len(resp) < 10 or resp[0] != POLY_CHANNEL or resp[1] != CMD_TAP_TERMS:
        return None
    return (resp[2], bool(resp[7] & FLAG_AUTO_SHIFT), bool(resp[7] & FLAG_AUTO_SHIFT_ON),
            resp[8] | (resp[9] << 8))


def parse_reply(resp):
    """(status byte, tapping ms, flow tap ms, flow_tap_available), or None when the
    reply is not a cmd 44 reply long enough to carry the terms."""
    if not resp or len(resp) < 8 or resp[0] != POLY_CHANNEL or resp[1] != CMD_TAP_TERMS:
        return None
    return (resp[2], resp[3] | (resp[4] << 8), resp[5] | (resp[6] << 8),
            bool(resp[7] & FLAG_FLOW_TAP))


def other_value(current: int, a: int, b: int) -> int:
    """A value different from ``current`` (a, or b when current is a). A test that
    must make the firmware write EEPROM has to CHANGE the value."""
    return a if current != a else b
