#!/usr/bin/env python3
"""Self-tests for the SubagentStop enforcement hook.

Each case builds the synchronous-ledger JSONL format and injects it directly
into the production policy. This keeps test-only transcript paths out of the
production trust boundary while preserving the exit-code contract.

Covers the audit's live-demonstrated failure modes:
  A  false allow  — failing sample_size laundered by a passing parallel sibling
  B  false block  — run_tests in the same message as the gated call must count
  C  escape abuse — a 3-call failing fan-out is ONE attempt, not an instant escape
  D  import path  — the installed policy resolves its own gate implementation
  E  trust source — ambient transcript paths cannot replace the injected ledger
  F  laundering   — a later unrelated tool must not supersede a failing design
  G  supersede    — a passing re-run of the SAME tool does clear its failure
  H  elicitation  — no gated calls -> allow

Run: python3 hooks/tests/test_enforce_verification.py   (from the suite root)
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "agent-harness"))
import verification_policy  # noqa: E402
from verification import (canonical_json, content_hash, domain_arguments,
                          public_arguments_hash)  # noqa: E402
from gates import MANUAL_CHECKS, combined_gate, design_checks_for  # noqa: E402
from final_report import canonical_report, join_reports, privacy_safe_view  # noqa: E402

SERVER = "mcp__experiment-design__"

GOOD_SS_INPUT = {"endpoint_type": "binary", "study_type": "poc",
                 "design": "single_arm", "null_param": 0.2, "alt_param": 0.4}
BAD_SS_INPUT = {"endpoint_type": "binary", "study_type": "poc",
                "design": "single_arm", "null_param": 0.4, "alt_param": 0.2}
SS_RESULT = {"results": [{"design": "single_arm", "n_total": 30,
                          "power_target": 0.8, "power_achieved": 0.82}]}
RAND_RESULT = {
    "method": "block", "n": 4, "arms": ["control", "treatment"],
    "counts": {"control": 2, "treatment": 2}, "seed": 42,
    "assignment": [
        {"unit": 1, "arm": "control"}, {"unit": 2, "arm": "treatment"},
        {"unit": 3, "arm": "control"}, {"unit": 4, "arm": "treatment"},
    ],
}
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
CALLS = {}


def tool_use_line(mid, tid, short, inp):
    CALLS[tid] = (short, inp)
    return {"message": {"id": mid, "role": "assistant", "content": [
        {"type": "tool_use", "id": tid, "name": SERVER + short, "input": inp}]}}


def tool_result_line(tid, result):
    short, inp = CALLS.get(tid, ("", {}))
    if short not in ("run_tests", "validate_config"):
        runtime_id = f"analysis-{tid}"
        raw_result = dict(result)
        provenance = {"engine_fingerprint": "test-engine", "config_hash": "test-config"}
        verdict = combined_gate(*design_checks_for(short, inp, raw_result))
        public_result = privacy_safe_view(short, raw_result)
        envelope = {
            "identity": {
                "analysis_id": runtime_id,
                "call_id": f"call-{tid}",
                "tool": short,
                "args_hash": content_hash(domain_arguments(inp)),
                "result_hash": content_hash(raw_result),
                "provenance_hash": content_hash(provenance),
            },
            "status": "PASS_PARTIAL" if verdict.passed else "RETRY_REQUIRED",
            "presentable": verdict.passed,
            "checks": dict(verdict.checks, reproducible=True,
                           **{"tests:all_ok_boolean": True,
                              "tests:complete_skill_set": True}),
            "failures": verdict.failures,
            "blocked": list(verdict.blocked) + list(MANUAL_CHECKS),
            "notes": verdict.notes,
        }
        if verdict.passed:
            envelope["report"] = canonical_report(
                short, inp, raw_result, envelope, provenance,
            )
            envelope["public_result_hash"] = content_hash(public_result)
            envelope["report_hash"] = content_hash(envelope["report"])
        envelope["identity"].pop("result_hash", None)
        envelope["identity"].pop("args_hash", None)
        envelope["identity"]["public_args_hash"] = public_arguments_hash(short, inp)
        result = dict(public_result) if isinstance(public_result, dict) else {"result": public_result}
        result["_verification"] = envelope
        result["_provenance"] = provenance
    return {"message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid,
         "content": [{"type": "text", "text": json.dumps(result)}]}]}}


def run_hook(lines, stdin_extra=None):
    stdin = {
        "agent_type": "experiment-designer",
        "session_id": "in-memory-session",
        "prompt_id": "in-memory-prompt",
        "hook_event_name": "Stop",
    }
    reports_by_call = {}
    call_keys = {}
    call_groups = {}
    for line_index, row in enumerate(lines):
        for block in row.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                short = str(block.get("name", ""))[len(SERVER):]
                args = block.get("input") or {}
                call_keys[block.get("id")] = (short, content_hash(domain_arguments(args)))
                call_groups[block.get("id")] = line_index
            if block.get("type") != "tool_result":
                continue
            content = block.get("content") or []
            try:
                payload = json.loads(content[0]["text"])
                envelope = payload.get("_verification")
                if envelope and envelope.get("presentable"):
                    reports_by_call[block.get("tool_use_id")] = envelope["report"]
            except Exception:
                pass
    latest = {}
    for call_id, report in reports_by_call.items():
        if call_id in call_keys:
            latest[call_keys[call_id]] = (call_groups[call_id], report)
    if latest:
        stdin["last_assistant_message"] = join_reports(
            report for _, report in sorted(latest.values())
        )
    stdin.update(stdin_extra or {})
    stdin["_expdesign_agent_scope"] = stdin.get("agent_type") or "experiment-designer"
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            verification_policy.enforce(
                stdin, line_source=lambda _data: [json.dumps(line) for line in lines],
            )
    except SystemExit as exc:
        return int(exc.code or 0), stderr.getvalue()
    return 0, stderr.getvalue()


passed = failed = 0


def check(name, got, want, stderr=""):
    global passed, failed
    if got == want:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL (exit {got}, wanted {want}) {stderr[:200]}")
        failed += 1


# A — failing sample_size + passing randomize in ONE message (shared id), then
#     passing run_tests: the failing design must still BLOCK.
lines = [
    tool_use_line("msg_A", "t1", "sample_size", BAD_SS_INPUT),
    tool_result_line("t1", SS_RESULT),
    tool_use_line("msg_A", "t2", "randomize", {"n": 4}),
    tool_result_line("t2", RAND_RESULT),
    tool_use_line("msg_B", "t3", "run_tests", {}),
    tool_result_line("t3", RT_OK),
]
rc, err = run_hook(lines)
check("A_parallel_sibling_no_launder", rc, 2, err)

# B — passing run_tests + passing sample_size in ONE message: must ALLOW
#     (run_tests in the same group counts; no false "did not run the gate").
lines = [
    tool_use_line("msg_A", "t1", "run_tests", {}),
    tool_result_line("t1", RT_OK),
    tool_use_line("msg_A", "t2", "sample_size", GOOD_SS_INPUT),
    tool_result_line("t2", SS_RESULT),
]
rc, err = run_hook(lines)
check("B_same_message_run_tests_counts", rc, 0, err)

# C — three failing calls in one fan-out message = ONE attempt: the
#     DESIGN_FAIL_ESCAPE(3) must NOT fire on the first stop -> BLOCK.
lines = []
for i in range(3):
    lines.append(tool_use_line("msg_A", f"t{i}", "sample_size", BAD_SS_INPUT))
    lines.append(tool_result_line(f"t{i}", SS_RESULT))
lines += [tool_use_line("msg_B", "t9", "run_tests", {}), tool_result_line("t9", RT_OK)]
rc, err = run_hook(lines)
check("C_fanout_is_one_attempt", rc, 2, err)

# C2 — three consecutive failures do NOT become verified. The first terminal
#      stop is blocked until a value-free failure report is produced.
lines = []
for i in range(3):
    lines.append(tool_use_line(f"msg_{i}", f"t{i}", "sample_size",
                               dict(BAD_SS_INPUT, verification_id="failed-analysis")))
    lines.append(tool_result_line(f"t{i}", SS_RESULT))
lines += [tool_use_line("msg_rt", "t9", "run_tests", {}), tool_result_line("t9", RT_OK)]
rc, err = run_hook(lines)
check("C2_three_attempts_require_failure_report", rc, 2, err)
rc, err = run_hook(lines, stdin_extra={
    "stop_hook_active": True,
    "last_assistant_message": "Verification failed; results withheld as not trustworthy.",
})
check("C3_value_free_escape_report_allows", rc, 0, err)
rc, err = run_hook(lines, stdin_extra={
    "stop_hook_active": True,
    "last_assistant_message": "Verification failed; results withheld. The result was 30.",
})
check("C4_numeric_escape_report_blocks", rc, 2, err)

# D — the installed policy resolves gates relative to its real module path.
lines = [
    tool_use_line("msg_A", "t1", "sample_size", BAD_SS_INPUT),
    tool_result_line("t1", SS_RESULT),
    tool_use_line("msg_B", "t2", "run_tests", {}),
    tool_result_line("t2", RT_OK),
]
rc, err = run_hook(lines)
check("D_installed_policy_still_blocks", rc, 2, err)
rc, err = run_hook(lines, stdin_extra={
    "stop_hook_active": True,
    "last_assistant_message": "Verification failed; results withheld as not trustworthy.",
})
check("D2_single_failure_cannot_skip_retry_budget", rc, 2, err)

# E — ambient transcript paths are ignored; only the injected ledger is read.
rc, err = run_hook(lines, stdin_extra={
    "transcript_path": "/tmp/untrusted-parent.jsonl",
    "agent_transcript_path": "/tmp/untrusted-agent.jsonl",
})
check("E_transcript_paths_cannot_override_ledger", rc, 2, err)

# F — failing simulate_design, then a later PASSING randomize, then run_tests:
#     the unrelated tool must not launder the failing design -> BLOCK.
bad_sim_cfg = {"config": BAD_SS_INPUT}
bad_sim_result = {"sample_size": [{"design": "single_arm", "n_total": 30,
                                   "power_achieved": 0.8}],
                  "oc": [{"true_param": 0.4, "p_go": 0.9},
                         {"true_param": 0.2, "p_go": 0.1}], "seed": 42}
lines = [
    tool_use_line("msg_A", "t1", "simulate_design", bad_sim_cfg),
    tool_result_line("t1", bad_sim_result),
    tool_use_line("msg_B", "t2", "randomize", {"n": 4}),
    tool_result_line("t2", RAND_RESULT),
    tool_use_line("msg_C", "t3", "run_tests", {}),
    tool_result_line("t3", RT_OK),
]
rc, err = run_hook(lines)
check("F_later_tool_does_not_launder", rc, 2, err)

# G — failing sample_size superseded by a PASSING re-run of the same tool,
#     with run_tests after: ALLOW.
lines = [
    tool_use_line("msg_A", "t1", "sample_size",
                  dict(BAD_SS_INPUT, verification_id="same-analysis")),
    tool_result_line("t1", SS_RESULT),
    tool_use_line("msg_B", "t2", "sample_size",
                  dict(GOOD_SS_INPUT, verification_id="same-analysis")),
    tool_result_line("t2", SS_RESULT),
    tool_use_line("msg_C", "t3", "run_tests", {}),
    tool_result_line("t3", RT_OK),
]
rc, err = run_hook(lines)
check("G_same_tool_rerun_supersedes", rc, 0, err)

# H — pure elicitation (no gated calls): ALLOW.
rc, err = run_hook([{"message": {"role": "assistant",
                                 "content": [{"type": "text", "text":
                                              'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}'}]}}])
check("H_structured_elicitation_allows", rc, 0, err)

rc, err = run_hook([{"message": {"role": "assistant", "content": [
    {"type": "text", "text": "What does n_total=30 mean?"}
]}}])
check("H2_question_shaped_numeric_claim_blocks", rc, 2, err)

rc, err = run_hook([{"message": {"role": "assistant", "content": [
    {"type": "text", "text": "A dozen participants should be enough."}
]}}])
check("H3_unbound_word_number_claim_blocks", rc, 2, err)

rc, err = run_hook([{"message": {"role": "assistant", "content": [
    {"type": "text", "text": "What should we do, given that a dozen participants were enrolled?"}
]}}])
check("H4_question_shaped_quantified_claim_blocks", rc, 2, err)

# H5 — a successful validate_config-only turn is configuration feedback, not a
# statistical result, and therefore does not require run_tests.
validate_args = dict(GOOD_SS_INPUT, alphas=0.1, powers=0.8)
validated = {
    "valid": True, "endpoint_type": "binary", "study_type": "poc",
    "design": "single_arm", "go_target": 0.8,
    "alphas": [0.1], "powers": [0.8],
    "resolved_config": {
        "endpoint_type": "binary", "study_type": "poc", "design": "single_arm",
        "estimand": "response_probability", "direction": "greater",
        "sidedness": "one_sided", "null_param": 0.2, "alt_param": 0.4,
        "sd": None, "alloc_ratio": 1, "alphas": [0.1], "powers": [0.8],
        "prior_params": {"a": 0.5, "b": 0.5}, "go_threshold": 0.9,
        "consider_threshold": 0.6, "go_target": 0.8, "p3_n": None,
        "p3_alloc_ratio": 1, "p3_alpha": 0.025, "accrual_time": None,
        "followup_time": None, "tte_method": None, "exposure_time": None,
        "rate_method": None, "has_p2_data": False,
        "has_p2_control_data": False,
    },
    "simulation_defaults": {"seed": 42, "b_oc": 5000},
}
validated["configuration_report"] = "CONFIGURATION_VALIDATED " + canonical_json(validated)
lines = [
    tool_use_line("msg_A", "validate1", "validate_config", validate_args),
    tool_result_line("validate1", validated),
]
rc, err = run_hook(lines, stdin_extra={
    "last_assistant_message": validated["configuration_report"]
})
check("H5_validate_only_configuration_feedback_allows", rc, 0, err)
rc, err = run_hook(lines, stdin_extra={
    "last_assistant_message": "The configuration is valid and needs 100 participants."
})
check("H6_validate_only_cannot_authorize_statistics", rc, 2, err)

# I — result produced, run_tests NEVER run: BLOCK with the "call run_tests" reason.
lines = [
    tool_use_line("msg_A", "t1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("t1", SS_RESULT),
]
rc, err = run_hook(lines)
check("I_missing_run_tests_blocks", rc, 2, err)

# J — early-turn failing run_tests must NOT lift the requirement for a result
#     produced later with no run_tests after it (the old escape did).
lines = [
    tool_use_line("msg_A", "t1", "run_tests", {}),
    tool_result_line("t1", RT_BAD),
    tool_use_line("msg_B", "t2", "run_tests", {}),
    tool_result_line("t2", RT_BAD),
    tool_use_line("msg_C", "t3", "sample_size", GOOD_SS_INPUT),
    tool_result_line("t3", SS_RESULT),
]
rc, err = run_hook(lines)
check("J_early_rt_failures_no_escape", rc, 2, err)

# K — genuinely broken framework may terminate only through a value-free report.
lines = [
    tool_use_line("msg_A", "t1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("t1", SS_RESULT),
    tool_use_line("msg_B", "t2", "run_tests", {}),
    tool_result_line("t2", RT_BAD),
    tool_use_line("msg_C", "t3", "run_tests", {}),
    tool_result_line("t3", RT_BAD),
]
rc, err = run_hook(lines)
check("K_broken_suite_first_stop_blocks", rc, 2, err)
rc, err = run_hook(lines, stdin_extra={
    "stop_hook_active": True,
    "last_assistant_message": "Verification failed; results withheld as not trustworthy.",
})
check("K2_broken_suite_failure_report_allows", rc, 0, err)

# L — the bundled design-verifier is governed by the same fail-closed policy.
lines = [
    tool_use_line("msg_A", "t1", "sample_size", BAD_SS_INPUT),
    tool_result_line("t1", SS_RESULT),
]
rc, err = run_hook(lines, stdin_extra={"agent_type": "design-verifier"})
check("L_verifier_is_not_exempt", rc, 2, err)

lines = [
    tool_use_line("msg_A", "verifier1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("verifier1", SS_RESULT),
    tool_use_line("msg_B", "verifier2", "run_tests", {}),
    tool_result_line("verifier2", RT_OK),
]
rc, err = run_hook(lines, stdin_extra={"agent_type": "design-verifier"})
check("L2_verifier_canonical_success_is_allowed", rc, 0, err)

# M — same tool with a different config does not supersede without a shared
#     verification_id; both analyses retain independent identities.
lines = [
    tool_use_line("msg_A", "t1", "sample_size", BAD_SS_INPUT),
    tool_result_line("t1", SS_RESULT),
    tool_use_line("msg_B", "t2", "sample_size", GOOD_SS_INPUT),
    tool_result_line("t2", SS_RESULT),
    tool_use_line("msg_C", "t3", "run_tests", {}),
    tool_result_line("t3", RT_OK),
]
rc, err = run_hook(lines)
check("M_server_withheld_failure_cannot_contaminate_fresh_analysis", rc, 0, err)

# N — truthy strings and incomplete regression manifests are never accepted.
lines = [
    tool_use_line("msg_A", "t1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("t1", SS_RESULT),
    tool_use_line("msg_B", "t2", "run_tests", {}),
    tool_result_line("t2", dict(RT_OK, all_ok="false")),
]
rc, err = run_hook(lines)
check("N_truthy_regression_status_rejected", rc, 2, err)

# O — stochastic results require an identical-argument replay.
sim_input = {"verification_id": "sim-one", "config": {
    "endpoint_type": "binary", "study_type": "poc", "design": "single_arm",
    "null_param": 0.2, "alt_param": 0.4,
}, "seed": 42}
sim_result = {"sample_size": [{"design": "single_arm", "n_total": 30,
                                "power_target": 0.8, "power_achieved": 0.82}],
              "oc": [{"true_param": 0.2, "p_go": 0.05},
                     {"true_param": 0.4, "p_go": 0.85}], "seed": 42}
lines = [
    tool_use_line("msg_A", "t1", "simulate_design", sim_input),
    tool_result_line("t1", sim_result),
    tool_use_line("msg_B", "t2", "run_tests", {}),
    tool_result_line("t2", RT_OK),
]
rc, err = run_hook(lines)
check("O_server_preverified_stochastic_call_allows", rc, 0, err)
lines = [
    tool_use_line("msg_A", "t1", "simulate_design", sim_input),
    tool_result_line("t1", sim_result),
    tool_use_line("msg_B", "t2", "simulate_design", sim_input),
    tool_result_line("t2", sim_result),
    tool_use_line("msg_C", "t3", "run_tests", {}),
    tool_result_line("t3", RT_OK),
]
rc, err = run_hook(lines)
check("O2_identical_stochastic_replay_allows", rc, 0, err)

# P — prose that merely reuses available numbers, swaps labels, or spells out
#     a fabricated number is not a canonical final report.
lines = [
    tool_use_line("msg_A", "bind1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("bind1", SS_RESULT),
    tool_use_line("msg_B", "bind2", "run_tests", {}),
    tool_result_line("bind2", RT_OK),
]
rc, err = run_hook(lines, stdin_extra={
    "last_assistant_message": "The verified sample size is one million participants."
})
check("P_number_words_cannot_bypass_binding", rc, 2, err)
rc, err = run_hook(lines, stdin_extra={
    "last_assistant_message": "The sample size is 0.82 and achieved power is 30."
})
check("P2_semantic_numeric_swap_cannot_bypass_binding", rc, 2, err)
rc, err = run_hook(lines, stdin_extra={
    "stop_hook_active": True,
    "last_assistant_message": "Verification failed; results withheld as not trustworthy.",
})
check("P3_canonical_mismatch_can_end_value_free", rc, 0, err)

# Q — even a presentable envelope cannot authorize a report that was altered
#     after server verification.
tampered = json.loads(json.dumps(lines))
for row in tampered:
    for block in row.get("message", {}).get("content", []):
        if block.get("type") == "tool_result" and block.get("tool_use_id") == "bind1":
            payload = json.loads(block["content"][0]["text"])
            payload["_verification"]["report"] += "\nTampered"
            block["content"][0]["text"] = json.dumps(payload)
rc, err = run_hook(tampered)
check("Q_tampered_canonical_report_rejected", rc, 2, err)

# Q2 — recomputing report_hash does not make a jointly tampered report trusted;
# the stop policy deterministically re-renders the report from bound fields.
jointly_tampered = json.loads(json.dumps(lines))
for row in jointly_tampered:
    for block in row.get("message", {}).get("content", []):
        if block.get("type") == "tool_result" and block.get("tool_use_id") == "bind1":
            payload = json.loads(block["content"][0]["text"])
            payload["_verification"]["report"] += "\nJointly tampered"
            payload["_verification"]["report_hash"] = content_hash(
                payload["_verification"]["report"]
            )
            block["content"][0]["text"] = json.dumps(payload)
rc, err = run_hook(jointly_tampered)
check("Q2_report_and_hash_tampering_rejected", rc, 2, err)

# R — regression health is chronological: a later failure supersedes an
#     earlier pass after the same result.
lines = [
    tool_use_line("msg_A", "latest1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("latest1", SS_RESULT),
    tool_use_line("msg_B", "latest2", "run_tests", {}),
    tool_result_line("latest2", RT_OK),
    tool_use_line("msg_C", "latest3", "run_tests", {}),
    tool_result_line("latest3", RT_BAD),
]
rc, err = run_hook(lines)
check("R_latest_regression_failure_blocks", rc, 2, err)

# S — a server envelope may not omit the independent/manual limitations that
#     make a passing computational gate only partially verified.
lines = [
    tool_use_line("msg_A", "manual1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("manual1", SS_RESULT),
    tool_use_line("msg_B", "manual2", "run_tests", {}),
    tool_result_line("manual2", RT_OK),
]
for row in lines:
    for block in row.get("message", {}).get("content", []):
        if block.get("type") == "tool_result" and block.get("tool_use_id") == "manual1":
            payload = json.loads(block["content"][0]["text"])
            payload["_verification"]["blocked"] = []
            block["content"][0]["text"] = json.dumps(payload)
rc, err = run_hook(lines)
check("S_omitted_manual_limitations_block", rc, 2, err)

# T — a later server-withheld analysis is the terminal state for the turn; it
# may not disappear and leave an older successful report authorized.
lines = [
    tool_use_line("msg_A", "stale1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("stale1", SS_RESULT),
    tool_use_line("msg_B", "stale2", "sample_size", BAD_SS_INPUT),
    tool_result_line("stale2", SS_RESULT),
    tool_use_line("msg_C", "stale3", "run_tests", {}),
    tool_result_line("stale3", RT_OK),
]
rc, err = run_hook(lines)
check("T_latest_withheld_result_invalidates_older_report", rc, 2, err)

# U — MCP-issued identities require provenance binding. Removing only the hash
# must fail closed even when every result and report field is otherwise intact.
lines = [
    tool_use_line("msg_A", "prov1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("prov1", SS_RESULT),
    tool_use_line("msg_B", "prov2", "run_tests", {}),
    tool_result_line("prov2", RT_OK),
]
for row in lines:
    for block in row.get("message", {}).get("content", []):
        if block.get("type") == "tool_result" and block.get("tool_use_id") == "prov1":
            payload = json.loads(block["content"][0]["text"])
            payload["_verification"]["identity"].pop("provenance_hash", None)
            block["content"][0]["text"] = json.dumps(payload)
rc, err = run_hook(lines)
check("U_missing_provenance_hash_blocks", rc, 2, err)

# V — a malformed result poisons only its own call. A later presentable rerun
# of the same tool supersedes it, but an unrelated success cannot launder it.
malformed_result = {"message": {"role": "user", "content": [{
    "type": "tool_result", "tool_use_id": "malformed1",
    "content": [{"type": "text", "text": "R execution failed"}],
}]}}
lines = [
    tool_use_line("msg_A", "malformed1", "sample_size", GOOD_SS_INPUT),
    malformed_result,
    tool_use_line("msg_B", "corrected1", "sample_size", GOOD_SS_INPUT),
    tool_result_line("corrected1", SS_RESULT),
    tool_use_line("msg_C", "corrected-tests", "run_tests", {}),
    tool_result_line("corrected-tests", RT_OK),
]
rc, err = run_hook(lines)
check("V_malformed_result_same_tool_correction_allows", rc, 0, err)

lines = [
    tool_use_line("msg_A", "malformed1", "sample_size", GOOD_SS_INPUT),
    malformed_result,
    tool_use_line("msg_B", "unrelated1", "randomize", {"n": 4}),
    tool_result_line("unrelated1", RAND_RESULT),
    tool_use_line("msg_C", "unrelated-tests", "run_tests", {}),
    tool_result_line("unrelated-tests", RT_OK),
]
rc, err = run_hook(lines)
check("V2_malformed_result_unrelated_success_blocks", rc, 2, err)

# W — hook observability is structured and does not persist a deterministic
#     digest of potentially sensitive gate diagnostics.
with tempfile.TemporaryDirectory() as directory:
    previous_log = os.environ.get("EXPDESIGN_HOOK_LOG")
    trace_path = os.path.join(directory, "hook.jsonl")
    os.environ["EXPDESIGN_HOOK_LOG"] = trace_path
    try:
        rc, err = run_hook([], {
            "session_id": "session-test",
            "prompt_id": "prompt-test",
            "hook_event_name": "Stop",
            "last_assistant_message": (
                'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}'
            ),
        })
        with open(trace_path, encoding="utf-8") as handle:
            trace_rows = [json.loads(line) for line in handle.read().splitlines()]
        expected_trace_fields = {
            "schema_version", "timestamp", "sequence", "outcome", "session_id",
            "prompt_id", "principal", "hook_event",
        }
        structured = (
            bool(trace_rows)
            and expected_trace_fields <= set(trace_rows[-1])
            and "reason_sha256" not in trace_rows[-1]
        )
        check("W_structured_private_hook_trace", int(not (rc == 0 and structured)), 0, err)

        target_path = os.path.join(directory, "unrelated.txt")
        symlink_path = os.path.join(directory, "trace-link.jsonl")
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write("unchanged")
        os.chmod(target_path, 0o644)
        os.symlink(target_path, symlink_path)
        os.environ["EXPDESIGN_HOOK_LOG"] = symlink_path
        os.environ["EXPDESIGN_HOOK_LOG_REQUIRED"] = "1"
        symlink_rc, _symlink_err = run_hook([], {
            "session_id": "session-symlink",
            "prompt_id": "prompt-symlink",
            "hook_event_name": "Stop",
            "last_assistant_message": (
                'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}'
            ),
        })
        with open(target_path, encoding="utf-8") as handle:
            target_unchanged = handle.read() == "unchanged"
        target_mode_unchanged = stat.S_IMODE(os.stat(target_path).st_mode) == 0o644
        check(
            "W2_hook_trace_symlink_is_rejected_without_touching_target",
            int(not (symlink_rc == 2 and target_unchanged and target_mode_unchanged)),
            0,
            _symlink_err,
        )

        hard_target_path = os.path.join(directory, "hard-target.txt")
        hardlink_path = os.path.join(directory, "trace-hardlink.jsonl")
        with open(hard_target_path, "w", encoding="utf-8") as handle:
            handle.write("hardlink-unchanged")
        os.chmod(hard_target_path, 0o644)
        os.link(hard_target_path, hardlink_path)
        os.environ["EXPDESIGN_HOOK_LOG"] = hardlink_path
        hardlink_rc, hardlink_err = run_hook([], {
            "session_id": "session-hardlink",
            "prompt_id": "prompt-hardlink",
            "hook_event_name": "Stop",
            "last_assistant_message": (
                'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}'
            ),
        })
        with open(hard_target_path, encoding="utf-8") as handle:
            hard_target_unchanged = handle.read() == "hardlink-unchanged"
        hard_target_mode_unchanged = (
            stat.S_IMODE(os.stat(hard_target_path).st_mode) == 0o644
        )
        check(
            "W3_hook_trace_hardlink_is_rejected_without_touching_target",
            int(not (
                hardlink_rc == 2
                and hard_target_unchanged
                and hard_target_mode_unchanged
            )),
            0,
            hardlink_err,
        )
    finally:
        os.environ.pop("EXPDESIGN_HOOK_LOG_REQUIRED", None)
        if previous_log is None:
            os.environ.pop("EXPDESIGN_HOOK_LOG", None)
        else:
            os.environ["EXPDESIGN_HOOK_LOG"] = previous_log

# X — the Python policy applies the same strict principal semantics as the
# launcher and ledger.  Only SubagentStop accepts a concrete subagent id;
# Stop accepts absent/null as the main principal and rejects every present id.
elicitation = [{"message": {"role": "assistant", "content": [{
    "type": "text",
    "text": 'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}',
}]}}]
rc, err = run_hook(elicitation, {
    "hook_event_name": "SubagentStop",
    "agent_id": "domain-worker-1",
})
check("X_valid_subagent_stop_identity_allows", rc, 0, err)

for label, invalid_id in (
    ("missing", "__missing__"),
    ("null", None),
    ("empty", ""),
    ("zero", 0),
    ("false", False),
    ("list", []),
    ("object", {}),
    ("nul", "domain-worker\0other"),
):
    identity = {"hook_event_name": "SubagentStop"}
    if invalid_id != "__missing__":
        identity["agent_id"] = invalid_id
    rc, err = run_hook(elicitation, identity)
    check(f"X_subagent_stop_rejects_{label}_agent_id", rc, 2, err)

rc, err = run_hook(elicitation, {
    "hook_event_name": "Stop",
    "agent_id": "domain-worker-1",
})
check("X_stop_rejects_subagent_identity", rc, 2, err)

rc, err = run_hook(elicitation, {
    "hook_event_name": "Stop",
    "agent_id": None,
})
check("X_stop_explicit_null_is_main", rc, 0, err)

for label, invalid_type in (
    ("empty", ""),
    ("zero", 0),
    ("false", False),
    ("list", []),
    ("object", {}),
    ("nul", "experiment-designer\0other"),
):
    rc, err = run_hook(elicitation, {"agent_type": invalid_type})
    check(f"X_rejects_{label}_agent_type", rc, 2, err)

rc, err = run_hook(elicitation, {"hook_event_name": "PreToolUse"})
check("X_policy_rejects_nonterminal_event", rc, 2, err)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
