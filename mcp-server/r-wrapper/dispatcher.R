#!/usr/bin/env Rscript
# dispatcher.R — JSON-in / JSON-out bridge to the experiment design R framework.
# Called by the MCP server as: Rscript dispatcher.R <tool_name> <input.json> <output.json>

args <- commandArgs(trailingOnly = TRUE)
if (length(args) != 3) {
  cat('{"error":"Usage: Rscript dispatcher.R <tool> <input.json> <output.json>"}', file = stderr())
  quit(status = 1)
}
tool_name  <- args[1]
input_file <- args[2]
output_file <- args[3]

suppressPackageStartupMessages({
  if (!requireNamespace("jsonlite", quietly = TRUE))
    stop("jsonlite package required. Install: install.packages('jsonlite')")
})

params <- jsonlite::fromJSON(input_file, simplifyVector = TRUE)

this_script <- sub("--file=", "", grep("--file=", commandArgs(FALSE), value = TRUE)[1])
suite_root <- normalizePath(file.path(dirname(this_script), "..", ".."), mustWork = TRUE)

source_skill <- function(skill, ...) {
  if (!skill %in% runtime_profile$skills) stop("Statistical engine is unavailable in this runtime profile")
  skill_r <- file.path(suite_root, skill, "scripts", "R")
  for (f in list(...)) source(file.path(skill_r, f), local = FALSE)
}

# Paths of any NaN/Inf leaves (a broken computation). Distinct from NA_real_,
# which is a legitimate "not applicable" (is.nan(NA) is FALSE) and stays null.
.nonfinite_paths <- function(x, path = "") {
  if (is.numeric(x)) {
    idx <- which(is.nan(x) | is.infinite(x))
    if (!length(idx)) return(character(0))
    return(if (length(x) > 1) paste0(path, "[", idx, "]") else path)
  }
  if (is.list(x) && length(x)) {
    nms <- names(x); if (is.null(nms)) nms <- as.character(seq_along(x))
    return(unlist(lapply(seq_along(x), function(i)
      .nonfinite_paths(x[[i]], if (nzchar(path)) paste0(path, ".", nms[i]) else nms[i])),
      use.names = FALSE))
  }
  character(0)
}

write_result <- function(result) {
  # Reject non-finite outputs instead of silently emitting them as null, which
  # would slip an invalid computation past the output-contract gate.
  bad <- .nonfinite_paths(result)
  if (length(bad) > 0 && is.null(result$error)) {
    result <- list(error = paste0(
      "non-finite (NaN/Inf) values in tool output at: ",
      paste(utils::head(bad, 5), collapse = ", "),
      if (length(bad) > 5) paste0(" (+", length(bad) - 5, " more)") else "",
      " — computation is invalid"))
    json <- jsonlite::toJSON(result, auto_unbox = TRUE, pretty = TRUE, na = "null")
    writeLines(json, output_file)
    quit(status = 1)
  }
  json <- jsonlite::toJSON(result, auto_unbox = TRUE, pretty = TRUE, na = "null")
  writeLines(json, output_file)
}

write_error <- function(msg) {
  write_result(list(error = msg))
  quit(status = 1)
}

# Call an R function passing only the args it actually declares. Provided keys
# that are NOT formals of fn are no longer dropped silently: they are echoed
# back in an `ignored_params` field of the result so the caller can see that a
# knob it set did not reach the computation. `exclude` names dispatcher-level
# routing keys (already consumed to pick fn) that must not be reported.
call_filtered <- function(fn, args, exclude = character(0)) {
  if (is.null(args)) args <- list()
  keep <- names(args) %in% names(formals(fn))
  res <- do.call(fn, args[keep])
  ignored <- setdiff(names(args)[!keep], exclude)
  if (length(ignored) > 0 && is.list(res)) {
    res$ignored_params <- I(ignored)  # I(): stays a JSON array even when length 1
  }
  res
}

# Make RSM run-order semantics explicit and verifiable at the public bridge.
# The private design routine is always asked for its deterministic standard
# order; this bridge then applies the requested seeded permutation, records the
# original standard position, and numbers the delivered run order.  A
# degenerate identity draw is rotated so randomize=TRUE is never observationally
# identical to the unrandomized contract.
bind_rsm_run_order <- function(res, randomize, seed) {
  if (!is.list(res) || !is.data.frame(res$design) || nrow(res$design) < 1)
    stop("RSM design did not return a nonempty rectangular data frame")
  metadata_columns <- c("point_type", "run", "std_order")
  factor_columns <- names(res$design)[
    !tolower(names(res$design)) %in% metadata_columns
  ]
  # Match the public projector's deterministic factor-name order.  Canonical
  # public aliases use numeric suffix order (factor_2 before factor_10); all
  # other factor names use stable lexical order.  Never let data-frame column
  # insertion order define std_order because the public projection renames
  # columns independently of that insertion order.
  canonical_alias <- grepl("^factor_[1-9][0-9]*$", factor_columns)
  alias_suffix <- ifelse(
    canonical_alias, sub("^factor_", "", factor_columns), ""
  )
  factor_column_order <- order(
    ifelse(canonical_alias, 0L, 1L),
    ifelse(canonical_alias, nchar(alias_suffix, type = "bytes"), 0L),
    ifelse(canonical_alias, alias_suffix, factor_columns),
    method = "radix"
  )
  factor_columns <- factor_columns[factor_column_order]
  point_rank <- match(
    tolower(as.character(res$design$point_type)),
    c("factorial", "axial", "edge", "center")
  )
  if (!length(factor_columns) || anyNA(point_rank))
    stop("RSM design has invalid factor or point-type columns")
  sort_fields <- c(list(point_rank), unname(lapply(
    factor_columns, function(column) res$design[[column]]
  )))
  canonical_order <- do.call(order, c(
    sort_fields, list(na.last = NA, method = "radix")
  ))
  if (length(canonical_order) != nrow(res$design))
    stop("RSM design cannot be placed in canonical standard order")
  res$design <- res$design[canonical_order, , drop = FALSE]
  rownames(res$design) <- NULL
  n_runs <- nrow(res$design)
  res$design$std_order <- seq_len(n_runs)
  order <- seq_len(n_runs)
  if (isTRUE(randomize)) {
    set.seed(seed)
    order <- sample.int(n_runs)
    if (n_runs > 1 && identical(order, seq_len(n_runs)))
      order <- c(order[-1], order[1])
  }
  res$design <- res$design[order, , drop = FALSE]
  rownames(res$design) <- NULL
  res$design$run <- seq_len(n_runs)
  if (isTRUE(randomize)) res$seed <- seed else res$seed <- NULL
  res
}

tryCatch({
  source(file.path(suite_root, "mcp-server", "r-wrapper", "runtime-profile.R"))
  runtime_profile <- load_runtime_profile(suite_root)
  if (!tool_name %in% runtime_profile$tools)
    stop("Tool is unavailable in this runtime profile")

  if (tool_name == "validate_config") {
    source_skill("vera-experiment-designing", "config.R")
    cfg <- do.call(create_config, params)
    json_null_if_absent <- function(value) if (is.null(value)) NA else value
    estimand <- switch(cfg$endpoint_type,
      binary = if (cfg$design == "single_arm") "response_probability" else "risk_difference",
      continuous = if (cfg$design == "single_arm") "mean" else "mean_difference",
      tte = if (cfg$design == "single_arm") "hazard_rate" else "hazard_ratio",
      incidence_rate = if (cfg$design == "single_arm") "incidence_rate" else "rate_difference"
    )
    write_result(list(
      valid = TRUE,
      # Retain the original flat fields for existing clients while exposing the
      # complete privacy-safe preflight contract below.
      endpoint_type = cfg$endpoint_type,
      study_type = cfg$study_type,
      design = cfg$design,
      go_target = cfg$go_target,
      alphas = cfg$alphas,
      powers = cfg$powers,
      resolved_config = list(
        endpoint_type = cfg$endpoint_type,
        study_type = cfg$study_type,
        design = cfg$design,
        estimand = estimand,
        direction = cfg$direction,
        sidedness = "one_sided",
        null_param = cfg$null_param,
        alt_param = cfg$alt_param,
        sd = json_null_if_absent(cfg$sd),
        alloc_ratio = cfg$alloc_ratio,
        alphas = cfg$alphas,
        powers = cfg$powers,
        prior_params = cfg$prior_params,
        go_threshold = cfg$go_threshold,
        consider_threshold = cfg$consider_threshold,
        go_target = cfg$go_target,
        p3_n = json_null_if_absent(cfg$p3_n),
        p3_alloc_ratio = cfg$p3_alloc_ratio,
        p3_alpha = cfg$p3_alpha,
        accrual_time = json_null_if_absent(cfg$accrual_time),
        followup_time = json_null_if_absent(cfg$followup_time),
        tte_method = json_null_if_absent(cfg$tte_method),
        exposure_time = json_null_if_absent(cfg$exposure_time),
        rate_method = json_null_if_absent(cfg$rate_method),
        has_p2_data = !is.null(cfg$p2_data),
        has_p2_control_data = !is.null(cfg$p2_data_ctrl)
      ),
      simulation_defaults = list(seed = 42L, B_oc = 5000L)
    ))

  } else if (tool_name == "sample_size") {
    source_skill("vera-experiment-designing", "config.R", "sample_size.R", "run_framework.R")
    cfg <- do.call(create_config, params)
    ss <- compute_sample_size(cfg)
    write_result(list(results = ss))

  } else if (tool_name == "simulate_design") {
    source_skill("vera-experiment-designing",
                 "config.R", "sample_size.R", "bayesian.R", "frequentist.R", "ppos.R", "run_framework.R")
    cfg_params <- params$config
    seed <- if (!is.null(params$seed)) params$seed else 42
    n_oc <- params$n_oc
    B_oc <- if (!is.null(params$B_oc)) params$B_oc else 5000
    # Preserve NULL so controlled binary/continuous OC derives the configured
    # margin from go_target - null_param. Zero is an explicit caller choice.
    delta <- if (!is.null(params$delta)) params$delta else NULL

    cfg <- do.call(create_config, cfg_params)
    set.seed(seed)

    ss <- compute_sample_size(cfg)

    if (is.null(n_oc)) {
      if (cfg$design == "controlled") {
        ss_ctrl <- ss[ss$design == "controlled", , drop = FALSE]
        if (nrow(ss_ctrl) > 0) {
          n_oc <- list(n_trt = ss_ctrl$n_trt[1], n_ctrl = ss_ctrl$n_ctrl[1])
        } else {
          n_oc <- list(n_trt = 50, n_ctrl = 50)
        }
      } else {
        n_oc <- ss$n_total[1]
      }
    }

    # The OC replicate count is hard-capped at 5000. Echo the EFFECTIVE values
    # (B_used, n_oc_used) so a capped B_oc or a derived n_oc is never invisible.
    B_used <- min(B_oc, 5000)
    n_units <- if (is.list(n_oc)) n_oc$n_trt + n_oc$n_ctrl else n_oc
    if (!is.finite(n_units) || n_units * B_used > 10000000) {
      stop("requested OC workload exceeds the 10,000,000 simulated-unit limit",
           call. = FALSE)
    }
    oc <- compute_oc(cfg, n = n_oc, B = B_used, seed = seed, delta = delta)

    result <- list(sample_size = ss, oc = oc, seed = seed,
                   B_used = B_used, n_oc_used = n_oc)

    if (cfg$study_type == "confirmatory" && !is.null(cfg$p2_data)) {
      source(file.path(suite_root, "vera-experiment-designing", "scripts", "R", "ppos.R"))
      # Re-seed so PPOS depends only on (seed, p2_data, p3_n, p3_alpha) and does
      # NOT inherit the RNG state compute_oc left (which varies with B_oc/n_oc). S-1.
      set.seed(seed)
      # A failed confirmatory PPOS is a failed analysis, not a presentable
      # partial payload. Let the dispatcher emit the standard top-level error.
      result$ppos <- compute_ppos(cfg)
    }

    write_result(result)

  } else if (tool_name == "master_simulate") {
    source_skill("vera-master-experiment-designing",
                 "shared_utils.R", "master_config.R",
                 "basket_frequentist.R", "basket_bhm.R", "basket_bayesian.R",
                 "basket_borrowing.R",
                 "umbrella_mams.R", "umbrella_dtl.R", "umbrella_bar.R",
                 "platform_rar.R", "platform_ncc.R", "platform_simulation.R",
                 "run_master_framework.R")
    cfg_params <- params$config
    output_dir <- if (!is.null(params$output_dir)) params$output_dir else tempdir()
    cfg_params$output_dir <- output_dir

    cfg <- do.call(create_master_config, cfg_params)

    capture.output({
      result <- run_master_framework(cfg)
    }, type = "output") -> run_log

    write_result(list(
      result = result,
      seed = cfg$seed,
      output_dir = output_dir,
      log = paste(run_log, collapse = "\n")
    ))

  } else if (tool_name == "indirect_compare") {
    source_skill("vera-indirect-comparing", "indirect_comparison.R")
    method <- params$method
    if (is.null(method)) write_error("method is required: 'bucher' or 'maic'")

    if (method == "bucher") {
      comparisons <- params$comparisons
      if (is.data.frame(comparisons)) {
        n_comp <- nrow(comparisons)
        get_comp <- function(i) lapply(comparisons[i, , drop = FALSE], function(x) x[[1]])
      } else {
        n_comp <- length(comparisons)
        get_comp <- function(i) comparisons[[i]]
      }
      result_rows <- list()
      for (i in seq_len(n_comp)) {
        comp <- get_comp(i)
        em <- if (!is.null(comp$effect_measure)) comp$effect_measure else "log_odds_ratio"
        as <- if (!is.null(comp$analysis_scale)) comp$analysis_scale else em
        effect_ab <- data.frame(estimate = comp$estimate_ab, se = comp$se_ab,
                                effect_measure = em, analysis_scale = as,
                                stringsAsFactors = FALSE)
        effect_cb <- data.frame(estimate = comp$estimate_cb, se = comp$se_cb,
                                effect_measure = em, analysis_scale = as,
                                stringsAsFactors = FALSE)
        r <- combine_indirect(
          effect_ab = effect_ab, effect_cb = effect_cb,
          treatment_a = comp$treatment_a, treatment_c = comp$treatment_c,
          common_comparator = comp$common_comparator,
          method = if (!is.null(comp$method)) comp$method else "bucher",
          alpha = if (!is.null(comp$alpha)) comp$alpha else 0.05
        )
        result_rows[[i]] <- r
      }
      write_result(list(comparisons = result_rows))

    } else if (method == "maic") {
      ipd_file <- params$ipd_file
      targets_file <- params$targets_file
      if (is.null(ipd_file) || is.null(targets_file))
        write_error("maic requires ipd_file and targets_file")
      if (is.null(params$treatment_arm))
        write_error("maic requires treatment_arm (the active-arm label in the IPD arm column)")

      result <- run_maic(
        ipd_csv = ipd_file,
        target_csv = targets_file,
        output_dir = if (!is.null(params$output_dir)) params$output_dir else tempdir(),
        covariates = params$covariates,
        treatment_arm = params$treatment_arm,
        comparator_arm = params$comparator_arm,
        arm_col = if (!is.null(params$arm_col)) params$arm_col else "arm",
        endpoint_type = if (!is.null(params$maic_endpoint_type)) params$maic_endpoint_type else "binary",
        outcome_col = if (!is.null(params$outcome_col)) params$outcome_col else "response",
        event_col = params$event_col,
        time_col = params$time_col,
        status_col = params$status_col,
        measure = params$measure,
        tte_method = if (!is.null(params$tte_method)) params$tte_method else "cox",
        alpha = if (!is.null(params$alpha)) params$alpha else 0.05,
        bootstrap_replicates = if (!is.null(params$bootstrap_replicates)) params$bootstrap_replicates else 200L,
        bootstrap_seed = if (!is.null(params$bootstrap_seed)) params$bootstrap_seed else 42L,
        export_row_weights = isTRUE(params$export_row_weights)
      )
      write_result(result)
    } else {
      write_error(paste("Unknown method:", method))
    }

  } else if (tool_name == "meta_analyze") {
    source_skill("vera-meta-analyzing", "meta_endpoint_core.R")
    endpoint_type <- params$endpoint_type
    if (is.null(endpoint_type)) write_error("endpoint_type is required")

    input_fn <- switch(endpoint_type,
      binary_single = binary_logit_inputs,
      binary_comparative = binary_comparative_inputs,
      continuous_single = continuous_mean_inputs,
      continuous_comparative = continuous_comparative_inputs,
      time_to_event = time_to_event_inputs,
      incidence_single = incidence_rate_inputs,
      incidence_comparative = incidence_rate_ratio_inputs,
      NULL
    )
    if (is.null(input_fn)) write_error(paste("Unknown endpoint_type:", endpoint_type))

    studies <- params$studies
    if (is.data.frame(studies)) {
      n_studies <- nrow(studies)
      get_study <- function(i) lapply(studies[i, , drop = FALSE], function(x) x[[1]])
    } else {
      n_studies <- length(studies)
      get_study <- function(i) studies[[i]]
    }
    # Forward only keys that are real parameters of the endpoint input function,
    # so a stray study label or a misnamed field yields the input function's own
    # informative error rather than an opaque "unused argument" crash.
    formal_names <- names(formals(input_fn))
    yi <- rep(NA_real_, n_studies)
    vi <- rep(NA_real_, n_studies)
    effect_measures <- rep(NA_character_, n_studies)
    drop_reason <- rep(NA_character_, n_studies)
    for (i in seq_len(n_studies)) {
      s <- get_study(i)
      s <- s[names(s) %in% formal_names]
      inp <- tryCatch(do.call(input_fn, s), error = function(e) e)
      if (inherits(inp, "error")) {
        drop_reason[i] <- conditionMessage(inp)
        next
      }
      yi[i] <- inp$yi
      vi[i] <- inp$vi
      if (!is.null(inp$measure) && length(inp$measure) > 0L) {
        effect_measures[i] <- as.character(inp$measure[[1L]])
      }
      if ("exclusion_reason" %in% names(inp) &&
          length(inp$exclusion_reason) > 0L && !is.na(inp$exclusion_reason[[1]])) {
        drop_reason[i] <- as.character(inp$exclusion_reason[[1]])
      }
    }
    # Fail before meta_iv() if successfully parsed studies are on different
    # statistical scales. Pooling their numeric yi values would produce a
    # dimensionless but scientifically meaningless result.
    effect_measure <- common_effect_measure(effect_measures)
    # meta_iv() silently pools only studies with finite yi/vi and vi > 0. Mirror
    # that rule here so every exclusion is VISIBLE (n_input / k_used /
    # dropped_studies / dropped_detail) instead of silent, and NA-out non-finite
    # values so the echoed per-study vectors pass the non-finite output gate.
    ok <- is.finite(yi) & is.finite(vi) & vi > 0
    drop_reason[!ok & is.na(drop_reason)] <- "non-finite effect or variance"
    yi[!ok] <- NA_real_
    vi[!ok] <- NA_real_

    alpha <- if (!is.null(params$alpha)) params$alpha else 0.05
    random <- if (!is.null(params$random)) params$random else TRUE
    inference_method <- if (!is.null(params$inference_method)) params$inference_method else "hksj"
    pool <- meta_iv(yi, vi, alpha = alpha, random = random,
                    inference_method = inference_method)
    result <- as.list(pool)
    result$effect_measure <- effect_measure
    result$endpoint_type <- endpoint_type
    result$study_effects <- yi
    result$study_variances <- vi
    result$study_effect_measures <- effect_measures
    result$n_input <- n_studies
    result$k_used <- pool$k
    result$dropped_studies <- n_studies - pool$k
    if (result$dropped_studies > 0) {
      result$dropped_detail <- data.frame(
        study = which(!ok),
        reason = drop_reason[!ok],
        stringsAsFactors = FALSE
      )
    }

    write_result(result)

  } else if (tool_name == "ab_test") {
    source_skill("vera-doe-designing", "doe.R")
    write_result(call_filtered(ab_test_size, params))

  } else if (tool_name == "factorial_design") {
    source_skill("vera-doe-designing", "doe.R")
    if (!is.null(params$generators) && is.matrix(params$generators)) {
      params$generators <- lapply(seq_len(nrow(params$generators)),
                                  function(i) as.integer(params$generators[i, ]))
    }
    if (!is.null(params$fraction) && params$fraction > 0) {
      res <- call_filtered(fractional_factorial, params)
    } else {
      # fraction (=0) is a routing key here, consumed above — not "ignored".
      res <- call_filtered(full_factorial, params, exclude = "fraction")
    }
    # Echo the seed actually used whenever run order was randomized (doe.R
    # defaults to 42) so the plan is reproducible from the output alone.
    if (isTRUE(params$randomize)) {
      res$seed <- if (!is.null(params$seed)) params$seed else 42
    }
    write_result(res)

  } else if (tool_name == "rsm_design") {
    source_skill("vera-doe-designing", "doe.R")
    design <- if (!is.null(params$design)) params$design else "ccd"
    randomize_requested <- isTRUE(params$randomize)
    seed_used <- if (!is.null(params$seed)) params$seed else 42L
    engine_params <- params
    engine_params$randomize <- FALSE
    engine_params$seed <- NULL
    if (design == "bbd") {
      res <- call_filtered(bbd_design, engine_params, exclude = "design")
    } else {
      res <- call_filtered(ccd_design, engine_params, exclude = "design")
    }
    res <- bind_rsm_run_order(res, randomize_requested, seed_used)
    write_result(res)

  } else if (tool_name == "randomize") {
    source_skill("vera-doe-designing", "doe.R")
    res <- call_filtered(randomize_units, params)
    write_result(res)

  } else if (tool_name == "run_tests") {
    # Every skill exposed as an MCP tool must have wired tests.
    skills_to_test <- runtime_profile$skills
    results <- list()
    for (skill in skills_to_test) {
      test_file <- file.path(suite_root, skill, "scripts", "tests", "run_tests.R")
      if (!file.exists(test_file)) {
        # A missing test suite is a FAILURE, not a silent skip.
        results[[skill]] <- list(output = "no test file wired", passes = 0L,
                                 fails = 0L, ok = FALSE, missing = TRUE)
        next
      }
      rscript <- file.path(R.home("bin"),
                           paste0("Rscript", if (.Platform$OS.type == "windows") ".exe" else ""))
      out <- system2(rscript, c("--vanilla", shQuote(test_file)), stdout = TRUE, stderr = TRUE)
      status  <- attr(out, "status")               # non-NULL only on nonzero exit
      crashed <- !is.null(status) && status != 0
      passes  <- sum(grepl("^TEST .* : PASS$", out))
      fails   <- sum(grepl("^TEST .* : FAIL( |$)", out))
      results[[skill]] <- list(
        output = paste(out, collapse = "\n"),
        passes = passes, fails = fails,
        exit_status = if (is.null(status)) 0L else as.integer(status),
        # Green requires: clean exit, no FAIL lines, AND at least one PASS
        # (so a script that crashes before any test can't read as green).
        ok = (!crashed) && fails == 0 &&
          passes == runtime_profile$expected_regression_pass_counts[[skill]]
      )
    }
    all_ok <- length(results) == length(skills_to_test) &&
      all(vapply(results, function(r) isTRUE(r$ok), logical(1)))
    write_result(list(all_ok = all_ok, skills = results))

  } else {
    write_error(paste("Unknown tool:", tool_name))
  }

}, error = function(e) {
  write_result(list(error = conditionMessage(e)))
  quit(status = 1)
})
