# VERA experiment-designing: public R engine

This module provides executable single-endpoint study planning, Bayesian and
frequentist summaries, operating-characteristic simulation, and predictive
probability of success (PPOS). The public agent and the full installation use
these same R source files. The R engine can also run directly without an LLM,
an API key, or another VERA statistical module.

**Availability:** this is the single-endpoint study-design section, and its
selected R core is currently the only statistical module published in this
repository. Binary, continuous, time-to-event, and incidence-rate endpoints are
supported in single-arm and controlled designs, within the models listed below.
See the [repository availability table](../README.md#statistical-code-available-now)
for the other sections and their public-release status.

## Requirements

- R. The release checks were run with R 4.5.3.
- The core engine and examples use packages distributed with R.
- `jsonlite` is required by the agent dispatcher and the 31-check regression
  suite (tested with 2.0.0).
- `Exact` is optional for the Barnard-type Z-pooled binary sensitivity test.
  If unavailable, that test reports `dependency_unavailable`; Fisher and chi-squared
  results remain available. A failed optional calculation reports its failure
  instead of supplying a replacement result.

From the repository root, check the installation and run a small synthetic example:

```sh
Rscript vera-experiment-designing/scripts/R/validate_framework.R
QDF_EXAMPLE_QUICK=1 QDF_OUTPUT_DIR=/tmp/vera-study-example \
  Rscript vera-experiment-designing/examples/study-planning.R
```

The second command runs single-arm, controlled 1:1, and controlled 2:1 binary
examples. Each scenario calls the shared engine and saves CSV tables and PDF
plots in a separate output folder. The scripts resolve modules relative to
their own location, so they can also be invoked by absolute path from another
working directory.

## Supported calculations

| Endpoint | Planning and analysis scope |
| --- | --- |
| Binary | Exact-binomial single-arm sizing; unpooled-Z controlled sizing; Beta-binomial posterior calculations and frequentist sensitivity tests. |
| Continuous | One-sample and equal-allocation two-sample t-based sizing; a labeled normal approximation for unequal allocation; Normal–inverse-gamma posterior calculations. |
| Time to event | Exponential-model planning with accrual and follow-up, event/time summaries, posterior calculations, and simulation. |
| Incidence rate | Poisson-model planning with exposure; exact single-arm calculations and a labeled controlled rate-difference Wald approximation. |

Both single-arm and controlled designs are supported. Configurations identify
the study stage as `signal_detection`, `poc`, or `confirmatory`. Alpha values are
one-sided. The configuration validates endpoint-specific parameters, treatment:control
allocation ratios of at least 1, prior parameters, decision thresholds, and PPOS inputs.

Operating characteristics estimate decision probabilities across specified true
parameter values. PPOS integrates future-study conditional power over posterior
uncertainty from the supplied earlier-study summaries. It is a model-based
assurance calculation, not a claim that a study will succeed. Monte Carlo
diagnostics accompany the main simulation estimates; quick-mode budgets are
intended to check the installation and illustrate outputs.

`tte_method="cox_ph"`, `rate_method="negbin"`, and `overdispersion` are rejected
because those models are not implemented. Binary and continuous configurations
use a higher-is-better alternative; time-to-event configurations use a lower
hazard alternative. Incidence-rate configurations support either rate direction.
The reported method labels distinguish exact, t-based, and normal/Wald
approximations. Configuration validation does not replace choosing an appropriate
model and assumptions for a real study.

## Use the R API

From the repository root:

```r
source("vera-experiment-designing/scripts/R/run_framework.R")

cfg <- create_config(
  endpoint_type = "binary", study_type = "poc", design = "single_arm",
  null_param = 0.30, alt_param = 0.50,
  alphas = 0.025, powers = 0.80, prior = "jeffreys", go_target = 0.40
)

sizes <- compute_sample_size(cfg)
set.seed(42)
results <- run_quantitative_framework(
  cfg, observed_data = list(x = 8, n = 20), B_oc = 1000,
  output_dir = file.path(tempdir(), "vera-analysis")
)
```

`results` retains the configuration, sample-size table, applicable Bayesian and
frequentist results, and operating-characteristic or PPOS results. Saved CSVs
cover sample size, fixed-N power, operating characteristics, and PPOS sensitivity
when applicable; plots are PDFs. Existing files with the same names in the
chosen output directory are replaced on rerun.

For direct PPOS use, supply `p2_data` and `p3_n` to a confirmatory configuration.
Controlled designs also require `p2_data_ctrl`. Use `set.seed()` before PPOS to
reproduce Monte Carlo results. `compute_oc()` accepts its own `seed` argument.
`run_quantitative_framework()` retains its standard defaults of 100,000 PPOS
draws and 50,000 draws per sensitivity point; `n_mc_ppos` and
`n_mc_sensitivity` let callers explicitly choose different budgets. Its OC
budget is capped at 5,000 replicates; use `compute_oc()` directly for a larger
chosen budget.

## Examples and template

All supplied example inputs are illustrative synthetic summaries, not participant
records or validated design recommendations.

| File | Purpose |
| --- | --- |
| `examples/study-planning.R` | Three binary planning scenarios using the same engine. |
| `scripts/R/examples.R` | Six binary/continuous examples including confirmatory PPOS and an alternative prior. |
| `examples/analysis-template.R` | Editable standalone analysis with explicit configuration and output settings. |

Set `QDF_OUTPUT_DIR` to an output directory. Without it, examples write below
`qdf-output/` in the current working directory. Set `QDF_EXAMPLE_QUICK=1` for
small demonstration simulation budgets. Each driver fixes its random seed.
For example:

```sh
QDF_EXAMPLE_QUICK=1 QDF_OUTPUT_DIR=/tmp/vera-worked-examples \
  Rscript vera-experiment-designing/scripts/R/examples.R
```

The template finds `scripts/R` relative to its installed location. If you copy
it elsewhere, set `QDF_FRAMEWORK_DIR` to the absolute path of the published
`scripts/R` directory. No private instructions or reference documents are needed.

The older scenario-specific grid drivers and `v1_sample_size.R` are not part
of the public distribution. The published examples call the six shared core
files instead of maintaining separate statistical implementations.

## Validation

Run from the repository root:

```sh
Rscript vera-experiment-designing/scripts/tests/run_tests.R
Rscript vera-experiment-designing/scripts/tests/public_packaging.R
```

The first command runs 31 regression checks, including one against the public
agent dispatcher. Keep the module in the repository layout for that suite.
The second checks an isolated copy containing only the public core and examples:
execution from another directory, all example scenarios, a relocated template,
CSV reproducibility, and explicit PPOS simulation budgets. It needs only R.
The optional `Exact` integration is exercised when that package is installed;
the suite also tests missing-package and calculation-failure reporting.

## License

The public R core, examples, tests, and this module's documentation are licensed
under GPL-3.0-only; see [LICENSE.txt](LICENSE.txt). This module's license is
separate from the repository's MPL-2.0 agent-layer license. Dependencies retain
their own licenses. Proprietary skill instructions and the other statistical
modules are outside this public release.
