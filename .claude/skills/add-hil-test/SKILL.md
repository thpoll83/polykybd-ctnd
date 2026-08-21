---
name: add-hil-test
description: Add a hardware-in-the-loop test to the PolyKybd rig suite (station/hil_tests.py) — pick the right gate and cost tier, write the (raw, log) test, mirror any host-side packer through the firmware's own decoder, add offline unit tests, and verify the count actually changed. Use when asked to "add a HIL test", "cover cmd NN on the rig", "test <firmware feature> on hardware", "the rig should check X", or after adding a HID command / PROTOCOL_VERSION that nothing on the rig exercises. NOT for diagnosing a red HIL check (that is diagnose-hil-failure), NOT for firmware perf numbers (measure-firmware-perf), and NOT for host-only logic that needs no keyboard.
---

# Add a HIL test

The rig suite is `polykybd-ctnd/station/hil_tests.py`. A test is a dict in `TESTS`
with a `name` and an `fn(raw: RawHID, log: Callable[[str], None]) -> bool`.
Everything hard about adding one is in the **gates**, the **cost tier**, and in
not writing a test that passes for the wrong reason.

## 1. Decide what the test can actually observe

Before writing anything, answer: *what would this test do if the feature were
broken?* The rig has no camera, so it cannot see a keycap. What it can observe:

| Observable | How |
|---|---|
| a reply byte (ACK `.` / NACK `!` / a value) | `raw.send(...)` |
| a multi-report reply | `raw.send_and_read_all(...)` |
| **liveness** after a silent command | send the burst, then a `GET_ID` |
| a firmware log line | the console tap — **needs `"needs_console": True`** |
| state surviving a power cycle | runner-level, `FlashController.reset()` |

⚠️ **A silent command can only ever be a liveness guard.** The overlay uploads and
cmd 33 mapping are no-reply by design and nothing reads the mapping back, so the
honest assertion is "decoding this report did not wedge the master", not a
round-trip. Say so in the docstring; do not dress it up as verification.

⚠️ **If the assertion needs a console line, gate it `needs_console`.** Without the
gate, a run where the console did not come up asserts nothing and reports a green
it did not earn. Most firmware diagnostics are `debug_enable`-gated (default
false) and never appear on the rig — check the gate in the firmware source before
designing around any line. `Split link:` is deliberately ungated.

## 2. Pick the gate

```python
{"name": "...", "fn": test_x, "min_protocol": 12}   # SKIP on older firmware
{"name": "...", "fn": test_x, "min_fw": "0.15.2"}   # not tied to a protocol bump
{"name": "...", "fn": test_x, "xfail": "needs host release"}
```

Version gates **fail open** — an unreadable `GET_ID` runs the test rather than
skipping, because a real fault should surface. Gate on `min_protocol` whenever the
command was introduced by a `PROTOCOL_VERSION` bump; otherwise the rig goes red on
old firmware for a feature it correctly does not have.

## 3. Pick the cost tier

```python
{"name": "...", "fn": test_x, "tier": TIER_EXTENDED}
```

⚠️ **Tier is about COST, never confidence.** Extended = slow (a 14 s animation, a
10 s fade) or disruptive (a power cycle). Anything *unreliable* belongs in
`docs/FUTURE_TESTS.md` until it is trustworthy — otherwise "extended" becomes
where failing tests go to be forgotten. Unlike the version gates this one **fails
closed**: a caps dict that never heard of tiers skips, because the cost is the point.

Extended tests run on `--extended` / `HIL_EXTENDED=1`, the `hil-extended` PR label,
`[hil-extended]` in a pushed commit message, `workflow_dispatch`, or the touch UI
toggle.

## 4. Write it

Mutate-and-restore in a `finally` if the test changes device state. Put
side-effecting tests late in `TESTS` (most disruptive last). Log what you sent and
what came back — the log is the whole diagnostic when it fails on the rig.

⚠️ **`raw.send()` RETRIES by re-writing the request.** That is safe only because
the commands are idempotent — `GET_ID` is the exception (it consumes the one-shot
fresh-boot marker), so any read that observes a **one-shot side effect** must pass
`attempts=1`. Do not "centralise" this by special-casing a command id inside
`send()`: six of the seven `GET_ID` call sites *depend* on the retry.

⚠️ **Mirror a host-side packer through the FIRMWARE's decoder, not by eye.** If the
test packs bits (mapping values, an ROI header, an RLE stream), re-implement the
firmware's decode in the offline unit tests and round-trip through it. Every bug
these commands shipped with was in the bit arithmetic.

## 5. Register and verify the count changed

```python
TESTS = [ ..., {"name": "my new thing (v12)", "fn": test_my_new_thing, "min_protocol": 12} ]
```

Add offline unit tests under `tests/` (no hardware — that is where the packers and
the pure classifier functions get pinned), then:

```bash
cd polykybd-ctnd
python -m unittest discover -s tests -p "*_test.py" 2>&1 | tail -3
```

⚠️ **Judge the run by the test COUNT changing, not by "still green".** A test
method appended after a file's trailing `if __name__ == "__main__":` block parses,
never runs, and never fails. Same family: a `flash_and_test` caller that forgets
`tests=TESTS` returns `{"passed": True, "results": []}` — **a "passed" with an
empty `results` list is not a pass.**

## 6. Sanity-check on real hardware

The suite only runs against a keyboard, so the first real execution is a PR's HIL
check. Read the job log for the `[test] PASS:` line and the values it printed —
the numbers are how you find out the test measured what you thought. Confirm the
tier line (`suite tier: …`) if the test is extended, or a default-tier run silently
skipped it.

## Pitfalls

- **A green suite that asserted nothing** is the recurring failure here — the empty
  `results` bug, the un-run appended method, the missing `needs_console` gate. Every
  step above that says "verify" exists because one of them shipped.
- **Don't fail on an isolated no-answer.** The master has a real post-overlay deaf
  window; `classify_get_id_stress` fails on a freeze *signature* (a consecutive run),
  not the first miss.
- **The rig runs `/opt/polykybd-ctnd`, not a fresh checkout** — CI force-syncs it to
  `origin/main` before the run, so a test must be **merged to `main`** to execute.
- **Don't assert a latency threshold without a published baseline.** Report it, read
  a few runs, then promote — see `docs/FUTURE_TESTS.md`, which carries the measured
  Eden/animation numbers and the trap that the reported `max` absorbs the harness's
  retry timeout.
