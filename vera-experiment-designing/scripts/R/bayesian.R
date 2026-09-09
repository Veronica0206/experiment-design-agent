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

# A bounded deterministic inner probability calculation is separate from outer
# simulation uncertainty. QUADPACK error estimates are numerical diagnostics.
QDF_POSTERIOR_MAX_EVALUATIONS <- 8192L
QDF_POSTERIOR_ABS_TOL <- 1e-8

.qdf_probability <- function(value, method, abs_error = 0, evaluations = 1L) {
  if (!is.numeric(value) || length(value) != 1L || !is.finite(value) ||
      value < -abs_error || value > 1 + abs_error)
    stop("Posterior probability calculation is invalid", call. = FALSE)
  list(probability = min(1, max(0, value)), method = method,
       abs_error = abs_error, evaluations = as.integer(evaluations))
}

.qdf_integrate_probability <- function(f, thresholds = numeric(0)) {
  evaluations <- 0L
  bounded <- function(u) {
    evaluations <<- evaluations + length(u)
    if (evaluations > QDF_POSTERIOR_MAX_EVALUATIONS)
      stop("Posterior quadrature exceeded its evaluation budget", call. = FALSE)
    value <- f(u)
    if (length(value) != length(u) || any(!is.finite(value)) || any(value < 0 | value > 1))
      stop("Posterior quadrature integrand is invalid", call. = FALSE)
    value
  }
  tolerance <- QDF_POSTERIOR_ABS_TOL
  for (limit in c(64L, 128L)) {
    result <- integrate(bounded, 0, 1, subdivisions = limit,
                        rel.tol = tolerance, abs.tol = tolerance,
                        stop.on.error = FALSE)
    valid <- identical(result$message, "OK") && is.finite(result$abs.error) &&
      result$abs.error <= tolerance
    separated <- !length(thresholds) ||
      all(abs(result$value - thresholds) > result$abs.error)
    if (valid && separated)
      return(.qdf_probability(result$value, "adaptive_quadrature", result$abs.error, evaluations))
    tolerance <- 1e-11
  }
  stop("Posterior decision is unresolved at the bounded numerical precision", call. = FALSE)
}

beta_difference_probability <- function(a_trt, b_trt, a_ctrl, b_ctrl, delta = 0,
                                        thresholds = numeric(0)) {
  if (delta <= -1) return(.qdf_probability(1, "adaptive_quadrature", evaluations = 0L))
  if (delta >= 1) return(.qdf_probability(0, "adaptive_quadrature", evaluations = 0L))
  if (delta == 0 && a_trt == a_ctrl && b_trt == b_ctrl)
    return(.qdf_probability(0.5, "adaptive_quadrature", evaluations = 0L))
  # Average the wider posterior's CDF over the narrower posterior. The reverse
  # direction can hide an almost discontinuous step near u=0/1 from QUADPACK.
  width_t <- qbeta(.75, a_trt, b_trt) - qbeta(.25, a_trt, b_trt)
  width_c <- qbeta(.75, a_ctrl, b_ctrl) - qbeta(.25, a_ctrl, b_ctrl)
  if (width_t < width_c) {
    return(.qdf_integrate_probability(function(u) {
      pbeta(qbeta(u, a_trt, b_trt) - delta, a_ctrl, b_ctrl)
    }, thresholds))
  }
  .qdf_integrate_probability(function(u) {
    pbeta(qbeta(u, a_ctrl, b_ctrl) + delta, a_trt, b_trt, lower.tail = FALSE)
  }, thresholds)
}

student_difference_probability <- function(post_trt, post_ctrl, delta = 0,
                                           thresholds = numeric(0)) {
  if (delta == 0 && identical(post_trt, post_ctrl))
    return(.qdf_probability(0.5, "adaptive_quadrature", evaluations = 0L))
  # Interquartile widths also exist when a Student posterior has no variance.
  width_t <- post_trt$scale * qt(.75, post_trt$df)
  width_c <- post_ctrl$scale * qt(.75, post_ctrl$df)
  if (width_t < width_c) {
    return(.qdf_integrate_probability(function(u) {
      treatment <- post_trt$mu_n + post_trt$scale * qt(u, post_trt$df)
      pt((treatment - delta - post_ctrl$mu_n) / post_ctrl$scale, post_ctrl$df)
    }, thresholds))
  }
  .qdf_integrate_probability(function(u) {
    control <- post_ctrl$mu_n + post_ctrl$scale * qt(u, post_ctrl$df)
    pt((control + delta - post_trt$mu_n) / post_trt$scale,
       post_trt$df, lower.tail = FALSE)
  }, thresholds)
}

# Independent Gamma(shape, rate) variables have a scaled beta-prime ratio.
# Its first moment exists iff the denominator shape is greater than one.
gamma_ratio_probability <- function(shape_trt, rate_trt, shape_ctrl, rate_ctrl,
                                    target = 1, lower.tail = TRUE) {
  values <- c(shape_trt, rate_trt, shape_ctrl, rate_ctrl)
  if (length(values) != 4L || any(!is.finite(values)) || any(values <= 0) ||
      !is.numeric(target) || length(target) != 1L || !is.finite(target) || target < 0 ||
      !is.logical(lower.tail) || length(lower.tail) != 1L || is.na(lower.tail))
    stop("Gamma ratio requires positive finite shapes/rates and a nonnegative target", call. = FALSE)
  # A positive Gamma ratio cannot be below zero; this is a mathematical
  # endpoint, not numerical saturation of a strictly positive target.
  if (target == 0)
    return(.qdf_probability(if (lower.tail) 0 else 1, "scaled_beta_prime"))
  log_odds <- log(target) + log(rate_trt) - log(rate_ctrl)
  # I_x(a,b) = 1 - I_(1-x)(b,a). Evaluate the smaller logistic boundary
  # directly, with swapped shapes and the reversed tail when x is near one.
  # Constructing x first can round it to one while the omitted Beta tail is
  # still substantial for small shape parameters.
  boundary <- plogis(-abs(log_odds))
  # Reject subnormal boundaries too: their relative precision degrades near
  # zero, and platform logistic implementations can flush them to zero early.
  if (!is.finite(boundary) || boundary < .Machine$double.xmin)
    stop("Gamma ratio probability boundary underflowed its supported precision",
         call. = FALSE)
  probability <- if (log_odds > 0) {
    pbeta(boundary, shape_ctrl, shape_trt, lower.tail = !lower.tail)
  } else {
    pbeta(boundary, shape_trt, shape_ctrl, lower.tail = lower.tail)
  }
  .qdf_probability(probability, "scaled_beta_prime")
}

gamma_ratio_summary <- function(shape_trt, rate_trt, shape_ctrl, rate_ctrl,
                                target = 1, lower.tail = TRUE) {
  probability <- gamma_ratio_probability(shape_trt, rate_trt, shape_ctrl, rate_ctrl,
                                         target, lower.tail)
  log_scale <- log(rate_ctrl) - log(rate_trt)
  # qf avoids the loss of the upper beta tail when qbeta rounds to one.
  quantiles <- exp(log_scale + log(shape_trt) - log(shape_ctrl) +
                     log(qf(c(0.025, 0.5, 0.975), 2 * shape_trt, 2 * shape_ctrl)))
  if (any(!is.finite(quantiles)))
    stop("Gamma ratio quantiles exceed the numerical range", call. = FALSE)
  mean_status <- if (shape_ctrl > 1) "finite" else "does_not_exist"
  ratio_mean <- if (shape_ctrl > 1)
    exp(log(shape_trt) + log_scale - log(shape_ctrl - 1)) else NA_real_
  if (shape_ctrl > 1 && !is.finite(ratio_mean))
    stop("Gamma ratio mean exceeds the numerical range", call. = FALSE)
  list(mean = ratio_mean, mean_status = mean_status, median = quantiles[2],
       ci = quantiles[c(1, 3)], probability = probability$probability,
       method = "scaled_beta_prime", abs_error = 0, evaluations = 1L)
}

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
                                 n_mc = 100000, seed = NULL, decision_thresholds = numeric(0)) {
  if (!is.null(seed)) set.seed(seed)
  a_t <- prior_a + x_trt;  b_t <- prior_b + n_trt - x_trt
  a_c <- prior_a + x_ctrl; b_c <- prior_b + n_ctrl - x_ctrl
  theta_trt  <- rbeta(n_mc, a_t, b_t)
  theta_ctrl <- rbeta(n_mc, a_c, b_c)
  diff_draws <- theta_trt - theta_ctrl
  probability <- beta_difference_probability(a_t, b_t, a_c, b_c, delta, decision_thresholds)

  list(
    x_trt = x_trt, n_trt = n_trt,
    x_ctrl = x_ctrl, n_ctrl = n_ctrl,
    delta = delta,
    diff_mean   = round(a_t / (a_t + b_t) - a_c / (a_c + b_c), 4),
    diff_median = round(median(diff_draws), 4),
    diff_ci     = round(quantile(diff_draws, c(0.025, 0.975)), 4),
    prob_above_delta = probability$probability,
    probability_method = probability$method,
    probability_abs_error = probability$abs_error
  )
}

# =============================================================================
# NORMAL-INVERSE-GAMMA: CONJUGATE UPDATE
# =============================================================================

nig_update <- function(x_bar, s2, n,
                       mu0 = 0, kappa0 = 0,
                       alpha0 = 0, beta0 = 0) {
  objective <- kappa0 == 0 && alpha0 == 0 && beta0 == 0
  if (!is.finite(n) || n < 2 || n != floor(n) || !is.finite(s2) || s2 < 0 ||
      !is.finite(x_bar) || any(!is.finite(c(mu0, kappa0, alpha0, beta0))) ||
      (!objective && any(c(kappa0, alpha0, beta0) <= 0)))
    stop("Normal posterior requires valid data and an objective or proper NIG prior", call. = FALSE)
  if (objective && s2 <= 0)
    stop("Joint Jeffreys normal posterior requires positive sample variance", call. = FALSE)
  kappa_n <- kappa0 + n
  mu_n    <- if (objective) x_bar else (kappa0 * mu0 + n * x_bar) / kappa_n
  alpha_n <- alpha0 + n / 2
  beta_n  <- beta0 + 0.5 * (n - 1) * s2 +
             if (objective) 0 else (kappa0 * n * (x_bar - mu0)^2) / (2 * kappa_n)
  if (any(!is.finite(c(mu_n, kappa_n, alpha_n, beta_n))) ||
      any(c(kappa_n, alpha_n, beta_n) <= 0))
    stop("Normal posterior exceeds the numerical range", call. = FALSE)
  post_scale <- sqrt(beta_n / alpha_n / kappa_n)
  if (!is.finite(post_scale) || post_scale <= 0 || !is.finite(2 * alpha_n))
    stop("Normal posterior scale exceeds the numerical range", call. = FALSE)
  list(mu_n = mu_n, kappa_n = kappa_n, alpha_n = alpha_n, beta_n = beta_n,
       df = 2 * alpha_n, scale = post_scale)
}

# =============================================================================
# NIG: SINGLE-ARM POSTERIOR
# =============================================================================

bayes_continuous_single_arm <- function(x_bar, s2, n, target,
                                        mu0 = 0, kappa0 = 0,
                                        alpha0 = 0, beta0 = 0) {
  post <- nig_update(x_bar, s2, n, mu0, kappa0, alpha0, beta0)
  t_stat  <- (target - post$mu_n) / post$scale
  prob_go <- pt(t_stat, df = post$df, lower.tail = FALSE)
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
                                     mu0 = 0, kappa0 = 0,
                                     alpha0 = 0, beta0 = 0,
                                     n_mc = 100000, seed = NULL, decision_thresholds = numeric(0)) {
  if (!is.null(seed)) set.seed(seed)
  post_t <- nig_update(x_bar_trt, s2_trt, n_trt, mu0, kappa0, alpha0, beta0)
  post_c <- nig_update(x_bar_ctrl, s2_ctrl, n_ctrl, mu0, kappa0, alpha0, beta0)
  mu_t <- post_t$mu_n + post_t$scale * rt(n_mc, df = post_t$df)
  mu_c <- post_c$mu_n + post_c$scale * rt(n_mc, df = post_c$df)
  diff_draws <- mu_t - mu_c
  probability <- student_difference_probability(post_t, post_c, delta, decision_thresholds)

  list(
    diff_mean   = round(post_t$mu_n - post_c$mu_n, 4),
    diff_median = round(median(diff_draws), 4),
    diff_ci     = round(quantile(diff_draws, c(0.025, 0.975)), 4),
    prob_above_delta = probability$probability,
    probability_method = probability$method,
    probability_abs_error = probability$abs_error
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
  summary <- gamma_ratio_summary(prior_shape + events_trt, prior_rate + pt_trt,
                                 prior_shape + events_ctrl, prior_rate + pt_ctrl,
                                 target_HR)
  list(events_trt = events_trt, pt_trt = pt_trt, events_ctrl = events_ctrl, pt_ctrl = pt_ctrl,
       target_HR = target_HR, hr_mean = round(summary$mean, 4),
       hr_mean_status = summary$mean_status, hr_median = round(summary$median, 4),
       hr_ci = round(summary$ci, 4), prob_hr_below_target = summary$probability,
       probability_method = summary$method, probability_abs_error = 0)

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
  summary <- gamma_ratio_summary(prior_shape + count_trt, prior_rate + exp_trt,
                                 prior_shape + count_ctrl, prior_rate + exp_ctrl,
                                 target_RR, lower.tail = direction != "greater")
  out <- list(count_trt = count_trt, exp_trt = exp_trt, count_ctrl = count_ctrl, exp_ctrl = exp_ctrl,
              target_RR = target_RR, direction = direction,
              rr_mean = round(summary$mean, 4), rr_mean_status = summary$mean_status,
              rr_median = round(summary$median, 4), rr_ci = round(summary$ci, 4),
              prob_target_met = summary$probability, probability_method = summary$method,
              probability_abs_error = 0)
  if (direction == "greater") out$prob_rr_above_target <- out$prob_target_met
  else out$prob_rr_below_target <- out$prob_target_met
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
                                   prior_a = pr$a, prior_b = pr$b,
                                   decision_thresholds = c(config$go_threshold, config$consider_threshold))
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
                                       alpha0 = pr$alpha0, beta0 = pr$beta0,
                                       decision_thresholds = c(config$go_threshold, config$consider_threshold))
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

validate_oc_grid <- function(config, true_params) {
  if (!is.numeric(true_params) || !length(true_params) || length(true_params) > 64L ||
      any(!is.finite(true_params)))
    stop("OC grid requires 1 to 64 finite parameter values", call. = FALSE)
  invalid <- switch(config$endpoint_type,
    binary = any(true_params < 0 | true_params > 1),
    continuous = FALSE, tte = any(true_params <= 0),
    incidence_rate = any(true_params < 0), TRUE)
  if (invalid) stop("OC grid is outside the endpoint domain", call. = FALSE)
  sort(unique(true_params))
}

default_oc_grid <- function(config) {
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
  validate_oc_grid(config, c(true_params, config$null_param, config$alt_param))
}

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
  pr <- config$prior_params
  if (!is.null(delta)) {
    if (config$design != "controlled" || !config$endpoint_type %in% c("binary", "continuous"))
      stop("delta is available only for controlled binary or continuous designs", call. = FALSE)
    if (!is.numeric(delta) || length(delta) != 1L || !is.finite(delta) ||
        (config$endpoint_type == "binary" && abs(delta) > 1))
      stop("delta is outside the endpoint effect domain", call. = FALSE)
  }
  true_params <- if (is.null(true_params)) default_oc_grid(config) else validate_oc_grid(config, true_params)
  inner_error_max <- 0
  inner_evaluations_max <- 1L
  record_probability <- function(result) {
    inner_error_max <<- max(inner_error_max, result$abs_error)
    inner_evaluations_max <<- max(inner_evaluations_max, result$evaluations)
    result$probability
  }
  thresholds <- c(config$go_threshold, config$consider_threshold)
  probability_cache <- new.env(parent = emptyenv(), hash = TRUE)
  cache_size <- 0L

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

  set.seed(seed)
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
        key <- paste(x_t, x_c, sep = ":")
        if (exists(key, probability_cache, inherits = FALSE)) {
          probability <- get(key, probability_cache, inherits = FALSE)
        } else {
          probability <- beta_difference_probability(a_t, b_t, a_c, b_c, go_delta, thresholds)
          if (cache_size < 10000L) {
            assign(key, probability, probability_cache)
            cache_size <- cache_size + 1L
          }
        }
        prob <- record_probability(probability)
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
        prob <- record_probability(student_difference_probability(post_t, post_c, go_delta, thresholds))
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
        prob <- record_probability(gamma_ratio_probability(pr$shape + d_t, pr$rate + pt_t,
                                    pr$shape + d_c, pr$rate + pt_c, target_HR))
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
        prob <- record_probability(gamma_ratio_probability(pr$shape + y_t, pr$rate + exp_t,
                                    pr$shape + y_c, pr$rate + exp_c, target_RR,
                                    lower.tail = rate_dir != "greater"))
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
  quadrature <- !is_single_arm && config$endpoint_type %in% c("binary", "continuous")
  results$inner_probability_method <- if (quadrature) "adaptive_quadrature" else
    if (config$endpoint_type == "binary") "analytic_beta_cdf" else
    if (config$endpoint_type == "continuous") "student_t_cdf" else
    if (is_single_arm) "gamma_cdf" else "scaled_beta_prime"
  results$inner_probability_abs_error_max <- inner_error_max
  results$inner_probability_evaluations_max <- inner_evaluations_max
  results$inner_probability_tolerance <- if (quadrature) QDF_POSTERIOR_ABS_TOL else 0
  results$inner_probability_max_evaluations <- if (quadrature) QDF_POSTERIOR_MAX_EVALUATIONS else 1L
  results$inner_precision_ok <- TRUE

  return(results)
}

cat("bayesian.R loaded.\n")
