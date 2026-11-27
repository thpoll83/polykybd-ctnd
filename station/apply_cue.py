# SPDX-License-Identifier: GPL-2.0-only
"""The orange cue of both halves during a firmware apply, seen by the camera.

Reports only. The runner records from just before FW_UP_APPLY until the board
is back, and this module turns the recording into a per-half LED timeline:
when each half's LEDs change from the level before the apply (the orange cue
starts), and when they change again (the half reboots and its firmware writes
the LEDs). The firmware is ``keyboards/polykybd/lib/fw_update/fw_apply.c``
(``poly_board_unusable_cue``).
"""

RECORD_S = 20.0
LEAD_S = 2.0         # recorded before FW_UP_APPLY
READY_S = 10.0       # the longest wait for the first frame
# camera.record() limits each of its waits: 30 s for each of its two camera
# locks, then rpicam-vid's own timeout of seconds + 30. Waiting longer than their
# sum means a recorder thread is known to have ended when the wait returns.
RECORDER_MAX_S = 30 + 30 + RECORD_S + 30
STEP_MS = 100.0      # one logged sample per STEP_MS
CHANGE = 8.0         # a change of the LED mean luma that counts (camera levels)
SIDES = ("left", "right")


def levels(camera, rec, s, boards, step_ms: float = STEP_MS) -> list:
    """[(t_ms from the first frame, {side: led mean})], one sample per step_ms."""
    out, nxt = [], 0.0
    t0 = rec.timestamps_ms[0] if rec.timestamps_ms else 0.0
    for t, luma in zip(rec.timestamps_ms, rec.lumas):
        t -= t0
        if t < nxt:
            continue
        nxt += step_ms
        out.append((t, {sd: camera.half_levels(luma, rec.width, rec.height, s, sd,
                                               boards[sd])[0] for sd in SIDES}))
    return out


def events(samples: list, side: str, change: float = CHANGE) -> tuple:
    """(start_ms, end_ms) of the first plateau after the starting level: start is
    the first sample that differs from the first sample by more than ``change``,
    end the first later sample that differs from the level at start by more than
    ``change``. None where it did not happen."""
    if not samples:
        return None, None
    base = samples[0][1][side]
    start = level = None
    for t, lv in samples:
        v = lv[side]
        if start is None:
            if abs(v - base) > change:
                start, level = t, v
        elif abs(v - level) > change:
            return start, t
    return start, None


def summary(samples: list, t_apply_ms: float) -> list[str]:
    """Report lines, times relative to the FW_UP_APPLY command."""
    lines = []
    ev = {sd: events(samples, sd) for sd in SIDES}
    for sd in SIDES:
        a, b = ev[sd]
        fmt = lambda t: "never" if t is None else f"{(t - t_apply_ms) / 1000:+.2f} s"
        lines.append(f"[apply-cue] {sd}: LEDs changed {fmt(a)}, changed again {fmt(b)}"
                     + (f" (held {(b - a) / 1000:.2f} s)" if a is not None and b is not None
                        else ""))
    (la, lb), (ra, rb) = ev["left"], ev["right"]
    if la is not None and ra is not None:
        lines.append(f"[apply-cue] start: right minus left {(ra - la):+.0f} ms")
    if lb is not None and rb is not None:
        lines.append(f"[apply-cue] end: right minus left {(rb - lb):+.0f} ms")
    return lines
