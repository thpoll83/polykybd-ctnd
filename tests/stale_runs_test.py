# SPDX-License-Identifier: GPL-2.0-only
"""The Runner tab's stale-run rule and the cancel fallback (station/ui/app.py).

A run GitHub will never finish: queued with no jobs, or in progress with every
job completed, unchanged for an hour. The rule must not touch a young run, a
run whose jobs wait for a runner or a run with a job still going, and a cancel
that GitHub refuses with 409 must fall back to force-cancel.
Skipped where Flask is not installed (the app module needs it).
"""
import sys
import types
import unittest

if "hid" not in sys.modules:  # pragma: no cover - environment shim
    _hid = types.ModuleType("hid")
    _hid.enumerate = lambda *a, **k: []
    _hid.Device = object
    sys.modules["hid"] = _hid

try:
    import station.ui.app as app
except ImportError:  # pragma: no cover - no Flask here
    app = None

NOW = 1_800_000_000.0   # 2027-01-15T08:00:00Z


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@unittest.skipIf(app is None, "Flask not installed")
class StaleRunTest(unittest.TestCase):
    def setUp(self):
        self._orig = app._gh_api
        self.calls = []
        self.jobs = {}   # run id -> list of job statuses
        app._STALE_VERDICT.clear()

        def fake(path, timeout=10, method="GET"):
            self.calls.append((method, path))
            run_id = int(path.split("/runs/")[1].split("/")[0])
            jobs = [{"status": st} for st in self.jobs[run_id]]
            return 200, {"total_count": len(jobs), "jobs": jobs}
        app._gh_api = fake

    def tearDown(self):
        app._gh_api = self._orig
        app._STALE_VERDICT.clear()

    def run_(self, run_id, age_s, status="queued"):
        return {"id": run_id, "status": status, "created_at": _iso(NOW - age_s - 5),
                "updated_at": _iso(NOW - age_s)}

    def test_old_run_without_jobs_is_stale(self):
        self.jobs[1] = []
        self.assertTrue(app._is_stale(self.run_(1, 2 * 3600), NOW))

    def test_old_run_with_waiting_jobs_is_not_stale(self):
        self.jobs[2] = ["queued"]
        self.assertFalse(app._is_stale(self.run_(2, 2 * 3600), NOW))

    def test_young_run_is_not_asked_about(self):
        self.jobs[3] = []
        self.assertFalse(app._is_stale(self.run_(3, app.STALE_RUN_MIN_AGE_S - 60), NOW))
        self.assertEqual(self.calls, [])

    def test_running_run_with_a_live_job_is_not_stale(self):
        self.jobs[4] = ["completed", "in_progress"]
        self.assertFalse(app._is_stale(self.run_(4, 9 * 3600, "in_progress"), NOW))

    def test_running_run_whose_jobs_all_ended_is_stale(self):
        # #57230: its one job completed on 2026-08-09, the run never did.
        self.jobs[10] = ["completed"]
        self.assertTrue(app._is_stale(self.run_(10, 2615 * 3600, "in_progress"), NOW))

    def test_running_verdict_false_is_asked_again(self):
        self.jobs[11] = ["in_progress"]
        app._is_stale(self.run_(11, 2 * 3600, "in_progress"), NOW)
        self.jobs[11] = ["completed"]
        self.assertTrue(app._is_stale(self.run_(11, 2 * 3600, "in_progress"), NOW))

    def test_finished_run_is_never_stale(self):
        self.assertFalse(app._is_stale(self.run_(12, 9 * 3600, "completed"), NOW))
        self.assertEqual(self.calls, [])

    def test_rerun_of_a_stale_run_is_judged_again(self):
        # The run id stays on a re-run; attempt, status and updated_at change.
        self.jobs[13] = []
        self.assertTrue(app._is_stale(self.run_(13, 2 * 3600), NOW))
        rerun = dict(self.run_(13, 60), run_attempt=2)
        self.assertFalse(app._is_stale(rerun, NOW))

    def test_queued_with_jobs_then_running_with_all_jobs_done_is_stale(self):
        self.jobs[14] = ["queued"]
        self.assertFalse(app._is_stale(self.run_(14, 2 * 3600), NOW))
        self.jobs[14] = ["completed"]
        self.assertTrue(app._is_stale(self.run_(14, 2 * 3600, "in_progress"), NOW))

    def test_verdict_is_cached(self):
        self.jobs[5] = []
        app._is_stale(self.run_(5, 2 * 3600), NOW)
        app._is_stale(self.run_(5, 2 * 3600), NOW)
        self.assertEqual(len(self.calls), 1)

    def test_failed_jobs_query_is_not_stale_and_not_cached(self):
        app._gh_api = lambda path, timeout=10, method="GET": (502, None)
        self.assertFalse(app._is_stale(self.run_(6, 2 * 3600), NOW))
        self.assertEqual(app._STALE_VERDICT, {})


@unittest.skipIf(app is None, "Flask not installed")
class CancelRunTest(unittest.TestCase):
    def setUp(self):
        self._orig = app._gh_api

    def tearDown(self):
        app._gh_api = self._orig

    def _fake(self, answers):
        calls = []

        def fake(path, timeout=10, method="GET"):
            calls.append(path.rsplit("/", 1)[1])
            return answers[path.rsplit("/", 1)[1]], {}
        app._gh_api = fake
        return calls

    def test_cancel(self):
        calls = self._fake({"cancel": 202})
        self.assertEqual(app._cancel_run(7), "cancelled")
        self.assertEqual(calls, ["cancel"])

    def test_409_falls_back_to_force_cancel(self):
        calls = self._fake({"cancel": 409, "force-cancel": 202})
        self.assertEqual(app._cancel_run(8), "cancelled")
        self.assertEqual(calls, ["cancel", "force-cancel"])

    def test_missing_permission_is_named(self):
        self._fake({"cancel": 403})
        self.assertIn("Actions: Read and write", app._cancel_run(9))


@unittest.skipIf(app is None, "Flask not installed")
class CiJobOnRigTest(unittest.TestCase):
    """The UI refuses rig actions while the runner's Runner.Worker runs."""

    def _proc(self, cmdlines):
        import os
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for pid, cmd in cmdlines.items():
            os.makedirs(os.path.join(tmp.name, pid))
            with open(os.path.join(tmp.name, pid, "cmdline"), "wb") as f:
                f.write(cmd)
        os.makedirs(os.path.join(tmp.name, "self"))
        return tmp.name

    def setUp(self):
        self._orig = dict(app._runner_state)
        app._runner_state["status"] = "online"

    def tearDown(self):
        app._runner_state.clear()
        app._runner_state.update(self._orig)

    def test_worker_process_means_a_job(self):
        proc = self._proc({"10": b"/home/pi/actions-runner/bin/Runner.Worker\0spawnclient\0"})
        self.assertTrue(app._ci_job_on_rig(proc))

    def test_listener_alone_is_no_job(self):
        proc = self._proc({"10": b"/home/pi/actions-runner/bin/Runner.Listener\0run\0"})
        self.assertFalse(app._ci_job_on_rig(proc))

    def test_github_busy_flag_is_the_fallback(self):
        app._runner_state["status"] = "busy"
        self.assertTrue(app._ci_job_on_rig(self._proc({})))


@unittest.skipIf(app is None, "Flask not installed")
class CiBadgeTest(unittest.TestCase):
    """The CI badge pages past stale runs to find a live one."""

    def setUp(self):
        self._orig = (app._gh_api, app._is_stale, app.socketio.emit, dict(app._ci_state))

    def tearDown(self):
        app._gh_api, app._is_stale, app.socketio.emit, state = self._orig
        app._ci_state.clear()
        app._ci_state.update(state)

    def _poll(self, pages):
        calls = []

        def fake(path, timeout=10, method="GET"):
            page = int(path.rsplit("page=", 1)[1])
            calls.append(page)
            return 200, {"workflow_runs": pages[page - 1] if page <= len(pages) else []}
        app._gh_api = fake
        app._is_stale = lambda run, now=None: run.get("stale", False)
        app.socketio.emit = lambda *a, **k: None
        app._ci_poll_once()
        return calls

    def test_live_run_on_page_two_is_found(self):
        stale = [{"id": i, "stale": True} for i in range(100)]
        calls = self._poll([stale, [{"id": 200, "html_url": "u"}]])
        self.assertEqual(calls, [1, 2])
        self.assertEqual(app._ci_state, {"running": True, "url": "u"})

    def test_only_stale_runs_mean_not_running(self):
        calls = self._poll([[{"id": 1, "stale": True}]])
        self.assertEqual(calls, [1])
        self.assertFalse(app._ci_state["running"])


if __name__ == "__main__":
    unittest.main()
