import { createHash, randomUUID } from "node:crypto";
import {
  closeSync,
  constants as fsConstants,
  fchmodSync,
  fstatSync,
  fsyncSync,
  openSync,
  readdirSync,
  readSync,
  realpathSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join, resolve } from "node:path";


export const MAX_PUBLISHED_ARTIFACT_BYTES = 50 * 1024 * 1024;


function sha256(bytes: Buffer): string {
  return createHash("sha256").update(bytes).digest("hex");
}


function expectedDigest(value: unknown, name: string): string {
  const hashes = value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
  const expected = hashes[name];
  if (typeof expected !== "string" || !/^[a-f0-9]{64}$/i.test(expected)) {
    throw new Error(`artifact '${name}' is not bound by provenance`);
  }
  return expected.toLowerCase();
}


export function readBoundedArtifactFile(path: string): Buffer {
  const fd = openSync(
    path,
    fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW | fsConstants.O_NONBLOCK,
  );
  try {
    const opened = fstatSync(fd);
    if (!opened.isFile()) throw new Error("artifact is not a regular file");
    if (
      !Number.isSafeInteger(opened.size) || opened.size < 0 ||
      opened.size > MAX_PUBLISHED_ARTIFACT_BYTES
    ) {
      throw new Error(
        `artifact exceeds the ${MAX_PUBLISHED_ARTIFACT_BYTES}-byte limit`,
      );
    }

    // Size the buffer from descriptor metadata and never request more bytes
    // than that already-validated size. Re-check the descriptor afterwards so
    // truncation or growth during the read fails closed.
    const bytes = Buffer.allocUnsafe(opened.size);
    let offset = 0;
    while (offset < bytes.length) {
      const bytesRead = readSync(fd, bytes, offset, bytes.length - offset, offset);
      if (bytesRead === 0) throw new Error("artifact changed while it was being read");
      offset += bytesRead;
    }
    const completed = fstatSync(fd);
    if (!completed.isFile()) throw new Error("artifact is not a regular file");
    if (completed.size !== opened.size) {
      throw new Error("artifact changed while it was being read");
    }
    return bytes;
  } finally {
    closeSync(fd);
  }
}


export function hashArtifactForProvenance(path: string): string {
  return sha256(readBoundedArtifactFile(path));
}


export function hashArtifactsForProvenance(outputDirectory: string): Record<string, string> {
  const outputRoot = realpathSync(outputDirectory);
  return Object.fromEntries(
    readdirSync(outputRoot, { withFileTypes: true })
      .sort((left, right) => left.name.localeCompare(right.name))
      .flatMap((entry) => {
        if (entry.name.startsWith(".expdesign-") || entry.isDirectory()) return [];
        const path = join(outputRoot, entry.name);
        return [[entry.name, hashArtifactForProvenance(path)]];
      }),
  );
}


/**
 * Atomically replace each candidate with a server-owned snapshot of the exact
 * provenance-bound bytes, returning only paths safe to advertise.
 *
 * The new inode is written privately, flushed, made read-only, and renamed
 * over the R-produced path. A mutation before the descriptor read fails its
 * digest check; a mutation during publication is overwritten by the snapshot.
 * Consumers must still re-hash the opened bytes immediately before delivery,
 * because a same-user process can chmod and mutate any file after publication.
 */
export function publishVerifiedArtifacts(
  candidates: unknown[],
  outputDirectory: unknown,
  artifactHashes: unknown,
): string[] {
  const unique = [...new Set(candidates.filter(
    (candidate): candidate is string => typeof candidate === "string",
  ).map((candidate) => resolve(candidate)))];
  if (!unique.length) return [];
  if (typeof outputDirectory !== "string") {
    throw new Error("verified artifacts require a managed output directory");
  }
  const outputRoot = realpathSync(outputDirectory);
  const published: string[] = [];

  for (const candidateLabel of unique) {
    const name = basename(candidateLabel);
    if (!name || name.startsWith(".expdesign-")) continue;
    const candidateParent = realpathSync(dirname(candidateLabel));
    if (candidateParent !== outputRoot) {
      throw new Error(`artifact '${name}' is outside its managed output directory`);
    }
    const candidate = join(outputRoot, name);
    const expected = expectedDigest(artifactHashes, name);
    const verifiedBytes = readBoundedArtifactFile(candidate);
    if (sha256(verifiedBytes) !== expected) {
      throw new Error(`artifact '${name}' changed after provenance was recorded`);
    }

    const temporary = join(
      outputRoot,
      `.expdesign-publish-${expected}-${randomUUID()}.tmp`,
    );
    let temporaryExists = false;
    try {
      const fd = openSync(
        temporary,
        fsConstants.O_WRONLY | fsConstants.O_CREAT | fsConstants.O_EXCL |
          fsConstants.O_NOFOLLOW,
        0o600,
      );
      temporaryExists = true;
      try {
        writeFileSync(fd, verifiedBytes);
        fsyncSync(fd);
        fchmodSync(fd, 0o400);
      } finally {
        closeSync(fd);
      }
      renameSync(temporary, candidate);
      temporaryExists = false;

      // Verify the advertised pathname after the atomic replacement. The
      // Python delivery boundary repeats this check on the exact bytes it sends.
      if (sha256(readBoundedArtifactFile(candidate)) !== expected) {
        throw new Error(`artifact '${name}' changed during publication`);
      }
      published.push(candidate);
    } finally {
      if (temporaryExists) {
        try { unlinkSync(temporary); } catch { /* best-effort staging cleanup */ }
      }
    }
  }
  return published;
}
