import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import {
  boundedToolResult,
  MAX_PUBLIC_TOOL_RESULT_BYTES,
} from "../dist/response-budget.js";
import { RepositoryToolBoundary } from "../dist/tool-boundary.js";


const server = new Server(
  { name: "response-budget-regression", version: "1" },
  { capabilities: { tools: { listChanged: false } } },
);
const boundary = new RepositoryToolBoundary(server);

const resultAtSize = (targetBytes) => {
  const empty = { content: [{ type: "text", text: "" }] };
  const fixedBytes = Buffer.byteLength(JSON.stringify(empty), "utf8");
  if (targetBytes < fixedBytes) throw new Error("invalid response-budget fixture");
  return { content: [{ type: "text", text: "x".repeat(targetBytes - fixedBytes) }] };
};

boundary.register(
  "max_result",
  "Return one tool result exactly at the reviewed serialized byte limit.",
  z.object({}).strict(),
  async () => boundedToolResult(resultAtSize(MAX_PUBLIC_TOOL_RESULT_BYTES)),
);

boundary.register(
  "over_result",
  "Attempt one tool result exactly one byte beyond the reviewed limit.",
  z.object({}).strict(),
  async () => boundedToolResult(resultAtSize(MAX_PUBLIC_TOOL_RESULT_BYTES + 1)),
);

boundary.register(
  "strict_probe",
  "Exercise repository-owned schema and error-taxonomy handling.",
  z.object({ count: z.number().int().min(1).max(2) }).strict(),
  async ({ count }) => boundedToolResult({
    content: [{ type: "text", text: JSON.stringify({ count }) }],
  }),
);

await server.connect(new StdioServerTransport());
