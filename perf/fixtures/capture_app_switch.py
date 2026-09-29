# SPDX-License-Identifier: GPL-2.0-only
"""Record the exact HID report stream PolyKybdHost sends for an app switch.

The perf harness replays these streams on the rig (``station.perf``
``measure_app_switch``). Recording them from the host's own
``PolyKybd.send_overlays_mru`` means the replay uses the host's encoder choice,
its mapping packing and its rate-limit pauses, rather than a hand-built copy
that could drift from what users actually send.

Two phases are recorded per overlay set, against one fresh ``OverlayMRUCache``:

* ``cold`` — the first switch into the app: every image is uploaded.
* ``warm`` — the same switch again: every image is a cache hit, so only the
  prepare, the mapping and the enable go out. This is also the lower bound
  for a switch whose images come from a flash icon library.

Run from anywhere, pointing at a PolyKybdHost checkout whose dependencies are
installed (the ``hid`` module needs ``libhidapi-hidraw0``)::

    python perf/fixtures/capture_app_switch.py --host-dir ../PolyKybdHost \\
        word_template jetbrains_template

Each stem becomes ``perf/fixtures/app_switch_<name>.json``, where ``<name>`` is
the stem without ``_template``.
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


class _RecordingHid:
    """Stands in for HidHelper and records every report the host would send."""

    def __init__(self):
        self.ops = []

    def send_multiple(self, data):
        self.ops.append({"op": "write", "hex": bytes(data).hex()})
        return True, 64

    def send_and_read_validate(self, data, timeout=100, expected_prefix=None):
        self.ops.append({"op": "request", "hex": bytes(data).hex()})
        return True, bytearray(b"P\x0b.")


class _SleepRecordingTime:
    """Replaces poly_kybd's module-level ``time`` reference only.

    Every attribute is the real ``time`` module's except ``sleep``, which records
    the host's rate-limit pause instead of waiting. Assigning to ``time.sleep``
    itself would change the shared standard-library module for the whole process."""

    def __init__(self, record):
        self._record = record

    def sleep(self, seconds):
        self._record(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


class _Settings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _file_order(path):
    """Primary sheet first, then the combo/extra/gui sheets, like the mapping lists them."""
    name = os.path.basename(path)
    primary = name.endswith(".mods.png") and name.count(".") == 2 or name.count(".") == 1
    return (0 if primary else 1, name)


def capture(host_dir: str, stem: str) -> dict:
    sys.path.insert(0, host_dir)
    import polyhost.device.poly_kybd as pk
    from polyhost.device.device_settings import DeviceSettings
    from polyhost.device.overlay_cache import OverlayMRUCache

    files = sorted(glob.glob(os.path.join(host_dir, "polyhost", "res", "overlays", stem + ".*png")),
                   key=_file_order)
    if not files:
        raise SystemExit(f"no overlay PNGs for stem {stem!r}")

    settings = _Settings(max_hid_message_before_delay=15, delay_time_after_max_hid_messages=0.3)
    kb = pk.PolyKybd(DeviceSettings(), settings)
    kb.protocol_version = 18
    rec = _RecordingHid()
    kb.hid = rec
    real_time = pk.time
    pk.time = _SleepRecordingTime(lambda s: rec.ops.append({"op": "pause", "s": s}))
    try:
        cache = OverlayMRUCache(600)
        phases = {}
        for phase in ("cold", "warm"):
            rec.ops = []
            if not kb.send_overlays_mru(files, cache):
                raise SystemExit(f"send_overlays_mru failed for {stem} ({phase})")
            phases[phase] = rec.ops
    finally:
        pk.time = real_time

    commit = subprocess.run(["git", "-C", host_dir, "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    return {
        "stem": stem,
        "host_commit": commit,
        "protocol": 18,
        "files": [os.path.basename(f) for f in files],
        "cold": phases["cold"],
        "warm": phases["warm"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host-dir", required=True, help="PolyKybdHost checkout")
    ap.add_argument("stems", nargs="+", help="overlay stems, e.g. word_template")
    args = ap.parse_args(argv)
    host_dir = os.path.abspath(args.host_dir)
    for stem in args.stems:
        fixture = capture(host_dir, stem)
        name = stem.removesuffix("_template")
        out = os.path.join(HERE, f"app_switch_{name}.json")
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=1)
            fh.write("\n")
        count = {p: sum(o["op"] != "pause" for o in fixture[p]) for p in ("cold", "warm")}
        pauses = sum(o["op"] == "pause" for o in fixture["cold"])
        print(f"{out}: cold {count['cold']} reports, {pauses} pauses; warm {count['warm']} reports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
