#!/usr/bin/env python3
"""Tests for the generic and strict-public publication boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1]
GUARD = TOOLS / "check_publish_source.py"
PUBLIC_GUARD = TOOLS / "validate_public_distribution.py"
SCANNER = TOOLS / "check_diff_credentials.py"
SYNC = TOOLS / "sync-to-github.sh"
GIT = "/usr/bin/git"
EXPECTED_URL = "https://github.com/Veronica0206/experiment-design-agent.git"
EXPECTED_AUTHOR = "VERA Public Release"
EXPECTED_EMAIL = "Veronica0206@users.noreply.github.com"
EXPECTED_MESSAGE = "Sync reviewed public portfolio distribution"
OWNER_HOME = pwd.getpwuid(os.getuid()).pw_dir
passed = failed = 0
FAKE_SK_TOKEN = b"sk" + b"-" + b"ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
FAKE_FINE_GRAINED_TOKEN = b"github" + b"_pat_" + b"11AA22BB33CC44DD55EE66FF77"


def check(name: str, condition: bool) -> None:
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL")
        failed += 1


def publication_env(**updates: str) -> dict[str, str]:
    env = dict(os.environ, EXPDESIGN_APPROVED_GIT=GIT)
    env.update(updates)
    return env


def authorize(root: Path, paths: list[Path]) -> dict[str, str]:
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }
    manifest = root / "publish-manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": 1,
        "authorization": {
            "approved_by": "release-owner",
            "approved_at": "2026-07-27T00:00:00Z",
            "purpose": "authorized distribution",
        },
        "files": files,
    }), encoding="utf-8")
    return publication_env(
        EXPDESIGN_PUBLISH_MANIFEST_SHA256=hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest(),
    )


def run_guard(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(GUARD), *args, str(root)],
        capture_output=True,
        text=True,
        env=env,
    )


def run_public(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(PUBLIC_GUARD), *args, str(root)],
        capture_output=True,
        text=True,
        env=env or publication_env(),
    )


def run_public_workflow(path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(PUBLIC_GUARD), "--public-workflow", str(path)],
        capture_output=True,
        text=True,
        env=publication_env(),
    )


def git(root: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [GIT, "-C", str(root), *args],
        check=True,
        capture_output=True,
        env=env,
    )


reviewed_workflow = TOOLS.parent / ".github" / "workflows" / "public-assurance.yml"
check(
    "reviewed_public_workflow_pin_passes",
    run_public_workflow(reviewed_workflow).returncode == 0,
)
with tempfile.TemporaryDirectory() as directory:
    changed_workflow = Path(directory) / "public-assurance.yml"
    changed_workflow.write_bytes(
        reviewed_workflow.read_bytes().replace(b"contents: read", b"contents: write")
    )
    changed = run_public_workflow(changed_workflow)
    check(
        "changed_public_workflow_pin_fails_closed",
        changed.returncode == 1 and "structurally reviewed pin" in changed.stderr,
    )


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    skill = root / "vera-example"
    skill.mkdir()
    plaintext = skill / "SKILL.md"
    plaintext.write_text("proprietary", encoding="utf-8")
    result = run_guard(root, authorize(root, [plaintext]))
    check("plaintext_skill_rejected", result.returncode == 1 and "proprietary" in result.stderr)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime = root / "runtime.json"
    bundle = root / "bundle.skill.enc"
    runtime.write_text("{}", encoding="utf-8")
    bundle.write_text("encrypted", encoding="utf-8")
    approved_env = authorize(root, [runtime, bundle])
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    check("generic_encrypted_distribution_allowed", run_guard(root, approved_env).returncode == 0)
    public = run_public(root)
    check(
        "encrypted_bundle_unconditionally_rejected_publicly",
        public.returncode == 1 and "encrypted skill bundles" in public.stderr,
    )
    git(root, "add", "-A")
    check("authorized_staged_distribution_allowed", run_guard(root, approved_env, "--staged").returncode == 0)
    runtime.write_text("tampered", encoding="utf-8")
    check("post_authorization_tamper_rejected", run_guard(root, approved_env).returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    skill = root / "VERA-renamed"
    skill.mkdir()
    renamed = skill / "workflow.txt"
    renamed.write_text("plaintext", encoding="utf-8")
    approved_env = authorize(root, [renamed])
    generic = run_guard(root, approved_env)
    public = run_public(root)
    check("case_changed_generic_proprietary_path_rejected", generic.returncode == 1)
    check(
        "casefolded_vera_component_rejected_publicly",
        public.returncode == 1 and "vera-*" in public.stderr,
    )

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime = root / "runtime.json"
    runtime.write_text("{}", encoding="utf-8")
    approved_env = authorize(root, [runtime])
    (root / "link.json").symlink_to(runtime)
    result = run_guard(root, approved_env)
    check("symlink_in_publish_tree_rejected", result.returncode == 1 and "symlinks" in result.stderr)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime = root / "runtime.json"
    runtime.write_text("{}", encoding="utf-8")
    approved_env = authorize(root, [runtime])
    fifo = root / "blocking.pipe"
    os.mkfifo(fifo)
    result = run_guard(root, approved_env)
    check(
        "nonregular_working_entry_rejected_without_reading",
        result.returncode == 1 and "non-regular" in result.stderr,
    )

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime = root / "runtime.json"
    runtime.write_text("{}", encoding="utf-8")
    env = authorize(root, [runtime])
    manifest = root / "publish-manifest.json"
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    manifest.write_text(
        '{"schema_version":1,"schema_version":1,'
        '"authorization":{"approved_by":"a","approved_at":"b","purpose":"c"},'
        f'"files":{{"runtime.json":"{digest}"}}}}',
        encoding="utf-8",
    )
    env["EXPDESIGN_PUBLISH_MANIFEST_SHA256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    result = run_guard(root, env)
    check("duplicate_manifest_json_key_rejected", result.returncode == 1 and "duplicate JSON key" in result.stderr)

large_diff = b"+" + FAKE_SK_TOKEN + b"\n" + b"+safe line\n" * 300000
result = subprocess.run([sys.executable, "-B", str(SCANNER)], input=large_diff, capture_output=True)
check("large_diff_credential_detected_without_sigpipe", result.returncode == 1)
result = subprocess.run([sys.executable, "-B", str(SCANNER)], input=b"+safe line\n" * 300000, capture_output=True)
check("large_clean_diff_allowed", result.returncode == 0)
result = subprocess.run([sys.executable, "-B", str(SCANNER)], input=FAKE_FINE_GRAINED_TOKEN, capture_output=True)
check("fine_grained_github_token_detected", result.returncode == 1)

boundary = b"x" * (1024 * 1024 - 5) + FAKE_SK_TOKEN[:5] + FAKE_SK_TOKEN[5:]
result = subprocess.run([sys.executable, "-B", str(SCANNER)], input=boundary, capture_output=True)
check("chunk_boundary_credential_detected", result.returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    binary = root / "payload.bin"
    binary.write_bytes(b"\x00\x01" + FAKE_FINE_GRAINED_TOKEN + b"\x00")
    git(root, "add", "payload.bin")
    result = subprocess.run(
        [sys.executable, "-B", str(SCANNER), "--staged", str(root)],
        capture_output=True,
        env=publication_env(),
    )
    check("staged_binary_credential_detected", result.returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    result = subprocess.run(
        [sys.executable, "-B", str(SCANNER), "--staged", str(root)],
        capture_output=True,
        env=publication_env(),
    )
    check("scanner_runtime_failure_has_distinct_status", result.returncode == 2)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    git(root, "config", "core.autocrlf", "true")
    runtime = root / "runtime.txt"
    runtime.write_bytes(b"first\r\nsecond\r\n")
    approved_env = authorize(root, [runtime])
    working = run_guard(root, approved_env)
    git(root, "add", "-A")
    staged = run_guard(root, approved_env, "--staged")
    check(
        "staged_byte_normalization_cannot_bypass_manifest",
        working.returncode == 0 and staged.returncode == 1 and "hash mismatch" in staged.stderr,
    )

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    runtime = root / "runtime.txt"
    runtime.write_text("reviewed bytes\n", encoding="utf-8")
    approved_env = authorize(root, [runtime])
    git(root, "add", "-A")
    commit_env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                      GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    git(root, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "authorized", env=commit_env)
    commit = git(root, "rev-parse", "HEAD").stdout.decode().strip()
    committed = run_guard(root, approved_env, "--tree-ish", commit)
    runtime.write_text("uncommitted mutation\n", encoding="utf-8")
    still_committed = run_guard(root, approved_env, "--tree-ish", commit)
    injected = run_guard(root, approved_env, "--tree-ish", "HEAD")
    check("exact_committed_tree_validated_independently", committed.returncode == 0 and still_committed.returncode == 0)
    check("treeish_revision_expression_rejected", injected.returncode == 1 and "resolved Git object ID" in injected.stderr)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    forbidden = root / "nested" / "vErA-engine"
    forbidden.mkdir(parents=True)
    (forbidden / "data.txt").write_text("x", encoding="utf-8")
    git(root, "add", "-A")
    staged = run_public(root, "--staged")
    commit_env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                      GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
    git(root, "commit", "-q", "-m", "test", env=commit_env)
    commit = git(root, "rev-parse", "HEAD").stdout.decode().strip()
    committed = run_public(root, "--tree-ish", commit)
    check("strict_public_rule_runs_on_staged_tree", staged.returncode == 1)
    check("strict_public_rule_runs_on_committed_tree", committed.returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run([GIT, "init", "-q", "-b", "main", str(root)], check=True)
    (root / ".gitignore").write_text(
        "mcp-server/node_modules/\nmcp-server/dist/\n", encoding="utf-8",
    )
    (root / "README.md").write_text("public\n", encoding="utf-8")
    git(root, "add", ".gitignore", "README.md")
    clean_env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    git(root, "commit", "-q", "-m", "public", env=clean_env)
    ignored = root / "mcp-server" / "node_modules"
    ignored.mkdir(parents=True)
    (ignored / "generated-link").symlink_to("/tmp")
    first = run_public(root, "--public-clone")
    second = run_public(root, "--public-clone")
    check(
        "public_clone_check_is_repeatable_with_ignored_build_outputs",
        first.returncode == 0 and second.returncode == 0,
    )
    dirty = root / "unreviewed.txt"
    dirty.write_text("untracked\n", encoding="utf-8")
    untracked = run_public(root, "--public-clone")
    check(
        "public_clone_rejects_nonignored_untracked_file",
        untracked.returncode == 1 and "nonignored untracked" in untracked.stderr,
    )
    dirty.unlink()
    (root / "README.md").write_text("modified\n", encoding="utf-8")
    tracked = run_public(root, "--public-clone")
    check(
        "public_clone_rejects_tracked_modification",
        tracked.returncode == 1 and "tracked or nonignored" in tracked.stderr,
    )
    (root / "README.md").write_text("public\n", encoding="utf-8")
    info_exclude = root / ".git" / "info" / "exclude"
    info_exclude.write_text("secret-output/\n", encoding="utf-8")
    (root / "secret-output").mkdir()
    (root / "secret-output" / "payload.txt").write_text("hidden\n", encoding="utf-8")
    unexpected_ignored = run_public(root, "--public-clone")
    check(
        "public_clone_ignores_only_fixed_generated_outputs",
        unexpected_ignored.returncode == 1 and "unexpected ignored" in unexpected_ignored.stderr,
    )

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run([GIT, "init", "-q", "-b", "main", str(root)], check=True)
    forbidden = root / "VeRa-private" / "engine.txt"
    forbidden.parent.mkdir()
    forbidden.write_text("x", encoding="utf-8")
    git(root, "add", "-A")
    commit_env = dict(
        os.environ,
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    git(root, "commit", "-q", "-m", "forbidden", env=commit_env)
    forbidden_clone = run_public(root, "--public-clone")
    check(
        "public_clone_validates_exact_committed_tree_paths",
        forbidden_clone.returncode == 1 and "vera-*" in forbidden_clone.stderr,
    )


def metadata_payload(sha: str) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    user = {"login": "Veronica0206"}
    repository = {
        "id": 1327793911,
        "node_id": "R_kgDOTySG9w",
        "full_name": "Veronica0206/experiment-design-agent",
        "html_url": "https://github.com/Veronica0206/experiment-design-agent",
        "clone_url": EXPECTED_URL,
        "url": "https://api.github.com/repos/Veronica0206/experiment-design-agent",
        "private": False,
        "visibility": "public",
        "default_branch": "main",
        "owner": {"login": "Veronica0206"},
    }
    branch = {"name": "main", "commit": {"sha": sha}}
    return user, repository, branch


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    sha = "a" * 40
    user, repository, branch = metadata_payload(sha)
    for name, payload in (("user.json", user), ("repository.json", repository), ("branch.json", branch)):
        (root / name).write_text(json.dumps(payload), encoding="utf-8")
    valid = subprocess.run(
        [sys.executable, "-B", str(PUBLIC_GUARD), "--github-metadata", str(root)],
        capture_output=True,
        text=True,
        env=publication_env(),
    )
    check("exact_github_identity_metadata_accepted", valid.returncode == 0 and valid.stdout.strip() == sha)
    repository["node_id"] = "wrong"
    (root / "repository.json").write_text(json.dumps(repository), encoding="utf-8")
    wrong_repo = subprocess.run(
        [sys.executable, "-B", str(PUBLIC_GUARD), "--github-metadata", str(root)],
        capture_output=True,
        text=True,
        env=publication_env(),
    )
    check("wrong_repository_identity_rejected", wrong_repo.returncode == 1 and "node_id" in wrong_repo.stderr)
    repository["node_id"] = "R_kgDOTySG9w"
    (root / "repository.json").write_text(json.dumps(repository), encoding="utf-8")
    user["login"] = "someone-else"
    (root / "user.json").write_text(json.dumps(user), encoding="utf-8")
    wrong_user = subprocess.run(
        [sys.executable, "-B", str(PUBLIC_GUARD), "--github-metadata", str(root)],
        capture_output=True,
        text=True,
        env=publication_env(),
    )
    check("wrong_authenticated_user_rejected", wrong_user.returncode == 1)


def make_publication_clone(root: Path, *, correct_metadata: bool = True) -> str:
    subprocess.run([GIT, "init", "-q", "-b", "main", str(root)], check=True)
    (root / "README.md").write_text("public\n", encoding="utf-8")
    git(root, "add", "README.md")
    if correct_metadata:
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME=EXPECTED_AUTHOR,
            GIT_AUTHOR_EMAIL=EXPECTED_EMAIL,
            GIT_COMMITTER_NAME=EXPECTED_AUTHOR,
            GIT_COMMITTER_EMAIL=EXPECTED_EMAIL,
        )
        message = EXPECTED_MESSAGE
    else:
        env = dict(
            os.environ,
            GIT_AUTHOR_NAME="Private Name",
            GIT_AUTHOR_EMAIL="private@example.invalid",
            GIT_COMMITTER_NAME="Private Name",
            GIT_COMMITTER_EMAIL="private@example.invalid",
        )
        message = "private message"
    git(root, "commit", "-q", "-m", message, env=env)
    sha = git(root, "rev-parse", "HEAD").stdout.decode().strip()
    git(root, "remote", "add", "origin", EXPECTED_URL)
    git(root, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main")
    git(root, "config", "branch.main.remote", "origin")
    git(root, "config", "branch.main.merge", "refs/heads/main")
    git(root, "update-ref", "refs/remotes/origin/main", sha)
    return sha


def validate_repo(root: Path, sha: str, *, metadata: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-B",
        str(PUBLIC_GUARD),
        "--repository-state",
        str(root),
        "--expected-head",
        sha,
        "--expected-origin-head",
        sha,
    ]
    if metadata:
        command.append("--require-public-commit-metadata")
    return subprocess.run(command, capture_output=True, text=True, env=env or publication_env())


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    sha = make_publication_clone(root)
    good = validate_repo(root, sha, metadata=True)
    check("pinned_clone_and_fixed_commit_metadata_accepted", good.returncode == 0)
    hostile_config = Path(directory).parent / "hostile-publication-gitconfig"
    hostile_config.write_text("[url \"file:///private/hostile/\"]\n\tinsteadOf = https://github.com/\n", encoding="utf-8")
    isolated = validate_repo(
        root,
        sha,
        metadata=True,
        env=publication_env(GIT_CONFIG_GLOBAL=str(hostile_config)),
    )
    check("ambient_git_config_cannot_redirect_guard", isolated.returncode == 0)
    git(root, "config", "remote.origin.pushurl", "https://example.invalid/wrong.git")
    pushurl = validate_repo(root, sha)
    check("local_pushurl_rejected", pushurl.returncode == 1)
    git(root, "config", "--unset-all", "remote.origin.pushurl")
    git(root, "config", "include.path", "/tmp/hostile-config")
    include = validate_repo(root, sha)
    check("local_git_include_rejected", include.returncode == 1)
    git(root, "config", "--unset-all", "include.path")
    git(root, "remote", "set-url", "origin", "https://github.com/Veronica0206/wrong.git")
    wrong_url = validate_repo(root, sha)
    check("wrong_origin_url_rejected", wrong_url.returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    sha = make_publication_clone(root, correct_metadata=False)
    wrong_metadata = validate_repo(root, sha, metadata=True)
    check("private_or_custom_commit_metadata_rejected", wrong_metadata.returncode == 1)

sync_text = SYNC.read_text(encoding="utf-8")
check("sync_uses_absolute_shell", sync_text.startswith("#!/bin/sh\n"))
check("sync_contains_no_hardcoded_user_home", "/Users/" not in sync_text)
check("sync_has_no_reusable_clone_or_repo_override", "${EXPDESIGN_REPO:-" not in sync_text and "${EXPDESIGN_CLONE:-" not in sync_text)
check("sync_uses_fixed_public_commit_metadata", EXPECTED_AUTHOR in sync_text and EXPECTED_EMAIL in sync_text and EXPECTED_MESSAGE in sync_text)
check("sync_has_no_custom_message_option", "-m)" not in sync_text and 'MSG=' not in sync_text)
check("sync_requires_explicit_publish_review_flag", "--publish-reviewed) DRY=0" in sync_text)
check(
    "sync_uses_exact_remote_sha_lease",
    '--force-with-lease="refs/heads/$EXPECTED_BRANCH:$remote_sha"' in sync_text
    and re.search(r"--force(?:\s|$)", sync_text) is None,
)
check(
    "sync_isolates_github_cli_api_configuration",
    "GH_ISOLATED_CONFIG=$TEMP_ROOT/gh-config" in sync_text
    and 'GH_CONFIG_DIR="$GH_ISOLATED_CONFIG" GH_TOKEN="$publication_token"' in sync_text
    and '"$GH_BIN" auth token --hostname github.com' in sync_text,
)
check("sync_uses_ephemeral_clone", "/usr/bin/mktemp" in sync_text and "experiment-design-publication" in sync_text)
check(
    "sync_never_uses_ambient_security_tools",
    "command -v" not in sync_text
    and re.search(r"^\s*(?:python3|git|gh|make|rsync|dirname)\s", sync_text, re.MULTILINE)
    is None,
)

with tempfile.TemporaryDirectory() as directory:
    hostile = Path(directory)
    marker = hostile / "executed"
    for name in ("python3", "git", "gh", "make", "rsync", "dirname"):
        shim = hostile / name
        shim.write_text(f"#!/bin/sh\necho {name} >> {marker}\nexit 99\n", encoding="utf-8")
        shim.chmod(0o755)
    hostile_path = subprocess.run(
        ["/bin/sh", str(SYNC), "--dry-run"],
        capture_output=True,
        text=True,
        env={"HOME": OWNER_HOME, "PATH": str(hostile)},
    )
    check("hostile_path_shims_never_execute", hostile_path.returncode == 1 and not marker.exists())

redirected = subprocess.run(
    ["/bin/sh", str(SYNC), "--dry-run"],
    capture_output=True,
    text=True,
    env={"HOME": OWNER_HOME, "PATH": "/usr/bin:/bin", "GIT_CONFIG_GLOBAL": "/tmp/hostile"},
)
check("publication_rejects_git_environment_redirection", redirected.returncode == 1 and "Git environment redirection" in redirected.stderr)

overridden = subprocess.run(
    ["/bin/sh", str(SYNC), "--dry-run"],
    capture_output=True,
    text=True,
    env={"HOME": OWNER_HOME, "PATH": "/usr/bin:/bin", "EXPDESIGN_REPO": "other/repo"},
)
check("publication_rejects_destination_override", overridden.returncode == 1 and "overrides are forbidden" in overridden.stderr)

no_action = subprocess.run(
    ["/bin/sh", str(SYNC)],
    capture_output=True,
    text=True,
    env={"HOME": OWNER_HOME, "PATH": "/usr/bin:/bin"},
)
check("publication_without_explicit_action_fails_usage", no_action.returncode == 2 and "Usage:" in no_action.stderr)

owner_home = subprocess.run(
    [sys.executable, "-B", str(PUBLIC_GUARD), "--owner-home"],
    capture_output=True,
    text=True,
    env=publication_env(),
)
check("owner_home_is_derived_from_operating_system", owner_home.returncode == 0 and owner_home.stdout.strip() == OWNER_HOME)

arbitrary_runner = subprocess.run(
    ["/bin/sh", str(TOOLS / "run-publication-python.sh"), str(TOOLS / "validate_manifests.py")],
    capture_output=True,
    text=True,
)
check("publication_python_rejects_arbitrary_target", arbitrary_runner.returncode == 2 and "only the three fixed" in arbitrary_runner.stderr)

relative_git = publication_env(EXPDESIGN_APPROVED_GIT="git")
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run([GIT, "init", "-q", str(root)], check=True)
    result = run_public(root, "--staged", env=relative_git)
    check("relative_git_override_rejected", result.returncode == 1 and "approved absolute Git" in result.stderr)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
