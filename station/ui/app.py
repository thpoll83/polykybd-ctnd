# SPDX-License-Identifier: GPL-2.0-only
import json
import os
import signal
import threading
import urllib.request
import urllib.error
from pathlib import Path

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

from station.config import FIRMWARE_DIR, UI_HOST, UI_PORT, GITHUB_REPO, GITHUB_TOKEN

app = Flask(__name__)
app.config["SECRET_KEY"] = "polykybd-ctnd"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_status   = {"value": "idle"}
_ci_state = {"running": False, "url": None}
_usb_state = {"left": None, "right": None}  # None = unknown, True/False = on/off


def emit_log(msg: str) -> None:
    socketio.emit("log", {"msg": msg})


def set_status(s: str) -> None:
    _status["value"] = s
    socketio.emit("status", {"value": s})


# ── CI status poller ──────────────────────────────────────────────────────────

def _ci_poll_once():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/actions/runs?status=in_progress&per_page=1"
    req = urllib.request.Request(url, headers={"User-Agent": "polykybd-ctnd/1.0"})
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
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


if GITHUB_REPO:
    threading.Thread(target=_ci_poll_loop, daemon=True).start()


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
        pass  # GPIO / uhubctl not available (e.g. dev machine) — stay as None


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
    socketio.emit("status", {"value": _status["value"]})
    socketio.emit("usb_state", dict(_usb_state))
    if GITHUB_REPO:
        socketio.emit("ci_status", dict(_ci_state))


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


@socketio.on("reset_board")
def on_reset_board(data):
    side = data.get("side")
    if side not in ("left", "right"):
        return

    def _do():
        from station.flash import FlashController
        fc = FlashController()
        try:
            fc.reset(side, log=emit_log)
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
                fc.usb_power(side, False)
            except Exception:
                pass
        fc.cleanup()
    except Exception:
        pass
    raise SystemExit(0)


signal.signal(signal.SIGTERM, _on_sigterm)


if __name__ == "__main__":
    socketio.run(app, host=UI_HOST, port=UI_PORT, allow_unsafe_werkzeug=True)
