# SPDX-License-Identifier: GPL-3.0-only
# Public R packaging checks. Run from any directory; no private engines required.

main <- function() {
  arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
  if (!length(arg)) stop("Run this check with Rscript")
  script_path <- sub("^--file=", "", arg[1])
  if (!file.exists(script_path)) script_path <- gsub("~+~", " ", script_path, fixed = TRUE)
  test_dir <- dirname(normalizePath(script_path, mustWork = TRUE))
  module_root <- normalizePath(file.path(test_dir, "..", ".."))
  staging <- tempfile("qdf public packaging ")
  dir.create(staging)
  previous_cwd <- getwd()
  on.exit({ setwd(previous_cwd); unlink(staging, recursive = TRUE) }, add = TRUE)
  public_root <- file.path(staging, "public module")
  files <- c(paste0("scripts/R/", c("config", "sample_size", "bayesian",
                                    "frequentist", "ppos", "run_framework",
                                    "validate_framework", "examples"), ".R"),
             "examples/study-planning.R", "examples/analysis-template.R")
  for (file in files) {
    destination <- file.path(public_root, file)
    dir.create(dirname(destination), recursive = TRUE, showWarnings = FALSE)
    stopifnot(file.copy(file.path(module_root, file), destination))
  }
  outside <- file.path(staging, "external working directory")
  dir.create(outside)
  outside <- normalizePath(outside)
  setwd(outside)

  passed <- 0L
  failed <- 0L
  check <- function(name, expr) {
    tryCatch({
      force(expr)
      passed <<- passed + 1L
      cat("TEST", name, ": PASS\n")
    }, error = function(e) {
      failed <<- failed + 1L
      cat("TEST", name, ": FAIL (", conditionMessage(e), ")\n")
    })
  }
  run_script <- function(relative_path, output, framework = "") {
    logfile <- tempfile(tmpdir = staging, fileext = ".log")
    status <- system2(file.path(R.home("bin"), "Rscript"),
                      shQuote(file.path(public_root, relative_path)),
                      stdout = logfile, stderr = logfile,
                      env = c(paste0("QDF_OUTPUT_DIR=", shQuote(output)),
                              "QDF_EXAMPLE_QUICK=1",
                              paste0("QDF_FRAMEWORK_DIR=", shQuote(framework))))
    if (status != 0L) stop(paste(readLines(logfile, warn = FALSE), collapse = "\n"))
    readLines(logfile, warn = FALSE)
  }

  check("public_engine_smoke_from_unrelated_directory", {
    output <- run_script("scripts/R/validate_framework.R", file.path(staging, "smoke"))
    stopifnot(any(output == "VALIDATE_FRAMEWORK=TRUE"), identical(getwd(), outside))
  })

  study_output <- file.path(staging, "study outputs")
  check("public_shared_engine_runs_three_study_scenarios", {
    run_script("examples/study-planning.R", study_output)
    for (scenario in c("single_arm", "controlled_1_1", "controlled_2_1")) {
      folder <- file.path(study_output, scenario)
      sample_size <- read.csv(file.path(folder, "sample_size.csv"))
      oc <- read.csv(file.path(folder, "oc_curves.csv"))
      stopifnot(all(sample_size$n_total > 0), nrow(oc) > 1,
                all(oc$p_go >= 0 & oc$p_go <= 1),
                file.info(file.path(folder, "power_curve.pdf"))$size > 0)
      if (scenario != "single_arm") {
        selected <- sample_size[sample_size$design == "controlled", ]
        stopifnot(nrow(selected) > 0,
                  all(selected$n_total == selected$n_trt + selected$n_ctrl))
      }
    }
  })

  check("public_worked_examples_include_bayesian_and_ppos_outputs", {
    output <- file.path(staging, "worked outputs")
    run_script("scripts/R/examples.R", output)
    folders <- list.dirs(output, full.names = FALSE, recursive = FALSE)
    stopifnot(length(folders) == 6L)
    for (folder in c("ex3_binary_confirm_ppos", "ex5_continuous_confirm_ppos")) {
      ppos <- read.csv(file.path(output, folder, "ppos_sensitivity.csv"))
      stopifnot(nrow(ppos) > 1L, all(is.finite(ppos$ppos)),
                all(ppos$ppos >= 0 & ppos$ppos <= 1))
    }
  })

  check("public_template_can_be_relocated_without_changing_directory", {
    relocated <- file.path(public_root, "copied-template.R")
    stopifnot(file.copy(file.path(public_root, "examples", "analysis-template.R"),
                        relocated))
    output <- file.path(staging, "template outputs")
    runner <- file.path(public_root, "template-runner.R")
    writeLines(c("before <- getwd()",
                 paste0("source(", encodeString(relocated, quote = '"'), ")"),
                 "stopifnot(identical(getwd(), before))"), runner)
    run_script("template-runner.R", output, file.path(public_root, "scripts", "R"))
    stopifnot(file.exists(file.path(output, "sample_size.csv")),
              file.exists(file.path(output, "oc_curves.csv")),
              identical(getwd(), outside), length(list.files(outside)) == 0L)
  })

  check("public_example_csv_outputs_are_reproducible", {
    repeated <- file.path(staging, "repeat outputs")
    run_script("examples/study-planning.R", repeated)
    csv_files <- list.files(study_output, pattern = "\\.csv$", recursive = TRUE)
    stopifnot(length(csv_files) > 0L)
    for (file in csv_files) {
      stopifnot(identical(readLines(file.path(study_output, file)),
                          readLines(file.path(repeated, file))))
    }
  })

  check("public_framework_exposes_validated_ppos_budgets", {
    source(file.path(public_root, "scripts", "R", "run_framework.R"))
    cfg <- create_config(endpoint_type = "binary", study_type = "confirmatory",
                         design = "single_arm", null_param = 0.30, alt_param = 0.50,
                         alphas = 0.025, powers = 0.80,
                         p2_data = list(x = 8, n = 20), p3_n = 50)
    defaults <- formals(run_quantitative_framework)
    stopifnot(identical(defaults$n_mc_ppos, 100000),
              identical(defaults$n_mc_sensitivity, 50000))
    set.seed(42)
    invisible(capture.output(result <- run_quantitative_framework(
      cfg, n_mc_ppos = 128L, n_mc_sensitivity = 64L)))
    stopifnot(result$ppos$n_mc == 128L,
              length(result$ppos$cond_power_draws) == 128L)
    rejects <- function(value) tryCatch({
      run_quantitative_framework(cfg, n_mc_ppos = value)
      FALSE
    }, error = function(e) TRUE)
    stopifnot(rejects(0), rejects(1.5), rejects(Inf))
  })

  cat("\n--- Public packaging:", passed, "passed,", failed, "failed ---\n")
  if (failed) quit(status = 1L)
}
main()
