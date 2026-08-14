#!/usr/bin/env python3
"""Protocol-order tests for the stdio MCP client."""

from __future__ import annotations

import os
import json
import queue
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

import mcp_client as mcp_client_module  # noqa: E402
from mcp_client import (CONTROL_TIMEOUT_S, MCP_SERVER_DIR, PRIVATE_ARTIFACT_META_KEY,
                        PRIVATE_PROVENANCE_META_KEY, SERVER_LAUNCHER,
                        TOOL_TIMEOUT_S, MCPClient, MCPToolError)  # noqa: E402


client = MCPClient()
client._proc = SimpleNamespace(poll=lambda: None, returncode=None)
client._waiters = {1: queue.Queue(maxsize=1), 2: queue.Queue(maxsize=1)}
client._waiters[2].put({"jsonrpc": "2.0", "id": 2, "result": {"value": "second"}})
client._waiters[1].put({"jsonrpc": "2.0", "id": 1, "result": {"value": "first"}})

first = client._recv(1)
second = client._recv(2)
ok = first == {"value": "first"} and second == {"value": "second"}
print(f"TEST out_of_order_responses_are_dispatched_by_id : {'PASS' if ok else 'FAIL'}")

restart_client = MCPClient()
restart_client._generation = 2
restart_client._closed = False
restart_waiter = queue.Queue(maxsize=1)
restart_client._waiters = {1: restart_waiter}
old_proc = SimpleNamespace(stdout=[])
restart_client._read_stdout(old_proc, 1)
restart_ok = not restart_client._closed and restart_waiter.empty()
print(f"TEST old_reader_cannot_poison_new_generation : {'PASS' if restart_ok else 'FAIL'}")

budget_ok = TOOL_TIMEOUT_S >= 390 and CONTROL_TIMEOUT_S < TOOL_TIMEOUT_S
print(f"TEST tool_timeout_covers_valid_server_budget : {'PASS' if budget_ok else 'FAIL'}")

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
print(f"TEST timeout_cancels_request_and_cleans_waiter : {'PASS' if cancel_ok else 'FAIL'}")

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
print(f"TEST late_cancelled_reply_cannot_contaminate_next_request : {'PASS' if late_ok else 'FAIL'}")

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
print(f"TEST private_metadata_is_recovered_outside_model_text : {'PASS' if metadata_ok else 'FAIL'}")

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
print(f"TEST model_visible_private_provenance_is_rejected : {'PASS' if hostile_ok else 'FAIL'}")


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
print(f"TEST failed_process_cleanup_retains_handle : {'PASS' if cleanup_ok else 'FAIL'}")


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
    and spawn_calls[0][0][0] != "node"
    and spawn_calls[0][1].get("env", {}).get("PATH") == "/private/hostile-path"
    and "NODE_OPTIONS" not in spawn_calls[0][1].get("env", {})
    and "NODE_PATH" not in spawn_calls[0][1].get("env", {})
    and "EXPDESIGN_RUNTIME_REGISTRY_DIR" in spawn_calls[0][1].get("env", {})
    and "EXPDESIGN_RUNTIME_REGISTRY_TOKEN" in spawn_calls[0][1].get("env", {})
    and partial_client._runtime_registry is None
)
print(
    "TEST partial_startup_cleans_process_and_ignores_hostile_node_path : "
    f"{'PASS' if partial_cleanup_ok else 'FAIL'}"
)


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
    print(
        "TEST frozen_node_forced_fallback_reaps_registered_runtime_group : "
        f"{'PASS' if forced_fallback_ok else 'FAIL'}"
    )
else:
    print("TEST frozen_node_forced_fallback_reaps_registered_runtime_group : SKIP")


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
    print(
        "TEST natural_target_exit_reaps_background_descendant_and_drains_lease : "
        f"{'PASS' if natural_descendant_ok else 'FAIL'}"
    )
else:
    print("TEST natural_target_exit_reaps_background_descendant_and_drains_lease : SKIP")

sys.exit(0 if all((ok, restart_ok, budget_ok, cancel_ok, late_ok,
                   metadata_ok, hostile_ok, cleanup_ok, partial_cleanup_ok,
                   forced_fallback_ok, natural_descendant_ok)) else 1)
