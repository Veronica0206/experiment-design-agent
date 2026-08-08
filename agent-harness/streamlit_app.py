"""Streamlit front-end for the Experiment Design Agent.

Researcher-friendly UI for configuring and running experiment designs
through the MCP tools directly (form modes) or the gated agent (chat mode).

Note: `harness` (and the Anthropic SDK) is imported lazily inside the chat
mode only, so the four pure-R form modes work even without the SDK installed.
"""

from __future__ import annotations

import json
from typing import Any

import streamlit as st

from artifact_download import read_verified_artifact
from mcp_client import MCPClient
from gates import GateVerdict, check_regression_tests, combined_gate
from verification import public_envelope_matches_call

st.set_page_config(page_title="Experiment Design Agent", layout="wide")


# ── Safe formatting (R NA -> JSON null -> Python None) ─────────────

def fmt(value: Any, spec: str = ".4f") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "n/a" if value is None else str(value)
    try:
        return format(value, spec)
    except (ValueError, TypeError):
        return str(value)


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


# ── Sidebar ────────────────────────────────────────────────────────

st.sidebar.title("Experiment Design Agent")
mode = st.sidebar.radio(
    "Design type",
    [
        "Single-endpoint design",
        "Multi-arm adaptive design",
        "Indirect comparison",
        "Meta-analysis",
        "Free-form (agent chat)",
    ],
)

if "messages" not in st.session_state:
    st.session_state.messages = []


# ── Parameter forms ────────────────────────────────────────────────

def single_endpoint_form() -> dict | None:
    with st.form("single_design"):
        st.subheader("Single-Endpoint Design")
        st.caption(
            "Direction: for **tte** / **incidence_rate**, lower is better "
            "(set alt < null). For **binary** / **continuous**, higher is better "
            "(set alt > null)."
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


def indirect_comparison_form() -> dict | None:
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
        except Exception as e:  # noqa: BLE001 - surfaced to the user
            st.error(f"Error: {e}")
            return

    if isinstance(result, dict) and result.get("error"):
        st.error(f"R error: {result['error']}")
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


def display_result(tool_name: str, result: dict):
    if tool_name in ("validate_config", "sample_size", "simulate_design"):
        ss = result.get("results") or result.get("sample_size")
        if ss:
            st.subheader("Sample Size Table")
            st.dataframe(ss)
        oc = result.get("oc")
        if oc is not None:
            st.subheader("Operating Characteristics")
            st.dataframe(oc) if isinstance(oc, list) else st.json(oc)
        ppos = result.get("ppos")
        if ppos is not None:
            st.subheader("Predictive Probability of Success")
            st.json(ppos)

    elif tool_name == "master_simulate":
        st.subheader("Multi-Arm Design Results")
        st.json(result.get("result", result))
        if result.get("output_dir"):
            st.caption(f"Outputs written to: {result['output_dir']}")

    elif tool_name == "indirect_compare":
        comps = result.get("comparisons", [])
        st.subheader("Indirect Comparison Results")
        for comp in comps:
            if isinstance(comp, list):
                comp = comp[0]
            c1, c2, c3 = st.columns(3)
            c1.metric("Estimate", fmt(comp.get("estimate")))
            c2.metric("SE", fmt(comp.get("se")))
            c3.metric("95% CI",
                      f"[{fmt(comp.get('lower'), '.3f')}, {fmt(comp.get('upper'), '.3f')}]")
            if comp.get("natural_estimate") is not None:
                st.caption(
                    f"Natural scale: {fmt(comp.get('natural_estimate'))} "
                    f"[{fmt(comp.get('natural_lower'))}, {fmt(comp.get('natural_upper'))}]"
                )

    elif tool_name == "meta_analyze":
        st.subheader("Meta-Analysis Results")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Pooled estimate", fmt(result.get("estimate")))
        c2.metric("SE", fmt(result.get("se")))
        c3.metric("I²", fmt(result.get("i2"), ".1f") + ("%" if result.get("i2") is not None else ""))
        c4.metric("95% CI",
                  f"[{fmt(result.get('lower'), '.3f')}, {fmt(result.get('upper'), '.3f')}]")
        if result.get("tau2") is not None:
            st.caption(f"τ² = {fmt(result.get('tau2'))},  k = {result.get('k', 'n/a')}")
        effects = result.get("study_effects")
        variances = result.get("study_variances")
        if isinstance(effects, list) and isinstance(variances, list):
            st.subheader("Study Effects")
            for i, (yi, vi) in enumerate(zip(effects, variances)):
                st.text(f"  Study {i + 1}: yi={fmt(yi)}, vi={fmt(vi)}")

    with st.expander("Raw JSON"):
        st.json(result)


# ── Agent chat mode ────────────────────────────────────────────────

def agent_chat():
    st.subheader("Agent Chat")
    st.caption("Describe your design problem in natural language. The agent "
               "elicits details across turns, runs the analysis, verifies it, "
               "then interprets. Conversation history is preserved.")

    # Lazy import so the SDK is only required for this mode (C-3).
    try:
        from harness import ExperimentDesignHarness
    except ImportError as e:
        st.error(f"Agent chat needs the Anthropic SDK (`pip install anthropic`). "
                 f"The form modes above work without it. Import error: {e}")
        return

    # One long-lived harness per session: preserves history and avoids
    # re-spawning the MCP server per message (A-3 / C-5).
    if "harness" not in st.session_state:
        h = ExperimentDesignHarness()
        try:
            h.start()
        except Exception as e:  # noqa: BLE001
            st.error(f"Could not start the agent: {e}")
            return
        st.session_state.harness = h

    harness = st.session_state.harness

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
                        if etype == "phase":
                            st.info(f"Phase: {ev['phase']}")
                        elif etype == "message":
                            st.markdown(ev["content"])
                        elif etype == "unverified_message":
                            st.error("Unverified content was withheld.")
                        elif etype == "tool_call":
                            st.caption(f"→ {ev['tool']}")
                        elif etype == "tool_result":
                            with st.expander(f"{ev['tool']} result"):
                                st.json(ev["result"])
                        elif etype == "tool_result_withheld":
                            st.error(
                                f"{ev['tool']} result withheld — "
                                f"{ev.get('verification', {}).get('verification_status', 'FAILED')}"
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
                            st.error(f"The agent hit an error and rolled back this "
                                     f"turn (you can retry): {ev.get('message')}")
                except Exception as e:  # noqa: BLE001
                    st.error(f"Agent error: {e}")

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

if mode == "Single-endpoint design":
    p = single_endpoint_form()
    if p:
        run_tool_directly("simulate_design", p)
elif mode == "Multi-arm adaptive design":
    p = adaptive_design_form()
    if p:
        run_tool_directly("master_simulate", p)
elif mode == "Indirect comparison":
    p = indirect_comparison_form()
    if p:
        run_tool_directly("indirect_compare", p)
elif mode == "Meta-analysis":
    p = meta_analysis_form()
    if p:
        run_tool_directly("meta_analyze", p)
elif mode == "Free-form (agent chat)":
    agent_chat()
