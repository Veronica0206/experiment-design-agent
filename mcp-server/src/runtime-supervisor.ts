import {
  spawn,
  spawnSync,
  type ChildProcess,
  type SpawnOptions,
} from "node:child_process";
import { createHash, createHmac, randomUUID } from "node:crypto";
import {
  accessSync,
  closeSync,
  constants as fsConstants,
  fchmodSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  openSync,
  opendirSync,
  readFileSync,
  readSync,
  realpathSync,
  statSync,
  unlinkSync,
  writeSync,
} from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const REGISTRY_DIR_ENV = "EXPDESIGN_RUNTIME_REGISTRY_DIR";
const REGISTRY_TOKEN_ENV = "EXPDESIGN_RUNTIME_REGISTRY_TOKEN";
const REGISTRY_DEV_ENV = "EXPDESIGN_RUNTIME_REGISTRY_DEV";
const REGISTRY_INO_ENV = "EXPDESIGN_RUNTIME_REGISTRY_INO";
const SERVER_PID_ENV = "EXPDESIGN_RUNTIME_SERVER_PID";
const NONCE_ENV = "EXPDESIGN_RUNTIME_NONCE";
const TARGET_CWD_ENV = "EXPDESIGN_RUNTIME_TARGET_CWD";
const INTERNAL_MODE = "--expdesign-runtime-supervisor-v1";
const STOP_BASENAME = ".stop";
const STOP_POLL_MS = 100;
export const MAX_SUPERVISOR_PROGRAM_BYTES = 128 * 1024;
const MAX_PROC_DIRECTORY_ENTRIES = 262_144;
const MAX_PROC_STAT_BYTES = 4_096;
const MAX_PS_OUTPUT_BYTES = 4 * 1024 * 1024;
const MAX_PS_PROCESS_ROWS = 262_144;
const SUPERVISOR_COMMITMENT_DOMAIN =
  "experiment-design/runtime-supervisor-program/v1";
const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,256}$/;
const NONCE_PATTERN = /^[0-9a-f]{32}$/;
const supervisedChildren = new WeakSet<ChildProcess>();

type RuntimeRegistryRecord = {
  version: 1;
  server_pid: number;
  pid: number;
  nonce: string;
  start_identity: string;
  auth: string;
};

type RuntimeSpawnOptions = Pick<SpawnOptions, "cwd" | "env" | "stdio"> & {
  detached: boolean;
};

/**
 * Capture one bounded supervisor program through a pinned descriptor. The
 * optional hook exists for deterministic lifecycle race tests and runs only
 * after the initial descriptor/name/size binding.
 */
export function captureBoundedSupervisorProgram(
  path: string,
  afterInitialBinding?: () => void,
): string {
  if (!isAbsolute(path)) {
    throw new Error("runtime supervisor program path must be absolute");
  }
  const noFollow = typeof fsConstants.O_NOFOLLOW === "number" ? fsConstants.O_NOFOLLOW : 0;
  const nonBlock = typeof fsConstants.O_NONBLOCK === "number" ? fsConstants.O_NONBLOCK : 0;
  const fd = openSync(path, fsConstants.O_RDONLY | noFollow | nonBlock);
  try {
    const before = fstatSync(fd, { bigint: true });
    const namedBefore = lstatSync(path, { bigint: true });
    if (!before.isFile() || !namedBefore.isFile() || namedBefore.isSymbolicLink() ||
        before.dev !== namedBefore.dev || before.ino !== namedBefore.ino ||
        before.size !== namedBefore.size || before.mtimeNs !== namedBefore.mtimeNs ||
        before.ctimeNs !== namedBefore.ctimeNs || before.size < 0n ||
        before.size > BigInt(MAX_SUPERVISOR_PROGRAM_BYTES)) {
      throw new Error("runtime supervisor program is unsafe");
    }
    afterInitialBinding?.();

    const expectedBytes = Number(before.size);
    const bytes = Buffer.allocUnsafe(expectedBytes);
    let offset = 0;
    while (offset < expectedBytes) {
      const count = readSync(fd, bytes, offset, expectedBytes - offset, offset);
      if (count === 0) {
        throw new Error("runtime supervisor program changed while it was captured");
      }
      offset += count;
    }
    const probe = Buffer.allocUnsafe(1);
    if (readSync(fd, probe, 0, 1, expectedBytes) !== 0) {
      throw new Error("runtime supervisor program grew while it was captured");
    }

    const after = fstatSync(fd, { bigint: true });
    const namedAfter = lstatSync(path, { bigint: true });
    if (!after.isFile() || !namedAfter.isFile() || namedAfter.isSymbolicLink() ||
        before.dev !== after.dev || before.ino !== after.ino ||
        before.size !== after.size || before.mtimeNs !== after.mtimeNs ||
        before.ctimeNs !== after.ctimeNs ||
        before.dev !== namedAfter.dev || before.ino !== namedAfter.ino ||
        before.size !== namedAfter.size || before.mtimeNs !== namedAfter.mtimeNs ||
        before.ctimeNs !== namedAfter.ctimeNs) {
      throw new Error("runtime supervisor program changed while it was captured");
    }
    return bytes.toString("utf8");
  } finally {
    closeSync(fd);
  }
}

function captureSupervisorProgram(): string | undefined {
  // A wrapper launched through --eval has INTERNAL_MODE at argv[1] and must
  // not reopen a mutable file. The long-lived server captures these bytes once
  // while its complete dist tree is being bound into the startup fingerprint.
  if (process.argv[1] === INTERNAL_MODE) return undefined;
  return captureBoundedSupervisorProgram(fileURLToPath(import.meta.url));
}

const PINNED_SUPERVISOR_PROGRAM = captureSupervisorProgram();

/** Commit to the exact UTF-8 program string passed to Node's --eval. */
export function runtimeSupervisorProgramCommitment(program: string): string {
  const bytes = Buffer.from(program, "utf8");
  const length = Buffer.alloc(8);
  length.writeBigUInt64BE(BigInt(bytes.length));
  return createHash("sha256")
    .update(SUPERVISOR_COMMITMENT_DOMAIN, "utf8")
    .update(Buffer.from([0]))
    .update(length)
    .update(bytes)
    .digest("hex");
}

// This is deliberately derived from the same immutable string reference used
// by spawnRuntimeProcess. Startup provenance consumes this commitment instead
// of reopening runtime-supervisor.js after dependency evaluation.
export const PINNED_RUNTIME_SUPERVISOR_COMMITMENT =
  PINNED_SUPERVISOR_PROGRAM === undefined
    ? undefined
    : runtimeSupervisorProgramCommitment(PINNED_SUPERVISOR_PROGRAM);

type PrivateRegistryDirectory = {
  dev: bigint;
  ino: bigint;
};

function configuration(environment: NodeJS.ProcessEnv): {
  directory: string;
  token: string;
  dev: bigint;
  ino: bigint;
} | undefined {
  const directory = environment[REGISTRY_DIR_ENV];
  const token = environment[REGISTRY_TOKEN_ENV];
  const rawDev = environment[REGISTRY_DEV_ENV];
  const rawIno = environment[REGISTRY_INO_ENV];
  if ([directory, token, rawDev, rawIno].every((value) => value === undefined)) {
    return undefined;
  }
  if (!directory || !token || !rawDev || !rawIno) {
    throw new Error("runtime supervisor registry configuration is incomplete");
  }
  if (process.platform !== "linux" && process.platform !== "darwin") {
    throw new Error("runtime supervisor is unavailable on this platform");
  }
  if (!isAbsolute(directory)) {
    throw new Error("runtime supervisor registry directory must be absolute");
  }
  if (!TOKEN_PATTERN.test(token)) {
    throw new Error("runtime supervisor registry token is malformed");
  }
  if (!/^[0-9]+$/.test(rawDev) || !/^[1-9][0-9]*$/.test(rawIno)) {
    throw new Error("runtime supervisor registry identity is malformed");
  }
  return { directory, token, dev: BigInt(rawDev), ino: BigInt(rawIno) };
}

function stripSupervisorEnvironment(environment: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const childEnvironment = { ...environment };
  for (const name of [
    REGISTRY_DIR_ENV, REGISTRY_TOKEN_ENV, REGISTRY_DEV_ENV, REGISTRY_INO_ENV,
    SERVER_PID_ENV, NONCE_ENV, TARGET_CWD_ENV,
  ]) {
    delete childEnvironment[name];
  }
  return childEnvironment;
}

function canonicalTargetCwd(cwd: SpawnOptions["cwd"]): string {
  let path: string;
  if (cwd === undefined) path = process.cwd();
  else if (typeof cwd === "string") path = cwd;
  else path = fileURLToPath(cwd);
  const canonical = realpathSync(resolve(path));
  if (!statSync(canonical).isDirectory()) {
    throw new Error("runtime target working directory is not a directory");
  }
  return canonical;
}

/**
 * Spawn a runtime directly for standalone servers, or through the authenticated
 * detached wrapper used by MCPClient forced-shutdown cleanup. The wrapper uses
 * the exact stdio descriptors and working directory requested for the target.
 */
export function spawnRuntimeProcess(
  executable: string,
  args: readonly string[],
  options: RuntimeSpawnOptions,
): ChildProcess {
  if (!isAbsolute(executable)) {
    throw new Error("runtime executable must be absolute");
  }
  const requestedTargetEnvironment = { ...(options.env ?? {}) };
  const registry = configuration(requestedTargetEnvironment) ?? configuration(process.env);
  const targetEnvironment = stripSupervisorEnvironment(requestedTargetEnvironment);
  if (!registry) {
    return spawn(executable, args, { ...options, env: targetEnvironment });
  }

  const nonce = randomUUID().replaceAll("-", "");
  const wrapperEnvironment = { ...targetEnvironment };
  // These values belong only to the authenticated Node wrapper. The final
  // runtime receives requestedTargetEnvironment after the internal names are
  // stripped, so target allowlists never need to carry supervisor secrets.
  wrapperEnvironment[REGISTRY_DIR_ENV] = registry.directory;
  wrapperEnvironment[REGISTRY_TOKEN_ENV] = registry.token;
  wrapperEnvironment[REGISTRY_DEV_ENV] = String(registry.dev);
  wrapperEnvironment[REGISTRY_INO_ENV] = String(registry.ino);
  wrapperEnvironment[SERVER_PID_ENV] = String(process.pid);
  wrapperEnvironment[NONCE_ENV] = nonce;
  wrapperEnvironment[TARGET_CWD_ENV] = canonicalTargetCwd(options.cwd);
  // The wrapper is itself Node code, so inherited Node preload/search options
  // must not run before it can establish the registry boundary.
  delete wrapperEnvironment.NODE_OPTIONS;
  delete wrapperEnvironment.NODE_PATH;

  if (PINNED_SUPERVISOR_PROGRAM === undefined) {
    throw new Error("runtime supervisor program is unavailable");
  }
  const proc = spawn(
    process.execPath,
    [
      "--input-type=module", "--eval", PINNED_SUPERVISOR_PROGRAM, "--",
      INTERNAL_MODE, executable, ...args,
    ],
    {
      ...options,
      // The wrapper and host now share one kernel-pinned registry identity.
      // The original target cwd is restored only after this identity is checked.
      cwd: registry.directory,
      detached: true,
      env: wrapperEnvironment,
    },
  );
  supervisedChildren.add(proc);
  return proc;
}

/** Ask a live wrapper to kill its group while retaining its host-cleaned lease. */
export function killRuntimeProcessTree(
  proc: ChildProcess,
  signal: NodeJS.Signals,
): void {
  try {
    if (supervisedChildren.has(proc)) {
      // SIGTERM is an internal control message to the wrapper. It retains the
      // authenticated entry for host cleanup, then SIGKILLs its complete group.
      if (!proc.kill("SIGTERM")) throw new Error("runtime wrapper is not running");
    } else if (process.platform !== "win32" && proc.pid) {
      process.kill(-proc.pid, signal);
    } else {
      proc.kill(signal);
    }
  } catch {
    try { proc.kill(signal); } catch { /* process group already exited */ }
  }
}

function positivePid(raw: string | undefined, label: string): number {
  if (!raw || !/^[1-9][0-9]*$/.test(raw)) {
    throw new Error(`${label} is malformed`);
  }
  const value = Number(raw);
  if (!Number.isSafeInteger(value)) throw new Error(`${label} is malformed`);
  return value;
}

function linuxStatFields(): string[] {
  const raw = readFileSync("/proc/self/stat", "utf8").trim();
  const commandEnd = raw.lastIndexOf(")");
  if (commandEnd < 0) throw new Error("runtime supervisor process identity is unavailable");
  const fields = raw.slice(commandEnd + 1).trim().split(/\s+/);
  // The suffix starts with field 3 (state): pgrp is field 5 and starttime is 22.
  if (fields.length <= 19 || !/^[0-9]+$/.test(fields[2]) ||
      !/^[0-9]+$/.test(fields[19])) {
    throw new Error("runtime supervisor process identity is malformed");
  }
  return fields;
}

function darwinPs(field: "pgid" | "lstart"): string {
  const result = spawnSync(
    "/bin/ps", ["-o", `${field}=`, "-p", String(process.pid)],
    {
      encoding: "utf8",
      env: { PATH: "/usr/bin:/bin", LANG: "C", LC_ALL: "C" },
      timeout: 2_000,
      maxBuffer: 4_096,
    },
  );
  // Normalize ps's presentation-only spacing exactly as the host validator
  // does, including the double-space day field for days 1-9.
  const value = result.status === 0 ? result.stdout.trim().replace(/\s+/g, " ") : "";
  if (!value) throw new Error("runtime supervisor process identity is unavailable");
  return value;
}

function assertProcessGroupLeader(): string {
  if (process.platform === "linux") {
    const fields = linuxStatFields();
    if (Number(fields[2]) !== process.pid) {
      throw new Error("runtime supervisor is not its process-group leader");
    }
    return `linux:${fields[19]}`;
  }
  if (process.platform === "darwin") {
    if (positivePid(darwinPs("pgid"), "runtime supervisor process group") !== process.pid) {
      throw new Error("runtime supervisor is not its process-group leader");
    }
    return `darwin:${darwinPs("lstart")}`;
  }
  throw new Error("runtime supervisor is unavailable on this platform");
}

function linuxProcessGroup(pid: string): number | undefined {
  const path = `/proc/${pid}/stat`;
  const noFollow = typeof fsConstants.O_NOFOLLOW === "number" ? fsConstants.O_NOFOLLOW : 0;
  let fd: number;
  try {
    fd = openSync(path, fsConstants.O_RDONLY | noFollow);
  } catch (error) {
    if (["ENOENT", "ESRCH"].includes((error as NodeJS.ErrnoException).code ?? "")) {
      return undefined;
    }
    throw error;
  }
  try {
    const bytes = Buffer.alloc(MAX_PROC_STAT_BYTES + 1);
    const count = readSync(fd, bytes, 0, bytes.length, null);
    if (count > MAX_PROC_STAT_BYTES) {
      throw new Error("runtime supervisor process table row exceeded its bound");
    }
    const raw = bytes.subarray(0, count).toString("utf8").trim();
    const commandEnd = raw.lastIndexOf(")");
    if (commandEnd < 0) {
      throw new Error("runtime supervisor process table row is malformed");
    }
    const fields = raw.slice(commandEnd + 1).trim().split(/\s+/);
    if (fields.length <= 2 || !/^[0-9]+$/.test(fields[2])) {
      throw new Error("runtime supervisor process group is malformed");
    }
    const group = Number(fields[2]);
    if (!Number.isSafeInteger(group) || group <= 0) {
      throw new Error("runtime supervisor process group is malformed");
    }
    return group;
  } finally {
    closeSync(fd);
  }
}

/** Return every live member of our PGID except the wrapper itself. */
function remainingProcessGroupMembers(): number[] {
  if (process.platform === "linux") {
    const members: number[] = [];
    const directory = opendirSync("/proc");
    let entries = 0;
    try {
      for (;;) {
        const entry = directory.readSync();
        if (entry === null) break;
        entries += 1;
        if (entries > MAX_PROC_DIRECTORY_ENTRIES) {
          throw new Error("runtime supervisor process table exceeded its bound");
        }
        if (!/^[1-9][0-9]*$/.test(entry.name)) continue;
        const pid = Number(entry.name);
        if (!Number.isSafeInteger(pid) || pid === process.pid) continue;
        const group = linuxProcessGroup(entry.name);
        if (group === process.pid) members.push(pid);
      }
    } finally {
      directory.closeSync();
    }
    return members.sort((left, right) => left - right);
  }
  if (process.platform === "darwin") {
    const result = spawnSync(
      "/bin/ps", ["-A", "-o", "pid=", "-o", "pgid="],
      {
        encoding: "utf8",
        env: { PATH: "/usr/bin:/bin", LANG: "C", LC_ALL: "C" },
        timeout: 2_000,
        maxBuffer: MAX_PS_OUTPUT_BYTES,
      },
    );
    if (result.error || result.status !== 0 || result.signal) {
      throw new Error("runtime supervisor process table is unavailable");
    }
    const lines = result.stdout.split("\n");
    if (lines.length > MAX_PS_PROCESS_ROWS + 1) {
      throw new Error("runtime supervisor process table exceeded its bound");
    }
    const members: number[] = [];
    for (const line of lines) {
      if (!line.trim()) continue;
      const match = /^\s*([1-9][0-9]*)\s+([1-9][0-9]*)\s*$/.exec(line);
      if (!match) throw new Error("runtime supervisor process table row is malformed");
      const pid = Number(match[1]);
      const group = Number(match[2]);
      if (!Number.isSafeInteger(pid) || !Number.isSafeInteger(group)) {
        throw new Error("runtime supervisor process table row is malformed");
      }
      // The bounded ps probe itself briefly inherits our group; it has exited
      // by the time spawnSync returns and is not a runtime descendant.
      if (pid !== process.pid && pid !== result.pid && group === process.pid) {
        members.push(pid);
      }
    }
    return members.sort((left, right) => left - right);
  }
  throw new Error("runtime supervisor process table is unsupported");
}

function validatePrivateRegistryDirectory(
  expected: Pick<PrivateRegistryDirectory, "dev" | "ino">,
): PrivateRegistryDirectory {
  const noFollow = typeof fsConstants.O_NOFOLLOW === "number" ? fsConstants.O_NOFOLLOW : 0;
  const directoryFlag = typeof fsConstants.O_DIRECTORY === "number" ? fsConstants.O_DIRECTORY : 0;
  const fd = openSync(".", fsConstants.O_RDONLY | noFollow | directoryFlag);
  let info: ReturnType<typeof fstatSync>;
  let named: ReturnType<typeof lstatSync>;
  try {
    info = fstatSync(fd, { bigint: true });
    named = lstatSync(".", { bigint: true });
  } finally {
    closeSync(fd);
  }
  const uid = typeof process.getuid === "function" ? BigInt(process.getuid()) : undefined;
  if (!info.isDirectory() || !named.isDirectory() || named.isSymbolicLink() ||
      info.dev !== named.dev || info.ino !== named.ino ||
      info.dev !== expected.dev || info.ino !== expected.ino ||
      (uid !== undefined && (info.uid !== uid || named.uid !== uid)) ||
      (info.mode & 0o777n) !== 0o700n || (named.mode & 0o777n) !== 0o700n) {
    throw new Error("runtime supervisor registry directory is not private");
  }
  return { dev: info.dev, ino: info.ino };
}

function assertRegistryDirectoryCurrent(directory: PrivateRegistryDirectory): void {
  const current = lstatSync(".", { bigint: true });
  if (!current.isDirectory() || current.isSymbolicLink() ||
      current.dev !== directory.dev || current.ino !== directory.ino) {
    throw new Error("runtime supervisor registry directory changed");
  }
}

function directoryEntryExists(path: string): boolean {
  try {
    lstatSync(path);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}

function stopRequested(path: string): boolean {
  if (!directoryEntryExists(path)) return false;
  const noFollow = typeof fsConstants.O_NOFOLLOW === "number" ? fsConstants.O_NOFOLLOW : 0;
  const nonBlock = typeof fsConstants.O_NONBLOCK === "number" ? fsConstants.O_NONBLOCK : 0;
  const fd = openSync(path, fsConstants.O_RDONLY | noFollow | nonBlock);
  try {
    const descriptor = fstatSync(fd, { bigint: true });
    const named = lstatSync(path, { bigint: true });
    const uid = typeof process.getuid === "function" ? BigInt(process.getuid()) : undefined;
    if (!descriptor.isFile() || !named.isFile() || named.isSymbolicLink() ||
        descriptor.dev !== named.dev || descriptor.ino !== named.ino ||
        descriptor.nlink !== 1n || named.nlink !== 1n ||
        (descriptor.mode & 0o777n) !== 0o600n ||
        (uid !== undefined && (descriptor.uid !== uid || named.uid !== uid))) {
      throw new Error("runtime supervisor stop sentinel is unsafe");
    }
    if (descriptor.size !== 5n || readFileSync(fd, "utf8") !== "stop\n") {
      throw new Error("runtime supervisor stop sentinel is malformed");
    }
    const after = fstatSync(fd, { bigint: true });
    if (!after.isFile() || after.dev !== descriptor.dev || after.ino !== descriptor.ino ||
        after.size !== descriptor.size || after.mtimeNs !== descriptor.mtimeNs ||
        after.ctimeNs !== descriptor.ctimeNs || after.nlink !== 1n) {
      throw new Error("runtime supervisor stop sentinel changed while being read");
    }
    return true;
  } finally {
    closeSync(fd);
  }
}

function openRegistryEntry(path: string, record: RuntimeRegistryRecord): {
  fd: number;
  dev: bigint;
  ino: bigint;
} {
  const noFollow = typeof fsConstants.O_NOFOLLOW === "number" ? fsConstants.O_NOFOLLOW : 0;
  const fd = openSync(
    path,
    fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL | noFollow,
    0o600,
  );
  try {
    fchmodSync(fd, 0o600);
    const payload = Buffer.from(`${JSON.stringify(record)}\n`, "utf8");
    let offset = 0;
    while (offset < payload.length) {
      const written = writeSync(fd, payload, offset);
      if (written <= 0) throw new Error("runtime supervisor registry write stalled");
      offset += written;
    }
    fsyncSync(fd);
    const descriptor = fstatSync(fd, { bigint: true });
    const named = lstatSync(path, { bigint: true });
    const uid = typeof process.getuid === "function" ? BigInt(process.getuid()) : undefined;
    if (!descriptor.isFile() || !named.isFile() || named.isSymbolicLink() ||
        descriptor.dev !== named.dev || descriptor.ino !== named.ino ||
        descriptor.nlink !== 1n || named.nlink !== 1n ||
        (descriptor.mode & 0o777n) !== 0o600n ||
        (uid !== undefined && (descriptor.uid !== uid || named.uid !== uid))) {
      throw new Error("runtime supervisor registry entry is unsafe");
    }
    return { fd, dev: descriptor.dev, ino: descriptor.ino };
  } catch (error) {
    try {
      const descriptor = fstatSync(fd, { bigint: true });
      const named = lstatSync(path, { bigint: true });
      if (descriptor.dev === named.dev && descriptor.ino === named.ino) unlinkSync(path);
    } catch { /* leave a missing or replaced entry for host validation */ }
    closeSync(fd);
    throw error;
  }
}

function closeRegistryEntry(
  path: string,
  entry: { fd: number; dev: bigint; ino: bigint },
): void {
  try {
    const descriptor = fstatSync(entry.fd, { bigint: true });
    const named = lstatSync(path, { bigint: true });
    if (!descriptor.isFile() || !named.isFile() || named.isSymbolicLink() ||
        descriptor.dev !== entry.dev || descriptor.ino !== entry.ino ||
        named.dev !== entry.dev || named.ino !== entry.ino ||
        descriptor.nlink !== 1n || named.nlink !== 1n) {
      throw new Error("runtime supervisor registry entry changed before cleanup");
    }
    unlinkSync(path);
    if (fstatSync(entry.fd, { bigint: true }).nlink !== 0n) {
      throw new Error("runtime supervisor registry entry remained linked after cleanup");
    }
  } finally {
    closeSync(entry.fd);
  }
}

function runSupervisor(modeIndex: number): void {
  const registry = configuration(process.env);
  if (!registry) throw new Error("runtime supervisor registry configuration is absent");
  const serverPid = positivePid(process.env[SERVER_PID_ENV], "runtime server pid");
  const nonce = process.env[NONCE_ENV];
  if (!nonce || !NONCE_PATTERN.test(nonce)) {
    throw new Error("runtime supervisor nonce is malformed");
  }
  if (process.ppid !== serverPid) {
    throw new Error("runtime supervisor parent identity does not match its server");
  }
  const executable = process.argv[modeIndex + 1];
  if (!executable || !isAbsolute(executable)) {
    throw new Error("runtime supervisor target executable must be absolute");
  }
  accessSync(executable, fsConstants.X_OK);
  const canonicalExecutable = realpathSync(executable);
  const startIdentity = assertProcessGroupLeader();
  const targetCwd = process.env[TARGET_CWD_ENV];
  if (!targetCwd || !isAbsolute(targetCwd) ||
      realpathSync(targetCwd) !== targetCwd || !statSync(targetCwd).isDirectory()) {
    throw new Error("runtime supervisor target working directory is malformed");
  }
  const registryDirectory = validatePrivateRegistryDirectory(registry);
  const stopPath = STOP_BASENAME;
  if (stopRequested(stopPath)) {
    throw new Error("runtime supervisor host is stopping");
  }

  const entryPath = `runtime-${process.pid}-${nonce}.json`;
  const authenticatedFields = [
    1, serverPid, process.pid, nonce, startIdentity,
  ] as const;
  const entry = openRegistryEntry(entryPath, {
    version: 1,
    server_pid: serverPid,
    pid: process.pid,
    nonce,
    start_identity: startIdentity,
    auth: createHmac("sha256", registry.token)
      .update(JSON.stringify(authenticatedFields), "utf8")
      .digest("hex"),
  });
  assertRegistryDirectoryCurrent(registryDirectory);
  let entryOpen = true;
  let stopping = false;
  let targetSettled = false;
  let target: ChildProcess | undefined;
  let stopPoll: NodeJS.Timeout | undefined;

  const unregister = () => {
    if (!entryOpen) return;
    entryOpen = false;
    closeRegistryEntry(entryPath, entry);
  };
  const killOwnGroup = () => {
    if (stopping) return;
    stopping = true;
    if (stopPoll) clearInterval(stopPoll);
    // Keep the entry linked until the host verifies this exact process identity
    // is dead. Removing it first would create an unregistered-live-group window.
    process.kill(-process.pid, "SIGKILL");
  };

  process.once("SIGTERM", killOwnGroup);
  process.once("SIGINT", killOwnGroup);
  process.once("SIGHUP", killOwnGroup);

  const finishTarget = (code: number | null, signal: NodeJS.Signals | null) => {
    if (targetSettled || stopping) return;
    targetSettled = true;
    if (stopPoll) clearInterval(stopPoll);
    try {
      // The direct target can exit while a background child remains in this
      // detached group. Never erase the only host-authenticated kill handle in
      // that state: retain the lease and kill the complete group instead.
      if (remainingProcessGroupMembers().length > 0) {
        killOwnGroup();
        return;
      }
      unregister();
      if (signal) process.kill(process.pid, signal);
      else process.exitCode = code ?? 1;
    } catch {
      killOwnGroup();
    }
  };

  try {
    assertRegistryDirectoryCurrent(registryDirectory);
    if (stopRequested(stopPath)) {
      throw new Error("runtime supervisor host is stopping");
    }
    const targetEnvironment = stripSupervisorEnvironment(process.env);
    if (process.platform !== "win32") targetEnvironment.PWD = targetCwd;
    target = spawn(canonicalExecutable, process.argv.slice(modeIndex + 2), {
      stdio: ["inherit", "inherit", "inherit"],
      detached: false,
      env: targetEnvironment,
      cwd: targetCwd,
    });
    target.once("error", () => finishTarget(127, null));
    target.once("close", finishTarget);
    stopPoll = setInterval(() => {
      try {
        assertRegistryDirectoryCurrent(registryDirectory);
        if (stopRequested(stopPath)) killOwnGroup();
      } catch {
        // Loss of the authenticated registry boundary is terminal.
        killOwnGroup();
      }
    }, STOP_POLL_MS);
  } catch (error) {
    if (stopPoll) clearInterval(stopPoll);
    try { unregister(); } catch { /* preserve the original startup failure */ }
    throw error;
  }
}

let supervisorModeIndex: number | undefined;
try {
  if (process.argv[1] === INTERNAL_MODE) {
    supervisorModeIndex = 1;
  } else if (process.argv[2] === INTERNAL_MODE &&
      realpathSync(resolve(process.argv[1])) === realpathSync(fileURLToPath(import.meta.url))) {
    supervisorModeIndex = 2;
  }
} catch { /* imports and malformed argv never start a supervisor */ }
if (supervisorModeIndex !== undefined) {
  try {
    runSupervisor(supervisorModeIndex);
  } catch (error) {
    console.error(error instanceof Error ? error.message : "runtime supervisor failed");
    process.exitCode = 1;
  }
}
