# Public single-endpoint release

This release makes the single-endpoint statistical engine usable independently
and through the agent. The source workspace still supports the complete private
installation; a separate public copy selects the single-endpoint profile.

## Included R source

The shared implementation consists of `config.R`, `sample_size.R`,
`bayesian.R`, `frequentist.R`, `ppos.R`, and `run_framework.R` under
`vera-experiment-designing/scripts/R/`. The public edition also includes:

- `scripts/R/examples.R` and `scripts/R/validate_framework.R`;
- `scripts/tests/run_tests.R` and `scripts/tests/public_packaging.R`;
- `scripts/tests/test_planning_consistency.R` and `scripts/tests/test_posterior_contract.R`;
- `examples/study-planning.R` and `examples/analysis-template.R`;
- the module README and existing GPLv3 license.

All examples call the shared implementation. The four older scenario drivers
and `v1_sample_size.R` stay outside the initial public selection. The original
reference template and all skill instructions/workflow/reference documents are
excluded; the new standalone template is explicitly selected instead.

## Maintained boundaries

The exact source catalog is `governance/public-release-files.json`. The separate
`tools/public_release_policy.py` permits only the named public engine files
under any `vera-*` path. Adding an arbitrary R file, changing filename case,
embedding a skill directory elsewhere, or copying an encrypted bundle does not
grant publication permission.

The exporter refuses symlinks, unexpected files, credential patterns, local
account paths, and an existing destination. It creates a fresh directory outside
the working source. The selected R core has one shared implementation; no public
fork of the statistical algorithms is maintained.

## Prepare an isolated candidate

From the complete source workspace, choose a new absolute destination outside
that workspace:

```sh
python3 -B tools/prepare_public_release.py --output /tmp/experiment-design-public-candidate
```

This copies only catalogued files, selects `single-endpoint`, narrows the agent
registry and coordinator, and writes a SHA-256 `release-inventory.json`.
It performs no Git commit, authentication, remote update or publication.

```sh
python3 -B tools/prepare_public_release.py --check /tmp/experiment-design-public-candidate
```

Review changes in source, then export a new candidate rather than editing
untracked release files. The candidate inventory proves byte identity and scope;
it is not a publication-authorization manifest.

## Validate

Use a separate clean local Git copy of the prepared candidate for tests. Install
the documented dependencies and run `make public-check`. Generated dependencies
and build files are ignored and are never part of the exported source inventory.
The checks perform actual numerical work, including regression and replay,
without any model-provider call or other private engine installation.

Public validation covers the selected implementation and installed runtime.
The complete installation's broader release and platform-specific package-byte
attestations remain separate. The optional authenticated Claude/Python model
conversation also remains separate from deterministic public validation.

The shared [statistical assurance contract](STATISTICAL-ASSURANCE.md) describes
method identifiers, prior semantics, numerical limits, trust boundaries and
the review regressions. Public CI separately audits exact Python and CRAN pins
against OSV; a matching advisory or incomplete audit fails that job.
