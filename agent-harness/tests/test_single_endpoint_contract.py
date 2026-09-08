#!/usr/bin/env python3
"""Scientific metadata must survive actual R output, verification and reporting."""

from __future__ import annotations

import contextlib
import copy
import io
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "agent-harness"))
from final_report import (
    SINGLE_ENDPOINT_RESULT_CONTRACT, canonical_report, privacy_safe_view,
    single_endpoint_contract_errors,
)
from gates import GateVerdict, MANUAL_CHECKS, combined_gate, design_checks_for
from verification import VerificationIdentity, envelope_from_verdict
import server_verify

passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"TEST {name} : PASS")


def run_r(tool: str, arguments: dict, *, expect_error: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="single-endpoint-contract-") as directory:
        request = Path(directory) / "input.json"
        output = Path(directory) / "output.json"
        request.write_text(json.dumps(arguments), encoding="utf-8")
        completed = subprocess.run(
            [str(ROOT / "tools/run-reviewed-r.sh"),
             str(ROOT / "mcp-server/r-wrapper/dispatcher.R"), tool, str(request), str(output)],
            cwd=ROOT, capture_output=True, text=True, timeout=180,
        )
        if not output.is_file():
            raise AssertionError("R contract fixture execution failed")
        result = json.loads(output.read_text(encoding="utf-8"))
        if expect_error:
            if completed.returncode != 1 or not isinstance(result.get("error"), str):
                raise AssertionError("R did not reject an inadmissible workload")
            return result
        if completed.returncode != 0:
            raise AssertionError("R contract fixture execution failed")
        if "error" in result:
            raise AssertionError("R contract fixture was rejected")
        return result


def report(tool: str, arguments: dict, result: dict) -> str:
    identity = VerificationIdentity.from_call(
        tool, arguments, result, "call-contract-fixture",
    )
    envelope = envelope_from_verdict(
        identity,
        GateVerdict(passed=True, checks={"regression_suite": True, "reproducibility": True},
                    blocked=list(MANUAL_CHECKS)),
    )
    return canonical_report(tool, arguments, result, envelope.to_dict())


config = {
    "endpoint_type": "binary", "study_type": "confirmatory", "design": "single_arm",
    "null_param": 0.21, "alt_param": 0.29, "alphas": [0.025], "powers": [0.8],
    "p2_data": {"x": 30, "n": 100}, "p3_n": 225,
}
arguments = {"config": config, "B_oc": 625, "seed": 42}
simulation = run_r("simulate_design", arguments)
check("real_r_simulation_satisfies_versioned_contract",
      not single_endpoint_contract_errors("simulate_design", simulation, arguments))
budget_rejection = run_r("simulate_design", {
    "config": {**config, "design": "controlled", "study_type": "poc",
               "p2_data": None, "p3_n": None},
    "n_oc": {"n_trt": 500, "n_ctrl": 500}, "B_oc": 5000,
}, expect_error=True)
check("dispatcher_rejects_actual_scenario_work_before_simulation",
      "scenario or posterior evaluation budget" in budget_rejection["error"])
public = privacy_safe_view("simulate_design", simulation)
sizing_row = simulation["sample_size"][0]
exact_power = sum(math.comb(sizing_row["n_total"], x) * config["alt_param"] ** x
                  * (1 - config["alt_param"]) ** (sizing_row["n_total"] - x)
                  for x in range(sizing_row["k_crit"], sizing_row["n_total"] + 1))
check("dispatcher_retains_full_precision_against_independent_binomial_sum",
      abs(sizing_row["power_achieved"] - exact_power) < 1e-12)
for result_type, raw, safe in (
    ("sample_size_row", simulation["sample_size"][0], public["sample_size"][0]),
    ("oc_row", simulation["oc"][0], public["oc"][0]),
    ("ppos_result", simulation["ppos"], public["ppos"]),
):
    fields = SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"][result_type]["required"]
    check(f"all_required_{result_type}_fields_survive_projection",
          all(field in safe and safe[field] == raw[field] for field in fields))
check("public_scientific_projection_is_idempotent",
      privacy_safe_view("simulate_design", public) == public)

private = copy.deepcopy(simulation)
private["secret_note"] = "PRIVATE-PATIENT-4711"
private["oc"][0]["label"] = "PRIVATE-PATIENT-4711"
private["ppos"]["cond_power_draws"] = [0.5] * private["ppos"]["n_mc"]
private["ppos"]["analysis_comment"] = "PRIVATE-PATIENT-4711"
rendered = report("simulate_design", arguments, private)
check("canonical_report_preserves_precision_and_method_without_private_text",
      "PRIVATE-PATIENT-4711" not in rendered
      and "cond_power_draws" not in rendered
      and "Monte Carlo precision" in rendered
      and "Confirmatory test: exact_binomial" in rendered
      and "p_go_mcse" in rendered and "ppos_mcse" in rendered
      and "Posterior draws" in rendered and "Assurance dimensions" in rendered)
check("canonical_report_is_deterministic",
      rendered == report("simulate_design", arguments, private))
check("workload_upper_bound_is_preserved_and_explained",
      public.get("workload") == simulation.get("workload")
      and "not runtime predictions" in rendered)
check("prior_label_comes_from_resolved_result_not_an_invented_default",
      "prior" not in config and simulation["ppos"]["prior_method"] == "binary_jeffreys"
      and "Prior method: binary_jeffreys" in rendered
      and "Supplied assumptions" in rendered and "Omitted engine defaults" in rendered)

for type_name, container, item in (
    ("sample_size_row", "sample_size", 0),
    ("oc_row", "oc", 0),
    ("ppos_result", "ppos", None),
):
    required = SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"][type_name]["required"]
    for field in required:
        damaged = copy.deepcopy(simulation)
        row = damaged[container] if item is None else damaged[container][item]
        del row[field]
        check(f"missing_{type_name}_{field}_fails_contract",
              bool(single_endpoint_contract_errors("simulate_design", damaged, arguments)))

for value in ("PRIVATE-PATIENT-4711", 7, None):
    damaged = copy.deepcopy(simulation)
    damaged["ppos"]["confirmatory_test"] = value
    check(f"unapproved_confirmatory_method_{type(value).__name__}_fails_closed",
          bool(single_endpoint_contract_errors("simulate_design", damaged, arguments)))

for field, value in (
    ("mc_replicates", 1), ("mc_replicates", 625.5), ("mc_replicates", True),
    ("p_go_mcse", float("nan")), ("p_go_mc_upper", float("inf")),
    ("inner_probability_abs_error_max", -1),
):
    damaged = copy.deepcopy(simulation)
    damaged["oc"][0][field] = value
    check(f"invalid_oc_{field}_{repr(value)}_fails_closed",
          bool(single_endpoint_contract_errors("simulate_design", damaged, arguments)))

for draws in ([0.5], [float("nan")] * simulation["ppos"]["n_mc"], None):
    damaged = copy.deepcopy(simulation)
    damaged["ppos"]["cond_power_draws"] = draws
    # Omission is valid in a public DTO; a supplied null/invalid raw draw array is not.
    check(f"invalid_ppos_draws_{type(draws).__name__}_{len(draws) if draws else 0}",
          bool(single_endpoint_contract_errors("simulate_design", damaged, arguments)))

for rows in (None, 2, "private label", []):
    damaged = copy.deepcopy(simulation)
    damaged["oc"] = rows
    check(f"invalid_oc_collection_{type(rows).__name__}_fails_without_exception",
          bool(single_endpoint_contract_errors("simulate_design", damaged, arguments)))

for field, value in (("replicates", 1), ("scenario_count", 0), ("simulated_units", float("inf"))):
    damaged = copy.deepcopy(simulation)
    damaged["workload"][field] = value
    check(f"invalid_workload_{field}_fails_closed",
          bool(single_endpoint_contract_errors("simulate_design", damaged, arguments)))

gamma = copy.deepcopy(simulation)
gamma["oc"][0]["inner_probability_method"] = "gamma_cdf"
check("gamma_cdf_method_survives_contract_and_reporting",
      not single_endpoint_contract_errors("simulate_design", gamma, arguments)
      and "gamma_cdf" in report("simulate_design", arguments, gamma))

damaged = copy.deepcopy(simulation)
del damaged["ppos"]["n_mc"]
try:
    report("simulate_design", arguments, damaged)
except ValueError:
    rejected = True
else:
    rejected = False
check("canonical_reporting_cannot_silently_drop_required_metadata", rejected)
check("missing_contract_version_fails_strict_presentation_admission",
      bool(single_endpoint_contract_errors("sample_size", {"results": [{"n_total": 30}]})))
check("unrelated_result_summaries_do_not_inherit_single_endpoint_requirements",
      not single_endpoint_contract_errors("meta_analyze", {"estimate": 1.2}))

for name, sizing_config, expected_method in (
    ("continuous_unequal", {
        "endpoint_type": "continuous", "study_type": "poc", "design": "controlled",
        "null_param": 0, "alt_param": 1, "sd": 2, "alloc_ratio": 2,
        "alphas": [0.025], "powers": [0.8],
    }, "two_sample_z_normal_approximation"),
    ("poisson_controlled", {
        "endpoint_type": "incidence_rate", "study_type": "poc", "design": "controlled",
        "null_param": 1, "alt_param": 0.7, "exposure_time": 1,
        "alphas": [0.025], "powers": [0.8],
    }, "poisson_rate_difference_wald"),
):
    sizes = run_r("sample_size", sizing_config)
    check(f"real_r_{name}_sizing_satisfies_contract",
          not single_endpoint_contract_errors("sample_size", sizes, sizing_config))
    safe_sizes = privacy_safe_view("sample_size", sizes)
    check(f"real_r_{name}_method_is_not_dropped",
          expected_method in {row["test"] for row in safe_sizes["results"]}
          and expected_method in report("sample_size", sizing_config, sizes))

for ratio in ("hr", "rr"):
    changed = copy.deepcopy(simulation)
    changed["ppos"].update({
        f"{ratio}_post_mean": None, f"{ratio}_post_mean_status": "does_not_exist",
        f"{ratio}_post_median": 2.0, f"{ratio}_post_ci": [0.1, 500.0],
        "ratio_summary_method": "scaled_beta_prime",
    })
    check(f"undefined_{ratio}_mean_keeps_median_interval_and_status",
          not single_endpoint_contract_errors("simulate_design", changed, arguments)
          and "mean does not exist" in report("simulate_design", arguments, changed))
    changed["ppos"][f"{ratio}_post_mean"] = 123.0
    check(f"fabricated_finite_{ratio}_mean_is_rejected",
          bool(single_endpoint_contract_errors("simulate_design", changed, arguments)))

search_config = {
    "endpoint_type": "binary", "study_type": "poc", "design": "single_arm",
    "null_param": 0.2, "alt_param": 0.20001, "alphas": [0.025], "powers": [0.8],
}
unavailable = run_r("sample_size", search_config)
unavailable_report = report("sample_size", search_config, unavailable)
check("real_r_search_limit_outcome_preserves_unavailable_sizes_and_power",
      not single_endpoint_contract_errors("sample_size", unavailable, search_config)
      and unavailable["results"][0]["sizing_status"] == "search_limit_reached"
      and unavailable["results"][0]["power_achieved"] is None
      and "no design is validated for those rows" in unavailable_report
      and "Unavailable" in unavailable_report)
for field in ("n_total", "n_trt", "n_ctrl", "power_achieved"):
    invented = copy.deepcopy(unavailable)
    invented["results"][0][field] = 0.8 if field == "power_achieved" else 2
    check(f"search_limit_cannot_claim_available_{field}",
          bool(single_endpoint_contract_errors("sample_size", invented, search_config)))
invented = copy.deepcopy(unavailable)
invented["results"][0]["sizing_status"] = "target_met"
check("target_met_requires_available_sizes_and_achieved_power",
      bool(single_endpoint_contract_errors("sample_size", invented, search_config)))

poor = copy.deepcopy(simulation)
for row in poor["oc"]:
    row["p_go"] = 0.1
    row["p_consider"] = 0.4
    row["p_nogo"] = 0.5
    for probability in ("p_go", "p_consider", "p_nogo"):
        row[probability + "_mc_lower"] = max(0, row[probability] - 0.03)
        row[probability + "_mc_upper"] = min(1, row[probability] + 0.03)
poor_report = report("simulate_design", arguments, poor)
check("unfavorable_design_is_described_without_promoting_or_hiding_calculation",
      "Does not meet the selected OC heuristics" in poor_report
      and "An unfavorable result is retained for design review." in poor_report)

provenance = {
    **{key: "contract-fixture" for key in (
        "engine_version", "engine_fingerprint", "node_version", "python_version",
        "python_runtime_fingerprint", "r_version", "r_runtime_fingerprint",
    )},
    "r_package_versions": {}, "config_bound": True,
    "input_file_count": 0, "artifact_count": 0,
}
request = {
    "tool": "simulate_design", "args": arguments, "result": damaged,
    "runtime_id": "analysis-contract-fixture", "public_provenance": provenance,
}
out = io.StringIO()
with patch.object(server_verify, "design_checks_for", return_value=[]), patch.object(
    server_verify, "check_regression_tests", return_value=GateVerdict(passed=True),
), patch.object(sys, "stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(out):
    server_verify.main()
withheld = json.loads(out.getvalue())
check("server_verifier_withholds_invalid_contract_without_values",
      withheld["presentable"] is False and withheld["status"] == "RETRY_REQUIRED"
      and "public_result" not in withheld and "report" not in withheld
      and "PRIVATE-PATIENT-4711" not in out.getvalue())

request.update({"tool": "sample_size", "args": search_config, "result": unavailable})
out = io.StringIO()
with patch.object(
    server_verify, "check_regression_tests", return_value=GateVerdict(passed=True),
), patch.object(sys, "stdin", io.StringIO(json.dumps(request))), contextlib.redirect_stdout(out):
    server_verify.main()
partial = json.loads(out.getvalue())
check("real_search_limit_passes_actual_gates_as_an_unavailable_partial_result",
      partial["presentable"] is True and partial["status"] == "PASS_PARTIAL"
      and partial["public_result"]["results"][0]["n_total"] is None
      and "no design is validated for those rows" in partial["report"])

# Exercise the complete supported endpoint/design matrix using actual R output
# and real design gates. A small, explicit OC budget keeps this a contract test;
# its limited precision is disclosed by the gates, not promoted to high precision.
endpoint_cases = (
    ("binary", {"null_param": 0.2, "alt_param": 0.4},
     {"x": 8, "n": 20}, {"x": 4, "n": 20}),
    ("continuous", {"null_param": 0, "alt_param": 0.5, "sd": 1},
     {"n": 20, "x_bar": 0.5, "s2": 1}, {"n": 20, "x_bar": 0, "s2": 1}),
    ("tte", {"null_param": 0.2, "alt_param": 0.1,
             "accrual_time": 12, "followup_time": 6},
     {"events": 3, "person_time": 30}, {"events": 6, "person_time": 30}),
    ("incidence_rate", {"null_param": 0.2, "alt_param": 0.1, "exposure_time": 1},
     {"count": 3, "exposure": 30}, {"count": 6, "exposure": 30}),
)
for endpoint, parameters, observed, control in endpoint_cases:
    for design in ("single_arm", "controlled"):
        matrix_config = {
            "endpoint_type": endpoint, "study_type": "confirmatory", "design": design,
            "alphas": [0.025], "powers": [0.8], "p3_n": 80,
            "p2_data": observed, **parameters,
        }
        if design == "controlled":
            matrix_config["p2_data_ctrl"] = control
        matrix_args = {
            "config": matrix_config, "B_oc": 32, "seed": 42,
            "n_oc": {"n_trt": 20, "n_ctrl": 20} if design == "controlled" else 20,
        }
        matrix_result = run_r("simulate_design", matrix_args)
        matrix_gate = combined_gate(*design_checks_for(
            "simulate_design", matrix_args, matrix_result,
        ), GateVerdict(passed=True, blocked=list(MANUAL_CHECKS)))
        matrix_errors = single_endpoint_contract_errors(
            "simulate_design", matrix_result, matrix_args,
        )
        if not matrix_gate.passed or matrix_errors:
            raise AssertionError(f"public_{endpoint}_{design}_actual_R_contract_gates_report")
        matrix_identity = VerificationIdentity.from_call(
            "simulate_design", matrix_args, matrix_result,
            f"call-endpoint-matrix-{endpoint}-{design}",
        )
        matrix_report = canonical_report(
            "simulate_design", matrix_args, matrix_result,
            envelope_from_verdict(matrix_identity, matrix_gate).to_dict(),
        )
        matrix_public = privacy_safe_view("simulate_design", matrix_result)
        method_fields = (
            [row["test"] for row in matrix_result["sample_size"]]
            + [row["inner_probability_method"] for row in matrix_result["oc"]]
            + [matrix_result["ppos"]["confirmatory_test"], matrix_result["ppos"]["prior_method"]]
        )
        check(f"public_{endpoint}_{design}_actual_R_contract_gates_report",
              all(method in matrix_report for method in method_fields)
              and matrix_public["oc"] == matrix_result["oc"]
              and all(matrix_public["ppos"][field] == matrix_result["ppos"][field]
                      for field in SINGLE_ENDPOINT_RESULT_CONTRACT["result_types"]["ppos_result"]["required"])
              and all(field in matrix_report for field in (
                  "p_go_mcse", "p_consider_mcse", "p_nogo_mcse", "mc_replicates",
                  "ppos_mcse", "ppos_mc_lower", "ppos_mc_upper", "n_mc",
              )))

print(f"Single-endpoint result contract tests: {passed} passed, 0 failed")
