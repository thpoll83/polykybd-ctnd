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


if __name__ == "__main__":
    unittest.main()
