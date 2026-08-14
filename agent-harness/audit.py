"""Structured audit logger for experiment design agent runs.

Every tool call, gate decision, and agent phase transition is logged
as a JSONL line with timestamp, event type, and payload.
"""

from __future__ import annotations

import json
import fcntl
import math
import os
import stat
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


def _close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _open_private_directory(path: Path) -> int:
    """Open the audit directory without following its final path component."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError("this platform cannot safely open the audit directory")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode):
            raise OSError("audit path must be a directory")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise OSError("audit directory has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise OSError("audit directory permissions could not be secured")
        return fd
    except BaseException:
        _close_fd(fd)
        raise


def _open_owned_regular_at(
    directory_fd: int,
    name: str,
    flags: int,
    mode: int = 0o600,
) -> int:
    """Open one private file relative to a pinned directory descriptor."""
    if not name or Path(name).name != name:
        raise ValueError("audit filename must be a non-empty basename")
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("this platform cannot safely open private audit files")
    secure_flags = (
        flags
        | nofollow
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(name, secure_flags, mode, dir_fd=directory_fd)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("private audit file must be regular")
        # Reject pre-existing hard links before changing permissions or writing.
        if metadata.st_nlink != 1:
            raise OSError("private audit file must have exactly one link")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise OSError("private audit file has the wrong owner")
        if stat.S_IMODE(metadata.st_mode) != mode:
            os.fchmod(fd, mode)
        secured = os.fstat(fd)
        if (
            not stat.S_ISREG(secured.st_mode)
            or secured.st_nlink != 1
            or stat.S_IMODE(secured.st_mode) != mode
            or (hasattr(os, "getuid") and secured.st_uid != os.getuid())
        ):
            raise OSError("private audit file changed while it was secured")
        return fd
    except BaseException:
        _close_fd(fd)
        raise


def _check_named_owned_regular_at(
    directory_fd: int,
    name: str,
    fd: int,
    mode: int = 0o600,
) -> None:
    """Require *name* to still designate the secured open descriptor."""
    opened = os.fstat(fd)
    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    rebound = os.fstat(fd)
    current_uid = os.getuid() if hasattr(os, "getuid") else None

    def secure_regular(metadata: os.stat_result) -> bool:
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_nlink == 1
            and stat.S_IMODE(metadata.st_mode) == mode
            and (current_uid is None or metadata.st_uid == current_uid)
        )

    if (
        not secure_regular(opened)
        or not secure_regular(named)
        or not secure_regular(rebound)
        or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        or (opened.st_dev, opened.st_ino) != (rebound.st_dev, rebound.st_ino)
    ):
        raise OSError("private audit file directory entry changed while locked")


@contextmanager
def _retention_lock(directory_fd: int):
    fd = _open_owned_regular_at(
        directory_fd,
        ".audit-retention.lock",
        os.O_RDWR | os.O_CREAT,
    )
    locked = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        # A waiter may have opened the old lock inode before another process
        # unlinked or replaced its directory entry. Rebind the acquired lock to
        # the pinned directory entry before entering the retention transaction.
        _check_named_owned_regular_at(directory_fd, ".audit-retention.lock", fd)
        try:
            yield
        except BaseException:
            raise
        else:
            # A successful transaction must still own the one canonical lock
            # inode. Removal, replacement, or hard-linking fails closed rather
            # than allowing later callers to synchronize on a different inode.
            _check_named_owned_regular_at(directory_fd, ".audit-retention.lock", fd)
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _pid_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _unlink_at(directory_fd: int, name: str) -> None:
    """Best-effort unlink relative to the already validated audit directory."""
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _unlink_same_regular_at(
    directory_fd: int,
    name: str,
    expected_device: int,
    expected_inode: int,
) -> None:
    """Delete a retention candidate only while its directory entry still matches."""
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            stat.S_ISREG(current.st_mode)
            and current.st_dev == expected_device
            and current.st_ino == expected_inode
        ):
            os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        pass


def _sanitize(obj: Any) -> Any:
    """Replace NaN/Inf with None so emitted JSONL stays strictly parseable."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize(v) for v in obj]
    return obj


def _protected_summary(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    summary: dict[str, Any] = {
        "redacted": True,
        "type": type(obj).__name__,
    }
    if isinstance(obj, dict):
        # Dictionary keys are data, not schema: participant IDs and sensitive
        # strata are commonly used as keys.  Never persist them in default logs.
        summary["length"] = len(obj)
    elif isinstance(obj, (list, tuple)):
        summary["length"] = len(obj)
    return summary


def _verification_summary(value: Any) -> Any:
    if isinstance(value, list):
        return [_verification_summary(item) for item in value]
    if not isinstance(value, dict):
        return _protected_summary(value)
    identity = value.get("identity") if isinstance(value.get("identity"), dict) else {}
    return {
        "identity": {
            "analysis_id": identity.get("analysis_id"),
            "call_id": identity.get("call_id"),
            "tool": identity.get("tool"),
        },
        "status": value.get("status"),
        "presentable": value.get("presentable"),
        "checks": _protected_summary(value.get("checks", {})),
        "failure_count": len(value.get("failures") or []),
        "blocked_count": len(value.get("blocked") or []),
        "note_count": len(value.get("notes") or []),
        "provenance": _protected_summary(value.get("provenance", {})),
    }


@dataclass
class AuditEntry:
    timestamp: float
    event: str
    phase: str | None = None
    tool: str | None = None
    params: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    duration_ms: float | None = None
    gate_verdict: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class AuditLog:
    def __init__(self, log_dir: Path, run_id: str, raw: bool | None = None):
        log_name = f"{run_id}.audit.jsonl"
        if not isinstance(run_id, str) or not run_id or Path(log_name).name != log_name:
            raise ValueError("run_id must form a non-empty audit filename basename")
        self.log_dir = log_dir
        self.run_id = run_id
        self.log_file = log_dir / log_name
        self.raw = (os.environ.get("EXPDESIGN_AUDIT_RAW") == "1") if raw is None else raw
        self.degraded = False
        self.write_errors: list[str] = []
        self._entries: list[AuditEntry] = []
        self._write_lock = threading.Lock()
        self._closed = False
        self._lease_token = uuid.uuid4().hex
        self.lease_file = log_dir / (
            f".audit-active-{os.getpid()}-{self._lease_token}.json"
        )
        self._dir_fd = _open_private_directory(self.log_dir)
        self._log_fd: int | None = None
        lease_created = False
        try:
            with _retention_lock(self._dir_fd):
                self._log_fd = _open_owned_regular_at(
                    self._dir_fd,
                    self.log_file.name,
                    os.O_WRONLY | os.O_APPEND | os.O_CREAT,
                )
                lease_fd = _open_owned_regular_at(
                    self._dir_fd,
                    self.lease_file.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                )
                lease_created = True
                with os.fdopen(lease_fd, "w", encoding="utf-8") as handle:
                    json.dump({"pid": os.getpid(), "log": self.log_file.name,
                               "token": self._lease_token, "created_at": time.time()}, handle)
                self._apply_retention_locked()
        except BaseException:
            if lease_created:
                try:
                    os.unlink(self.lease_file.name, dir_fd=self._dir_fd)
                except OSError:
                    pass
            if self._log_fd is not None:
                _close_fd(self._log_fd)
            _close_fd(self._dir_fd)
            raise
        # Register directory first so abnormal-GC finalization runs in the safe
        # reverse order: lease, log descriptor, then directory descriptor.
        self._dir_fd_finalizer = weakref.finalize(self, _close_fd, self._dir_fd)
        self._log_fd_finalizer = weakref.finalize(self, _close_fd, self._log_fd)
        self._lease_finalizer = weakref.finalize(
            self, _unlink_at, self._dir_fd, self.lease_file.name,
        )

    def _apply_retention(self) -> None:
        """Bound local audit retention without touching non-audit files."""
        try:
            with _retention_lock(self._dir_fd):
                self._apply_retention_locked()
        except Exception as exc:
            self.degraded = True
            self.write_errors.append(f"retention: {exc}")

    def _apply_retention_locked(self) -> None:
        """Apply retention while the cross-process lifecycle lock is held."""
        try:
            days = max(1, int(os.environ.get("EXPDESIGN_AUDIT_RETENTION_DAYS", "30")))
            max_files = max(1, int(os.environ.get("EXPDESIGN_AUDIT_MAX_FILES", "100")))
            cutoff = time.time() - days * 86400
            live_logs: set[str] = set()
            names = os.listdir(self._dir_fd)
            lease_names = [
                name for name in names
                if name.startswith(".audit-active-") and name.endswith(".json")
            ]
            for lease_name in lease_names:
                try:
                    lease_fd = _open_owned_regular_at(
                        self._dir_fd, lease_name, os.O_RDONLY,
                    )
                    try:
                        lease_info = os.fstat(lease_fd)
                        if lease_info.st_size > 4096:
                            raise ValueError("audit lease is oversized")
                        payload_bytes = bytearray()
                        while len(payload_bytes) <= 4096:
                            chunk = os.read(lease_fd, 4097 - len(payload_bytes))
                            if not chunk:
                                break
                            payload_bytes.extend(chunk)
                        if len(payload_bytes) > 4096:
                            raise ValueError("audit lease is oversized")
                    finally:
                        os.close(lease_fd)
                    payload = json.loads(payload_bytes.decode("utf-8"))
                    pid = int(payload["pid"])
                    log_name = str(payload["log"])
                    token = str(payload.get("token") or "")
                    created_at = float(payload.get("created_at", float("nan")))
                    max_lease_seconds = max(1, int(os.environ.get(
                        "EXPDESIGN_AUDIT_MAX_LEASE_SECONDS", "86400")))
                    valid_name = (
                        Path(log_name).name == log_name
                        and log_name.endswith(".audit.jsonl")
                        and len(token) >= 16
                        and lease_name == f".audit-active-{pid}-{token}.json"
                        and math.isfinite(created_at)
                        and 0 <= time.time() - created_at <= max_lease_seconds
                    )
                    if pid > 0 and valid_name and _pid_is_live(pid):
                        live_logs.add(log_name)
                    else:
                        _unlink_at(self._dir_fd, lease_name)
                except Exception:
                    _unlink_at(self._dir_fd, lease_name)
            # Migration is intentionally part of the locked lifecycle path:
            # every pre-existing audit log is tightened before retention or a
            # new writer can observe a mixed-permission directory.  Opening
            # with O_NOFOLLOW and checking the descriptor prevents a matching
            # symlink or non-regular file from being chmod'd through.
            files_with_mtime: list[tuple[str, float, int, int]] = []
            open_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            for name in names:
                if not name.endswith(".audit.jsonl"):
                    continue
                try:
                    before = os.stat(
                        name, dir_fd=self._dir_fd, follow_symlinks=False,
                    )
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    fd = _open_owned_regular_at(self._dir_fd, name, open_flags)
                    try:
                        current = os.fstat(fd)
                        if (
                            not stat.S_ISREG(current.st_mode)
                            or current.st_dev != before.st_dev
                            or current.st_ino != before.st_ino
                        ):
                            continue
                        os.fchmod(fd, 0o600)
                        files_with_mtime.append((
                            name, current.st_mtime, current.st_dev, current.st_ino,
                        ))
                    finally:
                        os.close(fd)
                except OSError as exc:
                    self.degraded = True
                    self.write_errors.append(f"secure audit log {name}: {exc}")
            files = sorted(files_with_mtime, key=lambda item: item[1], reverse=True)
            kept = 0
            for name, modified_at, device, inode in files:
                if name in live_logs:
                    kept += 1
                    continue
                if kept >= max_files or modified_at < cutoff:
                    _unlink_same_regular_at(
                        self._dir_fd, name, device, inode,
                    )
                else:
                    kept += 1
        except Exception as exc:
            self.degraded = True
            self.write_errors.append(f"retention: {exc}")

    def close(self) -> None:
        # Serialize the terminal state transition and descriptor close with
        # writers. A writer that prepared a row before shutdown must re-check
        # `_closed` only after it owns this lock; otherwise the numeric descriptor
        # could be reused and receive audit bytes after close().
        with self._write_lock:
            if self._closed:
                return
            self._closed = True
            if getattr(self, "_lease_finalizer", None) is not None:
                self._lease_finalizer()
            if getattr(self, "_log_fd_finalizer", None) is not None:
                self._log_fd_finalizer()
            self._log_fd = None
        try:
            self._apply_retention()
        finally:
            if getattr(self, "_dir_fd_finalizer", None) is not None:
                self._dir_fd_finalizer()

    def _protect_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        if self.raw:
            return dict(metadata or {})
        safe: dict[str, Any] = {}
        for key, value in (metadata or {}).items():
            if key.lower() in {"message", "prompt", "user_message", "params", "result"}:
                safe[key] = _protected_summary(value)
            elif key.lower() in {"envelope", "unresolved", "verification"}:
                safe[key] = _verification_summary(value)
            elif isinstance(value, (bool, int, float)) or value is None:
                safe[key] = value
            else:
                # Unknown metadata is private by default. An allow-list of key
                # names is brittle because callers can put a path, identifier,
                # or participant record under any label.
                safe[key] = _protected_summary(value)
        return safe

    def log(self, event: str, **kwargs: Any) -> AuditEntry:
        if not self.raw:
            if "params" in kwargs:
                kwargs["params"] = _protected_summary(kwargs.get("params"))
            if "result" in kwargs:
                kwargs["result"] = _protected_summary(kwargs.get("result"))
            kwargs["metadata"] = self._protect_metadata(kwargs.get("metadata"))
            if kwargs.get("error") is not None:
                # Deterministic unsalted hashes of prompts, parameters, results,
                # or diagnostics remain dictionary-testable when values have a
                # small domain.  Default audit mode records only shape/count
                # metadata; exact commitments remain available in explicit raw
                # mode and in the user-visible verification envelope.
                kwargs["error"] = "[redacted]"
        entry = AuditEntry(timestamp=time.time(), event=event, **kwargs)
        self._entries.append(entry)
        # Logging must never crash the run it observes (B-9): swallow any I/O
        # or serialization failure, and never emit bare NaN/Infinity tokens.
        try:
            line = json.dumps(_sanitize(asdict(entry)), default=str, allow_nan=False)
            payload = (line + "\n").encode("utf-8")
            with self._write_lock:
                if self._closed or self._log_fd is None:
                    raise OSError("audit log is closed")
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(self._log_fd, remaining)
                    if written <= 0:
                        raise OSError("audit log write made no progress")
                    remaining = remaining[written:]
        except Exception as exc:
            self.degraded = True
            self.write_errors.append(str(exc))
        return entry

    def log_tool_call(
        self, tool: str, params: dict, result: dict | None, duration_ms: float,
        phase: str | None = None, error: str | None = None,
    ) -> AuditEntry:
        return self.log(
            "tool_call", tool=tool, params=params, result=result,
            duration_ms=duration_ms, phase=phase, error=error,
        )

    def log_phase(self, phase: str, **kwargs: Any) -> AuditEntry:
        return self.log("phase_start", phase=phase, **kwargs)

    def log_gate(self, phase: str, verdict: str, **kwargs: Any) -> AuditEntry:
        return self.log("gate_decision", phase=phase, gate_verdict=verdict, **kwargs)

    def log_error(self, error: str, **kwargs: Any) -> AuditEntry:
        return self.log("error", error=error, **kwargs)

    @property
    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def summary(self) -> dict[str, Any]:
        tool_calls = [e for e in self._entries if e.event == "tool_call"]
        gates = [e for e in self._entries if e.event == "gate_decision"]
        errors = [e for e in self._entries if e.error]
        return {
            "run_id": self.run_id,
            "total_events": len(self._entries),
            "tool_calls": len(tool_calls),
            "gates": len(gates),
            "errors": len(errors),
            "gate_verdicts": [g.gate_verdict for g in gates],
            "audit_degraded": self.degraded,
            "write_errors": list(self.write_errors),
            "raw_logging": self.raw,
            "total_duration_ms": sum(
                e.duration_ms for e in tool_calls if e.duration_ms
            ),
        }
