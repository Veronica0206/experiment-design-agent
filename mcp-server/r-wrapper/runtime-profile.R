# The installation selects capabilities in a repository-owned manifest. Neither
# dispatcher arguments nor environment variables can reduce required engines.
load_runtime_profile <- function(suite_root) {
  invalid <- function() stop("Repository runtime profile is invalid")
  path <- file.path(suite_root, "governance", "runtime-profiles.json")
  if (!file.exists(path) || dir.exists(path) || nzchar(Sys.readlink(dirname(path))) ||
      nzchar(Sys.readlink(path)) ||
      file.info(path)$size > 65536) invalid()
  value <- jsonlite::fromJSON(path, simplifyVector = FALSE)
  exact_keys <- function(x, keys) {
    is.list(x) && length(x) == length(keys) &&
      !anyDuplicated(names(x)) && setequal(names(x), keys)
  }
  skills <- c("vera-experiment-designing", "vera-master-experiment-designing",
              "vera-doe-designing", "vera-indirect-comparing", "vera-meta-analyzing")
  tools <- c("validate_config", "sample_size", "simulate_design", "run_tests",
             "master_simulate", "ab_test", "factorial_design", "rsm_design",
             "randomize", "indirect_compare", "meta_analyze")
  domains <- c("single_endpoint", "master_protocol", "doe", "randomization",
               "indirect_comparison", "meta_analysis")
  packages <- c("jsonlite", "survival", "Exact", "mvtnorm", "MAMS")
  counts <- setNames(as.list(c(31L, 53L, 14L, 15L, 14L)), skills)
  if (!exact_keys(value, c("schema_version", "active", "profiles")) ||
      !identical(value$schema_version, 1L) ||
      !is.character(value$active) || length(value$active) != 1L ||
      !value$active %in% c("complete", "single-endpoint") ||
      !exact_keys(value$profiles, c("complete", "single-endpoint"))) invalid()
  expected <- list(
    complete = list(skills = skills, domains = domains, tools = tools,
                    r_packages = packages, expected_regression_pass_counts = counts),
    `single-endpoint` = list(skills = skills[1], domains = domains[1], tools = tools[1:4],
                            r_packages = packages[1:3],
                            expected_regression_pass_counts = counts[1])
  )
  for (name in names(expected)) {
    profile <- value$profiles[[name]]
    reference <- expected[[name]]
    if (!exact_keys(profile, names(reference))) invalid()
    for (field in c("skills", "domains", "tools", "r_packages")) {
      # JSON arrays remain unnamed lists; reject scalars, objects, and duplicates.
      if (!is.list(profile[[field]]) || !is.null(names(profile[[field]])) ||
          !identical(profile[[field]], as.list(reference[[field]]))) invalid()
    }
    observed <- profile$expected_regression_pass_counts
    if (!exact_keys(observed, names(reference$expected_regression_pass_counts)) ||
        !identical(observed[names(reference$expected_regression_pass_counts)],
                   reference$expected_regression_pass_counts)) invalid()
  }
  c(list(name = value$active), expected[[value$active]])
}
