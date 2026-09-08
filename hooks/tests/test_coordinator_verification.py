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
        capture_output=True, text=True,
        env={"CLAUDE_CODE_FORK_SUBAGENT": "0", **env},
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
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**env, "CLAUDE_CODE_FORK_SUBAGENT": "0"},
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
            "last_assistant_message": SAFE_FAILURE,
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

def stop(base: dict, text: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return run("enforce", {**base, "hook_event_name": "Stop", "agent_type": SCOPE,
                           "last_assistant_message": text}, env)


def clarified(prior: list[str], current: str) -> str:
    return json.dumps({"type": "clarified_user_request", "prior_user_messages": prior,
                       "current_user_message": current}, ensure_ascii=True,
                      separators=(",", ":"))


def native_response(content: str, agent_id: str = "a4d2c8f1e0b3a297") -> list[dict]:
    return [{"type": "text", "text": content}, {"type": "text", "text": (
        f"agentId: {agent_id} (use SendMessage with to: '{agent_id}', "
        "summary: '<5-10 word recap>' to continue this agent)"
        "\n<usage>subagent_tokens: 123\ntool_uses: 2\nduration_ms: 456</usage>"
    )}]


with tempfile.TemporaryDirectory() as directory:
    env = dict(os.environ, EXPDESIGN_HOOK_LEDGER_DIR=directory)
    base = {"session_id": "runtime-mode", "prompt_id": "first"}
    for fork_mode in (None, "1", "false"):
        effective = {**env}
        effective.pop("CLAUDE_CODE_FORK_SUBAGENT", None)
        if fork_mode is not None:
            effective["CLAUDE_CODE_FORK_SUBAGENT"] = fork_mode
        result = subprocess.run(["sh", str(LAUNCHER), "capture", SCOPE],
                                input=json.dumps(prompt_capture(base, "study")),
                                capture_output=True, text=True, env=effective)
        check(f"incompatible_effective_fork_mode_{fork_mode}_blocks_before_capture",
              result.returncode == 2 and "CLAUDE_CODE_FORK_SUBAGENT=0" in result.stderr,
              result.stderr)

    for disabled in ("1", "true", " YES ", "On"):
        result = run("capture", prompt_capture(base, "study"), {
            **env, "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": disabled,
        })
        check(f"background_disabled_{disabled.strip()}_blocks_before_capture",
              result.returncode == 2 and "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS" in result.stderr,
              result.stderr)
    for disabled in ("0", "false", " NO ", "Off"):
        result = run("capture", prompt_capture(base, "study"), {
            **env, "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": disabled,
        })
        check(f"background_enabled_{disabled.strip()}_permits_capture",
              result.returncode == 0, result.stderr)

    first = {"session_id": "clarification", "prompt_id": "first"}
    second = {**first, "prompt_id": "second"}
    third = {**first, "prompt_id": "third"}
    original = "Design a two-arm study; power 0.8, alpha 0.05. Private code PRIVATE-48392."
    reply = "Binary endpoint."
    final_reply = "Control 0.2; treatment 0.35."
    question = 'CLARIFICATION_REQUEST {"fields":["endpoint_type"]}'
    captured = run("capture", prompt_capture(first, original), env)
    questioned = stop(first, question, env)
    next_capture = run("capture", prompt_capture(second, reply), env)
    check("router_clarification_keeps_bound_user_context",
          captured.returncode == questioned.returncode == next_capture.returncode == 0
          and "1 prior user message" in next_capture.stdout,
          (captured.stderr, questioned.stderr, next_capture.stderr))
    for label, request in (
        ("latest_only", reply),
        ("mutated_prior", clarified([original + " altered"], reply)),
        ("omitted_prior", clarified([], reply)),
        ("mutated_current", clarified([original], reply + " altered")),
        ("assistant_injection", clarified([original, question], reply)),
    ):
        rejected = run("bind", dispatch(second, request), env)
        check(f"clarification_{label}_rejected", rejected.returncode == 2, rejected.stderr)
    compact = clarified([original], reply)
    pretty = json.dumps(json.loads(compact), indent=2)
    accepted = run("bind", dispatch(second, pretty), env)
    update = json.loads(accepted.stdout).get("hookSpecificOutput", {}).get("updatedInput", {})
    check("host_normalizes_only_the_verified_envelope_before_execution",
          accepted.returncode == 0 and update.get("prompt") == compact,
          (accepted.stderr, accepted.stdout))
    child_question = 'CLARIFICATION_REQUEST {"fields":["null_param","alt_param"]}'
    recorded = run("record", batch(second, [child(
        "single-endpoint-designer", native_response(child_question),
    )]), env)
    child_stop = stop(second, child_question, env)
    third_capture = run("capture", prompt_capture(third, final_reply), env)
    check("child_clarification_extends_the_same_user_request",
          recorded.returncode == child_stop.returncode == third_capture.returncode == 0
          and "2 prior user message" in third_capture.stdout,
          (recorded.stderr, child_stop.stderr, third_capture.stderr))
    reordered = run("bind", dispatch(third, clarified([reply, original], final_reply)), env)
    phase_switch = run("bind", dispatch(
        third, clarified([original, reply], final_reply), child_agent="meta-analysis-analyst",
    ), env)
    check("clarification_reordering_and_phase_switch_rejected",
          reordered.returncode == phase_switch.returncode == 2,
          (reordered.stderr, phase_switch.stderr))
    final_dispatch = run("bind", dispatch(third, clarified([original, reply], final_reply)), env)
    report = "VERIFIED complete study report"
    final_record = run("record", batch(third, [child(
        "single-endpoint-designer", native_response(report),
    )]), env)
    files = [path for path in Path(directory).rglob("*") if path.is_file()]
    check("clarification_state_never_persists_raw_user_messages",
          all(original.encode() not in path.read_bytes()
              and reply.encode() not in path.read_bytes()
              and stat.S_IMODE(path.stat().st_mode) == 0o600 for path in files))
    completed = stop(third, report, env)
    fresh = {**first, "prompt_id": "unrelated"}
    fresh_capture = run("capture", prompt_capture(fresh, "New independent request"), env)
    fresh_dispatch = run("bind", dispatch(fresh, "New independent request"), env)
    check("successful_request_clears_clarification_context_for_next_request",
          final_dispatch.returncode == final_record.returncode == completed.returncode
          == fresh_capture.returncode == fresh_dispatch.returncode == 0
          and not list((Path(directory) / "prompt-bindings").glob("episode-*.json")),
          (final_dispatch.stderr, final_record.stderr, completed.stderr,
           fresh_capture.stderr, fresh_dispatch.stderr))

    failed_first = {"session_id": "failure-clears", "prompt_id": "first"}
    run("capture", prompt_capture(failed_first, original), env)
    stop(failed_first, question, env)
    failed_reply = {**failed_first, "prompt_id": "reply"}
    run("capture", prompt_capture(failed_reply, reply), env)
    terminal = stop(failed_reply, SAFE_FAILURE, env)
    after_failure = {**failed_first, "prompt_id": "fresh"}
    run("capture", prompt_capture(after_failure, "New request"), env)
    unbound = run("bind", dispatch(after_failure, "New request"), env)
    check("failure_clears_pending_clarification_context",
          terminal.returncode == unbound.returncode == 0,
          (terminal.stderr, unbound.stderr))

    old = {"session_id": "old-version", "prompt_id": "first"}
    run("capture", prompt_capture(old, original), env)
    old_file = next(path for path in (Path(directory) / "prompt-bindings").glob("*.json")
                    if json.loads(path.read_text()).get("metadata", {}).get("session_id")
                    == "old-version")
    state = json.loads(old_file.read_text())
    state["version"] = 2
    old_file.write_text(json.dumps(state))
    rejected = run("bind", dispatch(old, original), env)
    check("old_binding_version_fails_closed_without_migration",
          rejected.returncode == 2 and "context mismatch" in rejected.stderr,
          rejected.stderr)

    bounded_results = []
    for index in range(16):
        bounded = {"session_id": "clarification-limit", "prompt_id": f"turn-{index}"}
        captured = run("capture", prompt_capture(bounded, f"reply {index}"), env)
        completed = stop(bounded, question, env)
        bounded_results.append((captured.returncode, completed.returncode))
    terminal = stop(bounded, SAFE_FAILURE, env)
    check("clarification_turn_limit_blocks_and_value_free_failure_clears",
          bounded_results == [(0, 0)] * 15 + [(0, 2)] and terminal.returncode == 0,
          (bounded_results, terminal.stderr))
    too_large = run("capture", prompt_capture(
        {"session_id": "byte-limit", "prompt_id": "large"}, "x" * 131073,
    ), env)
    check("oversized_user_request_rejected_before_dispatch",
          too_large.returncode == 2 and "byte limit" in too_large.stderr,
          too_large.stderr)

    partial = {"session_id": "partial-fanout", "prompt_id": "first"}
    run("capture", prompt_capture(partial, original), env)
    run("bind", dispatch(partial, original), env)
    run("bind", dispatch(partial, original, child_agent="doe-designer"), env)
    partial_record = run("record", batch(partial, [
        child("single-endpoint-designer", report), child("doe-designer", question),
    ]), env)
    partial_stop = stop(partial, report + SEPARATOR + question, env)
    partial_next = {**partial, "prompt_id": "reply"}
    partial_capture = run("capture", prompt_capture(partial_next, reply), env)
    partial_dispatch = run("bind", dispatch(partial_next, clarified([original], reply)), env)
    check("mixed_success_and_clarification_retains_only_original_user_context",
          partial_record.returncode == partial_stop.returncode == partial_capture.returncode
          == partial_dispatch.returncode == 0,
          (partial_record.stderr, partial_stop.stderr, partial_capture.stderr,
           partial_dispatch.stderr))

    for label, contents in (("success", report), ("failure", SAFE_FAILURE),
                            ("clarification", question)):
        case = {"session_id": "native-footer", "prompt_id": label}
        record = run("record", batch(case, [child(
            "single-endpoint-designer", native_response(contents),
        )]), env)
        leaked = stop(case, "\n".join(block["text"] for block in native_response(contents)), env)
        clean = stop(case, contents, env)
        check(f"native_footer_{label}_only_child_content_is_canonical",
              record.returncode == clean.returncode == 0 and leaked.returncode == 2,
              (record.stderr, leaked.stderr, clean.stderr))

    mixed = {"session_id": "native-footer", "prompt_id": "mixed"}
    record = run("record", batch(mixed, [
        child("single-endpoint-designer", native_response(SAFE_FAILURE)),
        child("doe-designer", native_response(report, "b123")),
    ]), env)
    laundering = stop(mixed, SAFE_FAILURE + SEPARATOR + report, env)
    clean = stop(mixed, SAFE_FAILURE, env)
    check("native_failed_child_with_successful_sibling_cannot_be_laundered",
          record.returncode == clean.returncode == 0 and laundering.returncode == 2,
          (record.stderr, laundering.stderr, clean.stderr))
    footer = native_response(report)[-1]
    for label, malformed in (
        ("footer_only", [footer]),
        ("embedded_footer", [dict(type="text", text=report + "\n" + footer["text"])]),
        ("duplicated_footer", [*native_response(report), footer]),
        ("reordered_footer", [footer, dict(type="text", text=report)]),
        ("mismatched_agent_id", [dict(type="text", text=report),
                                 dict(type="text", text=footer["text"].replace("to: 'a", "to: 'b"))]),
        ("unexpected_usage", [dict(type="text", text=report),
                              dict(type="text", text=footer["text"].replace("tool_uses: 2", "tool_uses: bad"))]),
        ("unstructured_string", report + "\n" + footer["text"]),
        ("structured_content_footer", {"status": "completed", "content": native_response(report)}),
        ("async_result", {"status": "async_launched", "content": native_response(report)}),
    ):
        case = {"session_id": "native-footer", "prompt_id": label}
        rejected = run("record", batch(case, [child("single-endpoint-designer", malformed)]), env)
        check(f"ambiguous_native_metadata_{label}_fails_closed", rejected.returncode == 2,
              rejected.stderr)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
raise SystemExit(1 if failed else 0)
