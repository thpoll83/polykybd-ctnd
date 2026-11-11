"""Cmd 48 (runtime combos / tap dance): the packers, the parsers, and
test_combos_round_trip against a fake board that behaves like poly_combos.c.
No hardware."""
import sys
import types
import unittest

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

from station import combos, hil_tests  # noqa: E402


def _combo_ok(w):
    keys, result = w[:4], w[4]
    if not any(w):
        return True
    n = 0
    while n < 4 and keys[n]:
        n += 1
    return n >= 2 and not any(keys[n:]) and result != 0


def _td_kc_ok(kc):
    return kc <= 0x1FFF or 0x5200 <= kc <= 0x523F or 0x5260 <= kc <= 0x527F


def _td_ok(w):
    term = w[4]
    return all(_td_kc_ok(k) for k in w[:4]) and (term == 0 or (100 <= term <= 500 and term % 10 == 0))


class FakeBoard:
    def __init__(self, available=True, validates=True, reaches_other_half=True):
        self.ram = {0: [[0] * 5 for _ in range(16)], 1: [[0] * 5 for _ in range(16)]}
        self.flash = None
        self.available = available
        self.validates = validates
        self.reaches_other_half = reaches_other_half

    def send(self, data, **_):
        data = bytes(data).ljust(64, b"\0")
        op = data[2]
        if op == combos.OP_INFO:
            body = bytes([16, 16, 10, 1 if self.available else 0, 50, 0])
            return (bytes([0x50, 48, ord("." if self.available else "!")]) + body).ljust(64, b"\0")
        if op == combos.OP_COMMIT:
            ok = self.available
            if ok:
                self.flash = {k: [list(w) for w in v] for k, v in self.ram.items()}
            return bytes([0x50, 48, ord("." if ok else "!"),
                          1 if (ok and self.reaches_other_half) else 0]).ljust(64, b"\0")
        kind, index = data[3], data[4]
        table = self.ram.get(kind)
        ok = self.available and table is not None and index < 16
        if ok and op == combos.OP_SET:
            words = [data[5 + 2 * i] | (data[6 + 2 * i] << 8) for i in range(5)]
            valid = _combo_ok(words) if kind == 0 else _td_ok(words)
            if valid or not self.validates:
                table[index] = words
            ok = valid
        words = table[index] if ok or (table is not None and index < 16) else [0] * 5
        body = bytes([kind, index]) + b"".join(bytes([w & 0xFF, w >> 8]) for w in words)
        return (bytes([0x50, 48, ord("." if ok else "!")]) + body).ljust(64, b"\0")


class PackerTest(unittest.TestCase):
    def test_set_request_is_little_endian(self):
        self.assertEqual(combos.set_request(1, 3, [0x29, 0x5221, 0, 0, 250]),
                         bytes([0x50, 48, 2, 1, 3, 0x29, 0, 0x21, 0x52, 0, 0, 0, 0, 0xFA, 0]))

    def test_parsers(self):
        b = FakeBoard()
        self.assertEqual(combos.parse_info(b.send(combos.info_request())), (ord("."), True, 16, 16))
        b.send(combos.set_request(0, 2, [0x0D, 0x0E, 0, 0, 0x29]))
        self.assertEqual(combos.parse_record(b.send(combos.get_request(0, 2))),
                         (ord("."), 0, 2, [0x0D, 0x0E, 0, 0, 0x29]))
        self.assertEqual(combos.parse_commit(b.send(combos.commit_request())), (ord("."), True))
        self.assertIsNone(combos.parse_record(bytes([0x50, 47, 0x2E])))


class RoundTripTest(unittest.TestCase):
    def test_passes_restores_and_commits(self):
        b = FakeBoard()
        b.ram[0][15] = [0x0F, 0x33, 0, 0, 0x2A]
        self.assertTrue(hil_tests.test_combos_round_trip(b, lambda _m: None))
        self.assertEqual(b.ram[0][15], [0x0F, 0x33, 0, 0, 0x2A])
        self.assertEqual(b.flash[0][15], [0x0F, 0x33, 0, 0, 0x2A])   # the restore was committed

    def test_fails_when_a_bad_record_is_stored(self):
        self.assertFalse(hil_tests.test_combos_round_trip(FakeBoard(validates=False), lambda _m: None))

    def test_fails_when_the_other_half_missed_the_copy(self):
        self.assertFalse(hil_tests.test_combos_round_trip(FakeBoard(reaches_other_half=False),
                                                          lambda _m: None))

    def test_fails_on_a_build_without_combos(self):
        self.assertFalse(hil_tests.test_combos_round_trip(FakeBoard(available=False), lambda _m: None))

    def test_registered_with_the_protocol_gate(self):
        entry = next(t for t in hil_tests.TESTS if t["fn"] is hil_tests.test_combos_round_trip)
        self.assertEqual(entry["min_protocol"], 33)


if __name__ == "__main__":
    unittest.main()
