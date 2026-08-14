#!/usr/bin/env python3
"""Report whether stdin or any staged Git blob contains credentials."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


PATTERN = re.compile(
    rb"sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    rb"npm_[A-Za-z0-9]{20,}|pypi-[A-Za-z0-9_-]{20,}|"
    rb"AIza[0-9A-Za-z_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY"
)
APPROVED_GIT_PATHS = {
    "/usr/bin/git",
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/opt/local/bin/git",
}


def _approved_git() -> str:
    git = os.environ.get("EXPDESIGN_APPROVED_GIT", "")
    if git not in APPROVED_GIT_PATHS:
        raise RuntimeError("an approved absolute Git executable must be supplied")
    path = Path(git)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("the approved Git executable is unavailable")
    return git


def _git_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


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
    git = _approved_git()
    records = subprocess.run(
        [git, "-C", str(root), "ls-files", "--stage", "-z"],
        check=True,
        capture_output=True,
        env=_git_environment(),
    ).stdout.split(b"\0")
    for record in records:
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
            name = raw_name.decode("utf-8", errors="surrogateescape")
        except Exception as exc:
            raise RuntimeError(f"could not parse staged inventory: {exc}") from exc
        if mode not in {b"100644", b"100755"} or stage != b"0":
            raise RuntimeError(f"unsupported staged entry {name!r}")
        if re.fullmatch(rb"[0-9a-fA-F]{40,64}", object_id) is None:
            raise RuntimeError(f"invalid staged object for {name!r}")
        proc = subprocess.Popen(
            [git, "-C", str(root), "cat-file", "blob", object_id.decode("ascii")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_git_environment(),
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
