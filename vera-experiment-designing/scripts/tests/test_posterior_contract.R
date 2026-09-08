#!/usr/bin/env Rscript
# Numerical reproductions for objective priors, posterior ratios and OC decisions.
script <- sub("^--file=", "", grep("^--file=", commandArgs(FALSE), value = TRUE)[1])
if (!file.exists(script)) script <- gsub("~+~", " ", script, fixed = TRUE)
r_dir <- normalizePath(file.path(dirname(script), "..", "R"), mustWork = TRUE)
source(file.path(r_dir, "config.R"))
source(file.path(r_dir, "bayesian.R"))
passed <- failed <- 0L
test <- function(name, expression) {
  tryCatch({
    force(expression)
    passed <<- passed + 1L
    cat("TEST", name, ": PASS\n")
  }, error = function(error) {
    failed <<- failed + 1L
    cat("TEST", name, ": FAIL", conditionMessage(error), "\n")
  })
}
fails <- function(expression) inherits(tryCatch({force(expression); NULL}, error = identity), "error")

test("review_continuous_prior_counterexample_reproduced", {
  old <- function(multiplier) bayes_continuous_single_arm(
    .2 * multiplier, .04 * multiplier^2, 10, .1 * multiplier,
    mu0 = 0, kappa0 = .01, alpha0 = .5, beta0 = .5)$prob_above_target
  stopifnot(abs(old(1) - .80576) < 1e-5, abs(old(100) - .94552) < 1e-5,
            classify_decision(old(1))$decision == "CONSIDER",
            classify_decision(old(100))$decision == "GO")
})
test("joint_jeffreys_matches_analytic_student_posterior", {
  prior <- resolve_prior("jeffreys", "continuous", 0)
  posterior <- do.call(nig_update, c(list(x_bar = .2, s2 = .04, n = 10), prior))
  stopifnot(posterior$df == 10, posterior$mu_n == .2,
            abs(posterior$scale - .06) < 1e-15,
            abs(bayes_continuous_single_arm(.2, .04, 10, .1)$prob_above_target -
                  pt((.2 - .1)/.06, 10)) < 1e-15)
})
test("joint_jeffreys_decision_invariant_to_units_and_origin", {
  reference <- bayes_continuous_single_arm(.2, .04, 10, .1)$prob_above_target
  for (multiplier in c(.001, 1, 100)) for (origin in c(0, 32)) {
    current <- bayes_continuous_single_arm(origin + multiplier * .2,
      multiplier^2 * .04, 10, origin + multiplier * .1)$prob_above_target
    config <- create_config("continuous", "poc", "single_arm", origin, origin + multiplier*.2,
                            sd = multiplier*.2, go_target = origin + multiplier*.1)
    decision <- compute_decision(config, list(x_bar = origin + multiplier*.2, s2 = multiplier^2*.04, n = 10))
    stopifnot(config$prior_method == "normal_jeffreys_joint",
              abs(decision$posterior_prob - reference) < 1e-10, abs(current - reference) < 1e-10,
              classify_decision(current)$decision == classify_decision(reference)$decision)
  }
})
test("proper_nig_prior_transforms_with_measurement_units", {
  baseline <- bayes_continuous_single_arm(.2, .04, 10, .1,
    mu0 = .05, kappa0 = .2, alpha0 = 2, beta0 = .3)$prob_above_target
  transformed <- bayes_continuous_single_arm(52, 400, 10, 42,
    mu0 = 37, kappa0 = .2, alpha0 = 2, beta0 = 3000)$prob_above_target
  stopifnot(abs(baseline - transformed) < 1e-12)
})
test("improper_normal_posterior_is_explicitly_rejected", {
  stopifnot(fails(nig_update(1, 0, 10)), fails(nig_update(1, 1, 1)),
            is.finite(nig_update(1, 0, 10, kappa0 = .01, alpha0 = .5, beta0 = .5)$scale),
            fails(resolve_prior(list(mu0 = 0, kappa0 = 0, alpha0 = 0, beta0 = 0), "continuous", 0)))
})
test("gamma_ratio_nonexistent_mean_has_valid_quantiles", {
  result <- gamma_ratio_summary(.5, 10, .5, 10)
  stopifnot(is.na(result$mean), result$mean_status == "does_not_exist",
            abs(result$median - 1) < 1e-12, all(is.finite(result$ci)),
            abs(result$probability - .5) < 1e-14,
            abs(prod(result$ci) - 1) < 1e-10,
            is.na(gamma_ratio_summary(2, 10, 1, 10)$mean))
})
test("gamma_ratio_finite_mean_matches_inverse_gamma_moment", {
  result <- gamma_ratio_summary(3, 2, 4, 5, 2)
  stopifnot(result$mean_status == "finite", abs(result$mean - 2.5) < 1e-12,
            abs(result$probability - pf(2 / ((3/4)*(5/2)), 6, 8)) < 1e-12)
})
test("gamma_ratio_summaries_are_invariant_to_exposure_units", {
  baseline <- gamma_ratio_summary(.5, 12, 2.5, 14)
  rescaled <- gamma_ratio_summary(.5, 1200, 2.5, 1400)
  stopifnot(max(abs(c(baseline$mean, baseline$median, baseline$ci, baseline$probability) -
                        c(rescaled$mean, rescaled$median, rescaled$ci, rescaled$probability))) < 1e-12)
})
test("controlled_gamma_decisions_report_undefined_ratio_means", {
  hazard <- bayes_tte_two_arm(1, 10, 0, 10)
  rate <- bayes_rate_two_arm(1, 10, 0, 10)
  stopifnot(is.na(hazard$hr_mean), hazard$hr_mean_status == "does_not_exist",
            is.na(rate$rr_mean), rate$rr_mean_status == "does_not_exist",
            identical(hazard$hr_ci, rate$rr_ci),
            gamma_ratio_probability(.5, 1, .5, 1, 0)$probability == 0)
})
test("beta_difference_probability_matches_independent_density_integral", {
  result <- beta_difference_probability(4.5, 6.5, 2.5, 8.5, .15)
  oracle <- integrate(function(x) dbeta(x, 2.5, 8.5) *
    pbeta(x + .15, 4.5, 6.5, lower.tail = FALSE), 0, .85, abs.tol = 1e-11)$value
  stopifnot(abs(result$probability - oracle) < 2e-8,
            result$abs_error <= QDF_POSTERIOR_ABS_TOL,
            result$evaluations <= QDF_POSTERIOR_MAX_EVALUATIONS)
})
test("student_difference_probability_matches_independent_density_integral", {
  treatment <- nig_update(.2, .04, 10)
  control <- nig_update(0, .05, 12)
  result <- student_difference_probability(treatment, control, .1)
  oracle <- integrate(function(z) dt(z, control$df) *
    pt((control$mu_n + control$scale*z + .1-treatment$mu_n)/treatment$scale,
       treatment$df, lower.tail = FALSE), -Inf, Inf, abs.tol = 1e-10)$value
  stopifnot(abs(result$probability - oracle) < 2e-8)
})
test("quadrature_threshold_ambiguity_fails_without_false_certainty", {
  stopifnot(fails(.qdf_integrate_probability(function(u) rep(.9, length(u)), .9)))
})
test("asymmetric_posterior_quadrature_resolves_tail_transition", {
  # For uniform control, P(T>C) = E[T], including a concentrated treatment.
  beta <- beta_difference_probability(999000000, 1000000, 1, 1, thresholds = .9995)
  reverse <- beta_difference_probability(1, 1, 999000000, 1000000)
  stopifnot(abs(beta$probability - .999) < 1e-9,
            abs(reverse$probability - .001) < 1e-9)
  control <- list(mu_n = 0, scale = 1, df = 10)
  treatment <- list(mu_n = qt(.999, 10), scale = 1e-10, df = 10)
  student <- student_difference_probability(treatment, control, thresholds = .9995)
  reverse <- student_difference_probability(control, treatment)
  stopifnot(abs(student$probability - .999) < 1e-9,
            abs(reverse$probability - .001) < 1e-9)
})
test("decision_dispatch_uses_deterministic_controlled_probability", {
  configs <- list(create_config("binary", "poc", "controlled", .2, .4),
                  create_config("continuous", "poc", "controlled", 0, .5, sd = 1))
  observed <- list(list(x_trt = 8, n_trt = 10, x_ctrl = 3, n_ctrl = 10),
                   list(x_bar_trt = .8, s2_trt = .4, n_trt = 10,
                        x_bar_ctrl = .1, s2_ctrl = .5, n_ctrl = 10))
  for (index in seq_along(configs)) {
    set.seed(1); first <- compute_decision(configs[[index]], observed[[index]])
    set.seed(222); second <- compute_decision(configs[[index]], observed[[index]])
    stopifnot(identical(first$posterior_prob, second$posterior_prob),
              identical(first$decision, second$decision),
              first$probability_method == "adaptive_quadrature")
  }
})
test("default_oc_grids_include_exact_null_and_alternative", {
  configs <- list(
    create_config("binary", "poc", "single_arm", .21, .29),
    create_config("continuous", "poc", "single_arm", .21, .29, sd = 1),
    create_config("tte", "poc", "single_arm", .29, .21, accrual_time = 1, followup_time = 1),
    create_config("incidence_rate", "poc", "single_arm", .002, 0, exposure_time = 1))
  for (config in configs) {
    grid <- default_oc_grid(config)
    stopifnot(config$null_param %in% grid, config$alt_param %in% grid,
              !anyDuplicated(grid), all(is.finite(grid)))
  }
})
test("invalid_oc_grids_and_ignored_delta_are_rejected", {
  config <- create_config("binary", "poc", "single_arm", .21, .29)
  for (grid in list(numeric(0), c(.2, NaN), c(-.1, .2), c(.2, 1.1), rep(.2, 65)))
    stopifnot(fails(compute_oc(config, 10, grid, B = 1)))
  stopifnot(fails(compute_oc(config, 10, B = 1, delta = .1)))
})
test("controlled_oc_uses_no_inner_posterior_random_draws", {
  local({
    for (name in c("rbeta", "rt", "rgamma"))
      assign(name, function(...) stop("unexpected posterior random draw"), .GlobalEnv)
    on.exit(rm(list = c("rbeta", "rt", "rgamma"), envir = .GlobalEnv), add = TRUE)
    configs <- list(
      create_config("binary", "poc", "controlled", .21, .29),
      create_config("continuous", "poc", "controlled", 0, .5, sd = 1),
      create_config("tte", "poc", "controlled", .2, .1, accrual_time = 1, followup_time = 1),
      create_config("incidence_rate", "poc", "controlled", .2, .1, exposure_time = 1))
    for (config in configs) {
      result <- compute_oc(config, list(n_trt = 10, n_ctrl = 10),
                           c(config$null_param, config$alt_param), B = 4)
      stopifnot(all(result$inner_precision_ok),
                all(result$inner_probability_abs_error_max <= result$inner_probability_tolerance),
                all(result$inner_probability_evaluations_max <= result$inner_probability_max_evaluations))
    }
  })
})
test("oc_anchor_counterexample_replays_exactly_with_diagnostics", {
  config <- create_config("binary", "poc", "single_arm", .21, .29)
  result <- compute_oc(config, 225, B = 16, seed = 17)
  stopifnot(.21 %in% result$true_param, .29 %in% result$true_param,
            identical(result, compute_oc(config, 225, B = 16, seed = 17)),
            all(result$inner_probability_method == "analytic_beta_cdf"))
})
cat("\n--- Results:", passed, "passed,", failed, "failed ---\n")
if (failed) quit(status = 1)
