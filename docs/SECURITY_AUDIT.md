# PolyKybd security audit — findings tracker

Cross-repo tracker for the security audit findings (`FW-*` firmware, `HOST-*` host app,
`HIL-*` rig/CI). It lives here because most of the still-open items are rig items, but it
covers all four repos — check the **Where** column before going looking for code.

Status verified against: `polykybd-ctnd` @ `61e170c`, `qmk_firmware` @ `0.9.90`,
`PolyKybdHost` @ `0.10.6`, `polykybd-docs` @ PR #28. Last review **2026-08-02**.

> The finding IDs originate from an audit that was only ever held in session context. This
> file is the first committed record of them, reconstructed and re-verified against the
> code — so treat the *status* here as authoritative and the *numbering* as historical.

## Status at a glance

| ID | Title | Where | Status |
|---|---|---|---|
| FW-1 | ROI clamp | qmk | ✅ fixed |
| FW-2 | Firmware image signing (Ed25519) | qmk / host / docs | ⚠️ **warn-only** — see below |
| FW-3 / FW-5 | Dynamic-keymap buffer OOB | qmk | ✅ fixed (PR #112) |
| FW-4 | `get_overlay` OOB | qmk | ✅ fixed |
| FW-6 | (note only) | qmk | ✅ closed (PR #112) |
| FW-7 | Plain-overlay 1-byte over-read | qmk / host / rig | ✅ fixed as the protocol-11 reframe (#120 / #96 / #43) |
| FW-8 | RLE non-aligned OOB | qmk | ✅ fixed (PR #112) |
| HOST-1 | Legacy plaintext window relay on by default | host | ✅ fixed (PR #133) |
| HIL-1 | Control UI bound to all interfaces | ctnd | ✅ fixed |
| HIL-2 | Self-hosted runner reachable from fork PRs | qmk *(repo settings)* | 🔲 **open — needs a settings decision** |
| HIL-3 | Self-update pulls and runs `main` unverified | ctnd | 🔲 open (accepted risk, documented) |
| HIL-4 | Unauthenticated privileged SocketIO handlers | ctnd | 🟡 accepted + documented (mitigated by HIL-1) |
| HIL-5 | Firmware-filename path traversal | ctnd | ✅ fixed |
| HIL-6 | PAT in `config.yaml` with default perms | ctnd | ✅ fixed |
| HIL-7 | Sudoers wildcard admits extra `systemctl` arguments | ctnd | 🔲 **open — needs a decision** |

---

## 🔲 Open

### HIL-2 — self-hosted runner RCE from fork PRs (Critical; settings, not code)

`.github/workflows/qmk-test.yml` in the **public** `thpoll83/qmk_firmware` repo runs two
jobs on the rig (`runs-on: [self-hosted, polykybd-ctnd]`, lines 101 and 312) and triggers
on `pull_request: [opened, synchronize, reopened, labeled]`. A fork PR that reaches the
runner executes attacker-controlled code on the Pi — which holds the GitHub PAT, drives
GPIO, and can flash the keyboard.

**Fix (no code change):** qmk_firmware → Settings → Actions → General → *Fork pull request
workflows from outside collaborators* → **Require approval for all external
contributors** (GitHub's default is only *first-time* contributors, which is not enough —
one merged trivial PR earns an attacker unreviewed runner access forever). Consider also
isolating the runner (own network segment, no long-lived PAT on the box).

This is the highest-impact open item and costs one settings change.

### HIL-7 — sudoers wildcard admits extra `systemctl` arguments

Raised by CodeRabbit on PR #51 and recorded rather than fixed there — it is pre-existing
code outside that PR's diff, and the safe fix is bigger than a one-liner.

`scripts/setup.sh` installs:

```text
$CTND_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start actions.runner.*, … stop …, … restart …
```

Sudoers matches command-line arguments as a single concatenated string, and `*` **matches
space characters**. So the rule does not mean "one unit whose name starts with
`actions.runner.`" — it permits any argument tail beginning with that prefix, including
further unit names and `systemctl` options. The station user gets that grant, and the
control UI runs as the station user with no authentication (HIL-4), so the two compound:
whatever this rule ultimately permits is reachable by anyone who can open the UI.

**Not yet established:** whether a concrete root-code-execution path exists through it
(e.g. whether `systemctl` will act on an attacker-writable unit *file path* supplied as a
second argument). That needs checking on the rig — there is no systemd PID 1 in the dev
container to test against. The wildcard-matches-spaces behaviour itself is documented
sudoers semantics and is not in doubt.

**Candidate fixes:**
- a small **root-owned wrapper** that accepts exactly one of `start|stop|restart` plus one
  unit name it validates against `^actions\.runner\.[A-Za-z0-9_.-]+\.service$`, with the
  sudoers rule scoped to the wrapper. Most robust; costs a new script and a
  `register-runner.sh` change.
- or generate an **exact** sudoers rule at registration time, once the real unit name is
  known (it is `actions.runner.<owner>-<repo>.<name>.service`, not knowable at
  `setup.sh` time — which is why the wildcard is there).

The same pattern is worth re-checking in `/etc/sudoers.d/polykybd-usb` while doing this.

### HIL-3 — self-update pulls and runs `main` unverified (accepted risk)

`scripts/self-update.sh` fetches `origin/$BRANCH` (default `main`), `git merge --ff-only`s
it and `systemctl restart`s the station. There is no signature or tag verification, so
anyone who can push to `main` — or any compromised token with push rights — gets code
execution on the rig at the next timer tick (≤5 min).

Mitigating factors: push access to `main` is already fully trusted, the merge is
`--ff-only` (no history rewrite), and the update defers while the rig is busy.

**If we ever want to close it:** require signed commits on `main` and verify with
`git verify-commit` before the fast-forward, or pin the rig to reviewed release tags
instead of a branch head. Not urgent, but note that HIL-2 and HIL-3 compound: runner
compromise (HIL-2) is a plausible route to obtaining push credentials (HIL-3).

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
user, so anything the station user can do without a password (see HIL-7) is reachable by
whoever can reach the UI. Weigh new sudo grants with that in mind.

---

## ✅ Fixed — verification notes

Kept because "is this actually fixed?" was re-asked once per finding; these are the checks
that answer it.

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
`setup.sh` is what fixes it in the field. The initial `cp` also runs under `umask 077`, so
the file is never briefly world-readable; note that the window it closes never contained a
secret (a freshly-copied file is the example, whose `token` is empty) — the point is that
the invariant should not depend on that remaining true.

---

## ⚠️ FW-2 — firmware signing is warn-only until keys are provisioned

The Ed25519 image-signature check ships **warn-only**: `base/fw_pubkey.h` is an all-zero
placeholder and the verification result is logged, never enforced. To actually enforce
authenticity, in this order:

1. `python3 keyboards/polykybd/tools/gen_signing_key.py …` — writes `base/fw_pubkey.h`
   (commit it) and a private key that must **not** enter the repo.
2. Add that private key as the `FW_SIGNING_KEY` secret in `qmk_firmware`; release CI then
   signs the `.bin` and ships a `.bin.sig` alongside it.
3. Add `OPT_DEFS += -DFW_REQUIRE_SIGNATURE` to `keyboards/polykybd/rules.mk`.

⚠️ **Step 3 only after 1 and 2** — otherwise the firmware refuses to flash anything,
including the image that would undo it. Full procedure:
`qmk_firmware/keyboards/polykybd/tools/SIGNING.md`.
