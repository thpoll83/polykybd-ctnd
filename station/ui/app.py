# SPDX-License-Identifier: GPL-2.0-only
import json
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
    FIRMWARE_DIR, UI_HOST, UI_PORT, GITHUB_REPO, GITHUB_TOKEN, RUNNER_LABELS,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = "polykybd-ctnd"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# Labels the HIL job requires (case-insensitive). GitHub adds `self-hosted`,
# `Linux`, `ARM64` automatically; `polykybd-ctnd` comes from the runner config.
REQUIRED_LABELS = {label.lower() for label in RUNNER_LABELS}

_status         = {"value": "idle"}
_ci_state       = {"running": False, "url": None}
_runner_state   = {"status": "unknown"}            # unknown|online|busy|offline|missing|noauth
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
            pass
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
            pass
        time.sleep(30)


if GITHUB_REPO:
    threading.Thread(target=_ci_poll_loop, daemon=True).start()
    threading.Thread(target=_runner_poll_loop, daemon=True).start()


# ── On-demand runner diagnostics ──────────────────────────────────────────────
# Streams a full troubleshooting report to the log panel: local runner service /
# process, GitHub-side registration + labels, queued jobs (and the labels they
# request), connectivity, then a plain-language verdict. Triggered by the RUNNER
# badge or the "Diagnose Runner" button.

def _diag_local_runner(log) -> dict:
    """Report the local runner systemd service + process. Returns
    {"systemctl": bool, "unit": str|None, "active": bool, "process_running": bool}
    so the verdict can give an exact start command."""
    info = {"systemctl": bool(shutil.which("systemctl")),
            "unit": None, "active": False, "process_running": False}
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


def _diag_queued(log) -> None:
    """List queued workflow runs and the labels each waiting job requests."""
    code, data = _gh_api(f"/repos/{GITHUB_REPO}/actions/runs?status=queued&per_page=5")
    if data is None:
        log(f"    [?] could not query queued runs (status {code})")
        return
    runs = data.get("workflow_runs", [])
    if not runs:
        log("    none queued")
        return
    for run in runs:
        log(f"    #{run.get('run_number')} '{run.get('name')}'  "
            f"{run.get('event')} on {run.get('head_branch')}")
        _, jdata = _gh_api(f"/repos/{GITHUB_REPO}/actions/runs/{run.get('id')}/jobs")
        for job in (jdata or {}).get("jobs", []):
            if job.get("status") in ("queued", "waiting"):
                labels = job.get("labels", [])
                log(f"        ⏳ job '{job.get('name')}' wants [{', '.join(labels)}]")


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

    # Local runner state is the most common and most actionable cause — lead with it.
    if local.get("unit") and not local.get("active"):
        out.append("Root cause (local): the runner service is installed but NOT running —")
        out.append("nothing on this Pi will ever pick up the job. Start it:")
        out.append(f"  sudo systemctl start {local['unit']}")
        out.append("  (or:  cd ~/actions-runner && sudo ./svc.sh start)")
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
        if all((not has_labels(r)) or r.get("status") != "online" or r.get("busy")
               for r in runners):
            out.append("A matching runner exists but is BUSY — it will pick up the job")
            out.append("as soon as the current one finishes.")
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
        out.append("Root cause: the matching runner is OFFLINE.")
        if local.get("process_running"):
            out.append("It runs locally but GitHub sees it offline → outbound HTTPS to")
            out.append("*.actions.githubusercontent.com may be blocked, or it is a duplicate")
            out.append("registration. Check ~/actions-runner/_diag/ logs.")
        else:
            out.append("Start it on the Pi:  cd ~/actions-runner && sudo ./svc.sh start")
            out.append("(or re-register:  scripts/register-runner.sh --token <TOKEN>)")
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
        _diag_queued(log)
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
        pass  # GPIO / uhubctl not available (e.g. dev machine)


threading.Thread(target=_query_usb_state_at_startup, daemon=True).start()


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


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
    if GITHUB_REPO:
        socketio.emit("ci_status", dict(_ci_state))


@socketio.on("run_diagnostics")
def on_run_diagnostics(_data=None):
    threading.Thread(target=_run_diagnostics, daemon=True).start()


@socketio.on("flash")
def on_flash(data):
    side = data.get("side")
    uf2  = data.get("uf2")
    if side not in ("left", "right") or not uf2:
        return
    uf2_path = str(Path(FIRMWARE_DIR) / uf2)
    if not os.path.exists(uf2_path):
        emit_log(f"[ui] firmware not found: {uf2_path}")
        return

    set_status(f"flashing-{side}")

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

    threading.Thread(target=_do, daemon=True).start()


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


@socketio.on("run_tests")
def on_run_tests(data):
    left_uf2  = data.get("left_uf2")
    right_uf2 = data.get("right_uf2")
    if not left_uf2 or not right_uf2:
        return

    left_path  = str(Path(FIRMWARE_DIR) / left_uf2)
    right_path = str(Path(FIRMWARE_DIR) / right_uf2)

    def _do():
        from station.test_runner import TestRunner
        runner = TestRunner(log=emit_log)
        try:
            result = runner.flash_and_test(left_path, right_path)
            socketio.emit("test_result", result)
        except Exception as exc:
            emit_log(f"[ui] test error: {exc}")
            set_status("error")
        finally:
            runner.cleanup()

    threading.Thread(target=_do, daemon=True).start()


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
