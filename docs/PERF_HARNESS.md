# Firmware performance harness

Moved out of `CLAUDE.md` 2026-09-14. Verbatim.

## Performance measurement (`station/perf.py`, `station/perf_runner.py`)

The rig does more than pass/fail HIL testing: it can **measure firmware
performance automatically**, replacing the old loop of "deploy a build by hand,
poke the keyboard, paste the `LoopProf:` block from the console".

- **What drives it**: the firmware's main-loop profiler
  (`qmk_firmware/keyboards/polykybd/profiling/`, built with
  `-e POLYKYBD_LOOP_PROFILE=yes`) plus its **on-demand control command, HID cmd
  32** (`RESET` / `READ` / `LOG`). Every measurement is therefore a *bounded
  window*: RESET → run one workload → READ the counters back as binary. ⚠️ The
  periodic console block alone cannot do this — its counters are cumulative from
  boot and `worst` is an all-time maximum, so it can never attribute a number to
  a specific workload.
- **⚠️ cmd 32 NACKs on a normal build, by design.** The whole `case 32` is inside
  `#ifdef POLYKYBD_LOOP_PROFILE`. That NACK is the capability signal — it is how
  the harness distinguishes "no profiler in this firmware" from a real answer
  instead of reporting a page of zeros. If a perf run says *"not a
  POLYKYBD_LOOP_PROFILE build"*, the wrong images were flashed.
- **Workloads** (each in its own profiler window): a quiet-loop **idle baseline**
  (the control — without it a burst number has no reference), an **overlay burst**
  in both flavours (plain cmd 10, and RLE/core1 cmd 16), and a host-side **HID
  round-trip latency** burst (p50/p95/p99/max). Boot-to-first-stable-HID comes
  from the runner, which owns the flash timing.
- **Run it**: `python -m station.perf_runner --left …_perf_hil_left.uf2 --right
  …_perf_hil_right.uf2 --json perf.json --markdown perf.md`. `--no-flash`
  measures whatever is already on the rig (handy when iterating on the harness).
  The touch UI has a **Measure Perf** button (`run_perf` → `PerfRunner`) that
  takes the same selected firmware pair as **Run Tests**.
- **CI: opt-in, report-only.** The `Performance measurement (split72)` job in
  qmk's `qmk-test.yml` runs on the `hil-perf` PR label (⚠️ renamed from `perf`
  2026-08-29 — the bare `perf`/`[perf]` names now fire nothing), `[hil-perf]` in a
  commit message, or a manual `workflow_dispatch`. It posts a markdown table to the job summary +
  a PR comment and uploads `perf-report.json`. It **never fails on a regression** —
  these are wall-clock numbers on shared hardware and a flaky red check is one
  people learn to ignore; only a *measurement* failure (wrong build, dead device)
  exits non-zero. It is ordered `needs: [build-perf, hil-test]` with `always()`,
  so the two rig jobs can't interleave their flashes but a red HIL suite still
  gets a perf number (often exactly what explains a timing-related HIL failure).
- **Baselines** live in `perf/baselines/<label>.json`, committed. ⚠️ There is
  **deliberately no automatic baseline update** — a self-rewriting baseline
  ratchets a slow regression in silently. Move it by committing a run's JSON (see
  `perf/baselines/README.md`). `--update-baseline` exists for local iteration, but
  CI force-syncs the rig checkout to `origin/main` before every run, so anything
  written there is discarded next run.
- **Reuse, don't duplicate, the readiness gates.** `PerfRunner` composes
  `TestRunner` and calls its (now public) `flash_halves()`, `wait_for_master_ready()`
  and `settle_master()`. The sustained-settle logic is subtle and load-bearing (see
  the stale-rig warning above); a perf run that skipped it would measure the
  master's boot-time busy window instead of the workload.
- ⚠️ **A `HIDConsole` read is a report-sized FRAGMENT, not a line.** QMK's console
  delivers whatever fitted in one 32/64-byte report, so a long line (a `LoopProf:`
  block, a `Split link:` summary) arrives split across several reads — and a split
  can land mid-word. Anything that *filters or parses* console output must buffer
  and reassemble across reads, and only classify lines terminated by `\n`; matching
  the raw chunk instead silently drops every continuation fragment and truncates
  what it keeps (`ovltot wall=16ms bridg` — shipped once, fixed in `perf_runner.py`
  `_start_console`/`_flush_console`). Also flush the trailing unterminated fragment
  when the reader stops, or the last — usually most interesting — line is the one
  that goes missing. `test_runner.py` is unaffected only because it just echoes each
  chunk to the log verbatim and never parses it.
- **Offline tests**: `tests/perf_test.py` (`python -m unittest discover -s tests -p
  "*_test.py"`, 23 tests, no hardware). Its `FakeProfilerDevice` re-implements the
  firmware's cmd-32 replies byte for byte, so it is a genuine **contract test of
  the wire format** — if the C encoder and the Python decoder ever disagree on
  layout/ordering/endianness it fails there rather than producing plausible
  nonsense on the rig. `LOOP_PROFILE_SNAPSHOT_VERSION` (firmware) and
  `SNAPSHOT_VERSION` (`perf.py`) must move together; a mismatch is refused loudly.

