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

    def __init__(self, drop_frame=None):
        self.parser = bp.FrameParser()
        self.out = bytearray()
        self.sink_bytes = 0
        self.frames_ok = 0
        self.crc = 0
        self.first_ms = 0
        self.last_ms = 0
        self.sink_seen = 0
        self.drop_frame = drop_frame

    def write(self, fd, data):
        for cmd, payload in self.parser.feed(bytes(data)):
            self.frames_ok += 1
            if cmd == bp.CMD_PING:
                self.out += bp.pack_frame(cmd | bp.REPLY, struct.pack("<BHBB", 1, 512, 64, 8) + b"1.9.3\0")
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


if __name__ == "__main__":
    unittest.main()
