"""Cmd 44 (protocol v28+): the tap-hold terms. Request packing and reply parsing,
shared by the HIL test and the reboot-persistence check, and unit-tested offline.

Mirrors qmk_firmware keyboards/polykybd/base/tap_terms.h and hid_cmd_44:
  request  data[2] op (0 query, 1 set), data[3..4] tapping term ms,
           data[5..6] Flow Tap term ms, little-endian; 0xFFFF keeps a term.
  reply    data[3..4] tapping term, data[5..6] Flow Tap term, data[7] flags
           (bit 0: Flow Tap is compiled in). ACK and NACK replies both carry them.
"""

CMD_TAP_TERMS = 44
TAP_TERMS_MIN_PROTOCOL = 28

POLY_CHANNEL = ord("P")
KEEP = 0xFFFF
FLAG_FLOW_TAP = 0x01

STEP_MS = 10
TAPPING_MIN_MS, TAPPING_MAX_MS = 100, 500
FLOW_TAP_MIN_MS, FLOW_TAP_MAX_MS = 0, 300
DEFAULT_TAPPING_MS, DEFAULT_FLOW_TAP_MS = 200, 150


def query_request() -> bytes:
    return bytes([POLY_CHANNEL, CMD_TAP_TERMS, 0, 0, 0, 0, 0])


def set_request(tapping_ms: int = KEEP, flow_tap_ms: int = KEEP) -> bytes:
    return bytes([POLY_CHANNEL, CMD_TAP_TERMS, 1,
                  tapping_ms & 0xFF, (tapping_ms >> 8) & 0xFF,
                  flow_tap_ms & 0xFF, (flow_tap_ms >> 8) & 0xFF])


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
