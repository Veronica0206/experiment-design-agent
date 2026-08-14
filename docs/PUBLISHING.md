# Publishing the public portfolio distribution

This document describes the safety boundary for publishing this repository. It
does not contain account credentials, local filesystem locations, private
package inventories, or instructions for distributing proprietary engines.

## Distribution contract

The public repository is a portfolio and source-review distribution. It may
contain only files positively authorized by the reviewed publication manifest.
The proprietary statistical engines are intentionally excluded, so a public
clone is not a standalone executable agent.

Never include any of the following in a public candidate:

- Proprietary statistical engine or skill directories, regardless of case.
- Plaintext or encrypted skill bundles.
- Credentials, tokens, private keys, authentication files, or credential-like
  values in source, diffs, commit metadata, or history.
- Private datasets, generated analysis artifacts, row-level outputs, or local
  filesystem paths.
- Files that are merely absent from a denylist but are not positively approved
  by the publication manifest.

## Required publication boundary

Publication must use the reviewed synchronization workflow rather than a
reusable working clone. That workflow must:

1. Resolve its shell, interpreter, version-control client, and supporting
   executables from reviewed absolute locations and sanitize inherited runtime
   configuration.
2. Build the candidate in a newly created temporary clone and remove that clone
   after the attempt.
3. Verify the immutable destination identity, canonical repository URL,
   account, branch, and expected remote state before any push.
4. Apply the strict public-distribution rules unconditionally; repository
   visibility is mutable metadata and is not a safety switch.
5. Validate the candidate source and staged tree, scan every staged blob for
   credentials, then validate the created commit and its metadata.
6. Require the publication manifest and its independently reviewed SHA-256 pin
   to authorize the exact candidate tree.
7. Fail closed on any mismatch. Do not weaken a guard to make a release pass.

A push is a separate, explicitly authorized action. A successful dry run does
not authorize publication.

The publisher refuses to act when invoked without a mode. After preparing and
independently reviewing an exact public candidate and its manifest hash, run a
non-mutating rehearsal first:

```bash
EXPDESIGN_PUBLISH_SRC=/absolute/path/to/reviewed-public-candidate \
EXPDESIGN_PUBLISH_MANIFEST_SHA256=<reviewed-sha256> \
tools/sync-to-github.sh --dry-run
```

Only a separate, deliberate invocation with `--publish-reviewed` may push the
same reviewed candidate. Do not reuse a prior dry run as authorization, and do
not substitute another repository, branch, clone, commit message, or manifest
pin.

Anyone reviewing a public clone can run `make public-check`. That limited check
enforces the strict public path policy, compiles the distributed MCP interface,
and verifies that npm publication is blocked. It does not execute the omitted
engines or replace the complete internal release gate.

## Validation claims

Keep these evidence levels separate:

- A TypeScript build shows that the distributed MCP source compiles.
- A public-distribution check shows that the candidate satisfies the public
  tree policy it actually evaluates.
- The complete internal release suite requires the authorized installation,
  including the omitted engines.
- MCP executable startup checks only that required private engine entrypoints
  are present before it connects or advertises tools. That check is not a
  scientific, dependency, regression, or release-readiness attestation.

None of these checks alone establishes that a study design is appropriate for
a particular scientific, clinical, ethical, or regulatory use.

## If a check fails

Stop the publication attempt, retain only non-sensitive diagnostic evidence,
and correct the source or workflow. If public history may contain private
material or credentials, treat that as an incident: stop further publication,
assess every reachable ref, rotate affected credentials, and coordinate any
history repair before resuming.
