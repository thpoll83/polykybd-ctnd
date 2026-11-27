"""Watch both status displays with the camera during a HIL run.

Every STEP_S a short clip; each half's status display is classed 'text' (the
status page), 'idle' (the idle animation) or 'dark' (camera.display_state). Every
change is logged with the test that was running, and a sample where the halves
differ is flagged: the master decides idle and copies it to the slave, so the two
halves should always show the same. Reports only; it never grades the run.

Checks that use the camera themselves wrap their recording in ``paused()``, and
the runner pauses the watch while it records the flash cue.
"""
import contextlib
import threading
import time
from typing import Callable

STEP_S = 10.0
SIDES = ("left", "right")

_active = None   # the running watch, for paused()


def classify(levels: dict, state_of: Callable[[int], str]) -> dict:
    """{side: state} from {side: (led, display p95)}."""
    return {side: state_of(levels[side][1]) for side in SIDES}


class StatusWatch:
    """``sample()`` returns {side: (led mean, display p95)} from one clip, or
    raises; ``state_of`` classes a p95 (camera.display_state)."""

    def __init__(self, sample: Callable[[], dict], state_of: Callable[[int], str],
                 log: Callable[[str], None], step_s: float = STEP_S,
                 clock: Callable[[], float] = time.monotonic):
        self._sample, self._state_of, self._log = sample, state_of, log
        self._step, self._clock = step_s, clock
        self._stop = threading.Event()
        self._busy = threading.Lock()   # held while a clip records
        self._thread = None
        self._t0 = None
        self._last = None
        self.phase = "start"
        self.timeline = []   # (seconds, phase, {side: state}, {side: p95})
        self.mismatches = []
        self._off = threading.Event()   # stop_sampling(): no more clips this run

    def start(self) -> None:
        global _active
        self._t0 = self._clock()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _active = self

    def stop(self) -> None:
        global _active
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._step + 30)
        if _active is self:
            _active = None

    @contextlib.contextmanager
    def paused(self):
        """No clip records while the block runs (it waits for one in progress)."""
        with self._busy:
            yield

    def _run(self) -> None:
        while not self._stop.is_set():
            self.step()
            self._stop.wait(self._step)

    def stop_sampling(self) -> None:
        """Take no more samples for the rest of the run."""
        self._off.set()

    def step(self) -> dict | None:
        """One sample: record, class, log the changes. None when the camera failed
        or sampling was stopped."""
        if self._off.is_set():
            return None
        with self._busy:
            if self._off.is_set():   # stopped while this step waited for the lock
                return None
            phase = self.phase   # the test running while the clip records
            try:
                levels = self._sample()
            except Exception as exc:   # a busy or failed camera skips one sample
                self._log(f"[status] camera: {exc}")
                return None
        t = self._clock() - self._t0
        states = classify(levels, self._state_of)
        p95 = {side: levels[side][1] for side in SIDES}
        self.timeline.append((round(t, 1), phase, states, p95))
        if states != self._last:
            self._log(f"[status] +{t:4.0f} s ({phase}): "
                      + ", ".join(f"{s} {states[s]} ({p95[s]})" for s in SIDES))
            if states["left"] != states["right"]:
                self.mismatches.append((round(t, 1), phase, states))
                self._log(f"[status] WARNING: the halves differ during {phase}: "
                          f"left {states['left']}, right {states['right']}")
        self._last = states
        return states

    def summary(self) -> list[str]:
        """Lines for the end of the run."""
        if not self.timeline:
            return ["[status] no camera samples"]
        lines = [f"[status] {len(self.timeline)} samples of both status displays"]
        if self.mismatches:
            for t, phase, st in self.mismatches:
                lines.append(f"[status]   halves differed at +{t:.0f} s during {phase}: "
                             f"left {st['left']}, right {st['right']}")
        else:
            lines.append("[status]   the two halves always showed the same")
        return lines


@contextlib.contextmanager
def paused():
    """Pause the running watch, if any, for a check that uses the camera."""
    watch = _active
    if watch is None:
        yield
    else:
        with watch.paused():
            yield


def stop_sampling() -> None:
    """Stop the running watch, if any, from taking more samples this run."""
    watch = _active
    if watch is not None:
        watch.stop_sampling()


def camera_sampler(camera, label: str = "status watch") -> Callable[[], dict]:
    """A sample() for StatusWatch: one 0.3 s clip, both halves' levels."""
    s = camera.snapshot_settings(camera.SETTINGS)
    boards = {side: camera.load_board(side) for side in SIDES}

    def sample() -> dict:
        rec = camera.record(s, seconds=0.3, label={"text": label}, keep_frames=False)
        luma = rec.lumas[len(rec.lumas) // 2]
        return {side: camera.half_levels(luma, rec.width, rec.height, s, side, boards[side])
                for side in SIDES}
    return sample
