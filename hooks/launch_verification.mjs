#!/usr/bin/env node
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const mode = process.argv[2];
const agentScope = process.argv[3];
const scripts = {
  record: "record_verification.py",
  enforce: "enforce_verification.py",
};

function main() {
  try {
    const script = scripts[mode];
    if (!script) {
      process.stderr.write("INTERNAL_ERROR. Unknown verification hook mode.");
      return 2;
    }
    if (!new Set(["experiment-designer", "design-verifier"]).has(agentScope)) {
      process.stderr.write("INTERNAL_ERROR. A governed agent scope is required.");
      return 2;
    }
    const data = JSON.parse(readFileSync(0, "utf8"));
    if (!data || typeof data !== "object" || Array.isArray(data)) {
      process.stderr.write("INTERNAL_ERROR. Verification hook input must be a JSON object.");
      return 2;
    }
    if (data.agent_type && data.agent_type !== agentScope) {
      process.stderr.write("INTERNAL_ERROR. Verification hook agent scope mismatch.");
      return 2;
    }
    data._expdesign_agent_scope = agentScope;
    const input = JSON.stringify(data);
    const path = join(dirname(fileURLToPath(import.meta.url)), script);
    for (const executable of ["python3", "python"]) {
      const result = spawnSync(executable, [path], { input, encoding: "utf8" });
      if (result.error && result.error.code === "ENOENT") continue;
      if (result.stdout) process.stdout.write(result.stdout);
      if (result.stderr) process.stderr.write(result.stderr);
      return result.status === 0 ? 0 : 2;
    }
    process.stderr.write("INTERNAL_ERROR. No Python interpreter is available for verification.");
    return 2;
  } catch {
    process.stderr.write("INTERNAL_ERROR. Verification hook launcher failed.");
    return 2;
  }
}

process.exit(main());
