import assert from "node:assert/strict";
import { runRscript, activeRProcessGroupCount } from "../dist/r-bridge.js";
import {
  activeVerifierProcessGroupCount,
  runVerifierRequest,
} from "../dist/verifier.js";
import {
  activeRuntimeProbeProcessGroupCount,
  currentPythonRuntimeSnapshot,
} from "../dist/integrity.js";
import { installServerShutdownHandlers } from "../dist/index.js";

const [
  pidFile,
  grandchildPidFile,
  blocker,
  verifierPidFile,
  verifierGrandchildPidFile,
  verifier,
] = process.argv.slice(2);
assert.ok(
  pidFile && grandchildPidFile && blocker && verifierPidFile &&
  verifierGrandchildPidFile && verifier,
);

const before = {
  sigint: process.listenerCount("SIGINT"),
  sigterm: process.listenerCount("SIGTERM"),
  stdinEnd: process.stdin.listenerCount("end"),
  stdinClose: process.stdin.listenerCount("close"),
};
const closeServer = async () => {};
installServerShutdownHandlers(closeServer);
installServerShutdownHandlers(closeServer);
assert.equal(process.listenerCount("SIGINT"), before.sigint + 1);
assert.equal(process.listenerCount("SIGTERM"), before.sigterm + 1);
assert.equal(process.stdin.listenerCount("end"), before.stdinEnd + 1);
assert.equal(process.stdin.listenerCount("close"), before.stdinClose + 1);
process.stdin.resume();

const rOutcome = runRscript([
  "--vanilla", "-e",
  `writeLines(as.character(Sys.getpid()), ${JSON.stringify(pidFile)}); ` +
    `system2(${JSON.stringify(blocker)}, wait=FALSE); Sys.sleep(60)`,
]);
const verifierOutcome = runVerifierRequest("{}", undefined, verifier).then(
  (value) => ({ ok: true, value }),
  (error) => ({ ok: false, error }),
);
const runtimeProbeOutcome = currentPythonRuntimeSnapshot().then(
  (value) => ({ ok: true, value }),
  (error) => ({ ok: false, error }),
);

const outcome = await rOutcome;
assert.equal(outcome.aborted, true);
const verification = await verifierOutcome;
assert.equal(verification.ok, false);
assert.match(String(verification.error), /Verifier runtime is shutting down/);
const runtimeProbe = await runtimeProbeOutcome;
assert.equal(runtimeProbe.ok, false);
assert.match(String(runtimeProbe.error), /Runtime fingerprint probes are shutting down/);
assert.equal(activeRProcessGroupCount(), 0);
assert.equal(activeVerifierProcessGroupCount(), 0);
assert.equal(activeRuntimeProbeProcessGroupCount(), 0);
await assert.rejects(
  runRscript(["--vanilla", "-e", "quit(status=0)"]),
  /R runtime is shutting down/,
);
await assert.rejects(
  runVerifierRequest("{}", undefined, verifier),
  /Verifier runtime is shutting down/,
);
await assert.rejects(
  currentPythonRuntimeSnapshot(),
  /Runtime fingerprint probes are shutting down/,
);
