# CLAUDE.md — PolyKybd CTND

## Code review conventions (all PolyKybd repos)

- **Docstring coverage: ignore CodeRabbit's "Docstring Coverage … threshold 80%" pre-merge check.** That 80% target is a CodeRabbit default, **not** a project policy — the check is non-blocking and we deliberately do not chase it. Do **not** add docstrings to existing functions just to satisfy it (out-of-scope churn). Document new code where a docstring genuinely helps a reader, and no more.

## Getting repo access in a new session

This repo (`thpoll83/polykybd-ctnd`) must be added to the session's authorised repository list before you can push.
The other two repos in this project are already configured:
- `thpoll83/polykybdhost`
- `thpoll83/qmk_firmware`

Ask the user to add `thpoll83/polykybd-ctnd` to the session when starting, or verify access with:

```bash
git -C /home/user/polykybd-ctnd push --dry-run 2>&1
```

If it returns `Proxy error: repository not authorized`, the repo is not yet in the session's allowed list.

The local git proxy URL pattern is:
```
http://local_proxy@127.0.0.1:36951/git/thpoll83/<repo-name>
```

---

## Project overview

**polykybd-ctnd** is a Raspberry Pi 4 hardware-in-the-loop (HIL) test and deploy station for the PolyKybd split mechanical keyboard. It:

1. Flashes QMK firmware to both keyboard halves over USB — fully automated via GPIO-controlled BOOTSEL + uhubctl per-port USB power switching (no physical button access needed)
2. Reads QMK's HID console debug output
3. Sends Raw HID commands (same protocol as PolyKybdHost)
4. Serves a touch-friendly web UI on a 52Pi 7" 1024×600 capacitive display via Chromium kiosk
5. Acts as a GitHub Actions self-hosted runner (`runs-on: [self-hosted, polykybd-ctnd]`) so CI jobs in `qmk_firmware` can build firmware in the cloud then run HIL tests on real hardware

## Repository layout

```
station/config.py       GPIO pin numbers, uhubctl hub/port config, QMK VID/PID
station/flash.py        FlashController class — power off port, assert BOOTSEL, copy UF2
station/hid.py          HIDConsole (reads QMK debug log) + RawHID (sends commands)
station/test_runner.py  TestRunner class + __main__ CLI entry point
station/perf.py         Firmware profiler client (HID cmd 32) + perf workloads
station/perf_runner.py  PerfRunner + __main__ CLI — flash, measure, compare, report
perf/baselines/         Committed perf baselines, one JSON per board
tests/perf_test.py      Offline tests for the perf harness (no hardware needed)
station/ui/app.py       Flask + Flask-SocketIO server; emits log/status events via WebSocket
station/ui/templates/   index.html — 1024×600 dark touch UI
station/ui/static/      style.css, app.js
systemd/                Service units: Flask daemon, Chromium kiosk, self-update timer+oneshot
scripts/setup.sh        One-shot RPi4 setup (apt, udev, venv, systemd)
scripts/self-update.sh  Pull the tracked branch + restart the station (idle-gated; timer/UI driven)
scripts/kiosk.sh        Manual kiosk launch fallback
.github/workflows/      Example CI workflow to copy into qmk_firmware repo
firmware/               Drop UF2 files here; the UI picks them up automatically
```

## Key design decisions

- **uhubctl per-port power switching** is exposed as a manual convenience in the UI (and for the BOOTSEL data-disconnect on some boards), *not* as a way to choose the split master. ⚠️ **The RPi4's built-in USB-A ports do NOT support per-port power switching** — the VL805 host controller ignores the command, so `uhubctl ... -a off` reports success but VBUS stays energized. An earlier version of this doc claimed native per-port power control; that was wrong and is why "turning off the port" never dropped a half to slave. Real per-port power switching needs an external `uhubctl`-compatible powered hub. The flash sequence itself only needs the RUN/BOOTSEL GPIO pins and does not depend on cutting power.
- **GPIO pins** (BCM 17/18 for left, 22/23 for right) drive the RUN and BOOTSEL pads on the PCB via a 2N2222 NPN BJT low-side switch circuit (see below). Pads are exposed on the assembled boards since no key switches are fitted.
- **Master/slave selection on the rig is forced in firmware at compile time, per side.** Stock PolyKybd firmware picks the master from `USB_VBUS_PIN` (GP24). On the rig both halves are cabled to the Pi, so both read VBUS high and both detect as master — and the Pi can't drop VBUS (see uhubctl note above). The fix is **two per-side HIL images**, each overriding `is_keyboard_master_impl()` in `keyboards/polykybd/polykybd.c` (shared by split72/split42): `-e POLYKYBD_HIL=left` (or the `=yes` alias) builds the **master** image (`return true`), and `-e POLYKYBD_HIL=right` builds the **slave** image (`usb_disconnect(); return false`, so it doesn't enumerate as a second keyboard). The role is **fixed at build time — it is NOT read from EE_HANDS** (the rig provisions no handedness marker; a fresh EEPROM reads back "not left", which would make *both* halves slaves). Normal user firmware never defines `POLYKYBD_HIL` and keeps VBUS detection. ⚠️ `test_runner.py` must be handed the **`*_hil_left.uf2` for `--left` and the `*_hil_right.uf2` for `--right`** — flashing a single master image to both sides makes *both* enumerate as master (the original `qmk-test.yml` bug that `single master enumerates` catches).
- **EE_HANDS** stores the side in EEPROM for *normal* firmware (set once via QMK Toolbox or a keymap combo; survives reflashes). The HIL build does **not** use it — `is_keyboard_master_impl()` ignores EE_HANDS and the role comes from the per-side compile flag above — so the rig needs no handedness provisioning before a HIL run.
- **Flask-SocketIO** (threading mode) is used for the web UI so log lines stream to the browser in real time without polling.
- **Idle screen blanking + backlight off** uses a two-layer stack. (1) X11 DPMS (`xset s 300; xset +dpms; xset dpms 0 0 300`) blanks the display after 5 min idle. (2) `xss-lock` watches the screensaver idle event and runs `scripts/backlight-locker.sh`, which calls `vcgencmd display_power 0` to physically cut the HDMI output at the VideoCore firmware level — this is what actually turns off the panel backlight (DPMS alone does not on this display). `xss-lock` sends SIGTERM to the locker on the first touch/keypress; the locker's EXIT trap calls `vcgencmd display_power 1` to restore HDMI. The USB touch controller stays powered so touches reach X11 even while HDMI is off, triggering the wake. `xss-lock` runs in the same `ExecStart` bash command as Chromium (backgrounded before `exec chromium-browser`) so cgroup cleanup kills it when the service stops. `vcgencmd` requires the user in the `video` group (`setup.sh` adds it). `xset` ships in `x11-xserver-utils`, `xss-lock` in the `xss-lock` package (both installed by `setup.sh`). X11-only; a Wayland move would need `swayidle` + `wlr-randr`.
- **Right half is flashed first** in `test_runner.py` because it communicates via the PIO UART split cable, not USB HID, so a brief USB reboot on the right half doesn't disrupt the test HID path.

## Hardware facts (verified)

- **VID/PID**: `0x2021:0x2007` (PolyTasten PolyKybd Split72) — in `config/config.yaml`
- **Raw HID usage**: `RAW_USAGE_PAGE 0xFF61`, `RAW_USAGE_ID 0x62` — from `split72/config.h`
- **RUN and BOOTSEL circuits**: identical 2N2222 NPN BJT low-side switch (`docs/RIG_HARDWARE.md`). `gpio.inverted: true` in config (the default).
- **GPIO logic**: GPIO HIGH → BJT saturated → pin pulled to ~0.1 V (asserted). GPIO LOW → BJT off → pin held HIGH by RP2040 internal pull-up (~50 kΩ) → running/released. Idle state is GPIO LOW.
- **EE_HANDS**: firmware stores the handedness side in EEPROM (`#define EE_HANDS` in `split72/config.h`) for *normal* (non-HIL) use; survives all UF2 reflashes. It sets left/right only — **not** master/slave — and the HIL build ignores it entirely (see below), so it does not need provisioning for a HIL run.
- **HIL master forcing**: the rig flashes **two per-side images** — `-e POLYKYBD_HIL=left` (master) to the left half, `-e POLYKYBD_HIL=right` (slave, `usb_disconnect()`) to the right half. This is required — with a single image both halves detect as master because both see USB VBUS on GP24 and the RPi4 can't drop it. The override lives in `keyboards/polykybd/polykybd.c` (`is_keyboard_master_impl`), gated by `-DPOLYKYBD_HIL` / `-DPOLYKYBD_HIL_SLAVE` from `keyboards/polykybd/rules.mk` (`POLYKYBD_HIL=left|right|yes`). `=yes` is an alias for `left`. **Flashing one `=yes` image to both sides (the original `qmk-test.yml`) makes both master — that is the bug the HIL suite now catches.**
- **CI workflow**: `.github/workflows/qmk-test.yml` is live in `thpoll83/qmk_firmware` on the `PolyKybd` branch.

### Reset / BOOTSEL driver circuit

Both pins use an identical **2N2222 NPN low-side switch** (`gpio.inverted: true`, the
default): GPIO HIGH saturates it and pulls the pin to ~0.1 V (**asserted**); GPIO LOW
leaves the RP2040's ~50 k internal pull-up (**released**). Idle state is GPIO LOW.
The schematic, the measured voltages and the two parts that do **not** work (a 2N7000
has too much R_DS(on) at 3.3 V gate drive; any Darlington sits above the RP2040 reset
threshold) are in [`docs/RIG_HARDWARE.md`](docs/RIG_HARDWARE.md).

## What still needs doing

- [ ] Verify `USB_HUB_LOCATION`, `LEFT_USB_PORT`, `RIGHT_USB_PORT` by running `uhubctl` on the RPi4 and update `config/config.yaml`
- [ ] Set EE_HANDS EEPROM marker on each half once (QMK Toolbox → "Set EEPROM Hand", or a keymap combo) before the first HIL run
- [ ] Register (or re-register) the GitHub Actions self-hosted runner — see `scripts/register-runner.sh` and `docs/RUNNER.md`
- [x] **The graded suite is written, and its whole history — every test, what it
  asserts and why, and the traps that shaped it — is
  [`docs/HIL_SUITE_NOTES.md`](docs/HIL_SUITE_NOTES.md).** Read it before adding,
  tightening or loosening a rig test. Five rules bind code outside that file:
  - ⚠️ **Count the entries in `TESTS`** rather than trusting a number written in
    prose; the count has been stale here before.
  - ⚠️ **A `"passed": True` carrying an empty `results` list is NOT a pass.** Every
    caller of `flash_and_test` must pass `tests=TESTS` explicitly — the touch UI's
    Run Tests button flashed, blanked the displays and reported success while
    asserting nothing, for months.
  - **The slow checks are OPT-IN (`TIER_EXTENDED`)**: `--extended` / `HIL_EXTENDED=1`,
    the **`hil-extended`** PR label, `[hil-extended]` in a pushed commit, a manual
    `workflow_dispatch`, or the UI toggle. ⚠️ A **re-run replays the original event
    payload**, so a label added afterwards is invisible and the run silently repeats
    the default tier — label the PR, don't re-run an older run. ⚠️ **Tier is about
    COST, never confidence**: anything unreliable belongs in `docs/FUTURE_TESTS.md`,
    not in a tier nobody runs.
  - ⚠️ **`station/iso_lang_country.py` is the frozen index table, mirrored
    byte-identically** in `qmk_firmware/keyboards/polykybd/lang/` and
    `PolyKybdHost/polyhost/services/` — keep all three in sync (`cmp`) or the rig
    decodes wrong languages.
  - ⚠️ **The console is STOPPED for the whole firmware-update section**, so anything
    added there reads a `TAP` nothing is feeding: present, passing, asserting
    nothing. Assert the console is live at the moment you measure, not at run start.
- [ ] **Provisioning-drift self-check.** Nothing compares the *installed*
  `/etc/systemd/system/polykybd-*.{service,timer}` + `/etc/sudoers.d/polykybd-*`
  against the repo's templates, so any unit or grant added after a rig was built
  stays silently absent until someone presses the button that needs it (2026-08-03:
  `polykybd-update.service`/`.timer`, missing since that rig was provisioned). Compare
  them at startup and surface drift as a header badge + a line in the ⚕ Diagnose
  report; the fix is then `sudo bash ./scripts/setup.sh --units-only`. The diagnose
  plumbing already exists — this is the durable fix for the whole class, of which
  the UI's in-process update fallback only softens one instance.
- [ ] Add GPIO-driven key matrix simulation so tests can simulate key presses
- [ ] Test `scripts/setup.sh` on a fresh RPi4 and fix any issues

## Debug loop: running an ad-hoc probe on the rig (`--probe`)

The graded suite answers fixed questions. A **probe** answers a one-off one — *"flash
this build, send these commands, show me what the firmware printed"* — so a firmware
bug can be chased without a human flashing a `.bin` and pasting a console log back. It
is a Python file in the **firmware** repo under
`keyboards/polykybd/tools/hil_probes/`, so the probe and the firmware it probes are
one commit on one branch. The file shape, the CLI and the containment rules are in
[`docs/PROBES.md`](docs/PROBES.md); the `debug-firmware-on-rig` skill drives it
(that skill lives in `qmk_firmware`, so it is only reachable when that repo is
attached to the session too).

- **A probe REPLACES the suite** unless `--probe-with-suite` is passed.
- ⚠️ **The console cannot see the flash window** — QMK drops output nobody drains, and
  during a flash nothing does. A gap in the `[qmk]` timestamps spanning a flash is
  expected, not a symptom.
- ✅ **A brick is self-recovering here**, because the rig asserts BOOTSEL over GPIO and
  BOOTSEL/UF2 bypasses `fw_staging` entirely. That is what makes the rig the right —
  and the only — place to exercise the firmware-apply path.

## Writing test cases

A test is a plain dict — `{"name": ..., "fn": ...}` — whose `fn(raw: RawHID, log)`
returns a bool, plus optional gate markers (`min_protocol`, `min_fw`, `xfail`,
`needs_console`, tier) that **SKIP** rather than fail on firmware predating the check,
and un-skip themselves once a satisfying image is flashed. The shapes, the gate table,
the three `RawHID` send forms and the per-test reporting are in
[`docs/HIL_SUITE_NOTES.md`](docs/HIL_SUITE_NOTES.md); the `add-hil-test` skill drives
the whole job.

⚠️ **`RawHID.send()` RETRIES by re-writing the request, and that is safe only because
the commands are idempotent — `GET_ID` is the one exception.** It consumes the
firmware's one-shot fresh-boot marker, so a retry returns a perfectly correct reply for
a marker the firmware has already cleared: the test sees **wrong data** (graded FAIL)
instead of a timeout (graded WARN), silently turning a rig hiccup into a red HIL check.
That one call is pinned to `attempts=1`.
⚠️ **Do NOT "centralise" this by special-casing GET_ID inside `send()`** — six of its
seven call sites depend on the retry, and they are exactly the probes that run in the
master's post-overlay deaf window. The property is *"this read observes a one-shot side
effect"*, which belongs to the call site, not to the command.

## Performance measurement (`station/perf.py`, `station/perf_runner.py`)

The rig measures firmware performance automatically, replacing "deploy a build by hand,
poke the keyboard, paste the `LoopProf:` block": bounded profiler windows over **HID
cmd 32** (RESET -> workload -> READ), an idle baseline, overlay bursts in both
flavours, HID round-trip latency, and boot-to-first-stable-HID. Workloads, CI wiring,
the baseline procedure and the offline wire-format contract test are in
[`docs/PERF_HARNESS.md`](docs/PERF_HARNESS.md); the `measure-firmware-perf` skill
drives a run (it lives in `qmk_firmware` — attach that repo to reach it).

- ⚠️ **cmd 32 NACKs on a normal build, by design** — the whole `case 32` is inside
  `#ifdef POLYKYBD_LOOP_PROFILE`, and that NACK is the capability signal rather than a
  fault. A run reporting *"not a POLYKYBD_LOOP_PROFILE build"* means the wrong images
  were flashed.
- **CI is opt-in and report-only** (`hil-perf` label, `[hil-perf]` in a commit, or a
  dispatch). It never fails on a regression — wall-clock numbers on shared hardware
  make a flaky red check people learn to ignore; only a *measurement* failure exits
  non-zero.
- ⚠️ **There is deliberately no automatic baseline update.** A self-rewriting baseline
  ratchets a slow regression in silently.
- ⚠️ **A `HIDConsole` read is a report-sized FRAGMENT, not a line.** Anything that
  filters or parses console output must buffer across reads and classify only
  `\n`-terminated lines, then flush the trailing fragment when the reader stops —
  otherwise every continuation is dropped and the last line goes missing.

## Runner troubleshooting

When a CI job hangs at "Waiting for a runner to pick up this job", the touch UI can
diagnose and recover the self-hosted runner without SSH: the `CI` / `RUNNER` header
badges, **⚕ Diagnose** (unit, process, GitHub-side registration, labels, queued jobs,
connectivity, verdict), **⟳ Restart** and **↻ Re-register**, all over
`scripts/register-runner.sh`. Its modes, the PAT sources and the scoped sudoers grant
are in [`docs/RUNNER.md`](docs/RUNNER.md).

⚠️ **The FIRST registration must be done over SSH** — it installs the systemd unit.
Only after that can the touchscreen recover it.

## Development workflow

The RPi4 is not directly accessible from Claude Code on the web. Development cycle:

1. Edit files here (cloud session)
2. Commit + push to `main`
3. The rig **deploys itself** — `polykybd-update.timer` fetches `main` every ~5 min
   and, when it gains commits *and the rig is idle*, fast-forwards, reinstalls deps
   if `requirements.txt` changed, and restarts the station. No SSH needed. To apply
   immediately, tap the **UPDATE** badge in the touch UI (or `sudo systemctl start
   polykybd-update.service`). The old manual path still works:
   `git -C /opt/polykybd-ctnd pull && sudo systemctl restart polykybd-ctnd`.

> **⚠️ HIL CI runs the *installed* `/opt/polykybd-ctnd`, NOT a fresh checkout** —
> `qmk-test.yml`'s "Locate station directory" step just finds the install and runs
> `venv/bin/python -m station.test_runner`. So the suite only ever runs the station
> code the **self-update timer has already fast-forwarded** onto the rig. When that
> update lags (the rig was busy/offline, or the timer deferred), HIL runs **stale**
> station code and already-merged rig fixes silently don't apply — you'll chase a
> failure that was fixed days ago (seen 2026-07: the rig ran the old `need=3` settle
> for ~5 days while `main` had `need=15`, so the boot-burst flake kept "recurring").
> **When a HIL check flakes, verify the rig is current *first*, before blaming the
> firmware or a test.** The cheapest tell is the settle log line: `master settled —
> N consecutive GET_LANG replies … after N probe(s)` — `need=3` means stale
> (pre-`df6401d`), `need=15` means current. If stale, tap **UPDATE** (or wait a
> timer tick) and re-run before diagnosing anything else. The durable fix is to make
> CI pull `main` before the run (qmk `qmk-test.yml` "Sync station to current ctnd
> main" step) so a lagging timer can't leave HIL on stale code.
>
> ⚠️ **That sync is a FORCE checkout — `git checkout -q -f -B main origin/main` —
> so the rig's checkout is NOT a place to park a branch.** Testing an unmerged
> ctnd branch on the rig (e.g. to get a `setup.sh` flag that isn't on `main` yet)
> works only until the next HIL run, which discards it with no warning and no log
> line anyone reads. The self-update side dislikes it too: `self-update.sh` tracks
> `main`, so while a branch is checked out it reads 0-behind and does nothing, and
> once `main` advances the two diverge and its `--ff-only` merge fails (exit 75)
> on every 5-minute tick. **Check the branch out, do the one thing you need, then
> `git checkout main` in the same sitting.** Anything installed *outside* the
> checkout (systemd units, sudoers) survives the switch back — that is what makes
> the round trip safe.
>
> ⚠️ **Corollary that bites the OTHER repo: a green HIL board on a firmware PR does
> NOT mean that PR's own new rig test ran.** Because the rig runs `main`, a test added
> in an *unmerged* ctnd PR does not exist on the rig — the suite happily goes green
> having never executed it, and the firmware PR's checklist claims coverage nothing
> produced. Seen 2026-08-22: qmk#227 (keycap legend size) listed
> `test_glyph_size_round_trip` as tested while the test lived only in the still-open
> ctnd#71; the HIL log names every test it ran, and that one appears nowhere in it.
> **So a paired firmware+rig change has a merge ORDER: land the ctnd PR first, then
> re-run HIL on the firmware PR** — otherwise the firmware merges on a board that
> never checked the thing the rig PR was written to check. Verify rather than assume:
> grep the HIL job log for the test's own name (each prints `[test] PASS: <name>`),
> not just the job's conclusion.
>
> ✅ **But merging does NOT forfeit that coverage — it defers it by one run.**
> `qmk-test.yml` also triggers on `push: [PolyKybd]`, so the **merge commit itself**
> starts a HIL run that executes the new test. (The follow-on auto-bump `chore:`
> commit does not — it carries `[skip ci]`.) That is what decides the case where the
> rig is unavailable and the choice is "hold the PR open or merge anyway": both reach
> the same coverage at the same moment, so a rig outage is not a reason to leave a
> reviewed, hardware-confirmed PR dangling. Seen 2026-08-27 on qmk#233 — the merge
> started run #851, the first run able to execute `layer names (v14)`.
>
> ⚠️ **A red HIL that never RAN is a different thing again, and the settle line
> cannot tell you.** The job can die in workflow setup — fetching a GitHub Action —
> before checkout, before the station sync, before a single `[test]` line. On
> 2026-08-27 the rig lost outbound access to `codeload.github.com` and failed that
> way **three times** (twice on a PR, once on the `PolyKybd` merge push), leaving the
> default branch red on the rig's network rather than on any code. The tell is a log
> ending at `Prepare all required actions`; full triage in the `diagnose-hil-failure`
> skill, §1.5. Note the runner stays *online* throughout — it picks jobs up and
> streams logs — so "the rig is up" is not evidence its network is healthy.
>
> ⚠️ **The order is measured against the rig's SYNC STEP, not the run's start — so
> merging the two a minute apart in the WRONG order can still be fine.** On
> 2026-08-28 qmk#234 merged at 12:06:47 and ctnd#74 at 12:07:47, i.e. the firmware
> landed first, which the rule above says forfeits the new test. It did not: the
> merge-commit run started at 12:06:48 but `qmk-test.yml` force-syncs the station to
> ctnd `main` inside the **hil-test** job, which waits on the cloud build — several
> minutes later, by which time the ctnd merge had landed. So the window that matters
> is "ctnd `main` is current **when the sync step runs**", and a build long enough to
> cover a same-minute merge closes it for you. Do not rely on that: it is luck, the
> build time is not a contract, and the check is unchanged — **grep the HIL log for
> the test's own name** and re-run the job if it is absent, since a re-run syncs
> again and will then pick it up.

### Self-update mechanism

`scripts/self-update.sh` is the single actuator, run by both the 5-minute
`polykybd-update.timer` and the UI's UPDATE badge: fetch the tracked branch, **defer
while the rig is busy** (never abort a flash or HIL run), else fast-forward
(`--ff-only`), pip-install only if `requirements.txt` changed, and restart the station.
The units, the badge states and the scoped sudoers grant are in
[`docs/SELF_UPDATE.md`](docs/SELF_UPDATE.md).

⚠️ **A rig provisioned before a unit landed never gets it — `setup.sh` is the only
installer and nothing re-runs it.** Recovery is `sudo bash ./scripts/setup.sh
--units-only` (`bash …`, not `./…`: an older checkout lacks the execute bit, and with
no `x` bit set *even root* gets `Permission denied`).

⚠️ **The CI force-sync hid that for the rig's whole life.** Generalise before adding
the next such workaround: **a compensating sync hides the difference between "slow" and
"absent"**, and nothing here checks that the installed units and grants still match the
repo.

For rapid UI iteration the Flask dev server can be run directly:
```bash
cd /opt/polykybd-ctnd
PYTHONPATH=. venv/bin/python -m station.ui.app
```

## Dependencies

| Package | Purpose |
|---|---|
| `flask` | Web server |
| `flask-socketio` | WebSocket event layer |
| `simple-websocket` | WebSocket transport for flask-socketio threading mode |
| `RPi.GPIO` | GPIO control for RUN/BOOTSEL pins |
| `hid` | HID device access (hidapi Python bindings) |

System packages required: `uhubctl`, `libhidapi-hidraw0`, `libhidapi-libusb0`

## Security

**`docs/SECURITY_AUDIT.md` is the cross-repo findings tracker** (`FW-*` / `HOST-*` /
`HIL-*`) — status, verification notes, and the two items still open. Read it before
touching the UI's bind/CORS config, `config.yaml` handling, the self-update path, or the
`runs-on: [self-hosted, …]` workflow, and update it in the same PR. Two standing facts it
records that are easy to trip over:

- **The control UI has no authentication of any kind** — every SocketIO handler (flash,
  GPIO, USB power, runner re-register, self-update) is reachable by anyone who can open
  the page. The *only* thing protecting it is the loopback bind, which
  `ui.allow_lan: true` disables. Don't add a handler assuming some auth layer exists.
- **The rig is a self-hosted runner for a public repo**, so fork-PR approval settings on
  `qmk_firmware` are load-bearing security, not CI hygiene (HIL-2, still open).
- ✅ **FW-9 is FIXED (qmk #243): the `.plyx` DOOM engine pack is signed too.** It is
  *executable code* on the same HID transport, and `doom_pack_load.c` used to authenticate
  it with a CRC32 only before branching into it — arbitrary code execution for anyone who
  could talk raw HID (crafted pack + IDDQD idle style over cmd 28). It now carries an
  Ed25519 signature verified at load time under `FW_REQUIRE_SIGNATURE`. ⚠️ The `.whx` /
  `.plyf` resources ride the same transport and are still authenticity-unchecked — but they
  are *data*, so the exposure is parser bugs, not direct code execution (SECURITY_AUDIT.md
  FW-9 tail).

## Related repos

| Repo | Role |
|---|---|
| `thpoll83/qmk_firmware` | Keyboard firmware — source of UF2 files, target of CI workflow |
| `thpoll83/PolyKybdHost` | Host application — shares the Raw HID protocol used in `station/hid.py` |
