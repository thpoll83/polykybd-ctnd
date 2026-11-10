# SPDX-License-Identifier: GPL-2.0-only
"""Every libhidapi call that touches its process-wide state is serialised.

libhidapi's hidraw backend keeps one global error string; hid_enumerate() and
hid_open_path() each free and reallocate it, and the `hid` package reads it after
a failed open. The console reader thread and the test thread both enumerate and
open across every reboot, and an overlap crashed the runner with SIGSEGV
(2026-11-10). These tests pin the lock, and that no call site goes around it.
"""
import ast
import pathlib
import sys
import threading
import time
import types
import unittest

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import hid as station_hid  # noqa: E402

STATION = pathlib.Path(__file__).resolve().parent.parent / "station"


class OverlapProbe:
    """Stands in for a libhidapi entry point and records whether two threads
    were ever inside it, or the other probe, at the same time."""

    def __init__(self):
        self._inside = 0
        self._guard = threading.Lock()
        self.overlapped = False
        self.calls = 0

    def __call__(self, *args, **kwargs):
        with self._guard:
            self._inside += 1
            self.calls += 1
            if self._inside > 1:
                self.overlapped = True
        time.sleep(0.002)          # widen the window a race needs
        with self._guard:
            self._inside -= 1
        return []


class HidapiGlobalLockTest(unittest.TestCase):

    def setUp(self):
        self._saved = (station_hid.hid.enumerate, station_hid.hid.Device)
        self.addCleanup(self._restore)

    def _restore(self):
        station_hid.hid.enumerate, station_hid.hid.Device = self._saved

    def test_enumerate_and_open_never_run_at_the_same_time(self):
        probe = OverlapProbe()
        station_hid.hid.enumerate = probe
        station_hid.hid.Device = lambda path: probe()

        def enumerate_loop():
            for _ in range(40):
                station_hid._find_path(1, 2, 3, 4)

        def open_loop():
            for _ in range(40):
                station_hid.open_device(b"/dev/hidraw0")

        threads = [threading.Thread(target=f) for f in (enumerate_loop, open_loop,
                                                        enumerate_loop, open_loop)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(probe.calls, 160)
        self.assertFalse(probe.overlapped,
                         "two threads were inside libhidapi's enumerate/open at once")

    def test_the_lock_is_held_inside_the_call(self):
        seen = []
        station_hid.hid.enumerate = lambda *a: seen.append(
            station_hid._HIDAPI_GLOBAL_LOCK.locked()) or []
        station_hid.hid.Device = lambda path: seen.append(
            station_hid._HIDAPI_GLOBAL_LOCK.locked())
        station_hid._find_path(1, 2, 3, 4)
        station_hid.open_device(b"/dev/hidraw0")
        self.assertEqual(seen, [True, True])

    def test_a_failed_open_releases_the_lock(self):
        def boom(path):
            raise OSError("gone")
        station_hid.hid.Device = boom
        with self.assertRaises(OSError):
            station_hid.open_device(b"/dev/hidraw0")
        self.assertFalse(station_hid._HIDAPI_GLOBAL_LOCK.locked())

    def test_no_call_site_goes_around_the_lock(self):
        """Only _enumerate and open_device may call hid.enumerate / hid.Device."""
        allowed = {("hid.py", "_enumerate"), ("hid.py", "open_device")}
        offenders = []
        for path in sorted(STATION.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for func in ast.walk(tree):
                if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(func):
                    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and isinstance(node.func.value, ast.Name)
                            and node.func.value.id == "hid"
                            and node.func.attr in ("enumerate", "Device")
                            and (path.name, func.name) not in allowed):
                        offenders.append(f"{path.name}:{node.lineno} in {func.name}")
        self.assertEqual(offenders, [], "call the locked helpers instead")


if __name__ == "__main__":
    unittest.main()
