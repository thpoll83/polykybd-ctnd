---
paths:
  - "station/perf.py"
  - "station/perf_runner.py"
---
# Editing the perf harness

Full notes: `docs/PERF_HARNESS.md`.

- ⚠️ **cmd 32 NACKs on a normal build by design** — `case 32` is inside
  `#ifdef POLYKYBD_LOOP_PROFILE`, and the NACK is the capability signal. "Not a
  POLYKYBD_LOOP_PROFILE build" means the wrong images were flashed, not a fault.
- ⚠️ **A `HIDConsole` read is a report-sized FRAGMENT, not a line.** Buffer across
  reads, classify only `\n`-terminated lines, and flush the trailing fragment when
  the reader stops — matching a raw chunk drops every continuation and truncates
  what it keeps (`ovltot wall=16ms bridg` shipped once).
- **Reuse `TestRunner`'s readiness gates** (`flash_halves` / `wait_for_master_ready`
  / `settle_master`); skipping the sustained settle measures the master's boot-time
  busy window instead of the workload.
- ⚠️ **Never auto-update a baseline** — it ratchets a slow regression in silently.
- `LOOP_PROFILE_SNAPSHOT_VERSION` (firmware) and `SNAPSHOT_VERSION` here move together.
