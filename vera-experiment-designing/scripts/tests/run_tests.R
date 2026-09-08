###############################################################################
# run_tests.R
# Regression tests for vera-experiment-designing
# Run: Rscript scripts/tests/run_tests.R  (from skill root)
###############################################################################

# --- Resolve source directory relative to this test file ---
test_dir <- tryCatch({
  # When source()'d
  dirname(normalizePath(sys.frame(1)$ofile))
}, error = function(e) {
  # When run via Rscript --file=
  arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (length(arg) > 0) {
    script_path <- sub("^--file=", "", arg[1])
    if (!file.exists(script_path)) script_path <- gsub("~+~", " ", script_path, fixed = TRUE)
    dirname(normalizePath(script_path, mustWork = TRUE))
  } else getwd()
})

r_dir <- normalizePath(file.path(test_dir, "..", "R"))

source(file.path(r_dir, "config.R"))
source(file.path(r_dir, "sample_size.R"))
source(file.path(r_dir, "bayesian.R"))
source(file.path(r_dir, "frequentist.R"))
source(file.path(r_dir, "ppos.R"))

# Also source the dispatcher (compute_sample_size, compute_oc live here)
# but run_framework.R re-sources the above files via resolve_script_dir().
# We override resolve_script_dir so it doesn't clobber our already-loaded fns.
resolve_script_dir <- function() r_dir
source(file.path(r_dir, "run_framework.R"))

pass <- 0L
fail <- 0L

run_test <- function(name, expr) {
  result <- tryCatch({
    expr
    TRUE
  }, error = function(e) {
    cat("TEST", name, ": FAIL (", conditionMessage(e), ")\n")
    FALSE
  })
  if (isTRUE(result)) {
    cat("TEST", name, ": PASS\n")
    pass <<- pass + 1L
  } else {
    fail <<- fail + 1L
  }
}

# ===========================================================================
# TEST 1: Poisson k_crit boundary exactness
# ===========================================================================
run_test("poisson_k_crit_boundary", {
  # Test the ENGINE's critical value (poisson_k_crit_lower), not a re-derived
  # inline formula — the old form of this test asserted properties of its own
  # arithmetic and passed even while the engine's clamp inflated type I error.
  alpha <- 0.05
  mu <- 0.5 * 40  # = 20: a regime where a valid rejection region exists

  k_crit <- poisson_k_crit_lower(mu, alpha)
  # Boundary exactness: largest integer with P(X <= k | mu) <= alpha
  stopifnot(
    k_crit >= 0,
    ppois(k_crit, mu) <= alpha,
    ppois(k_crit + 1, mu) > alpha
  )

  # Cross-check with power_poisson_single_arm: power is exactly the CDF at
  # k_crit under the alternative
  pwr <- power_poisson_single_arm(n = 40, lambda0 = 0.5, lambda1 = 0.3,
                                  exposure_time = 1,
                                  alpha = alpha, direction = "less")
  stopifnot(abs(pwr - ppois(k_crit, 0.3 * 40)) < 1e-12)
})

# ===========================================================================
# TEST 2: TTE zero-event continuity
# ===========================================================================
run_test("tte_zero_event_continuity", {
  # One arm has 0 events -- must not produce NaN/Inf

  result <- freq_tte_two_arm(events_trt = 0, pt_trt = 100,
                             events_ctrl = 5, pt_ctrl = 100,
                             alpha = 0.025)

  stopifnot(
    is.finite(result$z_stat),
    is.finite(result$HR),
    is.finite(result$se_log_HR),
    is.finite(result$p_value)
  )

  # Also test the symmetric case: ctrl has 0 events
  result2 <- freq_tte_two_arm(events_trt = 5, pt_trt = 100,
                              events_ctrl = 0, pt_ctrl = 100)
  stopifnot(
    is.finite(result2$z_stat),
    is.finite(result2$HR)
  )
})

# ===========================================================================
# TEST 3: Posterior seed independence
# ===========================================================================
run_test("posterior_seed_independence", {
  # With seed=NULL, bayes_binary_two_arm should consume from the caller's
  # RNG stream, so two consecutive calls produce different results.
  set.seed(123)
  r1 <- bayes_binary_two_arm(x_trt = 7, n_trt = 10,
                             x_ctrl = 3, n_ctrl = 10, seed = NULL)
  r2 <- bayes_binary_two_arm(x_trt = 7, n_trt = 10,
                             x_ctrl = 3, n_ctrl = 10, seed = NULL)

  # The two MC samples should differ (prob_above_delta based on 100k draws)
  stopifnot(r1$prob_above_delta != r2$prob_above_delta)

  # Conversely, with a fixed seed both calls should agree
  r3 <- bayes_binary_two_arm(x_trt = 7, n_trt = 10,
                             x_ctrl = 3, n_ctrl = 10, seed = 999)
  r4 <- bayes_binary_two_arm(x_trt = 7, n_trt = 10,
                             x_ctrl = 3, n_ctrl = 10, seed = 999)
  stopifnot(r3$prob_above_delta == r4$prob_above_delta)
})

# ===========================================================================
# TEST 4: Config incidence equality guard
# ===========================================================================
run_test("config_incidence_equality_guard", {
  # null_param == alt_param for incidence_rate must error
  # (the validation uses alt_param != null_param)
  err <- tryCatch({
    create_config(
      endpoint_type = "incidence_rate",
      study_type    = "poc",
      design        = "single_arm",
      null_param    = 0.5,
      alt_param     = 0.5,
      exposure_time = 1
    )
    FALSE  # should not reach here
  }, error = function(e) TRUE)

  stopifnot(isTRUE(err))

  # Binary also rejects equality (alt_param > null_param)
  err2 <- tryCatch({
    create_config(
      endpoint_type = "binary",
      study_type    = "poc",
      design        = "single_arm",
      null_param    = 0.5,
      alt_param     = 0.5
    )
    FALSE
  }, error = function(e) TRUE)

  stopifnot(isTRUE(err2))
})

# ===========================================================================
# TEST 5: n_oc controlled-row scalar
# ===========================================================================
run_test("n_oc_controlled_row_scalar", {
  cfg <- create_config(
    endpoint_type = "tte",
    study_type    = "poc",
    design        = "controlled",
    null_param    = 0.05,
    alt_param     = 0.025,
    alloc_ratio   = 1,
    accrual_time  = 12,
    followup_time = 12
  )

  ss <- compute_sample_size(cfg)
  ctrl_rows <- ss[ss$design == "controlled", , drop = FALSE]

  stopifnot(nrow(ctrl_rows) >= 1)

  # n_trt and n_ctrl in the first controlled row must be length-1 scalars
  n_t <- ctrl_rows$n_trt[1]
  n_c <- ctrl_rows$n_ctrl[1]

  stopifnot(
    length(n_t) == 1, !is.na(n_t), is.numeric(n_t), n_t > 0,
    length(n_c) == 1, !is.na(n_c), is.numeric(n_c), n_c > 0
  )
})

# ===========================================================================
# TEST 6: OC delta passthrough
# ===========================================================================
run_test("oc_delta_passthrough", {
  cfg <- create_config(
    endpoint_type = "binary",
    study_type    = "poc",
    design        = "controlled",
    null_param    = 0.20,
    alt_param     = 0.50,
    alloc_ratio   = 1
  )

  # compute_oc with delta=0 should not error
  # Use small B for speed
  oc <- compute_oc(cfg,
                   n = list(n_trt = 30, n_ctrl = 30),
                   true_params = c(0.20, 0.35, 0.50),
                   B = 50,
                   seed = 42,
                   delta = 0)

  stopifnot(
    is.data.frame(oc),
    "p_go" %in% names(oc),
    all(c("p_go_mcse", "p_go_mc_lower", "p_go_mc_upper",
          "mc_replicates", "mc_worst_case_se", "mc_precision_ok") %in% names(oc)),
    nrow(oc) >= 3,
    all(is.finite(oc$p_go)),
    all(oc$mc_replicates == 50),
    all(!oc$mc_precision_ok),
    all(oc$p_go >= oc$p_go_mc_lower & oc$p_go <= oc$p_go_mc_upper)
  )
})

# ===========================================================================
# TEST 7: Unimplemented methods are rejected, not silently degraded
# ===========================================================================
run_test("unimplemented_methods_rejected", {
  # tte_method='cox_ph' must ERROR (was previously a warning + silent degrade
  # to exponential). The honesty guarantee is regression-covered here.
  err_cox <- tryCatch({
    create_config(
      endpoint_type = "tte",
      study_type    = "poc",
      design        = "single_arm",
      null_param    = 0.05,
      alt_param     = 0.025,
      accrual_time  = 12,
      followup_time = 12,
      tte_method    = "cox_ph"
    )
    FALSE
  }, error = function(e) grepl("not yet implemented", conditionMessage(e)))
  stopifnot(isTRUE(err_cox))

  # rate_method='negbin' must ERROR (was warning + silent degrade to Poisson).
  err_nb <- tryCatch({
    create_config(
      endpoint_type  = "incidence_rate",
      study_type     = "poc",
      design         = "single_arm",
      null_param     = 0.5,
      alt_param      = 0.3,
      exposure_time  = 1,
      rate_method    = "negbin",
      overdispersion = 2
    )
    FALSE
  }, error = function(e) grepl("not yet implemented", conditionMessage(e)))
  stopifnot(isTRUE(err_nb))

  # The implemented defaults still build cleanly.
  cfg_ok <- create_config(
    endpoint_type = "tte", study_type = "poc", design = "single_arm",
    null_param = 0.05, alt_param = 0.025, accrual_time = 12, followup_time = 12
  )
  stopifnot(identical(cfg_ok$tte_method, "exponential"))
})

# ===========================================================================
# TEST 8: Incidence-rate direction — harm detection analyzed in the UPPER tail
# ===========================================================================
run_test("rate_direction_analysis", {
  # Frequentist single-arm, harm direction: exact upper-tail p-value.
  # count=15 observed, null expects 10: p = P(X >= 15 | mu=10).
  res_g <- freq_rate_single_arm(count = 15, exposure = 20, null_rate = 0.5,
                                alpha = 0.025, direction = "greater")
  stopifnot(abs(res_g$p_value - ppois(14, 10, lower.tail = FALSE)) < 1e-6,
            identical(res_g$direction, "greater"))
  # Protective (default) direction unchanged: p = P(X <= 15 | mu=10).
  res_l <- freq_rate_single_arm(count = 15, exposure = 20, null_rate = 0.5,
                                alpha = 0.025)
  stopifnot(abs(res_l$p_value - ppois(15, 10)) < 1e-6,
            identical(res_l$direction, "less"))

  # Frequentist two-arm, harm direction: upper-tail normal p-value.
  res2 <- freq_rate_two_arm(count_trt = 30, exp_trt = 20,
                            count_ctrl = 10, exp_ctrl = 20,
                            alpha = 0.025, direction = "greater")
  z_expect <- (1.5 - 0.5) / sqrt(1.5 / 20 + 0.5 / 20)
  stopifnot(abs(res2$p_value - pnorm(z_expect, lower.tail = FALSE)) < 1e-6,
            isTRUE(res2$reject_h0))

  # Bayesian single-arm, harm direction: success = P(lambda > target).
  post <- bayes_rate_single_arm(count = 40, exposure = 50, target = 0.65,
                                prior_shape = 0.5, prior_rate = 1e-6,
                                direction = "greater")
  expect <- pgamma(0.65, 40.5, 1e-6 + 50, lower.tail = FALSE)
  stopifnot(abs(post$prob_target_met - expect) < 1e-4,
            !is.null(post$prob_above_target),
            is.null(post$prob_below_target))

  # End-to-end: a harm-detection config (alt > null) is legal, carries
  # direction="greater", and its decision/OC use the upper tail.
  cfg <- create_config(
    endpoint_type = "incidence_rate", study_type = "poc",
    design = "single_arm", null_param = 0.5, alt_param = 0.8,
    exposure_time = 1
  )
  stopifnot(identical(cfg$direction, "greater"))
  dec <- compute_decision(cfg, list(count = 40, exposure = 50))
  stopifnot(abs(dec$prob_target_met -
                pgamma(cfg$go_target, 0.5 + 40, 1e-6 + 50,
                       lower.tail = FALSE)) < 1e-4)
  # OC curve must point the right way: P(Go) higher at the harmful alternative
  # than at the null. (Before the fix this was INVERTED: 0.565 at null vs
  # 0.005 at alt.)
  oc <- compute_oc(cfg, n = 50, true_params = c(0.5, 0.8), B = 300, seed = 7)
  p_go_null <- oc$p_go[oc$true_param == 0.5]
  p_go_alt  <- oc$p_go[oc$true_param == 0.8]
  stopifnot(p_go_alt > p_go_null, p_go_alt > 0.5, p_go_null < 0.2)

  # Protective configs still size/analyze below the target (regression guard).
  cfg_p <- create_config(
    endpoint_type = "incidence_rate", study_type = "poc",
    design = "single_arm", null_param = 0.5, alt_param = 0.3,
    exposure_time = 1
  )
  stopifnot(identical(cfg_p$direction, "less"))
  dec_p <- compute_decision(cfg_p, list(count = 15, exposure = 50))
  stopifnot(abs(dec_p$prob_target_met -
                pgamma(cfg_p$go_target, 0.5 + 15, 1e-6 + 50)) < 1e-4)

  # PPOS must also honor the direction: for a harm-detection confirmatory
  # design, stage-2 data AT the harmful alternative must give a high PPOS and
  # stage-2 data at the null a low one (before the fix both were computed in
  # the protective tail).
  set.seed(11)
  cfg_hi <- create_config(
    endpoint_type = "incidence_rate", study_type = "confirmatory",
    design = "single_arm", null_param = 0.5, alt_param = 0.8,
    exposure_time = 1, p2_data = list(count = 60, exposure = 75), p3_n = 100
  )
  cfg_lo <- create_config(
    endpoint_type = "incidence_rate", study_type = "confirmatory",
    design = "single_arm", null_param = 0.5, alt_param = 0.8,
    exposure_time = 1, p2_data = list(count = 37, exposure = 75), p3_n = 100
  )
  p_hi <- compute_ppos(cfg_hi, n_mc = 5000)$ppos
  p_lo <- compute_ppos(cfg_lo, n_mc = 5000)$ppos
  stopifnot(p_hi > 0.8, p_lo < 0.2)
})

run_test("controlled_rate_direction_analysis", {
  # Pin the two-arm Bayesian tail directly. The same seeded draws must strongly
  # support harm in the upper tail and reject the opposite lower-tail claim.
  set.seed(23)
  upper <- bayes_rate_two_arm(
    count_trt = 60, exp_trt = 75, count_ctrl = 37, exp_ctrl = 75,
    target_RR = 1.3, n_mc = 20000, direction = "greater")
  set.seed(23)
  lower <- bayes_rate_two_arm(
    count_trt = 60, exp_trt = 75, count_ctrl = 37, exp_ctrl = 75,
    target_RR = 1.3, n_mc = 20000, direction = "less")
  stopifnot(upper$prob_target_met > 0.8,
            lower$prob_target_met < 0.2,
            upper$prob_target_met + lower$prob_target_met == 1)

  cfg <- create_config(
    endpoint_type = "incidence_rate", study_type = "poc",
    design = "controlled", null_param = 0.5, alt_param = 0.8,
    exposure_time = 1)
  stopifnot(identical(cfg$direction, "greater"))
  decision <- compute_decision(cfg, list(
    count_trt = 60, exp_trt = 75, count_ctrl = 37, exp_ctrl = 75))
  stopifnot(decision$prob_target_met > 0.8,
            !is.null(decision$prob_rr_above_target),
            is.null(decision$prob_rr_below_target))

  # Pin the controlled OC branch that previously had no mutation guard.
  oc <- compute_oc(cfg, n = list(n_trt = 50, n_ctrl = 50),
                   true_params = c(0.5, 0.8), B = 150, seed = 19)
  stopifnot(oc$p_go[oc$true_param == 0.8] > oc$p_go[oc$true_param == 0.5],
            oc$p_go[oc$true_param == 0.5] < 0.15)

  # Pin the controlled PPOS sign convention for harm detection.
  high <- create_config(
    endpoint_type = "incidence_rate", study_type = "confirmatory",
    design = "controlled", null_param = 0.5, alt_param = 0.8,
    exposure_time = 1, p2_data = list(count = 60, exposure = 75),
    p2_data_ctrl = list(count = 37, exposure = 75), p3_n = 100)
  nullish <- create_config(
    endpoint_type = "incidence_rate", study_type = "confirmatory",
    design = "controlled", null_param = 0.5, alt_param = 0.8,
    exposure_time = 1, p2_data = list(count = 37, exposure = 75),
    p2_data_ctrl = list(count = 37, exposure = 75), p3_n = 100)
  set.seed(29); p_high <- ppos_rate_two_arm(high, n_mc = 5000)$ppos
  set.seed(29); p_null <- ppos_rate_two_arm(nullish, n_mc = 5000)$ppos
  stopifnot(p_high > p_null, p_high > 0.35, p_null < 0.15)
})

# ===========================================================================
# TEST 9: Exact Poisson — no fabricated rejection region at low information
# ===========================================================================
run_test("poisson_no_rejection_region", {
  # mu0 = 5 * 0.05 = 0.25: even X = 0 has P = 0.78 > alpha, so NO valid
  # rejection region exists. The old max(0, ...) clamp fabricated one at k=0,
  # returning n=5 with claimed power 0.95 and ACTUAL size 0.78.
  stopifnot(poisson_k_crit_lower(0.25, 0.025) < 0)
  pwr <- power_poisson_single_arm(n = 5, lambda0 = 0.05, lambda1 = 0.01,
                                  exposure_time = 1, alpha = 0.025,
                                  direction = "less")
  stopifnot(pwr == 0)

  # The sizing search must skip the no-region regime and return a design whose
  # ACTUAL size respects alpha and whose power meets the target.
  ss <- ss_poisson_single_arm(lambda0 = 0.05, lambda1 = 0.01,
                              exposure_time = 1, alpha = 0.025, power = 0.80,
                              direction = "less")
  stopifnot(!is.na(ss$n), ss$n > 5, ss$k_crit >= 0)
  actual_size <- ppois(ss$k_crit, 0.05 * ss$total_exposure)
  stopifnot(actual_size <= 0.025 + 1e-12, ss$power >= 0.80)
})

# ===========================================================================
# TEST 10: Reserved knobs rejected for EVERY endpoint; direction stored
# ===========================================================================
run_test("reserved_params_cross_endpoint", {
  # A reserved method label must error even on an endpoint that never reads
  # the knob — silently ignoring it is the same dishonesty class.
  err <- tryCatch({
    create_config(endpoint_type = "binary", study_type = "poc",
                  design = "single_arm", null_param = 0.2, alt_param = 0.4,
                  tte_method = "cox_ph")
    FALSE
  }, error = function(e) grepl("not yet implemented", conditionMessage(e)))
  stopifnot(isTRUE(err))

  # overdispersion is read by nothing: non-NULL must error, not be stored.
  err_od <- tryCatch({
    create_config(endpoint_type = "incidence_rate", study_type = "poc",
                  design = "single_arm", null_param = 0.5, alt_param = 0.3,
                  exposure_time = 1, overdispersion = 2)
    FALSE
  }, error = function(e) grepl("reserved", conditionMessage(e)))
  stopifnot(isTRUE(err_od))

  # Every config carries the favorable direction it was validated under.
  cfg_bin <- create_config(endpoint_type = "binary", study_type = "poc",
                           design = "single_arm",
                           null_param = 0.2, alt_param = 0.4)
  cfg_tte <- create_config(endpoint_type = "tte", study_type = "poc",
                           design = "single_arm",
                           null_param = 0.05, alt_param = 0.025,
                           accrual_time = 12, followup_time = 12)
  stopifnot(identical(cfg_bin$direction, "greater"),
            identical(cfg_tte$direction, "less"))
})

# ===========================================================================
# Summary
# ===========================================================================
run_test("controlled_contracts_do_not_fallback_single_arm", {
  missing_ctrl <- tryCatch({
    create_config(endpoint_type = "binary", study_type = "confirmatory",
                  design = "controlled", null_param = 0.2, alt_param = 0.4,
                  p2_data = list(x = 8, n = 20), p3_n = 100)
    FALSE
  }, error = function(e) grepl("p2_data_ctrl", conditionMessage(e)))
  stopifnot(isTRUE(missing_ctrl))

  cfg <- create_config(endpoint_type = "binary", study_type = "poc",
                       design = "controlled", null_param = 0.2,
                       alt_param = 0.4)
  scalar_oc <- tryCatch({ compute_oc(cfg, n = 40, B = 2); FALSE },
                        error = function(e) grepl("n_trt", conditionMessage(e)))
  stopifnot(isTRUE(scalar_oc))

  vector_oc <- tryCatch({
    compute_oc(cfg, n = list(n_trt = c(20, 40), n_ctrl = 30), B = 2)
    FALSE
  }, error = function(e) grepl("scalar positive integers", conditionMessage(e)))
  stopifnot(isTRUE(vector_oc))

  fractional_oc <- tryCatch({
    compute_oc(cfg, n = list(n_trt = 20.5, n_ctrl = 30), B = 2)
    FALSE
  }, error = function(e) grepl("scalar positive integers", conditionMessage(e)))
  stopifnot(isTRUE(fractional_oc))
})

run_test("strict_numeric_and_ppos_contracts", {
  rejects <- function(expr) tryCatch({ force(expr); FALSE }, error = function(e) TRUE)
  stopifnot(rejects(create_config(
    endpoint_type = "binary", study_type = "confirmatory", design = "single_arm",
    null_param = 0.2, alt_param = 0.4,
    p2_data = list(x = c(1, 2), n = 10), p3_n = 40)))
  stopifnot(rejects(create_config(
    endpoint_type = "continuous", study_type = "confirmatory", design = "single_arm",
    null_param = 0, alt_param = 1, sd = 1,
    p2_data = list(x_bar = 0.2, s2 = NaN, n = 10), p3_n = 20)))
  stopifnot(rejects(create_config(
    endpoint_type = "continuous", study_type = "confirmatory", design = "controlled",
    null_param = 0, alt_param = 1, sd = 1,
    p2_data = list(x_bar = 0.2, s2 = 1, n = 10),
    p2_data_ctrl = list(x_bar = 0, s2 = 1, n = 10),
    p3_n = 3, p3_alloc_ratio = 1)))
  stopifnot(rejects(create_config(
    endpoint_type = "binary", study_type = "poc", design = "single_arm",
    null_param = 0.2, alt_param = 0.4, prior = list(a = -1, b = 1))))
})

run_test("observed_data_contract_and_continuous_minimum_n", {
  cfg <- create_config(endpoint_type = "binary", study_type = "poc",
                       design = "controlled", null_param = 0.2, alt_param = 0.4)
  bad <- list(x = 4, n = 10)
  stopifnot(tryCatch({ compute_decision(cfg, bad); FALSE }, error = function(e) TRUE))
  stopifnot(tryCatch({ compute_freq_decision(cfg, bad); FALSE }, error = function(e) TRUE))

  ccfg <- create_config(endpoint_type = "continuous", study_type = "poc",
                        design = "single_arm", null_param = 0, alt_param = 1, sd = 1)
  stopifnot(tryCatch({ compute_oc(ccfg, n = 1, B = 2); FALSE }, error = function(e) TRUE))
})

run_test("controlled_continuous_frequentist_uses_arm_variances", {
  cfg <- create_config(endpoint_type = "continuous", study_type = "poc",
                       design = "controlled", null_param = 0, alt_param = 1,
                       sd = 1)
  result <- compute_freq_decision(cfg, list(
    x_bar_trt = 1.2, s2_trt = 1.44, n_trt = 20,
    x_bar_ctrl = 0.2, s2_ctrl = 1.00, n_ctrl = 20
  ))
  stopifnot(identical(result$test, "welch_t"),
            is.finite(result$t_stat), is.finite(result$p_value))
})

run_test("approximation_method_labels_match_implemented_estimands", {
  unequal <- ss_ttest_two_arm(delta = 0.5, sd = 1, alloc_ratio = 2)
  stopifnot(identical(unequal$test, "two_sample_z_normal_approximation"),
            identical(unequal$legacy_test, "two_sample_t"))
  equal <- ss_ttest_two_arm(delta = 0.5, sd = 1, alloc_ratio = 1)
  stopifnot(identical(equal$test, "two_sample_t"))

  sized <- ss_poisson_two_arm(lambda0 = 0.5, lambda1 = 0.3,
                              exposure_time = 1)
  analyzed <- freq_rate_two_arm(count_trt = 30, exp_trt = 100,
                                count_ctrl = 50, exp_ctrl = 100)
  stopifnot(identical(sized$test, "poisson_rate_difference_wald"),
            identical(analyzed$test, "poisson_rate_difference_wald"),
            identical(sized$legacy_test, "poisson_rate_ratio"),
            identical(analyzed$estimand, "rate_difference"))
})

run_test("dispatcher_validate_config_exposes_resolved_defaults_without_raw_data", {
  suite_root <- normalizePath(file.path(test_dir, "..", "..", ".."))
  dispatcher <- file.path(suite_root, "mcp-server", "r-wrapper", "dispatcher.R")
  input_path <- tempfile(fileext = ".json")
  output_path <- tempfile(fileext = ".json")
  on.exit(unlink(c(input_path, output_path)), add = TRUE)
  jsonlite::write_json(list(
    endpoint_type = "binary", study_type = "confirmatory",
    design = "single_arm", null_param = 0.2, alt_param = 0.4,
    p2_data = list(x = 4, n = 10), p3_n = 50
  ), input_path, auto_unbox = TRUE)
  exit_code <- system2(
    file.path(R.home("bin"), "Rscript"),
    c(shQuote(dispatcher), "validate_config", shQuote(input_path), shQuote(output_path)),
    stdout = FALSE, stderr = FALSE
  )
  payload <- jsonlite::fromJSON(output_path, simplifyVector = TRUE)
  resolved <- payload$resolved_config
  stopifnot(exit_code == 0, isTRUE(payload$valid),
            identical(resolved$estimand, "response_probability"),
            identical(resolved$direction, "greater"),
            identical(resolved$sidedness, "one_sided"),
            identical(resolved$prior_params$a, 0.5),
            isTRUE(resolved$has_p2_data),
            is.null(resolved$p2_data),
            identical(payload$simulation_defaults$seed, 42L),
            identical(payload$simulation_defaults$B_oc, 5000L))
})

run_test("tte_followup_changes_observation_and_ppos_has_no_alpha_floor", {
  set.seed(91); short <- simulate_qdf_tte_data(200, 0.2, 12, 0.01)
  set.seed(91); long  <- simulate_qdf_tte_data(200, 0.2, 12, 120)
  stopifnot(long$events > short$events, sum(long$time) > sum(short$time))

  cfg <- create_config(endpoint_type = "continuous", study_type = "confirmatory",
                       design = "single_arm", null_param = 0, alt_param = 1, sd = 1,
                       p2_data = list(x_bar = -0.5, s2 = 1, n = 20), p3_n = 20,
                       p3_alpha = 0.025)
  set.seed(7)
  assurance <- ppos_continuous_single_arm(cfg, n_mc = 2000)
  stopifnot(assurance$ppos < cfg$p3_alpha)
})

run_test("config_probability_threshold_and_target_validation", {
  rejects <- function(args) tryCatch({ do.call(create_config, args); FALSE },
                                      error = function(e) TRUE)
  base <- list(endpoint_type = "binary", study_type = "poc", design = "single_arm",
               null_param = 0.2, alt_param = 0.4)
  stopifnot(rejects(modifyList(base, list(alphas = 2))),
            rejects(modifyList(base, list(powers = 0))),
            rejects(modifyList(base, list(consider_threshold = 0.95, go_threshold = 0.9))),
            rejects(modifyList(base, list(go_target = 2))),
            rejects(modifyList(base, list(p2_data_ctrl = list(x = 1, n = 2)))))
})

run_test("arbitrary_allocation_and_ppos_grid_validation", {
  arms <- get_arms(40, "controlled_3_1")
  stopifnot(arms$n_trt == 30, arms$n_ctrl == 10)
  cfg <- create_config(endpoint_type = "binary", study_type = "confirmatory",
                       design = "controlled", null_param = 0.2, alt_param = 0.4,
                       p2_data = list(x = 2, n = 5),
                       p2_data_ctrl = list(x = 1, n = 5), p3_n = 4)
  stopifnot(tryCatch({ ppos_sensitivity(cfg, p3_n_grid = 1, n_mc = 10); FALSE },
                     error = function(e) TRUE))
  set.seed(2)
  p <- ppos_binary_two_arm(cfg, n_mc = 200)
  stopifnot(p$ppos > 0)

  set.seed(2)
  p_checked <- compute_ppos(cfg, n_mc = 200)
  stopifnot(is.finite(p_checked$ppos_mcse),
            p_checked$ppos_mc_lower <= p_checked$ppos,
            p_checked$ppos <= p_checked$ppos_mc_upper,
            identical(p_checked$n_mc, 200L),
            identical(p_checked$mc_precision_ok, FALSE))
})

run_test("direct_runner_reports_effective_oc_budget", {
  stopifnot(identical(formals(run_quantitative_framework)$B_oc, 5000))
  cfg <- create_config(endpoint_type = "binary", study_type = "poc",
                       design = "single_arm", null_param = 0.2, alt_param = 0.4)
  out <- run_quantitative_framework(
    cfg, observed_data = list(x = 4, n = 10), B_oc = 2, n_oc = 10)
  stopifnot(out$B_used == 2, identical(out$n_oc_used, 10))
})

run_test("frequentist_ci_level_and_zero_variance_contract", {
  exact <- freq_binary_single_arm(7, 10, 0.2, alpha = 0.10)
  reference <- binom.test(7, 10, p = 0.2, alternative = "greater",
                          conf.level = 0.90)
  stopifnot(exact$conf_level == 0.90,
            abs(exact$ci_lower - round(reference$conf.int[1], 4)) < 1e-12)

  ccfg <- create_config(endpoint_type = "continuous", study_type = "poc",
                        design = "single_arm", null_param = 0,
                        alt_param = 1, sd = 1)
  set.seed(7)
  bayes_zero <- compute_decision(ccfg, list(x_bar = 1, s2 = 0, n = 5))
  stopifnot(is.finite(bayes_zero$posterior_prob),
            bayes_zero$posterior_prob > 0.9)
  stopifnot(tryCatch({
    compute_freq_decision(ccfg, list(x_bar = 0, s2 = 0, n = 10)); FALSE
  }, error = function(e) TRUE))
})

run_test("controlled_go_target_changes_effect_estimand", {
  base <- list(endpoint_type = "binary", study_type = "poc",
               design = "controlled", null_param = 0.2, alt_param = 0.5)
  cfg_zero <- do.call(create_config, c(base, list(go_target = 0.2)))
  cfg_margin <- do.call(create_config, c(base, list(go_target = 0.35)))
  observed <- list(x_trt = 20, n_trt = 40, x_ctrl = 8, n_ctrl = 40)
  set.seed(91); zero <- compute_decision(cfg_zero, observed)
  set.seed(91); margin <- compute_decision(cfg_margin, observed)
  stopifnot(zero$prob_above_delta > margin$prob_above_delta,
            abs(margin$delta - 0.15) < 1e-12)
})

run_test("controlled_incidence_requires_positive_control_rate", {
  rejected <- tryCatch({
    create_config(endpoint_type = "incidence_rate", study_type = "poc",
                  design = "controlled", null_param = 0, alt_param = 1,
                  exposure_time = 1)
    FALSE
  }, error = function(e) grepl("null_param > 0", conditionMessage(e), fixed = TRUE))
  single <- create_config(endpoint_type = "incidence_rate", study_type = "poc",
                          design = "single_arm", null_param = 0, alt_param = 1,
                          exposure_time = 1)
  stopifnot(rejected, single$null_param == 0)
})

run_test("bayesian_decisions_use_unrounded_posterior_probability", {
  # The raw probability is just below the Go boundary but rounds to 0.9000.
  # Classification must use the raw value, not the reporting precision.
  target <- qbeta(0.10004, 5.5, 5.5)
  cfg <- create_config(
    endpoint_type = "binary", study_type = "poc", design = "single_arm",
    null_param = 0.2, alt_param = 0.4, go_target = target,
    go_threshold = 0.9, consider_threshold = 0.6)
  decision <- compute_decision(cfg, list(x = 5, n = 10))
  stopifnot(decision$posterior_prob < cfg$go_threshold,
            round(decision$posterior_prob, 4) == cfg$go_threshold,
            identical(decision$decision, "CONSIDER"),
            abs(decision$posterior_prob - 0.89996) < 1e-12)
})

run_test("exponential_event_probability_is_stable_for_small_hazards", {
  probability <- prob_event_exponential(
    lambda = 1e-10, accrual_time = 1, followup_time = 1)
  first_order <- 1e-10 * (1 + 1 / 2)
  stopifnot(probability > 0,
            is.finite(probability),
            abs(probability / first_order - 1) < 1e-8)
})

run_test("tte_low_event_conditional_power_is_not_clamped", {
  lambda <- 0.01
  p_event <- prob_event_exponential(lambda, 1, 1)
  expected_events <- 10 * p_event
  expected <- pnorm(sqrt(expected_events) * log(0.02 / lambda) -
                      qnorm(1 - 0.025))
  actual <- tte_single_arm_conditional_power(
    lambda = lambda, p3_n = 10, null_lambda = 0.02,
    accrual_time = 1, followup_time = 1, alpha = 0.025)
  stopifnot(expected_events < 1, actual > 0, actual < 1,
            abs(actual - expected) < 1e-15)
})

run_test("tte_two_arm_low_event_conditional_power_is_not_clamped", {
  lambda_trt <- 0.01
  lambda_ctrl <- 0.02
  n_trt <- 5
  n_ctrl <- 5
  expected_events <-
    n_trt * prob_event_exponential(lambda_trt, 1, 1) +
    n_ctrl * prob_event_exponential(lambda_ctrl, 1, 1)
  allocation_information <- (n_trt * n_ctrl) / (n_trt + n_ctrl)^2
  expected <- pnorm(
    sqrt(expected_events * allocation_information) *
      log(lambda_ctrl / lambda_trt) - qnorm(1 - 0.025))
  actual <- tte_two_arm_conditional_power(
    lambda_trt, lambda_ctrl, n_trt, n_ctrl,
    accrual_time = 1, followup_time = 1, alpha = 0.025)
  stopifnot(expected_events < 1, actual > 0, actual < 1,
            abs(actual - expected) < 1e-15)
})

run_test("barnard_unavailable_dependency_is_reported", {
  result <- local({
    original_exact_available <- .freq_exact_available
    assign(".freq_exact_available", function() FALSE, envir = .GlobalEnv)
    on.exit(assign(".freq_exact_available", original_exact_available,
                   envir = .GlobalEnv), add = TRUE)
    freq_binary_two_arm(8, 10, 4, 10)
  })
  stopifnot(is.na(result$p_barnard), is.na(result$reject_barnard),
            identical(result$barnard_status, "dependency_unavailable"),
            grepl("Exact", result$barnard_failure_reason, fixed = TRUE),
            identical(result$barnard_test,
                      "unconditional_exact_z_pooled"))
})

run_test("barnard_computation_failure_is_not_silently_missing", {
  result <- local({
    original_exact_available <- .freq_exact_available
    original_barnard_runner <- .freq_barnard_z_pooled
    assign(".freq_exact_available", function() TRUE, envir = .GlobalEnv)
    assign(".freq_barnard_z_pooled", function(data) {
      stop("synthetic Exact failure", call. = FALSE)
    }, envir = .GlobalEnv)
    on.exit({
      assign(".freq_exact_available", original_exact_available,
             envir = .GlobalEnv)
      assign(".freq_barnard_z_pooled", original_barnard_runner,
             envir = .GlobalEnv)
    }, add = TRUE)
    freq_binary_two_arm(8, 10, 4, 10)
  })
  stopifnot(is.na(result$p_barnard), is.na(result$reject_barnard),
            identical(result$barnard_status, "computation_failed"),
            identical(result$barnard_failure_reason,
                      "synthetic Exact failure"))
})

run_test("barnard_optional_exact_package_uses_supported_z_pooled_api", {
  if (base::requireNamespace("Exact", quietly = TRUE)) {
    mat <- matrix(c(8, 2, 4, 6), nrow = 2, byrow = TRUE)
    reference <- Exact::exact.test(
      mat, alternative = "greater", method = "z-pooled",
      model = "Binomial", cond.row = TRUE, to.plot = FALSE)
    result <- freq_binary_two_arm(8, 10, 4, 10)
    stopifnot(identical(result$barnard_status, "computed"),
              is.na(result$barnard_failure_reason),
              is.finite(result$p_barnard),
              result$p_barnard >= 0, result$p_barnard <= 1,
              abs(result$p_barnard - round(reference$p.value, 6)) < 1e-12,
              identical(result$reject_barnard,
                        reference$p.value <= result$alpha),
              identical(result$barnard_method,
                        "Exact::exact.test(method='z-pooled')"))
  }
})

cat("\n--- Results: ", pass, "passed,", fail, "failed ---\n")
if (fail > 0) quit(status = 1)
