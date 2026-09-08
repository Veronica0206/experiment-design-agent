###############################################################################

.qdf_scalar <- function(value, name, lower = -Inf, upper = Inf,
                        integer = FALSE, lower_open = FALSE,
                        upper_open = FALSE) {
  valid <- is.numeric(value) && length(value) == 1L && is.finite(value)
  if (valid) {
    valid <- if (lower_open) value > lower else value >= lower
    valid <- valid && if (upper_open) value < upper else value <= upper
    valid <- valid && (!integer || value == floor(value))
  }
  if (!valid) stop(name, " has an invalid value", call. = FALSE)
  invisible(TRUE)
}

.qdf_require_fields <- function(data, fields, label) {
  if (!is.list(data)) stop(label, " must be a named list", call. = FALSE)
  missing <- setdiff(fields, names(data))
  if (length(missing)) {
    stop(label, " is missing: ", paste(missing, collapse = ", "), call. = FALSE)
  }
}

.qdf_validate_arm <- function(data, endpoint_type, label = "data") {
  if (endpoint_type == "binary") {
    .qdf_require_fields(data, c("x", "n"), label)
    .qdf_scalar(data$n, paste0(label, "$n"), lower = 1, integer = TRUE)
    .qdf_scalar(data$x, paste0(label, "$x"), lower = 0,
                upper = data$n, integer = TRUE)
  } else if (endpoint_type == "continuous") {
    .qdf_require_fields(data, c("x_bar", "s2", "n"), label)
    .qdf_scalar(data$x_bar, paste0(label, "$x_bar"))
    # Zero variance is valid with a proper NIG prior. The objective Jeffreys
    # update and the frequentist layer perform their stricter checks.
    .qdf_scalar(data$s2, paste0(label, "$s2"), lower = 0)
    .qdf_scalar(data$n, paste0(label, "$n"), lower = 2, integer = TRUE)
  } else if (endpoint_type == "tte") {
    .qdf_require_fields(data, c("events", "person_time"), label)
    .qdf_scalar(data$events, paste0(label, "$events"), lower = 0, integer = TRUE)
    .qdf_scalar(data$person_time, paste0(label, "$person_time"),
                lower = 0, lower_open = TRUE)
  } else if (endpoint_type == "incidence_rate") {
    .qdf_require_fields(data, c("count", "exposure"), label)
    .qdf_scalar(data$count, paste0(label, "$count"), lower = 0, integer = TRUE)
    .qdf_scalar(data$exposure, paste0(label, "$exposure"),
                lower = 0, lower_open = TRUE)
  }
  invisible(TRUE)
}

# Validate the public observed-data contract before either analysis dispatcher
# selects an arm-specific implementation. Controlled designs must never be
# silently reinterpreted as single-arm analyses when control fields are absent.
validate_observed_data <- function(config, data) {
  if (config$design == "single_arm") {
    .qdf_validate_arm(data, config$endpoint_type, "data")
    return(invisible(TRUE))
  }

  mapping <- switch(config$endpoint_type,
    binary = list(trt = c(x = "x_trt", n = "n_trt"),
                  ctrl = c(x = "x_ctrl", n = "n_ctrl")),
    continuous = list(trt = c(x_bar = "x_bar_trt", s2 = "s2_trt", n = "n_trt"),
                      ctrl = c(x_bar = "x_bar_ctrl", s2 = "s2_ctrl", n = "n_ctrl")),
    tte = list(trt = c(events = "events_trt", person_time = "pt_trt"),
               ctrl = c(events = "events_ctrl", person_time = "pt_ctrl")),
    incidence_rate = list(trt = c(count = "count_trt", exposure = "exp_trt"),
                          ctrl = c(count = "count_ctrl", exposure = "exp_ctrl"))
  )
  all_fields <- c(unname(mapping$trt), unname(mapping$ctrl))
  .qdf_require_fields(data, all_fields, "controlled data")
  trt <- setNames(lapply(mapping$trt, function(nm) data[[nm]]), names(mapping$trt))
  ctrl <- setNames(lapply(mapping$ctrl, function(nm) data[[nm]]), names(mapping$ctrl))
  .qdf_validate_arm(trt, config$endpoint_type, "treatment data")
  .qdf_validate_arm(ctrl, config$endpoint_type, "control data")
  invisible(TRUE)
}
# config.R
# Generic Quantitative Decision Framework — Configuration Builder
###############################################################################

#' Create an endpoint configuration for the quantitative decision framework
#'
#' @param endpoint_type  "binary", "continuous", "tte", or "incidence_rate"
#' @param study_type     "signal_detection", "poc", or "confirmatory"
#' @param design         "single_arm" or "controlled"
#' @param null_param     H0 rate (binary), H0 mean change (continuous), H0 hazard rate (tte), or H0 event rate per person-time (incidence_rate)
#' @param alt_param      H1 rate (binary), H1 mean change (continuous), H1 hazard rate (tte), or H1 event rate per person-time (incidence_rate)
#' @param sd             SD of endpoint (required for continuous)
#' @param alloc_ratio    Treatment:control allocation (1 = 1:1, 2 = 2:1); ignored for single_arm
#' @param alphas         One-sided alpha levels; NULL = auto from study_type
#' @param powers         Power targets; NULL = auto from study_type
#' @param prior          "jeffreys", "flat", "skeptical", or list(a=, b=) for binary / list(mu0=, kappa0=, alpha0=, beta0=) for continuous / list(shape=, rate=) for tte and incidence_rate
#' @param go_threshold   Posterior probability threshold for Go (non-confirmatory)
#' @param consider_threshold  Posterior probability threshold for Consider
#' @param go_target      Target parameter for Go/No-Go posterior: P(theta > go_target)
#' @param p2_data        Exploratory-stage observed data for PPOS: list(x=, n=) binary; list(x_bar=, s2=, n=) continuous; list(events=, person_time=) tte; list(count=, exposure=) incidence_rate
#' @param p2_data_ctrl   Exploratory-stage control arm data (controlled only): same structure as p2_data
#' @param p3_n           Planned Confirmatory-stage total sample size
#' @param p3_alloc_ratio Confirmatory-stage allocation ratio (for controlled designs)
#' @param p3_alpha       Confirmatory-stage one-sided alpha (default 0.025 = 0.05 two-sided)
#' @param accrual_time   Accrual period in time units (required for tte)
#' @param followup_time  Follow-up time after last participant enrolled (required for tte)
#' @param tte_method     "exponential" (default, conjugate Gamma). "cox_ph" is reserved but NOT yet implemented — it errors at config time.
#' @param exposure_time  Per-participant exposure/follow-up time (required for incidence_rate)
#' @param rate_method    "poisson" (default, conjugate Gamma). "negbin" is reserved but NOT yet implemented — it errors at config time.
#' @param overdispersion NegBin dispersion parameter theta — reserved for the future negbin implementation. Not read by any computation, so a non-NULL value ERRORS at config time rather than being silently ignored.
#' @param label          Display label for the endpoint
create_config <- function(
  endpoint_type   = c("binary", "continuous", "tte", "incidence_rate"),
  study_type      = c("signal_detection", "poc", "confirmatory"),
  design          = c("single_arm", "controlled"),

  # Assumptions
  null_param,
  alt_param,
  sd              = NULL,
  alloc_ratio     = 1,

  # Alpha / Power (auto-set if NULL)
  alphas          = NULL,
  powers          = NULL,

  # Bayesian prior
  prior           = "jeffreys",

  # Decision thresholds (non-confirmatory)
  go_threshold    = 0.90,
  consider_threshold = 0.60,

  # Go/No-Go target: P(theta > go_target | data)
  go_target       = NULL,

  # PPOS inputs (confirmatory)
  p2_data         = NULL,
  p2_data_ctrl    = NULL,
  p3_n            = NULL,
  p3_alloc_ratio  = NULL,
  p3_alpha        = 0.025,

  # TTE-specific
  accrual_time    = NULL,
  followup_time   = NULL,
  tte_method      = c("exponential", "cox_ph"),

  # Incidence rate-specific
  exposure_time   = NULL,
  rate_method     = c("poisson", "negbin"),
  overdispersion  = NULL,

  # Label
  label           = "Endpoint"
) {
  endpoint_type <- match.arg(endpoint_type)
  study_type    <- match.arg(study_type)
  design        <- match.arg(design)
  # Truth-in-advertising, validated for EVERY endpoint type (not just the one
  # that would read the knob — a reserved value must never pass silently):
  # 'cox_ph' and 'negbin' are not wired into any computation. Rather than
  # silently degrade to the exponential/Poisson model, reject so the caller
  # never trusts a result under a method label that never ran. (Both are on
  # the engine roadmap.)
  tte_method  <- match.arg(tte_method)
  rate_method <- match.arg(rate_method)
  if (tte_method == "cox_ph") {
    stop("tte_method='cox_ph' is not yet implemented — the framework computes ",
         "sample size, OC, and PPOS with the exponential model only. ",
         "Use tte_method='exponential'.", call. = FALSE)
  }
  if (rate_method == "negbin") {
    stop("rate_method='negbin' is not yet implemented — the framework computes ",
         "sample size, OC, and PPOS with the Poisson model only. ",
         "Use rate_method='poisson'.", call. = FALSE)
  }
  if (!is.null(overdispersion)) {
    stop("overdispersion is reserved for the future negbin implementation and ",
         "is not read by any computation — omit it. (With rate_method='poisson' ",
         "it would be silently ignored, which this framework does not allow.)",
         call. = FALSE)
  }

  # --- Validation ---
  .qdf_scalar(null_param, "null_param")
  .qdf_scalar(alt_param, "alt_param")

  if (endpoint_type == "binary") {
    stopifnot(null_param >= 0, null_param <= 1,
              alt_param >= 0, alt_param <= 1,
              alt_param > null_param)
  }
  if (endpoint_type == "continuous") {
    if (is.null(sd)) stop("sd is required for continuous endpoints")
    .qdf_scalar(sd, "sd", lower = 0, lower_open = TRUE)
    stopifnot(alt_param > null_param)
  }
  if (endpoint_type == "tte") {
    stopifnot(null_param > 0, alt_param > 0)
    # For TTE, lower hazard = better treatment, so alt_param < null_param
    stopifnot(alt_param < null_param)
    if (is.null(accrual_time)) stop("accrual_time is required for tte endpoints")
    if (is.null(followup_time)) stop("followup_time is required for tte endpoints")
    .qdf_scalar(accrual_time, "accrual_time", lower = 0, lower_open = TRUE)
    .qdf_scalar(followup_time, "followup_time", lower = 0, lower_open = TRUE)
  }
  if (endpoint_type == "incidence_rate") {
    stopifnot(null_param >= 0, alt_param >= 0, alt_param != null_param)
    if (design == "controlled" && null_param <= 0) {
      stop("controlled incidence-rate designs require null_param > 0 because the treatment target is expressed as a rate ratio",
           call. = FALSE)
    }
    if (is.null(exposure_time)) stop("exposure_time is required for incidence_rate endpoints")
    .qdf_scalar(exposure_time, "exposure_time", lower = 0, lower_open = TRUE)
  }
  if (design == "controlled") {
    .qdf_scalar(alloc_ratio, "alloc_ratio", lower = 1)
  }

  # --- Auto-set alpha/power from study_type ---
  if (is.null(alphas)) {
    alphas <- switch(study_type,
      signal_detection = 0.10,
      poc              = c(0.025, 0.05, 0.10),
      confirmatory     = 0.025
    )
  }
  if (is.null(powers)) {
    powers <- switch(study_type,
      signal_detection = 0.80,
      poc              = c(0.80, 0.90),
      confirmatory     = 0.90
    )
  }
  if (!is.numeric(alphas) || !length(alphas) || any(!is.finite(alphas)) ||
      any(alphas <= 0 | alphas >= 1)) {
    stop("alphas must contain finite probabilities strictly between 0 and 1", call. = FALSE)
  }
  if (!is.numeric(powers) || !length(powers) || any(!is.finite(powers)) ||
      any(powers <= 0 | powers >= 1)) {
    stop("powers must contain finite probabilities strictly between 0 and 1", call. = FALSE)
  }
  .qdf_scalar(consider_threshold, "consider_threshold", lower = 0, upper = 1,
              lower_open = TRUE, upper_open = TRUE)
  .qdf_scalar(go_threshold, "go_threshold", lower = 0, upper = 1,
              lower_open = TRUE, upper_open = TRUE)
  if (consider_threshold >= go_threshold) {
    stop("consider_threshold must be strictly below go_threshold", call. = FALSE)
  }

  # --- Resolve prior ---
  prior_params <- resolve_prior(prior, endpoint_type, null_param)

  # --- Default go_target ---
  if (is.null(go_target)) {
    if (endpoint_type == "tte") {
      # Geometric mean of hazard rates (midpoint on log scale)
      go_target <- exp((log(null_param) + log(alt_param)) / 2)
    } else {
      # Arithmetic midpoint for binary, continuous, incidence_rate
      go_target <- (null_param + alt_param) / 2
    }
  }
  .qdf_scalar(go_target, "go_target")
  if (endpoint_type == "binary" && (go_target < 0 || go_target > 1)) {
    stop("binary go_target must lie in [0, 1]", call. = FALSE)
  }
  if (endpoint_type == "tte" && go_target <= 0) {
    stop("tte go_target must be positive", call. = FALSE)
  }
  if (endpoint_type == "incidence_rate" && go_target < 0) {
    stop("incidence-rate go_target must be non-negative", call. = FALSE)
  }

  # --- Default p3_alloc_ratio ---
  if (is.null(p3_alloc_ratio)) p3_alloc_ratio <- alloc_ratio
  if (design == "single_arm" && !is.null(p2_data_ctrl)) {
    stop("p2_data_ctrl is not valid for a single-arm design", call. = FALSE)
  }

  # --- PPOS validation ---
  if (study_type == "confirmatory" && !is.null(p2_data)) {
    if (is.null(p3_n)) stop("p3_n is required when PPOS data are supplied", call. = FALSE)
    .qdf_scalar(p3_n, "p3_n", lower = 1, integer = TRUE)
    .qdf_scalar(p3_alpha, "p3_alpha", lower = 0, upper = 1,
                lower_open = TRUE, upper_open = TRUE)
    .qdf_scalar(p3_alloc_ratio, "p3_alloc_ratio", lower = 0, lower_open = TRUE)
    if (design == "controlled" && is.null(p2_data_ctrl)) {
      stop("p2_data_ctrl is required when confirmatory PPOS is requested for a controlled design",
           call. = FALSE)
    }
    .qdf_validate_arm(p2_data, endpoint_type, "p2_data")
    if (!is.null(p2_data_ctrl)) .qdf_validate_arm(p2_data_ctrl, endpoint_type, "p2_data_ctrl")
    if (endpoint_type == "continuous" && prior_params$kappa0 == 0 &&
        (p2_data$s2 <= 0 || (!is.null(p2_data_ctrl) && p2_data_ctrl$s2 <= 0)))
      stop("Joint Jeffreys normal posterior requires positive sample variance in each observed arm", call. = FALSE)

    if (design == "single_arm") {
      minimum <- if (endpoint_type == "continuous") 2 else 1
      if (p3_n < minimum) stop("p3_n is too small for this endpoint", call. = FALSE)
    } else {
      n_ctrl_p3 <- floor(p3_n / (p3_alloc_ratio + 1))
      n_trt_p3 <- p3_n - n_ctrl_p3
      minimum <- if (endpoint_type == "continuous") 2 else 1
      if (n_ctrl_p3 < minimum || n_trt_p3 < minimum) {
        stop("p3_n and p3_alloc_ratio must allocate at least ", minimum,
             " participant(s) to each arm", call. = FALSE)
      }
    }
  }

  # --- Favorable direction of the alternative ---
  # binary/continuous are validation-locked to "greater" and tte to "less";
  # incidence_rate legitimately supports both (protective reduction vs harm
  # detection). Stored explicitly so every analysis/OC/PPOS consumer tests the
  # SAME tail the design was sized for — deriving it ad hoc at each call site
  # is how the wrong-tail bug happened.
  direction <- if (alt_param > null_param) "greater" else "less"

  # --- Build config ---
  cfg <- list(
    endpoint_type      = endpoint_type,
    study_type         = study_type,
    design             = design,
    null_param         = null_param,
    alt_param          = alt_param,
    direction          = direction,
    sd                 = sd,
    alloc_ratio        = alloc_ratio,
    alphas             = alphas,
    powers             = powers,
    prior_params       = prior_params,
    prior_method       = if (endpoint_type == "continuous" && is.character(prior) &&
                              identical(prior, "jeffreys")) "normal_jeffreys_joint" else
                           if (is.list(prior)) "custom_proper" else paste(endpoint_type, prior, sep = "_"),
    go_threshold       = go_threshold,
    consider_threshold = consider_threshold,
    go_target          = go_target,
    p2_data            = p2_data,
    p2_data_ctrl       = p2_data_ctrl,
    p3_n               = p3_n,
    p3_alloc_ratio     = p3_alloc_ratio,
    p3_alpha           = p3_alpha,
    # TTE-specific
    accrual_time       = accrual_time,
    followup_time      = followup_time,
    tte_method         = if (endpoint_type == "tte") tte_method else NULL,
    # Incidence rate-specific
    exposure_time      = exposure_time,
    rate_method        = if (endpoint_type == "incidence_rate") rate_method else NULL,
    label              = label
  )
  class(cfg) <- "qdf_config"
  return(cfg)
}

#' Resolve prior specification to numeric hyperparameters
resolve_prior <- function(prior, endpoint_type, null_param) {
  if (is.list(prior)) {
    required <- switch(endpoint_type,
      binary = c("a", "b"),
      continuous = c("mu0", "kappa0", "alpha0", "beta0"),
      tte = c("shape", "rate"),
      incidence_rate = c("shape", "rate"),
      NULL
    )
    if (!is.null(required)) {
      missing <- setdiff(required, names(prior))
      if (length(missing) > 0)
        stop("Prior for ", endpoint_type, " is missing: ", paste(missing, collapse = ", "))
    }
    resolved <- prior
  } else if (endpoint_type == "binary") {
    resolved <- switch(prior,
      jeffreys  = list(a = 0.5, b = 0.5),
      flat      = list(a = 1, b = 1),
      skeptical = list(a = null_param * 5, b = (1 - null_param) * 5),
      stop("Unknown prior: ", prior, ". Use 'jeffreys', 'flat', 'skeptical', or list(a=, b=)")
    )
  } else if (endpoint_type == "continuous") {
    resolved <- switch(prior,
      # Joint Jeffreys density p(mu, variance) is proportional to variance^(-3/2).
      # The zero boundary gives a proper posterior for n>=2 and positive s2.
      jeffreys  = list(mu0 = 0, kappa0 = 0, alpha0 = 0, beta0 = 0),
      flat      = list(mu0 = 0, kappa0 = 0.001, alpha0 = 0.001, beta0 = 0.001),
      skeptical = list(mu0 = null_param, kappa0 = 1, alpha0 = 1, beta0 = 1),
      stop("Unknown prior: ", prior, ". Use 'jeffreys', 'flat', 'skeptical', or list(mu0=, kappa0=, alpha0=, beta0=)")
    )
  } else if (endpoint_type %in% c("tte", "incidence_rate")) {
    resolved <- switch(prior,
      jeffreys  = list(shape = 0.5, rate = 1e-6),
      flat      = list(shape = 0.001, rate = 0.001),
      skeptical = list(shape = 2, rate = 2 / null_param),
      stop("Unknown prior: ", prior, ". Use 'jeffreys', 'flat', 'skeptical', or list(shape=, rate=)")
    )
  }

  positive <- switch(endpoint_type,
    binary = c("a", "b"), continuous = c("kappa0", "alpha0", "beta0"),
    tte = c("shape", "rate"), incidence_rate = c("shape", "rate"))
  for (name in names(resolved)) .qdf_scalar(resolved[[name]], paste0("prior$", name))
  objective_normal <- endpoint_type == "continuous" &&
    identical(unname(unlist(resolved[c("kappa0", "alpha0", "beta0")])), c(0, 0, 0))
  if (objective_normal && is.list(prior))
    stop("Use the named jeffreys prior for the objective normal boundary", call. = FALSE)
  for (name in positive) {
    if (!objective_normal && resolved[[name]] <= 0) stop("prior$", name, " must be positive", call. = FALSE)
  }
  resolved
}

#' Print method for config
print.qdf_config <- function(x, ...) {
  cat("=== Quantitative Decision Framework Config ===\n")
  cat("  Endpoint:    ", x$endpoint_type, "\n")
  cat("  Study type:  ", x$study_type, "\n")
  cat("  Design:      ", x$design, "\n")
  cat("  H0:          ", x$null_param, "\n")
  cat("  H1:          ", x$alt_param, "\n")
  if (x$endpoint_type == "continuous") cat("  SD:          ", x$sd, "\n")
  if (x$endpoint_type == "tte") {
    cat("  Accrual:     ", x$accrual_time, "\n")
    cat("  Follow-up:   ", x$followup_time, "\n")
    cat("  TTE method:  ", x$tte_method, "\n")
    cat("  Median(H0):  ", round(log(2) / x$null_param, 2), "\n")
    cat("  Median(H1):  ", round(log(2) / x$alt_param, 2), "\n")
  }
  if (x$endpoint_type == "incidence_rate") {
    cat("  Exposure:    ", x$exposure_time, "\n")
    cat("  Rate method: ", x$rate_method, "\n")
    cat("  Direction:   ", if (x$direction == "greater") "harm detection (rate increase)"
                           else "protective (rate reduction)", "\n")
  }
  if (x$design == "controlled") cat("  Allocation:  ", x$alloc_ratio, ":1\n")
  cat("  Alpha(s):    ", paste(x$alphas, collapse = ", "), "\n")
  cat("  Power(s):    ", paste(x$powers, collapse = ", "), "\n")
  cat("  Go target:   ", x$go_target, "\n")
  cat("  Go threshold:", x$go_threshold, " | Consider:", x$consider_threshold, "\n")
  if (!is.null(x$p2_data)) {
    cat("  P2 data:      provided (PPOS enabled)\n")
    cat("  P3 N:        ", x$p3_n, "\n")
  }
  cat("  Label:       ", x$label, "\n")
  invisible(x)
}

cat("config.R loaded.\n")
