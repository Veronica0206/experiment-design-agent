# Publishing the public single-endpoint edition

The public edition contains the agent infrastructure and the explicitly selected
single-endpoint R engine. The remaining statistical engines and all proprietary
skill instructions, workflow/reference documents and bundles are excluded.

## Preparation and validation

Follow [PUBLIC-RELEASE.md](PUBLIC-RELEASE.md) to prepare a fresh, isolated copy.
The exact-file catalog and the independent engine allowlist both apply. A file
being absent from a denylist does not make it approved for publication.

`release-inventory.json` records a prepared candidate's hashes. It is a local
review artifact, not permission to publish. Review the exact candidate, its
license boundaries and the clean-copy `make public-check` results before a
remote update. Keep source preparation separate from the source workspace's
unrelated Git changes and history.

Never include credentials, private datasets, generated analysis artifacts,
local account paths, `.git`, virtual environments, dependencies, build outputs,
plaintext skill instructions or encrypted skill bundles in the source candidate.

## Remote publication

Remote publication requires explicit authorization for the reviewed candidate.
The existing `tools/sync-to-github.sh --publish-reviewed` entry point remains a
separate action. It requires an independently reviewed `publish-manifest.json`
and its SHA-256 pin, uses an ephemeral isolated clone, validates the pinned
GitHub repository identity and author metadata, and checks the source, index
and commit. It pushes the exact reviewed commit with an exact-head lease.

The prepared inventory does not supply or manufacture that authorization. When
publication is authorized, the publication manifest must cover all intended
source files, including the prepared inventory, with their exact hashes.
Preserve the source-profile selection and run the public edition's checks on
the candidate in addition to the complete installation's release checks where
that installation is used to publish.

The fixed public destination is `Veronica0206/experiment-design-agent` on
`main`. Do not substitute another repository, disable content/credential checks,
relax the engine allowlist, or use the nested source workspace as the push tree.

## Evidence boundaries

- A prepared inventory shows exactly which source bytes were selected.
- Public validation executes the single-endpoint engine and its agent contract
  with the other private engines absent.
- Complete-installation validation also covers the private engine domains.
- Neither deterministic check claims an authenticated model-provider session,
  scientific validation for a particular study, or a completed remote push.
