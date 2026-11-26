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
        # Nine sampled LED pixels per half: allow half a frame (8.6 ms at 58 fps).
        self.assertAlmostEqual(res.offset_ms, 437.0, delta=9.0)

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


class RunnerHookTest(unittest.TestCase):
    def test_no_camera_skips_with_reason(self):
        from station.test_runner import TestRunner
        logs = []
        runner = TestRunner.__new__(TestRunner)   # no hardware set-up
        runner.log = logs.append
        rec = runner.rgb_phase_check(None, "no camera detected")
        self.assertEqual(rec["status"], "skip")
        self.assertEqual(rec["reason"], "no camera detected")
        self.assertTrue(any("SKIP" in line for line in logs))

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
