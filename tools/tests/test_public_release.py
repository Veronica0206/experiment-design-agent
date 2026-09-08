#!/usr/bin/env python3
"""Exercise exact public export boundaries using synthetic source fixtures."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
from prepare_public_release import CATALOG, INVENTORY, prepare, verify
from public_release_policy import PUBLIC_ENGINE_FILES, public_path_error

passed = 0


def check(name, action):
    global passed
    action()
    passed += 1
    print(f"TEST {name} : PASS")


def expect_failure(action):
    try:
        action()
    except (ValueError, OSError):
        return
    raise AssertionError("unsafe candidate was accepted")


def fixture(root):
    files = sorted(PUBLIC_ENGINE_FILES | {
        CATALOG, "governance/runtime-profiles.json", "governance/agents.json", "README.md",
    })
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Synthetic publication-boundary fixture\n")
    (root / CATALOG).write_text(json.dumps({"schema_version": 1, "files": files}))
    for name in ("runtime-profiles.json", "agents.json"):
        (root / "governance" / name).write_bytes((ROOT / "governance" / name).read_bytes())


with tempfile.TemporaryDirectory(prefix="public-release-tests-") as directory:
    base = Path(directory)
    source = base / "source"
    source.mkdir()
    fixture(source)
    candidate = base / "candidate"
    check("isolated_exact_export", lambda: prepare(source, candidate))
    check("prepared_inventory_matches", lambda: verify(candidate))
    profile = json.loads((candidate / "governance/runtime-profiles.json").read_text())
    registry = json.loads((candidate / "governance/agents.json").read_text())
    assert profile["active"] == "single-endpoint"
    assert len(registry["agents"]) == 4
    assert all(a["allowed_children"] == ["single-endpoint-designer"]
               for a in registry["agents"] if a["role"] == "coordinator")
    check("public_profile_and_registry_projection", lambda: None)
    check("refuses_existing_destination", lambda: expect_failure(lambda: prepare(source, candidate)))
    check("refuses_nested_working_tree_export", lambda: expect_failure(lambda: prepare(source, source / "public")))

    readme = candidate / "README.md"
    original = readme.read_bytes()
    readme.write_bytes(original + b"changed\n")
    check("detects_modified_candidate_bytes", lambda: expect_failure(lambda: verify(candidate)))
    readme.write_bytes(original)

    extra = candidate / "vera-experiment-designing" / "SKILL.md"
    extra.write_text("Synthetic forbidden fixture; no private content.\n")
    check("rejects_unlisted_skill_instructions", lambda: expect_failure(lambda: verify(candidate)))
    extra.unlink()
    symlink = candidate / "unlisted-link"
    symlink.symlink_to(source, target_is_directory=True)
    check("rejects_symlinked_candidate_tree", lambda: expect_failure(lambda: verify(candidate)))
    symlink.unlink()

    catalog = source / CATALOG
    content = catalog.read_bytes()
    data = json.loads(content)
    data["files"].append("vera-master-experiment-designing/scripts/R/private.R")
    data["files"].sort()
    catalog.write_text(json.dumps(data))
    check("cannot_expand_to_another_engine", lambda: expect_failure(lambda: prepare(source, base / "invalid")))
    catalog.write_bytes(content)

    for relative in (
        "vera-experiment-designing/scripts/R/v1_sample_size.R",
        "vera-experiment-designing/reference/templates/analysis-template.R",
        "VERA-experiment-designing/scripts/R/sample_size.R",
        "nested/vera-experiment-designing/scripts/R/sample_size.R",
        "SKILL.md", "bundle.skill.enc", "../README.md",
    ):
        assert public_path_error(relative), relative
    check("exact_engine_exceptions_do_not_expand_scope", lambda: None)
    assert all(public_path_error(name) is None for name in PUBLIC_ENGINE_FILES)
    check("selected_engine_files_are_explicitly_allowed", lambda: None)

    manifest_path = candidate / "publish-manifest.json"
    published_files = json.loads((candidate / INVENTORY).read_text())["files"]
    published_files[INVENTORY] = hashlib.sha256((candidate / INVENTORY).read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps({
        "schema_version": 1,
        "authorization": {"approved_by": "synthetic test", "approved_at": "test", "purpose": "fixture only"},
        "files": published_files,
    }))
    check("published_clone_accepts_complete_matching_manifest", lambda: verify(candidate))
    manifest_path.write_text('{"schema_version":1,"files":{}}')
    check("published_clone_rejects_malformed_manifest", lambda: expect_failure(lambda: verify(candidate)))
    manifest_path.unlink()

    profile_path = candidate / "governance/runtime-profiles.json"
    original = profile_path.read_bytes()
    profile["active"] = "complete"
    profile_path.write_text(json.dumps(profile))
    inventory_path = candidate / INVENTORY
    inventory = json.loads(inventory_path.read_text())
    inventory["files"]["governance/runtime-profiles.json"] = hashlib.sha256(profile_path.read_bytes()).hexdigest()
    inventory_path.write_text(json.dumps(inventory))
    check("public_candidate_cannot_select_complete_profile", lambda: expect_failure(lambda: verify(candidate)))

print(f"Public release tests: {passed} passed, 0 failed")
