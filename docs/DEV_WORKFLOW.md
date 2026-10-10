# Development workflow: how a change reaches the rig

_Moved verbatim from `CLAUDE.md` on 2026-10-10. CLAUDE.md keeps a short pointer._


The RPi4 is not directly accessible from Claude Code on the web. Development cycle:

1. Edit files here (cloud session)
2. Commit + push to `main`
3. The rig **deploys itself** — `polykybd-update.timer` fetches `main` every ~5 min
   and, when it gains commits *and the rig is idle*, fast-forwards, reinstalls deps
   if `requirements.txt` changed, and restarts the station. No SSH needed. To apply
   immediately, tap the **UPDATE** badge in the touch UI (or `sudo systemctl start
   polykybd-update.service`). The old manual path still works:
   `git -C /opt/polykybd-ctnd pull && sudo systemctl restart polykybd-ctnd`.

> **⚠️ HIL CI runs the *installed* `/opt/polykybd-ctnd`, NOT a fresh checkout** —
> `qmk-test.yml`'s "Locate station directory" step just finds the install and runs
> `venv/bin/python -m station.test_runner`. So the suite only ever runs the station
> code the **self-update timer has already fast-forwarded** onto the rig. When that
> update lags (the rig was busy/offline, or the timer deferred), HIL runs **stale**
> station code and already-merged rig fixes silently don't apply — you'll chase a
> failure that was fixed days ago (seen 2026-07: the rig ran the old `need=3` settle
> for ~5 days while `main` had `need=15`, so the boot-burst flake kept "recurring").
> **When a HIL check flakes, verify the rig is current *first*, before blaming the
> firmware or a test.** The cheapest tell is the settle log line: `master settled —
> N consecutive GET_LANG replies … after N probe(s)` — `need=3` means stale
> (pre-`df6401d`), `need=15` means current. If stale, tap **UPDATE** (or wait a
> timer tick) and re-run before diagnosing anything else. The durable fix is to make
> CI pull `main` before the run (qmk `qmk-test.yml` "Sync station to current ctnd
> main" step) so a lagging timer can't leave HIL on stale code.
>
> ⚠️ **That sync is a FORCE checkout — `git checkout -q -f -B main origin/main` —
> so the rig's checkout is NOT a place to park a branch.** Testing an unmerged
> ctnd branch on the rig (e.g. to get a `setup.sh` flag that isn't on `main` yet)
> works only until the next HIL run, which discards it with no warning and no log
> line anyone reads. The self-update side dislikes it too: `self-update.sh` tracks
> `main`, so while a branch is checked out it reads 0-behind and does nothing, and
> once `main` advances the two diverge and its `--ff-only` merge fails (exit 75)
> on every 5-minute tick. **Check the branch out, do the one thing you need, then
> `git checkout main` in the same sitting.** Anything installed *outside* the
> checkout (systemd units, sudoers) survives the switch back — that is what makes
> the round trip safe.
>
> ⚠️ **Corollary that bites the OTHER repo: a green HIL board on a firmware PR does
> NOT mean that PR's own new rig test ran.** Because the rig runs `main`, a test added
> in an *unmerged* ctnd PR does not exist on the rig — the suite happily goes green
> having never executed it, and the firmware PR's checklist claims coverage nothing
> produced. Seen 2026-08-22: qmk#227 (keycap legend size) listed
> `test_glyph_size_round_trip` as tested while the test lived only in the still-open
> ctnd#71; the HIL log names every test it ran, and that one appears nowhere in it.
> **So a paired firmware+rig change has a merge ORDER: land the ctnd PR first, then
> re-run HIL on the firmware PR** — otherwise the firmware merges on a board that
> never checked the thing the rig PR was written to check. Verify rather than assume:
> grep the HIL job log for the test's own name (each prints `[test] PASS: <name>`),
> not just the job's conclusion.
>
> ✅ **But merging does NOT forfeit that coverage — it defers it by one run.**
> `qmk-test.yml` also triggers on `push: [PolyKybd]`, so the **merge commit itself**
> starts a HIL run that executes the new test. (The follow-on auto-bump `chore:`
> commit does not — it carries `[skip ci]`.) That is what decides the case where the
> rig is unavailable and the choice is "hold the PR open or merge anyway": both reach
> the same coverage at the same moment, so a rig outage is not a reason to leave a
> reviewed, hardware-confirmed PR dangling. Seen 2026-08-27 on qmk#233 — the merge
> started run #851, the first run able to execute `layer names (v14)`.
>
> ⚠️ **A red HIL that never RAN is a different thing again, and the settle line
> cannot tell you.** The job can die in workflow setup — fetching a GitHub Action —
> before checkout, before the station sync, before a single `[test]` line. On
> 2026-08-27 the rig lost outbound access to `codeload.github.com` and failed that
> way **three times** (twice on a PR, once on the `PolyKybd` merge push), leaving the
> default branch red on the rig's network rather than on any code. The tell is a log
> ending at `Prepare all required actions`; full triage in the `diagnose-hil-failure`
> skill, §1.5. Note the runner stays *online* throughout — it picks jobs up and
> streams logs — so "the rig is up" is not evidence its network is healthy.
>
> ⚠️ **The order is measured against the rig's SYNC STEP, not the run's start — so
> merging the two a minute apart in the WRONG order can still be fine.** On
> 2026-08-28 qmk#234 merged at 12:06:47 and ctnd#74 at 12:07:47, i.e. the firmware
> landed first, which the rule above says forfeits the new test. It did not: the
> merge-commit run started at 12:06:48 but `qmk-test.yml` force-syncs the station to
> ctnd `main` inside the **hil-test** job, which waits on the cloud build — several
> minutes later, by which time the ctnd merge had landed. So the window that matters
> is "ctnd `main` is current **when the sync step runs**", and a build long enough to
> cover a same-minute merge closes it for you. Do not rely on that: it is luck, the
> build time is not a contract, and the check is unchanged — **grep the HIL log for
> the test's own name** and re-run the job if it is absent, since a re-run syncs
> again and will then pick it up.
