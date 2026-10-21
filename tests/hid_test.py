# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for RawHID.send()'s kept handle.

send() keeps one hidraw handle open across calls (a new handle per call cost
~75 ms per request on the Pi and lost replies). These tests pin the four things
that change compared with a handle per call: one open for many requests, stale
reports drained before a write and never returned, a leftover handle reopened
without sending the request twice, and a read that fails after the write never
followed by a second write (a second cmd 43 would be a second reboot).
"""
import sys
import types
import unittest
from collections import deque

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import hid as station_hid  # noqa: E402
from station.config import HID_RAW_USAGE, HID_RAW_USAGE_PAGE  # noqa: E402


class FakeBoard:
    """The raw HID interface as hidraw shows it: every open handle gets its own
    queue, and a write makes the board answer on every open handle."""

    def __init__(self):
        self.present = True
        self.handles = []
        self.writes = []
        self.answer = True

    def enumerate(self, *_a, **_k):
        if not self.present:
            return []
        return [{"usage_page": HID_RAW_USAGE_PAGE, "usage": HID_RAW_USAGE, "path": b"/dev/hidraw0"}]

    def open(self, path):
        dev = FakeHandle(self)
        self.handles.append(dev)
        return dev

    def reply_to(self, report):
        self.writes.append(report)
        if self.answer:
            reply = bytes([report[1], report[2], ord(".")]) + bytes(61)
            for h in self.handles:
                if not h.closed:
                    h.queue.append(reply)


class FakeHandle:
    def __init__(self, board):
        self.board = board
        self.queue = deque()
        self.closed = False
        self.dead = False
        self.reset_on_next_write = False
        self.fail_writes = False
        self.late_after_write = None          # a report that arrives right after the next write

    def write(self, report):
        if self.dead or self.fail_writes:
            raise OSError("device disconnected")
        if self.reset_on_next_write:          # the request lands, then the board resets
            self.board.writes.append(report)
            self.dead = True
            return
        if self.late_after_write is not None:
            self.queue.append(self.late_after_write)
            self.late_after_write = None
        self.board.reply_to(report)

    def read(self, size, timeout=0):
        if self.dead:
            raise OSError("device disconnected")
        return self.queue.popleft() if self.queue else b""

    def close(self):
        self.closed = True


class RawHIDKeptHandleTest(unittest.TestCase):
    def setUp(self):
        self.board = FakeBoard()
        self._saved = (station_hid.hid.enumerate, station_hid.hid.Device)
        station_hid.hid.enumerate = self.board.enumerate
        station_hid.hid.Device = lambda path: self.board.open(path)
        self.raw = station_hid.RawHID()

    def tearDown(self):
        station_hid.hid.enumerate, station_hid.hid.Device = self._saved

    def test_many_requests_share_one_handle(self):
        for _ in range(5):
            self.assertEqual(self.raw.send(b"P\x06")[:3], b"P\x06.")
        self.assertEqual(self.raw.handle_opens, 1)

    def test_a_report_waiting_before_the_write_is_drained_not_returned(self):
        self.raw.send(b"P\x06")
        self.raw._dev.queue.append(b"P\x27late" + bytes(58))   # a late reply to an earlier request
        resp = self.raw.send(b"P\x16")
        self.assertEqual(resp[:3], b"P\x16.")
        self.assertEqual(self.raw.stale_replies, 1)

    def test_a_leftover_handle_is_reopened_and_the_request_sent_once(self):
        self.raw.send(b"P\x06")
        self.raw._dev.dead = True                              # the board re-enumerated
        resp = self.raw.send(b"P\x16")
        self.assertEqual(resp[:3], b"P\x16.")
        self.assertEqual(self.raw.handle_opens, 2)
        self.assertEqual([w[1:3] for w in self.board.writes].count(b"P\x16"), 1)

    def test_a_failed_write_on_a_readable_handle_reopens_and_sends_once(self):
        self.raw.send(b"P\x06")
        self.raw._dev.fail_writes = True                       # drain works, the write fails
        resp = self.raw.send(b"P\x16")
        self.assertEqual(resp[:3], b"P\x16.")
        self.assertEqual(self.raw.handle_opens, 2)
        self.assertEqual([w[1:3] for w in self.board.writes].count(b"P\x16"), 1)

    def test_a_late_reply_to_another_command_after_the_drain_is_skipped(self):
        self.raw.send(b"P\x06")
        self.raw._dev.late_after_write = b"P\x27." + bytes(61)  # arrives after the drain
        resp = self.raw.send(b"P\x16")
        self.assertEqual(resp[:3], b"P\x16.")
        self.assertEqual(self.raw.stale_replies, 1)

    def test_a_legacy_command_reply_is_not_checked(self):
        resp = self.raw.send(bytes([0x06]))                    # VIA keymap reset: replies 0x06 ...
        self.assertIsNotNone(resp)

    def test_a_missing_interface_raises_as_before(self):
        self.raw.send(b"P\x06")
        self.raw._dev.dead = True
        self.board.present = False
        with self.assertRaisesRegex(RuntimeError, "interface not found"):
            self.raw.send(b"P\x06")

    def test_a_read_that_fails_after_the_write_is_never_written_again(self):
        self.raw.send(b"P\x06")
        self.raw._dev.reset_on_next_write = True               # cmd 43: the board resets before replying
        with self.assertRaises(OSError):
            self.raw.send(b"P\x2b", attempts=3)
        self.assertEqual([w[1:3] for w in self.board.writes].count(b"P\x2b"), 1)
        self.assertIsNone(self.raw._dev)

    def test_a_lost_reply_is_retried_on_the_same_handle(self):
        self.board.answer = False
        self.assertIsNone(self.raw.send(b"P\x06", timeout_ms=1, attempts=3))
        self.assertEqual(len(self.board.writes), 3)
        self.assertEqual(self.raw.handle_opens, 1)
        self.assertEqual(self.raw.timeouts_failed, 1)
        self.assertEqual(self.raw.lost_replies, 3)

    def test_attempts_1_writes_once(self):
        self.board.answer = False
        self.assertIsNone(self.raw.send(b"P\x06", timeout_ms=1, attempts=1))
        self.assertEqual(len(self.board.writes), 1)


if __name__ == "__main__":
    unittest.main()
