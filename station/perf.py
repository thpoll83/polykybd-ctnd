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
import glob
import json
import os
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
# v2 appends window_us to page 0 (the window's length by the keyboard's clock) and
# serves page 1 from a copy latched at the page-0 read. v1 is still decoded, with
# window_us None, so the harness keeps working against older profiling images.
SNAPSHOT_VERSION = 2
SNAPSHOT_VERSIONS = (1, 2)
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
    # v2 only: microseconds since RESET, by the keyboard's clock. None on v1.
    window_us: int | None = None
    version: int = SNAPSHOT_VERSION

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
        out = {
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
            "snapshot_version": self.version,
        }
        if self.window_us is not None:
            out["device_window_ms"] = round(self.window_us / 1000.0, 2)
        return out


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
    if len(head) < 4:
        raise ValueError(f"profiler snapshot page 0 too short: {len(head)} bytes")
    version, flags = head[0], head[1]
    if version not in SNAPSHOT_VERSIONS:
        raise ValueError(
            f"profiler snapshot version {version} not in {SNAPSHOT_VERSIONS} — "
            "the firmware's loop_profile.h wire format changed; update perf.py"
        )
    need0 = 40 if version >= 2 else 36
    if len(head) < need0:
        raise ValueError(f"profiler snapshot page 0 too short: {len(head)} < {need0}")
    (iters, ovl_iters, max_us, max_bridge_us, max_render_us,
     ovl_wall_us, ovl_bridge_us, ovl_render_us) = struct.unpack_from("<8I", head, 4)
    window_us = struct.unpack_from("<I", head, 36)[0] if version >= 2 else None

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
        window_us=window_us, version=version,
    )


class Profiler:
    """Drives the firmware's on-demand profiler over Raw HID (cmd 32)."""

    def __init__(self, raw: RawHID, log: Callable[[str], None] = print):
        self._raw = raw
        self._log = log
        # Bookkeeping for the window opened by the last reset():
        #   retries  - replies RawHID.send() waited out and re-sent for
        #   failed   - requests that got no reply after every attempt
        #   host_window_s - RESET reply to page-0 reply, on the host's clock
        self.retries = 0
        self.failed = 0
        self.host_window_s = None
        self._t_reset = None

    def _hid_counters(self) -> tuple:
        return (getattr(self._raw, "lost_replies", 0),
                getattr(self._raw, "timeouts_failed", 0))

    def _exchange(self, sub: int, page: int = 0) -> bytes | None:
        """Send one profiler sub-command; return the report body, or None on NACK.

        None means the firmware answered but refused (``P<32>!``) — which on a
        normal build is the dispatcher's unknown-command NACK, i.e. "no profiler
        here". A dropped reply raises, because that is a device fault rather than
        a capability answer, and must not be mistaken for "profiler absent"."""
        rec0, fail0 = self._hid_counters()
        try:
            resp = self._raw.send(bytes([POLY_CHANNEL, CMD_PROFILE, sub, page]))
        finally:
            rec1, fail1 = self._hid_counters()
            self.retries += rec1 - rec0
            self.failed += fail1 - fail0
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
        self.host_window_s = None
        if self._exchange(PROF_SUB_RESET) is None:
            raise ProfilerUnavailable("firmware NACKed the profiler RESET (cmd 32) — "
                                      "not a POLYKYBD_LOOP_PROFILE build")
        self._t_reset = time.perf_counter()
        # Zeroed AFTER the RESET reply: a lost RESET reply delays the window's start,
        # it does not stretch the window, so it must not count against it.
        self.retries = 0
        self.failed = 0

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
            if page == 0 and self._t_reset is not None:
                # Page 0 is where the firmware snapshots its counters, so the
                # window ends at ITS reply, not at the end of the host's sleep.
                self.host_window_s = time.perf_counter() - self._t_reset
        prof = decode_snapshot(pages)
        if self.retries or self.failed:
            self._log(f"[perf]   WARNING: {self.retries} profiler reply(ies) lost and "
                      f"re-sent in this window ({self.failed} never answered)")
        return prof

    def window_info(self) -> dict:
        """The retry count and host-side window length for the last reset()/read()."""
        out = {"hid_retries": self.retries, "hid_failed": self.failed}
        if self.host_window_s is not None:
            out["host_window_ms"] = round(self.host_window_s * 1000.0, 1)
        return out

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
    out.update(profiler.window_info())
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


# --- recorded app switches ------------------------------------------------------
#
# The bursts above send blank images to 8 keys. A real app switch uploads 30-100
# images in the smallest of four encodings, maps ~40-120 display positions, and
# pauses 0.3 s after every 15 image reports. These fixtures are that stream,
# recorded from PolyKybdHost's own send_overlays_mru (perf/fixtures/
# capture_app_switch.py), so the replay is exactly what a user's host sends.

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "perf", "fixtures")


def load_app_switch_fixtures(directory: str = FIXTURE_DIR) -> dict:
    """``{name: fixture}`` for every ``app_switch_<name>.json`` in ``directory``."""
    out = {}
    for path in sorted(glob.glob(os.path.join(directory, "app_switch_*.json"))):
        name = os.path.basename(path)[len("app_switch_"):-len(".json")]
        with open(path, encoding="utf-8") as fh:
            out[name] = json.load(fh)
    return out


def replay_app_switch(raw: RawHID, ops: list, pace: bool = True) -> dict:
    """Send one recorded phase in order.

    ``write`` ops are fire-and-forget like ``send_multiple``; consecutive writes go
    out on one handle. ``request`` ops wait for the reply like
    ``send_and_read_validate``. ``pause`` ops are the host's rate-limit sleeps and
    are honoured when ``pace`` is set, because they are part of what a user waits
    for.

    A request counts as answered only when the reply echoes its command with the
    ACK marker. A NACK or no reply stops the replay there: after a failed prepare
    the uploads would land against a mapping nobody reset, and after a failed
    enable the phase did not happen. ``failed_request`` names the op index."""
    pending: list = []
    stats = {"writes": 0, "requests": 0, "unanswered": 0, "pauses": 0, "pause_s": 0.0,
             "failed_request": None}

    def flush():
        if pending:
            raw.write_reports(list(pending))
            stats["writes"] += len(pending)
            pending.clear()

    for index, op in enumerate(ops):
        kind = op["op"]
        if kind == "write":
            pending.append(bytes.fromhex(op["hex"]))
        elif kind == "request":
            flush()
            stats["requests"] += 1
            data = bytes.fromhex(op["hex"])
            resp = raw.send(data, timeout_ms=1000, attempts=1)
            acked = (bool(resp) and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                     and resp[1] == data[1] and resp[2] == ACK)
            if not acked:
                if not resp:
                    stats["unanswered"] += 1
                stats["failed_request"] = index
                break
        elif kind == "pause":
            flush()
            stats["pauses"] += 1
            stats["pause_s"] += op["s"]
            if pace:
                time.sleep(op["s"])
        else:
            raise ValueError(f"unknown fixture op: {kind!r}")
    flush()
    stats["pause_s"] = round(stats["pause_s"], 2)
    return stats


def measure_app_switch(raw: RawHID, profiler: Profiler, log: Callable[[str], None],
                       fixture: dict, phase: str) -> dict:
    """Replay one phase (``cold`` or ``warm``) of a recorded app switch inside a
    profiler window.

    ``cold`` uploads every image, as the first switch into an app does and as
    every switch after a reconnect does. ``warm`` is the same switch with every
    image already in the pool: prepare + mapping + enable only. ``warm`` is also
    the lower bound for a switch whose images come from a flash icon library
    (qmk_firmware OVERLAY_ICON_LIBRARY_DESIGN.md), which adds only its fill
    reports on top."""
    ops = fixture[phase]
    name = fixture.get("stem", "?")
    reports = sum(1 for o in ops if o["op"] != "pause")
    log(f"[perf] app switch {name} ({phase}): {reports} reports")
    profiler.reset()
    t0 = time.perf_counter()
    stats = replay_app_switch(raw, ops)
    settled = _settle_after_burst(raw, log)
    wall_ms = (time.perf_counter() - t0) * 1000.0
    prof = profiler.read()

    out = prof.to_dict()
    out.update(profiler.window_info())
    out.update(stats)
    out.update({
        "stem": name,
        "phase": phase,
        "reports": reports,
        "host_wall_ms": round(wall_ms, 1),
        "host_wall_excl_pause_ms": round(wall_ms - stats["pause_s"] * 1000.0, 1),
        "settled": settled,
        # Only a phase that ran to its enable and let the master answer again
        # measured a whole app switch. Anything else is clipped, and must not
        # reach a baseline comparison (see perf_runner.metric_is_usable).
        "valid": settled and stats["failed_request"] is None,
    })
    if stats["failed_request"] is not None:
        log(f"[perf]   WARNING: request #{stats['failed_request']} was not ACKed — "
            "replay stopped, this phase is not a valid measurement")
    log(f"[perf]   host {out['host_wall_ms']} ms ({stats['pause_s']} s paused), "
        f"overlay {out['ovl_wall_ms']} ms: bridge {out['ovl_bridge_ms']} / "
        f"render {out['ovl_render_ms']} / rest {out['ovl_rest_ms']} ms, "
        f"worst iteration {out['worst_iter_ms']} ms, "
        f"{out['long_iters_ge_10ms']} iteration(s) >= 10 ms")
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
    out.update(profiler.window_info())
    out.update(idle_rate(prof, seconds, profiler.host_window_s, profiler.retries))
    if out.get("iters_per_s") is not None:
        log(f"[perf]   {out['iters_per_s']} loop iterations/s over "
            f"{out['window_s']} s ({out['window_source']} clock), "
            f"worst {out['worst_iter_ms']} ms, "
            f"{out['long_iters_ge_10ms']} iteration(s) >= 10 ms")
    return out


def idle_rate(prof: LoopProfile, nominal_s: float, host_window_s: float | None,
              retries: int) -> dict:
    """Loop rate over the window the firmware actually measured.

    Dividing by the seconds the host SLEPT was wrong whenever a READ reply was lost:
    RawHID.send() waits 3 s per attempt, so two retries turned a 3 s window into 9 s
    and reported the idle loop at 3013/s while it ran at 1004/s (run 37152259246).

    Prefers the keyboard's own window_us (snapshot v2), then the host's RESET-reply
    to page-0-reply time. A v1 window that needed retries is marked invalid: its
    page 1 was read live seconds after page 0, so the histogram does not match the
    counters.

    >>> p = LoopProfile(iters=9039, window_us=9_001_000)
    >>> r = idle_rate(p, 3.0, 9.0, 4)
    >>> r["iters_per_s"], r["window_source"], r.get("valid")
    (1004.2, 'device', None)
    >>> r = idle_rate(LoopProfile(iters=9039, version=1), 3.0, 9.0, 4)
    >>> r["iters_per_s"], r["window_source"], r["valid"]
    (1004.3, 'host', False)
    """
    out = {"nominal_window_s": nominal_s}
    if prof.window_us is not None:
        window_s, source = prof.window_us / 1e6, "device"
    elif host_window_s:
        window_s, source = host_window_s, "host"
    else:
        window_s, source = nominal_s, "nominal"
    out.update({"window_s": round(window_s, 3), "window_source": source,
                "iters_per_s": round(prof.iters / window_s, 1) if window_s > 0 else None})
    if prof.window_us is None and retries:
        out["valid"] = False
    return out
