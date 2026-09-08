# SPDX-License-Identifier: GPL-3.0-only
# Synthetic binary study planning: single arm, controlled 1:1 and controlled 2:1.
# All calculations use the shared engine; no statistical methods are copied here.
# QDF_OUTPUT_DIR chooses the output directory. QDF_EXAMPLE_QUICK=1 reduces the
# simulation budget for checking the installation, not precise estimation.

script_dir <- local({
  frames <- sys.frames()
  paths <- lapply(frames, function(frame) frame$ofile)
  paths <- Filter(Negate(is.null), paths)
  if (length(paths)) {
    dirname(normalizePath(paths[[length(paths)]]))
  } else {
    arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
    if (!length(arg)) stop("Cannot determine example location", call. = FALSE)
    script_path <- sub("^--file=", "", arg[1])
    if (!file.exists(script_path)) script_path <- gsub("~+~", " ", script_path, fixed = TRUE)
    dirname(normalizePath(script_path, mustWork = TRUE))
  }
})
source(file.path(script_dir, "..", "scripts", "R", "run_framework.R"))

outdir <- Sys.getenv("QDF_OUTPUT_DIR",
                     unset = file.path(getwd(), "qdf-output", "study-planning"))
B <- if (identical(Sys.getenv("QDF_EXAMPLE_QUICK"), "1")) 32L else 256L
set.seed(42)
cat("Synthetic study-planning examples; OC replicates per point:", B, "\n")

scenarios <- list(
  single_arm = list(design = "single_arm", ratio = 1,
                    observed = list(x = 9, n = 20)),
  controlled_1_1 = list(design = "controlled", ratio = 1,
                       observed = list(x_trt = 9, n_trt = 20,
                                       x_ctrl = 6, n_ctrl = 20)),
  controlled_2_1 = list(design = "controlled", ratio = 2,
                       observed = list(x_trt = 18, n_trt = 40,
                                       x_ctrl = 6, n_ctrl = 20))
)

for (name in names(scenarios)) {
  scenario <- scenarios[[name]]
  cfg <- create_config(
    endpoint_type = "binary", study_type = "poc",
    design = scenario$design, null_param = 0.30, alt_param = 0.50,
    alloc_ratio = scenario$ratio, alphas = 0.025, powers = 0.80,
    prior = "jeffreys", go_target = 0.40,
    label = paste("Synthetic", name)
  )
  results <- run_quantitative_framework(
    config = cfg, observed_data = scenario$observed, B_oc = B,
    output_dir = file.path(outdir, name)
  )
}
cat("\nOutputs saved to:", normalizePath(outdir), "\n")
