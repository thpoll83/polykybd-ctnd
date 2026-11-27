# SPDX-License-Identifier: GPL-2.0-only
"""station/apply_cue.py: the per-half timeline of the apply's orange cue."""
import unittest

from station import apply_cue


def _samples(left, right, step=100.0):
    return [(i * step, {"left": l, "right": r}) for i, (l, r) in enumerate(zip(left, right))]


class EventsTest(unittest.TestCase):
    def test_start_and_end(self):
        s = _samples([10, 10, 40, 40, 40, 5, 5], [10, 40, 40, 40, 40, 40, 5])
        self.assertEqual(apply_cue.events(s, "left"), (200.0, 500.0))
        self.assertEqual(apply_cue.events(s, "right"), (100.0, 600.0))

    def test_small_changes_do_not_count(self):
        s = _samples([10, 15, 12, 17], [10, 10, 10, 10])
        self.assertEqual(apply_cue.events(s, "left"), (None, None))

    def test_no_end(self):
        s = _samples([10, 40, 40], [10, 10, 10])
        self.assertEqual(apply_cue.events(s, "left"), (100.0, None))

    def test_summary(self):
        s = _samples([10, 10, 40, 40, 40, 5, 5], [10, 40, 40, 40, 40, 40, 5])
        lines = apply_cue.summary(s, 150.0)
        self.assertIn("left: LEDs changed +0.05 s, changed again +0.35 s (held 0.30 s)",
                      lines[0])
        self.assertIn("start: right minus left -100 ms", lines[2])
        self.assertIn("end: right minus left +100 ms", lines[3])

    def test_empty(self):
        self.assertEqual(apply_cue.events([], "left"), (None, None))


if __name__ == "__main__":
    unittest.main()
