"""Cmd 44 (tap-hold terms): the request packer, the reply parser, and
test_tap_terms_round_trip against a fake board that behaves like hid_cmd_44.
No hardware."""
import sys
import types
import unittest

# The station package imports hidapi at module load; it is not installed off the rig.
if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import hil_tests, tap_terms  # noqa: E402


class FakeBoard:
    """hid_cmd_44: closed ranges, both fields validated before either applies."""

    def __init__(self, validates_first=True):
        self.tap, self.flow = 200, 150
        self.validates_first = validates_first

    @staticmethod
    def _valid(ms, lo, hi):
        return lo <= ms <= hi and ms % tap_terms.STEP_MS == 0

    def send(self, data, **_):
        op = data[2]
        tap, flow = data[3] | (data[4] << 8), data[5] | (data[6] << 8)
        ok = op == 0
        if op == 1:
            t_ok = tap == tap_terms.KEEP or self._valid(tap, 100, 500)
            f_ok = flow == tap_terms.KEEP or self._valid(flow, 0, 300)
            ok = t_ok and f_ok
            if ok or not self.validates_first:
                if t_ok and tap != tap_terms.KEEP:
                    self.tap = tap
                if f_ok and flow != tap_terms.KEEP:
                    self.flow = flow
        return bytes([ord("P"), 44, ord("." if ok else "!"),
                      self.tap & 0xFF, self.tap >> 8, self.flow & 0xFF, self.flow >> 8, 1]
                     ).ljust(64, b"\0")


class PackerTest(unittest.TestCase):
    def test_set_request_is_little_endian(self):
        self.assertEqual(tap_terms.set_request(300, 120),
                         bytes([0x50, 44, 1, 0x2C, 0x01, 120, 0]))

    def test_keep_is_0xffff(self):
        self.assertEqual(tap_terms.set_request(flow_tap_ms=0)[3:7], bytes([0xFF, 0xFF, 0, 0]))

    def test_query_is_op_0(self):
        self.assertEqual(tap_terms.query_request()[:3], bytes([0x50, 44, 0]))

    def test_parse(self):
        reply = bytes([0x50, 44, ord("."), 0x2C, 0x01, 0, 0, 0])
        self.assertEqual(tap_terms.parse_reply(reply), (ord("."), 300, 0, False))
        self.assertIsNone(tap_terms.parse_reply(reply[:7]))
        self.assertIsNone(tap_terms.parse_reply(bytes([0x50, 43]) + reply[2:]))

    def test_other_value_always_changes(self):
        self.assertEqual(tap_terms.other_value(200, 300, 250), 300)
        self.assertEqual(tap_terms.other_value(300, 300, 250), 250)


class RoundTripTest(unittest.TestCase):
    def test_passes_and_restores(self):
        board = FakeBoard()
        board.tap, board.flow = 220, 0
        self.assertTrue(hil_tests.test_tap_terms_round_trip(board, lambda _m: None))
        self.assertEqual((board.tap, board.flow), (220, 0))

    def test_fails_when_one_bad_field_still_applies_the_other(self):
        board = FakeBoard(validates_first=False)
        self.assertFalse(hil_tests.test_tap_terms_round_trip(board, lambda _m: None))
        self.assertEqual((board.tap, board.flow), (200, 150))   # restored anyway

    def test_registered_with_the_protocol_gate(self):
        entry = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_tap_terms_round_trip)
        self.assertEqual(entry["min_protocol"], 29)


if __name__ == "__main__":
    unittest.main()
