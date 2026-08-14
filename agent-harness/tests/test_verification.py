#!/usr/bin/env python3
"""Tests for verification identities, envelopes, and ledger isolation."""

from __future__ import annotations

import os
import json
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from gates import GateVerdict  # noqa: E402
from final_report import (canonical_public_report, canonical_report,
                          is_elicitation_message, privacy_safe_view,
                          public_arguments_view)  # noqa: E402
from verification import (PublicVerificationIdentity, VerificationIdentity, VerificationLedger,
                          VerificationStatus, canonical_value, envelope_from_verdict,
                          content_hash, identity_matches_call,
                          public_arguments_hash,
                          public_envelope_matches_call)  # noqa: E402


def identity(call_id, verification_id, value):
    return VerificationIdentity.from_call(
        "sample_size", {"verification_id": verification_id, "value": value},
        {"n": value}, call_id,
    )


passed = failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL")
        failed += 1


def node_json_roundtrip(value):
    """Return a value after the JavaScript transport's parse/stringify pass."""
    completed = subprocess.run(
        [
            "node", "-e",
            "const fs=require('fs');"
            "const value=JSON.parse(fs.readFileSync(0,'utf8'));"
            "process.stdout.write(JSON.stringify(value));",
        ],
        input=json.dumps(value, separators=(",", ":"), allow_nan=False),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


ledger = VerificationLedger()
failed_env = envelope_from_verdict(
    identity("call-a", "analysis-a", 1),
    GateVerdict(passed=False, failures=["bad"]),
)
ledger.record(failed_env, lineage_id="lineage-a")
other_env = envelope_from_verdict(
    identity("call-b", "analysis-b", 2), GateVerdict(passed=True),
)
ledger.record(other_env, lineage_id="lineage-b")
check("unrelated_pass_does_not_clear_failure", len(ledger.unresolved()) == 1)

corrected = envelope_from_verdict(
    identity("call-c", "analysis-c", 3), GateVerdict(passed=True),
)
ledger.record(corrected, lineage_id="lineage-a")
check("same_analysis_correction_clears_failure", not ledger.unresolved())
check("old_call_remains_failed", ledger.for_call("call-a").status == VerificationStatus.FAILED)

partial = envelope_from_verdict(
    identity("call-d", "analysis-d", 4),
    GateVerdict(passed=True, blocked=["manual check"]),
)
check("partial_is_presentable_with_disclosure",
      partial.status == VerificationStatus.PASS_PARTIAL and partial.presentable)
check("argument_hash_excludes_verification_id",
      identity("x", "one", 9).args_hash == identity("y", "two", 9).args_hash)
check("nonfinite_output_can_be_identity_hashed",
      bool(VerificationIdentity.from_call("sample_size", {}, {"n": float("nan")}, "nan").result_hash))
check("integral_float_and_integer_share_canonical_identity",
      content_hash({"sd": 2.0, "allocation": [-0.0, 3.0]}) ==
      content_hash({"sd": 2, "allocation": [0, 3]}))
wire_float_payload = {
    "small": 2.0,
    "negative_zero": -0.0,
    "unsafe_integer_float": 1.0000000000000001e18,
    "fixed_notation_upper_edge": 9.999999999999999e20,
    "exponential_notation_edge": 1e21,
}
wire_float_roundtrip = node_json_roundtrip(wire_float_payload)
check("integral_floats_match_node_wire_canonical_identity",
      content_hash(wire_float_payload) == content_hash(wire_float_roundtrip)
      and canonical_value(wire_float_payload) == {
          "small": 2,
          "negative_zero": 0,
          "unsafe_integer_float": 1000000000000000100,
          "fixed_notation_upper_edge": 999999999999999900000,
          "exponential_notation_edge": 1e21,
      })
adjacent_huge_integers = [1000000000000000128, 1000000000000000129]
adjacent_huge_roundtrip = node_json_roundtrip(adjacent_huge_integers)
check("adjacent_huge_python_integers_remain_distinct_in_python_identity",
      canonical_value(adjacent_huge_integers) == adjacent_huge_integers
      and content_hash(adjacent_huge_integers[0])
      != content_hash(adjacent_huge_integers[1])
      and adjacent_huge_roundtrip[0] == adjacent_huge_roundtrip[1])
bound = identity("bound", "analysis-bound", 9)
check("identity_match_recomputes_hashes",
      identity_matches_call(bound, "sample_size", {"value": 9}, {"n": 9}))
check("identity_match_rejects_tampered_result",
      not identity_matches_call(bound, "sample_size", {"value": 9}, {"n": 10}))
provenance = {"input_hashes": {"ipd_file": "abc"}}
provenance_bound = VerificationIdentity.from_call(
    "indirect_compare", {"method": "maic"}, {"estimate": 1},
    "provenance-bound", provenance,
)
check("identity_match_binds_provenance",
      identity_matches_call(provenance_bound, "indirect_compare",
                            {"method": "maic"}, {"estimate": 1}, provenance))
check("identity_match_rejects_tampered_provenance",
      not identity_matches_call(
          provenance_bound, "indirect_compare", {"method": "maic"},
          {"estimate": 1}, {"input_hashes": {"ipd_file": "changed"}}))
missing_provenance = dict(provenance_bound.__dict__)
missing_provenance.pop("provenance_hash")
check("identity_match_requires_provenance_hash",
      not identity_matches_call(
          missing_provenance, "indirect_compare", {"method": "maic"},
          {"estimate": 1}, provenance))

conflict = envelope_from_verdict(
    VerificationIdentity.from_call(
        "randomize", {"verification_id": "analysis-a"}, {"n": 2}, "cross-tool"
    ),
    GateVerdict(passed=True),
)
try:
    ledger.record(conflict)
    conflict_rejected = False
except ValueError:
    conflict_rejected = True
check("cross_tool_identity_reuse_rejected", conflict_rejected)

def report_envelope(tool, args, result, analysis_id):
    bound_args = dict(args, verification_id=analysis_id)
    report_identity = VerificationIdentity.from_call(
        tool, bound_args, result, "call-" + analysis_id,
    )
    return envelope_from_verdict(
        report_identity, GateVerdict(passed=True, blocked=["manual check"]),
    )


report_args = {"verification_id": "ignored", "endpoint_type": "binary",
               "ipd_file": "/private/source.csv"}
report_result = {"n": 30, "output_dir": "/private/results"}
report_env = report_envelope("sample_size", report_args, report_result,
                             "analysis-report")
report = canonical_report(
    "sample_size",
    report_args, report_result, report_env.to_dict(),
)
check("canonical_report_uses_public_not_raw_commitment",
      report_env.identity.analysis_id in report
      and report_env.identity.result_hash not in report
      and "public_result_hash" in report)
check("partial_report_title_matches_partial_status",
      report.startswith("# Partially verified experiment-design result"))
check("canonical_report_redacts_paths",
      "/private/source.csv" not in report and "/private/results" not in report)

full_args = {"endpoint_type": "binary", "study_type": "poc",
             "design": "single_arm", "null_param": 0.2, "alt_param": 0.4}
full_result = {"results": [{"n_total": 30}]}
full_identity = VerificationIdentity.from_call(
    "sample_size", full_args, full_result, "call-analysis-full")
full_env = envelope_from_verdict(full_identity, GateVerdict(passed=True))
full_report = canonical_report(
    "sample_size", full_args, full_result, full_env.to_dict())
check("verified_report_title_requires_no_blocked_checks",
      full_report.startswith("# Verified experiment-design result"))

single_assumptions = public_arguments_view("simulate_design", {"config": {
    "endpoint_type": "binary", "study_type": "poc", "design": "single_arm",
    "null_param": 0.2, "alt_param": 0.4,
    "prior": {"a": 2, "b": 3},
}})
check("canonical_assumptions_retain_result_affecting_prior",
      single_assumptions.get("config", {}).get("prior") == {"a": 2, "b": 3})

master_assumptions = public_arguments_view("master_simulate", {"config": {
    "master_design_type": "basket", "endpoint_type": "binary",
    "n_subgroups": 2, "null_params": [0.2, 0.2], "alt_params": [0.4, 0.4],
    "borrowing_method": "full_bhm", "phase": "phase2", "n_interims": 2,
    "tau_prior": {"type": "half_normal", "params": {"scale": 1}},
    "homogeneity_prior": 0.5, "response_prior": 0.5,
    "fwer_control": "bonferroni",
}})
master_config = master_assumptions.get("config", {})
check("canonical_assumptions_retain_master_method_and_priors",
      master_config.get("borrowing_method") == "full_bhm"
      and master_config.get("phase") == "phase2"
      and master_config.get("n_interims") == 2
      and master_config.get("tau_prior") == {
          "type": "half_normal", "params": {"scale": 1}}
      and master_config.get("fwer_control") == "bonferroni")
try:
    canonical_report("sample_size", report_args, {"n": 31}, report_env.to_dict())
    mismatch_rejected = False
except ValueError:
    mismatch_rejected = True
check("canonical_report_rejects_identity_mismatch", mismatch_rejected)
artifact_result = {"artifacts": ["/private/run/weights.csv", "/private/run/diagnostics.json"]}
artifact_env = report_envelope("indirect_compare", {}, artifact_result,
                               "analysis-artifact")
artifact_report = canonical_report(
    "indirect_compare", {},
    artifact_result, artifact_env.to_dict(),
)
check("canonical_report_redacts_artifact_lists",
      "/private/run" not in artifact_report and '"length": 2' in artifact_report)
embedded_result = {"log": "Saved: /private/run/output.csv",
                   "../private/source.csv": 3,
                   "message": r"See \\server\share\result.csv"}
embedded_env = report_envelope("master_simulate", {}, embedded_result,
                               "analysis-embedded")
embedded_path_report = canonical_report(
    "master_simulate", {}, embedded_result, embedded_env.to_dict(),
)
check("canonical_report_redacts_embedded_paths",
      "/private/run/output.csv" not in embedded_path_report
      and "../private/source.csv" not in embedded_path_report
      and "server\\\\share" not in embedded_path_report)
stat_result = {"log_hr": -0.6931, "se_log_hr": 0.2, "log": "private diagnostic"}
stat_env = report_envelope("simulate_design", {}, stat_result, "analysis-log-fields")
stat_report = canonical_report("simulate_design", {}, stat_result, stat_env.to_dict())
check("canonical_report_drops_impossible_root_log_scale_aliases",
      '"log_hr": -0.6931' not in stat_report and '"se_log_hr": 0.2' not in stat_report
      and "private diagnostic" not in stat_report)
meta_args = {"endpoint_type": "continuous_single", "studies": [
    {"mean": 1.2, "sd": 0.4, "n": 20, "mrn": "P-SECRET", "email": "x@example.test"},
]}
meta_result = {"estimate": 1.2}
meta_env = report_envelope("meta_analyze", meta_args, meta_result, "analysis-meta-private")
meta_report = canonical_report("meta_analyze", meta_args, meta_result, meta_env.to_dict())
check("canonical_report_drops_unknown_study_identifiers",
      "P-SECRET" not in meta_report and "x@example.test" not in meta_report
      and '"studies": {' in meta_report and '"length": 1' in meta_report
      and '"mean": 1.2' not in meta_report)
tte_meta_args = {"endpoint_type": "time_to_event", "studies": [
    {"hr": 0.7, "ci_lower": 0.5, "ci_upper": 0.98, "source_ci_level": 0.90},
]}
tte_meta_result = {"estimate": -0.3}
tte_meta_env = report_envelope("meta_analyze", tte_meta_args, tte_meta_result,
                               "analysis-meta-ci-level")
tte_meta_report = canonical_report(
    "meta_analyze", tte_meta_args, tte_meta_result, tte_meta_env.to_dict(),
)
check("canonical_report_summarizes_sensitive_study_records",
      '"source_ci_level": 0.9' not in tte_meta_report
      and '"studies": {' in tte_meta_report and '"length": 1' in tte_meta_report)
random_args = {"n": 2, "arms": ["participant-P001", "participant-P002"],
               "method": "simple", "seed": 42}
random_result = {
    "n": 2,
    "arms": ["participant-P001", "participant-P002"],
    "counts": {"participant-P001": 1, "participant-P002": 1},
    "assignment": [{"unit": 1, "arm": "participant-P001"}],
    "seed": 42,
}
random_env = report_envelope("randomize", random_args, random_result,
                             "analysis-random-private-arms")
random_report = canonical_report(
    "randomize", random_args, random_result, random_env.to_dict(),
)
check("canonical_report_pseudonymizes_arm_labels",
      "participant-P001" not in random_report
      and "participant-P002" not in random_report
      and '"arm_1"' in random_report and '"arm_2"' in random_report)
label_args = {"method": "bucher", "comparisons": [{
    "estimate_ab": 0.2, "se_ab": 0.1, "estimate_cb": 0.1, "se_cb": 0.1,
    "treatment_a": "Patient Jane", "treatment_c": "MRN-4242",
    "common_comparator": "Named Comparator",
}]}
label_result = {"treatment": "Patient Jane", "comparator": "MRN-4242",
                "custom_note": "MRN-4242"}
label_env = report_envelope("indirect_compare", label_args, label_result,
                            "analysis-freeform-labels")
label_report = canonical_report(
    "indirect_compare", label_args, label_result, label_env.to_dict())
check("canonical_report_redacts_freeform_labels",
      "Patient Jane" not in label_report and "MRN-4242" not in label_report
      and "Named Comparator" not in label_report)

bucher_result = {"method": "bucher", "comparisons": [{
    "contrast": "Patient Jane vs MRN-4242",
    "connection_treatment": "Named Comparator",
    "connection_role": "private derived role",
    "estimate": 0.1, "se": 0.2, "lower": -0.3, "upper": 0.5,
    "natural_estimate": 1.105, "natural_lower": 0.741,
    "natural_upper": 1.649, "effect_measure": "log_odds_ratio",
    "unknown_numeric_alias": 999,
}]}
bucher_public = privacy_safe_view("indirect_compare", bucher_result)
check("bucher_public_dto_drops_derived_labels_and_unknown_fields",
      "Patient Jane" not in str(bucher_public)
      and "MRN-4242" not in str(bucher_public)
      and "Named Comparator" not in str(bucher_public)
      and "unknown_numeric_alias" not in str(bucher_public)
      and bucher_public.get("comparisons", [{}])[0].get("estimate") == 0.1
      and bucher_public.get("comparisons", [{}])[0].get("se") == 0.2)

maic_result = {
    "method": "maic",
    "weight_summary": {
        "n": 20, "sum": 19.5, "mean": 0.975, "min": 0.2,
        "q25": 0.7, "median": 0.9, "q75": 1.1, "max": 2.3,
    },
    "balance": [{
        "covariate": "MRN-derived-age-band", "target_mean": 0.6,
        "source_unweighted_mean": 0.4, "source_weighted_mean": 0.59,
        "unweighted_gap": -0.2, "weighted_gap": -0.01,
    }],
    "ess": [{"arm": "Secret Arm", "n": 20, "weight_sum": 19.5, "ess": 15.2}],
    "arm_summary": [{
        "treatment": "Secret Arm", "weighted_events": 4.2,
        "weight_sum": 19.5, "weighted_rate": 0.215,
    }],
    "source_effect": [{
        "contrast": "Secret A vs Secret B", "effect": -0.3, "se": 0.12,
        "ci_lower": -0.535, "ci_upper": -0.065,
        "natural_estimate": 0.741, "natural_lower": 0.586,
        "natural_upper": 0.937, "effect_measure": "odds_ratio",
        "bootstrap_replicates_requested": 500,
        "bootstrap_replicates_used": 498,
    }],
    "anchored_result": {"estimate": -0.1, "se": 0.2, "lower": -0.5, "upper": 0.3},
}
maic_public = privacy_safe_view("indirect_compare", maic_result)
check("maic_public_dto_is_numeric_complete_label_free_and_idempotent",
      privacy_safe_view("indirect_compare", maic_public) == maic_public
      and "MRN-derived-age-band" not in str(maic_public)
      and "Secret Arm" not in str(maic_public)
      and "Secret A vs Secret B" not in str(maic_public)
      and maic_public.get("balance", [{}])[0].get("weighted_gap") == -0.01
      and maic_public.get("ess", [{}])[0].get("ess") == 15.2
      and maic_public.get("source_effect", [{}])[0].get("natural_estimate") == 0.741)

public_result = privacy_safe_view("randomize", random_result)
public_envelope = random_env.to_dict()
public_envelope["report"] = random_report
public_envelope["public_result_hash"] = content_hash(public_result)
public_envelope["report_hash"] = content_hash(random_report)
public_envelope["identity"].pop("result_hash", None)
public_envelope["identity"]["public_args_hash"] = public_arguments_hash(
    "randomize", random_args,
)
public_envelope["identity"].pop("args_hash", None)
check("public_envelope_binds_safe_result_and_report",
      public_envelope_matches_call(
          public_envelope, "randomize", random_args, public_result, {}))
check("public_envelope_rejects_tampered_report",
      not public_envelope_matches_call(
          dict(public_envelope, report=random_report + " altered"),
          "randomize", random_args, public_result, {}))
fresh_envelope = envelope_from_verdict(
    PublicVerificationIdentity.from_dict(public_envelope["identity"]),
    GateVerdict(
        passed=True,
        checks={"fresh:regression": True},
        blocked=["manual check"],
    ),
)
fresh_report = canonical_public_report(
    "randomize", random_args, public_result, fresh_envelope.to_dict(),
    public_envelope, {},
)
check("canonical_public_report_renders_fresh_combined_gate",
      "scientific_validation=pass" in fresh_report
      and random_env.identity.result_hash not in fresh_report)
tampered_envelope = failed_env.to_dict()
tampered_envelope["presentable"] = True
try:
    canonical_report("sample_size", {"value": 1}, {"n": 1}, tampered_envelope)
    promoted_failure_rejected = False
except ValueError:
    promoted_failure_rejected = True
check("presentable_flag_cannot_promote_failed_envelope", promoted_failure_rejected)
check("only_structured_clarification_is_accepted",
      is_elicitation_message('CLARIFICATION_REQUEST {"fields":["endpoint_type"]}')
      and is_elicitation_message(
          'CLARIFICATION_REQUEST {"fields":["alpha_sidedness"]}')
      and not is_elicitation_message("What does n_total=30 mean?")
      and not is_elicitation_message("What is the endpoint?"))
failed_summary = failed_env.public_summary()
check("failed_public_summary_is_value_free",
      failed_summary["failures"] == ["verification_failed"]
      and not failed_summary["blocked"] and not failed_summary["notes"]
      and "bad" not in str(failed_summary))
check("public_summary_omits_raw_hash_and_keeps_report",
      "result_hash" not in report_env.public_summary(report)
      and report_env.public_summary(report)["report"] == report)

foreign_numeric = privacy_safe_view("sample_size", {
    "results": [{
        "design": 7,
        "n_total": 30,
        "estimate": 991.25,
        "weighted_gap": -8.2,
        "secret_numeric": 4242,
    }],
    "balance": [{"n_total": 999}],
})
check("exact_path_schema_drops_foreign_numeric_fields_and_enum_type_confusion",
      foreign_numeric == {"results": [{"n_total": 30}]})

meta_public_args = public_arguments_view("meta_analyze", meta_args)
meta_public_result = privacy_safe_view("meta_analyze", {
    "estimate": 1.2, "study_effects": [1.1, 1.3],
    "study_variances": [0.1, 0.2],
})
check("argument_and_result_schemas_are_separate_and_collection_safe",
      meta_public_args.get("studies") == {"available": True, "length": 1}
      and "estimate" not in meta_public_args
      and meta_public_result == {
          "estimate": 1.2,
          "study_effects": {"available": True, "length": 2},
          "study_variances": {"available": True, "length": 2},
      })

oracle_result_a = {"n": 30, "private_note": "secret candidate A"}
oracle_result_b = {"n": 30, "private_note": "secret candidate B"}
oracle_env_a = report_envelope(
    "sample_size", {}, oracle_result_a, "analysis-oracle",
)
oracle_env_b = report_envelope(
    "sample_size", {}, oracle_result_b, "analysis-oracle",
)
oracle_report_a = canonical_report(
    "sample_size", {}, oracle_result_a, oracle_env_a.to_dict(),
)
oracle_report_b = canonical_report(
    "sample_size", {}, oracle_result_b, oracle_env_b.to_dict(),
)
check("dropped_secret_does_not_change_public_deterministic_commitment",
      oracle_env_a.identity.result_hash != oracle_env_b.identity.result_hash
      and oracle_report_a == oracle_report_b
      and "secret candidate" not in oracle_report_a)

secret_verdict = GateVerdict(
    passed=True,
    checks={"secret/path/PATIENT-42": True},
    blocked=["secret limitation /private/source.csv"],
    notes=["secret note MRN-4242"],
)
secret_env = envelope_from_verdict(
    VerificationIdentity.from_call(
        "sample_size", {"verification_id": "analysis-secret-codes"},
        {"n": 30}, "call-secret-codes",
    ),
    secret_verdict,
)
secret_report = canonical_report(
    "sample_size", {}, {"n": 30}, secret_env.to_dict(),
)
secret_summary = secret_env.public_summary(secret_report)
check("verification_text_is_collapsed_to_fixed_public_codes",
      "PATIENT-42" not in secret_report and "/private/source.csv" not in secret_report
      and "MRN-4242" not in str(secret_summary)
      and secret_summary["blocked"] == ["additional_manual_review_required"]
      and secret_summary["notes"] == ["additional_verification_note"])

raw_public_oracle = random_env.to_dict()
raw_public_oracle.update({
    "report": random_report,
    "report_hash": content_hash(random_report),
    "public_result_hash": content_hash(public_result),
})
check("public_envelope_rejects_raw_result_commitment",
      not public_envelope_matches_call(
          raw_public_oracle, "randomize", random_args, public_result, {}))

private_path_a = {
    "method": "maic", "ipd_file": "/private/patient-a.csv",
    "targets_file": "/private/target-a.csv",
}
private_path_b = {
    "method": "maic", "ipd_file": "/private/patient-b.csv",
    "targets_file": "/private/target-b.csv",
}
check("public_argument_commitment_is_not_a_private_path_oracle",
      public_arguments_hash("indirect_compare", private_path_a)
      == public_arguments_hash("indirect_compare", private_path_b))
private_strata_a = {
    "n": 2, "method": "stratified", "strata": ["patient-a", "patient-b"],
}
private_strata_b = {
    "n": 2, "method": "stratified", "strata": ["patient-c", "patient-d"],
}
check("public_argument_commitment_is_not_a_strata_oracle",
      public_arguments_hash("randomize", private_strata_a)
      == public_arguments_hash("randomize", private_strata_b))

case_projection = privacy_safe_view("sample_size", {
    "RESULTS": [{"N_TOTAL": 30}],
})
check("noncanonical_case_aliases_are_rejected",
      case_projection == {})
check("dispatcher_impossible_root_fields_are_rejected",
      privacy_safe_view("sample_size", {"n": 37}) == {}
      and privacy_safe_view("simulate_design", {
          "log_hr": -0.2, "se_log_hr": 0.1, "delta": 0.3,
      }) == {})
check("scalar_oc_budget_is_preserved_at_its_exact_union_path",
      public_arguments_view("simulate_design", {"n_oc": 25}) == {"n_oc": 25}
      and privacy_safe_view("simulate_design", {"n_oc_used": 25})
      == {"n_oc_used": 25})
check("collection_summary_rejects_boolean_and_negative_lengths",
      privacy_safe_view("meta_analyze", {
          "study_effects": {"available": True, "length": True},
          "study_variances": {"available": True, "length": -1},
      }) == {})
bad_count_projection = privacy_safe_view("randomize", {
    "n": 4,
    "counts": {"A": True, "B": float("inf"), "C": 2},
})
check("randomize_counts_reject_boolean_and_nonfinite_values",
      bad_count_projection.get("counts") == {"arm_3": 2})
random_artifact_public = privacy_safe_view("randomize", {
    "n": 2, "counts": {"A": 1, "B": 1},
    "private_assignment_artifact": "/private/assignments.json",
})
check("randomize_public_dto_is_idempotent_with_private_artifact_summary",
      privacy_safe_view("randomize", random_artifact_public) == random_artifact_public)
mixed_design = privacy_safe_view("factorial_design", {
    "design": [{"Private Factor": 1}, {"Private Factor": "not numeric"}],
})
check("design_projection_skips_drop_sentinel_values",
      mixed_design == {"design": [{"factor_1": 1}, {}]}
      and "object at" not in str(mixed_design))
factorial_views = []
for factor_count in range(1, 13):
    # Reverse insertion order demonstrates that arbitrary private names retain
    # a deterministic lexical projection before canonical aliases are ordered
    # numerically on every subsequent projection.
    raw_design = [{
        f"Private Factor {index:02d}": index
        for index in range(factor_count, 0, -1)
    }]
    public_design = privacy_safe_view("factorial_design", {"design": raw_design})
    factorial_views.append(public_design)
check("factorial_public_dto_is_idempotent_for_one_to_twelve_factors",
      all(privacy_safe_view("factorial_design", view) == view
          for view in factorial_views)
      and factorial_views[9]["design"][0]["factor_10"] == 10
      and factorial_views[11]["design"][0]["factor_12"] == 12)

ten_factor_args = {"n_factors": 10, "fraction": 5}
ten_factor_raw = {
    "n_factors": 10,
    "fraction": 5,
    "n_runs": 1,
    "design": [{f"Private Factor {index:02d}": index for index in range(1, 11)}],
}
ten_factor_env = report_envelope(
    "factorial_design", ten_factor_args, ten_factor_raw, "analysis-ten-factor",
)
ten_factor_report = canonical_report(
    "factorial_design", ten_factor_args, ten_factor_raw, ten_factor_env.to_dict(),
)
ten_factor_public = privacy_safe_view("factorial_design", ten_factor_raw)
ten_factor_server_envelope = ten_factor_env.to_dict()
ten_factor_server_envelope["report"] = ten_factor_report
ten_factor_server_envelope["public_result_hash"] = content_hash(ten_factor_public)
ten_factor_server_envelope["report_hash"] = content_hash(ten_factor_report)
ten_factor_server_envelope["identity"].pop("result_hash", None)
ten_factor_server_envelope["identity"].pop("args_hash", None)
ten_factor_server_envelope["identity"]["public_args_hash"] = public_arguments_hash(
    "factorial_design", ten_factor_args,
)
ten_factor_rebuilt_report = canonical_public_report(
    "factorial_design", ten_factor_args, ten_factor_public,
    ten_factor_server_envelope, ten_factor_server_envelope, {},
)
check("ten_factor_stop_rebuild_preserves_server_report_and_hash",
      ten_factor_rebuilt_report == ten_factor_report
      and ten_factor_server_envelope["public_result_hash"]
      == content_hash(privacy_safe_view("factorial_design", ten_factor_public))
      and public_envelope_matches_call(
          ten_factor_server_envelope, "factorial_design", ten_factor_args,
          ten_factor_public, {},
      ))

# The MCP boundary serializes 1.0 as 1, so the verifier hashes and renders an
# integral float exactly as the host does when it reads the same argument
# straight from JSON. Otherwise the Stop hook rejects a correct report.
whole_number_float_args = {
    "n_factors": 2, "levels": 2.0, "center_points": 0.0,
}
whole_number_int_args = {
    "n_factors": 2, "levels": 2, "center_points": 0,
}
whole_number_result = {
    "type": "full_factorial", "n_factors": 2, "levels": [2, 2], "n_runs": 4,
    "replicates": 1,
    "design": [{"A": a, "B": b} for a in (-1, 1) for b in (-1, 1)],
}
whole_number_reports = []
for whole_number_args in (whole_number_float_args, whole_number_int_args):
    whole_number_identity = VerificationIdentity.from_call(
        "factorial_design", {**whole_number_args, "verification_id": "analysis-whole"},
        whole_number_result, "call-whole",
    )
    whole_number_envelope = {
        "identity": {
            "analysis_id": whole_number_identity.analysis_id,
            "call_id": whole_number_identity.call_id,
            "tool": whole_number_identity.tool,
            "args_hash": whole_number_identity.args_hash,
            "result_hash": whole_number_identity.result_hash,
            "provenance_hash": whole_number_identity.provenance_hash,
        },
        "status": "VERIFIED", "presentable": True, "checks": {}, "failures": [],
        "blocked": [], "notes": [],
    }
    whole_number_reports.append(canonical_report(
        "factorial_design", whole_number_args, whole_number_result,
        whole_number_envelope, {},
    ))
check("integral_float_and_int_arguments_render_one_canonical_report",
      whole_number_reports[0] == whole_number_reports[1]
      and '"levels": 2,' in whole_number_reports[0]
      and "2.0" not in whole_number_reports[0])

# Integral floats below 1e21 use JavaScript's fixed-notation wire value, even
# above 2**53; at 1e21 JSON.stringify switches to exponential notation. Native
# Python integers never take this path and retain arbitrary precision.
check("integral_floats_follow_javascript_fixed_notation_boundary",
      canonical_value(2.0) == 2
      and isinstance(canonical_value(2.0), int)
      and canonical_value(float(2 ** 53)) == 2 ** 53
      and isinstance(canonical_value(float(2 ** 53)), int)
      and canonical_value(float(2 ** 53 + 2)) == 2 ** 53 + 2
      and isinstance(canonical_value(float(2 ** 53 + 2)), int)
      and canonical_value(1.0000000000000001e18) == 1000000000000000100
      and isinstance(canonical_value(1.0000000000000001e18), int)
      and isinstance(canonical_value(1e21), float))

unsafe_float_args = {
    "endpoint_type": "continuous", "study_type": "poc",
    "design": "single_arm", "null_param": 0.0,
    "alt_param": 1.0000000000000001e18,
    "sd": 1.0000000000000001e18,
}
unsafe_float_wire_args = node_json_roundtrip(unsafe_float_args)
unsafe_float_result = {
    "results": [{
        "design": "single_arm", "n_total": 2,
        "power_target": 0.8, "power_achieved": 0.9,
    }],
}
unsafe_float_reports = []
for report_args_value in (unsafe_float_args, unsafe_float_wire_args):
    report_env_value = report_envelope(
        "sample_size", report_args_value, unsafe_float_result,
        "analysis-unsafe-float",
    )
    unsafe_float_reports.append(canonical_report(
        "sample_size", report_args_value, unsafe_float_result,
        report_env_value.to_dict(), {},
    ))
check("unsafe_integral_float_node_roundtrip_preserves_report_and_argument_hash",
      public_arguments_hash("sample_size", unsafe_float_args)
      == public_arguments_hash("sample_size", unsafe_float_wire_args)
      and unsafe_float_reports[0] == unsafe_float_reports[1]
      and "1000000000000000100" in unsafe_float_reports[0]
      and "1000000000000000128" not in unsafe_float_reports[0])

simulation_completeness = privacy_safe_view("simulate_design", {
    "B_used": 250,
    "ppos": {
        "ppos": 0.73,
        "p2_post_ci": [0.21, 0.55],
        "cond_power_mean": 0.73,
        "cond_power_draws": [0.1, 0.9],
        "private_label": "participant-7",
    },
})
check("predictive_probability_summary_is_preserved_without_draws_or_labels",
      simulation_completeness == {
          "b_used": 250,
          "ppos": {
              "ppos": 0.73,
              "p2_post_ci": [0.21, 0.55],
              "cond_power_mean": 0.73,
          },
      }
      and public_arguments_view("simulate_design", {"B_oc": 250}) == {"b_oc": 250})

check("fixed_method_enums_match_engine_outputs",
      privacy_safe_view("ab_test", {"metric": "mean"}) == {"metric": "mean"}
      and privacy_safe_view("rsm_design", {"alpha_type": "face"})
      == {"alpha_type": "face"}
      and privacy_safe_view("meta_analyze", {
          "inference_method": "DL tau2 with modified HKSJ t inference",
      }) == {"inference_method": "hksj"})

master_completeness = privacy_safe_view("master_simulate", {"result": {
    "oc_table": [{"metric": "FWER (%)", "value": 2.5}],
    "fwer_table": [{"scenario": "Global null", "fwer": 2.5}],
    "sample_size_table": [{"design": "Umbrella (MAMS)", "expected_n": 84}],
    "arm_results": [{
        "arm": 1, "true_effect": -0.4, "effect_scale": "log_hazard_ratio",
        "truly_active": True, "reject_rate": 81.2,
    }],
    "typeI_table": [{"arm": 1, "type_I_error": 2.5}],
    "alloc_df": [{"private-arm-name": 0.5}],
    "decision_matrix": [[True, False]],
    "multiplicity_method": "Bonferroni final; futility-only interim stopping",
    "final_alpha_per_arm": 0.0125,
    "interim_stopping_applied": True,
    "boundary_source": "approximation",
}})
check("master_aggregate_tables_are_complete_and_label_safe",
      master_completeness == {"result": {
          "oc_table": [{"metric": "fwer_percent", "value": 2.5}],
          "fwer_table": [{"scenario": "global_null", "fwer": 2.5}],
          "sample_size_table": [{"design": "umbrella_mams", "expected_n": 84}],
          "arm_results": [{
              "arm": 1, "true_effect": -0.4,
              "effect_scale": "log_hazard_ratio", "truly_active": True,
              "reject_rate": 81.2,
          }],
          "type_i_table": [{"arm": 1, "type_i_error": 2.5}],
          "alloc_df": {"available": True, "length": 1},
          "decision_matrix": {"available": True, "length": 1},
          "multiplicity_method": "bonferroni_final_futility_interim",
          "final_alpha_per_arm": 0.0125,
          "interim_stopping_applied": True,
          "boundary_source": "approximation",
      }}
      and privacy_safe_view("master_simulate", master_completeness)
      == master_completeness)

check("master_decision_reasons_collapse_to_fixed_codes",
      privacy_safe_view("master_simulate", {"result": {
          "oc_table": [{"decision": "No-Go (futility at Stage 1)"}],
      }}) == {"result": {"oc_table": [{"decision": "no-go"}]}})

fixed_regression_ok = {
    "all_ok": True, "suite_count": 5, "passed_suite_count": 5,
    "failed_suite_count": 0,
    "checks": {"declared_all_ok": True, "complete_suite_set": True,
               "suite_records_valid": True, "expected_check_count": True},
}
fixed_public_provenance = {
    "engine_version": "test-version",
    "engine_fingerprint": "test-engine",
    "node_version": "test-node",
    "python_version": "test-python",
    "python_runtime_fingerprint": "test-python-runtime",
    "r_version": "test-r",
    "r_runtime_fingerprint": "test-r-runtime",
    "r_package_versions": {},
    "config_bound": True,
    "input_file_count": 0,
    "artifact_count": 0,
}
server_request = {
    "tool": "sample_size",
    "args": {"endpoint_type": "binary", "study_type": "poc",
             "design": "single_arm", "null_param": 0.2, "alt_param": 0.4},
    "result": {"results": [{"design": "single_arm", "n_total": 30,
                              "power_target": 0.8, "power_achieved": 0.82}],
               "private_note": "SECRET-RESULT-COMMITMENT"},
    "runtime_id": "analysis-public-success",
    "public_provenance": fixed_public_provenance,
    "regression": fixed_regression_ok,
}
verifier_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..",
                             "server_verify.py")


def server_verdict(request):
    completed = subprocess.run(
        [sys.executable, verifier_path], input=json.dumps(request),
        capture_output=True, text=True,
    )
    return completed.returncode, json.loads(completed.stdout)


success_rc, success_public = server_verdict(server_request)
failed_request = dict(server_request, runtime_id="analysis-public-failure",
                      regression={})
failed_rc, failed_public = server_verdict(failed_request)
check("server_success_and_withheld_envelopes_omit_raw_result_commitment",
      success_rc == 0 and success_public.get("presentable") is True
      and failed_rc == 0 and failed_public.get("presentable") is False
      and "result_hash" not in success_public.get("identity", {})
      and "result_hash" not in failed_public.get("identity", {})
      and "SECRET-RESULT-COMMITMENT" not in json.dumps(success_public)
      and "SECRET-RESULT-COMMITMENT" not in json.dumps(failed_public))

raw_digest = "4" * 64
oracular_request = {
    **server_request,
    "runtime_id": "analysis-oracular-provenance",
    "public_provenance": {
        **fixed_public_provenance,
        "input_hashes": {"ipd_file": raw_digest},
    },
}
oracular_rc, oracular_public = server_verdict(oracular_request)
check("server_verifier_rejects_oracular_public_provenance",
      oracular_rc != 0 and oracular_public == {
          "status": "INTERNAL_ERROR", "presentable": False,
          "failures": ["verification_internal_error"],
      } and raw_digest not in json.dumps(oracular_public))

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
