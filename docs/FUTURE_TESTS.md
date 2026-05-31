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
  persistent state (EEPROM, language, layer), restore it at the end.
- Log generously via the `log` callback — the rig has no other console.
- Reference the firmware command IDs in
  [`keyboards/handwired/polykybd/hid_com.c`](https://github.com/thpoll83/qmk_firmware/blob/PolyKeyboard/keyboards/handwired/polykybd/hid_com.c)
  (`raw_hid_receive()` dispatcher); responses are `"P<id><status>…"` where status is
  `.` (ACK), `!` (NACK), or `*` (GET_ID fresh-boot marker).

## Implemented (for reference)

- [x] **single master enumerates** — exactly one Raw HID interface present (validates the
  `POLYKYBD_HIL` force-master build: right half `usb_disconnect()`s).
- [x] **raw HID GET_ID** — master answers GET_ID (cmd 6) with a well-formed `Split72` identity string.

## Backlog

### Master / slave topology (highest value — this is what the rig was built for)

- [ ] **fresh-boot marker clears** — first GET_ID after a flash returns status `*`
  (`s_fresh_boot`); a second GET_ID returns `.`. Confirms the master actually rebooted
  from the flash, not a stale enumeration from a previous run.
- [ ] **handedness is honoured** — after flashing with left/right EE_HANDS markers swapped,
  confirm the *other* physical half becomes the sole master. Guards against the override
  reading the wrong handedness source. (Needs the rig to set EE_HANDS per half — see the
  GPIO key-matrix item below or a flash-time EEPROM write.)
- [ ] **slave really is gone from USB** — assert no QMK *keyboard* interface besides the
  master's enumerates either (extend `enumerate_raw_interfaces` to the keyboard usage page),
  i.e. the slave didn't sneak back as an HID keyboard.

### Split link / sync (needs the UART split cable connected on the rig)

- [ ] **slave responds through the master** — press a key on the slave side (via GPIO matrix
  injection, below) and confirm the master reports it. Directly exercises the split UART
  transport that the "slave goes unresponsive" bug lives in.
- [ ] **default-layer change survives** — change default layer, power-cycle, confirm it
  persisted (the deferred-EEPROM-write fix). Watch for the slave-unresponsive regression.
- [ ] **language round-trip** — set language (cmd for lang change), read it back via GET_LANG
  (cmd 7), assert it matches; then restore the original. Exercises the `save_user_latin`
  EEPROM path called out as a remaining risk in the firmware notes.

### Display / overlay pipeline

- [ ] **overlay upload + ROI refresh** — send a small known overlay (cmds `0x10`/`0x11`) and a
  ROI update (`0x12`/`0x13`); assert ACKs and no timeout. This is the path that triggered the
  core1 hang — a HIL regression guard for the `cpsid i` workaround.
- [ ] **MRU program-switch refresh** — replay a multi-chunk overlay mapping (>24 pairs, so ESC
  lands in a later chunk) and confirm the final state syncs (the `DISPLAY_OVERLAYS` /
  `OVERLAY_SYNCED_STATE_FLAGS` fix). Hard to assert purely over HID without reading back
  display state — may need a firmware debug read or a camera.

### Robustness / soak

- [ ] **GET_ID stress** — N rapid GET_IDs, assert all ACK and latency stays bounded (catches the
  master freezing under load — the core1-hang symptom).
- [ ] **reflash idempotency** — flash → test → reflash same UF2 → test again, several cycles,
  confirm stable enumeration (no descriptor/handedness drift across reflashes).
- [ ] **brightness/contrast key soak** — drive brightness changes repeatedly and confirm the
  slave stays responsive afterwards (targets the 5 s deferred-write housekeeping window).

## Infrastructure these tests depend on

- [ ] **GPIO key-matrix injection** — a way to simulate key presses on either half from the Pi so
  split-link and per-half tests don't need a human. Tracked in `CLAUDE.md` → "What still needs doing".
- [ ] **EEPROM read-back / flash-time handedness set** — needed for the handedness tests and to make
  language/layer round-trips deterministic.
- [ ] **HID console (CONSOLE_ENABLE)** — currently off in the PolyKybd build, so the runner treats it
  as best-effort. Several soak tests would be far easier to assert with the debug log available;
  decide whether to ship a console-enabled HIL build variant.
