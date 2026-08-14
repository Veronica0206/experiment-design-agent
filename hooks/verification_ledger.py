"""Synchronous, append-safe PostToolBatch ledger for verification."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance.registry import RegistryError, get_agent  # noqa: E402
from private_state import (  # noqa: E402
    atomic_write_bytes_at,
    locked_private_directory,
    read_bytes_at,
    unlink_owned_regular_at,
)


SERVER = "mcp__experiment-design__"
AGENT_TOOL = "Agent"
LEDGER_VERSION = 4
LEDGER_KEY_DOMAIN = "experiment-design-verification-ledger-key-v1"
CONTEXT_COMMITMENT_DOMAIN = "experiment-design-verification-ledger-context-v1"
OPTIONAL_CONTEXT_FIELDS = ("orchestration_id", "task_id", "parent_task_id", "attempt")
MAIN_ONLY_EVENTS = frozenset({"UserPromptSubmit", "PreToolUse", "Stop"})
SUBAGENT_ONLY_EVENTS = frozenset({"SubagentStop"})
DUAL_PRINCIPAL_EVENTS = frozenset({"PostToolBatch"})


def governed_agent(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, str) or not scope:
        raise ValueError("a governed agent scope is required for the verification ledger")
    try:
        return get_agent(scope)
    except RegistryError as exc:
        raise ValueError(str(exc)) from exc


def context_principal(data: dict[str, Any], scope: str) -> str:
    """Return one fail-closed main/subagent identity for a governed hook event."""
    agent = governed_agent(scope)
    event = data.get("hook_event_name")
    if event not in MAIN_ONLY_EVENTS | SUBAGENT_ONLY_EVENTS | DUAL_PRINCIPAL_EVENTS:
        raise ValueError("verification ledger hook event has no principal semantics")

    runtime_type = data.get("agent_type")
    if runtime_type is not None and (
        not isinstance(runtime_type, str)
        or not runtime_type
        or "\0" in runtime_type
        or runtime_type != scope
    ):
        raise ValueError("verification hook agent scope mismatch")

    agent_id = data.get("agent_id")
    if agent_id is not None:
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("agent_id must be a non-empty string when provided")
        if "\0" in agent_id:
            raise ValueError("verification ledger key components must not contain NUL")

    if agent["role"] == "coordinator":
        if event in SUBAGENT_ONLY_EVENTS or agent_id is not None:
            raise ValueError("the governed coordinator must run as the main agent")
        return f"main:{scope}"

    if event in SUBAGENT_ONLY_EVENTS:
        if agent_id is None:
            raise ValueError("SubagentStop requires a non-empty agent_id")
        return f"subagent:{scope}:{agent_id}"
    if event in MAIN_ONLY_EVENTS:
        if agent_id is not None:
            raise ValueError("main-agent hook events must not carry agent_id")
        return f"main:{scope}"
    return (
        f"subagent:{scope}:{agent_id}"
        if agent_id is not None else f"main:{scope}"
    )


def _key_components(data: dict[str, Any]) -> tuple[str, str, str]:
    scope = data.get("_expdesign_agent_scope")
    values = (
        data.get("session_id"), data.get("prompt_id"),
        context_principal(data, scope),
    )
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("session_id and prompt_id are required for the verification ledger")
    components = tuple(str(value) for value in values)
    if any("\0" in value for value in components):
        raise ValueError("verification ledger key components must not contain NUL")
    return components


def _length_framed_digest(domain: str, components: tuple[str, ...]) -> str:
    """Hash an unambiguous UTF-8 framing of a domain and ordered components."""
    values = (domain, *components)
    framed = bytearray(len(values).to_bytes(4, "big"))
    for value in values:
        encoded = value.encode("utf-8")
        framed.extend(len(encoded).to_bytes(8, "big"))
        framed.extend(encoded)
    return hashlib.sha256(framed).hexdigest()


def _context_commitment(data: dict[str, Any]) -> str:
    return _length_framed_digest(CONTEXT_COMMITMENT_DOMAIN, _key_components(data))


def _context_metadata(data: dict[str, Any], scope: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "scope": scope,
        "principal": context_principal(data, scope),
        "context_commitment": _context_commitment(data),
    }
    for field in OPTIONAL_CONTEXT_FIELDS:
        value = data.get(field)
        if value is None:
            continue
        if field == "attempt":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("attempt must be a positive integer when provided")
        elif not isinstance(value, str) or not value:
            raise ValueError(f"{field} must be a non-empty string when provided")
        metadata[field] = value
    return metadata


def _key(data: dict[str, Any]) -> str:
    return _length_framed_digest(LEDGER_KEY_DOMAIN, _key_components(data))


def ledger_path(data: dict[str, Any]) -> Path:
    root = Path(os.environ.get(
        "EXPDESIGN_HOOK_LEDGER_DIR",
        str(Path(tempfile.gettempdir()) / "experiment-design-hook-ledgers"),
    ))
    if not root.is_absolute():
        raise ValueError("verification ledger root must be absolute")
    return root / f"{_key(data)}.json"


def _read_ledger_file(directory_fd: int, name: str) -> dict[str, Any]:
    try:
        return json.loads(
            read_bytes_at(
                directory_fd, name, "verification ledger",
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("verification ledger is malformed") from exc


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    """Serialize ledgers on one stable inode, avoiding per-prompt lock leaks."""
    with locked_private_directory(
        path.parent,
        ".verification.lock",
        "verification ledger root",
        "verification ledger lock",
    ) as directory_fd:
        yield directory_fd


def _response_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts = [
            str(block.get("text", ""))
            for block in value
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(texts) if texts else None
    if isinstance(value, dict):
        if isinstance(value.get("content"), list):
            return _response_text(value["content"])
        return json.dumps(value, separators=(",", ":"), allow_nan=False)
    return None


def _new_ledger(data: dict[str, Any], policy: str) -> dict[str, Any]:
    scope = str(data["_expdesign_agent_scope"])
    return {
        "version": LEDGER_VERSION,
        "policy": policy,
        "metadata": _context_metadata(data, scope),
        "batch_count": 0,
        "batches": [],
        "lines": [],
    }


def _validate_ledger(ledger: Any, *, require_complete: bool = True) -> dict[str, Any]:
    if not isinstance(ledger, dict) or ledger.get("version") != LEDGER_VERSION:
        raise ValueError("verification ledger has an unsupported version")
    if ledger.get("policy") not in {"mcp", "coordinator"}:
        raise ValueError("verification ledger has an invalid policy")
    metadata = ledger.get("metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("scope"), str):
        raise ValueError("verification ledger has invalid context metadata")
    if not isinstance(metadata.get("principal"), str):
        raise ValueError("verification ledger has invalid principal metadata")
    if set(metadata) - {
        "scope", "principal", "context_commitment", *OPTIONAL_CONTEXT_FIELDS,
    }:
        raise ValueError("verification ledger has unsupported context metadata")
    commitment = metadata.get("context_commitment")
    if not (
        isinstance(commitment, str)
        and len(commitment) == 64
        and all(character in "0123456789abcdef" for character in commitment)
    ):
        raise ValueError("verification ledger has invalid context commitment")
    for field in OPTIONAL_CONTEXT_FIELDS:
        value = metadata.get(field)
        if value is None:
            continue
        if field == "attempt":
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("verification ledger has invalid attempt metadata")
        elif not isinstance(value, str) or not value:
            raise ValueError("verification ledger has invalid lineage metadata")
    lines = ledger.get("lines")
    batches = ledger.get("batches")
    count = ledger.get("batch_count")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ValueError("verification ledger contains invalid entries")
    if not isinstance(batches, list) or not isinstance(count, int) or count != len(batches):
        raise ValueError("verification ledger batch metadata is inconsistent")
    cursor = 0
    for expected, batch in enumerate(batches, start=1):
        if not isinstance(batch, dict):
            raise ValueError("verification ledger batch metadata is malformed")
        number = batch.get("number")
        start = batch.get("line_start")
        end = batch.get("line_end")
        call_count = batch.get("call_count")
        complete = batch.get("complete")
        if (
            number != expected or not isinstance(start, int) or start != cursor
            or not isinstance(end, int) or end < start or end > len(lines)
            or not isinstance(call_count, int) or call_count < 1
            or end - start != 2 * call_count
            or not isinstance(complete, bool)
        ):
            raise ValueError("verification ledger contains a batch gap")
        if require_complete and complete is not True:
            raise ValueError("verification ledger contains an incomplete batch")
        cursor = end
    if cursor != len(lines):
        raise ValueError("verification ledger has unaccounted entries")
    return ledger


def _merge_context(ledger: dict[str, Any], data: dict[str, Any], policy: str) -> None:
    scope = str(data["_expdesign_agent_scope"])
    incoming = _context_metadata(data, scope)
    if ledger.get("policy") != policy:
        raise ValueError("verification ledger policy changed within one principal")
    metadata = ledger["metadata"]
    if metadata.get("scope") != scope or metadata.get("principal") != incoming["principal"]:
        raise ValueError("verification ledger principal metadata mismatch")
    if metadata.get("context_commitment") != incoming["context_commitment"]:
        raise ValueError("verification ledger context commitment mismatch")
    for field in OPTIONAL_CONTEXT_FIELDS:
        if field in metadata and field in incoming and metadata[field] != incoming[field]:
            raise ValueError(f"verification ledger {field} changed within one principal")
        if field in incoming and field not in metadata:
            metadata[field] = incoming[field]


def _tool_allowed(agent: dict[str, Any], name: str) -> bool:
    tools = agent["tools"]
    return f"{SERVER}*" in tools or name in tools


def _strict_child_response(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value.strip() else None
    if isinstance(value, list):
        if not value or not all(
            isinstance(block, dict)
            and set(block).issubset({"type", "text"})
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            for block in value
        ):
            return None
        text = "\n".join(str(block["text"]) for block in value)
        return text if text.strip() else None
    if isinstance(value, dict):
        status = value.get("status")
        if ("is_error" in value and value.get("is_error") is not False) or (
            status is not None
            and (not isinstance(status, str) or status.lower() not in {"completed", "success"})
        ):
            return None
        # Runtime metadata (agent ID, duration, usage) is deliberately ignored;
        # only the completed child's final content crosses the ledger boundary.
        if "content" in value:
            return _strict_child_response(value["content"])
    return None


def _append_pair(
    lines: list[str], *, batch_number: int, call_id: str, name: str,
    args: dict[str, Any], response: str,
) -> None:
    lines.append(json.dumps({"message": {
        "id": f"ledger-batch-{batch_number}", "role": "assistant", "content": [{
            "type": "tool_use", "id": call_id, "name": name, "input": args,
        }],
    }}, separators=(",", ":"), allow_nan=False))
    lines.append(json.dumps({"message": {
        "role": "user", "content": [{
            "type": "tool_result", "tool_use_id": call_id,
            "content": [{"type": "text", "text": response}],
        }],
    }}, separators=(",", ":"), allow_nan=False))


def _atomic_write(directory_fd: int, name: str, ledger: dict[str, Any]) -> None:
    payload = json.dumps(
        ledger,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    atomic_write_bytes_at(
        directory_fd,
        name,
        payload,
        "verification ledger",
    )


def record_batch(data: dict[str, Any]) -> None:
    if data.get("hook_event_name") != "PostToolBatch":
        raise ValueError("record_batch accepts only PostToolBatch input")
    scope = data.get("_expdesign_agent_scope")
    agent = governed_agent(scope)
    policy = str(agent["ledger_policy"])
    calls = data.get("tool_calls")
    if not isinstance(calls, list):
        raise ValueError("PostToolBatch input is missing tool_calls")
    # Validate the principal even for an empty batch.  The launcher normally
    # enforces this boundary first, but direct callers must not observe weaker
    # identity semantics simply because there is nothing to persist.
    context_principal(data, str(scope))
    if not calls:
        return

    path = ledger_path(data)
    failures: list[str] = []
    with _locked(path) as directory_fd:
        try:
            ledger = _validate_ledger(
                _read_ledger_file(directory_fd, path.name)
            )
        except FileNotFoundError:
            ledger = _new_ledger(data, policy)
        _merge_context(ledger, data, policy)

        batch_number = int(ledger["batch_count"]) + 1
        lines = ledger["lines"]
        line_start = len(lines)
        seen_ids: set[str] = set()
        for index, call in enumerate(calls):
            call = call if isinstance(call, dict) else {}
            raw_id = call.get("tool_use_id")
            call_id = raw_id if isinstance(raw_id, str) and raw_id else f"batch-{batch_number}-call-{index}"
            if call_id in seen_ids:
                failures.append(f"{call_id}: duplicate tool_use_id")
            seen_ids.add(call_id)
            name = call.get("tool_name") if isinstance(call.get("tool_name"), str) else ""
            raw_args = call.get("tool_input")
            args = raw_args if isinstance(raw_args, dict) else {}

            if policy == "coordinator":
                child = args.get("subagent_type")
                sanitized_args = {
                    "subagent_type": child if isinstance(child, str) else "__invalid__",
                }
                if name != AGENT_TOOL:
                    failures.append(f"{call_id}: coordinator may call only Agent")
                expected_fields = {
                    "prompt", "description", "subagent_type", "run_in_background",
                }
                if not isinstance(raw_args, dict) or set(raw_args) != expected_fields:
                    failures.append(f"{call_id}: unsupported or incomplete Agent input shape")
                if child not in agent["allowed_children"]:
                    failures.append(f"{call_id}: unapproved or missing child agent")
                if args.get("description") != f"Dispatch current request to {child}":
                    failures.append(f"{call_id}: Agent description violates the deterministic contract")
                if not isinstance(args.get("prompt"), str):
                    failures.append(f"{call_id}: Agent prompt must be a string")
                if args.get("run_in_background") is not False:
                    failures.append(f"{call_id}: asynchronous child launch is forbidden")
                response = _strict_child_response(call.get("tool_response"))
                if response is None:
                    failures.append(f"{call_id}: child response lacks completed final content")
                    response = "EXPDESIGN_LEDGER_ERROR: child response unavailable"
                _append_pair(
                    lines, batch_number=batch_number, call_id=call_id,
                    name=AGENT_TOOL, args=sanitized_args, response=response,
                )
            else:
                if not name.startswith(SERVER) or not _tool_allowed(agent, name):
                    failures.append(f"{call_id}: tool is outside the registered agent allowlist")
                try:
                    response = _response_text(call.get("tool_response"))
                except (TypeError, ValueError) as exc:
                    response = None
                    failures.append(f"{call_id}: response serialization failed: {exc}")
                if response is None:
                    failures.append(f"{call_id}: MCP tool response has no text payload")
                    response = "EXPDESIGN_LEDGER_ERROR: response unavailable"
                _append_pair(
                    lines, batch_number=batch_number, call_id=call_id,
                    name=name, args=args, response=response,
                )

        ledger["batch_count"] = batch_number
        ledger["batches"].append({
            "number": batch_number,
            "line_start": line_start,
            "line_end": len(lines),
            "call_count": len(calls),
            "complete": not failures,
        })
        _validate_ledger(ledger, require_complete=False)
        _atomic_write(directory_fd, path.name, ledger)

    if failures:
        raise ValueError("; ".join(failures))


def read_lines(data: dict[str, Any]) -> list[str]:
    path = ledger_path(data)
    with _locked(path) as directory_fd:
        ledger = _validate_ledger(
            _read_ledger_file(directory_fd, path.name)
        )
        incoming = _context_metadata(data, str(data["_expdesign_agent_scope"]))
        metadata = ledger["metadata"]
        if metadata.get("scope") != incoming["scope"] or metadata.get("principal") != incoming["principal"]:
            raise ValueError("verification ledger principal metadata mismatch")
        if metadata.get("context_commitment") != incoming["context_commitment"]:
            raise ValueError("verification ledger context commitment mismatch")
        for field in OPTIONAL_CONTEXT_FIELDS:
            if field in incoming and metadata.get(field) != incoming[field]:
                raise ValueError(f"verification ledger {field} does not match the stop context")
        return list(ledger["lines"])


def clear(data: dict[str, Any]) -> None:
    try:
        path = ledger_path(data)
        with _locked(path) as directory_fd:
            unlink_owned_regular_at(
                directory_fd, path.name, "verification ledger",
            )
    except (OSError, ValueError):
        pass
    if data.get("_expdesign_agent_scope") == "experiment-design-coordinator":
        try:
            from prompt_binding import clear_prompt_binding
            clear_prompt_binding(data)
        except (ImportError, OSError, ValueError):
            pass
