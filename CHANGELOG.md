# Suite Changelog — infrastructure components

Engine (R) history lives in each skill's `docs/changelog.md`. This file covers
the cross-cutting components: `agent-harness/`, `hooks/`, `.claude/agents/`,
`mcp-server/`, and suite-level tooling.

## 2026-09-08 statistical review corrections

- Share a versioned single-endpoint result contract across execution,
  verification and deterministic reporting. Retain method names, simulation
  uncertainty, numerical precision and posterior moment-existence diagnostics.
- Anchor OC grids to the exact null and alternative; report unfavorable design
  performance separately from execution and numerical validity.
- Use one controlled-binary planning/PPOS power rule, validate noncentral-t
  tails, enforce integer arm minima and recompute achieved sizing power.
- Correct the continuous Jeffreys prior to be invariant to units; use analytic
  Gamma-ratio summaries and deterministic bounded posterior probabilities.
- Account for scenario and inner-probability work before admitting OC jobs.
  Add numerical counterexamples and actual R-to-public-report regressions.
- Audit every locked Python and CRAN package against OSV, alongside the Node
  audit, and update vulnerable Python dependencies. The harness now requires
  Python 3.10 or newer.
- Document statistical assumptions, host trust boundaries and measured
  numerical work in [Statistical assurance](docs/STATISTICAL-ASSURANCE.md).

## Unreleased — 2026-09-08 public single-endpoint edition

- Select complete or single-endpoint capabilities from a strict repository-owned
  profile; scope tools, agent routing, startup dependencies, regression counts,
  fingerprints and native hooks to that installation.
- Prepare an isolated public source tree with the six shared R engine files,
  curated examples and tests. Exclude other engines, proprietary skill content,
  reference documents, older duplicate scenario drivers and generated artifacts.
- Keep default numerical methods unchanged; add explicit example simulation
  budgets and robust paths, including directories containing spaces.
- Add actual public MCP calculation/replay tests, profile rejection tests and
  source-inventory checks. Public CI validates the runnable selected engine.
- Preserve the R module's existing GPLv3 notice alongside the agent's MPL 2.0
  notice. Preparation records byte hashes and performs no remote publication.

## Unreleased — 2026-09-04 agent review fixes

- Configure the native coordinator for foreground dispatch and check the
  Claude Code 2.1.233 protocol at startup. Separate the exact native Agent
  telemetry footer from child content before canonical-result comparison and
  failure propagation.
- Preserve unresolved clarification inputs as exact user-message envelopes.
  Native hooks verify keyed commitments without persisting raw prompts;
  the Python coordinator forwards only the context a specialist has not
  already received. Completed results are not added to clarification inputs.
- Preserve validated MCP input rejections as typed errors and permit bounded
  correction or structured clarification. Rejected requests do not trigger
  statistical verification. Malformed evidence remains terminal, and
  ambiguous correction calls cannot share a verification lineage.
- Exclude uncommitted staging runs from committed artifact retention capacity.
  Aborting a run removes its own staging output without evicting verified
  output; capacity cleanup applies when a verified run is committed.
- Keep a supervisor as the process-group leader for standalone detached
  Linux/macOS runtimes until descendant cleanup finishes, including when
  descendants retain inherited descriptors after the runtime exits.
- Add regressions for native dispatch and footer handling, clarification
  integrity, bounded request correction, artifact retention, and standalone
  descendant cleanup.

## Unreleased — 2026-09-03 review and SCD demo readiness

- Made the lifecycle mutating-file assertion accept the open-phase detection
  (`changed while opening`) as well as the read-phase detection. Under CPU
  contention the first append landed between the named `lstat` and the
  descriptor `stat`, which the fingerprint code correctly rejects, and the
  release gate failed on wording alone.
- Raised the production dependency overrides after new advisories:
  `fast-uri` 3.1.5 -> 3.1.7 (four high-severity host-confusion/SSRF advisories
  in 3.0.0-3.1.5) and a new `qs` 6.16.0 override (array-limit bypass and
  attacker-controlled `isBuffer` denial of service in <= 6.15.3).
  `npm audit --omit=dev --audit-level=high` is clean again.
- Added a MAIC form to the Streamlit indirect-comparison mode. The direct form
  path previously offered only Bucher, so a matching-adjusted comparison could
  run only through the LLM chat mode or the Claude Code agent.
- Documented the workspace-trust requirement for agent-frontmatter hooks
  (Claude Code 2.1.218+) and how to confirm that the ledger and Stop hooks
  actually fired, and documented the two-turn anchored-MAIC workflow (MAIC, then
  Bucher with the restated effect) in the READMEs, the indirect-comparison
  agent prompt, and the legacy harness prompt.
- Known limitation recorded, not changed: public MAIC and Bucher reports omit
  arm, covariate, and treatment labels by design (strings pass the projection
  only through enum tables), so `arm_summary`, `ess`, and `balance` rows are
  identified by position (arms in alphabetical label order; covariates in
  request order). Labels remain in the private artifacts.

## Unreleased — 2026-08-26 third-round public-interface review

- Bound response-surface outputs to the effective request, canonical standard
  order, reported run order, and requested randomization state. CCD and BBD
  geometry checks now reject incomplete, malformed, and resolution-II cores,
  while public metadata remains literal and projection-idempotent. Standard
  order now uses the same numeric-aware factor-name order as public aliases,
  independent of private column insertion order.
- Moved MCP tool dispatch behind a repository-controlled strict boundary so
  malformed tool names, argument containers, schemas, and supported
  cross-field combinations receive the fixed public error taxonomy before an
  R workload or input-file read begins. A real stdio boundary regression runs
  in the engine-free public lifecycle as well as the complete installation.
- Replaced ambient Node environment inheritance with an explicit Python-side
  allowlist and a second, empty-environment launcher boundary. Only reviewed
  runtime, locale, temporary-directory, artifact, and authenticated supervisor
  values reach the MCP server process.
- Added compositional response budgets at the tool-result, verifier-envelope,
  canonical-report, public-result, and client-frame layers. Exact-limit and
  over-limit regressions are now part of both the local suite and the pinned
  public assurance workflow.

## Unreleased — 2026-08-26 second-round public-interface review

- Made the standalone MCP smoke suite fail closed: every executed check is
  registered centrally, any failure produces a nonzero process status, and a
  meta-test rejects direct result printing or scattered exit paths.
- Bound custom fractional-factorial generators to both the effective request
  and the returned design matrix. Public reports now use label-free,
  one-based structures for generators, defining relations, and bounded
  main/two-factor alias classes. Platform reports now distinguish requested,
  enabled, applied, and fallback analysis behavior.
- Made allocation-ratio validation invariant to common positive scaling in
  both TypeScript preflight and Python gates, while continuing to reject
  ratios whose normalized integer quotas exceed the supported bound.
- Added a fixed, path-free public MCP error taxonomy and bounded binary stdout
  and stderr capture. Arbitrary server messages, invalid-output previews,
  local paths, and stack details remain confined to the private diagnostic
  stream.
- Pinned the exact public assurance workflow before `make public-check` trusts
  it, and regenerated the fractional-factorial canonical report example from
  the reviewed complete installation after the reporting contract changed.

## Unreleased — 2026-08-25 independent public-interface review

- Reconciled the master-protocol MCP schema with its verifier: unsupported
  efficacy stopping is no longer advertised, platform essentials are checked
  before execution, and unsupported interim/platform-only combinations fail
  with configuration errors instead of consuming an R workload first.
- Bound A/B sizing and randomization outputs back to their effective requests,
  including sample-size arithmetic, seeds, methods, arm rosters, allocation
  contracts, block structure, and exact unit coverage.
- Replaced unbounded runtime and mutable engine-source fingerprinting plus
  inherited child environments with bounded descriptor reads, mutation
  rebinding, cancellation, and explicit runtime environment allowlists.
- Added a hard verifier-stdout ceiling and terminal, nonblocking JSON-RPC
  handling. Response envelopes are validated before routing, duplicate IDs
  retire only their own process generation, and concurrent cleanup is
  serialized so a delayed reader cannot stop a restarted server.
- Added a least-privilege public CI workflow for public-distribution, Node
  lifecycle, Python contract, hook/governance, dependency, and credential
  checks. Checkout credentials are discarded, and a structural validator pins
  the exact read-only permissions, actions, steps, environments, and commands.
  The public lifecycle stops honestly at the missing-engine preflight. Its
  reduced test mode now requires an exact `--public-only` argument and rejects
  the former ambient environment switch, so a polluted shell cannot silently
  weaken a complete lifecycle run.

## Unreleased — 2026-08-14 public distribution boundary

- Made the public portfolio boundary prominent in both READMEs: the required
  proprietary statistical engines are excluded, a fresh clone is not a
  standalone executable agent, and a public build is not a complete readiness
  attestation.
- Added an executable-only MCP startup preflight that requires the private
  engine entrypoints before transport connection or tool advertisement. Module
  import remains side-effect free; an incomplete installation exits with one
  fixed, path-free diagnostic and has no preflight bypass. The presence check
  is explicitly not presented as scientific, regression, or release evidence.
- Added a public-safe publishing policy that separates candidate-tree checks,
  complete internal validation, and explicit push authorization while keeping
  proprietary material, credentials, private data, and local paths outside the
  public distribution.
- Replaced the reusable publication clone with a temporary clone and a pinned,
  public-only publication chain. The workflow resolves reviewed executables,
  validates the fixed GitHub repository identity and every candidate state,
  fixes commit metadata, requires an explicit publish mode, and applies the
  manifest pin plus strict public path rules before and after commit creation.
- Added `make public-check` for the source-review distribution. It validates
  the public path policy, compiles the public MCP interface, and confirms npm
  publication remains blocked without claiming that the omitted engines run.
- Added a bounded pre-site sanitizer for RECORD-declared bytecode shipped in
  locked wheels. It removes only regular `.pyc`/`.pyo` files inside the selected
  venv package roots, rejects symlinks and traversal races, and immediately
  hands the normalized environment to the exact distribution/RECORD validator.

## Unreleased — 2026-08-13 adversarial review (round 7)

- Bound a fractional design's defining relation to the matrix it describes:
  every reported word's column product must equal +1 on every non-center run,
  and the run count must match 2^(k-p) plus centers. Name, length, and
  cardinality checks alone accepted a design generated as D=AB that claimed
  I=ABCD, reporting resolution IV while its main effects were aliased with
  two-factor interactions.
- Bound factorial output back to the caller's effective factor count, levels,
  replicate count, centers, and fraction. Fractional designs now honor the
  supported replicate option explicitly, while non-two-level fractional
  requests fail before R execution.
- Required MAMS boundary provenance to agree with the requested configuration
  and stage behavior: package boundaries enable efficacy at every stage and
  have no fallback, while approximated or user-supplied boundaries enable only
  final-stage efficacy and disclose a fallback; only an actual user-boundary
  request may report `user_supplied`.
- Matched Python's canonical form to JavaScript's fixed-notation number wire
  format for integral floats below 1e21, and rejected every integral MCP input
  outside JavaScript's exact-safe range. A large decimal can otherwise retain
  the same text after a JavaScript round trip even though its numeric value has
  already changed, defeating a text- or hash-only corruption check.
- Made launch-configuration validation type-strict and duplicate-key safe:
  `autoPort: 0` no longer satisfies `false`, `port: true` no longer satisfies
  8501, and a repeated JSON key fails instead of silently keeping the last.
  Added direct mutation, duplicate-key, and symlink tests over the launch policy.
- Stopped malformed or duplicate platform analysis-method declarations (for
  example `[{}]` or repeated semicolon tokens) from raising or being collapsed
  into an apparently valid set before the gate returned its verdict.

## Unreleased — 2026-08-13 adversarial review (round 6)

- Bound the Streamlit UI to the loopback interface in the preview
  configuration, the README, the MCP README, and the bootstrap hint. The UI is
  unauthenticated and exposes approved input roots and private artifact
  downloads, and the previous launch listened on every interface.
- Brought `.claude/launch.json` inside the release boundary: it is now a
  required regular runtime file and is pinned by whole-document equality, so a
  replaced interpreter, an added flag, a non-loopback bind address, or an
  automatic port reassignment fails `make validate-config`.
- Required a fractional design's defining relation to be unique, non-repeating
  words of two or more of that design's own factor letters, with the group
  cardinality 2^p - 1. Comparing `len(str(value))` with the resolution had
  accepted `null`, `true`, and `1000` as four-character words.
- Made the MAMS boundary contract unskippable: an absent or null
  `boundary_source` no longer bypasses validation for the default umbrella
  method, and the boundary vectors must cover exactly the design's stages
  rather than merely matching each other's length.
- Reconciled the platform analysis-method declaration with the per-arm methods
  actually run, and rejected blank or whitespace-only method labels.
- Rendered canonical reports from the same normalized value used for identity
  hashing. A whole-number argument crosses the MCP boundary through JavaScript
  as `1` but reads back in Python as `1.0`, so the server and the Stop hook
  rendered `"sd": 1` and `"sd": 1.0` for one analysis and the hook rejected a
  correct report. Found by adding the Stop-hook assertion to the one-stage
  umbrella integration case.

## Unreleased — 2026-08-12 comprehensive review (round 5)

- Made the public factorial-design projection idempotent: canonical `factor_N`
  aliases are ordered numerically, so a 10-, 11-, or 12-factor design no longer
  re-aliases when the Stop hook rebuilds the expected report. A correct
  many-factor design was previously blocked as not canonically bound.
- Identified center rows in the full-factorial gate by coordinate vector rather
  than position, kept the natural midpoints of an odd-level grid, and validated
  the complete replicated grid as a multiset. A randomized run order with center
  points was previously failed as a level-count defect.
- Restored the array shape at the three gate reads of an R vector that can hold
  one element (`_r_vector`): a fractional design's defining relation, the MAMS
  per-stage boundary pair, and the platform analysis-method set. The dispatcher
  serializes with `auto_unbox = TRUE`, so those arrived as scalars and were
  rejected as missing contracts. Every other vector a gate reads is either a
  data frame, which jsonlite always emits as an array of row objects, or has at
  least two elements by construction. Added live-serializer regressions rather
  than hand-built list fixtures.
- Replaced the duplicated hook domain-tool table with one frozen
  `hooks/domain_tool_policy.mjs`, and made the manifest validator execute its
  inspector and require three-way parity with `governance/agents.json` and
  `registry.DOMAIN_TOOLS`, with drift rejected from either side.
- Corrected the enforcement documentation: the server verifier runs the
  raw-result `design_checks_for` mapping, while the hook, Python harness, and
  Streamlit form validate the server-bound public envelope, attestation, and
  canonical-report binding and require fresh regression evidence. Removed the
  unused gate import that implied a second recomputation layer.

## Unreleased — 2026-08-08 comprehensive remediation

- Rejected mixed meta-analysis effect scales before pooling and retained the
  effective measure in the public result and verification contract.
- Added resolved, privacy-safe configuration reports; retained result-affecting
  priors and adaptive-design settings in canonical reports; and distinguished
  partially verified reports in their title.
- Added Monte Carlo uncertainty and precision disclosure across single- and
  multi-arm simulations. Missing uncertainty now fails verification and low
  precision remains explicitly partial.
- Corrected package-backed MAMS boundary validation and the Exact 3.3
  unconditional-test API, with regressions for installed, missing, and failed
  optional-package paths.
- Closed non-finite JSON coercion, malformed-envelope, result-file replacement,
  publication case-folding/post-commit, dependency-provenance, and hook retry
  gaps with adversarial regressions.
- Removed routing ambiguity by requiring explicit Codex skill invocation,
  aligned clarification/failure contracts across agent surfaces, and expanded
  validators to parse R/Python/shell sources plus prompt fixtures.
- Added hash-locked Python requirements and an R `renv.lock`; the release gate
  now validates the installed R runtime/packages and the actual Claude Code
  runtime before certifying the suite.
- Removed the obsolete free-form defaults-confirmation instruction. Governed
  configuration-only turns now return the exact server-generated report.

## Unreleased — 2026-08-05 Ultra-review closure

- Scoped the trust claim honestly: the governed agent's completed answer is
  canonically bound; interim tool-turn narration is prohibited by prompt but
  is not visible to the hook API, and parent-agent paraphrase is outside the
  boundary.
- Made the synchronous ledger atomic, locked, gap-detecting, and fail-closed;
  malformed MCP results are retryable per call, while coupled report/hash
  tampering, identity reuse, launcher failures, and unrelated-call laundering
  are rejected.
- Replaced model-visible private filesystem paths with opaque artifact handles;
  input-file reads now authorize a resolved regular file before a nonblocking
  open and retain the descriptor-level identity check. Artifact provenance
  hashing, automated verification, publication, and host-side download also use
  no-follow, nonblocking, bounded descriptor reads, rejecting symlinks,
  non-regular files, and artifacts larger than 50 MiB.
- Added exact regression check-count attestation, framed runtime hashing,
  staged-blob publication validation, fail-closed credential scanning, a
  self-contained skill validator, and patched dependency overrides.
- Closed reviewed statistical gaps: controlled-continuous sensitivity,
  controlled incidence direction coverage, sparse risk-difference exclusions,
  modified HKSJ regression coverage, Simon two-stage dispatch, BHM TTE/rate
  execution, MAMS boundary-source disclosure, global-null umbrella FWER guards,
  and explicit survival dependency failure.

## Unreleased — 2026-07-27 identity-safe verification hardening

- Final Ultra-review closure: verification events now enter a synchronous,
  per-turn ledger before stop enforcement; the server attests the active
  regression suite and live runtime/package-tree fingerprint, binds privacy-safe results
  to the exact canonical report, and exposes sensitive artifacts only through
  user-audience resource links. The audience annotation is routing metadata,
  not access control. Artifact staging, leases, release, and cleanup use
  nonce-bearing ownership records; immutable lifecycle locks are never
  auto-reclaimed after a crash because filesystem check/remove lacks an
  owner-CAS primitive.
- Canonical, privacy-aware completed reports are rendered from verified
  payloads. The hook re-renders and compares the completed answer after report
  normalization; the harness substitutes the canonical report. Interim text
  and parent-agent paraphrase are not covered by that technical guarantee.
- Runtime identities are immutable, retry budgets persist by analysis lineage,
  and server/hook argument hashing now uses the same caller-domain boundary.
- Reproducibility ignores runtime envelopes while comparing domain values
  exactly, including arbitrary-size integers.
- Artifact directories are atomically claimed through one lifecycle manager;
  accepted outputs use bounded retention, while oversized unverified outputs
  are discarded synchronously.
- R executable, resolved library/package paths, and installed package-tree
  bytes participate in every regression-cache key and post-run fingerprint
  check; authorized CSVs are read in bounded chunks from the opened descriptor.
- Publishing scans consume the entire staged diff, avoiding `pipefail`/SIGPIPE
  credential bypasses; audit metadata and provenance are private by default.
- Platform budgets, study-horizon schedules, endpoint/NCC compatibility, and
  scalar arm sizes are rejected before execution when invalid.
- Regression manifests, master CSV/PDF content, and skill/agent inventories now
  reconcile structurally and fail closed.
- Portable MCP loading with `${CLAUDE_PROJECT_DIR:-.}` and clean-start smoke coverage.
- Exact-result verification envelopes and a per-analysis ledger replace global
  pass state; unrelated calls can no longer inherit trust.
- Failed tool payloads are buffered and redacted from model/UI context.
- Hook escapes now permit only value-free failure reports and internal hook
  failures fail closed.
- Strict regression manifests, full-field reproducibility, target-power/OC
  proximity checks, unique randomization IDs, and non-vacuous tool contracts.
- Strict top-level MCP schemas, persistent/sandboxed output paths, read-root
  controls, resource/result limits, and provenance hashes.
- Private redacted audit logs, out-of-order MCP response retention, cross-platform
  skill validation, proprietary publish guard, and unified `make release-check`.

## Unreleased — 2026-07-16 comprehensive review (round 3)

Five parallel adversarial audits (MCP server, harness, hook + agent
definitions, R engine, docs) plus a 35-proposal improvement sweep; every
confirmed finding fixed and regression-covered.

### Enforcement hook (`hooks/enforce_verification.py`) — rewritten
- **The hook never fired in production**: on current Claude Code, SubagentStop
  delivers the subagent's transcript as `agent_transcript_path`; the hook read
  `transcript_path` — the PARENT session file, which contains none of the
  subagent's tool calls — so every stop was silently allowed. Now reads the
  agent transcript (parent path kept as an older-runtime fallback).
- Grouping fixed: the transcript stores one content block per JSONL line;
  parallel tool_use blocks share `message.id`. The old line-index "order" made
  both round-2 fixes ineffective (a failing call was laundered by a passing
  parallel sibling; run_tests in the same message never counted).
- Laundering closed: enforcement now covers the **last message group per
  tool** — a failing design is superseded only by a passing re-run of the same
  tool, never by a later unrelated call.
- Loop escapes count consecutive failing **attempts** (message groups), so a
  3-call fan-out can no longer satisfy the escape on the first stop; the
  run_tests escape additionally requires the latest attempt to be at/after the
  last gated call.
- Symlink-safe `realpath` import of `gates.py` (a symlinked install previously
  fail-opened the design gate silently); degraded mode now logs to stderr.
- Validates the server-bound public envelope and design-check attestation, then
  requires a fresh regression result; it does not recompute private raw-result
  checks in the hook process.
- New self-test suite `hooks/tests/test_enforce_verification.py` covering
  synthetic transcripts in the verified real format.

### Verification gates (`agent-harness/gates.py`)
- New `design_checks_for()` — one raw-result tool→checks mapping run inside the
  server verifier. The harness, hook, and Streamlit form validate the resulting
  public binding and attestation and add their own fresh regression evidence.
- New invariant checks for the five previously check-less tools:
  `check_bucher` (estimate/SE/CI algebra recomputed from the caller's inputs;
  indirect SE must exceed each direct SE), `check_meta` (CI reconstruction,
  convexity of IV pooling, heterogeneity bounds, dropped-study surfacing),
  `check_randomize` (assignment completeness, count/label consistency, seed
  echo), `check_factorial` (run counts, ±1 orthogonality, resolution =
  shortest defining word), `check_rsm` (CCD/BBD point-type geometry).
- `check_master_config_reserved`: reserved/inert master knobs
  (holm, power_type, rar_eta, shared_control, overdispersion, non-derived
  selection_rule) rejected at the gate layer too.
- incidence_rate direction policy updated: both directions valid (harm
  detection is now analyzed correctly by the engine); harm framing produces an
  advisory note, not a failure.
- No more vacuous passes: missing power fields in ab_test/sample_size and
  partial search-cap exhaustion are surfaced as "PASS (partial)" blocked items;
  a reproducibility re-run that errors reports the real cause.
- New unit suite `agent-harness/tests/test_gates.py`, plus all five
  new checks validated against real dispatcher outputs.

### Harness (`agent-harness/harness.py`, `streamlit_app.py`, `mcp_client.py`)
- **Gate budget counted passes**: after two clean PASSes in one turn, further
  analysis tools ran ungated and were delivered as verified. The retry budget
  now counts consecutive FAILURES only.
- The audit log records the COMBINED verdict (earlier-in-round design-check
  failures were logged as PASS) including blocked/notes.
- Unverified prose containment: text produced after a failed gate streams as a
  distinct `unverified_message` event (rendered visibly blocked in Streamlit),
  and withheld prose is scrubbed from conversation history so a next-turn
  "so what was the n?" cannot parrot it.
- The Streamlit direct form consumes the same server-verified public envelope
  as the agent path and withholds failed results; its incidence-direction
  caption was corrected.
- Reproducibility re-runs are audited and streamed; `MCPClient.start()` after
  `stop()` works (fresh inbox, closed flag reset); seed-injection comment now
  states the truth (model-supplied seed wins).
- System prompt: harm-detection incidence documented as supported; a
  defaults-confirmation line added (state resolved alpha/power/go_target/
  prior/seed before the expensive run).

### MCP server (`mcp-server/`)
- **Silently-stripped parameters closed** (zod strip mode deleted unknown
  keys): `simulate_design` gains `p2_data_ctrl`/`p3_alloc_ratio` (a controlled
  confirmatory design previously degraded to single-arm PPOS with no warning);
  `master_simulate` gains the full documented surface (`phase`,
  `chen_strategy`, `tau_prior`, `futility_boundaries`, `soc_data`, RAR/NCC
  tuning, …) — `phase: "phase3"` previously ran a phase-2 design under a
  phase-3 label. Both config objects are `.strict()`.
- `validate_config`/`sample_size` gain `prior`/`go_threshold`/
  `consider_threshold`/`go_target` so they validate the config submitted.
- Honest effective values: `B_used`/`n_oc_used` echoed (B_oc capped at 5000 in
  the schema), `master_simulate`/`factorial`/`rsm` echo seeds,
  `randomize` echoes `block_size_used`, `meta_analyze` echoes
  `n_input`/`k_used`/`dropped_studies`/`dropped_detail`, `call_filtered`
  returns `ignored_params` instead of dropping keys silently.
- MAIC's working `covariates`/`tte_method` options exposed; Bucher description
  no longer claims an unwired "chain" mode; Box-Behnken described as 3-5
  factors; r-bridge kills the whole process group on timeout (grandchild
  Rscripts no longer survive).

### Suite tooling
- Root `README.md` (capability overview + verification model), `Makefile`
  (five R suites plus the local Python policy/unit matrix), `tools/bootstrap.sh`
  (fresh-checkout setup: toolchain check, npm build, R package check, full
  test run).

### R engines (summary — details in the skill changelogs)
- Harm-detection incidence-rate designs fully supported (direction threaded
  through analyzers/decision/OC/PPOS; OC grid direction-agnostic) — the OC
  curve for a rate-increase design was previously fully inverted.
- Exact-Poisson no-rejection-region fix (actual size could reach 0.78 at
  claimed 0.025 for low-rate designs).
- Umbrella `selection_rule` guard was dead code — now really rejects.
- NULL-means-default handling; cross-design knob rejection; `overdispersion`
  rejected everywhere; platform NCC labels which TTE analysis actually ran.
