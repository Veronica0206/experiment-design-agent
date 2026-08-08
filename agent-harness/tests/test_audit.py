#!/usr/bin/env python3
"""Privacy and durability tests for the structured audit log."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from audit import AuditLog  # noqa: E402


with tempfile.TemporaryDirectory() as directory:
    log = AuditLog(Path(directory) / "private", "run")
    log.log("turn_start", metadata={"message": "participant identifier 123"})
    log.log("custom", metadata={"arbitrary": "participant P-777",
                                "verification": {"identity": {},
                                                 "provenance": {"source_path": "/private/ipd.csv"}}})
    log.log("keyed", metadata={"custom": {"participant-P-777": "secret"}})
    log.log_tool_call("sample_size", {"secret": 123}, {"n": 99}, 1.0)
    raw = log.log_file.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in raw.splitlines()]
    file_mode = stat.S_IMODE(log.log_file.stat().st_mode)
    dir_mode = stat.S_IMODE(log.log_dir.stat().st_mode)
    checks = {
        "file_mode_0600": file_mode == 0o600,
        "dir_mode_0700": dir_mode == 0o700,
        "prompt_redacted": "participant identifier" not in raw,
        "params_redacted": '"secret": 123' not in raw,
        "result_redacted": '"n": 99' not in raw,
        "arbitrary_metadata_redacted": "participant P-777" not in raw,
        "dictionary_keys_redacted": "participant-P-777" not in raw,
        "provenance_redacted": "/private/ipd.csv" not in raw,
        "ordered_gate_summary": isinstance(log.summary()["gate_verdicts"], list),
        "rows_written": len(rows) == 4,
    }

    migration_dir = Path(directory) / "migration"
    migration_dir.mkdir(parents=True)
    legacy_a = migration_dir / "legacy-a.audit.jsonl"
    legacy_b = migration_dir / "legacy-b.audit.jsonl"
    legacy_a.write_text("{}\n", encoding="utf-8")
    legacy_b.write_text("{}\n", encoding="utf-8")
    legacy_a.chmod(0o644)
    legacy_b.chmod(0o666)
    external_target = Path(directory) / "external-target.jsonl"
    external_target.write_text("{}\n", encoding="utf-8")
    external_target.chmod(0o644)
    linked_log = migration_dir / "linked.audit.jsonl"
    linked_log.symlink_to(external_target)
    (migration_dir / "directory.audit.jsonl").mkdir()
    old_migration_limit = os.environ.get("EXPDESIGN_AUDIT_MAX_FILES")
    os.environ["EXPDESIGN_AUDIT_MAX_FILES"] = "100"
    try:
        migrated = AuditLog(migration_dir, "current")
        checks["legacy_regular_logs_migrated_to_0600"] = all(
            stat.S_IMODE(path.stat().st_mode) == 0o600
            for path in (legacy_a, legacy_b)
        )
        checks["matching_symlink_target_not_chmodded"] = (
            linked_log.is_symlink()
            and stat.S_IMODE(external_target.stat().st_mode) == 0o644
        )
        checks["matching_nonregular_entry_ignored"] = (
            migration_dir / "directory.audit.jsonl"
        ).is_dir()
        migrated.close()
    finally:
        if old_migration_limit is None:
            os.environ.pop("EXPDESIGN_AUDIT_MAX_FILES", None)
        else:
            os.environ["EXPDESIGN_AUDIT_MAX_FILES"] = old_migration_limit

    old_limit = os.environ.get("EXPDESIGN_AUDIT_MAX_FILES")
    os.environ["EXPDESIGN_AUDIT_MAX_FILES"] = "1"
    try:
        retention_dir = Path(directory) / "retention"
        retention_dir.mkdir(parents=True)
        bogus_lease = retention_dir / ".audit-active-0-forged.json"
        bogus_lease.write_text(
            json.dumps({"pid": 0, "log": "forged.audit.jsonl"}),
            encoding="utf-8",
        )
        forged_current = retention_dir / f".audit-active-{os.getpid()}-forged.json"
        (retention_dir / "forged.audit.jsonl").write_text("{}\n", encoding="utf-8")
        forged_current.write_text(
            json.dumps({"pid": os.getpid(), "log": "forged.audit.jsonl"}),
            encoding="utf-8",
        )
        active_a = AuditLog(retention_dir, "active-a")
        checks["pid_zero_lease_rejected"] = not bogus_lease.exists()
        checks["current_pid_without_nonce_and_expiry_is_rejected"] = not forged_current.exists()
        active_b = AuditLog(retention_dir, "active-b")
        active_a.log("still_live")
        checks["live_log_not_evicted"] = active_a.log_file.exists() and not active_a.degraded
        checks["active_logs_exempt_from_cap"] = active_b.log_file.exists()
        active_b.close()
        checks["closing_other_log_preserves_active"] = active_a.log_file.exists()
        active_a.close()
        final_log = AuditLog(retention_dir, "final")
        final_log.close()
        checks["retention_bounded"] = len(list(retention_dir.glob("*.audit.jsonl"))) <= 1
    finally:
        if old_limit is None:
            os.environ.pop("EXPDESIGN_AUDIT_MAX_FILES", None)
        else:
            os.environ["EXPDESIGN_AUDIT_MAX_FILES"] = old_limit
    log.close()

failed = [name for name, ok in checks.items() if not ok]
for name, ok in checks.items():
    print(f"TEST {name} : {'PASS' if ok else 'FAIL'}")
sys.exit(1 if failed else 0)
