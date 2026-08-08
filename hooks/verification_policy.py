"""Fail-closed Stop/SubagentStop verification policy.

This module is intentionally separate from the hook entrypoint so policy logic
can be unit-tested without coupling it to Claude Code process wiring.
"""

from __future__ import annotations

import json
import hashlib
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

from verification_ledger import clear as clear_ledger
from verification_ledger import read_lines as read_ledger_lines


SERVER = "mcp__experiment-design__"
VERIFY_TOOL = "run_tests"
UNGATED = {"validate_config", VERIFY_TOOL}
ENFORCED_AGENTS = {"experiment-designer", "design-verifier"}
RUNTESTS_ESCAPE = 2
DESIGN_FAIL_ESCAPE = 3
SAFE_FAILURE_MESSAGE = "Verification failed; results withheld as not trustworthy."


def _trace(outcome: str, detail: str = "") -> None:
    path = Path(os.environ.get(
        "EXPDESIGN_HOOK_LOG",
        str(Path(tempfile.gettempdir()) / "experiment-design-hook.log"),
    ))
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            digest = hashlib.sha256(detail.encode("utf-8", errors="replace")).hexdigest()[:16]
            handle.write(f"{outcome}\treason_sha256={digest}\n")
        os.chmod(path, 0o600)
    except Exception:
        # Governance must not depend on observability; the decision still runs.
        pass


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
        design_checks_for,
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
        "design_checks_for": design_checks_for,
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


def enforce(
    data: dict[str, Any],
    line_source: Callable[[dict[str, Any]], list[str]] = read_ledger_lines,
) -> None:
    """Enforce policy against a trusted synchronous-ledger source.

    Tests may inject an in-memory ledger reader. Production callers never read
    transcript paths or environment-selected alternate trust sources.
    """
    agent_type = data.get("agent_type") or None
    scope = data.get("_expdesign_agent_scope")
    if scope not in ENFORCED_AGENTS:
        _block("INTERNAL_ERROR. A governed agent scope is required for verification.")
    if agent_type is not None and agent_type != scope:
        _block("INTERNAL_ERROR. Verification hook agent scope mismatch.")

    try:
        lines = line_source(data)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        message = _last_message_text(data.get("last_assistant_message"))
        try:
            api = _load_gates()
        except Exception as load_exc:
            _terminal_or_block(data, f"Verification gates could not load: {load_exc}.", "INTERNAL_ERROR")
        if api["is_elicitation_message"](message):
            _allow("no ledger; explicit open-ended elicitation")
        if _safe_failure_report(message):
            clear_ledger(data)
            _allow("no ledger; value-free failure report")
        _terminal_or_block(data, f"The synchronous verification ledger could not be read: {exc}.", "INTERNAL_ERROR")

    try:
        api = _load_gates()
    except Exception as exc:
        _terminal_or_block(data, f"Verification gates could not load: {exc}.", "INTERNAL_ERROR")

    calls, runtests, ungated, parse_errors = _parse_transcript(lines, api)
    if parse_errors:
        _terminal_or_block(data, "; ".join(parse_errors[:5]), "INTERNAL_ERROR")
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
                "go_target", "alphas", "powers", "configuration_report",
            }
            if (latest_config.get("result_error") is None
                    and isinstance(result, dict) and set(result) == fields
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
        _terminal_or_block(
            data,
            "An analysis result was withheld or malformed and was not corrected by a later presentable call to the same tool.",
            "FAILED",
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
