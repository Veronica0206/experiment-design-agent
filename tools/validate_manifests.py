#!/usr/bin/env python3
"""Validate Codex manifests and project runtime configuration."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_SKILL_NAMES = {
    "vera-doe-designing",
    "vera-experiment-designing",
    "vera-indirect-comparing",
    "vera-master-experiment-designing",
    "vera-meta-analyzing",
}
EXPECTED_AGENT_NAMES = {"design-verifier.md", "experiment-designer.md"}
MINIMUM_CLAUDE_CODE = (2, 1, 197)
EXPECTED_AGENT_MODEL = "claude-sonnet-5"
EXPECTED_AGENT_TOOLS = {"mcp__experiment-design__*"}
REQUIRED_RUNTIME_FILES = {
    "hooks/launch_verification.sh",
    "hooks/launch_verification.mjs",
    "hooks/record_verification.py",
    "hooks/enforce_verification.py",
}


def normalized_tools(value: object) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return {item.strip() for item in value if item.strip()}
    raise ValueError("tools must be a comma-separated string or a YAML list of strings")


def valid_agent_model(value: object) -> bool:
    return value == EXPECTED_AGENT_MODEL


def valid_agent_tools(value: object) -> bool:
    try:
        return normalized_tools(value) == EXPECTED_AGENT_TOOLS
    except ValueError:
        return False


def inventory_delta(discovered: set[str], expected: set[str]) -> tuple[list[str], list[str]]:
    return sorted(expected - discovered), sorted(discovered - expected)


def semantic_version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value.strip())
    return tuple(map(int, match.groups())) if match else None


def valid_mcp_config(value: object) -> bool:
    expected = {
        "mcpServers": {
            "experiment-design": {
                "command": "node",
                "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp-server/dist/index.js"],
            }
        }
    }
    return value == expected


def valid_hook_launcher(value: object, mode: str, agent_scope: str) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("type") == "command"
        and value.get("command") == "sh"
        and value.get("args") == [
            "${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh",
            mode,
            agent_scope,
        ]
        and isinstance(value.get("timeout"), int)
        and value["timeout"] >= 10
    )


def valid_agent_hooks(value: object, agent_scope: str) -> bool:
    """Require one scoped recorder and one scoped stop gate in agent frontmatter."""
    if not isinstance(value, dict) or set(value) != {"PostToolBatch", "Stop"}:
        return False
    expected_modes = {"PostToolBatch": "record", "Stop": "enforce"}
    for event, mode in expected_modes.items():
        entries = value.get(event)
        if not isinstance(entries, list) or len(entries) != 1:
            return False
        entry = entries[0]
        if not isinstance(entry, dict) or set(entry) != {"hooks"}:
            return False
        commands = entry.get("hooks")
        if not isinstance(commands, list) or len(commands) != 1:
            return False
        if not valid_hook_launcher(commands[0], mode, agent_scope):
            return False
    return True


def fail(message: str, failures: list[str]) -> None:
    failures.append(message)
    print(f"FAIL: {message}")


def main() -> int:
    failures: list[str] = []
    for relative in sorted(REQUIRED_RUNTIME_FILES):
        path = ROOT / relative
        if not path.is_file() or path.is_symlink():
            fail(f"required runtime file must be a regular file: {relative}", failures)
    discovered_skills = {path.parent.name for path in ROOT.glob("vera-*/SKILL.md")}
    missing_skills, unexpected_skills = inventory_delta(discovered_skills, EXPECTED_SKILL_NAMES)
    if missing_skills or unexpected_skills:
        fail(f"skill inventory mismatch; missing={missing_skills}, unexpected={unexpected_skills}", failures)
    skills = [ROOT / name for name in sorted(EXPECTED_SKILL_NAMES)]

    agent_root = ROOT / ".claude" / "agents"
    discovered_agents = {path.name for path in agent_root.glob("*.md")}
    missing_agents, unexpected_agents = inventory_delta(discovered_agents, EXPECTED_AGENT_NAMES)
    if missing_agents or unexpected_agents:
        fail(f"Claude agent inventory mismatch; missing={missing_agents}, unexpected={unexpected_agents}", failures)
    claude_agents = [agent_root / name for name in sorted(EXPECTED_AGENT_NAMES)]

    for skill in skills:
        manifest_path = skill / "agents" / "openai.yaml"
        try:
            manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"{skill.name}: invalid agents/openai.yaml: {exc}", failures)
            continue
        interface = manifest.get("interface", {}) if isinstance(manifest, dict) else {}
        description = interface.get("short_description")
        prompt = interface.get("default_prompt")
        if not isinstance(description, str) or not 25 <= len(description) <= 64:
            fail(f"{skill.name}: short_description must be 25-64 characters", failures)
        if not isinstance(prompt, str) or f"${skill.name}" not in prompt:
            fail(f"{skill.name}: default_prompt must mention ${skill.name}", failures)
        policy = manifest.get("policy", {}) if isinstance(manifest, dict) else {}
        if not isinstance(policy.get("allow_implicit_invocation"), bool):
            fail(f"{skill.name}: allow_implicit_invocation must be boolean", failures)

    try:
        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        if not valid_mcp_config(mcp):
            fail(".mcp.json must contain only the approved experiment-design Node server", failures)
    except Exception as exc:
        fail(f"invalid .mcp.json: {exc}", failures)

    try:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        if settings != {"hooks": {}}:
            fail(
                ".claude/settings.json must leave verification hooks agent-scoped in frontmatter",
                failures,
            )
    except Exception as exc:
        fail(f"invalid .claude/settings.json: {exc}", failures)

    try:
        bootstrap = (ROOT / "tools" / "bootstrap.sh").read_text(encoding="utf-8")
        match = re.search(
            r'^MIN_CLAUDE_CODE_VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$',
            bootstrap,
            flags=re.MULTILINE,
        )
        configured = semantic_version(match.group(1)) if match else None
        if configured is None or configured < MINIMUM_CLAUDE_CODE:
            fail(
                "tools/bootstrap.sh must require Claude Code 2.1.197+ for "
                "Claude Sonnet 5 agent compatibility",
                failures,
            )
    except Exception as exc:
        fail(f"invalid Claude Code bootstrap requirement: {exc}", failures)

    for path in claude_agents:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n") or "\n---\n" not in text[4:]:
            fail(f"{path.name}: missing YAML frontmatter", failures)
            continue
        frontmatter = text.split("\n---\n", 1)[0][4:]
        try:
            agent = yaml.safe_load(frontmatter)
        except Exception as exc:
            fail(f"{path.name}: invalid frontmatter: {exc}", failures)
            continue
        if not valid_agent_model(agent.get("model")):
            fail(
                f"{path.name}: model must be {EXPECTED_AGENT_MODEL!r}",
                failures,
            )
        scope = path.stem
        if agent.get("name") != scope:
            fail(f"{path.name}: frontmatter name must be {scope!r}", failures)
        if not valid_agent_hooks(agent.get("hooks"), scope):
            fail(
                f"{path.name}: hooks must contain the exact scoped PostToolBatch recorder "
                "and Stop enforcer",
                failures,
            )
        if not valid_agent_tools(agent.get("tools", "")):
            fail(
                f"{path.name}: tools must be exactly the experiment-design MCP wildcard",
                failures,
            )

    if failures:
        print(f"\n{len(failures)} manifest/config validation failure(s)")
        return 1
    print(f"Validated {len(skills)} skill manifests and project runtime configuration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
