#!/usr/bin/env python3
"""Remove wheel-shipped bytecode before exact venv validation.

This narrowly scoped pre-site step runs with the selected venv interpreter
under ``-E -s -S -B -I``.  It never adds or imports from site-packages.  A
complete, bounded, no-follow scan must succeed before it removes regular
``.pyc``/``.pyo`` files and then empty ``__pycache__`` directories.
"""

from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import Iterable, NamedTuple, Sequence


BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})
MAX_SITE_PACKAGES_ENTRIES = 500_000
MINIMUM_PYTHON = (3, 10)


class SanitizationError(RuntimeError):
    """The selected environment cannot be sanitized without weakening policy."""


class _ScannedPath(NamedTuple):
    root_index: int
    relative_parts: tuple[str, ...]
    path: Path
    device: int
    inode: int


def _is_bytecode_name(path: Path) -> bool:
    return path.suffix.lower() in BYTECODE_SUFFIXES


def _regular_directory(path: Path, description: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SanitizationError(f"{description} is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SanitizationError(f"{description} is not a regular directory: {path}")
    return metadata


def _inside(path: Path, root: Path, description: str) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise SanitizationError(f"{description} escapes the selected venv: {path}") from exc


def _validated_package_roots(
    environment_prefix: Path, package_roots: Iterable[Path],
) -> tuple[Path, tuple[Path, ...]]:
    prefix_input = Path(environment_prefix)
    _regular_directory(prefix_input, "selected venv prefix")
    try:
        prefix = prefix_input.resolve(strict=True)
    except OSError as exc:
        raise SanitizationError("could not resolve the selected venv prefix") from exc

    resolved_roots: list[Path] = []
    for candidate_value in package_roots:
        candidate = Path(candidate_value)
        try:
            candidate_metadata = candidate.lstat()
        except OSError as exc:
            raise SanitizationError(
                f"selected venv package path is unavailable: {candidate}"
            ) from exc
        if stat.S_ISLNK(candidate_metadata.st_mode):
            raise SanitizationError(
                f"selected venv package path is a symlink: {candidate}"
            )
        if not stat.S_ISDIR(candidate_metadata.st_mode):
            raise SanitizationError(
                f"selected venv package path is not a regular directory: {candidate}"
            )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SanitizationError(
                f"could not resolve the selected venv package path: {candidate}"
            ) from exc
        _inside(resolved, prefix, "selected venv package path")
        _regular_directory(resolved, "resolved venv package path")
        if resolved not in resolved_roots:
            resolved_roots.append(resolved)
    if not resolved_roots:
        raise SanitizationError("the selected venv has no supported site-packages directory")
    return prefix, tuple(resolved_roots)


def _directory_open_flags() -> int:
    directory_flag = getattr(os, "O_DIRECTORY", None)
    no_follow_flag = getattr(os, "O_NOFOLLOW", None)
    if directory_flag is None or no_follow_flag is None:
        raise SanitizationError(
            "the host cannot provide descriptor-pinned no-follow traversal"
        )
    return os.O_RDONLY | directory_flag | no_follow_flag


def _open_relative_directory(
    root_descriptor: int, relative_parts: tuple[str, ...], display_path: Path,
) -> int:
    try:
        current = os.dup(root_descriptor)
    except OSError as exc:
        raise SanitizationError(
            f"could not pin the selected venv package path: {display_path}"
        ) from exc
    try:
        for part in relative_parts:
            try:
                child = os.open(
                    part, _directory_open_flags(), dir_fd=current,
                )
            except OSError as exc:
                raise SanitizationError(
                    f"site-packages directory changed or became a symlink: {display_path}"
                ) from exc
            os.close(current)
            current = child
        metadata = os.fstat(current)
        if not stat.S_ISDIR(metadata.st_mode):
            raise SanitizationError(
                f"site-packages entry is not a directory: {display_path}"
            )
        return current
    except Exception:
        os.close(current)
        raise


def _open_package_roots(package_roots: tuple[Path, ...]) -> list[int]:
    descriptors: list[int] = []
    try:
        for root in package_roots:
            try:
                descriptor = os.open(root, _directory_open_flags())
            except OSError as exc:
                raise SanitizationError(
                    f"could not pin the selected venv package path: {root}"
                ) from exc
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(descriptor)
                raise SanitizationError(
                    f"selected venv package path is not a directory: {root}"
                )
            descriptors.append(descriptor)
    except Exception:
        for descriptor in descriptors:
            os.close(descriptor)
        raise
    return descriptors


def _scan_package_roots(
    package_roots: tuple[Path, ...],
    root_descriptors: list[int],
    max_entries: int,
) -> tuple[list[_ScannedPath], list[_ScannedPath], int]:
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
        raise SanitizationError("site-packages inspection bound must be a positive integer")

    bytecode_files: list[_ScannedPath] = []
    cache_directories: list[_ScannedPath] = []
    visited = 0
    for root_index, package_root in enumerate(package_roots):
        pending: list[tuple[tuple[str, ...], Path]] = [((), package_root)]
        while pending:
            relative_parts, directory = pending.pop()
            directory_descriptor = _open_relative_directory(
                root_descriptors[root_index], relative_parts, directory,
            )
            try:
                inspected_entries: list[tuple[str, os.stat_result]] = []
                with os.scandir(directory_descriptor) as entries:
                    for entry in entries:
                        visited += 1
                        if visited > max_entries:
                            raise SanitizationError(
                                "site-packages inspection exceeded its bound"
                            )
                        path = directory / entry.name
                        try:
                            metadata = entry.stat(follow_symlinks=False)
                        except OSError as exc:
                            raise SanitizationError(
                                f"could not inspect site-packages entry: {path}"
                            ) from exc
                        inspected_entries.append((entry.name, metadata))
            except OSError as exc:
                raise SanitizationError(
                    f"could not inspect the selected venv package path: {directory}"
                ) from exc
            finally:
                os.close(directory_descriptor)
            inspected_entries.sort(key=lambda item: item[0])
            for name, metadata in inspected_entries:
                path = directory / name
                mode = metadata.st_mode
                if stat.S_ISLNK(mode):
                    raise SanitizationError(f"symlinked site-packages entry is forbidden: {path}")
                entry_parts = (*relative_parts, name)
                scanned = _ScannedPath(
                    root_index, entry_parts, path, metadata.st_dev, metadata.st_ino,
                )
                if stat.S_ISDIR(mode):
                    if _is_bytecode_name(path):
                        raise SanitizationError(
                            f"bytecode path is not a regular file: {path}"
                        )
                    if name == "__pycache__":
                        cache_directories.append(scanned)
                    pending.append((entry_parts, path))
                    continue
                if _is_bytecode_name(path):
                    if not stat.S_ISREG(mode):
                        raise SanitizationError(
                            f"bytecode path is not a regular file: {path}"
                        )
                    bytecode_files.append(scanned)
    return bytecode_files, cache_directories, visited


def _parent_descriptor(
    scanned: _ScannedPath, root_descriptors: list[int],
) -> tuple[int, str]:
    if not scanned.relative_parts:
        raise SanitizationError(f"refusing to mutate a package root: {scanned.path}")
    descriptor = _open_relative_directory(
        root_descriptors[scanned.root_index],
        scanned.relative_parts[:-1],
        scanned.path.parent,
    )
    return descriptor, scanned.relative_parts[-1]


def _unchanged_regular_file(
    scanned: _ScannedPath, root_descriptors: list[int],
) -> tuple[int, str]:
    parent_descriptor, name = _parent_descriptor(scanned, root_descriptors)
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        os.close(parent_descriptor)
        raise SanitizationError(
            f"bytecode path changed after inspection: {scanned.path}"
        ) from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_dev != scanned.device
        or metadata.st_ino != scanned.inode
    ):
        os.close(parent_descriptor)
        raise SanitizationError(
            f"bytecode path changed after inspection: {scanned.path}"
        )
    return parent_descriptor, name


def _unchanged_directory(
    scanned: _ScannedPath, root_descriptors: list[int],
) -> tuple[int, str]:
    parent_descriptor, name = _parent_descriptor(scanned, root_descriptors)
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except OSError as exc:
        os.close(parent_descriptor)
        raise SanitizationError(
            f"bytecode cache directory changed after inspection: {scanned.path}"
        ) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_dev != scanned.device
        or metadata.st_ino != scanned.inode
    ):
        os.close(parent_descriptor)
        raise SanitizationError(
            f"bytecode cache directory changed after inspection: {scanned.path}"
        )
    return parent_descriptor, name


def sanitize_package_roots(
    environment_prefix: Path,
    package_roots: Iterable[Path],
    *,
    max_entries: int = MAX_SITE_PACKAGES_ENTRIES,
) -> tuple[int, int, int]:
    """Sanitize explicit venv package roots after a complete safe scan.

    Returns ``(removed_bytecode_files, removed_cache_directories, visited)``.
    Nothing is removed if traversal, bounds, or entry-shape validation fails.
    """
    _, roots = _validated_package_roots(environment_prefix, package_roots)
    root_descriptors = _open_package_roots(roots)
    try:
        bytecode_files, cache_directories, visited = _scan_package_roots(
            roots, root_descriptors, max_entries,
        )

        removed_files = 0
        for scanned in bytecode_files:
            parent_descriptor, name = _unchanged_regular_file(
                scanned, root_descriptors,
            )
            try:
                os.unlink(name, dir_fd=parent_descriptor)
            except OSError as exc:
                raise SanitizationError(
                    f"could not remove bytecode file: {scanned.path}"
                ) from exc
            finally:
                os.close(parent_descriptor)
            removed_files += 1

        removed_directories = 0
        for scanned in sorted(
            cache_directories,
            key=lambda item: len(item.relative_parts),
            reverse=True,
        ):
            parent_descriptor, name = _unchanged_directory(
                scanned, root_descriptors,
            )
            try:
                os.rmdir(name, dir_fd=parent_descriptor)
            except OSError as exc:
                if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                    continue
                raise SanitizationError(
                    f"could not remove bytecode cache directory: {scanned.path}"
                ) from exc
            finally:
                os.close(parent_descriptor)
            removed_directories += 1
        return removed_files, removed_directories, visited
    finally:
        for descriptor in root_descriptors:
            os.close(descriptor)


def _require_pre_site_interpreter() -> None:
    required_flags = (
        sys.flags.no_site == 1,
        sys.flags.no_user_site == 1,
        sys.flags.ignore_environment == 1,
        sys.flags.isolated == 1,
        sys.flags.dont_write_bytecode == 1,
    )
    if not all(required_flags) or "site" in sys.modules:
        raise SanitizationError(
            "sanitizer must run before site initialization under -E -s -S -B -I"
        )


def _selected_environment() -> tuple[Path, tuple[Path, ...]]:
    if sys.version_info < MINIMUM_PYTHON:
        raise SanitizationError("Python 3.10+ is required for the harness")
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise SanitizationError("selected Python executable is not absolute")
    prefix_input = executable.parent.parent
    configuration = prefix_input / "pyvenv.cfg"
    try:
        configuration_metadata = configuration.lstat()
    except OSError as exc:
        raise SanitizationError(
            "the sanitizer Python must be an isolated venv with pyvenv.cfg"
        ) from exc
    if (
        stat.S_ISLNK(configuration_metadata.st_mode)
        or not stat.S_ISREG(configuration_metadata.st_mode)
    ):
        raise SanitizationError("the selected venv configuration is not a regular file")
    try:
        prefix = prefix_input.resolve(strict=True)
    except OSError as exc:
        raise SanitizationError("could not resolve the selected venv prefix") from exc

    version_directory = f"python{sys.version_info.major}.{sys.version_info.minor}"
    roots: list[Path] = []
    for candidate in (
        prefix / "lib" / version_directory / "site-packages",
        prefix / "lib64" / version_directory / "site-packages",
    ):
        if not candidate.exists():
            continue
        resolved = candidate.resolve(strict=True)
        _inside(resolved, prefix, "selected venv package path")
        if resolved not in roots:
            roots.append(resolved)
    validated_prefix, validated_roots = _validated_package_roots(prefix, roots)
    active_paths = {
        Path(value).resolve(strict=False) for value in sys.path if value
    }
    if any(root in active_paths for root in validated_roots):
        raise SanitizationError("site-packages was active before environment sanitization")
    return validated_prefix, validated_roots


def main(argv: Sequence[str] | None = None) -> int:
    if argv is not None and list(argv):
        print("ERROR: sanitize_python_environment.py accepts no arguments", file=sys.stderr)
        return 2
    try:
        _require_pre_site_interpreter()
        prefix, roots = _selected_environment()
        removed_files, removed_directories, visited = sanitize_package_roots(
            prefix, roots,
        )
    except (OSError, SanitizationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Removed {removed_files} regular bytecode file(s) and "
        f"{removed_directories} empty __pycache__ directorie(s) after a bounded "
        f"scan of {visited} site-packages entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
