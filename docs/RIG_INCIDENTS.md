# Rig incidents

A dated log of every HIL failure whose cause was the rig, the rig's test code or the
rig's setup, and of failures that are still unexplained. Newest first. The suite notes
(`HIL_SUITE_NOTES.md`) say how each check works; this file says what went wrong and when.

Add a row whenever a HIL run is red or never ran and the firmware under test was not the
cause, and whenever a failure stays unexplained. An unexplained row is the useful one:
the second time the same symptom appears, the row shows it is a pattern.

Each entry has:

- **Date** (UTC) and the **run** (`qmk_firmware` Actions run id, and the attempt if a re-run).
- **Symptom**: the log line to grep for, copied exactly.
- **Class**: `rig hardware`, `rig network`, `rig setup` (a unit, grant or package missing on
  the Pi), `stale rig code`, `rig test` (the ctnd test was wrong), or `unexplained`.
- **Cause and fix**: what it was, and the PR or action that fixed it.
- **Cost**: what it blocked (a release with no files, a PR held red, days of a recurring flake).

The triage steps are in the `diagnose-hil-failure` skill (in `qmk_firmware`).

## Open

### 2026-11-13: FW_UP_COMMIT got no reply after a complete staging
- **Run:** 43317964268, attempt 2 ("Firmware apply round-trip (split72)", the qmk#435
  merge commit, firmware 1.13.3 image). Attempt 1 of the same run passed this step.
- **Symptom:** `FAIL: FW_UP_COMMIT — no reply`, after `all 599688 bytes delivered` and
  `signature sent`. Both signature halves waited their 2 s timeout, so the master did not
  answer HID for more than 10 s.
- **Class:** unexplained. The console is stopped during the firmware-update section, so
  the log has nothing from the keyboard for that window.
- **Notes:** firmware 1.13.3 added a combo check that runs every 60 s on the master and is
  not paused during a firmware update. One check is a single split message, so it is not
  a likely cause; nothing in the log links them.
- **Next step:** if it recurs, run a debug probe that keeps the console open across COMMIT.
- **Cost:** firmware 1.13.4 stayed published with no files.

## Closed

### 2026-11-13: combo commit "refused" after a firmware apply
- **Runs:** 43226924017 (qmk#428) and 43317964268 attempt 1 (qmk#435).
- **Symptom:** `[test] FAIL: reboot persistence: the keyboard refused combo [13, 14, 0, 0, 41] or its commit`
- **Class:** rig test.
- **Cause and fix:** after an apply the slave installs the master's image and comes back as
  a second master, so no commit can reach the other half. The reboot-persistence check
  required the other-half bit. ctnd#148 checks this half only in that state.
- **Cost:** the release gate refused firmware 1.13.4, published with no files.

### 2026-10-13 to 2026-10-14: USB-suspend check without its sudo grant
- **Symptom:** the USB-suspend check failed on every extended run; the firmware-apply job
  then skipped the apply (`skipping the firmware apply round-trip — the suite already has a failure`).
- **Class:** rig setup.
- **Cause and fix:** ctnd#114 added the check and its root wrapper, but `setup.sh` runs only
  by hand on the Pi, so the grant was never installed. ctnd#116 made the check SKIP while
  the grant is absent. Installing it: `sudo bash ./scripts/setup.sh --units-only` on the Pi.
- **Cost:** firmware 1.9.2 was published with no files.

### 2026-07: rig ran stale station code for about 5 days
- **Symptom:** the boot-burst flake (an early read times out, everything after passes) kept
  coming back. The settle line read `need=3` instead of `need=15`.
- **Class:** stale rig code.
- **Cause and fix:** HIL ran the installed `/opt/polykybd-ctnd`, and the self-update timer
  had not fast-forwarded it. CI now force-syncs the rig to ctnd `main` before each run.

### 2026-07: rig test with a stale `POLY_OS_COUNT`
- **Symptom:** one test failed on a wrong value: `SET_OS(6)` was ACKed where the test
  expected a NACK.
- **Class:** rig test.
- **Cause and fix:** the rig's mirror said 6 OSes; the firmware enum had grown to 8. The
  ctnd test was updated.

### Structural: two masters after every apply
- **Run where it was measured:** 33733020495 (12930 of 12930 frames failed, `crc_err=0`).
- **Symptom:** `2 Raw HID interfaces after the apply`.
- **Class:** rig setup (by design).
- **Cause:** the rig flashes different images to the two halves (`POLYKYBD_HIL=left/right`),
  and an apply gives both the master's image. The apply round-trip reports this and does
  not grade it; `post_apply_split_link` re-flashes the slave to measure the link.

## Recurring classes with no dated entry yet

Add a dated row the next time one of these happens.

- **Rig network:** the job ends at `Prepare all required actions` with
  `Failed to download action 'https://codeload.github.com/...'`. The runner was online;
  only `codeload.github.com` was unreachable.
- **Rig offline:** `HIL test (split72)` stays `queued` with no log. Nothing fails, so
  nobody is alerted.
- **No enumeration:** `raw HID interfaces present: 0` and every test fails. One re-run; if
  it repeats, power-cycle or reseat a half.
- **Missing unit on an older rig:** a unit added after the rig was set up is never
  installed. `sudo bash ./scripts/setup.sh --units-only`.
