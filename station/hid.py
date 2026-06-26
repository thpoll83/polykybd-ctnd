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


def _frame(data: bytes) -> bytes:
    """Wrap a command payload as the 65-byte numbered Raw HID report we write:
    report id ``0x00`` + the 64-byte data block, zero-padded. Rejects an
    oversized payload up front with a clear message instead of the cryptic
    ``bytes: negative count`` that ``bytes(64 - len(data))`` would otherwise raise.
    """
    if len(data) > 64:
        raise ValueError(f"raw HID payload too large: {len(data)} > 64 bytes")
    return bytes([0x00]) + data + bytes(64 - len(data))


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
        """Stop the reader thread and close the device, in that order."""
        self._running = False
        # Join the reader thread BEFORE closing the device. The loop calls
        # self._dev.read() in a background thread; closing a hidapi handle while
        # that read is in flight is a use-after-free in libhidapi's hidraw
        # backend (the per-open input queue is torn down under the reader),
        # which aborts the whole process with "free(): invalid pointer"
        # (SIGABRT, exit 134). With CONSOLE_ENABLE on (the default firmware now
        # streams [qmk] lines) the reader is almost always mid-read at stop()
        # time, so this turned otherwise-green HIL runs into exit-134 CI
        # failures. The loop's read timeout is 200 ms, so it observes the
        # cleared _running and returns promptly; join with a margin above that.
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None


class RawHID:
    """Sends and receives Raw HID reports (same protocol as PolyKybdHost)."""

    def __init__(self, vendor_id: int = QMK_VENDOR_ID, product_id: int = QMK_PRODUCT_ID):
        self._vid = vendor_id
        self._pid = product_id
        # Link-blip bookkeeping (read by the runner to classify outcomes). A lone
        # dropped/late HID reply on the rig is a transient USB read hiccup, not a
        # firmware fault, so send() retries the read once; these record whether
        # that retry rescued the reply (recovered) or not (failed).
        self.timeouts_recovered = 0
        self.timeouts_failed = 0

    def send(self, data: bytes, timeout_ms: int = 3000, attempts: int = 3) -> bytes | None:
        """Write one report and read one reply (or None after all attempts time out).

        **Centralized transient-timeout tolerance.** The rig's master→slave split
        link can briefly stall the firmware's main loop (EEPROM write + full keycap
        refresh after a set command; an occasional multi-second boot-window blip),
        long enough to miss one — or a couple — of HID replies before it recovers.
        Rather than sprinkle retry helpers across individual tests (whack-a-mole),
        send() itself re-issues the request up to ``attempts`` times, so EVERY test
        inherits the tolerance for free. The read timeout per attempt is generous,
        so total tolerance is ~attempts × timeout_ms.

        Safe because every command sent via send() is idempotent (a query, or a set
        to a fixed value), so re-issuing is harmless; a fresh handle is opened per
        call so a stale late reply never bleeds into the next command. A genuine
        hang still returns None (all attempts time out) — the runner's
        freeze-signature logic (consecutive misses) is what flags a real fault, so
        masking a single blip here does not hide a true regression."""
        path = _find_path(self._vid, self._pid, HID_RAW_USAGE_PAGE, HID_RAW_USAGE)
        if path is None:
            raise RuntimeError("QMK Raw HID interface not found")
        dev = hid.Device(path=path)
        try:
            for attempt in range(max(1, attempts)):
                dev.write(_frame(data))
                response = dev.read(64, timeout=timeout_ms)
                if response:
                    if attempt:
                        self.timeouts_recovered += 1
                    return bytes(response)
            self.timeouts_failed += 1
            return None
        finally:
            dev.close()

    def send_and_read_all(
        self,
        data: bytes,
        first_timeout_ms: int = 1000,
        next_timeout_ms: int = 250,
        max_reports: int = 32,
    ) -> list[bytes]:
        """Write one command, then read *every* input report it produces.

        ``send()`` opens a fresh handle and reads exactly once, so it only ever
        sees the first 64-byte packet. Some firmware commands answer with more
        than one — e.g. GET_LANG_LIST splits the language codes across many
        reports (15 codes per packet, so 143 languages = 10 reports). This opens
        the handle once, writes, then keeps reading (with a short per-read
        timeout) until a read returns nothing or ``max_reports`` is hit,
        returning the packets in order. Opening a single handle for the whole
        exchange is what keeps the firmware's back-to-back replies from being
        dropped by the per-open hidraw queue being torn down between reads.

        ``max_reports`` is only a safety stop against a runaway firmware; the
        normal terminator is the empty read after the last real packet. Keep it
        comfortably above the largest real reply (GET_LANG_LIST: ceil(NUM_LANG /
        15) packets) so a growing language list never silently truncates the
        read — a too-low cap was why a 143-language list (10 packets) decoded as
        only 120 codes and mismatched the packed list.
        """
        path = _find_path(self._vid, self._pid, HID_RAW_USAGE_PAGE, HID_RAW_USAGE)
        if path is None:
            raise RuntimeError("QMK Raw HID interface not found")
        dev = hid.Device(path=path)
        try:
            dev.write(_frame(data))
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
                dev.write(_frame(data))
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
        report = _frame(data)
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
                        dev.write(report)
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
