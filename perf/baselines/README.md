# Performance baselines

One JSON file per board (`split72.json`, …), each a full report emitted by
`python -m station.perf_runner --json …`. The perf run compares the current
measurement against the file matching its `--label` and reports any metric that
moved by more than the tolerance (±15% by default).

**There is deliberately no automatic baseline update.** A baseline that rewrites
itself every run ratchets a slow regression in silently — each run looks fine
against yesterday, and a 3× slowdown accumulated over a month is never flagged.
Moving the baseline is a deliberate, reviewable commit.

## Recording or moving a baseline

1. Run the measurement — from CI (the opt-in `Performance measurement (split72)`
   job in `qmk_firmware`'s `qmk-test.yml`) or on the rig directly.
2. Take the run's `perf-report.json` (a CI artifact, or the `--json` output).
3. Commit it here as `<label>.json`, with a message saying *why* the numbers
   moved — a firmware change that legitimately costs time, new hardware, a
   changed workload size.

The runner also accepts `--update-baseline` for local iteration, but note that CI
force-syncs the rig's checkout to `origin/main` before every run, so anything it
writes there is discarded on the next one. The committed file is the only
baseline that survives.

## No baseline yet?

The first run reports its numbers and says *"No baseline recorded yet — this run
establishes the reference"*. Nothing fails; commit that run's JSON here when the
numbers look sane, and subsequent runs get a comparison table.

A baseline whose `schema` does not match the runner's `REPORT_SCHEMA` is ignored
with a warning rather than compared — fields can change meaning between schema
versions, and a silently wrong comparison is worse than none. Re-record it after
a schema bump.
