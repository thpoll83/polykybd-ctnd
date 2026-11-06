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


class _Board:
    def __init__(self, protocol=26, keeps_placement=True):
        self.protocol = protocol
        self.keeps_placement = keeps_placement
        self.ram = {"idle": 0, "place": 0, "script": 7}
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
        raise AssertionError(f"unexpected cmd {cmd}")

    def reset(self, _side, _log):
        self.ram = dict(self.eeprom)
        if not self.keeps_placement:
            self.ram["place"] = 0


class RebootPersistenceTest(unittest.TestCase):

    def _run(self, board):
        runner = tr.TestRunner(log=lambda _m: None)
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
        self.assertEqual(board.ram, {"idle": 0, "place": 0, "script": 7})
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


if __name__ == "__main__":
    unittest.main()
