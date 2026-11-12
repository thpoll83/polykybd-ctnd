# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the USB MIDI interface check (no hardware).

A fake /sys/bus/usb/devices tree with the layout Linux writes: a device
directory with idVendor/idProduct, interface directories ``<dev>:1.<n>`` with
bInterfaceClass/bInterfaceSubClass and ``ep_XX`` endpoint directories.
"""
import os
import sys
import tempfile
import types
import unittest

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import usb_midi                          # noqa: E402
from station.hil_tests import TESTS, skip_reason      # noqa: E402


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text + "\n")


def make_device(root, name, vid="2021", pid="2007", interfaces=()):
    """interfaces: (class, subclass, [endpoint addresses]) per interface."""
    dev = os.path.join(root, name)
    _write(os.path.join(dev, "idVendor"), vid)
    _write(os.path.join(dev, "idProduct"), pid)
    os.makedirs(os.path.join(dev, "ep_00"))  # the control endpoint sits on the device
    for n, (cls, sub, eps) in enumerate(interfaces):
        itf = os.path.join(dev, f"{name}:1.{n}")
        _write(os.path.join(itf, "bInterfaceClass"), cls)
        _write(os.path.join(itf, "bInterfaceSubClass"), sub)
        for ep in eps:
            os.makedirs(os.path.join(itf, f"ep_{ep:02x}"))
    return dev


HID = ("03", "00", [0x81])
SEND_ONLY = [HID, ("01", "01", []), ("01", "03", [0x83]), ("02", "02", [0x84])]


class UsbMidiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_only_the_keyboard(self):
        make_device(self.root, "1-1", vid="1d6b", pid="0002")
        kb = make_device(self.root, "1-1.2", interfaces=SEND_ONLY)
        os.makedirs(os.path.join(self.root, "usb1"))  # no idVendor: skipped
        self.assertEqual(usb_midi.keyboard_devices(self.root), [kb])

    def test_send_only_passes(self):
        dev = make_device(self.root, "1-1.2", interfaces=SEND_ONLY)
        itfs = usb_midi.audio_interfaces(dev)
        self.assertEqual([(i["subclass"], i["endpoints"]) for i in itfs],
                         [("01", []), ("03", [0x83])])
        self.assertEqual(usb_midi.check_send_only(itfs), [])

    def test_out_endpoint_fails(self):
        # QMK's default descriptor: MIDIStreaming with an OUT and an IN endpoint.
        dev = make_device(self.root, "1-1.2", interfaces=[HID, ("01", "01", []), ("01", "03", [0x03, 0x83])])
        problems = usb_midi.check_send_only(usb_midi.audio_interfaces(dev))
        self.assertEqual(len(problems), 1)
        self.assertIn("MIDI_SEND_ONLY", problems[0])

    def test_missing_function_fails(self):
        dev = make_device(self.root, "1-1.2", interfaces=[HID])
        problems = usb_midi.check_send_only(usb_midi.audio_interfaces(dev))
        self.assertEqual(problems, ["0 AudioControl interfaces (expected 1)",
                                    "0 MIDIStreaming interfaces (expected 1)"])

    def test_gate_skips_older_firmware(self):
        entry = next(t for t in TESTS if t["fn"].__name__ == "test_usb_midi_send_only")
        self.assertEqual(entry["min_fw"], "1.13.0")
        self.assertTrue(skip_reason(entry, {"protocol": 32, "fw": "1.12.0"}))
        self.assertIsNone(skip_reason(entry, {"protocol": 32, "fw": "1.13.0"}))

    def test_runs_after_the_fresh_boot_marker(self):
        names = [t["fn"].__name__ for t in TESTS]
        self.assertLess(names.index("test_fresh_boot_marker"), names.index("test_usb_midi_send_only"))


if __name__ == "__main__":
    unittest.main()
