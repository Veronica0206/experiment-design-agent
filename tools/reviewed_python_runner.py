#!/usr/bin/env python3
"""Validate the selected venv before adding its package paths or running code."""

from __future__ import annotations

import importlib.util
import os
import re
import runpy
import stat
import sys
from pathlib import Path
from types import ModuleType


TOOLS_ROOT = Path(__file__).resolve().parent
SUITE_ROOT = TOOLS_ROOT.parent
VALIDATOR_PATH = TOOLS_ROOT / "validate_python_environment.py"
REQUIREMENTS_PATH = SUITE_ROOT / "agent-harness" / "requirements.txt"
LOCK_PATH = SUITE_ROOT / "agent-harness" / "requirements.lock"
MINIMUM_PYTHON = (3, 10)
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
BYTECODE_SUFFIXES = {".pyc", ".pyo"}
MAX_SOURCE_ENTRIES = 100_000
EXCLUDED_SOURCE_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "node_modules",
}


class RunnerError(RuntimeError):
    """The pre-site Python execution boundary could not be established."""


def _reject_source_bytecode(root: Path, environment_prefix: Path) -> None:
    """Reject executable caches anywhere Python may import project source."""
    visited = 0
    environment_prefix = environment_prefix.resolve()
    for directory, directory_names, file_names in os.walk(
        root, topdown=True, followlinks=False,
    ):
        directory_path = Path(directory)
        retained: list[str] = []
        for name in directory_names:
            visited += 1
            if visited > MAX_SOURCE_ENTRIES:
                raise RunnerError("project Python source inspection exceeded its bound")
            candidate = directory_path / name
            if name in EXCLUDED_SOURCE_DIRS:
                continue
            if candidate == environment_prefix:
                continue
            if candidate.is_symlink():
                raise RunnerError(
                    f"symlinked project source entry is forbidden: {candidate}"
                )
            if name == "__pycache__":
                raise RunnerError(
                    f"executable bytecode/cache artifact is forbidden: {candidate}"
                )
            retained.append(name)
        directory_names[:] = retained
        for name in file_names:
            visited += 1
            if visited > MAX_SOURCE_ENTRIES:
                raise RunnerError("project Python source inspection exceeded its bound")
            candidate = directory_path / name
            if candidate.is_symlink():
                raise RunnerError(
                    f"symlinked project source entry is forbidden: {candidate}"
                )
            if candidate.suffix.lower() in BYTECODE_SUFFIXES:
                raise RunnerError(
                    f"executable bytecode/cache artifact is forbidden: {candidate}"
                )


def _selected_environment() -> tuple[Path, tuple[Path, ...]]:
    if sys.version_info < MINIMUM_PYTHON:
        raise RunnerError("Python 3.10+ is required for the harness")
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise RunnerError("selected Python executable is not absolute")
    environment_prefix = executable.parent.parent
    configuration = environment_prefix / "pyvenv.cfg"
    try:
        configuration_stat = configuration.lstat()
    except OSError as exc:
        raise RunnerError(
            "the reviewed application Python must be an isolated venv with pyvenv.cfg"
        ) from exc
    if not stat.S_ISREG(configuration_stat.st_mode) or configuration.is_symlink():
        raise RunnerError("the selected venv configuration is not a regular file")
    environment_prefix = environment_prefix.resolve(strict=True)

    version_directory = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = (
        environment_prefix / "lib" / version_directory / "site-packages",
        environment_prefix / "lib64" / version_directory / "site-packages",
    )
    package_paths: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise RunnerError("the selected venv package path is not a regular directory")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(environment_prefix)
        except ValueError as exc:
            raise RunnerError("the selected venv package path escapes its prefix") from exc
        if resolved not in package_paths:
            package_paths.append(resolved)
    if not package_paths:
        raise RunnerError("the selected venv has no supported site-packages directory")
    return environment_prefix, tuple(package_paths)


def _load_validator() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "_expdesign_python_environment_validator", VALIDATOR_PATH,
    )
    if specification is None or specification.loader is None:
        raise RunnerError("could not load the Python environment validator")
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _stdlib_paths() -> list[str]:
    """Keep interpreter-owned paths but remove cwd and project source entries."""
    controlled: list[str] = []
    for raw_path in sys.path:
        if not raw_path:
            continue
        candidate = Path(raw_path).resolve(strict=False)
        if candidate == TOOLS_ROOT or candidate == SUITE_ROOT:
            continue
        if candidate == SUITE_ROOT or SUITE_ROOT in candidate.parents:
            continue
        value = str(candidate)
        if value not in controlled:
            controlled.append(value)
    return controlled


def _execute(arguments: list[str], package_paths: tuple[Path, ...]) -> None:
    if not arguments:
        raise RunnerError("usage: run-reviewed-python.sh [--validate-only|-m MODULE|SCRIPT] [args]")
    if arguments == ["--validate-only"]:
        return

    controlled_paths = [str(path) for path in package_paths]
    if arguments[0] == "-m":
        if len(arguments) < 2 or not MODULE_RE.fullmatch(arguments[1]):
            raise RunnerError("a valid module name must follow -m")
        module_name = arguments[1]
        sys.path[:] = [str(SUITE_ROOT), *controlled_paths, *_stdlib_paths()]
        sys.argv = [module_name, *arguments[2:]]
        runpy.run_module(module_name, run_name="__main__", alter_sys=True)
        return

    requested_script = Path(arguments[0])
    if not requested_script.is_absolute():
        requested_script = SUITE_ROOT / requested_script
    if requested_script.is_symlink():
        raise RunnerError("reviewed Python target is not a regular .py file")
    try:
        script = requested_script.resolve(strict=True)
        script.relative_to(SUITE_ROOT)
    except (OSError, ValueError) as exc:
        raise RunnerError("reviewed Python scripts must be regular files inside the suite") from exc
    if not script.is_file() or script.suffix != ".py":
        raise RunnerError("reviewed Python target is not a regular .py file")
    sys.path[:] = [str(script.parent), *controlled_paths, *_stdlib_paths()]
    sys.argv = [str(script), *arguments[1:]]
    runpy.run_path(str(script), run_name="__main__")


def main(arguments: list[str] | None = None) -> int:
    try:
        environment_prefix, package_paths = _selected_environment()
        # This scan happens before importing even the repository validator.
        _reject_source_bytecode(SUITE_ROOT, environment_prefix)
        validator = _load_validator()
        locked_count, verified_files = validator.validate_environment(
            REQUIREMENTS_PATH,
            LOCK_PATH,
            distribution_paths=package_paths,
            environment_prefix=environment_prefix,
        )
        _execute(list(sys.argv[1:] if arguments is None else arguments), package_paths)
    except Exception as exc:
        # Do not expose imported application values; boundary errors contain
        # only reviewed paths/policy text from this launcher and validator.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if list(sys.argv[1:] if arguments is None else arguments) == ["--validate-only"]:
        print(
            f"Validated {locked_count} locked application distributions, only the "
            f"explicit pip/setuptools toolchain allowance, and {verified_files} "
            "installed RECORD file hashes/sizes under the pre-site runner"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
