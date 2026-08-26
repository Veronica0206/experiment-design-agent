#!/usr/bin/env python3
"""Protocol-order tests for the stdio MCP client."""

from __future__ import annotations

import ast
import io
import json
import os
import queue
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace


CHECK_RESULTS: dict[str, bool] = {}


def _usage_error(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _record_check(name: str, passed: bool) -> None:
    if name in CHECK_RESULTS:
        raise AssertionError(f"duplicate MCP-client test result: {name}")
    if not isinstance(passed, bool):
        raise TypeError(f"MCP-client test result is not boolean: {name}")
    CHECK_RESULTS[name] = passed
    print(f"TEST {name} : {'PASS' if passed else 'FAIL'}")


def _record_skip(name: str) -> None:
    if name in CHECK_RESULTS:
        raise AssertionError(f"executed MCP-client test was also skipped: {name}")
    print(f"TEST {name} : SKIP")


def _result_exit_code(results: dict[str, bool]) -> int:
    return 0 if results and all(results.values()) else 1


def _finish_checks(summary_name: str | None = None) -> None:
    exit_code = _result_exit_code(CHECK_RESULTS)
    if summary_name is not None:
        print(f"TEST {summary_name} : {'PASS' if exit_code == 0 else 'FAIL'}")
    raise SystemExit(exit_code)


def _runner_structure_is_centralized(source: str) -> bool:
    """Reject test output or terminal exits outside the shared result boundary."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False

    functions = {
        node.name: (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def inside(function_names: set[str], line: int) -> bool:
        return any(
            name in functions and functions[name][0] <= line <= functions[name][1]
            for name in function_names
        )

    output_functions = {"_record_check", "_record_skip", "_finish_checks"}
    exit_functions = {"_usage_error", "_finish_checks"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            is_print = isinstance(node.func, ast.Name) and node.func.id == "print"
            contains_test_output = any(
                isinstance(part, ast.Constant)
                and isinstance(part.value, str)
                and "TEST " in part.value
                for part in ast.walk(node)
            )
            if is_print and contains_test_output and not inside(
                output_functions, node.lineno,
            ):
                return False
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "sys"
                and node.func.attr == "exit"
            ):
                return False
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            if (
                isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "SystemExit"
                and not inside(exit_functions, node.lineno)
            ):
                return False
    return True


if "EXPDESIGN_PUBLIC_ONLY" in os.environ:
    _usage_error("EXPDESIGN_PUBLIC_ONLY is forbidden; use --public-only")

if sys.argv[1:] not in ([], ["--public-only"]):
    _usage_error("Usage: test_mcp_client.py [--public-only]")

PUBLIC_ONLY = sys.argv[1:] == ["--public-only"]

runner_source = Path(__file__).read_text(encoding="utf-8")
registry_meta_ok = (
    _result_exit_code({"passing": True}) == 0
    and _result_exit_code({"passing": True, "failing": False}) == 1
    and _result_exit_code({}) == 1
)
_record_check("result_registry_fails_closed", registry_meta_ok)
structure_meta_ok = (
    _runner_structure_is_centralized(runner_source)
    and not _runner_structure_is_centralized(
        runner_source + '\nprint("TEST unregistered_result : FAIL")\n'
    )
    and not _runner_structure_is_centralized(runner_source + "\nsys.exit(0)\n")
)
_record_check("test_runner_structure_is_centralized", structure_meta_ok)
if not registry_meta_ok or not structure_meta_ok:
    _finish_checks()

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

import mcp_client as mcp_client_module  # noqa: E402
from mcp_client import (CONTROL_TIMEOUT_S, MAX_JSONRPC_MESSAGE_BYTES,
                        MAX_SERVER_STDERR_BYTES, MCP_SERVER_DIR,
                        PRIVATE_ARTIFACT_META_KEY, PRIVATE_PROVENANCE_META_KEY,
                        PUBLIC_TOOL_ERROR_MESSAGES, SERVER_LAUNCHER,
                        TOOL_TIMEOUT_S, MCPClient, MCPToolError)  # noqa: E402

client = MCPClient()
client._proc = SimpleNamespace(poll=lambda: None, returncode=None)
client._waiters = {1: queue.Queue(maxsize=1), 2: queue.Queue(maxsize=1)}
client._waiters[2].put({"jsonrpc": "2.0", "id": 2, "result": {"value": "second"}})
client._waiters[1].put({"jsonrpc": "2.0", "id": 1, "result": {"value": "first"}})

first = client._recv(1)
second = client._recv(2)
ok = first == {"value": "first"} and second == {"value": "second"}
_record_check("out_of_order_responses_are_dispatched_by_id", ok)

restart_client = MCPClient()
restart_client._generation = 2
restart_client._closed = False
restart_waiter = queue.Queue(maxsize=1)
restart_client._waiters = {1: restart_waiter}
old_proc = SimpleNamespace(stdout=[])
restart_client._read_stdout(old_proc, 1)
restart_ok = not restart_client._closed and restart_waiter.empty()
_record_check("old_reader_cannot_poison_new_generation", restart_ok)

budget_ok = TOOL_TIMEOUT_S >= 390 and CONTROL_TIMEOUT_S < TOOL_TIMEOUT_S
_record_check("tool_timeout_covers_valid_server_budget", budget_ok)

timeout_client = MCPClient()
timeout_client._proc = SimpleNamespace(poll=lambda: None, returncode=None)
timeout_client._waiters = {9: queue.Queue(maxsize=1)}
sent = []
timeout_client._send = sent.append
try:
    timeout_client._recv(9, timeout_s=0.01)
except TimeoutError:
    timed_out = True
else:
    timed_out = False
cancel_ok = (
    timed_out
    and 9 not in timeout_client._waiters
    and sent == [{
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 9, "reason": "client timeout"},
    }]
)
_record_check("timeout_cancels_request_and_cleans_waiter", cancel_ok)

late_client = MCPClient()
late_client._generation = 3
late_client._closed = False
late_waiter = queue.Queue(maxsize=2)
late_client._waiters = {10: late_waiter}
late_proc = SimpleNamespace(
    stdout=[
        '{"jsonrpc":"2.0","id":9,"result":{"value":"late"}}\n',
        '{"jsonrpc":"2.0","id":10,"result":{"value":"next"}}\n',
    ],
    poll=lambda: None,
    returncode=None,
)
late_client._proc = late_proc
late_client._read_stdout(late_proc, 3)
next_result = late_client._recv(10)
late_ok = next_result == {"value": "next"}
_record_check("late_cancelled_reply_cannot_contaminate_next_request", late_ok)

duplicate_client = MCPClient()
duplicate_client._generation = 4
duplicate_client._closed = False
duplicate_client._waiters = {
    31: queue.Queue(maxsize=1),
    32: queue.Queue(maxsize=1),
}
duplicate_proc = SimpleNamespace(
    stdout=[
        '{"jsonrpc":"2.0","id":31,"result":{"value":"first"}}\n',
        '{"jsonrpc":"2.0","id":31,"result":{"value":"duplicate"}}\n',
        '{"jsonrpc":"2.0","id":32,"result":{"value":"must-not-arrive"}}\n',
    ],
    poll=lambda: None,
    returncode=None,
)
duplicate_client._proc = duplicate_proc
duplicate_cleanup_calls = []
duplicate_client._schedule_protocol_shutdown = (
    lambda generation, proc: duplicate_cleanup_calls.append((generation, proc))
)
duplicate_reader = threading.Thread(
    target=duplicate_client._read_stdout,
    args=(duplicate_proc, 4),
    daemon=True,
)
duplicate_reader.start()
duplicate_reader.join(timeout=1)
duplicate_errors = []
if not duplicate_reader.is_alive():
    for request_id in (31, 32):
        try:
            duplicate_client._recv(request_id, timeout_s=0.1)
        except MCPToolError as error:
            duplicate_errors.append(str(error))
duplicate_ok = (
    not duplicate_reader.is_alive()
    and duplicate_client._closed
    and duplicate_cleanup_calls == [(4, duplicate_proc)]
    and duplicate_errors == [
        "MCP protocol violation: duplicate response id",
        "MCP protocol violation: duplicate response id",
    ]
)
_record_check("duplicate_response_fails_generation_without_blocking_reader", duplicate_ok)

eof_client = MCPClient()
eof_client._generation = 5
eof_client._closed = False
eof_waiter = queue.Queue(maxsize=1)
eof_waiter.put({"jsonrpc": "2.0", "id": 41, "result": {"value": "complete"}})
eof_client._waiters = {41: eof_waiter}
eof_proc = SimpleNamespace(stdout=[], poll=lambda: 0, returncode=0)
eof_client._proc = eof_proc
eof_reader = threading.Thread(
    target=eof_client._read_stdout,
    args=(eof_proc, 5),
    daemon=True,
)
eof_reader.start()
eof_reader.join(timeout=1)
eof_result = eof_client._recv(41, timeout_s=0.1) if not eof_reader.is_alive() else None
eof_ok = not eof_reader.is_alive() and eof_result == {"value": "complete"}
_record_check("eof_never_blocks_or_overwrites_queued_response", eof_ok)


def malformed_response_error(raw_message: str) -> str | None:
    malformed_client = MCPClient()
    malformed_client._generation = 6
    malformed_client._closed = False
    malformed_client._waiters = {1: queue.Queue(maxsize=1)}
    malformed_proc = SimpleNamespace(
        stdout=[raw_message + "\n"], poll=lambda: None, returncode=None,
    )
    malformed_client._proc = malformed_proc
    malformed_client._schedule_protocol_shutdown = lambda generation, proc: None
    malformed_client._read_stdout(malformed_proc, 6)
    try:
        malformed_client._recv(1, timeout_s=0.1)
    except MCPToolError as error:
        return str(error)
    return None


malformed_envelopes = [
    '{"jsonrpc":"2.0","id":true,"result":{}}',
    '{"id":1,"result":{}}',
    '{"jsonrpc":"2.0","id":1,"result":{},"error":{"code":-1,"message":"x"}}',
    '{"jsonrpc":"2.0","id":1,"error":{"code":true,"message":"x"}}',
]
malformed_errors = [malformed_response_error(raw) for raw in malformed_envelopes]
malformed_ok = malformed_errors == [
    "MCP protocol violation: malformed JSON-RPC envelope"
] * len(malformed_envelopes)
_record_check("malformed_jsonrpc_envelopes_fail_closed", malformed_ok)


def sized_response(response_id: int, size: int) -> tuple[bytes, int]:
    prefix = (
        b'{"jsonrpc":"2.0","id":' + str(response_id).encode("ascii")
        + b',"result":{"blob":"'
    )
    suffix = b'"}}'
    fill = size - len(prefix) - len(suffix)
    if fill < 0:
        raise ValueError("requested JSON-RPC test message is too small")
    return prefix + (b"x" * fill) + suffix, fill


exact_line, exact_fill = sized_response(71, MAX_JSONRPC_MESSAGE_BYTES)
exact_client = MCPClient()
exact_client._generation = 61
exact_client._closed = False
exact_client._waiters = {71: queue.Queue(maxsize=1)}
exact_proc = SimpleNamespace(
    stdout=io.BytesIO(exact_line), poll=lambda: None, returncode=None,
)
exact_client._proc = exact_proc
exact_client._schedule_protocol_shutdown = lambda generation, proc: None
exact_client._read_stdout(exact_proc, 61)
try:
    exact_result = exact_client._recv(71, timeout_s=0.1)
except MCPToolError:
    exact_result = None
exact_limit_ok = (
    exact_result is not None
    and len(exact_result.get("blob", "")) == exact_fill
    and exact_client._reader_error is None
)
_record_check("jsonrpc_message_at_byte_limit_is_accepted", exact_limit_ok)

over_line, _over_fill = sized_response(72, MAX_JSONRPC_MESSAGE_BYTES + 1)
over_client = MCPClient()
over_client._generation = 62
over_client._closed = False
over_client._waiters = {72: queue.Queue(maxsize=1)}
over_proc = SimpleNamespace(
    stdout=io.BytesIO(over_line), poll=lambda: None, returncode=None,
)
over_client._proc = over_proc
over_cleanup_calls = []
over_client._schedule_protocol_shutdown = (
    lambda generation, proc: over_cleanup_calls.append((generation, proc))
)
over_client._read_stdout(over_proc, 62)
try:
    over_client._recv(72, timeout_s=0.1)
except MCPToolError as error:
    over_error = str(error)
else:
    over_error = None
over_limit_ok = (
    over_error == (
        "MCP protocol violation: JSON-RPC message exceeds "
        f"{MAX_JSONRPC_MESSAGE_BYTES} bytes"
    )
    and over_client._closed
    and over_cleanup_calls == [(62, over_proc)]
)
_record_check("jsonrpc_message_one_byte_over_limit_fails_generation", over_limit_ok)

private_path = b"/private/runtime/install/diagnostic-sentinel"
stderr_client = MCPClient()
stderr_client._generation = 63
stderr_proc = SimpleNamespace(
    stderr=io.BytesIO(b"x" * (MAX_SERVER_STDERR_BYTES + 17) + private_path),
)
stderr_client._drain_stderr(stderr_proc, 63)
stderr_bound_ok = (
    len(stderr_client._stderr_buf) == MAX_SERVER_STDERR_BYTES
    and private_path.decode("ascii") in stderr_client._private_recent_stderr()
)
_record_check("stderr_diagnostics_are_strictly_byte_bounded", stderr_bound_ok)

hostile_error_client = MCPClient()
hostile_error_client._call = lambda *_args, **_kwargs: {
    "isError": True,
    "content": [{
        "type": "text",
        "text": "handler failed at /private/runtime/install/diagnostic-sentinel",
    }],
}
try:
    hostile_error_client.call_tool("indirect_compare", {})
except MCPToolError as error:
    hostile_error = str(error)
else:
    hostile_error = ""
hostile_error_ok = (
    private_path.decode("ascii") not in hostile_error
    and hostile_error == (
        "Tool request failed [internal_error]: "
        + PUBLIC_TOOL_ERROR_MESSAGES["internal_error"]
    )
)
_record_check("arbitrary_server_error_text_is_not_reflected", hostile_error_ok)

public_error_client = MCPClient()
public_error_client._call = lambda *_args, **_kwargs: {
    "isError": True,
    "content": [{
        "type": "text",
        "text": json.dumps({"error": {
            "code": "request_cancelled",
            "message": PUBLIC_TOOL_ERROR_MESSAGES["request_cancelled"],
        }}),
    }],
}
try:
    public_error_client.call_tool("sample_size", {})
except MCPToolError as error:
    public_error = str(error)
else:
    public_error = ""
public_error_ok = public_error == (
    "Tool request failed [request_cancelled]: "
    + PUBLIC_TOOL_ERROR_MESSAGES["request_cancelled"]
)
_record_check("fixed_public_error_taxonomy_is_preserved", public_error_ok)

invalid_request_client = MCPClient()
invalid_request_client._call = lambda *_args, **_kwargs: {
    "isError": True,
    "content": [{
        "type": "text",
        "text": json.dumps({"error": {
            "code": "invalid_request",
            "message": PUBLIC_TOOL_ERROR_MESSAGES["invalid_request"],
        }}),
    }],
}
try:
    invalid_request_client.call_tool("factorial_design", {})
except MCPToolError as error:
    invalid_request_error = str(error)
else:
    invalid_request_error = ""
invalid_request_ok = invalid_request_error == (
    "Tool request failed [invalid_request]: "
    + PUBLIC_TOOL_ERROR_MESSAGES["invalid_request"]
)
_record_check("fixed_invalid_request_taxonomy_is_preserved", invalid_request_ok)

unknown_code_client = MCPClient()
unknown_code_client._call = lambda *_args, **_kwargs: {
    "isError": True,
    "content": [{
        "type": "text",
        "text": json.dumps({"error": {
            "code": "private_diagnostic",
            "message": None,
        }}),
    }],
}
try:
    unknown_code_client.call_tool("sample_size", {})
except MCPToolError as error:
    unknown_code_error = str(error)
else:
    unknown_code_error = ""
unknown_code_ok = unknown_code_error == (
    "Tool request failed [internal_error]: "
    + PUBLIC_TOOL_ERROR_MESSAGES["internal_error"]
)
_record_check("unknown_public_error_codes_fail_closed", unknown_code_ok)


generation_client = MCPClient()
old_generation_proc = object()
new_generation_proc = object()
generation_client._generation = 8
generation_client._proc = new_generation_proc
old_cleanup_calls = []
generation_client._stop_locked = lambda: old_cleanup_calls.append(True)
generation_client._cleanup_protocol_generation(7, old_generation_proc)
old_generation_ok = old_cleanup_calls == [] and generation_client._proc is new_generation_proc
_record_check("delayed_old_generation_cleanup_cannot_stop_restart", old_generation_ok)


class CountingRegistry:
    def __init__(self):
        self.calls = []
        self.calls_lock = threading.Lock()

    def _record(self, name):
        with self.calls_lock:
            self.calls.append(name)
        time.sleep(0.01)

    def stop_accepting(self):
        self._record("stop_accepting")

    def terminate_registered_groups(self):
        self._record("terminate_registered_groups")

    def finish(self):
        self._record("finish")


serialized_client = MCPClient()
serialized_registry = CountingRegistry()
serialized_client._runtime_registry = serialized_registry
serialized_client._proc = SimpleNamespace(poll=lambda: 0, returncode=0)
serialized_errors = []


def capture_stop_error(_index):
    try:
        serialized_client.stop()
    except Exception as error:
        serialized_errors.append(str(error))


serialized_threads = [
    threading.Thread(target=capture_stop_error, args=(index,), daemon=True)
    for index in range(2)
]
for thread in serialized_threads:
    thread.start()
for thread in serialized_threads:
    thread.join(timeout=1)
serialized_ok = (
    not any(thread.is_alive() for thread in serialized_threads)
    and serialized_errors == []
    and serialized_registry.calls == [
        "stop_accepting", "terminate_registered_groups", "finish",
    ]
    and serialized_client._runtime_registry is None
)
_record_check("concurrent_stop_serializes_registry_cleanup", serialized_ok)

analysis_id = "analysis-private-meta"
input_digest = "1" * 64
artifact_digest = "2" * 64
artifact_handle = "12345678-1234-4123-8123-123456789abc"
artifact_path = "/managed/assignment.csv"
model_payload = {
    "method": "block",
    "_verification": {"identity": {"analysis_id": analysis_id}},
    "_provenance": {"config_bound": True, "input_file_count": 1,
                    "artifact_count": 1},
}
model_text = json.dumps(model_payload)
metadata_response = {
    "content": [
        {"type": "text", "text": model_text},
        {
            "type": "resource_link", "name": "assignment.csv",
            "uri": f"expdesign-artifact://{analysis_id}/{artifact_handle}",
            "annotations": {"audience": ["user"]},
            "_meta": {
                PRIVATE_ARTIFACT_META_KEY: {
                    "version": 2, "analysis_id": analysis_id,
                    "sha256": artifact_digest, "handle": artifact_handle,
                },
            },
        },
    ],
    "_meta": {
        PRIVATE_PROVENANCE_META_KEY: {
            "version": 2, "analysis_id": analysis_id,
            "config_hash": "3" * 64,
            "input_hashes": {"ipd_file": input_digest},
            "artifact_handles": {artifact_handle: artifact_path},
        },
    },
}
metadata_client = MCPClient()
metadata_client._call = lambda *_args, **_kwargs: metadata_response
metadata_result = metadata_client.call_tool("randomize", {})
metadata_ok = (
    input_digest not in model_text and artifact_digest not in model_text
    and metadata_result.get("_private_provenance", {}).get("input_hashes")
    == {"ipd_file": input_digest}
    and metadata_result.get("_private_provenance", {}).get("artifact_hashes")
    == {"assignment.csv": artifact_digest}
    and metadata_result.get("_private_provenance", {}).get("artifact_handles")
    == {artifact_handle: artifact_path}
    and metadata_result.get("_private_provenance", {}).get("analysis_id") == analysis_id
    and not metadata_response["content"][1]["uri"].startswith("file:")
    and artifact_path not in metadata_response["content"][1]["uri"]
)
_record_check("private_metadata_is_recovered_outside_model_text", metadata_ok)

hostile_client = MCPClient()
hostile_client._call = lambda *_args, **_kwargs: {
    "content": [{"type": "text", "text": json.dumps({
        "_private_provenance": {"artifact_hashes": {"assignment.csv": artifact_digest}},
    })}],
}
try:
    hostile_client.call_tool("randomize", {})
except MCPToolError:
    hostile_ok = True
else:
    hostile_ok = False
_record_check("model_visible_private_provenance_is_rejected", hostile_ok)


class CleanupProcess:
    def __init__(self):
        self.alive = True
        self.returncode = None

    def poll(self):
        return None if self.alive else self.returncode

    def terminate(self):
        raise OSError("termination failed")

    def wait(self, timeout=None):
        if self.alive:
            raise TimeoutError("still alive")
        return self.returncode

    def kill(self):
        raise OSError("kill failed")


cleanup_client = MCPClient()
cleanup_process = CleanupProcess()
cleanup_client._proc = cleanup_process
try:
    cleanup_client.stop()
except RuntimeError:
    cleanup_failed_closed = True
else:
    cleanup_failed_closed = False
cleanup_ok = cleanup_failed_closed and cleanup_client._proc is cleanup_process
_record_check("failed_process_cleanup_retains_handle", cleanup_ok)

if PUBLIC_ONLY:
    _finish_checks("public_only_protocol_boundary")


class PartialStartupProcess:
    def __init__(self):
        self.alive = True
        self.returncode = None
        self.pid = 424242
        self.stdin = SimpleNamespace()
        self.stdout = []
        self.stderr = []

    def poll(self):
        return None if self.alive else self.returncode

    def terminate(self):
        self.alive = False
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.alive = False
        self.returncode = -9


partial_process = PartialStartupProcess()
spawn_calls = []
original_popen = mcp_client_module.subprocess.Popen
original_path = os.environ.get("PATH")
original_node_options = os.environ.get("NODE_OPTIONS")
original_node_path = os.environ.get("NODE_PATH")
os.environ["PATH"] = "/private/hostile-path"
os.environ["NODE_OPTIONS"] = "--require=/private/hostile-preload.js"
os.environ["NODE_PATH"] = "/private/hostile-modules"


def fake_popen(args, **kwargs):
    spawn_calls.append((list(args), dict(kwargs)))
    return partial_process


mcp_client_module.subprocess.Popen = fake_popen
partial_client = MCPClient()
partial_client._call = lambda *_args, **_kwargs: (_ for _ in ()).throw(
    RuntimeError("initialize failed after spawn")
)
try:
    partial_client.start()
except RuntimeError:
    partial_failed = True
else:
    partial_failed = False
finally:
    mcp_client_module.subprocess.Popen = original_popen
    if original_path is None:
        os.environ.pop("PATH", None)
    else:
        os.environ["PATH"] = original_path
    for key, original in (
        ("NODE_OPTIONS", original_node_options), ("NODE_PATH", original_node_path),
    ):
        if original is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = original

partial_cleanup_ok = (
    partial_failed and not partial_process.alive and partial_client._proc is None
    and len(spawn_calls) == 1
    and spawn_calls[0][0] == ["/bin/sh", str(SERVER_LAUNCHER)]
    and spawn_calls[0][1].get("cwd") == str(MCP_SERVER_DIR)
    and spawn_calls[0][1].get("start_new_session") is True
    and spawn_calls[0][1].get("text") is False
    and spawn_calls[0][1].get("bufsize") == 0
    and spawn_calls[0][0][0] != "node"
    and spawn_calls[0][1].get("env", {}).get("PATH") == "/private/hostile-path"
    and "NODE_OPTIONS" not in spawn_calls[0][1].get("env", {})
    and "NODE_PATH" not in spawn_calls[0][1].get("env", {})
    and "EXPDESIGN_RUNTIME_REGISTRY_DIR" in spawn_calls[0][1].get("env", {})
    and "EXPDESIGN_RUNTIME_REGISTRY_TOKEN" in spawn_calls[0][1].get("env", {})
    and partial_client._runtime_registry is None
)
_record_check("partial_startup_cleans_process_and_ignores_hostile_node_path", partial_cleanup_ok)


def identity_probe_available() -> bool:
    if sys.platform.startswith("linux"):
        return True
    if sys.platform != "darwin":
        return False
    try:
        outcome = mcp_client_module.subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())],
            capture_output=True,
            text=True,
            timeout=2,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
            check=False,
        )
    except Exception:
        return False
    return outcome.returncode == 0 and bool(outcome.stdout.strip())


def wait_for_path(path: Path, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.025)
    return False


def wait_for_registry_entry(path: Path, timeout: float = 10) -> Path | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entries = list(path.glob("runtime-*-*.json")) if path.is_dir() else []
        if len(entries) == 1:
            return entries[0]
        time.sleep(0.025)
    return None


def process_or_group_gone(identifier: int, *, group: bool = False) -> bool:
    try:
        if group:
            os.killpg(identifier, 0)
        else:
            os.kill(identifier, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def wait_until_gone(identifier: int, *, group: bool = False) -> bool:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process_or_group_gone(identifier, group=group):
            return True
        time.sleep(0.025)
    return False


forced_fallback_ok = True
if os.name == "posix" and identity_probe_available():
    original_rscript = os.environ.get("EXPDESIGN_RSCRIPT")
    frozen_client = MCPClient()
    frozen_node_pid = None
    frozen_runtime_pid = None
    frozen_runtime_group = None
    frozen_worker = None
    frozen_worker_errors = []
    with tempfile.TemporaryDirectory(prefix="expdesign-frozen-node-") as temp_root:
        temp_path = Path(temp_root)
        runtime_pid_file = temp_path / "runtime.pid"
        rscript_shim = temp_path / "Rscript"
        rscript_shim.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$$\" > '{runtime_pid_file}'\n"
            "exec /bin/sleep 60\n",
            encoding="utf-8",
        )
        rscript_shim.chmod(0o700)
        try:
            os.environ["EXPDESIGN_RSCRIPT"] = str(rscript_shim)
            frozen_client.start()
            assert frozen_client._proc is not None
            assert frozen_client._runtime_registry is not None
            frozen_node_pid = frozen_client._proc.pid
            registry_path = frozen_client._runtime_registry.path

            def blocked_runtime_call():
                try:
                    frozen_client.call_tool("run_tests", {})
                except BaseException as exc:
                    frozen_worker_errors.append(exc)

            frozen_worker = threading.Thread(target=blocked_runtime_call, daemon=True)
            frozen_worker.start()
            entry_path = wait_for_registry_entry(registry_path)
            if entry_path is None or not wait_for_path(runtime_pid_file):
                raise AssertionError("runtime supervisor did not publish its live lease")
            lease_raw = entry_path.read_text(encoding="utf-8")
            lease = json.loads(lease_raw)
            frozen_runtime_group = int(lease["pid"])
            frozen_runtime_pid = int(runtime_pid_file.read_text(encoding="ascii").strip())
            if os.getpgid(frozen_runtime_pid) != frozen_runtime_group:
                raise AssertionError("runtime target was not in its supervisor group")
            if "token" in lease or frozen_client._runtime_registry.token in lease_raw:
                raise AssertionError("runtime registry exposed its authentication token")

            # Freeze both control planes. The wrapper cannot observe .stop and
            # Node cannot run shutdown hooks, so only the Python host fallback
            # can authenticate, revalidate, and kill the complete runtime group.
            os.killpg(frozen_runtime_group, signal.SIGSTOP)
            os.kill(frozen_node_pid, signal.SIGSTOP)
            frozen_client.stop()
            frozen_worker.join(timeout=10)
            forced_fallback_ok = (
                not frozen_worker.is_alive()
                and len(frozen_worker_errors) == 1
                and isinstance(frozen_worker_errors[0], MCPToolError)
                and wait_until_gone(frozen_runtime_pid)
                and wait_until_gone(frozen_runtime_group, group=True)
                and wait_until_gone(frozen_node_pid)
                and not registry_path.exists()
                and frozen_client._proc is None
                and frozen_client._runtime_registry is None
            )
        except BaseException:
            forced_fallback_ok = False
        finally:
            if original_rscript is None:
                os.environ.pop("EXPDESIGN_RSCRIPT", None)
            else:
                os.environ["EXPDESIGN_RSCRIPT"] = original_rscript
            if frozen_runtime_group is not None:
                for cleanup_signal in (signal.SIGCONT, signal.SIGKILL):
                    try:
                        os.killpg(frozen_runtime_group, cleanup_signal)
                    except ProcessLookupError:
                        break
            if frozen_node_pid is not None and frozen_client._proc is not None:
                try:
                    os.kill(frozen_node_pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                try:
                    os.killpg(frozen_node_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                frozen_client.stop()
            except RuntimeError:
                pass
            if frozen_worker is not None:
                frozen_worker.join(timeout=2)
    _record_check(
        "frozen_node_forced_fallback_reaps_registered_runtime_group",
        forced_fallback_ok,
    )
else:
    _record_skip("frozen_node_forced_fallback_reaps_registered_runtime_group")


natural_descendant_ok = True
if os.name == "posix" and identity_probe_available():
    original_rscript = os.environ.get("EXPDESIGN_RSCRIPT")
    descendant_client = MCPClient()
    descendant_group = None
    descendant_pid = None
    with tempfile.TemporaryDirectory(prefix="expdesign-natural-descendant-") as temp_root:
        temp_path = Path(temp_root)
        descendant_pid_file = temp_path / "descendant.pid"
        rscript_shim = temp_path / "Rscript"
        rscript_shim.write_text(
            "#!/bin/sh\n"
            "/bin/sleep 60 </dev/null >/dev/null 2>&1 &\n"
            f"printf '%s\\n' \"$!\" > '{descendant_pid_file}'\n"
            "exit 0\n",
            encoding="utf-8",
        )
        rscript_shim.chmod(0o700)
        registry_path = None
        try:
            os.environ["EXPDESIGN_RSCRIPT"] = str(rscript_shim)
            descendant_client.start()
            assert descendant_client._runtime_registry is not None
            registry_path = descendant_client._runtime_registry.path
            try:
                descendant_client.call_tool("run_tests", {})
            except MCPToolError:
                call_failed_closed = True
            else:
                call_failed_closed = False
            entry_path = wait_for_registry_entry(registry_path)
            if entry_path is None or not wait_for_path(descendant_pid_file):
                raise AssertionError("background descendant lease was not retained")
            lease = json.loads(entry_path.read_text(encoding="utf-8"))
            descendant_group = int(lease["pid"])
            descendant_pid = int(descendant_pid_file.read_text(encoding="ascii").strip())
            descendant_gone = wait_until_gone(descendant_pid)
            group_gone = wait_until_gone(descendant_group, group=True)
            descendant_client.stop()
            natural_descendant_ok = (
                call_failed_closed and descendant_gone and group_gone
                and not registry_path.exists()
                and descendant_client._proc is None
                and descendant_client._runtime_registry is None
            )
        except BaseException:
            natural_descendant_ok = False
        finally:
            if original_rscript is None:
                os.environ.pop("EXPDESIGN_RSCRIPT", None)
            else:
                os.environ["EXPDESIGN_RSCRIPT"] = original_rscript
            if descendant_group is not None:
                try:
                    os.killpg(descendant_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                descendant_client.stop()
            except RuntimeError:
                pass
    _record_check(
        "natural_target_exit_reaps_background_descendant_and_drains_lease",
        natural_descendant_ok,
    )
else:
    _record_skip("natural_target_exit_reaps_background_descendant_and_drains_lease")

_finish_checks()
