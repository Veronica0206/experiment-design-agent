import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

const serverRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(resolve(serverRoot, "package.json"), "utf8"));
const lock = JSON.parse(readFileSync(resolve(serverRoot, "package-lock.json"), "utf8"));

assert.equal(manifest.private, true, "package.json must remain private");
assert.equal(lock.packages?.[""]?.private, true, "package-lock.json must preserve private=true");

// npm must reject before contacting a registry. Pointing it at a closed local
// endpoint makes an accidental network attempt deterministic and observable.
const cache = mkdtempSync(join(tmpdir(), "expdesign-npm-private-test-"));
let publish;
try {
  const userConfig = resolve(cache, "npmrc");
  // Satisfy npm's local auth preflight so the package-owned prepublish guard,
  // rather than credentials or a network request, determines the outcome.
  writeFileSync(userConfig, "//127.0.0.1:9/:_authToken=not-a-real-token\n");
  publish = spawnSync(
    "npm",
    [
      "publish", "--json", "--registry=http://127.0.0.1:9", "--fetch-retries=0",
      `--cache=${cache}`, `--userconfig=${userConfig}`,
    ],
    { cwd: serverRoot, encoding: "utf8", timeout: 10_000 },
  );
} finally {
  rmSync(cache, { recursive: true, force: true });
}
const output = `${publish.stdout ?? ""}\n${publish.stderr ?? ""}`;
assert.equal(publish.error, undefined, publish.error?.message);
assert.notEqual(publish.status, 0, "npm publish unexpectedly accepted a private package");
assert.match(output, /EXPDESIGN_PRIVATE_PACKAGE/);
assert.doesNotMatch(output, /ECONNREFUSED|ENETUNREACH|EAI_AGAIN|ETIMEDOUT/i);

console.log("TEST npm_publish_is_locally_blocked_for_private_package : PASS");
