# SPDX-License-Identifier: GPL-2.0-only
"""USB-suspend the master from the Pi, without cutting its power.

The Pi 4's own ports cannot switch VBUS (``uhubctl -a off`` reports success and
the rail stays up), so the rig cannot power a half down. It does not need to: a
host that sleeps leaves VBUS on and stops the bus, and that bus suspend is what
runs the firmware's ``suspend_power_down_kb()``, its EEPROM flush and the core1
suspend hold. The kernel's runtime PM produces the same bus state:

1. unbind the driver from every interface of the keyboard (``UNBINDABLE``:
   ``usbhid``, ``cdc_acm`` for the bulk pipe, ``snd-usb-audio`` for USB MIDI). A
   bound driver whose device is open (X or the kiosk on an input device, a reader
   on ``/dev/ttyACM0``, a MIDI client) holds a PM reference and the device never
   autosuspends; an interface with no driver holds none.
2. ``power/autosuspend_delay_ms`` = 0 and ``power/control`` = ``auto``. The USB
   core then suspends the port, and ``power/runtime_status`` reads
   ``suspended``. That file is the proof the suspend happened.
3. Wake: ``power/control`` = ``on`` resumes the port, then ``usbhid`` is bound
   again and the hidraw nodes come back.

Every sysfs write goes through ``sudo -n polykybd-usb-suspend``
(``scripts/usb-suspend-ctl.sh``, installed by ``setup.sh``; :func:`grant_missing`
says whether it is). The wrapper accepts
four operations and only on the PolyKybd's own device, so the sudo grant is not
a root write to any file. Reads need no privilege. ``root`` and ``ctl`` are
parameters so the offline tests can run this against a fake tree.
"""
import os
import subprocess
import time
from typing import Callable

SYSFS_USB = "/sys/bus/usb"
USBHID = "usbhid"
# The drivers the keyboard's interfaces use, which polykybd-usb-suspend may unbind:
# usbhid (the HID interfaces), cdc_acm (the bulk pipe) and snd-usb-audio (USB MIDI).
# A bound driver can hold the device awake, so all of them are unbound.
UNBINDABLE = (USBHID, "cdc_acm", "snd-usb-audio")
USB_SUSPEND_CTL = "/usr/local/sbin/polykybd-usb-suspend"
GRANT_PROBE_TIMEOUT_S = 10.0   # grant_missing(): sudo -n answers in well under 1 s


def sudo_ctl(*args: str) -> None:
    """Run one wrapper operation as root: ``unbind``/``bind`` <intf> [driver],
    ``control`` <dev> on|auto, or ``delay`` <dev> <ms>.

    ``-n``: a missing grant fails at once instead of waiting on a password
    prompt nobody will answer. The fix is ``setup.sh --units-only``."""
    try:
        subprocess.run(["sudo", "-n", USB_SUSPEND_CTL, *args],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip()
        raise RuntimeError(f"polykybd-usb-suspend {' '.join(args)} failed: {detail} "
                           "(is the rig provisioned? sudo bash ./scripts/setup.sh "
                           "--units-only)") from exc


def grant_missing(ctl: str = USB_SUSPEND_CTL,
                  run: Callable[..., object] = subprocess.run):
    """Why the wrapper cannot run as root on this rig, or None when it can.

    Runs the wrapper as root with no arguments, which only prints its usage line
    and exits 2 (``scripts/usb-suspend-ctl.sh``), so nothing is written. That
    line on stderr proves sudo ran this exact command without a password. ``-n``
    makes a missing grant fail at once, ``-k`` ignores a cached password, and
    sudo's own refusal exits 1. ``sudo
    -l`` would not prove it: a password-required rule such as ``%sudo ALL``
    lists the command as allowed too (Sourcery, Greptile, ctnd#116).

    The rig is updated by pushing to ctnd ``main``, but a
    root-owned wrapper and its sudoers line arrive only through ``setup.sh``, run
    by hand on the Pi. A rig that has not had that run cannot execute the check
    at all, so the caller SKIPs it instead of failing a run that gates releases
    (2026-10-14: firmware 1.9.2 published with no files). On a provisioned rig
    this returns None and the check runs and grades as before."""
    if not os.path.isfile(ctl):
        return f"{ctl} is not installed"
    try:
        # Audit: a literal argv with no shell; ctl is the module constant
        # unless a test passes a temp path.
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        res = run(["sudo", "-k", "-n", ctl], stdout=subprocess.DEVNULL,
                  stderr=subprocess.PIPE, timeout=GRANT_PROBE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"sudo -n {ctl} did not answer within {GRANT_PROBE_TIMEOUT_S:.0f} s"
    except OSError as exc:
        return f"could not run sudo: {exc}"
    stderr = (res.stderr or b"").decode("utf-8", "replace")
    if res.returncode == 2 and "usage: polykybd-usb-suspend" in stderr:
        return None
    detail = stderr.strip().splitlines()[0] if stderr.strip() else f"exit {res.returncode}"
    return f"no passwordless sudo grant for {ctl} ({detail})"


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


def interface_drivers(device: str, root: str = SYSFS_USB) -> dict:
    """Every interface of the device and the driver bound to it (None if none)."""
    devices = os.path.join(root, "devices")
    out = {}
    for name in sorted(os.listdir(devices)):
        if not name.startswith(device + ":"):
            continue
        link = os.path.join(devices, name, "driver")
        out[name] = os.path.basename(os.readlink(link)) if os.path.islink(link) else None
    return out


class HeldAwake(RuntimeError):
    """The device did not suspend while some interfaces were still bound, because
    the wrapper refused to unbind their driver (an installed copy older than
    cdc_acm and snd-usb-audio support, or a driver not in ``UNBINDABLE``). The rig
    then cannot tell whether that driver or the firmware kept it awake. ``others``
    maps each such interface to its driver."""

    def __init__(self, message: str, others: dict):
        super().__init__(message)
        self.others = others


def runtime_status(device: str, root: str = SYSFS_USB) -> str:
    """``active``, ``suspended``, ``suspending``, ``resuming`` or ``error``."""
    return _read(os.path.join(root, "devices", device, "power", "runtime_status"))


class UsbSuspend:
    """Suspend one USB device and put everything back on :meth:`resume`.

    :meth:`resume` is safe to call more than once and after a failed
    :meth:`suspend`, so a caller can put it in a ``finally``."""

    def __init__(self, device: str, root: str = SYSFS_USB,
                 ctl: Callable[..., None] = sudo_ctl,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        self.device = device
        self._root = root
        self._ctl = ctl
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
        # Record each interface only once its unbind succeeded: resume() rebinds
        # exactly these, and a bind of one still bound fails with EBUSY.
        self._unbound = []
        for intf, drv in interface_drivers(self.device, self._root).items():
            if drv not in UNBINDABLE:
                continue
            # Unbinding the control interface of a CDC or audio pair releases its
            # partner too, so read the driver again before each unbind.
            if self._driver_of(intf) != drv:
                continue
            if drv == USBHID:
                self._ctl("unbind", intf)
            else:
                try:
                    self._ctl("unbind", intf, drv)
                except RuntimeError:
                    # A wrapper installed before cdc_acm and snd-usb-audio were
                    # added refuses them. The driver stays bound, the device stays
                    # awake, and suspend() raises HeldAwake (a SKIP, not a FAIL).
                    continue
            self._unbound.append((intf, drv))
        self._ctl("delay", self.device, "0")
        self._ctl("control", self.device, "auto")
        start = self._clock()
        while True:
            status = runtime_status(self.device, self._root)
            if status == "suspended":
                return self._clock() - start
            if self._clock() - start >= timeout_s:
                msg = (f"{self.device} still reads runtime_status={status} "
                       f"{timeout_s:.0f} s after control=auto")
                others = {i: d for i, d in interface_drivers(self.device, self._root).items()
                          if d is not None and d != USBHID}
                if others:
                    held = ", ".join(f"{i}={d}" for i, d in others.items())
                    raise HeldAwake(f"{msg}, with interfaces still bound to other "
                                    f"drivers ({held})", others)
                raise RuntimeError(msg)
            self._sleep(0.1)

    def _driver_of(self, intf: str):
        link = os.path.join(self._root, "devices", intf, "driver")
        return os.path.basename(os.readlink(link)) if os.path.islink(link) else None

    def resume(self) -> None:
        """Wake the port, bind the drivers again, then restore the PM settings.

        The settings go back last: restoring ``control=auto`` while no driver is
        bound would let the port autosuspend again before the bind. Each piece of
        pending state is dropped only once its write succeeded, so after a failed
        write a second call retries what is left."""
        if self._saved is not None:
            self._ctl("control", self.device, "on")
        while self._unbound:
            intf, drv = self._unbound[0]
            # Binding the control interface of a CDC or audio pair claims its
            # partner, so a partner that is bound again needs no bind of its own.
            if self._driver_of(intf) is None:
                if drv == USBHID:
                    self._ctl("bind", intf)
                else:
                    self._ctl("bind", intf, drv)
            self._unbound.pop(0)
        if self._saved is not None:
            control, delay = self._saved
            self._ctl("delay", self.device, delay)
            if control != "on":
                self._ctl("control", self.device, control)
            self._saved = None
