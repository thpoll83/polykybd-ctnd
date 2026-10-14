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
import subprocess
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

    def ctl(self, op, target, value=None):
        """The sudo_ctl stand-in: apply the wrapper operation the way the kernel
        would. ``fail_unbind`` makes the unbind of that interface fail."""
        self.writes.append((op, target, value))
        if op == "unbind" and target == getattr(self, "fail_unbind", None):
            raise RuntimeError(f"unbind {target} failed")
        devices = os.path.join(self.root, "devices")
        if op in ("unbind", "bind"):
            link = os.path.join(devices, target, "driver")
            if op == "unbind":
                os.unlink(link)
            else:
                if os.path.islink(link):
                    raise RuntimeError(f"bind {target}: EBUSY")
                os.symlink(self.drivers, link)
        else:
            attr = {"control": "control", "delay": "autosuspend_delay_ms"}[op]
            self._put(os.path.join(devices, target, "power", attr), value + "\n")
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
        s = UsbSuspend(fs.DEV, root=self.root, ctl=fs.ctl, sleep=sleep, clock=clock)
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
        s = UsbSuspend(fs.DEV, root=self.root, ctl=fs.ctl, sleep=sleep, clock=clock)
        s.suspend()
        fs.writes.clear()
        s.resume()
        ops = [w[0] for w in fs.writes]
        self.assertEqual(fs.writes[0], ("control", fs.DEV, "on"))
        self.assertLess(max(i for i, op in enumerate(ops) if op == "bind"),
                        ops.index("delay"))
        self.assertEqual(fs.writes[-1], ("control", fs.DEV, "auto"))
        self.assertEqual(usb_suspend.runtime_status(fs.DEV, self.root), "active")

    def test_a_failed_unbind_rebinds_only_what_was_unbound(self):
        fs = FakeSysfs(self.root, control="on", delay="2000")
        fs.fail_unbind = f"{fs.DEV}:1.1"
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, ctl=fs.ctl, sleep=sleep, clock=clock)
        with self.assertRaises(RuntimeError):
            s.suspend()
        s.resume()   # would raise EBUSY on a still-bound interface
        self.assertEqual(len(bound_interfaces(fs.DEV, root=self.root)), 3)
        self.assertEqual((fs.control(), fs.delay()), ("on", "2000"))
        self.assertEqual([w[1] for w in fs.writes if w[0] == "bind"], [f"{fs.DEV}:1.0"])

    def test_a_device_held_awake_raises_and_resume_still_cleans_up(self):
        fs = FakeSysfs(self.root, holds_awake=True)
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, ctl=fs.ctl, sleep=sleep, clock=clock)
        with self.assertRaises(RuntimeError):
            s.suspend(timeout_s=2.0)
        s.resume()
        self.assertEqual(len(bound_interfaces(fs.DEV, root=self.root)), 3)
        self.assertEqual(fs.control(), "on")

    def test_a_failed_bind_on_wake_is_retried_by_the_next_resume(self):
        fs = FakeSysfs(self.root, control="on", delay="2000")
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, ctl=fs.ctl, sleep=sleep, clock=clock)
        s.suspend()
        real = fs.ctl
        failed = {"done": False}

        def flaky(op, target, value=None):
            if op == "bind" and target.endswith(":1.1") and not failed["done"]:
                failed["done"] = True
                raise RuntimeError("bind failed")
            return real(op, target, value)
        s._ctl = flaky
        with self.assertRaises(RuntimeError):
            s.resume()
        s.resume()
        self.assertEqual(len(bound_interfaces(fs.DEV, root=self.root)), 3)
        self.assertEqual((fs.control(), fs.delay()), ("on", "2000"))

    def test_resume_twice_writes_nothing_the_second_time(self):
        fs = FakeSysfs(self.root)
        sleep, clock = self._clock()
        s = UsbSuspend(fs.DEV, root=self.root, ctl=fs.ctl, sleep=sleep, clock=clock)
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

    def __init__(self, style=0, flush_works=True, os_value=3, os_auto=True):
        self.ram = style
        self.eeprom = style
        self.flush_works = flush_works
        self.idle_starts = 0
        self.os = [os_value, os_auto]          # RAM
        self.os_eeprom = list(self.os)
        self.flush_writes = []                  # per suspend: did a byte change?

    def send(self, data, **_kw):
        cmd = data[1]
        if cmd == CMD_IDLE_STYLE:
            if data[2] != 0xFF:
                self.ram = data[2]
            return bytes([POLY_CHANNEL, cmd, ACK, self.ram])
        if cmd == CMD_SAVE_EEPROM:
            self.eeprom = self.ram
            self.os_eeprom = list(self.os)
            return bytes([POLY_CHANNEL, cmd, ACK])
        if cmd == CMD_IDLE_STATE:
            if data[2]:
                self.idle_starts += 1
            return bytes([POLY_CHANNEL, cmd, ACK])
        if cmd == tr.CMD_SET_OS:
            if data[2] == 0xFE:
                self.os[1] = True
            elif data[2] != 0xFF:
                self.os = [data[2], False]
            return bytes([POLY_CHANNEL, cmd, ACK, self.os[0], int(self.os[1])])
        if cmd == CMD_GET_ID:
            return bytes([POLY_CHANNEL, cmd, ACK]) + b"fw 1.9.1"
        return None

    def suspend_flush(self):
        if self.flush_works:
            changed = (self.eeprom, self.os_eeprom) != (self.ram, self.os)
            self.flush_writes.append(changed)
            self.eeprom = self.ram
            self.os_eeprom = list(self.os)

    def power_cycle(self):
        self.ram = self.eeprom


class SuspendWakeEdenTest(unittest.TestCase):

    def setUp(self):
        self._saved = {n: getattr(tr, n) for n in
                       ("time", "TAP", "find_device", "UsbSuspend",
                        "_crash_record_body", "_wait_for_reboot", "grant_missing")}
        self.addCleanup(lambda: [setattr(tr, n, v) for n, v in self._saved.items()])
        tr.grant_missing = lambda: None   # a provisioned rig
        tr.time = types.SimpleNamespace(sleep=lambda _s: None, monotonic=lambda: 0.0)
        self.idle_line = "Transition to idle [style=eden] - eden screensaver"
        self.stall_lines = []
        self.stall_checks = 0
        def mark():
            self.stall_checks = 0    # each round starts its own console window
            return 0
        tr.TAP = types.SimpleNamespace(
            mark=mark,
            wait_for=lambda *a, **k: self.idle_line,
            find_all=lambda *a, **k: self._stall_lines_now(),
            flush=lambda: None)
        tr.find_device = lambda *_a: "1-1.3"
        self.wake_state = "back"
        tr._wait_for_reboot = lambda *_a: self.wake_state
        self.crash_clean = bytes(49)      # flags 0: no record
        self.crash_after = self.crash_clean
        self.crash_before = self.crash_clean
        self.crash_reads = 0

        def crash_body(_raw, _which):
            self.crash_reads += 1
            return self.crash_after if self.crash_reads > 2 else self.crash_before
        tr._crash_record_body = crash_body

    def _stall_lines_now(self):
        """``stall_lines`` appear only after ``stall_after`` console checks, the
        way the second report copy arrives seconds after the wake."""
        self.stall_checks += 1
        if self.stall_checks > getattr(self, "stall_after", 0):
            return list(self.stall_lines)
        return []

    def _run(self, kb, fw="1.9.1", log=None):
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
        runner = tr.TestRunner(log=log or (lambda _m: None))
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

    def test_every_round_gives_the_suspend_flush_something_to_write(self):
        for os_value, os_auto in ((3, True), (1, False), (2, False)):
            with self.subTest(os=os_value, auto=os_auto):
                kb = FakeKeyboard(style=0, os_value=os_value, os_auto=os_auto)
                result, _seen = self._run(kb)
                self.assertEqual(result["status"], "pass", result)
                self.assertEqual(kb.flush_writes, [True] * tr.SUSPEND_ROUNDS)
                self.assertEqual((kb.os, kb.os_eeprom),
                                 ([os_value, os_auto], [os_value, os_auto]))

    def test_a_failure_after_the_park_restores_the_original_style_in_EEPROM(self):
        # Original Eden; the park writes 0; the crash read then fails before any
        # suspend. RAM reads Eden again after the cleanup's set, EEPROM must too.
        self.crash_before = None
        kb = FakeKeyboard(style=IDLE_STYLE_EDEN)
        result, _seen = self._run(kb)
        self.assertEqual(result["status"], "fail")
        self.assertEqual((kb.ram, kb.eeprom), (IDLE_STYLE_EDEN, IDLE_STYLE_EDEN))

    def test_a_failing_OS_restore_still_restores_the_idle_style(self):
        kb = FakeKeyboard(style=0)
        real = kb.send
        state = {"rounds_done": False}

        def send(data, **kw):
            # Refuse cmd 29 only in the cleanup (after the reset reloaded RAM).
            if data[1] == tr.CMD_SET_OS and state["rounds_done"] and data[2] != 0xFF:
                raise RuntimeError("interface gone")
            return real(data, **kw)
        kb.send = send
        orig_cycle = kb.power_cycle

        def cycle():
            state["rounds_done"] = True
            orig_cycle()
        kb.power_cycle = cycle
        result, _seen = self._run(kb)
        self.assertEqual(result["status"], "pass", result)
        self.assertEqual((kb.ram, kb.eeprom), (0, 0))

    def test_a_refused_OS_restore_is_logged_as_FAILED(self):
        kb = FakeKeyboard(style=0)
        real = kb.send
        state = {"after_reset": False}

        def send(data, **kw):
            if data[1] == tr.CMD_SET_OS and state["after_reset"] and data[2] != 0xFF:
                return bytes([POLY_CHANNEL, data[1], ord("!")])
            return real(data, **kw)
        kb.send = send
        orig_cycle = kb.power_cycle

        def cycle():
            state["after_reset"] = True
            orig_cycle()
        kb.power_cycle = cycle
        logs = []
        result, _seen = self._run(kb, log=logs.append)
        self.assertEqual(result["status"], "pass", result)
        self.assertTrue(any("restore OS setting" in m and "FAILED" in m for m in logs),
                        logs)

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

    def test_eden_left_in_eeprom_by_an_earlier_session_cannot_fake_a_pass(self):
        # RAM says pulse, EEPROM still says Eden, and the suspend flush is broken.
        kb = FakeKeyboard(style=0, flush_works=False)
        kb.eeprom = IDLE_STYLE_EDEN
        result, _seen = self._run(kb)
        self.assertEqual(result["status"], "fail", "a stale Eden in EEPROM hid the "
                         "missing flush")

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

    def test_a_stall_line_that_arrives_late_still_FAILS(self):
        # The first report copy is lost to the console reattaching; the second
        # comes on a later host report, about 10 s on.
        self.stall_lines = ["WARNING core1 stalled: no answer for 500 ms at suspend"]
        self.stall_after = 8
        result, _seen = self._run(FakeKeyboard())
        self.assertEqual(result["status"], "fail")
        self.assertIn("core1 stall", result["error"])

    def test_unreadable_crash_records_FAIL_rather_than_pass(self):
        self.crash_before = None
        result, _seen = self._run(FakeKeyboard())
        self.assertEqual(result["status"], "fail")
        self.crash_before, self.crash_reads = self.crash_clean, 0
        self.crash_after = None
        result, _seen = self._run(FakeKeyboard())
        self.assertEqual(result["status"], "fail")
        self.assertIn("unreadable", result["error"])

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

    def test_a_rig_without_the_grant_SKIPS_before_touching_anything(self):
        # 2026-10-14: the grant had never been installed, the check FAILED on
        # every apply run, and the release gate refused firmware 1.9.2.
        tr.grant_missing = lambda: "no passwordless sudo grant for x"
        lines = []
        result, seen = self._run(FakeKeyboard(), log=lines.append)
        self.assertEqual(result["status"], "skip")
        self.assertIn("setup.sh --units-only", result["reason"])
        self.assertEqual(seen["suspends"], 0)
        self.assertTrue(any(line.startswith("[test] SKIP:") for line in lines))


class GrantMissingTest(unittest.TestCase):

    def setUp(self):
        fd, self.ctl = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.unlink, self.ctl)
        self.calls = []

    def _sudo(self, returncode, stderr):
        """A fake subprocess.run that answers the way real sudo does: a result
        with a return code, never an exception (the probe passes no check=)."""
        def run(argv, **kw):
            self.calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, returncode, None, stderr)
        return run

    def test_a_passwordless_grant_runs_the_wrapper_bare_and_is_not_missing(self):
        usage = (b"usage: polykybd-usb-suspend {unbind|bind} <dev:cfg.intf> | "
                 b"control <dev> {on|auto} | delay <dev> <ms>\n")
        self.assertIsNone(usb_suspend.grant_missing(self.ctl, run=self._sudo(2, usage)))
        (argv, kw), = self.calls
        self.assertEqual(argv, ["sudo", "-n", self.ctl])   # no operation: writes nothing
        self.assertIn("timeout", kw)

    def test_a_password_required_rule_is_missing(self):
        # %sudo ALL=(ALL:ALL) ALL allows the command but wants a password:
        # `sudo -l` would have called this granted (Greptile, ctnd#116).
        reason = usb_suspend.grant_missing(
            self.ctl, run=self._sudo(1, b"sudo: a password is required\n"))
        self.assertIn("no passwordless sudo grant", reason)
        self.assertIn("a password is required", reason)

    def test_exit_2_without_the_usage_line_is_missing(self):
        # Exit 2 alone could be anything; only the wrapper's own line counts.
        self.assertIsNotNone(usb_suspend.grant_missing(
            self.ctl, run=self._sudo(2, b"sudo: something else\n")))

    def test_a_hung_probe_is_missing_rather_than_hanging_the_run(self):
        def hangs(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        self.assertIn("did not answer", usb_suspend.grant_missing(self.ctl, run=hangs))

    def test_no_sudo_binary_is_missing(self):
        def no_sudo(argv, **_kw):
            raise FileNotFoundError("sudo")
        self.assertIsNotNone(usb_suspend.grant_missing(self.ctl, run=no_sudo))

    def test_an_uninstalled_wrapper_is_missing_and_sudo_is_not_asked(self):
        reason = usb_suspend.grant_missing(self.ctl + ".absent", run=self._sudo(2, b""))
        self.assertIn("is not installed", reason)
        self.assertEqual(self.calls, [])

    def test_the_real_wrapper_answers_a_bare_call_with_the_usage_line_and_exit_2(self):
        # The probe's success signal, taken from the script itself rather than
        # restated. Run as the test user: a bare call exits before any write.
        script = os.path.join(os.path.dirname(__file__), "..", "scripts",
                              "usb-suspend-ctl.sh")
        res = subprocess.run(["bash", script], capture_output=True, timeout=10)
        self.assertEqual(res.returncode, 2)
        self.assertIn(b"usage: polykybd-usb-suspend", res.stderr)


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
        from station.hil_tests import suspend_round_verdict as v
        self.assertIsNone(v("back", []))
        self.assertIsNone(v("not-rebooted", []))
        self.assertIn("reset while suspended", v("fresh", []))
        self.assertIn("did not answer", v("no-reply", []))
        self.assertIn("core1 stall", v("back", ["x"]))


# --- the root wrapper (scripts/usb-suspend-ctl.sh) -------------------------------

import shutil      # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

CTL = Path(__file__).resolve().parent.parent / "scripts" / "usb-suspend-ctl.sh"


@unittest.skipUnless(os.geteuid() != 0 or shutil.which("setpriv"),
                     "as root the wrapper ignores the fake tree; needs setpriv to drop it")
class UsbSuspendCtlTest(unittest.TestCase):
    """The wrapper's value is what it REFUSES, so the refusals are pinned here.

    It honours POLYKYBD_SYSFS_USB only when not root, so as root (this container)
    the script runs under setpriv as nobody."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fs = FakeSysfs(self._tmp.name)
        os.makedirs(os.path.join(self._tmp.name, "drivers", "usbhid"), exist_ok=True)
        for f in ("bind", "unbind"):
            FakeSysfs._put(os.path.join(self._tmp.name, "drivers", "usbhid", f), "")
        # A second HID device that is not the keyboard.
        d = os.path.join(self._tmp.name, "devices", "1-1.4")
        os.makedirs(os.path.join(d, "power"))
        FakeSysfs._put(os.path.join(d, "idVendor"), "046d\n")
        FakeSysfs._put(os.path.join(d, "idProduct"), "c52b\n")
        FakeSysfs._put(os.path.join(d, "power", "control"), "on\n")
        os.chmod(self._tmp.name, 0o777)
        for dirpath, dirs, files in os.walk(self._tmp.name):
            for n in dirs + files:
                p = os.path.join(dirpath, n)
                if not os.path.islink(p):
                    os.chmod(p, 0o777)

    def run_ctl(self, *args):
        cmd = ["bash", str(CTL), *args]
        if os.geteuid() == 0:
            cmd = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"] + cmd
        env = {**os.environ, "POLYKYBD_SYSFS_USB": self._tmp.name}
        return subprocess.run(cmd, env=env, capture_output=True, text=True)

    def read(self, *parts):
        with open(os.path.join(self._tmp.name, *parts), encoding="ascii") as f:
            return f.read()

    def test_the_four_operations_write_their_file(self):
        dev = self.fs.DEV
        for args, path, want in (
                (("unbind", f"{dev}:1.0"), ("drivers", "usbhid", "unbind"), f"{dev}:1.0"),
                (("bind", f"{dev}:1.2"), ("drivers", "usbhid", "bind"), f"{dev}:1.2"),
                (("control", dev, "auto"), ("devices", dev, "power", "control"), "auto"),
                (("delay", dev, "0"), ("devices", dev, "power", "autosuspend_delay_ms"), "0")):
            with self.subTest(args=args):
                r = self.run_ctl(*args)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.read(*path), want)

    def test_refusals(self):
        dev = self.fs.DEV
        for args in (
                (),
                ("unbind",),
                ("unbind", "1-1.4:1.0"),                 # another vendor's device
                ("control", "1-1.4", "auto"),
                ("unbind", f"{dev}:1.0", "extra"),
                ("unbind", "../../../etc/passwd"),
                ("control", f"{dev}/../1-1.4", "auto"),
                ("control", dev, "suspend"),
                ("delay", dev, "0; rm -rf /"),
                ("delay", dev, "99999999"),
                ("tee", "/etc/sudoers", "x"),
                ("control", "9-9", "on")):              # no such device
            with self.subTest(args=args):
                r = self.run_ctl(*args)
                self.assertNotEqual(r.returncode, 0, f"accepted {args}")
        self.assertEqual(self.read("devices", "1-1.4", "power", "control"), "on\n")


if __name__ == "__main__":
    unittest.main()
