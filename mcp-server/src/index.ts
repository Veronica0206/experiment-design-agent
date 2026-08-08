import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import { createHash, randomUUID } from "node:crypto";
import { constants as fsConstants, readFileSync, readdirSync, realpathSync, statSync } from "node:fs";
import { mkdtemp, open, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, delimiter, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { callR } from "./r-bridge.js";
import {
  hashArtifactsForProvenance,
  publishVerifiedArtifacts,
} from "./artifact-publication.js";
import { readBoundedFile } from "./bounded-read.js";
import { preverify, type VerificationEnvelope } from "./verifier.js";
import { publicRegressionStatus, publicValidatedConfig } from "./public-projection.js";
import {
  currentPythonRuntimeSnapshot,
  currentRRuntimeSnapshot,
  FingerprintPromiseCache,
  hashFramedFields,
  mutableEngineFiles,
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

const SERVER_VERSION = "1.1.0";
const PRIVATE_PROVENANCE_META_KEY = "experiment-design/private-provenance";
const PRIVATE_ARTIFACT_META_KEY = "experiment-design/private-artifact";
const SHA256_HEX = /^[0-9a-f]{64}$/;
const SUITE_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
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

// JavaScript modules and dependency declarations are loaded at process start;
// preserve the fingerprint of those exact startup bytes even if files change.
const STARTUP_RUNTIME_FINGERPRINT = hashFiles([
  ...flatFiles(join(SUITE_ROOT, "mcp-server", "dist"), ".js"),
  join(SUITE_ROOT, "mcp-server", "package.json"),
  join(SUITE_ROOT, "mcp-server", "package-lock.json"),
]);

type BoundRuntime = { r: RRuntimeSnapshot; python: PythonRuntimeSnapshot };
const runtimeByEngineFingerprint = new Map<string, BoundRuntime>();

function engineFingerprint(): string {
  // R and Python modules are loaded by a fresh child process for each call, so
  // recompute their hashes at provenance time rather than freezing startup state.
  const rRuntime = currentRRuntimeSnapshot();
  const pythonRuntime = currentPythonRuntimeSnapshot();
  const fingerprint = hashFiles(
    mutableEngineFiles(SUITE_ROOT),
    `${STARTUP_RUNTIME_FINGERPRINT}:${rRuntime.fingerprint}:${pythonRuntime.fingerprint}`,
  );
  runtimeByEngineFingerprint.set(fingerprint, { r: rRuntime, python: pythonRuntime });
  while (runtimeByEngineFingerprint.size > 16) {
    const oldest = runtimeByEngineFingerprint.keys().next().value;
    if (typeof oldest !== "string") break;
    runtimeByEngineFingerprint.delete(oldest);
  }
  return fingerprint;
}

function assertEngineUnchanged(expected: string, phase: string): void {
  if (engineFingerprint() !== expected) {
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
    assertEngineUnchanged(fingerprint, "regression attestation was running");
    return result;
  });
  return waitForSharedPromise(shared, signal);
}

const verificationMeta = {
  verification_id: z.string().trim().min(1).max(128).optional()
    .describe("Deprecated caller hint; the runtime always replaces it with its own analysis identity."),
};

const registerStrictTool = (
  name: string,
  description: string,
  shape: z.ZodRawShape,
  handler: (params: any, signal: AbortSignal) => Promise<any>,
) => server.registerTool(name, {
  description,
  inputSchema: z.object(shape).strict(),
}, (params, extra) => handler(params, extra.signal));

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
  executionFingerprint = engineFingerprint(),
) {
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
      identity?: VerificationEnvelope["identity"] & { result_hash?: unknown };
    };
    if (!rawIdentity) return rest;
    const { result_hash: _rawResultHash, ...publicIdentity } = rawIdentity;
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
  const executionFingerprint = engineFingerprint();
  const regression = await regressionAttestation(executionFingerprint, signal);
  const result = await callR(tool, domain, signal);
  const stochastic = tool === "simulate_design" || tool === "randomize" ||
    ((tool === "factorial_design" || tool === "rsm_design") && domain.randomize === true);
  const replay = stochastic ? await callR(tool, domain, signal) : undefined;
  assertEngineUnchanged(executionFingerprint, "the analysis was running");
  const provenance = buildProvenance(result, params, undefined, executionFingerprint);
  const verification = await preverify(
    tool, domain, result, replay, stochastic, [], publicProvenance(provenance), regression,
  );
  assertEngineUnchanged(executionFingerprint, "the verifier was running");
  return toolResponse(result, params, verification, provenance);
}

async function runRandomizeTool(
  params: Record<string, unknown>,
  signal: AbortSignal,
) {
  const domain = domainParams(params);
  const executionFingerprint = engineFingerprint();
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
    assertEngineUnchanged(executionFingerprint, "the analysis was running");
    const provenance = buildProvenance(
      result, params, undefined, executionFingerprint,
    );
    const verification = await preverify(
      "randomize", domain, result, replay, true, [], publicProvenance(provenance), regression,
    );
    assertEngineUnchanged(executionFingerprint, "the verifier was running");
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
    result = await regressionAttestation(engineFingerprint(), signal) as Record<string, unknown>;
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

function boundedCsvRows(bytes: Buffer, maximum: number, label: string): void {
  // Counting physical lines is deliberately conservative for quoted multiline
  // cells: it may reject an unusually encoded file, but it never undercounts a
  // memory-amplifying input before the R CSV parser materializes it.
  let lines = bytes.length ? 1 : 0;
  for (const byte of bytes) if (byte === 0x0a) lines += 1;
  const dataRows = Math.max(0, lines - 1);
  if (dataRows > maximum) throw new Error(`${label} exceeds the ${maximum}-row limit`);
}

async function persistentOutputDir(rawPath: string | undefined, prefix: string): Promise<string> {
  return createManagedArtifactDir(prefix, rawPath);
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
  z.record(z.number()),
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
  sd: z.number().optional(),
  alloc_ratio: z.number().optional(),
  alphas: z.union([z.number().gt(0).lt(1), z.array(z.number().gt(0).lt(1))]).optional(),
  powers: z.union([z.number().gt(0).lt(1), z.array(z.number().gt(0).lt(1))]).optional(),
  prior: priorSchema,
  go_threshold: z.number().gt(0).lt(1).optional()
    .describe("Posterior probability threshold for Go (non-confirmatory decisions; default 0.90)"),
  consider_threshold: z.number().gt(0).lt(1).optional()
    .describe("Posterior probability threshold for Consider (default 0.60)"),
  go_target: z.number().optional()
    .describe("Go/No-Go posterior target: P(theta > go_target | data). Default: midpoint of null/alt (geometric mean for tte)"),
  accrual_time: z.number().optional(),
  followup_time: z.number().optional(),
  exposure_time: z.number().optional(),
  tte_method: tteMethod,
  rate_method: rateMethod,
};

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
const strictPath = z.string().min(1).max(4096).refine(
  (value) => value === value.trim(),
  "paths may not contain leading or trailing whitespace",
);

function assertSingleEndpointParams(params: Record<string, unknown>): void {
  const go = Number(params.go_threshold ?? 0.90);
  const consider = Number(params.consider_threshold ?? 0.60);
  if (!(consider < go)) throw new Error("consider_threshold must be below go_threshold");
  const endpoint = params.endpoint_type;
  const nullParam = Number(params.null_param);
  const altParam = Number(params.alt_param);
  if ((endpoint === "binary" || endpoint === "continuous") && !(altParam > nullParam)) {
    throw new Error("alt_param > null_param is required for this endpoint");
  }
  if (params.design === "single_arm" && params.p2_data_ctrl !== undefined) {
    throw new Error("p2_data_ctrl is not valid for a single-arm design");
  }
  if (params.go_target !== undefined) {
    const target = Number(params.go_target);
    if (params.endpoint_type === "binary" && (target < 0 || target > 1)) {
      throw new Error("binary go_target must lie in [0,1]");
    }
    if (params.endpoint_type === "tte" && target <= 0) throw new Error("tte go_target must be positive");
    if (params.endpoint_type === "incidence_rate" && target < 0) {
      throw new Error("incidence-rate go_target must be non-negative");
    }
  }
}

const server = new McpServer({
  name: "experiment-design",
  version: SERVER_VERSION,
});

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
      p3_alpha: z.number().gt(0).lt(1).optional(),
      label: z.string().optional().describe("Display label for the endpoint"),
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
    if (config.design === "controlled" && config.p2_data && !config.p2_data_ctrl) {
      throw new Error("controlled confirmatory PPOS requires p2_data_ctrl");
    }
    if (params.n_oc !== undefined) {
      if (config.design === "controlled" && typeof params.n_oc === "number") {
        throw new Error("controlled OC requires n_oc={n_trt,n_ctrl}; scalar n_oc is single-arm only");
      }
      const units = typeof params.n_oc === "number"
        ? params.n_oc
        : Number(params.n_oc.n_trt) + Number(params.n_oc.n_ctrl);
      const replicates = Number(params.B_oc ?? 5000);
      if (units * replicates > MAX_SIMULATED_UNITS) {
        throw new Error(
          `requested OC workload=${units * replicates} (= ${units} units × ` +
          `${replicates} replicates) exceeds the ${MAX_SIMULATED_UNITS} limit; ` +
          "reduce B_oc or n_oc",
        );
      }
      if (config.design === "single_arm" && typeof params.n_oc === "object") {
        throw new Error("single-arm OC requires scalar n_oc");
      }
    }
    return runTool("simulate_design", params, signal);
  }
);

// --- Tool: master_simulate ---

registerStrictTool(
  "master_simulate",
  "Run a multi-arm adaptive design simulation: basket (with borrowing), umbrella (MAMS/DTL/BAR), or platform (with NCC adjustment). Returns OC tables, FWER, subgroup decisions, and output file paths.",
  {
    ...verificationMeta,
    // Every key below is a real create_master_config() formal that an engine
    // reads (or that the R layer explicitly rejects with an informative error —
    // selection_rule / power_type / shared_control, and the design-scoped knobs
    // phase / borrowing_method / umbrella_method / ncc_method when sent to the
    // wrong design type). `overdispersion` and `rar_eta` stay unexposed: no
    // engine reads them and R rejects them, so with .strict() they fail loudly
    // here instead of being zod-stripped into a silent default run.
    config: z.object({
      master_design_type: z.enum(["basket", "umbrella", "platform"]),
      endpoint_type: z.enum(["binary", "continuous", "tte", "incidence_rate"]),
      n_subgroups: z.number().int().min(2).max(50),
      null_params: z.union([z.number(), z.array(z.number())]),
      alt_params: z.array(z.number()),
      n_per_subgroup: z.number().int().min(1).max(100000).optional(),
      alpha: z.number().gt(0).lt(1).optional(),
      n_sims: z.number().int().min(1).max(10000).optional(),
      seed: z.number().int().min(0).max(2147483647).optional().describe("Integer RNG seed (default 42); echoed in the result"),
      label: z.string().optional()
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
      soc_data: z.object({ means: z.array(z.number()) }).optional()
        .describe("Reserved for the disabled wathen_sti prototype; any supplied value is rejected"),
      ia_pruning_alpha: z.number().gt(0).lt(1).optional()
        .describe("Basket chen_confirmatory: pruning threshold at interim (default 0.10)"),
      chen_strategy: z.enum(["d1", "d2", "d3"]).optional()
        .describe("Basket chen_confirmatory decision strategy: d1 survivors complete their planned per-basket maximum; d2 the planned pooled final total is redistributed over survivors; d3 the planned post-IA enrollment is redistributed over survivors (default d1)"),
      // Umbrella-specific
      umbrella_method: z.enum(["mams", "drop_the_losers", "bayesian_adaptive_randomization"]).optional()
        .describe("Umbrella only (rejected for basket/platform): design method (default mams)"),
      n_arms: z.number().int().min(2).optional(),
      n_stages: z.number().int().min(1).max(20).optional(),
      n_per_arm_stage: z.number().int().min(2).max(100000).optional(),
      sd: z.number().optional(),
      futility_boundaries: z.array(z.number()).optional()
        .describe("Umbrella: stage-wise futility boundaries, length n_stages (default: calculated)"),
      n_drop_per_stage: z.array(z.number().int().nonnegative()).optional()
        .describe("Umbrella drop_the_losers: number of arms to drop per stage, length n_stages-1 (default: drop worst)"),
      rar_gamma: z.number().nonnegative().optional()
        .describe("Umbrella BAR: allocation-aggressiveness exponent, held CONSTANT across stages. The R default is the stage-increasing FUNCTION j/J, which cannot be expressed in JSON — omit this to keep it."),
      selection_rule: z.enum(["rank_best", "threshold"]).optional()
        .describe("Derived automatically (rank_best for drop_the_losers, threshold otherwise) — the engines' selection logic is fixed. An explicit value that differs from the derived default is REJECTED by the R layer; omit it."),
      power_type: z.enum(["one_minimum"]).optional()
        .describe("Only 'one_minimum' exists — power is always reported per-arm / at-least-one-minimum. Any other value is REJECTED by the R layer."),
      // Platform-specific
      n_periods: z.number().int().min(2).max(100).optional(),
      n_per_period: z.number().int().min(1).max(100000).optional(),
      arms_schedule: z.object({
        enter: z.array(z.number().int()).describe("Period each arm enters (length = n_subgroups)"),
        leave: z.array(z.number().int()).describe("Period each arm leaves (length = n_subgroups, each >= enter)"),
      }).optional().describe("Platform designs only: when each arm is active. Required for master_design_type='platform'."),
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
        .describe("Platform: interim analysis every N periods (default 1)"),
      effect_threshold: z.number().gt(0).lt(1).optional()
        .describe("Platform: posterior probability threshold for early effect stopping (default 0.99)"),
      futility_threshold: z.number().gt(0).lt(1).optional()
        .describe("Platform: posterior probability threshold for early futility stopping (default 0.05)"),
      // Endpoint-specific
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
    const nSims = Number(cfgForBudget.n_sims ?? 10000);
    const nSubgroups = Number(cfgForBudget.n_subgroups);
    const workload = designType === "basket"
      ? nSims * nSubgroups * Number(cfgForBudget.n_per_subgroup ?? 25)
      : designType === "umbrella"
        ? nSims * (nSubgroups + 1) * Number(cfgForBudget.n_per_arm_stage ?? 9) * Number(cfgForBudget.n_stages ?? 2)
        : nSims * Number(cfgForBudget.n_periods) * Number(cfgForBudget.n_per_period);
    if (!Number.isFinite(workload) || workload > MAX_SIMULATED_UNITS) {
      throw new Error(
        `requested ${designType} workload=${workload} simulated units exceeds the ` +
        `${MAX_SIMULATED_UNITS} limit (n_sims=${nSims}, n_subgroups=${nSubgroups}); ` +
        "reduce n_sims or the per-subgroup/per-stage/per-period sample size",
      );
    }
    if (designType === "basket" && Number(cfgForBudget.nogo_threshold ?? 0.10) >= Number(cfgForBudget.go_threshold ?? 0.90)) {
      throw new Error("nogo_threshold must be below go_threshold");
    }
    if (cfgForBudget.endpoint_type === "binary") {
      const nulls = Array.isArray(cfgForBudget.null_params) ? cfgForBudget.null_params : [cfgForBudget.null_params];
      const alts = cfgForBudget.alt_params as number[];
      if ([...nulls, ...alts].some((value) => Number(value) < 0 || Number(value) > 1)) {
        throw new Error("binary endpoint parameters must lie in [0,1]");
      }
    }
    const borrowingMethod = String(cfgForBudget.borrowing_method ?? "");
    if (["snti", "sep_gibbs", "wathen_sti"].includes(borrowingMethod)) {
      throw new Error(
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
        throw new Error("complete borrowing requires identical null_params across subgroups");
      }
      if (borrowingMethod === "simons_bayesian" &&
          (heterogeneous(nulls) || heterogeneous(alts))) {
        throw new Error("simons_bayesian requires identical null_params and alt_params across subgroups");
      }
    }
    if (designType === "basket" && cfgForBudget.n_per_interim !== undefined &&
        Number(cfgForBudget.n_per_interim) * Number(cfgForBudget.n_interims ?? 1) >=
          Number(cfgForBudget.n_per_subgroup ?? 25)) {
      throw new Error("n_per_interim * n_interims must be below n_per_subgroup so the final look has participants");
    }
    if (designType === "umbrella") {
      const stages = Number(cfgForBudget.n_stages ?? 2);
      const arms = Number(cfgForBudget.n_arms ?? nSubgroups);
      const boundaries = cfgForBudget.futility_boundaries as number[] | undefined;
      if (boundaries && boundaries.length !== stages) {
        throw new Error("futility_boundaries must contain exactly one value per stage");
      }
      if (boundaries) {
        const alpha = Number(cfgForBudget.alpha ?? 0.025);
        // R performs the authoritative normal-quantile check; this structural
        // check keeps obviously malformed arrays from reaching the engine.
        if (!Number.isFinite(boundaries[stages - 1]) || alpha <= 0 || arms < 2) {
          throw new Error("invalid final boundary, alpha, or arm count");
        }
      }
      const drops = cfgForBudget.n_drop_per_stage as number[] | undefined;
      if (drops && (drops.length !== Math.max(0, stages - 1) ||
          drops.reduce((sum, value) => sum + value, 0) >= arms)) {
        throw new Error("n_drop_per_stage must cover every interim and leave at least one arm");
      }
    }
    if (designType === "platform" && Number(cfgForBudget.futility_threshold ?? 0.05) >= Number(cfgForBudget.effect_threshold ?? 0.99)) {
      throw new Error("futility_threshold must be below effect_threshold");
    }
    const executionFingerprint = engineFingerprint();
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
      assertEngineUnchanged(executionFingerprint, "the analysis was running");
      const provenance = buildProvenance(
        result, params, undefined, executionFingerprint,
      );
      const verification = await preverify(
        "master_simulate", identityDomain, result, replay, replayRequired,
        replayRequired ? [] : ["same-seed replay skipped above 1000 simulations"],
        publicProvenance(provenance), regression,
      );
      assertEngineUnchanged(executionFingerprint, "the verifier was running");
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
      treatment_a: z.string(),
      treatment_c: z.string(),
      common_comparator: z.string(),
      effect_measure: z.string().optional(),
      analysis_scale: z.string().optional(),
      alpha: z.number().gt(0).lt(1).optional(),
    }).strict()).min(1).max(1000).optional(),
    ipd_file: strictPath.optional(),
    targets_file: strictPath.optional(),
    output_dir: strictPath.optional(),
    treatment_arm: z.string().optional().describe("MAIC: active-arm label in the IPD arm column (required for method='maic')"),
    comparator_arm: z.string().optional().describe("MAIC: comparator-arm label in the IPD; omit for unanchored"),
    arm_col: z.string().optional().describe("MAIC: name of the arm/treatment column in the IPD CSV (default 'arm')"),
    maic_endpoint_type: z.enum(["binary", "continuous", "rate", "tte"]).optional().describe("MAIC: outcome type in the IPD (default 'binary')"),
    outcome_col: z.string().optional().describe("MAIC: outcome column (binary/continuous) in the IPD CSV (default 'response')"),
    event_col: z.string().optional().describe("MAIC: event-count column in the IPD CSV (required for maic_endpoint_type='rate')"),
    time_col: z.string().optional().describe("MAIC: person-time/follow-up column in the IPD CSV (required for 'rate' and 'tte')"),
    status_col: z.string().optional().describe("MAIC: event-status (0/1) column in the IPD CSV (required for maic_endpoint_type='tte')"),
    covariates: z.array(z.string()).optional()
      .describe("MAIC: covariate columns to weight on (default: every column in the targets CSV)"),
    tte_method: z.enum(["cox", "exponential"]).optional()
      .describe("MAIC tte analysis model: 'cox' (default, weighted Cox PH) or 'exponential' (base-R person-time proxy)"),
    measure: z.string().optional().describe("MAIC: effect measure, e.g. log_odds_ratio / log_hazard_ratio / log_rate_ratio / mean_difference"),
    alpha: z.number().gt(0).lt(1).optional(),
    bootstrap_replicates: z.number().int().min(50).max(5000).optional()
      .describe("MAIC non-Cox: stratified bootstrap replicates with weights refit (default 200)"),
    bootstrap_seed: z.number().int().min(0).max(2147483647).optional()
      .describe("MAIC non-Cox bootstrap seed (default 42)"),
    export_row_weights: z.boolean().optional()
      .describe("MAIC: explicitly export row index, arm, and weight; false by default"),
  },
  async (params, signal) => {
    const identityDomain = domainParams(params);
    const domain = { ...identityDomain };
    let managedOutput: string | undefined;
    let inputSnapshot: string | undefined;
    let boundInputHashes: Record<string, string> | undefined;
    let completed = false;
    try {
      if (domain.method === "maic") {
        if (typeof domain.ipd_file !== "string" || typeof domain.targets_file !== "string") {
          throw new Error("maic requires ipd_file and targets_file");
        }
        const [ipdBytes, targetsBytes] = await Promise.all([
          readAllowedFile(domain.ipd_file),
          readAllowedFile(domain.targets_file),
        ]);
        boundedCsvRows(ipdBytes, MAX_IPD_ROWS, "IPD CSV");
        boundedCsvRows(targetsBytes, MAX_TARGET_ROWS, "target CSV");

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
      const executionFingerprint = engineFingerprint();
      const regression = await regressionAttestation(executionFingerprint, signal);
      const result = await callR("indirect_compare", domain, signal);
      assertEngineUnchanged(executionFingerprint, "the analysis was running");
      const provenance = buildProvenance(
        result, params, boundInputHashes, executionFingerprint,
      );
      const verification = await preverify(
        "indirect_compare", identityDomain, result, undefined, false, [],
        publicProvenance(provenance),
        regression,
      );
      assertEngineUnchanged(executionFingerprint, "the verifier was running");
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
    alpha: z.number().gt(0).lt(1).optional(),
    random: z.boolean().optional(),
    inference_method: z.enum(["hksj", "dl_z"]).optional()
      .describe("Random-effects inference (default hksj; dl_z available for compatibility)"),
  },
  async (params, signal) => runTool("meta_analyze", params, signal)
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
    sd: z.number().optional().describe("Standard deviation (required for metric='mean')"),
    alpha: z.number().gt(0).lt(1).optional(),
    power: z.number().gt(0).lt(1).optional(),
    sided: z.union([z.literal(1), z.literal(2)]).optional(),
    ratio: z.number().positive().optional().describe("Allocation ratio n_treatment / n_control (default 1)"),
  },
  async (params, signal) => runTool("ab_test", params, signal)
);

// --- Tool: factorial_design ---

registerStrictTool(
  "factorial_design",
  "Construct a factorial design matrix. fraction=0 gives a full factorial (2^k or mixed-level); fraction=p gives a 2^(k-p) fractional factorial with its resolution, generators, defining relation, and alias structure. Standard minimum-aberration generators are used for common designs; supply your own via 'generators'.",
  {
    ...verificationMeta,
    n_factors: z.number().int().min(1).max(12),
    levels: z.union([
      z.number().int().min(2).max(20),
      z.array(z.number().int().min(2).max(20)).min(1).max(12),
    ]).optional().describe("Integer levels per factor (2-20; default 2). Fractional designs are 2-level only."),
    fraction: z.number().int().min(0).optional().describe("0 = full factorial; p>0 = 2^(k-p) fractional"),
    generators: z.array(z.array(z.number().int().min(1).max(12)).min(2)).optional().describe("For fractional: each added factor's unique basic-factor indices, e.g. [[1,2],[1,3]] for D=AB, E=AC"),
    center_points: z.number().int().min(0).max(1000).optional(),
    replicates: z.number().int().min(1).max(100).optional(),
    randomize: z.boolean().optional(),
    seed: z.number().int().min(0).max(2147483647).optional().describe("Integer RNG seed for run-order randomization (default 42); echoed in the result when randomize=true"),
  },
  async (params, signal) => {
    if (Number(params.fraction ?? 0) === 0) {
      const levels = Array.isArray(params.levels)
        ? params.levels : Array(Number(params.n_factors)).fill(Number(params.levels ?? 2));
      if (levels.length !== Number(params.n_factors)) throw new Error("levels must have length 1 or n_factors");
      const runs = levels.reduce((product: number, value: number) => product * value, 1)
        * Number(params.replicates ?? 1) + Number(params.center_points ?? 0);
      if (!Number.isFinite(runs) || runs > 10000) throw new Error("factorial design exceeds the 10,000-run limit");
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
    alpha: z.union([z.enum(["rotatable", "face"]), z.number().positive()]).optional().describe("Positive CCD axial distance (default 'rotatable'); not read by design='bbd'"),
    fraction: z.number().int().min(0).optional().describe("CCD: fractionate the factorial core, e.g. 1 for a half-fraction; not read by design='bbd'"),
    randomize: z.boolean().optional(),
    seed: z.number().int().min(0).max(2147483647).optional().describe("Integer RNG seed for run-order randomization (default 42); echoed in the result when randomize=true"),
  },
  async (params, signal) => runTool("rsm_design", params, signal)
);

// --- Tool: randomize ---

registerStrictTool(
  "randomize",
  "Generate a seeded treatment-assignment plan for n units. method='simple' is unrestricted; 'block' uses permuted blocks for balance; 'stratified' block-randomizes within each stratum. Supports >2 arms and an allocation ratio.",
  {
    ...verificationMeta,
    n: z.number().int().min(1).max(10000),
    arms: z.union([z.number().int().min(2).max(100), z.array(z.string()).min(2).max(100)]).optional().describe("Number of arms, or explicit arm names"),
    method: z.enum(["simple", "block", "stratified"]).optional(),
    block_size: z.number().int().min(1).max(10000).optional()
      .describe("Block/stratified only. Honored only when it is a whole multiple of the smallest exact integer allocation implied by the ratio; otherwise that exact base block is used. The effective size is echoed as block_size_used."),
    strata: z.array(z.union([z.string(), z.number()])).optional().describe("Per-unit stratum labels (length n), required for method='stratified'"),
    ratio: z.array(z.number().positive()).optional().describe("Positive allocation weights, one per arm"),
    seed: z.number().int().min(0).max(2147483647).optional(),
  },
  async (params, signal) => runRandomizeTool(params, signal)
);

// --- Tool: run_tests ---

registerStrictTool(
  "run_tests",
  "Run the regression suite and return a fixed aggregate health attestation. Raw test logs, suite labels, and paths are never returned. Use this before trusting any simulation results.",
  {},
  async (params, signal) => runUngatedTool("run_tests", params, signal)
);

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

let invokedAsEntrypoint = false;
try {
  invokedAsEntrypoint = Boolean(process.argv[1]) &&
    realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url));
} catch { /* imported modules and invalid argv never start a stdio server */ }
if (invokedAsEntrypoint) main().catch(console.error);
