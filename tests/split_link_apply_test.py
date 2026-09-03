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


class _ApplyFixture(unittest.TestCase):
    """Stubs for every device interaction the apply path makes.

    A TestCase with no test methods of its own, so the subclasses below share
    it without re-running each other's tests — inheriting a populated TestCase
    would silently run the parent's assertions once per subclass and inflate
    the count that is meant to prove new tests were registered at all.
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

        sleeps = []

        def fake_sleep(seconds):
            clock["t"] += seconds
            sleeps.append(seconds)

        fake_time = types.SimpleNamespace(sleep=fake_sleep,
                                          monotonic=lambda: clock["t"])
        test_runner.time = fake_time
        self.clock = clock
        self.sleeps = sleeps
        test_runner.TAP = types.SimpleNamespace(
            mark=lambda: 0,
            wait_for=lambda *a, **k: "banner",
            flush=lambda: None,
        )

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.tr, name, value)

    def _runner(self, console_ok=True, link=None, masters=1,
                flash_raises=False):
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

        seen["log"] = []
        runner = self.tr.TestRunner(log=seen["log"].append)
        runner._console = FakeConsole()
        class FakeFlash:
            def __init__(self):
                self.calls = []

            def flash(self, side, path, log=None):
                self.calls.append((side, path))
                if flash_raises:
                    raise RuntimeError("BOOTSEL never enumerated")

        runner._flash = FakeFlash()
        seen["flash"] = runner._flash
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

class ApplyRoundTripWiringTest(_ApplyFixture):
    """The link check must run against a console that is actually being fed.

    This is the test that would have caught the first cut of this feature. The
    suite STOPS the console before the whole firmware-update section (BEGIN
    tears USB down during the master's staging erase), so passing the
    run-start "did the console come up" flag straight through made the post-apply
    measurement read a `TAP` nothing was feeding: `LINK_NO_SUMMARY` on every run,
    present and passing and asserting nothing. Rendering the wiring correct in a
    diff is not the same as exercising it.
    """

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

    def test_the_two_master_run_does_not_also_blame_the_console(self):
        """One UNVERIFIED outcome, one reason — and not a false one.

        The console-blaming note used to fire on every LINK_NO_SUMMARY path, so a
        two-master run printed both the real reason and "the console did not
        reattach after the reboot". Run 33745711432 disproved that in its own
        log: the apply banner it printed a second earlier is only readable
        THROUGH that console. A diagnostic the same log falsifies is worse than
        none.
        """
        import tempfile
        runner, seen, _fc = self._runner(masters=2)
        with tempfile.TemporaryDirectory() as tmp:
            self._apply(runner, tmp)
        blamed = [ln for ln in seen["log"] if "did not reattach" in ln]
        self.assertEqual(blamed, [], f"blamed the console as well: {blamed}")
        self.assertTrue([ln for ln in seen["log"] if "second master" in ln],
                        "the real reason was not reported")

    def test_a_console_that_never_came_back_still_says_so(self):
        # The note is still the right one when the console IS the reason.
        import tempfile
        runner, seen, _fc = self._runner(console_ok=False)
        with tempfile.TemporaryDirectory() as tmp:
            self._apply(runner, tmp)
        self.assertTrue([ln for ln in seen["log"] if "did not reattach" in ln],
                        "the console failure was not reported")

    def test_a_dead_console_is_reported_even_when_two_masters_explain_the_run(self):
        """A dead console is its own fault, not an alternative explanation.

        With no console there is nothing to measure, so the late master-count
        re-check can set `explained` and suppress the console note — losing the
        only signal that says the counters will be unreadable for every later
        check too. The message must also not claim a measurement that never ran.
        """
        import tempfile
        # Console never comes back; the count settles at 1, then goes to 2 on the
        # post-measurement re-check.
        runner, seen, _fc = self._runner(console_ok=False, masters=[1] * 40 + [2])
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertFalse(seen["measured"])
        self.assertTrue([ln for ln in seen["log"] if "did not reattach" in ln],
                        "the console failure was suppressed")
        self.assertEqual(
            [ln for ln in seen["log"] if "during the measurement" in ln], [],
            "claimed a measurement that never ran")
        self.assertEqual(result["status"], "pass")


class PostApplySlaveReflashTest(_ApplyFixture):
    """The re-flash that lets the split link be measured at all on this rig.

    An apply necessarily converts the rig's slave into a second master (it
    installs the master's bridged bytes, and the two halves run different images
    by construction), so the link check inside the apply can only ever report
    UNVERIFIED here. Re-flashing the slave's own image and measuring afterwards
    asks the question that IS answerable: can the **applied master image** bring
    a split link back up?

    ⚠️ It does NOT answer "did the slave survive its own apply" — nothing on
    this rig can, and a test named as though it did would be worse than none.
    """

    def _post(self, runner):
        return runner.post_apply_split_link("right.uf2", console=True)

    def test_the_slave_is_reflashed_and_the_link_measured(self):
        runner, seen, _fc = self._runner()
        result = self._post(runner)
        self.assertEqual(seen["flash"].calls, [("right", "right.uf2")],
                         "the SLAVE half is the one that must be re-flashed")
        self.assertTrue(seen["measured"])
        self.assertTrue(seen["console_live_at_measure"],
                        "measured a TAP nothing was feeding — the check is inert")
        self.assertEqual(result["status"], "pass")

    def test_the_link_settles_BEFORE_it_is_measured(self):
        """The reconnect must not land inside the measured window.

        A master that exhausted SPLIT_MAX_CONNECTION_ERRORS throttles to one
        attempt per SPLIT_CONNECTION_CHECK_TIMEOUT and clears its error count on
        the first success — so the link recovers on its own, but the failing
        attempts before it are real transport_fails. The soak tolerates ~1% of
        ~450 frames, i.e. about 4, which a reconnect window clears easily, so
        measuring immediately would grade the reconnect as the fault.
        """
        at = {}
        runner, seen, _fc = self._runner()
        inner = self.tr.measure_split_link

        def timed(raw, log):
            at["sleeps"] = list(self.sleeps)
            return inner(raw, log)

        self.tr.measure_split_link = timed
        self._post(runner)
        self.assertIn("sleeps", at, "the link was never measured")
        # ⚠️ Assert the settle SLEEP happened, not that enough clock elapsed.
        # `_masters_after_apply` polls for up to _MASTERS_SETTLE_S right before
        # the measurement, so an elapsed-total form passes with the settle
        # deleted — it did, and this mutation escaped until the test asked the
        # question it meant to ask.
        self.assertIn(
            runner.POST_APPLY_LINK_SETTLE_S, at["sleeps"],
            "measured without waiting POST_APPLY_LINK_SETTLE_S for the link to "
            "re-establish — the reconnect lands inside the measured window")

    def test_a_link_fault_after_the_reflash_FAILS(self):
        runner, seen, _fc = self._runner(link=LINK_FAULT)
        result = self._post(runner)
        self.assertEqual(result["status"], "fail")

    def test_two_masters_after_the_reflash_is_a_rig_fault_and_is_not_measured(self):
        """A slave that did not come back as a slave leaves no link to measure.

        Grading that as a firmware fault would report the rig's own failed flash
        as the applied image being unable to talk to its slave.
        """
        runner, seen, _fc = self._runner(masters=2)
        result = self._post(runner)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(seen["measured"],
                         "measured a link that cannot exist")
        self.assertIn("RIG fault", "\n".join(seen["log"]))

    def test_a_dead_console_SKIPS_rather_than_failing(self):
        runner, seen, _fc = self._runner(console_ok=False)
        result = self._post(runner)
        self.assertEqual(result["status"], "skip")
        self.assertFalse(seen["measured"])

    def test_no_summary_SKIPS_rather_than_failing(self):
        runner, seen, _fc = self._runner(link=LINK_NO_SUMMARY)
        result = self._post(runner)
        self.assertEqual(result["status"], "skip")

    def test_a_failed_reflash_fails_without_measuring(self):
        runner, seen, _fc = self._runner(flash_raises=True)
        result = self._post(runner)
        self.assertEqual(result["status"], "fail")
        self.assertFalse(seen["measured"])


class ApplyResultCarriesTheLinkOutcomeTest(_ApplyFixture):
    """The apply reports WHICH link outcome it reached, not just pass/fail.

    That is what lets the caller skip the re-flash on a run whose link was
    already healthy — re-flashing there costs a full flash cycle to learn
    nothing — and it is the only thing distinguishing the two cases from
    outside, since both return ``status: pass``.
    """

    def test_a_healthy_link_is_reported_as_such(self):
        import tempfile
        runner, _seen, _fc = self._runner()
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertEqual(result.get("link"), LINK_OK)

    def test_an_unverified_link_is_reported_as_such(self):
        import tempfile
        runner, _seen, _fc = self._runner(masters=2)
        with tempfile.TemporaryDirectory() as tmp:
            result = self._apply(runner, tmp)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result.get("link"), LINK_NO_SUMMARY)


class ShouldReflashSlaveTest(unittest.TestCase):
    """The switch that decides whether the re-flash runs at all.

    Pinned because an unexercised one-line gate is exactly how the first cut of
    the post-apply link check shipped inert — present, passing, asserting
    nothing. Inverting either condition here has to turn a test red.
    """

    def setUp(self):
        from station.test_runner import should_reflash_slave
        self.decide = should_reflash_slave

    def test_an_unverified_link_after_a_passing_apply_is_followed_up(self):
        self.assertTrue(self.decide({"status": "pass", "link": LINK_NO_SUMMARY},
                                    enabled=True))

    def test_it_is_OFF_unless_the_caller_opts_in(self):
        """The opt-in is checked first, and defaults off.

        This runs inside the apply job, and require_fwapply_run.py gates
        publishing on that job's conclusion being success — so anything that can
        fail here can refuse a release. It has never executed against the rig,
        so per-merge is a decision for after a clean proof run.
        """
        self.assertFalse(self.decide({"status": "pass", "link": LINK_NO_SUMMARY},
                                     enabled=False))

    def test_a_healthy_link_needs_no_reflash(self):
        self.assertFalse(self.decide({"status": "pass", "link": LINK_OK},
                                     enabled=True))

    def test_a_failed_apply_is_not_followed_up(self):
        """Its diagnosis is the apply's, and a re-flash overwrites the evidence."""
        self.assertFalse(self.decide({"status": "fail", "link": LINK_NO_SUMMARY},
                                     enabled=True))

    def test_a_skipped_apply_is_not_followed_up(self):
        self.assertFalse(self.decide({"status": "skip",
                                      "reason": "no .sig beside the image"},
                                     enabled=True))

    def test_an_apply_result_with_no_link_key_is_followed_up(self):
        """An older result shape must not silently disable the check.

        Absent is not the same as OK: a result that never recorded a link
        outcome has not measured one, so the question is still open.
        """
        self.assertTrue(self.decide({"status": "pass"}, enabled=True))


class ReflashSlaveDefaultsOffTest(unittest.TestCase):
    """The CLI default is the safety property, so it is pinned.

    ``post_apply_split_link`` runs inside the firmware-apply job, and
    ``require_fwapply_run.py`` gates publishing on that job's conclusion being
    ``success`` — so anything that can fail there can refuse a release. It has
    never executed against the rig, so it must stay opt-in until a proof run
    says otherwise. A mutation flipping this default escaped the suite before
    the parser was made reachable.

    ⚠️ What is STILL uncovered is the one-line hand-off inside ``__main__``
    (``reflash_slave=args.reflash_slave``): a mutation wiring it to
    ``args.extended`` passes every test here, because nothing can import that
    block. Both ends are pinned — the parser default below and
    ``should_reflash_slave``'s opt-in — but the wire between them is not, and
    that is true of every flag in this file. Extracting a full ``main()`` is
    what would close it.
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
        from station.test_runner import build_parser
        self.build_parser = build_parser
        self.argv = ["--left", "l_hil_left.uf2", "--right", "r_hil_right.uf2"]

    def _parse(self, env=None):
        import os
        saved = os.environ.pop("HIL_RESLAVE", None)
        try:
            if env is not None:
                os.environ["HIL_RESLAVE"] = env
            return self.build_parser().parse_args(self.argv)
        finally:
            os.environ.pop("HIL_RESLAVE", None)
            if saved is not None:
                os.environ["HIL_RESLAVE"] = saved

    def test_it_is_off_when_nothing_asks_for_it(self):
        self.assertFalse(self._parse().reflash_slave)

    def test_the_flag_turns_it_on(self):
        self.argv += ["--reflash-slave"]
        self.assertTrue(self._parse().reflash_slave)

    def test_the_env_var_turns_it_on(self):
        """CI passes it as an env var, mirroring HIL_EXTENDED."""
        self.assertTrue(self._parse(env="1").reflash_slave)

    def test_an_empty_env_var_does_not_turn_it_on(self):
        self.assertFalse(self._parse(env="").reflash_slave)

    def test_extended_is_unaffected(self):
        """The two opt-ins are independent — extended must not imply this one."""
        self.argv += ["--extended"]
        args = self._parse()
        self.assertTrue(args.extended)
        self.assertFalse(args.reflash_slave)
