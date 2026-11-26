# SPDX-License-Identifier: GPL-2.0-only
"""Rig camera: snapshots for the UI and the RGB phase check between the halves.

The camera is a Raspberry Pi CSI camera driven through the ``rpicam-apps``
command-line tools (``rpicam-still``, ``rpicam-vid``). They come with
Raspberry Pi OS, so the rig venv needs no new package (picamera2 is an apt
package and is not visible inside the venv).

The phase check measures the offset between the two halves' copies of the
breathing flash cue that the firmware shows while it stages an image
(``keys_com/rgb.c``: ``phase = timer_read32() >> 3``, a 2048 ms triangle). The
camera records both halves side by side; the analysis picks the pixels that
breathe on each side of ``split_x``, builds one brightness trace per half, and
finds the lag with the highest circular cross-correlation within one period.

Everything after the capture is plain Python with no camera, so
``tests/camera_test.py`` runs it on synthetic frames.
"""
import json
import logging
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from . import rig_lock
from .config import CAMERA

# The flash cue's period: 256 phase steps of 8 ms (timer_read32() >> 3, as uint8).
FLASH_CUE_PERIOD_MS = 2048.0

_lock = threading.Lock()   # one capture at a time inside this process

_VAR = Path(__file__).resolve().parent.parent / "var"
# One capture at a time across processes: the CI runner records the phase check,
# the UI takes snapshots. Only the camera, not the rig: the UI may show the camera
# while a CI job runs, except during the CI's recording.
CAMERA_LOCK_PATH = _VAR / "camera.lock"
# The recording in progress, in RAM: rec.yuv grows frame by frame and rec.json
# says what records it, so the UI can show its newest frame meanwhile.
LIVE_DIR = Path("/dev/shm/polykybd-cam") if os.path.isdir("/dev/shm") else _VAR / "camera-live"
# Three frames of the last recording (start, middle, end) with the same label.
FRAMES_DIR = _VAR / "camera_frames"
FRAMES_KEPT = 3


class CameraBusy(RuntimeError):
    """Another process (or thread) holds the camera."""


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


QUAD_SIDES = ("left", "right")   # the board halves, as named in var/camera_view.json


def parse_quads(v) -> tuple:
    """{"left": [[x, y] * 4], "right": [...]} (or the tuple form) to
    ((left corners), (right corners)). Each corner lies in 0..1 and each quad is
    convex, so the outline can be warped into it and its inside tested."""
    if not v:
        return ()
    if isinstance(v, dict):
        if set(v) != set(QUAD_SIDES):
            raise ValueError(f"camera quads need exactly the keys left and right, not {sorted(v)}")
        v = [v[k] for k in QUAD_SIDES]
    if not isinstance(v, (list, tuple)) or len(v) != 2:
        raise ValueError("camera quads must be two four-corner outlines")
    out = []
    for side, q in zip(QUAD_SIDES, v):
        try:
            pts = tuple((float(x), float(y)) for x, y in q)
        except (TypeError, ValueError):
            raise ValueError(f"camera quad {side} must be four [x, y] pairs") from None
        if len(pts) != 4:
            raise ValueError(f"camera quad {side} must have 4 corners, not {len(pts)}")
        if not all(0 <= c <= 1 for p in pts for c in p):
            raise ValueError(f"camera quad {side} must lie inside 0..1")
        if not _convex(pts):
            raise ValueError(f"camera quad {side} must be convex (corners in order, no crossing edges)")
        out.append(pts)
    return tuple(out)


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _convex(pts) -> bool:
    turns = [_cross(pts[i], pts[(i + 1) % 4], pts[(i + 2) % 4]) for i in range(4)]
    return all(t > 0 for t in turns) or all(t < 0 for t in turns)


def _parse_bool(key: str, v) -> bool:
    """A real bool or an explicit spelling. A typo (ture, flase) is an error,
    so it disables the camera with the reason instead of reading as false."""
    if isinstance(v, bool):
        return v
    t = str(v).strip().lower()
    if t in _TRUE:
        return True
    if t in _FALSE:
        return False
    raise ValueError(f"camera.{key} must be true or false, not {v!r}")


@dataclass
class CameraSettings:
    enabled: str = "auto"          # auto | yes | no
    rotation: int = 0              # 0 or 180 (rpicam-apps supports only these)
    hflip: bool = False
    vflip: bool = False
    split_x: float = 0.5           # left/right boundary, fraction of image width
    master_on_left: bool = True    # the image's left side shows the master half
    width: int = 320               # recording size; a multiple of 64 (no row padding)
    height: int = 240
    framerate: int = 58
    mode: str = "640:480:10:P"     # OV5647 640x480 mode, 58.92 fps max; "" lets rpicam choose
    shutter_us: int = 15000        # fixed exposure, below one frame (17.2 ms at 58 fps)
    gain: float = 4.0
    awb_gains: str = "1.5,1.5"     # fixed red,blue gains: no auto white balance
    record_s: float = 8.0
    roi: str = ""                  # digital crop "x,y,w,h" as fractions of the full view; "" = none
    # Two four-corner outlines from the UI's "Adjust view": (left board, right
    # board), each ((x, y) * 4) in sensor fractions, corners in the order of the
    # board outline's top-left, top-right, bottom-right, bottom-left. () = none;
    # the check then splits the image at split_x.
    quads: tuple = ()

    @classmethod
    def from_config(cls, c: dict | None) -> "CameraSettings":
        if c is not None and not isinstance(c, dict):
            raise TypeError(f"camera must be a mapping of settings, not {type(c).__name__} {c!r}")
        s = cls()
        for k, v in (c or {}).items():
            if not hasattr(s, k) or v is None:
                continue
            if k == "quads":
                s.quads = parse_quads(v)
                continue
            default = getattr(s, k)
            if isinstance(default, bool):
                v = _parse_bool(k, v)
            elif k != "enabled":
                v = type(default)(v)
            setattr(s, k, v)
        # YAML reads a bare yes/no as a bool, so normalise enabled to auto|yes|no.
        e = s.enabled
        if isinstance(e, bool):
            s.enabled = "yes" if e else "no"
        else:
            e = str(e).strip().lower()
            s.enabled = {"true": "yes", "on": "yes", "1": "yes",
                         "false": "no", "off": "no", "0": "no"}.get(e, e)
        if s.enabled not in ("auto", "yes", "no"):
            raise ValueError(f"camera.enabled must be auto, yes or no, not {s.enabled!r}")
        if s.rotation not in (0, 180):
            raise ValueError(f"camera.rotation must be 0 or 180, not {s.rotation}")
        if s.width % 64:
            raise ValueError(f"camera.width must be a multiple of 64, not {s.width}")
        if s.roi:
            try:
                x, y, w, h = (float(v) for v in s.roi.split(","))
            except ValueError:
                raise ValueError(f"camera.roi must be 'x,y,w,h' (four fractions), not {s.roi!r}") from None
            if not (0 <= x < 1 and 0 <= y < 1 and 0 < w <= 1 - x + 1e-9 and 0 < h <= 1 - y + 1e-9):
                raise ValueError(f"camera.roi {s.roi!r} must lie inside 0..1 with w, h > 0")
        if not 0.05 < s.split_x < 0.95:
            raise ValueError(f"camera.split_x must be between 0.05 and 0.95, not {s.split_x}")
        return s

    def orient_args(self, crop: bool = True) -> list[str]:
        """rpicam arguments for rotation, flips and (unless crop is False) the ROI."""
        args = []
        if self.rotation:
            args += ["--rotation", str(self.rotation)]
        if self.hflip:
            args.append("--hflip")
        if self.vflip:
            args.append("--vflip")
        if self.roi and crop:
            args += ["--roi", self.roi]
        return args


# The crop and split line set graphically in the UI's Camera tab ("Adjust
# view"). They override camera.roi and camera.split_x from config.yaml and
# nothing else. The UI writes the file, the CI runner reads it at import (each
# run is a new process). A missing or invalid file leaves config.yaml in charge.
VIEW_OVERRIDE_PATH = Path(__file__).resolve().parent.parent / "var" / "camera_view.json"
VIEW_KEYS = ("roi", "split_x", "quads")
_view_lock = threading.Lock()   # file write/unlink and the SETTINGS update, as one step


def load_view_override(path: Path | None = None) -> dict:
    path = VIEW_OVERRIDE_PATH if path is None else path
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return {k: d[k] for k in VIEW_KEYS if isinstance(d, dict) and k in d}


def _load_settings(c: dict | None, view: dict | None = None) -> tuple[CameraSettings, str]:
    """Settings from config.yaml, or a disabled camera and the reason.

    A bad value in the optional camera block must not stop test_runner (which
    imports this module) from loading, so it disables the camera instead. The
    check then SKIPs with the reason, and the UI shows it.
    """
    try:
        s = CameraSettings.from_config(c)
    except (TypeError, ValueError) as exc:
        logging.getLogger("polykybd-ctnd").warning("camera block in config.yaml ignored: %s", exc)
        return CameraSettings(enabled="no"), f"camera block in config.yaml is invalid: {exc}"
    if view:
        try:
            merged = {**(c or {}), **view}
            v = CameraSettings.from_config(merged)
            s.roi, s.split_x, s.quads = v.roi, v.split_x, v.quads
        except (TypeError, ValueError) as exc:
            logging.getLogger("polykybd-ctnd").warning(
                "camera view override ignored (config.yaml values used): %s", exc)
    return s, ""


SETTINGS, CONFIG_ERROR = _load_settings(CAMERA, load_view_override())


def _flip_axes(s: CameraSettings) -> tuple[bool, bool]:
    """Whether the output image is mirrored horizontally and vertically against
    the sensor (rotation 180 flips both axes)."""
    rot = s.rotation == 180
    return s.hflip != rot, s.vflip != rot


def _roi_box(s: CameraSettings) -> tuple[float, float, float, float]:
    if not s.roi:
        return 0.0, 0.0, 1.0, 1.0
    x, y, w, h = (float(v) for v in s.roi.split(","))
    return x, y, w, h


def to_output(pt, s: CameraSettings) -> tuple[float, float]:
    """A sensor fraction (x, y) as a fraction of the recorded (cropped, flipped)
    image. Outside 0..1 when the point lies outside the crop."""
    rx, ry, rw, rh = _roi_box(s)
    u, v = (pt[0] - rx) / rw, (pt[1] - ry) / rh
    fh, fv = _flip_axes(s)
    return (1 - u if fh else u), (1 - v if fv else v)


def view_from_quads(quads: tuple, s: CameraSettings) -> tuple[str, float]:
    """The crop (roi) and split_x that go with two quads: the crop is the
    bounding box of both, rounded outwards to 0.001; split_x lies between the
    two quads in the recorded image (in the middle of the gap, or between their
    centres when they overlap in x)."""
    xs = [x for q in quads for x, _ in q]
    ys = [y for q in quads for _, y in q]
    x0, y0 = math.floor(min(xs) * 1000) / 1000, math.floor(min(ys) * 1000) / 1000
    x1, y1 = math.ceil(max(xs) * 1000) / 1000, math.ceil(max(ys) * 1000) / 1000
    full = x0 <= 0.001 and y0 <= 0.001 and x1 >= 0.999 and y1 >= 0.999
    roi = "" if full else f"{x0:g},{y0:g},{round(x1 - x0, 3):g},{round(y1 - y0, 3):g}"
    out = replace(s, roi=roi)
    us = [[to_output(p, out)[0] for p in q] for q in quads]
    a, b = sorted(us, key=lambda u: sum(u))
    split = (max(a) + min(b)) / 2 if max(a) < min(b) else (sum(a) + sum(b)) / 8
    return roi, round(split, 3)


def save_view_override(roi: str, split_x: float, path: Path | None = None,
                       quads=None) -> str:
    """Validate and store the view from the UI, and apply it to SETTINGS in
    place (functions take SETTINGS as a default argument, so the object must
    stay the same). With quads, the crop and split_x are derived from them and
    the roi and split_x given are ignored. Returns "" or the reason it was
    refused.
    """
    path = VIEW_OVERRIDE_PATH if path is None else path
    try:
        if quads:
            q = parse_quads(quads)
            roi, split_x = view_from_quads(q, CameraSettings.from_config(CAMERA))
        v = CameraSettings.from_config({**(CAMERA or {}), "roi": roi, "split_x": split_x,
                                        "quads": quads or None})
    except (TypeError, ValueError) as exc:
        return str(exc)
    data = {"roi": v.roi, "split_x": v.split_x}
    if v.quads:
        data["quads"] = {k: [list(p) for p in q] for k, q in zip(QUAD_SIDES, v.quads)}
    with _view_lock:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data))
            os.replace(tmp, path)
        except OSError as exc:
            return f"could not save the view: {exc}"
        SETTINGS.roi, SETTINGS.split_x, SETTINGS.quads = v.roi, v.split_x, v.quads
    return ""


def snapshot_settings(settings: CameraSettings) -> CameraSettings:
    """A copy taken under _view_lock, so a save or reset cannot land between
    reading roi and split_x (a torn copy would pair one view's crop with
    another view's split)."""
    with _view_lock:
        return replace(settings)


def clear_view_override(path: Path | None = None) -> str:
    """Remove the UI override; config.yaml's roi and split_x apply again."""
    path = VIEW_OVERRIDE_PATH if path is None else path
    with _view_lock:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            return f"could not remove the view override: {exc}"
        s, _ = _load_settings(CAMERA)
        SETTINGS.roi, SETTINGS.split_x, SETTINGS.quads = s.roi, s.split_x, s.quads
    return ""


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    # Audit: argv is a list (no shell). argv[0] is a fixed rpicam-apps tool name;
    # every other element comes from CameraSettings, which config.yaml fills and
    # from_config() type-checks. No request data reaches this call.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)


def availability(settings: CameraSettings = SETTINGS) -> tuple[bool, str]:
    """(usable, reason). Never raises: a rig without a camera SKIPs."""
    if settings is SETTINGS and CONFIG_ERROR:
        return False, CONFIG_ERROR
    if settings.enabled == "no":
        return False, "camera.enabled is no in config.yaml"
    if shutil.which("rpicam-hello") is None:
        return False, "rpicam-apps not installed (rpicam-hello not on PATH)"
    try:
        r = _run(["rpicam-hello", "--list-cameras"], timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"rpicam-hello --list-cameras failed: {exc}"
    out = (r.stdout + r.stderr).decode(errors="replace")
    if "No cameras available" in out or ": " not in out:
        return False, "no camera detected (rpicam-hello --list-cameras)"
    return True, out.strip().splitlines()[-1] if out.strip() else "camera found"


def snapshot_jpeg(settings: CameraSettings = SETTINGS,
                  width: int = 1296, height: int = 972, crop: bool = True) -> bytes:
    """One JPEG with the configured rotation and flips, for the UI's camera view.
    ``crop=False`` ignores the ROI (the UI's "Adjust view" draws the crop on the
    full image).

    Raises RuntimeError when the camera is disabled in config.yaml, busy (a CI
    capture holds it) or absent.
    """
    if settings is SETTINGS and CONFIG_ERROR:
        raise RuntimeError(CONFIG_ERROR)
    if settings.enabled == "no":
        raise RuntimeError("camera.enabled is no in config.yaml")
    if not _lock.acquire(blocking=False):
        raise CameraBusy("camera busy (a capture is running)")
    cam_fd = None
    try:
        cam_fd = rig_lock.try_acquire(CAMERA_LOCK_PATH)
        if cam_fd is None:
            raise CameraBusy("camera busy (a recording is running)")
        r = _run(["rpicam-still", "-n", "-t", "300", "--immediate",
                  "--width", str(width), "--height", str(height),
                  "-e", "jpg", "-o", "-", *settings.orient_args(crop)], timeout=20)
        if r.returncode != 0 or not r.stdout.startswith(b"\xff\xd8"):
            err = r.stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(err[-1] if err else f"rpicam-still exit {r.returncode}")
        return r.stdout
    finally:
        rig_lock.release(cam_fd)
        _lock.release()


# ── Recording ────────────────────────────────────────────────────────────────

@dataclass
class Recording:
    width: int
    height: int
    timestamps_ms: list[float]
    lumas: list[bytes] = field(repr=False)   # the Y plane of each frame


def parse_pts(text: str) -> list[float]:
    """rpicam-vid --save-pts: a '# timecode format v2' header, then one ms value per line."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(float(line))
    return out


def split_yuv420(data: bytes, width: int, height: int) -> list[bytes]:
    """The Y planes of a raw I420 stream (width a multiple of 64, so no row padding)."""
    frame = width * height * 3 // 2
    y = width * height
    return [data[i:i + y] for i in range(0, len(data) - frame + 1, frame)]


def _acquire_camera(timeout_s: float):
    """The cross-process camera lock, waiting up to timeout_s for a snapshot to end."""
    deadline = time.monotonic() + timeout_s
    while True:
        fd = rig_lock.try_acquire(CAMERA_LOCK_PATH)
        if fd is not None or time.monotonic() >= deadline:
            return fd
        time.sleep(0.05)


def record(settings: CameraSettings = SETTINGS, seconds: float | None = None,
           label: dict | None = None) -> Recording:
    """Record raw frames to RAM (/dev/shm) and return their Y planes and timestamps.

    While it records, LIVE_DIR holds the growing raw file and its label, for
    live_frame(). Afterwards three frames are kept in FRAMES_DIR (save_frames())."""
    seconds = settings.record_s if seconds is None else seconds
    label = recording_label() if label is None else label
    if not _lock.acquire(timeout=30):
        raise CameraBusy("camera busy (a snapshot did not finish)")
    cam_fd = None
    try:
        cam_fd = _acquire_camera(30)
        if cam_fd is None:
            raise CameraBusy("camera busy (another process holds it)")
        started = time.time()   # after any wait for a snapshot to finish
        LIVE_DIR.mkdir(parents=True, exist_ok=True)
        raw, pts = str(LIVE_DIR / "rec.yuv"), str(LIVE_DIR / "rec.pts")
        for stale in (raw, pts):
            if os.path.exists(stale):
                os.unlink(stale)
        _write_json(LIVE_DIR / "rec.json", {"width": settings.width, "height": settings.height,
                                            "started": started, "label": label})
        try:
            argv = ["rpicam-vid", "-n", "-t", str(int(seconds * 1000)),
                    "--codec", "yuv420", "-o", raw, "--save-pts", pts,
                    "--width", str(settings.width), "--height", str(settings.height),
                    "--framerate", str(settings.framerate),
                    "--shutter", str(settings.shutter_us), "--gain", str(settings.gain),
                    "--awbgains", settings.awb_gains, "--denoise", "cdn_off",
                    *settings.orient_args()]
            if settings.mode:
                argv += ["--mode", settings.mode]
            r = _run(argv, timeout=seconds + 30)
            if r.returncode != 0:
                err = r.stderr.decode(errors="replace").strip().splitlines()
                raise RuntimeError(err[-1] if err else f"rpicam-vid exit {r.returncode}")
            with open(raw, "rb") as f:
                lumas = split_yuv420(f.read(), settings.width, settings.height)
            with open(pts) as f:
                ts = parse_pts(f.read())
        finally:
            for name in ("rec.json", "rec.yuv", "rec.pts"):
                try:
                    (LIVE_DIR / name).unlink()
                except OSError:
                    pass
    finally:
        rig_lock.release(cam_fd)
        _lock.release()
    n = min(len(lumas), len(ts))
    rec = Recording(settings.width, settings.height, ts[:n], lumas[:n])
    try:
        save_frames(rec, label, started)
    except OSError as exc:
        logging.getLogger(__name__).warning("camera: could not save frames: %s", exc)
    return rec


# ── Frames for the UI: the recording in progress and the last one ────────────

def recording_label(env: dict | None = None) -> dict:
    """What records: a CI job (run number, job, PR or branch, commit) or the UI.
    ``text`` is the caption the UI shows."""
    env = os.environ if env is None else env
    if env.get("GITHUB_ACTIONS") == "true":
        m = re.match(r"refs/pull/(\d+)/", env.get("GITHUB_REF", ""))
        where = (f"PR #{m.group(1)}" if m else
                 env.get("GITHUB_HEAD_REF") or env.get("GITHUB_REF_NAME") or "")
        lab = {"source": "ci", "run": env.get("GITHUB_RUN_NUMBER", ""),
               "run_id": env.get("GITHUB_RUN_ID", ""), "job": env.get("GITHUB_JOB", ""),
               "where": where, "commit": env.get("GITHUB_SHA", "")[:7]}
        parts = [f"CI run #{lab['run']}" if lab["run"] else "CI run", lab["job"],
                 lab["where"], lab["commit"]]
        lab["text"] = " · ".join(p for p in parts if p)
        return lab
    return {"source": "ui", "text": "UI · Measure phase now"}


def png_gray(width: int, height: int, data: bytes) -> bytes:
    """An 8-bit greyscale PNG of one Y plane (no library needed)."""
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    rows = b"".join(b"\x00" + data[y * width:(y + 1) * width] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def save_frames(rec: Recording, label: dict, started: float,
                directory: Path | None = None) -> dict | None:
    """Keep FRAMES_KEPT frames of a recording (start, middle, end) as PNG files
    plus frames.json (label, time of each frame). Returns the metadata."""
    directory = FRAMES_DIR if directory is None else directory
    n = len(rec.lumas)
    if not n:
        return None
    picks = sorted({0, n // 2, n - 1})
    directory.mkdir(parents=True, exist_ok=True)
    t0 = rec.timestamps_ms[0] if rec.timestamps_ms else 0.0
    frames = []
    for k, i in enumerate(picks):
        t_ms = (rec.timestamps_ms[i] - t0) if i < len(rec.timestamps_ms) else 0.0
        name = f"frame{k}.png"
        tmp = directory / (name + ".tmp")
        tmp.write_bytes(png_gray(rec.width, rec.height, rec.lumas[i]))
        os.replace(tmp, directory / name)
        frames.append({"file": name, "t_ms": round(t_ms, 1),
                       "at": datetime.fromtimestamp(started + t_ms / 1000, timezone.utc)
                                     .isoformat(timespec="seconds")})
    meta = {"label": label, "frames": frames, "count": n,
            "started": datetime.fromtimestamp(started, timezone.utc).isoformat(timespec="seconds")}
    _write_json(directory / "frames.json", meta)
    return meta


def load_frames(directory: Path | None = None) -> dict | None:
    directory = FRAMES_DIR if directory is None else directory
    try:
        with open(directory / "frames.json") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def live_frame(directory: Path | None = None) -> tuple[bytes, dict] | None:
    """The newest complete frame of the recording in progress as PNG, with its
    label, or None when nothing records."""
    directory = LIVE_DIR if directory is None else directory
    try:
        with open(directory / "rec.json") as f:
            meta = json.load(f)
        w, h = int(meta["width"]), int(meta["height"])
        size = os.path.getsize(directory / "rec.yuv")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    frame = w * h * 3 // 2
    count = size // frame
    if count < 1:
        return None
    try:
        with open(directory / "rec.yuv", "rb") as f:
            f.seek((count - 1) * frame)
            y = f.read(w * h)
    except OSError:   # the recording ended and removed the file meanwhile
        return None
    if len(y) < w * h:
        return None
    meta["frame"] = count
    meta["elapsed_s"] = round(time.time() - float(meta.get("started", time.time())), 1)
    return png_gray(w, h, y), meta


class BackgroundRecording:
    """Run record() in a thread: start() when the cue begins, result() afterwards."""

    def __init__(self, settings: CameraSettings = SETTINGS, seconds: float | None = None):
        self._settings, self._seconds = settings, seconds
        self._thread = None
        self._rec = None
        self._err = None

    @property
    def started(self) -> bool:
        return self._thread is not None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._body, daemon=True)
            self._thread.start()

    def _body(self) -> None:
        try:
            self._rec = record(self._settings, self._seconds)
        except Exception as exc:   # reported by result(), never kills the caller
            self._err = exc

    def result(self, timeout: float = 60.0) -> Recording:
        if self._thread is None:
            raise RuntimeError("recording never started")
        self._thread.join(timeout)
        if self._thread.is_alive():
            raise RuntimeError("recording did not finish")
        if self._err is not None:
            raise self._err
        return self._rec


# ── Analysis ─────────────────────────────────────────────────────────────────

def quad_pixels(s: CameraSettings, width: int, height: int) -> tuple:
    """settings.quads in pixels of the recorded image: ((left board), (right
    board)), corners in the outline's order. () without quads."""
    return tuple(tuple((u * width, v * height) for u, v in (to_output(p, s) for p in q))
                 for q in s.quads)


def inside_quad(q, x: float, y: float) -> bool:
    """Whether (x, y) lies in the convex quad q (either winding)."""
    turns = [_cross(q[i], q[(i + 1) % 4], (x, y)) for i in range(4)]
    return all(t >= 0 for t in turns) or all(t <= 0 for t in turns)


def side_traces(rec: Recording, split_x: float, step: int = 4,
                keep: float = 0.25, quads_px: tuple = ()) -> tuple[list[float], list[float]]:
    """One brightness trace per image side (left of split_x, right of it).

    With quads_px (two quads in pixels, see quad_pixels), each side is the
    inside of one quad instead, and the image-left side is the quad whose
    centre lies further left.
    Samples every ``step``-th pixel in x and y, keeps the samples on each side
    whose variance over time is at least ``keep`` times that side's largest
    variance (the breathing LEDs), and averages them per frame. Static parts of
    the image (keycap legends, the desk) drop out because they do not vary,
    however small the LEDs are in the frame.
    """
    w, h = rec.width, rec.height
    cut = int(w * split_x)
    cols = list(range(0, w, step))
    rows = list(range(0, h, step))
    if quads_px:
        a, b = sorted(quads_px, key=lambda q: sum(x for x, _ in q))
        idx_l = [r * w + c for r in rows for c in cols if inside_quad(a, c, r)]
        idx_r = [r * w + c for r in rows for c in cols if inside_quad(b, c, r)]
        if not idx_l or not idx_r:
            raise ValueError("a camera quad covers no pixel of the recording; adjust the view again")
    else:
        idx_l = [r * w + c for r in rows for c in cols if c < cut]
        idx_r = [r * w + c for r in rows for c in cols if c >= cut]

    def pick(idx: list[int]) -> list[int]:
        n = len(rec.lumas)
        s = [0.0] * len(idx)
        s2 = [0.0] * len(idx)
        for y in rec.lumas:
            for j, i in enumerate(idx):
                v = y[i]
                s[j] += v
                s2[j] += v * v
        var = [(s2[j] - s[j] * s[j] / n) / n for j in range(len(idx))]
        limit = max(var) * keep
        chosen = [idx[j] for j in range(len(idx)) if var[j] >= limit and var[j] > 0]
        return chosen or idx[:1]

    sel_l, sel_r = pick(idx_l), pick(idx_r)
    left = [sum(y[i] for i in sel_l) / len(sel_l) for y in rec.lumas]
    right = [sum(y[i] for i in sel_r) / len(sel_r) for y in rec.lumas]
    return left, right


# ── Rectifying one half ──────────────────────────────────────────────────────
# The quad of a half is where the corners of its board outline (the view box of
# station/ui/static/pcb/<board>.svg) are in the image. A homography maps board
# millimetres to image pixels, so each LED of the board JSON can be found in a
# recording, and a half can be cut out as a straight, top-down image.

BOARD_DIR = Path(__file__).resolve().parent / "ui" / "static" / "pcb"
BOARDS = {"left": "split72_left", "right": "split72_right"}


def load_board(side: str, directory: Path | None = None) -> dict:
    """The board JSON of one half (width_mm, height_mm, leds with ref, x, y)."""
    d = BOARD_DIR if directory is None else directory
    return json.loads((d / f"{BOARDS[side]}.json").read_text())


def homography(src, dst) -> list[float]:
    """The 3x3 projective map (row-major, h[8] = 1) taking four src points to
    four dst points, by Gaussian elimination on the 8x8 system."""
    a = []
    for (x, y), (u, v) in zip(src, dst):
        a.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        a.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    for col in range(8):
        piv = max(range(col, 8), key=lambda r: abs(a[r][col]))
        if abs(a[piv][col]) < 1e-12:
            raise ValueError("degenerate quad: three corners on a line")
        a[col], a[piv] = a[piv], a[col]
        for r in range(8):
            if r != col:
                f = a[r][col] / a[col][col]
                a[r] = [rv - f * cv for rv, cv in zip(a[r], a[col])]
    return [a[i][8] / a[i][i] for i in range(8)] + [1.0]


def apply_h(h: list[float], x: float, y: float) -> tuple[float, float]:
    d = h[6] * x + h[7] * y + h[8]
    return (h[0] * x + h[1] * y + h[2]) / d, (h[3] * x + h[4] * y + h[5]) / d


def board_to_image(quad_px, board: dict) -> list[float]:
    """The homography from board millimetres (the view box) to image pixels."""
    w, h = board["width_mm"], board["height_mm"]
    return homography([(0, 0), (w, 0), (w, h), (0, h)], quad_px)


def led_pixels(quad_px, board: dict) -> list[tuple[str, float, float]]:
    """Each LED of the board as (ref, x, y) in image pixels."""
    hm = board_to_image(quad_px, board)
    return [(led["ref"], *apply_h(hm, led["x"], led["y"])) for led in board["leds"]]


def rectify(luma: bytes, width: int, height: int, quad_px, board: dict,
            px_per_mm: float = 1.0) -> tuple[int, int, bytes]:
    """One half cut out of a luma frame and straightened to the board's view
    box, px_per_mm pixels per millimetre (nearest sample; 0 outside the frame).
    Returns (width, height, luma)."""
    ow = max(1, round(board["width_mm"] * px_per_mm))
    oh = max(1, round(board["height_mm"] * px_per_mm))
    hm = board_to_image(quad_px, board)
    out = bytearray(ow * oh)
    for j in range(oh):
        for i in range(ow):
            x, y = apply_h(hm, (i + 0.5) / px_per_mm, (j + 0.5) / px_per_mm)
            xi, yi = int(x), int(y)
            if 0 <= xi < width and 0 <= yi < height and x >= 0 and y >= 0:
                out[j * ow + i] = luma[yi * width + xi]
    return ow, oh, bytes(out)


def resample(ts: list[float], values: list[float], dt: float) -> list[float]:
    """Linear interpolation of (ts, values) onto a uniform grid with spacing dt."""
    if len(ts) < 2:
        return list(values)
    out, j, t = [], 0, ts[0]
    while t <= ts[-1]:
        while j < len(ts) - 2 and ts[j + 1] < t:
            j += 1
        t0, t1 = ts[j], ts[j + 1]
        a = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
        out.append(values[j] + a * (values[j + 1] - values[j]))
        t += dt
    return out


@dataclass
class PhaseResult:
    offset_ms: float     # right trace lags the left one by this much, in [-P/2, P/2)
    correlation: float   # normalised peak, 1.0 = identical shapes
    swing_left: float    # peak-to-peak brightness of each trace (camera luma units)
    swing_right: float
    frames: int
    fps: float
    periodicity_left: float = 1.0    # each trace against itself one period later
    periodicity_right: float = 1.0
    step_left_ms: float = 0.0        # median time between LED brightness changes
    step_right_ms: float = 0.0


# The flash cue has 32 brightness levels (keys_com/rgb.c: v = 5 + (tri >> 2), 5..36).
CUE_LEVELS = 32


def step_interval_ms(ts: list[float], values: list[float]) -> float:
    """Median time between frames where the brightness changed by more than half
    a cue level: how often the LEDs on this half were actually updated.

    A half that redraws its LEDs normally changes level about every 32 ms (one
    level per 32 ms over a 1024 ms half cycle); a half whose LED task runs only
    now and then holds each level longer. 0.0 when fewer than two changes."""
    if len(values) < 3:
        return 0.0
    level = (max(values) - min(values)) / (CUE_LEVELS - 1)
    if level <= 0:
        return 0.0
    changes = [ts[i] for i in range(1, len(values))
               if abs(values[i] - values[i - 1]) > level / 2]
    gaps = sorted(b - a for a, b in zip(changes, changes[1:]))
    return gaps[len(gaps) // 2] if gaps else 0.0


def periodic_pearson(x: list[float], y: list[float]) -> float:
    """Pearson correlation of two equally long sequences; 0 when either is flat."""
    m = len(x)
    if m < 2:
        return 0.0
    mx, my = sum(x) / m, sum(y) / m
    sxy = sxx = syy = 0.0
    for p, q in zip(x, y):
        dp, dq = p - mx, q - my
        sxy += dp * dq
        sxx += dp * dp
        syy += dq * dq
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else 0.0


def periodicity(x: list[float], lag: int) -> float:
    """Pearson correlation of ``x`` with itself ``lag`` samples later.

    Close to 1 for the flash cue, which repeats every period. Low for a one-off
    brightness change (room light, the kiosk display), which also varies but
    does not repeat. 0 when the trace is shorter than one period plus a quarter.
    """
    n = len(x) - lag
    if lag <= 0 or n < lag // 4:
        return 0.0
    a, b = x[:n], x[lag:]
    ma, mb = sum(a) / n, sum(b) / n
    sab = sum((p - ma) * (q - mb) for p, q in zip(a, b))
    saa = sum((p - ma) ** 2 for p in a)
    sbb = sum((q - mb) ** 2 for q in b)
    return sab / math.sqrt(saa * sbb) if saa > 0 and sbb > 0 else 0.0


def phase_offset(ts: list[float], left: list[float], right: list[float],
                 period_ms: float = FLASH_CUE_PERIOD_MS, dt: float = 4.0) -> PhaseResult:
    """Circular cross-correlation of two traces over lags within one period."""
    a = resample(ts, left, dt)
    b = resample(ts, right, dt)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    period = int(round(period_ms / dt))
    per_a, per_b = periodicity(a, period), periodicity(b, period)
    ma, mb = sum(a) / n, sum(b) / n
    a = [x - ma for x in a]
    b = [x - mb for x in b]
    half = int(period_ms / dt / 2)

    def corr(k: int) -> float:
        # Positive k: b is a delayed copy of a, so compare a[i] with b[i + k].
        # Pearson correlation of the overlapping parts only, each with its own
        # mean and norm. Means and norms of the whole trace bias the peak once
        # the overlap stops covering whole periods (-22 ms at an 800 ms offset).
        x, y = (a[:n - k], b[k:]) if k >= 0 else (a[-k:], b[:n + k])
        return periodic_pearson(x, y)

    lags = range(-half, half)
    cs = [corr(k) for k in lags]
    best = max(range(len(cs)), key=cs.__getitem__)
    shift = 0.0
    if 0 < best < len(cs) - 1:   # parabola through the peak and its neighbours
        y0, y1, y2 = cs[best - 1], cs[best], cs[best + 1]
        den = y0 - 2 * y1 + y2
        if den:
            shift = 0.5 * (y0 - y2) / den
    fps = (len(ts) - 1) * 1000.0 / (ts[-1] - ts[0]) if len(ts) > 1 and ts[-1] > ts[0] else 0.0
    return PhaseResult(offset_ms=(lags[best] + shift) * dt, correlation=cs[best],
                       swing_left=max(left) - min(left), swing_right=max(right) - min(right),
                       frames=len(ts), fps=fps,
                       periodicity_left=per_a, periodicity_right=per_b,
                       step_left_ms=step_interval_ms(ts, left),
                       step_right_ms=step_interval_ms(ts, right))


def analyse_traces(rec: Recording, settings: CameraSettings = SETTINGS
                   ) -> tuple[PhaseResult, list[float], list[float]]:
    """analyse() plus the two traces (master first), for the UI chart."""
    left, right = side_traces(rec, settings.split_x,
                              quads_px=quad_pixels(settings, rec.width, rec.height))
    if not settings.master_on_left:
        left, right = right, left
    return phase_offset(rec.timestamps_ms, left, right), left, right


def analyse(rec: Recording, settings: CameraSettings = SETTINGS) -> PhaseResult:
    return analyse_traces(rec, settings)[0]


# ── The rig check ────────────────────────────────────────────────────────────

RGB_PHASE_TEST_NAME = "RGB flash-cue phase between halves (camera)"
# One frame at 58 fps is 17.2 ms; a synced cue should land within two frames.
PHASE_TOLERANCE_MS = 35.0
MIN_SWING = 3.0          # luma units; below this the camera saw no breathing
MIN_CORRELATION = 0.6
MIN_PERIODICITY = 0.6    # each trace must repeat after one cue period


def judge(res: PhaseResult) -> dict:
    """The result record for one phase measurement.

    Report-only for now: an offset above the tolerance is ``xfail`` (the known
    unsynced flash cue, keys_com/rgb.c), not ``fail``, so the suite stays green
    until the firmware fix lands. A measurement the camera could not make is a
    skip with the reason.
    """
    info = (f"offset {res.offset_ms:+.1f} ms, correlation {res.correlation:.2f}, "
            f"swing L {res.swing_left:.1f} / R {res.swing_right:.1f}, "
            f"periodicity L {res.periodicity_left:.2f} / R {res.periodicity_right:.2f}, "
            f"LED steps L {res.step_left_ms:.0f} / R {res.step_right_ms:.0f} ms, "
            f"{res.frames} frames at {res.fps:.0f} fps")
    rec = {"name": RGB_PHASE_TEST_NAME, "info": info, "offset_ms": round(res.offset_ms, 1)}
    if min(res.swing_left, res.swing_right) < MIN_SWING:
        return {**rec, "status": "skip",
                "reason": "the camera saw no breathing on one half — check framing, "
                          "split_x and exposure in the UI camera view (" + info + ")"}
    if min(res.periodicity_left, res.periodicity_right) < MIN_PERIODICITY:
        return {**rec, "status": "skip",
                "reason": "a half does not repeat with the 2048 ms flash-cue period, so "
                          "the camera saw another brightness change, not the cue (" + info + ")"}
    if res.correlation < MIN_CORRELATION:
        return {**rec, "status": "skip",
                "reason": f"traces do not match well enough to measure ({info})"}
    if abs(res.offset_ms) <= PHASE_TOLERANCE_MS:
        return {**rec, "status": "pass"}
    return {**rec, "status": "xfail",
            "reason": "flash cue phase differs between halves (keys_com/rgb.c uses "
                      "timer_read32(), not sync_timer_read32()) — " + info}


# ── The last measurement, for the UI ─────────────────────────────────────────
# The CI runner is a separate process from the UI, so the runner writes its last
# result here and the UI's Camera tab reads it. var/ is gitignored, and the CI
# force-sync (git checkout -f) leaves untracked files in place.

LAST_RESULT_PATH = Path(__file__).resolve().parent.parent / "var" / "camera_phase_last.json"
CHART_POINTS = 300   # samples per trace kept for the chart


def _downsample(ts: list[float], values: list[float], n: int = CHART_POINTS):
    if len(ts) <= n:
        idx = range(len(ts))
    else:
        idx = [round(i * (len(ts) - 1) / (n - 1)) for i in range(n)]
    t0 = ts[0] if ts else 0.0
    return [round(ts[i] - t0, 1) for i in idx], [round(values[i], 2) for i in idx]


HISTORY_PATH = LAST_RESULT_PATH.with_name("camera_phase_history.json")
HISTORY_LEN = 10   # measurements kept to compare builds (e.g. a release against a fix)
_HISTORY_KEYS = ("time", "source", "fw", "commit", "ref", "status", "offset_ms", "reason", "info")


def _append_history(entry: dict, path: Path) -> None:
    """Append one summary (no traces) and keep the newest HISTORY_LEN.

    The CI runner and the UI are separate processes, so the read-modify-write
    runs under an flock on a lock file next to the history.
    """
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            hist = json.loads(path.read_text())
            if not isinstance(hist, list):
                hist = []
        except (OSError, ValueError):
            hist = []
        hist.append({k: entry[k] for k in _HISTORY_KEYS if k in entry})
        hist = hist[-HISTORY_LEN:]
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(hist))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def load_history(path: Path | None = None) -> list:
    """Newest first."""
    path = LAST_RESULT_PATH.with_name(HISTORY_PATH.name) if path is None else path
    try:
        hist = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    return list(reversed(hist)) if isinstance(hist, list) else []


def image_fw(bin_path: str | None) -> str | None:
    """The firmware version in an image's GET_ID literal, or None."""
    if not bin_path:
        return None
    from .fw_update import caps_from_image   # needs hid; keep the import lazy
    try:
        with open(bin_path, "rb") as f:
            caps = caps_from_image(f.read())
    except OSError:
        return None
    return caps["fw"] if caps else None


def save_last(record: dict, source: str, ts: list[float] | None = None,
              master: list[float] | None = None, slave: list[float] | None = None,
              path: Path | None = None, fw: str | None = None,
              history_path: Path | None = None) -> dict:
    """Write the result record (and the traces, when there are any) atomically,
    append its summary to the history, and return what was written (also when
    a write failed).

    ``fw`` is the firmware version of the staged image. A CI run also records
    the commit and branch from the Actions environment.

    Never raises: the UI view is a convenience, and a write failure must not
    change a test result.
    """
    path = LAST_RESULT_PATH if path is None else path
    # The history always sits next to the result file (tests point both at a temp dir).
    history_path = history_path or path.with_name(HISTORY_PATH.name)
    data = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": source, **record}
    if fw:
        data["fw"] = fw
    if source == "ci" and os.environ.get("GITHUB_SHA"):
        data["commit"] = os.environ["GITHUB_SHA"][:7]
        ref = os.environ.get("GITHUB_HEAD_REF") or os.environ.get("GITHUB_REF_NAME")
        if ref:
            data["ref"] = ref
    if ts and master and slave:
        t, m = _downsample(ts, master)
        _, sl = _downsample(ts, slave)
        data["traces"] = {"t_ms": t, "master": m, "slave": sl}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A unique temp file per writer: the CI runner and the UI are separate
        # processes and can finish at the same moment.
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(data))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        logging.getLogger("polykybd-ctnd").warning("could not save the camera result: %s", exc)
    try:
        _append_history(data, history_path)
    except OSError as exc:
        logging.getLogger("polykybd-ctnd").warning("could not append the camera history: %s", exc)
    return data


def load_last(path: Path | None = None) -> dict | None:
    path = LAST_RESULT_PATH if path is None else path
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def measure_now(bin_path: str, log, settings: CameraSettings = SETTINGS,
                prepare=None) -> dict:
    """The UI's "Measure phase now": stage ``bin_path`` over HID (which shows the
    flash cue), record meanwhile, judge, save and return the saved record
    (time, source, traces included).

    ``prepare`` runs once the camera is known to be available, before the
    staging (the UI resets both halves with a known stagger there).

    Non-destructive like the CI step: stage + verify only, no APPLY. The image
    must be the build the rig runs (stage_and_verify checks the version).
    """
    from .fw_update import stage_and_verify   # needs hid; keep the import lazy

    ok, why = availability(settings)
    if not ok:
        # Saved like any other outcome, so the panel never keeps showing an
        # older measurement after this one could not run.
        return save_last({"name": RGB_PHASE_TEST_NAME, "status": "skip", "reason": why}, "ui",
                         fw=image_fw(bin_path))
    # One copy for the whole measurement: a view saved meanwhile (Adjust view
    # changes SETTINGS in place) must not split frames captured with the old crop.
    settings = snapshot_settings(settings)
    if prepare is not None:
        prepare()
    rec = BackgroundRecording(settings)
    fails = []   # the staging's FAIL lines, for the saved reason

    def _log(line):
        if "FAIL:" in line:
            fails.append(line.split("FAIL:", 1)[1].strip())
        log(line)

    staged = stage_and_verify(bin_path, _log, on_begin=rec.start, require_identity=True)
    if not staged and not rec.started:
        # BEGIN never succeeded, so the camera never ran: report the staging.
        why = fails[0] if fails else "see the log"
        return save_last({"name": RGB_PHASE_TEST_NAME, "status": "skip",
                          "reason": f"the HID staging failed before the flash cue: {why}"},
                         "ui", fw=image_fw(bin_path))
    try:
        recording = rec.result()
        res, master, slave = analyse_traces(recording, settings)
    except Exception as exc:
        why = f"camera recording failed: {exc}"
        if not staged:
            why += " (the HID staging failed too, see the log)"
        return save_last({"name": RGB_PHASE_TEST_NAME, "status": "skip", "reason": why}, "ui",
                         fw=image_fw(bin_path))
    if not staged:
        # The recording may be fine, but the staging it belongs to was refused
        # or broke off (version mismatch, transfer, COMMIT). Report that, with
        # the traces for reference, instead of a pass or xfail.
        return save_last({"name": RGB_PHASE_TEST_NAME, "status": "skip",
                          "reason": "the HID staging failed (see the log), so this "
                                    "measurement is not used: " + judge(res).get("info", "")},
                         "ui", recording.timestamps_ms, master, slave, fw=image_fw(bin_path))
    return save_last(judge(res), "ui", recording.timestamps_ms, master, slave,
                     fw=image_fw(bin_path))
