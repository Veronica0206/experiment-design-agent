# Experiment Design MCP Server

Wraps the R experiment-design framework as 11 typed MCP tools over stdio:

**Planning** — `validate_config`, `sample_size`, `simulate_design`,
`master_simulate`, `indirect_compare`, `meta_analyze`
**Construction (DOE)** — `ab_test`, `factorial_design`, `rsm_design`, `randomize`
**Verification** — `run_tests`

## One-time setup

```bash
cd mcp-server
npm ci
npm run build      # compiles src/ -> dist/ (dist is gitignored)
```

This package is intentionally non-publishable: `private: true` and a local
`prepublishOnly` guard make an accidental `npm publish` fail closed.

The setup commands above require Node/npm on the operator's `PATH`. At runtime,
the MCP server never resolves Rscript or Python from ambient `PATH`: it uses a
closed list of absolute installation candidates and prefers
`../agent-harness/.venv/bin/python`. For nonstandard installations, set reviewed
absolute `EXPDESIGN_RSCRIPT` and `EXPDESIGN_PYTHON` paths; relative overrides
are rejected. R profile and library environment variables are cleared, so the
governed runtime uses only the selected R installation's default libraries;
project-local `renv` libraries are not implicitly activated. R must provide
`jsonlite`, `mvtnorm`, and `survival`; the release gate also validates their
applicable transitive lock closure and every installed `renv.lock` record. The
release gate separately compares bounded, no-follow SHA-256 hashes of every
applicable installed R package tree with the reviewed exact-version/platform
profile. That profile detects post-review installed-byte drift; it does not
authenticate package archives and must be generated only after an independently
clean, lock-restored review. The Claude agent path additionally requires a
canonical Claude Code product identity and version 2.1.197+ for agent-scoped hooks,
`prompt_id` ledger binding, and the configured Claude Sonnet 5 model. The
release gate checks that installed version without requiring login;
`make check-claude-live` is the optional authenticated-session check.
That readiness check uses only standard absolute installation candidates or a
reviewed absolute `EXPDESIGN_CLAUDE` override, not ambient `PATH`. Its strict
product/version line prevents accidental tool confusion; it is not a
cryptographic attestation against a deliberately impersonating same-user binary.

## Use as a Claude Code agent

`../.mcp.json` registers this server as `experiment-design`. Open Claude Code in
the suite directory and launch the recommended routing-only coordinator with
`claude --agent experiment-design-coordinator`. It delegates to the smallest
approved specialist set and combines only verified canonical reports. The
legacy **experiment-designer** remains available as an all-domain compatibility
surface. `design-verifier` is an optional fresh re-execution surface governed
by the same engine and fail-closed policy; it is not an independent methodology
audit.

## Use from Python (Streamlit harness)

`../agent-harness/` drives the same tools. From the suite root, launch its UI
through the reviewed pre-site boundary with
`tools/run-reviewed-python.sh -m streamlit run agent-harness/streamlit_app.py
--server.headless true --server.address 127.0.0.1 --server.port 8501`. The UI is
unauthenticated, so it must stay bound to the loopback interface.
That boundary verifies the exact venv distribution/file surface before it adds
the validated package paths; it never processes `.pth` startup code and rejects
executable bytecode caches before the UI starts.

## Schema contract (truth at the tool boundary)

The MCP SDK parses arguments with zod; a key absent from a schema would be
silently stripped, so the run would proceed with defaults under the caller's
requested label. The server closes that class of bug three ways:

- **Complete schemas.** Every engine-read `create_config()` /
  `create_master_config()` formal is exposed and typed:
  - `validate_config` / `sample_size` accept `prior` (named or custom
    hyperparameters), `go_threshold`, `consider_threshold`, `go_target` — so
    validating a config validates *the* config that will be simulated.
  - `simulate_design` additionally accepts `p2_data_ctrl` and `p3_alloc_ratio`;
    a controlled confirmatory design requires control-arm exploratory data;
    missing control data is rejected instead of silently degrading to single-arm
    PPOS. Controlled OC likewise requires `{n_trt,n_ctrl}` rather than a scalar.
  - `master_simulate` accepts the full documented basket/umbrella/platform
    parameter set (`phase`, `chen_strategy`, `tau_prior`, `homogeneity_prior`,
    `response_prior`, `cbhm_a`/`cbhm_b`, `soc_data`, `ia_pruning_alpha`,
    `futility_boundaries`, `n_drop_per_stage`, `rar_gamma` (constant form only —
    the stage-increasing function default cannot be expressed in JSON),
    `n_per_interim`, `ncc_weight_decay`, `rar_burn_in`,
    `rar_min_alloc`, `interim_frequency`, `effect_threshold`,
    `futility_threshold`, ...).
- **Strict tool objects.** Every top-level tool input and nested configuration
  object is strict: an unknown key (typo, or a reserved knob
  like `overdispersion` / `rar_eta`) errors at the boundary instead of being
  stripped. Knobs intentionally rejected by the R layer stay exposed so the
  request fails rather than silently degrading; model-facing execution errors
  use a fixed value-free message. These include single-value enums (`tte_method`, `rate_method`,
  `fwer_control`, `power_type`, `shared_control`), `selection_rule` (derived
  only), and the design-scoped keys `phase` / `borrowing_method` (basket),
  `umbrella_method` (umbrella), `ncc_method` (platform), which error when sent
  to the wrong design type.
- **Echoed effective values.** Where R adjusts or defaults an input, the result
  says so:
  - `simulate_design`: `B_used` (B_oc is hard-capped at 5000 — the schema also
    enforces `max(5000)`), `n_oc_used` (the OC sample size actually simulated,
    number or `{n_trt, n_ctrl}`), `seed`.
  - `master_simulate`: `seed` (the config's resolved seed).
  - `factorial_design` / `rsm_design`: `seed` echoed when `randomize=true`.
  - `randomize`: `block_size_used` — a requested `block_size` is honored only
    when it is a whole multiple of the ratio's base block; otherwise the base
    block is used, and this field shows which.
  - `meta_analyze`: `n_input`, `k_used`, `dropped_studies` (+ per-study
    `dropped_detail` with reasons) — pooling drops studies with non-finite or
    malformed inputs, and that exclusion is now visible instead of silent.
  - DOE tools (`ab_test`, `factorial_design`, `rsm_design`, `randomize`):
    provided parameters that the selected R routine does not declare are
    returned in `ignored_params` instead of being dropped silently (e.g.
    `levels` on a fractional design, `alpha`/`fraction` on a Box-Behnken).

At the orchestration layer, the least-privilege indirect-comparison agent is
deliberately restricted to one Bucher comparison or one MAIC analysis per
delegated task. The lower-level MCP `indirect_compare` tool accepts a batch of
1–1000 independent Bucher comparison objects in `comparisons`; that batching is
not a multi-edge Bucher chain or network meta-analysis workflow. The MAIC path
accepts `covariates` (default: all target-CSV columns) and
`tte_method` (`cox` default, or `exponential`). Non-Cox source-effect inference
uses a stratified nonparametric bootstrap that refits the MAIC weights; its
replicate count and seed are explicit and echoed. Row-level MAIC weights remain in
the private artifact directory; model-visible output contains aggregate weight
summaries, ESS, balance, effects, and artifact provenance only.

## Trust boundary

The server is local, single-user tooling and shells out to `Rscript`. Reads are
restricted to the suite root by default (extend with
`EXPDESIGN_ALLOWED_READ_ROOTS`); outputs are restricted to the persistent run
root (`EXPDESIGN_RUNS_DIR`). Paths must already be whitespace-normalized. The
resolved input path must be inside an allowed root and name a regular file
before it is opened with no-follow and nonblocking flags; a descriptor-level
device/inode check detects replacement of the inspected file between `stat`
and `open`. Tool sizes and result context are bounded before allocation.
Authorized CSVs are read in
bounded chunks from that descriptor, so a concurrent append cannot bypass the
50-MiB cap.
Oversized results are withheld from model context and discarded without
creating a model-visible or persistent unverified artifact; their managed
directory is deleted synchronously. Caller-selected output directories
must not already exist and are claimed atomically. Managed run directories,
for accepted results, default to 30-day/100-directory
retention (`EXPDESIGN_ARTIFACT_RETENTION_DAYS`, `EXPDESIGN_ARTIFACT_MAX_DIRS`).
Cross-process lifecycle locks use immutable nonce-bearing owner records and are
never auto-reclaimed: after a process crash, an operator must confirm the
recorded PID is gone before explicitly removing `.expdesign-lifecycle.lock`.
This fail-safe policy trades availability for protection against check/remove
ABA races that could otherwise permit concurrent retention mutations.

Before a gated result reaches model context, the server runs the shared Python
design checks, the cached full regression suite for the active engine
fingerprint, and required same-seed replay. The fingerprint is recomputed from
pinned R and Python executables resolved only from reviewed absolute overrides
or closed installation-location lists, the underlying R engine/shared
runtime, resolved library/package paths, and the complete installed Node
production dependency closure resolved from `package-lock.json` (including
flattened transitive packages). That startup traversal fails closed above
20,000 files or 256 MiB. The fingerprint also covers
R/Python gate sources, and every executed R regression script. Regression
children run with `--vanilla`, so untracked R profiles cannot alter the
attestation; a runtime or package change cannot reuse an older cached result.
Per-request cancellation stops only that caller's wait and cannot cancel
another caller's shared regression run. Failed payloads are replaced by a
safe, value-free envelope. Accepted model-visible results are privacy-safe
views bound to the server's exact canonical report. `_verification` carries the
runtime-issued identity, public-result/report commitments, and a commitment to
only the allowlisted public argument projection. Raw argument and result hashes
remain verifier-internal so private paths, labels, rows, and strata do not
become offline equality oracles. Model-visible
`_provenance` carries runtime and package identity plus boolean/count summaries;
it deliberately omits the canonical config digest and raw input/artifact
digests, which would otherwise act as offline equality oracles. Those digests
remain in client-only MCP `_meta`; private artifacts receive opaque
`expdesign-artifact://` handles mapped to paths only in that client metadata,
and each digest is identity-bound to its `audience: ["user"]` resource link for
host-side download verification. Provenance hashing, automated verification,
publication, and download open artifact descriptors with no-follow and
nonblocking flags, confirm regular-file type, and perform bounded reads so a
FIFO cannot block the process and a concurrent append cannot bypass the 50-MiB
artifact cap.
Private row-level or assignment files are never embedded in model text. The
audience annotation is a client-routing hint, not an access-control boundary;
the host-side handle resolver, root containment, regular-file checks, and
bounded digest/identity validation form the artifact-delivery boundary.
This local, single-user boundary does not claim isolation from a malicious
process running as the same operating-system user and replacing an authorized
input parent before inspection or an entire managed output directory. Allowed
input roots, their parents, and the configured run root must remain private and
trusted.

Client cancellation also terminates a running Python verifier process group.
On `SIGINT`, `SIGTERM`, or stdio EOF/close, the executable server cancels and
awaits every registered R analysis, Python verifier, and R/Python runtime-
fingerprint probe process group before closing; imported modules do not install
process-global handlers.

## Architecture

`src/index.ts` (tool schemas) → `src/r-bridge.ts` (spawns the pinned Rscript detached as a
process-group leader; JSON via temp files; timeout kills the whole process
group so `run_tests` grandchildren cannot survive; regression grandchildren
also use `--vanilla`) → `r-wrapper/dispatcher.R`
(sources the skill R and dispatches). R diagnostics stay private; model-facing
errors use a fixed category and remediation rather than exposing paths, labels,
or data-dependent text. Workload-limit errors disclose only the computed
formula inputs and the safe knob to reduce.
