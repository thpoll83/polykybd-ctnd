#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0-only
#
# polykybd-runner-ctl — start/stop/restart the GitHub Actions runner service.
#
# SECURITY (HIL-7): this exists so the station user's passwordless sudo grant can
# be an EXACT command list with no wildcard in it. The grant it replaces was:
#
#     $USER ALL=(root) NOPASSWD: /usr/bin/systemctl start actions.runner.*, …
#
# Sudoers matches command-line arguments as a single concatenated string and `*`
# matches SPACES, so that rule did not mean "one unit whose name starts with
# actions.runner." — it permitted any argument tail beginning with that prefix,
# including further unit names and systemctl options. The control UI runs as the
# station user with no authentication (HIL-4), so whatever that admitted was
# reachable by anyone who could open the page.
#
# The fix is not a tighter pattern (sudoers cannot express "one word"), it is to
# take the unit name out of the caller's hands entirely: this script accepts ONE
# argument, an action from a fixed set, and DISCOVERS the unit itself. There is
# no attacker-supplied string anywhere in the systemctl invocation, so there is
# nothing to inject.
#
# ⚠️ INSTALLED COPY ONLY. setup.sh copies this to /usr/local/sbin/, root-owned
# and not writable by the station user, and grants sudo on THAT path. Never point
# the sudoers rule at the repo checkout: the station user can write there (and
# self-update rewrites it unattended), so a repo-resident script under sudo would
# hand back exactly the root escalation this removes. A change here therefore
# only takes effect once setup.sh has been re-run on the rig.

set -euo pipefail

readonly USAGE="usage: polykybd-runner-ctl {start|stop|restart}"

# Exactly one argument, from a fixed set. Anything else is refused — no
# pass-through of extra arguments to systemctl, by construction.
if [[ $# -ne 1 ]]; then
    echo "$USAGE" >&2
    exit 2
fi

case "$1" in
    start|stop|restart) action="$1" ;;
    *) echo "$USAGE" >&2; exit 2 ;;
esac

# Discover the unit rather than accepting one. Same query register-runner.sh's
# find_runner_unit() uses, so both agree on which unit is "the" runner. A rig
# only ever has one registered.
unit="$(systemctl list-units --all --no-legend --plain --type=service \
            'actions.runner.*' 2>/dev/null | awk 'NR==1{print $1}' || true)"

if [[ -z "$unit" ]]; then
    echo "polykybd-runner-ctl: no installed actions.runner unit found." >&2
    echo "Register the runner first (scripts/register-runner.sh)." >&2
    exit 1
fi

# Belt and braces: the discovered name is not attacker-controlled (it comes from
# systemd's own unit list), but assert the shape anyway so a surprising unit name
# fails loudly here instead of being passed to systemctl.
if [[ ! "$unit" =~ ^actions\.runner\.[A-Za-z0-9_.@-]+\.service$ ]]; then
    echo "polykybd-runner-ctl: refusing unexpected unit name: $unit" >&2
    exit 1
fi

exec systemctl "$action" "$unit"
