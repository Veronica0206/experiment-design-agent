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
  const started = performance.now();
  const response = await client.callTool({ name, arguments: args }, undefined, { timeout: 180_000 });
  assert.notEqual(response.isError, true, JSON.stringify(response));
  const content = response.content.find((item) => item.type === "text");
  assert.ok(content);
  console.log(`TIMING ${name} ${(performance.now() - started).toFixed(0)} ms (includes verification and any replay)`);
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

  const anchoredConfig = { ...config, null_param: 0.21, alt_param: 0.29, alphas: [0.025] };
  const anchored = await call("simulate_design", { config: anchoredConfig, seed: 31415, B_oc: 32 });
  check("public_non_aligned_binary_grid_is_verified", verified(anchored)
    && anchored._verification.checks.output_contract === true);
  check("public_non_aligned_binary_grid_preserves_exact_anchors", [0.21, 0.29].every(
    value => anchored.oc.some(row => row.true_param === value)));
  check("public_exact_binomial_counterexample_retains_achieved_power", anchored.sample_size[0].n_total === 225
    && anchored.sample_size[0].power_achieved >= 0.8);
  check("public_oc_uncertainty_survives_projection", anchored.result_contract_version === 1
    && anchored.oc.every(row => row.mc_replicates === 32 && typeof row.mc_precision_ok === "boolean"
      && Number.isFinite(row.p_go_mcse) && Number.isFinite(row.p_go_mc_lower)
      && Number.isFinite(row.p_go_mc_upper) && typeof row.inner_precision_ok === "boolean"));
  check("public_oc_operation_budget_is_explicit", anchored.workload.scenario_count === anchored.oc.length
    && anchored.workload.simulated_units === 225 * 32 * anchored.oc.length);

  const nearAnchors = await call("simulate_design", {
    config: { ...config, null_param: 0.15, alt_param: 0.35 }, seed: 31415, B_oc: 32,
  });
  check("public_near_duplicate_grid_preserves_verified_exact_anchors", verified(nearAnchors)
    && [0.15, 0.35].every(value => nearAnchors.oc.some(row => row.true_param === value))
    && nearAnchors._verification.checks.reproducibility === true);
  check("public_near_duplicate_grid_retains_design_assessment",
    nearAnchors._verification.report.includes("### Design-performance assessment")
    && !nearAnchors._verification.report.includes("Not assessed:"));

  const continuousConfig = { endpoint_type: "continuous", study_type: "confirmatory", design: "controlled",
    null_param: 0, alt_param: 0.4, sd: 1, alloc_ratio: 2, alphas: [0.025], powers: [0.8] };
  const continuousValidation = await call("validate_config", continuousConfig);
  check("public_continuous_prior_contract_is_explicit", continuousValidation.resolved_config.prior_method === "normal_jeffreys_joint"
    && continuousValidation.resolved_config.prior_params.kappa0 === 0
    && continuousValidation.resolved_config.prior_params.beta0 === 0);
  const continuousSizing = await call("sample_size", continuousConfig);
  check("public_current_continuous_method_label_survives", verified(continuousSizing)
    && continuousSizing.results.some(row => row.test === "two_sample_z_normal_approximation"
      && row.n_trt >= 2 && row.n_ctrl >= 2 && row.power_achieved >= row.power_target)
    && continuousSizing._verification.report.includes("two_sample_z_normal_approximation"));

  for (const endpoint of ["tte", "incidence_rate"]) {
    const endpointFields = endpoint === "tte"
      ? { accrual_time: 12, followup_time: 6, p2_data: { events: 3, person_time: 100 },
          p2_data_ctrl: { events: 0, person_time: 100 } }
      : { exposure_time: 1, p2_data: { count: 3, exposure: 100 },
          p2_data_ctrl: { count: 0, exposure: 100 } };
    const request = { endpoint_type: endpoint, study_type: "confirmatory", design: "controlled",
      null_param: 0.2, alt_param: 0.1, alphas: [0.025], powers: [0.8], p3_n: 100, ...endpointFields };
    const value = await call("simulate_design", { config: request, n_oc: { n_trt: 40, n_ctrl: 40 }, B_oc: 32, seed: 1701 });
    const ratio = endpoint === "tte" ? "hr" : "rr";
    check(`public_${endpoint}_ppos_uncertainty_and_method_survive`, verified(value)
      && typeof value.ppos.confirmatory_test === "string" && value.ppos.n_mc === 100000
      && Number.isFinite(value.ppos.ppos_mcse) && Number.isFinite(value.ppos.ppos_mc_lower)
      && typeof value.ppos.mc_precision_ok === "boolean");
    check(`public_${endpoint}_nonexistent_ratio_mean_is_disclosed`, value.ppos[`${ratio}_post_mean`] === null
      && value.ppos[`${ratio}_post_mean_status`] === "does_not_exist"
      && Number.isFinite(value.ppos[`${ratio}_post_median`])
      && value.ppos[`${ratio}_post_ci`].length === 2
      && value._verification.report.includes("does_not_exist"));
    if (endpoint === "incidence_rate") check("public_current_poisson_method_label_survives",
      value.sample_size.some(row => row.test === "poisson_rate_difference_wald")
      && value._verification.report.includes("poisson_rate_difference_wald"));
  }
} finally {
  await client.close();
}
