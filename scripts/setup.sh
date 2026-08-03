#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail

# Parse flags
# --local       Use the current directory as the install location instead of
#               cloning into /opt/polykybd-ctnd. Useful when you have already
#               cloned the repo and want to run it in place.
# --units-only  Install ONLY the systemd units + their sudoers grants into the
#               existing in-place install, then exit. For a rig provisioned
#               before a unit existed (e.g. polykybd-update.service/.timer):
#               re-running the full setup would apt-update and rebuild the venv
#               on a working rig just to drop two files. Idempotent, and it
#               touches neither config.yaml nor the venv.
LOCAL=false
UNITS_ONLY=false
for arg in "$@"; do
    case "$arg" in
        --local)      LOCAL=true ;;
        --units-only) UNITS_ONLY=true; LOCAL=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 1 ;;
    esac
done

# Resolve the real user even when the script is run via sudo. Running the script
# *through* sudo is fine and expected — it needs root for every tee/systemctl —
# but that must not make the station itself run as root: CTND_USER is baked into
# the unit files as User=. From a plain root shell there is no SUDO_USER to
# recover the intended owner from, so refuse rather than silently install
# User=root units.
CTND_USER="${SUDO_USER:-$USER}"
if [[ "$CTND_USER" == "root" ]]; then
    echo "Error: run this as the station user (sudo is fine: 'sudo bash ./scripts/setup.sh')," >&2
    echo "       not from a root shell — the units would be installed as User=root." >&2
    echo "       If you really mean root, set SUDO_USER to the intended owner." >&2
    exit 1
fi
CTND_HOME=$(getent passwd "$CTND_USER" | cut -d: -f6)

# Guard: must be run from the repository root
if [[ ! -f station/ui/app.py ]]; then
    echo "Error: run this script from the polykybd-ctnd repository root." >&2
    exit 1
fi

if $UNITS_ONLY; then
    INSTALL_DIR=$(pwd)
    echo "=== PolyKybd CTND Setup — units only, in $INSTALL_DIR (user: $CTND_USER) ==="
elif $LOCAL; then
    INSTALL_DIR=$(pwd)
    echo "=== PolyKybd CTND Setup — local install in $INSTALL_DIR (user: $CTND_USER) ==="
else
    INSTALL_DIR=/opt/polykybd-ctnd
    echo "=== PolyKybd CTND Setup — install to $INSTALL_DIR (user: $CTND_USER) ==="
fi

# Detect chromium package and binary name.
# Raspberry Pi OS Bookworm (Debian 12) renamed the package from
# 'chromium-browser' to 'chromium'.
if apt-cache show chromium &>/dev/null 2>&1; then
    CHROMIUM_PKG=chromium
    CHROMIUM_BIN=chromium
else
    CHROMIUM_PKG=chromium-browser
    CHROMIUM_BIN=chromium-browser
fi
echo "Chromium package: $CHROMIUM_PKG"

# ── Service sudoers grants + systemd units ────────────────────────────────────
# Defined as functions because --units-only needs exactly these two steps and
# nothing else (no apt, no venv, no chown) — see the flag comment at the top.

# Grants that let the touch UI recover the rig without SSH. Scoped to specific
# units; deliberately no 'status' (it can open a pager — a known escalation vector).
RUNNER_CTL_BIN=/usr/local/sbin/polykybd-runner-ctl

install_service_sudoers() {
  SYSTEMCTL_BIN=$(command -v systemctl)
  [[ -n "$SYSTEMCTL_BIN" ]] || return 0

  # "Re-register runner" button → start/stop/restart the runner service.
  #
  # SECURITY (HIL-7): granted on the polykybd-runner-ctl wrapper with an EXACT
  # command list, NOT on `systemctl … actions.runner.*`. Sudoers matches
  # arguments as one concatenated string and `*` spans spaces, so the old
  # wildcard rule permitted an argument tail (further units, systemctl options)
  # rather than a single unit — and the UI that triggers it is unauthenticated
  # (HIL-4). Three literal commands admit no arguments at all; the wrapper
  # discovers the unit itself. See scripts/runner-ctl.sh.
  #
  # Installed to /usr/local/sbin (root-owned, not writable by the station user).
  # It must NOT be granted at its repo path: the station user can write to the
  # checkout, and self-update rewrites it unattended, so that would restore the
  # very escalation this closes.
  sudo install -o root -g root -m 0755 scripts/runner-ctl.sh "$RUNNER_CTL_BIN"

  printf '%s ALL=(root) NOPASSWD: %s start, %s stop, %s restart\n' \
    "$CTND_USER" "$RUNNER_CTL_BIN" "$RUNNER_CTL_BIN" "$RUNNER_CTL_BIN" \
    | sudo tee /etc/sudoers.d/polykybd-runner > /dev/null
  sudo chmod 0440 /etc/sudoers.d/polykybd-runner

  # Self-update (see scripts/self-update.sh):
  #  - the updater (running as $CTND_USER) restarts the station after a pull
  #  - the "Update" UI button kicks the oneshot updater unit
  # Both scoped to the two specific units only.
  printf '%s ALL=(root) NOPASSWD: %s restart polykybd-ctnd.service, %s start --no-block polykybd-update.service, %s start polykybd-update.service\n' \
    "$CTND_USER" "$SYSTEMCTL_BIN" "$SYSTEMCTL_BIN" "$SYSTEMCTL_BIN" \
    | sudo tee /etc/sudoers.d/polykybd-update > /dev/null
  sudo chmod 0440 /etc/sudoers.d/polykybd-update
}

# Install every unit, substituting the actual username, home directory, install
# path and chromium binary for the 'pi' placeholders in the templates.
install_systemd_units() {
  sed "s|User=pi|User=$CTND_USER|g; s|/home/pi|$CTND_HOME|g; s|/opt/polykybd-ctnd|$INSTALL_DIR|g" \
    systemd/polykybd-ctnd.service \
    | sudo tee /etc/systemd/system/polykybd-ctnd.service > /dev/null

  sed "s|User=pi|User=$CTND_USER|g; s|/home/pi|$CTND_HOME|g; s|/opt/polykybd-ctnd|$INSTALL_DIR|g; s|chromium-browser|$CHROMIUM_BIN|g" \
    systemd/polykybd-kiosk.service \
    | sudo tee /etc/systemd/system/polykybd-kiosk.service > /dev/null

  # Self-update timer + oneshot (pulls the tracked branch and restarts the station
  # when it gains commits and the rig is idle — see scripts/self-update.sh).
  for unit in polykybd-update.service polykybd-update.timer; do
    sed "s|User=pi|User=$CTND_USER|g; s|/home/pi|$CTND_HOME|g; s|/opt/polykybd-ctnd|$INSTALL_DIR|g" \
      "systemd/$unit" \
      | sudo tee "/etc/systemd/system/$unit" > /dev/null
  done

  sudo systemctl daemon-reload
  sudo systemctl enable polykybd-ctnd.service polykybd-kiosk.service
  sudo systemctl enable --now polykybd-update.timer
}

if $UNITS_ONLY; then
    install_service_sudoers
    install_systemd_units
    echo ""
    echo "Units and sudoers grants installed. Nothing else was touched."
    echo "The self-update timer is enabled; the UI's UPDATE button works now."
    systemctl list-timers polykybd-update.timer --no-pager || true
    exit 0
fi

# System packages
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  uhubctl \
  picotool \
  libhidapi-hidraw0 libhidapi-libusb0 \
  x11-xserver-utils \
  xss-lock \
  "$CHROMIUM_PKG"

# Allow the user to access GPIO and USB without root
sudo usermod -aG gpio,plugdev,video "$CTND_USER"

# udev rules: HID access for the running keyboard (VID 2021 = PolyTasten, the
# PolyKybd Split72/split42), BOOTSEL access so picotool can talk to the RP2040
# without root, and a rule telling UDisks2 / the desktop volume monitor to
# ignore the RP2040 mass-storage volume.
sudo tee /etc/udev/rules.d/99-polykybd.rules > /dev/null <<'EOF'
# PolyKybd running keyboard — HID console + Raw HID (VID 2021 = PolyTasten).
# Must match the keyboard's USB idVendor (see qmk:vendor_id in config.yaml), or
# the hidraw node stays root-only and the station can't open it.
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2021", MODE="0660", GROUP="plugdev"
# RP2040 in BOOTSEL mode — required for picotool
SUBSYSTEM=="usb", ATTRS{idVendor}=="2e8a", ATTRS{idProduct}=="0003", MODE="0660", GROUP="plugdev"
# RP2040 BOOTSEL mass-storage volume — hide it from UDisks2 / the desktop volume
# monitor. We flash over PICOBOOT (picotool), never the mounted volume, so the
# desktop's auto-mount only races picotool's reboot and throws UI errors on the
# kiosk ("Removable Medium Found"; "Object does not exist .../block_devices/sdaN"
# when the device vanishes mid-mount). UDISKS_IGNORE stops automount and popups.
SUBSYSTEM=="block", ENV{ID_FS_LABEL}=="RPI-RP2", ENV{UDISKS_IGNORE}="1"
SUBSYSTEM=="block", ENV{ID_VENDOR_ID}=="2e8a", ENV{ID_MODEL_ID}=="0003", ENV{UDISKS_IGNORE}="1"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=block

# Allow the station user to run uhubctl and picotool without a password.
# Both need /dev/bus/usb/ access that isn't available to a normal user service.
UHUBCTL_BIN=$(command -v uhubctl)
PICOTOOL_BIN=$(command -v picotool)
printf '%s ALL=(ALL) NOPASSWD: %s\n' "$CTND_USER" "$UHUBCTL_BIN" \
  | sudo tee /etc/sudoers.d/polykybd-usb > /dev/null
printf '%s ALL=(ALL) NOPASSWD: %s\n' "$CTND_USER" "$PICOTOOL_BIN" \
  | sudo tee -a /etc/sudoers.d/polykybd-usb > /dev/null
sudo chmod 0440 /etc/sudoers.d/polykybd-usb

# Allow the station user to recover the runner and drive self-update from the
# touch UI without SSH (definitions above; --units-only installs just these).
install_service_sudoers

# Install application
if $LOCAL; then
    # Running in place — repo is already here, nothing to clone
    mkdir -p "$INSTALL_DIR/firmware"
else
    # Clone or update the repo in $INSTALL_DIR so future updates only need:
    #   sudo git -C $INSTALL_DIR pull && sudo systemctl restart polykybd-ctnd
    REPO_URL=$(git remote get-url origin 2>/dev/null || echo "https://github.com/thpoll83/polykybd-ctnd.git")
    if [ -d "$INSTALL_DIR/.git" ]; then
        echo "Updating existing installation in $INSTALL_DIR ..."
        sudo git -C "$INSTALL_DIR" pull
    else
        echo "Cloning $REPO_URL into $INSTALL_DIR ..."
        sudo git clone "$REPO_URL" "$INSTALL_DIR"
    fi
    sudo mkdir -p "$INSTALL_DIR/firmware"
    sudo chown -R "$CTND_USER:$CTND_USER" "$INSTALL_DIR"
fi

python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

# Create local config from the example if it doesn't exist yet
if [ ! -f "$INSTALL_DIR/config/config.yaml" ]; then
    # umask in a subshell so the file is never briefly world-readable, rather
    # than relying on the chmod below to close the window. The freshly-copied
    # file holds no secret (it is the example, whose token is empty), so this is
    # about making "config.yaml is never group/world readable" hold
    # unconditionally instead of depending on that.
    (umask 077; cp "$INSTALL_DIR/config/config.yaml.example" \
                    "$INSTALL_DIR/config/config.yaml")
    echo "Created $INSTALL_DIR/config/config.yaml — edit it to match your hardware."
fi

# SECURITY (HIL-6): config.yaml holds the GitHub PAT used to mint runner
# registration tokens (Administration: read/write), so it must not be
# world/group readable. Applied on every run, not just on creation — an
# existing rig was provisioned before this line and still carries the umask
# default (0644).
sudo chown "$CTND_USER:$CTND_USER" "$INSTALL_DIR/config/config.yaml"
sudo chmod 600 "$INSTALL_DIR/config/config.yaml"

# Install the systemd units (definitions above; --units-only installs just these).
install_systemd_units

echo ""
echo "=== GitHub Actions Runner ==="
echo "1. Go to: https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new"
echo "2. Select: Linux / ARM64"
echo "3. Download, configure, and when prompted for labels enter: polykybd-ctnd"
echo "4. Install as a service: sudo ./svc.sh install && sudo ./svc.sh start"
echo ""
echo "Done. Reboot to start all services."
