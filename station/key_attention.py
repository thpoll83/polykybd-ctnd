"""Cmd 47 KEY ATTENTION (protocol v32): the focus ripple on one key. Request
packing and reply parsing for the HIL test, unit-tested offline.

Mirrors qmk_firmware keyboards/polykybd/anim/key_attention.c (key_attention_hid)
and PROTOCOL_HISTORY.md, v32:
  data[2] op, CLOSED: 0 START, 1 CANCEL; any other op NACKs.
  START   data[3] row, data[4] col (matrix position; a key without a display
          NACKs), data[5] ripple count (0 = until cancelled), data[6] period in
          100 ms units, CLOSED 23..100. A bad field NACKs and changes nothing; a
          START replaces a running request.
  CANCEL  ends the request; a ripple already running finishes.
  Reply "P\\x2F." (ACK) or "P\\x2F!" (NACK), no data. A refused op prints
  "attn: refused op N" to the console. RAM only.

v34 adds op 2 GUIDE (base/attn_guide.c): data[3] ripples in total (0 = until done),
data[4] period (the same closed range), data[5] target layer, data[6] steps 1..4,
then {row, col, layer} per step from data[7]. Layers are below
DYNAMIC_KEYMAP_LAYER_COUNT (13); two steps on one layer, a step on the target layer
and a key without a display NACK.
"""

CMD_KEY_ATTENTION = 47
KEY_ATTENTION_MIN_PROTOCOL = 32
KEY_GUIDE_MIN_PROTOCOL = 34

POLY_CHANNEL = ord("P")
OP_START, OP_CANCEL, OP_GUIDE = 0, 1, 2
GUIDE_MAX_STEPS = 4          # ATTN_GUIDE_MAX_STEPS
LAYER_COUNT = 13             # DYNAMIC_KEYMAP_LAYER_COUNT (split72)

PERIOD_MIN = 23              # KEY_ATTN_PERIOD_MIN, 100 ms units (one ripple runs 2.2 s)
PERIOD_MAX = 100             # KEY_ATTN_PERIOD_MAX

REFUSED_LINE = "attn: refused op"


def start_request(row: int, col: int, count: int, period: int) -> bytes:
    """Values are packed as given (masked to a byte), so a test can send an
    out-of-range one."""
    return bytes([POLY_CHANNEL, CMD_KEY_ATTENTION, OP_START,
                  row & 0xFF, col & 0xFF, count & 0xFF, period & 0xFF])


def guide_request(steps, target: int, count: int, period: int) -> bytes:
    """`steps` is a list of (row, col, layer). Values are packed as given (masked to
    a byte); the step count is len(steps), so a test can send 0 or 5."""
    flat = [v & 0xFF for s in steps for v in s]
    return bytes([POLY_CHANNEL, CMD_KEY_ATTENTION, OP_GUIDE, count & 0xFF, period & 0xFF,
                  target & 0xFF, len(steps) & 0xFF] + flat)


def cancel_request() -> bytes:
    return bytes([POLY_CHANNEL, CMD_KEY_ATTENTION, OP_CANCEL])


def op_request(op: int) -> bytes:
    return bytes([POLY_CHANNEL, CMD_KEY_ATTENTION, op & 0xFF])


def status(resp):
    """The status byte ('.' or '!') of a cmd 47 reply, or None."""
    if not resp or len(resp) < 3 or resp[0] != POLY_CHANNEL or resp[1] != CMD_KEY_ATTENTION:
        return None
    return resp[2]
