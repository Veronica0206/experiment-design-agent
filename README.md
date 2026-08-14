# Experiment Design Agent

> **Public portfolio distribution:** This repository intentionally excludes the
> required proprietary statistical engine packages. It documents the agent
> experience and includes the public orchestration, governance, verification,
> and interface code for review, but a fresh clone is not a standalone
> executable agent. MCP analyses and the complete internal release checks
> require an authorized, complete local installation.

Experiment Design Agent is a coordinated team of statistical specialists that
helps researchers plan studies, evaluate complex trial designs, construct
experiments and randomization plans, and synthesize comparative evidence.

You can start with a research question in plain language. A coordinator directs
the request to the most relevant specialist and returns a structured report
that separates assumptions, results, automated checks, reproducibility
information where applicable, and remaining limitations.

## What it helps you do

- Plan single-endpoint studies and estimate the required sample size.
- Simulate operating characteristics, power, and Go/Consider/No-Go behavior.
- Evaluate basket, umbrella, and platform master protocols.
- Size A/B tests and construct factorial or response-surface designs.
- Generate reproducible simple, blocked, or stratified randomization plans.
- Perform Bucher indirect comparisons or matching-adjusted indirect
  comparisons (MAIC).
- Pool evidence across studies with fixed- or random-effects meta-analysis.

## Meet the agent team

The recommended experience uses one coordinator and six specialist sub-agents.
The coordinator selects the narrowest specialist that can answer the question;
when a request contains genuinely independent tasks, it can return more than
one specialist report.

| Agent | What it helps with | Typical outputs |
|---|---|---|
| **Experiment Design Coordinator** (`experiment-design-coordinator`) | Selects the appropriate specialist, requests missing information, and coordinates independent deliverables | Clarifying questions when needed, followed by one or more specialist reports |
| **Single-Endpoint Study Designer** (`single-endpoint-designer`) | Single-arm or controlled binary, continuous, time-to-event, and incidence-rate studies in signal-detection, proof-of-concept, or confirmatory settings | Resolved assumptions, sample-size tables, achieved power, critical values, operating characteristics, and optional predictive probability of success (PPOS) |
| **Master Protocol Designer** (`master-protocol-designer`) | Basket, umbrella, and platform protocols with multiple arms, subgroups, stages, or adaptive decisions | Simulated power and error rates, family-wise error rate, arm or subgroup decisions, sample-size summaries, and downloadable operating-characteristic tables or plots |
| **Design of Experiments Specialist** (`doe-designer`) | Two-arm A/B experiments, full or fractional factorial screening, and response-surface design | Per-arm and total A/B sample size, design matrices, generators, resolution and alias information, central composite designs, or Box–Behnken designs |
| **Randomization Planner** (`randomization-planner`) | Seeded simple, permuted-block, or stratified allocation for two or more arms, including unequal ratios | Allocation summary and a private treatment-assignment artifact |
| **Indirect Comparison Analyst** (`indirect-comparison-analyst`) | One aggregate-data Bucher comparison or one MAIC analysis using individual-level data and published target information | Effect estimate, standard error, confidence interval, resolved method inputs, and effective-sample-size or balance diagnostics where applicable |
| **Meta-Analysis Analyst** (`meta-analysis-analyst`) | Fixed- or random-effects synthesis for binary, continuous, time-to-event, and incidence-rate evidence | Pooled effect and confidence interval, study-level effects, Q, I², tau², and transparent counts of included or excluded studies |

## Example questions

- “How many users per arm do I need to detect a three-percentage-point
  conversion-rate improvement in a two-sided A/B test?”
- “How many participants do I need for a controlled binary-endpoint
  proof-of-concept study?”
- “What are the operating characteristics of this basket trial under the null
  and alternative response scenarios?”
- “Evaluate a two-stage umbrella protocol and report its arm-level power and
  error rates.”
- “Create a Resolution IV fractional-factorial design for six two-level
  factors.”
- “Build a central composite design for studying curvature across three process
  factors.”
- “Generate a reproducible 2:1 stratified randomization plan for 180
  participants.”
- “Estimate treatment A versus treatment C through a common comparator using
  the Bucher method.”
- “Reweight this trial’s individual-level data to the published target
  population using MAIC.”
- “Pool these log hazard ratios with a random-effects model and report
  heterogeneity.”

If essential information is missing—such as the endpoint, design type, null and
alternative assumptions, alpha, power, allocation ratio, analysis method, or
data source—the agent asks for the specific missing fields instead of inventing
them.

## What you receive

Depending on the question, the final report can include:

- The assumptions and effective settings used for the analysis.
- Sample-size, power, operating-characteristic, or evidence-synthesis tables.
- Go/Consider/No-Go probabilities or arm-level decision summaries.
- Factorial or response-surface design matrices.
- Effect estimates, confidence intervals, heterogeneity measures, and
  assumption checks.
- Reproducibility information where available, including the effective random
  seed for stochastic work.
- A verification status and an explicit list of items that still require human
  review.
- Private, opaque artifact handles for sensitive assignments or row-level
  outputs, rather than reproducing those records in conversational text.

## Result assurance and privacy

- Reported values come from statistical analysis engines; the language model
  does not invent numerical results.
- Reproducibility information is reported where available. Same-seed replay is
  applied only to supported stochastic workflows and workload ranges.
- Unsupported, ambiguous, or internally inconsistent requests are rejected or
  clarified rather than silently converted to a nearby method.
- Results are withheld when required checks fail. Currently, all presentable
  statistical-analysis reports are labelled **Partially verified** because
  some endpoint-specific formula checks and complete assessment of the chosen
  configuration still require human review.
- Participant assignments, individual-level input data, identifiers, strata,
  row-level weights, and local file paths are kept out of the narrative report.

These safeguards support consistency, reproducibility, and honest reporting.
They do not establish that a method is scientifically optimal for a particular
program, and they do not replace independent statistical, clinical, ethical,
or regulatory review.

## Current scope and limitations

- The agent supports the method families listed above and does not claim an
  unsupported method under a familiar label.
- The indirect-comparison specialist handles one Bucher comparison or one MAIC
  analysis per request. It does not perform network meta-analysis or a
  multi-edge Bucher chain.
- An estimate produced by indirect comparison or meta-analysis is not
  automatically promoted into a future study’s effect assumption, prior, null,
  alternative, or decision threshold. The user must confirm that use in a new
  step.
- Evidence-analysis requests and prospective study-design requests are handled
  in separate user steps rather than combined into one automatic pipeline.
- Prospective time-to-event study-design calculations use the supported
  exponential model; Cox proportional hazards is not offered there.
- Incidence-rate design calculations use the supported Poisson model;
  negative-binomial design calculations are not currently offered.
- Master-protocol analyses evaluate a specified basket, umbrella, or platform
  design. They are not an unrestricted automatic protocol optimizer.
- Monte Carlo precision limits or workload caps may add further limitations to
  the report.

## Availability

This public repository supports portfolio and source review of the distributed
agent surface. A successful build or public-tree check does not establish that
the omitted statistical engines are installed, that analyses can run, or that
the complete internal release suite has passed. Full execution is available
only through an authorized local installation containing those engines.

In a public clone, the intentionally limited check is:

```bash
make public-check
```

It validates the public path policy, installs and compiles the distributed MCP
interface, and confirms that the npm package remains non-publishable. It does
not execute or attest the omitted statistical engines.

This project is a research and design-support tool. Final study decisions remain
the responsibility of qualified domain experts.

## License

Except where otherwise noted, the files distributed in this public repository
are licensed under the [Mozilla Public License 2.0](LICENSE) (`MPL-2.0`).
Copyright 2026 Veronica Liu.

This license applies only to files actually distributed in this public
repository. Proprietary statistical skill packages, private datasets,
credentials, private generated analysis artifacts not present in this
repository, and trademarks are not included in this distribution or licensed
by it. Third-party dependencies remain subject to their own license terms.
