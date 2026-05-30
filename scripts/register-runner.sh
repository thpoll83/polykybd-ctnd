#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Re-register the GitHub Actions self-hosted runner after its registration
# has been deleted from GitHub (e.g. after a token expiry or manual removal).
#
# Usage:
#   ./scripts/register-runner.sh                  # mint token from a stored PAT
#   ./scripts/register-runner.sh --pat <PAT>      # mint token from this PAT
#   ./scripts/register-runner.sh --token <TOKEN>  # use a manual registration token
#   ./scripts/register-runner.sh --no-reinstall   # re-config + restart only (kiosk
#                                                 # button path; no svc.sh reinstall)
#
# Registration tokens expire in ~1 hour, so copying one from the GitHub UI for
# every re-register is tedious. Instead, store a long-lived PAT once and this
# script mints a fresh registration token for you via the API. PAT is read from
# (first match wins):  --pat  →  $GITHUB_PAT  →  github.token in config/config.yaml
# The PAT needs repo "Administration: write" (fine-grained) or "repo" scope
# (classic). The same token also powers the RUNNER status badge if you grant it
# "Administration: read" as well.
#
# Manual fallback — get a one-off registration token at:
#   https://github.com/thpoll83/qmk_firmware/settings/actions/runners/new
#   → Linux / ARM64 → copy the --token value from the ./config.sh command shown
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SLUG="thpoll83/qmk_firmware"
REPO_URL="https://github.com/${REPO_SLUG}"

# ── Argument parsing ──────────────────────────────────────────────────────────
TOKEN=""
PAT=""
RUNNER_NAME="RP4-HIL"
RUNNER_DIR=""
NO_REINSTALL=false   # re-config + systemctl restart only; don't touch the unit

while [[ $# -gt 0 ]]; do
    case "$1" in
        --token)        TOKEN="$2";       shift 2 ;;
        --pat)          PAT="$2";         shift 2 ;;
        --name)         RUNNER_NAME="$2"; shift 2 ;;
        --dir)          RUNNER_DIR="$2";  shift 2 ;;
        --no-reinstall) NO_REINSTALL=true; shift ;;
        -h|--help)
            grep '^#' "$0" | grep -v '#!/' | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

# ── Resolve a registration token ──────────────────────────────────────────────
# A registration token (--token) is used as-is. Otherwise mint one from a PAT.

read_cfg_token() {
    local cfg="$SCRIPT_DIR/../config/config.yaml"
    [[ -f "$cfg" ]] || return 0
    local venv_py="$SCRIPT_DIR/../venv/bin/python"
    if [[ -x "$venv_py" ]]; then
        "$venv_py" - "$cfg" <<'PY' 2>/dev/null
import sys, yaml
try:
    c = yaml.safe_load(open(sys.argv[1])) or {}
    print((c.get("github") or {}).get("token") or "")
except Exception:
    pass
PY
    else
        # Best-effort fallback: `token: "ghp_xxx"  # comment`
        grep -E '^[[:space:]]*token:' "$cfg" | head -1 \
            | sed -E 's/^[[:space:]]*token:[[:space:]]*//; s/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//'
    fi
}

mint_token() {  # $1 = PAT → echoes a registration token, or nothing on failure
    local pat="$1"
    local api="https://api.github.com/repos/${REPO_SLUG}/actions/runners/registration-token"
    local resp=""
    if command -v curl >/dev/null 2>&1; then
        resp=$(curl -fsSL -X POST \
            -H "Authorization: Bearer ${pat}" \
            -H "Accept: application/vnd.github+json" \
            -H "X-GitHub-Api-Version: 2022-11-28" \
            "$api" 2>/dev/null) || true
    elif command -v python3 >/dev/null 2>&1; then
        resp=$(python3 - "$pat" "$api" <<'PY' 2>/dev/null
import sys, json, urllib.request
pat, api = sys.argv[1], sys.argv[2]
req = urllib.request.Request(api, method="POST", headers={
    "Authorization": f"Bearer {pat}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "polykybd-ctnd-register/1.0",
})
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        print(r.read().decode())
except Exception:
    pass
PY
        ) || true
    fi
    printf '%s' "$resp" | grep -oE '"token"[[:space:]]*:[[:space:]]*"[^"]+"' | head -1 \
        | sed -E 's/.*"token"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/'
}

if [[ -z "$TOKEN" ]]; then
    [[ -z "$PAT" ]] && PAT="${GITHUB_PAT:-}"
    [[ -z "$PAT" ]] && PAT="$(read_cfg_token)"
    if [[ -z "$PAT" ]]; then
        echo "Error: no registration token and no PAT to mint one." >&2
        echo "" >&2
        echo "Either store a PAT (so this works with no arguments next time):" >&2
        echo "  • set github.token in config/config.yaml, or export GITHUB_PAT=…, or pass --pat" >&2
        echo "    (PAT needs repo 'Administration: write' / classic 'repo' scope)" >&2
        echo "Or pass a one-off registration token with --token:" >&2
        echo "  https://github.com/${REPO_SLUG}/settings/actions/runners/new" >&2
        exit 1
    fi
    echo "Minting a fresh registration token from the PAT …"
    TOKEN="$(mint_token "$PAT")"
    if [[ -z "$TOKEN" ]]; then
        echo "Error: could not mint a registration token." >&2
        echo "Check the PAT has repo 'Administration: write' (fine-grained) or 'repo'" >&2
        echo "scope (classic), and that the Pi can reach api.github.com." >&2
        exit 1
    fi
    echo "Got a registration token (valid ~1 hour)."
fi

# ── Locate runner directory ───────────────────────────────────────────────────
# id -un is the robust fallback when neither SUDO_USER nor USER is exported.
CTND_USER="${SUDO_USER:-${USER:-$(id -un)}}"
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

# run config.sh as the runner user — but skip sudo when we already are that user
# (e.g. invoked from the Flask UI service, which has no password-less sudo -u).
run_as_ctnd() {
    if [[ "$(id -un)" == "$CTND_USER" ]]; then
        "$@"
    else
        sudo -u "$CTND_USER" "$@"
    fi
}

# Discover the installed systemd unit (read-only, no sudo needed).
RUNNER_UNIT=""
if command -v systemctl >/dev/null 2>&1; then
    RUNNER_UNIT=$(systemctl list-units --all --no-legend --plain --type=service \
                  'actions.runner.*' 2>/dev/null | awk 'NR==1{print $1}')
fi

# ── Stop the service ─────────────────────────────────────────────────────────
# --no-reinstall (the kiosk button) only needs systemctl stop/start on the unit,
# which is granted password-less in sudoers. The full path uses svc.sh, which
# also (re)installs the unit and needs broader root — fine over SSH.
if $NO_REINSTALL; then
    if [[ -n "$RUNNER_UNIT" ]]; then
        echo "Stopping $RUNNER_UNIT …"
        sudo systemctl stop "$RUNNER_UNIT" 2>/dev/null || true
    else
        echo "Error: no installed actions.runner unit found." >&2
        echo "Run once over SSH without --no-reinstall to install the service first." >&2
        exit 1
    fi
elif [[ -f ".svc" ]]; then
    echo "Stopping service …"
    sudo ./svc.sh stop      2>/dev/null || true
    echo "Uninstalling service …"
    sudo ./svc.sh uninstall 2>/dev/null || true
fi

# ── Wipe stale runner credentials ────────────────────────────────────────────
# config.sh remove can fail if the server-side registration is already gone.
# Deleting these three files is equivalent to what config.sh remove does locally.
# Also covers the "Not configured" state, where the listener starts but exits
# because .runner is missing while other artefacts may linger.
if [[ -f ".runner" || -f ".credentials" || -f ".credentials_rsaparams" ]]; then
    echo "Removing stale runner credentials …"
    rm -f .runner .credentials .credentials_rsaparams
fi

# ── Re-register ───────────────────────────────────────────────────────────────
# --replace makes this idempotent: if a runner with the same name still exists
# on GitHub (e.g. showing offline), it is replaced instead of erroring out.
echo "Configuring runner …"
run_as_ctnd ./config.sh \
    --url "$REPO_URL" \
    --token "$TOKEN" \
    --name "$RUNNER_NAME" \
    --labels polykybd-ctnd \
    --replace \
    --unattended

# ── (Re)install and start the service ────────────────────────────────────────
if $NO_REINSTALL; then
    echo "Starting $RUNNER_UNIT …"
    sudo systemctl start "$RUNNER_UNIT"
    sleep 2
    if systemctl is-active --quiet "$RUNNER_UNIT"; then
        echo "Done. $RUNNER_UNIT is active (running)."
    else
        echo "Warning: $RUNNER_UNIT did not stay active — check 'journalctl -u $RUNNER_UNIT'." >&2
        exit 1
    fi
else
    echo "Installing service …"
    sudo ./svc.sh install "$CTND_USER" 2>/dev/null || sudo ./svc.sh install
    echo "Starting service …"
    sudo ./svc.sh start
    echo ""
    echo "Done. Checking status:"
    sudo ./svc.sh status
fi
