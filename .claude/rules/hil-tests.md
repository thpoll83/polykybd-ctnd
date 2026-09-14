---
paths:
  - "station/hil_tests.py"
  - "station/test_runner.py"
  - "station/probe.py"
---
# Editing the HIL suite

Full notes: `docs/HIL_SUITE_NOTES.md`. Four things that are easy to get wrong here:

- ⚠️ **`RawHID.send()` retries by re-writing the request.** Safe for every command
  except `GET_ID`, which consumes the one-shot fresh-boot marker — a retry then
  returns correct-looking data for a marker already cleared, so the runner grades a
  rig hiccup as a FAIL (wrong data) instead of a WARN (timeout). That one call is
  pinned `attempts=1`. Do **not** special-case GET_ID inside `send()`: six of its
  seven call sites depend on the retry.
- ⚠️ **A test that asserts nothing must SKIP, not pass.** `needs_console` gates the
  console readers; `min_protocol` / `min_fw` gate version-dependent checks. A check
  that runs with nothing to read reports a green it did not earn.
- ⚠️ **Tier is about COST, never confidence.** `TIER_EXTENDED` is for slow or
  disruptive; anything flaky belongs in `docs/FUTURE_TESTS.md`.
- ⚠️ **Count `TESTS` rather than trusting prose.** Any new caller of
  `flash_and_test` must pass `tests=TESTS` — a `"passed": True` with an empty
  `results` list is not a pass.
