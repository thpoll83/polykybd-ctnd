---
name: verify-security-finding
description: Triage a security finding from an external scanner (Aikido, Snyk, Dependabot, CodeQL, a pasted audit report) against the PolyKybd repos before changing anything — establish whether the flagged file is ours or inherited upstream, whether it is compiled or reachable at all, and whether our inputs can reach the vulnerable path; then fix it, or record it as inert. Use this whenever a report names a file and a line number and asks "is this real?", and before opening any PR in response to one. Always ends by writing the disposition into polykybd-ctnd/docs/SECURITY_AUDIT.md, including for findings that need no code change.
---

# Verify a security finding

A scanner walks paths. This project is a **fork of a 30k-commit upstream** with a
**vendored third-party game engine**, a **committed build directory** or two, and
several repos — so most of what a path-based scanner can see was written by someone
else and much of it never enters a shipped artifact. The finding is usually a true
statement about the *file* and a false statement about the *product*.

Measured on the 2026-08-29 Aikido scan: **2 of 5 findings were real**, neither of
them where the report was looking, and the two most valuable outputs of the session
came from the verification rather than from the findings.

**The deliverable is a disposition per finding, recorded in the tracker — never a
silent dismissal.** A dismissed finding with nothing written down is re-raised in
full by the next scan at the same cost.

## 1. Restate each finding as a claim you can falsify

Split the report into one row per finding: **repo · path · line · claim**. Do not
batch them; they fail for different reasons. Resist fixing anything yet — on that
scan, two findings dissolved before any code was read.

## 2. Is the file OURS, or inherited from upstream?

```bash
cd /home/user/qmk_firmware
P=.github/workflows/ci_build_major_branch_keymap.yml
curl -sSL "https://raw.githubusercontent.com/qmk/qmk_firmware/master/$P" | diff - "$P" \
  && echo "IDENTICAL TO UPSTREAM"
```

Byte-identical means it is upstream's code; it is only *ours* to fix if we can reach
it (step 3). Also check where the path sits:

| Location | Reading |
|---|---|
| `keyboards/polykybd/**`, `modules/polykybd/**`, `polyhost/**`, `station/**` | **ours** — go to step 4 |
| `.github/workflows/**` | mostly upstream — in `qmk_firmware` only **5 of 23** are ours (see the 404 loop below) |
| `lib/**`, `quantum/**`, `platforms/**`, `drivers/**` | upstream; check `keyboards/polykybd/UPSTREAM_PATCHES.md` for the ones we do patch |
| `keyboards/polykybd/doom/engine/**` | **vendored verbatim** — see the warning in step 5 |
| any `cmake-build-*/`, `.build/`, `dist/`, `node_modules/` | a build artifact; the real question is why it is tracked (step 6) |

## 3. Is it reachable — compiled, triggered, called?

An unreachable finding cannot be a vulnerability in the shipped artifact.

**C / firmware — is the translation unit built?**
```bash
cd /home/user/qmk_firmware/keyboards/polykybd
grep -rn "textscreen" rules.mk */rules.mk *.mk 2>/dev/null   # nothing => never compiled
```
Check **both** flavours: PR CI builds only `POLYKYBD_DOOM_PACK=yes`, while the
monolithic `POLYKYBD_DOOM=yes` is built solely by the release workflow.

**A workflow — can it ever run?**
```bash
sed -n '1,40p' "$P"                                   # on:, or workflow_call only?
grep -rn "$(basename "$P" .yml)" .github/workflows/   # who calls it?
grep -n "if: github.repository" .github/workflows/<caller>.yml
```
⚠️ Upstream gates several workflows on `if: github.repository == 'qmk/qmk_firmware'`,
which is **permanently false in this fork** — such a chain is dead code here.

⚠️ **Don't guess which workflows are ours — ask upstream.** A 404 on the raw URL
means the file does not exist there, i.e. we wrote it. Measured on `qmk_firmware`
2026-08-29, the five are `bump-version.yml`, `cppcheck.yml`,
`polykybd-unit-test.yml`, `qmk-test.yml`, `release.yml`:
```bash
for f in .github/workflows/*.yml; do b=$(basename "$f")
  c=$(curl -sSL -o /dev/null -w '%{http_code}' \
      "https://raw.githubusercontent.com/qmk/qmk_firmware/master/.github/workflows/$b")
  [ "$c" = 404 ] && echo "OURS: $b"; done
```

**Python / host** — is the module imported, and is the flagged call on a path a
user or a remote peer can drive? For the host specifically, check whether the input
crosses a trust boundary: `polyhost/server/` (authkey-gated), the legacy plaintext
relay (off by default), or a user-chosen file.

## 4. Can OUR inputs reach the vulnerable path?

This is the step that finds the real ones. Ask what the attacker controls, not
whether the pattern looks bad.

- **Workflow `${{ }}` in a `run:` body** — is the expression attacker-controllable?
  A **PR label, title, branch name, or comment body** is; `github.sha` is not. This
  is how HIL-10 was found: `bump-version.yml` interpolated
  `github.event.pull_request.labels.*.name` into a shell body, and anyone who can
  label a PR could inject. Demonstrate it before fixing — apply a harmless hostile
  label to a scratch PR and read the rendered command.
- **A dependency CVE** — read the CVE's *trigger*, then grep for it. CVE-2026-54058
  needed an image opened **by filename**, which is exactly `Image.open(filename)` in
  `im_converter.open()` on a user-chosen overlay image. Had it required a `BytesIO`
  path we do not use, the answer would have been different.

## 5. Fix, or record as inert

**Fix** when reachable. Keep the diff minimal and match the repo's conventions.
For workflow injection the pattern is: pass every `${{ }}` through `env:` and read
it as `"$VAR"` (shell) or `os.environ[...]` (Python), so **no `${{ }}` appears
inside any `run:` body**. `no_run_interpolation.py` beside this file asserts that:

```bash
S=/home/user/polykybd-ctnd/.claude/skills/verify-security-finding/no_run_interpolation.py
python3 "$S" .github/workflows/bump-version.yml      # exit 0 = clean
python3 "$S" .github/workflows/*.yml                 # sweep for candidates
```

⚠️ **It is a triage aid, not a gate — a hit is a candidate, not a finding.** Run it
against the pre-fix file to see it work, then decide each hit by
attacker-controllability (step 4). Measured on `qmk_firmware` 2026-08-29: **31 hits
across 8 workflows**, of which `bump-version.yml`'s two were the only exploitable
ones. Most of the rest interpolate `github.repository`, a `matrix.*` value from a
list the workflow itself wrote, or a `steps.*.outputs` we produced. The four stock
upstream workflows (`lint`, `format`, `feature_branch_update`,
`ci_build_major_branch_keymap`) account for a third of them and are not ours to
change. **`qmk-test.yml` and `release.yml` are ours and do still interpolate** —
their values are build outputs and `env:` constants rather than user text, so they
are not injection, but that has not been audited line by line.

⚠️ **Write a check like this so a POSITIVE CONTROL proves it fires.** The first
version of this one was an `awk` range using `\s`, which **mawk does not support** —
it reported "clean" against the known-vulnerable pre-fix file, i.e. it was
fail-open, the exact shape this project's CLAUDE.md files warn about repeatedly.
Always run it against a file you know is bad *and* one you know is good before
believing either answer:
```bash
git show <pre-fix-sha>:.github/workflows/<f>.yml > /tmp/pre.yml
python3 "$S" /tmp/pre.yml    # MUST report hits, or the checker is broken
```

**Record as inert** otherwise. ⚠️ **Never patch a vendored tree in place** —
`doom/engine/` is a verbatim upstream snapshot refetchable by
`mirror_rp2040_doom.py`, so a local edit is silently reverted by the next mirror and
makes the tree undiffable until then. Its disposition goes in
`qmk_firmware/keyboards/polykybd/doom/engine/PROVENANCE.md` instead, beside the code.

⚠️ **State the real reason it is inert, and only the real reason.** On that scan the
first draft argued the doom findings were "desktop-only because they sit inside
`#ifndef _WIN32`" — which is **wrong**, and CodeRabbit caught it: an RP2040 build is
not `_WIN32` either, so the preprocessor takes exactly that branch. The only thing
holding was **build membership**. A plausible-but-false rationale is worse than none,
because it survives review and gets trusted next time.

## 6. A tracked build artifact is its own finding

If the flagged path is inside a build directory, the vulnerability is upstream's and
irrelevant — but **the directory being in version control is a real problem you just
found for free**. `cmake-build-pinned/` held 5,224 files and 11 MB of FreeType and
HarfBuzz sources, tracked purely because `.gitignore` listed sibling names instead of
globbing. Untrack it (`git rm -r --cached <dir>`, which leaves the files on disk) and
replace the name list with a glob.

⚠️ A commit that large blows past **both** LLM reviewers — Sourcery refuses a diff
over 20,000 lines and CodeRabbit skips a PR over 100 files — so keep it in its own PR
and expect it to be unreviewed by design, rather than mixing it into reviewable work.

## 7. Write the disposition down — always

`polykybd-ctnd/docs/SECURITY_AUDIT.md` is the cross-repo tracker.

- **Real and fixed** → a row in *Status at a glance* with an ID (`FW-*` firmware,
  `HOST-*` host, `HIL-*` rig/CI — CI in `qmk_firmware` lives under `HIL-*`, see
  HIL-2) and, if the "is this actually fixed?" question is likely to recur, a note
  under *✅ Fixed — verification notes*.
- **Inert** → an entry under *✅ Checked and NOT vulnerable — don't re-litigate*,
  with the **commands** that establish it, not just the conclusion.
- Update the dated provenance line at the top of the file in the same edit.

## Output format

```
FINDING 1 — <repo> <path>:<line>
  claim:     <what the scanner says>
  ours?      <stock upstream / ours / vendored / build artifact>   [evidence: <cmd>]
  reachable? <yes / no — why>                                      [evidence: <cmd>]
  verdict:   REAL -> fix in <repo> | INERT -> record | SIDE-FINDING -> <what>
...
Recorded: <tracker section(s) edited>
PRs: <repo#n per repo touched>
```

## Pitfalls

- ⚠️ **Do not fix before step 3.** Two of five findings needed no code change and
  one of them pointed at a path that ceased to exist once a build directory was
  untracked.
- ⚠️ **Do not assume the scanner looked at our code.** It flagged an inherited
  upstream workflow and missed the genuine injection in a workflow we wrote, in the
  same repo. **A report naming an upstream path is a prompt to audit the sibling
  files we own.**
- ⚠️ **A dismissal with no artifact is not a dismissal** — it is the same work
  again next scan.
- **One PR per repo**, on the designated branch, and per repo rules **do not open a
  PR unless asked**. A `.gitignore`/untrack PR stays separate from a code PR (step 6).
- ⚠️ **Do not claim a PR was reviewed without checking.** On that session Sourcery
  refused all six PRs and three were reviewed by nobody at all; Greptile says
  *nothing* when it skips one. `pull_request_read` `get_reviews`, then compare each
  review's `commit_id` to the head sha — see the review-conventions section of
  `PolyKybdHost/CLAUDE.md`.
