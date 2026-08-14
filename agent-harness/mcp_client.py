"""MCP client that spawns the experiment-design MCP server and calls tools.

Manages the node subprocess lifecycle and JSON-RPC communication over stdio.
"""

from __future__ import annotations

import atexit
import hmac
import json
import os
import queue
import re
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


MCP_SERVER_DIR = Path(__file__).resolve().parent.parent / "mcp-server"
SERVER_ENTRY = MCP_SERVER_DIR / "dist" / "index.js"
SERVER_LAUNCHER = MCP_SERVER_DIR / "launch-server.sh"

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
_RUNTIME_ENTRY = re.compile(
    r"^runtime-([1-9][0-9]*)-([0-9a-f]{32})\.json$",
)
_RUNTIME_REGISTRY_MAX_BYTES = 4096
_RUNTIME_REGISTRY_WAIT_S = 5
_RUNTIME_STOP_PAYLOAD = b"stop\n"


class MCPToolError(RuntimeError):
    """Raised when a tool call fails (server-side error or bad output)."""


class _RuntimeGroupRegistry:
    """Private, authenticated leases for runtime groups outside Node's group.

    R, verifier, and fingerprint supervisors create one lease before they spawn
    a detached runtime group. The Python host retains a descriptor for the
    private registry and can therefore stop those groups even if Node is frozen
    and cannot run its cooperative shutdown handlers.
    """

    def __init__(self) -> None:
        if os.name != "posix" or not (
            sys.platform == "darwin" or sys.platform.startswith("linux")
        ):
            raise RuntimeError(
                "secure MCP runtime-group supervision is unavailable on this platform"
            )
        if not all(hasattr(os, name) for name in ("O_NOFOLLOW", "O_DIRECTORY")):
            raise RuntimeError("secure MCP runtime registry primitives are unavailable")
        self.path = Path(tempfile.mkdtemp(prefix="expdesign-runtime-groups-"))
        os.chmod(self.path, 0o700)
        self.fd = os.open(
            self.path,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
        )
        self.token = secrets.token_hex(32)
        self.server_pid: int | None = None
        self._closed = False
        self._check_directory()

    def _check_directory(self) -> os.stat_result:
        metadata = os.fstat(self.fd)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise OSError("MCP runtime registry is not a private owned directory")
        return metadata

    def environment(self) -> dict[str, str]:
        directory = self._check_directory()
        return {
            "EXPDESIGN_RUNTIME_REGISTRY_DIR": str(self.path),
            "EXPDESIGN_RUNTIME_REGISTRY_TOKEN": self.token,
            "EXPDESIGN_RUNTIME_REGISTRY_DEV": str(directory.st_dev),
            "EXPDESIGN_RUNTIME_REGISTRY_INO": str(directory.st_ino),
        }

    def bind_server(self, pid: Any) -> None:
        if not isinstance(pid, int) or pid <= 0:
            raise OSError("MCP server did not publish a valid process identity")
        self.server_pid = pid

    def stop_accepting(self) -> None:
        """Atomically publish a private stop latch before signalling Node."""
        if self._closed:
            return
        self._check_directory()
        flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            fd = os.open(".stop", flags, 0o600, dir_fd=self.fd)
        except FileExistsError:
            fd = os.open(
                ".stop", os.O_RDONLY | os.O_NOFOLLOW
                | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self.fd,
            )
            try:
                self._validate_stop_file(fd)
            finally:
                os.close(fd)
            return
        try:
            self._write_all(fd, _RUNTIME_STOP_PAYLOAD)
            os.fsync(fd)
            self._check_private_regular(fd, "runtime stop latch")
            self._check_named_fd(".stop", fd, "runtime stop latch")
            os.fsync(self.fd)
        finally:
            os.close(fd)

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("short MCP runtime registry write")
            remaining = remaining[written:]

    @staticmethod
    def _check_private_regular(fd: int, label: str) -> os.stat_result:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (hasattr(os, "getuid") and metadata.st_uid != os.getuid())
        ):
            raise OSError(f"{label} is not a private singly-linked owned file")
        return metadata

    def _check_named_fd(self, name: str, fd: int, label: str) -> None:
        named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise OSError(f"{label} directory entry changed")

    def _validate_stop_file(self, fd: int) -> os.stat_result:
        before = self._check_private_regular(fd, "runtime stop latch")
        self._check_named_fd(".stop", fd, "runtime stop latch")
        if before.st_size != len(_RUNTIME_STOP_PAYLOAD):
            raise OSError("runtime stop latch is malformed")
        payload = os.read(fd, len(_RUNTIME_STOP_PAYLOAD) + 1)
        after = self._check_private_regular(fd, "runtime stop latch")
        self._check_named_fd(".stop", fd, "runtime stop latch")
        if (
            payload != _RUNTIME_STOP_PAYLOAD
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            raise OSError("runtime stop latch changed or is malformed")
        return after

    def _read_entry(self, name: str) -> tuple[dict[str, Any], os.stat_result]:
        match = _RUNTIME_ENTRY.fullmatch(name)
        if match is None:
            raise OSError("runtime registry contains an unknown entry")
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=self.fd,
        )
        try:
            before = self._check_private_regular(fd, "runtime group lease")
            self._check_named_fd(name, fd, "runtime group lease")
            if before.st_size > _RUNTIME_REGISTRY_MAX_BYTES:
                raise OSError("runtime group lease is oversized")
            payload = bytearray()
            while len(payload) <= _RUNTIME_REGISTRY_MAX_BYTES:
                chunk = os.read(fd, _RUNTIME_REGISTRY_MAX_BYTES + 1 - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > _RUNTIME_REGISTRY_MAX_BYTES:
                raise OSError("runtime group lease is oversized")
            after = self._check_private_regular(fd, "runtime group lease")
            self._check_named_fd(name, fd, "runtime group lease")
            if (
                (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise OSError("runtime group lease changed while it was read")
            parsed = json.loads(payload.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise OSError("runtime group lease is malformed")
            return parsed, after
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OSError("runtime group lease is malformed") from exc
        finally:
            os.close(fd)

    @staticmethod
    def _process_identity(pid: int) -> tuple[str, int, int] | None:
        """Return (start identity, process group, uid), or None after exit."""
        if sys.platform.startswith("linux"):
            try:
                raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
                suffix = raw[raw.rfind(")") + 2:].split()
                group = int(suffix[2])       # proc(5) field 5
                start = suffix[19]           # proc(5) field 22
                status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
                uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
                uid = int(uid_line.split()[1])
                return f"linux:{start}", group, uid
            except (FileNotFoundError, ProcessLookupError):
                return None
            except (IndexError, StopIteration, ValueError) as exc:
                raise OSError("runtime supervisor identity is unreadable") from exc
        if sys.platform == "darwin":
            clean_env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
            try:
                started = subprocess.run(
                    ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=2, env=clean_env, check=False,
                )
                details = subprocess.run(
                    ["/bin/ps", "-o", "pgid=", "-o", "uid=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=2, env=clean_env, check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise OSError("runtime supervisor identity probe timed out") from exc
            start_text = " ".join(started.stdout.split())
            fields = details.stdout.split()
            if started.returncode != 0 or details.returncode != 0 or not start_text:
                return None
            if len(fields) != 2:
                raise OSError("runtime supervisor identity is unreadable")
            try:
                return f"darwin:{start_text}", int(fields[0]), int(fields[1])
            except ValueError as exc:
                raise OSError("runtime supervisor identity is unreadable") from exc
        raise OSError("runtime supervisor identity is unsupported")

    def _entry_authentication(self, parsed: dict[str, Any]) -> str:
        payload = json.dumps(
            [
                parsed["version"], parsed["server_pid"], parsed["pid"],
                parsed["nonce"], parsed["start_identity"],
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hmac.new(self.token.encode("ascii"), payload, "sha256").hexdigest()

    def _authenticate_entry(
        self, name: str, parsed: dict[str, Any], metadata: os.stat_result,
    ) -> tuple[int, str, os.stat_result]:
        match = _RUNTIME_ENTRY.fullmatch(name)
        assert match is not None
        expected_keys = {
            "version", "server_pid", "pid", "nonce", "start_identity", "auth",
        }
        if set(parsed) != expected_keys:
            raise OSError("runtime group lease has unexpected fields")
        pid = parsed.get("pid")
        nonce = parsed.get("nonce")
        start_identity = parsed.get("start_identity")
        auth = parsed.get("auth")
        if (
            type(parsed.get("version")) is not int or parsed["version"] != 1
            or type(parsed.get("server_pid")) is not int
            or parsed["server_pid"] != self.server_pid
            or type(pid) is not int or pid <= 0 or str(pid) != match.group(1)
            or not isinstance(nonce, str) or nonce != match.group(2)
            or not isinstance(start_identity, str) or not start_identity
            or len(start_identity) > 256
            or not isinstance(auth, str) or not _SHA256.fullmatch(auth)
            or not hmac.compare_digest(auth, self._entry_authentication(parsed))
        ):
            raise OSError("runtime group lease failed authentication")
        return pid, start_identity, metadata

    def _validated_entry(
        self, name: str,
    ) -> tuple[int, str, os.stat_result] | None:
        parsed, metadata = self._read_entry(name)
        pid, expected_identity, metadata = self._authenticate_entry(
            name, parsed, metadata,
        )
        identity = self._process_identity(pid)
        if identity is None:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise OSError(
                    "runtime supervisor exited but its group cannot be inspected"
                ) from exc
            else:
                # Without the leader's start identity, killing a still-live
                # numeric PGID could target a reused group. Retain the lease and
                # fail closed instead of making an unauthenticated kill.
                raise OSError(
                    "runtime supervisor exited while its process group remains live"
                )
            self._unlink_same(name, metadata)
            return None
        start_identity, group, uid = identity
        if (
            start_identity != expected_identity
            or group != pid
            or (hasattr(os, "getuid") and uid != os.getuid())
        ):
            raise OSError("runtime group lease does not identify its live supervisor")
        return pid, start_identity, metadata

    def _unlink_same(self, name: str, expected: os.stat_result) -> None:
        current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if (
            stat.S_ISREG(current.st_mode)
            and (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino)
        ):
            os.unlink(name, dir_fd=self.fd)

    def _runtime_names(self) -> list[str]:
        self._check_directory()
        names = sorted(os.listdir(self.fd))
        unknown = [name for name in names if name != ".stop" and not _RUNTIME_ENTRY.fullmatch(name)]
        if unknown:
            raise OSError("runtime registry contains unrecognized state")
        return [name for name in names if _RUNTIME_ENTRY.fullmatch(name)]

    def terminate_registered_groups(self) -> None:
        """Stop, revalidate, then kill each authenticated runtime process group."""
        if self._closed:
            return
        for name in self._runtime_names():
            entry = self._validated_entry(name)
            if entry is None:
                continue
            pid, start_identity, _metadata = entry
            try:
                os.killpg(pid, signal.SIGSTOP)
            except ProcessLookupError:
                continue
            stopped_identity = self._process_identity(pid)
            if stopped_identity is None:
                try:
                    os.killpg(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                raise OSError("runtime supervisor exited before kill revalidation")
            if stopped_identity[:2] != (start_identity, pid):
                # Never kill a process group after identity drift. Resume any
                # group stopped by the conservative guard and fail closed.
                try:
                    os.killpg(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                raise OSError("runtime supervisor identity changed before termination")
            try:
                os.killpg(pid, signal.SIGKILL)
            except BaseException:
                try:
                    os.killpg(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                raise

    def _entry_identity_is_live(self, name: str) -> bool:
        entry = self._validated_entry(name)
        return entry is not None

    def finish(self) -> None:
        """Drain authenticated leases, remove private state, and close the fd."""
        if self._closed:
            return
        deadline = time.monotonic() + _RUNTIME_REGISTRY_WAIT_S
        while True:
            self.terminate_registered_groups()
            live = []
            for name in self._runtime_names():
                if self._entry_identity_is_live(name):
                    live.append(name)
            if not live:
                break
            if time.monotonic() >= deadline:
                raise OSError("runtime process groups did not terminate")
            time.sleep(0.025)

        for name in self._runtime_names():
            parsed, metadata = self._read_entry(name)
            # A now-dead entry must still authenticate before host cleanup.
            self._authenticate_entry(name, parsed, metadata)
            self._unlink_same(name, metadata)
        try:
            stop_metadata = os.stat(".stop", dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            stop_metadata = None
        if stop_metadata is not None:
            stop_fd = os.open(
                ".stop", os.O_RDONLY | os.O_NOFOLLOW
                | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=self.fd,
            )
            try:
                stop_metadata = self._validate_stop_file(stop_fd)
            finally:
                os.close(stop_fd)
            self._unlink_same(".stop", stop_metadata)
        if os.listdir(self.fd):
            raise OSError("runtime registry did not drain completely")
        directory = self._check_directory()
        named = os.stat(self.path, follow_symlinks=False)
        if (named.st_dev, named.st_ino) != (directory.st_dev, directory.st_ino):
            raise OSError("runtime registry path changed before cleanup")
        try:
            os.rmdir(self.path)
        except BaseException:
            # Keep the pinned descriptor and registry object available so a
            # later stop() can retry cleanup or an operator can inspect state.
            raise
        os.close(self.fd)
        self._closed = True


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
        self._runtime_registry: _RuntimeGroupRegistry | None = None

    def start(self) -> dict:
        if not SERVER_ENTRY.is_file() or not SERVER_LAUNCHER.is_file():
            raise FileNotFoundError(
                f"MCP server or launcher is unavailable. Run 'npm run build' in {MCP_SERVER_DIR}"
            )
        # Fully retire a prior generation before publishing new shared state.
        if self._proc is not None or self._runtime_registry is not None or any(
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
        try:
            registry = _RuntimeGroupRegistry()
            self._runtime_registry = registry
            server_env = dict(os.environ)
            # Absolute interpreter selection is handled by the audited launcher.
            # Do not let Node preload code or search caller-controlled module roots.
            server_env.pop("NODE_OPTIONS", None)
            server_env.pop("NODE_PATH", None)
            for name in (
                "EXPDESIGN_RUNTIME_REGISTRY_DIR",
                "EXPDESIGN_RUNTIME_REGISTRY_TOKEN",
                "EXPDESIGN_RUNTIME_REGISTRY_DEV",
                "EXPDESIGN_RUNTIME_REGISTRY_INO",
                "EXPDESIGN_RUNTIME_SERVER_PID",
                "EXPDESIGN_RUNTIME_NONCE",
                "EXPDESIGN_RUNTIME_TARGET_CWD",
            ):
                server_env.pop(name, None)
            server_env.update(registry.environment())
            self._proc = subprocess.Popen(
                ["/bin/sh", str(SERVER_LAUNCHER)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(MCP_SERVER_DIR),
                env=server_env,
                start_new_session=True,
            )
            proc = self._proc
            registry.bind_server(proc.pid)
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
        except BaseException as startup_error:
            # Popen succeeds before initialize/notifications can fail. Retire
            # that partial generation here so direct MCPClient users cannot
            # orphan Node or its reader threads when start() itself raises.
            try:
                self.stop()
            except Exception:
                raise RuntimeError(
                    "MCP server startup failed and cleanup did not complete"
                ) from startup_error
            raise

    def stop(self):
        """Stop Node and every registered runtime group, retaining failed state."""
        proc = self._proc
        registry = self._runtime_registry
        cleanup_failed = False
        if registry is not None:
            try:
                # Prevent new wrappers before asking Node to shut down. Every
                # wrapper checks the latch before registration and target spawn.
                registry.stop_accepting()
            except Exception:
                cleanup_failed = True
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                cleanup_failed = True
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                pass
            except Exception:
                cleanup_failed = True
            if proc.poll() is None:
                if registry is not None:
                    try:
                        registry.terminate_registered_groups()
                    except Exception:
                        cleanup_failed = True
                try:
                    if (
                        os.name == "posix" and isinstance(proc.pid, int)
                        and proc.pid > 0
                    ):
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                except Exception:
                    cleanup_failed = True
                try:
                    proc.wait(timeout=STOP_GRACE_S)
                except Exception:
                    cleanup_failed = True
        # Even a cooperative Node exit must not leave a wrapper group behind.
        # This second pass catches groups whose lease appeared during shutdown.
        if registry is not None:
            try:
                registry.terminate_registered_groups()
            except Exception:
                cleanup_failed = True
        current = threading.current_thread()
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None and thread is not current:
                thread.join(timeout=STOP_GRACE_S)
        process_live = proc is not None and proc.poll() is None
        stdout_live = self._stdout_thread is not None and self._stdout_thread.is_alive()
        stderr_live = self._stderr_thread is not None and self._stderr_thread.is_alive()
        if not process_live:
            self._proc = None
        if not stdout_live:
            self._stdout_thread = None
        if not stderr_live:
            self._stderr_thread = None
        if not process_live and registry is not None:
            try:
                registry.finish()
            except Exception:
                cleanup_failed = True
            else:
                self._runtime_registry = None
        self._closed = True
        registry_live = self._runtime_registry is not None
        if cleanup_failed or process_live or stdout_live or stderr_live or registry_live:
            raise RuntimeError("MCP server process could not be fully stopped")

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
