# SPDX-License-Identifier: GPL-2.0-only
"""Concrete HIL test cases for the PolyKybd test/deploy station.

Each test is a dict ``{"name": str, "fn": callable}``. ``fn`` receives
``(raw_hid: RawHID, log: Callable[[str], None])`` and returns a bool — True for
pass, False for fail. Exceptions are caught by the runner and reported as a
failure, so a test may also simply assert.

Tolerating expected changes
---------------------------
A test dict may carry optional **gate** keys so a check that is *known* to need a
firmware change which isn't deployed yet does not hard-fail the run:

* ``"min_protocol": N`` — the test is SKIPPED (not failed) unless the flashed
  firmware advertises ``PROTOCOL_VERSION`` >= N in its GET_ID string. It un-skips
  automatically the moment a firmware reporting >= N is flashed.
* ``"min_fw": "0.8.22"`` — same idea, gated on ``FW_VERSION`` (for changes not
  tied to a protocol bump).
* ``"xfail": "reason"`` — run the test but treat a failure as XFAIL (tolerated)
  and an unexpected pass as XPASS (surfaced loudly so the marker gets removed).
  For "details" not visible in GET_ID.

SKIP / XFAIL / XPASS never fail the overall run — only a genuine FAIL does. The
device's advertised protocol/fw version is parsed from GET_ID via
``parse_device_caps`` and the gate decision is in ``skip_reason`` (both pure and
unit-testable); the runner reads the caps lazily, after the fresh-boot test has
consumed the one-shot ``*`` marker.

A test dict may also carry ``"needs_console": True`` when it asserts on what the
firmware *printed* (``station/console_log.py`` taps the QMK HID console). That is
a property of the RUN, not of the device, so the runner merges it into the caps
dict; a run whose console never came up SKIPs those tests rather than letting them
assert nothing and report a green they did not earn.

The suite exercises every Raw HID command that can be checked in a meaningful,
side-effect-free way on the unattended rig (no human at the keys, no camera, no
GPIO key-matrix injection): the read-only identity/state
queries (GET_ID/GET_LANG/GET_LANG_LIST_PACKED/GET_DEFAULT_LAYER), the ACK/NACK
error and bounds paths (unknown command, the retired ASCII GET_LANG_LIST which
must now NACK, out-of-range brightness, invalid unicode mode), the state commands
with a clean round-trip + restore (language, overlay
flags), and the upload + soak paths that guard documented firmware regressions
(overlay upload — plain, core1 RLE, the two-packet continuation and ROI — keeping
the master alive; rapid GET_ID stress; a bridged soak read against the firmware's
own split-link health counter). Commands that can't be asserted without side
effects or extra infrastructure are catalogued in ``docs/FUTURE_TESTS.md``
(host-driven KEYPRESS injects real keystrokes; ENTER_BOOTLOADER ends the session;
overlay *render* checks need a camera or firmware read-back; SETTING handedness
needs flash control -- READING it no longer does, see the banner test below). Two checks live in the runner rather than here because they need
the GPIO or the flashed image: the reboot-persistence power cycle and the
firmware-update stage+verify.

Run them via the CLI::

    python -m station.test_runner --left left_hil.uf2 --right right_hil.uf2

See ``docs/FUTURE_TESTS.md`` for the planned-but-not-yet-implemented backlog.
"""
import os
import re
import time
from typing import Callable

from .console_log import TAP, classify_link_health, link_delta
from .hid import RawHID, enumerate_raw_interfaces
from .iso_lang_country import decode_packed

# --- Raw HID protocol constants (mirror keyboards/polykybd/hid_com.c
# and PolyKybdHost's polyhost/device/command_ids.py) ---
# A command report is: data[0] = 'P' (channel marker), data[1] = command id,
# data[2..] = payload. Responses echo "P<id><status>...", where status is
# '.' (ACK) / '!' (NACK), and GET_ID uses '*' instead of '.' on the first
# exchange after a fresh boot.
POLY_CHANNEL = ord("P")  # 0x50

CMD_GET_ID                  = 6   # device identity string
CMD_GET_LANG                = 7   # current language code ("llCC")
CMD_GET_LANG_LIST           = 8   # legacy ASCII list — RETIRED in protocol v2; firmware NACKs it
CMD_GET_LANG_LIST_PACKED    = 27  # language codes as 2-byte ISO index pairs (protocol v2+; the only list cmd)
CMD_CHANGE_LANG             = 9   # set language by 4-char code
CMD_SEND_OVERLAY            = 10  # uncompressed overlay segment (no ACK)
CMD_SEND_OVERLAY_MAPPING_W  = 33  # overlay mapping, host-chosen value width (protocol v12+; no ACK)
CMD_OVERLAY_FLAGS_ON        = 11  # set overlay flag bits
CMD_OVERLAY_FLAGS_OFF       = 12  # clear overlay flag bits
CMD_SET_BRIGHTNESS          = 13  # set OLED contrast (0..FULL_BRIGHT)
CMD_IDLE_STATE              = 15  # start (1) / stop (0) display idle
CMD_START_COMPRESSED_OVERLAY = 16 # first RLE-compressed overlay packet (core1)
CMD_CONT_COMPRESSED_OVERLAY = 17  # continuation RLE packet (same fragment context)
CMD_START_ROI_OVERLAY       = 18  # first ROI packet: 5-byte ROI header then data
CMD_CONT_ROI_OVERLAY        = 19  # continuation ROI packet (same fragment context)
CMD_SEND_OVERLAY_MAPPING    = 21  # overlay mapping, fixed 10-bit values (no ACK)
CMD_REPLAY_ANIM             = 31  # replay the one-shot startup ("Eden") animation
CMD_SET_UNICODE_MODE        = 20  # unicode input mode (0..4)
CMD_GET_DEFAULT_LAYER       = 22  # current default layer index
CMD_IDLE_STYLE              = 28  # get/set idle (anti-burn-in) display style (protocol v4+)
CMD_SET_OS                  = 29  # get/set active host-OS identity (protocol v7+)
CMD_GLYPH_SCRIPT            = 30  # get/set glyph-script override (v9+ tengwar; v10 adds 9 more scripts)
GLYPH_SCRIPT_MAX            = 10  # highest valid poly_glyph_script value (BRAILLE) as of protocol v10
CMD_GLYPH_SIZE              = 34  # get/set the keycap legend size (protocol v13+)
CMD_MACRO_INFO              = 36  # count, label stride, capacity, bytes used (v15+)
CMD_MACRO_BODY              = 37  # windowed read/write of the shared body buffer (v15+)
CMD_MACRO_LOOK              = 38  # get/set one macro's whole keycap look (v15+)
CMD_CRASH_RECORD            = 39  # read/clear the firmware crash record (v16+)
CMD_SEND_PRC_OVERLAY        = 41  # PRC (Predictive Range Coding) overlay records (v19+; no ACK)
CMD_FILL_POOL_FROM_ICON     = 42  # fill pool slots from the flash icon library (v20+; REPLIED)
CMD_REBOOT                  = 43  # reboot both halves, nothing persisted (v22+; ACK before reset)
MACRO_LOOK_HEADER           = 9   # id, caption length, style, 4 little-endian icon bytes
MACRO_STYLE_INDEX           = 0   # "M3" above the caption -- the default
MACRO_STYLE_ICON            = 1   # a chosen glyph above the caption
MACRO_STYLE_TEXT            = 2   # the caption alone, at the largest face that fits
MACRO_STYLE_ICON_ONLY       = 3   # the icon alone, filling the whole key
MACRO_STYLE_UNKNOWN         = 200 # far past any plausible POLY_MACRO_STYLE_COUNT
MACRO_ICON_PROBE            = 0x1F4E7  # an emoji-plane codepoint: three non-zero bytes
GLYPH_SIZE_MAX              = 2   # highest valid poly_glyph_size value (LARGE)
CMD_GET_LAYER_NAMES         = 35  # read the remappable layers' names (protocol v14+)
LAYER_NAME_MAX              = 8   # firmware POLY_LAYER_NAME_MAX: longest name, sans NUL
LAYER_NAMES_HEADER          = 2   # the total byte and the count byte
VIA_DYNAMIC_KEYMAP_GET_LAYER_COUNT = 0x11  # QMK id_dynamic_keymap_get_layer_count
POLY_OS_COUNT               = 8   # enum poly_os values 0..7 valid (UNKNOWN/WIN/MAC/LINUX/ANDROID/IOS-reserved/LINUX_GNOME/LINUX_KDE); firmware SET_OS accepts arg < POLY_OS_COUNT
# Font-pack flash transport (protocol v6+; same BEGIN/CHUNK/COMMIT staging as the
# firmware update, reused per-bundle). Reply status byte is reply[2] ('.'/'!'/'~').
CMD_FONTPACK_BEGIN          = 0x50  # data[2..5]=pack_size, [6..9]=pack_crc32, [10]=bundle_id
CMD_FONTPACK_CHUNK          = 0x51  # data[2..5]=offset, [6..]=56 bytes
CMD_FONTPACK_COMMIT         = 0x52  # verify staged CRC + reload (no reboot); reply[3..4]=content_version

# FONTPACK_COMMIT status byte (firmware `hid_fontpack.h` FONTPACK_COMMIT_*).
# ⚠️ Three-valued since qmk#209 — it is NOT ok-or-'!' any more, and the difference
# is the whole point of that change: 'R' means the master's finalize REFUSED the
# image (staged CRC / not a valid PlyF) and re-sending cannot help, while 'L' means
# the master committed fine — its copy is live and reply[3..4] carries the real
# content_version — and only the SLAVE's ack was lost on the split link, which is
# retryable. Collapsing them is exactly the misdiagnosis #209 removed: the field
# report that started it was "CRC mismatch or the font pack was rejected" for a pack
# whose CRC was perfect and whose data was already on the keyboard.
FONTPACK_COMMIT_OK       = ord('.')   # both halves finalized
FONTPACK_COMMIT_REJECTED = ord('R')   # master refused the image -> data failure
FONTPACK_COMMIT_NO_SLAVE = ord('L')   # master live, slave ack lost -> link failure
FONTPACK_COMMIT_LEGACY   = ord('!')   # pre-#209 firmware: "one of the above"


def describe_fontpack_commit(reply) -> str:
    """Human-readable diagnosis of a FONTPACK_COMMIT reply, for the FAIL log.

    Pure and total: any shape of reply gets a sentence, because this runs on the
    failure path where the least useful thing to print is the raw bytes alone.
    """
    if not reply or len(reply) < 3:
        return "no reply (the master never answered COMMIT)"
    status = reply[2]
    if status == FONTPACK_COMMIT_OK:
        return "ok"
    if status == FONTPACK_COMMIT_REJECTED:
        return ("'R' — the MASTER refused the image (staged CRC mismatch, or not a "
                "valid PlyF). A data failure: re-sending the same bytes cannot help")
    if status == FONTPACK_COMMIT_NO_SLAVE:
        return ("'L' — the master committed (its copy is LIVE) but the SLAVE did not "
                "ack within the bridge retries. A split-LINK failure, not a data one; "
                "COMMIT is safe to retry")
    if status == FONTPACK_COMMIT_LEGACY:
        return ("'!' — legacy pre-#209 firmware, which collapsed rejected and "
                "slave-unconfirmed into one byte; flash a newer image to tell them apart")
    return f"unknown status byte {status:#04x}"
FONTPACK_CHUNK_SIZE         = 56    # payload bytes/chunk (matches firmware FW_UP_CHUNK_SIZE)
# Doom easter egg pseudo bundles (same BEGIN/CHUNK/COMMIT transport, routed to the
# game-data / engine-pack resource slots). Lockstep with qmk base/fw_staging.h
# FONTPACK_BUNDLE_DOOMWAD/_DOOMPACK and PolyKybdHost hid_fontpack.py.
DOOMWAD_BUNDLE_ID           = 0x7F
DOOMPACK_BUNDLE_ID          = 0x7E
# BEGIN erase-poll cap for a doom slot. The engine-pack slot is ~210 KB / 52
# sectors, and the deferred erase is paced by these 0.3 s re-polls (the firmware
# advances it in housekeeping between BEGIN re-polls), so the erase spans ~17
# polls of wall-clock and readiness (`.`) lags the `erase complete` printf by a
# further pass or two. On the FW-9 set the doom slot is flashed THREE times in a
# row (valid / tampered / unsigned), and the 3rd erase drifted past the old
# 20-attempt (~6 s) cap on qmk#249 — the erase completed but BEGIN still reported
# `~` at attempt 20, so the test failed at "FONTPACK_BEGIN never became ready"
# even though nothing was wrong. 60 attempts (~18 s) gives ~3x headroom over the
# observed ~20 without masking a real hang (a genuinely stuck erase never logs
# `erase complete` and still fails, just later). Only the doom slot needs this;
# the font-pack wipe flashes a 32-byte empty pack (1-sector erase) and keeps 20.
DOOM_BEGIN_ERASE_ATTEMPTS   = 60
# ...but that big budget is ONLY for the erase-busy (`~`) path, which is cheap
# (~0.3 s/poll). A NO-reply is the dead/hung-keyboard signal, and it is
# EXPENSIVE: raw.send() already retries attempts×timeout_ms internally (3×15 s
# here) before returning None, so one no-reply outer iteration is ~45 s. Sharing
# the 60-cap across both would let a dead board stall ONE flash for ~45 min
# before failing (was ~15 min at 20) — 3x slower HIL diagnostics (Greptile,
# ctnd#81). So consecutive no-replies get their own tight cap and fail fast
# (~2 min); a `~` in between means the erase is progressing, so it resets the
# counter — an occasional dropped reply on a healthy-but-flaky link is tolerated.
DOOM_BEGIN_NO_REPLY_MAX     = 3

# VIA "reset dynamic keymap" report (bare command id, NOT a 'P' command — see
# test_runner.VIA_DYNAMIC_KEYMAP_RESET). data[0]==0x06 -> legacy_command_kb ->
# dynamic_keymap_reset(); the firmware echoes the request back (no "P<cmd>." ACK).
VIA_DYNAMIC_KEYMAP_RESET    = 0x06

ACK        = ord(".")
NACK       = ord("!")

# --- suite tiers --------------------------------------------------------------
# Most checks are cheap (a report or two) and run on every PR. A few are slow by
# nature — they wait out a 10 s idle fade, a 14 s animation, a 450-frame link
# soak, or a power cycle — and together they add most of a minute to a run that
# gates every push. Those carry ``"tier": TIER_EXTENDED`` and only run when the
# run ASKS for them (``test_runner --extended``, the CI label, or the touch UI's
# Extended toggle), so the default gate stays fast and the deep checks are there
# for a release or a change big enough to want them.
#
# ⚠️ Tier is about COST, not importance: an extended test is one that is slow or
# disruptive, never one that is flaky or unproven. Anything unreliable belongs in
# ``docs/FUTURE_TESTS.md`` until it is trustworthy, not in a tier nobody runs.
TIER_DEFAULT  = "default"
TIER_EXTENDED = "extended"
# A THIRD opt-in tier, separate from EXTENDED, for the signed-DOOM-pack checks
# (FW-9). They are opt-in for two reasons at once: they flash a ~230 KB .plyx over
# HID and drive the idle screensaver (slow, like EXTENDED), AND they need a signed
# pack artifact CI builds only on demand (see --plyx-valid / the `hil-doom` label).
TIER_DOOM     = "doom"
FRESH_BOOT = ord("*")    # GET_ID status byte when the firmware just (re)booted

# Firmware facts (keyboards/polykybd/{config.h,base/com.h}).
FULL_BRIGHT          = 50    # max OLED contrast; > this is rejected (NACK)
# SET_BRIGHTNESS flags byte (protocol v5+, data[2]; mirror base/com.h). 0 = the
# legacy persisted set. VOLATILE = a daylight/auto value (applied only while auto
# mode is engaged, never persisted); AUTO_ON / AUTO_OFF engage/leave host-auto.
BR_FLAG_VOLATILE     = 1 << 0
BR_FLAG_AUTO_ON      = 1 << 1
BR_FLAG_AUTO_OFF     = 1 << 2
MAX_LAYERS           = 12    # DYNAMIC_KEYMAP_LAYER_COUNT (split72/config.h). Was
                             # 14 here long after the firmware dropped to 12 — a
                             # loose sanity bound, so nothing failed. Same drift
                             # class as the host's layer_names.yaml.
DISPLAY_OVERLAYS_BIT = 0x01  # overlay_flag DISPLAY_OVERLAYS (base/com.h)
KC_A                 = 0x04  # QMK keycode for 'A'; A..Z = 0x04..0x1D
KC_P                 = 0x13  # 'P': on the RIGHT half of split72, so its upload is bridged
NUM_SEGMENTS         = 6     # NUM_SEGMENTS_PER_OVERLAY
PLAIN_SEG_BYTES      = 60    # data bytes per plain overlay report (64 - 4 header, protocol 11)
OVERLAY_BYTES        = 360   # NUM_SEGMENTS_PER_OVERLAY * BYTES_PER_SEGMENT
COMPRESSED_TEST_KEYS = 8     # KC_A..KC_H — exercise the core1 path repeatedly
# Variable-width overlay mapping (cmd 33, protocol v12+). Mirrors the firmware's
# OVERLAY_MAP_W_HDR / OVERLAY_MAP_W_BYTES / OVERLAY_MAP_IDX_CNT (config.h).
OVERLAY_MAP_W_HDR    = 3     # channel + cmd + width byte
OVERLAY_MAP_W_BYTES  = 64 - OVERLAY_MAP_W_HDR   # 61 data bytes per report
OVERLAY_MAP_IDX_CNT  = 1440  # NUM_OVERLAYS * NUM_VARIATIONS_WITH_MAP (90 * 16) at v12
# The widths PolyKybdHost actually emits — 11 is the ceiling (max `from` is
# 90*15+89 = 1439 < 2048). 9 and 11 are the load-bearing ones: gcd(w,8)==1, so
# they are the only widths that walk all eight bit offsets and reach a third byte.
OVERLAY_MAP_TEST_WIDTHS = (8, 9, 10, 11)
# Outside OVERLAY_MAP_WIDTH_MIN(8)..MAX(16) — the firmware must drop these.
OVERLAY_MAP_BAD_WIDTHS  = (7, 17)
# cmd 11 action bits that restore the power-on state: MAPPING_RESET (1<<7)
# re-establishes the identity mapping, USAGE_RESET (1<<6) clears the "this
# display position has an overlay" bits (base/com.h). Both self-clear.
OVERLAY_MAPPING_RESET_BITS = (1 << 7) | (1 << 6)

# Compressed-overlay packet framing (config.h COMPRESSED_START / COMPRESSED_MAX).
# The first packet (cmd 16) carries keycode+modifier after the 2-byte header, so
# only 60 payload bytes fit; a continuation (cmd 17) has no such prefix and
# carries 62. A stream longer than COMPRESSED_START is what forces cmd 17 to be
# used at all — see test_compressed_overlay_two_packets.
COMPRESSED_START     = 60
COMPRESSED_MAX       = 62
# ROI packet framing (config.h ROI_START / ROI_MAX). The first packet (cmd 18)
# carries the 5-byte ROI header, leaving 57 payload bytes; cmd 19 carries 62.
ROI_HDR_BYTES        = 5
ROI_START            = 64 - 2 - ROI_HDR_BYTES   # 57
ROI_MAX              = 64 - 2                   # 62
SCREEN_WIDTH         = 72    # keycap OLED, px
SCREEN_HEIGHT        = 40
IDLE_STYLE_IDDQD     = 2     # enum poly_idle_style (state.h): the DOOM screensaver
IDLE_STYLE_EDEN      = 3     # enum poly_idle_style (state.h): pulse/jitter/iddqd/eden
# Firmware version that introduced the Eden animation + IDLE_STYLE_EDEN, for the
# min_fw gate (the feature bumps no PROTOCOL_VERSION, so GET_ID's P<n> can't gate it).
EDEN_MIN_FW          = "0.11.0"
# One-shot intro length (anim/startup_anim.c SA_TOTAL_MS = 5000+5000+3200+1000).
ANIM_TOTAL_S         = 14.2
OVERLAY_MAP_IDX_BITS = 10    # cmd 21's fixed value width
HID_DATA_MAX         = 62    # payload bytes in a cmd-21 mapping report
# Split-link soak: every cmd-21 report costs exactly one bridged frame, and the
# firmware prints its health summary every LINK_STATS_LOG_EVERY (200) frames — so
# ~450 reports guarantees at least two summaries, i.e. one full measurable window.
LINK_SOAK_REPORTS    = 450
LINK_SOAK_BATCH      = 50    # written per open handle, with a breath in between
# Animation / idle timing. The intro is ~14.2 s, so recovery is given a wide
# margin; "recovered" is a short streak of fast replies, the same shape as the
# runner's settle gate (one fast reply can land between two frames).
ANIM_BURST_SENDS      = 20
ANIM_RECOVERED_MS     = 200    # a reply this fast means no frame is being rendered
ANIM_RECOVERED_STREAK = 3
ANIM_RECOVER_MARGIN_S = 20.0
EDEN_BURST_SENDS      = 30
# The idle fade runs for FADE_TRANSITION_TIME (10 s) after the backdated start
# before the transition fires; wait comfortably past it.
IDLE_ENGAGE_TIMEOUT_S = 25.0
# The signed-pack tests wait for the idle fade to complete AND the loader to run
# its ~230 KB CRC + one SHA-512 before it logs a verdict — a touch longer than the
# bare idle-engage window.
DOOM_LOAD_TIMEOUT_S   = 30.0
# The soak's frames are sent in a few hundred ms, but the firmware prints its
# summary from send_to_bridge, i.e. as it drains them.
LINK_SUMMARY_TIMEOUT_S = 30.0


# --- device capability gate ---------------------------------------------------
# The firmware's GET_ID reply (cmd 6) names the board, firmware version and
# protocol version, e.g. the text after the "P\x06." header reads
#   "Split72 0.8.21 P2 HW1 "
# (see keyboards/polykybd/hid_com.c: name = "P\x06.Split72 " FW_VERSION
#  " P" STR(PROTOCOL_VERSION) " HW" STR(DEVICE_VER) " "). That advertised version
# is the "has the update landed yet?" signal the per-test gate keys read.
_CAPS_RE = re.compile(r"Split72\s+(?P<fw>\S+)\s+P(?P<proto>\d+)")


def parse_device_caps(identity: str) -> dict:
    """Extract ``{'fw': '0.8.21', 'protocol': 2}`` from a GET_ID identity string
    (the text *after* the ``P\\x06.`` header). Returns ``{}`` if it doesn't match
    — e.g. an older firmware that didn't advertise a ``P<n>`` token — so the gate
    can fall back to running the test rather than skipping on an unparsable line.
    """
    m = _CAPS_RE.search(identity or "")
    if not m:
        return {}
    return {"fw": m.group("fw"), "protocol": int(m.group("proto"))}


def _ver_tuple(v: str) -> tuple:
    """Leading dotted-numeric prefix of a version string as an int tuple, for
    comparison (``"0.8.21"`` -> ``(0, 8, 21)``). Stops at the first non-numeric
    component so a suffix like ``"0.8.21-rc1"`` compares on ``(0, 8, 21)``."""
    parts = []
    for p in re.split(r"[.\-]", (v or "").strip()):
        if p.isdigit():
            parts.append(int(p))
        else:
            break
    return tuple(parts)


def skip_reason(test: dict, caps: dict):
    """Return a human-readable reason to SKIP ``test`` given the device ``caps``
    (from ``parse_device_caps``), or ``None`` to run it.

    A gate only skips when the device *positively* reports a version below the
    requirement. If ``caps`` is empty (GET_ID unreadable/unparsable) the gate
    declines to skip — better to run the test and surface a real failure than to
    hide it behind an unverifiable gate.
    """
    min_proto = test.get("min_protocol")
    dev_proto = caps.get("protocol")
    if min_proto is not None and dev_proto is not None and dev_proto < min_proto:
        return f"needs protocol >= {min_proto}, device reports P{dev_proto}"
    min_fw = test.get("min_fw")
    dev_fw = caps.get("fw")
    if min_fw and dev_fw and _ver_tuple(dev_fw) < _ver_tuple(min_fw):
        return f"needs firmware >= {min_fw}, device reports {dev_fw}"
    # A test that asserts on firmware console output can only do so when the
    # console actually came up (CONSOLE_ENABLE in the build, and the interface
    # opened this run). SKIP rather than run it: without console lines such a test
    # would assert nothing and report a green it did not earn — the "reads as
    # coverage" failure this whole gate mechanism exists to avoid. Only skip on a
    # POSITIVE False, so a caps dict that never learned about the console (an older
    # runner, a unit test) runs the test as before.
    if test.get("needs_console") and caps.get("console") is False:
        return "needs the QMK HID console, which did not come up this run"
    # Cost gate (see TIER_EXTENDED). Unlike the version gates this one is
    # fail-CLOSED: an extended test is skipped unless the run positively opted in,
    # because the whole point is that the default PR gate does not pay for it.
    if test.get("tier") == TIER_EXTENDED and not caps.get("extended"):
        return "extended suite — re-run with --extended (or the hil-extended label)"
    # The doom-pack tier is fail-closed like EXTENDED, but on its OWN opt-in: it
    # also needs a signed .plyx artifact that only the `doom` CI path produces.
    if test.get("tier") == TIER_DOOM and not caps.get("doom"):
        return "doom suite — re-run with --doom + a signed --plyx-valid (or the hil-doom label)"
    return None


def _rle_compress(byte_stream: bytes) -> bytes:
    """MSB-first bit run-length encoder.

    Produces a stream the firmware's ``rle_decompress`` accepts (same scheme as
    PolyKybdHost ``rle_util.rle_compress``): a run is one byte whose high bit is
    the run's value and whose low 7 bits are the length (1..127); longer runs
    repeat. Zero-length runs are skipped, so every emitted byte is a real 1..127
    run. Used to build a valid compressed overlay for the core1 liveness guard
    without depending on PolyKybdHost.
    """
    out = bytearray()
    current = 0x00
    count = 0

    def flush(cnt: int, bit: int) -> None:
        if cnt == 0:
            return  # never emit a 0-length run (e.g. when the first bit is set)
        while cnt > 127:
            out.append(127 if bit == 0 else 255)
            cnt -= 127
        out.append(cnt if bit == 0 else 128 + cnt)

    for byte in byte_stream:
        for _ in range(8):
            if (byte & 0x80) == current:
                count += 1
            else:
                flush(count, current)
                current = byte & 0x80
                count = 1
            byte = (byte << 1) & 0xFF
    flush(count, current)
    return bytes(out)


# A fully-blank (all-zero) 360-byte overlay compresses to 23 bytes
# (22 × 0x7F + 0x56) and decodes back to exactly 360 zero bytes — small enough
# to ship in a single cmd-16 packet, harmless to render.
_BLANK_OVERLAY_RLE = _rle_compress(bytes(OVERLAY_BYTES))


def _resp_ok(response, cmd: int, log: Callable[[str], None], expect_status=ACK) -> bool:
    """Validate a "P<cmd><status>…" reply. ``expect_status=None`` skips the
    status check (e.g. liveness probes that accept ACK or fresh-boot)."""
    if response is None:
        log(f"  FAIL: no response (timeout) to cmd {cmd:#04x}")
        return False
    if len(response) < 3:
        log(f"  FAIL: response too short ({len(response)} bytes) to cmd {cmd:#04x}: {response!r}")
        return False
    if response[0] != POLY_CHANNEL or response[1] != cmd:
        log(f"  FAIL: bad header {response[0]:#04x} {response[1]:#04x} (want 'P' {cmd:#04x})")
        return False
    if expect_status is not None and response[2] != expect_status:
        log(f"  FAIL: status {response[2]:#04x} != expected {expect_status:#04x} ('{chr(expect_status)}')")
        return False
    return True


def _reply_text(response) -> str:
    """Decode the ASCII payload after the 3-byte header, stopping at the first
    NUL (replies are NUL-padded to 64 bytes)."""
    return bytes(response[3:]).split(b"\x00", 1)[0].decode("ascii", "replace")


def parse_fontpack_versions(response):
    """Decode the per-bundle font-pack version block a GET_ID reply carries on
    PROTOCOL_VERSION >= 6: AFTER the NUL-terminated id string,
    ``['V'][count][u16 little-endian content_version x count]`` in bundle-slot
    order. Returns ``{slot: content_version}`` or ``None`` if absent/malformed
    (pre-v6 firmware has no block). Pure — unit-testable without hardware."""
    raw = bytes(response)
    nul = raw.find(b"\x00", 3)               # id string starts after the 3-byte header
    if nul < 0:
        return None
    p = nul + 1
    if p + 2 > len(raw) or raw[p] != ord("V"):
        return None
    count = raw[p + 1]
    p += 2
    if count == 0 or p + count * 2 > len(raw):
        return None
    return {i: int.from_bytes(raw[p + 2 * i:p + 2 * i + 2], "little") for i in range(count)}


def _master_alive(raw: RawHID, log: Callable[[str], None], attempts: int = 3) -> bool:
    """GET_ID with a few retries. After an upload (or any EEPROM/refresh command)
    the master finishes an EEPROM write + a full keycap display refresh on its
    main loop and briefly stops servicing HID — long enough to miss a single
    GET_ID. Retrying tolerates that transient busy window; a genuine hang (e.g. a
    core1 wedge) still fails every attempt."""
    for i in range(attempts):
        resp = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
        if _resp_ok(resp, CMD_GET_ID, log, expect_status=None):
            if i:
                log(f"  master answered GET_ID on attempt {i + 1}/{attempts}")
            return True
        log(f"  GET_ID attempt {i + 1}/{attempts}: no answer (master busy?) — retrying")
    return False


def _send_with_retry(raw: RawHID, request: bytes, cmd: int,
                     log: Callable[[str], None], attempts: int = 3,
                     expect_status=None):
    """Send a 'P<cmd>' query up to `attempts` times, returning the first reply
    that passes _resp_ok. An EEPROM-writing command (CHANGE_LANG -> save_user_latin
    + slave lang sync) can leave the master briefly unable to answer; retrying
    tolerates that window the way _master_alive() does for uploads. A genuine hang
    still fails every attempt. Returns the last reply (caller logs the failure)."""
    resp = None
    for i in range(attempts):
        resp = raw.send(request)
        if _resp_ok(resp, cmd, lambda *_a: None, expect_status=expect_status):
            if i:
                log(f"  cmd {cmd:#04x} answered on attempt {i + 1}/{attempts}")
            return resp
        log(f"  cmd {cmd:#04x} attempt {i + 1}/{attempts}: no answer (master busy?) — retrying")
        time.sleep(0.05)
    return resp


# --- structural / enumeration -------------------------------------------------

def test_single_master_enumerates(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Exactly one half presents a Raw HID interface (the master).

    This is the headline assertion for the POLYKYBD_HIL firmware build. On the
    rig both halves are USB-powered, so stock VBUS-based master detection makes
    *both* enumerate as master. The HIL build forces left=master/right=slave by
    handedness and has the slave usb_disconnect(), leaving a single Raw HID
    interface. Zero interfaces = neither half came up as master (or USB/flash
    failed); two = the force-master override did not take effect.
    """
    interfaces = enumerate_raw_interfaces()
    count = len(interfaces)
    for d in interfaces:
        # manufacturer_string / product_string help eyeball which half answered
        log(f"  raw iface: path={d['path']!r} product={d.get('product_string')!r}")
    log(f"raw HID interfaces present: {count} (expected exactly 1)")
    if count == 0:
        log("  FAIL: no master enumerated — check flash, USB cabling, EE_HANDS marker")
    elif count > 1:
        log("  FAIL: multiple masters — POLYKYBD_HIL force-master override not effective")
    return count == 1


# --- identity / fresh-boot ----------------------------------------------------

def test_fresh_boot_marker(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The first GET_ID after a flash reports a fresh boot ('*'), the next ACKs.

    The firmware sets ``s_fresh_boot`` on boot and clears it after the first
    GET_ID, so the host can tell a real reflash from a stale enumeration left
    over from a previous run even when USB never dropped. This must therefore be
    the *first* GET_ID the suite sends — it runs before ``raw HID GET_ID`` in
    TESTS, and consumes the marker, which is why the later test accepts a plain
    ACK. A first byte of '.' here means the master was already queried (or not
    actually reflashed) before the suite started.
    """
    # ⚠️ attempts=1 — this is the ONE send() in the suite that must NOT retry.
    # GET_ID is not idempotent: it consumes the one-shot fresh-boot marker. If
    # the reply to the first write is dropped, the firmware has *already* cleared
    # the marker, so send()'s retry re-issues GET_ID and gets a perfectly correct
    # '.' — which this test would then report as wrong data, failing the run for
    # a lost reply. Keeping it single-attempt leaves a dropped reply a *timeout*,
    # which the runner classifies as a non-failing WARN (see test_runner.py).
    first = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]), attempts=1)
    log(f"GET_ID #1: {first!r}")
    if not _resp_ok(first, CMD_GET_ID, log, expect_status=None):
        return False
    if first[2] != FRESH_BOOT:
        log(f"  FAIL: first GET_ID status {first[2]:#04x} ('{chr(first[2])}') is not '*' — "
            "firmware did not report a fresh boot (already queried, or not reflashed?)")
        return False
    second = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
    log(f"GET_ID #2: {second!r}")
    if not _resp_ok(second, CMD_GET_ID, log, expect_status=ACK):
        log("  FAIL: fresh-boot marker did not clear to '.' on the second GET_ID")
        return False
    return True


def test_get_id(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The master answers GET_ID with a well-formed Split72 identity string.

    Sends ``P\\x06`` and expects a reply that echoes the channel + command id,
    carries an ACK ('.') or fresh-boot ('*') status byte, and names the board.
    Proves the surviving master is not just enumerated but actually servicing
    the Raw HID command dispatcher.
    """
    response = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
    log(f"GET_ID response: {response!r}")
    if response is None:
        log("  FAIL: no response to GET_ID")
        return False
    if len(response) < 3:
        log(f"  FAIL: response too short ({len(response)} bytes), expected at least 3")
        return False
    if response[0] != POLY_CHANNEL or response[1] != CMD_GET_ID:
        log(f"  FAIL: bad header {response[0]:#04x} {response[1]:#04x}, want 'P' 0x06")
        return False
    if response[2] not in (ACK, FRESH_BOOT):
        log(f"  FAIL: status byte {response[2]:#04x} is neither ACK '.' nor '*'")
        return False
    # Identity string follows the 3-byte header; firmware sends "...Split72 <ver>..."
    identity = _reply_text(response)
    log(f"  identity: {identity!r}")
    if "Split72" not in identity:
        log("  FAIL: identity string does not contain 'Split72'")
        return False
    return True


def test_fontpack_version_block(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GET_ID (v6+) appends a per-bundle font-pack version block after the id string.

    Validates the ``['V'][count][u16 x count]`` block is present and well-formed:
    a plausible bundle count with contiguous slot ids 0..count-1. The host reads
    these per-bundle ``content_version``s and flashes only the bundles the
    keyboard is missing or behind on, so a malformed/absent block on v6 firmware
    would silently disable per-bundle font flashing. Read-only.
    """
    response = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
    log(f"GET_ID response: {response!r}")
    if not _resp_ok(response, CMD_GET_ID, log, expect_status=None):
        return False
    versions = parse_fontpack_versions(response)
    if versions is None:
        log("  FAIL: no font-pack version block after the id string (expected on v6+)")
        return False
    log(f"  font-pack bundle versions: {versions}")
    if not 1 <= len(versions) <= 16:
        log(f"  FAIL: implausible bundle count {len(versions)}")
        return False
    if sorted(versions.keys()) != list(range(len(versions))):
        log(f"  FAIL: bundle slots not contiguous 0..{len(versions) - 1}: {sorted(versions)}")
        return False
    return True


# --- read-only state queries --------------------------------------------------

def test_get_lang(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GET_LANG (cmd 7) returns the current language as a well-formed code.

    Read-only. The firmware answers ``P\\x07.<llCC>`` (e.g. ``enUS``) for a
    known language, or ``P\\x07!`` if the stored language is somehow invalid.
    Asserts the ACK and the two-lowercase-two-uppercase code shape.
    """
    response = raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG]))
    log(f"GET_LANG response: {response!r}")
    if not _resp_ok(response, CMD_GET_LANG, log):
        return False
    lang = _reply_text(response)
    log(f"  current language: {lang!r}")
    if not re.fullmatch(r"[a-z]{2}[A-Z]{2}", lang):
        log("  FAIL: language code not in expected 'llCC' form")
        return False
    return True


def _read_packed_lang_codes(raw: RawHID, log: Callable[[str], None],
                            attempts: int = 3):
    """Read GET_LANG_LIST_PACKED (cmd 27) and decode it to a list of 'llCC' codes.

    The list is a count byte + two ISO index bytes per language, possibly split
    across several reports; the leading count makes the total length deterministic
    (no reliance on a read timeout). Decodes via the shared frozen
    station/iso_lang_country.py table. The payload is binary, so reports are
    reassembled by raw byte slices, not text decoding. Returns the list of codes
    (empty if the firmware reports zero languages), or None on any protocol /
    decoding failure (already logged).

    This is the **largest** reply the rig reads — it now spans ~6+ reports and
    grows with every language batch (NUM_LANG). ``send_and_read_all`` (unlike
    ``send``) has no built-in retry and ends the read on the first empty
    inter-packet gap, so a stall in the master's main loop can truncate the read
    or miss the first packet. That stall is a *timing* artifact, not a link
    problem: the rig uses the same clean full-duplex two-wire split link as a
    shipping keyboard, but it fires this query inside the master's boot-time busy
    window (the initial 72-keycap render + split-sync to the just-booted slave) —
    something a human user never triggers. The proven host reader handles the same
    list reliably; the only reason the rig needs more is that it has no outer
    retry loop (the host re-enumerates on its next 1 s reconnect probe). So give
    it room: longer first-/inter-packet timeouts than the defaults, and — because
    the count byte tells us the exact expected length — **retry the whole
    exchange** (fresh handle each time) when it comes back empty or short, rather
    than failing on a single slow transfer. A *complete* payload that won't decode
    (bad index) is a real table fault, not a timing blip, so that fails
    immediately without burning the retries."""
    last_err = "no packed-list packets received"
    for attempt in range(max(1, attempts)):
        packets = raw.send_and_read_all(
            bytes([POLY_CHANNEL, CMD_GET_LANG_LIST_PACKED]),
            first_timeout_ms=2500, next_timeout_ms=600)
        if not packets:
            last_err = "no packed-list packets received"
        else:
            payload = bytearray()
            header_err = []  # capture the first bad-header detail (not just "garbled")
            for p in packets:
                if not _resp_ok(p, CMD_GET_LANG_LIST_PACKED,
                                lambda m: header_err.append(m) if not header_err else None):
                    break
                payload += bytes(p[3:])  # strip "P\x1b." header; keep raw bytes (binary!)
            if header_err:
                detail = header_err[0].strip()  # _resp_ok messages start with "FAIL: "
                detail = detail[5:].strip() if detail.startswith("FAIL:") else detail
                last_err = f"non-ACK / garbled header packet: {detail}"
            elif not payload:
                last_err = "packed reply had only headers, no payload (count byte missing)"
            else:
                count = payload[0]
                total = 1 + 2 * count
                if len(payload) < total:
                    # Almost certainly the master's boot-time busy window cut the
                    # read short — retry the whole exchange before giving up.
                    last_err = f"truncated payload ({len(payload)} bytes, need {total})"
                else:
                    skipped = []  # index pairs the frozen table couldn't resolve
                    try:
                        codes = decode_packed(
                            payload[:total],
                            on_skip=lambda pos, li, ci: skipped.append((pos, li, ci)))
                    except (KeyError, IndexError) as e:
                        log(f"  FAIL: could not decode packed list: {e}")
                        return None
                    if len(codes) != count:
                        # A drifted index pair is dropped by decode_packed, so name it
                        # (frozen-table-vs-firmware drift) instead of a bare count miss.
                        log(f"  FAIL: decoded {len(codes)} codes but count byte says "
                            f"{count} (unknown/skipped index pairs: {skipped})")
                        return None
                    if attempt:
                        log(f"  packed list read OK on attempt {attempt + 1}/{attempts}")
                    return codes
        if attempt + 1 < attempts:
            log(f"  packed-list read attempt {attempt + 1}/{attempts}: {last_err} — retrying")
    log(f"  FAIL: {last_err} (after {attempts} attempts)")
    return None


def test_legacy_lang_list_nacked(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GET_LANG_LIST (cmd 8, the legacy ASCII list) was RETIRED in protocol v2.

    The firmware now answers a single ``P\\x08!`` NACK; hosts must use the packed
    GET_LANG_LIST_PACKED (cmd 27) instead. This is the regression guard against the
    old multi-packet ASCII table coming back: an ACK ('.') here means the board is
    running pre-v2 firmware (or cmd 8 was un-retired), which is a failure on a v2
    rig. Exactly one report is expected — the NACK.
    """
    resp = raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG_LIST]))
    log(f"GET_LANG_LIST (legacy ASCII) response: {resp!r}")
    if not _resp_ok(resp, CMD_GET_LANG_LIST, log, expect_status=NACK):
        log("  FAIL: legacy ASCII GET_LANG_LIST must NACK "
            "(retired in protocol v2 — hosts use cmd 27)")
        return False
    return True


def test_enumerate_languages_packed(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GET_LANG_LIST_PACKED (cmd 27) — the ONLY language-list command in protocol v2.

    Reads the compact list (count byte + 2 ISO index bytes per language across
    reports) and decodes it via the shared frozen station/iso_lang_country.py table,
    then validates it stands on its own — the ASCII GET_LANG_LIST it used to be
    cross-checked against is retired (see test_legacy_lang_list_nacked). Asserts a
    positive count that matches the decoded length (done in the helper), every code
    in canonical 'llCC' form, the staple locales present, and the board's current
    language (cmd 7) appearing in the list. Guards against index drift between the
    firmware table and the rig's frozen copy.
    """
    codes = _read_packed_lang_codes(raw, log)
    if not codes:
        log("  FAIL: packed language list is empty or could not be read")
        return False
    log(f"  {len(codes)} languages decoded: {codes}")

    malformed = [c for c in codes if not re.fullmatch(r"[a-z]{2}[A-Z]{2}", c)]
    if malformed:
        log(f"  FAIL: malformed language code(s): {malformed}")
        return False
    for required in ("enUS", "deDE", "frFR"):
        if required not in codes:
            log(f"  FAIL: expected language {required!r} missing from list")
            return False

    # Consistency: the board's current language must be one of the listed codes.
    cur = raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG]))
    if _resp_ok(cur, CMD_GET_LANG, log, expect_status=None):
        current = _reply_text(cur)
        if current and current not in codes:
            log(f"  FAIL: current language {current!r} not present in packed list")
            return False
        log(f"  current language {current!r} present in list")
    return True


def test_get_default_layer(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GET_DEFAULT_LAYER (cmd 22) returns a plausible default-layer index.

    Read-only. Reply is ``P\\x16.`` followed by the layer byte. Asserts the ACK,
    that the payload byte is present, and that it is below the configured layer
    count (a wild value would point at an uninitialised/garbage read).
    """
    response = raw.send(bytes([POLY_CHANNEL, CMD_GET_DEFAULT_LAYER]))
    log(f"GET_DEFAULT_LAYER response: {response!r}")
    if not _resp_ok(response, CMD_GET_DEFAULT_LAYER, log):
        return False
    if len(response) < 4:
        log("  FAIL: response missing the default-layer byte")
        return False
    layer = response[3]
    log(f"  default layer index: {layer}")
    if layer >= MAX_LAYERS:
        log(f"  FAIL: default layer {layer} >= {MAX_LAYERS} (likely a bad read)")
        return False
    return True


def test_reset_keymap(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The VIA reset-dynamic-keymap report (0x06) resets the keymap, master live.

    A UF2/HID flash does not erase the keyboard's wear-leveled EEPROM, so a
    dynamic keymap stored under an older firmware whose layer layout differs
    survives the update. The VIA reset report clears it back to the compiled
    defaults on both halves (the firmware routes data[0]==0x06 to
    legacy_command_kb -> dynamic_keymap_reset(), bridges the reset to the slave,
    and echoes the request — there is no "P<cmd>." ACK). The reset triggers a
    display refresh + a split-sync to the slave, so this also guards that the
    master keeps servicing HID afterwards (the same busy-window class as the
    brightness/overlay paths). Resetting to firmware defaults is the desired
    clean state on a freshly-flashed board, so running it in the suite leaves the
    rig in a good state rather than a dirty one — and is exactly what the runner
    does before the suite (see TestRunner._reset_keymap).
    """
    response = raw.send(bytes([VIA_DYNAMIC_KEYMAP_RESET]))
    log(f"VIA keymap-reset response: {response!r}")
    if response is None or len(response) < 1:
        log("  FAIL: no/short response to the VIA keymap-reset report")
        return False
    if response[0] != VIA_DYNAMIC_KEYMAP_RESET:
        log(f"  FAIL: echo byte {response[0]:#04x} != request {VIA_DYNAMIC_KEYMAP_RESET:#04x}")
        return False
    if not _master_alive(raw, log):
        log("  FAIL: master not answering GET_ID after keymap reset")
        return False
    return True


# --- error / bounds paths (no persistent state change) ------------------------

def test_unknown_command_nacks(raw: RawHID, log: Callable[[str], None]) -> bool:
    """An unrecognised PolyKybd command id is NACKed, not silently dropped.

    Sends ``P\\x7e`` (0x7E — outside the defined cmd range 6..26 and the
    firmware-update range 0x40..0x44). The dispatcher's default case sets the
    status byte to '!' and echoes the report back unchanged otherwise. Confirms
    the error path and that the master keeps servicing HID for unknown input.
    """
    unknown = 0x7E
    response = raw.send(bytes([POLY_CHANNEL, unknown]))
    log(f"unknown cmd {unknown:#04x} response: {response!r}")
    if response is None or len(response) < 3:
        log("  FAIL: no/short response to unknown command")
        return False
    if response[0] != POLY_CHANNEL or response[1] != unknown:
        log("  FAIL: echoed header altered by the dispatcher")
        return False
    if response[2] != NACK:
        log(f"  FAIL: status {response[2]:#04x} is not NACK '!' (0x21)")
        return False
    return True


def test_set_brightness(raw: RawHID, log: Callable[[str], None]) -> bool:
    """SET_BRIGHTNESS (cmd 13) accepts a valid level and rejects out-of-range.

    Sends FULL_BRIGHT (valid, expect ACK) then FULL_BRIGHT+5 (out of range,
    expect NACK — the firmware refuses and changes nothing). Then re-queries
    GET_ID: the contrast write is deferred to housekeeping, and this is the
    command class behind the "brightness key wedges the slave" bug, so the
    master must still answer afterwards. Leaves the rig at full brightness (no
    read-back command exists to restore the prior value; full bright is the
    sensible default for a freshly-flashed board and a key press/reboot also
    restores the saved value).
    """
    ok = raw.send(bytes([POLY_CHANNEL, CMD_SET_BRIGHTNESS, FULL_BRIGHT]))
    log(f"set brightness {FULL_BRIGHT} -> {ok!r}")
    if not _resp_ok(ok, CMD_SET_BRIGHTNESS, log, expect_status=ACK):
        return False
    bad = raw.send(bytes([POLY_CHANNEL, CMD_SET_BRIGHTNESS, FULL_BRIGHT + 5]))
    log(f"set brightness {FULL_BRIGHT + 5} (out of range) -> {bad!r}")
    if not _resp_ok(bad, CMD_SET_BRIGHTNESS, log, expect_status=NACK):
        return False
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after brightness change")
        return False
    return True


def test_set_unicode_mode(raw: RawHID, log: Callable[[str], None]) -> bool:
    """SET_UNICODE_MODE (cmd 20) accepts a valid mode and rejects an invalid one.

    Sets mode 0 (Linux) — valid *and* correct for the Linux rig, expect ACK —
    then 0xFF (invalid, expect NACK; the default case changes nothing).
    """
    ok = raw.send(bytes([POLY_CHANNEL, CMD_SET_UNICODE_MODE, 0]))
    log(f"set unicode mode 0 (Linux) -> {ok!r}")
    if not _resp_ok(ok, CMD_SET_UNICODE_MODE, log, expect_status=ACK):
        return False
    bad = raw.send(bytes([POLY_CHANNEL, CMD_SET_UNICODE_MODE, 0xFF]))
    log(f"set unicode mode 0xFF (invalid) -> {bad!r}")
    if not _resp_ok(bad, CMD_SET_UNICODE_MODE, log, expect_status=NACK):
        return False
    return True


def test_set_brightness_flags(raw: RawHID, log: Callable[[str], None]) -> bool:
    """SET_BRIGHTNESS flags byte (protocol v5): VOLATILE / AUTO_ON / AUTO_OFF.

    There is no brightness or auto-mode read-back command, so this validates the
    command class rather than a state round-trip: the firmware must ACK each flag
    combination, still bounds-check the level even with flags present, and keep
    the master responsive afterwards (cmd 13 is the path behind the "brightness
    key wedges the slave" bug). Engages host-auto with a volatile value, then
    leaves auto mode, and finishes with a plain full-bright set so the rig is
    left at full brightness with auto mode OFF — the same read-back-free clean
    state test_set_brightness targets.
    """
    # Engage auto mode + push a volatile (daylight) value — expect ACK.
    on = raw.send(bytes([POLY_CHANNEL, CMD_SET_BRIGHTNESS, 30, BR_FLAG_VOLATILE | BR_FLAG_AUTO_ON]))
    log(f"set brightness 30 VOLATILE|AUTO_ON -> {on!r}")
    if not _resp_ok(on, CMD_SET_BRIGHTNESS, log, expect_status=ACK):
        return False
    # Leave auto mode (level ignored on AUTO_OFF) — expect ACK.
    off = raw.send(bytes([POLY_CHANNEL, CMD_SET_BRIGHTNESS, 0, BR_FLAG_AUTO_OFF]))
    log(f"set brightness AUTO_OFF -> {off!r}")
    if not _resp_ok(off, CMD_SET_BRIGHTNESS, log, expect_status=ACK):
        return False
    # An out-of-range level must still NACK even when a flags byte is present.
    bad = raw.send(bytes([POLY_CHANNEL, CMD_SET_BRIGHTNESS, FULL_BRIGHT + 5, BR_FLAG_VOLATILE]))
    log(f"set brightness {FULL_BRIGHT + 5} VOLATILE (out of range) -> {bad!r}")
    if not _resp_ok(bad, CMD_SET_BRIGHTNESS, log, expect_status=NACK):
        return False
    # Restore: explicit full-bright set (flags=0) -> persists + leaves auto off.
    restore = raw.send(bytes([POLY_CHANNEL, CMD_SET_BRIGHTNESS, FULL_BRIGHT]))
    log(f"restore full bright -> {restore!r}")
    if not _resp_ok(restore, CMD_SET_BRIGHTNESS, log, expect_status=ACK):
        return False
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after brightness flag writes")
        return False
    return True


def test_idle_wake(raw: RawHID, log: Callable[[str], None]) -> bool:
    """IDLE_STATE (cmd 15) "stop idle" wakes/refreshes the display and ACKs.

    Byte 0 = stop idle (the safe direction — it wakes the panels rather than
    fading them out, and the runner blanks them again after a passing run).
    Asserts the ``P\\x0f.`` ACK; exercises the idle-wake path end to end.
    """
    response = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STATE, 0]))
    log(f"idle-state stop (wake) -> {response!r}")
    return _resp_ok(response, CMD_IDLE_STATE, log, expect_status=ACK)


# --- mutate + restore ---------------------------------------------------------

def test_idle_style_round_trip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """IDLE_STYLE (cmd 28, protocol v4+) get/set round-trip + invalid NACK.

    Reads the current style (query byte 0xFF), sets JITTER (1) and reads it
    back, then restores the original value. An out-of-range style (0xFE, not the
    0xFF query sentinel) must NACK. The style write is deferred to the EEPROM
    flush, so the live state is what we read back here.
    """
    cur = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, 0xFF]))
    log(f"idle-style query -> {cur!r}")
    if not _resp_ok(cur, CMD_IDLE_STYLE, log, expect_status=ACK):
        return False
    if len(cur) < 4:
        log("  FAIL: idle-style query reply has no value byte")
        return False
    original = cur[3]
    log(f"  current idle style = {original}")

    target = 1 if original != 1 else 0   # flip to the other valid style
    set_resp = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, target]))
    log(f"idle-style set {target} -> {set_resp!r}")
    if not _resp_ok(set_resp, CMD_IDLE_STYLE, log, expect_status=ACK):
        return False

    back = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, 0xFF]))
    if not _resp_ok(back, CMD_IDLE_STYLE, log, expect_status=ACK) or len(back) < 4:
        return False
    if back[3] != target:
        log(f"  FAIL: read back {back[3]} != set {target}")
        return False

    bad = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, 0xFE]))
    log(f"idle-style set 0xFE (invalid) -> {bad!r}")
    if not _resp_ok(bad, CMD_IDLE_STYLE, log, expect_status=NACK):
        return False

    # Restore the original style so the rig is left as it was found.
    restore = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, original]))
    return _resp_ok(restore, CMD_IDLE_STYLE, log, expect_status=ACK)


def test_glyph_script_round_trip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GLYPH_SCRIPT (cmd 30, protocol v9+) get/set round-trip.

    Reads the current script (query byte 0xFF), sets TENGWAR (1) and reads it
    back, then restores the original value. The write is deferred to the EEPROM
    flush, so the live state is what we read back here. Selecting a script does
    NOT require the fantasy font-pack bundle to be present — the firmware just
    falls back to Latin legends when a glyph is missing — so this round-trip is
    pack-agnostic. (No invalid-index NACK check here: from v10 the firmware
    accepts any index and degrades gracefully — see test_glyph_script_expansion.)
    """
    cur = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, 0xFF]))
    log(f"glyph-script query -> {cur!r}")
    if not _resp_ok(cur, CMD_GLYPH_SCRIPT, log, expect_status=ACK):
        return False
    if len(cur) < 4:
        log("  FAIL: glyph-script query reply has no value byte")
        return False
    original = cur[3]
    log(f"  current glyph script = {original}")

    target = 1 if original != 1 else 0   # flip to the other always-present script
    set_resp = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, target]))
    log(f"glyph-script set {target} -> {set_resp!r}")
    if not _resp_ok(set_resp, CMD_GLYPH_SCRIPT, log, expect_status=ACK):
        return False

    back = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, 0xFF]))
    if not _resp_ok(back, CMD_GLYPH_SCRIPT, log, expect_status=ACK):
        return False
    if len(back) < 4:
        log("  FAIL: glyph-script read-back reply has no value byte")
        return False
    if back[3] != target:
        log(f"  FAIL: read back {back[3]} != set {target}")
        return False

    # Restore the original script so the rig is left as it was found.
    restore = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, original]))
    return _resp_ok(restore, CMD_GLYPH_SCRIPT, log, expect_status=ACK)


def test_glyph_script_expansion(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Protocol v10 open-ended glyph-script index (cmd 30).

    v10 turns the glyph script into an OPEN-ENDED INDEX: it added 9 scripts
    (runes, Aurebesh, SGA, Cirth, IBM VGA, C64, Amiga, APL, Braille; values 2..10)
    AND made the firmware accept ANY index 0..0xFE — one it can't render just falls
    back to the normal legend instead of NACKing, so new font faces never need a
    protocol bump. This walks a few known scripts (incl. the max BRAILLE=10), then
    sets a deliberately-unknown high index (200) and asserts it is ACCEPTED and
    stored verbatim (a pre-v10 firmware would NACK here) — that acceptance is the
    whole point of the decoupling. Restores the original. Pack-agnostic.
    """
    cur = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, 0xFF]))
    if not _resp_ok(cur, CMD_GLYPH_SCRIPT, log, expect_status=ACK) or len(cur) < 4:
        log("  FAIL: could not read current glyph script")
        return False
    original = cur[3]

    # Known scripts RUNES(2), IBM VGA(6), BRAILLE(10) round-trip exactly.
    # Then an UNKNOWN index (200) must also be accepted + stored (open-ended).
    for target in (2, 6, GLYPH_SCRIPT_MAX, 200):
        set_resp = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, target]))
        note = " (unknown index -> should ACK + degrade to normal)" if target > GLYPH_SCRIPT_MAX else ""
        log(f"glyph-script set {target}{note} -> {set_resp!r}")
        if not _resp_ok(set_resp, CMD_GLYPH_SCRIPT, log, expect_status=ACK):
            log(f"  FAIL: set script {target} did not ACK (open-ended accept expected)")
            return False
        back = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, 0xFF]))
        if not _resp_ok(back, CMD_GLYPH_SCRIPT, log, expect_status=ACK) or len(back) < 4:
            return False
        if back[3] != target:
            log(f"  FAIL: read back {back[3]} != set {target}")
            return False
        log(f"  script {target} round-tripped")

    restore = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SCRIPT, original]))
    return _resp_ok(restore, CMD_GLYPH_SCRIPT, log, expect_status=ACK)


def _look_request(macro_id: int, text=None, style: int = MACRO_STYLE_INDEX,
                  icon: int = 0) -> bytes:
    """Build a MACRO_LOOK report -- a query when `text` is None, else a set.

    All four fields travel in ONE command because a keycap composes them together:
    a host that could set a caption without also naming a style would leave the key
    drawing a combination nobody asked for until the next write landed.
    """
    n = 0xFF if text is None else len(text)
    header = bytes([POLY_CHANNEL, CMD_MACRO_LOOK, macro_id, n, style,
                    icon & 0xFF, (icon >> 8) & 0xFF,
                    (icon >> 16) & 0xFF, (icon >> 24) & 0xFF])
    return header if text is None else header + text


def _look_reply(response):
    """Decode a MACRO_LOOK reply into (caption, style, icon), or None if malformed.

    The reply mirrors the request's layout, so this is also what pins the icon's
    byte order: a swap turns U+1F4E7 into U+E7D40100 and the equality check fails.
    """
    if response is None or len(response) < MACRO_LOOK_HEADER:
        return None
    n = response[3]
    if MACRO_LOOK_HEADER + n > len(response):
        return None
    icon = (response[5] | (response[6] << 8)
            | (response[7] << 16) | (response[8] << 24))
    return bytes(response[MACRO_LOOK_HEADER:MACRO_LOOK_HEADER + n]), response[4], icon


def test_macro_round_trip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Macros (cmds 36/37/38, protocol v15+): the info header, a windowed body
    read/write round-trip, and a keycap-look round-trip (caption, style, icon).

    Unlike the overlay uploads next door these commands ALL reply, so this is a real
    round-trip rather than a liveness guard: what is written to the shared body buffer
    is read straight back out of it.

    Three things it is specifically shaped to catch, all of which are arithmetic:

    * the info header is four fields packed little-endian across five bytes, so a
      byte-order slip reads a 2267-byte capacity as 56066 and the editor sizes its
      storage bar from that number;
    * the body is written and read in report-sized WINDOWS, so an offset that fails to
      advance re-reads window 0 forever and a body longer than one window silently
      comes back as its own first 58 bytes. The probe is deliberately longer than one
      window for exactly that reason;
    * a label is cut to the stride the header advertises, and the reply echoes what was
      actually stored -- which is how an over-long label proves it was truncated rather
      than rejected;
    * the look's icon is a 4-byte little-endian codepoint beside a 1-byte style, so a
      byte-order or offset slip lands the style in the icon's low byte and back.

    ⚠️ An UNKNOWN STYLE is ACCEPTED and stored as INDEX -- this range is OPEN, the
    deliberate opposite of the glyph SIZE two tests down. A style names how the keycap
    composes itself, and INDEX always draws something (it needs neither a font pack nor
    a chosen icon), so a keyboard that does not know a style a newer host offers still
    shows the macro. A size instead names a rendering tier the firmware must know, so an
    unknown one is refused. The two assert opposite things on purpose.

    ⚠️ MUTATE AND RESTORE, over a PROBE PREFIX -- not the whole buffer. It rewrites
    the first ``probe_len`` bytes (enough to span more than one window) and macro 0's
    label; everything past the prefix is never read or written. That prefix and the
    label are saved first and put back in the ``finally``, so a failure mid-test still
    leaves the keyboard as it was found -- and the restore is CHECKED, because a
    silent restore failure would leave a rig with real macros quietly corrupted while
    the test reported a pass.
    """
    # ---- info header --------------------------------------------------------
    info = raw.send(bytes([POLY_CHANNEL, CMD_MACRO_INFO]))
    log(f"macro info -> {info!r}")
    if not _resp_ok(info, CMD_MACRO_INFO, log, expect_status=ACK):
        return False
    if len(info) < 9:
        log("  FAIL: macro info reply is too short to carry the header")
        return False
    count, label_len = info[3], info[4]
    capacity = info[5] | (info[6] << 8)
    used = info[7] | (info[8] << 8)
    # data[9] is how many keycap styles this firmware can DRAW -- the host offers
    # exactly that many in its picker, so a zero would leave it with no style at all.
    styles = info[9] if len(info) > 9 else 1
    log(f"  {count} macros, {label_len}-byte labels, {used}/{capacity} bytes used, "
        f"{styles} keycap style(s)")
    if styles == 0:
        log("  FAIL: a keyboard advertising macros must be able to draw at least one style")
        return False
    if count == 0 or label_len == 0:
        log("  FAIL: a keyboard advertising macros must have a non-zero count and stride")
        return False
    if capacity < 256 or capacity > 0x8000:
        log(f"  FAIL: implausible capacity {capacity} -- byte order?")
        return False
    if used > capacity:
        log(f"  FAIL: {used} bytes used of {capacity} -- more than exists")
        return False

    chunk = 58                      # 64-byte report minus the 6-byte header
    probe_len = chunk + 20          # deliberately spans two windows (see the docstring)
    if probe_len > capacity:
        log("  FAIL: capacity too small to probe a multi-window write")
        return False

    original = None
    original_look = None
    passed = False
    restored = True
    try:
        # ---- save what is there --------------------------------------------
        original = _macro_read(raw, log, probe_len, chunk)
        if original is None:
            return False
        lab = raw.send(_look_request(0))
        if not _resp_ok(lab, CMD_MACRO_LOOK, log, expect_status=ACK):
            log("  FAIL: could not read macro 0's look")
            return False
        original_look = _look_reply(lab)
        if original_look is None:
            log(f"  FAIL: malformed look reply {lab!r}")
            return False
        log(f"  saved {len(original)} body bytes and look {original_look!r}")

        # ---- body round-trip ------------------------------------------------
        # A recognisable, non-repeating pattern: a window-boundary slip that returned
        # the first window twice would still match a constant fill.
        pattern = bytes((0x41 + (i % 26)) for i in range(probe_len - 1)) + b"\x00"
        if not _macro_write(raw, log, 0, pattern, chunk):
            return False
        back = _macro_read(raw, log, probe_len, chunk)
        if back is None:
            return False
        if back != pattern:
            first = next((i for i in range(len(pattern)) if back[i] != pattern[i]), -1)
            log(f"  FAIL: body mismatch at offset {first} "
                f"(wrote {pattern[first]:#04x}, read {back[first]:#04x})")
            return False
        log(f"  {probe_len} bytes round-tripped across {-(-probe_len // chunk)} windows")

        # ---- an ESCAPED body round-trips too -----------------------------------
        # The pattern above is printable ASCII, i.e. the one body shape that carries no
        # 0x01 escape byte -- so it proves the windows and nothing about the encoding.
        # A chord is `01 02 <kc>` / `01 03 <kc>` and a pause is `01 04 <ascii digits>`,
        # and 0x01 is exactly the byte a buffer implementation is most likely to treat
        # as special. The host has written these since the step editor landed; before
        # that nothing on either side produced one, so nothing here had ever stored one.
        #
        # ⚠️ This asserts STORAGE, not playback. Playing it needs a keypress the rig
        # has no fingers for; `make test:polykybd_macro_decode` covers the decoding.
        chord = bytes([
            0x01, 0x02, 0xE0,       # hold  KC_LEFT_CTRL
            0x01, 0x02, 0xE1,       # hold  KC_LEFT_SHIFT
            0x01, 0x01, 0x13,       # tap   KC_P
            0x01, 0x03, 0xE1,       # release KC_LEFT_SHIFT
            0x01, 0x03, 0xE0,       # release KC_LEFT_CTRL
            0x01, 0x04,             # wait…
        ]) + b"250" + b"ok" + b"\x00"
        chord = chord.ljust(probe_len, b"\x00")
        if not _macro_write(raw, log, 0, chord, chunk):
            return False
        back = _macro_read(raw, log, probe_len, chunk)
        if back is None:
            return False
        if back != chord:
            first = next((i for i in range(len(chord)) if back[i] != chord[i]), -1)
            log(f"  FAIL: escaped body mismatch at offset {first} "
                f"(wrote {chord[first]:#04x}, read {back[first]:#04x})")
            return False
        log("  escaped body (chord + pause + text) round-tripped byte for byte")

        # ---- caption round-trip ----------------------------------------------
        for text in (b"rig", b"work mail"):
            resp = raw.send(_look_request(0, text))
            if not _resp_ok(resp, CMD_MACRO_LOOK, log, expect_status=ACK):
                log(f"  FAIL: setting caption {text!r} was refused")
                return False
            got = _look_reply(resp)
            if got is None or got[0] != text:
                log(f"  FAIL: caption read back {got!r} != {text!r}")
                return False
            log(f"  caption {text!r} round-tripped")

        # ---- style + icon ------------------------------------------------------
        # A macro owns its whole keycap -- it cannot ride a modifier, because QMK
        # carries the wrapped key in one byte and a macro keycode does not fit -- so
        # the cell is free to be more than a legend. The three fields ride one
        # command, and this is what proves one write does not clobber another's field.
        for style in (MACRO_STYLE_ICON, MACRO_STYLE_TEXT, MACRO_STYLE_ICON_ONLY):
            if style >= styles:
                log(f"  style {style} not drawable by this firmware ({styles}) — skipped")
                continue
            icon = MACRO_ICON_PROBE if style != MACRO_STYLE_TEXT else 0
            resp = raw.send(_look_request(0, b"rig", style, icon))
            if not _resp_ok(resp, CMD_MACRO_LOOK, log, expect_status=ACK):
                log(f"  FAIL: setting style {style} was refused")
                return False
            got = _look_reply(resp)
            if got != (b"rig", style, icon):
                log(f"  FAIL: look read back {got!r} != {(b'rig', style, icon)!r}")
                return False
            log(f"  style {style} with icon U+{icon:04X} round-tripped")

        # An UNKNOWN style is ACCEPTED and stored as INDEX -- see the docstring. A
        # NACK here would mean a keyboard refusing a style a newer host offers, which
        # is the failure the open range exists to avoid.
        resp = raw.send(_look_request(0, b"rig", MACRO_STYLE_UNKNOWN, 0))
        if not _resp_ok(resp, CMD_MACRO_LOOK, log, expect_status=ACK):
            log(f"  FAIL: style {MACRO_STYLE_UNKNOWN} was refused — this range is OPEN, "
                "an unknown style must degrade to the index style")
            return False
        got = _look_reply(resp)
        if got is None or got[1] != MACRO_STYLE_INDEX:
            log(f"  FAIL: unknown style stored as {got!r}, want style "
                f"{MACRO_STYLE_INDEX} (index)")
            return False
        log(f"  unknown style {MACRO_STYLE_UNKNOWN} degraded to index")

        # Bytes the _Nano_ face cannot draw are DROPPED, not stored: the firmware's
        # label_store() keeps only 0x20..0x7E, because a codepoint that draws nothing
        # is indistinguishable from a bug once it is on a keycap. Without this the
        # firmware could store the raw bytes and still pass everything above.
        mixed = b"caf\xc3\xa9 \xe2\x82\xac"
        resp = raw.send(_look_request(0, mixed))
        if not _resp_ok(resp, CMD_MACRO_LOOK, log, expect_status=ACK):
            log("  FAIL: a label with non-ASCII bytes should store the printable part")
            return False
        decoded = _look_reply(resp)
        if decoded is None:
            log(f"  FAIL: malformed look reply {resp!r}")
            return False
        got = decoded[0]
        if any(b < 0x20 or b > 0x7E for b in got):
            log(f"  FAIL: label kept undrawable bytes {got!r}")
            return False
        if got != bytes(b for b in mixed if 0x20 <= b <= 0x7E):
            log(f"  FAIL: label {got!r} is not the printable part of {mixed!r}")
            return False
        log(f"  non-ASCII label stripped to {got!r}")

        # An over-long label is TRUNCATED to the advertised stride, not refused --
        # the firmware cuts what the nano face cannot fit on the panel anyway.
        long_label = b"X" * (label_len + 8)
        resp = raw.send(_look_request(0, long_label))
        if not _resp_ok(resp, CMD_MACRO_LOOK, log, expect_status=ACK) or len(resp) < 4:
            log("  FAIL: an over-long label should truncate, not fail")
            return False
        if resp[3] > label_len:
            log(f"  FAIL: stored {resp[3]} label bytes, stride is {label_len}")
            return False
        log(f"  over-long label cut to {resp[3]} bytes (stride {label_len})")

        # An id past the count must be REFUSED. The count is what the host lays its
        # editor out from, so a keyboard accepting id 200 would be storing somewhere.
        bad = raw.send(_look_request(count))
        log(f"macro look id {count} (out of range) -> {bad!r}")
        if not _resp_ok(bad, CMD_MACRO_LOOK, log, expect_status=NACK):
            return False
        log("  out-of-range macro id refused")
        passed = True
    finally:
        # The restore is part of the test, not cleanup after it. This rig writes to a
        # real keyboard's persistent macro storage, so a restore that times out or is
        # NACKed leaves someone's macros overwritten -- and a pass logged over that is
        # the worst of both, because nothing afterwards would look wrong. Report it.
        if original is not None and not _macro_write(raw, log, 0, original, chunk):
            log("  FAIL: could not restore the macro body -- the keyboard still holds "
                "the test pattern in its first bytes")
            restored = False
        if original_look is not None:
            text, style, icon = original_look
            resp = raw.send(_look_request(0, text, style, icon))
            if not _resp_ok(resp, CMD_MACRO_LOOK, log, expect_status=ACK):
                log(f"  FAIL: could not restore macro 0's look {original_look!r}")
                restored = False
        # ⚠️ LAST, and the order is load-bearing: the firmware invalidates the buffer's
        # FINAL byte on any window that does not carry it, so the prefix restore above
        # re-marks it. Clearing the byte first and restoring the prefix after leaves the
        # board with macros that silently refuse to play -- which is exactly what the
        # first version of this did, caught by the offline fake mirroring the firmware.
        if capacity > 0 and not _macro_write(raw, log, capacity - 1, b"\x00", chunk):
            log("  FAIL: could not clear the macro buffer's incomplete marker")
            restored = False
        if restored:
            log("  restored the original macro body prefix and keycap look")
    return passed and restored


def _macro_read(raw: RawHID, log: Callable[[str], None], size: int, chunk: int):
    """Read `size` bytes of the macro body buffer in report-sized windows."""
    out = bytearray()
    while len(out) < size:
        want = min(chunk, size - len(out))
        off = len(out)
        resp = raw.send(bytes([POLY_CHANNEL, CMD_MACRO_BODY, 0,
                               off & 0xFF, (off >> 8) & 0xFF, want]))
        if not _resp_ok(resp, CMD_MACRO_BODY, log, expect_status=ACK) or len(resp) < 6:
            log(f"  FAIL: macro body read failed at offset {off}")
            return None
        got = resp[3]
        if got == 0:
            # Would leave the offset where it was; a naive loop would spin here.
            log(f"  FAIL: macro body read returned 0 bytes at offset {off}")
            return None
        out += bytes(resp[6:6 + got])
    return bytes(out[:size])


def _macro_write(raw: RawHID, log: Callable[[str], None], offset: int,
                 data: bytes, chunk: int) -> bool:
    """Write `data` into the macro body buffer in report-sized windows."""
    for i in range(0, len(data), chunk):
        piece = data[i:i + chunk]
        off = offset + i
        resp = raw.send(bytes([POLY_CHANNEL, CMD_MACRO_BODY, 1,
                               off & 0xFF, (off >> 8) & 0xFF, len(piece)]) + piece)
        if not _resp_ok(resp, CMD_MACRO_BODY, log, expect_status=ACK):
            log(f"  FAIL: macro body write failed at offset {off}")
            return False
    return True


def test_glyph_size_round_trip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GLYPH_SIZE (cmd 34, protocol v13+) get/set round-trip + out-of-range NACK.

    The keycap legend size selects how large a key's MAIN legend is drawn (0 small
    = the original face, 1 medium, 2 large); the shift/AltGr previews are
    unaffected. Query byte is 0xFF. The write is deferred to the EEPROM flush, so
    the live state is what reads back here.

    ⚠️ The NACK check is the POINT of this test, not a bounds nicety. This range is
    CLOSED where the glyph SCRIPT next door is open-ended: an unknown script index
    is accepted and degrades to the normal legend, but an unknown SIZE would be
    stored, synced and persisted while still rendering small — a setting that
    silently does nothing. If a firmware change ever makes cmd 34 accept anything,
    this is what catches it. test_glyph_script_expansion asserts the OPPOSITE for
    cmd 30; the two together pin the deliberate asymmetry.

    Pack-agnostic: selecting a bigger size with no `latinbig` bundle flashed just
    renders the small face, so the get/set state round-trips regardless.
    """
    cur = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SIZE, 0xFF]))
    log(f"glyph-size query -> {cur!r}")
    if not _resp_ok(cur, CMD_GLYPH_SIZE, log, expect_status=ACK):
        return False
    if len(cur) < 4:
        log("  FAIL: glyph-size query reply has no value byte")
        return False
    original = cur[3]
    log(f"  current glyph size = {original}")

    last_set = original
    for target in range(GLYPH_SIZE_MAX + 1):
        set_resp = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SIZE, target]))
        log(f"glyph-size set {target} -> {set_resp!r}")
        if not _resp_ok(set_resp, CMD_GLYPH_SIZE, log, expect_status=ACK):
            return False
        back = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SIZE, 0xFF]))
        if not _resp_ok(back, CMD_GLYPH_SIZE, log, expect_status=ACK):
            return False
        if len(back) < 4:
            log("  FAIL: glyph-size read-back reply has no value byte")
            return False
        if back[3] != target:
            log(f"  FAIL: read back {back[3]} != set {target}")
            return False
        last_set = target
        log(f"  size {target} round-tripped")

    # An index past the enum must be REFUSED (see the docstring).
    bad = GLYPH_SIZE_MAX + 1
    nack = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SIZE, bad]))
    log(f"glyph-size set {bad} (out of range) -> {nack!r}")
    if not _resp_ok(nack, CMD_GLYPH_SIZE, log, expect_status=NACK):
        log("  FAIL: an out-of-range glyph size was accepted — cmd 34 must stay a CLOSED range")
        return False

    # ...and must not have changed the live value.
    still = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SIZE, 0xFF]))
    if not _resp_ok(still, CMD_GLYPH_SIZE, log, expect_status=ACK) or len(still) < 4:
        return False
    # Against what the loop actually last set, not against GLYPH_SIZE_MAX: the two
    # are equal today, but only because the loop happens to end there — a coupling
    # a later edit to the loop would silently break.
    if still[3] != last_set:
        log(f"  FAIL: refused size moved the state {last_set} -> {still[3]}")
        return False

    restore = raw.send(bytes([POLY_CHANNEL, CMD_GLYPH_SIZE, original]))
    return _resp_ok(restore, CMD_GLYPH_SIZE, log, expect_status=ACK)


def test_os_round_trip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """SET_OS (cmd 29, protocol v7+) get/set round-trip + invalid NACK.

    The active host-OS identity is its own state (independent of the unicode mode).
    Query (0xFF) replies status '.', byte[3] = active OS, byte[4] = auto-mode flag.
    This pins a couple of OSes (flags bit0 = manual pin) and reads them back, then
    re-engages auto mode (0xFE) and confirms the auto flag, checks an out-of-range
    value NACKs, and finally restores whatever was found (side-effecting but tidy).
    """
    cur = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, 0xFF]))
    log(f"OS query -> {cur!r}")
    if not _resp_ok(cur, CMD_SET_OS, log, expect_status=ACK):
        return False
    if len(cur) < 5:
        log("  FAIL: OS query reply missing value/auto bytes")
        return False
    original_os, original_auto = cur[3], cur[4]
    log(f"  current OS = {original_os}, auto = {original_auto}")

    # Pin macOS (2) then Windows (1) with flags bit0=1; read back each time.
    for target in (2, 1):
        set_resp = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, target, 0x01]))
        log(f"OS pin {target} -> {set_resp!r}")
        if not _resp_ok(set_resp, CMD_SET_OS, log, expect_status=ACK):
            return False
        back = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, 0xFF]))
        if not _resp_ok(back, CMD_SET_OS, log, expect_status=ACK) or len(back) < 5:
            return False
        if back[3] != target:
            log(f"  FAIL: read back OS {back[3]} != pinned {target}")
            return False
        if back[4] != 0:
            log(f"  FAIL: auto flag set ({back[4]}) after a manual pin")
            return False

    # Re-engage auto mode (0xFE) and confirm the auto flag comes back.
    auto_resp = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, 0xFE]))
    log(f"OS engage auto (0xFE) -> {auto_resp!r}")
    if not _resp_ok(auto_resp, CMD_SET_OS, log, expect_status=ACK):
        return False
    back = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, 0xFF]))
    if not _resp_ok(back, CMD_SET_OS, log, expect_status=ACK) or len(back) < 5:
        return False
    if back[4] != 1:
        log(f"  FAIL: auto flag not set ({back[4]}) after engaging auto")
        return False

    # An out-of-range OS value (>= POLY_OS_COUNT, and not the 0xFE/0xFF sentinels) NACKs.
    bad = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, POLY_OS_COUNT, 0x01]))
    log(f"OS set {POLY_OS_COUNT} (invalid) -> {bad!r}")
    if not _resp_ok(bad, CMD_SET_OS, log, expect_status=NACK):
        return False

    # Restore the original mode/OS so the rig is left as found.
    if original_auto:
        restore = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, 0xFE]))
    else:
        restore = raw.send(bytes([POLY_CHANNEL, CMD_SET_OS, original_os, 0x01]))
    return _resp_ok(restore, CMD_SET_OS, log, expect_status=ACK)


def test_overlay_flags_round_trip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """OVERLAY_FLAGS on/off (cmd 11/12) round-trips a flag and restores default.

    Sets DISPLAY_OVERLAYS (bit 0) on, then off again, leaving the default
    (off). Both ACK, and because DISPLAY_OVERLAYS is in
    OVERLAY_SYNCED_STATE_FLAGS each toggle force-syncs state to the slave over
    the split link, so this also lightly exercises that master→slave path.
    """
    on = raw.send(bytes([POLY_CHANNEL, CMD_OVERLAY_FLAGS_ON, DISPLAY_OVERLAYS_BIT]))
    log(f"overlay flags on (0x01) -> {on!r}")
    if not _resp_ok(on, CMD_OVERLAY_FLAGS_ON, log, expect_status=ACK):
        return False
    off = raw.send(bytes([POLY_CHANNEL, CMD_OVERLAY_FLAGS_OFF, DISPLAY_OVERLAYS_BIT]))
    log(f"overlay flags off (0x01) -> {off!r}")
    if not _resp_ok(off, CMD_OVERLAY_FLAGS_OFF, log, expect_status=ACK):
        return False
    return True


def test_language_round_trip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """CHANGE_LANG (cmd 9) switches language, reads it back, then restores it.

    Reads the current language (cmd 7), picks a different one from the packed
    list (cmd 27), switches to it, confirms GET_LANG reflects the change, then
    always restores the original in a ``finally``. Exercises the ``save_user_latin``
    EEPROM path and the slave language sync called out as a remaining risk in
    the firmware notes, while leaving the rig's language unchanged.
    """
    # All reads/writes here are retried: a preceding test (overlay upload) or the
    # CHANGE_LANG below (save_user_latin EEPROM write + slave lang sync) can leave
    # the master in a brief busy window that drops a single HID reply — the same
    # transient _master_alive() tolerates for uploads. A genuine hang still fails
    # every attempt (and would also wedge the next test's GET_ID).
    cur = _send_with_retry(raw, bytes([POLY_CHANNEL, CMD_GET_LANG]), CMD_GET_LANG, log)
    if not _resp_ok(cur, CMD_GET_LANG, log):
        log("  FAIL: could not read current language to begin round-trip")
        return False
    original = _reply_text(cur)
    log(f"  original language: {original!r}")

    codes = _read_packed_lang_codes(raw, log)
    if not codes:
        log("  FAIL: could not read packed language list to pick a target")
        return False
    target = next((c for c in codes if c != original), None)
    if target is None:
        log("  FAIL: could not find a second language to switch to")
        return False
    log(f"  switching {original!r} -> {target!r}")

    target_req = bytes([POLY_CHANNEL, CMD_CHANGE_LANG]) + target.encode("ascii")
    try:
        set_resp = _send_with_retry(raw, target_req, CMD_CHANGE_LANG, log, expect_status=ACK)
        log(f"  CHANGE_LANG {target!r} -> {set_resp!r}")
        if not _resp_ok(set_resp, CMD_CHANGE_LANG, log, expect_status=ACK):
            return False
        check = _send_with_retry(raw, bytes([POLY_CHANNEL, CMD_GET_LANG]), CMD_GET_LANG, log)
        now = _reply_text(check) if _resp_ok(check, CMD_GET_LANG, log, expect_status=None) else ""
        log(f"  read-back after switch: {now!r}")
        if now != target:
            log(f"  FAIL: language did not change (got {now!r}, want {target!r})")
            return False
        return True
    finally:
        restore_req = bytes([POLY_CHANNEL, CMD_CHANGE_LANG]) + original.encode("ascii")
        restore = _send_with_retry(raw, restore_req, CMD_CHANGE_LANG, log, expect_status=ACK)
        restored = _resp_ok(restore, CMD_CHANGE_LANG, log, expect_status=ACK)
        log(f"  restored language to {original!r}: {'ok' if restored else 'FAILED — left changed!'}")


# --- every layout drawn once (extended) ---------------------------------------
#
# The demo-mode crash of 2026-10-01: hy-AM's Shift legend U+2014 was in no font,
# the missing-glyph fallback read '!' out of IconsFont (first in g_all_fonts, at
# U+100000), the glyph index underflowed and the master HardFaulted a few seconds
# after the demo switched to Armenian. language round-trip above only ever draws
# ONE other layout, so nothing on the rig had drawn the other 158.

LANG_SWEEP_DWELL_S  = 0.4   # after CHANGE_LANG: let the master redraw its keycaps
LANG_SWEEP_REBOOT_S = 8.0   # how long a silent master gets to come back as '*'


def _crash_record_body(raw: RawHID, which: int):
    """The cmd 39 reply body ([flags][48-byte record]) for one half, or None."""
    resp = raw.send(bytes([POLY_CHANNEL, CMD_CRASH_RECORD, which]))
    if resp is None or len(resp) < 3 + CRASH_HID_BODY_LEN:
        return None
    if resp[0] != POLY_CHANNEL or resp[1] != CMD_CRASH_RECORD or resp[2] != ACK:
        return None
    return bytes(resp[3:3 + CRASH_HID_BODY_LEN])


def _probe_after_switch(raw: RawHID, log: Callable[[str], None],
                        reboot_s: float = LANG_SWEEP_REBOOT_S) -> str:
    """'alive', 'rebooted' or 'no-reply' for the master right after a CHANGE_LANG.

    GET_ID goes out with ``attempts=1`` because it consumes the fresh-boot marker:
    the first GET_ID after a reboot answers '*', and a retry inside send() would
    swallow it and answer '.', hiding the crash. A reboot also re-enumerates USB,
    so send() can raise while the interface is gone; that is polled through too."""
    deadline = time.monotonic() + reboot_s
    misses = 0
    while True:
        try:
            resp = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]), attempts=1)
        except RuntimeError:
            resp = None
        if _resp_ok(resp, CMD_GET_ID, lambda *_a: None, expect_status=None):
            if resp[2] == FRESH_BOOT:
                return "rebooted"
            if misses:
                log(f"    master answered GET_ID after {misses} miss(es)")
            return "alive"
        misses += 1
        if time.monotonic() >= deadline:
            return "no-reply"
        time.sleep(0.5)


def classify_language_sweep(results, total: int) -> tuple:
    """(ok, message) for a sweep. ``results`` is [(code, outcome, detail)] with
    outcome one of ok / nack / mismatch / rebooted / no-reply. Passes only when
    EVERY listed language was reached and came back ok — a sweep that stopped
    early is a fail even if each language it reached was fine. Pure."""
    bad = [(c, o, d) for c, o, d in results if o != "ok"]
    msg = f"swept {len(results)}/{total} languages"
    if bad:
        msg += ": " + "; ".join(f"{c} {o}" + (f" ({d})" if d else "") for c, o, d in bad)
    elif len(results) != total:
        msg += ": stopped early"
    return (not bad and len(results) == total and total > 0), msg


def _sweep_languages(raw: RawHID, log: Callable[[str], None], codes,
                     dwell_s: float = LANG_SWEEP_DWELL_S,
                     reboot_s: float = LANG_SWEEP_REBOOT_S):
    """Switch to each code in turn and check the master survived the redraw.

    Stops at the first reboot or silent master: that language is the finding,
    and driving a board that just crashed through 100 more switches only adds
    noise. A NACK or a wrong read-back is recorded and the sweep goes on."""
    results = []
    for code in codes:
        req = bytes([POLY_CHANNEL, CMD_CHANGE_LANG]) + code.encode("ascii")
        resp = _send_with_retry(raw, req, CMD_CHANGE_LANG, log, expect_status=ACK)
        if not _resp_ok(resp, CMD_CHANGE_LANG, lambda *_a: None, expect_status=ACK):
            results.append((code, "nack", f"reply {bytes(resp[:3])!r}" if resp else "no reply"))
            continue
        time.sleep(dwell_s)
        state = _probe_after_switch(raw, log, reboot_s)
        if state != "alive":
            results.append((code, state, "the master crashed drawing this layout"
                            if state == "rebooted" else "the master stopped answering"))
            log(f"  FAIL at {code!r}: {state}")
            break
        check = _send_with_retry(raw, bytes([POLY_CHANNEL, CMD_GET_LANG]), CMD_GET_LANG, log)
        now = _reply_text(check) if _resp_ok(check, CMD_GET_LANG, lambda *_a: None,
                                             expect_status=None) else ""
        if now != code:
            results.append((code, "mismatch", f"read back {now!r}"))
            continue
        results.append((code, "ok", ""))
    return results


def test_language_sweep(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Every layout in the packed list (cmd 27) is switched to (cmd 9) and drawn.

    After each switch the master gets ``LANG_SWEEP_DWELL_S`` to redraw, then a
    single-attempt GET_ID tells a live master ('.') from one that crashed and
    rebooted ('*'), and GET_LANG confirms the switch. The two halves' crash
    records (cmd 39) are read before and after, so a fault that rebooted quietly,
    or one on the slave the master pulled, still fails here and is printed. The
    console scan at the end of the suite covers a slave fault the master never
    pulled. The original language is restored in a ``finally``.

    What it draws: each keycap's base legend plus the Shift preview and the AltGr
    hint drawn beside it (where the layout does not hide them), so three of the
    four LUT columns per layout. Caps only
    shows with Caps Lock on, and the rig cannot hold a modifier: key injection
    (cmd 14) needs debug mode, which only the physical DB_TOGG key sets. The
    firmware's tools/check_glyph_coverage.py checks all four columns of all
    layouts against the fonts on every PR; this test is the hardware half.

    EXTENDED tier: about 160 switches at roughly half a second each, and each
    CHANGE_LANG writes the language to EEPROM (save_user_latin)."""
    cur = _send_with_retry(raw, bytes([POLY_CHANNEL, CMD_GET_LANG]), CMD_GET_LANG, log)
    if not _resp_ok(cur, CMD_GET_LANG, log):
        log("  FAIL: could not read the current language")
        return False
    original = _reply_text(cur)
    codes = _read_packed_lang_codes(raw, log)
    if not codes:
        log("  FAIL: could not read the packed language list")
        return False
    log(f"  original language: {original!r}; sweeping {len(codes)} languages")

    before = {which: _crash_record_body(raw, which) for which in (0, 1)}
    try:
        results = _sweep_languages(raw, log, codes)
    finally:
        restore_req = bytes([POLY_CHANNEL, CMD_CHANGE_LANG]) + original.encode("ascii")
        restore = _send_with_retry(raw, restore_req, CMD_CHANGE_LANG, log, expect_status=ACK)
        restored = _resp_ok(restore, CMD_CHANGE_LANG, log, expect_status=ACK)
        log(f"  restored language to {original!r}: {'ok' if restored else 'FAILED, left changed'}")

    ok, msg = classify_language_sweep(results, len(codes))
    log(("  " if ok else "  FAIL: ") + msg)
    for which, side in ((0, "master"), (1, "slave")):
        after = _crash_record_body(raw, which)
        if after is not None and after != before[which] and after[0] & CRASH_HID_FLAG_PRESENT:
            log(f"  FAIL: the {side} half has a NEW crash record: {describe_crash_record(after)}")
            ok = False
    return ok


# --- boot loop: reboot until a boot goes wrong (cmd 43, v22) --------------------

REBOOT_MIN_PROTOCOL = 22
BOOT_LOOP_DEFAULT_ROUNDS = 20
BOOT_LOOP_MAX_ROUNDS = 50
BOOT_LOOP_RETURN_S = 60.0    # a boot plus one watchdog recovery (8 s) fits easily
BOOT_LOOP_SLAVE_WAIT_S = 8.0  # the master pulls the slave's record 2-6 s after link-up


def boot_loop_rounds(env=None) -> int:
    """Rounds to run: HIL_BOOT_LOOP_ROUNDS if set and sane, else the default,
    clamped to 1..BOOT_LOOP_MAX_ROUNDS. Pure."""
    env = os.environ if env is None else env
    try:
        n = int(env.get("HIL_BOOT_LOOP_ROUNDS", BOOT_LOOP_DEFAULT_ROUNDS))
    except (TypeError, ValueError):
        n = BOOT_LOOP_DEFAULT_ROUNDS
    return max(1, min(BOOT_LOOP_MAX_ROUNDS, n))


def classify_boot_round(master_body, slave_body) -> tuple:
    """(ok, detail) for the cmd 39 bodies read after one reboot. Pure.

    A deliberate reboot never archives a record (the firmware's shutdown_user()
    disarms the watchdog first), so a FRESH record on either half means this boot
    went wrong: typically the late-boot guard recovering a stall, which leaves
    `kind=watchdog phase=boot`. An unreadable master body is a failure too; a
    missing slave body is not, since the master may not have pulled it yet."""
    if master_body is None:
        return False, "master crash record unreadable"
    for side, body in (("master", master_body), ("slave", slave_body)):
        if body is not None and body[0] & CRASH_HID_FLAG_PRESENT and body[0] & CRASH_HID_FLAG_FRESH:
            return False, f"fresh {side} record: {describe_crash_record(body)}"
    return True, ""


def _wait_for_reboot(raw: RawHID, deadline_s: float) -> str:
    """'fresh' (GET_ID answered '*'), 'back' ('.' after the interface went away,
    so the '*' was lost to a read), 'not-rebooted' ('.' with no gap at all) or
    'no-reply'. Single-attempt GET_ID, polled through re-enumeration: see
    _probe_after_switch for why a retry would hide the marker."""
    deadline = time.monotonic() + deadline_s
    gone = False
    while time.monotonic() < deadline:
        try:
            resp = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]), attempts=1)
        except RuntimeError:
            resp = None
        if _resp_ok(resp, CMD_GET_ID, lambda *_a: None, expect_status=None):
            if resp[2] == FRESH_BOOT:
                return "fresh"
            return "back" if gone else "not-rebooted"
        gone = True
        time.sleep(0.5)
    return "no-reply"


def test_boot_loop(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Reboot (cmd 43) N times and stop at the first boot that went wrong.

    The rig's twin of the host's boot-loop diagnostic. After each reboot a
    single-attempt GET_ID waits for the master to come back, then cmd 39 is read
    on both halves; the slave's for up to BOOT_LOOP_SLAVE_WAIT_S, until the master
    has pulled it. It FAILS on a fresh crash record (printed decoded) or a reboot
    that does not come back within BOOT_LOOP_RETURN_S, and logs each round's boot
    time. It hunts the intermittent stall in the 63%..75% boot window that the
    late-boot watchdog guard recovers from (fw 1.3.2 field record 1:0x16e1).

    EXTENDED tier: N reboots at several seconds each (HIL_BOOT_LOOP_ROUNDS,
    default 20, at most 50). Runs after the crash-record test, which clears the
    archive, so an older record cannot read as this run's."""
    rounds = boot_loop_rounds()
    log(f"  {rounds} reboot(s)")
    boot_times = []
    for n in range(1, rounds + 1):
        resp = raw.send(bytes([POLY_CHANNEL, CMD_REBOOT]))
        if not _resp_ok(resp, CMD_REBOOT, log, expect_status=ACK):
            log(f"  FAIL: round {n}: the reboot request was not ACKed")
            return False
        t0 = time.monotonic()
        state = _wait_for_reboot(raw, BOOT_LOOP_RETURN_S)
        dt = time.monotonic() - t0
        if state == "no-reply":
            log(f"  FAIL: round {n}: no answer within {BOOT_LOOP_RETURN_S:.0f} s, a boot "
                f"hang the watchdog did not reset")
            return False
        if state == "not-rebooted":
            log(f"  FAIL: round {n}: the master answered without ever going away")
            return False
        boot_times.append(dt)
        master = _crash_record_body(raw, 0)
        slave = None
        slave_deadline = time.monotonic() + BOOT_LOOP_SLAVE_WAIT_S
        while True:
            slave = _crash_record_body(raw, 1)
            if (slave is not None and slave[0] & CRASH_HID_FLAG_PRESENT) or \
                    time.monotonic() >= slave_deadline:
                break
            time.sleep(1.0)
        ok, detail = classify_boot_round(master, slave)
        if not ok:
            log(f"  FAIL: round {n} (boot {dt:.1f} s): {detail}")
            return False
        log(f"    round {n}: back in {dt:.1f} s ({state})")
    log(f"  {rounds} clean reboot(s); boot {min(boot_times):.1f}..{max(boot_times):.1f} s")
    return True


# --- upload / soak regression guards ------------------------------------------

def test_plain_overlay_keeps_master_alive(raw: RawHID, log: Callable[[str], None]) -> bool:
    """A full uncompressed overlay upload (cmd 10) doesn't wedge the master.

    Streams a blank overlay to KC_A as the 6 × 60-byte segments PolyKybdHost
    sends, matching the production framing exactly. These reports get no
    firmware reply, so success is the master still answering GET_ID afterwards.
    Guards the "overlay upload wedges the master" symptom class and exercises
    the uncompressed upload + display-refresh + master→slave overlay split-sync
    path (USER_SYNC_OVERLAY_DATA).
    """
    blank = bytes(PLAIN_SEG_BYTES)
    reports = [
        # Protocol 11: header is [channel, cmd, keycode, (segment<<4)|modifier];
        # modifier 0 here, so the packed byte is just seg<<4. The 4-byte header
        # leaves a full 60-byte segment fitting the 64-byte report.
        bytes([POLY_CHANNEL, CMD_SEND_OVERLAY, KC_A, (seg << 4) | 0x00]) + blank
        for seg in range(NUM_SEGMENTS)
    ]
    raw.write_reports(reports)
    log(f"uploaded blank plain overlay to KC_A ({NUM_SEGMENTS} segments)")
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after plain overlay upload")
        return False
    log("  master still answering GET_ID after plain overlay upload")
    return True


def test_compressed_overlay_keeps_master_alive(raw: RawHID, log: Callable[[str], None]) -> bool:
    """RLE-compressed overlay uploads (cmd 16) don't trigger the core1 hang.

    Pushes a blank RLE-compressed overlay (a single 23-byte stream per key) to
    several keycodes. With USE_CORE1 the firmware decompresses on core1 while
    core0 busy-waits for the per-fragment handshake — the exact path of the
    documented core1 hang. If core1 wedges, core0 never returns to the USB loop
    and the GET_ID below times out. This is the highest-value regression guard
    for the ``cpsid i`` workaround in ``multicore_exec.c``.
    """
    reports = [
        bytes([POLY_CHANNEL, CMD_START_COMPRESSED_OVERLAY, kc, 0x00]) + _BLANK_OVERLAY_RLE
        for kc in range(KC_A, KC_A + COMPRESSED_TEST_KEYS)
    ]
    raw.write_reports(reports)
    log(f"uploaded blank compressed overlay to {COMPRESSED_TEST_KEYS} keycodes "
        f"(RLE stream {len(_BLANK_OVERLAY_RLE)} bytes each, core1 decompress path)")
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after compressed overlay upload — "
            "possible core1 hang regression (see multicore_exec.c cpsid i workaround)")
        return False
    log("  master still answering GET_ID after compressed overlay upload")
    return True


# --- overlay upload: the packet shapes the single-packet tests never reach -----

# A stream that CANNOT fit one packet: 8 bytes of alternating bits (64 one-bit
# runs) followed by a blank tail. ~86 bytes, so it spans cmd 16 (60) + cmd 17.
_TWO_PACKET_OVERLAY_RLE = _rle_compress(bytes([0xAA] * 8) + bytes(OVERLAY_BYTES - 8))


def test_compressed_overlay_two_packets(raw: RawHID, log: Callable[[str], None]) -> bool:
    """A compressed overlay that spans TWO packets (cmd 16 then cmd 17).

    ⚠️ The existing single-packet guard never reaches cmd 17 at all: a fully blank
    overlay compresses to 23 bytes, which fits the 60-byte cmd-16 payload, so the
    continuation opcode — the one every *real* (non-blank) image uses — had zero
    coverage. The two packets take different firmware paths on purpose:

    * cmd 16 resets the fragment context, reads keycode+modifier out of the report
      and hands 60 bytes from ``&data[4]`` to the decompressor as ``first``;
    * cmd 17 falls through with the *retained* context and hands 62 bytes from
      ``&data[2]`` as a continuation, with ``core1_bit_index`` deciding the length.

    So this exercises fragment-context retention across reports and the second,
    differently-framed core1 hand-off, on top of the decompression itself. Silent
    like every overlay upload, so success is the master still answering GET_ID.
    """
    stream = _TWO_PACKET_OVERLAY_RLE
    if not (COMPRESSED_START < len(stream) <= COMPRESSED_START + COMPRESSED_MAX):
        log(f"  FAIL: test builder drift — the RLE stream is {len(stream)} bytes, "
            f"which does not span exactly two packets "
            f"({COMPRESSED_START + 1}..{COMPRESSED_START + COMPRESSED_MAX})")
        return False
    first = (bytes([POLY_CHANNEL, CMD_START_COMPRESSED_OVERLAY, KC_A, 0x00])
             + stream[:COMPRESSED_START])
    cont = bytes([POLY_CHANNEL, CMD_CONT_COMPRESSED_OVERLAY]) + stream[COMPRESSED_START:]
    raw.write_reports([first, cont])
    log(f"uploaded a {len(stream)}-byte RLE stream to KC_A as cmd 16 "
        f"({COMPRESSED_START} bytes) + cmd 17 ({len(stream) - COMPRESSED_START} bytes)")
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after a two-packet compressed overlay — "
            "the cmd-17 continuation path (retained fragment context / second "
            "core1 hand-off) is the new surface here")
        return False
    log("  master still answering GET_ID after the two-packet upload")
    return True


def _roi_header(keycode: int, modifier: int, x: int, y: int, xx: int, yy: int,
                compressed: bool = False) -> bytes:
    """Build the 5-byte ROI header cmd 18 carries.

    The inverse of the firmware's ``set_fragment_context_from_buffer``
    (base/overlay.c), which packs the 6-bit ``y`` across two bytes to keep the
    header at five bytes::

        [0] keycode
        [1] modifier (low nibble) | (y & 0x3c) << 2      -- y bits 2..5
        [2] (y & 0x03) | yy << 2                          -- y bits 0..1, then yy
        [3] x
        [4] (xx & 0x7f) | 0x80 if the payload is RLE-compressed

    ``x``/``y`` are inclusive start pixels, ``xx``/``yy`` exclusive ends.
    """
    return bytes([
        keycode & 0xFF,
        (modifier & 0x0F) | ((y & 0x3C) << 2),
        (y & 0x03) | ((yy & 0x3F) << 2),
        x & 0xFF,
        (xx & 0x7F) | (0x80 if compressed else 0x00),
    ])


def test_roi_overlay_keeps_master_alive(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Partial-region overlay updates (cmd 18/19) don't wedge the master.

    The ROI pair was the last overlay path with **no** liveness guard at all,
    despite going through the same core1 hand-off as the compressed one — a
    *different* command though (``CORE1_CMD_ROI_UPDATE``, preceded by
    ``core1_roi_start()``'s ``CORE1_CMD_RESET_BIT_IDX``), so the existing
    decompression guard does not cover it.

    Two shapes, both silent (success = the master still answers GET_ID):

    1. A well-formed full-width strip large enough to need a **continuation**
       (cmd 19), so the ``first``/continue framing split (57 vs 62 payload bytes)
       is exercised the way a real partial refresh does it.
    2. A deliberately **out-of-bounds** header (x/xx/yy past the 72x40 keycap).
       ``set_fragment_context_from_buffer`` clamps those — an OOB write into the
       adjacent overlay pool is the documented hazard — and logs
       ``ROI overlay: … clamped``. This is the ROI twin of the cmd-33
       bad-width case: assert the guard holds rather than that the pixels landed.
    """
    rows = 13                                   # 72 x 13 px = 117 payload bytes
    payload = bytes((SCREEN_WIDTH * rows) // 8)  # blank region — harmless to render
    hdr = _roi_header(KC_A, 0x00, x=0, y=0, xx=SCREEN_WIDTH, yy=rows)
    first = bytes([POLY_CHANNEL, CMD_START_ROI_OVERLAY]) + hdr + payload[:ROI_START]
    cont = bytes([POLY_CHANNEL, CMD_CONT_ROI_OVERLAY]) + payload[ROI_START:]
    raw.write_reports([first, cont])
    log(f"sent a {SCREEN_WIDTH}x{rows} ROI to KC_A: cmd 18 ({ROI_START} bytes) "
        f"+ cmd 19 ({len(payload) - ROI_START} bytes)")
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after a two-packet ROI upload "
            "(core1 ROI path: RESET_BIT_IDX + ROI_UPDATE)")
        return False

    # Out-of-bounds region: y/yy up to 63, x up to 255, xx up to 127 all fit the
    # wire format but not the 72x40 panel. The firmware must clamp, not index out.
    bad = _roi_header(KC_A, 0x00, x=200, y=60, xx=127, yy=63)
    raw.write_reports([bytes([POLY_CHANNEL, CMD_START_ROI_OVERLAY]) + bad
                       + bytes(ROI_START)])
    log("sent an out-of-bounds ROI header (x=200 y=60 xx=127 yy=63) — expect the "
        "firmware to clamp it and log 'ROI overlay: … clamped'")
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after an out-of-bounds ROI header — the "
            "bounds clamp in set_fragment_context_from_buffer did not hold")
        return False
    log("  master still answering GET_ID after both ROI shapes")
    return True


def _pack_mapping_values(values: list[int], data_bytes: int, width: int) -> bytes:
    """Pack ``values`` LSB-first at ``width`` bits into ``data_bytes`` bytes.

    Mirrors PolyKybdHost ``bit_packing.pack_values`` and the inverse of the
    firmware's ``set_packed_overlay_mapping`` (fill_overlay.c): value *i* occupies
    bits ``[i*width, i*width+width-1]``, little-endian. The second and third byte
    are only touched when the value genuinely extends there, so a narrow width at
    the tail of the buffer can't index past it.
    """
    buf = bytearray(data_bytes)
    mask = (1 << width) - 1
    for idx, v in enumerate(values):
        start = idx * width
        b, s = divmod(start, 8)
        shifted = (v & mask) << s
        buf[b] |= shifted & 0xFF
        if s + width > 8:
            buf[b + 1] |= (shifted >> 8) & 0xFF
        if s + width > 16:
            buf[b + 2] |= (shifted >> 16) & 0xFF
    return bytes(buf)


def _overlay_map_w_report(width: int) -> tuple[bytes, int]:
    """One full cmd-33 report at ``width``, plus the pair count it carries.

    Fills every value slot — there is no count field in the report, so the
    firmware decodes all ``(61*8)//width`` of them and the sender must mean every
    one. ``from`` values are drawn from the band that genuinely *needs* this
    width (``2**(width-1) .. 2**width-1``, clamped to the flat index space), so
    the report exercises the width it claims rather than riding narrow values in
    a wide field. Consecutive values walk every bit offset, which is the point
    for the odd widths — see the test docstring.
    """
    pairs = (OVERLAY_MAP_W_BYTES * 8 // width) // 2
    lo = min(1 << (width - 1), OVERLAY_MAP_IDX_CNT - 1)
    hi = min((1 << width) - 1, OVERLAY_MAP_IDX_CNT - 1)
    span = hi - lo + 1
    values: list[int] = []
    for i in range(pairs):
        values.append(lo + (i % span))     # from: a real display position
        values.append(i % 4)               # to:   an in-pool slot (< NUM_OVERLAY_SLOTS)
    data = _pack_mapping_values(values, OVERLAY_MAP_W_BYTES, width)
    return bytes([POLY_CHANNEL, CMD_SEND_OVERLAY_MAPPING_W, width]) + data, pairs


# --- Icon library fills (cmd 42, protocol v20) ------------------------------------
#
# (pool slot, icon id) pairs in cmd 33's packing; each half draws the icon from its
# own flash (the PlyI bundle in font-pack slot 8). Unlike the other bulk overlay
# commands it is REPLIED: 'P' 42 '.' when every pair applied on both halves, else
# 'P' 42 '!' and byte 3 = the first pair the host must upload itself.
ICON_FILL_MIN_PROTOCOL = 20
ICONS_BUNDLE_SLOT      = 8
NUM_OVERLAY_SLOTS      = 600
# cmd 11 cleanup: RESET_BUFFERS (1<<5, base/com.h) clears the pool the fill wrote,
# on top of the mapping + usage reset the other overlay tests use. Self-clearing.
ICON_FILL_CLEANUP_BITS = OVERLAY_MAPPING_RESET_BITS | (1 << 5)


def _icon_fill_report(pairs: list[tuple[int, int]], width: int) -> bytes:
    """One cmd 42 report, padded by repeating the last pair (there is no count)."""
    n = (OVERLAY_MAP_W_BYTES * 8 // width) // 2
    padded = pairs + [pairs[-1]] * (n - len(pairs))
    values = [v for pair in padded[:n] for v in pair]
    return (bytes([POLY_CHANNEL, CMD_FILL_POOL_FROM_ICON, width])
            + _pack_mapping_values(values, OVERLAY_MAP_W_BYTES, width))


def _icon_fill_verdict(response) -> tuple[str, int] | None:
    """('.', 0xFF) / ('!', first unapplied pair), or None for a malformed reply."""
    if response is None or len(response) < 4 or response[0] != ord("P") \
            or response[1] != CMD_FILL_POOL_FROM_ICON or response[2] not in (ord("."), ord("!")):
        return None
    return chr(response[2]), response[3]


def test_icon_fill_replies(raw: RawHID, log: Callable[[str], None]) -> bool:
    """cmd 42 (v20) answers every report with the verdict the host acts on.

    The rig flashes firmware by UF2 and may or may not hold ``icons.plyi`` in slot 8,
    so the assertions hold either way and the test says which case it ran:

    1. GET_ID's version block lists slot 8 (nine bundles on v20). Without it the
       host never flashes the icon library at all.
    2. One pair (slot 0, icon 0): '.' when the library is present on both halves,
       else '!' at pair 0 -- the host then uploads the image itself.
    3. A width outside 8..16 is refused at pair 0, before any pair is read.
    4. A pool slot past the pool (700 of 600), as pair 1 behind a good pair 0: '!'
       at pair 0 either way. With the library the master stops at pair 1, but the
       slave refuses the whole report (its ack is one byte, no index), and a refused
       bridge is reported as pair 0 -- the conservative answer, since re-uploading a
       good pair only costs a report.

    ⚠️ Pixels are not checked: nothing reads the pool back. The blit and the pair
    decoding are the firmware unit test's job (``make test:polykybd_icon_lib``).
    Side effects undone in a finally, and a cleanup the keyboard does not ACK
    fails the test: cmd 11 ``RESET_BUFFERS|MAPPING_RESET|USAGE_RESET`` clears the
    pool slot the fill wrote (there is no way to read the old image back) and
    returns the mapping and usage bits to the power-on identity.
    """
    response = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
    versions = parse_fontpack_versions(response)
    if versions is None or ICONS_BUNDLE_SLOT not in versions:
        log(f"  FAIL: the GET_ID version block does not list the icon slot "
            f"{ICONS_BUNDLE_SLOT}: {versions}")
        return False
    have = versions[ICONS_BUNDLE_SLOT] > 0
    log(f"icon library in slot {ICONS_BUNDLE_SLOT}: "
        + (f"present, v{versions[ICONS_BUNDLE_SLOT]}" if have else "absent (v0)"))
    cases = [
        ("one pair (slot 0, icon 0) at 9 bits", _icon_fill_report([(0, 0)], 9),
         (".", 0xFF) if have else ("!", 0)),
        ("width 7, below the codec", _icon_fill_report([(0, 0)], 7), ("!", 0)),
        ("pool slot 700 as pair 1", _icon_fill_report([(0, 0), (NUM_OVERLAY_SLOTS + 100, 0)], 10),
         ("!", 0)),
    ]
    passed = False
    try:
        for label, report, want in cases:
            got = _icon_fill_verdict(raw.send(report))
            log(f"{label}: reply {got}, expected {want}")
            if got != want:
                log(f"  FAIL: {label} answered {got}, expected {want}")
                return False
        passed = _master_alive(raw, log)
    finally:
        restore = raw.send(bytes([POLY_CHANNEL, CMD_OVERLAY_FLAGS_ON,
                                  ICON_FILL_CLEANUP_BITS]))
        cleaned = _resp_ok(restore, CMD_OVERLAY_FLAGS_ON, lambda *_a: None, expect_status=ACK)
        log("  reset pool, mapping and usage bits: "
            + ("ok" if cleaned else "FAILED — rig left with the filled slot until next boot"))
    return passed and cleaned


# --- PRC overlays (cmd 41, protocol v19) ----------------------------------------
#
# A cmd 41 report carries whole images as records: a 6-byte big-endian bit field
#   keycode 8 | modifier 4 | top 6 | left 7 | height-1 6 | width-1 7 | len 6 | 0 4
# then `len` payload bytes. A keycode byte of 0 (the zero padding) ends the list.
# Mirrors the firmware's prc_parse_record() (base/prc_codec.c) and the host's
# prc_codec.pack_record(); tests/hil_tests_test.py pins it against both.
PRC_MIN_PROTOCOL = 19
PRC_RECORD_HDR   = 6
PRC_REPORT_BYTES = 64 - 2        # records after 'P' + the command byte
PRC_MALFORMED_LINE = "malformed PRC record"   # fill_overlay.c, uprintf (not debug-gated)
# Every PRC warning fill_overlay.c can print: malformed record, unsupported
# keycode, and a bridge give-up ("PRC overlay for keycode … did not reach the slave").
PRC_WARNING_RE = re.compile(r"malformed PRC record|PRC overlay for")
PRC_SETTLE_S = 3.0

# Real PRC payloads from the host's golden vectors (tests/util/prc_codec_vectors.json,
# table v1): what the host encoder produced for these shapes. The firmware decodes
# them to the vector's overlay; the rig cannot see pixels, so the bytes matter only
# as a realistic record, not as a check of the decode (the firmware unit test is).
PRC_SQUARE_BOX   = (10, 30, 10, 10)                      # top, left, height, width
PRC_SQUARE       = bytes.fromhex("ffffe512e915")         # solid 10x10 square
PRC_CHECKER_BOX  = (16, 60, 8, 8)
PRC_CHECKER      = bytes.fromhex("fc8b0d09d324a77598e846e4aba9b7c12d52bc2465")


def _prc_record(keycode: int, modifier: int, top: int, left: int, height: int,
                width: int, payload: bytes, reserved: int = 0, length: int | None = None) -> bytes:
    """One cmd 41 record. ``reserved`` and ``length`` exist only to build the
    malformed records the firmware must refuse; normal callers leave them alone."""
    n = len(payload) if length is None else length
    f = modifier & 0x0F
    f = (f << 6) | (top & 0x3F)
    f = (f << 7) | (left & 0x7F)
    f = (f << 6) | ((height - 1) & 0x3F)
    f = (f << 7) | ((width - 1) & 0x7F)
    f = (f << 6) | (n & 0x3F)
    f = (f << 4) | (reserved & 0x0F)
    return bytes([keycode & 0xFF]) + f.to_bytes(5, "big") + payload


def _prc_report(*records: bytes) -> bytes:
    body = b"".join(records)
    if len(body) > PRC_REPORT_BYTES:
        raise ValueError(f"{len(body)} bytes of records do not fit one report")
    return bytes([POLY_CHANNEL, CMD_SEND_PRC_OVERLAY]) + body


def _prc_malformed_reports() -> list:
    """(label, report) for the three refusals prc_parse_record() makes."""
    return [
        ("reserved bits set", _prc_report(_prc_record(KC_A, 0, *PRC_SQUARE_BOX, PRC_SQUARE, reserved=0x1))),
        ("box past the frame (top 39 + height 2)", _prc_report(_prc_record(KC_A, 0, 39, 0, 2, 1, b""))),
        ("length past the report (63 of 56)",
         _prc_report(_prc_record(KC_A, 0, *PRC_SQUARE_BOX, PRC_SQUARE, length=63))),
    ]


def test_prc_overlay_keeps_master_alive(raw: RawHID, log: Callable[[str], None]) -> bool:
    """PRC overlay records (cmd 41, v19) decode on both halves without wedging.

    ⚠️ **A liveness guard, not a pixel check.** cmd 41 is silent and nothing reads
    the pool back, so success is "the master still answers GET_ID". Whether the
    decode is RIGHT is the firmware unit test's job (``make test:polykybd_prc_codec``
    decodes the host's golden vectors and compares all 360 bytes).

    Three shapes:

    1. Two real records in one report: KC_A (decoded on the master) and KC_P (a
       right-half key on split72's base layer, so with the default layout the
       record crosses the split link on USER_SYNC_COMPRESSED_DATA with
       PRC_BRIDGE_FLAG and the slave decodes it; a language that remaps P through
       translate_a_to_z can move it).
    2. A full 72x40 box with an empty payload: every pixel is decoded from zero
       bytes, the longest decode a single record can ask for.
    3. The three malformed records the parser refuses (reserved bits, a box past
       the frame, a length past the report).
    """
    both = _prc_report(_prc_record(KC_A, 0, *PRC_SQUARE_BOX, PRC_SQUARE),
                       _prc_record(KC_P, 0, *PRC_CHECKER_BOX, PRC_CHECKER))
    raw.write_reports([both])
    log(f"sent one cmd 41 report with 2 records ({len(both) - 2} bytes): KC_A square "
        f"({len(PRC_SQUARE)} B payload, master) + KC_P checkerboard "
        f"({len(PRC_CHECKER)} B, bridged to the slave)")
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after two PRC records — the decode or the "
            "PRC_BRIDGE_FLAG hand-off to the slave hung")
        return False

    full = _prc_report(_prc_record(KC_A, 0, 0, 0, 40, 72, b""))
    raw.write_reports([full])
    log("sent a full-frame 72x40 PRC record with an empty payload (2880 pixels decoded)")
    if not _master_alive(raw, log):
        log("  FAIL: master unresponsive after a full-frame PRC decode")
        return False

    for label, report in _prc_malformed_reports():
        raw.write_reports([report])
        log(f"sent a malformed PRC record: {label}")
        if not _master_alive(raw, log):
            log(f"  FAIL: master unresponsive after a malformed record ({label}) — "
                "prc_parse_record() did not refuse it")
            return False
    log("  master still answering GET_ID after every PRC shape")
    return True


def test_prc_malformed_record_is_refused(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The firmware REFUSES a malformed PRC record, and says so on the console.

    The liveness test above shows a malformed record does not wedge the master; it
    cannot tell a refusal from a record decoded into garbage. The firmware logs
    ``Warning: malformed PRC record at byte N`` (uprintf, not debug-gated) when it
    stops at a record it cannot parse, so the console can. The inverse is checked
    too: a valid report must produce no such line, or the parser would be refusing
    good records and every PRC keycap would stay blank. ``needs_console``.
    """
    for label, report in _prc_malformed_reports():
        mark = TAP.mark()
        raw.write_reports([report])
        line = TAP.wait_for(PRC_MALFORMED_LINE, mark, timeout=3.0)
        if line is None:
            log(f"  FAIL: no '{PRC_MALFORMED_LINE}' line after a record with {label} — "
                "the firmware accepted a record it must refuse")
            return False
        log(f"refused ({label}): {line.strip()}")
        if not _master_alive(raw, log):
            return False

    mark = TAP.mark()
    raw.write_reports([_prc_report(_prc_record(KC_A, 0, *PRC_SQUARE_BOX, PRC_SQUARE),
                                   _prc_record(KC_P, 0, *PRC_CHECKER_BOX, PRC_CHECKER))])
    if not _master_alive(raw, log):
        return False
    # The firmware warns while decoding, before it answers the GET_ID above, but the
    # console drains on the main loop and can lag the HID reply. So poll for the
    # whole settle window rather than snapshotting once: any PRC warning that
    # arrives inside it fails the test.
    warning = TAP.wait_for(PRC_WARNING_RE, mark, timeout=PRC_SETTLE_S)
    if warning is not None:
        log(f"  FAIL: a valid PRC report produced a warning: {warning.strip()}")
        return False
    log(f"  a valid 2-record report produced no PRC warning within {PRC_SETTLE_S:.0f}s")
    return True


def test_overlay_mapping_widths(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Variable-width overlay mapping (cmd 33, v12) survives every width it uses.

    ⚠️ **This is a liveness/robustness guard, not a round-trip.** cmd 33 is
    silent by design (it sits in the no-reply overlay-activity group with cmd 21),
    and no command reads ``display_to_pool`` back — so the test cannot assert the
    mapping *landed*, only that decoding it does not wedge the master. That is
    still the coverage that matters here, because the bugs this command shipped
    with were all in the bit arithmetic:

    * The decoder walks a **different byte pattern per width**. ``gcd(width,8)``
      decides it: at 8 every value is one whole byte at offset 0; at 10 the
      offsets stay in {0,2,4,6} and never reach a third byte; **only the odd
      widths 9 and 11 walk all eight offsets and read a third byte**. Those two
      are new in v12 and are exactly where the pre-v12 fixed expression computed
      ``0xff >> (8 - n)`` — a shift by −2 at offset 7, unreachable at 10 bits.
    * Width 8 is where an unconditional second-byte read ran past the last data
      byte.

    So each of 8/9/10/11 gets a **full** report (every value slot filled, ``from``
    drawn from the band that needs that width, including the ``>= 1024``
    GUI-combo band that only protocol 12 can address), and the master must still
    answer GET_ID after each. Then two out-of-range widths (7 and 17) confirm the
    ``OVERLAY_MAP_WIDTH_MIN/MAX`` guard clause drops the report instead of
    slicing garbage — the firmware logs ``REJECTED overlay mapping report: bad
    width``, visible in the captured console output on a failure.

    Side effects are undone: the mapping and usage bits are reset to the
    power-on identity via cmd 11 ``MAPPING_RESET|USAGE_RESET`` in a finally.
    """
    try:
        for width in OVERLAY_MAP_TEST_WIDTHS:
            report, pairs = _overlay_map_w_report(width)
            raw.write_reports([report])
            log(f"sent cmd 33 mapping at width {width}: {pairs} pairs "
                f"({OVERLAY_MAP_W_BYTES * 8 // width} values in {OVERLAY_MAP_W_BYTES} bytes)"
                + (" — GUI-combo band, needs v12" if width == 11 else ""))
            if not _master_alive(raw, log):
                log(f"  FAIL: master unresponsive after a {width}-bit mapping report — "
                    "decoder regression (bit-offset arithmetic / out-of-buffer read)")
                return False

        for bad in OVERLAY_MAP_BAD_WIDTHS:
            # Hand-framed: _overlay_map_w_report would try to pack at this width.
            raw.write_reports([bytes([POLY_CHANNEL, CMD_SEND_OVERLAY_MAPPING_W, bad])
                               + bytes(OVERLAY_MAP_W_BYTES)])
            log(f"sent cmd 33 with out-of-range width {bad} (expect firmware to reject + log)")
            if not _master_alive(raw, log):
                log(f"  FAIL: master unresponsive after width {bad} — the "
                    "OVERLAY_MAP_WIDTH_MIN/MAX guard clause did not hold")
                return False

        log("  master still answering GET_ID after every width")
        return True
    finally:
        restore = raw.send(bytes([POLY_CHANNEL, CMD_OVERLAY_FLAGS_ON, OVERLAY_MAPPING_RESET_BITS]))
        ok = _resp_ok(restore, CMD_OVERLAY_FLAGS_ON, lambda *_a: None, expect_status=ACK)
        log(f"  reset mapping + usage bits to identity: "
            f"{'ok' if ok else 'FAILED — rig left with test mappings until next boot'}")


# --- Mapping flags on cmd 33 (protocol v21) ----------------------------------------
#
# Bits 5/6 of cmd 33's width byte run the enable / prepare steps that used to take
# their own cmd 11 reports; the firmware masks the width with 0x1F before decoding.
MAPPING_FLAGS_MIN_PROTOCOL = 21
MAPPING_FLAG_SHOW          = 0x20   # OVERLAY_MAP_W_SHOW: DISPLAY_OVERLAYS after the pairs
MAPPING_FLAG_RESET         = 0x40   # OVERLAY_MAP_W_RESET: the prepare step before them
MAPPING_BAD_WIDTH_LINE     = "REJECTED overlay mapping report: bad width"
MIRROR_OVERLAYS_BIT        = 1 << 2 # overlay_flag MIRROR_OVERLAYS (base/com.h)


def test_mapping_flags_ride_cmd_33(raw: RawHID, log: Callable[[str], None]) -> bool:
    """cmd 33 (v21) decodes a report whose width byte carries the prepare and
    enable flags, and still refuses a bad width hidden under them.

    A firmware older than v21 reads 0x60|9 as width 105 and drops the report with
    ``REJECTED overlay mapping report: bad width 105``: the host would then send
    every warm switch's mappings into nothing, and every keycap would keep the old
    app's icons. So the firmware must print no such line for a flagged report. The
    width-7 report is the positive control: it must produce the line, which proves
    the console is live at the moment the silence is measured. ``needs_console``.

    Nothing reads the mapping or the overlay flags back, so the test cannot show
    that the reset and the enable RAN, only that the report was accepted. Cleanup
    in a finally: cmd 11 resets mapping and usage, cmd 12 clears the DISPLAY and
    MIRROR bits the two flags set, and a cleanup the keyboard does not ACK fails
    the test.
    """
    report, pairs = _overlay_map_w_report(9)
    flagged = bytes([report[0], report[1], report[2] | MAPPING_FLAG_RESET | MAPPING_FLAG_SHOW]) + report[3:]
    passed = False
    try:
        mark = TAP.mark()
        raw.write_reports([flagged])
        log(f"sent cmd 33 width byte {flagged[2]:#04x} (reset + show, width 9): {pairs} pairs")
        if not _master_alive(raw, log):
            return False
        line = TAP.wait_for(MAPPING_BAD_WIDTH_LINE, mark, timeout=PRC_SETTLE_S)
        if line is not None:
            log(f"  FAIL: the flagged report was refused: {line.strip()} — the width "
                "byte is not masked (pre-v21 decoder)")
            return False
        log(f"  no bad-width line within {PRC_SETTLE_S:.0f}s")

        hidden = bytes([POLY_CHANNEL, CMD_SEND_OVERLAY_MAPPING_W,
                        MAPPING_FLAG_RESET | 7]) + bytes(OVERLAY_MAP_W_BYTES)
        mark = TAP.mark()
        raw.write_reports([hidden])
        line = TAP.wait_for(MAPPING_BAD_WIDTH_LINE, mark, timeout=PRC_SETTLE_S)
        if line is None:
            log("  FAIL: no bad-width line for width 7 under the reset flag — either "
                "the console is not live or the mask let a bad width through")
            return False
        log(f"  width 7 under the reset flag refused: {line.strip()}")
        passed = _master_alive(raw, log)
    finally:
        restore = raw.send(bytes([POLY_CHANNEL, CMD_OVERLAY_FLAGS_ON, OVERLAY_MAPPING_RESET_BITS]))
        off = raw.send(bytes([POLY_CHANNEL, CMD_OVERLAY_FLAGS_OFF,
                              DISPLAY_OVERLAYS_BIT | MIRROR_OVERLAYS_BIT]))
        ok = (_resp_ok(restore, CMD_OVERLAY_FLAGS_ON, lambda *_a: None, expect_status=ACK)
              and _resp_ok(off, CMD_OVERLAY_FLAGS_OFF, lambda *_a: None, expect_status=ACK))
        log("  reset mapping + usage, cleared DISPLAY/MIRROR: "
            + ("ok" if ok else "FAILED — rig left with test mappings until next boot"))
    return passed and ok

# GET_ID stress pass/fail tuning. ``send_repeated`` already retries transient
# host-side USB errors internally, so each no-answer counted here is a *sustained*
# silence (~retries x timeout). Two failure shapes must be told apart:
#   * isolated no-answers = the documented post-overlay "deaf window" — the master
#     finishes an EEPROM write + a full keycap refresh and, since the split-sync
#     re-fire fix (qmk #80), may bridge a few extra frames to the slave before it
#     services HID again. The device recovers within a send or two. EXPECTED.
#   * a long run of consecutive no-answers = a genuine freeze (the core1 hang):
#     once core0 wedges it answers nothing from that point on. REGRESSION.
# So tolerate a small number of isolated misses but fail on a freeze signature.
STRESS_FREEZE_RUN = 3   # >= this many consecutive no-answers ⇒ treat as a freeze


def classify_get_id_stress(oks: list[bool]) -> tuple[bool, int, int]:
    """Pure pass/fail decision for a GET_ID stress burst from per-send OK flags.

    The burst size is ``len(oks)`` — classify what was actually received, so the
    flags list and the nominal count can't drift apart. Returns
    ``(passed, misses, longest_miss_run)``. Fails only on a freeze signature —
    more than ``max(2, len(oks) // 10)`` total no-answers, or a run of
    ``>= STRESS_FREEZE_RUN`` consecutive no-answers (a permanent core1 hang
    answers nothing from the hang point on). Isolated transient misses pass."""
    max_misses = max(2, len(oks) // 10)
    misses = run = longest = 0
    for ok in oks:
        if ok:
            run = 0
        else:
            misses += 1
            run += 1
            longest = max(longest, run)
    return (misses <= max_misses and longest < STRESS_FREEZE_RUN), misses, longest


def test_get_id_stress(raw: RawHID, log: Callable[[str], None], n: int = 50) -> bool:
    """N rapid GET_IDs on one persistent connection; reports latency.

    Catches the master freezing under load (the core1-hang symptom and any
    descriptor/dispatch flakiness). Runs all N exchanges on a single open handle
    — the way the real host talks to the device — rather than reopening the
    hidraw node per send: rapid open/close churn trips a host-side USB "Protocol
    error" (EPROTO) on the Pi that has nothing to do with firmware health.
    ``send_repeated`` retries such transient errors (and counts them).

    A single isolated no-answer is NOT failed: the immediately-preceding overlay
    test leaves the master in its transient deaf window — the post-overlay EEPROM
    write + full keycap refresh, which the split-sync re-fire fix (#80) can
    lengthen — so an occasional GET_ID times out and then recovers. Only a freeze *signature*
    (``classify_get_id_stress``) fails. A settle/liveness check first drains the
    deaf window and still catches a hang carried over from the overlay test.
    Logs min/avg/max round-trip so a slow-down trend is visible across runs.
    """
    if not _master_alive(raw, log):
        log("  FAIL: master not answering GET_ID before stress burst (hang carried "
            "over from the overlay upload?)")
        return False
    responses, latencies, transient = raw.send_repeated(
        bytes([POLY_CHANNEL, CMD_GET_ID]), n)
    oks = [_resp_ok(r, CMD_GET_ID, lambda *_a: None, expect_status=None)
           for r in responses]
    passed, misses, longest = classify_get_id_stress(oks)
    if not passed:
        first_bad = next((i for i, ok in enumerate(oks) if not ok), -1)
        log(f"  FAIL: GET_ID stress freeze signature — {misses}/{n} no-answer, "
            f"longest consecutive run {longest} (first at #{first_bad + 1}); "
            f"{transient} transient HID retries this run")
        return False
    if misses:
        log(f"  tolerated {misses}/{n} transient no-answer(s) "
            f"(longest run {longest}) — master recovered each time")
    log(f"  {n - misses}/{n} GET_IDs OK ({transient} transient HID retries) — latency "
        f"min/avg/max = {min(latencies):.0f}/{sum(latencies) / len(latencies):.0f}"
        f"/{max(latencies):.0f} ms")
    return True


def _percentile(values: list, pct: float) -> float:
    """Nearest-rank percentile of a non-empty list (no numpy on the rig)."""
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * len(ordered) + 0.5)) - 1))
    return ordered[k]


def _hid_burst(raw: RawHID, log: Callable[[str], None], n: int, what: str,
               timeout_ms: int = 3000) -> tuple:
    """``n`` GET_IDs on one handle while ``what`` is happening; classify + report.

    Returns ``(passed, median_ms)``. Pass/fail uses the same freeze signature as
    the GET_ID stress test — isolated misses are tolerated, a run of consecutive
    no-answers is not — because that is the difference between "the main loop is
    busy" (expected while an animation owns the CPU) and "the main loop is gone".
    The latency numbers are *reported*, not asserted, since the rig has no
    published baseline for them yet; see the note in test_idle_eden_screensaver.
    """
    responses, latencies, transient = raw.send_repeated(
        bytes([POLY_CHANNEL, CMD_GET_ID]), n, timeout_ms=timeout_ms)
    oks = [_resp_ok(r, CMD_GET_ID, lambda *_a: None, expect_status=None)
           for r in responses]
    passed, misses, longest = classify_get_id_stress(oks)
    median = _percentile(latencies, 50) if latencies else 0.0
    log(f"  {n - misses}/{n} GET_IDs answered while {what} "
        f"({transient} transient HID retries) — latency "
        f"median/p95/max = {median:.0f}/{_percentile(latencies, 95):.0f}"
        f"/{max(latencies):.0f} ms")
    if not passed:
        log(f"  FAIL: freeze signature while {what} — {misses}/{n} no-answer, "
            f"longest consecutive run {longest}")
    return passed, median


def test_replay_animation(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The startup animation replays on demand (cmd 31), and the keyboard survives it.

    Cmd 31 had no coverage at all, and it is not merely an ACK to tick off: while
    the one-shot intro runs it owns the keycaps and renders a frame per
    housekeeping pass, deliberately **unsliced** (unlike the looping idle
    screensaver — see test_idle_eden_screensaver). So the properties worth
    asserting are the ones a wedged or non-terminating animation would break:

    1. the command ACKs;
    2. HID keeps being serviced *between* frames — a burst that goes entirely
       unanswered means the render never hands the main loop back, which is a
       hang, not a slow frame;
    3. the animation **ends by itself** and the master returns to normal
       responsiveness within a bounded time.

    The measured HID latency during the animation is logged rather than asserted:
    it is the number that would move if the frame cost changed, and nobody has a
    rig baseline for it yet.
    """
    resp = raw.send(bytes([POLY_CHANNEL, CMD_REPLAY_ANIM]))
    if not _resp_ok(resp, CMD_REPLAY_ANIM, log, expect_status=ACK):
        return False
    log("cmd 31 ACKed — the one-shot Eden intro is now playing on both halves")
    started = time.monotonic()
    passed, _median = _hid_burst(raw, log, ANIM_BURST_SENDS, "the intro animation runs")
    if not passed:
        log("  FAIL: the animation starved the main loop of HID entirely — "
            "startup_anim_tick() is not returning between frames")
        return False

    # It must finish on its own. "Finished" = back to answering fast, which is the
    # only observable the host has (nothing reports startup_anim_active()).
    deadline = started + ANIM_TOTAL_S + ANIM_RECOVER_MARGIN_S
    streak = 0
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        ok = _resp_ok(raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]),
                               timeout_ms=1000, attempts=1),
                      CMD_GET_ID, lambda *_a: None, expect_status=None)
        fast = ok and (time.monotonic() - t0) * 1000.0 <= ANIM_RECOVERED_MS
        streak = streak + 1 if fast else 0
        if streak >= ANIM_RECOVERED_STREAK:
            log(f"  animation finished and the master is responsive again after "
                f"{time.monotonic() - started:.1f}s "
                f"(expected ~{ANIM_TOTAL_S:.0f}s of animation)")
            return True
        time.sleep(0.2)
    log(f"  FAIL: the master never returned to <= {ANIM_RECOVERED_MS} ms replies "
        f"within {ANIM_TOTAL_S + ANIM_RECOVER_MARGIN_S:.0f}s — the animation did "
        "not end, or it left the display path stuck")
    return False


def test_idle_eden_screensaver(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Idle actually ENGAGES on demand (cmd 15 start), and the Eden screensaver
    keeps servicing HID while it owns the keycaps.

    Three things had no coverage before this and all three have a bug history:

    * **cmd 15 with a non-zero payload** (start idle). Only *stop* was tested. The
      start path backdates the activity timestamp by a full fade-out interval, and
      the old signed arithmetic **underflowed for the first FADE_OUT_TIME ms of
      uptime** — clamped to 0, so idle silently never started. The rig fires its
      commands within seconds of boot, i.e. inside exactly that window, which
      makes it the natural place to catch a regression of it.
    * **the idle transition itself**, confirmed from the firmware console
      (``Transition to idle [style=…]``) rather than assumed. Without that line the
      test would pass whether or not anything happened — hence ``needs_console``.
    * **the sliced Eden render**. The looping screensaver renders keycaps until
      ``EDEN_IDLE_SLICE_MS`` is spent and then *returns*, resuming mid-frame next
      pass. Rendered whole instead, a frame is ~150 ms during which the matrix is
      not scanned — the shipped "Eden doesn't wake on the first keypress" bug. We
      cannot inject a keypress, so HID round-trip time is the proxy.

    ⚠️ The latency is **reported, not asserted**. A sliced frame should keep
    round-trips in the tens of ms and an unsliced one push them toward the ~150 ms
    frame cost, so the median is the number to watch — but the rig has never
    published a baseline for it, and shipping a threshold guessed from the source
    is how a check becomes flaky and then ignored. Read the logged median across a
    few runs, then promote it to an assertion. The freeze signature (no answers at
    all) IS asserted, because that needs no baseline.
    """
    cur = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, 0xFF]))
    if not _resp_ok(cur, CMD_IDLE_STYLE, log, expect_status=ACK) or len(cur) < 4:
        return False
    original = cur[3]
    log(f"current idle style: {original}")
    try:
        set_resp = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, IDLE_STYLE_EDEN]))
        if not _resp_ok(set_resp, CMD_IDLE_STYLE, log, expect_status=ACK):
            log("  FAIL: the firmware rejected IDLE_STYLE_EDEN although it reports a "
                f"version >= {EDEN_MIN_FW}")
            return False

        mark = TAP.mark()
        start = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STATE, 1]))
        if not _resp_ok(start, CMD_IDLE_STATE, log, expect_status=ACK):
            return False
        log(f"start-idle ACKed — waiting up to {IDLE_ENGAGE_TIMEOUT_S:.0f}s for the "
            "fade to complete and the transition to be logged")
        line = TAP.wait_for("Transition to idle", mark, timeout=IDLE_ENGAGE_TIMEOUT_S)
        if line is None:
            log("  FAIL: idle never engaged — no 'Transition to idle' line within "
                f"{IDLE_ENGAGE_TIMEOUT_S:.0f}s. Either the start-idle backdate did not "
                "take (the documented near-boot underflow) or the fade never reached "
                "MIN_BRIGHT")
            return False
        log(f"  firmware: {line}")
        if "style=eden" not in line:
            log("  FAIL: idle engaged with the wrong style — the style set over cmd 28 "
                "did not reach the idle state machine")
            return False

        passed, _median = _hid_burst(raw, log, EDEN_BURST_SENDS,
                                     "the Eden screensaver renders")
        if not passed:
            log("  FAIL: the Eden idle loop starved HID completely — the time-sliced "
                "render (EDEN_IDLE_SLICE_MS) is not handing the main loop back")
            return False
        return True
    finally:
        stop = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STATE, 0]))
        log("  stop-idle: " + ("ok" if _resp_ok(stop, CMD_IDLE_STATE, lambda *_a: None,
                                                expect_status=ACK) else f"unexpected {stop!r}"))
        restore = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, original]))
        log(f"  restored idle style {original}: "
            + ("ok" if _resp_ok(restore, CMD_IDLE_STYLE, lambda *_a: None,
                                expect_status=ACK) else f"unexpected {restore!r}"))


def _link_soak_report() -> bytes:
    """One cmd-21 mapping report whose pairs are all OFF-SCREEN.

    Every cmd-21 report costs exactly one bridged frame, which is what makes it
    the cheapest way to generate measurable split-link traffic. The ``from``
    values are drawn from the high modifier-variant band (>= 90*10), which
    ``overlay_from_index_visible`` reports as off-screen, so the firmware stages
    them silently instead of requesting a display refresh per report — the soak
    then measures the *link*, not the renderer.
    """
    per_report = (HID_DATA_MAX * 8) // OVERLAY_MAP_IDX_BITS   # 49 values
    values = []
    for i in range(per_report):
        if i % 2 == 0:
            values.append(900 + (i % 400))    # from: an off-screen variant slot
        else:
            values.append(0)                  # to:   pool slot 0, always in range
    data = _pack_mapping_values(values, HID_DATA_MAX, OVERLAY_MAP_IDX_BITS)
    return bytes([POLY_CHANNEL, CMD_SEND_OVERLAY_MAPPING]) + data


# measure_split_link outcomes. Deliberately THREE, not a bool: "the console
# produced no summary" is a failure to MEASURE, and a caller that cannot assume
# a live console (the runner, straight after an apply reboot) must not report it
# as a dead slave. The distinction is sound because the master prints the
# summary from send_to_bridge regardless of whether the slave answers — a dead
# slave still yields two summaries, with transport_fail climbing.
LINK_OK         = "ok"
LINK_FAULT      = "fault"
LINK_NO_SUMMARY = "no-summary"


def measure_split_link(raw: RawHID, log: Callable[[str], None]) -> str:
    """Bridge a soak of real traffic and assert the link's error DELTA is clean.

    Extracted from :func:`test_split_link_health` so the *runner* can ask the
    same question at a moment a graded test cannot reach — immediately after a
    firmware APPLY, once the master has rebooted onto the new image. Both
    callers must measure the link the same way; two implementations of "is the
    link healthy" would be free to disagree about the one thing that matters
    here (that ``nack`` is not a fault).

    Returns one of ``LINK_OK`` / ``LINK_FAULT`` / ``LINK_NO_SUMMARY``.
    Restores the mapping table either way.
    """
    report = _link_soak_report()
    mark = TAP.mark()
    try:
        sent = 0
        while sent < LINK_SOAK_REPORTS:
            batch = min(LINK_SOAK_BATCH, LINK_SOAK_REPORTS - sent)
            raw.write_reports([report] * batch)
            sent += batch
            time.sleep(0.05)   # let the firmware drain rather than fill the endpoint
        log(f"bridged {sent} mapping reports (cmd 21) to generate measurable "
            "split-link traffic")
        if not _master_alive(raw, log):
            log("  FAIL: master unresponsive after the mapping soak")
            return LINK_FAULT

        deadline = time.monotonic() + LINK_SUMMARY_TIMEOUT_S
        stats = TAP.link_stats(mark)
        while len(stats) < 2 and time.monotonic() < deadline:
            time.sleep(0.25)
            stats = TAP.link_stats(mark)
        if len(stats) < 2:
            log(f"  FAIL: only {len(stats)} 'Split link:' summary line(s) after "
                f"{sent} bridged reports. Either the reports never reached the "
                "firmware, or the summary cadence (LINK_STATS_LOG_EVERY) changed "
                "and this test's arithmetic is stale")
            return LINK_NO_SUMMARY

        delta = link_delta(stats[0], stats[-1])
        ok, errors, tolerance = classify_link_health(delta)
        log(f"  link over {delta['tx']} bridged frames: crc_err +{delta['crc_err']}, "
            f"transport_fail +{delta['transport_fail']}, nack +{delta['nack']} "
            f"(not an error), giveup +{delta['giveup']}")
        if not ok:
            log(f"  FAIL: {errors} link fault(s) in that window, tolerance "
                f"{tolerance}. Steady state on the full-duplex link is ZERO ongoing "
                "errors — a non-zero crc_err means frames are arriving corrupted, a "
                "non-zero transport_fail means they are not arriving at all")
            return LINK_FAULT
        log(f"  {errors} link fault(s), tolerance {tolerance} — link healthy")
        return LINK_OK
    finally:
        restore = raw.send(bytes([POLY_CHANNEL, CMD_OVERLAY_FLAGS_ON,
                                  OVERLAY_MAPPING_RESET_BITS]))
        ok = _resp_ok(restore, CMD_OVERLAY_FLAGS_ON, lambda *_a: None, expect_status=ACK)
        log("  reset mapping + usage bits to identity: "
            + ("ok" if ok else "FAILED — rig left with the soak mappings until next boot"))


def test_split_link_health(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The master↔slave link carries a burst of real traffic with no wire errors.

    The split link is the subsystem with the worst field record in this project
    (the stuck-idle slave, the missing overlay after an MRU switch, the discarded
    ``send_to_bridge`` ack) and until now **CI never looked at it**: the firmware
    prints a health counter and the rig only echoed it into the log.

    Mechanism: cmd 21 bridges exactly one frame per report, and the firmware
    prints ``Split link: N tx crc_err=… nack=… transport_fail=… giveup=…`` every
    200 frames. So sending ~450 of them guarantees at least two summaries, i.e.
    one fully-measured window, and the delta between them is the error rate of
    traffic *this test caused* — which is the only honest way to read it:

    ⚠️ **Absolutes are useless here.** A healthy rig has a documented BOOT burst
    (crc_err/giveup in the tens) from the window before the link settles, so any
    check on the cumulative counters would either fail every run or be set so high
    it never fires. The delta excludes it by construction.

    ⚠️ **``nack`` is not an error** — a ``SYNC_BUSY``/``SYNC_NACK_REFUSED`` reply
    means the wire worked and the slave said something other than yes. ``SYNC_BUSY``
    arrives on every erase re-poll of a flash. The firmware's own ``err%`` excludes
    it for exactly this reason, and so does ``classify_link_health``.

    This also happens to be the only coverage cmd 21 (the fixed 10-bit mapping
    older hosts still send) has — cmd 33 got a test at v12 and its predecessor
    never did.
    """
    # This test carries ``needs_console``, so the console IS live here and
    # LINK_NO_SUMMARY means the reports never reached the firmware — a failure.
    return measure_split_link(raw, log) == LINK_OK


# --- firmware crash record (console line + cmd 39) ---------------------------
#
# The firmware records a HardFault / unhandled exception / watchdog timeout into
# NOLOAD RAM, reboots, and announces it ONCE per boot on the console as
# ``crash: side=<master|slave> kind=… pc=… … fw=…`` (base/crash_record.c). Until
# now a crash on the rig looked like a boot-burst flake or a dead HID reply; the
# console line is the firmware's own confession, and a run that shows it must
# not go green whatever the other tests say.

CRASH_LINE_MARK = "crash: side="
CRASH_HID_FLAG_PRESENT = 1 << 0
CRASH_HID_FLAG_FRESH   = 1 << 1
CRASH_HID_BODY_LEN     = 49    # [flags][48-byte poly_crash_record_t]

# The 48-byte record, as qmk base/crash_record.h lays it out (packed, little-endian):
# magic, kind, core, consecutive, reset_reason, pc, lr, sp, xpsr, icsr, uptime_ms,
# phase, phase_arg, fw[8], crc.
_CRASH_REC_FMT   = "<IBBBBIIIIIIHH8sI"
_CRASH_KINDS     = {0: "none", 1: "hardfault", 2: "unhandled", 3: "watchdog", 4: "halt"}
_CRASH_PHASES    = {0: "unknown", 1: "boot", 2: "loop", 3: "hid", 4: "bridge",
                    5: "core1_wait", 6: "flash", 7: "suspend", 8: "apply"}


def describe_crash_record(body: bytes) -> str:
    """One line naming what a cmd 39 record says: kind, phase and its argument.

    The test used to log only the first 8 reply bytes and then CLEAR the record,
    so a fresh record failed the run while destroying the one thing that says
    where the board stopped. A BOOT-phase argument is ``step << 8 | sub`` (sub
    0xE1..0xE3 = the status-panel paint, the keycap logo, the final dwell), so
    it is printed as ``step.sub`` to match ``polyctl crash show``. Bit 12 is not
    part of the step: boot_diag.c sets it once core1 has reached core1_entry()
    (on the sub-step paint marks, and on 0xE1..0xE3 since fw 1.3.2), so 0x15E2
    prints as ``boot 5.0xE2 core1=1``, not ``boot 21.0xE2``."""
    import struct
    rec = body[1:1 + struct.calcsize(_CRASH_REC_FMT)]
    if len(rec) < struct.calcsize(_CRASH_REC_FMT):
        return f"record too short ({len(rec)} bytes)"
    (_magic, kind, core, consecutive, reset_reason, pc, lr, sp, xpsr, icsr,
     uptime_ms, phase, arg, fw, _crc) = struct.unpack(_CRASH_REC_FMT, rec)
    if phase == 1 and arg & 0xFF00:
        where = (f"boot {(arg >> 8) & 0x0F}.0x{arg & 0xFF:02X} "
                 f"core1={(arg >> 12) & 1}")
    else:
        where = f"{_CRASH_PHASES.get(phase, phase)} arg=0x{arg:04X}"
    return (f"kind={_CRASH_KINDS.get(kind, kind)} phase={where} core={core} "
            f"consecutive={consecutive} reset=0x{reset_reason:02X} pc=0x{pc:08X} "
            f"lr=0x{lr:08X} sp=0x{sp:08X} xpsr=0x{xpsr:08X} icsr=0x{icsr:08X} "
            f"uptime={uptime_ms}ms "
            f"fw={fw.rstrip(bytes(1)).decode('ascii', 'replace')}")

# Where THIS run's console history starts. The tap is process-global and rolls
# across runs in the long-lived UI process, so a crash line left by a previous
# run must not fail the next one: the runner stamps the mark before it flashes.
_SESSION_MARK = 0


def begin_session() -> None:
    """Called by the runner at the start of a run, before the flash."""
    global _SESSION_MARK
    _SESSION_MARK = TAP.mark()


def classify_crash_lines(lines) -> tuple:
    """(ok, message) for the crash lines the console produced in this run.

    Pure so it is unit-testable: any ``crash: side=`` line is a firmware crash
    somewhere in the run — there is no benign shape of that line."""
    lines = [ln.strip() for ln in lines if CRASH_LINE_MARK in ln]
    if not lines:
        return True, "no crash record announced on the console"
    return False, (f"{len(lines)} firmware crash record line(s) on the console: "
                   + " | ".join(lines))


HAND_LINE_MARK = "hand: "
# ⚠️ ANCHORED, and the optional tail is NOT decoration. boot_diag.c prints
#   "hand: %s (%s) slot=%u/%u writer=0x%02X%s"
# where the last %s is " [EEPROM byte repaired from the stamp]" whenever
# poly_hand_ee_repaired() is true — a real state, seen on hardware, not a
# hypothetical. Anchoring with a bare `$` (as a review suggested) would turn
# every EEPROM-repair boot into a graded FAIL, and nothing here would have
# caught it: no test covered that banner until this comment was written.
# Anchoring anyway, WITH the suffix spelled out, is what rejects a trailing
# garbage line such as `writer=0x550` while keeping the repaired form passing.
_HAND_RE = re.compile(
    r"hand:\s+(LEFT|RIGHT)\s+\((flash stamp|stamped from EEPROM|EEPROM, UNSTAMPED)\)"
    r"\s+slot=(\d+)/(\d+)\s+writer=0x([0-9A-Fa-f]{2})"
    r"(?:\s+\[EEPROM byte repaired from the stamp\])?$")

# base/hand_stamp.c: no record leaves the descriptors at 0xFF / 0, and a page
# index can only be 0..15 (STAMP_PAGES = 4096 / 256) — so the sector can hold
# at most 16 valid records. Both ends are bounded: an impossible descriptor
# (slot=0/17) must not pass as self-consistent just because it is non-zero.
_HAND_NO_RECORD_SLOT = 255
_HAND_MAX_SLOT = 15
_HAND_MAX_COUNT = 16
# make_hand_uf2.py writes 0x55 into pad[0]; stamp_write() leaves 0x00.
_HAND_WRITERS = {0x00, 0x55}


def classify_hand_line(lines) -> tuple:
    """(ok, message) for the boot banner's ``hand:`` line.

    Pure, so it is unit-testable without a keyboard.

    The assertion is INTERNAL CONSISTENCY, not a particular side: the rig's
    halves take whatever handedness their flash holds, and the HIL role is fixed
    at compile time anyway (``POLYKYBD_HIL=left|right``), so demanding LEFT or
    RIGHT here would be asserting the bench's state rather than the firmware's
    behaviour.

    What must hold is that the SOURCE and the record descriptors agree:

    * ``flash stamp`` / ``stamped from EEPROM`` — a record answered or was just
      written, so it has a real page index and the sector holds at least one.
    * ``EEPROM, UNSTAMPED`` — nothing was found, so the descriptors must still
      read "no record" (slot 255, count 0).

    That is exactly the class of bug this firmware keeps producing: a state whose
    display and effect disagree. A ``stamp_read()`` that reported a hit while the
    descriptors said "nothing there" (or the reverse) is a broken CRC check, a
    broken slot scan, or a migration that did not re-scan — and all three would
    otherwise be invisible, because handedness resolves to *something* either way
    and the board comes up looking fine.
    """
    hits = [ln.strip() for ln in lines if HAND_LINE_MARK in ln]
    if not hits:
        return False, "no `hand:` line on the console — the boot banner did not report handedness"
    m = _HAND_RE.search(hits[-1])
    if not m:
        return False, (f"`hand:` line does not parse: {hits[-1]!r} "
                       "(expected `hand: <SIDE> (<source>) slot=N/M writer=0xNN`)")
    side, source, slot, count, writer = (
        m.group(1), m.group(2), int(m.group(3)), int(m.group(4)), int(m.group(5), 16))
    if writer not in _HAND_WRITERS:
        return False, f"writer=0x{writer:02X} is neither firmware (0x00) nor the UF2 tool (0x55)"
    if source == "EEPROM, UNSTAMPED":
        if slot != _HAND_NO_RECORD_SLOT or count != 0:
            return False, (f"source says no stamp but the descriptors claim one: "
                           f"slot={slot}/{count} (expected 255/0)")
    else:
        if slot > _HAND_MAX_SLOT:
            return False, f"source is `{source}` but slot={slot} is outside the sector's 0..15 pages"
        if not 1 <= count <= _HAND_MAX_COUNT:
            return False, (f"source is `{source}` but the sector reports {count} valid "
                           f"records (the sector holds at most {_HAND_MAX_COUNT})")
    return True, f"hand: {side} ({source}) slot={slot}/{count} writer=0x{writer:02X} — self-consistent"


def test_handedness_banner(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Handedness resolution reports itself consistently (boot banner ``hand:``).

    READ-ONLY: it parses a line the firmware already printed at boot. No device
    writes, no reboot, and nothing persistent is touched — which matters because
    the handedness stamp lives in its own flash sector and SURVIVES a reflash, so
    a test that wrote it would leave the rig stamped until someone noticed.

    ``docs/FUTURE_TESTS.md`` listed handedness as blocked on "EEPROM read-back /
    flash-time handedness set". The read-back half of that is no longer missing:
    the banner now carries ``slot=N/M writer=0xNN`` (qmk ``poly_hand_stamp_slot``
    / ``_count`` / ``_writer``), so the resolution can be checked from the console
    without EEPROM access. Setting handedness still needs flash control, and cmd
    25 additionally reboots, so the write half stays on the backlog.

    ``min_fw`` because those fields did not exist before; older firmware prints a
    bare ``hand: LEFT (flash stamp)`` and would fail the parse rather than SKIP.
    ``needs_console`` because without the tap it asserts nothing.
    """
    ok, msg = classify_hand_line(TAP.find_all(HAND_LINE_MARK, mark=_SESSION_MARK))
    log(("  " if ok else "  FAIL: ") + msg)
    return ok


def test_no_crash_record(raw: RawHID, log: Callable[[str], None]) -> bool:
    """No keyboard half crashed during this run (console ``crash:`` line absent).

    Reads the console tap from the start of THIS run (``begin_session``), so a
    fault anywhere in the run — during the flash-and-enumerate, inside another
    test, on the SLAVE (the master pulls the slave's record and prints it as
    ``side=slave``) — is caught here even when every other test passed around
    it. It is the LAST entry of the suite, whatever tiers ran, so every other
    test is inside its window. ``needs_console``: without the tap it can
    assert nothing and would report a green it did not earn."""
    ok, msg = classify_crash_lines(TAP.find_all(CRASH_LINE_MARK, mark=_SESSION_MARK))
    log(("  " if ok else "  FAIL: ") + msg)
    return ok


def test_crash_record_command(raw: RawHID, log: Callable[[str], None]) -> bool:
    """CRASH_RECORD (cmd 39, protocol v16+): read both halves, clear, read back.

    ``data[2]`` 0 = this half's archived record, 1 = the slave's (as pulled over
    the split link), 2 = clear the archive; anything else NACKs. A reply carries
    ``[flags][48-byte record]``; bit0 = a record is present, bit1 = it is FRESH
    (recorded by the boot before this one). A fresh record on the rig IS a crash
    in this run's flash-and-boot and fails; an archived (non-fresh) one is older
    history, logged and then cleared so the next run starts clean and the clear
    sub-op is exercised for real."""
    def read(which: int):
        resp = raw.send(bytes([POLY_CHANNEL, CMD_CRASH_RECORD, which]))
        log(f"crash-record read {which} -> {resp[:8]!r}…" if resp else f"crash-record read {which} -> None")
        if not _resp_ok(resp, CMD_CRASH_RECORD, log, expect_status=ACK):
            return None
        if len(resp) < 3 + CRASH_HID_BODY_LEN:
            log(f"  FAIL: reply body is {len(resp) - 3} bytes, expected {CRASH_HID_BODY_LEN}")
            return None
        if resp[3] & CRASH_HID_FLAG_PRESENT:
            log(f"  record: {describe_crash_record(bytes(resp[3:3 + CRASH_HID_BODY_LEN]))}")
        return resp[3]

    ok = True
    for which, side in ((0, "master"), (1, "slave")):
        flags = read(which)
        if flags is None:
            return False
        if flags & CRASH_HID_FLAG_FRESH:
            log(f"  FAIL: the {side} half reports a FRESH crash record (it crashed on the boot before this one)")
            ok = False
        elif flags & CRASH_HID_FLAG_PRESENT:
            log(f"  note: the {side} half holds an older (archived) crash record — clearing it")
        else:
            log(f"  {side}: no crash record")

    bad = raw.send(bytes([POLY_CHANNEL, CMD_CRASH_RECORD, 3]))
    log(f"crash-record sub-op 3 -> {bad!r}")
    if not _resp_ok(bad, CMD_CRASH_RECORD, log, expect_status=NACK):
        log("  FAIL: an unknown sub-op must NACK")
        return False

    clear = raw.send(bytes([POLY_CHANNEL, CMD_CRASH_RECORD, 2]))
    log(f"crash-record clear -> {clear!r}")
    if not _resp_ok(clear, CMD_CRASH_RECORD, log, expect_status=ACK):
        return False
    flags = read(0)
    if flags is None:
        return False
    if flags & CRASH_HID_FLAG_PRESENT:
        log("  FAIL: the master still reports a record after the clear")
        return False
    log("  clear round-tripped")
    return ok


def _build_empty_fontpack() -> bytes:
    """A minimal valid 32-byte 'empty' PlyF pack (font_count 0) — the wipe sentinel.
    Mirrors PolyKybdHost hid_fontpack.build_empty_pack(): header only, body CRC of an
    empty body. Flashing it to a slot empties that bundle (font_count 0 is a valid
    pack the firmware accepts → the slot contributes no fonts)."""
    import binascii, struct
    body_crc = binascii.crc32(b"") & 0xFFFFFFFF
    # <4sHHIIIIII: magic, abi, flags, content_version, font_count, font_table_off,
    #              total_size, crc32, reserved
    # abi=2: column-native (OLED-page) glyph bitmaps — the per-glyph byte length
    # formula changed (w*((h+7)//8) vs the old row-major (w*h+7)//8), so ABI-1
    # row packs are structurally incompatible with ABI-2 firmware and vice versa.
    return struct.pack("<4sHHIIIIII", b"PlyF", 2, 0, 0, 0, 32, 32, body_crc, 0)


def _doom_slot_flash(raw: RawHID, log: Callable[[str], None],
                     payload: bytes, bundle_id: int) -> bytes | None:
    """BEGIN/CHUNK/COMMIT `payload` to one doom pseudo-bundle slot; returns the raw
    COMMIT reply (the caller checks ACK vs NACK) or None on a transport failure.
    A BEGIN NACK (firmware without the doom slots) is returned as-is so the caller
    reports a clear failure — the shipping HIL firmware always builds the slots
    (qmk-test.yml POLYKYBD_DOOM_PACK=yes), so a NACK now means the routing broke."""
    import binascii, struct
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    begin = bytes([POLY_CHANNEL, CMD_FONTPACK_BEGIN]) + struct.pack("<IIB", len(payload), crc, bundle_id)
    ready = False
    no_reply = 0
    for attempt in range(DOOM_BEGIN_ERASE_ATTEMPTS):
        reply = raw.send(begin, timeout_ms=15000)
        if reply and len(reply) >= 3 and reply[2] == ord('.'):
            ready = True
            break
        if reply and len(reply) >= 3 and reply[2] == ord('~'):
            log(f"  BEGIN: erasing doom slot 0x{bundle_id:02x}... (attempt {attempt + 1})")
            no_reply = 0        # erase is progressing — not a dead board
            time.sleep(0.3)
            continue
        if reply and len(reply) >= 3 and reply[2] == ord('!'):
            log(f"  BEGIN NACK for pseudo bundle 0x{bundle_id:02x} — firmware without the doom slots?")
            return reply
        # No/short reply: the board may be dead. Each of these already burned
        # ~attempts×timeout_ms inside raw.send, so fail fast on a run of them
        # instead of spending the whole (long) erase budget here.
        no_reply += 1
        if no_reply >= DOOM_BEGIN_NO_REPLY_MAX:
            log(f"  BEGIN: no reply {no_reply}x in a row (last {reply!r}) — giving up (keyboard dead?)")
            break
        log(f"  BEGIN: no/short reply {reply!r} (attempt {attempt + 1}) — retrying")
        time.sleep(0.3)
    if not ready:
        log("  FAIL: FONTPACK_BEGIN never became ready")
        return None
    for off in range(0, len(payload), FONTPACK_CHUNK_SIZE):
        part = payload[off:off + FONTPACK_CHUNK_SIZE]
        padded = part + b"\xff" * (FONTPACK_CHUNK_SIZE - len(part))
        chunk = bytes([POLY_CHANNEL, CMD_FONTPACK_CHUNK]) + struct.pack("<I", off) + padded
        reply = raw.send(chunk, timeout_ms=8000)
        if not (reply and len(reply) >= 3 and reply[2] == ord('.')):
            _fontpack_abort(raw)
            log(f"  FAIL: FONTPACK_CHUNK @ {off} not ACKed: {reply!r}")
            return None
    return raw.send(bytes([POLY_CHANNEL, CMD_FONTPACK_COMMIT]), timeout_ms=8000)


def test_doomwad_slot_roundtrip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Flash a tiny stub WHX (valid 'IWHX' magic, 2 chunks) to the DOOMWAD pseudo
    bundle (0x7F) and assert COMMIT ACKs — the COMMIT success gate re-reads the
    just-written slot and checks the WHX magic in place, so a '.' proves the whole
    routing chain: pseudo-bundle -> FW_TARGET_DOOMWAD -> the game-data slot at the
    top of the resource region, on both halves (the BEGIN bridges the slave).

    Side effect: overwrites the rig's game-data slot with the stub (harmless — the
    rig never starts the game; a real install rewrites the slot). On firmware
    without the doom slots the BEGIN NACKs -> FAIL (the shipping HIL image always
    has the slots, so a NACK is a real routing regression, not tolerated any more)."""
    stub = b"IWHX" + bytes(range(60))   # 64 bytes -> exercises a 2-chunk transfer
    reply = _doom_slot_flash(raw, log, stub, DOOMWAD_BUNDLE_ID)
    if not (reply and len(reply) >= 3 and reply[2] == ord('.')):
        log(f"  FAIL: DOOMWAD COMMIT rejected (in-place WHX magic gate): {reply!r}")
        return False
    log("  COMMIT ok — WHX stub accepted into the game-data slot on both halves")
    return True


def test_doompack_commit_magic_gate(raw: RawHID, log: Callable[[str], None]) -> bool:
    """The DOOMPACK (0x7E) COMMIT gate is an O(1) in-place header check: 'PlyX'
    magic + image_size fits the slot. Assert both directions: a bad-magic image
    must NACK, then a minimal valid PlyX header (image_size 0) must ACK — so the
    slot is left in a well-formed (empty) state, which the game-entry loader
    refuses gracefully (fire-demo fallback), never a crash."""
    import struct
    bad = b"NOPE" + b"\x00" * 60
    reply = _doom_slot_flash(raw, log, bad, DOOMPACK_BUNDLE_ID)
    if reply is None:
        return False
    if len(reply) >= 3 and reply[2] == ord('.'):
        log("  FAIL: COMMIT ACKed a non-PlyX image — the magic gate is not checking")
        return False
    log(f"  bad-magic image rejected as expected ({reply!r})")

    # 64-byte header only: magic + abi 0 + image_size 0 + zeroed rest — passes the
    # COMMIT header sanity; the loader's full CRC/ram-pairing check runs at game entry.
    good = struct.pack("<4sII", b"PlyX", 0, 0) + b"\x00" * 52
    reply = _doom_slot_flash(raw, log, good, DOOMPACK_BUNDLE_ID)
    if not (reply and len(reply) >= 3 and reply[2] == ord('.')):
        log(f"  FAIL: minimal valid PlyX header rejected: {reply!r}")
        return False
    log("  COMMIT ok — engine-pack slot header gate passes valid, rejects invalid")
    return True


# --- signed DOOM engine-pack round trip (FW-9, TIER_DOOM) --------------------
# The magic gate above proves the flash TRANSPORT and the COMMIT header check.
# These three tests prove the LOAD-time Ed25519 gate in doom_pack_load.c: a pack
# signed with the key the HIL image was built against loads and the engine runs;
# a tampered or unsigned pack is refused and the fire demo runs instead. All three
# read the ungated `doom:` printf console lines (needs_console) — the verdict is
# NOT observable over HID, only on the console (and the idle-transition line is
# uprint/debug-gated, so we key off the doom loader's own printf lines).
#
# CI (qmk-test.yml `doom` opt-in) builds the HIL images against an EPHEMERAL key
# and ships ONE .plyx signed with it via --plyx-valid; the rig derives the tampered
# and unsigned variants below, so only one artifact crosses and the derivation is
# unit-tested. Without --plyx-valid these SKIP (the TIER_DOOM gate).
DOOM_SIG_SIZE = 64  # doom_pack_abi.h DOOM_PACK_SIG_SIZE — the trailing Ed25519 sig

# --- FW-9 follow-up: narrowing the intermittent post-doom slave wedge ---------
# On a doom-slot reflash AFTER the doom engine has run, the SLAVE half sometimes
# wedges: the master erases its 52 sectors, then FONTPACK_BEGIN reports the slave
# not ready (slave_ack=0xe4 = SYNC_GIVEUP) and the flash fails with the board dead.
# It is a coin-flip, and the qmk-side capture proved the slave never runs doom
# itself (its core1 is the plain RLE service throughout) — so the doom linkage is
# a CROSS-LINK effect of the master's IDDQD session (the doom-mirror RLE stream to
# the slave / the ~150 ms per-frame busy window), not doom on the slave.
#
# This soak isolates ONE variable: it flashes the SAME ~211 KB engine pack to the
# doom slot repeatedly with NO IDDQD run between the flashes — the "repeated big
# pack flashes ALONE" arm. It runs BEFORE the three IDDQD load tests, so none of
# its flashes is preceded by a doom run. Paired with those tests (the "flash +
# run" arm), the two outcomes discriminate the trigger:
#   * this soak WEDGES  -> repeated erases alone suffice; the doom mirror/run is
#     NOT required, so the fault is the fw_staging erase halt/restart vs the
#     slave's plain RLE-service core1 (look there, independent of doom).
#   * this soak is CLEAN but the IDDQD tests wedge -> the doom RUN (IDDQD + the
#     mirror stream to the slave) is what primes it; look at the doom-mirror path.
# It flashes MORE times than the IDDQD arm and drops the run, so if flashes alone
# could trigger it, this arm should trigger it MORE readily, not less.
#
# ⚠️ COST: each full ~211 KB doom-slot flash is ~5 min of bridged chunk streaming
# on the rig (measured run #923: 3773 chunks, and slow even on the FIRST flash
# before any doom run — the stream cost is the bridged-flash baseline, not a
# post-doom effect). So this soak alone adds ~N×5 min to the doom job. 3 keeps it
# ~15 min for a P(catch)≈0.66 at a per-flash wedge rate ~0.3; raise it for more
# confidence at ~5 min/step if the rig has the time.
DOOM_FLASH_SOAK_REPEATS = 3

_DOOM_VALID_PLYX: "bytes | None" = None


def set_doom_pack(pack: "bytes | None") -> None:
    """Give the TIER_DOOM tests the signed .plyx to flash (the runner calls this
    from --plyx-valid). Left None otherwise, which — together with caps['doom'] —
    keeps those tests skipped."""
    global _DOOM_VALID_PLYX
    _DOOM_VALID_PLYX = pack


def _doom_tamper_sig(pack: bytes) -> bytes:
    """Flip one bit of the trailing signature. Header + image stay byte-for-byte
    valid (magic / CRC / ram-pairing all pass at load), so the loader reaches the
    Ed25519 check and takes the 'signature is INVALID' branch — a genuine authorship
    failure, not a corrupt image that would be caught earlier."""
    if len(pack) <= DOOM_SIG_SIZE:
        raise ValueError("pack too short to carry a signature")
    b = bytearray(pack)
    b[-1] ^= 0x01
    return bytes(b)


def _doom_strip_sig(pack: bytes) -> bytes:
    """Drop the 64-byte trailer, leaving header+image with no signature. At load the
    firmware reads the signature slot one past the flashed bytes (erased flash =
    0xFF) and reports 'is unsigned' — the pre-signing pack case."""
    if len(pack) <= DOOM_SIG_SIZE:
        raise ValueError("pack too short to carry a signature")
    return pack[:-DOOM_SIG_SIZE]


def classify_doom_verdict(lines) -> str:
    """Reduce the captured `doom:` console lines to one verdict token: 'loaded'
    (accept), 'invalid' / 'unsigned' (the two FW-9 refusals), or 'none' (the loader
    logged no signature verdict at all). Pure so it can be unit-tested against the
    firmware's exact strings without hardware. The two refusals are checked before
    'loaded' so a stray earlier 'loaded' from a prior attempt can't mask a refusal."""
    joined = "\n".join(lines)
    if "signature is INVALID" in joined:
        return "invalid"
    if "is unsigned" in joined:
        return "unsigned"
    if "pack v" in joined and "loaded" in joined:
        return "loaded"
    return "none"


def _doom_idle_verdict(raw: RawHID, log: Callable[[str], None], pack: bytes):
    """Flash `pack` to the DOOMPACK slot, engage the IDDQD screensaver, and return
    ``(committed, lines)`` — whether COMMIT ACKed, and the console lines the loader
    emitted while trying to load it. The IDDQD idle is what triggers the load; the
    loader logs its verdict via ungated printf, and `attract screensaver up` fires
    on BOTH the accept and the fire-demo-fallback paths, so it is the anchor to
    wait on before reading the verdict out of the captured lines. Stops idle and
    restores the previous style in a finally."""
    reply = _doom_slot_flash(raw, log, pack, DOOMPACK_BUNDLE_ID)
    if not (reply and len(reply) >= 3 and reply[2] == ord('.')):
        log(f"  FAIL: DOOMPACK COMMIT rejected: {reply!r}")
        return False, []
    cur = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, 0xFF]))
    original = cur[3] if (cur and len(cur) >= 4) else IDLE_STYLE_EDEN
    try:
        set_resp = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, IDLE_STYLE_IDDQD]))
        if not _resp_ok(set_resp, CMD_IDLE_STYLE, log, expect_status=ACK):
            log("  FAIL: firmware rejected IDLE_STYLE_IDDQD")
            return True, []
        mark = TAP.mark()
        start = raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STATE, 1]))
        if not _resp_ok(start, CMD_IDLE_STATE, log, expect_status=ACK):
            return True, []
        log(f"  IDDQD idle started — waiting up to {DOOM_LOAD_TIMEOUT_S:.0f}s for the loader")
        up = TAP.wait_for("attract screensaver up", mark, timeout=DOOM_LOAD_TIMEOUT_S)
        lines = TAP.since(mark)
        if up is None:
            log("  (no 'attract screensaver up' line — the screensaver never engaged)")
        for ln in lines:
            if ln.startswith("doom:"):
                log(f"  firmware: {ln}")
        return True, lines
    finally:
        raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STATE, 0]))
        raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, original]))


def test_doompack_flash_only_soak(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Flash the valid engine pack to the doom slot DOOM_FLASH_SOAK_REPEATS times
    back-to-back with NO IDDQD run in between — the "repeated big pack flashes
    alone" arm of the post-doom wedge narrowing (see DOOM_FLASH_SOAK_REPEATS).

    Each flash exercises the same 52-sector deferred erase + ~211 KB stream +
    COMMIT that the IDDQD tests do, and bridges the slave (halting/restarting its
    core1) — but the slave's core1 is never driven by the doom-mirror RLE stream,
    because no IDDQD idle is engaged. A wedge here (a BEGIN that never becomes
    ready / a run of no-replies, i.e. slave_ack=0xe4 on the master console) is the
    finding that repeated erases alone suffice; a clean run across every repeat
    points the finger at the doom RUN instead. Runs BEFORE the three IDDQD tests
    so none of its flashes is post-doom-run. Leaves a valid loadable pack in the
    slot (the accept test re-flashes it anyway)."""
    if _DOOM_VALID_PLYX is None:
        log("  FAIL: TIER_DOOM ran without a --plyx-valid pack (runner misconfigured)")
        return False
    ok = 0
    for i in range(DOOM_FLASH_SOAK_REPEATS):
        log(f"  flash {i + 1}/{DOOM_FLASH_SOAK_REPEATS} (no IDDQD run before it)")
        reply = _doom_slot_flash(raw, log, _DOOM_VALID_PLYX, DOOMPACK_BUNDLE_ID)
        if not (reply and len(reply) >= 3 and reply[2] == ord('.')):
            log(f"  FAIL: doom-slot flash {i + 1}/{DOOM_FLASH_SOAK_REPEATS} did not "
                f"COMMIT ({reply!r}) — the slave wedged on a reflash with NO doom "
                "run before it, so repeated erases ALONE trigger the wedge")
            return False
        # A live master answers GET_ID; a wedged split link does not stop the
        # master answering HID, so this only rules out a full master death — the
        # real wedge tell is the BEGIN-never-ready above and slave_ack on console.
        if not _master_alive(raw, log):
            log(f"  FAIL: master unresponsive after flash {i + 1}/{DOOM_FLASH_SOAK_REPEATS}")
            return False
        ok += 1
    log(f"  PASS: {ok}/{DOOM_FLASH_SOAK_REPEATS} back-to-back doom-slot flashes with no "
        "IDDQD run — repeated erases alone did NOT wedge the slave this run")
    return True


def test_doompack_signed_load(raw: RawHID, log: Callable[[str], None]) -> bool:
    """A .plyx signed with the key the HIL image was built against LOADS — the FW-9
    Ed25519 accept path. Flash it, engage IDDQD, and confirm the console logs
    `doom: pack vN loaded`. Requires --plyx-valid (SKIPs via the TIER_DOOM gate
    otherwise)."""
    if _DOOM_VALID_PLYX is None:
        log("  FAIL: TIER_DOOM ran without a --plyx-valid pack (runner misconfigured)")
        return False
    committed, lines = _doom_idle_verdict(raw, log, _DOOM_VALID_PLYX)
    if not committed:
        return False
    verdict = classify_doom_verdict(lines)
    if verdict != "loaded":
        log(f"  FAIL: a correctly-signed pack was not loaded (verdict={verdict}) — the "
            "accept path is broken, or the pack was not signed with this image's key")
        return False
    log("  PASS: signed pack loaded — the Ed25519 accept path works")
    return True


def test_doompack_tampered_refused(raw: RawHID, log: Callable[[str], None]) -> bool:
    """A pack whose signature bit is flipped is REFUSED at load — header/image/CRC
    all still valid, so the loader reaches the Ed25519 check and takes the
    'signature is INVALID' branch, then runs the fire demo instead of branching into
    the image. This is the core FW-9 property: an unauthenticated pack does not
    execute."""
    if _DOOM_VALID_PLYX is None:
        log("  FAIL: TIER_DOOM ran without a --plyx-valid pack (runner misconfigured)")
        return False
    committed, lines = _doom_idle_verdict(raw, log, _doom_tamper_sig(_DOOM_VALID_PLYX))
    if not committed:
        return False
    verdict = classify_doom_verdict(lines)
    if verdict != "invalid":
        log(f"  FAIL: a tampered-signature pack was not refused as INVALID "
            f"(verdict={verdict}) — the FW-9 gate is not authenticating the pack")
        return False
    log("  PASS: tampered-signature pack refused (fire demo runs, image not executed)")
    return True


def test_doompack_unsigned_refused(raw: RawHID, log: Callable[[str], None]) -> bool:
    """A pack with no signature trailer is REFUSED at load ('is unsigned'), never
    branched into — the pre-signing pack case. There is deliberately NO on-keycap
    prompt here (unlike an unsigned firmware image): the load runs at idle with
    nobody present, so it is refused outright."""
    if _DOOM_VALID_PLYX is None:
        log("  FAIL: TIER_DOOM ran without a --plyx-valid pack (runner misconfigured)")
        return False
    committed, lines = _doom_idle_verdict(raw, log, _doom_strip_sig(_DOOM_VALID_PLYX))
    if not committed:
        return False
    verdict = classify_doom_verdict(lines)
    if verdict != "unsigned":
        log(f"  FAIL: an unsigned pack was not refused as unsigned (verdict={verdict})")
        return False
    log("  PASS: unsigned pack refused (fire demo runs, image not executed)")
    return True


def _fontpack_abort(raw: RawHID) -> None:
    """Best-effort abort: a COMMIT clears fw_up mode on both halves regardless of
    outcome, so a partial transfer can't leave the keyboard stuck mid-flash."""
    try:
        raw.send(bytes([POLY_CHANNEL, CMD_FONTPACK_COMMIT]), timeout_ms=5000)
    except Exception:   # noqa: BLE001 — cleanup must never mask the original failure
        pass


def test_fontpack_wipe_roundtrip(raw: RawHID, log: Callable[[str], None]) -> bool:
    """Flash the 32-byte empty-pack sentinel to bundle slot 0 over the REAL HID
    transport (BEGIN/CHUNK/COMMIT, cmds 0x50-0x52) and assert COMMIT succeeds.

    This is the only HIL test that exercises the actual per-bundle font-pack flash
    path: the deferred-erase staging, the master-slave bridge, and — the key
    assertion — the COMMIT success gate (`fontpack_slot_present`), which a field bug
    once made falsely NACK on a wipe (it gated on the whole-pack `fontpack_present()`,
    false once every slot is empty). A NACK here is exactly that regression. Then
    re-reads GET_ID and confirms slot 0 now advertises content_version 0.

    Side effect: empties the 'symbol' bundle (slot 0) on the rig. That is harmless on
    the unattended rig (no host attached) and a real PolyKybdHost re-flashes it on the
    next connect. The empty-pack flash erases only ~2 sectors, so it is fast. v6+ only
    (gated in TESTS)."""
    import binascii, struct
    SLOT = 0
    pack = _build_empty_fontpack()
    pack_crc = binascii.crc32(pack) & 0xFFFFFFFF

    # -- FONTPACK_BEGIN: erases the slot's first sectors; answers '.' ready, '~' while
    #    erasing (retry), '!' on failure. Generous timeout so the single read catches
    #    the reply rather than triggering send()'s internal re-write.
    begin = bytes([POLY_CHANNEL, CMD_FONTPACK_BEGIN]) + struct.pack("<IIB", len(pack), pack_crc, SLOT)
    ready = False
    for attempt in range(20):
        reply = raw.send(begin, timeout_ms=15000)
        if reply and len(reply) >= 3 and reply[2] == ord('.'):
            ready = True
            break
        if reply and len(reply) >= 3 and reply[2] == ord('~'):
            log(f"  BEGIN: erasing slot {SLOT}... (attempt {attempt + 1})")
            time.sleep(0.3)
            continue
        if reply and len(reply) >= 3 and reply[2] == ord('!'):
            log("  FAIL: FONTPACK_BEGIN NACK — slave half could not be prepared")
            return False
        log(f"  BEGIN: no/short reply {reply!r} (attempt {attempt + 1}) — retrying")
        time.sleep(0.3)
    if not ready:
        log("  FAIL: FONTPACK_BEGIN never became ready")
        return False

    # -- one FONTPACK_CHUNK at offset 0 (32 real bytes, padded to 56 with 0xff).
    padded = pack + b"\xff" * (FONTPACK_CHUNK_SIZE - len(pack))
    chunk = bytes([POLY_CHANNEL, CMD_FONTPACK_CHUNK]) + struct.pack("<I", 0) + padded
    reply = raw.send(chunk, timeout_ms=8000)
    if not (reply and len(reply) >= 3 and reply[2] == ord('.')):
        _fontpack_abort(raw)
        log(f"  FAIL: FONTPACK_CHUNK not ACKed: {reply!r}")
        return False

    # -- FONTPACK_COMMIT: verifies the staged CRC and loads the slot. '.' is the
    #    wipe-success gate that the field bug broke, so only '.' passes — but say
    #    WHICH failure it was, since 'R' (data) and 'L' (link) send an investigation
    #    in opposite directions.
    reply = raw.send(bytes([POLY_CHANNEL, CMD_FONTPACK_COMMIT]), timeout_ms=8000)
    if not (reply and len(reply) >= 3 and reply[2] == FONTPACK_COMMIT_OK):
        log(f"  FAIL: FONTPACK_COMMIT did not succeed: {describe_fontpack_commit(reply)}")
        log(f"        raw reply: {reply!r}")
        return False
    cver = struct.unpack_from("<H", reply, 3)[0] if len(reply) >= 5 else None
    log(f"  COMMIT ok — slot {SLOT} wiped (content_version={cver})")

    # -- verify GET_ID's v6 version block now reports content_version 0 for the slot.
    versions = parse_fontpack_versions(raw.send(bytes([POLY_CHANNEL, CMD_GET_ID])))
    if versions is None:
        log("  FAIL: no font-pack version block in GET_ID after the wipe")
        return False
    if versions.get(SLOT, -1) != 0:
        log(f"  FAIL: slot {SLOT} reports version {versions.get(SLOT)} != 0 after wipe")
        return False
    log(f"  GET_ID confirms slot {SLOT} content_version 0 after wipe — flash transport OK")
    return True


def test_layer_names(raw: RawHID, log: Callable[[str], None]) -> bool:
    """GET_LAYER_NAMES (cmd 35, protocol v14+): the layers the host may remap, named.

    The host layout editor labels its layer tabs from this. It used to label them
    from a file generated out of the firmware's layers.h at build time, which went
    stale silently and described an enum the firmware no longer had — so the point
    of the command is that a name the keyboard states itself cannot drift.

    ⚠️ The cross-check against id_dynamic_keymap_get_layer_count is the POINT of this
    test. The firmware answers both from the same constant precisely so the editor
    cannot size its tab strip from one and label it from the other; if the two ever
    disagree, the editor draws a tab it has no name for. Nothing else would catch a
    change that made cmd 35 report DYNAMIC_KEYMAP_LAYER_COUNT instead of the write cap.

    The payload is [total][count] then count NUL-terminated names. The total is
    read first and bounds everything after it, so the report's zero fill is never
    examined and an unnamed layer stays expressible.

    Read-only, so there is nothing to restore.

    ``send_and_read_all`` has no built-in retry, and this was the ONE test in its
    neighbourhood without tolerance for the master's transient deaf windows: on
    qmk#236's first HIL run the tests on either side each *recovered 1 read
    timeout* via ``send()``'s retry while this one failed with "no reply" — a
    false red on a command whose reply is perfectly idempotent (no one-shot
    marker). Same remedy as the packed language list above: retry the whole
    exchange (fresh handle each time) when NOTHING arrives. A reply that arrives
    but fails validation is a real protocol fault and still fails immediately
    without burning retries.
    """
    packets: list[bytes] = []
    for attempt in range(3):
        # The lengthened timeouts are half of the remedy (same values as the
        # packed language list): 3 x the default 1 s first-read would give a
        # ~3 s total window, which the observed 5104 ms deaf interval outlasts.
        packets = raw.send_and_read_all(
            bytes([POLY_CHANNEL, CMD_GET_LAYER_NAMES]),
            first_timeout_ms=2500, next_timeout_ms=600)
        if packets:
            if attempt:
                log(f"  reply arrived on attempt {attempt + 1}/3")
            break
        tail = "retrying" if attempt + 1 < 3 else "giving up"
        log(f"  attempt {attempt + 1}/3: no reply (master busy?) — {tail}")
    if not packets:
        log("  FAIL: no reply to GET_LAYER_NAMES")
        return False
    log(f"layer-names reply: {len(packets)} report(s)")

    payload = bytearray()
    for i, pkt in enumerate(packets):
        if len(pkt) < 4 or pkt[0] != POLY_CHANNEL or pkt[1] != CMD_GET_LAYER_NAMES:
            log(f"  FAIL: report {i} is not a GET_LAYER_NAMES reply: {pkt[:4]!r}")
            return False
        if pkt[2] != ACK:
            log(f"  FAIL: report {i} status {pkt[2]!r} != ACK")
            return False
        payload += pkt[3:]

    if len(payload) < LAYER_NAMES_HEADER:
        log("  FAIL: reply too short to carry a header")
        return False
    total, count = payload[0], payload[1]
    if total < LAYER_NAMES_HEADER + 1 or total > 255:
        log(f"  FAIL: implausible total length {total}")
        return False
    if count == 0 or count > MAX_LAYERS:
        log(f"  FAIL: implausible layer count {count} (expected 1..{MAX_LAYERS})")
        return False
    if len(payload) < total:
        log(f"  FAIL: truncated — {len(payload)} bytes for a {total}-byte payload")
        return False

    # The total is what bounds this slice, so the report's zero fill is never read
    # as a name separator — and an UNNAMED layer (a bare terminator) stays
    # distinguishable from that fill.
    names = [n.decode("ascii", "replace")
             for n in bytes(payload[LAYER_NAMES_HEADER:total]).split(b"\x00")[:count]]
    if len(names) < count:
        log(f"  FAIL: total {total} carries {len(names)} names, count says {count}")
        return False
    log(f"  total={total} count={count}: {', '.join(repr(n) for n in names)}")

    for idx, name in enumerate(names):
        if not name:
            log(f"  note: layer {idx} is unnamed")
            continue
        if not all(0x20 <= ord(c) < 0x7F for c in name):
            log(f"  FAIL: layer {idx} name {name!r} is not printable ASCII")
            return False
        if len(name) > LAYER_NAME_MAX:
            log(f"  FAIL: layer {idx} name {name!r} exceeds {LAYER_NAME_MAX} chars")
            return False

    reply = raw.send(bytes([VIA_DYNAMIC_KEYMAP_GET_LAYER_COUNT]))
    if not reply or len(reply) < 2 or reply[0] != VIA_DYNAMIC_KEYMAP_GET_LAYER_COUNT:
        log(f"  FAIL: could not read the dynamic layer count back: {reply!r}")
        return False
    if reply[1] != count:
        log(f"  FAIL: cmd 35 names {count} layers but the layer count reports "
            f"{reply[1]} — the editor would draw a tab it has no name for")
        return False
    log(f"  layer count agrees ({reply[1]})")
    return True



# Ordered cheap → expensive and dependency-aware:
#   * the structural enumerate check first (no HID traffic);
#   * fresh-boot BEFORE any other GET_ID — it must see and consume the one-shot
#     '*' marker, which is why "raw HID GET_ID" that follows accepts a plain ACK;
#   * read-only queries and error/bounds paths next (no persistent state change);
#   * mutate+restore round-trips, then the upload/soak guards which are the most
#     likely to disrupt the device, last.
TESTS = [
    {"name": "single master enumerates",        "fn": test_single_master_enumerates},
    {"name": "fresh-boot marker clears",        "fn": test_fresh_boot_marker},
    {"name": "raw HID GET_ID",                  "fn": test_get_id},
    {"name": "font-pack version block (v6)",     "fn": test_fontpack_version_block, "min_protocol": 6},
    {"name": "get current language",            "fn": test_get_lang},
    # cmd 8 retiring + the packed list (cmd 27) only exist on protocol v2+; on a
    # pre-v2 board these SKIP instead of failing the run (see skip_reason).
    {"name": "legacy ASCII lang list NACKs (retired)", "fn": test_legacy_lang_list_nacked, "min_protocol": 2},
    {"name": "enumerate language list (packed, v2)", "fn": test_enumerate_languages_packed, "min_protocol": 2},
    {"name": "get default layer",               "fn": test_get_default_layer},
    {"name": "reset dynamic keymap (echo + live)", "fn": test_reset_keymap},
    {"name": "unknown command NACKs",           "fn": test_unknown_command_nacks},
    {"name": "set brightness (ACK + range NACK)", "fn": test_set_brightness},
    # SET_BRIGHTNESS flags (VOLATILE / AUTO_ON / AUTO_OFF) only exist on protocol
    # v5+; on an older board this SKIPs instead of failing (see skip_reason).
    {"name": "set brightness flags (v5: volatile/auto)", "fn": test_set_brightness_flags, "min_protocol": 5},
    {"name": "set unicode mode (ACK + NACK)",   "fn": test_set_unicode_mode},
    {"name": "idle wake ACK",                   "fn": test_idle_wake},
    {"name": "idle style round-trip (v4)",      "fn": test_idle_style_round_trip, "min_protocol": 4},
    {"name": "OS round-trip (v7)",              "fn": test_os_round_trip, "min_protocol": 7},
    {"name": "glyph script round-trip (v9)",    "fn": test_glyph_script_round_trip, "min_protocol": 9},
    {"name": "glyph script expansion (v10)",    "fn": test_glyph_script_expansion,  "min_protocol": 10},
    {"name": "glyph size round-trip (v13)",     "fn": test_glyph_size_round_trip,   "min_protocol": 13},
    {"name": "layer names (v14)",               "fn": test_layer_names,             "min_protocol": 14},
    {"name": "macro round-trip (v15)",          "fn": test_macro_round_trip,        "min_protocol": 15},
    {"name": "overlay flags round-trip",        "fn": test_overlay_flags_round_trip},
    # Animation + idle. Both are slow by nature (the intro is ~14 s, the idle fade
    # 10 s), so they are EXTENDED-tier: they run when a release or a big change
    # asks for them, not on every push. The Eden one needs the console to CONFIRM
    # idle engaged — without it the test would assert nothing.
    {"name": "replay startup animation (cmd 31)", "fn": test_replay_animation,
     "min_fw": EDEN_MIN_FW, "tier": TIER_EXTENDED},
    {"name": "idle engages + Eden screensaver keeps HID alive (cmd 15/28)",
     "fn": test_idle_eden_screensaver, "min_protocol": 4, "min_fw": EDEN_MIN_FW,
     "needs_console": True, "tier": TIER_EXTENDED},
    # picks a second language from the packed list (cmd 27) — protocol v2+ only.
    {"name": "language round-trip",             "fn": test_language_round_trip, "min_protocol": 2},
    # Every layout, one after another: the demo-mode crash (hy-AM, 2026-10-01) was
    # one layout's legend drawing a glyph no font had. v16 for the cmd 39 crash
    # records it compares; EXTENDED for the ~160 switches.
    {"name": "every language draws without a crash (cmd 9 sweep)", "fn": test_language_sweep,
     "min_protocol": 16, "tier": TIER_EXTENDED},
    {"name": "plain overlay keeps master alive", "fn": test_plain_overlay_keeps_master_alive, "min_protocol": 11},
    {"name": "compressed overlay keeps master alive (core1)", "fn": test_compressed_overlay_keeps_master_alive},
    # The packet shapes the single-packet guards above never reach: the cmd-17
    # continuation (every non-blank image uses it) and the ROI pair (cmd 18/19),
    # which had no liveness guard at all despite its own core1 hand-off.
    {"name": "compressed overlay spans two packets (cmd 16+17)",
     "fn": test_compressed_overlay_two_packets},
    {"name": "ROI overlay keeps master alive (cmd 18/19 + bounds clamp)",
     "fn": test_roi_overlay_keeps_master_alive},
    {"name": "overlay mapping widths 8/9/10/11 (v12 cmd 33)", "fn": test_overlay_mapping_widths,
     "min_protocol": 12},
    {"name": "mapping flags ride cmd 33 (v21, console)", "fn": test_mapping_flags_ride_cmd_33,
     "min_protocol": MAPPING_FLAGS_MIN_PROTOCOL, "needs_console": True},
    # PRC overlays (cmd 41): real records decoded on both halves, the longest
    # decode, and the three malformed records. The console twin proves the
    # malformed ones are REFUSED rather than merely survived.
    {"name": "PRC overlay records keep master alive (v19 cmd 41)",
     "fn": test_prc_overlay_keeps_master_alive, "min_protocol": PRC_MIN_PROTOCOL},
    {"name": "PRC malformed record is refused (v19 cmd 41, console)",
     "fn": test_prc_malformed_record_is_refused, "min_protocol": PRC_MIN_PROTOCOL,
     "needs_console": True},
    # Icon library fills (cmd 42): the replied verdict the host's fallback acts on.
    {"name": "icon fill replies (v20 cmd 42)",
     "fn": test_icon_fill_replies, "min_protocol": ICON_FILL_MIN_PROTOCOL},
    {"name": "GET_ID stress",                   "fn": test_get_id_stress},
    # Deliberate bridged-traffic soak + the firmware's own link health counter.
    # After the stress burst (which wants a quiet master) and before the flash
    # tests. Needs the console (the counter is only observable there) and ~5 s of
    # deliberate traffic, so it is EXTENDED-tier.
    {"name": "split link health under a bridged soak (cmd 21)",
     "fn": test_split_link_health, "needs_console": True, "tier": TIER_EXTENDED},
    # The firmware's own crash confession, half one: cmd 39 (a fresh record means
    # the boot before this one faulted; also exercises clear). The console scan is
    # the LAST entry of the list, below.
    {"name": "crash record command (v16 cmd 39)", "fn": test_crash_record_command,
     "min_protocol": 16},
    # Reboot until a boot goes wrong. AFTER the crash-record test (it clears the
    # archive) and before the flash tests. EXTENDED: N reboots of several seconds.
    {"name": "boot loop (v22 cmd 43)", "fn": test_boot_loop,
     "min_protocol": REBOOT_MIN_PROTOCOL, "tier": TIER_EXTENDED},
    # Real per-bundle font-pack flash (BEGIN/CHUNK/COMMIT) of the empty-pack sentinel
    # to slot 0 — exercises the flash transport + the COMMIT slot-present success gate.
    # LAST: it empties the 'symbol' bundle (a host re-flashes it on the next connect).
    # v6+ only; a pre-v6 board SKIPs it (no font-pack flash transport).
    {"name": "font-pack wipe round-trip (v6 flash)", "fn": test_fontpack_wipe_roundtrip, "min_protocol": 6},
    # Doom easter egg resource slots (pseudo bundles 0x7F/0x7E over the same flash
    # transport). Not visible in GET_ID (no protocol bump — the transport is the
    # v6 fontpack flow). The doom firmware is now the shipping shape: qmk-test.yml
    # builds the HIL images with POLYKYBD_DOOM_PACK=yes, so every HIL run has the
    # slots — the xfail markers were removed once the rig XPASSed them (qmk #122).
    # These now hard-PASS on doom firmware and hard-FAIL if the slot routing breaks.
    {"name": "doom game-data slot round-trip (WHX stub)", "fn": test_doomwad_slot_roundtrip,
     "min_protocol": 6},
    {"name": "doom engine-pack slot magic gate", "fn": test_doompack_commit_magic_gate,
     "min_protocol": 6},
    # FW-9 follow-up: narrow the intermittent post-doom slave wedge. This soak
    # flashes the doom slot DOOM_FLASH_SOAK_REPEATS× back-to-back with NO IDDQD run — the
    # "repeated erases alone" arm. It runs BEFORE the three IDDQD tests (so none of
    # its flashes is post-doom-run); a wedge here vs a clean-here-but-wedge-there
    # discriminates whether the doom RUN is required to prime the fault. TIER_DOOM
    # (needs the signed --plyx-valid pack and is slow, same as the load tests).
    {"name": "doom engine-pack flash-only soak (no IDDQD run)",
     "fn": test_doompack_flash_only_soak, "min_protocol": 6,
     "needs_console": True, "tier": TIER_DOOM},
    # FW-9: the LOAD-time Ed25519 gate on the executable engine pack. TIER_DOOM
    # (its own opt-in): they flash a ~230 KB signed .plyx and drive the IDDQD
    # screensaver, and need a signed pack CI builds only on the `hil-doom` label. The
    # tampered/unsigned variants are derived from the signed one on the rig. LAST,
    # after the magic gate: they leave a real (or refused) pack in the slot.
    {"name": "doom signed engine-pack loads (FW-9 accept)",
     "fn": test_doompack_signed_load, "min_protocol": 6,
     "needs_console": True, "tier": TIER_DOOM},
    {"name": "doom tampered-signature pack refused (FW-9)",
     "fn": test_doompack_tampered_refused, "min_protocol": 6,
     "needs_console": True, "tier": TIER_DOOM},
    {"name": "doom unsigned pack refused (FW-9)",
     "fn": test_doompack_unsigned_refused, "min_protocol": 6,
     "needs_console": True, "tier": TIER_DOOM},
    {"name": "handedness resolution is self-consistent (boot banner)",
     "fn": test_handedness_banner, "needs_console": True, "min_fw": "0.27.4"},
    # LAST, whatever tiers ran: the console scan over this run's whole window, so
    # a crash inside any test above — the flash tests and the doom set included —
    # or on the slave lands here rather than reading as a flake.
    {"name": "no firmware crash during the run (console crash: line)",
     "fn": test_no_crash_record, "needs_console": True},
]
