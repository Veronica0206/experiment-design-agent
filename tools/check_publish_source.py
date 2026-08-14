#!/usr/bin/env python3
"""Allow publication only from a separately pinned, hash-locked tree."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath

MANIFEST = "publish-manifest.json"
LICENSE_NAMES = {"license", "license.md", "license.txt"}
APPROVED_GIT_PATHS = {
    "/usr/bin/git",
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/opt/local/bin/git",
}


class DuplicateJsonKey(ValueError):
    """Raised when an authorization manifest contains an ambiguous object."""


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def fail(message: str) -> int:
    print(f"ABORT: {message}", file=sys.stderr)
    return 1


def _forbidden_proprietary_path(relative: str) -> str | None:
    path = PurePosixPath(relative)
    lowered = path.name.lower()
    if lowered == "skill.md" or lowered.endswith(".skill"):
        return f"proprietary plaintext/bundle filename forbidden: {relative}"
    if any(part.lower().startswith("vera-") for part in path.parts):
        allowed = lowered.endswith(".skill.enc") or lowered in LICENSE_NAMES
        if not allowed:
            return (
                "only encrypted skill bundles and license files may appear under "
                f"vera-* paths: {relative}"
            )
    return None


def _working_entries(root: Path) -> tuple[dict[str, bytes], str | None]:
    entries: dict[str, bytes] = {}
    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(
        root, topdown=True, onerror=raise_walk_error, followlinks=False,
    ):
        current_path = Path(current)
        # Repository internals are neither release content nor manifest input.
        if current_path == root and ".git" in directories:
            git_metadata = current_path / ".git"
            try:
                metadata_mode = git_metadata.lstat().st_mode
            except OSError as exc:
                return {}, f"could not inspect repository metadata: {exc}"
            if not stat.S_ISDIR(metadata_mode):
                return {}, "root .git metadata must be a real directory"
            directories.remove(".git")
        for name in directories:
            path = current_path / name
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                return {}, f"could not inspect publish-tree entry {path}: {exc}"
            if stat.S_ISLNK(mode):
                return {}, f"symlinks are forbidden in a publish tree: {path.relative_to(root)}"
            if not stat.S_ISDIR(mode):
                return {}, f"non-directory traversal entry is forbidden: {path.relative_to(root)}"
        for name in files:
            if name == ".git" and current_path == root:
                return {}, "root .git metadata must be a real directory"
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            try:
                mode = path.lstat().st_mode
            except OSError as exc:
                return {}, f"could not inspect publish-tree entry {relative}: {exc}"
            if stat.S_ISLNK(mode):
                return {}, f"symlinks are forbidden in a publish tree: {relative}"
            if not stat.S_ISREG(mode):
                return {}, f"non-regular publish-tree entry is forbidden: {relative}"
            entries[relative] = path.read_bytes()
    return entries, None


def _approved_git() -> str:
    git = os.environ.get("EXPDESIGN_APPROVED_GIT", "")
    if git not in APPROVED_GIT_PATHS:
        raise RuntimeError("an approved absolute Git executable must be supplied")
    path = Path(git)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError("the approved Git executable is unavailable")
    return git


def _git(root: Path, *args: str) -> bytes:
    git = _approved_git()
    return subprocess.run(
        [git, "-C", str(root), *args],
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": "/var/empty",
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
        },
    ).stdout


def _staged_entries(root: Path) -> tuple[dict[str, bytes], str | None]:
    raw = _git(root, "ls-files", "--stage", "-z")
    entries: dict[str, bytes] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
            name = raw_name.decode("utf-8", errors="surrogateescape")
        except Exception as exc:
            return {}, f"could not parse staged inventory: {exc}"
        if mode == b"120000":
            return {}, f"symlinks are forbidden in a publish tree: {name}"
        if mode not in {b"100644", b"100755"}:
            return {}, f"unsupported staged entry mode {mode.decode()}: {name}"
        if stage != b"0" or re.fullmatch(rb"[0-9a-fA-F]{40,64}", object_id) is None:
            return {}, f"unmerged or invalid staged entry is forbidden: {name}"
        entries[name] = _git(root, "cat-file", "blob", object_id.decode("ascii"))
    return entries, None


def _tree_entries(root: Path, treeish: str) -> tuple[dict[str, bytes], str | None]:
    # The sync path passes a resolved commit object ID. Restricting this input
    # to an object ID avoids option/revision-expression injection and makes the
    # exact bytes being authorized unambiguous.
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", treeish) is None:
        return {}, "tree-ish must be a resolved Git object ID"
    raw = _git(root, "ls-tree", "-r", "-z", "--full-tree", treeish)
    entries: dict[str, bytes] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
            name = raw_name.decode("utf-8", errors="surrogateescape")
        except Exception as exc:
            return {}, f"could not parse committed inventory: {exc}"
        if mode == b"120000":
            return {}, f"symlinks are forbidden in a publish tree: {name}"
        if mode not in {b"100644", b"100755"} or object_type != b"blob":
            return {}, f"unsupported committed entry mode {mode.decode()}: {name}"
        entries[name] = _git(root, "cat-file", "blob", object_id.decode("ascii"))
    return entries, None


def _validate(
    root: Path,
    entries: dict[str, bytes],
    pin: str,
) -> int:
    manifest_bytes = entries.get(MANIFEST)
    if manifest_bytes is None:
        return fail(f"authorized distribution requires a regular {MANIFEST}")
    actual_manifest = digest_bytes(manifest_bytes)
    if len(pin) != 64 or pin != actual_manifest:
        return fail(
            "publish manifest does not match the separately supplied operator pin; set "
            "EXPDESIGN_PUBLISH_MANIFEST_SHA256 to its reviewed SHA-256"
        )
    try:
        manifest = json.loads(
            manifest_bytes.decode("utf-8"), object_pairs_hook=_strict_object,
        )
    except Exception as exc:
        return fail(f"invalid {MANIFEST}: {exc}")
    auth = manifest.get("authorization") if isinstance(manifest, dict) else None
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(auth, dict):
        return fail("manifest schema_version=1 and authorization object are required")
    if not all(isinstance(auth.get(key), str) and auth[key].strip()
               for key in ("approved_by", "approved_at", "purpose")):
        return fail("authorization requires approved_by, approved_at, and purpose")
    if not isinstance(files, dict) or not files:
        return fail("manifest files must be a non-empty path-to-sha256 mapping")

    actual = set(entries) - {MANIFEST}
    for relative in actual:
        violation = _forbidden_proprietary_path(relative)
        if violation:
            return fail(violation)
    declared = set(files)
    if actual != declared:
        extra = sorted(actual - declared)
        missing = sorted(declared - actual)
        return fail(f"manifest inventory mismatch; unlisted={extra[:10]}, missing={missing[:10]}")
    for relative, expected in files.items():
        path = PurePosixPath(relative) if isinstance(relative, str) else None
        if path is None or path.is_absolute() or ".." in path.parts or str(path) != relative:
            return fail(f"unsafe manifest path: {relative!r}")
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
            return fail(f"invalid sha256 for {relative}")
        if digest_bytes(entries[relative]) != expected.lower():
            return fail(f"hash mismatch for {relative}")
    print(f"Pinned publish manifest passed: {root} ({len(files)} files)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    source_mode = parser.add_mutually_exclusive_group()
    source_mode.add_argument("--staged", action="store_true")
    source_mode.add_argument("--tree-ish", metavar="OBJECT_ID")
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    if args.source.is_symlink():
        return fail("publish source root may not be a symlink")
    root = args.source.resolve()
    if not root.is_dir():
        return fail(f"publish source is not a directory: {root}")
    inspected = "committed" if args.tree_ish else ("staged" if args.staged else "working")
    try:
        if args.tree_ish:
            entries, error = _tree_entries(root, args.tree_ish)
        elif args.staged:
            entries, error = _staged_entries(root)
        else:
            entries, error = _working_entries(root)
    except Exception as exc:
        return fail(f"could not inspect {inspected} publish tree: {exc}")
    if error:
        return fail(error)
    pin = os.environ.get("EXPDESIGN_PUBLISH_MANIFEST_SHA256", "").lower()
    return _validate(root, entries, pin)


if __name__ == "__main__":
    raise SystemExit(main())
