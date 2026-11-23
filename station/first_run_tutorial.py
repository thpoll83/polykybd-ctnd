"""The first-run tutorial on the rig (TIER_TUTORIAL): key presses over HID cmd 14 and
the console lines that say what the tutorial waits for. Unit-tested offline.

Needs a POLYKYBD_DEBUG image (qmk_firmware rules.mk): that build boots with
debug_enable on, and cmd 14 refuses every press without it. The qmk-test.yml
`tutorial-test` job builds that image pair and runs the runner with --tutorial.

Mirrors qmk_firmware keyboards/polykybd:
  cmd 14     lib/hid/hid_com.c hid_cmd_14: data[2..3] keycode (big-endian), data[4]
             0 = press, anything else = release. ACK '.', NACK '!' when debug_enable is
             off or the keycode is on neither the highest active layer nor the default
             layer. The lookup uses the SYNCED layer state, which follows a layer change
             within one housekeeping pass, so a press after a layer switch waits
             LAYER_SETTLE_S first.
  console    features/anim/tutorial.c: "Tutorial start (...)", one
             "Tutorial slotN: LEFT|RIGHT idx=I letter=X(C) cands=K" per letter, and
             "Tutorial phase <NAME>" at each step change (k_tut_phase_names[], the
             tut_phase_t names without the TUT_ prefix). keys_com/tutorial_board.c:
             "Tutorial skipped; displays handed back" at the end.
  keycodes   split72 default keymap: Fn is MO(_FL) on _L0, TO(_SL) is on _FL,
             KC_SETTINGS_MORE shows the advanced row of _SL where KC_EDEN is.
             KC_EDEN and KC_SETTINGS_MORE are pinned in keys_com/keycode_helper.h.
  skip       holding Esc (slot 0,0) for TUT_SKIP_HOLD_MS (1000 ms) skips the lesson
             from any step.
  flow       base/tutorial_plan.c: after the second Shift the tutorial goes to the board
             reveal and the language preview (BOARD_REVEAL ... LANG_MORE2), then the
             key tour (TOUR_WAIT) or FINALE. The layer chapter (LAYER_WAIT ...
             NOTATION) is postponed: tut_tick() no longer routes to it.
  language   while the preview shows another language, GET_LANG (cmd 7) still reports
             the user's own (poly_reported_lang(), keys_com/tutorial_board.c), and the
             skip writes it back.
"""
import re
import time

CMD_KEYPRESS = 14
CMD_GET_LANG = 7
POLY_CHANNEL = ord("P")
ACK = ord(".")

# QMK keycodes (quantum/keycodes.h) and the PolyKybd ones the test presses.
KC_A = 0x04
KC_ESC = 0x29
KC_LSFT = 0xE1
KC_RSFT = 0xE5
LAYER_FL = 5
LAYER_SL = 9
MO_FL = 0x5220 | LAYER_FL        # QK_MOMENTARY | layer
TO_SL = 0x5200 | LAYER_SL        # QK_TO | layer
KC_EDEN = 0x7FA0
KC_SETTINGS_MORE = 0x7FA3

LETTERS = 3                      # TUT_LETTERS
LAYER_SETTLE_S = 0.4             # the synced layer state follows within one pass
HOLD_S = 0.4                     # how long a Shift or Fn press is held
SKIP_HOLD_S = 1.6                # > TUT_SKIP_HOLD_MS (1.0 s)
START_TIMEOUT_S = 45.0           # Eden (~14 s) runs before the tutorial starts
STEP_TIMEOUT_S = 20.0            # the longest timed run between two waits is ~9 s
END_TIMEOUT_S = 10.0
# The board reveal, up to 11 preview items at ~9.25 s each and the two "more" screens.
PREVIEW_TIMEOUT_S = 150.0

START_LINE = "Tutorial start"
SKIPPED_LINE = "Tutorial skipped; displays handed back"
_AFTER_PREVIEW_RE = re.compile(r"Tutorial phase (TOUR_WAIT|FINALE)$")
_LETTER_RE = re.compile(r"Tutorial slot(\d+): (LEFT|RIGHT) idx=\d+ letter=([A-Za-z])\(")


def phase_line(name: str) -> str:
    """The console line for a step; a substring match, so it must be the whole name."""
    return f"Tutorial phase {name}"


def phase_pattern(name: str):
    """A regex that matches the step's line and not a longer name (SHIFT vs SHIFT_X)."""
    return re.compile(r"Tutorial phase " + re.escape(name) + r"$")


def keypress_request(keycode: int, pressed: bool) -> bytes:
    return bytes([POLY_CHANNEL, CMD_KEYPRESS, (keycode >> 8) & 0xFF, keycode & 0xFF,
                  0 if pressed else 1])


def letter_keycode(letter: str) -> int:
    """KC_A..KC_Z for an ASCII letter (the tutorial names letters from keymaps[_BL])."""
    ch = letter.upper()
    if len(ch) != 1 or not "A" <= ch <= "Z":
        raise ValueError(f"not a letter: {letter!r}")
    return KC_A + (ord(ch) - ord("A"))


def parse_letters(lines) -> list:
    """The letters from the "Tutorial slotN: ..." lines, in slot order.

    Returns an empty list unless slots 0..LETTERS-1 are all present, so a partly read
    console cannot send the test after the wrong keys."""
    found = {}
    for line in lines:
        m = _LETTER_RE.search(line)
        if m:
            found[int(m.group(1))] = m.group(3).upper()
    if all(i in found for i in range(LETTERS)):
        return [found[i] for i in range(LETTERS)]
    return []


class TutorialRun:
    """Drives one tutorial through cmd 14 and the console tap.

    ``raw`` is a RawHID, ``tap`` the console tap (station.console_log.TAP), ``log``
    the test log. ``sleep`` is injectable for the offline tests."""

    def __init__(self, raw, tap, log, sleep=time.sleep):
        self.raw = raw
        self.tap = tap
        self.log = log
        self.sleep = sleep
        self.held = []           # keycodes pressed and not yet released
        # The console position to search from for the next step a key waits on. A step
        # line can come before the previous press's reply, so each wait searches from
        # a mark taken BEFORE the press that leads to it.
        self._mark_before_wait = 0
        self.started = False     # the "Tutorial start" line was seen
        self.ended = False       # the "Tutorial skipped" line was seen

    # -- key presses --
    def press(self, keycode: int) -> bool:
        # attempts=1: a press is not idempotent, and send() retries by re-writing.
        resp = self.raw.send(keypress_request(keycode, True), attempts=1)
        ok = resp is not None and len(resp) > 2 and resp[1] == CMD_KEYPRESS and resp[2] == ACK
        if ok:
            self.held.append(keycode)
        else:
            self.log(f"  FAIL: cmd 14 press 0x{keycode:04X} not ACKed (reply {_hex(resp)})")
        return ok

    def release(self, keycode: int) -> bool:
        resp = self.raw.send(keypress_request(keycode, False), attempts=1)
        if keycode in self.held:
            self.held.remove(keycode)
        ok = resp is not None and len(resp) > 2 and resp[1] == CMD_KEYPRESS and resp[2] == ACK
        if not ok:
            self.log(f"  FAIL: cmd 14 release 0x{keycode:04X} not ACKed (reply {_hex(resp)})")
        return ok

    def tap_key(self, keycode: int, hold_s: float = 0.05) -> bool:
        if not self.press(keycode):
            return False
        self.sleep(hold_s)
        return self.release(keycode)

    def release_all(self):
        for kc in list(self.held):
            try:
                self.release(kc)
            except Exception as exc:     # cleanup must not hide the real failure
                self.log(f"  cleanup: release 0x{kc:04X} raised {exc}")

    def abandon(self):
        """After a failure: release what is held, and skip a lesson that is still running,
        so the board does not stay in the tutorial for the rest of the run."""
        self.release_all()
        if not self.started or self.ended:
            return
        mark = self.tap.mark()
        try:
            if self.tap_key(KC_ESC, SKIP_HOLD_S) and \
                    self.tap.wait_for(SKIPPED_LINE, mark, timeout=END_TIMEOUT_S) is not None:
                self.log("  cleanup: skipped the unfinished lesson")
                return
        except Exception as exc:         # cleanup must not hide the real failure
            self.log(f"  cleanup: the skip raised {exc}")
        self.log("  cleanup: could not skip the lesson; the board may still be in it")

    def read_lang(self):
        """GET_LANG's code ("enUS"), or None. Read-only, so send() may retry."""
        resp = self.raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG]))
        if resp is None or len(resp) < 3 or resp[1] != CMD_GET_LANG or resp[2] != ACK:
            return None
        return bytes(resp[3:]).split(b"\x00", 1)[0].decode("ascii", "replace")

    # -- console --
    def wait_phase(self, name: str, mark: int, timeout: float = STEP_TIMEOUT_S) -> bool:
        line = self.tap.wait_for(phase_pattern(name), mark, timeout=timeout)
        if line is None:
            self.log(f"  FAIL: no '{phase_line(name)}' line within {timeout:.0f} s")
            return False
        return True

    # -- the lesson --
    def start(self) -> list:
        """Fn + TO(_SL), show the advanced row, press KC_EDEN; return the letters, or []."""
        mark = self.tap.mark()
        self._mark_before_wait = mark
        if not self.press(MO_FL):
            self.log("  (cmd 14 needs a POLYKYBD_DEBUG image: is this the debug build?)")
            return []
        self.sleep(LAYER_SETTLE_S)
        ok = self.tap_key(TO_SL)
        ok = self.release(MO_FL) and ok
        if not ok:
            return []
        self.sleep(LAYER_SETTLE_S)
        if not self.tap_key(KC_SETTINGS_MORE):
            return []
        self.sleep(LAYER_SETTLE_S)
        if not self.tap_key(KC_EDEN):
            return []
        self.log("  KC_EDEN pressed: waiting for Eden and the tutorial to start")
        if self.tap.wait_for(START_LINE, mark, timeout=START_TIMEOUT_S) is None:
            self.log(f"  FAIL: no '{START_LINE}' line within {START_TIMEOUT_S:.0f} s")
            return []
        self.started = True
        letters = parse_letters(self.tap.since(mark))
        if not letters:
            self.log("  FAIL: the console did not name the three letters "
                     "('Tutorial slotN: ... letter=X')")
        return letters

    def run(self) -> bool:
        letters = self.start()
        if not letters:
            return False
        self.log(f"  letters asked for: {' '.join(letters)}")
        # Chapter 1: each letter is accepted from its fade-in on, so wait for the
        # fade-in, press, and expect the ripple.
        for i, letter in enumerate(letters):
            mark = self._mark_before_wait
            if not self.wait_phase("LETTER_IN", mark):
                return False
            mark = self.tap.mark()
            if not self.tap_key(letter_keycode(letter)):
                return False
            if not self.wait_phase("RIPPLE", mark):
                self.log(f"  (letter {i + 1}, {letter}, was pressed but not accepted)")
                return False
            self._mark_before_wait = mark
        # Chapter 2: left Shift, then right Shift.
        for name, key in (("SHIFT_WAIT", KC_LSFT), ("SHIFT_AGAIN", KC_RSFT)):
            if not self.wait_phase(name, self._mark_before_wait):
                return False
            mark = self.tap.mark()
            if not self.tap_key(key, HOLD_S):
                return False
            if not self.wait_phase("SHIFT_SWEEP", mark):
                self.log(f"  (0x{key:04X} was held but the board did not sweep)")
                return False
            self._mark_before_wait = mark
        # Chapter 3: the board reveal and the language preview run on timers. The
        # layer chapter is postponed in the firmware, so no key is asked for here.
        if not self.wait_phase("BOARD_REVEAL", self._mark_before_wait):
            return False
        mark = self._mark_before_wait
        line = self.tap.wait_for(_AFTER_PREVIEW_RE, mark, timeout=PREVIEW_TIMEOUT_S)
        if line is None:
            self.log(f"  FAIL: the preview did not reach TOUR_WAIT or FINALE within "
                     f"{PREVIEW_TIMEOUT_S:.0f} s")
            return False
        shown = sum(1 for ln in self.tap.since(mark) if phase_pattern("LANG_SHOW").search(ln))
        if shown == 0:
            self.log("  FAIL: the preview ran without a single LANG_SHOW step")
            return False
        self.log(f"  preview: {shown} item(s) shown, then {line.split()[-1]}")
        # Skip: hold Esc past TUT_SKIP_HOLD_MS.
        mark = self.tap.mark()
        if not self.tap_key(KC_ESC, SKIP_HOLD_S):
            return False
        if self.tap.wait_for(SKIPPED_LINE, mark, timeout=END_TIMEOUT_S) is None:
            self.log(f"  FAIL: holding Esc for {SKIP_HOLD_S} s did not end the lesson "
                     f"(no '{SKIPPED_LINE}' line)")
            return False
        self.ended = True
        self.log("  chapters 1-2 answered over cmd 14, the preview ran, skipped with a "
                 "held Esc, displays handed back")
        return True


def _hex(resp) -> str:
    return "none" if resp is None else bytes(resp[:4]).hex()
