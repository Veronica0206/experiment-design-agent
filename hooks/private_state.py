"""Descriptor-safe storage primitives for private hook state.

Locked transactions pin one verified private directory descriptor and perform
every lock, read, write, replace, and unlink relative to that descriptor.  A
rename or symlink swap of the configured pathname therefore cannot redirect an
in-flight transaction.  Private regular files must be owned by this user, have
mode 0600, and have exactly one link before and after any write.
"""

from __future__ import annotations

import fcntl
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


def close_fd(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def check_private_directory(fd: int, label: str) -> None:
    """Require an owned real directory with private permissions."""
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"{label} must be a real directory")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise OSError(f"{label} has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise OSError(f"{label} must have mode 0700")


def open_private_directory(path: Path, label: str) -> int:
    """Create/open one owned mode-0700 directory without following its leaf."""
    try:
        path.mkdir(parents=True, mode=0o700)
    except FileExistsError:
        pass
    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if nofollow is None or directory is None:
        raise OSError(f"this platform cannot safely open {label}")
    fd = os.open(
        path,
        os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        check_private_directory(fd, label)
        return fd
    except BaseException:
        close_fd(fd)
        raise


def ensure_private_directory(path: Path, label: str) -> None:
    """Create and verify a private directory, then close its descriptor."""
    fd = open_private_directory(path, label)
    close_fd(fd)


def check_owned_regular(fd: int, label: str, mode: int = 0o600) -> None:
    """Require a singly linked, owned regular file with exact permissions."""
    metadata = os.fstat(fd)
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError(f"{label} must be a regular file")
    if metadata.st_nlink != 1:
        raise OSError(f"{label} must have exactly one link")
    if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
        raise OSError(f"{label} has the wrong owner")
    if stat.S_IMODE(metadata.st_mode) != mode:
        raise OSError(f"{label} must have mode {mode:04o}")


def _basename(name: str, label: str) -> str:
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"{label} name must be a non-empty basename")
    return name


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _check_named_fd(directory_fd: int, name: str, fd: int, label: str) -> None:
    """Require *name* to still designate the already-validated descriptor."""
    named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    opened = os.fstat(fd)
    if not stat.S_ISREG(named.st_mode) or not _same_inode(named, opened):
        raise OSError(f"{label} directory entry changed during the transaction")


def open_owned_regular_at(
    directory_fd: int,
    name: str,
    flags: int,
    label: str,
    mode: int = 0o600,
) -> int:
    """Open one basename relative to a pinned directory descriptor."""
    name = _basename(name, label)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError(f"this platform cannot safely open {label}")
    fd = os.open(
        name,
        flags | nofollow | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0),
        mode,
        dir_fd=directory_fd,
    )
    try:
        check_owned_regular(fd, label, mode)
        _check_named_fd(directory_fd, name, fd, label)
        return fd
    except BaseException:
        close_fd(fd)
        raise


@contextmanager
def locked_private_directory(
    path: Path,
    lock_name: str,
    directory_label: str,
    lock_label: str,
) -> Iterator[int]:
    """Pin and exclusively lock one private directory for a full transaction."""
    directory_fd = open_private_directory(path, directory_label)
    lock_fd = -1
    locked = False
    try:
        lock_fd = open_owned_regular_at(
            directory_fd,
            lock_name,
            os.O_RDWR | os.O_CREAT,
            lock_label,
        )
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        locked = True
        # Recheck after waiting: a removed/relinked lock inode must not govern a
        # transaction that other writers can enter through a replacement lock.
        check_private_directory(directory_fd, directory_label)
        check_owned_regular(lock_fd, lock_label)
        _check_named_fd(directory_fd, lock_name, lock_fd, lock_label)
        try:
            yield directory_fd
        except BaseException:
            raise
        else:
            check_private_directory(directory_fd, directory_label)
            check_owned_regular(lock_fd, lock_label)
            _check_named_fd(directory_fd, lock_name, lock_fd, lock_label)
    finally:
        if locked:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if lock_fd >= 0:
            close_fd(lock_fd)
        close_fd(directory_fd)


def _write_all(fd: int, value: bytes) -> None:
    remaining = memoryview(value)
    while remaining:
        try:
            written = os.write(fd, remaining)
        except InterruptedError:
            continue
        if written <= 0:
            raise OSError("short private-state write")
        remaining = remaining[written:]


def read_bytes_at(directory_fd: int, name: str, label: str) -> bytes:
    """Read one validated private file relative to a pinned directory."""
    fd = open_owned_regular_at(directory_fd, name, os.O_RDONLY, label)
    try:
        check_owned_regular(fd, label)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, 65536)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        check_owned_regular(fd, label)
        _check_named_fd(directory_fd, name, fd, label)
        return b"".join(chunks)
    finally:
        close_fd(fd)


def create_bytes_at(directory_fd: int, name: str, value: bytes, label: str) -> None:
    """Create, write, and durably validate one new private file."""
    fd = open_owned_regular_at(
        directory_fd,
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        label,
    )
    try:
        check_owned_regular(fd, label)
        _write_all(fd, value)
        os.fsync(fd)
        check_owned_regular(fd, label)
        _check_named_fd(directory_fd, name, fd, label)
        os.fsync(directory_fd)
    except BaseException:
        try:
            _check_named_fd(directory_fd, name, fd, label)
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        close_fd(fd)


def append_bytes_at(directory_fd: int, name: str, value: bytes, label: str) -> None:
    """Append bytes only after and before validating the private file."""
    fd = open_owned_regular_at(
        directory_fd,
        name,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT,
        label,
    )
    try:
        check_owned_regular(fd, label)
        _write_all(fd, value)
        os.fsync(fd)
        check_owned_regular(fd, label)
        _check_named_fd(directory_fd, name, fd, label)
    finally:
        close_fd(fd)


def _check_existing_at(directory_fd: int, name: str, label: str) -> None:
    try:
        fd = open_owned_regular_at(directory_fd, name, os.O_RDONLY, label)
    except FileNotFoundError:
        return
    close_fd(fd)


def atomic_write_bytes_at(
    directory_fd: int,
    name: str,
    value: bytes,
    label: str,
) -> None:
    """Atomically replace one private file inside a pinned directory."""
    name = _basename(name, label)
    _check_existing_at(directory_fd, name, label)
    temp_name = f"{name}.{os.getpid()}-{uuid.uuid4().hex}.tmp"
    fd = -1
    try:
        fd = open_owned_regular_at(
            directory_fd,
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            label,
        )
        check_owned_regular(fd, label)
        _write_all(fd, value)
        os.fsync(fd)
        check_owned_regular(fd, label)
        _check_named_fd(directory_fd, temp_name, fd, label)

        # Revalidate an existing destination immediately before replacing it;
        # symlinks and multiply-linked files are rejected, never overwritten.
        _check_existing_at(directory_fd, name, label)
        os.replace(
            temp_name,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        check_owned_regular(fd, label)
        _check_named_fd(directory_fd, name, fd, label)
        final_fd = open_owned_regular_at(directory_fd, name, os.O_RDONLY, label)
        try:
            if not _same_inode(os.fstat(fd), os.fstat(final_fd)):
                raise OSError(f"{label} replacement identity changed")
            check_owned_regular(final_fd, label)
        finally:
            close_fd(final_fd)
        os.fsync(directory_fd)
    finally:
        if fd >= 0:
            close_fd(fd)
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def unlink_owned_regular_at(
    directory_fd: int,
    name: str,
    label: str,
    *,
    missing_ok: bool = True,
) -> None:
    """Unlink only a validated private regular file in the pinned directory."""
    try:
        fd = open_owned_regular_at(directory_fd, name, os.O_RDONLY, label)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    try:
        check_owned_regular(fd, label)
        _check_named_fd(directory_fd, name, fd, label)
        os.unlink(name, dir_fd=directory_fd)
        unlinked = os.fstat(fd)
        if (
            not stat.S_ISREG(unlinked.st_mode)
            or unlinked.st_nlink != 0
            or stat.S_IMODE(unlinked.st_mode) != 0o600
            or (hasattr(os, "getuid") and unlinked.st_uid != os.getuid())
        ):
            raise OSError(f"{label} changed while it was unlinked")
        os.fsync(directory_fd)
    finally:
        close_fd(fd)
