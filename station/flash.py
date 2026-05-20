# SPDX-License-Identifier: GPL-2.0-only
import glob
import shutil
import subprocess
import time
from pathlib import Path

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
            ["sudo", "uhubctl", "-l", USB_HUB_LOCATION, "-p", str(port), "-a", "on" if on else "off"],
            check=True, capture_output=True,
        )

    def _await_mount(self, timeout: int = 10) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            matches = glob.glob(f"/media/**/{MASS_STORAGE_LABEL}", recursive=True)
            if matches:
                return matches[0]
            time.sleep(0.3)
        raise TimeoutError(f"Mass storage '{MASS_STORAGE_LABEL}' not found after {timeout}s")

    def _flash_uf2(self, firmware_path: str, log) -> None:
        mount = self._await_mount()
        log(f"[flash] mounted at {mount} — writing {firmware_path}")
        shutil.copy(firmware_path, mount)

    def _flash_bin(self, firmware_path: str, log) -> None:
        # picotool talks to the RP2040 over USB in BOOTSEL mode.
        # It writes diagnostics to stdout, not stderr.
        log(f"[flash] loading with picotool: {firmware_path}")
        try:
            subprocess.run(
                ["sudo", "picotool", "load", firmware_path, "--update"],
                check=True, capture_output=True,
            )
        except subprocess.CalledProcessError as exc:
            out = (exc.stdout or b"").decode(errors="replace").strip()
            err = (exc.stderr or b"").decode(errors="replace").strip()
            raise RuntimeError(
                f"picotool exited {exc.returncode}: {err or out or '(no output)'}"
            ) from exc
        subprocess.run(["sudo", "picotool", "reboot"], check=True, capture_output=True)

    def flash(self, side: str, firmware_path: str, log=print) -> None:
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}' — expected 'left' or 'right'")
        ext = Path(firmware_path).suffix.lower()
        if ext not in (".uf2", ".bin"):
            raise ValueError(f"Unsupported firmware format '{ext}' — expected .uf2 or .bin")

        run_pin, bootsel_pin, usb_port = _SIDES[side]

        # Power off first so the device has no USB power at all.
        log(f"[flash:{side}] powering off USB port {usb_port}")
        self._usb_power(usb_port, False)
        time.sleep(0.5)

        # Assert BOOTSEL and toggle RUN while USB is still off.
        # BOOTSEL must still be held when USB (and therefore board power)
        # comes back — that is when the RP2040 samples the pin.
        log(f"[flash:{side}] entering BOOTSEL (RUN=BCM{run_pin}, BOOTSEL=BCM{bootsel_pin})")
        GPIO.output(bootsel_pin, GPIO.LOW)
        time.sleep(0.05)
        GPIO.output(run_pin, GPIO.LOW)
        time.sleep(0.05)
        GPIO.output(run_pin, GPIO.HIGH)

        # Power on with BOOTSEL still held → device boots into BOOTSEL mode.
        log(f"[flash:{side}] powering on USB port {usb_port}")
        self._usb_power(usb_port, True)
        time.sleep(0.2)
        GPIO.output(bootsel_pin, GPIO.HIGH)  # release after boot has started

        if ext == ".uf2":
            log(f"[flash:{side}] waiting for mass storage")
            self._flash_uf2(firmware_path, log)
        else:
            time.sleep(1.8)  # wait for USB enumeration (2 s total with the 0.2 s above)
            self._flash_bin(firmware_path, log)

        log(f"[flash:{side}] complete — waiting for reboot")
        time.sleep(2.5)

    def usb_power(self, side: str, on: bool, log=print) -> None:
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}'")
        _, _, usb_port = _SIDES[side]
        log(f"[usb:{side}] port {usb_port} → {'on' if on else 'off'}")
        self._usb_power(usb_port, on)

    def query_usb_state(self, side: str) -> bool | None:
        """Return True/False for the current USB power state, or None if unreadable."""
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}'")
        _, _, usb_port = _SIDES[side]
        result = subprocess.run(
            ["sudo", "uhubctl", "-l", USB_HUB_LOCATION, "-p", str(usb_port)],
            capture_output=True, text=True,
        )
        for line in result.stdout.splitlines():
            if f"Port {usb_port}:" in line:
                return "power" in line
        return None

    def reset(self, side: str, log=print) -> None:
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}'")
        run_pin, _, _ = _SIDES[side]
        log(f"[reset:{side}] asserting RUN low (BCM{run_pin})")
        GPIO.output(run_pin, GPIO.LOW)
        time.sleep(0.2)
        GPIO.output(run_pin, GPIO.HIGH)
        # Hold HIGH for 500 ms so the RP2040 is well into its boot sequence
        # before GPIO.cleanup() releases the pin to a floating input.
        time.sleep(0.5)
        log(f"[reset:{side}] released RUN high (BCM{run_pin})")

    def cleanup(self) -> None:
        # Drive all pins HIGH before switching to input so the RP2040's
        # internal pull-ups don't have to fight any residual LOW drive
        # from a previous operation.
        for pin in [LEFT_RUN_PIN, LEFT_BOOTSEL_PIN, RIGHT_RUN_PIN, RIGHT_BOOTSEL_PIN]:
            try:
                GPIO.output(pin, GPIO.HIGH)
            except Exception:
                pass
        GPIO.cleanup()
