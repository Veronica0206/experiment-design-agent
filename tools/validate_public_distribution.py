#!/usr/bin/env python3
"""Fail closed on public-tree paths and the pinned GitHub destination."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath


EXPECTED_OWNER = "Veronica0206"
EXPECTED_FULL_NAME = "Veronica0206/experiment-design-agent"
EXPECTED_NODE_ID = "R_kgDOTySG9w"
EXPECTED_REST_ID = 1327793911
EXPECTED_HTML_URL = "https://github.com/Veronica0206/experiment-design-agent"
EXPECTED_CLONE_URL = f"{EXPECTED_HTML_URL}.git"
EXPECTED_API_URL = "https://api.github.com/repos/Veronica0206/experiment-design-agent"
EXPECTED_BRANCH = "main"
EXPECTED_AUTHOR_NAME = "VERA Public Release"
EXPECTED_AUTHOR_EMAIL = "Veronica0206@users.noreply.github.com"
EXPECTED_COMMIT_MESSAGE = "Sync reviewed public portfolio distribution"
EXPECTED_PUBLIC_ASSURANCE_SHA256 = (
    "898c0bd0347b9967f1f4ac95eef15632e86af778cb6cb0aea6efdc3e0788dbb5"
)
APPROVED_GIT_PATHS = {
    "/usr/bin/git",
    "/opt/homebrew/bin/git",
    "/usr/local/bin/git",
    "/opt/local/bin/git",
}
ALLOWED_LOCAL_CONFIG = {
    "core.repositoryformatversion",
    "core.filemode",
    "core.bare",
    "core.logallrefupdates",
    "core.ignorecase",
    "core.precomposeunicode",
    "remote.origin.url",
    "remote.origin.fetch",
    "branch.main.remote",
    "branch.main.merge",
}
ALLOWED_PUBLIC_BUILD_OUTPUTS = {
    PurePosixPath("mcp-server/node_modules"),
    PurePosixPath("mcp-server/dist"),
}


class DuplicateJsonKey(ValueError):
    pass


def fail(message: str) -> int:
    print(f"ABORT: {message}", file=sys.stderr)
    return 1


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


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


def _git_result(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [_approved_git(), "-C", str(root), *args],
        check=False,
        capture_output=True,
        env=_git_environment(),
    )


def _git(root: Path, *args: str) -> bytes:
    result = _git_result(root, *args)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Git inspection failed: {detail or args[0]}")
    return result.stdout


def _working_paths(root: Path) -> tuple[list[str], str | None]:
    paths: list[str] = []
    def raise_walk_error(error: OSError) -> None:
        raise error

    for current, directories, files in os.walk(
        root, topdown=True, onerror=raise_walk_error, followlinks=False,
    ):
        current_path = Path(current)
        if current_path == root and ".git" in directories:
            metadata = current_path / ".git"
            mode = metadata.lstat().st_mode
            if not stat.S_ISDIR(mode):
                return [], "root .git metadata must be a real directory"
            directories.remove(".git")
        for name in directories:
            path = current_path / name
            mode = path.lstat().st_mode
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(mode):
                return [], f"symlinks are forbidden in a public tree: {relative}"
            if not stat.S_ISDIR(mode):
                return [], f"non-directory traversal entry is forbidden: {relative}"
        for name in files:
            if current_path == root and name == ".git":
                return [], "root .git metadata must be a real directory"
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                return [], f"symlinks are forbidden in a public tree: {relative}"
            if not stat.S_ISREG(mode):
                return [], f"non-regular public-tree entry is forbidden: {relative}"
            paths.append(relative)
    return paths, None


def _staged_paths(root: Path) -> tuple[list[str], str | None]:
    paths: list[str] = []
    for record in _git(root, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
            name = raw_name.decode("utf-8", errors="surrogateescape")
        except Exception as exc:
            return [], f"could not parse staged inventory: {exc}"
        if mode not in {b"100644", b"100755"} or stage != b"0":
            return [], f"unsupported staged entry: {name}"
        if re.fullmatch(rb"[0-9a-fA-F]{40,64}", object_id) is None:
            return [], f"invalid staged object: {name}"
        paths.append(name)
    return paths, None


def _tree_paths(root: Path, object_id: str) -> tuple[list[str], str | None]:
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", object_id) is None:
        return [], "tree-ish must be a resolved Git object ID"
    paths: list[str] = []
    for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", object_id).split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_name = record.split(b"\t", 1)
            mode, object_type, blob_id = metadata.split(b" ", 2)
            name = raw_name.decode("utf-8", errors="surrogateescape")
        except Exception as exc:
            return [], f"could not parse committed inventory: {exc}"
        if mode not in {b"100644", b"100755"} or object_type != b"blob":
            return [], f"unsupported committed entry: {name}"
        if re.fullmatch(rb"[0-9a-fA-F]{40,64}", blob_id) is None:
            return [], f"invalid committed object: {name}"
        paths.append(name)
    return paths, None


def _validate_public_paths(paths: list[str]) -> int:
    for relative in paths:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or str(path) != relative:
            return fail(f"unsafe public-tree path: {relative!r}")
        if any(part.casefold().startswith("vera-") for part in path.parts):
            return fail(f"vera-* path components are forbidden publicly: {relative}")
        if path.name.casefold().endswith(".skill.enc"):
            return fail(f"encrypted skill bundles are forbidden publicly: {relative}")
    print(f"Strict public path policy passed ({len(paths)} entries)")
    return 0


def _load_json(path: Path) -> object:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"metadata must be a regular file: {path.name}")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)


def _validate_github_metadata(root: Path) -> tuple[int, str | None]:
    if root.is_symlink() or not root.is_dir():
        return fail("GitHub metadata path must be a real directory"), None
    try:
        user = _load_json(root / "user.json")
        repository = _load_json(root / "repository.json")
        branch = _load_json(root / "branch.json")
    except Exception as exc:
        return fail(f"invalid GitHub metadata: {exc}"), None
    if not isinstance(user, dict) or user.get("login") != EXPECTED_OWNER:
        return fail(f"authenticated GitHub user must be {EXPECTED_OWNER}"), None
    expected_repository = {
        "id": EXPECTED_REST_ID,
        "node_id": EXPECTED_NODE_ID,
        "full_name": EXPECTED_FULL_NAME,
        "html_url": EXPECTED_HTML_URL,
        "clone_url": EXPECTED_CLONE_URL,
        "url": EXPECTED_API_URL,
        "private": False,
        "visibility": "public",
        "default_branch": EXPECTED_BRANCH,
    }
    if not isinstance(repository, dict):
        return fail("GitHub repository metadata must be an object"), None
    for key, expected in expected_repository.items():
        if repository.get(key) != expected:
            return fail(f"GitHub destination {key} does not match the public pin"), None
    owner = repository.get("owner")
    if not isinstance(owner, dict) or owner.get("login") != EXPECTED_OWNER:
        return fail("GitHub destination owner does not match the public pin"), None
    if not isinstance(branch, dict) or branch.get("name") != EXPECTED_BRANCH:
        return fail(f"GitHub destination branch must be {EXPECTED_BRANCH}"), None
    commit = branch.get("commit")
    sha = commit.get("sha") if isinstance(commit, dict) else None
    if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{40}", sha) is None:
        return fail("GitHub main branch did not provide an exact commit SHA"), None
    return 0, sha


def _validated_owner_home() -> tuple[int, str | None]:
    try:
        uid = os.getuid()
        if uid == 0:
            return fail("public publication may not run as root"), None
        account = pwd.getpwuid(uid)
        home = Path(account.pw_dir)
        if not home.is_absolute() or home.is_symlink() or not home.is_dir():
            return fail("the operating-system account home is not a real absolute directory"), None
        metadata = home.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != uid:
            return fail("the operating-system account home has the wrong type or owner"), None
        if home.resolve() != home:
            return fail("the operating-system account home must be canonical"), None
    except Exception as exc:
        return fail(f"could not resolve the operating-system account home: {exc}"), None
    return 0, str(home)


def _local_config(root: Path) -> dict[str, list[str]]:
    result = _git_result(root, "config", "--local", "--null", "--list")
    if result.returncode != 0:
        raise RuntimeError("could not read the clone's local Git config")
    config: dict[str, list[str]] = {}
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        try:
            raw_key, raw_value = item.split(b"\n", 1)
        except ValueError:
            raise RuntimeError("could not parse the clone's local Git config")
        key = raw_key.decode("utf-8", errors="strict").casefold()
        value = raw_value.decode("utf-8", errors="strict")
        config.setdefault(key, []).append(value)
    return config


def _validate_repository_state(
    root: Path,
    expected_head: str,
    expected_origin_head: str,
    public_metadata: bool,
) -> int:
    if root.is_symlink() or not root.is_dir():
        return fail("publication clone must be a real directory")
    if re.fullmatch(r"[0-9a-f]{40}", expected_head) is None:
        return fail("expected clone HEAD must be an exact SHA-1")
    if re.fullmatch(r"[0-9a-f]{40}", expected_origin_head) is None:
        return fail("expected origin/main must be an exact SHA-1")
    try:
        if _git(root, "rev-parse", "--is-inside-work-tree").strip() != b"true":
            return fail("publication clone is not a Git work tree")
        actual_head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
        if actual_head != expected_head:
            return fail("publication clone HEAD differs from the pinned remote SHA")
        branch = _git(root, "symbolic-ref", "--short", "HEAD").decode("utf-8").strip()
        if branch != EXPECTED_BRANCH:
            return fail(f"publication clone branch must be {EXPECTED_BRANCH}")
        remotes = _git(root, "remote").decode("utf-8").splitlines()
        if remotes != ["origin"]:
            return fail("publication clone must contain only the origin remote")
        if _git(root, "remote", "get-url", "origin").decode().strip() != EXPECTED_CLONE_URL:
            return fail("origin URL differs from the pinned public repository")
        if _git(root, "remote", "get-url", "--push", "origin").decode().strip() != EXPECTED_CLONE_URL:
            return fail("origin push URL differs from the pinned public repository")
        config = _local_config(root)
        unexpected = sorted(set(config) - ALLOWED_LOCAL_CONFIG)
        if unexpected:
            return fail(f"unexpected local Git configuration: {unexpected[:5]}")
        required_values = {
            "remote.origin.url": [EXPECTED_CLONE_URL],
            "remote.origin.fetch": ["+refs/heads/main:refs/remotes/origin/main"],
            "branch.main.remote": ["origin"],
            "branch.main.merge": ["refs/heads/main"],
        }
        for key, expected in required_values.items():
            if config.get(key) != expected:
                return fail(f"local Git configuration differs at {key}")
        origin_head = _git(root, "rev-parse", "refs/remotes/origin/main").decode().strip()
        if origin_head != expected_origin_head:
            return fail("origin/main differs from the pinned remote SHA")
        if _git(root, "status", "--porcelain", "--untracked-files=all"):
            return fail("publication clone must be clean at destination verification")
        if public_metadata:
            raw = _git(
                root,
                "show",
                "-s",
                "--format=format:%an%x00%ae%x00%cn%x00%ce%x00%B%x00",
                "HEAD",
            )
            fields = raw.split(b"\0")
            if len(fields) < 6:
                return fail("could not parse public commit metadata")
            decoded = [field.decode("utf-8", errors="strict") for field in fields[:5]]
            expected = [
                EXPECTED_AUTHOR_NAME,
                EXPECTED_AUTHOR_EMAIL,
                EXPECTED_AUTHOR_NAME,
                EXPECTED_AUTHOR_EMAIL,
                EXPECTED_COMMIT_MESSAGE + "\n",
            ]
            if decoded != expected:
                return fail("public commit author, committer, or message is not fixed")
    except Exception as exc:
        return fail(f"could not validate publication clone: {exc}")
    print(f"Pinned publication clone passed at {expected_head}")
    return 0


def _validate_public_clone(root: Path) -> int:
    """Validate the clean committed public tree, not ignored build outputs."""
    if root.is_symlink() or not root.is_dir():
        return fail("public clone must be a real directory")
    try:
        top_level = Path(
            _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
        ).resolve()
        if top_level != root.resolve():
            return fail("public-clone mode must run at the Git work-tree root")
        dirty = _git(
            root, "status", "--porcelain=v1", "--untracked-files=all",
        )
        if dirty:
            return fail("public clone has tracked or nonignored untracked changes")
        ignored = _git(
            root,
            "status",
            "--porcelain=v1",
            "-z",
            "--ignored=matching",
            "--untracked-files=all",
        )
        for record in ignored.split(b"\0"):
            if not record:
                continue
            if not record.startswith(b"!! "):
                return fail("public clone status contained an unexpected entry")
            relative = record[3:].decode("utf-8", errors="surrogateescape").rstrip("/")
            path = PurePosixPath(relative)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not any(path == allowed or allowed in path.parents
                           for allowed in ALLOWED_PUBLIC_BUILD_OUTPUTS)
            ):
                return fail(f"unexpected ignored public-clone entry: {relative}")
        for allowed in ALLOWED_PUBLIC_BUILD_OUTPUTS:
            generated = root.joinpath(*allowed.parts)
            if generated.exists() or generated.is_symlink():
                mode = generated.lstat().st_mode
                if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                    return fail(f"ignored build-output root must be a real directory: {allowed}")
        head = _git(
            root, "rev-parse", "--verify", "HEAD^{commit}",
        ).decode("ascii").strip()
        if re.fullmatch(r"[0-9a-f]{40,64}", head) is None:
            return fail("public clone HEAD did not resolve to an exact object ID")
        paths, error = _tree_paths(root, head)
    except Exception as exc:
        return fail(f"could not inspect public clone: {exc}")
    if error:
        return fail(error)
    status = _validate_public_paths(paths)
    if status == 0:
        print(f"Clean committed public clone passed at {head}")
    return status


def _validate_public_workflow(path: Path) -> int:
    """Pin the exact least-privilege public workflow using stdlib only.

    The private release validator still parses and checks the workflow's YAML
    structure. This public check makes that reviewed structure self-attesting
    in GitHub Actions without installing a YAML parser: any byte change must be
    reviewed by the structural validator and accompanied by an explicit pin
    update here.
    """
    try:
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            return fail("public assurance workflow must be a real absolute file")
        metadata = path.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
            return fail("public assurance workflow has an invalid file shape")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception as exc:
        return fail(f"could not inspect public assurance workflow: {exc}")
    if digest != EXPECTED_PUBLIC_ASSURANCE_SHA256:
        return fail(
            "public assurance workflow differs from the structurally reviewed pin"
        )
    print("Pinned public assurance workflow passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--staged", action="store_true")
    mode.add_argument("--tree-ish", metavar="OBJECT_ID")
    mode.add_argument("--github-metadata", type=Path)
    mode.add_argument("--repository-state", type=Path)
    mode.add_argument("--public-clone", type=Path)
    mode.add_argument("--public-workflow", type=Path)
    mode.add_argument("--owner-home", action="store_true")
    parser.add_argument("--expected-head")
    parser.add_argument("--expected-origin-head")
    parser.add_argument("--require-public-commit-metadata", action="store_true")
    parser.add_argument("source", type=Path, nargs="?")
    args = parser.parse_args()

    if args.owner_home:
        if (
            args.source is not None
            or args.expected_head is not None
            or args.expected_origin_head is not None
            or args.require_public_commit_metadata
        ):
            return fail("owner-home mode accepts no repository or tree options")
        status, home = _validated_owner_home()
        if status == 0 and home is not None:
            print(home)
        return status
    if args.public_clone is not None:
        if (
            args.source is not None
            or args.expected_head is not None
            or args.expected_origin_head is not None
            or args.require_public_commit_metadata
        ):
            return fail("public-clone mode accepts only its clone root")
        return _validate_public_clone(args.public_clone)
    if args.public_workflow is not None:
        if (
            args.source is not None
            or args.expected_head is not None
            or args.expected_origin_head is not None
            or args.require_public_commit_metadata
        ):
            return fail("public-workflow mode accepts only its workflow path")
        return _validate_public_workflow(args.public_workflow)
    if args.github_metadata is not None:
        if args.source is not None or args.expected_head is not None:
            return fail("GitHub metadata mode accepts only its metadata directory")
        status, sha = _validate_github_metadata(args.github_metadata)
        if status == 0 and sha is not None:
            print(sha)
        return status
    if args.repository_state is not None:
        if (
            args.source is not None
            or args.expected_head is None
            or args.expected_origin_head is None
        ):
            return fail(
                "repository-state mode requires --expected-head and "
                "--expected-origin-head"
            )
        return _validate_repository_state(
            args.repository_state,
            args.expected_head,
            args.expected_origin_head,
            args.require_public_commit_metadata,
        )
    if (
        args.expected_head is not None
        or args.expected_origin_head is not None
        or args.require_public_commit_metadata
    ):
        return fail("repository-state options require --repository-state")
    if args.source is None:
        return fail("a public distribution source is required")
    if args.source.is_symlink():
        return fail("public distribution source root may not be a symlink")
    root = args.source.resolve()
    if not root.is_dir():
        return fail(f"public distribution source is not a directory: {root}")
    try:
        if args.tree_ish:
            paths, error = _tree_paths(root, args.tree_ish)
        elif args.staged:
            paths, error = _staged_paths(root)
        else:
            paths, error = _working_paths(root)
    except Exception as exc:
        return fail(f"could not inspect public distribution: {exc}")
    if error:
        return fail(error)
    return _validate_public_paths(paths)


if __name__ == "__main__":
    raise SystemExit(main())
