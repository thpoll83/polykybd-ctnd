# SPDX-License-Identifier: GPL-2.0-only
import os
import sys
import time
from typing import Callable

from .flash import FlashController
from .hid import HIDConsole, RawHID
from .fw_update import stage_and_verify
from .hil_tests import parse_device_caps, skip_reason

# Raw HID display-off control command — mirrors the firmware dispatcher in
# keyboards/handwired/polykybd/hid_com.c (case 24 / 0x18). A command report is
# data[0]='P' (channel marker) then data[1]=command id; the firmware replies "P\x18.".
POLY_CHANNEL     = 0x50  # 'P'
CMD_GET_ID       = 0x06  # device identity string — advertises fw + protocol version
CMD_GET_LANG     = 0x07  # read current language — readiness probe (no side effects)
CMD_DISPLAY_OFF  = 0x18
ACK              = ord(".")

# VIA-style "reset dynamic keymap" report. Unlike the 'P'-channel commands above,
# this is a bare report whose first byte is the VIA command id (no 'P' marker):
# the firmware routes data[0]==0x06 to legacy_command_kb(), which calls
# dynamic_keymap_reset(), bridges the reset to the slave over the split link, and
# echoes the request back unchanged (no "P<cmd>." ACK). Same id PolyKybdHost uses
# in PolyKybd.reset_dynamic_keymap(). See keyboards/handwired/polykybd/hid_com.c.
VIA_DYNAMIC_KEYMAP_RESET = 0x06


class TestRunner:
    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.status = "idle"
        self._flash = FlashController()
        self._console = HIDConsole()
        self._raw = RawHID()
        self._caps = None  # device caps (fw/protocol), read lazily for the gate

    def flash_and_test(self, left_uf2: str, right_uf2: str, tests: list = None,
                       bin_path: str = None) -> dict:
        # HIL images are built per side (POLYKYBD_HIL=left/right). If either path
        # looks like a HIL image, enforce the per-side contract before touching
        # hardware — flashing one master image (or a swapped pair) to both halves
        # makes both enumerate as master, the failure this build exists to prevent.
        # Production deploys (non-_hil names, often the same image to both halves)
        # are intentionally not constrained.
        left_name, right_name = os.path.basename(left_uf2), os.path.basename(right_uf2)
        if ("_hil" in left_name or "_hil" in right_name) and not (
            left_name.endswith("_hil_left.uf2") and right_name.endswith("_hil_right.uf2")
        ):
            raise ValueError(
                "HIL runs need per-side images: pass *_hil_left.uf2 to --left and "
                f"*_hil_right.uf2 to --right (got left={left_name!r}, right={right_name!r})"
            )
        results = []
        try:
            self.status = "flashing"
            # Flash right first — it communicates via split cable, not USB HID
            self._flash.flash("right", right_uf2, self.log)
            self._flash.flash("left",  left_uf2,  self.log)

            self.log("[runner] waiting for keyboard to enumerate...")
            time.sleep(3)

            # The QMK HID console (debug log streaming) is diagnostic only — it
            # requires CONSOLE_ENABLE in the firmware. Current PolyKybd builds do
            # stream [qmk] lines, but a build with it off, or any transient open
            # failure, must not fail the test run — so treat startup as
            # best-effort. (When it IS enabled the reader thread is live, which is
            # why HIDConsole.stop() must join it before closing the handle.)
            console_started = False
            try:
                self._console.start(lambda msg: self.log(f"[qmk] {msg}"))
                console_started = True
            except Exception as exc:
                self.log(f"[runner] HID console unavailable (continuing without it): {exc}")

            self.status = "testing"
            # Wait out the post-cold-flash window during which the master blocks
            # its main loop on the initial 72-keycap render + split-sync and
            # times out HID reads, so the marker-sensitive GET_ID tests don't run
            # inside it (see _wait_for_master_ready).
            self._wait_for_master_ready()
            # Clear any dynamic keymap left in the keyboard's EEPROM by a previous
            # firmware whose layer layout differs from the freshly-flashed build.
            # A UF2/HID flash does not erase the wear-leveled EEPROM, and the
            # firmware has no build-date magic that auto-resets it, so a stale
            # keymap survives the flash and can make the rig behave as if it were
            # running the old layout. Reset it before the suite so every run starts
            # from the compiled defaults. Best-effort — a failure here is logged
            # but does not abort the run.
            self._reset_keymap()
            # The keymap reset triggers a master->slave sync; wait for that (and
            # the post-cold-flash split-sync settling in general) to clear before
            # the graded suite, so no test runs while the master's loop is briefly
            # stalled in split retries. Best-effort.
            self._settle_master()
            for test in (tests or []):
                name = test.get("name", "unnamed")
                # Capability gate: skip (do NOT fail) a test the flashed firmware
                # can't yet satisfy — e.g. a protocol-v3 command checked before
                # the v3 firmware is deployed. The device advertises its
                # fw/protocol version in GET_ID, so a gated test un-skips itself
                # automatically once a firmware meeting the requirement is
                # flashed. Caps are read lazily (only when a gated test is first
                # reached) which is after test_fresh_boot_marker has consumed the
                # one-shot '*' marker, so the gate's GET_ID doesn't disturb it.
                if test.get("min_protocol") is not None or test.get("min_fw"):
                    reason = skip_reason(test, self._device_caps())
                    if reason:
                        results.append({"name": name, "status": "skip", "reason": reason})
                        self.log(f"[test] SKIP: {name} ({reason})")
                        continue
                # An xfail test is known to fail until some change lands that
                # isn't visible in GET_ID: a FAIL is tolerated (XFAIL), an
                # unexpected PASS is flagged (XPASS) so the marker gets removed.
                xfail = test.get("xfail")
                try:
                    passed = bool(test["fn"](self._raw, self.log))
                    status = ("xpass" if passed else "xfail") if xfail else (
                        "pass" if passed else "fail")
                    rec = {"name": name, "status": status}
                    if xfail:
                        rec["reason"] = xfail
                    results.append(rec)
                    self.log(f"[test] {status.upper()}: {name}"
                             + (f" (expected to fail: {xfail})" if xfail else ""))
                except Exception as exc:
                    # An exception is a failure; under xfail it's still tolerated.
                    status = "xfail" if xfail else "fail"
                    rec = {"name": name, "status": status, "error": str(exc)}
                    if xfail:
                        rec["reason"] = xfail
                    results.append(rec)
                    self.log(f"[test] {status.upper()}: {name}: {exc}"
                             + (f" (expected to fail: {xfail})" if xfail else ""))

            if console_started:
                self._console.stop()

            # Firmware-update coverage: drive the keyboard's own HID update path
            # (BEGIN -> CHUNK -> COMMIT) with the built .bin. This is the one
            # path the UF2/BOOTSEL flash never exercises, and the most split-link-
            # sensitive code in the firmware. Stage + verify only (no APPLY), so
            # it's non-destructive — the keyboard keeps running its current image.
            # Run after the console is stopped (BEGIN tears USB down during the
            # master's staging erase) and regardless of the suite outcome.
            if bin_path:
                self.log("[runner] firmware update (HID stage+verify) — driving "
                         "BEGIN/CHUNK/COMMIT…")
                try:
                    fw_ok = stage_and_verify(bin_path, self.log)
                except Exception as exc:
                    fw_ok = False
                    self.log(f"[runner] firmware update ERROR: {exc}")
                results.append({"name": "firmware update (stage+verify .bin)",
                                "status": "pass" if fw_ok else "fail"})
                self.log(f"[test] {'PASS' if fw_ok else 'FAIL'}: "
                         f"firmware update (stage+verify .bin)")

            # Only a genuine FAIL fails the run; SKIP / XFAIL / XPASS do not.
            passed = not any(r.get("status") == "fail" for r in results)
            if passed:
                # Successful HIL test/deploy — park the OLEDs so they don't sit
                # lit between runs and age. (With no tests defined this is a bare
                # successful flash, which is still a deploy worth blanking after.)
                self._turn_off_displays()
            self.status = "idle"
            return {"passed": passed, "results": results}

        except Exception as exc:
            self.status = "error"
            self.log(f"[runner] fatal: {exc}")
            raise
        finally:
            self._flash.cleanup()

    def _wait_for_master_ready(self, need: int = 3, timeout: float = 30.0,
                               probe_timeout_ms: int = 800, spacing: float = 0.3) -> bool:
        """Block until the master is *stably* servicing HID, before the suite runs.

        After a cold flash the master answers an HID exchange or two, then blocks
        its main loop for several seconds doing the initial 72-keycap OLED render
        and split-sync to the just-booted slave (longer the larger the glyph
        set), during which Raw HID reads time out — then it recovers and stays
        responsive. The CI logs show the very first GET_ID succeeding (it catches
        the brief gap *before* that render starts) and the next two timing out,
        which fails the fresh-boot and GET_ID tests for a reason that has nothing
        to do with the firmware under test. So a single successful probe is not
        enough — require ``need`` consecutive replies to confirm the render is
        actually over, not just a momentary pre-render gap.

        Probes GET_LANG (cmd 7), which is read-only and — unlike GET_ID — does
        NOT consume the one-shot fresh-boot '*' marker that test_fresh_boot_marker
        asserts on, so this gate never disturbs that test. Best-effort: if the
        master never stabilises within ``timeout`` we log and run anyway, so a
        genuine hang still surfaces as the test failures it causes rather than
        being hidden here."""
        deadline = time.monotonic() + timeout
        streak = 0
        probes = 0
        last_err = None
        while time.monotonic() < deadline:
            probes += 1
            try:
                resp = self._raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG]),
                                      timeout_ms=probe_timeout_ms)
            except Exception as exc:
                resp = None
                # Treat any send error as "not ready yet" and keep polling, but
                # surface it (deduped, so the normal window doesn't flood the
                # log) so a *persistent* fault — e.g. the Raw HID interface never
                # reappearing, or a wrong VID/PID — is visible rather than hiding
                # behind a silent wait until the timeout.
                msg = f"{type(exc).__name__}: {exc}"
                if msg != last_err:
                    self.log(f"[runner] readiness probe error (still waiting): {msg}")
                    last_err = msg
            # Require the ACK status byte too, not just a well-formed header, so
            # a half-initialised reply during the boot window doesn't count as
            # ready. GET_LANG answers 'P\x07.<llCC>' (ACK) for a valid language.
            ok = (bool(resp) and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                  and resp[1] == CMD_GET_LANG and resp[2] == ACK)
            streak = streak + 1 if ok else 0
            if streak >= need:
                self.log(f"[runner] master ready — {need} consecutive GET_LANG "
                         f"replies after {probes} probe(s)")
                return True
            time.sleep(spacing)
        self.log(f"[runner] WARNING: master not stably responsive after {timeout:.0f}s "
                 f"({probes} probes) — running tests anyway")
        return False

    def _settle_master(self, need: int = 3, timeout: float = 15.0,
                       fast_ms: int = 250, probe_timeout_ms: int = 2500,
                       spacing: float = 0.2) -> bool:
        """Wait until the master answers *quickly* and consistently, so the graded
        suite never starts inside the post-cold-flash split-sync settling window.

        The read-only readiness gate (GET_LANG) can pass while the slave is still
        unreachable: with no state change there's no master->slave sync, so the
        main loop isn't stalled and probes return fast. But once a state-changing
        command lands (the keymap reset's slave sync, or the first test that
        toggles state), the master can block its loop in split-sync retries —
        delaying HID long enough to time out whatever command is in flight. The
        firmware change (1-attempt periodic syncs) bounds that stall; this gate is
        the belt-and-suspenders complement: it requires ``need`` consecutive
        GET_LANG replies that each come back within ``fast_ms`` (a stalled loop
        blows past that), confirming the settling window has cleared. GET_LANG is
        used (not GET_ID) so the one-shot fresh-boot '*' marker is left intact for
        test_fresh_boot_marker. Best-effort: logs and proceeds on timeout."""
        deadline = time.monotonic() + timeout
        streak = 0
        probes = 0
        worst_ms = 0.0
        while time.monotonic() < deadline:
            probes += 1
            t0 = time.monotonic()
            try:
                resp = self._raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG]),
                                      timeout_ms=probe_timeout_ms)
            except Exception:
                resp = None
            latency_ms = (time.monotonic() - t0) * 1000.0
            ok = (bool(resp) and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                  and resp[1] == CMD_GET_LANG and resp[2] == ACK
                  and latency_ms <= fast_ms)
            if ok:
                streak += 1
            else:
                worst_ms = max(worst_ms, latency_ms)
                streak = 0
            if streak >= need:
                self.log(f"[runner] master settled — {need} consecutive GET_LANG "
                         f"replies <= {fast_ms} ms after {probes} probe(s)")
                return True
            time.sleep(spacing)
        self.log(f"[runner] WARNING: master not settled after {timeout:.0f}s "
                 f"({probes} probes, worst {worst_ms:.0f} ms) — running anyway")
        return False

    def _device_caps(self) -> dict:
        """Read & cache the device's advertised capabilities (fw + protocol
        version) from GET_ID, for the per-test capability gate.

        Lazy and cached: only invoked when a gated test is first reached, which
        is after test_fresh_boot_marker has consumed the one-shot '*' marker, so
        this GET_ID doesn't disturb that test. Returns ``{}`` if the identity
        can't be read or parsed — ``skip_reason`` then runs the gated test rather
        than skipping it, so a read failure surfaces as a real result instead of
        being silently hidden. The empty dict is still cached to avoid re-probing.
        """
        if self._caps is not None:
            return self._caps
        caps = {}
        try:
            resp = self._raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
        except Exception as exc:
            self.log(f"[runner] could not read device caps "
                     f"(gated tests will run anyway): {exc}")
            resp = None
        if (resp and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                and resp[1] == CMD_GET_ID):
            identity = bytes(resp[3:]).split(b"\x00", 1)[0].decode("ascii", "replace")
            caps = parse_device_caps(identity)
            if caps:
                self.log(f"[runner] device caps: protocol P{caps.get('protocol')}, "
                         f"fw {caps.get('fw')}")
            else:
                self.log(f"[runner] could not parse device caps from {identity!r} "
                         "(gated tests will run anyway)")
        self._caps = caps
        return caps

    def _turn_off_displays(self) -> None:
        """Blank both OLEDs (status + per-key) after a passing run so the panels
        don't sit lit and age/burn in. The firmware turns them off without
        persisting to EEPROM (a key press or reboot restores brightness) and
        stays USB-enumerated. Best-effort: a failure here must not fail the run."""
        try:
            resp = self._raw.send(bytes([POLY_CHANNEL, CMD_DISPLAY_OFF]))
        except Exception as exc:
            self.log(f"[runner] could not turn off displays (non-fatal): {exc}")
            return
        if resp and len(resp) >= 3 and resp[0] == POLY_CHANNEL and resp[1] == CMD_DISPLAY_OFF and resp[2] == ACK:
            self.log("[runner] displays off — OLEDs blanked to prevent wear")
        else:
            self.log(f"[runner] display-off sent; unexpected/no ACK: {resp!r}")

    def _reset_keymap(self) -> None:
        """Reset the dynamic keymap (host-remappable layers 0..8) to the compiled
        firmware defaults so a stale keymap stored in EEPROM by an earlier build
        can't taint the run. Uses the existing VIA reset report (0x06) — the same
        path PolyKybdHost uses; the master resets its own copy, bridges the reset
        to the slave, and echoes the request back. Best-effort: a failure here is
        logged but must not fail the run."""
        try:
            resp = self._raw.send(bytes([VIA_DYNAMIC_KEYMAP_RESET]))
        except Exception as exc:
            self.log(f"[runner] could not reset dynamic keymap (non-fatal): {exc}")
            return
        if resp and len(resp) >= 1 and resp[0] == VIA_DYNAMIC_KEYMAP_RESET:
            self.log("[runner] dynamic keymap reset to firmware defaults")
        else:
            self.log(f"[runner] keymap-reset sent; unexpected/no echo: {resp!r}")

    def cleanup(self) -> None:
        self._flash.cleanup()
        self._console.stop()


def _derive_label(left_uf2: str) -> str:
    """Best-effort board name for the summary title, from the UF2 filename."""
    base = os.path.basename(left_uf2 or "")
    for board in ("split72", "corne42"):
        if board in base:
            return board
    return ""


# Per-status presentation. SKIP/XFAIL/XPASS are non-failing outcomes (see the
# capability gate / xfail markers in hil_tests.py); only ❌ FAIL fails the run.
_STATUS_MARK = {
    "pass": "✅", "fail": "❌", "skip": "⏭️", "xfail": "🟡", "xpass": "❗",
}
# Order + plural label for the summary count line.
_STATUS_WORD = [
    ("pass", "passed"), ("fail", "failed"), ("skip", "skipped"),
    ("xfail", "xfail"), ("xpass", "xpass"),
]


def _status_of(r: dict) -> str:
    """Status of a result record, tolerating older bool-``passed`` records.

    The compatibility layer between legacy ``{"passed": bool}`` records and the
    newer ``{"status": str}`` ones; ``status`` wins when both are present.

    >>> _status_of({"status": "skip"})
    'skip'
    >>> _status_of({"passed": True})
    'pass'
    >>> _status_of({"passed": False})
    'fail'
    >>> _status_of({"status": "xfail", "passed": False})
    'xfail'
    """
    return r.get("status") or ("pass" if r.get("passed") else "fail")


def write_github_summary(result: dict, label: str = "") -> None:
    """Surface each test as its own line in the GitHub Actions run.

    Writes a per-test markdown bullet to ``$GITHUB_STEP_SUMMARY`` (rendered on the
    job summary page) marked by status — ✅ pass, ❌ fail, ⏭️ skip (gated off on
    this firmware), 🟡 xfail (expected fail), ❗ xpass (xfail that unexpectedly
    passed) — and emits one ``::error::`` workflow annotation per real failure
    (plus a ``::warning::`` per xpass so a stale marker gets noticed), so it is
    obvious what happened without scrolling the raw log. No-ops when not under
    Actions / when the env vars are absent, so local runs are unaffected.
    """
    results = result.get("results", [])
    fatal = result.get("fatal")
    n_total = len(results)
    counts = {}
    for r in results:
        st = _status_of(r)
        counts[st] = counts.get(st, 0) + 1

    # Workflow-command annotations (parsed from stdout by the Actions runner).
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for r in results:
            st = _status_of(r)
            if st == "fail":
                detail = f" — {r['error']}" if r.get("error") else ""
                print(f"::error title=HIL test failed::{r['name']}{detail}")
            elif st == "xpass":
                print(f"::warning title=HIL xfail unexpectedly passed::{r['name']}"
                      " — the firmware now satisfies this; remove its xfail marker")
        if fatal:
            print(f"::error title=HIL run aborted::{fatal}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    title = f"HIL test results — {label}" if label else "HIL test results"
    lines = [f"## {title}", ""]
    if fatal:
        lines += [f"> ⚠️ **Run aborted before tests completed:** `{fatal}`", ""]
    if n_total:
        summary = ", ".join(f"{counts[st]} {word}"
                            for st, word in _STATUS_WORD if counts.get(st))
        lines += [f"**{summary}**", ""]
        for r in results:
            st = _status_of(r)
            mark = _STATUS_MARK.get(st, "❓")
            line = f"- {mark} {r['name']}"
            if st == "skip" and r.get("reason"):
                line += f" — _skipped: {r['reason']}_"
            elif st == "xfail" and r.get("reason"):
                line += f" — _expected fail: {r['reason']}_"
            elif st == "xpass":
                why = f" ({r['reason']})" if r.get("reason") else ""
                line += f" — **xfail marker can be removed**{why}"
            elif st == "fail" and r.get("error"):
                line += f" — `{r['error']}`"
            lines.append(line)
    elif not fatal:
        lines.append("_No tests were run._")
    lines.append("")

    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"[runner] could not write GITHUB_STEP_SUMMARY: {exc}")


if __name__ == "__main__":
    import argparse
    from .hil_tests import TESTS
    parser = argparse.ArgumentParser(description="Flash and test PolyKybd firmware")
    parser.add_argument("--left",  required=True, help="Path to left half UF2")
    parser.add_argument("--right", required=True, help="Path to right half UF2")
    parser.add_argument("--label", default=None,
                        help="Board name for the run summary title (default: inferred from --left)")
    parser.add_argument("--bin", dest="bin_path", default=None,
                        help="Optional raw .bin image; after the suite, drives the "
                             "keyboard's HID firmware-update path (BEGIN/CHUNK/COMMIT, "
                             "stage+verify only — non-destructive, no apply/reboot)")
    args = parser.parse_args()
    runner = TestRunner()
    try:
        result = runner.flash_and_test(args.left, args.right, tests=TESTS,
                                       bin_path=args.bin_path)
    except Exception as exc:
        # A fatal flash/enumerate error still gets a summary line so the run page
        # shows *why* there are no per-test results, not just a red X.
        result = {"passed": False, "results": [], "fatal": str(exc)}
    finally:
        runner.cleanup()

    write_github_summary(result, label=args.label or _derive_label(args.left))

    # Hard-exit after flushing. The native hid / RPi.GPIO libraries can double-
    # free during interpreter shutdown ("free(): invalid pointer"), which aborts
    # the process with SIGABRT — turning a passing run into a CI failure (exit
    # 134) and dropping buffered stdout (the PASS/FAIL lines). os._exit() skips
    # that teardown entirely; flush first (and the summary above is already
    # written to disk) so the logs are complete.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result["passed"] else 1)
