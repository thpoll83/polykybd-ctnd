# CLAUDE.md — PolyKybd CTND

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
station/ui/app.py       Flask + Flask-SocketIO server; emits log/status events via WebSocket
station/ui/templates/   index.html — 1024×600 dark touch UI
station/ui/static/      style.css, app.js
systemd/                Service units for the Flask daemon and Chromium kiosk
scripts/setup.sh        One-shot RPi4 setup (apt, udev, venv, systemd)
scripts/kiosk.sh        Manual kiosk launch fallback
.github/workflows/      Example CI workflow to copy into qmk_firmware repo
firmware/               Drop UF2 files here; the UI picks them up automatically
```

## Key design decisions

- **uhubctl per-port power switching** is exposed as a manual convenience in the UI (and for the BOOTSEL data-disconnect on some boards), *not* as a way to choose the split master. ⚠️ **The RPi4's built-in USB-A ports do NOT support per-port power switching** — the VL805 host controller ignores the command, so `uhubctl ... -a off` reports success but VBUS stays energized. An earlier version of this doc claimed native per-port power control; that was wrong and is why "turning off the port" never dropped a half to slave. Real per-port power switching needs an external `uhubctl`-compatible powered hub. The flash sequence itself only needs the RUN/BOOTSEL GPIO pins and does not depend on cutting power.
- **GPIO pins** (BCM 17/18 for left, 22/23 for right) drive the RUN and BOOTSEL pads on the PCB via a 2N2222 NPN BJT low-side switch circuit (see below). Pads are exposed on the assembled boards since no key switches are fitted.
- **Master/slave selection on the rig is forced in firmware at compile time, per side.** Stock PolyKybd firmware picks the master from `USB_VBUS_PIN` (GP24). On the rig both halves are cabled to the Pi, so both read VBUS high and both detect as master — and the Pi can't drop VBUS (see uhubctl note above). The fix is **two per-side HIL images**, each overriding `is_keyboard_master_impl()` in `keyboards/handwired/polykybd/polykybd.c` (shared by split72/corne42): `-e POLYKYBD_HIL=left` (or the `=yes` alias) builds the **master** image (`return true`), and `-e POLYKYBD_HIL=right` builds the **slave** image (`usb_disconnect(); return false`, so it doesn't enumerate as a second keyboard). The role is **fixed at build time — it is NOT read from EE_HANDS** (the rig provisions no handedness marker; a fresh EEPROM reads back "not left", which would make *both* halves slaves). Normal user firmware never defines `POLYKYBD_HIL` and keeps VBUS detection. ⚠️ `test_runner.py` must be handed the **`*_hil_left.uf2` for `--left` and the `*_hil_right.uf2` for `--right`** — flashing a single master image to both sides makes *both* enumerate as master (the original `qmk-test.yml` bug that `single master enumerates` catches).
- **EE_HANDS** stores the side in EEPROM for *normal* firmware (set once via QMK Toolbox or a keymap combo; survives reflashes). The HIL build does **not** use it — `is_keyboard_master_impl()` ignores EE_HANDS and the role comes from the per-side compile flag above — so the rig needs no handedness provisioning before a HIL run.
- **Flask-SocketIO** (threading mode) is used for the web UI so log lines stream to the browser in real time without polling.
- **Idle screen blanking** is plain X11 DPMS, armed by the kiosk unit's `ExecStartPre` (`xset s off; xset +dpms; xset dpms 0 0 300`): the HDMI output blanks after 5 min idle and any touch wakes it. Relies on the panel backlight following the HDMI signal while the USB touch controller stays powered — so wake-on-touch needs no daemon. `xset` ships in `x11-xserver-utils` (installed by `setup.sh`). X11-only; a Wayland move would need `swayidle` + `wlr-randr`.
- **Right half is flashed first** in `test_runner.py` because it communicates via the PIO UART split cable, not USB HID, so a brief USB reboot on the right half doesn't disrupt the test HID path.

## Hardware facts (verified)

- **VID/PID**: `0x2021:0x2007` (PolyFabriq PolyKybd Split72) — in `config/config.yaml`
- **Raw HID usage**: `RAW_USAGE_PAGE 0xFF61`, `RAW_USAGE_ID 0x62` — from `split72/config.h`
- **RUN and BOOTSEL circuits**: identical 2N2222 NPN BJT low-side switch (see schematic below). `gpio.inverted: true` in config (the default).
- **GPIO logic**: GPIO HIGH → BJT saturated → pin pulled to ~0.1 V (asserted). GPIO LOW → BJT off → pin held HIGH by RP2040 internal pull-up (~50 kΩ) → running/released. Idle state is GPIO LOW.
- **EE_HANDS**: firmware stores the handedness side in EEPROM (`#define EE_HANDS` in `split72/config.h`) for *normal* (non-HIL) use; survives all UF2 reflashes. It sets left/right only — **not** master/slave — and the HIL build ignores it entirely (see below), so it does not need provisioning for a HIL run.
- **HIL master forcing**: the rig flashes **two per-side images** — `-e POLYKYBD_HIL=left` (master) to the left half, `-e POLYKYBD_HIL=right` (slave, `usb_disconnect()`) to the right half. This is required — with a single image both halves detect as master because both see USB VBUS on GP24 and the RPi4 can't drop it. The override lives in `keyboards/handwired/polykybd/polykybd.c` (`is_keyboard_master_impl`), gated by `-DPOLYKYBD_HIL` / `-DPOLYKYBD_HIL_SLAVE` from `keyboards/handwired/polykybd/rules.mk` (`POLYKYBD_HIL=left|right|yes`). `=yes` is an alias for `left`. **Flashing one `=yes` image to both sides (the original `qmk-test.yml`) makes both master — that is the bug the HIL suite now catches.**
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
- [x] Write concrete test cases — `station/hil_tests.py` now covers every Raw HID command testable without side effects on the unattended rig (15 tests: identity/fresh-boot, language get/list/round-trip, default layer, ACK/NACK error+bounds paths, overlay-flags round-trip, plain + core1-compressed overlay liveness, GET_ID stress), wired into the `test_runner.py` CLI. Remaining infra-dependent / camera-needing / deliberately-excluded items are in `docs/FUTURE_TESTS.md`.
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

`RawHID` offers three send shapes: `send()` (one report, one reply — the common case),
`send_and_read_all()` (one report, *all* replies — for multi-packet commands like
GET_LANG_LIST), and `write_reports()` (a burst with no reply — for the overlay upload
commands, which the firmware does not ACK; follow with a `send(GET_ID)` liveness check).

The runner reports each test as its own line: a `[test] PASS/FAIL: <name>` log line, plus
— under GitHub Actions — a ✅/❌ bullet per test in the job **Step Summary** and a
`::error::` annotation for each failure, so it is obvious from the run page which test
failed without scrolling the raw log.

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
3. On the RPi4: `git -C /opt/polykybd-ctnd pull && sudo systemctl restart polykybd-ctnd`

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
