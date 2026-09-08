/**
 * Maximum serialized MCP tool-result object permitted to leave a handler.
 *
 * The Python client accepts a complete JSON-RPC frame up to 10 MiB. Keeping
 * the already-escaped `result` object at or below 4 MiB leaves deterministic
 * headroom for the JSON-RPC envelope, request id, and transport newline. The
 * verifier has its own 5 MiB stdout ceiling; a verifier result that expands
 * beyond this public transport budget is rejected here with the fixed public
 * internal-error taxonomy instead of tearing down the client generation.
 */
export const MAX_PUBLIC_TOOL_RESULT_BYTES = 4 * 1024 * 1024;

/** Private-only failure used by the repository-controlled tool boundary. */
export class ToolResultBudgetError extends Error {
  constructor() {
    super("The serialized MCP tool result exceeded the reviewed public transport budget");
    this.name = "ToolResultBudgetError";
  }
}

/**
 * Serialize once, enforce the exact UTF-8 byte budget, then return the parsed
 * snapshot that the SDK will serialize. Snapshotting prevents a mutable result
 * or stateful `toJSON` implementation from growing after the size check.
 */
export function boundedToolResult<T>(
  value: T,
  maxBytes = MAX_PUBLIC_TOOL_RESULT_BYTES,
): T {
  if (!Number.isSafeInteger(maxBytes) || maxBytes < 1) {
    throw new ToolResultBudgetError();
  }
  let serialized: string | undefined;
  try {
    serialized = JSON.stringify(value);
  } catch {
    throw new ToolResultBudgetError();
  }
  if (serialized === undefined || Buffer.byteLength(serialized, "utf8") > maxBytes) {
    throw new ToolResultBudgetError();
  }
  try {
    return JSON.parse(serialized) as T;
  } catch {
    throw new ToolResultBudgetError();
  }
}
