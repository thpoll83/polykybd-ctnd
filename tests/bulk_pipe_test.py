# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the bulk pipe check (no hardware, no tty).

* framing against the firmware's layout (poly_pipe.h): magic, little-endian
  length, zlib.crc32 over cmd+len+payload, reply = cmd | 0x80;
* the sysfs port search and the SKIP reasons (absent port, unwritable port);
* ``measure`` against a fake firmware that answers over injected read/write,
  including a keyboard that drops a frame (must FAIL on the count/CRC);
* the ``skip_if`` gate in ``skip_reason`` and the runner's gate condition.
"""
import os
import struct
import sys
import tempfile
import types
import unittest
import zlib

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import bulk_pipe as bp                     # noqa: E402
from station.hil_tests import TESTS, skip_reason         # noqa: E402


class FramingTest(unittest.TestCase):
    def test_layout(self):
        f = bp.pack_frame(bp.CMD_ECHO, b"ab")
        self.assertEqual(f[:2], b"PB")
        self.assertEqual(f[2], bp.CMD_ECHO)
        self.assertEqual(struct.unpack_from("<H", f, 3)[0], 2)
        self.assertEqual(f[5:7], b"ab")
        self.assertEqual(struct.unpack_from("<I", f, 7)[0],
                         zlib.crc32(bytes([bp.CMD_ECHO, 2, 0, 0x61, 0x62])) & 0xFFFFFFFF)

    def test_parser_split_and_noise(self):
        reply = bp.pack_frame(bp.CMD_STAT | bp.REPLY, bytes(28))
        p = bp.FrameParser()
        self.assertEqual(p.feed(b"AT\r\n" + reply[:7]), [])
        self.assertEqual(p.feed(reply[7:]), [(bp.CMD_STAT | bp.REPLY, bytes(28))])
        self.assertEqual(p.dropped, 4)

    def test_parser_rejects_bad_crc(self):
        good = bp.pack_frame(bp.CMD_RESET | bp.REPLY)
        bad = bytearray(good)
        bad[-2] ^= 1
        self.assertEqual(bp.FrameParser().feed(bytes(bad) + good), [(bp.CMD_RESET | bp.REPLY, b"")])

    def test_parse_stat_and_ping(self):
        st = bp.parse_stat(struct.pack("<7I", 1, 2, 3, 4, 5, 6, 7))
        self.assertEqual((st["sink_bytes"], st["last_ms"]), (1, 7))
        with self.assertRaises(ValueError):
            bp.parse_stat(bytes(27))
        ping = bp.parse_ping(struct.pack("<BHBB", 1, 512, 64, 8) + b"1.9.3\0junk")
        self.assertEqual(ping, {"proto": 1, "max_payload": 512, "epsize": 64,
                                "out_capacity": 8, "fw": "1.9.3"})


class PortSearchTest(unittest.TestCase):
    def _tree(self, root, name, vid):
        dev = os.path.join(root, "devs", name)
        os.makedirs(os.path.join(dev, "iface"))
        with open(os.path.join(dev, "idVendor"), "w") as f:
            f.write(vid + "\n")
        os.makedirs(os.path.join(root, "tty"), exist_ok=True)
        os.symlink(os.path.join(dev, "iface"), os.path.join(root, "tty", name, "device")
                   if os.path.isdir(os.path.join(root, "tty", name)) else self._mk(root, name))

    def _mk(self, root, name):
        os.makedirs(os.path.join(root, "tty", name))
        return os.path.join(root, "tty", name, "device")

    def test_finds_keyboard_not_other_acm(self):
        with tempfile.TemporaryDirectory() as root:
            self._tree(root, "ttyACM0", "2e8a")   # a Pico, not the keyboard
            self._tree(root, "ttyACM1", "2021")
            self.assertEqual(bp.find_port(os.path.join(root, "tty")), "/dev/ttyACM1")

    def test_none_when_absent(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "tty"))
            self.assertIsNone(bp.find_port(os.path.join(root, "tty")))

    def test_skip_reasons(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "tty"))
            self.assertIn("without POLYKYBD_BULK_PIPE", bp.skip_reason(os.path.join(root, "tty")))
            self._tree(root, "ttyACM0", "2021")
            tty = os.path.join(root, "tty")
            self.assertIn("setup.sh", bp.skip_reason(tty, access=lambda *_a: False))
            self.assertIsNone(bp.skip_reason(tty, access=lambda *_a: True))


class FakeFirmware:
    """Answers the pipe commands the way poly_pipe.c does; ``drop`` loses one SINK frame."""

    def __init__(self, drop_frame=None, proto=2, image_size=None, drop_fw_frames=(),
                 reject_fw_frames=()):
        self.parser = bp.FrameParser()
        self.out = bytearray()
        self.sink_bytes = 0
        self.frames_ok = 0
        self.crc = 0
        self.first_ms = 0
        self.last_ms = 0
        self.sink_seen = 0
        self.drop_frame = drop_frame
        self.proto = proto
        # FW_CHUNK: the staging cursor, like fw_staging_write_chunk: a frame at the
        # cursor is written, one behind it is a duplicate (ACK, no write), anything
        # else is NACKed with the cursor. drop_fw_frames never answer (a bad CRC on
        # the wire), reject_fw_frames are NACKed (a relay failure).
        self.image_size = image_size
        self.cursor = 0
        self.staged = bytearray()
        self.fw_frames = 0
        self.drop_fw_frames = set(drop_fw_frames)
        self.reject_fw_frames = set(reject_fw_frames)

    def write(self, fd, data):
        for cmd, payload in self.parser.feed(bytes(data)):
            self.frames_ok += 1
            if cmd == bp.CMD_PING:
                self.out += bp.pack_frame(cmd | bp.REPLY,
                                          struct.pack("<BHBB", self.proto, 512, 64, 8) + b"1.9.3\0")
            elif cmd == bp.CMD_FW_CHUNK:
                self.fw_frames += 1
                if self.fw_frames in self.drop_fw_frames:
                    continue
                acked = False
                if len(payload) >= 5 and len(payload) - 4 <= bp.FW_CHUNK_MAX \
                        and self.fw_frames not in self.reject_fw_frames:
                    offset = struct.unpack_from("<I", payload)[0]
                    body = payload[4:]
                    if offset < self.cursor:
                        acked = True
                    elif offset == self.cursor:
                        if self.image_size is not None:
                            body = body[:self.image_size - offset]
                        self.staged += body
                        self.cursor += len(body)
                        acked = True
                self.out += bp.pack_frame(cmd | bp.REPLY, (b"." if acked else b"!")
                                          + struct.pack("<I", self.cursor))
            elif cmd == bp.CMD_RESET:
                self.sink_bytes = self.crc = self.first_ms = self.last_ms = 0
                self.out += bp.pack_frame(cmd | bp.REPLY)
            elif cmd == bp.CMD_SINK:
                self.sink_seen += 1
                if self.sink_seen == self.drop_frame:
                    continue
                if not self.first_ms:
                    self.first_ms = 100
                self.last_ms = self.first_ms + 10 * self.sink_seen
                self.sink_bytes += len(payload)
                self.crc = zlib.crc32(payload, self.crc) & 0xFFFFFFFF
            elif cmd == bp.CMD_STAT:
                self.out += bp.pack_frame(cmd | bp.REPLY, struct.pack(
                    "<7I", self.sink_bytes, self.frames_ok, 0, 0, self.crc, self.first_ms, self.last_ms))
        return len(data)

    def read(self, fd, n):
        chunk, self.out = bytes(self.out[:n]), self.out[n:]
        return chunk


class MeasureTest(unittest.TestCase):
    def test_intact_stream_passes(self):
        fw = FakeFirmware()
        pipe = bp.Pipe(0, read=fw.read, write=fw.write)
        lines = []
        r = bp.measure(pipe, 40_000, 4096, lines.append)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["payload"], 9 * 4096)
        self.assertEqual(r["stats"]["sink_bytes"], 9 * 4096)
        self.assertTrue(any("keyboard clock" in ln for ln in lines))

    def test_dropped_frame_fails(self):
        fw = FakeFirmware(drop_frame=3)
        pipe = bp.Pipe(0, read=fw.read, write=fw.write)
        r = bp.measure(pipe, 40_000, 4096, lambda _l: None)
        self.assertFalse(r["ok"])
        self.assertNotEqual(r["stats"]["sink_crc"], r["expect_crc"])

    def test_silent_keyboard_reports_error(self):
        t = [0.0]

        def clock():
            t[0] += 1.0
            return t[0]
        pipe = bp.Pipe(0, read=lambda fd, n: b"", write=lambda fd, d: len(d), clock=clock)
        r = bp.measure(pipe, 4096, 4096, lambda _l: None)
        self.assertEqual(r, {"ok": False, "error": "no PING reply"})


class GateTest(unittest.TestCase):
    def test_skip_if_reason_wins_and_none_runs(self):
        self.assertEqual(skip_reason({"skip_if": lambda: "no port"}, {}), "no port")
        self.assertIsNone(skip_reason({"skip_if": lambda: None}, {}))

    def test_registered_with_gate(self):
        entries = [t for t in TESTS if t["fn"].__name__ == "test_bulk_pipe_soak"]
        self.assertEqual(len(entries), 1)
        self.assertIs(entries[0]["skip_if"], bp.skip_reason)

    def test_runner_evaluates_skip_if(self):
        # The runner only consults skip_reason for tests carrying a gate key;
        # read the condition rather than trusting it.
        src = open(os.path.join(os.path.dirname(__file__), "..", "station", "test_runner.py")).read()
        self.assertIn('or test.get("skip_if")', src)


class FirmwareStreamTest(unittest.TestCase):
    """``stream_firmware`` against the fake firmware: whole image in 236-byte
    frames, and the recovery paths (a lost frame, a rejected one, a dead keyboard)."""

    IMAGE = bytes((i * 31 + 7) & 0xFF for i in range(236 * 9 + 100))

    def _pipe(self, fw, clock=None):
        kw = {"clock": clock} if clock else {}
        return bp.Pipe(0, read=fw.read, write=fw.write, **kw)

    def test_whole_image(self):
        fw = FakeFirmware(image_size=len(self.IMAGE))
        ok, offset, why = bp.stream_firmware(self._pipe(fw), self.IMAGE)
        self.assertEqual((ok, offset, why), (True, len(self.IMAGE), None))
        self.assertEqual(bytes(fw.staged), self.IMAGE)
        self.assertEqual(fw.fw_frames, 10)

    def test_lost_frame_recovers(self):
        t = [0.0]

        def clock():
            t[0] += 0.01
            return t[0]

        fw = FakeFirmware(image_size=len(self.IMAGE), drop_fw_frames={3})
        ok, offset, why = bp.stream_firmware(self._pipe(fw, clock), self.IMAGE)
        self.assertEqual((ok, offset, why), (True, len(self.IMAGE), None))
        self.assertEqual(bytes(fw.staged), self.IMAGE)

    def test_rejected_frame_recovers(self):
        fw = FakeFirmware(image_size=len(self.IMAGE), reject_fw_frames={2})
        ok, offset, why = bp.stream_firmware(self._pipe(fw), self.IMAGE)
        self.assertEqual((ok, offset, why), (True, len(self.IMAGE), None))
        self.assertEqual(bytes(fw.staged), self.IMAGE)

    def test_dead_keyboard_reports_cursor(self):
        t = [0.0]

        def clock():
            t[0] += 0.5
            return t[0]

        fw = FakeFirmware(image_size=len(self.IMAGE), drop_fw_frames=set(range(3, 500)))
        ok, offset, why = bp.stream_firmware(self._pipe(fw, clock), self.IMAGE)
        self.assertEqual((ok, offset, why), (False, 2 * 236, "no reply"))

    def test_fw_chunk_gate(self):
        with tempfile.TemporaryDirectory() as d:
            tty = os.path.join(d, "ttyACM0")
            os.makedirs(os.path.join(tty, "device"))
            with open(os.path.join(tty, "idVendor"), "w") as f:
                f.write("2021\n")
            # os.path.join(tty, "device", "..", "idVendor") resolves to tty/idVendor
            fw_old = FakeFirmware(proto=1)
            fw_new = FakeFirmware(proto=2)
            self.assertIn("FW_CHUNK needs 2", bp.fw_chunk_skip_reason(
                d, access=lambda *a: True, open_pipe=lambda p: (self._pipe(fw_old), lambda: None)))
            self.assertIsNone(bp.fw_chunk_skip_reason(
                d, access=lambda *a: True, open_pipe=lambda p: (self._pipe(fw_new), lambda: None)))
            self.assertIn("not writable", bp.fw_chunk_skip_reason(d, access=lambda *a: False))

    def test_fw_chunk_gate_returns_reason_when_open_raises(self):
        # The gate does I/O; an unopenable port is a SKIP reason, not an exception.
        with tempfile.TemporaryDirectory() as d:
            tty = os.path.join(d, "ttyACM0")
            os.makedirs(os.path.join(tty, "device"))
            with open(os.path.join(tty, "idVendor"), "w") as f:
                f.write("2021\n")

            def open_pipe(p):
                raise OSError(16, "Device or resource busy")
            reason = bp.fw_chunk_skip_reason(d, access=lambda *a: True, open_pipe=open_pipe)
            self.assertIsInstance(reason, str)
            self.assertIn("cannot be opened or probed", reason)
            self.assertIn("busy", reason)

    def test_fw_chunk_gate_when_port_vanishes(self):
        # skip_reason passed (an injected access says so) but find_port sees no
        # port: a reason string, never a TypeError from open_raw(None).
        with tempfile.TemporaryDirectory() as d:
            reason = bp.fw_chunk_skip_reason(d, access=lambda *a: True)
            self.assertIsInstance(reason, str)

    def test_parse_fw_chunk_reply_rejects_unknown_status(self):
        self.assertIsNone(bp.parse_fw_chunk_reply(b"?\x00\x00\x00\x00"))
        self.assertIsNone(bp.parse_fw_chunk_reply(b".\x00\x00"))
        self.assertEqual(bp.parse_fw_chunk_reply(b"." + struct.pack("<I", 236)), (True, 236))
        self.assertEqual(bp.parse_fw_chunk_reply(b"!" + struct.pack("<I", 0)), (False, 0))

    def test_fake_firmware_write_returns_frame_length(self):
        fw = FakeFirmware(image_size=1000)
        frame = bp.fw_chunk_frame(0, b"\x5a" * 100)
        self.assertEqual(fw.write(0, frame), len(frame))
        self.assertEqual(fw.cursor, 100)


class ResourceSlotOverPipeTest(unittest.TestCase):
    """The resource-slot pipe test's payload and its place in the suite."""

    def test_payload_has_the_whx_magic_and_crosses_a_block(self):
        from station import hil_tests
        p = hil_tests.resource_pipe_payload()
        self.assertEqual(p[:4], b"IWHX")
        self.assertEqual(len(p), 96 * 1024)
        self.assertGreater(len(p), 64 * 1024)          # one block erase + sectors
        self.assertEqual(len(p) % 4096, 0)
        # No erased-flash run: an all-0xFF stretch would pass a CRC over bytes
        # the pipe never wrote.
        self.assertNotIn(b"\xff" * 64, p)

    def _drive(self, stream=(True, 96 * 1024, ""), begin=(True, b"P\x50."),
               commit=bytes([0x50, 0x52, ord(".")]), find_port="/dev/ttyACM0"):
        """Run the HIL test against mocks: BEGIN and COMMIT answers, the pipe
        stream's (ok, offset, reason) and the port. Returns (verdict, log lines,
        the raw HID commands sent, the abort count)."""
        from station import hil_tests
        from unittest import mock
        sent, lines, aborts = [], [], []

        def raw_send(pkt, timeout_ms=None, **kw):
            sent.append(pkt[1])
            return commit if pkt[1] == hil_tests.CMD_FONTPACK_COMMIT else b"P\x06."

        raw = mock.Mock()
        raw.send.side_effect = raw_send
        # The fd is opened inside the open_raw mock, so a run that returns
        # before open_raw allocates none; the test under test closes it.
        with mock.patch.object(hil_tests, "_doom_slot_begin", return_value=begin), \
             mock.patch.object(hil_tests, "_fontpack_abort", side_effect=lambda r: aborts.append(1)), \
             mock.patch.object(bp, "find_port", return_value=find_port), \
             mock.patch.object(bp, "open_raw", side_effect=lambda port: os.open(os.devnull, os.O_RDONLY)), \
             mock.patch.object(bp, "stream_firmware", return_value=stream):
            ok = hil_tests.test_bulk_pipe_resource_slot(raw, lines.append)
        return ok, lines, sent, len(aborts)

    def test_a_complete_stream_and_an_acked_commit_pass(self):
        from station import hil_tests
        ok, lines, sent, aborts = self._drive()
        self.assertTrue(ok, lines)
        self.assertEqual(sent, [hil_tests.CMD_FONTPACK_COMMIT])      # BEGIN is mocked, no HID chunk
        self.assertEqual(aborts, 0)
        self.assertTrue(any("staged over the bulk pipe" in l for l in lines), lines)

    def test_a_stopped_pipe_fails_without_an_hid_fallback(self):
        from station import hil_tests
        ok, lines, sent, aborts = self._drive(stream=(False, 1234, "no reply"))
        self.assertFalse(ok)
        self.assertNotIn(hil_tests.CMD_FONTPACK_CHUNK, sent)
        self.assertNotIn(hil_tests.CMD_FONTPACK_COMMIT, sent)       # the abort is the only COMMIT
        self.assertEqual(aborts, 1)
        self.assertTrue(any("stopped at 1234" in l for l in lines), lines)

    def test_a_rejected_commit_fails(self):
        ok, lines, sent, aborts = self._drive(commit=bytes([0x50, 0x52, ord("R")]))
        self.assertFalse(ok)
        self.assertTrue(any("FONTPACK_COMMIT after the pipe transfer" in l for l in lines), lines)

    def test_a_begin_that_never_becomes_ready_fails_before_the_pipe(self):
        ok, lines, sent, aborts = self._drive(begin=(False, None))
        self.assertFalse(ok)
        self.assertEqual(sent, [])
        self.assertTrue(any("did not become ready" in l for l in lines), lines)

    def test_a_vanished_port_fails_and_aborts(self):
        ok, lines, sent, aborts = self._drive(find_port=None)
        self.assertFalse(ok)
        self.assertEqual(aborts, 1)
        self.assertEqual(sent, [])

    def test_entry_is_gated_and_runs_after_the_hid_round_trip(self):
        from station import hil_tests
        entry = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_bulk_pipe_resource_slot)
        self.assertIs(entry["skip_if"], bp.fw_chunk_skip_reason)
        self.assertEqual(entry["min_protocol"], 6)
        fns = [t["fn"] for t in hil_tests.TESTS]
        self.assertLess(fns.index(hil_tests.test_doomwad_slot_roundtrip), fns.index(entry["fn"]))


if __name__ == "__main__":
    unittest.main()
