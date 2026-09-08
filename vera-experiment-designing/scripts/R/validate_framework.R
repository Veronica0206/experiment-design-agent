cat("=== vera-experiment-designing smoke test ===\n")
# Resolve modules from this file so the smoke check works from any directory.
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
  stop("Cannot determine validate_framework.R location", call. = FALSE)
}
source(file.path(resolve_script_dir(), "run_framework.R"))

# Binary single-arm
cfg <- create_config(endpoint_type = "binary", study_type = "poc",
                     design = "single_arm", null_param = 0.20, alt_param = 0.50)
ss <- ss_binomial_single_arm(0.20, 0.50, alpha = 0.025, power = 0.80)
stopifnot(ss$n > 0)
cat("  binary single-arm: N =", ss$n, "\n")

# Continuous two-arm
cfg2 <- create_config(endpoint_type = "continuous", study_type = "poc",
                      design = "controlled", null_param = 0, alt_param = 5, sd = 10)
ss2 <- ss_ttest_two_arm(delta = 5, sd = 10, alpha = 0.025, power = 0.80,
                        alloc_ratio = 1)
stopifnot(ss2$n_total > 0)
cat("  continuous two-arm: N_total =", ss2$n_total, "\n")

# TTE single-arm
cfg3 <- create_config(endpoint_type = "tte", study_type = "poc",
                      design = "single_arm", null_param = log(2) / 4,
                      alt_param = log(2) / 6, accrual_time = 18,
                      followup_time = 12)
ss3 <- ss_logrank_single_arm(lambda0 = log(2) / 4, lambda1 = log(2) / 6,
                             alpha = 0.025, power = 0.80,
                             accrual_time = 18, followup_time = 12)
stopifnot(ss3$n > 0)
cat("  tte single-arm: N =", ss3$n, "\n")

# Incidence rate two-arm
cfg4 <- create_config(endpoint_type = "incidence_rate", study_type = "poc",
                      design = "controlled", null_param = 0.10,
                      alt_param = 0.05, exposure_time = 1)
ss4 <- ss_poisson_two_arm(lambda0 = 0.10, lambda1 = 0.05, alpha = 0.025,
                          power = 0.80, exposure_time = 1, alloc_ratio = 1)
stopifnot(ss4$n_total > 0)
cat("  incidence-rate two-arm: N_total =", ss4$n_total, "\n")

cat("VALIDATE_FRAMEWORK=TRUE\n")
