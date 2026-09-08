/** Execute real, verified statistics from a prepared public installation. */
import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { loadRuntimeProfile } from "../dist/runtime-profile.js";

const mcpRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const suiteRoot = resolve(mcpRoot, "..");
const profile = loadRuntimeProfile(suiteRoot);
assert.equal(profile.name, "single-endpoint", "Run this test from the prepared public installation");
for (const excluded of ["vera-master-experiment-designing", "vera-doe-designing",
  "vera-indirect-comparing", "vera-meta-analyzing"]) {
  assert.equal(existsSync(join(suiteRoot, excluded)), false,
    "Public smoke must run without other statistical engines installed");
}

const transport = new StdioClientTransport({
  command: process.execPath,
  args: [join(mcpRoot, "dist", "index.js")],
  cwd: mcpRoot,
  env: Object.fromEntries(Object.entries(process.env).filter(([, value]) => value !== undefined)),
  stderr: "pipe",
});
let stderr = "";
transport.stderr?.on("data", (chunk) => { stderr += chunk.toString(); });
const client = new Client({ name: "public-statistics-smoke", version: "1" });
const check = (name, condition) => {
  assert.ok(condition, `${name}: ${stderr}`);
  console.log(`TEST ${name} : PASS`);
};
const call = async (name, args) => {
  const response = await client.callTool({ name, arguments: args }, undefined, { timeout: 180_000 });
  assert.notEqual(response.isError, true, JSON.stringify(response));
  const content = response.content.find((item) => item.type === "text");
  assert.ok(content);
  return JSON.parse(content.text);
};
const verified = (result) => result._verification?.presentable === true
  && result._verification?.checks?.regression_suite === true
  && typeof result._verification?.report === "string"
  && /^[0-9a-f]{64}$/.test(result._verification?.report_hash)
  && /^[0-9a-f]{64}$/.test(result._provenance?.engine_fingerprint);
const config = {
  endpoint_type: "binary", study_type: "poc", design: "single_arm",
  null_param: 0.3, alt_param: 0.5, alphas: [0.05], powers: [0.8],
};

try {
  await client.connect(transport);
  const listed = await client.listTools();
  assert.deepEqual(listed.tools.map(({ name }) => name).sort(), [...profile.tools].sort());
  check("public_mcp_advertises_only_four_installed_tools", true);
  const invalid = await client.callTool({ name: "master_simulate", arguments: {} });
  check("public_mcp_rejects_unavailable_tools", invalid.isError === true
    && JSON.parse(invalid.content[0].text).error.code === "invalid_request");

  const validated = await call("validate_config", config);
  check("public_mcp_validates_real_configuration", validated.endpoint_type === "binary"
    && validated.resolved_config?.null_param === 0.3 && validated.resolved_config?.alt_param === 0.5);
  const regression = await call("run_tests", {});
  check("public_mcp_attests_exact_public_regression_suite", regression.all_ok === true
    && regression.suite_count === 1 && regression.passed_suite_count === 1
    && regression.failed_suite_count === 0
    && Object.values(regression.checks).every((value) => value === true));

  const sizing = await call("sample_size", config);
  check("public_mcp_returns_verified_sample_size_report", verified(sizing));
  const rows = Array.isArray(sizing.results) ? sizing.results : [sizing.results];
  check("public_sample_size_has_valid_numerical_output", rows.length === 1
    && Number.isInteger(rows[0]?.n_total) && rows[0].n_total > 0
    && rows[0].power_achieved >= 0.8 && rows[0].power_achieved <= 1);

  const simulation = await call("simulate_design", { config, seed: 31415, B_oc: 32 });
  check("public_mcp_returns_verified_simulation_report", verified(simulation));
  check("public_simulation_passes_same_seed_replay", simulation._verification?.checks?.reproducibility === true);
  assert.equal(simulation.seed, 31415);
  assert.equal(simulation.b_used, 32);
  const operatingCharacteristics = Array.isArray(simulation.oc) ? simulation.oc : [simulation.oc];
  check("public_simulation_returns_operating_characteristics", operatingCharacteristics.length > 0
    && operatingCharacteristics.every((row) => row && typeof row === "object"));
  const serialized = JSON.stringify({ sizing, simulation });
  check("public_results_do_not_expose_installation_paths", !serialized.includes(suiteRoot)
    && !serialized.includes("/Users/") && !serialized.includes("/private/tmp/"));
} finally {
  await client.close();
}
