import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { killRuntimeProcessTree, spawnRuntimeProcess } from "../dist/runtime-supervisor.js";

const pause = (milliseconds) => new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));
const alive = (pid) => {
  try { process.kill(pid, 0); return true; } catch { return false; }
};
const registryNames = [
  "EXPDESIGN_RUNTIME_REGISTRY_DIR", "EXPDESIGN_RUNTIME_REGISTRY_TOKEN",
  "EXPDESIGN_RUNTIME_REGISTRY_DEV", "EXPDESIGN_RUNTIME_REGISTRY_INO",
];

async function waitUntil(predicate, label) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (predicate()) return;
    await pause(25);
  }
  assert.fail(`timed out waiting for ${label}`);
}

async function completed(proc) {
  let stderr = "";
  let stdout = "";
  proc.stderr?.on("data", (chunk) => { stderr += chunk.toString(); });
  proc.stdout?.on("data", (chunk) => { stdout += chunk.toString(); });
  let timeout;
  try {
    return await Promise.race([
      new Promise((resolvePromise, reject) => {
        proc.once("error", reject);
        proc.once("close", (code, signal) => resolvePromise({ code, signal, stderr, stdout }));
      }),
      new Promise((_, reject) => {
        timeout = setTimeout(() => {
          killRuntimeProcessTree(proc, "SIGKILL");
          reject(new Error(`runtime did not close: ${stderr}`));
        }, 8_000);
      }),
    ]);
  } finally {
    clearTimeout(timeout);
  }
}

export async function testManagedLifecycles() {
  const workspace = await mkdtemp(join(tmpdir(), "expdesign-managed-lifecycle-"));
  const environmentNames = [
    "EXPDESIGN_RUNS_DIR", "EXPDESIGN_ARTIFACT_MAX_DIRS", ...registryNames,
  ];
  const previous = new Map(environmentNames.map((name) => [name, process.env[name]]));
  try {
    process.env.EXPDESIGN_RUNS_DIR = join(workspace, "artifacts");
    process.env.EXPDESIGN_ARTIFACT_MAX_DIRS = "1";
    for (const name of registryNames) delete process.env[name];
    const { createManagedArtifactDir, releaseManagedArtifactDir, abortManagedArtifactDir } =
      await import(`../dist/artifacts.js?managed-lifecycle=${Date.now()}`);

    const good = await createManagedArtifactDir("verified-");
    await writeFile(join(good, "result.json"), "verified-result");
    await releaseManagedArtifactDir(good);
    const pending = await createManagedArtifactDir("pending-");
    const failing = await createManagedArtifactDir("failing-");
    await abortManagedArtifactDir(failing);
    assert.equal(existsSync(failing), false);
    assert.equal(existsSync(pending), true);
    assert.equal(readFileSync(join(good, "result.json"), "utf8"), "verified-result");
    await abortManagedArtifactDir(pending);
    assert.equal(existsSync(good), true);
    console.log("TEST aborted_staging_runs_preserve_verified_artifact : PASS");

    process.env.EXPDESIGN_ARTIFACT_MAX_DIRS = "2";
    const nextGood = await createManagedArtifactDir("verified-next-");
    await releaseManagedArtifactDir(nextGood);
    const live = await createManagedArtifactDir("still-pending-");
    await pause(20);
    const latestGood = await createManagedArtifactDir("verified-latest-");
    await releaseManagedArtifactDir(latestGood);
    assert.equal(existsSync(good), false);
    assert.equal(existsSync(nextGood), true);
    assert.equal(existsSync(latestGood), true);
    assert.equal(existsSync(live), true);
    console.log("TEST staging_runs_do_not_consume_committed_retention_capacity : PASS");

    process.env.EXPDESIGN_ARTIFACT_MAX_DIRS = "1";
    await abortManagedArtifactDir(live);
    assert.equal(existsSync(nextGood), true);
    assert.equal(existsSync(latestGood), true);
    const replacement = await createManagedArtifactDir("verified-replacement-");
    await releaseManagedArtifactDir(replacement);
    assert.equal(existsSync(nextGood), false);
    assert.equal(existsSync(latestGood), false);
    assert.equal(existsSync(replacement), true);
    console.log("TEST retention_policy_changes_apply_on_successful_release_only : PASS");

    if (process.platform !== "linux" && process.platform !== "darwin") return;
    const environment = { PATH: "/usr/bin:/bin", LANG: "C", LC_ALL: "C" };
    const normal = spawnRuntimeProcess("/bin/sh", [
      "-c", "printf 'runtime-output'; printf 'runtime-diagnostic' >&2; exit 7",
    ], { cwd: workspace, stdio: ["ignore", "pipe", "pipe"], detached: true, env: environment });
    const normalOutcome = await completed(normal);
    assert.equal(normalOutcome.code, 7, normalOutcome.stderr);
    assert.equal(normalOutcome.signal, null);
    assert.equal(normalOutcome.stdout, "runtime-output");
    assert.equal(normalOutcome.stderr, "runtime-diagnostic");
    console.log("TEST standalone_supervisor_preserves_exit_status_and_streams : PASS");

    for (const descriptors of ["redirected", "inherited"]) {
      const pidFile = join(workspace, `standalone-${descriptors}.pid`);
      const script = [
        `/bin/sleep 60 ${descriptors === "redirected" ? "</dev/null >/dev/null 2>&1" : ""} &`,
        'printf "%s\\n" "$!" > "$1"',
        "exit 0",
      ].join("\n");
      // Use the production R bridge with a shell executable so these lifecycle
      // cases do not depend on a private statistical engine installation.
      const source = `
        import { runRscript, activeRProcessGroupCount, shutdownActiveRProcesses }
          from ${JSON.stringify(new URL("../dist/r-bridge.js", import.meta.url).href)};
        const outcome = await runRscript(${JSON.stringify(["-c", script, "lifecycle-probe", pidFile])});
        const activeBeforeShutdown = activeRProcessGroupCount();
        await shutdownActiveRProcesses();
        console.log(JSON.stringify({ outcome, activeBeforeShutdown, activeAfterShutdown: activeRProcessGroupCount() }));
      `;
      const proc = spawn(process.execPath, ["--input-type=module", "--eval", source], {
        cwd: workspace, stdio: ["ignore", "pipe", "pipe"],
        env: { ...environment, EXPDESIGN_RSCRIPT: "/bin/sh" },
      });
      try {
        const result = await completed(proc);
        assert.equal(result.code, 0, result.stderr);
        const payload = JSON.parse(result.stdout);
        assert.equal(payload.outcome.code, null);
        assert.equal(payload.outcome.signal, "SIGKILL");
        assert.equal(payload.outcome.timedOut, false);
        assert.equal(payload.activeBeforeShutdown, 0);
        assert.equal(payload.activeAfterShutdown, 0);
        const descendantPid = Number(readFileSync(pidFile, "utf8").trim());
        await waitUntil(() => !alive(descendantPid), `${descriptors} descendant cleanup`);
      } finally {
        if (proc.exitCode === null && proc.signalCode === null) proc.kill("SIGKILL");
        // A failed pre-fix regression must not leave its synthetic child alive.
        if (existsSync(pidFile)) {
          const descendantPid = Number(readFileSync(pidFile, "utf8").trim());
          if (Number.isSafeInteger(descendantPid) && descendantPid > 0 && alive(descendantPid)) {
            try { process.kill(descendantPid, "SIGKILL"); } catch { /* already exited */ }
          }
        }
      }
      console.log(`TEST standalone_r_leader_exit_reaps_${descriptors}_descendants : PASS`);
    }
  } finally {
    for (const [name, value] of previous) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    await rm(workspace, { recursive: true, force: true });
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  await testManagedLifecycles();
}
