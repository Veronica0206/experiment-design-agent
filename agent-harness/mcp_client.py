"""MCP client that spawns the experiment-design MCP server and calls tools.

Manages the node subprocess lifecycle and JSON-RPC communication over stdio.
"""

from __future__ import annotations

import atexit
import json
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


MCP_SERVER_DIR = Path(__file__).resolve().parent.parent / "mcp-server"
SERVER_ENTRY = MCP_SERVER_DIR / "dist" / "index.js"

CONTROL_TIMEOUT_S = 30
# A valid stochastic tool can consume three independent 120-second R budgets
# (cached regression on first use, analysis, replay) plus a 30-second verifier.
# Leave bounded transport overhead so the client never preempts server policy.
TOOL_TIMEOUT_S = 450
STOP_GRACE_S = 5

_EOF = object()  # sentinel: server closed stdout

PRIVATE_PROVENANCE_META_KEY = "experiment-design/private-provenance"
PRIVATE_ARTIFACT_META_KEY = "experiment-design/private-artifact"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_HANDLE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class MCPToolError(RuntimeError):
    """Raised when a tool call fails (server-side error or bad output)."""


class MCPClient:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._id_counter = 0
        self._stderr_buf: list[str] = []
        self._stderr_thread: threading.Thread | None = None
        self._stdout_thread: threading.Thread | None = None
        self._waiters: dict[int, queue.Queue] = {}
        self._waiters_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = False
        self._generation = 0

    def start(self) -> dict:
        if not SERVER_ENTRY.exists():
            raise FileNotFoundError(
                f"MCP server not built. Run 'npm run build' in {MCP_SERVER_DIR}"
            )
        # Fully retire a prior generation before publishing new shared state.
        if self._proc is not None or any(
            thread is not None and thread.is_alive()
            for thread in (self._stdout_thread, self._stderr_thread)
        ):
            self.stop()
        self._generation += 1
        generation = self._generation
        # Support restart on the same instance: reset the closed flag and use a
        # FRESH inbox — the old queue may hold a stale _EOF sentinel from the
        # previous stdout thread, which would fail the first _send immediately.
        self._closed = False
        self._waiters = {}
        self._proc = subprocess.Popen(
            ["node", str(SERVER_ENTRY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        proc = self._proc
        # Drain stderr continuously in a background thread so a chatty server
        # can never deadlock by filling its stderr pipe (B-8).
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, args=(proc, generation), daemon=True,
        )
        self._stderr_thread.start()
        # Read stdout in a thread (blocking readline on the raw stream) and feed
        # parsed messages into a queue — avoids select() on a buffered text
        # stream, where buffered bytes are invisible to select (V-1).
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, args=(proc, generation), daemon=True,
        )
        self._stdout_thread.start()
        # Ensure the child is reaped even on an abnormal interpreter exit (B-10).
        atexit.register(self.stop)

        resp = self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "experiment-design-harness", "version": "1.1.0"},
        })
        self._notify("notifications/initialized")
        return resp

    def stop(self):
        proc, self._proc = self._proc, None
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=STOP_GRACE_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=STOP_GRACE_S)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            pass
        current = threading.current_thread()
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=STOP_GRACE_S)
        self._stdout_thread = None
        self._stderr_thread = None
        self._closed = True

    def list_tools(self) -> list[dict]:
        resp = self._call("tools/list", {})
        return resp.get("tools", [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict:
        resp = self._call(
            "tools/call", {"name": name, "arguments": arguments},
            timeout_s=TOOL_TIMEOUT_S,
        )
        content = resp.get("content") or []
        text = content[0].get("text", "") if content else ""

        # The SDK reports a thrown handler error as isError=true with the error
        # message (plain text, not JSON) in the content — surface it as-is
        # rather than blindly json.loads()-ing it into an opaque decode error.
        if resp.get("isError"):
            raise MCPToolError(f"Tool '{name}' failed: {text.strip() or '(no message)'}")

        if not content:
            return resp
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                if "_private_provenance" in parsed:
                    raise MCPToolError(
                        f"Tool '{name}' placed host-only provenance in model-visible text"
                    )
                resources = [
                    block for block in content[1:]
                    if isinstance(block, dict) and block.get("type") == "resource_link"
                ]
                private_provenance = self._private_provenance(resp, resources, parsed)
                if private_provenance:
                    parsed["_private_provenance"] = private_provenance
                if resources:
                    parsed["_private_resources"] = resources
            return parsed
        except json.JSONDecodeError as e:
            raise MCPToolError(
                f"Tool '{name}' returned non-JSON output: {text[:500]!r} ({e})"
            ) from e

    @staticmethod
    def _digest_map(value: Any, label: str) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise MCPToolError(f"Private {label} metadata is malformed")
        normalized: dict[str, str] = {}
        for key, digest in value.items():
            if not isinstance(key, str) or not key or len(key) > 4096:
                raise MCPToolError(f"Private {label} metadata has an invalid key")
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest.lower()):
                raise MCPToolError(f"Private {label} metadata has an invalid digest")
            normalized[key] = digest.lower()
        return normalized

    @classmethod
    def _private_provenance(
        cls,
        response: dict[str, Any],
        resources: list[dict[str, Any]],
        parsed: dict[str, Any],
    ) -> dict[str, Any]:
        """Recover client-only provenance without copying it into model text."""
        response_meta = response.get("_meta")
        private = (
            response_meta.get(PRIVATE_PROVENANCE_META_KEY)
            if isinstance(response_meta, dict) else None
        )
        result: dict[str, Any] = {}
        analysis_ids: set[str] = set()
        if private is not None:
            if not isinstance(private, dict) or private.get("version") != 2:
                raise MCPToolError("Private provenance metadata is malformed")
            analysis_id = private.get("analysis_id")
            if not isinstance(analysis_id, str) or not analysis_id:
                raise MCPToolError("Private provenance metadata lacks an analysis identity")
            analysis_ids.add(analysis_id)
            result = {
                "version": 2,
                "analysis_id": analysis_id,
                "input_hashes": cls._digest_map(
                    private.get("input_hashes"), "input-hash",
                ),
                "artifact_hashes": {},
            }
            raw_handles = private.get("artifact_handles")
            if not isinstance(raw_handles, dict):
                raise MCPToolError("Private provenance metadata lacks artifact handles")
            artifact_handles: dict[str, str] = {}
            for handle, path in raw_handles.items():
                if (not isinstance(handle, str) or not _ARTIFACT_HANDLE.fullmatch(handle)
                        or not isinstance(path, str) or not path or len(path) > 4096
                        or not Path(path).is_absolute()):
                    raise MCPToolError("Private artifact handle metadata is malformed")
                artifact_handles[handle.lower()] = path
            result["artifact_handles"] = artifact_handles
            config_hash = private.get("config_hash")
            if config_hash is not None:
                if not isinstance(config_hash, str) or not _SHA256.fullmatch(config_hash.lower()):
                    raise MCPToolError("Private provenance metadata has an invalid config digest")
                result["config_hash"] = config_hash.lower()

        artifact_hashes: dict[str, str] = {}
        resource_handles: set[str] = set()
        for resource in resources:
            resource_meta = resource.get("_meta")
            artifact = (
                resource_meta.get(PRIVATE_ARTIFACT_META_KEY)
                if isinstance(resource_meta, dict) else None
            )
            if not isinstance(artifact, dict) or artifact.get("version") != 2:
                raise MCPToolError("Private artifact resource lacks trusted digest metadata")
            analysis_id = artifact.get("analysis_id")
            resource_name = resource.get("name")
            handle = artifact.get("handle")
            if not isinstance(analysis_id, str) or not analysis_id:
                raise MCPToolError("Private artifact metadata lacks an analysis identity")
            if not isinstance(resource_name, str) or not resource_name:
                raise MCPToolError("Private artifact resource lacks a name")
            if not isinstance(handle, str) or not _ARTIFACT_HANDLE.fullmatch(handle):
                raise MCPToolError("Private artifact resource lacks a valid opaque handle")
            handle = handle.lower()
            parsed_uri = urlparse(str(resource.get("uri") or ""))
            if (
                parsed_uri.scheme != "expdesign-artifact"
                or unquote(parsed_uri.netloc) != analysis_id
                or parsed_uri.path != f"/{handle}"
                or bool(parsed_uri.params) or bool(parsed_uri.query) or bool(parsed_uri.fragment)
            ):
                raise MCPToolError("Private artifact resource URI is not identity-bound")
            if handle in resource_handles:
                raise MCPToolError("A private artifact handle was reused")
            resource_handles.add(handle)
            analysis_ids.add(analysis_id)
            digest = cls._digest_map(
                {resource_name: artifact.get("sha256")}, "artifact-hash",
            )[resource_name]
            prior = artifact_hashes.get(resource_name)
            if prior is not None and prior != digest:
                raise MCPToolError("Conflicting private artifact digests were returned")
            artifact_hashes[resource_name] = digest

        public_identity = (
            parsed.get("_verification", {}).get("identity", {})
            if isinstance(parsed.get("_verification"), dict) else {}
        )
        public_analysis_id = (
            public_identity.get("analysis_id")
            if isinstance(public_identity, dict) else None
        )
        if analysis_ids:
            if len(analysis_ids) != 1:
                raise MCPToolError("Private provenance identities do not agree")
            bound_id = next(iter(analysis_ids))
            if public_analysis_id != bound_id:
                raise MCPToolError("Private provenance is not bound to the public result")
            if not result:
                result = {
                    "version": 2,
                    "analysis_id": bound_id,
                    "input_hashes": {},
                    "artifact_hashes": {},
                    "artifact_handles": {},
                }
            result["artifact_hashes"] = artifact_hashes
            handles = result.get("artifact_handles")
            if not isinstance(handles, dict) or set(handles) != resource_handles:
                raise MCPToolError("Private artifact handles do not match returned resources")
        return result

    def _read_stdout(self, proc, generation: int):
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:      # blocking readline loop on the raw stream
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    response_id = msg.get("id") if isinstance(msg, dict) else None
                    if isinstance(response_id, int):
                        with self._waiters_lock:
                            waiter = self._waiters.get(response_id)
                        if waiter is not None:
                            waiter.put(msg)
                except json.JSONDecodeError:
                    continue
        except (ValueError, OSError):
            pass
        finally:
            if generation != self._generation:
                return
            self._closed = True
            with self._waiters_lock:
                waiters = list(self._waiters.values())
            for waiter in waiters:
                waiter.put(_EOF)

    def _drain_stderr(self, proc, generation: int):
        if not proc or not proc.stderr:
            return
        try:
            for line in proc.stderr:
                if generation == self._generation:
                    self._stderr_buf.append(line)
                    if len(self._stderr_buf) > 500:
                        del self._stderr_buf[:250]
        except (ValueError, OSError):
            pass

    def _recent_stderr(self) -> str:
        return "".join(self._stderr_buf[-20:]).strip()

    def _next_id(self) -> int:
        self._id_counter += 1
        return self._id_counter

    def _send(self, msg: dict):
        if not self._proc or not self._proc.stdin or self._closed:
            raise MCPToolError(
                f"MCP server is not running (stderr: {self._recent_stderr() or 'none'})"
            )
        line = json.dumps(msg) + "\n"
        try:
            with self._write_lock:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
        except OSError as e:
            # Server died between calls: surface a clean error with diagnostics
            # instead of a raw BrokenPipeError (F-1).
            raise MCPToolError(
                f"MCP server write failed ({e}); stderr: {self._recent_stderr() or 'none'}"
            ) from e

    def _cancel_request(self, expected_id: int) -> None:
        """Best-effort MCP cancellation after the local request budget expires."""
        try:
            self._notify(
                "notifications/cancelled",
                {"requestId": expected_id, "reason": "client timeout"},
            )
        except Exception:
            # The timeout remains the authoritative error even if the server
            # has already exited or its stdin cannot accept cancellation.
            pass

    def _recv(self, expected_id: int, timeout_s: float = TOOL_TIMEOUT_S) -> dict:
        """Wait only on this request's queue; concurrent calls cannot steal replies."""
        proc = self._proc
        if not proc:
            raise MCPToolError("MCP server is not running")
        with self._waiters_lock:
            waiter = self._waiters.get(expected_id)
        if waiter is None:
            raise MCPToolError(f"No response waiter registered for request {expected_id}")
        deadline = time.monotonic() + timeout_s
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._cancel_request(expected_id)
                    raise TimeoutError(
                        f"MCP call timed out after {timeout_s:g}s "
                        f"(server stderr: {self._recent_stderr() or 'none'})"
                    )
                # The reader thread does the blocking read; we wait on the queue
                # with a bounded timeout, so the deadline is always enforced.
                try:
                    msg = waiter.get(timeout=min(remaining, 1.0))
                except queue.Empty:
                    if self._closed or proc.poll() is not None:
                        raise MCPToolError(
                            f"MCP server exited (code {proc.returncode}); "
                            f"stderr: {self._recent_stderr() or 'none'}"
                        )
                    continue
                if msg is _EOF:
                    raise MCPToolError(
                        f"MCP server closed stdout; stderr: "
                        f"{self._recent_stderr() or 'none'}"
                    )
                return self._result_from_message(msg)
        finally:
            with self._waiters_lock:
                self._waiters.pop(expected_id, None)
            # Notifications have no id and can be ignored safely.

    @staticmethod
    def _result_from_message(msg: dict) -> dict:
        if "error" in msg:
            err = msg["error"]
            raise MCPToolError(
                f"MCP error {err.get('code')}: {err.get('message')}"
            )
        return msg.get("result", {})

    def _call(
        self,
        method: str,
        params: dict,
        timeout_s: float = CONTROL_TIMEOUT_S,
    ) -> dict:
        with self._waiters_lock:
            msg_id = self._next_id()
            self._waiters[msg_id] = queue.Queue(maxsize=1)
        try:
            self._send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
        except Exception:
            with self._waiters_lock:
                self._waiters.pop(msg_id, None)
            raise
        return self._recv(msg_id, timeout_s=timeout_s)

    def _notify(self, method: str, params: dict | None = None):
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
