import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { createHash, randomUUID } from "node:crypto";
import {
  constants as fsConstants,
  lstatSync,
  readFileSync,
  readdirSync,
  realpathSync,
  statSync,
} from "node:fs";
import { mkdtemp, open, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, delimiter, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { callR, shutdownActiveRProcesses } from "./r-bridge.js";
import {
  hashArtifactsForProvenance,
  publishVerifiedArtifacts,
} from "./artifact-publication.js";
import { readBoundedFile } from "./bounded-read.js";
import {
  preverify,
  shutdownActiveVerifierProcesses,
  type VerificationEnvelope,
} from "./verifier.js";
import { publicRegressionStatus, publicValidatedConfig } from "./public-projection.js";
import {
  currentPythonRuntimeSnapshot,
  currentRRuntimeSnapshot,
  FingerprintPromiseCache,
  fingerprintMutableEngineRuntime,
  hashFramedFields,
  shutdownActiveRuntimeProbeProcesses,
  waitForSharedPromise,
  type PythonRuntimeSnapshot,
  type RRuntimeSnapshot,
} from "./integrity.js";
import {
  ARTIFACT_ROOT,
  abortManagedArtifactDir,
  createManagedArtifactDir,
  releaseManagedArtifactDir,
} from "./artifacts.js";
import {
  InvalidRequestToolError,
} from "./tool-errors.js";
import { PINNED_RUNTIME_SUPERVISOR_COMMITMENT } from "./runtime-supervisor.js";
import { normalizedAllocationWeights } from "./allocation-ratio.js";
import { boundedToolResult } from "./response-budget.js";
import { RepositoryToolBoundary } from "./tool-boundary.js";
import { ACTIVE_RUNTIME_PROFILE, loadRuntimeProfile } from "./runtime-profile.js";
export { ACTIVE_RUNTIME_PROFILE } from "./runtime-profile.js";

const SERVER_VERSION = "1.1.0";
const PRIVATE_PROVENANCE_META_KEY = "experiment-design/private-provenance";
const PRIVATE_ARTIFACT_META_KEY = "experiment-design/private-artifact";
const SHA256_HEX = /^[0-9a-f]{64}$/;
const SUITE_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const MCP_ROOT = join(SUITE_ROOT, "mcp-server");
const MAX_STARTUP_DEPENDENCY_FILES = 20_000;
const MAX_STARTUP_DEPENDENCY_BYTES = 256 * 1024 * 1024;
export const MISSING_PRIVATE_ENGINE_INSTALLATION_MESSAGE =
  "Experiment Design MCP startup refused: the statistical engine installation " +
  "required by the repository runtime profile is unavailable.";

// This is an availability preflight, not a scientific or release-readiness
// attestation. The later runtime fingerprint and regression gates remain the
// authority for bytes and behavior. Keep this list limited to the maintained R
// entry files directly sourced by the public dispatcher plus every required
// regression entrypoint.
const ALL_ENGINE_RUNTIME_FILES: readonly string[] = [
  "vera-experiment-designing/scripts/R/config.R",
  "vera-experiment-designing/scripts/R/sample_size.R",
  "vera-experiment-designing/scripts/R/bayesian.R",
  "vera-experiment-designing/scripts/R/frequentist.R",
  "vera-experiment-designing/scripts/R/ppos.R",
  "vera-experiment-designing/scripts/R/run_framework.R",
  "vera-master-experiment-designing/scripts/R/shared_utils.R",
  "vera-master-experiment-designing/scripts/R/master_config.R",
  "vera-master-experiment-designing/scripts/R/basket_frequentist.R",
  "vera-master-experiment-designing/scripts/R/basket_bhm.R",
  "vera-master-experiment-designing/scripts/R/basket_bayesian.R",
  "vera-master-experiment-designing/scripts/R/basket_borrowing.R",
  "vera-master-experiment-designing/scripts/R/umbrella_mams.R",
  "vera-master-experiment-designing/scripts/R/umbrella_dtl.R",
  "vera-master-experiment-designing/scripts/R/umbrella_bar.R",
  "vera-master-experiment-designing/scripts/R/platform_rar.R",
  "vera-master-experiment-designing/scripts/R/platform_ncc.R",
  "vera-master-experiment-designing/scripts/R/platform_simulation.R",
  "vera-master-experiment-designing/scripts/R/run_master_framework.R",
  "vera-indirect-comparing/scripts/R/indirect_comparison.R",
  "vera-meta-analyzing/scripts/R/meta_endpoint_core.R",
  "vera-doe-designing/scripts/R/doe.R",
  ...["vera-experiment-designing", "vera-master-experiment-designing",
    "vera-doe-designing", "vera-indirect-comparing", "vera-meta-analyzing"]
    .map((skill) => `${skill}/scripts/tests/run_tests.R`),
];

export function requiredEngineRuntimeFiles(suiteRoot = SUITE_ROOT): readonly string[] {
  const profile = loadRuntimeProfile(suiteRoot);
  return ALL_ENGINE_RUNTIME_FILES.filter((path) => profile.skills.includes(path.split("/")[0]));
}

export const REQUIRED_PRIVATE_ENGINE_RUNTIME_FILES = requiredEngineRuntimeFiles();

class MissingPrivateEngineInstallationError extends Error {
  constructor() {
    super(MISSING_PRIVATE_ENGINE_INSTALLATION_MESSAGE);
    this.name = "MissingPrivateEngineInstallationError";
  }
}

/**
 * Refuse executable startup when any engine required by the profile is absent.
 *
 * This check intentionally has no environment-variable bypass. It runs only
 * from executable main, before the MCP transport is connected, so importing
 * this module does not connect a transport and an incomplete installation never
 * advertises tools it cannot execute.
 */
export function assertRequiredPrivateEngineInstallation(
  suiteRoot = SUITE_ROOT,
): void {
  let canonicalRoot: string;
  try {
    canonicalRoot = realpathSync(resolve(suiteRoot));
  } catch {
    throw new MissingPrivateEngineInstallationError();
  }

  for (const relativePath of requiredEngineRuntimeFiles(canonicalRoot)) {
    const candidate = resolve(canonicalRoot, ...relativePath.split("/"));
    if (!candidate.startsWith(`${canonicalRoot}${sep}`)) {
      throw new MissingPrivateEngineInstallationError();
    }
    try {
      const entry = lstatSync(candidate);
      if (!entry.isFile() || realpathSync(candidate) !== candidate) {
        throw new MissingPrivateEngineInstallationError();
      }
    } catch {
      throw new MissingPrivateEngineInstallationError();
    }
  }
}

const READ_ROOTS = (process.env.EXPDESIGN_ALLOWED_READ_ROOTS ?? SUITE_ROOT)
  .split(delimiter)
  .filter(Boolean)
  .map((root) => realpathSync(resolve(root)));

function hashFiles(files: string[], seed = ""): string {
  return hashFramedFields(
    files.sort().flatMap((path) => [path.replace(SUITE_ROOT, ""), readFileSync(path)]),
    seed,
  );
}

function flatFiles(root: string, suffix: string): string[] {
  try {
    return readdirSync(root)
      .filter((name) => name.endsWith(suffix))
      .map((name) => join(root, name));
  } catch {
    return [];
  }
}

type FingerprintBudget = {
  files: number;
  bytes: number;
  maxFiles: number;
  maxBytes: number;
};

function regularTreeFiles(
  root: string,
  seen = new Set<string>(),
  budget?: FingerprintBudget,
): string[] {
  const canonical = realpathSync(root);
  if (seen.has(canonical)) return [];
  const info = statSync(canonical);
  seen.add(canonical);
  if (info.isFile()) {
    if (budget) {
      budget.files += 1;
      budget.bytes += info.size;
      if (budget.files > budget.maxFiles || budget.bytes > budget.maxBytes) {
        throw new Error("installed production dependency closure exceeds fingerprint limits");
      }
    }
    return [canonical];
  }
  if (!info.isDirectory()) return [];
  return readdirSync(canonical).flatMap((name) =>
    regularTreeFiles(join(canonical, name), seen, budget));
}

/** Resolve every installed, non-dev package represented in an npm v3 lock. */
export function installedProductionDependencyRoots(
  packageRoot: string,
  lockPath = join(packageRoot, "package-lock.json"),
): string[] {
  const lock = JSON.parse(readFileSync(lockPath, "utf8")) as {
    packages?: Record<string, { dev?: boolean; optional?: boolean }>;
  };
  if (!lock.packages || typeof lock.packages !== "object") {
    throw new Error("package-lock.json lacks an installed package graph");
  }
  const modulesRoot = resolve(packageRoot, "node_modules");
  const roots: string[] = [];
  for (const [lockedPath, metadata] of Object.entries(lock.packages).sort()) {
    if (!lockedPath.startsWith("node_modules/") || metadata?.dev === true) continue;
    const candidate = resolve(packageRoot, lockedPath);
    if (candidate !== modulesRoot && !candidate.startsWith(`${modulesRoot}${sep}`)) {
      throw new Error("package-lock.json contains an invalid installed package path");
    }
    try {
      roots.push(realpathSync(candidate));
    } catch (error) {
      if (metadata?.optional === true) continue;
      throw new Error(`required production dependency is not installed: ${lockedPath}`, {
        cause: error,
      });
    }
  }
  return [...new Set(roots)].sort();
}

export function fingerprintStartupRuntime(
  nodeExecutable: string,
  runtimeFiles: string[],
  dependencyRoots: string[],
  runtimeVersion = process.version,
  limits: { maxFiles?: number; maxBytes?: number } = {},
  pinnedRuntimeCommitments: readonly string[] = [],
): string {
  for (const commitment of pinnedRuntimeCommitments) {
    if (!SHA256_HEX.test(commitment)) {
      throw new Error("pinned startup runtime commitment is malformed");
    }
  }
  const seen = new Set<string>();
  const budget: FingerprintBudget = {
    files: 0,
    bytes: 0,
    maxFiles: limits.maxFiles ?? MAX_STARTUP_DEPENDENCY_FILES,
    maxBytes: limits.maxBytes ?? MAX_STARTUP_DEPENDENCY_BYTES,
  };
  const seed = hashFramedFields([
    "experiment-design/startup-runtime-fingerprint/v2",
    ...pinnedRuntimeCommitments.flatMap((commitment, index) => [
      `pinned-runtime-${index}`,
      commitment,
    ]),
  ], runtimeVersion);
  return hashFiles([
    realpathSync(nodeExecutable),
    ...runtimeFiles,
    ...dependencyRoots.flatMap((root) => regularTreeFiles(root, seen, budget)),
  ], seed);
}

// JavaScript modules and dependency declarations are loaded at process start;
// preserve the fingerprint of those exact startup bytes even if files change.
// Bind the interpreter binary and complete installed production dependency
// closure too: lockfiles describe intended dependencies, but only installed
// bytes are executed. The bounded traversal covers npm's flattened transitives.
if (PINNED_RUNTIME_SUPERVISOR_COMMITMENT === undefined) {
  throw new Error("pinned runtime supervisor commitment is unavailable");
}
const runtimeSupervisorDistPath = resolve(MCP_ROOT, "dist", "runtime-supervisor.js");
const startupJavaScriptFiles = flatFiles(join(MCP_ROOT, "dist"), ".js");
if (!startupJavaScriptFiles.some((path) => resolve(path) === runtimeSupervisorDistPath)) {
  throw new Error("compiled runtime supervisor is absent from the startup runtime");
}
export const STARTUP_RUNTIME_FINGERPRINT = fingerprintStartupRuntime(
  process.execPath,
  [
    // runtime-supervisor.js was captured during dependency evaluation. Hashing
    // its pathname here would bind a later reopen, not the program we execute.
    ...startupJavaScriptFiles.filter(
      (path) => resolve(path) !== runtimeSupervisorDistPath,
    ),
    join(MCP_ROOT, "package.json"),
    join(MCP_ROOT, "package-lock.json"),
    join(SUITE_ROOT, "governance", "runtime-profiles.json"),
    join(SUITE_ROOT, "governance", "runtime_profile.mjs"),
  ],
  installedProductionDependencyRoots(MCP_ROOT),
  process.version,
  {},
  [PINNED_RUNTIME_SUPERVISOR_COMMITMENT],
);

type BoundRuntime = { r: RRuntimeSnapshot; python: PythonRuntimeSnapshot };
const runtimeByEngineFingerprint = new Map<string, BoundRuntime>();

async function engineFingerprint(signal?: AbortSignal): Promise<string> {
  if (JSON.stringify(loadRuntimeProfile(SUITE_ROOT)) !== JSON.stringify(ACTIVE_RUNTIME_PROFILE)) {
    throw new Error("Repository runtime profile changed after server startup");
  }
  // R and Python modules are loaded by a fresh child process for each call, so
  // recompute their hashes at provenance time rather than freezing startup state.
  const rRuntime = await currentRRuntimeSnapshot(signal);
  const pythonRuntime = await currentPythonRuntimeSnapshot(signal);
  const fingerprint = await fingerprintMutableEngineRuntime(
    SUITE_ROOT,
    `${STARTUP_RUNTIME_FINGERPRINT}:${rRuntime.fingerprint}:${pythonRuntime.fingerprint}`,
    signal,
  );
  runtimeByEngineFingerprint.set(fingerprint, { r: rRuntime, python: pythonRuntime });
  while (runtimeByEngineFingerprint.size > 16) {
    const oldest = runtimeByEngineFingerprint.keys().next().value;
    if (typeof oldest !== "string") break;
    runtimeByEngineFingerprint.delete(oldest);
  }
  return fingerprint;
}

async function assertEngineUnchanged(
  expected: string,
  phase: string,
  signal?: AbortSignal,
): Promise<void> {
  if (await engineFingerprint(signal) !== expected) {
    throw new Error(`engine sources changed while ${phase}`);
  }
}

const regressionCache = new FingerprintPromiseCache<unknown>();

async function regressionAttestation(
  fingerprint: string,
  signal?: AbortSignal,
): Promise<unknown> {
  const shared = regressionCache.get(fingerprint, async () => {
    // The single-flight job is process-wide. Individual request cancellation
    // cancels only that request's wait, never another caller's attestation.
    const result = publicRegressionStatus(await callR("run_tests", {}));
    await assertEngineUnchanged(fingerprint, "regression attestation was running");
    return result;
  });
  return waitForSharedPromise(shared, signal);
}

const verificationMeta = {
  verification_id: z.string().trim().min(1).max(128).optional()
    .describe("Deprecated caller hint; the runtime always replaces it with its own analysis identity."),
};

const MAX_GRID_VALUES = 64;
const MAX_PRIOR_FIELDS = 16;
const MAX_PRIOR_KEY_CHARS = 64;
const MAX_LABEL_CHARS = 256;
const MAX_COLUMN_NAME_CHARS = 128;
const MAX_COVARIATES = 256;

/**
 * JSON.parse accepts exponent overflows such as 1e309 as Infinity. It also
 * accepts integer tokens outside Number.MAX_SAFE_INTEGER, where the parsed
 * Number may no longer equal the integer the caller supplied even when
 * JSON.stringify happens to reproduce the same decimal spelling. Zod 3's
 * unconstrained z.number() accepts both cases. Apply one recursive guard to
 * every tool request after schema parsing and immediately before any handler
 * can run, before either value can be laundered across another JSON boundary.
 */
export function assertFiniteNumericInputs(value: unknown): void {
  const seen = new WeakSet<object>();
  const visit = (item: unknown): void => {
    if (typeof item === "number") {
      if (!Number.isFinite(item)) {
        throw new InvalidRequestToolError("All numeric inputs must be finite");
      }
      if (Number.isInteger(item) && !Number.isSafeInteger(item)) {
        throw new InvalidRequestToolError(
          "Integral numeric inputs must be within JavaScript's safe-integer range",
        );
      }
      return;
    }
    if (!item || typeof item !== "object" || seen.has(item)) return;
    seen.add(item);
    if (Array.isArray(item)) {
      for (const child of item) visit(child);
      return;
    }
    for (const child of Object.values(item as Record<string, unknown>)) visit(child);
  };
  visit(value);
}

const registerStrictTool = (
  name: string,
  description: string,
  shape: z.ZodRawShape,
  handler: (params: any, signal: AbortSignal) => Promise<any>,
) => {
  if (!ACTIVE_RUNTIME_PROFILE.tools.includes(name)) return;
  toolBoundary.register(
  name,
  description,
  z.object(shape).strict(),
  async (params, signal) => {
    assertFiniteNumericInputs(params);
    return boundedToolResult(await handler(params, signal));
  },
  );
};

function domainParams(params: Record<string, unknown>): Record<string, unknown> {
  const { verification_id: _verificationId, ...domain } = params;
  return domain;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function buildProvenance(
  result: unknown,
  params: Record<string, unknown>,
  boundInputHashes?: Record<string, string>,
  executionFingerprint?: string,
) {
  if (!executionFingerprint) throw new Error("bound runtime provenance is unavailable");
  const domain = domainParams(params);
  const runtime = runtimeByEngineFingerprint.get(executionFingerprint);
  if (!runtime) throw new Error("bound runtime provenance is unavailable");
  const requestedInputs = ["ipd_file", "targets_file"]
    .filter((key) => typeof domain[key] === "string");
  if (requestedInputs.length && !boundInputHashes) {
    throw new Error("input-file provenance requires an immutable bound snapshot");
  }
  return {
    engine_version: SERVER_VERSION,
    engine_fingerprint: executionFingerprint,
    node_version: process.version,
    python_version: runtime.python.version,
    python_runtime_fingerprint: runtime.python.fingerprint,
    r_version: runtime.r.version,
    r_runtime_fingerprint: runtime.r.fingerprint,
    r_package_versions: runtime.r.packageVersions,
    config_hash: createHash("sha256").update(stableJson(domain)).digest("hex"),
    input_hashes: boundInputHashes ?? {},
    artifact_hashes: (() => {
      const value = result as Record<string, unknown> | null;
      const outputDir = value && typeof value.output_dir === "string" ? value.output_dir : null;
      if (!outputDir) return {};
      return hashArtifactsForProvenance(outputDir);
    })(),
  };
}

function digestMap(value: unknown, label: string): Record<string, string> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} provenance is malformed`);
  }
  const normalized: Record<string, string> = {};
  for (const [key, digest] of Object.entries(value as Record<string, unknown>)) {
    if (!key || typeof digest !== "string" || !SHA256_HEX.test(digest.toLowerCase())) {
      throw new Error(`${label} provenance is malformed`);
    }
    normalized[key] = digest.toLowerCase();
  }
  return normalized;
}

function requiredProvenanceString(value: unknown): string {
  if (typeof value !== "string" || !value) {
    throw new Error("runtime provenance is malformed");
  }
  return value;
}

function publicProvenance(provenance: Record<string, unknown>): Record<string, unknown> {
  const inputHashes = digestMap(provenance.input_hashes, "input");
  const artifactHashes = digestMap(provenance.artifact_hashes, "artifact");
  const rawPackages = provenance.r_package_versions;
  if (!rawPackages || typeof rawPackages !== "object" || Array.isArray(rawPackages)) {
    throw new Error("runtime provenance is malformed");
  }
  const rPackageVersions = Object.fromEntries(
    Object.entries(rawPackages as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, version]) => {
        if (!name || (typeof version !== "string" && version !== null)) {
          throw new Error("runtime provenance is malformed");
        }
        return [name, version];
      }),
  );
  const configHash = requiredProvenanceString(provenance.config_hash).toLowerCase();
  if (!SHA256_HEX.test(configHash)) throw new Error("configuration provenance is malformed");
  return {
    engine_version: requiredProvenanceString(provenance.engine_version),
    engine_fingerprint: requiredProvenanceString(provenance.engine_fingerprint),
    node_version: requiredProvenanceString(provenance.node_version),
    python_version: requiredProvenanceString(provenance.python_version),
    python_runtime_fingerprint: requiredProvenanceString(provenance.python_runtime_fingerprint),
    r_version: requiredProvenanceString(provenance.r_version),
    r_runtime_fingerprint: requiredProvenanceString(provenance.r_runtime_fingerprint),
    r_package_versions: rPackageVersions,
    config_bound: true,
    input_file_count: Object.keys(inputHashes).length,
    artifact_count: Object.keys(artifactHashes).length,
  };
}

function privateProvenanceMeta(
  provenance: Record<string, unknown>,
  analysisId: string,
  artifactHandles: Record<string, string> = {},
): Record<string, unknown> {
  const configHash = requiredProvenanceString(provenance.config_hash).toLowerCase();
  if (!SHA256_HEX.test(configHash)) throw new Error("configuration provenance is malformed");
  return {
    [PRIVATE_PROVENANCE_META_KEY]: {
      version: 2,
      analysis_id: analysisId,
      config_hash: configHash,
      input_hashes: digestMap(provenance.input_hashes, "input"),
      artifact_handles: artifactHandles,
    },
  };
}

function toolResponse(
  result: unknown,
  params: Record<string, unknown>,
  verification?: VerificationEnvelope,
  boundProvenance?: Record<string, unknown>,
) {
  const provenance = boundProvenance ?? buildProvenance(result, params);
  const publicProvenanceView = publicProvenance(provenance);
  const analysisId = verification?.identity?.analysis_id;
  if (verification && (typeof analysisId !== "string" || !analysisId)) {
    throw new Error("verification is missing its runtime analysis identity");
  }
  const publicVerification = verification ? (() => {
    const {
      public_result: _publicResult,
      identity: rawIdentity,
      ...rest
    } = verification as VerificationEnvelope & {
      identity?: VerificationEnvelope["identity"] & {
        args_hash?: unknown;
        result_hash?: unknown;
      };
    };
    if (!rawIdentity) return rest;
    const {
      args_hash: _rawArgsHash,
      result_hash: _rawResultHash,
      ...publicIdentity
    } = rawIdentity;
    if (typeof publicIdentity.public_args_hash !== "string" ||
        !publicIdentity.public_args_hash) {
      throw new Error("verification is missing its public argument binding");
    }
    return { ...rest, identity: publicIdentity };
  })() : undefined;
  if (verification && !verification.presentable) {
    const clientMeta = analysisId ? privateProvenanceMeta(provenance, analysisId) : undefined;
    return { content: [{ type: "text" as const, text: JSON.stringify({
      error: "Result withheld because server-side verification failed.",
      _verification: publicVerification,
      _provenance: publicProvenanceView,
    }, null, 2) }], ...(clientMeta ? { _meta: clientMeta } : {}) };
  }
  let visibleResult = result;
  let visibleVerification = verification;
  if (verification) {
    if (verification.public_result === undefined || !verification.report_hash ||
        !verification.public_result_hash) {
      throw new Error("presentable verification is missing its privacy-safe binding");
    }
    visibleResult = verification.public_result;
    visibleVerification = publicVerification;
  }
  const payload = visibleResult && typeof visibleResult === "object" && !Array.isArray(visibleResult)
    ? { ...(visibleResult as Record<string, unknown>), _verification: visibleVerification, _provenance: publicProvenanceView }
    : { result: visibleResult, _verification: visibleVerification, _provenance: publicProvenanceView };
  let artifacts: string[] = [];
  if (result && typeof result === "object") {
    const value = result as Record<string, unknown>;
    const candidates: unknown[] = [
      ...(value.private_assignment_artifact ? [value.private_assignment_artifact] : []),
      ...(Array.isArray(value.artifacts) ? value.artifacts : []),
    ];
    if (typeof value.output_dir === "string") {
      try {
        candidates.push(...readdirSync(value.output_dir, { withFileTypes: true })
          .filter((entry) => !entry.name.startsWith(".expdesign-") &&
            (entry.isFile() || entry.isSymbolicLink()))
          .map((entry) => join(value.output_dir as string, entry.name)));
      } catch { /* output directory may be absent for non-artifact tools */ }
    }
    artifacts = publishVerifiedArtifacts(
      candidates,
      value.output_dir,
      (provenance as Record<string, unknown>).artifact_hashes,
    );
  }
  const artifactHashes = digestMap(
    (provenance as Record<string, unknown>).artifact_hashes,
    "artifact",
  );
  const artifactRecords = artifacts.map((path) => {
    const name = basename(path);
    const digest = artifactHashes[name];
    if (!analysisId || !digest) {
      throw new Error("published artifact is missing identity-bound provenance");
    }
    const handle = randomUUID();
    return { path, name, digest, handle };
  });
  const clientMeta = analysisId ? privateProvenanceMeta(
    provenance,
    analysisId,
    Object.fromEntries(artifactRecords.map(({ handle, path }) => [handle, path])),
  ) : undefined;
  return { content: [
    { type: "text" as const, text: JSON.stringify(payload, null, 2) },
    ...artifactRecords.map(({ name, digest, handle }) => {
      return {
        type: "resource_link" as const,
        name,
        title: "Private verified artifact",
        uri: `expdesign-artifact://${encodeURIComponent(analysisId!)}/${handle}`,
        description: "Host-side artifact; contents are not included in model context.",
        annotations: { audience: ["user" as const] },
        _meta: {
          [PRIVATE_ARTIFACT_META_KEY]: {
            version: 2,
            analysis_id: analysisId,
            sha256: digest,
            handle,
          },
        },
      };
    }),
  ], ...(clientMeta ? { _meta: clientMeta } : {}) };
}

async function runTool(
  tool: string,
  params: Record<string, unknown>,
  signal: AbortSignal,
) {
  const domain = domainParams(params);
  const executionFingerprint = await engineFingerprint(signal);
  const regression = await regressionAttestation(executionFingerprint, signal);
  const result = await callR(tool, domain, signal);
  const stochastic = tool === "simulate_design" || tool === "randomize" ||
    ((tool === "factorial_design" || tool === "rsm_design") && domain.randomize === true);
  const replay = stochastic ? await callR(tool, domain, signal) : undefined;
  await assertEngineUnchanged(executionFingerprint, "the analysis was running", signal);
  const provenance = buildProvenance(result, params, undefined, executionFingerprint);
  const verification = await preverify(
    tool, domain, result, replay, stochastic, [], publicProvenance(provenance), regression, signal,
  );
  await assertEngineUnchanged(executionFingerprint, "the verifier was running", signal);
  return toolResponse(result, params, verification, provenance);
}

async function runRandomizeTool(
  params: Record<string, unknown>,
  signal: AbortSignal,
) {
  const domain = domainParams(params);
  const executionFingerprint = await engineFingerprint(signal);
  const regression = await regressionAttestation(executionFingerprint, signal);
  const managedOutput = await persistentOutputDir(undefined, "randomize-");
  let completed = false;
  try {
    const raw = await callR("randomize", domain, signal);
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw new Error("randomize returned a non-object result");
    }
    const assignmentPath = join(managedOutput, "assignments.json");
    await writeFile(assignmentPath, JSON.stringify({
      assignment: (raw as Record<string, unknown>).assignment,
      seed: (raw as Record<string, unknown>).seed,
    }, null, 2), { mode: 0o600 });
    const result = {
      ...(raw as Record<string, unknown>),
      output_dir: managedOutput,
      private_assignment_artifact: assignmentPath,
    };
    const replay = await callR("randomize", domain, signal);
    await assertEngineUnchanged(executionFingerprint, "the analysis was running", signal);
    const provenance = buildProvenance(
      result, params, undefined, executionFingerprint,
    );
    const verification = await preverify(
      "randomize", domain, result, replay, true, [], publicProvenance(provenance), regression,
      signal,
    );
    await assertEngineUnchanged(executionFingerprint, "the verifier was running", signal);
    const response = toolResponse(result, params, verification, provenance);
    completed = verification.presentable;
    return response;
  } finally {
    if (completed) await releaseManagedArtifactDir(managedOutput);
    else await abortManagedArtifactDir(managedOutput);
  }
}

async function runUngatedTool(
  tool: string,
  params: Record<string, unknown>,
  signal: AbortSignal,
) {
  let result: Record<string, unknown>;
  if (tool === "run_tests") {
    result = await regressionAttestation(
      await engineFingerprint(signal), signal,
    ) as Record<string, unknown>;
  } else if (tool === "validate_config") {
    result = publicValidatedConfig(await callR(tool, domainParams(params), signal));
  } else {
    throw new Error("unsupported ungated tool");
  }
  return { content: [{ type: "text" as const, text: JSON.stringify(result, null, 2) }] };
}

function within(root: string, target: string): boolean {
  return target === root || target.startsWith(root + sep);
}

export async function readAllowedFile(rawPath: string): Promise<Buffer> {
  try {
    return await readAllowedFileAuthorized(rawPath);
  } catch (error) {
    if (error instanceof InvalidRequestToolError) throw error;
    throw new InvalidRequestToolError(
      `Read path could not be authorized safely: ${rawPath}`,
      error,
    );
  }
}

async function readAllowedFileAuthorized(rawPath: string): Promise<Buffer> {
  const candidate = resolve(rawPath);
  // Resolve and authorize before opening. In particular, do not open FIFOs,
  // devices, or outside-root paths merely to discover that they are forbidden.
  const target = realpathSync(candidate);
  if (!READ_ROOTS.some((root) => within(root, target))) {
    throw new Error(`Read path is outside EXPDESIGN_ALLOWED_READ_ROOTS: ${rawPath}`);
  }
  const artifactRoots = [resolve(ARTIFACT_ROOT)];
  try { artifactRoots.push(realpathSync(ARTIFACT_ROOT)); } catch { /* root may not exist yet */ }
  if (artifactRoots.some((root) => within(root, candidate) || within(root, target))) {
    throw new Error("Managed output artifacts cannot be used as analysis inputs");
  }
  const authorized = statSync(target);
  if (!authorized.isFile()) throw new Error(`Read path is not a regular file: ${rawPath}`);
  if (authorized.size > MAX_INPUT_BYTES) {
    throw new Error(`Input file exceeds the ${MAX_INPUT_BYTES}-byte limit: ${rawPath}`);
  }
  const handle = await open(
    target,
    fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK,
  );
  try {
    const opened = await handle.stat();
    if (!opened.isFile()) throw new Error(`Read path is not a regular file: ${rawPath}`);
    if (opened.size > MAX_INPUT_BYTES) {
      throw new Error(`Input file exceeds the ${MAX_INPUT_BYTES}-byte limit: ${rawPath}`);
    }
    // Bind the object inspected at the canonical name to the opened object.
    // This detects replacement after stat; authorized parent-directory
    // stability is part of the local single-user trust boundary documented in
    // the server README.
    if (authorized.dev !== opened.dev || authorized.ino !== opened.ino) {
      throw new Error(`Read path changed during authorization: ${rawPath}`);
    }
    return await readBoundedFile(handle, MAX_INPUT_BYTES, `Input file ${rawPath}`);
  } finally {
    await handle.close();
  }
}

function boundedCsvRows(bytes: Buffer, maximum: number, label: string): number {
  // Counting physical lines is deliberately conservative for quoted multiline
  // cells: it may reject an unusually encoded file, but it never undercounts a
  // memory-amplifying input before the R CSV parser materializes it.
  let lines = bytes.length ? 1 : 0;
  for (const byte of bytes) if (byte === 0x0a) lines += 1;
  const dataRows = Math.max(0, lines - 1);
  if (dataRows > maximum) {
    throw new InvalidRequestToolError(`${label} exceeds the ${maximum}-row limit`);
  }
  return dataRows;
}

async function persistentOutputDir(rawPath: string | undefined, prefix: string): Promise<string> {
  try {
    return await createManagedArtifactDir(prefix, rawPath);
  } catch (error) {
    if (rawPath === undefined || error instanceof InvalidRequestToolError) throw error;
    throw new InvalidRequestToolError(
      `The requested output directory could not be authorized safely: ${rawPath}`,
      error,
    );
  }
}

// Truth-in-advertising at the TOOL BOUNDARY: only the implemented method values
// are accepted. The MCP SDK parses arguments with zod in strip mode, so a key
// absent from the schema would be silently DELETED — a request for "cox_ph" /
// "negbin" / "holm" would quietly degrade to exponential/Poisson/no-correction
// and the R-side config error would never fire. Exposing each knob as a
// single-value enum makes the reserved values fail loudly here instead.
// Widen these enums only when the corresponding method actually computes.
const tteMethod = z.enum(["exponential"]).optional()
  .describe("Only 'exponential' is implemented; 'cox_ph' is reserved and rejected");
const rateMethod = z.enum(["poisson"]).optional()
  .describe("Only 'poisson' is implemented; 'negbin' is reserved and rejected");
const fwerControl = z.enum(["none"]).optional()
  .describe("No selectable extra procedure is implemented. Basket FWER is measured; umbrella/platform final decisions use built-in Bonferroni critical values. 'holm'/'bonferroni' inputs are rejected.");

// Prior accepted by config.R resolve_prior: a named prior string, or a custom
// hyperparameter list (all-numeric values; required keys depend on endpoint).
const priorSchema = z.union([
  z.enum(["jeffreys", "flat", "skeptical"]),
  z.record(z.string().min(1).max(MAX_PRIOR_KEY_CHARS), z.number()).refine(
    (value) => Object.keys(value).length <= MAX_PRIOR_FIELDS,
    `custom priors may contain at most ${MAX_PRIOR_FIELDS} hyperparameters`,
  ),
]).optional().describe(
  "Bayesian prior: 'jeffreys' | 'flat' | 'skeptical', or custom hyperparameters — " +
  "binary {a,b}; continuous {mu0,kappa0,alpha0,beta0}; tte/incidence_rate {shape,rate}"
);

// create_config() parameters shared by validate_config, sample_size, and the
// simulate_design config object. Every key here is a real create_config formal;
// keeping them in one place stops a tool from validating/sizing a DIFFERENT
// config than the one simulate_design would run.
const singleEndpointParams = {
  endpoint_type: z.enum(["binary", "continuous", "tte", "incidence_rate"]),
  study_type: z.enum(["signal_detection", "poc", "confirmatory"]),
  design: z.enum(["single_arm", "controlled"]),
  null_param: z.number(),
  alt_param: z.number(),
  sd: z.number().positive().optional(),
  alloc_ratio: z.number().positive().optional(),
  alphas: z.union([
    z.number().gt(0).lt(1),
    z.array(z.number().gt(0).lt(1)).min(1).max(MAX_GRID_VALUES),
  ]).optional().describe(
    "One-sided alpha level(s); 0.025 one-sided corresponds to 0.05 two-sided",
  ),
  powers: z.union([
    z.number().gt(0).lt(1),
    z.array(z.number().gt(0).lt(1)).min(1).max(MAX_GRID_VALUES),
  ]).optional(),
  prior: priorSchema,
  go_threshold: z.number().gt(0).lt(1).optional()
    .describe("Posterior probability threshold for Go (non-confirmatory decisions; default 0.90)"),
  consider_threshold: z.number().gt(0).lt(1).optional()
    .describe("Posterior probability threshold for Consider (default 0.60)"),
  go_target: z.number().optional()
    .describe("Go/No-Go posterior target: P(theta > go_target | data). Default: midpoint of null/alt (geometric mean for tte)"),
  accrual_time: z.number().positive().optional(),
  followup_time: z.number().positive().optional(),
  exposure_time: z.number().positive().optional(),
  tte_method: tteMethod,
  rate_method: rateMethod,
};

const SINGLE_ENDPOINT_SCOPED_FIELDS = [
  "sd", "accrual_time", "followup_time", "exposure_time",
  "tte_method", "rate_method",
] as const;
const SINGLE_ENDPOINT_ALLOWED_SCOPED_FIELDS: Record<string, ReadonlySet<string>> = {
  binary: new Set(),
  continuous: new Set(["sd"]),
  tte: new Set(["accrual_time", "followup_time", "tte_method"]),
  incidence_rate: new Set(["exposure_time", "rate_method"]),
};

function assertSingleEndpointFieldScope(params: Record<string, unknown>): void {
  const endpoint = String(params.endpoint_type);
  const allowed = SINGLE_ENDPOINT_ALLOWED_SCOPED_FIELDS[endpoint];
  if (!allowed) return;
  const foreignFields = SINGLE_ENDPOINT_SCOPED_FIELDS.filter(
    (field) => params[field] !== undefined && !allowed.has(field),
  );
  if (foreignFields.length > 0) {
    throw new InvalidRequestToolError(
      `endpoint_type='${endpoint}' does not accept ${foreignFields.join(", ")}`,
    );
  }
  if (params.design === "single_arm" && params.alloc_ratio !== undefined) {
    throw new InvalidRequestToolError(
      "alloc_ratio is accepted only for controlled single-endpoint designs",
    );
  }
  const prior = params.prior;
  if (prior && typeof prior === "object" && !Array.isArray(prior)) {
    const expectedPriorKeys: Record<string, readonly string[]> = {
      binary: ["a", "b"],
      continuous: ["alpha0", "beta0", "kappa0", "mu0"],
      tte: ["rate", "shape"],
      incidence_rate: ["rate", "shape"],
    };
    const actual = Object.keys(prior as Record<string, unknown>).sort();
    const expected = expectedPriorKeys[endpoint];
    if (actual.length !== expected.length ||
        actual.some((key, index) => key !== expected[index])) {
      throw new InvalidRequestToolError(
        "custom prior keys do not match the selected endpoint_type",
      );
    }
  }
}

const p2DataSchema = z.union([
  z.object({ x: z.number().int().nonnegative(), n: z.number().int().positive() }).strict(),
  z.object({ x_bar: z.number(), s2: z.number().nonnegative(), n: z.number().int().min(2) }).strict(),
  z.object({ events: z.number().int().nonnegative(), person_time: z.number().positive() }).strict(),
  z.object({ count: z.number().int().nonnegative(), exposure: z.number().positive() }).strict(),
]);

const MAX_SIMULATED_UNITS = 10_000_000;
const MAX_INPUT_BYTES = 50 * 1024 * 1024;
const MAX_IPD_ROWS = 1_000_000;
const MAX_TARGET_ROWS = 10_000;
const MAX_MAIC_BOOTSTRAP_ROW_OPERATIONS = 50_000_000;
const strictPath = z.string().min(1).max(4096).refine(
  (value) => value === value.trim(),
  "paths may not contain leading or trailing whitespace",
);

function assertSingleEndpointParams(params: Record<string, unknown>): void {
  assertSingleEndpointFieldScope(params);
  const go = Number(params.go_threshold ?? 0.90);
  const consider = Number(params.consider_threshold ?? 0.60);
  if (!(consider < go)) {
    throw new InvalidRequestToolError("consider_threshold must be below go_threshold");
  }
  const endpoint = params.endpoint_type;
  const nullParam = Number(params.null_param);
  const altParam = Number(params.alt_param);
  if (endpoint === "binary") {
    if (nullParam < 0 || nullParam > 1 || altParam < 0 || altParam > 1) {
      throw new InvalidRequestToolError("binary endpoint parameters must lie in [0,1]");
    }
    if (!(altParam > nullParam)) {
      throw new InvalidRequestToolError("binary alt_param must exceed null_param");
    }
  } else if (endpoint === "continuous") {
    if (!(altParam > nullParam)) {
      throw new InvalidRequestToolError("continuous alt_param must exceed null_param");
    }
    if (!(Number(params.sd) > 0)) {
      throw new InvalidRequestToolError("continuous endpoints require a positive sd");
    }
  } else if (endpoint === "tte") {
    if (!(nullParam > 0 && altParam > 0 && altParam < nullParam)) {
      throw new InvalidRequestToolError(
        "tte endpoint parameters must be positive with alt_param below null_param",
      );
    }
    if (!(Number(params.accrual_time) > 0 && Number(params.followup_time) > 0)) {
      throw new InvalidRequestToolError(
        "tte endpoints require positive accrual_time and followup_time",
      );
    }
  } else if (endpoint === "incidence_rate") {
    if (nullParam < 0 || altParam < 0 || altParam === nullParam) {
      throw new InvalidRequestToolError(
        "incidence-rate parameters must be non-negative and distinct",
      );
    }
    if (!(Number(params.exposure_time) > 0)) {
      throw new InvalidRequestToolError(
        "incidence-rate endpoints require a positive exposure_time",
      );
    }
  }
  if (params.design === "single_arm" && params.p2_data_ctrl !== undefined) {
    throw new InvalidRequestToolError("p2_data_ctrl is not valid for a single-arm design");
  }
  if (params.go_target !== undefined) {
    const target = Number(params.go_target);
    if (params.endpoint_type === "binary" && (target < 0 || target > 1)) {
      throw new InvalidRequestToolError("binary go_target must lie in [0,1]");
    }
    if (params.endpoint_type === "tte" && target <= 0) {
      throw new InvalidRequestToolError("tte go_target must be positive");
    }
    if (params.endpoint_type === "incidence_rate" && target < 0) {
      throw new InvalidRequestToolError("incidence-rate go_target must be non-negative");
    }
  }
}

const server = new Server(
  { name: "experiment-design", version: SERVER_VERSION },
  { capabilities: { tools: { listChanged: false } } },
);
const toolBoundary = new RepositoryToolBoundary(server);

// --- Tool: validate_config ---

registerStrictTool(
  "validate_config",
  "Validate experiment design parameters without running a simulation. Returns resolved defaults (alphas, powers, go_target).",
  { ...singleEndpointParams },
  async (params, signal) => {
    assertSingleEndpointParams(params);
    return runUngatedTool("validate_config", params, signal);
  }
);

// --- Tool: sample_size ---

registerStrictTool(
  "sample_size",
  "Compute sample sizes for a single-endpoint experiment design. Returns a table of designs with n_total, n_trt, n_ctrl, power_achieved, and critical values across configured alpha/power grids.",
  { ...singleEndpointParams, ...verificationMeta },
  async (params, signal) => {
    assertSingleEndpointParams(params);
    return runTool("sample_size", params, signal);
  }
);

// --- Tool: simulate_design ---

registerStrictTool(
  "simulate_design",
  "Run a full single-endpoint experiment design simulation: sample size calculation, operating characteristics (Go/No-Go probabilities under null and alternative), and optionally PPOS. All statistics come from versioned, seeded R code.",
  {
    ...verificationMeta,
    // `overdispersion` stays unexposed (meaningful only with negbin, which is
    // rejected). tte_method/rate_method use the single-value enums above so a
    // reserved value errors at the boundary instead of being zod-stripped.
    // .strict(): any key NOT in this schema errors at the boundary — without it
    // zod strip mode silently deletes unknown keys and the run quietly proceeds
    // with defaults under the caller's requested label.
    config: z.object({
      ...singleEndpointParams,
      p2_data: p2DataSchema.optional()
        .describe("Exploratory-stage treatment-arm data for PPOS (confirmatory): binary {x,n}; continuous {x_bar,s2,n}; tte {events,person_time}; incidence_rate {count,exposure}"),
      p2_data_ctrl: p2DataSchema.optional()
        .describe("Exploratory-stage CONTROL-arm data (controlled designs; same shape as p2_data). Required whenever controlled-design PPOS is requested; missing data is rejected."),
      p3_n: z.number().int().min(1).max(10_000_000).optional()
        .describe("Planned confirmatory-stage total sample size (PPOS)"),
      p3_alloc_ratio: z.number().gt(0).optional()
        .describe("Confirmatory-stage allocation ratio treatment:control for the two-arm PPOS (default: alloc_ratio)"),
      p3_alpha: z.number().gt(0).lt(1).optional()
        .describe("Confirmatory-stage one-sided alpha; 0.025 corresponds to 0.05 two-sided"),
      label: z.string().max(MAX_LABEL_CHARS).optional().describe("Display label for the endpoint"),
    }).strict(),
    seed: z.number().int().min(0).max(2147483647).optional().describe("Integer RNG seed (default 42); echoed in the result"),
    n_oc: z.union([
      z.number().int().min(1).max(10_000_000),
      z.object({
        n_trt: z.number().int().min(1).max(10_000_000),
        n_ctrl: z.number().int().min(1).max(10_000_000),
      }).strict(),
    ]).optional()
      .describe("OC simulation sample size: total n (single-arm) or {n_trt, n_ctrl} (controlled). Default: taken from the sample-size table. The effective value is echoed as n_oc_used."),
    B_oc: z.number().int().min(1).max(5000).optional()
      .describe("OC simulation replicates (default 5000). The R layer hard-caps at 5000; the effective value is echoed as B_used."),
    delta: z.number().optional(),
  },
  async (params, signal) => {
    const config = params.config as Record<string, unknown>;
    assertSingleEndpointParams(config);
    const pposFields = [
      "p2_data", "p2_data_ctrl", "p3_n", "p3_alloc_ratio", "p3_alpha",
    ] as const;
    if (config.study_type !== "confirmatory" && pposFields.some(
      (field) => config[field] !== undefined,
    )) {
      throw new InvalidRequestToolError(
        "PPOS data and confirmatory-stage settings require study_type='confirmatory'",
      );
    }
    const p2KeysByEndpoint: Record<string, readonly string[]> = {
      binary: ["n", "x"],
      continuous: ["n", "s2", "x_bar"],
      tte: ["events", "person_time"],
      incidence_rate: ["count", "exposure"],
    };
    const expectedP2Keys = p2KeysByEndpoint[String(config.endpoint_type)];
    for (const field of ["p2_data", "p2_data_ctrl"] as const) {
      const data = config[field] as Record<string, unknown> | undefined;
      if (data === undefined) continue;
      const keys = Object.keys(data).sort();
      if (keys.length !== expectedP2Keys.length ||
          keys.some((key, index) => key !== expectedP2Keys[index])) {
        throw new InvalidRequestToolError(
          `${field} does not match endpoint_type='${String(config.endpoint_type)}'`,
        );
      }
      if (config.endpoint_type === "binary" && Number(data.x) > Number(data.n)) {
        throw new InvalidRequestToolError(
          `${field} binary responders cannot exceed its sample size`,
        );
      }
    }
    const hasP2Data = config.p2_data !== undefined;
    const hasP2ControlData = config.p2_data_ctrl !== undefined;
    if (config.design === "controlled" && hasP2Data !== hasP2ControlData) {
      throw new InvalidRequestToolError(
        "controlled confirmatory PPOS requires paired p2_data and p2_data_ctrl",
      );
    }
    const p3Fields = ["p3_n", "p3_alloc_ratio", "p3_alpha"] as const;
    if (!hasP2Data && p3Fields.some((field) => config[field] !== undefined)) {
      throw new InvalidRequestToolError(
        "confirmatory-stage PPOS settings require p2_data",
      );
    }
    if (config.design !== "controlled" && config.p3_alloc_ratio !== undefined) {
      throw new InvalidRequestToolError(
        "p3_alloc_ratio is accepted only for controlled designs",
      );
    }
    if (params.n_oc !== undefined) {
      if (config.design === "controlled" && typeof params.n_oc === "number") {
        throw new InvalidRequestToolError(
          "controlled OC requires n_oc={n_trt,n_ctrl}; scalar n_oc is single-arm only",
        );
      }
      const units = typeof params.n_oc === "number"
        ? params.n_oc
        : Number(params.n_oc.n_trt) + Number(params.n_oc.n_ctrl);
      const replicates = Number(params.B_oc ?? 5000);
      if (units * replicates > MAX_SIMULATED_UNITS) {
        throw new InvalidRequestToolError(
          `requested OC workload=${units * replicates} (= ${units} units × ` +
          `${replicates} replicates) exceeds the ${MAX_SIMULATED_UNITS} limit; ` +
          "reduce B_oc or n_oc",
        );
      }
      if (config.design === "single_arm" && typeof params.n_oc === "object") {
        throw new InvalidRequestToolError("single-arm OC requires scalar n_oc");
      }
    }
    return runTool("simulate_design", params, signal);
  }
);

// --- Tool: master_simulate ---

const PLATFORM_REQUIRED_MASTER_FIELDS = [
  "n_periods", "n_per_period", "arms_schedule",
] as const;
const PLATFORM_ONLY_MASTER_FIELDS = [
  ...PLATFORM_REQUIRED_MASTER_FIELDS,
  "shared_control", "ncc_method", "ncc_weight_decay", "rar_enabled",
  "rar_burn_in", "rar_min_alloc", "interim_frequency", "futility_threshold",
] as const;
const BASKET_ONLY_MASTER_FIELDS = [
  "n_per_subgroup", "borrowing_method", "phase", "n_interims",
  "n_per_interim", "go_threshold", "nogo_threshold", "tau_prior",
  "homogeneity_prior", "response_prior", "cbhm_a", "cbhm_b",
  "ia_pruning_alpha", "chen_strategy",
] as const;
const UMBRELLA_ONLY_MASTER_FIELDS = [
  "umbrella_method", "n_arms", "n_stages", "n_per_arm_stage",
  "futility_boundaries", "n_drop_per_stage", "rar_gamma",
  "selection_rule", "power_type",
] as const;
const MASTER_ENDPOINT_SCOPED_FIELDS = [
  "sd", "accrual_time", "followup_time", "exposure_time",
  "tte_method", "rate_method",
] as const;
const MASTER_ENDPOINT_ALLOWED_FIELDS: Record<string, ReadonlySet<string>> = {
  binary: new Set(),
  continuous: new Set(["sd"]),
  tte: new Set(["accrual_time", "followup_time", "tte_method"]),
  incidence_rate: new Set(["exposure_time", "rate_method"]),
};
const PLATFORM_INTERIM_UNAVAILABLE_MESSAGE =
  "interim_frequency/futility_threshold are unavailable for this endpoint and " +
  "NCC method because no consistent interim model is implemented";

registerStrictTool(
  "master_simulate",
  "Run a multi-arm adaptive design simulation: basket (with borrowing), umbrella (MAMS/DTL/BAR), or platform (with NCC adjustment). Returns OC tables, FWER, subgroup decisions, and output file paths.",
  {
    ...verificationMeta,
    // Every key below is part of the implemented or explicitly rejected public
    // contract. Unsupported controls such as effect_threshold, overdispersion,
    // and rar_eta stay unexposed, so .strict() rejects them at the boundary
    // instead of allowing a run under a capability label the engine cannot honor.
    config: z.object({
      master_design_type: z.enum(["basket", "umbrella", "platform"]),
      endpoint_type: z.enum(["binary", "continuous", "tte", "incidence_rate"]),
      n_subgroups: z.number().int().min(2).max(50),
      null_params: z.union([z.number(), z.array(z.number()).min(1).max(50)]),
      alt_params: z.array(z.number()).min(1).max(50),
      n_per_subgroup: z.number().int().min(1).max(100000).optional(),
      alpha: z.number().gt(0).lt(1).optional()
        .describe("One-sided significance level used by the selected master design"),
      n_sims: z.number().int().min(1).max(10000).optional(),
      seed: z.number().int().min(0).max(2147483647).optional().describe("Integer RNG seed (default 42); echoed in the result"),
      label: z.string().max(MAX_LABEL_CHARS).optional()
        .describe("Run label used in output file names (default: design type + timestamp)"),
      // Basket-specific
      borrowing_method: z.enum([
        "none", "complete", "simon_two_stage", "simons_bayesian", "chen_confirmatory",
        "wathen_sti", "cbhm", "full_bhm", "snti", "sep_gibbs",
      ]).optional()
        .describe("Basket only (rejected for umbrella/platform): analysis method. simon_two_stage is a non-borrowing phase2 binary minimax design with one interim and a derived maximum n; phase2 binary otherwise defaults to simons_bayesian only for common null/alternative parameters, and phase3 defaults to chen_confirmatory. wathen_sti, snti, and sep_gibbs are reserved and rejected for every endpoint."),
      phase: z.enum(["phase2", "phase3"]).optional()
        .describe("Basket only (rejected for umbrella/platform): selects the alpha preset (phase2: 0.10, phase3: 0.025 one-sided) and the default borrowing method"),
      n_interims: z.number().int().min(0).max(20).optional()
        .describe("Basket: number of interim analyses (default 1)"),
      n_per_interim: z.number().int().min(1).max(100000).optional()
        .describe("Basket: participants per subgroup at each interim; the final look receives the exact remainder"),
      go_threshold: z.number().gt(0).lt(1).optional()
        .describe("Basket: posterior probability threshold for Go (default 0.90)"),
      nogo_threshold: z.number().gt(0).lt(1).optional()
        .describe("Basket: posterior probability threshold for No-Go/futility (default 0.10)"),
      tau_prior: z.object({
        type: z.literal("half_normal"),
        params: z.object({ scale: z.number().positive() }).strict(),
      }).strict().optional()
        .describe("Basket full_bhm: prior on BHM heterogeneity tau, e.g. {type:'half_normal', params:{scale:1}} (the default). cbhm calibrates this prior scale from cbhm_a/cbhm_b; tau remains estimated."),
      homogeneity_prior: z.number().gt(0).lt(1).optional()
        .describe("Basket simons_bayesian: prior probability of homogeneity across subgroups (default 0.50)"),
      response_prior: z.number().gt(0).lt(1).optional()
        .describe("Basket simons_bayesian: prior probability that treatment is effective (default 0.50)"),
      cbhm_a: z.number().gt(0).optional()
        .describe("Basket cbhm: calibration parameter a (default 0.5)"),
      cbhm_b: z.number().gt(0).optional()
        .describe("Basket cbhm: calibration parameter b (default 0.5)"),
      soc_data: z.object({ means: z.array(z.number()).min(1).max(50) }).strict().optional()
        .describe("Reserved for the disabled wathen_sti prototype; any supplied value is rejected"),
      ia_pruning_alpha: z.number().gt(0).lt(1).optional()
        .describe("Basket chen_confirmatory: one-sided interim pruning alpha (default 0.10)"),
      chen_strategy: z.enum(["d1", "d2", "d3"]).optional()
        .describe("Basket chen_confirmatory decision strategy: d1 survivors complete their planned per-basket maximum; d2 the planned pooled final total is redistributed over survivors; d3 the planned post-IA enrollment is redistributed over survivors (default d1)"),
      // Umbrella-specific
      umbrella_method: z.enum(["mams", "drop_the_losers", "bayesian_adaptive_randomization"]).optional()
        .describe("Umbrella only (rejected for basket/platform): design method (default mams)"),
      n_arms: z.number().int().min(2).max(50).optional(),
      n_stages: z.number().int().min(1).max(20).optional(),
      n_per_arm_stage: z.number().int().min(2).max(100000).optional(),
      futility_boundaries: z.array(z.number()).min(1).max(20).optional()
        .describe("Umbrella: stage-wise futility boundaries, length n_stages (default: calculated)"),
      n_drop_per_stage: z.array(z.number().int().nonnegative()).max(19).optional()
        .describe("Umbrella drop_the_losers: number of arms to drop per stage, length n_stages-1 (default: drop worst)"),
      rar_gamma: z.number().nonnegative().optional()
        .describe("Umbrella BAR: allocation-aggressiveness exponent, held CONSTANT across stages. The R default is the stage-increasing FUNCTION j/J, which cannot be expressed in JSON — omit this to keep it."),
      selection_rule: z.enum(["rank_best", "threshold"]).optional()
        .describe("Derived automatically (rank_best for drop_the_losers, threshold otherwise) — the engines' selection logic is fixed. An explicit value that differs from the derived default is REJECTED by the R layer; omit it."),
      power_type: z.enum(["one_minimum"]).optional()
        .describe("Only 'one_minimum' exists — power is always reported per-arm / at-least-one-minimum. Any other value is REJECTED by the R layer."),
      // Platform-specific
      n_periods: z.number().int().min(2).max(100).optional()
        .describe("Platform designs only: required number of enrollment periods"),
      n_per_period: z.number().int().min(1).max(100000).optional()
        .describe("Platform designs only: required participants per period"),
      arms_schedule: z.object({
        enter: z.array(z.number().int()).min(2).max(50).describe("Period each arm enters (length = n_subgroups)"),
        leave: z.array(z.number().int()).min(2).max(50).describe("Period each arm leaves (length = n_subgroups, each >= enter)"),
      }).strict().optional().describe("Platform designs only: when each arm is active. Required for master_design_type='platform'."),
      shared_control: z.literal(true).optional()
        .describe("Platform simulations always share control (concurrency governed by ncc_method); only true is accepted — false is REJECTED by the R layer"),
      ncc_method: z.enum(["none", "pooled", "regression", "time_machine"]).optional()
        .describe("Platform only (rejected for basket/umbrella): non-concurrent-control adjustment method (default regression)"),
      ncc_weight_decay: z.number().gt(0).lt(1).optional()
        .describe("Platform time_machine: temporal decay parameter (default 0.90)"),
      rar_enabled: z.boolean().optional()
        .describe("Platform: enable response-adaptive randomization (default false)"),
      rar_burn_in: z.number().int().min(0).optional()
        .describe("Platform RAR: participants enrolled before RAR starts (default 100)"),
      rar_min_alloc: z.number().gt(0).lt(1).optional()
        .describe("Platform RAR: minimum allocation fraction per arm (default 0.10)"),
      interim_frequency: z.number().int().min(1).optional()
        .describe("Platform binary/continuous with ncc_method='none' only: interim analysis every N periods (default 1)"),
      futility_threshold: z.number().gt(0).lt(1).optional()
        .describe("Platform binary/continuous with ncc_method='none' only: posterior probability threshold for early futility stopping (default 0.05). Early efficacy stopping is not exposed."),
      // Endpoint-specific
      sd: z.number().positive().optional(),
      accrual_time: z.number().positive().optional(),
      followup_time: z.number().positive().optional(),
      exposure_time: z.number().positive().optional(),
      tte_method: tteMethod,
      rate_method: rateMethod,
      fwer_control: fwerControl,
    }).strict(),
    output_dir: strictPath.optional(),
  },
  async (params, signal) => {
    const identityDomain = domainParams(params);
    const domain = { ...identityDomain };
    const cfgForBudget = domain.config as Record<string, unknown>;
    const designType = String(cfgForBudget.master_design_type);
    const nSubgroups = Number(cfgForBudget.n_subgroups);
    if (cfgForBudget.soc_data !== undefined) {
      throw new InvalidRequestToolError(
        "soc_data is reserved for a disabled prototype and is not accepted",
      );
    }
    if (designType !== "basket" && BASKET_ONLY_MASTER_FIELDS.some(
      (field) => cfgForBudget[field] !== undefined,
    )) {
      throw new InvalidRequestToolError(
        "basket-only configuration fields require master_design_type='basket'",
      );
    }
    if (designType !== "umbrella" && UMBRELLA_ONLY_MASTER_FIELDS.some(
      (field) => cfgForBudget[field] !== undefined,
    )) {
      throw new InvalidRequestToolError(
        "umbrella-only configuration fields require master_design_type='umbrella'",
      );
    }
    const borrowingMethod = String(cfgForBudget.borrowing_method ?? "");
    if (cfgForBudget.tau_prior !== undefined && borrowingMethod !== "full_bhm") {
      throw new InvalidRequestToolError(
        "tau_prior requires borrowing_method='full_bhm'",
      );
    }
    if ((cfgForBudget.cbhm_a !== undefined || cfgForBudget.cbhm_b !== undefined) &&
        borrowingMethod !== "cbhm") {
      throw new InvalidRequestToolError(
        "cbhm calibration fields require borrowing_method='cbhm'",
      );
    }
    if ((cfgForBudget.homogeneity_prior !== undefined ||
         cfgForBudget.response_prior !== undefined) &&
        borrowingMethod !== "simons_bayesian") {
      throw new InvalidRequestToolError(
        "Simon's prior fields require borrowing_method='simons_bayesian'",
      );
    }
    if ((cfgForBudget.ia_pruning_alpha !== undefined ||
         cfgForBudget.chen_strategy !== undefined) &&
        borrowingMethod !== "chen_confirmatory") {
      throw new InvalidRequestToolError(
        "Chen decision fields require borrowing_method='chen_confirmatory'",
      );
    }
    const umbrellaMethod = String(cfgForBudget.umbrella_method ?? "mams");
    if (designType === "umbrella" && cfgForBudget.n_arms !== undefined &&
        Number(cfgForBudget.n_arms) !== nSubgroups) {
      throw new InvalidRequestToolError(
        "umbrella n_arms must equal n_subgroups",
      );
    }
    const derivedSelectionRule = umbrellaMethod === "drop_the_losers"
      ? "rank_best"
      : "threshold";
    if (designType === "umbrella" && cfgForBudget.selection_rule !== undefined &&
        cfgForBudget.selection_rule !== derivedSelectionRule) {
      throw new InvalidRequestToolError(
        "selection_rule must match the selected umbrella_method",
      );
    }
    if (cfgForBudget.n_drop_per_stage !== undefined &&
        umbrellaMethod !== "drop_the_losers") {
      throw new InvalidRequestToolError(
        "n_drop_per_stage requires umbrella_method='drop_the_losers'",
      );
    }
    if (cfgForBudget.rar_gamma !== undefined &&
        umbrellaMethod !== "bayesian_adaptive_randomization") {
      throw new InvalidRequestToolError(
        "rar_gamma requires umbrella_method='bayesian_adaptive_randomization'",
      );
    }
    if (cfgForBudget.ncc_weight_decay !== undefined &&
        cfgForBudget.ncc_method !== "time_machine") {
      throw new InvalidRequestToolError(
        "ncc_weight_decay requires ncc_method='time_machine'",
      );
    }
    if ((cfgForBudget.rar_burn_in !== undefined ||
         cfgForBudget.rar_min_alloc !== undefined) &&
        cfgForBudget.rar_enabled !== true) {
      throw new InvalidRequestToolError(
        "platform RAR settings require rar_enabled=true",
      );
    }
    const allowedEndpointFields = MASTER_ENDPOINT_ALLOWED_FIELDS[
      String(cfgForBudget.endpoint_type)
    ];
    if (allowedEndpointFields && MASTER_ENDPOINT_SCOPED_FIELDS.some(
      (field) => cfgForBudget[field] !== undefined &&
        !allowedEndpointFields.has(field),
    )) {
      throw new InvalidRequestToolError(
        "master configuration contains fields for a different endpoint_type",
      );
    }
    const altParams = cfgForBudget.alt_params as number[];
    const nullParams = cfgForBudget.null_params;
    if (altParams.length !== nSubgroups ||
        (Array.isArray(nullParams) && nullParams.length !== nSubgroups)) {
      throw new InvalidRequestToolError(
        "master parameter arrays must contain exactly n_subgroups values",
      );
    }
    const endpointType = String(cfgForBudget.endpoint_type);
    if (endpointType === "continuous" &&
        !(typeof cfgForBudget.sd === "number" && cfgForBudget.sd > 0)) {
      throw new InvalidRequestToolError("continuous master designs require a positive scalar sd");
    }
    if (endpointType === "tte" &&
        !(Number(cfgForBudget.accrual_time) > 0 &&
          Number(cfgForBudget.followup_time) > 0)) {
      throw new InvalidRequestToolError(
        "time-to-event master designs require positive accrual_time and followup_time",
      );
    }
    if (endpointType === "incidence_rate" &&
        !(Number(cfgForBudget.exposure_time) > 0)) {
      throw new InvalidRequestToolError(
        "incidence-rate master designs require a positive exposure_time",
      );
    }
    const nullValues = Array.isArray(nullParams)
      ? nullParams.map(Number)
      : Array<number>(nSubgroups).fill(Number(nullParams));
    const alternativeValues = altParams.map(Number);
    const directionIsValid = alternativeValues.every((alternative, index) => {
      const nullValue = nullValues[index];
      if (endpointType === "binary") {
        return nullValue >= 0 && nullValue <= 1 &&
          alternative >= 0 && alternative <= 1 && alternative > nullValue;
      }
      if (endpointType === "continuous") return alternative > nullValue;
      if (endpointType === "tte" || endpointType === "incidence_rate") {
        return nullValue > 0 && alternative > 0 && alternative < nullValue;
      }
      return false;
    });
    if (!directionIsValid) {
      throw new InvalidRequestToolError(
        "master null and alternative parameters violate endpoint bounds or direction",
      );
    }
    const nSims = typeof cfgForBudget.n_sims === "number"
      ? cfgForBudget.n_sims : 10000;
    let platformBudget: { nPeriods: number; nPerPeriod: number } | undefined;
    if (designType === "platform") {
      if (PLATFORM_REQUIRED_MASTER_FIELDS.some(
        (field) => cfgForBudget[field] === undefined,
      )) {
        throw new InvalidRequestToolError(
          "platform designs require n_periods, n_per_period, and arms_schedule",
        );
      }
      const nPeriods = cfgForBudget.n_periods as number;
      const nPerPeriod = cfgForBudget.n_per_period as number;
      const schedule = cfgForBudget.arms_schedule as {
        enter: number[];
        leave: number[];
      };
      if (schedule.enter.length !== nSubgroups ||
          schedule.leave.length !== nSubgroups) {
        throw new InvalidRequestToolError(
          "arms_schedule enter and leave must each contain exactly n_subgroups values",
        );
      }
      const scheduleIsValid = schedule.enter.every((enter, index) => {
        const leave = schedule.leave[index];
        return Number.isInteger(enter) && Number.isInteger(leave) &&
          enter >= 1 && enter <= leave && leave <= nPeriods;
      });
      if (!scheduleIsValid) {
        throw new InvalidRequestToolError(
          "arms_schedule must satisfy 1 <= enter[i] <= leave[i] <= n_periods for every subgroup",
        );
      }
      const hasInterimSettings = cfgForBudget.interim_frequency !== undefined ||
        cfgForBudget.futility_threshold !== undefined;
      const interimSettingsSupported =
        ["binary", "continuous"].includes(String(cfgForBudget.endpoint_type)) &&
        cfgForBudget.ncc_method === "none";
      if (hasInterimSettings && !interimSettingsSupported) {
        throw new InvalidRequestToolError(PLATFORM_INTERIM_UNAVAILABLE_MESSAGE);
      }
      platformBudget = { nPeriods, nPerPeriod };
    } else if (PLATFORM_ONLY_MASTER_FIELDS.some(
      (field) => cfgForBudget[field] !== undefined,
    )) {
      throw new InvalidRequestToolError(
        "platform-only configuration fields require master_design_type='platform'",
      );
    }
    const nPerSubgroup = typeof cfgForBudget.n_per_subgroup === "number"
      ? cfgForBudget.n_per_subgroup : 25;
    const nPerArmStage = typeof cfgForBudget.n_per_arm_stage === "number"
      ? cfgForBudget.n_per_arm_stage : 9;
    const nStages = typeof cfgForBudget.n_stages === "number"
      ? cfgForBudget.n_stages : 2;
    const workload = designType === "basket"
      ? nSims * nSubgroups * nPerSubgroup
      : designType === "umbrella"
        ? nSims * (nSubgroups + 1) * nPerArmStage * nStages
        : nSims * platformBudget!.nPeriods * platformBudget!.nPerPeriod;
    if (!Number.isFinite(workload) || workload > MAX_SIMULATED_UNITS) {
      throw new InvalidRequestToolError(
        `requested ${designType} workload=${workload} simulated units exceeds the ` +
        `${MAX_SIMULATED_UNITS} limit (n_sims=${nSims}, n_subgroups=${nSubgroups}); ` +
        "reduce n_sims or the per-subgroup/per-stage/per-period sample size",
      );
    }
    if (designType === "basket" && Number(cfgForBudget.nogo_threshold ?? 0.10) >= Number(cfgForBudget.go_threshold ?? 0.90)) {
      throw new InvalidRequestToolError("nogo_threshold must be below go_threshold");
    }
    if (["snti", "sep_gibbs", "wathen_sti"].includes(borrowingMethod)) {
      throw new InvalidRequestToolError(
        `borrowing_method='${borrowingMethod}' is disabled because the available treatment-only likelihood does not identify a treatment effect`,
      );
    }
    if (designType === "basket") {
      const nulls = Array.isArray(cfgForBudget.null_params)
        ? cfgForBudget.null_params.map(Number) : [Number(cfgForBudget.null_params)];
      const alts = Array.isArray(cfgForBudget.alt_params)
        ? cfgForBudget.alt_params.map(Number) : [Number(cfgForBudget.alt_params)];
      const tolerance = Math.sqrt(Number.EPSILON);
      const heterogeneous = (values: number[]) =>
        values.some((value) => Math.abs(value - values[0]) > tolerance);
      if (borrowingMethod === "complete" && heterogeneous(nulls)) {
        throw new InvalidRequestToolError(
          "complete borrowing requires identical null_params across subgroups",
        );
      }
      if (borrowingMethod === "simons_bayesian" &&
          (heterogeneous(nulls) || heterogeneous(alts))) {
        throw new InvalidRequestToolError(
          "simons_bayesian requires identical null_params and alt_params across subgroups",
        );
      }
    }
    if (designType === "basket" && cfgForBudget.n_per_interim !== undefined &&
        Number(cfgForBudget.n_per_interim) * Number(cfgForBudget.n_interims ?? 1) >=
          Number(cfgForBudget.n_per_subgroup ?? 25)) {
      throw new InvalidRequestToolError(
        "n_per_interim * n_interims must be below n_per_subgroup so the final look has participants",
      );
    }
    if (designType === "umbrella") {
      const stages = Number(cfgForBudget.n_stages ?? 2);
      const arms = Number(cfgForBudget.n_arms ?? nSubgroups);
      const boundaries = cfgForBudget.futility_boundaries as number[] | undefined;
      if (boundaries && boundaries.length !== stages) {
        throw new InvalidRequestToolError(
          "futility_boundaries must contain exactly one value per stage",
        );
      }
      if (boundaries) {
        const alpha = Number(cfgForBudget.alpha ?? 0.025);
        // R performs the authoritative normal-quantile check; this structural
        // check keeps obviously malformed arrays from reaching the engine.
        if (!Number.isFinite(boundaries[stages - 1]) || alpha <= 0 || arms < 2) {
          throw new InvalidRequestToolError("invalid final boundary, alpha, or arm count");
        }
      }
      const drops = cfgForBudget.n_drop_per_stage as number[] | undefined;
      if (drops && (drops.length !== Math.max(0, stages - 1) ||
          drops.reduce((sum, value) => sum + value, 0) >= arms)) {
        throw new InvalidRequestToolError(
          "n_drop_per_stage must cover every interim and leave at least one arm",
        );
      }
    }
    const executionFingerprint = await engineFingerprint(signal);
    const regression = await regressionAttestation(executionFingerprint, signal);
    const managedOutput = await persistentOutputDir(
      typeof identityDomain.output_dir === "string" ? identityDomain.output_dir : undefined,
      "master-",
    );
    domain.output_dir = managedOutput;
    let completed = false;
    try {
      const result = await callR("master_simulate", domain, signal);
      const cfg = domain.config as Record<string, unknown>;
      const nSims = typeof cfg.n_sims === "number" ? cfg.n_sims : 10000;
      const replayRequired = nSims <= 1000;
      let replay: unknown;
      let replayDir: string | undefined;
      if (replayRequired) {
        replayDir = await mkdtemp(join(tmpdir(), "expdesign-master-replay-"));
        try {
          replay = await callR(
            "master_simulate", { ...domain, output_dir: replayDir }, signal,
          );
        } finally {
          await rm(replayDir, { recursive: true, force: true });
        }
      }
      await assertEngineUnchanged(executionFingerprint, "the analysis was running", signal);
      const provenance = buildProvenance(
        result, params, undefined, executionFingerprint,
      );
      const verification = await preverify(
        "master_simulate", identityDomain, result, replay, replayRequired,
        replayRequired ? [] : ["same-seed replay skipped above 1000 simulations"],
        publicProvenance(provenance), regression, signal,
      );
      await assertEngineUnchanged(executionFingerprint, "the verifier was running", signal);
      const response = toolResponse(result, params, verification, provenance);
      completed = verification.presentable;
      return response;
    } finally {
      if (completed) await releaseManagedArtifactDir(managedOutput);
      else await abortManagedArtifactDir(managedOutput);
    }
  }
);

// --- Tool: indirect_compare ---

const BUCHER_TOP_LEVEL_FIELDS = new Set(["method", "comparisons"]);

function assertIndirectComparisonParams(params: Record<string, unknown>): void {
  if (params.method === "bucher") {
    if (!Array.isArray(params.comparisons)) {
      throw new InvalidRequestToolError("bucher requires comparisons");
    }
    if (Object.keys(params).some((key) => !BUCHER_TOP_LEVEL_FIELDS.has(key))) {
      throw new InvalidRequestToolError(
        "bucher does not accept MAIC-only settings",
      );
    }
    return;
  }

  if (params.method !== "maic") {
    throw new InvalidRequestToolError("unsupported indirect-comparison method");
  }
  if (params.comparisons !== undefined) {
    throw new InvalidRequestToolError("maic does not accept Bucher comparisons");
  }
  for (const field of ["ipd_file", "targets_file", "treatment_arm"] as const) {
    if (typeof params[field] !== "string" || !params[field]) {
      throw new InvalidRequestToolError(`maic requires ${field}`);
    }
  }

  const endpoint = String(params.maic_endpoint_type ?? "binary");
  const has = (field: string): boolean => params[field] !== undefined;
  if (endpoint === "binary" || endpoint === "continuous") {
    if (["event_col", "time_col", "status_col", "tte_method"].some(has)) {
      throw new InvalidRequestToolError(
        "binary/continuous MAIC does not accept rate or TTE column settings",
      );
    }
  } else if (endpoint === "rate") {
    if (!has("event_col") || !has("time_col")) {
      throw new InvalidRequestToolError(
        "rate MAIC requires event_col and time_col",
      );
    }
    if (["outcome_col", "status_col", "tte_method"].some(has)) {
      throw new InvalidRequestToolError(
        "rate MAIC does not accept outcome, status, or TTE-model settings",
      );
    }
  } else if (endpoint === "tte") {
    if (!has("time_col") || !has("status_col")) {
      throw new InvalidRequestToolError(
        "TTE MAIC requires time_col and status_col",
      );
    }
    if (["outcome_col", "event_col"].some(has)) {
      throw new InvalidRequestToolError(
        "TTE MAIC does not accept outcome or rate-event columns",
      );
    }
    const tteMethod = String(params.tte_method ?? "cox");
    if (tteMethod === "cox" &&
        (has("bootstrap_replicates") || has("bootstrap_seed"))) {
      throw new InvalidRequestToolError(
        "Cox MAIC does not accept non-Cox bootstrap settings",
      );
    }
  }
}

registerStrictTool(
  "indirect_compare",
  "Run an indirect treatment comparison: Bucher method for published aggregate data, or MAIC for IPD-vs-published comparisons. Returns effect estimates, SEs, CIs, and assumption audits.",
  {
    ...verificationMeta,
    method: z.enum(["bucher", "maic"]),
    comparisons: z.array(z.object({
      estimate_ab: z.number(),
      se_ab: z.number().positive(),
      estimate_cb: z.number(),
      se_cb: z.number().positive(),
      treatment_a: z.string().max(MAX_LABEL_CHARS),
      treatment_c: z.string().max(MAX_LABEL_CHARS),
      common_comparator: z.string().max(MAX_LABEL_CHARS),
      effect_measure: z.string().max(MAX_COLUMN_NAME_CHARS).optional(),
      analysis_scale: z.string().max(MAX_COLUMN_NAME_CHARS).optional(),
      alpha: z.number().gt(0).lt(1).optional()
        .describe("Two-sided alpha for the confidence interval"),
    }).strict()).min(1).max(1000).optional(),
    ipd_file: strictPath.optional(),
    targets_file: strictPath.optional(),
    output_dir: strictPath.optional(),
    treatment_arm: z.string().min(1).max(MAX_LABEL_CHARS).optional().describe("MAIC: active-arm label in the IPD arm column (required for method='maic')"),
    comparator_arm: z.string().min(1).max(MAX_LABEL_CHARS).optional().describe("MAIC: comparator-arm label in the IPD; omit for unanchored"),
    arm_col: z.string().min(1).max(MAX_COLUMN_NAME_CHARS).optional().describe("MAIC: name of the arm/treatment column in the IPD CSV (default 'arm')"),
    maic_endpoint_type: z.enum(["binary", "continuous", "rate", "tte"]).optional().describe("MAIC: outcome type in the IPD (default 'binary')"),
    outcome_col: z.string().min(1).max(MAX_COLUMN_NAME_CHARS).optional().describe("MAIC: outcome column (binary/continuous) in the IPD CSV (default 'response')"),
    event_col: z.string().min(1).max(MAX_COLUMN_NAME_CHARS).optional().describe("MAIC: event-count column in the IPD CSV (required for maic_endpoint_type='rate')"),
    time_col: z.string().min(1).max(MAX_COLUMN_NAME_CHARS).optional().describe("MAIC: person-time/follow-up column in the IPD CSV (required for 'rate' and 'tte')"),
    status_col: z.string().min(1).max(MAX_COLUMN_NAME_CHARS).optional().describe("MAIC: event-status (0/1) column in the IPD CSV (required for maic_endpoint_type='tte')"),
    covariates: z.array(z.string().min(1).max(MAX_COLUMN_NAME_CHARS)).min(1).max(MAX_COVARIATES).optional()
      .describe("MAIC: covariate columns to weight on (default: every column in the targets CSV)"),
    tte_method: z.enum(["cox", "exponential"]).optional()
      .describe("MAIC tte analysis model: 'cox' (default, weighted Cox PH) or 'exponential' (base-R person-time proxy)"),
    measure: z.string().max(MAX_COLUMN_NAME_CHARS).optional().describe("MAIC: effect measure, e.g. log_odds_ratio / log_hazard_ratio / log_rate_ratio / mean_difference"),
    alpha: z.number().gt(0).lt(1).optional()
      .describe("Two-sided alpha for the confidence interval"),
    bootstrap_replicates: z.number().int().min(50).max(5000).optional()
      .describe("MAIC non-Cox: stratified bootstrap replicates with weights refit (default 200)"),
    bootstrap_seed: z.number().int().min(0).max(2147483647).optional()
      .describe("MAIC non-Cox bootstrap seed (default 42)"),
    export_row_weights: z.boolean().optional()
      .describe("MAIC: explicitly export row index, arm, and weight; false by default"),
  },
  async (params, signal) => {
    const identityDomain = domainParams(params);
    assertIndirectComparisonParams(identityDomain);
    const domain = { ...identityDomain };
    let managedOutput: string | undefined;
    let inputSnapshot: string | undefined;
    let boundInputHashes: Record<string, string> | undefined;
    let completed = false;
    try {
      if (domain.method === "maic") {
        const ipdFile = domain.ipd_file as string;
        const targetsFile = domain.targets_file as string;
        const [ipdBytes, targetsBytes] = await Promise.all([
          readAllowedFile(ipdFile),
          readAllowedFile(targetsFile),
        ]);
        const ipdRows = boundedCsvRows(ipdBytes, MAX_IPD_ROWS, "IPD CSV");
        boundedCsvRows(targetsBytes, MAX_TARGET_ROWS, "target CSV");
        const endpoint = String(domain.maic_endpoint_type ?? "binary");
        const usesBootstrap = endpoint !== "tte" || domain.tte_method === "exponential";
        if (usesBootstrap) {
          const replicates = Number(domain.bootstrap_replicates ?? 200);
          const workload = ipdRows * replicates;
          if (workload > MAX_MAIC_BOOTSTRAP_ROW_OPERATIONS) {
            throw new InvalidRequestToolError(
              `requested MAIC bootstrap workload=${workload} row-replicates exceeds the ` +
              `${MAX_MAIC_BOOTSTRAP_ROW_OPERATIONS} limit; reduce the IPD rows or ` +
              "bootstrap_replicates",
            );
          }
        }

        // Execute against immutable per-call snapshots, and bind provenance to
        // those same bytes. This closes the hash/use race if a caller edits a
        // source CSV while R is running.
        inputSnapshot = await mkdtemp(join(tmpdir(), "expdesign-maic-input-"));
        const ipdSnapshot = join(inputSnapshot, "ipd.csv");
        const targetsSnapshot = join(inputSnapshot, "targets.csv");
        await writeFile(ipdSnapshot, ipdBytes, { mode: 0o600 });
        await writeFile(targetsSnapshot, targetsBytes, { mode: 0o600 });
        boundInputHashes = {
          ipd_file: createHash("sha256").update(ipdBytes).digest("hex"),
          targets_file: createHash("sha256").update(targetsBytes).digest("hex"),
        };
        domain.ipd_file = ipdSnapshot;
        domain.targets_file = targetsSnapshot;
        managedOutput = await persistentOutputDir(
          typeof domain.output_dir === "string" ? domain.output_dir : undefined,
          "maic-",
        );
        domain.output_dir = managedOutput;
      }
      const executionFingerprint = await engineFingerprint(signal);
      const regression = await regressionAttestation(executionFingerprint, signal);
      const result = await callR("indirect_compare", domain, signal);
      await assertEngineUnchanged(executionFingerprint, "the analysis was running", signal);
      const provenance = buildProvenance(
        result, params, boundInputHashes, executionFingerprint,
      );
      const verification = await preverify(
        "indirect_compare", identityDomain, result, undefined, false, [],
        publicProvenance(provenance),
        regression,
        signal,
      );
      await assertEngineUnchanged(executionFingerprint, "the verifier was running", signal);
      const response = toolResponse(result, params, verification, provenance);
      completed = verification.presentable;
      return response;
    } finally {
      if (managedOutput) {
        if (completed) await releaseManagedArtifactDir(managedOutput);
        else await abortManagedArtifactDir(managedOutput);
      }
      if (inputSnapshot) await rm(inputSnapshot, { recursive: true, force: true });
    }
  }
);

// --- Tool: meta_analyze ---

const metaStudySchema = z.object({
  responders: z.number().optional(), total: z.number().optional(), continuity: z.number().positive().optional(),
  events_t: z.number().optional(), total_t: z.number().optional(),
  events_c: z.number().optional(), total_c: z.number().optional(),
  measure: z.enum([
    "risk_difference", "log_risk_ratio", "log_odds_ratio",
    "mean_difference", "standardized_mean_difference",
  ]).optional(),
  mean: z.number().optional(), sd: z.number().positive().optional(), n: z.number().positive().optional(),
  mean_t: z.number().optional(), sd_t: z.number().positive().optional(), n_t: z.number().positive().optional(),
  mean_c: z.number().optional(), sd_c: z.number().positive().optional(), n_c: z.number().positive().optional(),
  hr: z.number().positive().optional(), ci_lower: z.number().positive().optional(),
  ci_upper: z.number().positive().optional(), log_hr: z.number().optional(), se: z.number().positive().optional(),
  source_ci_level: z.number().gt(0).lt(1).optional(),
  events: z.number().nonnegative().optional(), person_time: z.number().positive().optional(),
  person_time_t: z.number().positive().optional(), person_time_c: z.number().positive().optional(),
}).strict();

const META_STUDY_FIELDS_BY_ENDPOINT: Record<string, ReadonlySet<string>> = {
  binary_single: new Set(["responders", "total", "continuity"]),
  binary_comparative: new Set([
    "events_t", "total_t", "events_c", "total_c", "continuity", "measure",
  ]),
  continuous_single: new Set(["mean", "sd", "n"]),
  continuous_comparative: new Set([
    "mean_t", "sd_t", "n_t", "mean_c", "sd_c", "n_c", "measure",
  ]),
  time_to_event: new Set([
    "hr", "ci_lower", "ci_upper", "log_hr", "se", "source_ci_level",
  ]),
  incidence_single: new Set(["events", "person_time"]),
  incidence_comparative: new Set([
    "events_t", "person_time_t", "events_c", "person_time_c",
  ]),
};
const BINARY_META_MEASURES = new Set([
  "risk_difference", "log_risk_ratio", "log_odds_ratio",
]);
const CONTINUOUS_META_MEASURES = new Set([
  "mean_difference", "standardized_mean_difference",
]);

function assertMetaStudyFieldScope(
  endpoint: string,
  studies: ReadonlyArray<Record<string, unknown>>,
): void {
  const allowed = META_STUDY_FIELDS_BY_ENDPOINT[endpoint];
  if (!allowed) return;
  for (const study of studies) {
    if (Object.keys(study).some((field) => !allowed.has(field))) {
      throw new InvalidRequestToolError(
        `meta-analysis studies contain fields not accepted for endpoint_type='${endpoint}'`,
      );
    }
    const measure = study.measure;
    if (measure !== undefined &&
        ((endpoint === "binary_comparative" && !BINARY_META_MEASURES.has(String(measure))) ||
         (endpoint === "continuous_comparative" &&
          !CONTINUOUS_META_MEASURES.has(String(measure))))) {
      throw new InvalidRequestToolError(
        `meta-analysis measure is not accepted for endpoint_type='${endpoint}'`,
      );
    }
  }
}

function isUsableMetaStudy(
  endpoint: string,
  study: Record<string, unknown>,
): boolean {
  const numeric = (field: string) => typeof study[field] === "number" &&
    Number.isFinite(study[field]);
  const positive = (field: string) => numeric(field) && Number(study[field]) > 0;
  const nonnegative = (field: string) => numeric(field) && Number(study[field]) >= 0;
  if (endpoint === "binary_single") {
    return nonnegative("responders") && positive("total") &&
      Number(study.responders) <= Number(study.total);
  }
  if (endpoint === "binary_comparative") {
    return nonnegative("events_t") && positive("total_t") &&
      nonnegative("events_c") && positive("total_c") &&
      Number(study.events_t) <= Number(study.total_t) &&
      Number(study.events_c) <= Number(study.total_c);
  }
  if (endpoint === "continuous_single") {
    return numeric("mean") && positive("sd") && positive("n");
  }
  if (endpoint === "continuous_comparative") {
    return numeric("mean_t") && positive("sd_t") && positive("n_t") &&
      numeric("mean_c") && positive("sd_c") && positive("n_c");
  }
  if (endpoint === "time_to_event") {
    const hasEffect = positive("hr") || numeric("log_hr");
    const hasUncertainty = positive("se") ||
      (positive("ci_lower") && positive("ci_upper") &&
       Number(study.ci_lower) < Number(study.ci_upper));
    return hasEffect && hasUncertainty;
  }
  if (endpoint === "incidence_single") {
    return nonnegative("events") && positive("person_time");
  }
  if (endpoint === "incidence_comparative") {
    return nonnegative("events_t") && positive("person_time_t") &&
      nonnegative("events_c") && positive("person_time_c");
  }
  return false;
}

registerStrictTool(
  "meta_analyze",
  "Run a fixed-effect or random-effects meta-analysis across studies. Supports binary (logit), continuous (mean difference), time-to-event (log-HR), and incidence rate endpoints. Returns pooled estimate, CI, heterogeneity (Q, I², tau²), per-study effects, and n_input/k_used/dropped_studies so any study exclusion is visible.",
  {
    ...verificationMeta,
    endpoint_type: z.enum([
      "binary_single", "binary_comparative",
      "continuous_single", "continuous_comparative",
      "time_to_event",
      "incidence_single", "incidence_comparative",
    ]),
    studies: z.array(metaStudySchema).min(2).max(10000),
    alpha: z.number().gt(0).lt(1).optional()
      .describe("Two-sided alpha for the pooled confidence interval"),
    random: z.boolean().optional(),
    inference_method: z.enum(["hksj", "dl_z"]).optional()
      .describe("Random-effects inference (default hksj; dl_z available for compatibility)"),
  },
  async (params, signal) => {
    const endpoint = String(params.endpoint_type);
    const studies = params.studies as Array<Record<string, unknown>>;
    assertMetaStudyFieldScope(
      endpoint,
      studies,
    );
    const usableStudies = studies.filter((study) => isUsableMetaStudy(endpoint, study));
    if (usableStudies.length < 2) {
      throw new InvalidRequestToolError(
        "meta-analysis requires at least two usable studies for the selected endpoint",
      );
    }
    const defaultMeasure = endpoint === "binary_comparative"
      ? "log_odds_ratio"
      : endpoint === "continuous_comparative"
        ? "mean_difference"
        : undefined;
    if (defaultMeasure) {
      const effectiveMeasures = new Set(
        usableStudies.map((study) => String(study.measure ?? defaultMeasure)),
      );
      if (effectiveMeasures.size !== 1) {
        throw new InvalidRequestToolError(
          "all usable meta-analysis studies must use the same effect measure",
        );
      }
    }
    if (params.random === false && params.inference_method !== undefined) {
      throw new InvalidRequestToolError(
        "fixed-effect meta-analysis does not accept random-effects inference_method",
      );
    }
    return runTool("meta_analyze", params, signal);
  }
);

// --- Tool: ab_test ---

registerStrictTool(
  "ab_test",
  "Size a two-arm A/B experiment. Given a baseline metric and a minimum detectable effect (absolute or relative), returns the per-arm and total sample size, plus achieved power. Supports proportion (conversion-rate) and mean (continuous) metrics with an allocation ratio.",
  {
    ...verificationMeta,
    baseline: z.number().describe("Baseline metric: control proportion in (0,1), or control mean"),
    effect: z.number().describe("Minimum detectable effect (absolute delta, or relative fraction of baseline if effect_type='relative')"),
    metric: z.enum(["proportion", "mean"]).optional(),
    effect_type: z.enum(["absolute", "relative"]).optional(),
    sd: z.number().positive().optional().describe("Standard deviation (required for metric='mean')"),
    alpha: z.number().gt(0).lt(1).optional()
      .describe("Total type-I error probability interpreted according to sided"),
    power: z.number().gt(0).lt(1).optional(),
    sided: z.union([z.literal(1), z.literal(2)]).optional()
      .describe("Test sidedness: 1 for one-sided, 2 for two-sided (default 2)"),
    ratio: z.number().positive().optional().describe("Allocation ratio n_treatment / n_control (default 1)"),
  },
  async (params, signal) => {
    const metric = String(params.metric ?? "proportion");
    const effectiveMde = params.effect_type === "relative"
      ? Number(params.baseline) * Number(params.effect)
      : Number(params.effect);
    if (effectiveMde === 0) {
      throw new InvalidRequestToolError(
        "A/B tests require a nonzero effective minimum detectable effect",
      );
    }
    if (metric === "proportion" &&
        !(Number(params.baseline) > 0 && Number(params.baseline) < 1)) {
      throw new InvalidRequestToolError(
        "proportion A/B tests require baseline strictly between zero and one",
      );
    }
    if (metric === "proportion" && params.sd !== undefined) {
      throw new InvalidRequestToolError(
        "proportion A/B tests do not accept sd",
      );
    }
    if (metric === "proportion") {
      const baseline = Number(params.baseline);
      const effect = Number(params.effect);
      const alternative = params.effect_type === "relative"
        ? baseline * (1 + effect)
        : baseline + effect;
      if (!(alternative > 0 && alternative < 1) || alternative === baseline) {
        throw new InvalidRequestToolError(
          "proportion A/B tests require a distinct alternative proportion in (0,1)",
        );
      }
    }
    if (metric === "mean" && !(Number(params.sd) > 0)) {
      throw new InvalidRequestToolError("mean A/B tests require a positive sd");
    }
    return runTool("ab_test", params, signal);
  }
);

// --- Tool: factorial_design ---

registerStrictTool(
  "factorial_design",
  "Construct a factorial design matrix. fraction=0 gives a full factorial (2^k or mixed-level); fraction=p gives a 2^(k-p) fractional factorial with its resolution, generators, defining relation, and alias structure. Standard minimum-aberration generators are used for common designs; supply exactly p custom generators via 'generators'. Public generator/relation metadata uses one-based public factor-column indices, and public alias classes are bounded to main and two-factor effects.",
  {
    ...verificationMeta,
    n_factors: z.number().int().min(1).max(12),
    levels: z.union([
      z.number().int().min(2).max(20),
      z.array(z.number().int().min(2).max(20)).min(1).max(12),
    ]).optional().describe("Integer levels per factor (2-20; default 2). Fractional designs are 2-level only."),
    fraction: z.number().int().min(0).max(11).optional().describe("0 = full factorial; p>0 = 2^(k-p) fractional"),
    generators: z.array(
      z.array(z.number().int().min(1).max(12)).min(2).max(12),
    ).max(11).optional().describe("For fractional: each added factor's unique basic-factor indices, e.g. [[1,2],[1,3]] for D=AB, E=AC"),
    center_points: z.number().int().min(0).max(1000).optional(),
    replicates: z.number().int().min(1).max(100).optional(),
    randomize: z.boolean().optional(),
    seed: z.number().int().min(0).max(2147483647).optional().describe("Integer RNG seed for run-order randomization (default 42); echoed in the result when randomize=true"),
  },
  async (params, signal) => {
    const fraction = Number(params.fraction ?? 0);
    const nFactors = Number(params.n_factors);
    if (params.seed !== undefined && params.randomize !== true) {
      throw new InvalidRequestToolError(
        "factorial seed is accepted only when randomize=true",
      );
    }
    if (fraction >= nFactors) {
      throw new InvalidRequestToolError("fraction must be below n_factors");
    }
    const generators = params.generators as number[][] | undefined;
    if (generators !== undefined) {
      if (fraction === 0) {
        throw new InvalidRequestToolError("custom generators require a positive fraction");
      }
      if (generators.length !== fraction) {
        throw new InvalidRequestToolError(
          "custom generators must contain exactly fraction definitions",
        );
      }
      const basicFactors = nFactors - fraction;
      const generatorKeys = new Set<string>();
      for (const generator of generators) {
        const normalized = [...generator].sort((left, right) => left - right);
        if (new Set(normalized).size !== normalized.length ||
            normalized.some((index) => index > basicFactors)) {
          throw new InvalidRequestToolError(
            "each custom generator must contain unique indices of the k-p basic factors",
          );
        }
        const key = normalized.join(",");
        if (generatorKeys.has(key)) {
          throw new InvalidRequestToolError("custom generator definitions must be unique");
        }
        generatorKeys.add(key);
      }
    }
    const requestedLevels: number[] = Array.isArray(params.levels)
      ? params.levels.map((value: unknown) => Number(value))
      : [Number(params.levels ?? 2)];
    const levels: number[] = requestedLevels.length === 1
      ? Array<number>(nFactors).fill(requestedLevels[0]) : requestedLevels;
    if (levels.length !== nFactors) {
      throw new InvalidRequestToolError("levels must have length 1 or n_factors");
    }
    let baseRuns: number;
    if (fraction === 0) {
      baseRuns = levels.reduce((product: number, value: number) => product * value, 1);
    } else {
      if (levels.some((value) => value !== 2)) {
        throw new InvalidRequestToolError(
          "fractional factorial designs require two levels per factor",
        );
      }
      baseRuns = 2 ** (nFactors - fraction);
    }
    const runs = baseRuns * Number(params.replicates ?? 1) + Number(params.center_points ?? 0);
    if (!Number.isFinite(runs) || runs > 10000) {
      throw new InvalidRequestToolError("factorial design exceeds the 10,000-run limit");
    }
    return runTool("factorial_design", params, signal);
  }
);

// --- Tool: rsm_design ---

registerStrictTool(
  "rsm_design",
  "Construct a response-surface design. design='ccd' builds a central composite design (factorial + axial + center points) with a rotatable/face/custom alpha; design='bbd' builds a Box-Behnken design (3-5 factors). Returns the design matrix with point-type labels.",
  {
    ...verificationMeta,
    n_factors: z.number().int().min(2).max(8),
    design: z.enum(["ccd", "bbd"]).optional(),
    center_points: z.number().int().min(0).max(1000).optional(),
    alpha: z.union([z.enum(["rotatable", "face"]), z.number().positive()]).optional().describe("Positive CCD axial distance (default 'rotatable'); rejected for design='bbd'"),
    fraction: z.number().int().min(0).optional().describe("CCD: fractionate the factorial core, e.g. 1 for a half-fraction; rejected for design='bbd'"),
    randomize: z.boolean().optional(),
    seed: z.number().int().min(0).max(2147483647).optional().describe("Integer RNG seed for run-order randomization (default 42); echoed in the result when randomize=true"),
  },
  async (params, signal) => {
    const design = String(params.design ?? "ccd");
    const nFactors = Number(params.n_factors);
    if (params.seed !== undefined && params.randomize !== true) {
      throw new InvalidRequestToolError(
        "RSM seed is accepted only when randomize=true",
      );
    }
    if (design === "bbd") {
      if (nFactors < 3 || nFactors > 5) {
        throw new InvalidRequestToolError("Box-Behnken designs require 3-5 factors");
      }
      if (params.alpha !== undefined || params.fraction !== undefined) {
        throw new InvalidRequestToolError(
          "Box-Behnken designs do not accept CCD-only alpha or fraction settings",
        );
      }
    } else if (Number(params.fraction ?? 0) >= nFactors) {
      throw new InvalidRequestToolError("CCD fraction must be below n_factors");
    }
    return runTool("rsm_design", params, signal);
  }
);

// --- Tool: randomize ---

registerStrictTool(
  "randomize",
  "Generate a seeded treatment-assignment plan for n units. method='simple' is unrestricted equal-probability randomization; a non-equal allocation ratio requires 'block' or 'stratified'. 'block' uses permuted blocks for balance; 'stratified' block-randomizes within each stratum.",
  {
    ...verificationMeta,
    n: z.number().int().min(1).max(10000),
    arms: z.union([
      z.number().int().min(2).max(100),
      z.array(
        z.string().min(1).max(MAX_LABEL_CHARS).refine(
          (label) => label.trim().length > 0,
          "arm labels may not be blank",
        ),
      ).min(2).max(100),
    ]).optional().describe("Number of arms, or explicit arm names"),
    method: z.enum(["simple", "block", "stratified"]).optional(),
    block_size: z.number().int().min(1).max(10000).optional()
      .describe("Block/stratified only. Honored only when it is a whole multiple of the smallest exact integer allocation implied by the ratio; otherwise that exact base block is used. The effective size is echoed as block_size_used."),
    strata: z.array(z.union([z.string().max(MAX_LABEL_CHARS), z.number()])).max(10000).optional().describe("Per-unit stratum labels (length n), required for method='stratified'"),
    ratio: z.array(z.number().positive()).min(2).max(100).optional()
      .describe("Positive allocation weights, one per arm. Non-equal weights require method='block' or 'stratified'; method='simple' supports equal weights only."),
    seed: z.number().int().min(0).max(2147483647).optional(),
  },
  async (params, signal) => {
    const method = String(params.method ?? "simple");
    const ratio = params.ratio as number[] | undefined;
    if (method === "simple" && params.block_size !== undefined) {
      throw new InvalidRequestToolError(
        "block_size is accepted only for block or stratified randomization",
      );
    }
    if (Array.isArray(params.arms) && new Set(params.arms).size !== params.arms.length) {
      throw new InvalidRequestToolError("treatment arm labels must be unique");
    }
    const armCount = Array.isArray(params.arms)
      ? params.arms.length
      : Number(params.arms ?? 2);
    if (ratio && ratio.length !== armCount) {
      throw new InvalidRequestToolError(
        "allocation ratio length must equal the number of treatment arms",
      );
    }
    const strata = params.strata as unknown[] | undefined;
    if (method === "stratified") {
      if (!strata || strata.length !== Number(params.n)) {
        throw new InvalidRequestToolError(
          "stratified randomization requires one stratum label per unit",
        );
      }
    } else if (strata !== undefined) {
      throw new InvalidRequestToolError(
        "strata are accepted only for method='stratified'",
      );
    }
    const normalizedRatio = ratio
      ? normalizedAllocationWeights(ratio)
      : undefined;
    if (ratio && normalizedRatio === null) {
      throw new InvalidRequestToolError(
        "allocation ratio cannot be represented within the 10,000-run base-block limit",
      );
    }
    if (method === "simple" && normalizedRatio &&
        normalizedRatio.some((weight) => weight !== normalizedRatio[0])) {
      throw new InvalidRequestToolError(
        "method='simple' supports only equal allocation; use method='block' or " +
        "'stratified' for a non-equal ratio",
      );
    }
    return runRandomizeTool(params, signal);
  }
);

// --- Tool: run_tests ---

registerStrictTool(
  "run_tests",
  "Run the regression suite and return a fixed aggregate health attestation. Raw test logs, suite labels, and paths are never returned. Use this before trusting any simulation results.",
  {},
  async (params, signal) => runUngatedTool("run_tests", params, signal)
);

async function main() {
  assertRequiredPrivateEngineInstallation();
  installServerShutdownHandlers();
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

let shutdownHandlersInstalled = false;
let shutdownPromise: Promise<void> | undefined;

/** Install process handlers only for the executable server, never on import. */
export function installServerShutdownHandlers(
  closeServer: () => Promise<void> | void = () => server.close(),
): void {
  if (shutdownHandlersInstalled) return;
  shutdownHandlersInstalled = true;

  const shutdown = (): Promise<void> => {
    shutdownPromise ??= (async () => {
      // Invoke every terminal latch before the first await so no new child of
      // any runtime can cross the shutdown boundary.
      await Promise.all([
        shutdownActiveRProcesses(),
        shutdownActiveVerifierProcesses(),
        shutdownActiveRuntimeProbeProcesses(),
      ]);
      await closeServer();
    })();
    return shutdownPromise;
  };
  const shutdownAndExit = (exitCode: number) => {
    void shutdown().then(
      () => process.exit(exitCode),
      (error) => {
        console.error(error);
        process.exit(1);
      },
    );
  };

  process.once("SIGINT", () => shutdownAndExit(130));
  process.once("SIGTERM", () => shutdownAndExit(143));
  process.stdin.once("end", () => { void shutdown().catch(console.error); });
  process.stdin.once("close", () => { void shutdown().catch(console.error); });
}

let invokedAsEntrypoint = false;
try {
  invokedAsEntrypoint = Boolean(process.argv[1]) &&
    realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url));
} catch { /* imported modules and invalid argv never start a stdio server */ }
if (invokedAsEntrypoint) {
  main().catch(async (error) => {
    if (error instanceof MissingPrivateEngineInstallationError) {
      // Keep the expected public-clone diagnostic fixed and path-free.
      console.error(MISSING_PRIVATE_ENGINE_INSTALLATION_MESSAGE);
    } else {
      console.error(error);
    }
    await Promise.all([
      shutdownActiveRProcesses(),
      shutdownActiveVerifierProcesses(),
      shutdownActiveRuntimeProbeProcesses(),
    ]);
    process.exitCode = 1;
  });
}
