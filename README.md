# Experiment Design Agent Suite

A governed local experimental-design suite: it plans studies (sample size,
operating characteristics, adaptive multi-arm designs, indirect comparison,
meta-analysis) and constructs designs (A/B sizing,
factorial/fractional-factorial, response-surface, randomization). On the
governed Claude and Python-harness surfaces, the LLM never computes statistics:
reported values come from seeded R code and are released only through the
implementation-consistency gate. That gate is not an independent statistical
methodology review.

## Components

| Component | Where | What it is |
|---|---|---|
| 5 R skills | `vera-*/` | The statistical engines + SKILL.md workflows (single-endpoint Go/No-Go, basket/umbrella/platform, DoE, Bucher/MAIC, meta-analysis) |
| MCP server | `mcp-server/` | 11 typed tools (TypeScript, stdio) bridging to the R engines via `r-wrapper/dispatcher.R` |
| Agent team | `.claude/agents/` + `.mcp.json` | A routing-only coordinator and six least-privilege domain agents. The original `experiment-designer` remains the legacy all-domain executor; `design-verifier` is a fresh same-engine re-executor, not an independent method audit |
| Governance registry | `governance/agents.json` | Declarative source for agent names, roles, domains, grants, and ledger policy. Python and the hook launcher retain fixed least-privilege domain-tool authorization tables; `validate-config` requires exact three-way parity so registry edits cannot silently expand either runtime |
| Codex skills | `vera-*/agents/openai.yaml` | Explicit-invocation workflows; implicit routing is disabled to prevent overlapping experiment-design triggers |
| Enforcement hooks | `hooks/` | Exact-prompt capture and foreground dispatch binding for the coordinator, plus an agent-scoped synchronous `PostToolBatch` ledger and `Stop` gate (automatically `SubagentStop` when spawned): only exact verified results or value-free terminal failure reports may finish |
| Python harness | `agent-harness/` | Standalone multi-agent coordinator, isolated domain-agent loops, deterministic handoff/fan-in, and Streamlit UI |
| Verification gates | `agent-harness/gates.py` | The server verifier runs the single raw-result tool→checks mapping (`design_checks_for`). The hook, Python harness, and Streamlit form validate the server-bound public envelope and attestation, then require their own fresh regression evidence rather than recomputing checks from private raw output |

## Multi-agent architecture

`experiment-design-coordinator` only classifies the request, asks a structured
clarifying question when routing is unsafe, and dispatches to an allowlisted
subset of six domain agents:

- `single-endpoint-designer`
- `master-protocol-designer`
- `doe-designer`
- `randomization-planner`
- `indirect-comparison-analyst`
- `meta-analysis-analyst`

Each domain agent receives only its registry-declared MCP tools and cannot spawn
another agent. Each child returns a typed, hash-bound handoff. The host validates
those handoffs and joins successful reports in registry order; an LLM does not
paraphrase or numerically synthesize the fan-in. A failed, mixed-clarification,
duplicate, cross-task, or tampered handoff fails closed.

Dependent evidence-to-design work is intentionally not chained in one user
turn. For example, an indirect or meta-analytic estimate is first returned as
verified evidence. Using it as a design input requires a subsequent user turn,
which makes that decision explicit and creates a new governed task boundary.
The native pre-dispatch hook derives this boundary from each child's registry
domain, atomically permits each child at most once per prompt, and still allows
distinct children to fan out within the same phase.
The legacy all-domain executor remains available for compatibility, but it is
not the recommended entry point for new work.

## Quickstart

```bash
# 0. Create the recommended isolated Python application environment from the
#    hash-locked archives
python3 -m venv agent-harness/.venv
agent-harness/.venv/bin/python -m pip install \
  --require-hashes -r agent-harness/requirements.lock

# 1. One-time setup (dependency verification, TypeScript build, R checks, tests)
tools/bootstrap.sh

# Harness-only setup does not require a Claude Code installation/session
tools/bootstrap.sh --harness-only

# Optional: reproduce the lock for standalone R development. The governed MCP
# deliberately ignores project profiles and ambient user-library variables.
Rscript -e 'install.packages("renv"); renv::restore()'

# 2. Complete release check (registry access required; provider login not required)
make release-check # full gate, including dependency audit and Claude Code identity/version
make harness-release-check # MCP/Python harness gate; no Claude install required

# Optional: confirm that Claude Code also has an authenticated live session
make check-claude-live

# 3a. Use from Claude Code: open this folder, approve the project MCP server
#     once when prompted, then run the coordinator:
claude --agent experiment-design-coordinator

# 3b. Use the Streamlit harness (needs ANTHROPIC_API_KEY for chat mode).
#     The UI has no authentication and can read approved input roots and offer
#     private artifact downloads, so bind it to the loopback interface only.
tools/run-reviewed-python.sh -m streamlit run agent-harness/streamlit_app.py \
  --server.headless true --server.address 127.0.0.1 --server.port 8501
```

The bootstrap prefers `agent-harness/.venv/bin/python`, then falls back through
the same closed absolute Python installation list as the MCP runtime (or uses
an explicitly reviewed absolute `EXPDESIGN_PYTHON`). It fails before
building or testing if Python is too old, any resolved lock entry lacks a
SHA-256 hash, or the installed application distribution set differs from the
lock. Only the `pip` and `setuptools` tools bootstrapped by `venv` may appear in
addition to the lock. Every hashable installed distribution file is checked
against its `RECORD` hash and size (only `RECORD` itself may be unhashed), and
unrecorded package-surface files are rejected. All governed Python tests,
validators, and the Streamlit UI run through `tools/run-reviewed-python.sh`.
It starts the selected venv with `-E -s -S -B -I`, validates the environment
using only the standard library and explicit venv metadata paths, and only then
prepends the validated `site-packages` directories for the requested script or
module. Python's `site` initialization never processes `.pth` files: a
distribution-recorded `.pth` is hash/size-bound as inert data, while an
unrecorded `.pth` fails validation. Executable `.pyc`/`.pyo` files,
`__pycache__` directories, and symlinked entries on the suite source import
surface are rejected before the target starts. These
post-install checks establish internal `RECORD` consistency; wheel-archive
provenance is established by creating or recreating the environment with the
documented `pip install --require-hashes` command above, not by reconstructing
an archive hash from installed files. `RECORD` is mutable local metadata, not
an external trust anchor: a same-user rewrite of both a package file and its
`RECORD` is outside this post-install check's proof. The bootstrap never performs an
unpinned global Python install; the error prints the exact hash-enforcing setup
command. `tools/bootstrap.sh --check-python-lock` performs this Python-only
check without requiring Node.

Setup commands require Node 20+, Python 3.9+, and npm. Node is resolved from
the MCP launcher's closed absolute installation list (or absolute
`EXPDESIGN_NODE`); npm must be on the operator's `PATH` for installation.
R is resolved from the same closed absolute installation list as the MCP (or
from absolute `EXPDESIGN_RSCRIPT`) and must provide `jsonlite`, `mvtnorm`, and
`survival` in that installation's default library.
The release profile currently pins R 4.5.3 on
`aarch64-apple-darwin20`; another exact R version/platform or optional-package
set needs its own reviewed integrity profile. Claude Code 2.1.197+ with the
canonical `VERSION (Claude Code)` identity output is required for agent-scoped
hooks, the ledger's `prompt_id` binding,
and the Claude Sonnet 5 agent model.
The Claude readiness probe resolves only the current account's standard
`.local/bin` installation, fixed system installation candidates, or an
explicitly reviewed absolute `EXPDESIGN_CLAUDE` path; it never selects Claude
from ambient `PATH`. Strict product/version output prevents accidental
misidentification, but it is not a cryptographic attestation against a hostile
same-user executable that deliberately impersonates Claude Code.
Neither the MCP launcher nor the security-hook launcher resolves Node through
ambient `PATH`. Both accept a reviewed fixed system location or an explicitly
reviewed absolute `EXPDESIGN_NODE` path; `.mcp.json` invokes its launcher with
absolute `/bin/sh`. Once launched, the MCP runtime likewise resolves Rscript
and Python only from its closed absolute candidate lists (preferring
`agent-harness/.venv/bin/python`). For nonstandard installations, set reviewed
absolute `EXPDESIGN_RSCRIPT` and `EXPDESIGN_PYTHON` paths; relative overrides
are rejected. R profile and library environment variables are cleared for the
governed runtime, which uses only the selected R installation's default
libraries; the release gate validates the required roots, every applicable
transitive dependency, and every installed lock record against `renv.lock`.
It also hashes every regular file in those applicable installed package trees
with bounded, no-follow SHA-256 traversal and compares the exact R
version/platform profile in `governance/r-package-integrity.json`. This detects
same-version file additions, removals, and byte changes after review. It does
**not** authenticate CRAN/source/binary archives: `renv.lock` contains version
and repository records, not archive digests. The profile becomes trustworthy
only when generated from an independently clean, lock-restored and reviewed
installation, then reviewed as a source change.
A project-local `renv::restore()` is therefore for standalone R
development and is not silently activated by the MCP server.
Optional R packages: `Exact` (Barnard sensitivity analysis) and `MAMS`.
Without `MAMS`, umbrella MAMS uses a documented approximation and reports
`boundary_source=approximation`; it is never presented as a package-derived
boundary.

## The verification model (what makes this agent different)

1. **Truth in advertising** — an unimplemented method (`cox_ph`, `negbin`,
   `holm`, inert tuning knobs) errors loudly at three layers (MCP schema →
   R config → gate); nothing silently degrades to a weaker model and nothing
   is reported under a label that did not run.
2. **Identity-safe verification** — every analysis tool result receives a
   `verification_id` plus verifier-local argument/result commitments,
   provenance, public-result, and canonical-report hashes. Inside the server's
   private verifier it then passes `design_checks_for` (config
   completeness/direction, OC direction and power separation, table sanity,
   algebraic invariants for Bucher/meta/DoE tools) plus a regression attestation.
   The hook, Python harness, and Streamlit form do not receive the private raw
   payload and therefore do not rerun those checks; they validate the bound public
   envelope and attestation and require a fresh `run_tests` result. The hook blocks
   the governed agent from finishing otherwise. Completed answers are rendered canonically from the verified
   payload rather than transcribed by the model; the harness withholds
   unverified answers. On the native coordinator surface, a keyed commitment
   also binds every approved foreground child dispatch to the exact current
   user prompt, and the terminal gate permits only the exact child report or
   deterministic registry-order fan-in. Transient prose beside a tool call
   remains prompt-governed because tool hooks do not expose assistant text, but
   it cannot be accepted as the terminal governed report.
3. **Honest partiality** — checks that could not run are reported as
   `PASS_PARTIAL` with the un-checked items listed and a “Partially verified”
   report title, never as a clean pass;
   infeasible sizing (search-cap `n = NA`) is a reportable answer, not an error.
4. **Reproducibility** — each stochastic result is replayed with the same
   arguments and seed and all stable fields are compared. Monte Carlo results
   carry simulation budgets, MCSEs, confidence intervals, and precision flags;
   inadequate precision is explicitly partial. Cost-capped master simulations
   are explicitly partial rather than fully verified.
5. **Containment and privacy** — failed payloads are withheld from the model
   and UI; private row-level outputs and assignments are referenced only by
   opaque, user-audience artifact handles, while absolute paths remain in
   client-only metadata (the audience annotation is a routing hint, not access
   control). The model-visible `public_args_hash` commits only to the approved
   public argument projection; raw argument and result commitments stay inside
   the verifier so private paths, labels, rows, and strata do not become an
   offline equality oracle. Input-file access is restricted to configured roots, and artifact
   provenance, verification, publication, and download reject symlinks,
   non-regular files, and files larger than 50 MiB; audit logs are redacted by
   default, written with private permissions, and retained for at most 30
   days/100 files unless configured otherwise.

## Tests

```bash
make release-check          # complete networked gate; provider login is not required
make check-claude-live      # optional authenticated-session availability check
make harness-release-check  # standalone harness gate; no Claude install required
make test           # all 5 R suites + local Python policy/unit tests
make test-r         # all 5 R regression suites
make test-py        # gates, ledger, harness, client, audit, hook, publish guard
make test-integration # real MCP startup/schema/path/persistence smoke
make validate-skills  # manifests, prompt fixtures, links/paths, and source syntax
make validate-r-lock  # R lock closure + exact-platform installed-tree SHA-256 profile
make validate-python-lock # exact app distributions + installed RECORD integrity
make build       # rebuild the MCP server (dist/)
```

After an independently clean restore into the selected R installation's
default library, a reviewer can emit (but should not blindly bless) a candidate
profile with:

```bash
tools/run-reviewed-python.sh tools/validate_r_environment.py \
  --generate-manifest --acknowledge-reviewed-restore
```

Generation requires that explicit acknowledgement, binds the complete
`renv.lock` bytes, and emits JSON to standard output for separate review. The
normal release gate never regenerates or updates the reviewed profile.

## Publishing

The proprietary source tree is not publishable. The sync command accepts only
a separately prepared, explicitly authorized distribution tree that contains
no plaintext `SKILL.md` or `.skill` bundles:

```bash
EXPDESIGN_PUBLISH_SRC=/path/to/authorized-dist \
EXPDESIGN_PUBLISH_MANIFEST_SHA256=<reviewed-manifest-sha256> \
tools/sync-to-github.sh --dry-run
```

The script requires an exact file/hash manifest plus a separately reviewed
operator pin, refuses
plaintext skill material and symlinks, revalidates the mirrored clone, requires
`make release-check`, verifies the staged Git blobs, and aborts if anything
resembling a credential appears. This prevents accidental publication; it is
not a cryptographic authorization system and does not remediate content that
may already exist on a remote.

`.mcp.json` uses `${CLAUDE_PROJECT_DIR:-.}` for configuration-time portability;
the launchers use the runtime-provided `$CLAUDE_PROJECT_DIR`.

The explicit Codex skill workflows, governed domain agent, and lower-level MCP
tool are related but not capability-identical. The direct skill can orchestrate
a documented chain workflow; the domain agent accepts one Bucher comparison or
one MAIC analysis per delegated task; and the MCP tool accepts 1–1000 independent
Bucher comparisons in one batch or one MAIC analysis. An MCP batch is not a
multi-edge Bucher chain or network meta-analysis. Do not infer MCP support from
a skill's routing description.

See `CHANGELOG.md` for suite-level infra changes and each skill's
`docs/changelog.md` for engine history. The
[dated completeness review](docs/reviews/COMPLETENESS-REVIEW-2026-07-09.md)
tracks what the suite does NOT yet do, with a status header for what has
landed since.
