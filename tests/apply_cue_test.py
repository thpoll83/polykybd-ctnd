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

    def test_no_frame_holds_the_pause_until_the_recorder_ends(self):
        """A late recorder may still launch rpicam-vid, so the status watch stays
        paused until the recorder thread has ended; then no report is written."""
        from station import test_runner
        order = []

        class Pause:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                order.append("released")

        class Rec:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def wait_ready(self, timeout):
                return False

            def cancel(self):
                order.append("cancelled")

            def result(self, timeout):
                order.append("recorder ended")   # a late recording ran to its end
                raise RuntimeError("late")

            def alive(self):
                return False

        saved = (test_runner.camera, test_runner.status_watch, apply_cue.READY_S)
        test_runner.camera = types.SimpleNamespace(
            skip_reason=lambda: None, snapshot_settings=lambda s: s, SETTINGS=None,
            BackgroundRecording=Rec)
        test_runner.status_watch = types.SimpleNamespace(paused=Pause)
        apply_cue.READY_S = 0.1
        logs = []
        me = types.SimpleNamespace(log=logs.append)
        me._apply_cue_release = lambda r, p: test_runner.TestRunner._apply_cue_release(me, r, p)
        try:
            cue = test_runner.TestRunner._apply_cue_start(me)
            self.assertEqual(order, ["cancelled"])   # still paused
            self.assertIsNone(cue[3])
            test_runner.TestRunner._apply_cue_report(me, cue, 0.0)
        finally:
            test_runner.camera, test_runner.status_watch, apply_cue.READY_S = saved
        self.assertEqual(order, ["cancelled", "recorder ended", "released"])
        self.assertIn("no frame", logs[0])
        self.assertEqual(len(logs), 1)   # no report lines

    def test_the_wait_outlasts_every_wait_in_record(self):
        """The report waits longer than record() can take, so a recorder thread
        has ended when the pause is released."""
        import inspect
        from station import camera
        src = inspect.getsource(camera.record)
        self.assertIn("_lock.acquire(timeout=30)", src)
        self.assertIn("_acquire_camera(30)", src)
        self.assertIn("timeout=seconds + 30", src)
        self.assertGreaterEqual(apply_cue.RECORDER_MAX_S, 30 + 30 + apply_cue.RECORD_S + 30)

    def test_a_recorder_still_running_stops_the_watch(self):
        """result() timed out with the recorder thread alive: the status watch
        takes no more samples this run, then the pause is released."""
        import threading
        from station import camera, test_runner
        order = []

        class Pause:
            def __exit__(self, *exc):
                order.append("released")

        stop = threading.Event()
        rec = camera.BackgroundRecording.__new__(camera.BackgroundRecording)
        rec._thread = threading.Thread(target=stop.wait, daemon=True)
        rec._thread.start()
        rec._err = rec._rec = None
        saved = (apply_cue.RECORDER_MAX_S, test_runner.status_watch)
        apply_cue.RECORDER_MAX_S = -9.9   # result() waits 0.1 s
        test_runner.status_watch = types.SimpleNamespace(
            stop_sampling=lambda: order.append("watch stopped"))
        logs = []
        me = types.SimpleNamespace(log=logs.append)
        me._apply_cue_release = lambda r, p: test_runner.TestRunner._apply_cue_release(me, r, p)
        try:
            for t_first in (None, 1.0):   # the timeout path and the normal path
                test_runner.TestRunner._apply_cue_report(me, (rec, None, Pause(), t_first), 0.0)
        finally:
            apply_cue.RECORDER_MAX_S, test_runner.status_watch = saved
            stop.set()
        self.assertEqual(order, ["watch stopped", "released"] * 2)
        self.assertEqual(sum("no more samples" in line for line in logs), 2)


class RecordReadyTest(unittest.TestCase):
    """camera.record(): ready belongs to this recording, cancel stops a late start."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from station import camera
        self.camera = camera
        self.dir = Path(tempfile.mkdtemp())
        self.saved = (camera.LIVE_DIR, camera._acquire_camera, camera._run,
                      camera.rig_lock.release)
        camera.LIVE_DIR = self.dir
        camera._acquire_camera = lambda timeout: 1
        camera.rig_lock.release = lambda fd: None
        self.s = camera.CameraSettings(width=64, height=8)

    def tearDown(self):
        (self.camera.LIVE_DIR, self.camera._acquire_camera, self.camera._run,
         self.camera.rig_lock.release) = self.saved

    def test_cancel_before_start_runs_nothing(self):
        import threading
        ran = []
        self.camera._run = lambda argv, timeout: ran.append(argv)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(self.camera.CameraBusy):
            self.camera.record(self.s, seconds=1, keep_frames=False, cancel=cancel)
        self.assertEqual(ran, [])

    def test_ready_ignores_an_old_live_file(self):
        import threading
        frame = 64 * 8 * 3 // 2
        (self.dir / "rec.yuv").write_bytes(b"\x00" * frame)   # left by another recording
        ready = threading.Event()
        seen_at_start = []

        def run(argv, timeout):
            seen_at_start.append(ready.wait(0.1))   # the old file must not count
            raw = argv[argv.index("-o") + 1]
            with open(raw, "wb") as f:
                f.write(b"\x00" * frame)
            with open(argv[argv.index("--save-pts") + 1], "w") as f:
                f.write("# timecode format v2\n0\n")
            ready.wait(1.0)
            return types.SimpleNamespace(returncode=0, stderr=b"")
        self.camera._run = run
        rec = self.camera.record(self.s, seconds=1, keep_frames=False, ready=ready)
        self.assertEqual(seen_at_start, [False])
        self.assertTrue(ready.is_set())
        self.assertEqual(len(rec.lumas), 1)


if __name__ == "__main__":
    unittest.main()
