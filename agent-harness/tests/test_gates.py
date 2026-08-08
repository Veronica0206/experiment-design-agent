#!/usr/bin/env python3
"""Unit tests for gates.py — the verification safety net for every runtime
(harness, SubagentStop hook, Streamlit form). Plain script, no pytest needed.

Run: python3 agent-harness/tests/test_gates.py   (from the suite root)
"""
from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
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
            {"overdispersion": 2}, {"tte_method": "cox_ph"}):
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

# ── check_ab_test ──────────────────────────────────────────────────
ab_ok = {"n_total": 200, "n_control": 100, "n_treatment": 100, "mde": 0.05,
         "sided": 2, "target_power": 0.8, "achieved_power": 0.81}
v = check_ab_test(ab_ok)
check("ab_good_passes", v.passed and not v.blocked, str(v))
v = check_ab_test({k: v2 for k, v2 in ab_ok.items()
                   if k not in ("target_power", "achieved_power")})
check("ab_missing_power_fails", not v.passed, str(v))
v = check_ab_test(dict(ab_ok, achieved_power=0.5))
check("ab_power_shortfall_fails", not v.passed, str(v))

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
        "subgroup,null_param,alt_param,reject_rate\n1,0.2,0.4,80\n", encoding="utf-8")
    (root / "basket_fwer.csv").write_text("scenario,fwer\nGlobal null,5\n", encoding="utf-8")
    (root / "basket_subgroup_decisions.csv").write_text(
        "subgroup,decision\n1,Go\n", encoding="utf-8")
    (root / "basket_oc_curves.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    master_result = {
        "result": {"oc_table": [{"subgroup": 1, "null_param": 0.2,
                                   "alt_param": 0.4, "reject_rate": 80}]},
        "output_dir": str(root),
    }
    check("master_artifacts_consistent_pass",
          check_master_result(master_result, {"master_design_type": "basket"}).passed)
    (root / "basket_oc_table.csv").write_text(
        "subgroup,null_param,alt_param,reject_rate\n1,0.2,0.4,NaN\n", encoding="utf-8")
    check("master_artifact_nonfinite_rejected",
          not check_master_result(master_result, {"master_design_type": "basket"}).passed)
    (root / "basket_oc_table.csv").write_text(
        "subgroup,null_param,alt_param,reject_rate\n1,0.2,0.4,70\n", encoding="utf-8")
    check("master_artifact_json_mismatch_rejected",
          not check_master_result(master_result, {"master_design_type": "basket"}).passed)
    check("master_unknown_family_rejected",
          not check_master_result(master_result, {"master_design_type": "unknown"}).passed)

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
           "study_effects": [0.4, 0.5, 0.7],
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

# ── check_randomize ────────────────────────────────────────────────
rand_ok = {"method": "block", "n": 4, "arms": ["control", "treatment"],
           "counts": {"control": 2, "treatment": 2}, "seed": 42,
           "assignment": [{"unit": 1, "arm": "control"},
                          {"unit": 2, "arm": "treatment"},
                          {"unit": 3, "arm": "control"},
                          {"unit": 4, "arm": "treatment"}]}
v = check_randomize(rand_ok, {})
check("randomize_good_passes", v.passed, str(v))
v = check_randomize(dict(rand_ok, assignment=rand_ok["assignment"][:3]), {})
check("randomize_missing_unit_fails", not v.passed, str(v))
v = check_randomize(dict(rand_ok, counts={"control": 3, "treatment": 2}), {})
check("randomize_counts_mismatch_fails", not v.passed, str(v))
v = check_randomize({k: v2 for k, v2 in rand_ok.items() if k != "seed"}, {})
check("randomize_no_seed_fails", not v.passed, str(v))
duplicate_units = dict(rand_ok, assignment=[
    {"unit": 1, "arm": "control"}, {"unit": 1, "arm": "treatment"},
    {"unit": 3, "arm": "control"}, {"unit": 4, "arm": "treatment"},
])
v = check_randomize(duplicate_units, {})
check("randomize_duplicate_unit_fails", not v.passed, str(v))

# ── check_factorial ────────────────────────────────────────────────
fact_ok = {"type": "full_factorial", "n_factors": 2, "n_runs": 4,
           "levels": [2, 2], "replicates": 1,
           "design": [{"A": -1, "B": -1}, {"A": 1, "B": -1},
                      {"A": -1, "B": 1}, {"A": 1, "B": 1}]}
v = check_factorial(fact_ok, {})
check("factorial_orthogonal_passes", v.passed, str(v))
v = check_factorial(dict(fact_ok, design=fact_ok["design"][:3], n_runs=3), {})
check("factorial_broken_orthogonality_fails", not v.passed, str(v))
v = check_factorial(dict(fact_ok, n_runs=8), {})
check("factorial_run_count_mismatch_fails", not v.passed, str(v))
check("factorial_empty_fails", not check_factorial({}, {}).passed)
frac = {"type": "fractional_factorial", "n_factors": 4, "n_runs": 8,
        "resolution": 4, "defining_relation": ["ABCD"],
        "design": [{"A": a, "B": b, "C": c, "D": a * b * c}
                   for a in (-1, 1) for b in (-1, 1) for c in (-1, 1)]}
v = check_factorial(frac, {})
check("fractional_resolution_matches", v.passed, str(v))
v = check_factorial(dict(frac, resolution=3), {})
check("fractional_resolution_mismatch_fails", not v.passed, str(v))

# ── check_rsm ──────────────────────────────────────────────────────
ccd = {"type": "central_composite", "n_factors": 2, "alpha": 1.4142,
       "n_factorial": 4, "n_axial": 4, "n_center": 5, "n_runs": 13,
       "design": [{"A": 0, "B": 0}] * 13}
v = check_rsm(ccd, {})
check("ccd_counts_pass", v.passed, str(v))
v = check_rsm(dict(ccd, n_runs=12), {})
check("ccd_count_mismatch_fails", not v.passed, str(v))
v = check_rsm(dict(ccd, n_axial=3, n_runs=12), {})
check("ccd_axial_not_2k_fails", not v.passed, str(v))
v = check_rsm({"type": "box_behnken", "n_edge": 12, "n_center": 3,
               "n_runs": 15, "design": [{"A": 0, "B": 0, "C": 0}] * 15}, {})
check("bbd_counts_pass", v.passed, str(v))
check("rsm_empty_fails", not check_rsm({}, {}).passed)

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
vs = design_checks_for("randomize", {"n": 4}, rand_ok)
check("dcf_randomize_wired", combined_gate(*vs).passed)
vs = design_checks_for("meta_analyze", {"alpha": 0.05}, dict(meta_ok, lower=0.6))
check("dcf_meta_catches_bad_ci", not combined_gate(*vs).passed)
vs = design_checks_for("some_future_tool", {}, {"value": 1})
check("dcf_unknown_tool_fails_closed", not combined_gate(*vs).passed)

v = check_output_contract({"sample_size": {"n": 20}, "ppos": {"error": "failed"}})
check("nested_tool_error_fails_output_contract", not v.passed, str(v))

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
