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


def prc_parse_records(buf: bytes) -> list:
    """Port of the firmware's prc_parse_record() loop (base/prc_codec.c +
    receive_prc_overlay_report): records until a keycode of 0, too few bytes for a
    header, or a record the firmware refuses. Returns (fields, refused_at) where
    refused_at is the byte offset of a refused record, or None."""
    out, pos = [], 0
    while len(buf) - pos >= 6 and buf[pos] != 0:
        f = int.from_bytes(buf[pos + 1:pos + 6], "big")
        top, left = (f >> 30) & 0x3F, (f >> 23) & 0x7F
        h, w, n = ((f >> 17) & 0x3F) + 1, ((f >> 10) & 0x7F) + 1, (f >> 4) & 0x3F
        if f & 0x0F or top + h > 40 or left + w > 72 or n > len(buf) - pos - 6:
            return out, pos
        out.append((buf[pos], (f >> 36) & 0x0F, top, left, h, w, bytes(buf[pos + 6:pos + 6 + n])))
        pos += 6 + n
    return out, None


class PrcRecordTest(unittest.TestCase):
    """cmd 41 records as the rig builds them, checked against the firmware parser
    and against a record the HOST packed (PolyKybdHost tests/util/prc_codec_vectors.json)."""

    def test_matches_a_host_packed_record(self):
        # Vector "solid 10x10 block": keycode 6, modifier 6, host-packed bytes.
        rec = hil_tests._prc_record(6, 6, *hil_tests.PRC_SQUARE_BOX, hil_tests.PRC_SQUARE)
        self.assertEqual(rec.hex(), "06628f122460ffffe512e915")
        # Vector "8x8 checkerboard": keycode 7, modifier 9.
        rec = hil_tests._prc_record(7, 9, *hil_tests.PRC_CHECKER_BOX, hil_tests.PRC_CHECKER)
        self.assertEqual(rec[:6].hex(), "07941e0e1d50")

    def test_every_field_round_trips_through_the_parser(self):
        for kc, mod, top, left, h, w in [(4, 0, 0, 0, 1, 1), (0xE7, 15, 39, 71, 1, 1),
                                         (4, 5, 0, 0, 40, 72), (0x13, 8, 12, 33, 7, 21)]:
            payload = bytes(range(1, 11))
            recs, refused = prc_parse_records(hil_tests._prc_record(kc, mod, top, left, h, w, payload))
            self.assertIsNone(refused)
            self.assertEqual(recs, [(kc, mod, top, left, h, w, payload)])

    def test_the_two_record_report_parses_as_two_then_stops(self):
        report = hil_tests._prc_report(
            hil_tests._prc_record(hil_tests.KC_A, 0, *hil_tests.PRC_SQUARE_BOX, hil_tests.PRC_SQUARE),
            hil_tests._prc_record(hil_tests.KC_P, 0, *hil_tests.PRC_CHECKER_BOX, hil_tests.PRC_CHECKER))
        self.assertEqual(report[:2], bytes([ord("P"), 41]))
        # The HID layer zero-pads to 62 bytes; the firmware stops at the padding.
        recs, refused = prc_parse_records(report[2:].ljust(hil_tests.PRC_REPORT_BYTES, b"\0"))
        self.assertIsNone(refused)
        self.assertEqual([r[0] for r in recs], [hil_tests.KC_A, hil_tests.KC_P])

    def test_a_report_refuses_records_that_do_not_fit(self):
        rec = hil_tests._prc_record(4, 0, 0, 0, 40, 72, bytes(56))   # 62 bytes: fits exactly
        self.assertEqual(len(hil_tests._prc_report(rec)), 64)
        with self.assertRaises(ValueError):
            hil_tests._prc_report(rec, hil_tests._prc_record(4, 0, 0, 0, 1, 1, b""))

    def test_the_full_frame_record_is_accepted(self):
        recs, refused = prc_parse_records(hil_tests._prc_record(4, 0, 0, 0, 40, 72, b""))
        self.assertIsNone(refused)
        self.assertEqual(recs[0][4:6], (40, 72))

    def test_every_malformed_report_is_refused_at_its_first_record(self):
        labels = []
        for label, report in hil_tests._prc_malformed_reports():
            self.assertEqual(report[:2], bytes([ord("P"), 41]), label)
            recs, refused = prc_parse_records(report[2:].ljust(hil_tests.PRC_REPORT_BYTES, b"\0"))
            self.assertEqual((recs, refused), ([], 0), label)
            labels.append(label)
        self.assertEqual(len(labels), 3)

    def test_the_warning_pattern_matches_every_firmware_prc_warning(self):
        # Rendered forms of the three uprintf()s in fill_overlay.c.
        for line in ["Warning: malformed PRC record at byte 12; rest of the report dropped.",
                     "Warning: PRC overlay for unsupported keycode 0xe8 dropped.",
                     "Warning: PRC overlay for keycode 0x13 (idx 19) did not reach the slave."]:
            self.assertTrue(hil_tests.PRC_WARNING_RE.search(line), line)
        self.assertIsNone(hil_tests.PRC_WARNING_RE.search("Received overlay for keycode 0x4"))

    def test_both_tests_are_registered_and_gated_on_v19(self):
        by_name = {t["fn"].__name__: t for t in hil_tests.TESTS}
        alive = by_name["test_prc_overlay_keeps_master_alive"]
        refused = by_name["test_prc_malformed_record_is_refused"]
        self.assertEqual(alive["min_protocol"], 19)
        self.assertEqual(refused["min_protocol"], 19)
        self.assertTrue(refused.get("needs_console"))
        self.assertFalse(alive.get("needs_console", False))


class IconFillReportTest(unittest.TestCase):
    """cmd 42's report and verdict, pinned off the rig (the firmware's twin is
    base/tests/icon_lib_tests.cpp)."""

    def _values(self, report, width):
        bits = int.from_bytes(report[3:], "little")
        n = (hil_tests.OVERLAY_MAP_W_BYTES * 8 // width)
        return [(bits >> (i * width)) & ((1 << width) - 1) for i in range(n)]

    def test_the_header_names_the_command_and_the_width(self):
        r = hil_tests._icon_fill_report([(3, 7)], 9)
        self.assertEqual(r[:3], bytes([hil_tests.POLY_CHANNEL, 42, 9]))
        self.assertEqual(len(r), 64)

    def test_pairs_are_padded_by_repeating_the_last(self):
        for width in (8, 9, 10, 11):
            vals = self._values(hil_tests._icon_fill_report([(1, 2), (700, 5)], width), width)
            pairs = list(zip(vals[0::2], vals[1::2]))
            self.assertEqual(pairs[0], (1, 2 & ((1 << width) - 1)))
            if width >= 10:
                self.assertEqual(set(pairs[1:]), {(700, 5)})

    def test_the_verdict_is_parsed_and_anything_else_is_None(self):
        self.assertEqual(hil_tests._icon_fill_verdict(bytes([0x50, 42, ord("."), 0xFF]) + bytes(60)), (".", 0xFF))
        self.assertEqual(hil_tests._icon_fill_verdict(bytes([0x50, 42, ord("!"), 1]) + bytes(60)), ("!", 1))
        self.assertIsNone(hil_tests._icon_fill_verdict(None))
        self.assertIsNone(hil_tests._icon_fill_verdict(bytes([0x50, 41, ord("."), 0])))
        self.assertIsNone(hil_tests._icon_fill_verdict(bytes([0x50, 42, ord("?"), 0])))


class IconFillCleanupTest(unittest.TestCase):
    """The rig test must clear the pool slot it wrote, and a cleanup the keyboard
    does not ACK must fail it (Sourcery on ctnd#101)."""

    class _Raw:
        def __init__(self, cleanup_ok):
            self.cleanup_ok = cleanup_ok
            self.sent = []

        def send(self, data, *a, **k):
            self.sent.append(bytes(data))
            cmd = data[1]
            if cmd == hil_tests.CMD_GET_ID:
                return (b"P\x06.Split72 1.2.0 P20 HW1 \x00V\x09" + bytes(18)).ljust(64, b"\x00")
            if cmd == hil_tests.CMD_FILL_POOL_FROM_ICON:
                return bytes([0x50, cmd, ord("!"), 0]).ljust(64, b"\x00")
            if cmd == hil_tests.CMD_OVERLAY_FLAGS_ON:
                return bytes([0x50, cmd, ord(".") if self.cleanup_ok else ord("!")]).ljust(64, b"\x00")
            return None

    def test_passes_and_clears_the_pool(self):
        raw = self._Raw(cleanup_ok=True)
        self.assertTrue(hil_tests.test_icon_fill_replies(raw, lambda *_a: None))
        cleanup = [d for d in raw.sent if d[1] == hil_tests.CMD_OVERLAY_FLAGS_ON]
        self.assertEqual(len(cleanup), 1)
        self.assertTrue(cleanup[0][2] & (1 << 5), "RESET_BUFFERS clears the filled slot")

    def test_a_refused_cleanup_fails_the_test(self):
        self.assertFalse(hil_tests.test_icon_fill_replies(self._Raw(cleanup_ok=False),
                                                          lambda *_a: None))


class MappingFlagsTest(unittest.TestCase):
    """The v21 rig test against a model of each decoder: a v21 firmware masks the
    width byte with 0x1F, a v20 one reads it whole."""

    class _Raw:
        def __init__(self, mask, cleanup_ok=True, console=True):
            self.mask, self.cleanup_ok, self.console = mask, cleanup_ok, console
            self.sent = []

        def write_reports(self, reports):
            from station.console_log import TAP
            for r in reports:
                self.sent.append(bytes(r))
                width = r[2] & self.mask
                if self.console and not 8 <= width <= 16:
                    TAP.feed(f"{hil_tests.MAPPING_BAD_WIDTH_LINE} {width}\n")

        def send(self, data, *a, **k):
            self.sent.append(bytes(data))
            cmd = data[1]
            if cmd == hil_tests.CMD_GET_ID:
                return b"P\x06.Split72 1.3.0 P21 HW1 \x00".ljust(64, b"\x00")
            if cmd in (hil_tests.CMD_OVERLAY_FLAGS_ON, hil_tests.CMD_OVERLAY_FLAGS_OFF):
                return bytes([0x50, cmd, ord(".") if self.cleanup_ok else ord("!")]).ljust(64, b"\x00")
            return None

    def setUp(self):
        self._settle = hil_tests.PRC_SETTLE_S
        hil_tests.PRC_SETTLE_S = 0.2

    def tearDown(self):
        hil_tests.PRC_SETTLE_S = self._settle

    def _run(self, raw):
        return hil_tests.test_mapping_flags_ride_cmd_33(raw, lambda *_a: None)

    def test_a_v21_decoder_passes_and_cleans_up(self):
        raw = self._Raw(mask=0x1F)
        self.assertTrue(self._run(raw))
        flagged = next(r for r in raw.sent if r[1] == hil_tests.CMD_SEND_OVERLAY_MAPPING_W)
        self.assertEqual(flagged[2], 0x60 | 9)
        off = [r for r in raw.sent if r[1] == hil_tests.CMD_OVERLAY_FLAGS_OFF]
        self.assertEqual(len(off), 1)
        self.assertEqual(off[0][2], hil_tests.DISPLAY_OVERLAYS_BIT | hil_tests.MIRROR_OVERLAYS_BIT)

    def test_a_v20_decoder_fails(self):
        self.assertFalse(self._run(self._Raw(mask=0xFF)))

    def test_a_silent_console_fails_rather_than_passing(self):
        self.assertFalse(self._run(self._Raw(mask=0x1F, console=False)))

    def test_a_refused_cleanup_fails(self):
        self.assertFalse(self._run(self._Raw(mask=0x1F, cleanup_ok=False)))

    def test_entry_is_gated_on_v21_and_the_console(self):
        entry = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_mapping_flags_ride_cmd_33)
        self.assertEqual(entry["min_protocol"], 21)
        self.assertTrue(entry.get("needs_console"))

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
            # ~160 CHANGE_LANG switches, each an EEPROM write and a full redraw.
            "every language draws without a crash (cmd 9 sweep)",
            # 20 reboots by default, each a few seconds plus the slave-record wait.
            "boot loop (v22 cmd 43)",
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


    @staticmethod
    def _body(kind, phase, arg, flags=0x03):
        import struct
        rec = struct.pack(hil_tests._CRASH_REC_FMT, 0xC4A5C0DE, kind, 0, 1, 0x10,
                          0x10001234, 0x10005678, 0x20040FF0, 0x21000003, 0, 0,
                          phase, arg, b"0.29.3", 0)
        return bytes([flags]) + rec

    def test_record_is_48_bytes_like_the_firmware_struct(self):
        import struct
        self.assertEqual(1 + struct.calcsize(hil_tests._CRASH_REC_FMT),
                         hil_tests.CRASH_HID_BODY_LEN)

    def test_boot_watchdog_names_step_and_sub(self):
        msg = hil_tests.describe_crash_record(self._body(3, 1, 0x06E1))
        self.assertIn("kind=watchdog", msg)
        self.assertIn("phase=boot 6.0xE1", msg)
        self.assertIn("fw=0.29.3", msg)
        # Every register the record carries: the clear that follows destroys it.
        for want in ("pc=0x10001234", "lr=0x10005678", "sp=0x20040FF0", "xpsr=0x21000003"):
            self.assertIn(want, msg)

    def test_boot_core1_flag_is_not_part_of_the_step(self):
        # 0x15E2: step 5 logo draw with core1 in core1_entry() (bit 12).
        msg = hil_tests.describe_crash_record(self._body(3, 1, 0x15E2))
        self.assertIn("phase=boot 5.0xE2 core1=1", msg)
        msg = hil_tests.describe_crash_record(self._body(3, 1, 0x05E2))
        self.assertIn("phase=boot 5.0xE2 core1=0", msg)

    def test_non_boot_phase_prints_the_raw_argument(self):
        msg = hil_tests.describe_crash_record(self._body(1, 3, 0x0015))
        self.assertIn("kind=hardfault", msg)
        self.assertIn("phase=hid arg=0x0015", msg)

    def test_short_body_is_reported_not_raised(self):
        self.assertIn("too short", hil_tests.describe_crash_record(b"\x03\x00"))

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


class ClassifyHandLine(unittest.TestCase):
    """classify_hand_line() — the boot banner's handedness self-consistency check.

    Pure, so the whole gate is testable without a keyboard. The cases that matter
    are the DISAGREEMENTS: a source claiming a record while the descriptors say
    the sector is empty (or the reverse) is the "display and effect disagree" bug
    this firmware keeps producing, and on hardware it is invisible — handedness
    resolves to something either way and the board comes up looking fine.
    """

    def ok(self, line):
        return hil_tests.classify_hand_line([line])

    def test_a_stamped_half_is_accepted(self):
        ok, msg = self.ok("   hand: LEFT (flash stamp) slot=0/1 writer=0x55")
        self.assertTrue(ok, msg)
        self.assertIn("self-consistent", msg)

    def test_an_unstamped_half_is_accepted(self):
        ok, msg = self.ok("   hand: RIGHT (EEPROM, UNSTAMPED) slot=255/0 writer=0x00")
        self.assertTrue(ok, msg)

    def test_a_migrated_half_is_accepted(self):
        """After a migration the firmware re-scans, so the record it just wrote
        must be described — slot=255/0 here would be the pre-write scan leaking
        into the banner."""
        ok, msg = self.ok("   hand: LEFT (stamped from EEPROM) slot=0/1 writer=0x00")
        self.assertTrue(ok, msg)

    def test_a_source_claiming_a_record_the_sector_does_not_have_fails(self):
        ok, msg = self.ok("   hand: LEFT (flash stamp) slot=255/0 writer=0x00")
        self.assertFalse(ok)
        self.assertIn("outside the sector's 0..15 pages", msg)

    def test_a_zero_count_with_a_real_slot_fails(self):
        ok, msg = self.ok("   hand: LEFT (flash stamp) slot=3/0 writer=0x00")
        self.assertFalse(ok)
        self.assertIn("0 valid records", msg)

    def test_an_unstamped_source_claiming_a_record_fails(self):
        ok, msg = self.ok("   hand: RIGHT (EEPROM, UNSTAMPED) slot=0/1 writer=0x55")
        self.assertFalse(ok)
        self.assertIn("expected 255/0", msg)

    def test_an_unknown_writer_fails(self):
        """Only the firmware (0x00) and make_hand_uf2.py (0x55) write records; a
        third value means pad[0] is carrying something nobody wrote deliberately."""
        ok, msg = self.ok("   hand: LEFT (flash stamp) slot=0/1 writer=0xAA")
        self.assertFalse(ok)
        self.assertIn("neither firmware", msg)

    def test_the_eeprom_repair_suffix_is_accepted(self):
        """⚠️ The banner is NOT always the bare field list.

        `boot_diag.c` appends " [EEPROM byte repaired from the stamp]" whenever
        `poly_hand_ee_repaired()` is true — the stamp outranked the EEPROM byte
        and the firmware put the byte back. That is a real boot, seen on
        hardware, and it must PASS.

        This case exists because a review asked to anchor `_HAND_RE` with a bare
        `$`, which would have failed every repaired boot. Nothing covered the
        suffix at the time, so the suite would have gone green on a change that
        breaks a real state on the rig.
        """
        ok, msg = self.ok("   hand: LEFT (flash stamp) slot=0/1 writer=0x55"
                          " [EEPROM byte repaired from the stamp]")
        self.assertTrue(ok, msg)
        self.assertIn("self-consistent", msg)

    def test_trailing_garbage_after_the_writer_fails_to_parse(self):
        """The regex is anchored, so `writer=0x550` is not read as `0x55`.

        The firmware cannot emit it (`%02X` of a uint8 is exactly two chars), so
        this is a corrupt console line — which is precisely what a validator
        exists to reject rather than silently truncate.
        """
        ok, msg = self.ok("   hand: LEFT (flash stamp) slot=0/1 writer=0x550")
        self.assertFalse(ok)
        self.assertIn("does not parse", msg)

    def test_a_count_above_the_sector_capacity_fails(self):
        """STAMP_PAGES is 4096/256 = 16, so 17 valid records is impossible.

        Symmetrical with the slot bound: a descriptor that cannot exist must not
        pass as self-consistent merely for being non-zero.
        """
        ok, msg = self.ok("   hand: LEFT (flash stamp) slot=0/17 writer=0x00")
        self.assertFalse(ok)
        self.assertIn("at most 16", msg)

    def test_an_old_banner_without_the_fields_fails_to_parse(self):
        """Pre-0.27.4 firmware prints a bare line. The min_fw gate should SKIP
        such a run, but if it ever reaches here it must FAIL loudly rather than
        pass by finding nothing to check."""
        ok, msg = self.ok("   hand: LEFT (flash stamp)")
        self.assertFalse(ok)
        self.assertIn("does not parse", msg)

    def test_a_missing_line_fails(self):
        ok, msg = hil_tests.classify_hand_line([])
        self.assertFalse(ok)
        self.assertIn("did not report handedness", msg)

    def test_the_last_line_wins(self):
        """The tap spans the whole run, and the board reboots inside it (the
        reboot-persistence check power-cycles the master), so more than one
        banner can be present. The current boot is the last one."""
        ok, msg = hil_tests.classify_hand_line([
            "   hand: LEFT (EEPROM, UNSTAMPED) slot=255/0 writer=0x00",
            "   hand: LEFT (flash stamp) slot=0/1 writer=0x55",
        ])
        self.assertTrue(ok, msg)
        self.assertIn("slot=0/1", msg)


class LanguageSweepTest(unittest.TestCase):
    """The every-layout sweep: a reboot must read as a crash (the single-attempt
    GET_ID that sees '*'), a sweep that stopped early must fail, and a NEW crash
    record on either half must fail even when every switch looked fine."""

    CODES = ["enUS", "deDE", "hyAM", "kaGE", "roRO"]

    class _Board:
        """A master that switches language, answers GET_ID with its one-shot
        fresh-boot marker, and can crash, go silent or NACK on given codes."""

        def __init__(self, crash_on=(), silent_on=(), nack_on=(), unplugged_reads=0):
            import struct
            self.lang = "enUS"
            self.fresh = False
            self.crash_on, self.silent_on, self.nack_on = set(crash_on), set(silent_on), set(nack_on)
            self.silent = False
            self.unplugged = 0
            self.unplugged_reads = unplugged_reads
            self.switched = []
            self.get_id_attempts = []
            self.records = {0: bytes(hil_tests.CRASH_HID_BODY_LEN), 1: bytes(hil_tests.CRASH_HID_BODY_LEN)}
            rec = struct.pack(hil_tests._CRASH_REC_FMT, 0x504B4352, 1, 0, 1, 0x22,
                              0x10015A82, 0x10015A5F, 0x20040F00, 0x21000003, 3,
                              173066, 2, 0x9103, b"1.4.0\0\0\0", 0)
            self.crash_body = bytes([hil_tests.CRASH_HID_FLAG_PRESENT]) + rec

        def send(self, data, timeout_ms=3000, attempts=3):
            cmd = data[1]
            if self.unplugged:
                self.unplugged -= 1
                raise RuntimeError("QMK Raw HID interface not found")
            if self.silent:
                return None
            if cmd == hil_tests.CMD_CHANGE_LANG:
                code = bytes(data[2:6]).decode("ascii")
                if code in self.nack_on:
                    return bytes([POLY_CHANNEL, cmd, hil_tests.NACK])
                self.lang = code
                self.switched.append(code)
                if code in self.crash_on:   # crashes drawing it, reboots
                    self.fresh = True
                    self.unplugged = self.unplugged_reads
                    self.records[0] = self.crash_body
                if code in self.silent_on:
                    self.silent = True
                return bytes([POLY_CHANNEL, cmd, ACK])
            if cmd == CMD_GET_ID:
                self.get_id_attempts.append(attempts)
                status = FRESH_BOOT if self.fresh else ACK
                self.fresh = False
                return bytes([POLY_CHANNEL, cmd, status]) + IDENTITY
            if cmd == hil_tests.CMD_GET_LANG:
                return bytes([POLY_CHANNEL, cmd, ACK]) + self.lang.encode("ascii") + b"\0"
            if cmd == hil_tests.CMD_CRASH_RECORD:
                return bytes([POLY_CHANNEL, cmd, ACK]) + self.records[data[2]]
            raise AssertionError(f"unexpected cmd {cmd}")

    def sweep(self, board, codes=None):
        return hil_tests._sweep_languages(board, lambda *_a: None, codes or self.CODES,
                                          dwell_s=0, reboot_s=0)

    def test_clean_sweep_visits_every_language(self):
        board = self._Board()
        results = self.sweep(board)
        self.assertEqual([c for c, o, _ in results if o == "ok"], self.CODES)
        self.assertTrue(hil_tests.classify_language_sweep(results, len(self.CODES))[0])
        # The fresh-boot marker is one-shot: a retried GET_ID would hide a reboot.
        self.assertEqual(set(board.get_id_attempts), {1})

    def test_a_reboot_is_a_crash_and_stops_the_sweep(self):
        board = self._Board(crash_on={"hyAM"})
        results = self.sweep(board)
        self.assertEqual(results[-1][:2], ("hyAM", "rebooted"))
        self.assertNotIn("kaGE", board.switched)
        ok, msg = hil_tests.classify_language_sweep(results, len(self.CODES))
        self.assertFalse(ok)
        self.assertIn("hyAM rebooted", msg)
        self.assertIn("3/5", msg)

    def test_a_reboot_seen_through_re_enumeration_is_still_a_crash(self):
        # USB drops out while the master reboots; send() raises until it is back.
        board = self._Board(crash_on={"hyAM"}, unplugged_reads=2)
        results = hil_tests._sweep_languages(board, lambda *_a: None, self.CODES,
                                             dwell_s=0, reboot_s=5)
        self.assertEqual(results[-1][:2], ("hyAM", "rebooted"))

    def test_a_silent_master_is_no_reply(self):
        results = self.sweep(self._Board(silent_on={"kaGE"}))
        self.assertEqual(results[-1][:2], ("kaGE", "no-reply"))

    def test_a_nack_is_recorded_and_the_sweep_goes_on(self):
        results = self.sweep(self._Board(nack_on={"deDE"}))
        self.assertEqual([o for _, o, _ in results], ["ok", "nack", "ok", "ok", "ok"])
        ok, msg = hil_tests.classify_language_sweep(results, len(self.CODES))
        self.assertFalse(ok)
        self.assertIn("deDE nack", msg)

    def test_an_early_stop_or_an_empty_list_is_not_a_pass(self):
        ok_rows = [(c, "ok", "") for c in self.CODES[:2]]
        self.assertFalse(hil_tests.classify_language_sweep(ok_rows, 5)[0])
        self.assertFalse(hil_tests.classify_language_sweep([], 0)[0])

    def _run_test(self, board):
        from unittest import mock
        logged = []
        with mock.patch.object(hil_tests, "_read_packed_lang_codes", return_value=self.CODES), \
             mock.patch.object(hil_tests.time, "sleep"):
            ok = hil_tests.test_language_sweep(board, logged.append)
        return ok, logged

    def test_full_test_restores_the_language(self):
        board = self._Board()
        board.lang = "frFR"
        ok, _ = self._run_test(board)
        self.assertTrue(ok)
        self.assertEqual(board.lang, "frFR")

    def test_full_test_prints_the_new_crash_record(self):
        board = self._Board(crash_on={"hyAM"})
        ok, logged = self._run_test(board)
        self.assertFalse(ok)
        self.assertEqual(board.lang, "enUS")
        line = next(ln for ln in logged if "NEW crash record" in ln)
        self.assertIn("master", line)
        self.assertIn("pc=0x10015A82", line)

    def test_an_old_record_left_in_place_does_not_fail(self):
        board = self._Board()
        board.records[1] = board.crash_body     # archived before the sweep
        self.assertTrue(self._run_test(board)[0])

    def test_registered_extended_v16_before_the_console_scan(self):
        names = [t["fn"] for t in hil_tests.TESTS]
        entry = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_language_sweep)
        self.assertEqual(entry["tier"], hil_tests.TIER_EXTENDED)
        self.assertEqual(entry["min_protocol"], 16)
        self.assertLess(names.index(hil_tests.test_language_sweep),
                        names.index(hil_tests.test_no_crash_record))



class BootLoopTest(unittest.TestCase):
    """test_boot_loop (cmd 43 + cmd 39, v22) against a fake keyboard that reboots."""

    @staticmethod
    def _body(flags, kind=3, phase=1, arg=0x16E1):
        import struct
        rec = struct.pack(hil_tests._CRASH_REC_FMT, 0xC4A5C0DE, kind, 0, 1, 0x11,
                          0, 0, 0, 0, 0, 0, phase, arg, b"1.6.1", 0)
        return bytes([flags]) + rec

    class FakeBoard:
        """GET_ID answers '*' once after each reboot, after `gone` exchanges
        in which the interface is missing (RuntimeError)."""

        def __init__(self, crash_on=None, hang_on=None, slave_crash_on=None, gone=2,
                     ack_lost=False, slow=0, bad_status=0):
            self.crash_on, self.hang_on, self.slave_crash_on = crash_on, hang_on, slave_crash_on
            self.gone_per_boot = gone
            self.ack_lost = ack_lost          # the reboot happens, its ACK never arrives
            self.slow = slow                  # GET_ID read timeouts with the interface present
            self.bad_status = bad_status      # GET_ID answers '!' this many times first
            self.reboot_writes = 0
            self.reboots = 0
            self._gone = 0
            self._fresh = False
            self.master = None
            self.slave = None

        def send(self, data, timeout_ms=3000, attempts=3):
            cmd = data[1]
            if cmd == hil_tests.CMD_REBOOT:
                # send() re-writes the request once per attempt while no reply comes.
                self.reboot_writes += attempts if self.ack_lost else 1
                self.reboots += 1
                self._gone, self._fresh = self.gone_per_boot, True
                fresh = BootLoopTest._body(0x03)
                old = BootLoopTest._body(0x01)
                self.master = fresh if self.reboots == self.crash_on else (old if self.master else None)
                self.slave = fresh if self.reboots == self.slave_crash_on else None
                if self.ack_lost:
                    return None
                return bytes([POLY_CHANNEL, cmd, ACK]).ljust(64, b"\x00")
            if self.hang_on is not None and self.reboots >= self.hang_on:
                raise RuntimeError("interface not found")
            if self._gone:
                self._gone -= 1
                raise RuntimeError("interface not found")
            if cmd == hil_tests.CMD_GET_ID and self.slow:
                self.slow -= 1
                return None
            if cmd == hil_tests.CMD_GET_ID and self.bad_status:
                self.bad_status -= 1
                return bytes([POLY_CHANNEL, cmd, ord("!")]).ljust(64, b"\x00")
            if cmd == hil_tests.CMD_GET_ID:
                status = hil_tests.FRESH_BOOT if self._fresh else ACK
                self._fresh = False
                return bytes([POLY_CHANNEL, cmd, status]).ljust(64, b"\x00")
            if cmd == hil_tests.CMD_CRASH_RECORD:
                body = self.master if data[2] == 0 else self.slave
                body = body if body is not None else bytes(hil_tests.CRASH_HID_BODY_LEN)
                return (bytes([POLY_CHANNEL, cmd, ACK]) + body).ljust(64, b"\x00")
            return None

    def setUp(self):
        for name, value in (("BOOT_LOOP_RETURN_S", 0.3), ("BOOT_LOOP_SLAVE_WAIT_S", 0.0)):
            orig = getattr(hil_tests, name)
            setattr(hil_tests, name, value)
            self.addCleanup(setattr, hil_tests, name, orig)
        orig_sleep = hil_tests.time.sleep
        hil_tests.time.sleep = lambda s: None
        self.addCleanup(setattr, hil_tests.time, "sleep", orig_sleep)
        self.lines = []

    def _run(self, board, rounds=5):
        env = {"HIL_BOOT_LOOP_ROUNDS": str(rounds)}
        orig = hil_tests.os.environ
        hil_tests.os.environ = env
        try:
            return hil_tests.test_boot_loop(board, self.lines.append)
        finally:
            hil_tests.os.environ = orig

    def test_clean_rounds_pass_and_reboot_each_time(self):
        board = self.FakeBoard()
        self.assertTrue(self._run(board, 5))
        self.assertEqual(board.reboots, 5)
        self.assertIn("5 clean reboot(s)", "\n".join(self.lines))

    def test_a_fresh_master_record_fails_and_stops(self):
        board = self.FakeBoard(crash_on=3)
        self.assertFalse(self._run(board, 10))
        self.assertEqual(board.reboots, 3)
        out = "\n".join(self.lines)
        self.assertIn("round 3", out)
        self.assertIn("fresh master record", out)
        self.assertIn("phase=boot 6.0xE1 core1=1", out)

    def test_a_fresh_slave_record_fails(self):
        board = self.FakeBoard(slave_crash_on=2)
        self.assertFalse(self._run(board, 10))
        self.assertIn("fresh slave record", "\n".join(self.lines))

    def test_an_old_record_does_not_fail(self):
        board = self.FakeBoard()
        board.master = self._body(0x01)
        self.assertTrue(self._run(board, 3))

    def test_a_board_that_never_comes_back_fails(self):
        board = self.FakeBoard(hang_on=2)
        self.assertFalse(self._run(board, 5))
        self.assertIn("a boot hang the watchdog did not reset", "\n".join(self.lines))

    def test_a_master_that_never_went_away_fails(self):
        board = self.FakeBoard(gone=0)
        board._fresh = False
        orig = board.send

        def send(data, *a, **k):
            r = orig(data, *a, **k)
            if data[1] == hil_tests.CMD_REBOOT:
                board._fresh = False      # the '*' never shows
            return r
        board.send = send
        self.assertFalse(self._run(board, 2))
        self.assertIn("without ever going away", "\n".join(self.lines))

    def test_a_lost_marker_after_a_gap_still_counts_as_back(self):
        board = self.FakeBoard()
        orig = board.send

        def send(data, *a, **k):
            r = orig(data, *a, **k)
            if data[1] == hil_tests.CMD_REBOOT:
                board._fresh = False      # the '*' reply was lost to a read
            return r
        board.send = send
        self.assertTrue(self._run(board, 2))

    def test_a_lost_ack_does_not_resend_the_reboot(self):
        # send() re-writes on a timeout; a second cmd 43 is a second reboot.
        board = self.FakeBoard(ack_lost=True)
        self.assertTrue(self._run(board, 3))
        self.assertEqual(board.reboot_writes, 3)
        self.assertIn("no ACK to the reboot request", "\n".join(self.lines))

    def test_a_slow_reply_is_not_a_reboot(self):
        # A read timeout with the interface present, then '.': nothing went away.
        board = self.FakeBoard(gone=0, slow=2)
        orig = board.send

        def send(data, *a, **k):
            r = orig(data, *a, **k)
            if data[1] == hil_tests.CMD_REBOOT:
                board._fresh = False
            return r
        board.send = send
        self.assertFalse(self._run(board, 1))
        self.assertIn("without ever going away", "\n".join(self.lines))

    def test_an_unexpected_status_is_not_evidence(self):
        board = self.FakeBoard(bad_status=2)
        self.assertTrue(self._run(board, 1))
        self.assertIn("(fresh)", "\n".join(self.lines))

    def test_classifier(self):
        ok, _ = hil_tests.classify_boot_round(self._body(0x01), None)
        self.assertTrue(ok)
        self.assertFalse(hil_tests.classify_boot_round(None, None)[0])
        self.assertFalse(hil_tests.classify_boot_round(bytes(49), self._body(0x03))[0])

    def test_round_count_from_the_environment(self):
        self.assertEqual(hil_tests.boot_loop_rounds({}), 20)
        self.assertEqual(hil_tests.boot_loop_rounds({"HIL_BOOT_LOOP_ROUNDS": "7"}), 7)
        self.assertEqual(hil_tests.boot_loop_rounds({"HIL_BOOT_LOOP_ROUNDS": "500"}), 50)
        self.assertEqual(hil_tests.boot_loop_rounds({"HIL_BOOT_LOOP_ROUNDS": "0"}), 1)
        self.assertEqual(hil_tests.boot_loop_rounds({"HIL_BOOT_LOOP_ROUNDS": "x"}), 20)

    def test_it_is_extended_gated_on_v22_and_after_the_crash_record_test(self):
        entry = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_boot_loop)
        self.assertEqual(entry["tier"], hil_tests.TIER_EXTENDED)
        self.assertEqual(entry["min_protocol"], 22)
        fns = [t["fn"] for t in hil_tests.TESTS]
        self.assertLess(fns.index(hil_tests.test_crash_record_command),
                        fns.index(hil_tests.test_boot_loop))


if __name__ == "__main__":
    unittest.main()
