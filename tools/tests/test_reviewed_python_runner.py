#!/usr/bin/env python3
"""Behavioral regressions for the pre-site application Python boundary."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = ROOT / "tools" / "run-reviewed-python.sh"


def record_hash(value: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(value).digest()).rstrip(b"=")
    return digest.decode("ascii")


checks: dict[str, bool] = {
    "current_test_started_without_site_or_bytecode_writes": (
        sys.flags.no_site == 1
        and sys.flags.isolated == 1
        and sys.dont_write_bytecode
        and "site" not in sys.modules
    ),
}

with tempfile.TemporaryDirectory(prefix="expdesign-pth-runner.") as directory:
    fixture = Path(directory)
    venv = fixture / "venv"
    create_venv = subprocess.run(
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
    package = site_packages / "fixture"
    metadata = site_packages / "fixture-1.0.dist-info"
    package.mkdir(parents=True)
    metadata.mkdir()
    package_bytes = b"VALUE = 'reviewed'\n"
    metadata_bytes = b"Name: fixture\nVersion: 1.0\n"
    (package / "__init__.py").write_bytes(package_bytes)
    (metadata / "METADATA").write_bytes(metadata_bytes)
    (metadata / "RECORD").write_text(
        "fixture/__init__.py,sha256=" + record_hash(package_bytes)
        + f",{len(package_bytes)}\n"
        + "fixture-1.0.dist-info/METADATA,sha256=" + record_hash(metadata_bytes)
        + f",{len(metadata_bytes)}\n"
        + "fixture-1.0.dist-info/RECORD,,\n",
        encoding="utf-8",
    )
    pth_marker = fixture / "pth-executed"
    (site_packages / "escape.pth").write_text(
        "import os; open(" + repr(str(pth_marker))
        + ", 'w').write('executed'); os._exit(0)\n",
        encoding="utf-8",
    )
    attempted = subprocess.run(
        ["/bin/sh", str(LAUNCHER), "--validate-only"],
        cwd=ROOT,
        env={**os.environ, "EXPDESIGN_PYTHON": str(venv / "bin" / "python")},
        capture_output=True,
        text=True,
        check=False,
    )
    checks["unrecorded_pth_cannot_execute_before_validation"] = (
        create_venv.returncode == 0
        and attempted.returncode != 0
        and not pth_marker.exists()
        and "unrecorded package file" in attempted.stderr
        and "escape.pth" in attempted.stderr
    )
    if not checks["unrecorded_pth_cannot_execute_before_validation"]:
        print("PTH fixture creation stderr:", create_venv.stderr)
        print("PTH reviewed-runner stdout:", attempted.stdout)
        print("PTH reviewed-runner stderr:", attempted.stderr)

with tempfile.TemporaryDirectory(
    prefix=".reviewed-python-bytecode-", dir=ROOT,
) as directory:
    fixture = Path(directory)
    module_path = fixture / "poison.py"
    reviewed_source = b"VALUE = 'safe'\n"
    malicious_source = b"VALUE = 'evil'\n"
    module_path.write_bytes(malicious_source)
    py_compile.compile(str(module_path), doraise=True)
    malicious_cache = Path(importlib.util.cache_from_source(str(module_path)))
    source_mtime = module_path.stat().st_mtime_ns
    module_path.write_bytes(reviewed_source)
    os.utime(module_path, ns=(source_mtime, source_mtime))

    direct_probe = subprocess.run(
        [
            sys.executable, "-E", "-s", "-B", "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "import poison; print(poison.VALUE)",
            str(fixture),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    target_marker = fixture / "target-executed"
    target = fixture / "target.py"
    target.write_text(
        "from pathlib import Path\n"
        f"Path({str(target_marker)!r}).write_text('executed')\n"
        "import poison\n"
        "raise SystemExit(0 if poison.VALUE == 'safe' else 9)\n",
        encoding="utf-8",
    )
    reviewed_attempt = subprocess.run(
        ["/bin/sh", str(LAUNCHER), str(target)],
        cwd=ROOT,
        env={**os.environ, "EXPDESIGN_PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )
    checks["timestamp_valid_malicious_pyc_is_rejected_before_target"] = (
        malicious_cache.is_file()
        and direct_probe.returncode == 0
        and direct_probe.stdout.strip() == "evil"
        and reviewed_attempt.returncode != 0
        and not target_marker.exists()
        and "executable bytecode/cache artifact is forbidden" in reviewed_attempt.stderr
    )

with tempfile.TemporaryDirectory(
    prefix=".reviewed-python-symlink-dir-", dir=ROOT,
) as directory, tempfile.TemporaryDirectory(
    prefix="expdesign-external-source-",
) as external_directory:
    fixture = Path(directory)
    external = Path(external_directory)
    import_marker = external / "import-executed"
    target_marker = fixture / "target-executed"
    package = external / "linked_package"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    (fixture / "linked_package").symlink_to(package, target_is_directory=True)
    target = fixture / "target.py"
    target.write_text(
        "from pathlib import Path\n"
        f"Path({str(target_marker)!r}).write_text('executed')\n"
        "import linked_package\n",
        encoding="utf-8",
    )
    attempted = subprocess.run(
        ["/bin/sh", str(LAUNCHER), str(target)],
        cwd=ROOT,
        env={**os.environ, "EXPDESIGN_PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )
    checks["symlinked_source_directory_is_rejected_before_target"] = (
        attempted.returncode != 0
        and not target_marker.exists()
        and not import_marker.exists()
        and "symlinked project source entry is forbidden" in attempted.stderr
    )

with tempfile.TemporaryDirectory(
    prefix=".reviewed-python-symlink-file-", dir=ROOT,
) as directory, tempfile.TemporaryDirectory(
    prefix="expdesign-external-file-",
) as external_directory:
    fixture = Path(directory)
    external = Path(external_directory)
    import_marker = external / "import-executed"
    target_marker = fixture / "target-executed"
    external_module = external / "linked_module.py"
    external_module.write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('executed')\n",
        encoding="utf-8",
    )
    (fixture / "linked_module.py").symlink_to(external_module)
    target = fixture / "target.py"
    target.write_text(
        "from pathlib import Path\n"
        f"Path({str(target_marker)!r}).write_text('executed')\n"
        "import linked_module\n",
        encoding="utf-8",
    )
    attempted = subprocess.run(
        ["/bin/sh", str(LAUNCHER), str(target)],
        cwd=ROOT,
        env={**os.environ, "EXPDESIGN_PYTHON": sys.executable},
        capture_output=True,
        text=True,
        check=False,
    )
    checks["symlinked_source_file_is_rejected_before_target"] = (
        attempted.returncode != 0
        and not target_marker.exists()
        and not import_marker.exists()
        and "symlinked project source entry is forbidden" in attempted.stderr
    )

for name, passed in checks.items():
    print(f"TEST {name} : {'PASS' if passed else 'FAIL'}")
raise SystemExit(1 if not all(checks.values()) else 0)
