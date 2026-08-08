import { createHash } from "node:crypto";
import {
  accessSync,
  constants as fsConstants,
  readdirSync,
  readFileSync,
  realpathSync,
  statSync,
} from "node:fs";
import { spawnSync } from "node:child_process";
import { delimiter, isAbsolute, join, resolve, sep } from "node:path";

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

/** Resolve a child executable once so later spawns cannot select a new PATH entry. */
export function resolveRuntimeExecutable(command: string): string {
  const explicit = isAbsolute(command) || command.includes(sep) || command.includes("/") ||
    command.includes("\\");
  const names = (() => {
    if (process.platform !== "win32" || /\.[A-Za-z0-9]+$/.test(command)) return [command];
    const extensions = (process.env.PATHEXT ?? ".EXE;.CMD;.BAT;.COM")
      .split(";").filter(Boolean);
    return [command, ...extensions.map((extension) => `${command}${extension.toLowerCase()}`),
      ...extensions.map((extension) => `${command}${extension.toUpperCase()}`)];
  })();
  const candidates = explicit
    ? names.map((name) => resolve(name))
    : (process.env.PATH ?? "").split(delimiter).filter(Boolean)
      .flatMap((directory) => names.map((name) => join(directory, name)));
  for (const candidate of candidates) {
    try {
      accessSync(candidate, process.platform === "win32" ? fsConstants.F_OK : fsConstants.X_OK);
      const canonical = realpathSync(candidate);
      if (statSync(canonical).isFile()) return canonical;
    } catch { /* try the next PATH entry */ }
  }
  throw new Error(`required runtime executable is unavailable: ${command}`);
}

export const RSCRIPT_EXECUTABLE = resolveRuntimeExecutable(
  process.env.EXPDESIGN_RSCRIPT ?? "Rscript",
);
export const PYTHON_EXECUTABLE = resolveRuntimeExecutable(
  process.env.EXPDESIGN_PYTHON ?? "python3",
);

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

/** Resolve and hash the runtime that a fresh --vanilla R child will execute. */
export function currentRRuntimeSnapshot(): RRuntimeSnapshot {
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
  const outcome = spawnSync(RSCRIPT_EXECUTABLE, ["--vanilla", "-e", expression], {
    encoding: "utf8", maxBuffer: 1024 * 1024,
  });
  if (outcome.status !== 0) {
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

/** Bind the interpreter and shared runtime used by each fresh Python verifier. */
export function currentPythonRuntimeSnapshot(): PythonRuntimeSnapshot {
  const expression = [
    "import json, os, sys, sysconfig",
    "library = os.path.join(sysconfig.get_config_var('LIBDIR') or '', sysconfig.get_config_var('LDLIBRARY') or '')",
    "files = [os.path.realpath(path) for path in (sys.executable, library) if path and os.path.isfile(path)]",
    "print(json.dumps({'version': sys.version, 'executable': os.path.realpath(sys.executable), 'runtime_files': files}, sort_keys=True))",
  ].join("; ");
  const outcome = spawnSync(PYTHON_EXECUTABLE, ["-E", "-s", "-S", "-c", expression], {
    encoding: "utf8", maxBuffer: 1024 * 1024,
  });
  if (outcome.status !== 0) {
    throw new Error(`Python runtime fingerprint failed: ${(outcome.stderr || "unknown error").trim()}`);
  }
  let state: Record<string, unknown>;
  try {
    state = JSON.parse(outcome.stdout) as Record<string, unknown>;
  } catch {
    throw new Error("Python runtime fingerprint returned invalid JSON");
  }
  const runtimeFiles = Array.isArray(state.runtime_files)
    ? state.runtime_files.filter((path): path is string => typeof path === "string")
    : [];
  if (typeof state.version !== "string" || typeof state.executable !== "string" ||
      runtimeFiles.length < 1) {
    throw new Error("Python runtime fingerprint returned an incomplete state");
  }
  state.invoked_python = PYTHON_EXECUTABLE;
  const files = [...new Set(
    [PYTHON_EXECUTABLE, state.executable, ...runtimeFiles]
      .flatMap((path) => regularTreeFiles(path as string)),
  )];
  return {
    fingerprint: hashRuntimeFiles(files, stableJson(state)),
    version: state.version,
  };
}

/** Files whose bytes determine analysis or verification behavior at runtime. */
export function mutableEngineFiles(suiteRoot: string): string[] {
  const files: string[] = [
    join(suiteRoot, "mcp-server", "r-wrapper", "dispatcher.R"),
    ...["gates.py", "artifact_download.py", "final_report.py", "verification.py",
      "server_verify.py"]
      .map((name) => join(suiteRoot, "agent-harness", name)),
    ...["verification_policy.py", "verification_ledger.py", "record_verification.py",
      "enforce_verification.py", "launch_verification.mjs", "launch_verification.sh"]
      .map((name) => join(suiteRoot, "hooks", name)),
    join(suiteRoot, ".claude", "settings.json"),
    join(suiteRoot, ".claude", "agents", "experiment-designer.md"),
    join(suiteRoot, ".claude", "agents", "design-verifier.md"),
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
