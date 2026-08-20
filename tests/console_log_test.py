# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the firmware-console tap and the split-link health parser.

Two things are pinned here, both of which have a documented way of going wrong
silently:

* **Fragment reassembly.** A HID console read returns whatever fitted in one
  report, not a whole line, and a split can land mid-word. Code that matched raw
  chunks shipped once in the perf runner and produced truncated garbage
  (``ovltot wall=16ms bridg``), so the tap is tested against chunk boundaries in
  the middle of a word and in the middle of the number we parse.
* **The link classifier's exclusions.** ``nack`` and ``giveup`` must not count as
  errors — the firmware's own ``err%`` excludes them, and ``SYNC_BUSY`` (a nack)
  arrives on every erase re-poll of a flash. A classifier that counted them would
  redden a healthy run the moment a font-pack test ran.
"""
import re
import unittest

from station.console_log import (
    ConsoleTap,
    classify_link_health,
    link_delta,
    parse_link_stats,
)

LINE = ("Split link: 1200 tx crc_err=39 nack=11 transport_fail=1 giveup=13 "
        "err=3.3%")


class ParseLinkStatsTest(unittest.TestCase):
    def test_parses_every_counter(self):
        self.assertEqual(
            parse_link_stats(LINE),
            {"tx": 1200, "crc_err": 39, "nack": 11, "transport_fail": 1, "giveup": 13},
        )

    def test_other_console_lines_are_not_mistaken_for_a_summary(self):
        for line in ("Overlay mapping data received.", "Start idle.", "", "Split link"):
            self.assertIsNone(parse_link_stats(line), line)


class LinkDeltaTest(unittest.TestCase):
    def test_delta_excludes_the_boot_burst(self):
        # The absolutes carry a documented boot burst; only the growth is a
        # statement about the traffic the test itself caused.
        before = {"tx": 200, "crc_err": 39, "nack": 0, "transport_fail": 1, "giveup": 13}
        after = {"tx": 700, "crc_err": 39, "nack": 4, "transport_fail": 1, "giveup": 13}
        self.assertEqual(link_delta(before, after)["crc_err"], 0)
        self.assertEqual(link_delta(before, after)["tx"], 500)

    def test_a_reboot_between_summaries_cannot_read_as_a_repaired_link(self):
        before = {"tx": 900, "crc_err": 39, "nack": 0, "transport_fail": 2, "giveup": 5}
        after = {"tx": 200, "crc_err": 0, "nack": 0, "transport_fail": 0, "giveup": 0}
        self.assertEqual(link_delta(before, after),
                         {"tx": 0, "crc_err": 0, "nack": 0,
                          "transport_fail": 0, "giveup": 0})


class ClassifyLinkHealthTest(unittest.TestCase):
    def test_a_clean_window_passes(self):
        ok, errors, _tol = classify_link_health(
            {"tx": 450, "crc_err": 0, "nack": 0, "transport_fail": 0, "giveup": 0})
        self.assertTrue(ok)
        self.assertEqual(errors, 0)

    def test_nacks_are_not_errors(self):
        # SYNC_BUSY answers arrive on every erase re-poll of a flash: the wire
        # worked, the slave just said "not yet".
        ok, errors, _tol = classify_link_health(
            {"tx": 450, "crc_err": 0, "nack": 400, "transport_fail": 0, "giveup": 0})
        self.assertTrue(ok)
        self.assertEqual(errors, 0)

    def test_giveups_alone_are_not_double_counted(self):
        ok, errors, _tol = classify_link_health(
            {"tx": 450, "crc_err": 0, "nack": 0, "transport_fail": 0, "giveup": 30})
        self.assertTrue(ok)
        self.assertEqual(errors, 0)

    def test_corrupted_frames_fail(self):
        ok, errors, tol = classify_link_health(
            {"tx": 450, "crc_err": 20, "nack": 0, "transport_fail": 0, "giveup": 20})
        self.assertFalse(ok)
        self.assertEqual((errors, tol), (20, 4))

    def test_silent_frames_fail(self):
        ok, _errors, _tol = classify_link_health(
            {"tx": 450, "crc_err": 0, "nack": 0, "transport_fail": 20, "giveup": 20})
        self.assertFalse(ok)

    def test_a_single_blip_in_a_big_window_is_tolerated(self):
        ok, _errors, _tol = classify_link_health(
            {"tx": 450, "crc_err": 1, "nack": 0, "transport_fail": 0, "giveup": 1})
        self.assertTrue(ok)


class ConsoleTapTest(unittest.TestCase):
    def test_a_line_split_mid_word_is_reassembled(self):
        tap = ConsoleTap()
        tap.feed("Split link: 1200 tx crc_er")
        self.assertEqual(tap.link_stats(), [], "an unterminated line is not a line")
        tap.feed("r=39 nack=11 transport_fail=1 giveup=13 err=3.3%\n")
        self.assertEqual(tap.link_stats()[0]["crc_err"], 39)

    def test_several_lines_in_one_read_all_arrive(self):
        tap = ConsoleTap()
        tap.feed("Start idle.\nTransition to idle [style=eden]\npartial")
        self.assertEqual(len(tap.since(0)), 2)
        self.assertEqual(tap.find_all("Transition to idle")[0],
                         "Transition to idle [style=eden]")

    def test_flush_releases_the_trailing_fragment(self):
        # The last line is usually the interesting one and the firmware does not
        # always newline-terminate before the reader stops.
        tap = ConsoleTap()
        tap.feed("LoopProf: frame 148ms, worst slice 3ms")
        self.assertEqual(tap.since(0), [])
        tap.flush()
        self.assertEqual(len(tap.since(0)), 1)

    def test_mark_scopes_lines_to_what_happened_after_it(self):
        tap = ConsoleTap()
        tap.feed("before\n")
        mark = tap.mark()
        tap.feed("after\n")
        self.assertEqual(tap.since(mark), ["after"])
        self.assertEqual(tap.find_all("before", mark), [])

    def test_marks_survive_eviction_without_returning_the_wrong_lines(self):
        tap = ConsoleTap(maxlen=3)
        mark = tap.mark()
        for i in range(10):
            tap.feed(f"line{i}\n")
        # Only the last three are retained; none of them predates the mark, and
        # the ones returned are genuinely the newest.
        self.assertEqual(tap.since(mark), ["line7", "line8", "line9"])

    def test_wait_for_accepts_a_regex_and_gives_up_rather_than_hanging(self):
        tap = ConsoleTap()
        tap.feed("Transition to idle [style=eden] - eden screensaver\n")
        self.assertIsNotNone(tap.wait_for(re.compile(r"style=eden"), timeout=0))
        self.assertIsNone(tap.wait_for("never printed", timeout=0))

    def test_carriage_returns_do_not_become_part_of_the_line(self):
        tap = ConsoleTap()
        tap.feed("Start idle.\r\n")
        self.assertEqual(tap.since(0), ["Start idle."])


if __name__ == "__main__":
    unittest.main()
