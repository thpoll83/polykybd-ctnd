# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the USB suspend/wake check (no hardware, no sysfs).

Two layers:

* :mod:`station.usb_suspend` against a fake sysfs tree whose ``runtime_status``
  follows the kernel's rule: the port suspends only with ``control=auto`` and no
  interface bound to a driver.
* :meth:`TestRunner.suspend_wake_eden` against a fake keyboard with separate RAM
  and EEPROM idle styles. Only the fake suspend copies RAM to EEPROM, and only
  the fake RUN-pin reset copies EEPROM back, so the read-back assertion is
  exercised for real: a flush that never runs must FAIL the test.
"""
import os
import sys
import tempfile
import types
import unittest

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

from station import usb_suspend                        # noqa: E402
from station.usb_suspend import UsbSuspend, bound_interfaces, find_device  # noqa: E402


class FakeSysfs:
    """A /sys/bus/usb tree with one keyboard (three HID interfaces) and a hub."""

    DEV = "1-1.3"

    def __init__(self, root: str, control: str = "on", delay: str = "2000",
                 holds_awake: bool = False):
        self.root = root
        self.holds_awake = holds_awake   # something besides usbhid keeps it awake
        self.writes = []
        devices = os.path.join(root, "devices")
        self.drivers = os.path.join(root, "drivers", "usbhid")
        os.makedirs(self.drivers)
        for name, vid, pid in (("usb1", "1d6b", "0002"), ("1-1", "2109", "3431"),
                               (self.DEV, "2021", "2007")):
            d = os.path.join(devices, name)
            os.makedirs(os.path.join(d, "power"))
            self._put(os.path.join(d, "idVendor"), vid + "\n")
            self._put(os.path.join(d, "idProduct"), pid + "\n")
        power = os.path.join(devices, self.DEV, "power")
        self._put(os.path.join(power, "control"), control + "\n")
        self._put(os.path.join(power, "autosuspend_delay_ms"), delay + "\n")
        self._put(os.path.join(power, "runtime_status"), "active\n")
        for i in range(3):
            intf = os.path.join(devices, f"{self.DEV}:1.{i}")
            os.makedirs(intf)
            os.symlink(self.drivers, os.path.join(intf, "driver"))

    @staticmethod
    def _put(path, text):
        with open(path, "w", encoding="ascii") as f:
            f.write(text)

    def _get(self, *parts):
        with open(os.path.join(self.root, *parts), encoding="ascii") as f:
            return f.read().strip()

    def control(self):
        return self._get("devices", self.DEV, "power", "control")

    def delay(self):
        return self._get("devices", self.DEV, "power", "autosuspend_delay_ms")

    def write(self, path, value):
        """The sudo_write stand-in: apply the write the way the kernel would."""
        self.writes.append((os.path.relpath(path, self.root), value))
        rel = os.path.relpath(path, self.root)
        intf = os.path.join(self.root, "devices", value, "driver")
        if rel == "drivers/usbhid/unbind":
            os.unlink(intf)
        elif rel == "drivers/usbhid/bind":
            os.symlink(self.drivers, intf)
        else:
            self._put(path, value + "\n")
        self._update_status()

    def _update_status(self):
        bound = bound_interfaces(self.DEV, root=self.root)
        asleep = self.control() == "auto" and not bound and not self.holds_awake
        self._put(os.path.join(self.root, "devices", self.DEV, "power",
                               "runtime_status"),
                  ("suspended" if asleep else "active") + "\n")


class UsbSuspendTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

    def _clock(self):
        t = {"now": 0.0}

        def sleep(s):
            t["now"] += s
        return sleep, (lambda: t["now"])

    def test_find_device_matches_vid_pid_and_skips_interfaces(self):
        FakeSysfs(self.root)
        self.assertEqual(find_device(0x2021, 0x2007, root=self.root), "1-1.3")
        self.assertIsNone(find_device(0x2021, 0x9999, root=self.root))

    def test_two_masters_is_an_error_not_a_pick(self):
        fs = FakeSysfs(self.root)
        d = os.path.join(self.root, "devices", "1-1.4")
        os.makedirs(d)
        fs._put(os.path.join(d, "idVendor"), "2021\n")
        fs._put(os.path.join(d, "idProduct"), "2007\n")
        with self.assertRaises(RuntimeError):
            find_device(0x2021, 0x2007, root=self.root)

    def test_suspend_reads_suspended_and_resume_restores_everything(self):
        fs = FakeSysfs(self.root, control="on", delay="2000")
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, write=fs.write, sleep=sleep, clock=clock)
        s.suspend()
        self.assertEqual(usb_suspend.runtime_status(fs.DEV, self.root), "suspended")
        self.assertEqual(bound_interfaces(fs.DEV, root=self.root), [])
        s.resume()
        self.assertEqual(usb_suspend.runtime_status(fs.DEV, self.root), "active")
        self.assertEqual(len(bound_interfaces(fs.DEV, root=self.root)), 3)
        self.assertEqual((fs.control(), fs.delay()), ("on", "2000"))

    def test_resume_rebinds_before_restoring_control_auto(self):
        # Restoring control=auto before the bind would let the port suspend again
        # with nothing bound.
        fs = FakeSysfs(self.root, control="auto", delay="2000")
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, write=fs.write, sleep=sleep, clock=clock)
        s.suspend()
        fs.writes.clear()
        s.resume()
        kinds = [w[0].rsplit("/", 1)[-1] for w in fs.writes]
        self.assertEqual(kinds[0], "control")
        self.assertEqual(fs.writes[0][1], "on")
        self.assertLess(max(i for i, k in enumerate(kinds) if k == "bind"),
                        kinds.index("autosuspend_delay_ms"))
        self.assertEqual(fs.writes[-1], (f"devices/{fs.DEV}/power/control", "auto"))
        self.assertEqual(usb_suspend.runtime_status(fs.DEV, self.root), "active")

    def test_a_device_held_awake_raises_and_resume_still_cleans_up(self):
        fs = FakeSysfs(self.root, holds_awake=True)
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, write=fs.write, sleep=sleep, clock=clock)
        with self.assertRaises(RuntimeError):
            s.suspend(timeout_s=2.0)
        s.resume()
        self.assertEqual(len(bound_interfaces(fs.DEV, root=self.root)), 3)
        self.assertEqual(fs.control(), "on")

    def test_resume_twice_writes_nothing_the_second_time(self):
        fs = FakeSysfs(self.root)
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, write=fs.write, sleep=sleep, clock=clock)
        s.suspend()
        s.resume()
        n = len(fs.writes)
        s.resume()
        self.assertEqual(len(fs.writes), n)


# --- the runner check -----------------------------------------------------------

from station import test_runner as tr      # noqa: E402
from station.hil_tests import (            # noqa: E402
    ACK, CMD_GET_ID, CMD_IDLE_STATE, IDLE_STYLE_EDEN, POLY_CHANNEL,
)

CMD_IDLE_STYLE = tr.CMD_IDLE_STYLE
CMD_SAVE_EEPROM = tr.CMD_SAVE_EEPROM


class FakeKeyboard:
    """RAM vs EEPROM idle style; suspend flushes, RUN reset reloads."""

    def __init__(self, style=0, flush_works=True):
        self.ram = style
        self.eeprom = style
        self.flush_works = flush_works
        self.idle_starts = 0

    def send(self, data, **_kw):
        cmd = data[1]
        if cmd == CMD_IDLE_STYLE:
            if data[2] != 0xFF:
                self.ram = data[2]
            return bytes([POLY_CHANNEL, cmd, ACK, self.ram])
        if cmd == CMD_SAVE_EEPROM:
            self.eeprom = self.ram
            return bytes([POLY_CHANNEL, cmd, ACK])
        if cmd == CMD_IDLE_STATE:
            if data[2]:
                self.idle_starts += 1
            return bytes([POLY_CHANNEL, cmd, ACK])
        if cmd == CMD_GET_ID:
            return bytes([POLY_CHANNEL, cmd, ACK]) + b"fw 1.9.1"
        return None

    def suspend_flush(self):
        if self.flush_works:
            self.eeprom = self.ram

    def power_cycle(self):
        self.ram = self.eeprom


class SuspendWakeEdenTest(unittest.TestCase):

    def setUp(self):
        self._saved = {n: getattr(tr, n) for n in
                       ("time", "TAP", "find_device", "UsbSuspend",
                        "_crash_record_body", "_wait_for_reboot")}
        self.addCleanup(lambda: [setattr(tr, n, v) for n, v in self._saved.items()])
        tr.time = types.SimpleNamespace(sleep=lambda _s: None, monotonic=lambda: 0.0)
        self.idle_line = "Transition to idle [style=eden] - eden screensaver"
        self.stall_lines = []
        tr.TAP = types.SimpleNamespace(
            mark=lambda: 0,
            wait_for=lambda *a, **k: self.idle_line,
            find_all=lambda *a, **k: list(self.stall_lines),
            flush=lambda: None)
        tr.find_device = lambda *_a: "1-1.3"
        self.wake_state = "back"
        tr._wait_for_reboot = lambda *_a: self.wake_state
        self.crash_after = None
        self.crash_reads = 0

        def crash_body(_raw, _which):
            self.crash_reads += 1
            return self.crash_after if self.crash_reads > 2 else None
        tr._crash_record_body = crash_body

    def _run(self, kb, fw="1.9.1"):
        seen = {"suspends": 0, "resumed": 0}

        class FakeSuspend:
            def __init__(self, _dev):
                pass

            def suspend(self):
                seen["suspends"] += 1
                kb.suspend_flush()
                return 0.2

            def resume(self):
                seen["resumed"] += 1

        tr.UsbSuspend = FakeSuspend
        runner = tr.TestRunner(log=lambda _m: None)
        runner._raw = kb
        runner._flash = types.SimpleNamespace(reset=lambda *_a: kb.power_cycle())
        runner.wait_for_master_ready = lambda *a, **k: True
        runner.settle_master = lambda *a, **k: True
        runner._device_caps = lambda: {"fw": fw, "protocol": 23}
        return runner.suspend_wake_eden(), seen

    def test_a_clean_run_passes_and_restores_the_style(self):
        kb = FakeKeyboard(style=0)
        result, seen = self._run(kb)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual(seen["suspends"], tr.SUSPEND_ROUNDS)
        self.assertEqual(kb.idle_starts, tr.SUSPEND_ROUNDS)
        self.assertEqual((kb.ram, kb.eeprom), (0, 0))

    def test_a_flush_that_never_runs_FAILS(self):
        kb = FakeKeyboard(style=0, flush_works=False)
        result, _seen = self._run(kb)
        self.assertEqual(result["status"], "fail")
        self.assertIn("suspend flush", result["error"])

    def test_original_eden_is_parked_first_so_the_read_back_still_means_something(self):
        kb = FakeKeyboard(style=IDLE_STYLE_EDEN, flush_works=False)
        result, _seen = self._run(kb)
        self.assertEqual(result["status"], "fail", "Eden already in EEPROM hid the "
                         "missing flush")
        kb = FakeKeyboard(style=IDLE_STYLE_EDEN)
        result, _seen = self._run(kb)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual((kb.ram, kb.eeprom), (IDLE_STYLE_EDEN, IDLE_STYLE_EDEN))

    def test_a_reset_while_suspended_FAILS(self):
        self.wake_state = "fresh"
        result, seen = self._run(FakeKeyboard())
        self.assertEqual(result["status"], "fail")
        self.assertIn("reset while suspended", result["error"])
        self.assertGreaterEqual(seen["resumed"], 1)

    def test_a_core1_stall_line_FAILS(self):
        self.stall_lines = ["WARNING core1 stalled: no answer for 500 ms at suspend"]
        result, _seen = self._run(FakeKeyboard())
        self.assertEqual(result["status"], "fail")
        self.assertIn("core1 stall", result["error"])

    def test_a_new_crash_record_FAILS(self):
        self.crash_after = bytes([1]) + bytes(48)
        result, _seen = self._run(FakeKeyboard())
        self.assertEqual(result["status"], "fail")
        self.assertIn("NEW crash record", result["error"])

    def test_eden_that_never_engages_FAILS_rather_than_suspending_idle(self):
        self.idle_line = None
        result, seen = self._run(FakeKeyboard())
        self.assertEqual(result["status"], "fail")
        self.assertEqual(seen["suspends"], 0)

    def test_firmware_before_the_fix_SKIPS(self):
        result, seen = self._run(FakeKeyboard(), fw="1.9.0")
        self.assertEqual(result["status"], "skip")
        self.assertEqual(seen["suspends"], 0)


class SuspendWiringTest(unittest.TestCase):
    """flash_and_test runs the check only on an extended run, with the console
    LIVE at the call (it asserts on a console line)."""

    def _flash_and_test(self, extended=True, console_ok=True):
        seen = {"called": False, "console_live": None}

        class FakeConsole:
            live = False

            def start(self, _cb):
                if not console_ok:
                    raise RuntimeError("no console")
                FakeConsole.live = True

            def stop(self):
                FakeConsole.live = False

        runner = tr.TestRunner(log=lambda _m: None)
        runner._console = FakeConsole()
        runner._flash = types.SimpleNamespace(flash=lambda *a, **k: None,
                                              cleanup=lambda: None)
        runner.wait_for_master_ready = lambda *a, **k: True
        runner.settle_master = lambda *a, **k: True
        runner._reset_keymap = lambda: None
        runner._turn_off_displays = lambda: None
        runner.reboot_persistence = lambda: {"name": "rp", "status": "pass"}

        def fake_check():
            seen["called"] = True
            seen["console_live"] = FakeConsole.live
            return {"name": tr.SUSPEND_TEST_NAME, "status": "pass"}
        runner.suspend_wake_eden = fake_check
        saved = tr.time, tr.TAP
        tr.time = types.SimpleNamespace(sleep=lambda _s: None, monotonic=lambda: 0.0)
        tr.TAP = types.SimpleNamespace(flush=lambda: None)
        try:
            out = runner.flash_and_test("left.uf2", "right.uf2",
                                        tests=[{"name": "noop", "fn": lambda r, l: True}],
                                        extended=extended)
        finally:
            tr.time, tr.TAP = saved
        names = {r["name"]: r["status"] for r in out["results"]}
        return seen, names

    def test_extended_run_calls_it_with_the_console_live(self):
        seen, names = self._flash_and_test()
        self.assertTrue(seen["called"])
        self.assertTrue(seen["console_live"])
        self.assertEqual(names[tr.SUSPEND_TEST_NAME], "pass")

    def test_default_run_records_a_skip(self):
        seen, names = self._flash_and_test(extended=False)
        self.assertFalse(seen["called"])
        self.assertEqual(names[tr.SUSPEND_TEST_NAME], "skip")

    def test_no_console_records_a_skip(self):
        seen, names = self._flash_and_test(console_ok=False)
        self.assertFalse(seen["called"])
        self.assertEqual(names[tr.SUSPEND_TEST_NAME], "skip")


class VerdictTest(unittest.TestCase):

    def test_states(self):
        self.assertIsNone(tr.suspend_round_verdict("back", []))
        self.assertIsNone(tr.suspend_round_verdict("not-rebooted", []))
        self.assertIn("reset while suspended", tr.suspend_round_verdict("fresh", []))
        self.assertIn("did not answer", tr.suspend_round_verdict("no-reply", []))
        self.assertIn("core1 stall", tr.suspend_round_verdict("back", ["x"]))


if __name__ == "__main__":
    unittest.main()
