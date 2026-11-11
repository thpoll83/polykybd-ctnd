"""Cmd 47 KEY ATTENTION (v32): the packers, and the HIL test against a fake board
that validates like key_attention_hid. No hardware."""
import sys
import types
import unittest

# The station package imports hidapi at module load; it is not installed off the rig.
if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import hil_tests, key_attention as ka  # noqa: E402

MATRIX_ROWS, MATRIX_COLS = 10, 8          # split72 config.h


def split72_has_display(row, col):
    """split72.c key_has_display() + key_display_index()'s right-half col-0 fold."""
    if (row, col) in ((3, 7), (8, 0)):
        return False
    return not (5 <= row <= 8 and col == 0)


class FakeBoard:
    """key_attention_hid: CANCEL always ACKs; START validates every field before
    anything changes; any other op NACKs."""

    def __init__(self, period_min=ka.PERIOD_MIN, period_max=ka.PERIOD_MAX,
                 check_display=True, cancel_ok=True):
        self.period_min, self.period_max = period_min, period_max
        self.check_display = check_display
        self.cancel_ok = cancel_ok
        self.request = None                  # (row, col, count, period) or None
        self.seq = 0
        self.sent = []

    def _reply(self, cmd, ok):
        return bytes([ord("P"), cmd, ord(".") if ok else ord("!")]).ljust(64, b"\0")

    def send(self, data, **_):
        data = bytes(data).ljust(64, b"\0")   # RawHID zero-pads every report
        self.sent.append(data)
        if data[1] == hil_tests.CMD_GET_ID:
            return self._reply(hil_tests.CMD_GET_ID, True)
        op = data[2]
        if op == ka.OP_CANCEL:
            if self.cancel_ok:
                self.request, self.seq = None, self.seq + 1
            return self._reply(ka.CMD_KEY_ATTENTION, self.cancel_ok)
        ok = op == ka.OP_START
        if ok:
            row, col, count, period = data[3], data[4], data[5], data[6]
            ok = (row < MATRIX_ROWS and col < MATRIX_COLS
                  and self.period_min <= period <= self.period_max
                  and (not self.check_display or split72_has_display(row, col)))
        if ok:
            self.request, self.seq = (row, col, count, period), self.seq + 1
        return self._reply(ka.CMD_KEY_ATTENTION, ok)


class PackTest(unittest.TestCase):
    def test_start_layout(self):
        self.assertEqual(ka.start_request(1, 2, 3, 23), bytes([ord("P"), 47, 0, 1, 2, 3, 23]))
        self.assertEqual(ka.start_request(255, 0, 0, 101)[3], 255)
        self.assertEqual(ka.start_request(0, 0, 0, 101)[6], 101)

    def test_cancel_and_op(self):
        self.assertEqual(ka.cancel_request(), b"P\x2f\x01")
        self.assertEqual(ka.op_request(2), b"P\x2f\x02")

    def test_status(self):
        self.assertEqual(ka.status(b"P\x2f."), ord("."))
        self.assertEqual(ka.status(b"P\x2f!"), ord("!"))
        self.assertIsNone(ka.status(b"P\x2e."))      # another command's reply
        self.assertIsNone(ka.status(b"P\x2f"))
        self.assertIsNone(ka.status(None))

    def test_period_range_matches_firmware(self):
        self.assertEqual((ka.PERIOD_MIN, ka.PERIOD_MAX), (23, 100))

    def test_test_keys_match_split72(self):
        self.assertTrue(split72_has_display(*hil_tests.KEY_ATTN_TEST_KEY))
        self.assertFalse(split72_has_display(*hil_tests.KEY_ATTN_NO_DISPLAY_KEY))


class HilTest(unittest.TestCase):
    def run_test(self, board):
        lines = []
        return hil_tests.test_key_attention(board, lines.append), lines

    def test_passes_and_ends_cancelled(self):
        board = FakeBoard()
        ok, lines = self.run_test(board)
        self.assertTrue(ok, "\n".join(lines))
        self.assertIsNone(board.request)
        last_attn = [d for d in board.sent if d[1] == ka.CMD_KEY_ATTENTION][-1]
        self.assertEqual(last_attn[2], ka.OP_CANCEL)

    def test_fails_when_period_22_is_accepted(self):
        ok, _ = self.run_test(FakeBoard(period_min=22))
        self.assertFalse(ok)

    def test_fails_when_period_101_is_accepted(self):
        self.assertFalse(self.run_test(FakeBoard(period_max=101))[0])

    def test_fails_when_period_100_is_refused(self):
        self.assertFalse(self.run_test(FakeBoard(period_max=99))[0])

    def test_fails_when_a_key_without_a_display_is_accepted(self):
        self.assertFalse(self.run_test(FakeBoard(check_display=False))[0])

    def test_fails_when_cancel_is_refused(self):
        self.assertFalse(self.run_test(FakeBoard(cancel_ok=False))[0])

    def test_cancels_even_after_a_failure(self):
        board = FakeBoard(period_min=22)
        self.run_test(board)
        last_attn = [d for d in board.sent if d[1] == ka.CMD_KEY_ATTENTION][-1]
        self.assertEqual(last_attn[2], ka.OP_CANCEL)
        self.assertIsNone(board.request)

    def test_registered_and_gated(self):
        entries = [t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_key_attention]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["min_protocol"], 32)
        self.assertNotIn("tier", entry)              # default tier
        self.assertNotIn("needs_console", entry)


if __name__ == "__main__":
    unittest.main()
