# SPDX-License-Identifier: GPL-3.0-only
# Standalone analysis template with synthetic inputs. Copy and adapt this file.
# Set QDF_FRAMEWORK_DIR to the published scripts/R directory if you move it.
# Set QDF_OUTPUT_DIR to choose where generated CSVs and PDFs are saved.
# All alpha values are one-sided. No working-directory change is required.

FRAMEWORK_DIR <- Sys.getenv("QDF_FRAMEWORK_DIR", unset = "")
if (!nzchar(FRAMEWORK_DIR)) {
  script_dir <- local({
    frames <- sys.frames()
    paths <- Filter(Negate(is.null), lapply(frames, function(frame) frame$ofile))
    if (length(paths)) {
      dirname(normalizePath(paths[[length(paths)]]))
    } else {
      arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
      if (!length(arg)) stop("Set QDF_FRAMEWORK_DIR when running interactively")
      script_path <- sub("^--file=", "", arg[1])
      if (!file.exists(script_path)) script_path <- gsub("~+~", " ", script_path, fixed = TRUE)
      dirname(normalizePath(script_path, mustWork = TRUE))
    }
  })
  FRAMEWORK_DIR <- file.path(script_dir, "..", "scripts", "R")
}
if (!file.exists(file.path(FRAMEWORK_DIR, "run_framework.R"))) {
  stop("QDF_FRAMEWORK_DIR must point to the published scripts/R directory")
}
source(file.path(FRAMEWORK_DIR, "run_framework.R"))

# User inputs: an illustrative binary single-arm study.
ENDPOINT_TYPE <- "binary"
STUDY_TYPE <- "poc"
DESIGN <- "single_arm"
NULL_PARAM <- 0.30
ALT_PARAM <- 0.50
SD <- NULL                       # Required for a continuous endpoint.
ALLOC_RATIO <- 1                 # Treatment:control ratio for controlled studies.
PRIOR <- "jeffreys"
GO_TARGET <- 0.40
OBSERVED_DATA <- list(x = 8, n = 20)

# Optional confirmatory PPOS inputs. Controlled studies also need control data.
P2_DATA <- NULL                  # Binary: list(x = ..., n = ...).
P2_DATA_CTRL <- NULL
P3_N <- NULL
P3_ALPHA <- 0.025

OUTPUT_DIR <- Sys.getenv("QDF_OUTPUT_DIR",
                         unset = file.path(getwd(), "qdf-output", "analysis"))
quick <- identical(Sys.getenv("QDF_EXAMPLE_QUICK"), "1")
set.seed(42)
cfg <- create_config(
  endpoint_type = ENDPOINT_TYPE, study_type = STUDY_TYPE, design = DESIGN,
  null_param = NULL_PARAM, alt_param = ALT_PARAM, sd = SD,
  alloc_ratio = ALLOC_RATIO, prior = PRIOR, go_target = GO_TARGET,
  p2_data = P2_DATA, p2_data_ctrl = P2_DATA_CTRL, p3_n = P3_N,
  p3_alpha = P3_ALPHA, label = "Synthetic primary endpoint"
)
results <- run_quantitative_framework(
  config = cfg, observed_data = OBSERVED_DATA,
  B_oc = if (quick) 32L else 1000L,
  n_mc_ppos = if (quick) 500L else 100000L,
  n_mc_sensitivity = if (quick) 250L else 50000L,
  output_dir = OUTPUT_DIR
)
cat("\nOutputs saved to:", normalizePath(OUTPUT_DIR), "\n")
