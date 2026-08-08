import type { FileHandle } from "node:fs/promises";

/** Read at most maxBytes + 1 from an already-authorized descriptor. */
export async function readBoundedFile(
  handle: FileHandle,
  maxBytes: number,
  label: string,
): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let total = 0;
  let position = 0;
  for (;;) {
    const remaining = maxBytes + 1 - total;
    if (remaining <= 0) throw new Error(`${label} exceeds the ${maxBytes}-byte limit`);
    const chunk = Buffer.allocUnsafe(Math.min(64 * 1024, remaining));
    const { bytesRead } = await handle.read(chunk, 0, chunk.length, position);
    if (bytesRead === 0) break;
    chunks.push(Buffer.from(chunk.subarray(0, bytesRead)));
    total += bytesRead;
    position += bytesRead;
  }
  if (total > maxBytes) throw new Error(`${label} exceeds the ${maxBytes}-byte limit`);
  return Buffer.concat(chunks, total);
}
