/** Closed repository-owned installed capabilities, shared by MCP and hooks. */
import { lstatSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const suiteDirectory = dirname(dirname(fileURLToPath(import.meta.url)));
const skills = ["vera-experiment-designing", "vera-master-experiment-designing",
  "vera-doe-designing", "vera-indirect-comparing", "vera-meta-analyzing"];
const tools = ["validate_config", "sample_size", "simulate_design", "run_tests",
  "master_simulate", "ab_test", "factorial_design", "rsm_design", "randomize",
  "indirect_compare", "meta_analyze"];
const profiles = {
  complete: {
    skills, domains: ["single_endpoint", "master_protocol", "doe", "randomization",
      "indirect_comparison", "meta_analysis"], tools,
    r_packages: ["jsonlite", "survival", "Exact", "mvtnorm", "MAMS"],
    expected_regression_pass_counts: Object.fromEntries(
      skills.map((skill, index) => [skill, [31, 53, 14, 15, 14][index]])),
  },
  "single-endpoint": {
    skills: [skills[0]], domains: ["single_endpoint"], tools: tools.slice(0, 4),
    r_packages: ["jsonlite", "survival", "Exact"],
    expected_regression_pass_counts: { [skills[0]]: 31 },
  },
};

function sortedValue(value) {
  if (Array.isArray(value)) return value.map(sortedValue);
  if (value && typeof value === "object") return Object.fromEntries(
    Object.keys(value).sort().map((key) => [key, sortedValue(value[key])]),
  );
  return value;
}

function compactJSON(source) {
  // Keep string bytes intact. Requiring JSON.parse's canonical representation
  // after whitespace removal rejects duplicate keys (including nested ones),
  // ambiguous numeric spellings, and escaped duplicates without a second parser.
  let quoted = false;
  let escaped = false;
  let compact = "";
  for (const character of source) {
    if (quoted) {
      compact += character;
      if (escaped) escaped = false;
      else if (character === "\\") escaped = true;
      else if (character === '"') quoted = false;
    } else if (character === '"') {
      quoted = true;
      compact += character;
    } else if (!/[\t\n\r ]/.test(character)) compact += character;
  }
  return compact;
}

function freezeDeep(value) {
  if (value && typeof value === "object") {
    for (const child of Object.values(value)) freezeDeep(child);
    Object.freeze(value);
  }
  return value;
}

export function loadRuntimeProfile(suiteRoot = suiteDirectory) {
  const path = join(suiteRoot, "governance", "runtime-profiles.json");
  if (!lstatSync(dirname(path)).isDirectory()) {
    throw new Error("runtime profile directory must not be a symlink");
  }
  const metadata = lstatSync(path);
  if (!metadata.isFile() || metadata.size > 65536) {
    throw new Error("runtime profile must be a bounded regular non-symlink file");
  }
  const source = readFileSync(path, "utf8");
  const value = JSON.parse(source);
  if (compactJSON(source) !== JSON.stringify(value)) {
    throw new Error("runtime profile contains duplicate or noncanonical JSON values");
  }
  if (!value || typeof value !== "object" || Array.isArray(value)
      || Object.keys(value).sort().join(",") !== "active,profiles,schema_version"
      || value.schema_version !== 1 || typeof value.active !== "string"
      || !Object.hasOwn(profiles, value.active)
      || JSON.stringify(sortedValue(value.profiles)) !== JSON.stringify(sortedValue(profiles))) {
    throw new Error("runtime profile differs from the reviewed capability catalog");
  }
  return freezeDeep({ name: value.active, ...structuredClone(profiles[value.active]) });
}
