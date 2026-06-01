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


if __name__ == "__main__":
    import argparse
    from .hil_tests import TESTS
    parser = argparse.ArgumentParser(description="Flash and test PolyKybd firmware")
    parser.add_argument("--left",  required=True, help="Path to left half UF2")
    parser.add_argument("--right", required=True, help="Path to right half UF2")
    args = parser.parse_args()
    runner = TestRunner()
    result = runner.flash_and_test(args.left, args.right, tests=TESTS)
    runner.cleanup()
    # Hard-exit after flushing. The native hid / RPi.GPIO libraries can double-
    # free during interpreter shutdown ("free(): invalid pointer"), which aborts
    # the process with SIGABRT — turning a passing run into a CI failure (exit
    # 134) and dropping buffered stdout (the PASS/FAIL lines). os._exit() skips
    # that teardown entirely; flush first so the logs and the tee into
    # GITHUB_STEP_SUMMARY are complete.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result["passed"] else 1)
