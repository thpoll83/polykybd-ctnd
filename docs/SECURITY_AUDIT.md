# PolyKybd security audit — findings tracker

Cross-repo tracker for the security audit findings (`FW-*` firmware, `HOST-*` host app,
`HIL-*` rig/CI). It lives here because most of the still-open items are rig items, but it
covers all four repos — check the **Where** column before going looking for code.

Status verified against: `polykybd-ctnd` @ `61e170c`, `qmk_firmware` @ `0.9.90`,
`PolyKybdHost` @ `0.10.6`, `polykybd-docs` @ PR #28. Last review **2026-08-04**
(HIL-2 confirmed set; HIL-6 remediated on the rig; HIL-7 raised and fixed;
HIL-8 raised and remediated; HIL-9 raised and partly mitigated; FW-2 key
provisioned).

> The finding IDs originate from an audit that was only ever held in session context. This
> file is the first committed record of them, reconstructed and re-verified against the
> code — so treat the *status* here as authoritative and the *numbering* as historical.

## Status at a glance

| ID | Title | Where | Status |
|---|---|---|---|
| FW-1 | ROI clamp | qmk | ✅ fixed |
| FW-2 | Firmware image signing (Ed25519) | qmk / host / docs | ⚠️ warn-only — verified on hardware, only enforcement left |
| FW-3 / FW-5 | Dynamic-keymap buffer OOB | qmk | ✅ fixed (PR #112) |
| FW-4 | `get_overlay` OOB | qmk | ✅ fixed |
| FW-6 | (note only) | qmk | ✅ closed (PR #112) |
| FW-7 | Plain-overlay 1-byte over-read | qmk / host / rig | ✅ fixed as the protocol-11 reframe (#120 / #96 / #43) |
| FW-8 | RLE non-aligned OOB | qmk | ✅ fixed (PR #112) |
| HOST-1 | Legacy plaintext window relay on by default | host | ✅ fixed (PR #133) |
| HIL-1 | Control UI bound to all interfaces | ctnd | ✅ fixed |
| HIL-2 | Self-hosted runner reachable from fork PRs | qmk *(repo settings)* | ✅ fixed (settings) |
| HIL-3 | Self-update pulls and runs `main` unverified | ctnd | 🔲 open (accepted risk, documented) |
| HIL-4 | Unauthenticated privileged SocketIO handlers | ctnd | 🟡 accepted + documented (mitigated by HIL-1) |
| HIL-5 | Firmware-filename path traversal | ctnd | ✅ fixed |
| HIL-6 | PAT in `config.yaml` with default perms | ctnd | ✅ fixed |
| HIL-7 | Sudoers wildcard admits extra `systemctl` arguments | ctnd | ✅ fixed |
| HIL-8 | Station user holds blanket passwordless root | ctnd *(rig state)* | ✅ fixed (rig) |
| HIL-9 | Operator account and Actions runner are the same user | ctnd *(rig state)* | 🟡 (1) fixed; (2) deferred by decision |

---

## 🔲 Open

### HIL-9 — the operator account and the Actions runner are the same user

Raised 2026-08-03, immediately after HIL-8 — removing blanket root revealed the next layer
rather than finishing the job.

The Actions runner executes workflow code as `thpoll`, which is also the human operator's
login. Two consequences survive HIL-8:

1. ~~**The sudo credential cache is shared.**~~ **CLOSED 2026-08-03.** The rig ran
   `Defaults timestamp_type=global` (its own stock file, `/etc/sudoers.d/010_global-tty`,
   containing exactly that one line), so one successful `sudo` authenticated the *user*,
   not the terminal, for `timestamp_timeout` (15 min default) — any process running as
   `thpoll` in that window, workflow code included, could `sudo` to root without knowing
   the password, with the operator opening the window just by working on the desktop.
   Removed, reverting sudo to its upstream default `timestamp_type=tty`: the credential is
   keyed to the authenticating terminal, and the runner service has no tty (a pty it
   allocated would be a different one). See the verification below.
2. **CI has write access to the station code.** `qmk-test.yml` force-syncs the checkout
   (`git checkout -q -f -B main origin/main`) and then runs
   `venv/bin/python -m station.test_runner` from it. So workflow code can modify station
   code that later runs as `thpoll` under systemd — an escalation that needs no sudo at
   all, just patience.

(2) remains, and is gated in practice by HIL-2 (fork PRs need approval) — which is again
doing more work than a settings toggle should have to.

**Partial mitigation applied 2026-08-03.** Chosen over `timestamp_timeout=0`, which buys
the same thing but makes the operator retype a password on every single `sudo`; per-tty
scoping keeps normal ergonomics within one terminal. Verified with two terminals:

```console
# terminal A
$ sudo -k && sudo true      # authenticate A
# terminal B
$ sudo -n true
sudo: a password is required
```

B failing is the proof — before the change it succeeded silently by borrowing A's
credential, which is precisely what workflow code could do. Nothing on the rig depends on
the shared cache: `self-update.sh` and the UI use `sudo -n` against NOPASSWD grants, and
`flash.py`'s `uhubctl`/`picotool` are NOPASSWD too — none consult a timestamp. The only
visible change is a password once per terminal instead of once per 15 minutes across all
of them.

**The full fix for (2) is a dedicated unprivileged runner user**, separate from the login.
Not attempted, because it is more than a `useradd` — anyone taking it on needs:

- group membership for the hardware the tests drive: `gpio` (RPi.GPIO in `flash.py`) and
  `plugdev` (hidraw, per `99-polykybd.rules`);
- the `uhubctl` and `picotool` scoped grants — the HIL job really does flash through
  `sudo`, so those follow the runner user, not the operator;
- **write access to the station checkout**, because of the force-sync above. That is the
  awkward part: it either re-opens consequence 2 for the new user, or the sync has to move
  somewhere the runner cannot write to;
- re-registration of the runner under the new account (`svc.sh install <user>`).

**Decision 2026-08-03: (2) is deferred, not rejected.** The dedicated-runner-user work is
recorded as a recommendation in the rig setup guide (`README.md` §5d) so a new rig build
sees it at the point it would be cheapest to do — before the runner is registered — rather
than only in this tracker. Revisit if the rig ever serves a repo taking outside
contributions routinely; until then HIL-2's approval gate is what keeps untrusted code off
the box.

⚠️ Like HIL-8 this is **rig state, not repo state** — re-check with `sudo -l` and
`systemctl show -p User actions.runner.*.service` when re-auditing.

### HIL-3 — self-update pulls and runs `main` unverified (accepted risk)

`scripts/self-update.sh` fetches `origin/$BRANCH` (default `main`), `git merge --ff-only`s
it and `systemctl restart`s the station. There is no signature or tag verification, so
anyone who can push to `main` — or any compromised token with push rights — gets code
execution on the rig at the next timer tick (≤5 min).

Mitigating factors: push access to `main` is already fully trusted, the merge is
`--ff-only` (no history rewrite), and the update defers while the rig is busy.

**If we ever want to close it:** require signed commits on `main` and verify with
`git verify-commit` before the fast-forward, or pin the rig to reviewed release tags
instead of a branch head. Not urgent. Note that HIL-2 and HIL-3 compound — runner
compromise is a plausible route to obtaining push credentials — so the urgency of this one
now rests on HIL-2's *settings* state holding, which nothing in the repo enforces.

---

## 🟡 Accepted and documented

### HIL-4 — the control UI has no authentication at all

Every SocketIO handler in `station/ui/app.py` is unauthenticated — not just the privileged
four originally flagged. The full set: `flash`, `usb_power`, `bootsel`, `reset_board`,
`set_handedness`, `run_tests`, `run_perf`, `run_diagnostics`, `restart_runner`,
`reregister_runner`, `update_now`. Several shell out via `sudo` (systemctl, the runner
register script) or drive GPIO and the flasher.

**Decision: no auth layer.** The UI is a local touch kiosk; HIL-1 keeps it on loopback and
scopes the SocketIO CORS origins to `localhost`, so there is no remote caller to
authenticate in the default configuration. Adding a token would mean the kiosk browser
carrying a secret for no gain.

⚠️ **The whole mitigation is the loopback bind.** Setting `ui.allow_lan: true` hands full,
unauthenticated rig control to every host that can reach the port *and* widens CORS to
`"*"`. If remote access is needed, use an SSH tunnel to the loopback bind rather than
`allow_lan`. This caveat is repeated in `config/config.yaml.example`, which is where
someone about to make the mistake will actually read it.

If `allow_lan` ever becomes a routine deployment mode, revisit this — a shared-secret
token on connect (required only when `allow_lan` is set) is the intended next step.

⚠️ HIL-4 is also the **amplifier** for every station-user privilege: the UI runs as that
user, so anything the station user can do without a password (see HIL-7, and HIL-8 for
why that set was, until 2026-08-03, *everything*) is reachable by whoever can reach the
UI. Weigh new sudo grants with that in mind.

---

## ✅ Fixed — verification notes

Kept because "is this actually fixed?" was re-asked once per finding; these are the checks
that answer it.

### HIL-8 — the station user held blanket passwordless root

Found 2026-08-03 while verifying HIL-7 on the rig, by the check that was supposed to
prove the new grant was exact:

```console
$ sudo /usr/local/sbin/polykybd-runner-ctl status   # expected: refused by sudo
usage: polykybd-runner-ctl {start|stop|restart}     # actual: it RAN, as root
```

`status` is not one of the three permitted commands, so something else was granting it.
`sudo -l` on the rig:

```text
(ALL : ALL) ALL          ← sudo group membership (password required)
(ALL) NOPASSWD: ALL      ← /etc/sudoers.d/010_pi-nopasswd, stock Raspberry Pi OS
```

**Consequence: every scoped grant in this repo is decorative on a stock rig.** The HIL-7
wrapper, `polykybd-update`, `polykybd-uhubctl`, `polykybd-usb` — the station user does not
need any of them, it can ask for root directly. And because the Actions runner executes
workflow code **as that user**, on such a rig *code execution on the rig* and *root on the
rig* are the same thing. That is what made HIL-2's approval setting load-bearing well
beyond what its own entry claimed.

**What it costs to remove** — checked against the tree, not assumed. Every automated path
is already covered by a scoped grant:

| Path | Needs | Covered by |
|---|---|---|
| `flash.py` | `sudo uhubctl`, `sudo picotool` | `polykybd-uhubctl`, `polykybd-usb` |
| UI restart / self-update | `sudo -n systemctl …` on two units | `polykybd-update` |
| UI ⟳ Restart, ↻ Re-register | `sudo polykybd-runner-ctl …` | `polykybd-runner` |
| `register-runner.sh` `run_as_ctnd()` | `sudo -u $CTND_USER` | n/a — skipped, already that user |
| **HIL CI jobs** | nothing | `qmk-test.yml` contains no `sudo` at all |

Only two things start prompting, both interactive admin operations where that is correct:
first-time runner installation (`sudo ./svc.sh install`, in the full registration path —
*not* the kiosk button) and `setup.sh` itself.

**Remediated on the rig 2026-08-03** — `/etc/sudoers.d/010_pi-nopasswd` removed, leaving
`(ALL : ALL) ALL` (password required) plus the scoped NOPASSWD grants. ⚠️ Confirm
`sudo passwd -S <user>` reports `P` before removing it on any rig: with no usable password
this takes away sudo entirely.

⚠️ **Verify by whether sudo PROMPTS, not by whether it refuses.** The obvious check —
"a non-granted command must be refused" — is wrong for any user in the `sudo` group:
`(ALL : ALL) ALL` permits everything *with a password*, so a non-granted command prompts,
it is never refused. Worse, `timestamp_type=global` means a recent `sudo` elsewhere makes
it run with no prompt at all, which looks exactly like the blanket rule still being there.
That false negative cost a round here. Clear the cache first:

```console
$ sudo -k
$ sudo /usr/local/sbin/polykybd-runner-ctl status   # PROMPTS  → blanket rule is gone
$ sudo -k
$ sudo /usr/local/sbin/polykybd-runner-ctl start    # no prompt → scoped grant works
```

Both confirmed on the rig. The `status` run also exercised the HIL-7 wrapper end to end on
real hardware: sudo let it through on the password, and the wrapper itself rejected the
action with its usage message and exit 2.

`setup.sh` **warns** when it detects this (`warn_if_blanket_sudo`) rather than removing
it — pulling a distro file out from under an operator who may have no password set is not
something an install script should do unasked. The warning is what stops the next person
installing scoped grants and reasonably assuming they mean something.

⚠️ **This finding is rig state, not repo state** — nothing in the repo can pin it, and a
reimaged Pi will have it again, which is why `setup.sh` warns rather than assuming. Re-check
with `sudo -l` when re-auditing. Removing the blanket rule does not finish the job: see
HIL-9 for what it exposed.

### HIL-7 — sudoers wildcard admitted extra `systemctl` arguments

Raised by CodeRabbit on PR #51, recorded there, fixed separately.

The grant `setup.sh` used to install was:

```text
$CTND_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start actions.runner.*, … stop …, … restart …
```

Sudoers matches command-line arguments as a single concatenated string and `*` **matches
space characters**, so that did not mean "one unit whose name starts with
`actions.runner.`" — it permitted any argument tail beginning with that prefix, including
further unit names and `systemctl` options. Because the control UI runs as the station
user with no authentication (HIL-4), whatever it admitted was reachable by anyone who
could open the page.

**The fix is not a tighter pattern** — sudoers cannot express "one word", so no wildcard
formulation is safe. Instead the unit name was taken out of the caller's hands:

- `scripts/runner-ctl.sh` (installed as `/usr/local/sbin/polykybd-runner-ctl`, root-owned
  `0755`) takes **exactly one argument** from `{start, stop, restart}` and **discovers the
  unit itself** with the same query `register-runner.sh` uses. No caller-supplied string
  reaches `systemctl`, so there is nothing to inject.
- The sudoers rule is now three **literal** commands with no arguments to fill in:
  `polykybd-runner-ctl start`, `… stop`, `… restart`.
- `register-runner.sh` calls the wrapper via `runner_ctl()`, falling back to the old
  `sudo systemctl` path when the wrapper is absent — so a rig provisioned before this
  change keeps its recovery button until `setup.sh` re-runs.

⚠️ **The wrapper must be granted at its `/usr/local/sbin` path, never at its repo path.**
The station user can write to the checkout, and self-update rewrites it unattended, so a
sudo grant pointing into the repo would hand back exactly the escalation this removes.
That also means **a change to `runner-ctl.sh` only takes effect once `setup.sh` has re-run
on the rig** — the installed copy is what the grant refers to.

`tests/runner_ctl_test.py` pins the refusal paths (extra arguments, unknown actions,
missing/odd unit names) against a fake `systemctl` on `PATH`, because the security value
here is entirely in what the script *refuses* and none of that is visible from reading the
sudoers rule.

**Deploy step:** `sudo bash scripts/setup.sh --units-only` on the rig installs the wrapper
and replaces the grant. Until then the rig still carries the wildcard. Confirm with
`sudo -l | grep runner-ctl`.

Not established, and deliberately not claimed: whether the old wildcard had a concrete
root-code-execution path (it depends on what `systemctl` does with a second argument, and
the dev container has no systemd PID 1 to test against). The fix does not rest on that
question — the argument tail should never have been reachable either way.

Still worth a look while in the area: `/etc/sudoers.d/polykybd-usb` uses the same
grant shape for `uhubctl`/`picotool` and was not part of this change.

### HIL-2 — self-hosted runner RCE from fork PRs

`.github/workflows/qmk-test.yml` in the **public** `thpoll83/qmk_firmware` repo runs two
jobs on the rig (`runs-on: [self-hosted, polykybd-ctnd]`) and triggers on
`pull_request: [opened, synchronize, reopened, labeled]`. A fork PR that reached the
runner would execute attacker-controlled code on the Pi — which holds the GitHub PAT,
drives GPIO, and can flash the keyboard.

Closed by tightening the repo setting (Settings → Actions → General → *Fork pull request
workflows from outside collaborators*) to **Require approval for all external
contributors**. GitHub's default is only *first-time* contributors, which is not enough:
one merged trivial PR would earn an attacker unreviewed runner access from then on.

**Verified 2026-08-03** by the repo owner:

```console
$ gh api repos/thpoll83/qmk_firmware/actions/permissions/fork-pr-contributor-approval
{ "approval_policy": "all_external_contributors" }
```

⚠️ **This one is a settings state, not code, so nothing in the repo pins it** — it can be
changed back at any time with no diff, no review and no CI signal, and the affected
workflows keep passing either way. Re-run the command above when re-auditing rather than
trusting this entry. (Note it is not readable from a Claude Code session: the agent proxy
refuses `/actions/permissions/*` with *"Access to this GitHub Actions path is not
permitted through this proxy"*, so it has to be checked by a human or from CI.)

Still worth considering as defence in depth, since approval is now the only thing standing
between a fork PR and the hardware: isolate the runner on its own network segment, and
stop keeping a long-lived PAT on the box.

#### Why only `qmk_firmware` and not the other repos

The question comes up every time, so: the setting is deliberately **not** applied to the
other seven repos. The risk is not "fork PRs run CI", it is "fork PRs run CI **on our
Pi**", and only `qmk_firmware` has a self-hosted job a fork PR can reach. Surveyed
2026-08-03 — all eight repos are public, so fork PRs are possible everywhere:

| Repo | Self-hosted job reachable by a fork PR? |
|---|---|
| `qmk_firmware` | **yes** — 2 jobs `runs-on: [self-hosted, polykybd-ctnd]`, triggered by `pull_request` |
| `polykybd-ctnd` | no — its workflow *mentions* `self-hosted` but is the reference copy, `on: workflow_dispatch` only (needs write access to fire) |
| `PolyKybdHost`, `polykybd-docs`, `wincompose`, `Adafruit-GFX-Library` | no — all `ubuntu-latest` |
| `PolyKybd`, `gnome-wayland-winreader` | no workflows |

On `ubuntu-latest` an attacker gets an ephemeral VM that GitHub destroys afterwards, with a
read-only `GITHUB_TOKEN` and no secrets (`pull_request` from a fork withholds them by
design) — running untrusted code there is what it is for. On the rig they get a persistent
box holding the PAT, wired to GPIO, able to flash the keyboard. Same trigger, entirely
different blast radius. Enabling approval on the other repos buys ~nothing and costs a
manual click before every contributor's CI run.

**The rule to apply going forward is "does a fork PR reach a self-hosted runner?", not "is
this repo public?"** If a `runs-on: [self-hosted, …]` job is ever added to another repo,
that repo needs this setting on the same day.

⚠️ **The approval setting does not gate `pull_request_target`.** That trigger runs in the
*base* repo context with secrets and a write token no matter what the fork-PR policy says.
`qmk_firmware` has one — the stock upstream `labeler.yml` — which is safe as written
because it runs on `ubuntu-latest` and never checks out the PR's code. If any
`pull_request_target` workflow ever gains a checkout of
`github.event.pull_request.head.sha`, that is a full compromise regardless of HIL-2.

### HIL-1 — UI bind

`station/config.py`: `ALLOW_LAN` is parsed with a strict `_as_bool()` (plain `bool()`
would make the string `"false"` truthy), and without it a non-loopback `ui.host` is
**overridden** to `127.0.0.1` with a warning rather than honoured — so an already-deployed
rig whose gitignored `config.yaml` still says `0.0.0.0` is protected by the upgrade.
`UI_CORS_ORIGINS` is scoped to the three localhost origins unless `ALLOW_LAN`.

### HIL-5 — firmware path traversal

`station/ui/app.py` `_selected_firmware()`. Verified against each escape shape:

| Input | Rejected by |
|---|---|
| `/etc/passwd` | `Path(name).name != name` (`'passwd' != '/etc/passwd'`) |
| `a/../b` | same (`'b' != 'a/../b'`) |
| `..` | passes the name check (`Path('..').name == '..'`), then caught by `path.parent != root` and `is_file()` |
| symlink in `firmware/` → outside | `resolve()` follows the link, so `path.parent != root` |

Only a bare filename resolving to a real file directly in `FIRMWARE_DIR` is accepted.

### HIL-6 — PAT file permissions

`config.yaml` holds a GitHub PAT that needs `Administration: read/write` to mint runner
registration tokens. `scripts/setup.sh` now `chown`s it to the station user and `chmod`s
it `600` **on every run**, not only when creating it from the example — an existing rig
was provisioned before this line and still carries the `0644` umask default, so re-running
`setup.sh` is what fixes it in the field.

**Rig remediated 2026-08-03** — the deployed `config.yaml` was `chmod 600`'d by hand
(faster than a full `setup.sh` re-run on a healthy rig), and the **PAT was rotated**. The
rotation is the part that mattered: the file had been world-readable while HIL-2 still let
returning contributors run code on the box, so tightening the mode afterwards would not
have helped if the old token had been read. Any token predating 2026-08-03 is
revoked. The initial `cp` also runs under `umask 077`, so
the file is never briefly world-readable; note that the window it closes never contained a
secret (a freshly-copied file is the example, whose `token` is empty) — the point is that
the invariant should not depend on that remaining true.

---

## ⚠️ FW-2 — firmware signing is warn-only until enforcement is switched on

The Ed25519 image-signature check ships **warn-only**: the verification result is logged,
never enforced. Progress toward enforcing authenticity, in the order the steps must happen:

1. ✅ **Done 2026-08-04** (qmk PR #183) — real keypair generated with
   `tools/gen_signing_key.py`; `base/fw_pubkey.h` committed with the real public key (no
   longer the all-zero placeholder). The private half was generated on the maintainer's
   machine and never entered the repo or a transcript. Build + HIL both green with it.
2. ✅ **Done 2026-08-04** — `FW_SIGNING_KEY` set; `PolyKybd-fw-v0.9.94` ships
   `polykybd_split72_default.bin.sig` (64 B). Verified beyond "the asset exists": the
   released `.bin` + `.sig` check out against the committed `fw_pubkey.h`, so the secret's
   private key and the firmware's public key are a confirmed pair.
   ⚠️ Two traps hit here, both costing a release run. The signing step fails **quietly**
   when the secret is *absent* (`::notice::FW_SIGNING_KEY not set`, unsigned release, job
   still green) — so check for the `.sig`, never a green run. And when the secret is
   *malformed* the job fails **loudly but misleadingly**: `base64.b64decode` discards
   non-alphabet characters while keeping letters, so a value short by one character (or
   carrying the tool's label) raises `binascii.Error: Incorrect padding` with the trailing
   `=` plainly present — which reads as a padding problem and sends you to the wrong place.
   qmk PR #184 replaces that with the character count and a regeneration one-liner.
3. ✅ **Done 2026-08-04** — flashed `PolyKybd-fw-v0.9.94` over HID; the console printed
   `FW_UP: image signature OK`. That is the first moment the private key, the committed
   public key and the firmware's Monocypher verifier were shown to agree on hardware.
   ⚠️ **The verdict is invisible from the host log** — a flash runs under
   `worker.exclusive()`, which suspends the console-read periodic, and QMK drops console
   output nobody drains. Capture it with `PolyKybdHost/tools/poly_console.py` in a second
   terminal (`qmk console` refuses to run outside MSYS2 MinGW64 on Windows). The verdict
   prints at COMMIT, before APPLY reboots, so staging alone is enough — no need to apply.
   ⚠️ Before step 4, also confirm the **negative** cases the same way: no `.sig` →
   `UNSIGNED`, and a byte-flipped `.sig` → `INVALID`. Enforcement rejects on `sig != 1`,
   so a verifier that wrongly returned OK for a bad signature would make step 4 pure
   theatre — and the passing case looks identical either way.
4. 🔲 Add `OPT_DEFS += -DFW_REQUIRE_SIGNATURE` to `keyboards/polykybd/rules.mk`.

Note that after step 1 a keyboard logs `UNSIGNED` for every flash until releases are
actually signed. That is expected, not a regression.

⚠️ **Step 4 only after 1–3** — otherwise the firmware refuses to flash anything, including
the image that would undo it. Full procedure:
`qmk_firmware/keyboards/polykybd/tools/SIGNING.md`.

Enforcement needs **no slave-side work**: verification is master-only by design, on the
argument that reaching the slave requires a cable to the UART bridge, and anyone with that
access can flash over BOOTSEL anyway — so slave verification would add
split-transaction-window risk for no real threat reduction.
