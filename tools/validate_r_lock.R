#!/usr/bin/env Rscript

# Fail closed when the R runtime or installed engine packages drift from the
# repository lock. Optional packages are checked only when installed so the
# documented fallback remains a supported, reproducible configuration.

script_arg <- grep("^--file=", commandArgs(trailingOnly = FALSE), value = TRUE)
if (length(script_arg) != 1L) {
  stop("could not resolve validator path", call. = FALSE)
}
script_path <- normalizePath(sub("^--file=", "", script_arg), mustWork = TRUE)
root <- dirname(dirname(script_path))
lock_path <- file.path(root, "renv.lock")

if (!requireNamespace("jsonlite", quietly = TRUE)) {
  stop("jsonlite is required to validate renv.lock", call. = FALSE)
}
if (!file.exists(lock_path)) {
  stop("renv.lock is missing", call. = FALSE)
}

`%||%` <- function(lhs, rhs) if (is.null(lhs)) rhs else lhs

lock <- jsonlite::fromJSON(lock_path, simplifyVector = FALSE)
locked_r <- lock$R$Version
if (!is.character(locked_r) || length(locked_r) != 1L ||
    !identical(as.character(getRversion()), locked_r)) {
  stop(
    sprintf("R runtime does not match renv.lock (installed %s, locked %s)",
            as.character(getRversion()), locked_r %||% "missing"),
    call. = FALSE
  )
}

required <- c("jsonlite", "mvtnorm", "survival")
optional <- c("Exact", "MAMS")
locked_packages <- lock$Packages
if (!is.list(locked_packages)) {
  stop("renv.lock Packages must be an object", call. = FALSE)
}

locked_version <- function(package) {
  record <- locked_packages[[package]]
  version <- if (is.list(record)) record$Version else NULL
  if (!is.character(version) || length(version) != 1L) {
    stop(sprintf("renv.lock is missing a version for %s", package), call. = FALSE)
  }
  version
}

dependency_names <- function(record) {
  fields <- c("Depends", "Imports", "LinkingTo")
  values <- unlist(record[intersect(names(record), fields)], use.names = FALSE)
  if (!length(values)) return(character())
  entries <- trimws(unlist(strsplit(as.character(values), ",", fixed = TRUE)))
  matches <- regexec("^([A-Za-z][A-Za-z0-9.]*)", entries)
  parsed <- regmatches(entries, matches)
  unique(vapply(parsed[lengths(parsed) >= 2L], `[[`, character(1), 2L))
}

# Optional roots must retain lock records even on a fallback-only host, while
# only installed optional branches make their dependency closure applicable.
invisible(lapply(c(required, optional), locked_version))
installed <- utils::installed.packages()
installed_names <- rownames(installed)
base_packages <- installed_names[installed[, "Priority"] %in% "base"]
active_roots <- c(required, optional[optional %in% installed_names])
missing_required <- setdiff(required, installed_names)
if (length(missing_required)) {
  stop(
    sprintf("required R package is missing: %s", paste(missing_required, collapse = ", ")),
    call. = FALSE
  )
}

applicable <- character()
queue <- active_roots
while (length(queue)) {
  package <- queue[[1L]]
  queue <- queue[-1L]
  if (package %in% applicable || package %in% base_packages || identical(package, "R")) {
    next
  }
  record <- locked_packages[[package]]
  if (!is.list(record)) {
    stop(
      sprintf("renv.lock is missing an applicable dependency record for %s", package),
      call. = FALSE
    )
  }
  locked_version(package)
  applicable <- c(applicable, package)
  for (dependency in dependency_names(record)) {
    if (dependency %in% base_packages || identical(dependency, "R")) next
    if (!is.list(locked_packages[[dependency]])) {
      stop(
        sprintf(
          "renv.lock is missing dependency %s required by %s",
          dependency, package
        ),
        call. = FALSE
      )
    }
    if (!dependency %in% applicable) queue <- c(queue, dependency)
  }
}

# Also validate every lock record that is installed, even if it is reserved for
# a currently inactive optional branch. Uninstalled inactive records remain a
# reproducible fallback rather than becoming a hard dependency.
applicable <- sort(unique(c(
  applicable,
  intersect(names(locked_packages), installed_names)
)))
for (package in applicable) {
  expected <- locked_version(package)
  if (!package %in% installed_names) {
    stop(sprintf("applicable R package is missing: %s", package), call. = FALSE)
  }
  actual <- installed[package, "Version"]
  versions_match <- tryCatch(
    package_version(actual) == package_version(expected),
    error = function(...) FALSE
  )
  if (!isTRUE(versions_match)) {
    stop(
      sprintf("R package %s does not match renv.lock (installed %s, locked %s)",
              package, actual, expected),
      call. = FALSE
    )
  }
}

exact_ready <- "Exact" %in% installed_names
cat("Validated R ", locked_r, " and ", length(applicable),
    " applicable package locks/dependencies", sep = "")
if (exact_ready) cat("; Exact branch available")
if ("MAMS" %in% installed_names) cat("; MAMS branch available")
cat("\n")
