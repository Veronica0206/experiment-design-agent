#!/usr/bin/env python3
"""Adversarial regressions for pre-site venv bytecode sanitization."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SANITIZER_PATH = ROOT / "tools" / "sanitize_python_environment.py"
VALIDATOR_PATH = ROOT / "tools" / "validate_python_environment.py"
BOOTSTRAP_PATH = ROOT / "tools" / "bootstrap.sh"


def load_module(name: str, path: Path) -> object:
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification and specification.loader
    loaded = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = loaded
    specification.loader.exec_module(loaded)
    return loaded


sanitizer = load_module("sanitize_python_environment", SANITIZER_PATH)
validator = load_module("sanitize_test_python_environment_validator", VALIDATOR_PATH)


class FakeHash:
    def __init__(self, value: bytes) -> None:
        self.mode = "sha256"
        self.value = base64.urlsafe_b64encode(
            hashlib.sha256(value).digest()
        ).rstrip(b"=").decode("ascii")


class FakeFile:
    def __init__(self, path: str, value: bytes, *, hashed: bool = True) -> None:
        self.path = path
        self.hash = FakeHash(value) if hashed else None
        self.size = len(value) if hashed else None

    def __fspath__(self) -> str:
        return self.path

    def __str__(self) -> str:
        return self.path


class FakeDistribution:
    def __init__(self, root: Path, files: list[FakeFile]) -> None:
        self.root = root
        self.metadata = {"Name": "example"}
        self.version = "1.0"
        self.files = files

    def locate_file(self, path: object) -> Path:
        return self.root / str(path)


def wheel_style_distribution(root: Path) -> tuple[FakeDistribution, Path]:
    package_value = b"VALUE = 1\n"
    metadata_value = b"Name: example\nVersion: 1.0\n"
    bytecode_value = b"\x42\x0d\x0d\x0a wheel-shipped-bytecode"
    package_file = FakeFile("example/__init__.py", package_value)
    metadata_file = FakeFile("example-1.0.dist-info/METADATA", metadata_value)
    bytecode_file = FakeFile(
        "example/__pycache__/module.cpython-39.pyc", bytecode_value,
    )
    record_file = FakeFile(
        "example-1.0.dist-info/RECORD", b"record\n", hashed=False,
    )
    for relative, value in (
        (package_file, package_value),
        (metadata_file, metadata_value),
        (bytecode_file, bytecode_value),
        (record_file, b"record\n"),
    ):
        path = root / str(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return (
        FakeDistribution(
            root, [package_file, metadata_file, bytecode_file, record_file],
        ),
        root / str(bytecode_file),
    )


checks: dict[str, bool] = {}

with tempfile.TemporaryDirectory(prefix="expdesign-wheel-bytecode.") as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "lib" / "python3.9" / "site-packages"
    root.mkdir(parents=True)
    distribution, bytecode = wheel_style_distribution(root)
    removed_files, removed_directories, visited = sanitizer.sanitize_package_roots(
        prefix, [root],
    )
    validated = validator.validate_installed_distributions(
        {"example": "1.0"},
        distributions=[distribution],
        environment_prefix=prefix,
        toolchain_allowlist=frozenset(),
    )
    checks["record_declared_wheel_bytecode_is_removed_before_exact_validation"] = (
        removed_files == 1
        and removed_directories == 1
        and visited >= 5
        and not bytecode.exists()
        and not bytecode.parent.exists()
        and validated[0] == 1
    )

with tempfile.TemporaryDirectory(prefix="expdesign-sanitizer-symlink.") as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    root.mkdir(parents=True)
    bytecode = root / "a.pyc"
    bytecode.write_bytes(b"bytecode")
    outside = Path(directory) / "outside.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    (root / "z-linked.py").symlink_to(outside)
    try:
        sanitizer.sanitize_package_roots(prefix, [root])
        rejected_symlink = False
    except sanitizer.SanitizationError as exc:
        rejected_symlink = "symlinked site-packages entry is forbidden" in str(exc)
    checks["symlink_is_rejected_before_any_bytecode_is_removed"] = (
        rejected_symlink and bytecode.exists() and outside.exists()
    )

with tempfile.TemporaryDirectory(prefix="expdesign-sanitizer-escape.") as directory:
    base = Path(directory)
    prefix = base / "venv"
    prefix.mkdir()
    outside = base / "outside-site-packages"
    outside.mkdir()
    try:
        sanitizer.sanitize_package_roots(prefix, [outside])
        rejected_escape = False
    except sanitizer.SanitizationError as exc:
        rejected_escape = "escapes the selected venv" in str(exc)
    checks["package_root_escape_is_rejected"] = rejected_escape

with tempfile.TemporaryDirectory(prefix="expdesign-sanitizer-bound.") as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    root.mkdir(parents=True)
    first = root / "a.pyc"
    second = root / "b.pyo"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    try:
        sanitizer.sanitize_package_roots(prefix, [root], max_entries=1)
        rejected_bound = False
    except sanitizer.SanitizationError as exc:
        rejected_bound = "inspection exceeded its bound" in str(exc)
    checks["inspection_bound_is_fail_closed_before_removal"] = (
        rejected_bound and first.exists() and second.exists()
    )

with tempfile.TemporaryDirectory(prefix="expdesign-sanitizer-nonregular.") as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    root.mkdir(parents=True)
    nonregular = root / "payload.pyc"
    nonregular.mkdir()
    try:
        sanitizer.sanitize_package_roots(prefix, [root])
        rejected_nonregular = False
    except sanitizer.SanitizationError as exc:
        rejected_nonregular = "bytecode path is not a regular file" in str(exc)
    checks["nonregular_bytecode_path_is_rejected"] = (
        rejected_nonregular and nonregular.is_dir()
    )

with tempfile.TemporaryDirectory(prefix="expdesign-sanitizer-scope.") as directory:
    base = Path(directory)
    prefix = base / "venv"
    root = prefix / "site-packages"
    root.mkdir(parents=True)
    project_bytecode = base / "project-source" / "unsafe.pyc"
    project_bytecode.parent.mkdir()
    project_bytecode.write_bytes(b"project bytecode")
    cache = root / "example" / "__pycache__"
    cache.mkdir(parents=True)
    installed_bytecode = cache / "installed.pyc"
    installed_bytecode.write_bytes(b"installed bytecode")
    preserved = cache / "preserved.txt"
    preserved.write_text("not bytecode\n", encoding="utf-8")
    removed_files, removed_directories, _ = sanitizer.sanitize_package_roots(
        prefix, [root],
    )
    checks["cleanup_is_confined_and_removes_only_empty_cache_directories"] = (
        removed_files == 1
        and removed_directories == 0
        and not installed_bytecode.exists()
        and preserved.exists()
        and cache.exists()
        and project_bytecode.exists()
    )

with tempfile.TemporaryDirectory(prefix="expdesign-sanitizer-cli.") as directory:
    fixture = Path(directory)
    venv = fixture / "venv"
    created = subprocess.run(
        [
            sys.executable, "-E", "-s", "-S", "-B", "-m", "venv",
            "--without-pip", str(venv),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    site_packages = (
        venv / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    cache = site_packages / "example" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    bytecode = cache / "installed.pyc"
    bytecode.write_bytes(b"installed bytecode")
    marker = fixture / "sitecustomize-executed"
    (site_packages / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    executed = subprocess.run(
        [
            str(venv / "bin" / "python"), "-E", "-s", "-S", "-B", "-I",
            str(SANITIZER_PATH),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    checks["cli_runs_pre_site_with_selected_venv_and_removes_shipped_bytecode"] = (
        created.returncode == 0
        and executed.returncode == 0
        and "Removed 1 regular bytecode file(s)" in executed.stdout
        and not marker.exists()
        and not bytecode.exists()
        and not cache.exists()
    )
    if not checks["cli_runs_pre_site_with_selected_venv_and_removes_shipped_bytecode"]:
        print("venv creation stderr:", created.stderr)
        print("sanitizer stdout:", executed.stdout)
        print("sanitizer stderr:", executed.stderr)

bootstrap = BOOTSTRAP_PATH.read_text(encoding="utf-8")
sanitizer_call = bootstrap.find(
    '"$HARNESS_PYTHON" -E -s -S -B -I \\\n'
    "    tools/sanitize_python_environment.py"
)
reviewed_validation = bootstrap.find(
    "tools/run-reviewed-python.sh --validate-only", sanitizer_call,
)
checks["bootstrap_sanitizes_pre_site_before_reviewed_validation"] = (
    sanitizer_call >= 0
    and reviewed_validation > sanitizer_call
    and "pip install --no-compile --require-hashes" in bootstrap
)

with tempfile.TemporaryDirectory(prefix="expdesign-broken-venv.") as directory:
    broken_venv = Path(directory) / "broken-venv"
    (broken_venv / "bin").mkdir(parents=True)
    (broken_venv / "bin" / "python").symlink_to(sys.executable)
    (broken_venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\n",
        encoding="utf-8",
    )
    failed_bootstrap = subprocess.run(
        ["/bin/bash", str(BOOTSTRAP_PATH), "--check-python-lock"],
        cwd=ROOT,
        env={**os.environ, "EXPDESIGN_PYTHON": str(broken_venv / "bin" / "python")},
        capture_output=True,
        text=True,
        check=False,
    )
    failure_output = failed_bootstrap.stdout + failed_bootstrap.stderr
    checks["sanitizer_failure_prints_exact_isolated_setup_recipe"] = (
        failed_bootstrap.returncode != 0
        and str(broken_venv / "bin" / "python") in failure_output
        and "-E -s -S -B -I -m venv agent-harness/.venv" in failure_output
        and "agent-harness/.venv/bin/python -E -s -B -I -m pip install "
        "--no-compile --require-hashes -r agent-harness/requirements.lock"
        in failure_output
    )

for name, passed in checks.items():
    print(f"TEST {name} : {'PASS' if passed else 'FAIL'}")
raise SystemExit(1 if not all(checks.values()) else 0)
