# SPDX-License-Identifier: GPL-2.0-only
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import urllib.request
import urllib.error
from pathlib import Path

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

from station.config import (
    FIRMWARE_DIR, UI_HOST, UI_PORT, UI_CORS_ORIGINS, GITHUB_REPO, GITHUB_TOKEN,
    RUNNER_LABELS, UPDATE_BRANCH,
)

# Background pollers run unattended; log their failures (to journald via systemd)
# instead of swallowing them silently, so a wedged poller is diagnosable.
# Named _log to avoid colliding with the `log` emit-callback param used widely below.
_log = logging.getLogger("polykybd-ctnd")

app = Flask(__name__)
app.config["SECRET_KEY"] = "polykybd-ctnd"
socketio = SocketIO(app, cors_allowed_origins=UI_CORS_ORIGINS, async_mode="threading")

# Labels the HIL job requires (case-insensitive). GitHub adds `self-hosted`,
# `Linux`, `ARM64` automatically; `polykybd-ctnd` comes from the runner config.
REQUIRED_LABELS = {label.lower() for label in RUNNER_LABELS}

# This repo's root — the install can live anywhere (setup.sh --local), so derive
# it from __file__ rather than hardcoding /opt/polykybd-ctnd in user-facing hints.
REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTER_SCRIPT = REPO_ROOT / "scripts" / "register-runner.sh"
UPDATE_SCRIPT   = REPO_ROOT / "scripts" / "self-update.sh"
UPDATE_SERVICE  = "polykybd-update.service"   # oneshot unit that runs self-update.sh
CTND_SERVICE    = "polykybd-ctnd.service"     # this very service (restarted to apply)

_status         = {"value": "idle"}
_ci_state       = {"running": False, "url": None}
_runner_state   = {"status": "unknown"}            # unknown|online|busy|offline|missing|noauth
_update_state   = {"state": "unknown", "behind": None, "branch": UPDATE_BRANCH}  # unknown|current|behind|updating
_usb_state      = {"left": None,  "right": None}   # None = unknown
_bootsel_state  = {"left": False, "right": False}   # False = released (HIGH)
_run_state      = {"left": False, "right": False}   # False = released (HIGH)


def emit_log(msg: str) -> None:
    socketio.emit("log", {"msg": msg})


def set_status(s: str) -> None:
    _status["value"] = s
    socketio.emit("status", {"value": s})


# ── GitHub API helper ─────────────────────────────────────────────────────────

def _gh_api(path: str, timeout: int = 10):
    """GET https://api.github.com{path}. Returns (status_code|None, parsed_json|None).

    Never raises: on a transport error returns (None, None); on an HTTP error
    returns (code, None) so callers can distinguish 401/403/404 from a dead network.
    """
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={
            "User-Agent": "polykybd-ctnd/1.0",
            "Accept": "application/vnd.github+json",
        },
    )
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return None, None


# ── CI status poller ──────────────────────────────────────────────────────────

def _ci_poll_once():
    _, data = _gh_api(f"/repos/{GITHUB_REPO}/actions/runs?status=in_progress&per_page=1")
    if data is None:
        return
    running = data.get("total_count", 0) > 0
    run_url = data["workflow_runs"][0]["html_url"] if running and data.get("workflow_runs") else None
    _ci_state.update({"running": running, "url": run_url})
    socketio.emit("ci_status", dict(_ci_state))


def _ci_poll_loop():
    import time
    while True:
        try:
            _ci_poll_once()
        except Exception:
            _log.warning("CI poll failed", exc_info=True)
        time.sleep(60)


# ── Self-hosted runner status poller ──────────────────────────────────────────

def _runner_status() -> str:
    """Summarise self-hosted runner availability for the header badge.

    Returns one of: online (a matching runner is online & idle), busy (matching
    runner online but running a job), offline (matching runner registered but
    offline), missing (no runner advertises all REQUIRED_LABELS), noauth (token
    can't read runner status), unknown (couldn't reach the API).
    """
    code, data = _gh_api(f"/repos/{GITHUB_REPO}/actions/runners")
    if code in (401, 403, 404):
        return "noauth"
    if data is None:
        return "unknown"
    matching = [
        r for r in data.get("runners", [])
        if REQUIRED_LABELS <= {l["name"].lower() for l in r.get("labels", [])}
    ]
    if not matching:
        return "missing"
    if any(r.get("status") == "online" and not r.get("busy") for r in matching):
        return "online"
    if any(r.get("status") == "online" and r.get("busy") for r in matching):
        return "busy"
    return "offline"


def _runner_poll_once():
    _runner_state["status"] = _runner_status()
    socketio.emit("runner_status", dict(_runner_state))


def _runner_poll_loop():
    import time
    while True:
        try:
            _runner_poll_once()
        except Exception:
            _log.warning("runner poll failed", exc_info=True)
        time.sleep(30)


if GITHUB_REPO:
    threading.Thread(target=_ci_poll_loop, daemon=True).start()
    threading.Thread(target=_runner_poll_loop, daemon=True).start()


# ── Self-update status poller ───────────────────────────────────────────────
# Shows an UPDATE header badge: current / N-behind / updating. The badge is
# informational + a manual trigger; the *automatic* apply is done out-of-process
# by polykybd-update.timer → self-update.sh (so it survives the app restart and
# defers while the rig is busy). This poller just fetches the tracked branch and
# counts how far behind the checkout is.

def _git(*args, timeout=20):
    """Run git in the repo root. Returns stdout (stripped) or None on any error."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _update_status() -> dict:
    """Fetch the tracked branch and report how far HEAD is behind it.

    Returns {state, behind, branch}: 'current' (0 behind), 'behind' (N>0),
    or 'unknown' (not a git checkout / fetch failed / detached) — mirroring the
    other badges' graceful-degradation style.
    """
    # Fetch is best-effort; if it fails we still report against the last-known ref.
    _git("fetch", "--quiet", "origin", UPDATE_BRANCH)
    behind = _git("rev-list", "--count", f"HEAD..origin/{UPDATE_BRANCH}")
    if behind is None or not behind.isdigit():
        return {"state": "unknown", "behind": None, "branch": UPDATE_BRANCH}
    n = int(behind)
    return {"state": "current" if n == 0 else "behind", "behind": n, "branch": UPDATE_BRANCH}


def _update_poll_once():
    # Don't clobber a transient 'updating' state we set when the button is pressed.
    if _update_state.get("state") == "updating":
        return
    _update_state.update(_update_status())
    socketio.emit("update_status", dict(_update_state))


def _update_poll_loop():
    import time
    while True:
        try:
            _update_poll_once()
        except Exception:
            _log.warning("update poll failed", exc_info=True)
        time.sleep(120)


# Only run the poller on a real git checkout (skip e.g. a tarball install).
if (REPO_ROOT / ".git").exists():
    threading.Thread(target=_update_poll_loop, daemon=True).start()


# ── On-demand runner diagnostics ──────────────────────────────────────────────
# Streams a full troubleshooting report to the log panel: local runner service /
# process, GitHub-side registration + labels, queued jobs (and the labels they
# request), connectivity, then a plain-language verdict. Triggered by the RUNNER
# badge or the "Diagnose Runner" button.

def _runner_dir(unit=None):
    """Best-effort location of the actions-runner install (for the .runner check).
    Prefers the systemd unit's WorkingDirectory (most reliable, no sudo); falls
    back to common paths. Returns a Path or None."""
    candidates = []
    env_dir = os.environ.get("CTND_RUNNER_DIR")
    if env_dir:
        candidates.append(Path(env_dir))
    if unit and shutil.which("systemctl"):
        try:
            wd = subprocess.run(
                ["systemctl", "show", "-p", "WorkingDirectory", "--value", unit],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if wd:
                candidates.append(Path(wd))
        except Exception:
            pass
    home = Path.home()
    candidates += [home / "actions-runner", home / "polykybd-ctnd" / "actions-runner",
                   REPO_ROOT / "actions-runner",
                   Path("/opt/actions-runner"), Path("/opt/polykybd-ctnd/actions-runner"),
                   Path("/home/pi/actions-runner")]
    for c in candidates:
        try:
            if (c / "config.sh").exists():
                return c
        except Exception:
            pass
    return None


def _diag_local_runner(log) -> dict:
    """Report the local runner systemd service + process. Returns
    {"systemctl": bool, "unit": str|None, "active": bool, "process_running": bool,
     "configured": bool|None, "runner_dir": str|None, "active_secs": float|None}
    so the verdict can tell "just stopped" (start it) from "Not configured" (must
    re-register), quote the real install path, and avoid crying "offline!" about a
    runner that only started seconds ago. configured=None means unknown."""
    info = {"systemctl": bool(shutil.which("systemctl")),
            "unit": None, "active": False, "process_running": False,
            "configured": None, "runner_dir": None, "active_secs": None}
    if info["systemctl"]:
        try:
            out = subprocess.run(
                ["systemctl", "list-units", "--all", "--no-legend", "--plain",
                 "--type=service", "actions.runner.*"],
                capture_output=True, text=True, timeout=10).stdout
            units = [ln.split() for ln in out.splitlines() if ln.strip()]
            if units:
                for parts in units:
                    name   = parts[0]
                    active = parts[2] if len(parts) > 2 else "?"
                    sub    = parts[3] if len(parts) > 3 else "?"
                    mark   = "✓" if sub == "running" else "✗"
                    log(f"    [{mark}] {name}  {active}/{sub}")
                    info["unit"] = info["unit"] or name
                    if sub == "running":
                        info["active"] = True
            else:
                log("    [✗] no actions.runner.*.service installed (run svc.sh install)")
        except Exception as exc:
            log(f"    [?] systemctl: {exc}")

    # How long has the unit been active? A runner that just started hasn't had
    # time to connect to the Actions broker, so GitHub legitimately still shows
    # it offline for ~15-60s — don't misdiagnose that as a network block.
    if info["active"] and info["unit"]:
        try:
            ts = subprocess.run(
                ["systemctl", "show", "-p", "ActiveEnterTimestampMonotonic",
                 "--value", info["unit"]],
                capture_output=True, text=True, timeout=5).stdout.strip()
            mono = int(ts) / 1_000_000  # microseconds → seconds since boot
            with open("/proc/uptime") as f:
                uptime = float(f.read().split()[0])
            if mono > 0:
                info["active_secs"] = max(0.0, uptime - mono)
        except Exception:
            pass

    rdir = _runner_dir(info["unit"])
    if rdir is not None:
        info["runner_dir"] = str(rdir)
        info["configured"] = (rdir / ".runner").exists()
        if info["configured"]:
            log(f"    [✓] runner configured ({rdir}/.runner present)")
        else:
            log(f"    [✗] runner NOT configured — {rdir}/.runner missing "
                f"(→ Re-register, not Restart)")

    try:
        proc = subprocess.run(["pgrep", "-af", "Runner.Listener"],
                              capture_output=True, text=True, timeout=5)
        if proc.returncode == 0 and proc.stdout.strip():
            log(f"    [✓] Runner.Listener running (pid {proc.stdout.split()[0]})")
            info["process_running"] = True
        else:
            log("    [✗] Runner.Listener process not running")
    except Exception as exc:
        log(f"    [?] pgrep: {exc}")
    return info


def _diag_github_runners(log) -> dict:
    """Report GitHub-side runner registration + labels. Returns
    {"runners": <raw list or None>, "have_match": bool}; runners=None means the
    query failed (auth/network)."""
    code, data = _gh_api(f"/repos/{GITHUB_REPO}/actions/runners")
    if code == 401:
        log("    [✗] 401 Unauthorized — github.token missing or invalid")
        return {"runners": None, "have_match": False}
    if code == 403:
        log("    [✗] 403 Forbidden — token needs repo-admin (administration:read) scope")
        return {"runners": None, "have_match": False}
    if code == 404:
        log("    [✗] 404 — repo not found, or token lacks Administration access")
        return {"runners": None, "have_match": False}
    if data is None:
        log(f"    [?] could not query runners (status {code})")
        return {"runners": None, "have_match": False}

    runners = data.get("runners", [])
    if not runners:
        log("    [✗] NO self-hosted runners registered on this repo")
        log("        → on the Pi: scripts/register-runner.sh --token <TOKEN>")
        return {"runners": [], "have_match": False}

    have_match = False
    for r in runners:
        labels  = {l["name"].lower() for l in r.get("labels", [])}
        online  = r.get("status") == "online"
        busy    = bool(r.get("busy"))
        missing = REQUIRED_LABELS - labels
        state   = ("online·busy" if busy else "online") if online else "OFFLINE"
        mark    = "✓" if (online and not missing) else "✗"
        names   = ", ".join(sorted(l["name"] for l in r.get("labels", [])))
        log(f"    [{mark}] {r.get('name')}  {state}")
        log(f"        labels: {names}")
        if missing:
            log(f"        ✗ missing required label(s): {', '.join(sorted(missing))}")
        if online and not missing:
            have_match = True
    return {"runners": runners, "have_match": have_match}


def _diag_queued(log) -> bool:
    """List queued workflow runs and the labels each waiting job requests.
    Returns True if anything is queued, so the verdict can stay quiet when the
    queue is empty (a healthy, idle station)."""
    code, data = _gh_api(f"/repos/{GITHUB_REPO}/actions/runs?status=queued&per_page=5")
    if data is None:
        log(f"    [?] could not query queued runs (status {code})")
        return False
    runs = data.get("workflow_runs", [])
    if not runs:
        log("    none queued")
        return False
    for run in runs:
        log(f"    #{run.get('run_number')} '{run.get('name')}'  "
            f"{run.get('event')} on {run.get('head_branch')}")
        _, jdata = _gh_api(f"/repos/{GITHUB_REPO}/actions/runs/{run.get('id')}/jobs")
        for job in (jdata or {}).get("jobs", []):
            if job.get("status") in ("queued", "waiting"):
                labels = job.get("labels", [])
                log(f"        ⏳ job '{job.get('name')}' wants [{', '.join(labels)}]")
    return True


def _diag_network(log) -> None:
    code, _ = _gh_api("/rate_limit", timeout=8)
    if code == 200:
        log("    [✓] api.github.com reachable, token valid")
    elif code == 401:
        log("    [✗] api.github.com reachable but token INVALID (401)")
    elif code is None:
        log("    [✗] cannot reach api.github.com (network / DNS / proxy?)")
    else:
        log(f"    [?] api.github.com returned {code}")


def _diag_verdict(facts) -> list:
    """Turn the collected facts into a short, plain-language likely-cause list."""
    out = []
    local = facts.get("local") or {}

    ssh_hint = f"cd {REPO_ROOT} && ./scripts/register-runner.sh"

    # Local runner state is the most common and most actionable cause — lead with it.
    if local.get("unit") and not local.get("active") and local.get("configured") is False:
        # Service installed but no .runner identity → start/restart can't help.
        out.append("Root cause (local): the runner is installed but NOT configured —")
        out.append("it has no registration, so starting the service just exits with")
        out.append("'Not configured'. You must RE-REGISTER (not Restart):")
        out.append("  • touchscreen: Runner ↻ Re-register, or")
        out.append(f"  • SSH:  {ssh_hint}")
        out.append("    (needs a PAT in config.yaml, or pass --token <TOKEN>)")
    elif local.get("unit") and not local.get("active"):
        out.append("Root cause (local): the runner service is installed but NOT running —")
        out.append("nothing on this Pi will ever pick up the job. Start it:")
        out.append(f"  sudo systemctl start {local['unit']}")
        out.append("  (or: touchscreen Runner ⟳ Restart)")
        out.append(f"  why it stopped:  sudo journalctl -u {local['unit']} -n 50 --no-pager")
    elif local.get("systemctl") and not local.get("unit") and not local.get("process_running"):
        out.append("Root cause (local): no runner service is installed on this Pi.")
        out.append("  install/register:  scripts/register-runner.sh --token <TOKEN>")

    if not facts["repo"]:
        out.append("github.repo not set in config.yaml — GitHub-side checks skipped.")
    if not facts["token"]:
        out.append("github.token not set — runner status needs a PAT with repo-admin")
        out.append("(administration:read) scope.")

    gh = facts.get("gh") or {}
    runners = gh.get("runners")
    if runners is None:
        if local.get("process_running"):
            out.append("Runner.Listener is up locally but GitHub status couldn't be read.")
            out.append("Fix the token scope above to confirm it's registered and online.")
        return out
    if not runners:
        out.append("Root cause: no runner registered. On the Pi run:")
        out.append("  scripts/register-runner.sh --token <TOKEN>")
        return out

    def has_labels(r):
        return REQUIRED_LABELS <= {l["name"].lower() for l in r.get("labels", [])}

    if gh.get("have_match"):
        # facts["queued"] is True/False from _diag_queued; None if not checked.
        nothing_queued = facts.get("queued") is False
        if all((not has_labels(r)) or r.get("status") != "online" or r.get("busy")
               for r in runners):
            out.append("A matching runner exists but is BUSY — it will pick up the job")
            out.append("as soon as the current one finishes.")
        elif nothing_queued:
            out.append("All good: a matching runner is online & idle and nothing is")
            out.append("queued. The station is ready to pick up the next HIL job.")
        else:
            out.append("A matching online runner exists, so if the job still waits it is")
            out.append("most likely one of:")
            out.append(" • PR is from a fork — Actions needs manual approval; open the")
            out.append("   run on GitHub and click 'Approve and run'.")
            out.append(" • a label typo: compare the job's requested labels above with")
            out.append("   the runner's labels.")
        return out

    if all(not has_labels(r) for r in runners):
        out.append("Root cause: LABEL MISMATCH. The job needs "
                   f"[{', '.join(sorted(REQUIRED_LABELS))}]")
        out.append("but no registered runner advertises all of them. Add the label in")
        out.append("GitHub (Settings → Actions → Runners → ⚙) or re-run")
        out.append("scripts/register-runner.sh (it sets --labels polykybd-ctnd).")
    else:
        rdir = local.get("runner_dir") or "~/actions-runner"
        out.append("Root cause: the matching runner is registered but OFFLINE.")
        if local.get("configured") is False:
            # GitHub still lists a stale registration, but the Pi lost its
            # credentials → start/restart can't reconnect it. Must re-register.
            out.append("GitHub still lists this runner, but the Pi has no local")
            out.append("registration (.runner missing) — a stale split-brain. Starting")
            out.append("the service won't reconnect it. RE-REGISTER to replace it:")
            out.append("  • touchscreen: Runner ↻ Re-register, or")
            out.append(f"  • SSH:  {ssh_hint}")
        elif local.get("process_running"):
            secs = local.get("active_secs")
            if secs is not None and secs < 60:
                out.append(f"The runner only started {int(secs)}s ago and is still")
                out.append("connecting to GitHub's Actions broker — this is normal. Wait")
                out.append("~30s and re-run Diagnose; it should flip to online (queued")
                out.append("jobs are then picked up automatically).")
            else:
                out.append("It runs locally but GitHub sees it offline → outbound HTTPS to")
                out.append("*.actions.githubusercontent.com may be blocked, or it is a duplicate")
                out.append(f"registration. Check {rdir}/_diag/ logs.")
        else:
            out.append("Start it on the Pi (touchscreen Runner ⟳ Restart, or")
            out.append(f"cd {rdir} && sudo ./svc.sh start). If it then exits with")
            out.append("'Not configured', RE-REGISTER instead (↻ Re-register).")
    return out


def _run_diagnostics():
    log = emit_log
    log("")
    log("════════ RUNNER DIAGNOSTICS ════════")
    log(f"• required labels : {', '.join(sorted(REQUIRED_LABELS))}")
    log(f"• target repo     : {GITHUB_REPO or '(not set)'}")

    facts = {"repo": bool(GITHUB_REPO), "token": bool(GITHUB_TOKEN)}

    log("• local runner")
    facts["local"] = _diag_local_runner(log)

    if GITHUB_REPO:
        log("• github registration")
        facts["gh"] = _diag_github_runners(log)
        log("• queued jobs")
        facts["queued"] = _diag_queued(log)
        log("• connectivity")
        _diag_network(log)

    log("• verdict")
    verdict = _diag_verdict(facts)
    if not verdict:
        log("    no obvious problem detected from the station's side.")
    for line in verdict:
        log(f"    {line}")
    log("═════════════════════════")
    log("")

    try:
        _runner_poll_once()
    except Exception:
        pass


def _query_usb_state_at_startup():
    try:
        from station.flash import FlashController
        fc = FlashController()
        try:
            for side in ("left", "right"):
                state = fc.query_usb_state(side)
                if state is not None:
                    _usb_state[side] = state
        finally:
            fc.cleanup()
        socketio.emit("usb_state", dict(_usb_state))
    except Exception:
        # Expected on a dev machine without GPIO / uhubctl; debug-level only.
        _log.debug("startup USB-state query unavailable", exc_info=True)


threading.Thread(target=_query_usb_state_at_startup, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def status():
    """Current station status as JSON. Polled by scripts/self-update.sh so the
    updater can defer a pull+restart while the rig is mid flash/test (any value
    other than 'idle'/'error' means busy)."""
    return jsonify({"status": _status["value"]})


@app.route("/ci-jobs")
def list_ci_jobs():
    if not GITHUB_REPO:
        return jsonify([])
    jobs = []
    for status in ("queued", "in_progress"):
        code, data = _gh_api(f"/repos/{GITHUB_REPO}/actions/runs?status={status}&per_page=10")
        if data is None:
            return jsonify({"error": "GitHub API failure", "status": code}), 502
        for run in data.get("workflow_runs", []):
            jobs.append({
                "id":          run.get("id"),
                "name":        run.get("name"),
                "run_number":  run.get("run_number"),
                "status":      run.get("status"),
                "head_branch": run.get("head_branch"),
                "event":       run.get("event"),
                "html_url":    run.get("html_url"),
                "created_at":  run.get("created_at"),
            })
    return jsonify(jobs)


@app.route("/firmware")
def list_firmware():
    base = Path(FIRMWARE_DIR)
    if not base.exists():
        return jsonify([])
    files = sorted(p.name for p in base.iterdir() if p.suffix.lower() in (".uf2", ".bin"))
    return jsonify(files)


# ── SocketIO events ───────────────────────────────────────────────────────────

@socketio.on("connect")
def on_connect(_auth=None):
    socketio.emit("status",        {"value": _status["value"]})
    socketio.emit("usb_state",     dict(_usb_state))
    socketio.emit("bootsel_state", dict(_bootsel_state))
    socketio.emit("run_state",     dict(_run_state))
    socketio.emit("runner_status", dict(_runner_state))
    socketio.emit("update_status", dict(_update_state))
    if GITHUB_REPO:
        socketio.emit("ci_status", dict(_ci_state))


@socketio.on("run_diagnostics")
def on_run_diagnostics(_data=None):
    threading.Thread(target=_run_diagnostics, daemon=True).start()


def _run_runner_script(flag: str, banner: str, ok_msg: str, fail_msg: str):
    """Stream `register-runner.sh <flag>` output to the log panel and reflect the
    result in the status dot + RUNNER badge. Runs on a background thread."""
    if _status["value"] == "registering":
        return
    set_status("registering")

    def _do():
        script = str(REGISTER_SCRIPT)
        emit_log("")
        emit_log(f"════════ {banner} ════════")
        rc = None
        try:
            proc = subprocess.Popen(
                ["bash", script, flag],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in proc.stdout:
                emit_log(line.rstrip())
            rc = proc.wait()
        except Exception as exc:
            emit_log(f"[ui] {fail_msg}: {exc}")
            set_status("error")
        else:
            if rc == 0:
                emit_log(f"[ui] {ok_msg}")
                set_status("idle")
            else:
                emit_log(f"[ui] {fail_msg} (exit {rc})")
                set_status("error")
        finally:
            emit_log("════════════════════════════════════")
            try:
                _runner_poll_once()
            except Exception:
                pass

    threading.Thread(target=_do, daemon=True).start()


@socketio.on("reregister_runner")
def on_reregister_runner(_data=None):
    _run_runner_script("--no-reinstall", "RE-REGISTER RUNNER",
                       "runner re-registered ✓", "re-register failed")


@socketio.on("restart_runner")
def on_restart_runner(_data=None):
    _run_runner_script("--restart-only", "RESTART RUNNER",
                       "runner restarted ✓", "restart failed")


def _diagnose_unit_start_failure(stderr: str) -> list[str]:
    """Explain why `systemctl start polykybd-update.service` was refused.

    The two causes need opposite fixes, so guessing one is worse than saying
    nothing: a MISSING UNIT means the rig was provisioned before the self-update
    units existed (re-run setup.sh — and note the *timer* is missing too, so the
    rig has not been auto-updating at all), while a sudo refusal means the unit
    is there but /etc/sudoers.d/polykybd-update isn't.
    """
    s = (stderr or "").lower()
    if "not found" in s or "no such file" in s:
        return [
            f"[ui] {UPDATE_SERVICE} is not installed on this rig — it was provisioned",
            "[ui] before the self-update units landed. polykybd-update.timer is missing",
            "[ui] too, so unattended updates have never been running here.",
            f"[ui] fix over SSH: cd {REPO_ROOT} && ./scripts/setup.sh --units-only",
        ]
    if "password" in s or "not allowed" in s or "sudo" in s:
        return [
            "[ui] sudo refused — the NOPASSWD grant is missing.",
            f"[ui] fix over SSH: cd {REPO_ROOT} && ./scripts/setup.sh --units-only",
        ]
    return [f"[ui] fix over SSH: cd {REPO_ROOT} && ./scripts/setup.sh --units-only"]


def _update_in_process() -> None:
    """Apply the update from THIS process when the oneshot unit can't be started.

    Runs the same actuator with ``--no-restart`` so the pull finishes while we are
    still alive, then restarts the station separately. The split is the whole
    point: letting the script issue its own restart would tear down our cgroup —
    which is fine *after* the fast-forward, but would kill it mid-pull otherwise.
    Losing this process to the restart is the expected end state, so everything
    worth reading is logged before it is issued.
    """
    if not UPDATE_SCRIPT.exists():
        emit_log(f"[ui] {UPDATE_SCRIPT} is missing — cannot update from the UI.")
        return

    emit_log("[ui] falling back to running scripts/self-update.sh directly.")
    try:
        proc = subprocess.Popen(
            ["bash", str(UPDATE_SCRIPT), "--no-restart"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        for line in proc.stdout:
            emit_log(line.rstrip())
        rc = proc.wait()
    except Exception as exc:
        emit_log(f"[ui] self-update.sh failed: {exc}")
        return
    if rc != 0:
        emit_log(f"[ui] self-update.sh exited {rc} — not restarting the station.")
        return

    emit_log(f"[ui] restarting {CTND_SERVICE} to apply — the UI will reconnect.")
    try:
        res = subprocess.run(["sudo", "-n", "systemctl", "restart", CTND_SERVICE],
                             capture_output=True, text=True, timeout=30)
    except Exception as exc:
        res = None
        emit_log(f"[ui] restart failed: {exc}")
    if res is not None and res.returncode != 0:
        emit_log(f"[ui] could not restart: {res.stderr.strip() or res.returncode}")
        emit_log("[ui] the code IS updated — it runs after the next restart or reboot:")
        emit_log(f"[ui]   sudo systemctl restart {CTND_SERVICE}")


@socketio.on("update_now")
def on_update_now(_data=None):
    """Manual "Update" button: check the tracked branch and, if behind, kick the
    out-of-process updater (polykybd-update.service → self-update.sh). The updater
    runs in its own cgroup, so it survives the service restart it performs at the
    end; this handler returns immediately. Progress lands in journald; the browser
    reconnects after the restart and the UPDATE badge re-polls to 'current'."""

    def _do():
        emit_log("")
        emit_log("════════ UPDATE STATION ════════")
        emit_log(f"[ui] checking origin/{UPDATE_BRANCH}…")
        st = _update_status()
        _update_state.update(st)
        behind = st.get("behind")
        if st["state"] == "unknown":
            emit_log("[ui] could not determine update status (not a git checkout, or fetch failed).")
            socketio.emit("update_status", dict(_update_state))
            emit_log("════════════════════════════════════")
            return
        if not behind:
            emit_log(f"[ui] already up to date with origin/{UPDATE_BRANCH}.")
            socketio.emit("update_status", dict(_update_state))
            emit_log("════════════════════════════════════")
            return

        for line in (_git("--no-pager", "log", "--oneline", "--no-decorate",
                          f"HEAD..origin/{UPDATE_BRANCH}") or "").splitlines():
            emit_log(f"    {line}")
        emit_log(f"[ui] {behind} commit(s) behind — pulling and restarting the station.")
        emit_log("[ui] the UI will drop and reconnect automatically.")
        _update_state["state"] = "updating"
        socketio.emit("update_status", dict(_update_state))

        # Fire the oneshot updater in its own unit/cgroup (--no-block: don't wait;
        # it will restart this very service). Needs the NOPASSWD sudoers grant that
        # setup.sh installs for `systemctl start polykybd-update.service`.
        try:
            rc = subprocess.run(
                ["sudo", "-n", "systemctl", "start", "--no-block", UPDATE_SERVICE],
                capture_output=True, text=True, timeout=15,
            )
            if rc.returncode != 0:
                emit_log(f"[ui] could not start {UPDATE_SERVICE}: {rc.stderr.strip() or rc.returncode}")
                for line in _diagnose_unit_start_failure(rc.stderr):
                    emit_log(line)
                # The unit is only the preferred *carrier*; the actuator is a plain
                # script this process can run itself. So a missing unit / missing
                # sudoers grant degrades the button to "slower and it takes the UI
                # down with it", not to "does nothing".
                _update_in_process()
                _update_poll_once()  # revert the badge from 'updating'
        except Exception as exc:
            emit_log(f"[ui] update trigger failed: {exc}")
            _update_poll_once()

    threading.Thread(target=_do, daemon=True).start()


def _selected_firmware(name) -> str | None:
    """Resolve a UI-selected firmware filename to a real path inside FIRMWARE_DIR.

    The name arrives in a Socket.IO payload, so it is untrusted input. Joining it
    onto FIRMWARE_DIR directly would let an absolute path or a ``..`` component
    escape the directory and have the flasher copy some *other* local file onto
    the keyboard's bootloader drive. So accept a bare filename only, and require
    it to resolve to an existing file sitting directly in FIRMWARE_DIR.

    Returns the absolute path, or None if the selection is not acceptable.
    """
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    root = Path(FIRMWARE_DIR).resolve()
    path = (root / name).resolve()
    if path.parent != root or not path.is_file():
        return None
    return str(path)


# The rig drives exactly one keyboard pair, so the long-running device-owning
# operations — a manual flash, a HIL run, a perf run — must not overlap. Two of
# them at once would flash, reset and read HID on the same hardware
# simultaneously and interleave their GPIO cleanup. `set_status()` alone does not
# prevent that: nothing reads it as a gate. This single process-wide lock does.
_RIG_LOCK = threading.Lock()


def _run_exclusive(what: str, busy_status: str, body) -> bool:
    """Run ``body()`` on a worker thread while holding the rig lock.

    A second request while one is running is **rejected with a log line, not
    queued** — on a touch UI a queued run would fire minutes later with the
    operator long gone, and a double-tap would silently start two. The lock is
    released in a ``finally`` inside the worker, so every path frees it,
    exceptions included."""
    if not _RIG_LOCK.acquire(blocking=False):
        emit_log(f"[ui] rig busy — {what} ignored "
                 "(a flash / test / perf run is already active)")
        return False
    set_status(busy_status)

    def _wrapped():
        try:
            body()
        finally:
            _RIG_LOCK.release()

    threading.Thread(target=_wrapped, daemon=True).start()
    return True


@socketio.on("flash")
def on_flash(data):
    if not isinstance(data, dict):
        return
    side = data.get("side")
    if side not in ("left", "right"):
        return
    uf2_path = _selected_firmware(data.get("uf2"))
    if not uf2_path:
        emit_log(f"[ui] firmware not found in {FIRMWARE_DIR}: {data.get('uf2')!r}")
        return

    def _do():
        from station.flash import FlashController
        fc = FlashController()
        try:
            fc.flash(side, uf2_path, log=emit_log)
        except Exception as exc:
            emit_log(f"[ui] flash error: {exc}")
            set_status("error")
        else:
            set_status("idle")
        finally:
            fc.cleanup()

    _run_exclusive(f"flash-{side}", f"flashing-{side}", _do)


@socketio.on("usb_power")
def on_usb_power(data):
    side = data.get("side")
    on   = data.get("on")
    if side not in ("left", "right") or not isinstance(on, bool):
        return

    def _do():
        from station.flash import FlashController
        fc = FlashController()
        try:
            fc.usb_power(side, on, log=emit_log)
            _usb_state[side] = on
            socketio.emit("usb_state", dict(_usb_state))
        except Exception as exc:
            emit_log(f"[ui] USB error: {exc}")
        finally:
            fc.cleanup()

    threading.Thread(target=_do, daemon=True).start()


@socketio.on("bootsel")
def on_bootsel(data):
    side     = data.get("side")
    asserted = data.get("asserted")
    if side not in ("left", "right") or not isinstance(asserted, bool):
        return

    def _do():
        from station.flash import FlashController
        fc = FlashController()
        try:
            fc.set_bootsel(side, asserted, log=emit_log)
            _bootsel_state[side] = asserted
            socketio.emit("bootsel_state", dict(_bootsel_state))
        except Exception as exc:
            emit_log(f"[ui] BOOTSEL error: {exc}")
        finally:
            fc.cleanup()

    threading.Thread(target=_do, daemon=True).start()


@socketio.on("reset_board")
def on_reset_board(data):
    side     = data.get("side")
    asserted = data.get("asserted")
    if side not in ("left", "right") or not isinstance(asserted, bool):
        return

    def _do():
        from station.flash import FlashController
        fc = FlashController()
        try:
            fc.set_run(side, asserted, log=emit_log)
            _run_state[side] = asserted
            socketio.emit("run_state", dict(_run_state))
        except Exception as exc:
            emit_log(f"[ui] reset error: {exc}")
        finally:
            fc.cleanup()

    threading.Thread(target=_do, daemon=True).start()


@socketio.on("set_handedness")
def on_set_handedness(data):
    """Set the keyboard's EE_HANDS marker (cmd 25) so a half shows the correct
    side. master_is_left=True ⇒ the USB/master half is the left half. Sent to
    the master half, which syncs the opposite to the slave; both then reboot."""
    master_is_left = data.get("master_is_left")
    if not isinstance(master_is_left, bool):
        return

    def _do():
        from station.set_handedness import set_handedness
        try:
            set_handedness(master_is_left, log=emit_log)
        except Exception as exc:
            emit_log(f"[ui] set-handedness error: {exc}")

    threading.Thread(target=_do, daemon=True).start()


@socketio.on("run_tests")
def on_run_tests(data):
    if not isinstance(data, dict):
        return
    left_path  = _selected_firmware(data.get("left_uf2"))
    right_path = _selected_firmware(data.get("right_uf2"))
    if not left_path or not right_path:
        emit_log("[ui] select a valid left and right firmware file first")
        return

    def _do():
        from station.test_runner import TestRunner
        runner = TestRunner(log=emit_log)
        try:
            result = runner.flash_and_test(left_path, right_path)
            socketio.emit("test_result", result)
            set_status("idle")
        except Exception as exc:
            emit_log(f"[ui] test error: {exc}")
            set_status("error")
        finally:
            runner.cleanup()

    # Busy status marks the whole flash+test run — the self-update timer reads
    # it to defer a pull+restart until the rig is idle.
    _run_exclusive("run_tests", "testing", _do)


@socketio.on("run_perf")
def on_run_perf(data):
    """Measure firmware performance from the touch UI.

    The selected images must be a **profiling** pair (built with
    ``-e POLYKYBD_LOOP_PROFILE=yes``); a normal build NACKs the profiler command
    and the run reports that instead of guessing. Same flash+settle path as a
    HIL run, so it is safe to launch from the kiosk."""
    if not isinstance(data, dict):
        return
    left_path  = _selected_firmware(data.get("left_uf2"))
    right_path = _selected_firmware(data.get("right_uf2"))
    if not left_path or not right_path:
        emit_log("[ui] select a valid left and right firmware file first")
        return

    def _do():
        from station.perf_runner import (
            PerfRunner, compare_to_baseline, format_markdown, load_baseline,
        )
        from station.test_runner import _derive_label
        label = _derive_label(left_path) or "split72"
        runner = PerfRunner(log=emit_log)
        try:
            report = runner.run(left_path, right_path, label=label)
            baseline = load_baseline(
                str(Path(__file__).resolve().parents[2] / "perf" / "baselines" / f"{label}.json"),
                log=emit_log)
            comparison = compare_to_baseline(report, baseline) if baseline else []
            for line in format_markdown(report, comparison).splitlines():
                emit_log(f"[perf] {line}")
            socketio.emit("perf_result", {"report": report, "comparison": comparison})
            set_status("idle")
        except Exception as exc:
            emit_log(f"[ui] perf error: {exc}")
            set_status("error")
        finally:
            runner.cleanup()

    # Same busy marker as a test run, so the self-update timer defers a
    # pull+restart until the measurement is finished rather than killing it.
    _run_exclusive("run_perf", "testing", _do)


def _on_sigterm(signum, frame):
    try:
        from station.flash import FlashController
        fc = FlashController()
        for side in ("left", "right"):
            try:
                fc.usb_power(side, True)
            except Exception:
                pass
        fc.cleanup()
        FlashController.gpio_cleanup_final()
    except Exception:
        pass
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _on_sigterm)


if __name__ == "__main__":
    socketio.run(app, host=UI_HOST, port=UI_PORT, allow_unsafe_werkzeug=True)
