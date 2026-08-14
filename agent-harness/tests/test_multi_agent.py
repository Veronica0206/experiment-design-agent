#!/usr/bin/env python3
"""Tests for routing, least privilege, and deterministic domain handoffs."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from handoff import (HandoffEnvelope, HandoffError, HandoffLedger, SAFE_FAILURE_MESSAGE,
                     aggregate_handoffs)  # noqa: E402
from harness import ExperimentDesignHarness, RunResult  # noqa: E402
from multi_agent_harness import MultiAgentExperimentDesignHarness  # noqa: E402
from verification import content_hash  # noqa: E402


passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {detail}")
        failed += 1


def route_response(action, agents=(), fields=()):
    return SimpleNamespace(content=[SimpleNamespace(
        type="tool_use", name="route_experiment_design", id="route-1",
        input={
            "action": action,
            "agents": list(agents),
            "clarification_fields": list(fields),
        },
    )])


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.messages = FakeMessages(responses)


class FakeExecutor:
    def __init__(self, result, events=None):
        self.result = result
        self.requests = []
        self.events = list(events) if events is not None else [
            {"event": "phase", "phase": "Execute"},
        ]

    def conversation_checkpoint(self):
        return len(self.requests)

    def rollback_conversation(self, checkpoint):
        del self.requests[checkpoint:]

    def run(self, message):
        self.requests.append(message)
        yield from self.events
        return self.result


def verified_result(agent, domain, analysis_id, label):
    report = (
        f"# Verified experiment-design result\n\n"
        f"## Result\n{label}\n\n"
        f"## Verification\n- verification_id: `{analysis_id}`"
    )
    return RunResult(
        run_id=f"run-{agent}", final_answer=report,
        agent_name=agent, domain=domain,
        canonical_report_bindings=[{
            "analysis_id": analysis_id, "status": "VERIFIED",
            "report": report, "report_hash": content_hash(report), "blocked": [],
        }],
    )


def consume(generator):
    events = []
    while True:
        try:
            events.append(next(generator))
        except StopIteration as stop:
            return events, stop.value


with tempfile.TemporaryDirectory() as directory:
    fake_client = FakeClient([route_response(
        "dispatch", ["doe-designer", "single-endpoint-designer"],
    )])
    multi = MultiAgentExperimentDesignHarness(
        anthropic_client=fake_client, log_dir=Path(directory),
    )
    executors = {
        "single-endpoint-designer": FakeExecutor(verified_result(
            "single-endpoint-designer", "single_endpoint", "analysis-single", "single",
        )),
        "doe-designer": FakeExecutor(verified_result(
            "doe-designer", "doe", "analysis-doe", "doe",
        )),
    }
    multi._executor = lambda name: executors[name]  # type: ignore[method-assign]
    multi.start()
    events, result = consume(multi.run("two independent deliverables"))
    multi.stop()

check("router_uses_only_closed_structured_tool",
      fake_client.messages.calls[0]["tools"][0]["name"] == "route_experiment_design"
      and fake_client.messages.calls[0]["tool_choice"] == {
          "type": "tool", "name": "route_experiment_design"})
check("fan_in_uses_registry_order_not_model_order",
      result.final_answer.index("single") < result.final_answer.index("doe")
      and "\n\n---\n\n" in result.final_answer)
check("children_receive_original_request_not_router_rewrite",
      all(item.requests == ["two independent deliverables"] for item in executors.values()))
check("agent_events_carry_domain_identity",
      {event.get("agent") for event in events if event.get("event") == "agent_complete"}
      == set(executors))

clarify_client = FakeClient([route_response("clarify", fields=["analysis_method"])])
clarify = MultiAgentExperimentDesignHarness(anthropic_client=clarify_client)
clarify.start()
_events, clarify_result = consume(clarify.run("help me analyze this"))
clarify.stop()
check("ambiguous_route_returns_only_structured_clarification",
      clarify_result.final_answer
      == 'CLARIFICATION_REQUEST {"fields":["analysis_method"]}')

bad_client = FakeClient([SimpleNamespace(content=[SimpleNamespace(
    type="text", text="Use the meta agent",
)])])
bad = MultiAgentExperimentDesignHarness(anthropic_client=bad_client)
bad.start()
_events, bad_result = consume(bad.run("pool studies"))
bad.stop()
check("router_prose_fails_closed", bad_result.final_answer == SAFE_FAILURE_MESSAGE)

mixed_client = FakeClient([route_response(
    "dispatch", ["single-endpoint-designer", "doe-designer"],
)])
mixed = MultiAgentExperimentDesignHarness(anthropic_client=mixed_client)
secret_sibling_report = "SECRET-SIBLING-RESULT-999"
successful_sibling = verified_result(
    "single-endpoint-designer", "single_endpoint", "analysis-good",
    secret_sibling_report,
)
successful_sibling.private_resources = [{"secret": secret_sibling_report}]
mixed_executors = {
    "single-endpoint-designer": FakeExecutor(successful_sibling, events=[
        {"event": "tool_call", "tool": "sample_size",
         "params": {"secret": secret_sibling_report}},
        {"event": "tool_result", "tool": "sample_size",
         "result": {"report": secret_sibling_report}},
        {"event": "message", "content": secret_sibling_report},
    ]),
    "doe-designer": FakeExecutor(RunResult(
        run_id="run-bad", final_answer=SAFE_FAILURE_MESSAGE,
        agent_name="doe-designer", domain="doe",
    )),
}
mixed._executor = lambda name: mixed_executors[name]  # type: ignore[method-assign]
mixed.start()
_events, mixed_result = consume(mixed.run("independent work"))
mixed.stop()
check("failed_child_cannot_be_laundered_by_successful_sibling",
      mixed_result.final_answer == SAFE_FAILURE_MESSAGE
      and not mixed_result.private_resources
      and not mixed_result.verification_records
      and secret_sibling_report not in str(_events))

phase_mix_client = FakeClient([route_response(
    "dispatch", ["single-endpoint-designer", "meta-analysis-analyst"],
)])
phase_mix = MultiAgentExperimentDesignHarness(anthropic_client=phase_mix_client)
phase_mix.start()
_events, phase_mix_result = consume(phase_mix.run("dependent evidence and design"))
phase_mix.stop()
check("evidence_and_planning_cannot_share_one_user_turn",
      phase_mix_result.final_answer == SAFE_FAILURE_MESSAGE
      and phase_mix_result.stopped == "routing_failed")

base = HandoffEnvelope(
    orchestration_id="o", task_id="t", parent_task_id="p",
    agent_name="single-endpoint-designer", agent_instance_id="a",
    domain="single_endpoint", status="VERIFIED",
    canonical_report="report", report_hash="tampered", analysis_ids=("x",),
)
try:
    aggregate_handoffs([base], ["single-endpoint-designer"])
    tamper_rejected = False
except HandoffError:
    tamper_rejected = True
check("coordinator_rejects_tampered_report_commitment", tamper_rejected)

fabricated = RunResult(
    run_id="run-fabricated", final_answer="FABRICATED RESULT 999; analysis-x",
    agent_name="single-endpoint-designer", domain="single_endpoint",
    verification_records=[{
        "status": "VERIFIED", "presentable": True, "blocked": [],
        "identity": {"analysis_id": "analysis-x", "tool": "sample_size"},
    }],
)
try:
    HandoffEnvelope.from_run_result(
        fabricated, orchestration_id="o", task_id="fabricated-task",
        parent_task_id="p", expected_agent="single-endpoint-designer",
        expected_domain="single_endpoint",
    )
    fabricated_report_rejected = False
except HandoffError:
    fabricated_report_rejected = True
check("self_asserted_numeric_report_is_rejected", fabricated_report_rejected)

artifact_analysis_id = "analysis-artifact"
artifact_uuid = "123e4567-e89b-42d3-a456-426614174000"
artifact_result = verified_result(
    "single-endpoint-designer", "single_endpoint", artifact_analysis_id,
    "artifact result",
)
artifact_uri = f"expdesign-artifact://{artifact_analysis_id}/{artifact_uuid}"
artifact_result.private_resources = [{
    "resource": {"uri": artifact_uri},
    "verification_id": artifact_analysis_id,
}]
artifact_handoff = HandoffEnvelope.from_run_result(
    artifact_result, orchestration_id="o", task_id="artifact-task",
    parent_task_id="p", expected_agent="single-endpoint-designer",
    expected_domain="single_endpoint",
)
check("handoff_accepts_only_identity_bound_opaque_artifact_uri",
      artifact_handoff.artifact_handles == (artifact_uri,)
      and artifact_handoff.public_summary()["artifact_handles"] == [artifact_uri])

malformed_artifact_bundles = [
    [{"resource": {
        "uri": artifact_uri + "?host_path=/private/subject.csv",
    }, "verification_id": artifact_analysis_id}],
    [{"resource": {
        "uri": f"expdesign-artifact://analysis-other/{artifact_uuid}",
    }, "verification_id": "analysis-other"}],
    [{"resource": {
        "uri": f"expdesign-artifact://{artifact_analysis_id}/not-a-uuid",
    }, "verification_id": artifact_analysis_id}],
    [{"resource": {"uri": artifact_uri + "#private"},
      "verification_id": artifact_analysis_id}],
    [{"resource": {"uri": artifact_uri}, "verification_id": "analysis-other"}],
    [{"resource": {
        "uri": f"expdesign-artifact://analysis%2Dartifact/{artifact_uuid}",
    }, "verification_id": artifact_analysis_id}],
    [{"resource": {
        "uri": f"expdesign-artifact://[invalid/{artifact_uuid}",
    }, "verification_id": artifact_analysis_id}],
    [{"resource": {}}],
    [{"secret": "/private/subject.csv"}],
    ["not-a-bundle"],
    {"resource": {"uri": artifact_uri}},
]
malformed_artifacts_rejected = True
for index, private_resources in enumerate(malformed_artifact_bundles):
    candidate = verified_result(
        "single-endpoint-designer", "single_endpoint", artifact_analysis_id,
        f"malformed artifact result {index}",
    )
    candidate.private_resources = private_resources
    try:
        HandoffEnvelope.from_run_result(
            candidate, orchestration_id="o", task_id=f"malformed-artifact-{index}",
            parent_task_id="p", expected_agent="single-endpoint-designer",
            expected_domain="single_endpoint",
        )
        malformed_artifacts_rejected = False
    except HandoffError:
        pass
check("handoff_rejects_malformed_or_cross_identity_artifact_bundles",
      malformed_artifacts_rejected)

direct_malformed = HandoffEnvelope(
    orchestration_id="o", task_id="direct-malformed", parent_task_id="p",
    agent_name="single-endpoint-designer", agent_instance_id="a",
    domain="single_endpoint", status="VERIFIED",
    canonical_report="direct report", report_hash=content_hash("direct report"),
    analysis_ids=(artifact_analysis_id,),
    artifact_handles=(artifact_uri + "?host_path=/private/subject.csv",),
)
direct_boundaries_rejected = True
for boundary in (
    direct_malformed.public_summary,
    lambda: HandoffLedger().record(direct_malformed),
    lambda: aggregate_handoffs(
        [direct_malformed], ["single-endpoint-designer"],
    ),
):
    try:
        boundary()
        direct_boundaries_rejected = False
    except HandoffError:
        pass
check("direct_handoff_construction_cannot_bypass_artifact_uri_validation",
      direct_boundaries_rejected)

fan_in_one = HandoffEnvelope(
    orchestration_id="o", task_id="t1", parent_task_id="p",
    agent_name="single-endpoint-designer", agent_instance_id="a",
    domain="single_endpoint", status="VERIFIED", canonical_report="report one",
    report_hash=content_hash("report one"), analysis_ids=("analysis-shared",),
)
fan_in_two = HandoffEnvelope(
    orchestration_id="o", task_id="t2", parent_task_id="p",
    agent_name="doe-designer", agent_instance_id="b",
    domain="doe", status="VERIFIED", canonical_report="report two",
    report_hash=content_hash("report two"), analysis_ids=("analysis-shared",),
)
try:
    aggregate_handoffs(
        [fan_in_one, fan_in_two],
        ["single-endpoint-designer", "doe-designer"],
    )
    reused_analysis_rejected = False
except HandoffError:
    reused_analysis_rejected = True
check("coordinator_rejects_reused_analysis_identity_across_handoffs",
      reused_analysis_rejected)

duplicate_client = FakeClient([route_response(
    "dispatch", ["single-endpoint-designer", "doe-designer"],
)])
duplicate = MultiAgentExperimentDesignHarness(anthropic_client=duplicate_client)
duplicate_secret = "AGGREGATION-SECRET-777"
duplicate_executors = {
    "single-endpoint-designer": FakeExecutor(verified_result(
        "single-endpoint-designer", "single_endpoint", "analysis-reused",
        duplicate_secret + "-single",
    ), events=[{"event": "message", "content": duplicate_secret + "-single"}]),
    "doe-designer": FakeExecutor(verified_result(
        "doe-designer", "doe", "analysis-reused", duplicate_secret + "-doe",
    ), events=[{"event": "tool_result", "tool": "ab_test",
                "result": {"report": duplicate_secret + "-doe"}}]),
}
duplicate._executor = lambda name: duplicate_executors[name]  # type: ignore[method-assign]
duplicate.start()
duplicate_events, duplicate_result = consume(duplicate.run("duplicate identity"))
duplicate.stop()
check("aggregation_failure_publishes_no_child_content_or_records",
      duplicate_result.final_answer == SAFE_FAILURE_MESSAGE
      and duplicate_result.stopped == "aggregation_failed"
      and not duplicate_result.verification_records
      and not duplicate_result.private_resources
      and duplicate_secret not in str(duplicate_events)
      and all(not executor.requests for executor in duplicate_executors.values()),
      (duplicate_events, duplicate_result))

cross_parent = HandoffEnvelope(
    **{
        **fan_in_two.__dict__,
        "parent_task_id": "different-parent",
        "analysis_ids": ("analysis-two",),
    }
)
try:
    aggregate_handoffs(
        [fan_in_one, cross_parent],
        ["single-endpoint-designer", "doe-designer"],
    )
    mixed_parent_rejected = False
except HandoffError:
    mixed_parent_rejected = True
check("coordinator_rejects_cross_parent_handoffs", mixed_parent_rejected)

config_report = "# Resolved configuration\n\n- alpha convention: one-sided"
config_result = RunResult(
    run_id="run-config", final_answer=config_report,
    agent_name="single-endpoint-designer", domain="single_endpoint",
    validated_configuration_reports=[config_report],
)
config_handoff = HandoffEnvelope.from_run_result(
    config_result, orchestration_id="o", task_id="config-task",
    parent_task_id="p", expected_agent="single-endpoint-designer",
    expected_domain="single_endpoint",
)
check("validated_configuration_has_explicit_machine_binding",
      config_handoff.status == "VALIDATED_CONFIG"
      and config_handoff.canonical_report == config_report)
second_config_report = "# Resolved configuration\n\n- alpha convention: two-sided"
joined_config_report = config_report + "\n\n---\n\n" + second_config_report
joined_config_handoff = HandoffEnvelope.from_run_result(
    RunResult(
        run_id="run-two-configs", final_answer=joined_config_report,
        agent_name="single-endpoint-designer", domain="single_endpoint",
        validated_configuration_reports=[config_report, second_config_report],
    ),
    orchestration_id="o", task_id="two-config-task", parent_task_id="p",
    expected_agent="single-endpoint-designer", expected_domain="single_endpoint",
)
check("multiple_validated_configurations_keep_deterministic_binding",
      joined_config_handoff.status == "VALIDATED_CONFIG"
      and joined_config_handoff.canonical_report == joined_config_report)
try:
    HandoffEnvelope.from_run_result(
        RunResult(
            run_id="run-unbound", final_answer=config_report,
            agent_name="single-endpoint-designer", domain="single_endpoint",
        ),
        orchestration_id="o", task_id="unbound-task", parent_task_id="p",
        expected_agent="single-endpoint-designer", expected_domain="single_endpoint",
    )
    unbound_config_rejected = False
except HandoffError:
    unbound_config_rejected = True
check("unbound_configuration_text_is_rejected", unbound_config_rejected)


class StopExecutor:
    def __init__(self, fails=False):
        self.fails = fails
        self.stop_count = 0

    def stop(self):
        self.stop_count += 1
        if self.fails:
            raise RuntimeError("private stop detail")


cleanup = MultiAgentExperimentDesignHarness(anthropic_client=FakeClient([]))
first_stop = StopExecutor(fails=True)
second_stop = StopExecutor()
cleanup._executors = {  # type: ignore[assignment]
    "single-endpoint-designer": first_stop,
    "meta-analysis-analyst": second_stop,
}
cleanup._started = True
try:
    cleanup.stop()
    cleanup_failed_closed = False
except RuntimeError as exc:
    cleanup_failed_closed = str(exc) == "one or more domain runtimes failed to stop"
check("team_stop_attempts_every_child_and_retains_only_failed_handles",
      cleanup_failed_closed and first_stop.stop_count == second_stop.stop_count == 1
      and list(cleanup._executors) == ["single-endpoint-designer"]
      and cleanup._started is False)
first_stop.fails = False
cleanup.stop()
check("team_stop_retry_clears_the_remaining_failed_handle",
      first_stop.stop_count == 2 and cleanup._executors == {})


class FakeMCP:
    def __init__(self):
        self.stopped = False

    def start(self):
        return {}

    def stop(self):
        self.stopped = True

    def list_tools(self):
        return [
            {"name": name, "inputSchema": {"type": "object"}}
            for name in (
                "validate_config", "sample_size", "simulate_design", "master_simulate",
                "indirect_compare", "meta_analyze", "ab_test", "factorial_design",
                "rsm_design", "randomize", "run_tests",
            )
        ]


with tempfile.TemporaryDirectory() as directory:
    fake_mcp = FakeMCP()
    domain_harness = ExperimentDesignHarness(
        anthropic_client=FakeClient([]), log_dir=Path(directory),
        agent_name="doe-designer", mcp_client=fake_mcp,
    )
    domain_harness.start()
    exposed = {item["name"] for item in domain_harness._tools}
    domain_harness.stop()
check("domain_harness_enforces_exact_tool_allowlist",
      exposed == {"ab_test", "factorial_design", "rsm_design", "run_tests"})
check("domain_harness_does_not_expose_randomization_to_doe",
      "randomize" not in exposed and fake_mcp.stopped)


class BrokenStartupMCP(FakeMCP):
    def __init__(self):
        super().__init__()
        self.started = False

    def start(self):
        self.started = True
        return {}

    def list_tools(self):
        raise RuntimeError("private startup detail")


broken_mcp = BrokenStartupMCP()
broken_harness = ExperimentDesignHarness(
    anthropic_client=FakeClient([]), agent_name="doe-designer",
    mcp_client=broken_mcp,
)
try:
    broken_harness.start()
    startup_failed = False
except RuntimeError:
    startup_failed = True
check("failed_child_startup_cleans_started_mcp",
      startup_failed and broken_mcp.started and broken_mcp.stopped)


class PartialRaiseMCP(FakeMCP):
    def __init__(self):
        super().__init__()
        self.started = False

    def start(self):
        self.started = True
        raise RuntimeError("initialize failed after process spawn")


partial_mcp = PartialRaiseMCP()
partial_harness = ExperimentDesignHarness(
    anthropic_client=FakeClient([]), agent_name="doe-designer",
    mcp_client=partial_mcp,
)
try:
    partial_harness.start()
    partial_start_failed = False
except RuntimeError:
    partial_start_failed = True
check("direct_harness_cleans_mcp_when_start_itself_raises",
      partial_start_failed and partial_mcp.started and partial_mcp.stopped)


class ToolUseResponse:
    def __init__(self, name):
        self.content = [SimpleNamespace(
            type="tool_use", name=name, input={"n": 4}, id="forged-tool",
        )]
        self.stop_reason = "tool_use"


class RecordingMCP(FakeMCP):
    def __init__(self):
        super().__init__()
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {}


with tempfile.TemporaryDirectory() as directory:
    recording_mcp = RecordingMCP()
    forged_client = FakeClient([ToolUseResponse("randomize")])
    forged_harness = ExperimentDesignHarness(
        anthropic_client=forged_client, log_dir=Path(directory),
        agent_name="doe-designer", mcp_client=recording_mcp,
    )
    forged_harness.start()
    _forged_events, forged_result = consume(forged_harness.run("construct DOE"))
    forged_harness.stop()
check("model_cannot_execute_tool_outside_domain_grant",
      forged_result.stopped == "error" and recording_mcp.calls == [])


with tempfile.TemporaryDirectory() as directory:
    cancellation_mcp = RecordingMCP()
    cancellation_harness = ExperimentDesignHarness(
        anthropic_client=FakeClient([ToolUseResponse("ab_test")]),
        log_dir=Path(directory), agent_name="doe-designer",
        mcp_client=cancellation_mcp,
    )
    cancellation_harness.start()
    cancelled_stream = cancellation_harness.run("cancel this turn")
    first_cancel_event = next(cancelled_stream)
    cancelled_stream.close()
    cancellation_messages = list(cancellation_harness.messages)
    cancellation_harness.stop()
check("direct_generator_cancellation_rolls_back_half_turn",
      first_cancel_event.get("event") == "tool_call"
      and cancellation_messages == [] and cancellation_mcp.calls == [],
      (first_cancel_event, cancellation_messages, cancellation_mcp.calls))


class CancellationExecutor:
    def __init__(self):
        self.messages = ["baseline"]
        self.closed = False

    def conversation_checkpoint(self):
        return len(self.messages)

    def rollback_conversation(self, checkpoint):
        del self.messages[checkpoint:]

    def run(self, _message):
        self.messages.append("cancelled turn")
        try:
            yield {
                "event": "tool_call", "tool": "sample_size",
                "params": {"secret": "CANCELLED-SECRET-555"},
            }
            return verified_result(
                "single-endpoint-designer", "single_endpoint",
                "analysis-cancelled", "CANCELLED-SECRET-555",
            )
        finally:
            self.closed = True


cancel_client = FakeClient([route_response(
    "dispatch", ["single-endpoint-designer"],
)])
cancel_team = MultiAgentExperimentDesignHarness(anthropic_client=cancel_client)
cancel_executor = CancellationExecutor()
cancel_team._executor = lambda _name: cancel_executor  # type: ignore[method-assign]
cancel_team.start()
cancel_team_stream = cancel_team.run("cancel coordinator turn")
cancel_route = next(cancel_team_stream)
cancel_start = next(cancel_team_stream)
cancel_progress = next(cancel_team_stream)
cancel_team_stream.close()
cancel_team.stop()
check("coordinator_cancellation_closes_child_and_rolls_back_all_state",
      cancel_route.get("event") == "route"
      and cancel_start.get("event") == "agent_start"
      and cancel_progress == {
          "event": "tool_call", "tool": "sample_size",
          "agent": "single-endpoint-designer",
          "task_id": cancel_progress.get("task_id"),
      }
      and "CANCELLED-SECRET-555" not in str(cancel_progress)
      and cancel_executor.closed and cancel_executor.messages == ["baseline"]
      and cancel_team._router_messages == [],
      (cancel_progress, cancel_executor.messages, cancel_team._router_messages))

meta_prompt = ExperimentDesignHarness(
    anthropic_client=FakeClient([]), agent_name="meta-analysis-analyst",
    mcp_client=FakeMCP(),
).system_prompt
check("domain_prompt_lists_only_registered_tools",
      "meta_analyze" in meta_prompt and "run_tests" in meta_prompt
      and "master_simulate" not in meta_prompt and "validate_config" not in meta_prompt)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
