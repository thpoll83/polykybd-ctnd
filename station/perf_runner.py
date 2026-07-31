# SPDX-License-Identifier: GPL-2.0-only
"""Flash a profiling firmware, run the performance workloads, report the numbers.

This is the automated replacement for "deploy a build by hand, poke the keyboard,
copy the ``LoopProf:`` block out of the console". It flashes the pair of
``POLYKYBD_LOOP_PROFILE`` HIL images, waits for the master to settle using the
*same* readiness gates the HIL suite uses, then drives each workload inside a
bounded profiler window (see :mod:`station.perf`) and emits:

* a JSON report (``--json``) — the machine-readable record,
* a markdown summary (the GitHub Actions job summary, and ``--markdown``),
* a comparison against a stored baseline, so a regression is visible without
  anyone remembering last week's numbers.

Run it::

    python -m station.perf_runner --left  fw/…_hil_left.uf2 \\
                                  --right fw/…_hil_right.uf2 \\
                                  --json perf.json --markdown perf.md

``--no-flash`` measures whatever is already on the rig (useful when iterating on
the harness itself), and ``--update-baseline`` records the run as the new
reference.

**Reporting, not gating.** A regression is surfaced loudly but never fails the
process: these are wall-clock measurements on real hardware sharing a rig with
other work, and a flaky red check trains people to ignore it. Only a *measurement*
failure (no profiler in the firmware, device not responding) exits non-zero.
"""
import argparse
import json
import os
import sys
import time

from .hid import HIDConsole
from .perf import (
    Profiler, ProfilerUnavailable, measure_hid_latency, measure_idle_overhead,
    measure_overlay_burst,
)
from .test_runner import (
    POLY_CHANNEL, CMD_GET_ID, TestRunner, _derive_label,
)
from .hil_tests import parse_device_caps

# Report schema version — bumped if the JSON layout changes, so a baseline
# recorded by an older harness is refused rather than compared field-by-field
# against a report that means something different.
REPORT_SCHEMA = 1

# Metrics compared against the baseline. Each entry is
# (dotted path into the report, human label, unit, higher_is_worse).
# Deliberately a curated list rather than "every number": the histograms and
# iteration counts are context for a human reading the report, but they move with
# workload size and would only add noise to a regression table.
TRACKED_METRICS = [
    ("overlay_plain.worst_iter_ms",       "Overlay burst (plain) — worst iteration",   "ms",   True),
    ("overlay_plain.ovl_wall_ms",         "Overlay burst (plain) — total overlay time", "ms",  True),
    ("overlay_plain.ovl_bridge_ms",       "Overlay burst (plain) — bridge (to slave)", "ms",   True),
    ("overlay_plain.ovl_render_ms",       "Overlay burst (plain) — render (keycaps)",  "ms",   True),
    ("overlay_compressed.worst_iter_ms",  "Overlay burst (RLE/core1) — worst iteration", "ms", True),
    ("overlay_compressed.ovl_wall_ms",    "Overlay burst (RLE/core1) — total overlay time", "ms", True),
    ("hid_latency.p50",                   "HID round-trip p50",                        "ms",   True),
    ("hid_latency.p95",                   "HID round-trip p95",                        "ms",   True),
    ("hid_latency.max",                   "HID round-trip max",                        "ms",   True),
    ("idle.worst_iter_ms",                "Idle — worst iteration",                    "ms",   True),
    ("idle.iters_per_s",                  "Idle — main-loop rate",                     "/s",   False),
    ("timing.boot_to_ready_s",            "Boot to first stable HID",                  "s",    True),
]

# A metric must move by more than this to be called a regression/improvement.
# Rig measurements are noisy (USB scheduling, the rig's own load, thermal drift),
# and a 5% band flagged essentially every run in practice.
DEFAULT_TOLERANCE_PCT = 15.0

# Console lines worth keeping in the report for a human reading it later: the
# profiler's own summary block and the split-link health counter.
CONSOLE_KEEP_PREFIXES = ("LoopProf:", "  norm", "  ovl", "  ovltot", "Split link:")
CONSOLE_TAIL_MAX = 40


def dig(data: dict, path: str):
    """Fetch a dotted path out of a nested dict, or None if any hop is missing.

    >>> dig({"a": {"b": 2}}, "a.b")
    2
    >>> dig({"a": {}}, "a.b") is None
    True
    >>> dig({}, "a.b.c") is None
    True
    """
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def compare_to_baseline(report: dict, baseline: dict,
                        tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> list:
    """Diff the tracked metrics against a baseline report.

    Returns one record per comparable metric with a ``verdict`` of ``regression``
    / ``improvement`` / ``same``. Metrics absent from either side are skipped
    rather than reported as a change — a newly added measurement must not look
    like an infinite regression on its first run.

    >>> rep  = {"hid_latency": {"p50": 11.0}}
    >>> base = {"hid_latency": {"p50": 10.0}}
    >>> [r["verdict"] for r in compare_to_baseline(rep, base, tolerance_pct=15)]
    ['same']
    >>> [r["verdict"] for r in compare_to_baseline(rep, base, tolerance_pct=5)]
    ['regression']
    >>> compare_to_baseline({"hid_latency": {"p50": 11.0}}, {}) # metric absent -> skipped
    []
    >>> c = compare_to_baseline({"idle": {"iters_per_s": 500.0}},
    ...                         {"idle": {"iters_per_s": 1000.0}}, tolerance_pct=5)
    >>> c[0]["verdict"], c[0]["delta_pct"]        # lower rate is worse
    ('regression', -50.0)
    """
    out = []
    for path, label, unit, higher_is_worse in TRACKED_METRICS:
        cur, base = dig(report, path), dig(baseline, path)
        if cur is None or base is None:
            continue
        try:
            cur, base = float(cur), float(base)
        except (TypeError, ValueError):
            continue
        if base == 0:
            # No meaningful percentage against a zero baseline; report the raw
            # move so it is still visible, but never call it a regression.
            delta_pct = None
            verdict = "same"
        else:
            delta_pct = round((cur - base) / abs(base) * 100.0, 1)
            worse = delta_pct > 0 if higher_is_worse else delta_pct < 0
            if abs(delta_pct) <= tolerance_pct:
                verdict = "same"
            else:
                verdict = "regression" if worse else "improvement"
        out.append({
            "metric": path, "label": label, "unit": unit,
            "current": round(cur, 2), "baseline": round(base, 2),
            "delta_pct": delta_pct, "verdict": verdict,
        })
    return out


def format_markdown(report: dict, comparison: list = None,
                    tolerance_pct: float = DEFAULT_TOLERANCE_PCT) -> str:
    """Render the report as the markdown posted to the CI job summary."""
    label = report.get("label") or "PolyKybd"
    dev = report.get("device", {})
    lines = [f"## ⏱️ Firmware performance — {label}", ""]
    if dev:
        lines.append(f"Firmware `{dev.get('fw', '?')}`, protocol "
                     f"`P{dev.get('protocol', '?')}`")
        lines.append("")

    regressions = [c for c in (comparison or []) if c["verdict"] == "regression"]
    if comparison:
        if regressions:
            lines.append(f"> ⚠️ **{len(regressions)} metric(s) regressed** beyond the "
                         f"±{tolerance_pct:g}% tolerance. Perf numbers are measured on "
                         "shared hardware — confirm with a re-run before treating one as real.")
        else:
            lines.append(f"> ✅ No metric moved beyond the ±{tolerance_pct:g}% tolerance.")
        lines += ["", "| metric | current | baseline | change |", "|---|---:|---:|---:|"]
        mark = {"regression": "🔺", "improvement": "🟢", "same": "▪️"}
        for c in comparison:
            delta = "n/a" if c["delta_pct"] is None else f"{c['delta_pct']:+.1f}%"
            lines.append(
                f"| {mark[c['verdict']]} {c['label']} | {c['current']} {c['unit']} "
                f"| {c['baseline']} {c['unit']} | {delta} |"
            )
        lines.append("")
    else:
        lines += ["_No baseline recorded yet — this run establishes the reference._", ""]

    lines += ["| measurement | value |", "|---|---:|"]
    for path, mlabel, unit, _ in TRACKED_METRICS:
        val = dig(report, path)
        if val is not None:
            lines.append(f"| {mlabel} | {val} {unit} |")
    lines.append("")

    # The bucket histograms answer "how many iterations were long enough to eat a
    # keystroke", which the scalar table cannot show.
    for key, title in (("overlay_plain", "Overlay burst (plain)"),
                       ("overlay_compressed", "Overlay burst (RLE/core1)"),
                       ("idle", "Idle")):
        section = report.get(key)
        if not section:
            continue
        norm, ovl = section.get("hist_norm", {}), section.get("hist_ovl", {})
        if not norm and not ovl:
            continue
        lines += [f"<details><summary>{title} — main-loop iteration histogram</summary>", ""]
        buckets = list(norm or ovl)
        lines.append("| iterations | " + " | ".join(buckets) + " |")
        lines.append("|---" * (len(buckets) + 1) + "|")
        lines.append("| normal | " + " | ".join(str(norm.get(b, 0)) for b in buckets) + " |")
        lines.append("| overlay | " + " | ".join(str(ovl.get(b, 0)) for b in buckets) + " |")
        lines += ["",
                  f"Iterations ≥ 10 ms (a fast tap can be missed inside one): "
                  f"**{section.get('long_iters_ge_10ms', 0)}**", "", "</details>", ""]

    tail = report.get("console_tail") or []
    if tail:
        lines += ["<details><summary>Firmware console (profiler + split link)</summary>",
                  "", "```"] + tail + ["```", "</details>", ""]
    return "\n".join(lines)


class PerfRunner:
    """Owns one end-to-end performance run."""

    def __init__(self, log=print):
        self.log = log
        self._runner = TestRunner(log=log)
        self._console = HIDConsole()
        self._console_lines = []

    def _device_info(self) -> dict:
        try:
            resp = self._runner.raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
        except Exception as exc:
            self.log(f"[perf] could not read device identity: {exc}")
            return {}
        if not resp or len(resp) < 3:
            return {}
        identity = bytes(resp[3:]).split(b"\x00", 1)[0].decode("ascii", "replace")
        return parse_device_caps(identity) or {}

    def _start_console(self) -> bool:
        """Best-effort HID console capture (needs CONSOLE_ENABLE in the firmware).

        Only the profiler/split-link lines are retained: the console also carries
        boot chatter and per-key traffic, which would bury the report."""
        def sink(msg: str) -> None:
            for line in msg.splitlines():
                if line.startswith(CONSOLE_KEEP_PREFIXES):
                    self._console_lines.append(line.rstrip())
                    del self._console_lines[:-CONSOLE_TAIL_MAX]
            self.log(f"[qmk] {msg}")

        try:
            self._console.start(sink)
            return True
        except Exception as exc:
            self.log(f"[perf] HID console unavailable (continuing without it): {exc}")
            return False

    def run(self, left_uf2: str = None, right_uf2: str = None, *,
            label: str = "", keys: int = 8, latency_n: int = 100,
            idle_s: float = 3.0) -> dict:
        timing = {}
        console_started = False
        try:
            if left_uf2 and right_uf2:
                t_flash = time.perf_counter()
                self._runner.flash_halves(left_uf2, right_uf2)
                timing["flash_s"] = round(time.perf_counter() - t_flash, 1)

                self.log("[perf] waiting for keyboard to enumerate...")
                t_boot = time.perf_counter()
                time.sleep(3)
                console_started = self._start_console()
                self._runner.wait_for_master_ready()
                timing["boot_to_ready_s"] = round(time.perf_counter() - t_boot, 1)
            else:
                self.log("[perf] --no-flash: measuring the firmware already on the rig")
                console_started = self._start_console()
                self._runner.wait_for_master_ready()

            # Same sustained-responsiveness gate the HIL suite uses. Without it the
            # first workload lands inside the master's boot-time busy window and
            # measures the boot, not the workload.
            t_settle = time.perf_counter()
            self._runner.settle_master()
            timing["settle_s"] = round(time.perf_counter() - t_settle, 1)

            self._runner.status = "testing"
            raw = self._runner.raw
            profiler = Profiler(raw, self.log)
            # Fail fast, and precisely. A NACK means the firmware has no profiler;
            # a missing reply means the device is not answering at all. Collapsing
            # the second into the first would send someone off rebuilding firmware
            # that was fine, so let each surface its own message.
            try:
                profiler.reset()
            except ProfilerUnavailable as exc:
                raise ProfilerUnavailable(
                    f"{exc}. Build the HIL images with `-e POLYKYBD_LOOP_PROFILE=yes` "
                    "— a normal build compiles the profiler out entirely."
                ) from exc

            device = self._device_info()
            if device:
                self.log(f"[perf] device: fw {device.get('fw')}, "
                         f"protocol P{device.get('protocol')}")

            report = {
                "schema": REPORT_SCHEMA,
                "label": label,
                "device": device,
                "timing": timing,
                "idle": measure_idle_overhead(raw, profiler, self.log, seconds=idle_s),
                "overlay_plain": measure_overlay_burst(raw, profiler, self.log,
                                                       kind="plain", keys=keys),
                "overlay_compressed": measure_overlay_burst(raw, profiler, self.log,
                                                            kind="compressed", keys=keys),
            }
            # Latency last: it is the only workload with no profiler window, and
            # running it after the bursts also samples the post-overlay recovery
            # the host actually experiences on a program switch.
            report["hid_latency"] = measure_hid_latency(raw, self.log, n=latency_n)

            # Leave a human-readable block in the captured console log too, so the
            # raw CI log tells the same story as the JSON.
            profiler.log_to_console()
            time.sleep(0.5)  # let the console reader drain the block

            report["console_tail"] = list(self._console_lines)
            self._runner.status = "idle"
            return report
        finally:
            if console_started:
                self._console.stop()
            self._runner._flash.cleanup()

    def cleanup(self) -> None:
        self._console.stop()
        self._runner.cleanup()


def write_github_summary(markdown: str) -> None:
    """Append the report to the GitHub Actions job summary (no-op locally)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(markdown + "\n")
    except OSError as exc:
        print(f"[perf] could not write GITHUB_STEP_SUMMARY: {exc}")


def load_baseline(path: str, log=print) -> dict:
    """Load a baseline report, or return {} when there isn't a usable one.

    A missing file is the normal first-run case. A schema mismatch is refused
    loudly rather than compared: fields with the same name can mean different
    things across schema versions, and a silently wrong comparison is worse than
    none."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        log(f"[perf] could not read baseline {path}: {exc}")
        return {}
    if data.get("schema") != REPORT_SCHEMA:
        log(f"[perf] baseline {path} has schema {data.get('schema')} != {REPORT_SCHEMA} "
            "— ignoring it; re-record with --update-baseline")
        return {}
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure PolyKybd firmware performance on the HIL rig")
    parser.add_argument("--left",  help="Path to the left-half (master) profiling UF2")
    parser.add_argument("--right", help="Path to the right-half (slave) profiling UF2")
    parser.add_argument("--no-flash", action="store_true",
                        help="Measure the firmware already on the rig (skip flashing)")
    parser.add_argument("--label", default=None,
                        help="Board name for the report title (default: inferred from --left)")
    parser.add_argument("--keys", type=int, default=8,
                        help="Keycodes per overlay burst (default: 8)")
    parser.add_argument("--latency-n", type=int, default=100,
                        help="GET_ID sends in the latency burst (default: 100)")
    parser.add_argument("--idle-seconds", type=float, default=3.0,
                        help="Idle baseline window in seconds (default: 3)")
    parser.add_argument("--json", dest="json_path", default=None,
                        help="Write the full report as JSON here")
    parser.add_argument("--markdown", dest="md_path", default=None,
                        help="Write the markdown summary here (also posted to the CI summary)")
    parser.add_argument("--baseline", default=None,
                        help="Baseline report to compare against "
                             "(default: perf/baselines/<label>.json)")
    parser.add_argument("--update-baseline", action="store_true",
                        help="Record this run as the new baseline")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_PCT,
                        help=f"Regression tolerance in percent (default: {DEFAULT_TOLERANCE_PCT:g})")
    args = parser.parse_args(argv)

    if not args.no_flash and not (args.left and args.right):
        parser.error("--left and --right are required unless --no-flash is given")

    label = args.label or _derive_label(args.left or "") or "split72"
    baseline_path = args.baseline or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "perf", "baselines", f"{label}.json")

    runner = PerfRunner()
    try:
        report = runner.run(
            None if args.no_flash else args.left,
            None if args.no_flash else args.right,
            label=label, keys=args.keys, latency_n=args.latency_n,
            idle_s=args.idle_seconds)
    except Exception as exc:
        # A measurement failure is a real failure (wrong build flashed, device
        # dead) — unlike a regression, which is only reported.
        print(f"[perf] FATAL: {exc}")
        markdown = (f"## ⏱️ Firmware performance — {label}\n\n"
                    f"> ❌ **Measurement failed:** `{exc}`\n")
        write_github_summary(markdown)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::error title=Perf measurement failed::{exc}")
        return 2
    finally:
        runner.cleanup()

    baseline = load_baseline(baseline_path)
    comparison = compare_to_baseline(report, baseline, args.tolerance) if baseline else []
    markdown = format_markdown(report, comparison, args.tolerance)

    print(markdown)
    write_github_summary(markdown)
    if args.md_path:
        with open(args.md_path, "w", encoding="utf-8") as fh:
            fh.write(markdown)
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print(f"[perf] wrote {args.json_path}")

    for c in comparison:
        if c["verdict"] == "regression" and os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::warning title=Perf regression::{c['label']}: "
                  f"{c['current']}{c['unit']} vs baseline {c['baseline']}{c['unit']} "
                  f"({c['delta_pct']:+.1f}%)")

    if args.update_baseline:
        os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
        with open(baseline_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
        print(f"[perf] baseline updated: {baseline_path}")

    return 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Same reason as test_runner: the native hid / RPi.GPIO libraries can abort
    # the process during interpreter shutdown, which would turn a completed run
    # into an exit-134 CI failure.
    os._exit(code)
