#!/usr/bin/env python3
"""Validate Codex manifests and project runtime configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance.registry import (  # noqa: E402
    DOMAIN_TOOLS, MCP_PREFIX, REGISTRY_PATH, RegistryError, load_registry,
    validate_registry,
)

EXPECTED_SKILL_NAMES = {
    "vera-doe-designing",
    "vera-experiment-designing",
    "vera-indirect-comparing",
    "vera-master-experiment-designing",
    "vera-meta-analyzing",
}
MINIMUM_CLAUDE_CODE = (2, 1, 197)
EXPECTED_AGENT_MODEL = "claude-sonnet-5"
EXPECTED_AGENT_TOOLS = {"mcp__experiment-design__*"}
R_INTEGRITY_PROOF_BOUNDARY = (
    "Pins installed package-tree paths and bytes after an operator-reviewed "
    "restore; it does not authenticate source or binary archives."
)
REQUIRED_RUNTIME_FILES = {
    ".github/workflows/public-assurance.yml",
    ".claude/launch.json",
    "governance/agents.json",
    "governance/r-package-integrity.json",
    "mcp-server/launch-server.sh",
    "tools/run-reviewed-r.sh",
    "tools/run-reviewed-python.sh",
    "tools/run-publication-python.sh",
    "tools/check_publish_source.py",
    "tools/check_diff_credentials.py",
    "tools/reviewed_python_runner.py",
    "tools/sanitize_python_environment.py",
    "tools/validate_r_environment.py",
    "tools/validate_r_lock.R",
    "tools/validate_python_environment.py",
    "tools/validate_public_distribution.py",
    "hooks/launch_verification.sh",
    "hooks/launch_verification.mjs",
    "hooks/domain_tool_policy.mjs",
    "hooks/describe_domain_policy.mjs",
    "hooks/private_state.py",
    "hooks/prompt_binding.py",
    "hooks/prompt_binding_hook.py",
    "hooks/record_verification.py",
    "hooks/enforce_verification.py",
}

LOCK_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s+\\\s*$")
LOCK_HASH_RE = re.compile(
    r"^\s*--hash=sha256:([0-9a-f]{64})(?:\s+(\\))?\s*$"
)


def normalized_tools(value: object) -> set[str]:
    if isinstance(value, str):
        return {item.strip() for item in value.split(",") if item.strip()}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return {item.strip() for item in value if item.strip()}
    raise ValueError("tools must be a comma-separated string or a YAML list of strings")


def valid_agent_model(value: object) -> bool:
    return value == EXPECTED_AGENT_MODEL


def valid_agent_tools(
    value: object, expected: object = EXPECTED_AGENT_TOOLS, *, require_yaml_list: bool = False,
) -> bool:
    try:
        if require_yaml_list and not isinstance(value, list):
            return False
        return normalized_tools(value) == set(expected)  # type: ignore[arg-type]
    except ValueError:
        return False


def inventory_delta(discovered: set[str], expected: set[str]) -> tuple[list[str], list[str]]:
    return sorted(expected - discovered), sorted(discovered - expected)


def is_regular_runtime_file(path: Path) -> bool:
    """Require the named runtime entry itself to be a regular, non-symlink file."""
    return path.is_file() and not path.is_symlink()


def _registry_domain_tool_policy(value: object) -> dict[str, list[str]] | None:
    """Extract exact unprefixed domain grants without trusting registry validity."""
    if not isinstance(value, dict) or not isinstance(value.get("agents"), list):
        return None
    policy: dict[str, list[str]] = {}
    for agent in value["agents"]:
        if not isinstance(agent, dict) or agent.get("role") != "domain_executor":
            continue
        domain = agent.get("domain")
        tools = agent.get("tools")
        if (
            not isinstance(domain, str)
            or domain in policy
            or not isinstance(tools, list)
            or not tools
            or not all(
                isinstance(tool, str)
                and tool.startswith(MCP_PREFIX)
                and len(tool) > len(MCP_PREFIX)
                for tool in tools
            )
        ):
            return None
        policy[domain] = [tool[len(MCP_PREFIX):] for tool in tools]
    return policy


def valid_domain_tool_policy(registry: object, launcher_document: object) -> bool:
    """Require exact JSON, Python, and fixed JS-launcher authorization parity."""
    schema_version = (
        launcher_document.get("schema_version")
        if isinstance(launcher_document, dict) else None
    )
    if (
        not isinstance(launcher_document, dict)
        or set(launcher_document) != {"schema_version", "domain_tools"}
        or isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != 1
    ):
        return False
    launcher_policy = launcher_document.get("domain_tools")
    if not isinstance(launcher_policy, dict) or set(launcher_policy) != set(DOMAIN_TOOLS):
        return False
    if any(
        not isinstance(domain, str)
        or not isinstance(tools, list)
        or not tools
        or not all(isinstance(tool, str) and tool for tool in tools)
        or len(tools) != len(set(tools))
        for domain, tools in launcher_policy.items()
    ):
        return False
    python_policy = {domain: list(tools) for domain, tools in DOMAIN_TOOLS.items()}
    return _registry_domain_tool_policy(registry) == python_policy == launcher_policy


def load_launcher_domain_tool_policy() -> object:
    """Query the frozen JS authorization module through its side-effect-free CLI."""
    configured = os.environ.get("EXPDESIGN_NODE")
    if configured is not None and not Path(configured).is_absolute():
        raise ValueError("EXPDESIGN_NODE must be an absolute path")
    candidates = (
        [Path(configured)] if configured is not None else [
            Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node"),
            Path("/usr/bin/node"), Path("/opt/local/bin/node"),
        ]
    )
    node: Path | None = None
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK):
            node = resolved
            break
    if node is None:
        raise ValueError("no approved absolute Node.js executable is available")
    inspector = ROOT / "hooks" / "describe_domain_policy.mjs"
    environment = {
        "PATH": f"{node.parent}:/usr/bin:/bin",
        **{
            name: os.environ[name]
            for name in ("LANG", "LC_ALL", "TMPDIR")
            if name in os.environ
        },
    }
    completed = subprocess.run(
        [str(node), str(inspector)],
        cwd=ROOT,
        input="",
        capture_output=True,
        text=True,
        env=environment,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0 or completed.stderr or not completed.stdout:
        raise ValueError("policy inspector could not describe the fixed domain-tool policy")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("policy inspector returned malformed domain-tool policy JSON") from exc


def semantic_version(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value.strip())
    return tuple(map(int, match.groups())) if match else None


def valid_mcp_config(value: object) -> bool:
    expected = {
        "mcpServers": {
            "experiment-design": {
                "command": "/bin/sh",
                "args": ["${CLAUDE_PROJECT_DIR:-.}/mcp-server/launch-server.sh"],
            }
        }
    }
    return value == expected


def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Fail closed on duplicate JSON keys instead of silently keeping the last."""
    seen: dict[str, object] = {}
    for key, item in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key: {key}")
        seen[key] = item
    return seen


def strictly_equal(value: object, expected: object) -> bool:
    """Compare by exact type as well as value.

    Python treats False == 0 and True == 1, so a plain equality check would
    accept `"autoPort": 0` as the reviewed `false`, and `"port": true` as 1.
    """
    if type(value) is not type(expected):
        return False
    if isinstance(expected, dict):
        return set(value) == set(expected) and all(  # type: ignore[arg-type]
            strictly_equal(value[key], expected[key])  # type: ignore[index]
            for key in expected
        )
    if isinstance(expected, list):
        return len(value) == len(expected) and all(  # type: ignore[arg-type]
            strictly_equal(item, other)
            for item, other in zip(value, expected)  # type: ignore[arg-type]
        )
    return value == expected


def valid_preview_launch_config(value: object) -> bool:
    """Pin the preview launcher exactly.

    The host executes this command, so an unreviewed replacement would run
    outside the reviewed pre-site Python boundary. Whole-document equality keeps
    the release gate red for any edit, including a different interpreter, an
    added flag, or a bind address that is not the loopback interface. The UI is
    unauthenticated and exposes approved input roots and private artifact
    downloads, so a non-loopback address is a network exposure, not a
    convenience. `autoPort` stays false because Streamlit itself binds the
    port named in `runtimeArgs`.
    """
    expected = {
        "version": "0.0.1",
        "configurations": [
            {
                "name": "experiment-design-ui",
                "runtimeExecutable": "tools/run-reviewed-python.sh",
                "runtimeArgs": [
                    "-m", "streamlit", "run", "agent-harness/streamlit_app.py",
                    "--server.headless", "true",
                    "--server.address", "127.0.0.1",
                    "--server.port", "8501",
                ],
                "port": 8501,
                "autoPort": False,
            }
        ],
    }
    return strictly_equal(value, expected)


def valid_private_mcp_package(package: object, lock: object) -> bool:
    """Require both npm metadata and the publish blocker to fail closed."""
    if not isinstance(package, dict) or not isinstance(lock, dict):
        return False
    scripts = package.get("scripts")
    packages = lock.get("packages")
    lock_root = packages.get("") if isinstance(packages, dict) else None
    return (
        package.get("private") is True
        and isinstance(scripts, dict)
        and scripts.get("prepublishOnly") == "node scripts/block-publish.mjs"
        and scripts.get("test:private") == "node tests/private-package.mjs"
        and scripts.get("test:public-lifecycle") == (
            "npm run build && npm run test:private && "
            "EXPDESIGN_RSCRIPT=/usr/bin/false "
            "node tests/lifecycle.mjs --public-only"
        )
        and scripts.get("test:lifecycle") == (
            "npm run build && npm run test:private && node tests/lifecycle.mjs"
        )
        and isinstance(lock_root, dict)
        and lock_root.get("private") is True
        and lock_root.get("name") == package.get("name")
        and lock_root.get("version") == package.get("version")
    )


def valid_r_package_integrity_manifest(value: object) -> bool:
    """Validate the closed schema for reviewed, platform-specific R tree hashes."""
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "proof_boundary", "environments",
    }:
        return False
    environments = value.get("environments")
    if (
        value.get("schema_version") != 1
        or value.get("proof_boundary") != R_INTEGRITY_PROOF_BOUNDARY
        or not isinstance(environments, list)
        or not 1 <= len(environments) <= 32
    ):
        return False
    identities: set[tuple[str, str, str, frozenset[str]]] = set()
    for environment in environments:
        if not isinstance(environment, dict) or set(environment) != {
            "r_version", "r_platform", "r_os", "profile",
            "renv_lock_sha256", "packages",
        }:
            return False
        metadata = [
            environment.get("r_version"), environment.get("r_platform"),
            environment.get("r_os"), environment.get("profile"),
        ]
        packages = environment.get("packages")
        if (
            not all(isinstance(item, str) and item for item in metadata)
            or not isinstance(environment.get("renv_lock_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", environment["renv_lock_sha256"]) is None
            or not isinstance(packages, dict)
            or not packages
        ):
            return False
        identity = (
            str(metadata[0]), str(metadata[1]), str(metadata[2]), frozenset(packages),
        )
        if identity in identities:
            return False
        identities.add(identity)
        for name, record in packages.items():
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9.]*", name) is None
                or not isinstance(record, dict)
                or set(record) != {
                    "version", "file_count", "total_bytes", "tree_sha256",
                }
                or not isinstance(record.get("version"), str)
                or not record["version"]
                or not isinstance(record.get("file_count"), int)
                or isinstance(record.get("file_count"), bool)
                or record["file_count"] <= 0
                or not isinstance(record.get("total_bytes"), int)
                or isinstance(record.get("total_bytes"), bool)
                or record["total_bytes"] < 0
                or not isinstance(record.get("tree_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", record["tree_sha256"]) is None
            ):
                return False
    return True


def valid_python_requirements_lock(lock_text: str) -> bool:
    """Require each continued pin to be followed immediately by its hashes."""
    pins: dict[str, str] = {}
    lines = lock_text.splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        pin = LOCK_PIN_RE.fullmatch(raw_line)
        if pin is None:
            # Reject plain/uncontinued pins, orphaned hashes, options, and any
            # other syntax that this deterministic release parser cannot bind.
            return False
        package = re.sub(r"[-_.]+", "-", pin.group(1)).lower()
        version = pin.group(2)
        if package in pins:
            return False
        pins[package] = version
        index += 1
        entry_hashes: set[str] = set()
        while index < len(lines):
            digest = LOCK_HASH_RE.fullmatch(lines[index])
            if digest is None:
                return False
            entry_hashes.add(digest.group(1))
            index += 1
            continued = digest.group(2) == "\\"
            if not continued:
                break
            if index >= len(lines):
                return False
        if not entry_hashes:
            return False
    return bool(pins)


def valid_hook_launcher(value: object, mode: str, agent_scope: str) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        value.get("type") == "command"
        and value.get("command") == "/bin/sh"
        and value.get("args") == [
            "${CLAUDE_PROJECT_DIR}/hooks/launch_verification.sh",
            mode,
            agent_scope,
        ]
        and isinstance(value.get("timeout"), int)
        and value["timeout"] >= 10
    )


def valid_agent_hooks(value: object, agent_scope: str, *, coordinator: bool = False) -> bool:
    """Require the exact scoped prompt/dispatch and verification hook set."""
    expected_modes = {"PostToolBatch": "record", "Stop": "enforce"}
    if coordinator:
        expected_modes = {
            "UserPromptSubmit": "capture", "PreToolUse": "bind", **expected_modes,
        }
    if not isinstance(value, dict) or set(value) != set(expected_modes):
        return False
    for event, mode in expected_modes.items():
        entries = value.get(event)
        if not isinstance(entries, list) or len(entries) != 1:
            return False
        entry = entries[0]
        expected_entry_fields = {"matcher", "hooks"} if event == "PreToolUse" else {"hooks"}
        if not isinstance(entry, dict) or set(entry) != expected_entry_fields:
            return False
        if event == "PreToolUse" and entry.get("matcher") != "Agent":
            return False
        commands = entry.get("hooks")
        if not isinstance(commands, list) or len(commands) != 1:
            return False
        if not valid_hook_launcher(commands[0], mode, agent_scope):
            return False
    return True


def valid_bootstrap_python_lock(bootstrap: str) -> bool:
    """Require exact installed-environment validation before any Node selection."""
    required_fragments = {
        '"$HARNESS_PYTHON" -E -s -S -B -I -c',
        "tools/run-reviewed-python.sh --validate-only",
        '"$HARNESS_PYTHON" -E -s -S -B -I',
        "tools/sanitize_python_environment.py",
        "pip install --no-compile --require-hashes",
        "EXPDESIGN_PYTHON must be an absolute path",
        "EXPDESIGN_NODE must be an absolute path",
        "--check-python-lock",
        "Python requirements.lock check passed",
        "tools/run-reviewed-python.sh tools/validate_r_environment.py",
    }
    lock_branch = bootstrap.find('if [[ "$PYTHON_LOCK_CHECK_ONLY" == true ]]')
    node_selection = bootstrap.find("\nselect_reviewed_node\n", lock_branch)
    return (
        all(fragment in bootstrap for fragment in required_fragments)
        and lock_branch >= 0
        and node_selection > lock_branch
    )


def valid_reviewed_python_runner(shell_launcher: str, runner: str) -> bool:
    """Require a pre-site validation boundary for every application Python run."""
    shell_fragments = {
        'exec "$python_bin" -E -s -S -B -I',
        '"$script_dir/reviewed_python_runner.py" "$@"',
        "EXPDESIGN_PYTHON must be an absolute path",
        "[Pp][Yy][Tt][Hh][Oo][Nn]",
        "[Ll][Dd]_[A-Za-z0-9_]*",
        "[Dd][Yy][Ll][Dd]_[A-Za-z0-9_]*",
    }
    runner_fragments = {
        "_reject_source_bytecode(SUITE_ROOT, environment_prefix)",
        "validator = _load_validator()",
        "distribution_paths=package_paths",
        "environment_prefix=environment_prefix",
        'if arguments == ["--validate-only"]',
        "runpy.run_module(module_name, run_name=\"__main__\", alter_sys=True)",
        "runpy.run_path(str(script), run_name=\"__main__\")",
        'BYTECODE_SUFFIXES = {".pyc", ".pyo"}',
        'if name == "__pycache__"',
        "symlinked project source entry is forbidden",
    }
    scan = runner.find("_reject_source_bytecode(SUITE_ROOT, environment_prefix)")
    load = runner.find("validator = _load_validator()", scan)
    execute = runner.find("_execute(", load)
    return (
        all(fragment in shell_launcher for fragment in shell_fragments)
        and all(fragment in runner for fragment in runner_fragments)
        and 0 <= scan < load < execute
    )


def valid_claude_check_semantics(bootstrap: str) -> bool:
    """Keep the installed-version gate separate from optional live authentication."""
    required_fragments = {
        "--check-claude-version",
        "--check-claude-live",
        "check_claude_version()",
        "check_claude_live()",
        '"$REVIEWED_CLAUDE" auth status',
        "authentication was not required",
        r"\(Claude Code\)",
        "unrecognized Claude Code product/version output",
        "EXPDESIGN_CLAUDE must be an absolute path",
        "select_reviewed_claude()",
    }
    return (
        all(fragment in bootstrap for fragment in required_fragments)
        and "--check-claude-runtime" not in bootstrap
    )


def valid_release_entrypoints(makefile: str) -> bool:
    """Keep both release gates and the governed coordinator suites mandatory."""
    structural = all(re.search(pattern, makefile, flags=re.MULTILINE) for pattern in (
        r"^check-claude-version:\s*$",
        r"^\s*@tools/bootstrap\.sh --check-claude-version\s*$",
        r"^check-claude-live:\s*$",
        r"^\s*@tools/bootstrap\.sh --check-claude-live\s*$",
        r"^validate-python-lock:\s*$",
        r"^\s*@\$\(PYTHON_RUN\) --validate-only\s*$",
        r"^harness-release-check:\s*$",
        r"^release-check:\s*$",
        r"^public-check:\s*$",
        r'^\s*@tools/run-publication-python\.sh "\$\(CURDIR\)/tools/'
        r'validate_public_distribution\.py" --public-clone "\$\(CURDIR\)"\s*$',
        r"^\s*@cd mcp-server && npm run test:public-lifecycle\s*$",
        r"^\s*@\$\(MAKE\) check-claude-version\s*$",
        r"^\s*@\$\(MAKE\) validate-python-lock\s*$",
        r"^\s*@\$\(MAKE\) harness-release-check\s*$",
        r"^PYTHON \?= .+$",
        r'^PYTHON_RUN = EXPDESIGN_PYTHON="\$\(PYTHON\)" tools/run-reviewed-python\.sh$',
        r"^\s*@\$\(PYTHON_RUN\) tools/validate_r_environment\.py\s*$",
        r"^\s*@\$\(PYTHON_RUN\) agent-harness/tests/test_multi_agent\.py\s*$",
        r"^\s*@\$\(PYTHON_RUN\) agent-harness/tests/test_streamlit_surface\.py\s*$",
        r"^\s*@\$\(PYTHON_RUN\) hooks/tests/test_coordinator_verification\.py\s*$",
        r"^\s*@\$\(PYTHON_RUN\) tools/tests/test_validate_python_environment\.py\s*$",
        r"^\s*@\$\(PYTHON_RUN\) tools/tests/test_reviewed_python_runner\.py\s*$",
        r"^\s*@\$\(PYTHON_RUN\) tools/tests/test_sanitize_python_environment\.py\s*$",
    ))
    release = re.search(
        r"^release-check:\s*\n(?P<body>(?:^\t.*(?:\n|$))*)",
        makefile,
        flags=re.MULTILINE,
    )
    harness_release = re.search(
        r"^harness-release-check:\s*\n(?P<body>(?:^\t.*(?:\n|$))*)",
        makefile,
        flags=re.MULTILINE,
    )
    return (
        structural
        and release is not None
        and harness_release is not None
        and "check-claude-live" not in release.group("body")
        and "check-claude-live" not in harness_release.group("body")
        and all(
            "$(PYTHON_RUN)" in line
            or line.strip() == (
                '@tools/run-publication-python.sh '
                '"$(CURDIR)/tools/validate_public_distribution.py" '
                '--public-clone "$(CURDIR)"'
            )
            for line in makefile.splitlines()
            if line.startswith("\t") and ".py" in line
        )
    )


def valid_public_assurance_workflow(workflow: str) -> bool:
    """Keep public CI least-privilege, SHA-pinned, and engine-honest.

    This deliberately validates the executable YAML structure rather than
    searching source text.  Comments, alternate list syntax, inline mappings,
    quoted permission values, and job-level overrides therefore cannot satisfy
    or bypass the public assurance contract.
    """
    checkout = "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09"
    setup_node = "actions/setup-node@a0853c24544627f65ddf259abe73b1d18a591444"
    setup_python = "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"

    checkout_step = {
        "uses": checkout,
        "with": {"persist-credentials": False},
    }
    setup_node_step = {
        "uses": setup_node,
        "with": {
            "node-version": "20",
            "cache": "npm",
            "cache-dependency-path": "mcp-server/package-lock.json",
        },
    }
    expected_steps = {
        "public-distribution": [
            checkout_step,
            setup_node_step,
            {"run": "make public-check"},
        ],
        "node-boundaries": [
            checkout_step,
            setup_node_step,
            {
                "working-directory": "mcp-server",
                "run": "npm ci --no-audit --no-fund",
            },
            {
                "working-directory": "mcp-server",
                "env": {
                    "EXPDESIGN_PYTHON": "/usr/bin/python3",
                    "EXPDESIGN_RSCRIPT": "/usr/bin/false",
                },
                "run": "npm run test:public-lifecycle",
            },
        ],
        "python-contracts": [
            checkout_step,
            {
                "uses": setup_python,
                "with": {"python-version": "${{ matrix.python }}"},
            },
            {"run": "\n".join([
                '"$pythonLocation/bin/python" -I -E -s -S -B agent-harness/tests/test_gates.py',
                '"$pythonLocation/bin/python" -I -E -s -S -B agent-harness/tests/test_verification.py',
                '"$pythonLocation/bin/python" -I -E -s -S -B agent-harness/tests/test_audit.py',
                '"$pythonLocation/bin/python" -I -E -s -S -B agent-harness/tests/test_artifact_download.py',
                '"$pythonLocation/bin/python" -I -E -s -S -B agent-harness/tests/test_mcp_client.py --public-only',
            ])},
        ],
        "governance-hooks": [
            checkout_step,
            {
                "uses": setup_python,
                "with": {"python-version": "3.14"},
            },
            {"run": "\n".join([
                '"$pythonLocation/bin/python" -I -E -s -S -B hooks/tests/test_enforce_verification.py',
                '"$pythonLocation/bin/python" -I -E -s -S -B hooks/tests/test_coordinator_verification.py',
                '"$pythonLocation/bin/python" -I -E -s -S -B hooks/tests/test_verification_ledger.py',
            ])},
        ],
        "supply-chain": [
            {
                "uses": checkout,
                "with": {"persist-credentials": False, "fetch-depth": 0},
            },
            setup_node_step,
            {
                "working-directory": "mcp-server",
                "run": "npm audit --omit=dev --audit-level=high",
            },
            {
                "shell": "bash",
                "run": "\n".join([
                    "set -o pipefail",
                    "git rev-list --objects --all \\",
                    "  | awk '{print $1}' \\",
                    "  | git cat-file --batch \\",
                    '  | tools/run-publication-python.sh "$PWD/tools/check_diff_credentials.py"',
                ]),
            },
        ],
    }
    expected_job_options = {
        "public-distribution": {},
        "node-boundaries": {},
        "python-contracts": {
            "strategy": {
                "fail-fast": False,
                "matrix": {"python": ["3.9", "3.14"]},
            },
        },
        "governance-hooks": {},
        "supply-chain": {},
    }

    try:
        document = yaml.safe_load(workflow)
    except yaml.YAMLError:
        return False
    if not isinstance(document, dict):
        return False

    # PyYAML follows YAML 1.1 and resolves the unquoted GitHub key `on` to True.
    on_key: object = "on" if "on" in document else True
    if set(document) != {
        "name", on_key, "permissions", "concurrency", "jobs",
    }:
        return False
    if document.get("name") != "Public assurance":
        return False
    if document.get(on_key) != {
        "push": {"branches": ["main"]},
        "pull_request": None,
        "workflow_dispatch": None,
    }:
        return False
    if document.get("permissions") != {"contents": "read"}:
        return False
    if document.get("concurrency") != {
        "group": "public-assurance-${{ github.ref }}",
        "cancel-in-progress": True,
    }:
        return False

    jobs = document.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) != set(expected_steps):
        return False

    actions: list[str] = []
    for job_name, expected in expected_steps.items():
        job = jobs.get(job_name)
        if not isinstance(job, dict) or not isinstance(job.get("name"), str):
            return False
        actual_job = dict(job)
        actual_job.pop("name")
        steps = actual_job.pop("steps", None)
        if actual_job != {
            "runs-on": "ubuntu-24.04",
            "timeout-minutes": 15,
            **expected_job_options[job_name],
        }:
            return False
        if not isinstance(steps, list) or len(steps) != len(expected):
            return False
        normalized_steps: list[dict[str, object]] = []
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("name"), str):
                return False
            normalized = dict(step)
            normalized.pop("name")
            if "run" in normalized:
                if not isinstance(normalized["run"], str):
                    return False
                normalized["run"] = normalized["run"].strip()
            if "uses" in normalized:
                if not isinstance(normalized["uses"], str):
                    return False
                actions.append(normalized["uses"])
            normalized_steps.append(normalized)
        if normalized_steps != expected:
            return False

    approved_actions = {checkout, setup_node, setup_python}
    return (
        bool(actions)
        and set(actions) == approved_actions
        and all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in actions)
        and "secrets." not in workflow
    )


def valid_publish_sync_guards(
    sync_script: str,
    publication_runner: str,
    public_validator: str,
) -> bool:
    """Validate the actual fail-closed public publication architecture."""
    sync_required = {
        '[ "$#" -eq 1 ] || usage',
        "--dry-run) DRY=1 ;;",
        "--publish-reviewed) DRY=0 ;;",
        "EXPECTED_REPO=Veronica0206/experiment-design-agent",
        "EXPECTED_CLONE_URL=https://github.com/Veronica0206/experiment-design-agent.git",
        "MKTEMP_BIN=/usr/bin/mktemp",
        "TEMP_ROOT=$($MKTEMP_BIN -d /tmp/experiment-design-publication.XXXXXX)",
        "GH_ISOLATED_CONFIG=$TEMP_ROOT/gh-config",
        'GH_CONFIG_DIR="$GH_ISOLATED_CONFIG" GH_TOKEN="$publication_token"',
        '"$GH_BIN" auth token --hostname github.com',
        'OWNER_HOME=$("$PYTHON_RUN" "$PUBLIC_GUARD" --owner-home)',
        'validate_tree working "$CLONE"',
        'validate_tree staged "$CLONE"',
        'validate_tree committed "$CLONE" "$commit_sha"',
        '"$PYTHON_RUN" "$PUBLISH_GUARD" "$tree"',
        '"$PYTHON_RUN" "$PUBLIC_GUARD" "$tree"',
        '"$PYTHON_RUN" "$PUBLISH_GUARD" --staged "$tree"',
        '"$PYTHON_RUN" "$PUBLIC_GUARD" --staged "$tree"',
        '"$PYTHON_RUN" "$PUBLISH_GUARD" --tree-ish "$object_id" "$tree"',
        '"$PYTHON_RUN" "$PUBLIC_GUARD" --tree-ish "$object_id" "$tree"',
        '"$PYTHON_RUN" "$PUBLIC_GUARD" --github-metadata "$metadata_dir"',
        'prepush_sha=$(fetch_destination_metadata "$TEMP_ROOT/prepush")',
        'published_sha=$(fetch_destination_metadata "$TEMP_ROOT/postpush")',
        "--require-public-commit-metadata",
        'run_git_commit -C "$CLONE" commit --quiet --message "$PUBLIC_COMMIT_MESSAGE"',
        '--force-with-lease="refs/heads/$EXPECTED_BRANCH:$remote_sha"',
        '"$commit_sha:refs/heads/$EXPECTED_BRANCH"',
    }
    runner_targets = {
        '"$script_dir/check_publish_source.py"',
        '"$script_dir/validate_public_distribution.py"',
        '"$script_dir/check_diff_credentials.py"',
    }
    validator_required = {
        'EXPECTED_NODE_ID = "R_kgDOTySG9w"',
        "EXPECTED_REST_ID = 1327793911",
        'EXPECTED_FULL_NAME = "Veronica0206/experiment-design-agent"',
        'EXPECTED_BRANCH = "main"',
        '"visibility": "public"',
        'part.casefold().startswith("vera-")',
        'path.name.casefold().endswith(".skill.enc")',
        'mode.add_argument("--public-clone", type=Path)',
        'root, "status", "--porcelain=v1", "--untracked-files=all"',
        '"--ignored=matching"',
        'PurePosixPath("mcp-server/node_modules")',
        'PurePosixPath("mcp-server/dist")',
        'root, "rev-parse", "--verify", "HEAD^{commit}"',
    }
    return (
        sync_script.startswith("#!/bin/sh\n")
        and publication_runner.startswith("#!/bin/sh\n")
        and all(fragment in sync_script for fragment in sync_required)
        and sync_script.count('validate_tree working "$PUBLISH_SRC"') >= 2
        and sync_script.count("EXPDESIGN_REPO") == 1
        and sync_script.count("EXPDESIGN_CLONE") == 1
        and "${EXPDESIGN_REPO:-" not in sync_script
        and "${EXPDESIGN_CLONE:-" not in sync_script
        and "-m)" not in sync_script
        and "--force " not in sync_script
        and "--force\n" not in sync_script
        and "/Users/" not in sync_script
        and all(target in publication_runner for target in runner_targets)
        and publication_runner.count('"$script_dir/') == 3
        and "/usr/bin/env -i" in publication_runner
        and 'EXPDESIGN_APPROVED_GIT="$git_bin"' in publication_runner
        and all(fragment in public_validator for fragment in validator_required)
        and "/Users/" not in public_validator
    )


def fail(message: str, failures: list[str]) -> None:
    failures.append(message)
    print(f"FAIL: {message}")


def main() -> int:
    failures: list[str] = []
    registry: object = None
    try:
        registry = load_registry(REGISTRY_PATH)
        registry_agents = {item["name"]: item for item in registry["agents"]}
    except (RegistryError, OSError, ValueError) as exc:
        fail(f"invalid governance/agents.json: {exc}", failures)
        registry_agents = {}
    expected_agent_names = {f"{name}.md" for name in registry_agents}
    for relative in sorted(REQUIRED_RUNTIME_FILES):
        path = ROOT / relative
        if not is_regular_runtime_file(path):
            fail(f"required runtime file must be a regular file: {relative}", failures)
    try:
        launcher_policy = load_launcher_domain_tool_policy()
        if not valid_domain_tool_policy(registry, launcher_policy):
            fail(
                "domain-tool grants differ across governance/agents.json, "
                "governance/registry.py, and the fixed hook-launcher policy",
                failures,
            )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        fail(f"hook domain-tool policy could not be validated: {exc}", failures)
    discovered_skills = {path.parent.name for path in ROOT.glob("vera-*/SKILL.md")}
    missing_skills, unexpected_skills = inventory_delta(discovered_skills, EXPECTED_SKILL_NAMES)
    if missing_skills or unexpected_skills:
        fail(f"skill inventory mismatch; missing={missing_skills}, unexpected={unexpected_skills}", failures)
    skills = [ROOT / name for name in sorted(EXPECTED_SKILL_NAMES)]

    agent_root = ROOT / ".claude" / "agents"
    discovered_agents = {path.name for path in agent_root.glob("*.md")}
    missing_agents, unexpected_agents = inventory_delta(discovered_agents, expected_agent_names)
    if missing_agents or unexpected_agents:
        fail(f"Claude agent inventory mismatch; missing={missing_agents}, unexpected={unexpected_agents}", failures)
    claude_agents = [agent_root / name for name in sorted(expected_agent_names)]

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
        if policy.get("allow_implicit_invocation") is not False:
            fail(
                f"{skill.name}: allow_implicit_invocation must be false for explicit, "
                "collision-free routing",
                failures,
            )

    try:
        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        if not valid_mcp_config(mcp):
            fail(".mcp.json must contain only the approved experiment-design Node server", failures)
    except Exception as exc:
        fail(f"invalid .mcp.json: {exc}", failures)

    try:
        launch = json.loads(
            (ROOT / ".claude" / "launch.json").read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
        if not valid_preview_launch_config(launch):
            fail(
                ".claude/launch.json must launch the reviewed Python boundary on the "
                "loopback interface with the pinned port and no automatic reassignment",
                failures,
            )
    except Exception as exc:
        fail(f"invalid .claude/launch.json: {exc}", failures)

    try:
        mcp_package = json.loads(
            (ROOT / "mcp-server" / "package.json").read_text(encoding="utf-8")
        )
        mcp_lock = json.loads(
            (ROOT / "mcp-server" / "package-lock.json").read_text(encoding="utf-8")
        )
        if not valid_private_mcp_package(mcp_package, mcp_lock):
            fail(
                "mcp-server must be private in package and lock metadata, retain the "
                "local publish blocker, and run its no-registry publication test",
                failures,
            )
    except Exception as exc:
        fail(f"invalid private mcp-server package metadata: {exc}", failures)

    try:
        r_integrity = json.loads(
            (ROOT / "governance" / "r-package-integrity.json").read_text(
                encoding="utf-8"
            )
        )
        if not valid_r_package_integrity_manifest(r_integrity):
            fail(
                "governance/r-package-integrity.json must contain exact-platform "
                "installed package-tree SHA-256 records and the explicit archive "
                "provenance boundary",
                failures,
            )
    except Exception as exc:
        fail(f"invalid R package integrity manifest: {exc}", failures)

    try:
        python_lock = (ROOT / "agent-harness" / "requirements.lock").read_text(
            encoding="utf-8"
        )
        if not valid_python_requirements_lock(python_lock):
            fail(
                "agent-harness/requirements.lock must give every resolved package "
                "at least one valid SHA-256 hash",
                failures,
            )
    except Exception as exc:
        fail(f"invalid Python requirements lock: {exc}", failures)

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
        if not valid_claude_check_semantics(bootstrap):
            fail(
                "tools/bootstrap.sh must separate the installed Claude version "
                "check from the optional authenticated-session check",
                failures,
            )
        if not valid_bootstrap_python_lock(bootstrap):
            fail(
                "tools/bootstrap.sh must compare every resolved requirements.lock "
                "package version to the selected Python environment",
                failures,
            )
        python_launcher = (ROOT / "tools" / "run-reviewed-python.sh").read_text(
            encoding="utf-8"
        )
        python_runner = (ROOT / "tools" / "reviewed_python_runner.py").read_text(
            encoding="utf-8"
        )
        if not valid_reviewed_python_runner(python_launcher, python_runner):
            fail(
                "application Python must start pre-site, reject executable caches, "
                "validate the exact selected venv, then add only reviewed paths",
                failures,
            )
    except Exception as exc:
        fail(f"invalid Claude Code bootstrap requirement: {exc}", failures)

    try:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        if not valid_release_entrypoints(makefile):
            fail(
                "Makefile must enforce the full Python lock and installed Claude "
                "Code version while keeping live authentication optional",
                failures,
            )
    except Exception as exc:
        fail(f"invalid release entrypoints: {exc}", failures)

    try:
        public_workflow = (ROOT / ".github" / "workflows" /
                           "public-assurance.yml").read_text(encoding="utf-8")
        if not valid_public_assurance_workflow(public_workflow):
            fail(
                "public assurance workflow must remain read-only, SHA-pinned, "
                "public-safe, and explicit about its missing-engine boundary",
                failures,
            )
    except Exception as exc:
        fail(f"invalid public assurance workflow: {exc}", failures)

    try:
        sync_script = (ROOT / "tools" / "sync-to-github.sh").read_text(encoding="utf-8")
        publication_runner = (ROOT / "tools" / "run-publication-python.sh").read_text(
            encoding="utf-8"
        )
        public_validator = (ROOT / "tools" / "validate_public_distribution.py").read_text(
            encoding="utf-8"
        )
        if not valid_publish_sync_guards(
            sync_script, publication_runner, public_validator,
        ):
            fail(
                "public sync must require explicit review, use an ephemeral clone, "
                "pin repository identity, apply all public-tree gates, and push "
                "the exact fixed-metadata commit under an exact-SHA lease",
                failures,
            )
    except Exception as exc:
        fail(f"invalid publish sync guards: {exc}", failures)

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
        registry_agent = registry_agents.get(scope)
        if registry_agent is None:
            fail(f"{path.name}: agent is absent from governance registry", failures)
            continue
        if agent.get("name") != scope:
            fail(f"{path.name}: frontmatter name must be {scope!r}", failures)
        if not valid_agent_hooks(
            agent.get("hooks"), scope,
            coordinator=registry_agent["role"] == "coordinator",
        ):
            fail(
                f"{path.name}: hooks must contain the exact scoped prompt capture, "
                "dispatch binding, recorder, and stop enforcement required by its role",
                failures,
            )
        if not valid_agent_tools(
            agent.get("tools", ""), registry_agent["tools"],
            require_yaml_list=registry_agent["role"] == "coordinator",
        ):
            fail(
                f"{path.name}: tools must exactly match the governance registry",
                failures,
            )

    if failures:
        print(f"\n{len(failures)} manifest/config validation failure(s)")
        return 1
    print(f"Validated {len(skills)} skill manifests and project runtime configuration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
