"""Cmd 49 HOST TUTORIAL (protocol v35): request packing and reply parsing for the HIL
tests, unit-tested offline.

Mirrors qmk_firmware keyboards/polykybd/base/host_tutorial_plan.[ch] and
features/anim/host_tutorial.c (host_tutorial_hid):
  data[2] op: 0 INFO, 1 TEXT, 2 SPOTLIGHT, 3 REVEAL, 4 ARM, 5 POLL, 6 END; any other
            NACKs.
  INFO      reply [3] op bitmask (0x7F), [4] max conditions (4), [5] watchdog s (10),
            [6] text bytes (59).
  TEXT      [3] len0, line 0, then len1, line 1, all in one 64-byte report; every byte
            0x20..0x7E. Both empty clears the text.
  SPOTLIGHT [3] flags (bit 0 on; any other bit NACKs), [4..13] mask, bit = row * 8 + col.
  REVEAL    [3] row, [4] col: a key with a display, else NACK.
  ARM       [3] count 0..4, then {kind, a, b}: 1 KEY (row, col), 2 LAYER (layer < 13,
            b = 0), 3 KEYCODE (low, high). Anything else NACKs and keeps the old set.
  POLL      reply [3] flags (1 text, 2 spotlight, 4 armed), [4] armed count,
            [5] matched bits (sticky since ARM), [6] the highest active layer.
  END       clears text, spotlight and conditions.
The first-run tutorial refuses TEXT, SPOTLIGHT, REVEAL and ARM while it runs. The
master ends the session by itself after 10 s without a cmd 49 report. The DOOM
monolith has no cmd 49 and NACKs every op.
"""

CMD_HOST_TUTORIAL = 49
HOST_TUTORIAL_MIN_PROTOCOL = 35

POLY_CHANNEL = ord("P")
OP_INFO, OP_TEXT, OP_SPOTLIGHT, OP_REVEAL, OP_ARM, OP_POLL, OP_END = range(7)
OP_REMOVED = 7                  # EMOJI_MODE, proposed in qmk#443 and closed unmerged: NACKs
OP_UNKNOWN = 8                  # no op 8 in any version
INFO_OPS = 0x7F
REPORT_BYTES = 64
TEXT_BYTES = 59                 # 64 - 3 header - 2 length bytes
MASK_BYTES = 10
MAX_CONDS = 4
COND_KEY, COND_LAYER, COND_KEYCODE = 1, 2, 3
POLL_TEXT, POLL_SPOT, POLL_ARMED = 0x01, 0x02, 0x04
WATCHDOG_S = 10
LAYER_COUNT = 13                # DYNAMIC_KEYMAP_LAYER_COUNT (split72)
MATRIX_COLS = 8                 # split72


def _req(op: int, *payload: int) -> bytes:
    return bytes([POLY_CHANNEL, CMD_HOST_TUTORIAL, op & 0xFF] + [v & 0xFF for v in payload])


def info_request() -> bytes:
    return _req(OP_INFO)


def text_request(line0: bytes, line1: bytes = b"") -> bytes:
    """Lengths are packed as given, so a test can send a line that runs past the report;
    the request is cut at REPORT_BYTES like a real report."""
    return _req(OP_TEXT, len(line0), *line0, len(line1), *line1)[:REPORT_BYTES]


def text_request_raw(*payload: int) -> bytes:
    """A TEXT report with hand-written length and text bytes."""
    return _req(OP_TEXT, *payload)


def spotlight_request(keys, on: bool = True, flags: int | None = None) -> bytes:
    """`keys` is a list of (row, col); `flags` overrides the flag byte."""
    mask = bytearray(MASK_BYTES)
    for row, col in keys:
        bit = row * MATRIX_COLS + col
        mask[bit // 8] |= 1 << (bit % 8)
    return _req(OP_SPOTLIGHT, (1 if on else 0) if flags is None else flags, *mask)


def reveal_request(row: int, col: int) -> bytes:
    return _req(OP_REVEAL, row, col)


def arm_request(conds) -> bytes:
    """`conds` is a list of (kind, a, b); the count is len(conds), so a test can send 5."""
    return _req(OP_ARM, len(conds), *[v for c in conds for v in c])


def poll_request() -> bytes:
    return _req(OP_POLL)


def end_request() -> bytes:
    return _req(OP_END)


def op_request(op: int) -> bytes:
    return _req(op)


def status(resp):
    """The status byte ('.' or '!') of a cmd 49 reply, or None."""
    if not resp or len(resp) < 3 or resp[0] != POLY_CHANNEL or resp[1] != CMD_HOST_TUTORIAL:
        return None
    return resp[2]


def parse_info(resp):
    """{ops, max_conds, watchdog_s, text_bytes} from an ACKed INFO, else None."""
    if status(resp) != ord(".") or len(resp) < 7:
        return None
    return {"ops": resp[3], "max_conds": resp[4], "watchdog_s": resp[5],
            "text_bytes": resp[6]}


def parse_poll(resp):
    """{flags, armed, matched, layer} from an ACKed POLL, else None."""
    if status(resp) != ord(".") or len(resp) < 7:
        return None
    return {"flags": resp[3], "armed": resp[4], "matched": resp[5], "layer": resp[6]}
