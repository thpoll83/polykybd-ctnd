"""The status-display watch and the camera's backlight / display measurements."""
import sys
import threading
import types
import unittest

sys.modules.setdefault("hid", types.ModuleType("hid"))

from station import camera, status_watch  # noqa: E402
from station.camera import CameraSettings  # noqa: E402

LEFT_Q = [[0.05, 0.1], [0.45, 0.1], [0.45, 0.9], [0.05, 0.9]]
RIGHT_Q = [[0.55, 0.1], [0.95, 0.1], [0.95, 0.9], [0.55, 0.9]]


class DisplayStateTest(unittest.TestCase):
    def test_levels_measured_on_the_rig(self):
        # Probe backlight_status (2026-11-27): page 76..97, idle 25..47, off 1..3.
        for p95, want in ((1, "dark"), (3, "dark"), (10, "dark"), (25, "idle"),
                          (47, "idle"), (59, "idle"), (60, "text"), (76, "text"), (97, "text")):
            self.assertEqual(camera.display_state(p95), want, p95)


class BacklightJudgeTest(unittest.TestCase):
    def test_both_halves_must_brighten(self):
        ok, lines = camera.judge_backlight({"left": 69.2, "right": 50.2},
                                           {"left": 3.4, "right": 0.4})
        self.assertTrue(ok)
        self.assertEqual(len(lines), 2)
        ok, lines = camera.judge_backlight({"left": 69.2, "right": 8.0},
                                           {"left": 3.4, "right": 0.4})
        self.assertFalse(ok)
        self.assertIn("FAIL", lines[1])
        self.assertNotIn("FAIL", lines[0])


class HalfLevelsTest(unittest.TestCase):
    """A synthetic frame: both quads fill known halves of a 640x480 image."""

    def _frame(self, led_value, display_value, side="left"):
        w, h = 640, 480
        s = CameraSettings.from_config({"quads": {"left": LEFT_Q, "right": RIGHT_Q}})
        board = camera.load_board(side)
        luma = bytearray(w * h)
        to_px = camera.board_mapper(s, side, board, w, h)
        x0, y0, x1, y1 = board["status_display"]
        for y in range(y0, y1):
            for x in range(x0, x1):
                px, py = to_px(x + 0.5, y + 0.5)
                luma[int(py) * w + int(px)] = display_value
        for led in board["leds"]:
            px, py = to_px(led["x"], led["y"])
            luma[int(py) * w + int(px)] = led_value
        return luma, w, h, s, board

    def test_reads_the_leds_and_the_display_box(self):
        for side in ("left", "right"):
            luma, w, h, s, board = self._frame(200, 90, side)
            led, disp = camera.half_levels(bytes(luma), w, h, s, side, board)
            self.assertAlmostEqual(led, 200, delta=1, msg=side)
            self.assertEqual(disp, 90, side)

    def test_a_dark_frame_reads_dark(self):
        luma, w, h, s, board = self._frame(0, 0)
        self.assertEqual(camera.half_levels(bytes(luma), w, h, s, "left", board), (0.0, 0))

    def test_both_boards_have_a_display_box_inside_them(self):
        for side in ("left", "right"):
            board = camera.load_board(side)
            x0, y0, x1, y1 = board["status_display"]
            self.assertTrue(0 <= x0 < x1 <= board["width_mm"] and 0 <= y0 < y1 <= board["height_mm"])


def _levels(left_p95, right_p95):
    return {"left": (0.0, left_p95), "right": (0.0, right_p95)}


class WatchTest(unittest.TestCase):
    def _watch(self, samples):
        it = iter(samples)
        lines = []
        clock = iter(range(0, 1000, 10))
        w = status_watch.StatusWatch(lambda: next(it), camera.display_state, lines.append,
                                     clock=lambda: next(clock))
        w._t0 = 0
        return w, lines

    def test_logs_changes_only(self):
        w, lines = self._watch([_levels(90, 85), _levels(91, 80), _levels(30, 31)])
        w.phase = "test A"
        w.step(); w.step()
        w.phase = "test B"
        w.step()
        changes = [l for l in lines if "WARNING" not in l]
        self.assertEqual(len(changes), 2)
        self.assertIn("left text (90), right text (85)", changes[0])
        self.assertIn("(test B): left idle (30), right idle (31)", changes[1])
        self.assertEqual(w.mismatches, [])
        self.assertIn("always showed the same", w.summary()[-1])

    def test_flags_halves_that_differ(self):
        w, lines = self._watch([_levels(90, 85), _levels(2, 85)])
        w.phase = "glyph script"
        w.step(); w.step()
        self.assertEqual(len(w.mismatches), 1)
        self.assertTrue(any("WARNING: the halves differ during glyph script: left dark, right text"
                            in l for l in lines))
        self.assertIn("left dark, right text", w.summary()[-1])

    def test_the_phase_is_the_one_while_the_clip_records(self):
        lines = []
        w = None

        def sample():
            w.phase = "test B"   # the runner moves on while the clip records
            return _levels(90, 90)
        w = status_watch.StatusWatch(sample, camera.display_state, lines.append, clock=lambda: 0)
        w._t0 = 0
        w.phase = "test A"
        w.step()
        self.assertEqual(w.timeline[0][1], "test A")
        self.assertIn("(test A)", lines[0])

    def test_a_camera_error_skips_one_sample(self):
        def boom():
            raise RuntimeError("camera busy")
        lines = []
        w = status_watch.StatusWatch(boom, camera.display_state, lines.append, clock=lambda: 0)
        w._t0 = 0
        self.assertIsNone(w.step())
        self.assertEqual(w.timeline, [])
        self.assertIn("camera busy", lines[0])

    def test_paused_blocks_a_sample_until_the_block_ends(self):
        order = []
        w = status_watch.StatusWatch(lambda: order.append("sample") or _levels(90, 90),
                                     camera.display_state, lambda _l: None, clock=lambda: 0)
        w._t0 = 0
        status_watch._active = w
        try:
            with status_watch.paused():
                t = threading.Thread(target=w.step)
                t.start()
                t.join(0.2)
                order.append("block")
            t.join(2)
        finally:
            status_watch._active = None
        self.assertEqual(order, ["block", "sample"])

    def test_stop_sampling_takes_no_more_samples(self):
        order = []
        w = status_watch.StatusWatch(lambda: order.append("sample") or _levels(90, 90),
                                     camera.display_state, lambda _l: None, clock=lambda: 0)
        w._t0 = 0
        status_watch._active = w
        try:
            with status_watch.paused():
                t = threading.Thread(target=w.step)   # waits for the pause
                t.start()
                t.join(0.2)
                status_watch.stop_sampling()
            t.join(2)
            self.assertIsNone(w.step())               # a later step samples nothing
        finally:
            status_watch._active = None
        self.assertEqual(order, [])
        self.assertEqual(w.timeline, [])

    def test_the_sampler_keeps_its_clips_out_of_the_ui(self):
        calls = []
        fake = types.SimpleNamespace(
            SETTINGS=None, snapshot_settings=lambda s: s, load_board=lambda side: side,
            record=lambda s, **kw: calls.append(kw) or types.SimpleNamespace(
                lumas=[b""], width=1, height=1),
            half_levels=lambda *a: (0.0, 0))
        status_watch.camera_sampler(fake)()
        self.assertIs(calls[0]["keep_frames"], False)

    def test_paused_without_a_watch_is_a_no_op(self):
        with status_watch.paused():
            pass


if __name__ == "__main__":
    unittest.main()
