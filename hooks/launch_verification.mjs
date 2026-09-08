#!/usr/bin/env node
import { accessSync, constants, readFileSync, realpathSync, statSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, isAbsolute, join } from "node:path";
import { fileURLToPath } from "node:url";
import { activeDomainTools } from "./domain_tool_policy.mjs";

const mode = process.argv[2];
const agentScope = process.argv[3];
const scripts = {
  capture: "prompt_binding_hook.py",
  bind: "prompt_binding_hook.py",
  record: "record_verification.py",
  enforce: "enforce_verification.py",
};
const hookDirectory = dirname(fileURLToPath(import.meta.url));
const registryPath = join(hookDirectory, "..", "governance", "agents.json");
const mcpPrefix = "mcp__experiment-design__";
// This fixed policy is an authorization boundary, not configuration loaded from
// the mutable registry. validate-config invokes a separate inspector over the
// same frozen module and compares it with agents.json and registry.py.

class HookIdentityError extends Error {}

function registryAgents() {
  const domainTools = activeDomainTools();
  const registry = JSON.parse(readFileSync(registryPath, "utf8"));
  if (!registry || typeof registry !== "object" || Array.isArray(registry)
      || Object.keys(registry).sort().join(",") !== "agents,schema_version"
      || registry.schema_version !== 1 || !Array.isArray(registry.agents)
      || registry.agents.length === 0) {
    throw new Error("invalid agent registry");
  }
  const fields = ["allowed_children", "domain", "ledger_policy", "name", "role", "tools"];
  const names = new Set();
  let coordinators = 0;
  for (const agent of registry.agents) {
    if (!agent || typeof agent !== "object" || Array.isArray(agent)
        || Object.keys(agent).sort().join(",") !== fields.join(",")
        || typeof agent.name !== "string" || !/^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/.test(agent.name)
        || names.has(agent.name) || !Array.isArray(agent.tools) || agent.tools.length === 0
        || !agent.tools.every((item) => typeof item === "string" && item.length > 0)
        || new Set(agent.tools).size !== agent.tools.length
        || !Array.isArray(agent.allowed_children)
        || !agent.allowed_children.every((item) => typeof item === "string" && item.length > 0)
        || new Set(agent.allowed_children).size !== agent.allowed_children.length
        || !["legacy_executor", "reexecutor", "coordinator", "domain_executor"].includes(agent.role)
        || !["all", "verification", "coordination", ...Object.keys(domainTools)].includes(agent.domain)
        || !["mcp", "coordinator"].includes(agent.ledger_policy)) {
      throw new Error("invalid agent registry entry");
    }
    if (agent.role === "coordinator") {
      coordinators += 1;
      const tool = `Agent(${agent.allowed_children.join(", ")})`;
      if (agent.ledger_policy !== "coordinator" || agent.domain !== "coordination"
          || agent.allowed_children.length === 0 || agent.tools.length !== 1
          || agent.tools[0] !== tool) throw new Error("invalid coordinator registry entry");
    } else if (agent.ledger_policy !== "mcp" || agent.allowed_children.length !== 0
               || !agent.tools.every((tool) => tool === `${mcpPrefix}*`
                 || tool.startsWith(mcpPrefix))) {
      throw new Error("invalid governed MCP agent entry");
    }
    if (agent.role === "legacy_executor"
        && (agent.domain !== "all" || JSON.stringify(agent.tools) !== JSON.stringify([`${mcpPrefix}*`]))) {
      throw new Error("invalid legacy executor registry entry");
    }
    if (agent.role === "reexecutor"
        && (agent.domain !== "verification" || JSON.stringify(agent.tools) !== JSON.stringify([`${mcpPrefix}*`]))) {
      throw new Error("invalid reexecutor registry entry");
    }
    if (agent.role === "domain_executor") {
      const expected = (domainTools[agent.domain] ?? []).map((tool) => `${mcpPrefix}${tool}`);
      if (expected.length === 0 || JSON.stringify(agent.tools) !== JSON.stringify(expected)) {
        throw new Error("invalid domain executor tool grant");
      }
    }
    names.add(agent.name);
  }
  if (coordinators !== 1) throw new Error("registry must contain one coordinator");
  const domainAgents = registry.agents.filter((agent) => agent.role === "domain_executor");
  if (domainAgents.length !== Object.keys(domainTools).length
      || new Set(domainAgents.map((agent) => agent.domain)).size !== Object.keys(domainTools).length
      || registry.agents.filter((agent) => agent.role === "legacy_executor").length !== 1
      || registry.agents.filter((agent) => agent.role === "reexecutor").length !== 1) {
    throw new Error("registry role/domain roster is incomplete");
  }
  const roleByName = new Map(registry.agents.map((agent) => [agent.name, agent.role]));
  const domainNames = new Set(domainAgents.map((agent) => agent.name));
  for (const agent of registry.agents) {
    if (agent.allowed_children.some((child) => !names.has(child) || roleByName.get(child) !== "domain_executor")) {
      throw new Error("registry references an unknown child");
    }
    if (agent.role === "coordinator"
        && (agent.allowed_children.length !== domainNames.size
          || agent.allowed_children.some((child) => !domainNames.has(child)))) {
      throw new Error("coordinator must route all domain executors");
    }
  }
  return new Map(registry.agents.map((agent) => [agent.name, agent]));
}

function resolvePython() {
  const configured = process.env.EXPDESIGN_PYTHON;
  if (configured && !isAbsolute(configured)) {
    throw new Error("EXPDESIGN_PYTHON must be an absolute path");
  }
  const candidates = configured
    ? [configured]
    : ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"];
  for (const candidate of candidates) {
    try {
      accessSync(candidate, constants.X_OK);
      const canonical = realpathSync(candidate);
      if (isAbsolute(canonical) && statSync(canonical).isFile()) return canonical;
    } catch { /* try the next pinned absolute location */ }
  }
  throw new Error("no approved absolute Python interpreter is available");
}

function sanitizedEnvironment(python) {
  const allowed = [
    "EXPDESIGN_HOOK_LEDGER_DIR", "EXPDESIGN_HOOK_LOG",
    "EXPDESIGN_HOOK_LOG_REQUIRED", "LANG", "LC_ALL", "TMPDIR",
  ];
  const env = { PATH: `${dirname(python)}:/usr/bin:/bin` };
  for (const key of allowed) {
    if (typeof process.env[key] === "string") env[key] = process.env[key];
  }
  return env;
}

function validateHookIdentity(data, agent, agentScope) {
  const runtimeType = data.agent_type;
  if (runtimeType != null && (
    typeof runtimeType !== "string" || runtimeType.length === 0
    || runtimeType.includes("\0") || runtimeType !== agentScope
  )) {
    throw new HookIdentityError("verification hook agent scope mismatch");
  }

  const agentId = data.agent_id;
  if (agentId != null && (
    typeof agentId !== "string" || agentId.length === 0 || agentId.includes("\0")
  )) {
    throw new HookIdentityError(
      agentId && typeof agentId === "string" && agentId.includes("\0")
        ? "verification ledger key components must not contain NUL"
        : "agent_id must be a non-empty NUL-free string",
    );
  }

  const event = data.hook_event_name;
  if (agent.role === "coordinator") {
    if (event === "SubagentStop" || agentId != null) {
      throw new HookIdentityError("the governed coordinator must run as the main agent");
    }
    return;
  }
  if (event === "SubagentStop" && agentId == null) {
    throw new HookIdentityError("SubagentStop requires agent_id");
  }
  if (["UserPromptSubmit", "PreToolUse", "Stop"].includes(event) && agentId != null) {
    throw new HookIdentityError("main-agent hook events must not carry agent_id");
  }
}

function main() {
  try {
    const script = scripts[mode];
    if (!script) {
      process.stderr.write("INTERNAL_ERROR. Unknown verification hook mode.");
      return 2;
    }
    const registry = registryAgents();
    if (!registry.has(agentScope)) {
      process.stderr.write("INTERNAL_ERROR. A governed agent scope is required.");
      return 2;
    }
    const agent = registry.get(agentScope);
    if (["capture", "bind"].includes(mode) && agent.role !== "coordinator") {
      process.stderr.write("INTERNAL_ERROR. Prompt binding requires the governed coordinator.");
      return 2;
    }
    const data = JSON.parse(readFileSync(0, "utf8"));
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      process.stderr.write("INTERNAL_ERROR. Verification hook input must be a JSON object.");
      return 2;
    }
    const expectedEvents = {
      capture: "UserPromptSubmit", bind: "PreToolUse",
      record: "PostToolBatch", enforce: ["Stop", "SubagentStop"],
    };
    const eventContract = expectedEvents[mode];
    if ((typeof eventContract === "string" && data.hook_event_name !== eventContract)
        || (Array.isArray(eventContract) && !eventContract.includes(data.hook_event_name))) {
      process.stderr.write("INTERNAL_ERROR. Verification hook event does not match its mode.");
      return 2;
    }
    validateHookIdentity(data, agent, agentScope);
    if (agent.role === "coordinator" && ["capture", "bind"].includes(mode)
        && process.env.CLAUDE_CODE_FORK_SUBAGENT !== "0") {
      process.stderr.write("INTERNAL_ERROR. Coordinator requires CLAUDE_CODE_FORK_SUBAGENT=0 before Claude starts; restart with the project's foreground settings.");
      return 2;
    }
    // Claude 2.1.233 removes run_in_background from Agent's schema when this
    // boolean is true, even with fork mode off. Match its trimmed bool parser.
    if (agent.role === "coordinator" && ["capture", "bind"].includes(mode)
        && ["1", "true", "yes", "on"].includes(
          (process.env.CLAUDE_CODE_DISABLE_BACKGROUND_TASKS ?? "").trim().toLowerCase(),
        )) {
      process.stderr.write("INTERNAL_ERROR. Coordinator's explicit foreground contract is incompatible with CLAUDE_CODE_DISABLE_BACKGROUND_TASKS; unset that preference or start another session after setting it false.");
      return 2;
    }
    data._expdesign_agent_scope = agentScope;
    const input = JSON.stringify(data);
    const path = join(hookDirectory, script);
    const executable = resolvePython();
    const result = spawnSync(executable, ["-E", "-s", "-S", "-B", path], {
      input, encoding: "utf8", env: sanitizedEnvironment(executable),
    });
    if (result.stdout) process.stdout.write(result.stdout);
    if (result.stderr) process.stderr.write(result.stderr);
    if (result.error) process.stderr.write("INTERNAL_ERROR. Verification Python could not start.");
    return result.status === 0 ? 0 : 2;
  } catch (error) {
    if (error instanceof HookIdentityError) {
      process.stderr.write(`INTERNAL_ERROR. ${error.message}`);
    } else {
      process.stderr.write("INTERNAL_ERROR. Verification hook launcher failed.");
    }
    return 2;
  }
}

process.exit(main());
