"""Cmd 48 (protocol v33+): runtime combos and tap dance. Request packing and reply
parsing, shared by the HIL test and the reboot-persistence check, and unit-tested
offline.

Mirrors qmk_firmware keyboards/polykybd/poly_combos.c and base/combo_store.h:
  request  data[2] op: 0 INFO, 1 GET, 2 SET, 3 COMMIT; GET/SET data[3] kind
           (0 combo, 1 tap dance), data[4] index, SET data[5..14] five u16 LE.
  reply    INFO data[3] combo slots, [4] tap-dance slots, [5] record bytes,
           [6] bit 0 available, [7..8] combo term ms. GET/SET data[3] kind,
           [4] index, [5..14] the record as it is now. COMMIT data[3] bit 0: the
           other half took the copy. ACK and NACK replies carry the same layout.
"""

CMD_COMBOS = 48
COMBOS_MIN_PROTOCOL = 33

POLY_CHANNEL = ord("P")
OP_INFO, OP_GET, OP_SET, OP_COMMIT = 0, 1, 2, 3
KIND_COMBO, KIND_TAP_DANCE = 0, 1
FLAG_AVAILABLE = 0x01
FLAG_OTHER_HALF = 0x01
SLOTS = 16


def info_request() -> bytes:
    return bytes([POLY_CHANNEL, CMD_COMBOS, OP_INFO])


def get_request(kind: int, index: int) -> bytes:
    return bytes([POLY_CHANNEL, CMD_COMBOS, OP_GET, kind, index])


def set_request(kind: int, index: int, words) -> bytes:
    body = bytearray([POLY_CHANNEL, CMD_COMBOS, OP_SET, kind, index])
    for w in words:
        body += bytes([w & 0xFF, (w >> 8) & 0xFF])
    return bytes(body)


def commit_request() -> bytes:
    return bytes([POLY_CHANNEL, CMD_COMBOS, OP_COMMIT])


def _is_reply(resp, n) -> bool:
    return bool(resp) and len(resp) >= n and resp[0] == POLY_CHANNEL and resp[1] == CMD_COMBOS


def parse_info(resp):
    """(status, available, combo slots, tap-dance slots), or None."""
    if not _is_reply(resp, 9):
        return None
    return resp[2], bool(resp[6] & FLAG_AVAILABLE), resp[3], resp[4]


def parse_record(resp):
    """(status, kind, index, [five words]), or None."""
    if not _is_reply(resp, 15):
        return None
    words = [resp[5 + 2 * i] | (resp[6 + 2 * i] << 8) for i in range(5)]
    return resp[2], resp[3], resp[4], words


def parse_commit(resp):
    """(status, other half took it), or None."""
    if not _is_reply(resp, 4):
        return None
    return resp[2], bool(resp[3] & FLAG_OTHER_HALF)
