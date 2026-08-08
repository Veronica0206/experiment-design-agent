#!/usr/bin/env python3
"""Standalone structural validator for this repository's skill folders."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml


ALLOWED_FRONTMATTER = {"name", "description", "license", "allowed-tools", "metadata"}
IGNORE_DIRS = {".git", ".venv", "__pycache__", "runs"}
IGNORE_FILES = {".DS_Store"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
ABS_PATH_RE = re.compile(r"/Users/(?!<)[A-Za-z0-9_.-]+/")


def frontmatter(path: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---(?:\n|$)", content, re.DOTALL)
    if not match:
        raise ValueError("missing or malformed YAML frontmatter")
    parsed = yaml.safe_load(match.group(1)) or {}
    if not isinstance(parsed, dict):
        raise ValueError("frontmatter must be a mapping")
    return parsed


def discover(target: Path) -> list[Path]:
    if (target / "SKILL.md").is_file():
        return [target]
    found: list[Path] = []
    for directory, names, files in os.walk(target, followlinks=False):
        names[:] = [name for name in names if name not in IGNORE_DIRS and not name.startswith(".")]
        if "SKILL.md" in files:
            found.append(Path(directory))
            names[:] = []
    return sorted(found)


def iter_files(root: Path) -> list[Path]:
    return [
        path for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name not in IGNORE_FILES
        and not any(part in IGNORE_DIRS for part in path.relative_to(root).parts)
    ]


def validate(skill: Path) -> list[str]:
    errors: list[str] = []
    try:
        metadata = frontmatter(skill / "SKILL.md")
    except Exception as exc:
        return [f"{skill.name}: SKILL.md: {exc}"]
    extra = set(metadata) - ALLOWED_FRONTMATTER
    if extra:
        errors.append(f"{skill.name}: unsupported frontmatter keys: {sorted(extra)}")
    name = metadata.get("name")
    if name != skill.name or not isinstance(name, str) or len(name) > 64 or not NAME_RE.fullmatch(name):
        errors.append(f"{skill.name}: invalid or mismatched frontmatter name")
    description = metadata.get("description")
    if (not isinstance(description, str) or not description.strip()
            or len(description) > 1024 or "<" in description or ">" in description):
        errors.append(f"{skill.name}: invalid frontmatter description")

    manifest = skill / "agents" / "openai.yaml"
    if not manifest.is_file():
        errors.append(f"{skill.name}: missing agents/openai.yaml")
    else:
        try:
            if not isinstance(yaml.safe_load(manifest.read_text(encoding="utf-8")), dict):
                errors.append(f"{skill.name}: agents/openai.yaml must be a mapping")
        except Exception as exc:
            errors.append(f"{skill.name}: invalid agents/openai.yaml: {exc}")

    prompts = skill / "test-prompts.json"
    if not prompts.is_file():
        errors.append(f"{skill.name}: missing test-prompts.json")

    for path in iter_files(skill):
        relative = path.relative_to(skill).as_posix()
        if path.suffix == ".json":
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if path.name == "test-prompts.json" and (
                    not isinstance(value, dict) or not isinstance(value.get("prompts"), list)
                    or not value["prompts"]
                ):
                    errors.append(f"{skill.name}: test-prompts.json needs a non-empty prompts list")
            except Exception as exc:
                errors.append(f"{skill.name}: {relative} invalid JSON: {exc}")
        elif path.suffix == ".py":
            try:
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
            except Exception as exc:
                errors.append(f"{skill.name}: {relative} does not compile: {exc}")
        if path.suffix in {".md", ".json", ".yaml", ".yml", ".txt", ".py", ".sh"}:
            if ABS_PATH_RE.search(path.read_text(encoding="utf-8", errors="replace")):
                errors.append(f"{skill.name}: {relative} contains a hardcoded user path")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="*", type=Path, default=[Path(".")])
    args = parser.parse_args()
    skills: list[Path] = []
    for target in args.targets:
        if not target.is_dir():
            print(f"ERROR: {target} is not a directory", file=sys.stderr)
            return 1
        skills.extend(discover(target))
    skills = list({path.resolve(): path for path in skills}.values())
    if not skills:
        print("ERROR: no skill folders found", file=sys.stderr)
        return 1
    errors = [error for skill in skills for error in validate(skill)]
    print(f"Checked {len(skills)} skills.")
    for error in errors:
        print(f"ERROR: {error}")
    if errors:
        return 1
    print("Validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
