# HIL suite — how a test is written, and why each one asserts what it does

Extracted from `CLAUDE.md` 2026-09-14. The prose is unchanged; only heading levels
and relative links were adjusted to suit a standalone file.

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
  - The two **PRC overlay** tests (cmd 41, `min_protocol: 19`) cover the
    Predictive Range Coding upload. `test_prc_overlay_keeps_master_alive` is a
    **liveness guard**, like cmd 33: two real records in one report (KC_A decoded
    on the master, KC_P bridged to the slave on the compressed transaction with
    `PRC_BRIDGE_FLAG`), a full 72x40 box with an empty payload (the longest decode
    one record can ask for), and the three records the parser refuses. It cannot
    tell a refusal from a record decoded into garbage, so
    `test_prc_malformed_record_is_refused` (`needs_console`) reads the firmware's
    `Warning: malformed PRC record at byte N` line (a `uprintf`, not debug-gated)
    for each malformed record, and asserts a valid report produces no PRC warning.
    Whether the decoded PIXELS are right is not the rig's question: the firmware
    unit test `make test:polykybd_prc_codec` decodes the host's golden vectors
    and compares all 360 bytes. The payloads here are copied from those vectors,
    and `tests/hil_tests_test.py` pins the rig's packer against a host-packed
    record and a port of `prc_parse_record()`.
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
- [x] **A firmware crash anywhere in the run now FAILS the run — `test_no_crash_record`.**
  The firmware announces a HardFault / unhandled exception / watchdog reboot as a
  `crash: side=<master|slave> kind=… pc=… fw=…` console line on its next boot (qmk
  `base/crash_record.*`, protocol v16 cmd 39). Until this the rig echoed it into the
  log like any other line, so a crash inside a test read as a flake or a dead reply.
  The scan runs `needs_console`, DEFAULT tier, **last among the default tests** so
  its window covers them all, and reads `TAP.find_all("crash: side=")` from the start
  of the session — the slave's record included, since the master pulls and prints
  it. `test_crash_record_command` (`min_protocol: 16`) reads both halves over cmd 39,
  fails on a **fresh** record (bit1 — the boot before this one crashed), notes an
  archived one, checks the unknown-sub-op NACK and exercises the clear so the next
  run starts clean. ⚠️ No **deliberate** fault has been driven through the naked
  handler on the rig yet; a probe that faults on purpose is the way to close that.
  - ✅ **Both tests earned their keep on their FIRST run (33809919200, 2026-09-03)
    by catching a phantom, not a crash.** Both halves reported a fresh
    `kind=watchdog … n=3 reason=0x12` straight after the rig's flash: the bootrom's
    reboot after a UF2 copy is itself a watchdog reboot, so `WATCHDOG.REASON.TIMER`
    reads set on the first boot after every BOOTSEL flash, and the pre-fix firmware
    counted each rig flash as a hang — two more and it would have halted the rig in
    `wfi`. Fixed firmware-side (qmk#271: a timeout counts only while the SDK's
    `watchdog_enable()` scratch marker is present). That run also drove the whole
    reporting chain end to end — boot capture, console line on both halves, the
    slave pull over the split link, cmd 39 read and clear — so "never fired" is no
    longer true of anything but the fault handler itself.
  - **The slave keeps its archived record across a reflash** — the archive is a
    flash sector a UF2 copy does not touch — so the run after a red one logs
    `note: the slave half holds an older (archived) crash record — clearing it`.
    That is the test doing its job (an archived record is a note, a fresh one is a
    failure), not a second crash; the master's had already been cleared by the
    previous run's clear sub-op.
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
- [x] **The apply round-trip asserts the SLAVE too, not just the master.** An
  apply reboots BOTH halves — the slave copies its own staged image and resets a
  few seconds after the master — and until 2026-09-03 nothing looked at the link
  afterwards: the suite's split-link soak runs long *before* the update, and the
  only post-apply assertion was that the master re-enumerated on the right
  version. A field report had exactly that gap's shape: the master came back
  perfectly while the link went silent, `transport_fail` climbing on **201 of 201**
  frames with `crc_err=0` — the slave answering nothing rather than answering
  corrupt. Every assertion the test made would have passed. It now ends by
  bridging the same cmd-21 soak and measuring the counters
  (`hil_tests.measure_split_link`, shared with `test_split_link_health` so the two
  cannot disagree about what a fault is).
  - ⚠️ **On THIS RIG the apply necessarily destroys the slave, and that is
    STRUCTURAL — not a firmware fault, and not the field bug it resembles.** The
    slave installs its own STAGED image, and the staged bytes are the ones the
    master bridged during CHUNK, i.e. the **master's** image. On a real keyboard
    that is exactly right: both halves run one identical image and the role is
    decided at runtime by VBUS. Here the halves run **different** images by
    construction (`POLYKYBD_HIL=left`/`right`), so the slave applies the
    left/master image, stops calling `usb_disconnect()`, and comes back as a
    **second master** — no slave, so 100% `transport_fail`. Measured on run
    33733020495: **12930 of 12930 frames, `crc_err=0`**, which is the same
    signature as the 2026-09-03 field report and has a completely different
    cause. Read this before concluding the rig has reproduced a slave failure.
    - **It is observed, not assumed:** two enumerated Raw HID interfaces IS both
      halves being master (the signal `test_single_master` already uses), so the
      two cases stay distinguishable — one master plus a dead link is a REAL
      slave failure and still fails the run; two masters is reported and not
      graded.
    - ⚠️ **So the fwapply tier cannot answer "did the slave survive its own
      apply?" on this rig at all**, and no amount of assertion strength changes
      that. It is a property of the per-side images, not of the check.
    - ✅ **The answerable half of that question IS now asked —
      `post_apply_split_link` re-flashes the slave and measures.** It runs after
      a passing apply whose own link check came back UNVERIFIED, re-flashes
      `*_hil_right.uf2` over BOOTSEL, and bridges the same soak. What it proves
      is deliberately narrower than the name suggests: **the applied master
      image can bring a split link back up with a fresh slave** — a master that
      returned subtly wrong (broken transport, mis-sized shared-memory struct,
      dead PIO) fails here and passes every other assertion in the round-trip.
      That is the other half of the 2026-09-03 field report, where the master
      enumerated perfectly and the link stayed silent. It also restores the
      rig's two-image invariant, which an apply otherwise leaves broken until
      the next run's flash.
      - ⚠️ **SETTLE before measuring, or the reconnect is graded as the fault.**
        A master that exhausted `SPLIT_MAX_CONNECTION_ERRORS` (200 here)
        throttles to one attempt per `SPLIT_CONNECTION_CHECK_TIMEOUT` (500 ms)
        and zeroes its error count on the first success
        (`quantum/split_common/split_util.c`, `transport_master_if_connected`) —
        so the link does come back by itself, but every failing attempt before
        it is a real `transport_fail`, and the soak tolerates only ~1% of ~450
        frames. Hence `POST_APPLY_LINK_SETTLE_S`.
      - ⚠️ **Two masters AFTER the re-flash is a RIG fault, not a firmware one**
        — the flash did not take, so there is no link to measure and grading one
        would report the rig's own failure as the applied image being unable to
        talk to its slave. It fails, and says which.
      - ⚠️ **`_masters_after_apply`'s enumeration-failure sentinel means OPPOSITE
        things at its two callers, which is why it is a parameter.** It reports
        `unknown` when it cannot enumerate at all; for the apply round-trip that
        must read as **1** (a hiccup sends the run down the *measuring* path
        rather than buying it a free pass), and for the re-flash check it must
        **not**, because there the count is the evidence that the slave came back
        as a slave. Reading it as a confirmed single master lets an unresolved
        USB state be measured and its transport failures land on the applied
        firmware — failing the release-gating job for a rig fault. The re-flash
        check passes `MASTERS_UNKNOWN` and **SKIPs**, the same tri-state
        reasoning as `LINK_NO_SUMMARY`. Raised by Greptile on ctnd#90.
        - ⚠️ At the apply call site every branch tests `> 1`, so `1` and `-1`
          are behaviourally identical there and a mutation between them is
          **inert, not escaped**. The boundary mutation is `2`, which does make
          it stop measuring and is caught.
      - ⚠️ **OFF by default — `--reflash-slave` / `HIL_RESLAVE=1`, and that is a
        RELEASE-GATE decision, not a cost one.** The obvious wiring is "it is
        extended-only, and the apply round-trip is extended-only, so it rides
        along" — but the fwapply job passes `--extended` and runs on **every
        merge to `PolyKybd`**, so "extended" and "every merge" are the same set
        here. And this check lives INSIDE that job, whose conclusion
        `require_fwapply_run.py` requires to be `success` before a release can
        publish (`covered_by()`). So anything that can fail here can refuse a
        release — which is not a thing to switch on for code that has never
        executed against the rig. Prove it with a dispatch first, then decide.
        ✅ **Proven 2026-09-03**, dispatch run #987 (`tier: fwapply`, the first
        execution with `HIL_RESLAVE=true`): the apply reported UNVERIFIED as
        designed, the re-flash fired, and the soak came back `crc_err +0,
        transport_fail +0` over 200 frames against a tolerance of 2 — PASS, with
        the settle comfortable rather than marginal (master ready in 3 probes).
        **Cost measured at ~39 s**, not the ~25 s a flash alone suggests: BOOTSEL
        + picotool 12 s, reboot-to-ready 8 s, then `POST_APPLY_LINK_SETTLE_S` and
        `_masters_after_apply`, which has **no early exit for the expected count
        of 1** and so always spends its full `_MASTERS_SETTLE_S`. ⚠️ That figure
        read **~45–55 s** here until it was measured — an estimate assembled from
        the constants, and wrong by a third. Take the number from a run.
        - ⚠️ **One green run is not a base rate — do NOT read "proven" above as
          "flip the default".** The proof establishes that the path works; it
          says almost nothing about how often it fails, and the asymmetry is
          what decides this: a flake costs a **refused release** (recovery is a
          manual dispatch), while the coverage bought is narrow — the apply
          round-trip already catches a master that fails to come back or reports
          the wrong version, so this adds specifically *a `fw_staging` copy
          corruption that spares USB/HID and breaks the split transport*. Worth
          having; not worth a false refusal on one sample. It also lands
          downstream of the **unexplained post-apply settle anomaly** (slow in
          4 of 5 runs, worst ~365–450 ms, against 15 probes after a plain power
          cycle), so `POST_APPLY_LINK_SETTLE_S = 5` is validated by exactly one
          observation in a neighbourhood nobody has explained.
        - **Accrue the evidence WITHOUT touching the default**: `HIL_RESLAVE` is
          already true for the `hil-fwapply` label and the `[hil-fwapply]` commit
          marker, so any PR touching the apply path can opt in and costs nobody
          else anything. **Flip it after ~5 clean executions**, or earlier if you
          want the coverage now and accept a manual dispatch as the recovery. A
          flake found that way is a red PR check; the same flake found after the
          flip is a blocked release.
        - **Open question, deliberately not a plan**: decoupling the check from
          the release gate would make the default uncontroversial, but a check
          that cannot fail is one people learn to ignore (the reasoning the perf
          job accepts and a correctness check should not). Nobody has thought
          this through — do not treat the sentence as a design.
      - ⚠️ **A ctnd branch CANNOT be proven on the rig before it merges** — every
        rig job hard-checkouts ctnd `main` (`git checkout -q -f -B main
        origin/main`, five times over in `qmk-test.yml`), so a dispatch runs
        `main`'s station code no matter which branch you dispatch on. That is
        why the opt-in exists at all rather than a "try it on the branch first"
        plan: the only sequence that works is **merge off → dispatch on →
        decide**.
      - **`should_reflash_slave()` is the gate, extracted and pure** for the
        reason `classify_link_health` and `decide_stale_bundles` are: a one-line
        switch nobody exercises is exactly how the first cut of the post-apply
        link check shipped inert. It needs the apply's `link` outcome, which is
        why that result dict carries one.
  - ⚠️ **`measure_split_link` is TRI-state, and the third value is what keeps the
    check honest.** `LINK_NO_SUMMARY` means the console produced no `Split link:`
    line, i.e. the measurement did not happen — reporting that as a dead slave
    would turn a console problem into a false red on the firmware. The
    distinction is sound because the **master** prints that summary from
    `send_to_bridge` regardless of whether the slave answers, so a genuinely dead
    slave still yields two summaries with `transport_fail` climbing. The graded
    test carries `needs_console` and so may treat anything but `LINK_OK` as a
    failure; the runner cannot, and says "the SLAVE IS UNVERIFIED for this run".
- [x] ⚠️ **The console is STOPPED before the whole firmware-update section, so
  anything after that point reads a `TAP` nothing is feeding — and that, not the
  reader dying, is why the apply test's banner check had never fired.**
  `flash_and_test` calls `self._console.stop()` before the `--bin` stage+verify
  and the `--apply-bin` round-trip, deliberately: `BEGIN` tears USB down during
  the master's staging erase. So `TAP.wait_for("last self-apply COMPLETED")`
  could not possibly match, and both the 0.17.4 and the 0.18.0 runs printed *"no
  apply banner seen … the re-enumeration check above still passed"* and went
  green on the weaker assertion.
  - ⚠️ **This is the trap for anything added to that section**, and the first cut
    of the post-apply link check walked straight into it: it took the run-start
    "did the console come up" flag and passed it through, so the measurement
    would have returned `LINK_NO_SUMMARY` on **every** run — present, passing,
    and asserting nothing, i.e. exactly the non-coverage the check was written to
    remove. Caught by Greptile on ctnd#86, not by the suite; the test that pins it
    now asserts the console is **live at the moment `measure_split_link` is
    called**, not that the wiring reads correctly.
  - `_reattach_console()` re-opens it after the reboot, which is necessarily
    *after* boot — so a banner printed during boot is missed **by construction**
    and its absence says nothing about the firmware. The old note offered "the
    console did not come up, or this firmware predates the in-flash apply log";
    neither was the reason, and that reading closed the question for months.
- [x] **The console reader also never survived a re-enumeration**, which is a
  second, independent defect: `HIDConsole` opened one hidraw handle at start, and
  after any reboot that node is gone, every later `read` raises, and the old loop
  just slept on the exception — silently, with nothing logged. `HIDConsole._loop` now reopens (after
  `_REOPEN_AFTER_ERRORS` consecutive failures, so a momentary USB hiccup does not
  make it fight `RawHID`, which opens its own handle per call). Lines printed
  while the device is away are lost and always will be — the firmware drops
  console output nobody is draining.
  - ⚠️ **`stop()`'s join is TIMED, so it can return with the reader still alive —
    and closing the handle anyway is the exact use-after-free the
    join-before-close ordering exists to prevent** (SIGABRT, exit 134, which
    once turned green HIL runs red). The 2 s margin rested on "the loop only
    ever waits 200 ms"; the **callback runs on that same thread** (in the touch
    UI it is a SocketIO emit, an unbounded wait), and the reopen sleeps and then
    calls `hid.enumerate()`/`hid.Device()` at the precise moment the device is
    re-enumerating. So when the thread is still alive the handle is
    **abandoned, not closed** (`abandoned_handles` counts it): one leaked fd
    until process exit beats aborting the process, and the reader is a daemon
    thread so it cannot hold the process open. Found by CodeRabbit on ctnd#86 —
    a latent hazard whose guard rested on a premise the reopen weakened.
  - **Generalise: a best-effort diagnostic that fails silently reads as evidence
    of absence.** The apply test's log line offered "the console did not come up"
    as one of two explanations and nobody checked which; the *other* explanation
    (a firmware predating the in-flash apply log) was plausible enough to close
    the question for months.
- **Post-apply the master does not settle, reproducibly — and that is REPORTED,
  not failed.** Measured on two consecutive runs (merge run 33721791934 on 0.17.4,
  dispatch 33726949359 on 0.18.0): after an APPLY the master answers GET_LANG in a
  uniform ~450 ms for the whole 30 s settle window — **66 probes, worst 446 ms**,
  byte-identical across both — where the same master after an ordinary RUN-pin
  power cycle settles in 15 probes. ⚠️ **Do not theorise a mechanism from that.**
  ~450 ms is in split-transaction retry territory and nothing on the rig measures
  it; this file's history is full of confident mechanisms that turned out wrong.
  Record the measurement, and note that the difference is between the two *reboot
  paths*, not between two firmware versions.

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
- **`boot loop (v22 cmd 43)` reboots N times and stops at the first boot that went
  wrong** (`test_boot_loop`, EXTENDED, `min_protocol` 22). The rig's twin of the
  host's boot-loop diagnostic. Each round: cmd 43 (the firmware ACKs before it
  resets), a single-attempt GET_ID polled through re-enumeration until it answers,
  then cmd 39 for the master and, for up to 8 s, the slave. A deliberate reboot never
  archives a record, so a FRESH one on either half fails the run and is printed
  decoded; so does a reboot that does not come back within 60 s. It hunts the
  intermittent stall in the 63%..75% boot window that the late-boot guard recovers
  (fw 1.3.2 field record `1:0x16e1`).
  - **`HIL_BOOT_LOOP_ROUNDS`** sets N (default 20, clamped to 1..50).
  - ⚠️ **It runs on every merge to `PolyKybd`**, because the fwapply job passes
    `--extended` (above). At roughly 5–15 s a round, the default 20 costs a few
    minutes there.
  - **It sits after the crash-record test**, which clears the archive, so an older
    record cannot be read as this run's.
  - **A `.` answer counts as "back" only after the interface went away at least
    once**: the `*` can be lost to a read, but a master that never dropped off USB
    did not reboot, and that fails.
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

