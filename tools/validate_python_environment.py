#!/usr/bin/env python3
"""Validate the installed harness environment against its resolved lock.

The lock hashes authenticate downloaded archives only when installation uses
``pip install --require-hashes``.  This validator separately proves that the
selected environment has the exact application distribution set and that its
installed package surface is internally consistent with every distribution's
RECORD hashes and sizes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Iterable, Sequence


LOCK_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)\s+\\\s*$")
LOCK_HASH_RE = re.compile(
    r"^\s*--hash=sha256:([0-9a-f]{64})(?:\s+(\\))?\s*$"
)
DIRECT_PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
BYTECODE_SUFFIXES = frozenset({".pyc", ".pyo"})

# ``venv`` bootstraps these packaging utilities outside the application lock.
# They are the only extra distributions permitted, and their installed files
# are subjected to the same RECORD integrity and unrecorded-file checks.
TOOLCHAIN_DISTRIBUTIONS = frozenset({"pip", "setuptools"})


class EnvironmentValidationError(ValueError):
    """The selected Python environment does not match the release contract."""


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_direct_requirements(text: str) -> dict[str, str]:
    required: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = DIRECT_PIN_RE.fullmatch(line)
        if match is None:
            raise EnvironmentValidationError(
                f"unsupported direct requirement: {line}"
            )
        package = canonical_name(match.group(1))
        if package in required:
            raise EnvironmentValidationError(
                f"duplicate direct requirement: {package}"
            )
        required[package] = match.group(2)
    if not required:
        raise EnvironmentValidationError("no direct harness requirements were declared")
    return required


def parse_hash_lock(text: str) -> dict[str, str]:
    """Parse the intentionally small, fully pinned requirements syntax."""
    pins: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        pin = LOCK_PIN_RE.fullmatch(lines[index])
        if pin is None:
            raise EnvironmentValidationError(
                "malformed or unhashed lock entry in requirements.lock"
            )
        package = canonical_name(pin.group(1))
        if package in pins:
            raise EnvironmentValidationError(
                f"duplicate or conflicting lock entry for {package}"
            )
        pins[package] = pin.group(2)
        index += 1
        entry_hashes: set[str] = set()
        while index < len(lines):
            digest = LOCK_HASH_RE.fullmatch(lines[index])
            if digest is None:
                raise EnvironmentValidationError(
                    "malformed or unhashed lock entry in requirements.lock"
                )
            entry_hashes.add(digest.group(1))
            index += 1
            if digest.group(2) != "\\":
                break
            if index >= len(lines):
                raise EnvironmentValidationError(
                    "malformed or unhashed lock entry in requirements.lock"
                )
        if not entry_hashes:
            raise EnvironmentValidationError(
                "malformed or unhashed lock entry in requirements.lock"
            )
    if not pins:
        raise EnvironmentValidationError(
            "no hash-locked harness requirements were declared"
        )
    return pins


def _is_executable_bytecode(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix.lower() in BYTECODE_SUFFIXES


def _file_digest(path: Path, algorithm: str) -> str:
    try:
        digest = hashlib.new(algorithm)
    except ValueError as exc:
        raise EnvironmentValidationError(
            f"unsupported RECORD hash algorithm {algorithm!r} for {path}"
        ) from exc
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def _distribution_name(distribution: metadata.Distribution) -> str:
    raw_name = distribution.metadata.get("Name")
    if not raw_name:
        raise EnvironmentValidationError("installed distribution has no Name metadata")
    return canonical_name(str(raw_name))


def validate_installed_distributions(
    locked: dict[str, str],
    *,
    distributions: Iterable[metadata.Distribution] | None = None,
    distribution_paths: Sequence[Path] | None = None,
    environment_prefix: Path | None = None,
    toolchain_allowlist: frozenset[str] = TOOLCHAIN_DISTRIBUTIONS,
) -> tuple[int, int]:
    """Validate exact versions, RECORD bytes, and the recorded package surface."""
    if distributions is None:
        installed_distributions = list(metadata.distributions(
            path=None if distribution_paths is None else [
                str(path) for path in distribution_paths
            ]
        ))
    else:
        installed_distributions = list(distributions)
    prefix = (environment_prefix or Path(sys.prefix)).resolve()
    installed: dict[str, metadata.Distribution] = {}
    for distribution in installed_distributions:
        name = _distribution_name(distribution)
        if name in installed:
            raise EnvironmentValidationError(
                f"duplicate installed distribution metadata for {name}"
            )
        installed[name] = distribution

    recorded_paths: dict[Path, str] = {}
    package_roots: set[Path] = set()
    verified_files = 0
    for name, distribution in sorted(installed.items()):
        if name in locked and distribution.version != locked[name]:
            raise EnvironmentValidationError(
                f"{name}=={distribution.version} is installed; expected {locked[name]}"
            )
        files = distribution.files
        if not files:
            raise EnvironmentValidationError(
                f"{name} has no installed RECORD file inventory"
            )
        package_root = Path(distribution.locate_file("")).resolve()
        try:
            package_root.relative_to(prefix)
        except ValueError as exc:
            raise EnvironmentValidationError(
                f"{name} is installed outside the selected environment: {package_root}"
            ) from exc
        package_roots.add(package_root)

        record_entries = 0
        for relative_file in files:
            located_path = Path(distribution.locate_file(relative_file))
            path = located_path.resolve(strict=False)
            if _is_executable_bytecode(located_path):
                # Some installers retain unhashed RECORD declarations for a
                # platform cache outside the venv. The reviewed runner starts
                # with -S/-B and no pycache prefix, so those paths are not an
                # import surface. Any bytecode actually present under a
                # validated package root is rejected by the complete scan.
                continue
            try:
                path.relative_to(prefix)
            except ValueError as exc:
                raise EnvironmentValidationError(
                    f"{name} RECORD entry escapes the selected environment: {relative_file}"
                ) from exc
            if located_path.is_symlink() or not path.is_file():
                raise EnvironmentValidationError(
                    f"{name} RECORD entry is missing or not a regular file: {relative_file}"
                )
            previous_owner = recorded_paths.get(path)
            if previous_owner is not None and previous_owner != name:
                raise EnvironmentValidationError(
                    f"installed file is claimed by both {previous_owner} and {name}: {path}"
                )
            recorded_paths[path] = name

            file_hash = getattr(relative_file, "hash", None)
            size = getattr(relative_file, "size", None)
            if file_hash is None:
                if not str(relative_file).endswith(".dist-info/RECORD"):
                    raise EnvironmentValidationError(
                        f"{name} has an unhashed installed file: {relative_file}"
                    )
                record_entries += 1
                continue
            if size is None or path.stat().st_size != int(size):
                raise EnvironmentValidationError(
                    f"{name} installed file size differs from RECORD: {relative_file}"
                )
            actual_digest = _file_digest(path, file_hash.mode)
            if actual_digest != file_hash.value:
                raise EnvironmentValidationError(
                    f"{name} installed file hash differs from RECORD: {relative_file}"
                )
            verified_files += 1
        if record_entries != 1:
            raise EnvironmentValidationError(
                f"{name} must have exactly one unhashed .dist-info/RECORD entry"
            )

    # Scan every package root plus each environment-level surface to which a
    # distribution installed a script or data file (for example bin/, etc/,
    # or share/). This catches an injected console script as well as an
    # injected importable module without treating unrelated prefix contents as
    # package-owned.
    package_surface_roots = set(package_roots)
    for recorded_path in recorded_paths:
        if any(
            recorded_path == root or root in recorded_path.parents
            for root in package_roots
        ):
            continue
        relative_path = recorded_path.relative_to(prefix)
        if relative_path.parts:
            package_surface_roots.add(prefix / relative_path.parts[0])

    venv_scaffold = {
        prefix / "bin" / "Activate.ps1",
        prefix / "bin" / "activate",
        prefix / "bin" / "activate.csh",
        prefix / "bin" / "activate.fish",
        prefix / "bin" / "python",
        prefix / "bin" / f"python{sys.version_info.major}",
        prefix / "bin" / f"python{sys.version_info.major}.{sys.version_info.minor}",
    }
    for package_root in sorted(package_surface_roots):
        for directory, directory_names, file_names in os.walk(
            package_root, topdown=True, followlinks=False
        ):
            directory_path = Path(directory)
            for directory_name in list(directory_names):
                candidate = directory_path / directory_name
                if candidate.is_symlink():
                    raise EnvironmentValidationError(
                        f"unrecorded package-directory symlink: {candidate}"
                    )
                if directory_name == "__pycache__":
                    raise EnvironmentValidationError(
                        f"executable bytecode/cache artifact is forbidden: {candidate}"
                    )
            for file_name in file_names:
                path = directory_path / file_name
                if _is_executable_bytecode(path):
                    raise EnvironmentValidationError(
                        f"executable bytecode/cache artifact is forbidden: {path}"
                    )
                resolved = path.resolve(strict=False)
                if path in venv_scaffold:
                    continue
                if path.is_symlink() or resolved not in recorded_paths:
                    raise EnvironmentValidationError(
                        f"unrecorded package file: {path}"
                    )
    unexpected = sorted(set(installed) - set(locked) - set(toolchain_allowlist))
    missing = sorted(set(locked) - set(installed))
    if unexpected:
        raise EnvironmentValidationError(
            "unexpected installed distribution(s): " + ", ".join(unexpected)
        )
    if missing:
        raise EnvironmentValidationError(
            "locked distribution(s) missing: " + ", ".join(missing)
        )
    return len(locked), verified_files


def validate_environment(
    requirements_path: Path,
    lock_path: Path,
    *,
    distribution_paths: Sequence[Path] | None = None,
    environment_prefix: Path | None = None,
) -> tuple[int, int]:
    required = parse_direct_requirements(requirements_path.read_text(encoding="utf-8"))
    locked = parse_hash_lock(lock_path.read_text(encoding="utf-8"))
    for package, version in required.items():
        if locked.get(package) != version:
            raise EnvironmentValidationError(
                f"{package}=={version} is not identically pinned in {lock_path}"
            )
    return validate_installed_distributions(
        locked,
        distribution_paths=distribution_paths,
        environment_prefix=environment_prefix,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--requirements", type=Path,
        default=Path("agent-harness/requirements.txt"),
    )
    parser.add_argument(
        "--lock", type=Path, default=Path("agent-harness/requirements.lock"),
    )
    args = parser.parse_args(argv)
    try:
        locked_count, verified_files = validate_environment(
            args.requirements, args.lock
        )
    except (EnvironmentValidationError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    print(
        f"Validated {locked_count} locked application distributions, only the "
        f"explicit pip/setuptools toolchain allowance, and {verified_files} "
        "installed RECORD file hashes/sizes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
