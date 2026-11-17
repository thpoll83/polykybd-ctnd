"""Cmd 49 HOST TUTORIAL (v35): the packers, and both HIL tests against a fake board
that decodes like qmk base/host_tutorial_plan.c and anim/host_tutorial.c. No hardware."""
import sys
import types
import unittest
import unittest.mock as mock

# The station package imports hidapi at module load; it is not installed off the rig.
if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import hil_tests, host_tutorial as ht  # noqa: E402

MATRIX_ROWS, MATRIX_COLS = 10, 8          # split72 config.h
DEFAULT_LAYER = 4


def split72_has_display(row, col):
    """split72.c key_has_display() + key_display_index()'s right-half col-0 fold."""
    if (row, col) in ((3, 7), (8, 0)):
        return False
    return not (5 <= row <= 8 and col == 0)


class FakeBoard:
    """host_tutorial_hid over the pure parsers, with switches that break one rule each,
    so the HIL test is shown to catch it."""

    def __init__(self, check_ascii=True, check_flags=True, check_display=True,
                 max_conds=ht.MAX_CONDS, layer_match=True, end_clears=True, watchdog=True,
                 end_keeps_spot=False, keeps_nothing=False,
                 info=(0x7F, ht.MAX_CONDS, ht.WATCHDOG_S, ht.TEXT_BYTES), protocol=None,
                 emoji_restore=True, emoji_before_ok=True, emoji_check_value=True):
        self.check_ascii, self.check_flags = check_ascii, check_flags
        self.check_display, self.max_conds = check_display, max_conds
        self.layer_match, self.end_clears, self.watchdog = layer_match, end_clears, watchdog
        self.info = info
        self.end_keeps_spot = end_keeps_spot   # END clears all but an active spotlight
        self.keeps_nothing = keeps_nothing     # TEXT and SPOTLIGHT ACK and store nothing
        self.text, self.spot, self.conds, self.matched = None, None, [], 0
        self.sent = []
        self.protocol = protocol        # in GET_ID; None sends no identity
        self.emoji_restore, self.emoji_before_ok = emoji_restore, emoji_before_ok
        self.emoji_check_value = emoji_check_value
        self.emoji, self.emoji_saved = 0, None

    def _reply(self, cmd, ok, body=b""):
        return (bytes([ord("P"), cmd, ord(".") if ok else ord("!")]) + body).ljust(64, b"\0")

    def _end(self):
        if self.emoji_saved is not None and self.emoji_restore:
            self.emoji = self.emoji_saved
        self.emoji_saved = None
        if self.end_clears:
            spot = self.spot if self.end_keeps_spot else None
            self.text, self.spot, self.conds, self.matched = None, spot, [], 0

    def expire(self):
        """The watchdog time passed with no cmd 49."""
        if self.watchdog:
            self._end()

    def send(self, data, **_):
        data = bytes(data).ljust(64, b"\0")
        self.sent.append(data)
        cmd = data[1]
        if cmd == hil_tests.CMD_GET_ID:
            ident = b"" if self.protocol is None else f"Split72 1.13.10 P{self.protocol} HW1 ".encode()
            return self._reply(cmd, True, ident)
        if cmd == hil_tests.CMD_GET_DEFAULT_LAYER:
            return self._reply(cmd, True, bytes([DEFAULT_LAYER]))
        op = data[2]
        if op == ht.OP_INFO:
            return self._reply(ht.CMD_HOST_TUTORIAL, True, bytes(self.info))
        if op == ht.OP_TEXT:
            return self._reply(ht.CMD_HOST_TUTORIAL, self._text(data))
        if op == ht.OP_SPOTLIGHT:
            flags = data[3]
            ok = not (self.check_flags and flags & ~1)
            if ok and not self.keeps_nothing:
                self.spot = data[4:14] if flags & 1 else None
            return self._reply(ht.CMD_HOST_TUTORIAL, ok)
        if op == ht.OP_REVEAL:
            row, col = data[3], data[4]
            ok = row < MATRIX_ROWS and col < MATRIX_COLS and (
                not self.check_display or split72_has_display(row, col))
            return self._reply(ht.CMD_HOST_TUTORIAL, ok)
        if op == ht.OP_ARM:
            return self._reply(ht.CMD_HOST_TUTORIAL, self._arm(data))
        if op == ht.OP_POLL:
            if self.layer_match:
                for i, (kind, a, _b) in enumerate(self.conds):
                    if kind == ht.COND_LAYER and a == DEFAULT_LAYER:
                        self.matched |= 1 << i
            flags = (1 if self.text else 0) | (2 if self.spot is not None else 0) | \
                    (4 if self.conds else 0)
            return self._reply(ht.CMD_HOST_TUTORIAL, True,
                               bytes([flags, len(self.conds), self.matched, DEFAULT_LAYER]))
        if op == ht.OP_END:
            self._end()
            return self._reply(ht.CMD_HOST_TUTORIAL, True)
        if op == ht.OP_EMOJI_MODE and (self.protocol or 0) >= 37:
            before, value = self.emoji, data[3]
            if self.emoji_check_value and value not in (0, 1, 0xFF):
                return self._reply(ht.CMD_HOST_TUTORIAL, False, bytes([before]))
            if value in (0, 1):
                if self.emoji_saved is None:
                    self.emoji_saved = before
                self.emoji = value
            shown = before if self.emoji_before_ok else self.emoji
            return self._reply(ht.CMD_HOST_TUTORIAL, True, bytes([shown]))
        return self._reply(ht.CMD_HOST_TUTORIAL, False)

    def _text(self, data):
        pos, lines = 3, []
        for _ in range(2):
            n = data[pos]
            pos += 1
            if n > ht.TEXT_BYTES or pos + n > 64:
                return False
            line = data[pos:pos + n]
            if self.check_ascii and any(not 0x20 <= ch <= 0x7E for ch in line):
                return False
            lines.append(line)
            pos += n
        if not self.keeps_nothing:
            self.text = lines if any(lines) else None
        return True

    def _arm(self, data):
        n = data[3]
        if n > self.max_conds:
            return False
        conds = [tuple(data[4 + 3 * i:7 + 3 * i]) for i in range(n)]
        for kind, a, b in conds:
            if kind == ht.COND_KEY and (a >= MATRIX_ROWS or b >= MATRIX_COLS):
                return False
            if kind == ht.COND_LAYER and (a >= ht.LAYER_COUNT or b):
                return False
            if kind not in (ht.COND_KEY, ht.COND_LAYER, ht.COND_KEYCODE):
                return False
        self.conds, self.matched = conds, 0
        return True


class PackTest(unittest.TestCase):
    def test_layouts(self):
        self.assertEqual(ht.text_request(b"Hi", b"Fn"), b"P\x31\x01\x02Hi\x02Fn")
        self.assertEqual(len(ht.text_request(b"i" * 59)), 64)
        self.assertEqual(len(ht.text_request(b"i" * 60)), 64)      # cut like a report
        self.assertEqual(ht.reveal_request(9, 0), bytes([ord("P"), 49, 3, 9, 0]))
        self.assertEqual(ht.arm_request([(1, 2, 0), (3, 0x08, 0x7E)]),
                         bytes([ord("P"), 49, 4, 2, 1, 2, 0, 3, 0x08, 0x7E]))
        self.assertEqual(ht.end_request(), b"P\x31\x06")

    def test_spotlight_mask_matches_the_firmware_bit_order(self):
        # host_tut_mask_has(): bit = row * cols + col, byte bit / 8, bit bit % 8.
        req = ht.spotlight_request([(2, 0), (9, 7), (1, 1)])
        mask = req[4:14]
        self.assertEqual(req[3], 1)
        for row in range(MATRIX_ROWS):
            for col in range(MATRIX_COLS):
                bit = row * MATRIX_COLS + col
                lit = bool(mask[bit // 8] & (1 << (bit % 8)))
                self.assertEqual(lit, (row, col) in {(2, 0), (9, 7), (1, 1)})
        self.assertEqual(ht.spotlight_request([], on=False)[3], 0)
        self.assertEqual(ht.spotlight_request([], flags=2)[3], 2)

    def test_parsers(self):
        self.assertEqual(ht.parse_info(b"P\x31.\x7f\x04\x0a\x3b"),
                         {"ops": 0x7F, "max_conds": 4, "watchdog_s": 10, "text_bytes": 59})
        self.assertIsNone(ht.parse_info(b"P\x31!\x7f\x04\x0a\x3b"))
        self.assertEqual(ht.parse_poll(b"P\x31.\x07\x02\x01\x04"),
                         {"flags": 7, "armed": 2, "matched": 1, "layer": 4})
        self.assertIsNone(ht.status(b"P\x30."))


class HilTest(unittest.TestCase):
    def run_test(self, board):
        lines = []
        return hil_tests.test_host_tutorial(board, lines.append), lines

    def test_passes_and_ends_with_end(self):
        board = FakeBoard()
        ok, lines = self.run_test(board)
        self.assertTrue(ok, "\n".join(lines))
        last = [d for d in board.sent if d[1] == ht.CMD_HOST_TUTORIAL][-1]
        self.assertEqual(last[2], ht.OP_END)
        self.assertEqual((board.text, board.spot, board.conds), (None, None, []))

    def test_catches_each_broken_rule(self):
        broken = {
            "accepts UTF-8": FakeBoard(check_ascii=False),
            "accepts an unknown spotlight flag": FakeBoard(check_flags=False),
            "accepts a key without a display": FakeBoard(check_display=False),
            "accepts five conditions": FakeBoard(max_conds=5),
            "never matches the layer": FakeBoard(layer_match=False),
            "END keeps state": FakeBoard(end_clears=False),
            "END keeps an active spotlight": FakeBoard(end_keeps_spot=True),
            "INFO reports another watchdog": FakeBoard(info=(0x7F, 4, 5, 59)),
        }
        for why, board in broken.items():
            with self.subTest(why):
                self.assertFalse(self.run_test(board)[0])

    def test_info_ops_follow_the_protocol(self):
        self.assertTrue(self.run_test(FakeBoard(protocol=36))[0])
        self.assertTrue(self.run_test(FakeBoard(protocol=37, info=(0xFF, 4, 10, 59)))[0])
        self.assertFalse(self.run_test(FakeBoard(protocol=37))[0])     # 7 ops on a v37
        self.assertFalse(self.run_test(FakeBoard(protocol=36, info=(0xFF, 4, 10, 59)))[0])

    def test_old_firmware_nacks_info(self):
        board = FakeBoard()
        board.send = lambda data, **_: board._reply(ht.CMD_HOST_TUTORIAL, False)
        self.assertFalse(self.run_test(board)[0])


class EmojiModeTest(unittest.TestCase):
    def run_test(self, board):
        lines = []
        return hil_tests.test_host_tutorial_emoji_mode(board, lines.append), lines

    def test_passes_and_leaves_the_mode(self):
        for mode in (0, 1):
            with self.subTest(mode=mode):
                board = FakeBoard(protocol=37)
                board.emoji = mode
                ok, lines = self.run_test(board)
                self.assertTrue(ok, "\n".join(lines))
                self.assertEqual(board.emoji, mode)

    def test_catches_each_broken_rule(self):
        broken = {
            "END does not restore": FakeBoard(protocol=37, emoji_restore=False),
            "the reply carries the new mode": FakeBoard(protocol=37, emoji_before_ok=False),
            "accepts value 2": FakeBoard(protocol=37, emoji_check_value=False),
            "v36 NACKs op 7": FakeBoard(protocol=36),
        }
        for why, board in broken.items():
            with self.subTest(why):
                self.assertFalse(self.run_test(board)[0])


class WatchdogTest(unittest.TestCase):
    def run_test(self, board):
        lines = []
        clock = [0.0]

        def sleep(s):
            clock[0] += s
            if clock[0] >= ht.WATCHDOG_S:
                board.expire()
        with mock.patch.object(hil_tests.time, "monotonic", lambda: clock[0]), \
                mock.patch.object(hil_tests.time, "sleep", sleep):
            return hil_tests.test_host_tutorial_watchdog(board, lines.append), lines

    def test_passes_when_the_session_ends(self):
        ok, lines = self.run_test(FakeBoard())
        self.assertTrue(ok, "\n".join(lines))

    def test_fails_without_a_watchdog(self):
        self.assertFalse(self.run_test(FakeBoard(watchdog=False))[0])

    def test_fails_when_text_and_spotlight_store_nothing(self):
        # Otherwise the final POLL reads 0 without any watchdog at work.
        self.assertFalse(self.run_test(FakeBoard(keeps_nothing=True))[0])


class RegistrationTest(unittest.TestCase):
    def test_gates_and_tier(self):
        by_fn = {t["fn"]: t for t in hil_tests.TESTS}
        main = by_fn[hil_tests.test_host_tutorial]
        dog = by_fn[hil_tests.test_host_tutorial_watchdog]
        self.assertEqual(main["min_protocol"], 35)
        self.assertNotIn("tier", main)
        self.assertEqual(dog["min_protocol"], 35)
        emoji = by_fn[hil_tests.test_host_tutorial_emoji_mode]
        self.assertEqual(emoji["min_protocol"], 37)
        self.assertNotIn("tier", emoji)
        self.assertEqual(dog["tier"], hil_tests.TIER_EXTENDED)


if __name__ == "__main__":
    unittest.main()
