# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for station/camera.py: settings, parsing and the phase analysis.

The frames are synthetic: each half of the image breathes with the firmware's
2048 ms triangle, the right half delayed by a known amount, over a static
texture that the variance filter has to ignore.
"""
import random
import sys
import types
import unittest

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    sys.modules["hid"] = _hid
if "RPi" not in sys.modules:  # pragma: no cover - environment shim
    _rpi, _gpio = types.ModuleType("RPi"), types.ModuleType("RPi.GPIO")
    for _name in ("setmode", "setup", "output", "cleanup", "setwarnings"):
        setattr(_gpio, _name, lambda *a, **k: None)
    for _name in ("BCM", "OUT", "HIGH", "LOW"):
        setattr(_gpio, _name, 0)
    _rpi.GPIO = _gpio
    sys.modules["RPi"], sys.modules["RPi.GPIO"] = _rpi, _gpio

from station import camera  # noqa: E402
from station.camera import CameraSettings, Recording  # noqa: E402


def tri(t_ms: float, period: float = camera.FLASH_CUE_PERIOD_MS) -> float:
    """The flash cue's brightness curve: phase = t >> 3, triangle 0..127..0."""
    phase = (t_ms / period) % 1.0
    return phase * 2 if phase < 0.5 else 2 - phase * 2


def synth(delay_ms: float, seconds: float = 8.0, fps: float = 58.0,
          width: int = 64, height: int = 16, swing: float = 30.0,
          seed: int = 1) -> Recording:
    rnd = random.Random(seed)
    texture = bytes(rnd.randrange(20, 60) for _ in range(width * height))
    # LEDs: a block in each half; the rest of the image is the static texture.
    rows = range(height // 4, height * 3 // 4)
    led_l = {r * width + c for r in rows for c in range(width // 8, width * 3 // 8)}
    led_r = {r * width + c for r in rows for c in range(width * 5 // 8, width * 7 // 8)}
    ts, lumas, t = [], [], 0.0
    while t < seconds * 1000:
        bl = int(swing * tri(t))
        br = int(swing * tri(t - delay_ms))
        frame = bytearray(texture)
        for i in led_l:
            frame[i] = min(255, frame[i] + bl)
        for i in led_r:
            frame[i] = min(255, frame[i] + br)
        ts.append(t)
        lumas.append(bytes(frame))
        t += 1000.0 / fps + rnd.uniform(-0.5, 0.5)   # frame timing jitter
    return Recording(width, height, ts, lumas)


class StepIntervalTest(unittest.TestCase):
    """How often each half's LEDs change level, from the camera trace."""

    @staticmethod
    def staircase(update_ms: float, fps: float = 62.0, seconds: float = 8.0):
        # The cue's 32 levels over a 2048 ms triangle, held between LED updates
        # that come every update_ms.
        ts, vals, t = [], [], 0.0
        while t < seconds * 1000:
            shown = (t // update_ms) * update_ms
            vals.append(float(int(31 * tri(shown))))
            ts.append(t)
            t += 1000.0 / fps
        return ts, vals

    def test_normal_redraw_changes_about_every_level(self):
        ts, vals = self.staircase(update_ms=8.0)
        self.assertLess(camera.step_interval_ms(ts, vals), 50.0)

    def test_sparse_redraw_holds_levels_longer(self):
        ts, vals = self.staircase(update_ms=100.0)
        self.assertGreater(camera.step_interval_ms(ts, vals), 80.0)

    def test_flat_trace_gives_zero(self):
        self.assertEqual(camera.step_interval_ms([0.0, 16.0, 32.0], [5.0, 5.0, 5.0]), 0.0)

    def test_judge_reports_the_steps(self):
        res = camera.PhaseResult(offset_ms=-50.0, correlation=0.99, swing_left=120,
                                 swing_right=130, frames=480, fps=62,
                                 step_left_ms=97.0, step_right_ms=32.0)
        self.assertIn("LED steps L 97 / R 32 ms", camera.judge(res)["info"])


class FramesTest(unittest.TestCase):
    """The frames the UI shows: the recording in progress and the last one."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_png_gray_is_a_valid_png(self):
        import struct
        import zlib
        png = camera.png_gray(3, 2, bytes([0, 128, 255, 10, 20, 30]))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        w, h = struct.unpack(">II", png[16:24])
        self.assertEqual((w, h), (3, 2))
        idat = png.index(b"IDAT")
        n = struct.unpack(">I", png[idat - 4:idat])[0]
        rows = zlib.decompress(png[idat + 4:idat + 4 + n])
        self.assertEqual(rows, b"\x00\x00\x80\xff\x00\x0a\x14\x1e")

    def test_label_of_a_ci_pr_run(self):
        lab = camera.recording_label({
            "GITHUB_ACTIONS": "true", "GITHUB_RUN_NUMBER": "1870", "GITHUB_RUN_ID": "9",
            "GITHUB_JOB": "hil-test", "GITHUB_REF": "refs/pull/485/merge",
            "GITHUB_SHA": "317d3111fa68"})
        self.assertEqual(lab["text"], "CI run #1870 · hil-test · PR #485 · 317d311")

    def test_label_of_a_ci_branch_run(self):
        lab = camera.recording_label({
            "GITHUB_ACTIONS": "true", "GITHUB_RUN_NUMBER": "7", "GITHUB_JOB": "fwapply-test",
            "GITHUB_REF": "refs/heads/PolyKybd", "GITHUB_REF_NAME": "PolyKybd",
            "GITHUB_SHA": "5d9b041000"})
        self.assertEqual(lab["text"], "CI run #7 · fwapply-test · PolyKybd · 5d9b041")

    def test_label_outside_ci(self):
        self.assertEqual(camera.recording_label({})["source"], "ui")

    def test_three_frames_kept_with_their_times(self):
        rec = Recording(2, 2, [100.0 + 16 * i for i in range(9)],
                        [bytes([i] * 4) for i in range(9)])
        meta = camera.save_frames(rec, {"text": "x"}, 1700000000.0, self.dir)
        self.assertEqual([f["t_ms"] for f in meta["frames"]], [0.0, 64.0, 128.0])
        self.assertEqual(camera.load_frames(self.dir)["label"]["text"], "x")
        for k in range(3):
            self.assertTrue((self.dir / f"frame{k}.png").read_bytes().startswith(b"\x89PNG"))

    def test_live_frame_is_the_newest_complete_one(self):
        import json
        import time
        w, h = 4, 2
        frame = w * h * 3 // 2
        (self.dir / "rec.json").write_text(json.dumps(
            {"width": w, "height": h, "started": time.time(), "label": {"text": "CI run #1"}}))
        # Two complete frames and half of a third (still being written).
        data = bytes([1] * frame) + bytes([2] * frame) + bytes([3] * (frame // 2))
        (self.dir / "rec.yuv").write_bytes(data)
        png, meta = camera.live_frame(self.dir)
        self.assertEqual(meta["frame"], 2)
        self.assertEqual(meta["label"]["text"], "CI run #1")
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_no_live_frame_without_a_recording(self):
        self.assertIsNone(camera.live_frame(self.dir))


class SettingsTest(unittest.TestCase):
    def test_defaults(self):
        s = CameraSettings.from_config({})
        self.assertEqual(s.enabled, "auto")
        self.assertEqual(s.width % 64, 0)

    def test_yaml_bools_for_enabled(self):
        self.assertEqual(CameraSettings.from_config({"enabled": True}).enabled, "yes")
        self.assertEqual(CameraSettings.from_config({"enabled": False}).enabled, "no")
        self.assertEqual(CameraSettings.from_config({"enabled": "Auto"}).enabled, "auto")

    def test_bool_typo_is_an_error(self):
        for bad in ({"hflip": "flase"}, {"master_on_left": "ture"}, {"vflip": 2}):
            with self.assertRaises(ValueError, msg=bad):
                CameraSettings.from_config(bad)

    def test_bool_strings(self):
        s = CameraSettings.from_config({"hflip": "false", "vflip": "yes"})
        self.assertFalse(s.hflip)
        self.assertTrue(s.vflip)

    def test_rejects_bad_values(self):
        for bad in ({"rotation": 90}, {"width": 300}, {"split_x": 1.0},
                    {"enabled": "maybe"}):
            with self.assertRaises(ValueError, msg=bad):
                CameraSettings.from_config(bad)

    def test_roi(self):
        s = CameraSettings.from_config({"roi": "0.1,0.25,0.8,0.5"})
        self.assertEqual(s.orient_args()[-2:], ["--roi", "0.1,0.25,0.8,0.5"])
        for bad in ("0.1,0.2,0.8", "a,b,c,d", "0.5,0,0.6,1", "0,0,0,1"):
            with self.assertRaises(ValueError, msg=bad):
                CameraSettings.from_config({"roi": bad})

    def test_orient_args(self):
        s = CameraSettings.from_config({"rotation": 180, "hflip": True})
        self.assertEqual(s.orient_args(), ["--rotation", "180", "--hflip"])

    def test_disabled_is_unavailable(self):
        ok, why = camera.availability(CameraSettings.from_config({"enabled": "no"}))
        self.assertFalse(ok)
        self.assertIn("enabled", why)


class LoadSettingsTest(unittest.TestCase):
    def test_bad_block_disables_the_camera_instead_of_raising(self):
        with self.assertLogs("polykybd-ctnd", level="WARNING"):
            s, err = camera._load_settings({"rotation": 90})
        self.assertEqual(s.enabled, "no")
        self.assertIn("rotation", err)

    def test_non_mapping_block_disables_the_camera(self):
        for bad in ("disabled", [1, 2], 5, [], False):
            with self.assertLogs("polykybd-ctnd", level="WARNING"):
                s, err = camera._load_settings(bad)
            self.assertEqual(s.enabled, "no", msg=repr(bad))
            self.assertIn("mapping", err)

    def test_missing_block_uses_defaults(self):
        s, err = camera._load_settings(None)
        self.assertEqual((s.enabled, err), ("auto", ""))

    def test_good_block_has_no_error(self):
        s, err = camera._load_settings({"rotation": 180})
        self.assertEqual((s.rotation, err), (180, ""))


class ParseTest(unittest.TestCase):
    def test_parse_pts(self):
        self.assertEqual(camera.parse_pts("# timecode format v2\n0.000\n11.112\n\n22.2\n"),
                         [0.0, 11.112, 22.2])

    def test_split_yuv420_drops_partial_frame(self):
        w, h = 64, 2
        frame = bytes([7]) * (w * h) + bytes([128]) * (w * h // 2)
        planes = camera.split_yuv420(frame * 3 + b"\x00" * 10, w, h)
        self.assertEqual(len(planes), 3)
        self.assertEqual(planes[0], bytes([7]) * (w * h))


class PhaseTest(unittest.TestCase):
    def check(self, delay_ms: float, tol: float = 6.0):
        res = camera.analyse(synth(delay_ms), CameraSettings.from_config({"split_x": 0.5}))
        self.assertAlmostEqual(res.offset_ms, delay_ms, delta=tol)
        self.assertGreater(res.correlation, 0.9)
        return res

    def test_in_phase(self):
        self.check(0.0)

    def test_right_lags(self):
        self.check(300.0)

    def test_right_leads(self):
        self.check(-700.0)

    def test_no_bias_at_large_offsets(self):
        # The overlap of an 8 s recording stops covering whole periods at large
        # lags; whole-trace means once pulled the peak by -22 ms at +800 ms.
        for d in (800.0, 1000.0, -1000.0):
            self.check(d, tol=3.0)

    def test_wraps_into_half_period(self):
        # 1500 ms late is the same picture as 548 ms early.
        self.check(1500.0 - camera.FLASH_CUE_PERIOD_MS)
        res = camera.analyse(synth(1500.0), CameraSettings.from_config({}))
        self.assertAlmostEqual(res.offset_ms, 1500.0 - camera.FLASH_CUE_PERIOD_MS, delta=6.0)

    def test_master_on_right_flips_sign(self):
        s = CameraSettings.from_config({"master_on_left": False})
        res = camera.analyse(synth(300.0), s)
        self.assertAlmostEqual(res.offset_ms, -300.0, delta=6.0)

    def test_small_leds_in_a_full_size_frame(self):
        # 320x240 as on the rig, LEDs on 2 % of each half: the variance filter
        # must still find them among the static texture.
        rec = synth(437.0, width=320, height=240, seconds=6.0)
        w = rec.width
        led = {r * w + c for r in range(100, 110) for c in list(range(40, 52)) + list(range(260, 272))}
        base = rec.lumas[0]
        rec.lumas = [bytes(y[i] if i in led else base[i] for i in range(len(y)))
                     for y in rec.lumas]
        res = camera.analyse(rec, CameraSettings.from_config({}))
        self.assertAlmostEqual(res.offset_ms, 437.0, delta=6.0)

    def test_fps_reported(self):
        res = self.check(0.0)
        self.assertAlmostEqual(res.fps, 58.0, delta=1.0)


class PeriodicityTest(unittest.TestCase):
    def test_shared_brightness_step_skips(self):
        # No cue on either half, one room-light step: identical traces with zero
        # lag and correlation 1. Without the periodicity check this read as pass.
        rec = synth(0.0, swing=0.0)
        w, h = rec.width, rec.height
        rec.lumas = [bytes(min(255, v + (40 if t > 3500 else 0)) for v in y)
                     for t, y in zip(rec.timestamps_ms, rec.lumas)]
        res = camera.analyse(rec, CameraSettings.from_config({}))
        self.assertLess(min(res.periodicity_left, res.periodicity_right),
                        camera.MIN_PERIODICITY)
        self.assertEqual(camera.judge(res)["status"], "skip")

    def test_cue_is_periodic(self):
        res = camera.analyse(synth(300.0), CameraSettings.from_config({}))
        self.assertGreater(min(res.periodicity_left, res.periodicity_right), 0.9)
        self.assertEqual(camera.judge(res)["status"], "xfail")

    def test_short_trace_is_not_periodic(self):
        self.assertEqual(camera.periodicity([1.0, 2.0, 3.0], 512), 0.0)


class SnapshotTest(unittest.TestCase):
    def test_disabled_camera_takes_no_snapshot(self):
        with self.assertRaises(RuntimeError) as ctx:
            camera.snapshot_jpeg(CameraSettings.from_config({"enabled": "no"}))
        self.assertIn("enabled", str(ctx.exception))


class JudgeTest(unittest.TestCase):
    def res(self, **kw):
        base = dict(offset_ms=0.0, correlation=0.98, swing_left=20.0,
                    swing_right=20.0, frames=464, fps=58.0)
        base.update(kw)
        return camera.PhaseResult(**base)

    def test_pass_within_tolerance(self):
        self.assertEqual(camera.judge(self.res(offset_ms=-30.0))["status"], "pass")

    def test_offset_is_xfail_not_fail(self):
        r = camera.judge(self.res(offset_ms=640.0))
        self.assertEqual(r["status"], "xfail")
        self.assertIn("timer_read32", r["reason"])

    def test_no_breathing_skips(self):
        self.assertEqual(camera.judge(self.res(swing_right=0.5))["status"], "skip")

    def test_not_periodic_skips(self):
        r = camera.judge(self.res(periodicity_right=0.2))
        self.assertEqual(r["status"], "skip")
        self.assertIn("2048 ms", r["reason"])

    def test_poor_match_skips(self):
        self.assertEqual(camera.judge(self.res(correlation=0.2))["status"], "skip")


class LastResultTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "var" / "last.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_round_trip_with_downsampled_traces(self):
        rec = synth(300.0)
        res, master, slave = camera.analyse_traces(rec, CameraSettings.from_config({}))
        record = camera.judge(res)
        camera.save_last(record, "ci", rec.timestamps_ms, master, slave, path=self.path)
        d = camera.load_last(self.path)
        self.assertEqual((d["source"], d["status"]), ("ci", record["status"]))
        self.assertIn("time", d)
        tr = d["traces"]
        self.assertEqual(len(tr["t_ms"]), camera.CHART_POINTS)
        self.assertEqual(len(tr["master"]), len(tr["slave"]))
        self.assertEqual(tr["t_ms"][0], 0.0)

    def test_skip_without_traces(self):
        camera.save_last({"name": "x", "status": "skip", "reason": "r"}, "ui", path=self.path)
        d = camera.load_last(self.path)
        self.assertEqual(d["status"], "skip")
        self.assertNotIn("traces", d)

    def test_missing_or_broken_file_is_none(self):
        self.assertIsNone(camera.load_last(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        self.assertIsNone(camera.load_last(self.path))

    def test_no_temp_files_left(self):
        for i in range(3):
            camera.save_last({"status": "pass", "n": i}, "ci", path=self.path)
        self.assertEqual(camera.load_last(self.path)["n"], 2)
        self.assertEqual([p.name for p in self.path.parent.iterdir() if p.suffix == ".tmp"], [])

    def test_history_keeps_the_last_ten_newest_first_without_traces(self):
        import os
        rec = synth(300.0)
        res, master, slave = camera.analyse_traces(rec, CameraSettings.from_config({}))
        old = {k: os.environ.get(k) for k in ("GITHUB_SHA", "GITHUB_HEAD_REF", "GITHUB_REF_NAME")}
        os.environ.update(GITHUB_SHA="0123456789abcdef", GITHUB_HEAD_REF="fix/cue")
        try:
            for i in range(12):
                camera.save_last({**camera.judge(res), "n": i}, "ci", rec.timestamps_ms,
                                 master, slave, path=self.path, fw=f"1.9.{i}")
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        hist = camera.load_history(self.path.with_name(camera.HISTORY_PATH.name))
        self.assertEqual(len(hist), camera.HISTORY_LEN)
        self.assertEqual([h["fw"] for h in hist[:2]], ["1.9.11", "1.9.10"])   # newest first
        self.assertEqual((hist[0]["commit"], hist[0]["ref"], hist[0]["source"]),
                         ("0123456", "fix/cue", "ci"))
        self.assertNotIn("traces", hist[0])
        self.assertIn("traces", camera.load_last(self.path))   # the latest keeps its traces

    def test_ui_measurement_has_no_commit(self):
        camera.save_last({"status": "pass", "offset_ms": 3.0}, "ui", path=self.path, fw="1.9.4")
        h = camera.load_history(self.path.with_name(camera.HISTORY_PATH.name))[0]
        self.assertEqual((h["source"], h["fw"]), ("ui", "1.9.4"))
        self.assertNotIn("commit", h)

    def test_write_failure_does_not_raise(self):
        # A path under a file cannot be created: save_last logs and returns.
        blocker = self.path.parent
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("a file, not a directory")
        with self.assertLogs("polykybd-ctnd", level="WARNING"):
            camera.save_last({"status": "pass"}, "ci", path=self.path)


class MeasureNowTest(unittest.TestCase):
    def test_no_camera_skips_without_staging(self):
        import tempfile
        from pathlib import Path
        staged = []
        import station.fw_update as fw
        orig, orig_path = fw.stage_and_verify, camera.LAST_RESULT_PATH
        fw.stage_and_verify = lambda *a, **k: staged.append(1) or True
        with tempfile.TemporaryDirectory() as tmp:
            camera.LAST_RESULT_PATH = Path(tmp) / "last.json"
            try:
                out = camera.measure_now("x.bin", lambda m: None,
                                         CameraSettings.from_config({"enabled": "no"}))
                self.assertEqual(camera.load_last()["status"], "skip")
            finally:
                fw.stage_and_verify, camera.LAST_RESULT_PATH = orig, orig_path
        self.assertEqual(out["status"], "skip")
        self.assertEqual(out["source"], "ui")
        self.assertIn("time", out)
        self.assertEqual(staged, [])

    def test_measures_while_staging(self):
        import station.fw_update as fw
        calls = {}
        orig_stage, orig_av, orig_rec = fw.stage_and_verify, camera.availability, camera.record
        orig_path = camera.LAST_RESULT_PATH

        def fake_stage(path, log, on_begin=None, **k):
            calls["path"] = path
            on_begin()   # BEGIN succeeded: the recording starts here
            return True

        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            fw.stage_and_verify = fake_stage
            camera.availability = lambda s=None: (True, "fake camera")
            camera.record = lambda s=None, seconds=None: synth(640.0)
            camera.LAST_RESULT_PATH = Path(tmp) / "last.json"
            try:
                out = camera.measure_now("fw.bin", lambda m: None, CameraSettings.from_config({}))
                saved = camera.load_last()
            finally:
                fw.stage_and_verify, camera.availability, camera.record = orig_stage, orig_av, orig_rec
                camera.LAST_RESULT_PATH = orig_path
        self.assertEqual(calls["path"], "fw.bin")
        self.assertEqual(out["status"], "xfail")
        self.assertAlmostEqual(out["offset_ms"], 640.0, delta=9.0)
        self.assertEqual(saved["source"], "ui")
        self.assertIn("traces", saved)
        self.assertEqual(out["traces"], saved["traces"])   # returned = what was saved


class MeasureNowSettingsCopyTest(unittest.TestCase):
    def test_measurement_uses_its_own_copy_of_the_settings(self):
        import tempfile
        from pathlib import Path
        import station.fw_update as fw
        seen = {}
        orig = (fw.stage_and_verify, camera.availability, camera.BackgroundRecording,
                camera.LAST_RESULT_PATH)

        class FakeRec:
            def __init__(self, settings, seconds=None):
                seen["settings"] = settings
            def start(self): pass
            def result(self, timeout=60.0): return synth(0.0)

        def fake_stage(path, log, on_begin=None, **k):
            on_begin()
            camera.SETTINGS.split_x = 0.9   # a view saved during the measurement
            return True

        with tempfile.TemporaryDirectory() as tmp:
            fw.stage_and_verify, camera.BackgroundRecording = fake_stage, FakeRec
            camera.availability = lambda s=None: (True, "fake camera")
            camera.LAST_RESULT_PATH = Path(tmp) / "last.json"
            old_split = camera.SETTINGS.split_x
            # A fixed split, independent of any view saved on this machine:
            # synth() puts the LEDs at 1/8..3/8 and 5/8..7/8 of the width.
            camera.SETTINGS.split_x = 0.5
            try:
                out = camera.measure_now("fw.bin", lambda m: None)
            finally:
                (fw.stage_and_verify, camera.availability, camera.BackgroundRecording,
                 camera.LAST_RESULT_PATH) = orig
                camera.SETTINGS.split_x = old_split
        self.assertIsNot(seen["settings"], camera.SETTINGS)
        self.assertEqual(seen["settings"].split_x, 0.5)
        self.assertEqual(out["status"], "pass")   # split at the old 0.5: both halves in phase


class MeasureNowStagingFailedTest(unittest.TestCase):
    def test_failed_staging_is_not_a_pass(self):
        import tempfile
        from pathlib import Path
        import station.fw_update as fw
        orig = (fw.stage_and_verify, camera.availability, camera.record, camera.LAST_RESULT_PATH)

        def fake_stage(path, log, on_begin=None, **k):
            on_begin()
            return False   # e.g. COMMIT refused after the recording started

        with tempfile.TemporaryDirectory() as tmp:
            fw.stage_and_verify = fake_stage
            camera.availability = lambda s=None: (True, "fake camera")
            camera.record = lambda s=None, seconds=None: synth(0.0)
            camera.LAST_RESULT_PATH = Path(tmp) / "last.json"
            try:
                out = camera.measure_now("fw.bin", lambda m: None, CameraSettings.from_config({}))
            finally:
                fw.stage_and_verify, camera.availability, camera.record, camera.LAST_RESULT_PATH = orig
        self.assertEqual(out["status"], "skip")
        self.assertIn("HID staging failed", out["reason"])
        self.assertIn("traces", out)

    def test_staging_failed_before_begin_names_the_failure(self):
        import tempfile
        from pathlib import Path
        import station.fw_update as fw
        orig = (fw.stage_and_verify, camera.availability, camera.LAST_RESULT_PATH)

        def fake_stage(path, log, on_begin=None, **k):
            log("  FAIL: Raw HID interface not found")
            return False   # BEGIN never sent, so on_begin never ran

        with tempfile.TemporaryDirectory() as tmp:
            fw.stage_and_verify = fake_stage
            camera.availability = lambda s=None: (True, "fake camera")
            camera.LAST_RESULT_PATH = Path(tmp) / "last.json"
            try:
                out = camera.measure_now("fw.bin", lambda m: None, CameraSettings.from_config({}))
            finally:
                fw.stage_and_verify, camera.availability, camera.LAST_RESULT_PATH = orig
        self.assertEqual(out["status"], "skip")
        self.assertIn("before the flash cue: Raw HID interface not found", out["reason"])
        self.assertNotIn("camera recording failed", out["reason"])

    def test_prepare_runs_before_staging_and_only_with_a_camera(self):
        import tempfile
        from pathlib import Path
        import station.fw_update as fw
        orig = (fw.stage_and_verify, camera.availability, camera.LAST_RESULT_PATH)
        order = []

        def fake_stage(path, log, on_begin=None, **k):
            order.append("stage")
            return False

        with tempfile.TemporaryDirectory() as tmp:
            fw.stage_and_verify = fake_stage
            camera.LAST_RESULT_PATH = Path(tmp) / "last.json"
            try:
                camera.availability = lambda s=None: (False, "no camera")
                camera.measure_now("fw.bin", lambda m: None, CameraSettings.from_config({}),
                                   prepare=lambda: order.append("prepare"))
                self.assertEqual(order, [])
                camera.availability = lambda s=None: (True, "fake camera")
                camera.measure_now("fw.bin", lambda m: None, CameraSettings.from_config({}),
                                   prepare=lambda: order.append("prepare"))
            finally:
                fw.stage_and_verify, camera.availability, camera.LAST_RESULT_PATH = orig
        self.assertEqual(order, ["prepare", "stage"])


class ViewOverrideTest(unittest.TestCase):
    """The crop and split line set in the UI's "Adjust view"."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "var" / "camera_view.json"
        self._orig = (camera.SETTINGS.roi, camera.SETTINGS.split_x)

    def tearDown(self):
        camera.SETTINGS.roi, camera.SETTINGS.split_x = self._orig
        self._tmp.cleanup()

    def test_override_wins_over_config_for_roi_and_split_only(self):
        s, err = camera._load_settings({"roi": "0,0,1,1", "split_x": 0.5, "rotation": 180},
                                       {"roi": "0.1,0.2,0.8,0.5", "split_x": 0.45})
        self.assertEqual((s.roi, s.split_x, s.rotation, err), ("0.1,0.2,0.8,0.5", 0.45, 180, ""))

    def test_invalid_override_is_ignored_not_fatal(self):
        with self.assertLogs("polykybd-ctnd", level="WARNING"):
            s, err = camera._load_settings({"split_x": 0.5}, {"roi": "0.9,0,0.5,1"})
        self.assertEqual((s.enabled, s.roi, s.split_x, err), ("auto", "", 0.5, ""))

    def test_load_keeps_only_view_keys(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"roi": "0,0,0.5,0.5", "split_x": 0.4, "enabled": "no"}')
        self.assertEqual(camera.load_view_override(self.path), {"roi": "0,0,0.5,0.5", "split_x": 0.4})
        self.path.write_text("not json")
        self.assertEqual(camera.load_view_override(self.path), {})

    def test_save_applies_in_place_and_clear_restores(self):
        settings_obj = camera.SETTINGS
        self.assertEqual(camera.save_view_override("0.1,0.3,0.8,0.4", 0.55, self.path), "")
        self.assertIs(camera.SETTINGS, settings_obj)   # default arguments keep working
        self.assertEqual((camera.SETTINGS.roi, camera.SETTINGS.split_x), ("0.1,0.3,0.8,0.4", 0.55))
        self.assertEqual(camera.load_view_override(self.path)["split_x"], 0.55)
        self.assertEqual(camera.clear_view_override(self.path), "")
        self.assertFalse(self.path.exists())
        self.assertEqual(camera.clear_view_override(self.path), "")   # already gone: fine

    def test_save_rejects_bad_values_and_writes_nothing(self):
        self.assertIn("roi", camera.save_view_override("0.5,0,0.8,1", 0.5, self.path))
        self.assertIn("split_x", camera.save_view_override("", 0.99, self.path))
        self.assertFalse(self.path.exists())

    def test_concurrent_save_and_clear_leave_file_and_settings_consistent(self):
        import threading
        errors = []

        def worker(i):
            for _ in range(20):
                if i % 2:
                    errors.append(camera.save_view_override("0.1,0.3,0.8,0.4", 0.55, self.path))
                else:
                    errors.append(camera.clear_view_override(self.path))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(set(errors), {""})
        on_disk = camera.load_view_override(self.path)
        if on_disk:
            self.assertEqual((camera.SETTINGS.roi, camera.SETTINGS.split_x),
                             (on_disk["roi"], on_disk["split_x"]))
        else:
            base, _ = camera._load_settings(camera.CAMERA)
            self.assertEqual((camera.SETTINGS.roi, camera.SETTINGS.split_x), (base.roi, base.split_x))

    def test_snapshot_waits_for_a_view_update_in_progress(self):
        import threading
        got = {}
        camera._view_lock.acquire()   # a save is between the file write and SETTINGS
        t = threading.Thread(target=lambda: got.setdefault("s", camera.snapshot_settings(camera.SETTINGS)))
        try:
            t.start()
            t.join(0.2)
            self.assertTrue(t.is_alive())   # the copy waits instead of reading half an update
            camera.SETTINGS.roi, camera.SETTINGS.split_x = "0.1,0.3,0.8,0.4", 0.55
        finally:
            # Release on every path, so a regression cannot leave the lock held
            # for the tests that run after this one.
            camera._view_lock.release()
            t.join(2)
        self.assertEqual((got["s"].roi, got["s"].split_x), ("0.1,0.3,0.8,0.4", 0.55))
        self.assertIsNot(got["s"], camera.SETTINGS)

    def test_full_view_snapshot_drops_only_the_roi(self):
        s = CameraSettings.from_config({"roi": "0.1,0.25,0.8,0.5", "hflip": True})
        self.assertEqual(s.orient_args(crop=False), ["--hflip"])
        self.assertIn("--roi", s.orient_args())


class RunnerHookTest(unittest.TestCase):
    def setUp(self):
        # rgb_phase_check() saves its result; keep it out of the checkout's var/.
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = camera.LAST_RESULT_PATH
        camera.LAST_RESULT_PATH = Path(self._tmp.name) / "last.json"

    def tearDown(self):
        camera.LAST_RESULT_PATH = self._orig_path
        self._tmp.cleanup()

    def test_no_camera_skips_with_reason(self):
        from station.test_runner import TestRunner
        logs = []
        runner = TestRunner.__new__(TestRunner)   # no hardware set-up
        runner.log = logs.append
        rec = runner.rgb_phase_check(None, "no camera detected")
        self.assertEqual(rec["status"], "skip")
        self.assertEqual(rec["reason"], "no camera detected")
        self.assertTrue(any("SKIP" in line for line in logs))
        self.assertEqual(camera.load_last()["reason"], "no camera detected")

    def test_recording_error_skips(self):
        from station.test_runner import TestRunner
        runner = TestRunner.__new__(TestRunner)
        runner.log = lambda _m: None
        never = camera.BackgroundRecording()
        rec = runner.rgb_phase_check(never, "")
        self.assertEqual(rec["status"], "skip")
        self.assertIn("never started", rec["reason"])


if __name__ == "__main__":
    unittest.main()
