###############################################################################
# run_framework.R
# Generic Quantitative Decision Framework — Master Entry Point
#
# Updated to use: z_unpooled (binary controlled), unconditional_exact (Barnard-type),
#   power_* functions for fixed-N power, get_arms() helper
# Alpha convention: alpha = one-sided throughout (e.g., 0.025)
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
source(file.path(framework_dir, "config.R"))
source(file.path(framework_dir, "sample_size.R"))
source(file.path(framework_dir, "bayesian.R"))
source(file.path(framework_dir, "frequentist.R"))
source(file.path(framework_dir, "ppos.R"))

#' Compute sample sizes for all alpha/power combinations
#'
#' @param config  A qdf_config object from create_config()
#' @return data.frame with columns: design, test, alpha, power_target,
#'         n_total, n_trt, n_ctrl, power_achieved, k_crit
compute_sample_size <- function(config) {
  results <- data.frame()

  for (a in config$alphas) {
    for (pwr in config$powers) {

      if (config$endpoint_type == "binary") {
        p0 <- config$null_param; p1 <- config$alt_param

        # Single-arm: exact binomial
        sa <- ss_binomial_single_arm(p0, p1, alpha = a, power = pwr)
        results <- rbind(results, data.frame(
          design = "single_arm", test = "exact_binomial",
          alpha = a, power_target = pwr,
          n_total = sa$n, n_trt = sa$n, n_ctrl = NA,
          power_achieved = sa$power, k_crit = sa$k_crit,
          stringsAsFactors = FALSE))

        if (config$design == "controlled") {
          zu <- ss_z_unpooled(p0, p1, a, pwr, config$alloc_ratio)
          results <- rbind(results, data.frame(
            design = "controlled", test = "z_unpooled",
            alpha = a, power_target = pwr,
            n_total = zu$n_total, n_trt = zu$n_trt, n_ctrl = zu$n_ctrl,
            power_achieved = zu$power, k_crit = NA,
            stringsAsFactors = FALSE))
        }

      } else if (config$endpoint_type == "continuous") {
        delta <- config$alt_param - config$null_param

        sa <- ss_ttest_single_arm(delta, config$sd, a, pwr)
        results <- rbind(results, data.frame(
          design = "single_arm", test = "one_sample_t",
          alpha = a, power_target = pwr,
          n_total = sa$n, n_trt = sa$n, n_ctrl = NA,
          power_achieved = pwr, k_crit = NA,
          stringsAsFactors = FALSE))

        if (config$design == "controlled") {
          ta <- ss_ttest_two_arm(delta, config$sd, a, pwr, config$alloc_ratio)
          results <- rbind(results, data.frame(
            design = "controlled", test = ta$test,
            alpha = a, power_target = pwr,
            n_total = ta$n_total, n_trt = ta$n_trt, n_ctrl = ta$n_ctrl,
            power_achieved = pwr, k_crit = NA,
            stringsAsFactors = FALSE))
        }

      } else if (config$endpoint_type == "tte") {
        lam0 <- config$null_param; lam1 <- config$alt_param
        acr <- config$accrual_time; fu <- config$followup_time

        sa <- ss_logrank_single_arm(lam0, lam1, acr, fu, a, pwr)
        results <- rbind(results, data.frame(
          design = "single_arm", test = "exponential_rate",
          alpha = a, power_target = pwr,
          n_total = sa$n, n_trt = sa$n, n_ctrl = NA,
          power_achieved = sa$power, k_crit = sa$events,
          stringsAsFactors = FALSE))

        if (config$design == "controlled") {
          lr <- ss_logrank_two_arm(lam0, lam1, acr, fu, a, pwr, config$alloc_ratio)
          results <- rbind(results, data.frame(
            design = "controlled", test = "logrank",
            alpha = a, power_target = pwr,
            n_total = lr$n_total, n_trt = lr$n_trt, n_ctrl = lr$n_ctrl,
            power_achieved = lr$power, k_crit = lr$events,
            stringsAsFactors = FALSE))
        }

      } else if (config$endpoint_type == "incidence_rate") {
        lam0 <- config$null_param; lam1 <- config$alt_param
        T_exp <- config$exposure_time

        sa <- ss_poisson_single_arm(lam0, lam1, T_exp, a, pwr,
                                    direction = config$direction)
        results <- rbind(results, data.frame(
          design = "single_arm", test = "exact_poisson",
          alpha = a, power_target = pwr,
          n_total = sa$n, n_trt = sa$n, n_ctrl = NA,
          power_achieved = sa$power, k_crit = sa$k_crit,
          stringsAsFactors = FALSE))

        if (config$design == "controlled") {
          pr <- ss_poisson_two_arm(lam0, lam1, T_exp, a, pwr, config$alloc_ratio)
          results <- rbind(results, data.frame(
            design = "controlled", test = pr$test,
            alpha = a, power_target = pwr,
            n_total = pr$n_total, n_trt = pr$n_trt, n_ctrl = pr$n_ctrl,
            power_achieved = pr$power, k_crit = NA,
            stringsAsFactors = FALSE))
        }
      }
    }
  }

  results$label <- config$label
  return(results)
}

#' Run the full quantitative decision framework
#'
#' @param config        A qdf_config object
#' @param observed_data Observed data for Go/No-Go (non-confirmatory)
#' @param delta         Optional controlled effect target. NULL derives it from
#'                      go_target - null_param for binary/continuous endpoints.
#' @param B_oc          Number of OC simulations
#' @param n_oc          Sample size for OC curves (NULL = auto from sample_size output)
#' @param output_dir    If provided, save CSVs + PDFs to this directory
#' @param n_mc_ppos     Posterior draws for PPOS (default 100000)
#' @param n_mc_sensitivity Posterior draws per PPOS sensitivity point (default 50000)
#' @return list of all results
run_quantitative_framework <- function(config,
                                       observed_data = NULL,
                                       delta = NULL,
                                       B_oc = 5000,
                                       n_oc = NULL,
                                       output_dir = NULL,
                                       n_mc_ppos = 100000,
                                       n_mc_sensitivity = 50000) {
  stopifnot(inherits(config, "qdf_config"))
  if (!is.numeric(B_oc) || length(B_oc) != 1L || !is.finite(B_oc) ||
      B_oc < 1 || B_oc != floor(B_oc)) {
    stop("B_oc must be one positive integer", call. = FALSE)
  }
  .qdf_scalar(n_mc_ppos, "n_mc_ppos", lower = 2, integer = TRUE)
  .qdf_scalar(n_mc_sensitivity, "n_mc_sensitivity", lower = 2, integer = TRUE)
  cat("\n=== Quantitative Decision Framework ===\n")
  print(config)

  results <- list(config = config)

  # --- 1. SAMPLE SIZE (always) ---
  cat("\n--- Sample Size Calculations ---\n")
  ss <- compute_sample_size(config)
  results$sample_size <- ss
  print(ss[, c("design", "test", "alpha", "power_target", "n_total",
               "n_trt", "n_ctrl")], row.names = FALSE)

  # --- 2. NON-CONFIRMATORY: Go/No-Go ---
  if (config$study_type %in% c("signal_detection", "poc") && !is.null(observed_data)) {
    cat("\n--- Bayesian Go/No-Go Decision ---\n")
    bayes_dec <- compute_decision(config, observed_data, delta)
    results$bayesian_decision <- bayes_dec
    cat("  Posterior prob:", bayes_dec$posterior_prob, "\n")
    cat("  Decision:     ", bayes_dec$decision, "\n")
    cat("  Note:         ", bayes_dec$note, "\n")

    # Frequentist sensitivity
    cat("\n--- Frequentist Sensitivity ---\n")
    freq_dec <- compute_freq_decision(config, observed_data)
    results$frequentist_decision <- freq_dec
    if (!is.null(freq_dec$p_value)) {
      cat("  p-value:", freq_dec$p_value, "\n")
      cat("  Reject H0:", freq_dec$reject_h0, "\n")
    } else {
      cat("  Fisher p:", freq_dec$p_fisher, "| Reject:", freq_dec$reject_fisher, "\n")
      cat("  Chi-sq p:", freq_dec$p_chisq, "| Reject:", freq_dec$reject_chisq, "\n")
      if (!is.na(freq_dec$p_barnard)) {
        cat("  Barnard-type Z-pooled p:", freq_dec$p_barnard,
            "| Reject:", freq_dec$reject_barnard, "\n")
      } else {
        cat("  Barnard-type Z-pooled test: not computed (",
            freq_dec$barnard_status, ") — ",
            freq_dec$barnard_failure_reason, "\n", sep = "")
      }
    }

    # Operating characteristics
    cat("\n--- Operating Characteristics ---\n")
    if (is.null(n_oc)) {
      if (config$design == "controlled") {
        ss_ctrl <- results$sample_size[results$sample_size$design == "controlled", , drop = FALSE]
        if (!is.null(observed_data$n_trt)) {
          n_oc <- list(n_trt = observed_data$n_trt, n_ctrl = observed_data$n_ctrl)
        } else if (nrow(ss_ctrl) > 0) {
          # First controlled row = primary alpha x first power target;
          # scalars only (whole columns would mix single-arm NA rows in)
          n_oc <- list(n_trt = ss_ctrl$n_trt[1], n_ctrl = ss_ctrl$n_ctrl[1])
        } else {
          n_oc <- list(n_trt = 50, n_ctrl = 50)
        }
      } else {
        n_oc <- if (!is.null(observed_data$n)) observed_data$n else 10
      }
    }
    B_used <- min(B_oc, 5000)
    oc <- compute_oc(config, n = n_oc, B = B_used, delta = delta)
    results$oc <- oc
    results$B_used <- B_used
    results$n_oc_used <- n_oc
    if (is.list(n_oc)) {
      cat("  Computed for N_trt =", n_oc$n_trt, ", N_ctrl =", n_oc$n_ctrl, "\n")
    } else {
      cat("  Computed for N =", n_oc, "\n")
    }
  }

  # --- 3. CONFIRMATORY: PPOS ---
  if (config$study_type == "confirmatory" && !is.null(config$p2_data)) {
    cat("\n--- Pre-Study Assurance (PPOS) ---\n")
    ppos_res <- compute_ppos(config, n_mc = n_mc_ppos)
    results$ppos <- ppos_res
    cat("  PPOS:              ", ppos_res$ppos, "\n")
    cat("  Cond. Power (mean):", ppos_res$cond_power_mean, "\n")
    cat("  Cond. Power (med): ", ppos_res$cond_power_median, "\n")
    cat("  P3 N:              ", ppos_res$p3_n, "\n")

    # PPOS sensitivity across P3 sample sizes
    cat("\n--- PPOS Sensitivity ---\n")
    ppos_sens <- ppos_sensitivity(config, n_mc = n_mc_sensitivity)
    results$ppos_sensitivity <- ppos_sens
    print(ppos_sens, row.names = FALSE)
  }

  # --- 4. FIXED-N POWER (V2) ---
  if (config$study_type %in% c("signal_detection", "poc")) {
    cat("\n--- Fixed-N Power (V2) ---\n")
    n_grid <- seq(20, 60, by = 10)
    v2 <- data.frame()

    for (n_val in n_grid) {
      for (a in config$alphas) {
        if (config$endpoint_type == "binary") {
          p0 <- config$null_param; p1 <- config$alt_param
          if (config$design == "single_arm") {
            pwr <- power_binomial_single_arm(n_val, p0, p1, a)
            v2 <- rbind(v2, data.frame(design = "single_arm", test = "exact_binomial",
                                       n_total = n_val, alpha = a, power = round(pwr, 4),
                                       stringsAsFactors = FALSE))
          } else {
            arms <- get_arms(n_val, paste0("controlled_", config$alloc_ratio, "_1"))
            pwr_z <- power_z_unpooled(arms$n_trt, arms$n_ctrl, p0, p1, a)
            v2 <- rbind(v2, data.frame(design = "controlled", test = "z_unpooled",
                                       n_total = n_val, alpha = a, power = round(pwr_z, 4),
                                       stringsAsFactors = FALSE))
          }
        } else if (config$endpoint_type == "continuous") {
          delta <- config$alt_param - config$null_param
          if (config$design == "single_arm") {
            pwr <- power_ttest_single_arm(n_val, delta, config$sd, a)
            v2 <- rbind(v2, data.frame(design = "single_arm", test = "one_sample_t",
                                       n_total = n_val, alpha = a, power = round(pwr, 4),
                                       stringsAsFactors = FALSE))
          } else {
            arms <- get_arms(n_val, paste0("controlled_", config$alloc_ratio, "_1"))
            pwr <- power_ttest_two_arm(arms$n_trt, arms$n_ctrl, delta, config$sd, a)
            method <- if (arms$n_trt == arms$n_ctrl) "two_sample_t" else
              "two_sample_z_normal_approximation"
            v2 <- rbind(v2, data.frame(design = "controlled", test = method,
                                       n_total = n_val, alpha = a, power = round(pwr, 4),
                                       stringsAsFactors = FALSE))
          }
        } else if (config$endpoint_type == "tte") {
          lam0 <- config$null_param; lam1 <- config$alt_param
          if (config$design == "single_arm") {
            pwr <- power_logrank_single_arm(n_val, lam0, lam1,
                                            config$accrual_time, config$followup_time, a)
            v2 <- rbind(v2, data.frame(design = "single_arm", test = "exponential_rate",
                                       n_total = n_val, alpha = a, power = round(pwr, 4),
                                       stringsAsFactors = FALSE))
          } else {
            arms <- get_arms(n_val, paste0("controlled_", config$alloc_ratio, "_1"))
            pwr <- power_logrank_two_arm(arms$n_trt, arms$n_ctrl, lam0, lam1,
                                         config$accrual_time, config$followup_time, a)
            v2 <- rbind(v2, data.frame(design = "controlled", test = "logrank",
                                       n_total = n_val, alpha = a, power = round(pwr, 4),
                                       stringsAsFactors = FALSE))
          }
        } else if (config$endpoint_type == "incidence_rate") {
          lam0 <- config$null_param; lam1 <- config$alt_param
          direction <- config$direction
          if (config$design == "single_arm") {
            pwr <- power_poisson_single_arm(n_val, lam0, lam1, config$exposure_time, a, direction)
            v2 <- rbind(v2, data.frame(design = "single_arm", test = "exact_poisson",
                                       n_total = n_val, alpha = a, power = round(pwr, 4),
                                       stringsAsFactors = FALSE))
          } else {
            arms <- get_arms(n_val, paste0("controlled_", config$alloc_ratio, "_1"))
            pwr <- power_poisson_two_arm(arms$n_trt, arms$n_ctrl, lam0, lam1,
                                         config$exposure_time, a)
            v2 <- rbind(v2, data.frame(design = "controlled",
                                       test = "poisson_rate_difference_wald",
                                       n_total = n_val, alpha = a, power = round(pwr, 4),
                                       stringsAsFactors = FALSE))
          }
        }
      }
    }
    results$v2_power <- v2
    cat("  Computed power for N =", paste(n_grid, collapse = ", "), "\n")
  }

  # --- 5. SAVE OUTPUTS ---
  if (!is.null(output_dir)) {
    dir.create(output_dir, showWarnings = FALSE, recursive = TRUE)
    cat("\n--- Saving Outputs ---\n")

    # Sample size table
    write.csv(ss, file.path(output_dir, "sample_size.csv"), row.names = FALSE)
    cat("  Saved: sample_size.csv\n")

    # V2 power table
    if (!is.null(results$v2_power)) {
      write.csv(results$v2_power, file.path(output_dir, "v2_power.csv"), row.names = FALSE)
      cat("  Saved: v2_power.csv\n")
    }

    # OC table
    if (!is.null(results$oc)) {
      write.csv(results$oc, file.path(output_dir, "oc_curves.csv"), row.names = FALSE)
      cat("  Saved: oc_curves.csv\n")
    }

    # PPOS sensitivity
    if (!is.null(results$ppos_sensitivity)) {
      write.csv(results$ppos_sensitivity,
                file.path(output_dir, "ppos_sensitivity.csv"), row.names = FALSE)
      cat("  Saved: ppos_sensitivity.csv\n")
    }

    # Visualizations
    save_plots(config, results, output_dir)
  }

  cat("\n=== Done ===\n")
  return(invisible(results))
}

# =============================================================================
# VISUALIZATION HELPER
# =============================================================================

save_plots <- function(config, results, output_dir) {

  # --- Power Curve ---
  if (config$endpoint_type == "binary") {
    pdf(file.path(output_dir, "power_curve.pdf"), width = 8, height = 6)
    ns <- 5:100
    p0 <- config$null_param; p1 <- config$alt_param
    colors <- c("darkred", "darkorange", "steelblue")

    pwr_list <- lapply(config$alphas, function(a) {
      sapply(ns, function(n) power_binomial_single_arm(n, p0, p1, a))
    })

    plot(ns, pwr_list[[1]], type = "l", lwd = 2.5, col = colors[1],
         xlab = "N", ylab = "Power",
         main = paste0(config$label, " (H0=", p0, ", H1=", p1, ")"),
         ylim = c(0, 1))
    for (i in seq_along(config$alphas)[-1]) {
      if (i <= length(pwr_list)) lines(ns, pwr_list[[i]], lwd = 2.5, col = colors[i])
    }
    abline(h = config$powers, lty = 2, col = "gray50")
    legend("bottomright",
           legend = paste0("alpha=", config$alphas),
           col = colors[1:length(config$alphas)], lwd = 2.5, cex = 0.8)
    dev.off()
    cat("  Saved: power_curve.pdf\n")
  }

  # --- OC Curve ---
  if (!is.null(results$oc)) {
    pdf(file.path(output_dir, "oc_curve.pdf"), width = 8, height = 6)
    oc <- results$oc
    xlab <- switch(config$endpoint_type,
      binary = "True Response Rate",
      continuous = "True Mean Change",
      tte = "True Hazard Rate",
      incidence_rate = "True Event Rate"
    )

    plot(oc$true_param, oc$p_go, type = "l", lwd = 2.5, col = "green4",
         xlab = xlab, ylab = "Probability",
         main = paste0(config$label, " - Operating Characteristics"),
         ylim = c(0, 1))
    lines(oc$true_param, oc$p_consider, lwd = 2.5, col = "darkorange")
    lines(oc$true_param, oc$p_nogo, lwd = 2.5, col = "red3")
    abline(v = c(config$null_param, config$alt_param), lty = 2, col = "gray50")
    legend("topleft",
           legend = c("P(Go)", "P(Consider)", "P(No-Go)"),
           col = c("green4", "darkorange", "red3"), lwd = 2.5, cex = 0.8)
    dev.off()
    cat("  Saved: oc_curve.pdf\n")
  }

  # --- V2 Power Curves ---
  if (!is.null(results$v2_power)) {
    pdf(file.path(output_dir, "v2_power_curves.pdf"), width = 10, height = 6)
    v2 <- results$v2_power
    colors <- c("darkred", "darkorange", "steelblue")
    plot(NULL, xlim = range(v2$n_total), ylim = c(0, 1),
         xlab = "N total", ylab = "Power",
         main = paste0(config$label, " - Power vs N"))
    abline(h = c(0.80, 0.90), lty = 2, col = "gray60")
    for (i in seq_along(config$alphas)) {
      d <- v2[v2$alpha == config$alphas[i], ]
      lines(d$n_total, d$power, col = colors[min(i, 3)], lwd = 2)
      points(d$n_total, d$power, col = colors[min(i, 3)], pch = 19, cex = 0.8)
    }
    legend("bottomright", paste0("alpha=", config$alphas),
           col = colors[1:min(length(config$alphas), 3)], lwd = 2, cex = 0.7)
    dev.off()
    cat("  Saved: v2_power_curves.pdf\n")
  }

  # --- PPOS Sensitivity Curve ---
  if (!is.null(results$ppos_sensitivity)) {
    pdf(file.path(output_dir, "ppos_sensitivity.pdf"), width = 8, height = 6)
    ps <- results$ppos_sensitivity
    plot(ps$p3_n, ps$ppos, type = "b", lwd = 2.5, col = "steelblue",
         pch = 16, xlab = "Confirmatory-stage N", ylab = "PPOS (Assurance)",
         main = paste0(config$label, " - PPOS vs. Confirmatory-stage Sample Size"),
         ylim = c(0, 1))
    abline(h = c(0.5, 0.8), lty = 2, col = c("gray60", "red3"))
    text(max(ps$p3_n), 0.82, "80% threshold", cex = 0.8, col = "red3", pos = 2)
    dev.off()
    cat("  Saved: ppos_sensitivity.pdf\n")
  }

  # --- Posterior + PPOS Distribution ---
  if (!is.null(results$ppos)) {
    pdf(file.path(output_dir, "ppos_distribution.pdf"), width = 8, height = 6)
    cp <- results$ppos$cond_power_draws
    hist(cp, breaks = 50, col = "steelblue", border = "white",
         main = paste0(config$label, " - Conditional Power Distribution\nPPOS = ",
                       results$ppos$ppos),
         xlab = "Conditional Power of Confirmatory-stage",
         xlim = c(0, 1), freq = FALSE)
    abline(v = results$ppos$ppos, col = "red3", lwd = 2, lty = 2)
    text(results$ppos$ppos, par("usr")[4] * 0.9,
         paste0("PPOS=", results$ppos$ppos), col = "red3", pos = 4, cex = 0.9)
    dev.off()
    cat("  Saved: ppos_distribution.pdf\n")
  }
}

cat("run_framework.R loaded.\n")
