#!/usr/bin/env python3
"""Regression tests for the repository skill validator."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "tools" / "validate_skills.py"


def make_skill(root: Path, r_source: str, prompts: object | None = None) -> Path:
    skill = root / "example-skill"
    (skill / "agents").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: example-skill\ndescription: Synthetic validator fixture.\n---\n",
        encoding="utf-8",
    )
    (skill / "agents" / "openai.yaml").write_text(
        "interface:\n  display_name: Example\npolicy:\n  allow_implicit_invocation: false\n",
        encoding="utf-8",
    )
    payload = prompts if prompts is not None else {
        "prompts": [{"id": "one", "prompt": "Run the example.", "expected": "success"}],
    }
    (skill / "test-prompts.json").write_text(
        json.dumps(payload), encoding="utf-8",
    )
    (skill / "scripts" / "example.R").write_text(r_source, encoding="utf-8")
    return skill


def run(skill: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VALIDATOR), str(skill)],
        capture_output=True, text=True, check=False,
    )


checks: dict[str, bool] = {}
with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    checks["valid_r_skill_passes"] = run(
        make_skill(base / "valid", "f <- function(x) x + 1\n"),
    ).returncode == 0
    invalid_r = run(make_skill(base / "invalid-r", "f <- function(\n"))
    checks["invalid_r_is_rejected"] = (
        invalid_r.returncode == 1 and "does not parse" in invalid_r.stdout
    )
    hardcoded = run(make_skill(
        base / "hardcoded", 'source_path <- "/Users/person/private.csv"\n',
    ))
    checks["hardcoded_path_in_r_is_rejected"] = (
        hardcoded.returncode == 1 and "hardcoded user path" in hardcoded.stdout
    )
    malformed_prompts = run(make_skill(
        base / "prompts", "x <- 1\n",
        {"prompts": [{"id": "one", "prompt": "Missing expected."}]},
    ))
    checks["malformed_prompt_contract_is_rejected"] = (
        malformed_prompts.returncode == 1 and "non-empty id, prompt, and expected" in malformed_prompts.stdout
    )

failed = [name for name, passed in checks.items() if not passed]
for name, passed in checks.items():
    print(f"TEST {name} : {'PASS' if passed else 'FAIL'}")
raise SystemExit(1 if failed else 0)
