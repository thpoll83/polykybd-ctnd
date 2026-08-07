# PolyKybd security audit — findings tracker

Cross-repo tracker for the security audit findings (`FW-*` firmware, `HOST-*` host app,
`HIL-*` rig/CI). It lives here because most of the still-open items are rig items, but it
covers all four repos — check the **Where** column before going looking for code.

Status verified against: `polykybd-ctnd` @ `0d1463a`, `qmk_firmware` @ `0.11.0`,
`PolyKybdHost` @ `0.11.0`, `polykybd-docs` @ PR #35. **2026-08-06**: HOST-3 added alongside
the telemetry feature — a single new surface reviewed on its own, not a fresh sweep of the
tracker, so the review scope below still stands. Last full review **2026-08-05 (evening)** —
a *fresh* evaluation of the surfaces FW-2 changed, plus one never examined before. It
raised **FW-9** (executable DOOM pack is unsigned — it bypasses FW-2 entirely), FW-10 and
HOST-2. Earlier the same day: FW-2 enforced, with the unsigned-build escape hatch moved to
an on-keycap ACCEPT/REJECT prompt. Previous review 2026-08-04 (HIL-2 confirmed set; HIL-6
remediated on the rig; HIL-7 raised and fixed; HIL-8 raised and remediated; HIL-9 raised
and partly mitigated; FW-2 key provisioned).

⚠️ **Read FW-9 before concluding that firmware signing closes the code-execution
surface. It does not.**

> The finding IDs originate from an audit that was only ever held in session context. This
> file is the first committed record of them, reconstructed and re-verified against the
> code — so treat the *status* here as authoritative and the *numbering* as historical.

## Status at a glance

| ID | Title | Where | Status |
|---|---|---|---|
| FW-1 | ROI clamp | qmk | ✅ fixed |
| FW-2 | Firmware image signing (Ed25519) | qmk / host / docs | ✅ enforced — key provisioned, both verdicts confirmed on hardware |
| **FW-9** | **Executable DOOM pack (`.plyx`) is CRC-checked, not signed — arbitrary code execution, bypasses FW-2** | qmk | 🔲 **open (high)** |
| FW-10 | Unsigned-flash confirmation is a repeatable input DoS (60 s modal) | qmk | 🟡 accepted + documented |
| FW-3 / FW-5 | Dynamic-keymap buffer OOB | qmk | ✅ fixed (PR #112) |
| FW-4 | `get_overlay` OOB | qmk | ✅ fixed |
| FW-6 | (note only) | qmk | ✅ closed (PR #112) |
| FW-7 | Plain-overlay 1-byte over-read | qmk / host / rig | ✅ fixed as the protocol-11 reframe (#120 / #96 / #43) |
| FW-8 | RLE non-aligned OOB | qmk | ✅ fixed (PR #112) |
| HOST-1 | Legacy plaintext window relay on by default | host | ✅ fixed (PR #133) |
| HOST-2 | Confirmation polling holds `worker.exclusive()` for up to 75 s | host | 🟡 accepted + documented |
| HOST-3 | Usage telemetry: new outbound surface, unauthenticated collector, on by default | host | 🟡 accepted + documented |
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

### FW-9 — the executable DOOM pack is CRC-checked, not signed (bypasses FW-2)

Raised **2026-08-05**, by a fresh look at what FW-2 does *not* cover. FW-2 signs the
**firmware image**. It does not sign the **DOOM engine pack** (`.plyx`) — which is
executable code, flashed over the same HID transport, and *called* by the firmware.

`doom/doom_pack_load.c` validates a flashed pack with: the `PlyX` magic, `abi ==
DOOM_PACK_ABI`, `image_size` fits the slot, the `ram_base`/`ram_size` pairing, and a
**CRC32** over the body. Every one of those is an integrity/compatibility check. **None
authenticates the author** — a CRC32 is trivially satisfied by whoever crafts the image.
It then does:

```c
doom_pack_init_fn init = (…)(slot + sizeof(*hdr) + hdr->entry_off + 1u);
const doom_pack_api_t *api = init(&s_fw_api);
```

i.e. it **branches into attacker-supplied bytes at an attacker-supplied offset**, on a
Cortex-M0+ with no MPU. Once executing, the code is not confined to `s_fw_api` — it has
the whole address space, including the flash-write routines.

**The chain needs no physical access and no user interaction:**

1. Flash a crafted `.plyx` (cmds `0x50`–`0x52`, DOOMPACK target). No signature is checked
   on this path — `fw_staging_check_signature()` is only called in the `FW_TARGET_FIRMWARE`
   branch of `fw_staging_finalize_impl`.
2. Set the idle style to `IDLE_STYLE_IDDQD` (2) over **HID cmd 28** — in range, so it is
   accepted.
3. Wait for the keyboard to idle. `doom_begin` → `doom_session_start` →
   `doom_pack_load()` → the call above.

So an attacker who can open the raw HID interface gets **arbitrary code execution with
full firmware privilege**, which is precisely the outcome FW-2 exists to prevent. It
applies to shipped keyboards: `release.yml` builds the `POLYKYBD_DOOM_PACK` flavour, and
the `.plyx` is a published release asset.

**Fix (recommended): verify the pack with the existing Ed25519 machinery, at LOAD time.**
The verifier and `FW_SIGNING_PUBKEY` are already compiled in (`base/crypto/`,
`base/fw_pubkey.h`). Put the 64-byte signature in the PlyX header (a `PACK_VERSION` bump)
and check it in `doom_pack_load()` immediately before computing `init`. Verify at *load*,
not at COMMIT: flash can be rewritten afterwards, so a "was validated once" flag is not a
control. The cost is one SHA-512 over ~211 KB at session start — the loader already walks
the whole image for the CRC there, so it is the same order of work, once per game session,
not per frame. `release.yml` signs the `.plyx` alongside the `.bin`.

**Interim mitigation** if that is not done promptly: build releases without
`POLYKYBD_DOOM_PACK`. That removes the feature, so it is a stopgap, not a fix.

⚠️ **The same "flashed over HID, authenticated by CRC only" property applies to the WAD
(`.whx`) and the font-pack bundles (`.plyf`)** — but those are *data*, so the exposure is
parser bugs in `fontpack.c` / the WAD reader rather than direct code execution. Lower
severity, same root cause: the resource-flash path has no notion of authenticity.

### FW-10 — the unsigned-flash confirmation is a repeatable input DoS

Raised **2026-08-05**, as a known cost of the FW-2 escape hatch rather than a defect.

Reaching COMMIT with an unsigned image raises the on-keycap prompt, which blanks every
keycap and makes `process_record_user` swallow **all** key events for up to
`FW_CONFIRM_WINDOW_MS` (60 s). Anyone who can talk raw HID can do this, repeatedly, by
streaming ~446 KB and committing — so the keyboard can be held unusable in 60-second
windows.

**Accepted.** Severity is bounded and the trade is clearly worth it:

- Pressing **R** ends it instantly, and the status OLED says what is being asked, so the
  user is not left guessing.
- The attacker is one who could *already* flash arbitrary firmware before FW-2 was
  enforced. This is what a total-compromise capability was reduced *to*.
- A flash already disrupts typing regardless (`poly_prepare_for_flash` drops to the base
  layer), so the marginal new capability is the 60 s modal window.

Revisit only if the window is ever raised, or if the prompt is made reachable without a
completed, CRC-valid transfer.


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

### HOST-2 — the confirmation poll holds `worker.exclusive()` for up to 75 s

Raised **2026-08-05**. When the keyboard raises the unsigned-image prompt, the host polls
COMMIT for up to `CONFIRM_POLL_TIMEOUT_S` (75 s) — all of it inside the
`worker.exclusive()` the flash already held. For that window the daemon serves no other
device RPC: `polyctl` device calls return the "suspended" error, and the reconnect probe
does not run.

**Accepted.** A flash already monopolises the device by design, this only extends it by
the user's decision time, and the failure mode is an honest error rather than a stale
answer (see the `polyctl fw version` note in `PolyKybdHost/CLAUDE.md` — returning a cached
value instead was the actual bug). Worth revisiting only if the daemon ever needs to serve
something time-critical during a flash.


### HOST-3 — usage telemetry is a new outbound surface, and it is on by default

Raised **2026-08-06** with the telemetry feature (`PolyKybdHost/polyhost/services/telemetry.py`,
collector in `PolyKybdHost/telemetry-collector/`). Full payload + rationale in
`PolyKybdHost/docs/telemetry.md`. Four things a reviewer should know before touching it:

- **The collector endpoint is unauthenticated, deliberately.** Any credential shipped in an
  open-source client is a public credential, so pretending otherwise buys nothing. Consequences
  accepted: `install_id` is client-generated and spoofable, so the counts are a floor with noise,
  not an audit. Abuse is bounded by `UNIQUE(install_id, day)` — the Worker inserts with
  `INSERT OR IGNORE`, so a same-day repeat is silently discarded rather than erroring — plus a
  per-IP rate limit, not by authentication. **Never treat a number out of this dataset as
  attested.** ⚠️ That rate limit is a **Workers rate-limit binding**, not a WAF rule: WAF rules
  are zone-scoped and the endpoint is on `workers.dev`, which is Cloudflare's zone, not ours.
- **The payload is allow-listed at BOTH ends, and that is load-bearing.** The host process sees
  active window titles, application names and (with daylight brightness) an approximate location.
  The guarantee is the pair of **runtime** checks: `build_payload()` copies named fields only and
  never filters a dict, and the Worker re-validates rather than trusting the client. The test that
  pins the payload to a frozen key set is the *regression guard* on the first of those — it is
  what makes an accidental new field fail CI, not what stops one being sent. Do not "simplify"
  either runtime check into a passthrough.
- **The client IP reaches the collector** (it must — it is a TCP connection). The Worker derives a
  country from it and stores neither the IP nor anything derived beyond that. Cloudflare, as the
  operator, sees connections regardless; that is disclosed in the user-facing doc rather than
  papered over.
- **`TELEMETRY_ENDPOINT` ships empty** — a build that posted to a hostname we had not yet
  registered would invite someone to register it and collect the pings. Sending stays off until
  that string names a host we own.

**Consent posture: on by default, with NO in-app consent step.** ⚠️ This was originally
shipped with a first-run dialog whose dismiss/Esc path turned telemetry *off* — a fail-safe
where the ambiguous answer never sent data. That dialog was **removed** (2026-08-07,
`PolyKybdHost#153`) as an unwanted interruption on every upgrade, so the fail-safe is gone
with it: an install reports unless someone deliberately turns it off.

What carries the disclosure now, all of it passive: the **release notes** (which a user reads
at download time, before the app runs — so the notes for any release that changes the payload
are part of the disclosure, not marketing), the public *Usage Data & Privacy* page,
`PolyKybdHost/docs/telemetry.md`, the `Settings → Telemetry` checkbox, an INFO line the core
logs on every start, and `polyctl telemetry preview` printing the exact bytes.

Still a deliberate product decision — an opt-in buried in settings yields numbers too sparse
to act on — taken while the user base is a handful of known testers who were told directly.
It is the weakest part of this entry, and the part to revisit first if the install base ever
stops being people we know.

**Accepted.** Revisit if the endpoint ever grows a read API (it has none by design — the Worker
only accepts writes, so there is no route to leak the dataset), if the payload gains a field that
is not obviously non-identifying, or when the install base is large enough that "anonymous" should
mean an anonymity set rather than just an absence of identifiers.


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

## ✅ Checked and NOT vulnerable — don't re-litigate

Things this audit examined and found sound. Recorded because each one looks like a hole
until you read the guard, and re-deriving that costs more than reading it.

### Key injection is double-gated — host debug flag AND firmware debug flag

Asked directly during the 2026-08-05 review ("is the execute-command file gated by the
firmware debug flag?"). The answer has two halves and they are *different* flags:

| layer | gate | default | how it unlocks |
|---|---|---|---|
| Host — command file / `commands.execute` RPC | `allow_key_injection`, set from the host's `--debug` | off | restart the host in debug mode |
| Firmware — HID **cmd 14** itself | `debug_enable` | off | **`DB_TOGG` key — physical access** |

`PolyCore.execute_commands` runs `strip_key_injection()` (`poly_core.py`), dropping
`press`/`release` unless the host process was started in debug mode — so a command file,
or an unauthenticated caller on the control socket, cannot drive keystrokes on a
production host. Everything *else* in a command file still runs; only the injection subset
is gated.

That is a host **policy**, and a policy on the host is not a control on the keyboard — any
local process can talk raw HID directly and skip it. What actually holds is the second
gate: `hid_com.c` case 14 is wrapped in `if (debug_enable)` and NACKs otherwise. So key
injection needs a debug host **and** a physically unlocked keyboard.

⚠️ The residual to check if this is ever revisited: anything *other than* `DB_TOGG` that
can set `debug_enable` (a VIA/QMK-side debug toggle reachable over HID would collapse the
second gate to nothing). Not observed, not exhaustively ruled out.

### The FW-2 confirmation state machine cannot be raced

The prompt leaves a window — up to 60 s — between the CRC being verified and the header
being stamped. Two ways that could have gone wrong, both closed:

- **Swapping the image while the user decides.** `fw_staging_write_chunk()` rejects
  `offset >= s_image_size`, so once a complete image has been staged no further bytes can
  be written without a new BEGIN — and `fw_staging_begin` resets `s_confirm` to `IDLE`, so
  a new image always re-asks. (`write_chunk` does *not* test `s_fw_up_active`, which
  finalize has already cleared by then — the size bound is what actually holds the line.)
- **Reusing an acceptance.** `CONFIRM_ACCEPTED` is consumed by the finalize that acts on
  it, and that finalize re-checks the CRC and the signature of the staged image. One
  confirmation authorises exactly one image.

### Cancelling the prompt over HID is safe; accepting it is not exposed

COMMIT carrying `'x'` in `data[2]` resolves a pending prompt to a **refusal**. That is
reachable by anyone who can talk HID, deliberately: a cancel can only ever *deny*, so it
grants an attacker nothing they did not already have (they can simply not flash). The
worst case is refusing a legitimate user's own in-progress confirmation — an annoyance,
not an escalation. Accepting remains a keypress on the matrix and must stay that way.

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

## ✅ FW-2 — firmware signing is enforced

The Ed25519 image-signature check is **enforced**: an image without a valid signature over
the project key is not applied. The steps, in the order they had to happen:

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
   ✅ **Negative case confirmed the same day**: a byte-flipped `.sig` produced
   `FW_UP: image signature INVALID`. This is the test that matters — enforcement rejects
   on `sig != 1`, so a verifier that returned OK for a bad signature would make step 4
   pure theatre, and the passing case cannot distinguish the two. Verified OK *and*
   INVALID, so the check genuinely discriminates. ✅ **`UNSIGNED` exercised 2026-08-05**
   as a side effect of testing the confirmation prompt — an unsigned developer build now
   has its own path, so all three verifier branches have been seen on hardware.

   ⚠️ **UNSIGNED and INVALID are not the same event and must not share a path.** No
   signature is a developer build; a signature that fails to verify is an image that is
   not what it claims to be. The physical confirmation is offered only for the first.
   Offering it for the second would hand an attacker the one thing the gate exists to
   withhold — a user who has been told to press A.
4. ✅ **Done 2026-08-05** (qmk PR #186) — `OPT_DEFS += -DFW_REQUIRE_SIGNATURE` in
   `keyboards/polykybd/rules.mk`. Enabled only after step 3's *negative* case, since a
   verifier that rubber-stamped would make enforcement theatre.

   The escape hatch for unsigned developer builds is an **on-keycap confirmation**, not a
   host dialog: at COMMIT the keyboard blanks every keycap except a big **A / ACCEPT** on
   the left half's home-row index key and **R / REJECT** on the right's, and waits 60 s for
   a press. COMMIT answers a new `?` status meanwhile and the host re-polls — it must not
   block, because COMMIT runs on the same main loop that scans the matrix, so a busy-wait
   would guarantee the keypress is never seen.

   ⚠️ **Accepting must stay physical.** FW-2's threat model is *any process that can talk
   the HID flash protocol*, so an acknowledgement carried over that channel is forgeable by
   exactly the attacker it is meant to stop. Cancelling the prompt (COMMIT with `'x'` in
   `data[2]`) *is* exposed over HID, because a cancel can only ever deny — the host's abort
   path and the HIL rig use it rather than leaving the board modal for the full window.
   Do not turn the accept side into a host-side checkbox.

Note that after step 1 a keyboard logs `UNSIGNED` for every flash until releases are
actually signed. That is expected, not a regression.

⚠️ **Step 4 only after 1–3** — otherwise the firmware refuses to flash anything, including
the image that would undo it (BOOTSEL/UF2 remains the unconditional recovery path, since it
bypasses `fw_staging` entirely). Full procedure:
`qmk_firmware/keyboards/polykybd/tools/SIGNING.md`.

Enforcement needs **no slave-side work**: verification is master-only by design, on the
argument that reaching the slave requires a cable to the UART bridge, and anyone with that
access can flash over BOOTSEL anyway — so slave verification would add
split-transaction-window risk for no real threat reduction.
