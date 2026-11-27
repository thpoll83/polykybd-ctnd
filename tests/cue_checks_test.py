# SPDX-License-Identifier: GPL-2.0-only
"""station/cue_checks.py: the staging cue start, the update screen and the reboot."""
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

from station import cue_checks as cc  # noqa: E402


def _led(rows, step=16.0):
    return [(i * step, {"left": l, "right": r}) for i, (l, r) in enumerate(rows)]


class StagingTest(unittest.TestCase):
    base = {"left": [0] * 10, "right": [0] * 10}
    other = {"left": [60] * 10, "right": [60] * 10}

    def test_together_and_both_screens_change(self):
        led = _led([(3, 1)] * 5 + [(150, 80)] * 5)
        rec = cc.judge_staging(cc.staging_report(led, [self.other] * 10, self.base))
        self.assertEqual(rec["status"], "pass")
        self.assertIn("+0 ms", rec["info"])

    def test_slave_far_ahead_is_xfail(self):
        rows = [(3, 1)] * 5 + [(3, 80)] * 10 + [(150, 80)] * 5   # 160 ms gap
        rec = cc.judge_staging(cc.staging_report(_led(rows), [self.other] * 20, self.base))
        self.assertEqual(rec["status"], "xfail")
        self.assertIn("-160 ms", rec["reason"])

    def test_a_screen_that_kept_its_picture_is_xfail(self):
        led = _led([(3, 1)] * 2 + [(150, 80)] * 2)
        screens = [{"left": self.other["left"], "right": self.base["right"]}] * 4
        rec = cc.judge_staging(cc.staging_report(led, screens, self.base))
        self.assertEqual(rec["status"], "xfail")
        self.assertIn("right status display kept its picture", rec["reason"])

    def test_no_cue_is_xfail(self):
        led = _led([(3, 1)] * 6)
        rec = cc.judge_staging(cc.staging_report(led, [self.other] * 6, self.base))
        self.assertEqual(rec["status"], "xfail")
        self.assertIn("did not start", rec["reason"])

    def test_screen_diff(self):
        self.assertEqual(cc.screen_diff([0, 10], [4, 4]), 5.0)
        self.assertEqual(cc.screen_diff([], [1]), 0.0)


class ResetTest(unittest.TestCase):
    def _run(self, led_after, disp_after=(5, 5)):
        led = [(t, {"left": 7, "right": 1}) for t in range(0, 1000, 50)]
        led += [(t, {"left": led_after[0], "right": led_after[1]}) for t in range(1000, 2500, 50)]
        disp = [(t, {"left": 80, "right": 80}) for t in range(0, 1300, 50)]
        disp += [(t, {"left": disp_after[0], "right": disp_after[1]}) for t in range(1300, 2500, 50)]
        return cc.judge_reset(cc.reset_report(led, disp, 1000.0))

    def test_no_change_passes(self):
        rec = self._run((7, 1))
        self.assertEqual(rec["status"], "pass")
        self.assertIn("left 300 ms", rec["info"])

    def test_slave_orange_is_xfail(self):
        rec = self._run((7, 25))
        self.assertEqual(rec["status"], "xfail")
        self.assertIn("right LEDs changed by 24", rec["reason"])

    def test_a_display_that_stays_lit_is_xfail(self):
        rec = self._run((7, 1), disp_after=(5, 80))
        self.assertEqual(rec["status"], "xfail")
        self.assertIn("right status display never went dark", rec["reason"])



class ResetRunnerTest(unittest.TestCase):
    """TestRunner.reset_cue_check: lights off, record, reboot, wait, judge."""

    def test_order_and_result(self):
        from station import test_runner as tr
        order = []

        class Rec:
            def __init__(self, *a, **k):
                pass

            def start(self):
                order.append("record")

            def wait_ready(self, timeout):
                return True

            def result(self, timeout):
                return types.SimpleNamespace(width=1, height=1, timestamps_ms=[0.0, 1600.0, 2000.0],
                                             lumas=[b"a", b"b", b"c"])

            def alive(self):
                return False

        levels = iter([(7, 80), (1, 80), (7, 80), (1, 80), (7, 5), (1, 5)])
        fake_cam = types.SimpleNamespace(
            skip_reason=lambda: None, snapshot_settings=lambda s: s, SETTINGS=None,
            load_board=lambda sd: {}, BackgroundRecording=Rec,
            half_levels=lambda *a: next(levels))

        class Raw:
            def send(self, data, attempts=3):
                order.append("lights off" if data[0] == 0x07 else f"cmd {data[1]:#x}")
                return b"ok"

        clock = {"t": 0.0}
        saved = (tr.camera, tr.time)
        tr.camera = fake_cam
        tr.time = types.SimpleNamespace(
            sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
            monotonic=lambda: clock["t"])
        runner = tr.TestRunner(log=lambda _m: None)
        runner._raw = Raw()
        runner._device_caps = lambda: {"protocol": 39}
        runner.wait_for_master_ready = lambda *a, **k: order.append("wait") or True
        try:
            rec = runner.reset_cue_check()
        finally:
            tr.camera, tr.time = saved
        self.assertEqual(order, ["lights off", "record", "cmd 0x2b", "wait"])
        self.assertEqual(rec["status"], "pass", rec)
        self.assertIn("display dark after left 500 ms", rec["info"])

    def test_skips_below_protocol_39(self):
        import types
        from station import test_runner as tr
        saved = tr.camera
        tr.camera = types.SimpleNamespace(skip_reason=lambda: None)
        runner = tr.TestRunner(log=lambda _m: None)
        runner._device_caps = lambda: {"protocol": 38}
        try:
            rec = runner.reset_cue_check()
        finally:
            tr.camera = saved
        self.assertEqual(rec["status"], "skip")
        self.assertIn("protocol 39", rec["reason"])

    def test_staging_start_waits_for_the_first_frame(self):
        import types
        from station import test_runner as tr
        order = []
        rec = types.SimpleNamespace(start=lambda: order.append("start"),
                                    wait_ready=lambda t: order.append("ready") or True)
        tr.TestRunner._start_and_wait(types.SimpleNamespace(log=order.append), rec)
        self.assertEqual(order, ["start", "ready"])

    def test_a_failed_reboot_recording_stops_the_watch_inside_the_pause(self):
        import types
        from station import test_runner as tr
        order = []

        class Rec:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def wait_ready(self, timeout):
                return False

            def cancel(self):
                order.append("cancel")

            def result(self, timeout):
                order.append("waited")
                raise RuntimeError("late")

            def alive(self):
                return True

        class Pause:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                order.append("pause released")

        saved = (tr.camera, tr.status_watch, tr.time)
        tr.camera = types.SimpleNamespace(
            skip_reason=lambda: None, snapshot_settings=lambda s: s, SETTINGS=None,
            load_board=lambda sd: {}, BackgroundRecording=Rec)
        tr.status_watch = types.SimpleNamespace(paused=Pause,
                                                stop_sampling=lambda: order.append("watch stopped"))
        tr.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=lambda: 0.0)
        runner = tr.TestRunner(log=lambda _m: None)
        runner._raw = types.SimpleNamespace(send=lambda *a, **k: b"ok")
        runner._device_caps = lambda: {"protocol": 39}
        runner.wait_for_master_ready = lambda *a, **k: True
        try:
            rec = runner.reset_cue_check()
        finally:
            tr.camera, tr.status_watch, tr.time = saved
        self.assertEqual(rec["status"], "skip")
        self.assertEqual(order, ["cancel", "waited", "watch stopped", "pause released"])

    def test_a_failed_reboot_send_stops_the_watch_inside_the_pause(self):
        import types
        from station import test_runner as tr
        order = []

        class Rec:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def wait_ready(self, timeout):
                return True

            def result(self, timeout):
                order.append("waited")
                raise RuntimeError("still running")

            def alive(self):
                return True

        class Pause:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                order.append("pause released")

        def send(data, attempts=3):
            if data[:2] == bytes([0x50, 0x2B]):
                raise OSError("HID gone")
            return b"ok"

        saved = (tr.camera, tr.status_watch, tr.time)
        tr.camera = types.SimpleNamespace(
            skip_reason=lambda: None, snapshot_settings=lambda s: s, SETTINGS=None,
            load_board=lambda sd: {}, BackgroundRecording=Rec)
        tr.status_watch = types.SimpleNamespace(paused=Pause,
                                                stop_sampling=lambda: order.append("watch stopped"))
        tr.time = types.SimpleNamespace(sleep=lambda s: None, monotonic=lambda: 0.0)
        runner = tr.TestRunner(log=lambda _m: None)
        runner._raw = types.SimpleNamespace(send=send)
        runner._device_caps = lambda: {"protocol": 39}
        runner.wait_for_master_ready = lambda *a, **k: True
        try:
            rec = runner.reset_cue_check()
        finally:
            tr.camera, tr.status_watch, tr.time = saved
        self.assertEqual(rec["status"], "skip")
        self.assertIn("HID gone", rec["reason"])
        self.assertEqual(order, ["waited", "watch stopped", "pause released"])


if __name__ == "__main__":
    unittest.main()
