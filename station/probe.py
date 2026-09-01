# SPDX-License-Identifier: GPL-2.0-only
"""Load an ad-hoc HIL *probe* committed on the firmware branch.

The graded suite in :mod:`station.hil_tests` answers fixed questions. A probe
answers a one-off one — "flash this build, send these commands, and show me what
the firmware printed" — so a firmware bug can be chased on the rig without a
human flashing a ``.bin`` and pasting a console log back.

A probe is an ordinary Python file living in the FIRMWARE repo under
``keyboards/polykybd/tools/hil_probes/``, next to the change it is investigating,
so the probe and the firmware it probes are one commit and one branch. It
defines::

    NAME = "what this is looking for"      # optional, defaults to the filename
    MIN_PROTOCOL = 15                      # optional, same gate keys as a test
    NEEDS_CONSOLE = True                   # optional

    def probe(raw, log):
        reply = raw.send(bytes([0x50, 0x06]))
        log(f"GET_ID -> {reply!r}")
        return reply is not None

:func:`load_probe` turns that into the same test dict the suite uses, so a probe
inherits the whole runner for free: the per-side flash, the readiness gates, the
console tap (its ``[qmk]`` lines land in the run log), the capability gates and
the pass/fail reporting. That reuse is the point — a parallel code path would
drift from the suite's hard-won startup sequencing.

⚠️ **The containment check below is not a security boundary, and must not be
described as one.** By the time the runner executes, CI has already checked out
the whole firmware repo and the build jobs have already run code from it, so a
branch that wanted to run something else on the rig never needed a probe to do
it. What stops an untrusted branch reaching the rig is the workflow's fork gate
(see ``qmk-test.yml``; HIL-2 in ``docs/SECURITY_AUDIT.md``). The check here is
operational: it keeps "what can be a probe" explicit and stops a mistyped path
importing some unrelated module for its side effects.
"""

import importlib.util
import os
from typing import Callable, Optional

# Where a probe must live, relative to the firmware checkout root.
PROBE_SUBDIR = os.path.join("keyboards", "polykybd", "tools", "hil_probes")


class ProbeError(Exception):
    """A probe could not be located or does not present the expected shape."""


def probe_root(firmware_dir: str) -> str:
    """Absolute path of the directory probes are read from."""
    return os.path.realpath(os.path.join(firmware_dir, PROBE_SUBDIR))


def resolve_probe(name: str, firmware_dir: str) -> str:
    """Resolve ``name`` to a probe file inside ``firmware_dir``.

    ``name`` may be a bare probe name (``fw_apply``), a filename
    (``fw_apply.py``) or a path relative to the checkout. It is resolved against
    :func:`probe_root` and must land inside it — a ``..`` traversal, an absolute
    path elsewhere, or a symlink pointing out is refused (see the module note on
    what that check is and is not for).
    """
    if not name:
        raise ProbeError("no probe name given")
    root = probe_root(firmware_dir)
    if not os.path.isdir(root):
        raise ProbeError(f"no probe directory at {root} — is --firmware-dir right?")

    candidate = name if name.endswith(".py") else name + ".py"
    # A path relative to the checkout root is accepted too, so a caller can paste
    # the path as it appears in the repo.
    if os.path.isabs(candidate):
        target = candidate
    elif candidate.replace("\\", "/").startswith(PROBE_SUBDIR.replace("\\", "/")):
        target = os.path.join(firmware_dir, candidate)
    else:
        target = os.path.join(root, candidate)

    # realpath BEFORE the containment test, so a symlink cannot escape it.
    target = os.path.realpath(target)
    if target != root and not target.startswith(root + os.sep):
        raise ProbeError(f"probe {name!r} resolves outside {root} — refusing")
    if not os.path.isfile(target):
        avail = sorted(f[:-3] for f in os.listdir(root)
                       if f.endswith(".py") and not f.startswith("_"))
        raise ProbeError(f"no probe {name!r} in {root} "
                         f"(available: {', '.join(avail) or 'none'})")
    return target


def load_probe(path: str, log: Optional[Callable[[str], None]] = None) -> dict:
    """Import the probe at ``path`` and return it as a suite test dict.

    Raises :class:`ProbeError` when the module has no ``probe`` callable, rather
    than letting the runner start a flash for a probe that cannot run.
    """
    stem = os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(f"hil_probe_{stem}", path)
    if spec is None or spec.loader is None:
        raise ProbeError(f"cannot import a probe from {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
        raise ProbeError(f"probe {stem!r} failed to import: {exc}") from exc

    fn = getattr(module, "probe", None)
    if not callable(fn):
        raise ProbeError(f"probe {stem!r} defines no probe(raw, log) function")

    test = {"name": getattr(module, "NAME", None) or f"probe: {stem}", "fn": fn}
    # Reuse the suite's own gate keys, so a probe for an unreleased command SKIPs
    # on an older board instead of failing and reading as a firmware fault.
    for attr, key in (("MIN_PROTOCOL", "min_protocol"),
                      ("MIN_FW", "min_fw"),
                      ("NEEDS_CONSOLE", "needs_console"),
                      ("XFAIL", "xfail")):
        value = getattr(module, attr, None)
        if value is not None:
            test[key] = value
    if log:
        log(f"[runner] loaded probe {stem!r} from {path}")
    return test
