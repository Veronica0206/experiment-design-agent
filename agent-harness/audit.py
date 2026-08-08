"""Structured audit logger for experiment design agent runs.

Every tool call, gate decision, and agent phase transition is logged
as a JSONL line with timestamp, event type, and payload.
"""

from __future__ import annotations

import json
import hashlib
import fcntl
import math
import os
import stat
import time
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@contextmanager
def _retention_lock(log_dir: Path):
    lock_path = log_dir / ".audit-retention.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
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


def _remove_lease(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
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


def _fingerprint(obj: Any) -> str:
    raw = json.dumps(_sanitize(obj), sort_keys=True, default=str, allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _protected_summary(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    summary: dict[str, Any] = {
        "redacted": True,
        "sha256": _fingerprint(obj),
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
            "args_hash": identity.get("args_hash"),
            "provenance_hash": identity.get("provenance_hash"),
        },
        "status": value.get("status"),
        "presentable": value.get("presentable"),
        "public_result_hash": value.get("public_result_hash"),
        "report_hash": value.get("report_hash"),
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
        self.log_dir = log_dir
        self.run_id = run_id
        self.log_file = log_dir / f"{run_id}.audit.jsonl"
        self.raw = (os.environ.get("EXPDESIGN_AUDIT_RAW") == "1") if raw is None else raw
        self.degraded = False
        self.write_errors: list[str] = []
        self.log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.log_dir, 0o700)
        self._entries: list[AuditEntry] = []
        self._lease_token = uuid.uuid4().hex
        self.lease_file = log_dir / (
            f".audit-active-{os.getpid()}-{self._lease_token}.json"
        )
        with _retention_lock(self.log_dir):
            fd = os.open(self.log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            os.close(fd)
            os.chmod(self.log_file, 0o600)
            lease_fd = os.open(self.lease_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(lease_fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "log": self.log_file.name,
                           "token": self._lease_token, "created_at": time.time()}, handle)
            self._apply_retention_locked()
        self._lease_finalizer = weakref.finalize(self, _remove_lease, self.lease_file)

    def _apply_retention(self) -> None:
        """Bound local audit retention without touching non-audit files."""
        try:
            with _retention_lock(self.log_dir):
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
            for lease in self.log_dir.glob(".audit-active-*.json"):
                try:
                    payload = json.loads(lease.read_text(encoding="utf-8"))
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
                        and lease.name == f".audit-active-{pid}-{token}.json"
                        and math.isfinite(created_at)
                        and 0 <= time.time() - created_at <= max_lease_seconds
                    )
                    if pid > 0 and valid_name and _pid_is_live(pid):
                        live_logs.add(log_name)
                    else:
                        lease.unlink(missing_ok=True)
                except Exception:
                    lease.unlink(missing_ok=True)
            # Migration is intentionally part of the locked lifecycle path:
            # every pre-existing audit log is tightened before retention or a
            # new writer can observe a mixed-permission directory.  Opening
            # with O_NOFOLLOW and checking the descriptor prevents a matching
            # symlink or non-regular file from being chmod'd through.
            files_with_mtime: list[tuple[Path, float]] = []
            open_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
            nofollow = getattr(os, "O_NOFOLLOW", 0)
            for path in self.log_dir.glob("*.audit.jsonl"):
                try:
                    before = path.lstat()
                    if not stat.S_ISREG(before.st_mode):
                        continue
                    fd = os.open(path, open_flags | nofollow)
                    try:
                        current = os.fstat(fd)
                        if (
                            not stat.S_ISREG(current.st_mode)
                            or current.st_dev != before.st_dev
                            or current.st_ino != before.st_ino
                        ):
                            continue
                        os.fchmod(fd, 0o600)
                        files_with_mtime.append((path, current.st_mtime))
                    finally:
                        os.close(fd)
                except OSError as exc:
                    self.degraded = True
                    self.write_errors.append(f"secure audit log {path.name}: {exc}")
            files = sorted(files_with_mtime, key=lambda item: item[1], reverse=True)
            kept = 0
            for path, modified_at in files:
                if path.name in live_logs:
                    kept += 1
                    continue
                if kept >= max_files or modified_at < cutoff:
                    path.unlink(missing_ok=True)
                else:
                    kept += 1
        except Exception as exc:
            self.degraded = True
            self.write_errors.append(f"retention: {exc}")

    def close(self) -> None:
        if getattr(self, "_lease_finalizer", None) is not None:
            self._lease_finalizer()
        self._apply_retention()

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
                kwargs["error"] = f"[redacted:{_fingerprint(kwargs['error'])[:16]}]"
        entry = AuditEntry(timestamp=time.time(), event=event, **kwargs)
        self._entries.append(entry)
        # Logging must never crash the run it observes (B-9): swallow any I/O
        # or serialization failure, and never emit bare NaN/Infinity tokens.
        try:
            line = json.dumps(_sanitize(asdict(entry)), default=str, allow_nan=False)
            fd = os.open(self.log_file, os.O_WRONLY | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
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
