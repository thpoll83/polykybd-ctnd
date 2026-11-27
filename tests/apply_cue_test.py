# SPDX-License-Identifier: GPL-2.0-only
"""station/apply_cue.py: the per-half timeline of the apply's orange cue."""
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

from station import apply_cue  # noqa: E402


def _samples(left, right, step=100.0):
    return [(i * step, {"left": l, "right": r}) for i, (l, r) in enumerate(zip(left, right))]


class EventsTest(unittest.TestCase):
    def test_start_and_end(self):
        s = _samples([10, 10, 40, 40, 40, 5, 5], [10, 40, 40, 40, 40, 40, 5])
        self.assertEqual(apply_cue.events(s, "left"), (200.0, 500.0))
        self.assertEqual(apply_cue.events(s, "right"), (100.0, 600.0))

    def test_small_changes_do_not_count(self):
        s = _samples([10, 15, 12, 17], [10, 10, 10, 10])
        self.assertEqual(apply_cue.events(s, "left"), (None, None))

    def test_no_end(self):
        s = _samples([10, 40, 40], [10, 10, 10])
        self.assertEqual(apply_cue.events(s, "left"), (100.0, None))

    def test_summary(self):
        s = _samples([10, 10, 40, 40, 40, 5, 5], [10, 40, 40, 40, 40, 40, 5])
        lines = apply_cue.summary(s, 150.0)
        self.assertIn("left: LEDs changed +0.05 s, changed again +0.35 s (held 0.30 s)",
                      lines[0])
        self.assertIn("start: right minus left -100 ms", lines[2])
        self.assertIn("end: right minus left +100 ms", lines[3])

    def test_empty(self):
        self.assertEqual(apply_cue.events([], "left"), (None, None))


class CueStartTest(unittest.TestCase):
    """A failing recording setup releases the status watch and skips the recording."""

    def test_setup_failure_releases_the_pause(self):
        from station import test_runner
        released = []

        class Pause:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                released.append(True)

        def broken(*a, **k):
            raise RuntimeError("camera busy")

        saved = (test_runner.camera, test_runner.status_watch)
        test_runner.camera = types.SimpleNamespace(
            skip_reason=lambda: None, snapshot_settings=lambda s: s, SETTINGS=None,
            BackgroundRecording=broken)
        test_runner.status_watch = types.SimpleNamespace(paused=Pause)
        logs = []
        try:
            got = test_runner.TestRunner._apply_cue_start(types.SimpleNamespace(log=logs.append))
        finally:
            test_runner.camera, test_runner.status_watch = saved
        self.assertIsNone(got)
        self.assertEqual(released, [True])
        self.assertIn("camera busy", logs[-1])

    def test_no_frame_releases_the_pause(self):
        from station import test_runner
        released = []

        class Pause:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                released.append(True)

        class Rec:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

        saved = (test_runner.camera, test_runner.status_watch, apply_cue.READY_S)
        test_runner.camera = types.SimpleNamespace(
            skip_reason=lambda: None, snapshot_settings=lambda s: s, SETTINGS=None,
            BackgroundRecording=Rec, live_frame=lambda: None)
        test_runner.status_watch = types.SimpleNamespace(paused=Pause)
        apply_cue.READY_S = 0.1
        logs = []
        try:
            got = test_runner.TestRunner._apply_cue_start(types.SimpleNamespace(log=logs.append))
        finally:
            test_runner.camera, test_runner.status_watch, apply_cue.READY_S = saved
        self.assertIsNone(got)
        self.assertEqual(released, [True])
        self.assertIn("no frame", logs[-1])


if __name__ == "__main__":
    unittest.main()
