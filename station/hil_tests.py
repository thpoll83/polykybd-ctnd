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

The suite exercises every Raw HID command that can be checked in a meaningful,
side-effect-free way on the unattended rig (no human at the keys, no camera, no
GPIO key-matrix injection, HID console off): the read-only identity/state
queries (GET_ID/GET_LANG/GET_LANG_LIST_PACKED/GET_DEFAULT_LAYER), the ACK/NACK
error and bounds paths (unknown command, the retired ASCII GET_LANG_LIST which
must now NACK, out-of-range brightness, invalid unicode mode), the state commands
with a clean round-trip + restore (language, overlay
flags), and the upload + soak paths that guard documented firmware regressions
(overlay upload — plain and core1 RLE — keeping the master alive; rapid GET_ID
stress). Commands that can't be asserted without side effects or extra
infrastructure are catalogued in ``docs/FUTURE_TESTS.md`` (host-driven KEYPRESS
injects real keystrokes; ENTER_BOOTLOADER ends the session; ROI/mapping render
checks need a camera or firmware read-back; handedness/idempotency need flash
control).

Run them via the CLI::

    python -m station.test_runner --left left_hil.uf2 --right right_hil.uf2

See ``docs/FUTURE_TESTS.md`` for the planned-but-not-yet-implemented backlog.
"""
import re
import time
from typing import Callable

from .hid import RawHID, enumerate_raw_interfaces
from .iso_lang_country import decode_packed

# --- Raw HID protocol constants (mirror keyboards/handwired/polykybd/hid_com.c
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
CMD_OVERLAY_FLAGS_ON        = 11  # set overlay flag bits
CMD_OVERLAY_FLAGS_OFF       = 12  # clear overlay flag bits
CMD_SET_BRIGHTNESS          = 13  # set OLED contrast (0..FULL_BRIGHT)
CMD_IDLE_STATE              = 15  # start (1) / stop (0) display idle
CMD_START_COMPRESSED_OVERLAY = 16 # first RLE-compressed overlay packet (core1)
CMD_SET_UNICODE_MODE        = 20  # unicode input mode (0..4)
CMD_GET_DEFAULT_LAYER       = 22  # current default layer index
CMD_IDLE_STYLE              = 28  # get/set idle (anti-burn-in) display style (protocol v4+)

# VIA "reset dynamic keymap" report (bare command id, NOT a 'P' command — see
# test_runner.VIA_DYNAMIC_KEYMAP_RESET). data[0]==0x06 -> legacy_command_kb ->
# dynamic_keymap_reset(); the firmware echoes the request back (no "P<cmd>." ACK).
VIA_DYNAMIC_KEYMAP_RESET    = 0x06

ACK        = ord(".")
NACK       = ord("!")
FRESH_BOOT = ord("*")    # GET_ID status byte when the firmware just (re)booted

# Firmware facts (keyboards/handwired/polykybd/{config.h,base/com.h}).
FULL_BRIGHT          = 50    # max OLED contrast; > this is rejected (NACK)
# SET_BRIGHTNESS flags byte (protocol v5+, data[2]; mirror base/com.h). 0 = the
# legacy persisted set. VOLATILE = a daylight/auto value (applied only while auto
# mode is engaged, never persisted); AUTO_ON / AUTO_OFF engage/leave host-auto.
BR_FLAG_VOLATILE     = 1 << 0
BR_FLAG_AUTO_ON      = 1 << 1
BR_FLAG_AUTO_OFF     = 1 << 2
MAX_LAYERS           = 14    # DYNAMIC_KEYMAP_LAYER_COUNT (split72/config.h)
DISPLAY_OVERLAYS_BIT = 0x01  # overlay_flag DISPLAY_OVERLAYS (base/com.h)
KC_A                 = 0x04  # QMK keycode for 'A'; A..Z = 0x04..0x1D
NUM_SEGMENTS         = 6     # NUM_SEGMENTS_PER_OVERLAY
PLAIN_SEG_BYTES      = 59    # data bytes per plain overlay report (64 - 5 header)
OVERLAY_BYTES        = 360   # NUM_SEGMENTS_PER_OVERLAY * BYTES_PER_SEGMENT
COMPRESSED_TEST_KEYS = 8     # KC_A..KC_H — exercise the core1 path repeatedly


# --- device capability gate ---------------------------------------------------
# The firmware's GET_ID reply (cmd 6) names the board, firmware version and
# protocol version, e.g. the text after the "P\x06." header reads
#   "Split72 0.8.21 P2 HW1 "
# (see keyboards/handwired/polykybd/hid_com.c: name = "P\x06.Split72 " FW_VERSION
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
    first = raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
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


def _read_packed_lang_codes(raw: RawHID, log: Callable[[str], None]):
    """Read GET_LANG_LIST_PACKED (cmd 27) and decode it to a list of 'llCC' codes.

    The list is a count byte + two ISO index bytes per language, possibly split
    across several reports; the leading count makes the total length deterministic
    (no reliance on a read timeout). Decodes via the shared frozen
    station/iso_lang_country.py table. The payload is binary, so reports are
    reassembled by raw byte slices, not text decoding. Returns the list of codes
    (empty if the firmware reports zero languages), or None on any protocol /
    decoding failure (already logged).
    """
    packets = raw.send_and_read_all(bytes([POLY_CHANNEL, CMD_GET_LANG_LIST_PACKED]))
    if not packets:
        log("  FAIL: no packed-list packets received")
        return None
    payload = bytearray()
    for p in packets:
        if not _resp_ok(p, CMD_GET_LANG_LIST_PACKED, log):
            return None
        payload += bytes(p[3:])  # strip "P\x1b." header; keep raw bytes (binary!)
    if not payload:
        log("  FAIL: packed reply had only headers, no payload (count byte missing)")
        return None
    count = payload[0]
    total = 1 + 2 * count
    if len(payload) < total:
        log(f"  FAIL: truncated payload ({len(payload)} bytes, need {total})")
        return None
    try:
        codes = decode_packed(payload[:total])
    except (KeyError, IndexError) as e:
        log(f"  FAIL: could not decode packed list: {e}")
        return None
    if len(codes) != count:
        log(f"  FAIL: decoded {len(codes)} codes but count byte says {count}")
        return None
    return codes


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
        bytes([POLY_CHANNEL, CMD_SEND_OVERLAY, KC_A, 0x00, seg]) + blank
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
    test leaves the master in its transient deaf window (worsened on the rig,
    where master→slave sync is flaky, by the split-sync re-fire fix), so an
    occasional GET_ID times out and then recovers. Only a freeze *signature*
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
    {"name": "overlay flags round-trip",        "fn": test_overlay_flags_round_trip},
    # picks a second language from the packed list (cmd 27) — protocol v2+ only.
    {"name": "language round-trip",             "fn": test_language_round_trip, "min_protocol": 2},
    {"name": "plain overlay keeps master alive", "fn": test_plain_overlay_keeps_master_alive},
    {"name": "compressed overlay keeps master alive (core1)", "fn": test_compressed_overlay_keeps_master_alive},
    {"name": "GET_ID stress",                   "fn": test_get_id_stress},
]
