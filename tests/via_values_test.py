"""Offline checks of station/via_values.py against the firmware's wire format
(qmk_firmware keyboards/polykybd/lib/hid/hid_com.c legacy_command_kb)."""
import unittest

from station import via_values as V


class RequestTest(unittest.TestCase):
    def test_request_bytes(self):
        self.assertEqual(V.rgb_get_request(V.EFFECT), bytes([0x08, 3, 2, 0, 0]))
        self.assertEqual(V.rgb_set_request(V.COLOR, 10, 300), bytes([0x07, 3, 4, 10, 300 & 0xFF]))
        self.assertEqual(V.uptime_request(), bytes([0x02, 0x01, 0, 0, 0, 0]))
        self.assertEqual(V.switch_matrix_request(), bytes([0x02, 0x03, 0]))
        self.assertEqual(V.indication_request(5), bytes([0x03, 0x05, 5]))


class ParseTest(unittest.TestCase):
    def test_rgb(self):
        self.assertEqual(V.parse_rgb(bytes([0x08, 3, 2, 7, 0]) + bytes(59), V.EFFECT), (7,))
        self.assertEqual(V.parse_rgb(bytes([0x08, 3, 4, 10, 200]) + bytes(59), V.COLOR), (10, 200))
        self.assertIsNone(V.parse_rgb(bytes([0xFF, 3, 2, 7, 0]), V.EFFECT))
        self.assertIsNone(V.parse_rgb(bytes([0x08, 3, 1, 7, 0]), V.EFFECT))
        self.assertIsNone(V.parse_rgb(None, V.EFFECT))

    def test_uptime_is_big_endian(self):
        self.assertEqual(V.parse_uptime(bytes([0x02, 0x01, 0x00, 0x01, 0x11, 0x70])), 70000)
        self.assertIsNone(V.parse_uptime(bytes([0xFF, 0x01, 0, 0, 0, 0])))

    def test_switch_matrix(self):
        resp = bytes([0x02, 0x03, 0]) + bytes([0, 4, 0, 0, 0, 0x80, 0, 0, 0, 0]) + bytes(51)
        self.assertEqual(V.parse_switch_matrix(resp, 10), [0, 4, 0, 0, 0, 0x80, 0, 0, 0, 0])
        self.assertIsNone(V.parse_switch_matrix(bytes([0xFF, 0x03, 0]) + bytes(61), 10))
        self.assertTrue(V.is_unhandled(bytes([0xFF, 0x03])))
        self.assertFalse(V.is_unhandled(None))

    def test_brightness_round_trip(self):
        # split72 ceiling 100: a set of 255 stores 100, which reads as 255.
        self.assertEqual(V.brightness_round_trip(255, 100), 255)
        # A value read back from a get survives a set exactly (stored 1 -> 3 -> 1).
        for stored in range(101):
            got = (stored * 255 + 99) // 100
            self.assertEqual(V.brightness_round_trip(got, 100), got, stored)


if __name__ == "__main__":
    unittest.main()
