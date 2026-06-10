# SPDX-License-Identifier: GPL-2.0-only
"""HID firmware-update (stage + verify) client for the HIL rig.

The rig normally flashes both halves over BOOTSEL/picotool (UF2), which never
exercises the keyboard's *own* HID firmware-update path — the one a real user
hits from PolyKybdHost, and the one whose split-link behaviour during the flash
erase is the most timing-sensitive code in the firmware (it is why
SPLIT_MAX_CONNECTION_ERRORS is raised to 200). This module drives that path
against the live keyboard so CI catches regressions in it.

It performs **stage + verify** only — ``BEGIN -> N*CHUNK -> COMMIT`` — which is
deliberately *non-destructive*: COMMIT verifies the CRC32 the keyboard
accumulated while staging the image into its spare flash region, but does NOT
activate it or reboot (that is the separate FW_UP_APPLY step). So the keyboard
keeps running its current firmware throughout; a failure leaves nothing to
recover. That still exercises the whole risky surface: the master's synchronous
staging erase, the slave's deferred sector erase, the chunk stream relayed over
the split bridge with identity-bound ACKs, and the final CRC check.

APPLY is intentionally not done here: on the rig it would reboot the master onto
the staged (non-HIL) image, which then uses VBUS master-detection and makes both
halves enumerate as master until the next UF2 flash — recoverable but messy, and
not worth the risk on an unattended rig.

Protocol mirrors PolyKybdHost ``polyhost/device/hid_fw_up.py`` and the firmware
``keyboards/handwired/polykybd/hid_fw_up.c``.
"""
import binascii
import struct
import time
from typing import Callable

import hid

from .hid import _find_path, _frame
from .config import QMK_VENDOR_ID, QMK_PRODUCT_ID, HID_RAW_USAGE_PAGE, HID_RAW_USAGE

HID_POLYKYBD          = 0x50   # ord('P')
CMD_FW_UP_BEGIN       = 0x40
CMD_FW_UP_CHUNK       = 0x41
CMD_FW_UP_COMMIT      = 0x42
CMD_FW_UP_GET_VERSION = 0x43

FW_UP_CHUNK_SIZE  = 56
FW_UP_VERSION_LEN = 16
ACK   = ord(".")
NACK  = ord("!")
BUSY  = ord("~")   # BEGIN: still erasing — re-poll


class _FwLink:
    """A persistent Raw HID handle that can reopen itself across a USB dropout.

    The master tears USB down during its synchronous staging erase (and again is
    momentarily unreachable around the slave's deferred erase), so a single
    open() is not enough — the handle has to be reacquired by path, which can
    change after re-enumeration.
    """

    def __init__(self, vid: int, pid: int):
        self._vid = vid
        self._pid = pid
        self._dev = None

    def open(self, timeout_s: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            path = _find_path(self._vid, self._pid, HID_RAW_USAGE_PAGE, HID_RAW_USAGE)
            if path is not None:
                try:
                    self._dev = hid.Device(path=path)
                    return True
                except Exception:
                    pass
            time.sleep(0.2)
        return False

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None

    def xfer(self, payload: bytes, timeout_ms: int) -> bytes | None:
        """Write one report and read one reply. Returns None on timeout/error
        and drops the handle so the caller can reopen()."""
        if self._dev is None:
            return None
        try:
            self._dev.write(_frame(payload))
            chunk = self._dev.read(64, timeout=timeout_ms)
            return bytes(chunk) if chunk else None
        except Exception:
            self.close()
            return None


def _get_version(link: _FwLink, log: Callable[[str], None]) -> dict | None:
    reply = link.xfer(bytes([HID_POLYKYBD, CMD_FW_UP_GET_VERSION]), timeout_ms=5000)
    if (reply is None or len(reply) < 3 + FW_UP_VERSION_LEN + 8
            or reply[0] != HID_POLYKYBD or reply[1] != CMD_FW_UP_GET_VERSION
            or reply[2] != ACK):
        return None
    version = bytes(reply[3:3 + FW_UP_VERSION_LEN]).rstrip(b"\x00").decode("utf-8", "replace")
    size = struct.unpack_from("<I", bytes(reply), 3 + FW_UP_VERSION_LEN)[0]
    crc  = struct.unpack_from("<I", bytes(reply), 3 + FW_UP_VERSION_LEN + 4)[0]
    return {"version": version, "size": size, "crc": crc}


def stage_and_verify(bin_path: str, log: Callable[[str], None],
                     vid: int = QMK_VENDOR_ID, pid: int = QMK_PRODUCT_ID) -> bool:
    """Run BEGIN -> CHUNK* -> COMMIT for ``bin_path`` against the live keyboard.

    Returns True iff COMMIT ACKs (the keyboard's accumulated CRC matched the
    image we streamed). Non-destructive: no APPLY, no reboot.
    """
    try:
        with open(bin_path, "rb") as f:
            fw = f.read()
    except OSError as exc:
        log(f"  FAIL: cannot read firmware image {bin_path!r}: {exc}")
        return False

    fw_size = len(fw)
    if fw_size < 264:
        log(f"  FAIL: image too small ({fw_size} bytes) to be an RP2040 .bin")
        return False
    fw_crc = binascii.crc32(fw) & 0xFFFFFFFF
    total_chunks = (fw_size + FW_UP_CHUNK_SIZE - 1) // FW_UP_CHUNK_SIZE
    log(f"  staging {fw_size} bytes ({fw_size // 1024} KB), CRC32 0x{fw_crc:08X}, "
        f"{total_chunks} chunks")

    link = _FwLink(vid, pid)
    if not link.open():
        log("  FAIL: Raw HID interface not found")
        return False
    try:
        running = _get_version(link, log)
        if running:
            log(f"  running firmware: {running['version']!r} "
                f"(size {running['size']}, crc 0x{running['crc']:08X})")

        # -- BEGIN: erase staging on both halves. '.' ready / '~' erasing / '!'
        #    error / no reply (master tore USB down during its erase -> reopen). --
        begin = bytes([HID_POLYKYBD, CMD_FW_UP_BEGIN]) + struct.pack("<II", fw_size, fw_crc)
        deadline = time.monotonic() + 90.0
        timeout_ms = 15000   # first send covers the master's ~6 s synchronous erase
        ready = False
        while not ready:
            if time.monotonic() > deadline:
                log("  FAIL: FW_UP_BEGIN timed out (>90 s erasing)")
                return False
            reply = link.xfer(begin, timeout_ms)
            timeout_ms = 5000
            if reply is None or len(reply) < 3:
                # No reply: either the master tore USB down during its synchronous
                # staging erase (handle now dead -> reopen by path), or it's simply
                # still busy (handle alive -> just re-poll).
                if link._dev is None:
                    log("  BEGIN: USB dropped during master erase; reopening…")
                    if not link.open():
                        log("  FAIL: keyboard did not re-enumerate after BEGIN erase")
                        return False
                else:
                    log("  BEGIN: no reply yet (still erasing); re-polling…")
                    time.sleep(0.3)
                continue
            if reply[2] == ACK:
                ready = True
            elif reply[2] == BUSY:
                time.sleep(0.3)   # slave still erasing; let the split loop run
            else:
                log("  FAIL: FW_UP_BEGIN NACK — slave half could not be prepared "
                    "(disconnected / old firmware?)")
                return False
        log("  BEGIN ok — staging erased on both halves")

        # -- CHUNK x N. ACK '.' advances; NACK '!' may carry a resume offset
        #    (bytes 3..6) — rewind to it; otherwise back off and re-send. --
        CHUNK_TIMEOUT = 8000
        CHUNK_ATTEMPTS = 8
        MAX_REWINDS = 200
        i = attempts = rewinds = 0
        while i < total_chunks:
            offset = i * FW_UP_CHUNK_SIZE
            raw = fw[offset:offset + FW_UP_CHUNK_SIZE]
            padded = raw + b"\xff" * (FW_UP_CHUNK_SIZE - len(raw))
            pkt = bytes([HID_POLYKYBD, CMD_FW_UP_CHUNK]) + struct.pack("<I", offset) + padded
            reply = link.xfer(pkt, CHUNK_TIMEOUT)

            if reply is not None and len(reply) >= 3 and reply[2] == ACK:
                attempts = 0
                i += 1
                if i % 500 == 0 or i == total_chunks:
                    log(f"  chunk {i}/{total_chunks} "
                        f"({(i * FW_UP_CHUNK_SIZE) // 1024} KB)")
                continue

            resume = (struct.unpack_from("<I", bytes(reply), 3)[0]
                      if reply is not None and len(reply) >= 7 else 0)
            if (reply is not None and len(reply) >= 7 and reply[2] == NACK
                    and 0 < resume < offset and resume % FW_UP_CHUNK_SIZE == 0
                    and rewinds < MAX_REWINDS):
                rewinds += 1
                attempts = 0
                i = resume // FW_UP_CHUNK_SIZE
                log(f"  halves resynced — rewinding to chunk {i + 1} "
                    f"(offset {resume}, resync {rewinds})")
                time.sleep(0.05)
                continue

            attempts += 1
            if attempts >= CHUNK_ATTEMPTS:
                why = "rejected" if reply is not None else "no reply"
                log(f"  FAIL: chunk at offset {offset} {why} after {CHUNK_ATTEMPTS} attempts")
                return False
            if link._dev is None and not link.open():
                log("  FAIL: lost the keyboard mid-stream and it did not re-enumerate")
                return False
            time.sleep(min(0.05 * (2 ** (attempts - 1)), 1.0))

        log(f"  all {total_chunks} chunks delivered "
            f"({rewinds} resync rewind(s)) — verifying CRC…")

        # -- COMMIT: verify the keyboard's accumulated CRC32. No apply/reboot. --
        reply = link.xfer(bytes([HID_POLYKYBD, CMD_FW_UP_COMMIT]), timeout_ms=8000)
        if reply is None or len(reply) < 3:
            log("  FAIL: FW_UP_COMMIT — no reply")
            return False
        if reply[2] != ACK:
            log(f"  FAIL: FW_UP_COMMIT NACK (status {reply[2]:#04x}) — staged CRC mismatch")
            return False
        log("  COMMIT ok — staged image CRC verified on the keyboard (not applied)")
        return True
    finally:
        link.close()
