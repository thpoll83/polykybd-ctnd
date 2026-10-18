# SPDX-License-Identifier: GPL-2.0-only
"""The keyboard's bulk pipe on the rig: find it, open it raw, measure it.

A ``POLYKYBD_BULK_PIPE`` firmware adds a CDC-ACM interface next to raw HID
(qmk ``keyboards/polykybd/BULK_PIPE.md``). On the Pi it is ``/dev/ttyACM*``
with the keyboard's idVendor. The frame format is the firmware's
(``poly_pipe.h``)::

    'P' 'B'  cmd  len_lo len_hi  payload[len]  crc32 LE

with ``zlib.crc32`` over cmd, len and payload, and replies carrying
``cmd | 0x80``. Only the standard library is used (termios), so the rig needs
no new Python package.

The port is root:dialout by default. ``setup.sh`` adds the station user to
``dialout`` and a udev rule, but a push never provisions the rig, so
:func:`skip_reason` tells the suite to SKIP (not fail) while either is absent.
"""
import glob
import os
import struct
import termios
import time
import zlib

VID = "2021"
MAGIC = b"PB"
CMD_PING, CMD_SINK, CMD_STAT, CMD_RESET, CMD_ECHO = 0x01, 0x02, 0x03, 0x04, 0x05
REPLY = 0x80
STAT_FIELDS = ("sink_bytes", "frames_ok", "frames_bad", "resync_bytes",
               "sink_crc", "first_ms", "last_ms")
SYS_TTY = "/sys/class/tty"


def pack_frame(cmd: int, payload: bytes = b"") -> bytes:
    body = bytes([cmd]) + struct.pack("<H", len(payload)) + payload
    return MAGIC + body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def parse_stat(payload: bytes) -> dict:
    if len(payload) != 4 * len(STAT_FIELDS):
        raise ValueError(f"STAT reply has {len(payload)} bytes, expected {4 * len(STAT_FIELDS)}")
    return dict(zip(STAT_FIELDS, struct.unpack("<" + "I" * len(STAT_FIELDS), payload)))


def parse_ping(payload: bytes) -> dict:
    proto, max_payload, epsize, out_cap = struct.unpack_from("<BHBB", payload)
    fw = payload[5:].split(b"\0", 1)[0].decode(errors="replace")
    return {"proto": proto, "max_payload": max_payload, "epsize": epsize,
            "out_capacity": out_cap, "fw": fw}


class FrameParser:
    """Reassemble reply frames from a byte stream (noise and splits tolerated)."""

    def __init__(self):
        self.buf = bytearray()
        self.dropped = 0

    def feed(self, data: bytes) -> list:
        self.buf += data
        out = []
        while True:
            start = self.buf.find(MAGIC)
            if start < 0:
                keep = 1 if self.buf.endswith(b"P") else 0
                self.dropped += len(self.buf) - keep
                del self.buf[:len(self.buf) - keep]
                return out
            if start:
                self.dropped += start
                del self.buf[:start]
            if len(self.buf) < 5:
                return out
            length = struct.unpack_from("<H", self.buf, 3)[0]
            end = 5 + length + 4
            if len(self.buf) < end:
                return out
            body = bytes(self.buf[2:5 + length])
            crc = struct.unpack_from("<I", self.buf, 5 + length)[0]
            if zlib.crc32(body) & 0xFFFFFFFF == crc:
                out.append((body[0], body[3:]))
                del self.buf[:end]
            else:
                self.dropped += 2
                del self.buf[:2]


def find_port(sys_tty: str = SYS_TTY, vid: str = VID):
    """The /dev node of the first ttyACM whose USB parent has the keyboard's
    idVendor, or None. Reads sysfs only, so it is unit-testable with a fake tree."""
    for tty in sorted(glob.glob(os.path.join(sys_tty, "ttyACM*"))):
        try:
            with open(os.path.join(tty, "device", "..", "idVendor")) as f:
                if f.read().strip() == vid:
                    return "/dev/" + os.path.basename(tty)
        except OSError:
            continue
    return None


def skip_reason(sys_tty: str = SYS_TTY, access=os.access):
    """Why the pipe check cannot run on this rig, or None when it can.

    Absent port: the flashed firmware has no bulk pipe (older firmware, or a
    ``POLYKYBD_BULK_PIPE=no`` build) — a capability, so SKIP. Port present but
    not writable: the rig was provisioned before the dialout/udev step of
    ``setup.sh`` — SKIP, never FAIL (polykybd-ctnd/CLAUDE.md, ctnd#116)."""
    port = find_port(sys_tty)
    if port is None:
        return "no CDC port with idVendor 2021: firmware without POLYKYBD_BULK_PIPE"
    if not access(port, os.R_OK | os.W_OK):
        return f"{port} not writable by this user: run setup.sh (dialout group + udev rule)"
    return None


def open_raw(port: str, read_timeout_ds: int = 2) -> int:
    """Open the tty with no line discipline and a VTIME read timeout (deciseconds)."""
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[0] &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
                  | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.IXON)
    attrs[1] &= ~termios.OPOST
    attrs[2] &= ~(termios.CSIZE | termios.PARENB)
    attrs[2] |= termios.CS8 | termios.CLOCAL | termios.CREAD
    attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = read_timeout_ds
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


class Pipe:
    """Request/reply over an open fd. ``read``/``write`` are injectable for tests."""

    def __init__(self, fd, read=os.read, write=os.write, clock=time.monotonic):
        self.fd = fd
        self._read = read
        self._write = write
        self._clock = clock
        self.parser = FrameParser()

    def write_all(self, data: bytes, chunk: int = 65536):
        view = memoryview(data)
        off = 0
        while off < len(data):
            off += self._write(self.fd, view[off:off + chunk])

    def request(self, cmd: int, payload: bytes = b"", timeout: float = 5.0):
        self.write_all(pack_frame(cmd, payload))
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            data = self._read(self.fd, 4096)
            if not data:
                continue
            for got, body in self.parser.feed(data):
                if got == (cmd | REPLY):
                    return body
        return None


def sink_payload(size: int) -> bytes:
    """A fixed, non-repeating-looking block (what the firmware sees is what the
    expected CRC is computed over, so any content works; this avoids long runs)."""
    return bytes((i * 7919 + 13) & 0xFF for i in range(size))


def measure(pipe: Pipe, total_bytes: int, frame_payload: int, log) -> dict:
    """Stream SINK frames, then read the keyboard's own counters.

    Returns a dict with ``ok`` (byte count, CRC and bad-frame count all right)
    plus the numbers, so the test can log them and FUTURE_TESTS.md can gather a
    baseline before any rate threshold is asserted."""
    info = pipe.request(CMD_PING)
    if info is None:
        return {"ok": False, "error": "no PING reply"}
    ping = parse_ping(info)
    log(f"  pipe: firmware {ping['fw']} proto {ping['proto']} packet {ping['epsize']} B "
        f"out queue {ping['out_capacity']}")
    if pipe.request(CMD_RESET) is None:
        return {"ok": False, "error": "no RESET reply"}
    block = sink_payload(frame_payload)
    frames = max(1, total_bytes // frame_payload)
    wire = pack_frame(CMD_SINK, block) * frames
    expect_crc = 0
    for _ in range(frames):
        expect_crc = zlib.crc32(block, expect_crc) & 0xFFFFFFFF
    t0 = time.perf_counter()
    pipe.write_all(wire)
    st = pipe.request(CMD_STAT, timeout=20.0)
    host_s = time.perf_counter() - t0
    if st is None:
        return {"ok": False, "error": "no STAT reply after the SINK stream"}
    stats = parse_stat(st)
    payload = frames * frame_payload
    dev_s = (stats["last_ms"] - stats["first_ms"]) / 1000.0
    result = {
        "ok": (stats["sink_bytes"] == payload and stats["sink_crc"] == expect_crc
               and stats["frames_bad"] == 0),
        "payload": payload, "frames": frames, "host_s": host_s, "device_s": dev_s,
        "expect_crc": expect_crc, "stats": stats, "ping": ping,
    }
    log(f"  SINK: {payload} B in {frames} frames; host {host_s:.3f} s "
        f"({payload / host_s / 1e6:.3f} MB/s)"
        + (f", keyboard clock {dev_s:.3f} s ({payload / dev_s / 1e6:.3f} MB/s)" if dev_s > 0 else ""))
    log(f"  keyboard: {stats['sink_bytes']} B, {stats['frames_ok']} good, {stats['frames_bad']} bad, "
        f"{stats['resync_bytes']} resync, crc 0x{stats['sink_crc']:08x} "
        f"(expected 0x{expect_crc:08x})")
    return result
