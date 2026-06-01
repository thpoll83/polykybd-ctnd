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

    def send_and_read_all(
        self,
        data: bytes,
        first_timeout_ms: int = 1000,
        next_timeout_ms: int = 250,
        max_reports: int = 8,
    ) -> list[bytes]:
        """Write one command, then read *every* input report it produces.

        ``send()`` opens a fresh handle and reads exactly once, so it only ever
        sees the first 64-byte packet. Some firmware commands answer with more
        than one — e.g. GET_LANG_LIST splits the language codes across two
        reports. This opens the handle once, writes, then keeps reading (with a
        short per-read timeout) until a read returns nothing or ``max_reports``
        is hit, returning the packets in order. Opening a single handle for the
        whole exchange is what keeps the firmware's back-to-back replies from
        being dropped by the per-open hidraw queue being torn down between reads.
        """
        path = _find_path(self._vid, self._pid, HID_RAW_USAGE_PAGE, HID_RAW_USAGE)
        if path is None:
            raise RuntimeError("QMK Raw HID interface not found")
        dev = hid.Device(path=path)
        try:
            report = bytes([0x00]) + data + bytes(64 - len(data))
            dev.write(report[:65])
            packets: list[bytes] = []
            timeout = first_timeout_ms
            for _ in range(max_reports):
                chunk = dev.read(64, timeout=timeout)
                if not chunk:
                    break
                packets.append(bytes(chunk))
                timeout = next_timeout_ms
            return packets
        finally:
            dev.close()

    def write_reports(self, reports: list[bytes]) -> None:
        """Write several command reports on one handle without reading replies.

        For fire-and-forget command bursts whose firmware replies are disabled —
        the overlay/ROI upload commands (cmd 10/16/17/18/19) send no ACK. Doing
        the burst on a single handle matches how PolyKybdHost streams overlays
        (``send_multiple``); the follow-up liveness check is a separate GET_ID
        via ``send()``.
        """
        path = _find_path(self._vid, self._pid, HID_RAW_USAGE_PAGE, HID_RAW_USAGE)
        if path is None:
            raise RuntimeError("QMK Raw HID interface not found")
        dev = hid.Device(path=path)
        try:
            for data in reports:
                if len(data) > 64:
                    raise ValueError(f"raw HID report too long: {len(data)} > 64 bytes")
                report = bytes([0x00]) + data + bytes(64 - len(data))
                dev.write(report[:65])
        finally:
            dev.close()

    def send_repeated(
        self,
        data: bytes,
        count: int,
        timeout_ms: int = 1000,
        retries: int = 2,
    ) -> tuple[list[bytes | None], list[float], int]:
        """Send the same report ``count`` times on ONE persistent handle.

        Returns ``(responses, latencies_ms, transient_errors)``. Reusing a single
        open handle is how the real host talks to the device and avoids the
        per-call open/close churn that trips a host-side USB "Protocol error"
        (EPROTO) on the Pi under rapid repetition — a stack-level hiccup unrelated
        to firmware health. On such a transient error (or an empty read) the
        exchange is retried up to ``retries`` times, reopening the handle after an
        exception; a genuine firmware freeze still surfaces as a ``None`` response
        the caller can assert on. Used by the GET_ID stress test.
        """
        path = _find_path(self._vid, self._pid, HID_RAW_USAGE_PAGE, HID_RAW_USAGE)
        if path is None:
            raise RuntimeError("QMK Raw HID interface not found")
        report = bytes([0x00]) + data + bytes(64 - len(data))
        dev = hid.Device(path=path)
        responses: list[bytes | None] = []
        latencies: list[float] = []
        transient = 0
        try:
            for _ in range(count):
                t0 = time.perf_counter()
                resp: bytes | None = None
                for attempt in range(retries + 1):
                    try:
                        dev.write(report[:65])
                        chunk = dev.read(64, timeout=timeout_ms)
                        resp = bytes(chunk) if chunk else None
                    except Exception:
                        resp = None
                        # Transient host-side USB error — reopen and retry.
                        try:
                            dev.close()
                        except Exception:
                            pass
                        try:
                            dev = hid.Device(path=path)
                        except Exception:
                            pass
                    if resp is not None:
                        break
                    if attempt < retries:
                        transient += 1
                latencies.append((time.perf_counter() - t0) * 1000.0)
                responses.append(resp)
            return responses, latencies, transient
        finally:
            try:
                dev.close()
            except Exception:
                pass
