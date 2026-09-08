###############################################################################
# ppos.R
# Generic Quantitative Decision Framework — Pre-Study Assurance (PPOS)
#
# Pre-study assurance = expected power of a planned Confirmatory-stage study,
# integrating over the Exploratory-stage posterior uncertainty on the treatment effect.
#
# PPOS = E[Power(theta) | P2 data]
#      = integral Power(theta) * pi(theta | P2 data) d(theta)
#
# Computed via Monte Carlo: draw theta_i ~ posterior, compute Power(theta_i),
# average across draws.
###############################################################################

resolve_script_dir <- function() {
  frames <- sys.frames()
  for (i in rev(seq_along(frames))) {
    if (!is.null(frames[[i]]$ofile)) {
      return(dirname(normalizePath(frames[[i]]$ofile)))
    }
  }
  script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (length(script_arg) > 0) {
    script_path <- sub("^--file=", "", script_arg[1])
    # Some R frontends encode spaces as ~+~ in commandArgs(), not in source().
    if (!file.exists(script_path)) script_path <- gsub("~+~", " ", script_path, fixed = TRUE)
    return(dirname(normalizePath(script_path, mustWork = TRUE)))
  }
  getwd()
}

framework_dir <- resolve_script_dir()
source(file.path(framework_dir, "bayesian.R"))
source(file.path(framework_dir, "sample_size.R"))

append_ppos_mc_diagnostics <- function(result, cond_power, n_mc,
                                       precision_target = 0.005) {
  if (!is.numeric(n_mc) || length(n_mc) != 1L || !is.finite(n_mc) ||
      n_mc < 2 || n_mc != floor(n_mc)) {
    stop("n_mc must be an integer of at least 2", call. = FALSE)
  }
  if (!is.numeric(cond_power) || length(cond_power) != n_mc ||
      any(!is.finite(cond_power)) || any(cond_power < 0 | cond_power > 1)) {
    stop("conditional-power draws must be finite probabilities matching n_mc",
         call. = FALSE)
  }
  mcse <- stats::sd(cond_power) / sqrt(n_mc)
  half <- qnorm(0.975) * mcse
  # Preserve the historical four-decimal value for display clients while the
  # primary estimate and interval use the same unrounded Monte Carlo mean.
  result$ppos_display <- result$ppos
  result$ppos <- mean(cond_power)
  result$n_mc <- as.integer(n_mc)
  result$ppos_mcse <- round(mcse, 6)
  result$ppos_mc_lower <- round(max(0, mean(cond_power) - half), 6)
  result$ppos_mc_upper <- round(min(1, mean(cond_power) + half), 6)
  result$mc_worst_case_se <- round(0.5 / sqrt(n_mc), 6)
  result$mc_precision_target_se <- precision_target
  result$mc_precision_ok <- (0.5 / sqrt(n_mc)) <= precision_target
  result
}

# =============================================================================
# BINARY: SINGLE-ARM PPOS
# =============================================================================

#' Compute pre-study assurance for binary single-arm Confirmatory-stage
#'
#' @param config  A qdf_config object with p2_data, p3_n, p3_alpha set
#' @param n_mc    Number of MC draws from P2 posterior
#' @return list with ppos, conditional_power_summary, posterior_summary
ppos_binary_single_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2 <- config$p2_data

  # P2 posterior: Beta(a + x, b + n - x)
  post_a <- pr$a + p2$x
  post_b <- pr$b + p2$n - p2$x

  # Draw true theta from P2 posterior
  theta_draws <- rbeta(n_mc, post_a, post_b)

  # For each theta, compute power of P3 exact binomial test
  # P3 rejects H0 if X >= k_crit where P(X >= k_crit | p0) <= alpha
  p3_n <- config$p3_n
  p3_alpha <- config$p3_alpha
  p0 <- config$null_param

  k_crit <- qbinom(1 - p3_alpha, size = p3_n, prob = p0) + 1

  # Conditional power: P(X >= k_crit | theta_i, n_p3)
  cond_power <- 1 - pbinom(k_crit - 1, size = p3_n, prob = theta_draws)

  # PPOS = mean conditional power
  ppos <- mean(cond_power)

  list(
    ppos           = round(ppos, 4),
    confirmatory_test = "exact_binomial",
    p3_n           = p3_n,
    p3_alpha       = p3_alpha,
    p3_k_crit      = k_crit,
    p2_post_a      = post_a,
    p2_post_b      = post_b,
    p2_post_mean   = round(post_a / (post_a + post_b), 4),
    p2_post_ci     = round(qbeta(c(0.025, 0.975), post_a, post_b), 4),
    cond_power_mean   = round(mean(cond_power), 4),
    cond_power_median = round(median(cond_power), 4),
    cond_power_q25    = round(quantile(cond_power, 0.25), 4),
    cond_power_q75    = round(quantile(cond_power, 0.75), 4),
    cond_power_draws  = cond_power  # for plotting
  )
}

# =============================================================================
# BINARY: TWO-ARM (CONTROLLED) PPOS
# =============================================================================

#' Compute pre-study assurance for binary controlled Confirmatory-stage
#'
#' @param config  A qdf_config with p2_data (treatment), p2_data_ctrl (control)
#' @param n_mc    Number of MC draws
#' @return list with ppos + summaries
ppos_binary_two_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2_trt  <- config$p2_data
  p2_ctrl <- config$p2_data_ctrl

  # P2 posteriors
  a_t <- pr$a + p2_trt$x;  b_t <- pr$b + p2_trt$n - p2_trt$x
  a_c <- pr$a + p2_ctrl$x; b_c <- pr$b + p2_ctrl$n - p2_ctrl$x

  theta_trt  <- rbeta(n_mc, a_t, b_t)
  theta_ctrl <- rbeta(n_mc, a_c, b_c)

  # P3 design
  p3_n <- config$p3_n
  r    <- config$p3_alloc_ratio
  n_ctrl_p3 <- floor(p3_n / (r + 1))
  n_trt_p3  <- p3_n - n_ctrl_p3
  p3_alpha  <- config$p3_alpha

  # The same unpooled Wald rejection-rule approximation used by sizing and
  # fixed-N power. Posterior integration changes the rates, not the test.
  cond_power <- sapply(1:n_mc, function(i) {
    power_z_unpooled(n_trt_p3, n_ctrl_p3, theta_ctrl[i], theta_trt[i], p3_alpha)
  })

  ppos <- mean(cond_power)

  list(
    ppos           = round(ppos, 4),
    confirmatory_test = "z_unpooled",
    p3_n           = p3_n,
    p3_n_trt       = n_trt_p3,
    p3_n_ctrl      = n_ctrl_p3,
    p3_alpha       = p3_alpha,
    p2_trt_post    = c(a_t, b_t),
    p2_ctrl_post   = c(a_c, b_c),
    p2_trt_mean    = round(a_t / (a_t + b_t), 4),
    p2_ctrl_mean   = round(a_c / (a_c + b_c), 4),
    diff_post_mean = round(mean(theta_trt - theta_ctrl), 4),
    cond_power_mean   = round(mean(cond_power), 4),
    cond_power_median = round(median(cond_power), 4),
    cond_power_draws  = cond_power
  )
}

# =============================================================================
# CONTINUOUS: SINGLE-ARM PPOS
# =============================================================================

ppos_continuous_single_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2 <- config$p2_data

  # P2 NIG posterior
  post <- nig_update(p2$x_bar, p2$s2, p2$n,
                     pr$mu0, pr$kappa0, pr$alpha0, pr$beta0)

  # Draw mu and sigma^2 from joint posterior
  # sigma^2 ~ InverseGamma(alpha_n, beta_n) = 1 / Gamma(alpha_n, beta_n)
  sigma2_draws <- 1 / rgamma(n_mc, shape = post$alpha_n, rate = post$beta_n)
  sigma_draws  <- sqrt(sigma2_draws)

  # mu | sigma^2 ~ Normal(mu_n, sigma^2 / kappa_n)
  mu_draws <- rnorm(n_mc, mean = post$mu_n,
                    sd = sqrt(sigma2_draws / post$kappa_n))

  # P3 design
  p3_n     <- config$p3_n
  p3_alpha <- config$p3_alpha
  mu0      <- config$null_param

  # Conditional power for each draw: power of one-sample t-test
  # Noncentrality parameter: ncp = (mu_true - mu0) / (sigma / sqrt(n))
  # Power = P(T > t_crit | ncp)
  t_crit <- qt(p3_alpha, df = p3_n - 1, lower.tail = FALSE)

  cond_power <- sapply(1:n_mc, function(i) {
    ncp <- (mu_draws[i] - mu0) / (sigma_draws[i] / sqrt(p3_n))
    noncentral_t_upper_tail(t_crit, p3_n - 1, ncp)
  })

  ppos <- mean(cond_power)

  list(
    ppos           = round(ppos, 4),
    confirmatory_test = "one_sample_noncentral_t",
    p3_n           = p3_n,
    p3_alpha       = p3_alpha,
    p2_post_mu     = round(post$mu_n, 4),
    p2_post_df     = round(post$df, 2),
    p2_post_scale  = round(post$scale, 4),
    mu_draws_mean  = round(mean(mu_draws), 4),
    sigma_draws_mean = round(mean(sigma_draws), 4),
    cond_power_mean   = round(mean(cond_power), 4),
    cond_power_median = round(median(cond_power), 4),
    cond_power_draws  = cond_power
  )
}

# =============================================================================
# CONTINUOUS: TWO-ARM PPOS
# =============================================================================

ppos_continuous_two_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2_trt  <- config$p2_data
  p2_ctrl <- config$p2_data_ctrl

  # P2 posteriors for each arm
  post_t <- nig_update(p2_trt$x_bar, p2_trt$s2, p2_trt$n,
                       pr$mu0, pr$kappa0, pr$alpha0, pr$beta0)
  post_c <- nig_update(p2_ctrl$x_bar, p2_ctrl$s2, p2_ctrl$n,
                       pr$mu0, pr$kappa0, pr$alpha0, pr$beta0)

  # Draw from joint posteriors
  sig2_t <- 1 / rgamma(n_mc, post_t$alpha_n, post_t$beta_n)
  mu_t   <- rnorm(n_mc, post_t$mu_n, sqrt(sig2_t / post_t$kappa_n))
  sig_t  <- sqrt(sig2_t)

  sig2_c <- 1 / rgamma(n_mc, post_c$alpha_n, post_c$beta_n)
  mu_c   <- rnorm(n_mc, post_c$mu_n, sqrt(sig2_c / post_c$kappa_n))
  sig_c  <- sqrt(sig2_c)

  # P3 design
  p3_n <- config$p3_n
  r    <- config$p3_alloc_ratio
  n_c3 <- floor(p3_n / (r + 1))
  n_t3 <- p3_n - n_c3
  p3_alpha <- config$p3_alpha

  # Conditional power via Welch t-test approximation
  cond_power <- sapply(1:n_mc, function(i) {
    delta <- mu_t[i] - mu_c[i]
    se    <- sqrt(sig_t[i]^2 / n_t3 + sig_c[i]^2 / n_c3)
    if (!is.finite(se) || se <= 0)
      stop("continuous conditional-power standard error must be positive and finite", call. = FALSE)
    # Welch df
    v_t <- sig_t[i]^2 / n_t3; v_c <- sig_c[i]^2 / n_c3
    df_w <- (v_t + v_c)^2 / (v_t^2 / (n_t3 - 1) + v_c^2 / (n_c3 - 1))
    t_crit <- qt(p3_alpha, df = df_w, lower.tail = FALSE)
    ncp <- delta / se
    noncentral_t_upper_tail(t_crit, df_w, ncp)
  })

  ppos <- mean(cond_power)

  list(
    ppos           = round(ppos, 4),
    confirmatory_test = "welch_noncentral_t_approximation",
    p3_n           = p3_n,
    p3_n_trt       = n_t3,
    p3_n_ctrl      = n_c3,
    p3_alpha       = p3_alpha,
    diff_post_mean = round(mean(mu_t - mu_c), 4),
    cond_power_mean   = round(mean(cond_power), 4),
    cond_power_median = round(median(cond_power), 4),
    cond_power_draws  = cond_power
  )
}

# =============================================================================
# TTE: SINGLE-ARM PPOS (exponential model)
# =============================================================================

tte_single_arm_conditional_power <- function(lambda, p3_n, null_lambda,
                                              accrual_time, followup_time,
                                              alpha) {
  if (!is.numeric(lambda) || length(lambda) != 1L || !is.finite(lambda) ||
      lambda <= 0) stop("conditional-power lambda must be positive and finite",
                        call. = FALSE)
  p_event <- prob_event_exponential(lambda, accrual_time, followup_time)
  d_expected <- p3_n * p_event
  # Retain the signed alternative: lambda below the null is favorable. There is
  # no statistical basis for setting power to exactly zero merely because the
  # expected event count is below one; doing so creates a discontinuity in PPOS.
  signal <- if (d_expected > 0) {
    sqrt(d_expected) * log(null_lambda / lambda)
  } else {
    0
  }
  pnorm(signal - qnorm(1 - alpha))
}

ppos_tte_single_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2 <- config$p2_data
  post_shape <- pr$shape + p2$events
  post_rate  <- pr$rate + p2$person_time

  # Draw true lambda from Gamma posterior
  lambda_draws <- rgamma(n_mc, shape = post_shape, rate = post_rate)

  # P3 design
  p3_n <- config$p3_n
  p3_alpha <- config$p3_alpha
  accrual <- config$accrual_time; followup <- config$followup_time

  # For each lambda, compute expected events and power of P3 rate test
  cond_power <- sapply(lambda_draws, function(lam) {
    tte_single_arm_conditional_power(
      lam, p3_n, config$null_param, accrual, followup, p3_alpha)
  })

  ppos <- mean(cond_power)
  list(
    ppos = round(ppos, 4), p3_n = p3_n, p3_alpha = p3_alpha,
    confirmatory_test = "exponential_event_normal_approximation",
    p2_post_shape = post_shape, p2_post_rate = post_rate,
    p2_post_mean_lambda = round(post_shape / post_rate, 6),
    cond_power_mean = round(mean(cond_power), 4),
    cond_power_median = round(median(cond_power), 4),
    cond_power_draws = cond_power
  )
}

# =============================================================================
# TTE: TWO-ARM PPOS
# =============================================================================

tte_two_arm_conditional_power <- function(lambda_trt, lambda_ctrl,
                                          n_trt, n_ctrl,
                                          accrual_time, followup_time,
                                          alpha) {
  hazards <- c(lambda_trt, lambda_ctrl)
  if (any(!is.finite(hazards)) || any(hazards <= 0)) {
    stop("conditional-power hazards must be positive and finite", call. = FALSE)
  }
  sizes <- c(n_trt, n_ctrl)
  if (any(!is.finite(sizes)) || any(sizes <= 0)) {
    stop("conditional-power arm sizes must be positive and finite", call. = FALSE)
  }
  pe_t <- prob_event_exponential(lambda_trt, accrual_time, followup_time)
  pe_c <- prob_event_exponential(lambda_ctrl, accrual_time, followup_time)
  d_expected <- n_trt * pe_t + n_ctrl * pe_c
  if (d_expected <= 0) return(0)
  allocation_information <- (n_trt * n_ctrl) / (n_trt + n_ctrl)^2
  signal <- sqrt(d_expected * allocation_information) *
    log(lambda_ctrl / lambda_trt)
  pnorm(signal - qnorm(1 - alpha))
}

ppos_tte_two_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2_trt  <- config$p2_data
  p2_ctrl <- config$p2_data_ctrl

  lam_t <- rgamma(n_mc, pr$shape + p2_trt$events, pr$rate + p2_trt$person_time)
  lam_c <- rgamma(n_mc, pr$shape + p2_ctrl$events, pr$rate + p2_ctrl$person_time)
  ratio_summary <- gamma_ratio_summary(pr$shape + p2_trt$events,
    pr$rate + p2_trt$person_time, pr$shape + p2_ctrl$events,
    pr$rate + p2_ctrl$person_time)

  p3_n <- config$p3_n; r <- config$p3_alloc_ratio; p3_alpha <- config$p3_alpha
  n_ctrl_p3 <- floor(p3_n / (r + 1)); n_trt_p3 <- p3_n - n_ctrl_p3

  cond_power <- sapply(1:n_mc, function(i) {
    tte_two_arm_conditional_power(
      lam_t[i], lam_c[i], n_trt_p3, n_ctrl_p3,
      config$accrual_time, config$followup_time, p3_alpha)
  })

  ppos <- mean(cond_power)
  list(
    ppos = round(ppos, 4), p3_n = p3_n, p3_alpha = p3_alpha,
    confirmatory_test = "exponential_event_normal_approximation",
    hr_post_mean = ratio_summary$mean,
    hr_post_mean_status = ratio_summary$mean_status,
    hr_post_median = ratio_summary$median,
    hr_post_ci = ratio_summary$ci,
    ratio_summary_method = ratio_summary$method,
    cond_power_mean = round(mean(cond_power), 4),
    cond_power_draws = cond_power
  )
}

# =============================================================================
# INCIDENCE RATE: SINGLE-ARM PPOS
# =============================================================================

ppos_rate_single_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2 <- config$p2_data
  post_shape <- pr$shape + p2$count
  post_rate  <- pr$rate + p2$exposure

  lambda_draws <- rgamma(n_mc, shape = post_shape, rate = post_rate)

  p3_n <- config$p3_n; p3_alpha <- config$p3_alpha; T_exp <- config$exposure_time
  total_exp_p3 <- p3_n * T_exp
  direction <- if (!is.null(config$direction)) config$direction
               else if (config$alt_param > config$null_param) "greater" else "less"

  mu0 <- config$null_param * total_exp_p3
  if (direction == "greater") {
    # Harm detection: reject if count >= k_crit (upper tail, size <= alpha)
    k_crit <- qpois(1 - p3_alpha, lambda = mu0) + 1
    cond_power <- sapply(lambda_draws, function(lam) {
      1 - ppois(k_crit - 1, lambda = lam * total_exp_p3)
    })
  } else {
    # Protective: reject if count <= k_crit. Exact critical value: largest k
    # with P(X <= k | H0) <= alpha. When even X=0 exceeds alpha (mu0 too small)
    # there is NO valid rejection region — conditional power is 0, not the
    # inflated pseudo-test at k=0 the old max(0, ...) clamp produced.
    k_crit <- qpois(p3_alpha, lambda = mu0)
    if (ppois(k_crit, mu0) > p3_alpha) k_crit <- k_crit - 1
    cond_power <- if (k_crit < 0) rep(0, n_mc) else {
      sapply(lambda_draws, function(lam) ppois(k_crit, lambda = lam * total_exp_p3))
    }
  }

  ppos <- mean(cond_power)
  list(
    ppos = round(ppos, 4), p3_n = p3_n, p3_alpha = p3_alpha,
    confirmatory_test = "exact_poisson",
    p2_post_mean_rate = round(post_shape / post_rate, 6),
    cond_power_mean = round(mean(cond_power), 4),
    cond_power_median = round(median(cond_power), 4),
    cond_power_draws = cond_power
  )
}

# =============================================================================
# INCIDENCE RATE: TWO-ARM PPOS
# =============================================================================

ppos_rate_two_arm <- function(config, n_mc = 100000) {
  pr <- config$prior_params
  p2_trt  <- config$p2_data
  p2_ctrl <- config$p2_data_ctrl

  lam_t <- rgamma(n_mc, pr$shape + p2_trt$count, pr$rate + p2_trt$exposure)
  lam_c <- rgamma(n_mc, pr$shape + p2_ctrl$count, pr$rate + p2_ctrl$exposure)
  ratio_summary <- gamma_ratio_summary(pr$shape + p2_trt$count,
    pr$rate + p2_trt$exposure, pr$shape + p2_ctrl$count,
    pr$rate + p2_ctrl$exposure)

  p3_n <- config$p3_n; r <- config$p3_alloc_ratio; p3_alpha <- config$p3_alpha
  T_exp <- config$exposure_time
  n_ctrl_p3 <- floor(p3_n / (r + 1)); n_trt_p3 <- p3_n - n_ctrl_p3
  direction <- if (!is.null(config$direction)) config$direction
               else if (config$alt_param > config$null_param) "greater" else "less"

  cond_power <- sapply(1:n_mc, function(i) {
    diff <- lam_t[i] - lam_c[i]
    se <- sqrt(lam_t[i] / (n_trt_p3 * T_exp) + lam_c[i] / (n_ctrl_p3 * T_exp))
    signed_diff <- if (direction == "less") -diff else diff
    if (se == 0) return(as.numeric(signed_diff > 0))
    z_pwr <- signed_diff / se - qnorm(1 - p3_alpha)
    pnorm(z_pwr)
  })

  ppos <- mean(cond_power)
  list(
    ppos = round(ppos, 4), p3_n = p3_n, p3_alpha = p3_alpha,
    confirmatory_test = "poisson_rate_difference_wald",
    rr_post_mean = ratio_summary$mean,
    rr_post_mean_status = ratio_summary$mean_status,
    rr_post_median = ratio_summary$median,
    rr_post_ci = ratio_summary$ci,
    ratio_summary_method = ratio_summary$method,
    rate_difference_post_mean = round(mean(lam_t - lam_c), 6),
    cond_power_mean = round(mean(cond_power), 4),
    cond_power_median = round(median(cond_power), 4),
    cond_power_draws = cond_power
  )
}

# =============================================================================
# UNIFIED DISPATCHER: compute_ppos(config)
# =============================================================================

#' Compute Pre-study Probability of Success (Assurance)
#'
#' @param config  A qdf_config with study_type="confirmatory", p2_data, p3_n set
#' @param n_mc    Number of MC draws
#' @return list with ppos + supporting statistics
compute_ppos <- function(config, n_mc = 100000) {
  if (is.null(config$p2_data)) stop("p2_data required for PPOS")
  if (is.null(config$p3_n)) stop("p3_n (planned P3 sample size) required for PPOS")
  if (config$design == "controlled" && is.null(config$p2_data_ctrl)) {
    stop("p2_data_ctrl is required for controlled-design PPOS", call. = FALSE)
  }

  result <- if (config$endpoint_type == "binary") {
    if (config$design == "single_arm") {
      ppos_binary_single_arm(config, n_mc)
    } else {
      ppos_binary_two_arm(config, n_mc)
    }
  } else if (config$endpoint_type == "continuous") {
    if (config$design == "single_arm") {
      ppos_continuous_single_arm(config, n_mc)
    } else {
      ppos_continuous_two_arm(config, n_mc)
    }
  } else if (config$endpoint_type == "tte") {
    if (config$design == "single_arm") {
      ppos_tte_single_arm(config, n_mc)
    } else {
      ppos_tte_two_arm(config, n_mc)
    }
  } else if (config$endpoint_type == "incidence_rate") {
    if (config$design == "single_arm") {
      ppos_rate_single_arm(config, n_mc)
    } else {
      ppos_rate_two_arm(config, n_mc)
    }
  } else {
    stop("Unsupported endpoint_type for PPOS: ", config$endpoint_type, call. = FALSE)
  }
  result$prior_method <- config$prior_method
  append_ppos_mc_diagnostics(result, result$cond_power_draws, n_mc)
}

# =============================================================================
# PPOS SENSITIVITY: Vary P3 sample size, compute PPOS for each
# =============================================================================

#' Compute PPOS across a range of Confirmatory-stage sample sizes
#'
#' @param config    A qdf_config
#' @param p3_n_grid Vector of P3 sample sizes to evaluate
#' @param n_mc      MC draws per evaluation
#' @return data.frame with p3_n, ppos
ppos_sensitivity <- function(config, p3_n_grid = NULL, n_mc = 50000) {
  .qdf_scalar(n_mc, "n_mc", lower = 2, integer = TRUE)
  minimum <- if (config$endpoint_type == "continuous") 2L else 1L
  minimum_total <- if (config$design == "single_arm") minimum else {
    r <- config$p3_alloc_ratio
    max(2L * minimum, ceiling(minimum * (r + 1)), ceiling(minimum * (r + 1) / r))
  }
  if (is.null(p3_n_grid)) {
    # Auto grid based on current P3 N
    base_n <- if (!is.null(config$p3_n)) config$p3_n else 100
    p3_n_grid <- unique(pmax(minimum_total,
      round(seq(base_n * 0.5, base_n * 2, length.out = 10))))
  }
  if (!is.numeric(p3_n_grid) || !length(p3_n_grid) ||
      any(!is.finite(p3_n_grid)) || any(p3_n_grid != floor(p3_n_grid)) ||
      any(p3_n_grid < minimum_total)) {
    stop("p3_n_grid must contain valid integer totals that allocate the minimum per arm",
         call. = FALSE)
  }

  results <- data.frame()
  for (n_val in p3_n_grid) {
    cfg_tmp <- config
    cfg_tmp$p3_n <- n_val
    res <- compute_ppos(cfg_tmp, n_mc = n_mc)
    results <- rbind(results, data.frame(
      p3_n = n_val, ppos = res$ppos,
      stringsAsFactors = FALSE))
  }
  return(results)
}

cat("ppos.R loaded.\n")
