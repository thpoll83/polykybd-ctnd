# PolyKybd HIL — future test backlog

This is the planned-but-not-yet-implemented test backlog for the hardware-in-the-loop
(HIL) rig. Implemented tests live in [`station/hil_tests.py`](../station/hil_tests.py)
and are exported via its `TESTS` list; the runner ([`station/test_runner.py`](../station/test_runner.py))
flashes both halves then calls each `fn(raw_hid, log) -> bool`.

When you implement one of these, move it out of this file, add it to `hil_tests.py`
(`TESTS`), and tick the box below in the same PR.

## Conventions

- A test is a dict `{"name": str, "fn": Callable[[RawHID, Callable[[str], None]], bool]}`.
- `fn` returns `True` to pass, `False` to fail; raising is also treated as a failure.
- Keep tests **independent and order-tolerant** where possible. If a test mutates
  persistent state (EEPROM, language, layer), restore it at the end (see
  `test_language_round_trip` for the `try/finally` pattern).
- Log generously via the `log` callback — the rig has no other console.
- Reference the firmware command IDs in
  [`keyboards/handwired/polykybd/hid_com.c`](https://github.com/thpoll83/qmk_firmware/blob/PolyKeyboard/keyboards/handwired/polykybd/hid_com.c)
  (`raw_hid_receive()` dispatcher); responses are `"P<id><status>…"` where status is
  `.` (ACK), `!` (NACK), or `*` (GET_ID fresh-boot marker). The command IDs also have
  names in PolyKybdHost's `polyhost/device/command_ids.py` (the shared protocol).
- For multi-report replies use `RawHID.send_and_read_all`; for fire-and-forget
  command bursts with no ACK use `RawHID.write_reports`.

## Implemented (for reference)

Every Raw HID command that can be asserted in a side-effect-free way on the
unattended rig now has a test. Covered:

- [x] **single master enumerates** — exactly one Raw HID interface present (validates the
  `POLYKYBD_HIL` force-master build: right half `usb_disconnect()`s).
- [x] **fresh-boot marker clears** — first GET_ID after a flash returns status `*`
  (`s_fresh_boot`); the next returns `.`. Confirms the master actually rebooted from the
  flash, not a stale enumeration. Runs before `raw HID GET_ID`, which is why that test
  accepts a plain ACK.
- [x] **raw HID GET_ID** (cmd 6) — master answers with a well-formed `Split72` identity string.
- [x] **get current language** (cmd 7) — `P\x07.<llCC>`, well-formed language code.
- [x] **enumerate language list** (cmd 8) — multi-packet list (uses `send_and_read_all`);
  staple locales present, whole number of 4-char codes.
- [x] **get default layer** (cmd 22) — plausible default-layer index (`< DYNAMIC_KEYMAP_LAYER_COUNT`).
- [x] **unknown command NACKs** — an undefined command id (0x7E) gets status `!`, dispatcher
  error path exercised.
- [x] **set brightness (ACK + range NACK)** (cmd 13) — valid level ACKs, out-of-range NACKs,
  master still answers afterward (deferred-write liveness).
- [x] **set unicode mode (ACK + NACK)** (cmd 20) — valid mode (Linux) ACKs, invalid NACKs.
- [x] **idle wake ACK** (cmd 15) — "stop idle" wakes/refreshes the display and ACKs.
- [x] **overlay flags round-trip** (cmd 11/12) — DISPLAY_OVERLAYS on then off; also force-syncs
  state to the slave (it is in `OVERLAY_SYNCED_STATE_FLAGS`). Restores default.
- [x] **language round-trip** (cmd 9) — switch to a different language, read back via GET_LANG,
  restore the original in a `finally`. Exercises the `save_user_latin` EEPROM + slave sync path.
- [x] **plain overlay keeps master alive** (cmd 10) — full uncompressed overlay upload (6
  segments) does not wedge the master; GET_ID still answers. Exercises the uncompressed
  upload + `USER_SYNC_OVERLAY_DATA` split-sync.
- [x] **compressed overlay keeps master alive (core1)** (cmd 16) — blank RLE overlay to several
  keycodes drives the core1 decompression path; GET_ID still answers. The HIL regression
  guard for the `cpsid i` workaround in `multicore_exec.c`.
- [x] **GET_ID stress** — 50 rapid GET_IDs all ACK, latency reported. Catches the master
  freezing under load.

## Backlog

### Master / slave topology

- [ ] **handedness is honoured** — after flashing with left/right EE_HANDS markers swapped,
  confirm the *other* physical half becomes the sole master. Guards against the override
  reading the wrong handedness source. Needs the rig to set EE_HANDS per half (flash-time
  EEPROM write or the GPIO key-matrix item below), so it is a runner-level test, not a
  `(raw, log)` one.
- [ ] **slave really is gone from USB** — assert no second QMK *keyboard* interface enumerates
  besides the master's (extend enumeration to the keyboard usage page `0x0001`/`0x0006`),
  i.e. the slave didn't sneak back as an HID keyboard rather than `usb_disconnect()`-ing.
  Pure enumeration, no device writes — implementable once the master's exact keyboard
  interface count is confirmed on hardware (so the `== 1` assertion can't false-fail).

### Split link / sync (needs the UART split cable connected on the rig)

- [ ] **slave responds through the master** — register a key that physically lives on the slave
  matrix (via GPIO matrix injection, below) and confirm the master reports it. Directly
  exercises the split UART transport the "slave goes unresponsive" bug lives in. (The
  host-driven KEYPRESS command is *not* a substitute — see "Deliberately excluded".) Note the
  overlay-upload tests already cover the master→slave overlay/compressed split-sync direction.
- [ ] **default-layer change survives a power cycle** — change default layer, power-cycle the
  master, confirm it persisted (the deferred-EEPROM-write fix). `get default layer` (cmd 22)
  is now implemented as the read-back half; this still needs a layer-change command + a
  controlled reboot, so it is runner-level. Watch for the slave-unresponsive regression.

### Display / overlay pipeline (render verification needs a camera or firmware read-back)

- [ ] **ROI overlay keeps master alive** (cmd 18/19) — the ROI sibling of the compressed-overlay
  core1 guard. Recipe: build the 4-byte ROI header with `compose_roi_header`-equivalent packing
  (top/bottom/left/right region + RLE bit), send cmd 18 (start) then cmd 19 (continue) for a
  small region, then GET_ID for liveness. Deferred only because the ROI geometry framing is
  fiddlier than the full-overlay path already covered.
- [ ] **overlay mapping ACK** (cmd 21) — `SEND_OVERLAY_MAPPING` is the one overlay command that
  ACKs per chunk (`P\x15.`) and is the exact path of the "slave doesn't show overlays after MRU
  switch" bug. Recipe: 10-bit-pack a few `{display_idx: pool_slot}` pairs (see
  `polyhost/device/bit_packing.pack_dict_10_bit`), send cmd 21, expect the ACK, then reset the
  mapping to identity (cmd 11 with `MAPPING_RESET` 0x80) to restore. Deferred to avoid shipping
  an unverified 10-bit packer.
- [ ] **MRU program-switch refresh** — replay a multi-chunk overlay mapping (>24 pairs, so ESC
  lands in a later chunk) and confirm the final rendered state syncs (the `DISPLAY_OVERLAYS` /
  `OVERLAY_SYNCED_STATE_FLAGS` fix). Hard to assert purely over HID — needs a firmware debug
  read or a camera.

### Robustness / soak

- [ ] **brightness deferred-write soak** — set brightness, wait >5 s for the housekeeping
  `brightness_save_if_pending()` flush (the RP2040 wear-levelling consolidation window), then
  confirm the slave is still responsive. The valid-set + immediate-liveness half is already in
  `test_set_brightness`; this adds the timed wait that targets the slave-unresponsive regression.
- [ ] **reflash idempotency** — flash → test → reflash same UF2 → test again, several cycles,
  confirm stable enumeration (no descriptor/handedness drift across reflashes). Runner-level
  (needs `FlashController` access), not a `(raw, log)` test.

## Deliberately excluded (not suitable for the unattended rig)

- **KEYPRESS** (cmd 14) — injects a *real* key event: the firmware resolves the keycode to a
  matrix position and runs `action_exec`, so it types a character into whatever has focus on the
  rig host (the kiosk). It also can't confirm the slave received the bridged event without a
  camera. The meaningful version of "did the keypress reach the slave" is the GPIO key-matrix
  injection infra item, not this host-driven command.
- **ENTER_BOOTLOADER** (cmd 23) — resets the master into the RP2040 ROM bootloader and ends the
  HID session. Only meaningful as a terminal action, which the flash sequence already performs.

## Infrastructure these tests depend on

- [ ] **GPIO key-matrix injection** — a way to simulate key presses on either half from the Pi so
  split-link and per-half tests don't need a human. Tracked in `CLAUDE.md` → "What still needs doing".
- [ ] **EEPROM read-back / flash-time handedness set** — needed for the handedness tests and to make
  language/layer round-trips deterministic.
- [ ] **HID console (CONSOLE_ENABLE)** — currently off in the PolyKybd build, so the runner treats it
  as best-effort. Several soak tests would be far easier to assert with the debug log available;
  decide whether to ship a console-enabled HIL build variant.
