#!/usr/bin/env python3
"""Live contract tests for the synchronous PostToolBatch verification ledger."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "agent-harness"))
sys.path.insert(0, str(ROOT / "hooks"))

from final_report import canonical_report, privacy_safe_view  # noqa: E402
from gates import (GateVerdict, MANUAL_CHECKS, check_regression_tests,
                   combined_gate, design_checks_for)  # noqa: E402
import private_state  # noqa: E402
import verification_ledger  # noqa: E402
from verification import (VerificationIdentity, canonical_json, content_hash,
                          envelope_from_verdict, public_arguments_hash)  # noqa: E402

LAUNCHER = ROOT / "hooks" / "launch_verification.sh"
SERVER = "mcp__experiment-design__"
LEDGER_KEY_DOMAIN = "experiment-design-verification-ledger-key-v1"
CONTEXT_COMMITMENT_DOMAIN = "experiment-design-verification-ledger-context-v1"
RT_OK = {
    "all_ok": True, "suite_count": 5, "passed_suite_count": 5,
    "failed_suite_count": 0,
    "checks": {"declared_all_ok": True, "complete_suite_set": True,
               "suite_records_valid": True, "expected_check_count": True},
}


def framed_digest(domain: str, *components: str) -> str:
    values = (domain, *components)
    framed = bytearray(len(values).to_bytes(4, "big"))
    for value in values:
        encoded = value.encode("utf-8")
        framed.extend(len(encoded).to_bytes(8, "big"))
        framed.extend(encoded)
    return hashlib.sha256(framed).hexdigest()


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
    envelope["identity"].pop("args_hash", None)
    envelope["identity"]["public_args_hash"] = public_arguments_hash(tool, args)
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


with tempfile.TemporaryDirectory() as temporary:
    race_root = Path(temporary)
    configured = race_root / "private"
    pinned = race_root / "private-pinned"
    decoy = race_root / "decoy"
    decoy.mkdir(mode=0o700)
    with private_state.locked_private_directory(
        configured, ".lock", "race directory", "race lock",
    ) as directory_fd:
        configured.rename(pinned)
        configured.symlink_to(decoy, target_is_directory=True)
        private_state.atomic_write_bytes_at(
            directory_fd, "state.json", b"pinned", "race state",
        )
        private_state.append_bytes_at(
            directory_fd, "trace.jsonl", b"pinned\n", "race trace",
        )
    check(
        "directory_rename_and_symlink_swap_cannot_redirect_locked_write",
        (pinned / "state.json").read_bytes() == b"pinned"
        and (pinned / "trace.jsonl").read_bytes() == b"pinned\n"
        and not (decoy / "state.json").exists()
        and not (decoy / "trace.jsonl").exists()
        and stat.S_IMODE((pinned / "state.json").stat().st_mode) == 0o600
        and (pinned / "state.json").stat().st_nlink == 1,
    )

with tempfile.TemporaryDirectory() as temporary:
    hardlink_root = Path(temporary) / "private"
    external = Path(temporary) / "external-link"
    raised = False
    temp_paths: list[Path] = []
    original_open = private_state.open_owned_regular_at
    original_write = private_state._write_all

    def remember_temp(
        pinned_fd: int, name: str, flags: int, label: str,
        mode: int = 0o600,
    ) -> int:
        result = original_open(pinned_fd, name, flags, label, mode)
        if name.endswith(".tmp"):
            temp_paths.append(hardlink_root / name)
        return result

    def inject_hardlink(fd: int, value: bytes) -> None:
        original_write(fd, value)
        os.link(temp_paths[-1], external)

    try:
        with private_state.locked_private_directory(
            hardlink_root, ".lock", "hardlink directory", "hardlink lock",
        ) as directory_fd:
            private_state.open_owned_regular_at = remember_temp
            private_state._write_all = inject_hardlink
            try:
                private_state.atomic_write_bytes_at(
                    directory_fd, "state.json", b"blocked", "hardlink state",
                )
            except OSError:
                raised = True
            finally:
                private_state._write_all = original_write
                private_state.open_owned_regular_at = original_open
    finally:
        private_state._write_all = original_write
        private_state.open_owned_regular_at = original_open
        if external.exists():
            external.unlink()

    leftovers = sorted(path.name for path in hardlink_root.iterdir())
    check(
        "post_write_hardlink_is_rejected_and_temp_state_is_cleaned",
        raised and leftovers == [".lock"],
        leftovers,
    )


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

    security_call = [{
        "tool_name": SERVER + "run_tests", "tool_input": {},
        "tool_response": json.dumps(RT_OK),
    }]

    with tempfile.TemporaryDirectory() as collision_directory:
        collision_env = dict(env, EXPDESIGN_HOOK_LEDGER_DIR=collision_directory)
        collision_principal = "subagent:experiment-designer:collision-agent"
        first = {
            "session_id": "collision-session-a",
            "prompt_id": "collision-prompt-a",
            "agent_id": "collision-agent",
        }
        second = {
            "session_id": "collision-session-b",
            "prompt_id": "collision-prompt-b",
            "agent_id": "collision-agent",
        }
        first_record = run("record", batch(first, 1, security_call), collision_env)
        second_record = run("record", batch(second, 1, security_call), collision_env)
        first_path = Path(collision_directory) / (
            framed_digest(
                LEDGER_KEY_DOMAIN,
                first["session_id"], first["prompt_id"], collision_principal,
            ) + ".json"
        )
        second_path = Path(collision_directory) / (
            framed_digest(
                LEDGER_KEY_DOMAIN,
                second["session_id"], second["prompt_id"], collision_principal,
            ) + ".json"
        )
        first_ledger = json.loads(first_path.read_text(encoding="utf-8"))
        expected_commitment = framed_digest(
            CONTEXT_COMMITMENT_DOMAIN,
            first["session_id"], first["prompt_id"], collision_principal,
        )
        check(
            "ledger_uses_length_framed_key_and_non_raw_context_commitment",
            first_record.returncode == second_record.returncode == 0
            and first_path != second_path and first_path.exists() and second_path.exists()
            and first_ledger["metadata"].get("context_commitment") == expected_commitment
            and "session_id" not in first_ledger["metadata"]
            and "prompt_id" not in first_ledger["metadata"],
            (first_record.stderr, second_record.stderr, first_ledger.get("metadata")),
        )

        malformed_contexts = [
            {**first, "session_id": "collision-session\0a"},
            {**first, "prompt_id": "collision-prompt\0a"},
            {**first, "agent_id": "collision-agent\0a"},
            # These two contexts had identical delimiter-joined key bytes before
            # the key framing and NUL boundary checks were introduced.
            {**first, "session_id": "a", "prompt_id": "b\0c"},
            {**first, "session_id": "a\0b", "prompt_id": "c"},
        ]
        malformed_results = [
            run("record", batch(value, index + 2, security_call), collision_env)
            for index, value in enumerate(malformed_contexts)
        ]
        check(
            "session_prompt_and_principal_nul_components_are_rejected",
            all(result.returncode == 2 for result in malformed_results)
            and all("must not contain NUL" in result.stderr for result in malformed_results)
            and len(list(Path(collision_directory).glob("*.json"))) == 2,
            [(result.returncode, result.stderr) for result in malformed_results],
        )

        # Simulate a same-owner copy/path-substitution attack: the copied file
        # is structurally valid and has the same governed principal, but belongs
        # to a different session/prompt identity. Reads must validate the stored
        # opaque commitment rather than trusting only the selected filename.
        second_path.write_bytes(first_path.read_bytes())
        second_path.chmod(0o600)
        cross_bind_rejected = False
        previous_ledger_root = os.environ.get("EXPDESIGN_HOOK_LEDGER_DIR")
        try:
            os.environ["EXPDESIGN_HOOK_LEDGER_DIR"] = collision_directory
            try:
                verification_ledger.read_lines({
                    **second,
                    "hook_event_name": "SubagentStop",
                    "_expdesign_agent_scope": "experiment-designer",
                })
            except ValueError as exc:
                cross_bind_rejected = (
                    str(exc) == "verification ledger context commitment mismatch"
                )
        finally:
            if previous_ledger_root is None:
                os.environ.pop("EXPDESIGN_HOOK_LEDGER_DIR", None)
            else:
                os.environ["EXPDESIGN_HOOK_LEDGER_DIR"] = previous_ledger_root
        check(
            "copied_ledger_cannot_cross_bind_a_different_context",
            cross_bind_rejected,
        )

    with tempfile.TemporaryDirectory() as principal_directory:
        principal_env = dict(env, EXPDESIGN_HOOK_LEDGER_DIR=principal_directory)
        main_context = {
            "session_id": "principal-session",
            "prompt_id": "principal-prompt",
        }
        main_record = run(
            "record", batch(main_context, 1, security_call), principal_env,
        )
        null_main_record = run(
            "record", batch({**main_context, "agent_id": None}, 2, security_call),
            principal_env,
        )
        invalid_agent_ids = ["", 0, False, [], {}, ["nested"]]
        invalid_records = [
            run(
                "record",
                batch({**main_context, "agent_id": agent_id}, index + 3, security_call),
                principal_env,
            )
            for index, agent_id in enumerate(invalid_agent_ids)
        ]
        invalid_subagent_stops = [
            run("enforce", {
                **main_context,
                **({} if agent_id == "__missing__" else {"agent_id": agent_id}),
                "hook_event_name": "SubagentStop",
                "agent_type": "experiment-designer",
                "last_assistant_message": "Verification failed; results withheld as not trustworthy.",
            }, principal_env)
            for agent_id in ["__missing__", None, *invalid_agent_ids]
        ]
        stop_with_subagent_id = run("enforce", {
            **main_context,
            "agent_id": "valid-subagent-id",
            "hook_event_name": "Stop",
            "agent_type": "experiment-designer",
            "last_assistant_message": "Verification failed; results withheld as not trustworthy.",
        }, principal_env)

        principal = "main:experiment-designer"
        main_path = Path(principal_directory) / (
            framed_digest(
                LEDGER_KEY_DOMAIN,
                main_context["session_id"], main_context["prompt_id"], principal,
            ) + ".json"
        )
        main_ledger = json.loads(main_path.read_text(encoding="utf-8"))
        direct_cross_bind_rejected = True
        previous_ledger_root = os.environ.get("EXPDESIGN_HOOK_LEDGER_DIR")
        try:
            os.environ["EXPDESIGN_HOOK_LEDGER_DIR"] = principal_directory
            for agent_id in ["__missing__", None, *invalid_agent_ids]:
                context = {
                    **main_context,
                    **({} if agent_id == "__missing__" else {"agent_id": agent_id}),
                    "hook_event_name": "SubagentStop",
                    "_expdesign_agent_scope": "experiment-designer",
                }
                try:
                    verification_ledger.read_lines(context)
                    direct_cross_bind_rejected = False
                except ValueError:
                    pass
        finally:
            if previous_ledger_root is None:
                os.environ.pop("EXPDESIGN_HOOK_LEDGER_DIR", None)
            else:
                os.environ["EXPDESIGN_HOOK_LEDGER_DIR"] = previous_ledger_root

        check(
            "falsey_and_nonstring_agent_ids_cannot_cross_bind_main_ledger",
            main_record.returncode == null_main_record.returncode == 0
            and all(result.returncode == 2 for result in invalid_records)
            and all(result.returncode == 2 for result in invalid_subagent_stops)
            and stop_with_subagent_id.returncode == 2
            and direct_cross_bind_rejected
            and len(list(Path(principal_directory).glob("*.json"))) == 1
            and main_ledger.get("batch_count") == 2
            and main_ledger.get("metadata", {}).get("principal") == principal,
            (
                [result.stderr for result in invalid_records],
                [result.stderr for result in invalid_subagent_stops],
                stop_with_subagent_id.stderr,
                main_ledger,
            ),
        )

        null_main_stop = run("enforce", {
            **main_context,
            "agent_id": None,
            "hook_event_name": "Stop",
            "agent_type": "experiment-designer",
            "last_assistant_message": "Verification failed; results withheld as not trustworthy.",
        }, principal_env)
        check(
            "explicit_null_agent_id_is_main_only_for_main_capable_event",
            null_main_stop.returncode == 0,
            null_main_stop.stderr,
        )

    with tempfile.TemporaryDirectory() as real_root_directory, \
            tempfile.TemporaryDirectory() as symlink_parent_directory:
        symlink_root = Path(symlink_parent_directory) / "ledger-root"
        symlink_root.symlink_to(real_root_directory, target_is_directory=True)
        symlink_root_result = run("record", batch({
            "session_id": "security", "prompt_id": "symlink-root",
            "agent_id": "security-agent",
        }, 1, security_call), dict(env, EXPDESIGN_HOOK_LEDGER_DIR=str(symlink_root)))
    check("symlink_ledger_root_is_rejected",
          symlink_root_result.returncode == 2, symlink_root_result.stderr)

    with tempfile.TemporaryDirectory() as lock_root_directory:
        lock_root = Path(lock_root_directory)
        lock_target = lock_root / "lock-target"
        lock_target.write_text("unchanged", encoding="utf-8")
        (lock_root / ".verification.lock").symlink_to(lock_target)
        lock_result = run("record", batch({
            "session_id": "security", "prompt_id": "symlink-lock",
            "agent_id": "security-agent",
        }, 1, security_call), dict(env, EXPDESIGN_HOOK_LEDGER_DIR=str(lock_root)))
        lock_unchanged = lock_target.read_text(encoding="utf-8") == "unchanged"
    check("symlink_ledger_lock_is_rejected_without_touching_target",
          lock_result.returncode == 2 and lock_unchanged, lock_result.stderr)

    with tempfile.TemporaryDirectory() as lock_root_directory:
        lock_root = Path(lock_root_directory)
        lock_target = lock_root / "hard-lock-target"
        lock_target.write_text("hard-lock-unchanged", encoding="utf-8")
        lock_target.chmod(0o644)
        os.link(lock_target, lock_root / ".verification.lock")
        lock_result = run("record", batch({
            "session_id": "security", "prompt_id": "hardlink-lock",
            "agent_id": "security-agent",
        }, 1, security_call), dict(env, EXPDESIGN_HOOK_LEDGER_DIR=str(lock_root)))
        lock_unchanged = (
            lock_target.read_text(encoding="utf-8") == "hard-lock-unchanged"
            and stat.S_IMODE(lock_target.stat().st_mode) == 0o644
        )
    check("hardlink_ledger_lock_is_rejected_without_touching_target",
          lock_result.returncode == 2 and lock_unchanged, lock_result.stderr)

    with tempfile.TemporaryDirectory() as data_root_directory:
        data_root = Path(data_root_directory)
        data_target = data_root / "ledger-target"
        data_target.write_text("unchanged", encoding="utf-8")
        data_base = {
            "session_id": "security", "prompt_id": "symlink-data",
            "agent_id": "security-agent",
        }
        principal = "subagent:experiment-designer:security-agent"
        digest = framed_digest(
            LEDGER_KEY_DOMAIN, "security", "symlink-data", principal,
        )
        (data_root / f"{digest}.json").symlink_to(data_target)
        data_result = run(
            "record", batch(data_base, 1, security_call),
            dict(env, EXPDESIGN_HOOK_LEDGER_DIR=str(data_root)),
        )
        data_unchanged = data_target.read_text(encoding="utf-8") == "unchanged"
    check("symlink_ledger_file_is_rejected_without_touching_target",
          data_result.returncode == 2 and data_unchanged, data_result.stderr)

    with tempfile.TemporaryDirectory() as data_root_directory:
        data_root = Path(data_root_directory)
        data_target = data_root / "hard-ledger-target"
        data_target.write_text("hard-data-unchanged", encoding="utf-8")
        data_target.chmod(0o644)
        data_base = {
            "session_id": "security", "prompt_id": "hardlink-data",
            "agent_id": "security-agent",
        }
        principal = "subagent:experiment-designer:security-agent"
        digest = framed_digest(
            LEDGER_KEY_DOMAIN, "security", "hardlink-data", principal,
        )
        os.link(data_target, data_root / f"{digest}.json")
        data_result = run(
            "record", batch(data_base, 1, security_call),
            dict(env, EXPDESIGN_HOOK_LEDGER_DIR=str(data_root)),
        )
        data_unchanged = (
            data_target.read_text(encoding="utf-8") == "hard-data-unchanged"
            and stat.S_IMODE(data_target.stat().st_mode) == 0o644
        )
    check("hardlink_ledger_file_is_rejected_without_touching_target",
          data_result.returncode == 2 and data_unchanged, data_result.stderr)

    domain_base = {"session_id": "session-ledger", "prompt_id": "domain-allowlist",
                   "agent_id": "domain-agent"}
    wrong_domain_tool = run("record", batch(domain_base, 1, [{
        "tool_name": SERVER + "randomize", "tool_input": {"n": 4},
        "tool_response": json.dumps({"n": 4}),
    }]), env, scope="single-endpoint-designer")
    allowed_domain_tool = run("record", batch({
        **domain_base, "prompt_id": "domain-allowlist-ok",
    }, 1, [{
        "tool_name": SERVER + "run_tests", "tool_input": {},
        "tool_response": json.dumps(RT_OK),
    }]), env, scope="single-endpoint-designer")
    check("domain_tool_allowlist_is_fail_closed",
          wrong_domain_tool.returncode == 2 and allowed_domain_tool.returncode == 0,
          (wrong_domain_tool.stderr, allowed_domain_tool.stderr))

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
               for batch_record in value.get("batches", [])) and any(
                   SERVER + "sample_size" in line for line in value.get("lines", [])
               ):
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
    ledger_files = [
        path for path in Path(directory).iterdir()
        if path.is_file() and not path.is_symlink()
        and (path.suffix == ".json" or path.name == ".verification.lock")
    ]
    check("ledger_root_lock_and_files_are_private",
          stat.S_IMODE(Path(directory).stat().st_mode) == 0o700
          and ledger_files
          and all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in ledger_files),
          [(str(path), oct(stat.S_IMODE(path.stat().st_mode))) for path in ledger_files])

print(f"\n--- Results: {passed} passed, {failed} failed ---")
raise SystemExit(1 if failed else 0)
