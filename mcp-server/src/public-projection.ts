type JsonRecord = Record<string, unknown>;


const ENDPOINT_TYPES = new Set(["binary", "continuous", "tte", "incidence_rate"]);
const STUDY_TYPES = new Set(["signal_detection", "poc", "confirmatory"]);
const DESIGNS = new Set(["single_arm", "controlled"]);
const ESTIMANDS = new Set([
  "response_probability", "risk_difference", "mean", "mean_difference",
  "hazard_rate", "hazard_ratio", "incidence_rate", "rate_difference",
]);
const DIRECTIONS = new Set(["greater", "less"]);
const SIDEDNESS = new Set(["one_sided"]);
const TTE_METHODS = new Set(["exponential"]);
const RATE_METHODS = new Set(["poisson"]);
const PRIOR_FIELDS = new Set([
  "a", "b", "mu0", "kappa0", "alpha0", "beta0", "shape", "rate",
]);

const REGRESSION_SUITES = [
  "vera-experiment-designing",
  "vera-master-experiment-designing",
  "vera-indirect-comparing",
  "vera-meta-analyzing",
  "vera-doe-designing",
] as const;

const EXPECTED_REGRESSION_PASS_COUNTS: Record<(typeof REGRESSION_SUITES)[number], number> = {
  "vera-experiment-designing": 31,
  "vera-master-experiment-designing": 53,
  "vera-indirect-comparing": 15,
  "vera-meta-analyzing": 14,
  "vera-doe-designing": 14,
};

export const REGRESSION_STATUS_CHECKS = [
  "declared_all_ok",
  "complete_suite_set",
  "suite_records_valid",
  "expected_check_count",
] as const;


function record(value: unknown): JsonRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonRecord : null;
}


function finiteNumber(value: unknown, field: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`validate_config returned an invalid ${field}`);
  }
  return value;
}


function finiteNumberArray(value: unknown, field: string): number[] {
  if (typeof value === "number" && Number.isFinite(value)) return [value];
  if (!Array.isArray(value) || value.length < 1 || value.length > 64 ||
      value.some((item) => typeof item !== "number" || !Number.isFinite(item))) {
    throw new Error(`validate_config returned an invalid ${field}`);
  }
  return value as number[];
}


function fixedEnum(value: unknown, allowed: Set<string>, field: string): string {
  if (typeof value !== "string" || !allowed.has(value)) {
    throw new Error(`validate_config returned an invalid ${field}`);
  }
  return value;
}


function nullableFiniteNumber(value: unknown, field: string): number | null {
  if (value === null) return null;
  return finiteNumber(value, field);
}


function nullableEnum(
  value: unknown, allowed: Set<string>, field: string,
): string | null {
  if (value === null) return null;
  return fixedEnum(value, allowed, field);
}


function fixedBoolean(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`validate_config returned an invalid ${field}`);
  }
  return value;
}


function finitePrior(value: unknown): JsonRecord {
  const source = record(value);
  if (!source || Object.keys(source).length < 2 || Object.keys(source).length > 4 ||
      Object.keys(source).some((key) => !PRIOR_FIELDS.has(key))) {
    throw new Error("validate_config returned invalid prior_params");
  }
  const projected: JsonRecord = {};
  for (const key of Object.keys(source).sort()) {
    projected[key] = finiteNumber(source[key], `prior_params.${key}`);
  }
  return projected;
}


/** Project the validator result to its exact, value-typed public DTO. */
export function publicValidatedConfig(value: unknown): JsonRecord {
  const source = record(value);
  if (!source || source.valid !== true) {
    throw new Error("validate_config did not return a valid configuration");
  }
  const resolvedSource = record(source.resolved_config);
  const simulationSource = record(source.simulation_defaults);
  if (!resolvedSource || !simulationSource) {
    throw new Error("validate_config omitted resolved defaults");
  }
  const resolvedConfig = {
    endpoint_type: fixedEnum(resolvedSource.endpoint_type, ENDPOINT_TYPES, "resolved_config.endpoint_type"),
    study_type: fixedEnum(resolvedSource.study_type, STUDY_TYPES, "resolved_config.study_type"),
    design: fixedEnum(resolvedSource.design, DESIGNS, "resolved_config.design"),
    estimand: fixedEnum(resolvedSource.estimand, ESTIMANDS, "resolved_config.estimand"),
    direction: fixedEnum(resolvedSource.direction, DIRECTIONS, "resolved_config.direction"),
    sidedness: fixedEnum(resolvedSource.sidedness, SIDEDNESS, "resolved_config.sidedness"),
    null_param: finiteNumber(resolvedSource.null_param, "resolved_config.null_param"),
    alt_param: finiteNumber(resolvedSource.alt_param, "resolved_config.alt_param"),
    sd: nullableFiniteNumber(resolvedSource.sd, "resolved_config.sd"),
    alloc_ratio: finiteNumber(resolvedSource.alloc_ratio, "resolved_config.alloc_ratio"),
    alphas: finiteNumberArray(resolvedSource.alphas, "resolved_config.alphas"),
    powers: finiteNumberArray(resolvedSource.powers, "resolved_config.powers"),
    prior_params: finitePrior(resolvedSource.prior_params),
    go_threshold: finiteNumber(resolvedSource.go_threshold, "resolved_config.go_threshold"),
    consider_threshold: finiteNumber(resolvedSource.consider_threshold, "resolved_config.consider_threshold"),
    go_target: finiteNumber(resolvedSource.go_target, "resolved_config.go_target"),
    p3_n: nullableFiniteNumber(resolvedSource.p3_n, "resolved_config.p3_n"),
    p3_alloc_ratio: finiteNumber(resolvedSource.p3_alloc_ratio, "resolved_config.p3_alloc_ratio"),
    p3_alpha: finiteNumber(resolvedSource.p3_alpha, "resolved_config.p3_alpha"),
    accrual_time: nullableFiniteNumber(resolvedSource.accrual_time, "resolved_config.accrual_time"),
    followup_time: nullableFiniteNumber(resolvedSource.followup_time, "resolved_config.followup_time"),
    tte_method: nullableEnum(resolvedSource.tte_method, TTE_METHODS, "resolved_config.tte_method"),
    exposure_time: nullableFiniteNumber(resolvedSource.exposure_time, "resolved_config.exposure_time"),
    rate_method: nullableEnum(resolvedSource.rate_method, RATE_METHODS, "resolved_config.rate_method"),
    has_p2_data: fixedBoolean(resolvedSource.has_p2_data, "resolved_config.has_p2_data"),
    has_p2_control_data: fixedBoolean(
      resolvedSource.has_p2_control_data, "resolved_config.has_p2_control_data",
    ),
  };
  const seed = finiteNumber(simulationSource.seed, "simulation_defaults.seed");
  const bOc = finiteNumber(simulationSource.B_oc, "simulation_defaults.B_oc");
  if (!Number.isInteger(seed) || seed < 0 || !Number.isInteger(bOc) || bOc < 1) {
    throw new Error("validate_config returned invalid simulation defaults");
  }
  const simulationDefaults = { seed, b_oc: bOc };
  const projected = {
    valid: true,
    endpoint_type: fixedEnum(source.endpoint_type, ENDPOINT_TYPES, "endpoint_type"),
    study_type: fixedEnum(source.study_type, STUDY_TYPES, "study_type"),
    design: fixedEnum(source.design, DESIGNS, "design"),
    go_target: finiteNumber(source.go_target, "go_target"),
    alphas: finiteNumberArray(source.alphas, "alphas"),
    powers: finiteNumberArray(source.powers, "powers"),
    resolved_config: resolvedConfig,
    simulation_defaults: simulationDefaults,
  };
  const reportValue = {
    alphas: projected.alphas,
    design: projected.design,
    endpoint_type: projected.endpoint_type,
    go_target: projected.go_target,
    powers: projected.powers,
    resolved_config: projected.resolved_config,
    simulation_defaults: projected.simulation_defaults,
    study_type: projected.study_type,
    valid: projected.valid,
  };
  return {
    ...projected,
    configuration_report: `CONFIGURATION_VALIDATED ${JSON.stringify(reportValue)}`,
  };
}


function validSuiteRecord(value: unknown): boolean {
  const source = record(value);
  if (!source) return false;
  const output = source.output;
  const passes = source.passes;
  const fails = source.fails;
  const exitStatus = source.exit_status;
  if (source.ok !== true || typeof output !== "string" || !output.trim() ||
      !Number.isInteger(passes) || typeof passes !== "number" || passes <= 0 ||
      !Number.isInteger(fails) || typeof fails !== "number" || fails !== 0 ||
      !Number.isInteger(exitStatus) || typeof exitStatus !== "number" || exitStatus !== 0) {
    return false;
  }
  const lines = output.split(/\r?\n/);
  const outputPasses = lines.filter((line) => /^TEST .* : PASS$/.test(line)).length;
  const outputFails = lines.filter((line) => /^TEST .* : FAIL(?: |$)/.test(line)).length;
  return outputPasses === passes && outputFails === fails;
}


function hasExpectedPassCount(value: unknown, expected: number): boolean {
  const source = record(value);
  return validSuiteRecord(value) && source?.passes === expected;
}


/** Reduce raw regression logs to a fixed-key, label-free health attestation. */
export function publicRegressionStatus(value: unknown): JsonRecord {
  const source = record(value) ?? {};
  const skills = record(source.skills) ?? {};
  const actualSuites = Object.keys(skills).sort();
  const expectedSuites = [...REGRESSION_SUITES].sort();
  const completeSuiteSet = actualSuites.length === expectedSuites.length &&
    actualSuites.every((name, index) => name === expectedSuites[index]);
  const baseValidSuiteCount = REGRESSION_SUITES.reduce(
    (count, name) => count + (validSuiteRecord(skills[name]) ? 1 : 0), 0,
  );
  const passedSuiteCount = REGRESSION_SUITES.reduce(
    (count, name) => count + (
      hasExpectedPassCount(skills[name], EXPECTED_REGRESSION_PASS_COUNTS[name]) ? 1 : 0
    ), 0,
  );
  const checks = {
    declared_all_ok: source.all_ok === true,
    complete_suite_set: completeSuiteSet,
    suite_records_valid: baseValidSuiteCount === REGRESSION_SUITES.length,
    expected_check_count: passedSuiteCount === REGRESSION_SUITES.length,
  };
  const allOk = Object.values(checks).every((item) => item === true);
  return {
    all_ok: allOk,
    suite_count: REGRESSION_SUITES.length,
    passed_suite_count: passedSuiteCount,
    failed_suite_count: REGRESSION_SUITES.length - passedSuiteCount,
    checks,
  };
}
