# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the macro HIL test's windowing arithmetic.

The rig is not reachable from a development session, so what is pinned here is the
part a wrong constant silently breaks: ``_macro_read`` / ``_macro_write`` walk the
shared body buffer in report-sized windows, and an offset that fails to advance
re-reads window 0 forever while still returning plausible-looking bytes.

``FakeMacroDevice`` re-implements the FIRMWARE's side of cmds 35/36/37 -- the
little-endian header, the clamped windows, the label stride, the out-of-range NACK --
so a round-trip through it is a round-trip through the decoder, not through a fixture
of what the decoder was assumed to do. That is the standing rule for anything on this
rig that packs bytes: mirror the firmware's decode, never check the packing by eye.
"""
import sys
import types
import unittest

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station.hil_tests import (  # noqa: E402
    ACK,
    CMD_MACRO_BODY,
    CMD_MACRO_INFO,
    CMD_MACRO_LABEL,
    NACK,
    POLY_CHANNEL,
    _macro_read,
    _macro_write,
    test_macro_round_trip,
)

CHUNK = 58          # 64-byte report minus the 6-byte header
REPORT = 64


class FakeMacroDevice:
    """The firmware's half of cmds 35/36/37, close enough to catch arithmetic.

    Deliberately models the awkward parts: the info header is little-endian across
    five bytes, a body window is clamped to what the report can carry, and a label is
    cut to the stride rather than refused.
    """

    COUNT = 16
    LABEL_LEN = 12
    CAPACITY = 2267

    def __init__(self):
        self.buf = bytearray(self.CAPACITY)
        self.labels = [""] * self.COUNT
        self.reads = 0
        self.writes = 0

    def send(self, data: bytes, timeout_ms: int = 3000, attempts: int = 3):
        cmd = data[1]
        if cmd == CMD_MACRO_INFO:
            used = 0
            for i, b in enumerate(self.buf):
                if b:
                    used = i + 2
            used = min(used, self.CAPACITY)
            return bytes([POLY_CHANNEL, cmd, ACK, self.COUNT, self.LABEL_LEN,
                          self.CAPACITY & 0xFF, self.CAPACITY >> 8,
                          used & 0xFF, used >> 8]).ljust(REPORT, b"\0")

        if cmd == CMD_MACRO_BODY:
            sub = data[2]
            off = data[3] | (data[4] << 8)
            want = min(data[5], CHUNK)
            if sub == 0:
                self.reads += 1
                got = self.buf[off:off + want]
                return (bytes([POLY_CHANNEL, cmd, ACK, len(got), 0, 0]) + bytes(got)
                        ).ljust(REPORT, b"\0")
            self.writes += 1
            payload = data[6:6 + want]
            self.buf[off:off + len(payload)] = payload
            return bytes([POLY_CHANNEL, cmd, ACK]).ljust(REPORT, b"\0")

        if cmd == CMD_MACRO_LABEL:
            macro_id, n = data[2], data[3]
            if macro_id >= self.COUNT:
                return bytes([POLY_CHANNEL, cmd, NACK]).ljust(REPORT, b"\0")
            if n != 0xFF:
                text = data[4:4 + n].decode("ascii", "replace")
                clean = "".join(c for c in text if 0x20 <= ord(c) <= 0x7E)
                self.labels[macro_id] = clean[:self.LABEL_LEN]
            stored = self.labels[macro_id].encode("ascii")
            return (bytes([POLY_CHANNEL, cmd, ACK, len(stored)]) + stored
                    ).ljust(REPORT, b"\0")

        return None


def _quiet(_msg):
    pass


class WindowingTest(unittest.TestCase):
    def setUp(self):
        self.dev = FakeMacroDevice()

    def test_a_single_window_round_trips(self):
        data = bytes(range(1, 21))
        self.assertTrue(_macro_write(self.dev, _quiet, 0, data, CHUNK))
        self.assertEqual(_macro_read(self.dev, _quiet, len(data), CHUNK), data)
        self.assertEqual(self.dev.writes, 1)

    def test_a_payload_longer_than_one_window_spans_reports(self):
        """The case that catches an offset which does not advance: window 0 returned
        twice still looks like data unless the pattern is non-repeating."""
        data = bytes((0x41 + (i % 26)) for i in range(CHUNK + 20))
        self.assertTrue(_macro_write(self.dev, _quiet, 0, data, CHUNK))
        self.assertEqual(self.dev.writes, 2)
        self.assertEqual(_macro_read(self.dev, _quiet, len(data), CHUNK), data)
        self.assertEqual(self.dev.reads, 2)

    def test_writes_land_at_the_offset_they_name(self):
        self.assertTrue(_macro_write(self.dev, _quiet, 100, b"XYZ", CHUNK))
        self.assertEqual(bytes(self.dev.buf[100:103]), b"XYZ")
        self.assertEqual(self.dev.buf[99], 0)

    def test_a_read_of_exactly_one_window_uses_one_report(self):
        """An off-by-one in the loop bound would spend a second, empty report."""
        _macro_write(self.dev, _quiet, 0, bytes(CHUNK), CHUNK)
        self.dev.reads = 0
        self.assertEqual(len(_macro_read(self.dev, _quiet, CHUNK, CHUNK)), CHUNK)
        self.assertEqual(self.dev.reads, 1)

    def test_a_zero_length_reply_stops_instead_of_spinning(self):
        """It leaves the offset where it was, so a naive loop never terminates."""
        class Stuck(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                if data[1] == CMD_MACRO_BODY and data[2] == 0:
                    return bytes([POLY_CHANNEL, CMD_MACRO_BODY, ACK, 0, 0, 0]
                                 ).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        self.assertIsNone(_macro_read(Stuck(), _quiet, 128, CHUNK))

    def test_a_nacked_read_is_not_treated_as_data(self):
        class Refusing(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                if data[1] == CMD_MACRO_BODY:
                    return bytes([POLY_CHANNEL, CMD_MACRO_BODY, NACK]).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        self.assertIsNone(_macro_read(Refusing(), _quiet, 32, CHUNK))


class RoundTripTest(unittest.TestCase):
    """The whole HIL test, driven against the fake firmware."""

    def test_it_passes_against_a_correct_device(self):
        self.assertTrue(test_macro_round_trip(FakeMacroDevice(), _quiet))

    def test_it_restores_what_it_found(self):
        dev = FakeMacroDevice()
        dev.buf[0:5] = b"hello"
        dev.labels[0] = "keepme"
        before = bytes(dev.buf)
        self.assertTrue(test_macro_round_trip(dev, _quiet))
        self.assertEqual(bytes(dev.buf), before)
        self.assertEqual(dev.labels[0], "keepme")

    def test_it_restores_even_when_it_fails_midway(self):
        """The restore is in a finally, so a failure must not leave the rig rewritten."""
        class BadLabel(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                # Refuse a label WRITE (not the query), after the body has been rewritten.
                if data[1] == CMD_MACRO_LABEL and data[3] != 0xFF and data[3] != len("keepme"):
                    return bytes([POLY_CHANNEL, CMD_MACRO_LABEL, NACK]).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        dev = BadLabel()
        dev.buf[0:5] = b"hello"
        dev.labels[0] = "keepme"
        before = bytes(dev.buf)
        self.assertFalse(test_macro_round_trip(dev, _quiet))
        self.assertEqual(bytes(dev.buf), before)

    def test_a_byte_swapped_capacity_is_caught(self):
        """The header is little-endian; reading it big-endian turns 2267 into 56066.
        That number sizes the host's storage bar, so it must not pass silently."""
        class Swapped(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                r = bytearray(super().send(data, timeout_ms, attempts))
                if data[1] == CMD_MACRO_INFO:
                    r[5], r[6] = r[6], r[5]
                return bytes(r)

        self.assertFalse(test_macro_round_trip(Swapped(), _quiet))

    def test_a_device_that_never_advances_is_caught(self):
        """Every read returns window 0. A repeating probe pattern would not notice."""
        class Stuck(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                if data[1] == CMD_MACRO_BODY and data[2] == 0:
                    d = bytearray(data)
                    d[3] = d[4] = 0
                    return super().send(bytes(d), timeout_ms, attempts)
                return super().send(data, timeout_ms, attempts)

        self.assertFalse(test_macro_round_trip(Stuck(), _quiet))

    def test_a_device_accepting_an_out_of_range_id_is_caught(self):
        class Lax(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                if data[1] == CMD_MACRO_LABEL and data[2] >= self.COUNT:
                    return bytes([POLY_CHANNEL, CMD_MACRO_LABEL, ACK, 0]).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        self.assertFalse(test_macro_round_trip(Lax(), _quiet))

    def test_a_device_that_does_not_truncate_a_long_label_is_caught(self):
        class NoTruncate(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                if data[1] == CMD_MACRO_LABEL and data[3] != 0xFF:
                    text = bytes(data[4:4 + data[3]])
                    self.labels[data[2]] = text.decode("ascii", "replace")  # no cut
                    return (bytes([POLY_CHANNEL, CMD_MACRO_LABEL, ACK, len(text)]) + text
                            ).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        self.assertFalse(test_macro_round_trip(NoTruncate(), _quiet))

    def test_a_device_that_keeps_undrawable_label_bytes_is_caught(self):
        """The firmware's label_store() drops anything outside 0x20..0x7E, because a
        codepoint the _Nano_ face cannot draw is indistinguishable from a bug once it
        is on a keycap. A device that stores the raw bytes must not pass."""
        class KeepsRaw(FakeMacroDevice):
            def send(self, data, timeout_ms=3000, attempts=3):
                if data[1] == CMD_MACRO_LABEL and data[3] != 0xFF:
                    raw = bytes(data[4:4 + data[3]])[:self.LABEL_LEN]
                    self.labels[data[2]] = raw.decode("latin-1")
                    return (bytes([POLY_CHANNEL, CMD_MACRO_LABEL, ACK, len(raw)]) + raw
                            ).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        self.assertFalse(test_macro_round_trip(KeepsRaw(), _quiet))

    def test_a_failed_body_restore_fails_the_test(self):
        """The rig writes to real persistent macro storage. A restore that is refused
        after the assertions passed leaves someone's macros overwritten, so it has to
        turn the verdict red rather than be logged as cleanup."""
        class RefusesRestore(FakeMacroDevice):
            def __init__(self):
                super().__init__()
                self.starts = 0

            def send(self, data, timeout_ms=3000, attempts=3):
                if data[1] == CMD_MACRO_BODY and data[2] == 1:
                    # A write run starts at offset 0 and walks forward in windows.
                    # Run 1 is the test's own pattern and must go through in FULL --
                    # refusing its second window would fail the test for the wrong
                    # reason and this case would pass vacuously. Run 2 is the restore.
                    if data[3] == 0 and data[4] == 0:
                        self.starts += 1
                    if self.starts >= 2:
                        return bytes([POLY_CHANNEL, CMD_MACRO_BODY, NACK]).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        dev = RefusesRestore()
        self.assertFalse(test_macro_round_trip(dev, _quiet))
        self.assertGreaterEqual(dev.starts, 2, "the restore never ran; test is vacuous")

    def test_a_failed_label_restore_fails_the_test(self):
        class RefusesLabelRestore(FakeMacroDevice):
            def __init__(self):
                super().__init__()
                self.restores = 0

            def send(self, data, timeout_ms=3000, attempts=3):
                # Discriminate on CONTENT, not on a call count: a fresh fake's saved
                # label is empty, so the zero-length set IS the restore, while every
                # set the test itself makes carries text. Counting calls would make
                # this case fail for the wrong reason the moment a label assertion is
                # added or removed -- which is exactly what happened to its sibling.
                if data[1] == CMD_MACRO_LABEL and data[3] == 0:
                    self.restores += 1
                    return bytes([POLY_CHANNEL, CMD_MACRO_LABEL, NACK]).ljust(REPORT, b"\0")
                return super().send(data, timeout_ms, attempts)

        dev = RefusesLabelRestore()
        self.assertFalse(test_macro_round_trip(dev, _quiet))
        self.assertEqual(dev.restores, 1, "the label restore never ran; test is vacuous")


if __name__ == "__main__":
    unittest.main()
