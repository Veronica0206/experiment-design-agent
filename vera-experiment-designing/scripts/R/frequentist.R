###############################################################################
# frequentist.R
# Generic Quantitative Decision Framework — Frequentist Sensitivity Analysis
###############################################################################

# =============================================================================
# BINARY: SINGLE-ARM (Exact Binomial Test)
# =============================================================================

freq_binary_single_arm <- function(x, n, null_param, alpha = 0.05) {
  res <- binom.test(x, n, p = null_param, alternative = "greater",
                    conf.level = 1 - alpha)
  list(
    test      = "exact_binomial",
    x         = x, n = n,
    obs_rate  = round(x / n, 4),
    null      = null_param,
    p_value   = round(res$p.value, 6),
    ci_lower  = round(res$conf.int[1], 4),
    ci_upper  = round(res$conf.int[2], 4),
    reject_h0 = res$p.value <= alpha,
    alpha     = alpha,
    conf_level = 1 - alpha
  )
}

# =============================================================================
# BINARY: TWO-ARM (Fisher, Barnard, Chi-squared)
# =============================================================================

.freq_exact_available <- function() {
  base::requireNamespace("Exact", quietly = TRUE)
}

.freq_barnard_z_pooled <- function(data) {
  Exact::exact.test(
    data,
    alternative = "greater",
    method = "z-pooled",
    model = "Binomial",
    cond.row = TRUE,
    to.plot = FALSE
  )
}

freq_binary_two_arm <- function(x_trt, n_trt, x_ctrl, n_ctrl, alpha = 0.05) {
  mat <- matrix(c(x_trt, n_trt - x_trt, x_ctrl, n_ctrl - x_ctrl),
                nrow = 2, byrow = TRUE)

  # Fisher's exact
  fisher_res <- fisher.test(mat, alternative = "greater")

  # Chi-squared (with Yates correction)
  chisq_res <- prop.test(c(x_trt, x_ctrl), c(n_trt, n_ctrl),
                         alternative = "greater", correct = TRUE)

  # Barnard-type unconditional exact test with Z-pooled (score) ordering. This
  # matches the ordering statistic used by power_unconditional_exact(). The
  # Exact package calls this method "z-pooled"; "Barnard" is not a supported
  # method value. ExactData is needed for the optional CSM ordering, not for
  # the Z-pooled test used here.
  barnard_p <- NA_real_
  barnard_status <- "dependency_unavailable"
  barnard_failure_reason <- "R package 'Exact' is not installed"
  if (.freq_exact_available()) {
    barnard_attempt <- tryCatch(
      list(result = .freq_barnard_z_pooled(mat), error = NULL),
      error = function(e) list(result = NULL, error = conditionMessage(e))
    )
    if (!is.null(barnard_attempt$error)) {
      barnard_status <- "computation_failed"
      barnard_failure_reason <- barnard_attempt$error
    } else {
      candidate <- barnard_attempt$result$p.value
      if (is.numeric(candidate) && length(candidate) == 1L &&
          is.finite(candidate) && candidate >= 0 && candidate <= 1) {
        barnard_p <- as.numeric(candidate)
        barnard_status <- "computed"
        barnard_failure_reason <- NA_character_
      } else {
        barnard_status <- "invalid_result"
        barnard_failure_reason <-
          "Exact::exact.test returned a non-finite or out-of-range p-value"
      }
    }
  }

  rate_trt  <- x_trt / n_trt
  rate_ctrl <- x_ctrl / n_ctrl
  diff      <- rate_trt - rate_ctrl

  list(
    rate_trt    = round(rate_trt, 4),
    rate_ctrl   = round(rate_ctrl, 4),
    diff        = round(diff, 4),
    p_fisher    = round(fisher_res$p.value, 6),
    p_chisq     = round(chisq_res$p.value, 6),
    p_barnard   = if (!is.na(barnard_p)) round(barnard_p, 6) else NA,
    reject_fisher  = fisher_res$p.value <= alpha,
    reject_chisq   = chisq_res$p.value <= alpha,
    reject_barnard = if (!is.na(barnard_p)) barnard_p <= alpha else NA,
    barnard_test   = "unconditional_exact_z_pooled",
    barnard_method = "Exact::exact.test(method='z-pooled')",
    barnard_status = barnard_status,
    barnard_failure_reason = barnard_failure_reason,
    alpha          = alpha
  )
}

# =============================================================================
# CONTINUOUS: SINGLE-ARM (One-sample t-test)
# =============================================================================

freq_continuous_single_arm <- function(x_bar, sd, n, null_param, alpha = 0.05) {
  if (!is.numeric(sd) || length(sd) != 1L || !is.finite(sd) || sd <= 0) {
    stop("one-sample t inference requires a finite, positive observed SD",
         call. = FALSE)
  }
  se <- sd / sqrt(n)
  t_stat <- (x_bar - null_param) / se
  df <- n - 1
  p_value <- 1 - pt(t_stat, df)
  ci_lo <- x_bar - qt(1 - alpha, df) * se  # one-sided lower bound
  ci_hi <- Inf

  list(
    test      = "one_sample_t",
    x_bar     = x_bar, sd = sd, n = n,
    null      = null_param,
    t_stat    = round(t_stat, 4),
    df        = df,
    p_value   = round(p_value, 6),
    ci_lower  = round(ci_lo, 4),
    reject_h0 = p_value <= alpha,
    alpha     = alpha
  )
}

# =============================================================================
# CONTINUOUS: TWO-ARM (Welch's t-test)
# =============================================================================

freq_continuous_two_arm <- function(x_bar_trt, sd_trt, n_trt,
                                    x_bar_ctrl, sd_ctrl, n_ctrl,
                                    alpha = 0.05) {
  if (any(!is.finite(c(sd_trt, sd_ctrl))) || sd_trt <= 0 || sd_ctrl <= 0) {
    stop("Welch t inference requires finite, positive observed SDs in both arms",
         call. = FALSE)
  }
  diff <- x_bar_trt - x_bar_ctrl
  se   <- sqrt(sd_trt^2 / n_trt + sd_ctrl^2 / n_ctrl)
  # Welch-Satterthwaite degrees of freedom
  df_num <- (sd_trt^2 / n_trt + sd_ctrl^2 / n_ctrl)^2
  df_den <- (sd_trt^2 / n_trt)^2 / (n_trt - 1) +
            (sd_ctrl^2 / n_ctrl)^2 / (n_ctrl - 1)
  df <- df_num / df_den

  t_stat  <- diff / se
  p_value <- 1 - pt(t_stat, df)
  ci_lo   <- diff - qt(1 - alpha, df) * se

  list(
    test       = "welch_t",
    diff       = round(diff, 4),
    se         = round(se, 4),
    t_stat     = round(t_stat, 4),
    df         = round(df, 2),
    p_value    = round(p_value, 6),
    ci_lower   = round(ci_lo, 4),
    reject_h0  = p_value <= alpha,
    alpha      = alpha
  )
}

# =============================================================================
# TTE: SINGLE-ARM (exponential rate test via Poisson on events)
# =============================================================================

freq_tte_single_arm <- function(events, person_time, null_lambda, alpha = 0.025) {
  # Exact test: events ~ Poisson(null_lambda * person_time) under H0
  # One-sided: H1: lambda < null_lambda (lower hazard = better)
  expected <- null_lambda * person_time
  p_value <- ppois(events, lambda = expected)  # P(X <= observed | H0)
  obs_rate <- events / person_time

  list(
    test        = "exponential_rate",
    events      = events, person_time = round(person_time, 2),
    obs_lambda  = round(obs_rate, 6),
    null_lambda = null_lambda,
    p_value     = round(p_value, 6),
    reject_h0   = p_value <= alpha,
    alpha       = alpha
  )
}

# =============================================================================
# TTE: TWO-ARM (log-rank normal approximation)
# =============================================================================

freq_tte_two_arm <- function(events_trt, pt_trt, events_ctrl, pt_ctrl,
                             alpha = 0.025) {
  events_trt <- max(events_trt, 0.5)
  events_ctrl <- max(events_ctrl, 0.5)
  lam_t <- events_trt / pt_trt
  lam_c <- events_ctrl / pt_ctrl
  HR <- lam_t / lam_c
  log_hr <- log(HR)
  se_log_hr <- sqrt(1 / events_trt + 1 / events_ctrl)
  z_stat <- log_hr / se_log_hr
  p_value <- pnorm(z_stat)  # one-sided: H1: HR < 1

  list(
    test       = "logrank_approx",
    HR         = round(HR, 4),
    log_HR     = round(log_hr, 4),
    se_log_HR  = round(se_log_hr, 4),
    z_stat     = round(z_stat, 4),
    p_value    = round(p_value, 6),
    reject_h0  = p_value <= alpha,
    alpha      = alpha
  )
}

# =============================================================================
# INCIDENCE RATE: SINGLE-ARM (exact Poisson test)
# =============================================================================

freq_rate_single_arm <- function(count, exposure, null_rate, alpha = 0.025,
                                 direction = "less") {
  expected <- null_rate * exposure
  # direction "less":    H1: rate < null_rate (protective — treatment reduces rate)
  # direction "greater": H1: rate > null_rate (harm detection — rate increase)
  p_value <- if (direction == "greater") {
    ppois(count - 1, lambda = expected, lower.tail = FALSE)  # P(X >= observed | H0)
  } else {
    ppois(count, lambda = expected)                          # P(X <= observed | H0)
  }
  obs_rate <- count / exposure

  list(
    test      = "exact_poisson",
    direction = direction,
    count     = count, exposure = round(exposure, 2),
    obs_rate  = round(obs_rate, 6),
    null_rate = null_rate,
    p_value   = round(p_value, 6),
    reject_h0 = p_value <= alpha,
    alpha     = alpha
  )
}

# =============================================================================
# INCIDENCE RATE: TWO-ARM (rate-difference Wald Z-test)
# =============================================================================

freq_rate_two_arm <- function(count_trt, exp_trt, count_ctrl, exp_ctrl,
                              alpha = 0.025, direction = "less") {
  count_trt  <- max(count_trt, 0.5)
  count_ctrl <- max(count_ctrl, 0.5)
  rate_t <- count_trt / exp_trt
  rate_c <- count_ctrl / exp_ctrl
  diff <- rate_t - rate_c
  se <- sqrt(rate_t / exp_trt + rate_c / exp_ctrl)
  z_stat <- diff / se
  # direction "less":    H1: rate_trt < rate_ctrl (protective)
  # direction "greater": H1: rate_trt > rate_ctrl (harm detection)
  p_value <- if (direction == "greater") {
    pnorm(z_stat, lower.tail = FALSE)
  } else {
    pnorm(z_stat)
  }

  list(
    test       = "poisson_rate_difference_wald",
    legacy_test = "poisson_rate_ratio",
    estimand   = "rate_difference",
    direction  = direction,
    rate_trt   = round(rate_t, 6),
    rate_ctrl  = round(rate_c, 6),
    diff       = round(diff, 6),
    z_stat     = round(z_stat, 4),
    p_value    = round(p_value, 6),
    reject_h0  = p_value <= alpha,
    alpha      = alpha
  )
}

# =============================================================================
# UNIFIED DISPATCHER: compute_freq_decision(config, data)
# =============================================================================

#' Compute frequentist sensitivity analysis
#'
#' @param config A qdf_config object
#' @param data   Observed data (same format as compute_decision in bayesian.R)
#' @param alpha  One-sided alpha for testing (default: smallest alpha in config)
#' @return list with test results
compute_freq_decision <- function(config, data, alpha = NULL) {
  validate_observed_data(config, data)
  if (is.null(alpha)) alpha <- min(config$alphas)

  if (config$endpoint_type == "binary") {
    if (config$design == "single_arm") {
      freq_binary_single_arm(data$x, data$n, config$null_param, alpha)
    } else {
      freq_binary_two_arm(data$x_trt, data$n_trt,
                          data$x_ctrl, data$n_ctrl, alpha)
    }
  } else if (config$endpoint_type == "continuous") {
    resolve_sd <- function(sd_value, variance_value) {
      if (!is.null(sd_value)) {
        if (!is.numeric(sd_value) || length(sd_value) != 1L ||
            !is.finite(sd_value) || sd_value <= 0) {
          stop("frequentist continuous sensitivity requires a positive finite SD",
               call. = FALSE)
        }
        return(sd_value)
      }
      if (!is.numeric(variance_value) || length(variance_value) != 1L ||
          !is.finite(variance_value) || variance_value <= 0) {
        stop("frequentist continuous sensitivity requires a positive finite variance",
             call. = FALSE)
      }
      sqrt(variance_value)
    }
    if (config$design == "single_arm") {
      sd_val <- resolve_sd(data$sd, data$s2)
      freq_continuous_single_arm(data$x_bar, sd_val, data$n,
                                 config$null_param, alpha)
    } else {
      sd_ctrl <- resolve_sd(data$sd_ctrl, data$s2_ctrl)
      sd_trt  <- resolve_sd(data$sd_trt, data$s2_trt)
      freq_continuous_two_arm(data$x_bar_trt, sd_trt, data$n_trt,
                              data$x_bar_ctrl, sd_ctrl, data$n_ctrl, alpha)
    }
  } else if (config$endpoint_type == "tte") {
    if (config$design == "single_arm") {
      freq_tte_single_arm(data$events, data$person_time,
                          config$null_param, alpha)
    } else {
      freq_tte_two_arm(data$events_trt, data$pt_trt,
                       data$events_ctrl, data$pt_ctrl, alpha)
    }
  } else if (config$endpoint_type == "incidence_rate") {
    # Incidence rate is the one endpoint where both directions are valid:
    # protective (alt < null) tests the lower tail, harm detection (alt > null)
    # the upper tail. Derive from the config so the analysis always matches
    # the direction the design was sized for.
    direction <- if (!is.null(config$direction)) config$direction
                 else if (config$alt_param > config$null_param) "greater" else "less"
    if (config$design == "single_arm") {
      freq_rate_single_arm(data$count, data$exposure,
                           config$null_param, alpha, direction = direction)
    } else {
      freq_rate_two_arm(data$count_trt, data$exp_trt,
                        data$count_ctrl, data$exp_ctrl, alpha,
                        direction = direction)
    }
  }
}

cat("frequentist.R loaded.\n")
