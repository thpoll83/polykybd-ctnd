# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the flasher's BOOTSEL device targeting (no GPIO, no USB).

The rig flashes one half at a time. When the OTHER half is already sitting in
BOOTSEL, an untargeted ``picotool load`` refuses to choose ("Command requires a
single RP-series device to be targeted", exit 248) and every HIL run aborts at
the first flash (qmk#363, 2026-10-29). The flasher now records which bootrom
appeared after it reset the half and passes that one to picotool.
"""
import subprocess
import sys
import types
import unittest
from unittest import mock

if "RPi" not in sys.modules:  # pragma: no cover - environment shim
    _rpi, _gpio = types.ModuleType("RPi"), types.ModuleType("RPi.GPIO")
    for _name in ("setmode", "setup", "output", "cleanup", "setwarnings"):
        setattr(_gpio, _name, lambda *a, **k: None)
    for _name in ("BCM", "OUT", "HIGH", "LOW"):
        setattr(_gpio, _name, 0)
    _rpi.GPIO = _gpio
    sys.modules["RPi"], sys.modules["RPi.GPIO"] = _rpi, _gpio

from station import flash  # noqa: E402

LSUSB = """Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 014: ID 2e8a:0003 Raspberry Pi RP2 Boot
Bus 001 Device 009: ID 2021:2007 PolyKybd Split72
Bus 001 Device 017: ID 2e8a:0003 Raspberry Pi RP2 Boot
"""


class FakeFlasher(flash.FlashController):
    """lsusb answers come from a script: one set per poll, the last repeating.
    An entry that is an exception is raised instead, like a failed lsusb."""

    def __init__(self, polls):
        super().__init__()
        self._polls = list(polls)

    def _scan_bootsel(self):
        item = self._polls.pop(0) if len(self._polls) > 1 else self._polls[0]
        if isinstance(item, Exception):
            raise item
        return set(item)


class ParseTest(unittest.TestCase):
    def test_parses_bus_and_address_of_each_bootrom(self):
        self.assertEqual(flash.parse_bootsel_devices(LSUSB), {(1, 14), (1, 17)})

    def test_no_bootrom(self):
        self.assertEqual(flash.parse_bootsel_devices("Bus 001 Device 009: ID 2021:2007 x\n"), set())

    def test_target_arguments(self):
        self.assertEqual(flash._picotool_target((1, 17)), ["--bus", "1", "--address", "17"])
        self.assertEqual(flash._picotool_target(None), [])


class WaitTest(unittest.TestCase):
    def wait(self, polls, before):
        return FakeFlasher(polls)._wait_for_bootsel(
            "right", 22, 23, log=lambda *_: None, timeout=1.0, poll=0.0, before=before)

    def test_picks_the_new_device_beside_one_already_in_bootsel(self):
        # The field case: the left half sits in BOOTSEL at (1, 14) the whole time.
        self.assertEqual(self.wait([{(1, 14)}, {(1, 14), (1, 18)}], before={(1, 14)}), (1, 18))

    def test_half_already_in_bootsel_reenumerates_at_a_new_address(self):
        # Resetting a half that was in BOOTSEL drops (1, 14) and brings it back new.
        self.assertEqual(self.wait([set(), {(1, 19)}], before={(1, 14)}), (1, 19))

    def test_timeout_names_the_devices_that_were_already_there(self):
        with self.assertRaisesRegex(RuntimeError, "1 other RP2040"):
            self.wait([{(1, 14)}], before={(1, 14)})

    def test_two_new_devices_is_an_error_not_a_guess(self):
        with self.assertRaisesRegex(RuntimeError, "cannot tell which"):
            self.wait([{(1, 20), (1, 21)}], before=set())


class LsusbFailureTest(unittest.TestCase):
    """An unreadable lsusb must never pass for "no bootrom" (review on ctnd#130)."""

    def test_scan_raises_on_nonzero_exit(self):
        res = subprocess.CompletedProcess(["lsusb"], 1, stdout="", stderr="boom")
        with mock.patch.object(subprocess, "run", return_value=res):
            with self.assertRaisesRegex(RuntimeError, "lsusb exited 1"):
                flash.FlashController()._scan_bootsel()

    def test_scan_raises_on_timeout(self):
        with mock.patch.object(subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("lsusb", 5)):
            with self.assertRaisesRegex(RuntimeError, "lsusb failed"):
                flash.FlashController()._scan_bootsel()

    def test_best_effort_count_still_returns_zero(self):
        with mock.patch.object(subprocess, "run", side_effect=OSError("no lsusb")):
            self.assertEqual(flash.FlashController()._count_bootsel_devices(), 0)

    def test_flash_refuses_without_a_baseline(self):
        # The field risk: the other half is in BOOTSEL, the baseline read fails,
        # and the first good poll would pass that half off as this one.
        f = FakeFlasher([RuntimeError("lsusb failed"), {(1, 14)}])
        with mock.patch.object(flash.time, "sleep"), \
             mock.patch.object(f, "_flash_with_picotool") as load:
            with self.assertRaisesRegex(RuntimeError, "lsusb failed"):
                f.flash("right", "fw.uf2", log=lambda *_: None)
        load.assert_not_called()

    def test_a_failed_poll_is_skipped_not_counted(self):
        f = FakeFlasher([RuntimeError("lsusb failed"), {(1, 14), (1, 18)}])
        self.assertEqual(f._wait_for_bootsel("right", 22, 23, log=lambda *_: None,
                                             timeout=1.0, poll=0.0, before={(1, 14)}),
                         (1, 18))


class PicotoolCallTest(unittest.TestCase):
    def test_load_and_reboot_both_target_the_device(self):
        calls = []
        with mock.patch.object(subprocess, "run", side_effect=lambda cmd, **k: calls.append(cmd)):
            flash.FlashController()._flash_with_picotool("fw.uf2", lambda *_: None, (1, 18))
        self.assertEqual(calls, [
            ["sudo", "picotool", "load", "fw.uf2", "--update", "--bus", "1", "--address", "18"],
            ["sudo", "picotool", "reboot", "--bus", "1", "--address", "18"],
        ])

    def test_flash_passes_the_device_it_waited_for(self):
        f = FakeFlasher([{(1, 14)}, {(1, 14), (1, 18)}])
        seen = {}
        with mock.patch.object(flash.time, "sleep"), \
             mock.patch.object(f, "_flash_with_picotool",
                               side_effect=lambda path, log, device: seen.update(device=device)):
            f.flash("right", "fw.uf2", log=lambda *_: None)
        self.assertEqual(seen["device"], (1, 18))


if __name__ == "__main__":
    unittest.main()
