import { realpathSync } from "node:fs";
import { randomUUID } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, rename, rm, stat, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const SUITE_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
export const ARTIFACT_ROOT = resolve(
  process.env.EXPDESIGN_RUNS_DIR ?? join(SUITE_ROOT, "agent-harness", "runs", "artifacts"),
);

let lifecycleQueue: Promise<void> = Promise.resolve();
const activeArtifacts = new Set<string>();
const leaseByArtifact = new Map<string, string>();
const nonceByArtifact = new Map<string, string>();
const MANAGED_MARKER = ".expdesign-managed.json";
const REGISTRY_NAME = ".expdesign-registry.json";
const LEASE_PREFIX = ".expdesign-active-";
const LOCK_NAME = ".expdesign-lifecycle.lock";
const MARKER_OWNER = "experiment-design-mcp";
const LOCK_OWNER_NAME = "owner.json";

type ManagedRegistry = { version: 1; entries: Record<string, string> };
type LifecycleLockOwner = {
  token: string;
  pid: number;
  created_at: number;
};

async function readRegistry(root: string): Promise<ManagedRegistry> {
  try {
    const parsed = JSON.parse(await readFile(join(root, REGISTRY_NAME), "utf8")) as Partial<ManagedRegistry>;
    if (parsed.version !== 1 || !parsed.entries || typeof parsed.entries !== "object" || Array.isArray(parsed.entries)) {
      return { version: 1, entries: {} };
    }
    const entries = Object.fromEntries(Object.entries(parsed.entries).filter(([name, nonce]) =>
      basename(name) === name && typeof nonce === "string" && nonce.length >= 16));
    return { version: 1, entries };
  } catch {
    // Missing or malformed registry data must fail safe: no directory becomes
    // deletion-eligible merely because it contains a marker-named file.
    return { version: 1, entries: {} };
  }
}

async function writeRegistry(root: string, registry: ManagedRegistry): Promise<void> {
  const temp = join(root, `${REGISTRY_NAME}.${process.pid}-${randomUUID()}.tmp`);
  await writeFile(temp, JSON.stringify(registry), { mode: 0o600 });
  await rename(temp, join(root, REGISTRY_NAME));
}

async function registeredMarkerMatches(
  path: string,
  registry: ManagedRegistry,
): Promise<boolean> {
  const expected = registry.entries[basename(path)];
  if (!expected) return false;
  try {
    const marker = JSON.parse(await readFile(join(path, MANAGED_MARKER), "utf8")) as {
      version?: unknown; owner?: unknown; nonce?: unknown; state?: unknown;
    };
    return marker.version === 1 && marker.owner === MARKER_OWNER &&
      marker.nonce === expected && marker.state === "committed";
  } catch {
    return false;
  }
}

function within(root: string, target: string): boolean {
  return target === root || target.startsWith(root + sep);
}

function positiveEnv(name: string, fallback: number): number {
  const parsed = Number(process.env[name] ?? fallback);
  return Number.isFinite(parsed) && parsed >= 1 ? Math.floor(parsed) : fallback;
}

async function withLifecycleLock<T>(operation: () => Promise<T>): Promise<T> {
  const prior = lifecycleQueue;
  let release!: () => void;
  lifecycleQueue = new Promise<void>((resolvePromise) => { release = resolvePromise; });
  await prior;
  try {
    return await operation();
  } finally {
    release();
  }
}

async function withFilesystemLock<T>(root: string, operation: () => Promise<T>): Promise<T> {
  const lock = join(root, LOCK_NAME);
  const ownerToken = randomUUID();
  const waitMs = positiveEnv("EXPDESIGN_ARTIFACT_LOCK_WAIT_MS", 40_000);
  const deadline = Date.now() + waitMs;
  for (;;) {
    try {
      await mkdir(lock, { mode: 0o700 });
      try {
        await writeFile(join(lock, LOCK_OWNER_NAME), JSON.stringify({
          token: ownerToken, pid: process.pid, created_at: Date.now(),
        } satisfies LifecycleLockOwner), { mode: 0o600 });
      } catch (error) {
        await rm(lock, { recursive: true, force: true });
        throw error;
      }
      break;
    } catch (error) {
      const code = (error as NodeJS.ErrnoException).code;
      if (code !== "EEXIST") throw error;
      // Never auto-reclaim an existing directory. Filesystem rename/unlink does
      // not offer compare-and-swap ownership: a check-then-remove reclaimer can
      // steal a successor lock after an owner releases. A crash therefore
      // requires explicit operator cleanup, favoring availability loss over
      // concurrent registry mutation or artifact deletion.
      if (Date.now() >= deadline) {
        throw new Error(
          `timed out waiting for artifact lifecycle lock; if no process owns it, ` +
          `remove ${lock} explicitly after checking ${join(lock, LOCK_OWNER_NAME)}`,
        );
      }
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 25));
    }
  }
  try {
    return await operation();
  } finally {
    try {
      const owner = JSON.parse(await readFile(join(lock, LOCK_OWNER_NAME), "utf8")) as { token?: unknown };
      if (owner.token === ownerToken) await rm(lock, { recursive: true, force: true });
    } catch { /* a stale owner must never remove a successor's lock */ }
  }
}

async function artifactHasLiveLease(path: string): Promise<boolean> {
  let entries;
  try {
    entries = await readdir(path, { withFileTypes: true });
  } catch {
    return false;
  }
  for (const entry of entries) {
    if (!entry.isFile() || !entry.name.startsWith(LEASE_PREFIX)) continue;
    const lease = join(path, entry.name);
    try {
      const payload = JSON.parse(await readFile(lease, "utf8")) as {
        pid?: unknown; token?: unknown; nonce?: unknown; created_at?: unknown;
      };
      const pid = Number(payload.pid);
      const createdAt = Number(payload.created_at);
      const maxAge = positiveEnv("EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS", 86_400) * 1000;
      let markerNonce: unknown;
      try {
        markerNonce = (JSON.parse(await readFile(join(path, MANAGED_MARKER), "utf8")) as { nonce?: unknown }).nonce;
      } catch { markerNonce = undefined; }
      const validToken = typeof payload.token === "string" && payload.token.length >= 16 &&
        entry.name.includes(payload.token);
      const validAge = Number.isFinite(createdAt) && createdAt <= Date.now() && Date.now() - createdAt <= maxAge;
      if (Number.isInteger(pid) && pid > 0 && validToken && validAge &&
          typeof payload.nonce === "string" && payload.nonce === markerNonce) {
        try {
          process.kill(pid, 0);
          return true;
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code === "EPERM") return true;
        }
      }
      await unlink(lease);
    } catch {
      await rm(lease, { force: true });
    }
  }
  return false;
}

async function enforceRetention(root: string, protectedPaths: ReadonlySet<string> = new Set()): Promise<void> {
  const retentionDays = positiveEnv("EXPDESIGN_ARTIFACT_RETENTION_DAYS", 30);
  const maxDirs = positiveEnv("EXPDESIGN_ARTIFACT_MAX_DIRS", 100);
  const cutoff = Date.now() - retentionDays * 86_400_000;
  const registry = await readRegistry(root);
  const managed = [];
  const existingDirectories = new Set<string>();
  for (const entry of await readdir(root, { withFileTypes: true })) {
    if (!entry.isDirectory() || entry.name === LOCK_NAME) continue;
    const path = join(root, entry.name);
    existingDirectories.add(entry.name);
    if (await registeredMarkerMatches(path, registry)) managed.push(entry);
    else {
      // Uncommitted staging directories are never retention-eligible. Reap
      // abandoned ones after the bounded lease window; live cross-process work
      // remains protected by its nonce-bound lease.
      try {
        const marker = JSON.parse(await readFile(join(path, MANAGED_MARKER), "utf8")) as {
          owner?: unknown; state?: unknown; created_at?: unknown;
        };
        const stale = marker.owner === MARKER_OWNER && marker.state === "staging" &&
          Date.now() - Number(marker.created_at) >
            positiveEnv("EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS", 86_400) * 1000;
        if (stale && !await artifactHasLiveLease(path)) {
          await rm(path, { recursive: true, force: true });
          existingDirectories.delete(entry.name);
        }
      } catch { /* user directories and malformed markers are never deleted */ }
    }
  }
  const dated = await Promise.all(managed.map(async (entry) => {
    const path = join(root, entry.name);
    return { path, modified: (await stat(path)).mtimeMs };
  }));
  dated.sort((a, b) => b.modified - a.modified);

  // Capacity applies only to authenticated committed outputs. Active staging
  // runs are protected separately and must never consume these retention slots.
  const keep = new Set<string>(dated.filter((item) =>
    activeArtifacts.has(item.path) || protectedPaths.has(item.path)
  ).map((item) => item.path));
  for (const item of dated) {
    if (await artifactHasLiveLease(item.path)) keep.add(item.path);
  }
  for (const item of dated) {
    if (item.modified >= cutoff && keep.size < maxDirs) keep.add(item.path);
  }
  const deleted = dated.filter((item) =>
    !keep.has(item.path) && (item.modified < cutoff || keep.size >= maxDirs)
  );
  await Promise.all(deleted.map((item) => rm(item.path, { recursive: true, force: true })));
  for (const item of deleted) delete registry.entries[basename(item.path)];
  for (const name of Object.keys(registry.entries)) {
    if (!existingDirectories.has(name)) delete registry.entries[name];
  }
  await writeRegistry(root, registry);
}

export async function createManagedArtifactDir(
  prefix: string,
  rawPath?: string,
): Promise<string> {
  return withLifecycleLock(async () => {
    await mkdir(ARTIFACT_ROOT, { recursive: true, mode: 0o700 });
    const root = realpathSync(ARTIFACT_ROOT);
    return withFilesystemLock(root, async () => {
      let claimed: string;

      if (!rawPath) {
        claimed = await mkdtemp(join(root, prefix));
      } else {
        const configuredRoot = resolve(ARTIFACT_ROOT);
        const target = resolve(rawPath);
        if (target === configuredRoot || !within(configuredRoot, target)) {
          throw new Error(`Output path must be a new directory inside ${ARTIFACT_ROOT}`);
        }
        const canonicalTarget = resolve(root, relative(configuredRoot, target));
        const parent = dirname(canonicalTarget);
        if (parent !== root) {
          throw new Error("Output path must be a direct child of the run root");
        }
        const realParent = realpathSync(parent);
        if (!within(root, realParent)) {
          throw new Error("Output path parent resolves outside the run root");
        }
        const atomicTarget = join(realParent, basename(target));
        if (resolve(atomicTarget) !== canonicalTarget) {
          throw new Error("Output path contains a symlinked parent");
        }
        try {
          await mkdir(atomicTarget, { recursive: false, mode: 0o700 });
        } catch (error) {
          const code = (error as NodeJS.ErrnoException).code;
          if (code === "EEXIST") {
            throw new Error("Output path must not already exist; existing artifacts are never overwritten");
          }
          throw error;
        }
        claimed = realpathSync(atomicTarget);
        if (!within(root, claimed)) throw new Error("Output path resolves outside the run root");
      }

      const nonce = randomUUID();
      const leaseToken = randomUUID();
      const lease = join(claimed, `${LEASE_PREFIX}${process.pid}-${leaseToken}.json`);
      try {
        await writeFile(join(claimed, MANAGED_MARKER), JSON.stringify({
          version: 1, owner: MARKER_OWNER, nonce, state: "staging", created_at: Date.now(),
        }), { mode: 0o600 });
        await writeFile(lease, JSON.stringify({
          pid: process.pid, token: leaseToken, nonce, created_at: Date.now(),
        }), { mode: 0o600 });
      } catch (error) {
        await rm(claimed, { recursive: true, force: true });
        throw error;
      }
      activeArtifacts.add(claimed);
      leaseByArtifact.set(claimed, lease);
      nonceByArtifact.set(claimed, nonce);
      // Retention is committed only when a completed artifact is released.
      // Enforcing it here would let an unverified run evict the last valid
      // artifact and then disappear itself when the run aborts.
      return claimed;
    });
  });
}

export async function releaseManagedArtifactDir(path: string): Promise<void> {
  await withLifecycleLock(async () => {
    const root = realpathSync(ARTIFACT_ROOT);
    await withFilesystemLock(root, async () => {
      const nonce = nonceByArtifact.get(path);
      if (!activeArtifacts.has(path) || !nonce) {
        throw new Error("refusing to release an artifact directory not owned by this process");
      }
      const registry = await readRegistry(root);
      registry.entries[basename(path)] = nonce;
      await writeRegistry(root, registry);
      await writeFile(join(path, MANAGED_MARKER), JSON.stringify({
        version: 1, owner: MARKER_OWNER, nonce, state: "committed", committed_at: Date.now(),
      }), { mode: 0o600 });
      const lease = leaseByArtifact.get(path);
      if (lease) await rm(lease, { force: true });
      leaseByArtifact.delete(path);
      nonceByArtifact.delete(path);
      activeArtifacts.delete(path);
      await enforceRetention(root, new Set([path]));
    });
  });
}

export async function abortManagedArtifactDir(path: string): Promise<void> {
  await withLifecycleLock(async () => {
    const root = realpathSync(ARTIFACT_ROOT);
    await withFilesystemLock(root, async () => {
      const resolved = resolve(path);
      if (!within(root, resolved) || !activeArtifacts.has(resolved)) {
        throw new Error("refusing to abort an artifact directory not owned by this process");
      }
      const nonce = nonceByArtifact.get(resolved);
      let marker: { owner?: unknown; nonce?: unknown; state?: unknown } = {};
      try { marker = JSON.parse(await readFile(join(resolved, MANAGED_MARKER), "utf8")); } catch { /* checked below */ }
      if (!nonce || marker.owner !== MARKER_OWNER || marker.nonce !== nonce || marker.state !== "staging") {
        throw new Error("refusing to abort an artifact directory without matching staging ownership");
      }
      const registry = await readRegistry(root);
      const lease = leaseByArtifact.get(resolved);
      if (lease) await rm(lease, { force: true });
      leaseByArtifact.delete(resolved);
      nonceByArtifact.delete(resolved);
      activeArtifacts.delete(resolved);
      await rm(resolved, { recursive: true, force: true });
      delete registry.entries[basename(resolved)];
      await writeRegistry(root, registry);
      // A failed run only removes its own staging output. Retention (including
      // changes to configured capacity) is committed by a successful release.
    });
  });
}
