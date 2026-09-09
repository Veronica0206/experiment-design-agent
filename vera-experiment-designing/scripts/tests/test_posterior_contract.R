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
test("gamma_ratio_saturated_boundary_preserves_both_beta_tails", {
  # Independently high-precision checked counterexample from the follow-up
  # review: the old logistic boundary rounded to one and falsely implied GO.
  lower <- gamma_ratio_probability(.01, 1e18, .01, 1)$probability
  upper <- gamma_ratio_probability(.01, 1e18, .01, 1, lower.tail = FALSE)$probability
  stopifnot(abs(lower - .6695997136756292) < 2e-14,
            abs(upper - .3304002863243708) < 2e-14,
            abs(lower + upper - 1) < 2e-15,
            classify_decision(lower)$decision == "CONSIDER")
  # Asymmetric high-precision references also expose an incorrect shape swap
  # that equal-shape tests would not catch.
  asymmetric_upper <- gamma_ratio_probability(.01,1e18,.3,1,lower.tail=FALSE)$probability
  asymmetric_lower <- gamma_ratio_probability(.01,1e-18,.3,1)$probability
  stopifnot(abs(asymmetric_upper/1.2894346381216462e-7-1) < 1e-13,
            abs(asymmetric_lower-.6419786530269519) < 2e-14)
})
test("gamma_ratio_reciprocal_small_shapes_preserve_tail_identity", {
  for (shapes in list(c(.01, .01), c(.01, .03), c(.03, .01))) {
    for (rate_ratio in c(1e-18, 1, 1e18)) for (target in c(.25, 1, 4)) {
      for (lower_tail in c(TRUE, FALSE)) {
        direct <- gamma_ratio_probability(shapes[1], rate_ratio, shapes[2], 1,
                                            target, lower_tail)$probability
        inverse <- gamma_ratio_probability(shapes[2], 1, shapes[1], rate_ratio,
                                             1/target, !lower_tail)$probability
        stopifnot(direct > 0, direct < 1, abs(direct-inverse) < 2e-14)
      }
    }
  }
})
test("gamma_ratio_exponential_tail_has_known_closed_form", {
  # Independent exponential-ratio survival: r_ctrl/(r_ctrl + target*r_trt).
  # The small upper tail must survive even when its complement rounds to one.
  expected <- 1e-18 / (1 + 1e-18)
  upper <- gamma_ratio_probability(1, 1e18, 1, 1, lower.tail = FALSE)$probability
  lower_inverse <- gamma_ratio_probability(1, 1, 1, 1e18)$probability
  stopifnot(abs(upper/expected-1) < 1e-13,
            abs(lower_inverse/expected-1) < 1e-13)
})
test("gamma_ratio_boundary_underflow_is_not_reported_as_certainty", {
  for (rates in list(c(1e300,1e-300), c(1e-300,1e300), c(1e308,1e-308), c(1e-308,1e308))) {
    for (lower_tail in c(TRUE,FALSE)) {
      # At shapes .0001 and rates 1e308/1e-308 the correct lower probability
      # is approximately .56612, so accepting an underflowed 0/1 is unsafe.
      error <- tryCatch(gamma_ratio_probability(.0001,rates[1],.0001,rates[2],
                                                lower.tail=lower_tail), error=identity)
      stopifnot(inherits(error,"error"), grepl("boundary underflowed",conditionMessage(error)))
    }
  }
  # Apply the same supported-range boundary whether plogis produces a
  # subnormal value or flushes it to zero on the current R/platform build.
  for (lower_tail in c(TRUE,FALSE)) {
    stopifnot(fails(gamma_ratio_probability(.0001,exp(709),.0001,1,lower.tail=lower_tail)),
              fails(gamma_ratio_probability(.0001,1,.0001,exp(709),lower.tail=lower_tail)))
    supported <- gamma_ratio_probability(.0001,exp(708),.0001,1,lower.tail=lower_tail)$probability
    stopifnot(is.finite(supported),supported>0,supported<1)
  }
  stopifnot(gamma_ratio_probability(.01,1e300,.01,1e-300,target=0)$probability == 0,
            gamma_ratio_probability(.01,1e300,.01,1e-300,target=0,lower.tail=FALSE)$probability == 1)
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
