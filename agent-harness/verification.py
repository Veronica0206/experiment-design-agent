"""Identity-safe verification records shared by every agent surface.

The statistical gates answer whether a result is scientifically acceptable.
This module answers the separate governance question: *which exact result* did
that verdict cover?  A verdict can only authorize presentation when the tool,
normalized arguments, and result hashes match the recorded envelope.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class VerificationStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PASS_PARTIAL = "PASS_PARTIAL"
    RETRY_REQUIRED = "RETRY_REQUIRED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    UNVERIFIED_ESCAPE = "UNVERIFIED_ESCAPE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


PRESENTABLE_STATUSES = {
    VerificationStatus.VERIFIED,
    VerificationStatus.PASS_PARTIAL,
}


PUBLIC_CHECK_CATEGORIES = frozenset({
    "artifact_integrity",
    "design_validation",
    "output_contract",
    "regression_suite",
    "reproducibility",
    "seed_disclosure",
    "scientific_validation",
})

PUBLIC_LIMITATION_MESSAGES = {
    "additional_manual_review_required": "An additional verifier limitation requires manual review.",
    "boundary_exactness_not_automated": "Boundary exactness was not automated.",
    "extended_config_consistency_not_automated": "Extended configuration consistency was not automated.",
    "oc_verification_incomplete": "Operating-characteristic verification was incomplete.",
    "orthogonality_not_automated": "Orthogonality was not automatically verified for this coding.",
    "power_verification_incomplete": "Power-versus-target verification was incomplete.",
    "same_seed_replay_not_run_for_large_simulation": "Same-seed replay was not run for a large simulation.",
    "study_effect_range_not_automated": "The pooled-effect range check could not run because per-study effects were unavailable.",
    "unsupported_configuration_requires_review": "An unsupported configuration requires review.",
}

_LIMITATION_EXACT = {
    "#5 boundary exactness (not automated)": "boundary_exactness_not_automated",
    "#6 config consistency beyond required-params/direction (not automated)": (
        "extended_config_consistency_not_automated"
    ),
    "orthogonality not checked (not a ±1-coded 2-level design)": (
        "orthogonality_not_automated"
    ),
    "study-effect range check not run because per-study effects were unavailable": (
        "study_effect_range_not_automated"
    ),
}


def public_check_summary(checks: dict[str, bool] | None) -> dict[str, bool]:
    """Collapse internal check names into a fixed, non-data-bearing vocabulary."""
    grouped: dict[str, list[bool]] = {}
    for raw_name, raw_value in (checks or {}).items():
        name = str(raw_name)
        if name in PUBLIC_CHECK_CATEGORIES:
            category = name
        elif name.startswith("tests:"):
            category = "regression_suite"
        elif name.startswith("artifact") or "output_dir" in name:
            category = "artifact_integrity"
        elif "reproduc" in name:
            category = "reproducibility"
        elif name == "seed_echoed":
            category = "seed_disclosure"
        elif name in {"nonempty_object", "no_tool_error", "no_nan_inf"}:
            category = "output_contract"
        elif name.startswith("has:") or name.endswith("_supported") or name in {
            "direction", "params_present", "requested_design_present",
        }:
            category = "design_validation"
        else:
            category = "scientific_validation"
        grouped.setdefault(category, []).append(raw_value is True)
    return {
        category: all(values)
        for category, values in sorted(grouped.items())
    }


def public_limitation_codes(items: list[str] | tuple[str, ...] | None) -> list[str]:
    codes: set[str] = set()
    for item in items or []:
        text = str(item)
        code = text if text in PUBLIC_LIMITATION_MESSAGES else _LIMITATION_EXACT.get(text)
        if code is None and text.startswith("same-seed replay skipped"):
            code = "same_seed_replay_not_run_for_large_simulation"
        elif code is None and ("OC curve unavailable" in text or "OC grid too coarse" in text):
            code = "oc_verification_incomplete"
        elif code is None and text.startswith("config-completeness: unrecognized"):
            code = "unsupported_configuration_requires_review"
        elif code is None and text.startswith("power-vs-target check not run"):
            code = "power_verification_incomplete"
        codes.add(code or "additional_manual_review_required")
    return sorted(codes)


def public_note_codes(items: list[str] | tuple[str, ...] | None) -> list[str]:
    """Expose warnings only as fixed codes; raw text may contain values or labels."""
    codes: set[str] = set()
    public_codes = {
        "additional_verification_note", "distribution_assumption_warning",
        "low_power_warning", "studies_dropped_warning",
    }
    for item in items or []:
        text = str(item)
        if text in public_codes:
            codes.add(text)
        elif text.startswith("p_go at the alternative is low"):
            codes.add("low_power_warning")
        elif "studies were dropped" in text:
            codes.add("studies_dropped_warning")
        elif "Poisson" in text or "overdispersion" in text:
            codes.add("distribution_assumption_warning")
        else:
            codes.add("additional_verification_note")
    return sorted(codes)


def public_failure_codes(checks: dict[str, bool] | None) -> list[str]:
    """Return value-free failure codes safe to expose across trust boundaries."""
    failed = sorted(
        name for name, value in public_check_summary(checks).items()
        if value is False
    )
    return [f"check_failed:{name}" for name in failed] or ["verification_failed"]


def _canonicalize(value: Any) -> Any:
    """Make arbitrary tool output hashable without normalizing bad numerics away."""
    if isinstance(value, float):
        if not math.isfinite(value):
            return {"__nonfinite_float__": repr(value)}
        # JSON has one number type. JavaScript parses 2 and 2.0 to the same
        # Number and serializes both as 2, while Python otherwise preserves the
        # lexical distinction. Normalize integral floats in the range where
        # JSON.stringify uses ordinary integer notation so host and MCP hashes
        # bind the same semantic arguments.
        if value.is_integer() and abs(value) < 1e21:
            return int(value)
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, set):
        return sorted((_canonicalize(item) for item in value), key=repr)
    return value


def _json_default(value: Any) -> dict[str, str]:
    return {"__python_object__": f"{type(value).__module__}.{type(value).__qualname__}",
            "value": str(value)}


def canonical_json(value: Any) -> str:
    """Stable JSON used for verification identities and audit provenance."""
    return json.dumps(
        _canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=_json_default,
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def domain_arguments(arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Remove governance-only fields before hashing statistical arguments."""
    cleaned = dict(arguments or {})
    cleaned.pop("verification_id", None)
    return cleaned


def identity_matches_call(
    identity: "VerificationIdentity | dict[str, Any]",
    tool: str,
    arguments: dict[str, Any] | None,
    result: Any,
    provenance: dict[str, Any] | None = None,
) -> bool:
    """Recompute the trust binding before a result crosses a presentation boundary."""
    data = asdict(identity) if isinstance(identity, VerificationIdentity) else identity
    return bool(
        isinstance(data, dict)
        and data.get("analysis_id")
        and data.get("tool") == tool
        and data.get("args_hash") == content_hash(domain_arguments(arguments))
        and data.get("result_hash") == content_hash(result)
        and bool(data.get("provenance_hash"))
        and data.get("provenance_hash") == content_hash(provenance or {})
    )


@dataclass(frozen=True)
class VerificationIdentity:
    analysis_id: str
    call_id: str
    tool: str
    args_hash: str
    result_hash: str
    provenance_hash: str = ""

    @classmethod
    def from_call(
        cls,
        tool: str,
        arguments: dict[str, Any] | None,
        result: Any,
        call_id: str,
        provenance: dict[str, Any] | None = None,
    ) -> "VerificationIdentity":
        args = dict(arguments or {})
        # The caller of this method must supply a runtime-issued ID in the
        # arguments.  Model-provided IDs are overwritten at the harness/server
        # boundary before this point.
        analysis_id = str(args.get("verification_id") or f"analysis-{call_id}")
        return cls(
            analysis_id=analysis_id,
            call_id=call_id,
            tool=tool,
            args_hash=content_hash(domain_arguments(args)),
            result_hash=content_hash(result),
            provenance_hash=content_hash(provenance or {}),
        )


def public_envelope_matches_call(
    envelope: dict[str, Any],
    tool: str,
    arguments: dict[str, Any] | None,
    public_result: Any,
    provenance: dict[str, Any] | None,
) -> bool:
    """Validate the safe MCP view without requiring model access to raw data."""
    identity = envelope.get("identity") if isinstance(envelope, dict) else None
    report = envelope.get("report") if isinstance(envelope, dict) else None
    return bool(
        isinstance(identity, dict)
        and identity.get("analysis_id")
        and "result_hash" not in identity
        and identity.get("tool") == tool
        and identity.get("args_hash") == content_hash(domain_arguments(arguments))
        and bool(identity.get("provenance_hash"))
        and identity.get("provenance_hash") == content_hash(provenance or {})
        and envelope.get("public_result_hash") == content_hash(public_result)
        and isinstance(report, str)
        and envelope.get("report_hash") == content_hash(report)
    )

@dataclass(frozen=True)
class PublicVerificationIdentity:
    """Identity fields safe to expose with an allowlisted public result.

    The raw result commitment remains verifier-internal.  Publishing it would
    provide an offline equality oracle for values deliberately removed from the
    public DTO.
    """

    analysis_id: str
    call_id: str
    tool: str
    args_hash: str
    provenance_hash: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PublicVerificationIdentity":
        if not isinstance(value, dict) or "result_hash" in value:
            raise ValueError("public verification identity is malformed")
        fields = ("analysis_id", "call_id", "tool", "args_hash", "provenance_hash")
        data = {field: value.get(field) for field in fields}
        if not all(isinstance(item, str) and item for item in data.values()):
            raise ValueError("public verification identity is incomplete")
        return cls(**data)


@dataclass
class VerificationEnvelope:
    identity: Any
    status: VerificationStatus
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    @property
    def presentable(self) -> bool:
        return self.status in PRESENTABLE_STATUSES

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["presentable"] = self.presentable
        return data

    def public_summary(self, report: str | None = None) -> dict[str, Any]:
        """A safe result substitute: verification metadata, never raw values."""
        failures = (["verification_failed"] if self.failures else []) if self.presentable \
            else public_failure_codes(self.checks)
        blocked = public_limitation_codes(self.blocked) if self.presentable else []
        notes = public_note_codes(self.notes) if self.presentable else []
        return {
            "verification_id": self.identity.analysis_id,
            "verification_status": self.status.value,
            "presentable": self.presentable,
            "failures": failures,
            "blocked": blocked,
            "notes": notes,
            **({"report": report} if self.presentable and report is not None else {}),
        }


class VerificationLedger:
    """Tracks envelopes without allowing unrelated calls to inherit trust."""

    def __init__(self) -> None:
        self._by_call: dict[str, VerificationEnvelope] = {}
        self._latest_by_lineage: dict[tuple[str, str], VerificationEnvelope] = {}
        self._tool_by_lineage: dict[str, str] = {}
        self._used_analysis_ids: dict[str, str] = {}

    def record(self, envelope: VerificationEnvelope, lineage_id: str | None = None) -> None:
        analysis_id = envelope.identity.analysis_id
        tool = envelope.identity.tool
        prior_call = self._used_analysis_ids.get(analysis_id)
        if prior_call is not None and prior_call != envelope.identity.call_id:
            raise ValueError(f"verification identity {analysis_id!r} was reused")
        self._used_analysis_ids[analysis_id] = envelope.identity.call_id
        lineage = lineage_id or analysis_id
        prior_tool = self._tool_by_lineage.get(lineage)
        if prior_tool is not None and prior_tool != tool:
            raise ValueError(
                f"verification lineage {lineage!r} is already bound to {prior_tool!r}"
            )
        self._tool_by_lineage[lineage] = tool
        self._by_call[envelope.identity.call_id] = envelope
        self._latest_by_lineage[(lineage, tool)] = envelope

    def for_call(self, call_id: str) -> VerificationEnvelope | None:
        return self._by_call.get(call_id)

    def latest_for_analysis(self, analysis_id: str, tool: str | None = None) -> VerificationEnvelope | None:
        for envelope in reversed(list(self._by_call.values())):
            if envelope.identity.analysis_id == analysis_id and (tool is None or envelope.identity.tool == tool):
                return envelope
        return None

    def unresolved(self) -> list[VerificationEnvelope]:
        return [
            envelope
            for envelope in self._latest_by_lineage.values()
            if not envelope.presentable
        ]

    def latest(self) -> list[VerificationEnvelope]:
        return list(self._latest_by_lineage.values())

    def all(self) -> list[VerificationEnvelope]:
        return list(self._by_call.values())


def envelope_from_verdict(
    identity: Any,
    verdict: Any,
    provenance: dict[str, Any] | None = None,
) -> VerificationEnvelope:
    if not bool(getattr(verdict, "passed", False)):
        status = VerificationStatus.FAILED
    elif getattr(verdict, "blocked", None):
        status = VerificationStatus.PASS_PARTIAL
    else:
        status = VerificationStatus.VERIFIED
    return VerificationEnvelope(
        identity=identity,
        status=status,
        checks=dict(getattr(verdict, "checks", {}) or {}),
        failures=list(getattr(verdict, "failures", []) or []),
        blocked=list(getattr(verdict, "blocked", []) or []),
        notes=list(getattr(verdict, "notes", []) or []),
        provenance=dict(provenance or {}),
    )
