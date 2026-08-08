#!/usr/bin/env python3
"""Adversarial unit tests for release-manifest helpers."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_manifests.py"
spec = importlib.util.spec_from_file_location("validate_manifests", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

checks = {
    "string_tools_are_structural": module.normalized_tools("Bash, Read") == {"Bash", "Read"},
    "yaml_list_tools_are_structural": module.normalized_tools(["Bash", "Read"]) == {"Bash", "Read"},
    "alternate_read_syntax_detectable": any(
        tool == "Read" or tool.startswith("Read(") or tool.startswith("Read:")
        for tool in module.normalized_tools(["Bash", "Read(/tmp/**)"])
    ),
    "missing_skill_detected": module.inventory_delta(
        {"vera-a"}, {"vera-a", "vera-b"}) == (["vera-b"], []),
    "unexpected_skill_detected": module.inventory_delta(
        {"vera-a", "vera-b"}, {"vera-a"}) == ([], ["vera-b"]),
    "semantic_version_handles_two_digit_components": (
        module.semantic_version("12.34.56") == (12, 34, 56)
    ),
    "semantic_version_rejects_trailing_text": (
        module.semantic_version("2.1.197-beta") is None
    ),
    "exact_agent_model_accepted": module.valid_agent_model("claude-sonnet-5"),
    "model_downgrade_rejected": not module.valid_agent_model("haiku"),
    "exact_agent_tool_scope_accepted": module.valid_agent_tools(
        "mcp__experiment-design__*"
    ),
    "bash_tool_escape_rejected": not module.valid_agent_tools(
        "mcp__experiment-design__*, Bash"
    ),
    "write_tool_escape_rejected": not module.valid_agent_tools(
        ["mcp__experiment-design__*", "Write"]
    ),
    "foreign_mcp_scope_rejected": not module.valid_agent_tools(
        ["mcp__experiment-design__*", "mcp__filesystem__*"]
    ),
    "exact_mcp_config_accepted": module.valid_mcp_config({
        "mcpServers": {"experiment-design": {
            "command": "node",
            "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp-server/dist/index.js"],
        }}
    }),
    "arbitrary_mcp_command_rejected": not module.valid_mcp_config({
        "mcpServers": {"experiment-design": {
            "command": "curl",
            "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp-server/dist/index.js"],
        }}
    }),
    "extra_mcp_server_rejected": not module.valid_mcp_config({
        "mcpServers": {
            "experiment-design": {
                "command": "node",
                "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp-server/dist/index.js"],
            },
            "extra": {"command": "node", "args": ["extra.js"]},
        }
    }),
    "record_launcher_accepted": module.valid_hook_launcher({
        "type": "command",
        "command": "sh",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                 "experiment-designer"],
        "timeout": 120,
    }, "record", "experiment-designer"),
    "wrong_hook_mode_rejected": not module.valid_hook_launcher({
        "type": "command",
        "command": "sh",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                 "experiment-designer"],
        "timeout": 120,
    }, "enforce", "experiment-designer"),
    "wrong_hook_scope_rejected": not module.valid_hook_launcher({
        "type": "command",
        "command": "sh",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                 "design-verifier"],
        "timeout": 120,
    }, "record", "experiment-designer"),
    "shell_hook_command_rejected": not module.valid_hook_launcher({
        "type": "command",
        "command": "node ${CLAUDE_PROJECT_DIR}/hooks/launch_verification.mjs enforce",
        "timeout": 120,
    }, "enforce", "experiment-designer"),
    "agent_scoped_hooks_accepted": module.valid_agent_hooks({
        "PostToolBatch": [{"hooks": [{
            "type": "command", "command": "sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                     "experiment-designer"],
            "timeout": 120,
        }]}],
        "Stop": [{"hooks": [{
            "type": "command", "command": "sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "enforce",
                     "experiment-designer"],
            "timeout": 120,
        }]}],
    }, "experiment-designer"),
    "missing_stop_hook_rejected": not module.valid_agent_hooks({
        "PostToolBatch": [{"hooks": [{
            "type": "command", "command": "sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                     "experiment-designer"],
            "timeout": 120,
        }]}],
    }, "experiment-designer"),
}
try:
    module.normalized_tools({"Read": True})
    checks["malformed_tools_rejected"] = False
except ValueError:
    checks["malformed_tools_rejected"] = True

checks["repository_manifest_main_passes"] = module.main() == 0

for name, ok in checks.items():
    print(f"TEST {name} : {'PASS' if ok else 'FAIL'}")
sys.exit(1 if not all(checks.values()) else 0)
