"""Experiment Design Agent Harness.

Orchestrates a gated, multi-turn workflow over the MCP tools:
  the agent elicits/configures across turns, runs an analysis tool, the
  harness runs an automated verification gate, and only then does the
  agent interpret. History persists across user turns on the instance.

Uses the Anthropic API for reasoning, the MCP server for R computations,
and an audit log for every decision.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator

SUITE_ROOT = Path(__file__).resolve().parent.parent
if str(SUITE_ROOT) not in sys.path:
    sys.path.insert(0, str(SUITE_ROOT))

import anthropic

from mcp_client import MCPClient
from audit import AuditLog
from final_report import canonical_public_report, is_elicitation_message, join_reports
from gates import (
    GateVerdict,
    MANUAL_CHECKS,
    check_regression_tests,
    combined_gate,
)
from verification import (
    VerificationLedger,
    PublicVerificationIdentity,
    content_hash,
    envelope_from_verdict,
    public_check_summary,
    public_envelope_matches_call,
    public_limitation_codes,
    public_note_codes,
)
from governance.registry import RegistryError, get_agent

MODEL = "claude-sonnet-5"
MAX_TOKENS = 8192
MAX_TOOL_ROUNDS = 20
VERIFY_RETRIES = 1          # how many times a failed gate may ask the agent to fix
REPRO_MAX_SIMS = 1000       # skip the same-seed reproducibility re-run above this
SAFE_FAILURE_MESSAGE = "Verification failed; results withheld as not trustworthy."

TOOL_PURPOSES = {
    "validate_config": "validate one single-endpoint configuration",
    "sample_size": "size one single-endpoint design",
    "simulate_design": "simulate one single-endpoint design",
    "master_simulate": "simulate one basket, umbrella, or platform design",
    "indirect_compare": "run one Bucher batch or one MAIC analysis",
    "meta_analyze": "pool compatible study effects",
    "ab_test": "size a two-arm A/B experiment",
    "factorial_design": "construct a factorial experiment",
    "rsm_design": "construct a response-surface experiment",
    "randomize": "create a seeded assignment plan",
    "run_tests": "run the fixed regression attestation",
}

DOMAIN_GUIDANCE = {
    "single_endpoint": (
        "Resolve endpoint direction explicitly. TTE requires alt < null; binary and "
        "continuous require alt > null; incidence supports either protective alt < null "
        "or harm-detection alt > null. Validate before expensive simulation."
    ),
    "master_protocol": (
        "Handle only basket, umbrella, platform, multi-arm, or multi-stage protocols."
    ),
    "doe": "Handle only A/B sizing, factorial screening, and response-surface construction.",
    "randomization": (
        "Handle only seeded assignment. Participant-level assignments remain private artifacts."
    ),
    "indirect_comparison": (
        "Handle one independent Bucher batch or one MAIC analysis; do not chain a network."
    ),
    "meta_analysis": (
        "Handle only fixed- or random-effects pooling on a common, validated effect scale."
    ),
}

# Verification is OPT-OUT: every MCP analysis tool that returns a numeric/design
# result must pass the gate. Only the pre-check (validate_config) and the gate
# itself (run_tests) are exempt. New tools are therefore gated by DEFAULT — they
# get regression + output-contract at minimum (plus any tool-specific check wired
# in _verify) instead of silently escaping verification.
UNGATED_TOOLS = {"validate_config", "run_tests"}
PUBLIC_MANUAL_CHECKS = frozenset(public_limitation_codes(MANUAL_CHECKS))


def _is_gated(tool_name: str) -> bool:
    return tool_name not in UNGATED_TOOLS


SYSTEM_PROMPT = """\
You are an experiment design agent. You help researchers plan quantitative \
studies by orchestrating an R framework through MCP tools.

## Available tools

Planning (how many units, what decision rule):
- validate_config: check parameters without running a simulation
- sample_size: sample sizes for single-endpoint designs
- simulate_design: full simulation (sample size + operating characteristics + optional PPOS)
- master_simulate: multi-arm adaptive designs (basket/umbrella/platform)
- indirect_compare: Bucher ITC or MAIC
- meta_analyze: fixed/random-effects meta-analysis

Construction (which runs to perform, how to assign units):
- ab_test: two-arm A/B sizing from a baseline + minimum detectable effect
- factorial_design: full 2^k or fractional 2^(k-p) factorial (resolution + aliasing)
- rsm_design: response-surface designs (central composite or Box-Behnken)
- randomize: seeded assignment plans (simple / block / stratified)

Verification:
- run_tests: regression suite (the harness runs this for you during verification)

## Workflow
1. ELICIT: If the request is underspecified, return only a structured clarification \
request in this exact form: CLARIFICATION_REQUEST {"fields":["endpoint_type"]}. \
Use one or more allowlisted missing field names and no other prose or values. It is \
correct to return this request without a tool call; the conversation continues on \
the next turn with full history.
2. RESOLVE THE ENDPOINT: If endpoint_type is not fixed, request it through the \
structured clarification action. Do not invent an efficiency ranking or numeric \
trade-off outside a verified tool result, and never silently substitute a different \
model for an unsupported endpoint.
3. CONFIGURE: Build a valid config and call validate_config before an expensive \
simulation. The runtime returns a canonical `configuration_report`; do not restate \
or reconstruct its resolved defaults in free-form prose.
4. EXECUTE: Run the appropriate analysis tool. Never choose a verification_id;
the runtime issues and binds it, including for corrections.
5. VERIFY: The harness runs an automated gate (regression suite + config \
completeness/direction + tool-specific design checks + power-separation / \
reproducibility / output-contract). Wait for its verdict. A `PASS_PARTIAL` \
verdict lists checks that could NOT be automated — relay those to the researcher.
6. REPORT: Once verification passes, return only the exact canonical report \
bound to the server-verified result.

## Endpoint framing (trade-offs, and what is NOT supported)
- binary responder: simplest to explain, but dichotomizing a continuous measurement \
throws away information (often ~1/3+ of effective N). Dichotomize only when the \
threshold is itself the meaningful event. A *reduction* endpoint must be modelled as \
the complementary *response* (higher = better).
- continuous: most efficient when a scale exists; needs a defensible sd (size a grid \
if uncertain). A repeated-measures trajectory reduces to a two-sample t-test at the \
primary timepoint (mildly conservative).
- incidence_rate: right for recurring counts, but the model is POISSON — real counts \
are usually overdispersed, so the N is an optimistic floor; flag it. Supports BOTH \
directions: protective (alt < null, success = rate reduction) and harm detection \
(alt > null, sized and analyzed in the upper tail — 'Go' = increase signal detected; \
state that framing explicitly).
- master-protocol incidence-rate simulation supports only protective alternatives \
(alt_params <= null_params); harm-direction master designs are rejected.
- tte: most efficient when first-event timing + censoring are real; power is driven by \
event count, not enrolled N. Assumes exponential/proportional-hazards, uniform accrual.
- NOT sized (say so, don't substitute silently): ordinal/proportional-odds, \
win-ratio/DOOR composites, co-primary gatekeeping, explicit MMRM correlation, \
negative-binomial rates, Cox-PH/non-PH TTE, non-inferiority/equivalence margins. \
Requesting negbin/cox_ph/holm is rejected (tool schema + config error + gate) — \
never offer them, and never label results with a method that did not run.

## Direction conventions (critical)
- TTE: lower hazard = better -> alt_param < null_param
- binary / continuous: higher response/mean = better -> alt_param > null_param
- incidence: EITHER direction is valid; the config's alt-vs-null side selects the \
tested tail (alt < null protective, alt > null harm detection)
- Go/No-Go uses posterior probabilities, not frequentist p-values
- Report the seed used.

## Rules
- Never write statistics code — all numbers come from the R tools.
- Never state a numeric result you did not obtain from a tool call this turn.
  Do not estimate or recall sample sizes / power / effects from memory — every
  number you report must trace to a verified tool output.
- If a tool returns an error, diagnose from the message and retry with fixed params.
- If verification fails and cannot be fixed, report that honestly; do not fabricate results.
- Never include participant identifiers, raw IPD, or sensitive strata labels in
  prose. File access is limited to MCP-approved roots.
- For a completed analysis, copy each `_verification.report` verbatim, in call
  order, separated by a Markdown horizontal rule, and add no other prose. The
  runtime replaces any free-form final wording with this canonical report.
- FAILED or escape reports must be exactly: Verification failed; results withheld as not trustworthy.
"""


@dataclass
class RunResult:
    run_id: str
    final_answer: str = ""
    gate_verdicts: list[str] = field(default_factory=list)
    audit_file: str = ""
    stopped: str = "end_turn"
    private_resources: list[dict[str, Any]] = field(default_factory=list)
    agent_name: str = "experiment-designer"
    domain: str = "all"
    verification_records: list[dict[str, Any]] = field(default_factory=list)
    validated_configuration_reports: list[str] = field(default_factory=list)
    canonical_report_bindings: list[dict[str, Any]] = field(default_factory=list)


def _build_tools(mcp_tools: list[dict]) -> list[dict]:
    return [{
        "name": t["name"],
        "description": t.get("description", ""),
        "input_schema": t.get("inputSchema", {"type": "object", "properties": {}}),
    } for t in mcp_tools]


class ExperimentDesignHarness:
    def __init__(
        self,
        anthropic_client: anthropic.Anthropic | None = None,
        log_dir: Path | None = None,
        model: str = MODEL,
        seed: int = 42,
        agent_name: str = "experiment-designer",
        system_prompt: str | None = None,
        mcp_client: MCPClient | None = None,
    ):
        self.client = anthropic_client or anthropic.Anthropic()
        self.model = model
        self.seed = seed
        try:
            self.agent_spec = get_agent(agent_name)
        except RegistryError as exc:
            raise ValueError(f"invalid governed agent selection: {exc}") from exc
        if self.agent_spec["role"] == "coordinator":
            raise ValueError(
                "the coordinator cannot run inside the MCP executor harness; "
                "use MultiAgentExperimentDesignHarness"
            )
        self.agent_name = agent_name
        self.domain = str(self.agent_spec["domain"])
        self.system_prompt = system_prompt or self._system_prompt_for_agent()
        self.log_dir = log_dir or SUITE_ROOT / "agent-harness" / "runs"
        self.mcp = mcp_client or MCPClient()
        self.messages: list[dict] = []      # persists across run() calls (multi-turn)
        self._tools: list[dict] = []
        configured_tools = self._allowed_mcp_tools()
        self._allowed_tool_names: set[str] = (
            set(TOOL_PURPOSES) if configured_tools is None else set(configured_tools)
        )
        self.run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.audit: AuditLog | None = None
        self._pending_retry_lineages_by_tool: dict[str, list[str]] = {}
        self._retry_failures_by_lineage: dict[str, int] = {}
        self._lineage_by_call: dict[str, str] = {}
        self._lineage_by_analysis: dict[str, str] = {}

    def _system_prompt_for_agent(self) -> str:
        """Return the shared governed contract with a role-specific boundary."""
        if self.agent_spec["role"] in {"legacy_executor", "reexecutor"}:
            return SYSTEM_PROMPT
        allowed = [
            name.removeprefix("mcp__experiment-design__")
            for name in self.agent_spec["tools"]
        ]
        descriptions = "\n".join(
            f"- {name}: {TOOL_PURPOSES[name]}" for name in allowed
        )
        guidance = DOMAIN_GUIDANCE.get(self.domain, "Stay inside the registered domain.")
        return f"""\
You are the governed {self.agent_name} domain executor for {self.domain}.
You have exactly these tools and no others:
{descriptions}

{guidance}

Workflow:
1. If the request is outside this domain or lacks required inputs, return only
   CLARIFICATION_REQUEST {{"fields":["analysis_method"]}}.
2. Call only an available domain tool. The host runs the regression attestation
   for every analysis and withholds any failed payload.
3. After a presentable result, return only its exact canonical verification
   report. Never calculate, reconstruct, paraphrase, or add numeric prose.

Rules:
- Never write statistics code or invent a value; all values come from the R tools.
- Never choose or alter verification_id. Never expose identifiers, raw IPD,
  sensitive labels or strata, host paths, or private artifact metadata.
- A partial result must retain every host-supplied limitation.
- On terminal verification failure return exactly: {SAFE_FAILURE_MESSAGE}
"""

    def _allowed_mcp_tools(self) -> set[str] | None:
        grants = set(self.agent_spec["tools"])
        if "mcp__experiment-design__*" in grants:
            return None
        prefix = "mcp__experiment-design__"
        return {
            grant[len(prefix):]
            for grant in grants
            if grant.startswith(prefix)
        }

    def start(self):
        try:
            self.mcp.start()
            available = self.mcp.list_tools()
            allowed = self._allowed_mcp_tools()
            if allowed is not None:
                by_name = {
                    str(item.get("name")): item
                    for item in available
                    if isinstance(item, dict) and isinstance(item.get("name"), str)
                }
                missing = sorted(allowed - set(by_name))
                if missing:
                    raise RuntimeError(
                        "governed agent tool grant is unavailable: " + ", ".join(missing)
                    )
                available = [by_name[name] for name in sorted(allowed)]
            self._tools = _build_tools(available)
            self._allowed_tool_names = {str(item["name"]) for item in self._tools}
            self.audit = AuditLog(self.log_dir, self.run_id)
        except BaseException as startup_error:
            # start() may fail after the MCP client has spawned a process but
            # before its initialize handshake returns. stop() is idempotent, so
            # always attempt cleanup even when mcp.start() itself raised.
            try:
                self.mcp.stop()
            except Exception:
                raise RuntimeError(
                    "experiment-design runtime startup cleanup did not complete"
                ) from startup_error
            raise

    def stop(self):
        try:
            self.mcp.stop()
        finally:
            if self.audit is not None:
                self.audit.close()

    def conversation_checkpoint(self) -> int:
        """Return an opaque rollback point for one coordinator child turn."""
        return len(self.messages)

    def rollback_conversation(self, checkpoint: int) -> None:
        """Discard every conversation block written after ``checkpoint``."""
        if (not isinstance(checkpoint, int) or isinstance(checkpoint, bool)
                or checkpoint < 0 or checkpoint > len(self.messages)):
            raise ValueError("invalid conversation checkpoint")
        del self.messages[checkpoint:]

    # ── seed injection (A-6): default the seed when the model omits one ──
    # NOTE: setdefault means a model-supplied seed WINS — the harness seed is a
    # default, not authoritative. Reproducibility re-runs are unaffected (the
    # same injected args are reused for the re-run either way).
    def _inject_seed(self, tool_name: str, tool_input: dict) -> dict:
        args = dict(tool_input)
        if tool_name == "simulate_design":
            if isinstance(args.get("config"), dict):
                args["config"] = dict(args["config"])   # don't alias the model's block (F-3)
            args.setdefault("seed", self.seed)
        elif tool_name == "master_simulate":
            cfg = dict(args.get("config") or {})
            cfg.setdefault("seed", self.seed)
            args["config"] = cfg
        elif tool_name == "randomize":
            args.setdefault("seed", self.seed)
        elif tool_name in ("factorial_design", "rsm_design") and args.get("randomize"):
            args.setdefault("seed", self.seed)
        return args

    def _prepare_args(self, tool_name: str, tool_input: dict, call_id: str) -> dict:
        args = self._inject_seed(tool_name, tool_input)
        if _is_gated(tool_name):
            # Every call receives a fresh immutable identity. A correction may
            # inherit an internal retry lineage only when exactly one failed
            # analysis of this tool is pending; ambiguous same-tool fan-out is
            # never guessed or silently cleared.
            args.pop("verification_id", None)
            if call_id in self._lineage_by_call:
                raise ValueError(f"duplicate tool call id in one turn: {call_id!r}")
            analysis_id = f"analysis-{uuid.uuid4().hex}"
            pending = self._pending_retry_lineages_by_tool.get(tool_name, [])
            lineage_id = pending[0] if len(pending) == 1 else f"lineage-{uuid.uuid4().hex}"
            self._lineage_by_call[call_id] = lineage_id
            self._lineage_by_analysis[analysis_id] = lineage_id
            args["verification_id"] = analysis_id
        return args

    def _exec_tool(self, tool_name: str, tool_input: dict) -> tuple[dict, float, str | None]:
        t0 = time.time()
        try:
            result = self.mcp.call_tool(tool_name, tool_input)
            error = result.get("error") if isinstance(result, dict) else None
        except Exception as e:                          # noqa: BLE001 - surfaced to model
            result = {"error": str(e)}
            error = str(e)
        return result, (time.time() - t0) * 1000, error

    def run(self, user_message: str) -> Generator[dict[str, Any], None, RunResult]:
        """Run one user turn of the gated workflow, yielding progress events.

        History persists on self.messages, so successive calls continue the
        same conversation (elicitation across turns works).
        """
        assert self.audit is not None, "call start() first"
        audit = self.audit
        audit.log("turn_start", metadata={"message": user_message})
        # Snapshot so a mid-turn failure rolls the conversation back to a valid
        # state instead of leaving a dangling user message (W-3).
        snapshot = len(self.messages)
        self.messages.append({"role": "user", "content": user_message})

        # Retry lineages are scoped to one autonomous model turn. A later user
        # turn is a new request and must never inherit/clear an unrelated failed
        # analysis merely because it uses the same tool.
        self._pending_retry_lineages_by_tool = {}
        self._retry_failures_by_lineage = {}
        self._lineage_by_call = {}
        self._lineage_by_analysis = {}

        gate_verdicts: list[str] = []
        ledger = VerificationLedger()
        reports_by_lineage: dict[str, str] = {}
        report_bindings_by_lineage: dict[str, dict[str, Any]] = {}
        resources_by_lineage: dict[str, list[dict[str, Any]]] = {}
        configuration_reports: list[str] = []
        max_gate_fails = VERIFY_RETRIES + 1

        def failure_result(reason: str, stopped: str = "verify_failed") -> RunResult:
            unresolved = ledger.unresolved()
            details = [f for env in unresolved for f in env.failures]
            audit.log(
                "answer_withheld_unverified",
                metadata={"reason": reason, "unresolved": [e.to_dict() for e in unresolved]},
            )
            # Retain only the user's request and a value-free failure summary.
            del self.messages[snapshot + 1:]
            self.messages.append({"role": "assistant", "content": [{
                "type": "text",
                "text": "[withheld] This turn produced an unverified result. "
                        "Its payload was discarded and must not be reconstructed.",
            }]})
            return RunResult(
                run_id=self.run_id,
                final_answer=SAFE_FAILURE_MESSAGE,
                gate_verdicts=gate_verdicts,
                audit_file=str(audit.log_file),
                stopped=stopped,
                agent_name=self.agent_name,
                domain=self.domain,
                verification_records=[item.to_dict() for item in ledger.all()],
            )

        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    system=self.system_prompt,
                    tools=self._tools,
                    messages=self.messages,
                )

                round_text: list[str] = []
                tool_calls: list[tuple[str, dict, dict, str]] = []  # (name, args, result, id)
                assistant_content: list[dict] = []
                refused_more_analysis = False

                for block in response.content:
                    if block.type == "text":
                        round_text.append(block.text)
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        if block.name not in self._allowed_tool_names:
                            raise ValueError(
                                "model requested a tool outside the governed allowlist"
                            )
                        args = self._prepare_args(block.name, block.input, block.id)
                        assistant_content.append({
                            "type": "tool_use", "id": block.id,
                            "name": block.name, "input": args,
                        })
                        yield {"event": "tool_call", "tool": block.name, "params": args}
                        analysis_id = str(args.get("verification_id", ""))
                        lineage_id = self._lineage_by_call.get(block.id, analysis_id)
                        if (_is_gated(block.name) and
                                self._retry_failures_by_lineage.get(lineage_id, 0) >= max_gate_fails):
                            refused_more_analysis = True
                            result, dur, err = ({"error": "verification retry budget exhausted; call not executed"}, 0.0,
                                                "verification retry budget exhausted")
                        else:
                            result, dur, err = self._exec_tool(block.name, args)
                        audit.log_tool_call(
                            block.name, args, result, dur, phase="Execute", error=err,
                        )
                        tool_calls.append((block.name, args, result, block.id))

                self.messages.append({"role": "assistant", "content": assistant_content})

                # No tools this round -> the agent responded or asked a question.
                # End the turn and hand control back to the user (covers end_turn,
                # max_tokens, refusal, etc. — no prefill spin, A-2/A-5).
                if not tool_calls:
                    if ledger.unresolved():
                        yield {"event": "done"}
                        return failure_result("FAILED")
                    latest = ledger.latest()
                    latest_reports = []
                    latest_report_bindings: list[dict[str, Any]] = []
                    latest_resources: list[dict[str, Any]] = []
                    for item in latest:
                        lineage = self._lineage_by_analysis.get(item.identity.analysis_id)
                        if item.presentable and lineage in reports_by_lineage:
                            latest_reports.append(reports_by_lineage[lineage])
                            binding = report_bindings_by_lineage.get(lineage)
                            if binding is not None:
                                latest_report_bindings.append(binding)
                            latest_resources.extend(resources_by_lineage.get(lineage, []))
                    candidate = "\n".join(round_text).strip()
                    if latest_reports:
                        final = join_reports(latest_reports)
                    elif configuration_reports:
                        final = "\n\n---\n\n".join(configuration_reports)
                    elif not is_elicitation_message(candidate):
                        yield {"event": "done"}
                        return failure_result("UNBOUND_FREE_TEXT")
                    else:
                        final = candidate
                    if response.stop_reason == "max_tokens" and not latest_reports:
                        audit.log("turn_truncated")
                        yield {"event": "done"}
                        return failure_result("TRUNCATED_ELICITATION")
                    self.messages[-1] = {"role": "assistant", "content": [
                        {"type": "text", "text": final}
                    ]}
                    yield {"event": "message", "content": final}
                    audit.log("turn_complete", metadata={
                        "answer_len": len(final),
                        "verification": [e.to_dict() for e in ledger.all()],
                    })
                    yield {"event": "done"}
                    return RunResult(
                        run_id=self.run_id,
                        final_answer=final,
                        gate_verdicts=gate_verdicts,
                        audit_file=str(audit.log_file),
                        stopped=response.stop_reason or "end_turn",
                        private_resources=latest_resources,
                        agent_name=self.agent_name,
                        domain=self.domain,
                        verification_records=[item.to_dict() for item in ledger.all()],
                        validated_configuration_reports=list(configuration_reports),
                        canonical_report_bindings=latest_report_bindings,
                    )

                if refused_more_analysis:
                    yield {"event": "done"}
                    return failure_result("RETRY_REQUIRED")

                analysis = [(n, a, r, tid) for (n, a, r, tid) in tool_calls if _is_gated(n)]
                safe_results: dict[str, dict] = {}
                round_failed = False

                # Framework health is evaluated once per analysis round, then
                # attached to every exact result envelope in that round.
                test_result = None
                if analysis:
                    yield {"event": "phase", "phase": "Verify"}
                    yield {"event": "tool_call", "tool": "run_tests", "params": {}}
                    test_result, dur, err = self._exec_tool("run_tests", {})
                    audit.log_tool_call(
                        "run_tests", {}, test_result, dur, phase="Verify", error=err,
                    )

                for name, args, result, tid in tool_calls:
                    if not _is_gated(name):
                        if name == "validate_config" and isinstance(result, dict):
                            report = result.get("configuration_report")
                            if (result.get("valid") is True and isinstance(report, str)
                                    and report.strip() and len(report) <= 64 * 1024):
                                configuration_reports.append(report)
                                safe_results[tid] = {"configuration_report": report}
                            else:
                                safe_results[tid] = result
                        else:
                            safe_results[tid] = result
                        yield {"event": "tool_result", "tool": name,
                               "result": safe_results[tid]}
                        continue

                    gate = yield from self._verify(name, args, result, audit, test_result)
                    server_envelope = (
                        result.get("_verification") or {}
                        if isinstance(result, dict) else {}
                    )
                    try:
                        identity_data = server_envelope.get("identity")
                        if not isinstance(identity_data, dict):
                            raise ValueError("verification identity is missing")
                        identity = PublicVerificationIdentity.from_dict(identity_data)
                    except (KeyError, TypeError, ValueError):
                        # A malformed server envelope cannot be assigned a
                        # trustworthy lineage, so do not enter the retry ledger
                        # or expose any part of the payload. End this turn with
                        # the same exact value-free failure contract used by all
                        # other fail-closed paths.
                        gate_verdicts.append("INTERNAL_ERROR")
                        audit.log_gate(
                            "Verify", "INTERNAL_ERROR",
                            metadata={"reason": "malformed_server_verification_envelope"},
                        )
                        yield {
                            "event": "gate", "tool": name,
                            "verification_id": None,
                            "verdict": "INTERNAL_ERROR", "checks": {},
                            "failures": ["verification_failed"],
                            "blocked": [], "notes": [],
                        }
                        yield {
                            "event": "tool_result_withheld", "tool": name,
                            "verification": {
                                "status": "INTERNAL_ERROR",
                                "presentable": False,
                                "failures": ["verification_failed"],
                                "blocked": [], "notes": [],
                            },
                        }
                        yield {"event": "done"}
                        return failure_result("INTERNAL_ERROR")
                    provenance = dict(result.get("_provenance") or {})
                    private_provenance = dict(result.get("_private_provenance") or {})
                    lineage_id = self._lineage_by_call.get(tid, identity.analysis_id)
                    # The server binds the raw result to a privacy-safe DTO. The
                    # harness's just-completed combined gate is authoritative for
                    # presentation, so never reconstruct status from an older
                    # server verdict.
                    envelope = envelope_from_verdict(identity, gate, provenance)
                    ledger.record(envelope, lineage_id=lineage_id)
                    gate_verdicts.append(envelope.status.value)
                    audit.log_gate(
                        "Verify",
                        envelope.status.value,
                        metadata={"envelope": envelope.to_dict()},
                    )
                    public_envelope = envelope.public_summary()
                    yield {
                        "event": "gate",
                        "tool": name,
                        "verification_id": identity.analysis_id,
                        "verdict": envelope.status.value,
                        "checks": public_check_summary(envelope.checks),
                        "failures": public_envelope["failures"],
                        "blocked": public_envelope["blocked"],
                        "notes": public_envelope["notes"],
                    }
                    if envelope.presentable:
                        pending = self._pending_retry_lineages_by_tool.get(name, [])
                        self._pending_retry_lineages_by_tool[name] = [
                            item for item in pending if item != lineage_id
                        ]
                        self._retry_failures_by_lineage.pop(lineage_id, None)
                        public_result = {
                            key: value for key, value in result.items()
                            if key not in {"_verification", "_provenance", "_private_provenance",
                                           "_private_resources"}
                        }
                        report = canonical_public_report(
                            name, args, public_result, envelope.to_dict(),
                            server_envelope, provenance,
                        )
                        reports_by_lineage[lineage_id] = report
                        report_bindings_by_lineage[lineage_id] = {
                            "analysis_id": identity.analysis_id,
                            "status": envelope.status.value,
                            "report": report,
                            "report_hash": content_hash(report),
                            "blocked": list(envelope.blocked),
                        }
                        resources_by_lineage[lineage_id] = [
                            {
                                "resource": dict(resource),
                                "private_provenance": private_provenance,
                                "verification_id": identity.analysis_id,
                            }
                            for resource in (result.get("_private_resources") or [])
                            if isinstance(resource, dict)
                        ]
                        summary = envelope.public_summary(report=report)
                        # Neither the model nor UI receives the raw result. The
                        # deterministic report is the only public representation;
                        # row-level assignments and labels remain private artifacts.
                        safe_results[tid] = {"_verification": summary}
                        yield {"event": "tool_result", "tool": name,
                               "result": {"report": report},
                               "verification": summary}
                    else:
                        resources_by_lineage.pop(lineage_id, None)
                        pending = self._pending_retry_lineages_by_tool.setdefault(name, [])
                        if lineage_id not in pending:
                            pending.append(lineage_id)
                        self._retry_failures_by_lineage[lineage_id] = (
                            self._retry_failures_by_lineage.get(lineage_id, 0) + 1
                        )
                        round_failed = True
                        safe_results[tid] = envelope.public_summary()
                        yield {"event": "tool_result_withheld", "tool": name,
                               "verification": envelope.public_summary()}

                self.messages.append({"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": tid,
                     "content": json.dumps(safe_results[tid], default=str)}
                    for (_n, _a, _r, tid) in tool_calls
                ]})

                if round_failed:
                    unresolved = ledger.unresolved()
                    retry_ids = [
                        self._lineage_by_analysis.get(e.identity.analysis_id, e.identity.analysis_id)
                        for e in unresolved
                    ]
                    can_retry = all(
                        self._retry_failures_by_lineage.get(
                            self._lineage_by_analysis.get(item.identity.analysis_id, item.identity.analysis_id), 0
                        ) < max_gate_fails
                        for item in unresolved
                    )
                    if can_retry:
                        self.messages.append({"role": "user", "content": (
                            "Verification FAILED. Raw outputs were withheld. Correct the "
                            "same analysis. The runtime will bind the correction to: "
                            f"{retry_ids}. Do not supply or alter verification_id, and do "
                            "not quote or reconstruct the failed values."
                        )})
                    else:
                        self.messages.append({"role": "user", "content": (
                            "Verification retries are exhausted. Do not call another "
                            "analysis tool. Return only a value-free failure explanation."
                        )})
                else:
                    summaries = [
                        e.public_summary(
                            report=reports_by_lineage.get(
                                self._lineage_by_analysis.get(e.identity.analysis_id, "")
                            )
                        ) for e in ledger.latest()
                    ]
                    self.messages.append({"role": "user", "content": (
                        "The exact results above are presentable. Copy each canonical "
                        f"_verification.report verbatim and add no prose: {summaries}"
                    )})

            audit.log_error("Max tool rounds exceeded")
            yield {"event": "done"}
            return failure_result("INTERNAL_ERROR", "max_rounds")

        except GeneratorExit:
            # A caller may cancel after any yielded progress event. GeneratorExit
            # is not an Exception, so handle it explicitly and restore the exact
            # pre-turn conversation before propagating cancellation without a
            # user-visible result.
            del self.messages[snapshot:]
            audit.log("turn_cancelled")
            raise
        except Exception as e:  # noqa: BLE001 - keep the conversation recoverable
            # Roll back this turn's messages so the next turn sends a valid
            # sequence (no dangling/duplicate user message) — W-3.
            del self.messages[snapshot:]
            audit.log_error(f"run failed: {e}")
            yield {"event": "error", "message": str(e)}
            return RunResult(
                run_id=self.run_id,
                final_answer="",
                gate_verdicts=gate_verdicts,
                audit_file=str(audit.log_file),
                stopped="error",
                agent_name=self.agent_name,
                domain=self.domain,
                verification_records=[item.to_dict() for item in ledger.all()],
            )

    def _verify(
        self, tool_name: str, args: dict, result: dict, audit: AuditLog,
        test_result: dict | None,
    ) -> Generator[dict, None, GateVerdict]:
        """Run the appropriate verification gate for the tool that just ran.

        Returns the verdict; the run() loop combines it with any earlier-in-round
        design checks and logs the COMBINED verdict to the audit (logging here
        would understate failures found by those sibling checks)."""
        if False:  # retain the generator contract used by the event loop
            yield {}
        verdicts: list[GateVerdict] = []
        server = result.get("_verification") if isinstance(result, dict) else None
        provenance = result.get("_provenance") if isinstance(result, dict) else None
        public_result = {
            key: value for key, value in result.items()
            if key not in {"_verification", "_provenance", "_private_provenance",
                           "_private_resources"}
        } if isinstance(result, dict) else None
        if not isinstance(server, dict) or not isinstance(public_result, dict):
            verdicts.append(GateVerdict(
                passed=False, failures=["trusted server verification envelope is missing"],
            ))
        else:
            checks = public_check_summary(dict(server.get("checks") or {}))
            failures = (["server verification reported failure"]
                        if server.get("failures") else [])
            blocked = public_limitation_codes(list(server.get("blocked") or []))
            missing = sorted(PUBLIC_MANUAL_CHECKS - set(blocked))
            bound = public_envelope_matches_call(
                server, tool_name, args, public_result,
                provenance if isinstance(provenance, dict) else {},
            )
            verdicts.append(GateVerdict(
                passed=(server.get("presentable") is True and bound and not failures
                        and not missing and all(value is not False for value in checks.values())),
                checks=checks,
                failures=(failures
                          + ([] if bound else ["server public-result/report binding failed"])
                          + (["server envelope omitted verifier limitations: "
                              + "; ".join(missing)] if missing else [])),
                blocked=blocked,
                notes=public_note_codes(list(server.get("notes") or [])),
            ))

        # Keep the fresh post-analysis health check last so its check values are
        # authoritative if a cached server attestation used the same names.
        verdicts.append(check_regression_tests(test_result or {}))
        return combined_gate(*verdicts)

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
