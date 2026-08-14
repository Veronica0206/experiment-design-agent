import { createHash } from "node:crypto";
import {
  accessSync,
  constants as fsConstants,
  lstatSync,
  readdirSync,
  readFileSync,
  realpathSync,
  statSync,
} from "node:fs";
import { open as openFile } from "node:fs/promises";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import {
  killRuntimeProcessTree,
  spawnRuntimeProcess,
} from "./runtime-supervisor.js";

export const REGRESSION_SKILLS = [
  "vera-experiment-designing",
  "vera-master-experiment-designing",
  "vera-doe-designing",
  "vera-indirect-comparing",
  "vera-meta-analyzing",
] as const;

export const TRACKED_R_PACKAGES = [
  "jsonlite", "survival", "Exact", "mvtnorm", "MAMS",
] as const;

export const TRACKED_R_BASE_PACKAGES = [
  "base", "stats", "graphics", "grDevices", "utils", "datasets", "methods",
] as const;

export type RRuntimeSnapshot = {
  fingerprint: string;
  version: string;
  packageVersions: Record<string, string | null>;
};

export type PythonRuntimeSnapshot = {
  fingerprint: string;
  version: string;
};

type RuntimeProbeOutcome = {
  code: number | null;
  signal: NodeJS.Signals | null;
  stdout: string;
  stderr: string;
};

interface ActiveRuntimeProbeProcessGroup {
  terminate: () => void;
  closed: Promise<void>;
}

const MAX_RUNTIME_PROBE_OUTPUT_BYTES = 1024 * 1024;
const RUNTIME_PROBE_TIMEOUT_MS = 60_000;
const MAX_PYTHON_RUNTIME_FILES = 2_048;
const MAX_PYTHON_RUNTIME_BYTES = 128 * 1024 * 1024;
const PYTHON_RUNTIME_READ_CHUNK_BYTES = 64 * 1024;
const activeRuntimeProbeProcessGroups = new Map<
  number, ActiveRuntimeProbeProcessGroup
>();
let acceptingRuntimeProbeProcesses = true;

/** Number of live runtime-fingerprint process groups, for lifecycle tests. */
export function activeRuntimeProbeProcessGroupCount(): number {
  return activeRuntimeProbeProcessGroups.size;
}

/**
 * Permanently stop new probes, kill every active detached probe group, and
 * wait for each leader to close. Descendants share the leader's POSIX process
 * group, so shutdown cannot leave an R or Python helper running in the
 * background.
 */
export async function shutdownActiveRuntimeProbeProcesses(): Promise<void> {
  acceptingRuntimeProbeProcesses = false;
  for (;;) {
    const active = [...activeRuntimeProbeProcessGroups.values()];
    if (active.length === 0) return;
    for (const group of active) group.terminate();
    await Promise.all(active.map((group) => group.closed));
  }
}

function runtimeProbeAbortError(): Error {
  const error = new Error("Runtime fingerprint probe cancelled by the MCP client");
  error.name = "AbortError";
  return error;
}

/** Run one bounded runtime probe as a tracked, cancellable process group. */
function runRuntimeProbe(
  executable: string,
  args: string[],
  environment: NodeJS.ProcessEnv | undefined,
  signal?: AbortSignal,
): Promise<RuntimeProbeOutcome> {
  if (!acceptingRuntimeProbeProcesses) {
    return Promise.reject(new Error("Runtime fingerprint probes are shutting down"));
  }
  if (signal?.aborted) return Promise.reject(runtimeProbeAbortError());

  return new Promise((resolvePromise, rejectPromise) => {
    const proc = spawnRuntimeProcess(executable, args, {
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
      ...(environment ? { env: environment } : {}),
    });
    const stdout: Buffer[] = [];
    const stderr: Buffer[] = [];
    let stdoutBytes = 0;
    let stderrBytes = 0;
    let settled = false;
    let stopping = false;
    let closedMarked = false;
    let abortListening = false;
    let closeFallback: NodeJS.Timeout | undefined;
    let resolveClosed!: () => void;
    const closed = new Promise<void>((resolveClosedPromise) => {
      resolveClosed = resolveClosedPromise;
    });
    const processGroupId = proc.pid;

    const markClosed = () => {
      if (closedMarked) return;
      closedMarked = true;
      if (closeFallback) clearTimeout(closeFallback);
      if (processGroupId !== undefined) {
        activeRuntimeProbeProcessGroups.delete(processGroupId);
      }
      resolveClosed();
    };
    const cleanupResult = () => {
      clearTimeout(timeout);
      if (abortListening) signal?.removeEventListener("abort", onAbort);
      abortListening = false;
    };
    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      cleanupResult();
      rejectPromise(error);
    };
    const succeed = (outcome: RuntimeProbeOutcome) => {
      if (settled) return;
      settled = true;
      cleanupResult();
      resolvePromise(outcome);
    };
    const stopProcessGroup = (error: Error) => {
      if (!stopping) {
        stopping = true;
        // Immediate SIGKILL covers a fast-exiting leader and all descendants.
        killRuntimeProcessTree(proc, "SIGKILL");
        // Do not let an inherited descriptor prevent terminal shutdown after
        // the complete process group has already received SIGKILL.
        closeFallback = setTimeout(markClosed, 1_000);
      }
      fail(error);
    };
    const onAbort = () => {
      if (!settled) stopProcessGroup(runtimeProbeAbortError());
    };
    const appendBounded = (
      chunks: Buffer[],
      chunk: Buffer,
      currentBytes: number,
      streamName: "stdout" | "stderr",
    ): number => {
      const nextBytes = currentBytes + chunk.length;
      if (nextBytes > MAX_RUNTIME_PROBE_OUTPUT_BYTES) {
        stopProcessGroup(new Error(
          `Runtime fingerprint probe ${streamName} exceeded ` +
          `${MAX_RUNTIME_PROBE_OUTPUT_BYTES} bytes`,
        ));
        return currentBytes;
      }
      chunks.push(chunk);
      return nextBytes;
    };
    const timeout = setTimeout(() => {
      stopProcessGroup(new Error(
        `Runtime fingerprint probe timed out after ${RUNTIME_PROBE_TIMEOUT_MS / 1000}s`,
      ));
    }, RUNTIME_PROBE_TIMEOUT_MS);

    proc.stdout?.on("data", (chunk: Buffer) => {
      stdoutBytes = appendBounded(stdout, chunk, stdoutBytes, "stdout");
    });
    proc.stderr?.on("data", (chunk: Buffer) => {
      stderrBytes = appendBounded(stderr, chunk, stderrBytes, "stderr");
    });
    proc.on("error", (error) => {
      if (processGroupId === undefined) markClosed();
      fail(new Error(`Failed to spawn runtime fingerprint probe: ${error.message}`));
    });
    proc.on("close", (code, processSignal) => {
      markClosed();
      if (settled) return;
      succeed({
        code,
        signal: processSignal,
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      });
    });
    if (processGroupId !== undefined) {
      activeRuntimeProbeProcessGroups.set(processGroupId, {
        terminate: () => stopProcessGroup(
          new Error("Runtime fingerprint probes are shutting down"),
        ),
        closed,
      });
    }
    if (signal) {
      abortListening = true;
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    }
  });
}

/**
 * Resolve a child executable once from an explicit absolute path or a closed
 * list of installation locations. Ambient PATH is deliberately never
 * consulted: MCP requests may arrive after an untrusted caller has changed the
 * process environment.
 */
export function resolveRuntimeExecutable(
  command: string,
  approvedCandidates: readonly string[] = [],
): string {
  const candidates = isAbsolute(command)
    ? [command]
    : [...approvedCandidates];
  if (candidates.length === 0) {
    throw new Error(`required runtime executable must use an absolute path: ${command}`);
  }
  for (const candidate of candidates) {
    try {
      accessSync(candidate, process.platform === "win32" ? fsConstants.F_OK : fsConstants.X_OK);
      const canonical = realpathSync(candidate);
      if (statSync(canonical).isFile()) return canonical;
    } catch { /* try the next approved installation path */ }
  }
  throw new Error(`required runtime executable is unavailable: ${command}`);
}

const SUITE_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));

function configuredRuntime(
  environmentName: "EXPDESIGN_RSCRIPT" | "EXPDESIGN_PYTHON",
  displayName: string,
  approvedCandidates: readonly string[],
): string {
  const configured = process.env[environmentName];
  if (configured !== undefined) {
    if (!isAbsolute(configured)) {
      throw new Error(`${environmentName} must be an absolute path`);
    }
    return resolveRuntimeExecutable(configured);
  }
  return resolveRuntimeExecutable(displayName, approvedCandidates);
}

export const RSCRIPT_EXECUTABLE = configuredRuntime(
  "EXPDESIGN_RSCRIPT",
  "Rscript",
  [
    "/Library/Frameworks/R.framework/Resources/bin/Rscript",
    "/opt/homebrew/bin/Rscript",
    "/usr/local/bin/Rscript",
    "/usr/bin/Rscript",
    "/opt/local/bin/Rscript",
  ],
);
export const PYTHON_EXECUTABLE = configuredRuntime(
  "EXPDESIGN_PYTHON",
  "python3",
  [
    join(SUITE_ROOT, "agent-harness", ".venv", "bin", "python"),
    "/opt/homebrew/bin/python3",
    "/usr/local/bin/python3",
    "/usr/bin/python3",
    "/opt/local/bin/python3",
  ],
);

const APPROVED_CHILD_PATH = [
  "/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin",
  "/usr/local/bin", "/opt/local/bin",
  "/Library/Frameworks/R.framework/Resources/bin",
].join(":");

function removeNativeLoaderEnvironment(environment: NodeJS.ProcessEnv): void {
  for (const name of Object.keys(environment)) {
    const normalized = name.toUpperCase();
    if (normalized.startsWith("LD_") || normalized.startsWith("DYLD_")) {
      delete environment[name];
    }
  }
}

/**
 * Build the environment for every R process from a closed library search
 * policy. `--vanilla` skips profile files but R still honors R_LIBS variables,
 * so inheriting them would execute an ambient package before provenance is
 * established.
 */
export function sanitizedRChildEnvironment(): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = { ...process.env };
  removeNativeLoaderEnvironment(environment);
  for (const name of [
    "R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER",
    "R_HOME", "R_USER",
  ]) {
    delete environment[name];
  }
  environment.R_LIBS = "";
  environment.R_LIBS_USER = "";
  environment.R_LIBS_SITE = "";
  environment.R_DEFAULT_PACKAGES = "datasets,utils,grDevices,graphics,stats,methods";
  if (process.platform !== "win32") environment.PATH = APPROVED_CHILD_PATH;
  return environment;
}

/** Prevent Python/site and native-loader injection into fingerprint probes. */
export function sanitizedPythonChildEnvironment(): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = { ...process.env };
  removeNativeLoaderEnvironment(environment);
  for (const name of Object.keys(environment)) {
    if (name.toUpperCase().startsWith("PYTHON") || name === "__PYVENV_LAUNCHER__") {
      delete environment[name];
    }
  }
  if (process.platform !== "win32") environment.PATH = APPROVED_CHILD_PATH;
  return environment;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([a], [b]) => a.localeCompare(b));
    return `{${entries.map(([key, item]) =>
      `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function regularTreeFiles(path: string, seen = new Set<string>()): string[] {
  const canonical = realpathSync(path);
  const info = statSync(canonical);
  if (info.isFile()) return [canonical];
  if (!info.isDirectory() || seen.has(canonical)) return [];
  seen.add(canonical);
  return readdirSync(canonical).flatMap((name) =>
    regularTreeFiles(join(canonical, name), seen));
}

/** Hash unambiguous length-prefixed fields rather than raw concatenation. */
export function hashFramedFields(
  fields: ReadonlyArray<string | Uint8Array>,
  seed = "",
): string {
  const hash = createHash("sha256");
  for (const field of [seed, ...fields]) {
    const bytes = typeof field === "string" ? Buffer.from(field, "utf8") : Buffer.from(field);
    const length = Buffer.alloc(8);
    length.writeBigUInt64BE(BigInt(bytes.length));
    hash.update(length).update(bytes);
  }
  return hash.digest("hex");
}

function hashRuntimeFiles(files: string[], seed: string): string {
  return hashFramedFields(
    files.sort().flatMap((path) => [path, readFileSync(path)]),
    seed,
  );
}

/**
 * Hash the Python import closure without allowing a module path to turn the
 * fingerprint step into an unbounded or uninterruptible read. Each descriptor
 * is bound to the canonical name before and after the read, and metadata drift
 * during the read fails closed.
 */
async function hashBoundedPythonRuntimeFiles(
  files: readonly string[],
  seed: string,
  signal?: AbortSignal,
): Promise<string> {
  if (signal?.aborted) throw runtimeProbeAbortError();
  const canonicalFiles = [...new Set(files.map((path) => realpathSync(path)))].sort();
  if (canonicalFiles.length > MAX_PYTHON_RUNTIME_FILES) {
    throw new Error(
      `Python runtime fingerprint exceeds the ${MAX_PYTHON_RUNTIME_FILES}-file limit`,
    );
  }

  const hash = createHash("sha256");
  const updateFieldPrefix = (length: number | bigint) => {
    const header = Buffer.alloc(8);
    header.writeBigUInt64BE(BigInt(length));
    hash.update(header);
  };
  const updateCompleteField = (bytes: Buffer) => {
    updateFieldPrefix(bytes.length);
    hash.update(bytes);
  };
  updateCompleteField(Buffer.from(seed, "utf8"));

  const noFollow = typeof fsConstants.O_NOFOLLOW === "number"
    ? fsConstants.O_NOFOLLOW : 0;
  const nonBlock = typeof fsConstants.O_NONBLOCK === "number"
    ? fsConstants.O_NONBLOCK : 0;
  let totalBytes = 0n;
  for (const path of canonicalFiles) {
    if (signal?.aborted) throw runtimeProbeAbortError();
    const handle = await openFile(path, fsConstants.O_RDONLY | noFollow | nonBlock);
    try {
      const opened = await handle.stat({ bigint: true });
      const named = lstatSync(path, { bigint: true });
      if (!opened.isFile() || !named.isFile()) {
        throw new Error("Python runtime fingerprint path is not a regular file");
      }
      if (opened.dev !== named.dev || opened.ino !== named.ino) {
        throw new Error("Python runtime fingerprint path changed while it was opened");
      }
      if (opened.size < 0n || opened.size > BigInt(MAX_PYTHON_RUNTIME_BYTES) ||
          totalBytes + opened.size > BigInt(MAX_PYTHON_RUNTIME_BYTES)) {
        throw new Error(
          `Python runtime fingerprint exceeds the ${MAX_PYTHON_RUNTIME_BYTES}-byte limit`,
        );
      }
      totalBytes += opened.size;

      updateCompleteField(Buffer.from(path, "utf8"));
      updateFieldPrefix(opened.size);
      let offset = 0n;
      while (offset < opened.size) {
        if (signal?.aborted) throw runtimeProbeAbortError();
        const requested = Number(
          opened.size - offset > BigInt(PYTHON_RUNTIME_READ_CHUNK_BYTES)
            ? BigInt(PYTHON_RUNTIME_READ_CHUNK_BYTES)
            : opened.size - offset,
        );
        const chunk = Buffer.allocUnsafe(requested);
        const { bytesRead } = await handle.read(chunk, 0, requested, Number(offset));
        if (bytesRead === 0) {
          throw new Error("Python runtime file changed while it was being read");
        }
        hash.update(chunk.subarray(0, bytesRead));
        offset += BigInt(bytesRead);
      }

      const completed = await handle.stat({ bigint: true });
      const rebound = lstatSync(path, { bigint: true });
      if (!completed.isFile() || !rebound.isFile() ||
          completed.dev !== opened.dev || completed.ino !== opened.ino ||
          completed.size !== opened.size || completed.mtimeNs !== opened.mtimeNs ||
          completed.ctimeNs !== opened.ctimeNs ||
          rebound.dev !== opened.dev || rebound.ino !== opened.ino ||
          rebound.size !== opened.size || rebound.mtimeNs !== opened.mtimeNs ||
          rebound.ctimeNs !== opened.ctimeNs) {
        throw new Error("Python runtime file changed while it was being read");
      }
    } finally {
      await handle.close();
    }
  }
  if (signal?.aborted) throw runtimeProbeAbortError();
  return hash.digest("hex");
}

/** Resolve and hash the runtime that a fresh --vanilla R child will execute. */
export async function currentRRuntimeSnapshot(
  signal?: AbortSignal,
): Promise<RRuntimeSnapshot> {
  const expression = [
    `requested <- c(${TRACKED_R_PACKAGES.map((name) => JSON.stringify(name)).join(",")})`,
    `defaults <- c(${TRACKED_R_BASE_PACKAGES.map((name) => JSON.stringify(name)).join(",")})`,
    "installed <- utils::installed.packages()",
    "roots <- intersect(requested, rownames(installed))",
    "dependencies <- if (length(roots)) unname(unlist(tools::package_dependencies(roots, db=installed, recursive=TRUE), use.names=FALSE)) else character()",
    "packages <- sort(unique(c(defaults, requested, dependencies[!is.na(dependencies) & dependencies != 'R'])))",
    "available <- vapply(packages, requireNamespace, logical(1), quietly=TRUE)",
    "versions <- setNames(lapply(packages, function(p) if (available[[p]]) as.character(utils::packageVersion(p)) else NULL), packages)",
    "paths <- setNames(lapply(packages, function(p) if (available[[p]]) normalizePath(find.package(p), winslash='/', mustWork=TRUE) else NULL), packages)",
    "rscript <- normalizePath(file.path(R.home('bin'), paste0('Rscript', if (.Platform$OS.type == 'windows') '.exe' else '')), winslash='/', mustWork=TRUE)",
    "runtime_candidates <- c(file.path(R.home('bin'), 'exec', 'R'), file.path(R.home('bin'), 'R'), file.path(R.home('bin'), 'R.exe'), file.path(R.home('bin'), 'x64', 'R.exe'), file.path(R.home('lib'), 'libR.so'), file.path(R.home('lib'), 'libR.dylib'), file.path(R.home('bin'), 'R.dll'), file.path(R.home('bin'), 'x64', 'R.dll'))",
    "runtime_files <- unname(normalizePath(runtime_candidates[file.exists(runtime_candidates)], winslash='/', mustWork=TRUE))",
    "state <- list(version=R.version.string, home=normalizePath(R.home(), winslash='/', mustWork=TRUE), rscript=rscript, runtime_files=I(runtime_files), lib_paths=unname(normalizePath(.libPaths(), winslash='/', mustWork=TRUE)), package_versions=versions, package_paths=paths)",
    "cat(jsonlite::toJSON(state, auto_unbox=TRUE, null='null', na='null'))",
  ].join("; ");
  const outcome = await runRuntimeProbe(
    RSCRIPT_EXECUTABLE,
    ["--vanilla", "-e", expression],
    sanitizedRChildEnvironment(),
    signal,
  );
  if (outcome.code !== 0) {
    throw new Error(`R runtime fingerprint failed: ${(outcome.stderr || "unknown error").trim()}`);
  }
  let state: Record<string, unknown>;
  try {
    state = JSON.parse(outcome.stdout) as Record<string, unknown>;
  } catch {
    throw new Error("R runtime fingerprint returned invalid JSON");
  }
  const packagePaths = state.package_paths;
  const packageVersions = state.package_versions;
  const runtimeFiles = typeof state.runtime_files === "string"
    ? [state.runtime_files]
    : Array.isArray(state.runtime_files)
      ? state.runtime_files.filter((path): path is string => typeof path === "string")
      : [];
  if (typeof state.version !== "string" || typeof state.rscript !== "string" ||
      runtimeFiles.length < 1 ||
      !packagePaths || typeof packagePaths !== "object" || Array.isArray(packagePaths) ||
      !packageVersions || typeof packageVersions !== "object" || Array.isArray(packageVersions)) {
    throw new Error("R runtime fingerprint returned an incomplete state");
  }
  state.invoked_rscript = RSCRIPT_EXECUTABLE;
  const roots = [RSCRIPT_EXECUTABLE, state.rscript, ...runtimeFiles, ...Object.values(packagePaths)]
    .filter((path): path is string => typeof path === "string");
  const files = [...new Set(roots.flatMap((path) => regularTreeFiles(path)))];
  return {
    fingerprint: hashRuntimeFiles(files, stableJson(state)),
    version: state.version,
    packageVersions: packageVersions as Record<string, string | null>,
  };
}

const PYTHON_VERIFIER = join(SUITE_ROOT, "agent-harness", "server_verify.py");

/**
 * Bind the interpreter, shared runtime, and the file-backed import closure used
 * by each fresh Python verifier. The path override exists only so lifecycle
 * tests can prove imported dependency drift is covered; production callers use
 * the fixed audited verifier above.
 */
export async function currentPythonRuntimeSnapshot(
  signal?: AbortSignal,
  verifierPath = PYTHON_VERIFIER,
): Promise<PythonRuntimeSnapshot> {
  if (!isAbsolute(verifierPath)) {
    throw new Error("Python runtime verifier path must be absolute");
  }
  const canonicalVerifier = realpathSync(verifierPath);
  if (!statSync(canonicalVerifier).isFile()) {
    throw new Error("Python runtime verifier path is not a regular file");
  }
  const expression = `
import sys
entry = sys.argv[1]
# A -c child starts with an empty-string path. Replace it with the verifier's
# directory so import resolution matches Python's normal script mode and
# never gains an ambient current-working-directory entry.
sys.path[0] = sys.argv[2]
scope = {
    "__name__": "_expdesign_verifier_probe",
    "__file__": entry,
    "__package__": None,
    "__spec__": None,
}
with open(entry, "rb") as source:
    code = compile(source.read(), entry, "exec")
exec(code, scope, scope)

# gates.py imports NormalDist only on the indirect/meta-analysis paths. Load it
# here so the fingerprint covers that audited deferred dependency before any
# request can select one of those paths.
__import__("statistics")

# Capture the modules needed by startup, the verifier, its maintained local
# imports, and its deferred statistics branch before importing probe-only
# sysconfig below.
import json
import os
module_files = {entry}
for module in tuple(sys.modules.values()):
    spec = getattr(module, "__spec__", None)
    for candidate in (
        getattr(module, "__file__", None),
        getattr(module, "__cached__", None),
        getattr(spec, "origin", None),
    ):
        if isinstance(candidate, str) and os.path.isfile(candidate):
            module_files.add(os.path.realpath(candidate))

import sysconfig
library = os.path.join(
    sysconfig.get_config_var("LIBDIR") or "",
    sysconfig.get_config_var("LDLIBRARY") or "",
)
runtime_files = sorted({
    os.path.realpath(path)
    for path in (sys.executable, library)
    if path and os.path.isfile(path)
})
print(json.dumps({
    "version": sys.version,
    "executable": os.path.realpath(sys.executable),
    "verifier_entry": entry,
    "runtime_files": runtime_files,
    "module_files": sorted(module_files),
}, sort_keys=True))
`.trim();
  const outcome = await runRuntimeProbe(
    PYTHON_EXECUTABLE,
    [
      "-E", "-s", "-S", "-c", expression,
      canonicalVerifier, dirname(canonicalVerifier),
    ],
    sanitizedPythonChildEnvironment(),
    signal,
  );
  if (outcome.code !== 0) {
    throw new Error(`Python runtime fingerprint failed: ${(outcome.stderr || "unknown error").trim()}`);
  }
  let state: Record<string, unknown>;
  try {
    state = JSON.parse(outcome.stdout) as Record<string, unknown>;
  } catch {
    throw new Error("Python runtime fingerprint returned invalid JSON");
  }
  const runtimeFiles = state.runtime_files;
  const moduleFiles = state.module_files;
  if (typeof state.version !== "string" || typeof state.executable !== "string" ||
      state.verifier_entry !== canonicalVerifier ||
      !Array.isArray(runtimeFiles) || runtimeFiles.length < 1 ||
      runtimeFiles.some((path) => typeof path !== "string") ||
      !Array.isArray(moduleFiles) || moduleFiles.length < 1 ||
      moduleFiles.some((path) => typeof path !== "string")) {
    throw new Error("Python runtime fingerprint returned an incomplete state");
  }
  state.invoked_python = PYTHON_EXECUTABLE;
  const files = [
    PYTHON_EXECUTABLE,
    state.executable,
    ...runtimeFiles,
    ...moduleFiles,
  ] as string[];
  return {
    fingerprint: await hashBoundedPythonRuntimeFiles(
      files, stableJson(state), signal,
    ),
    version: state.version,
  };
}

/**
 * Maintained, deterministic source manifest for non-R analysis, orchestration,
 * and verification behavior. Keep categories explicit so a newly introduced
 * coordinator boundary cannot silently fall outside runtime provenance.
 */
export const ENGINE_RUNTIME_RELATIVE_FILE_CATEGORIES = {
  core: [
    "mcp-server/launch-server.sh",
    "mcp-server/r-wrapper/dispatcher.R",
    "mcp-server/src/runtime-supervisor.ts",
  ],
  agentHarness: [
    "agent-harness/artifact_download.py",
    "agent-harness/audit.py",
    "agent-harness/final_report.py",
    "agent-harness/gates.py",
    "agent-harness/handoff.py",
    "agent-harness/harness.py",
    "agent-harness/mcp_client.py",
    "agent-harness/multi_agent_harness.py",
    "agent-harness/server_verify.py",
    "agent-harness/verification.py",
  ],
  governance: [
    "governance/__init__.py",
    "governance/agents.json",
    "governance/registry.py",
  ],
  verificationHooks: [
    "hooks/describe_domain_policy.mjs",
    "hooks/domain_tool_policy.mjs",
    "hooks/enforce_verification.py",
    "hooks/launch_verification.mjs",
    "hooks/launch_verification.sh",
    "hooks/private_state.py",
    "hooks/prompt_binding.py",
    "hooks/prompt_binding_hook.py",
    "hooks/record_verification.py",
    "hooks/verification_ledger.py",
    "hooks/verification_policy.py",
  ],
  agentDefinitions: [
    ".claude/settings.json",
    ".claude/agents/design-verifier.md",
    ".claude/agents/doe-designer.md",
    ".claude/agents/experiment-design-coordinator.md",
    ".claude/agents/experiment-designer.md",
    ".claude/agents/indirect-comparison-analyst.md",
    ".claude/agents/master-protocol-designer.md",
    ".claude/agents/meta-analysis-analyst.md",
    ".claude/agents/randomization-planner.md",
    ".claude/agents/single-endpoint-designer.md",
  ],
} as const;

/** Files whose bytes determine analysis, orchestration, or verification behavior. */
export function mutableEngineFiles(suiteRoot: string): string[] {
  const files: string[] = [
    ...Object.values(ENGINE_RUNTIME_RELATIVE_FILE_CATEGORIES)
      .flatMap((category) => category)
      .map((path) => join(suiteRoot, ...path.split("/"))),
    ...REGRESSION_SKILLS.map((skill) =>
      join(suiteRoot, skill, "scripts", "tests", "run_tests.R")),
  ];
  for (const skill of readdirSync(suiteRoot).filter((name) => name.startsWith("vera-"))) {
    const rRoot = join(suiteRoot, skill, "scripts", "R");
    try {
      for (const name of readdirSync(rRoot).filter((item) => item.endsWith(".R")).sort()) {
        files.push(join(rRoot, name));
      }
    } catch { /* skill without an R runtime */ }
  }
  return files;
}

/** Hash the complete maintained engine manifest using root-relative path labels. */
export function fingerprintMutableEngineRuntime(suiteRoot: string, seed = ""): string {
  const root = resolve(suiteRoot);
  const fields: Array<string | Uint8Array> = [];
  for (const path of mutableEngineFiles(root).sort()) {
    const absolute = resolve(path);
    const label = relative(root, absolute).split(sep).join("/");
    if (!label || label === ".." || label.startsWith("../")) {
      throw new Error("engine runtime source escaped the suite root");
    }
    fields.push(label, readFileSync(absolute));
  }
  return hashFramedFields(fields, seed);
}

/** One resolved value plus one shared in-flight load for each fingerprint. */
export class FingerprintPromiseCache<T> {
  private resolved?: { fingerprint: string; value: T };
  private readonly inFlight = new Map<string, Promise<T>>();
  private generation = 0;

  async get(fingerprint: string, loader: () => Promise<T>): Promise<T> {
    if (this.resolved?.fingerprint === fingerprint) return this.resolved.value;
    const current = this.inFlight.get(fingerprint);
    if (current) return current;

    const generation = ++this.generation;
    const pending = (async () => {
      const value = await loader();
      // A newer fingerprint request must not be overwritten by an older load
      // that happened to finish later.
      if (generation === this.generation) {
        this.resolved = { fingerprint, value };
      }
      return value;
    })();
    this.inFlight.set(fingerprint, pending);
    try {
      return await pending;
    } finally {
      if (this.inFlight.get(fingerprint) === pending) {
        this.inFlight.delete(fingerprint);
      }
    }
  }
}

/** Let each caller cancel its wait without cancelling shared single-flight work. */
export function waitForSharedPromise<T>(
  promise: Promise<T>,
  signal?: AbortSignal,
): Promise<T> {
  if (!signal) return promise;
  const abortError = () => {
    const error = new Error("MCP request cancelled while awaiting shared verification");
    error.name = "AbortError";
    return error;
  };
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<T>((resolvePromise, reject) => {
    const onAbort = () => {
      signal.removeEventListener("abort", onAbort);
      reject(abortError());
    };
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(
      (value) => {
        signal.removeEventListener("abort", onAbort);
        resolvePromise(value);
      },
      (error) => {
        signal.removeEventListener("abort", onAbort);
        reject(error);
      },
    );
  });
}
