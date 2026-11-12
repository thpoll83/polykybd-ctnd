# SPDX-License-Identifier: GPL-2.0-only
"""The keyboard's USB MIDI interfaces, read from sysfs.

Firmware 1.13.0 (qmk#427) enumerates a send-only USB MIDI function on every
flavour except the DOOM monolith: an AudioControl interface (class 0x01,
subclass 0x01) with no endpoints, and a MIDIStreaming interface (class 0x01,
subclass 0x03) with exactly ONE endpoint, bulk IN. ``MIDI_SEND_ONLY`` (a
PolyKybd patch to QMK core) removes the OUT endpoint, so an OUT endpoint here
means the build lost that define and spends ~400 B of RAM on a receive path the
firmware never reads (qmk keyboards/polykybd/MIDI.md).

Reads sysfs only, so it is unit-testable with a fake tree.
"""
import glob
import os

VID = "2021"
PID = "2007"
SYS_USB = "/sys/bus/usb/devices"

AUDIO_CLASS = "01"
AUDIO_CONTROL = "01"
MIDI_STREAMING = "03"


def _read(path: str) -> str:
    with open(path) as f:
        return f.read().strip()


def keyboard_devices(sys_usb: str = SYS_USB, vid: str = VID, pid: str = PID) -> list:
    """sysfs device directories with the keyboard's idVendor/idProduct.

    On the rig the slave half calls usb_disconnect(), so there is one."""
    out = []
    for dev in sorted(glob.glob(os.path.join(sys_usb, "*"))):
        try:
            if _read(os.path.join(dev, "idVendor")) == vid and _read(os.path.join(dev, "idProduct")) == pid:
                out.append(dev)
        except OSError:
            continue
    return out


def audio_interfaces(dev: str) -> list:
    """The device's audio-class interfaces as dicts:
    ``{"name", "subclass", "endpoints": [address, ...]}``.

    Interface directories are ``<dev-name>:<config>.<ifnum>``; an endpoint is an
    ``ep_XX`` directory (XX = address in hex, bit 7 set = IN)."""
    base = os.path.basename(dev)
    out = []
    for itf in sorted(glob.glob(os.path.join(dev, base + ":*"))):
        try:
            if _read(os.path.join(itf, "bInterfaceClass")) != AUDIO_CLASS:
                continue
            subclass = _read(os.path.join(itf, "bInterfaceSubClass"))
        except OSError:
            continue
        eps = sorted(int(os.path.basename(e)[3:], 16) for e in glob.glob(os.path.join(itf, "ep_*")))
        out.append({"name": os.path.basename(itf), "subclass": subclass, "endpoints": eps})
    return out


def check_send_only(interfaces: list) -> list:
    """Problems with a send-only MIDI function, or [] when it is correct."""
    control = [i for i in interfaces if i["subclass"] == AUDIO_CONTROL]
    streaming = [i for i in interfaces if i["subclass"] == MIDI_STREAMING]
    problems = []
    if len(control) != 1:
        problems.append(f"{len(control)} AudioControl interfaces (expected 1)")
    if len(streaming) != 1:
        problems.append(f"{len(streaming)} MIDIStreaming interfaces (expected 1)")
        return problems
    eps = streaming[0]["endpoints"]
    if len(eps) != 1 or not eps[0] & 0x80:
        listed = ", ".join(f"{e:#04x}" for e in eps) or "none"
        problems.append(f"MIDIStreaming endpoints {listed} (expected one IN endpoint; "
                        "an OUT endpoint means MIDI_SEND_ONLY is missing)")
    return problems
