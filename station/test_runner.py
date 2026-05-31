# SPDX-License-Identifier: GPL-2.0-only
import sys
import time
from typing import Callable

from .flash import FlashController
from .hid import HIDConsole, RawHID


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
            self.status = "idle"
            return {"passed": all(r["passed"] for r in results), "results": results}

        except Exception as exc:
            self.status = "error"
            self.log(f"[runner] fatal: {exc}")
            raise
        finally:
            self._flash.cleanup()

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
    sys.exit(0 if result["passed"] else 1)
