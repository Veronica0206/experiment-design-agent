"""Least-privilege coordinator for the experiment-design domain agents.

Routing is an LLM classification step with a closed structured-tool contract.
Statistical execution happens only inside independently stateful domain
``ExperimentDesignHarness`` instances whose MCP tools are filtered through the
governance registry.  Fan-in is deterministic and never model-generated.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Generator

import anthropic

from governance.registry import RegistryError, agent_map, get_agent
from handoff import (
    HandoffEnvelope,
    HandoffError,
    HandoffLedger,
    SAFE_FAILURE_MESSAGE,
    aggregate_handoffs,
    phase_agents,
)
from harness import ExperimentDesignHarness, MODEL, RunResult, SUITE_ROOT


ROUTER_TOOL = "route_experiment_design"
CLARIFICATION_FIELDS = (
    "endpoint_type", "study_type", "design", "estimand", "null_param",
    "alt_param", "sd", "alpha", "alpha_sidedness", "power",
    "allocation_ratio", "sample_size", "number_of_arms",
    "number_of_stages", "decision_threshold", "prior", "followup_time",
    "exposure_time", "randomization_method", "factor_levels",
    "analysis_method", "data_source",
)


def _router_prompt(children: list[str]) -> str:
    rules = {
        "single-endpoint-designer": "one-endpoint validation, power, sample size, OC, PPOS.",
        "master-protocol-designer": "basket, umbrella, platform, multi-arm or multi-stage.",
        "doe-designer": "A/B sizing, factorial screening, response-surface design.",
        "randomization-planner": "seeded simple, block, or stratified assignment only.",
        "indirect-comparison-analyst": "one Bucher comparison or one MAIC analysis.",
        "meta-analysis-analyst": "fixed- or random-effects pooling across studies.",
    }
    return f"""\
You are the routing-only coordinator for a governed experiment-design system.
You never calculate, interpret, or restate a statistical result. Return exactly
one `{ROUTER_TOOL}` tool call and no text.

Allowed domain agents, in canonical output order:
{chr(10).join(f'- {name}' for name in children)}

Routing rules:
{chr(10).join(f'- {name}: {rules[name]}' for name in children)}
Capabilities absent from this roster are unavailable in the installed edition.
For an unavailable request, clarify the intended supported analysis; never route
it to an unrelated specialist or pretend the missing capability is installed.

Prefer one agent. Select several only for explicitly independent deliverables.
Never route a dependent evidence-to-design pipeline in one turn: an indirect or
meta estimate may become a design assumption only in a later user turn that
explicitly confirms the exact estimate and intended design role. If routing is
ambiguous, choose action=clarify with the smallest allowlisted field list.
Never include private data, paths, labels, rows, strata, or numeric values in the
route decision; child agents receive the original user message from the host.
When routing or a domain agent required clarification, the host also includes
unseen verbatim user messages from that unresolved request, separately from the
current user reply. Completed requests remain in their own domain conversations
and are never copied to a newly selected domain agent.
"""


def _router_tool(children: list[str]) -> dict[str, Any]:
    return {
        "name": ROUTER_TOOL,
        "description": "Select governed domain agents or request structured clarification.",
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "action": {"type": "string", "enum": ["dispatch", "clarify"]},
                "agents": {
                    "type": "array", "uniqueItems": True, "maxItems": len(children),
                    "items": {"type": "string", "enum": children},
                },
                "clarification_fields": {
                    "type": "array", "uniqueItems": True, "maxItems": 8,
                    "items": {"type": "string", "enum": list(CLARIFICATION_FIELDS)},
                },
            },
            "required": ["action", "agents", "clarification_fields"],
        },
    }


class RouteError(ValueError):
    """Raised when the model violates the closed routing contract."""


class MultiAgentExperimentDesignHarness:
    """Hub-and-spoke harness with isolated domain conversations and MCP clients."""

    def __init__(
        self,
        anthropic_client: anthropic.Anthropic | None = None,
        log_dir: Path | None = None,
        model: str = MODEL,
        seed: int = 42,
    ) -> None:
        self.client = anthropic_client or anthropic.Anthropic()
        self.log_dir = log_dir or SUITE_ROOT / "agent-harness" / "runs"
        self.model = model
        self.seed = seed
        try:
            self.coordinator = get_agent("experiment-design-coordinator")
            self.registry = agent_map()
        except RegistryError as exc:
            raise ValueError(f"invalid multi-agent registry: {exc}") from exc
        self.children = list(self.coordinator["allowed_children"])
        if not self.children:
            raise ValueError("coordinator has no governed domain agents")
        self._router_messages: list[dict[str, Any]] = []
        # Only raw user messages from an unresolved clarification episode live
        # here. Track what each domain already received to avoid duplicate
        # history while allowing clarification to select a different domain.
        self._pending_user_messages: list[str] = []
        self._pending_messages_seen_by_agent: dict[str, int] = {}
        self._executors: dict[str, ExperimentDesignHarness] = {}
        self._started = False
        self.orchestration_id = f"orchestration-{uuid.uuid4().hex}"

    def start(self) -> None:
        # Child MCP runtimes are started lazily after a validated route. This
        # keeps an elicitation-only turn cheap and prevents unused capabilities
        # from receiving processes or state.
        self._started = True

    def stop(self) -> None:
        failed: dict[str, ExperimentDesignHarness] = {}
        for name, executor in list(self._executors.items()):
            try:
                executor.stop()
            except Exception:
                # Continue through the roster so one broken child cannot leave
                # every later MCP/R runtime alive. Retain only failed handles,
                # allowing the UI or caller to retry cleanup deterministically.
                failed[name] = executor
        self._executors = failed
        # A later start creates fresh conversations for retired handles. Those
        # domains must receive the unresolved request again when dispatched.
        self._pending_messages_seen_by_agent = {
            name: count for name, count in self._pending_messages_seen_by_agent.items()
            if name in failed
        }
        self._started = False
        if failed:
            raise RuntimeError("one or more domain runtimes failed to stop")

    def _executor(self, agent_name: str) -> ExperimentDesignHarness:
        if agent_name not in self.children:
            raise RouteError("router selected a non-child agent")
        executor = self._executors.get(agent_name)
        if executor is None:
            executor = ExperimentDesignHarness(
                anthropic_client=self.client,
                log_dir=self.log_dir,
                model=self.model,
                seed=self.seed,
                agent_name=agent_name,
            )
            # Publish the handle before startup. If initialization or its
            # cleanup fails, team.stop() still has a handle it can retry.
            self._executors[agent_name] = executor
            try:
                executor.start()
            except Exception:
                try:
                    executor.stop()
                except Exception as exc:
                    raise RuntimeError(
                        "domain runtime startup cleanup failed"
                    ) from exc
                self._executors.pop(agent_name, None)
                raise
        return executor

    @staticmethod
    def _tool_blocks(response: Any) -> list[Any]:
        content = getattr(response, "content", None)
        if not isinstance(content, list):
            raise RouteError("router returned no content blocks")
        text_blocks = [item for item in content if getattr(item, "type", None) == "text"]
        tool_blocks = [item for item in content if getattr(item, "type", None) == "tool_use"]
        if text_blocks or len(tool_blocks) != 1:
            raise RouteError("router must return exactly one tool call and no prose")
        if getattr(tool_blocks[0], "name", None) != ROUTER_TOOL:
            raise RouteError("router called an unapproved tool")
        return tool_blocks

    @staticmethod
    def _value_free_child_event(
        event: Any, allowed_tools: set[str],
    ) -> dict[str, Any] | None:
        """Project a child event to progress-only data before atomic fan-in.

        Child reports, tool arguments/results, analysis identities, paths, and
        diagnostics must not cross the coordinator boundary until every child
        handoff has passed. The final aggregate message is the sole
        content-bearing publication.
        """
        if not isinstance(event, dict):
            return None
        event_type = event.get("event")
        if event_type == "phase":
            phase = event.get("phase")
            if phase in {"Elicit", "Configure", "Execute", "Verify"}:
                return {"event": "phase", "phase": phase}
            return None
        if event_type in {
            "tool_call", "tool_result", "tool_result_withheld", "tool_request_rejected",
        }:
            tool = event.get("tool")
            if tool not in allowed_tools:
                return None
            projected: dict[str, Any] = {"event": event_type, "tool": tool}
            if event_type == "tool_result_withheld":
                verification = event.get("verification")
                status = verification.get("status") if isinstance(verification, dict) else None
                if status not in {"FAILED", "INTERNAL_ERROR"}:
                    status = "FAILED"
                projected["verification"] = {"status": status}
            return projected
        if event_type == "gate":
            tool = event.get("tool")
            verdict = event.get("verdict")
            if tool not in allowed_tools or verdict not in {
                "VERIFIED", "PASS_PARTIAL", "FAILED", "INTERNAL_ERROR",
            }:
                return None
            return {
                "event": "gate", "tool": tool, "verdict": verdict,
                "checks": {}, "failures": [], "blocked": [],
            }
        if event_type in {"error", "unverified_message"}:
            return {"event": event_type}
        # Child `message`, `done`, and unknown events are intentionally dropped.
        return None

    @staticmethod
    def _child_checkpoint(child: Any) -> int | None:
        checkpoint = getattr(child, "conversation_checkpoint", None)
        if not callable(checkpoint):
            return None
        value = checkpoint()
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("domain runtime returned an invalid conversation checkpoint")
        return value

    @staticmethod
    def _rollback_children(checkpoints: list[tuple[Any, int]]) -> None:
        """Best-effort rollback of every child touched by the current turn."""
        for child, checkpoint in reversed(checkpoints):
            rollback = getattr(child, "rollback_conversation", None)
            if not callable(rollback):
                continue
            try:
                rollback(checkpoint)
            except Exception:
                # The published result still fails closed. A broken executor is
                # retained by the team lifecycle so stop() can retire it later.
                continue

    def _route(self, user_message: str) -> tuple[str, list[str], list[str]]:
        self._router_messages.append({"role": "user", "content": user_message})
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_router_prompt(self.children),
                tools=[_router_tool(self.children)],
                tool_choice={"type": "tool", "name": ROUTER_TOOL},
                messages=self._router_messages,
            )
            block = self._tool_blocks(response)[0]
            value = getattr(block, "input", None)
            if not isinstance(value, dict) or set(value) != {
                "action", "agents", "clarification_fields",
            }:
                raise RouteError("router output has an invalid schema")
            action = value.get("action")
            agents = value.get("agents")
            fields = value.get("clarification_fields")
            if (not isinstance(agents, list) or not isinstance(fields, list)
                    or not all(isinstance(item, str) for item in agents + fields)
                    or len(agents) != len(set(agents))
                    or len(fields) != len(set(fields))):
                raise RouteError("router lists are malformed")
            if action == "dispatch":
                if not agents or fields or any(name not in self.children for name in agents):
                    raise RouteError("dispatch route is malformed")
                selected = set(agents)
                planning_agents, evidence_agents = phase_agents()
                if selected & planning_agents and selected & evidence_agents:
                    raise RouteError(
                        "evidence and planning work require separate user turns"
                    )
                agents = sorted(agents, key=self.children.index)
            elif action == "clarify":
                if agents or not 1 <= len(fields) <= 8 or any(
                    field not in CLARIFICATION_FIELDS for field in fields
                ):
                    raise RouteError("clarification route is malformed")
            else:
                raise RouteError("router action is unsupported")
            tool_id = str(getattr(block, "id", "route"))
            self._router_messages.extend([
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": tool_id, "name": ROUTER_TOOL,
                    "input": value,
                }]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": tool_id,
                    "content": "route accepted by host policy",
                }]},
            ])
            return str(action), list(agents), list(fields)
        except Exception:
            # Do not retain a half-turn that would make the next Anthropic call
            # invalid. The caller receives only the value-free failure contract.
            if self._router_messages and self._router_messages[-1].get("role") == "user":
                self._router_messages.pop()
            raise

    def run(self, user_message: str) -> Generator[dict[str, Any], None, RunResult]:
        if not self._started:
            raise RuntimeError("call start() before run()")
        parent_task_id = f"turn-{uuid.uuid4().hex}"
        router_checkpoint = len(self._router_messages)
        pending_checkpoint = list(self._pending_user_messages)
        seen_checkpoint = dict(self._pending_messages_seen_by_agent)
        child_checkpoints: list[tuple[Any, int]] = []
        try:
            try:
                action, agents, fields = self._route(user_message)
            except Exception:
                yield {"event": "done"}
                return RunResult(
                    run_id=self.orchestration_id,
                    final_answer=SAFE_FAILURE_MESSAGE,
                    stopped="routing_failed",
                    agent_name="experiment-design-coordinator",
                    domain="coordination",
                )

            if action == "clarify":
                self._pending_user_messages.append(user_message)
            else:
                # A terminal execution failure must not carry an abandoned
                # request into another domain. An accepted child clarification
                # retains the episode after fan-in; cancellation restores both
                # checkpoints alongside router and child conversations.
                self._pending_user_messages.clear()
                self._pending_messages_seen_by_agent.clear()

            yield {"event": "route", "action": action, "agents": agents, "fields": fields}
            if action == "clarify":
                request = "CLARIFICATION_REQUEST " + json.dumps(
                    {"fields": fields}, separators=(",", ":"), ensure_ascii=True,
                )
                yield {"event": "message", "content": request}
                yield {"event": "done"}
                return RunResult(
                    run_id=self.orchestration_id,
                    final_answer=request,
                    stopped="clarification",
                    agent_name="experiment-design-coordinator",
                    domain="coordination",
                )

            ledger = HandoffLedger()
            resources: list[dict[str, Any]] = []
            audit_files: list[str] = []
            for index, agent_name in enumerate(agents):
                task_id = f"{parent_task_id}-task-{index + 1}"
                yield {"event": "agent_start", "agent": agent_name, "task_id": task_id}
                try:
                    child = self._executor(agent_name)
                    checkpoint = self._child_checkpoint(child)
                    if checkpoint is not None:
                        child_checkpoints.append((child, checkpoint))
                    child_request = user_message
                    unseen_messages = pending_checkpoint[seen_checkpoint.get(agent_name, 0):]
                    if unseen_messages:
                        child_request = json.dumps({
                            "type": "clarified_user_request",
                            "prior_user_messages": unseen_messages,
                            "current_user_message": user_message,
                        }, separators=(",", ":"), ensure_ascii=True)
                    generator = child.run(child_request)
                    try:
                        while True:
                            try:
                                event = next(generator)
                            except StopIteration as stop:
                                child_result = stop.value
                                break
                            allowed_tools = {
                                str(grant).removeprefix("mcp__experiment-design__")
                                for grant in self.registry[agent_name]["tools"]
                            }
                            projected = self._value_free_child_event(event, allowed_tools)
                            if projected is not None:
                                projected["agent"] = agent_name
                                projected["task_id"] = task_id
                                yield projected
                    finally:
                        close = getattr(generator, "close", None)
                        if callable(close):
                            close()
                    if not isinstance(child_result, RunResult):
                        raise HandoffError("domain agent returned no run result")
                    spec = self.registry[agent_name]
                    envelope = HandoffEnvelope.from_run_result(
                        child_result,
                        orchestration_id=self.orchestration_id,
                        task_id=task_id,
                        parent_task_id=parent_task_id,
                        expected_agent=agent_name,
                        expected_domain=str(spec["domain"]),
                    )
                    ledger.record(envelope)
                    resources.extend(child_result.private_resources)
                    if child_result.audit_file:
                        audit_files.append(child_result.audit_file)
                    yield {
                        "event": "agent_complete", "agent": agent_name,
                        "task_id": task_id, "status": envelope.status,
                    }
                except Exception:
                    self._rollback_children(child_checkpoints)
                    resources.clear()
                    yield {"event": "agent_failed", "agent": agent_name, "task_id": task_id}
                    yield {"event": "done"}
                    return RunResult(
                        run_id=self.orchestration_id,
                        final_answer=SAFE_FAILURE_MESSAGE,
                        audit_file=";".join(audit_files),
                        stopped="child_failed",
                        agent_name="experiment-design-coordinator",
                        domain="coordination",
                    )

            try:
                final = aggregate_handoffs(ledger.all(), self.children)
            except HandoffError:
                self._rollback_children(child_checkpoints)
                resources.clear()
                yield {"event": "message", "content": SAFE_FAILURE_MESSAGE}
                yield {"event": "done"}
                return RunResult(
                    run_id=self.orchestration_id,
                    final_answer=SAFE_FAILURE_MESSAGE,
                    audit_file=";".join(audit_files),
                    stopped="aggregation_failed",
                    agent_name="experiment-design-coordinator",
                    domain="coordination",
                )

            if any(item.status == "CLARIFICATION" for item in ledger.all()):
                # aggregate_handoffs permits only one child for clarification.
                # Save raw user input only, never its elicitation output or any
                # generated evidence. Other domains may still need this input.
                self._pending_user_messages[:] = [*pending_checkpoint, user_message]
                self._pending_messages_seen_by_agent.update(seen_checkpoint)
                self._pending_messages_seen_by_agent[agents[0]] = len(self._pending_user_messages)

            yield {"event": "message", "content": final}
            yield {"event": "done"}
            return RunResult(
                run_id=self.orchestration_id,
                final_answer=final,
                gate_verdicts=[item.status for item in ledger.all()],
                audit_file=";".join(audit_files),
                stopped="end_turn",
                private_resources=resources,
                agent_name="experiment-design-coordinator",
                domain="coordination",
                verification_records=[item.public_summary() for item in ledger.all()],
            )
        except GeneratorExit:
            # No user result is emitted for caller cancellation. Restore both
            # the routing conversation and every child touched by this turn.
            del self._router_messages[router_checkpoint:]
            self._pending_user_messages[:] = pending_checkpoint
            self._pending_messages_seen_by_agent.clear()
            self._pending_messages_seen_by_agent.update(seen_checkpoint)
            self._rollback_children(child_checkpoints)
            raise

    def __enter__(self) -> "MultiAgentExperimentDesignHarness":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
