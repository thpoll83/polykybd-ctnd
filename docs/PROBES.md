# Ad-hoc rig probes (`--probe`)

Extracted from `CLAUDE.md` 2026-09-14. The prose is unchanged; only heading levels
and relative links were adjusted to suit a standalone file.

## Debug loop: running an ad-hoc probe on the rig (`--probe`)

The graded suite answers fixed questions. A **probe** answers a one-off one —
*"flash this build, send these commands, show me what the firmware printed"* — so
a firmware bug can be chased on the rig **without a human flashing a `.bin` and
pasting a console log back**. That round trip used to be the slowest part of every
hardware bug, and it is what the HID-apply brick (qmk#258) was diagnosed through.

A probe is a Python file in the **firmware** repo under
`keyboards/polykybd/tools/hil_probes/`, so the probe and the firmware it probes
are one commit on one branch:

```python
NAME = "what this is looking for"     # optional
MIN_PROTOCOL = 15                     # optional — same gate keys as a test
NEEDS_CONSOLE = True                  # optional

def probe(raw, log):
    reply = raw.send(bytes([0x50, 0x06]))
    log(f"GET_ID -> {reply!r}")
    return reply is not None
```

`station/probe.py` turns it into the **same test dict the suite uses**, so it
inherits the per-side flash, the readiness gates, the console tap, the capability
gates and the pass/fail reporting. That reuse is the point: a parallel code path
would drift from the suite's hard-won startup sequencing (`wait_for_master_ready`
/ `settle_master` are load-bearing, see the stale-rig warning in `CLAUDE.md` § Development workflow).

```bash
python -m station.test_runner --left … --right … \
    --probe fw_apply --firmware-dir /path/to/qmk_firmware
```

- **A probe REPLACES the suite** unless `--probe-with-suite` is passed. A debug
  loop wants the probe alone; the suite costs rig time on every iteration.
- **It is resolved and imported BEFORE the flash**, so a typo'd name or a probe
  that raises at import costs a message rather than a flash-and-enumerate cycle.
- ⚠️ **The containment check in `resolve_probe` is NOT a security boundary and
  must not be described as one.** By the time the runner executes, CI has checked
  out the whole firmware repo and the build jobs have already run code from it —
  a branch that wanted to run something else on the rig never needed a probe. What
  keeps an untrusted branch off the rig is the workflow's fork gate (HIL-2 in
  `docs/SECURITY_AUDIT.md`). The check is operational: it keeps "what can be a
  probe" explicit and stops a mistyped path importing something for its side
  effects.

⚠️ **The console cannot see the flash window.** QMK drops console output nobody
drains, and during a flash nothing does (the host-side note is in
`PolyKybdHost/CLAUDE.md`) — so a probe can observe before and after an update, not
during it. A gap in the `[qmk]` timestamps spanning a flash is expected, not a
symptom.

✅ **A brick is self-recovering here**, because the rig asserts BOOTSEL over GPIO
and BOOTSEL/UF2 bypasses `fw_staging` entirely. That is what makes the rig the
right — and the only — place to exercise the firmware-apply path: the failure mode
being guarded against cannot strand the hardware.

