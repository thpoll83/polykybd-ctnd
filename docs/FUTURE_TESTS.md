# PolyKybd HIL — future test backlog

This is the planned-but-not-yet-implemented test backlog for the hardware-in-the-loop
(HIL) rig. Implemented tests live in [`station/hil_tests.py`](../station/hil_tests.py)
and are exported via its `TESTS` list; the runner ([`station/test_runner.py`](../station/test_runner.py))
flashes both halves then calls each `fn(raw_hid, log) -> bool`.

When you implement one of these, move it out of this file, add it to `hil_tests.py`
(`TESTS`), and tick the box below in the same PR.

## Conventions

- A test is a dict `{"name": str, "fn": Callable[[RawHID, Callable[[str], None]], bool]}`.
- **Cost tier.** `"tier": TIER_EXTENDED` marks a check that is slow or disruptive
  (waits out a fade, plays an animation, soaks the link, power-cycles the board).
  Those are skipped unless the run opts in — `test_runner --extended`,
  `HIL_EXTENDED=1`, the `hil-extended` PR label (which starts its own run — a
  re-run of an older run replays the original payload and cannot see a label added
  later), `[hil-extended]` in a pushed commit message, a manual workflow run, or the
  touch UI's Extended toggle — so the per-push gate stays fast. ⚠️ Tier is about **cost, not confidence**: a check that
  is merely unproven belongs in this file until it is trustworthy, never in a tier
  nobody runs.
- `fn` returns `True` to pass, `False` to fail; raising is also treated as a failure.
- Keep tests **independent and order-tolerant** where possible. If a test mutates
  persistent state (EEPROM, language, layer), restore it at the end (see
  `test_language_round_trip` for the `try/finally` pattern).
- Log generously via the `log` callback — the rig has no other console.
- Reference the firmware command IDs in
  [`keyboards/polykybd/hid_com.c`](https://github.com/thpoll83/qmk_firmware/blob/PolyKybd/keyboards/polykybd/hid_com.c)
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
- [x] **legacy ASCII lang list NACKs (retired)** (cmd 8) — the old multi-packet ASCII list was
  retired in protocol v2; the firmware now answers a single `P\x08!` NACK (hosts use cmd 27).
  Regression guard against the ASCII table coming back.
- [x] **enumerate language list (packed, v2)** (cmd 27) — the compact 2-byte ISO-index list,
  reassembled by raw byte slices and decoded via the frozen `iso_lang_country.py`. Self-validating
  (no ASCII ground truth): staple locales present, every code well-formed `llCC`, decoded count
  matches the count byte, current language present. The only language-list command now.
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
- [x] **compressed overlay spans two packets** (cmd 16 + **17**) — a blank overlay
  compresses to 23 bytes and fits ONE packet, so the guard above never reached the
  continuation opcode that every real image uses. An 86-byte stream forces cmd 17,
  exercising fragment-context retention across reports and the second, differently-framed
  core1 hand-off (62 bytes from `data[2]`, not 60 from `data[4]`).
- [x] **ROI overlay keeps master alive** (cmd 18/19 + the bounds clamp) — a full-width
  13-row region, large enough to need the cmd-19 continuation, then a deliberately
  out-of-bounds header (x=200 y=60 xx=127 yy=63) which `set_fragment_context_from_buffer`
  must clamp rather than index past the overlay pool. The ROI pair goes through its own
  core1 command (`CORE1_CMD_ROI_UPDATE` after `RESET_BIT_IDX`), so the decompression guard
  did not cover it.
- [x] **split link health under a bridged soak** (cmd 21) — 450 mapping reports, each
  costing exactly one bridged frame, so the firmware's `Split link:` health counter
  (printed every 200 frames) yields at least two summaries; the **delta** between them
  must show no `crc_err` / `transport_fail` growth. Deltas, not absolutes: a healthy rig
  has a documented boot burst. `nack` is excluded, matching the firmware's own `err%`
  (`SYNC_BUSY` arrives on every erase re-poll of a flash). This is also the only coverage
  cmd 21 has — the fixed 10-bit mapping older hosts still send. Needs the console.
- [x] **replay startup animation** (cmd 31) — the command ACKs, HID keeps being serviced
  between the (deliberately unsliced) intro frames, and the animation **ends by itself**
  with the master back to fast replies inside a bounded window.
- [x] **idle engages + Eden screensaver keeps HID alive** (cmd 15 start + cmd 28) — the
  first coverage of cmd 15 with a non-zero payload, whose backdate underflowed for the
  first FADE_OUT_TIME ms of uptime (idle silently never started — the exact window the rig
  runs in). Confirmed from the console's `Transition to idle [style=eden]` line rather than
  assumed, then a HID burst while the time-sliced screensaver owns the keycaps. Needs the
  console.
- [x] **reboot persistence** (runner-level) — set the idle style, flush it with **cmd 26**
  (`save_all_dirty`, previously untested), power-cycle the master over the RUN pin, and read
  it back. The only check in the suite that survives a reboot, and the only one that can
  see the suspend-only EEPROM model failing.
- [x] **firmware version cross-check** (cmd 0x43) — `FW_UP_GET_VERSION` was read and thrown
  away; the staged `.bin` carries the same GET_ID literal the firmware answers with, so the
  two are compared. Catches a flash that silently did not take, which is otherwise invisible
  (every test build reports the same `FW_VERSION`, and the UF2 filenames carry no version).

## Backlog

### Master / slave topology

- [ ] **per-side image reaches the correct half** — the HIL role is fixed at compile time
  (`POLYKYBD_HIL=left` master / `=right` slave in `polykybd.c`), **not** by EE_HANDS, so the
  only handedness risk is the rig flashing the images to the wrong sides. `single master
  enumerates` already catches a same-image-to-both pair; a stronger guard would flash the
  images *swapped* (left image → right half) and confirm the sole master moves to the other
  half. Runner-level (needs `FlashController` control), not a `(raw, log)` test. (The old
  EE_HANDS-swap idea does not apply — the HIL build ignores EE_HANDS.)
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

- [ ] **compressed ROI** (cmd 18 with the 0x80 flag) — the ROI test sends an *uncompressed*
  region; the compressed variant takes a different core1 route again. Cheap to add now that
  `_roi_header` exists (it already takes `compressed=`), it just needs an RLE stream sized to
  the region rather than to a whole overlay.
- [ ] **MRU program-switch refresh** — replay a multi-chunk overlay mapping (>24 pairs, so ESC
  lands in a later chunk) and confirm the final rendered state syncs (the `DISPLAY_OVERLAYS` /
  `OVERLAY_SYNCED_STATE_FLAGS` fix). Hard to assert purely over HID — needs a firmware debug
  read or a camera.

### Robustness / soak

- [ ] **brightness deferred-write soak** — set brightness, wait >5 s for the housekeeping
  `brightness_save_if_pending()` flush (the RP2040 wear-levelling consolidation window), then
  confirm the slave is still responsive. The valid-set + immediate-liveness half is already in
  `test_set_brightness`; this adds the timed wait that targets the slave-unresponsive regression.
- [ ] **more state through the reboot** — `reboot_persistence` carries exactly one value (the
  idle style). Language, glyph script and the default layer ride different EEPROM blocks and
  different flush paths; each is a small extension of the same runner-level test, and the
  brightness one needs a read-back command that does not exist yet.
- [ ] **promote the animation/idle latency to an assertion — THERE IS NOW A BASELINE, and
  taking it revealed that `test_replay_animation`'s third assertion is currently inert.**
  Both tests *report* HID round-trip latency and assert only the freeze signature. The first
  extended run (qmk run #805, `workflow_dispatch`, 2026-08-20, FW 0.15.7) published these,
  and the two populations are an order of magnitude apart:

  | window | GET_IDs | median | p95 | max |
  |---|---|---|---|---|
  | intro animation (cmd 31, deliberately **unsliced**) | 20/20 | **96 ms** | 97 | 97 |
  | Eden screensaver (idle, **sliced**) | 30/30 | **4 ms** | 7 | 10 |
  | idle, no animation (the stress burst's floor) | 50/50 | ~3 ms | — | — |

  The firmware's own line for the same frame was `Eden idle 177ms (frame 109ms, worst slice
  5ms)`, i.e. the ~3 ms slice budget is being honoured and the host sees it.

  ⚠️ **`ANIM_RECOVERED_MS` (200) sits ABOVE the in-animation latency (96 ms), so the
  "the animation ends by itself" assertion cannot fail.** The test declares the intro
  finished after 3 consecutive replies under 200 ms — but the animation *itself* answers in
  ~96 ms, so the streak is satisfied while it is still running. On run #805 the test logged
  *"animation finished and the master is responsive again after 2.6s (expected ~14s)"* while
  the firmware logged `Eden done (14218ms)` **eleven seconds later**; the intro was in fact
  still playing when the next test started. A non-terminating animation that stayed
  responsive would pass this identically — which is the "reads as coverage" failure the
  version gates exist to avoid, in the one assertion whose whole point is non-termination.
  The fix is now cheap because the populations are separated: drop `ANIM_RECOVERED_MS` to
  ~30 ms (well above the ~3 ms idle floor, well below the ~96 ms animation cost). Do that
  first; only then is the median worth turning into a threshold, which is what would catch a
  slice regression (the shipped "Eden doesn't wake on the first keypress" bug) rather than a
  total wedge.

  ⚠️ Take any *new* baseline from a run whose burst reports **0 transient HID retries** — see
  the retry-accounting note in `CLAUDE.md`, or the max is the harness's timeout, not the
  device's latency.
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
- [x] **HID console (CONSOLE_ENABLE)** — ⚠️ this said "currently off in the PolyKybd build", and
  that has been stale for a while: `split72/keyboard.json` sets `"console": true`, so the rig has
  been receiving firmware diagnostics all along and merely echoing them. `station/console_log.py`
  now taps them (reassembling the report-sized fragments into lines) and tests read them back via
  a `"needs_console": True` gate that SKIPs when the console did not come up.
  ⚠️ Most firmware diagnostics are gated on `debug_enable`, which defaults **false** — including
  `Failed to sync … for transaction X` and `Bridge sync retry`. Do not design a check around
  those lines; the `Split link:` summary is deliberately ungated, which is why the link health
  test reads the counter and not the failures.
