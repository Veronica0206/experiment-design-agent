"""Strict loader for the versioned agent governance registry.

The JSON document is the declarative source of agent names, grants, routing, and
ledger policies. ``DOMAIN_TOOLS`` is an independent fixed authorization boundary:
the loader rejects registry expansion, while validate-config also compares this
Python policy with the JS hook launcher's fixed policy. Consumers must use this
loader (or enforce the equivalent closed policy) and fail closed on mismatch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from governance.runtime_profile import RuntimeProfileError, load_runtime_profile


REGISTRY_PATH = Path(__file__).with_name("agents.json")
SCHEMA_VERSION = 1
ENTRY_FIELDS = {
    "name", "role", "domain", "tools", "allowed_children", "ledger_policy",
}
ROLES = {"legacy_executor", "reexecutor", "coordinator", "domain_executor"}
DOMAINS = {
    "all", "verification", "coordination", "single_endpoint",
    "master_protocol", "doe", "randomization", "indirect_comparison",
    "meta_analysis",
}
LEDGER_POLICIES = {"mcp", "coordinator"}
NAME_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
MCP_PREFIX = "mcp__experiment-design__"
DOMAIN_TOOLS = {
    "single_endpoint": ["validate_config", "sample_size", "simulate_design", "run_tests"],
    "master_protocol": ["master_simulate", "run_tests"],
    "doe": ["ab_test", "factorial_design", "rsm_design", "run_tests"],
    "randomization": ["randomize", "run_tests"],
    "indirect_comparison": ["indirect_compare", "run_tests"],
    "meta_analysis": ["meta_analyze", "run_tests"],
}
PLANNING_DOMAINS = frozenset({
    "single_endpoint", "master_protocol", "doe", "randomization",
})
EVIDENCE_DOMAINS = frozenset({"indirect_comparison", "meta_analysis"})


def domain_phase(domain: str) -> str:
    """Return the governed workflow phase for one registry domain."""
    if domain in PLANNING_DOMAINS:
        return "planning"
    if domain in EVIDENCE_DOMAINS:
        return "evidence"
    raise RegistryError(f"domain has no governed workflow phase: {domain}")


class RegistryError(ValueError):
    """Raised when governance data does not satisfy the closed schema."""


def _string_list(value: Any, field: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise RegistryError(f"{field} must be a list of non-empty strings")
    if not allow_empty and not value:
        raise RegistryError(f"{field} must not be empty")
    if len(value) != len(set(value)):
        raise RegistryError(f"{field} must not contain duplicates")
    return list(value)


def validate_registry(value: Any) -> dict[str, Any]:
    try:
        active_domains = set(load_runtime_profile()["domains"])
    except RuntimeProfileError as exc:
        raise RegistryError(f"invalid installed runtime profile: {exc}") from exc
    if (PLANNING_DOMAINS & EVIDENCE_DOMAINS
            or PLANNING_DOMAINS | EVIDENCE_DOMAINS != set(DOMAIN_TOOLS)):
        raise RegistryError("every domain executor must have exactly one workflow phase")
    if not isinstance(value, dict) or set(value) != {"schema_version", "agents"}:
        raise RegistryError("registry must contain exactly schema_version and agents")
    schema_version = value.get("schema_version")
    if (isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != SCHEMA_VERSION):
        raise RegistryError("unsupported agent registry schema version")
    agents = value.get("agents")
    if not isinstance(agents, list) or not agents:
        raise RegistryError("agents must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    names: list[str] = []
    coordinators = 0
    for index, raw in enumerate(agents):
        if not isinstance(raw, dict) or set(raw) != ENTRY_FIELDS:
            raise RegistryError(f"agents[{index}] has unsupported or missing fields")
        name = raw.get("name")
        role = raw.get("role")
        domain = raw.get("domain")
        policy = raw.get("ledger_policy")
        if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
            raise RegistryError(f"agents[{index}].name is invalid")
        if role not in ROLES or domain not in DOMAINS or policy not in LEDGER_POLICIES:
            raise RegistryError(f"agents[{index}] has an unsupported enum value")
        tools = _string_list(raw.get("tools"), f"agents[{index}].tools", allow_empty=False)
        children = _string_list(
            raw.get("allowed_children"), f"agents[{index}].allowed_children",
            allow_empty=True,
        )
        if role == "coordinator":
            coordinators += 1
            if domain != "coordination" or policy != "coordinator" or not children:
                raise RegistryError("coordinator role/domain/policy/children are inconsistent")
            expected = f"Agent({', '.join(children)})"
            if tools != [expected]:
                raise RegistryError("coordinator tool grant must exactly match allowed_children")
        else:
            if children or policy != "mcp":
                raise RegistryError("only the coordinator may route children")
            if not all(tool == f"{MCP_PREFIX}*" or tool.startswith(MCP_PREFIX) for tool in tools):
                raise RegistryError("MCP agents may only receive experiment-design tools")
            if role == "legacy_executor" and (domain != "all" or tools != [f"{MCP_PREFIX}*"]):
                raise RegistryError("legacy executor must retain the exact wildcard grant")
            if role == "reexecutor" and (domain != "verification" or tools != [f"{MCP_PREFIX}*"]):
                raise RegistryError("reexecutor must retain the exact wildcard grant")
            if role == "domain_executor":
                if domain not in active_domains:
                    raise RegistryError("domain executor is unavailable in the installed profile")
                expected_tools = [f"{MCP_PREFIX}{tool}" for tool in DOMAIN_TOOLS.get(domain, [])]
                if not expected_tools or tools != expected_tools:
                    raise RegistryError("domain executor tools must exactly match its domain")
        names.append(name)
        normalized.append({
            "name": name, "role": role, "domain": domain, "tools": tools,
            "allowed_children": children, "ledger_policy": policy,
        })

    if len(names) != len(set(names)):
        raise RegistryError("agent names must be unique")
    if coordinators != 1:
        raise RegistryError("registry must contain exactly one coordinator")
    domain_agents = [agent for agent in normalized if agent["role"] == "domain_executor"]
    if (len(domain_agents) != len(active_domains)
            or {agent["domain"] for agent in domain_agents} != active_domains):
        raise RegistryError("registry must contain exactly one executor for each domain")
    if sum(agent["role"] == "legacy_executor" for agent in normalized) != 1:
        raise RegistryError("registry must contain exactly one legacy executor")
    if sum(agent["role"] == "reexecutor" for agent in normalized) != 1:
        raise RegistryError("registry must contain exactly one reexecutor")
    known = set(names)
    for agent in normalized:
        unknown = set(agent["allowed_children"]) - known
        if unknown:
            raise RegistryError(f"{agent['name']} references unknown child agents")
        if any(
            next(item for item in normalized if item["name"] == child)["role"]
            != "domain_executor"
            for child in agent["allowed_children"]
        ):
            raise RegistryError("coordinator children must be domain executors")
        if agent["role"] == "coordinator":
            executors = {item["name"] for item in normalized if item["role"] == "domain_executor"}
            if set(agent["allowed_children"]) != executors:
                raise RegistryError("coordinator must route every and only domain executor")
    return {"schema_version": SCHEMA_VERSION, "agents": normalized}


def active_domain_tools() -> dict[str, list[str]]:
    """Project the independent fixed policy onto reviewed installed domains."""
    try:
        return {domain: list(DOMAIN_TOOLS[domain])
                for domain in load_runtime_profile()["domains"]}
    except RuntimeProfileError as exc:
        raise RegistryError(f"invalid installed runtime profile: {exc}") from exc


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        if not path.is_file() or path.is_symlink():
            raise RegistryError("agent registry must be a regular, non-symlink file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RegistryError(f"agent registry could not be loaded: {exc}") from exc
    return validate_registry(value)


def agent_map(path: Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    registry = load_registry(path)
    return {item["name"]: item for item in registry["agents"]}


def get_agent(name: str, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        return agent_map(path)[name]
    except KeyError as exc:
        raise RegistryError(f"unknown governed agent: {name}") from exc
