type JsonRecord = Record<string, unknown>;


const ENDPOINT_TYPES = new Set(["binary", "continuous", "tte", "incidence_rate"]);
const STUDY_TYPES = new Set(["signal_detection", "poc", "confirmatory"]);
const DESIGNS = new Set(["single_arm", "controlled"]);

const REGRESSION_SUITES = [
  "vera-experiment-designing",
  "vera-master-experiment-designing",
  "vera-indirect-comparing",
  "vera-meta-analyzing",
  "vera-doe-designing",
] as const;

const EXPECTED_REGRESSION_PASS_COUNTS: Record<(typeof REGRESSION_SUITES)[number], number> = {
  "vera-experiment-designing": 26,
  "vera-master-experiment-designing": 47,
  "vera-indirect-comparing": 15,
  "vera-meta-analyzing": 12,
  "vera-doe-designing": 13,
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


/** Project the validator result to its exact, value-typed public DTO. */
export function publicValidatedConfig(value: unknown): JsonRecord {
  const source = record(value);
  if (!source || source.valid !== true) {
    throw new Error("validate_config did not return a valid configuration");
  }
  const projected = {
    valid: true,
    endpoint_type: fixedEnum(source.endpoint_type, ENDPOINT_TYPES, "endpoint_type"),
    study_type: fixedEnum(source.study_type, STUDY_TYPES, "study_type"),
    design: fixedEnum(source.design, DESIGNS, "design"),
    go_target: finiteNumber(source.go_target, "go_target"),
    alphas: finiteNumberArray(source.alphas, "alphas"),
    powers: finiteNumberArray(source.powers, "powers"),
  };
  const reportValue = {
    alphas: projected.alphas,
    design: projected.design,
    endpoint_type: projected.endpoint_type,
    go_target: projected.go_target,
    powers: projected.powers,
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
