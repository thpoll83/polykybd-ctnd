# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the ad-hoc HIL probe loader (``station.probe``).

The probe path is what lets a firmware bug be chased on the rig with nobody
flashing a ``.bin`` by hand, so the parts pinned here are the ones whose failure
would waste a whole rig cycle (a bad path discovered only after the flash) or
silently run the wrong thing (a name resolving outside the probe directory).
"""

import os
import tempfile
import unittest

from station.probe import (PROBE_SUBDIR, ProbeError, load_probe, probe_root,
                           resolve_probe)

GOOD = '''
NAME = "check the thing"
MIN_PROTOCOL = 15

def probe(raw, log):
    log("ran")
    return True
'''


class ProbeFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fw = self._tmp.name
        self.root = os.path.join(self.fw, PROBE_SUBDIR)
        os.makedirs(self.root)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, "w") as fh:
            fh.write(body)
        return path


class ResolveTest(ProbeFixture):
    def test_a_bare_name_resolves_to_the_probe_file(self):
        want = self.write("fw_apply.py", GOOD)
        self.assertEqual(resolve_probe("fw_apply", self.fw), os.path.realpath(want))

    def test_a_filename_and_a_repo_relative_path_resolve_the_same(self):
        want = os.path.realpath(self.write("fw_apply.py", GOOD))
        self.assertEqual(resolve_probe("fw_apply.py", self.fw), want)
        self.assertEqual(
            resolve_probe(os.path.join(PROBE_SUBDIR, "fw_apply.py"), self.fw), want)

    def test_a_missing_probe_names_what_IS_available(self):
        self.write("alpha.py", GOOD)
        self.write("_helper.py", GOOD)          # underscore = not a probe
        with self.assertRaises(ProbeError) as ctx:
            resolve_probe("beta", self.fw)
        self.assertIn("alpha", str(ctx.exception))
        self.assertNotIn("_helper", str(ctx.exception))

    def test_a_traversal_is_refused(self):
        outside = os.path.join(self.fw, "escape.py")
        with open(outside, "w") as fh:
            fh.write(GOOD)
        with self.assertRaises(ProbeError) as ctx:
            resolve_probe("../../../escape", self.fw)
        self.assertIn("outside", str(ctx.exception))

    def test_a_symlink_pointing_out_is_refused(self):
        outside = os.path.join(self.fw, "escape.py")
        with open(outside, "w") as fh:
            fh.write(GOOD)
        link = os.path.join(self.root, "sneaky.py")
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable here")
        # realpath runs BEFORE the containment test, so the link is followed to
        # its target and the target is what must be inside the probe dir.
        with self.assertRaises(ProbeError) as ctx:
            resolve_probe("sneaky", self.fw)
        self.assertIn("outside", str(ctx.exception))

    def test_a_missing_probe_directory_says_so_rather_than_KeyError(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ProbeError) as ctx:
                resolve_probe("anything", empty)
            self.assertIn("no probe directory", str(ctx.exception))

    def test_probe_root_is_under_the_firmware_checkout(self):
        self.assertEqual(probe_root(self.fw), os.path.realpath(self.root))


class LoadTest(ProbeFixture):
    def test_a_probe_becomes_a_suite_test_dict_carrying_its_gates(self):
        test = load_probe(self.write("p.py", GOOD))
        self.assertEqual(test["name"], "check the thing")
        self.assertEqual(test["min_protocol"], 15)
        self.assertTrue(callable(test["fn"]))
        lines = []
        self.assertTrue(test["fn"](None, lines.append))
        self.assertEqual(lines, ["ran"])

    def test_the_name_defaults_to_the_filename(self):
        test = load_probe(self.write("fw_apply.py", "def probe(raw, log): return True\n"))
        self.assertEqual(test["name"], "probe: fw_apply")

    def test_only_the_gates_the_probe_declares_are_set(self):
        test = load_probe(self.write("p.py", "def probe(raw, log): return True\n"))
        for key in ("min_protocol", "min_fw", "needs_console", "xfail"):
            self.assertNotIn(key, test)

    def test_a_module_with_no_probe_function_is_refused(self):
        with self.assertRaises(ProbeError) as ctx:
            load_probe(self.write("p.py", "NAME = 'nope'\n"))
        self.assertIn("no probe(raw, log)", str(ctx.exception))

    def test_a_probe_is_registered_in_sys_modules_before_it_executes(self):
        """importlib's prescribed idiom, and not optional for a probe.

        Anything resolving its own defining module through ``sys.modules`` during
        import fails without it — at LOAD time, so a whole rig run is spent on an
        error that has nothing to do with the firmware under test.

        ⚠️ The fixture is deliberately ``from __future__ import annotations`` plus
        ``@dataclass``, NOT a bare ``@dataclass``. A bare one does not reproduce:
        dataclasses fall back to empty globals when the module is absent. String
        annotations are what force the lookup. A test written against the obvious
        example would pass against the broken code and prove nothing.
        """
        body = ("from __future__ import annotations\n"
                "from dataclasses import dataclass, field\n"
                "@dataclass\n"
                "class C:\n"
                "    xs: list[int] = field(default_factory=list)\n"
                "def probe(raw, log):\n"
                "    return C().xs == []\n")
        test = load_probe(self.write("p.py", body))
        self.assertTrue(test["fn"](None, lambda *_: None))

    def test_a_failed_import_does_not_leave_the_module_registered(self):
        # The other half of the idiom: a probe that raises must not leave a
        # half-initialised module behind for the next load of the same name.
        import sys
        before = set(sys.modules)
        with self.assertRaises(ProbeError):
            load_probe(self.write("p.py", "raise RuntimeError('boom')\n"))
        self.assertEqual([m for m in set(sys.modules) - before
                          if m.startswith("hil_probe_")], [])

    def test_a_probe_that_fails_to_IMPORT_is_refused_before_any_flash(self):
        # Reported as a ProbeError, not raised through the runner: the CLI turns
        # it into a parser error, so a broken probe costs a message rather than a
        # flash-and-enumerate cycle on the rig.
        with self.assertRaises(ProbeError) as ctx:
            load_probe(self.write("p.py", "raise RuntimeError('boom')\n"))
        self.assertIn("failed to import", str(ctx.exception))
        self.assertIn("boom", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
