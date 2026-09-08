#!/usr/bin/env python3
"""Prepare or verify an isolated public source candidate; never commit or push.

Only the reviewed exact-file catalog is copied. The inventory records bytes
for review and is deliberately not a publication-authorization manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_diff_credentials import PATTERN
from public_release_policy import PUBLIC_ENGINE_FILES, public_path_error

ROOT = Path(__file__).resolve().parent.parent
CATALOG = "governance/public-release-files.json"
INVENTORY = "release-inventory.json"
MAX_FILE_BYTES = 16 * 1024 * 1024
PUBLIC_AGENTS = frozenset({
    "experiment-designer", "design-verifier", "experiment-design-coordinator",
    "single-endpoint-designer",
})
GENERATED_ROOTS = {".git", "mcp-server/node_modules", "mcp-server/dist", "agent-harness/.venv"}


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def decode_json(data: bytes):
    return json.loads(data.decode("utf-8"), object_pairs_hook=strict_object)


def read_regular(root: Path, relative: str) -> bytes:
    error = public_path_error(relative)
    if error:
        raise ValueError(f"{relative}: {error}")
    current = root
    for part in Path(relative).parts:
        current = current / part
        if stat.S_ISLNK(current.lstat().st_mode):
            raise ValueError(f"symlink in selected public path: {relative}")
    metadata = current.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_FILE_BYTES:
        raise ValueError(f"unsupported public file: {relative}")
    content = current.read_bytes()
    if len(content) != metadata.st_size:
        raise ValueError(f"public source changed while reading: {relative}")
    if PATTERN.search(content):
        raise ValueError(f"credential pattern in selected public file: {relative}")
    # Reject actual workstation identity/path leakage without printing it.
    if str(Path.home()).encode() in content:
        raise ValueError(f"local account path in selected public file: {relative}")
    return content


def selected_files(root: Path) -> list[str]:
    data = decode_json(read_regular(root, CATALOG))
    if (not isinstance(data, dict) or set(data) != {"schema_version", "files"}
            or type(data["schema_version"]) is not int or data["schema_version"] != 1):
        raise ValueError("invalid public source catalog")
    names = data["files"]
    if (not isinstance(names, list) or not names
            or any(not isinstance(name, str) or public_path_error(name) for name in names)
            or names != sorted(set(names)) or CATALOG not in names
            or not PUBLIC_ENGINE_FILES.issubset(names)):
        raise ValueError("public source catalog must be sorted, unique, safe and complete")
    if INVENTORY in names or "publish-manifest.json" in names:
        raise ValueError("generated or authorization manifests cannot be copied as source")
    return names


def public_bytes(relative: str, content: bytes, source_root: Path) -> bytes:
    if relative == "governance/runtime-profiles.json":
        data = decode_json(content)
        if (data.get("schema_version") != 1 or data.get("active") not in {"complete", "single-endpoint"}
                or set(data.get("profiles", {})) != {"complete", "single-endpoint"}):
            raise ValueError("unsupported runtime profiles")
        data["active"] = "single-endpoint"
        return (json.dumps(data, indent=2) + "\n").encode()
    if relative == "governance/agents.json":
        data = decode_json(content)
        data["agents"] = [entry for entry in data["agents"] if entry["name"] in PUBLIC_AGENTS]
        if {entry["name"] for entry in data["agents"]} != PUBLIC_AGENTS:
            raise ValueError("public agent registry is incomplete")
        for entry in data["agents"]:
            if entry["role"] == "coordinator":
                entry["allowed_children"] = ["single-endpoint-designer"]
                entry["tools"] = ["Agent(single-endpoint-designer)"]
        return (json.dumps(data, indent=2) + "\n").encode()
    if relative == ".claude/agents/experiment-design-coordinator.md":
        text = content.decode()
        text, count = re.subn(r"(?m)^  - Agent\([^\n]+\)$", "  - Agent(single-endpoint-designer)", text)
        if count != 1:
            raise ValueError("coordinator tool declaration shape changed")
        routing = (
            "## Routing table\n\n"
            "- Single-endpoint validation, sample size, operating characteristics, or PPOS:\n"
            "  `single-endpoint-designer`.\n\n"
            "This public edition has no other domain agents. Never dispatch another\n"
            "agent or substitute a different method for an unsupported request.\n"
            "If the requested method is unsupported or cannot be selected safely,\n"
            "return only `CLARIFICATION_REQUEST {\"fields\":[\"analysis_method\"]}`.\n\n"
        )
        text, count = re.subn(r"## Routing table\n.*?(?=## Orchestration contract)", routing, text, flags=re.S)
        if count != 1:
            raise ValueError("coordinator routing section shape changed")
        return text.encode()
    if relative == ".claude/agents/experiment-designer.md":
        # The legacy entry uses the same public domain contract, retaining its
        # independently validated name, wildcard grant and native hook header.
        text = content.decode()
        header = text.split("---", 2)
        single = read_regular(source_root, ".claude/agents/single-endpoint-designer.md").decode().split("---", 2)
        if len(header) != 3 or len(single) != 3:
            raise ValueError("agent frontmatter shape changed")
        return ("---" + header[1] + "---" + single[2]).encode()
    return content


def verify(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("candidate root must be a real directory")
    inventory = decode_json(read_regular(root, INVENTORY))
    if (not isinstance(inventory, dict)
            or set(inventory) != {"schema_version", "status", "profile", "files"}
            or inventory["schema_version"] != 1
            or inventory["status"] != "prepared-not-published"
            or inventory["profile"] != "single-endpoint"
            or not isinstance(inventory["files"], dict)):
        raise ValueError("invalid prepared release inventory")
    expected = set(selected_files(root))
    if set(inventory["files"]) != expected:
        raise ValueError("release inventory differs from the exact public catalog")
    actual = set()
    for current, directories, files in os.walk(root, followlinks=False):
        parent = Path(current)
        for name in list(directories):
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError(f"symlinked candidate directory: {relative}")
            if relative in GENERATED_ROOTS:
                directories.remove(name)
        for name in files:
            relative = (parent / name).relative_to(root).as_posix()
            if relative not in {INVENTORY, "publish-manifest.json"}:
                actual.add(relative)
    if actual != expected:
        raise ValueError("unexpected or missing source files in public candidate")
    for name in sorted(expected):
        digest = hashlib.sha256(read_regular(root, name)).hexdigest()
        if digest != inventory["files"][name]:
            raise ValueError(f"prepared source bytes changed: {name}")
    profiles = decode_json(read_regular(root, "governance/runtime-profiles.json"))
    if profiles.get("active") != "single-endpoint":
        raise ValueError("public candidate must select the single-endpoint profile")
    registry = decode_json(read_regular(root, "governance/agents.json"))
    if {item["name"] for item in registry["agents"]} != PUBLIC_AGENTS:
        raise ValueError("public candidate advertises unavailable agents")
    publication = root / "publish-manifest.json"
    if publication.exists() or publication.is_symlink():
        # A published clone also contains the separately reviewed manifest.
        # Validate its complete byte inventory without manufacturing approval
        # or substituting for the publisher's independent operator SHA-256 pin.
        manifest = decode_json(read_regular(root, "publish-manifest.json"))
        authorization = manifest.get("authorization") if isinstance(manifest, dict) else None
        records = manifest.get("files") if isinstance(manifest, dict) else None
        if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 1
                or not isinstance(authorization, dict)
                or any(not isinstance(authorization.get(key), str) or not authorization[key].strip()
                       for key in ("approved_by", "approved_at", "purpose"))
                or not isinstance(records, dict) or set(records) != expected | {INVENTORY}):
            raise ValueError("invalid publication manifest accompanying the prepared inventory")
        for name, digest in records.items():
            if digest != hashlib.sha256(read_regular(root, name)).hexdigest():
                raise ValueError(f"publication manifest hash mismatch: {name}")
    return inventory


def prepare(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_dir() or not destination.is_absolute():
        raise ValueError("source must be a real directory and destination must be absolute")
    source = source.resolve()
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists; choose a new isolated directory")
    destination = destination.parent.resolve() / destination.name
    if destination == source or source in destination.parents:
        raise ValueError("public candidate must be outside the source working tree")
    names = selected_files(source)
    staging = Path(tempfile.mkdtemp(prefix=".public-candidate-", dir=destination.parent))
    try:
        digests = {}
        for name in names:
            content = public_bytes(name, read_regular(source, name), source)
            output = staging / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            output.chmod(0o755 if (source / name).stat().st_mode & 0o111 else 0o644)
            digests[name] = hashlib.sha256(content).hexdigest()
        (staging / INVENTORY).write_text(json.dumps({
            "schema_version": 1, "status": "prepared-not-published",
            "profile": "single-endpoint", "files": digests,
        }, indent=2) + "\n")
        verify(staging)
        staging.rename(destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    print(f"Prepared {len(names)} public source files: {destination}")
    print("This inventory records a local candidate; it does not authorize publication.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output", type=Path)
    group.add_argument("--check", type=Path)
    options = parser.parse_args()
    try:
        if options.check:
            inventory = verify(options.check)
            print(f"Public candidate inventory passed ({len(inventory['files'])} files)")
        else:
            prepare(options.source, options.output)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
