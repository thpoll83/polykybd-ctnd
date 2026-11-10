"""Cmd 45 AI and cmd 46 AI_CARD (agent keys, v31): the packers and parsers, and
the three HIL tests against a fake board that behaves like ai_hid_cmd /
ai_hid_card. No hardware."""
import sys
import types
import unittest

# The station package imports hidapi at module load; it is not installed off the rig.
if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import agent_keys as ak, hil_tests  # noqa: E402
from station.console_log import TAP  # noqa: E402


class FakeBoard:
    """ai_hid_cmd / ai_hid_card: closed ranges, validated before anything applies."""

    def __init__(self, found=10, esc=1, seq=0, events=(), accept_bad_flags=False,
                 relay_ok=True, console=True):
        self.found, self.esc, self.seq, self.events = found, esc, seq, list(events)
        self.states, self.flags, self.slots = [0] * 10, 0, [ak.NO_SLOT] * 12
        self.pool = {}
        self.accept_bad_flags = accept_bad_flags
        self.relay_ok = relay_ok
        self.console = console

    def _print(self, line):
        if self.console:
            TAP.feed(line + "\n")

    def _reply(self, cmd, ok, extra=b""):
        return (bytes([ak.POLY_CHANNEL, cmd, ord(".") if ok else ord("!")]) + extra).ljust(64, b"\0")

    def send(self, data, **_):
        data = bytes(data).ljust(64, b"\0")   # RawHID zero-pads every report
        if data[1] == hil_tests.CMD_GET_ID:
            return self._reply(hil_tests.CMD_GET_ID, True)
        if data[1] == ak.CMD_AI_CARD:
            header = data[2] | (data[3] << 8)
            slot, seg = header & 0x3FF, (header >> 12) & 0x7
            ok = slot < ak.POOL_SLOTS and seg < ak.SEGMENTS
            if ok:
                self.pool.setdefault(slot, bytearray(360))[seg * 60:seg * 60 + 60] = data[4:64]
                if not self.relay_ok:
                    self._print(f"ai: card slot {slot} segment {seg} did not reach the slave")
            else:
                self._print("ai: refused card segment")
            return self._reply(ak.CMD_AI_CARD, ok)
        op = data[2]
        if op == 0:
            return self._reply(ak.CMD_AI, True, bytes([0, self.seq, self.found, self.esc]))
        if op == 1:
            states = list(data[3:13])
            flags = data[13]
            slots = [data[14 + 2 * i] | (data[15 + 2 * i] << 8) for i in range(12)]
            ok = (all(s <= ak.STATE_MAX for s in states)
                  and (self.accept_bad_flags or not flags & ~ak.FLAGS_VALID)
                  and all(s < ak.POOL_SLOTS or s == ak.NO_SLOT for s in slots))
            if ok:
                self.states, self.flags, self.slots = states, flags, slots
            return self._reply(ak.CMD_AI, ok)
        if op == 2:
            evs = [e for e in self.events if e[0] > data[3]]
            body = bytes([self.seq, len(evs)]) + b"".join(bytes(e) for e in evs)
            return self._reply(ak.CMD_AI, True, body)
        return self._reply(ak.CMD_AI, False)


class PackTest(unittest.TestCase):
    def test_set_keys_layout(self):
        req = ak.set_keys_request(list(range(10)), 0x0F, [0x0102] * 11 + [ak.NO_SLOT])
        self.assertEqual(req[:3], bytes([ord("P"), 45, 1]))
        self.assertEqual(list(req[3:13]), list(range(10)))
        self.assertEqual(req[13], 0x0F)
        self.assertEqual(req[14:16], bytes([0x02, 0x01]))
        self.assertEqual(req[36:38], b"\xff\xff")
        self.assertEqual(len(req), 3 + 10 + 1 + 24)
        with self.assertRaises(ValueError):
            ak.set_keys_request([0] * 9)

    def test_card_header(self):
        req = ak.card_request(599, 5, b"\x01\x02")
        header = req[2] | (req[3] << 8)
        self.assertEqual((header & 0x3FF, header >> 12), (599, 5))
        self.assertEqual(len(req), 4 + 60)
        self.assertEqual(req[4:6], b"\x01\x02")

    def test_parsers(self):
        self.assertEqual(ak.parse_query(FakeBoard(seq=4).send(ak.query_request())),
                         (ord("."), 0, 4, 10, 1))
        board = FakeBoard(seq=3, events=[(1, 3), (2, 10), (3, 0)])
        self.assertEqual(ak.parse_events(board.send(ak.read_events_request(1))),
                         (ord("."), 3, [(2, 10), (3, 0)]))
        bad = bytes([ord("P"), 45, ord("."), 0, 9]).ljust(64, b"\0")   # count past 8
        self.assertIsNone(ak.parse_events(bad))
        self.assertIsNone(ak.parse_query(b"P\x2c."))


class HilTest(unittest.TestCase):
    def run_test(self, fn, board):
        lines = []
        return fn(board, lines.append), lines

    def test_commands_pass_and_restore(self):
        board = FakeBoard()
        ok, lines = self.run_test(hil_tests.test_agent_keys_commands, board)
        self.assertTrue(ok, "\n".join(lines))
        self.assertEqual((board.states, board.flags), ([0] * 10, 0))   # restored

    def test_commands_fail_when_a_bad_flag_is_accepted(self):
        ok, _ = self.run_test(hil_tests.test_agent_keys_commands, FakeBoard(accept_bad_flags=True))
        self.assertFalse(ok)

    def test_commands_fail_when_keys_are_missing(self):
        self.assertFalse(self.run_test(hil_tests.test_agent_keys_commands, FakeBoard(found=6))[0])
        self.assertFalse(self.run_test(hil_tests.test_agent_keys_commands, FakeBoard(esc=0))[0])

    def test_commands_with_events_in_the_ring(self):
        board = FakeBoard(seq=2, events=[(1, 3), (2, 10)])
        self.assertTrue(self.run_test(hil_tests.test_agent_keys_commands, board)[0])

    def test_card_upload_writes_zeros_back(self):
        board = FakeBoard()
        ok, lines = self.run_test(hil_tests.test_agent_card_upload, board)
        self.assertTrue(ok, "\n".join(lines))
        self.assertEqual(bytes(board.pool[hil_tests.AGENT_CARD_TEST_SLOT]), bytes(360))

    def test_card_relay(self):
        self.assertTrue(self.run_test(hil_tests.test_agent_card_reaches_the_slave, FakeBoard())[0])
        self.assertFalse(self.run_test(hil_tests.test_agent_card_reaches_the_slave,
                                       FakeBoard(relay_ok=False))[0])
        # A console that shows no ai: lines proves nothing: fail, not pass.
        self.assertFalse(self.run_test(hil_tests.test_agent_card_reaches_the_slave,
                                       FakeBoard(console=False))[0])

    def test_registered_and_gated(self):
        names = {t["fn"]: t for t in hil_tests.TESTS}
        for fn in (hil_tests.test_agent_keys_commands, hil_tests.test_agent_card_upload,
                   hil_tests.test_agent_card_reaches_the_slave):
            self.assertEqual(names[fn]["min_protocol"], 31)
        self.assertTrue(names[hil_tests.test_agent_card_reaches_the_slave]["needs_console"])


if __name__ == "__main__":
    unittest.main()
