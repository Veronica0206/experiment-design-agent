"""Deterministic, privacy-aware rendering of verified analysis results.

The model may explain an elicitation question freely, but it must never be the
authority that transcribes a completed numeric analysis.  Both the Python
harness and the Claude stop hook use this renderer so the final answer is an
exact function of the verified arguments, result, and identity.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from verification import (PUBLIC_LIMITATION_MESSAGES, canonical_value,
                          content_hash,
                          domain_arguments, identity_matches_call,
                          public_check_summary, public_envelope_matches_call,
                          public_limitation_codes)


# Public reports are explicit data-transfer contracts.  Unknown fields are
# private by default, and a field is admitted only at its documented tool/path
# and in the correct direction (argument or result).  This prevents a new R
# field, a derived label alias, or a numeric field borrowed from another tool
# from silently becoming model-visible.
_ENUM_VALUES = {
    "alpha_type": {"rotatable", "face", "custom"},
    "analysis_scale": {
        "identity", "log", "log_odds_ratio", "log_hazard_ratio", "log_rate_ratio",
        "mean_difference", "risk_difference", "rate_difference",
    },
    "decision": {"go", "no-go", "consider", "pruned"},
    "design": {"single_arm", "controlled", "ccd", "bbd"},
    "effect_measure": {
        "log_odds_ratio", "log_hazard_ratio", "mean_difference", "log_rate_ratio",
        "risk_difference", "rate_difference", "odds_ratio", "hazard_ratio", "rate_ratio",
    },
    "effect_scale": {"difference", "log_hazard_ratio", "rate_difference", "log_rate_ratio"},
    "effect_type": {"absolute", "relative"},
    "endpoint_type": {
        "binary", "continuous", "tte", "incidence_rate", "binary_single",
        "binary_comparative", "continuous_single", "continuous_comparative",
        "time_to_event", "incidence_single", "incidence_comparative",
    },
    "inference_method": {"hksj", "dl_z", "fixed_normal"},
    "borrowing_method": {
        "none", "complete", "simon_two_stage", "simons_bayesian",
        "chen_confirmatory", "wathen_sti", "cbhm", "full_bhm", "snti",
        "sep_gibbs",
    },
    "chen_strategy": {"d1", "d2", "d3"},
    "fwer_control": {"bonferroni", "none"},
    "maic_endpoint_type": {"binary", "continuous", "rate", "tte"},
    "master_design_type": {"basket", "umbrella", "platform"},
    "method": {"simple", "block", "stratified", "bucher", "maic"},
    "metric": {"proportion", "mean"},
    "ncc_method": {"none", "pooled", "regression", "time_machine"},
    "phase": {"phase2", "phase3"},
    "point_type": {"factorial", "axial", "center", "edge"},
    "power_type": {"one_minimum"},
    "rate_method": {"poisson"},
    "selection_rule": {"rank_best", "threshold"},
    "study_type": {"signal_detection", "poc", "confirmatory"},
    "test": {"exact_binomial", "one_sample_t", "z_unpooled", "two_sample_t",
             "exponential_rate", "logrank", "exact_poisson", "poisson_rate_ratio"},
    "tte_method": {"exponential", "cox"},
    "type": {"full_factorial", "fractional_factorial", "central_composite", "box_behnken"},
    "umbrella_method": {"mams", "drop_the_losers", "bayesian_adaptive_randomization"},
}

_BOOLEAN_FIELDS = {
    "interim_stopping_applied", "orthogonal", "random", "randomize",
    "rar_enabled", "shared_control", "truly_active",
}

_PRIOR_NAMES = {"jeffreys", "flat", "skeptical"}
_CUSTOM_PRIOR_FIELDS = {
    "a", "b", "mu0", "kappa0", "alpha0", "beta0", "shape", "rate",
}

_ENUM_ALIASES = {
    ("meta_analyze", "inference_method"): {
        "dl tau2 with modified hksj t inference": "hksj",
        "dl tau2 with normal inference": "dl_z",
        "fixed effect normal inference": "fixed_normal",
    },
}

_MASTER_METRIC_CODES = {
    "fwer (%)": "fwer_percent",
    "mean total n": "mean_total_n",
    "mean duration (periods)": "mean_duration_periods",
    "1-minimum power (%)": "one_minimum_power_percent",
    "complete power (%)": "complete_power_percent",
    "complete correct power (%)": "complete_correct_power_percent",
    "p(recommend best) (%)": "recommend_best_percent",
    "p(recommend effective) (%)": "recommend_effective_percent",
    "total n (fixed)": "total_n_fixed",
}

_MASTER_DESIGN_CODES = {
    "umbrella (mams)": "umbrella_mams",
    "traditional (separate studies)": "traditional_separate_studies",
}

_MASTER_SCENARIO_CODES = {"global null": "global_null"}
_MASTER_MULTIPLICITY_CODES = {
    "bonferroni final; futility-only interim stopping": (
        "bonferroni_final_futility_interim"
    ),
}
_MASTER_BOUNDARY_SOURCE_CODES = {
    "mams_package": "mams_package",
    "approximation": "approximation",
    "user_supplied": "user_supplied",
}

# These are exact engine spellings that are not lowercase. They are admitted
# only at the documented tool/direction/path and emitted under one canonical
# lowercase key. Arbitrary case aliases remain private-by-default.
_SOURCE_KEY_ALIASES = {
    ("simulate_design", "arguments", ()): {"B_oc": "b_oc"},
    ("simulate_design", "result", ()): {"B_used": "b_used"},
    ("master_simulate", "result", ("result",)): {
        "typeI_table": "type_i_table",
    },
    ("master_simulate", "result", ("result", "type_i_table")): {
        "type_I_error": "type_i_error",
    },
}

_SINGLE_CONFIG = {
    "endpoint_type", "study_type", "design", "null_param", "alt_param", "sd",
    "alloc_ratio", "alphas", "powers", "go_threshold", "consider_threshold",
    "go_target", "accrual_time", "followup_time", "exposure_time", "tte_method",
    "rate_method", "prior",
}
_SAMPLE_ROW = {
    "design", "test", "alpha", "power_target", "power_achieved", "n", "n_total",
    "n_trt", "n_ctrl", "k_crit", "events", "allocation_ratio", "alloc_ratio",
}
_OC_ROW = {
    "design", "test", "true_param", "null_param", "alt_param", "n", "n_total",
    "n_trt", "n_ctrl", "alpha", "power", "p_go", "p_consider", "p_nogo",
}
_PPOS_FIELDS = {
    "ppos", "p3_n", "p3_n_trt", "p3_n_ctrl", "p3_alpha", "p3_k_crit",
    "p2_post_a", "p2_post_b", "p2_post_mean", "p2_post_ci",
    "p2_trt_post", "p2_ctrl_post", "p2_trt_mean", "p2_ctrl_mean",
    "diff_post_mean", "p2_post_mu", "p2_post_df", "p2_post_scale",
    "mu_draws_mean", "sigma_draws_mean", "hr_post_mean", "rr_post_mean",
    "cond_power_mean", "cond_power_median", "cond_power_q25",
    "cond_power_q75",
}
_MASTER_ROW = {
    "subgroup", "arm", "scenario", "estimate", "se", "lower", "upper", "power",
    "reject_rate", "fwer", "type1_error", "n", "n_total", "decision", "stage",
    "null_param", "alt_param", "truly_active", "mean_estimate", "true_effect",
    "effect_scale", "per_arm_power", "cond_estimate", "selection_prob", "metric",
    "value", "expected_n", "enter_period", "leave_period", "true_alt_param",
    "true_null_param", "mean_n", "mean_early_posterior_prob",
    "mean_final_p_value", "type_i_error",
}
_BUCHER_ROW = {
    "estimate", "se", "lower", "upper", "ci_lower", "ci_upper", "natural_estimate",
    "natural_lower", "natural_upper", "effect_measure",
}
_SOURCE_EFFECT_ROW = _BUCHER_ROW | {
    "effect", "bootstrap_replicates", "bootstrap_seed",
    "bootstrap_replicates_requested", "bootstrap_replicates_used",
}

# Mapping path -> exact fields permitted at that mapping.  List rows use the
# same path as the containing list (for example ("results",)).
_RESULT_FIELDS_BY_PATH: dict[str, dict[tuple[str, ...], set[str]]] = {
    "validate_config": {(): set(_SINGLE_CONFIG)},
    "sample_size": {(): {"results"}, ("results",): set(_SAMPLE_ROW)},
    "simulate_design": {
        (): {"sample_size", "oc", "ppos", "seed", "b_used", "n_oc_used"},
        ("sample_size",): set(_SAMPLE_ROW),
        ("oc",): set(_OC_ROW),
        ("ppos",): set(_PPOS_FIELDS),
        ("n_oc_used",): {"n", "n_trt", "n_ctrl"},
    },
    "master_simulate": {
        (): {"result"},
        ("result",): {
            "oc_table", "fwer_table", "power_table", "sample_size_table",
            "arm_results", "type_i_table", "alloc_df", "decision_matrix",
            "multiplicity_method", "final_alpha_per_arm",
            "interim_stopping_applied", "boundary_source",
        },
        ("result", "oc_table"): set(_MASTER_ROW),
        ("result", "fwer_table"): set(_MASTER_ROW),
        ("result", "power_table"): set(_MASTER_ROW),
        ("result", "sample_size_table"): set(_MASTER_ROW) | {"design"},
        ("result", "arm_results"): set(_MASTER_ROW),
        ("result", "type_i_table"): set(_MASTER_ROW),
    },
    "indirect_compare": {
        (): {"method", "comparisons", "estimate", "se", "lower", "upper",
             "ci_lower", "ci_upper", "natural_estimate", "natural_lower",
             "natural_upper", "effect_measure", "weight_summary", "balance", "ess",
             "arm_summary", "source_effect", "anchored_result", "n", "n_source",
             "n_target", "mean_weight", "sd_weight", "min_weight", "max_weight",
             "bootstrap_replicates", "bootstrap_seed", "maic_endpoint_type",
             "tte_method"},
        ("comparisons",): set(_BUCHER_ROW),
        ("weight_summary",): {"n", "sum", "mean", "min", "q25", "median", "q75", "max"},
        ("balance",): {"target_mean", "source_unweighted_mean", "source_weighted_mean",
                       "unweighted_gap", "weighted_gap"},
        ("ess",): {"n", "weight_sum", "ess"},
        ("arm_summary",): {"n", "weight_sum", "weighted_events", "weighted_rate",
                           "mean", "sd", "estimate", "se"},
        ("source_effect",): set(_SOURCE_EFFECT_ROW),
        ("anchored_result",): set(_SOURCE_EFFECT_ROW),
    },
    "meta_analyze": {
        (): {"estimate", "se", "lower", "upper", "tau2", "q", "i2", "k", "k_used",
             "n_input", "dropped_studies", "random", "inference_method",
             "effect_measure", "study_effects", "study_variances"},
    },
    "ab_test": {
        (): {"baseline", "effect", "effect_type", "metric", "sd", "alpha", "power",
             "ratio", "sided", "n_total", "n_control", "n_treatment", "mde",
             "target_power", "achieved_power", "allocation_ratio"},
    },
    "factorial_design": {
        (): {"n_factors", "levels", "fraction", "replicates", "center_points",
             "randomize", "seed", "n_runs", "resolution", "orthogonal", "design", "type"},
    },
    "rsm_design": {
        (): {"n_factors", "alpha", "fraction", "center_points", "randomize", "seed",
             "n_runs", "n_factorial", "n_axial", "n_center", "n_edge", "design",
             "type", "alpha_type"},
    },
    "randomize": {(): {"n", "method", "block_size", "block_size_used", "ratio", "seed"}},
}

_MASTER_CONFIG = {
    "master_design_type", "endpoint_type", "n_subgroups", "null_params", "alt_params",
    "n_per_subgroup", "n_sims", "seed", "alpha", "n_arms", "n_stages",
    "n_per_arm_stage", "n_periods", "n_per_period", "arms_schedule", "go_threshold",
    "nogo_threshold", "futility_threshold", "effect_threshold", "sd", "accrual_time",
    "followup_time", "exposure_time", "tte_method", "rate_method", "borrowing_method",
    "phase", "n_interims", "n_per_interim", "tau_prior", "homogeneity_prior",
    "response_prior", "cbhm_a", "cbhm_b", "ia_pruning_alpha", "chen_strategy",
    "umbrella_method", "futility_boundaries", "n_drop_per_stage", "rar_gamma",
    "selection_rule", "power_type", "shared_control", "ncc_method", "ncc_weight_decay",
    "rar_enabled", "rar_burn_in", "rar_min_alloc", "interim_frequency", "fwer_control",
}
_ARG_FIELDS_BY_PATH: dict[str, dict[tuple[str, ...], set[str]]] = {
    "validate_config": {(): set(_SINGLE_CONFIG)},
    "sample_size": {(): set(_SINGLE_CONFIG)},
    "simulate_design": {
        (): {"config", "seed", "n_oc", "b_oc", "delta"},
        ("config",): set(_SINGLE_CONFIG) | {"p2_data", "p2_data_ctrl", "p3_n",
                                                  "p3_alloc_ratio", "p3_alpha"},
        ("config", "prior"): set(_CUSTOM_PRIOR_FIELDS),
        ("n_oc",): {"n", "n_trt", "n_ctrl"},
    },
    "master_simulate": {
        (): {"config"},
        ("config",): set(_MASTER_CONFIG),
        ("config", "arms_schedule"): {"enter", "leave"},
        ("config", "tau_prior"): {"type", "params"},
        ("config", "tau_prior", "params"): {"scale"},
    },
    "indirect_compare": {
        (): {"method", "comparisons", "covariates", "alpha", "analysis_scale",
             "effect_measure", "maic_endpoint_type", "tte_method", "bootstrap_replicates",
             "bootstrap_seed", "n_source", "n_target"},
    },
    "meta_analyze": {
        (): {"endpoint_type", "studies", "random", "alpha", "inference_method",
             "effect_measure"},
    },
    "ab_test": {(): set(_RESULT_FIELDS_BY_PATH["ab_test"][()])},
    "factorial_design": {
        (): {"n_factors", "levels", "fraction", "replicates", "center_points",
             "randomize", "seed"},
    },
    "rsm_design": {
        (): {"n_factors", "design", "alpha", "fraction", "center_points",
             "randomize", "seed"},
    },
    "randomize": {(): {"n", "method", "block_size", "ratio", "seed"}},
}

_NUMERIC_SEQUENCE_PATHS = {
    ("validate_config", "arguments", ("alphas",)),
    ("validate_config", "arguments", ("powers",)),
    ("sample_size", "arguments", ("alphas",)),
    ("sample_size", "arguments", ("powers",)),
    ("simulate_design", "arguments", ("config", "alphas")),
    ("simulate_design", "arguments", ("config", "powers")),
    ("master_simulate", "arguments", ("config", "null_params")),
    ("master_simulate", "arguments", ("config", "alt_params")),
    ("master_simulate", "arguments", ("config", "arms_schedule", "enter")),
    ("master_simulate", "arguments", ("config", "arms_schedule", "leave")),
    ("master_simulate", "arguments", ("config", "futility_boundaries")),
    ("master_simulate", "arguments", ("config", "n_drop_per_stage")),
    ("factorial_design", "arguments", ("levels",)),
    ("factorial_design", "result", ("levels",)),
    ("randomize", "arguments", ("ratio",)),
    ("randomize", "result", ("ratio",)),
    ("simulate_design", "result", ("ppos", "p2_post_ci")),
    ("simulate_design", "result", ("ppos", "p2_trt_post")),
    ("simulate_design", "result", ("ppos", "p2_ctrl_post")),
}

_SUMMARY_PATHS = {
    ("simulate_design", "arguments", ("config", "p2_data")),
    ("simulate_design", "arguments", ("config", "p2_data_ctrl")),
    ("indirect_compare", "arguments", ("comparisons",)),
    ("indirect_compare", "arguments", ("covariates",)),
    ("meta_analyze", "arguments", ("studies",)),
    ("meta_analyze", "result", ("study_effects",)),
    ("meta_analyze", "result", ("study_variances",)),
    ("master_simulate", "result", ("result", "alloc_df")),
    ("master_simulate", "result", ("result", "decision_matrix")),
}

_SCALAR_OR_MAPPING_PATHS = {
    ("simulate_design", "arguments", ("n_oc",)),
    ("simulate_design", "result", ("n_oc_used",)),
}

_PRIVATE_ARTIFACT_FIELDS = {"artifacts", "private_assignment_artifact", "output_dir"}
_DROP = object()

_CLARIFICATION_FIELDS = {
    "endpoint_type", "study_type", "design", "estimand", "null_param", "alt_param",
    "sd", "alpha", "alpha_sidedness", "power", "allocation_ratio", "sample_size", "number_of_arms",
    "number_of_stages", "decision_threshold", "prior", "followup_time", "exposure_time",
    "randomization_method", "factor_levels", "analysis_method", "data_source",
}
_CLARIFICATION_PREFIX = "CLARIFICATION_REQUEST "

def _collection_summary(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"available", "length"}:
        length = value.get("length")
        if (isinstance(value.get("available"), bool)
                and isinstance(length, int) and not isinstance(length, bool)
                and length >= 0):
            return {"available": value["available"], "length": length}
        return _DROP
    if isinstance(value, (dict, list, tuple)):
        return {"available": True, "length": len(value)}
    return _DROP


def _number(value: Any) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return _DROP
    if isinstance(value, float) and (value != value or value in {float("inf"), float("-inf")}):
        return _DROP
    return value


def _bounded_sequence(
    tool: str,
    value: list[Any] | tuple[Any, ...],
    path: tuple[str, ...],
    schemas: dict[str, dict[tuple[str, ...], set[str]]],
    mode: str,
) -> Any:
    if len(value) > 200:
        return _collection_summary(value)
    projected: list[Any] = []
    for item in value:
        if isinstance(item, dict):
            public = _project_mapping(tool, item, path, schemas, mode)
        elif isinstance(item, (list, tuple)):
            public = _bounded_sequence(tool, item, path, schemas, mode)
        else:
            public = _DROP
        if public is not _DROP:
            projected.append(public)
    return projected


def _factor_name_sort_key(name: str) -> tuple[int, int, str]:
    """Sort canonical public aliases numerically and all other names stably."""
    prefix = "factor_"
    if name.startswith(prefix):
        suffix = name[len(prefix):]
        # Generated aliases are positive, canonical decimal integers.  Compare
        # by digit length and then text so even an unexpectedly large suffix is
        # ordered numerically without converting attacker-controlled text.
        if suffix and suffix.isascii() and suffix.isdigit() and suffix[0] != "0":
            return (0, len(suffix), suffix)
    return (1, 0, name)


def _project_design(value: Any) -> Any:
    """Preserve a numeric design matrix while pseudonymizing factor names."""
    if not isinstance(value, (list, tuple)):
        return _DROP
    if len(value) > 200:
        return _collection_summary(value)
    rows = [row for row in value if isinstance(row, dict)]
    factor_names = sorted(
        {
            str(key) for row in rows for key, item in row.items()
            if str(key).lower() not in {"point_type", "run"}
            and _number(item) is not _DROP
        },
        key=_factor_name_sort_key,
    )
    aliases = {name: f"factor_{index + 1}" for index, name in enumerate(factor_names)}
    public_rows: list[dict[str, Any]] = []
    for row in rows:
        public_row: dict[str, Any] = {}
        for key, item in sorted(row.items(), key=lambda pair: str(pair[0])):
            name = str(key)
            lowered = name.lower()
            if name in aliases:
                public = _number(item)
                if public is not _DROP:
                    public_row[aliases[name]] = public
                continue
            if lowered == "run":
                public = _number(item)
                if public is not _DROP:
                    public_row["run"] = public
            elif lowered == "point_type" and isinstance(item, str):
                normalized = item.lower()
                if normalized in _ENUM_VALUES["point_type"]:
                    public_row["point_type"] = normalized
        public_rows.append(public_row)
    return public_rows


def _project_randomize(value: Any, mode: str) -> Any:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    schemas = _ARG_FIELDS_BY_PATH if mode == "arguments" else _RESULT_FIELDS_BY_PATH
    for key in ("n", "method", "block_size", "block_size_used", "ratio", "seed"):
        if key in value:
            public = _project_value("randomize", key, value[key], (), schemas, mode)
            if public is not _DROP:
                safe[key] = public
    arms = value.get("arms")
    counts = value.get("counts")
    arm_names = [str(item) for item in arms] if isinstance(arms, (list, tuple)) else []
    if not arm_names and isinstance(counts, dict):
        arm_names = [str(name) for name in counts]
    aliases = {name: f"arm_{index + 1}" for index, name in enumerate(arm_names)}
    if aliases:
        safe["arms"] = list(aliases.values())
    if isinstance(counts, dict):
        safe_counts: dict[str, Any] = {}
        for index, (name, count) in enumerate(counts.items()):
            public = _number(count)
            if public is not _DROP:
                safe_counts[aliases.get(str(name), f"arm_{index + 1}")] = public
        safe["counts"] = safe_counts
    private_count = 1 if value.get("private_assignment_artifact") else 0
    if isinstance(value.get("artifacts"), (list, tuple)):
        private_count += len(value["artifacts"])
    if private_count:
        safe["private_artifacts"] = {"available": True, "length": private_count}
    elif "private_artifacts" in value:
        summary = _collection_summary(value["private_artifacts"])
        if summary is not _DROP:
            safe["private_artifacts"] = summary
    return safe


def _project_value(
    tool: str,
    key: str,
    value: Any,
    path: tuple[str, ...],
    schemas: dict[str, dict[tuple[str, ...], set[str]]],
    mode: str,
) -> Any:
    lowered = key.lower()
    schema = schemas.get(tool, {})
    allowed = schema.get(path, set())
    if lowered not in allowed:
        return _DROP
    child_path = path + (lowered,)
    contract = (tool, mode, child_path)
    summary = _collection_summary(value)
    if contract in _SUMMARY_PATHS:
        return summary
    if summary is not _DROP and isinstance(value, dict) and set(value) == {"available", "length"}:
        return summary
    if lowered == "design" and tool in {"factorial_design", "rsm_design"} and mode == "result":
        return _project_design(value)
    if tool == "master_simulate" and lowered == "metric":
        if not isinstance(value, str):
            return _DROP
        normalized = value.lower()
        if normalized in _MASTER_METRIC_CODES.values():
            return normalized
        return _MASTER_METRIC_CODES.get(normalized, _DROP)
    if tool == "master_simulate" and lowered == "design":
        if not isinstance(value, str):
            return _DROP
        normalized = value.lower()
        if normalized in _MASTER_DESIGN_CODES.values():
            return normalized
        return _MASTER_DESIGN_CODES.get(normalized, _DROP)
    if tool == "master_simulate" and lowered == "scenario":
        if not isinstance(value, str):
            return _DROP
        normalized = value.lower()
        if normalized in _MASTER_SCENARIO_CODES.values():
            return normalized
        return _MASTER_SCENARIO_CODES.get(normalized, _DROP)
    if tool == "master_simulate" and lowered == "multiplicity_method":
        if not isinstance(value, str):
            return _DROP
        normalized = value.lower()
        if normalized in _MASTER_MULTIPLICITY_CODES.values():
            return normalized
        return _MASTER_MULTIPLICITY_CODES.get(normalized, _DROP)
    if tool == "master_simulate" and lowered == "boundary_source":
        if not isinstance(value, str):
            return _DROP
        return _MASTER_BOUNDARY_SOURCE_CODES.get(value.lower(), _DROP)
    if lowered == "decision":
        if not isinstance(value, str):
            return _DROP
        normalized = value.lower()
        if normalized.startswith("no-go"):
            return "no-go"
        if normalized.startswith("go"):
            return "go"
        if normalized.startswith("consider"):
            return "consider"
        if normalized.startswith("pruned"):
            return "pruned"
        return _DROP
    if mode == "arguments" and lowered == "prior" and tool in {
        "validate_config", "sample_size", "simulate_design",
    }:
        if isinstance(value, str):
            normalized = value.lower()
            return normalized if normalized in _PRIOR_NAMES else _DROP
        if isinstance(value, dict) and value and set(value) <= _CUSTOM_PRIOR_FIELDS:
            projected = {str(name): _number(item) for name, item in sorted(value.items())}
            return projected if all(item is not _DROP for item in projected.values()) else _DROP
        return _DROP
    if (tool == "master_simulate" and mode == "arguments"
            and path == ("config", "tau_prior") and lowered == "type"):
        return "half_normal" if value == "half_normal" else _DROP
    if lowered in _ENUM_VALUES:
        if not isinstance(value, str):
            return _DROP
        normalized = value.lower()
        normalized = _ENUM_ALIASES.get((tool, lowered), {}).get(normalized, normalized)
        return normalized if normalized in _ENUM_VALUES.get(lowered, set()) else _DROP
    if lowered in _BOOLEAN_FIELDS:
        return value if isinstance(value, bool) else _DROP
    if value is None:
        return None if child_path not in schema else _DROP
    numeric = _number(value)
    if numeric is not _DROP:
        return (numeric if child_path not in schema
                or contract in _SCALAR_OR_MAPPING_PATHS else _DROP)
    if isinstance(value, dict):
        return (_project_mapping(tool, value, child_path, schemas, mode)
                if child_path in schema else _DROP)
    if isinstance(value, (list, tuple)):
        if contract in _NUMERIC_SEQUENCE_PATHS:
            projected = [_number(item) for item in value]
            return [item for item in projected if item is not _DROP]
        return (_bounded_sequence(tool, value, child_path, schemas, mode)
                if child_path in schema else _DROP)
    return _DROP


def _project_mapping(
    tool: str,
    value: dict[Any, Any],
    path: tuple[str, ...],
    schemas: dict[str, dict[tuple[str, ...], set[str]]],
    mode: str,
) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    private_count = 0
    for item_key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
        if not isinstance(item_key, str):
            continue
        aliases = _SOURCE_KEY_ALIASES.get((tool, mode, path), {})
        lowered = item_key if item_key == item_key.lower() else aliases.get(item_key)
        if lowered is None:
            continue
        if lowered == "private_artifacts" and path == ():
            summary = _collection_summary(item)
            if summary is not _DROP:
                safe["private_artifacts"] = summary
            continue
        if lowered in _PRIVATE_ARTIFACT_FIELDS:
            if lowered != "output_dir":
                private_count += len(item) if isinstance(item, (list, tuple)) else int(bool(item))
            continue
        public = _project_value(tool, lowered, item, path, schemas, mode)
        if public is not _DROP:
            safe[lowered] = public
    if private_count:
        safe["private_artifacts"] = {"available": True, "length": private_count}
    return safe


def privacy_safe_view(tool: str, value: Any) -> Any:
    """Return the allowlisted, deterministic model-visible result DTO."""
    if tool == "randomize":
        return _project_randomize(value, "result")
    if not isinstance(value, dict):
        return {}
    return _project_mapping(tool, value, (), _RESULT_FIELDS_BY_PATH, "result")


def public_arguments_view(tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Return the allowlisted public DTO for resolved tool arguments."""
    value = domain_arguments(arguments)
    if tool == "randomize":
        return _project_randomize(value, "arguments")
    return _project_mapping(tool, value, (), _ARG_FIELDS_BY_PATH, "arguments")


def _normal_status(envelope: dict[str, Any]) -> str:
    blocked = list(envelope.get("blocked") or [])
    return "PASS_PARTIAL" if blocked else "VERIFIED"


def _validate_presentable_envelope(envelope: dict[str, Any]) -> None:
    status = str(envelope.get("status") or "")
    blocked = list(envelope.get("blocked") or [])
    failures = list(envelope.get("failures") or [])
    checks = envelope.get("checks") if isinstance(envelope.get("checks"), dict) else {}
    expected = "PASS_PARTIAL" if blocked else "VERIFIED"
    if envelope.get("presentable") is not True or status != expected:
        raise ValueError("verification envelope status is not presentable")
    if failures or any(value is False for value in checks.values()):
        raise ValueError("verification envelope contains failed checks")


def _render_report(
    tool: str,
    assumptions: dict[str, Any],
    safe_result: dict[str, Any],
    envelope: dict[str, Any],
) -> str:
    identity = envelope.get("identity") if isinstance(envelope, dict) else None
    if not isinstance(identity, dict):
        raise ValueError("verification identity is required for canonical reporting")
    analysis_id = str(identity.get("analysis_id") or "")
    public_result_hash = content_hash(safe_result)
    _validate_presentable_envelope(envelope)
    if not analysis_id:
        raise ValueError("only presentable identity-bound results can be rendered")
    raw_checks = envelope.get("checks") if isinstance(envelope.get("checks"), dict) else {}
    checks = public_check_summary(raw_checks)
    check_text = ", ".join(
        f"{name}={'pass' if value is True else 'fail'}"
        for name, value in sorted(checks.items())
    ) or "none recorded"
    blocked = public_limitation_codes(list(envelope.get("blocked") or []))
    limitations = "\n".join(
        f"- {PUBLIC_LIMITATION_MESSAGES[code]}" for code in blocked
    ) if blocked else "- None recorded."

    title = (
        "# Partially verified experiment-design result"
        if blocked else "# Verified experiment-design result"
    )
    return (
        f"{title}\n\n"
        "## Question and estimand\n"
        f"Tool: `{tool}`\n\n"
        "## Resolved assumptions\n"
        "```json\n"
        f"{json.dumps(canonical_value(assumptions), sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)}\n"
        "```\n\n"
        "## Result\n"
        "```json\n"
        f"{json.dumps(canonical_value(safe_result), sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)}\n"
        "```\n\n"
        "## Verification\n"
        f"- verification_id: `{analysis_id}`\n"
        f"- public_result_hash: `{public_result_hash}`\n"
        f"- status: `{_normal_status(envelope)}`\n"
        f"- checks: {check_text}\n\n"
        "## Limitations\n"
        f"{limitations}\n\n"
        "## Decision\n"
        "Use only the decision fields, if any, in the presentable Result block above; "
        "no additional model-generated numeric interpretation is authorized."
    )


def canonical_report(
    tool: str,
    arguments: dict[str, Any] | None,
    result: dict[str, Any],
    envelope: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> str:
    """Render the only final report authorized for one verified result."""
    identity = envelope.get("identity") if isinstance(envelope, dict) else None
    if not isinstance(identity, dict):
        raise ValueError("verification identity is required for canonical reporting")
    if not identity_matches_call(identity, tool, arguments, result, provenance):
        raise ValueError("verification identity does not match the report inputs")
    return _render_report(
        tool,
        public_arguments_view(tool, arguments),
        privacy_safe_view(tool, result),
        envelope,
    )


def canonical_public_report(
    tool: str,
    arguments: dict[str, Any] | None,
    public_result: dict[str, Any],
    envelope: dict[str, Any],
    server_envelope: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> str:
    """Render a host-gated report from a server-bound public result DTO.

    The server binds the raw result to its public DTO and original report. The
    host then attaches a *fresh* combined gate (including its own regression
    attestation) and renders that verdict without needing raw private data.
    """
    identity = envelope.get("identity") if isinstance(envelope, dict) else None
    server_identity = (
        server_envelope.get("identity") if isinstance(server_envelope, dict) else None
    )
    if not isinstance(identity, dict) or identity != server_identity:
        raise ValueError("fresh verification identity does not match the server envelope")
    if not public_envelope_matches_call(
        server_envelope, tool, arguments, public_result, provenance or {},
    ):
        raise ValueError("server public-result/report binding failed")
    return _render_report(
        tool,
        public_arguments_view(tool, arguments),
        privacy_safe_view(tool, public_result),
        envelope,
    )


def join_reports(reports: Iterable[str]) -> str:
    return "\n\n---\n\n".join(report.strip() for report in reports)


def normalize_report_text(value: str) -> str:
    return "\n".join(line.rstrip() for line in value.strip().splitlines())


def is_elicitation_message(value: str) -> bool:
    """Accept only a fixed structured clarification request.

    Free-form question classification is not a trust boundary: a numeric claim,
    participant identifier, or prior result can all be phrased as a question.
    """
    candidate = value.strip()
    if not candidate.startswith(_CLARIFICATION_PREFIX):
        return False
    try:
        payload = json.loads(candidate[len(_CLARIFICATION_PREFIX):])
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or set(payload) != {"fields"}:
        return False
    fields = payload.get("fields")
    return bool(
        isinstance(fields, list)
        and fields
        and len(fields) <= 8
        and all(isinstance(field, str) and field in _CLARIFICATION_FIELDS for field in fields)
        and len(set(fields)) == len(fields)
    )
