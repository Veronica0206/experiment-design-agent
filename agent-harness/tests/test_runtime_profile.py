#!/usr/bin/env python3
"""Exercise installed capability boundaries in a clean, engine-free fixture.

No protected skill files or statistical engines are copied. Fresh processes read
the public profile from their own repository, including real native hook entrypoints.
"""
from __future__ import annotations

import ast
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from governance.runtime_profile import RuntimeProfileError, load_runtime_profile, validate_runtime_profiles

passed = failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"TEST {name} : PASS")
    else:
        failed += 1
        print(f"TEST {name} : FAIL {detail}")


def rejects(call) -> bool:
    try:
        call()
    except (RuntimeProfileError, OSError):
        return True
    return False


catalog = json.loads((ROOT / "governance/runtime-profiles.json").read_text())
for profile, expected_count in (("complete", 5), ("single-endpoint", 1)):
    selected = {**catalog, "active": profile}
    check(f"profile_{profile}_exact_catalog", len(validate_runtime_profiles(selected)["skills"]) == expected_count)

mutations = {
    "unknown_profile": lambda doc: doc.update(active="custom"),
    "boolean_schema": lambda doc: doc.update(schema_version=True),
    "unexpected_field": lambda doc: doc.update(overrides={}),
    "removed_catalog": lambda doc: doc["profiles"].pop("complete"),
    "expanded_public_tool": lambda doc: doc["profiles"]["single-endpoint"]["tools"].append("meta_analyze"),
    "duplicate_public_tool": lambda doc: doc["profiles"]["single-endpoint"]["tools"].append("run_tests"),
    "reduced_regression_count": lambda doc: doc["profiles"]["single-endpoint"]["expected_regression_pass_counts"].update({"vera-experiment-designing": 1}),
    "boolean_regression_count": lambda doc: doc["profiles"]["single-endpoint"]["expected_regression_pass_counts"].update({"vera-experiment-designing": True}),
}
for name, mutate in mutations.items():
    document = copy.deepcopy(catalog)
    mutate(document)
    check(f"profile_rejects_{name}", rejects(lambda: validate_runtime_profiles(document)))

node = os.environ.get("EXPDESIGN_NODE") or shutil.which("node")
check("profile_node_runtime_available", bool(node))
rscript = os.environ.get("EXPDESIGN_RSCRIPT") or shutil.which("Rscript")
check("profile_r_runtime_available", bool(rscript))
with tempfile.TemporaryDirectory(prefix="expdesign-public-profile-") as directory:
    fixture = Path(directory)
    for folder in ("governance", "hooks", "agent-harness"):
        (fixture / folder).mkdir()
        for source in (ROOT / folder).iterdir():
            if source.is_file() and source.suffix in {".py", ".mjs"}:
                shutil.copyfile(source, fixture / folder / source.name)
    profile_path = fixture / "governance/runtime-profiles.json"
    shutil.copyfile(ROOT / "governance/single-endpoint-result-contract.json",
                    fixture / "governance/single-endpoint-result-contract.json")
    document = {**catalog, "active": "single-endpoint"}
    profile_path.write_text(json.dumps(document))

    def r_profile() -> subprocess.CompletedProcess:
        return subprocess.run([
            rscript, "--vanilla", "-e",
            'args <- commandArgs(TRUE); source(args[1]); p <- load_runtime_profile(args[2]); cat(p$name, length(p$skills), length(p$tools), sep="|")',
            str(ROOT / "mcp-server/r-wrapper/runtime-profile.R"), str(fixture),
        ], capture_output=True, text=True, timeout=30)

    if rscript:
        result = r_profile()
        check("r_profile_matches_python_and_javascript_capabilities", result.returncode == 0 and result.stdout == "single-endpoint|1|4", result.stderr)
    registry = json.loads((ROOT / "governance/agents.json").read_text())
    registry["agents"] = [agent for agent in registry["agents"]
                          if agent["role"] != "domain_executor" or agent["domain"] == "single_endpoint"]
    for agent in registry["agents"]:
        if agent["role"] == "coordinator":
            agent["allowed_children"] = ["single-endpoint-designer"]
            agent["tools"] = ["Agent(single-endpoint-designer)"]
    (fixture / "governance/agents.json").write_text(json.dumps(registry))
    check("public_loader_selects_single_endpoint", load_runtime_profile(profile_path)["name"] == "single-endpoint")
    check("missing_profile_rejected", rejects(lambda: load_runtime_profile(fixture / "absent.json")))
    link = fixture / "linked.json"
    link.symlink_to(profile_path)
    check("symlink_profile_rejected", rejects(lambda: load_runtime_profile(link)))
    alias = fixture / "aliased-suite"
    alias.mkdir()
    (alias / "governance").symlink_to(profile_path.parent, target_is_directory=True)
    check("symlink_governance_directory_rejected", rejects(lambda: load_runtime_profile(alias / "governance/runtime-profiles.json")))

    def python_check(source: str) -> subprocess.CompletedProcess:
        prefix = (
            "import sys\n"
            f"sys.path[:0] = {list(map(str, [fixture, fixture / 'agent-harness', fixture / 'hooks']))!r}\n"
        )
        return subprocess.run([sys.executable, "-E", "-s", "-B", "-c", prefix + source],
                              cwd=fixture, text=True, capture_output=True, timeout=30)

    source = r'''
from governance.runtime_profile import load_runtime_profile
from governance.registry import agent_map, get_agent, RegistryError
from gates import check_regression_tests
from verification_ledger import _tool_allowed
profile = load_runtime_profile()
agents = agent_map()
assert len(agents) == 4
assert get_agent("experiment-design-coordinator")["allowed_children"] == ["single-endpoint-designer"]
try:
    get_agent("meta-analysis-analyst")
except RegistryError:
    pass
else:
    raise AssertionError("unavailable specialist was offered")
for name in ("experiment-designer", "design-verifier", "single-endpoint-designer"):
    agent = get_agent(name)
    assert _tool_allowed(agent, "mcp__experiment-design__sample_size")
    assert not _tool_allowed(agent, "mcp__experiment-design__meta_analyze")
status = {"all_ok": True, "suite_count": 1, "passed_suite_count": 1, "failed_suite_count": 0,
          "checks": {"declared_all_ok": True, "complete_suite_set": True,
                     "suite_records_valid": True, "expected_check_count": True}}
assert check_regression_tests(status).passed
assert not check_regression_tests({**status, "suite_count": 5, "passed_suite_count": 5}).passed
'''
    result = python_check(source)
    check("public_registry_hook_grants_and_regression_attestation", result.returncode == 0, result.stderr)

    # Import the harness in a configured environment, but never contact a provider.
    result = python_check(r'''
from types import SimpleNamespace
from harness import ExperimentDesignHarness
from multi_agent_harness import _router_prompt, _router_tool, MultiAgentExperimentDesignHarness, RouteError
harness = ExperimentDesignHarness(anthropic_client=object())
assert harness._allowed_mcp_tools() == {"validate_config", "sample_size", "simulate_design", "run_tests"}
assert "meta_analyze" not in harness.system_prompt
coordinator = MultiAgentExperimentDesignHarness(anthropic_client=object())
assert coordinator.children == ["single-endpoint-designer"]
assert "master-protocol-designer" not in _router_prompt(coordinator.children)
assert _router_tool(coordinator.children)["input_schema"]["properties"]["agents"]["items"]["enum"] == coordinator.children
reply = SimpleNamespace(content=[SimpleNamespace(type="tool_use", name="route_experiment_design", id="one",
    input={"action": "dispatch", "agents": ["meta-analysis-analyst"], "clarification_fields": []})])
coordinator.client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kwargs: reply))
try:
    coordinator._route("Run an unavailable analysis")
except RouteError:
    pass
else:
    raise AssertionError("coordinator accepted an unavailable route")
assert not coordinator._executors
''')
    check("public_harness_and_router_offer_only_installed_capabilities", result.returncode == 0, result.stderr)

    tree = ast.parse((fixture / "agent-harness/streamlit_app.py").read_text())
    function = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "available_modes")
    namespace = {"load_runtime_profile": lambda: load_runtime_profile(profile_path), "CHAT_MODE": "Free-form (multi-agent chat)"}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<public-ui>", "exec"), namespace)
    check("public_ui_hides_unavailable_forms", namespace["available_modes"]() == ["Single-endpoint design", "Free-form (multi-agent chat)"])

    if node:
        inspection = 'import {pathToFileURL} from "node:url"; const {loadRuntimeProfile} = await import(pathToFileURL(process.argv[1])); const p = loadRuntimeProfile(process.argv[2]); if (!Object.isFrozen(p) || !Object.isFrozen(p.tools) || !Object.isFrozen(p.expected_regression_pass_counts)) process.exit(2);'
        result = subprocess.run([node, "--input-type=module", "-e", inspection,
                                 str(fixture / "governance/runtime_profile.mjs"), str(fixture)],
                                capture_output=True, text=True, timeout=15)
        check("javascript_profile_metadata_is_deeply_immutable", result.returncode == 0, result.stderr)
        result = subprocess.run([node, "--input-type=module", "-e", inspection,
                                 str(fixture / "governance/runtime_profile.mjs"), str(alias)],
                                capture_output=True, text=True, timeout=15)
        check("javascript_rejects_symlink_governance_directory", result.returncode != 0)
        result = subprocess.run([node, str(fixture / "hooks/describe_domain_policy.mjs")],
                                capture_output=True, text=True, timeout=15,
                                env={**os.environ, "EXPDESIGN_RUNTIME_PROFILE": "complete"})
        check("javascript_policy_matches_public_profile", result.returncode == 0 and json.loads(result.stdout)["domain_tools"] == {
            "single_endpoint": ["validate_config", "sample_size", "simulate_design", "run_tests"]}, result.stderr)
        payload = json.dumps({"hook_event_name": "PostToolBatch", "session_id": "public-profile", "prompt_id": "one", "tool_calls": []})
        environment = {**os.environ, "EXPDESIGN_PYTHON": sys.executable,
                       "EXPDESIGN_HOOK_LEDGER_DIR": str(fixture / "ledger")}
        for agent, expected in (("single-endpoint-designer", 0), ("meta-analysis-analyst", 2)):
            result = subprocess.run([node, str(fixture / "hooks/launch_verification.mjs"), "record", agent],
                                    input=payload, capture_output=True, text=True, env=environment, timeout=15)
            check(f"native_hook_profile_scope_{agent}", result.returncode == expected, result.stderr)
        # Expanding agents.json cannot expand either independent fixed policy.
        expanded_registry = copy.deepcopy(registry)
        expanded_registry["agents"].append({
            "name": "meta-analysis-analyst", "role": "domain_executor", "domain": "meta_analysis",
            "tools": ["mcp__experiment-design__meta_analyze", "mcp__experiment-design__run_tests"],
            "allowed_children": [], "ledger_policy": "mcp",
        })
        (fixture / "governance/agents.json").write_text(json.dumps(expanded_registry))
        result = python_check("from governance.registry import load_registry\nload_registry()")
        check("public_python_rejects_full_registry_expansion", result.returncode != 0)
        result = subprocess.run([node, str(fixture / "hooks/launch_verification.mjs"), "record", "single-endpoint-designer"],
                                input=payload, capture_output=True, text=True, env=environment, timeout=15)
        check("public_native_hook_rejects_full_registry_expansion", result.returncode == 2)

    result = python_check('import os\nos.environ["EXPDESIGN_RUNTIME_PROFILE"] = "complete"\nfrom governance.runtime_profile import load_runtime_profile\nassert load_runtime_profile()["name"] == "single-endpoint"')
    check("ambient_environment_cannot_expand_python_profile", result.returncode == 0, result.stderr)

    duplicate = json.dumps(document).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1)
    profile_path.write_text(duplicate)
    check("python_rejects_duplicate_profile_keys", rejects(lambda: load_runtime_profile(profile_path)))
    if node:
        result = subprocess.run([node, str(fixture / "hooks/describe_domain_policy.mjs")], capture_output=True, text=True, timeout=15)
        check("javascript_rejects_duplicate_profile_keys", result.returncode != 0)
    if rscript:
        result = r_profile()
        check("r_rejects_duplicate_profile_keys", result.returncode != 0)

    # Changing either the active tool catalog or expected test counts must fail
    # in every language, including the R dispatcher before it sources an engine.
    expanded = copy.deepcopy(document)
    expanded["profiles"]["single-endpoint"]["tools"].append("meta_analyze")
    profile_path.write_text(json.dumps(expanded))
    if rscript:
        result = r_profile()
        check("r_rejects_expanded_public_tool_catalog", result.returncode != 0)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
