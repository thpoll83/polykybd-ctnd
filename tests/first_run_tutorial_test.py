"""First-run tutorial (TIER_TUTORIAL): the cmd 14 packer, the console parsers, and the
test run against a fake board that answers like qmk lib/hid/hid_com.c hid_cmd_14 and
prints what features/anim/tutorial.c prints. No hardware."""
import sys
import types
import unittest

# The rig's hid and RPi.GPIO modules are not installed off the Pi.
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

from station import hil_tests, first_run_tutorial as frt  # noqa: E402
from station import test_runner  # noqa: E402

LETTERS = ["Q", "J", "C"]
SKIP_HOLD_MS = 1000


class FakeTap:
    """mark / since / wait_for like station.console_log.ConsoleTap, without blocking."""

    def __init__(self):
        self.lines = []

    def emit(self, line):
        self.lines.append(line)

    def mark(self):
        return len(self.lines)

    def since(self, mark):
        return self.lines[mark:]

    def wait_for(self, pattern, mark=0, timeout=10.0, poll=0.1):
        for line in self.lines[mark:]:
            if (pattern.search(line) if hasattr(pattern, "search") else pattern in line):
                return line
        return None


class FakeBoard:
    """cmd 14 and the tutorial's console lines. Each switch breaks one rule, so the test
    is shown to catch it."""

    def __init__(self, tap, clock, debug=True, accept_letters=True, accept_shift=True,
                 log_phases=True, skip_works=True):
        self.tap, self.clock = tap, clock
        self.debug, self.accept_letters = debug, accept_letters
        self.accept_shift, self.log_phases, self.skip_works = accept_shift, log_phases, skip_works
        self.layer = "L0"
        self.settings_more = False
        self.phase = None
        self.letter_i = 0
        self.esc_down_at = None
        self.down = set()
        self.sent = []

    # the console
    def _phase(self, *names):
        for name in names:
            self.phase = name
            if self.log_phases:
                self.tap.emit(f"Tutorial phase {name}")

    # cmd 14
    def send(self, data, timeout_ms=3000, attempts=3):
        assert attempts == 1, "cmd 14 must not be retried"
        assert data[0] == frt.POLY_CHANNEL and data[1] == frt.CMD_KEYPRESS
        kc = data[2] << 8 | data[3]
        pressed = data[4] == 0
        self.sent.append((kc, pressed))
        # hid_cmd_14 releases a key it pressed at the press's position, whatever the
        # layer is now; anything else is looked up on the current layer.
        held = not pressed and kc in self.down
        if not self.debug or not (held or self._on_layer(kc)):
            return bytes([ord("P"), 14, ord("!")])
        if pressed:
            self.down.add(kc)
            self._press(kc)
        else:
            self.down.discard(kc)
            self._release(kc)
        return bytes([ord("P"), 14, ord(".")])

    def _on_layer(self, kc):
        if kc == frt.MO_FL:
            return True              # on _L0, the default layer
        if kc == frt.TO_SL:
            return self.layer == "FL"
        if kc == frt.KC_SETTINGS_MORE:
            return self.layer == "SL"
        if kc == frt.KC_EDEN:
            return self.layer == "SL" and self.settings_more
        return True

    def _press(self, kc):
        if self.phase is None:
            if kc == frt.MO_FL:
                self.layer = "FL"
            elif kc == frt.TO_SL:
                self.layer = "SL"
            elif kc == frt.KC_SETTINGS_MORE:
                self.settings_more = True
            elif kc == frt.KC_EDEN:
                self.tap.emit("Tutorial start (left=1, master=1)")
                for i, letter in enumerate(LETTERS):
                    self.tap.emit(f"Tutorial slot{i}: LEFT idx={i} letter={letter}({ord(letter)}) cands=26")
                self.layer = "L0"
                self._phase("BLANK", "TEXT", "LETTER_IN")
            return
        if self.phase == "LETTER_IN" and self.accept_letters:
            if kc == frt.letter_keycode(LETTERS[self.letter_i]):
                self.letter_i += 1
                if self.letter_i < len(LETTERS):
                    self._phase("RIPPLE", "GAP", "LETTER_IN")
                else:
                    self._phase("RIPPLE", "GAP", "REVEAL", "SHIFT_WAIT")
        elif self.phase == "SHIFT_WAIT" and kc == frt.KC_LSFT and self.accept_shift:
            self._phase("SHIFT_SWEEP", "SHIFT_HELD", "SHIFT_AGAIN")
        elif self.phase == "SHIFT_AGAIN" and kc == frt.KC_RSFT and self.accept_shift:
            self._phase("SHIFT_SWEEP", "SHIFT_HELD", "LAYER_WAIT")
        elif self.phase == "LAYER_WAIT" and kc == frt.MO_FL:
            self._phase("LAYER_SWEEP", "LAYER_HELD", "NOTATION")
        if kc == frt.KC_ESC:
            self.esc_down_at = self.clock[0]

    def _release(self, kc):
        if self.phase is None and kc == frt.MO_FL and self.layer == "FL":
            self.layer = "L0"
        if kc == frt.KC_ESC and self.esc_down_at is not None:
            held_ms = (self.clock[0] - self.esc_down_at) * 1000
            self.esc_down_at = None
            if held_ms >= SKIP_HOLD_MS and self.skip_works and self.phase is not None:
                self._phase("DONE")
                self.tap.emit("Tutorial skipped; displays handed back")


def make(**switches):
    tap, clock, logs = FakeTap(), [0.0], []

    def sleep(s):
        clock[0] += s

    board = FakeBoard(tap, clock, **switches)
    run = frt.TutorialRun(board, tap, logs.append, sleep=sleep)
    return run, board, logs


class PackerTest(unittest.TestCase):
    def test_press_and_release(self):
        self.assertEqual(frt.keypress_request(frt.KC_EDEN, True), bytes([0x50, 14, 0x7F, 0xA0, 0]))
        self.assertEqual(frt.keypress_request(frt.MO_FL, False), bytes([0x50, 14, 0x52, 0x25, 1]))

    def test_keycodes_match_the_firmware(self):
        self.assertEqual(frt.MO_FL, 0x5225)        # MO(_FL), _FL = 5
        self.assertEqual(frt.TO_SL, 0x5209)        # TO(_SL), _SL = 9
        self.assertEqual(frt.KC_EDEN, 0x7FA0)      # keycode_helper.h static_assert
        self.assertEqual(frt.KC_SETTINGS_MORE, 0x7FA3)

    def test_letter_keycode(self):
        self.assertEqual(frt.letter_keycode("A"), 0x04)
        self.assertEqual(frt.letter_keycode("z"), 0x1D)
        with self.assertRaises(ValueError):
            frt.letter_keycode("1")


class ParserTest(unittest.TestCase):
    def test_letters_in_slot_order(self):
        lines = ["Tutorial slot2: RIGHT idx=3 letter=K(75) cands=20",
                 "Tutorial slot0: LEFT idx=9 letter=a(97) cands=20",
                 "noise",
                 "Tutorial slot1: LEFT idx=1 letter=S(83) cands=20"]
        self.assertEqual(frt.parse_letters(lines), ["A", "S", "K"])

    def test_missing_slot_gives_nothing(self):
        lines = ["Tutorial slot0: LEFT idx=9 letter=A(65) cands=20",
                 "Tutorial slot2: RIGHT idx=3 letter=K(75) cands=20"]
        self.assertEqual(frt.parse_letters(lines), [])

    def test_phase_pattern_is_the_whole_name(self):
        pat = frt.phase_pattern("SHIFT_WAIT")
        self.assertTrue(pat.search("Tutorial phase SHIFT_WAIT"))
        self.assertFalse(pat.search("Tutorial phase SHIFT_WAITING"))
        self.assertFalse(frt.phase_pattern("LANG_MORE").search("Tutorial phase LANG_MORE2"))


class RunTest(unittest.TestCase):
    def test_a_working_board_passes(self):
        run, board, logs = make()
        self.assertTrue(run.run(), "\n".join(logs))
        self.assertEqual(board.phase, "DONE")
        self.assertEqual(run.held, [])
        self.assertEqual(board.down, set())

    def test_not_a_debug_build(self):
        run, board, logs = make(debug=False)
        self.assertFalse(run.run())
        self.assertTrue(any("POLYKYBD_DEBUG" in line for line in logs))

    def test_a_letter_that_is_not_accepted(self):
        run, _, logs = make(accept_letters=False)
        self.assertFalse(run.run())
        self.assertTrue(any("RIPPLE" in line for line in logs))

    def test_shift_that_does_nothing(self):
        run, _, logs = make(accept_shift=False)
        self.assertFalse(run.run())
        self.assertTrue(any("did not sweep" in line for line in logs))

    def test_no_phase_lines(self):
        run, _, logs = make(log_phases=False)
        self.assertFalse(run.run())
        self.assertTrue(any("LETTER_IN" in line for line in logs))

    def test_skip_that_does_not_end_the_lesson(self):
        run, _, logs = make(skip_works=False)
        self.assertFalse(run.run())
        self.assertTrue(any("did not end the lesson" in line for line in logs))

    def test_esc_is_held_past_the_skip_time(self):
        self.assertGreater(frt.SKIP_HOLD_S * 1000, SKIP_HOLD_MS)

    def test_abandon_skips_an_unfinished_lesson(self):
        run, board, logs = make(accept_shift=False)
        self.assertFalse(run.run())
        run.abandon()
        self.assertEqual(board.phase, "DONE")
        self.assertTrue(any("skipped the unfinished lesson" in line for line in logs))
        self.assertEqual(board.down, set())

    def test_abandon_before_the_start_presses_nothing(self):
        run, board, _ = make(debug=False)
        self.assertFalse(run.run())
        sent = len(board.sent)
        run.abandon()
        self.assertEqual(len(board.sent), sent)

    def test_release_all_after_a_failure(self):
        run, board, _ = make()
        run.press(frt.KC_LSFT)
        run.release_all()
        self.assertEqual(run.held, [])
        self.assertEqual(board.down, set())


class GateTest(unittest.TestCase):
    def test_tier_is_fail_closed(self):
        test = {"tier": hil_tests.TIER_TUTORIAL}
        self.assertIn("--tutorial", hil_tests.skip_reason(test, {}))
        self.assertIsNone(hil_tests.skip_reason(test, {"tutorial": True}))

    def test_registered_once_and_gated(self):
        entries = [t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_first_run_tutorial]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tier"], hil_tests.TIER_TUTORIAL)
        self.assertTrue(entries[0]["needs_console"])

    def test_tutorial_suite_is_the_tutorial_and_the_crash_scan(self):
        suite = hil_tests.tutorial_suite(hil_tests.TESTS)
        self.assertEqual([t["fn"] for t in suite],
                         [hil_tests.test_first_run_tutorial, hil_tests.test_no_crash_record])

    def test_runner_flag(self):
        args = test_runner.build_parser().parse_args(
            ["--left", "l.uf2", "--right", "r.uf2", "--tutorial"])
        self.assertTrue(args.tutorial)
        args = test_runner.build_parser().parse_args(["--left", "l.uf2", "--right", "r.uf2"])
        self.assertFalse(args.tutorial)


if __name__ == "__main__":
    unittest.main()
