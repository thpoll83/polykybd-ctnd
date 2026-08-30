#!/usr/bin/env python3
"""Fail if any GitHub Actions `run:` body interpolates a ${{ }} expression.

The remedy for workflow template injection is to pass every expression through
`env:` and read it as "$VAR" / os.environ[...], so that no attacker-controllable
text is ever pasted into a shell or Python body. This asserts that property.

    python3 no_run_interpolation.py .github/workflows/*.yml

Exit 0 = clean, 1 = at least one interpolation found, 2 = usage error.

Deliberately a line/indent scanner rather than a YAML parse: `run:` bodies are
block scalars, and a YAML loader hands them back as one string with the source
line numbers already gone -- which is the thing a reviewer needs.
"""
import re
import sys

# `run:` in every form -- block scalar (`run: |`, `run: >-`) AND inline
# (`run: echo ...`), with or without a leading `- `. Group 1 is the indent,
# group 2 the rest of the line, which for the inline form IS the command.
RUN = re.compile(r"^(\s*)(?:-\s+)?run:(.*)$")
KEY = re.compile(r"^(\s*)(?:-\s+)?[A-Za-z_][\w-]*:")  # next key -- or `- name:` list item
EXPR = re.compile(r"\$\{\{")


def offenders(path):
    """Yield (lineno, text) for every ${{ }} in a run: command, block or inline.

    An inline `run: echo ...` carries the command on the `run:` line itself, so
    that line is checked too -- a checker that only entered block scalars would
    report a genuinely injectable inline command as clean, which is the
    fail-open direction and the whole thing this tool exists to avoid. Lines
    indented under either form are scanned the same way, which also covers a
    plain scalar continued across lines.
    """
    out = []
    indent = None                      # indent of the `run:` key, or None outside

    def open_run(n, line):
        """Enter a run: at this line if it starts one; check it, return its indent."""
        m = RUN.match(line)
        if not m:
            return None
        if EXPR.search(m.group(2)):    # inline command on the run: line itself
            out.append((n, line.rstrip()))
        return len(m.group(1))

    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        if indent is None:
            indent = open_run(n, line)
            continue
        if line.strip():               # blank lines never close a block scalar
            m = KEY.match(line)
            if m and len(m.group(1)) <= indent:
                # dedented to a sibling key -- which may itself be another run:
                indent = open_run(n, line)
                continue
            if EXPR.search(line):
                out.append((n, line.rstrip()))
    return out


def main(argv):
    if not argv:
        print(__doc__.strip().splitlines()[4].strip(), file=sys.stderr)
        return 2
    bad = 0
    for path in argv:
        for n, text in offenders(path):
            print(f"{path}:{n}: interpolation inside a run: body -- {text.strip()}")
            bad += 1
    print("clean" if not bad else f"{bad} interpolation(s) found", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
