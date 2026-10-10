# CLAUDE.md: polykybd-ctnd (HIL test and deploy rig)

A Raspberry Pi 4 that flashes both PolyKybd halves (GPIO-driven RUN/BOOTSEL), reads the
firmware's HID console, sends raw HID commands, serves a touch UI on a 7" kiosk display,
and is the self-hosted GitHub Actions runner (`runs-on: [self-hosted, polykybd-ctnd]`)
for the HIL jobs in `qmk_firmware`.

**Rules for all PolyKybd repos** (review, branching, mirrored files, releases, web
session limits) are in `../polykybd-claude/CLAUDE.md`, with the shared skills. If that
repo is not attached, ask the user to attach `thpoll83/polykybd-claude`. This file holds
only rig rules. `.claude/rules/*.md` add warnings when you Read a matching file.

## Layout

| path | what |
|---|---|
| `station/hil_tests.py` | the graded suite: `TESTS`, a list of `{"name", "fn", gates…}` dicts |
| `station/test_runner.py` | `TestRunner` + CLI (`python -m station.test_runner`) |
| `station/flash.py`, `uf2.py`, `fw_update.py` | flashing over GPIO BOOTSEL; the HID firmware-apply path |
| `station/hid.py`, `console_log.py` | `RawHID` and `HIDConsole` |
| `station/probe.py` | ad-hoc probes (`--probe`) |
| `station/perf.py`, `perf_runner.py` | firmware profiler client (cmd 32) and runner |
| `station/set_handedness.py` | handedness over HID cmd 25 |
| `station/iso_lang_country.py` | mirrored index table (see polykybd-claude) |
| `station/ui/` | Flask + Flask-SocketIO (threading mode) touch UI |
| `config/config.yaml.example` | GPIO pins, uhubctl ports, VID/PID `0x2021:0x2007`, raw usage `0xFF61`/`0x62` (from qmk `split72/config.h`); copy to the gitignored `config/config.yaml` |
| `systemd/`, `scripts/` | units, `setup.sh`, `self-update.sh`, `register-runner.sh`, `runner-ctl.sh`, kiosk and backlight scripts |
| `tests/` | offline unit tests (no hardware) |
| `perf/baselines/`, `perf/fixtures/` | committed perf baselines |
| `.github/workflows/qmk-test.yml` | an old 94-line template. The live workflow is in `qmk_firmware` (1000+ lines); don't copy this one over it |

`firmware/` is created by `setup.sh`; the UI picks up UF2 files dropped there.
Python deps: `requirements.txt`. System packages (`uhubctl`, hidapi, `picotool`,
`xss-lock`, `x11-xserver-utils`, chromium) come from `setup.sh`.

## Hardware and roles

- **RUN and BOOTSEL are driven by a 2N2222 NPN low-side switch per pin** (BCM 17/18
  left, 22/23 right). GPIO HIGH saturates it and pulls the pin to ~0.1 V (asserted); LOW leaves the RP2040's ~50 kΩ pull-up (released, the idle state; `gpio.inverted: true`). A 2N7000
  or any Darlington does not work. [`docs/RIG_HARDWARE.md`](docs/RIG_HARDWARE.md).
- ⚠️ **The RPi4's own USB ports cannot switch power.** `uhubctl -a off` reports success
  and VBUS stays on. Per-port power needs an external uhubctl-compatible hub. Flashing
  does not depend on it.
- ⚠️ **Master and slave are fixed at compile time, per side.** Both halves see VBUS on
  the rig, so stock VBUS detection makes both master. `-e POLYKYBD_HIL=left` (alias
  `yes`) builds the master image, `=right` the slave. `rules.mk` turns them into
  `-DPOLYKYBD_HIL` / `-DPOLYKYBD_HIL_SLAVE`, and `is_keyboard_master_impl()` in
  `qmk_firmware/keyboards/polykybd/polykybd.c` returns true, or calls
  `usb_disconnect()` and returns false so the slave does not enumerate. Flash `*_hil_left.uf2` to `--left` and
  `*_hil_right.uf2` to `--right`. One image on both sides makes both master, which the
  `single master enumerates` test catches.
- **Handedness is a flash stamp in the firmware**, set over HID cmd 25
  (`station/set_handedness.py`). The firmware does not use EE_HANDS. The HIL build
  ignores handedness, so the rig needs no provisioning before a run.
- **The right half is flashed first**, because it talks over the split UART rather than
  USB HID.

## The HIL suite

Every test, what it asserts and why, and the traps that shaped them:
[`docs/HIL_SUITE_NOTES.md`](docs/HIL_SUITE_NOTES.md). Adding one: the `add-hil-test`
skill. Unreliable ideas go to [`docs/FUTURE_TESTS.md`](docs/FUTURE_TESTS.md).

- ⚠️ **Count the entries in `TESTS`** rather than trusting a number in prose.
- ⚠️ **`"passed": True` with an empty `results` list is not a pass.** Every caller of
  `flash_and_test` passes `tests=TESTS` explicitly.
- **Gate markers SKIP rather than fail** on firmware that predates a check
  (`min_protocol`, `min_fw`, `xfail`, `needs_console`, tier).
- **The slow checks are opt-in (`TIER_EXTENDED`)**: `--extended`, `HIL_EXTENDED=1`, the
  `hil-extended` PR label, `[hil-extended]` in a commit, a dispatch, or the UI toggle.
  ⚠️ A re-run replays the original event, so a label added later is invisible. Tier is
  about cost, never confidence.
- ⚠️ **`RawHID.send()` retries by re-writing the request.** That is safe only for
  idempotent commands. A call that observes a one-shot side effect (GET_ID's fresh-boot
  marker, REBOOT) is pinned to `attempts=1` at the call site. Don't special-case a
  command inside `send()`: most GET_ID callers depend on the retry.
- ⚠️ **The console is stopped for the whole firmware-update section.** Assert the
  console is live when you measure, not at run start.
- ⚠️ **A `HIDConsole` read is a report-sized fragment, not a line.** Buffer across reads,
  parse only `\n`-terminated lines, flush the tail when the reader stops.

## Probes and performance

- **A probe** is a one-off question for the hardware: a Python file in
  `qmk_firmware/keyboards/polykybd/tools/hil_probes/`, run with `tier: debug`.
  [`docs/PROBES.md`](docs/PROBES.md), skill `debug-firmware-on-rig`. A probe replaces the
  suite unless `--probe-with-suite` is passed. A brick self-recovers here, since BOOTSEL
  bypasses `fw_staging`.
- **Performance runs** use cmd 32, which NACKs on a normal build by design (the images
  must be `POLYKYBD_LOOP_PROFILE`). CI is opt-in (`hil-perf`) and report-only, and there
  is deliberately no automatic baseline update. [`docs/PERF_HARNESS.md`](docs/PERF_HARNESS.md),
  skill `measure-firmware-perf`.

## How changes reach the rig

The Pi is not reachable from a cloud session. Push to `main`; `polykybd-update.timer`
fetches every ~5 min and fast-forwards when the rig is idle (or tap UPDATE in the UI).
The full workflow and its worked examples: [`docs/DEV_WORKFLOW.md`](docs/DEV_WORKFLOW.md);
the updater: [`docs/SELF_UPDATE.md`](docs/SELF_UPDATE.md).

- ⚠️ **HIL CI runs the installed `/opt/polykybd-ctnd`, force-synced to ctnd `main`**
  (`git checkout -q -f -B main origin/main`). So the rig is never a place to park a
  branch, and a test from an unmerged ctnd PR does not run.
- ⚠️ **A paired firmware + rig change has a merge order: land the ctnd PR first**, then
  re-run HIL on the firmware PR. What counts is ctnd `main` at the sync step. Merging the
  firmware first only defers coverage to the merge-commit run. Either way, grep the job
  log for `[test] PASS: <name>`.
- ⚠️ **When HIL flakes, check the rig is current first.** The settle line says
  `need=15` on current code, `need=3` on stale.
- ⚠️ **A HIL job that dies at `Prepare all required actions` never ran.** That is the
  rig's network (e.g. `codeload.github.com` blocked), not code. The runner stays online
  throughout. Triage: the `diagnose-hil-failure` skill.
- ⚠️ **A rig provisioned before a unit landed never gets it.**
  `sudo bash ./scripts/setup.sh --units-only` (with `bash`: older checkouts lack the
  execute bit).
- **The runner's first registration needs SSH**; after that the UI's ⚕ Diagnose,
  ⟳ Restart and ↻ Re-register recover it. [`docs/RUNNER.md`](docs/RUNNER.md).

## Security

[`docs/SECURITY_AUDIT.md`](docs/SECURITY_AUDIT.md) is the tracker for all repos
(`FW-*`, `HOST-*`, `HIL-*`). Update it in the same PR as any fix, and record findings
that need no change too.

- ⚠️ **The control UI has no authentication.** Every SocketIO handler (flash, GPIO, USB
  power, runner re-register, self-update) is protected only by the loopback bind, which
  `ui.allow_lan: true` disables.
- ⚠️ **The rig is a self-hosted runner for a public repo**, so the fork-PR approval
  settings on `qmk_firmware` are security, not CI hygiene (HIL-2, open).

## Open work

- [ ] Verify `USB_HUB_LOCATION`, `LEFT_USB_PORT`, `RIGHT_USB_PORT` with `uhubctl` and
  update `config/config.yaml`.
- [ ] Register or re-register the runner (`scripts/register-runner.sh`, `docs/RUNNER.md`).
- [ ] **Provisioning-drift self-check.** Nothing compares the installed
  `/etc/systemd/system/polykybd-*` units and `/etc/sudoers.d/polykybd-*` with the repo's
  templates, so a unit added after a rig was built stays absent. Compare at startup and
  show drift as a header badge and a ⚕ Diagnose line; the fix is
  `setup.sh --units-only`. A compensating sync (like CI's force-sync) hides the
  difference between slow and absent.
- [ ] GPIO-driven key matrix simulation, so tests can press keys.
- [ ] Test `scripts/setup.sh` on a fresh RPi4.

For UI work the Flask dev server runs directly:
`cd /opt/polykybd-ctnd && PYTHONPATH=. venv/bin/python -m station.ui.app`.
