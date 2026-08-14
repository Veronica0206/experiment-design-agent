#!/usr/bin/env python3
"""Focused static/dynamic checks for the governed Streamlit surface."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any


SOURCE = Path(__file__).resolve().parents[1] / "streamlit_app.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))

passed = failed = 0


def check(name: str, condition: bool) -> None:
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL")
        failed += 1


agent_chat = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "agent_chat"
)
chat_imports = [
    alias.name
    for node in ast.walk(agent_chat)
    if isinstance(node, ast.ImportFrom) and node.module == "multi_agent_harness"
    for alias in node.names
]
check(
    "chat_uses_multi_agent_coordinator",
    chat_imports == ["MultiAgentExperimentDesignHarness"],
)
check(
    "chat_does_not_instantiate_legacy_single_agent",
    "ExperimentDesignHarness" not in ast.unparse(agent_chat).replace(
        "MultiAgentExperimentDesignHarness", ""
    ) and 'session_state.get("harness")' not in ast.unparse(agent_chat),
)
check(
    "explicit_session_end_stops_child_runtimes",
    "stop_agent_session()" in ast.unparse(agent_chat),
)

# Execute only the constant and pure mapping helper. Importing the full app
# would start Streamlit widgets and make this unit check environment-dependent.
selected = [
    node for node in TREE.body
    if (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in {
            "AGENT_LABELS", "CHAT_MODE", "MODE_STATE_KEY",
        }
                for target in node.targets)
    ) or (
        isinstance(node, ast.FunctionDef)
        and node.name in {
            "agent_event_view", "stop_agent_session", "reconcile_agent_mode",
            "rsm_limits", "clamp_session_integer",
        }
    )
]
namespace: dict[str, Any] = {"Any": Any}
exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), namespace)
event_view = namespace["agent_event_view"]
CHAT_MODE = namespace["CHAT_MODE"]

route_view = event_view({
    "event": "route",
    "action": "dispatch",
    "agents": ["doe-designer"],
    "task_id": "private-task-id",
})
complete_view = event_view({
    "event": "agent_complete",
    "agent": "randomization-planner",
    "status": "VERIFIED",
    "analysis_ids": ["private-analysis-id"],
})
rendered = " ".join((route_view or ("", ""))[1:] + (complete_view or ("", ""))[1:])
check(
    "lifecycle_view_omits_task_and_analysis_identity",
    "private-task-id" not in rendered and "private-analysis-id" not in rendered,
)
check(
    "partial_completion_is_visibly_qualified",
    event_view({
        "event": "agent_complete", "agent": "meta-analysis-analyst",
        "status": "PASS_PARTIAL",
    })[0] == "warning",
)
check(
    "failed_agent_is_reported_as_withheld",
    "withheld" in event_view({
        "event": "agent_failed", "agent": "single-endpoint-designer",
    })[1],
)


class FakeState(dict):
    def __getattr__(self, name: str) -> Any:
        return self[name]

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value


class FakeHarness:
    stopped = False

    def stop(self) -> None:
        self.stopped = True


fake_harness = FakeHarness()
fake_state = FakeState(multi_agent_harness=fake_harness, messages=[{"role": "user"}])
namespace["st"] = SimpleNamespace(session_state=fake_state, error=lambda _message: None)
stopped = namespace["stop_agent_session"]()
check(
    "session_stop_closes_team_and_clears_history",
    stopped is True and fake_harness.stopped
    and "multi_agent_harness" not in fake_state
    and fake_state.messages == [],
)

legacy_harness = FakeHarness()
team_harness = FakeHarness()
dual_state = FakeState(
    harness=legacy_harness,
    multi_agent_harness=team_harness,
    messages=[{"role": "user"}],
)
namespace["st"] = SimpleNamespace(session_state=dual_state, error=lambda _message: None)
stopped = namespace["stop_agent_session"]()
check(
    "session_stop_closes_legacy_and_team_handles",
    stopped is True
    and legacy_harness.stopped
    and team_harness.stopped
    and "harness" not in dual_state
    and "multi_agent_harness" not in dual_state
    and dual_state.messages == [],
)


class FailingHarness:
    def stop(self) -> None:
        raise RuntimeError("private runtime detail")


errors: list[str] = []
failed_state = FakeState(
    multi_agent_harness=FailingHarness(), messages=[{"role": "user"}],
)
namespace["st"] = SimpleNamespace(session_state=failed_state, error=errors.append)
stopped = namespace["stop_agent_session"]()
check(
    "failed_stop_retains_handle_for_retry",
    stopped is False
    and "multi_agent_harness" in failed_state
    and failed_state.messages == [{"role": "user"}]
    and errors == ["The agent session could not be fully closed. Please retry."],
)

successful_team = FakeHarness()
mixed_state = FakeState(
    harness=FailingHarness(),
    multi_agent_harness=successful_team,
    messages=[{"role": "user"}],
)
namespace["st"] = SimpleNamespace(session_state=mixed_state, error=lambda _message: None)
stopped = namespace["stop_agent_session"]()
check(
    "partial_cleanup_retains_only_failed_handle_and_history",
    stopped is False
    and "harness" in mixed_state
    and "multi_agent_harness" not in mixed_state
    and successful_team.stopped
    and mixed_state.messages == [{"role": "user"}],
)

mode_harness = FakeHarness()
mode_state = FakeState(
    multi_agent_harness=mode_harness,
    messages=[{"role": "user"}],
    experiment_design_active_mode="Free-form (multi-agent chat)",
)
namespace["st"] = SimpleNamespace(session_state=mode_state, error=lambda _message: None)
reconciled = namespace["reconcile_agent_mode"]("Single-endpoint design")
check(
    "leaving_chat_stops_team_and_records_new_mode",
    reconciled is True
    and mode_harness.stopped
    and "multi_agent_harness" not in mode_state
    and mode_state.messages == []
    and mode_state.experiment_design_active_mode == "Single-endpoint design",
)

failed_mode_state = FakeState(
    multi_agent_harness=FailingHarness(),
    messages=[{"role": "user"}],
    experiment_design_active_mode="Free-form (multi-agent chat)",
)
namespace["st"] = SimpleNamespace(
    session_state=failed_mode_state, error=lambda _message: None,
)
reconciled = namespace["reconcile_agent_mode"]("Meta-analysis")
check(
    "failed_mode_change_cleanup_retains_chat_state_for_retry",
    reconciled is False
    and "multi_agent_harness" in failed_mode_state
    and failed_mode_state.messages == [{"role": "user"}]
    and failed_mode_state.experiment_design_active_mode
    == "Free-form (multi-agent chat)",
)

legacy_mode_state = FakeState(
    harness=FailingHarness(),
    messages=[{"role": "user"}],
    experiment_design_active_mode="Free-form (multi-agent chat)",
)
namespace["st"] = SimpleNamespace(
    session_state=legacy_mode_state, error=lambda _message: None,
)
reconciled = namespace["reconcile_agent_mode"]("Meta-analysis")
check(
    "failed_legacy_cleanup_blocks_direct_mode_and_retains_handle",
    reconciled is False
    and "harness" in legacy_mode_state
    and legacy_mode_state.messages == [{"role": "user"}]
    and legacy_mode_state.experiment_design_active_mode == CHAT_MODE,
)

legacy_chat_harness = FakeHarness()
legacy_chat_state = FakeState(
    harness=legacy_chat_harness,
    messages=[{"role": "user"}],
    experiment_design_active_mode="Single-endpoint design",
)
namespace["st"] = SimpleNamespace(
    session_state=legacy_chat_state, error=lambda _message: None,
)
reconciled = namespace["reconcile_agent_mode"](CHAT_MODE)
check(
    "entering_chat_migrates_legacy_runtime_before_render",
    reconciled is True
    and legacy_chat_harness.stopped
    and "harness" not in legacy_chat_state
    and legacy_chat_state.messages == []
    and legacy_chat_state.experiment_design_active_mode == CHAT_MODE,
)

stale_team = FakeHarness()
nonchat_state = FakeState(
    multi_agent_harness=stale_team,
    messages=[{"role": "user"}],
    experiment_design_active_mode="Meta-analysis",
)
namespace["st"] = SimpleNamespace(
    session_state=nonchat_state, error=lambda _message: None,
)
reconciled = namespace["reconcile_agent_mode"]("Randomization")
check(
    "nonchat_transition_stops_any_stale_team_before_render",
    reconciled is True
    and stale_team.stopped
    and "multi_agent_harness" not in nonchat_state
    and nonchat_state.messages == []
    and nonchat_state.experiment_design_active_mode == "Randomization",
)

main_cleanup_guard = next(
    node for node in TREE.body
    if isinstance(node, ast.If) and ast.unparse(node.test) == "not mode_ready"
    and "Mode controls and execution" in ast.unparse(node)
)
check(
    "failed_cleanup_blocks_all_mode_rendering_and_execution",
    bool(main_cleanup_guard.orelse)
    and isinstance(main_cleanup_guard.orelse[0], ast.If)
    and ast.unparse(main_cleanup_guard.orelse[0].test)
    == "mode == 'Single-endpoint design'"
    and "single_endpoint_form()" in ast.unparse(main_cleanup_guard.orelse[0]),
)

rsm_limits = namespace["rsm_limits"]
check(
    "rsm_limits_match_engine_constraints",
    rsm_limits("bbd", 3) == (3, 5, None)
    and rsm_limits("bbd", 5) == (3, 5, None)
    and rsm_limits("ccd", 2) == (2, 8, 1)
    and rsm_limits("ccd", 8) == (2, 8, 7),
)

source_text = SOURCE.read_text(encoding="utf-8")
check(
    "direct_surface_covers_doe_and_randomization",
    'mode == "Design of experiments"' in source_text
    and 'run_tool_directly("randomize", p)' in source_text,
)
check(
    "incidence_caption_describes_both_valid_directions",
    "incidence_rate** supports both a protective reduction" in source_text
    and "harm detection (alt > null, upper tail)" in source_text,
)
doe_form = next(
    node for node in TREE.body
    if isinstance(node, ast.FunctionDef) and node.name == "doe_form"
)
doe_source = ast.unparse(doe_form)
check(
    "rsm_widgets_apply_dynamic_engine_limits",
    'rsm_limits(rsm_type, 3)' in doe_source
    and "max_value=max_fraction" in doe_source
    and "CCD fraction exponent must be below the number of factors" in doe_source,
)
check(
    "runtime_exceptions_are_not_rendered_verbatim",
    "ev.get('message')" not in source_text and 'st.error(f"Error: {e}")' not in source_text
    and 'st.error(f"R error:' not in source_text
    and "except ImportError as" not in source_text
    and "Import error:" not in source_text,
)
check(
    "raw_tool_result_renderer_is_absent",
    "def display_result(" not in source_text and 'st.json(result)' not in source_text,
)

# Run the real Streamlit script once to ensure the top-level guard prevents a
# failed legacy cleanup from rendering the newly selected direct-mode form.
from streamlit.testing.v1 import AppTest  # noqa: E402


class AppTestFailingHarness:
    def stop(self) -> None:
        raise RuntimeError("private runtime detail")


app = AppTest.from_file(str(SOURCE), default_timeout=10)
sys.path.insert(0, str(SOURCE.parent))
app.run()
app.session_state["harness"] = AppTestFailingHarness()
app.session_state["messages"] = [{"role": "user", "content": "private"}]
app.session_state["experiment_design_active_mode"] = CHAT_MODE
app.sidebar.radio[0].set_value("Meta-analysis")
app.run()
app_errors = [element.value for element in app.error]
check(
    "app_failed_legacy_cleanup_blocks_direct_mode_rendering",
    not app.exception
    and "harness" in app.session_state
    and app.session_state["experiment_design_active_mode"] == CHAT_MODE
    and any("Mode controls and execution" in value for value in app_errors)
    and not app.get("form"),
)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
