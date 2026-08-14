#!/usr/bin/env python3
"""Validate the reviewed R lock closure and installed package-tree bytes.

``renv.lock`` proves the selected versions and dependency records, but it does
not contain archive digests.  The separately reviewed integrity manifest pins
the resulting installed package paths and bytes for an exact R version and
platform after an operator-reviewed restore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent.parent
LOCK_PATH = ROOT / "renv.lock"
MANIFEST_PATH = ROOT / "governance" / "r-package-integrity.json"
R_LAUNCHER = ROOT / "tools" / "run-reviewed-r.sh"
R_LOCK_VALIDATOR = ROOT / "tools" / "validate_r_lock.R"

REQUIRED_ROOTS = ("jsonlite", "mvtnorm", "survival")
OPTIONAL_ROOTS = ("Exact", "MAMS")
PROOF_BOUNDARY = (
    "Pins installed package-tree paths and bytes after an operator-reviewed "
    "restore; it does not authenticate source or binary archives."
)

MAX_ENVIRONMENTS = 32
MAX_PACKAGE_FILES = 50_000
MAX_PACKAGE_FILE_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_TREE_BYTES = 512 * 1024 * 1024
MAX_PACKAGE_DEPTH = 64
MAX_RELATIVE_PATH_BYTES = 4096
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9.]*$")
DEPENDENCY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9.]*)")

R_INVENTORY_CODE = r'''
installed <- utils::installed.packages()
metadata <- c(as.character(getRversion()), R.version$platform, R.version$os)
if (any(grepl("[\t\r\n]", metadata))) stop("invalid R runtime metadata")
writeLines(paste(c("META", metadata), collapse = "\t"))
for (index in seq_len(nrow(installed))) {
  package <- rownames(installed)[[index]]
  version <- installed[index, "Version"]
  priority <- installed[index, "Priority"]
  if (is.na(priority)) priority <- ""
  package_path <- normalizePath(
    file.path(installed[index, "LibPath"], package),
    winslash = "/", mustWork = TRUE
  )
  fields <- c(package, version, priority, package_path)
  if (any(grepl("[\t\r\n]", fields))) stop("invalid installed package metadata")
  writeLines(paste(c("PACKAGE", fields), collapse = "\t"))
}
'''


class ValidationError(RuntimeError):
    """A release-environment invariant was not satisfied."""


@dataclass(frozen=True)
class InstalledPackage:
    version: str
    priority: str
    path: Path


@dataclass(frozen=True)
class RuntimeInventory:
    r_version: str
    r_platform: str
    r_os: str
    packages: dict[str, InstalledPackage]


def _run_reviewed_r(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["/bin/sh", str(R_LAUNCHER), *arguments],
            cwd=ROOT,
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValidationError("reviewed R validation timed out") from exc


def validate_r_lock() -> None:
    result = _run_reviewed_r([str(R_LOCK_VALIDATOR)])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise ValidationError(detail or "R lock validation failed")


def load_runtime_inventory() -> RuntimeInventory:
    result = _run_reviewed_r(["-e", R_INVENTORY_CODE])
    if result.returncode != 0:
        raise ValidationError("could not inventory the reviewed R installation")
    metadata: tuple[str, str, str] | None = None
    packages: dict[str, InstalledPackage] = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if fields[0] == "META" and len(fields) == 4 and metadata is None:
            metadata = (fields[1], fields[2], fields[3])
            continue
        if fields[0] != "PACKAGE" or len(fields) != 5:
            raise ValidationError("reviewed R returned malformed inventory output")
        name, version, priority, raw_path = fields[1:]
        if not PACKAGE_RE.fullmatch(name) or name in packages:
            raise ValidationError("reviewed R returned an invalid package inventory")
        package_path = Path(raw_path)
        try:
            resolved = package_path.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(f"installed R package path is unavailable: {name}") from exc
        if (
            not package_path.is_absolute()
            or package_path != resolved
            or resolved.name != name
            or not resolved.is_dir()
        ):
            raise ValidationError(f"installed R package path is not canonical: {name}")
        packages[name] = InstalledPackage(version, priority, resolved)
    if metadata is None or not all(metadata) or not packages:
        raise ValidationError("reviewed R inventory was incomplete")
    return RuntimeInventory(*metadata, packages)


def _flatten_strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _flatten_strings(item)


def dependency_names(record: dict[str, object]) -> set[str]:
    dependencies: set[str] = set()
    for field in ("Depends", "Imports", "LinkingTo"):
        for value in _flatten_strings(record.get(field)):
            for entry in value.split(","):
                match = DEPENDENCY_RE.match(entry.strip())
                if match:
                    dependencies.add(match.group(1))
    return dependencies


def _version_key(value: str) -> tuple[int, ...] | None:
    parts = re.split(r"[.-]", value)
    if not parts or any(not part.isdigit() for part in parts):
        return None
    numbers = [int(part) for part in parts]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    return tuple(numbers)


def versions_match(actual: str, expected: str) -> bool:
    actual_key = _version_key(actual)
    expected_key = _version_key(expected)
    return (
        actual == expected
        if actual_key is None or expected_key is None
        else actual_key == expected_key
    )


def applicable_packages(
    lock: dict[str, object], inventory: RuntimeInventory,
) -> dict[str, InstalledPackage]:
    locked_packages = lock.get("Packages")
    if not isinstance(locked_packages, dict):
        raise ValidationError("renv.lock Packages must be an object")
    locked_r = lock.get("R")
    expected_r = locked_r.get("Version") if isinstance(locked_r, dict) else None
    if expected_r != inventory.r_version:
        raise ValidationError(
            f"R runtime does not match renv.lock (installed {inventory.r_version}, "
            f"locked {expected_r or 'missing'})"
        )

    def locked_record(name: str) -> dict[str, object]:
        record = locked_packages.get(name)
        if not isinstance(record, dict) or not isinstance(record.get("Version"), str):
            raise ValidationError(f"renv.lock is missing a version for {name}")
        return record

    for name in (*REQUIRED_ROOTS, *OPTIONAL_ROOTS):
        locked_record(name)
    missing = sorted(set(REQUIRED_ROOTS) - inventory.packages.keys())
    if missing:
        raise ValidationError(f"required R package is missing: {', '.join(missing)}")

    base = {
        name for name, package in inventory.packages.items()
        if package.priority == "base"
    }
    active_roots = list(REQUIRED_ROOTS) + [
        name for name in OPTIONAL_ROOTS if name in inventory.packages
    ]
    applicable: set[str] = set()
    queue = list(active_roots)
    while queue:
        name = queue.pop(0)
        if name in applicable or name in base or name == "R":
            continue
        record = locked_record(name)
        applicable.add(name)
        for dependency in sorted(dependency_names(record)):
            if dependency in base or dependency == "R":
                continue
            if dependency not in locked_packages:
                raise ValidationError(
                    f"renv.lock is missing dependency {dependency} required by {name}"
                )
            if dependency not in applicable:
                queue.append(dependency)

    applicable.update(set(locked_packages).intersection(inventory.packages))
    selected: dict[str, InstalledPackage] = {}
    for name in sorted(applicable):
        package = inventory.packages.get(name)
        if package is None:
            raise ValidationError(f"applicable R package is missing: {name}")
        expected = locked_record(name)["Version"]
        assert isinstance(expected, str)
        if not versions_match(package.version, expected):
            raise ValidationError(
                f"R package {name} does not match renv.lock "
                f"(installed {package.version}, locked {expected})"
            )
        selected[name] = package
    return selected


def _read_regular_file(
    directory_fd: int, name: str, expected: os.stat_result, relative: str,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ValidationError(f"could not safely open R package file: {relative}") from exc
    digest = hashlib.sha256()
    consumed = 0
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)
            or opened.st_size != expected.st_size
        ):
            raise ValidationError(f"R package file changed during inspection: {relative}")
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, expected.st_size - consumed + 1))
            if not chunk:
                break
            consumed += len(chunk)
            if consumed > expected.st_size or consumed > MAX_PACKAGE_FILE_BYTES:
                raise ValidationError(f"R package file exceeds integrity bounds: {relative}")
            digest.update(chunk)
        finished = os.fstat(descriptor)
        if (
            consumed != expected.st_size
            or finished.st_size != expected.st_size
            or finished.st_mtime_ns != opened.st_mtime_ns
            or finished.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ValidationError(f"R package file changed during inspection: {relative}")
    finally:
        os.close(descriptor)
    return digest.digest()


def package_tree_record(package_path: Path) -> dict[str, object]:
    """Return a deterministic, bounded, no-follow record for one package tree."""
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(package_path, directory_flags)
    except OSError as exc:
        raise ValidationError("could not safely open installed R package directory") from exc
    records: list[tuple[bytes, int, bytes]] = []
    total_bytes = 0

    def walk(directory_fd: int, prefix: str, depth: int) -> None:
        nonlocal total_bytes
        if depth > MAX_PACKAGE_DEPTH:
            raise ValidationError("R package tree exceeds the maximum directory depth")
        try:
            with os.scandir(directory_fd) as iterator:
                entries = list(iterator)
        except OSError as exc:
            raise ValidationError("could not enumerate installed R package tree") from exc
        for entry in sorted(entries, key=lambda item: os.fsencode(item.name)):
            relative = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                relative_bytes = relative.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValidationError("R package path is not valid UTF-8") from exc
            if len(relative_bytes) > MAX_RELATIVE_PATH_BYTES:
                raise ValidationError("R package path exceeds integrity bounds")
            try:
                inspected = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValidationError(f"R package entry changed during inspection: {relative}") from exc
            if stat.S_ISLNK(inspected.st_mode):
                raise ValidationError(f"R package tree contains a symlink: {relative}")
            if stat.S_ISDIR(inspected.st_mode):
                try:
                    child_fd = os.open(entry.name, directory_flags, dir_fd=directory_fd)
                except OSError as exc:
                    raise ValidationError(
                        f"could not safely open R package directory: {relative}"
                    ) from exc
                try:
                    opened = os.fstat(child_fd)
                    if (
                        not stat.S_ISDIR(opened.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (inspected.st_dev, inspected.st_ino)
                    ):
                        raise ValidationError(
                            f"R package directory changed during inspection: {relative}"
                        )
                    walk(child_fd, relative, depth + 1)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(inspected.st_mode):
                raise ValidationError(f"R package tree contains a non-regular entry: {relative}")
            if inspected.st_size > MAX_PACKAGE_FILE_BYTES:
                raise ValidationError(f"R package file exceeds integrity bounds: {relative}")
            content_digest = _read_regular_file(
                directory_fd, entry.name, inspected, relative,
            )
            records.append((relative_bytes, inspected.st_size, content_digest))
            if len(records) > MAX_PACKAGE_FILES:
                raise ValidationError("R package tree contains too many files")
            total_bytes += inspected.st_size
            if total_bytes > MAX_PACKAGE_TREE_BYTES:
                raise ValidationError("R package tree exceeds integrity byte bounds")

    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise ValidationError("installed R package path is not a directory")
        walk(root_fd, "", 0)
    finally:
        os.close(root_fd)

    if not records:
        raise ValidationError("installed R package tree is empty")
    tree_digest = hashlib.sha256(b"expdesign-r-package-tree-v1\0")
    for relative_bytes, size, content_digest in sorted(records):
        tree_digest.update(struct.pack(">I", len(relative_bytes)))
        tree_digest.update(relative_bytes)
        tree_digest.update(struct.pack(">Q", size))
        tree_digest.update(content_digest)
    return {
        "file_count": len(records),
        "total_bytes": total_bytes,
        "tree_sha256": tree_digest.hexdigest(),
    }


def stable_package_tree_record(package_path: Path) -> dict[str, object]:
    first = package_tree_record(package_path)
    second = package_tree_record(package_path)
    if first != second:
        raise ValidationError("installed R package tree changed during inspection")
    return first


def build_environment_record(
    inventory: RuntimeInventory,
    selected: dict[str, InstalledPackage],
    lock_digest: str,
) -> dict[str, object]:
    packages: dict[str, dict[str, object]] = {}
    for name, package in selected.items():
        packages[name] = {
            "version": package.version,
            **stable_package_tree_record(package.path),
        }
    profile_source = "\0".join(packages).encode("ascii")
    return {
        "r_version": inventory.r_version,
        "r_platform": inventory.r_platform,
        "r_os": inventory.r_os,
        "profile": hashlib.sha256(profile_source).hexdigest()[:16],
        "renv_lock_sha256": lock_digest,
        "packages": packages,
    }


def generated_manifest(
    inventory: RuntimeInventory,
    selected: dict[str, InstalledPackage],
    lock_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "proof_boundary": PROOF_BOUNDARY,
        "environments": [build_environment_record(inventory, selected, lock_digest)],
    }


def load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValidationError("R package integrity manifest is missing or invalid") from exc
    if not isinstance(value, dict):
        raise ValidationError("R package integrity manifest must be an object")
    return value


def validate_manifest(
    manifest: dict[str, object],
    inventory: RuntimeInventory,
    selected: dict[str, InstalledPackage],
    lock_digest: str,
) -> None:
    if set(manifest) != {"schema_version", "proof_boundary", "environments"}:
        raise ValidationError("R package integrity manifest has unexpected fields")
    if manifest.get("schema_version") != 1 or manifest.get("proof_boundary") != PROOF_BOUNDARY:
        raise ValidationError("R package integrity manifest policy is invalid")
    environments = manifest.get("environments")
    if (
        not isinstance(environments, list)
        or not environments
        or len(environments) > MAX_ENVIRONMENTS
    ):
        raise ValidationError("R package integrity environments are invalid")

    expected_names = set(selected)
    matching: list[dict[str, object]] = []
    for environment in environments:
        if not isinstance(environment, dict) or set(environment) != {
            "r_version", "r_platform", "r_os", "profile",
            "renv_lock_sha256", "packages",
        }:
            raise ValidationError("R package integrity environment is malformed")
        packages = environment.get("packages")
        if not isinstance(packages, dict):
            raise ValidationError("R package integrity package map is malformed")
        if (
            environment.get("r_version") == inventory.r_version
            and environment.get("r_platform") == inventory.r_platform
            and environment.get("r_os") == inventory.r_os
            and set(packages) == expected_names
        ):
            matching.append(environment)
    if len(matching) != 1:
        raise ValidationError(
            "R package integrity manifest has no unique profile for this exact "
            "R version, platform, and installed lock set"
        )
    environment = matching[0]
    if environment.get("renv_lock_sha256") != lock_digest:
        raise ValidationError("R package integrity profile does not bind the current renv.lock")
    expected_profile = hashlib.sha256("\0".join(sorted(selected)).encode("ascii")).hexdigest()[:16]
    if environment.get("profile") != expected_profile:
        raise ValidationError("R package integrity profile identifier is invalid")

    package_records = environment["packages"]
    assert isinstance(package_records, dict)
    for name, package in selected.items():
        expected = package_records.get(name)
        if not isinstance(expected, dict) or set(expected) != {
            "version", "file_count", "total_bytes", "tree_sha256",
        }:
            raise ValidationError(f"R package integrity record is malformed: {name}")
        if expected.get("version") != package.version:
            raise ValidationError(f"R package integrity version differs: {name}")
        digest = expected.get("tree_sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise ValidationError(f"R package integrity digest is invalid: {name}")
        actual = stable_package_tree_record(package.path)
        comparable = {key: expected[key] for key in (
            "file_count", "total_bytes", "tree_sha256",
        )}
        if actual != comparable:
            raise ValidationError(
                f"R package {name} installed-tree bytes differ from the reviewed manifest"
            )


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--generate-manifest",
        action="store_true",
        help="emit a new profile after an independently reviewed clean restore",
    )
    parser.add_argument(
        "--acknowledge-reviewed-restore",
        action="store_true",
        help="required acknowledgement when generating a profile",
    )
    options = parser.parse_args(arguments)
    if options.generate_manifest != options.acknowledge_reviewed_restore:
        parser.error(
            "--generate-manifest and --acknowledge-reviewed-restore must be used together"
        )
    try:
        validate_r_lock()
        inventory = load_runtime_inventory()
        lock_bytes = LOCK_PATH.read_bytes()
        lock = json.loads(lock_bytes)
        if not isinstance(lock, dict):
            raise ValidationError("renv.lock must be an object")
        selected = applicable_packages(lock, inventory)
        lock_digest = hashlib.sha256(lock_bytes).hexdigest()
        if options.generate_manifest:
            print(json.dumps(
                generated_manifest(inventory, selected, lock_digest),
                indent=2,
                sort_keys=True,
            ))
            return 0
        validate_manifest(
            load_manifest(MANIFEST_PATH), inventory, selected, lock_digest,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"Validated R {inventory.r_version}/{inventory.r_platform}, "
        f"{len(selected)} package versions, and installed package-tree SHA-256 manifest"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
