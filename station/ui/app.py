# SPDX-License-Identifier: GPL-2.0-only
import os
import threading
from pathlib import Path

from flask import Flask, render_template, jsonify
from flask_socketio import SocketIO

from station.config import FIRMWARE_DIR, UI_HOST, UI_PORT

app = Flask(__name__)
app.config["SECRET_KEY"] = "polykybd-ctnd"
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

_status = {"value": "idle"}


def emit_log(msg: str) -> None:
    socketio.emit("log", {"msg": msg})


def set_status(s: str) -> None:
    _status["value"] = s
    socketio.emit("status", {"value": s})


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/firmware")
def list_firmware():
    base = Path(FIRMWARE_DIR)
    if not base.exists():
        return jsonify([])
    files = sorted(p.name for p in base.glob("*.uf2"))
    return jsonify(files)


@socketio.on("connect")
def on_connect(_auth=None):
    socketio.emit("status", {"value": _status["value"]})


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


if __name__ == "__main__":
    socketio.run(app, host=UI_HOST, port=UI_PORT)
