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


BINDING_VERSION = 3
EPISODE_VERSION = 1
MAX_CLARIFICATION_TURNS = 16
MAX_REQUEST_BYTES = 131072
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


def _episode_name(context: dict[str, str]) -> str:
    identity = json.dumps(_episode_context(context), sort_keys=True, separators=(",", ":"))
    return "episode-" + hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json"


def _episode_context(context: dict[str, str]) -> dict[str, str]:
    return {key: context[key] for key in ("session_id", "principal", "scope")}


def _validate_turns(turns: Any, context: dict[str, str]) -> list[dict[str, Any]]:
    if not isinstance(turns, list) or len(turns) >= MAX_CLARIFICATION_TURNS:
        raise ValueError("clarification history exceeds the supported turn limit")
    ids: set[str] = set()
    total = 0
    for turn in turns:
        if not isinstance(turn, dict) or set(turn) != {"context", "commitment", "bytes"}:
            raise ValueError("clarification turn has an invalid schema")
        previous = turn["context"]
        if (not isinstance(previous, dict) or set(previous) != set(context)
                or _episode_context(previous) != _episode_context(context)
                or not isinstance(previous.get("prompt_id"), str)
                or not previous["prompt_id"] or "\0" in previous["prompt_id"]
                or previous["prompt_id"] in ids):
            raise ValueError("clarification history has invalid turn identities")
        ids.add(previous["prompt_id"])
        digest = turn["commitment"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise ValueError("clarification commitment is invalid")
        size = turn["bytes"]
        if type(size) is not int or size < 0:
            raise ValueError("clarification byte count is invalid")
        total += size
    if total > MAX_REQUEST_BYTES:
        raise ValueError("clarification history exceeds the supported byte limit")
    return turns


def _read_episode(directory_fd: int, context: dict[str, str]) -> dict[str, Any]:
    empty = {"version": EPISODE_VERSION, "metadata": _episode_context(context),
             "turns": [], "phase": None}
    try:
        value = json.loads(read_bytes_at(
            directory_fd, _episode_name(context), "clarification episode",
        ).decode("utf-8"))
    except FileNotFoundError:
        return empty
    if (not isinstance(value, dict) or set(value) != set(empty)
            or value["version"] != EPISODE_VERSION
            or value["metadata"] != empty["metadata"]
            or value["phase"] not in {None, "planning", "evidence"}):
        raise ValueError("clarification episode has an invalid schema or context")
    _validate_turns(value["turns"], context)
    return value


def _binding(
    context: dict[str, str], commitment: str, prompt_bytes: int,
    episode: dict[str, Any],
) -> dict[str, Any]:
    return {
        "version": BINDING_VERSION,
        "metadata": context,
        "commitment": commitment,
        "prompt_bytes": prompt_bytes,
        "prior_turns": episode["turns"],
        "dispatch_phase": episode["phase"],
        "dispatched_children": [],
    }


def _validate_binding(value: Any, context: dict[str, str]) -> dict[str, Any]:
    expected_fields = {
        "version", "metadata", "commitment", "dispatch_phase",
        "dispatched_children", "prompt_bytes", "prior_turns",
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
    turns = _validate_turns(value["prior_turns"], context)
    if any(turn["context"]["prompt_id"] == context["prompt_id"] for turn in turns):
        raise ValueError("current prompt is already in the clarification history")
    size = value["prompt_bytes"]
    if (type(size) is not int or size < 0
            or size + sum(turn["bytes"] for turn in turns) > MAX_REQUEST_BYTES):
        raise ValueError("request exceeds the supported byte limit")
    children = value.get("dispatched_children")
    phase = value.get("dispatch_phase")
    coordinator = _coordinator(context["scope"])
    if (not isinstance(children, list)
            or not all(isinstance(child, str) for child in children)
            or len(children) != len(set(children))
            or any(child not in coordinator["allowed_children"] for child in children)):
        raise ValueError("prompt binding has invalid dispatch state")
    if not children:
        if phase is not None and (not turns or phase not in {"planning", "evidence"}):
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


def capture_prompt(data: dict[str, Any]) -> int:
    """Capture one raw user prompt as a private keyed commitment."""
    if data.get("hook_event_name") != "UserPromptSubmit":
        raise ValueError("capture accepts only UserPromptSubmit input")
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("UserPromptSubmit input is missing the raw prompt")
    context = _context(data)
    path = _root() / f"{_binding_key(context)}.json"
    with _locked(path) as directory_fd:
        episode = _read_episode(directory_fd, context)
        expected = _binding(
            context,
            _commitment(_secret(directory_fd), context, prompt),
            len(prompt.encode("utf-8")), episode,
        )
        _validate_binding(expected, context)
        try:
            current = _read_binding(directory_fd, path.name, context)
        except FileNotFoundError:
            _atomic_write(directory_fd, path.name, expected)
            return len(expected["prior_turns"])
        if not hmac.compare_digest(current["commitment"], expected["commitment"]):
            raise ValueError("a different prompt is already bound to this prompt identity")
        if current["prior_turns"] != expected["prior_turns"]:
            raise ValueError("clarification history changed for this prompt identity")
        return len(current["prior_turns"])


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("clarification envelope contains duplicate keys")
        result[key] = value
    return result


def _authorize_request(
    secret: bytes, context: dict[str, str], value: dict[str, Any], prompt: str,
) -> str:
    current = prompt
    if len(prompt.encode("utf-8")) > MAX_REQUEST_BYTES * 6 + 512:
        raise ValueError("Agent request exceeds the supported byte limit")
    previous = value["prior_turns"]
    if previous:
        envelope = json.loads(prompt, object_pairs_hook=_unique_object)
        if (not isinstance(envelope, dict)
                or set(envelope) != {"type", "prior_user_messages", "current_user_message"}
                or envelope["type"] != "clarified_user_request"
                or not isinstance(envelope["prior_user_messages"], list)
                or len(envelope["prior_user_messages"]) != len(previous)
                or not all(isinstance(item, str) for item in envelope["prior_user_messages"])
                or not isinstance(envelope["current_user_message"], str)):
            raise ValueError("Agent requires the complete host-bound clarification envelope")
        for turn, message in zip(previous, envelope["prior_user_messages"]):
            digest = _commitment(secret, turn["context"], message)
            if not hmac.compare_digest(turn["commitment"], digest):
                raise ValueError("Agent changed, omitted, or reordered a prior user message")
        current = envelope["current_user_message"]
        prompt = json.dumps({
            "type": "clarified_user_request",
            "prior_user_messages": envelope["prior_user_messages"],
            "current_user_message": current,
        }, ensure_ascii=True, separators=(",", ":"))
    expected = _commitment(secret, context, current)
    if not hmac.compare_digest(value["commitment"], expected):
        raise ValueError("Agent prompt does not match the current raw user prompt")
    return prompt


def authorize_agent_dispatch(data: dict[str, Any]) -> dict[str, Any]:
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
        episode = _read_episode(directory_fd, context)
        if episode["turns"] != value["prior_turns"]:
            raise ValueError("clarification history changed before dispatch")
        prompt = _authorize_request(_secret(directory_fd), context, value, prompt)
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
        return {**tool_input, "prompt": prompt}


def preserve_clarification_context(data: dict[str, Any]) -> None:
    """Keep only commitments to unresolved user input after an accepted question."""
    context = _context(data)
    path = _root() / f"{_binding_key(context)}.json"
    with _locked(path) as directory_fd:
        try:
            value = _read_binding(directory_fd, path.name, context)
        except FileNotFoundError:
            # No captured request means there is nothing safe to preserve.
            return
        episode = _read_episode(directory_fd, context)
        if episode["turns"] != value["prior_turns"]:
            raise ValueError("cannot retire stale clarification context")
        turns = [*value["prior_turns"], {
            "context": context, "commitment": value["commitment"],
            "bytes": value["prompt_bytes"],
        }]
        _validate_turns(turns, context)
        _atomic_write(directory_fd, _episode_name(context), {
            **episode, "turns": turns, "phase": value["dispatch_phase"],
        })
        unlink_owned_regular_at(directory_fd, path.name, "prompt commitment ledger")


def clear_prompt_binding(data: dict[str, Any]) -> None:
    """Remove the per-prompt commitment after a terminal coordinator response."""
    try:
        context = _context(data)
        path = _root() / f"{_binding_key(context)}.json"
        with _locked(path) as directory_fd:
            try:
                value = _read_binding(directory_fd, path.name, context)
            except FileNotFoundError:
                value = None
            if value is not None:
                episode = _read_episode(directory_fd, context)
                if episode["turns"] != value["prior_turns"]:
                    raise ValueError("cannot clear stale clarification context")
                unlink_owned_regular_at(
                    directory_fd, _episode_name(context), "clarification episode",
                )
            unlink_owned_regular_at(
                directory_fd, path.name, "prompt commitment ledger",
            )
    except (OSError, ValueError):
        pass
