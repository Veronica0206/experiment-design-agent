# Statistical assurance for the public engine

This document describes the statistical contracts of the public single-endpoint
engine and the corrections tracked as R1–R7. It distinguishes reproducible
calculation from the separate judgment that a proposed study is scientifically
appropriate. The public MCP profile exposes `validate_config`, `sample_size`,
`simulate_design`, and `run_tests`.

## What an assurance result establishes

| Dimension | Evidence and limits |
| --- | --- |
| Calculation | Regression checks, output validation, current method labels, valid numerical ranges, and explicit precision diagnostics support the implemented calculation under its stated model. |
| Replay | Recomputing the same inputs and seed with the bound engine and dependencies checks reproducibility. It can reproduce a misspecified model or an unattractive design. |
| Assumptions | Endpoint, direction, allocation, variance, prior, censoring, exposure, and confirmatory-test assumptions need substantive review. Configuration validation checks supported inputs, not their empirical truth. |
| Design performance | OC probabilities at the exact null and alternative describe the proposed decision rule. Poor separation or low probability of Go remains reportable and should prompt design review. Repeating seeds until the result looks favorable is invalid. |
| Live provider | Deterministic statistical/MCP checks make no authenticated model-provider call. They do not establish end-to-end behavior of a live model conversation. |

The runtime assumes a trusted host, interpreter, installed dependencies, and
governed source files. Caller-selected data and other user files are inputs whose
contents and provenance require appropriate review. Runtime fingerprints,
environment checks, process cleanup, output projection, and replay provide
specific controls; they do not create an operating-system sandbox against a
hostile local user, administrator, interpreter, dependency, or modified runtime.

## Result and method contracts — R1, R3, R7

`sample_size` and `simulate_design` emit `result_contract_version: 1`, bound to
`governance/single-endpoint-result-contract.json`. The public report preserves
the approved scientific fields, including method identifiers, achieved power,
replicate counts, uncertainty intervals, precision status, and ratio-mean status.
Missing required fields or unknown method names fail the scientific result
contract. Raw posterior draws and arbitrary labels are excluded from the public
report.

All planning alpha values are one-sided. Sample-size rows distinguish
`power_target` from `power_achieved`. Achieved power is recomputed at the returned
integer arm sizes. Continuous designs require at least two participants in each
arm. Bounded searches report failure explicitly; an R result with
`sizing_status: search_limit_reached` is not an achieved design and cannot pass
the public achieved-design gate.

| Calculation | Current method and interpretation |
| --- | --- |
| Single-arm binary sizing/PPOS | `exact_binomial`; a discrete rejection threshold is used. |
| Controlled binary sizing/PPOS | `z_unpooled`; both use the same unpooled normal approximation, with standard error `sqrt(pT*(1-pT)/nT + pC*(1-pC)/nC)`. This is an approximation to the Wald test, not an exact finite-sample calibration. |
| Continuous sizing | `one_sample_t` for a single arm; `two_sample_t` for equal final arm sizes; `two_sample_z_normal_approximation` for unequal final arm sizes. The search evaluates the actual rounded allocation. |
| Continuous PPOS | `one_sample_noncentral_t` or `welch_noncentral_t_approximation`; the latter accounts for posterior variance draws using a Welch approximation. Its assumptions differ from equal-variance planning. |
| Time-to-event planning/PPOS | Exponential/event-information approximations. The `logrank` sizing label does not establish validity under arbitrary nonproportional hazards. The public model uses constant hazards, uniform accrual, and administrative follow-up. |
| Incidence-rate sizing/PPOS | `exact_poisson` for a single arm and `poisson_rate_difference_wald` for controlled designs. Controlled Bayesian Go decisions use a rate-ratio target; this is a separate decision rule from the confirmatory rate-difference test. |

The controlled binary review example (`pC=.1`, `pT=.3`, allocation `2:1`,
alpha `.025`, target power `.8`) returns `78/39`. The shared conditional-power
formula gives approximately `.80743`; the former inconsistent pooled cutoff
gave approximately `.70169` for those same design parameters.

## Noncentral-t tails — R4

Large noncentrality alone no longer forces conditional power to zero or one.
For `df=1`, one-sided alpha `.001`, and `ncp=40`, the correct upper-tail
probability is approximately `.1000017`, despite the large noncentrality.

The helper uses R's noncentral-t implementation within its documented range
when it returns without warning. Otherwise it integrates the defining
representation `T=(Z+ncp)/sqrt(V/df)`, with independent standard-normal `Z`
and chi-square `V`. Integration truncates `Z` to `[-10,10]`, omitting less
than `1.6e-23` normal probability, and checks quadrature convergence. Failure
is explicit. R documents the `|ncp| <= 37.62` accuracy range and possible
upper-tail cancellation. See the official
[Student t distribution documentation](https://stat.ethz.ch/R-manual/R-devel/library/stats/html/TDist.html).

## Priors and units — R5

For continuous data, `prior="jeffreys"` now means the **joint Jeffreys prior**
for unknown normal mean and variance:

```text
p(mu, v) proportional to v^(-3/2), where v = sigma^2
mu | data = x_bar + sqrt((n-1)*s2)/n * Student_t(df=n)
```

`prior_method` identifies it as `normal_jeffreys_joint`. The resolved
`kappa0`, `alpha0`, and `beta0` are exactly zero; this named improper-prior
boundary is accepted only when the posterior is proper. The implementation
requires `n >= 2` and positive sample variance. It errors for zero variance.
The joint construction has `df=n`; the distinct independence/reference
construction has `df=n-1`. This distinction is explained in the
[Cambridge prior-distribution lecture, slides 6-6 and 6-17](https://www.statslab.cam.ac.uk/Dept/People/djsteaching/2009/ABS-lect6-09.pdf).

The review example (`n=10`, mean `.2`, variance `.04`, target `.1`) now has
posterior probability approximately `.9367263`, unchanged when measurements
and target are multiplied by `100` and variance by `10000`. Tests also cover
changes of origin. This corrects the previous unit-dependent proper prior
that could change a decision from Consider to Go after rescaling.

Other named presets retain their explicit parameters in `prior_params`:

| Endpoint | `jeffreys` | `flat` | `skeptical` |
| --- | --- | --- | --- |
| Binary | Beta(.5,.5) | Beta(1,1) | Beta(5*p0,5*(1-p0)) |
| Continuous | Joint construction above | NIG(mu0=0,kappa0=.001,alpha0=.001,beta0=.001) | NIG(mu0=p0,kappa0=1,alpha0=1,beta0=1) |
| TTE/incidence rate | Gamma(shape=.5,rate=1e-6) | Gamma(shape=.001,rate=.001) | Gamma(shape=2,rate=2/p0) |

The continuous `flat` preset is a proper NIG distribution; its name does not
mean a uniform density. The Gamma `jeffreys` preset is a proper regularized
preset with a positive rate, not an exactly invariant improper prior. These
proper presets are unit-aware. For equivalent custom NIG priors under
`y'=a+c*y`, `c>0`, transform `mu0'=a+c*mu0`, `beta0'=c^2*beta0`, leaving
`kappa0` and `alpha0` unchanged. If exposure is multiplied by `c`, multiply a
custom Gamma rate hyperparameter by `c` as well. Custom proper priors are
identified as `custom_proper`; custom zero/negative shape or scale parameters
are rejected. Prior choice and sensitivity remain substantive assumptions.

## Posterior ratio summaries — R6

For independent treatment and control Gamma posteriors in shape/rate form,
the treatment/control ratio has a scaled beta-prime distribution. Its mean is

```text
shapeT * rateC / (rateT * (shapeC - 1)), only when shapeC > 1.
```

When the denominator shape is at most one, the positive ratio has no finite
mean. The engine returns an unavailable mean (`NA` in R, `null` in JSON) and
`mean_status="does_not_exist"`; it never substitutes a finite sample average.
The status is `finite` when the analytic mean exists. The median and equal-tail
95% credible interval are computed from the scaled F distribution, and the
target probability from a Beta CDF. The relation is supported by R's official
[F distribution documentation](https://stat.ethz.ch/R-manual/R-devel/library/stats/html/Fdist.html).

Standalone decisions use `hr_mean`/`rr_mean` and their corresponding status,
median, and interval fields. PPOS uses `hr_post_mean`/`rr_post_mean`,
`hr_post_mean_status`/`rr_post_mean_status`, `hr_post_median`/`rr_post_median`,
`hr_post_ci`/`rr_post_ci`, and `ratio_summary_method="scaled_beta_prime"`.
PPOS itself still averages conditional power over posterior draws; an undefined
ratio mean does not imply undefined PPOS.

## OC scenarios and two sources of numerical uncertainty — R2

Every default operating-characteristic grid includes the exact configured null
and alternative, including the binary `.21/.29` review example. Grids are
finite, sorted, deduplicated, and domain-validated before simulation; explicit
R grids accept 1–64 values. Binary probabilities lie in `[0,1]`, hazards are
positive, and incidence rates are nonnegative. Controlled binary/continuous
`delta` defaults to `go_target-null_param`; an explicit value overrides it.
Supplying `delta` where it has no defined effect is rejected.

Outer simulation reports Go, Consider, and No-Go probabilities with Monte Carlo
standard errors, 95% Wilson intervals, replicate counts, and a worst-case
standard error `0.5/sqrt(B)`. `mc_precision_ok` compares that worst-case error
with `.02`; observed zero or one does not manufacture a precision pass.

Controlled binary/continuous posterior decision probabilities now use
deterministic quadrature rather than an inner sample of 5,000 posterior draws.
The integration averages the wider posterior's CDF over the narrower posterior
to resolve sharply concentrated distributions. Its absolute-error tolerance
is `1e-8`; it may refine to `1e-11` near a decision threshold. At most 8,192
integrand evaluations are allowed in total across both attempts. An unresolved
threshold or numerical failure produces an error. Analytic shortcuts and the
single-arm/Gamma CDF paths avoid quadrature.

Every OC row carries `inner_probability_method`,
`inner_probability_abs_error_max`, `inner_probability_evaluations_max`,
`inner_probability_tolerance`, `inner_probability_max_evaluations`, and
`inner_precision_ok`. The maxima summarize the entire OC call and are repeated
on its rows. A zero error on an analytic path means there is no quadrature
error estimate; it does not claim exact floating-point arithmetic.
QUADPACK's error estimate is a diagnostic, not a proven mathematical bound.
R explicitly describes this limitation in its
[integration documentation](https://stat.ethz.ch/R-manual/R-devel/library/stats/html/integrate.html).

Standalone controlled binary/continuous posterior medians and credible
intervals still use posterior draws; their decision probabilities are
deterministic. PPOS retains its own Monte Carlo diagnostics. Neither outer MC
precision nor quadrature diagnostics account for model misspecification.

## Work bounds and reproducible benchmarks

The MCP dispatcher uses the same deduplicated grid for workload admission and
simulation. It caps effective `B_oc` at 5,000, simulated units per scenario at
10,000,000, simulated units across scenarios at 100,000,000, and the OC
posterior-evaluation upper bound at 1,000,000,000. Controlled binary/continuous
work uses the conservative 8,192-evaluation bound per simulated decision;
other endpoints use one probability evaluation. The returned `workload` records
the scenario count and these budgets. They describe OC work, not a prediction
of elapsed time or a total budget for sample-size search, PPOS, and replay.
Direct R calls are not subject to all MCP admission caps.

One local run on 2026-09-08 used R 4.5.3, platform
`aarch64-apple-darwin20`. Both cases used 40 participants per arm, `B=5000`,
seed `812`, the named `jeffreys` prior, Go threshold `.9`, Consider threshold
`.6`, and the default midpoint Go target:

| Endpoint and inputs | Scenarios | Elapsed seconds | Maximum inner evaluations | Maximum estimated absolute error |
| --- | ---: | ---: | ---: | ---: |
| Binary, null `.2`, alternative `.4`, target `.3` | 19 | 0.879 | 651 | 9.973562e-9 |
| Continuous, null `0`, alternative `.5`, SD `1`, target `.25` | 22 | 27.286 | 777 | 9.999956e-9 |

These are single R timings, excluding MCP startup, sample-size search, PPOS,
report generation, and verification replay. They are not Ubuntu measurements
or a latency guarantee. Replay performs additional computation. Binary OC can
reuse probabilities for repeated observed count pairs through a bounded cache.

A separate public MCP measurement on the same host used the same two endpoint
configurations, `n_oc={n_trt:40,n_ctrl:40}`, `B_oc=5000`, and `seed=812`.
Per-request time after connection was 8.733 seconds for binary and 60.378 seconds
for continuous. Both returned a presentable partial result, a passing regression
attestation, and a passing same-seed replay. These timings include calculation,
runtime checks, attestation, replay and reporting; they exclude connection
startup and any provider call. Other validation was running on the host, and
these single observations are not latency guarantees. Clients must allow
sufficient time for both the calculation and its replay; this measurement used
a 180-second request timeout. The public MCP smoke test prints comparable
per-request timings for its smaller regression inputs.

Run from the repository root to reproduce the benchmark inputs:

```sh
Rscript --vanilla - <<'RS'
source("vera-experiment-designing/scripts/R/config.R")
source("vera-experiment-designing/scripts/R/bayesian.R")
cat(R.version.string, "\n", R.version$platform, "\n")
for (endpoint in c("binary", "continuous")) {
  cfg <- if (endpoint == "binary") {
    create_config(endpoint, "poc", "controlled", .2, .4)
  } else {
    create_config(endpoint, "poc", "controlled", 0, .5, sd = 1)
  }
  elapsed <- system.time(out <- compute_oc(
    cfg, list(n_trt = 40, n_ctrl = 40), B = 5000, seed = 812
  ))["elapsed"]
  cat(endpoint, "scenarios", nrow(out), "seconds", elapsed,
      "max_evaluations", max(out$inner_probability_evaluations_max),
      "max_error", max(out$inner_probability_abs_error_max), "\n")
}
RS
```

The focused regression commands are:

```sh
tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/test_planning_consistency.R
tools/run-reviewed-r.sh vera-experiment-designing/scripts/tests/test_posterior_contract.R
```

They cover the supplied counterexamples, independent distribution/integration
oracles, unit transformations, ratio-moment existence, integer sizing, exact OC
anchors, threshold ambiguity, and absence of inner OC posterior random draws.
The reviewed run recorded 10 planning checks and 18 posterior checks passing
with no failures. The complete public distribution additionally runs
`make public-check` in a clean prepared clone, including numerical regression,
scientific output-contract checks, runtime lifecycle, and actual MCP
regression/replay. A passing local check alone does not establish publication
status, CI success, or live provider behavior.
