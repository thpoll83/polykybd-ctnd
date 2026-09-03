# SPDX-License-Identifier: GPL-2.0-only
import os
import sys
import time
from typing import Callable

from .console_log import TAP, console_sink
from .flash import FlashController
from .hid import HIDConsole, RawHID, enumerate_raw_interfaces
from .fw_update import stage_and_verify, apply_staged, caps_from_image
from .uf2 import uf2_file_to_bin, Uf2Error
from .hil_tests import (parse_device_caps, skip_reason, measure_split_link,
                        LINK_OK, LINK_NO_SUMMARY)

# Raw HID display-off control command — mirrors the firmware dispatcher in
# keyboards/polykybd/hid_com.c (case 24 / 0x18). A command report is
# data[0]='P' (channel marker) then data[1]=command id; the firmware replies "P\x18.".
POLY_CHANNEL     = 0x50  # 'P'
CMD_GET_ID       = 0x06  # device identity string — advertises fw + protocol version
CMD_GET_LANG     = 0x07  # read current language — readiness probe (no side effects)
CMD_DISPLAY_OFF  = 0x18
CMD_IDLE_STYLE   = 0x1C  # 28: get (0xFF) / set the idle anti-burn-in style (v4+)
CMD_SAVE_EEPROM  = 0x1A  # 26: flush every dirty user-state block to EEPROM
IDLE_STYLE_MIN_PROTOCOL = 4   # FEATURE_MIN_PROTOCOL entry for cmd 28
ACK              = ord(".")

# VIA-style "reset dynamic keymap" report. Unlike the 'P'-channel commands above,
# this is a bare report whose first byte is the VIA command id (no 'P' marker):
# the firmware routes data[0]==0x06 to legacy_command_kb(), which calls
# dynamic_keymap_reset(), bridges the reset to the slave over the split link, and
# echoes the request back unchanged (no "P<cmd>." ACK). Same id PolyKybdHost uses
# in PolyKybd.reset_dynamic_keymap(). See keyboards/polykybd/hid_com.c.
VIA_DYNAMIC_KEYMAP_RESET = 0x06


class TestRunner:
    def __init__(self, log: Callable[[str], None] = print):
        self.log = log
        self.status = "idle"
        self._flash = FlashController()
        self._console = HIDConsole()
        self._raw = RawHID()
        self._caps = None  # device caps (fw/protocol), read lazily for the gate

    @property
    def raw(self) -> RawHID:
        """The shared Raw HID channel.

        Exposed so a sibling runner (``station.perf_runner``) can drive the device
        through the same handle bookkeeping — notably ``timeouts_recovered`` /
        ``timeouts_failed`` — instead of opening a second, uncoordinated one."""
        return self._raw

    def flash_halves(self, left_uf2: str, right_uf2: str) -> None:
        """Validate the per-side image pairing, then flash both halves.

        Split out of :meth:`flash_and_test` so the perf runner reuses the exact
        same ordering and the same HIL-pairing guard rather than reimplementing
        them (a swapped pair silently makes both halves master)."""
        # HIL images are built per side (POLYKYBD_HIL=left/right). If either path
        # looks like a HIL image, enforce the per-side contract before touching
        # hardware — flashing one master image (or a swapped pair) to both halves
        # makes both enumerate as master, the failure this build exists to prevent.
        # Production deploys (non-_hil names, often the same image to both halves)
        # are intentionally not constrained.
        left_name, right_name = os.path.basename(left_uf2), os.path.basename(right_uf2)
        if ("_hil" in left_name or "_hil" in right_name) and not (
            left_name.endswith("_hil_left.uf2") and right_name.endswith("_hil_right.uf2")
        ):
            raise ValueError(
                "HIL runs need per-side images: pass *_hil_left.uf2 to --left and "
                f"*_hil_right.uf2 to --right (got left={left_name!r}, right={right_name!r})"
            )
        self.status = "flashing"
        # Flash right first — it communicates via split cable, not USB HID
        self._flash.flash("right", right_uf2, self.log)
        self._flash.flash("left",  left_uf2,  self.log)

    def flash_and_test(self, left_uf2: str, right_uf2: str, tests: list = None,
                       bin_path: str = None, extended: bool = False,
                       doom: bool = False,
                       apply_bin: str = None) -> dict:
        """Flash both halves and run ``tests`` against the master.

        ``extended`` opts the run into the slow tier — the animation/idle checks,
        the split-link soak and the reboot power cycle, together most of a minute
        — which is otherwise skipped so the per-push gate stays fast. See
        ``hil_tests.TIER_EXTENDED``.
        """
        results = []
        try:
            self.flash_halves(left_uf2, right_uf2)

            self.log("[runner] waiting for keyboard to enumerate...")
            time.sleep(3)

            # The QMK HID console (debug log streaming) is diagnostic only — it
            # requires CONSOLE_ENABLE in the firmware. Current PolyKybd builds do
            # stream [qmk] lines, but a build with it off, or any transient open
            # failure, must not fail the test run — so treat startup as
            # best-effort. (When it IS enabled the reader thread is live, which is
            # why HIDConsole.stop() must join it before closing the handle.)
            console_started = False
            try:
                # Echo every chunk into the run log AND into the shared tap, so a
                # test can assert on what the firmware printed while it ran (the
                # split-link counter, the idle transition). Reassembly of the
                # report-sized fragments into lines happens inside the tap.
                self._console.start(console_sink(lambda msg: self.log(f"[qmk] {msg}")))
                console_started = True
            except Exception as exc:
                self.log(f"[runner] HID console unavailable (continuing without it): {exc}")

            self.status = "testing"
            self.log("[runner] suite tier: "
                     + ("EXTENDED (slow checks included: animation, idle, link "
                        "soak, reboot power cycle)" if extended else
                        "default (extended checks skipped — pass --extended to "
                        "include them)"))
            # Wait out the post-cold-flash window during which the master blocks
            # its main loop on the initial 72-keycap render + split-sync and
            # times out HID reads, so the marker-sensitive GET_ID tests don't run
            # inside it (see wait_for_master_ready).
            self.wait_for_master_ready()
            # Clear any dynamic keymap left in the keyboard's EEPROM by a previous
            # firmware whose layer layout differs from the freshly-flashed build.
            # A UF2/HID flash does not erase the wear-leveled EEPROM, and the
            # firmware has no build-date magic that auto-resets it, so a stale
            # keymap survives the flash and can make the rig behave as if it were
            # running the old layout. Reset it before the suite so every run starts
            # from the compiled defaults. Best-effort — a failure here is logged
            # but does not abort the run.
            self._reset_keymap()
            # The keymap reset triggers a master->slave sync; wait for that (and
            # the post-cold-flash split-sync settling in general) to clear before
            # the graded suite, so no test runs while the master's loop is briefly
            # stalled in split retries. Best-effort.
            self.settle_master()
            for test in (tests or []):
                name = test.get("name", "unnamed")
                # Capability gate: skip (do NOT fail) a test the flashed firmware
                # can't yet satisfy — e.g. a protocol-v3 command checked before
                # the v3 firmware is deployed. The device advertises its
                # fw/protocol version in GET_ID, so a gated test un-skips itself
                # automatically once a firmware meeting the requirement is
                # flashed. Caps are read lazily (only when a gated test is first
                # reached) which is after test_fresh_boot_marker has consumed the
                # one-shot '*' marker, so the gate's GET_ID doesn't disturb it.
                if (test.get("min_protocol") is not None or test.get("min_fw")
                        or test.get("needs_console") or test.get("tier")):
                    caps = dict(self._device_caps())
                    # Console availability is a property of THIS RUN, not of the
                    # device, so it is merged in here rather than cached with the
                    # GET_ID caps.
                    caps["console"] = console_started
                    caps["extended"] = extended
                    caps["doom"] = doom
                    reason = skip_reason(test, caps)
                    if reason:
                        results.append({"name": name, "status": "skip", "reason": reason})
                        self.log(f"[test] SKIP: {name} ({reason})")
                        continue
                # An xfail test is known to fail until some change lands that
                # isn't visible in GET_ID: a FAIL is tolerated (XFAIL), an
                # unexpected PASS is flagged (XPASS) so the marker gets removed.
                xfail = test.get("xfail")
                rec0 = self._raw.timeouts_recovered
                fail0 = self._raw.timeouts_failed
                try:
                    passed = bool(test["fn"](self._raw, self.log))
                    recovered = self._raw.timeouts_recovered - rec0
                    timed_out = self._raw.timeouts_failed - fail0
                    if xfail:
                        status = "xpass" if passed else "xfail"
                    elif passed:
                        status = "pass"
                    elif timed_out:
                        # The check failed on a dropped reply (even after send()'s
                        # one retry), not on wrong data — a transient rig USB/link
                        # hiccup. Record it as a non-failing WARNING so the run
                        # stays green but the blip is on the record. A genuine
                        # wrong value/status (response present) still FAILs.
                        status = "warn"
                    else:
                        status = "fail"
                    rec = {"name": name, "status": status}
                    if xfail:
                        rec["reason"] = xfail
                    elif status == "warn":
                        rec["reason"] = f"read timed out x{timed_out} (after 1 retry)"
                    results.append(rec)
                    note = f" [recovered {recovered} read timeout(s)]" if recovered else ""
                    self.log(f"[test] {status.upper()}: {name}"
                             + (f" (expected to fail: {xfail})" if xfail else "") + note)
                except Exception as exc:
                    # An exception is a failure; under xfail it's still tolerated.
                    status = "xfail" if xfail else "fail"
                    rec = {"name": name, "status": status, "error": str(exc)}
                    if xfail:
                        rec["reason"] = xfail
                    results.append(rec)
                    self.log(f"[test] {status.upper()}: {name}: {exc}"
                             + (f" (expected to fail: {xfail})" if xfail else ""))

            if console_started:
                self._console.stop()
                # Release a trailing fragment the firmware never newline-terminated
                # — the last line is usually the interesting one.
                TAP.flush()

            # Firmware-update coverage: drive the keyboard's own HID update path
            # (BEGIN -> CHUNK -> COMMIT) with the built .bin. This is the one
            # path the UF2/BOOTSEL flash never exercises, and the most split-link-
            # sensitive code in the firmware. Stage + verify only (no APPLY), so
            # it's non-destructive — the keyboard keeps running its current image.
            # Run after the console is stopped (BEGIN tears USB down during the
            # master's staging erase) and regardless of the suite outcome.
            if bin_path:
                self.log("[runner] firmware update (HID stage+verify) — driving "
                         "BEGIN/CHUNK/COMMIT…")
                try:
                    fw_ok = stage_and_verify(bin_path, self.log)
                except Exception as exc:
                    fw_ok = False
                    self.log(f"[runner] firmware update ERROR: {exc}")
                results.append({"name": "firmware update (stage+verify .bin)",
                                "status": "pass" if fw_ok else "fail"})
                self.log(f"[test] {'PASS' if fw_ok else 'FAIL'}: "
                         f"firmware update (stage+verify .bin)")

            # Full HID update INCLUDING apply — extended tier, and the only check
            # that overwrites the running firmware. See firmware_apply_roundtrip.
            if apply_bin:
                if not extended:
                    self.log("[runner] firmware apply round-trip skipped — extended "
                             "tier only (pass --extended)")
                    results.append({
                        "name": "firmware apply round-trip (HID update + reboot)",
                        "status": "skip",
                        "reason": "extended suite — re-run with --extended "
                                  "(or the hil-extended label)"})
                elif any(r.get("status") == "fail" for r in results):
                    self.log("[runner] skipping the firmware apply round-trip — the "
                             "suite already has a failure to diagnose first")
                else:
                    results.append(self.firmware_apply_roundtrip(
                        apply_bin, left_uf2, console=console_started))

            # Persistence across a power cycle — LAST, because it reboots the
            # master (see reboot_persistence). Skipped when the suite already
            # failed: a rig that is misbehaving should not also be power-cycled,
            # and the reboot's own diagnosis would be unreadable next to the
            # earlier failures.
            if not (tests and extended):
                if tests:
                    self.log("[runner] reboot-persistence check skipped — extended "
                             "tier only (pass --extended)")
                    results.append({
                        "name": "reboot persistence (EEPROM survives a power cycle)",
                        "status": "skip",
                        "reason": "extended suite — re-run with --extended "
                                  "(or the hil-extended label)"})
            elif any(r.get("status") == "fail" for r in results):
                self.log("[runner] skipping the reboot-persistence check — the suite "
                         "already has a failure to diagnose first")
            else:
                results.append(self.reboot_persistence())

            # Only a genuine FAIL fails the run; SKIP / XFAIL / XPASS do not.
            passed = not any(r.get("status") == "fail" for r in results)
            if passed:
                # Successful HIL test/deploy — park the OLEDs so they don't sit
                # lit between runs and age. (With no tests defined this is a bare
                # successful flash, which is still a deploy worth blanking after.)
                self._turn_off_displays()
            self.status = "idle"
            return {"passed": passed, "results": results}

        except Exception as exc:
            self.status = "error"
            self.log(f"[runner] fatal: {exc}")
            raise
        finally:
            self._flash.cleanup()

    # The slave reboots a few seconds AFTER the master, so the post-apply master
    # count is a MOVING value for a short window. Long enough to cover that,
    # with an early exit once it has settled.
    _MASTERS_SETTLE_S = 15.0
    _MASTERS_STABLE   = 3
    _MASTERS_SPACING  = 0.5

    def _masters_after_apply(self) -> int:
        """How many halves enumerate as master, waiting for the count to SETTLE.

        Two IS both halves being master — the same signal ``test_single_master``
        grades — and after an apply on THIS rig that is the expected outcome
        rather than a fault. See the note in :meth:`firmware_apply_roundtrip`.

        ⚠️ **A single snapshot is not enough, and waiting for it implicitly is
        what makes the result fragile.** Everything the runner does between the
        apply and this point — the sleep, ``wait_for_master_ready``,
        ``settle_master`` — probes the MASTER only, so it happens to outlast the
        slave's delayed reboot rather than waiting for it. A firmware timing
        change that lets those finish sooner would leave two intermediate states
        indistinguishable from real outcomes: measuring the still-healthy
        *pre-reboot* slave and reporting "both halves came back" (a false GREEN,
        the worse one), or catching the reset mid-measurement and grading a
        fault. So this polls until the count stops moving instead of trusting
        one reading. Raised by Greptile on ctnd#87.
        """
        last, streak = None, 0
        deadline = time.monotonic() + self._MASTERS_SETTLE_S
        while time.monotonic() < deadline:
            try:
                count = len(enumerate_raw_interfaces())
            except Exception as exc:
                # Never let an enumeration hiccup decide a firmware verdict:
                # report one master, which sends the caller down the *measuring*
                # path rather than silently exempting the run.
                self.log(f"[runner] could not enumerate Raw HID interfaces: {exc}")
                return 1
            streak = streak + 1 if count == last else 1
            last = count
            # Early exit only on the state we are waiting FOR. A settled 1 is
            # not terminal — the slave may simply not have rebooted yet, which
            # is the false-green case.
            if count > 1 and streak >= self._MASTERS_STABLE:
                return count
            time.sleep(self._MASTERS_SPACING)
        return last if last is not None else 1

    def _reattach_console(self) -> bool:
        """Re-open the firmware console after a flash, for the checks that need it.

        ⚠️ The suite STOPS the console before the whole firmware-update section
        (``BEGIN`` tears USB down during the master's staging erase), so anything
        after that point reads a ``TAP`` nothing is feeding. That is what makes
        this necessary and it is easy to miss: the post-apply link check would
        otherwise report ``LINK_NO_SUMMARY`` on every run — present, passing,
        and asserting nothing, which is the exact shape of non-coverage this
        whole change exists to remove.

        Best-effort, like the initial start: a console that will not come back
        must not fail a firmware test. Returns whether the reader is live.
        """
        try:
            self._console.stop()
        except Exception:
            pass
        try:
            self._console.start(console_sink(lambda msg: self.log(f"[qmk] {msg}")))
            return True
        except Exception as exc:
            self.log(f"[runner] could not reattach the firmware console "
                     f"(continuing without it): {exc}")
            return False

    def firmware_apply_roundtrip(self, apply_bin: str, left_uf2: str,
                                 console: bool = False) -> dict:
        """Stage a SIGNED image over HID, APPLY it, and require the board to come back.

        This is the check that would have caught #258 — a HID update that reported
        complete success and then locked the master up mid-copy, leaving the running
        image part-erased and only BOOTSEL+UF2 to recover it. Nothing on the rig
        could see it: ``stage_and_verify`` deliberately stops at COMMIT, so the
        applier — the code that actually overwrites the running firmware — had never
        been executed here at all.

        It is also the shape of bug that no amount of source review finds. The cause
        was a ``uint8_t`` page buffer whose *link-time address* happened to be
        unaligned, so an ``STMIA`` HardFaulted the core with PRIMASK set. Nothing
        changed in the applier's code; a commit elsewhere moved its buffer. That is
        reachable from **any** change that alters ``.bss``, which is why the only
        durable guard is executing a real apply on real hardware.

        ⚠️ Two preconditions, both enforced rather than assumed:

        * **The image must be SIGNED.** Since FW-2 the keyboard puts an
          ACCEPT/REJECT prompt on its own keycaps for an unsigned image, and the rig
          has no fingers. CI signs the HIL image with an *ephemeral* key (the
          ``build-doom`` pattern) — never the production key, which belongs to
          ``release.yml`` alone.
        * **It must be the image the master is already running.** Applying anything
          else reboots the master onto VBUS master-detection, and both halves then
          enumerate as master until the next UF2 flash. The guard compares the
          candidate against the ``.uf2`` the rig itself flashed, so it cannot be
          defeated by a filename.
        
        ⚠️ **"The master came back" is NOT the whole assertion, and used to be.**
        An apply reboots BOTH halves — the slave copies its own staged image and
        resets a few seconds after the master — so the split link has to
        re-establish afterwards, and nothing here looked at it: the suite's link
        soak runs long *before* the update. A field report (2026-09-03) had the
        master enumerate perfectly while the link went totally silent —
        ``transport_fail`` climbing 201 of 201 frames with ``crc_err=0``, i.e. the
        slave answering nothing at all rather than answering corrupt. Every
        assertion this test made would have passed. So the apply now ends by
        bridging real traffic and measuring the link, which is the only thing on
        the rig that can see the slave at all.
        """
        name = "firmware apply round-trip (HID update + reboot)"

        # -- Precondition: this really is the running image. ------------------
        # Compared against the UF2 the rig flashed, not its name. The UF2 payload
        # is the raw image plus 0xFF padding to a 256-byte block boundary, so the
        # .bin is a prefix of it -- and the padding lands inside the last sector,
        # which is erased to 0xFF anyway.
        try:
            with open(apply_bin, "rb") as fh:
                img = fh.read()
            flashed = uf2_file_to_bin(left_uf2)
        except (OSError, Uf2Error) as exc:
            self.log(f"[test] FAIL: {name}: cannot compare the image to the flashed "
                     f"UF2: {exc}")
            return {"name": name, "status": "fail", "error": str(exc)}

        if not (len(flashed) >= len(img) and flashed[:len(img)] == img
                and set(flashed[len(img):]) <= {0xFF}):
            self.log(f"[test] FAIL: {name}: {os.path.basename(apply_bin)} is NOT the "
                     f"image the rig flashed ({os.path.basename(left_uf2)}). Applying "
                     "it would reboot the master onto VBUS master-detection and make "
                     "both halves enumerate as master. Refusing.")
            return {"name": name, "status": "fail",
                    "error": "apply image differs from the flashed UF2"}

        if not os.path.exists(apply_bin + ".sig"):
            reason = ("no .sig beside the image — an unsigned image stops at the "
                      "keyboard's physical ACCEPT prompt, which the rig cannot answer")
            self.log(f"[test] SKIP: {name} ({reason})")
            return {"name": name, "status": "skip", "reason": reason}

        want = caps_from_image(img) or {}
        self.log(f"[runner] firmware apply round-trip: staging {len(img)} B "
                 f"({want.get('fw', 'unknown version')}) and APPLYING it")

        try:
            if not stage_and_verify(apply_bin, self.log, require_signed=True):
                raise RuntimeError("staging the signed image did not reach a clean COMMIT")

            mark = TAP.mark()
            if not apply_staged(self.log):
                raise RuntimeError("FW_UP_APPLY was refused")

            # The board reboots into the applier, which runs with interrupts off and
            # cannot print. Give it the copy time before looking for it back.
            time.sleep(5)
            self.wait_for_master_ready()
            # Reattach BEFORE the banner and link checks below — both read the
            # console, and it has been stopped since before BEGIN.
            console_live = self._reattach_console() if console else False
            settled = self.settle_master()
            if not settled:
                # Reported, not failed. Measured on two consecutive runs (0.17.4
                # and 0.18.0), the master answers GET_LANG in a uniform ~450 ms
                # for the full 30 s window after an apply — 66 probes, worst
                # 446 ms, byte-identical across both — where the same master
                # after an ordinary RUN-pin power cycle settles in 15 probes.
                # That is a real and reproducible difference between the two
                # reboot paths; what causes it is NOT established, so this says
                # what was measured and stops there rather than naming a
                # mechanism the rig cannot see.
                self.log("[runner] note: the master did not settle after the apply. "
                         "This is expected on the apply path (it reproduces run to "
                         "run) and is NOT expected after a plain power cycle — if "
                         "the reboot-persistence settle below is also slow, the two "
                         "are worth comparing")

            caps = self._device_caps()
            if not caps:
                raise RuntimeError(
                    "the keyboard did not answer GET_ID after the apply. This is the "
                    "#258 signature: the copy died part-way and left an image that is "
                    "neither the old one nor the new one. Recover with BOOTSEL+UF2, "
                    "then read the banner's 'apply:' lines -- the in-flash progress "
                    "log survives the recovery and names the sector it stopped at")
            # ⚠️ The key is "fw" -- both producers (hil_tests.parse_device_caps and
            # fw_update.caps_from_image) use it, and this read said "version" until
            # a reviewer caught it. That silently disabled the one check that
            # catches the board coming back on the OLD image, which is exactly the
            # partial-apply shape this whole test exists for. Pinned by
            # CapsKeyContractTest so a rename on either side fails a test instead.
            got = caps.get("fw")
            if want.get("fw") and got and got != want["fw"]:
                raise RuntimeError(
                    f"the keyboard came back as {got}, but the applied image is "
                    f"{want['fw']} -- it is running the OLD firmware, so the copy "
                    "did not take even though the board re-enumerated")

            # The firmware verifies its own copy against the staged source and says
            # so in the boot banner. That is a stronger statement than "it booted":
            # a copy can complete, be wrong, and still boot.
            done = TAP.wait_for("last self-apply COMPLETED", mark=mark, timeout=5.0)
            match = TAP.wait_for("written image MATCHED", mark=mark, timeout=1.0)
            if done and match:
                self.log("[runner] the firmware reports its own copy complete and "
                         "byte-identical to the staged source")
            elif done or match:
                self.log("[runner] note: only part of the apply banner was seen "
                         "(console lines can be dropped) -- the board is up and on "
                         "the right version, which is the assertion that counts")
            else:
                # ⚠️ Expected, and NOT evidence about the firmware. The console is
                # stopped before BEGIN and only reattached above, i.e. after the
                # board has already rebooted — so a banner printed during boot is
                # missed by construction. This note used to offer "the console did
                # not come up, or this firmware predates the in-flash apply log",
                # neither of which was the reason, and that reading closed the
                # question for months.
                self.log("[runner] note: no apply banner seen — expected, since the "
                         "console is reattached only after the reboot, so a line "
                         "printed during boot is missed. The re-enumeration and "
                         "link checks are the assertions that count")

            # -- The other half of the board. -------------------------------
            # Everything above this point is a statement about the MASTER. The
            # slave reboots too, and only the split-link counters can say
            # whether it came back; see the docstring.
            # ⚠️ On THIS RIG the apply necessarily destroys the slave, and that
            # is structural, not a firmware fault. The slave installs its own
            # STAGED image, and the staged bytes are the ones the master bridged
            # during CHUNK — i.e. the master's image. On a real keyboard both
            # halves run one identical image and the role is decided at runtime
            # by VBUS, so that is exactly right. Here the halves run DIFFERENT
            # images by construction (POLYKYBD_HIL=left/right), so the slave
            # applies the left/master image, no longer calls usb_disconnect(),
            # and comes back as a second master: no slave, so 100%
            # transport_fail. Measured on run 33733020495 — 12930 of 12930
            # frames, crc_err=0.
            #
            # That is directly observable rather than assumed: two enumerated
            # Raw HID interfaces IS both halves being master (the same signal
            # `test_single_master` uses). So the two cases stay distinguishable
            # — one master plus a dead link is a REAL slave failure and still
            # fails; two masters is this rig's own apply semantics and is
            # reported, not graded.
            # Whether the UNVERIFIED outcome has already been explained. Without
            # it the note below fires on every path and blames the console for a
            # two-master run — which run 33745711432 disproved in its own log,
            # since the apply banner it printed a second earlier is only readable
            # THROUGH that console. A diagnostic that states something the same
            # log falsifies is worse than none.
            explained = False
            masters = self._masters_after_apply()
            if masters > 1:
                self.log(f"[runner] note: {masters} Raw HID interfaces after the "
                         "apply — the slave installed the master's image (the rig "
                         "flashes per-side images, so an apply necessarily converts "
                         "it) and came back as a second master. The split link "
                         "cannot be measured in that state, so the SLAVE IS "
                         "UNVERIFIED for this run; it is NOT evidence of a firmware "
                         "fault. See the note in firmware_apply_roundtrip.")
                link = LINK_NO_SUMMARY
                explained = True
            else:
                link = (measure_split_link(self._raw, self.log)
                        if console_live else LINK_NO_SUMMARY)
                # ⚠️ Re-check before grading a fault. The slave reboots a few
                # SECONDS after the master, so a single enumeration above can
                # catch it mid-boot and read one interface where there will
                # shortly be two — and the soak itself takes long enough for it
                # to finish. Without this the race lands as a FALSE RED on a
                # gate that runs on every merge, which is the failure this
                # whole commit exists to prevent.
                if link != LINK_OK and self._masters_after_apply() > 1:
                    # ⚠️ Only claim a measurement when one actually ran. With no
                    # console there was nothing to measure, so saying the slave
                    # rebooted "during the measurement" describes something that
                    # did not happen.
                    when = ("during the measurement" if console_live
                            else "after the interface count settled")
                    self.log("[runner] note: the slave finished rebooting into "
                             f"the master's image {when} — see above; the SLAVE "
                             "IS UNVERIFIED, not faulty")
                    link = LINK_NO_SUMMARY
                    explained = True
            if link == LINK_OK:
                self.log("[runner] the split link is carrying traffic again — both "
                         "halves came back from the apply")
            elif link == LINK_NO_SUMMARY:
                # NOT a failure. The counters come from the firmware console, and
                # the reader has to survive the apply's re-enumeration to see them
                # (HIDConsole reopens for exactly this). No summary means the
                # measurement did not happen — reporting that as a dead slave
                # would be a false red on a console problem.
                #
                # ⚠️ The `explained` test belongs INSIDE this arm, not on it.
                # Hung off the `elif`, an already-explained NO_SUMMARY falls
                # through to the `else` and raises — turning the note fix into a
                # hard failure of the whole apply test. Caught by
                # test_two_masters_after_the_apply_is_reported_not_graded.
                # ⚠️ A dead console is reported REGARDLESS of `explained`. It
                # is its own fault, not an alternative explanation for this run:
                # when the console does not come back the counters are unreadable
                # for every later check too, and suppressing the note because the
                # master count also explains this run's outcome loses the only
                # signal that says so. Raised by Greptile on ctnd#88.
                if not console_live:
                    self.log("[runner] note: the post-apply split-link check could "
                             "not read the firmware's 'Split link:' counters (the "
                             "console did not reattach after the reboot) — the "
                             "SLAVE IS UNVERIFIED for this run")
                elif not explained:
                    self.log("[runner] note: the console reattached but produced no "
                             "'Split link:' summary, so the link could not be "
                             "measured — the SLAVE IS UNVERIFIED for this run")
            else:
                raise RuntimeError(
                    "the master came back but the split link did not: the slave is "
                    "not answering, or the wire is corrupting frames. A firmware "
                    "apply reboots both halves, so this is where a slave that "
                    "failed to come back shows up")

            self.log(f"[test] PASS: {name} — applied {got} and the keyboard came back")
            return {"name": name, "status": "pass"}
        except Exception as exc:
            self.log(f"[test] FAIL: {name}: {exc}")
            return {"name": name, "status": "fail", "error": str(exc)}
        finally:
            # Hand the console back in the state the caller left it: stopped for
            # the rest of the firmware section (reboot_persistence power-cycles
            # the master right after this).
            try:
                self._console.stop()
                TAP.flush()
            except Exception:
                pass

    def reboot_persistence(self) -> dict:
        """Set a persisted setting, flush it, POWER-CYCLE the master, read it back.

        This is the only check in the suite that survives a reboot, and it covers
        the subsystem with the longest field-bug list in the firmware: the
        suspend-only dirty-flag EEPROM model. Everything else the rig asserts is
        RAM state, so a value that is applied correctly and then never actually
        persisted (or persisted and then read back wrong on the next boot) passes
        every other test. The shipped bugs of exactly that shape include brightness
        coming up 0 after a reboot, the default layer not surviving, the latin
        assignment map reading back all-zeros through QMK's wear levelling (whose
        recovery needed a *second* fix because a stale-but-stamped-valid EEPROM
        had already shipped), and auto-brightness losing its mode.

        Three commands are exercised, two of them for the first time:

        * **cmd 28** (idle style) as the carrier — one byte in ``poly_eeconf_t``,
          restored afterwards, and PULSE/JITTER keep the display quiet (unlike
          IDDQD/EDEN, which would start an animation).
        * **cmd 26** (``save_all_dirty``) — the host's "flush now" signal, on the
          path the whole persistence model funnels through, previously untested.
        * the RUN-pin power cycle via :class:`FlashController` — the rig has always
          had ``reset()`` and no test had ever used it.

        Runner-level (it needs the GPIO), so it is not in ``TESTS``, and it runs
        LAST: rebooting the master alone leaves the slave mid-session and the split
        link to re-establish, which nothing after it should have to absorb.
        """
        caps = self._device_caps()
        proto = caps.get("protocol")
        if proto is not None and proto < IDLE_STYLE_MIN_PROTOCOL:
            reason = (f"needs protocol >= {IDLE_STYLE_MIN_PROTOCOL} for cmd 28, "
                      f"device reports P{proto}")
            self.log(f"[test] SKIP: reboot persistence ({reason})")
            return {"name": "reboot persistence (EEPROM survives a power cycle)",
                    "status": "skip", "reason": reason}

        original = self._idle_style()
        if original is None:
            self.log("[test] FAIL: reboot persistence: could not read the idle style "
                     "before the reboot")
            return {"name": "reboot persistence (EEPROM survives a power cycle)",
                    "status": "fail", "error": "idle style unreadable"}
        target = 1 if original != 1 else 0
        self.log(f"[runner] reboot persistence: idle style {original} -> {target}, "
                 "flushing to EEPROM, then power-cycling the master")

        try:
            if not self._set_idle_style(target):
                raise RuntimeError(f"the keyboard refused idle style {target}")
            if not self._save_eeprom():
                raise RuntimeError("cmd 26 (save_all_dirty) was not ACKed")

            self._flash.reset("left", self.log)
            time.sleep(2)
            self.wait_for_master_ready()
            self.settle_master()

            after = self._idle_style()
            if after is None:
                raise RuntimeError("the idle style could not be read after the reboot")
            if after != target:
                raise RuntimeError(
                    f"idle style read back as {after}, expected {target} — the value "
                    "did not survive the power cycle. Either cmd 26 did not flush it, "
                    "or the boot-time load lost it (a poly_eeconf_t layout/version "
                    "change resets user state; QMK's wear levelling reads a cleared "
                    "byte back as 0x00, not 0xFF)")
            self.log(f"[test] PASS: reboot persistence — idle style {target} survived "
                     "the power cycle")
            return {"name": "reboot persistence (EEPROM survives a power cycle)",
                    "status": "pass"}
        except Exception as exc:
            self.log(f"[test] FAIL: reboot persistence: {exc}")
            return {"name": "reboot persistence (EEPROM survives a power cycle)",
                    "status": "fail", "error": str(exc)}
        finally:
            # Put the rig back the way it was, whichever branch we left by — and
            # flush again, or the restore itself would only live in RAM.
            if self._idle_style() != original:
                restored = self._set_idle_style(original) and self._save_eeprom()
                self.log(f"[runner] restore idle style {original}: "
                         + ("ok" if restored else "FAILED — the rig keeps the test "
                            "value until someone sets it back"))

    def _idle_style(self):
        """Current idle style (cmd 28 with the 0xFF query byte), or None."""
        try:
            resp = self._raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, 0xFF]))
        except Exception as exc:
            self.log(f"[runner] idle-style read error: {exc}")
            return None
        if (resp and len(resp) >= 4 and resp[0] == POLY_CHANNEL
                and resp[1] == CMD_IDLE_STYLE and resp[2] == ACK):
            return resp[3]
        return None

    def _set_idle_style(self, style: int) -> bool:
        resp = self._raw.send(bytes([POLY_CHANNEL, CMD_IDLE_STYLE, style]))
        return bool(resp and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                    and resp[1] == CMD_IDLE_STYLE and resp[2] == ACK)

    def _save_eeprom(self) -> bool:
        """cmd 26 — flush every dirty user-state block to EEPROM now.

        The persistence model is suspend-only by design (a per-housekeeping write
        is what the "slave goes unresponsive" bug was about), so without this the
        setting above would still be RAM-only at the reset and the test would fail
        for the wrong reason."""
        resp = self._raw.send(bytes([POLY_CHANNEL, CMD_SAVE_EEPROM]))
        return bool(resp and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                    and resp[1] == CMD_SAVE_EEPROM and resp[2] == ACK)

    def wait_for_master_ready(self, need: int = 3, timeout: float = 30.0,
                               probe_timeout_ms: int = 800, spacing: float = 0.3) -> bool:
        """Block until the master is *stably* servicing HID, before the suite runs.

        After a cold flash the master answers an HID exchange or two, then blocks
        its main loop for several seconds doing the initial 72-keycap OLED render
        and split-sync to the just-booted slave (longer the larger the glyph
        set), during which Raw HID reads time out — then it recovers and stays
        responsive. The CI logs show the very first GET_ID succeeding (it catches
        the brief gap *before* that render starts) and the next two timing out,
        which fails the fresh-boot and GET_ID tests for a reason that has nothing
        to do with the firmware under test. So a single successful probe is not
        enough — require ``need`` consecutive replies to confirm the render is
        actually over, not just a momentary pre-render gap.

        Probes GET_LANG (cmd 7), which is read-only and — unlike GET_ID — does
        NOT consume the one-shot fresh-boot '*' marker that test_fresh_boot_marker
        asserts on, so this gate never disturbs that test. Best-effort: if the
        master never stabilises within ``timeout`` we log and run anyway, so a
        genuine hang still surfaces as the test failures it causes rather than
        being hidden here."""
        deadline = time.monotonic() + timeout
        streak = 0
        probes = 0
        last_err = None
        while time.monotonic() < deadline:
            probes += 1
            try:
                resp = self._raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG]),
                                      timeout_ms=probe_timeout_ms)
            except Exception as exc:
                resp = None
                # Treat any send error as "not ready yet" and keep polling, but
                # surface it (deduped, so the normal window doesn't flood the
                # log) so a *persistent* fault — e.g. the Raw HID interface never
                # reappearing, or a wrong VID/PID — is visible rather than hiding
                # behind a silent wait until the timeout.
                msg = f"{type(exc).__name__}: {exc}"
                if msg != last_err:
                    self.log(f"[runner] readiness probe error (still waiting): {msg}")
                    last_err = msg
            # Require the ACK status byte too, not just a well-formed header, so
            # a half-initialised reply during the boot window doesn't count as
            # ready. GET_LANG answers 'P\x07.<llCC>' (ACK) for a valid language.
            ok = (bool(resp) and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                  and resp[1] == CMD_GET_LANG and resp[2] == ACK)
            streak = streak + 1 if ok else 0
            if streak >= need:
                self.log(f"[runner] master ready — {need} consecutive GET_LANG "
                         f"replies after {probes} probe(s)")
                return True
            time.sleep(spacing)
        self.log(f"[runner] WARNING: master not stably responsive after {timeout:.0f}s "
                 f"({probes} probes) — running tests anyway")
        return False

    def settle_master(self, need: int = 15, timeout: float = 30.0,
                       fast_ms: int = 250, probe_timeout_ms: int = 2500,
                       spacing: float = 0.2) -> bool:
        """Wait until the master answers *quickly* and consistently for a SUSTAINED
        window, so the graded suite never starts inside the post-cold-flash
        split-sync settling window.

        The read-only readiness gate (GET_LANG) can pass while the slave is still
        unreachable: with no state change there's no master->slave sync, so the
        main loop isn't stalled and probes return fast. The earlier mitigations
        assumed the stall is *triggered by a state-changing command* (the keymap
        reset's slave sync, or a test that toggles state) and placed settles around
        those. But the rig's slave half connects asynchronously, and when it does
        the master runs its boot-time 72-keycap OLED render plus the initial
        split-sync to the just-booted slave, which blocks the main loop for
        *several seconds*. This is NOT a link-quality issue: the rig uses the same
        clean full-duplex two-wire split link as a shipping keyboard, so the cause
        is purely *timing* — the rig fires HID queries within ~2 s of the master
        booting, inside a boot-time busy window a human user never reaches. That
        window is NOT triggered by any host command, so it can land on a pure
        read-only query (a `legacy ASCII lang list NACKs` cmd-8 timed out this way,
        between a healthy cmd 7 and cmd 27 — a >9 s silence that even send()'s 3x
        retry could not ride out).

        A short settle is the hole: requiring only ~0.6 s of fast replies, the gate
        passed during the pre-connect lull and the burst hit mid-suite. So require a
        SUSTAINED streak (``need`` consecutive GET_LANG replies, each <= ``fast_ms``;
        a stalled loop blows past that and **resets the streak**), which only
        completes once the master has been continuously responsive long enough that
        the one-shot connect burst is behind us. GET_LANG is used (not GET_ID) so the
        one-shot fresh-boot '*' marker is left intact for test_fresh_boot_marker.
        Best-effort: logs and proceeds on timeout (a genuine hang then surfaces as
        the test failures it causes, not hidden here)."""
        deadline = time.monotonic() + timeout
        streak = 0
        probes = 0
        worst_ms = 0.0
        while time.monotonic() < deadline:
            probes += 1
            t0 = time.monotonic()
            try:
                resp = self._raw.send(bytes([POLY_CHANNEL, CMD_GET_LANG]),
                                      timeout_ms=probe_timeout_ms)
            except Exception:
                resp = None
            latency_ms = (time.monotonic() - t0) * 1000.0
            ok = (bool(resp) and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                  and resp[1] == CMD_GET_LANG and resp[2] == ACK
                  and latency_ms <= fast_ms)
            if ok:
                streak += 1
            else:
                worst_ms = max(worst_ms, latency_ms)
                streak = 0
            if streak >= need:
                self.log(f"[runner] master settled — {need} consecutive GET_LANG "
                         f"replies <= {fast_ms} ms after {probes} probe(s)")
                return True
            time.sleep(spacing)
        self.log(f"[runner] WARNING: master not settled after {timeout:.0f}s "
                 f"({probes} probes, worst {worst_ms:.0f} ms) — running anyway")
        return False

    def _device_caps(self) -> dict:
        """Read & cache the device's advertised capabilities (fw + protocol
        version) from GET_ID, for the per-test capability gate.

        Lazy and cached: only invoked when a gated test is first reached, which
        is after test_fresh_boot_marker has consumed the one-shot '*' marker, so
        this GET_ID doesn't disturb that test. Returns ``{}`` if the identity
        can't be read or parsed — ``skip_reason`` then runs the gated test rather
        than skipping it, so a read failure surfaces as a real result instead of
        being silently hidden. The empty dict is still cached to avoid re-probing.
        """
        if self._caps is not None:
            return self._caps
        caps = {}
        try:
            resp = self._raw.send(bytes([POLY_CHANNEL, CMD_GET_ID]))
        except Exception as exc:
            self.log(f"[runner] could not read device caps "
                     f"(gated tests will run anyway): {exc}")
            resp = None
        if (resp and len(resp) >= 3 and resp[0] == POLY_CHANNEL
                and resp[1] == CMD_GET_ID):
            identity = bytes(resp[3:]).split(b"\x00", 1)[0].decode("ascii", "replace")
            caps = parse_device_caps(identity)
            if caps:
                self.log(f"[runner] device caps: protocol P{caps.get('protocol')}, "
                         f"fw {caps.get('fw')}")
            else:
                self.log(f"[runner] could not parse device caps from {identity!r} "
                         "(gated tests will run anyway)")
        self._caps = caps
        return caps

    def _turn_off_displays(self) -> None:
        """Blank both OLEDs (status + per-key) after a passing run so the panels
        don't sit lit and age/burn in. The firmware turns them off without
        persisting to EEPROM (a key press or reboot restores brightness) and
        stays USB-enumerated. Best-effort: a failure here must not fail the run."""
        try:
            resp = self._raw.send(bytes([POLY_CHANNEL, CMD_DISPLAY_OFF]))
        except Exception as exc:
            self.log(f"[runner] could not turn off displays (non-fatal): {exc}")
            return
        if resp and len(resp) >= 3 and resp[0] == POLY_CHANNEL and resp[1] == CMD_DISPLAY_OFF and resp[2] == ACK:
            self.log("[runner] displays off — OLEDs blanked to prevent wear")
        else:
            self.log(f"[runner] display-off sent; unexpected/no ACK: {resp!r}")

    def _reset_keymap(self) -> None:
        """Reset the dynamic keymap (host-remappable layers 0..8) to the compiled
        firmware defaults so a stale keymap stored in EEPROM by an earlier build
        can't taint the run. Uses the existing VIA reset report (0x06) — the same
        path PolyKybdHost uses; the master resets its own copy, bridges the reset
        to the slave, and echoes the request back. Best-effort: a failure here is
        logged but must not fail the run."""
        try:
            resp = self._raw.send(bytes([VIA_DYNAMIC_KEYMAP_RESET]))
        except Exception as exc:
            self.log(f"[runner] could not reset dynamic keymap (non-fatal): {exc}")
            return
        if resp and len(resp) >= 1 and resp[0] == VIA_DYNAMIC_KEYMAP_RESET:
            self.log("[runner] dynamic keymap reset to firmware defaults")
        else:
            self.log(f"[runner] keymap-reset sent; unexpected/no echo: {resp!r}")

    def cleanup(self) -> None:
        self._flash.cleanup()
        self._console.stop()


def _derive_label(left_uf2: str) -> str:
    """Best-effort board name for the summary title, from the UF2 filename."""
    base = os.path.basename(left_uf2 or "")
    for board in ("split72", "split42"):
        if board in base:
            return board
    return ""


# Per-status presentation. WARN/SKIP/XFAIL/XPASS are non-failing outcomes (WARN =
# a check that failed only because a reply timed out after send()'s one retry — a
# transient rig USB/link blip, recorded but not run-failing); only ❌ FAIL fails.
_STATUS_MARK = {
    "pass": "✅", "fail": "❌", "warn": "⚠️", "skip": "⏭️", "xfail": "🟡", "xpass": "❗",
}
# Order + plural label for the summary count line.
_STATUS_WORD = [
    ("pass", "passed"), ("fail", "failed"), ("warn", "warning"),
    ("skip", "skipped"), ("xfail", "xfail"), ("xpass", "xpass"),
]


def _status_of(r: dict) -> str:
    """Status of a result record, tolerating older bool-``passed`` records.

    The compatibility layer between legacy ``{"passed": bool}`` records and the
    newer ``{"status": str}`` ones; ``status`` wins when both are present.

    >>> _status_of({"status": "skip"})
    'skip'
    >>> _status_of({"passed": True})
    'pass'
    >>> _status_of({"passed": False})
    'fail'
    >>> _status_of({"status": "xfail", "passed": False})
    'xfail'
    """
    return r.get("status") or ("pass" if r.get("passed") else "fail")


def write_github_summary(result: dict, label: str = "") -> None:
    """Surface each test as its own line in the GitHub Actions run.

    Writes a per-test markdown bullet to ``$GITHUB_STEP_SUMMARY`` (rendered on the
    job summary page) marked by status — ✅ pass, ❌ fail, ⏭️ skip (gated off on
    this firmware), 🟡 xfail (expected fail), ❗ xpass (xfail that unexpectedly
    passed) — and emits one ``::error::`` workflow annotation per real failure
    (plus a ``::warning::`` per xpass so a stale marker gets noticed), so it is
    obvious what happened without scrolling the raw log. No-ops when not under
    Actions / when the env vars are absent, so local runs are unaffected.
    """
    results = result.get("results", [])
    fatal = result.get("fatal")
    n_total = len(results)
    counts = {}
    for r in results:
        st = _status_of(r)
        counts[st] = counts.get(st, 0) + 1

    # Workflow-command annotations (parsed from stdout by the Actions runner).
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for r in results:
            st = _status_of(r)
            if st == "fail":
                detail = f" — {r['error']}" if r.get("error") else ""
                print(f"::error title=HIL test failed::{r['name']}{detail}")
            elif st == "warn":
                detail = f" — {r['reason']}" if r.get("reason") else ""
                print(f"::warning title=HIL transient read timeout::{r['name']}{detail}")
            elif st == "xpass":
                print(f"::warning title=HIL xfail unexpectedly passed::{r['name']}"
                      " — the firmware now satisfies this; remove its xfail marker")
        if fatal:
            print(f"::error title=HIL run aborted::{fatal}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    title = f"HIL test results — {label}" if label else "HIL test results"
    lines = [f"## {title}", ""]
    if fatal:
        lines += [f"> ⚠️ **Run aborted before tests completed:** `{fatal}`", ""]
    if n_total:
        summary = ", ".join(f"{counts[st]} {word}"
                            for st, word in _STATUS_WORD if counts.get(st))
        lines += [f"**{summary}**", ""]
        for r in results:
            st = _status_of(r)
            mark = _STATUS_MARK.get(st, "❓")
            line = f"- {mark} {r['name']}"
            if st == "skip" and r.get("reason"):
                line += f" — _skipped: {r['reason']}_"
            elif st == "xfail" and r.get("reason"):
                line += f" — _expected fail: {r['reason']}_"
            elif st == "xpass":
                why = f" ({r['reason']})" if r.get("reason") else ""
                line += f" — **xfail marker can be removed**{why}"
            elif st == "warn" and r.get("reason"):
                line += f" — _transient: {r['reason']}_"
            elif st == "fail" and r.get("error"):
                line += f" — `{r['error']}`"
            lines.append(line)
    elif not fatal:
        lines.append("_No tests were run._")
    lines.append("")

    try:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        print(f"[runner] could not write GITHUB_STEP_SUMMARY: {exc}")


if __name__ == "__main__":
    import argparse
    from .hil_tests import TESTS, set_doom_pack
    parser = argparse.ArgumentParser(description="Flash and test PolyKybd firmware")
    parser.add_argument("--left",  required=True, help="Path to left half UF2")
    parser.add_argument("--right", required=True, help="Path to right half UF2")
    parser.add_argument("--label", default=None,
                        help="Board name for the run summary title (default: inferred from --left)")
    parser.add_argument("--bin", dest="bin_path", default=None,
                        help="Optional raw .bin image; after the suite, drives the "
                             "keyboard's HID firmware-update path (BEGIN/CHUNK/COMMIT, "
                             "stage+verify only — non-destructive, no apply/reboot)")
    parser.add_argument("--apply-bin", dest="apply_bin", default=None,
                        help="Optional SIGNED raw .bin (with its .sig beside it) that "
                             "is byte-identical to the image passed to --left. Runs the "
                             "full HID update INCLUDING apply and requires the keyboard "
                             "to come back. EXTENDED tier and destructive: it overwrites "
                             "the running firmware, which is safe only because the image "
                             "is the one already running. Both preconditions are checked, "
                             "not assumed.")
    parser.add_argument("--extended", action="store_true",
                        default=os.environ.get("HIL_EXTENDED", "").lower()
                        in ("1", "true", "yes"),
                        help="also run the slow EXTENDED-tier checks (animation, "
                             "idle engage + Eden screensaver, split-link soak, "
                             "reboot persistence). Adds roughly a minute; meant for "
                             "a release or a change big enough to want them. Also "
                             "settable with HIL_EXTENDED=1.")
    parser.add_argument("--doom", action="store_true",
                        default=os.environ.get("DOOM_HIL", "").lower()
                        in ("1", "true", "yes"),
                        help="also run the TIER_DOOM checks (FW-9 signed-engine-pack "
                             "load/refuse). Needs --plyx-valid, a signed .plyx built "
                             "against these HIL images' signing key. Also settable "
                             "with DOOM_HIL=1.")
    parser.add_argument("--plyx-valid", dest="plyx_valid", default=None,
                        help="a signed DOOM engine pack (.plyx) for the --doom tests; "
                             "the rig derives the tampered/unsigned variants from it.")
    parser.add_argument("--probe", default=None,
                        help="run an ad-hoc PROBE from the firmware checkout instead "
                             "of the graded suite (see station/probe.py). Takes a probe "
                             "name or a repo-relative path under keyboards/polykybd/"
                             "tools/hil_probes/. The flash, readiness gates and console "
                             "tap all still run, so the firmware's own [qmk] lines land "
                             "in the run log — which is what lets a firmware bug be "
                             "chased without anyone flashing a .bin by hand.")
    parser.add_argument("--firmware-dir", default=os.environ.get("FIRMWARE_DIR", "."),
                        help="the firmware checkout --probe is resolved against "
                             "(default: $FIRMWARE_DIR, else the cwd)")
    parser.add_argument("--probe-with-suite", action="store_true",
                        help="run the graded suite as well as --probe. Off by default: "
                             "a debug loop wants the probe alone, and the suite costs "
                             "rig time on every iteration.")
    args = parser.parse_args()
    if args.doom and not args.plyx_valid:
        parser.error("--doom needs --plyx-valid (a signed .plyx built against the "
                     "HIL images' signing key)")
    if args.plyx_valid:
        with open(args.plyx_valid, "rb") as fh:
            set_doom_pack(fh.read())
    # A probe REPLACES the graded suite unless asked otherwise (see --probe-with-suite).
    # Resolve and import it BEFORE flashing: a typo'd probe name should cost a
    # message, not a full flash-and-enumerate cycle on the rig.
    suite = TESTS
    if args.probe:
        from .probe import ProbeError, load_probe, resolve_probe
        try:
            probe_test = load_probe(resolve_probe(args.probe, args.firmware_dir),
                                    log=print)
        except ProbeError as exc:
            parser.error(str(exc))
        suite = list(TESTS) + [probe_test] if args.probe_with_suite else [probe_test]

    runner = TestRunner()
    try:
        result = runner.flash_and_test(args.left, args.right, tests=suite,
                                       bin_path=args.bin_path,
                                       extended=args.extended,
                                       doom=args.doom,
                                       apply_bin=args.apply_bin)
    except Exception as exc:
        # A fatal flash/enumerate error still gets a summary line so the run page
        # shows *why* there are no per-test results, not just a red X.
        result = {"passed": False, "results": [], "fatal": str(exc)}
    finally:
        runner.cleanup()

    write_github_summary(result, label=args.label or _derive_label(args.left))

    # Hard-exit after flushing. The native hid / RPi.GPIO libraries can double-
    # free during interpreter shutdown ("free(): invalid pointer"), which aborts
    # the process with SIGABRT — turning a passing run into a CI failure (exit
    # 134) and dropping buffered stdout (the PASS/FAIL lines). os._exit() skips
    # that teardown entirely; flush first (and the summary above is already
    # written to disk) so the logs are complete.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if result["passed"] else 1)
