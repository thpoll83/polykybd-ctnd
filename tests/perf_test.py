# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the performance harness — no rig, no keyboard required.

The rig is not reachable from a development session, so the parts of
``station.perf`` / ``station.perf_runner`` that can be pinned down without
hardware are pinned down here:

* ``FakeProfilerDevice`` re-implements the firmware's cmd-32 replies **byte for
  byte** from the C in ``keyboards/polykybd/profiling/loop_profile.c``. That makes
  this a real contract test of the wire format: if the C encoder and the Python
  decoder ever disagree about layout, ordering or endianness, this fails here
  instead of producing plausible-looking nonsense on the rig.
* The workloads are driven end to end against that fake, so the report plumbing
  (report shape, baseline comparison, markdown) is exercised too.

Run with::

    python -m unittest discover -s tests -p "*_test.py"
"""
import struct
import sys
import types
import unittest

# The station package imports hidapi and RPi.GPIO at module load; neither exists
# (nor is needed) off the rig. Stub them before importing anything from station.
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

from station import perf                                        # noqa: E402
from station.perf_runner import (                               # noqa: E402
    TRACKED_METRICS, compare_to_baseline, dig, format_markdown,
)

POLY_CHANNEL = ord("P")
ACK, NACK = ord("."), ord("!")


class FakeProfilerDevice:
    """A stand-in keyboard that answers the profiler command like the firmware.

    Mirrors ``hid_com.c`` case 32 and ``loop_profile.c``: a 4-byte reply header
    (``P``, cmd, status, page) followed by the little-endian snapshot body, padded
    to a 64-byte report. ``profiling=False`` reproduces a NORMAL build, where the
    case is compiled out and the dispatcher's default branch NACKs — the signal
    the harness uses to detect a non-profiling firmware.
    """

    def __init__(self, profiling: bool = True, drop_every: int = 0,
                 snapshot_version: int = perf.SNAPSHOT_VERSION, window_us: int = 3_000_000):
        self.profiling = profiling
        self.snapshot_version = snapshot_version
        # What the firmware's own clock says the window lasted (v2 page 0).
        self.window_us = window_us
        # drop_every=N makes every Nth send_repeated exchange come back empty, so
        # the miss-counting path is actually exercised rather than asserted to be
        # zero against a fake that can never miss.
        self.drop_every = drop_every
        self.reset_count = 0
        self.log_count = 0
        self.reports_written = []
        # Counters the fake reports back; the reset zeroes them like the real one.
        self.state = dict(iters=12345, ovl_iters=206, max_us=105_000,
                          max_bridge_us=5_000, max_render_us=67_000, max_overlay=True,
                          ovl_wall_us=3_668_000, ovl_bridge_us=783_000,
                          ovl_render_us=1_927_000,
                          bkt_norm=[0, 264900, 13317, 51, 0, 0, 54],
                          bkt_ovl=[0, 0, 17, 153, 1, 7, 28])
        # v2 serves page 1 from a copy latched by the page-0 read.
        self.latched = (list(self.state["bkt_norm"]), list(self.state["bkt_ovl"]))
        # RawHID's timeout bookkeeping, which callers may read.
        self.timeouts_recovered = 0
        self.timeouts_failed = 0
        self.lost_replies = 0

    # --- the RawHID surface the harness uses ---------------------------------
    def send(self, data: bytes, timeout_ms: int = 3000, attempts: int = 3):
        cmd = data[1]
        if cmd == perf.CMD_PROFILE:
            return self._profile(data)
        if cmd == 0x06:  # GET_ID
            ident = b"Split72 0.9.83 P11 HW1 \x00"
            return (bytes([POLY_CHANNEL, 0x06, ACK]) + ident).ljust(64, b"\x00")
        return bytes([POLY_CHANNEL, cmd, ACK]).ljust(64, b"\x00")

    def write_reports(self, reports):
        self.reports_written.extend(reports)

    def send_repeated(self, data: bytes, count: int, timeout_ms: int = 1000, retries: int = 2):
        responses = [
            None if self.drop_every and (i + 1) % self.drop_every == 0 else self.send(data)
            for i in range(count)
        ]
        latencies = [3.0 + (i % 5) for i in range(count)]
        return responses, latencies, 0

    # --- firmware emulation ---------------------------------------------------
    def _profile(self, data: bytes):
        if not self.profiling:
            # Compiled out -> unknown command -> default branch NACK.
            return bytes([POLY_CHANNEL, perf.CMD_PROFILE, NACK]).ljust(64, b"\x00")
        sub, page = data[2], data[3]
        if sub == perf.PROF_SUB_RESET:
            self.reset_count += 1
            self.state.update(iters=0, ovl_iters=0, max_us=0, max_bridge_us=0,
                              max_render_us=0, max_overlay=False, ovl_wall_us=0,
                              ovl_bridge_us=0, ovl_render_us=0,
                              bkt_norm=[0] * 7, bkt_ovl=[0] * 7)
            self.latched = ([0] * 7, [0] * 7)
            return bytes([POLY_CHANNEL, perf.CMD_PROFILE, ACK]).ljust(64, b"\x00")
        if sub == perf.PROF_SUB_LOG:
            self.log_count += 1
            return bytes([POLY_CHANNEL, perf.CMD_PROFILE, ACK]).ljust(64, b"\x00")
        if sub == perf.PROF_SUB_READ:
            body = self._snapshot(page)
            if body is None:
                return bytes([POLY_CHANNEL, perf.CMD_PROFILE, NACK]).ljust(64, b"\x00")
            return (bytes([POLY_CHANNEL, perf.CMD_PROFILE, ACK, page]) + body).ljust(64, b"\x00")
        return bytes([POLY_CHANNEL, perf.CMD_PROFILE, NACK]).ljust(64, b"\x00")

    def _snapshot(self, page: int):
        s = self.state
        if page == 0:
            body = (bytes([self.snapshot_version, 0x01 if s["max_overlay"] else 0x00, 0, 0])
                    + struct.pack("<8I", s["iters"], s["ovl_iters"], s["max_us"],
                                  s["max_bridge_us"], s["max_render_us"],
                                  s["ovl_wall_us"], s["ovl_bridge_us"], s["ovl_render_us"]))
            if self.snapshot_version >= 2:
                body += struct.pack("<I", self.window_us)
                self.latched = (list(s["bkt_norm"]), list(s["bkt_ovl"]))
            return body
        if page == 1:
            norm, ovl = (self.latched if self.snapshot_version >= 2
                         else (s["bkt_norm"], s["bkt_ovl"]))
            return struct.pack("<14I", *(norm + ovl))
        return None


def _quiet(_msg):
    """Swallow harness log output so the test run stays readable."""


class TestSnapshotWireFormat(unittest.TestCase):
    def test_round_trip_matches_firmware_encoding(self):
        dev = FakeProfilerDevice()
        prof = perf.Profiler(dev, _quiet)
        got = prof.read()
        self.assertEqual(got.iters, 12345)
        self.assertEqual(got.ovl_iters, 206)
        self.assertEqual(got.max_us, 105_000)
        self.assertTrue(got.max_overlay)
        self.assertEqual(got.bkt_norm, [0, 264900, 13317, 51, 0, 0, 54])
        self.assertEqual(got.bkt_ovl, [0, 0, 17, 153, 1, 7, 28])
        # rest = wall - bridge - render, the line that settles bridge-vs-render.
        self.assertEqual(got.ovl_rest_us, 3_668_000 - 783_000 - 1_927_000)
        # >= 10 ms iterations across both histograms (buckets 4..6).
        self.assertEqual(got.long_iters, 54 + 1 + 7 + 28)

    def test_v2_carries_the_keyboards_window(self):
        got = perf.Profiler(FakeProfilerDevice(window_us=9_001_000), _quiet).read()
        self.assertEqual(got.version, 2)
        self.assertEqual(got.window_us, 9_001_000)
        self.assertEqual(got.to_dict()["device_window_ms"], 9001.0)

    def test_v1_still_decodes_without_a_window(self):
        got = perf.Profiler(FakeProfilerDevice(snapshot_version=1), _quiet).read()
        self.assertEqual(got.iters, 12345)
        self.assertIsNone(got.window_us)
        self.assertNotIn("device_window_ms", got.to_dict())

    def test_v2_page0_must_carry_window(self):
        body0 = bytes([2, 0, 0, 0]) + struct.pack("<8I", *([0] * 8))
        body1 = struct.pack("<14I", *([0] * 14))
        with self.assertRaisesRegex(ValueError, "too short"):
            perf.decode_snapshot({0: body0, 1: body1})

    def test_rejects_unknown_snapshot_version(self):
        body0 = bytes([max(perf.SNAPSHOT_VERSIONS) + 1, 0, 0, 0]) + struct.pack("<9I", *([0] * 9))
        body1 = struct.pack("<14I", *([0] * 14))
        with self.assertRaisesRegex(ValueError, "wire format changed"):
            perf.decode_snapshot({0: body0, 1: body1})

    def test_rejects_missing_page(self):
        body0 = bytes([perf.SNAPSHOT_VERSION, 0, 0, 0]) + struct.pack("<8I", *([0] * 8))
        with self.assertRaisesRegex(ValueError, "missing page"):
            perf.decode_snapshot({0: body0})

    def test_reset_zeroes_the_window(self):
        dev = FakeProfilerDevice()
        prof = perf.Profiler(dev, _quiet)
        prof.reset()
        self.assertEqual(dev.reset_count, 1)
        self.assertEqual(prof.read().iters, 0)


class _LossyDevice(FakeProfilerDevice):
    """Every profiler READ needs two re-sends, as in run 37152259246."""

    def send(self, data, timeout_ms=3000, attempts=3):
        if data[1] == perf.CMD_PROFILE and data[2] == perf.PROF_SUB_READ:
            self.lost_replies += 2
            self.timeouts_recovered += 1
        resp = super().send(data, timeout_ms, attempts)
        if data[1] == perf.CMD_PROFILE and data[2] == perf.PROF_SUB_RESET:
            self.state["iters"] = 9039   # what the loop did in the stretched window
        return resp


class TestIdleWindow(unittest.TestCase):
    """The idle rate must use the window the firmware measured, not the sleep."""

    def _lossy(self, **kw):
        return _LossyDevice(**kw)

    def test_rate_uses_the_keyboard_window_when_replies_were_lost(self):
        dev = self._lossy(window_us=9_001_000)
        out = perf.measure_idle_overhead(dev, perf.Profiler(dev, _quiet), _quiet, seconds=0)
        self.assertEqual(out["window_source"], "device")
        self.assertEqual(out["iters_per_s"], 1004.2)
        self.assertEqual(out["hid_retries"], 4)       # two per page
        self.assertNotIn("valid", out)                # the device clock makes it sound

    def test_v1_falls_back_to_host_clock_and_is_invalid_after_retries(self):
        dev = self._lossy(snapshot_version=1)
        out = perf.measure_idle_overhead(dev, perf.Profiler(dev, _quiet), _quiet, seconds=0)
        self.assertEqual(out["window_source"], "host")
        self.assertIs(out["valid"], False)
        self.assertGreater(out["hid_retries"], 0)

    def test_clean_v1_window_stays_valid(self):
        dev = FakeProfilerDevice(snapshot_version=1)
        out = perf.measure_idle_overhead(dev, perf.Profiler(dev, _quiet), _quiet, seconds=0)
        self.assertEqual(out["hid_retries"], 0)
        self.assertNotIn("valid", out)

    def test_v2_page1_is_latched_at_the_page0_read(self):
        dev = FakeProfilerDevice()
        prof = perf.Profiler(dev, _quiet)
        dev._profile(bytes([POLY_CHANNEL, perf.CMD_PROFILE, perf.PROF_SUB_READ, 0]))
        dev.state["bkt_norm"] = [999] * 7               # the loop ran on
        body = dev._profile(bytes([POLY_CHANNEL, perf.CMD_PROFILE, perf.PROF_SUB_READ, 1]))
        self.assertEqual(struct.unpack_from("<7I", body, 4)[0], 0)  # still the latch

    def test_markdown_flags_lost_replies(self):
        dev = self._lossy(window_us=9_001_000)
        idle = perf.measure_idle_overhead(dev, perf.Profiler(dev, _quiet), _quiet, seconds=0)
        md = format_markdown({"idle": idle})
        self.assertIn("Profiler replies were lost and re-sent", md)
        self.assertNotIn("clock: the firmware predates", md)


class TestProfilerAvailability(unittest.TestCase):
    def test_available_on_a_profiling_build(self):
        self.assertTrue(perf.Profiler(FakeProfilerDevice(True), _quiet).available())

    def test_unavailable_on_a_normal_build(self):
        """A normal build NACKs cmd 32 — that must read as 'no profiler', not a fault."""
        self.assertFalse(perf.Profiler(FakeProfilerDevice(False), _quiet).available())

    def test_reset_raises_a_clear_error_on_a_normal_build(self):
        prof = perf.Profiler(FakeProfilerDevice(False), _quiet)
        with self.assertRaises(perf.ProfilerUnavailable):
            prof.reset()

    def test_no_reply_is_a_device_fault_not_a_missing_profiler(self):
        """A dropped reply must NOT be reported as "not a profiling build".

        Collapsing the two would send someone off rebuilding firmware that was
        fine. A NACK is a capability answer; silence is a device fault."""
        class _SilentDevice(FakeProfilerDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                return None

        prof = perf.Profiler(_SilentDevice(), _quiet)
        with self.assertRaisesRegex(RuntimeError, "not responding"):
            prof.reset()
        # ...and it must not be swallowed as ProfilerUnavailable.
        with self.assertRaises(RuntimeError) as caught:
            prof.read()
        self.assertNotIsInstance(caught.exception, perf.ProfilerUnavailable)
        # available() downgrades a fault to False (it is a probe), but logs it.
        self.assertFalse(prof.available())


class TestOverlayWorkloads(unittest.TestCase):
    def test_plain_burst_framing_is_protocol_11(self):
        reports = perf.plain_overlay_reports(keys=3)
        self.assertEqual(len(reports), 3 * perf.NUM_SEGMENTS)
        for r in reports:
            # 4-byte header + a full 60-byte segment fills the report exactly.
            self.assertEqual(len(r), 64)
            self.assertEqual(r[0], POLY_CHANNEL)
            self.assertEqual(r[1], perf.CMD_SEND_OVERLAY)
        # keycodes ascend from KC_A, segment index lives in the high nibble.
        self.assertEqual(reports[0][2], perf.KC_A)
        self.assertEqual([r[3] >> 4 for r in reports[:perf.NUM_SEGMENTS]],
                         list(range(perf.NUM_SEGMENTS)))
        self.assertEqual(reports[perf.NUM_SEGMENTS][2], perf.KC_A + 1)

    def test_compressed_burst_is_one_report_per_key(self):
        reports = perf.compressed_overlay_reports(keys=5)
        self.assertEqual(len(reports), 5)
        self.assertTrue(all(r[1] == perf.CMD_START_COMPRESSED_OVERLAY for r in reports))

    def test_measure_overlay_burst_brackets_the_workload(self):
        dev = FakeProfilerDevice()
        prof = perf.Profiler(dev, _quiet)
        out = perf.measure_overlay_burst(dev, prof, _quiet, kind="plain", keys=4)
        # RESET ran before the burst, and the burst actually reached the device.
        self.assertEqual(dev.reset_count, 1)
        self.assertEqual(len(dev.reports_written), 4 * perf.NUM_SEGMENTS)
        self.assertEqual(out["kind"], "plain")
        self.assertEqual(out["reports"], 4 * perf.NUM_SEGMENTS)
        # Post-reset counters are zero, which is what a decoded window must show.
        self.assertEqual(out["iters"], 0)
        self.assertIn("worst_iter_ms", out)

    def test_unknown_burst_kind_is_rejected(self):
        dev = FakeProfilerDevice()
        with self.assertRaises(ValueError):
            perf.measure_overlay_burst(dev, perf.Profiler(dev, _quiet), _quiet, kind="nope")


class TestAppSwitchWorkload(unittest.TestCase):
    OPS = [
        {"op": "request", "hex": "500bc4"},
        {"op": "write", "hex": "5012" + "00" * 10},
        {"op": "write", "hex": "5013" + "00" * 10},
        {"op": "pause", "s": 0.3},
        {"op": "write", "hex": "5021" + "00" * 10},
        {"op": "request", "hex": "500b01"},
    ]

    def test_replay_keeps_order_and_counts(self):
        dev = FakeProfilerDevice()
        sent = []
        dev.send = lambda data, timeout_ms=3000, attempts=3: sent.append(bytes(data)) or b"P\x0b."
        stats = perf.replay_app_switch(dev, self.OPS, pace=False)
        self.assertEqual(stats, {"writes": 3, "requests": 2, "unanswered": 0,
                                 "pauses": 1, "pause_s": 0.3, "failed_request": None})
        self.assertEqual([r[1] for r in dev.reports_written], [0x12, 0x13, 0x21])
        self.assertEqual([r[2] for r in sent], [0xC4, 0x01])

    def test_unanswered_prepare_stops_the_replay(self):
        dev = FakeProfilerDevice()
        dev.send = lambda data, timeout_ms=3000, attempts=3: None
        stats = perf.replay_app_switch(dev, self.OPS, pace=False)
        self.assertEqual(stats["unanswered"], 1)
        self.assertEqual(stats["failed_request"], 0)
        self.assertEqual(dev.reports_written, [], "no upload after a failed prepare")

    def test_nacked_enable_fails_the_phase(self):
        dev = FakeProfilerDevice()
        replies = iter([b"P\x0b.", b"P\x0b!"])
        dev.send = lambda data, timeout_ms=3000, attempts=3: next(replies)
        stats = perf.replay_app_switch(dev, self.OPS, pace=False)
        self.assertEqual(stats["failed_request"], 5)
        self.assertEqual(stats["unanswered"], 0)

    def test_reply_to_another_command_is_not_an_ack(self):
        dev = FakeProfilerDevice()
        dev.send = lambda data, timeout_ms=3000, attempts=3: b"P\x06."
        stats = perf.replay_app_switch(dev, self.OPS, pace=False)
        self.assertEqual(stats["failed_request"], 0)

    def test_failed_phase_is_marked_invalid(self):
        dev = FakeProfilerDevice()
        fixture = {"stem": "t", "cold": self.OPS}
        real_send = dev.send
        dev.send = lambda data, timeout_ms=3000, attempts=3: (
            b"P\x0b!" if data[1] == 0x0B else real_send(data, timeout_ms, attempts))
        out = perf.measure_app_switch(dev, perf.Profiler(dev, _quiet), _quiet, fixture, "cold")
        self.assertFalse(out["valid"])

    def test_unknown_op_is_rejected(self):
        with self.assertRaises(ValueError):
            perf.replay_app_switch(FakeProfilerDevice(), [{"op": "nope"}], pace=False)

    def test_measure_app_switch_brackets_the_replay(self):
        dev = FakeProfilerDevice()
        fixture = {"stem": "t", "cold": [o for o in self.OPS if o["op"] != "pause"]}
        out = perf.measure_app_switch(dev, perf.Profiler(dev, _quiet), _quiet, fixture, "cold")
        self.assertEqual(dev.reset_count, 1)
        self.assertEqual(out["reports"], 5)
        self.assertEqual(out["phase"], "cold")
        self.assertTrue(out["settled"])
        self.assertTrue(out["valid"])
        self.assertIn("ovl_render_ms", out)

    def test_committed_fixtures_are_well_formed(self):
        """The recorded streams must look like a real send_overlays_mru: prepare
        first, enable last, and a warm phase that uploads no image at all."""
        fixtures = perf.load_app_switch_fixtures()
        self.assertIn("word", fixtures)
        image_cmds = {10, 16, 17, 18, 19}
        for name, fx in fixtures.items():
            for phase in ("cold", "warm"):
                ops = [o for o in fx[phase] if o["op"] != "pause"]
                self.assertTrue(all(o["op"] in ("write", "request") for o in ops), name)
                self.assertTrue(all(len(bytes.fromhex(o["hex"])) <= 64 for o in ops), name)
                self.assertEqual(ops[0]["hex"][:4], "500b", f"{name} {phase}: prepare first")
                self.assertEqual(ops[-1]["op"], "request", f"{name} {phase}: enable last")
            cold_cmds = {int(o["hex"][2:4], 16) for o in fx["cold"] if "hex" in o}
            warm_cmds = {int(o["hex"][2:4], 16) for o in fx["warm"] if "hex" in o}
            self.assertTrue(cold_cmds & image_cmds, f"{name}: cold uploads images")
            self.assertFalse(warm_cmds & image_cmds, f"{name}: warm uploads nothing")


class TestLatencyStats(unittest.TestCase):
    def test_percentiles_are_observed_samples(self):
        out = perf.percentiles([10.0, 20.0, 30.0, 40.0, 50.0])
        self.assertEqual(out["p50"], 30.0)
        self.assertEqual(out["max"], 50.0)
        self.assertEqual(out["n"], 5)
        # Nearest-rank never invents a value between two samples.
        self.assertIn(out["p95"], [10.0, 20.0, 30.0, 40.0, 50.0])

    def test_empty_sample_set(self):
        self.assertEqual(perf.percentiles([]), {})

    def test_measure_hid_latency_clean_run(self):
        out = perf.measure_hid_latency(FakeProfilerDevice(), _quiet, n=10)
        self.assertEqual(out["sent"], 10)
        self.assertEqual(out["misses"], 0)
        self.assertEqual(out["n"], 10)

    def test_measure_hid_latency_counts_misses(self):
        """Dropped replies are counted and excluded from the percentile sample."""
        dev = FakeProfilerDevice(drop_every=5)   # sends 5 and 10 come back empty
        out = perf.measure_hid_latency(dev, _quiet, n=10)
        self.assertEqual(out["sent"], 10)
        self.assertEqual(out["misses"], 2)
        # Percentiles are computed over the replies that arrived, not all sends.
        self.assertEqual(out["n"], 8)


class TestBaselineComparison(unittest.TestCase):
    def _report(self, **over):
        rep = {
            "overlay_plain": {"worst_iter_ms": 100.0, "ovl_wall_ms": 3668.0,
                              "ovl_bridge_ms": 783.0, "ovl_render_ms": 1927.0},
            "overlay_compressed": {"worst_iter_ms": 50.0, "ovl_wall_ms": 900.0},
            "hid_latency": {"p50": 4.0, "p95": 7.0, "max": 20.0},
            "idle": {"worst_iter_ms": 2.0, "iters_per_s": 1000.0},
            "timing": {"boot_to_ready_s": 6.0},
            "app_switch": {
                "word": {"cold": {"host_wall_ms": 1500.0, "ovl_wall_ms": 400.0},
                         "warm": {"host_wall_ms": 60.0, "ovl_wall_ms": 30.0}},
                "jetbrains": {"cold": {"host_wall_ms": 4500.0}},
            },
        }
        for path, value in over.items():
            section, _, key = path.partition("__")
            rep[section][key] = value
        return rep

    def test_identical_reports_show_no_change(self):
        rep = self._report()
        cmp = compare_to_baseline(rep, self._report())
        self.assertTrue(cmp, "expected the tracked metrics to be comparable")
        self.assertTrue(all(c["verdict"] == "same" for c in cmp))

    def test_slower_is_a_regression(self):
        cmp = compare_to_baseline(self._report(hid_latency__p95=14.0), self._report())
        p95 = next(c for c in cmp if c["metric"] == "hid_latency.p95")
        self.assertEqual(p95["verdict"], "regression")
        self.assertEqual(p95["delta_pct"], 100.0)

    def test_faster_is_an_improvement(self):
        cmp = compare_to_baseline(self._report(overlay_plain__ovl_render_ms=900.0),
                                  self._report())
        render = next(c for c in cmp if c["metric"] == "overlay_plain.ovl_render_ms")
        self.assertEqual(render["verdict"], "improvement")

    def test_lower_loop_rate_is_a_regression(self):
        """iters_per_s is the one metric where LOWER is worse — check the polarity."""
        cmp = compare_to_baseline(self._report(idle__iters_per_s=500.0), self._report())
        rate = next(c for c in cmp if c["metric"] == "idle.iters_per_s")
        self.assertEqual(rate["verdict"], "regression")

    def test_zero_baseline_never_reports_a_regression(self):
        base = self._report()
        base["hid_latency"]["p50"] = 0.0
        cmp = compare_to_baseline(self._report(), base)
        p50 = next(c for c in cmp if c["metric"] == "hid_latency.p50")
        self.assertEqual(p50["verdict"], "same")
        self.assertIsNone(p50["delta_pct"])

    def test_new_metric_is_skipped_not_flagged(self):
        base = self._report()
        del base["hid_latency"]["max"]
        metrics = {c["metric"] for c in compare_to_baseline(self._report(), base)}
        self.assertNotIn("hid_latency.max", metrics)
        self.assertIn("hid_latency.p95", metrics)

    def test_invalid_app_switch_phase_is_not_compared(self):
        """A clipped phase must not produce a verdict, on either side."""
        cur = self._report()
        cur["app_switch"]["word"]["cold"].update(valid=False, host_wall_ms=99999.0)
        metrics = {c["metric"] for c in compare_to_baseline(cur, self._report())}
        self.assertNotIn("app_switch.word.cold.host_wall_ms", metrics)
        self.assertIn("app_switch.word.warm.host_wall_ms", metrics)
        metrics = {c["metric"] for c in compare_to_baseline(self._report(), cur)}
        self.assertNotIn("app_switch.word.cold.host_wall_ms", metrics)

    def test_every_tracked_metric_path_is_reachable(self):
        """Guards against a typo'd dotted path silently dropping a metric."""
        rep = self._report()
        for path, label, _unit, _worse in TRACKED_METRICS:
            self.assertIsNotNone(dig(rep, path), f"{label}: unreachable path {path}")


class TestConsoleCapture(unittest.TestCase):
    """The HID console delivers fragments, not lines — reassembly must handle it."""

    def _runner(self):
        # PerfRunner's __init__ builds a TestRunner (GPIO/HID stubs above cover it);
        # only the console-buffer plumbing is exercised here.
        from station.perf_runner import PerfRunner
        r = PerfRunner(log=_quiet)
        r._console_lines = []
        r._console_pending = ""
        return r

    def _feed(self, runner, chunks):
        """Push raw HID chunks through the same sink the reader thread uses."""
        for chunk in chunks:
            buf = runner._console_pending + chunk
            parts = buf.split("\n")
            runner._console_pending = parts.pop()
            for line in parts:
                runner._keep_console_line(line)

    def test_line_split_across_reads_is_reassembled(self):
        """The exact failure from the first rig run: lines chopped mid-word.

        Previously each chunk was matched against the prefix list on its own, so
        continuations were dropped and the retained line ended at the chunk
        boundary (`ovltot wall=0ms b`)."""
        runner = self._runner()
        self._feed(runner, [
            "LoopProf: iters=781 ovl=8 worst=",
            "36ms(ovl br=12ms rn=166ms)\n",
            "  ovltot wall=261ms bridg",
            "e=12ms render=166ms rest=83ms\n",
        ])
        self.assertEqual(runner._console_lines, [
            "LoopProf: iters=781 ovl=8 worst=36ms(ovl br=12ms rn=166ms)",
            "  ovltot wall=261ms bridge=12ms render=166ms rest=83ms",
        ])

    def test_several_lines_in_one_read(self):
        runner = self._runner()
        self._feed(runner, ["LoopProf: a\n  norm  b\nSplit link: c\n"])
        self.assertEqual(runner._console_lines,
                         ["LoopProf: a", "  norm  b", "Split link: c"])

    def test_unrelated_console_chatter_is_dropped(self):
        runner = self._runner()
        self._feed(runner, ["LTR-559: lux=1\n", "some boot chatter\n"])
        self.assertEqual(runner._console_lines, [])

    def test_flush_emits_an_unterminated_trailing_line(self):
        """The final summary line may never get a newline before the reader stops."""
        runner = self._runner()
        self._feed(runner, ["LoopProf: iters=99 ovl=0 worst=1ms"])
        self.assertEqual(runner._console_lines, [])   # still buffered
        runner._flush_console()
        self.assertEqual(runner._console_lines, ["LoopProf: iters=99 ovl=0 worst=1ms"])

    def test_tail_is_bounded(self):
        runner = self._runner()
        self._feed(runner, [f"LoopProf: line {i}\n" for i in range(200)])
        from station.perf_runner import CONSOLE_TAIL_MAX
        self.assertEqual(len(runner._console_lines), CONSOLE_TAIL_MAX)
        self.assertEqual(runner._console_lines[-1], "LoopProf: line 199")


class TestMarkdown(unittest.TestCase):
    def test_reports_regressions_and_keeps_console_tail(self):
        rep = {
            "label": "split72",
            "device": {"fw": "0.9.83", "protocol": 11},
            "hid_latency": {"p50": 8.0},
            "console_tail": ["LoopProf: iters=1 ovl=0 worst=1ms(norm br=0ms rn=0ms)"],
        }
        cmp = compare_to_baseline(rep, {"hid_latency": {"p50": 4.0}})
        md = format_markdown(rep, cmp)
        self.assertIn("split72", md)
        self.assertIn("1 metric(s) regressed", md)
        self.assertIn("LoopProf:", md)

    def test_first_run_says_it_is_establishing_the_baseline(self):
        md = format_markdown({"label": "split72", "hid_latency": {"p50": 4.0}}, [])
        self.assertIn("No baseline recorded yet", md)


if __name__ == "__main__":
    unittest.main()
