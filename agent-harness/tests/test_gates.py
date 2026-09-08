#!/usr/bin/env python3
"""Unit tests for gates.py — the verification safety net for every runtime
(harness, SubagentStop hook, Streamlit form). Plain script, no pytest needed.

Run: tools/run-reviewed-python.sh agent-harness/tests/test_gates.py
"""
from __future__ import annotations

import os
import random
import signal
import sys
import tempfile
import time
from itertools import product
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

import gates as gates_module  # noqa: E402
from artifact_download import MAX_ARTIFACT_BYTES  # noqa: E402
from gates import (  # noqa: E402
    check_ab_test,
    check_bucher,
    check_config_completeness,
    check_factorial,
    check_master_config_reserved,
    check_master_result,
    check_meta,
    check_output_contract,
    check_randomize,
    check_regression_tests,
    check_reproducibility,
    check_rsm,
    check_sample_size,
    check_single_endpoint,
    combined_gate,
    design_checks_for,
)

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {detail}")
        failed += 1


# ── check_config_completeness ──────────────────────────────────────
good_bin = {"endpoint_type": "binary", "null_param": 0.2, "alt_param": 0.4}
v = check_config_completeness(good_bin)
check("cc_binary_good", v.passed and not v.blocked, str(v))

v = check_config_completeness({"endpoint_type": "binary",
                               "null_param": 0.4, "alt_param": 0.2})
check("cc_binary_wrong_direction_fails", not v.passed, str(v))

# incidence_rate: BOTH directions valid; harm direction noted, not failed.
v = check_config_completeness({"endpoint_type": "incidence_rate",
                               "null_param": 0.5, "alt_param": 0.3,
                               "exposure_time": 1})
check("cc_rate_protective_passes", v.passed and not v.notes, str(v))
v = check_config_completeness({"endpoint_type": "incidence_rate",
                               "null_param": 0.5, "alt_param": 0.8,
                               "exposure_time": 1})
check("cc_rate_harm_passes_with_note", v.passed and v.notes, str(v))
v = check_config_completeness({"endpoint_type": "incidence_rate",
                               "null_param": 0.5, "alt_param": 0.5,
                               "exposure_time": 1})
check("cc_rate_equal_fails", not v.passed, str(v))
v = check_config_completeness({"endpoint_type": "incidence_rate",
                               "null_param": 0.5, "alt_param": 0.3})
check("cc_rate_missing_exposure_fails", not v.passed, str(v))

# Reserved methods rejected regardless of endpoint.
v = check_config_completeness(dict(good_bin, rate_method="negbin"))
check("cc_reserved_negbin_fails", not v.passed, str(v))
v = check_config_completeness(dict(good_bin, tte_method="cox_ph"))
check("cc_reserved_coxph_fails", not v.passed, str(v))
v = check_config_completeness(dict(good_bin, tte_method="exponential"))
check("cc_default_method_ok", v.passed, str(v))

v = check_config_completeness({"endpoint_type": "weird"})
check("cc_unknown_endpoint_blocked", v.passed and v.blocked, str(v))

# ── check_master_config_reserved ───────────────────────────────────
v = check_master_config_reserved({"master_design_type": "basket"})
check("mr_clean_passes", v.passed, str(v))
for bad in ({"fwer_control": "holm"}, {"power_type": "complete"},
            {"rar_eta": 0.5}, {"shared_control": False},
            {"overdispersion": 2}, {"tte_method": "cox_ph"},
            {"effect_threshold": 0.99}):
    v = check_master_config_reserved(bad)
    check(f"mr_rejects_{list(bad)[0]}", not v.passed, str(v))
v = check_master_config_reserved({"master_design_type": "umbrella",
                                  "umbrella_method": "drop_the_losers",
                                  "selection_rule": "rank_best"})
check("mr_derived_selection_ok", v.passed, str(v))
v = check_master_config_reserved({"master_design_type": "umbrella",
                                  "umbrella_method": "mams",
                                  "selection_rule": "rank_best"})
check("mr_nonderived_selection_fails", not v.passed, str(v))
v = check_master_config_reserved({
    "master_design_type": "platform", "endpoint_type": "binary",
    "ncc_method": "regression", "futility_threshold": 0.1,
})
check("mr_inert_platform_interim_setting_fails", not v.passed, str(v))
v = check_master_config_reserved({
    "master_design_type": "platform", "endpoint_type": "binary",
    "ncc_method": "none", "futility_threshold": 0.1,
})
check("mr_supported_platform_futility_setting_passes", v.passed, str(v))

# ── check_sample_size ──────────────────────────────────────────────
ok_row = {"design": "single_arm", "n_total": 40,
          "power_target": 0.8, "power_achieved": 0.82}
v = check_sample_size({"results": [ok_row]}, {"design": "single_arm"})
check("ss_good_passes", v.passed and not v.blocked, str(v))

na_row = {"design": "single_arm", "n_total": None,
          "power_target": 0.9, "power_achieved": None}
v = check_sample_size({"results": [na_row]}, {"design": "single_arm"})
check("ss_all_na_partial_not_fail", v.passed and v.blocked, str(v))
v = check_sample_size({"results": [ok_row, na_row]}, {"design": "single_arm"})
check("ss_partial_na_surfaced", v.passed and v.blocked, str(v))

short_row = dict(ok_row, power_achieved=0.5)
v = check_sample_size({"results": [short_row]}, {"design": "single_arm"})
check("ss_power_short_fails", not v.passed, str(v))

v = check_sample_size({"results": [dict(ok_row, design="controlled")]},
                      {"design": "single_arm"})
check("ss_no_requested_rows_fails", not v.passed, str(v))

v = check_sample_size({"error": "boom"}, {"design": "single_arm"})
check("ss_error_fails", not v.passed, str(v))

# ── check_single_endpoint ──────────────────────────────────────────
sim_ok = {"sample_size": [ok_row],
          "oc": [{"true_param": 0.2, "p_go": 0.05},
                 {"true_param": 0.4, "p_go": 0.85}]}
v = check_single_endpoint(sim_ok, {"design": "single_arm",
                                   "null_param": 0.2, "alt_param": 0.4})
check("se_good_passes", v.passed, str(v))

sim_bad = {"sample_size": [ok_row],
           "oc": [{"true_param": 0.2, "p_go": 0.9},
                  {"true_param": 0.4, "p_go": 0.1}]}
v = check_single_endpoint(sim_bad, {"design": "single_arm",
                                    "null_param": 0.2, "alt_param": 0.4})
check("se_inverted_oc_fails", not v.passed, str(v))

sim_partial = {"sample_size": [ok_row, na_row], "oc": sim_ok["oc"]}
v = check_single_endpoint(sim_partial, {"design": "single_arm",
                                        "null_param": 0.2, "alt_param": 0.4})
check("se_partial_na_surfaced", v.passed and v.blocked, str(v))
v = check_single_endpoint(
    {"sample_size": [dict(ok_row, power_target=0.9, power_achieved=0.6)],
     "oc": sim_ok["oc"]},
    {"design": "single_arm", "null_param": 0.2, "alt_param": 0.4},
)
check("se_power_must_meet_requested_target", not v.passed, str(v))
v = check_single_endpoint(
    {"sample_size": [ok_row],
     "oc": [{"true_param": -0.1, "p_go": 0.05},
            {"true_param": 0.7, "p_go": 0.85}]},
    {"design": "single_arm", "null_param": 0.2, "alt_param": 0.4},
)
check("se_distant_oc_rows_fail", not v.passed, str(v))

sim_with_mc = {
    "sample_size": [ok_row],
    "B_used": 1000,
    "oc": [
        {"true_param": 0.2, "p_go": 0.05, "p_go_mcse": 0.0069,
         "p_go_mc_lower": 0.038, "p_go_mc_upper": 0.065,
         "mc_replicates": 1000, "mc_worst_case_se": 0.0158,
         "mc_precision_ok": True},
        {"true_param": 0.4, "p_go": 0.85, "p_go_mcse": 0.0113,
         "p_go_mc_lower": 0.827, "p_go_mc_upper": 0.871,
         "mc_replicates": 1000, "mc_worst_case_se": 0.0158,
         "mc_precision_ok": True},
    ],
}
v = check_single_endpoint(sim_with_mc, {"design": "single_arm",
                                        "null_param": 0.2, "alt_param": 0.4})
check("se_mc_uncertainty_passes", v.passed and not v.blocked, str(v))

low_precision = dict(sim_with_mc, B_used=100)
low_precision["oc"] = [dict(row, mc_replicates=100,
                             mc_worst_case_se=0.05, mc_precision_ok=False)
                       for row in sim_with_mc["oc"]]
v = check_single_endpoint(low_precision, {"design": "single_arm",
                                          "null_param": 0.2, "alt_param": 0.4})
check("se_low_mc_precision_is_partial", v.passed and v.blocked, str(v))

missing_mc = dict(sim_with_mc)
missing_mc["oc"] = sim_ok["oc"]
v = check_single_endpoint(missing_mc, {"design": "single_arm",
                                       "null_param": 0.2, "alt_param": 0.4})
check("se_missing_mc_uncertainty_fails", not v.passed, str(v))

# ── check_ab_test ──────────────────────────────────────────────────
ab_args = {
    "baseline": 0.1, "effect": 0.02, "metric": "proportion",
    "effect_type": "absolute", "alpha": 0.05, "power": 0.8,
    "sided": 2, "ratio": 1,
}
ab_ok = {
    "metric": "proportion", "n_control": 3839, "n_treatment": 3839,
    "n_total": 7678, "allocation_ratio": 1, "mde": 0.02,
    "baseline": 0.1, "alpha": 0.05, "sided": 2,
    "target_power": 0.8, "achieved_power": 0.8001,
}
v = check_ab_test(ab_ok, ab_args)
check("ab_good_passes", v.passed and not v.blocked, str(v))
v = check_ab_test(ab_ok, {"baseline": 0.1, "effect": 0.02})
check("ab_public_defaults_pass", v.passed, str(v))
v = check_ab_test({k: value for k, value in ab_ok.items()
                   if k not in ("target_power", "achieved_power")}, ab_args)
check("ab_missing_power_fails", not v.passed, str(v))
v = check_ab_test(dict(ab_ok, achieved_power=0.5), ab_args)
check("ab_power_shortfall_fails", not v.passed, str(v))

for field, bad_value in (
    ("baseline", 0.2), ("mde", 0.03), ("metric", "mean"),
    ("alpha", 0.1), ("target_power", 0.9),
    ("allocation_ratio", 2), ("sided", 1),
):
    v = check_ab_test(dict(ab_ok, **{field: bad_value}), ab_args)
    check(f"ab_mismatched_{field}_fails", not v.passed, str(v))

v = check_ab_test(ab_ok, dict(ab_args, effect=0.02, effect_type="relative"))
check("ab_effect_type_changes_effective_mde", not v.passed, str(v))
tiny_mde_args = dict(ab_args, effect=0.00004)
v = check_ab_test(dict(ab_ok, mde=0), tiny_mde_args)
check("ab_rounded_zero_mde_fails_closed", not v.passed, str(v))
v = check_ab_test(dict(ab_ok, n_total=7679), ab_args)
check("ab_arm_counts_must_sum_to_total", not v.passed, str(v))
v = check_ab_test(dict(ab_ok, n_treatment=3845, n_total=7684), ab_args)
check("ab_integer_allocation_must_match_ratio", not v.passed, str(v))
v = check_ab_test(ab_ok, dict(ab_args, baseline=0.9, effect=0.2,
                              effect_type="relative"))
check("ab_relative_treatment_rate_must_be_in_bounds", not v.passed, str(v))

mean_args = {
    "baseline": 10, "effect": 0.1, "metric": "mean",
    "effect_type": "relative", "sd": 2, "alpha": 0.1,
    "power": 0.9, "sided": 1, "ratio": 2,
}
mean_ok = {
    "metric": "mean", "n_control": 40, "n_treatment": 80,
    "n_total": 120, "allocation_ratio": 2, "mde": 1,
    "baseline": 10, "alpha": 0.1, "sided": 1,
    "target_power": 0.9, "achieved_power": 0.9033,
}
v = check_ab_test(mean_ok, mean_args)
check("ab_mean_sd_bound_by_reconstructed_size", v.passed, str(v))
v = check_ab_test(mean_ok, dict(mean_args, sd=4))
check("ab_mean_sd_mismatch_fails", not v.passed, str(v))

# ── check_reproducibility ──────────────────────────────────────────
a = {"x": 1.0, "y": [1, 2]}
v = check_reproducibility(a, {"x": 1.0, "y": [1, 2]})
check("repro_identical_passes", v.passed, str(v))
v = check_reproducibility(a, {"x": 1.1, "y": [1, 2]})
check("repro_differs_fails", not v.passed, str(v))
v = check_reproducibility(a, {"error": "server died"})
check("repro_error_reports_cause", not v.passed
      and any("error" in f for f in v.failures), str(v))
v = check_reproducibility({"decision": "go"}, {"decision": "no-go"})
check("repro_categorical_decision_differs", not v.passed, str(v))
v = check_reproducibility({}, {})
check("repro_empty_cannot_pass", not v.passed, str(v))
check("repro_verification_metadata_ignored", check_reproducibility(
    {"x": 1, "_verification": {"identity": {"call_id": "one"}},
     "_provenance": {"verification_id": "one"}},
    {"x": 1, "_verification": {"identity": {"call_id": "two"}},
     "_provenance": {"verification_id": "two"}},
).passed)
check("repro_large_adjacent_ints_differ", not check_reproducibility(
    {"x": 2 ** 53}, {"x": 2 ** 53 + 1}).passed)
check("repro_huge_ints_do_not_crash", not check_reproducibility(
    {"x": 10 ** 400}, {"x": 10 ** 400 + 1}).passed)

# ── output contract / regression / master presence ────────────────
check("oc_nan_fails", not check_output_contract({"v": float("nan")}).passed)
check("oc_error_fails", not check_output_contract({"error": "x"}).passed)
check("oc_none_is_na_ok", check_output_contract({"v": None}).passed)
rt_ok = {
    "all_ok": True, "suite_count": 5, "passed_suite_count": 5,
    "failed_suite_count": 0,
    "checks": {"declared_all_ok": True, "complete_suite_set": True,
               "suite_records_valid": True, "expected_check_count": True},
}
check("rt_all_ok", check_regression_tests(rt_ok).passed)
check("rt_truthy_string_rejected", not check_regression_tests(
    dict(rt_ok, all_ok="false")).passed)
check("rt_missing_fixed_check_rejected", not check_regression_tests(
    dict(rt_ok, checks={"declared_all_ok": True,
                        "suite_records_valid": True})).passed)
check("rt_fail", not check_regression_tests({
    "all_ok": False, "suite_count": 5, "passed_suite_count": 4,
    "failed_suite_count": 1,
    "checks": {"declared_all_ok": False, "complete_suite_set": True,
               "suite_records_valid": False, "expected_check_count": False},
}).passed)
check("rt_contradictory_counts_rejected", not check_regression_tests(
    dict(rt_ok, passed_suite_count=4, failed_suite_count=1)).passed)
check("rt_arbitrary_public_key_rejected", not check_regression_tests(
    dict(rt_ok, raw_output="private/path TEST secret : PASS")).passed)
check("master_missing_result_fails",
      not check_master_result({"something_else": 1}).passed)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / "basket_oc_table.csv").write_text(
        "subgroup,null_param,alt_param,reject_rate,reject_mcse_pct,"
        "reject_ci_lower_pct,reject_ci_upper_pct,reject_precision_met\n"
        "1,0.2,0.4,80,1.26,77.4,82.4,TRUE\n", encoding="utf-8")
    (root / "basket_fwer.csv").write_text(
        "scenario,n_true_null,fwer,fwer_mcse_pct,fwer_ci_lower_pct,"
        "fwer_ci_upper_pct,precision_met,n_simulations\n"
        "Global null,1,5,0.69,3.8,6.6,TRUE,1000\n", encoding="utf-8")
    (root / "basket_subgroup_decisions.csv").write_text(
        "subgroup,decision\n1,Go\n", encoding="utf-8")
    (root / "basket_oc_curves.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    master_result = {
        "result": {
            "oc_table": [{"subgroup": 1, "null_param": 0.2,
                          "alt_param": 0.4, "reject_rate": 80,
                          "reject_mcse_pct": 1.26,
                          "reject_ci_lower_pct": 77.4,
                          "reject_ci_upper_pct": 82.4,
                          "reject_precision_met": True}],
            "fwer_table": [{"scenario": "Global null", "n_true_null": 1,
                            "fwer": 5, "fwer_mcse_pct": 0.69,
                            "fwer_ci_lower_pct": 3.8,
                            "fwer_ci_upper_pct": 6.6,
                            "precision_met": True, "n_simulations": 1000}],
            "mc_precision_target_probability_half_width": 0.02,
        },
        "output_dir": str(root),
    }
    check("master_artifacts_consistent_pass",
          check_master_result(master_result, {"master_design_type": "basket",
                                              "n_sims": 1000}).passed)
    (root / "basket_oc_table.csv").write_text(
        "subgroup,null_param,alt_param,reject_rate,reject_mcse_pct,"
        "reject_ci_lower_pct,reject_ci_upper_pct,reject_precision_met\n"
        "1,0.2,0.4,NaN,1.26,77.4,82.4,TRUE\n", encoding="utf-8")
    check("master_artifact_nonfinite_rejected",
          not check_master_result(master_result, {"master_design_type": "basket"}).passed)
    (root / "basket_oc_table.csv").write_text(
        "subgroup,null_param,alt_param,reject_rate,reject_mcse_pct,"
        "reject_ci_lower_pct,reject_ci_upper_pct,reject_precision_met\n"
        "1,0.2,0.4,70,1.26,77.4,82.4,TRUE\n", encoding="utf-8")
    check("master_artifact_json_mismatch_rejected",
          not check_master_result(master_result, {"master_design_type": "basket"}).passed)
    check("master_unknown_family_rejected",
          not check_master_result(master_result, {"master_design_type": "unknown"}).passed)

    (root / "basket_oc_table.csv").write_text(
        "subgroup,null_param,alt_param,reject_rate,reject_mcse_pct,"
        "reject_ci_lower_pct,reject_ci_upper_pct,reject_precision_met\n"
        "1,0.2,0.4,80,1.26,77.4,82.4,FALSE\n", encoding="utf-8")
    low_precision_result = {
        **master_result,
        "result": {
            **master_result["result"],
            "oc_table": [dict(master_result["result"]["oc_table"][0],
                              reject_precision_met=False)],
        },
    }
    verdict = check_master_result(low_precision_result,
                                  {"master_design_type": "basket", "n_sims": 1000})
    check("master_low_mc_precision_is_partial", verdict.passed and verdict.blocked,
          str(verdict))

    (root / "basket_oc_table.csv").write_text(
        "subgroup,null_param,alt_param,reject_rate,reject_mcse_pct,"
        "reject_ci_lower_pct,reject_ci_upper_pct,reject_precision_met\n"
        "1,0.2,0.4,80,1.26,77.4,82.4,TRUE\n", encoding="utf-8")
    missing_mc_result = {
        **master_result,
        "result": {"oc_table": [{"subgroup": 1, "reject_rate": 80}],
                   "mc_precision_target_probability_half_width": 0.02},
    }
    check("master_missing_mc_uncertainty_fails",
          not check_master_result(missing_mc_result,
                                  {"master_design_type": "basket"}).passed)

    artifact = root / "basket_oc_table.csv"
    artifact.unlink()
    os.mkfifo(artifact)
    previous_handler = signal.getsignal(signal.SIGALRM)

    def master_fifo_timeout(_signum, _frame):
        raise TimeoutError("master artifact FIFO open blocked")

    signal.signal(signal.SIGALRM, master_fifo_timeout)
    started = time.monotonic()
    fifo_rejected = False
    fifo_timed_out = False
    try:
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        fifo_rejected = not check_master_result(
            master_result, {"master_design_type": "basket"},
        ).passed
    except TimeoutError:
        fifo_timed_out = True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    check(
        "master_artifact_fifo_is_rejected_without_blocking",
        fifo_rejected and not fifo_timed_out and time.monotonic() - started < 1.0,
    )

    artifact.unlink()
    with artifact.open("wb") as handle:
        handle.truncate(MAX_ARTIFACT_BYTES + 1)
    directory_fd = gates_module._open_master_output_directory(root)
    original_read = gates_module.os.read

    def unexpected_oversized_read(_fd, _count):
        unexpected_oversized_read.__dict__["attempted"] = True
        raise AssertionError("oversized artifact payload was read")

    gates_module.os.read = unexpected_oversized_read
    oversized_message = ""
    try:
        try:
            gates_module._read_master_artifact(directory_fd, artifact.name)
        except gates_module._MasterArtifactError as exc:
            oversized_message = str(exc)
    finally:
        gates_module.os.read = original_read
        os.close(directory_fd)
    check(
        "master_oversized_artifact_is_rejected_before_read",
        "size limit" in oversized_message
        and not bool(unexpected_oversized_read.__dict__.get("attempted")),
    )

    row_limited_payload = (
        b"subgroup,null_param,alt_param,reject_rate\n"
        + b"1,0.2,0.4,80\n" * (gates_module.MAX_MASTER_CSV_ROWS + 1)
    )
    csv_ok, finite_ok, consistency = gates_module._validate_master_csv(
        row_limited_payload,
        {"subgroup", "null_param", "alt_param", "reject_rate"},
        None,
    )
    check(
        "master_csv_row_limit_is_enforced",
        not csv_ok and not finite_ok and consistency is None,
    )

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / "umbrella_power_table.csv").write_text(
        "arm,per_arm_power,power_mcse_pct,power_ci_lower_pct,"
        "power_ci_upper_pct,power_precision_met\n"
        "1,80,1.26,77.4,82.4,TRUE\n", encoding="utf-8")
    (root / "umbrella_arm_comparison.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    umbrella_result = {
        "result": {
            "oc_table": [{"arm": 1, "per_arm_power": 80,
                          "power_mcse_pct": 1.26,
                          "power_ci_lower_pct": 77.4,
                          "power_ci_upper_pct": 82.4,
                          "power_precision_met": True}],
            "mc_precision_target_probability_half_width": 0.02,
            "boundary_source": "approximation",
            "boundary_approximation": True,
            "boundary_fallback_reason": "MAMS package unavailable",
            "decision_rule": "no interim efficacy; final Bonferroni",
            "boundaries": {
                "effect": [None, 2.24],
                "efficacy_enabled_by_stage": [False, True],
            },
        },
        "output_dir": str(root),
    }
    verdict = check_master_result(
        umbrella_result,
        {"master_design_type": "umbrella", "umbrella_method": "mams"},
    )
    check("master_mams_approximation_contract_passes", verdict.passed, str(verdict))
    bad_umbrella = {
        **umbrella_result,
        "result": {
            **umbrella_result["result"],
            "boundaries": {"effect": [2.0, 2.24],
                           "efficacy_enabled_by_stage": [False, True]},
        },
    }
    check("master_mams_unapplied_reported_boundary_fails",
          not check_master_result(
              bad_umbrella,
              {"master_design_type": "umbrella", "umbrella_method": "mams"},
          ).passed)

    # A one-stage fallback has only its final efficacy boundary, which jsonlite
    # encodes as a scalar. That final stage must remain enabled.
    single_stage = {
        **umbrella_result,
        "result": {
            **umbrella_result["result"],
            "boundaries": {
                "effect": 2.24, "efficacy_enabled_by_stage": True,
            },
        },
    }
    verdict = check_master_result(
        single_stage,
        {"master_design_type": "umbrella", "umbrella_method": "mams",
         "n_stages": 1},
    )
    check("master_mams_single_stage_unboxed_boundary_passes",
          verdict.passed
          and verdict.checks.get("mams_boundary_contract") is True, str(verdict))
    # The same one-stage payload must not satisfy a two-stage design.
    check("master_mams_single_stage_boundary_rejects_two_stage_config",
          not check_master_result(
              single_stage,
              {"master_design_type": "umbrella", "umbrella_method": "mams"},
          ).passed)

    single_stage_disabled = {
        **single_stage,
        "result": {
            **single_stage["result"],
            "boundaries": {
                "effect": None, "efficacy_enabled_by_stage": False,
            },
        },
    }
    check("master_mams_single_stage_disabled_final_boundary_fails_closed",
          not check_master_result(
              single_stage_disabled,
              {"master_design_type": "umbrella", "umbrella_method": "mams",
               "n_stages": 1},
          ).passed)

    single_stage_unapplied = {
        **umbrella_result,
        "result": {
            **umbrella_result["result"],
            "boundaries": {"effect": 2.0, "efficacy_enabled_by_stage": False},
        },
    }
    check("master_mams_single_stage_unapplied_boundary_fails",
          not check_master_result(
              single_stage_unapplied,
              {"master_design_type": "umbrella", "umbrella_method": "mams",
               "n_stages": 1},
          ).passed)

    missing_boundary_vector = {
        **umbrella_result,
        "result": {
            **umbrella_result["result"],
            "boundaries": {"efficacy_enabled_by_stage": [False, True]},
        },
    }
    check("master_mams_missing_boundary_vector_fails_closed",
          not check_master_result(
              missing_boundary_vector,
              {"master_design_type": "umbrella", "umbrella_method": "mams"},
          ).passed)

    # An absent or null boundary_source must not skip the contract entirely.
    for label, source in (("null", None), ("absent", "__omit__")):
        undeclared_source = dict(umbrella_result["result"])
        if source == "__omit__":
            undeclared_source.pop("boundary_source", None)
        else:
            undeclared_source["boundary_source"] = source
        check(f"master_mams_{label}_boundary_source_fails_closed",
              not check_master_result(
                  {**umbrella_result, "result": undeclared_source},
                  {"master_design_type": "umbrella", "umbrella_method": "mams"},
              ).passed)

    # The default umbrella method is MAMS, so an omitted method still requires
    # the boundary contract.
    check("master_default_umbrella_method_requires_boundary_contract",
          not check_master_result(
              {**umbrella_result,
               "result": {key: value
                          for key, value in umbrella_result["result"].items()
                          if key != "boundary_source"}},
              {"master_design_type": "umbrella"},
          ).passed)

    # Two reported boundaries cannot satisfy a three-stage design.
    check("master_mams_boundary_count_must_match_stages",
          not check_master_result(
              umbrella_result,
              {"master_design_type": "umbrella", "umbrella_method": "mams",
               "n_stages": 3},
          ).passed)
    verdict = check_master_result(
        umbrella_result,
        {"master_design_type": "umbrella", "umbrella_method": "mams", "n_stages": 2},
    )
    check("master_mams_explicit_matching_stage_count_passes",
          verdict.passed, str(verdict))

    # The engine pairs MAMS_package with approximation FALSE and both
    # approximation and user_supplied with TRUE; no other combination exists.
    for source, approximated in (
        ("approximation", False),
        ("user_supplied", False),
        ("MAMS_package", True),
    ):
        contradictory = {
            **umbrella_result,
            "result": {
                **umbrella_result["result"],
                "boundary_source": source,
                "boundary_approximation": approximated,
            },
        }
        check(f"master_mams_{source}_with_approximation_{approximated}_fails_closed",
              not check_master_result(
                  contradictory,
                  {"master_design_type": "umbrella", "umbrella_method": "mams"},
              ).passed)
    user_supplied = {
        **umbrella_result,
        "result": {**umbrella_result["result"], "boundary_source": "user_supplied"},
    }
    user_supplied_config = {
        "master_design_type": "umbrella", "umbrella_method": "mams",
        "futility_boundaries": [-1.0, 2.24],
    }
    verdict = check_master_result(
        user_supplied,
        user_supplied_config,
    )
    check("master_mams_user_supplied_approximation_passes", verdict.passed, str(verdict))
    check("master_mams_user_supplied_without_requested_boundaries_fails_closed",
          not check_master_result(
              user_supplied,
              {"master_design_type": "umbrella", "umbrella_method": "mams"},
          ).passed)

    package_calibrated = {
        **umbrella_result,
        "result": {
            **umbrella_result["result"],
            "boundary_source": "MAMS_package",
            "boundary_approximation": False,
            "boundary_fallback_reason": None,
            "boundaries": {
                "effect": [2.0, 2.24],
                "efficacy_enabled_by_stage": [True, True],
            },
        },
    }
    verdict = check_master_result(
        package_calibrated,
        {"master_design_type": "umbrella", "umbrella_method": "mams"},
    )
    check("master_mams_package_calibrated_pair_passes", verdict.passed, str(verdict))

    check("master_mams_package_with_requested_user_boundaries_fails_closed",
          not check_master_result(
              package_calibrated,
              {"master_design_type": "umbrella", "umbrella_method": "mams",
               "futility_boundaries": [-1.0, 2.24]},
          ).passed)

    package_with_fallback = {
        **package_calibrated,
        "result": {
            **package_calibrated["result"],
            "boundary_fallback_reason": "must be absent for package calibration",
        },
    }
    check("master_mams_package_fallback_reason_fails_closed",
          not check_master_result(
              package_with_fallback,
              {"master_design_type": "umbrella", "umbrella_method": "mams"},
          ).passed)

    wrong_fallback_flags = {
        **umbrella_result,
        "result": {
            **umbrella_result["result"],
            "boundaries": {
                "effect": [2.0, 2.24],
                "efficacy_enabled_by_stage": [True, True],
            },
        },
    }
    check("master_mams_approximation_wrong_stage_flags_fail_closed",
          not check_master_result(
              wrong_fallback_flags,
              {"master_design_type": "umbrella", "umbrella_method": "mams"},
          ).passed)

    check("master_mams_approximation_with_user_boundaries_fails_closed",
          not check_master_result(
              umbrella_result,
              {"master_design_type": "umbrella", "umbrella_method": "mams",
               "futility_boundaries": [-1.0, 2.24]},
          ).passed)

    blank_fallback = {
        **umbrella_result,
        "result": {
            **umbrella_result["result"],
            "boundary_fallback_reason": "   ",
        },
    }
    check("master_mams_approximation_blank_fallback_fails_closed",
          not check_master_result(
              blank_fallback,
              {"master_design_type": "umbrella", "umbrella_method": "mams"},
          ).passed)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    (root / "platform_arm_results.csv").write_text(
        "arm,reject_rate,mean_n,requested_ncc_method,actual_analysis_method,"
        "reject_mcse_pct,reject_ci_lower_pct,reject_ci_upper_pct,"
        "reject_precision_met\n"
        "1,5,100,regression,exact_stratified_cmh,0.69,3.8,6.6,TRUE\n",
        encoding="utf-8")
    (root / "platform_oc_table.csv").write_text(
        "metric,value,mcse,ci_lower,ci_upper,precision_met,n_simulations\n"
        "FWER (%),5,0.69,3.8,6.6,TRUE,1000\n", encoding="utf-8")
    (root / "platform_timeline.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (root / "platform_oc_curves.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    platform_result = {
        "result": {
            "arm_results": [{
                "arm": 1, "reject_rate": 5, "mean_n": 100,
                "requested_ncc_method": "regression",
                "actual_analysis_method": "exact_stratified_cmh",
                "reject_mcse_pct": 0.69, "reject_ci_lower_pct": 3.8,
                "reject_ci_upper_pct": 6.6, "reject_precision_met": True,
            }],
            "oc_table": [{"metric": "FWER (%)", "value": 5, "mcse": 0.69,
                          "ci_lower": 3.8, "ci_upper": 6.6,
                          "precision_met": True, "n_simulations": 1000}],
            "interim_stopping_applied": False,
            "interim_futility_enabled": False,
            "interim_efficacy_enabled": False,
            "interim_stopping_reason": "No NCC-consistent interim model",
            "requested_ncc_method": "regression",
            "actual_analysis_methods": ["exact_stratified_cmh"],
            "mc_precision_target_probability_half_width": 0.02,
        },
        "output_dir": str(root),
    }
    verdict = check_master_result(
        platform_result,
        {"master_design_type": "platform", "ncc_method": "regression"},
    )
    check("master_platform_actual_behavior_contract_passes", verdict.passed,
          str(verdict))
    bad_platform = {
        **platform_result,
        "result": {**platform_result["result"],
                   "interim_efficacy_enabled": True},
    }
    check("master_platform_inert_efficacy_claim_fails",
          not check_master_result(
              bad_platform,
              {"master_design_type": "platform", "ncc_method": "regression"},
          ).passed)

    # A run that uses one analysis method reports a length-1 vector, which
    # jsonlite encodes as a bare string.
    single_method = {
        **platform_result,
        "result": {**platform_result["result"],
                   "actual_analysis_methods": "exact_stratified_cmh"},
    }
    verdict = check_master_result(
        single_method,
        {"master_design_type": "platform", "ncc_method": "regression"},
    )
    check("master_platform_single_unboxed_analysis_method_passes",
          verdict.passed
          and verdict.checks.get("platform_actual_behavior_declared") is True,
          str(verdict))

    for label, methods in (
        ("empty_string", ""),
        ("nonstring", 3),
        ("whitespace", "   "),
        ("whitespace_member", ["exact_stratified_cmh", "   "]),
        ("duplicate_member", ["exact_stratified_cmh",
                              "exact_stratified_cmh"]),
        # An unhashable element must fail the contract, not raise before the
        # verdict is returned.
        ("unhashable_member", [{}]),
        ("nested_list_member", [["exact_concurrent_stratified"]]),
    ):
        undeclared_method = {
            **platform_result,
            "result": {**platform_result["result"],
                       "actual_analysis_methods": methods},
        }
        verdict = check_master_result(
            undeclared_method,
            {"master_design_type": "platform", "ncc_method": "regression"},
        )
        check(f"master_platform_{label}_analysis_method_fails_closed",
              not verdict.passed
              and verdict.checks.get("platform_actual_behavior_declared") is False,
              str(verdict))

    # An arm that used several methods across periods reports them ";"-joined,
    # and the declared list is their union over every arm.
    joined_label = "exact_stratified_cmh;concurrent_fisher_exact"
    with tempfile.TemporaryDirectory() as joined_directory:
        joined_root = Path(joined_directory)
        (joined_root / "platform_arm_results.csv").write_text(
            "arm,reject_rate,mean_n,requested_ncc_method,actual_analysis_method,"
            "reject_mcse_pct,reject_ci_lower_pct,reject_ci_upper_pct,"
            "reject_precision_met\n"
            f"1,5,100,regression,{joined_label},0.69,3.8,6.6,TRUE\n",
            encoding="utf-8")
        (joined_root / "platform_oc_table.csv").write_text(
            "metric,value,mcse,ci_lower,ci_upper,precision_met,n_simulations\n"
            "FWER (%),5,0.69,3.8,6.6,TRUE,1000\n", encoding="utf-8")
        (joined_root / "platform_timeline.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        (joined_root / "platform_oc_curves.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
        multi_method = {
            "output_dir": str(joined_root),
            "result": {
                **platform_result["result"],
                "actual_analysis_methods": [
                    "exact_stratified_cmh", "concurrent_fisher_exact",
                ],
                "arm_results": [
                    {**platform_result["result"]["arm_results"][0],
                     "actual_analysis_method": joined_label},
                ],
            },
        }
        verdict = check_master_result(
            multi_method,
            {"master_design_type": "platform", "ncc_method": "regression"},
        )
        check("master_platform_joined_arm_methods_reconcile", verdict.passed, str(verdict))

    for label, joined in (
        ("blank_token", "exact_stratified_cmh;"),
        ("padded_token", "exact_stratified_cmh; concurrent_fisher_exact"),
    ):
        malformed_join = {
            **platform_result,
            "result": {
                **multi_method["result"],
                "arm_results": [
                    {**platform_result["result"]["arm_results"][0],
                     "actual_analysis_method": joined},
                ],
            },
        }
        verdict = check_master_result(
            malformed_join,
            {"master_design_type": "platform", "ncc_method": "regression"},
        )
        check(f"master_platform_{label}_arm_method_fails_closed",
              not verdict.passed
              and verdict.checks.get("platform_actual_behavior_declared") is False,
              str(verdict))

    duplicate_arm_token = {
        **platform_result,
        "result": {
            **platform_result["result"],
            "arm_results": [
                {**platform_result["result"]["arm_results"][0],
                 "actual_analysis_method":
                     "exact_stratified_cmh;exact_stratified_cmh"},
            ],
        },
    }
    verdict = check_master_result(
        duplicate_arm_token,
        {"master_design_type": "platform", "ncc_method": "regression"},
    )
    check("master_platform_duplicate_arm_method_token_fails_closed",
          not verdict.passed
          and verdict.checks.get("platform_actual_behavior_declared") is False,
          str(verdict))

    # A declared method that no arm ran must fail even though every arm method
    # is itself declared.
    overdeclared = {
        **platform_result,
        "result": {
            **platform_result["result"],
            "actual_analysis_methods": [
                "exact_stratified_cmh", "never_ran_method",
            ],
        },
    }
    check("master_platform_overdeclared_method_fails_closed",
          not check_master_result(
              overdeclared,
              {"master_design_type": "platform", "ncc_method": "regression"},
          ).passed)

    # A declared set that does not cover what the arms actually ran, and an arm
    # row with no method at all, must both fail.
    unreconciled = {
        **platform_result,
        "result": {**platform_result["result"],
                   "actual_analysis_methods": ["some_other_method"]},
    }
    check("master_platform_unreconciled_arm_method_fails_closed",
          not check_master_result(
              unreconciled,
              {"master_design_type": "platform", "ncc_method": "regression"},
          ).passed)
    unlabelled_arm = {
        **platform_result,
        "result": {
            **platform_result["result"],
            "arm_results": [
                {key: value
                 for key, value in platform_result["result"]["arm_results"][0].items()
                 if key != "actual_analysis_method"}
            ],
        },
    }
    check("master_platform_unlabelled_arm_method_fails_closed",
          not check_master_result(
              unlabelled_arm,
              {"master_design_type": "platform", "ncc_method": "regression"},
          ).passed)

# ── combined_gate semantics ────────────────────────────────────────
from gates import GateVerdict  # noqa: E402
g = combined_gate(GateVerdict(passed=True), GateVerdict(passed=False, failures=["x"]))
check("combined_any_fail_fails", not g.passed)
g = combined_gate(GateVerdict(passed=True), GateVerdict(passed=True, blocked=["b"]))
check("combined_blocked_is_partial", g.passed and g.status == "PASS_PARTIAL")

# ── check_bucher ───────────────────────────────────────────────────
import math  # noqa: E402

bucher_args = {"method": "bucher", "comparisons": [
    {"estimate_ab": -0.5, "se_ab": 0.2, "estimate_cb": -0.2, "se_cb": 0.15,
     "alpha": 0.05, "treatment_a": "A", "treatment_c": "C",
     "common_comparator": "B"}]}
_se = math.sqrt(0.2 ** 2 + 0.15 ** 2)
bucher_res = {"comparisons": [[{"estimate": -0.3, "se": round(_se, 4),
                                "lower": round(-0.3 - 1.959964 * _se, 4),
                                "upper": round(-0.3 + 1.959964 * _se, 4)}]]}
v = check_bucher(bucher_res, bucher_args)
check("bucher_good_passes", v.passed, str(v))

bad = {"comparisons": [[dict(bucher_res["comparisons"][0][0], estimate=-0.7)]]}
v = check_bucher(bad, bucher_args)
check("bucher_wrong_estimate_fails", not v.passed, str(v))

bad = {"comparisons": [[dict(bucher_res["comparisons"][0][0], se=0.1)]]}
v = check_bucher(bad, bucher_args)
check("bucher_se_too_small_fails", not v.passed, str(v))

v = check_bucher({"weight_summary": {"n": 10},
                  "ess": [{"arm": "A", "ess": 8}],
                  "balance": [{"covariate": "age", "weighted_mean": 50}]},
                 {"method": "maic"})
check("maic_aggregate_contract_passes", v.passed, str(v))
v = check_bucher({"weights": []}, {"method": "maic"})
check("maic_row_level_payload_fails", not v.passed, str(v))

# ── check_meta ─────────────────────────────────────────────────────
meta_ok = {"k": 3, "estimate": 0.5, "se": 0.1,
           "lower": round(0.5 - 1.959964 * 0.1, 4),
           "upper": round(0.5 + 1.959964 * 0.1, 4),
           "tau2": 0.01, "q": 2.5, "i2": 20.0,
           "effect_measure": "mean_difference",
           "study_effects": [0.4, 0.5, 0.7],
           "study_effect_measures": ["mean_difference"] * 3,
           "n_input": 3, "k_used": 3, "dropped_studies": 0}
v = check_meta(meta_ok, {"alpha": 0.05})
check("meta_good_passes", v.passed and not v.notes, str(v))

v = check_meta(dict(meta_ok, lower=0.6), {"alpha": 0.05})
check("meta_bad_ci_fails", not v.passed, str(v))

v = check_meta(dict(meta_ok, estimate=0.9,
                    lower=round(0.9 - 1.959964 * 0.1, 4),
                    upper=round(0.9 + 1.959964 * 0.1, 4)), {"alpha": 0.05})
check("meta_outside_study_range_fails", not v.passed, str(v))

v = check_meta(dict(meta_ok, i2=140.0), {})
check("meta_i2_out_of_bounds_fails", not v.passed, str(v))

v = check_meta(dict(meta_ok, dropped_studies=1, n_input=4), {})
check("meta_dropped_studies_noted", v.passed and v.notes, str(v))

meta_without_effects = {key: value for key, value in meta_ok.items()
                        if key != "study_effects"}
v = check_meta(meta_without_effects, {})
check("meta_missing_study_effects_is_explicitly_partial",
      v.passed and "study-effect range check not run because per-study effects were unavailable"
      in v.blocked, str(v))

hksj_meta = dict(
    meta_ok,
    lower=0.5 - 4.302653 * 0.1,
    upper=0.5 + 4.302653 * 0.1,
    inference_method="DL tau2 with modified HKSJ t inference",
)
v = check_meta(hksj_meta, {"alpha": 0.05})
check("meta_hksj_t_interval_is_not_mischecked_as_normal", v.passed, str(v))

v = check_meta(dict(meta_ok,
                    study_effect_measures=["risk_difference", "log_odds_ratio"]), {})
check("meta_mixed_result_scales_fail", not v.passed, str(v))

v = check_meta(meta_ok, {"studies": [
    {"measure": "risk_difference"}, {"measure": "log_odds_ratio"},
]})
check("meta_mixed_requested_scales_fail", not v.passed, str(v))

v = check_meta({k: value for k, value in meta_ok.items()
                if k != "effect_measure"}, {})
check("meta_missing_effect_measure_fails", not v.passed, str(v))

# ── check_randomize ────────────────────────────────────────────────
simple_args = {"n": 5}
rand_ok = {
    "method": "simple", "n": 5, "arms": ["arm1", "arm2"],
    "counts": {"arm1": 4, "arm2": 1}, "seed": 42,
    "block_size_used": {},
    "assignment": [
        {"unit": 1, "stratum": None, "arm": "arm1"},
        {"unit": 2, "stratum": None, "arm": "arm1"},
        {"unit": 3, "stratum": None, "arm": "arm2"},
        {"unit": 4, "stratum": None, "arm": "arm1"},
        {"unit": 5, "stratum": None, "arm": "arm1"},
    ],
}
v = check_randomize(rand_ok, simple_args)
check("randomize_public_defaults_pass", v.passed, str(v))
v = check_randomize(rand_ok, {"n": 5, "ratio": [2, 2]})
check("randomize_simple_scaled_equal_ratio_passes", v.passed, str(v))
v = check_randomize(rand_ok, {"n": 5, "ratio": [3, 1]})
check("randomize_simple_non_equal_ratio_fails_closed", not v.passed, str(v))
for label, ratio in (
    ("tiny_decimal", [1e-5, 1e-5]),
    ("subnormal_scale", [1e-300, 1e-300]),
):
    v = check_randomize(rand_ok, {"n": 5, "ratio": ratio})
    check(f"randomize_simple_{label}_equal_ratio_is_scale_invariant",
          v.passed, str(v))

block_args = {
    "n": 8, "arms": ["control", "treatment"], "method": "block",
    "block_size": 4, "ratio": [1, 1], "seed": 42,
}
block_ok = {
    "method": "block", "n": 8, "arms": ["control", "treatment"],
    "counts": {"control": 4, "treatment": 4}, "seed": 42,
    "block_size_used": 4,
    "assignment": [
        {"unit": 1, "stratum": None, "arm": "control"},
        {"unit": 2, "stratum": None, "arm": "treatment"},
        {"unit": 3, "stratum": None, "arm": "treatment"},
        {"unit": 4, "stratum": None, "arm": "control"},
        {"unit": 5, "stratum": None, "arm": "control"},
        {"unit": 6, "stratum": None, "arm": "treatment"},
        {"unit": 7, "stratum": None, "arm": "control"},
        {"unit": 8, "stratum": None, "arm": "treatment"},
    ],
}
v = check_randomize(block_ok, block_args)
check("randomize_block_good_passes", v.passed, str(v))
v = check_randomize(block_ok, dict(block_args, ratio=[1e-5, 1e-5]))
check("randomize_block_equal_ratio_is_scale_invariant", v.passed, str(v))

scaled_weighted_args = {
    "n": 6, "arms": ["control", "treatment"], "method": "block",
    "block_size": 3, "ratio": [1e-5, 2e-5], "seed": 42,
}
scaled_weighted_ok = {
    "method": "block", "n": 6, "arms": ["control", "treatment"],
    "counts": {"control": 2, "treatment": 4}, "seed": 42,
    "block_size_used": 3,
    "assignment": [
        {"unit": 1, "stratum": None, "arm": "treatment"},
        {"unit": 2, "stratum": None, "arm": "control"},
        {"unit": 3, "stratum": None, "arm": "treatment"},
        {"unit": 4, "stratum": None, "arm": "treatment"},
        {"unit": 5, "stratum": None, "arm": "treatment"},
        {"unit": 6, "stratum": None, "arm": "control"},
    ],
}
v = check_randomize(scaled_weighted_ok, scaled_weighted_args)
check("randomize_block_weighted_ratio_is_scale_invariant", v.passed, str(v))
v = check_randomize(
    scaled_weighted_ok,
    dict(scaled_weighted_args, ratio=[1e-300, 1e300]),
)
check("randomize_unbounded_relative_quota_fails_closed", not v.passed, str(v))

fallback_args = dict(block_args, block_size=3)
fallback_ok = dict(block_ok, block_size_used=2)
v = check_randomize(fallback_ok, fallback_args)
check("randomize_invalid_requested_block_falls_back_to_base", v.passed, str(v))

weighted_args = {
    "n": 5, "arms": 3, "method": "block", "block_size": 7,
    "ratio": [2, 1, 1], "seed": 9,
}
weighted_ok = {
    "method": "block", "n": 5, "arms": ["arm1", "arm2", "arm3"],
    "counts": {"arm1": 3, "arm2": 1, "arm3": 1}, "seed": 9,
    "block_size_used": 4,
    "assignment": [
        {"unit": 1, "stratum": None, "arm": "arm2"},
        {"unit": 2, "stratum": None, "arm": "arm1"},
        {"unit": 3, "stratum": None, "arm": "arm1"},
        {"unit": 4, "stratum": None, "arm": "arm3"},
        {"unit": 5, "stratum": None, "arm": "arm1"},
    ],
}
v = check_randomize(weighted_ok, weighted_args)
check("randomize_integer_arms_weighted_ratio_and_tail_pass", v.passed, str(v))
v = check_randomize(dict(weighted_ok, ratio=[1, 1, 1]), weighted_args)
check("randomize_echoed_ratio_mismatch_fails", not v.passed, str(v))

v = check_randomize(dict(rand_ok, assignment=rand_ok["assignment"][:4]), simple_args)
check("randomize_missing_unit_fails", not v.passed, str(v))
v = check_randomize(dict(rand_ok, counts={"arm1": 3, "arm2": 2}), simple_args)
check("randomize_counts_mismatch_fails", not v.passed, str(v))
v = check_randomize({key: value for key, value in rand_ok.items()
                     if key != "seed"}, simple_args)
check("randomize_no_seed_fails", not v.passed, str(v))
duplicate_units = dict(rand_ok, assignment=[
    {"unit": 1, "stratum": None, "arm": "arm1"},
    {"unit": 1, "stratum": None, "arm": "arm1"},
    {"unit": 3, "stratum": None, "arm": "arm2"},
    {"unit": 4, "stratum": None, "arm": "arm1"},
    {"unit": 5, "stratum": None, "arm": "arm1"},
])
v = check_randomize(duplicate_units, simple_args)
check("randomize_duplicate_unit_fails", not v.passed, str(v))

for field, bad_value in (
    ("n", 7), ("method", "simple"),
    ("arms", ["private-arm-a", "private-arm-b"]), ("seed", 7),
):
    v = check_randomize(dict(block_ok, **{field: bad_value}), block_args)
    check(f"randomize_mismatched_{field}_fails", not v.passed, str(v))

v = check_randomize(dict(block_ok, block_size_used=2), block_args)
check("randomize_mismatched_effective_block_fails", not v.passed, str(v))
v = check_randomize(block_ok, dict(block_args, ratio=[3, 1], block_size=4))
check("randomize_mismatched_block_ratio_fails", not v.passed, str(v))

private_a = "SITE-PRIVATE-ALPHA"
private_b = "SITE-PRIVATE-BETA"
strat_args = {
    "n": 8, "arms": ["A", "B"], "method": "stratified",
    "block_size": 4, "ratio": [1, 1], "seed": 7,
    "strata": [private_a] * 4 + [private_b] * 4,
}
strat_ok = {
    "method": "stratified", "n": 8, "arms": ["A", "B"],
    "counts": {"A": 4, "B": 4}, "seed": 7, "block_size_used": 4,
    "assignment": [
        {"unit": 1, "stratum": private_a, "arm": "A"},
        {"unit": 2, "stratum": private_a, "arm": "B"},
        {"unit": 3, "stratum": private_a, "arm": "A"},
        {"unit": 4, "stratum": private_a, "arm": "B"},
        {"unit": 5, "stratum": private_b, "arm": "B"},
        {"unit": 6, "stratum": private_b, "arm": "A"},
        {"unit": 7, "stratum": private_b, "arm": "A"},
        {"unit": 8, "stratum": private_b, "arm": "B"},
    ],
}
v = check_randomize(strat_ok, strat_args)
check("randomize_stratified_good_passes_without_publishing_strata",
      v.passed and private_a not in str(v) and private_b not in str(v), str(v))
bad_strata_rows = [dict(row) for row in strat_ok["assignment"]]
bad_strata_rows[0]["stratum"] = "SHOULD-NOT-LEAK"
v = check_randomize(dict(strat_ok, assignment=bad_strata_rows), strat_args)
check("randomize_stratified_unit_stratum_mismatch_fails_privately",
      not v.passed and private_a not in str(v) and private_b not in str(v)
      and "SHOULD-NOT-LEAK" not in str(v), str(v))
bad_strat_units = [dict(row) for row in strat_ok["assignment"]]
bad_strat_units[1]["unit"] = 1
v = check_randomize(dict(strat_ok, assignment=bad_strat_units), strat_args)
check("randomize_stratified_unit_coverage_fails", not v.passed, str(v))

# ── check_factorial ────────────────────────────────────────────────
fact_ok = {"type": "full_factorial", "n_factors": 2, "n_runs": 4,
           "levels": [2, 2], "replicates": 1,
           "design": [{"A": -1, "B": -1}, {"A": 1, "B": -1},
                      {"A": -1, "B": 1}, {"A": 1, "B": 1}]}
fact_ok_args = {"n_factors": 2}
v = check_factorial(fact_ok, fact_ok_args)
check("factorial_orthogonal_passes", v.passed, str(v))
v = check_factorial(dict(fact_ok, design=fact_ok["design"][:3], n_runs=3),
                    fact_ok_args)
check("factorial_broken_orthogonality_fails", not v.passed, str(v))
v = check_factorial(dict(fact_ok, n_runs=8), fact_ok_args)
check("factorial_run_count_mismatch_fails", not v.passed, str(v))
check("factorial_empty_fails", not check_factorial({}, fact_ok_args).passed)

for label, mismatched_args, check_name in (
    ("n_factors", {"n_factors": 3}, "factorial_n_factors_matches_request"),
    ("levels", {"n_factors": 2, "levels": [3, 3]},
     "full_factorial_levels_match_request"),
    ("replicates", {"n_factors": 2, "replicates": 2},
     "full_factorial_replicates_match_request"),
):
    verdict = check_factorial(fact_ok, mismatched_args)
    check(f"factorial_valid_output_mismatched_{label}_request_fails_closed",
          not verdict.passed and verdict.checks.get(check_name) is False,
          str(verdict))
for label, levels in (
    ("scalar", 2), ("length_one_list", [2]), ("list", [2, 2]),
):
    verdict = check_factorial(fact_ok, {"n_factors": 2, "levels": levels})
    check(f"factorial_effective_{label}_levels_match_output",
          verdict.passed
          and verdict.checks.get("full_factorial_levels_match_request") is True,
          str(verdict))


def full_factorial_fixture(levels, replicates=1, center_points=0):
    columns = [chr(ord("A") + index) for index in range(len(levels))]
    values = ([(-1, 1) for _ in levels] if all(value == 2 for value in levels)
              else [range(1, value + 1) for value in levels])
    rows = [dict(zip(columns, combination))
            for combination in product(*values)
            for _ in range(replicates)]
    center = ([0] * len(levels) if all(value == 2 for value in levels)
              else [(value + 1) / 2 for value in levels])
    rows.extend(dict(zip(columns, center)) for _ in range(center_points))
    return {
        "type": "full_factorial",
        "n_factors": len(levels),
        "n_runs": len(rows),
        "levels": list(levels),
        "replicates": replicates,
        "design": rows,
    }


def full_factorial_request(result, center_points=0):
    request = {"n_factors": result["n_factors"]}
    result_levels = result.get("levels")
    normalized_levels = (
        [result_levels] if isinstance(result_levels, (int, float))
        and not isinstance(result_levels, bool) else result_levels
    )
    if (isinstance(normalized_levels, list)
            and any(value != 2 for value in normalized_levels)):
        request["levels"] = result_levels
    if result.get("replicates", 1) != 1:
        request["replicates"] = result["replicates"]
    if center_points != 0:
        request["center_points"] = center_points
    return request


two_level_centers = full_factorial_fixture([2, 2, 2], center_points=3)
two_level_args = full_factorial_request(two_level_centers, center_points=3)
ordered_verdict = check_factorial(two_level_centers, two_level_args)
check("factorial_ordered_centers_pass", ordered_verdict.passed, str(ordered_verdict))
for shuffle_seed in (1, 7, 42, 99):
    shuffled = dict(two_level_centers)
    shuffled["design"] = list(two_level_centers["design"])
    random.Random(shuffle_seed).shuffle(shuffled["design"])
    verdict = check_factorial(shuffled, two_level_args)
    check(f"factorial_randomized_centers_seed_{shuffle_seed}_passes",
          verdict.passed and verdict.checks == ordered_verdict.checks, str(verdict))

# A 3x3 base grid already contains the midpoint once per replicate. The gate
# must preserve those two legitimate rows while removing only the three added
# centers, irrespective of randomized run order.
odd_level_replicated = full_factorial_fixture(
    [3, 3], replicates=2, center_points=3,
)
random.Random(17).shuffle(odd_level_replicated["design"])
v = check_factorial(
    odd_level_replicated,
    full_factorial_request(odd_level_replicated, center_points=3),
)
check("factorial_odd_levels_replicates_preserve_native_midpoints",
      v.passed and v.checks.get("full_factorial_grid_complete") is True, str(v))

# A mixed even/odd grid uses a half-level center that is absent from its base
# grid. Exercise that coordinate path with replicates as well.
mixed_level_replicated = full_factorial_fixture(
    [3, 4], replicates=2, center_points=2,
)
random.Random(23).shuffle(mixed_level_replicated["design"])
v = check_factorial(
    mixed_level_replicated,
    full_factorial_request(mixed_level_replicated, center_points=2),
)
check("factorial_mixed_levels_replicates_randomized_centers_pass",
      v.passed and v.checks.get("center_rows_match_request") is True, str(v))

insufficient_centers = full_factorial_fixture([2, 2, 2], center_points=3)
insufficient_centers["design"][8] = dict(insufficient_centers["design"][0])
v = check_factorial(
    insufficient_centers,
    full_factorial_request(insufficient_centers, center_points=3),
)
check("factorial_insufficient_center_rows_fail_closed",
      not v.passed and v.checks.get("center_rows_match_request") is False, str(v))

extra_midpoint = full_factorial_fixture([3, 3], replicates=2, center_points=3)
extra_midpoint["design"][0] = {"A": 2, "B": 2}
v = check_factorial(
    extra_midpoint,
    full_factorial_request(extra_midpoint, center_points=3),
)
check("factorial_extra_midpoint_missing_grid_row_fails_closed",
      not v.passed and v.checks.get("center_rows_match_request") is False, str(v))

v = check_factorial(two_level_centers, {"n_factors": 3, "center_points": 2.5})
check("factorial_fractional_center_count_fails_closed", not v.passed, str(v))
v = check_factorial(two_level_centers, {"n_factors": 3, "center_points": True})
check("factorial_boolean_center_count_fails_closed", not v.passed, str(v))
v = check_factorial(two_level_centers, {"n_factors": 3, "center_points": 2})
check("factorial_valid_output_mismatched_center_request_fails_closed",
      not v.passed and v.checks.get("center_rows_match_request") is False, str(v))
malformed_factor_value = full_factorial_fixture([2, 2], center_points=1)
malformed_factor_value["design"][0]["A"] = []
v = check_factorial(
    malformed_factor_value,
    full_factorial_request(malformed_factor_value, center_points=1),
)
check("factorial_malformed_factor_value_fails_closed", not v.passed, str(v))

metadata_factorial = full_factorial_fixture([2, 2], center_points=0)
for index, row in enumerate(metadata_factorial["design"], start=1):
    row["RUN"] = index
    row["Std_Order"] = index
v = check_factorial(
    metadata_factorial, full_factorial_request(metadata_factorial),
)
check("factorial_shared_metadata_columns_are_not_factors", v.passed
      and v.checks.get("factor_columns_match_n_factors") is True
      and v.checks.get("factor_matrix_numeric_and_rectangular") is True, str(v))

for single_levels in (2, 3):
    single_factor = full_factorial_fixture(
        [single_levels], center_points=2,
    )
    # Match jsonlite's scalar encoding for an R vector of length one.
    single_factor["levels"] = single_levels
    random.Random(42).shuffle(single_factor["design"])
    v = check_factorial(
        single_factor,
        full_factorial_request(single_factor, center_points=2),
    )
    check(f"factorial_single_factor_{single_levels}_levels_passes",
          v.passed and v.checks.get("full_factorial_grid_complete") is True, str(v))

multi_factor_scalar_levels = full_factorial_fixture([2, 2], center_points=0)
multi_factor_scalar_levels["levels"] = 2
v = check_factorial(
    multi_factor_scalar_levels,
    full_factorial_request(multi_factor_scalar_levels),
)
check("factorial_multifactor_scalar_levels_fail_closed", not v.passed, str(v))

for label, first_row in (
    ("boolean", {"A": True, "B": False}),
    ("string", {"A": "low", "B": "high"}),
    ("absent", {}),
):
    malformed_first_row = full_factorial_fixture([2, 2], center_points=0)
    malformed_first_row["design"][0] = first_row
    v = check_factorial(
        malformed_first_row, full_factorial_request(malformed_first_row),
    )
    check(f"factorial_{label}_first_row_columns_fail_closed",
          not v.passed
          and v.checks.get("factor_columns_match_n_factors") is False
          and v.checks.get("factor_matrix_numeric_and_rectangular") is False,
          str(v))

inconsistent_keys = full_factorial_fixture([2, 2], center_points=0)
inconsistent_keys["design"][1]["C"] = 1
v = check_factorial(inconsistent_keys, full_factorial_request(inconsistent_keys))
check("factorial_inconsistent_factor_keys_fail_closed",
      not v.passed
      and v.checks.get("factor_matrix_numeric_and_rectangular") is False, str(v))

nonobject_row = full_factorial_fixture([2, 2], center_points=0)
nonobject_row["design"].append("not a row object")
# A forged n_runs matching only the four surviving dictionary rows must not
# allow the malformed fifth row to disappear through _design_rows filtering.
v = check_factorial(nonobject_row, full_factorial_request(nonobject_row))
check("factorial_nonobject_row_fails_closed",
      not v.passed and v.checks.get("design_rows_are_objects") is False, str(v))

for invalid_type in (None, "bogus"):
    invalid_typed = full_factorial_fixture([2, 2], center_points=0)
    invalid_typed["type"] = invalid_type
    v = check_factorial(invalid_typed, full_factorial_request(invalid_typed))
    check(f"factorial_type_{invalid_type!s}_fails_closed",
          not v.passed and v.checks.get("factorial_type_supported") is False,
          str(v))

wrong_n_factors = full_factorial_fixture([2, 2], center_points=0)
wrong_n_factors["n_factors"] = 3
v = check_factorial(wrong_n_factors, {"n_factors": 2})
check("factorial_n_factors_column_mismatch_fails_closed",
      not v.passed
      and v.checks.get("factor_columns_match_n_factors") is False, str(v))

frac = {"type": "fractional_factorial", "n_factors": 4, "n_runs": 8,
        "replicates": 1,
        "resolution": 4, "defining_relation": ["ABCD"],
        "design": [{"A": a, "B": b, "C": c, "D": a * b * c}
                   for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)]}
frac_args = {"n_factors": 4, "fraction": 1}
v = check_factorial(frac, frac_args)
check("fractional_resolution_matches", v.passed, str(v))
v = check_factorial(dict(frac, resolution=3), frac_args)
check("fractional_resolution_mismatch_fails", not v.passed, str(v))
v = check_factorial(frac, {"n_factors": 4})
check("fractional_missing_requested_fraction_fails_closed",
      not v.passed and v.checks.get("fractional_fraction_valid") is False, str(v))

# A half fraction has exactly one defining word, which jsonlite encodes as a
# scalar. Match that shape rather than the multi-word array.
half_fraction = dict(frac, defining_relation="ABCD")
v = check_factorial(half_fraction, frac_args)
check("fractional_unboxed_defining_relation_passes",
      v.passed and v.checks.get("resolution_matches_relation") is True, str(v))
v = check_factorial(dict(half_fraction, resolution=3), frac_args)
check("fractional_unboxed_resolution_mismatch_fails", not v.passed, str(v))
missing_relation = {key: value for key, value in frac.items()
                    if key != "defining_relation"}
v = check_factorial(missing_relation, frac_args)
check("fractional_missing_defining_relation_fails_closed", not v.passed, str(v))

# len(str(value)) would read each of these as a four-character defining word.
for label, forged in (("null", None), ("boolean", True), ("number", 1000)):
    v = check_factorial(dict(frac, defining_relation=forged), frac_args)
    check(f"fractional_{label}_defining_relation_fails_closed",
          not v.passed
          and v.checks.get("defining_relation_well_formed") is False, str(v))

for label, forged in (
    ("padded", " ABCD "),
    ("single_letter", "A"),
    ("repeated_letter", "AABC"),
    ("foreign_letter", "ABCZ"),
):
    v = check_factorial(dict(frac, defining_relation=[forged]), frac_args)
    check(f"fractional_{label}_defining_word_fails_closed",
          not v.passed
          and v.checks.get("defining_relation_well_formed") is False, str(v))

v = check_factorial(dict(frac, defining_relation=["ABCD", "ABCD"]), frac_args)
check("fractional_duplicate_defining_words_fail_closed",
      not v.passed and v.checks.get("defining_relation_well_formed") is False, str(v))

# A word is a claim about the built matrix, not a label. This design is
# generated as D=AB, so its true relation is I=ABD; claiming I=ABCD would report
# resolution IV while main effects are aliased with two-factor interactions.
forged_relation = dict(
    frac,
    design=[{"A": a, "B": b, "C": c, "D": a * b}
            for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)],
)
v = check_factorial(forged_relation, frac_args)
check("fractional_forged_defining_relation_fails_closed",
      not v.passed
      and v.checks.get("defining_relation_holds_in_design") is False, str(v))
v = check_factorial(dict(forged_relation, resolution=3, defining_relation=["ABD"]),
                    frac_args)
check("fractional_true_defining_relation_of_same_design_passes", v.passed, str(v))

# An internally valid regular half fraction is still the wrong answer when the
# caller explicitly requested D=AB. D=ABC with I=ABCD must fail request binding
# even though its matrix, relation, and resolution agree with one another.
custom_generator_args = {**frac_args, "generators": [[1, 2]]}
v = check_factorial(frac, custom_generator_args)
check("fractional_internally_valid_wrong_requested_generator_fails",
      not v.passed and v.checks.get("requested_generators_honored") is False,
      str(v))
requested_generator_design = dict(
    forged_relation, resolution=3, defining_relation=["ABD"],
)
v = check_factorial(requested_generator_design, custom_generator_args)
check("fractional_requested_generator_is_bound_to_generated_column",
      v.passed and v.checks.get("custom_generators_valid") is True
      and v.checks.get("requested_generators_honored") is True, str(v))
for label, invalid_generators in (
    ("wrong_count", []),
    ("duplicate_index", [[1, 1]]),
    ("generated_factor_reference", [[1, 4]]),
):
    v = check_factorial(
        requested_generator_design,
        {**frac_args, "generators": invalid_generators},
    )
    check(f"fractional_custom_generator_{label}_fails_closed",
          not v.passed and v.checks.get("custom_generators_valid") is False,
          str(v))

# A real 2^(5-2): basic A,B,C with D=AB and E=AC, so I=ABD=ACE=BCDE.
frac_two = {
    "type": "fractional_factorial", "n_factors": 5, "n_runs": 8,
    "replicates": 1, "resolution": 3,
    "defining_relation": ["ABD", "ACE", "BCDE"],
    "design": [{"A": a, "B": b, "C": c, "D": a * b, "E": a * c}
               for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)],
}
frac_two_args = {"n_factors": 5, "fraction": 2}
v = check_factorial(frac_two, frac_two_args)
check("fractional_complete_defining_group_passes",
      v.passed and v.checks.get("defining_relation_cardinality") is True
      and v.checks.get("defining_relation_group_closed") is True
      and v.checks.get("defining_relation_holds_in_design") is True, str(v))
v = check_factorial(
    frac_two, {**frac_two_args, "generators": [[1, 2], [1, 3]]},
)
check("fractional_multiple_requested_generators_are_bound_in_order",
      v.passed and v.checks.get("requested_generators_honored") is True, str(v))
v = check_factorial(
    frac_two, {**frac_two_args, "generators": [[1, 2], [2, 1]]},
)
check("fractional_duplicate_generator_definitions_fail_closed",
      not v.passed and v.checks.get("custom_generators_valid") is False, str(v))

for label, mismatched_args, check_name in (
    ("n_factors", {"n_factors": 6, "fraction": 2},
     "factorial_n_factors_matches_request"),
    ("levels", {**frac_two_args, "levels": 3},
     "fractional_levels_match_request"),
    ("replicates", {**frac_two_args, "replicates": 2},
     "fractional_replicates_match_request"),
):
    verdict = check_factorial(frac_two, mismatched_args)
    check(f"fractional_valid_output_mismatched_{label}_request_fails_closed",
          not verdict.passed and verdict.checks.get(check_name) is False,
          str(verdict))
verdict = check_factorial(frac_two, {**frac_two_args, "levels": [2]})
check("fractional_length_one_levels_list_normalizes_to_two_level",
      verdict.passed
      and verdict.checks.get("fractional_levels_match_request") is True,
      str(verdict))

replicated_frac_two = {
    **frac_two,
    "n_runs": 16,
    "replicates": 2,
    "design": [dict(row) for row in frac_two["design"] for _ in range(2)],
}
verdict = check_factorial(
    replicated_frac_two, {**frac_two_args, "replicates": 2},
)
check("fractional_requested_replicate_multiplicity_passes",
      verdict.passed
      and verdict.checks.get("fractional_replicates_match_request") is True
      and verdict.checks.get("fractional_run_count_matches_fraction") is True,
      str(verdict))

v = check_factorial(dict(frac_two, defining_relation=["ABD"]), frac_two_args)
check("fractional_truncated_defining_relation_fails_closed",
      not v.passed and v.checks.get("defining_relation_cardinality") is False, str(v))
v = check_factorial(
    dict(frac_two, defining_relation=["ABD", "ACE"]), frac_two_args,
)
check("fractional_non_group_cardinality_fails_closed",
      not v.passed and v.checks.get("defining_relation_cardinality") is False, str(v))

# The right number and lengths of words are insufficient. This forged set is a
# closed group, but two words do not multiply to +1 in the actual matrix.
closed_but_forged = dict(
    frac_two, defining_relation=["ABC", "ADE", "BCDE"],
)
v = check_factorial(closed_but_forged, frac_two_args)
check("fractional_closed_but_forged_relation_fails_design_binding",
      not v.passed
      and v.checks.get("defining_relation_group_closed") is True
      and v.checks.get("defining_relation_holds_in_design") is False, str(v))

# Conversely, a same-cardinality set that omits the symmetric difference of two
# members is not the complete defining group.
nonclosed_relation = dict(
    frac_two, defining_relation=["ABD", "ACE", "ABCE"],
)
v = check_factorial(nonclosed_relation, frac_two_args)
check("fractional_same_cardinality_nonclosed_relation_fails_closed",
      not v.passed
      and v.checks.get("defining_relation_cardinality") is True
      and v.checks.get("defining_relation_group_closed") is False, str(v))

# Centers may be randomized through the run table, but they must be excluded
# from relation products and counted exactly.
frac_two_centers = {
    **frac_two,
    "n_runs": 10,
    "design": [*frac_two["design"],
               {column: 0 for column in ("A", "B", "C", "D", "E")},
               {column: 0 for column in ("A", "B", "C", "D", "E")}],
}
random.Random(31).shuffle(frac_two_centers["design"])
v = check_factorial(
    frac_two_centers, {**frac_two_args, "center_points": 2},
)
check("fractional_randomized_centers_preserve_relation_checks",
      v.passed
      and v.checks.get("fractional_center_rows_match_request") is True
      and v.checks.get("defining_relation_holds_in_design") is True, str(v))
v = check_factorial(
    frac_two_centers, {**frac_two_args, "center_points": 1},
)
check("fractional_center_count_must_match_request",
      not v.passed
      and v.checks.get("fractional_center_rows_match_request") is False, str(v))

duplicate_base = {**frac_two, "design": list(frac_two["design"])}
duplicate_base["design"][0] = dict(duplicate_base["design"][1])
v = check_factorial(duplicate_base, frac_two_args)
check("fractional_base_runs_must_be_unique",
      not v.passed
      and v.checks.get("fractional_run_count_matches_fraction") is False, str(v))

mixed_zero_row = {**frac_two, "design": [dict(row) for row in frac_two["design"]]}
mixed_zero_row["design"][0]["A"] = 0
v = check_factorial(mixed_zero_row, frac_two_args)
check("fractional_mixed_zero_row_fails_closed",
      not v.passed
      and v.checks.get("fractional_rows_are_base_or_center") is False, str(v))
v = check_factorial(frac_two, {**frac_two_args, "center_points": True})
check("fractional_boolean_center_count_fails_closed",
      not v.passed
      and v.checks.get("fractional_center_count_valid") is False, str(v))

# Eight runs cannot satisfy a requested 2^(5-3) design; that requires four base
# runs plus any explicitly requested centers.
v = check_factorial(
    dict(frac_two, n_factors=5), {"n_factors": 5, "fraction": 3},
)
check("fractional_run_count_must_match_fraction",
      not v.passed
      and v.checks.get("fractional_run_count_matches_fraction") is False, str(v))

# ── check_rsm ──────────────────────────────────────────────────────


def ccd_fixture(n_factors=2, fraction=0, centers=None,
                alpha_type="rotatable", alpha=None):
    centers = n_factors if centers is None else centers
    n_factorial = 2 ** (n_factors - fraction)
    if fraction == 0:
        factorial_vectors = list(product((-1, 1), repeat=n_factors))
    elif n_factors == 3 and fraction == 1:
        factorial_vectors = [
            (left, right, left * right)
            for left, right in product((-1, 1), repeat=2)
        ]
    else:
        raise AssertionError("test fixture supports only full CCD or 3-factor half fraction")
    alpha = (
        1 if alpha_type == "face" else n_factorial ** 0.25
    ) if alpha is None else alpha
    names = [chr(ord("A") + index) for index in range(n_factors)]

    def row(vector, point_type):
        return {**dict(zip(names, vector)), "point_type": point_type}

    factorial_rows = [row(vector, "factorial") for vector in factorial_vectors]
    axial_rows = []
    for index in range(n_factors):
        for sign in (-1, 1):
            vector = [0] * n_factors
            vector[index] = sign * alpha
            axial_rows.append(row(vector, "axial"))
    center_rows = [row([0] * n_factors, "center") for _ in range(centers)]
    design = factorial_rows + axial_rows + center_rows
    rank = {"factorial": 0, "axial": 1, "edge": 2, "center": 3}
    design.sort(key=lambda item: (
        rank[item["point_type"]], *(item[name] for name in names),
    ))
    for index, design_row in enumerate(design, start=1):
        design_row["std_order"] = index
        design_row["run"] = index
    return {
        "type": "central_composite", "n_factors": n_factors,
        "alpha": alpha, "alpha_type": alpha_type,
        "n_factorial": n_factorial, "n_axial": 2 * n_factors,
        "n_center": centers, "n_runs": len(design), "design": design,
    }


def bbd_fixture(n_factors=3, centers=None):
    centers = n_factors if centers is None else centers
    names = [chr(ord("A") + index) for index in range(n_factors)]
    design = []
    for left in range(n_factors):
        for right in range(left + 1, n_factors):
            for left_sign, right_sign in product((-1, 1), repeat=2):
                vector = [0] * n_factors
                vector[left] = left_sign
                vector[right] = right_sign
                design.append({
                    **dict(zip(names, vector)), "point_type": "edge",
                })
    design.extend({
        **dict(zip(names, [0] * n_factors)), "point_type": "center",
    } for _ in range(centers))
    rank = {"factorial": 0, "axial": 1, "edge": 2, "center": 3}
    design.sort(key=lambda item: (
        rank[item["point_type"]], *(item[name] for name in names),
    ))
    for index, design_row in enumerate(design, start=1):
        design_row["std_order"] = index
        design_row["run"] = index
    n_edge = 4 * (n_factors * (n_factors - 1) // 2)
    return {
        "type": "box_behnken", "n_factors": n_factors,
        "n_edge": n_edge, "n_center": centers,
        "n_runs": len(design), "design": design,
    }


ccd_args = {"n_factors": 2}
ccd = ccd_fixture()
v = check_rsm(ccd, ccd_args)
check("ccd_geometry_and_effective_defaults_pass", v.passed, str(v))

fractional_ccd_args = {
    "n_factors": 3, "fraction": 1, "center_points": 1,
    "alpha": "face", "randomize": True, "seed": 7,
}
fractional_ccd = ccd_fixture(
    n_factors=3, fraction=1, centers=1, alpha_type="face",
)
random.Random(7).shuffle(fractional_ccd["design"])
for index, design_row in enumerate(fractional_ccd["design"], start=1):
    design_row["run"] = index
fractional_ccd["seed"] = 7
v = check_rsm(fractional_ccd, fractional_ccd_args)
check("fractional_randomized_ccd_request_binding_passes", v.passed, str(v))

v = check_rsm(dict(ccd, n_runs=12), ccd_args)
check("ccd_count_mismatch_fails", not v.passed, str(v))
v = check_rsm(dict(ccd, n_factors=3), ccd_args)
check("ccd_result_n_factors_must_match_request", not v.passed, str(v))
v = check_rsm(dict(ccd, type="box_behnken"), ccd_args)
check("ccd_type_must_match_request", not v.passed, str(v))
v = check_rsm(dict(ccd, alpha_type="face", alpha=1), ccd_args)
check("ccd_alpha_mode_and_value_must_match_request", not v.passed, str(v))
v = check_rsm(ccd, {**ccd_args, "center_points": 3})
check("ccd_center_count_must_match_request", not v.passed
      and v.checks.get("ccd_declared_counts_match_request") is False, str(v))
v = check_rsm(fractional_ccd, {**fractional_ccd_args, "seed": 8})
check("ccd_seed_echo_must_match_randomized_request", not v.passed, str(v))
unexpected_seed_ccd = dict(ccd, seed=42)
v = check_rsm(unexpected_seed_ccd, ccd_args)
check("ccd_nonrandomized_result_must_not_claim_seed", not v.passed
      and v.checks.get("rsm_randomization_seed_matches_request") is False, str(v))
v = check_rsm(ccd, {**ccd_args, "seed": 7})
check("ccd_request_cannot_supply_inert_seed", not v.passed
      and v.checks.get("rsm_seed_requires_randomization") is False, str(v))
missing_order_ccd = ccd_fixture()
for row in missing_order_ccd["design"]:
    row.pop("run")
    row.pop("std_order")
v = check_rsm(missing_order_ccd, ccd_args)
check("ccd_missing_run_order_metadata_fails_closed", not v.passed
      and v.checks.get("rsm_run_order_metadata_complete") is False, str(v))
unshuffled_randomized_ccd = {
    **fractional_ccd,
    "design": sorted(
        (dict(row) for row in fractional_ccd["design"]),
        key=lambda row: row["std_order"],
    ),
}
for index, row in enumerate(unshuffled_randomized_ccd["design"], start=1):
    row["run"] = index
v = check_rsm(unshuffled_randomized_ccd, fractional_ccd_args)
check("ccd_randomize_true_requires_nonstandard_run_order", not v.passed
      and v.checks.get("rsm_randomization_state_matches_request") is False,
      str(v))
forged_order_ccd = ccd_fixture()
forged_order_ccd["seed"] = 7
forged_order_ccd["design"][0]["std_order"] = 2
forged_order_ccd["design"][1]["std_order"] = 1
v = check_rsm(
    forged_order_ccd,
    {"n_factors": 2, "randomize": True, "seed": 7},
)
check("ccd_forged_nonidentity_std_order_cannot_claim_randomization",
      not v.passed
      and v.checks.get("rsm_std_order_matches_canonical_rows") is False,
      str(v))
v = check_rsm(fractional_ccd, {**fractional_ccd_args, "fraction": 0})
check("ccd_fraction_must_match_factorial_core", not v.passed, str(v))
irregular_fraction = {
    **fractional_ccd,
    "design": [dict(row) for row in fractional_ccd["design"]],
}
first_factorial = next(
    row for row in irregular_fraction["design"]
    if row["point_type"] == "factorial"
)
first_factorial["A"] = first_factorial["B"] = first_factorial["C"] = -1
v = check_rsm(irregular_fraction, fractional_ccd_args)
check("ccd_fractional_core_must_be_regular", not v.passed
      and v.checks.get("ccd_factorial_core_geometry") is False, str(v))

resolution_two_ccd = ccd_fixture(
    n_factors=3, fraction=1, centers=1, alpha_type="face",
)
resolution_two_vectors = [
    (-1, -1, -1), (-1, -1, 1), (1, 1, -1), (1, 1, 1),
]
for row, vector in zip(
    (row for row in resolution_two_ccd["design"]
     if row["point_type"] == "factorial"),
    resolution_two_vectors,
):
    row["A"], row["B"], row["C"] = vector
v = check_rsm(
    resolution_two_ccd,
    {"n_factors": 3, "fraction": 1, "center_points": 1, "alpha": "face"},
)
check("ccd_resolution_two_fraction_fails_main_effect_estimability", not v.passed
      and v.checks.get("ccd_factorial_core_geometry") is True
      and v.checks.get("ccd_factorial_main_effects_estimable") is False,
      str(v))

all_zero_ccd = ccd_fixture()
for row in all_zero_ccd["design"]:
    row["A"] = row["B"] = 0
v = check_rsm(all_zero_ccd, ccd_args)
check("all_zero_ccd_fails_geometry", not v.passed
      and v.checks.get("ccd_factorial_core_geometry") is False
      and v.checks.get("ccd_axial_geometry") is False, str(v))

wrong_labels = ccd_fixture()
wrong_labels["design"][0]["point_type"] = "center"
v = check_rsm(wrong_labels, ccd_args)
check("ccd_actual_point_type_counts_must_match", not v.passed
      and v.checks.get("ccd_point_type_counts_match") is False, str(v))

non_object_design = dict(ccd, design=[*ccd["design"][:-1], "not-a-row"])
v = check_rsm(non_object_design, ccd_args)
check("rsm_non_object_row_fails_closed", not v.passed
      and v.checks.get("rsm_design_rows_are_objects") is False, str(v))
missing_column = ccd_fixture()
del missing_column["design"][0]["B"]
v = check_rsm(missing_column, ccd_args)
check("rsm_missing_factor_column_fails_closed", not v.passed
      and v.checks.get("rsm_factor_matrix_numeric_and_rectangular") is False, str(v))
nonnumeric_column = ccd_fixture()
nonnumeric_column["design"][0]["B"] = "zero"
v = check_rsm(nonnumeric_column, ccd_args)
check("rsm_nonnumeric_factor_column_fails_closed", not v.passed
      and v.checks.get("rsm_factor_matrix_numeric_and_rectangular") is False, str(v))
nonfinite_column = ccd_fixture()
nonfinite_column["design"][0]["B"] = float("nan")
v = check_rsm(nonfinite_column, ccd_args)
check("rsm_nonfinite_factor_column_fails_closed", not v.passed
      and v.checks.get("rsm_factor_matrix_numeric_and_rectangular") is False, str(v))

metadata_ccd = ccd_fixture()
for index, row in enumerate(metadata_ccd["design"], start=1):
    row["RUN"] = row.pop("run")
    row["Std_Order"] = row.pop("std_order")
    row["Point_Type"] = row.pop("point_type").upper()
v = check_rsm(metadata_ccd, ccd_args)
check("rsm_metadata_columns_are_casefolded_not_factors", v.passed
      and v.checks.get("rsm_factor_columns_match_result") is True, str(v))

reverse_column_ccd = ccd_fixture()
reverse_column_ccd["design"] = [
    {
        "B": row["B"], "A": row["A"],
        "point_type": row["point_type"],
        "std_order": row["std_order"], "run": row["run"],
    }
    for row in reverse_column_ccd["design"]
]
v = check_rsm(reverse_column_ccd, ccd_args)
check("rsm_factor_insertion_order_does_not_change_public_standard_order",
      v.passed, str(v))

insertion_bound_ccd = ccd_fixture()
insertion_bound_ccd["design"] = [
    {
        "B": row["B"], "A": row["A"],
        "point_type": row["point_type"],
    }
    for row in insertion_bound_ccd["design"]
]
rank = {"factorial": 0, "axial": 1, "edge": 2, "center": 3}
insertion_bound_ccd["design"].sort(key=lambda row: (
    rank[row["point_type"]], row["B"], row["A"],
))
for index, row in enumerate(insertion_bound_ccd["design"], start=1):
    row["std_order"] = index
    row["run"] = index
v = check_rsm(insertion_bound_ccd, ccd_args)
check("rsm_std_order_cannot_bind_private_factor_insertion_order",
      not v.passed
      and v.checks.get("rsm_std_order_matches_canonical_rows") is False,
      str(v))

bbd_args = {"n_factors": 3, "design": "bbd"}
bbd = bbd_fixture()
v = check_rsm(bbd, bbd_args)
check("bbd_geometry_and_effective_defaults_pass", v.passed, str(v))

all_zero_bbd = bbd_fixture()
for row in all_zero_bbd["design"]:
    row["A"] = row["B"] = row["C"] = 0
v = check_rsm(all_zero_bbd, bbd_args)
check("all_zero_bbd_fails_geometry", not v.passed
      and v.checks.get("bbd_edge_geometry") is False, str(v))

v = check_rsm(bbd, {**bbd_args, "alpha": "face"})
check("bbd_rejects_inert_alpha_setting", not v.passed
      and v.checks.get("bbd_inapplicable_parameters_absent") is False, str(v))
v = check_rsm(bbd_fixture(2), {"n_factors": 2, "design": "bbd"})
check("bbd_requires_three_to_five_factors", not v.passed
      and v.checks.get("bbd_n_factors_supported") is False, str(v))
check("rsm_empty_fails", not check_rsm({}, ccd_args).passed)

# ── design_checks_for mapping ──────────────────────────────────────
vs = design_checks_for("sample_size", {"endpoint_type": "binary",
                                       "null_param": 0.4, "alt_param": 0.2,
                                       "design": "single_arm"},
                       {"results": [ok_row]})
check("dcf_sample_size_catches_direction",
      not combined_gate(*vs).passed)
vs = design_checks_for("master_simulate",
                       {"config": {"master_design_type": "basket",
                                   "fwer_control": "holm"}},
                       {"result": {"oc_table": []}})
check("dcf_master_catches_reserved", not combined_gate(*vs).passed)
vs = design_checks_for("ab_test", ab_args, ab_ok)
check("dcf_ab_test_request_fidelity_wired", combined_gate(*vs).passed)
vs = design_checks_for("ab_test", dict(ab_args, baseline=0.2), ab_ok)
check("dcf_ab_test_mismatch_fails", not combined_gate(*vs).passed)
vs = design_checks_for("randomize", simple_args, rand_ok)
check("dcf_randomize_wired", combined_gate(*vs).passed)
vs = design_checks_for("meta_analyze", {"alpha": 0.05}, dict(meta_ok, lower=0.6))
check("dcf_meta_catches_bad_ci", not combined_gate(*vs).passed)
vs = design_checks_for("some_future_tool", {}, {"value": 1})
check("dcf_unknown_tool_fails_closed", not combined_gate(*vs).passed)

v = check_output_contract({"sample_size": {"n": 20}, "ppos": {"error": "failed"}})
check("nested_tool_error_fails_output_contract", not v.passed, str(v))

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
