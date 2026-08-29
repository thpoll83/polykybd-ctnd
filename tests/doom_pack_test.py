# SPDX-License-Identifier: GPL-2.0-only
"""Offline tests for the TIER_DOOM signed-engine-pack checks (FW-9).

The rig is not reachable from a development session, so what is pinned here is the
part a hardware run cannot re-derive on its own: the pure verdict classifier
(against the firmware's *exact* console strings), the tampered/unsigned pack
derivations the rig applies to the one signed artifact, and the TIER_DOOM skip
gate. The device-driving `_doom_idle_verdict` itself needs hardware; its decision
logic lives in `classify_doom_verdict`, which is what these tests exercise.
"""
import sys
import types
import unittest

# station.hil_tests imports hidapi at module load (but not RPi.GPIO — like the
# sibling hil_tests_test.py, only `hid` needs stubbing). Deliberately do NOT stub
# RPi here: an incomplete RPi.GPIO stub would shadow the complete one another test
# module installs (perf_test drives flash.py, which reads GPIO.HIGH at import).
if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station.hil_tests import (  # noqa: E402
    DOOM_SIG_SIZE,
    TIER_DOOM,
    _doom_strip_sig,
    _doom_tamper_sig,
    classify_doom_verdict,
    skip_reason,
)

# The firmware's actual printf strings (keyboards/polykybd/doom/*.c) — copied
# verbatim so a wording change in the firmware fails HERE, not silently on the rig.
FW_LOADED   = "doom: pack v4 loaded (229376 B, arena_off 8192)"
FW_INVALID  = "doom: pack signature is INVALID — refuse (FW-9: flash a release-signed .plyx)"
FW_UNSIGNED = "doom: pack is unsigned — refuse (FW-9: flash a release-signed .plyx)"
FW_NO_PACK  = "doom: no PlyX pack at 0x107c0000"
FW_CRC      = "doom: pack CRC deadbeef != cafebabe — refuse"
FW_ATTRACT  = "doom: attract screensaver up"
FW_FIREDEMO = "doom: no usable engine pack — running the fire demo instead"


class ClassifyDoomVerdictTest(unittest.TestCase):
    def test_loaded(self):
        self.assertEqual("loaded", classify_doom_verdict([FW_LOADED, FW_ATTRACT]))

    def test_invalid(self):
        self.assertEqual(
            "invalid", classify_doom_verdict([FW_INVALID, FW_FIREDEMO, FW_ATTRACT]))

    def test_unsigned(self):
        self.assertEqual(
            "unsigned", classify_doom_verdict([FW_UNSIGNED, FW_FIREDEMO, FW_ATTRACT]))

    def test_none_when_no_signature_verdict(self):
        # A refusal that is not a signature verdict (no pack, CRC fail) is 'none' —
        # not our accept and not our two refusals.
        self.assertEqual("none", classify_doom_verdict([FW_NO_PACK, FW_FIREDEMO]))
        self.assertEqual("none", classify_doom_verdict([FW_CRC, FW_FIREDEMO]))
        self.assertEqual("none", classify_doom_verdict([]))

    def test_a_refusal_outranks_a_stray_loaded(self):
        # loaded and a refusal are mutually exclusive per run, but if a stale
        # 'loaded' from an earlier attempt is in the window, the refusal must win
        # so a genuine INVALID can never be masked into an accept.
        self.assertEqual(
            "invalid", classify_doom_verdict([FW_LOADED, FW_INVALID]))
        self.assertEqual(
            "unsigned", classify_doom_verdict([FW_LOADED, FW_UNSIGNED]))


class DerivedPackTest(unittest.TestCase):
    def _pack(self, image_len=200):
        # 64 header + image + 64 signature, distinct byte regions.
        return (bytes([0x11]) * 64) + (bytes([0x22]) * image_len) + (bytes([0x33]) * DOOM_SIG_SIZE)

    def test_tamper_changes_only_the_signature_and_keeps_length(self):
        p = self._pack()
        t = _doom_tamper_sig(p)
        self.assertEqual(len(p), len(t))                 # same length -> CRC/size checks still pass
        self.assertEqual(p[:-DOOM_SIG_SIZE], t[:-DOOM_SIG_SIZE])  # header+image untouched
        self.assertNotEqual(p[-DOOM_SIG_SIZE:], t[-DOOM_SIG_SIZE:])  # signature changed
        # exactly one bit differs
        diff = sum(bin(a ^ b).count("1") for a, b in zip(p, t))
        self.assertEqual(1, diff)

    def test_strip_removes_exactly_the_trailer(self):
        p = self._pack()
        s = _doom_strip_sig(p)
        self.assertEqual(len(p) - DOOM_SIG_SIZE, len(s))
        self.assertEqual(p[:-DOOM_SIG_SIZE], s)          # header+image preserved

    def test_derivations_refuse_a_too_short_pack(self):
        for fn in (_doom_tamper_sig, _doom_strip_sig):
            with self.assertRaises(ValueError):
                fn(bytes(DOOM_SIG_SIZE))                  # nothing before the trailer


class DoomTierGateTest(unittest.TestCase):
    def _t(self):
        return {"name": "x", "fn": lambda *a: True, "min_protocol": 6,
                "needs_console": True, "tier": TIER_DOOM}

    def test_skips_without_doom_optin(self):
        # console present, protocol ok, but doom not opted in -> skip (fail-closed).
        caps = {"protocol": 15, "console": True, "extended": True, "doom": False}
        self.assertIsNotNone(skip_reason(self._t(), caps))

    def test_runs_when_opted_in(self):
        caps = {"protocol": 15, "console": True, "extended": False, "doom": True}
        self.assertIsNone(skip_reason(self._t(), caps))

    def test_still_gated_on_console(self):
        # doom opted in but console down -> still skip (needs_console wins first).
        caps = {"protocol": 15, "console": False, "doom": True}
        self.assertIsNotNone(skip_reason(self._t(), caps))


if __name__ == "__main__":
    unittest.main()
