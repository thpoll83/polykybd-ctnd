#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Called by xss-lock when the X screensaver activates (5 min idle).
# Uses the RPi VideoCore mailbox to physically disable the HDMI output, which
# cuts the panel backlight — something DPMS alone cannot do on this display.
# xss-lock keeps this script alive and sends SIGTERM when user activity
# is detected; the EXIT trap re-enables HDMI before the process exits.
vcgencmd display_power 0
trap 'vcgencmd display_power 1' EXIT
sleep infinity
