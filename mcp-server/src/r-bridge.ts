import { spawn } from "node:child_process";
import { writeFile, readFile, rm, mkdtemp, stat } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { RSCRIPT_EXECUTABLE } from "./integrity.js";

const __dirname = fileURLToPath(new URL(".", import.meta.url));
const DISPATCHER = join(__dirname, "..", "r-wrapper", "dispatcher.R");
const TIMEOUT_MS = 120_000;
const SIGKILL_GRACE_MS = 5_000;
const MAX_STDERR_CHARS = 64 * 1024;
const MAX_RESULT_BYTES = 5 * 1024 * 1024;

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
      const outputStat = await stat(outputFile);
      if (outputStat.size > MAX_RESULT_BYTES) {
        throw new Error(
          `R result exceeded ${MAX_RESULT_BYTES} bytes and was withheld from model context; ` +
          "the unverified payload was discarded. Request a smaller design.",
        );
      }
      raw = await readFile(outputFile, "utf-8");
    } catch (error) {
      if (error instanceof Error && error.message.includes("exceeded")) throw error;
      raw = undefined;
    }
    try {
      parsed = raw === undefined ? undefined : JSON.parse(raw);
    } catch {
      parsed = undefined;
    }

    if (outcome.aborted) throw abortError();

    if (parsed && typeof parsed === "object" && "error" in parsed) {
      // R diagnostics can contain CSV-derived labels, values, or paths. Keep
      // them outside the MCP/model boundary; schema-level TypeScript errors
      // remain specific and safe because they never inspect private files.
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
  return new Promise((resolve, reject) => {
    // stdout is "ignore": the dispatcher returns data via the output file, and
    // sourced R scripts cat() loading banners to stdout. Piping-but-not-draining
    // it risks a 64KB-buffer deadlock, so we drop it at the OS level.
    // detached: true makes the child a process-group leader (darwin/linux) so
    // the timeout can kill the WHOLE group: run_tests spawns grandchildren via
    // system2 that would otherwise survive a kill of the direct child alone.
    const proc = spawn(RSCRIPT_EXECUTABLE, args, {
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
    });
    let stderr = "";
    let timedOut = false;
    let aborted = false;
    let settled = false;
    const timers: NodeJS.Timeout[] = [];
    const clearAll = () => timers.forEach(clearTimeout);
    let abortListening = false;

    // Signal the entire process group (negative pid); fall back to the direct
    // child if the group is already gone or the pid never materialized.
    const killTree = (sig: NodeJS.Signals) => {
      try {
        if (proc.pid) process.kill(-proc.pid, sig);
        else proc.kill(sig);
      } catch {
        try { proc.kill(sig); } catch { /* already exited */ }
      }
    };

    const settle = (outcome: RscriptOutcome) => {
      if (settled) return;
      settled = true;
      clearAll();
      if (abortListening) signal?.removeEventListener("abort", onAbort);
      resolve(outcome);
    };

    const stopProcessGroup = () => {
      killTree("SIGTERM");
      // Escalate to SIGKILL if the process group ignores SIGTERM.
      timers.push(setTimeout(() => killTree("SIGKILL"), SIGKILL_GRACE_MS));
      // Resolve even if an orphaned grandchild keeps stderr open.
      timers.push(setTimeout(
        () => settle({ code: null, signal: "SIGKILL", stderr, timedOut, aborted }),
        SIGKILL_GRACE_MS + 1000,
      ));
    };

    const onAbort = () => {
      if (settled || aborted) return;
      aborted = true;
      stopProcessGroup();
    };

    proc.stderr.on("data", (chunk: Buffer) => {
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
      reject(new Error(`Failed to spawn Rscript: ${err.message}`));
    });

    if (signal) {
      abortListening = true;
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();
    }
  });
}
