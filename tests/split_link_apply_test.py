# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the post-APPLY slave/link assertion.

A firmware apply reboots BOTH halves, and until now the rig only ever asserted
that the *master* re-enumerated. The field report this closes (2026-09-03) had
exactly that shape: the master came back perfectly while the split link went
silent — ``transport_fail`` climbing on 201 of 201 frames with ``crc_err=0``,
i.e. the slave answering nothing rather than answering corrupt. Every assertion
the apply test made passed.

Two things are pinned here, and the second is the one that keeps the first
honest:

* a link fault after the apply FAILS the test;
* a console that produced no ``Split link:`` summary does NOT, because that is a
  failure to *measure*, not evidence about the slave. The distinction is only
  sound because the master prints the summary from ``send_to_bridge`` whether or
  not the slave answers — so a dead slave still yields two summaries.
"""
import sys
import types
import unittest

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import hil_tests                                   # noqa: E402
from station.console_log import TAP                             # noqa: E402
from station.hil_tests import (                                 # noqa: E402
    ACK, CMD_GET_LANG, LINK_FAULT, LINK_NO_SUMMARY, LINK_OK,
    POLY_CHANNEL, measure_split_link,
)

SUMMARY = ("Split link: {tx} tx crc_err={crc} nack={nack} "
           "transport_fail={tf} giveup={gu} err=0.0%")


class FakeLinkDevice:
    """A master that answers liveness probes and prints link summaries.

    ``summaries`` are fed into the shared console tap as the soak drains, which
    is what the firmware does — it prints from ``send_to_bridge``, i.e. as the
    bridged frames go out, not in response to any query.
    """

    def __init__(self, summaries, alive: bool = True):
        self._summaries = list(summaries)
        self._alive = alive
        self.restored = False

    def write_reports(self, reports):
        # Release one summary per batch, so the poll in measure_split_link has
        # something to find without the test waiting on real timing.
        if self._summaries:
            TAP.feed(self._summaries.pop(0) + "\n")

    def send(self, data, **kwargs):
        cmd = data[1]
        if cmd == hil_tests.CMD_OVERLAY_FLAGS_ON:
            self.restored = True
            return bytes([POLY_CHANNEL, cmd, ACK])
        if not self._alive:
            return None
        return bytes([POLY_CHANNEL, cmd, ACK]) + b"enUS"


def summaries(*deltas):
    """Cumulative summary lines from per-window (crc, tf) deltas."""
    tx = crc = tf = 0
    out = []
    for d_crc, d_tf in deltas:
        tx += 200
        crc += d_crc
        tf += d_tf
        out.append(SUMMARY.format(tx=tx, crc=crc, nack=0, tf=tf, gu=0))
    return out


class MeasureSplitLinkTest(unittest.TestCase):
    def setUp(self):
        # Keep the soak short; the reports are fed to a fake, so the count only
        # decides how many batches (and therefore summaries) are produced.
        self._saved = (hil_tests.LINK_SOAK_REPORTS, hil_tests.LINK_SUMMARY_TIMEOUT_S)
        hil_tests.LINK_SOAK_REPORTS = 100      # 2 batches of 50 -> 2 summaries
        hil_tests.LINK_SUMMARY_TIMEOUT_S = 1.0

    def tearDown(self):
        (hil_tests.LINK_SOAK_REPORTS,
         hil_tests.LINK_SUMMARY_TIMEOUT_S) = self._saved

    def test_a_clean_window_is_ok(self):
        dev = FakeLinkDevice(summaries((0, 0), (0, 0)))
        self.assertEqual(measure_split_link(dev, lambda _m: None), LINK_OK)
        self.assertTrue(dev.restored)

    def test_the_field_failure_is_a_fault_not_a_clean_pass(self):
        # crc_err stays 0 and transport_fail climbs on every frame: the slave is
        # not answering at all. This is the shape the rig used to miss entirely.
        dev = FakeLinkDevice(summaries((0, 0), (0, 201)))
        self.assertEqual(measure_split_link(dev, lambda _m: None), LINK_FAULT)

    def test_corrupted_frames_are_a_fault_too(self):
        dev = FakeLinkDevice(summaries((0, 0), (7, 0)))
        self.assertEqual(measure_split_link(dev, lambda _m: None), LINK_FAULT)

    def test_nack_alone_is_not_a_fault(self):
        # SYNC_BUSY arrives on every erase re-poll of a flash: the wire worked
        # and the slave said something other than yes.
        dev = FakeLinkDevice([
            SUMMARY.format(tx=200, crc=0, nack=0, tf=0, gu=0),
            SUMMARY.format(tx=400, crc=0, nack=40, tf=0, gu=0),
        ])
        self.assertEqual(measure_split_link(dev, lambda _m: None), LINK_OK)

    def test_no_summary_is_reported_as_UNMEASURED_not_as_a_dead_slave(self):
        dev = FakeLinkDevice([])
        self.assertEqual(measure_split_link(dev, lambda _m: None), LINK_NO_SUMMARY)

    def test_one_summary_is_not_a_measured_window(self):
        # The delta needs two: a single cumulative reading carries the documented
        # boot burst and says nothing about traffic this soak caused.
        dev = FakeLinkDevice(summaries((0, 0)))
        self.assertEqual(measure_split_link(dev, lambda _m: None), LINK_NO_SUMMARY)

    def test_an_unresponsive_master_is_a_fault(self):
        dev = FakeLinkDevice(summaries((0, 0), (0, 0)), alive=False)
        self.assertEqual(measure_split_link(dev, lambda _m: None), LINK_FAULT)

    def test_the_mapping_table_is_restored_even_on_a_fault(self):
        dev = FakeLinkDevice(summaries((0, 0), (0, 201)))
        measure_split_link(dev, lambda _m: None)
        self.assertTrue(dev.restored)


class GradedTestGatingTest(unittest.TestCase):
    """The graded test carries ``needs_console``, so it may fail on no-summary."""

    def test_the_suite_entry_requires_the_console(self):
        entry = next(t for t in hil_tests.TESTS
                     if t["fn"] is hil_tests.test_split_link_health)
        self.assertTrue(entry.get("needs_console"))


if __name__ == "__main__":
    unittest.main()


class FakeHidDevice:
    """A console handle that can die the way a re-enumerating keyboard does."""

    def __init__(self, lines, dead_after=None):
        self._lines = list(lines)
        self._dead_after = dead_after
        self._reads = 0
        self.closed = False

    def read(self, size, timeout=0):
        self._reads += 1
        if self._dead_after is not None and self._reads > self._dead_after:
            raise OSError("device gone")   # what hidraw does after re-enumeration
        if self._lines:
            return (self._lines.pop(0) + "\n").encode()
        return b""

    def close(self):
        self.closed = True


class ConsoleReopenTest(unittest.TestCase):
    """The console reader must SURVIVE the reboot an APPLY causes.

    Without this the reader dies at the first re-enumeration and never returns:
    every later ``read`` raises and the old loop just slept on the exception,
    logging nothing. That is why the apply test's own banner assertion had never
    fired — the window it exists to read is precisely the window the reader was
    dead for — and it is what would silently turn the new post-apply link check
    into a permanent "unverified".
    """

    def setUp(self):
        from station import hid as station_hid
        self.mod = station_hid
        self._saved_hid = station_hid.hid
        self._saved_find = station_hid._find_path

    def tearDown(self):
        self.mod.hid = self._saved_hid
        self.mod._find_path = self._saved_find

    def _console(self, devices):
        """A console whose successive opens hand out ``devices`` in order."""
        opened = []

        class FakeHidModule:
            @staticmethod
            def Device(path=None):
                if not devices:
                    raise OSError("no device")
                dev = devices.pop(0)
                opened.append(dev)
                return dev

        self.mod.hid = FakeHidModule
        self.mod._find_path = lambda *a, **k: b"/dev/hidraw-fake"
        console = self.mod.HIDConsole()
        console._REOPEN_POLL_S = 0.01
        return console, opened

    def test_lines_resume_after_the_device_re_enumerates(self):
        first = FakeHidDevice(["before the apply"], dead_after=1)
        second = FakeHidDevice(["Split link: 200 tx crc_err=0 nack=0 "
                                "transport_fail=0 giveup=0 err=0.0%"])
        console, opened = self._console([first, second])
        seen = []
        console.start(seen.append)
        try:
            deadline = __import__("time").monotonic() + 5.0
            while len(seen) < 2 and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.02)
        finally:
            console.stop()
        self.assertEqual(len(opened), 2, "the dead handle was never reopened")
        self.assertTrue(first.closed, "the dead handle was leaked")
        self.assertIn("before the apply", seen[0])
        self.assertTrue(any("Split link:" in s for s in seen),
                        f"nothing was read after the reopen: {seen!r}")

    def test_a_single_read_error_does_not_reopen(self):
        # A read error is also what a momentary USB hiccup looks like, and
        # RawHID opens its own handle per call — re-enumerating on every blip
        # would fight it.
        class Blippy(FakeHidDevice):
            def read(self, size, timeout=0):
                self._reads += 1
                if self._reads == 2:
                    raise OSError("blip")
                return b"still here\n"

        dev = Blippy([])
        console, opened = self._console([dev, FakeHidDevice([])])
        seen = []
        console.start(seen.append)
        try:
            __import__("time").sleep(0.3)
        finally:
            console.stop()
        self.assertEqual(len(opened), 1, "a single blip forced a needless reopen")

    def test_stop_returns_promptly_while_the_device_is_gone(self):
        import time as _t
        console, _opened = self._console([FakeHidDevice([], dead_after=0)])
        console.start(lambda _m: None)
        _t.sleep(0.2)                      # let it get into the reopen loop
        t0 = _t.monotonic()
        console.stop()
        self.assertLess(_t.monotonic() - t0, 2.0,
                        "stop() hung waiting on the reopen poll")

    def test_stop_does_not_close_a_handle_the_reader_still_holds(self):
        """A timed join is not a guarantee, and closing anyway is the abort.

        Closing a hidapi handle under an in-flight read is a use-after-free in
        libhidapi's hidraw backend — SIGABRT, exit 134, which is what the
        join-before-close ordering exists to prevent. The join has a timeout, so
        it can return with the reader still inside ``read``, the callback (a
        SocketIO emit in the touch UI, an unbounded wait) or ``_reopen``'s
        device lookup. When that happens the handle is abandoned, not closed.
        """
        import time as _t

        class SlowDevice(FakeHidDevice):
            def read(self, size, timeout=0):
                _t.sleep(5.0)          # overruns any join timeout
                return b""

        dev = SlowDevice([])
        console, _opened = self._console([dev])
        console._STOP_JOIN_S = 0.2      # keep the test quick; the shape is what matters
        console.start(lambda _m: None)
        _t.sleep(0.05)                  # let the reader get into the blocking read
        console.stop()
        self.assertFalse(dev.closed,
                         "stop() closed a handle the reader thread was still using")
        self.assertEqual(console.abandoned_handles, 1)


class ApplyRoundTripWiringTest(unittest.TestCase):
    """The link check must run against a console that is actually being fed.

    This is the test that would have caught the first cut of this feature. The
    suite STOPS the console before the whole firmware-update section (BEGIN
    tears USB down during the master's staging erase), so passing the
    run-start "did the console come up" flag straight through made the post-apply
    measurement read a `TAP` nothing was feeding: `LINK_NO_SUMMARY` on every run,
    present and passing and asserting nothing. Rendering the wiring correct in a
    diff is not the same as exercising it.
    """

    def setUp(self):
        if "RPi" not in sys.modules:  # pragma: no cover - environment shim
            rpi, gpio = types.ModuleType("RPi"), types.ModuleType("RPi.GPIO")
            for name in ("setmode", "setup", "output", "cleanup", "setwarnings"):
                setattr(gpio, name, lambda *a, **k: None)
            for name in ("BCM", "OUT", "HIGH", "LOW"):
                setattr(gpio, name, 0)
            rpi.GPIO = gpio
            sys.modules["RPi"], sys.modules["RPi.GPIO"] = rpi, gpio
        from station import test_runner
        self.tr = test_runner
        self._saved = {n: getattr(test_runner, n) for n in
                       ("stage_and_verify", "apply_staged", "caps_from_image",
                        "uf2_file_to_bin", "measure_split_link", "time", "TAP",
                        "enumerate_raw_interfaces")}
        # The real path sleeps out the applier's copy window and waits on the
        # boot banner; neither is what these tests are about.
        clock = {"t": 0.0}

        def fake_sleep(seconds):
            clock["t"] += seconds

        fake_time = types.SimpleNamespace(sleep=fake_sleep,
                                          monotonic=lambda: clock["t"])
        test_runner.time = fake_time
        test_runner.TAP = types.SimpleNamespace(
            mark=lambda: 0,
            wait_for=lambda *a, **k: "banner",
            flush=lambda: None,
        )

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.tr, name, value)

    def _runner(self, console_ok=True, link=None, masters=1):
        """A runner with every device interaction stubbed out."""
        seen = {"console_live_at_measure": None, "measured": False}

        class FakeConsole:
            live = False

            def start(self, _cb):
                if not console_ok:
                    raise RuntimeError("console not found")
                FakeConsole.live = True

            def stop(self):
                FakeConsole.live = False

        runner = self.tr.TestRunner(log=lambda _m: None)
        runner._console = FakeConsole()
        runner._flash = None
        runner._caps = {"fw": "9.9.9"}
        runner.wait_for_master_ready = lambda *a, **k: True
        runner.settle_master = lambda *a, **k: True
        runner._device_caps = lambda: {"fw": "9.9.9"}

        self.tr.uf2_file_to_bin = lambda _p: b"IMAGE"
        self.tr.caps_from_image = lambda _i: {"fw": "9.9.9"}
        self.tr.stage_and_verify = lambda *a, **k: True
        self.tr.apply_staged = lambda *a, **k: True

        def fake_measure(_raw, _log):
            seen["measured"] = True
            seen["console_live_at_measure"] = FakeConsole.live
            return link if link is not None else self.tr.LINK_OK

        self.tr.measure_split_link = fake_measure
        counts = list(masters) if isinstance(masters, (list, tuple)) else [masters]
        seen["enumerations"] = 0

        def fake_enumerate():
            i = min(seen["enumerations"], len(counts) - 1)
            seen["enumerations"] += 1
            return [{"path": b"one"}] * counts[i]

        self.tr.enumerate_raw_interfaces = fake_enumerate
        return runner, seen, FakeConsole

    def _apply(self, runner, tmp):
        import pathlib
        binp = pathlib.Path(tmp) / "fw.bin"
        binp.write_bytes(b"IMAGE")
        (binp.parent / "fw.bin.sig").write_bytes(b"\0" * 64)
        return runner.firmware_apply_roundtrip(str(binp), "left.uf2", console=True)

    def test_the_console_is_LIVE_when_the_link_is_measured(self):
        import tempfile
        runner, seen, _fc = self._runner()
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertTrue(seen["measured"], "the link was never measured")
        self.assertTrue(seen["console_live_at_measure"],
                        "measured a TAP that nothing was feeding — the check is inert")
        self.assertEqual(result["status"], "pass")

    def test_a_link_fault_fails_the_apply(self):
        import tempfile
        runner, _seen, _fc = self._runner(link="fault")
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertEqual(result["status"], "fail")
        self.assertIn("split link", result["error"])

    def test_a_console_that_will_not_reattach_does_not_fail_the_apply(self):
        import tempfile
        runner, seen, _fc = self._runner(console_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertFalse(seen["measured"],
                         "measured the link with no console — the result is meaningless")
        self.assertEqual(result["status"], "pass")

    def test_the_console_is_left_stopped_for_what_follows(self):
        # reboot_persistence power-cycles the master straight after this.
        import tempfile
        runner, _seen, fc = self._runner()
        with tempfile.TemporaryDirectory() as tmp:
            self._apply(runner, tmp)
        self.assertFalse(fc.live)

    def test_two_masters_after_the_apply_is_reported_not_graded(self):
        """The rig's own apply semantics must not read as a firmware fault.

        The slave installs its own STAGED image, which is the master's image
        bridged during CHUNK. On a real keyboard that is correct — one image,
        role chosen at runtime by VBUS. On the rig the halves run different
        images by construction, so the slave applies the master image and comes
        back as a second master: no slave, 100% transport_fail. Observed on run
        33733020495 (12930/12930 frames, crc_err=0).
        """
        import tempfile
        runner, seen, _fc = self._runner(masters=2)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertFalse(seen["measured"],
                         "measured a link whose slave is running a master image")
        self.assertEqual(result["status"], "pass")

    def test_one_master_and_a_dead_link_still_FAILS(self):
        # The distinction is what keeps the check worth having: this is the
        # shape of the field report (master fine, slave silent).
        import tempfile
        runner, _seen, _fc = self._runner(masters=1, link="fault")
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertEqual(result["status"], "fail")

    def test_a_slave_that_reboots_LATE_is_not_graded_as_a_fault(self):
        """The slave reboots seconds after the master, so one enumeration races.

        A single check before the soak can catch the slave mid-boot and read one
        interface where there will shortly be two. Grading that as a fault would
        put a false red on a gate that runs on every merge — the exact failure
        this guard exists to prevent.
        """
        import tempfile
        runner, seen, _fc = self._runner(masters=[1, 2], link="fault")
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        # The settle loop now catches the transition BEFORE measuring, which is
        # strictly better than measuring and re-checking. What this test pins is
        # the outcome — a late reboot is never graded as a fault — not which of
        # the two paths reaches it.
        self.assertEqual(result["status"], "pass")

    def test_a_genuinely_dead_slave_still_fails_after_the_recheck(self):
        # One master throughout: nothing rebooted into a master image, the link
        # is simply dead. This is the field-report shape and must stay a FAIL.
        import tempfile
        runner, _seen, _fc = self._runner(masters=[1, 1], link="fault")
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertEqual(result["status"], "fail")

    def test_a_settled_ONE_master_is_not_an_early_exit(self):
        """The false-GREEN case: the slave may simply not have rebooted yet.

        A stable count of 1 is not terminal — on this rig the apply's terminal
        state is two masters — so returning early on it would let the runner
        measure the still-healthy PRE-reboot slave and report "both halves came
        back from the apply" when the slave had not yet applied anything.
        """
        import tempfile
        # 1 for a while, then the slave's delayed reboot lands.
        runner, seen, _fc = self._runner(masters=[1, 1, 1, 1, 2, 2, 2])
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertFalse(seen["measured"],
                         "measured before the slave's delayed reboot had landed")
        self.assertEqual(result["status"], "pass")

    def test_a_real_single_master_rig_still_measures(self):
        # Count never moves off 1: nothing rebooted into a master image, so the
        # link is measured normally rather than exempted.
        import tempfile
        runner, seen, _fc = self._runner(masters=1)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertTrue(seen["measured"])
        self.assertEqual(result["status"], "pass")

    def test_a_reboot_that_lands_DURING_the_measurement_is_still_caught(self):
        """The re-check after a measured fault is not made redundant by the settle.

        The settle loop covers a slave that transitions before the soak starts.
        One that transitions *during* it still reaches the measurement, comes
        back as a fault, and must be caught by the post-measurement re-check —
        so both guards are load-bearing.
        """
        import tempfile
        # Stable 1 for the whole settle window, so the loop times out and the
        # link is measured; the transition lands afterwards.
        runner, seen, _fc = self._runner(masters=[1] * 40 + [2], link="fault")
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertTrue(seen["measured"], "the settle loop should have timed out")
        self.assertEqual(result["status"], "pass")
