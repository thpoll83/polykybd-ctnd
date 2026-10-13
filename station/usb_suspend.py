# SPDX-License-Identifier: GPL-2.0-only
"""USB-suspend the master from the Pi, without cutting its power.

The Pi 4's own ports cannot switch VBUS (``uhubctl -a off`` reports success and
the rail stays up), so the rig cannot power a half down. It does not need to: a
host that sleeps leaves VBUS on and stops the bus, and that bus suspend is what
runs the firmware's ``suspend_power_down_kb()``, its EEPROM flush and the core1
suspend hold. The kernel's runtime PM produces the same bus state:

1. unbind ``usbhid`` from every interface of the keyboard. A bound ``usbhid``
   whose input device is open (X, the kiosk) holds a PM reference and the device
   never autosuspends; an interface with no driver holds none.
2. ``power/autosuspend_delay_ms`` = 0 and ``power/control`` = ``auto``. The USB
   core then suspends the port, and ``power/runtime_status`` reads
   ``suspended``. That file is the proof the suspend happened.
3. Wake: ``power/control`` = ``on`` resumes the port, then ``usbhid`` is bound
   again and the hidraw nodes come back.

Every sysfs write goes through ``sudo tee``, as ``uhubctl`` and ``picotool``
already do in :mod:`station.flash`. ``root`` and ``write`` are parameters so the
offline tests can run this against a fake tree.
"""
import os
import subprocess
import time
from typing import Callable

SYSFS_USB = "/sys/bus/usb"
USBHID = "usbhid"


def sudo_write(path: str, value: str) -> None:
    """Write one sysfs attribute as root."""
    subprocess.run(["sudo", "tee", path], input=value.encode("ascii"),
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)


def _read(path: str) -> str:
    with open(path, encoding="ascii") as f:
        return f.read().strip()


def find_device(vendor_id: int, product_id: int, root: str = SYSFS_USB):
    """The sysfs name (e.g. ``1-1.3``) of the USB device with this VID:PID, or None.

    Interface entries (``1-1.3:1.0``) carry no idVendor and are skipped. More
    than one match is an error: the rig has exactly one master enumerated."""
    devices = os.path.join(root, "devices")
    want = (f"{vendor_id:04x}", f"{product_id:04x}")
    found = []
    for name in sorted(os.listdir(devices)):
        if ":" in name:
            continue
        try:
            ids = (_read(os.path.join(devices, name, "idVendor")).lower(),
                   _read(os.path.join(devices, name, "idProduct")).lower())
        except OSError:
            continue
        if ids == want:
            found.append(name)
    if len(found) > 1:
        raise RuntimeError(f"{len(found)} USB devices match {want[0]}:{want[1]} "
                           f"({', '.join(found)}); expected one master")
    return found[0] if found else None


def bound_interfaces(device: str, driver: str = USBHID, root: str = SYSFS_USB) -> list:
    """The device's interfaces currently bound to ``driver``, sorted."""
    devices = os.path.join(root, "devices")
    out = []
    for name in sorted(os.listdir(devices)):
        if not name.startswith(device + ":"):
            continue
        link = os.path.join(devices, name, "driver")
        if os.path.islink(link) and os.path.basename(os.readlink(link)) == driver:
            out.append(name)
    return out


def runtime_status(device: str, root: str = SYSFS_USB) -> str:
    """``active``, ``suspended``, ``suspending``, ``resuming`` or ``error``."""
    return _read(os.path.join(root, "devices", device, "power", "runtime_status"))


class UsbSuspend:
    """Suspend one USB device and put everything back on :meth:`resume`.

    :meth:`resume` is safe to call more than once and after a failed
    :meth:`suspend`, so a caller can put it in a ``finally``."""

    def __init__(self, device: str, root: str = SYSFS_USB,
                 write: Callable[[str, str], None] = sudo_write,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.device = device
        self._root = root
        self._write = write
        self._sleep = sleep
        self._clock = clock
        self._power = os.path.join(root, "devices", device, "power")
        self._unbound: list = []
        self._saved = None   # (control, autosuspend_delay_ms) before suspend()

    def suspend(self, timeout_s: float = 10.0) -> float:
        """Suspend the device; return the seconds it took to read ``suspended``.

        Raises RuntimeError if it never does: then something else holds the
        device awake, and the test would measure nothing."""
        self._saved = (_read(os.path.join(self._power, "control")),
                       _read(os.path.join(self._power, "autosuspend_delay_ms")))
        self._unbound = bound_interfaces(self.device, root=self._root)
        unbind = os.path.join(self._root, "drivers", USBHID, "unbind")
        for intf in self._unbound:
            self._write(unbind, intf)
        self._write(os.path.join(self._power, "autosuspend_delay_ms"), "0")
        self._write(os.path.join(self._power, "control"), "auto")
        start = self._clock()
        while True:
            status = runtime_status(self.device, self._root)
            if status == "suspended":
                return self._clock() - start
            if self._clock() - start >= timeout_s:
                raise RuntimeError(f"{self.device} still reads runtime_status="
                                   f"{status} {timeout_s:.0f} s after control=auto")
            self._sleep(0.1)

    def resume(self) -> None:
        """Wake the port, rebind ``usbhid``, then restore the PM settings.

        The settings go back last: restoring ``control=auto`` while no driver is
        bound would let the port autosuspend again before the bind."""
        saved, self._saved = self._saved, None
        if saved is not None:
            self._write(os.path.join(self._power, "control"), "on")
        bind = os.path.join(self._root, "drivers", USBHID, "bind")
        unbound, self._unbound = self._unbound, []
        for intf in unbound:
            self._write(bind, intf)
        if saved is not None:
            control, delay = saved
            self._write(os.path.join(self._power, "autosuspend_delay_ms"), delay)
            if control != "on":
                self._write(os.path.join(self._power, "control"), control)
