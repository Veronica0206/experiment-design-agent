###############################################################################
# sample_size.R (UPDATED)
# Fixed alpha handling + Z-unpooled + Unconditional exact (Barnard-type)
#
# Alpha convention: alpha = one-sided alpha throughout (e.g., 0.025)
# All power.t.test calls use:
#   sig.level = alpha, alternative = "one.sided"
###############################################################################

# =============================================================================
# BINARY: SINGLE-ARM (Exact Binomial)
# =============================================================================

ss_binomial_single_arm <- function(p0, p1, alpha = 0.025, power = 0.80,
                                   n_min = 5, n_max = 300) {
  for (n in n_min:n_max) {
    k_crit <- qbinom(1 - alpha, size = n, prob = p0) + 1
    if (k_crit > n) next
    pwr <- 1 - pbinom(k_crit - 1, size = n, prob = p1)
    if (pwr >= power) {
      return(list(n = n, k_crit = k_crit, power = round(pwr, 4),
                  alpha = alpha, p0 = p0, p1 = p1))
    }
  }
  return(list(n = NA, k_crit = NA, power = NA,
              alpha = alpha, p0 = p0, p1 = p1))
}

power_binomial_single_arm <- function(n, p0, p1, alpha = 0.025) {
  k_crit <- qbinom(1 - alpha, size = n, prob = p0) + 1
  if (k_crit > n) return(0)
  return(1 - pbinom(k_crit - 1, size = n, prob = p1))
}

# =============================================================================
# BINARY: Z-TEST UNPOOLED (separate variances under H1)
# Standard two-proportion power calculation (industry convention)
# =============================================================================

power_z_unpooled <- function(n_trt, n_ctrl, p0, p1, alpha = 0.025) {
  se <- sqrt(p1 * (1 - p1) / n_trt + p0 * (1 - p0) / n_ctrl)
  ncp <- (p1 - p0) / se
  z_crit <- qnorm(1 - alpha)
  return(pnorm(ncp - z_crit))
}

ss_z_unpooled <- function(p0, p1, alpha = 0.025, power = 0.80,
                          alloc_ratio = 1, n_min = 5, n_max = 500) {
  for (n_ctrl in n_min:n_max) {
    n_trt <- ceiling(alloc_ratio * n_ctrl)
    pwr <- power_z_unpooled(n_trt, n_ctrl, p0, p1, alpha)
    if (pwr >= power) {
      return(list(n_total = n_trt + n_ctrl, n_trt = n_trt,
                  n_ctrl = n_ctrl, power = round(pwr, 4),
                  test = "z_unpooled"))
    }
  }
  return(list(n_total = NA, n_trt = NA, n_ctrl = NA,
              power = NA, test = "z_unpooled"))
}

# =============================================================================
# BINARY: UNCONDITIONAL EXACT (Barnard-type, Z-pooled statistic)
# Critical value calibrated by max Type I error over nuisance parameter
# =============================================================================

power_unconditional_exact <- function(n_trt, n_ctrl, p0, p1, alpha = 0.025,
                                      p_grid_step = 0.005) {
  n1 <- n_trt; n2 <- n_ctrl

  # Compute Z-pooled for all (x1, x2) pairs
  z_vals <- matrix(NA, n1 + 1, n2 + 1)
  for (x1 in 0:n1) {
    for (x2 in 0:n2) {
      p_pool <- (x1 + x2) / (n1 + n2)
      se <- sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
      z_vals[x1 + 1, x2 + 1] <- if (se > 0) (x1/n1 - x2/n2) / se else 0
    }
  }

  # Unique z values sorted decreasing
  all_z <- sort(unique(as.vector(z_vals)), decreasing = TRUE)
  p_grid <- seq(0.001, 0.999, by = p_grid_step)

  # Precompute binomial probs for null grid
  prob_x1_list <- lapply(p_grid, function(p) dbinom(0:n1, n1, p))
  prob_x2_list <- lapply(p_grid, function(p) dbinom(0:n2, n2, p))

  # Find smallest c (most powerful) where max Type I error <= alpha
  best_c <- Inf; best_power <- 0

  for (c_cand in all_z) {
    reject <- (z_vals >= c_cand)

    max_size <- 0
    for (i in seq_along(p_grid)) {
      prob_mat <- outer(prob_x1_list[[i]], prob_x2_list[[i]])
      size_at_p <- sum(prob_mat[reject])
      if (size_at_p > max_size) max_size <- size_at_p
    }

    if (max_size <= alpha) {
      prob_x1_h1 <- dbinom(0:n1, n1, p1)
      prob_x2_h0 <- dbinom(0:n2, n2, p0)
      power <- sum(outer(prob_x1_h1, prob_x2_h0)[reject])
      best_c <- c_cand; best_power <- power
    } else {
      break
    }
  }

  return(best_power)
}

# =============================================================================
# CONTINUOUS: SINGLE-ARM (One-sample t-test)
# =============================================================================

power_ttest_single_arm <- function(n, delta, sd, alpha = 0.025) {
  res <- power.t.test(n = n, delta = delta, sd = sd,
                      sig.level = alpha,
                      type = "one.sample",
                      alternative = "one.sided")
  return(res$power)
}

ss_ttest_single_arm <- function(delta, sd, alpha = 0.025, power = 0.80) {
  res <- power.t.test(delta = delta, sd = sd,
                      sig.level = alpha,
                      power = power,
                      type = "one.sample",
                      alternative = "one.sided")
  return(list(n = ceiling(res$n), delta = delta, sd = sd,
              alpha = alpha, power = power, test = "one_sample_t"))
}

# =============================================================================
# CONTINUOUS: TWO-ARM (Two-sample t-test)
# =============================================================================

power_ttest_two_arm <- function(n_trt, n_ctrl, delta, sd, alpha = 0.025) {
  if (n_trt == n_ctrl) {
    res <- power.t.test(n = n_trt, delta = delta, sd = sd,
                        sig.level = alpha,
                        type = "two.sample",
                        alternative = "one.sided")
    return(res$power)
  } else {
    za <- qnorm(1 - alpha)
    se <- sd * sqrt(1 / n_trt + 1 / n_ctrl)
    z_pwr <- (delta / se) - za
    return(pnorm(z_pwr))
  }
}

ss_ttest_two_arm <- function(delta, sd, alpha = 0.025, power = 0.80,
                             alloc_ratio = 1) {
  if (alloc_ratio == 1) {
    res <- power.t.test(delta = delta, sd = sd,
                        sig.level = alpha,
                        power = power,
                        type = "two.sample",
                        alternative = "one.sided")
    n_per <- ceiling(res$n)
    return(list(n_total = 2 * n_per, n_trt = n_per, n_ctrl = n_per,
                test = "two_sample_t"))
  }
  r <- alloc_ratio
  za <- qnorm(1 - alpha); zb <- qnorm(power)
  n_ctrl <- ceiling(((za + zb)^2 * sd^2 * (1 + 1/r)) / delta^2)
  n_trt  <- ceiling(r * n_ctrl)
  return(list(n_total = n_trt + n_ctrl, n_trt = n_trt, n_ctrl = n_ctrl,
              test = "two_sample_z_normal_approximation",
              legacy_test = "two_sample_t"))
}

# =============================================================================
# HELPER: Derive per-arm sizes from N total and design
# =============================================================================

get_arms <- function(n_total, design, alloc_ratio = NULL) {
  if (design == "single_arm") {
    return(list(n_trt = n_total, n_ctrl = NA))
  }
  if (is.null(alloc_ratio)) {
    parts <- strsplit(as.character(design), "_", fixed = TRUE)[[1]]
    alloc_ratio <- if (length(parts) >= 3 && suppressWarnings(!is.na(as.numeric(parts[2]))))
      as.numeric(parts[2]) / as.numeric(parts[3]) else 1
  }
  if (!is.numeric(alloc_ratio) || length(alloc_ratio) != 1L ||
      !is.finite(alloc_ratio) || alloc_ratio <= 0) {
    stop("alloc_ratio must be one positive finite number", call. = FALSE)
  }
  n_ctrl <- floor(n_total / (alloc_ratio + 1))
  n_trt <- n_total - n_ctrl
  list(n_trt = n_trt, n_ctrl = n_ctrl)
}

# =============================================================================
# TTE: Helper — Probability of observing an event (exponential + uniform accrual)
# =============================================================================

prob_event_exponential <- function(lambda, accrual_time, followup_time) {
  # Under uniform accrual over [0, accrual_time], follow-up starts at enrollment
  # P(event) = 1 - integral of S(t) over enrollment distribution
  if (lambda <= 0) return(0)
  a <- accrual_time; f <- followup_time
  # P(event) = 1 - exp(-lambda*f) * (1-exp(-lambda*a))/(lambda*a).
  # Evaluate on the log scale: the direct difference of exponentials loses all
  # precision when lambda*a is small and can return either zero or a value many
  # orders of magnitude too large.
  x <- lambda * a
  log_accrual_survival <- if (abs(x) < 1e-4) {
    -x / 2 + x^2 / 24 - x^4 / 2880
  } else {
    log(-expm1(-x)) - log(x)
  }
  p <- -expm1(-lambda * f + log_accrual_survival)
  return(max(0, min(1, p)))
}

# =============================================================================
# TTE: SINGLE-ARM (exponential rate test)
# Events required: d = (z_alpha + z_beta)^2 / (log(lambda_0 / lambda_1))^2
# =============================================================================

ss_logrank_single_arm <- function(lambda0, lambda1, accrual_time, followup_time,
                                  alpha = 0.025, power = 0.80) {
  za <- qnorm(1 - alpha); zb <- qnorm(power)
  log_hr <- log(lambda0 / lambda1)
  d <- ceiling((za + zb)^2 / log_hr^2)
  p_event <- prob_event_exponential(lambda1, accrual_time, followup_time)
  if (p_event <= 0) return(list(n = NA, events = d, p_event = 0, test = "exponential_rate"))
  n <- ceiling(d / p_event)
  return(list(n = n, events = d, p_event = round(p_event, 4),
              power = round(power, 4), alpha = alpha,
              lambda0 = lambda0, lambda1 = lambda1,
              median0 = log(2) / lambda0, median1 = log(2) / lambda1,
              test = "exponential_rate"))
}

power_logrank_single_arm <- function(n, lambda0, lambda1, accrual_time, followup_time,
                                     alpha = 0.025) {
  p_event <- prob_event_exponential(lambda1, accrual_time, followup_time)
  d <- n * p_event
  if (d <= 0) return(0)
  log_hr <- log(lambda0 / lambda1)
  z_stat <- sqrt(d) * abs(log_hr) - qnorm(1 - alpha)
  return(pnorm(z_stat))
}

# =============================================================================
# TTE: TWO-ARM (log-rank test, Schoenfeld formula)
# d = (z_alpha + z_beta)^2 / (allocation_fraction_product * log(HR)^2)
# where allocation_fraction_product = r / (1 + r)^2 for treatment:control r:1.
# =============================================================================

ss_logrank_two_arm <- function(lambda0, lambda1, accrual_time, followup_time,
                               alpha = 0.025, power = 0.80, alloc_ratio = 1) {
  za <- qnorm(1 - alpha); zb <- qnorm(power)
  HR <- lambda1 / lambda0  # HR < 1 = treatment benefit
  log_hr <- log(HR)
  r <- alloc_ratio
  alloc_info <- r / (1 + r)^2
  d <- ceiling((za + zb)^2 / (alloc_info * log_hr^2))
  # Weighted average P(event) across arms
  p_event_trt  <- prob_event_exponential(lambda1, accrual_time, followup_time)
  p_event_ctrl <- prob_event_exponential(lambda0, accrual_time, followup_time)
  p_event_avg  <- (r * p_event_trt + p_event_ctrl) / (r + 1)
  if (p_event_avg <= 0) return(list(n_total = NA, events = d, test = "logrank"))
  n_total <- ceiling(d / p_event_avg)
  n_ctrl  <- ceiling(n_total / (1 + r))
  n_trt   <- n_total - n_ctrl
  return(list(n_total = n_trt + n_ctrl, n_trt = n_trt, n_ctrl = n_ctrl,
              events = d, p_event_avg = round(p_event_avg, 4),
              HR = round(HR, 4), power = round(power, 4),
              test = "logrank"))
}

power_logrank_two_arm <- function(n_trt, n_ctrl, lambda0, lambda1,
                                  accrual_time, followup_time, alpha = 0.025) {
  r <- n_trt / n_ctrl
  p_event_trt  <- prob_event_exponential(lambda1, accrual_time, followup_time)
  p_event_ctrl <- prob_event_exponential(lambda0, accrual_time, followup_time)
  d <- n_trt * p_event_trt + n_ctrl * p_event_ctrl
  if (d <= 0) return(0)
  HR <- lambda1 / lambda0
  alloc_info <- r / (1 + r)^2
  z_stat <- sqrt(d * alloc_info) * abs(log(HR)) - qnorm(1 - alpha)
  return(pnorm(z_stat))
}

# =============================================================================
# INCIDENCE RATE: SINGLE-ARM (exact Poisson test)
# =============================================================================

# Exact lower-tail Poisson critical value: the largest k with
# P(X <= k | mu0) <= alpha, or -1 when no valid rejection region exists
# (mu0 so small that even X = 0 exceeds alpha). The old max(0, qpois - 1)
# clamp fabricated a k=0 region in that case, inflating the actual test size
# far above alpha (e.g. size 0.78 at claimed 0.025 for mu0 = 0.25).
poisson_k_crit_lower <- function(mu0, alpha) {
  k_crit <- qpois(alpha, lambda = mu0)
  if (ppois(k_crit, mu0) > alpha) k_crit <- k_crit - 1
  k_crit
}

ss_poisson_single_arm <- function(lambda0, lambda1, exposure_time,
                                  alpha = 0.025, power = 0.80,
                                  n_min = 5, n_max = 1000, direction = NULL) {
  # direction: "greater" if alt rate > null (harm detection), "less" if
  # alt rate < null (protective). NULL (default) auto-detects from the rates —
  # a hardcoded string default here would silently size the wrong tail.
  if (is.null(direction)) direction <- if (lambda1 > lambda0) "greater" else "less"
  for (n in n_min:n_max) {
    total_exp <- n * exposure_time
    if (direction == "greater") {
      # Reject if count >= k_crit
      k_crit <- qpois(1 - alpha, lambda = lambda0 * total_exp) + 1
      pwr <- 1 - ppois(k_crit - 1, lambda = lambda1 * total_exp)
    } else {
      # Reject if count <= k_crit (lower is better = treatment reduces rate).
      # k_crit = -1 means no valid rejection region at this n: power is 0 and
      # the search continues to larger n (more exposure -> region can appear).
      k_crit <- poisson_k_crit_lower(lambda0 * total_exp, alpha)
      pwr <- if (k_crit < 0) 0 else ppois(k_crit, lambda = lambda1 * total_exp)
    }
    if (pwr >= power) {
      return(list(n = n, k_crit = k_crit, total_exposure = total_exp,
                  power = round(pwr, 4), alpha = alpha,
                  lambda0 = lambda0, lambda1 = lambda1,
                  test = "exact_poisson"))
    }
  }
  return(list(n = NA, k_crit = NA, total_exposure = NA,
              power = NA, test = "exact_poisson"))
}

power_poisson_single_arm <- function(n, lambda0, lambda1, exposure_time,
                                     alpha = 0.025, direction = NULL) {
  # NULL direction auto-detects from the rates (see ss_poisson_single_arm) —
  # the previous "less"/"greater" split defaults meant sizing and power checks
  # could silently use opposite tails for the same design.
  if (is.null(direction)) direction <- if (lambda1 > lambda0) "greater" else "less"
  total_exp <- n * exposure_time
  if (direction == "less") {
    k_crit <- poisson_k_crit_lower(lambda0 * total_exp, alpha)
    if (k_crit < 0) return(0)  # no valid rejection region at this n
    return(ppois(k_crit, lambda = lambda1 * total_exp))
  } else {
    k_crit <- qpois(1 - alpha, lambda = lambda0 * total_exp) + 1
    return(1 - ppois(k_crit - 1, lambda = lambda1 * total_exp))
  }
}

# =============================================================================
# INCIDENCE RATE: TWO-ARM (rate-difference Wald normal approximation)
# =============================================================================

ss_poisson_two_arm <- function(lambda0, lambda1, exposure_time,
                               alpha = 0.025, power = 0.80, alloc_ratio = 1) {
  za <- qnorm(1 - alpha); zb <- qnorm(power)
  r <- alloc_ratio; T_exp <- exposure_time
  # Normal approx: N_ctrl = (za + zb)^2 * (lambda0/T + lambda1/(r*T)) / (lambda1 - lambda0)^2
  n_ctrl <- ceiling((za + zb)^2 * (lambda0 / T_exp + lambda1 / (r * T_exp)) /
                    (lambda1 - lambda0)^2)
  n_trt  <- ceiling(r * n_ctrl)
  # Verify power
  se <- sqrt(lambda0 / (n_ctrl * T_exp) + lambda1 / (n_trt * T_exp))
  z_pwr <- abs(lambda1 - lambda0) / se - za
  pwr <- pnorm(z_pwr)
  return(list(n_total = n_trt + n_ctrl, n_trt = n_trt, n_ctrl = n_ctrl,
              power = round(pwr, 4),
              estimand = "rate_difference",
              test = "poisson_rate_difference_wald",
              legacy_test = "poisson_rate_ratio"))
}

power_poisson_two_arm <- function(n_trt, n_ctrl, lambda0, lambda1, exposure_time,
                                  alpha = 0.025) {
  T_exp <- exposure_time
  se <- sqrt(lambda0 / (n_ctrl * T_exp) + lambda1 / (n_trt * T_exp))
  z_pwr <- abs(lambda1 - lambda0) / se - qnorm(1 - alpha)
  return(pnorm(z_pwr))
}

cat("sample_size.R loaded (updated: alpha fix + z_unpooled + unconditional exact + TTE logrank + incidence rate Poisson).\n")
