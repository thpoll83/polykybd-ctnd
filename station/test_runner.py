# SPDX-License-Identifier: GPL-2.0-only
import os
import sys
import time
from typing import Callable

from .flash import FlashController
from .hid import HIDConsole, RawHID

# Raw HID display-off control command — mirrors the firmware dispatcher in
# keyboards/handwired/polykybd/hid_com.c (case 24 / 0x18). A command report is
# data[0]='P' (channel marker) then data[1]=command id; the firmware replies "P\x18.".
POLY_CHANNEL    = 0x50  # 'P'
CMD_DISPLAY_OFF = 0x18
ACK             = ord(".")


class TestRunner:
    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.status = "idle"
        self._flash = FlashController()
        self._console = HIDConsole()
        self._raw = RawHID()

    def flash_and_test(self, left_uf2: str, right_uf2: str, tests: list = None) -> dict:
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
            # requires CONSOLE_ENABLE in the firmware, which the PolyKybd build
            # does not set. Its absence (or any transient open failure) must not
            # fail the test run, so treat startup as best-effort.
            console_started = False
            try:
                self._console.start(lambda msg: self.log(f"[qmk] {msg}"))
                console_started = True
            except Exception as exc:
                self.log(f"[runner] HID console unavailable (continuing without it): {exc}")

            self.status = "testing"
            for test in (tests or []):
                name = test.get("name", "unnamed")
                try:
                    passed = bool(test["fn"](self._raw, self.log))
                    results.append({"name": name, "passed": passed})
                    self.log(f"[test] {'PASS' if passed else 'FAIL'}: {name}")
                except Exception as exc:
                    results.append({"name": name, "passed": False, "error": str(exc)})
                    self.log(f"[test] ERROR: {name}: {exc}")

            if console_started:
                self._console.stop()
            passed = all(r["passed"] for r in results)
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


def write_github_summary(result: dict, label: str = "") -> None:
    """Surface each test as its own line in the GitHub Actions run.

    Writes a ✅/❌ markdown bullet per test to ``$GITHUB_STEP_SUMMARY`` (rendered
    on the job summary page) and emits one ``::error::`` workflow annotation per
    failure (shown at the top of the run and inline), so it is obvious which
    test failed without scrolling the raw log. No-ops when not under Actions /
    when the env vars are absent, so local runs are unaffected.
    """
    results = result.get("results", [])
    fatal = result.get("fatal")
    n_total = len(results)
    n_pass = sum(1 for r in results if r.get("passed"))

    # Workflow-command annotations (parsed from stdout by the Actions runner).
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for r in results:
            if not r.get("passed"):
                detail = f" — {r['error']}" if r.get("error") else ""
                print(f"::error title=HIL test failed::{r['name']}{detail}")
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
        lines += [f"**{n_pass}/{n_total} passed**", ""]
        for r in results:
            mark = "✅" if r.get("passed") else "❌"
            line = f"- {mark} {r['name']}"
            if not r.get("passed") and r.get("error"):
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
    args = parser.parse_args()
    runner = TestRunner()
    try:
        result = runner.flash_and_test(args.left, args.right, tests=TESTS)
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
