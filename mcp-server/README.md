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

Requires `Rscript` on PATH (the tools shell out to the skill R code under the
suite root). The Claude agent path additionally requires Claude Code 2.1.197+
for agent-scoped hooks, `prompt_id` ledger binding, and the configured Claude
Sonnet 5 model.

## Use as a Claude Code agent

`../.mcp.json` registers this server as `experiment-design`. Open Claude Code in
the suite directory and invoke the **experiment-designer** subagent (or launch
it with `claude --agent experiment-designer`)
(`.claude/agents/experiment-designer.md`) — it drives these tools, runs the
verification gate, and interprets the results. `design-verifier` is an optional
fresh re-execution surface governed by the same engine and fail-closed policy;
it is not an independent methodology audit.

## Use from Python (Streamlit harness)

`../agent-harness/` drives the same tools; see its `streamlit_app.py`.

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

`indirect_compare` runs single Bucher comparisons (no chaining) or MAIC; the
MAIC path accepts `covariates` (default: all target-CSV columns) and
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
pinned, PATH-resolved R and Python executables, the underlying R engine/shared
runtime, resolved library/package paths, installed package-tree bytes,
R/Python gate sources, and every executed R regression script. Regression
children run with `--vanilla`, so untracked R profiles cannot alter the
attestation; a runtime or package change cannot reuse an older cached result.
Per-request cancellation stops only that caller's wait and cannot cancel
another caller's shared regression run. Failed payloads are replaced by a
safe, value-free envelope. Accepted model-visible results are privacy-safe
views bound to the server's exact canonical report. `_verification` carries the
runtime-issued identity and public-result/report commitments. Model-visible
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

## Architecture

`src/index.ts` (tool schemas) → `src/r-bridge.ts` (spawns the pinned Rscript detached as a
process-group leader; JSON via temp files; timeout kills the whole process
group so `run_tests` grandchildren cannot survive; regression grandchildren
also use `--vanilla`) → `r-wrapper/dispatcher.R`
(sources the skill R and dispatches). R diagnostics stay private; model-facing
errors use a fixed category and remediation rather than exposing paths, labels,
or data-dependent text. Workload-limit errors disclose only the computed
formula inputs and the safe knob to reduce.
