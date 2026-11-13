"""TestRunner.reboot_persistence against a fake board (no hardware).

The fake keeps a RAM copy and an EEPROM copy of the idle style and the v26
glyph-script placement; cmd 26 copies RAM to EEPROM, and the RUN-pin reset loads
RAM back from EEPROM. A board that forgets the placement byte must fail.
"""
import sys
import types
import unittest

# Same environment shims as usb_suspend_test.py: the rig's hid and RPi.GPIO
# modules are not installed off the Pi.
if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid
if "RPi" not in sys.modules:  # pragma: no cover - environment shim
    _rpi, _gpio = types.ModuleType("RPi"), types.ModuleType("RPi.GPIO")
    for _name in ("setmode", "setup", "output", "cleanup", "setwarnings"):
        setattr(_gpio, _name, lambda *a, **k: None)
    for _name in ("BCM", "OUT", "HIGH", "LOW"):
        setattr(_gpio, _name, 0)
    _rpi.GPIO = _gpio
    sys.modules["RPi"], sys.modules["RPi.GPIO"] = _rpi, _gpio

import station.test_runner as tr  # noqa: E402
from station import combos, tap_terms  # noqa: E402


class _Board:
    def __init__(self, protocol=26, keeps_placement=True, keeps_tap_terms=True,
                 keeps_auto_shift=True, keeps_combos=True, combo_reaches_other_half=True,
                 combo_echo_index=None):
        self.protocol = protocol
        self.keeps_placement = keeps_placement
        self.keeps_tap_terms = keeps_tap_terms
        self.keeps_auto_shift = keeps_auto_shift
        self.keeps_combos = keeps_combos
        self.combo_reaches_other_half = combo_reaches_other_half
        self.combo_echo_index = combo_echo_index   # None: echo the requested index
        self.combo_ram = [0] * 5       # the last combo slot
        self.combo_flash = [0] * 5     # written by the commit, not by cmd 26
        self.ram = {"idle": 0, "place": 0, "script": 7, "tap": 200, "flow": 150,
                    "as_on": False, "as_ms": 180}
        self.eeprom = dict(self.ram)
        self.sent = []

    def send(self, data, **_):
        self.sent.append(bytes(data))
        cmd, arg = data[1], data[2] if len(data) > 2 else 0
        op = data[3] if len(data) > 3 else 0
        ok = bytes([tr.POLY_CHANNEL, cmd, tr.ACK])
        if cmd == tr.CMD_IDLE_STYLE:
            if arg != 0xFF:
                self.ram["idle"] = arg
            return (ok + bytes([self.ram["idle"]])).ljust(64, b"\0")
        if cmd == tr.CMD_SAVE_EEPROM:
            self.eeprom = dict(self.ram)
            return ok.ljust(64, b"\0")
        if cmd == tr.CMD_GLYPH_SCRIPT:
            if self.protocol >= 26 and op:
                self.ram["place"] = op - 1
            body = bytes([self.ram["script"], self.ram["place"]]) if self.protocol >= 26 \
                else bytes([self.ram["script"]])
            return (ok + body).ljust(64, b"\0")
        if cmd == tap_terms.CMD_TAP_TERMS and self.protocol >= 29:
            d = bytes(data).ljust(64, b"\0")
            if arg == 1:
                tap, flow = d[3] | (d[4] << 8), d[5] | (d[6] << 8)
                if tap != tap_terms.KEEP:
                    self.ram["tap"] = tap
                if flow != tap_terms.KEEP:
                    self.ram["flow"] = flow
                if self.protocol >= 30:
                    as_op, as_ms = d[7], d[8] | (d[9] << 8)
                    if as_ms not in (0, 0xFFFF):
                        self.ram["as_ms"] = as_ms
                    if as_op:
                        self.ram["as_on"] = as_op == tap_terms.AS_ON
            t, f = self.ram["tap"], self.ram["flow"]
            flags, tail = 1, [0, 0]
            if self.protocol >= 30:
                flags |= tap_terms.FLAG_AUTO_SHIFT | (tap_terms.FLAG_AUTO_SHIFT_ON if self.ram["as_on"] else 0)
                tail = [self.ram["as_ms"] & 0xFF, self.ram["as_ms"] >> 8]
            return (ok + bytes([t & 0xFF, t >> 8, f & 0xFF, f >> 8, flags] + tail)).ljust(64, b"\0")
        if cmd == combos.CMD_COMBOS and self.protocol >= 33:
            d = bytes(data).ljust(64, b"\0")
            if arg == combos.OP_COMMIT:
                self.combo_flash = list(self.combo_ram)
                return (ok + bytes([1 if self.combo_reaches_other_half else 0])).ljust(64, b"\0")
            if arg == combos.OP_SET:
                self.combo_ram = [d[5 + 2 * i] | (d[6 + 2 * i] << 8) for i in range(5)]
            index = d[4] if self.combo_echo_index is None else self.combo_echo_index
            body = bytes([d[3], index]) + b"".join(bytes([w & 0xFF, w >> 8]) for w in self.combo_ram)
            return (ok + body).ljust(64, b"\0")
        raise AssertionError(f"unexpected cmd {cmd}")

    def reset(self, _side, _log):
        self.ram = dict(self.eeprom)
        if not self.keeps_placement:
            self.ram["place"] = 0
        if not self.keeps_tap_terms:
            self.ram["tap"], self.ram["flow"] = 200, 150
        self.combo_ram = list(self.combo_flash) if self.keeps_combos else [0] * 5
        if not self.keeps_auto_shift:
            self.ram["as_on"], self.ram["as_ms"] = False, 180


class RebootPersistenceTest(unittest.TestCase):

    def _run(self, board, two_masters=False):
        runner = tr.TestRunner(log=lambda _m: None)
        runner._two_masters_after_apply = two_masters
        runner._raw = board
        runner._caps = {"protocol": board.protocol}
        runner._flash = types.SimpleNamespace(reset=board.reset)
        runner.wait_for_master_ready = lambda *a, **k: True
        runner.settle_master = lambda *a, **k: True
        saved = tr.time
        tr.time = types.SimpleNamespace(sleep=lambda _s: None, monotonic=lambda: 0.0)
        try:
            return runner.reboot_persistence()
        finally:
            tr.time = saved

    def test_v26_carries_the_placement_and_restores_it(self):
        board = _Board()
        result = self._run(board)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(board.ram, {"idle": 0, "place": 0, "script": 7, "tap": 200, "flow": 150,
                                     "as_on": False, "as_ms": 180})
        self.assertEqual(board.eeprom["place"], 0)       # the restore was flushed too
        self.assertIn(bytes([tr.POLY_CHANNEL, tr.CMD_GLYPH_SCRIPT, 0xFF, 2]), board.sent)

    def test_a_board_that_loses_the_placement_fails(self):
        result = self._run(_Board(keeps_placement=False))
        self.assertEqual(result["status"], "fail")
        self.assertIn("placement", result["error"])

    def test_v25_never_sends_the_placement_op(self):
        board = _Board(protocol=25)
        self.assertEqual(self._run(board)["status"], "pass")
        self.assertFalse(any(p[1] == tr.CMD_GLYPH_SCRIPT for p in board.sent))

    def test_v29_carries_the_tap_terms_and_restores_them(self):
        board = _Board(protocol=29)
        result = self._run(board)
        self.assertEqual(result["status"], "pass", result)
        self.assertIn(tap_terms.set_request(300, 90), board.sent)
        self.assertEqual((board.ram["tap"], board.ram["flow"]), (200, 150))
        self.assertEqual((board.eeprom["tap"], board.eeprom["flow"]), (200, 150))

    def test_a_board_that_loses_the_tap_terms_fails(self):
        result = self._run(_Board(protocol=29, keeps_tap_terms=False))
        self.assertEqual(result["status"], "fail")
        self.assertIn("tap terms", result["error"])

    def test_v28_never_sends_cmd_44(self):
        board = _Board(protocol=28)
        self.assertEqual(self._run(board)["status"], "pass")
        self.assertFalse(any(p[1] == tap_terms.CMD_TAP_TERMS for p in board.sent))

    def test_v30_carries_auto_shift_and_restores_it(self):
        board = _Board(protocol=30)
        result = self._run(board)
        self.assertEqual(result["status"], "pass", result)
        self.assertIn(tap_terms.auto_shift_request(tap_terms.AS_ON, 300), board.sent)
        self.assertEqual((board.ram["as_on"], board.ram["as_ms"]), (False, 180))
        self.assertEqual((board.eeprom["as_on"], board.eeprom["as_ms"]), (False, 180))

    def test_a_board_that_loses_auto_shift_fails(self):
        result = self._run(_Board(protocol=30, keeps_auto_shift=False))
        self.assertEqual(result["status"], "fail")
        self.assertIn("auto shift", result["error"])

    def test_v29_never_sends_the_auto_shift_fields(self):
        board = _Board(protocol=29)
        self.assertEqual(self._run(board)["status"], "pass")
        self.assertFalse(any(p[1] == tap_terms.CMD_TAP_TERMS and len(p) > 7 for p in board.sent))

    def test_v33_carries_a_committed_combo_and_restores_it(self):
        board = _Board(protocol=33)
        result = self._run(board)
        self.assertEqual(result["status"], "pass", result)
        self.assertIn(combos.set_request(0, 15, [0x0D, 0x0E, 0, 0, 0x29]), board.sent)
        self.assertEqual(board.combo_flash, [0] * 5)      # restored and committed

    def test_a_board_that_loses_the_combo_fails(self):
        board = _Board(protocol=33, keeps_combos=False)
        result = self._run(board)
        self.assertEqual(result["status"], "fail")
        self.assertIn("combo", result["error"])
        # RAM reads the original after the reset; the test record must not stay in flash.
        self.assertEqual(board.combo_flash, [0] * 5)

    def test_a_commit_that_misses_the_other_half_fails(self):
        result = self._run(_Board(protocol=33, combo_reaches_other_half=False))
        self.assertEqual(result["status"], "fail")
        self.assertIn("combo", result["error"])

    def test_after_an_apply_with_two_masters_this_half_is_enough(self):
        # The apply turned the slave into a second master: no commit can reach it.
        board = _Board(protocol=33, combo_reaches_other_half=False)
        result = self._run(board, two_masters=True)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(board.combo_flash, [0] * 5)      # restored and committed

    def test_a_reply_for_another_slot_is_not_the_last_slot(self):
        result = self._run(_Board(protocol=33, combo_echo_index=3))
        self.assertEqual(result["status"], "fail")

    def test_v32_never_sends_cmd_48(self):
        board = _Board(protocol=32)
        self.assertEqual(self._run(board)["status"], "pass")
        self.assertFalse(any(p[1] == combos.CMD_COMBOS for p in board.sent))


if __name__ == "__main__":
    unittest.main()
