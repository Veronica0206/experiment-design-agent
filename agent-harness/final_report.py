"""Deterministic, privacy-aware rendering of verified analysis results.

The model may explain an elicitation question freely, but it must never be the
authority that transcribes a completed numeric analysis.  Both the Python
harness and the Claude stop hook use this renderer so the final answer is an
exact function of the verified arguments, result, and identity.
"""

from __future__ import annotations

import json
import math
from itertools import combinations
from pathlib import Path
from typing import Any, Iterable

from verification import (PUBLIC_LIMITATION_MESSAGES, canonical_value,
                          content_hash,
                          domain_arguments, identity_matches_call,
                          normalize_platform_analysis_methods,
                          platform_interim_reason_code,
                          public_check_summary, public_envelope_matches_call,
                          public_limitation_codes)


# Design rows mix factor coordinates with a small, fixed metadata vocabulary.
# Keep this contract in the public-projection module and import it from the
# scientific gates so discovery, shape validation, and reporting cannot drift.
# Matching is case-insensitive at the private boundary; public output always
# uses these canonical lowercase names.
DESIGN_METADATA_COLUMNS = frozenset({"point_type", "run", "std_order"})

SINGLE_ENDPOINT_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "governance" / "single-endpoint-result-contract.json"
)
SINGLE_ENDPOINT_RESULT_CONTRACT = json.loads(
    SINGLE_ENDPOINT_CONTRACT_PATH.read_text(encoding="utf-8")
)
if (
    type(SINGLE_ENDPOINT_RESULT_CONTRACT.get("schema_version")) is not int
    or SINGLE_ENDPOINT_RESULT_CONTRACT["schema_version"] != 1
    or SINGLE_ENDPOINT_RESULT_CONTRACT.get("result_contract_version") != 1
    or set(SINGLE_ENDPOINT_RESULT_CONTRACT.get("result_types", {}))
    != {"sample_size_row", "oc_row", "ppos_result"}
):
    raise ValueError("unsupported single-endpoint result contract")


def normalized_design_metadata_column(value: Any) -> str | None:
    """Return a canonical design-metadata name, or ``None`` for a factor."""
    if not isinstance(value, str):
        return None
    normalized = value.casefold()
    return normalized if normalized in DESIGN_METADATA_COLUMNS else None


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
for _field, _values in SINGLE_ENDPOINT_RESULT_CONTRACT["enums"].items():
    _ENUM_VALUES.setdefault(_field, set()).update(_values)

_BOOLEAN_FIELDS = {
    "interim_efficacy_enabled", "interim_futility_enabled",
    "interim_stopping_applied", "orthogonal", "power_precision_met",
    "precision_met", "random", "randomize", "rar_enabled",
    "reject_precision_met", "shared_control", "truly_active",
    "mc_precision_ok", "inner_precision_ok",
}

_PRIOR_NAMES = {"jeffreys", "flat", "skeptical"}
_ENUM_VALUES["prior"] = set(_PRIOR_NAMES)
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
    "rate_method", "prior", "prior_method", "prior_params",
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
_SAMPLE_ROW.update(SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"]["sample_size_row"]["required"])
_SAMPLE_ROW.update(SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"]["sample_size_row"]["optional"])
_OC_ROW.update(SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"]["oc_row"]["required"])
_PPOS_FIELDS.update(SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"]["ppos_result"]["required"])
_PPOS_FIELDS.update(SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"]["ppos_result"]["optional"])
_MASTER_ROW = {
    "subgroup", "arm", "scenario", "estimate", "se", "lower", "upper", "power",
    "reject_rate", "fwer", "type1_error", "n", "n_total", "decision", "stage",
    "null_param", "alt_param", "truly_active", "mean_estimate", "true_effect",
    "effect_scale", "per_arm_power", "cond_estimate", "selection_prob", "metric",
    "value", "expected_n", "enter_period", "leave_period", "true_alt_param",
    "true_null_param", "mean_n", "mean_early_posterior_prob",
    "mean_final_p_value", "type_i_error", "requested_ncc_method",
    "actual_analysis_method", "actual_analysis_methods", "reject_mcse_pct",
    "reject_ci_lower_pct", "reject_ci_upper_pct", "reject_precision_met",
    "power_mcse_pct", "power_ci_lower_pct", "power_ci_upper_pct",
    "power_precision_met", "fwer_mcse_pct", "fwer_ci_lower_pct",
    "fwer_ci_upper_pct", "mcse", "ci_lower", "ci_upper", "precision_met",
    "n_simulations",
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
    "validate_config": {
        (): set(_SINGLE_CONFIG), ("prior_params",): set(_CUSTOM_PRIOR_FIELDS),
    },
    "sample_size": {
        (): {"results", "result_contract_version"}, ("results",): set(_SAMPLE_ROW),
    },
    "simulate_design": {
        (): {"sample_size", "oc", "ppos", "seed", "b_used", "n_oc_used",
             "result_contract_version", "workload"},
        ("sample_size",): set(_SAMPLE_ROW),
        ("oc",): set(_OC_ROW),
        ("ppos",): set(_PPOS_FIELDS),
        ("n_oc_used",): {"n", "n_trt", "n_ctrl"},
        ("workload",): {
            "scenario_count", "replicates", "simulated_units",
            "posterior_evaluation_upper_bound", "max_simulated_units",
            "max_posterior_evaluations",
        },
    },
    "master_simulate": {
        (): {"result"},
        ("result",): {
            "oc_table", "fwer_table", "power_table", "sample_size_table",
            "arm_results", "type_i_table", "alloc_df", "decision_matrix",
            "multiplicity_method", "final_alpha_per_arm",
            "interim_futility_enabled", "interim_efficacy_enabled",
            "interim_stopping_applied", "interim_stopping_reason",
            "requested_ncc_method", "actual_analysis_methods",
            "mc_precision_target_probability_half_width", "boundary_source",
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
             "randomize", "seed", "n_runs", "resolution", "orthogonal", "design",
             "type", "generators", "defining_relation", "alias_structure"},
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
    "nogo_threshold", "futility_threshold", "sd", "accrual_time",
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
             "randomize", "seed", "generators"},
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
    ("simulate_design", "result", ("ppos", "hr_post_ci")),
    ("simulate_design", "result", ("ppos", "rr_post_ci")),
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


def _contract_value_valid(field: str, value: Any, kind: str) -> bool:
    if kind == "enum":
        return (
            isinstance(value, str)
            and value in SINGLE_ENDPOINT_RESULT_CONTRACT["enums"].get(field, [])
        )
    if kind == "boolean":
        return isinstance(value, bool)
    if kind.startswith("nullable_"):
        return value is None or _contract_value_valid(field, value, kind[9:])
    if kind == "nonnegative_pair":
        return (
            isinstance(value, list) and len(value) == 2
            and all(_contract_value_valid(field, item, "nonnegative") for item in value)
            and value[0] <= value[1]
        )
    number = _number(value)
    if number is _DROP:
        return False
    if kind == "number":
        return True
    if kind == "probability":
        return 0 <= number <= 1
    if kind == "open_probability":
        return 0 < number < 1
    if kind == "nonnegative":
        return number >= 0
    if kind == "positive":
        return number > 0
    if kind in {"positive_integer", "nonnegative_integer"}:
        return (
            number >= (1 if kind == "positive_integer" else 0)
            and number == math.floor(number)
        )
    return False


def single_endpoint_contract_errors(
    tool: str, result: Any, arguments: dict[str, Any] | None = None,
) -> list[str]:
    """Validate complete scientific DTOs without interpreting private text.

    Unlike privacy_safe_view(), this is a presentation admission check, not a
    generic partial-object projection. Its fixed errors contain no result values,
    labels or unapproved field names. Unrelated tool contracts are unaffected.
    """
    if tool not in {"sample_size", "simulate_design"}:
        return []
    errors: set[str] = set()
    if (
        not isinstance(result, dict)
        or type(result.get("result_contract_version")) is not int
        or result["result_contract_version"]
        != SINGLE_ENDPOINT_RESULT_CONTRACT["result_contract_version"]
    ):
        return ["single_endpoint_contract:version"]

    def record(value: Any, name: str) -> bool:
        definition = SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"][name]
        if not isinstance(value, dict):
            errors.add(f"single_endpoint_contract:{name}_shape")
            return False
        valid = True
        for field, kind in definition["required"].items():
            if field not in value or not _contract_value_valid(field, value[field], kind):
                errors.add(f"single_endpoint_contract:{name}_required")
                valid = False
        for field, kind in definition["optional"].items():
            if field in value and not _contract_value_valid(field, value[field], kind):
                errors.add(f"single_endpoint_contract:{name}_optional")
                valid = False
        return valid

    sample_key = "results" if tool == "sample_size" else "sample_size"
    samples = result.get(sample_key)
    if not isinstance(samples, list) or not 1 <= len(samples) <= 200:
        errors.add("single_endpoint_contract:sample_size_rows")
    else:
        for row in samples:
            if not record(row, "sample_size_row"):
                continue
            if row["sizing_status"] == "search_limit_reached":
                if any(row.get(field) is not None for field in (
                    "n_total", "n_trt", "n_ctrl", "power_achieved",
                )):
                    errors.add("single_endpoint_contract:unavailable_sample_size")
                continue
            if any(not _contract_value_valid(field, row[field], kind) for field, kind in (
                ("n_total", "positive_integer"), ("n_trt", "positive_integer"),
                ("power_achieved", "probability"),
            )):
                errors.add("single_endpoint_contract:available_sample_size")
            if row["design"] == "controlled" and not _contract_value_valid(
                "n_ctrl", row.get("n_ctrl"), "positive_integer",
            ):
                errors.add("single_endpoint_contract:controlled_sample_size")

    if tool == "simulate_design":
        budgets = [result[key] for key in ("B_used", "b_used") if key in result]
        budget = budgets[0] if len(budgets) == 1 else None
        if not _contract_value_valid("b_used", budget, "positive_integer"):
            errors.add("single_endpoint_contract:oc_budget")
        rows = result.get("oc")
        if not isinstance(rows, list) or not 1 <= len(rows) <= 200:
            errors.add("single_endpoint_contract:oc_rows")
        else:
            for row in rows:
                if not record(row, "oc_row"):
                    continue
                if row["mc_replicates"] != budget:
                    errors.add("single_endpoint_contract:oc_budget")
                for probability in ("p_go", "p_consider", "p_nogo"):
                    if not (
                        row[f"{probability}_mc_lower"] - 1e-6
                        <= row[probability]
                        <= row[f"{probability}_mc_upper"] + 1e-6
                    ):
                        errors.add("single_endpoint_contract:oc_interval")
        ppos = result.get("ppos")
        if ppos is not None:
            if record(ppos, "ppos_result"):
                if ppos["n_mc"] < 2 or not (
                    ppos["ppos_mc_lower"] - 1e-6
                    <= ppos["ppos"] <= ppos["ppos_mc_upper"] + 1e-6
                ):
                    errors.add("single_endpoint_contract:ppos_interval")
                draws = ppos.get("cond_power_draws")
                if "cond_power_draws" in ppos and (
                    not isinstance(draws, list) or len(draws) != ppos["n_mc"]
                    or any(not _contract_value_valid("ppos", draw, "probability") for draw in draws)
                ):
                    errors.add("single_endpoint_contract:ppos_draws")
                for ratio in ("hr", "rr"):
                    mean = f"{ratio}_post_mean"
                    status = f"{mean}_status"
                    fields = {mean, status, f"{ratio}_post_median",
                              f"{ratio}_post_ci", "ratio_summary_method"}
                    if fields.intersection(ppos.keys()) - {"ratio_summary_method"}:
                        if not fields <= ppos.keys() or (
                            ppos.get(status) == "does_not_exist"
                            and ppos.get(mean) is not None
                        ) or (
                            ppos.get(status) == "finite"
                            and ppos.get(mean) is None
                        ):
                            errors.add("single_endpoint_contract:ratio_summary")
        config = (arguments or {}).get("config") or {}
        if (
            isinstance(config, dict) and config.get("study_type") == "confirmatory"
            and config.get("p2_data") is not None and config.get("p3_n") is not None
            and ppos is None
        ):
            errors.add("single_endpoint_contract:missing_ppos")
        workload = result.get("workload")
        if workload is not None:
            fields = _RESULT_FIELDS_BY_PATH["simulate_design"][("workload",)]
            if (
                not isinstance(workload, dict) or not fields <= workload.keys()
                or any(not _contract_value_valid(
                    field, workload[field], "nonnegative_integer",
                ) for field in fields)
                or not isinstance(rows, list)
                or workload.get("scenario_count") != len(rows)
                or workload.get("replicates") != budget
            ):
                errors.add("single_endpoint_contract:workload")
    if not errors:
        projected = privacy_safe_view(tool, result)
        pairs = [
            (raw, public, "sample_size_row")
            for raw, public in zip(samples, projected.get(sample_key, []))
        ]
        if len(projected.get(sample_key, [])) != len(samples):
            errors.add("single_endpoint_contract:projection_loss")
        if tool == "simulate_design":
            public_rows = projected.get("oc", [])
            if len(public_rows) != len(result["oc"]):
                errors.add("single_endpoint_contract:projection_loss")
            pairs.extend(
                (raw, public, "oc_row")
                for raw, public in zip(result["oc"], public_rows)
            )
            if result.get("ppos") is not None:
                pairs.append((result["ppos"], projected.get("ppos", {}), "ppos_result"))
        for raw, public, name in pairs:
            definition = SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"][name]
            scientific_fields = set(definition["required"]) | (
                set(definition["optional"]) & raw.keys()
            )
            if any(
                field not in public or canonical_value(public[field]) != canonical_value(raw[field])
                for field in scientific_fields
            ):
                errors.add("single_endpoint_contract:projection_loss")
    return sorted(errors)


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


def factor_name_sort_key(name: str) -> tuple[int, int, str]:
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


def _positive_factor_index(value: Any, maximum: int) -> int | None:
    if (isinstance(value, int) and not isinstance(value, bool)
            and 1 <= value <= maximum):
        return value
    return None


def _project_factorial_generators(value: dict[str, Any]) -> Any:
    """Normalize a custom generator request to one-based public factor indices."""
    n_factors = value.get("n_factors")
    fraction = value.get("fraction", 0)
    raw = value.get("generators")
    if raw is None:
        return _DROP
    if (not isinstance(n_factors, int) or isinstance(n_factors, bool)
            or not isinstance(fraction, int) or isinstance(fraction, bool)
            or not 1 <= fraction < n_factors or not isinstance(raw, list)
            or len(raw) != fraction):
        return _DROP
    basic_count = n_factors - fraction
    normalized: list[dict[str, Any]] = []
    seen_sources: set[tuple[int, ...]] = set()
    for offset, item in enumerate(raw):
        if isinstance(item, dict):
            expected_keys = {"generated_factor_index", "source_factor_indices"}
            if set(item) != expected_keys:
                return _DROP
            generated = _positive_factor_index(
                item.get("generated_factor_index"), n_factors,
            )
            sources_raw = item.get("source_factor_indices")
            if generated != basic_count + offset + 1:
                return _DROP
        else:
            generated = basic_count + offset + 1
            sources_raw = item
        if not isinstance(sources_raw, list) or len(sources_raw) < 2:
            return _DROP
        sources = [_positive_factor_index(source, basic_count)
                   for source in sources_raw]
        if any(source is None for source in sources):
            return _DROP
        source_key = tuple(sorted(int(source) for source in sources))
        if len(set(source_key)) != len(source_key) or source_key in seen_sources:
            return _DROP
        seen_sources.add(source_key)
        normalized.append({
            "generated_factor_index": generated,
            "source_factor_indices": list(source_key),
        })
    return normalized


def _factor_names_from_design(value: dict[str, Any]) -> list[str]:
    design = value.get("design")
    if not isinstance(design, list):
        return []
    rows = [row for row in design if isinstance(row, dict)]
    return sorted(
        {
            str(key) for row in rows for key, item in row.items()
            if normalized_design_metadata_column(key) is None
            and _number(item) is not _DROP
        },
        key=factor_name_sort_key,
    )


def _project_defining_relation(value: dict[str, Any]) -> Any:
    """Convert relation words to sorted lists of public factor indices."""
    factor_names = _factor_names_from_design(value)
    n_factors = value.get("n_factors")
    if (not isinstance(n_factors, int) or isinstance(n_factors, bool)
            or n_factors != len(factor_names) or not 1 <= n_factors <= 12):
        return _DROP
    raw = value.get("defining_relation")
    words = [raw] if isinstance(raw, str) else raw
    if not isinstance(words, list) or not words:
        return _DROP
    name_to_index = {name: index + 1 for index, name in enumerate(factor_names)}
    normalized: list[tuple[int, ...]] = []
    for word in words:
        if isinstance(word, str):
            if not word or word != word.strip():
                return _DROP
            indices = [name_to_index.get(letter) for letter in word]
            if any(index is None for index in indices):
                return _DROP
            candidate = tuple(sorted(int(index) for index in indices))
        elif isinstance(word, list):
            indices = [_positive_factor_index(index, n_factors) for index in word]
            if any(index is None for index in indices):
                return _DROP
            candidate = tuple(sorted(int(index) for index in indices))
        else:
            return _DROP
        if len(candidate) < 2 or len(set(candidate)) != len(candidate):
            return _DROP
        normalized.append(candidate)
    if len(set(normalized)) != len(normalized):
        return _DROP
    return [list(word) for word in sorted(normalized, key=lambda word: (len(word), word))]


def _factorial_result_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Build bounded, label-free generator, relation, and alias metadata."""
    if value.get("type") != "fractional_factorial":
        return {}
    relation = _project_defining_relation(value)
    if relation is _DROP:
        return {}
    n_factors = int(value["n_factors"])
    group_size = len(relation) + 1
    if group_size < 2 or group_size & (group_size - 1):
        return {}
    fraction = int(math.log2(group_size))
    if not 1 <= fraction < n_factors:
        return {}
    basic_count = n_factors - fraction
    word_sets = [frozenset(word) for word in relation]

    generators: list[dict[str, Any]] = []
    generated_factors = set(range(basic_count + 1, n_factors + 1))
    for generated in sorted(generated_factors):
        candidates = [
            word for word in word_sets
            if generated in word and not ((word - {generated}) & generated_factors)
        ]
        if len(candidates) != 1:
            generators = []
            break
        sources = sorted(candidates[0] - {generated})
        if not sources or any(source > basic_count for source in sources):
            generators = []
            break
        generators.append({
            "generated_factor_index": generated,
            "source_factor_indices": sources,
        })

    group = [frozenset(), *word_sets]
    visible_effects = [
        frozenset(effect)
        for order in (1, 2)
        for effect in combinations(range(1, n_factors + 1), order)
    ]
    alias_classes: set[tuple[tuple[int, ...], ...]] = set()
    for effect in visible_effects:
        visible_class = {
            tuple(sorted(effect.symmetric_difference(word)))
            for word in group
            if 1 <= len(effect.symmetric_difference(word)) <= 2
        }
        if visible_class:
            alias_classes.add(tuple(sorted(
                visible_class, key=lambda member: (len(member), member),
            )))
    ordered_classes = sorted(
        alias_classes,
        key=lambda members: tuple((len(member), member) for member in members),
    )
    metadata: dict[str, Any] = {
        "defining_relation": relation,
        "alias_structure": {
            "factor_index_basis": "one_based_public_design_columns",
            "scope": "main_and_two_factor",
            "classes": [
                {"effects": [list(effect) for effect in members]}
                for members in ordered_classes
            ],
        },
    }
    if len(generators) == fraction:
        metadata["generators"] = generators
    return metadata


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
            if normalized_design_metadata_column(key) is None
            and _number(item) is not _DROP
        },
        key=factor_name_sort_key,
    )
    aliases = {name: f"factor_{index + 1}" for index, name in enumerate(factor_names)}
    public_rows: list[dict[str, Any]] = []
    for row in rows:
        public_row: dict[str, Any] = {}
        for key, item in sorted(row.items(), key=lambda pair: str(pair[0])):
            name = str(key)
            metadata_name = normalized_design_metadata_column(key)
            if name in aliases:
                public = _number(item)
                if public is not _DROP:
                    public_row[aliases[name]] = public
                continue
            if metadata_name in {"run", "std_order"}:
                public = _number(item)
                if public is not _DROP:
                    public_row[metadata_name] = public
            elif metadata_name == "point_type" and isinstance(item, str):
                normalized = item.casefold()
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
    if (tool == "factorial_design" and path == ()
            and lowered in {"generators", "defining_relation", "alias_structure"}):
        # These fields are rebuilt together from the verified matrix/relation or
        # the validated request. Never pass through engine free text.
        return _DROP
    if (tool == "master_simulate" and mode == "result"
            and lowered in {"actual_analysis_method", "actual_analysis_methods"}):
        normalized_methods = normalize_platform_analysis_methods(value)
        return normalized_methods if normalized_methods is not None else _DROP
    if (tool == "master_simulate" and mode == "result"
            and lowered == "requested_ncc_method"):
        if not isinstance(value, str):
            return _DROP
        normalized_ncc = value.lower()
        return (normalized_ncc if normalized_ncc in _ENUM_VALUES["ncc_method"]
                else _DROP)
    if (tool == "master_simulate" and mode == "result"
            and lowered == "interim_stopping_reason"):
        reason = platform_interim_reason_code(value)
        return reason if reason is not None else _DROP
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
            output_key = (
                "actual_analysis_methods"
                if (tool == "master_simulate" and mode == "result"
                    and path == ("result", "arm_results")
                    and lowered in {"actual_analysis_method", "actual_analysis_methods"})
                else lowered
            )
            safe[output_key] = public
    if private_count:
        safe["private_artifacts"] = {"available": True, "length": private_count}
    return safe


def privacy_safe_view(tool: str, value: Any) -> Any:
    """Return the allowlisted, deterministic model-visible result DTO."""
    if tool == "randomize":
        return _project_randomize(value, "result")
    if not isinstance(value, dict):
        return {}
    safe = _project_mapping(tool, value, (), _RESULT_FIELDS_BY_PATH, "result")
    if tool == "factorial_design":
        safe.update(_factorial_result_metadata(value))
    return safe


def public_arguments_view(tool: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Return the allowlisted public DTO for resolved tool arguments."""
    value = domain_arguments(arguments)
    if tool == "randomize":
        return _project_randomize(value, "arguments")
    safe = _project_mapping(tool, value, (), _ARG_FIELDS_BY_PATH, "arguments")
    if tool == "factorial_design":
        generators = _project_factorial_generators(value)
        if generators is not _DROP:
            safe["generators"] = generators
    return safe


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


def _display_number(value: Any) -> str:
    if value is None:
        return "Unavailable"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return format(value, ".8g")


def _report_table(headers: list[str], rows: list[list[str]]) -> str:
    return "\n".join([
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(row) + " |" for row in rows),
    ])


def _select_oc_anchor(rows: list[dict[str, Any]], anchor: float) -> dict[str, Any] | None:
    """Prefer a unique exact anchor, otherwise a unique nearest row in tolerance.

    OC rows have already passed the scientific result contract. An exact
    duplicate or an equal-distance tie is ambiguous regardless of row order.
    """
    exact = [row for row in rows if row["true_param"] == anchor]
    if exact:
        return exact[0] if len(exact) == 1 else None
    nearby = [row for row in rows if math.isclose(
        row["true_param"], anchor, rel_tol=1e-12, abs_tol=1e-12,
    )]
    if not nearby:
        return None
    distance = min(abs(row["true_param"] - anchor) for row in nearby)
    nearest = [row for row in nearby if abs(row["true_param"] - anchor) == distance]
    return nearest[0] if len(nearest) == 1 else None


def _single_endpoint_readable_result(
    tool: str, assumptions: dict[str, Any], result: dict[str, Any],
    envelope: dict[str, Any],
) -> str:
    """Render only approved identifiers and verified numbers, never engine prose."""
    if tool not in {"sample_size", "simulate_design"} or "result_contract_version" not in result:
        return ""
    if single_endpoint_contract_errors(tool, result, assumptions):
        raise ValueError("single-endpoint readable report contract failed")
    samples = result["results" if tool == "sample_size" else "sample_size"]
    unavailable_sizes = any(row["sizing_status"] == "search_limit_reached" for row in samples)
    sections = [
        "## Statistical summary",
        "Display values are rounded; the structured result below retains their full precision.",
        _report_table(
            ["Design", "Sizing test", "Alpha", "Target power", "Achieved power",
             "Treatment n", "Control n", "Total n", "Sizing status"],
            [
                [row["design"], row["test"],
                 *(_display_number(row.get(key)) for key in (
                     "alpha", "power_target", "power_achieved", "n_trt", "n_ctrl", "n_total",
                 )), row["sizing_status"]]
                for row in samples
            ],
        ),
    ]
    if unavailable_sizes:
        sections.append(
            "Search-limit rows have no available sample size or achieved power. "
            "Their target power is a request, not an achieved result; no design is "
            "validated for those rows."
        )
    assessment = "Not assessed by OC criteria for this deterministic sizing result."
    if tool == "simulate_design":
        rows = result["oc"]
        sections.extend([
            "### Operating characteristics",
            "Intervals below are 95% Monte Carlo intervals for the simulated decision "
            "frequencies. They do not establish that the study assumptions are appropriate.",
            _report_table(
                ["True parameter", "GO [95% MC interval]", "CONSIDER [95% MC interval]",
                 "NO-GO [95% MC interval]", "Replicates"],
                [
                    [_display_number(row["true_param"]),
                     *(
                         f'{_display_number(row[name])} '
                         f'[{_display_number(row[name + "_mc_lower"])}, '
                         f'{_display_number(row[name + "_mc_upper"])}]'
                         for name in ("p_go", "p_consider", "p_nogo")
                     ), _display_number(row["mc_replicates"])]
                    for row in rows
                ],
            ),
            "### Monte Carlo precision",
            _report_table(
                ["True parameter", "GO MC SE", "CONSIDER MC SE", "NO-GO MC SE",
                 "Worst-case SE", "Target SE", "Outer precision met"],
                [
                    [_display_number(row[key]) for key in (
                        "true_param", "p_go_mcse", "p_consider_mcse", "p_nogo_mcse",
                        "mc_worst_case_se", "mc_precision_target_se", "mc_precision_ok",
                    )]
                    for row in rows
                ],
            ),
            "Outer Monte Carlo error and inner posterior-probability integration error "
            "are separate. Inner maxima summarize the reported computation.",
            _report_table(
                ["True parameter", "Inner probability method", "Maximum absolute error",
                 "Tolerance", "Maximum evaluations", "Evaluation limit", "Inner precision met"],
                [
                    [_display_number(row["true_param"]), row["inner_probability_method"],
                     *(_display_number(row[key]) for key in (
                         "inner_probability_abs_error_max", "inner_probability_tolerance",
                         "inner_probability_evaluations_max", "inner_probability_max_evaluations",
                         "inner_precision_ok",
                     ))]
                    for row in rows
                ],
            ),
        ])
        config = assumptions.get("config") or {}
        null = config.get("null_param")
        alternative = config.get("alt_param")
        row_null = row_alt = None
        if (_number(null) is not _DROP and _number(alternative) is not _DROP
                and null != alternative):
            row_null = _select_oc_anchor(rows, null)
            row_alt = _select_oc_anchor(rows, alternative)
        if (row_null is not None and row_alt is not None
                and row_null["true_param"] != row_alt["true_param"]):
            p_null, p_alt = row_null["p_go"], row_alt["p_go"]
            direction_met = p_alt > p_null
            separation_met = p_alt >= 3 * p_null if p_null > 0 else p_alt > 0
            assessment = (
                ("Meets" if direction_met and separation_met else "Does not meet")
                + " the selected OC heuristics; this is a design-performance assessment, "
                "not a calculation-validity judgment."
            )
            sections.extend([
                "### Design-performance assessment",
                _report_table(
                    ["Selected heuristic", "Null GO", "Alternative GO", "Met"],
                    [
                        ["Alternative GO exceeds null GO", _display_number(p_null),
                         _display_number(p_alt), _display_number(direction_met)],
                        ["Alternative GO is at least 3 times null GO (positive if null GO is zero)",
                         _display_number(p_null), _display_number(p_alt),
                         _display_number(separation_met)],
                    ],
                ),
                assessment + " These heuristics are not universal scientific acceptance "
                "criteria. An unfavorable result is retained for design review.",
            ])
        else:
            assessment = (
                "Not assessed: distinct, unambiguous null and alternative OC anchors "
                "were unavailable."
            )
        ppos = result.get("ppos")
        if ppos is not None:
            sections.extend([
                "### Predictive probability of success",
                f'Confirmatory test: {ppos["confirmatory_test"]}. '
                f'Prior method: {ppos["prior_method"]}.',
                _report_table(
                    ["PPOS", "95% MC interval", "Posterior draws", "MC SE", "Worst-case SE",
                     "Target SE", "Precision met"],
                    [[_display_number(ppos["ppos"]),
                      f'[{_display_number(ppos["ppos_mc_lower"])}, '
                      f'{_display_number(ppos["ppos_mc_upper"])}]',
                      *(_display_number(ppos[key]) for key in (
                          "n_mc", "ppos_mcse", "mc_worst_case_se",
                          "mc_precision_target_se", "mc_precision_ok",
                      ))]],
                ),
                "This interval describes Monte Carlo estimation error in PPOS; "
                "it is not a predictive interval for a future observed effect.",
            ])
            for ratio, label in (("hr", "hazard ratio"), ("rr", "rate ratio")):
                status = ppos.get(f"{ratio}_post_mean_status")
                if status is not None:
                    mean = (
                        "does not exist" if status == "does_not_exist"
                        else _display_number(ppos[f"{ratio}_post_mean"])
                    )
                    interval = ppos[f"{ratio}_post_ci"]
                    sections.append(
                        f"Posterior {label}: mean {mean}; median "
                        f'{_display_number(ppos[f"{ratio}_post_median"])}; 95% credible interval '
                        f"[{_display_number(interval[0])}, {_display_number(interval[1])}]. "
                        f'Summary method: {ppos["ratio_summary_method"]}.'
                    )
        workload = result.get("workload")
        if isinstance(workload, dict) and workload:
            sections.extend([
                "### Admitted computation",
                "These are operation-count bounds used for admission, not runtime predictions.",
                _report_table(
                    ["Work quantity", "Count"],
                    [[key, _display_number(value)] for key, value in sorted(workload.items())],
                ),
            ])
    checks = public_check_summary(envelope.get("checks") or {})
    sections.extend([
        "## Assurance dimensions",
        _report_table(
            ["Dimension", "Status"],
            [
                ["Calculation and result contract",
                 "Sizing search completed with unavailable rows; status and metadata preserved"
                 if unavailable_sizes else "Completed; required scientific metadata preserved"],
                ["Input and result binding", "Verified by the canonical reporting boundary"],
                ["Regression suite", "Passed" if checks.get("regression_suite") else "Not recorded"],
                ["Same-seed replay", "Passed" if checks.get("reproducibility") else "Not recorded"],
                ["Statistical assumptions", "Human review required; software checks do not establish suitability"],
                ["Design performance", assessment],
            ],
        ),
    ])
    return "\n\n".join(sections) + "\n\n"


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
    assumptions_heading = (
        "## Supplied assumptions\n"
        "These are the supplied, privacy-safe arguments. Omitted engine defaults "
        "are not inferred here; any PPOS prior method below comes from the resolved "
        "engine result.\n"
        if "result_contract_version" in safe_result else "## Resolved assumptions\n"
    )
    return (
        f"{title}\n\n"
        "## Question and estimand\n"
        f"Tool: `{tool}`\n\n"
        f"{assumptions_heading}"
        "```json\n"
        f"{json.dumps(canonical_value(assumptions), sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False)}\n"
        "```\n\n"
        f"{_single_endpoint_readable_result(tool, assumptions, safe_result, envelope)}"
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
    if "result_contract_version" in result and single_endpoint_contract_errors(
        tool, result, arguments,
    ):
        raise ValueError("single-endpoint result contract failed")
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
    if "result_contract_version" in public_result and single_endpoint_contract_errors(
        tool, public_result, arguments,
    ):
        raise ValueError("single-endpoint public result contract failed")
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
