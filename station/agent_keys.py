"""Cmd 45 AI and cmd 46 AI_CARD (protocol v31): the agent keys. Request packing
and reply parsing for the HIL test, unit-tested offline.

Mirrors qmk_firmware keyboards/polykybd/ai_mode.c (ai_hid_cmd, ai_hid_card) and
base/ai_events.h (PROTOCOL_HISTORY.md, v31):
  cmd 45 op 0 QUERY        reply data[3] AI mode, data[4] newest event seq,
                           data[5] number keys found (0..10), data[6] ESC found
  cmd 45 op 1 SET_KEYS     data[3..12] 10 states (0..5, CLOSED), data[13] flags
                           (bits 0..3, CLOSED), data[14..37] 12 pool slots u16 LE
                           (< 600 or 0xFFFF). Any bad field NACKs, nothing changes.
  cmd 45 op 2 READ_EVENTS  data[3] last seq seen; reply data[3] newest seq,
                           data[4] count n (0..8), then n (seq, key) pairs
  cmd 45 other op          NACK
  cmd 46                   data[2..3] u16 LE header: bits 0..9 pool slot (< 600),
                           bits 12..14 segment (0..5); data[4..63] 60 bytes.
"""

CMD_AI = 45
CMD_AI_CARD = 46
AGENT_KEYS_MIN_PROTOCOL = 31

POLY_CHANNEL = ord("P")
OP_QUERY, OP_SET_KEYS, OP_READ_EVENTS = 0, 1, 2

NUM_KEYS = 10
NUM_CARDS = 12
STATE_MAX = 5                # 0 empty, 1 working, 2 waiting, 3 done, 4 idle, 5 stale
FLAGS_VALID = 0x0F           # ESC attention, more pages, any waiting, extra cards
NO_SLOT = 0xFFFF
POOL_SLOTS = 600             # NUM_OVERLAY_SLOTS
RING_SIZE = 8
SEGMENTS = 6
SEGMENT_BYTES = 60


def query_request() -> bytes:
    return bytes([POLY_CHANNEL, CMD_AI, OP_QUERY])


def set_keys_request(states, flags: int = 0, slots=None) -> bytes:
    """`states`: 10 values; `slots`: 12 values (NO_SLOT when omitted). Values are
    packed as given, so a test can send an out-of-range one."""
    states = list(states)
    slots = list(slots) if slots is not None else [NO_SLOT] * NUM_CARDS
    if len(states) != NUM_KEYS or len(slots) != NUM_CARDS:
        raise ValueError("10 states and 12 slots")
    body = bytes(s & 0xFF for s in states) + bytes([flags & 0xFF])
    for slot in slots:
        body += bytes([slot & 0xFF, (slot >> 8) & 0xFF])
    return bytes([POLY_CHANNEL, CMD_AI, OP_SET_KEYS]) + body


def read_events_request(last_seq: int = 0) -> bytes:
    return bytes([POLY_CHANNEL, CMD_AI, OP_READ_EVENTS, last_seq & 0xFF])


def op_request(op: int) -> bytes:
    return bytes([POLY_CHANNEL, CMD_AI, op & 0xFF])


def card_request(slot: int, segment: int, payload: bytes) -> bytes:
    """`slot` and `segment` are packed into the header as given (masked to their
    bit fields), so a test can send slot 600 or segment 6."""
    header = (slot & 0x3FF) | ((segment & 0x7) << 12)
    payload = bytes(payload)[:SEGMENT_BYTES].ljust(SEGMENT_BYTES, b"\0")
    return bytes([POLY_CHANNEL, CMD_AI_CARD, header & 0xFF, header >> 8]) + payload


def status(resp, cmd: int):
    """The status byte ('.' or '!') of a reply to `cmd`, or None."""
    if not resp or len(resp) < 3 or resp[0] != POLY_CHANNEL or resp[1] != cmd:
        return None
    return resp[2]


def parse_query(resp):
    """(status, ai_mode, newest seq, number keys found, esc found) or None."""
    if status(resp, CMD_AI) is None or len(resp) < 7:
        return None
    return resp[2], resp[3], resp[4], resp[5], resp[6]


def parse_events(resp):
    """(status, newest seq, [(seq, key)]) or None. A count past the ring size is
    malformed."""
    if status(resp, CMD_AI) is None or len(resp) < 5:
        return None
    n = resp[4]
    if n > RING_SIZE or len(resp) < 5 + 2 * n:
        return None
    return resp[2], resp[3], [(resp[5 + 2 * i], resp[6 + 2 * i]) for i in range(n)]
