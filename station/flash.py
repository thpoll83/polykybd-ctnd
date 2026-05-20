# SPDX-License-Identifier: GPL-2.0-only
import glob
import shutil
import subprocess
import time

import RPi.GPIO as GPIO

from .config import (
    LEFT_BOOTSEL_PIN, LEFT_RUN_PIN, LEFT_USB_PORT,
    RIGHT_BOOTSEL_PIN, RIGHT_RUN_PIN, RIGHT_USB_PORT,
    USB_HUB_LOCATION, MASS_STORAGE_LABEL,
)

_SIDES = {
    "left":  (LEFT_RUN_PIN,  LEFT_BOOTSEL_PIN,  LEFT_USB_PORT),
    "right": (RIGHT_RUN_PIN, RIGHT_BOOTSEL_PIN, RIGHT_USB_PORT),
}


class FlashController:
    def __init__(self):
        GPIO.setmode(GPIO.BCM)
        for pin in [LEFT_RUN_PIN, LEFT_BOOTSEL_PIN, RIGHT_RUN_PIN, RIGHT_BOOTSEL_PIN]:
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH)

    def _usb_power(self, port: int, on: bool) -> None:
        subprocess.run(
            ["uhubctl", "-l", USB_HUB_LOCATION, "-p", str(port), "-a", "on" if on else "off"],
            check=True, capture_output=True,
        )

    def _enter_bootsel(self, run_pin: int, bootsel_pin: int) -> None:
        GPIO.output(bootsel_pin, GPIO.LOW)
        time.sleep(0.05)
        GPIO.output(run_pin, GPIO.LOW)
        time.sleep(0.05)
        GPIO.output(run_pin, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.output(bootsel_pin, GPIO.HIGH)

    def _await_mount(self, timeout: int = 10) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            matches = glob.glob(f"/media/**/{MASS_STORAGE_LABEL}", recursive=True)
            if matches:
                return matches[0]
            time.sleep(0.3)
        raise TimeoutError(f"Mass storage '{MASS_STORAGE_LABEL}' not found after {timeout}s")

    def flash(self, side: str, uf2_path: str, log=print) -> None:
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}' — expected 'left' or 'right'")
        run_pin, bootsel_pin, usb_port = _SIDES[side]

        log(f"[flash:{side}] powering off USB port {usb_port}")
        self._usb_power(usb_port, False)
        time.sleep(0.3)

        log(f"[flash:{side}] entering BOOTSEL")
        self._enter_bootsel(run_pin, bootsel_pin)

        log(f"[flash:{side}] powering on USB port {usb_port}")
        self._usb_power(usb_port, True)

        log(f"[flash:{side}] waiting for mass storage")
        mount = self._await_mount()

        log(f"[flash:{side}] mounted at {mount} — writing {uf2_path}")
        shutil.copy(uf2_path, mount)

        log(f"[flash:{side}] complete — waiting for reboot")
        time.sleep(2.5)

    def cleanup(self) -> None:
        GPIO.cleanup()
