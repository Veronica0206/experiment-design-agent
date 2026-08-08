import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { PYTHON_EXECUTABLE } from "./integrity.js";

const SUITE_ROOT = resolve(fileURLToPath(new URL("../..", import.meta.url)));
const VERIFIER = resolve(SUITE_ROOT, "agent-harness", "server_verify.py");
const VERIFY_TIMEOUT_MS = 30_000;

export interface VerificationEnvelope {
  identity?: { analysis_id: string; call_id: string; tool: string; args_hash: string; provenance_hash: string };
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

export async function preverify(
  tool: string,
  args: Record<string, unknown>,
  result: unknown,
  replay?: unknown,
  replayRequired = false,
  extraBlocked: string[] = [],
  publicProvenance: Record<string, unknown> = {},
  regression: unknown = undefined,
): Promise<VerificationEnvelope> {
  const runtimeId = `analysis-${randomUUID()}`;
  const request = JSON.stringify({
    tool, args, result, replay, replay_required: replayRequired,
    extra_blocked: extraBlocked, runtime_id: runtimeId,
    public_provenance: publicProvenance, regression,
  });
  return new Promise((resolvePromise, reject) => {
    // Ignore PYTHON* environment injection, user site packages, and automatic
    // sitecustomize loading. The verifier imports only its adjacent audited
    // modules and Python's standard library.
    const proc = spawn(
      PYTHON_EXECUTABLE, ["-E", "-s", "-S", VERIFIER],
      { stdio: ["pipe", "pipe", "pipe"] },
    );
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => proc.kill("SIGKILL"), VERIFY_TIMEOUT_MS);
    proc.stdout.on("data", (chunk: Buffer) => { stdout += chunk.toString(); });
    proc.stderr.on("data", (chunk: Buffer) => { stderr = (stderr + chunk.toString()).slice(-4000); });
    proc.on("error", reject);
    proc.on("close", (code) => {
      clearTimeout(timer);
      try {
        const parsed = JSON.parse(stdout) as VerificationEnvelope;
        if (code !== 0 || !parsed || typeof parsed.presentable !== "boolean") {
          reject(new Error(`verification process failed: ${stderr || stdout}`));
        } else resolvePromise(parsed);
      } catch (error) {
        reject(new Error(`verification process returned invalid JSON: ${String(error)}`));
      }
    });
    proc.stdin.end(request);
  });
}
