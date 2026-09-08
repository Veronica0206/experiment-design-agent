import { loadRuntimeProfile as readRuntimeProfile } from "../../governance/runtime_profile.mjs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";

export type RuntimeProfile = Readonly<{
  name: "complete" | "single-endpoint";
  skills: readonly string[];
  domains: readonly string[];
  tools: readonly string[];
  r_packages: readonly string[];
  expected_regression_pass_counts: Readonly<Record<string, number>>;
}>;

/** One repository-owned, strictly validated selector; never read from caller input. */
export function loadRuntimeProfile(suiteRoot?: string): RuntimeProfile {
  return readRuntimeProfile(suiteRoot) as RuntimeProfile;
}

export const ACTIVE_RUNTIME_PROFILE = loadRuntimeProfile(
  resolve(fileURLToPath(new URL("../..", import.meta.url))),
);
