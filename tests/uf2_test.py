# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the UF2 -> raw image conversion.

This converter guards the one destructive check on the rig: the firmware apply
round-trip compares its candidate image against the UF2 the rig actually
flashed, and refuses to apply anything else. A converter that silently produced
a *plausible but wrong* image would defeat that guard, so the cases below are
mostly about refusing bad input rather than accepting good input.
"""
import struct
import unittest

from station.uf2 import (uf2_to_bin, Uf2Error, UF2_MAGIC_START0, UF2_MAGIC_START1,
                         UF2_MAGIC_END, UF2_FLAG_NOT_MAIN_FLASH, XIP_BASE)


def block(addr, payload, flags=0, blk_no=0, num_blks=1, payload_size=None):
    size = len(payload) if payload_size is None else payload_size
    hdr = struct.pack("<8I", UF2_MAGIC_START0, UF2_MAGIC_START1, flags, addr,
                      size, blk_no, num_blks, 0xE48BFF56)
    body = payload + b"\x00" * (476 - len(payload))
    return hdr + body + struct.pack("<I", UF2_MAGIC_END)


class Uf2ToBinTest(unittest.TestCase):
    def test_a_single_block_round_trips(self):
        self.assertEqual(uf2_to_bin(block(XIP_BASE, b"\xAA" * 256)), b"\xAA" * 256)

    def test_contiguous_blocks_concatenate_in_address_order(self):
        data = (block(XIP_BASE, b"\x01" * 256, blk_no=0, num_blks=2)
                + block(XIP_BASE + 256, b"\x02" * 256, blk_no=1, num_blks=2))
        self.assertEqual(uf2_to_bin(data), b"\x01" * 256 + b"\x02" * 256)

    def test_a_short_final_block_keeps_only_its_payload(self):
        # RP2040 images are not a multiple of 256, so the last block is short.
        data = (block(XIP_BASE, b"\x01" * 256, blk_no=0, num_blks=2)
                + block(XIP_BASE + 256, b"\x02" * 16, blk_no=1, num_blks=2))
        self.assertEqual(uf2_to_bin(data), b"\x01" * 256 + b"\x02" * 16)

    def test_a_gap_is_refused_rather_than_closed(self):
        # THE important case: closing a hole shifts every later byte, which
        # stages and CRCs perfectly and then bricks the board on apply.
        data = (block(XIP_BASE, b"\x01" * 256)
                + block(XIP_BASE + 512, b"\x02" * 256))   # 256 bytes missing
        with self.assertRaises(Uf2Error) as ctx:
            uf2_to_bin(data)
        self.assertIn("gap", str(ctx.exception))

    def test_overlapping_blocks_are_refused(self):
        data = (block(XIP_BASE, b"\x01" * 256)
                + block(XIP_BASE + 128, b"\x02" * 256))
        with self.assertRaises(Uf2Error):
            uf2_to_bin(data)

    def test_a_wrong_start_address_is_refused(self):
        with self.assertRaises(Uf2Error):
            uf2_to_bin(block(0x20000000, b"\x01" * 256))

    def test_not_main_flash_blocks_are_skipped_without_breaking_contiguity(self):
        data = (block(XIP_BASE, b"\x01" * 256)
                + block(0xDEADBEEF, b"\xFF" * 8, flags=UF2_FLAG_NOT_MAIN_FLASH)
                + block(XIP_BASE + 256, b"\x02" * 256))
        self.assertEqual(uf2_to_bin(data), b"\x01" * 256 + b"\x02" * 256)

    def test_bad_start_magic_is_refused(self):
        bad = bytearray(block(XIP_BASE, b"\x01" * 256))
        bad[0] ^= 0xFF
        with self.assertRaises(Uf2Error):
            uf2_to_bin(bytes(bad))

    def test_bad_end_magic_is_refused(self):
        bad = bytearray(block(XIP_BASE, b"\x01" * 256))
        bad[-1] ^= 0xFF
        with self.assertRaises(Uf2Error):
            uf2_to_bin(bytes(bad))

    def test_a_truncated_file_is_refused(self):
        with self.assertRaises(Uf2Error):
            uf2_to_bin(block(XIP_BASE, b"\x01" * 256)[:-4])

    def test_an_oversized_payload_claim_is_refused(self):
        with self.assertRaises(Uf2Error):
            uf2_to_bin(block(XIP_BASE, b"\x01" * 256, payload_size=999))

    def test_an_empty_file_is_refused(self):
        with self.assertRaises(Uf2Error):
            uf2_to_bin(b"")

    def test_a_file_of_only_non_flash_blocks_is_refused(self):
        data = block(0xDEADBEEF, b"\xFF" * 8, flags=UF2_FLAG_NOT_MAIN_FLASH)
        with self.assertRaises(Uf2Error):
            uf2_to_bin(data)


class ApplyImageGuardTest(unittest.TestCase):
    """The runner's 'is this the image we flashed?' rule, stated directly.

    The rule is: the .bin must be a prefix of the UF2 payload, and everything
    after it must be 0xFF block padding. It is asserted here rather than only in
    the runner because getting it wrong in the permissive direction is what
    would let a non-HIL image be applied — after which both halves enumerate as
    master until someone reflashes over BOOTSEL.
    """
    @staticmethod
    def accepts(img, flashed):
        return (len(flashed) >= len(img) and flashed[:len(img)] == img
                and set(flashed[len(img):]) <= {0xFF})

    def test_exact_match_accepted(self):
        self.assertTrue(self.accepts(b"\x01\x02", b"\x01\x02"))

    def test_ff_block_padding_accepted(self):
        self.assertTrue(self.accepts(b"\x01\x02", b"\x01\x02" + b"\xFF" * 208))

    def test_a_different_image_of_the_same_length_rejected(self):
        self.assertFalse(self.accepts(b"\x01\x02", b"\x01\x03"))

    def test_a_longer_image_with_real_trailing_data_rejected(self):
        self.assertFalse(self.accepts(b"\x01\x02", b"\x01\x02\x03"))

    def test_zero_padding_is_not_accepted_as_block_padding(self):
        # UF2 pads with 0xFF because that is erased flash; 0x00 would mean the
        # candidate is genuinely a different, shorter image.
        self.assertFalse(self.accepts(b"\x01\x02", b"\x01\x02" + b"\x00" * 208))

    def test_a_candidate_longer_than_what_was_flashed_rejected(self):
        self.assertFalse(self.accepts(b"\x01\x02\x03", b"\x01\x02"))


if __name__ == "__main__":
    unittest.main()
