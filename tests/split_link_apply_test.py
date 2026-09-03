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
