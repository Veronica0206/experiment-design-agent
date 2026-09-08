# Experiment Design Agent

A runnable agent for single-endpoint study planning, backed by an open R
statistical engine and enforced result verification.

The **public single-endpoint edition** includes configuration validation,
sample-size calculations, frequentist and Bayesian operating characteristics,
and supported predictive probability of success (PPOS) calculations. Numerical
results come from R; the language model requests inputs and invokes tools.

A separate complete installation adds other statistical domains. Its private
engines and proprietary skill instructions are excluded from the public edition.

## Public capabilities

| Capability | Public edition |
|---|---|
| Validate study assumptions and resolve supported configuration | Included |
| Single-arm and controlled single-endpoint sample size | Included |
| Frequentist/Bayesian operating characteristics and supported PPOS | Included |
| Reproducible worked examples, R regression tests and agent verification | Included |
| Master protocols, DOE/A/B tools, randomization, indirect comparison, meta-analysis | Separate complete installation |

The public MCP server advertises exactly `validate_config`, `sample_size`,
`simulate_design`, and `run_tests`. It refuses unsupported tool names. Its
coordinator can dispatch only `single-endpoint-designer`.

See the [R engine documentation](vera-experiment-designing/README.md) for supported
endpoints, assumptions, method restrictions, optional packages, and examples.

## Start with the R engine

R calculations and examples run without a model account or API key. The engine
was tested with R 4.5.3; `renv.lock` records the complete development environment.
The agent bridge requires `jsonlite`; `survival` is normally supplied with R.
`Exact` is optional and its absence is surfaced by the relevant method checks.

From the repository root, after installing these R dependencies:

```sh
Rscript --vanilla vera-experiment-designing/scripts/tests/run_tests.R
Rscript --vanilla vera-experiment-designing/scripts/tests/public_packaging.R
```

The [worked examples](vera-experiment-designing/README.md#examples) use the same
six-file engine as the agent. The quick demonstration mode reduces simulation
budgets and is not a study-planning recommendation.

## Run the agent

Install Node.js 20 or newer and Python 3.9–3.12 (the locked dependency set is
exercised locally with 3.9 and in public CI with 3.11). Create the locked Python
environment and build the MCP server:

```sh
python3 -m venv agent-harness/.venv
agent-harness/.venv/bin/python -I -E -s -B -m pip install --no-compile --require-hashes -r agent-harness/requirements.lock
cd mcp-server
npm ci --no-audit --no-fund
npm run build
```

Return to the repository root. For the local web interface, configure your own
`ANTHROPIC_API_KEY` in your environment, then run:

```sh
tools/run-reviewed-python.sh -m streamlit run agent-harness/streamlit_app.py --server.headless true --server.address 127.0.0.1 --server.port 8501
```

The interface is unauthenticated and stays bound to localhost. Do not put API
keys in repository files. The bundled MCP tools and automated checks do not
require a model-provider call; natural-language coordination does.

For Claude Code, use the project MCP configuration and run the coordinator as
the main agent:

```sh
tools/bootstrap.sh --check-claude-version
claude --agent experiment-design-coordinator
```

The installed-host contract requires Claude Code 2.1.233 or newer and the
provided foreground-mode settings. This version/configuration check does not
exercise an authenticated model conversation.

## Verification

From a clean public clone with the dependencies above installed:

```sh
make public-check
```

This checks the exact public source inventory, public agent permissions,
malformed-profile rejection, R numerical regressions, relocatable examples,
process lifecycle, and real MCP configuration/sample-size/simulation requests.
It also checks regression attestations, replay and canonical reports with the
other statistical engines absent. No model API key is required.

Each result is bound to the installed profile and executable source/runtime
fingerprints. A missing required file, failed regression, inconsistent output,
or failed required replay withholds the result. The runtime does not reduce its
required checks because an engine happens to be missing.

Reports retain **Partially verified** status where method-boundary checks or
complete assessment of study assumptions still require human review. Passing
software checks does not establish suitability for a particular study.

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
