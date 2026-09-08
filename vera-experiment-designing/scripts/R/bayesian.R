###############################################################################

simulate_qdf_tte_data <- function(n, lambda, accrual_time, followup_time) {
  enrollment <- runif(n, 0, accrual_time)
  max_obs <- accrual_time + followup_time - enrollment
  event_times <- rexp(n, rate = lambda)
  status <- as.integer(event_times <= max_obs)
  list(time = pmin(event_times, max_obs), status = status,
       events = sum(status))
}
# bayesian.R
# Generic Quantitative Decision Framework — Bayesian Inference & Go/No-Go
###############################################################################

# =============================================================================
# BETA-BINOMIAL: SINGLE-ARM POSTERIOR
# =============================================================================

bayes_binary_single_arm <- function(x, n, target, prior_a = 0.5, prior_b = 0.5) {
  post_a <- prior_a + x
  post_b <- prior_b + n - x
  prob_go <- 1 - pbeta(target, post_a, post_b)

  list(
    x = x, n = n, target = target,
    post_a = post_a, post_b = post_b,
    post_mean   = round(post_a / (post_a + post_b), 4),
    post_median = round(qbeta(0.5, post_a, post_b), 4),
    post_ci     = c(round(qbeta(0.025, post_a, post_b), 4),
                    round(qbeta(0.975, post_a, post_b), 4)),
    prob_above_target = prob_go
  )
}

# =============================================================================
# BETA-BINOMIAL: TWO-ARM POSTERIOR (MC)
# =============================================================================

bayes_binary_two_arm <- function(x_trt, n_trt, x_ctrl, n_ctrl, delta = 0,
                                 prior_a = 0.5, prior_b = 0.5,
                                 n_mc = 100000, seed = NULL) {
  if (!is.null(seed)) set.seed(seed)
  a_t <- prior_a + x_trt;  b_t <- prior_b + n_trt - x_trt
  a_c <- prior_a + x_ctrl; b_c <- prior_b + n_ctrl - x_ctrl
  theta_trt  <- rbeta(n_mc, a_t, b_t)
  theta_ctrl <- rbeta(n_mc, a_c, b_c)
  diff_draws <- theta_trt - theta_ctrl

  list(
    x_trt = x_trt, n_trt = n_trt,
    x_ctrl = x_ctrl, n_ctrl = n_ctrl,
    delta = delta,
    diff_mean   = round(mean(diff_draws), 4),
    diff_median = round(median(diff_draws), 4),
    diff_ci     = round(quantile(diff_draws, c(0.025, 0.975)), 4),
    prob_above_delta = mean(diff_draws > delta)
  )
}

# =============================================================================
# NORMAL-INVERSE-GAMMA: CONJUGATE UPDATE
# =============================================================================

nig_update <- function(x_bar, s2, n,
                       mu0 = 0, kappa0 = 0.01,
                       alpha0 = 0.5, beta0 = 0.5) {
  kappa_n <- kappa0 + n
  mu_n    <- (kappa0 * mu0 + n * x_bar) / kappa_n
  alpha_n <- alpha0 + n / 2
  beta_n  <- beta0 + 0.5 * (n - 1) * s2 +
             (kappa0 * n * (x_bar - mu0)^2) / (2 * kappa_n)
  list(mu_n = mu_n, kappa_n = kappa_n, alpha_n = alpha_n, beta_n = beta_n,
       df = 2 * alpha_n, scale = sqrt(beta_n / (alpha_n * kappa_n)))
}

# =============================================================================
# NIG: SINGLE-ARM POSTERIOR
# =============================================================================

bayes_continuous_single_arm <- function(x_bar, s2, n, target,
                                        mu0 = 0, kappa0 = 0.01,
                                        alpha0 = 0.5, beta0 = 0.5) {
  post <- nig_update(x_bar, s2, n, mu0, kappa0, alpha0, beta0)
  t_stat  <- (target - post$mu_n) / post$scale
  prob_go <- 1 - pt(t_stat, df = post$df)
  ci_lo <- post$mu_n + post$scale * qt(0.025, post$df)
  ci_hi <- post$mu_n + post$scale * qt(0.975, post$df)

  list(
    x_bar = x_bar, s2 = s2, n = n, target = target,
    post_mu = round(post$mu_n, 4), post_df = round(post$df, 2),
    post_scale = round(post$scale, 4),
    post_ci = c(round(ci_lo, 4), round(ci_hi, 4)),
    prob_above_target = prob_go
  )
}

# =============================================================================
# NIG: TWO-ARM POSTERIOR (MC)
# =============================================================================

bayes_continuous_two_arm <- function(x_bar_trt, s2_trt, n_trt,
                                     x_bar_ctrl, s2_ctrl, n_ctrl,
                                     delta = 0,
                                     mu0 = 0, kappa0 = 0.01,
                                     alpha0 = 0.5, beta0 = 0.5,
                                     n_mc = 100000, seed = NULL) {
  if (!is.null(seed)) set.seed(seed)
  post_t <- nig_update(x_bar_trt, s2_trt, n_trt, mu0, kappa0, alpha0, beta0)
  post_c <- nig_update(x_bar_ctrl, s2_ctrl, n_ctrl, mu0, kappa0, alpha0, beta0)
  mu_t <- post_t$mu_n + post_t$scale * rt(n_mc, df = post_t$df)
  mu_c <- post_c$mu_n + post_c$scale * rt(n_mc, df = post_c$df)
  diff_draws <- mu_t - mu_c

  list(
    diff_mean   = round(mean(diff_draws), 4),
    diff_median = round(median(diff_draws), 4),
    diff_ci     = round(quantile(diff_draws, c(0.025, 0.975)), 4),
    prob_above_delta = mean(diff_draws > delta)
  )
}

# =============================================================================
# GAMMA: TTE SINGLE-ARM POSTERIOR (Exponential model)
# Prior: lambda ~ Gamma(shape, rate). Posterior: Gamma(shape + d, rate + T)
# For TTE, lower hazard = better, so P(lambda < target) is the "go" probability
# =============================================================================

bayes_tte_single_arm <- function(events, person_time, target,
                                 prior_shape = 0.5, prior_rate = 1e-6) {
  post_shape <- prior_shape + events
  post_rate  <- prior_rate + person_time
  post_mean  <- post_shape / post_rate
  # P(lambda < target) — lower hazard is better
  prob_go <- pgamma(target, shape = post_shape, rate = post_rate)

  list(
    events = events, person_time = person_time, target = target,
    post_shape = post_shape, post_rate = post_rate,
    post_mean  = round(post_mean, 6),
    post_ci    = round(qgamma(c(0.025, 0.975), post_shape, post_rate), 6),
    median_survival = round(log(2) / post_mean, 2),
    prob_below_target = prob_go
  )
}

# =============================================================================
# GAMMA: TTE TWO-ARM POSTERIOR (MC on hazard ratio)
# =============================================================================

bayes_tte_two_arm <- function(events_trt, pt_trt, events_ctrl, pt_ctrl,
                              target_HR = 1,
                              prior_shape = 0.5, prior_rate = 1e-6,
                              n_mc = 100000, seed = NULL) {
  if (!is.null(seed)) set.seed(seed)
  lam_t <- rgamma(n_mc, prior_shape + events_trt, prior_rate + pt_trt)
  lam_c <- rgamma(n_mc, prior_shape + events_ctrl, prior_rate + pt_ctrl)
  hr_draws <- lam_t / lam_c  # HR < 1 = treatment benefit
  prob_go <- mean(hr_draws < target_HR)

  list(
    events_trt = events_trt, pt_trt = pt_trt,
    events_ctrl = events_ctrl, pt_ctrl = pt_ctrl,
    target_HR = target_HR,
    hr_mean   = round(mean(hr_draws), 4),
    hr_median = round(median(hr_draws), 4),
    hr_ci     = round(quantile(hr_draws, c(0.025, 0.975)), 4),
    prob_hr_below_target = prob_go
  )
}

# =============================================================================
# GAMMA: INCIDENCE RATE SINGLE-ARM POSTERIOR (Poisson-Gamma model)
# Data: Y ~ Poisson(lambda * T). Prior: lambda ~ Gamma(shape, rate)
# Posterior: Gamma(shape + Y, rate + T)
# =============================================================================

bayes_rate_single_arm <- function(count, exposure, target,
                                  prior_shape = 0.5, prior_rate = 1e-6,
                                  direction = "less") {
  post_shape <- prior_shape + count
  post_rate  <- prior_rate + exposure
  post_mean  <- post_shape / post_rate

  # direction "less" (protective): success = P(lambda < target).
  # direction "greater" (harm detection): success = P(lambda > target).
  # prob_target_met is the direction-correct success probability; the
  # direction-specific alias (prob_below_target / prob_above_target) is kept
  # so the field name never mislabels the tail it reports.
  prob_met <- if (direction == "greater") {
    pgamma(target, post_shape, post_rate, lower.tail = FALSE)
  } else {
    pgamma(target, post_shape, post_rate)
  }

  out <- list(
    count = count, exposure = exposure, target = target,
    direction = direction,
    post_shape = post_shape, post_rate = post_rate,
    post_mean  = round(post_mean, 6),
    post_ci    = round(qgamma(c(0.025, 0.975), post_shape, post_rate), 6),
    prob_target_met = prob_met
  )
  if (direction == "greater") {
    out$prob_above_target <- out$prob_target_met
  } else {
    out$prob_below_target <- out$prob_target_met
  }
  out
}

# =============================================================================
# GAMMA: INCIDENCE RATE TWO-ARM POSTERIOR (MC on rate ratio)
# =============================================================================

bayes_rate_two_arm <- function(count_trt, exp_trt, count_ctrl, exp_ctrl,
                               target_RR = 1,
                               prior_shape = 0.5, prior_rate = 1e-6,
                               n_mc = 100000, seed = NULL,
                               direction = "less") {
  if (!is.null(seed)) set.seed(seed)
  lam_t <- rgamma(n_mc, prior_shape + count_trt, prior_rate + exp_trt)
  lam_c <- rgamma(n_mc, prior_shape + count_ctrl, prior_rate + exp_ctrl)
  rr_draws <- lam_t / lam_c
  # direction "less" (protective): success = P(RR < target_RR).
  # direction "greater" (harm detection): success = P(RR > target_RR).
  prob_met <- if (direction == "greater") {
    mean(rr_draws > target_RR)
  } else {
    mean(rr_draws < target_RR)
  }

  out <- list(
    count_trt = count_trt, exp_trt = exp_trt,
    count_ctrl = count_ctrl, exp_ctrl = exp_ctrl,
    target_RR = target_RR,
    direction = direction,
    rr_mean   = round(mean(rr_draws), 4),
    rr_median = round(median(rr_draws), 4),
    rr_ci     = round(quantile(rr_draws, c(0.025, 0.975)), 4),
    prob_target_met = prob_met
  )
  if (direction == "greater") {
    out$prob_rr_above_target <- out$prob_target_met
  } else {
    out$prob_rr_below_target <- out$prob_target_met
  }
  out
}

# =============================================================================
# DECISION CLASSIFICATION: Go / Consider / No-Go
# =============================================================================

classify_decision <- function(posterior_prob, go_threshold = 0.90,
                              consider_threshold = 0.60) {
  if (posterior_prob >= go_threshold) {
    list(decision = "GO", posterior_prob = posterior_prob,
         note = "Applied Consider met. Proceed to totality of evidence review.")
  } else if (posterior_prob >= consider_threshold) {
    list(decision = "CONSIDER", posterior_prob = posterior_prob,
         note = "Signal detected. Steering committee review required.")
  } else {
    list(decision = "NO-GO", posterior_prob = posterior_prob,
         note = "Insufficient evidence. Consider stopping or reformulating.")
  }
}

# =============================================================================
# UNIFIED DISPATCHER: compute_decision(config, data)
# =============================================================================

#' Compute Bayesian Go/No-Go decision
#'
#' @param config  A qdf_config object
#' @param data    Observed data: list(x=, n=) for binary SA;
#'                list(x_trt=, n_trt=, x_ctrl=, n_ctrl=) for controlled binary;
#'                list(x_bar=, s2=, n=) for continuous SA;
#'                list(x_bar_trt=, s2_trt=, n_trt=, x_bar_ctrl=, s2_ctrl=, n_ctrl=) for controlled continuous;
#'                list(events=, person_time=) for TTE SA;
#'                list(events_trt=, pt_trt=, events_ctrl=, pt_ctrl=) for controlled TTE;
#'                list(count=, exposure=) for incidence_rate SA;
#'                list(count_trt=, exp_trt=, count_ctrl=, exp_ctrl=) for controlled incidence_rate
#' @param delta   Optional controlled effect target. When NULL, derive the
#'                risk/mean difference target as go_target - null_param.
#' @return list with posterior summary + decision
compute_decision <- function(config, data, delta = NULL) {
  validate_observed_data(config, data)
  pr <- config$prior_params
  controlled_delta <- if (is.null(delta)) config$go_target - config$null_param else delta

  if (config$endpoint_type == "binary") {
    if (config$design == "single_arm") {
      post <- bayes_binary_single_arm(data$x, data$n, config$go_target,
                                      prior_a = pr$a, prior_b = pr$b)
      dec <- classify_decision(post$prob_above_target,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    } else {
      post <- bayes_binary_two_arm(data$x_trt, data$n_trt,
                                   data$x_ctrl, data$n_ctrl,
                                   delta = controlled_delta,
                                   prior_a = pr$a, prior_b = pr$b)
      dec <- classify_decision(post$prob_above_delta,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    }
  } else if (config$endpoint_type == "continuous") {
    if (config$design == "single_arm") {
      post <- bayes_continuous_single_arm(data$x_bar, data$s2, data$n,
                                          config$go_target,
                                          mu0 = pr$mu0, kappa0 = pr$kappa0,
                                          alpha0 = pr$alpha0, beta0 = pr$beta0)
      dec <- classify_decision(post$prob_above_target,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    } else {
      post <- bayes_continuous_two_arm(data$x_bar_trt, data$s2_trt, data$n_trt,
                                       data$x_bar_ctrl, data$s2_ctrl, data$n_ctrl,
                                       delta = controlled_delta,
                                       mu0 = pr$mu0, kappa0 = pr$kappa0,
                                       alpha0 = pr$alpha0, beta0 = pr$beta0)
      dec <- classify_decision(post$prob_above_delta,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    }
  } else if (config$endpoint_type == "tte") {
    if (config$design == "single_arm") {
      post <- bayes_tte_single_arm(data$events, data$person_time,
                                   config$go_target,
                                   prior_shape = pr$shape, prior_rate = pr$rate)
      dec <- classify_decision(post$prob_below_target,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    } else {
      # go_target is target HR for two-arm
      target_HR <- config$go_target / config$null_param  # convert hazard to HR
      post <- bayes_tte_two_arm(data$events_trt, data$pt_trt,
                                data$events_ctrl, data$pt_ctrl,
                                target_HR = target_HR,
                                prior_shape = pr$shape, prior_rate = pr$rate)
      dec <- classify_decision(post$prob_hr_below_target,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    }
  } else if (config$endpoint_type == "incidence_rate") {
    # Both directions are valid for incidence rates: protective (alt < null)
    # succeeds below the target, harm detection (alt > null) above it.
    direction <- if (!is.null(config$direction)) config$direction
                 else if (config$alt_param > config$null_param) "greater" else "less"
    if (config$design == "single_arm") {
      post <- bayes_rate_single_arm(data$count, data$exposure,
                                    config$go_target,
                                    prior_shape = pr$shape, prior_rate = pr$rate,
                                    direction = direction)
      dec <- classify_decision(post$prob_target_met,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    } else {
      target_RR <- config$go_target / config$null_param
      post <- bayes_rate_two_arm(data$count_trt, data$exp_trt,
                                 data$count_ctrl, data$exp_ctrl,
                                 target_RR = target_RR,
                                 prior_shape = pr$shape, prior_rate = pr$rate,
                                 direction = direction)
      dec <- classify_decision(post$prob_target_met,
                               config$go_threshold, config$consider_threshold)
      return(c(post, dec))
    }
  }
}

# =============================================================================
# OPERATING CHARACTERISTICS
# =============================================================================

#' Compute operating characteristics across a range of true parameter values
#'
#' @param config       A qdf_config object
#' @param n            Sample size (single-arm) or list(n_trt=, n_ctrl=)
#' @param true_params  Vector of true parameter values to evaluate
#' @param B            Number of simulations per true value
#' @return data.frame with decision probabilities and Monte Carlo uncertainty
compute_oc <- function(config, n, true_params = NULL, B = 10000, seed = 42, delta = NULL) {
  if (!is.numeric(B) || length(B) != 1L || !is.finite(B) ||
      B < 1 || B != floor(B)) {
    stop("B must be one positive integer", call. = FALSE)
  }
  set.seed(seed)
  pr <- config$prior_params
  # Inner MC draws for two-arm posterior probability estimation.
  # 5000 gives SE < 0.01 per replicate while keeping OC runtime manageable.
  # For higher precision, increase B (outer replicates) rather than n_mc_oc.
  n_mc_oc <- 5000

  # Default true_params grid
  if (is.null(true_params)) {
    if (config$endpoint_type == "binary") {
      true_params <- seq(0.05, 0.95, by = 0.05)
    } else if (config$endpoint_type == "tte") {
      # Grid of true hazard rates around null and alt
      true_params <- seq(config$alt_param * 0.5, config$null_param * 1.5, length.out = 20)
    } else if (config$endpoint_type == "incidence_rate") {
      # Direction-agnostic grid: from below the smaller of (null, alt) to above
      # the larger. The old form assumed alt < null and DEGENERATED to a single
      # repeated point for harm-detection (alt > null) configs.
      rng <- abs(config$alt_param - config$null_param)
      lo <- min(config$alt_param, config$null_param)
      hi <- max(config$alt_param, config$null_param)
      true_params <- seq(max(0.01, lo - rng * 0.5), hi + rng * 0.5,
                         length.out = 20)
    } else {
      rng <- config$alt_param - config$null_param
      true_params <- seq(config$null_param - rng * 0.5,
                         config$alt_param + rng * 0.5,
                         length.out = 20)
    }
  }

  results <- data.frame()

  minimum_n <- if (config$endpoint_type == "continuous") 2 else 1
  is_valid_n <- function(value) {
    is.numeric(value) && length(value) == 1 && is.finite(value) &&
      value >= minimum_n && value == floor(value)
  }
  is_single_arm <- config$design == "single_arm"
  if (is_single_arm && !is_valid_n(n)) {
    stop("single_arm operating characteristics require integer n >= ", minimum_n,
         call. = FALSE)
  }
  if (!is_single_arm &&
      !(is.list(n) && all(c("n_trt", "n_ctrl") %in% names(n)) &&
        is_valid_n(n$n_trt) && is_valid_n(n$n_ctrl))) {
    stop("controlled operating characteristics require scalar positive integers in n_trt and n_ctrl",
         if (minimum_n > 1) " (at least 2 for continuous endpoints)" else "",
         call. = FALSE)
  }

  if (config$endpoint_type == "binary" && is_single_arm) {
    # Binary single-arm OC
    n_val <- if (is.list(n)) n$n_trt else n
    for (p_true in true_params) {
      x_sim <- rbinom(B, size = n_val, prob = p_true)
      decisions <- sapply(x_sim, function(x) {
        post_a <- pr$a + x; post_b <- pr$b + n_val - x
        prob <- 1 - pbeta(config$go_target, post_a, post_b)
        if (prob >= config$go_threshold) "GO"
        else if (prob >= config$consider_threshold) "CONSIDER"
        else "NOGO"
      })
      results <- rbind(results, data.frame(
        true_param = p_true,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  } else if (config$endpoint_type == "binary" && !is_single_arm) {
    # Binary two-arm OC — matches compute_decision(delta=delta)
    n_trt_val  <- n$n_trt
    n_ctrl_val <- n$n_ctrl
    go_delta   <- if (is.null(delta)) config$go_target - config$null_param else delta
    for (p_trt in true_params) {
      decisions <- character(B)
      for (b in 1:B) {
        x_t <- rbinom(1, n_trt_val, p_trt)
        x_c <- rbinom(1, n_ctrl_val, config$null_param)
        a_t <- pr$a + x_t; b_t <- pr$b + n_trt_val - x_t
        a_c <- pr$a + x_c; b_c <- pr$b + n_ctrl_val - x_c
        diff <- rbeta(n_mc_oc, a_t, b_t) - rbeta(n_mc_oc, a_c, b_c)
        prob <- mean(diff > go_delta)
        decisions[b] <- if (prob >= config$go_threshold) "GO"
                        else if (prob >= config$consider_threshold) "CONSIDER"
                        else "NOGO"
      }
      results <- rbind(results, data.frame(
        true_param = p_trt,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  } else if (config$endpoint_type == "continuous" && !is_single_arm) {
    # Continuous two-arm OC — matches compute_decision(delta=delta)
    n_trt_val  <- n$n_trt
    n_ctrl_val <- n$n_ctrl
    go_delta   <- if (is.null(delta)) config$go_target - config$null_param else delta
    for (mu_trt_true in true_params) {
      decisions <- character(B)
      for (b in 1:B) {
        # Simulate treatment arm
        y_trt  <- rnorm(n_trt_val, mean = mu_trt_true, sd = config$sd)
        # Simulate control arm at null (no effect)
        y_ctrl <- rnorm(n_ctrl_val, mean = config$null_param, sd = config$sd)
        # Compute posteriors for each arm via NIG
        post_t <- nig_update(mean(y_trt), var(y_trt), n_trt_val,
                             pr$mu0, pr$kappa0, pr$alpha0, pr$beta0)
        post_c <- nig_update(mean(y_ctrl), var(y_ctrl), n_ctrl_val,
                             pr$mu0, pr$kappa0, pr$alpha0, pr$beta0)
        # MC draws from each posterior
        mu_t_draws <- post_t$mu_n + post_t$scale * rt(n_mc_oc, df = post_t$df)
        mu_c_draws <- post_c$mu_n + post_c$scale * rt(n_mc_oc, df = post_c$df)
        diff_draws <- mu_t_draws - mu_c_draws
        prob <- mean(diff_draws > go_delta)
        decisions[b] <- if (prob >= config$go_threshold) "GO"
                        else if (prob >= config$consider_threshold) "CONSIDER"
                        else "NOGO"
      }
      results <- rbind(results, data.frame(
        true_param = mu_trt_true,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  } else if (config$endpoint_type == "continuous" && is_single_arm) {
    # Continuous single-arm OC
    n_val <- if (is.list(n)) n$n_trt else n
    for (mu_true in true_params) {
      decisions <- character(B)
      for (b in 1:B) {
        y <- rnorm(n_val, mean = mu_true, sd = config$sd)
        post <- bayes_continuous_single_arm(mean(y), var(y), n_val,
                                            config$go_target,
                                            pr$mu0, pr$kappa0, pr$alpha0, pr$beta0)
        prob <- post$prob_above_target
        decisions[b] <- if (prob >= config$go_threshold) "GO"
                        else if (prob >= config$consider_threshold) "CONSIDER"
                        else "NOGO"
      }
      results <- rbind(results, data.frame(
        true_param = mu_true,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  } else if (config$endpoint_type == "tte" && is_single_arm) {
    # TTE single-arm OC (exponential model)
    n_val <- if (is.list(n)) n$n_trt else n
    for (lam_true in true_params) {
      decisions <- character(B)
      for (b in 1:B) {
        # Uniform accrual gives each participant their own administrative
        # censoring horizon at database lock (accrual + follow-up).
        tte <- simulate_qdf_tte_data(n_val, lam_true,
                                     config$accrual_time, config$followup_time)
        d <- tte$events
        pt <- sum(tte$time)
        post <- bayes_tte_single_arm(d, pt, config$go_target,
                                     pr$shape, pr$rate)
        prob <- post$prob_below_target
        decisions[b] <- if (prob >= config$go_threshold) "GO"
                        else if (prob >= config$consider_threshold) "CONSIDER"
                        else "NOGO"
      }
      results <- rbind(results, data.frame(
        true_param = lam_true,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  } else if (config$endpoint_type == "tte" && !is_single_arm) {
    # TTE two-arm OC
    n_trt_val  <- n$n_trt
    n_ctrl_val <- n$n_ctrl
    target_HR  <- config$go_target / config$null_param
    for (lam_trt in true_params) {
      decisions <- character(B)
      for (b in 1:B) {
        tte_t <- simulate_qdf_tte_data(n_trt_val, lam_trt,
                                       config$accrual_time, config$followup_time)
        tte_c <- simulate_qdf_tte_data(n_ctrl_val, config$null_param,
                                       config$accrual_time, config$followup_time)
        d_t <- tte_t$events; pt_t <- sum(tte_t$time)
        d_c <- tte_c$events; pt_c <- sum(tte_c$time)
        lam_t_draws <- rgamma(n_mc_oc, pr$shape + d_t, pr$rate + pt_t)
        lam_c_draws <- rgamma(n_mc_oc, pr$shape + d_c, pr$rate + pt_c)
        hr_draws <- lam_t_draws / lam_c_draws
        prob <- mean(hr_draws < target_HR)
        decisions[b] <- if (prob >= config$go_threshold) "GO"
                        else if (prob >= config$consider_threshold) "CONSIDER"
                        else "NOGO"
      }
      results <- rbind(results, data.frame(
        true_param = lam_trt,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  } else if (config$endpoint_type == "incidence_rate" && is_single_arm) {
    # Incidence rate single-arm OC (direction-aware: protective tests below
    # target, harm detection above — must match compute_decision)
    n_val <- if (is.list(n)) n$n_trt else n
    T_exp <- config$exposure_time
    rate_dir <- if (!is.null(config$direction)) config$direction
                else if (config$alt_param > config$null_param) "greater" else "less"
    for (lam_true in true_params) {
      decisions <- character(B)
      for (b in 1:B) {
        counts <- rpois(n_val, lambda = lam_true * T_exp)
        total_count <- sum(counts); total_exp <- n_val * T_exp
        post <- bayes_rate_single_arm(total_count, total_exp, config$go_target,
                                      pr$shape, pr$rate, direction = rate_dir)
        prob <- post$prob_target_met
        decisions[b] <- if (prob >= config$go_threshold) "GO"
                        else if (prob >= config$consider_threshold) "CONSIDER"
                        else "NOGO"
      }
      results <- rbind(results, data.frame(
        true_param = lam_true,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  } else if (config$endpoint_type == "incidence_rate" && !is_single_arm) {
    # Incidence rate two-arm OC (direction-aware, matches compute_decision)
    n_trt_val  <- n$n_trt
    n_ctrl_val <- n$n_ctrl
    T_exp      <- config$exposure_time
    target_RR  <- config$go_target / config$null_param
    rate_dir <- if (!is.null(config$direction)) config$direction
                else if (config$alt_param > config$null_param) "greater" else "less"
    for (lam_trt in true_params) {
      decisions <- character(B)
      for (b in 1:B) {
        y_t <- sum(rpois(n_trt_val, lam_trt * T_exp))
        y_c <- sum(rpois(n_ctrl_val, config$null_param * T_exp))
        exp_t <- n_trt_val * T_exp; exp_c <- n_ctrl_val * T_exp
        lam_t_draws <- rgamma(n_mc_oc, pr$shape + y_t, pr$rate + exp_t)
        lam_c_draws <- rgamma(n_mc_oc, pr$shape + y_c, pr$rate + exp_c)
        rr_draws <- lam_t_draws / lam_c_draws
        prob <- if (rate_dir == "greater") mean(rr_draws > target_RR)
                else mean(rr_draws < target_RR)
        decisions[b] <- if (prob >= config$go_threshold) "GO"
                        else if (prob >= config$consider_threshold) "CONSIDER"
                        else "NOGO"
      }
      results <- rbind(results, data.frame(
        true_param = lam_trt,
        p_go = round(mean(decisions == "GO"), 4),
        p_consider = round(mean(decisions == "CONSIDER"), 4),
        p_nogo = round(mean(decisions == "NOGO"), 4),
        stringsAsFactors = FALSE))
    }
  }

  # Decision probabilities are binomial Monte Carlo estimates. Report their
  # uncertainty explicitly, including Wilson intervals that remain informative
  # when an observed probability is exactly zero or one. `mc_precision_ok` uses
  # the distribution-free worst-case SE, not the potentially misleading
  # observed SE at a boundary.
  z_mc <- qnorm(0.975)
  denom <- 1 + z_mc^2 / B
  for (prob_name in c("p_go", "p_consider", "p_nogo")) {
    p_hat <- results[[prob_name]]
    center <- (p_hat + z_mc^2 / (2 * B)) / denom
    half <- z_mc * sqrt(p_hat * (1 - p_hat) / B + z_mc^2 / (4 * B^2)) / denom
    results[[paste0(prob_name, "_mcse")]] <- round(sqrt(p_hat * (1 - p_hat) / B), 6)
    results[[paste0(prob_name, "_mc_lower")]] <- round(pmax(0, center - half), 6)
    results[[paste0(prob_name, "_mc_upper")]] <- round(pmin(1, center + half), 6)
  }
  precision_target <- 0.02
  results$mc_replicates <- as.integer(B)
  results$mc_worst_case_se <- round(0.5 / sqrt(B), 6)
  results$mc_precision_target_se <- precision_target
  results$mc_precision_ok <- (0.5 / sqrt(B)) <= precision_target

  return(results)
}

cat("bayesian.R loaded.\n")
