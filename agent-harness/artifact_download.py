"""Host-side verification for private artifact downloads.

Resource links are metadata, not authority to read an arbitrary local path.
Before bytes cross the UI boundary, bind the advertised file to the managed
artifact root and to the SHA-256 digest recorded in analysis provenance.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


SUITE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ARTIFACT_ROOT = SUITE_ROOT / "agent-harness" / "runs" / "artifacts"
PRIVATE_ARTIFACT_META_KEY = "experiment-design/private-artifact"
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
READ_CHUNK_BYTES = 1024 * 1024
ARTIFACT_HANDLE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class ArtifactVerificationError(ValueError):
    """Raised when a private resource cannot be safely offered for download."""


def managed_artifact_root() -> Path:
    configured = os.environ.get("EXPDESIGN_RUNS_DIR")
    return Path(configured) if configured else DEFAULT_ARTIFACT_ROOT


def _expected_hash(provenance: Any, name: str) -> str:
    if not isinstance(provenance, dict):
        raise ArtifactVerificationError("artifact provenance is missing")
    hashes = provenance.get("artifact_hashes")
    expected = hashes.get(name) if isinstance(hashes, dict) else None
    if not isinstance(expected, str) or len(expected) != 64:
        raise ArtifactVerificationError("artifact is not bound by provenance")
    normalized = expected.lower()
    if any(character not in "0123456789abcdef" for character in normalized):
        raise ArtifactVerificationError("artifact provenance digest is invalid")
    return normalized


def read_verified_artifact(
    bundle: dict[str, Any],
    managed_root: Path | None = None,
) -> tuple[str, bytes]:
    """Return ``(safe filename, bytes)`` for a provenance-bound resource.

    ``bundle`` is private host metadata produced by the harness and contains the
    ``resource`` plus client-only ``private_provenance``. Unknown paths, links,
    non-regular files, identity mismatches, and changed bytes fail closed.
    """
    if not isinstance(bundle, dict):
        raise ArtifactVerificationError("artifact metadata is invalid")
    resource = bundle.get("resource")
    if not isinstance(resource, dict):
        raise ArtifactVerificationError("artifact resource metadata is missing")
    uri_value = resource.get("uri")
    name_value = resource.get("name")
    if not isinstance(uri_value, str) or not isinstance(name_value, str):
        raise ArtifactVerificationError("artifact resource name or URI is missing")
    if not name_value or Path(name_value).name != name_value:
        raise ArtifactVerificationError("artifact filename is invalid")

    private_provenance = bundle.get("private_provenance")
    verification_id = bundle.get("verification_id")
    resource_meta = resource.get("_meta")
    artifact_meta = (
        resource_meta.get(PRIVATE_ARTIFACT_META_KEY)
        if isinstance(resource_meta, dict) else None
    )
    if not isinstance(artifact_meta, dict) or artifact_meta.get("version") != 2:
        raise ArtifactVerificationError("artifact client metadata is missing")
    handle = artifact_meta.get("handle")
    if not isinstance(handle, str) or not ARTIFACT_HANDLE.fullmatch(handle):
        raise ArtifactVerificationError("artifact handle is invalid")
    handle = handle.lower()

    parsed = urlparse(uri_value)
    if (
        parsed.scheme != "expdesign-artifact"
        or unquote(parsed.netloc) != verification_id
        or parsed.path != f"/{handle}"
        or bool(parsed.params)
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ArtifactVerificationError("artifact URI is not an identity-bound opaque resource")
    artifact_handles = (
        private_provenance.get("artifact_handles")
        if isinstance(private_provenance, dict) else None
    )
    raw_path_value = artifact_handles.get(handle) if isinstance(artifact_handles, dict) else None
    if not isinstance(raw_path_value, str):
        raise ArtifactVerificationError("artifact handle has no host-side path binding")
    raw_path = Path(raw_path_value)
    if not raw_path.is_absolute():
        raise ArtifactVerificationError("artifact host-side path is invalid")

    try:
        root_label = Path(os.path.abspath(managed_root or managed_artifact_root()))
        root = root_label.resolve(strict=True)
    except OSError as exc:
        raise ArtifactVerificationError("managed artifact root is unavailable") from exc
    relative: Path | None = None
    for prefix in dict.fromkeys((root_label, root)):
        try:
            relative = raw_path.relative_to(prefix)
            break
        except ValueError:
            continue
    if relative is None or not relative.parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ArtifactVerificationError("artifact is outside the managed root")
    if relative.parts[-1] != name_value:
        raise ArtifactVerificationError("artifact filename does not match its resource")

    annotations = resource.get("annotations")
    if not isinstance(annotations, dict) or annotations.get("audience") != ["user"]:
        raise ArtifactVerificationError("artifact resource is not user-scoped")
    expected = _expected_hash(private_provenance, name_value)
    resource_digest = artifact_meta.get("sha256")
    if (
        not isinstance(resource_digest, str)
        or not hmac.compare_digest(resource_digest.lower(), expected)
        or artifact_meta.get("analysis_id") != verification_id
        or not isinstance(verification_id, str)
        or not verification_id
        or not isinstance(private_provenance, dict)
        or private_provenance.get("analysis_id") != verification_id
        or private_provenance.get("version") != 2
    ):
        raise ArtifactVerificationError("artifact client metadata is not identity-bound")
    if not all(
        hasattr(os, flag) for flag in ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")
    ):
        raise ArtifactVerificationError("secure artifact traversal is unavailable")
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptors: list[int] = []
    try:
        directory_fd = os.open(root, directory_flags)
        descriptors.append(directory_fd)
        for component in relative.parts[:-1]:
            directory_fd = os.open(
                component, directory_flags, dir_fd=directory_fd,
            )
            descriptors.append(directory_fd)
            if not stat.S_ISDIR(os.fstat(directory_fd).st_mode):
                raise ArtifactVerificationError("artifact path component is not a directory")
        fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        descriptors.append(fd)
    except (OSError, ArtifactVerificationError) as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        if isinstance(exc, ArtifactVerificationError):
            raise
        raise ArtifactVerificationError("artifact path is unavailable or unsafe") from exc
    try:
        descriptor = os.fstat(fd)
        if not stat.S_ISREG(descriptor.st_mode):
            raise ArtifactVerificationError("artifact is not a regular file")
        if descriptor.st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactVerificationError(
                f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte download limit"
            )
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        bytes_read = 0
        while bytes_read < MAX_ARTIFACT_BYTES:
            chunk = os.read(
                fd,
                min(READ_CHUNK_BYTES, MAX_ARTIFACT_BYTES - bytes_read),
            )
            if not chunk:
                break
            bytes_read += len(chunk)
            digest.update(chunk)
            chunks.append(chunk)
        final_descriptor = os.fstat(fd)
        if final_descriptor.st_size > MAX_ARTIFACT_BYTES:
            raise ArtifactVerificationError(
                f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte download limit"
            )
    finally:
        for opened in reversed(descriptors):
            os.close(opened)

    if not hmac.compare_digest(digest.hexdigest(), expected):
        raise ArtifactVerificationError("artifact digest no longer matches provenance")
    return name_value, b"".join(chunks)
