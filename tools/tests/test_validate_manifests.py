#!/usr/bin/env python3
"""Adversarial unit tests for release-manifest helpers."""

from __future__ import annotations

import contextlib
import importlib.util
import copy
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_manifests.py"
spec = importlib.util.spec_from_file_location("validate_manifests", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

public_workflow = (
    module.ROOT / ".github" / "workflows" / "public-assurance.yml"
).read_text(encoding="utf-8")

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
            "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp-server/launch-server.sh"],
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
                "command": "/bin/sh",
                "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp-server/launch-server.sh"],
            },
            "extra": {"command": "node", "args": ["extra.js"]},
        }
    }),
    "private_mcp_package_accepted": module.valid_private_mcp_package(
        {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
            "scripts": {
                "prepublishOnly": "node scripts/block-publish.mjs",
                "test:private": "node tests/private-package.mjs",
                "test:public-lifecycle": "npm run build && npm run test:private && EXPDESIGN_RSCRIPT=/usr/bin/false node tests/lifecycle.mjs --public-only",
                "test:lifecycle": "npm run build && npm run test:private && node tests/lifecycle.mjs",
            },
        },
        {"packages": {"": {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
        }}},
    ),
    "ambient_public_mode_mcp_package_rejected": not module.valid_private_mcp_package(
        {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
            "scripts": {
                "prepublishOnly": "node scripts/block-publish.mjs",
                "test:private": "node tests/private-package.mjs",
                "test:public-lifecycle": "npm run build && npm run test:private && EXPDESIGN_RSCRIPT=/usr/bin/false EXPDESIGN_PUBLIC_ONLY=1 node tests/lifecycle.mjs",
                "test:lifecycle": "npm run build && npm run test:private && node tests/lifecycle.mjs",
            },
        },
        {"packages": {"": {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
        }}},
    ),
    "full_lifecycle_public_mode_downgrade_rejected": not module.valid_private_mcp_package(
        {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
            "scripts": {
                "prepublishOnly": "node scripts/block-publish.mjs",
                "test:private": "node tests/private-package.mjs",
                "test:public-lifecycle": "npm run build && npm run test:private && EXPDESIGN_RSCRIPT=/usr/bin/false node tests/lifecycle.mjs --public-only",
                "test:lifecycle": "npm run build && npm run test:private && node tests/lifecycle.mjs --public-only",
            },
        },
        {"packages": {"": {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
        }}},
    ),
    "publishable_mcp_package_rejected": not module.valid_private_mcp_package(
        {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": False,
            "scripts": {
                "prepublishOnly": "node scripts/block-publish.mjs",
                "test:private": "node tests/private-package.mjs",
                "test:lifecycle": "npm run test:private",
            },
        },
        {"packages": {"": {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
        }}},
    ),
    "missing_publish_blocker_rejected": not module.valid_private_mcp_package(
        {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
            "scripts": {
                "test:private": "node tests/private-package.mjs",
                "test:lifecycle": "npm run test:private",
            },
        },
        {"packages": {"": {
            "name": "experiment-design-mcp", "version": "1.1.0", "private": True,
        }}},
    ),
    "public_assurance_workflow_accepted": (
        module.valid_public_assurance_workflow(public_workflow)
    ),
    "public_assurance_write_permission_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace("contents: read", "contents: write")
        )
    ),
    "public_assurance_other_write_permission_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "  contents: read", "  contents: read\n  issues: write"
            )
        )
    ),
    "public_assurance_write_all_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "permissions:\n  contents: read", "permissions: write-all"
            )
        )
    ),
    "public_assurance_inline_write_permission_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "permissions:\n  contents: read",
                'permissions: { contents: "write" }',
            )
        )
    ),
    "public_assurance_job_permission_override_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "    runs-on: ubuntu-24.04",
                '    runs-on: ubuntu-24.04\n    permissions: { issues: "write" }',
                1,
            )
        )
    ),
    "public_assurance_pull_request_target_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace("  pull_request:\n", "  pull_request_target:\n")
        )
    ),
    "public_assurance_floating_action_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09",
                "actions/checkout@v5",
            )
        )
    ),
    "public_assurance_list_style_floating_action_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "      - name: Check out reviewed source\n"
                "        uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5\n"
                "        with:\n"
                "          persist-credentials: false",
                "      - uses: actions/checkout@v5",
                1,
            )
        )
    ),
    "public_assurance_extra_floating_action_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "      - name: Validate the public source-review distribution",
                "      - name: Unpinned extra action\n"
                "        uses: actions/cache@v4\n"
                "      - name: Validate the public source-review distribution",
                1,
            )
        )
    ),
    "public_assurance_commented_lifecycle_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "run: npm run test:public-lifecycle",
                "run: true # npm run test:public-lifecycle",
            )
        )
    ),
    "public_assurance_ambient_public_mode_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                '"$pythonLocation/bin/python" -I -E -s -S -B agent-harness/tests/test_mcp_client.py --public-only',
                'EXPDESIGN_PUBLIC_ONLY=1 "$pythonLocation/bin/python" -I -E -s -S -B agent-harness/tests/test_mcp_client.py',
            )
        )
    ),
    "public_assurance_commented_credential_scan_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "          set -o pipefail\n",
                "          true # tools/check_diff_credentials.py\n",
            )
        )
    ),
    "public_assurance_persisted_checkout_credentials_rejected": not (
        module.valid_public_assurance_workflow(
            public_workflow.replace(
                "persist-credentials: false", "persist-credentials: true", 1
            )
        )
    ),
    "record_launcher_accepted": module.valid_hook_launcher({
        "type": "command",
        "command": "/bin/sh",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                 "experiment-designer"],
        "timeout": 120,
    }, "record", "experiment-designer"),
    "ambient_path_shell_rejected": not module.valid_hook_launcher({
        "type": "command",
        "command": "sh",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                 "experiment-designer"],
        "timeout": 120,
    }, "record", "experiment-designer"),
    "wrong_hook_mode_rejected": not module.valid_hook_launcher({
        "type": "command",
        "command": "/bin/sh",
        "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                 "experiment-designer"],
        "timeout": 120,
    }, "enforce", "experiment-designer"),
    "wrong_hook_scope_rejected": not module.valid_hook_launcher({
        "type": "command",
        "command": "/bin/sh",
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
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                     "experiment-designer"],
            "timeout": 120,
        }]}],
        "Stop": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "enforce",
                     "experiment-designer"],
            "timeout": 120,
        }]}],
    }, "experiment-designer"),
    "coordinator_prompt_binding_hooks_accepted": module.valid_agent_hooks({
        "UserPromptSubmit": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "capture",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "PreToolUse": [{"matcher": "Agent", "hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "bind",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "PostToolBatch": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "Stop": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "enforce",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
    }, "experiment-design-coordinator", coordinator=True),
    "coordinator_missing_capture_rejected": not module.valid_agent_hooks({
        "PreToolUse": [{"matcher": "Agent", "hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "bind",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "PostToolBatch": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "Stop": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "enforce",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
    }, "experiment-design-coordinator", coordinator=True),
    "coordinator_wrong_agent_matcher_rejected": not module.valid_agent_hooks({
        "UserPromptSubmit": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "capture",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "PreToolUse": [{"matcher": "Agent|SendMessage", "hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "bind",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "PostToolBatch": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
        "Stop": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "enforce",
                     "experiment-design-coordinator"],
            "timeout": 120,
        }]}],
    }, "experiment-design-coordinator", coordinator=True),
    "missing_stop_hook_rejected": not module.valid_agent_hooks({
        "PostToolBatch": [{"hooks": [{
            "type": "command", "command": "/bin/sh",
            "args": ["${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh", "record",
                     "experiment-designer"],
            "timeout": 120,
        }]}],
    }, "experiment-designer"),
}

bootstrap_text = (MODULE_PATH.parents[1] / "tools" / "bootstrap.sh").read_text(
    encoding="utf-8"
)
python_launcher_text = (
    MODULE_PATH.parents[1] / "tools" / "run-reviewed-python.sh"
).read_text(encoding="utf-8")
python_runner_text = (
    MODULE_PATH.parents[1] / "tools" / "reviewed_python_runner.py"
).read_text(encoding="utf-8")
publish_sync_text = (
    MODULE_PATH.parents[1] / "tools" / "sync-to-github.sh"
).read_text(encoding="utf-8")
publication_runner_text = (
    MODULE_PATH.parents[1] / "tools" / "run-publication-python.sh"
).read_text(encoding="utf-8")
public_validator_text = (
    MODULE_PATH.parents[1] / "tools" / "validate_public_distribution.py"
).read_text(encoding="utf-8")
lock_text = (MODULE_PATH.parents[1] / "agent-harness" / "requirements.lock").read_text(
    encoding="utf-8"
)
r_integrity = json.loads(
    (MODULE_PATH.parents[1] / "governance" / "r-package-integrity.json").read_text(
        encoding="utf-8"
    )
)
checks["reviewed_r_integrity_manifest_accepted"] = (
    module.valid_r_package_integrity_manifest(r_integrity)
)
checks["public_publication_boundary_accepted"] = module.valid_publish_sync_guards(
    publish_sync_text, publication_runner_text, public_validator_text,
)
checks["publication_runtime_files_are_required"] = {
    "tools/run-publication-python.sh",
    "tools/validate_public_distribution.py",
    "tools/check_publish_source.py",
    "tools/check_diff_credentials.py",
}.issubset(module.REQUIRED_RUNTIME_FILES)
checks["publication_without_explicit_review_flag_rejected"] = (
    not module.valid_publish_sync_guards(
        publish_sync_text.replace("--publish-reviewed) DRY=0 ;;", "--publish) DRY=0 ;;"),
        publication_runner_text,
        public_validator_text,
    )
)
checks["persistent_publication_clone_rejected"] = not module.valid_publish_sync_guards(
    publish_sync_text.replace(
        "TEMP_ROOT=$($MKTEMP_BIN -d /tmp/experiment-design-publication.XXXXXX)",
        "TEMP_ROOT=$HOME/.cache/experiment-design-agent",
    ),
    publication_runner_text,
    public_validator_text,
)
checks["missing_source_revalidation_rejected"] = not module.valid_publish_sync_guards(
    publish_sync_text.replace('validate_tree working "$PUBLISH_SRC"', "true", 1),
    publication_runner_text,
    public_validator_text,
)
checks["missing_staged_public_gate_rejected"] = not module.valid_publish_sync_guards(
    publish_sync_text.replace('validate_tree staged "$CLONE"', "true", 1),
    publication_runner_text,
    public_validator_text,
)
checks["missing_committed_public_validator_rejected"] = not module.valid_publish_sync_guards(
    publish_sync_text.replace(
        '"$PYTHON_RUN" "$PUBLIC_GUARD" --tree-ish "$object_id" "$tree"',
        "true",
        1,
    ),
    publication_runner_text,
    public_validator_text,
)
checks["wrong_public_repository_identity_rejected"] = not module.valid_publish_sync_guards(
    publish_sync_text,
    publication_runner_text,
    public_validator_text.replace("R_kgDOTySG9w", "wrong-node", 1),
)
checks["ambient_gh_config_reuse_rejected"] = not module.valid_publish_sync_guards(
    publish_sync_text.replace(
        'GH_CONFIG_DIR="$GH_ISOLATED_CONFIG" GH_TOKEN="$publication_token"',
        'HOME="$OWNER_HOME"',
    ),
    publication_runner_text,
    public_validator_text,
)
checks["missing_public_clone_committed_mode_rejected"] = (
    not module.valid_publish_sync_guards(
        publish_sync_text,
        publication_runner_text,
        public_validator_text.replace(
            'mode.add_argument("--public-clone", type=Path)',
            'mode.add_argument("--working-tree", type=Path)',
            1,
        ),
    )
)
checks["hardcoded_publication_home_rejected"] = not module.valid_publish_sync_guards(
    publish_sync_text + "\nOWNER_HOME=/" + "Users/example\n",
    publication_runner_text,
    public_validator_text,
)
checks["arbitrary_publication_python_target_rejected"] = (
    not module.valid_publish_sync_guards(
        publish_sync_text,
        publication_runner_text.replace(
            '"$script_dir/check_diff_credentials.py") ;;',
            '"$script_dir/check_diff_credentials.py"|"$script_dir/arbitrary.py") ;;',
        ),
        public_validator_text,
    )
)
weakened_r_boundary = copy.deepcopy(r_integrity)
weakened_r_boundary["proof_boundary"] = "versions are enough"
checks["weakened_r_integrity_boundary_rejected"] = (
    not module.valid_r_package_integrity_manifest(weakened_r_boundary)
)
malformed_r_digest = copy.deepcopy(r_integrity)
first_environment = malformed_r_digest["environments"][0]
first_package = next(iter(first_environment["packages"].values()))
first_package["tree_sha256"] = "not-a-sha256"
checks["malformed_r_tree_digest_rejected"] = (
    not module.valid_r_package_integrity_manifest(malformed_r_digest)
)
checks["fully_hashed_python_lock_accepted"] = module.valid_python_requirements_lock(
    lock_text
)


def remove_first_entry_hashes(value: str) -> str:
    """Return a syntactically pinned lock with one deliberately unhashed entry."""
    output: list[str] = []
    inside_first_entry = False
    first_entry_finished = False
    for line in value.splitlines():
        if module.LOCK_PIN_RE.fullmatch(line):
            if inside_first_entry:
                first_entry_finished = True
            inside_first_entry = not first_entry_finished
            output.append(line)
            continue
        if inside_first_entry and "--hash=" in line:
            continue
        output.append(line)
    return "\n".join(output) + "\n"


checks["unhashed_python_lock_entry_rejected"] = not module.valid_python_requirements_lock(
    remove_first_entry_hashes(lock_text)
)
checks["malformed_python_lock_hash_rejected"] = not module.valid_python_requirements_lock(
    lock_text.replace("--hash=sha256:", "--hash=sha256:not-a-digest-", 1)
)
checks["plain_pin_with_disconnected_hash_rejected"] = not module.valid_python_requirements_lock(
    "example==1.0.0\n# detached hash must not bind\n"
    + "    --hash=sha256:" + "0" * 64 + "\n"
)
checks["continued_pin_with_interrupted_hashes_rejected"] = (
    not module.valid_python_requirements_lock(
        "example==1.0.0 \\\n# interruption is not part of the requirement\n"
        + "    --hash=sha256:" + "0" * 64 + "\n"
    )
)
checks["full_python_lock_validation_accepted"] = module.valid_bootstrap_python_lock(
    bootstrap_text
)
checks["separate_claude_check_semantics_accepted"] = module.valid_claude_check_semantics(
    bootstrap_text
)
checks["missing_live_auth_probe_rejected"] = not module.valid_claude_check_semantics(
    bootstrap_text.replace('"$REVIEWED_CLAUDE" auth status', "true", 1)
)
checks["direct_only_python_validation_rejected"] = not module.valid_bootstrap_python_lock(
    bootstrap_text.replace(
        "tools/run-reviewed-python.sh --validate-only",
        '"$HARNESS_PYTHON" -E -s -S -B tools/validate_python_environment.py',
        1,
    )
)
checks["reviewed_python_runner_policy_accepted"] = module.valid_reviewed_python_runner(
    python_launcher_text, python_runner_text,
)
checks["runner_without_isolation_flag_rejected"] = not module.valid_reviewed_python_runner(
    python_launcher_text.replace(" -I \\\n", " \\\n", 1), python_runner_text,
)
checks["runner_without_source_symlink_policy_rejected"] = (
    not module.valid_reviewed_python_runner(
        python_launcher_text,
        python_runner_text.replace(
            "symlinked project source entry is forbidden",
            "project source entry could not be inspected",
        ),
    )
)
relative_python = subprocess.run(
    ["/bin/bash", "tools/bootstrap.sh", "--check-python-lock"],
    cwd=MODULE_PATH.parents[1],
    env={**os.environ, "EXPDESIGN_PYTHON": "agent-harness/.venv/bin/python"},
    capture_output=True,
    text=True,
    check=False,
)
checks["relative_python_override_rejected"] = (
    relative_python.returncode != 0
    and "EXPDESIGN_PYTHON must be an absolute path"
    in relative_python.stdout + relative_python.stderr
)
relative_node = subprocess.run(
    ["/bin/bash", "tools/bootstrap.sh", "--check-claude-version"],
    cwd=MODULE_PATH.parents[1],
    env={**os.environ, "EXPDESIGN_NODE": "node"},
    capture_output=True,
    text=True,
    check=False,
)
checks["relative_node_override_rejected"] = (
    relative_node.returncode != 0
    and "EXPDESIGN_NODE must be an absolute path"
    in relative_node.stdout + relative_node.stderr
)


def run_with_fake_claude(directory: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/bash", "tools/bootstrap.sh", "--check-claude-version"],
        cwd=MODULE_PATH.parents[1],
        env={
            **os.environ,
            "PATH": f"{directory}:/usr/bin:/bin",
            "EXPDESIGN_CLAUDE": str(directory / "claude"),
        },
        capture_output=True,
        text=True,
        check=False,
    )


with tempfile.TemporaryDirectory() as directory:
    fake_bin = Path(directory)
    (fake_bin / "claude").symlink_to("/bin/bash")
    fake_bash = run_with_fake_claude(fake_bin)
    checks["unrelated_executable_semver_is_rejected"] = (
        fake_bash.returncode != 0
        and "unrecognized Claude Code product/version output"
        in fake_bash.stdout + fake_bash.stderr
    )

with tempfile.TemporaryDirectory() as directory:
    fake_bin = Path(directory)
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/sh\nprintf '%s\\n' '99.99.99 (Not Claude Code)'\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    fake_semver = run_with_fake_claude(fake_bin)
    checks["fake_high_semver_product_is_rejected"] = (
        fake_semver.returncode != 0
        and "unrecognized Claude Code product/version output"
        in fake_semver.stdout + fake_semver.stderr
    )

with tempfile.TemporaryDirectory() as directory:
    fake_bin = Path(directory)
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        "#!/bin/sh\nprintf '%s\\n' '2.1.226 (Claude Code)'\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    canonical_claude = run_with_fake_claude(fake_bin)
    checks["canonical_claude_code_output_is_accepted"] = (
        canonical_claude.returncode == 0
        and "claude:  2.1.226 (Claude Code)" in canonical_claude.stdout
    )

relative_claude = subprocess.run(
    ["/bin/bash", "tools/bootstrap.sh", "--check-claude-version"],
    cwd=MODULE_PATH.parents[1],
    env={**os.environ, "EXPDESIGN_CLAUDE": "claude"},
    capture_output=True,
    text=True,
    check=False,
)
checks["relative_claude_override_rejected"] = (
    relative_claude.returncode != 0
    and "EXPDESIGN_CLAUDE must be an absolute path"
    in relative_claude.stdout + relative_claude.stderr
)

node_independent_python_check = subprocess.run(
    ["/bin/bash", "tools/bootstrap.sh", "--check-python-lock"],
    cwd=MODULE_PATH.parents[1],
    env={
        **os.environ,
        "EXPDESIGN_NODE": "/definitely/not/an/executable/node",
        "EXPDESIGN_PYTHON": sys.executable,
    },
    capture_output=True,
    text=True,
    check=False,
)
checks["python_lock_check_does_not_require_node"] = (
    node_independent_python_check.returncode == 0
    and "Python requirements.lock check passed"
    in node_independent_python_check.stdout
)

makefile_text = (MODULE_PATH.parents[1] / "Makefile").read_text(encoding="utf-8")
checks["authentication_independent_release_entrypoints_accepted"] = module.valid_release_entrypoints(
    makefile_text
)
with tempfile.TemporaryDirectory() as directory:
    hostile_root = Path(directory)
    marker = hostile_root / "sitecustomize-executed"
    (hostile_root / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "import os\n"
        "Path(os.environ['EXPDESIGN_PYTHON_MARKER']).write_text('executed')\n",
        encoding="utf-8",
    )
    isolated_make = subprocess.run(
        ["make", "-s", "validate-config"],
        cwd=MODULE_PATH.parents[1],
        env={
            **os.environ,
            "PYTHONPATH": str(hostile_root),
            "EXPDESIGN_PYTHON_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    checks["release_python_entrypoints_ignore_hostile_pythonpath"] = (
        isolated_make.returncode == 0 and not marker.exists()
    )
checks["live_auth_in_release_gate_rejected"] = not module.valid_release_entrypoints(
    makefile_text.replace(
        "\t@$(MAKE) check-claude-version\n",
        "\t@$(MAKE) check-claude-version\n\t@$(MAKE) check-claude-live\n",
        1,
    )
)
checks["live_auth_in_harness_gate_rejected"] = not module.valid_release_entrypoints(
    makefile_text.replace(
        "\t@$(MAKE) validate-python-lock\n",
        "\t@$(MAKE) validate-python-lock\n\t@$(MAKE) check-claude-live\n",
        1,
    )
)
registry = json.loads((MODULE_PATH.parents[1] / "governance" / "agents.json").read_text(
    encoding="utf-8"
))
checks["registry_schema_accepted"] = module.validate_registry(registry)["schema_version"] == 1
boolean_registry_version = copy.deepcopy(registry)
boolean_registry_version["schema_version"] = True
try:
    module.validate_registry(boolean_registry_version)
    checks["boolean_registry_schema_version_rejected"] = False
except ValueError:
    checks["boolean_registry_schema_version_rejected"] = True
launcher_policy = module.load_launcher_domain_tool_policy()
checks["launcher_domain_policy_matches_registry_and_python"] = (
    module.valid_domain_tool_policy(registry, launcher_policy)
)
boolean_launcher_version = copy.deepcopy(launcher_policy)
boolean_launcher_version["schema_version"] = True
checks["boolean_launcher_schema_version_rejected"] = not module.valid_domain_tool_policy(
    registry, boolean_launcher_version,
)
hook_environment = dict(os.environ)
hook_environment.pop("EXPDESIGN_NODE", None)
inspection_as_hook = subprocess.run(
    [
        "/bin/sh", str(MODULE_PATH.parents[1] / "hooks" / "launch_verification.sh"),
        "describe-domain-policy", "experiment-designer",
    ],
    cwd=MODULE_PATH.parents[1],
    input=json.dumps({
        "hook_event_name": "Stop", "agent_type": "experiment-designer",
        "session_id": "policy-mode", "prompt_id": "must-block",
    }),
    capture_output=True,
    text=True,
    env=hook_environment,
    check=False,
)
checks["policy_inspection_is_not_a_successful_hook_mode"] = (
    inspection_as_hook.returncode == 2
    and "Unknown verification hook mode" in inspection_as_hook.stderr
    and inspection_as_hook.stdout == ""
)
drifted_launcher_policy = copy.deepcopy(launcher_policy)
drifted_launcher_policy["domain_tools"]["doe"].append("unreviewed_tool")
checks["launcher_domain_policy_drift_is_rejected"] = not module.valid_domain_tool_policy(
    registry, drifted_launcher_policy,
)
drifted_registry_policy = copy.deepcopy(registry)
next(
    agent for agent in drifted_registry_policy["agents"]
    if agent.get("domain") == "doe"
)["tools"].append("mcp__experiment-design__unreviewed_tool")
checks["registry_domain_policy_drift_is_rejected"] = not module.valid_domain_tool_policy(
    drifted_registry_policy, launcher_policy,
)
bad_extra = copy.deepcopy(registry)
bad_extra["agents"][0]["unexpected"] = True
try:
    module.validate_registry(bad_extra)
    checks["registry_extra_field_rejected"] = False
except ValueError:
    checks["registry_extra_field_rejected"] = True
bad_child = copy.deepcopy(registry)
coordinator = next(item for item in bad_child["agents"] if item["role"] == "coordinator")
coordinator["allowed_children"].append("unknown-agent")
coordinator["tools"] = [f"Agent({', '.join(coordinator['allowed_children'])})"]
try:
    module.validate_registry(bad_child)
    checks["registry_unknown_child_rejected"] = False
except ValueError:
    checks["registry_unknown_child_rejected"] = True
original_load_registry = module.load_registry
try:
    def reject_registry(_path):
        raise module.RegistryError("synthetic invalid registry")

    module.load_registry = reject_registry
    invalid_registry_output = io.StringIO()
    with contextlib.redirect_stdout(invalid_registry_output):
        invalid_registry_status = module.main()
    checks["invalid_registry_fails_deterministically"] = (
        invalid_registry_status == 1
        and "invalid governance/agents.json: synthetic invalid registry"
        in invalid_registry_output.getvalue()
    )
except Exception:
    checks["invalid_registry_fails_deterministically"] = False
finally:
    module.load_registry = original_load_registry
coordinator_tools = next(
    item["tools"] for item in registry["agents"] if item["role"] == "coordinator"
)
checks["coordinator_requires_yaml_list"] = (
    module.valid_agent_tools(coordinator_tools, coordinator_tools, require_yaml_list=True)
    and not module.valid_agent_tools(coordinator_tools[0], coordinator_tools, require_yaml_list=True)
)
try:
    module.normalized_tools({"Read": True})
    checks["malformed_tools_rejected"] = False
except ValueError:
    checks["malformed_tools_rejected"] = True

launch_text = (MODULE_PATH.parents[1] / ".claude" / "launch.json").read_text(
    encoding="utf-8"
)
launch_config = json.loads(
    launch_text, object_pairs_hook=module.reject_duplicate_keys,
)
checks["repository_launch_config_accepted"] = module.valid_preview_launch_config(
    launch_config,
)
for label, mutate in (
    ("bind_address", lambda c: c["configurations"][0]["runtimeArgs"].__setitem__(
        c["configurations"][0]["runtimeArgs"].index("127.0.0.1"), "0.0.0.0")),
    ("direct_streamlit", lambda c: c["configurations"][0].__setitem__(
        "runtimeExecutable", "streamlit")),
    ("arbitrary_executable", lambda c: c["configurations"][0].__setitem__(
        "runtimeExecutable", "/bin/sh")),
    ("added_flag", lambda c: c["configurations"][0]["runtimeArgs"].append(
        "--server.enableCORS=false")),
    ("numeric_autoport", lambda c: c["configurations"][0].__setitem__("autoPort", 0)),
    ("true_autoport", lambda c: c["configurations"][0].__setitem__("autoPort", True)),
    ("port_drift", lambda c: c["configurations"][0].__setitem__("port", 8502)),
    ("boolean_port", lambda c: c["configurations"][0].__setitem__("port", True)),
    ("extra_env", lambda c: c["configurations"][0].__setitem__("env", {})),
    ("extra_cwd", lambda c: c["configurations"][0].__setitem__(
        "cwd", "${workspaceFolder}")),
    ("extra_program", lambda c: c["configurations"][0].__setitem__(
        "program", "agent-harness/streamlit_app.py")),
    ("extra_configuration", lambda c: c["configurations"].append(
        c["configurations"][0])),
    ("omitted_autoport", lambda c: c["configurations"][0].pop("autoPort")),
):
    mutated = copy.deepcopy(launch_config)
    mutate(mutated)
    checks[f"launch_config_{label}_mutation_rejected"] = (
        not module.valid_preview_launch_config(mutated)
    )

def duplicate_keys_rejected(raw: str) -> bool:
    try:
        json.loads(raw, object_pairs_hook=module.reject_duplicate_keys)
        return False
    except ValueError:
        return True


checks["duplicate_launch_autoport_rejected"] = duplicate_keys_rejected(
    launch_text.replace(
        '"autoPort": false', '"autoPort": true, "autoPort": false', 1,
    )
)
checks["duplicate_launch_executable_rejected"] = duplicate_keys_rejected(
    launch_text.replace(
        '"runtimeExecutable": "tools/run-reviewed-python.sh"',
        '"runtimeExecutable": "/bin/sh", '
        '"runtimeExecutable": "tools/run-reviewed-python.sh"',
        1,
    )
)

with tempfile.TemporaryDirectory() as directory:
    fixture_root = Path(directory)
    target = fixture_root / "launch-target.json"
    target.write_text(launch_text, encoding="utf-8")
    link = fixture_root / "launch.json"
    link.symlink_to(target)
    checks["launch_config_regular_file_accepted"] = module.is_regular_runtime_file(target)
    checks["launch_config_leaf_symlink_rejected"] = not module.is_regular_runtime_file(link)

checks["repository_manifest_main_passes"] = module.main() == 0

for name, ok in checks.items():
    print(f"TEST {name} : {'PASS' if ok else 'FAIL'}")
sys.exit(1 if not all(checks.values()) else 0)
