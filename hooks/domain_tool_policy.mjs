/** Fixed least-privilege authorization policy shared by the hook runtime and validator. */
import { loadRuntimeProfile } from "../governance/runtime_profile.mjs";

const completeDomainTools = Object.freeze({
  single_endpoint: Object.freeze(["validate_config", "sample_size", "simulate_design", "run_tests"]),
  master_protocol: Object.freeze(["master_simulate", "run_tests"]),
  doe: Object.freeze(["ab_test", "factorial_design", "rsm_design", "run_tests"]),
  randomization: Object.freeze(["randomize", "run_tests"]),
  indirect_comparison: Object.freeze(["indirect_compare", "run_tests"]),
  meta_analysis: Object.freeze(["meta_analyze", "run_tests"]),
});

export function activeDomainTools() {
  return Object.freeze(Object.fromEntries(
    loadRuntimeProfile().domains.map((domain) => [domain, completeDomainTools[domain]]),
  ));
}

export function domainPolicyDocument() {
  const domainTools = activeDomainTools();
  return {
    schema_version: 1,
    domain_tools: Object.fromEntries(
      Object.entries(domainTools).map(([domain, tools]) => [domain, [...tools]]),
    ),
  };
}
