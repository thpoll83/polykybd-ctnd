"""VIA values on the legacy raw HID ids (protocol v39+). Request packing and reply
parsing, shared by the HIL tests and unit-tested offline.

Mirrors qmk_firmware keyboards/polykybd/lib/hid/hid_com.c legacy_command_kb, which
ports these from QMK's quantum/via.c (VIA_ENABLE is off):
  0x07 / 0x08 channel 3  RGB matrix set / get. data[2] value id: 1 brightness
                         (0..255, VIA's scale), 2 effect (0 = off), 3 speed,
                         4 colour (data[3] hue, data[4] sat). RAM only.
  0x02 value 0x01        uptime: ms since boot, big-endian in data[2..5].
  0x02 value 0x03        switch matrix state: data[2] first row, rows from data[3],
                         one byte per row. Only while debug_enable is on.
  0x03 value 0x05        device indication: toggles the RGB matrix, RAM only.
The reply is the request with the value filled in. A refused request comes back
with data[0] = 0xFF (id_unhandled). Firmware older than v39 sends no reply.
"""

VIA_VALUES_MIN_PROTOCOL = 39

ID_GET_KEYBOARD_VALUE = 0x02
ID_SET_KEYBOARD_VALUE = 0x03
ID_CUSTOM_SET_VALUE = 0x07
ID_CUSTOM_GET_VALUE = 0x08
ID_UNHANDLED = 0xFF

UPTIME = 0x01
SWITCH_MATRIX_STATE = 0x03
DEVICE_INDICATION = 0x05

RGB_MATRIX_CHANNEL = 3
BRIGHTNESS, EFFECT, SPEED, COLOR = 1, 2, 3, 4


def rgb_get_request(value_id: int) -> bytes:
    return bytes([ID_CUSTOM_GET_VALUE, RGB_MATRIX_CHANNEL, value_id, 0, 0])


def rgb_set_request(value_id: int, *value: int) -> bytes:
    return bytes([ID_CUSTOM_SET_VALUE, RGB_MATRIX_CHANNEL, value_id, *(v & 0xFF for v in value)])


def uptime_request() -> bytes:
    return bytes([ID_GET_KEYBOARD_VALUE, UPTIME, 0, 0, 0, 0])


def switch_matrix_request(offset: int = 0) -> bytes:
    return bytes([ID_GET_KEYBOARD_VALUE, SWITCH_MATRIX_STATE, offset & 0xFF])


def indication_request(index: int = 0) -> bytes:
    return bytes([ID_SET_KEYBOARD_VALUE, DEVICE_INDICATION, index & 0xFF])


def is_unhandled(resp) -> bool:
    return bool(resp) and resp[0] == ID_UNHANDLED


def parse_rgb(resp, value_id: int):
    """The value bytes of an RGB matrix get/set reply: (v,) or (hue, sat) for COLOR.
    None when the reply is missing, refused or for another value."""
    if (not resp or len(resp) < 5 or resp[0] not in (ID_CUSTOM_GET_VALUE, ID_CUSTOM_SET_VALUE)
            or resp[1] != RGB_MATRIX_CHANNEL or resp[2] != value_id):
        return None
    return (resp[3], resp[4]) if value_id == COLOR else (resp[3],)


def parse_uptime(resp):
    """Milliseconds since boot, or None when the reply is not an uptime reply."""
    if not resp or len(resp) < 6 or resp[0] != ID_GET_KEYBOARD_VALUE or resp[1] != UPTIME:
        return None
    return int.from_bytes(bytes(resp[2:6]), "big")


def parse_switch_matrix(resp, rows: int):
    """``rows`` row bytes from a switch matrix reply, or None when refused/missing."""
    if (not resp or len(resp) < 3 + rows or resp[0] != ID_GET_KEYBOARD_VALUE
            or resp[1] != SWITCH_MATRIX_STATE):
        return None
    return list(resp[3:3 + rows])


def brightness_round_trip(v: int, max_brightness: int) -> int:
    """What a get returns after a set of ``v``: the firmware stores
    (v * (max + 1)) >> 8 (VIA's scale8) and reports the stored level rounded UP to
    0..255, so a set of a value a get returned keeps the stored level."""
    stored = min((v * (max_brightness + 1)) >> 8, max_brightness)
    return (stored * 255 + max_brightness - 1) // max_brightness
