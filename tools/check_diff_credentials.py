#!/usr/bin/env python3
"""Report whether stdin or any staged Git blob contains credentials."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


PATTERN = re.compile(
    rb"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    rb"npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|"
    rb"AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY"
)


def stream_contains_secret(stream) -> bool:
    found = False
    tail = b""
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        window = tail + chunk
        if PATTERN.search(window):
            found = True
        tail = window[-256:]
        # Continue consuming stdin after a match so the producer never receives
        # SIGPIPE under `set -o pipefail`.
    return found


def staged_contains_secret(root: Path) -> bool:
    names = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--name-only", "-z",
         "--diff-filter=ACMR"],
        check=True, capture_output=True,
    ).stdout.split(b"\0")
    for raw_name in names:
        if not raw_name:
            continue
        name = raw_name.decode("utf-8", errors="surrogateescape")
        proc = subprocess.Popen(
            ["git", "-C", str(root), "show", f":{name}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        found = stream_contains_secret(proc.stdout)
        _stderr = proc.stderr.read() if proc.stderr is not None else b""
        return_code = proc.wait()
        if return_code != 0:
            raise RuntimeError(f"could not read staged blob {name!r}")
        if found:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", type=Path)
    args = parser.parse_args()
    try:
        found = (staged_contains_secret(args.staged.resolve()) if args.staged
                 else stream_contains_secret(sys.stdin.buffer))
    except Exception as exc:
        print(f"credential scan failed closed: {exc}", file=sys.stderr)
        return 2
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
