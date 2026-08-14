#!/usr/bin/env python3
"""Regression tests for the installed-R versus renv.lock release gate."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "tools" / "validate_r_lock.R"
ENVIRONMENT_VALIDATOR = ROOT / "tools" / "validate_r_environment.py"
R_LAUNCHER = ROOT / "tools" / "run-reviewed-r.sh"
LOCK = json.loads((ROOT / "renv.lock").read_text(encoding="utf-8"))

spec = importlib.util.spec_from_file_location("validate_r_environment", ENVIRONMENT_VALIDATOR)
assert spec and spec.loader
environment_validator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = environment_validator
spec.loader.exec_module(environment_validator)


def run_with(lock: dict[str, object]) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "tools").mkdir()
        shutil.copy2(VALIDATOR, root / "tools" / "validate_r_lock.R")
        (root / "renv.lock").write_text(
            json.dumps(lock), encoding="utf-8",
        )
        return subprocess.run(
            ["/bin/sh", str(R_LAUNCHER), str(root / "tools" / "validate_r_lock.R")],
            capture_output=True, text=True, check=False,
        )


checks: dict[str, bool] = {}
valid = run_with(LOCK)
checks["matching_r_lock_passes"] = valid.returncode == 0

full_environment = subprocess.run(
    [sys.executable, "-E", "-s", "-B", str(ENVIRONMENT_VALIDATOR)],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=False,
)
checks["matching_r_tree_integrity_profile_passes"] = (
    full_environment.returncode == 0
    and "installed package-tree SHA-256 manifest" in full_environment.stdout
)

generated_environment = subprocess.run(
    [
        sys.executable, "-E", "-s", "-B", str(ENVIRONMENT_VALIDATOR),
        "--generate-manifest", "--acknowledge-reviewed-restore",
    ],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=False,
)
checks["reviewed_r_tree_manifest_is_current"] = (
    generated_environment.returncode == 0
    and json.loads(generated_environment.stdout)
    == json.loads((ROOT / "governance" / "r-package-integrity.json").read_text(
        encoding="utf-8"
    ))
)

unacknowledged_generation = subprocess.run(
    [
        sys.executable, "-E", "-s", "-B", str(ENVIRONMENT_VALIDATOR),
        "--generate-manifest",
    ],
    cwd=ROOT,
    capture_output=True,
    text=True,
    check=False,
)
checks["r_tree_manifest_generation_requires_restore_acknowledgement"] = (
    unacknowledged_generation.returncode != 0
    and "must be used together" in unacknowledged_generation.stderr
)

with tempfile.TemporaryDirectory() as directory:
    package_path = Path(directory) / "sameversion"
    (package_path / "R").mkdir(parents=True)
    (package_path / "DESCRIPTION").write_text(
        "Package: sameversion\nVersion: 1.0.0\n", encoding="utf-8",
    )
    behavior = package_path / "R" / "sameversion"
    behavior.write_text("result <- function() 'reviewed'\n", encoding="utf-8")
    package = environment_validator.InstalledPackage("1.0.0", "", package_path)
    inventory = environment_validator.RuntimeInventory(
        "4.5.3", "test-platform", "test-os", {"sameversion": package},
    )
    selected = {"sameversion": package}
    fixture_manifest = environment_validator.generated_manifest(
        inventory, selected, "0" * 64,
    )
    behavior.write_text("result <- function() 'tampered'\n", encoding="utf-8")
    try:
        environment_validator.validate_manifest(
            fixture_manifest, inventory, selected, "0" * 64,
        )
        checks["same_version_package_byte_tamper_fails_closed"] = False
    except environment_validator.ValidationError as exc:
        checks["same_version_package_byte_tamper_fails_closed"] = (
            "installed-tree bytes differ" in str(exc)
        )

with tempfile.TemporaryDirectory() as directory:
    package_path = Path(directory) / "symlinked"
    package_path.mkdir()
    outside = Path(directory) / "outside"
    outside.write_text("outside", encoding="utf-8")
    (package_path / "DESCRIPTION").write_text(
        "Package: symlinked\nVersion: 1.0.0\n", encoding="utf-8",
    )
    (package_path / "escape").symlink_to(outside)
    try:
        environment_validator.package_tree_record(package_path)
        checks["r_tree_integrity_rejects_symlinks"] = False
    except environment_validator.ValidationError as exc:
        checks["r_tree_integrity_rejects_symlinks"] = "contains a symlink" in str(exc)

runtime_drift = json.loads(json.dumps(LOCK))
runtime_drift["R"]["Version"] = "0.0.0"
result = run_with(runtime_drift)
checks["runtime_drift_fails_closed"] = (
    result.returncode != 0 and "R runtime does not match" in result.stderr
)

package_drift = json.loads(json.dumps(LOCK))
package_drift["Packages"]["jsonlite"]["Version"] = "0.0.0"
result = run_with(package_drift)
checks["required_package_drift_fails_closed"] = (
    result.returncode != 0 and "jsonlite does not match" in result.stderr
)

transitive_drift = json.loads(json.dumps(LOCK))
transitive_drift["Packages"]["Matrix"]["Version"] = "0.0.0"
result = run_with(transitive_drift)
checks["required_transitive_dependency_drift_fails_closed"] = (
    result.returncode != 0 and "Matrix does not match" in result.stderr
)

missing_transitive_record = json.loads(json.dumps(LOCK))
missing_transitive_record["Packages"].pop("Matrix")
result = run_with(missing_transitive_record)
checks["required_transitive_dependency_must_be_locked"] = (
    result.returncode != 0
    and "missing dependency Matrix required by survival" in result.stderr
)

missing_optional_record = json.loads(json.dumps(LOCK))
missing_optional_record["Packages"].pop("MAMS")
result = run_with(missing_optional_record)
checks["optional_branch_must_remain_locked"] = (
    result.returncode != 0 and "missing a version for MAMS" in result.stderr
)

relative_override = subprocess.run(
    ["/bin/sh", str(R_LAUNCHER), "--version"],
    env={**os.environ, "EXPDESIGN_RSCRIPT": "Rscript"},
    capture_output=True, text=True, check=False,
)
checks["relative_r_override_is_rejected"] = (
    relative_override.returncode != 0
    and "must be an absolute path" in relative_override.stderr
)

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    marker = root / "profile-executed"
    profile = root / "hostile-profile.R"
    profile.write_text(
        'writeLines("executed", Sys.getenv("EXPDESIGN_R_PROFILE_MARKER"))\n',
        encoding="utf-8",
    )
    clean_runtime = subprocess.run(
        ["/bin/sh", str(R_LAUNCHER), "-e", 'cat("clean\\n")'],
        env={
            **os.environ,
            "R_PROFILE_USER": str(profile),
            "EXPDESIGN_R_PROFILE_MARKER": str(marker),
        },
        capture_output=True, text=True, check=False,
    )
    checks["reviewed_r_ignores_ambient_profile"] = (
        clean_runtime.returncode == 0
        and clean_runtime.stdout == "clean\n"
        and not marker.exists()
    )

loader_environment = subprocess.run(
    [
        "/bin/sh", str(R_LAUNCHER), "-e",
        'cat(Sys.getenv("LD_EXPDESIGN_TEST"), "|", '
        'Sys.getenv("DYLD_EXPDESIGN_TEST"), "\\n", sep="")',
    ],
    env={
        **os.environ,
        "LD_EXPDESIGN_TEST": "must-not-reach-r",
        "DYLD_EXPDESIGN_TEST": "must-not-reach-r",
    },
    capture_output=True,
    text=True,
    check=False,
)
checks["reviewed_r_strips_all_loader_prefixed_variables"] = (
    loader_environment.returncode == 0 and loader_environment.stdout == "|\n"
)

failed = [name for name, passed in checks.items() if not passed]
for name, passed in checks.items():
    print(f"TEST {name} : {'PASS' if passed else 'FAIL'}")
raise SystemExit(1 if failed else 0)
