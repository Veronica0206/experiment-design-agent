###############################################################################
# examples.R
# Quantitative Decision Framework — Synthetic Worked Examples
# QDF_OUTPUT_DIR chooses the output directory. QDF_EXAMPLE_QUICK=1 uses
# small simulation budgets for checking the installation, not precise estimates.
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

script_dir <- resolve_script_dir()
source(file.path(script_dir, "run_framework.R"))

outdir <- Sys.getenv("QDF_OUTPUT_DIR",
                     unset = file.path(getwd(), "qdf-output", "worked-examples"))
quick <- identical(Sys.getenv("QDF_EXAMPLE_QUICK"), "1")
example_B <- if (quick) 32L else 5000L
example_ppos <- if (quick) 500L else 100000L
example_sensitivity <- if (quick) 250L else 50000L
set.seed(42)
cat("Synthetic examples; OC replicates:", example_B,
    "PPOS draws:", example_ppos, "sensitivity draws:", example_sensitivity, "\n")
dir.create(outdir, showWarnings = FALSE, recursive = TRUE)

###############################################################################
# EXAMPLE 1: BINARY, SIGNAL DETECTION, SINGLE-ARM
# Scenario: Early-stage, N=10, testing if response rate > baseline benchmark
###############################################################################

cat("\n\n################################################################\n")
cat("EXAMPLE 1: Binary Signal Detection (Single-Arm)\n")
cat("################################################################\n")

cfg1 <- create_config(
  endpoint_type = "binary",
  study_type    = "signal_detection",
  design        = "single_arm",
  null_param    = 0.50,         # baseline benchmark
  alt_param     = 0.75,         # Target
  prior         = "jeffreys",
  go_target     = 0.60,         # P(theta > 0.60) for decision
  go_threshold  = 0.90,
  consider_threshold = 0.60,
  label         = "Binary Signal Detection (SA)"
)

# Observed: 7/10 responders
res1 <- run_quantitative_framework(
  config = cfg1,
  observed_data = list(x = 7, n = 10),
  B_oc = example_B, n_mc_ppos = example_ppos,
  n_mc_sensitivity = example_sensitivity,
  output_dir = file.path(outdir, "ex1_binary_signal_sa")
)

###############################################################################
# EXAMPLE 2: BINARY, POC, CONTROLLED (2:1)
# Scenario: Mid-stage, full alpha/power grid, Go/No-Go
###############################################################################

cat("\n\n################################################################\n")
cat("EXAMPLE 2: Binary PoC (Controlled 2:1)\n")
cat("################################################################\n")

cfg2 <- create_config(
  endpoint_type = "binary",
  study_type    = "poc",
  design        = "controlled",
  null_param    = 0.10,         # baseline (no effective therapy)
  alt_param     = 0.50,         # Target
  alloc_ratio   = 2,            # 2:1
  prior         = "jeffreys",
  go_target     = 0.30,         # P(diff > 0) for two-arm
  go_threshold  = 0.90,
  consider_threshold = 0.60,
  label         = "Binary PoC (Controlled 2:1)"
)

# Observed: 16/40 (trt) vs 2/20 (ctrl)
res2 <- run_quantitative_framework(
  config = cfg2,
  observed_data = list(x_trt = 16, n_trt = 40, x_ctrl = 2, n_ctrl = 20),
  B_oc = example_B, n_mc_ppos = example_ppos,
  n_mc_sensitivity = example_sensitivity,
  output_dir = file.path(outdir, "ex2_binary_poc_ctrl")
)

###############################################################################
# EXAMPLE 3: BINARY, CONFIRMATORY, CONTROLLED
# Scenario: Confirmatory-stage planning using Exploratory-stage data for PPOS
###############################################################################

cat("\n\n################################################################\n")
cat("EXAMPLE 3: Binary Confirmatory — PPOS\n")
cat("################################################################\n")

cfg3 <- create_config(
  endpoint_type = "binary",
  study_type    = "confirmatory",
  design        = "controlled",
  null_param    = 0.55,         # baseline benchmark response rate
  alt_param     = 0.75,         # Target response rate
  alloc_ratio   = 1,            # 1:1
  prior         = "jeffreys",
  # Exploratory-stage observations
  p2_data       = list(x = 8, n = 10),      # Treatment arm
  p2_data_ctrl  = list(x = 5, n = 10),      # Control arm
  p3_n          = 120,                       # Planned Confirmatory-stage total N
  p3_alloc_ratio = 1,
  p3_alpha      = 0.025,                    # One-sided
  label         = "Binary Confirmatory — PPOS"
)

res3 <- run_quantitative_framework(
  config = cfg3,
  B_oc = example_B, n_mc_ppos = example_ppos,
  n_mc_sensitivity = example_sensitivity,
  output_dir = file.path(outdir, "ex3_binary_confirm_ppos")
)

###############################################################################
# EXAMPLE 4: CONTINUOUS, SIGNAL DETECTION, SINGLE-ARM
# Scenario: Early-stage, N=12, generic continuous summary
###############################################################################

cat("\n\n################################################################\n")
cat("EXAMPLE 4: Continuous Signal Detection (Single-Arm)\n")
cat("################################################################\n")

cfg4 <- create_config(
  endpoint_type = "continuous",
  study_type    = "signal_detection",
  design        = "single_arm",
  null_param    = 5,            # Reference value
  alt_param     = 35,           # Target value
  sd            = 25,           # SD on the analysis scale
  prior         = "jeffreys",
  go_target     = 20,           # P(mu > 20%) for decision
  go_threshold  = 0.90,
  consider_threshold = 0.60,
  label         = "Continuous Signal Detection (SA)"
)

# Observed: summary statistic 30, SD=25, N=12
res4 <- run_quantitative_framework(
  config = cfg4,
  observed_data = list(x_bar = 30, s2 = 625, sd = 25, n = 12),
  B_oc = example_B, n_mc_ppos = example_ppos,
  n_mc_sensitivity = example_sensitivity,
  output_dir = file.path(outdir, "ex4_continuous_signal_sa")
)

###############################################################################
# EXAMPLE 5: CONTINUOUS, CONFIRMATORY, CONTROLLED
# Scenario: Confirmatory-stage planning using Exploratory-stage continuous data
###############################################################################

cat("\n\n################################################################\n")
cat("EXAMPLE 5: Continuous Confirmatory — PPOS\n")
cat("################################################################\n")

cfg5 <- create_config(
  endpoint_type = "continuous",
  study_type    = "confirmatory",
  design        = "controlled",
  null_param    = 5,            # baseline mean change
  alt_param     = 35,           # Target
  sd            = 25,
  alloc_ratio   = 2,            # 2:1
  prior         = "jeffreys",
  p2_data       = list(x_bar = 32, s2 = 600, n = 12),
  p2_data_ctrl  = list(x_bar = 6, s2 = 500, n = 6),
  p3_n          = 60,           # Planned Confirmatory-stage total
  p3_alloc_ratio = 2,
  p3_alpha      = 0.025,
  label         = "Continuous Confirmatory — PPOS"
)

res5 <- run_quantitative_framework(
  config = cfg5,
  B_oc = example_B, n_mc_ppos = example_ppos,
  n_mc_sensitivity = example_sensitivity,
  output_dir = file.path(outdir, "ex5_continuous_confirm_ppos")
)

###############################################################################
# EXAMPLE 6: BINARY, POC, SINGLE-ARM — Skeptical prior
# Scenario: Same as Example 1 but with skeptical prior
###############################################################################

cat("\n\n################################################################\n")
cat("EXAMPLE 6: Binary PoC with Skeptical Prior\n")
cat("################################################################\n")

cfg6 <- create_config(
  endpoint_type = "binary",
  study_type    = "poc",
  design        = "single_arm",
  null_param    = 0.35,
  alt_param     = 0.55,
  prior         = "skeptical",   # Prior centered at H0
  go_target     = 0.40,
  go_threshold  = 0.90,
  consider_threshold = 0.60,
  label         = "Binary PoC — Skeptical Prior"
)

# Observed: 8/15 responders
res6 <- run_quantitative_framework(
  config = cfg6,
  observed_data = list(x = 8, n = 15),
  B_oc = example_B, n_mc_ppos = example_ppos,
  n_mc_sensitivity = example_sensitivity,
  output_dir = file.path(outdir, "ex6_binary_poc_skeptical")
)

cat("\n\n################################################################\n")
cat("ALL EXAMPLES COMPLETE\n")
cat("################################################################\n")
cat("Results saved to:", normalizePath(outdir), "\n")
