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
import math
import os
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field

from .config import CAMERA

# The flash cue's period: 256 phase steps of 8 ms (timer_read32() >> 3, as uint8).
FLASH_CUE_PERIOD_MS = 2048.0

_lock = threading.Lock()   # one capture at a time inside this process


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
    framerate: int = 90
    mode: str = "640:480:10:P"     # OV5647 90 fps sensor mode; "" lets rpicam choose
    shutter_us: int = 8000         # fixed exposure, below one frame at 90 fps
    gain: float = 4.0
    awb_gains: str = "1.5,1.5"     # fixed red,blue gains: no auto white balance
    record_s: float = 8.0

    @classmethod
    def from_config(cls, c: dict | None) -> "CameraSettings":
        s = cls()
        for k, v in (c or {}).items():
            if not hasattr(s, k) or v is None:
                continue
            default = getattr(s, k)
            if isinstance(default, bool):
                v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
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
        if not 0.05 < s.split_x < 0.95:
            raise ValueError(f"camera.split_x must be between 0.05 and 0.95, not {s.split_x}")
        return s

    def orient_args(self) -> list[str]:
        args = []
        if self.rotation:
            args += ["--rotation", str(self.rotation)]
        if self.hflip:
            args.append("--hflip")
        if self.vflip:
            args.append("--vflip")
        return args


SETTINGS = CameraSettings.from_config(CAMERA)


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    # Audit: argv is a list (no shell). argv[0] is a fixed rpicam-apps tool name;
    # every other element comes from CameraSettings, which config.yaml fills and
    # from_config() type-checks. No request data reaches this call.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.run(argv, capture_output=True, timeout=timeout, check=False)


def availability(settings: CameraSettings = SETTINGS) -> tuple[bool, str]:
    """(usable, reason). Never raises: a rig without a camera SKIPs."""
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
                  width: int = 1296, height: int = 972) -> bytes:
    """One JPEG with the configured rotation and flips, for the UI's camera view.

    Raises RuntimeError when the camera is busy (a CI capture holds it) or absent.
    """
    if not _lock.acquire(blocking=False):
        raise RuntimeError("camera busy (a capture is running)")
    try:
        r = _run(["rpicam-still", "-n", "-t", "300", "--immediate",
                  "--width", str(width), "--height", str(height),
                  "-e", "jpg", "-o", "-", *settings.orient_args()], timeout=20)
        if r.returncode != 0 or not r.stdout.startswith(b"\xff\xd8"):
            err = r.stderr.decode(errors="replace").strip().splitlines()
            raise RuntimeError(err[-1] if err else f"rpicam-still exit {r.returncode}")
        return r.stdout
    finally:
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


def record(settings: CameraSettings = SETTINGS, seconds: float | None = None) -> Recording:
    """Record raw frames to RAM (/dev/shm) and return their Y planes and timestamps."""
    seconds = settings.record_s if seconds is None else seconds
    if not _lock.acquire(timeout=30):
        raise RuntimeError("camera busy (a snapshot did not finish)")
    shm = "/dev/shm" if os.path.isdir("/dev/shm") else None
    try:
        with tempfile.TemporaryDirectory(prefix="polykybd-cam-", dir=shm) as tmp:
            raw, pts = os.path.join(tmp, "v.yuv"), os.path.join(tmp, "pts.txt")
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
        _lock.release()
    n = min(len(lumas), len(ts))
    return Recording(settings.width, settings.height, ts[:n], lumas[:n])


class BackgroundRecording:
    """Run record() in a thread: start() when the cue begins, result() afterwards."""

    def __init__(self, settings: CameraSettings = SETTINGS, seconds: float | None = None):
        self._settings, self._seconds = settings, seconds
        self._thread = None
        self._rec = None
        self._err = None

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

def side_traces(rec: Recording, split_x: float, step: int = 4,
                keep: float = 0.25) -> tuple[list[float], list[float]]:
    """One brightness trace per image side (left of split_x, right of it).

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


def phase_offset(ts: list[float], left: list[float], right: list[float],
                 period_ms: float = FLASH_CUE_PERIOD_MS, dt: float = 4.0) -> PhaseResult:
    """Circular cross-correlation of two traces over lags within one period."""
    a = resample(ts, left, dt)
    b = resample(ts, right, dt)
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    a = [x - ma for x in a]
    b = [x - mb for x in b]
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    half = int(period_ms / dt / 2)

    def corr(k: int) -> float:
        # Positive k: b is a delayed copy of a, so compare a[i] with b[i + k].
        if k >= 0:
            s = sum(a[i] * b[i + k] for i in range(n - k))
        else:
            s = sum(a[i - k] * b[i] for i in range(n + k))
        return s / (na * nb) * n / (n - abs(k))

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
                       frames=len(ts), fps=fps)


def analyse(rec: Recording, settings: CameraSettings = SETTINGS) -> PhaseResult:
    left, right = side_traces(rec, settings.split_x)
    if not settings.master_on_left:
        left, right = right, left
    return phase_offset(rec.timestamps_ms, left, right)


# ── The rig check ────────────────────────────────────────────────────────────

RGB_PHASE_TEST_NAME = "RGB flash-cue phase between halves (camera)"
# One frame at 90 fps is 11 ms; a synced cue should land within two frames.
PHASE_TOLERANCE_MS = 25.0
MIN_SWING = 3.0          # luma units; below this the camera saw no breathing
MIN_CORRELATION = 0.6


def judge(res: PhaseResult) -> dict:
    """The result record for one phase measurement.

    Report-only for now: an offset above the tolerance is ``xfail`` (the known
    unsynced flash cue, keys_com/rgb.c), not ``fail``, so the suite stays green
    until the firmware fix lands. A measurement the camera could not make is a
    skip with the reason.
    """
    info = (f"offset {res.offset_ms:+.1f} ms, correlation {res.correlation:.2f}, "
            f"swing L {res.swing_left:.1f} / R {res.swing_right:.1f}, "
            f"{res.frames} frames at {res.fps:.0f} fps")
    rec = {"name": RGB_PHASE_TEST_NAME, "info": info, "offset_ms": round(res.offset_ms, 1)}
    if min(res.swing_left, res.swing_right) < MIN_SWING:
        return {**rec, "status": "skip",
                "reason": "the camera saw no breathing on one half — check framing, "
                          "split_x and exposure in the UI camera view (" + info + ")"}
    if res.correlation < MIN_CORRELATION:
        return {**rec, "status": "skip",
                "reason": f"traces do not match well enough to measure ({info})"}
    if abs(res.offset_ms) <= PHASE_TOLERANCE_MS:
        return {**rec, "status": "pass"}
    return {**rec, "status": "xfail",
            "reason": "flash cue phase differs between halves (keys_com/rgb.c uses "
                      "timer_read32(), not sync_timer_read32()) — " + info}
