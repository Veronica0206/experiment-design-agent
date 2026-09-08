import { constants as fsConstants } from "node:fs";
import { writeFile, open, rm, mkdtemp } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { readBoundedFile } from "./bounded-read.js";
import { RSCRIPT_EXECUTABLE, sanitizedRChildEnvironment } from "./integrity.js";
import {
  killRuntimeProcessTree,
  spawnRuntimeProcess,
} from "./runtime-supervisor.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const DISPATCHER = join(__dirname, "..", "r-wrapper", "dispatcher.R");
const TIMEOUT_MS = 120_000;
const MAX_STDERR_CHARS = 64 * 1024;
const MAX_RESULT_BYTES = 5 * 1024 * 1024;

interface ActiveRProcessGroup {
  cancel: () => void;
  closed: Promise<void>;
}

const activeRProcessGroups = new Map<number, ActiveRProcessGroup>();
let acceptingRProcesses = true;

/** Number of live R process groups; exported for lifecycle observability/tests. */
export function activeRProcessGroupCount(): number {
  return activeRProcessGroups.size;
}

/**
 * Cancel every R group that is active when server shutdown begins, including
 * descendants, and wait until each group has closed (or its kill fallback has
 * completed). Repeat in case a request crossed the first shutdown snapshot.
 */
export async function shutdownActiveRProcesses(): Promise<void> {
  acceptingRProcesses = false;
  for (;;) {
    const active = [...activeRProcessGroups.values()];
    if (active.length === 0) return;
    for (const group of active) group.cancel();
    await Promise.all(active.map((group) => group.closed));
  }
}

export interface RscriptOutcome {
  code: number | null;
  signal: NodeJS.Signals | null;
  stderr: string;
  timedOut: boolean;
  aborted: boolean;
}

function abortError(): Error {
  const error = new Error("R process cancelled by the MCP client");
  error.name = "AbortError";
  return error;
}

/** Read the dispatcher result through one non-following, bounded descriptor. */
export async function readRResultFile(path: string): Promise<string> {
  const noFollow = typeof fsConstants.O_NOFOLLOW === "number" ? fsConstants.O_NOFOLLOW : 0;
  const nonBlock = typeof fsConstants.O_NONBLOCK === "number" ? fsConstants.O_NONBLOCK : 0;
  const handle = await open(path, fsConstants.O_RDONLY | noFollow | nonBlock);
  try {
    const before = await handle.stat({ bigint: true });
    if (!before.isFile()) throw new Error("R result is not a regular file");
    if (before.size > BigInt(MAX_RESULT_BYTES)) {
      throw new Error(
        `R result exceeded ${MAX_RESULT_BYTES} bytes and was withheld from model context; ` +
        "the unverified payload was discarded. Request a smaller design.",
      );
    }
    const bytes = await readBoundedFile(handle, MAX_RESULT_BYTES, "R result");
    const after = await handle.stat({ bigint: true });
    if (!after.isFile() || before.dev !== after.dev || before.ino !== after.ino ||
        before.size !== after.size || before.mtimeNs !== after.mtimeNs ||
        before.ctimeNs !== after.ctimeNs) {
      throw new Error("R result changed while it was being read");
    }
    return bytes.toString("utf-8");
  } finally {
    await handle.close();
  }
}

export async function callR(
  tool: string,
  params: Record<string, unknown>,
  signal?: AbortSignal,
): Promise<unknown> {
  if (signal?.aborted) throw abortError();
  const tmp = await mkdtemp(join(tmpdir(), "mcp-r-"));
  const inputFile = join(tmp, "input.json");
  const outputFile = join(tmp, "output.json");

  await writeFile(inputFile, JSON.stringify(params));

  try {
    const outcome = await runRscript(
      ["--vanilla", DISPATCHER, tool, inputFile, outputFile], signal,
    );

    // The dispatcher's contract: it ALWAYS writes output.json before exiting —
    // a result on success, or {"error": <message>} on failure — and the real
    // diagnostic lives in that file, not stderr. So read the file regardless of
    // exit code, and only fall back to exit-code/stderr if the file is unusable.
    let parsed: unknown;
    let raw: string | undefined;
    try {
      raw = await readRResultFile(outputFile);
    } catch (error) {
      const code = (error as NodeJS.ErrnoException | undefined)?.code;
      if (code === "ENOENT") raw = undefined;
      else if (error instanceof Error && error.message.startsWith("R result")) throw error;
      else throw new Error("R result file could not be read safely");
    }
    try {
      parsed = raw === undefined ? undefined : JSON.parse(raw);
    } catch {
      parsed = undefined;
    }

    if (outcome.aborted) throw abortError();

    if (parsed && typeof parsed === "object" && "error" in parsed) {
      // R diagnostics can contain CSV-derived labels, values, or paths. Keep
      // them outside the MCP/model boundary. Request preflights use the
      // explicit invalid-request type; an undifferentiated R failure remains
      // an internal error because this bridge cannot safely infer its cause.
      throw new Error("R analysis rejected the supplied inputs");
    }

    if (outcome.timedOut) {
      throw new Error(`R process timed out after ${TIMEOUT_MS / 1000}s running '${tool}'`);
    }

    if (outcome.code !== 0) {
      throw new Error("R analysis failed before producing a safe diagnostic");
    }

    if (parsed === undefined) {
      throw new Error(`Rscript '${tool}' produced no parseable output`);
    }

    return parsed;
  } finally {
    await rm(tmp, { recursive: true, force: true }).catch(() => {});
  }
}

export function runRscript(
  args: string[],
  signal?: AbortSignal,
): Promise<RscriptOutcome> {
  if (!acceptingRProcesses) {
    return Promise.reject(new Error("R runtime is shutting down"));
  }
  return new Promise((resolve, reject) => {
    // stdout is "ignore": the dispatcher returns data via the output file, and
    // sourced R scripts cat() loading banners to stdout. Piping-but-not-draining
    // it risks a 64KB-buffer deadlock, so we drop it at the OS level.
    // On darwin/linux the detached supervisor keeps group ownership until R
    // and its descendants finish, including when R exits before a grandchild.
    // Closing this handle therefore ends the entire managed runtime group.
    const proc = spawnRuntimeProcess(RSCRIPT_EXECUTABLE, args, {
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
      env: sanitizedRChildEnvironment(),
    });
    let stderr = "";
    let timedOut = false;
    let aborted = false;
    let settled = false;
    let stopping = false;
    const timers: NodeJS.Timeout[] = [];
    const clearAll = () => timers.forEach(clearTimeout);
    let abortListening = false;
    let resolveClosed!: () => void;
    const closed = new Promise<void>((resolveClosedPromise) => {
      resolveClosed = resolveClosedPromise;
    });
    const processGroupId = proc.pid;

    const unregister = () => {
      if (processGroupId !== undefined) activeRProcessGroups.delete(processGroupId);
      resolveClosed();
    };

    // Signal the entire process group (negative pid); fall back to the direct
    // child if the group is already gone or the pid never materialized.
    const settle = (outcome: RscriptOutcome) => {
      if (settled) return;
      settled = true;
      clearAll();
      if (abortListening) signal?.removeEventListener("abort", onAbort);
      unregister();
      resolve(outcome);
    };

    const stopProcessGroup = () => {
      if (stopping) return;
      stopping = true;
      // Cancellation and process shutdown are trust-boundary events. Kill the
      // complete detached group immediately so a fast-exiting leader cannot
      // clear a delayed escalation while leaving a grandchild behind.
      killRuntimeProcessTree(proc, "SIGKILL");
      // Resolve even if an unrelated inherited descriptor delays close.
      timers.push(setTimeout(
        () => settle({ code: null, signal: "SIGKILL", stderr, timedOut, aborted }),
        1_000,
      ));
    };

    const onAbort = () => {
      if (settled || aborted) return;
      aborted = true;
      stopProcessGroup();
    };

    proc.stderr?.on("data", (chunk: Buffer) => {
      stderr = (stderr + chunk.toString()).slice(-MAX_STDERR_CHARS);
    });

    timers.push(setTimeout(() => {
      timedOut = true;
      stopProcessGroup();
    }, TIMEOUT_MS));

    proc.on("close", (code, processSignal) => settle({
      code, signal: processSignal, stderr, timedOut, aborted,
    }));

    proc.on("error", (err) => {
      if (settled) return;
      settled = true;
      clearAll();
      if (abortListening) signal?.removeEventListener("abort", onAbort);
      unregister();
      reject(new Error(`Failed to spawn Rscript: ${err.message}`));
    });

    if (processGroupId !== undefined) {
      activeRProcessGroups.set(processGroupId, { cancel: onAbort, closed });
    }

    if (signal) {
      abortListening = true;
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    }
  });
}
