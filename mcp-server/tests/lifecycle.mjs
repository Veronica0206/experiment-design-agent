import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash, createHmac } from "node:crypto";
import fs from "node:fs";
import {
  appendFileSync,
  chmodSync,
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  renameSync,
  statSync,
  symlinkSync,
  truncateSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, dirname, join } from "node:path";

import {
  hashArtifactsForProvenance,
  MAX_PUBLISHED_ARTIFACT_BYTES,
  publishVerifiedArtifacts,
} from "../dist/artifact-publication.js";
import {
  currentPythonRuntimeSnapshot,
  currentRRuntimeSnapshot,
  ENGINE_RUNTIME_FINGERPRINT_LIMITS,
  ENGINE_RUNTIME_RELATIVE_FILE_CATEGORIES,
  FingerprintPromiseCache,
  fingerprintMutableEngineRuntime,
  hashBoundedRuntimeTree,
  hashFramedFields,
  mutableEngineFiles,
  mutableEngineRoots,
  PYTHON_EXECUTABLE,
  REGRESSION_SKILLS,
  RSCRIPT_EXECUTABLE,
  sanitizedPythonChildEnvironment,
  sanitizedRChildEnvironment,
  waitForSharedPromise,
} from "../dist/integrity.js";
import {
  callR,
  readRResultFile,
  runRscript,
} from "../dist/r-bridge.js";
import {
  assertFiniteNumericInputs,
  MISSING_PRIVATE_ENGINE_INSTALLATION_MESSAGE,
  REQUIRED_PRIVATE_ENGINE_RUNTIME_FILES,
  requiredEngineRuntimeFiles,
  assertRequiredPrivateEngineInstallation,
  fingerprintStartupRuntime,
  installedProductionDependencyRoots,
  STARTUP_RUNTIME_FINGERPRINT,
  readAllowedFile,
} from "../dist/index.js";
import { ACTIVE_RUNTIME_PROFILE, loadRuntimeProfile } from "../dist/runtime-profile.js";
import { publicRegressionStatus } from "../dist/public-projection.js";
import {
  MAX_VERIFIER_STDOUT_BYTES,
  runVerifierRequest,
} from "../dist/verifier.js";
import {
  captureBoundedSupervisorProgram,
  killRuntimeProcessTree,
  MAX_SUPERVISOR_PROGRAM_BYTES,
  PINNED_RUNTIME_SUPERVISOR_COMMITMENT,
  parseLinuxProcessGroupStat,
  runtimeSupervisorProgramCommitment,
  spawnRuntimeProcess,
} from "../dist/runtime-supervisor.js";
import {
  InvalidRequestToolError,
  PUBLIC_TOOL_ERROR_MESSAGES,
  publicToolErrorResponse,
} from "../dist/tool-errors.js";
import { normalizedAllocationWeights } from "../dist/allocation-ratio.js";
import { testManagedLifecycles } from "./managed-lifecycle.mjs";
import {
  boundedToolResult,
  MAX_PUBLIC_TOOL_RESULT_BYTES,
  ToolResultBudgetError,
} from "../dist/response-budget.js";


const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const pidAlive = (pid) => {
  try { process.kill(pid, 0); return true; } catch { return false; }
};
const waitForFiles = async (...paths) => {
  for (let attempt = 0; attempt < 200 && !paths.every(existsSync); attempt += 1) {
    await pause(25);
  }
  assert.equal(paths.every(existsSync), true, `timed out waiting for ${paths.join(", ")}`);
};
const waitForPidsToExit = async (...pids) => {
  for (let attempt = 0; attempt < 200 && pids.some(pidAlive); attempt += 1) {
    await pause(25);
  }
  for (const pid of pids) assert.equal(pidAlive(pid), false, `process ${pid} survived cancellation`);
};
const assertRejectsBeforeRead = (action, expected) => {
  const originalReadSync = fs.readSync;
  let readCalls = 0;
  fs.readSync = (...args) => {
    readCalls += 1;
    return originalReadSync(...args);
  };
  syncBuiltinESMExports();
  try {
    assert.throws(action, expected);
  } finally {
    fs.readSync = originalReadSync;
    syncBuiltinESMExports();
  }
  assert.equal(readCalls, 0, "oversized artifact bytes were read before rejection");
};
const boundedRuntimeLimits = (overrides = {}) => ({
  maxFiles: 32,
  maxDirectories: 32,
  maxDirectoryEntries: 64,
  maxDepth: 8,
  maxFileBytes: 8 * 1024 * 1024,
  maxTotalBytes: 16 * 1024 * 1024,
  readChunkBytes: 4 * 1024,
  ...overrides,
});
if (Object.prototype.hasOwnProperty.call(process.env, "EXPDESIGN_PUBLIC_ONLY")) {
  console.error("EXPDESIGN_PUBLIC_ONLY is forbidden; use --public-only");
  process.exit(2);
}
const lifecycleArguments = process.argv.slice(2);
if (
  lifecycleArguments.length > 1
  || (lifecycleArguments.length === 1 && lifecycleArguments[0] !== "--public-only")
) {
  console.error("Usage: node tests/lifecycle.mjs [--public-only]");
  process.exit(2);
}
const publicOnly = lifecycleArguments[0] === "--public-only";
const workspace = realpathSync(
  await mkdtemp(join(tmpdir(), "expdesign-lifecycle-test-")),
);

try {
  // /proc also contains kernel threads outside every user process group.
  // Parse their real field layout on every host so macOS CI catches this Linux
  // boundary without mocking process.platform or weakening leader validation.
  const groupRows = [
    "2 (kthreadd) S 0 0 0 0 -1 0 0 0 0",
    "412 (runtime helper (worker)) S 1 412 412 0 -1 0 0 0 0",
    "413 (sleep) S 412 412 412 0 -1 0 0 0 0",
    "510 (unrelated) S 1 510 510 0 -1 0 0 0 0",
  ];
  assert.deepEqual(groupRows.map(parseLinuxProcessGroupStat), [0, 412, 412, 510]);
  assert.equal(groupRows.filter((row) => parseLinuxProcessGroupStat(row) === 412).length, 2);
  for (const malformed of ["2 kthreadd S 0 0", "2 (kthreadd) S 0",
    "2 (kthreadd) S 0 -1", "2 (kthreadd) S 0 invalid",
    "2 (kthreadd) S 0 9007199254740992"]) {
    assert.throws(() => parseLinuxProcessGroupStat(malformed), /malformed/);
  }
  console.log("TEST linux_process_group_scan_accepts_kernel_threads_and_rejects_malformed_rows : PASS");
  await testManagedLifecycles();
  const outsideInput = join(workspace, "outside-input.csv");
  writeFileSync(outsideInput, "value\n1\n", { mode: 0o600 });
  let privatePathError;
  try {
    await readAllowedFile(outsideInput);
  } catch (error) {
    privatePathError = error;
  }
  assert.ok(privatePathError instanceof InvalidRequestToolError);
  const privateArtifactRoot = join(
    process.cwd(), "agent-harness", "runs", "artifacts",
  );
  const privateRuntimePath = join(process.cwd(), "private-runtime", "Rscript");
  const combinedPrivateError = new InvalidRequestToolError(
    `${privatePathError.message}; artifact=${privateArtifactRoot}; runtime=${privateRuntimePath}`,
  );
  const privateDiagnostics = [];
  const publicFailure = publicToolErrorResponse(
    "indirect_compare",
    combinedPrivateError,
    (message) => privateDiagnostics.push(message),
  );
  const publicFailureText = publicFailure.content[0].text;
  assert.equal(publicFailure.isError, true);
  assert.deepEqual(JSON.parse(publicFailureText), {
    error: {
      code: "invalid_request",
      message: PUBLIC_TOOL_ERROR_MESSAGES.invalid_request,
    },
  });
  for (const privateValue of [outsideInput, privateArtifactRoot, privateRuntimePath]) {
    assert.equal(publicFailureText.includes(privateValue), false);
    assert.equal(privateDiagnostics[0].includes(privateValue), true);
  }
  const cancelled = new Error("cancelled at /private/runtime/path");
  cancelled.name = "AbortError";
  assert.deepEqual(
    JSON.parse(publicToolErrorResponse("sample_size", cancelled, () => {}).content[0].text),
    {
      error: {
        code: "request_cancelled",
        message: PUBLIC_TOOL_ERROR_MESSAGES.request_cancelled,
      },
    },
  );
  assert.deepEqual(
    JSON.parse(publicToolErrorResponse(
      "run_tests",
      new Error(`runtime integrity failed at ${privateRuntimePath}`),
      () => {},
    ).content[0].text),
    {
      error: {
        code: "internal_error",
        message: PUBLIC_TOOL_ERROR_MESSAGES.internal_error,
      },
    },
  );
  console.log("TEST mcp_tool_errors_are_path_free_with_private_diagnostics : PASS");

  const budgetProbe = { content: [{ type: "text", text: "bounded" }] };
  const exactBudget = Buffer.byteLength(JSON.stringify(budgetProbe), "utf8");
  assert.equal(MAX_PUBLIC_TOOL_RESULT_BYTES, 4 * 1024 * 1024);
  assert.deepEqual(boundedToolResult(budgetProbe, exactBudget + 1), budgetProbe);
  assert.deepEqual(boundedToolResult(budgetProbe, exactBudget), budgetProbe);
  assert.throws(
    () => boundedToolResult(budgetProbe, exactBudget - 1),
    ToolResultBudgetError,
  );
  let toJsonCalls = 0;
  const statefulResult = {
    toJSON() {
      toJsonCalls += 1;
      return { content: [{ type: "text", text: "stable" }] };
    },
  };
  const statefulSnapshot = boundedToolResult(statefulResult, 1024);
  assert.equal(toJsonCalls, 1);
  statefulResult.toJSON = () => {
    throw new Error("a post-check serializer must never run");
  };
  assert.deepEqual(statefulSnapshot, {
    content: [{ type: "text", text: "stable" }],
  });
  console.log("TEST serialized_tool_result_budget_is_exact_and_snapshotted : PASS");

  const budgetServer = spawn(
    process.execPath,
    ["tests/response-budget-server.mjs"],
    { cwd: process.cwd(), stdio: ["pipe", "pipe", "pipe"] },
  );
  const budgetServerClosed = new Promise((resolve) => budgetServer.once("close", resolve));
  const budgetResponses = new Map();
  const budgetFrames = new Map();
  let budgetStdout = "";
  let budgetStderr = "";
  budgetServer.stdout.on("data", (chunk) => {
    budgetStdout += chunk.toString();
    for (;;) {
      const newline = budgetStdout.indexOf("\n");
      if (newline < 0) break;
      const line = budgetStdout.slice(0, newline);
      budgetStdout = budgetStdout.slice(newline + 1);
      if (!line.trim()) continue;
      const message = JSON.parse(line);
      if (message.id !== undefined) {
        budgetResponses.set(message.id, message);
        budgetFrames.set(message.id, Buffer.byteLength(`${line}\n`, "utf8"));
      }
    }
  });
  budgetServer.stderr.on("data", (chunk) => { budgetStderr += chunk.toString(); });
  budgetServer.stdin.write(JSON.stringify({
    jsonrpc: "2.0", id: 1, method: "initialize",
    params: {
      protocolVersion: "2024-11-05", capabilities: {},
      clientInfo: { name: "response-budget-regression", version: "1" },
    },
  }) + "\n");
  budgetServer.stdin.write(
    '{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
  );
  for (const [id, name] of [[2, "max_result"], [3, "over_result"]]) {
    budgetServer.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id, method: "tools/call",
      params: { name, arguments: {} },
    }) + "\n");
  }
  const publicBoundaryProbes = [
    [4, "strict_probe", { count: 1, unknown: "do-not-reflect" }],
    [5, "strict_probe", { count: "do-not-reflect" }],
    [6, "strict_probe", { count: 3 }],
    [7, "not_a_registered_tool", { secret: "do-not-reflect" }],
    [8, 7, {}],
    [9, "strict_probe", "do-not-reflect"],
  ];
  for (const [id, name, arguments_] of publicBoundaryProbes) {
    budgetServer.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id, method: "tools/call",
      params: { name, arguments: arguments_ },
    }) + "\n");
  }
  const budgetDeadline = Date.now() + 15_000;
  while (Date.now() < budgetDeadline &&
         ![1, 2, 3, 4, 5, 6, 7, 8, 9].every((id) => budgetResponses.has(id))) {
    await pause(20);
  }
  budgetServer.kill("SIGTERM");
  await budgetServerClosed;
  assert.ok(
    [1, 2, 3, 4, 5, 6, 7, 8, 9].every((id) => budgetResponses.has(id)),
    budgetStderr,
  );
  const maximumResult = budgetResponses.get(2).result;
  assert.equal(
    Buffer.byteLength(JSON.stringify(maximumResult), "utf8"),
    MAX_PUBLIC_TOOL_RESULT_BYTES,
  );
  assert.ok(budgetFrames.get(2) > MAX_PUBLIC_TOOL_RESULT_BYTES);
  assert.ok(budgetFrames.get(2) < 10 * 1024 * 1024);
  assert.deepEqual(JSON.parse(budgetResponses.get(3).result.content[0].text), {
    error: {
      code: "internal_error",
      message: PUBLIC_TOOL_ERROR_MESSAGES.internal_error,
    },
  });
  assert.ok(budgetFrames.get(3) < 1024);
  console.log("TEST maximum_tool_result_and_one_byte_over_cross_real_stdio_safely : PASS");

  const fixedInvalidRequest = JSON.stringify({
    error: {
      code: "invalid_request",
      message: PUBLIC_TOOL_ERROR_MESSAGES.invalid_request,
    },
  });
  for (const [id] of publicBoundaryProbes) {
    const response = budgetResponses.get(id);
    assert.equal(response.result?.isError, true, JSON.stringify(response));
    assert.equal(response.result?.content?.length, 1, JSON.stringify(response));
    assert.equal(response.result?.content?.[0]?.text, fixedInvalidRequest);
    assert.equal(JSON.stringify(response).includes("do-not-reflect"), false);
  }
  console.log("TEST public_lifecycle_exercises_fixed_value_free_tool_boundary : PASS");

  const overflow = JSON.parse('{"go_target":1e309,"nested":[{"value":-1e309}]}');
  assert.equal(overflow.go_target, Infinity);
  assert.throws(() => assertFiniteNumericInputs(overflow), /must be finite/);
  console.log("TEST raw_json_exponent_overflow_is_rejected_recursively : PASS");

  // This decimal spelling survives stringify even though V8 has already
  // rounded its numeric value. Text equality therefore cannot detect the
  // corruption; the governed boundary must reject the unsafe Number itself.
  const unsafePositive = JSON.parse('{"value":1000000000000000100}');
  const unsafeNegative = JSON.parse('{"value":-1000000000000000100}');
  const unsafeNested = JSON.parse('{"outer":[{"value":-1000000000000000100}]}');
  assert.equal(Number.isSafeInteger(unsafePositive.value), false);
  assert.equal(BigInt(unsafePositive.value), 1000000000000000128n);
  assert.notEqual(BigInt(unsafePositive.value), 1000000000000000100n);
  assert.equal(JSON.stringify(unsafePositive), '{"value":1000000000000000100}');
  assert.equal(JSON.stringify(unsafeNegative), '{"value":-1000000000000000100}');
  assert.equal(
    JSON.stringify(unsafeNested),
    '{"outer":[{"value":-1000000000000000100}]}',
  );
  for (const unsafe of [unsafePositive, unsafeNegative, unsafeNested]) {
    assert.throws(
      () => assertFiniteNumericInputs(unsafe),
      /safe-integer range/,
    );
  }
  assert.doesNotThrow(() => assertFiniteNumericInputs({
    lower: Number.MIN_SAFE_INTEGER,
    upper: Number.MAX_SAFE_INTEGER,
    fractional: 1.0000000000000002,
  }));
  console.log("TEST unsafe_integral_json_numbers_are_rejected_recursively : PASS");

  for (const scaled of [
    [1, 2],
    [2, 4],
    [0.00001, 0.00002],
    [1e-300, 2e-300],
  ]) {
    assert.deepEqual(normalizedAllocationWeights(scaled), [1, 2]);
  }
  assert.deepEqual(normalizedAllocationWeights([1.3333333333, 1]), [4, 3]);
  assert.deepEqual(normalizedAllocationWeights([1e-300, 1e-300]), [1, 1]);
  assert.equal(normalizedAllocationWeights([1e-300, 1e300]), null);
  console.log("TEST allocation_ratio_normalization_is_scale_invariant : PASS");

  const runtimeTreeRoot = join(workspace, "bounded-runtime-tree");
  const runtimeTreeNested = join(runtimeTreeRoot, "nested");
  mkdirSync(runtimeTreeNested, { recursive: true, mode: 0o700 });
  writeFileSync(join(runtimeTreeRoot, "root.bin"), "root\n", { mode: 0o600 });
  writeFileSync(join(runtimeTreeNested, "nested.bin"), "nested\n", { mode: 0o600 });
  const boundedTreeFingerprint = await hashBoundedRuntimeTree(
    [runtimeTreeRoot], "bounded-tree", boundedRuntimeLimits(),
  );
  assert.equal(
    await hashBoundedRuntimeTree(
      [runtimeTreeRoot], "bounded-tree", boundedRuntimeLimits(),
    ),
    boundedTreeFingerprint,
  );

  const oversizedRuntimeFile = join(workspace, "oversized-runtime.bin");
  writeFileSync(oversizedRuntimeFile, Buffer.alloc(2_049), { mode: 0o600 });
  await assert.rejects(
    hashBoundedRuntimeTree(
      [oversizedRuntimeFile], "oversized", boundedRuntimeLimits({
        maxFileBytes: 2_048,
        maxTotalBytes: 4_096,
      }),
    ),
    /file exceeds the 2048-byte limit/,
  );

  const manyRuntimeFiles = join(workspace, "many-runtime-files");
  mkdirSync(manyRuntimeFiles, { mode: 0o700 });
  for (let index = 0; index < 3; index += 1) {
    writeFileSync(join(manyRuntimeFiles, `${index}.bin`), `${index}\n`, { mode: 0o600 });
  }
  await assert.rejects(
    hashBoundedRuntimeTree(
      [manyRuntimeFiles], "many-files", boundedRuntimeLimits({ maxFiles: 2 }),
    ),
    /exceeds the 2-file limit/,
  );
  await assert.rejects(
    hashBoundedRuntimeTree(
      [manyRuntimeFiles], "many-entries",
      boundedRuntimeLimits({ maxDirectoryEntries: 2 }),
    ),
    /exceeds the 2-directory-entry limit/,
  );

  const deepRuntimeTree = join(workspace, "deep-runtime-tree");
  const deepRuntimeLeaf = join(deepRuntimeTree, "one", "two", "three");
  mkdirSync(deepRuntimeLeaf, { recursive: true, mode: 0o700 });
  writeFileSync(join(deepRuntimeLeaf, "leaf.bin"), "leaf\n", { mode: 0o600 });
  await assert.rejects(
    hashBoundedRuntimeTree(
      [deepRuntimeTree], "deep-tree", boundedRuntimeLimits({ maxDepth: 2 }),
    ),
    /exceeds the 2-level depth limit/,
  );

  if (process.platform !== "win32") {
    const symlinkRuntimeRoot = join(workspace, "symlink-runtime-tree");
    mkdirSync(symlinkRuntimeRoot, { mode: 0o700 });
    const symlinkTarget = join(workspace, "symlink-runtime-target.bin");
    writeFileSync(symlinkTarget, "target\n", { mode: 0o600 });
    symlinkSync(symlinkTarget, join(symlinkRuntimeRoot, "linked.bin"));
    await assert.rejects(
      hashBoundedRuntimeTree(
        [symlinkRuntimeRoot], "symlink-tree", boundedRuntimeLimits(),
      ),
      /refuses symbolic links/,
    );
  }

  const mutatingRuntimeFile = join(workspace, "mutating-runtime.bin");
  writeFileSync(mutatingRuntimeFile, Buffer.alloc(4 * 1024 * 1024), { mode: 0o600 });
  const mutatingHash = hashBoundedRuntimeTree(
    [mutatingRuntimeFile], "mutating-file", boundedRuntimeLimits(),
  );
  const mutationTimer = setInterval(() => {
    appendFileSync(mutatingRuntimeFile, "x");
  }, 1);
  try {
    await assert.rejects(mutatingHash, /changed while (opening|reading)/);
  } finally {
    clearInterval(mutationTimer);
  }

  const cancellableRuntimeFile = join(workspace, "cancellable-runtime.bin");
  writeFileSync(cancellableRuntimeFile, Buffer.alloc(4 * 1024 * 1024), { mode: 0o600 });
  const runtimeHashController = new AbortController();
  const cancellableHash = hashBoundedRuntimeTree(
    [cancellableRuntimeFile], "cancelled-file",
    boundedRuntimeLimits({ readChunkBytes: 1_024 }),
    runtimeHashController.signal,
  );
  setImmediate(() => runtimeHashController.abort());
  await assert.rejects(cancellableHash, (error) => error?.name === "AbortError");
  console.log("TEST runtime_tree_hashing_is_bounded_rebound_and_cancellable : PASS");

  const hostileChildEnvironment = {
    OPENAI_API_KEY: "host-secret",
    ANTHROPIC_API_KEY: "host-secret",
    HTTPS_PROXY: "http://hostile.invalid",
    http_proxy: "http://hostile.invalid",
    EXPDESIGN_UNTRUSTED_FEATURE: "enabled",
    R_TESTS: join(workspace, "hostile-r-tests.R"),
    OMP_NUM_THREADS: "999",
  };
  const previousHostileEnvironment = new Map(
    Object.keys(hostileChildEnvironment).map((name) => [name, process.env[name]]),
  );
  Object.assign(process.env, hostileChildEnvironment);
  try {
    for (const environment of [
      sanitizedRChildEnvironment(), sanitizedPythonChildEnvironment(),
    ]) {
      for (const name of [
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "HTTPS_PROXY", "http_proxy",
        "EXPDESIGN_UNTRUSTED_FEATURE", "R_TESTS",
      ]) {
        assert.equal(environment[name], undefined, `${name} leaked into a runtime target`);
      }
      assert.equal(environment.OMP_NUM_THREADS, "1");
      assert.equal(environment.LANG, "C");
      assert.equal(environment.LC_ALL, "C");
      assert.equal(environment.TZ, "UTC");
      assert.ok(environment.HOME);
      assert.ok(environment.TEMP);
      assert.ok(environment.TMP);
      assert.ok(environment.PATH);
      const environmentProbe = spawnSync(
        process.execPath,
        ["--input-type=module", "-e", "console.log(JSON.stringify(process.env))"],
        { encoding: "utf8", env: environment },
      );
      assert.equal(environmentProbe.status, 0, environmentProbe.stderr);
      const observed = JSON.parse(environmentProbe.stdout);
      assert.equal(observed.OPENAI_API_KEY, undefined);
      assert.equal(observed.HTTPS_PROXY, undefined);
      assert.equal(observed.R_TESTS, undefined);
      assert.equal(observed.OMP_NUM_THREADS, "1");
    }
  } finally {
    for (const [name, value] of previousHostileEnvironment) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
  console.log("TEST runtime_target_environments_use_a_closed_deterministic_allowlist : PASS");

  const oversizedVerifierMarker = "VERIFIER_STDOUT_SECRET_MARKER";
  const oversizedVerifier = join(workspace, "oversized-verifier-stdout.py");
  writeFileSync(
    oversizedVerifier,
    "import sys\n" +
      `marker = ${JSON.stringify(oversizedVerifierMarker)}.encode('ascii')\n` +
      `limit = ${MAX_VERIFIER_STDOUT_BYTES}\n` +
      "sys.stdout.buffer.write(marker + b'x' * (limit + 1 - len(marker)))\n" +
      "sys.stdout.buffer.flush()\n",
    { mode: 0o600 },
  );
  await assert.rejects(
    runVerifierRequest("{}", undefined, oversizedVerifier),
    (error) => {
      assert.equal(
        error?.message,
        `verification process output exceeded the ${MAX_VERIFIER_STDOUT_BYTES}-byte limit`,
      );
      assert.equal(error.message.includes(oversizedVerifierMarker), false);
      return true;
    },
  );
  console.log("TEST verifier_stdout_is_byte_bounded_without_content_leakage : PASS");

  const dependencyRoot = join(workspace, "installed-sdk");
  mkdirSync(dependencyRoot, { mode: 0o700 });
  const dependencyProbe = join(dependencyRoot, "runtime.js");
  writeFileSync(dependencyProbe, "export const version = 1;\n", { mode: 0o600 });
  const dependencyFingerprint = fingerprintStartupRuntime(
    process.execPath, [], [dependencyRoot],
  );
  appendFileSync(dependencyProbe, "export const changed = true;\n");
  assert.notEqual(
    fingerprintStartupRuntime(process.execPath, [], [dependencyRoot]),
    dependencyFingerprint,
  );
  console.log("TEST installed_dependency_byte_change_alters_startup_fingerprint : PASS");

  const pythonVerifierRoot = join(workspace, "python-verifier-runtime");
  mkdirSync(pythonVerifierRoot, { mode: 0o700 });
  const pythonVerifierDependency = join(
    pythonVerifierRoot, "fingerprint_fixture_dependency.py",
  );
  const syntheticPythonVerifier = join(pythonVerifierRoot, "synthetic_verifier.py");
  writeFileSync(pythonVerifierDependency, "VALUE = 1\n", { mode: 0o600 });
  writeFileSync(
    syntheticPythonVerifier,
    "import fingerprint_fixture_dependency\nimport hashlib\n",
    { mode: 0o600 },
  );
  const pythonVerifierFingerprint = await currentPythonRuntimeSnapshot(
    undefined, syntheticPythonVerifier,
  );
  assert.equal(
    (await currentPythonRuntimeSnapshot(undefined, syntheticPythonVerifier)).fingerprint,
    pythonVerifierFingerprint.fingerprint,
  );
  writeFileSync(pythonVerifierDependency, "VALUE = 2\n", { mode: 0o600 });
  assert.notEqual(
    (await currentPythonRuntimeSnapshot(undefined, syntheticPythonVerifier)).fingerprint,
    pythonVerifierFingerprint.fingerprint,
  );
  console.log("TEST imported_python_module_byte_change_alters_runtime_fingerprint : PASS");

  const fakeInstall = join(workspace, "flattened-production-install");
  const fakeSdk = join(fakeInstall, "node_modules", "@modelcontextprotocol", "sdk");
  const fakeTransitive = join(fakeInstall, "node_modules", "transitive-runtime");
  const fakeDev = join(fakeInstall, "node_modules", "dev-only");
  mkdirSync(fakeSdk, { recursive: true, mode: 0o700 });
  mkdirSync(fakeTransitive, { recursive: true, mode: 0o700 });
  mkdirSync(fakeDev, { recursive: true, mode: 0o700 });
  writeFileSync(join(fakeSdk, "index.js"), "export const sdk = 1;\n");
  const transitiveRuntime = join(fakeTransitive, "index.js");
  writeFileSync(transitiveRuntime, "export const transitive = 1;\n");
  writeFileSync(join(fakeDev, "index.js"), "export const dev = 1;\n");
  const fakeLock = join(fakeInstall, "package-lock.json");
  writeFileSync(fakeLock, JSON.stringify({
    lockfileVersion: 3,
    packages: {
      "": { dependencies: { "@modelcontextprotocol/sdk": "1" } },
      "node_modules/@modelcontextprotocol/sdk": { version: "1" },
      "node_modules/transitive-runtime": { version: "1" },
      "node_modules/dev-only": { version: "1", dev: true },
    },
  }));
  const productionRoots = installedProductionDependencyRoots(fakeInstall, fakeLock);
  assert.deepEqual(productionRoots, [realpathSync(fakeSdk), realpathSync(fakeTransitive)].sort());
  const flattenedFingerprint = fingerprintStartupRuntime(
    process.execPath, [fakeLock], productionRoots,
  );
  appendFileSync(transitiveRuntime, "export const tampered = true;\n");
  assert.notEqual(
    fingerprintStartupRuntime(process.execPath, [fakeLock], productionRoots),
    flattenedFingerprint,
  );
  assert.throws(
    () => fingerprintStartupRuntime(
      process.execPath, [fakeLock], productionRoots, process.version,
      { maxFiles: 1, maxBytes: 1024 * 1024 },
    ),
    /dependency closure exceeds fingerprint limits/,
  );
  console.log("TEST flattened_production_transitive_bytes_are_bounded_and_fingerprinted : PASS");

  const supervisorDist = join(process.cwd(), "dist", "runtime-supervisor.js");
  const capturedSupervisorProgram = readFileSync(supervisorDist, "utf8");
  assert.equal(
    PINNED_RUNTIME_SUPERVISOR_COMMITMENT,
    runtimeSupervisorProgramCommitment(capturedSupervisorProgram),
  );
  const growingSupervisorProgram = join(workspace, "growing-runtime-supervisor.js");
  const initialGrowingSupervisorBytes = "export const captured = true;\n";
  writeFileSync(growingSupervisorProgram, initialGrowingSupervisorBytes, { mode: 0o600 });
  assert.equal(
    captureBoundedSupervisorProgram(growingSupervisorProgram),
    initialGrowingSupervisorBytes,
  );
  let supervisorGrowthHookRan = false;
  assert.throws(
    () => captureBoundedSupervisorProgram(growingSupervisorProgram, () => {
      supervisorGrowthHookRan = true;
      appendFileSync(
        growingSupervisorProgram,
        Buffer.alloc(MAX_SUPERVISOR_PROGRAM_BYTES + 1, 0x78),
      );
    }),
    /runtime supervisor program grew while it was captured/,
  );
  assert.equal(supervisorGrowthHookRan, true);
  console.log("TEST supervisor_capture_rejects_post_size_check_growth_before_unbounded_read : PASS");

  const mutatedSupervisorProgram = `${capturedSupervisorProgram}\n` +
    "// deterministic post-capture mutation\n";
  const startupFilesWithoutSupervisor = fs.readdirSync(join(process.cwd(), "dist"))
    .filter((name) => name.endsWith(".js") && name !== "runtime-supervisor.js")
    .map((name) => join(process.cwd(), "dist", name));
  let fingerprintWhileSupervisorPathWasMutated;
  let mutatedSupervisorBaseline;
  try {
    writeFileSync(supervisorDist, mutatedSupervisorProgram);
    fingerprintWhileSupervisorPathWasMutated = fingerprintStartupRuntime(
      process.execPath,
      [
        ...startupFilesWithoutSupervisor,
        join(process.cwd(), "package.json"),
        join(process.cwd(), "package-lock.json"),
        join(process.cwd(), "..", "governance", "runtime-profiles.json"),
        join(process.cwd(), "..", "governance", "runtime_profile.mjs"),
      ],
      installedProductionDependencyRoots(process.cwd()),
      process.version,
      {},
      [PINNED_RUNTIME_SUPERVISOR_COMMITMENT],
    );
    mutatedSupervisorBaseline = fingerprintStartupRuntime(
      process.execPath,
      [
        ...startupFilesWithoutSupervisor,
        join(process.cwd(), "package.json"),
        join(process.cwd(), "package-lock.json"),
        join(process.cwd(), "..", "governance", "runtime-profiles.json"),
        join(process.cwd(), "..", "governance", "runtime_profile.mjs"),
      ],
      installedProductionDependencyRoots(process.cwd()),
      process.version,
      {},
      [runtimeSupervisorProgramCommitment(mutatedSupervisorProgram)],
    );
  } finally {
    writeFileSync(supervisorDist, capturedSupervisorProgram);
  }
  assert.equal(
    fingerprintWhileSupervisorPathWasMutated,
    STARTUP_RUNTIME_FINGERPRINT,
    "startup fingerprint followed a post-capture supervisor pathname mutation",
  );
  assert.notEqual(mutatedSupervisorBaseline, STARTUP_RUNTIME_FINGERPRINT);
  console.log("TEST startup_fingerprint_binds_pinned_supervisor_eval_bytes : PASS");

  const engineRoot = join(workspace, "engine-runtime-manifest");
  const maintainedEngineFiles = Object.values(ENGINE_RUNTIME_RELATIVE_FILE_CATEGORIES)
    .flatMap((category) => category)
    .filter((path) => ACTIVE_RUNTIME_PROFILE.name === "complete" || !path.startsWith(".claude/agents/") ||
      ["design-verifier.md", "experiment-design-coordinator.md", "experiment-designer.md", "single-endpoint-designer.md"]
        .some((name) => path === `.claude/agents/${name}`));
  const syntheticEngineFiles = [
    ...maintainedEngineFiles,
    ...REGRESSION_SKILLS.map((skill) => `${skill}/scripts/tests/run_tests.R`),
    ...REGRESSION_SKILLS.map((skill) => `${skill}/scripts/R/runtime_fixture.R`),
  ];
  for (const relativePath of syntheticEngineFiles) {
    const path = join(engineRoot, ...relativePath.split("/"));
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    writeFileSync(path, `runtime source: ${relativePath}\n`, { mode: 0o600 });
  }
  const profileSource = readFileSync(join(process.cwd(), "..", "governance", "runtime-profiles.json"), "utf8");
  writeFileSync(join(engineRoot, "governance", "runtime-profiles.json"), profileSource);
  const listedEngineFiles = new Set(mutableEngineFiles(engineRoot));
  for (const relativePath of maintainedEngineFiles) {
    assert.equal(
      listedEngineFiles.has(join(engineRoot, ...relativePath.split("/"))),
      true,
      `${relativePath} is absent from the engine runtime manifest`,
    );
  }
  const listedEngineRoots = new Set(mutableEngineRoots(engineRoot));
  for (const skill of REGRESSION_SKILLS) {
    assert.equal(
      listedEngineRoots.has(join(engineRoot, skill, "scripts", "R")),
      true,
      `${skill}/scripts/R is absent from the fixed engine roots`,
    );
  }
  let engineFingerprint = await fingerprintMutableEngineRuntime(
    engineRoot, "runtime-seed",
  );
  for (const relativePath of [
    ...maintainedEngineFiles,
    ...REGRESSION_SKILLS.map((skill) => `${skill}/scripts/R/runtime_fixture.R`),
  ]) {
    const path = join(engineRoot, ...relativePath.split("/"));
    appendFileSync(path, relativePath === "governance/runtime-profiles.json" ? "\n" : `mutation: ${relativePath}\n`);
    const mutatedFingerprint = await fingerprintMutableEngineRuntime(
      engineRoot, "runtime-seed",
    );
    assert.notEqual(
      mutatedFingerprint,
      engineFingerprint,
      `${relativePath} mutation did not alter the engine fingerprint`,
    );
    engineFingerprint = mutatedFingerprint;
  }

  const engineRRoot = join(engineRoot, REGRESSION_SKILLS[0], "scripts", "R");
  const oversizedEngineSource = join(engineRRoot, "oversized_runtime.R");
  writeFileSync(oversizedEngineSource, "", { mode: 0o600 });
  truncateSync(
    oversizedEngineSource,
    ENGINE_RUNTIME_FINGERPRINT_LIMITS.maxFileBytes + 1,
  );
  await assert.rejects(
    fingerprintMutableEngineRuntime(engineRoot, "oversized-engine"),
    new RegExp(
      `file exceeds the ${ENGINE_RUNTIME_FINGERPRINT_LIMITS.maxFileBytes}-byte limit`,
    ),
  );
  unlinkSync(oversizedEngineSource);

  if (process.platform !== "win32") {
    const outsideEngineSource = join(workspace, "outside-engine-source.R");
    const linkedEngineSource = join(engineRRoot, "linked_runtime.R");
    writeFileSync(outsideEngineSource, "outside <- TRUE\n", { mode: 0o600 });
    symlinkSync(outsideEngineSource, linkedEngineSource);
    await assert.rejects(
      fingerprintMutableEngineRuntime(engineRoot, "linked-engine"),
      /refuses symbolic links/,
    );
    unlinkSync(linkedEngineSource);
  }

  const mutatingEngineSource = join(engineRRoot, "mutating_runtime.R");
  writeFileSync(mutatingEngineSource, Buffer.alloc(4 * 1024 * 1024), { mode: 0o600 });
  const mutatingEngineFingerprint = fingerprintMutableEngineRuntime(
    engineRoot, "mutating-engine",
  );
  const engineMutationTimer = setInterval(() => {
    appendFileSync(mutatingEngineSource, "x");
  }, 1);
  try {
    await assert.rejects(
      mutatingEngineFingerprint,
      /changed while (?:opening|reading)/,
    );
  } finally {
    clearInterval(engineMutationTimer);
    unlinkSync(mutatingEngineSource);
  }
  console.log("TEST engine_runtime_roots_are_bounded_rebound_and_fingerprinted : PASS");

  const importHandlerProbe = spawnSync(process.execPath, [
    "--input-type=module", "-e", `
      const before = {
        sigint: process.listenerCount("SIGINT"),
        sigterm: process.listenerCount("SIGTERM"),
        stdinEnd: process.stdin.listenerCount("end"),
        stdinClose: process.stdin.listenerCount("close"),
      };
      await import("./dist/index.js");
      const after = {
        sigint: process.listenerCount("SIGINT"),
        sigterm: process.listenerCount("SIGTERM"),
        stdinEnd: process.stdin.listenerCount("end"),
        stdinClose: process.stdin.listenerCount("close"),
      };
      if (JSON.stringify(before) !== JSON.stringify(after)) process.exitCode = 2;
    `,
  ], { cwd: process.cwd(), encoding: "utf8", timeout: 10_000 });
  assert.equal(importHandlerProbe.status, 0, importHandlerProbe.stderr);
  console.log("TEST importing_server_installs_no_process_handlers : PASS");

  const dispatcherSource = readFileSync(
    join(process.cwd(), "r-wrapper", "dispatcher.R"),
    "utf8",
  );
  const dispatcherEngineFiles = [];
  for (const match of dispatcherSource.matchAll(
    /source_skill\(\s*"([^"]+)"([\s\S]*?)\)/g,
  )) {
    const skill = match[1];
    for (const fileMatch of match[2].matchAll(/"([^"]+\.R)"/g)) {
      dispatcherEngineFiles.push(`${skill}/scripts/R/${fileMatch[1]}`);
    }
  }
  const expectedRequiredEngineFiles = [
    ...new Set([
      ...dispatcherEngineFiles.filter((path) => ACTIVE_RUNTIME_PROFILE.skills.includes(path.split("/")[0])),
      ...REGRESSION_SKILLS.map(
        (skill) => `${skill}/scripts/tests/run_tests.R`,
      ),
    ]),
  ].sort();
  assert.deepEqual(
    [...REQUIRED_PRIVATE_ENGINE_RUNTIME_FILES].sort(),
    expectedRequiredEngineFiles,
  );
  console.log("TEST executable_engine_preflight_manifest_matches_dispatcher : PASS");

  const publicCopyRoot = join(workspace, "public-portfolio-copy");
  const publicCopyMcpRoot = join(publicCopyRoot, "mcp-server");
  mkdirSync(publicCopyMcpRoot, { recursive: true, mode: 0o700 });
  cpSync(join(process.cwd(), "dist"), join(publicCopyMcpRoot, "dist"), {
    recursive: true,
  });
  cpSync(
    join(process.cwd(), "package.json"),
    join(publicCopyMcpRoot, "package.json"),
  );
  cpSync(
    join(process.cwd(), "package-lock.json"),
    join(publicCopyMcpRoot, "package-lock.json"),
  );
  symlinkSync(
    realpathSync(join(process.cwd(), "node_modules")),
    join(publicCopyMcpRoot, "node_modules"),
    "dir",
  );

  mkdirSync(join(publicCopyRoot, "governance"));
  cpSync(join(process.cwd(), "..", "governance", "runtime_profile.mjs"),
    join(publicCopyRoot, "governance", "runtime_profile.mjs"));
  const singleProfileManifest = JSON.parse(profileSource);
  singleProfileManifest.active = "single-endpoint";
  writeFileSync(join(publicCopyRoot, "governance", "runtime-profiles.json"),
    JSON.stringify(singleProfileManifest, null, 2) + "\n");
  assert.equal(loadRuntimeProfile(publicCopyRoot).name, "single-endpoint");
  assert.equal(requiredEngineRuntimeFiles(publicCopyRoot).length, 7);
  assert.equal(requiredEngineRuntimeFiles(publicCopyRoot).every((path) => path.startsWith("vera-experiment-designing/")), true);

  const incompleteImportProbe = spawnSync(process.execPath, [
    "--input-type=module", "-e", 'await import("./dist/index.js")',
  ], {
    cwd: publicCopyMcpRoot,
    encoding: "utf8",
    timeout: 10_000,
  });
  assert.equal(incompleteImportProbe.status, 0, incompleteImportProbe.stderr);

  const initializeRequest = JSON.stringify({
    jsonrpc: "2.0", id: 1, method: "initialize",
    params: {
      protocolVersion: "2024-11-05", capabilities: {},
      clientInfo: { name: "incomplete-installation-regression", version: "1" },
    },
  });
  const incompleteExecutableProbe = spawnSync(
    process.execPath,
    ["dist/index.js"],
    {
      cwd: publicCopyMcpRoot,
      encoding: "utf8",
      input: `${initializeRequest}\n`,
      timeout: 10_000,
      env: {
        ...process.env,
        // A plausible bypass name must have no effect; no supported bypass
        // exists for the executable startup preflight.
        EXPDESIGN_SKIP_ENGINE_PREFLIGHT: "1",
      },
    },
  );
  assert.equal(incompleteExecutableProbe.status, 1, incompleteExecutableProbe.stderr);
  assert.equal(incompleteExecutableProbe.stdout, "");
  assert.equal(
    incompleteExecutableProbe.stderr.trim(),
    MISSING_PRIVATE_ENGINE_INSTALLATION_MESSAGE,
  );
  assert.equal(incompleteExecutableProbe.stderr.includes(publicCopyRoot), false);
  assert.equal(incompleteExecutableProbe.stderr.includes("vera-"), false);
  console.log("TEST executable_preflight_refuses_incomplete_public_copy_before_mcp_connect : PASS");

  for (const relativePath of requiredEngineRuntimeFiles(publicCopyRoot)) {
    const target = join(publicCopyRoot, relativePath);
    mkdirSync(dirname(target), { recursive: true });
    writeFileSync(target, "# availability fixture only\n");
  }
  assert.doesNotThrow(() => assertRequiredPrivateEngineInstallation(publicCopyRoot));
  const missingCoreFile = join(publicCopyRoot, "vera-experiment-designing/scripts/R/ppos.R");
  unlinkSync(missingCoreFile);
  assert.throws(() => assertRequiredPrivateEngineInstallation(publicCopyRoot), /required by the repository runtime profile is unavailable/);
  singleProfileManifest.active = "complete";
  writeFileSync(join(publicCopyRoot, "governance/runtime-profiles.json"), JSON.stringify(singleProfileManifest));
  assert.throws(() => assertRequiredPrivateEngineInstallation(publicCopyRoot), /required by the repository runtime profile is unavailable/);
  console.log("TEST runtime_profiles_require_exact_installed_core_without_skipping : PASS");

  if (publicOnly) {
    console.log("TEST public_only_lifecycle_stops_after_public_preflight : PASS");
  } else {
  if (process.platform !== "win32") {
    const rMarker = join(workspace, "unexpected-r-execution.txt");
    const rShim = join(workspace, "forbidden-rscript.sh");
    writeFileSync(
      rShim,
      `#!/bin/sh\necho executed > ${JSON.stringify(rMarker)}\nexit 99\n`,
      { mode: 0o700 },
    );
    const child = spawn(process.execPath, ["dist/index.js"], {
      cwd: process.cwd(),
      env: { ...process.env, EXPDESIGN_RSCRIPT: rShim },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const childClosed = new Promise((resolve) => child.once("close", resolve));
    let stdout = "";
    let stderr = "";
    const responses = new Map();
    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString();
      for (;;) {
        const newline = stdout.indexOf("\n");
        if (newline < 0) break;
        const line = stdout.slice(0, newline).trim();
        stdout = stdout.slice(newline + 1);
        if (!line) continue;
        const message = JSON.parse(line);
        if (message.id !== undefined) responses.set(message.id, message);
      }
    });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    const initialization = JSON.stringify({
      jsonrpc: "2.0", id: 1, method: "initialize",
      params: {
        protocolVersion: "2024-11-05", capabilities: {},
        clientInfo: { name: "security-regression", version: "1" },
      },
    });
    const common = '"endpoint_type":"binary","study_type":"poc",' +
      '"design":"single_arm","null_param":0.2,"alt_param":0.4';
    const alphaGrid = Array.from({ length: 65 }, () => 0.05);
    const prior = Object.fromEntries(
      Array.from({ length: 17 }, (_value, index) => [`field_${index}`, 1]),
    );
    child.stdin.write(initialization + "\n");
    child.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');
    child.stdin.write(
      '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{' +
      '"name":"validate_config","arguments":{' + common + ',"go_target":1e309}}}\n',
    );
    child.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id: 3, method: "tools/call",
      params: { name: "validate_config", arguments: {
        endpoint_type: "binary", study_type: "poc", design: "single_arm",
        null_param: 0.2, alt_param: 0.4, alphas: alphaGrid,
      } },
    }) + "\n");
    child.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id: 4, method: "tools/call",
      params: { name: "validate_config", arguments: {
        endpoint_type: "binary", study_type: "poc", design: "single_arm",
        null_param: 0.2, alt_param: 0.4, prior,
      } },
    }) + "\n");
    child.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id: 5, method: "tools/call",
      params: { name: "simulate_design", arguments: { config: {
        endpoint_type: "binary", study_type: "poc", design: "single_arm",
        null_param: 0.2, alt_param: 0.4, label: "x".repeat(257),
      } } },
    }) + "\n");
    child.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id: 6, method: "tools/call",
      params: { name: "factorial_design", arguments: {
        n_factors: 12, replicates: 100,
      } },
    }) + "\n");
    child.stdin.write(
      '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{' +
      '"name":"validate_config","arguments":{' +
      '"endpoint_type":"continuous","study_type":"poc",' +
      '"design":"single_arm","null_param":1000000000000000100,' +
      '"alt_param":1000000000000000200,"sd":1}}}\n',
    );
    child.stdin.write(JSON.stringify({
      jsonrpc: "2.0", id: 8, method: "tools/call",
      params: { name: "factorial_design", arguments: {
        n_factors: 5, fraction: 1, levels: 3,
      } },
    }) + "\n");
    const contractRequests = [
      [9, "validate_config", {
        endpoint_type: "binary", study_type: "poc", design: "single_arm",
        null_param: 0.2, alt_param: 0.4, unknown_field: "do-not-reflect",
      }],
      [10, "randomize", { n: "12" }],
      [11, "randomize", { n: 10001 }],
      [12, "randomize", { n: 12, method: "invalid-enum-value" }],
      [13, "randomize", {}],
      [14, "not_a_registered_tool", { secret_value: "do-not-reflect" }],
      [15, "randomize", { n: 12, arms: 3, ratio: [1, 1] }],
      [16, "randomize", {
        n: 3, method: "stratified", strata: ["a", "b"],
      }],
      [17, "rsm_design", { n_factors: 2, design: "bbd" }],
      [18, "rsm_design", {
        n_factors: 3, design: "bbd", alpha: "face",
      }],
      [19, "rsm_design", { n_factors: 3, design: "ccd", fraction: 3 }],
      [20, "validate_config", {
        endpoint_type: "continuous", study_type: "poc", design: "single_arm",
        null_param: 0, alt_param: 1,
      }],
      [21, "validate_config", {
        endpoint_type: "tte", study_type: "poc", design: "single_arm",
        null_param: 1, alt_param: 0.8,
      }],
      [22, "validate_config", {
        endpoint_type: "incidence_rate", study_type: "poc", design: "single_arm",
        null_param: 1, alt_param: 0.8,
      }],
      [23, "ab_test", { baseline: 10, effect: 1, metric: "mean" }],
      [24, "rsm_design", { n_factors: 3, seed: 123 }],
      [25, "randomize", "do-not-reflect"],
      [26, "randomize", ["do-not-reflect"]],
      [27, 7, {}],
      [28, undefined, {}],
      [29, "indirect_compare", { method: "bucher" }],
      [30, "indirect_compare", {
        method: "bucher", comparisons: [{
          estimate_ab: 0.2, se_ab: 0.1, estimate_cb: 0.1, se_cb: 0.1,
          treatment_a: "a", treatment_c: "c", common_comparator: "b",
        }], ipd_file: "/do-not-reflect",
      }],
      [31, "indirect_compare", {
        method: "maic", ipd_file: "/do-not-reflect",
        targets_file: "/do-not-reflect",
      }],
      [32, "indirect_compare", {
        method: "maic", ipd_file: "/do-not-reflect",
        targets_file: "/do-not-reflect", treatment_arm: "active",
        maic_endpoint_type: "rate",
      }],
      [33, "indirect_compare", {
        method: "maic", ipd_file: "/do-not-reflect",
        targets_file: "/do-not-reflect", treatment_arm: "active",
        maic_endpoint_type: "tte", time_col: "time",
      }],
      [34, "indirect_compare", {
        method: "maic", ipd_file: "/do-not-reflect",
        targets_file: "/do-not-reflect", treatment_arm: "active",
        comparisons: [{
          estimate_ab: 0.2, se_ab: 0.1, estimate_cb: 0.1, se_cb: 0.1,
          treatment_a: "a", treatment_c: "c", common_comparator: "b",
        }],
      }],
      [35, "indirect_compare", {
        method: "maic", ipd_file: "/do-not-reflect",
        targets_file: "/do-not-reflect", treatment_arm: "active",
        maic_endpoint_type: "tte", time_col: "time", status_col: "status",
        tte_method: "cox", bootstrap_replicates: 200,
      }],
      [36, "meta_analyze", {
        endpoint_type: "time_to_event", studies: [{}, {}], random: false,
        inference_method: "hksj",
      }],
      [37, "master_simulate", { config: {
        master_design_type: "umbrella", endpoint_type: "binary",
        n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
        phase: "phase2",
      } }],
      [38, "master_simulate", { config: {
        master_design_type: "platform", endpoint_type: "binary",
        n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
        borrowing_method: "none",
      } }],
      [39, "master_simulate", { config: {
        master_design_type: "basket", endpoint_type: "binary",
        n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
        umbrella_method: "mams",
      } }],
      [40, "master_simulate", { config: {
        master_design_type: "basket", endpoint_type: "binary",
        n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
        ncc_method: "none",
      } }],
      [41, "master_simulate", { config: {
        master_design_type: "basket", endpoint_type: "binary",
        n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
        soc_data: { means: [0.2, 0.2] },
      } }],
      [42, "simulate_design", { config: {
        endpoint_type: "binary", study_type: "confirmatory",
        design: "single_arm", null_param: 0.2, alt_param: 0.4,
        p2_data: { x_bar: 0.3, s2: 1, n: 10 },
      } }],
      [43, "simulate_design", { config: {
        endpoint_type: "binary", study_type: "confirmatory",
        design: "controlled", null_param: 0.2, alt_param: 0.4,
        p2_data: { x: 3, n: 10 },
      } }],
      [44, "simulate_design", { config: {
        endpoint_type: "binary", study_type: "confirmatory",
        design: "controlled", null_param: 0.2, alt_param: 0.4,
        p2_data_ctrl: { x: 2, n: 10 },
      } }],
      [45, "simulate_design", { config: {
        endpoint_type: "binary", study_type: "confirmatory",
        design: "single_arm", null_param: 0.2, alt_param: 0.4,
        p3_n: 100,
      } }],
      [46, "simulate_design", { config: {
        endpoint_type: "binary", study_type: "confirmatory",
        design: "single_arm", null_param: 0.2, alt_param: 0.4,
        p2_data: { x: 3, n: 10 }, p3_alloc_ratio: 1,
      } }],
      [47, "meta_analyze", {
        endpoint_type: "binary_single",
        studies: [{ responders: 3, total: 10, hr: 0.8 }, {}],
      }],
      [48, "ab_test", { baseline: 0, effect: 0.1, metric: "proportion" }],
      [49, "ab_test", {
        baseline: 0.2, effect: 0.1, metric: "proportion", sd: 1,
      }],
      [50, "randomize", { n: 12, method: "simple", block_size: 4 }],
      [51, "randomize", { n: 12, arms: ["A", "A"] }],
      [52, "randomize", { n: 12, arms: ["A", "   "] }],
      [53, "factorial_design", { n_factors: 3, seed: 123 }],
      [54, "validate_config", {
        endpoint_type: "binary", study_type: "poc", design: "single_arm",
        null_param: 0.2, alt_param: 0.4, sd: 1,
      }],
      [55, "validate_config", {
        endpoint_type: "continuous", study_type: "poc", design: "single_arm",
        null_param: 0, alt_param: 1, sd: 1, exposure_time: 1,
      }],
      [56, "validate_config", {
        endpoint_type: "tte", study_type: "poc", design: "single_arm",
        null_param: 1, alt_param: 0.8, accrual_time: 1, followup_time: 1,
        rate_method: "poisson",
      }],
      [57, "validate_config", {
        endpoint_type: "incidence_rate", study_type: "poc", design: "single_arm",
        null_param: 1, alt_param: 0.8, exposure_time: 1,
        tte_method: "exponential",
      }],
      [58, "validate_config", {
        endpoint_type: "binary", study_type: "poc", design: "single_arm",
        null_param: 0.2, alt_param: 0.4, alloc_ratio: 1,
      }],
    ];
    let nextContractId = 59;
    const addContractRequest = (name, arguments_) => {
      contractRequests.push([nextContractId, name, arguments_]);
      nextContractId += 1;
    };
    const basketContractBase = {
      master_design_type: "basket", endpoint_type: "binary",
      n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
      n_sims: 1,
    };
    const umbrellaContractBase = {
      master_design_type: "umbrella", endpoint_type: "binary",
      n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
      n_sims: 1,
    };
    const platformContractBase = {
      master_design_type: "platform", endpoint_type: "binary",
      n_subgroups: 2, null_params: 0.2, alt_params: [0.4, 0.4],
      n_periods: 2, n_per_period: 10,
      arms_schedule: { enter: [1, 1], leave: [2, 2] }, n_sims: 1,
    };
    const basketOnlyFieldValues = {
      n_per_subgroup: 10, borrowing_method: "none", phase: "phase2",
      n_interims: 1, n_per_interim: 2, go_threshold: 0.9,
      nogo_threshold: 0.1,
      tau_prior: { type: "half_normal", params: { scale: 1 } },
      homogeneity_prior: 0.5, response_prior: 0.5,
      cbhm_a: 0.5, cbhm_b: 0.5, ia_pruning_alpha: 0.1,
      chen_strategy: "d1",
    };
    for (const [field, value] of Object.entries(basketOnlyFieldValues)) {
      addContractRequest("master_simulate", {
        config: { ...umbrellaContractBase, [field]: value },
      });
    }
    const umbrellaOnlyFieldValues = {
      umbrella_method: "mams", n_arms: 2, n_stages: 2,
      n_per_arm_stage: 10, futility_boundaries: [0, 0],
      n_drop_per_stage: [1], rar_gamma: 1,
      selection_rule: "rank_best", power_type: "one_minimum",
    };
    for (const [field, value] of Object.entries(umbrellaOnlyFieldValues)) {
      addContractRequest("master_simulate", {
        config: { ...basketContractBase, [field]: value },
      });
    }
    const platformOnlyFieldValues = {
      n_periods: 2, n_per_period: 10,
      arms_schedule: { enter: [1, 1], leave: [2, 2] },
      shared_control: true, ncc_method: "none", ncc_weight_decay: 0.9,
      rar_enabled: true, rar_burn_in: 10, rar_min_alloc: 0.1,
      interim_frequency: 1, futility_threshold: 0.05,
    };
    for (const [field, value] of Object.entries(platformOnlyFieldValues)) {
      addContractRequest("master_simulate", {
        config: { ...basketContractBase, [field]: value },
      });
    }
    for (const config of [
      { ...basketContractBase, borrowing_method: "none",
        tau_prior: { type: "half_normal", params: { scale: 1 } } },
      { ...basketContractBase, borrowing_method: "none", cbhm_a: 0.5 },
      { ...basketContractBase, borrowing_method: "none", homogeneity_prior: 0.5 },
      { ...basketContractBase, borrowing_method: "none", ia_pruning_alpha: 0.1 },
      { ...umbrellaContractBase, umbrella_method: "mams", n_drop_per_stage: [1] },
      { ...umbrellaContractBase, umbrella_method: "mams", rar_gamma: 1 },
      { ...umbrellaContractBase, umbrella_method: "mams",
        selection_rule: "rank_best" },
      { ...platformContractBase, ncc_method: "regression", ncc_weight_decay: 0.9 },
      { ...platformContractBase, rar_burn_in: 10 },
    ]) addContractRequest("master_simulate", { config });
    for (const config of [
      { ...basketContractBase, alt_params: [0.4] },
      { ...basketContractBase, null_params: [0.2] },
      { ...umbrellaContractBase, n_arms: 3 },
      { ...umbrellaContractBase, endpoint_type: "continuous",
        null_params: 0, alt_params: [0.5, 0.5] },
      { ...umbrellaContractBase, endpoint_type: "tte",
        null_params: 1, alt_params: [0.8, 0.8] },
      { ...umbrellaContractBase, endpoint_type: "incidence_rate",
        null_params: 1, alt_params: [0.8, 0.8] },
      { ...basketContractBase, alt_params: [0.1, 0.4] },
      { ...umbrellaContractBase, endpoint_type: "continuous", sd: 1,
        null_params: 0, alt_params: [-0.1, 0.5] },
      { ...umbrellaContractBase, endpoint_type: "tte", accrual_time: 1,
        followup_time: 1, null_params: 1, alt_params: [1.2, 0.8] },
      { ...umbrellaContractBase, endpoint_type: "incidence_rate",
        exposure_time: 1, null_params: 1, alt_params: [1.2, 0.8] },
      { ...basketContractBase, alt_params: [1.2, 0.4] },
      { ...umbrellaContractBase, endpoint_type: "continuous", sd: 1,
        exposure_time: 1, null_params: 0, alt_params: [0.5, 0.5] },
    ]) addContractRequest("master_simulate", { config });
    const binarySingleEndpoint = {
      endpoint_type: "binary", study_type: "poc", design: "single_arm",
      null_param: 0.2, alt_param: 0.4,
    };
    for (const [endpointConfig, prior] of [
      [binarySingleEndpoint, { shape: 1, rate: 1 }],
      [{ endpoint_type: "continuous", study_type: "poc", design: "single_arm",
         null_param: 0, alt_param: 1, sd: 1 }, { a: 1, b: 1 }],
      [{ endpoint_type: "tte", study_type: "poc", design: "single_arm",
         null_param: 1, alt_param: 0.8, accrual_time: 1, followup_time: 1 },
       { a: 1, b: 1 }],
      [{ endpoint_type: "incidence_rate", study_type: "poc",
         design: "single_arm", null_param: 1, alt_param: 0.8,
         exposure_time: 1 }, { a: 1, b: 1 }],
    ]) addContractRequest("validate_config", { ...endpointConfig, prior });
    const pocPposValues = {
      p2_data: { x: 2, n: 10 }, p2_data_ctrl: { x: 2, n: 10 },
      p3_n: 100, p3_alloc_ratio: 1, p3_alpha: 0.025,
    };
    for (const [field, value] of Object.entries(pocPposValues)) {
      addContractRequest("simulate_design", {
        config: { ...binarySingleEndpoint, [field]: value },
      });
    }
    addContractRequest("simulate_design", { config: {
      ...binarySingleEndpoint, study_type: "confirmatory",
      p2_data: { x: 11, n: 10 },
    } });
    const oneUsableMetaStudy = {
      binary_single: { responders: 2, total: 10 },
      binary_comparative: { events_t: 2, total_t: 10, events_c: 1, total_c: 10 },
      continuous_single: { mean: 1, sd: 1, n: 10 },
      continuous_comparative: {
        mean_t: 1, sd_t: 1, n_t: 10, mean_c: 0, sd_c: 1, n_c: 10,
      },
      time_to_event: { hr: 0.8, se: 0.1 },
      incidence_single: { events: 2, person_time: 10 },
      incidence_comparative: {
        events_t: 2, person_time_t: 10, events_c: 1, person_time_c: 10,
      },
    };
    for (const [endpoint_type, study] of Object.entries(oneUsableMetaStudy)) {
      addContractRequest("meta_analyze", {
        endpoint_type, studies: [study, {}],
      });
    }
    addContractRequest("meta_analyze", {
      endpoint_type: "continuous_comparative",
      studies: [
        { ...oneUsableMetaStudy.continuous_comparative,
          measure: "mean_difference" },
        { ...oneUsableMetaStudy.continuous_comparative,
          measure: "standardized_mean_difference" },
      ],
    });
    for (const arguments_ of [
      { baseline: 0.9, effect: 0.2, metric: "proportion" },
      { baseline: 0.6, effect: 1, effect_type: "relative", metric: "proportion" },
      { baseline: 0.2, effect: 0, metric: "proportion" },
      { baseline: 10, effect: 0, metric: "mean", sd: 1 },
      { baseline: 0, effect: 0.1, effect_type: "relative", metric: "mean", sd: 1 },
    ]) addContractRequest("ab_test", arguments_);
    for (const [id, name, arguments_] of contractRequests) {
      child.stdin.write(JSON.stringify({
        jsonrpc: "2.0", id, method: "tools/call",
        params: { name, arguments: arguments_ },
      }) + "\n");
    }
    const expectedIds = Array.from(
      { length: nextContractId - 2 },
      (_value, index) => index + 2,
    );
    const deadline = Date.now() + 10_000;
    while (Date.now() < deadline &&
           !expectedIds.every((id) => responses.has(id))) {
      await pause(20);
    }
    child.kill("SIGTERM");
    await childClosed;
    assert.ok(expectedIds.every((id) => responses.has(id)), stderr);
    const expectedPublicError = JSON.stringify({
      error: {
        code: "invalid_request",
        message: PUBLIC_TOOL_ERROR_MESSAGES.invalid_request,
      },
    });
    for (const id of expectedIds) {
      const message = responses.get(id);
      assert.equal(message.result?.isError, true, JSON.stringify(message));
      assert.equal(message.result?.content?.length, 1, JSON.stringify(message));
      assert.equal(message.result?.content?.[0]?.type, "text", JSON.stringify(message));
      assert.equal(message.result?.content?.[0]?.text, expectedPublicError);
      assert.equal(JSON.stringify(message).includes("do-not-reflect"), false);
    }
    assert.equal(existsSync(rMarker), false, "an invalid request reached the R handler");
    console.log("TEST raw_mcp_schema_taxonomy_and_cross_field_guards_fail_before_r : PASS");
  } else {
    console.log("TEST raw_mcp_schema_taxonomy_and_cross_field_guards_fail_before_r : SKIP");
  }

  const dispatcherPath = join(process.cwd(), "r-wrapper", "dispatcher.R");
  const rsmOrderProbe = [
    `parsed <- parse(file=${JSON.stringify(dispatcherPath)})`,
    "assignment <- NULL",
    "for (expr in parsed) {",
    "  if (is.call(expr) && identical(expr[[1L]], as.name('<-')) &&",
    "      identical(as.character(expr[[2L]]), 'bind_rsm_run_order')) assignment <- expr",
    "}",
    "stopifnot(!is.null(assignment))",
    "scope <- new.env(parent=baseenv())",
    "eval(assignment, envir=scope)",
    "lexical <- scope$bind_rsm_run_order(list(design=data.frame(",
    "  B=c(-1,1,-1,1), A=c(-1,-1,1,1), point_type=rep('factorial',4)",
    ")), FALSE, 42L)$design",
    "stopifnot(identical(as.numeric(lexical$A), c(-1,-1,1,1)))",
    "stopifnot(identical(as.numeric(lexical$B), c(-1,1,-1,1)))",
    "stopifnot(identical(lexical$std_order, 1:4), identical(lexical$run, 1:4))",
    "numeric_alias <- scope$bind_rsm_run_order(list(design=data.frame(",
    "  factor_10=c(-1,-1,1,1), factor_2=c(-1,1,-1,1),",
    "  point_type=rep('factorial',4)",
    ")), FALSE, 42L)$design",
    "stopifnot(identical(as.numeric(numeric_alias$factor_2), c(-1,-1,1,1)))",
    "stopifnot(identical(as.numeric(numeric_alias$factor_10), c(-1,1,-1,1)))",
    "stopifnot(identical(numeric_alias$std_order, 1:4), identical(numeric_alias$run, 1:4))",
  ].join("\n");
  const rsmOrderResult = spawnSync(
    RSCRIPT_EXECUTABLE,
    ["--vanilla", "-e", rsmOrderProbe],
    { encoding: "utf8", env: sanitizedRChildEnvironment(), timeout: 10_000 },
  );
  assert.equal(rsmOrderResult.status, 0, rsmOrderResult.stderr);
  console.log("TEST rsm_standard_order_uses_public_canonical_factor_order : PASS");

  const safeRResult = join(workspace, "safe-r-result.json");
  writeFileSync(safeRResult, '{"ok":true}', { mode: 0o600 });
  assert.equal(await readRResultFile(safeRResult), '{"ok":true}');
  const oversizedRResult = join(workspace, "oversized-r-result.json");
  writeFileSync(oversizedRResult, "x", { mode: 0o600 });
  truncateSync(oversizedRResult, 5 * 1024 * 1024 + 1);
  await assert.rejects(readRResultFile(oversizedRResult), /exceeded/);
  if (process.platform !== "win32") {
    const linkedRResult = join(workspace, "linked-r-result.json");
    symlinkSync(safeRResult, linkedRResult);
    await assert.rejects(readRResultFile(linkedRResult));
    const fifoRResult = join(workspace, "fifo-r-result.json");
    assert.equal(spawnSync("mkfifo", [fifoRResult]).status, 0);
    await assert.rejects(readRResultFile(fifoRResult), /not a regular file/);
  }
  console.log("TEST r_result_reads_are_nofollow_regular_and_bounded : PASS");

  const forgedRegression = publicRegressionStatus({
    all_ok: true,
    skills: {
      "vera-experiment-designing": {
        ok: true, output: "TEST only : PASS", passes: 1, fails: 0, exit_status: 0,
      },
      "vera-master-experiment-designing": {
        ok: true, output: "TEST only : PASS", passes: 1, fails: 0, exit_status: 0,
      },
      "vera-indirect-comparing": {
        ok: true, output: "TEST only : PASS", passes: 1, fails: 0, exit_status: 0,
      },
      "vera-meta-analyzing": {
        ok: true, output: "TEST only : PASS", passes: 1, fails: 0, exit_status: 0,
      },
      "vera-doe-designing": {
        ok: true, output: "TEST only : PASS", passes: 1, fails: 0, exit_status: 0,
      },
    },
  });
  assert.equal(forgedRegression.all_ok, false);
  assert.equal(forgedRegression.checks.expected_check_count, false);
  assert.equal(forgedRegression.passed_suite_count, 0);
  console.log("TEST regression_attestation_rejects_reduced_check_counts : PASS");

  const suiteRecord = (passes) => ({
    ok: true,
    output: Array.from({ length: passes }, (_, index) =>
      `TEST ${index === 0 ? "PASS_in_test_name" : `case_${index}`} : PASS`).join("\n"),
    passes,
    fails: 0,
    exit_status: 0,
  });
  const exactRegression = publicRegressionStatus({
    all_ok: true,
    skills: {
      "vera-experiment-designing": suiteRecord(31),
      "vera-master-experiment-designing": suiteRecord(53),
      "vera-indirect-comparing": suiteRecord(15),
      "vera-meta-analyzing": suiteRecord(14),
      "vera-doe-designing": suiteRecord(14),
    },
  });
  assert.equal(exactRegression.all_ok, true);
  assert.equal(exactRegression.checks.expected_check_count, true);
  console.log("TEST regression_attestation_uses_exact_result_lines : PASS");

  assert.notEqual(
    hashFramedFields(["ab", "c"]),
    hashFramedFields(["a", "bc"]),
  );
  console.log("TEST fingerprint_fields_are_length_framed : PASS");

  const outputLabel = join(workspace, "managed-output");
  mkdirSync(outputLabel, { mode: 0o700 });
  const output = realpathSync(outputLabel);
  const artifact = join(output, "assignment.csv");
  const verified = Buffer.from("unit,arm\n1,A\n");
  writeFileSync(artifact, verified, { mode: 0o600 });
  const originalInode = statSync(artifact).ino;
  const expected = digest(verified);
  const published = publishVerifiedArtifacts(
    [artifact], output, { "assignment.csv": expected },
  );
  const snapshot = statSync(artifact);
  assert.deepEqual(published, [artifact]);
  assert.equal(readFileSync(artifact).equals(verified), true);
  assert.notEqual(snapshot.ino, originalInode);
  assert.equal(snapshot.mode & 0o777, 0o400);
  console.log("TEST artifact_publication_atomically_replaces_with_verified_snapshot : PASS");

  chmodSync(artifact, 0o600);
  writeFileSync(artifact, "mutated-before-publication");
  assert.throws(() => publishVerifiedArtifacts(
    [artifact], output, { "assignment.csv": expected },
  ), /changed after provenance/);
  console.log("TEST artifact_mutation_before_publication_fails_closed : PASS");

  const target = join(output, "target.csv");
  const linked = join(output, "linked.csv");
  writeFileSync(target, verified, { mode: 0o600 });
  symlinkSync(target, linked);
  assert.throws(() => publishVerifiedArtifacts(
    [linked], output, { "linked.csv": expected },
  ));
  assert.equal(lstatSync(linked).isSymbolicLink(), true);
  console.log("TEST artifact_symlink_replacement_fails_closed : PASS");

  const oversized = join(output, "oversized.csv");
  writeFileSync(oversized, "x", { mode: 0o600 });
  truncateSync(oversized, MAX_PUBLISHED_ARTIFACT_BYTES + 1);
  assertRejectsBeforeRead(
    () => publishVerifiedArtifacts(
      [oversized], output, { "oversized.csv": digest(Buffer.from("x")) },
    ),
    /byte limit/,
  );
  console.log("TEST oversized_artifact_publication_fails_before_read : PASS");

  if (process.platform !== "win32") {
    const fifo = join(output, "blocked.csv");
    const mkfifo = spawnSync("mkfifo", [fifo], { encoding: "utf8" });
    assert.equal(mkfifo.status, 0, mkfifo.stderr || mkfifo.error?.message);
    const fifoProbe = spawnSync(process.execPath, [
      "--input-type=module", "-e", `
        import {publishVerifiedArtifacts} from "./dist/artifact-publication.js";
        try {
          publishVerifiedArtifacts(
            [${JSON.stringify(fifo)}],
            ${JSON.stringify(output)},
            {"blocked.csv": ${JSON.stringify(digest(Buffer.alloc(0)))}}
          );
          console.error("FIFO publication unexpectedly succeeded");
          process.exitCode = 2;
        } catch (error) {
          if (!/not a regular file/.test(String(error?.message))) {
            console.error(error);
            process.exitCode = 3;
          }
        }
      `,
    ], { cwd: process.cwd(), encoding: "utf8", timeout: 3_000 });
    assert.equal(
      fifoProbe.error,
      undefined,
      `FIFO probe exceeded its timeout: ${fifoProbe.error?.message ?? "unknown error"}`,
    );
    assert.equal(fifoProbe.status, 0, fifoProbe.stderr);
    console.log("TEST fifo_artifact_publication_fails_without_blocking : PASS");
  } else {
    console.log("TEST fifo_artifact_publication_fails_without_blocking : SKIP");
  }

  const provenanceOutput = join(workspace, "provenance-output");
  mkdirSync(provenanceOutput, { mode: 0o700 });
  const oversizedProvenanceArtifact = join(provenanceOutput, "oversized.csv");
  writeFileSync(oversizedProvenanceArtifact, "x", { mode: 0o600 });
  truncateSync(oversizedProvenanceArtifact, MAX_PUBLISHED_ARTIFACT_BYTES + 1);
  assertRejectsBeforeRead(
    () => hashArtifactsForProvenance(provenanceOutput),
    /byte limit/,
  );
  console.log("TEST provenance_hashing_rejects_oversized_artifacts_before_read : PASS");

  if (process.platform !== "win32") {
    const raceOutput = join(workspace, "provenance-race-output");
    mkdirSync(raceOutput, { mode: 0o700 });
    const racedArtifact = join(raceOutput, "raced.csv");
    writeFileSync(racedArtifact, verified, { mode: 0o600 });
    assert.equal(statSync(racedArtifact).isFile(), true);
    unlinkSync(racedArtifact);
    const mkfifo = spawnSync("mkfifo", [racedArtifact], { encoding: "utf8" });
    assert.equal(mkfifo.status, 0, mkfifo.stderr || mkfifo.error?.message);
    const provenanceFifoProbe = spawnSync(process.execPath, [
      "--input-type=module", "-e", `
        import {hashArtifactForProvenance} from "./dist/artifact-publication.js";
        try {
          hashArtifactForProvenance(${JSON.stringify(racedArtifact)});
          console.error("replacement FIFO was unexpectedly hashed");
          process.exitCode = 2;
        } catch (error) {
          if (!/not a regular file/.test(String(error?.message))) {
            console.error(error);
            process.exitCode = 3;
          }
        }
      `,
    ], { cwd: process.cwd(), encoding: "utf8", timeout: 3_000 });
    assert.equal(
      provenanceFifoProbe.error,
      undefined,
      `provenance FIFO probe exceeded its timeout: ${
        provenanceFifoProbe.error?.message ?? "unknown error"
      }`,
    );
    assert.equal(provenanceFifoProbe.status, 0, provenanceFifoProbe.stderr);
    console.log("TEST provenance_hashing_rejects_regular_file_replaced_by_fifo : PASS");
  } else {
    console.log("TEST provenance_hashing_rejects_regular_file_replaced_by_fifo : SKIP");
  }

  const pidFile = join(workspace, "blocked-r.pid");
  const grandchildPidFile = join(workspace, "blocked-grandchild.pid");
  const blocker = join(workspace, "blocker.sh");
  writeFileSync(
    blocker,
    `#!/bin/sh\necho $$ > ${JSON.stringify(grandchildPidFile)}\nexec sleep 60\n`,
    { mode: 0o700 },
  );
  const controller = new AbortController();
  const blocked = runRscript([
    "--vanilla", "-e",
    `writeLines(as.character(Sys.getpid()), ${JSON.stringify(pidFile)}); ` +
      `system2(${JSON.stringify(blocker)}, wait=FALSE); Sys.sleep(60)`,
  ], controller.signal);
  for (let attempt = 0;
    attempt < 100 && (!existsSync(pidFile) || !existsSync(grandchildPidFile));
    attempt += 1) {
    await pause(25);
  }
  assert.equal(existsSync(pidFile), true);
  assert.equal(existsSync(grandchildPidFile), true);
  const blockedPid = Number(readFileSync(pidFile, "utf8").trim());
  const grandchildPid = Number(readFileSync(grandchildPidFile, "utf8").trim());
  const cancelledAt = Date.now();
  controller.abort();
  const cancelled = await blocked;
  assert.equal(cancelled.aborted, true);
  assert.ok(Date.now() - cancelledAt < 10_000);
  await pause(50);
  const alive = (pid) => {
    try { process.kill(pid, 0); return true; } catch { return false; }
  };
  assert.equal(alive(blockedPid), false);
  assert.equal(alive(grandchildPid), false);
  assert.equal(alive(-blockedPid), false);
  console.log("TEST abort_signal_terminates_blocked_r_process_group : PASS");

  if (process.platform !== "win32") {
    for (const trigger of ["SIGINT", "SIGTERM", "stdio-close"]) {
      const serverPidFile = join(workspace, `${trigger}-server-r.pid`);
      const serverGrandchildPidFile = join(workspace, `${trigger}-server-grandchild.pid`);
      const serverBlocker = join(workspace, `${trigger}-server-blocker.sh`);
      const serverVerifierPidFile = join(workspace, `${trigger}-server-verifier.pid`);
      const serverVerifierGrandchildPidFile = join(
        workspace, `${trigger}-server-verifier-grandchild.pid`,
      );
      const serverVerifier = join(workspace, `${trigger}-server-verifier.py`);
      const runtimeProbePidFile = join(workspace, `${trigger}-runtime-probe.pid`);
      const runtimeProbeGrandchildPidFile = join(
        workspace, `${trigger}-runtime-probe-grandchild.pid`,
      );
      const runtimeProbeBlocker = join(workspace, `${trigger}-runtime-probe-child.sh`);
      const runtimeProbeExecutable = join(workspace, `${trigger}-python-probe.sh`);
      writeFileSync(
        serverBlocker,
        `#!/bin/sh\necho $$ > ${JSON.stringify(serverGrandchildPidFile)}\nexec sleep 60\n`,
        { mode: 0o700 },
      );
      writeFileSync(
        serverVerifier,
        "import os, subprocess, time\n" +
          `open(${JSON.stringify(serverVerifierPidFile)}, 'w').write(str(os.getpid()))\n` +
          "child = subprocess.Popen([\"/bin/sh\", \"-c\", " +
          JSON.stringify(
            `echo $$ > ${serverVerifierGrandchildPidFile}; exec sleep 60`,
          ) + "])\n" +
          "while True: time.sleep(60)\n",
        { mode: 0o600 },
      );
      writeFileSync(
        runtimeProbeBlocker,
        `#!/bin/sh\necho $$ > ${JSON.stringify(runtimeProbeGrandchildPidFile)}\n` +
          "exec /bin/sleep 60\n",
        { mode: 0o700 },
      );
      writeFileSync(
        runtimeProbeExecutable,
        "#!/bin/sh\n" +
          "if [ \"$4\" = \"-c\" ]; then\n" +
          `  echo $$ > ${JSON.stringify(runtimeProbePidFile)}\n` +
          `  ${JSON.stringify(runtimeProbeBlocker)} &\n` +
          "  exec /bin/sleep 60\n" +
          "fi\n" +
          `exec ${JSON.stringify(PYTHON_EXECUTABLE)} \"$@\"\n`,
        { mode: 0o700 },
      );
      const serverChild = spawn(process.execPath, [
        "tests/server-shutdown-helper.mjs",
        serverPidFile,
        serverGrandchildPidFile,
        serverBlocker,
        serverVerifierPidFile,
        serverVerifierGrandchildPidFile,
        serverVerifier,
      ], {
        cwd: process.cwd(),
        stdio: ["pipe", "pipe", "pipe"],
        env: { ...process.env, EXPDESIGN_PYTHON: runtimeProbeExecutable },
      });
      let serverStderr = "";
      serverChild.stderr.on("data", (chunk) => { serverStderr += chunk.toString(); });
      const serverClosed = new Promise((resolve) => serverChild.once(
        "close", (code, signal) => resolve({ code, signal }),
      ));
      await waitForFiles(
        serverPidFile,
        serverGrandchildPidFile,
        serverVerifierPidFile,
        serverVerifierGrandchildPidFile,
        runtimeProbePidFile,
        runtimeProbeGrandchildPidFile,
      );
      if (trigger === "stdio-close") serverChild.stdin.end();
      else serverChild.kill(trigger);
      const closed = await Promise.race([
        serverClosed,
        pause(5_000).then(() => null),
      ]);
      if (closed === null) serverChild.kill("SIGKILL");
      assert.notEqual(closed, null, `${trigger} shutdown timed out: ${serverStderr}`);
      const expectedCode = trigger === "SIGINT" ? 130 : trigger === "SIGTERM" ? 143 : 0;
      assert.equal(closed.code, expectedCode, serverStderr);
      const serverR = Number(readFileSync(serverPidFile, "utf8").trim());
      const serverGrandchild = Number(readFileSync(serverGrandchildPidFile, "utf8").trim());
      const serverVerifierPid = Number(readFileSync(serverVerifierPidFile, "utf8").trim());
      const serverVerifierGrandchild = Number(
        readFileSync(serverVerifierGrandchildPidFile, "utf8").trim(),
      );
      const runtimeProbePid = Number(readFileSync(runtimeProbePidFile, "utf8").trim());
      const runtimeProbeGrandchildPid = Number(
        readFileSync(runtimeProbeGrandchildPidFile, "utf8").trim(),
      );
      await waitForPidsToExit(
        serverR,
        serverGrandchild,
        serverVerifierPid,
        serverVerifierGrandchild,
        runtimeProbePid,
        runtimeProbeGrandchildPid,
      );
    }
    console.log("TEST server_shutdown_leaves_no_runtime_descendants : PASS");
  } else {
    console.log("TEST server_shutdown_leaves_no_runtime_descendants : SKIP");
  }

  if (process.platform !== "win32") {
    for (const runtime of ["r", "python"]) {
      const probePidFile = join(workspace, `${runtime}-cancelled-probe.pid`);
      const probeGrandchildPidFile = join(
        workspace, `${runtime}-cancelled-probe-grandchild.pid`,
      );
      const probeBlocker = join(workspace, `${runtime}-cancelled-probe-child.sh`);
      const probeExecutable = join(workspace, `${runtime}-cancelled-probe.sh`);
      writeFileSync(
        probeBlocker,
        `#!/bin/sh\necho $$ > ${JSON.stringify(probeGrandchildPidFile)}\n` +
          "exec /bin/sleep 60\n",
        { mode: 0o700 },
      );
      writeFileSync(
        probeExecutable,
        `#!/bin/sh\necho $$ > ${JSON.stringify(probePidFile)}\n` +
          `${JSON.stringify(probeBlocker)} &\nexec /bin/sleep 60\n`,
        { mode: 0o700 },
      );
      const override = runtime === "r" ? "EXPDESIGN_RSCRIPT" : "EXPDESIGN_PYTHON";
      const previousOverride = process.env[override];
      process.env[override] = probeExecutable;
      let isolatedIntegrity;
      try {
        isolatedIntegrity = await import(
          `../dist/integrity.js?runtime-probe-cancellation=${runtime}-${Date.now()}`
        );
      } finally {
        if (previousOverride === undefined) delete process.env[override];
        else process.env[override] = previousOverride;
      }
      const probeController = new AbortController();
      const probePromise = runtime === "r"
        ? isolatedIntegrity.currentRRuntimeSnapshot(probeController.signal)
        : isolatedIntegrity.currentPythonRuntimeSnapshot(probeController.signal);
      await waitForFiles(probePidFile, probeGrandchildPidFile);
      const probePid = Number(readFileSync(probePidFile, "utf8").trim());
      const probeGrandchildPid = Number(
        readFileSync(probeGrandchildPidFile, "utf8").trim(),
      );
      probeController.abort();
      await assert.rejects(probePromise, (error) => error?.name === "AbortError");
      await waitForPidsToExit(probePid, probeGrandchildPid);
      for (let attempt = 0;
        attempt < 100 && isolatedIntegrity.activeRuntimeProbeProcessGroupCount() !== 0;
        attempt += 1) {
        await pause(10);
      }
      assert.equal(isolatedIntegrity.activeRuntimeProbeProcessGroupCount(), 0);
    }
    console.log("TEST abort_signal_terminates_r_and_python_runtime_probe_groups : PASS");
  } else {
    console.log("TEST abort_signal_terminates_r_and_python_runtime_probe_groups : SKIP");
  }

  if (process.platform !== "win32") {
    const verifierPidFile = join(workspace, "blocked-verifier.pid");
    const verifierChildPidFile = join(workspace, "blocked-verifier-child.pid");
    const blockedVerifier = join(workspace, "blocked-verifier.py");
    writeFileSync(
      blockedVerifier,
      "import os, subprocess, time\n" +
        `open(${JSON.stringify(verifierPidFile)}, 'w').write(str(os.getpid()))\n` +
        "child = subprocess.Popen([\"/bin/sh\", \"-c\", " +
        JSON.stringify(`echo $$ > ${verifierChildPidFile}; exec sleep 60`) + "])\n" +
        "while True: time.sleep(60)\n",
      { mode: 0o600 },
    );
    const verifierController = new AbortController();
    const blockedVerification = runVerifierRequest(
      "{}", verifierController.signal, blockedVerifier,
    );
    await waitForFiles(verifierPidFile, verifierChildPidFile);
    const verifierPid = Number(readFileSync(verifierPidFile, "utf8").trim());
    const verifierChildPid = Number(readFileSync(verifierChildPidFile, "utf8").trim());
    const verifierCancelledAt = Date.now();
    verifierController.abort();
    await assert.rejects(blockedVerification, (error) => error?.name === "AbortError");
    assert.ok(Date.now() - verifierCancelledAt < 2_000);
    await waitForPidsToExit(verifierPid, verifierChildPid);
    console.log("TEST abort_signal_terminates_verifier_process_tree : PASS");
  } else {
    console.log("TEST abort_signal_terminates_verifier_process_tree : SKIP");
  }

  const darwinSupervisorProbe = process.platform === "darwin"
    ? spawnSync("/bin/ps", ["-o", "lstart=", "-p", String(process.pid)], {
        encoding: "utf8",
        env: { PATH: "/usr/bin:/bin", LANG: "C", LC_ALL: "C" },
      })
    : undefined;
  const runtimeSupervisorTestable = process.platform === "linux" ||
    (process.platform === "darwin" && darwinSupervisorProbe?.status === 0);
  if (runtimeSupervisorTestable) {
    const registryRoot = join(workspace, "runtime-group-registry");
    mkdirSync(registryRoot, { mode: 0o700 });
    chmodSync(registryRoot, 0o700);
    const registryIdentity = statSync(registryRoot, { bigint: true });
    const registryToken = "a".repeat(64);
    const registryConfiguration = {
      EXPDESIGN_RUNTIME_REGISTRY_DIR: registryRoot,
      EXPDESIGN_RUNTIME_REGISTRY_TOKEN: registryToken,
      EXPDESIGN_RUNTIME_REGISTRY_DEV: String(registryIdentity.dev),
      EXPDESIGN_RUNTIME_REGISTRY_INO: String(registryIdentity.ino),
    };
    const supervisorEnvironment = {
      ...sanitizedRChildEnvironment(),
      ...registryConfiguration,
    };
    const waitForRegistryEntry = async () => {
      for (let attempt = 0; attempt < 200; attempt += 1) {
        const entries = fs.readdirSync(registryRoot)
          .filter((name) => /^runtime-[1-9][0-9]*-[0-9a-f]{32}\.json$/.test(name));
        if (entries.length === 1) return entries[0];
        await pause(10);
      }
      throw new Error("timed out waiting for an authenticated runtime registry entry");
    };

    const wrapperOnlyHostEnvironment = {
      ...registryConfiguration,
      OPENAI_API_KEY: "wrapper-host-secret",
      HTTPS_PROXY: "http://wrapper-hostile.invalid",
      EXPDESIGN_UNTRUSTED_FEATURE: "enabled",
      R_TESTS: join(workspace, "wrapper-hostile-r-tests.R"),
      OMP_NUM_THREADS: "999",
    };
    const previousWrapperHostEnvironment = new Map(
      Object.keys(wrapperOnlyHostEnvironment).map((name) => [name, process.env[name]]),
    );
    Object.assign(process.env, wrapperOnlyHostEnvironment);
    let supervised;
    try {
      supervised = spawnRuntimeProcess("/bin/sh", ["-c", [
        "if [ \"${EXPDESIGN_RUNTIME_REGISTRY_TOKEN+x}\" = x ] ||",
        "   [ \"${EXPDESIGN_RUNTIME_REGISTRY_DIR+x}\" = x ] ||",
        "   [ \"${EXPDESIGN_RUNTIME_REGISTRY_DEV+x}\" = x ] ||",
        "   [ \"${EXPDESIGN_RUNTIME_REGISTRY_INO+x}\" = x ] ||",
        "   [ \"${EXPDESIGN_RUNTIME_TARGET_CWD+x}\" = x ]; then exit 91; fi",
        "if [ \"${OPENAI_API_KEY+x}\" = x ] ||",
        "   [ \"${HTTPS_PROXY+x}\" = x ] ||",
        "   [ \"${EXPDESIGN_UNTRUSTED_FEATURE+x}\" = x ] ||",
        "   [ \"${R_TESTS+x}\" = x ]; then exit 92; fi",
        "if [ \"$OMP_NUM_THREADS\" != 1 ] || [ \"$LANG\" != C ] ||",
        "   [ \"$LC_ALL\" != C ] || [ \"$TZ\" != UTC ]; then exit 93; fi",
        "exec /bin/sleep 0.2",
      ].join("\n")], {
        stdio: ["ignore", "ignore", "pipe"],
        detached: true,
        env: sanitizedRChildEnvironment(),
      });
    } finally {
      for (const [name, value] of previousWrapperHostEnvironment) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
    let supervisedStderr = "";
    supervised.stderr.on("data", (chunk) => { supervisedStderr += chunk.toString(); });
    let supervisedClosed = new Promise((resolve) => supervised.once(
      "close", (code, signal) => resolve({ code, signal }),
    ));
    const naturalEntryName = await waitForRegistryEntry();
    const naturalEntryPath = join(registryRoot, naturalEntryName);
    const naturalEntryRaw = readFileSync(naturalEntryPath, "utf8");
    const naturalEntry = JSON.parse(naturalEntryRaw);
    assert.deepEqual(Object.keys(naturalEntry).sort(), [
      "auth", "nonce", "pid", "server_pid", "start_identity", "version",
    ]);
    assert.equal(naturalEntry.version, 1);
    assert.equal(naturalEntry.server_pid, process.pid);
    assert.equal(naturalEntry.pid, supervised.pid);
    assert.match(naturalEntry.nonce, /^[0-9a-f]{32}$/);
    assert.match(naturalEntry.start_identity, /^(linux:[0-9]+|darwin:.+)$/);
    const expectedAuth = createHmac("sha256", registryToken).update(JSON.stringify([
      1, naturalEntry.server_pid, naturalEntry.pid,
      naturalEntry.nonce, naturalEntry.start_identity,
    ])).digest("hex");
    assert.equal(naturalEntry.auth, expectedAuth);
    assert.equal(naturalEntryRaw.includes(registryToken), false);
    assert.equal(statSync(naturalEntryPath).mode & 0o777, 0o600);
    const naturalClose = await supervisedClosed;
    assert.equal(naturalClose.code, 0, supervisedStderr);
    assert.equal(existsSync(naturalEntryPath), false);

    const backgroundPidFile = join(workspace, "supervised-background-child.pid");
    supervised = spawnRuntimeProcess("/bin/sh", ["-c", [
      "/bin/sleep 60 </dev/null >/dev/null 2>&1 &",
      `printf '%s\\n' "$!" > ${JSON.stringify(backgroundPidFile)}`,
      "exit 0",
    ].join("\n")], {
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
      env: supervisorEnvironment,
    });
    supervisedClosed = new Promise((resolve) => supervised.once(
      "close", (code, signal) => resolve({ code, signal }),
    ));
    const backgroundEntryPath = join(registryRoot, await waitForRegistryEntry());
    await waitForFiles(backgroundPidFile);
    const backgroundPid = Number(readFileSync(backgroundPidFile, "utf8").trim());
    const backgroundGroup = supervised.pid;
    const backgroundClose = await supervisedClosed;
    assert.equal(backgroundClose.signal, "SIGKILL");
    await waitForPidsToExit(backgroundPid);
    for (let attempt = 0; attempt < 200; attempt += 1) {
      try {
        process.kill(-backgroundGroup, 0);
      } catch {
        break;
      }
      await pause(10);
    }
    assert.throws(() => process.kill(-backgroundGroup, 0));
    assert.equal(
      existsSync(backgroundEntryPath), true,
      "descendant cleanup removed its lease before the host verified group death",
    );
    const backgroundEntry = JSON.parse(readFileSync(backgroundEntryPath, "utf8"));
    const backgroundAuth = createHmac("sha256", registryToken).update(JSON.stringify([
      1, backgroundEntry.server_pid, backgroundEntry.pid,
      backgroundEntry.nonce, backgroundEntry.start_identity,
    ])).digest("hex");
    assert.equal(backgroundEntry.auth, backgroundAuth);
    unlinkSync(backgroundEntryPath);

    const replacedRegistry = join(workspace, "runtime-registry-replaced-before-spawn");
    const movedRegistry = `${replacedRegistry}-original`;
    mkdirSync(replacedRegistry, { mode: 0o700 });
    chmodSync(replacedRegistry, 0o700);
    const replacedIdentity = statSync(replacedRegistry, { bigint: true });
    renameSync(replacedRegistry, movedRegistry);
    mkdirSync(replacedRegistry, { mode: 0o700 });
    chmodSync(replacedRegistry, 0o700);
    const replacedTargetMarker = join(workspace, "replaced-registry-target-ran");
    supervised = spawnRuntimeProcess("/usr/bin/touch", [replacedTargetMarker], {
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
      env: {
        ...process.env,
        EXPDESIGN_RUNTIME_REGISTRY_DIR: replacedRegistry,
        EXPDESIGN_RUNTIME_REGISTRY_TOKEN: registryToken,
        EXPDESIGN_RUNTIME_REGISTRY_DEV: String(replacedIdentity.dev),
        EXPDESIGN_RUNTIME_REGISTRY_INO: String(replacedIdentity.ino),
      },
    });
    const replacedClose = await new Promise((resolve) => supervised.once(
      "close", (code, signal) => resolve({ code, signal }),
    ));
    assert.notEqual(replacedClose.code, 0);
    assert.equal(existsSync(replacedTargetMarker), false);
    assert.deepEqual(fs.readdirSync(replacedRegistry), []);
    assert.deepEqual(fs.readdirSync(movedRegistry), []);

    const liveRegistry = join(workspace, "runtime-registry-renamed-after-spawn");
    const liveMovedRegistry = `${liveRegistry}-original`;
    mkdirSync(liveRegistry, { mode: 0o700 });
    chmodSync(liveRegistry, 0o700);
    const liveIdentity = statSync(liveRegistry, { bigint: true });
    const liveEnvironment = {
      ...process.env,
      EXPDESIGN_RUNTIME_REGISTRY_DIR: liveRegistry,
      EXPDESIGN_RUNTIME_REGISTRY_TOKEN: registryToken,
      EXPDESIGN_RUNTIME_REGISTRY_DEV: String(liveIdentity.dev),
      EXPDESIGN_RUNTIME_REGISTRY_INO: String(liveIdentity.ino),
    };
    const liveTargetPidFile = join(workspace, "renamed-registry-target.pid");
    supervised = spawnRuntimeProcess("/bin/sh", ["-c", [
      `printf '%s\\n' "$$" > ${JSON.stringify(liveTargetPidFile)}`,
      "exec /bin/sleep 30",
    ].join("\n")], {
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
      env: liveEnvironment,
    });
    let liveEntryName;
    for (let attempt = 0; attempt < 200 && liveEntryName === undefined; attempt += 1) {
      liveEntryName = fs.readdirSync(liveRegistry)
        .find((name) => /^runtime-[1-9][0-9]*-[0-9a-f]{32}\.json$/.test(name));
      if (liveEntryName === undefined) await pause(10);
    }
    assert.ok(liveEntryName);
    await waitForFiles(liveTargetPidFile);
    const liveTargetPid = Number(readFileSync(liveTargetPidFile, "utf8").trim());
    renameSync(liveRegistry, liveMovedRegistry);
    mkdirSync(liveRegistry, { mode: 0o700 });
    chmodSync(liveRegistry, 0o700);
    writeFileSync(join(liveMovedRegistry, ".stop"), "stop\n", { mode: 0o600 });
    const liveClose = await new Promise((resolve) => supervised.once(
      "close", (code, signal) => resolve({ code, signal }),
    ));
    assert.equal(liveClose.signal, "SIGKILL");
    await waitForPidsToExit(liveTargetPid);
    for (let attempt = 0; attempt < 200; attempt += 1) {
      try {
        process.kill(-supervised.pid, 0);
      } catch {
        break;
      }
      await pause(10);
    }
    assert.throws(() => process.kill(-supervised.pid, 0));
    assert.equal(existsSync(join(liveMovedRegistry, liveEntryName)), true);
    assert.deepEqual(fs.readdirSync(liveRegistry), []);
    unlinkSync(join(liveMovedRegistry, liveEntryName));
    unlinkSync(join(liveMovedRegistry, ".stop"));

    supervised = spawnRuntimeProcess("/bin/sleep", ["30"], {
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
      env: supervisorEnvironment,
    });
    supervisedClosed = new Promise((resolve) => supervised.once(
      "close", (code, signal) => resolve({ code, signal }),
    ));
    const forcedEntryPath = join(registryRoot, await waitForRegistryEntry());
    killRuntimeProcessTree(supervised, "SIGKILL");
    await supervisedClosed;
    assert.equal(
      existsSync(forcedEntryPath), true,
      "forced shutdown removed its lease before the host verified group death",
    );
    unlinkSync(forcedEntryPath);

    const stoppedTargetMarker = join(registryRoot, "stopped-target-ran");
    writeFileSync(join(registryRoot, ".stop"), "stop\n", { mode: 0o600 });
    supervised = spawnRuntimeProcess("/usr/bin/touch", [stoppedTargetMarker], {
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
      env: supervisorEnvironment,
    });
    supervisedClosed = new Promise((resolve) => supervised.once(
      "close", (code, signal) => resolve({ code, signal }),
    ));
    const stoppedClose = await supervisedClosed;
    assert.notEqual(stoppedClose.code, 0);
    assert.equal(existsSync(stoppedTargetMarker), false);
    console.log("TEST authenticated_runtime_supervisor_lifecycle_is_fail_closed : PASS");
  } else {
    console.log("TEST authenticated_runtime_supervisor_lifecycle_is_fail_closed : SKIP");
  }

  const subsequent = await runRscript(["--vanilla", "-e", "quit(status=0)"]);
  assert.equal(subsequent.code, 0);
  assert.equal(subsequent.aborted, false);
  console.log("TEST cancellation_does_not_poison_subsequent_r_process : PASS");

  let releaseShared;
  const sharedGate = new Promise((resolve) => { releaseShared = resolve; });
  const cache = new FingerprintPromiseCache();
  let sharedLoads = 0;
  const loader = async () => {
    sharedLoads += 1;
    await sharedGate;
    return "verified";
  };
  const sharedOne = cache.get("engine", loader);
  const sharedTwo = cache.get("engine", loader);
  const cancelledController = new AbortController();
  const liveController = new AbortController();
  const cancelledWait = waitForSharedPromise(sharedOne, cancelledController.signal)
    .then(() => "resolved", (error) => error?.name);
  const liveWait = waitForSharedPromise(sharedTwo, liveController.signal);
  cancelledController.abort();
  assert.equal(await cancelledWait, "AbortError");
  releaseShared();
  assert.equal(await liveWait, "verified");
  assert.equal(sharedLoads, 1);
  console.log("TEST caller_cancellation_does_not_cancel_shared_attestation : PASS");

  const profile = join(workspace, "hostile-profile.R");
  const profileMarker = join(workspace, "profile-loaded.txt");
  writeFileSync(
    profile,
    "marker <- Sys.getenv('EXPDESIGN_PROFILE_MARKER')\n" +
      "if (nzchar(marker)) writeLines('loaded', marker)\n",
    { mode: 0o600 },
  );
  const oldProfile = process.env.R_PROFILE_USER;
  const oldMarker = process.env.EXPDESIGN_PROFILE_MARKER;
  process.env.R_PROFILE_USER = profile;
  process.env.EXPDESIGN_PROFILE_MARKER = profileMarker;
  try {
    const regression = await callR("run_tests", {});
    assert.equal(regression?.all_ok, true);
    assert.equal(existsSync(profileMarker), false);
  } finally {
    if (oldProfile === undefined) delete process.env.R_PROFILE_USER;
    else process.env.R_PROFILE_USER = oldProfile;
    if (oldMarker === undefined) delete process.env.EXPDESIGN_PROFILE_MARKER;
    else process.env.EXPDESIGN_PROFILE_MARKER = oldMarker;
  }
  console.log("TEST regression_children_ignore_hostile_r_profile : PASS");

  const pythonProfileRoot = join(workspace, "hostile-python");
  const pythonProfileMarker = join(workspace, "sitecustomize-loaded.txt");
  mkdirSync(pythonProfileRoot, { mode: 0o700 });
  writeFileSync(
    join(pythonProfileRoot, "sitecustomize.py"),
    "import os\n" +
      "marker = os.environ.get('EXPDESIGN_PYTHON_MARKER')\n" +
      "open(marker, 'w').write('loaded') if marker else None\n",
    { mode: 0o600 },
  );
  const oldPythonPath = process.env.PYTHONPATH;
  const oldPythonMarker = process.env.EXPDESIGN_PYTHON_MARKER;
  const oldPyvenvLauncher = process.env.__PYVENV_LAUNCHER__;
  process.env.PYTHONPATH = pythonProfileRoot;
  process.env.EXPDESIGN_PYTHON_MARKER = pythonProfileMarker;
  process.env.__PYVENV_LAUNCHER__ = "/hostile/python-launcher";
  const hostileLoaderVariables = [
    "LD_AUDIT", "LD_DEBUG", "LD_LIBRARY_PATH", "LD_PRELOAD",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
  ];
  const oldLoaderValues = new Map(
    hostileLoaderVariables.map((name) => [name, process.env[name]]),
  );
  for (const name of hostileLoaderVariables) process.env[name] = "/hostile/runtime-loader";
  try {
    const pythonEnvironment = sanitizedPythonChildEnvironment();
    const rEnvironment = sanitizedRChildEnvironment();
    assert.equal(
      Object.keys(pythonEnvironment).some((name) => name.startsWith("PYTHON")), false,
    );
    assert.equal(pythonEnvironment.__PYVENV_LAUNCHER__, undefined);
    for (const name of hostileLoaderVariables) {
      assert.equal(pythonEnvironment[name], undefined);
      assert.equal(rEnvironment[name], undefined);
    }
    const snapshot = await currentPythonRuntimeSnapshot();
    assert.equal(typeof snapshot.fingerprint, "string");
    assert.equal(existsSync(pythonProfileMarker), false);
    const cleanVerifier = join(workspace, "clean-environment-verifier.py");
    writeFileSync(
      cleanVerifier,
      "import json\nprint(json.dumps({'status': 'VERIFIED', 'presentable': True}))\n",
      { mode: 0o600 },
    );
    const verifierResult = await runVerifierRequest("{}", undefined, cleanVerifier);
    assert.equal(verifierResult.presentable, true);
    assert.equal(existsSync(pythonProfileMarker), false);
  } finally {
    if (oldPythonPath === undefined) delete process.env.PYTHONPATH;
    else process.env.PYTHONPATH = oldPythonPath;
    if (oldPythonMarker === undefined) delete process.env.EXPDESIGN_PYTHON_MARKER;
    else process.env.EXPDESIGN_PYTHON_MARKER = oldPythonMarker;
    if (oldPyvenvLauncher === undefined) delete process.env.__PYVENV_LAUNCHER__;
    else process.env.__PYVENV_LAUNCHER__ = oldPyvenvLauncher;
    for (const [name, value] of oldLoaderValues) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  }
  console.log("TEST python_runtime_probe_and_verifier_ignore_hostile_python_environment : PASS");

  if (process.platform !== "win32") {
    const shimRoot = join(workspace, "runtime-shims");
    mkdirSync(shimRoot, { mode: 0o700 });
    const rShim = join(shimRoot, "Rscript");
    const pythonShim = join(shimRoot, "python3");
    writeFileSync(rShim, `#!/bin/sh\nexec ${JSON.stringify(RSCRIPT_EXECUTABLE)} "$@"\n`, {
      mode: 0o700,
    });
    writeFileSync(pythonShim, `#!/bin/sh\nexec ${JSON.stringify(PYTHON_EXECUTABLE)} "$@"\n`, {
      mode: 0o700,
    });
    const shimEnv = {
      ...process.env,
      PATH: `${shimRoot}${delimiter}${process.env.PATH ?? ""}`,
    };
    delete shimEnv.EXPDESIGN_RSCRIPT;
    delete shimEnv.EXPDESIGN_PYTHON;
    const shimProbe = spawnSync(process.execPath, [
      "--input-type=module", "-e", `
        import {realpathSync} from "node:fs";
        import {
          currentPythonRuntimeSnapshot, currentRRuntimeSnapshot,
          PYTHON_EXECUTABLE, RSCRIPT_EXECUTABLE,
        } from "./dist/integrity.js";
        const firstR = await currentRRuntimeSnapshot();
        const secondR = await currentRRuntimeSnapshot();
        const firstPython = await currentPythonRuntimeSnapshot();
        const secondPython = await currentPythonRuntimeSnapshot();
        console.log(JSON.stringify({
          rIgnored: RSCRIPT_EXECUTABLE !== realpathSync(${JSON.stringify(rShim)}),
          pythonIgnored: PYTHON_EXECUTABLE !== realpathSync(${JSON.stringify(pythonShim)}),
          rStable: firstR.fingerprint === secondR.fingerprint,
          pythonStable: firstPython.fingerprint === secondPython.fingerprint,
        }));
      `,
    ], { cwd: process.cwd(), env: shimEnv, encoding: "utf8" });
    assert.equal(shimProbe.status, 0, shimProbe.stderr);
    assert.deepEqual(JSON.parse(shimProbe.stdout), {
      rIgnored: true,
      pythonIgnored: true,
      rStable: true,
      pythonStable: true,
    });

    for (const [name, value] of [
      ["EXPDESIGN_RSCRIPT", "Rscript"],
      ["EXPDESIGN_PYTHON", "python3"],
    ]) {
      const relativeOverride = spawnSync(process.execPath, [
        "--input-type=module", "-e", "await import('./dist/integrity.js')",
      ], {
        cwd: process.cwd(),
        env: { ...process.env, [name]: value },
        encoding: "utf8",
      });
      assert.notEqual(relativeOverride.status, 0);
      assert.match(relativeOverride.stderr, /must be an absolute path/);
    }

    const preloadMarker = join(workspace, "hostile-node-preload-ran");
    const preload = join(workspace, "hostile-node-preload.cjs");
    writeFileSync(
      preload,
      `require('fs').writeFileSync(${JSON.stringify(preloadMarker)}, 'preloaded')\n`,
      { mode: 0o600 },
    );
    const launcherProbe = spawnSync(
      "/bin/sh", [join(process.cwd(), "launch-server.sh")], {
        cwd: process.cwd(),
        env: {
          ...process.env,
          NODE_OPTIONS: `--require=${preload}`,
          NODE_PATH: shimRoot,
        },
        input: "",
        encoding: "utf8",
        timeout: 10_000,
      },
    );
    assert.equal(launcherProbe.status, 0, launcherProbe.stderr);
    assert.equal(existsSync(preloadMarker), false);

    const hostileLibrary = join(workspace, "hostile-r-library");
    const hostilePackage = join(workspace, "hostile-mvtnorm");
    const hostilePackageR = join(hostilePackage, "R");
    mkdirSync(hostileLibrary, { recursive: true, mode: 0o700 });
    mkdirSync(hostilePackageR, { recursive: true, mode: 0o700 });
    writeFileSync(join(hostilePackage, "DESCRIPTION"), [
      "Package: mvtnorm",
      "Type: Package",
      "Title: Hostile lifecycle fixture",
      "Version: 99.0.0",
      "Authors@R: person('Test', 'Fixture', role=c('aut','cre'), email='test@example.invalid')",
      "Description: A test-only package that records unexpected loading.",
      "License: MIT",
      "Encoding: UTF-8",
      "LazyData: true",
      "",
    ].join("\n"));
    writeFileSync(join(hostilePackage, "NAMESPACE"), "exportPattern('^[[:alpha:]]+')\n");
    writeFileSync(join(hostilePackageR, "zzz.R"), [
      ".onLoad <- function(libname, pkgname) {",
      "  marker <- Sys.getenv('EXPDESIGN_R_LIBRARY_MARKER', unset='')",
      "  if (nzchar(marker)) writeLines('loaded', marker)",
      "}",
      "fixture_value <- 1",
      "",
    ].join("\n"));
    const rBinary = join(dirname(RSCRIPT_EXECUTABLE), "R");
    const installFixture = spawnSync(
      rBinary,
      ["CMD", "INSTALL", `--library=${hostileLibrary}`, "--no-byte-compile", hostilePackage],
      { encoding: "utf8", env: sanitizedRChildEnvironment() },
    );
    assert.equal(installFixture.status, 0, installFixture.stderr);
    const hostileRMarker = join(workspace, "hostile-r-library-loaded");
    const oldRLibsUser = process.env.R_LIBS_USER;
    const oldRMarker = process.env.EXPDESIGN_R_LIBRARY_MARKER;
    process.env.R_LIBS_USER = hostileLibrary;
    process.env.EXPDESIGN_R_LIBRARY_MARKER = hostileRMarker;
    try {
      const safeSnapshot = await currentRRuntimeSnapshot();
      assert.notEqual(safeSnapshot.packageVersions.mvtnorm, "99.0.0");
      assert.equal(existsSync(hostileRMarker), false);
    } finally {
      if (oldRLibsUser === undefined) delete process.env.R_LIBS_USER;
      else process.env.R_LIBS_USER = oldRLibsUser;
      if (oldRMarker === undefined) delete process.env.EXPDESIGN_R_LIBRARY_MARKER;
      else process.env.EXPDESIGN_R_LIBRARY_MARKER = oldRMarker;
    }
  }
  console.log("TEST ambient_runtime_shims_node_preloads_and_r_libraries_are_ignored : PASS");
  }
} finally {
  await rm(workspace, { recursive: true, force: true });
}
