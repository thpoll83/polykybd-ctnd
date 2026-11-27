# SPDX-License-Identifier: GPL-2.0-only
"""Report-only checks of both halves' flash cue and status displays (camera).

Two checks, both measured from recordings the runner makes anyway or makes
for them; each reports pass or xfail and never fails a run:

* staging: the cyan cue starts on both halves within ONSET_MAX_MS of each other
  (keys_com/rgb.c starts it at the first written chunk, the slave 50 ms later),
  and both status displays leave the picture they showed before BEGIN (the
  firmware-update screen, lib/display/oled_helper.c).
* reset: on the HID reboot (cmd 43) with the lights off, neither half's LEDs
  change in the first RESET_WINDOW_MS (lib/fw_update/fw_apply.c: no orange on a
  plain reboot), and both status displays go dark.
"""
from . import apply_cue

SIDES = ("left", "right")   # the left board is the master on the rig

STAGING_TEST_NAME = "flash cue starts together, update screen on both halves (camera)"
RESET_TEST_NAME = "no cue on a plain reboot, both displays reset (camera)"

# One master LED update while chunks stream (98 ms measured). Measured starts: 601 ms
# before qmk_firmware#490, then -49, +33 and -65 ms (slave minus master).
ONSET_MAX_MS = 100.0
LED_CHANGE = apply_cue.CHANGE   # a change of the LED mean luma that counts
# Calibrated on the rig 2026-11-27 (two staging runs, 606 frames each): the normal
# status page differs from itself by 2.9 (left) and 5.4 (right) over 3 s; during
# staging every frame differs by 10.4 to 40.6, median 28 to 32.
SCREEN_CHANGE = 10.0      # mean absolute luma difference that counts as another picture
SCREEN_FRACTION = 0.5     # the share of staging frames that must show another picture
RESET_WINDOW_MS = 1000.0  # after cmd 43: the reboot itself, before the firmware boots


def screen_diff(a: list, b: list) -> float:
    """Mean absolute difference of two display_samples() lists (0 when empty)."""
    n = min(len(a), len(b))
    return sum(abs(a[i] - b[i]) for i in range(n)) / n if n else 0.0


def first_rise(samples: list, side: str, change: float = LED_CHANGE):
    """t_ms of the first sample whose LED level is above the first sample's by
    more than ``change``; None when it never is. samples: [(t_ms, {side: led})]."""
    if not samples:
        return None
    base = samples[0][1][side]
    for t, lv in samples:
        if lv[side] - base > change:
            return t
    return None


def staging_report(led: list, screens: list, baseline: dict) -> dict:
    """led: [(t_ms, {side: led mean})] from the staging recording; screens:
    [{side: display_samples}] for the same frames; baseline: {side:
    display_samples} from a clip before BEGIN."""
    onset = {sd: first_rise(led, sd) for sd in SIDES}
    changed = {}
    for sd in SIDES:
        diffs = [screen_diff(baseline[sd], f[sd]) for f in screens]
        changed[sd] = (sum(d >= SCREEN_CHANGE for d in diffs) / len(diffs)) if diffs else 0.0
    return {"onset_ms": onset, "screen_changed": changed}


def judge_staging(rep: dict) -> dict:
    problems = []
    on = rep["onset_ms"]
    if on["left"] is None or on["right"] is None:
        problems.append("the cue did not start on "
                        + " and ".join(sd for sd in SIDES if on[sd] is None))
        gap = None
    else:
        gap = on["right"] - on["left"]
        if abs(gap) > ONSET_MAX_MS:
            problems.append(f"the cue starts {gap:+.0f} ms apart (slave minus master), "
                            f"more than {ONSET_MAX_MS:.0f} ms")
    for sd in SIDES:
        if rep["screen_changed"][sd] < SCREEN_FRACTION:
            problems.append(f"the {sd} status display kept its picture "
                            f"({rep['screen_changed'][sd]:.0%} of frames changed)")
    info = (f"cue start slave minus master {gap:+.0f} ms" if gap is not None
            else "cue start not measured") + "; display changed in " + ", ".join(
        f"{sd} {rep['screen_changed'][sd]:.0%}" for sd in SIDES) + " of frames"
    rec = {"name": STAGING_TEST_NAME, "info": info,
           "status": "xfail" if problems else "pass"}
    if problems:
        rec["reason"] = "; ".join(problems)
    return rec


def reset_report(led: list, displays: list, t_cmd_ms: float) -> dict:
    """led: [(t_ms, {side: led mean})]; displays: [(t_ms, {side: p95})]; t_cmd_ms:
    when cmd 43 was sent, on the same time base."""
    before = [lv for t, lv in led if t < t_cmd_ms]
    after = [lv for t, lv in led if t_cmd_ms <= t < t_cmd_ms + RESET_WINDOW_MS]
    rep = {"led_change": {}, "dark_ms": {}}
    for sd in SIDES:
        base = (sum(lv[sd] for lv in before) / len(before)) if before else None
        rep["led_change"][sd] = (max(abs(lv[sd] - base) for lv in after)
                                 if base is not None and after else None)
        rep["dark_ms"][sd] = next((t - t_cmd_ms for t, p in displays
                                   if t >= t_cmd_ms and p[sd] <= _DARK), None)
    return rep


_DARK = 10   # camera.DISPLAY_DARK_MAX, kept here so this module needs no camera import


def judge_reset(rep: dict) -> dict:
    problems = []
    for sd in SIDES:
        ch = rep["led_change"][sd]
        if ch is None:
            problems.append(f"no {sd} LED samples around the reboot")
        elif ch > LED_CHANGE:
            problems.append(f"the {sd} LEDs changed by {ch:.0f} during the reboot "
                            "(a cue on a plain reboot)")
        if rep["dark_ms"][sd] is None:
            problems.append(f"the {sd} status display never went dark")
    info = "LED change " + ", ".join(
        f"{sd} {rep['led_change'][sd]:.1f}" if rep["led_change"][sd] is not None
        else f"{sd} n/a" for sd in SIDES) + "; display dark after " + ", ".join(
        f"{sd} {rep['dark_ms'][sd]:.0f} ms" if rep["dark_ms"][sd] is not None
        else f"{sd} never" for sd in SIDES)
    rec = {"name": RESET_TEST_NAME, "info": info,
           "status": "xfail" if problems else "pass"}
    if problems:
        rec["reason"] = "; ".join(problems)
    return rec
