import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs";
import {
  appendFileSync,
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  readFileSync,
  realpathSync,
  statSync,
  symlinkSync,
  truncateSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { syncBuiltinESMExports } from "node:module";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { delimiter, join } from "node:path";

import {
  hashArtifactsForProvenance,
  MAX_PUBLISHED_ARTIFACT_BYTES,
  publishVerifiedArtifacts,
} from "../dist/artifact-publication.js";
import {
  currentPythonRuntimeSnapshot,
  FingerprintPromiseCache,
  hashFramedFields,
  PYTHON_EXECUTABLE,
  RSCRIPT_EXECUTABLE,
  waitForSharedPromise,
} from "../dist/integrity.js";
import { callR, runRscript } from "../dist/r-bridge.js";
import { publicRegressionStatus } from "../dist/public-projection.js";


const digest = (bytes) => createHash("sha256").update(bytes).digest("hex");
const pause = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
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
const workspace = await mkdtemp(join(tmpdir(), "expdesign-lifecycle-test-"));

try {
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
      "vera-experiment-designing": suiteRecord(26),
      "vera-master-experiment-designing": suiteRecord(47),
      "vera-indirect-comparing": suiteRecord(15),
      "vera-meta-analyzing": suiteRecord(12),
      "vera-doe-designing": suiteRecord(13),
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
  process.env.PYTHONPATH = pythonProfileRoot;
  process.env.EXPDESIGN_PYTHON_MARKER = pythonProfileMarker;
  try {
    const snapshot = currentPythonRuntimeSnapshot();
    assert.equal(typeof snapshot.fingerprint, "string");
    assert.equal(existsSync(pythonProfileMarker), false);
  } finally {
    if (oldPythonPath === undefined) delete process.env.PYTHONPATH;
    else process.env.PYTHONPATH = oldPythonPath;
    if (oldPythonMarker === undefined) delete process.env.EXPDESIGN_PYTHON_MARKER;
    else process.env.EXPDESIGN_PYTHON_MARKER = oldPythonMarker;
  }
  console.log("TEST python_runtime_probe_ignores_hostile_sitecustomize : PASS");

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
        import {appendFileSync, realpathSync} from "node:fs";
        import {
          currentPythonRuntimeSnapshot, currentRRuntimeSnapshot,
          PYTHON_EXECUTABLE, RSCRIPT_EXECUTABLE,
        } from "./dist/integrity.js";
        const firstR = currentRRuntimeSnapshot();
        appendFileSync(RSCRIPT_EXECUTABLE, "\\n# fingerprint mutation\\n");
        const secondR = currentRRuntimeSnapshot();
        const firstPython = currentPythonRuntimeSnapshot();
        appendFileSync(PYTHON_EXECUTABLE, "\\n# fingerprint mutation\\n");
        const secondPython = currentPythonRuntimeSnapshot();
        console.log(JSON.stringify({
          rPinned: RSCRIPT_EXECUTABLE === realpathSync(${JSON.stringify(rShim)}),
          pythonPinned: PYTHON_EXECUTABLE === realpathSync(${JSON.stringify(pythonShim)}),
          rMutationDetected: firstR.fingerprint !== secondR.fingerprint,
          pythonMutationDetected: firstPython.fingerprint !== secondPython.fingerprint,
        }));
      `,
    ], { cwd: process.cwd(), env: shimEnv, encoding: "utf8" });
    assert.equal(shimProbe.status, 0, shimProbe.stderr);
    assert.deepEqual(JSON.parse(shimProbe.stdout), {
      rPinned: true,
      pythonPinned: true,
      rMutationDetected: true,
      pythonMutationDetected: true,
    });
  }
  console.log("TEST path_runtime_shims_are_pinned_and_fingerprinted : PASS");
} finally {
  await rm(workspace, { recursive: true, force: true });
}
