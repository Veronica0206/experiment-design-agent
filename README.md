# Experiment Design Agent

An agent-assisted toolkit for single-endpoint study planning. Statistical
calculations run in R; the language model collects inputs and invokes tools.
Results released through the governed agent/MCP path undergo automated
verification and deterministic reporting.

The **public single-endpoint edition** provides configuration validation,
sample-size calculations, operating-characteristic simulation for Bayesian
GO / CONSIDER / NO-GO decision rules, and supported predictive probability of
success (PPOS) calculations. The direct R API also provides frequentist
sensitivity analyses.

R calculations and worked examples run without a model account or API key.
Natural-language coordination requires a configured model-provider connection.

A separate complete installation adds other statistical domains. Its private
engines and proprietary skill instructions are excluded from the public edition.

## Statistical code available now

**The single-endpoint study-design section (`vera-experiment-designing`) is
currently the only statistical module with R implementation code included in
this public repository.** Its selected [R core](vera-experiment-designing/scripts/R/)
can run directly or through the agent.

| Section / module | Statistical code in this public repository |
| --- | --- |
| Single-endpoint study design — [vera-experiment-designing](vera-experiment-designing/README.md) | **Available now:** selected R core, worked examples, and regression tests |
| Master protocols — `vera-master-experiment-designing` | Not included; separate complete installation |
| DOE / A/B testing — `vera-doe-designing` | Not included; separate complete installation |
| Indirect comparison — `vera-indirect-comparing` | Not included; separate complete installation |
| Meta-analysis — `vera-meta-analyzing` | Not included; separate complete installation |
| Randomization | Not included; separate complete installation |

The available module supports **binary, continuous, time-to-event, and
incidence-rate endpoints**, with single-arm and controlled designs. It includes
configuration validation, sample-size calculation, frequentist and Bayesian
analysis, simulation of Bayesian decision-rule operating characteristics, and
supported PPOS calculations.
The [supported-calculations table](vera-experiment-designing/README.md#supported-calculations)
describes the implemented models and their restrictions.

The direct R API exposes the engine's analysis functions. The public agent
exposes four MCP tools rather than every direct R function:
`validate_config`, `sample_size`,
`simulate_design`, and `run_tests`. It refuses unsupported tool names. Its
coordinator can dispatch only `single-endpoint-designer`.

See the [R engine documentation](vera-experiment-designing/README.md) for supported
endpoints, assumptions, method restrictions, optional packages, and examples.

## Start with the R engine

R calculations and examples run without a model account or API key. The engine
was tested with R 4.5.3; `renv.lock` records the complete development environment.
The agent bridge requires `jsonlite`; `survival` is normally supplied with R.
`Exact` is optional and its absence is surfaced by the relevant method checks.

On **Ubuntu 24.04**, the public CI baseline installs R and the required bridge
package with:

```sh
sudo apt-get update
sudo apt-get install -y --no-install-recommends r-base r-cran-jsonlite
```

Use the appropriate R installation method for other operating systems. For
the governed agent, install packages for the R installation selected by its
runtime: project-local `renv` libraries are not automatically activated.
[MCP interpreter setup](mcp-server/README.md#build-and-installation-prerequisites)
explains the fixed installation locations and absolute `EXPDESIGN_RSCRIPT`
override.

From the repository root, run a small analysis:

```sh
QDF_EXAMPLE_QUICK=1 QDF_OUTPUT_DIR=/tmp/vera-study-example \
  Rscript --vanilla vera-experiment-designing/examples/study-planning.R
```

This runs three synthetic binary study-planning examples: single-arm,
controlled 1:1, and controlled 2:1. Each writes CSV tables and PDF plots in its
own folder below `/tmp/vera-study-example`; rerunning replaces outputs with
the same names. Quick mode uses small simulation budgets for demonstration,
not final study planning. See the other
[worked examples](vera-experiment-designing/README.md#examples-and-template)
and the R-only validation commands below.

## Run the agent

Use **Node.js 24 LTS** with npm, **Python 3.11**, R, Git, and Make. Node 24 is
the recommended and tested public-runtime target; see the official
[Node.js release schedule](https://nodejs.org/en/about/previous-releases).
The full locked Python harness supports 3.10–3.12, exercised locally with 3.12
and in public CI with 3.11. The setup below explicitly selects Python 3.11;
the additional Python 3.14 CI job covers selected contracts, not the full harness.

Use a POSIX shell and start in the repository root. With Node 24 selected in
your shell, bind that same executable for the governed launchers, create the
locked Python environment, and build the MCP server:

```sh
node --version
export EXPDESIGN_NODE="$(node -p 'process.execPath')"
python3.11 -I -E -s -S -B -m venv agent-harness/.venv
agent-harness/.venv/bin/python -I -E -s -B -m pip install --no-compile --require-hashes -r agent-harness/requirements.lock
agent-harness/.venv/bin/python -I -E -s -S -B tools/sanitize_python_environment.py
(
  cd mcp-server
  npm ci --no-audit --no-fund
  npm run build
)
```

The shell remains in the repository root. For the local web interface, configure
your own `ANTHROPIC_API_KEY` in your environment, then run:

```sh
tools/run-reviewed-python.sh -m streamlit run agent-harness/streamlit_app.py --server.headless true --server.address 127.0.0.1 --server.port 8501
```

The interface is unauthenticated and stays bound to localhost. Do not put API
keys in repository files. The bundled MCP tools and automated checks do not
require a model-provider call; natural-language coordination does.

For Claude Code, review the repository before accepting its workspace-trust
prompt. Agent-scoped verification hooks do not run in an untrusted folder;
see [hook verification](hooks/README.md) for how to confirm they ran.
Use the project MCP configuration and run the coordinator as the main agent:

```sh
tools/bootstrap.sh --check-claude-version
claude --agent experiment-design-coordinator
```

The installed-host contract requires Claude Code 2.1.233 or newer and the
provided foreground-mode settings. This version/configuration check does not
exercise an authenticated model conversation.

## Verification

For the **R-only checks**, run all four suites from the repository root:

```sh
Rscript --vanilla vera-experiment-designing/scripts/tests/run_tests.R
Rscript --vanilla vera-experiment-designing/scripts/tests/test_planning_consistency.R
Rscript --vanilla vera-experiment-designing/scripts/tests/test_posterior_contract.R
Rscript --vanilla vera-experiment-designing/scripts/tests/public_packaging.R
```

These cover the core calculations, consistency of planning and PPOS, posterior
probabilities and numerical boundaries, and relocatable examples/templates.

For the **broader public-installation check**, use a clean public clone with
the R, Python, Node, Git, and Make dependencies above installed:

```sh
make public-check
```

This checks the exact public source inventory, public agent permissions,
malformed-profile rejection, R numerical regressions, relocatable examples,
process lifecycle, and real MCP configuration/sample-size/simulation requests.
It also checks regression attestations, replay and canonical reports with the
other statistical engines absent. No model API key is required.

Results released through the **governed MCP/agent path** are bound to the
installed profile and executable source/runtime fingerprints. A missing
required file, failed regression, inconsistent output, or failed required replay
withholds the result. The runtime does not reduce its
required checks because an engine happens to be missing.

Direct R calls use the same statistical implementation but do not automatically
receive MCP verification or canonical reporting.

Reports retain **Partially verified** status where method-boundary checks or
complete assessment of study assumptions still require human review. Passing
software checks does not establish suitability for a particular study.

Reports preserve method labels and uncertainty and distinguish calculation
checks from design performance. See [statistical assurance](docs/STATISTICAL-ASSURANCE.md)
for prior definitions, boundary cases, workload limits and the host trust model.
CI also checks known advisories for locked Python and CRAN packages through OSV,
separately from package hash integrity and the Node dependency audit.

## Public and complete installations

`governance/runtime-profiles.json` selects one of two reviewed profiles:

- `single-endpoint`: the public engine and one domain specialist.
- `complete`: all five engine modules and six domain specialists.

The public exporter selects `single-endpoint` and projects its agent registry
and native coordinator permissions. Editing an environment variable cannot
change the installed profile. The complete working source keeps the `complete`
profile and its full `make release-check` gate, including its separate reviewed
R package-tree attestation. Public CI is not that complete-installation gate.

For release preparation and the explicit file boundary, see
[PUBLIC-RELEASE.md](docs/PUBLIC-RELEASE.md) and [PUBLISHING.md](docs/PUBLISHING.md).

## License

The agent/harness source is under [MPL 2.0](LICENSE), except where otherwise
noted. The explicitly distributed `vera-experiment-designing` R module carries
its existing [GNU GPL version 3 license](vera-experiment-designing/LICENSE.txt).
See [LICENSES.md](LICENSES.md) for the file boundaries and third-party notices.

Only distributed files are included in this release. Proprietary skill
instructions, other engines, private datasets, generated analysis artifacts,
credentials and trademarks are outside its scope.
