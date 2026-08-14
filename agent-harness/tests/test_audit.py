#!/usr/bin/env python3
"""Privacy and durability tests for the structured audit log."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

import audit  # noqa: E402


AuditLog = audit.AuditLog


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
        "redaction_has_no_dictionary_testable_digest": "sha256" not in raw,
        "ordered_gate_summary": isinstance(log.summary()["gate_verdicts"], list),
        "rows_written": len(rows) == 4,
    }

    # A pre-positioned symlink at the deterministic audit filename must be
    # rejected before either its content or permissions can be changed.
    link_attack_dir = Path(directory) / "link-attack"
    link_attack_dir.mkdir(mode=0o700)
    link_target = Path(directory) / "log-link-target.txt"
    link_target.write_text("UNCHANGED-LOG-TARGET\n", encoding="utf-8")
    link_target.chmod(0o644)
    (link_attack_dir / "known.audit.jsonl").symlink_to(link_target)
    link_target_before = link_target.read_bytes()
    link_mode_before = stat.S_IMODE(link_target.stat().st_mode)
    try:
        AuditLog(link_attack_dir, "known")
        link_rejected = False
    except OSError:
        link_rejected = True
    checks["audit_filename_symlink_rejected"] = link_rejected
    checks["audit_filename_symlink_target_unchanged"] = (
        link_target.read_bytes() == link_target_before
        and stat.S_IMODE(link_target.stat().st_mode) == link_mode_before
    )

    # The predictable retention lock is protected by the same descriptor checks.
    lock_attack_dir = Path(directory) / "lock-attack"
    lock_attack_dir.mkdir(mode=0o700)
    lock_target = Path(directory) / "lock-link-target.txt"
    lock_target.write_text("UNCHANGED-LOCK-TARGET\n", encoding="utf-8")
    lock_target.chmod(0o644)
    (lock_attack_dir / ".audit-retention.lock").symlink_to(lock_target)
    lock_target_before = lock_target.read_bytes()
    lock_mode_before = stat.S_IMODE(lock_target.stat().st_mode)
    try:
        AuditLog(lock_attack_dir, "run")
        lock_link_rejected = False
    except OSError:
        lock_link_rejected = True
    checks["retention_lock_symlink_rejected"] = lock_link_rejected
    checks["retention_lock_symlink_target_unchanged"] = (
        lock_target.read_bytes() == lock_target_before
        and stat.S_IMODE(lock_target.stat().st_mode) == lock_mode_before
    )

    # A waiter that opened the old lock before its directory entry was replaced
    # must not enter alongside a holder of the replacement inode. Force that
    # exact three-party interleaving and require the stale waiter to fail closed.
    lock_race_dir = Path(directory) / "lock-replacement-race"
    lock_race_fd = audit._open_private_directory(lock_race_dir)
    original_lock_open = audit._open_owned_regular_at
    stale_waiter_opened = threading.Event()
    stale_waiter_entered = threading.Event()
    stale_waiter_rejected = threading.Event()
    replacement_entered = threading.Event()
    release_replacement = threading.Event()
    lock_race_errors: list[str] = []

    def observe_lock_open(directory_fd, name, flags, mode=0o600):
        opened_fd = original_lock_open(directory_fd, name, flags, mode)
        if (threading.current_thread().name == "stale-audit-lock-waiter"
                and name == ".audit-retention.lock"):
            stale_waiter_opened.set()
        return opened_fd

    def stale_lock_waiter():
        try:
            with audit._retention_lock(lock_race_fd):
                stale_waiter_entered.set()
        except OSError:
            stale_waiter_rejected.set()
        except BaseException as exc:
            lock_race_errors.append(f"stale waiter: {exc!r}")

    def replacement_lock_holder():
        try:
            with audit._retention_lock(lock_race_fd):
                replacement_entered.set()
                release_replacement.wait(timeout=5)
        except BaseException as exc:
            lock_race_errors.append(f"replacement holder: {exc!r}")

    stale_thread = threading.Thread(
        target=stale_lock_waiter, name="stale-audit-lock-waiter",
    )
    replacement_thread = threading.Thread(
        target=replacement_lock_holder, name="replacement-audit-lock-holder",
    )
    replacement_was_concurrent = False
    original_holder_rejected = False
    try:
        audit._open_owned_regular_at = observe_lock_open
        try:
            with audit._retention_lock(lock_race_fd):
                stale_thread.start()
                if not stale_waiter_opened.wait(timeout=5):
                    lock_race_errors.append("stale waiter did not open the original lock")
                else:
                    os.unlink(".audit-retention.lock", dir_fd=lock_race_fd)
                    replacement_thread.start()
                    replacement_was_concurrent = replacement_entered.wait(timeout=5)
        except OSError:
            original_holder_rejected = True
        stale_thread.join(timeout=5)
    finally:
        release_replacement.set()
        stale_thread.join(timeout=5)
        if replacement_thread.ident is not None:
            replacement_thread.join(timeout=5)
        audit._open_owned_regular_at = original_lock_open
        os.close(lock_race_fd)
    checks["retention_lock_replacement_rejects_stale_waiter"] = (
        replacement_was_concurrent
        and original_holder_rejected
        and stale_waiter_rejected.is_set()
        and not stale_waiter_entered.is_set()
        and not stale_thread.is_alive()
        and not replacement_thread.is_alive()
        and not lock_race_errors
        and sorted(path.name for path in lock_race_dir.iterdir())
        == [".audit-retention.lock"]
    )

    # Adding a second name to the acquired lock inode invalidates the successful
    # transaction at exit; clean the injected link and leave only the lock file.
    hardlink_lock_dir = Path(directory) / "lock-hardlink-race"
    hardlink_lock_fd = audit._open_private_directory(hardlink_lock_dir)
    injected_lock_link = Path(directory) / "injected-retention-lock-link"
    hardlink_rejected = False
    try:
        try:
            with audit._retention_lock(hardlink_lock_fd):
                os.link(
                    ".audit-retention.lock", injected_lock_link,
                    src_dir_fd=hardlink_lock_fd,
                )
        except OSError:
            hardlink_rejected = True
    finally:
        if injected_lock_link.exists():
            injected_lock_link.unlink()
        os.close(hardlink_lock_fd)
    checks["retention_lock_post_acquire_hardlink_fails_closed"] = (
        hardlink_rejected
        and not injected_lock_link.exists()
        and sorted(path.name for path in hardlink_lock_dir.iterdir())
        == [".audit-retention.lock"]
        and (hardlink_lock_dir / ".audit-retention.lock").stat().st_nlink == 1
    )

    # All post-open lifecycle operations must remain anchored to the validated
    # directory descriptor. Replacing the original path with a symlink cannot
    # redirect retention, chmod, or lease cleanup into another directory.
    swap_path = Path(directory) / "directory-swap"
    swapped_log = AuditLog(swap_path, "stable")
    anchored_path = Path(directory) / "directory-swap-anchored"
    victim_dir = Path(directory) / "directory-swap-victim"
    victim_dir.mkdir(mode=0o700)
    victim_a = victim_dir / "a.audit.jsonl"
    victim_b = victim_dir / "b.audit.jsonl"
    victim_a.write_text("VICTIM-A\n", encoding="utf-8")
    victim_b.write_text("VICTIM-B\n", encoding="utf-8")
    victim_a.chmod(0o644)
    victim_b.chmod(0o644)
    victim_snapshot = {
        path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in (victim_a, victim_b)
    }
    swap_path.rename(anchored_path)
    swap_path.symlink_to(victim_dir, target_is_directory=True)
    swapped_log.close()
    checks["retention_stays_on_pinned_directory_after_path_swap"] = all(
        path.exists()
        and (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        == victim_snapshot[path.name]
        for path in (victim_a, victim_b)
    )
    checks["lease_cleanup_stays_on_pinned_directory_after_path_swap"] = not list(
        anchored_path.glob(".audit-active-*.json")
    )

    # Replacing the pathname after initialization cannot redirect later writes:
    # all appends stay on the already-validated descriptor.
    replacement_dir = Path(directory) / "replacement-attack"
    replacement_log = AuditLog(replacement_dir, "stable")
    replacement_target = Path(directory) / "replacement-target.txt"
    replacement_target.write_text("UNCHANGED-REPLACEMENT-TARGET\n", encoding="utf-8")
    replacement_target.chmod(0o644)
    replacement_before = replacement_target.read_bytes()
    replacement_mode_before = stat.S_IMODE(replacement_target.stat().st_mode)
    replacement_log.log_file.unlink()
    replacement_log.log_file.symlink_to(replacement_target)
    replacement_log.log("must_not_escape")
    replacement_log.close()
    checks["post_open_symlink_target_unchanged"] = (
        replacement_target.read_bytes() == replacement_before
        and stat.S_IMODE(replacement_target.stat().st_mode) == replacement_mode_before
    )

    # Deterministically pause a writer immediately before lock acquisition,
    # close the log, and reuse its old descriptor number. The paused writer must
    # observe the terminal state under the same lock and never write to the new
    # unrelated descriptor.
    close_race_dir = Path(directory) / "close-race"
    close_race_log = AuditLog(close_race_dir, "race")
    stale_fd = close_race_log._log_fd
    writer_waiting = threading.Event()
    release_writer = threading.Event()

    class OrderedWriteLock:
        def __init__(self):
            self._lock = threading.Lock()

        def __enter__(self):
            if threading.current_thread().name == "paused-audit-writer":
                writer_waiting.set()
                release_writer.wait(timeout=5)
            self._lock.acquire()
            return self

        def __exit__(self, *_exc):
            self._lock.release()

    close_race_log._write_lock = OrderedWriteLock()
    race_writer = threading.Thread(
        target=lambda: close_race_log.log("must_not_escape"),
        name="paused-audit-writer",
    )
    race_writer.start()
    writer_was_paused = writer_waiting.wait(timeout=5)
    close_race_log.close()
    reused_path = None
    reused_descriptors: list[int] = []
    for index in range(32):
        candidate = Path(directory) / f"unrelated-{index}.txt"
        descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        reused_descriptors.append(descriptor)
        if descriptor == stale_fd:
            reused_path = candidate
            break
    release_writer.set()
    race_writer.join(timeout=5)
    for descriptor in reused_descriptors:
        os.close(descriptor)
    reused_payload = reused_path.read_text(encoding="utf-8") if reused_path else ""
    checks["close_serializes_with_pending_audit_write"] = (
        writer_was_paused
        and reused_path is not None
        and not race_writer.is_alive()
        and "must_not_escape" not in reused_payload
        and close_race_log.degraded
    )

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
