#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Re-register the GitHub Actions self-hosted runner after its registration
# has been deleted from GitHub (e.g. after a token expiry or manual removal).
#
# Usage:
#   ./scripts/register-runner.sh --token <TOKEN>
#
# Get a fresh token at:
#   https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new
#   → Linux / ARM64 → copy the --token value from the ./config.sh command shown
#
set -euo pipefail

# ── Argument parsing ──────────────────────────────────────────────────────────
TOKEN=""
RUNNER_NAME="RP4-HIL"
RUNNER_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)   TOKEN="$2";       shift 2 ;;
        --name)    RUNNER_NAME="$2"; shift 2 ;;
        --dir)     RUNNER_DIR="$2";  shift 2 ;;
        -h|--help)
            grep '^#' "$0" | grep -v '#!/' | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$TOKEN" ]]; then
    echo "Error: --token is required." >&2
    echo ""
    echo "Get one at: https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new" >&2
    echo "  → Linux / ARM64 → copy the --token value from the ./config.sh command shown." >&2
    exit 1
fi

# ── Locate runner directory ───────────────────────────────────────────────────
CTND_USER="${SUDO_USER:-$USER}"
CTND_HOME=$(getent passwd "$CTND_USER" | cut -d: -f6)

if [[ -z "$RUNNER_DIR" ]]; then
    for candidate in "$CTND_HOME/actions-runner" /opt/actions-runner; do
        if [[ -f "$candidate/config.sh" ]]; then
            RUNNER_DIR="$candidate"
            break
        fi
    done
fi

if [[ -z "$RUNNER_DIR" || ! -f "$RUNNER_DIR/config.sh" ]]; then
    echo "Error: could not find an actions-runner installation." >&2
    echo "Pass --dir <path> to specify its location, or install it first:" >&2
    echo "  https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new" >&2
    exit 1
fi

echo "=== PolyKybd CTND — re-register GitHub Actions runner ==="
echo "Runner dir : $RUNNER_DIR"
echo "Runner name: $RUNNER_NAME"
echo "Labels     : self-hosted, polykybd-ctnd"
echo ""

cd "$RUNNER_DIR"

# ── Stop and uninstall the service ───────────────────────────────────────────
if [[ -f ".svc" ]]; then
    echo "Stopping service …"
    sudo ./svc.sh stop      2>/dev/null || true
    echo "Uninstalling service …"
    sudo ./svc.sh uninstall 2>/dev/null || true
fi

# ── Wipe stale runner credentials ────────────────────────────────────────────
# config.sh remove can fail if the server-side registration is already gone.
# Deleting these three files is equivalent to what config.sh remove does locally.
if [[ -f ".runner" ]]; then
    echo "Removing stale runner credentials …"
    rm -f .runner .credentials .credentials_rsaparams
fi

# ── Re-register ───────────────────────────────────────────────────────────────
echo "Configuring runner …"
sudo -u "$CTND_USER" ./config.sh \
    --url https://github.com/thpoll83/qmk_firmware \
    --token "$TOKEN" \
    --name "$RUNNER_NAME" \
    --labels polykybd-ctnd \
    --unattended

# ── Re-install and start the service ─────────────────────────────────────────
echo "Installing service …"
sudo ./svc.sh install "$CTND_USER" 2>/dev/null || sudo ./svc.sh install
echo "Starting service …"
sudo ./svc.sh start

echo ""
echo "Done. Checking status:"
sudo ./svc.sh status
