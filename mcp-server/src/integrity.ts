import { createHash } from "node:crypto";
import {
  accessSync,
  type BigIntStats,
  constants as fsConstants,
  lstatSync,
  realpathSync,
  statSync,
} from "node:fs";
import { open as openFile, opendir } from "node:fs/promises";
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
const MAX_R_RUNTIME_FILES = 32_768;
const MAX_R_RUNTIME_DIRECTORIES = 8_192;
const MAX_R_RUNTIME_DIRECTORY_ENTRIES = 65_536;
const MAX_R_RUNTIME_DEPTH = 64;
const MAX_R_RUNTIME_FILE_BYTES = 256 * 1024 * 1024;
const MAX_R_RUNTIME_BYTES = 1024 * 1024 * 1024;
const R_RUNTIME_READ_CHUNK_BYTES = 64 * 1024;
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

function controlledBaseChildEnvironment(): NodeJS.ProcessEnv {
  if (process.platform === "win32") {
    const configuredRoot = process.env.SystemRoot ?? process.env.SYSTEMROOT;
    if (!configuredRoot || !isAbsolute(configuredRoot)) {
      throw new Error("Windows runtime children require an absolute SystemRoot");
    }
    const systemRoot = realpathSync(configuredRoot);
    if (!statSync(systemRoot).isDirectory()) {
      throw new Error("Windows runtime SystemRoot is not a directory");
    }
    const system32 = join(systemRoot, "System32");
    return {
      SystemRoot: systemRoot,
      WINDIR: systemRoot,
      ComSpec: join(system32, "cmd.exe"),
      PATHEXT: ".COM;.EXE;.BAT;.CMD",
      PATH: `${system32};${systemRoot}`,
      HOME: join(system32, "config", "systemprofile"),
      USERPROFILE: join(system32, "config", "systemprofile"),
      TEMP: join(systemRoot, "Temp"),
      TMP: join(systemRoot, "Temp"),
      LANG: "C",
      LC_ALL: "C",
      TZ: "UTC",
    };
  }

  let controlledHome = "/";
  try {
    if (statSync("/var/empty").isDirectory()) controlledHome = "/var/empty";
  } catch { /* the immutable root remains the fail-closed fallback */ }
  return {
    PATH: APPROVED_CHILD_PATH,
    HOME: controlledHome,
    TMPDIR: "/tmp",
    TMP: "/tmp",
    TEMP: "/tmp",
    LANG: "C",
    LC_ALL: "C",
    TZ: "UTC",
  };
}

function fixedSingleThreadEnvironment(): NodeJS.ProcessEnv {
  return {
    OMP_NUM_THREADS: "1",
    OPENBLAS_NUM_THREADS: "1",
    MKL_NUM_THREADS: "1",
    VECLIB_MAXIMUM_THREADS: "1",
    NUMEXPR_NUM_THREADS: "1",
    BLIS_NUM_THREADS: "1",
  };
}

/**
 * Build the environment for every R process from a closed library search
 * policy. `--vanilla` skips profile files but R still honors R_LIBS variables,
 * so inheriting them would execute an ambient package before provenance is
 * established.
 */
export function sanitizedRChildEnvironment(): NodeJS.ProcessEnv {
  return {
    ...controlledBaseChildEnvironment(),
    ...fixedSingleThreadEnvironment(),
    R_LIBS: "",
    R_LIBS_USER: "",
    R_LIBS_SITE: "",
    R_DEFAULT_PACKAGES: "datasets,utils,grDevices,graphics,stats,methods",
  };
}

/** Prevent Python/site and native-loader injection into fingerprint probes. */
export function sanitizedPythonChildEnvironment(): NodeJS.ProcessEnv {
  return {
    ...controlledBaseChildEnvironment(),
    ...fixedSingleThreadEnvironment(),
  };
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

export type RuntimeFingerprintLimits = {
  maxFiles: number;
  maxDirectories: number;
  maxDirectoryEntries: number;
  maxDepth: number;
  maxFileBytes: number;
  maxTotalBytes: number;
  readChunkBytes: number;
};

function assertRuntimeFingerprintLimits(limits: RuntimeFingerprintLimits): void {
  for (const [name, value] of Object.entries(limits)) {
    if (!Number.isSafeInteger(value) || value < 1) {
      throw new Error(`Runtime fingerprint ${name} must be a positive safe integer`);
    }
  }
  if (limits.maxFileBytes > limits.maxTotalBytes) {
    throw new Error("Runtime fingerprint maxFileBytes exceeds maxTotalBytes");
  }
}

function sameRuntimeMetadata(left: BigIntStats, right: BigIntStats): boolean {
  return left.dev === right.dev && left.ino === right.ino &&
    left.size === right.size && left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs;
}

/**
 * Incrementally hash a bounded set of runtime files or directory trees.
 * Symlinks are never followed. Every regular file is opened with the strongest
 * no-follow/nonblocking flags available, then rebound to its pathname before
 * and after chunked reads. Directories receive the same descriptor/name checks
 * while their bounded, sorted children are traversed.
 */
export async function hashBoundedRuntimeTree(
  roots: readonly string[],
  seed: string,
  limits: RuntimeFingerprintLimits,
  signal?: AbortSignal,
  labelRoot?: string,
): Promise<string> {
  assertRuntimeFingerprintLimits(limits);
  if (signal?.aborted) throw runtimeProbeAbortError();

  const noFollow = typeof fsConstants.O_NOFOLLOW === "number"
    ? fsConstants.O_NOFOLLOW : 0;
  const nonBlock = typeof fsConstants.O_NONBLOCK === "number"
    ? fsConstants.O_NONBLOCK : 0;
  const directoryOnly = typeof fsConstants.O_DIRECTORY === "number"
    ? fsConstants.O_DIRECTORY : 0;
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

  let fileCount = 0;
  let directoryCount = 0;
  let directoryEntryCount = 0;
  let totalBytes = 0n;
  const visitedFiles = new Set<string>();
  const visitedDirectories = new Set<string>();
  const maximumFileBytes = BigInt(limits.maxFileBytes);
  const maximumTotalBytes = BigInt(limits.maxTotalBytes);
  let canonicalLabelRoot: string | undefined;
  if (labelRoot !== undefined) {
    if (!isAbsolute(labelRoot)) {
      throw new Error("Runtime fingerprint label root must be absolute");
    }
    const absoluteLabelRoot = resolve(labelRoot);
    canonicalLabelRoot = realpathSync(labelRoot);
    if (process.platform !== "win32" && canonicalLabelRoot !== absoluteLabelRoot) {
      throw new Error("Runtime fingerprint label root contains symbolic-link components");
    }
  }

  const fingerprintLabel = (path: string): string => {
    if (canonicalLabelRoot === undefined) return path;
    const label = relative(canonicalLabelRoot, path).split(sep).join("/");
    if (!label || label === ".." || label.startsWith("../")) {
      throw new Error("Runtime fingerprint source escaped its label root");
    }
    return label;
  };

  const assertUnchanged = (
    path: string,
    opened: BigIntStats,
    expectedKind: "file" | "directory",
    phase: string,
  ) => {
    const named = lstatSync(path, { bigint: true });
    const correctKind = expectedKind === "file"
      ? opened.isFile() && named.isFile()
      : opened.isDirectory() && named.isDirectory();
    if (!correctKind || named.isSymbolicLink() || !sameRuntimeMetadata(opened, named)) {
      throw new Error(`Runtime fingerprint ${expectedKind} changed ${phase}: ${path}`);
    }
  };

  const visit = async (path: string, depth: number): Promise<void> => {
    if (signal?.aborted) throw runtimeProbeAbortError();
    if (depth > limits.maxDepth) {
      throw new Error(
        `Runtime fingerprint tree exceeds the ${limits.maxDepth}-level depth limit`,
      );
    }
    const namedBeforeOpen = lstatSync(path, { bigint: true });
    if (namedBeforeOpen.isSymbolicLink()) {
      throw new Error(`Runtime fingerprint refuses symbolic links: ${path}`);
    }
    if (!namedBeforeOpen.isFile() && !namedBeforeOpen.isDirectory()) {
      throw new Error(`Runtime fingerprint path is not a regular file or directory: ${path}`);
    }

    const directory = namedBeforeOpen.isDirectory();
    let handle;
    try {
      handle = await openFile(
        path,
        fsConstants.O_RDONLY | noFollow | nonBlock | (directory ? directoryOnly : 0),
      );
    } catch (error) {
      // Windows does not expose a directory descriptor through fs.open. It
      // still receives named-path checks before and after bounded opendir;
      // regular files always require a descriptor on every platform.
      if (!(directory && process.platform === "win32")) throw error;
    }

    try {
      const opened = handle
        ? await handle.stat({ bigint: true })
        : namedBeforeOpen;
      if (!sameRuntimeMetadata(namedBeforeOpen, opened)) {
        throw new Error(`Runtime fingerprint path changed while opening: ${path}`);
      }
      assertUnchanged(path, opened, directory ? "directory" : "file", "while opening");

      if (!directory) {
        if (visitedFiles.has(path)) return;
        visitedFiles.add(path);
        fileCount += 1;
        if (fileCount > limits.maxFiles) {
          throw new Error(
            `Runtime fingerprint tree exceeds the ${limits.maxFiles}-file limit`,
          );
        }
        if (opened.size < 0n || opened.size > maximumFileBytes) {
          throw new Error(
            `Runtime fingerprint file exceeds the ${limits.maxFileBytes}-byte limit`,
          );
        }
        if (totalBytes + opened.size > maximumTotalBytes) {
          throw new Error(
            `Runtime fingerprint tree exceeds the ${limits.maxTotalBytes}-byte limit`,
          );
        }
        totalBytes += opened.size;
        updateCompleteField(Buffer.from(fingerprintLabel(path), "utf8"));
        updateFieldPrefix(opened.size);

        if (!handle) {
          throw new Error("Runtime fingerprint regular file descriptor is unavailable");
        }
        let offset = 0n;
        while (offset < opened.size) {
          if (signal?.aborted) throw runtimeProbeAbortError();
          const remaining = opened.size - offset;
          const requested = Number(
            remaining > BigInt(limits.readChunkBytes)
              ? BigInt(limits.readChunkBytes)
              : remaining,
          );
          const chunk = Buffer.allocUnsafe(requested);
          const { bytesRead } = await handle.read(chunk, 0, requested, Number(offset));
          if (bytesRead === 0) {
            throw new Error(`Runtime fingerprint file changed while reading: ${path}`);
          }
          hash.update(chunk.subarray(0, bytesRead));
          offset += BigInt(bytesRead);
        }
        if (signal?.aborted) throw runtimeProbeAbortError();
        const completed = await handle.stat({ bigint: true });
        if (!sameRuntimeMetadata(opened, completed)) {
          throw new Error(`Runtime fingerprint file changed while reading: ${path}`);
        }
        assertUnchanged(path, opened, "file", "while reading");
        return;
      }

      const directoryIdentity = `${opened.dev}:${opened.ino}`;
      if (visitedDirectories.has(directoryIdentity)) return;
      visitedDirectories.add(directoryIdentity);
      directoryCount += 1;
      if (directoryCount > limits.maxDirectories) {
        throw new Error(
          `Runtime fingerprint tree exceeds the ${limits.maxDirectories}-directory limit`,
        );
      }

      const names: string[] = [];
      const stream = await opendir(path);
      try {
        for await (const entry of stream) {
          if (signal?.aborted) throw runtimeProbeAbortError();
          directoryEntryCount += 1;
          if (directoryEntryCount > limits.maxDirectoryEntries) {
            throw new Error(
              "Runtime fingerprint tree exceeds the " +
              `${limits.maxDirectoryEntries}-directory-entry limit`,
            );
          }
          if (entry.isSymbolicLink()) {
            throw new Error(
              `Runtime fingerprint refuses symbolic links: ${join(path, entry.name)}`,
            );
          }
          names.push(entry.name);
        }
      } finally {
        await stream.close().catch(() => undefined);
      }
      names.sort();
      for (const name of names) {
        if (signal?.aborted) throw runtimeProbeAbortError();
        await visit(join(path, name), depth + 1);
      }
      if (signal?.aborted) throw runtimeProbeAbortError();
      const completed = handle
        ? await handle.stat({ bigint: true })
        : lstatSync(path, { bigint: true });
      if (!sameRuntimeMetadata(opened, completed)) {
        throw new Error(`Runtime fingerprint directory changed while reading: ${path}`);
      }
      assertUnchanged(path, opened, "directory", "while reading");
    } finally {
      await handle?.close();
    }
  };

  const canonicalRoots: string[] = [];
  if (BigInt(roots.length) > BigInt(limits.maxFiles) + BigInt(limits.maxDirectories)) {
    throw new Error("Runtime fingerprint root list exceeds its structural limits");
  }
  for (const root of roots) {
    if (signal?.aborted) throw runtimeProbeAbortError();
    if (!isAbsolute(root)) {
      throw new Error(`Runtime fingerprint root must be absolute: ${root}`);
    }
    const named = lstatSync(root, { bigint: true });
    if (named.isSymbolicLink()) {
      throw new Error(`Runtime fingerprint refuses symbolic links: ${root}`);
    }
    const absolute = resolve(root);
    const canonical = realpathSync(root);
    if (process.platform !== "win32" && canonical !== absolute) {
      throw new Error(`Runtime fingerprint refuses symbolic-link path components: ${root}`);
    }
    canonicalRoots.push(canonical);
  }
  for (const root of [...new Set(canonicalRoots)].sort()) {
    await visit(root, 0);
  }
  if (signal?.aborted) throw runtimeProbeAbortError();
  return hash.digest("hex");
}

async function hashBoundedPythonRuntimeFiles(
  files: readonly string[],
  seed: string,
  signal?: AbortSignal,
): Promise<string> {
  return hashBoundedRuntimeTree(files, seed, {
    maxFiles: MAX_PYTHON_RUNTIME_FILES,
    maxDirectories: 1,
    maxDirectoryEntries: 1,
    maxDepth: 1,
    maxFileBytes: MAX_PYTHON_RUNTIME_BYTES,
    maxTotalBytes: MAX_PYTHON_RUNTIME_BYTES,
    readChunkBytes: PYTHON_RUNTIME_READ_CHUNK_BYTES,
  }, signal);
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
  return {
    fingerprint: await hashBoundedRuntimeTree(roots, stableJson(state), {
      maxFiles: MAX_R_RUNTIME_FILES,
      maxDirectories: MAX_R_RUNTIME_DIRECTORIES,
      maxDirectoryEntries: MAX_R_RUNTIME_DIRECTORY_ENTRIES,
      maxDepth: MAX_R_RUNTIME_DEPTH,
      maxFileBytes: MAX_R_RUNTIME_FILE_BYTES,
      maxTotalBytes: MAX_R_RUNTIME_BYTES,
      readChunkBytes: R_RUNTIME_READ_CHUNK_BYTES,
    }, signal),
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
  return [
    ...Object.values(ENGINE_RUNTIME_RELATIVE_FILE_CATEGORIES)
      .flatMap((category) => category)
      .map((path) => join(suiteRoot, ...path.split("/"))),
    ...REGRESSION_SKILLS.map((skill) =>
      join(suiteRoot, skill, "scripts", "tests", "run_tests.R")),
  ];
}

/** Fixed files plus the five maintained R implementation directory roots. */
export function mutableEngineRoots(suiteRoot: string): string[] {
  return [
    ...mutableEngineFiles(suiteRoot),
    ...REGRESSION_SKILLS.map((skill) =>
      join(suiteRoot, skill, "scripts", "R")),
  ];
}

export const ENGINE_RUNTIME_FINGERPRINT_LIMITS = {
  maxFiles: 4_096,
  maxDirectories: 1_024,
  maxDirectoryEntries: 8_192,
  maxDepth: 32,
  maxFileBytes: 16 * 1024 * 1024,
  maxTotalBytes: 256 * 1024 * 1024,
  readChunkBytes: 64 * 1024,
} as const satisfies RuntimeFingerprintLimits;

/**
 * Hash the complete fixed engine manifest with suite-root-relative labels.
 * Directory discovery is limited to the five named scripts/R roots above;
 * ambient vera-* directories at the suite root are never enumerated.
 */
export async function fingerprintMutableEngineRuntime(
  suiteRoot: string,
  seed = "",
  signal?: AbortSignal,
): Promise<string> {
  const root = realpathSync(resolve(suiteRoot));
  return hashBoundedRuntimeTree(
    mutableEngineRoots(root),
    seed,
    ENGINE_RUNTIME_FINGERPRINT_LIMITS,
    signal,
    root,
  );
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
