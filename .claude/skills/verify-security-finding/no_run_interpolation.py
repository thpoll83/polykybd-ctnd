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

RUN = re.compile(r"^(\s*)-?\s*run:\s*[|>]")   # `run: |`, `run: >-`, `- run: |`
KEY = re.compile(r"^(\s*)(?:-\s+)?[A-Za-z_][\w-]*:")  # next key -- or `- name:` list item
EXPR = re.compile(r"\$\{\{")


def offenders(path):
    """Yield (lineno, text) for every ${{ }} inside a run: block scalar."""
    out = []
    indent = None                      # indent of the `run:` key, or None outside
    for n, line in enumerate(open(path, encoding="utf-8"), 1):
        if indent is None:
            m = RUN.match(line)
            if m:
                indent = len(m.group(1))
            continue
        if line.strip():               # blank lines never close a block scalar
            m = KEY.match(line)
            if m and len(m.group(1)) <= indent:
                indent = None          # dedented to a sibling key: block is over
                m2 = RUN.match(line)   # ...which may itself be another run:
                if m2:
                    indent = len(m2.group(1))
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
