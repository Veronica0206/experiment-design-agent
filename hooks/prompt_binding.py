"""Private host-side commitment and dispatch binding for the coordinator.

The raw user prompt is used only in memory.  Persistent state contains a
keyed commitment plus non-secret runtime identifiers, never the prompt itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from governance.registry import RegistryError, domain_phase, get_agent  # noqa: E402
from private_state import (  # noqa: E402
    atomic_write_bytes_at,
    create_bytes_at,
    ensure_private_directory,
    locked_private_directory,
    read_bytes_at,
    unlink_owned_regular_at,
)
from verification_ledger import context_principal  # noqa: E402


BINDING_VERSION = 2
AGENT_TOOL = "Agent"
KEY_BYTES = 32
ALLOWED_AGENT_INPUT_FIELDS = frozenset({
    "prompt", "description", "subagent_type", "run_in_background",
})


def _coordinator(scope: Any) -> dict[str, Any]:
    if not isinstance(scope, str) or not scope:
        raise ValueError("a governed coordinator scope is required")
    try:
        agent = get_agent(scope)
    except RegistryError as exc:
        raise ValueError(str(exc)) from exc
    if agent.get("role") != "coordinator" or agent.get("ledger_policy") != "coordinator":
        raise ValueError("prompt binding is available only to the governed coordinator")
    return agent


def _context(data: dict[str, Any]) -> dict[str, str]:
    scope = data.get("_expdesign_agent_scope")
    _coordinator(scope)
    principal = context_principal(data, scope)
    session_id = data.get("session_id")
    prompt_id = data.get("prompt_id")
    if not all(isinstance(value, str) and value and "\0" not in value
               for value in (session_id, prompt_id)):
        raise ValueError("session_id and prompt_id are required for prompt binding")
    return {
        "session_id": session_id,
        "prompt_id": prompt_id,
        "principal": principal,
        "scope": scope,
    }


def _root() -> Path:
    base = Path(os.environ.get(
        "EXPDESIGN_HOOK_LEDGER_DIR",
        str(Path(tempfile.gettempdir()) / "experiment-design-hook-ledgers"),
    ))
    if not base.is_absolute():
        raise ValueError("prompt-binding ledger root must be absolute")
    # The prompt-binding child and verification ledgers share this base.  Pin
    # its privacy contract explicitly so first use cannot create it as a 0755
    # incidental parent via mkdir(parents=True).
    ensure_private_directory(base, "verification ledger root")
    return base / "prompt-bindings"


def _binding_key(context: dict[str, str]) -> str:
    values = [context["session_id"], context["prompt_id"], context["principal"]]
    return hashlib.sha256("\0".join(values).encode("utf-8")).hexdigest()


def binding_path(data: dict[str, Any]) -> Path:
    return _root() / f"{_binding_key(_context(data))}.json"


def _secret(directory_fd: int) -> bytes:
    try:
        value = read_bytes_at(
            directory_fd, ".commitment.key", "prompt commitment key",
        )
    except FileNotFoundError:
        value = secrets.token_bytes(KEY_BYTES)
        try:
            create_bytes_at(
                directory_fd,
                ".commitment.key",
                value,
                "prompt commitment key",
            )
        except FileExistsError:
            value = read_bytes_at(
                directory_fd, ".commitment.key", "prompt commitment key",
            )
    if len(value) != KEY_BYTES:
        raise ValueError("prompt commitment key has an invalid length")
    return value


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    # One stable lock avoids an unbounded lock-file inode per prompt while
    # still serializing capture, authorization, and retirement atomically.
    with locked_private_directory(
        path.parent,
        ".bindings.lock",
        "prompt-binding ledger directory",
        "prompt commitment lock",
    ) as directory_fd:
        yield directory_fd


def _commitment(secret: bytes, context: dict[str, str], prompt: str) -> str:
    message = json.dumps({
        "domain": "experiment-design-agent-dispatch-v1",
        "principal": context["principal"],
        "prompt": prompt,
        "prompt_id": context["prompt_id"],
        "session_id": context["session_id"],
    }, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _child_phase(child: str) -> str:
    """Classify a child from its governed registry domain, never its name."""
    try:
        child_agent = get_agent(child)
        if child_agent.get("role") != "domain_executor":
            raise RegistryError("coordinator children must be domain executors")
        return domain_phase(str(child_agent.get("domain")))
    except RegistryError as exc:
        raise ValueError(str(exc)) from exc


def _binding(context: dict[str, str], commitment: str) -> dict[str, Any]:
    return {
        "version": BINDING_VERSION,
        "metadata": context,
        "commitment": commitment,
        "dispatch_phase": None,
        "dispatched_children": [],
    }


def _validate_binding(value: Any, context: dict[str, str]) -> dict[str, Any]:
    expected_fields = {
        "version", "metadata", "commitment", "dispatch_phase",
        "dispatched_children",
    }
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise ValueError("prompt binding has an invalid schema")
    if value.get("version") != BINDING_VERSION or value.get("metadata") != context:
        raise ValueError("prompt binding context mismatch")
    commitment = value.get("commitment")
    if not isinstance(commitment, str) or len(commitment) != 64:
        raise ValueError("prompt binding commitment is invalid")
    try:
        bytes.fromhex(commitment)
    except ValueError as exc:
        raise ValueError("prompt binding commitment is invalid") from exc
    children = value.get("dispatched_children")
    phase = value.get("dispatch_phase")
    coordinator = _coordinator(context["scope"])
    if (not isinstance(children, list)
            or not all(isinstance(child, str) for child in children)
            or len(children) != len(set(children))
            or any(child not in coordinator["allowed_children"] for child in children)):
        raise ValueError("prompt binding has invalid dispatch state")
    if not children:
        if phase is not None:
            raise ValueError("prompt binding has an orphaned dispatch phase")
    elif phase not in {"planning", "evidence"} or any(
        _child_phase(child) != phase for child in children
    ):
        raise ValueError("prompt binding has inconsistent dispatch phases")
    return value


def _read_binding(
    directory_fd: int,
    name: str,
    context: dict[str, str],
) -> dict[str, Any]:
    try:
        value = json.loads(
            read_bytes_at(
                directory_fd, name, "prompt commitment ledger",
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("prompt commitment ledger is malformed") from exc
    return _validate_binding(value, context)


def _atomic_write(directory_fd: int, name: str, value: dict[str, Any]) -> None:
    payload = json.dumps(
        value,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    atomic_write_bytes_at(
        directory_fd,
        name,
        payload,
        "prompt commitment ledger",
    )


def capture_prompt(data: dict[str, Any]) -> None:
    """Capture one raw user prompt as a private keyed commitment."""
    if data.get("hook_event_name") != "UserPromptSubmit":
        raise ValueError("capture accepts only UserPromptSubmit input")
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("UserPromptSubmit input is missing the raw prompt")
    context = _context(data)
    path = _root() / f"{_binding_key(context)}.json"
    with _locked(path) as directory_fd:
        expected = _binding(
            context,
            _commitment(_secret(directory_fd), context, prompt),
        )
        try:
            current = _read_binding(directory_fd, path.name, context)
        except FileNotFoundError:
            _atomic_write(directory_fd, path.name, expected)
            return
        if not hmac.compare_digest(current["commitment"], expected["commitment"]):
            raise ValueError("a different prompt is already bound to this prompt identity")


def authorize_agent_dispatch(data: dict[str, Any]) -> None:
    """Allow only an exact, synchronous dispatch of the captured user prompt."""
    if data.get("hook_event_name") != "PreToolUse" or data.get("tool_name") != AGENT_TOOL:
        raise ValueError("dispatch binding accepts only PreToolUse Agent input")
    context = _context(data)
    agent = _coordinator(context["scope"])
    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict) or set(tool_input) != ALLOWED_AGENT_INPUT_FIELDS:
        raise ValueError("Agent input has an unsupported or incomplete shape")
    prompt = tool_input.get("prompt")
    child = tool_input.get("subagent_type")
    description = tool_input.get("description")
    if not isinstance(prompt, str):
        raise ValueError("Agent input prompt must be a string")
    if child not in agent["allowed_children"]:
        raise ValueError("Agent input requests an unapproved child")
    if description != f"Dispatch current request to {child}":
        raise ValueError("Agent description must use the deterministic host contract")
    if tool_input.get("run_in_background") is not False:
        raise ValueError("Agent dispatch must explicitly run in the foreground")
    path = _root() / f"{_binding_key(context)}.json"
    with _locked(path) as directory_fd:
        value = _read_binding(directory_fd, path.name, context)
        expected = _commitment(_secret(directory_fd), context, prompt)
        if not hmac.compare_digest(value["commitment"], expected):
            raise ValueError("Agent prompt does not match the current raw user prompt")
        dispatched = value["dispatched_children"]
        if child in dispatched:
            raise ValueError("the same child may be dispatched only once per user prompt")
        phase = _child_phase(str(child))
        current_phase = value["dispatch_phase"]
        if current_phase is not None and current_phase != phase:
            raise ValueError(
                "evidence analysis and prospective planning require separate user prompts"
            )
        value["dispatch_phase"] = phase
        value["dispatched_children"] = [*dispatched, child]
        _atomic_write(directory_fd, path.name, value)


def clear_prompt_binding(data: dict[str, Any]) -> None:
    """Remove the per-prompt commitment after a terminal coordinator response."""
    try:
        context = _context(data)
        path = _root() / f"{_binding_key(context)}.json"
        with _locked(path) as directory_fd:
            unlink_owned_regular_at(
                directory_fd, path.name, "prompt commitment ledger",
            )
    except (OSError, ValueError):
        pass
