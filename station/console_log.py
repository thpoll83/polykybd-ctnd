# SPDX-License-Identifier: GPL-2.0-only
"""Capture and parse the keyboard's QMK HID console output.

The firmware streams diagnostics over the HID console interface
(``CONSOLE_ENABLE``, which split72's ``keyboard.json`` sets), and until now the
rig only *echoed* those lines into the run log — nothing ever asserted on them.
That left the split link, the one subsystem with the worst field record in the
project, completely unmeasured by CI even though the firmware prints a health
counter for it on every 200th bridged frame.

Two pieces live here:

* :class:`ConsoleTap` — a thread-safe rolling buffer of *reassembled* console
  lines, with a ``mark()``/``since()`` pair so a test can ask "what did the
  firmware print **while I was doing that**".
* the pure parsers/classifiers for the ``Split link:`` health counter, which are
  unit-testable without hardware (``tests/console_log_test.py``).

⚠️ **A console read is a report-sized FRAGMENT, not a line.** QMK delivers
whatever fitted in one 32/64-byte report, so a long line arrives split across
several reads and a split can land mid-word. Anything that filters or parses
console output must buffer and reassemble across reads — matching a raw chunk
silently drops every continuation fragment and truncates what it keeps. That is
why :meth:`ConsoleTap.feed` takes chunks and only ever emits ``\\n``-terminated
lines (plus whatever :meth:`ConsoleTap.flush` releases at the end).

⚠️ **Most firmware diagnostics are gated on ``debug_enable``, which defaults
false** (it was turned off to stop keystroke logging) — ``Bridge sync retry`` and
``Failed to sync … for transaction X`` are both behind that gate and therefore do
**not** appear on the rig. The ``Split link:`` summary is deliberately
ungated (``bridge_helper.c``: "a passive wire-health diagnostic with no key
content"), which is precisely why it, and not the failure lines, is what the link
health check reads.
"""
import re
import threading
import time
from collections import deque
from typing import Callable

# keyboards/polykybd/bridge_helper.c:
#   "Split link: %lu tx crc_err=%lu nack=%lu transport_fail=%lu giveup=%lu err=%lu.%lu%%\n"
_LINK_RE = re.compile(
    r"Split link:\s*(?P<tx>\d+)\s+tx\s+crc_err=(?P<crc_err>\d+)\s+nack=(?P<nack>\d+)"
    r"\s+transport_fail=(?P<transport_fail>\d+)\s+giveup=(?P<giveup>\d+)"
)

# Counter fields of a Split link: summary, in the order the firmware prints them.
LINK_FIELDS = ("tx", "crc_err", "nack", "transport_fail", "giveup")


def parse_link_stats(line: str):
    """Decode one ``Split link:`` summary line into a dict of cumulative counters.

    Returns ``None`` for any other line, so it doubles as the filter. The counters
    are cumulative since boot — the useful quantity is a *delta* between two
    summaries (see :func:`link_delta`), because the documented boot burst inflates
    the absolutes on a perfectly healthy link.

    >>> parse_link_stats("Split link: 800 tx crc_err=39 nack=11 "
    ...                  "transport_fail=1 giveup=13 err=5.0%")["tx"]
    800
    >>> parse_link_stats("Overlay mapping data received.") is None
    True
    """
    m = _LINK_RE.search(line or "")
    if not m:
        return None
    return {f: int(m.group(f)) for f in LINK_FIELDS}


def link_delta(before: dict, after: dict) -> dict:
    """Counter growth between two ``Split link:`` summaries.

    Clamped at 0 per field: the counters only ever climb, but a *reboot* resets
    them, and a negative "delta" from a straddled reboot must not read as a
    repaired link.

    >>> link_delta({"tx": 200, "crc_err": 39, "nack": 0, "transport_fail": 1, "giveup": 13},
    ...            {"tx": 600, "crc_err": 39, "nack": 4, "transport_fail": 1, "giveup": 13})
    {'tx': 400, 'crc_err': 0, 'nack': 4, 'transport_fail': 0, 'giveup': 0}
    """
    return {f: max(0, after.get(f, 0) - before.get(f, 0)) for f in LINK_FIELDS}


def classify_link_health(delta: dict) -> tuple:
    """Pass/fail decision for one window of split-link traffic.

    Returns ``(passed, errors, tolerance)``. An *error* is a real LINK fault —
    a corrupted frame (``crc_err``) or no answer at all (``transport_fail``).

    ⚠️ ``nack`` is deliberately NOT an error, matching the firmware's own
    ``err%``: a ``SYNC_BUSY`` / ``SYNC_NACK_REFUSED`` reply means the wire worked
    and the slave simply said something other than yes. ``SYNC_BUSY`` arrives on
    every erase re-poll of a flash, so counting those would redden a healthy run
    the moment a font-pack test runs. ``giveup`` is excluded for the same reason —
    it is derived from the same predicate and would double-count the crc/transport
    faults already counted.

    The tolerance is ``max(1, tx // 100)`` — i.e. 1%. Measured steady state on the
    full-duplex link is **zero** ongoing errors (the boot burst aside), so this is
    a wide margin around the expected value, chosen because an occasionally-red
    check is one people learn to scroll past.

    >>> classify_link_health({"tx": 400, "crc_err": 0, "nack": 9, "transport_fail": 0, "giveup": 0})
    (True, 0, 4)
    >>> classify_link_health({"tx": 400, "crc_err": 7, "nack": 0, "transport_fail": 2, "giveup": 9})
    (False, 9, 4)
    >>> classify_link_health({"tx": 50, "crc_err": 1, "nack": 0, "transport_fail": 0, "giveup": 1})
    (True, 1, 1)
    """
    errors = delta.get("crc_err", 0) + delta.get("transport_fail", 0)
    tolerance = max(1, delta.get("tx", 0) // 100)
    return errors <= tolerance, errors, tolerance


class ConsoleTap:
    """Thread-safe rolling buffer of reassembled QMK console lines.

    The runner feeds every console chunk in from the reader thread; tests read
    lines back out from the main thread. Both ends are cheap and lock-guarded.

    Marks are *monotonic line counts*, not indices into the retained window, so
    ``since(mark)`` stays correct after the ring buffer has evicted older lines
    (it simply returns fewer lines than were produced, never the wrong ones).
    """

    def __init__(self, maxlen: int = 4000):
        self._lock = threading.Lock()
        self._lines = deque(maxlen=maxlen)
        self._pending = ""
        self._produced = 0     # total lines ever appended (mark space)

    # -- writer side (console reader thread) --
    def feed(self, chunk: str) -> None:
        """Add one raw console read, emitting whatever completed lines it finishes."""
        with self._lock:
            buf = self._pending + (chunk or "")
            parts = buf.split("\n")
            # The trailing element is an unterminated fragment — hold it for the
            # next read rather than treating it as a line.
            self._pending = parts.pop()
            for line in parts:
                self._append(line)

    def flush(self) -> None:
        """Release a trailing fragment the firmware never newline-terminated.

        The most interesting line is often the last one, and without this it sits
        in the buffer forever."""
        with self._lock:
            pending, self._pending = self._pending, ""
            if pending:
                self._append(pending)

    def _append(self, line: str) -> None:
        line = line.rstrip("\r")
        if line:
            self._lines.append(line)
            self._produced += 1

    # -- reader side (test / runner thread) --
    def mark(self) -> int:
        """A token for "everything from here on", for :meth:`since`."""
        with self._lock:
            return self._produced

    def since(self, mark: int) -> list:
        """Lines produced after ``mark`` (oldest first), best-effort if evicted."""
        with self._lock:
            available = len(self._lines)
            produced = self._produced
        want = max(0, produced - max(0, mark))
        return list(self._lines)[-min(want, available):] if want else []

    def wait_for(self, pattern, mark: int = 0, timeout: float = 10.0,
                 poll: float = 0.1):
        """Block until a line after ``mark`` matches ``pattern``; return it or None.

        ``pattern`` is a substring or a compiled regex. Returns ``None`` on
        timeout — callers decide whether that is a failure or merely
        unconfirmed, because the console is best-effort infrastructure and a
        missing line must never be conflated with a firmware fault.
        """
        deadline = time.monotonic() + timeout
        while True:
            for line in self.since(mark):
                if _matches(pattern, line):
                    return line
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll)

    def find_all(self, pattern, mark: int = 0) -> list:
        """Every line after ``mark`` matching ``pattern`` (substring or regex)."""
        return [ln for ln in self.since(mark) if _matches(pattern, ln)]

    def link_stats(self, mark: int = 0) -> list:
        """Every parsed ``Split link:`` summary after ``mark``, in order."""
        out = []
        for line in self.since(mark):
            stats = parse_link_stats(line)
            if stats:
                out.append(stats)
        return out


def _matches(pattern, line: str) -> bool:
    if hasattr(pattern, "search"):
        return bool(pattern.search(line))
    return pattern in line


# The rig runs one keyboard per process, and a test's signature is
# ``(raw_hid, log)`` — there is no context object to thread a tap through. So the
# tap is a module-level singleton the runner feeds and tests read, in the same
# spirit as a logger. Tests that use it MUST carry ``"needs_console": True`` so
# they SKIP (rather than silently assert nothing) on a build or a run where the
# console never came up.
TAP = ConsoleTap()


def console_sink(echo: Callable[[str], None]) -> Callable[[str], None]:
    """Build the HIDConsole callback: echo each chunk *and* feed the tap."""
    def sink(chunk: str) -> None:
        TAP.feed(chunk)
        echo(chunk)
    return sink
