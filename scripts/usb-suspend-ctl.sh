#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# polykybd-usb-suspend — the four sysfs writes the HIL suspend/wake check needs.
#
# station/usb_suspend.py suspends the master's USB port through runtime PM:
# unbind usbhid from its interfaces, set power/autosuspend_delay_ms and
# power/control, and undo it all on wake. Those are root-only sysfs writes.
#
# SECURITY: granting `sudo tee` for them would be a root write to ANY file. This
# script is granted instead, and it writes only:
#
#   unbind <intf>          /sys/bus/usb/drivers/usbhid/unbind
#   bind <intf>            /sys/bus/usb/drivers/usbhid/bind
#   control <dev> on|auto  /sys/bus/usb/devices/<dev>/power/control
#   delay <dev> <ms>       /sys/bus/usb/devices/<dev>/power/autosuspend_delay_ms
#
# <dev> must be a USB device name (1-1.3) whose idVendor:idProduct is the
# PolyKybd's 2021:2007 (qmk split72/config.h), and <intf> one of its interfaces
# (1-1.3:1.0). So the grant can suspend the keyboard and nothing else.
#
# ⚠️ INSTALLED COPY ONLY, like runner-ctl.sh: setup.sh copies it root-owned to
# /usr/local/sbin and grants that path. Never grant the repo checkout, which the
# station user and self-update can rewrite. A change here takes effect only after
# `sudo bash ./scripts/setup.sh --units-only` on the rig.

set -euo pipefail

readonly USAGE="usage: polykybd-usb-suspend {unbind|bind} <dev:cfg.intf> | control <dev> {on|auto} | delay <dev> <ms>"
readonly VID=2021
readonly PID=2007

# The offline tests point this at a fake tree. As root it is always the real
# sysfs, so the override cannot redirect a privileged write.
SYSFS=/sys/bus/usb
if [[ $EUID -ne 0 && -n "${POLYKYBD_SYSFS_USB:-}" ]]; then
    SYSFS="$POLYKYBD_SYSFS_USB"
fi

fail() { echo "polykybd-usb-suspend: $*" >&2; exit 2; }

# A device name is bus-port[.port...]: no slashes, no dots at the edges.
check_dev() {
    [[ "$1" =~ ^[0-9]+-[0-9]+(\.[0-9]+)*$ ]] || fail "not a USB device name: $1"
    local d="$SYSFS/devices/$1"
    [[ -r "$d/idVendor" && -r "$d/idProduct" ]] || fail "no such device: $1"
    [[ "$(<"$d/idVendor")" == "$VID" && "$(<"$d/idProduct")" == "$PID" ]] \
        || fail "$1 is not the PolyKybd ($VID:$PID)"
}

[[ $# -ge 2 ]] || { echo "$USAGE" >&2; exit 2; }

case "$1" in
    unbind|bind)
        [[ $# -eq 2 ]] || { echo "$USAGE" >&2; exit 2; }
        [[ "$2" =~ ^([0-9]+-[0-9]+(\.[0-9]+)*):[0-9]+\.[0-9]+$ ]] \
            || fail "not a USB interface name: $2"
        check_dev "${BASH_REMATCH[1]}"
        printf '%s' "$2" > "$SYSFS/drivers/usbhid/$1"
        ;;
    control)
        [[ $# -eq 3 ]] || { echo "$USAGE" >&2; exit 2; }
        check_dev "$2"
        [[ "$3" == on || "$3" == auto ]] || fail "control must be on or auto"
        printf '%s' "$3" > "$SYSFS/devices/$2/power/control"
        ;;
    delay)
        [[ $# -eq 3 ]] || { echo "$USAGE" >&2; exit 2; }
        check_dev "$2"
        [[ "$3" =~ ^-?[0-9]{1,7}$ ]] || fail "delay must be an integer of ms"
        printf '%s' "$3" > "$SYSFS/devices/$2/power/autosuspend_delay_ms"
        ;;
    *)
        echo "$USAGE" >&2
        exit 2
        ;;
esac
