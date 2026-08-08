#!/usr/bin/env python3
"""Security-boundary tests for private artifact downloads."""

from __future__ import annotations

import hashlib
import os
import re
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

import artifact_download as artifact_module  # noqa: E402
from artifact_download import (ArtifactVerificationError,  # noqa: E402
                               MAX_ARTIFACT_BYTES,
                               read_verified_artifact)


passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {detail}")
        failed += 1


def rejected(bundle, root):
    try:
        read_verified_artifact(bundle, root)
    except ArtifactVerificationError:
        return True
    return False


def rejection_message(bundle, root):
    try:
        read_verified_artifact(bundle, root)
    except ArtifactVerificationError as exc:
        return str(exc)
    return ""


PRIVATE_ARTIFACT_META_KEY = "experiment-design/private-artifact"
HANDLE = "12345678-1234-4123-8123-123456789abc"


def resource_for(path, digest, analysis_id="analysis-1", name=None, handle=HANDLE):
    return {
        "type": "resource_link",
        "name": name or path.name,
        "uri": f"expdesign-artifact://{analysis_id}/{handle}",
        "annotations": {"audience": ["user"]},
        "_meta": {
            PRIVATE_ARTIFACT_META_KEY: {
                "version": 2,
                "analysis_id": analysis_id,
                "sha256": digest,
                "handle": handle,
            },
        },
    }


def private_provenance(name, digest, path, analysis_id="analysis-1", handle=HANDLE):
    return {
        "version": 2,
        "analysis_id": analysis_id,
        "input_hashes": {},
        "artifact_hashes": {name: digest},
        "artifact_handles": {handle: str(path)},
    }


typescript_limit_source = (
    Path(__file__).resolve().parents[2]
    / "mcp-server" / "src" / "artifact-publication.ts"
).read_text(encoding="utf-8")
typescript_limit_match = re.search(
    r"export const MAX_PUBLISHED_ARTIFACT_BYTES\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024",
    typescript_limit_source,
)
check(
    "cross_language_artifact_limits_match",
    typescript_limit_match is not None
    and int(typescript_limit_match.group(1)) * 1024 * 1024 == MAX_ARTIFACT_BYTES,
)


with tempfile.TemporaryDirectory() as directory:
    base = Path(directory)
    root = base / "managed"
    run = root / "analysis-1"
    run.mkdir(parents=True)
    artifact = run / "assignment.csv"
    artifact.write_bytes(b"unit,arm\n1,A\n")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    bundle = {
        "resource": resource_for(artifact, digest),
        "private_provenance": private_provenance(artifact.name, digest, artifact),
        "verification_id": "analysis-1",
    }

    name, data = read_verified_artifact(bundle, root)
    check("managed_provenance_bound_artifact_is_readable",
          name == artifact.name and data == artifact.read_bytes())

    outside = base / "outside.csv"
    outside.write_bytes(b"private\n")
    outside_bundle = {
        **bundle,
        "resource": resource_for(
            outside, hashlib.sha256(outside.read_bytes()).hexdigest(),
        ),
        "private_provenance": private_provenance(
            outside.name, hashlib.sha256(outside.read_bytes()).hexdigest(), outside,
        ),
    }
    check("outside_managed_root_is_rejected", rejected(outside_bundle, root))

    artifact.write_bytes(b"changed after verification\n")
    check("post_verification_mutation_is_rejected", rejected(bundle, root))
    artifact.write_bytes(b"unit,arm\n1,A\n")

    symlink = run / "linked.csv"
    symlink.symlink_to(artifact)
    symlink_bundle = {
        **bundle,
        "resource": resource_for(symlink, digest),
        "private_provenance": private_provenance(symlink.name, digest, symlink),
    }
    check("symlink_artifact_is_rejected", rejected(symlink_bundle, root))

    fifo = run / "artifact.fifo"
    os.mkfifo(fifo)
    fifo_bundle = {
        **bundle,
        "resource": resource_for(fifo, "0" * 64),
        "private_provenance": private_provenance(fifo.name, "0" * 64, fifo),
    }
    previous_handler = signal.getsignal(signal.SIGALRM)

    def fifo_timeout(_signum, _frame):
        raise TimeoutError("artifact FIFO open blocked")

    signal.signal(signal.SIGALRM, fifo_timeout)
    started = time.monotonic()
    fifo_rejected = False
    fifo_timed_out = False
    try:
        signal.setitimer(signal.ITIMER_REAL, 1.0)
        fifo_rejected = rejected(fifo_bundle, root)
    except TimeoutError:
        fifo_timed_out = True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
    check(
        "fifo_artifact_is_rejected_without_blocking",
        fifo_rejected and not fifo_timed_out and time.monotonic() - started < 1.0,
    )

    oversized = run / "oversized.bin"
    with oversized.open("wb") as handle:
        handle.truncate(MAX_ARTIFACT_BYTES + 1)
    oversized_bundle = {
        **bundle,
        "resource": resource_for(oversized, "0" * 64),
        "private_provenance": private_provenance(
            oversized.name, "0" * 64, oversized,
        ),
    }
    original_read = artifact_module.os.read
    oversized_read_attempted = False

    def tracking_read(fd, count):
        tracking_read.__dict__["attempted"] = True
        return original_read(fd, count)

    artifact_module.os.read = tracking_read
    try:
        oversized_rejection = rejection_message(oversized_bundle, root)
        oversized_read_attempted = bool(tracking_read.__dict__.get("attempted"))
    finally:
        artifact_module.os.read = original_read
    check(
        "oversized_artifact_is_rejected_before_read",
        oversized_rejection
        == f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte download limit"
        and not oversized_read_attempted,
    )

    linked_directory = root / "linked-directory"
    linked_directory.symlink_to(run, target_is_directory=True)
    linked_directory_bundle = {
        **bundle,
        "private_provenance": {
            **bundle["private_provenance"],
            "artifact_handles": {HANDLE: str(linked_directory / artifact.name)},
        },
    }
    check("intermediate_directory_symlink_is_rejected",
          rejected(linked_directory_bundle, root))

    original_open = artifact_module.os.open
    original_fstat = artifact_module.os.fstat
    original_close = artifact_module.os.close
    inspected_descriptor = None
    closed_descriptors = set()

    def inspection_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == run.name:
            inspection_open.__dict__["descriptor"] = descriptor
        return descriptor

    def failing_inspection(descriptor):
        if descriptor == inspection_open.__dict__.get("descriptor"):
            raise OSError("forced directory inspection failure")
        return original_fstat(descriptor)

    def tracking_close(descriptor):
        closed_descriptors.add(descriptor)
        return original_close(descriptor)

    artifact_module.os.open = inspection_open
    artifact_module.os.fstat = failing_inspection
    artifact_module.os.close = tracking_close
    try:
        inspection_rejected = rejected(bundle, root)
        inspected_descriptor = inspection_open.__dict__.get("descriptor")
    finally:
        artifact_module.os.open = original_open
        artifact_module.os.fstat = original_fstat
        artifact_module.os.close = original_close
    check(
        "failed_directory_inspection_closes_descriptor",
        inspection_rejected
        and inspected_descriptor is not None
        and inspected_descriptor in closed_descriptors,
    )

    race_run = root / "race-run"
    race_nested = race_run / "nested"
    race_nested.mkdir(parents=True)
    race_artifact = race_nested / "race.csv"
    race_bytes = b"verified race bytes\n"
    race_artifact.write_bytes(race_bytes)
    outside_directory = base / "race-outside"
    outside_directory.mkdir()
    (outside_directory / race_artifact.name).write_bytes(race_bytes)
    race_bundle = {
        "resource": resource_for(race_artifact, hashlib.sha256(race_bytes).hexdigest()),
        "private_provenance": private_provenance(
            race_artifact.name, hashlib.sha256(race_bytes).hexdigest(), race_artifact,
        ),
        "verification_id": "analysis-1",
    }
    original_open = artifact_module.os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal_swapped = racing_open.__dict__.setdefault("swapped", False)
        descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == race_run.name and not nonlocal_swapped:
            saved = race_run / "nested-original"
            race_nested.rename(saved)
            race_nested.symlink_to(outside_directory, target_is_directory=True)
            racing_open.__dict__["swapped"] = True
        return descriptor

    artifact_module.os.open = racing_open
    try:
        race_rejected = rejected(race_bundle, root)
        swapped = bool(racing_open.__dict__.get("swapped"))
    finally:
        artifact_module.os.open = original_open
    check("intermediate_symlink_swap_race_is_rejected", swapped and race_rejected)

    mismatch_bundle = {
        **bundle,
        "resource": resource_for(artifact, digest, name="different.csv"),
        "private_provenance": private_provenance(
            "different.csv", digest, artifact,
        ),
    }
    check("resource_name_mismatch_is_rejected", rejected(mismatch_bundle, root))

    query_bundle = {
        **bundle,
        "resource": {**bundle["resource"], "uri": bundle["resource"]["uri"] + "?download=1"},
    }
    check("file_uri_with_query_is_rejected", rejected(query_bundle, root))

    leaked_file_uri_bundle = {
        **bundle,
        "resource": {**bundle["resource"], "uri": artifact.as_uri()},
    }
    check("absolute_file_uri_is_rejected", rejected(leaked_file_uri_bundle, root))

    unbound_bundle = {
        **bundle,
        "private_provenance": {
            **bundle["private_provenance"], "artifact_hashes": {},
        },
    }
    check("missing_provenance_digest_is_rejected", rejected(unbound_bundle, root))

    missing_meta_bundle = {
        **bundle,
        "resource": {key: value for key, value in bundle["resource"].items()
                     if key != "_meta"},
    }
    check("missing_client_only_resource_digest_is_rejected",
          rejected(missing_meta_bundle, root))

    wrong_identity_bundle = {
        **bundle,
        "resource": resource_for(artifact, digest, analysis_id="analysis-other"),
    }
    check("resource_identity_mismatch_is_rejected",
          rejected(wrong_identity_bundle, root))

    model_audience_bundle = {
        **bundle,
        "resource": {
            **bundle["resource"], "annotations": {"audience": ["assistant"]},
        },
    }
    check("non_user_audience_is_rejected", rejected(model_audience_bundle, root))

    malformed_audience_bundle = {
        **bundle,
        "resource": {**bundle["resource"], "annotations": None},
    }
    check(
        "malformed_audience_metadata_fails_closed",
        rejected(malformed_audience_bundle, root),
    )


print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
