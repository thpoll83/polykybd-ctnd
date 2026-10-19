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
CMD_FW_CHUNK = 0x10        # pipe protocol 2: one firmware-update chunk (poly_pipe.h)
REPLY = 0x80
PIPE_PROTO_FW_CHUNK = 2    # the PING protocol a firmware needs for CMD_FW_CHUNK
FW_CHUNK_MAX = 236         # data bytes per frame (qmk FW_UP_RELAY_CHUNK_MAX: one split RPC)
FW_CHUNK_WINDOW = 4        # frames sent ahead of their replies (poly_pipe.h: the host must pace itself)
FW_CHUNK_TIMEOUT_S = 8.0   # a reply this late means the keyboard is gone (the HID path's 8000 ms)
FW_CHUNK_DRAIN_S = 1.0     # wait for the replies of the frames in flight after a NACK
FW_CHUNK_ATTEMPTS = 8
FW_CHUNK_MAX_REWINDS = 200
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


def fw_chunk_frame(offset: int, data: bytes) -> bytes:
    """One FW_CHUNK frame: the offset (u32 LE) and 1..FW_CHUNK_MAX data bytes."""
    return pack_frame(CMD_FW_CHUNK, struct.pack("<I", offset) + data)


def parse_fw_chunk_reply(payload: bytes):
    """``(acked, cursor)`` from a FW_CHUNK reply, or None for a malformed one
    (too short, or a status byte other than ``.`` and ``!``)."""
    if len(payload) < 5 or payload[0] not in (ord("."), ord("!")):
        return None
    return payload[0] == ord("."), struct.unpack_from("<I", payload, 1)[0]


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
    try:
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
    except (OSError, termios.error):
        # The port went away or refused the settings: do not leak the fd.
        os.close(fd)
        raise
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


def stream_firmware(pipe: "Pipe", fw: bytes, start: int = 0, chunk: int = FW_CHUNK_MAX,
                    window: int = FW_CHUNK_WINDOW, timeout: float = FW_CHUNK_TIMEOUT_S,
                    log=None) -> tuple:
    """Send ``fw[start:]`` as FW_CHUNK frames, ``window`` frames ahead of their
    replies, after FW_UP_BEGIN was ACKed over HID. Returns ``(ok, offset, reason)``:
    ``ok`` when the keyboard's cursor reached the end; otherwise ``offset`` is the
    cursor to continue from (over HID) and ``reason`` says why.

    Mirrors PolyKybdHost ``bulk_pipe.stream_firmware``: every reply carries the
    keyboard's cursor, an ACK advances the confirmed offset to it, a NACK rewinds
    to it once the replies of the frames in flight drained (a dropped frame never
    answers, so the drain gives up after FW_CHUNK_DRAIN_S), and a quiet window is
    resolved with a cursor probe (an empty FW_CHUNK, always NACKed with the cursor).
    """
    total = len(fw)
    offset = confirmed = start
    in_flight = drain = 0
    attempts = rewinds = 0
    clock = pipe._clock
    last_reply = clock()
    while True:
        if confirmed >= total:
            return True, total, None
        if drain == 0:
            while in_flight < window and offset < total:
                n = min(chunk, total - offset)
                pipe.write_all(fw_chunk_frame(offset, fw[offset:offset + n]))
                offset += n
                in_flight += 1
        data = pipe._read(pipe.fd, 4096)
        if data:
            for cmd, body in pipe.parser.feed(data):
                if cmd != (CMD_FW_CHUNK | REPLY):
                    continue
                parsed = parse_fw_chunk_reply(body)
                if parsed is None:
                    continue
                last_reply = clock()
                acked, cursor = parsed
                if drain:
                    drain -= 1
                    in_flight = max(0, in_flight - 1)
                    confirmed = max(confirmed, min(cursor, total))
                    if drain == 0:
                        offset = confirmed
                        in_flight = 0
                    continue
                in_flight = max(0, in_flight - 1)
                if acked:
                    if cursor > confirmed:
                        confirmed = cursor
                        attempts = 0
                    continue
                rewinds += 1
                if rewinds > FW_CHUNK_MAX_REWINDS:
                    return False, cursor, "rejected"
                if cursor <= confirmed:
                    attempts += 1
                    if attempts >= FW_CHUNK_ATTEMPTS:
                        return False, cursor, "rejected"
                if log:
                    log(f"  pipe: NACK, keyboard cursor {cursor} (resync {rewinds})")
                confirmed = max(confirmed, cursor) if cursor <= total else confirmed
                if in_flight:
                    drain = in_flight
                else:
                    offset = cursor
            continue
        quiet = clock() - last_reply
        if drain and quiet >= FW_CHUNK_DRAIN_S:
            drain = in_flight = 0
            offset = confirmed
            last_reply = clock()
            continue
        if quiet < timeout:
            continue
        attempts += 1
        if attempts >= FW_CHUNK_ATTEMPTS:
            return False, confirmed, "no reply"
        reply = pipe.request(CMD_FW_CHUNK, b"", timeout=timeout)
        parsed = parse_fw_chunk_reply(reply) if reply is not None else None
        if parsed is None:
            return False, confirmed, "no reply"
        if log:
            log(f"  pipe: quiet for {quiet:.1f} s, keyboard cursor {parsed[1]}")
        last_reply = clock()
        offset = confirmed = max(confirmed, parsed[1])
        in_flight = drain = 0


def fw_chunk_skip_reason(sys_tty: str = SYS_TTY, access=os.access, open_pipe=None):
    """Why a FW_CHUNK check cannot run: ``skip_reason`` plus a firmware whose
    pipe protocol predates FW_CHUNK (a capability, so SKIP)."""
    reason = skip_reason(sys_tty, access)
    if reason:
        return reason
    port = find_port(sys_tty)
    if port is None:
        # The port was there for skip_reason and is gone now (re-enumeration).
        return "CDC port vanished between the port search and the PING"
    if open_pipe is None:
        def open_pipe(p):
            fd = open_raw(p)
            return Pipe(fd), (lambda: os.close(fd))
    try:
        pipe, close = open_pipe(port)
        try:
            reply = pipe.request(CMD_PING, timeout=1.0)
        finally:
            close()
    except (OSError, termios.error) as exc:
        return f"{port} cannot be opened or probed: {exc}"
    if reply is None:
        return f"{port} does not answer PING"
    proto = parse_ping(reply)["proto"]
    if proto < PIPE_PROTO_FW_CHUNK:
        return f"pipe protocol {proto}: FW_CHUNK needs {PIPE_PROTO_FW_CHUNK}"
    return None
