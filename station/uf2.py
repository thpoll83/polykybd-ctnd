# SPDX-License-Identifier: GPL-2.0-only
"""UF2 -> raw image conversion, so the rig can stage the image it is running.

The rig flashes ``*_hil_left.uf2`` / ``*_hil_right.uf2`` over BOOTSEL, but the
HID firmware-update path takes the raw image (what ``objcopy -O binary`` emits),
not the UF2 wrapper. Converting the UF2 the rig already has is what lets the
apply test stage **the same image the master is running** — which is the whole
reason the apply is safe to run here at all: applying a *different*, non-HIL
image would reboot the master onto VBUS master-detection and make both halves
enumerate as master until the next UF2 flash.

Format (per the UF2 spec): 512-byte blocks, each with a 32-byte header, up to
476 bytes of payload and a trailing magic. Only ``payload_size`` bytes of each
block are real; RP2040 uses 256.
"""
import struct

UF2_MAGIC_START0 = 0x0A324655   # "UF2\n"
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END    = 0x0AB16F30
UF2_BLOCK_SIZE   = 512
UF2_FLAG_NOT_MAIN_FLASH = 0x00000001

XIP_BASE = 0x10000000


class Uf2Error(ValueError):
    """The bytes handed to :func:`uf2_to_bin` are not a usable UF2 image."""


def uf2_to_bin(data: bytes, base: int = XIP_BASE) -> bytes:
    """Return the contiguous image carried by UF2 ``data``.

    ⚠️ Raises rather than returning a short image on a gap. A UF2 addresses each
    block explicitly, so a missing block is a *hole*, and silently closing it
    would hand the keyboard an image whose every later byte is offset — which
    stages and CRCs perfectly and then bricks the board on apply. The firmware
    images this converts are contiguous, so a gap means something is wrong with
    the file, not with this assumption.
    """
    if len(data) % UF2_BLOCK_SIZE:
        raise Uf2Error(f"not a whole number of 512-byte UF2 blocks ({len(data)} bytes)")
    if not data:
        raise Uf2Error("empty file")

    out = bytearray()
    expected_addr = None
    for off in range(0, len(data), UF2_BLOCK_SIZE):
        blk = data[off:off + UF2_BLOCK_SIZE]
        m0, m1, flags, addr, payload_size, _blk_no, _num_blks, _famid = \
            struct.unpack("<8I", blk[:32])
        if m0 != UF2_MAGIC_START0 or m1 != UF2_MAGIC_START1:
            raise Uf2Error(f"block at {off} has a bad start magic")
        if struct.unpack("<I", blk[-4:])[0] != UF2_MAGIC_END:
            raise Uf2Error(f"block at {off} has a bad end magic")
        if flags & UF2_FLAG_NOT_MAIN_FLASH:
            continue
        if payload_size > 476:
            raise Uf2Error(f"block at {off} claims a {payload_size}-byte payload")
        if expected_addr is None:
            if addr != base:
                raise Uf2Error(f"image starts at {addr:#x}, expected {base:#x}")
        elif addr != expected_addr:
            raise Uf2Error(
                f"gap or overlap at {addr:#x}: expected {expected_addr:#x}. "
                "Refusing to guess — a hole silently shifts every later byte")
        out += blk[32:32 + payload_size]
        expected_addr = addr + payload_size

    if not out:
        raise Uf2Error("no main-flash blocks in the file")
    return bytes(out)


def uf2_file_to_bin(path: str, base: int = XIP_BASE) -> bytes:
    with open(path, "rb") as fh:
        return uf2_to_bin(fh.read(), base)
