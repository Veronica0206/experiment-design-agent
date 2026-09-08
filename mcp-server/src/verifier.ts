import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  PYTHON_EXECUTABLE,
  sanitizedPythonChildEnvironment,
} from "./integrity.js";
import {
  killRuntimeProcessTree,
  spawnRuntimeProcess,
} from "./runtime-supervisor.js";

const SUITE_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const VERIFIER = resolve(SUITE_ROOT, "agent-harness", "server_verify.py");
const VERIFY_TIMEOUT_MS = 30_000;
export const MAX_VERIFIER_STDOUT_BYTES = 5 * 1024 * 1024;

interface ActiveVerifierProcessGroup {
  terminate: () => void;
  closed: Promise<void>;
}

const activeVerifierProcessGroups = new Map<number, ActiveVerifierProcessGroup>();
let acceptingVerifierProcesses = true;

/** Number of live verifier process groups; exported for lifecycle observability/tests. */
export function activeVerifierProcessGroupCount(): number {
  return activeVerifierProcessGroups.size;
}

/**
 * Permanently stop accepting verifier work, terminate every active verifier
 * process group, and wait for each group leader to close. Because every
 * verifier is a detached process-group leader on POSIX, the same signal also
 * covers descendants created by verifier code.
 */
export async function shutdownActiveVerifierProcesses(): Promise<void> {
  acceptingVerifierProcesses = false;
  for (;;) {
    const active = [...activeVerifierProcessGroups.values()];
    if (active.length === 0) return;
    for (const group of active) group.terminate();
    await Promise.all(active.map((group) => group.closed));
  }
}

function verifierAbortError(): Error {
  const error = new Error("Verification cancelled by the MCP client");
  error.name = "AbortError";
  return error;
}

export interface VerificationEnvelope {
  identity?: {
    analysis_id: string;
    call_id: string;
    tool: string;
    public_args_hash: string;
    provenance_hash: string;
  };
  status: string;
  presentable: boolean;
  checks?: Record<string, boolean>;
  failures?: string[];
  blocked?: string[];
  notes?: string[];
  report?: string;
  report_hash?: string;
  public_result?: unknown;
  public_result_hash?: string;
}

/**
 * Run the verifier under the pinned Python interpreter. The path override is
 * exported only so lifecycle tests can exercise cancellation against a
 * deliberately blocked verifier; production preverification always uses the
 * fixed audited VERIFIER path below.
 */
export function runVerifierRequest(
  request: string,
  signal?: AbortSignal,
  verifierPath = VERIFIER,
): Promise<VerificationEnvelope> {
  if (!acceptingVerifierProcesses) {
    return Promise.reject(new Error("Verifier runtime is shutting down"));
  }
  if (signal?.aborted) return Promise.reject(verifierAbortError());
  return new Promise((resolvePromise, reject) => {
    // Ignore PYTHON* environment injection, user site packages, and automatic
    // sitecustomize loading. The verifier imports only its adjacent audited
    // modules and Python's standard library. A detached POSIX process group
    // lets cancellation terminate any verifier descendants as well.
    const proc = spawnRuntimeProcess(
      PYTHON_EXECUTABLE, ["-E", "-s", "-S", "-B", verifierPath],
      {
        stdio: ["pipe", "pipe", "pipe"],
        detached: process.platform !== "win32",
        env: sanitizedPythonChildEnvironment(),
      },
    );
    const stdoutChunks: Buffer[] = [];
    let stdoutBytes = 0;
    let settled = false;
    let stopping = false;
    let abortListening = false;
    let closedMarked = false;
    let resolveClosed!: () => void;
    const closed = new Promise<void>((resolveClosedPromise) => {
      resolveClosed = resolveClosedPromise;
    });
    const processGroupId = proc.pid;

    const markClosed = () => {
      if (closedMarked) return;
      closedMarked = true;
      if (processGroupId !== undefined) activeVerifierProcessGroups.delete(processGroupId);
      resolveClosed();
    };

    const cleanup = () => {
      clearTimeout(timer);
      if (abortListening) signal?.removeEventListener("abort", onAbort);
      abortListening = false;
    };
    const fail = (error: Error) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const succeed = (value: VerificationEnvelope) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolvePromise(value);
    };
    const stopProcessGroup = (error: Error) => {
      if (!stopping) {
        stopping = true;
        killRuntimeProcessTree(proc, "SIGKILL");
      }
      fail(error);
    };
    const onAbort = () => {
      if (settled) return;
      // Cancellation is an explicit request to stop work, so terminate the
      // whole group immediately rather than leaving an escalation timer alive.
      stopProcessGroup(verifierAbortError());
    };
    const terminateForShutdown = () => {
      stopProcessGroup(new Error("Verifier runtime is shutting down"));
    };
    const timer = setTimeout(() => {
      stopProcessGroup(
        new Error(`verification process timed out after ${VERIFY_TIMEOUT_MS / 1000}s`),
      );
    }, VERIFY_TIMEOUT_MS);

    proc.stdout?.on("data", (chunk: Buffer) => {
      if (settled) return;
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      if (stdoutBytes + bytes.length > MAX_VERIFIER_STDOUT_BYTES) {
        stopProcessGroup(new Error(
          `verification process output exceeded the ${MAX_VERIFIER_STDOUT_BYTES}-byte limit`,
        ));
        return;
      }
      stdoutBytes += bytes.length;
      stdoutChunks.push(bytes);
    });
    // Drain stderr so a verifier cannot block on a full pipe. Its content may
    // contain request values or paths, so it is never reflected to callers.
    proc.stderr?.on("data", () => {});
    proc.stdin?.on("error", () => { /* close/error supplies the final verdict */ });
    proc.on("error", (error) => {
      // A failed spawn has no process group and no group to await. If a pid was
      // assigned, retain the registry entry until the subsequent close event.
      if (processGroupId === undefined) markClosed();
      fail(new Error(`failed to spawn verifier: ${error.message}`));
    });
    proc.on("close", (code) => {
      markClosed();
      if (settled) return;
      const stdout = Buffer.concat(stdoutChunks, stdoutBytes).toString("utf8");
      try {
        const parsed = JSON.parse(stdout) as VerificationEnvelope;
        if (code !== 0 || !parsed || typeof parsed.presentable !== "boolean") {
          fail(new Error("verification process failed without a valid envelope"));
        } else succeed(parsed);
      } catch {
        fail(new Error("verification process returned invalid JSON"));
      }
    });
    if (processGroupId !== undefined) {
      activeVerifierProcessGroups.set(processGroupId, {
        terminate: terminateForShutdown,
        closed,
      });
    }
    if (signal) {
      abortListening = true;
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) {
        onAbort();
        return;
      }
    }
    proc.stdin?.end(request);
  });
}

export async function preverify(
  tool: string,
  args: Record<string, unknown>,
  result: unknown,
  replay?: unknown,
  replayRequired = false,
  extraBlocked: string[] = [],
  publicProvenance: Record<string, unknown> = {},
  regression: unknown = undefined,
  signal?: AbortSignal,
): Promise<VerificationEnvelope> {
  const runtimeId = `analysis-${randomUUID()}`;
  const request = JSON.stringify({
    tool, args, result, replay, replay_required: replayRequired,
    extra_blocked: extraBlocked, runtime_id: runtimeId,
    public_provenance: publicProvenance, regression,
  });
  return runVerifierRequest(request, signal);
}
