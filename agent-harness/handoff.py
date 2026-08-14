"""Host-side contracts for coordinator/domain-agent handoffs.

The model is never asked to synthesize verified numeric output.  A domain
executor returns a canonical report; this module binds that report to the
governed agent/run identity and performs deterministic fan-in.  The envelope
contains no raw arguments, paths, labels, strata, or row-level results.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

SUITE_ROOT = Path(__file__).resolve().parent.parent
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

from final_report import is_elicitation_message, join_reports
from governance.registry import agent_map, domain_phase
from verification import content_hash, public_limitation_codes


SCHEMA_VERSION = 1
SAFE_FAILURE_MESSAGE = "Verification failed; results withheld as not trustworthy."
PRESENTABLE_STATUSES = {"VERIFIED", "PASS_PARTIAL", "VALIDATED_CONFIG"}
ARTIFACT_HANDLE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _validated_artifact_uris(
    value: Any,
    analysis_ids: tuple[str, ...],
) -> tuple[str, ...]:
    """Require opaque artifact URIs bound to verified analysis identities."""
    if not isinstance(value, tuple):
        raise HandoffError("domain handoff artifact handles must be a tuple")

    allowed_analysis_ids = set(analysis_ids)
    for uri in value:
        if not isinstance(uri, str) or not uri:
            raise HandoffError("domain handoff artifact URI is missing")
        try:
            parsed = urlparse(uri)
        except ValueError as exc:
            raise HandoffError(
                "domain handoff artifact URI is malformed"
            ) from exc
        # Never normalize or decode the authority: the URI must bind byte-for-
        # byte to the runtime-issued analysis identity.
        analysis_id = parsed.netloc
        handle = parsed.path[1:] if parsed.path.startswith("/") else ""
        if (
            parsed.scheme != "expdesign-artifact"
            or analysis_id not in allowed_analysis_ids
            or parsed.path != f"/{handle}"
            or ARTIFACT_HANDLE.fullmatch(handle) is None
            or bool(parsed.params)
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise HandoffError("domain handoff artifact URI is not a verified opaque handle")
    if len(value) != len(set(value)):
        raise HandoffError("domain handoff reused an artifact URI")
    return value


def _bound_artifact_uris(value: Any, analysis_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Validate the public-safe portion of private artifact bundles.

    The full bundle remains host-only.  The handoff publishes only an opaque URI,
    and only when its authority is one of this result's verified analysis
    identities and its path is exactly one UUID handle.  Rejecting malformed
    bundles here keeps upstream implementation mistakes from becoming a path or
    query-string disclosure at the coordinator boundary.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise HandoffError("domain handoff private resources must be a list")

    uris: list[str] = []
    for bundle in value:
        if not isinstance(bundle, dict):
            raise HandoffError("domain handoff contains a malformed artifact bundle")
        resource = bundle.get("resource")
        uri = resource.get("uri") if isinstance(resource, dict) else None
        verification_id = bundle.get("verification_id")
        if not isinstance(uri, str) or not isinstance(verification_id, str):
            raise HandoffError("domain handoff artifact bundle lacks an identity-bound URI")

        try:
            analysis_id = urlparse(uri).netloc
        except ValueError as exc:
            raise HandoffError(
                "domain handoff artifact URI is malformed"
            ) from exc
        if verification_id != analysis_id:
            raise HandoffError("domain handoff artifact URI identity is inconsistent")
        uris.append(uri)
    return _validated_artifact_uris(tuple(uris), analysis_ids)


def phase_agents() -> tuple[set[str], set[str]]:
    """Derive phase membership from validated registry domains."""
    planning: set[str] = set()
    evidence: set[str] = set()
    for name, agent in agent_map().items():
        if agent["role"] != "domain_executor":
            continue
        phase = domain_phase(str(agent["domain"]))
        (planning if phase == "planning" else evidence).add(name)
    return planning, evidence


class HandoffError(ValueError):
    """Raised when an agent result cannot cross the coordinator boundary."""


@dataclass(frozen=True)
class HandoffEnvelope:
    orchestration_id: str
    task_id: str
    parent_task_id: str
    agent_name: str
    agent_instance_id: str
    domain: str
    status: str
    canonical_report: str
    report_hash: str
    analysis_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    artifact_handles: tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    @classmethod
    def from_run_result(
        cls,
        result: Any,
        *,
        orchestration_id: str,
        task_id: str,
        parent_task_id: str,
        expected_agent: str,
        expected_domain: str,
    ) -> "HandoffEnvelope":
        report = getattr(result, "final_answer", None)
        if not isinstance(report, str) or not report.strip():
            raise HandoffError("domain agent returned no canonical report")
        if getattr(result, "agent_name", None) != expected_agent:
            raise HandoffError("domain-agent identity does not match the dispatched task")
        if getattr(result, "domain", None) != expected_domain:
            raise HandoffError("domain-agent capability does not match the dispatched task")
        if report == SAFE_FAILURE_MESSAGE:
            raise HandoffError("domain agent reported a terminal verification failure")
        elicitation = is_elicitation_message(report)
        config_reports = list(
            getattr(result, "validated_configuration_reports", None) or []
        )
        config_report_is_bound = bool(
            config_reports
            and all(isinstance(item, str) and item.strip() for item in config_reports)
            and report == join_reports(config_reports)
        )
        if elicitation:
            status = "CLARIFICATION"
            records: list[dict[str, Any]] = []
        elif config_report_is_bound:
            status = "VALIDATED_CONFIG"
            records = []
        else:
            raw_bindings = getattr(result, "canonical_report_bindings", None)
            if not isinstance(raw_bindings, list) or not raw_bindings:
                raise HandoffError("domain handoff omitted canonical report bindings")
            records: list[dict[str, Any]] = []
            for item in raw_bindings:
                if not isinstance(item, dict) or set(item) != {
                    "analysis_id", "status", "report", "report_hash", "blocked",
                }:
                    raise HandoffError("canonical report binding is malformed")
                bound_report = item.get("report")
                if (not isinstance(bound_report, str) or not bound_report.strip()
                        or item.get("report_hash") != content_hash(bound_report)
                        or not isinstance(item.get("analysis_id"), str)
                        or not item["analysis_id"]
                        or item.get("status") not in {"VERIFIED", "PASS_PARTIAL"}
                        or not isinstance(item.get("blocked"), list)):
                    raise HandoffError("canonical report binding is invalid")
                records.append(item)
            if report != join_reports(item["report"] for item in records):
                raise HandoffError("final report does not match its canonical bindings")
            statuses = {str(item["status"]) for item in records}
            if not statuses <= {"VERIFIED", "PASS_PARTIAL"}:
                raise HandoffError("domain handoff contains a non-presentable status")
            status = "PASS_PARTIAL" if "PASS_PARTIAL" in statuses else "VERIFIED"

        analysis_ids = tuple(
            str(item["analysis_id"])
            for item in records
        )
        limitations = tuple(sorted({
            code
            for item in records
            for code in public_limitation_codes(list(item.get("blocked") or []))
        }))
        handles = _bound_artifact_uris(
            getattr(result, "private_resources", None), analysis_ids,
        )
        return cls(
            orchestration_id=orchestration_id,
            task_id=task_id,
            parent_task_id=parent_task_id,
            agent_name=expected_agent,
            agent_instance_id=str(getattr(result, "run_id", "")),
            domain=expected_domain,
            status=status,
            canonical_report=report,
            report_hash=content_hash(report),
            analysis_ids=analysis_ids,
            limitations=limitations,
            artifact_handles=handles,
        )

    def validate(self) -> None:
        strings = (
            self.orchestration_id, self.task_id, self.parent_task_id,
            self.agent_name, self.agent_instance_id, self.domain,
            self.status, self.canonical_report, self.report_hash,
        )
        if self.schema_version != SCHEMA_VERSION or not all(strings):
            raise HandoffError("domain handoff is incomplete")
        for label, values in (
            ("analysis identity", self.analysis_ids),
            ("limitation", self.limitations),
            ("artifact handle", self.artifact_handles),
        ):
            if (not isinstance(values, tuple)
                    or not all(isinstance(value, str) and value for value in values)
                    or len(values) != len(set(values))):
                raise HandoffError(f"domain handoff has invalid {label} values")
        if self.report_hash != content_hash(self.canonical_report):
            raise HandoffError("domain handoff report commitment does not match")
        if self.status not in PRESENTABLE_STATUSES | {"CLARIFICATION"}:
            raise HandoffError("domain handoff status is not allowed")
        _validated_artifact_uris(self.artifact_handles, self.analysis_ids)
        if self.status == "CLARIFICATION":
            if not is_elicitation_message(self.canonical_report) or self.analysis_ids:
                raise HandoffError("clarification handoff is malformed")
        elif self.status == "VALIDATED_CONFIG":
            if self.analysis_ids or self.artifact_handles:
                raise HandoffError("validated-configuration handoff is malformed")
        elif self.status in {"VERIFIED", "PASS_PARTIAL"} and not self.analysis_ids:
            raise HandoffError("verified handoff has no analysis identity")

    def public_summary(self) -> dict[str, Any]:
        """Return the fixed, model-safe handoff DTO."""
        self.validate()
        return {
            "schema_version": self.schema_version,
            "orchestration_id": self.orchestration_id,
            "task_id": self.task_id,
            "parent_task_id": self.parent_task_id,
            "agent_name": self.agent_name,
            "agent_instance_id": self.agent_instance_id,
            "domain": self.domain,
            "status": self.status,
            "analysis_ids": list(self.analysis_ids),
            "report_hash": self.report_hash,
            "canonical_report": self.canonical_report,
            "limitations": list(self.limitations),
            "artifact_handles": list(self.artifact_handles),
        }


@dataclass
class HandoffLedger:
    """Keep child outcomes isolated by logical task and retry attempt."""

    _by_task: dict[str, HandoffEnvelope] = field(default_factory=dict)

    def record(self, envelope: HandoffEnvelope) -> None:
        envelope.validate()
        prior = self._by_task.get(envelope.task_id)
        if prior is not None and prior.agent_name != envelope.agent_name:
            raise HandoffError("a task identity cannot move between domain agents")
        self._by_task[envelope.task_id] = envelope

    def all(self) -> list[HandoffEnvelope]:
        return list(self._by_task.values())


def aggregate_handoffs(
    envelopes: Iterable[HandoffEnvelope],
    canonical_agent_order: list[str],
) -> str:
    """Deterministically join presentable child reports without model prose."""
    items = list(envelopes)
    if not items:
        raise HandoffError("no domain handoff was provided")
    if len({item.task_id for item in items}) != len(items):
        raise HandoffError("duplicate task identity in coordinator fan-in")
    if len({item.agent_name for item in items}) != len(items):
        raise HandoffError("one domain agent was dispatched more than once")
    agent_names = {item.agent_name for item in items}
    planning_agents, evidence_agents = phase_agents()
    if agent_names & planning_agents and agent_names & evidence_agents:
        raise HandoffError(
            "evidence and planning handoffs require separate user turns"
        )
    if len({item.orchestration_id for item in items}) != 1:
        raise HandoffError("handoffs belong to different orchestrations")
    if len({item.parent_task_id for item in items}) != 1:
        raise HandoffError("handoffs belong to different parent tasks")
    for item in items:
        item.validate()
    analysis_ids = [value for item in items for value in item.analysis_ids]
    if len(analysis_ids) != len(set(analysis_ids)):
        raise HandoffError("analysis identity was reused across domain handoffs")
    artifact_handles = [value for item in items for value in item.artifact_handles]
    if len(artifact_handles) != len(set(artifact_handles)):
        raise HandoffError("artifact handle was reused across domain handoffs")
    clarifications = [item for item in items if item.status == "CLARIFICATION"]
    if clarifications:
        if len(items) != 1:
            raise HandoffError("clarification cannot be mixed with completed analyses")
        return clarifications[0].canonical_report
    if any(item.status not in PRESENTABLE_STATUSES for item in items):
        raise HandoffError("only presentable child results may be aggregated")
    rank = {name: index for index, name in enumerate(canonical_agent_order)}
    if any(item.agent_name not in rank for item in items):
        raise HandoffError("handoff came from a non-routable agent")
    ordered = sorted(items, key=lambda item: rank[item.agent_name])
    return join_reports(item.canonical_report for item in ordered)
