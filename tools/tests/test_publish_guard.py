#!/usr/bin/env python3
"""Tests for the proprietary publication guard."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import hashlib
import json
import os
from pathlib import Path


GUARD = Path(__file__).resolve().parents[1] / "check_publish_source.py"
SCANNER = Path(__file__).resolve().parents[1] / "check_diff_credentials.py"
passed = failed = 0
FAKE_SK_TOKEN = b"sk" + b"-" + b"ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"


def check(name, condition):
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL")
        failed += 1


def authorize(root: Path, paths: list[Path]):
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
            "purpose": "private authorized distribution",
        },
        "files": files,
    }), encoding="utf-8")
    return dict(
        os.environ,
        EXPDESIGN_PUBLISH_MANIFEST_SHA256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    skill = root / "vera-example"
    skill.mkdir()
    plaintext = skill / "SKILL.md"
    plaintext.write_text("proprietary", encoding="utf-8")
    approved_env = authorize(root, [plaintext])
    result = subprocess.run([sys.executable, str(GUARD), str(root)],
                            capture_output=True, text=True, env=approved_env)
    check("plaintext_skill_rejected",
          result.returncode == 1 and "proprietary" in result.stderr)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime = root / "runtime.json"
    bundle = root / "bundle.skill.enc"
    runtime.write_text("{}", encoding="utf-8")
    bundle.write_text("encrypted", encoding="utf-8")
    approved_env = authorize(root, [runtime, bundle])
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    result = subprocess.run([sys.executable, str(GUARD), str(root)],
                            capture_output=True, text=True, env=approved_env)
    check("authorized_distribution_tree_with_git_metadata_allowed", result.returncode == 0)

    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    staged = subprocess.run(
        [sys.executable, str(GUARD), "--staged", str(root)],
        capture_output=True, text=True, env=approved_env,
    )
    check("authorized_staged_distribution_allowed", staged.returncode == 0)

    runtime.write_text("tampered", encoding="utf-8")
    result = subprocess.run([sys.executable, str(GUARD), str(root)],
                            capture_output=True, text=True, env=approved_env)
    check("post_authorization_tamper_rejected", result.returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    skill = root / "vera-renamed"
    skill.mkdir()
    renamed = skill / "workflow.txt"
    renamed.write_text("plaintext", encoding="utf-8")
    approved_env = authorize(root, [renamed])
    result = subprocess.run([sys.executable, str(GUARD), str(root)],
                            capture_output=True, text=True, env=approved_env)
    check("renamed_proprietary_content_rejected",
          result.returncode == 1 and "vera-*" in result.stderr)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    skill = root / "VERA-renamed"
    skill.mkdir()
    renamed = skill / "workflow.txt"
    renamed.write_text("plaintext", encoding="utf-8")
    approved_env = authorize(root, [renamed])
    result = subprocess.run([sys.executable, str(GUARD), str(root)],
                            capture_output=True, text=True, env=approved_env)
    check("case_changed_proprietary_path_rejected",
          result.returncode == 1 and "vera-*" in result.stderr)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    runtime = root / "runtime.json"
    runtime.write_text("{}", encoding="utf-8")
    approved_env = authorize(root, [runtime])
    (root / "link.json").symlink_to(runtime)
    result = subprocess.run([sys.executable, str(GUARD), str(root)],
                            capture_output=True, text=True, env=approved_env)
    check("symlink_in_publish_tree_rejected",
          result.returncode == 1 and "symlinks are forbidden" in result.stderr)

large_diff = (b"+" + FAKE_SK_TOKEN + b"\n" + b"+safe line\n" * 300000)
result = subprocess.run([sys.executable, str(SCANNER)], input=large_diff,
                        capture_output=True)
check("large_diff_credential_detected_without_sigpipe", result.returncode == 1)
result = subprocess.run([sys.executable, str(SCANNER)], input=b"+safe line\n" * 300000,
                        capture_output=True)
check("large_clean_diff_allowed", result.returncode == 0)

boundary = b"x" * (1024 * 1024 - 5) + FAKE_SK_TOKEN[:5] + FAKE_SK_TOKEN[5:]
result = subprocess.run([sys.executable, str(SCANNER)], input=boundary,
                        capture_output=True)
check("chunk_boundary_credential_detected", result.returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    binary = root / "payload.bin"
    binary.write_bytes(b"\x00\x01" + FAKE_SK_TOKEN + b"\x00")
    subprocess.run(["git", "-C", str(root), "add", "payload.bin"], check=True)
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--staged", str(root)],
        capture_output=True,
    )
    check("staged_binary_credential_detected", result.returncode == 1)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    result = subprocess.run(
        [sys.executable, str(SCANNER), "--staged", str(root)], capture_output=True,
    )
    check("scanner_runtime_failure_has_distinct_status", result.returncode == 2)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "core.autocrlf", "true"], check=True)
    runtime = root / "runtime.txt"
    runtime.write_bytes(b"first\r\nsecond\r\n")
    approved_env = authorize(root, [runtime])
    working = subprocess.run([sys.executable, str(GUARD), str(root)],
                             capture_output=True, text=True, env=approved_env)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    staged = subprocess.run(
        [sys.executable, str(GUARD), "--staged", str(root)],
        capture_output=True, text=True, env=approved_env,
    )
    check("staged_byte_normalization_cannot_bypass_manifest",
          working.returncode == 0 and staged.returncode == 1
          and "hash mismatch" in staged.stderr)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.invalid"], check=True)
    runtime = root / "runtime.txt"
    runtime.write_text("reviewed bytes\n", encoding="utf-8")
    approved_env = authorize(root, [runtime])
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run([
        "git", "-C", str(root), "-c", "core.hooksPath=/dev/null",
        "commit", "-q", "-m", "authorized",
    ], check=True)
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    committed = subprocess.run(
        [sys.executable, str(GUARD), "--tree-ish", commit, str(root)],
        capture_output=True, text=True, env=approved_env,
    )
    runtime.write_text("uncommitted mutation\n", encoding="utf-8")
    still_committed = subprocess.run(
        [sys.executable, str(GUARD), "--tree-ish", commit, str(root)],
        capture_output=True, text=True, env=approved_env,
    )
    injected = subprocess.run(
        [sys.executable, str(GUARD), "--tree-ish", "HEAD", str(root)],
        capture_output=True, text=True, env=approved_env,
    )
    check("exact_committed_tree_is_validated_independently_of_worktree",
          committed.returncode == 0 and still_committed.returncode == 0)
    check("treeish_revision_expression_is_rejected",
          injected.returncode == 1 and "resolved Git object ID" in injected.stderr)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
