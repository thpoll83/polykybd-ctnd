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


if __name__ == "__main__":
    unittest.main()
