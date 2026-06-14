#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# Self-update the deployed station: fetch the tracked branch (default `main`),
# and if the checkout is behind, fast-forward to it, reinstall Python deps when
# requirements.txt changed, and restart the polykybd-ctnd service.
#
# Design notes:
#  - This is the actuator behind both the `polykybd-update.timer` (periodic,
#    unattended) and the "Update now" button in the touch UI (which triggers
#    `polykybd-update.service`). It runs in its OWN systemd unit / cgroup, so the
#    `systemctl restart polykybd-ctnd` at the end does NOT kill this script.
#  - "Defer until idle": before applying, it asks the running station whether it
#    is mid flash/test via GET /status. If busy, it logs and exits 0 WITHOUT
#    pulling — so the apply stays atomic (pull + restart together) and simply
#    retries on the next timer tick once the rig is idle. Never aborts a HIL run.
#  - config.yaml is gitignored, so the fast-forward never clobbers local config.
#  - The whole body is wrapped in a `{ ... }` group with an explicit exit so bash
#    parses the entire file before executing: a fast-forward that rewrites *this*
#    script mid-run can't make bash resume at a shifted file offset.
{
set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
SERVICE=polykybd-ctnd.service
PY="$REPO_DIR/venv/bin/python"

CHECK_ONLY=false
NO_RESTART=false
for arg in "$@"; do
    case "$arg" in
        --check)      CHECK_ONLY=true ;;
        --no-restart) NO_RESTART=true ;;
        *) echo "Unknown argument: $arg" >&2; exit 64 ;;
    esac
done

log() { printf '[self-update] %s\n' "$*"; }

# Read the tracked branch and UI port from config.yaml (single source of truth).
# Falls back to sane defaults if the venv/yaml/keys are missing.
BRANCH=main
PORT=5000
if [[ -x "$PY" ]]; then
    _cfg=$(cd "$REPO_DIR" && PYTHONPATH="$REPO_DIR" "$PY" -c \
        'from station.config import UPDATE_BRANCH, UI_PORT; print(UPDATE_BRANCH or "main"); print(UI_PORT or 5000)' \
        2>/dev/null) || _cfg=""
    if [[ -n "$_cfg" ]]; then
        BRANCH=$(printf '%s\n' "$_cfg" | sed -n 1p)
        PORT=$(printf '%s\n' "$_cfg" | sed -n 2p)
    fi
fi
[[ -n "$BRANCH" ]] || BRANCH=main
[[ -n "$PORT"   ]] || PORT=5000

cd "$REPO_DIR"

# ── Fetch the tracked branch (retry on transient network errors) ──────────────
fetched=false
for attempt in 1 2 3 4; do
    if git fetch --quiet origin "$BRANCH" 2>/dev/null; then
        fetched=true
        break
    fi
    log "git fetch failed (attempt $attempt) — retrying"
    sleep $(( attempt * 2 ))
done
if ! $fetched; then
    log "could not fetch origin/$BRANCH after retries — giving up this cycle"
    exit 0
fi

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse FETCH_HEAD)

if [[ "$LOCAL" == "$REMOTE" ]]; then
    log "up to date with origin/$BRANCH ($(git rev-parse --short HEAD))"
    exit 0
fi

BEHIND=$(git rev-list --count "HEAD..$REMOTE" 2>/dev/null || echo "?")
if [[ "$BEHIND" == "0" ]]; then
    # LOCAL != REMOTE but nothing to pull ⇒ the checkout is ahead of (or has
    # diverged from) origin/$BRANCH. Don't fast-forward/restart in that case.
    log "local checkout is at or ahead of origin/$BRANCH; nothing to apply"
    exit 0
fi
log "behind origin/$BRANCH by $BEHIND commit(s):"
git --no-pager log --oneline --no-decorate "HEAD..$REMOTE" 2>/dev/null | sed 's/^/[self-update]   /' || true

if $CHECK_ONLY; then
    exit 10   # behind — distinct from 0 (up to date) for callers/scripts
fi

# ── Defer until idle: don't reboot the rig mid flash/test ─────────────────────
status=$(curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/status" 2>/dev/null || true)
# crude JSON scrape — avoids a python dependency on this hot path
state=$(printf '%s' "$status" | grep -o '"status"[[:space:]]*:[[:space:]]*"[^"]*"' | head -1 | sed 's/.*"\([^"]*\)"$/\1/')
if [[ -n "$state" && "$state" != "idle" && "$state" != "error" ]]; then
    log "station busy (status='$state') — deferring update until idle (will retry next cycle)"
    exit 0
fi

# ── Apply ─────────────────────────────────────────────────────────────────────
REQ_BEFORE=$(git rev-parse "HEAD:requirements.txt" 2>/dev/null || echo none)

if ! git merge --ff-only "$REMOTE" 2>&1 | sed 's/^/[self-update]   /'; then
    log "fast-forward failed — local checkout has diverged from origin/$BRANCH."
    log "resolve manually on the Pi (git -C $REPO_DIR status). Not touching the service."
    exit 75   # EX_TEMPFAIL — surfaces in journald without crash-looping the unit
fi
log "fast-forwarded to $(git rev-parse --short HEAD)"

REQ_AFTER=$(git rev-parse "HEAD:requirements.txt" 2>/dev/null || echo none)
if [[ "$REQ_BEFORE" != "$REQ_AFTER" && -x "$REPO_DIR/venv/bin/pip" ]]; then
    log "requirements.txt changed — reinstalling Python dependencies"
    "$REPO_DIR/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt" \
        2>&1 | sed 's/^/[self-update]   /' || log "pip install reported a problem (continuing to restart)"
fi

if $NO_RESTART; then
    log "updated; --no-restart given, leaving $SERVICE running the old code"
    exit 0
fi

log "restarting $SERVICE"
if sudo -n systemctl restart "$SERVICE" 2>/dev/null; then
    log "done — $SERVICE restarted on $(git rev-parse --short HEAD)"
else
    log "could not restart $SERVICE (need NOPASSWD sudo for 'systemctl restart $SERVICE')."
    log "code is updated; it will run after the next manual restart/reboot."
    exit 1
fi
exit 0
}
