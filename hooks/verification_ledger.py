"""Synchronous, append-safe PostToolBatch ledger for verification."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


SERVER = "mcp__experiment-design__"
SCOPES = {"experiment-designer", "design-verifier"}
LEDGER_VERSION = 2


def _key(data: dict[str, Any]) -> str:
    scope = data.get("_expdesign_agent_scope")
    agent_id = data.get("agent_id")
    if not isinstance(scope, str) or scope not in SCOPES:
        raise ValueError("a governed agent scope is required for the verification ledger")
    principal = (
        f"subagent:{scope}:{agent_id}"
        if isinstance(agent_id, str) and agent_id else f"main:{scope}"
    )
    fields = [data.get("session_id"), data.get("prompt_id"), principal]
    if not all(isinstance(value, str) and value for value in fields):
        raise ValueError("session_id and prompt_id are required for the verification ledger")
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def ledger_path(data: dict[str, Any]) -> Path:
    root = Path(os.environ.get(
        "EXPDESIGN_HOOK_LEDGER_DIR",
        str(Path(tempfile.gettempdir()) / "experiment-design-hook-ledgers"),
    ))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root / f"{_key(data)}.json"


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Serialize writers/readers without ever locking the replaced data inode."""
    lock = path.with_name(f"{path.name}.lock")
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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


def _new_ledger() -> dict[str, Any]:
    return {"version": LEDGER_VERSION, "batch_count": 0, "batches": [], "lines": []}


def _validate_ledger(ledger: Any, *, require_complete: bool = True) -> dict[str, Any]:
    if not isinstance(ledger, dict) or ledger.get("version") != LEDGER_VERSION:
        raise ValueError("verification ledger has an unsupported version")
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


def _atomic_write(path: Path, ledger: dict[str, Any]) -> None:
    temp = path.with_name(f"{path.name}.{os.getpid()}-{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def record_batch(data: dict[str, Any]) -> None:
    if data.get("hook_event_name") != "PostToolBatch":
        raise ValueError("record_batch accepts only PostToolBatch input")
    calls = data.get("tool_calls")
    if not isinstance(calls, list):
        raise ValueError("PostToolBatch input is missing tool_calls")
    relevant = [
        call for call in calls
        if isinstance(call, dict)
        and isinstance(call.get("tool_name"), str)
        and call["tool_name"].startswith(SERVER)
    ]
    if not relevant:
        return

    path = ledger_path(data)
    failures: list[str] = []
    with _locked(path):
        try:
            ledger = _validate_ledger(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            ledger = _new_ledger()

        batch_number = int(ledger["batch_count"]) + 1
        lines = ledger["lines"]
        line_start = len(lines)
        for index, call in enumerate(relevant):
            name = str(call["tool_name"])
            call_id = str(call.get("tool_use_id") or f"batch-{batch_number}-call-{index}")
            args = call.get("tool_input") if isinstance(call.get("tool_input"), dict) else {}
            lines.append(json.dumps({"message": {
                "id": f"ledger-batch-{batch_number}", "role": "assistant", "content": [{
                    "type": "tool_use", "id": call_id, "name": name, "input": args,
                }],
            }}, separators=(",", ":"), allow_nan=False))
            try:
                response = _response_text(call.get("tool_response"))
            except (TypeError, ValueError) as exc:
                response = None
                failures.append(f"{call_id}: response serialization failed: {exc}")
            if response is None:
                failures.append(f"{call_id}: MCP tool response has no text payload")
                # Deliberately non-JSON: the policy associates this failure with
                # this call, while the incomplete-batch marker also fails closed.
                response = "EXPDESIGN_LEDGER_ERROR: response unavailable"
            lines.append(json.dumps({"message": {
                "role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": call_id,
                    "content": [{"type": "text", "text": response}],
                }],
            }}, separators=(",", ":"), allow_nan=False))

        ledger["batch_count"] = batch_number
        ledger["batches"].append({
            "number": batch_number,
            "line_start": line_start,
            "line_end": len(lines),
            "call_count": len(relevant),
            "complete": not failures,
        })
        _validate_ledger(ledger, require_complete=False)
        _atomic_write(path, ledger)

    if failures:
        raise ValueError("; ".join(failures))


def read_lines(data: dict[str, Any]) -> list[str]:
    path = ledger_path(data)
    with _locked(path):
        ledger = _validate_ledger(json.loads(path.read_text(encoding="utf-8")))
        return list(ledger["lines"])


def clear(data: dict[str, Any]) -> None:
    try:
        path = ledger_path(data)
        with _locked(path):
            path.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
