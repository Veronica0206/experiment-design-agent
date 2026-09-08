#!/usr/bin/env python3
"""Fail-closed verifier used by the MCP server before exposing R output."""

from __future__ import annotations

import json
import sys

from gates import (GateVerdict, MANUAL_CHECKS, check_regression_tests, check_reproducibility,
                   combined_gate, design_checks_for)
from final_report import canonical_report, privacy_safe_view
from verification import (VerificationIdentity, content_hash,
                          public_arguments_hash,
                          public_check_summary, public_failure_codes,
                          public_limitation_codes, public_note_codes)


_PUBLIC_PROVENANCE_STRING_FIELDS = {
    "engine_version", "engine_fingerprint", "node_version", "python_version",
    "python_runtime_fingerprint", "r_version", "r_runtime_fingerprint",
}
_PUBLIC_PROVENANCE_FIELDS = _PUBLIC_PROVENANCE_STRING_FIELDS | {
    "r_package_versions", "config_bound", "input_file_count", "artifact_count",
}

# The verifier deliberately carries both the structured public result and the
# canonical report. Bound each representation and their compact envelope before
# stdout so a scientifically valid but oversized result becomes a small,
# value-free verification failure rather than overflowing the downstream MCP
# frame. Node applies a second 4 MiB serialized tool-result boundary, and the
# Python client accepts a complete frame up to 10 MiB.
MAX_PUBLIC_RESULT_BYTES = 1 * 1024 * 1024
MAX_CANONICAL_REPORT_BYTES = 2 * 1024 * 1024
MAX_VERIFIER_PRESENTABLE_BYTES = 3 * 1024 * 1024


def _json_utf8_bytes(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _withhold_oversized_response(response: dict) -> None:
    """Replace a presentable envelope with one fixed, value-free failure."""
    for field in ("report", "public_result", "public_result_hash", "report_hash"):
        response.pop(field, None)
    response["status"] = "RETRY_REQUIRED"
    response["presentable"] = False
    response["checks"] = {**dict(response.get("checks") or {}),
                          "response_budget": False}
    response["failures"] = ["check_failed:response_budget"]
    response["blocked"] = []
    response["notes"] = []


def _presentable_payload_within_budget(
    public_result: object, report: str, prospective: dict,
) -> bool:
    return (
        len(_json_utf8_bytes(public_result)) <= MAX_PUBLIC_RESULT_BYTES
        and len(report.encode("utf-8")) <= MAX_CANONICAL_REPORT_BYTES
        and len(_json_utf8_bytes(prospective)) <= MAX_VERIFIER_PRESENTABLE_BYTES
    )


def validated_public_provenance(value: object) -> dict:
    """Accept only the fixed non-oracular provenance DTO bound to public output."""
    if not isinstance(value, dict) or set(value) != _PUBLIC_PROVENANCE_FIELDS:
        raise ValueError("public_provenance has an invalid schema")
    if any(not isinstance(value[field], str) or not value[field]
           for field in _PUBLIC_PROVENANCE_STRING_FIELDS):
        raise ValueError("public_provenance has an invalid runtime field")
    packages = value["r_package_versions"]
    if (not isinstance(packages, dict)
            or any(not isinstance(name, str) or not name
                   or (version is not None and not isinstance(version, str))
                   for name, version in packages.items())):
        raise ValueError("public_provenance has invalid package metadata")
    if value["config_bound"] is not True:
        raise ValueError("public_provenance is not configuration-bound")
    for field in ("input_file_count", "artifact_count"):
        count = value[field]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("public_provenance has an invalid count")
    return value


def main() -> int:
    request = json.load(sys.stdin)
    tool = str(request["tool"])
    args = request.get("args") or {}
    result = request.get("result")
    replay = request.get("replay")
    public_provenance = validated_public_provenance(
        request.get("public_provenance"),
    )
    regression = request.get("regression")
    runtime_id = str(request["runtime_id"])
    if not isinstance(result, dict):
        raise ValueError("tool result is not an object")
    verdicts = design_checks_for(tool, args, result)
    verdicts.append(check_regression_tests(regression))
    if request.get("replay_required"):
        if not isinstance(replay, dict):
            verdicts.append(GateVerdict(
                passed=False,
                failures=["server-side same-seed replay was required but missing"],
            ))
        else:
            verdicts.append(check_reproducibility(result, replay))
    extra_blocked = request.get("extra_blocked") or []
    if not isinstance(extra_blocked, list) or not all(isinstance(item, str) for item in extra_blocked):
        raise ValueError("extra_blocked must be a list of strings")
    if extra_blocked:
        verdicts.append(GateVerdict(passed=True, blocked=extra_blocked))
    verdicts.append(GateVerdict(passed=True, blocked=list(MANUAL_CHECKS)))
    verdict = combined_gate(*verdicts)
    identity_args = dict(args)
    identity_args["verification_id"] = runtime_id
    identity = VerificationIdentity.from_call(
        tool, identity_args, result, runtime_id.replace("analysis-", "call-", 1),
        provenance=public_provenance,
    )
    response = {
        "identity": {
            "analysis_id": identity.analysis_id,
            "call_id": identity.call_id,
            "tool": identity.tool,
            # The raw argument commitment remains inside this verifier.  Its
            # public replacement covers only the allowlisted argument DTO, so
            # private paths, labels, study rows, and strata do not become an
            # offline equality oracle.
            "public_args_hash": public_arguments_hash(tool, args),
            "result_hash": identity.result_hash,
            "provenance_hash": identity.provenance_hash,
        },
        "status": ("PASS_PARTIAL" if verdict.passed and verdict.blocked
                   else "VERIFIED" if verdict.passed
                   else "RETRY_REQUIRED"),
        "presentable": verdict.passed,
        "checks": public_check_summary(verdict.checks),
        "failures": [] if verdict.passed else public_failure_codes(verdict.checks),
        "blocked": public_limitation_codes(verdict.blocked) if verdict.passed else [],
        "notes": public_note_codes(verdict.notes) if verdict.passed else [],
    }
    if response["presentable"]:
        # Canonical rendering must verify the complete, verifier-local binding
        # before any private commitment is removed from the response DTO.
        # Never pass the public identity into this check: it intentionally does
        # not contain the raw argument or result commitments.
        private_rendering_envelope = {
            **response,
            "identity": {
                "analysis_id": identity.analysis_id,
                "call_id": identity.call_id,
                "tool": identity.tool,
                "args_hash": identity.args_hash,
                "result_hash": identity.result_hash,
                "provenance_hash": identity.provenance_hash,
            },
        }
        report = canonical_report(
            tool, args, result, private_rendering_envelope, public_provenance,
        )
        public_result = privacy_safe_view(tool, result)
        public_fields = {
            "report": report,
            "public_result": public_result,
            "public_result_hash": content_hash(public_result),
            "report_hash": content_hash(report),
        }
        prospective = {**response, **public_fields}
        if not _presentable_payload_within_budget(
            public_result, report, prospective,
        ):
            _withhold_oversized_response(response)
        else:
            response.update(public_fields)
    # Keep both raw commitments inside this verifier process. The public DTO
    # has its own commitments above; exposing either raw hash would turn every
    # dropped private value into an offline equality oracle.
    response["identity"].pop("result_hash", None)
    json.dump(response, sys.stdout, allow_nan=False, separators=(",", ":"))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # fail closed without returning the raw payload
        json.dump({"status": "INTERNAL_ERROR", "presentable": False,
                   "failures": ["verification_internal_error"]}, sys.stdout)
        raise SystemExit(1)
