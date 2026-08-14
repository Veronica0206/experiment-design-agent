#!/usr/bin/env python3
"""Live fail-closed tests for coordinator child routing and result binding."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LAUNCHER = ROOT / "hooks" / "launch_verification.sh"
NODE_LAUNCHER = ROOT / "hooks" / "launch_verification.mjs"
SCOPE = "experiment-design-coordinator"
SAFE_FAILURE = "Verification failed; results withheld as not trustworthy."
SEPARATOR = "\n\n---\n\n"


def run(mode: str, data: dict, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(LAUNCHER), mode, SCOPE], input=json.dumps(data),
        capture_output=True, text=True, env=env,
    )


def batch(base: dict, calls: list[dict]) -> dict:
    return {**base, "hook_event_name": "PostToolBatch", "tool_calls": [
        dict(call, tool_use_id=f"child-{index}") for index, call in enumerate(calls)
    ]}


def child(agent: str, response: object, *, background: bool = False) -> dict:
    return {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": agent,
            "description": f"Dispatch current request to {agent}",
            "prompt": "private coordinator prompt is intentionally not ledgered",
            "run_in_background": background,
        },
        "tool_response": response,
    }


def prompt_capture(base: dict, prompt: object) -> dict:
    return {
        **base, "hook_event_name": "UserPromptSubmit", "agent_type": SCOPE,
        "prompt": prompt,
    }


def dispatch(
    base: dict, prompt: object, *, child_agent: str = "single-endpoint-designer",
    background: object = False, extra: dict | None = None,
) -> dict:
    tool_input = {
        "prompt": prompt,
        "description": f"Dispatch current request to {child_agent}",
        "subagent_type": child_agent,
        "run_in_background": background,
    }
    tool_input.update(extra or {})
    return {
        **base, "hook_event_name": "PreToolUse", "agent_type": SCOPE,
        "tool_name": "Agent", "tool_use_id": "dispatch-binding-test",
        "tool_input": tool_input,
    }


passed = failed = 0


def check(name: str, condition: bool, detail: object = "") -> None:
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {str(detail)[:300]}")
        failed += 1


def private_target_unchanged(path: Path, expected: str) -> bool:
    return (
        path.read_text(encoding="utf-8") == expected
        and stat.S_IMODE(path.stat().st_mode) == 0o644
    )


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    binding_root = root / "prompt-bindings"
    binding_root.mkdir(mode=0o700)
    target = root / "key-hardlink-target"
    target.write_text("key-unchanged", encoding="utf-8")
    target.chmod(0o644)
    os.link(target, binding_root / ".commitment.key")
    hardlink_env = dict(os.environ, EXPDESIGN_HOOK_LEDGER_DIR=directory)
    rejected_key = run("capture", prompt_capture({
        "session_id": "hardlink", "prompt_id": "key",
    }, "private prompt"), hardlink_env)
    check(
        "prompt_key_hardlink_is_rejected_without_touching_target",
        rejected_key.returncode == 2 and private_target_unchanged(target, "key-unchanged"),
        rejected_key.stderr,
    )

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    binding_root = root / "prompt-bindings"
    binding_root.mkdir(mode=0o700)
    target = root / "lock-hardlink-target"
    target.write_text("lock-unchanged", encoding="utf-8")
    target.chmod(0o644)
    os.link(target, binding_root / ".bindings.lock")
    hardlink_env = dict(os.environ, EXPDESIGN_HOOK_LEDGER_DIR=directory)
    rejected_lock = run("capture", prompt_capture({
        "session_id": "hardlink", "prompt_id": "lock",
    }, "private prompt"), hardlink_env)
    check(
        "prompt_lock_hardlink_is_rejected_without_touching_target",
        rejected_lock.returncode == 2 and private_target_unchanged(target, "lock-unchanged"),
        rejected_lock.stderr,
    )

with tempfile.TemporaryDirectory() as directory:
    hardlink_env = dict(os.environ, EXPDESIGN_HOOK_LEDGER_DIR=directory)
    base = {"session_id": "hardlink", "prompt_id": "binding"}
    prompt = "private prompt"
    captured = run("capture", prompt_capture(base, prompt), hardlink_env)
    binding_file = next((Path(directory) / "prompt-bindings").glob("*.json"))
    binding_file.unlink()
    target = Path(directory) / "binding-hardlink-target"
    target.write_text("binding-unchanged", encoding="utf-8")
    target.chmod(0o644)
    os.link(target, binding_file)
    rejected_binding = run("bind", dispatch(base, prompt), hardlink_env)
    check(
        "prompt_binding_hardlink_is_rejected_without_touching_target",
        captured.returncode == 0
        and rejected_binding.returncode == 2
        and private_target_unchanged(target, "binding-unchanged"),
        rejected_binding.stderr,
    )


with tempfile.TemporaryDirectory() as parent_directory:
    fresh_root = Path(parent_directory) / "fresh-ledger-root"
    fresh_env = dict(os.environ, EXPDESIGN_HOOK_LEDGER_DIR=str(fresh_root))
    fresh_base = {"session_id": "fresh-root", "prompt_id": "capture-first"}
    captured_first = run(
        "capture", prompt_capture(fresh_base, "private prompt"), fresh_env,
    )
    recorded_second = run(
        "record",
        batch(fresh_base, [child("single-endpoint-designer", "child result")]),
        fresh_env,
    )
    check(
        "fresh_capture_first_preserves_shared_root_for_coordinator_ledger",
        captured_first.returncode == 0
        and recorded_second.returncode == 0
        and stat.S_IMODE(fresh_root.stat().st_mode) == 0o700,
        (captured_first.stderr, recorded_second.stderr),
    )


with tempfile.TemporaryDirectory() as directory:
    env = dict(os.environ, EXPDESIGN_HOOK_LEDGER_DIR=directory)

    raw_prompt = (
        "Design the exact requested study with participant code PRIVATE-984271; "
        "preserve this line byte-for-byte.\nSecond line."
    )
    binding_base = {"session_id": "binding-session", "prompt_id": "binding-prompt"}
    captured = run("capture", prompt_capture(binding_base, raw_prompt), env)
    exact = run("bind", dispatch(binding_base, raw_prompt), env)
    duplicate_child = run("bind", dispatch(binding_base, raw_prompt), env)
    distinct_planning_child = run("bind", dispatch(
        binding_base, raw_prompt, child_agent="doe-designer",
    ), env)
    incompatible_phase_child = run("bind", dispatch(
        binding_base, raw_prompt, child_agent="meta-analysis-analyst",
    ), env)
    paraphrase = run("bind", dispatch(binding_base, raw_prompt.replace("exact", "specified")), env)
    wrong_description = run("bind", dispatch(
        binding_base, raw_prompt, extra={"description": "Summarized child task"},
    ), env)
    background = run("bind", dispatch(binding_base, raw_prompt, background=True), env)
    missing_foreground_field = dispatch(binding_base, raw_prompt)
    missing_foreground_field["tool_input"].pop("run_in_background")
    omitted_background = run("bind", missing_foreground_field, env)
    unsupported_shapes = []
    for field, value in (
        ("resume", "old-agent"), ("follow_up", "continue"),
        ("model", "haiku"), ("name", "worker"),
    ):
        unsupported_shapes.append(run(
            "bind", dispatch(binding_base, raw_prompt, extra={field: value}), env,
        ))
    unapproved = run("bind", dispatch(
        binding_base, raw_prompt, child_agent="design-verifier",
    ), env)
    missing = run("bind", dispatch(
        {"session_id": "binding-session", "prompt_id": "never-captured"}, raw_prompt,
    ), env)
    replay = run("bind", dispatch(
        {"session_id": "different-session", "prompt_id": "binding-prompt"}, raw_prompt,
    ), env)
    recapture_tamper = run(
        "capture", prompt_capture(binding_base, raw_prompt + " altered"), env,
    )
    nested_capture = run("capture", {
        **prompt_capture(
            {"session_id": "binding-session", "prompt_id": "nested"}, raw_prompt,
        ),
        "agent_id": "nested-coordinator",
    }, env)
    binding_files = [path for path in Path(directory).rglob("*") if path.is_file()]
    raw_bytes = raw_prompt.encode("utf-8")
    private_absent = all(raw_bytes not in path.read_bytes() for path in binding_files)
    private_modes = all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in binding_files)
    check("exact_current_user_prompt_is_bound_before_dispatch",
          captured.returncode == exact.returncode == 0,
          (captured.stderr, exact.stderr))
    check("same_child_replay_is_rejected_but_distinct_same_phase_fanout_is_allowed",
          duplicate_child.returncode == 2 and distinct_planning_child.returncode == 0,
          (duplicate_child.stderr, distinct_planning_child.stderr))
    check("incompatible_phase_is_rejected_before_second_child_execution",
          incompatible_phase_child.returncode == 2, incompatible_phase_child.stderr)
    check("paraphrased_prompt_is_rejected_before_dispatch",
          paraphrase.returncode == 2, paraphrase.stderr)
    check("missing_or_cross_prompt_binding_is_rejected",
          missing.returncode == replay.returncode == 2,
          (missing.stderr, replay.stderr))
    check("capture_is_immutable_for_one_prompt_identity",
          recapture_tamper.returncode == 2, recapture_tamper.stderr)
    check("dispatch_shape_description_and_foreground_are_strict",
          wrong_description.returncode == background.returncode == omitted_background.returncode == 2
          and all(result.returncode == 2 for result in unsupported_shapes),
          (wrong_description.stderr, background.stderr, omitted_background.stderr,
           [result.stderr for result in unsupported_shapes]))
    check("unapproved_child_is_rejected_before_dispatch",
          unapproved.returncode == 2, unapproved.stderr)
    check("coordinator_cannot_run_as_nested_subagent",
          nested_capture.returncode == 2, nested_capture.stderr)
    check("private_prompt_never_enters_mode_0600_host_ledger",
          binding_files and private_absent and private_modes,
          [(str(path), oct(stat.S_IMODE(path.stat().st_mode))) for path in binding_files])

    atomic_base = {"session_id": "binding-session", "prompt_id": "atomic-dispatch"}
    run("capture", prompt_capture(atomic_base, raw_prompt), env)
    atomic_event = json.dumps(dispatch(
        atomic_base, raw_prompt, child_agent="indirect-comparison-analyst",
    ))
    atomic_processes = [subprocess.Popen(
        ["sh", str(LAUNCHER), "bind", SCOPE], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    ) for _ in range(2)]
    atomic_results = [process.communicate(atomic_event) for process in atomic_processes]
    atomic_codes = [process.returncode for process in atomic_processes]
    distinct_evidence = run("bind", dispatch(
        atomic_base, raw_prompt, child_agent="meta-analysis-analyst",
    ), env)
    atomic_state = next(
        json.loads(path.read_text(encoding="utf-8"))
        for path in (Path(directory) / "prompt-bindings").glob("*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("metadata", {}).get("prompt_id")
        == "atomic-dispatch"
    )
    atomic_terminal = run("enforce", {
        **atomic_base, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": SAFE_FAILURE,
    }, env)
    check("concurrent_same_child_claim_is_atomic_and_distinct_fanout_persists",
          sorted(atomic_codes) == [0, 2] and distinct_evidence.returncode == 0
          and atomic_state.get("dispatch_phase") == "evidence"
          and atomic_state.get("dispatched_children") == [
              "indirect-comparison-analyst", "meta-analysis-analyst",
          ] and atomic_terminal.returncode == 0,
          (atomic_codes, atomic_results, distinct_evidence.stderr, atomic_state,
           atomic_terminal.stderr))

    retained_base = {"session_id": "binding-session", "prompt_id": "blocked-stop"}
    run("capture", prompt_capture(retained_base, raw_prompt), env)
    blocked_stop = run("enforce", {
        **retained_base, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": "invented coordinator result",
    }, env)
    retained = run("bind", dispatch(retained_base, raw_prompt), env)
    clarify_base = {"session_id": "binding-session", "prompt_id": "terminal-clarify"}
    run("capture", prompt_capture(clarify_base, raw_prompt), env)
    clarify_terminal = run("enforce", {
        **clarify_base, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": 'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}',
    }, env)
    clarify_retired = run("bind", dispatch(clarify_base, raw_prompt), env)
    failure_base = {"session_id": "binding-session", "prompt_id": "terminal-failure"}
    run("capture", prompt_capture(failure_base, raw_prompt), env)
    failure_terminal = run("enforce", {
        **failure_base, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": SAFE_FAILURE,
    }, env)
    failure_retired = run("bind", dispatch(failure_base, raw_prompt), env)
    check("blocked_stop_retains_prompt_binding_for_retry",
          blocked_stop.returncode == 2 and retained.returncode == 0,
          (blocked_stop.stderr, retained.stderr))
    check("terminal_clarification_and_failure_retire_prompt_binding",
          clarify_terminal.returncode == failure_terminal.returncode == 0
          and clarify_retired.returncode == failure_retired.returncode == 2,
          (clarify_terminal.stderr, clarify_retired.stderr,
           failure_terminal.stderr, failure_retired.stderr))

    single = {"session_id": "coord", "prompt_id": "single"}
    single_prompt = "private coordinator prompt is intentionally not ledgered"
    run("capture", prompt_capture(single, single_prompt), env)
    one_report = "VERIFIED child report one"
    recorded = run("record", batch(single, [child("single-endpoint-designer", one_report)]), env)
    allowed = run("enforce", {
        **single, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": one_report,
    }, env)
    retired_after_result = run("bind", dispatch(single, single_prompt), env)
    check("single_child_exact_result_allowed", recorded.returncode == allowed.returncode == 0,
          (recorded.stderr, allowed.stderr))
    check("terminal_exact_child_result_retires_prompt_binding",
          retired_after_result.returncode == 2, retired_after_result.stderr)

    before_retention = sorted(Path(directory).rglob("*"))
    for index in range(20):
        bounded = {"session_id": "bounded-session", "prompt_id": f"turn-{index}"}
        run("capture", prompt_capture(bounded, f"bounded prompt {index}"), env)
        run("enforce", {
            **bounded, "hook_event_name": "Stop", "agent_type": SCOPE,
            "last_assistant_message": 'CLARIFICATION_REQUEST {"fields":["analysis_method"]}',
        }, env)
    after_retention = sorted(Path(directory).rglob("*"))
    binding_json = list((Path(directory) / "prompt-bindings").glob("*.json"))
    new_per_prompt_locks = [
        path for path in after_retention if path not in before_retention
        and path.name.endswith(".json.lock")
    ]
    check("terminal_turns_have_bounded_prompt_binding_retention",
          len(binding_json) == 2 and not new_per_prompt_locks,
          ([str(path) for path in binding_json], [str(path) for path in new_per_prompt_locks]))

    tamper = {"session_id": "coord", "prompt_id": "tamper"}
    run("record", batch(tamper, [child("single-endpoint-designer", one_report)]), env)
    rejected = run("enforce", {
        **tamper, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": one_report + " with coordinator interpretation",
    }, env)
    check("coordinator_paraphrase_rejected", rejected.returncode == 2, rejected.stderr)

    multi = {"session_id": "coord", "prompt_id": "multi",
             "orchestration_id": "orch-1", "task_id": "task-parent",
             "parent_task_id": "task-root", "attempt": 1}
    meta = "VERIFIED meta report"
    single_report = "VERIFIED single endpoint report"
    multi_record = run("record", batch(multi, [
        child("indirect-comparison-analyst", meta),
        child("meta-analysis-analyst", "VERIFIED second evidence report"),
    ]), env)
    expected = meta + SEPARATOR + "VERIFIED second evidence report"
    stored_lineage = any(
        isinstance(
            (metadata := json.loads(path.read_text(encoding="utf-8")).get("metadata")),
            dict,
        )
        and set(metadata) == {
            "scope", "principal", "context_commitment", "orchestration_id",
            "task_id", "parent_task_id", "attempt",
        }
        and metadata.get("scope") == SCOPE
        and metadata.get("principal") == f"main:{SCOPE}"
        and isinstance(metadata.get("context_commitment"), str)
        and len(metadata["context_commitment"]) == 64
        and metadata.get("orchestration_id") == "orch-1"
        and metadata.get("task_id") == "task-parent"
        and metadata.get("parent_task_id") == "task-root"
        and metadata.get("attempt") == 1
        for path in Path(directory).glob("*.json")
    )
    lineage_mismatch = run("enforce", {
        **multi, "attempt": 2, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": expected,
    }, env)
    multi_allow = run("enforce", {
        **multi, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": expected,
    }, env)
    check("multiple_children_join_in_registry_order",
          multi_record.returncode == multi_allow.returncode == 0
          and stored_lineage and lineage_mismatch.returncode == 2,
          (multi_record.stderr, lineage_mismatch.stderr, multi_allow.stderr))

    wrong_order = {"session_id": "coord", "prompt_id": "wrong-order"}
    run("record", batch(wrong_order, [
        child("meta-analysis-analyst", meta),
        child("indirect-comparison-analyst", single_report),
    ]), env)
    order_reject = run("enforce", {
        **wrong_order, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": meta + SEPARATOR + single_report,
    }, env)
    check("call_order_cannot_replace_registry_order", order_reject.returncode == 2,
          order_reject.stderr)

    mixed_stage = {"session_id": "coord", "prompt_id": "mixed-stage"}
    planning_report = "VERIFIED prospective planning report"
    evidence_report = "VERIFIED evidence analysis report"
    mixed_stage_record = run("record", batch(mixed_stage, [
        child("indirect-comparison-analyst", evidence_report),
        child("single-endpoint-designer", planning_report),
    ]), env)
    mixed_stage_stop = run("enforce", {
        **mixed_stage, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": planning_report + SEPARATOR + evidence_report,
    }, env)
    check("evidence_and_planning_cannot_mix_in_one_user_turn",
          mixed_stage_record.returncode == 0 and mixed_stage_stop.returncode == 2,
          (mixed_stage_record.stderr, mixed_stage_stop.stderr))
    mixed_stage_terminal = run("enforce", {
        **mixed_stage, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": SAFE_FAILURE,
    }, env)
    check("ledgered_phase_mix_can_end_with_value_free_failure_and_cleanup",
          mixed_stage_terminal.returncode == 0, mixed_stage_terminal.stderr)

    for label, bad_call in (
        ("unapproved_child", child("design-verifier", one_report)),
        ("async_child", child("single-endpoint-designer", one_report, background=True)),
        ("missing_child_output", child("single-endpoint-designer", None)),
        ("malformed_child_output", child("single-endpoint-designer", {"result": one_report})),
    ):
        base = {"session_id": "coord", "prompt_id": label}
        result = run("record", batch(base, [bad_call]), env)
        check(f"{label}_record_fails_closed", result.returncode == 2, result.stderr)

    strict_post_base = {"session_id": "coord", "prompt_id": "posttool-shape"}
    posttool_extra = child("single-endpoint-designer", one_report)
    posttool_extra["tool_input"]["resume"] = "old-agent"
    strict_post = run("record", batch(strict_post_base, [posttool_extra]), env)
    check("posttoolbatch_independently_rejects_agent_input_extras",
          strict_post.returncode == 2, strict_post.stderr)

    mixed = {"session_id": "coord", "prompt_id": "mixed"}
    mixed_record = run("record", batch(mixed, [
        child("single-endpoint-designer", one_report),
        child("meta-analysis-analyst", SAFE_FAILURE),
    ]), env)
    laundering = run("enforce", {
        **mixed, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": one_report,
    }, env)
    terminal = run("enforce", {
        **mixed, "hook_event_name": "Stop", "agent_type": SCOPE,
        "last_assistant_message": SAFE_FAILURE,
    }, env)
    check("failed_child_cannot_be_laundered_by_sibling",
          mixed_record.returncode == 0 and laundering.returncode == 2 and terminal.returncode == 0,
          (mixed_record.stderr, laundering.stderr, terminal.stderr))

    no_ledger = {"session_id": "coord", "prompt_id": "clarify"}
    clarify = run("enforce", {
        **no_ledger, "hook_event_name": "Stop", "agent_type": SCOPE,
        "transcript_path": "/tmp/untrusted-transcript.jsonl",
        "last_assistant_message": 'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}',
    }, env)
    check("strict_clarification_without_child_is_allowed", clarify.returncode == 0, clarify.stderr)

    # A hostile PATH python and import-time sitecustomize must not execute. The
    # Node launcher selects an approved absolute interpreter and strips PYTHON*.
    hostile = Path(directory) / "hostile"
    hostile.mkdir()
    marker = Path(directory) / "hostile-ran"
    fake_python = hostile / "python3"
    fake_python.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    node_marker = Path(directory) / "hostile-node-ran"
    fake_node = hostile / "node"
    fake_node.write_text(
        f"#!/bin/sh\ntouch {node_marker}\nexit 0\n", encoding="utf-8",
    )
    fake_node.chmod(fake_node.stat().st_mode | stat.S_IXUSR)
    (hostile / "sitecustomize.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('sitecustomize')\n",
        encoding="utf-8",
    )
    node = shutil.which("node")
    hostile_env = dict(
        env, PATH=f"{hostile}:{Path(node).parent if node else '/usr/bin'}:/usr/bin:/bin",
        PYTHONPATH=str(hostile), PYTHONSTARTUP=str(hostile / "sitecustomize.py"),
    )
    hostile_env.pop("EXPDESIGN_NODE", None)
    hostile_input = {
        "session_id": "coord", "prompt_id": "hostile",
        "hook_event_name": "PostToolBatch", "tool_calls": [],
    }
    hostile_run = subprocess.run(
        [str(node), str(NODE_LAUNCHER), "record", SCOPE],
        input=json.dumps(hostile_input), capture_output=True, text=True, env=hostile_env,
    ) if node else None
    check("hostile_python_path_and_sitecustomize_are_ignored",
          hostile_run is not None and hostile_run.returncode == 0 and not marker.exists(),
          hostile_run.stderr if hostile_run else "node unavailable")
    hostile_shell_run = subprocess.run(
        ["/bin/sh", str(LAUNCHER), "record", SCOPE],
        input=json.dumps(hostile_input), capture_output=True, text=True, env=hostile_env,
    )
    preload_marker = Path(directory) / "hostile-node-preload-ran"
    preload = Path(directory) / "hostile-preload.cjs"
    preload.write_text(
        f"require('fs').writeFileSync({str(preload_marker)!r}, 'preloaded')\n",
        encoding="utf-8",
    )
    hostile_preload_env = dict(
        hostile_env,
        NODE_OPTIONS=f"--require={preload}",
        NODE_PATH=str(hostile),
    )
    hostile_preload_run = subprocess.run(
        ["/bin/sh", str(LAUNCHER), "record", SCOPE],
        input=json.dumps(hostile_input), capture_output=True, text=True,
        env=hostile_preload_env,
    )
    relative_node_env = dict(hostile_env, EXPDESIGN_NODE="node")
    relative_node_run = subprocess.run(
        ["/bin/sh", str(LAUNCHER), "record", SCOPE],
        input=json.dumps(hostile_input), capture_output=True, text=True,
        env=relative_node_env,
    )
    check("shell_launcher_ignores_hostile_path_node_and_rejects_relative_override",
          hostile_shell_run.returncode == 0 and hostile_preload_run.returncode == 0
          and relative_node_run.returncode == 2 and not node_marker.exists()
          and not preload_marker.exists(),
          (hostile_shell_run.stderr, hostile_preload_run.stderr,
           relative_node_run.stderr))

    wrong_enforce_event = run("enforce", {
        "session_id": "coord", "prompt_id": "wrong-enforce-event",
        "hook_event_name": "PreToolUse", "agent_type": SCOPE,
        "last_assistant_message": SAFE_FAILURE,
    }, env)
    coordinator_subagent_stop = run("enforce", {
        "session_id": "coord", "prompt_id": "coordinator-subagent-stop",
        "hook_event_name": "SubagentStop", "agent_type": SCOPE,
        "last_assistant_message": SAFE_FAILURE,
    }, env)
    check("launcher_rejects_wrong_enforce_event",
          wrong_enforce_event.returncode == 2, wrong_enforce_event.stderr)
    check("coordinator_rejects_subagent_stop_identity",
          coordinator_subagent_stop.returncode == 2,
          coordinator_subagent_stop.stderr)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
raise SystemExit(1 if failed else 0)
