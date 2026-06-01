# SPDX-License-Identifier: GPL-2.0-only
import threading
import time
from typing import Callable

import hid

from .config import (
    QMK_VENDOR_ID, QMK_PRODUCT_ID,
    HID_CONSOLE_USAGE_PAGE, HID_CONSOLE_USAGE,
    HID_RAW_USAGE_PAGE, HID_RAW_USAGE,
)

# This module uses the `hid` package (apmorton/pyhidapi), which binds the
# hidraw backend of libhidapi. That backend populates usage_page/usage in
# hid.enumerate(), which is what lets us pick out the Raw HID interface by
# usage. (The similarly named `hidapi` package exposes hid.device()/open_path()
# but its libusb backend leaves usage_page/usage at 0, so enumeration-by-usage
# silently finds nothing.) Devices are therefore opened via hid.Device(path=...),
# not hid.device().open_path().


def _find_path(vendor_id: int, product_id: int, usage_page: int, usage: int) -> bytes | None:
    for d in hid.enumerate(vendor_id, product_id):
        if d["usage_page"] == usage_page and d["usage"] == usage:
            return d["path"]
    return None


def enumerate_raw_interfaces(
    vendor_id: int = QMK_VENDOR_ID, product_id: int = QMK_PRODUCT_ID
) -> list[dict]:
    """Return every enumerated Raw HID interface for the PolyKybd VID/PID.

    On the HIL rig the slave half calls usb_disconnect(), so a correctly
    flashed pair exposes exactly one Raw HID interface (the master's). A list
    longer than one means both halves enumerated as master — the failure this
    whole POLYKYBD_HIL build exists to prevent.
    """
    return [
        d
        for d in hid.enumerate(vendor_id, product_id)
        if d["usage_page"] == HID_RAW_USAGE_PAGE and d["usage"] == HID_RAW_USAGE
    ]


class HIDConsole:
    """Reads QMK's built-in HID console output (equivalent to hid-listen)."""

    def __init__(self, vendor_id: int = QMK_VENDOR_ID, product_id: int = QMK_PRODUCT_ID):
        self._vid = vendor_id
        self._pid = product_id
        self._dev = None
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self, callback: Callable[[str], None]) -> None:
        path = _find_path(self._vid, self._pid, HID_CONSOLE_USAGE_PAGE, HID_CONSOLE_USAGE)
        if path is None:
            raise RuntimeError("QMK HID console not found — is the keyboard plugged in?")
        self._dev = hid.Device(path=path)
        self._running = True
        self._thread = threading.Thread(target=self._loop, args=(callback,), daemon=True)
        self._thread.start()

    def _loop(self, callback: Callable[[str], None]) -> None:
        while self._running:
            try:
                data = self._dev.read(64, timeout=200)
                if data:
                    msg = bytes(data).rstrip(b"\x00").decode("utf-8", errors="replace")
                    if msg.strip():
                        callback(msg)
            except Exception:
                time.sleep(0.1)

    def stop(self) -> None:
        self._running = False
        if self._dev:
            self._dev.close()
            self._dev = None


class RawHID:
    """Sends and receives Raw HID reports (same protocol as PolyKybdHost)."""

    def __init__(self, vendor_id: int = QMK_VENDOR_ID, product_id: int = QMK_PRODUCT_ID):
        self._vid = vendor_id
        self._pid = product_id

    def send(self, data: bytes, timeout_ms: int = 1000) -> bytes | None:
        path = _find_path(self._vid, self._pid, HID_RAW_USAGE_PAGE, HID_RAW_USAGE)
        if path is None:
            raise RuntimeError("QMK Raw HID interface not found")
        dev = hid.Device(path=path)
        try:
            report = bytes([0x00]) + data + bytes(64 - len(data))
            dev.write(report[:65])
            response = dev.read(64, timeout=timeout_ms)
            return bytes(response) if response else None
        finally:
            dev.close()
