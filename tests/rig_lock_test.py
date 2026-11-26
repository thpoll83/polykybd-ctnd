# SPDX-License-Identifier: GPL-2.0-only
"""The rig lock shared by the UI and the CI runners (station/rig_lock.py).

The UI must be refused while another process holds the lock, and a CI runner
must wait until the holder lets go. A child process holds the lock here (the
real holders are separate processes) until the test writes a line to its stdin.
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from station import rig_lock

HOLD = """
import sys
from pathlib import Path
from station import rig_lock
fd = rig_lock.try_acquire(Path(sys.argv[1]))
print("held" if fd is not None else "busy", flush=True)
sys.stdin.readline()   # hold until the test lets go
"""


class RigLockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "var" / "rig.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def _hold(self):
        p = subprocess.Popen([sys.executable, "-c", HOLD, str(self.path)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
                             cwd=str(Path(__file__).resolve().parent.parent))
        self.addCleanup(self._let_go, p)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    @staticmethod
    def _let_go(p):
        if p.poll() is None:
            p.stdin.write("\n")
            p.stdin.flush()
        p.wait(timeout=10)
        p.stdin.close()
        p.stdout.close()

    def test_free_lock_is_taken_and_released(self):
        fd = rig_lock.try_acquire(self.path)
        self.assertIsNotNone(fd)
        rig_lock.release(fd)
        fd = rig_lock.try_acquire(self.path)
        self.assertIsNotNone(fd)
        rig_lock.release(fd)

    def test_ui_is_refused_while_another_process_holds_it(self):
        self._hold()
        self.assertIsNone(rig_lock.try_acquire(self.path))

    def test_holder_is_named(self):
        holder = self._hold()
        who = rig_lock.holder(self.path)
        self.assertTrue(who.startswith(str(holder.pid) + " "), who)

    def test_runner_waits_for_the_holder(self):
        holder = self._hold()
        lines = []

        def log(line):
            lines.append(line)
            if "waiting" in line:   # contended: now let the holder go
                self._let_go(holder)

        fd = rig_lock.acquire_waiting(log=log, path=self.path)
        rig_lock.release(fd)
        self.assertIn("waiting", lines[0])
        self.assertIn("rig free", lines[1])


if __name__ == "__main__":
    unittest.main()
