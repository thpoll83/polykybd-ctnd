# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the HIL suite's non-idempotent-command handling.

The rig is not reachable from a development session, so the part pinned down
here is the one that a *lost HID reply* can corrupt: ``test_fresh_boot_marker``.

``GET_ID`` is the only command in the suite that is **not idempotent** — it
consumes the firmware's one-shot fresh-boot marker. ``RawHID.send()`` retries a
dropped reply by re-writing the request, which for GET_ID means the firmware has
already cleared the marker and the retry returns a correct-but-different status
('.' instead of '*'). That turns a transient lost reply into apparent *wrong
data*, which the runner reports as a real FAIL rather than the non-failing WARN a
timeout would produce.

``FakeMarkerDevice`` reproduces that firmware behaviour exactly — the marker is
cleared by the *write*, not by the reply reaching the host — so these tests fail
if the ``attempts=1`` pin is ever removed.
"""
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

from station import hil_tests  # noqa: E402
from station.hil_tests import (  # noqa: E402
    ACK,
    CMD_GET_ID,
    FONTPACK_COMMIT_LEGACY,
    FONTPACK_COMMIT_NO_SLAVE,
    FONTPACK_COMMIT_OK,
    FONTPACK_COMMIT_REJECTED,
    FRESH_BOOT,
    POLY_CHANNEL,
    describe_fontpack_commit,
    test_fresh_boot_marker,
)

IDENTITY = b"Split72 0.11.6 P12 HW0x0320 \x00"


class FakeMarkerDevice:
    """A master whose fresh-boot marker is consumed by the WRITE, not the read.

    ``drop_first_reply`` models the rig's transient USB hiccup: the firmware sees
    the request and clears the marker, but the reply never reaches the host.
    ``attempts`` is honoured exactly as ``RawHID.send()`` honours it, so a test
    that passes ``attempts=1`` gets ``None`` where the default would silently
    retry into a second, marker-less answer.
    """

    def __init__(self, drop_first_reply: bool = False):
        self._fresh = True
        self._drop_next = drop_first_reply
        self.writes = 0
        self.timeouts_recovered = 0
        self.timeouts_failed = 0

    def _reply(self) -> bytes:
        status = FRESH_BOOT if self._fresh else ACK
        self._fresh = False          # the marker is one-shot: cleared on receipt
        return bytes([POLY_CHANNEL, CMD_GET_ID, status]) + IDENTITY

    def send(self, data: bytes, timeout_ms: int = 3000, attempts: int = 3):
        for attempt in range(max(1, attempts)):
            self.writes += 1
            reply = self._reply()     # firmware acts on every write it receives
            if self._drop_next:
                self._drop_next = False
                continue              # reply lost in flight — marker already gone
            if attempt:
                self.timeouts_recovered += 1
            return reply
        self.timeouts_failed += 1
        return None


class TestFreshBootMarker(unittest.TestCase):

    def test_passes_on_a_clean_boot(self):
        """The happy path is unchanged: '*' then '.' across two GET_IDs."""
        dev = FakeMarkerDevice()
        self.assertTrue(test_fresh_boot_marker(dev, lambda _m: None))
        self.assertEqual(dev.timeouts_failed, 0)

    def test_dropped_first_reply_is_a_timeout_not_wrong_data(self):
        """A lost reply must surface as a timeout, never as a bogus '.' status.

        This is the regression: with send()'s default retry the firmware clears
        the marker on the dropped first write, the retry answers '.', and the
        test reports a *wrong value* — which the runner grades FAIL. Pinned to a
        single attempt it stays a timeout, which the runner grades WARN and the
        run stays green.
        """
        dev = FakeMarkerDevice(drop_first_reply=True)
        self.assertFalse(test_fresh_boot_marker(dev, lambda _m: None))
        self.assertEqual(dev.writes, 1, "the marker read must not be retried")
        self.assertEqual(dev.timeouts_failed, 1, "must register as a timeout")
        self.assertEqual(dev.timeouts_recovered, 0,
                         "a 'recovered' timeout here means the retry consumed the marker")

    def test_retrying_the_marker_read_would_report_wrong_data(self):
        """Demonstrates the bug the attempts=1 pin prevents.

        Driving the same fake with the default retry shows the failure mode: two
        writes, no recorded timeout, and a reply carrying ACK instead of the
        fresh-boot marker — indistinguishable, to the runner, from a firmware
        that never rebooted.
        """
        dev = FakeMarkerDevice(drop_first_reply=True)
        reply = dev.send(bytes([POLY_CHANNEL, CMD_GET_ID]))   # default attempts=3
        self.assertEqual(dev.writes, 2)
        self.assertEqual(dev.timeouts_failed, 0)
        self.assertEqual(dev.timeouts_recovered, 1)
        self.assertEqual(reply[2], ACK)
        self.assertNotEqual(reply[2], FRESH_BOOT)


class DescribeFontpackCommitTest(unittest.TestCase):
    """The FONTPACK_COMMIT status byte is three-valued since qmk#209.

    These pin the DIAGNOSIS, not the pass/fail gate — only '.' passes either way.
    They matter because 'R' and 'L' send an investigation in opposite directions:
    'R' is a data failure that re-sending cannot fix, 'L' is a split-link failure
    where the master's copy is already live. Reporting one as the other is the
    exact misdiagnosis #209 was raised to remove, and it cost two field rounds.
    """

    def _reply(self, status):
        return bytes([POLY_CHANNEL, 0x52, status])

    def test_the_three_statuses_read_differently(self):
        said = {
            s: describe_fontpack_commit(self._reply(s))
            for s in (FONTPACK_COMMIT_OK, FONTPACK_COMMIT_REJECTED,
                      FONTPACK_COMMIT_NO_SLAVE, FONTPACK_COMMIT_LEGACY)
        }
        self.assertEqual(len(set(said.values())), 4, f"not all distinct: {said}")

    def test_a_rejection_is_not_described_as_retryable(self):
        text = describe_fontpack_commit(self._reply(FONTPACK_COMMIT_REJECTED))
        self.assertIn("MASTER refused", text)
        self.assertNotIn("safe to retry", text)

    def test_a_lost_slave_ack_is_not_described_as_a_data_failure(self):
        text = describe_fontpack_commit(self._reply(FONTPACK_COMMIT_NO_SLAVE))
        self.assertIn("LINK", text)
        self.assertIn("retry", text)
        # The master's copy IS live — saying "rejected" here is the bug.
        self.assertNotIn("refused", text)

    def test_no_reply_is_not_silently_read_as_a_status(self):
        # An empty / short reply must not index past the end or read as byte 0.
        for reply in (None, b"", b"P", b"P\x52"):
            with self.subTest(reply=reply):
                self.assertIn("no reply", describe_fontpack_commit(reply))

    def test_an_unknown_byte_is_reported_rather_than_swallowed(self):
        text = describe_fontpack_commit(self._reply(0x7A))
        self.assertIn("unknown", text)
        self.assertIn("0x7a", text)


# --- the packers/encoders the new upload tests build reports with ------------
# Every one of these is verified THROUGH a re-implementation of the firmware's
# own decoder rather than by eye — the standing rule in this project after a
# hand-checked bit layout shipped wrong. The decoders below are transcribed from
# keyboards/polykybd/base/overlay.c (set_fragment_context_from_buffer) and
# fill_overlay.c (set_packed_overlay_mapping).

def decode_roi_header(buf: bytes) -> dict:
    """Firmware-side read of the 5-byte ROI header (base/overlay.c)."""
    return {
        "keycode": buf[0],
        "modifier": buf[1] & 0x0F,
        "y": (buf[2] & 0x03) | ((buf[1] >> 2) & 0x3C),
        "yy": buf[2] >> 2,
        "x": buf[3],
        "xx": buf[4] & 0x7F,
        "compressed": bool(buf[4] & 0x80),
    }


def unpack_mapping_values(buf: bytes, width: int, count: int) -> list:
    """Firmware-side read of `width`-bit packed mapping values (fill_overlay.c)."""
    out = []
    mask = (1 << width) - 1
    for i in range(count):
        b, s = divmod(i * width, 8)
        raw = buf[b]
        if b + 1 < len(buf):
            raw |= buf[b + 1] << 8
        if b + 2 < len(buf):
            raw |= buf[b + 2] << 16
        out.append((raw >> s) & mask)
    return out


def rle_decode_bits(stream: bytes) -> list:
    """Firmware-side read of the RLE stream: high bit = value, low 7 = run length."""
    bits = []
    for byte in stream:
        bits.extend([(byte >> 7) & 1] * (byte & 0x7F))
    return bits


class RoiHeaderTest(unittest.TestCase):
    """The ROI header packs a 6-bit y across two bytes; verify via the decoder."""

    def test_round_trips_through_the_firmware_decoder(self):
        for x, y, xx, yy in ((0, 0, 72, 13), (7, 5, 40, 39), (0, 39, 1, 40),
                             (71, 38, 72, 40)):
            got = decode_roi_header(hil_tests._roi_header(hil_tests.KC_A, 0, x, y, xx, yy))
            self.assertEqual((got["x"], got["y"], got["xx"], got["yy"]),
                             (x, y, xx, yy), f"region {(x, y, xx, yy)}")

    def test_the_y_split_across_two_bytes_does_not_corrupt_the_modifier(self):
        # y bits 2..5 ride in the same byte as the modifier nibble.
        for y in range(0, 64):
            got = decode_roi_header(hil_tests._roi_header(hil_tests.KC_A, 0x0F, 0, y, 72, 40))
            self.assertEqual(got["modifier"], 0x0F, f"y={y}")
            self.assertEqual(got["y"], y)

    def test_the_compressed_flag_is_separate_from_xx(self):
        plain = decode_roi_header(hil_tests._roi_header(hil_tests.KC_A, 0, 0, 0, 72, 40))
        comp = decode_roi_header(
            hil_tests._roi_header(hil_tests.KC_A, 0, 0, 0, 72, 40, compressed=True))
        self.assertFalse(plain["compressed"])
        self.assertTrue(comp["compressed"])
        self.assertEqual(plain["xx"], comp["xx"])

    def test_the_out_of_bounds_header_really_is_out_of_bounds(self):
        # If this ever encoded to something the firmware considers in-range, the
        # clamp branch it is meant to exercise would never run — a test that
        # passes without reaching the code it names.
        got = decode_roi_header(hil_tests._roi_header(hil_tests.KC_A, 0, 200, 60, 127, 63))
        self.assertGreater(got["x"], hil_tests.SCREEN_WIDTH)
        self.assertGreater(got["y"], hil_tests.SCREEN_HEIGHT)
        self.assertGreater(got["xx"], hil_tests.SCREEN_WIDTH)
        self.assertGreater(got["yy"], hil_tests.SCREEN_HEIGHT)


class TwoPacketOverlayTest(unittest.TestCase):
    """The compressed-overlay stream must genuinely need the cmd-17 continuation."""

    def test_the_stream_spans_exactly_two_packets(self):
        n = len(hil_tests._TWO_PACKET_OVERLAY_RLE)
        self.assertGreater(n, hil_tests.COMPRESSED_START,
                           "fits one packet — cmd 17 would never be exercised")
        self.assertLessEqual(n, hil_tests.COMPRESSED_START + hil_tests.COMPRESSED_MAX,
                             "needs a third packet the test does not send")

    def test_it_decodes_to_a_whole_overlay(self):
        bits = rle_decode_bits(hil_tests._TWO_PACKET_OVERLAY_RLE)
        self.assertEqual(len(bits), hil_tests.OVERLAY_BYTES * 8)

    def test_no_zero_length_run_is_emitted(self):
        # A 0 run byte would be a decoder hazard, and the encoder documents that
        # it never emits one.
        self.assertTrue(all(b & 0x7F for b in hil_tests._TWO_PACKET_OVERLAY_RLE))


class LinkSoakReportTest(unittest.TestCase):
    """The soak's mapping report must be in-range and OFF-SCREEN.

    In range because an out-of-pool ``to`` is an OOB read in the firmware's
    render path; off-screen because an on-screen ``from`` would make every one of
    the 450 reports request a display refresh, and the soak would then measure the
    renderer instead of the link.
    """

    def setUp(self):
        report = hil_tests._link_soak_report()
        self.assertEqual(len(report), 64)
        count = (hil_tests.HID_DATA_MAX * 8) // hil_tests.OVERLAY_MAP_IDX_BITS
        self.values = unpack_mapping_values(report[2:], hil_tests.OVERLAY_MAP_IDX_BITS,
                                            count)

    def test_every_from_is_addressable_and_off_screen(self):
        for v in self.values[0::2]:
            self.assertLess(v, hil_tests.OVERLAY_MAP_IDX_CNT)
            # >= 90 * 10: past the modifier variants a session can actually hold.
            self.assertGreaterEqual(v, 900)

    def test_every_to_is_inside_the_overlay_pool(self):
        for v in self.values[1::2]:
            self.assertLess(v, 600)   # NUM_OVERLAY_SLOTS


class SkipReasonConsoleGateTest(unittest.TestCase):
    def test_a_console_test_skips_when_the_console_did_not_come_up(self):
        reason = hil_tests.skip_reason({"needs_console": True}, {"console": False})
        self.assertIn("console", reason)

    def test_it_runs_when_the_console_is_up(self):
        self.assertIsNone(hil_tests.skip_reason({"needs_console": True},
                                                {"console": True}))

    def test_an_unknowing_caps_dict_runs_the_test_rather_than_skipping_it(self):
        # Same fail-open principle as the version gates: only a POSITIVE "not
        # available" skips, so an older runner (or a unit test) that never
        # reported console state does not silently drop coverage.
        self.assertIsNone(hil_tests.skip_reason({"needs_console": True}, {}))

    def test_it_composes_with_the_version_gates(self):
        reason = hil_tests.skip_reason({"needs_console": True, "min_protocol": 12},
                                       {"protocol": 4, "console": True})
        self.assertIn("protocol", reason)


class PercentileTest(unittest.TestCase):
    def test_median_and_edges(self):
        self.assertEqual(hil_tests._percentile([5, 1, 3], 50), 3)
        self.assertEqual(hil_tests._percentile([1, 2, 3, 4], 100), 4)
        self.assertEqual(hil_tests._percentile([9], 95), 9)

    def test_p95_is_not_dragged_down_by_the_bulk(self):
        values = [10.0] * 99 + [900.0]
        self.assertGreaterEqual(hil_tests._percentile(values, 95), 10.0)
        self.assertEqual(max(values), 900.0)


class SuiteTierGateTest(unittest.TestCase):
    """The extended tier is fail-CLOSED — the opposite of the version gates.

    A version gate declines to skip when it cannot tell (better to run and see a
    real failure than hide one). The tier gate must do the reverse: an extended
    test costs a chunk of every push's gate time, so it runs only when the run
    positively asked for it.
    """

    def test_extended_is_skipped_by_default(self):
        reason = hil_tests.skip_reason({"tier": hil_tests.TIER_EXTENDED},
                                       {"extended": False})
        self.assertIn("extended", reason)

    def test_extended_runs_when_requested(self):
        self.assertIsNone(hil_tests.skip_reason({"tier": hil_tests.TIER_EXTENDED},
                                                {"extended": True}))

    def test_a_caps_dict_that_never_heard_of_tiers_still_skips(self):
        # Fail-closed: an older runner that does not report the tier must not
        # silently start paying for the slow checks on every push.
        self.assertIn("extended",
                      hil_tests.skip_reason({"tier": hil_tests.TIER_EXTENDED}, {}))

    def test_default_tier_tests_are_unaffected(self):
        for test in ({}, {"tier": hil_tests.TIER_DEFAULT}):
            self.assertIsNone(hil_tests.skip_reason(test, {"extended": False}))

    def test_a_version_gate_still_wins_over_the_tier(self):
        # Order matters for the message: "your firmware is too old" is more
        # actionable than "you did not ask for the slow suite".
        reason = hil_tests.skip_reason(
            {"tier": hil_tests.TIER_EXTENDED, "min_protocol": 12},
            {"protocol": 4, "extended": True})
        self.assertIn("protocol", reason)

    def test_every_extended_test_is_actually_slow_by_nature(self):
        # Tier is about COST, not confidence. This pins the membership so a test
        # cannot be quietly demoted to a tier nobody runs to make it stop failing.
        names = {t["name"] for t in hil_tests.TESTS
                 if t.get("tier") == hil_tests.TIER_EXTENDED}
        self.assertEqual(names, {
            "replay startup animation (cmd 31)",
            "idle engages + Eden screensaver keeps HID alive (cmd 15/28)",
            "split link health under a bridged soak (cmd 21)",
        })

    def test_the_cheap_new_checks_stay_in_the_default_suite(self):
        # The two-packet and ROI uploads cost a report each; making them opt-in
        # would drop real coverage for no time saved.
        for name in ("compressed overlay spans two packets (cmd 16+17)",
                     "ROI overlay keeps master alive (cmd 18/19 + bounds clamp)"):
            test = next(t for t in hil_tests.TESTS if t["name"] == name)
            self.assertIsNone(test.get("tier"), name)


class CrashRecordTest(unittest.TestCase):
    """The console crash line is a failure whatever else passed; the two tests
    sit in the DEFAULT tier (a crash is never a slow check) and the scan runs
    after every other default-tier test so its window covers them all."""

    LINE = ("crash: side=master kind=hardfault core=0 pc=0x10012345 lr=0x1000abcd "
            "sp=0x20040ff0 psr=0x21000003 icsr=0x00000003 phase=3:0x0015 "
            "up=123456ms n=1 reason=0x22 fw=0.18.0")

    def test_no_lines_is_ok(self):
        ok, msg = hil_tests.classify_crash_lines([])
        self.assertTrue(ok)
        self.assertIn("no crash", msg)

    def test_any_crash_line_fails_and_is_quoted(self):
        ok, msg = hil_tests.classify_crash_lines(["   " + self.LINE])
        self.assertFalse(ok)
        self.assertIn("1 firmware crash", msg)
        self.assertIn("side=master", msg)

    def test_unrelated_lines_are_ignored(self):
        ok, _ = hil_tests.classify_crash_lines(["Split link: 1 tx", "boot ok"])
        self.assertTrue(ok)

    def test_scan_reads_the_shared_tap_from_the_session_mark(self):
        from station.console_log import TAP
        # A crash line left by a PREVIOUS run must not fail this one...
        TAP.feed("   " + self.LINE + "\n")
        hil_tests.begin_session()
        logged = []
        self.assertTrue(hil_tests.test_no_crash_record(None, logged.append))
        # ...while one printed inside this run (the slave's, here) does.
        TAP.feed("   " + self.LINE.replace("side=master", "side=slave") + "\n")
        logged = []
        self.assertFalse(hil_tests.test_no_crash_record(None, logged.append))
        self.assertTrue(any("side=slave" in ln for ln in logged))
        self.assertFalse(any("side=master" in ln for ln in logged))
        hil_tests.begin_session()

    def test_membership_gates_and_order(self):
        names = [t["name"] for t in hil_tests.TESTS]
        scan = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_no_crash_record)
        cmd = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_crash_record_command)
        self.assertTrue(scan.get("needs_console"))
        self.assertIsNone(scan.get("tier"))
        self.assertEqual(cmd.get("min_protocol"), 16)
        self.assertIsNone(cmd.get("tier"))
        # The scan is the LAST entry, so every test of every tier is in its window.
        self.assertEqual(names[-1], scan["name"])


class LayerNamesRetryTest(unittest.TestCase):
    """test_layer_names must ride out a deaf window but never retry a real fault.

    On qmk#236's first HIL run the master had multi-second deaf windows; the
    tests on either side of layer names each recovered a read timeout via
    ``send()``'s retry while layer names — the one retry-less
    ``send_and_read_all`` in that stretch — failed with "no reply". The reply is
    idempotent (no one-shot marker), so the exchange now retries when NOTHING
    arrives; a reply that arrives but fails validation is a protocol fault and
    must still fail on the first attempt.
    """

    NAMES = [b"Qwerty", b"Stag!", b"ColemkDH", b"Neo", b"Workman",
             b"Fn", b"Numpad", b"Utility"]

    class FakeDevice:
        def __init__(self, deaf_exchanges: int = 0, garble: bool = False):
            body = b"".join(n + b"\x00" for n in LayerNamesRetryTest.NAMES)
            payload = bytes([2 + len(body), len(LayerNamesRetryTest.NAMES)]) + body
            report = bytes([POLY_CHANNEL, hil_tests.CMD_GET_LAYER_NAMES, ACK]) + payload
            if garble:
                report = bytes([POLY_CHANNEL, hil_tests.CMD_GET_LAYER_NAMES, ord("!")]) + payload
            self._report = report.ljust(64, b"\x00")
            self._deaf = deaf_exchanges
            self.exchanges = 0

        def send_and_read_all(self, data, **kwargs):
            self.exchanges += 1
            if self._deaf > 0:
                self._deaf -= 1
                return []
            return [self._report]

        def send(self, data, timeout_ms: int = 3000, attempts: int = 3):
            # The layer-count cross-check (id_dynamic_keymap_get_layer_count).
            return bytes([hil_tests.VIA_DYNAMIC_KEYMAP_GET_LAYER_COUNT,
                          len(LayerNamesRetryTest.NAMES)]).ljust(32, b"\x00")

    def test_a_deaf_window_is_ridden_out(self):
        dev = self.FakeDevice(deaf_exchanges=1)
        self.assertTrue(hil_tests.test_layer_names(dev, lambda m: None))
        self.assertEqual(dev.exchanges, 2)

    def test_a_permanently_deaf_master_still_fails(self):
        dev = self.FakeDevice(deaf_exchanges=99)
        self.assertFalse(hil_tests.test_layer_names(dev, lambda m: None))
        self.assertEqual(dev.exchanges, 3)   # bounded — never an infinite ride

    def test_a_real_protocol_fault_fails_without_burning_retries(self):
        dev = self.FakeDevice(garble=True)
        self.assertFalse(hil_tests.test_layer_names(dev, lambda m: None))
        self.assertEqual(dev.exchanges, 1)


class DoomSlotFlashBeginTest(unittest.TestCase):
    """The doom-slot FONTPACK_BEGIN poll shares one loop for two very different
    waits: a cheap erase-busy ``~`` (~0.3 s) and an EXPENSIVE no-reply (~45 s
    inside raw.send on a dead board — 3 attempts x 15 s). The erase budget
    (DOOM_BEGIN_ERASE_ATTEMPTS) must ride out a long, progressing erase, but a
    dead board must fail after DOOM_BEGIN_NO_REPLY_MAX consecutive no-replies —
    NOT after the full erase budget, or one flash stalls ~45 min (Greptile,
    ctnd#81)."""

    class FakeDoomDevice:
        # begin_script: per-BEGIN reply status bytes; None = no reply (a dead
        # exchange). Once exhausted it repeats the last entry. CHUNK/COMMIT always
        # ACK, so a BEGIN that becomes ready flows through to a '.' COMMIT reply.
        def __init__(self, begin_script):
            self.begin_script = list(begin_script)
            self.begin_calls = 0

        def send(self, data, timeout_ms: int = 3000, attempts: int = 3):
            cmd = data[1]
            if cmd == hil_tests.CMD_FONTPACK_BEGIN:
                i = min(self.begin_calls, len(self.begin_script) - 1)
                self.begin_calls += 1
                status = self.begin_script[i]
                if status is None:
                    return None
                return bytes([POLY_CHANNEL, cmd, status]).ljust(64, b"\x00")
            return bytes([POLY_CHANNEL, cmd, ord('.')]).ljust(64, b"\x00")

    def setUp(self):
        # Patch out the 0.3 s inter-poll sleep so the long-erase case is instant.
        self._sleep = hil_tests.time.sleep
        hil_tests.time.sleep = lambda *a, **k: None

    def tearDown(self):
        hil_tests.time.sleep = self._sleep

    def _flash(self, begin_script):
        dev = self.FakeDoomDevice(begin_script)
        reply = hil_tests._doom_slot_flash(dev, lambda m: None,
                                           b"PlyX" + b"\x00" * 60,
                                           hil_tests.DOOMPACK_BUNDLE_ID)
        return dev, reply

    def test_a_dead_board_fails_after_the_no_reply_cap_not_the_erase_budget(self):
        dev, reply = self._flash([None] * 100)
        self.assertIsNone(reply)
        self.assertEqual(dev.begin_calls, hil_tests.DOOM_BEGIN_NO_REPLY_MAX)
        self.assertLess(dev.begin_calls, hil_tests.DOOM_BEGIN_ERASE_ATTEMPTS)

    def test_a_long_erase_is_ridden_out_past_the_no_reply_cap(self):
        script = [ord('~')] * (hil_tests.DOOM_BEGIN_ERASE_ATTEMPTS - 1) + [ord('.')]
        dev, reply = self._flash(script)
        self.assertTrue(reply and reply[2] == ord('.'))          # BEGIN ready -> COMMIT ACK
        self.assertEqual(dev.begin_calls, hil_tests.DOOM_BEGIN_ERASE_ATTEMPTS)
        self.assertGreater(dev.begin_calls, hil_tests.DOOM_BEGIN_NO_REPLY_MAX)

    def test_a_dropped_reply_between_erase_polls_resets_the_counter(self):
        # More total no-replies than the cap, but never MAX in a row — the `~`
        # progress resets the counter, so it must NOT give up. Without the reset
        # the accumulated no-replies would trip the cap and fail.
        gap = hil_tests.DOOM_BEGIN_NO_REPLY_MAX - 1
        script = ([None] * gap + [ord('~')]) * 4 + [ord('.')]
        dev, reply = self._flash(script)
        self.assertTrue(reply and reply[2] == ord('.'))
        self.assertGreater(dev.begin_calls, hil_tests.DOOM_BEGIN_NO_REPLY_MAX)


if __name__ == "__main__":
    unittest.main()
