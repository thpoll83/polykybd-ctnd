# SPDX-License-Identifier: GPL-2.0-only
import subprocess
import time
from pathlib import Path

import RPi.GPIO as GPIO

from .config import (
    LEFT_BOOTSEL_PIN, LEFT_RUN_PIN, LEFT_USB_PORT,
    RIGHT_BOOTSEL_PIN, RIGHT_RUN_PIN, RIGHT_USB_PORT,
    USB_HUB_LOCATION, GPIO_INVERTED,
)

# Signal levels for asserting / releasing active-low pins.
# Inverted (2N2222 NPN BJT low-side switch, default):
#   GPIO HIGH → BJT saturated → pin ~0.1 V = asserted.
#   GPIO LOW  → BJT off → pin held HIGH by RP2040 internal pull-up = released.
# Direct (open-drain or direct wire, gpio.inverted: false):
#   GPIO LOW  = pin LOW = asserted.
#   GPIO HIGH = pin HIGH = released.
_ASSERT  = GPIO.HIGH if GPIO_INVERTED else GPIO.LOW
_RELEASE = GPIO.LOW  if GPIO_INVERTED else GPIO.HIGH

_SIDES = {
    "left":  (LEFT_RUN_PIN,  LEFT_BOOTSEL_PIN,  LEFT_USB_PORT),
    "right": (RIGHT_RUN_PIN, RIGHT_BOOTSEL_PIN, RIGHT_USB_PORT),
}

# Persist BOOTSEL and RUN pin state across FlashController instances so that
# asserting either pin via the UI survives subsequent calls.
_bootsel_asserted = {"left": False, "right": False}
_run_asserted     = {"left": False, "right": False}
_gpio_ready = False


def _ensure_gpio():
    global _gpio_ready
    if _gpio_ready:
        return
    # Quiet the "channel already in use" RuntimeWarning RPi.GPIO emits when the
    # pins were configured by a previous run/instance. Besides being noise, the
    # warning text includes this file's absolute path (leaking the rig's home
    # directory) into stdout — which the CI captures into the HIL log.
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    # All pins idle at _RELEASE so boards run freely and BOOTSEL is up.
    for pin in [LEFT_RUN_PIN, RIGHT_RUN_PIN, LEFT_BOOTSEL_PIN, RIGHT_BOOTSEL_PIN]:
        GPIO.setup(pin, GPIO.OUT, initial=_RELEASE)
    _gpio_ready = True


class FlashController:
    def __init__(self):
        _ensure_gpio()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _usb_power(self, port: int, on: bool) -> None:
        subprocess.run(
            ["sudo", "uhubctl", "-l", USB_HUB_LOCATION, "-p", str(port), "-a", "on" if on else "off"],
            check=True, capture_output=True,
        )

    def _flash_with_picotool(self, firmware_path: str, log) -> None:
        # picotool talks to the RP2040 bootrom over the PICOBOOT USB interface
        # (VID:PID 2e8a:0003), which the bootrom exposes whenever the chip is in
        # BOOTSEL mode. This is independent of whether valid firmware is already
        # flashed (a blank board powers straight into BOOTSEL) and independent of
        # any OS mass-storage auto-mount — so it works even when no firmware is
        # present, and is not affected by the desktop "Removable Medium Found"
        # prompt that prevents /media/**/RPI-RP2 from appearing.
        #
        # picotool accepts .uf2, .bin and .elf. It writes diagnostics to stdout,
        # not stderr.
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

    # ── Public API ────────────────────────────────────────────────────────────

    def flash(self, side: str, firmware_path: str, log=print) -> None:
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}' — expected 'left' or 'right'")
        ext = Path(firmware_path).suffix.lower()
        if ext not in (".uf2", ".bin"):
            raise ValueError(f"Unsupported firmware format '{ext}' — expected .uf2 or .bin")

        run_pin, bootsel_pin, usb_port = _SIDES[side]

        # RP2040 BOOTSEL entry sequence (direct drive, active-low pins):
        #   1. Assert RESET (RUN LOW)
        #   2. Assert BOOTSEL while RESET is held (BOOTSEL LOW)
        #   3. Release RESET (RUN HIGH) — board boots with BOOTSEL held → BOOTSEL mode
        #   4. Release BOOTSEL after enumeration
        log(f"[flash:{side}] entering BOOTSEL (RUN=BCM{run_pin}, BOOTSEL=BCM{bootsel_pin})")
        GPIO.output(run_pin, _ASSERT)      # 1. assert reset
        time.sleep(0.05)
        GPIO.output(bootsel_pin, _ASSERT)  # 2. assert BOOTSEL while reset held
        time.sleep(0.05)
        GPIO.output(run_pin, _RELEASE)     # 3. release reset → board boots into BOOTSEL mode

        log(f"[flash:{side}] waiting for BOOTSEL enumeration")
        time.sleep(2.0)  # hold BOOTSEL until the board has fully enumerated

        # 4. Restore BOOTSEL to whatever the user has set, not necessarily released.
        GPIO.output(bootsel_pin, _ASSERT if _bootsel_asserted[side] else _RELEASE)

        # Both .uf2 and .bin are loaded via picotool over the PICOBOOT interface
        # (the 2 s hold above covers USB enumeration). This avoids any dependency
        # on the OS auto-mounting the RP2040 mass-storage volume, which on the
        # kiosk desktop is intercepted by the file manager's "Removable Medium
        # Found" prompt and never lands under /media.
        self._flash_with_picotool(firmware_path, log)

        log(f"[flash:{side}] complete — waiting for reboot")
        time.sleep(2.5)

    def set_bootsel(self, side: str, asserted: bool, log=print) -> None:
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}'")
        _, bootsel_pin, _ = _SIDES[side]
        _bootsel_asserted[side] = asserted
        GPIO.output(bootsel_pin, _ASSERT if asserted else _RELEASE)
        log(f"[bootsel:{side}] BCM{bootsel_pin} → {'asserted' if asserted else 'released'}")

    def set_run(self, side: str, asserted: bool, log=print) -> None:
        if side not in _SIDES:
            raise ValueError(f"Unknown side '{side}'")
        run_pin, _, _ = _SIDES[side]
        _run_asserted[side] = asserted
        GPIO.output(run_pin, _ASSERT if asserted else _RELEASE)
        log(f"[run:{side}] BCM{run_pin} → {'asserted (reset held)' if asserted else 'released'}")

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
        run_pin, _, _usb_port = _SIDES[side]

        # USB data cut/restore is available via usb_power() if a clean
        # host-side disconnect is needed:
        #   self._usb_power(_usb_port, False); time.sleep(0.3)

        log(f"[reset:{side}] asserting reset (BCM{run_pin})")
        GPIO.output(run_pin, _ASSERT)
        _run_asserted[side] = True
        time.sleep(0.1)
        log(f"[reset:{side}] releasing reset (BCM{run_pin})")
        GPIO.output(run_pin, _RELEASE)
        _run_asserted[side] = False
        time.sleep(0.8)
        log(f"[reset:{side}] done")

    def cleanup(self) -> None:
        # Restore each pin to its current tracked toggle state so that a
        # user-held BOOTSEL or RUN pin survives the end of a flash/reset call.
        # GPIO stays initialised for the service's lifetime — only
        # gpio_cleanup_final() does the actual GPIO.cleanup().
        for side, (run_pin, bootsel_pin, _) in _SIDES.items():
            try:
                GPIO.output(run_pin,     _ASSERT if _run_asserted[side]     else _RELEASE)
            except Exception:
                pass
            try:
                GPIO.output(bootsel_pin, _ASSERT if _bootsel_asserted[side] else _RELEASE)
            except Exception:
                pass

    @staticmethod
    def gpio_cleanup_final() -> None:
        """Release all GPIO — call only on service exit."""
        global _gpio_ready
        try:
            GPIO.cleanup()
        except Exception:
            pass
        _gpio_ready = False
