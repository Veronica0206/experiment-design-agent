"""Streamlit front-end for the Experiment Design Agent.

Researcher-friendly UI for configuring and running experiment designs
through the MCP tools directly (form modes) or the governed multi-agent
coordinator (chat mode).

Note: `multi_agent_harness` (and the Anthropic SDK) is imported lazily inside the chat
mode only, so the six direct form modes work even without the SDK installed.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from artifact_download import read_verified_artifact
from mcp_client import MCPClient
from gates import GateVerdict, check_regression_tests, combined_gate
from verification import public_envelope_matches_call
from governance.runtime_profile import load_runtime_profile

st.set_page_config(page_title="Experiment Design Agent", layout="wide")


AGENT_LABELS = {
    "single-endpoint-designer": "Single-endpoint designer",
    "master-protocol-designer": "Master-protocol designer",
    "doe-designer": "Design-of-experiments specialist",
    "randomization-planner": "Randomization planner",
    "indirect-comparison-analyst": "Indirect-comparison analyst",
    "meta-analysis-analyst": "Meta-analysis analyst",
}

CHAT_MODE = "Free-form (multi-agent chat)"
MODE_STATE_KEY = "experiment_design_active_mode"


def available_modes() -> list[str]:
    modes = {
        "single_endpoint": "Single-endpoint design",
        "master_protocol": "Multi-arm adaptive design",
        "doe": "Design of experiments", "randomization": "Randomization",
        "indirect_comparison": "Indirect comparison", "meta_analysis": "Meta-analysis",
    }
    return [modes[domain] for domain in load_runtime_profile()["domains"]] + [CHAT_MODE]


def render_private_resources(
    bundles: list[dict[str, Any]],
    key_prefix: str,
) -> None:
    """Offer only managed, provenance-bound artifact bytes to the user."""
    for index, bundle in enumerate(bundles):
        try:
            name, data = read_verified_artifact(bundle)
        except Exception:  # fail closed without exposing a private host path
            st.error("A verified private artifact is unavailable or no longer matches provenance.")
            continue
        resource = bundle.get("resource") if isinstance(bundle, dict) else None
        mime = resource.get("mimeType") if isinstance(resource, dict) else None
        verification_id = str(bundle.get("verification_id") or "artifact")
        st.download_button(
            f"Download private verified artifact: {name}",
            data=data,
            file_name=name,
            mime=mime if isinstance(mime, str) else "application/octet-stream",
            key=f"{key_prefix}-{verification_id[:16]}-{index}",
        )


def agent_event_view(event: dict[str, Any]) -> tuple[str, str] | None:
    """Map orchestration lifecycle events to value-free UI messages.

    This deliberately ignores tool arguments, task IDs, analysis IDs, and
    artifact handles. Those values are unnecessary for progress display and
    can contain identity or equality-oracle information at the UI boundary.
    """
    event_type = event.get("event")
    if event_type == "route":
        if event.get("action") == "dispatch":
            labels = [
                AGENT_LABELS.get(str(name), "Approved specialist")
                for name in list(event.get("agents") or [])
            ]
            if labels:
                return "info", "Coordinator selected: " + ", ".join(labels)
        elif event.get("action") == "clarify":
            fields = [str(item).replace("_", " ") for item in event.get("fields") or []]
            if fields:
                return "info", "Coordinator needs clarification: " + ", ".join(fields)
        return "error", "The coordinator returned an invalid route; no analysis was run."
    if event_type == "agent_start":
        label = AGENT_LABELS.get(str(event.get("agent")), "Approved specialist")
        return "info", f"{label} started."
    if event_type == "agent_complete":
        label = AGENT_LABELS.get(str(event.get("agent")), "Approved specialist")
        status = str(event.get("status") or "UNKNOWN")
        level = "warning" if status == "PASS_PARTIAL" else "success"
        return level, f"{label} completed with status {status}."
    if event_type == "agent_failed":
        label = AGENT_LABELS.get(str(event.get("agent")), "Approved specialist")
        return "error", f"{label} failed; its result was withheld."
    return None


def render_agent_event(event: dict[str, Any]) -> bool:
    """Render a supported orchestration event and report whether it was handled."""
    view = agent_event_view(event)
    if view is None:
        return False
    level, message = view
    getattr(st, level)(message)
    return True


def stop_agent_session() -> bool:
    """Stop all lazily-created child runtimes before clearing UI session state."""
    outcomes: dict[int, bool] = {}
    cleanup_failed = False
    for state_key in ("harness", "multi_agent_harness"):
        harness = st.session_state.get(state_key)
        if harness is None:
            st.session_state.pop(state_key, None)
            continue
        identity = id(harness)
        succeeded = outcomes.get(identity)
        if succeeded is None:
            try:
                harness.stop()
            except Exception:  # keep failed handles so cleanup can be retried
                succeeded = False
            else:
                succeeded = True
            outcomes[identity] = succeeded
        if succeeded:
            st.session_state.pop(state_key, None)
        else:
            cleanup_failed = True
    if cleanup_failed:
        st.error("The agent session could not be fully closed. Please retry.")
        return False
    st.session_state.messages = []
    return True


def reconcile_agent_mode(current_mode: str) -> bool:
    """Close chat runtimes when the sidebar leaves chat mode.

    A failed stop keeps each failed runtime handle and the previous-mode marker
    so the next rerun retries cleanup instead of silently orphaning child MCP
    processes.
    """
    legacy_runtime = st.session_state.get("harness") is not None
    has_any_runtime = legacy_runtime or (
        st.session_state.get("multi_agent_harness") is not None
    )
    # Any runtime is stale outside chat. A legacy single-agent runtime is also
    # migrated before chat renders, so every mode is blocked if that stop fails.
    needs_cleanup = legacy_runtime or (current_mode != CHAT_MODE and has_any_runtime)
    if needs_cleanup and not stop_agent_session():
        return False
    st.session_state[MODE_STATE_KEY] = current_mode
    return True


def rsm_limits(design: str, n_factors: int) -> tuple[int, int, int | None]:
    """Return the engine-aligned factor and CCD-fraction limits."""
    if design == "bbd":
        return 3, 5, None
    if design == "ccd":
        return 2, 8, max(0, int(n_factors) - 1)
    raise ValueError("unsupported response-surface design")


def clamp_session_integer(key: str, default: int, lower: int, upper: int) -> int:
    """Keep a keyed number-input value valid when dynamic bounds shrink."""
    try:
        value = int(st.session_state.get(key, default))
    except (TypeError, ValueError):
        value = default
    bounded = min(max(value, lower), upper)
    if value != bounded:
        # Remove the now-invalid widget value before the widget is recreated;
        # its bounded `value` argument then becomes the new session value.
        st.session_state.pop(key, None)
    return bounded


# ── Sidebar ────────────────────────────────────────────────────────

st.sidebar.title("Experiment Design Agent")
mode = st.sidebar.radio(
    "Design type",
    available_modes(),
)

if "messages" not in st.session_state:
    st.session_state.messages = []
mode_ready = reconcile_agent_mode(mode)
if not mode_ready:
    st.sidebar.warning("Agent cleanup is incomplete; the session handle was retained for retry.")


# ── Parameter forms ────────────────────────────────────────────────

def single_endpoint_form() -> dict | None:
    with st.form("single_design"):
        st.subheader("Single-Endpoint Design")
        st.caption(
            "Direction: for **tte**, lower hazard is better (set alt < null). "
            "**incidence_rate** supports both a protective reduction (alt < null, "
            "lower tail) and harm detection (alt > null, upper tail). For "
            "**binary** / **continuous**, higher is better (set alt > null)."
        )
        col1, col2 = st.columns(2)
        with col1:
            endpoint = st.selectbox(
                "Endpoint type", ["binary", "continuous", "tte", "incidence_rate"]
            )
            study_type = st.selectbox(
                "Study type", ["signal_detection", "poc", "confirmatory"]
            )
            design = st.selectbox("Design", ["single_arm", "controlled"])
        with col2:
            null_param = st.number_input("Null parameter (H0)", value=0.10, format="%.4f")
            alt_param = st.number_input("Alt parameter (H1)", value=0.30, format="%.4f")
            sd = st.number_input("SD (continuous only)", value=1.0, min_value=0.01)

        col3, col4 = st.columns(2)
        with col3:
            alpha = st.number_input("Alpha", value=0.025, min_value=0.001, max_value=0.5)
            power = st.number_input("Power", value=0.80, min_value=0.5, max_value=0.99)
        with col4:
            seed = st.number_input("Random seed", value=42, min_value=1)
            n_sims = st.number_input("OC simulations", value=5000, min_value=100,
                                     max_value=5000, step=500)

        st.markdown("**Time parameters** (required for tte / incidence_rate)")
        col5, col6, col7 = st.columns(3)
        with col5:
            accrual_time = st.number_input("Accrual time (tte)", value=12.0, min_value=0.0)
        with col6:
            followup_time = st.number_input("Follow-up time (tte)", value=24.0, min_value=0.0)
        with col7:
            exposure_time = st.number_input("Exposure time (incidence)", value=1.0, min_value=0.0)

        submitted = st.form_submit_button("Design experiment")
        if not submitted:
            return None

        if endpoint == "tte" and alt_param >= null_param:
            st.warning(
                f"For tte, lower hazard is better — alt ({alt_param}) must be "
                f"below null ({null_param}). The R framework rejects alt >= null."
            )
        elif endpoint == "incidence_rate" and alt_param > null_param:
            st.info(
                f"alt ({alt_param}) > null ({null_param}): this is a HARM-DETECTION "
                "framing — the design is sized and analyzed in the upper tail, and "
                "'Go' means a rate-increase signal was detected."
            )

        config: dict[str, Any] = {
            "endpoint_type": endpoint,
            "study_type": study_type,
            "design": design,
            "null_param": null_param,
            "alt_param": alt_param,
            "alphas": alpha,
            "powers": power,
        }
        if endpoint == "continuous":
            config["sd"] = sd
        elif endpoint == "tte":
            config["accrual_time"] = accrual_time
            config["followup_time"] = followup_time
        elif endpoint == "incidence_rate":
            config["exposure_time"] = exposure_time
        return {"config": config, "seed": int(seed), "B_oc": int(n_sims)}


def adaptive_design_form() -> dict | None:
    with st.form("adaptive_design"):
        st.subheader("Multi-Arm Adaptive Design")
        col1, col2 = st.columns(2)
        with col1:
            master_type = st.selectbox("Design type", ["basket", "umbrella", "platform"])
            endpoint = st.selectbox(
                "Endpoint type", ["binary", "continuous", "tte", "incidence_rate"]
            )
            n_subgroups = st.number_input("Number of subgroups/arms", value=3,
                                          min_value=2, max_value=20)
        with col2:
            null_param = st.number_input("Null parameter", value=0.15, format="%.4f")
            alt_text = st.text_input(
                "Alt parameters (comma-separated, one per subgroup)", value="0.35,0.35,0.15"
            )
            n_sims = st.number_input("Simulations", value=500, min_value=50,
                                     max_value=10000, step=250)

        st.caption(
            "Platform designs also need a period schedule; a default "
            "(all arms enter period 1, leave the last period) is applied automatically."
        )
        col3, col4 = st.columns(2)
        with col3:
            n_periods = st.number_input("Periods (platform)", value=3, min_value=2, max_value=20)
        with col4:
            n_per_period = st.number_input("N per period (platform)", value=40, min_value=1)

        st.markdown("**Endpoint parameters**")
        col5, col6, col7 = st.columns(3)
        with col5:
            sd = st.number_input("SD (continuous)", value=1.0, min_value=0.01,
                                 key="adaptive_sd")
        with col6:
            accrual_time = st.number_input("Accrual time (tte)", value=12.0,
                                           min_value=0.01, key="adaptive_accrual")
            exposure_time = st.number_input("Exposure time (incidence)", value=1.0,
                                            min_value=0.01, key="adaptive_exposure")
        with col7:
            followup_time = st.number_input("Follow-up time (tte)", value=24.0,
                                            min_value=0.01, key="adaptive_followup")

        submitted = st.form_submit_button("Simulate design")
        if not submitted:
            return None

        try:
            alt_params = [float(x.strip()) for x in alt_text.split(",") if x.strip()]
        except ValueError:
            st.error("Alt parameters must be numbers.")
            return None
        if len(alt_params) != n_subgroups:
            st.error(f"Expected {n_subgroups} alt parameters, got {len(alt_params)}.")
            return None

        config: dict[str, Any] = {
            "master_design_type": master_type,
            "endpoint_type": endpoint,
            "n_subgroups": int(n_subgroups),
            "null_params": null_param,
            "alt_params": alt_params,
            "n_sims": int(n_sims),
        }
        if master_type == "platform":
            config["n_periods"] = int(n_periods)
            config["n_per_period"] = int(n_per_period)
            config["arms_schedule"] = {
                "enter": [1] * int(n_subgroups),
                "leave": [int(n_periods)] * int(n_subgroups),
            }
        if endpoint == "continuous":
            config["sd"] = sd
        elif endpoint == "tte":
            config["accrual_time"] = accrual_time
            config["followup_time"] = followup_time
        elif endpoint == "incidence_rate":
            config["exposure_time"] = exposure_time
        return {"config": config}


def doe_form() -> tuple[str, dict[str, Any]] | None:
    """Collect a small, schema-aligned input for each governed DoE tool."""
    st.subheader("Design of Experiments")
    # Keep the branch selector outside the form so switching design families
    # rerenders the appropriate controls before the user submits parameters.
    design_type = st.selectbox(
        "Design task", ["A/B sample size", "Factorial design", "Response surface"]
    )
    rsm_type: str | None = None
    rsm_n_factors: int | None = None
    if design_type == "Response surface":
        rsm_type = st.selectbox(
            "Response-surface design", ["ccd", "bbd"], key="rsm_design_type"
        )
        initial_min, initial_max, _ = rsm_limits(rsm_type, 3)
        factor_value = clamp_session_integer(
            "rsm_factors", 3, initial_min, initial_max
        )
        rsm_n_factors = int(st.number_input(
            "Number of factors",
            value=factor_value,
            min_value=initial_min,
            max_value=initial_max,
            key="rsm_factors",
        ))
    with st.form("doe_design"):
        if design_type == "A/B sample size":
            col1, col2 = st.columns(2)
            with col1:
                metric = st.selectbox("Metric", ["proportion", "mean"])
                baseline = st.number_input("Control baseline", value=0.10, format="%.4f")
                effect = st.number_input("Minimum detectable effect", value=0.02,
                                         format="%.4f")
                effect_type = st.selectbox("Effect type", ["absolute", "relative"])
            with col2:
                sd = st.number_input("SD (mean metric)", value=1.0, min_value=0.0001)
                alpha = st.number_input("Alpha", value=0.05, min_value=0.001,
                                        max_value=0.5)
                power = st.number_input("Power", value=0.80, min_value=0.5,
                                        max_value=0.99)
                sided = st.selectbox("Sidedness", [2, 1])
                ratio = st.number_input("Treatment/control allocation ratio", value=1.0,
                                        min_value=0.01)
        elif design_type == "Factorial design":
            col1, col2 = st.columns(2)
            with col1:
                n_factors = st.number_input("Number of factors", value=3,
                                            min_value=1, max_value=12)
                fraction = st.number_input("Fraction exponent (0 = full)", value=0,
                                           min_value=0, max_value=11)
                replicates = st.number_input("Replicates", value=1, min_value=1,
                                             max_value=100)
            with col2:
                center_points = st.number_input("Center points", value=0, min_value=0,
                                                max_value=1000)
                randomize = st.checkbox("Randomize run order", value=False,
                                        key="factorial_randomize")
                seed = st.number_input("Random seed", value=42, min_value=0,
                                       max_value=2147483647, key="factorial_seed")
        else:
            assert rsm_type is not None and rsm_n_factors is not None
            n_factors = rsm_n_factors
            col1, col2 = st.columns(2)
            with col1:
                center_points = st.number_input("Center points", value=3, min_value=0,
                                                max_value=1000, key="rsm_center")
            with col2:
                if rsm_type == "ccd":
                    alpha_choice = st.selectbox(
                        "CCD axial distance", ["rotatable", "face"]
                    )
                    _, _, max_fraction = rsm_limits(rsm_type, n_factors)
                    assert max_fraction is not None
                    fraction_value = clamp_session_integer(
                        "rsm_fraction", 0, 0, max_fraction
                    )
                    fraction = st.number_input(
                        "CCD fraction exponent",
                        value=fraction_value,
                        min_value=0,
                        max_value=max_fraction,
                        key="rsm_fraction",
                    )
                else:
                    alpha_choice = "rotatable"
                    fraction = 0
                    st.caption("Box-Behnken supports 3–5 factors; CCD-only controls are hidden.")
                randomize = st.checkbox("Randomize run order", value=False,
                                        key="rsm_randomize")
                seed = st.number_input("Random seed", value=42, min_value=0,
                                       max_value=2147483647, key="rsm_seed")

        submitted = st.form_submit_button("Create design")
        if not submitted:
            return None
        if design_type == "A/B sample size":
            params: dict[str, Any] = {
                "baseline": baseline,
                "effect": effect,
                "metric": metric,
                "effect_type": effect_type,
                "alpha": alpha,
                "power": power,
                "sided": sided,
                "ratio": ratio,
            }
            if metric == "mean":
                params["sd"] = sd
            return "ab_test", params
        if design_type == "Factorial design":
            if int(fraction) >= int(n_factors):
                st.error("The fraction exponent must be below the number of factors.")
                return None
            return "factorial_design", {
                "n_factors": int(n_factors),
                "fraction": int(fraction),
                "center_points": int(center_points),
                "replicates": int(replicates),
                "randomize": randomize,
                "seed": int(seed),
            }
        factor_min, factor_max, max_fraction = rsm_limits(rsm_type, int(n_factors))
        if not factor_min <= int(n_factors) <= factor_max:
            st.error(
                f"{rsm_type.upper()} requires {factor_min}–{factor_max} factors."
            )
            return None
        if rsm_type == "ccd" and (
            max_fraction is None or int(fraction) > max_fraction
        ):
            st.error("The CCD fraction exponent must be below the number of factors.")
            return None
        params = {
            "n_factors": int(n_factors),
            "design": rsm_type,
            "center_points": int(center_points),
            "randomize": randomize,
            "seed": int(seed),
        }
        if rsm_type == "ccd":
            params.update({"alpha": alpha_choice, "fraction": int(fraction)})
        return "rsm_design", params


def randomization_form() -> dict[str, Any] | None:
    with st.form("randomization"):
        st.subheader("Randomization Plan")
        st.caption(
            "Assignments are withheld from model text and offered as a private download "
            "only after verification."
        )
        col1, col2 = st.columns(2)
        with col1:
            n = st.number_input("Number of units", value=100, min_value=1, max_value=10000)
            arms = st.number_input("Number of arms", value=2, min_value=2, max_value=100)
            method = st.selectbox("Method", ["simple", "block", "stratified"])
        with col2:
            ratio_text = st.text_input("Allocation weights", value="1,1")
            block_size = st.number_input("Block size", value=4, min_value=1,
                                         max_value=10000)
            seed = st.number_input("Random seed", value=42, min_value=0,
                                   max_value=2147483647, key="randomization_seed")
        strata_json = st.text_area(
            "Per-unit strata (JSON array; stratified only)",
            value="[]",
            height=100,
        )
        submitted = st.form_submit_button("Generate assignments")
        if not submitted:
            return None
        try:
            ratio = [float(value.strip()) for value in ratio_text.split(",") if value.strip()]
        except ValueError:
            st.error("Allocation weights must be comma-separated numbers.")
            return None
        if len(ratio) != int(arms) or any(value <= 0 for value in ratio):
            st.error("Provide one positive allocation weight per arm.")
            return None
        params: dict[str, Any] = {
            "n": int(n), "arms": int(arms), "method": method,
            "ratio": ratio, "seed": int(seed),
        }
        if method in {"block", "stratified"}:
            params["block_size"] = int(block_size)
        if method == "stratified":
            try:
                strata = json.loads(strata_json)
            except json.JSONDecodeError:
                st.error("Strata must be a valid JSON array.")
                return None
            if not isinstance(strata, list) or len(strata) != int(n):
                st.error(f"Strata must contain exactly {int(n)} labels.")
                return None
            if any(isinstance(value, bool) or not isinstance(value, (str, int, float))
                   for value in strata):
                st.error("Each stratum label must be a string or number.")
                return None
            params["strata"] = strata
        return params


def bucher_form() -> dict | None:
    with st.form("indirect_compare"):
        st.subheader("Indirect Treatment Comparison (Bucher)")
        col1, col2 = st.columns(2)
        with col1:
            est_ab = st.number_input("Effect A vs B (estimate)", value=0.5)
            se_ab = st.number_input("SE (A vs B)", value=0.15, min_value=0.001)
            treatment_a = st.text_input("Treatment A name", value="Drug A")
        with col2:
            est_cb = st.number_input("Effect C vs B (estimate)", value=0.3)
            se_cb = st.number_input("SE (C vs B)", value=0.2, min_value=0.001)
            treatment_c = st.text_input("Treatment C name", value="Drug C")
        common = st.text_input("Common comparator name", value="Placebo")
        effect_measure = st.selectbox(
            "Effect measure",
            ["log_odds_ratio", "log_hazard_ratio", "mean_difference",
             "log_rate_ratio", "risk_difference", "rate_difference"],
        )
        submitted = st.form_submit_button("Compare")
        if not submitted:
            return None
        return {
            "method": "bucher",
            "comparisons": [{
                "estimate_ab": est_ab, "se_ab": se_ab,
                "estimate_cb": est_cb, "se_cb": se_cb,
                "treatment_a": treatment_a, "treatment_c": treatment_c,
                "common_comparator": common,
                "effect_measure": effect_measure,
            }],
        }


MAIC_ENDPOINT_COLUMNS = {
    "binary": "a 0/1 outcome column",
    "continuous": "a numeric outcome column",
    "rate": "an event-count column and a person-time column",
    "tte": "a follow-up time column and a 0/1 event-status column",
}


def maic_form() -> dict | None:
    """Collect one MAIC request; the MCP server enforces read roots and schema.

    Row-level data, weights, and file paths never reach the public report. An
    anchored comparison against a published comparator is a second, separate
    Bucher submission that restates the MAIC effect and standard error.
    """
    endpoint = st.selectbox(
        "IPD outcome type", list(MAIC_ENDPOINT_COLUMNS.keys()), key="maic_endpoint",
    )
    st.caption(f"The IPD must contain {MAIC_ENDPOINT_COLUMNS[endpoint]} for **{endpoint}**.")
    with st.form("indirect_compare_maic"):
        st.subheader("Matching-Adjusted Indirect Comparison (MAIC)")
        st.caption(
            "Both CSV files must sit inside an approved read root. The published "
            "target file needs `covariate,target_mean` columns."
        )
        ipd_file = st.text_input("Individual-level data CSV (absolute path)")
        targets_file = st.text_input("Published target means CSV (absolute path)")
        col1, col2 = st.columns(2)
        with col1:
            arm_col = st.text_input("Arm column", value="arm")
            treatment_arm = st.text_input("Treatment arm label")
            comparator_arm = st.text_input("Comparator arm label (blank = unanchored weights only)")
        with col2:
            covariates = st.text_input(
                "Covariates to match (comma-separated; blank = every target column)"
            )
            measure = st.text_input("Effect measure (blank = endpoint default)")
            alpha = st.number_input("Two-sided alpha", value=0.05, min_value=0.001,
                                    max_value=0.5)
        outcome_col = event_col = time_col = status_col = None
        tte_method = "cox"
        if endpoint in ("binary", "continuous"):
            outcome_col = st.text_input("Outcome column", value="response")
        elif endpoint == "rate":
            event_col = st.text_input("Event-count column")
            time_col = st.text_input("Person-time column")
        else:
            time_col = st.text_input("Follow-up time column")
            status_col = st.text_input("Event-status column (0/1)")
            tte_method = st.selectbox("Time-to-event model", ["cox", "exponential"])
        col3, col4 = st.columns(2)
        with col3:
            replicates = st.number_input(
                "Bootstrap replicates (non-Cox effects)", value=200, min_value=50,
                max_value=5000, step=50,
            )
        with col4:
            bootstrap_seed = st.number_input(
                "Bootstrap seed (non-Cox effects)", value=42, min_value=0,
                max_value=2147483647,
            )
        submitted = st.form_submit_button("Reweight and compare")
        if not submitted:
            return None

        def required(label: str, value: str | None) -> str | None:
            text = (value or "").strip()
            if not text:
                st.error(f"{label} is required.")
            return text or None

        ipd = required("The IPD CSV path", ipd_file)
        targets = required("The target means CSV path", targets_file)
        treatment = required("The treatment arm label", treatment_arm)
        if not (ipd and targets and treatment):
            return None
        params: dict[str, Any] = {
            "method": "maic",
            "ipd_file": ipd,
            "targets_file": targets,
            "treatment_arm": treatment,
            "arm_col": (arm_col or "").strip() or "arm",
            "maic_endpoint_type": endpoint,
            "alpha": float(alpha),
        }
        if (comparator_arm or "").strip():
            params["comparator_arm"] = comparator_arm.strip()
        covariate_list = [item.strip() for item in (covariates or "").split(",") if item.strip()]
        if covariate_list:
            params["covariates"] = covariate_list
        if (measure or "").strip():
            params["measure"] = measure.strip()
        if endpoint in ("binary", "continuous"):
            params["outcome_col"] = (outcome_col or "").strip() or "response"
        elif endpoint == "rate":
            events = required("The event-count column", event_col)
            person_time = required("The person-time column", time_col)
            if not (events and person_time):
                return None
            params["event_col"] = events
            params["time_col"] = person_time
        else:
            follow_up = required("The follow-up time column", time_col)
            status = required("The event-status column", status_col)
            if not (follow_up and status):
                return None
            params["time_col"] = follow_up
            params["status_col"] = status
            params["tte_method"] = tte_method
        if endpoint != "tte" or tte_method == "exponential":
            params["bootstrap_replicates"] = int(replicates)
            params["bootstrap_seed"] = int(bootstrap_seed)
        return params


def indirect_comparison_form() -> dict | None:
    method = st.radio(
        "Comparison method",
        ["Bucher (published aggregate effects)",
         "MAIC (individual-level data reweighted to published targets)"],
        horizontal=True,
        key="indirect_method",
    )
    if method.startswith("Bucher"):
        return bucher_form()
    return maic_form()


META_FIELDS = {
    "binary_single": "responders, total",
    "binary_comparative": "events_t, total_t, events_c, total_c",
    "continuous_single": "mean, sd, n",
    "continuous_comparative": "mean_t, sd_t, n_t, mean_c, sd_c, n_c",
    "time_to_event": "hr or log_hr, and se (or ci_lower + ci_upper)",
    "incidence_single": "events, person_time",
    "incidence_comparative": "events_t, person_time_t, events_c, person_time_c",
}


def meta_analysis_form() -> dict | None:
    with st.form("meta_analyze"):
        st.subheader("Meta-Analysis")
        endpoint = st.selectbox("Endpoint type", list(META_FIELDS.keys()),
                                index=3)
        st.caption(f"Required study fields for **{endpoint}**: `{META_FIELDS[endpoint]}`")
        studies_json = st.text_area(
            "Studies (JSON array)",
            value=json.dumps([
                {"mean_t": 5.0, "mean_c": 3.0, "sd_t": 2.0, "sd_c": 2.0, "n_t": 50, "n_c": 50},
                {"mean_t": 4.5, "mean_c": 3.2, "sd_t": 1.8, "sd_c": 2.1, "n_t": 40, "n_c": 40},
            ], indent=2),
            height=200,
        )
        random = st.checkbox("Random effects", value=True)
        submitted = st.form_submit_button("Analyze")
        if not submitted:
            return None
        try:
            studies = json.loads(studies_json)
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            return None
        return {"endpoint_type": endpoint, "studies": studies, "random": random}


# ── Direct tool execution (gated on regression tests) ──────────────

def run_tool_directly(tool_name: str, params: dict):
    params = dict(params)
    with st.spinner(f"Running {tool_name}..."):
        try:
            with MCPClient() as mcp:
                result = mcp.call_tool(tool_name, params)
                # Health is checked after the exact result, matching the hook's
                # ordering contract. The result remains buffered until all gates pass.
                test_result = mcp.call_tool("run_tests", {})
                verdicts = [check_regression_tests(test_result)]
                server_envelope = result.get("_verification") if isinstance(result, dict) else None
                if not isinstance(server_envelope, dict) or not server_envelope.get("presentable"):
                    verdicts.append(GateVerdict(
                        passed=False,
                        failures=["trusted server verification envelope missing or failed"],
                    ))
                else:
                    public_result = {
                        key: value for key, value in result.items()
                        if key not in {"_verification", "_provenance", "_private_provenance",
                                       "_private_resources"}
                    }
                    if not public_envelope_matches_call(
                        server_envelope, tool_name, params, public_result,
                        result.get("_provenance") if isinstance(result.get("_provenance"), dict) else {},
                    ):
                        verdicts.append(GateVerdict(
                            passed=False,
                            failures=["verification identity does not match this tool call and result"],
                        ))
                    server_failures = list(server_envelope.get("failures") or [])
                    server_checks = dict(server_envelope.get("checks") or {})
                    verdicts.append(GateVerdict(
                        passed=(not server_failures and
                                all(value is not False for value in server_checks.values())),
                        failures=server_failures,
                        checks=server_checks,
                        blocked=list(server_envelope.get("blocked") or []),
                        notes=list(server_envelope.get("notes") or []),
                    ))
                gate = combined_gate(*verdicts)
        except Exception:  # fail closed without exposing host/runtime details
            st.error("The local analysis could not complete. No result was published.")
            return

    if isinstance(result, dict) and result.get("error"):
        st.error("The analysis was rejected. No result was published.")
        return

    if not gate.passed:
        st.error("Design gate FAILED — results are not trustworthy and are "
                 "withheld: " + "; ".join(gate.failures))
        with st.expander("Gate details"):
            st.json({"checks": gate.checks, "failures": gate.failures,
                     "blocked": gate.blocked, "notes": gate.notes})
        return

    server_envelope = result["_verification"]
    status = str(server_envelope.get("status"))
    if status == "PASS_PARTIAL":
        st.warning("PASS_PARTIAL — NOT automatically checked: "
                   + "; ".join(gate.blocked))
    for note in gate.notes:
        st.info(note)

    st.success(f"{tool_name} completed — {status}")
    report = result.get("_verification", {}).get("report")
    if isinstance(report, str):
        st.markdown(report)
        private_provenance = (
            dict(result.get("_private_provenance") or {}) if isinstance(result, dict) else {}
        )
        identity = result.get("_verification", {}).get("identity", {})
        verification_id = str(identity.get("analysis_id") or "")
        bundles = [
            {
                "resource": dict(resource),
                "private_provenance": private_provenance,
                "verification_id": verification_id,
            }
            for resource in (result.get("_private_resources") or [])
            if isinstance(resource, dict)
        ]
        render_private_resources(bundles, f"private-artifact-{tool_name}")
    else:
        st.error("Verified canonical report is missing; raw result remains withheld.")


# ── Agent chat mode ────────────────────────────────────────────────

def agent_chat():
    st.subheader("Multi-Agent Design Team")
    st.caption(
        "A routing-only coordinator assigns each independent request to the smallest "
        "approved specialist set. Specialists use separate conversations and tool "
        "permissions; only verified canonical reports are combined."
    )

    # Lazy import so the SDK is only required for this mode (C-3).
    try:
        from multi_agent_harness import MultiAgentExperimentDesignHarness
    except ImportError:
        st.error(
            "Agent chat dependencies are unavailable. Reinstall the pinned "
            "harness environment; the direct form modes remain available."
        )
        return

    # One long-lived coordinator per session preserves routing and per-domain
    # conversations. Child MCP processes remain lazy and are stopped together.
    if "multi_agent_harness" not in st.session_state:
        h = None
        try:
            h = MultiAgentExperimentDesignHarness()
            h.start()
        except Exception:  # noqa: BLE001
            if h is not None:
                try:
                    h.stop()
                except Exception:
                    pass
            st.error("Could not start the multi-agent team.")
            return
        st.session_state.multi_agent_harness = h

    harness = st.session_state.multi_agent_harness

    if st.button("Start a new agent session", type="secondary"):
        if stop_agent_session():
            st.rerun()

    for message_index, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            render_private_resources(
                list(msg.get("private_resources") or []),
                f"chat-history-{message_index}",
            )

    if prompt := st.chat_input("Describe your experiment..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            final_answer = ""
            private_resources: list[dict[str, Any]] = []
            with st.spinner("Agent working..."):
                try:
                    gen = harness.run(prompt)
                    while True:
                        try:
                            ev = next(gen)
                        except StopIteration as stop:
                            result = stop.value
                            final_answer = result.final_answer if result else ""
                            private_resources = (
                                list(result.private_resources) if result else []
                            )
                            break
                        etype = ev.get("event")
                        if render_agent_event(ev):
                            continue
                        if etype == "phase":
                            agent = AGENT_LABELS.get(str(ev.get("agent")), "Specialist")
                            st.info(f"{agent} phase: {ev['phase']}")
                        elif etype == "message":
                            # The completed RunResult is rendered once below.
                            # Skipping this streamed duplicate also prevents the
                            # canonical report from appearing twice on rerun.
                            pass
                        elif etype == "unverified_message":
                            st.error("Unverified content was withheld.")
                        elif etype == "tool_call":
                            agent = AGENT_LABELS.get(str(ev.get("agent")), "Specialist")
                            st.caption(f"{agent} → {ev['tool']}")
                        elif etype == "tool_request_rejected":
                            st.caption("The specialist is correcting a rejected tool request.")
                        elif etype == "tool_result":
                            # The harness has already reduced this to a verified
                            # report, but the canonical final report is the only
                            # result representation needed by this UI.
                            st.caption(f"Verified {ev['tool']} result received.")
                        elif etype == "tool_result_withheld":
                            st.error(
                                f"{ev['tool']} result withheld — "
                                f"{ev.get('verification', {}).get('status', 'FAILED')}"
                            )
                        elif etype == "gate":
                            if ev["verdict"] in {"VERIFIED", "PASS_PARTIAL"}:
                                msg = f"Verification gate: {ev['verdict']}  {ev.get('checks', {})}"
                                if ev.get("blocked"):
                                    st.warning(msg + f"  — not auto-checked: {ev['blocked']}")
                                else:
                                    st.success(msg)
                            else:
                                st.error(f"Verification gate: {ev['verdict']} — "
                                         f"{ev.get('failures', [])}")
                        elif etype == "error":
                            # The harness rolled its history back; the session
                            # stays usable — the user can just retry (W-3).
                            st.error("The agent hit an internal error and rolled back this "
                                     "turn. No result was published; you can retry.")
                except Exception:  # noqa: BLE001
                    # A thrown UI/runtime error is not a verified result and
                    # must never be copied into conversation history.
                    st.error("The agent team hit an internal error. No result was published.")

            if final_answer:
                st.markdown(final_answer)
                render_private_resources(private_resources, "chat-current")
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": final_answer,
                        "private_resources": private_resources,
                    }
                )


# ── Main ───────────────────────────────────────────────────────────

st.title("Experiment Design Agent")

if not mode_ready:
    st.error(
        "The previous agent session is still closing. Mode controls and execution "
        "are disabled until cleanup succeeds; retry the selected mode."
    )
elif mode == "Single-endpoint design":
    p = single_endpoint_form()
    if p:
        run_tool_directly("simulate_design", p)
elif mode == "Multi-arm adaptive design":
    p = adaptive_design_form()
    if p:
        run_tool_directly("master_simulate", p)
elif mode == "Design of experiments":
    selection = doe_form()
    if selection:
        tool_name, p = selection
        run_tool_directly(tool_name, p)
elif mode == "Randomization":
    p = randomization_form()
    if p:
        run_tool_directly("randomize", p)
elif mode == "Indirect comparison":
    p = indirect_comparison_form()
    if p:
        run_tool_directly("indirect_compare", p)
elif mode == "Meta-analysis":
    p = meta_analysis_form()
    if p:
        run_tool_directly("meta_analyze", p)
elif mode == "Free-form (multi-agent chat)":
    agent_chat()
