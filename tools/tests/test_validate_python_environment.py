#!/usr/bin/env python3
"""Adversarial tests for exact installed-Python environment validation."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import tempfile
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "validate_python_environment.py"
spec = importlib.util.spec_from_file_location("validate_python_environment", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


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
    def __init__(
        self, root: Path, name: str, version: str, files: list[FakeFile]
    ) -> None:
        self.root = root
        self.metadata = {"Name": name}
        self.version = version
        self.files = files

    def locate_file(self, path: object) -> Path:
        return self.root / str(path)


def make_distribution(root: Path, name: str, version: str) -> FakeDistribution:
    package_file = FakeFile(f"{name}/__init__.py", b"VALUE = 1\n")
    metadata_file = FakeFile(
        f"{name}-{version}.dist-info/METADATA",
        f"Name: {name}\nVersion: {version}\n".encode(),
    )
    record_file = FakeFile(
        f"{name}-{version}.dist-info/RECORD", b"record\n", hashed=False,
    )
    files = [package_file, metadata_file, record_file]
    for file in files:
        path = root / str(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if file.hash is None:
            path.write_bytes(b"record\n")
        elif str(file).endswith("METADATA"):
            path.write_bytes(f"Name: {name}\nVersion: {version}\n".encode())
        else:
            path.write_bytes(b"VALUE = 1\n")
    return FakeDistribution(root, name, version, files)


checks: dict[str, bool] = {}


def lock_entry(name: str, marker: str = "") -> str:
    return f'{name}==1.0{marker} \\\n    --hash=sha256:{"0" * 64}\n'


def rejects_lock(text: str) -> bool:
    try:
        module.parse_hash_lock(text)
    except module.EnvironmentValidationError:
        return True
    return False


conditional = lock_entry("watchdog", '; platform_system != "Darwin"')
for host, expected in (
    ("Darwin", {"example": "1.0"}),
    ("Linux", {"example": "1.0", "watchdog": "1.0"}),
):
    with patch.object(module.platform, "system", return_value=host):
        checks[f"{host.lower()}_selects_exact_marked_requirements"] = (
            module.parse_hash_lock(lock_entry("example") + conditional) == expected
        )
        checks[f"{host.lower()}_rejects_duplicate_marked_names"] = rejects_lock(
            conditional + lock_entry("WATCHDOG")
        )
        checks[f"{host.lower()}_rejects_duplicate_inactive_markers"] = rejects_lock(
            conditional + conditional
        )
        checks[f"{host.lower()}_validates_marked_hashes"] = rejects_lock(
            lock_entry("example") + conditional.replace("0" * 64, "invalid")
        )
        checks[f"{host.lower()}_rejects_unhashed_marked_entry"] = rejects_lock(
            lock_entry("example") + conditional.split("\n", 1)[0] + "\n"
        )

for index, marker in enumerate((
    '; platform_system == "Darwin"',
    '; sys_platform != "darwin"',
    '; platform_system != "Darwin" or python_version > "0"',
    "; platform_system != 'Darwin'",
    ';platform_system!="Darwin"',
)):
    checks[f"unsupported_platform_marker_{index}_rejected"] = rejects_lock(
        lock_entry("example") + lock_entry("watchdog", marker)
    )

real_lock = (
    MODULE_PATH.parents[1] / "agent-harness" / "requirements.lock"
).read_text(encoding="utf-8")
with patch.object(module.platform, "system", return_value="Darwin"):
    darwin_pins = module.parse_hash_lock(real_lock)
with patch.object(module.platform, "system", return_value="Linux"):
    linux_pins = module.parse_hash_lock(real_lock)
checks["real_lock_keeps_51_darwin_requirements"] = (
    len(darwin_pins) == 51 and "watchdog" not in darwin_pins
)
checks["real_lock_adds_only_pinned_watchdog_on_linux"] = (
    len(linux_pins) == 52 and linux_pins == {**darwin_pins, "watchdog": "6.0.0"}
)

for host in ("Darwin", "Linux"):
    with patch.object(module.platform, "system", return_value=host):
        host_pins = module.parse_hash_lock(lock_entry("example") + conditional)
    with tempfile.TemporaryDirectory() as directory:
        prefix = Path(directory) / "venv"
        root = prefix / "site-packages"
        app = make_distribution(root, "example", "1.0")
        if host == "Linux":
            try:
                module.validate_installed_distributions(
                    host_pins, distributions=[app], environment_prefix=prefix,
                )
                checks["linux_requires_marked_distribution"] = False
            except module.EnvironmentValidationError as exc:
                checks["linux_requires_marked_distribution"] = (
                    "locked distribution(s) missing: watchdog" in str(exc)
                )
        watchdog = make_distribution(root, "watchdog", "1.0")
        try:
            result = module.validate_installed_distributions(
                host_pins, distributions=[app, watchdog], environment_prefix=prefix,
            )
            checks[f"{host.lower()}_marked_installation_enforced"] = (
                host == "Linux" and result[0] == 2
            )
        except module.EnvironmentValidationError as exc:
            checks[f"{host.lower()}_marked_installation_enforced"] = (
                host == "Darwin"
                and "unexpected installed distribution(s): watchdog" in str(exc)
            )
        if host == "Linux":
            watchdog.version = "2.0"
            try:
                module.validate_installed_distributions(
                    host_pins, distributions=[app, watchdog], environment_prefix=prefix,
                )
                checks["linux_marked_version_drift_rejected"] = False
            except module.EnvironmentValidationError as exc:
                checks["linux_marked_version_drift_rejected"] = (
                    "expected 1.0" in str(exc)
                )

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    pip = make_distribution(root, "pip", "99.0")
    result = module.validate_installed_distributions(
        {"example": "1.0"}, distributions=[app, pip], environment_prefix=prefix,
    )
    checks["exact_lock_plus_explicit_toolchain_passes"] = result[0] == 1

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    pth_bytes = b"import os; os._exit(99)\n"
    pth_file = FakeFile("reviewed.pth", pth_bytes)
    app.files.append(pth_file)
    (root / "reviewed.pth").write_bytes(pth_bytes)
    result = module.validate_installed_distributions(
        {"example": "1.0"}, distributions=[app], environment_prefix=prefix,
    )
    checks["recorded_pth_is_hash_bound_as_inert_data"] = result[0] == 1

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    extra = make_distribution(root, "injected", "1.0")
    try:
        module.validate_installed_distributions(
            {"example": "1.0"}, distributions=[app, extra],
            environment_prefix=prefix,
        )
        checks["unexpected_distribution_rejected"] = False
    except module.EnvironmentValidationError as exc:
        checks["unexpected_distribution_rejected"] = "unexpected installed" in str(exc)

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    (root / "example" / "__init__.py").write_text("TAMPERED = 1\n")
    try:
        module.validate_installed_distributions(
            {"example": "1.0"}, distributions=[app], environment_prefix=prefix,
        )
        checks["recorded_file_tamper_rejected"] = False
    except module.EnvironmentValidationError as exc:
        checks["recorded_file_tamper_rejected"] = (
            "differs from RECORD" in str(exc)
        )

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    (root / "example" / "injected.py").write_text("VALUE = 2\n")
    try:
        module.validate_installed_distributions(
            {"example": "1.0"}, distributions=[app], environment_prefix=prefix,
        )
        checks["unrecorded_package_file_rejected"] = False
    except module.EnvironmentValidationError as exc:
        checks["unrecorded_package_file_rejected"] = "unrecorded package file" in str(exc)

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    console_script = FakeFile("../bin/example", b"#!/bin/sh\n")
    app.files.append(console_script)
    script_path = root / str(console_script)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_bytes(b"#!/bin/sh\n")
    (prefix / "bin" / "injected-command").write_text("#!/bin/sh\n")
    try:
        module.validate_installed_distributions(
            {"example": "1.0"}, distributions=[app], environment_prefix=prefix,
        )
        checks["unrecorded_console_script_rejected"] = False
    except module.EnvironmentValidationError as exc:
        checks["unrecorded_console_script_rejected"] = "unrecorded package file" in str(exc)

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    package_file = root / "example" / "__init__.py"
    target = prefix / "replacement.py"
    target.write_text("VALUE = 1\n")
    package_file.unlink()
    package_file.symlink_to(target)
    try:
        module.validate_installed_distributions(
            {"example": "1.0"}, distributions=[app], environment_prefix=prefix,
        )
        checks["recorded_package_symlink_rejected"] = False
    except module.EnvironmentValidationError as exc:
        checks["recorded_package_symlink_rejected"] = "not a regular file" in str(exc)

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    app.files[0].hash = None
    app.files[0].size = None
    try:
        module.validate_installed_distributions(
            {"example": "1.0"}, distributions=[app], environment_prefix=prefix,
        )
        checks["unhashed_non_record_file_rejected"] = False
    except module.EnvironmentValidationError as exc:
        checks["unhashed_non_record_file_rejected"] = "unhashed installed file" in str(exc)

with tempfile.TemporaryDirectory() as directory:
    prefix = Path(directory) / "venv"
    root = prefix / "site-packages"
    app = make_distribution(root, "example", "1.0")
    try:
        module.validate_installed_distributions(
            {"example": "2.0"}, distributions=[app], environment_prefix=prefix,
        )
        checks["version_drift_rejected"] = False
    except module.EnvironmentValidationError as exc:
        checks["version_drift_rejected"] = "expected 2.0" in str(exc)

for name, passed in checks.items():
    print(f"TEST {name} : {'PASS' if passed else 'FAIL'}")
raise SystemExit(1 if not all(checks.values()) else 0)
