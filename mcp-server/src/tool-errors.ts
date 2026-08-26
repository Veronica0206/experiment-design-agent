/** Fixed, value-free errors permitted to cross the MCP/model boundary. */
export const PUBLIC_TOOL_ERROR_MESSAGES = {
  request_cancelled: "The tool request was cancelled.",
  invalid_request: "The tool request is invalid.",
  internal_error: "The tool could not complete safely.",
} as const;

export type PublicToolErrorCode = keyof typeof PUBLIC_TOOL_ERROR_MESSAGES;

/** An intentionally public error whose code and message are both fixed. */
export class PublicToolError extends Error {
  readonly code: PublicToolErrorCode;

  constructor(code: PublicToolErrorCode) {
    super(PUBLIC_TOOL_ERROR_MESSAGES[code]);
    this.name = "PublicToolError";
    this.code = code;
  }
}

/**
 * A caller-originated rejection with a private diagnostic. The diagnostic is
 * deliberately not the public message: only its fixed code crosses MCP.
 */
export class InvalidRequestToolError extends Error {
  readonly code = "invalid_request" as const;
  readonly privateCause: unknown;

  constructor(privateDiagnostic: string, cause?: unknown) {
    super(privateDiagnostic, cause === undefined ? undefined : { cause });
    this.name = "InvalidRequestToolError";
    this.privateCause = cause;
  }
}

/** Private diagnostic wrapper. Neither this object nor its cause is serialized. */
export class InternalToolError extends Error {
  readonly privateCause: unknown;
  readonly privateContext: Readonly<{ tool: string }>;

  constructor(tool: string, cause: unknown) {
    super("An internal MCP tool error was withheld from the public response");
    this.name = "InternalToolError";
    this.privateCause = cause;
    this.privateContext = Object.freeze({ tool });
  }
}

type PrivateDiagnosticWriter = (message: string) => void;

function privateDiagnostic(error: InternalToolError): string {
  const cause = error.privateCause;
  const detail = cause instanceof Error
    ? (cause.stack || cause.message)
    : String(cause);
  return `[experiment-design/private-tool-error] tool=${JSON.stringify(
    error.privateContext.tool,
  )} ${detail}`;
}

function publicErrorFor(error: unknown): PublicToolError {
  if (error instanceof PublicToolError) return error;
  if (error instanceof InvalidRequestToolError) {
    return new PublicToolError("invalid_request");
  }
  if (error instanceof Error && error.name === "AbortError") {
    return new PublicToolError("request_cancelled");
  }
  return new PublicToolError("internal_error");
}

/**
 * Convert every handler failure to the fixed MCP-visible taxonomy while keeping
 * the original exception available only on the server's private diagnostic
 * stream. Callers must never receive the diagnostic string.
 */
export function publicToolErrorResponse(
  tool: string,
  error: unknown,
  writePrivateDiagnostic: PrivateDiagnosticWriter = (message) => console.error(message),
) {
  const internal = error instanceof InternalToolError
    ? error
    : new InternalToolError(tool, error);
  try {
    writePrivateDiagnostic(privateDiagnostic(internal));
  } catch {
    // Diagnostic availability must never weaken the fail-closed public response.
  }
  const publicError = publicErrorFor(error);
  return {
    isError: true as const,
    content: [{
      type: "text" as const,
      text: JSON.stringify({
        error: {
          code: publicError.code,
          message: PUBLIC_TOOL_ERROR_MESSAGES[publicError.code],
        },
      }),
    }],
  };
}
