"""Offline tests for scripts/runner-ctl.sh (HIL-7).

The wrapper exists so the station user's passwordless sudo grant can be an exact
command list instead of a `actions.runner.*` wildcard. Its whole security value
is in what it REFUSES, and that is not visible from reading the sudoers rule — so
the refusal paths are pinned here rather than left to a manual check on the rig.

No systemd needed: a fake `systemctl` is placed first on PATH, which both stubs
the unit lookup and records what the script would have executed.
"""

import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "runner-ctl.sh"

# Stands in for `systemctl`. `list-units` prints the unit table the script parses;
# any other invocation appends its arguments to $FAKE_LOG so the test can assert
# on the command that was finally exec'd.
FAKE_SYSTEMCTL = """#!/usr/bin/env bash
if [[ "$1" == "list-units" ]]; then
    printf '%s\\n' "$FAKE_UNIT"
    exit 0
fi
printf '%s\\n' "$*" >> "$FAKE_LOG"
exit 0
"""


class RunnerCtlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        fake = tmp / "systemctl"
        fake.write_text(FAKE_SYSTEMCTL)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        self.log = tmp / "log"
        self.env = {
            **os.environ,
            "PATH": f"{tmp}:{os.environ['PATH']}",
            "FAKE_LOG": str(self.log),
            "FAKE_UNIT": "actions.runner.thpoll83-qmk_firmware.RP4-HIL.service",
        }

    def tearDown(self):
        self.tmp.cleanup()

    def run_ctl(self, *args, unit=None):
        env = dict(self.env)
        if unit is not None:
            env["FAKE_UNIT"] = unit
        # Clear per invocation: without this a subTest asserting "nothing ran"
        # reads the previous subTest's residue and passes/fails for the wrong
        # reason (it did exactly that while this file was being written).
        self.log.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env, capture_output=True, text=True,
        )

    def executed(self):
        return self.log.read_text().strip() if self.log.exists() else ""

    # ── accepted ──────────────────────────────────────────────────────────────

    def test_each_allowed_action_reaches_systemctl(self):
        for action in ("start", "stop", "restart"):
            with self.subTest(action=action):
                r = self.run_ctl(action)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(
                    self.executed(),
                    f"{action} actions.runner.thpoll83-qmk_firmware.RP4-HIL.service",
                )

    # ── refused ───────────────────────────────────────────────────────────────

    def test_no_argument_is_refused(self):
        r = self.run_ctl()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.executed(), "")

    def test_unknown_action_is_refused(self):
        for bad in ("status", "cat", "enable", "--version", ""):
            with self.subTest(arg=bad):
                r = self.run_ctl(bad)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(self.executed(), "")

    def test_extra_arguments_are_refused(self):
        """The point of the wrapper: no argument pass-through.

        Under the old sudoers wildcard these tails were permitted, because `*`
        matches spaces. Each must be rejected before systemctl is reached.
        """
        for extra in (
            ["restart", "polykybd-ctnd.service"],
            ["restart", "actions.runner.x.service", "--host=evil"],
            ["start", "/tmp/evil.service"],
            ["stop", "-H", "otherhost"],
        ):
            with self.subTest(args=extra):
                r = self.run_ctl(*extra)
                self.assertEqual(r.returncode, 2)
                self.assertEqual(self.executed(), "")

    def test_missing_unit_is_an_error(self):
        r = self.run_ctl("restart", unit="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no installed actions.runner unit", r.stderr)
        self.assertEqual(self.executed(), "")

    def test_unexpected_unit_name_is_refused(self):
        """Defence in depth: the name comes from systemd, not the caller, but a
        surprising one must fail loudly here rather than be passed through."""
        for unit in ("polykybd-ctnd.service", "-H", "actions.runner.x.service.evil"):
            with self.subTest(unit=unit):
                r = self.run_ctl("restart", unit=unit)
                self.assertEqual(r.returncode, 1)
                self.assertEqual(self.executed(), "")

    def test_whitespace_in_the_unit_line_is_truncated_not_passed_through(self):
        """A second token on the unit line is dropped, never forwarded.

        `find`'s `awk 'NR==1{print $1}'` splits on whitespace, so only the first
        field survives — systemctl receives one unit and nothing else. Pinned
        because it is the property that makes the discovery step safe, and it is
        easy to destroy by "fixing" the awk to print the whole line.
        """
        r = self.run_ctl("restart", unit="actions.runner.x.service evil.service")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.executed(), "restart actions.runner.x.service")


if __name__ == "__main__":
    unittest.main()
