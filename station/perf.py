# SPDX-License-Identifier: GPL-2.0-only
"""Automated firmware performance measurement on the HIL rig.

Why this exists
---------------
The firmware already carries a main-loop timing profiler
(``keyboards/polykybd/profiling/loop_profile.c``, built with
``-e POLYKYBD_LOOP_PROFILE=yes``). Until now reading it meant a human flashing a
profiling build, poking the keyboard by hand, watching the HID console and
pasting the ``LoopProf:`` block into a conversation. That is slow, unrepeatable,
and the numbers are not attributable to any particular workload — the counters
are cumulative from boot and ``worst`` is an all-time maximum.

This module closes the loop. It drives the profiler's on-demand control command
(HID cmd 32, see the firmware's ``hid_com.c`` case 32) so every measurement is a
bounded window::

    RESET  ->  run a defined workload  ->  READ back the window

and reports the result as structured data. The rig can then produce the same
numbers on every run, compare them against a stored baseline, and publish them
to a CI job summary — no human in the loop.

What is measured
----------------
* **Overlay burst (plain, cmd 10)** and **overlay burst (compressed, cmd 16 —
  the core1 RLE path)**: the program-switch traffic that actually stalls the
  main loop. The profiler attributes the stall to the master->slave bridge, the
  per-keycap re-render, or the rest of the loop.
* **HID round-trip latency**: host-side GET_ID percentiles, which is what a user
  perceives as "the keyboard went deaf for a moment".
* **Boot-to-responsive**: how long after a cold flash the master starts serving
  HID stably (supplied by the runner, which owns the flash timing).

Everything that decodes or derives numbers is a pure function so it can be
unit-tested without hardware; only ``Profiler`` and the ``measure_*`` helpers
touch the device.
"""
import statistics
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

from .hid import RawHID
from .hil_tests import (
    POLY_CHANNEL, ACK, CMD_GET_ID, CMD_SEND_OVERLAY, CMD_START_COMPRESSED_OVERLAY,
    KC_A, NUM_SEGMENTS, PLAIN_SEG_BYTES, _BLANK_OVERLAY_RLE,
)

# --- profiler control command (mirrors firmware hid_com.c case 32) -------------
# Deliberately NOT protocol-gated and bumps no PROTOCOL_VERSION: the command only
# exists in a POLYKYBD_LOOP_PROFILE build, where the whole case is compiled in. A
# normal build NACKs it via the dispatcher's default branch, and that NACK is the
# contract this module uses to detect "no profiler in this firmware".
CMD_PROFILE      = 32
PROF_SUB_RESET   = 0   # zero the counters, start a fresh window
PROF_SUB_READ    = 1   # binary snapshot of the current window (data[3] = page)
PROF_SUB_LOG     = 2   # dump the console summary block immediately

# Wire format of the snapshot. Must match LOOP_PROFILE_SNAPSHOT_VERSION /
# LOOP_PROFILE_SNAPSHOT_PAGES / LOOP_PROFILE_NBUCKET in loop_profile.h. A version
# we do not know is refused rather than mis-decoded as a reordered struct.
SNAPSHOT_VERSION = 1
SNAPSHOT_PAGES   = 2
NBUCKET          = 7
BUCKET_LABELS    = ("<1ms", "1-2ms", "2-5ms", "5-10ms", "10-20ms", "20-50ms", ">=50ms")

# Iterations at or above this bucket are long enough to swallow a fast key tap
# (QMK scans the matrix once per main-loop iteration), so they are the headline
# "missed keystroke risk" number. Index 4 = the 10-20ms bucket and up.
LONG_ITER_BUCKET = 4


class ProfilerUnavailable(RuntimeError):
    """The flashed firmware has no POLYKYBD_LOOP_PROFILE profiler (cmd 32 NACKs)."""


@dataclass
class LoopProfile:
    """One decoded profiler window (the counters between a RESET and a READ)."""

    iters: int = 0
    ovl_iters: int = 0
    max_us: int = 0
    max_bridge_us: int = 0
    max_render_us: int = 0
    max_overlay: bool = False
    ovl_wall_us: int = 0
    ovl_bridge_us: int = 0
    ovl_render_us: int = 0
    bkt_norm: list = field(default_factory=lambda: [0] * NBUCKET)
    bkt_ovl: list = field(default_factory=lambda: [0] * NBUCKET)

    @property
    def ovl_rest_us(self) -> int:
        """Overlay-iteration wall time that is neither bridge nor render.

        The firmware clamps bridge into the wall and render into what is left, so
        this can never go negative on the device; ``max(0, ...)`` here guards only
        against a truncated/garbled read."""
        return max(0, self.ovl_wall_us - self.ovl_bridge_us - self.ovl_render_us)

    @property
    def long_iters(self) -> int:
        """Iterations >= 10 ms — the window in which a fast tap can be missed."""
        return sum(self.bkt_norm[LONG_ITER_BUCKET:]) + sum(self.bkt_ovl[LONG_ITER_BUCKET:])

    def to_dict(self) -> dict:
        return {
            "iters": self.iters,
            "ovl_iters": self.ovl_iters,
            "worst_iter_ms": round(self.max_us / 1000.0, 2),
            "worst_iter_was_overlay": self.max_overlay,
            "worst_bridge_ms": round(self.max_bridge_us / 1000.0, 2),
            "worst_render_ms": round(self.max_render_us / 1000.0, 2),
            "ovl_wall_ms": round(self.ovl_wall_us / 1000.0, 2),
            "ovl_bridge_ms": round(self.ovl_bridge_us / 1000.0, 2),
            "ovl_render_ms": round(self.ovl_render_us / 1000.0, 2),
            "ovl_rest_ms": round(self.ovl_rest_us / 1000.0, 2),
            "long_iters_ge_10ms": self.long_iters,
            "hist_norm": dict(zip(BUCKET_LABELS, self.bkt_norm)),
            "hist_ovl": dict(zip(BUCKET_LABELS, self.bkt_ovl)),
        }


def decode_snapshot(pages: dict) -> LoopProfile:
    """Decode the two binary snapshot pages into a :class:`LoopProfile`.

    ``pages`` maps page index -> the report *body* (everything after the 4-byte
    ``P<cmd><status><page>`` header). Pure, so the wire format can be unit-tested
    without a keyboard.

    >>> body0 = bytes([1, 1, 0, 0]) + b"".join(
    ...     __import__("struct").pack("<I", v)
    ...     for v in (100, 7, 105000, 5000, 67000, 3668000, 783000, 1927000))
    >>> body1 = b"".join(__import__("struct").pack("<I", v) for v in range(14))
    >>> p = decode_snapshot({0: body0, 1: body1})
    >>> p.iters, p.ovl_iters, p.max_overlay
    (100, 7, True)
    >>> p.ovl_rest_us
    958000
    >>> p.bkt_ovl[0]
    7
    """
    missing = [i for i in range(SNAPSHOT_PAGES) if i not in pages]
    if missing:
        raise ValueError(f"profiler snapshot missing page(s) {missing}")

    head = pages[0]
    if len(head) < 36:
        raise ValueError(f"profiler snapshot page 0 too short: {len(head)} bytes")
    version, flags = head[0], head[1]
    if version != SNAPSHOT_VERSION:
        raise ValueError(
            f"profiler snapshot version {version} != expected {SNAPSHOT_VERSION} — "
            "the firmware's loop_profile.h wire format changed; update perf.py"
        )
    (iters, ovl_iters, max_us, max_bridge_us, max_render_us,
     ovl_wall_us, ovl_bridge_us, ovl_render_us) = struct.unpack_from("<8I", head, 4)

    hist = pages[1]
    need = NBUCKET * 2 * 4
    if len(hist) < need:
        raise ValueError(f"profiler snapshot page 1 too short: {len(hist)} < {need}")
    values = struct.unpack_from(f"<{NBUCKET * 2}I", hist, 0)

    return LoopProfile(
        iters=iters, ovl_iters=ovl_iters,
        max_us=max_us, max_bridge_us=max_bridge_us, max_render_us=max_render_us,
        max_overlay=bool(flags & 0x01),
        ovl_wall_us=ovl_wall_us, ovl_bridge_us=ovl_bridge_us, ovl_render_us=ovl_render_us,
        bkt_norm=list(values[:NBUCKET]), bkt_ovl=list(values[NBUCKET:]),
    )


class Profiler:
    """Drives the firmware's on-demand profiler over Raw HID (cmd 32)."""

    def __init__(self, raw: RawHID, log: Callable[[str], None] = print):
        self._raw = raw
        self._log = log

    def _exchange(self, sub: int, page: int = 0) -> bytes | None:
        """Send one profiler sub-command; return the report body, or None on NACK.

        None means the firmware answered but refused (``P<32>!``) — which on a
        normal build is the dispatcher's unknown-command NACK, i.e. "no profiler
        here". A dropped reply raises, because that is a device fault rather than
        a capability answer, and must not be mistaken for "profiler absent"."""
        resp = self._raw.send(bytes([POLY_CHANNEL, CMD_PROFILE, sub, page]))
        if resp is None:
            raise RuntimeError(
                f"no reply to profiler command sub={sub} page={page} — device not responding"
            )
        if len(resp) < 3 or resp[0] != POLY_CHANNEL or resp[1] != CMD_PROFILE:
            raise RuntimeError(f"malformed profiler reply: {bytes(resp[:8])!r}")
        if resp[2] != ACK:
            return None
        return bytes(resp[4:])

    def available(self) -> bool:
        """True when the flashed firmware carries the profiler.

        Probes with READ page 0 — read-only, so it neither disturbs an in-flight
        measurement nor has to be undone."""
        try:
            return self._exchange(PROF_SUB_READ, 0) is not None
        except RuntimeError as exc:
            self._log(f"[perf] profiler probe failed: {exc}")
            return False

    def reset(self) -> None:
        """Zero the counters and open a fresh measurement window."""
        if self._exchange(PROF_SUB_RESET) is None:
            raise ProfilerUnavailable("firmware NACKed the profiler RESET (cmd 32) — "
                                      "not a POLYKYBD_LOOP_PROFILE build")

    def read(self) -> LoopProfile:
        """Read back the current window as a decoded :class:`LoopProfile`."""
        pages = {}
        for page in range(SNAPSHOT_PAGES):
            body = self._exchange(PROF_SUB_READ, page)
            if body is None:
                raise ProfilerUnavailable(
                    f"firmware NACKed profiler READ page {page} (cmd 32) — "
                    "not a POLYKYBD_LOOP_PROFILE build"
                )
            pages[page] = body
        return decode_snapshot(pages)

    def log_to_console(self) -> None:
        """Ask the firmware to print its summary block to the HID console.

        Purely for the human-readable record in the captured log — the numbers
        this module reports come from the binary snapshot, not from parsing that
        text. Best-effort: a NACK here is not worth failing a run over."""
        try:
            self._exchange(PROF_SUB_LOG)
        except RuntimeError as exc:
            self._log(f"[perf] console dump failed (non-fatal): {exc}")


# --- workloads ---------------------------------------------------------------
#
# Each workload builds the same report framing PolyKybdHost sends in production,
# so the profiler measures the real code path rather than a synthetic one. They
# are deliberately ACK-less bursts (the firmware does not reply to overlay
# uploads), which is also what makes them a clean stimulus: the host cannot
# accidentally pace the device by waiting for replies.

def plain_overlay_reports(keys: int, modifier: int = 0) -> list:
    """The 6 x 60-byte plain-overlay segments (cmd 10) for ``keys`` keycodes.

    Protocol 11 framing: ``[channel, cmd, keycode, (segment << 4) | modifier]``
    then a full 60-byte segment, which fills the 64-byte report exactly.

    >>> r = plain_overlay_reports(2)
    >>> len(r), len(r[0])
    (12, 64)
    >>> r[1][3] >> 4            # second segment index
    1
    """
    blank = bytes(PLAIN_SEG_BYTES)
    return [
        bytes([POLY_CHANNEL, CMD_SEND_OVERLAY, kc, (seg << 4) | (modifier & 0x0F)]) + blank
        for kc in range(KC_A, KC_A + keys)
        for seg in range(NUM_SEGMENTS)
    ]


def compressed_overlay_reports(keys: int) -> list:
    """One RLE-compressed overlay packet (cmd 16) per keycode — the core1 path.

    >>> len(compressed_overlay_reports(3))
    3
    """
    return [
        bytes([POLY_CHANNEL, CMD_START_COMPRESSED_OVERLAY, kc, 0x00]) + _BLANK_OVERLAY_RLE
        for kc in range(KC_A, KC_A + keys)
    ]


def _settle_after_burst(raw: RawHID, log: Callable[[str], None],
                        tries: int = 10, spacing: float = 0.1) -> bool:
    """Wait for the master to service HID again after an ACK-less upload burst.

    The device finishes the upload on its own schedule (display refresh, split
    sync), so reading the profiler immediately would both time out and clip the
    tail of the very iterations being measured. Polling GET_ID until it answers
    ends the window at "the workload is actually done"."""
    for _ in range(tries):
        try:
            resp = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]), timeout_ms=1000, attempts=1)
        except Exception:
            resp = None
        if resp and len(resp) >= 2 and resp[1] == CMD_GET_ID:
            return True
        time.sleep(spacing)
    log("[perf] WARNING: master did not answer GET_ID after the burst — "
        "the window may be clipped")
    return False


def measure_overlay_burst(raw: RawHID, profiler: Profiler, log: Callable[[str], None],
                          kind: str = "plain", keys: int = 8) -> dict:
    """RESET, stream an overlay burst, then READ the window back.

    ``kind`` is ``"plain"`` (cmd 10, 6 segments per key) or ``"compressed"``
    (cmd 16, the core1 RLE decompress path)."""
    if kind == "plain":
        reports = plain_overlay_reports(keys)
    elif kind == "compressed":
        reports = compressed_overlay_reports(keys)
    else:
        raise ValueError(f"unknown overlay burst kind: {kind!r}")

    log(f"[perf] overlay burst ({kind}): {keys} keycodes, {len(reports)} reports")
    profiler.reset()
    t0 = time.perf_counter()
    raw.write_reports(reports)
    settled = _settle_after_burst(raw, log)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    prof = profiler.read()

    out = prof.to_dict()
    out.update({
        "kind": kind,
        "keys": keys,
        "reports": len(reports),
        "host_wall_ms": round(wall_ms, 1),
        "settled": settled,
    })
    log(f"[perf]   worst iteration {out['worst_iter_ms']} ms "
        f"({'overlay' if out['worst_iter_was_overlay'] else 'normal'}), "
        f"{out['ovl_iters']} overlay iterations, "
        f"bridge {out['ovl_bridge_ms']} / render {out['ovl_render_ms']} / "
        f"rest {out['ovl_rest_ms']} ms")
    return out


def percentiles(samples: list) -> dict:
    """p50/p95/p99/max/mean of a latency sample list, in milliseconds.

    Uses nearest-rank on the sorted samples, so the reported value is always an
    observed measurement rather than an interpolation between two of them — with
    the small n a HIL burst produces, interpolation invents numbers that were
    never seen.

    >>> percentiles([1.0, 2.0, 3.0, 4.0])["p50"]
    2.0
    >>> percentiles([5.0])["p99"]
    5.0
    >>> percentiles([])
    {}
    """
    if not samples:
        return {}
    ordered = sorted(samples)

    def rank(pct: int) -> float:
        # Nearest-rank: the ceil(n * pct / 100)-th smallest sample, 1-indexed.
        # Done in integer arithmetic so e.g. 0.95 * 20 can't land on 18.999999
        # and silently pick the wrong sample.
        idx = max(1, -(-len(ordered) * pct // 100)) - 1
        return ordered[idx]

    return {
        "p50": round(rank(50), 2),
        "p95": round(rank(95), 2),
        "p99": round(rank(99), 2),
        "max": round(ordered[-1], 2),
        "mean": round(statistics.fmean(ordered), 2),
        "n": len(ordered),
    }


def measure_hid_latency(raw: RawHID, log: Callable[[str], None], n: int = 100) -> dict:
    """Host-side GET_ID round-trip latency over a burst on one persistent handle.

    This is the number a user feels as responsiveness. Misses are reported rather
    than raising: an isolated no-answer is the documented post-overlay deaf window
    (see ``test_get_id_stress``), and a perf run should record it, not fail on it.
    """
    responses, latencies, transient = raw.send_repeated(
        bytes([POLY_CHANNEL, CMD_GET_ID]), n)
    ok_latencies = [ms for resp, ms in zip(responses, latencies) if resp is not None]
    misses = sum(1 for resp in responses if resp is None)
    out = percentiles(ok_latencies)
    out.update({"misses": misses, "transient_usb_errors": transient, "sent": n})
    if out.get("n"):
        log(f"[perf] HID latency over {n} GET_ID: p50 {out['p50']} ms, "
            f"p95 {out['p95']} ms, max {out['max']} ms, {misses} miss(es)")
    else:
        log(f"[perf] HID latency: no replies out of {n} sends")
    return out


def measure_idle_overhead(raw: RawHID, profiler: Profiler, log: Callable[[str], None],
                          seconds: float = 3.0) -> dict:
    """Baseline: the main loop with no host traffic at all.

    Without it a burst number has no reference — it is the control that says
    whether a regression is in the overlay path or in the loop generally. The
    only device traffic in the window is the closing READ, so the sample is a
    genuinely quiet loop."""
    log(f"[perf] idle baseline: {seconds:.0f}s with no host traffic")
    profiler.reset()
    time.sleep(seconds)
    prof = profiler.read()
    out = prof.to_dict()
    out.update({"window_s": seconds})
    if seconds > 0:
        out["iters_per_s"] = round(prof.iters / seconds, 1)
        log(f"[perf]   {out['iters_per_s']} loop iterations/s, "
            f"worst {out['worst_iter_ms']} ms, "
            f"{out['long_iters_ge_10ms']} iteration(s) >= 10 ms")
    return out
