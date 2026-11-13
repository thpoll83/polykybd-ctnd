# Rig incidents

A dated log of every HIL failure whose cause was the rig, the rig's test code or the
rig's setup, and of failures that are still unexplained. Newest first. The suite notes
(`HIL_SUITE_NOTES.md`) say how each check works; this file says what went wrong and when.

Add an entry whenever a HIL run is red or never ran and the firmware under test was not
the cause, and whenever a failure stays unexplained. Before you classify a new failure,
grep this file for the fixed text of its symptom line, without the values that change from
run to run (byte counts, CRCs, status bytes, run ids). A match is only a candidate repeat:
one line can have several causes (the bulk pipe soak, for example, prints the same
`count/CRC/bad-frame mismatch` for all three). Add the run to the existing entry only when
the cause is the same; otherwise start a new entry and name the one it resembles.

Each entry has:

- **Date** (UTC) and the **run** (`qmk_firmware` Actions run id, plus the attempt if it
  was a re-run, written `a2`).
- **Symptom**: the log line to grep for, copied exactly.
- **Class**: `rig hardware`, `rig network`, `rig setup` (a unit, grant or package missing on
  the Pi), `stale rig code`, `rig code` (a bug in the station), `rig test` (the ctnd test
  was wrong), or `unexplained`.
- **Cause and fix**: what it was, and the PR or action that fixed it.
- **Cost**: what it blocked (a release with no files, a PR held red, a red merge commit).

The triage steps are in the `diagnose-hil-failure` skill (in `qmk_firmware`). The entries
from 2026-10-15 to 2026-11-13 come from a sweep of all 222 `qmk-test.yml` runs in that
window: 25 graded HIL or apply jobs failed, 22 of them for a rig or unexplained cause.

## Open

### FW_UP_COMMIT gets no reply after a complete staging (4 times)
- **Runs:**
  - 2026-11-13: 43317964268 a2 (qmk#435 merge, firmware 1.13.3 image)
  - 2026-11-07: 42373381756 a1 (qmk#415 merge, 1.10.2)
  - 2026-11-06: 42305960229 a1 (qmk#414 merge, 1.10.1)
  - 2026-10-22: 39786033722 a1 (qmk#402 merge, 1.9.10). This log also has one
    `pipe: NACK … (resync 1)`.
- **Symptom:** `FAIL: FW_UP_COMMIT — no reply`, then `staging the signed image did not
  reach a clean COMMIT`. Each time it comes right after `all N bytes delivered … verifying
  CRC…` and `signature sent`, with the image staged over the bulk pipe. The signature
  replies also time out, so the master does not answer HID for more than 10 s.
- **Class:** unexplained. It happened in 4 of about 40 apply jobs. The console is stopped
  during the firmware-update section, so the log has nothing from the keyboard for that
  window. Three of the four predate the runtime combos (1.13.3), so they are not the cause.
- **Each time the next apply run on the same firmware passed:** 10-23, the 11-07 08:37
  dispatch, 11-09, and 43394412385 on 11-13.
- **Next step:** a debug probe that keeps the console open across COMMIT, to see what the
  master does in those 10 s.
- **Cost:** a red merge commit each time. 11-06 held the 1.10.2 release gate together with
  the PRC entry below. 11-13 left firmware 1.13.4 published with no files for about 45 min.

### 2026-11-10: master reset by its RUN pin during the language sweep
- **Run:** 42829271627 a1 (47d00071), Firmware apply round-trip.
- **Symptom:** `FAIL: swept 92/161 languages: esSV rebooted (the master crashed drawing this layout)`.
  The boot banner after it reads `reset: cause=run-pin reason=0x02 ram=kept record=no`.
- **Class:** unexplained, probably rig hardware. A RUN-pin reset is a hardware reset: no
  watchdog and no crash record (`crash_reset_cause_name()` in `base/crash_record.c`). The
  RUN line is driven by a 2N2222 switch (`RIG_HARDWARE.md`). Attempt a3 passed.
- **Cost:** with the segfault entry below, release 1.11.5 was refused on its first attempt.

### 2026-11-09: one corrupt frame in the bulk pipe soak
- **Run:** 42629563697 a1, HIL job, on an unrelated PR branch.
- **Symptom:** `keyboard: 1990656 B, 487 good, 1 bad, 3977 resync, crc 0x8fb18363 (expected 0x1840fb9d)`
  and `FAIL: count/CRC/bad-frame mismatch`.
- **Class:** unexplained; possibly the rig's USB link or cable. Attempt a2 passed.
- **Cost:** one re-run of a PR.

### 2026-11-13: USB-suspend check cannot suspend the keyboard
- **Run:** 43402659211 (extended dispatch on `PolyKybd` 779d7bad), the first run after
  `setup.sh --units-only` installed the grant. Until then every run had skipped the check
  (`rig not provisioned for it`) since it was added on 2026-10-13, so it never passed.
- **Symptom:** `[test] FAIL: USB suspend with Eden on core1 (flush + wake): 1-1.4 still reads runtime_status=active 10 s after control=auto`
- **Class:** rig code. `polykybd-usb-suspend` (`scripts/usb-suspend-ctl.sh`) can unbind
  only `usbhid`. The keyboard now also has a CDC interface (bulk pipe, `cdc_acm`) and,
  since firmware 1.13.0, USB MIDI (`snd-usb-audio`). A driver that holds an interface keeps
  the device awake. Which one does it here is not confirmed yet.
- **Interim:** the check logs the driver of every interface, and reports SKIP (not FAIL)
  when the device stays awake while a driver other than `usbhid` is bound, naming it. A
  device held awake with only `usbhid` bound still fails.
- **Fix:** extend the wrapper and the check to unbind every interface driver and bind it
  again on wake. The wrapper is installed by `setup.sh`, so it needs one more
  `sudo bash ./scripts/setup.sh --units-only` on the Pi.
- **Cost:** while it failed, the firmware-apply job skipped the apply on every merge, so
  the next firmware release would have been refused at the gate.

## Closed

### 2026-11-12 and 2026-11-13: combo commit "refused" after a firmware apply
- **Runs:** 43226924017 a1 (qmk#428 merge) and 43317964268 a1 (qmk#435 merge).
- **Symptom:** `[test] FAIL: reboot persistence: the keyboard refused combo [13, 14, 0, 0, 41] or its commit`
- **Class:** rig test.
- **Cause and fix:** after an apply the slave installs the master's image and comes back as
  a second master, so no commit can reach the other half. The reboot-persistence check
  required the other-half bit. ctnd#148 checks this half only in that state.
- **Cost:** the release gate refused firmware 1.13.4, published with no files.

### 2026-10-19 and 2026-11-10: test runner segfault (libhidapi race)
- **Runs:** 42829271627 a2 (apply job, in the boot loop) and 39251849392 a1 (HIL job,
  boot loop round 4, a PR branch).
- **Symptom:** `Segmentation fault  venv/bin/python -u -m station.test_runner …` and
  `##[error]Process completed with exit code 139.`
- **Class:** rig code. Two threads used libhidapi enumerate and open at once. Fixed by
  ctnd#141 (2026-11-10), which puts both behind one lock.
- **Cost:** with the RUN-pin entry above, release 1.11.5 was refused on its first attempt
  (release run 42831824568 a1). It was published at 14:12 and got its files at about 14:32.

### 2026-10-22 to 2026-11-06: PRC console test counted late warnings (6 runs)
- **Runs:** 39769699882 a1, 41067640263 a1, 41134054292 (qmk#407 merge), 41816035682,
  41824897499 a1, 42305960229 a2 (apply job).
- **Symptom:** `FAIL: a valid PRC report produced a warning: Warning: malformed PRC record at byte 0; rest of the report dropped.`
- **Class:** rig test. A warning from the previous, deliberately malformed report reached
  the console late and was counted against the valid one. Documented in ctnd#133; fixed by
  ctnd#136 (2026-11-07), which waits for a quiet console before counting. No repeat since.
- **Cost:** the qmk#407 merge commit was red. With the FW_UP_COMMIT entry it held the 1.10.2
  release gate until dispatch 42355874358 passed (2026-11-07 08:31).

### 2026-10-29: picotool refused to flash with both halves in BOOTSEL
- **Runs:** 40837074491 a1, a2 and a3 (qmk#363).
- **Symptoms:** a1: `Command '['sudo', 'picotool', 'reboot']' returned non-zero exit status 248.`
  a2 and a3: `fatal: picotool exited 248: ERROR: Command requires a single RP-series device to be targeted.`
- **Class:** rig code (`station/flash.py`). Fixed by ctnd#130 (target the half that just
  entered BOOTSEL) and ctnd#131 (refuse to flash without a readable BOOTSEL baseline).
  Attempt a4 passed.
- **Cost:** three attempts; the PR was held about 30 min.

### 2026-10-21: raw HID replies lost or late, one handle per call
- **Runs and symptoms:**
  - 39586675919 (6a1dcf43, a `PolyKybd` push): `decoded 78 codes but count byte says 99`
    and `[test] FAIL: enumerate language list (packed, v2)`.
  - 39572236621 a1: `[test] FAIL: boot loop (v22 cmd 43) [recovered 7 read timeout(s)]`,
    then `Protocol error` and `No such device`.
  - 39608481800: `FAIL: status 0xee != expected 0x2e ('.')` and `round 2: the reboot request was refused`.
- **Class:** rig code. `RawHID.send()` opened a new handle for every request. Fixed by
  ctnd#124 (2026-10-21), which keeps one handle open.
- **Cost:** a red commit on `PolyKybd`, and debug probe 39616286438 to investigate it.

### 2026-10-13 to 2026-10-14: USB-suspend check without its sudo grant
- **Symptom:** the USB-suspend check failed on every extended run; the firmware-apply job
  then skipped the apply (`skipping the firmware apply round-trip — the suite already has a failure`).
- **Class:** rig setup.
- **Cause and fix:** ctnd#114 added the check and its root wrapper, but `setup.sh` runs only
  by hand on the Pi, so the grant was never installed. ctnd#116 made the check SKIP while
  the grant is absent. The grant was installed on 2026-11-13 (see Open for what followed).
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

None of these happened from 2026-10-15 to 2026-11-13. Add a dated entry the next time one does.

- **Rig network:** the job ends at `Prepare all required actions` with
  `Failed to download action 'https://codeload.github.com/...'`. The runner was online;
  only `codeload.github.com` was unreachable.
- **Rig offline:** `HIL test (split72)` stays `queued` with no log. Nothing fails, so
  nobody is alerted. (On 2026-11-11 three runs stayed `queued` with no job created at all,
  for example 43098826223. That was on the GitHub side, not an offline rig.)
- **No enumeration:** `raw HID interfaces present: 0` and every test fails. One re-run; if
  it repeats, power-cycle or reseat a half.
- **Missing unit on an older rig:** a unit added after the rig was set up is never
  installed. `sudo bash ./scripts/setup.sh --units-only`.
