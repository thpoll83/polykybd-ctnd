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
- **RUN and BOOTSEL circuits**: identical 2N2222 NPN BJT low-side switch (see schematic below). `gpio.inverted: true` in config (the default).
- **GPIO logic**: GPIO HIGH → BJT saturated → pin pulled to ~0.1 V (asserted). GPIO LOW → BJT off → pin held HIGH by RP2040 internal pull-up (~50 kΩ) → running/released. Idle state is GPIO LOW.
- **EE_HANDS**: firmware stores the handedness side in EEPROM (`#define EE_HANDS` in `split72/config.h`) for *normal* (non-HIL) use; survives all UF2 reflashes. It sets left/right only — **not** master/slave — and the HIL build ignores it entirely (see below), so it does not need provisioning for a HIL run.
- **HIL master forcing**: the rig flashes **two per-side images** — `-e POLYKYBD_HIL=left` (master) to the left half, `-e POLYKYBD_HIL=right` (slave, `usb_disconnect()`) to the right half. This is required — with a single image both halves detect as master because both see USB VBUS on GP24 and the RPi4 can't drop it. The override lives in `keyboards/polykybd/polykybd.c` (`is_keyboard_master_impl`), gated by `-DPOLYKYBD_HIL` / `-DPOLYKYBD_HIL_SLAVE` from `keyboards/polykybd/rules.mk` (`POLYKYBD_HIL=left|right|yes`). `=yes` is an alias for `left`. **Flashing one `=yes` image to both sides (the original `qmk-test.yml`) makes both master — that is the bug the HIL suite now catches.**
- **CI workflow**: `.github/workflows/qmk-test.yml` is live in `thpoll83/qmk_firmware` on the `PolyKybd` branch.

### Reset / BOOTSEL driver circuit (per pin)

```
              RP2040
                │
            RUN ●──────────────┐
            (or               ─┴─ C1 100 nF (optional, noise immunity)
           BOOTSEL)            ─┬─
                │               │
           Collector            │
                │               │
         ┌──────┴──────┐        │
         │   2N2222    │        │
         │    (NPN)    │        │
         └──Base──Emit─┘        │
               │      │         │
  RPi4 GPIO ──[R1 1kΩ]┤         │
               │      │         │
            [R2 82kΩ] │         │
               │      │         │
              GND─────●─────────●─── GND (shared)
```

| RPi4 GPIO | Q1 state | Pin voltage | RP2040 state |
|-----------|----------|-------------|--------------|
| HIGH (3.3 V) | Saturated | ~0.1–0.2 V | **Asserted** (reset / BOOTSEL held) |
| LOW (0 V) | Off | ~3.3 V (internal pull-up) | **Released** (running / button up) |

**Design notes:**
- A 2N7000 MOSFET was tried first but R_DS(on) is too high at 3.3 V gate drive to pull RUN below the RP2040 reset threshold. The saturated 2N2222 drops only V_CE(sat) ≈ 0.1–0.2 V.
- Do **not** use a Darlington (e.g. TIP120): V_CE(sat) ≈ 0.9–1.2 V, above the RP2040 reset threshold (~0.8 V).
- 2N2222 pinout varies by package and manufacturer (TO-18 vs TO-92; some clones reverse C and E). Verify with a multimeter in diode mode before powering up. Swapped C/E gives inverse-active mode with V_CE ≈ 0.6–1.2 V — insufficient to assert reset.
- R2 (82 kΩ base pull-down) keeps Q1 off during RPi4 boot when the GPIO may be high-impedance.

## What still needs doing

- [ ] Verify `USB_HUB_LOCATION`, `LEFT_USB_PORT`, `RIGHT_USB_PORT` by running `uhubctl` on the RPi4 and update `config/config.yaml`
- [ ] Set EE_HANDS EEPROM marker on each half once (QMK Toolbox → "Set EEPROM Hand", or a keymap combo) before the first HIL run
- [ ] Register (or re-register) the GitHub Actions self-hosted runner — see `scripts/register-runner.sh` and "Runner troubleshooting" below
- [x] Write concrete test cases — `station/hil_tests.py` now covers every Raw HID command testable without side effects on the unattended rig (16 tests: identity/fresh-boot, language get/list/list-packed/round-trip, default layer, ACK/NACK error+bounds paths, overlay-flags round-trip, plain + core1-compressed overlay liveness, GET_ID stress), wired into the `test_runner.py` CLI. Remaining infra-dependent / camera-needing / deliberately-excluded items are in `docs/FUTURE_TESTS.md`.
  - The **packed language-list** test (cmd 27, protocol v2+) decodes the 2-byte ISO index pairs via `station/iso_lang_country.py` and validates the list **standalone** — staple locales present, every code well-formed `llCC`, decoded count matches the count byte, current language present. (It no longer cross-checks against the ASCII `GET_LANG_LIST`: that command is **retired** — a separate test asserts cmd 8 now NACKs.) ⚠️ `station/iso_lang_country.py` is the **frozen index table**, byte-identical to the copies in `qmk_firmware` (`keyboards/polykybd/lang/`) and `PolyKybdHost` (`polyhost/services/`); keep all three in sync (`cmp`) or the rig decodes wrong languages. cmd 27 is the only language-list command on v2+ firmware; on a pre-v2 board it NACKs — but the packed/legacy/round-trip tests now carry `"min_protocol": 2`, so a pre-v2 board **skips** them rather than failing (see "Tolerating not-yet-deployed changes" below).
  - The **`GET_ID stress`** test (`test_get_id_stress`) deliberately **tolerates isolated no-answers** and fails only on a *freeze signature* — decided by the pure `classify_get_id_stress(oks, n)`: FAIL if total misses `> max(2, n//10)` or there is a run of `>= STRESS_FREEZE_RUN` (3) consecutive misses; otherwise PASS. ⚠️ **Do not re-tighten it to fail on the first miss** — it runs right after the overlay-upload tests, which leave the master in its transient post-overlay **deaf window** (EEPROM write + full keycap refresh; `send_repeated` already retried the host-side USB hiccups internally). The qmk **split-sync re-fire fix** (#80, `sync_succeeded()`) can *lengthen* that window on the rig, where master→slave sync is flaky, so an occasional GET_ID times out and then recovers — that is not the core1 hang this test guards (a permanent hang answers nothing from the hang point on → a long consecutive run, which still fails). A retried `_master_alive` settle runs before the burst to drain the carried-over window and still catch a real hang.
  - The **`font-pack wipe round-trip`** test (`test_fontpack_wipe_roundtrip`, v6+) is the
    **only** HIL test that drives the actual per-bundle font-pack flash transport
    (`BEGIN/CHUNK/COMMIT`, cmds `0x50`–`0x52`): it flashes the 32-byte empty-pack
    sentinel to **slot 0** and asserts COMMIT returns `.` — that's the `fontpack_slot_present`
    success gate a field bug once made falsely NACK on a wipe — then re-reads GET_ID and
    confirms slot 0 advertises `content_version 0`. ⚠️ **Side-effecting**: it empties the
    `symbol` bundle on the rig (harmless — a real PolyKybdHost re-flashes it on the next
    connect; the empty-pack flash erases only ~2 sectors so it's fast). It runs **last**
    (most disruptive) and is gated `min_protocol: 6`, so a pre-v6 board SKIPs it. The
    read-only **`font-pack version block (v6)`** test just validates the GET_ID block shape.
    `_build_empty_fontpack()` is byte-identical to PolyKybdHost `hid_fontpack.build_empty_pack()`.
  - The **`glyph script round-trip (v9)`** test (`test_glyph_script_round_trip`) mirrors
    the idle-style round-trip for HID cmd 30 (`GLYPH_SCRIPT`): query `0xFF` → set the
    other always-present script → read back → restore. Gated `min_protocol: 9`,
    so a pre-v9 board SKIPs it. Pack-agnostic (selecting Tengwar with no `fantasy` bundle
    just falls back to Latin on the keycaps, but the get/set state round-trips regardless),
    and non-side-effecting (restores the original script), so it sits with the other
    mutate+restore round-trips, not among the disruptive upload tests. The companion
    **`glyph script expansion (v10)`** test (`test_glyph_script_expansion`, `min_protocol: 10`)
    covers the v10 **open-ended index**: it round-trips known scripts RUNES(2), IBMVGA(6)
    and the max BRAILLE(10), then sets a deliberately-unknown high index (200) and asserts
    it is **ACCEPTED + stored verbatim** (a pre-v10 board would NACK it) — that graceful
    acceptance is what decouples "add a font face" from the protocol version — then restores.
    Same pack-agnostic, mutate+restore shape; a pre-v10 board SKIPs it. `GLYPH_SCRIPT_MAX`
    (=10) tracks the highest *known* `poly_glyph_script`; higher indices are valid on the
    wire and just render the normal legend.
- [ ] Add GPIO-driven key matrix simulation so tests can simulate key presses
- [ ] Test `scripts/setup.sh` on a fresh RPi4 and fix any issues

## Writing test cases

Tests are plain dicts with `name` and `fn` keys. `fn` receives `(raw_hid: RawHID, log: Callable)` and returns a bool:

```python
from station.hid import RawHID

def test_ping(raw: RawHID, log) -> bool:
    response = raw.send(b'\x01')          # 0x01 = ping command (define in QMK)
    log(f"ping response: {response!r}")
    return response is not None and response[0] == 0x01

TESTS = [
    {"name": "raw HID ping", "fn": test_ping},
]
```

Pass `tests=TESTS` to `runner.flash_and_test(...)`.

### Tolerating not-yet-deployed changes (skip / xfail markers)

Every time the protocol (or some other firmware detail) changes, the rig used to
go red on the *old* firmware until the new image was built and flashed — even
though we already knew the new check can only pass after the update. A test dict
may now carry optional **gate markers** so such a check is *skipped* (or
*tolerated*) rather than hard-failing, and **un-skips itself automatically** once
the firmware that satisfies it is flashed:

| Key | Effect |
|---|---|
| `"min_protocol": N` | **SKIP** (not fail) unless the flashed firmware advertises `PROTOCOL_VERSION` ≥ N. Reads the `P<n>` token from `GET_ID` — un-skips the moment a firmware ≥ N is flashed. |
| `"min_fw": "0.8.22"` | Same, gated on `FW_VERSION` (dotted-numeric compare). For changes not tied to a protocol bump. |
| `"xfail": "reason"` | Run the test, but downgrade a FAIL to **XFAIL** (tolerated) and an unexpected PASS to **XPASS** (surfaced loudly so the marker gets removed). For "details" not visible in `GET_ID`. |

```python
TESTS = [
    {"name": "new cmd 28 round-trip", "fn": test_cmd28, "min_protocol": 3},
    {"name": "host-side fold landed", "fn": test_fold,  "xfail": "needs PolyKybdHost release"},
]
```

Only a genuine **FAIL** fails the run; SKIP / XFAIL / XPASS do not. The device's
advertised versions are parsed from `GET_ID` by `parse_device_caps()` and the
gate decision is `skip_reason()` (both pure + unit-testable in `hil_tests.py`);
the runner reads the caps **lazily** — only when a gated test is reached, which
is after the fresh-boot test has consumed the one-shot `*` marker, so the gate's
`GET_ID` never disturbs `test_fresh_boot_marker`. If `GET_ID` can't be read or
parsed, the gate **runs** the test rather than skipping, so a real fault still
surfaces. The job Step Summary marks each line ✅ pass · ❌ fail · ⏭️ skip · 🟡
xfail · ❗ xpass, with a count line and an `::error::`/`::warning::` annotation
per fail/xpass. The protocol-v2-only tests (legacy-NACK, packed list, language
round-trip) already carry `"min_protocol": 2`, so a pre-v2 board skips them
instead of going red.

`RawHID` offers three send shapes: `send()` (one report, one reply — the common case),
`send_and_read_all()` (one report, *all* replies — for multi-packet commands like
GET_LANG_LIST), and `write_reports()` (a burst with no reply — for the overlay upload
commands, which the firmware does not ACK; follow with a `send(GET_ID)` liveness check).

The runner reports each test as its own line: a `[test] PASS/FAIL: <name>` log line, plus
— under GitHub Actions — a ✅/❌ bullet per test in the job **Step Summary** and a
`::error::` annotation for each failure, so it is obvious from the run page which test
failed without scrolling the raw log.

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
  qmk's `qmk-test.yml` runs on the `perf` PR label, `[perf]` in a commit message,
  or a manual `workflow_dispatch`. It posts a markdown table to the job summary +
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
- **Offline tests**: `tests/perf_test.py` (`python -m unittest discover -s tests -p
  "*_test.py"`, 23 tests, no hardware). Its `FakeProfilerDevice` re-implements the
  firmware's cmd-32 replies byte for byte, so it is a genuine **contract test of
  the wire format** — if the C encoder and the Python decoder ever disagree on
  layout/ordering/endianness it fails there rather than producing plausible
  nonsense on the rig. `LOOP_PROFILE_SNAPSHOT_VERSION` (firmware) and
  `SNAPSHOT_VERSION` (`perf.py`) must move together; a mismatch is refused loudly.

## Runner troubleshooting

When a CI job hangs at "Waiting for a runner to pick up this job", the station can diagnose and recover the self-hosted runner without SSH.

- **Header badges** (`station/ui/app.py` pollers): `CI` (workflow running) and `RUNNER` (online/busy/offline/missing-labels/noauth). Both need a `github:` block in `config.yaml`.
- **⚕ Diagnose** (`run_diagnostics` → `_run_diagnostics`) streams a full report to the log: local systemd unit + `Runner.Listener` process, GitHub-side registration & labels, queued jobs and their requested labels, connectivity, then a plain-language verdict.
- **⟳ Restart** (`restart_runner` → `register-runner.sh --restart-only`): `systemctl restart` only. For a configured-but-wedged runner.
- **↻ Re-register** (`reregister_runner` → `register-runner.sh --no-reinstall`): stop → wipe creds → reconfigure → restart. For a broken/`Not configured` registration. Two-tap confirm in the UI.

`scripts/register-runner.sh` modes: bare (mint token from PAT), `--token` (manual), `--no-reinstall` (re-config + restart, kiosk path), `--restart-only`, `--restart-only`/`--no-reinstall` are mutually exclusive. The PAT comes from `--pat`, `$GITHUB_PAT`, or `github.token` in `config.yaml` (needs `Administration: Read and write` to mint registration tokens). The kiosk buttons require the `/etc/sudoers.d/polykybd-runner` grant that `setup.sh` installs (scoped to `systemctl start/stop/restart actions.runner.*`). The **first** registration must be done over SSH (it installs the systemd unit); after that the touchscreen can recover it.

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

### Self-update mechanism

- **`scripts/self-update.sh`** is the single actuator, run by both the timer
  (unattended) and the UI button. It fetches the tracked branch (`update.branch`
  in `config.yaml`, default `main`), and if behind: **defers while busy** (polls
  `GET /status`; any status other than `idle`/`error` ⇒ skip this tick, retry
  next — never aborts a flash/HIL run), else fast-forwards (`--ff-only`, so it
  never clobbers the gitignored `config.yaml` or rewrites history), pip-installs
  only if `requirements.txt` changed, and `sudo systemctl restart
  polykybd-ctnd`. The whole body is in a `{ … }` group with an explicit `exit` so
  bash parses the entire file before running — a pull that rewrites the script
  mid-run can't desync the interpreter. `--check` reports behind/ahead without
  applying (exit 10 = behind); `--no-restart` pulls without bouncing the service.
- **`polykybd-update.service`** (oneshot) runs the script in its **own cgroup**,
  so the `restart polykybd-ctnd` it issues at the end does not kill the updater.
  **`polykybd-update.timer`** fires it `OnBootSec=2min` then every 5 min.
- **UI**: the `UPDATE` header badge (`app.py` `_update_poll_once`, 120 s) shows
  `UP ✓` (current) / `UP ↓N` (behind) / `UP …` (updating); tap = two-tap-confirm
  `update_now`, which fetches, logs the incoming commits, and kicks the oneshot
  via `sudo -n systemctl start --no-block polykybd-update.service`. The badge
  re-polls to `current` after the service restarts and the browser reconnects.
- **`setup.sh`** installs both units (enables the timer) and a scoped
  `/etc/sudoers.d/polykybd-update` granting the station user NOPASSWD on exactly
  `systemctl restart polykybd-ctnd.service` and `systemctl start
  polykybd-update.service`.

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

## Related repos

| Repo | Role |
|---|---|
| `thpoll83/qmk_firmware` | Keyboard firmware — source of UF2 files, target of CI workflow |
| `thpoll83/PolyKybdHost` | Host application — shares the Raw HID protocol used in `station/hid.py` |
