# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the firmware-update client's version cross-check.

``FW_UP_GET_VERSION`` (cmd 0x43) was read and thrown away for as long as the
rig has driven the HID update path. Comparing it against the version compiled
into the image being staged is what turns it into a check — specifically the one
that catches a flash that silently did not take, which is otherwise invisible:
every test build reports the same ``FW_VERSION`` and the UF2 filenames carry no
version at all.

The parser is what needs pinning: it reads the firmware's GET_ID literal
(``"P\\x06." POLY_KB_NAME " " FW_VERSION " P" PROTOCOL_VERSION " HW" DEVICE_VER``)
straight out of the binary, so it has to survive both real image noise and a
board rename, and it must fail *softly* — returning None so the caller skips the
comparison rather than failing a run over a parsing miss.
"""
import sys
import types
import unittest

# station.fw_update imports hidapi at module load; stub it (see hil_tests_test).
if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station.fw_update import caps_from_image  # noqa: E402

# The literal as the compiler lays it down, NUL-terminated, surrounded by the
# binary noise of a real image.
IMAGE = (bytes(range(256)) + b"P\x06.Split72 0.15.5 P12 HW1 \x00"
         + b"\xff" * 64 + b"some other rodata\x00")


class CapsFromImageTest(unittest.TestCase):
    def test_reads_the_get_id_literal_out_of_a_realistic_image(self):
        self.assertEqual(caps_from_image(IMAGE),
                         {"board": "Split72", "fw": "0.15.5", "protocol": 12})

    def test_handles_the_other_board(self):
        self.assertEqual(
            caps_from_image(b"...P\x06.Split42 1.0.0 P12 HW2 \x00")["board"],
            "Split42")

    def test_a_prerelease_suffix_survives(self):
        self.assertEqual(
            caps_from_image(b"P\x06.Split72 0.16.0-rc2 P13 HW1 \x00")["fw"],
            "0.16.0-rc2")

    def test_an_unparsable_image_fails_soft(self):
        # None means "skip the comparison", not "the versions differ" — a parsing
        # miss must never fail a run on its own.
        self.assertIsNone(caps_from_image(b"\x00" * 4096))
        self.assertIsNone(caps_from_image(b""))
        self.assertIsNone(caps_from_image(None))

    def test_a_mismatch_is_detectable(self):
        # The comparison the caller makes: device version vs image version.
        self.assertNotEqual(caps_from_image(IMAGE)["fw"], "0.15.4")




class RequireIdentityTest(unittest.TestCase):
    """The UI's "Measure phase now" stages only an image it can match to the
    running build: without a GET_ID literal it must refuse before BEGIN."""

    def _run(self, require_identity):
        import tempfile
        import station.fw_update as fw
        sent = []

        class FakeLink:
            last_error = None
            def __init__(self, *a): pass
            def open(self, *a, **k): return True
            def close(self): pass
            def is_open(self): return True
            def xfer(self, data, *a, **k):
                sent.append(data[1])
                return bytes([0x50, data[1], fw.NACK])   # BEGIN refused: ends the run

        orig = (fw._FwLink, fw._get_version)
        fw._FwLink = FakeLink
        fw._get_version = lambda link, log: {"version": "0.15.5", "size": 1, "crc": 0}
        try:
            with tempfile.NamedTemporaryFile(suffix=".bin") as f:
                f.write(b"\x00" * 1024)   # no GET_ID literal
                f.flush()
                logs = []
                ok = fw.stage_and_verify(f.name, logs.append, require_identity=require_identity)
        finally:
            fw._FwLink, fw._get_version = orig
        return ok, sent, logs

    def test_strict_refuses_an_unidentifiable_image_before_begin(self):
        ok, sent, logs = self._run(True)
        self.assertFalse(ok)
        self.assertNotIn(0x40, sent)   # CMD_FW_UP_BEGIN never sent
        self.assertTrue(any("refusing to stage" in line for line in logs))

    def test_default_still_skips_the_check_and_proceeds(self):
        _, sent, logs = self._run(False)
        self.assertIn(0x40, sent)
        self.assertTrue(any("skipping the version cross-check" in line for line in logs))


class MissingKeyboardHintTest(unittest.TestCase):
    """The reason logged when no Raw HID interface shows up."""

    def _tree(self, devices):
        import os
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for i, (vid, pid) in enumerate(devices):
            d = os.path.join(tmp.name, f"1-1.{i}")
            os.makedirs(d)
            for name, value in (("idVendor", vid), ("idProduct", pid)):
                with open(os.path.join(d, name), "w") as f:
                    f.write(value + "\n")
        return tmp.name

    def test_bootsel_half_is_named(self):
        from station.fw_update import missing_keyboard_hint
        hint = missing_keyboard_hint(0x2021, 0x2007, self._tree([("2e8a", "0003")]))
        self.assertIn("BOOTSEL", hint)

    def test_keyboard_without_raw_hid(self):
        from station.fw_update import missing_keyboard_hint
        hint = missing_keyboard_hint(0x2021, 0x2007, self._tree([("2021", "2007")]))
        self.assertIn("no Raw HID interface", hint)

    def test_nothing_on_usb(self):
        from station.fw_update import missing_keyboard_hint
        hint = missing_keyboard_hint(0x2021, 0x2007, self._tree([("1d6b", "0002")]))
        self.assertIn("no PolyKybd on USB", hint)


if __name__ == "__main__":
    unittest.main()


class BeginTimeoutTest(unittest.TestCase):
    """A BEGIN that the master keeps answering with '~' (the slave has not
    confirmed its erase) names the slave half in the failure."""

    def _run(self, status):
        import tempfile
        import types as _types
        import station.fw_update as fw

        class FakeLink:
            last_error = None
            def __init__(self, *a): pass
            def open(self, *a, **k): return True
            def close(self): pass
            def is_open(self): return True
            def xfer(self, data, *a, **k):
                return bytes([0x50, data[1], status])

        clock = {"t": 0.0}
        def monotonic():
            clock["t"] += 10.0   # each poll moves the clock 10 s: the 90 s deadline passes
            return clock["t"]
        fake_time = _types.SimpleNamespace(monotonic=monotonic, sleep=lambda s: None,
                                           time=lambda: 0.0)
        orig = (fw._FwLink, fw._get_version, fw.time)
        fw._FwLink, fw.time = FakeLink, fake_time
        fw._get_version = lambda link, log: {"version": "0.15.5", "size": 1, "crc": 0}
        try:
            with tempfile.NamedTemporaryFile(suffix=".bin") as f:
                f.write(IMAGE)
                f.flush()
                logs = []
                ok = fw.stage_and_verify(f.name, logs.append)
        finally:
            fw._FwLink, fw._get_version, fw.time = orig
        return ok, [line for line in logs if "FAIL" in line]

    def test_busy_until_the_deadline_names_the_slave(self):
        import station.fw_update as fw
        ok, fails = self._run(fw.BUSY)
        self.assertFalse(ok)
        self.assertEqual(len(fails), 1)
        self.assertIn("slave half never confirmed its erase", fails[0])
