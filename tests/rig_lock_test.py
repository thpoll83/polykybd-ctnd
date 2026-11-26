# SPDX-License-Identifier: GPL-2.0-only
"""The rig lock shared by the UI and the CI runners (station/rig_lock.py).

The UI must be refused while another process holds the lock, and a CI runner
must wait until the holder lets go. A child process holds the lock here, since
an flock taken twice in one process through two descriptors also conflicts,
but the real holders are separate processes.
"""
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from station import rig_lock

HOLD = """
import sys, time
from pathlib import Path
from station import rig_lock
fd = rig_lock.try_acquire(Path(sys.argv[1]))
print("held" if fd is not None else "busy", flush=True)
time.sleep(float(sys.argv[2]))
"""


class RigLockTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "var" / "rig.lock"

    def tearDown(self):
        self._tmp.cleanup()

    def _hold(self, seconds):
        p = subprocess.Popen([sys.executable, "-c", HOLD, str(self.path), str(seconds)],
                             stdout=subprocess.PIPE, text=True,
                             cwd=str(Path(__file__).resolve().parent.parent))
        self.addCleanup(p.wait)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_free_lock_is_taken_and_released(self):
        fd = rig_lock.try_acquire(self.path)
        self.assertIsNotNone(fd)
        rig_lock.release(fd)
        fd = rig_lock.try_acquire(self.path)
        self.assertIsNotNone(fd)
        rig_lock.release(fd)

    def test_ui_is_refused_while_another_process_holds_it(self):
        self._hold(1.0)
        self.assertIsNone(rig_lock.try_acquire(self.path))

    def test_runner_waits_for_the_holder(self):
        self._hold(0.6)
        lines = []
        start = time.monotonic()
        fd = rig_lock.acquire_waiting(log=lines.append, path=self.path)
        waited = time.monotonic() - start
        rig_lock.release(fd)
        self.assertGreater(waited, 0.3)
        self.assertIn("waiting", lines[0])


if __name__ == "__main__":
    unittest.main()
