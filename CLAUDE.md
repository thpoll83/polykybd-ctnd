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
- [x] Write concrete test cases — `station/hil_tests.py` covers every Raw HID command testable without side effects on the unattended rig (identity/fresh-boot, language get/list/list-packed/round-trip, default layer, ACK/NACK error+bounds paths, idle-style / OS / glyph-script round-trips, overlay-flags round-trip, every overlay upload shape — plain, core1 RLE, the two-packet cmd-17 continuation, ROI + its bounds clamp, and both mapping commands — the animation and idle-screensaver paths, GET_ID stress, a bridged split-link soak, and the font-pack/doom flash transport), wired into the `test_runner.py` CLI, plus two runner-level checks that need hardware control (the firmware-update stage+verify and the reboot-persistence power cycle). ⚠️ Count the entries in `TESTS` rather than trusting a number written here — this line has been stale before. Remaining infra-dependent / camera-needing / deliberately-excluded items are in `docs/FUTURE_TESTS.md`.
  - The **packed language-list** test (cmd 27, protocol v2+) decodes the 2-byte ISO index pairs via `station/iso_lang_country.py` and validates the list **standalone** — staple locales present, every code well-formed `llCC`, decoded count matches the count byte, current language present. (It no longer cross-checks against the ASCII `GET_LANG_LIST`: that command is **retired** — a separate test asserts cmd 8 now NACKs.) ⚠️ `station/iso_lang_country.py` is the **frozen index table**, byte-identical to the copies in `qmk_firmware` (`keyboards/polykybd/lang/`) and `PolyKybdHost` (`polyhost/services/`); keep all three in sync (`cmp`) or the rig decodes wrong languages. cmd 27 is the only language-list command on v2+ firmware; on a pre-v2 board it NACKs — but the packed/legacy/round-trip tests now carry `"min_protocol": 2`, so a pre-v2 board **skips** them rather than failing (see "Tolerating not-yet-deployed changes" below).
  - The **`GET_ID stress`** test (`test_get_id_stress`) deliberately **tolerates isolated no-answers** and fails only on a *freeze signature* — decided by the pure `classify_get_id_stress(oks, n)`: FAIL if total misses `> max(2, n//10)` or there is a run of `>= STRESS_FREEZE_RUN` (3) consecutive misses; otherwise PASS. ⚠️ **Do not re-tighten it to fail on the first miss** — it runs right after the overlay-upload tests, which leave the master in its transient post-overlay **deaf window** (EEPROM write + full keycap refresh; `send_repeated` already retried the host-side USB hiccups internally). The qmk **split-sync re-fire fix** (#80, `sync_succeeded()`) can *lengthen* that window on the rig, where master→slave sync is flaky, so an occasional GET_ID times out and then recovers — that is not the core1 hang this test guards (a permanent hang answers nothing from the hang point on → a long consecutive run, which still fails). A retried `_master_alive` settle runs before the burst to drain the carried-over window and still catch a real hang.
    - ⚠️ **The `max` in that test's `min/avg/max` line is NOT a device latency once
      `transient` is non-zero — it is the harness's own retry timeout, and it reads
      exactly like a firmware stall.** `send_repeated` stamps `t0` **before** the retry
      loop and appends the latency **after** it, so an exchange that times out and
      succeeds on a later attempt records the dead timeout too. Run #805 logged
      `50/50 GET_IDs OK (2 transient HID retries) — latency min/avg/max = 3/43/2010 ms`,
      and 2010 ms is `2 × timeout_ms (1000) + ~10 ms` for the attempt that answered — one
      exchange, two lost reads. The arithmetic confirms it end to end: 49 samples at ~3 ms
      plus one at 2010 averages 43 ms, exactly the reported mean. So the honest reading is
      *"one exchange needed three attempts"*, **not** *"the firmware stalled for two
      seconds"* — which is how it was first written up, before the retry accounting was
      checked. Two consequences: **read `transient` before believing `max`**, and never
      take a latency **baseline** from a run whose burst reports a non-zero `transient`
      (see `docs/FUTURE_TESTS.md`). The `min` and the mean-minus-outlier stay
      trustworthy; only `max` absorbs the timeout.
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
  - The **`glyph size round-trip (v13)`** test (`test_glyph_size_round_trip`,
    `min_protocol: 13`) covers HID cmd 34 (`GLYPH_SIZE`) — the keycap legend size, 0
    small / 1 medium / 2 large. It round-trips all three and restores the original,
    same pack-agnostic mutate+restore shape as the two above. ⚠️ **Its out-of-range
    NACK check is the POINT of the test, not a bounds nicety, and it asserts the exact
    OPPOSITE of `test_glyph_script_expansion` one command over.** cmd 30's range is
    open-ended (an unknown script is accepted and degrades to the normal legend, which
    is what decouples new faces from the protocol); cmd 34's is CLOSED, because an
    unknown size would be stored, synced and persisted while still rendering small — a
    setting that silently does nothing. The pair is what pins that asymmetry as
    deliberate, so neither should be "made consistent" with the other. It also re-reads
    the size after the refusal: a NACK that still moved the state would otherwise pass.
  - The **`layer names (v14)`** test (`test_layer_names`, `min_protocol: 14`) reads
    HID cmd 35 — the names the host layout editor puts on its layer tabs. ⚠️ **Its
    cross-check against `id_dynamic_keymap_get_layer_count` is the POINT of the test,
    not a bounds nicety.** The firmware answers both from the same constant precisely
    so the editor cannot size its tab strip from one command and label it from the
    other; a change that made cmd 35 report `DYNAMIC_KEYMAP_LAYER_COUNT` (12) instead
    of the write cap (8) would leave the editor drawing tabs it has no names for, and
    nothing else on the rig would notice. Read-only, so nothing to restore. The
    payload is `[total][count]` then NUL-terminated names; the test reads the total
    first and bounds every later slice by it, so the report's zero fill is never read
    as a separator and an **unnamed layer** stays distinguishable from padding (it is
    logged as a note, not a failure).
    - ⚠️ Fixing this also corrected **`MAX_LAYERS`**, which had sat at **14** here
      long after the firmware dropped to 12. It is only a sanity bound on the
      default-layer read, so nothing failed — the same silent-staleness class as the
      host's `layer_names.yaml`, and the reason that file is now only a fallback.
  - The **`overlay mapping widths (v12)`** test (`test_overlay_mapping_widths`,
    `min_protocol: 12`) covers HID cmd 33 (`SEND_OVERLAY_MAPPING_W`), the
    variable-width mapping command. ⚠️ **It is a liveness guard, not a
    round-trip** — cmd 33 is silent by design (it sits in the no-reply
    overlay-activity group with cmd 21) and nothing reads `display_to_pool` back,
    so the test can only assert that decoding a report doesn't wedge the master.
    That is the coverage that matters, because every bug this command shipped
    with was in the bit arithmetic, and **the byte pattern differs per width**:
    `gcd(width,8)` decides it — at 8 each value is one whole byte at offset 0, at
    10 the offsets stay in {0,2,4,6} and never reach a third byte, and **only the
    odd widths 9 and 11 walk all eight offsets and read a third byte**. Those two
    are new at v12 and are exactly where the old fixed expression computed
    `0xff >> (8 - n)` (a shift by −2 at offset 7, unreachable at 10 bits); width 8
    is where an unconditional second-byte read ran past the buffer. So each of
    8/9/10/11 gets a **full** report — every value slot filled, `from` drawn from
    the band that genuinely needs that width, including the `>= 1024` GUI-combo
    band only v12 can address — followed by a GET_ID liveness check; then widths
    7 and 17 confirm the `OVERLAY_MAP_WIDTH_MIN/MAX` guard drops the report
    instead of slicing garbage (the firmware logs `REJECTED overlay mapping
    report: bad width`, which shows up in the captured console on a failure).
    Mutate+restore: a `finally` resets the mapping and usage bits to the power-on
    identity via cmd 11 `MAPPING_RESET|USAGE_RESET` (`0xC0`). ⚠️ `_pack_mapping_values`
    mirrors PolyKybdHost `bit_packing.pack_values` — verified byte-identical and
    round-tripped through its decoder at all four widths, per the standing
    "verify the packer through the decoder, not by eye" rule.
- [x] **Assert on the firmware console, don't just echo it.** `split72/keyboard.json`
  sets `"console": true`, so the rig has always received firmware diagnostics and
  only ever logged them. `station/console_log.py` now taps them and two tests read
  them back. Three things to know before writing another such check:
  - ⚠️ **A console read is a report-sized FRAGMENT, not a line** — reassemble
    across reads (`ConsoleTap.feed`) and classify only `\n`-terminated lines.
    Matching a raw chunk drops every continuation and truncates what it keeps;
    that shipped once in `perf_runner` (`ovltot wall=16ms bridg`).
  - ⚠️ **Most diagnostics are gated on `debug_enable`, which defaults FALSE** —
    `Failed to sync … for transaction X` and `Bridge sync retry` both are, so they
    never appear on the rig. The `Split link:` summary is deliberately ungated ("a
    passive wire-health diagnostic with no key content"), which is exactly why the
    link check reads the *counter* and not the failure lines. Check the gate in
    `bridge_helper.c` before designing around any console line.
  - A console-reading test carries **`"needs_console": True`** and SKIPs when the
    console did not come up. Without that gate it would assert nothing and report a
    green it did not earn — the "reads as coverage" failure the version gates
    already exist to avoid.
- [x] **Split-link health is now a CI check, not just a log line.**
  `test_split_link_health` bridges 450 cmd-21 mapping reports (one bridged frame
  each — the cheapest way to generate measurable traffic) so the firmware's
  200-frame `Split link:` summary fires at least twice, then asserts on the
  **delta**. Two things that are load-bearing and easy to get backwards:
  - **Absolutes are useless**: a healthy rig has a documented boot burst (crc_err
    and giveup in the tens), so any check on the cumulative counters either fails
    every run or is set so high it never fires.
  - **`nack` is not an error, and neither is `giveup` on its own** —
    `classify_link_health` counts only `crc_err + transport_fail`, matching the
    firmware's own `err%`. `SYNC_BUSY` (a nack) arrives on *every* erase re-poll of
    a flash, so counting nacks would redden a healthy run the moment the font-pack
    test runs.
  - The soak's `from` values are deliberately **off-screen** (>= 900): an on-screen
    one would make each of the 450 reports request a display refresh and the test
    would measure the renderer instead of the link.
- [x] **`reboot_persistence` is the only check that survives a power cycle**, and it
  is runner-level (it needs `FlashController.reset()` — which the rig had all along
  and no test had ever used). It sets the idle style, flushes with **cmd 26**
  (`save_all_dirty`), power-cycles the master over the RUN pin and reads it back.
  Everything else in the suite asserts RAM state, so a value that is applied
  correctly and never actually persisted passes every other test — which is the
  shape of most of the firmware's EEPROM field bugs (brightness coming up 0, the
  default layer not surviving, the latin map reading back all-zeros through wear
  levelling). It runs **last** and only when the rest of the suite is green:
  rebooting the master alone leaves the slave mid-session and the split link to
  re-establish, and a rig that is already misbehaving should not also be
  power-cycled. Staging a firmware image first is safe — `fw_staging_init()` clears
  the apply/reboot flags at boot, so a staged-but-uncommitted image stays inert.
- [x] **`FW_UP_GET_VERSION` (cmd 0x43) is asserted, not just logged.** The staged
  `.bin` contains the same compile-time GET_ID literal the firmware answers with
  (`caps_from_image`), so the two are compared. This is what catches a flash that
  silently did not take — otherwise invisible, since every test build reports the
  same `FW_VERSION` and the UF2 filenames carry no version. ⚠️ Compare the
  **version string only**: the running image is the HIL build and the `--bin` is the
  plain one, so `fw_size`/`fw_crc` legitimately differ between them.
- **The slow checks are OPT-IN — `TIER_EXTENDED`.** The animation, the idle-engage
  + Eden screensaver, the split-link soak and the reboot power cycle add most of a
  minute to a gate every push pays for, so they are skipped unless the run asks:
  `python -m station.test_runner --extended` (or `HIL_EXTENDED=1`), the
  **`hil-extended`** PR label, `[hil-extended]` in a commit message (push events
  only), a manual `workflow_dispatch`, or the touch UI's **Extended** toggle beside
  Run Tests. Three things worth knowing:
  - ⚠️ **The label starts its own run, but a RE-RUN can never pick it up.** A
    re-run replays the **original** event payload, so a label added afterwards is
    invisible to `github.event.pull_request.labels` and the run silently repeats
    the default tier. qmk's `build` job therefore excludes `labeled` events (so
    the auto-labeler cannot re-run the pipeline) *except* for this one label,
    matched on `github.event.label.name`. Label the PR — don't re-run an older
    run and expect it to notice.
  - **The gate is fail-CLOSED**, the opposite of the version gates: a caps dict
    that never heard of tiers still skips, because the cost is the whole point.
    The version gates fail *open* for the opposite reason (better to run and see a
    real failure than to hide one behind an unverifiable gate).
  - ⚠️ **Tier is about COST, never confidence.** An extended test is slow or
    disruptive — never flaky or unproven. Anything unreliable belongs in
    `docs/FUTURE_TESTS.md` until it is trustworthy, not in a tier nobody runs;
    otherwise "extended" becomes where failing tests go to be forgotten. A unit
    test pins the membership so a test cannot be quietly demoted to stop it failing.
- ⚠️ **The touch UI's "Run Tests" button ran NO tests until 2026-08-20.**
  `on_run_tests` called `flash_and_test(left, right)` without `tests=TESTS`, and
  the default is `None` → `for test in (tests or [])`, so it flashed, blanked the
  displays and returned `{"passed": True, "results": []}` — a green result from a
  run that asserted nothing. The CLI has always passed `TESTS`, so CI never saw it
  and the UI reported success the whole time. Generalise: **a "passed" with an
  empty `results` list is not a pass**, and any new caller of `flash_and_test` has
  to pass the suite explicitly.
- ⚠️ **Two tests REPORT latency instead of asserting it, deliberately.**
  `test_replay_animation` and `test_idle_eden_screensaver` assert the freeze
  signature (no answers at all) and log the HID round-trip median/p95/max. A sliced
  Eden frame should keep round-trips in the tens of ms and an unsliced one push them
  toward the ~150 ms frame cost — so the median is what would catch the shipped
  "Eden doesn't wake on the first keypress" regression — but the rig has never
  published a baseline for it, and a threshold guessed from the source is how a
  check becomes flaky and then ignored. Read the logged medians across a few runs,
  then promote it (tracked in `docs/FUTURE_TESTS.md`).
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

⚠️ **`send()` RETRIES by re-writing the request, and that is only safe because the
commands are idempotent — `GET_ID` is the one exception.** It consumes the firmware's
one-shot fresh-boot marker, so when a reply is dropped the firmware has *already*
cleared the marker, the retry re-issues GET_ID and gets a perfectly correct `.`, and
the test sees **wrong data** instead of a timeout. That matters because the runner
grades a dropped reply as a non-failing **WARN** but wrong data as a **FAIL** — so the
retry was silently converting a transient rig hiccup into a red HIL check
(qmk_firmware#197, where every other test in the run passed). Fixed in #66 by pinning
that one call to `attempts=1`; the regression test is `tests/hil_tests_test.py`, whose
`FakeMarkerDevice` clears the marker on the **write**, as the firmware does.
- The premise came from **#28**, which introduced the retry *and* the WARN status in
  the same change and stated "all commands sent via `send()` are idempotent". The
  retry it added is what kept the WARN path it added from ever seeing this failure.
- ⚠️ **Do NOT "centralise" this by special-casing GET_ID inside `send()`** (both AI
  reviewers on #66 suggested it). GET_ID is sent from **seven** places and **six
  depend on the retry** — `_master_alive`, the sustained-settle loop, the GET_ID
  stress burst, the identity test, the font-pack version read, and the second GET_ID
  in the marker test itself. Auto-pinning by command id would strip the tolerance
  from exactly the probes that run in the master's post-overlay deaf window, where
  isolated misses are expected. The property is **"this read observes a one-shot side
  effect"**, which belongs to the call site, not the command. If a *second*
  non-idempotent command ever appears, promote it to an explicit `send_once()` then.

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
- ⚠️ **A rig provisioned before a unit landed never gets it — `setup.sh` is the
  only installer, and nothing re-runs it.** Hit 2026-08-03: the UPDATE button
  failed with `Unit polykybd-update.service not found`, i.e. the rig predates the
  self-update feature, so **the timer was missing too and unattended updates had
  never run there** (HIL was unaffected only because `qmk-test.yml` force-syncs
  the station to `origin/main` itself — see the stale-rig warning above). The unit
  is just the *carrier*; `scripts/self-update.sh` is the actuator, so `update_now`
  now falls back to running it in-process (`--no-restart`, then a separate
  `systemctl restart` — the script's own restart would tear down the UI's cgroup
  mid-pull) and `_diagnose_unit_start_failure()` distinguishes a missing unit from
  a missing sudoers grant, which need opposite fixes. Recovery is
  **`sudo bash ./scripts/setup.sh --units-only`** (`bash …`, not `./…`: an older
  checkout lacks the execute bit, and with no `x` bit set *even root* gets
  `Permission denied`) — installs only the units + service
  sudoers grants (no apt, no venv rebuild, no `config.yaml`/chown churn), which is
  what you want on a *working* rig. Don't send someone through a full `setup.sh`
  run to drop two files.
  - ⚠️ **Why it hid for the rig's whole life: the CI force-sync masked it.** The
    "Sync station to current ctnd main" step was added so a *lagging* timer
    couldn't leave HIL on stale code — and it also removed the only symptom that
    would have revealed a timer that was never installed *at all*. HIL stayed
    green throughout; the UPDATE badge polls git directly, so it correctly showed
    "N behind" while reporting nothing about whether the mechanism that applies
    updates exists. Generalise before adding the next such workaround: **a
    compensating sync hides the difference between "slow" and "absent", and
    nothing here checks that the installed units/grants still match the repo.**

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
- ⚠️ **Firmware signing (FW-2) does NOT close the code-execution surface — FW-9 is open.**
  The `.plyx` DOOM engine pack is *executable code* flashed over the same HID transport,
  and `doom_pack_load.c` authenticates it with a CRC32 only, then branches into it. Anyone
  who can talk raw HID can flash a crafted pack, set the idle style to IDDQD over cmd 28,
  and get arbitrary code execution on the next idle. Don't cite "the firmware is signed"
  as though it settles this.

## Related repos

| Repo | Role |
|---|---|
| `thpoll83/qmk_firmware` | Keyboard firmware — source of UF2 files, target of CI workflow |
| `thpoll83/PolyKybdHost` | Host application — shares the Raw HID protocol used in `station/hid.py` |
