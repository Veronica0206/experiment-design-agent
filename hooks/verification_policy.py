"""Fail-closed Stop/SubagentStop verification policy.

This module is intentionally separate from the hook entrypoint so policy logic
can be unit-tested without coupling it to Claude Code process wiring.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from verification_ledger import clear as clear_ledger
from verification_ledger import context_principal
from verification_ledger import governed_agent
from verification_ledger import read_lines as read_ledger_lines
from private_state import (
    append_bytes_at,
    locked_private_directory,
)
from governance.registry import RegistryError, domain_phase, get_agent


SERVER = "mcp__experiment-design__"
VERIFY_TOOL = "run_tests"
UNGATED = {"validate_config", VERIFY_TOOL}
RUNTESTS_ESCAPE = 2
DESIGN_FAIL_ESCAPE = 3
SAFE_FAILURE_MESSAGE = "Verification failed; results withheld as not trustworthy."


_TRACE_CONTEXT: dict[str, Any] = {}


def _set_trace_context(data: dict[str, Any], principal: str | None) -> None:
    scope = data.get("_expdesign_agent_scope")
    _TRACE_CONTEXT.clear()
    _TRACE_CONTEXT.update({
        "session_id": data.get("session_id") if isinstance(data.get("session_id"), str) else None,
        "prompt_id": data.get("prompt_id") if isinstance(data.get("prompt_id"), str) else None,
        "principal": principal,
        "hook_event": data.get("hook_event_name") if isinstance(data.get("hook_event_name"), str) else None,
        "orchestration_id": data.get("orchestration_id") if isinstance(data.get("orchestration_id"), str) else None,
        "task_id": data.get("task_id") if isinstance(data.get("task_id"), str) else None,
        "parent_task_id": data.get("parent_task_id") if isinstance(data.get("parent_task_id"), str) else None,
        "attempt": data.get("attempt") if isinstance(data.get("attempt"), int) else None,
    })


def _trace(outcome: str, detail: str = "") -> None:
    configured_path = os.environ.get("EXPDESIGN_HOOK_LOG")
    path = (
        Path(configured_path)
        if configured_path
        else Path(tempfile.gettempdir()) / "experiment-design-hook-traces" / "hook.jsonl"
    )
    try:
        if configured_path and not path.is_absolute():
            raise OSError("configured hook trace path must be absolute")
        with locked_private_directory(
            path.parent,
            ".trace.lock",
            "hook trace directory",
            "hook trace lock",
        ) as directory_fd:
            # Do not persist a deterministic digest of the full reason: gate
            # diagnostics may contain low-entropy parameters that remain
            # dictionary-testable after an unsalted hash.
            event = {
                "schema_version": 1,
                "timestamp": time.time(),
                "sequence": time.time_ns(),
                "outcome": outcome,
                **_TRACE_CONTEXT,
            }
            line = json.dumps(event, sort_keys=True, allow_nan=False) + "\n"
            append_bytes_at(
                directory_fd,
                path.name,
                line.encode("utf-8"),
                "hook trace",
            )
    except Exception as exc:
        # Governance must not depend on observability; the decision still runs.
        if os.environ.get("EXPDESIGN_HOOK_LOG_REQUIRED") == "1":
            sys.stderr.write(f"INTERNAL_ERROR. Required hook trace could not be written: {exc}")
            raise SystemExit(2) from exc


def _allow(reason: str) -> None:
    _trace("ALLOW", reason)
    raise SystemExit(0)


def _block(reason: str) -> None:
    _trace("BLOCK", reason)
    sys.stderr.write(reason)
    raise SystemExit(2)


def _text_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(text) if text else None
    return None


def _last_message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _text_content(value) or ""
    if isinstance(value, dict):
        return _text_content(value.get("content")) or str(value.get("text", ""))
    return ""


def _safe_failure_report(message: str) -> bool:
    return " ".join(message.split()) == SAFE_FAILURE_MESSAGE


def _coordinator_child_phase(child: str) -> str:
    """Classify a child through the registry domain, not a name convention."""
    try:
        entry = get_agent(child)
        if entry.get("role") != "domain_executor":
            raise RegistryError("coordinator children must be domain executors")
        return domain_phase(str(entry.get("domain")))
    except RegistryError as exc:
        raise ValueError(str(exc)) from exc


def _safe_partial_report(message: str, limitations: list[str]) -> bool:
    lower = message.lower()
    labelled = ("pass_partial" in lower or "partial verification" in lower) and any(
        word in lower for word in ("not checked", "blocked", "limitation")
    )
    return labelled and all(" ".join(item.lower().split()) in " ".join(lower.split())
                            for item in limitations)


def _load_gates() -> dict[str, Any]:
    harness_dir = Path(__file__).resolve().parent.parent / "agent-harness"
    sys.path.insert(0, str(harness_dir))
    from gates import (  # type: ignore
        GateVerdict,
        MANUAL_CHECKS,
        check_regression_tests,
        combined_gate,
    )
    from verification import (canonical_json, content_hash, domain_arguments,
                              public_check_summary,
                              public_envelope_matches_call,
                              public_limitation_codes,
                              public_note_codes)  # type: ignore
    from final_report import (  # type: ignore
        canonical_public_report,
        is_elicitation_message,
        join_reports,
        normalize_report_text,
    )
    return {
        "GateVerdict": GateVerdict,
        "MANUAL_CHECKS": public_limitation_codes(list(MANUAL_CHECKS)),
        "check_regression_tests": check_regression_tests,
        "combined_gate": combined_gate,
        "content_hash": content_hash,
        "canonical_json": canonical_json,
        "domain_arguments": domain_arguments,
        "public_envelope_matches_call": public_envelope_matches_call,
        "public_check_summary": public_check_summary,
        "public_limitation_codes": public_limitation_codes,
        "public_note_codes": public_note_codes,
        "canonical_public_report": canonical_public_report,
        "join_reports": join_reports,
        "normalize_report_text": normalize_report_text,
        "is_elicitation_message": is_elicitation_message,
    }


def _last_assistant_text(lines: list[str]) -> str:
    latest = ""
    for raw in lines:
        try:
            row = json.loads(raw)
        except Exception:
            continue
        message = row.get("message", row) if isinstance(row, dict) else {}
        if isinstance(message, dict) and message.get("role") == "assistant":
            text = _text_content(message.get("content"))
            if text:
                latest = text
    return latest


def _parse_transcript(
    lines: list[str], api: dict[str, Any]
) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    groups: dict[str, int] = {}
    calls: list[dict] = []
    runtests: list[dict] = []
    ungated: list[dict] = []
    results: dict[str, dict] = {}
    result_errors: dict[str, str] = {}
    errors: list[str] = []

    def group_of(message_id: str) -> int:
        if message_id not in groups:
            groups[message_id] = len(groups) + 1
        return groups[message_id]

    for line_number, raw in enumerate(lines):
        try:
            row = json.loads(raw)
        except Exception:
            if raw.strip():
                errors.append(f"line {line_number + 1}: invalid transcript JSON")
            continue
        message = row.get("message", row) if isinstance(row, dict) else {}
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            continue
        group = group_of(str(message.get("id") or f"line-{line_number}"))
        for block in message["content"]:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = str(block.get("name", ""))
                if not name.startswith(SERVER):
                    continue
                tool = name[len(SERVER):]
                target = (
                    runtests if tool == VERIFY_TOOL
                    else ungated if tool in UNGATED
                    else calls
                )
                target.append({
                    "group": group,
                    "id": str(block.get("id") or ""),
                    "tool": tool,
                    "args": block.get("input") or {},
                })
            elif block.get("type") == "tool_result":
                call_id = str(block.get("tool_use_id") or "")
                text = _text_content(block.get("content"))
                if call_id and text:
                    try:
                        parsed = json.loads(text)
                        if isinstance(parsed, dict):
                            results[call_id] = parsed
                        else:
                            result_errors[call_id] = "non-object JSON payload"
                    except Exception:
                        result_errors[call_id] = "non-JSON or malformed payload"
                elif call_id:
                    result_errors[call_id] = "missing text payload"

    for call in calls + runtests + ungated:
        call["result"] = results.get(call["id"])
        call["result_error"] = result_errors.get(call["id"])
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        call["args_hash"] = api["content_hash"](api["domain_arguments"](args))
        result = call.get("result") if isinstance(call.get("result"), dict) else {}
        envelope = result.get("_verification") if isinstance(result, dict) else None
        identity = envelope.get("identity") if isinstance(envelope, dict) else None
        call["verification"] = envelope
        call["provenance"] = (
            result.get("_provenance")
            if isinstance(result.get("_provenance"), dict) else {}
        )
        call["analysis_id"] = str(identity.get("analysis_id") if isinstance(identity, dict) else "")
        call["raw_result"] = {
            key: value for key, value in result.items()
            if key not in {"_verification", "_provenance", "_private_resources"}
        } if isinstance(result, dict) else None
    return calls, runtests, ungated, errors


def _expected_final(api: dict[str, Any], latest: list[tuple[dict, Any]]) -> str:
    reports: list[str] = []
    for call, verdict in latest:
        server_envelope = call.get("verification") or {}
        reports.append(api["canonical_public_report"](
            call["tool"], call.get("args") or {}, call.get("raw_result") or {},
            server_envelope, server_envelope, call.get("provenance") or {},
        ))
    return api["join_reports"](reports)


def _is_stochastic(tool: str, args: dict) -> bool:
    return (
        tool in {"simulate_design", "master_simulate", "randomize"}
        or (tool in {"factorial_design", "rsm_design"} and args.get("randomize") is True)
    )


def _terminal_or_block(data: dict, reason: str, status: str) -> None:
    message = _last_message_text(data.get("last_assistant_message"))
    if data.get("stop_hook_active") is True and _safe_failure_report(message):
        clear_ledger(data)
        _allow(f"{status}: value-free failure report accepted")
    _block(
        f"{status}. {reason} Return a value-free failure report that says "
        f"exactly: {SAFE_FAILURE_MESSAGE}"
    )


def _coordinator_calls(lines: list[str], allowed_children: list[str]) -> list[dict[str, str]]:
    """Parse only the sanitized Agent-call pairs emitted by the trusted ledger."""
    if len(lines) % 2:
        raise ValueError("coordinator ledger contains an unpaired child call")
    calls: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_children: set[str] = set()
    for offset in range(0, len(lines), 2):
        try:
            use_row = json.loads(lines[offset])
            result_row = json.loads(lines[offset + 1])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("coordinator ledger contains malformed JSON") from exc
        use_message = use_row.get("message") if isinstance(use_row, dict) else None
        result_message = result_row.get("message") if isinstance(result_row, dict) else None
        use_blocks = use_message.get("content") if isinstance(use_message, dict) else None
        result_blocks = result_message.get("content") if isinstance(result_message, dict) else None
        if (
            not isinstance(use_blocks, list) or len(use_blocks) != 1
            or not isinstance(result_blocks, list) or len(result_blocks) != 1
            or not isinstance(use_blocks[0], dict) or not isinstance(result_blocks[0], dict)
        ):
            raise ValueError("coordinator ledger contains malformed child records")
        use = use_blocks[0]
        result = result_blocks[0]
        call_id = use.get("id")
        args = use.get("input")
        if (
            use.get("type") != "tool_use" or use.get("name") != "Agent"
            or not isinstance(call_id, str) or not call_id or call_id in seen_ids
            or not isinstance(args, dict)
            or set(args) != {"subagent_type"}
            or args.get("subagent_type") not in allowed_children
        ):
            raise ValueError("coordinator ledger contains an unapproved or asynchronous child call")
        child = str(args["subagent_type"])
        if child in seen_children:
            raise ValueError("coordinator may not present duplicate child results")
        if (
            result.get("type") != "tool_result" or result.get("tool_use_id") != call_id
        ):
            raise ValueError("coordinator child response does not match its call")
        content = result.get("content")
        final = _text_content(content)
        if not isinstance(final, str) or not final.strip() or final.startswith("EXPDESIGN_LEDGER_ERROR:"):
            raise ValueError("coordinator child response lacks completed final content")
        seen_ids.add(call_id)
        seen_children.add(child)
        calls.append({"child": child, "final": final})
    return calls


def _enforce_coordinator(
    data: dict[str, Any], line_source: Callable[[dict[str, Any]], list[str]],
    agent: dict[str, Any], api: dict[str, Any],
) -> None:
    message = _last_message_text(data.get("last_assistant_message"))
    try:
        lines = line_source(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        if api["is_elicitation_message"](message):
            clear_ledger(data)
            _allow("coordinator has no child ledger; explicit open-ended elicitation")
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("coordinator has no child ledger; value-free failure report")
        _block(f"INTERNAL_ERROR. The coordinator ledger could not be read: {exc}")

    try:
        calls = _coordinator_calls(lines, list(agent["allowed_children"]))
    except ValueError as exc:
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("malformed child workflow ended with value-free failure report")
        _block(f"INTERNAL_ERROR. {exc}")

    if not calls:
        if api["is_elicitation_message"](message):
            clear_ledger(data)
            _allow("coordinator made no child call; explicit open-ended elicitation")
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("coordinator made no child call; value-free failure report")
        _block("UNBOUND_FREE_TEXT. Coordinator output is not bound to a completed child result.")

    failures = [call for call in calls if _safe_failure_report(call["final"])]
    if failures:
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("child failure propagated as the value-free failure report")
        _block(
            "UNVERIFIED_ESCAPE. A failed child cannot be combined with or laundered by "
            "a successful sibling; return only the value-free failure report."
        )

    try:
        selected_phases = {_coordinator_child_phase(call["child"]) for call in calls}
    except ValueError as exc:
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("invalid child phase ended with value-free failure report")
        _block(f"INTERNAL_ERROR. {exc}")
    if len(selected_phases) > 1:
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("mixed workflow phases ended with value-free failure report")
        _block(
            "SEQUENTIAL_BOUNDARY. Evidence analysis and prospective planning may not "
            "run in the same user turn; return the evidence request first and require "
            "a new user prompt that explicitly confirms any downstream design input."
        )

    order = {name: index for index, name in enumerate(agent["allowed_children"])}
    ordered = sorted(calls, key=lambda call: order[call["child"]])
    expected = ordered[0]["final"] if len(ordered) == 1 else "\n\n---\n\n".join(
        call["final"] for call in ordered
    )
    if message != expected:
        _block(
            "VERIFIED. Coordinator output must copy the completed child result exactly, "
            "or join multiple results in registry order with blank-line horizontal-rule separators."
        )
    clear_ledger(data)
    _allow(f"VERIFIED: {len(ordered)} exact child result(s)")


def enforce(
    data: dict[str, Any],
    line_source: Callable[[dict[str, Any]], list[str]] = read_ledger_lines,
) -> None:
    """Enforce policy against a trusted synchronous-ledger source.

    Tests may inject an in-memory ledger reader. Production callers never read
    transcript paths or environment-selected alternate trust sources.
    """
    scope = data.get("_expdesign_agent_scope")
    try:
        if data.get("hook_event_name") not in {"Stop", "SubagentStop"}:
            raise ValueError("verification enforcement accepts only Stop or SubagentStop")
        agent = governed_agent(scope)
        principal = context_principal(data, scope)
    except ValueError as exc:
        _set_trace_context(data, None)
        _block(f"INTERNAL_ERROR. {exc}")
    _set_trace_context(data, principal)

    try:
        api = _load_gates()
    except Exception as exc:
        _terminal_or_block(data, f"Verification gates could not load: {exc}.", "INTERNAL_ERROR")

    if agent["ledger_policy"] == "coordinator":
        _enforce_coordinator(data, line_source, agent, api)
        return

    try:
        lines = line_source(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        message = _last_message_text(data.get("last_assistant_message"))
        if api["is_elicitation_message"](message):
            _allow("no ledger; explicit open-ended elicitation")
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("no ledger; value-free failure report")
        _terminal_or_block(data, f"The synchronous verification ledger could not be read: {exc}.", "INTERNAL_ERROR")

    calls, runtests, ungated, parse_errors = _parse_transcript(lines, api)
    if parse_errors:
        _terminal_or_block(data, "; ".join(parse_errors[:5]), "INTERNAL_ERROR")
    allowed_tools = set(agent["tools"])
    if f"{SERVER}*" not in allowed_tools:
        used_tools = {f"{SERVER}{item['tool']}" for item in calls + runtests + ungated}
        outside = sorted(used_tools - allowed_tools)
        if outside:
            _terminal_or_block(
                data, f"Tools outside the registered agent allowlist were used: {outside}.",
                "INTERNAL_ERROR",
            )
    if not calls:
        message = _last_message_text(data.get("last_assistant_message")) or _last_assistant_text(lines)
        validate_history = [
            item for item in ungated if item.get("tool") == "validate_config"
        ]
        if validate_history:
            latest_config = validate_history[-1]
            result = latest_config.get("result")
            fields = {
                "valid", "endpoint_type", "study_type", "design",
                "go_target", "alphas", "powers", "resolved_config",
                "simulation_defaults", "configuration_report",
            }
            resolved_fields = {
                "endpoint_type", "study_type", "design", "estimand", "direction",
                "sidedness", "null_param", "alt_param", "sd", "alloc_ratio",
                "alphas", "powers", "prior_params", "go_threshold",
                "consider_threshold", "go_target", "p3_n", "p3_alloc_ratio",
                "p3_alpha", "accrual_time", "followup_time", "tte_method",
                "exposure_time", "rate_method", "has_p2_data",
                "has_p2_control_data",
            }
            if (latest_config.get("result_error") is None
                    and isinstance(result, dict) and set(result) == fields
                    and isinstance(result.get("resolved_config"), dict)
                    and set(result["resolved_config"]) == resolved_fields
                    and isinstance(result.get("simulation_defaults"), dict)
                    and set(result["simulation_defaults"]) == {"seed", "b_oc"}
                    and result.get("valid") is True):
                core = {key: result[key] for key in fields - {"configuration_report"}}
                expected = "CONFIGURATION_VALIDATED " + api["canonical_json"](core)
                if result.get("configuration_report") != expected:
                    _terminal_or_block(
                        data, "The configuration report was malformed.", "INTERNAL_ERROR")
                if api["normalize_report_text"](message) != expected:
                    _terminal_or_block(
                        data,
                        "A validate_config-only turn must copy configuration_report exactly; "
                        "it authorizes no statistical result or additional prose.",
                        "VALIDATED_CONFIG",
                    )
                clear_ledger(data)
                _allow("validate_config-only turn; exact configuration report accepted")
        if api["is_elicitation_message"](message):
            _allow("no gated calls; explicit open-ended elicitation")
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("no gated calls; value-free failure report")
        _block("UNBOUND_FREE_TEXT. No verified analysis authorizes this answer; ask only explicit open-ended questions.")

    # A withheld, missing, or malformed result can be superseded only by a later
    # presentable call to the same analysis tool. Success from another tool must
    # never launder it.
    unresolved_withheld = [call for call in calls if (
        call.get("result_error") is not None
        or not isinstance(call.get("verification"), dict)
        or call["verification"].get("presentable") is False
    ) and not any(
        later["group"] > call["group"]
        and later["tool"] == call["tool"]
        and later.get("result_error") is None
        and isinstance(later.get("verification"), dict)
        and later["verification"].get("presentable") is True
        for later in calls
    )]
    if unresolved_withheld:
        # A safe failure sentence is a terminal escape, not a way to bypass the
        # documented retry budget on the second Stop-hook invocation. Count
        # distinct assistant message groups so parallel fan-out remains one
        # attempt. Malformed/missing transport envelopes may terminate through
        # INTERNAL_ERROR immediately; ordinary server-rejected designs require
        # the advertised number of attempts.
        design_rejections = [call for call in unresolved_withheld if (
            call.get("result_error") is None
            and isinstance(call.get("verification"), dict)
            and call["verification"].get("presentable") is False
        )]
        rejected_attempts = len({call["group"] for call in design_rejections})
        if design_rejections and rejected_attempts < DESIGN_FAIL_ESCAPE:
            _block(
                "RETRY_REQUIRED. The analysis result was withheld by server verification. "
                f"Correct and rerun the same tool ({rejected_attempts}/{DESIGN_FAIL_ESCAPE} "
                "failed attempts); a value-free terminal failure is not yet authorized."
            )
        _terminal_or_block(
            data,
            "An analysis result was withheld or malformed and was not corrected by a later presentable call to the same tool.",
            "UNVERIFIED_ESCAPE" if design_rejections else "INTERNAL_ERROR",
        )

    # A server-withheld payload contains no raw result and is safe to supersede
    # with a fresh runtime-issued analysis. It must never become reportable.
    calls = [call for call in calls if (
        call.get("result_error") is None
        and isinstance(call.get("verification"), dict)
        and call["verification"].get("presentable") is True
    )]
    if not calls:
        _terminal_or_block(data, "All analysis payloads were withheld by server verification.", "FAILED")

    identity_tools: dict[str, str] = {}
    for call in calls:
        envelope = call.get("verification")
        identity = envelope.get("identity") if isinstance(envelope, dict) else None
        if not isinstance(identity, dict) or not call.get("analysis_id"):
            _terminal_or_block(data, "A gated result lacks a runtime verification envelope.", "INTERNAL_ERROR")
        if not isinstance(identity.get("provenance_hash"), str) or not identity.get("provenance_hash"):
            _terminal_or_block(data, "A server verification identity lacks mandatory provenance binding.", "INTERNAL_ERROR")
        analysis_id = call["analysis_id"]
        raw = call.get("raw_result")
        if not api["public_envelope_matches_call"](
            envelope, call["tool"], call.get("args") or {}, raw,
            call.get("provenance") or {},
        ):
            _terminal_or_block(data, "Verification identity does not match the bound payload.", "INTERNAL_ERROR")
        prior = identity_tools.get(analysis_id)
        if prior is not None:
            _terminal_or_block(data, "A runtime verification identity was reused.", "INTERNAL_ERROR")
        identity_tools[analysis_id] = call["tool"]

    by_analysis: dict[tuple[str, str], list[dict]] = {}
    for call in calls:
        key = (call["tool"], call["args_hash"])
        by_analysis.setdefault(key, []).append(call)

    latest: list[tuple[dict, Any]] = []
    for history in by_analysis.values():
        call = history[-1]
        result = call.get("raw_result")
        envelope = call.get("verification") or {}
        if not isinstance(result, dict):
            verdict = api["GateVerdict"](
                passed=False,
                failures=["tool call has no parseable result"],
            )
        else:
            checks = api["public_check_summary"](dict(envelope.get("checks") or {}))
            failures = (["server verification reported failure"]
                        if envelope.get("failures") else [])
            blocked = api["public_limitation_codes"](
                list(envelope.get("blocked") or []))
            missing = [item for item in api["MANUAL_CHECKS"] if item not in blocked]
            regression_attested = checks.get("regression_suite") is True
            verdict = api["GateVerdict"](
                passed=(envelope.get("presentable") is True and not failures
                        and not missing and regression_attested
                        and all(value is not False for value in checks.values())),
                checks=checks,
                failures=(failures
                          + (["server envelope omitted verifier limitations: "
                              + "; ".join(missing)] if missing else [])
                          + ([] if regression_attested else
                             ["server envelope lacks complete regression attestation"])),
                blocked=blocked,
                notes=api["public_note_codes"](list(envelope.get("notes") or [])),
            )

        if verdict.passed and _is_stochastic(call["tool"], call["args"]):
            server_checks = api["public_check_summary"](
                (call.get("verification") or {}).get("checks") or {})
            server_blocked = (call.get("verification") or {}).get("blocked") or []
            if server_checks.get("reproducibility") is True:
                pass
            elif server_blocked and call["tool"] == "master_simulate":
                verdict = api["combined_gate"](
                    verdict, api["GateVerdict"](passed=True, blocked=list(server_blocked))
                )
            else:
                verdict = api["combined_gate"](
                    verdict, api["GateVerdict"](
                        passed=False,
                        failures=["trusted server envelope lacks same-seed replay evidence"],
                    )
                )
            latest.append((call, verdict))
            continue
        latest.append((call, verdict))

    latest.sort(key=lambda item: item[0]["group"])

    failed = [(call, verdict) for call, verdict in latest if not verdict.passed]
    partial = [(call, verdict) for call, verdict in latest if verdict.passed and verdict.blocked]

    # Regression health must be true and structurally complete at/after the
    # latest result. Historical failures never count toward an escape.
    last_group = max(call["group"] for call, _ in latest)
    relevant_tests = [t for t in runtests if t["group"] >= last_group]
    test_verdicts = [
        api["check_regression_tests"](test.get("result") or {})
        for test in relevant_tests
    ]
    # Health is a chronological state, not an historical achievement.  A later
    # failing suite must supersede an earlier pass after the same result.
    framework_ok = bool(test_verdicts) and test_verdicts[-1].passed

    group_pass: dict[int, bool] = {}
    # Count actual historical design attempts for loop safety, while still
    # treating a parallel fan-out in one message group as one attempt.
    for call in calls:
        envelope = call.get("verification")
        passed = bool(isinstance(envelope, dict) and envelope.get("presentable") is True)
        group_pass[call["group"]] = group_pass.get(call["group"], True) and passed
    consecutive_failures = 0
    for group in sorted(group_pass, reverse=True):
        if group_pass[group]:
            break
        consecutive_failures += 1

    if failed:
        details = "; ".join(
            f"{call['tool']}: {', '.join(verdict.failures)}"
            for call, verdict in failed
        )
        if consecutive_failures >= DESIGN_FAIL_ESCAPE:
            _terminal_or_block(data, details, "UNVERIFIED_ESCAPE")
        _block(
            "RETRY_REQUIRED. Exact result verification failed: " + details
            + ". Correct and rerun the same tool; the runtime will issue and bind the new verification identity."
        )

    if not framework_ok:
        if len(relevant_tests) >= RUNTESTS_ESCAPE:
            _terminal_or_block(
                data,
                "The regression framework did not produce a complete passing result.",
                "UNVERIFIED_ESCAPE",
            )
        _block(
            "RETRY_REQUIRED. Run the complete regression suite after the latest "
            "analysis and require a structurally valid all_ok=true result."
        )

    message = _last_message_text(data.get("last_assistant_message"))
    try:
        expected_final = _expected_final(api, latest)
    except SystemExit:
        raise
    except Exception:
        _terminal_or_block(
            data,
            "The verified report envelope was malformed.",
            "INTERNAL_ERROR",
        )
    if api["normalize_report_text"](message) != api["normalize_report_text"](expected_final):
        status = "PASS_PARTIAL" if partial else "VERIFIED"
        _terminal_or_block(
            data,
            "The result is not canonically bound to the final answer. Copy every "
            "presentable tool result's _verification.report verbatim, in call order, "
            "separated by a Markdown horizontal rule, and add no other prose.",
            status,
        )

    clear_ledger(data)
    _allow(f"{'PASS_PARTIAL' if partial else 'VERIFIED'}: {len(latest)} identity-safe result(s)")


def main() -> None:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook input must be an object")
    except Exception:
        _block("INTERNAL_ERROR. Hook input was not valid JSON; verification cannot be bypassed.")
    enforce(data)
