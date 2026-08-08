#!/usr/bin/env python3
"""Live contract tests for the synchronous PostToolBatch verification ledger."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "agent-harness"))

from final_report import canonical_report, privacy_safe_view  # noqa: E402
from gates import (GateVerdict, MANUAL_CHECKS, check_regression_tests,
                   combined_gate, design_checks_for)  # noqa: E402
from verification import (VerificationIdentity, canonical_json, content_hash,
                          envelope_from_verdict)  # noqa: E402

LAUNCHER = ROOT / "hooks" / "launch_verification.sh"
SERVER = "mcp__experiment-design__"
RT_OK = {
    "all_ok": True, "suite_count": 5, "passed_suite_count": 5,
    "failed_suite_count": 0,
    "checks": {"declared_all_ok": True, "complete_suite_set": True,
               "suite_records_valid": True, "expected_check_count": True},
}


def verified_payload(tool: str, args: dict, raw: dict, analysis_id: str) -> dict:
    provenance = {"engine_fingerprint": "ledger-engine", "config_hash": "ledger-config"}
    verdict = combined_gate(
        *design_checks_for(tool, args, raw),
        check_regression_tests(RT_OK),
        GateVerdict(passed=True, blocked=list(MANUAL_CHECKS)),
    )
    identity = VerificationIdentity.from_call(
        tool, dict(args, verification_id=analysis_id), raw,
        analysis_id.replace("analysis-", "call-"), provenance)
    envelope = envelope_from_verdict(identity, verdict, provenance).to_dict()
    public = privacy_safe_view(tool, raw)
    report = canonical_report(tool, args, raw, envelope, provenance)
    envelope.update({
        "report": report,
        "report_hash": content_hash(report),
        "public_result_hash": content_hash(public),
    })
    envelope["identity"].pop("result_hash", None)
    return {**public, "_verification": envelope, "_provenance": provenance}


def run(mode: str, data: dict, env: dict, scope: str | None = None) -> subprocess.CompletedProcess[str]:
    scope = scope or str(data.get("agent_type") or "experiment-designer")
    return subprocess.run(
        ["sh", str(LAUNCHER), mode, scope], input=json.dumps(data),
        capture_output=True, text=True, env=env,
    )


def batch(base: dict, number: int, calls: list[dict]) -> dict:
    return {**base, "hook_event_name": "PostToolBatch",
            "tool_calls": [dict(call, tool_use_id=f"tool-{number}-{index}")
                           for index, call in enumerate(calls)]}


passed = failed = 0


def check(name: str, condition: bool, detail="") -> None:
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {detail}")
        failed += 1


with tempfile.TemporaryDirectory() as directory:
    env = dict(os.environ, EXPDESIGN_HOOK_LEDGER_DIR=directory)
    invalid_input = subprocess.run(
        ["sh", str(LAUNCHER), "record", "experiment-designer"], input="not-json",
        capture_output=True, text=True, env=env,
    )
    unknown_mode = subprocess.run(
        ["sh", str(LAUNCHER), "unknown"], input="{}",
        capture_output=True, text=True, env=env,
    )
    main_batch = run("record", {
        "session_id": "main-session", "prompt_id": "main-prompt",
        "hook_event_name": "PostToolBatch", "tool_calls": [],
    }, env)
    missing_scope = subprocess.run(
        ["sh", str(LAUNCHER), "record"], input=json.dumps({}),
        capture_output=True, text=True, env=env,
    )
    check("launcher_failures_are_blocking",
          invalid_input.returncode == unknown_mode.returncode == missing_scope.returncode == 2,
          (invalid_input.stderr, unknown_mode.stderr, missing_scope.stderr))
    check("governed_main_agent_batch_is_recordable", main_batch.returncode == 0,
          main_batch.stderr)

    base = {"session_id": "session-ledger", "prompt_id": "prompt-good",
            "agent_id": "agent-good"}
    args = {"endpoint_type": "binary", "study_type": "poc",
            "design": "single_arm", "null_param": 0.2, "alt_param": 0.4}
    raw = {"results": [{"design": "single_arm", "n_total": 30,
                        "power_target": 0.8, "power_achieved": 0.82}]}
    payload = verified_payload("sample_size", args, raw, "analysis-ledger-good")
    report = payload["_verification"]["report"]
    record_analysis = run("record", batch(base, 1, [{
        "tool_name": SERVER + "sample_size", "tool_input": args,
        "tool_response": json.dumps(payload),
    }]), env)
    record_tests = run("record", batch(base, 2, [{
        "tool_name": SERVER + "run_tests", "tool_input": {},
        "tool_response": json.dumps(RT_OK),
    }]), env)
    stale = Path(directory) / "stale.jsonl"
    stale.write_text("", encoding="utf-8")
    stop = run("enforce", {**base, "hook_event_name": "SubagentStop",
                            "agent_type": "experiment-designer",
                            "agent_transcript_path": str(stale),
                            "last_assistant_message": report}, env)
    check("ledger_overrides_stale_transcript",
          record_analysis.returncode == record_tests.returncode == stop.returncode == 0,
          (record_analysis.stderr, record_tests.stderr, stop.stderr))

    mismatch_base = {"session_id": "session-ledger", "prompt_id": "prompt-mismatch",
                     "agent_id": "agent-mismatch"}
    run("record", batch(mismatch_base, 1, [{
        "tool_name": SERVER + "sample_size", "tool_input": args,
        "tool_response": json.dumps(payload),
    }]), env)
    run("record", batch(mismatch_base, 2, [{
        "tool_name": SERVER + "run_tests", "tool_input": {},
        "tool_response": json.dumps(RT_OK),
    }]), env)
    mismatch = run("enforce", {**mismatch_base, "hook_event_name": "SubagentStop",
                                "agent_type": "experiment-designer",
                                "last_assistant_message": report + " altered"}, env)
    check("current_last_message_is_authoritative", mismatch.returncode == 2, mismatch.stderr)

    main_base = {"session_id": "main-session", "prompt_id": "main-analysis"}
    main_record_analysis = run("record", batch(main_base, 1, [{
        "tool_name": SERVER + "sample_size", "tool_input": args,
        "tool_response": json.dumps(payload),
    }]), env)
    main_record_tests = run("record", batch(main_base, 2, [{
        "tool_name": SERVER + "run_tests", "tool_input": {},
        "tool_response": json.dumps(RT_OK),
    }]), env)
    main_stop = run("enforce", {
        **main_base, "hook_event_name": "Stop",
        "last_assistant_message": report,
    }, env)
    check("main_session_stop_uses_scoped_ledger",
          main_record_analysis.returncode == main_record_tests.returncode == main_stop.returncode == 0,
          (main_record_analysis.stderr, main_record_tests.stderr, main_stop.stderr))

    # A missing response is durably recorded as an incomplete batch before the
    # PostToolBatch hook blocks. A later batch cannot silently bridge the gap.
    gap_base = {"session_id": "session-ledger", "prompt_id": "prompt-gap",
                "agent_id": "agent-gap"}
    missing_response = run("record", batch(gap_base, 1, [{
        "tool_name": SERVER + "sample_size", "tool_input": args,
    }]), env)
    followup_after_gap = run("record", batch(gap_base, 2, [{
        "tool_name": SERVER + "sample_size", "tool_input": args,
        "tool_response": json.dumps(payload),
    }]), env)
    gap_stop = run("enforce", {
        **gap_base, "hook_event_name": "SubagentStop",
        "agent_type": "experiment-designer", "last_assistant_message": report,
    }, env)
    incomplete_ledgers = []
    for candidate in Path(directory).glob("*.json"):
        value = json.loads(candidate.read_text(encoding="utf-8"))
        if any(batch_record.get("complete") is False
               for batch_record in value.get("batches", [])):
            incomplete_ledgers.append(value)
    check("missing_response_persists_blocking_batch_gap",
          missing_response.returncode == followup_after_gap.returncode == gap_stop.returncode == 2
          and len(incomplete_ledgers) == 1
          and incomplete_ledgers[0]["batch_count"] == 1,
          (missing_response.stderr, followup_after_gap.stderr, gap_stop.stderr))

    # validate_config is ledgered for completeness but remains a configuration
    # pre-check, so a valid validate-only turn does not require run_tests.
    validate_base = {"session_id": "session-ledger", "prompt_id": "prompt-validate",
                     "agent_id": "agent-validate"}
    validate_result = {
        "valid": True, "endpoint_type": "binary", "study_type": "poc",
        "design": "single_arm", "go_target": 0.8,
        "alphas": [0.1], "powers": [0.8],
    }
    validate_result["configuration_report"] = (
        "CONFIGURATION_VALIDATED " + canonical_json(validate_result)
    )
    validate_record = run("record", batch(validate_base, 1, [{
        "tool_name": SERVER + "validate_config", "tool_input": args,
        "tool_response": json.dumps(validate_result),
    }]), env)
    validate_stop = run("enforce", {
        **validate_base, "hook_event_name": "SubagentStop",
        "agent_type": "experiment-designer",
        "last_assistant_message": validate_result["configuration_report"],
    }, env)
    check("validate_only_turn_is_recorded_and_allowed",
          validate_record.returncode == validate_stop.returncode == 0,
          (validate_record.stderr, validate_stop.stderr))

    # Concurrent PostToolBatch writers serialize through a separate lock inode;
    # no batch is lost when atomic replacement changes the data inode.
    concurrent_base = {"session_id": "session-ledger",
                       "prompt_id": "prompt-concurrent", "agent_id": "agent-concurrent"}
    processes = []
    for number in range(8):
        event = batch(concurrent_base, number, [{
            "tool_name": SERVER + "validate_config", "tool_input": args,
            "tool_response": json.dumps(validate_result),
        }])
        processes.append(subprocess.Popen(
            ["sh", str(LAUNCHER), "record", "experiment-designer"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env,
        ))
        processes[-1].stdin.write(json.dumps(event))
        processes[-1].stdin.close()
    concurrent_results = []
    for process in processes:
        stdout = process.stdout.read()
        stderr = process.stderr.read()
        process.wait()
        concurrent_results.append((process.returncode, stdout, stderr))
    concurrent_ledger = None
    for candidate in Path(directory).glob("*.json"):
        value = json.loads(candidate.read_text(encoding="utf-8"))
        if value.get("batch_count") == 8:
            concurrent_ledger = value
            break
    check("concurrent_batches_are_sequential_and_complete",
          all(code == 0 for code, _, _ in concurrent_results)
          and concurrent_ledger is not None
          and [item["number"] for item in concurrent_ledger["batches"]] == list(range(1, 9))
          and all(item["complete"] is True for item in concurrent_ledger["batches"])
          and len(concurrent_ledger["lines"]) == 16,
          concurrent_results)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
raise SystemExit(1 if failed else 0)
