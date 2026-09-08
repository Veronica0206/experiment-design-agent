import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { Protocol } from "@modelcontextprotocol/sdk/shared/protocol.js";
import {
  ListToolsRequestSchema,
  type CallToolResult,
  type Tool,
} from "@modelcontextprotocol/sdk/types.js";
import { z, type ZodTypeAny } from "zod";
import { zodToJsonSchema } from "zod-to-json-schema";
import {
  InvalidRequestToolError,
  publicToolErrorResponse,
} from "./tool-errors.js";

type RepositoryToolHandler = (
  params: Record<string, unknown>,
  signal: AbortSignal,
) => Promise<CallToolResult>;

type RepositoryTool = {
  readonly definition: Tool;
  readonly schema: ZodTypeAny;
  readonly handler: RepositoryToolHandler;
};

// The SDK Server override performs its own strict CallToolRequestSchema parse
// before invoking any registered tools/call callback.  Keep only method
// routing outside the repository boundary; every caller-controlled params
// field is parsed below so malformed containers receive the same fixed public
// taxonomy as malformed fields inside an otherwise valid argument object.
const RepositoryCallRequestSchema = z.object({
  method: z.literal("tools/call"),
  params: z.unknown().optional(),
}).passthrough();

const RepositoryCallParamsSchema = z.object({
  name: z.string().min(1),
  arguments: z.record(z.string(), z.unknown()).optional(),
  _meta: z.record(z.string(), z.unknown()).optional(),
}).strict();

/**
 * Own the complete public tools/list + tools/call boundary.
 *
 * The high-level SDK validates registered tool arguments before invoking the
 * repository callback and serializes its own free-form Zod diagnostics. This
 * boundary deliberately uses the SDK's public low-level Server API so strict
 * parsing, finite-number checks in the registered callback, execution, and
 * error serialization all live inside one reviewed catch boundary.
 */
export class RepositoryToolBoundary {
  private readonly tools = new Map<string, RepositoryTool>();

  constructor(server: Server) {
    server.setRequestHandler(ListToolsRequestSchema, async () => ({
      tools: [...this.tools.values()].map((tool) => tool.definition),
    }));

    // Call the public Protocol base implementation directly. Server's
    // tools/call override would otherwise re-introduce a pre-handler SDK parse.
    // This does not patch SDK state or private fields: it registers one normal
    // request handler through the exported base API on the Server instance.
    Protocol.prototype.setRequestHandler.call(
      server,
      RepositoryCallRequestSchema,
      async (
        request: z.infer<typeof RepositoryCallRequestSchema>,
        extra: { signal: AbortSignal },
      ) => {
        try {
          const parsedRequest = await RepositoryCallParamsSchema.safeParseAsync(
            request.params,
          );
          if (!parsedRequest.success) {
            throw new InvalidRequestToolError(
              "The tools/call envelope failed strict schema validation",
              parsedRequest.error,
            );
          }
          const requestedName = parsedRequest.data.name;
          const tool = this.tools.get(requestedName);
          if (!tool) {
            return publicToolErrorResponse(
              "unknown_tool",
              new InvalidRequestToolError("The requested tool is not registered"),
            );
          }

          const parsed = await tool.schema.safeParseAsync(
            parsedRequest.data.arguments ?? {},
          );
          if (!parsed.success) {
            throw new InvalidRequestToolError(
              "The tool arguments failed strict schema validation",
              parsed.error,
            );
          }
          return await tool.handler(
            parsed.data as Record<string, unknown>,
            extra.signal,
          );
        } catch (error) {
          return publicToolErrorResponse("tools/call", error);
        }
      },
    );
  }

  register(
    name: string,
    description: string,
    schema: ZodTypeAny,
    handler: RepositoryToolHandler,
  ): void {
    if (this.tools.has(name)) {
      throw new Error("A repository tool name was registered more than once");
    }
    const inputSchema = zodToJsonSchema(schema, {
      $refStrategy: "none",
      target: "jsonSchema7",
    }) as Tool["inputSchema"];
    this.tools.set(name, {
      definition: Object.freeze({ name, description, inputSchema }),
      schema,
      handler,
    });
  }
}
