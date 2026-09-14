# Rig self-update mechanism

Moved out of `CLAUDE.md` 2026-09-14. Verbatim.

### Self-update mechanism

- **`scripts/self-update.sh`** is the single actuator, run by both the timer
  (unattended) and the UI button. It fetches the tracked branch (`update.branch`
  in `config.yaml`, default `main`), and if behind: **defers while busy** (polls
  `GET /status`; any status other than `idle`/`error` ⇒ skip this tick, retry
  next — never aborts a flash/HIL run), else fast-forwards (`--ff-only`, so it
  never clobbers the gitignored `config.yaml` or rewrites history), pip-installs
  only if `requirements.txt` changed, and `sudo systemctl restart
  polykybd-ctnd`. The whole body is in a `{ … }` group with an explicit `exit` so
  bash parses the entire file before running — a pull that rewrites the script
  mid-run can't desync the interpreter. `--check` reports behind/ahead without
  applying (exit 10 = behind); `--no-restart` pulls without bouncing the service.
- **`polykybd-update.service`** (oneshot) runs the script in its **own cgroup**,
  so the `restart polykybd-ctnd` it issues at the end does not kill the updater.
  **`polykybd-update.timer`** fires it `OnBootSec=2min` then every 5 min.
- **UI**: the `UPDATE` header badge (`app.py` `_update_poll_once`, 120 s) shows
  `UP ✓` (current) / `UP ↓N` (behind) / `UP …` (updating); tap = two-tap-confirm
  `update_now`, which fetches, logs the incoming commits, and kicks the oneshot
  via `sudo -n systemctl start --no-block polykybd-update.service`. The badge
  re-polls to `current` after the service restarts and the browser reconnects.
- **`setup.sh`** installs both units (enables the timer) and a scoped
  `/etc/sudoers.d/polykybd-update` granting the station user NOPASSWD on exactly
  `systemctl restart polykybd-ctnd.service` and `systemctl start
  polykybd-update.service`.
- ⚠️ **A rig provisioned before a unit landed never gets it — `setup.sh` is the
  only installer, and nothing re-runs it.** Hit 2026-08-03: the UPDATE button
  failed with `Unit polykybd-update.service not found`, i.e. the rig predates the
  self-update feature, so **the timer was missing too and unattended updates had
  never run there** (HIL was unaffected only because `qmk-test.yml` force-syncs
  the station to `origin/main` itself — see the stale-rig warning above). The unit
  is just the *carrier*; `scripts/self-update.sh` is the actuator, so `update_now`
  now falls back to running it in-process (`--no-restart`, then a separate
  `systemctl restart` — the script's own restart would tear down the UI's cgroup
  mid-pull) and `_diagnose_unit_start_failure()` distinguishes a missing unit from
  a missing sudoers grant, which need opposite fixes. Recovery is
  **`sudo bash ./scripts/setup.sh --units-only`** (`bash …`, not `./…`: an older
  checkout lacks the execute bit, and with no `x` bit set *even root* gets
  `Permission denied`) — installs only the units + service
  sudoers grants (no apt, no venv rebuild, no `config.yaml`/chown churn), which is
  what you want on a *working* rig. Don't send someone through a full `setup.sh`
  run to drop two files.
  - ⚠️ **Why it hid for the rig's whole life: the CI force-sync masked it.** The
    "Sync station to current ctnd main" step was added so a *lagging* timer
    couldn't leave HIL on stale code — and it also removed the only symptom that
    would have revealed a timer that was never installed *at all*. HIL stayed
    green throughout; the UPDATE badge polls git directly, so it correctly showed
    "N behind" while reporting nothing about whether the mechanism that applies
    updates exists. Generalise before adding the next such workaround: **a
    compensating sync hides the difference between "slow" and "absent", and
    nothing here checks that the installed units/grants still match the repo.**

