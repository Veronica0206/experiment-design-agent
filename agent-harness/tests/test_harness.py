#!/usr/bin/env python3
"""Integration-style tests for identity-safe harness result handling."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from audit import AuditLog  # noqa: E402
from final_report import canonical_report, privacy_safe_view  # noqa: E402
from gates import (GateVerdict, MANUAL_CHECKS, check_regression_tests,
                   combined_gate, design_checks_for)  # noqa: E402
from harness import ExperimentDesignHarness, SAFE_FAILURE_MESSAGE  # noqa: E402
from verification import (VerificationIdentity, content_hash,
                          envelope_from_verdict, public_arguments_hash,
                          public_failure_codes)  # noqa: E402


RT_OK = {
    "all_ok": True, "suite_count": 5, "passed_suite_count": 5,
    "failed_suite_count": 0,
    "checks": {"declared_all_ok": True, "complete_suite_set": True,
               "suite_records_valid": True, "expected_check_count": True},
}
RT_BAD = {
    "all_ok": False, "suite_count": 5, "passed_suite_count": 4,
    "failed_suite_count": 1,
    "checks": {"declared_all_ok": False, "complete_suite_set": True,
               "suite_records_valid": False, "expected_check_count": False},
}
GOOD_ARGS = {"verification_id": "analysis-a", "endpoint_type": "binary",
             "study_type": "poc", "design": "single_arm",
             "null_param": 0.2, "alt_param": 0.4}
BAD_ARGS = dict(GOOD_ARGS, null_param=0.4, alt_param=0.2)
SS_RESULT = {"results": [{"design": "single_arm", "n_total": 30,
                           "power_target": 0.8, "power_achieved": 0.82}]}


def text(value):
    return SimpleNamespace(type="text", text=value)


def tool(call_id, name, args):
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


def response(*blocks):
    return SimpleNamespace(content=list(blocks), stop_reason="end_turn")


class FakeAnthropic:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.messages = self

    def create(self, **_kwargs):
        return next(self._responses)


class FakeMCP:
    def __init__(self, results):
        self.results = results
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        value = self.results[name]
        result = value(arguments) if callable(value) else value
        if name in {"run_tests", "validate_config"}:
            return result
        raw = dict(result)
        if raw.pop("_test_passthrough", False):
            return raw
        private_resources = list(raw.pop("_test_private_resources", []))
        artifact_hashes = dict(raw.pop("_test_artifact_hashes", {}))
        public_provenance = {
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
            "artifact_count": len(artifact_hashes),
        }
        verdicts = list(design_checks_for(name, arguments, raw))
        verdicts.append(check_regression_tests(RT_OK))
        verdicts.append(GateVerdict(passed=True, blocked=list(MANUAL_CHECKS)))
        if name in {"simulate_design", "randomize"}:
            verdicts.append(GateVerdict(passed=True, checks={"reproducible": True}))
        verdict = combined_gate(*verdicts)
        identity = VerificationIdentity.from_call(
            name, arguments, raw, f"call-{len(self.calls)}", public_provenance)
        envelope = envelope_from_verdict(identity, verdict, public_provenance)
        envelope_data = envelope.to_dict()
        public = privacy_safe_view(name, raw)
        if envelope.presentable:
            report = canonical_report(
                name, arguments, raw, envelope_data, public_provenance,
            )
            envelope_data["report"] = report
            envelope_data["report_hash"] = content_hash(report)
            envelope_data["public_result_hash"] = content_hash(public)
            payload = dict(public)
        else:
            envelope_data["failures"] = public_failure_codes(envelope_data.get("checks"))
            envelope_data["blocked"] = []
            envelope_data["notes"] = []
            payload = {"error": "Result withheld because server-side verification failed."}
        # Match the real server boundary: canonical rendering uses the private
        # identity above, while only an allowlisted argument commitment crosses
        # into the model-visible response.
        envelope_data["identity"] = {
            "analysis_id": identity.analysis_id,
            "call_id": identity.call_id,
            "tool": identity.tool,
            "public_args_hash": public_arguments_hash(name, arguments),
            "provenance_hash": identity.provenance_hash,
        }
        payload["_verification"] = envelope_data
        payload["_provenance"] = public_provenance
        payload["_private_provenance"] = {
            "version": 2,
            "analysis_id": identity.analysis_id,
            "config_hash": "c" * 64,
            "input_hashes": {},
            "artifact_hashes": artifact_hashes,
            "artifact_handles": {
                resource["_test_handle"]: resource["_test_path"]
                for resource in private_resources
            },
        }
        if private_resources:
            payload["_private_resources"] = [
                {
                    **{key: value for key, value in resource.items()
                       if not key.startswith("_test_")},
                    "_meta": {
                        "experiment-design/private-artifact": {
                            "version": 2,
                            "analysis_id": identity.analysis_id,
                            "sha256": artifact_hashes[resource["name"]],
                            "handle": resource["_test_handle"],
                        },
                    },
                }
                for resource in private_resources
            ]
        return payload


passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {detail}")
        failed += 1


def run_case(responses, results):
    with tempfile.TemporaryDirectory() as directory:
        harness = ExperimentDesignHarness(
            anthropic_client=FakeAnthropic(responses),
            log_dir=Path(directory),
        )
        harness.mcp = FakeMCP(results)
        harness.audit = AuditLog(Path(directory), "test-run")
        stream = harness.run("design this study")
        events = []
        try:
            while True:
                events.append(next(stream))
        except StopIteration as done:
            return events, done.value, harness.mcp.calls


def finish(stream):
    try:
        while True:
            next(stream)
    except StopIteration as done:
        return done.value


events, result, calls = run_case(
    [response(text('CLARIFICATION_REQUEST {"fields":["power"]}'))],
    {},
)
check("structured_clarification_is_allowed",
      result.final_answer.startswith("CLARIFICATION_REQUEST") and not calls,
      result.final_answer)

configuration_report = "# Resolved configuration\n\n- alpha convention: one-sided"
events, result, calls = run_case(
    [response(tool("validate", "validate_config", GOOD_ARGS)),
     response(text("I would otherwise restate the defaults here."))],
    {"validate_config": {
        "valid": True,
        "resolved_config": {"alpha": 0.05},
        "configuration_report": configuration_report,
    }},
)
check("validate_only_turn_returns_exact_configuration_report",
      result.final_answer == configuration_report
      and result.stopped == "end_turn"
      and not result.gate_verdicts,
      result)

for label, unsafe_question in (
    ("numeric_question", "What does n_total=30 mean?"),
    ("identifier_question", "Which arm should unit 1 at SITE-ALPHA receive?"),
    ("natural_question", "What is the endpoint?"),
):
    events, result, calls = run_case([response(text(unsafe_question))], {})
    check(label + "_is_withheld", result.final_answer == SAFE_FAILURE_MESSAGE,
          result.final_answer)

events, result, calls = run_case(
    [response(text("What should we do, given that a dozen participants were enrolled?"))],
    {},
)
check("question_shaped_quantified_claim_is_withheld",
      result.final_answer == SAFE_FAILURE_MESSAGE and not calls, result.final_answer)

for label, claim in (
    ("word_number_claim", "A dozen participants should be enough."),
    ("roman_numeral_claim", "Use cohort XII for the analysis."),
    ("non_english_number_claim", "样本量为一百。"),
):
    events, result, calls = run_case([response(text(claim))], {})
    check(label + "_is_withheld", result.final_answer == SAFE_FAILURE_MESSAGE, result.final_answer)


events, result, calls = run_case(
    [response(tool("bad", "sample_size", BAD_ARGS)),
     response(text("The answer is 30 participants."))],
    {"sample_size": SS_RESULT, "run_tests": RT_OK},
)
check("failed_payload_never_emitted",
      not any(e.get("event") == "tool_result" and e.get("tool") == "sample_size"
              for e in events), events)
check("failed_payload_has_redacted_event",
      any(e.get("event") == "tool_result_withheld" for e in events), events)
failed_gate_events = [e for e in events if e.get("event") == "gate"]
check("failed_gate_diagnostics_are_value_free_codes",
      bool(failed_gate_events)
      and all(code == "verification_failed" or code.startswith("check_failed:")
              for code in failed_gate_events[0].get("failures", []))
      and not failed_gate_events[0].get("blocked")
      and not failed_gate_events[0].get("notes"), failed_gate_events)
check("failed_numeric_answer_withheld", "30" not in result.final_answer, result.final_answer)
check("failure_answer_is_exact_value_free_contract",
      result.final_answer == SAFE_FAILURE_MESSAGE, result.final_answer)


events, result, calls = run_case(
    [response(tool("malformed", "sample_size", GOOD_ARGS))],
    {"sample_size": {
        "_test_passthrough": True,
        "secret_result": 999,
        "_verification": {"presentable": True, "identity": "not-an-object"},
    }, "run_tests": RT_OK},
)
check("malformed_server_envelope_returns_exact_safe_failure",
      result.final_answer == SAFE_FAILURE_MESSAGE
      and result.stopped == "verify_failed"
      and not any(event.get("event") == "error" for event in events),
      (events, result))
check("malformed_server_envelope_payload_is_never_emitted",
      "999" not in str(events)
      and any(event.get("event") == "tool_result_withheld" for event in events)
      and any(event.get("event") == "gate"
              and event.get("verdict") == "INTERNAL_ERROR" for event in events),
      events)


events, result, calls = run_case(
    [response(tool("bad", "sample_size", BAD_ARGS)),
     response(tool("good", "sample_size", GOOD_ARGS)),
     response(text("Verified answer is ready."))],
    {"sample_size": SS_RESULT, "run_tests": RT_OK},
)
check("same_analysis_correction_clears_failure",
      result.stopped == "end_turn" and "# Partially verified experiment-design result" in result.final_answer
      and "Verified answer is ready." not in result.final_answer,
      result)
check("only_corrected_result_is_presentable",
      sum(e.get("event") == "tool_result" and e.get("tool") == "sample_size"
          for e in events) == 1, events)


different_id = dict(GOOD_ARGS, verification_id="analysis-b")
events, result, calls = run_case(
    [response(tool("bad", "sample_size", BAD_ARGS)),
     response(tool("good", "sample_size", different_id)),
     response(text("Use the earlier result."))],
    {"sample_size": SS_RESULT, "run_tests": RT_OK},
)
sample_calls = [arguments for name, arguments in calls if name == "sample_size"]
check("model_supplied_identity_cannot_prevent_runtime_bound_correction",
      result.stopped == "end_turn" and len(sample_calls) == 2 and
      sample_calls[0]["verification_id"] != sample_calls[1]["verification_id"], result)


rand = {"method": "block", "n": 4, "arms": ["control", "treatment"],
        "counts": {"control": 2, "treatment": 2}, "seed": 42,
        "strata": ["SITE-SECRET"] * 4,
        "assignment": [{"unit": 1, "arm": "control"},
                       {"unit": 2, "arm": "treatment"},
                       {"unit": 3, "arm": "control"},
                       {"unit": 4, "arm": "treatment"}]}
events, result, calls = run_case(
    [response(tool("rand", "randomize", {"n": 4, "verification_id": "rand-a"})),
     response(text("Randomization verified."))],
    {"randomize": rand, "run_tests": RT_OK},
)
check("randomize_has_server_replay_attestation",
      sum(name == "randomize" for name, _args in calls) == 1, calls)
check("raw_randomization_is_not_emitted_to_model_or_ui",
      "assignment" not in str(events) and "SITE-SECRET" not in str(events), events)


events, result, calls = run_case(
    [response(tool("stale-server-health", "sample_size", GOOD_ARGS)),
     response(text("ready"))],
    {"sample_size": SS_RESULT, "run_tests": RT_BAD},
)
stale_gate = next((event for event in events if event.get("event") == "gate"), {})
check("fresh_host_regression_gate_overrides_stale_server_pass",
      stale_gate.get("verdict") == "FAILED"
      and stale_gate.get("checks", {}).get("regression_suite") is False
      and result.final_answer == SAFE_FAILURE_MESSAGE
      and not any(event.get("event") == "tool_result" for event in events),
      (stale_gate, result))


with tempfile.TemporaryDirectory() as directory:
    resource = {
        "type": "resource_link",
        "name": "assignment.csv",
        "uri": "expdesign-artifact://analysis-test/12345678-1234-4123-8123-123456789abc",
        "annotations": {"audience": ["user"]},
        "_test_handle": "12345678-1234-4123-8123-123456789abc",
        "_test_path": "/private/managed/assignment.csv",
    }
    client = FakeAnthropic([
        response(tool("private-artifact", "sample_size", GOOD_ARGS)),
        response(text("ready")),
    ])
    harness = ExperimentDesignHarness(anthropic_client=client, log_dir=Path(directory))
    harness.mcp = FakeMCP({
        "sample_size": {
            **SS_RESULT,
            "_test_private_resources": [resource],
            "_test_artifact_hashes": {"assignment.csv": "a" * 64},
        },
        "run_tests": RT_OK,
    })
    harness.audit = AuditLog(Path(directory), "resource-run")
    resource_result = finish(harness.run("produce a private artifact"))
    check("private_resource_metadata_survives_chat_result",
          resource_result.private_resources
          and resource_result.private_resources[0].get("resource", {}).get("uri")
          == resource["uri"]
          and resource_result.private_resources[0].get("private_provenance", {}).get(
              "artifact_hashes", {}).get("assignment.csv") == "a" * 64,
          resource_result.private_resources)
    check("private_resource_uri_never_enters_model_history",
          resource["uri"] not in str(harness.messages), harness.messages)


with tempfile.TemporaryDirectory() as directory:
    client = FakeAnthropic([
        response(tool("retry-1", "sample_size", BAD_ARGS)),
        response(tool("retry-2", "sample_size", BAD_ARGS)),
        response(text("done")),
        response(tool("retry-3", "sample_size", GOOD_ARGS)),
        response(text("Verified answer is ready.")),
    ])
    harness = ExperimentDesignHarness(anthropic_client=client, log_dir=Path(directory))
    harness.mcp = FakeMCP({"sample_size": SS_RESULT, "run_tests": RT_OK})
    harness.audit = AuditLog(Path(directory), "retry-run")

    first = finish(harness.run("first turn"))
    second = finish(harness.run("second turn"))
    executed = [name for name, _args in harness.mcp.calls if name == "sample_size"]
    check("new_turn_does_not_inherit_unrelated_retry_lineage",
          first.final_answer == SAFE_FAILURE_MESSAGE
          and "# Partially verified experiment-design result" in second.final_answer
          and len(executed) == 3,
          (first, second, harness.mcp.calls))


with tempfile.TemporaryDirectory() as directory:
    client = FakeAnthropic([
        response(tool("first", "sample_size", GOOD_ARGS)),
        response(text("ready")),
        response(text("The previously verified n_total is 999 and power is 99%.")),
    ])
    harness = ExperimentDesignHarness(anthropic_client=client, log_dir=Path(directory))
    harness.mcp = FakeMCP({"sample_size": SS_RESULT, "run_tests": RT_OK})
    harness.audit = AuditLog(Path(directory), "cross-turn-run")
    first = finish(harness.run("first turn"))
    second = finish(harness.run("summarize the result"))
    check("cross_turn_numeric_prose_is_withheld",
          "# Partially verified experiment-design result" in first.final_answer
          and second.final_answer == SAFE_FAILURE_MESSAGE
          and "999" not in second.final_answer,
          (first, second))


with tempfile.TemporaryDirectory() as directory:
    harness = ExperimentDesignHarness(
        anthropic_client=FakeAnthropic([
            response(tool("first-report", "sample_size", GOOD_ARGS)),
            response(text("ready")),
        ]),
        log_dir=Path(directory),
    )
    harness.mcp = FakeMCP({"sample_size": SS_RESULT, "run_tests": RT_OK})
    harness.audit = AuditLog(Path(directory), "stale-report-run")
    first = finish(harness.run("design study A"))
    harness.client = FakeAnthropic([response(text(first.final_answer))])
    second = finish(harness.run("design unrelated study B"))
    check("prior_turn_exact_report_replay_is_withheld",
          second.final_answer == SAFE_FAILURE_MESSAGE,
          (first.final_answer, second.final_answer))


with tempfile.TemporaryDirectory() as directory:
    second_args = dict(GOOD_ARGS, alt_param=0.5)
    client = FakeAnthropic([
        response(tool("same-id", "sample_size", GOOD_ARGS)), response(text("ready")),
        response(tool("same-id", "sample_size", second_args)), response(text("ready")),
    ])
    harness = ExperimentDesignHarness(anthropic_client=client, log_dir=Path(directory))
    harness.mcp = FakeMCP({"sample_size": SS_RESULT, "run_tests": RT_OK})
    harness.audit = AuditLog(Path(directory), "duplicate-id-run")
    first = finish(harness.run("first turn"))
    second = finish(harness.run("second turn"))
    first_id = first.final_answer.split("verification_id: `", 1)[1].split("`", 1)[0]
    second_id = second.final_answer.split("verification_id: `", 1)[1].split("`", 1)[0]
    check("external_tool_ids_cannot_reuse_verification_identity",
          first_id != second_id, (first_id, second_id))


print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
