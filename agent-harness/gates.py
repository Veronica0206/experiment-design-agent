"""Verification gates for the experiment design agent workflow.

Implements the tractable subset of the design-verifier spec (see
.claude/agents/design-verifier.md):
  #1 direction, #2 power separation, #3 regression suite,
  #4 reproducibility, #7 output contract, and the automated part of
  #6 config consistency (check_config_completeness: required params,
  favorable direction, reserved-method rejection).
Not implemented here (documented as out of scope for the automated gate):
  #5 boundary exactness (endpoint-specific closed-form invariants),
  #6 beyond the above (full config-reference comparison, go_target defaults).
"""

from __future__ import annotations

import csv
import io
import math
import os
import stat
from collections import Counter
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import product
from pathlib import Path
from statistics import NormalDist
from typing import Any

from artifact_download import MAX_ARTIFACT_BYTES
from verification import (VerificationStatus, normalize_platform_analysis_methods,
                          platform_interim_reason_code)

MANUAL_CHECKS = [
    "#5 boundary exactness (not automated)",
    "#6 config consistency beyond required-params/direction (not automated)",
]


@dataclass
class GateVerdict:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        # A failed check always dominates. `blocked` lists checks that could not
        # be automated (e.g. boundary exactness); it does NOT fail the gate, but
        # it is surfaced so a partial verification never reads as a full one.
        if not self.passed:
            return VerificationStatus.FAILED.value
        if self.blocked:
            return VerificationStatus.PASS_PARTIAL.value
        return VerificationStatus.VERIFIED.value


# ── R payload shapes ───────────────────────────────────────────────

def _r_vector(payload: Any, key: str) -> Any:
    """Read one R vector field, restoring an array jsonlite auto-unboxed.

    The dispatcher serializes with `auto_unbox = TRUE`, so a length-1 vector
    (one defining word, a one-stage boundary, a single analysis method) arrives
    as a bare scalar, and a length-1 NA arrives as `null`. Treat exactly that
    shape as the one-element array it represents. Data frames are unaffected:
    jsonlite always emits them as arrays of row objects, even for one row.

    An absent key stays absent so a missing field still fails its own contract,
    and any other value is returned unchanged so a malformed payload does too.
    """
    if not isinstance(payload, dict) or key not in payload:
        return None
    value = payload[key]
    if isinstance(value, list):
        return value
    if value is None or isinstance(value, (str, bool, int, float)):
        return [value]
    return value


# ── #3 Regression suite ────────────────────────────────────────────

EXPECTED_REGRESSION_SUITE_COUNT = 5
REGRESSION_STATUS_FIELDS = {
    "all_ok", "suite_count", "passed_suite_count", "failed_suite_count", "checks",
}
REGRESSION_STATUS_CHECKS = {
    "declared_all_ok", "complete_suite_set", "suite_records_valid",
    "expected_check_count",
}

def check_regression_tests(test_result: dict) -> GateVerdict:
    if not isinstance(test_result, dict):
        return GateVerdict(passed=False, failures=["run_tests returned no object"])
    status_checks = test_result.get("checks")
    exact_schema = (
        set(test_result) == REGRESSION_STATUS_FIELDS
        and isinstance(status_checks, dict)
        and set(status_checks) == REGRESSION_STATUS_CHECKS
        and all(isinstance(value, bool) for value in status_checks.values())
    )

    def integer(field: str) -> int | None:
        value = test_result.get(field)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    suite_count = integer("suite_count")
    passed_count = integer("passed_suite_count")
    failed_count = integer("failed_suite_count")
    counts_valid = (
        suite_count == EXPECTED_REGRESSION_SUITE_COUNT
        and passed_count is not None and failed_count is not None
        and passed_count >= 0 and failed_count >= 0
        and passed_count + failed_count == suite_count
        and passed_count == suite_count and failed_count == 0
    )
    public_checks = status_checks if isinstance(status_checks, dict) else {}
    checks = {
        "tests:fixed_status_schema": exact_schema,
        "tests:declared_all_ok": test_result.get("all_ok") is True
        and public_checks.get("declared_all_ok") is True,
        "tests:complete_suite_set": public_checks.get("complete_suite_set") is True,
        "tests:suite_records_valid": public_checks.get("suite_records_valid") is True,
        "tests:expected_check_count": public_checks.get("expected_check_count") is True,
        "tests:aggregate_counts_valid": counts_valid,
    }
    failures = [] if all(checks.values()) else ["run_tests fixed health attestation failed"]
    return GateVerdict(passed=not failures, checks=checks, failures=failures)


# ── #7 Output contract ─────────────────────────────────────────────

def _bad_numbers(obj: Any, path: str = "") -> list[str]:
    """Recursively find NaN/Inf leaves (None is allowed — it's a valid 'NA')."""
    bad: list[str] = []
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            bad.append(path or "<root>")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            bad.extend(_bad_numbers(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad.extend(_bad_numbers(v, f"{path}[{i}]"))
    return bad


def _nested_errors(obj: Any, path: str = "") -> list[str]:
    """Find non-empty error fields at any depth of a tool payload."""
    errors: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}" if path else str(key)
            if key == "error" and value not in (None, "", False):
                errors.append(child)
            else:
                errors.extend(_nested_errors(value, child))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            errors.extend(_nested_errors(value, f"{path}[{index}]"))
    return errors


def check_output_contract(result: dict) -> GateVerdict:
    checks: dict[str, bool] = {}
    failures: list[str] = []
    is_object = isinstance(result, dict) and bool(result)
    checks["nonempty_object"] = is_object
    if not is_object:
        failures.append("tool returned an empty or non-object result")
    nested_errors = _nested_errors(result) if isinstance(result, dict) else []
    checks["no_tool_error"] = not nested_errors
    if nested_errors:
        failures.append("tool returned error field(s) at: " + ", ".join(nested_errors[:5]))
    bad = _bad_numbers(result)
    checks["no_nan_inf"] = not bad
    if bad:
        failures.append(f"NaN/Inf in output at: {', '.join(bad[:5])}")
    return GateVerdict(passed=not failures, checks=checks, failures=failures)


# ── #1 Direction & #2 Power separation (single-endpoint OC) ────────

def _oc_rows(oc: Any) -> list[dict]:
    if not isinstance(oc, list):
        return []
    return [r for r in oc
            if isinstance(r, dict) and r.get("true_param") is not None
            and r.get("p_go") is not None]


def _nearest_row(rows: list[dict], target: float) -> dict | None:
    if target is None or not rows:
        return None
    return min(rows, key=lambda r: abs(r["true_param"] - target))


def check_single_endpoint(result: dict, config: dict) -> GateVerdict:
    checks: dict[str, bool] = {}
    failures: list[str] = []
    notes: list[str] = []
    blocked: list[str] = []

    # Sample-size sanity. Scope to the REQUESTED design and skip legitimate
    # n=NA rows (search-cap exhaustion; compute_sample_size also emits auxiliary
    # single_arm rows for controlled configs) — same NA semantics as
    # check_sample_size: only a numeric non-positive n is a hard failure.
    ss = result.get("sample_size")
    if isinstance(ss, list) and ss:
        req = (config or {}).get("design")
        rows = [r for r in ss if isinstance(r, dict)
                and (req is None or r.get("design") == req)]
        sized = [r for r in rows
                 if isinstance(r.get("n_total"), (int, float))
                 and not isinstance(r.get("n_total"), bool)]
        if sized:
            row = sized[0]
            n = row["n_total"]
            checks["n_positive"] = n > 0
            if n <= 0:
                failures.append(f"n_total={n}, expected positive")
            power = row.get("power_achieved")
            target = row.get("power_target")
            power_ok = (_num(power) and _num(target)
                        and power >= target - 0.05)
            checks["power_meets_target"] = power_ok
            if not power_ok:
                failures.append(
                    f"power_achieved={power!r} does not meet power_target={target!r}"
                )
            # PARTIAL exhaustion must also be surfaced (mirrors check_sample_size):
            # some alpha/power cells sized, others hit the search cap — the agent
            # must report those cells as infeasible, not present the table as
            # uniformly feasible.
            exhausted = sum(1 for r in rows if r.get("n_total") is None)
            if exhausted:
                blocked.append(
                    f"{exhausted} requested-design sizing cell(s) hit the engine "
                    "search cap (n=NA) — report those alpha/power cells as "
                    "infeasible within engine limits."
                )
        elif rows:
            blocked.append(
                "sample-size: every row for the requested design hit the engine "
                "search cap (n=NA) — sizing infeasible within engine limits; "
                "report that honestly"
            )
        else:
            checks["sample_size_present"] = False
            failures.append("no sample_size rows for the requested design")
    else:
        checks["sample_size_present"] = False
        failures.append("no sample_size table in result")

    # #1 Direction + #2 Power separation from the OC curve.
    rows = _oc_rows(result.get("oc"))

    # Any engine-generated OC payload that declares its simulation budget must
    # also carry uncertainty. Low precision is a transparent partial result,
    # not a numerically false failure of the underlying design.
    b_used = result.get("B_used")
    if _num(b_used):
        uncertainty_fields = (
            "p_go_mcse", "p_go_mc_lower", "p_go_mc_upper",
            "mc_replicates", "mc_worst_case_se", "mc_precision_ok",
        )
        uncertainty_complete = bool(rows) and all(
            all(field in row for field in uncertainty_fields) for row in rows
        )
        checks["oc_mc_uncertainty_present"] = uncertainty_complete
        if not uncertainty_complete:
            failures.append("OC simulation output is missing Monte Carlo uncertainty fields")
        else:
            budget_matches = all(row.get("mc_replicates") == b_used for row in rows)
            checks["oc_mc_budget_matches"] = budget_matches
            if not budget_matches:
                failures.append("OC Monte Carlo replicate counts do not match B_used")
            if any(row.get("mc_precision_ok") is not True for row in rows):
                blocked.append(
                    "OC Monte Carlo precision target was not met; increase B_oc "
                    "before using probability estimates for high-stakes decisions"
                )

    ppos = result.get("ppos")
    if isinstance(ppos, dict) and ppos.get("ppos") is not None:
        ppos_fields = {
            "n_mc", "ppos_mcse", "ppos_mc_lower", "ppos_mc_upper",
            "mc_worst_case_se", "mc_precision_ok",
        }
        ppos_uncertainty = ppos_fields.issubset(ppos)
        checks["ppos_mc_uncertainty_present"] = ppos_uncertainty
        if not ppos_uncertainty:
            failures.append("PPOS simulation output is missing Monte Carlo uncertainty fields")
        elif ppos.get("mc_precision_ok") is not True:
            blocked.append(
                "PPOS Monte Carlo precision target was not met; increase n_mc "
                "before high-stakes use"
            )
    null_p = config.get("null_param")
    alt_p = config.get("alt_param")
    row_null = _nearest_row(rows, null_p)
    row_alt = _nearest_row(rows, alt_p)

    if row_null is None or row_alt is None:
        # Direction/separation could NOT be checked — surface as blocked so the
        # verdict reads PASS_PARTIAL, not VERIFIED.
        blocked.append("direction/power-separation: OC curve unavailable")
    else:
        span = abs(alt_p - null_p) if _num(alt_p) and _num(null_p) else 0.0
        tolerance = max(1e-8, span * 0.10)
        null_dist = abs(row_null["true_param"] - null_p)
        alt_dist = abs(row_alt["true_param"] - alt_p)
        checks["oc_targets_represented"] = (
            null_dist <= tolerance and alt_dist <= tolerance
        )
        if not checks["oc_targets_represented"]:
            failures.append(
                "OC curve does not contain rows close enough to the requested "
                f"null/alternative (distances {null_dist:g}, {alt_dist:g}; "
                f"tolerance {tolerance:g})"
            )
    if row_null is not None and row_alt is not None and not failures:
        if row_null["true_param"] == row_alt["true_param"]:
            # Null and alt fell on the same OC grid point; the curve is too
            # coarse to judge direction/separation. Record an explicit partial.
            blocked.append("direction/power-separation: OC grid too coarse to resolve null vs alt")
        else:
            p_null = row_null["p_go"]
            p_alt = row_alt["p_go"]
            # The effective scenario must Go more often than the null scenario.
            checks["direction"] = p_alt > p_null
            if p_alt <= p_null:
                failures.append(
                    f"direction wrong: p_go(alt={alt_p})={p_alt} <= p_go(null={null_p})={p_null}"
                )
            # Spec rule: p_go(alt) >= 3x p_go(null).
            sep_ok = p_alt >= 3 * p_null if p_null > 0 else p_alt > 0
            checks["power_separation"] = sep_ok
            if not sep_ok:
                failures.append(
                    f"weak separation: p_go(alt)={p_alt} not >= 3x p_go(null)={p_null}"
                )
            if p_alt < 0.2:
                notes.append(f"p_go at the alternative is low ({p_alt}); design may be underpowered")

    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       notes=notes, blocked=blocked)


# ── Config completeness + favorable-direction invariant ───────────

# Endpoint-specific params required beyond null_param / alt_param.
_ENDPOINT_REQUIRED = {
    "binary": [],
    "continuous": ["sd"],
    "tte": ["accrual_time", "followup_time"],
    "incidence_rate": ["exposure_time"],
}
# Favorable direction: does the effective (alternative) scenario sit ABOVE or
# BELOW the null? binary/continuous: higher is better (alt > null); tte:
# lower is better (alt < null). Mirrors create_config's validation locks.
# incidence_rate is deliberately ABSENT: the engine supports BOTH directions
# (protective reduction alt < null, harm detection alt > null) and stores the
# direction in the config — either way is a valid design, only alt == null is
# an error.
_ALT_ABOVE_NULL = {
    "binary": True, "continuous": True, "tte": False,
}


def check_config_completeness(config: dict) -> GateVerdict:
    """Endpoint-appropriate config invariants for planning tools that have no OC
    curve (`sample_size`) — and as an extra guard alongside one (`simulate_design`).
    Confirms the required params are present and the alternative is on a valid
    side of the null for the endpoint, so a mis-directed design is caught at
    the config level before any numbers are trusted. incidence_rate accepts
    BOTH directions (the engine analyzes harm detection in the upper tail since
    the direction-threading fix); a harm-direction config is noted, not failed.
    """
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []

    # Reserved-methods defense-in-depth: the MCP schemas expose these knobs as
    # single-value enums, but this gate sees the model's ORIGINAL (un-stripped)
    # arguments in both runtimes — so even if a transport layer silently drops
    # an unknown key, a request for an unimplemented method fails loudly here
    # instead of degrading to Poisson/exponential/no-correction.
    _RESERVED = (("tte_method", "exponential"), ("rate_method", "poisson"),
                 ("fwer_control", "none"))
    for key, implemented in _RESERVED:
        val = config.get(key)
        if val is not None and val != implemented:
            checks[f"{key}_supported"] = False
            failures.append(
                f"{key}={val!r} is not implemented (only {implemented!r}); the R "
                f"layer rejects it and no result may be reported under that label"
            )

    ep = config.get("endpoint_type")
    if ep not in _ENDPOINT_REQUIRED:
        # Unknown/absent endpoint type — can't judge; surface, don't fake a pass.
        blocked.append(f"config-completeness: unrecognized endpoint_type={ep!r}")
        return GateVerdict(passed=not failures, checks=checks, failures=failures,
                           blocked=blocked)

    null_p = config.get("null_param")
    alt_p = config.get("alt_param")
    have_params = (isinstance(null_p, (int, float)) and not isinstance(null_p, bool)
                   and isinstance(alt_p, (int, float)) and not isinstance(alt_p, bool))
    checks["params_present"] = have_params
    if not have_params:
        failures.append("null_param/alt_param missing or non-numeric")

    for req in _ENDPOINT_REQUIRED[ep]:
        present = config.get(req) is not None
        checks[f"has:{req}"] = present
        if not present:
            failures.append(f"{ep} endpoint requires {req}")

    if have_params:
        if alt_p == null_p:
            checks["direction"] = False
            failures.append(f"alt_param == null_param ({alt_p}); no effect to size")
        elif ep == "incidence_rate":
            # Both directions are valid designs; the engine sizes AND analyzes
            # each in its own tail (direction is stored in the config). Note
            # the framing so the agent states it explicitly in its report.
            checks["direction"] = True
            if alt_p > null_p:
                notes.append(
                    f"harm-detection framing (alt={alt_p} > null={null_p}): the "
                    "design is sized and analyzed in the UPPER tail — 'Go' means "
                    "a rate INCREASE signal was detected. State this framing "
                    "explicitly when reporting."
                )
        else:
            ok = (alt_p > null_p) == _ALT_ABOVE_NULL[ep]
            checks["direction"] = ok
            if not ok:
                side = "above" if _ALT_ABOVE_NULL[ep] else "below"
                failures.append(
                    f"direction wrong for {ep}: the favorable alternative must be "
                    f"{side} the null, got alt={alt_p} vs null={null_p}"
                )
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked, notes=notes)


# ── A/B test result sanity + request fidelity ──────────────────────

def check_ab_test(result: dict, args: dict) -> GateVerdict:
    """Bind a two-arm A/B sizing result to the public request.

    The R result reports the *effective* MDE rather than ``effect`` and
    ``effect_type`` separately, so those request fields are normalized to the
    same effective MDE before comparison. Likewise, ``sd`` is not echoed for
    mean outcomes; its use is independently bound by reconstructing the
    closed-form normal-approximation sample size.
    """
    checks: dict[str, bool] = {}
    failures: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["ab_test returned no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=["ab_test returned an error"])
    if not isinstance(args, dict):
        return GateVerdict(passed=False, checks={"request_contract": False},
                           failures=["ab_test request is missing or malformed"])

    # Defaults are the current public MCP defaults and match ab_test_size.
    baseline = args.get("baseline")
    effect = args.get("effect")
    metric = args.get("metric", "proportion")
    effect_type = args.get("effect_type", "absolute")
    alpha = args.get("alpha", 0.05)
    power = args.get("power", 0.8)
    sided = args.get("sided", 2)
    ratio = args.get("ratio", 1)
    sd = args.get("sd")

    numeric_request = all(_num(value) for value in
                          (baseline, effect, alpha, power, ratio))
    request_valid = (
        numeric_request
        and metric in {"proportion", "mean"}
        and effect_type in {"absolute", "relative"}
        and 0 < alpha < 1 and 0 < power < 1 and ratio > 0
        and isinstance(sided, int) and not isinstance(sided, bool)
        and sided in {1, 2}
        and (metric != "mean" or (_num(sd) and sd > 0))
    )
    checks["request_contract"] = request_valid
    if not request_valid:
        return GateVerdict(
            passed=False, checks=checks,
            failures=["ab_test request fields are missing, invalid, or inconsistent"],
        )

    effective_mde = effect if effect_type == "absolute" else baseline * effect
    nonzero_mde = math.isfinite(effective_mde) and effective_mde != 0
    checks["effective_mde_nonzero"] = nonzero_mde
    if not nonzero_mde:
        failures.append("requested effective minimum detectable effect is not finite and non-zero")

    if metric == "proportion":
        treatment_rate = baseline + effective_mde
        rates_valid = 0 < baseline < 1 and 0 < treatment_rate < 1
        checks["proportion_rates_in_bounds"] = rates_valid
        if not rates_valid:
            failures.append("requested control or treatment proportion is outside (0, 1)")

    def _bind_number(check_name: str, result_key: str,
                     expected: int | float) -> bool:
        actual = result.get(result_key)
        try:
            actual_decimal = (Decimal(actual) if isinstance(actual, int)
                              else Decimal(repr(actual)))
            expected_decimal = (Decimal(expected) if isinstance(expected, int)
                                else Decimal(repr(expected)))
            # jsonlite's default encoder rounds ordinary decimals to four
            # places. Keep the tolerance absolute so large means cannot hide
            # materially different requested values behind a relative window.
            ok = (_num(actual)
                  and abs(actual_decimal - expected_decimal) <= Decimal("0.000051"))
        except (InvalidOperation, ValueError, OverflowError):
            ok = False
        checks[check_name] = ok
        if not ok:
            failures.append(f"ab_test result does not match requested {check_name}")
        return ok

    checks["metric_matches_request"] = result.get("metric") == metric
    if not checks["metric_matches_request"]:
        failures.append("ab_test result metric does not match the request")
    _bind_number("baseline", "baseline", baseline)
    _bind_number("effective_mde", "mde", effective_mde)
    result_mde = result.get("mde")
    checks["result_mde_nonzero"] = _num(result_mde) and result_mde != 0
    if not checks["result_mde_nonzero"]:
        failures.append("ab_test result mde is missing or zero")
    _bind_number("alpha", "alpha", alpha)
    _bind_number("target_power", "target_power", power)
    _bind_number("allocation_ratio", "allocation_ratio", ratio)
    result_sided = result.get("sided")
    checks["sided_matches_request"] = (
        isinstance(result_sided, int) and not isinstance(result_sided, bool)
        and result_sided == sided
    )
    if not checks["sided_matches_request"]:
        failures.append("ab_test result sidedness does not match the request")

    def _positive_integer(key: str) -> int | None:
        value = result.get(key)
        ok = isinstance(value, int) and not isinstance(value, bool) and value > 0
        checks[f"{key}_positive_integer"] = ok
        if not ok:
            failures.append(f"ab_test result {key} is not a positive integer")
        return value if ok else None

    n_total = _positive_integer("n_total")
    n_control = _positive_integer("n_control")
    n_treatment = _positive_integer("n_treatment")
    count_sum_ok = (
        n_total is not None and n_control is not None and n_treatment is not None
        and n_control + n_treatment == n_total
    )
    checks["arm_counts_sum_to_total"] = count_sum_ok
    if not count_sum_ok:
        failures.append("ab_test arm counts do not sum to n_total")

    rounding_ok = (
        n_control is not None and n_treatment is not None
        and abs(n_treatment - ratio * n_control) <= 1 + 1e-9
    )
    checks["integer_allocation_matches_ratio"] = rounding_ok
    if not rounding_ok:
        failures.append("ab_test integer arm counts are incompatible with the allocation ratio")

    # Mean sizing is the one place a request parameter (sd) is not directly
    # echoed. Reconstructing n_control proves that the requested sd, alpha,
    # power, sidedness, allocation ratio, and effective MDE all reached sizing.
    if metric == "mean" and nonzero_mde and n_control is not None:
        try:
            z_alpha = NormalDist().inv_cdf(1 - alpha / sided)
            z_power = NormalDist().inv_cdf(power)
            raw_control = (
                ((z_alpha + z_power) * sd / effective_mde) ** 2
                * (1 + 1 / ratio)
            )
            expected_control = math.ceil(raw_control)
            mean_size_ok = abs(n_control - expected_control) <= 1
        except (OverflowError, ValueError):
            mean_size_ok = False
        checks["mean_sd_and_size_match_request"] = mean_size_ok
        if not mean_size_ok:
            failures.append("mean ab_test sample size does not match the requested sd and design")

    target = result.get("target_power")
    achieved = result.get("achieved_power")
    achieved_valid = _num(achieved) and 0 <= achieved <= 1
    checks["achieved_power_valid"] = achieved_valid
    if not achieved_valid:
        failures.append("achieved_power is missing or outside [0, 1]")
    power_ok = _num(target) and achieved_valid and achieved >= target - 0.02
    checks["power_meets_target"] = power_ok
    if not power_ok:
        failures.append("achieved_power is materially below target_power")

    return GateVerdict(passed=not failures, checks=checks, failures=failures)


# ── sample_size table sanity ───────────────────────────────────────

def check_sample_size(result: dict, config: dict) -> GateVerdict:
    """Row-wise sanity for the sample_size table (dispatcher key 'results').

    NA semantics (critical): the engine legitimately returns n_total = NA (JSON
    null) when a power search exhausts its internal cap (exact binomial
    n_max=300, z-unpooled 500, exact Poisson 1000) — that is an honest
    "infeasible within engine caps" answer, not a wrong number. And
    compute_sample_size always emits auxiliary single_arm rows even when a
    controlled design was requested. So: hard-FAIL only genuinely wrong output
    (numeric n_total <= 0, achieved power short of its own target, malformed
    rows); surface search-cap NA rows as `blocked`/`notes` so the verdict reads
    PASS_PARTIAL and the agent reports infeasibility honestly instead of
    being told to "correct" a correct config.

    Note: continuous/t-test rows echo power_achieved = power_target, so the
    power check is structurally vacuous for that endpoint (documented, not a
    defect). Combine with check_config_completeness(config) for the
    direction/required-param check."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []
    if isinstance(result, dict) and result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=[f"tool returned error: {result['error']}"])
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list) or not rows:
        return GateVerdict(passed=False, checks={"table_present": False},
                           failures=["no sample-size table (key 'results') in output"])

    requested = (config or {}).get("design")  # scope hard checks to what was asked
    malformed = 0       # non-dict rows — a broken payload must not pass silently
    bad_n = 0           # numeric but <= 0 — a genuinely wrong number
    exhausted_req = 0   # requested-design rows where the search hit its cap (NA)
    exhausted_aux = 0   # auxiliary rows (other design) with NA — informational
    feasible_req = 0    # requested-design rows with a usable positive n
    short_pwr = 0
    power_unchecked = 0  # sized rows where the power check could not run
    for r in rows:
        if not isinstance(r, dict):
            malformed += 1
            continue
        n = r.get("n_total")
        is_req = requested is None or r.get("design") == requested
        if n is None:
            if is_req:
                exhausted_req += 1
            else:
                exhausted_aux += 1
            continue
        if not (isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0):
            bad_n += 1
            continue
        if is_req:
            feasible_req += 1
        pwr = r.get("power_achieved")
        tgt = r.get("power_target")
        # Only flag a row that falls short of ITS OWN target (a real defect), so a
        # deliberately low power target never false-fails.
        if isinstance(pwr, (int, float)) and isinstance(tgt, (int, float)):
            if pwr < tgt - 0.05:
                short_pwr += 1
        else:
            power_unchecked += 1

    checks["rows_well_formed"] = malformed == 0
    if malformed:
        failures.append(f"{malformed} malformed (non-dict) row(s) in the sample-size table")
    checks["n_positive_where_sized"] = bad_n == 0
    if bad_n:
        failures.append(f"{bad_n} row(s) with non-positive numeric n_total (engine defect)")
    checks["power_meets_target"] = short_pwr == 0
    if short_pwr:
        failures.append(f"{short_pwr} row(s) where achieved power fell short of its target")

    if exhausted_req:
        if feasible_req == 0:
            # Every requested-design cell hit the cap: the honest answer is
            # "infeasible within engine limits", and the agent must be free to
            # SAY that — blocked (partial), not FAIL (which would demand an
            # impossible 'fix' and end in the answer being withheld).
            blocked.append(
                f"sizing infeasible within engine search caps: all {exhausted_req} "
                f"{requested or 'requested'} row(s) returned n=NA (search exhausted). "
                "Report this honestly — the effect is too small to size at these "
                "alpha/power within engine limits; do not invent an n."
            )
        else:
            blocked.append(
                f"{exhausted_req} {requested or 'requested'} row(s) hit the engine's "
                "sample-size search cap (n=NA) — report those alpha/power cells as "
                "infeasible within engine limits; the remaining rows are usable."
            )
    if exhausted_aux:
        notes.append(
            f"{exhausted_aux} auxiliary row(s) for the non-requested design hit the "
            "search cap (informational only)"
        )
    if power_unchecked:
        blocked.append(f"power-vs-target check not run for {power_unchecked} sized "
                       "row(s) (missing power fields)")
    if requested is not None and feasible_req == 0 and exhausted_req == 0:
        # No row of any kind for the design that was asked for — an engine change
        # or contract drift; must not pass vacuously.
        checks["requested_design_present"] = False
        failures.append(f"no sample-size rows for the requested design {requested!r}")

    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked, notes=notes)


# ── Master (multi-arm) presence check ──────────────────────────────

MAX_MASTER_CSV_ROWS = 10_000
MASTER_ARTIFACT_READ_CHUNK_BYTES = 64 * 1024


class _MasterArtifactError(ValueError):
    """Raised when a master artifact cannot be inspected safely."""


def _secure_artifact_flags(*, directory: bool = False) -> int:
    required = ("O_NOFOLLOW", "O_NONBLOCK") + (("O_DIRECTORY",) if directory else ())
    if not all(hasattr(os, flag) for flag in required):
        raise _MasterArtifactError("secure artifact traversal is unavailable")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags |= os.O_DIRECTORY
    return flags


def _open_master_output_directory(path: Path) -> int:
    try:
        fd = os.open(path, _secure_artifact_flags(directory=True))
    except (OSError, _MasterArtifactError) as exc:
        raise _MasterArtifactError("master output directory is unavailable or unsafe") from exc
    try:
        descriptor = os.fstat(fd)
    except OSError as exc:
        os.close(fd)
        raise _MasterArtifactError("master output directory could not be inspected") from exc
    if not stat.S_ISDIR(descriptor.st_mode):
        os.close(fd)
        raise _MasterArtifactError("master output path is not a directory")
    return fd


def _read_master_artifact(
    directory_fd: int,
    filename: str,
    *,
    prefix_bytes: int | None = None,
) -> bytes:
    """Read a regular artifact through its pinned directory, within fixed bounds."""
    try:
        fd = os.open(filename, _secure_artifact_flags(), dir_fd=directory_fd)
    except (OSError, _MasterArtifactError) as exc:
        raise _MasterArtifactError("master artifact is unavailable or unsafe") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise _MasterArtifactError("master artifact is not a regular file")
        if before.st_size <= 0:
            raise _MasterArtifactError("master artifact is empty")
        if before.st_size > MAX_ARTIFACT_BYTES:
            raise _MasterArtifactError("master artifact exceeds the size limit")

        target = before.st_size if prefix_bytes is None else min(before.st_size, prefix_bytes)
        chunks: list[bytes] = []
        bytes_read = 0
        while bytes_read < target:
            chunk = os.read(
                fd,
                min(MASTER_ARTIFACT_READ_CHUNK_BYTES, target - bytes_read),
            )
            if not chunk:
                break
            bytes_read += len(chunk)
            chunks.append(chunk)

        after = os.fstat(fd)
        if after.st_size != before.st_size or bytes_read != target:
            raise _MasterArtifactError("master artifact size changed during verification")
        return b"".join(chunks)
    except OSError as exc:
        raise _MasterArtifactError("master artifact could not be read safely") from exc
    finally:
        os.close(fd)


def _validate_master_csv(
    payload: bytes,
    required_headers: set[str],
    expected_rows: Any,
) -> tuple[bool, bool, bool | None]:
    """Validate a byte-bounded CSV without retaining an unbounded row list."""
    nonfinite = {
        "nan", "+nan", "-nan", "inf", "+inf", "-inf",
        "infinity", "+infinity", "-infinity",
    }
    row_count = 0
    finite_ok = True
    consistent = True if isinstance(expected_rows, list) else None
    try:
        with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            headers = set(reader.fieldnames or [])
            for row in reader:
                row_count += 1
                if row_count > MAX_MASTER_CSV_ROWS:
                    raise _MasterArtifactError("master CSV exceeds the row limit")
                finite_ok = finite_ok and all(
                    str(value).strip().lower() not in nonfinite for value in row.values()
                )
                if isinstance(expected_rows, list):
                    consistent = bool(
                        consistent
                        and row_count <= len(expected_rows)
                        and _csv_row_matches_result(row, expected_rows[row_count - 1])
                    )
    except (UnicodeError, csv.Error, _MasterArtifactError):
        return False, False, False if isinstance(expected_rows, list) else None
    csv_ok = row_count > 0 and required_headers.issubset(headers)
    if isinstance(expected_rows, list):
        consistent = bool(consistent and row_count == len(expected_rows))
    return csv_ok, finite_ok, consistent

def check_master_result(result: dict, config: dict | None = None) -> GateVerdict:
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    oc: Any = None
    family = (config or {}).get("master_design_type")
    if result.get("error"):
        failures.append(f"tool returned error: {result['error']}")
    inner = result.get("result") if isinstance(result, dict) else None
    checks["result_present"] = isinstance(inner, dict) and bool(inner)
    if not checks["result_present"]:
        failures.append("no 'result' payload from master_simulate")
    else:
        oc = inner.get("oc_table")
        checks["oc_table_present"] = isinstance(oc, list) and bool(oc)
        if not checks["oc_table_present"]:
            failures.append("master_simulate result has no non-empty oc_table")
        else:
            # Every master engine reports simulated rejection/power
            # probabilities. Require family-specific MCSEs, Wilson intervals,
            # and a precision flag before those percentages can pass as a
            # scientifically interpretable result.
            mc_specs = {
                "basket": ("oc_table", "reject_rate", "reject_mcse_pct",
                           "reject_ci_lower_pct", "reject_ci_upper_pct",
                           "reject_precision_met"),
                "umbrella": ("oc_table", "per_arm_power", "power_mcse_pct",
                             "power_ci_lower_pct", "power_ci_upper_pct",
                             "power_precision_met"),
                "platform": ("arm_results", "reject_rate", "reject_mcse_pct",
                             "reject_ci_lower_pct", "reject_ci_upper_pct",
                             "reject_precision_met"),
            }
            spec = mc_specs.get(family)
            if spec is not None:
                table_name, estimate_key, se_key, lower_key, upper_key, precision_key = spec
                rows = inner.get(table_name)
                mc_contract = isinstance(rows, list) and bool(rows)
                low_precision = False
                if mc_contract:
                    for index, row in enumerate(rows):
                        values = [row.get(estimate_key), row.get(se_key),
                                  row.get(lower_key), row.get(upper_key)] \
                            if isinstance(row, dict) else []
                        if (len(values) != 4 or not all(_num(value) for value in values)
                                or not isinstance(row.get(precision_key), bool)):
                            mc_contract = False
                            failures.append(
                                f"{table_name} row {index + 1} is missing numeric "
                                "Monte Carlo SE/CI fields or a boolean precision flag"
                            )
                            continue
                        estimate, se, lower, upper = values
                        row_ok = (
                            0 <= estimate <= 100 and se >= 0
                            and 0 <= lower <= upper <= 100
                            and lower - 0.11 <= estimate <= upper + 0.11
                        )
                        if not row_ok:
                            mc_contract = False
                            failures.append(
                                f"{table_name} row {index + 1} has an invalid "
                                f"Monte Carlo interval [{lower}, {upper}] for {estimate_key}={estimate}"
                            )
                        low_precision = low_precision or row.get(precision_key) is False
                checks["master_mc_uncertainty_valid"] = mc_contract
                if not mc_contract and not any("Monte Carlo" in item for item in failures):
                    failures.append(
                        f"master {family} result lacks a valid Monte Carlo uncertainty contract"
                    )
                if low_precision:
                    blocked.append(
                        "master simulation did not meet its Monte Carlo precision target; "
                        "increase n_sims before high-stakes use"
                    )

            target = inner.get("mc_precision_target_probability_half_width")
            target_ok = _num(target) and 0 < target < 0.5
            checks["master_mc_precision_target_declared"] = target_ok
            if not target_ok:
                failures.append(
                    "master result must declare a probability half-width precision target"
                )

            # Only the MAMS engine calibrates efficacy boundaries, and it must
            # always declare them: treating an absent or null boundary_source as
            # "nothing to check" would let a silent contract change skip the
            # whole validation. Other umbrella methods are still validated
            # whenever they do report a boundary source.
            umbrella_method = (config or {}).get("umbrella_method") or "mams"
            if family == "umbrella" and (
                umbrella_method == "mams" or inner.get("boundary_source") is not None
            ):
                source = inner.get("boundary_source")
                approximation = inner.get("boundary_approximation")
                bounds = inner.get("boundaries")
                # A single-stage design reports one boundary per vector, which
                # the dispatcher unboxes to a scalar (or to null when efficacy
                # stopping is disabled at that stage).
                effect_bounds = _r_vector(bounds, "effect")
                efficacy_enabled = _r_vector(bounds, "efficacy_enabled_by_stage")
                # Equal-length vectors are not enough: both must cover exactly
                # the stages the design actually runs, or a design could report
                # one boundary for a multi-stage plan.
                requested_stages = (config or {}).get("n_stages", 2)
                expected_stages = (
                    int(requested_stages)
                    if _num(requested_stages) and int(requested_stages) == requested_stages
                    and requested_stages >= 1 else None
                )
                fallback_reason = inner.get("boundary_fallback_reason")
                has_user_boundaries = (
                    (config or {}).get("futility_boundaries") is not None
                )
                package_flags = (
                    [True] * expected_stages if expected_stages is not None else None
                )
                fallback_flags = (
                    [False] * (expected_stages - 1) + [True]
                    if expected_stages is not None else None
                )
                fallback_declared = (
                    isinstance(fallback_reason, str)
                    and bool(fallback_reason.strip())
                )
                source_contract = (
                    (
                        source == "MAMS_package"
                        and approximation is False
                        and not has_user_boundaries
                        and efficacy_enabled == package_flags
                        and fallback_reason is None
                    )
                    or (
                        source == "approximation"
                        and approximation is True
                        and not has_user_boundaries
                        and efficacy_enabled == fallback_flags
                        and fallback_declared
                    )
                    or (
                        source == "user_supplied"
                        and approximation is True
                        and has_user_boundaries
                        and efficacy_enabled == fallback_flags
                        and fallback_declared
                    )
                )
                boundary_contract = (
                    source_contract
                    and isinstance(inner.get("decision_rule"), str)
                    and bool(inner.get("decision_rule"))
                    and isinstance(bounds, dict)
                    and isinstance(effect_bounds, list)
                    and isinstance(efficacy_enabled, list)
                    and len(effect_bounds) == len(efficacy_enabled)
                    and expected_stages is not None
                    and len(effect_bounds) == expected_stages
                    and all(isinstance(flag, bool) for flag in efficacy_enabled)
                    and all(
                        (_num(bound) if enabled else bound is None)
                        for bound, enabled in zip(effect_bounds, efficacy_enabled)
                    )
                )
                checks["mams_boundary_contract"] = boundary_contract
                if not boundary_contract:
                    failures.append(
                        "MAMS result does not distinguish package calibration from "
                        "approximation or reports inconsistent efficacy boundaries"
                    )

            if family == "platform":
                # A run that uses one analysis method reports a length-1 vector,
                # which the dispatcher unboxes to a bare string.
                actual_methods = _r_vector(inner, "actual_analysis_methods")
                # Only hashable strings enter the set: a malformed payload such
                # as [{}] must fail the contract below, not raise before this
                # check can return a verdict.
                normalized_actual_methods = normalize_platform_analysis_methods(
                    actual_methods,
                )
                actual_methods_valid = normalized_actual_methods is not None
                declared_methods = set(normalized_actual_methods or [])
                # Each arm reports the ";"-joined set of methods it actually
                # used, and the declared list is the union of those sets over
                # every arm and period. Requiring exact set equality rejects both
                # a declared method no arm ran and an arm method never declared.
                arm_rows = inner.get("arm_results")
                per_arm_methods = (
                    [row.get("actual_analysis_method") for row in arm_rows
                     if isinstance(row, dict)]
                    if isinstance(arm_rows, list) else []
                )
                arm_tokens: set[str] = set()
                arm_labels_valid = bool(per_arm_methods)
                for method in per_arm_methods:
                    tokens = normalize_platform_analysis_methods(method)
                    if tokens is None:
                        arm_labels_valid = False
                        continue
                    arm_tokens.update(tokens)
                methods_reconciled = arm_labels_valid and arm_tokens == declared_methods
                platform_contract = (
                    isinstance(inner.get("interim_futility_enabled"), bool)
                    and inner.get("interim_efficacy_enabled") is False
                    and inner.get("interim_stopping_applied")
                    == inner.get("interim_futility_enabled")
                    and platform_interim_reason_code(
                        inner.get("interim_stopping_reason")
                    ) is not None
                    and actual_methods_valid
                    and methods_reconciled
                )
                requested = (config or {}).get("ncc_method")
                if requested is not None:
                    platform_contract = platform_contract and inner.get(
                        "requested_ncc_method"
                    ) == requested
                checks["platform_actual_behavior_declared"] = platform_contract
                if not platform_contract:
                    failures.append(
                        "platform result does not accurately declare enabled interim "
                        "behavior and actual analysis methods"
                    )
    output_dir = result.get("output_dir") if isinstance(result, dict) else None
    output_path = Path(output_dir) if isinstance(output_dir, str) and output_dir else None
    directory_fd: int | None = None
    if output_path is not None:
        try:
            directory_fd = _open_master_output_directory(output_path)
        except _MasterArtifactError:
            directory_fd = None
    checks["output_dir_persists"] = directory_fd is not None
    if not checks["output_dir_persists"]:
        failures.append("master_simulate output_dir is missing or does not persist")
    else:
        expected_by_family = {
            "basket": ["basket_oc_table.csv", "basket_fwer.csv",
                       "basket_subgroup_decisions.csv", "basket_oc_curves.pdf"],
            "umbrella": ["umbrella_power_table.csv", "umbrella_arm_comparison.pdf"],
            "platform": ["platform_arm_results.csv", "platform_oc_table.csv",
                         "platform_timeline.pdf", "platform_oc_curves.pdf"],
        }
        expected = expected_by_family.get(family)
        checks["known_master_design_type"] = expected is not None
        if expected is None:
            failures.append(f"unknown or missing master_design_type: {family!r}")
            expected = []
        required_headers = {
            "basket_oc_table.csv": {"subgroup", "null_param", "alt_param", "reject_rate",
                                    "reject_mcse_pct", "reject_ci_lower_pct",
                                    "reject_ci_upper_pct", "reject_precision_met"},
            "basket_fwer.csv": {"scenario", "fwer", "fwer_mcse_pct",
                                "fwer_ci_lower_pct", "fwer_ci_upper_pct",
                                "precision_met", "n_simulations"},
            "basket_subgroup_decisions.csv": {"subgroup", "decision"},
            "umbrella_power_table.csv": {"arm", "per_arm_power", "power_mcse_pct",
                                         "power_ci_lower_pct", "power_ci_upper_pct",
                                         "power_precision_met"},
            "platform_arm_results.csv": {"arm", "reject_rate", "mean_n",
                                         "requested_ncc_method", "actual_analysis_method",
                                         "reject_mcse_pct", "reject_ci_lower_pct",
                                         "reject_ci_upper_pct", "reject_precision_met"},
            "platform_oc_table.csv": {"metric", "value", "mcse", "ci_lower",
                                      "ci_upper", "precision_met", "n_simulations"},
        }
        result_table_by_file = {
            "basket_oc_table.csv": "oc_table",
            "basket_fwer.csv": "fwer_table",
            "umbrella_power_table.csv": "oc_table",
            "platform_arm_results.csv": "arm_results",
            "platform_oc_table.csv": "oc_table",
        }
        try:
            for filename in expected:
                try:
                    payload = _read_master_artifact(
                        directory_fd,
                        filename,
                        prefix_bytes=5 if filename.endswith(".pdf") else None,
                    )
                except _MasterArtifactError:
                    checks[f"artifact:{filename}"] = False
                    failures.append(
                        f"expected master artifact missing, empty, oversized, or unsafe: {filename}"
                    )
                    continue
                checks[f"artifact:{filename}"] = True
                if filename.endswith(".pdf"):
                    pdf_ok = payload == b"%PDF-"
                    checks[f"artifact_pdf:{filename}"] = pdf_ok
                    if not pdf_ok:
                        failures.append(f"master PDF artifact is malformed: {filename}")
                elif filename.endswith(".csv"):
                    result_key = result_table_by_file.get(filename)
                    expected_rows = (
                        inner.get(result_key) if isinstance(inner, dict) and result_key else None
                    )
                    csv_ok, finite_ok, consistent = _validate_master_csv(
                        payload, required_headers.get(filename, set()), expected_rows,
                    )
                    checks[f"artifact_schema:{filename}"] = csv_ok
                    checks[f"artifact_finite:{filename}"] = finite_ok
                    if not csv_ok:
                        failures.append(
                            f"master artifact has invalid CSV header/data rows: {filename}"
                        )
                    if not finite_ok:
                        failures.append(
                            f"master artifact contains non-finite values: {filename}"
                        )
                    if consistent is not None:
                        checks[f"artifact_rows_match_result:{filename}"] = consistent
                        if not consistent:
                            failures.append(
                                f"{filename} content does not match the returned oc_table"
                            )
        finally:
            os.close(directory_fd)
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked)


def _csv_row_matches_result(csv_row: dict[str, str], result_row: Any) -> bool:
    if not isinstance(result_row, dict):
        return False
    compared = 0
    for key, expected in result_row.items():
        if key not in csv_row or isinstance(expected, (dict, list)):
            continue
        actual = (csv_row.get(key) or "").strip()
        compared += 1
        if expected is None:
            if actual.lower() not in {"", "na", "null"}:
                return False
        elif isinstance(expected, bool):
            if actual.lower() not in ({"true", "t", "1"} if expected else {"false", "f", "0"}):
                return False
        elif _num(expected):
            try:
                parsed = float(actual)
            except (TypeError, ValueError, OverflowError):
                return False
            if not math.isfinite(parsed) or not _numbers_close(expected, parsed, 1e-6):
                return False
        elif actual != str(expected):
            return False
    return compared > 0


def check_master_config_reserved(config: dict) -> GateVerdict:
    """Reserved/inert-knob scan for master_simulate configs — defense-in-depth
    on the model's ORIGINAL arguments (the R layer rejects these too, but a
    transport layer that strips unknown keys would make that rejection
    unreachable; this gate sees the un-stripped args in both runtimes).
    Mirrors create_master_config's truth-in-advertising block."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    reserved = (("tte_method", "exponential"), ("rate_method", "poisson"),
                ("fwer_control", "none"), ("power_type", "one_minimum"))
    for key, implemented in reserved:
        val = config.get(key)
        if val is not None and val != implemented:
            checks[f"{key}_supported"] = False
            failures.append(
                f"{key}={val!r} is not implemented (only {implemented!r}); no "
                "result may be reported under that label")
    if config.get("rar_eta") is not None:
        checks["rar_eta_supported"] = False
        failures.append("rar_eta is not implemented — no engine reads it")
    if config.get("shared_control") not in (None, True):
        checks["shared_control_supported"] = False
        failures.append("shared_control=False is not implemented — platform "
                        "simulations always share control (see ncc_method)")
    if config.get("overdispersion") is not None:
        checks["overdispersion_supported"] = False
        failures.append("overdispersion is reserved (future negbin) and read by "
                        "no engine — omit it")
    if config.get("effect_threshold") is not None:
        checks["effect_threshold_supported"] = False
        failures.append(
            "effect_threshold is not implemented — platform interim efficacy "
            "stopping is disabled until a calibrated multiplicity procedure exists"
        )
    if config.get("master_design_type") == "platform":
        endpoint = config.get("endpoint_type")
        ncc_method = config.get("ncc_method", "regression")
        interim_supported = endpoint in {"binary", "continuous"} and ncc_method == "none"
        if not interim_supported and any(
            config.get(key) is not None
            for key in ("interim_frequency", "futility_threshold")
        ):
            checks["platform_interim_settings_supported"] = False
            failures.append(
                "interim_frequency/futility_threshold are unavailable for this "
                "endpoint and NCC method because no consistent interim model is implemented"
            )
    endpoint = config.get("endpoint_type")
    nulls = config.get("null_params")
    alts = config.get("alt_params")
    if isinstance(nulls, (int, float)):
        nulls = [nulls]
    if isinstance(alts, (int, float)):
        alts = [alts]
    if isinstance(nulls, list) and isinstance(alts, list) and len(nulls) == len(alts):
        pairs = [(a, n) for a, n in zip(alts, nulls) if _num(a) and _num(n)]
        if endpoint in ("binary", "continuous") and any(a < n for a, n in pairs):
            failures.append("master binary/continuous alternatives must be >= nulls")
        if endpoint in ("tte", "incidence_rate") and any(a > n for a, n in pairs):
            failures.append("master TTE/incidence-rate alternatives must be <= nulls")
    sel = config.get("selection_rule")
    if sel is not None:
        derived = ("rank_best" if config.get("umbrella_method") == "drop_the_losers"
                   else "threshold")
        if config.get("master_design_type") != "umbrella" or sel != derived:
            checks["selection_rule_supported"] = False
            failures.append(
                f"selection_rule={sel!r} is not implemented — stage selection is "
                "fixed per method (rank-best for drop_the_losers, threshold "
                "otherwise) and this parameter is not read by any engine")
    return GateVerdict(passed=not failures, checks=checks, failures=failures)


# ── #4 Reproducibility ─────────────────────────────────────────────

_VOLATILE_REPRO_KEYS = {
    "output_dir", "log", "timestamp", "created_at", "duration_ms",
    "_verification", "_provenance", "verification", "provenance",
    "analysis_id", "call_id", "verification_id", "lineage_id",
    "args_hash", "result_hash", "private_assignment_artifact",
    "_private_resources", "_private_provenance",
}


def _stable_leaves(obj: Any, path: str = "") -> dict[str, Any]:
    """Flatten every stable scalar so decisions/labels are compared too."""
    out: dict[str, Any] = {}
    if obj is None or isinstance(obj, (bool, str, int, float)):
        out[path or "<root>"] = obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in _VOLATILE_REPRO_KEYS:
                out.update(_stable_leaves(v, f"{path}.{k}" if path else str(k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(_stable_leaves(v, f"{path}[{i}]"))
    return out


def check_reproducibility(result_a: dict, result_b: dict, tol: float = 1e-9) -> GateVerdict:
    """Two same-seed runs must match on every stable output field."""
    # A transiently failed re-run is NOT a seeding bug — report the real cause
    # instead of "N numeric field(s) differ" (which sends the agent chasing a
    # nonexistent reproducibility defect).
    for label, res in (("first", result_a), ("re-run", result_b)):
        if isinstance(res, dict) and res.get("error"):
            return GateVerdict(
                passed=False, checks={"reproducible": False},
                failures=[f"reproducibility check could not run: the {label} "
                          f"execution returned an error: {res['error']}"])
    a = _stable_leaves(result_a)
    b = _stable_leaves(result_b)
    failures: list[str] = []
    mismatches = 0
    if not a or not b:
        return GateVerdict(
            passed=False,
            checks={"reproducible": False},
            failures=["reproducibility comparison had no stable output fields"],
        )
    for key in set(a) | set(b):
        va, vb = a.get(key), b.get(key)
        if key not in a or key not in b:
            mismatches += 1
        elif isinstance(va, float) and not math.isfinite(va) or \
                isinstance(vb, float) and not math.isfinite(vb):
            mismatches += 1
            if len(failures) < 5:
                failures.append(f"{key}: non-finite value cannot be reproducible")
        elif isinstance(va, bool) != isinstance(vb, bool):
            mismatches += 1
            if len(failures) < 5:
                failures.append(f"{key}: type mismatch {type(va).__name__} != {type(vb).__name__}")
        elif _num(va) and _num(vb) and not _numbers_close(va, vb, tol):
            mismatches += 1
            if len(failures) < 5:
                failures.append(f"{key}: {va} != {vb}")
        elif not (_num(va) and _num(vb)) and va != vb:
            mismatches += 1
            if len(failures) < 5:
                failures.append(f"{key}: {va} != {vb}")
    checks = {"reproducible": mismatches == 0}
    if mismatches:
        failures.insert(0, f"{mismatches} stable field(s) differ across same-seed runs")
    return GateVerdict(passed=mismatches == 0, checks=checks, failures=failures)


# ── Bucher indirect-comparison invariants ──────────────────────────

def _num(v) -> bool:
    return (
        isinstance(v, int) and not isinstance(v, bool)
        or isinstance(v, float) and math.isfinite(v)
    )


def _numbers_close(a: int | float, b: int | float, tol: float) -> bool:
    """Compare JSON numerics without lossy int-to-float conversion."""
    if isinstance(a, int) and isinstance(b, int):
        return a == b
    try:
        da = Decimal(a) if isinstance(a, int) else Decimal(repr(a))
        db = Decimal(b) if isinstance(b, int) else Decimal(repr(b))
        scale = max(Decimal(1), abs(da), abs(db))
        return abs(da - db) <= Decimal(repr(tol)) * scale
    except (InvalidOperation, ValueError, OverflowError):
        return a == b


def _close(a: float, b: float, tol: float = 5e-3) -> bool:
    # The dispatcher's JSON serialization rounds numerics (~4 decimals);
    # tolerances must absorb that while still catching real inversions.
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def check_bucher(result: dict, args: dict) -> GateVerdict:
    """Exact algebraic invariants of the Bucher combination, recomputed from the
    caller's own inputs: estimate = est_ab − est_cb, se = sqrt(se_ab²+se_cb²),
    CI = estimate ± z·se, and the indirect SE always exceeds each direct SE.
    Applies to method='bucher' only; MAIC gets a blocked marker instead."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=[f"tool returned error: {result['error']}"])
    if (args or {}).get("method") == "maic":
        summary = result.get("weight_summary")
        ess = result.get("ess")
        balance = result.get("balance")
        leaked = any(key in result for key in ("weights", "ipd", "participants"))
        checks["maic_no_row_level_payload"] = not leaked
        checks["maic_weight_summary_present"] = isinstance(summary, dict)
        checks["maic_ess_present"] = isinstance(ess, list) and bool(ess)
        checks["maic_balance_present"] = isinstance(balance, list) and bool(balance)
        if leaked:
            failures.append("MAIC output exposes row-level weights/IPD")
        if not isinstance(summary, dict):
            failures.append("MAIC output missing aggregate weight_summary")
        if not (isinstance(ess, list) and ess):
            failures.append("MAIC output missing ESS summary")
        if not (isinstance(balance, list) and balance):
            failures.append("MAIC output missing balance summary")
        return GateVerdict(passed=not failures, checks=checks, failures=failures)

    import math as _m
    rows = result.get("comparisons")
    inputs = (args or {}).get("comparisons")
    if not isinstance(rows, list) or not rows:
        return GateVerdict(passed=False, checks={"comparisons_present": False},
                           failures=["no 'comparisons' in indirect_compare output"])
    if not isinstance(inputs, list) or len(inputs) != len(rows):
        return GateVerdict(passed=False, checks={"comparison_counts_match": False},
                           failures=["Bucher input/output comparison counts differ"])

    for i, (row, inp) in enumerate(zip(rows, inputs)):
        if isinstance(row, list) and row:      # 1-row data.frame serializes as [obj]
            row = row[0]
        if not (isinstance(row, dict) and isinstance(inp, dict)):
            failures.append(f"comparison {i + 1}: malformed row")
            continue
        est, se = row.get("estimate"), row.get("se")
        lo, hi = row.get("lower"), row.get("upper")
        eab, ecb = inp.get("estimate_ab"), inp.get("estimate_cb")
        sab, scb = inp.get("se_ab"), inp.get("se_cb")
        if not all(_num(v) for v in (est, se, lo, hi, eab, ecb, sab, scb)):
            failures.append(f"comparison {i + 1}: required numeric fields missing")
            continue
        alpha = inp.get("alpha") if _num(inp.get("alpha")) else 0.05
        # z for the two-sided (1-alpha) CI via erfc inverse-free approximation:
        # use the exact relation through statistics.NormalDist if available.
        try:
            from statistics import NormalDist
            z = NormalDist().inv_cdf(1 - alpha / 2)
        except Exception:
            z = 1.959964
        ok_est = _close(est, eab - ecb)
        ok_se = _close(se, _m.sqrt(sab * sab + scb * scb))
        ok_ci = _close(lo, est - z * se) and _close(hi, est + z * se) and lo <= est <= hi
        ok_wider = se >= max(sab, scb) - 1e-9  # indirect is never more precise
        checks[f"cmp{i + 1}_estimate"] = ok_est
        checks[f"cmp{i + 1}_se"] = ok_se
        checks[f"cmp{i + 1}_ci"] = ok_ci
        checks[f"cmp{i + 1}_se_wider_than_direct"] = ok_wider
        if not ok_est:
            failures.append(f"comparison {i + 1}: estimate {est} != est_ab-est_cb "
                            f"({eab}-{ecb})")
        if not ok_se:
            failures.append(f"comparison {i + 1}: se {se} != sqrt(se_ab^2+se_cb^2)")
        if not ok_ci:
            failures.append(f"comparison {i + 1}: CI [{lo}, {hi}] does not reconstruct "
                            f"from estimate ± z*se at alpha={alpha}")
        if not ok_wider:
            failures.append(f"comparison {i + 1}: indirect se {se} smaller than a "
                            f"direct se (impossible for Bucher)")
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked)


# ── Meta-analysis invariants ───────────────────────────────────────

def check_meta(result: dict, args: dict) -> GateVerdict:
    """Deterministic invariants of inverse-variance pooling: CI reconstructs
    from estimate ± z·se, the pooled estimate is a convex combination of the
    study effects (so it lies inside their range), heterogeneity stats are in
    bounds, and any silently dropped study is surfaced for the report."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    notes: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=[f"tool returned error: {result['error']}"])

    effect_measure = result.get("effect_measure")
    measure_present = isinstance(effect_measure, str) and bool(effect_measure.strip())
    checks["effect_measure_present"] = measure_present
    if not measure_present:
        failures.append("meta output missing the common effect_measure")

    study_measures = result.get("study_effect_measures")
    if isinstance(study_measures, list):
        used_measures = {m for m in study_measures
                         if isinstance(m, str) and m.strip()}
        common_scale = len(used_measures) == 1 and effect_measure in used_measures
        checks["study_measures_commensurate"] = common_scale
        if not common_scale:
            failures.append(
                "per-study effect measures are mixed or disagree with effect_measure: "
                + ", ".join(sorted(used_measures))
            )

    requested_studies = (args or {}).get("studies")
    if isinstance(requested_studies, list):
        requested_measures = {
            row.get("measure") for row in requested_studies
            if isinstance(row, dict) and isinstance(row.get("measure"), str)
            and row.get("measure").strip()
        }
        if len(requested_measures) > 1:
            checks["requested_measures_commensurate"] = False
            failures.append(
                "request contains mixed per-study effect measures: "
                + ", ".join(sorted(requested_measures))
            )
        elif len(requested_measures) == 1 and measure_present:
            expected = next(iter(requested_measures))
            agrees = effect_measure == expected
            checks["effect_measure_matches_request"] = agrees
            if not agrees:
                failures.append(
                    f"reported effect_measure={effect_measure!r} does not match "
                    f"requested measure={expected!r}"
                )

    est, se = result.get("estimate"), result.get("se")
    lo, hi = result.get("lower"), result.get("upper")
    if all(_num(v) for v in (est, se, lo, hi)):
        alpha = (args or {}).get("alpha") if _num((args or {}).get("alpha")) else 0.05
        try:
            from statistics import NormalDist
            z = NormalDist().inv_cdf(1 - alpha / 2)
        except Exception:
            z = 1.959964
        method = str(result.get("inference_method") or "").lower()
        if "hksj" in method:
            left_width = est - lo
            right_width = hi - est
            implied_critical = right_width / se if se > 0 else float("nan")
            ok_ci = (
                lo <= est <= hi and se > 0
                and _close(left_width, right_width)
                and math.isfinite(implied_critical)
                and implied_critical >= z - 5e-3
            )
        else:
            ok_ci = (_close(lo, est - z * se) and _close(hi, est + z * se)
                     and lo <= est <= hi and se > 0)
        checks["ci_reconstructs"] = ok_ci
        if not ok_ci:
            failures.append(f"CI [{lo}, {hi}] is inconsistent with "
                            f"estimate={est}, se={se}, alpha={alpha}, and method={method!r}")
    else:
        failures.append("meta output missing numeric estimate/se/lower/upper")
    if not (_num(result.get("k")) or _num(result.get("k_used"))):
        failures.append("meta output missing numeric k/k_used")

    tau2, i2, q = result.get("tau2"), result.get("i2"), result.get("q")
    if not all(_num(v) for v in (tau2, i2, q)):
        failures.append("meta output missing numeric tau2/i2/q")
    if _num(tau2):
        checks["tau2_nonneg"] = tau2 >= 0
        if tau2 < 0:
            failures.append(f"tau2={tau2} negative")
    if _num(i2):
        checks["i2_in_bounds"] = 0 <= i2 <= 100
        if not 0 <= i2 <= 100:
            failures.append(f"I^2={i2} outside [0, 100]")
    if _num(q):
        checks["q_nonneg"] = q >= 0
        if q < 0:
            failures.append(f"Q={q} negative")

    effects = result.get("study_effects")
    range_checked = False
    if isinstance(effects, list):
        used = [e for e in effects if _num(e)]
        if used and _num(est):
            range_checked = True
            ok_convex = min(used) - 5e-3 <= est <= max(used) + 5e-3
            checks["pooled_within_study_range"] = ok_convex
            if not ok_convex:
                failures.append(
                    f"pooled estimate {est} lies outside the study-effect range "
                    f"[{min(used)}, {max(used)}] — impossible for IV pooling")
    if not range_checked:
        blocked.append(
            "study-effect range check not run because per-study effects were unavailable"
        )

    dropped = result.get("dropped_studies")
    if _num(dropped) and dropped > 0:
        notes.append(f"{int(dropped)} of {result.get('n_input')} studies were "
                     "excluded from pooling (invalid effect/variance) — report "
                     "the exclusion, not just the pooled result")
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked, notes=notes)


# ── Randomization plan invariants + request fidelity ───────────────

def _integer_allocation_weights(values: list[int | float]) -> list[int] | None:
    """Normalize positive JSON weights to their smallest integer allocation.

    Allocation weights describe proportions, so their common magnitude must
    not affect the verified quota.  Convert the JSON decimal spellings to exact
    fractions, divide by the smallest weight, and only then apply the bounded
    rational approximation.  This makes ``[1, 2]`` and
    ``[0.00001, 0.00002]`` equivalent while retaining the 10,000-run public
    block-size ceiling.

    Decimal JSON numbers such as 1.3333333333 are transport approximations to
    simple requested ratios (4/3 here). The denominator cap recovers that
    intent without creating unbounded integer quotas inside the verifier.
    """
    if not values or not all(_num(value) and value > 0 for value in values):
        return None
    try:
        exact = [Fraction(Decimal(str(value))) for value in values]
    except (InvalidOperation, ValueError, ZeroDivisionError):
        return None
    scale = min(exact)
    if scale <= 0:
        return None
    fractions = [
        (value / scale).limit_denominator(10_000)
        for value in exact
    ]
    if any(value.numerator <= 0 for value in fractions):
        return None
    denominator = math.lcm(*(value.denominator for value in fractions))
    whole = [value.numerator * (denominator // value.denominator)
             for value in fractions]
    divisor = math.gcd(*whole)
    normalized = [value // divisor for value in whole]
    if not normalized or sum(normalized) > 10_000:
        return None
    return normalized


def _allocation_sequence_matches(rows: list[dict], arms: list[str],
                                 quota: list[int], block_size: int) -> bool:
    """Check every complete permuted block and the capacity of its final tail."""
    base_size = sum(quota)
    if block_size < base_size or block_size % base_size:
        return False
    per_block = {
        arm: weight * (block_size // base_size)
        for arm, weight in zip(arms, quota)
    }
    labels = [row.get("arm") for row in sorted(rows, key=lambda row: row["unit"])]
    for start in range(0, len(labels), block_size):
        chunk = labels[start:start + block_size]
        observed = Counter(chunk)
        if len(chunk) == block_size:
            if any(observed.get(arm, 0) != per_block[arm] for arm in arms):
                return False
        elif any(observed.get(arm, 0) > per_block[arm] for arm in arms):
            return False
    return True


def check_randomize(result: dict, args: dict) -> GateVerdict:
    """Bind a randomization plan to n, method, arms, ratio, seed, and units.

    ``simple`` randomization is accepted only for equal allocation weights: its
    result does not echo weights, so a non-equal request cannot be proven from a
    finite stochastic realization. Block and stratified requests are bound by
    their exact integer quota in every complete block (and by the quota capacity
    of the final partial block).
    """
    checks: dict[str, bool] = {}
    failures: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=["randomize returned an error"])
    if not isinstance(args, dict):
        return GateVerdict(passed=False, checks={"request_contract": False},
                           failures=["randomize request is missing or malformed"])

    requested_n = args.get("n")
    requested_method = args.get("method", "simple")
    requested_seed = args.get("seed", 42)
    requested_arms = args.get("arms", 2)
    if (isinstance(requested_arms, int) and not isinstance(requested_arms, bool)
            and 2 <= requested_arms <= 100):
        normalized_arms = [f"arm{index}" for index in range(1, requested_arms + 1)]
    elif isinstance(requested_arms, list) and 2 <= len(requested_arms) <= 100:
        normalized_arms = list(requested_arms)
    else:
        normalized_arms = []
    requested_ratio = args.get("ratio", [1] * len(normalized_arms))
    quota = (
        _integer_allocation_weights(requested_ratio)
        if isinstance(requested_ratio, list) and 2 <= len(requested_ratio) <= 100
        else None
    )
    requested_block = args.get("block_size")
    requested_strata = args.get("strata")

    request_valid = (
        isinstance(requested_n, int) and not isinstance(requested_n, bool)
        and 1 <= requested_n <= 10_000
        and requested_method in {"simple", "block", "stratified"}
        and len(normalized_arms) >= 2
        and all(isinstance(arm, str) for arm in normalized_arms)
        and len(set(normalized_arms)) == len(normalized_arms)
        and quota is not None and len(quota) == len(normalized_arms)
        and isinstance(requested_seed, int) and not isinstance(requested_seed, bool)
        and 0 <= requested_seed <= 2_147_483_647
        and (requested_block is None or (
            isinstance(requested_block, int) and not isinstance(requested_block, bool)
            and 1 <= requested_block <= 10_000
        ))
        and (requested_method != "stratified" or (
            isinstance(requested_strata, list)
            and len(requested_strata) == requested_n
            and all(isinstance(value, str) or _num(value)
                    for value in requested_strata)
        ))
    )
    checks["request_contract"] = request_valid
    if not request_valid:
        return GateVerdict(
            passed=False, checks=checks,
            failures=["randomize request fields are missing, invalid, or inconsistent"],
        )

    normalized_strata = requested_strata
    if requested_method == "stratified" and any(
            isinstance(value, str) for value in requested_strata):
        # jsonlite simplifies a mixed string/number array to an R character
        # vector before randomize_units sees it. Mirror that effective request
        # without ever placing the private labels in checks or failures.
        def _r_character(value: Any) -> str:
            if isinstance(value, str):
                return value
            if isinstance(value, int):
                return str(value)
            if value == 0:
                return "0"
            if float(value).is_integer():
                return str(int(value))
            return format(value, ".15g")
        normalized_strata = [_r_character(value) for value in requested_strata]

    if requested_method == "simple":
        equal_ratio = len(set(quota)) == 1
        checks["simple_ratio_is_provable"] = equal_ratio
        if not equal_ratio:
            failures.append(
                "simple randomization cannot verify a non-equal allocation ratio"
            )

    n = result.get("n")
    assignment = result.get("assignment")
    arms = result.get("arms")
    counts = result.get("counts")
    n_matches = (
        isinstance(n, int) and not isinstance(n, bool) and n == requested_n
    )
    checks["n_matches_request"] = n_matches
    if not n_matches:
        failures.append("randomize result n does not match the request")
    checks["method_matches_request"] = result.get("method") == requested_method
    if not checks["method_matches_request"]:
        failures.append("randomize result method does not match the request")
    checks["arms_match_request"] = arms == normalized_arms
    if not checks["arms_match_request"]:
        failures.append("randomize result arms do not match the request")
    seed_matches = (
        isinstance(result.get("seed"), int)
        and not isinstance(result.get("seed"), bool)
        and result.get("seed") == requested_seed
    )
    checks["seed_matches_request"] = seed_matches
    if not seed_matches:
        failures.append("randomize result seed does not match the effective request seed")

    rows_valid = (
        isinstance(assignment, list)
        and len(assignment) == requested_n
        and all(isinstance(row, dict) for row in assignment)
    )
    checks["assignment_rows_valid"] = rows_valid
    if not rows_valid:
        failures.append("randomize assignment rows are missing or malformed")
    rows = assignment if rows_valid else []
    unit_ids = [row.get("unit") for row in rows]
    units_exact = (
        rows_valid
        and all(isinstance(unit, int) and not isinstance(unit, bool)
                for unit in unit_ids)
        and sorted(unit_ids) == list(range(1, requested_n + 1))
    )
    checks["requested_units_exactly_once"] = units_exact
    if not units_exact:
        failures.append("randomize result does not assign every requested unit exactly once")

    assignment_arms_valid = (
        rows_valid and all(row.get("arm") in normalized_arms for row in rows)
    )
    checks["assignment_arms_valid"] = assignment_arms_valid
    if not assignment_arms_valid:
        failures.append("randomize assignment contains an undeclared arm")

    counts_valid = (
        isinstance(counts, dict)
        and set(counts) == set(normalized_arms)
        and all(isinstance(value, int) and not isinstance(value, bool) and value >= 0
                for value in counts.values())
        and sum(counts.values()) == requested_n
    )
    checks["declared_counts_valid"] = counts_valid
    if not counts_valid:
        failures.append("randomize arm counts are missing, malformed, or do not sum to n")
    counts_match = (
        counts_valid and assignment_arms_valid
        and Counter(row["arm"] for row in rows) == Counter(counts)
    )
    checks["counts_match_assignment"] = counts_match
    if not counts_match:
        failures.append("randomize arm counts do not match the assignment rows")

    strata_match = True
    if requested_method == "stratified":
        strata_match = units_exact and all(
            row.get("stratum") == normalized_strata[row["unit"] - 1]
            for row in rows
        )
        if not strata_match:
            failures.append("stratified assignment does not match requested unit strata")
    else:
        strata_match = rows_valid and all(row.get("stratum") is None for row in rows)
        if not strata_match:
            failures.append("non-stratified assignment unexpectedly contains strata")
    checks["unit_strata_contract"] = strata_match

    ratio_contract = requested_method == "simple" and len(set(quota)) == 1
    if requested_method == "simple":
        empty_block = (
            "block_size_used" in result
            and result.get("block_size_used") in (None, {}, [])
        )
        checks["simple_has_no_block_size"] = empty_block
        if not empty_block:
            failures.append("simple randomization reported a non-empty block size")
    else:
        base_size = sum(quota)
        expected_block = (
            requested_block
            if requested_block is not None and requested_block % base_size == 0
            else base_size
        )
        used = result.get("block_size_used")
        block_contract = (
            isinstance(used, int) and not isinstance(used, bool)
            and used == expected_block
        )
        checks["effective_block_size_matches_request"] = block_contract
        if not block_contract:
            failures.append("randomize effective block size does not follow the requested rule")

        if block_contract and units_exact and assignment_arms_valid and strata_match:
            if requested_method == "block":
                ratio_contract = _allocation_sequence_matches(
                    rows, normalized_arms, quota, expected_block
                )
            else:
                rows_by_stratum: dict[Any, list[dict]] = {}
                for row in rows:
                    stratum = normalized_strata[row["unit"] - 1]
                    rows_by_stratum.setdefault(stratum, []).append(row)
                ratio_contract = True
                for stratum_rows in rows_by_stratum.values():
                    if not _allocation_sequence_matches(
                            stratum_rows, normalized_arms, quota, expected_block):
                        ratio_contract = False
                        break
            if not ratio_contract:
                failures.append("randomize assignment blocks do not match the requested ratio")
        else:
            ratio_contract = False
    checks["allocation_ratio_matches_request"] = ratio_contract

    # Future engine versions may echo the normalized ratio. Bind it when
    # present, but do not require a field that the current public result omits.
    if "ratio" in result:
        echoed = result.get("ratio")
        echoed_quota = (_integer_allocation_weights(echoed)
                        if isinstance(echoed, list) else None)
        echoed_ok = echoed_quota == quota
        checks["echoed_ratio_matches_request"] = echoed_ok
        if not echoed_ok:
            failures.append("randomize echoed ratio does not match the request")

    return GateVerdict(passed=not failures, checks=checks, failures=failures)


# ── Factorial / RSM design invariants ──────────────────────────────

def _design_rows(design) -> list[dict]:
    return [r for r in design if isinstance(r, dict)] if isinstance(design, list) else []


def _factor_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return [k for k in rows[0]
            if isinstance(k, str)
            and k not in ("point_type", "run", "std_order")
            and _num(rows[0].get(k))]


def _requested_factorial_generators(
    args: dict, n_factors: int, fraction: int,
) -> tuple[bool, list[list[int]] | None]:
    """Validate custom generators in their documented one-based request order."""
    raw = args.get("generators")
    if raw is None:
        return False, []
    basic_count = n_factors - fraction
    if not isinstance(raw, list) or len(raw) != fraction:
        return True, None
    normalized: list[list[int]] = []
    seen: set[tuple[int, ...]] = set()
    for generator in raw:
        if (not isinstance(generator, list) or len(generator) < 2
                or any(not isinstance(index, int) or isinstance(index, bool)
                       or not 1 <= index <= basic_count for index in generator)):
            return True, None
        source_key = tuple(sorted(generator))
        if len(set(source_key)) != len(source_key) or source_key in seen:
            return True, None
        seen.add(source_key)
        normalized.append(list(source_key))
    return True, normalized


def check_factorial(result: dict, args: dict) -> GateVerdict:
    """Invariants of 2-level factorial designs: run count matches the notation,
    ±1 factor columns are pairwise ORTHOGONAL (center rows contribute 0), and
    the resolution equals the shortest defining-relation word."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=[f"tool returned error: {result['error']}"])
    raw_design = result.get("design")
    design_rows_valid = (
        isinstance(raw_design, list) and bool(raw_design)
        and all(isinstance(row, dict) for row in raw_design)
    )
    checks["design_rows_are_objects"] = design_rows_valid
    if not design_rows_valid:
        failures.append("factorial design must be a nonempty list of row objects")
    rows = _design_rows(raw_design)
    n_runs = result.get("n_runs")
    n_runs_valid = (
        _num(n_runs) and int(n_runs) == n_runs and n_runs >= 1
    )
    checks["design_present"] = design_rows_valid and n_runs_valid
    if not checks["design_present"]:
        failures.append("factorial design matrix or n_runs is missing")
    if n_runs_valid and isinstance(raw_design, list):
        checks["n_runs_matches_design"] = len(raw_design) == n_runs
        if len(raw_design) != n_runs:
            failures.append(f"n_runs={n_runs} but design has {len(raw_design)} rows")
    design_type = result.get("type")
    supported_type = design_type in {"full_factorial", "fractional_factorial"}
    checks["factorial_type_supported"] = supported_type
    if not supported_type:
        failures.append(f"factorial output has unsupported or missing type={design_type!r}")

    cols = _factor_columns(rows)
    raw_n_factors = result.get("n_factors")
    n_factors_valid = (
        _num(raw_n_factors) and int(raw_n_factors) == raw_n_factors
        and raw_n_factors >= 1 and int(raw_n_factors) == len(cols)
    )
    checks["factor_columns_match_n_factors"] = n_factors_valid
    if not n_factors_valid:
        failures.append("factorial output has invalid or inconsistent factor columns")

    raw_requested_n_factors = (args or {}).get("n_factors")
    requested_n_factors = (
        int(raw_requested_n_factors)
        if _num(raw_requested_n_factors)
        and int(raw_requested_n_factors) == raw_requested_n_factors
        and raw_requested_n_factors >= 1 else None
    )
    n_factors_match_request = (
        n_factors_valid and requested_n_factors is not None
        and int(raw_n_factors) == requested_n_factors
    )
    checks["factorial_n_factors_matches_request"] = n_factors_match_request
    if not n_factors_match_request:
        failures.append(
            "factorial output n_factors does not match the requested n_factors"
        )

    raw_requested_levels = (args or {}).get("levels", 2)
    requested_levels: list[int] | None = None
    if requested_n_factors is not None:
        if (_num(raw_requested_levels)
                and int(raw_requested_levels) == raw_requested_levels
                and raw_requested_levels >= 2):
            requested_levels = [int(raw_requested_levels)] * requested_n_factors
        elif (isinstance(raw_requested_levels, list)
              and len(raw_requested_levels) in {1, requested_n_factors}
              and all(_num(value) and int(value) == value and value >= 2
                      for value in raw_requested_levels)):
            normalized = (
                raw_requested_levels * requested_n_factors
                if len(raw_requested_levels) == 1 else raw_requested_levels
            )
            requested_levels = [int(value) for value in normalized]

    raw_requested_replicates = (args or {}).get("replicates", 1)
    requested_replicates = (
        int(raw_requested_replicates)
        if _num(raw_requested_replicates)
        and int(raw_requested_replicates) == raw_requested_replicates
        and raw_requested_replicates >= 1 else None
    )

    metadata_columns = {"point_type", "run", "std_order"}
    factor_shape_valid = design_rows_valid and bool(cols) and all(
        all(isinstance(key, str) for key in row)
        and
        {
            key for key in row
            if isinstance(key, str) and key not in metadata_columns
        } == set(cols)
        and all(_num(row.get(column)) for column in cols)
        for row in rows
    )
    checks["factor_matrix_numeric_and_rectangular"] = factor_shape_valid
    if not factor_shape_valid:
        failures.append(
            "factorial design matrix has missing, extra, or nonnumeric factor values"
        )

    if design_type == "full_factorial" and rows and n_factors_valid and factor_shape_valid:
        raw_levels = result.get("levels")
        # jsonlite auto-unboxes the one-factor R vector. Scalar output is valid
        # only for that exact shape; every multi-factor result must retain one
        # level count per discovered factor column.
        levels = [raw_levels] if len(cols) == 1 and _num(raw_levels) else raw_levels
        levels_valid = (
            isinstance(levels, list) and len(levels) == len(cols)
            and all(_num(value) and int(value) == value and value >= 2
                    for value in levels)
        )
        checks["full_factorial_levels_valid"] = levels_valid
        if not levels_valid:
            failures.append("full factorial output has invalid level counts")
        else:
            level_counts = [int(value) for value in levels]
            levels_match_request = (
                requested_levels is not None
                and level_counts == requested_levels
            )
            checks["full_factorial_levels_match_request"] = levels_match_request
            if not levels_match_request:
                failures.append(
                    "full factorial output levels do not match the effective "
                    "requested levels"
                )
            raw_centers = (args or {}).get("center_points", 0)
            raw_replicates = result.get("replicates")
            centers_valid = (
                _num(raw_centers) and int(raw_centers) == raw_centers
                and raw_centers >= 0
            )
            replicates_valid = (
                _num(raw_replicates) and int(raw_replicates) == raw_replicates
                and raw_replicates >= 1
            )
            replicates_match_request = (
                replicates_valid and requested_replicates is not None
                and int(raw_replicates) == requested_replicates
            )
            checks["center_count_valid"] = centers_valid
            checks["replicates_valid"] = replicates_valid
            checks["full_factorial_replicates_match_request"] = (
                replicates_match_request
            )
            if not centers_valid:
                failures.append("center_points must be a nonnegative integer")
            if not replicates_valid:
                failures.append("full factorial output has invalid replicates")
            elif not replicates_match_request:
                failures.append(
                    "full factorial output replicates do not match the effective "
                    "requested replicates"
                )

            if centers_valid and replicates_valid:
                centers = int(raw_centers)
                replicates = int(raw_replicates)
                # The engine appends centers and may then shuffle every run. Find
                # them by coordinates, never position. For an odd-level grid the
                # same midpoint legitimately occurs once per replicate, so remove
                # exactly the requested number of indistinguishable added rows
                # while retaining those base-grid occurrences.
                center_vector = (
                    [0] * len(cols) if all(value == 2 for value in level_counts)
                    else [(value + 1) / 2 for value in level_counts]
                )

                def is_center(row: dict) -> bool:
                    return all(
                        _num(row.get(column))
                        and _numbers_close(row[column], center_vector[index], 0)
                        for index, column in enumerate(cols)
                    )

                center_matches = sum(is_center(row) for row in rows)
                natural_midpoints = (
                    replicates if all(value % 2 == 1 for value in level_counts)
                    else 0
                )
                centers_match = center_matches == centers + natural_midpoints
                checks["center_rows_match_request"] = centers_match
                if not centers_match:
                    failures.append(
                        "factorial design does not contain exactly the requested "
                        "number of center rows"
                    )

                remaining_centers = centers
                base_rows: list[dict] = []
                for row in rows:
                    if remaining_centers and is_center(row):
                        remaining_centers -= 1
                    else:
                        base_rows.append(row)

                expected_base = math.prod(level_counts) * replicates
                run_count_ok = (
                    remaining_centers == 0 and len(base_rows) == expected_base
                )
                checks["full_factorial_run_count"] = run_count_ok
                if not run_count_ok:
                    failures.append(
                        f"full factorial has {len(base_rows)} base rows; "
                        f"expected {expected_base}"
                    )

                numeric_rows = all(
                    all(_num(row.get(column)) for column in cols)
                    for row in base_rows
                )
                distinct_ok = numeric_rows and all(
                    len({row[column] for row in base_rows}) == level_counts[index]
                    for index, column in enumerate(cols)
                )
                checks["factor_level_counts_match"] = distinct_ok
                if not distinct_ok:
                    failures.append(
                        "factor columns do not contain the declared number of levels"
                    )

                # Validate the complete replicated grid, not only its marginal
                # level counts. This also catches a forged extra midpoint paired
                # with a missing non-center run.
                if all(value == 2 for value in level_counts):
                    expected_values = [(-1, 1) for _ in level_counts]
                else:
                    expected_values = [range(1, value + 1) for value in level_counts]
                if run_count_ok and numeric_rows:
                    expected_grid = Counter({
                        tuple(combination): replicates
                        for combination in product(*expected_values)
                    })
                    actual_grid = Counter(
                        tuple(row[column] for column in cols) for row in base_rows
                    )
                    grid_complete = actual_grid == expected_grid
                else:
                    grid_complete = False
                checks["full_factorial_grid_complete"] = grid_complete
                if not grid_complete:
                    failures.append(
                        "factorial rows do not form the declared replicated full grid"
                    )
    two_level = factor_shape_valid and all(
        r.get(c) in (-1, 0, 1) for r in rows for c in cols)
    if two_level and len(cols) >= 2:
        ortho = True
        for i in range(len(cols)):
            for j in range(i + 1, len(cols)):
                dot = sum(r[cols[i]] * r[cols[j]] for r in rows)
                if dot != 0:
                    ortho = False
                    failures.append(f"columns {cols[i]}·{cols[j]} not orthogonal "
                                    f"(dot={dot})")
        checks["orthogonal"] = ortho
    elif rows:
        blocked.append("orthogonality not checked (not a ±1-coded 2-level design)")
    res = result.get("resolution")
    raw_fraction = (args or {}).get("fraction")
    requested_fraction = (
        int(raw_fraction)
        if _num(raw_fraction) and int(raw_fraction) == raw_fraction
        and raw_fraction > 0 else None
    )
    is_fractional = (
        design_type == "fractional_factorial" or requested_fraction is not None
    )
    raw_requested_generators = (args or {}).get("generators")
    if raw_requested_generators is not None and requested_fraction is None:
        checks["custom_generators_require_fraction"] = False
        failures.append("custom factorial generators require a positive fraction")

    # A half fraction (p=1) has exactly one defining word, which the dispatcher
    # unboxes to a bare string. Every word must still be a real relation over
    # this design's own factor letters: comparing len(str(value)) alone would
    # accept null, true, or 1000 as a four-character "word".
    words = _r_vector(result, "defining_relation")
    factor_letters = set(cols)
    words_well_formed = isinstance(words, list) and bool(words) and all(
        isinstance(word, str) and word == word.strip() and len(word) >= 2
        and len(set(word)) == len(word)
        and set(word) <= factor_letters
        for word in words
    )
    word_sets = (
        [frozenset(word) for word in words] if words_well_formed else []
    )
    words_well_formed = (
        words_well_formed and len(set(word_sets)) == len(word_sets)
    )

    if is_fractional:
        fraction_valid = (
            design_type == "fractional_factorial"
            and n_factors_match_request
            and requested_fraction is not None
            and requested_fraction < len(cols)
        )
        checks["fractional_fraction_valid"] = fraction_valid
        if not fraction_valid:
            failures.append(
                "fractional factorial output requires an exact requested fraction "
                "p with 1 <= p < n_factors"
            )

        generators_supplied = False
        requested_generators: list[list[int]] | None = None
        if fraction_valid:
            generators_supplied, requested_generators = _requested_factorial_generators(
                args or {}, len(cols), requested_fraction,
            )
        custom_generators_valid = (
            fraction_valid and (not generators_supplied
                                or requested_generators is not None)
        )
        checks["custom_generators_valid"] = custom_generators_valid
        if not custom_generators_valid:
            failures.append(
                "custom generators must define exactly p unique generated columns "
                "using unique one-based indices of the k-p basic factors"
            )

        fractional_levels_match = (
            requested_levels is not None
            and len(requested_levels) == len(cols)
            and all(value == 2 for value in requested_levels)
        )
        checks["fractional_levels_match_request"] = fractional_levels_match
        if not fractional_levels_match:
            failures.append(
                "fractional factorial requires exactly two requested levels per factor"
            )

        raw_result_replicates = result.get("replicates")
        result_replicates = (
            int(raw_result_replicates)
            if _num(raw_result_replicates)
            and int(raw_result_replicates) == raw_result_replicates
            and raw_result_replicates >= 1 else None
        )
        fractional_replicates_match = (
            result_replicates is not None and requested_replicates is not None
            and result_replicates == requested_replicates
        )
        checks["fractional_replicates_match_request"] = (
            fractional_replicates_match
        )
        if not fractional_replicates_match:
            failures.append(
                "fractional factorial output replicates do not match the effective "
                "requested replicates"
            )

        raw_centers = (args or {}).get("center_points", 0)
        centers_valid = (
            _num(raw_centers) and int(raw_centers) == raw_centers
            and raw_centers >= 0
        )
        checks["fractional_center_count_valid"] = centers_valid
        if not centers_valid:
            failures.append("fractional center_points must be a nonnegative integer")

        # A regular fractional design contains only ±1 base runs plus the exact
        # number of all-zero center rows requested by the caller. Mixed zero/±1
        # rows are neither and must not disappear from the count.
        fractional_rows_coded = factor_shape_valid and all(
            all(row[column] in (-1, 1) for column in cols)
            or all(row[column] == 0 for column in cols)
            for row in rows
        )
        checks["fractional_rows_are_base_or_center"] = fractional_rows_coded
        if not fractional_rows_coded:
            failures.append(
                "fractional factorial rows must be all ±1 base runs or all-zero centers"
            )
        base_rows = [
            row for row in rows
            if all(row.get(column) in (-1, 1) for column in cols)
        ] if fractional_rows_coded else []
        center_rows = [
            row for row in rows
            if all(row.get(column) == 0 for column in cols)
        ] if fractional_rows_coded else []

        runs_match = False
        centers_match = False
        if (fraction_valid and centers_valid and fractional_rows_coded
                and fractional_levels_match and fractional_replicates_match):
            expected_unique_runs = 2 ** (len(cols) - requested_fraction)
            base_run_counts = Counter(
                tuple(row[column] for column in cols) for row in base_rows
            )
            runs_match = (
                len(base_run_counts) == expected_unique_runs
                and len(base_rows) == expected_unique_runs * requested_replicates
                and all(count == requested_replicates
                        for count in base_run_counts.values())
            )
            centers_match = len(center_rows) == int(raw_centers)
        checks["fractional_run_count_matches_fraction"] = runs_match
        checks["fractional_center_rows_match_request"] = centers_match
        if not runs_match:
            failures.append(
                "fractional factorial does not contain the exact 2^(k-p) base-run "
                "set with the requested replicate multiplicity"
            )
        if not centers_match:
            failures.append(
                "fractional factorial does not contain exactly the requested "
                "number of all-zero center rows"
            )

        generators_honored = not generators_supplied
        if generators_supplied:
            generators_honored = bool(
                requested_generators is not None and runs_match and base_rows
            )
            if generators_honored:
                basic_count = len(cols) - requested_fraction
                for offset, sources in enumerate(requested_generators or []):
                    generated_column = cols[basic_count + offset]
                    if any(
                        row[generated_column]
                        != math.prod(row[cols[source - 1]] for source in sources)
                        for row in base_rows
                    ):
                        generators_honored = False
                        break
        checks["requested_generators_honored"] = generators_honored
        if not generators_honored:
            failures.append(
                "fractional factorial matrix does not honor the requested generators"
            )

        checks["defining_relation_well_formed"] = words_well_formed
        if not words_well_formed:
            failures.append(
                "fractional factorial defining relation must list unique, "
                "non-repeating words of two or more of this design's factor letters"
            )

        expected_words = (
            2 ** requested_fraction - 1 if fraction_valid else None
        )
        cardinality_ok = (
            words_well_formed and expected_words is not None
            and len(word_sets) == expected_words
        )
        checks["defining_relation_cardinality"] = cardinality_ok
        if not cardinality_ok:
            failures.append(
                "fractional factorial defining relation must list exactly "
                "2^p - 1 non-identity words"
            )

        relation_with_identity = set(word_sets) | {frozenset()}
        group_closed = cardinality_ok and all(
            left.symmetric_difference(right) in relation_with_identity
            for left in relation_with_identity for right in relation_with_identity
        )
        checks["defining_relation_group_closed"] = group_closed
        if not group_closed:
            failures.append(
                "fractional factorial defining relation is not closed under "
                "symmetric difference"
            )

        # Well-formed names and closure prove nothing about this matrix. Every
        # reported relation word must multiply to +1 on every non-center run.
        relation_holds = (
            words_well_formed and runs_match and bool(base_rows)
            and all(
                math.prod(row[letter] for letter in word) == 1
                for word in words for row in base_rows
            )
        )
        checks["defining_relation_holds_in_design"] = relation_holds
        if not relation_holds:
            failures.append(
                "defining relation does not hold in the design matrix: a "
                "reported word's column product is not +1 on every base run"
            )

        resolution_valid = _num(res) and words_well_formed
        checks["resolution_matches_relation"] = bool(
            resolution_valid and min(len(word) for word in words) == res
        )
        if not checks["resolution_matches_relation"]:
            failures.append(
                "fractional factorial resolution is missing or does not equal "
                "the shortest defining word"
            )
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked)


def check_rsm(result: dict, args: dict) -> GateVerdict:
    """Point-type geometry of response-surface designs: CCD run counts decompose
    as factorial + 2k axial + centers with axial points on ±alpha, one factor at
    a time; Box-Behnken as edge + center runs."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=[f"tool returned error: {result['error']}"])
    dtype = result.get("type")
    n_runs = result.get("n_runs")
    rows = _design_rows(result.get("design"))
    checks["design_present"] = bool(rows) and _num(n_runs)
    if not checks["design_present"]:
        failures.append("RSM design matrix or n_runs is missing")
    elif len(rows) != n_runs:
        checks["n_runs_matches_design"] = False
        failures.append(f"n_runs={n_runs} but design has {len(rows)} rows")
    else:
        checks["n_runs_matches_design"] = True
    if dtype == "central_composite":
        parts = [result.get("n_factorial"), result.get("n_axial"),
                 result.get("n_center")]
        k = result.get("n_factors")
        if not (all(_num(p) for p in parts) and _num(k) and _num(result.get("alpha"))):
            failures.append("CCD output missing n_factorial/n_axial/n_center/n_factors/alpha")
        if all(_num(p) for p in parts) and _num(n_runs):
            ok = sum(parts) == n_runs
            checks["ccd_counts_decompose"] = ok
            if not ok:
                failures.append(f"n_runs={n_runs} != factorial+axial+center="
                                f"{sum(parts)}")
        if _num(k) and _num(result.get("n_axial")):
            ok = result["n_axial"] == 2 * k
            checks["axial_is_2k"] = ok
            if not ok:
                failures.append(f"n_axial={result['n_axial']} != 2k={2 * k}")
        a = result.get("alpha")
        cols = _factor_columns(rows)
        if _num(a):
            checks["alpha_positive"] = a > 0
            if a <= 0:
                failures.append("CCD alpha must be positive")
        if rows and cols and _num(a) and a > 0:
            bad_axial = 0
            for r in rows:
                if r.get("point_type") != "axial":
                    continue
                vals = [r.get(c) for c in cols if _num(r.get(c))]
                nz = [v for v in vals if abs(v) > 1e-9]
                if len(nz) != 1 or not _close(abs(nz[0]), a, 1e-3):
                    bad_axial += 1
            checks["axial_on_alpha"] = bad_axial == 0
            if bad_axial:
                failures.append(f"{bad_axial} axial row(s) not at ±alpha={a}")
    elif dtype == "box_behnken":
        parts = [result.get("n_edge"), result.get("n_center")]
        if not all(_num(p) for p in parts):
            failures.append("Box-Behnken output missing n_edge/n_center")
        if all(_num(p) for p in parts) and _num(n_runs):
            ok = sum(parts) == n_runs
            checks["bbd_counts_decompose"] = ok
            if not ok:
                failures.append(f"n_runs={n_runs} != edge+center={sum(parts)}")
    else:
        failures.append(f"RSM output has unsupported or missing type={dtype!r}")
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked)


# ── Shared tool → cheap design-check mapping ───────────────────────

def design_checks_for(tool_name: str, args: dict, result: dict) -> list[GateVerdict]:
    """Cheap, deterministic per-tool design checks — no run_tests, no re-run.

    This is the single raw-result tool→check mapping used by the private server
    verifier. Other surfaces intentionally cannot see the raw payload: they
    validate the server-bound public projection and check attestation, then add
    fresh regression evidence before presenting a result.
    """
    args = args or {}
    verdicts = [check_output_contract(result if isinstance(result, dict) else {})]
    if tool_name == "simulate_design":
        config = args.get("config", {}) or {}
        verdicts.append(check_config_completeness(config))
        verdicts.append(check_single_endpoint(result, config))
    elif tool_name == "master_simulate":
        config = args.get("config", {}) or {}
        verdicts.append(check_master_config_reserved(config))
        verdicts.append(check_master_result(result, config))
    elif tool_name == "sample_size":
        # Planning tool: no OC curve, but a mis-directed or incomplete config
        # and a table that misses its own power target must still be caught.
        verdicts.append(check_config_completeness(args))
        verdicts.append(check_sample_size(result, args))
    elif tool_name == "ab_test":
        verdicts.append(check_ab_test(result, args))
    elif tool_name == "indirect_compare":
        verdicts.append(check_bucher(result, args))
    elif tool_name == "meta_analyze":
        verdicts.append(check_meta(result, args))
    elif tool_name == "randomize":
        verdicts.append(check_randomize(result, args))
    elif tool_name == "factorial_design":
        verdicts.append(check_factorial(result, args))
    elif tool_name == "rsm_design":
        verdicts.append(check_rsm(result, args))
    else:
        verdicts.append(GateVerdict(
            passed=False,
            checks={"tool_specific_checks_registered": False},
            failures=[f"no tool-specific verification contract for {tool_name}"],
        ))
    return verdicts


# ── Combine ────────────────────────────────────────────────────────

def combined_gate(*verdicts: GateVerdict) -> GateVerdict:
    all_checks: dict[str, bool] = {}
    all_failures: list[str] = []
    all_blocked: list[str] = []
    all_notes: list[str] = []
    for v in verdicts:
        all_checks.update(v.checks)
        all_failures.extend(v.failures)
        all_blocked.extend(v.blocked)
        all_notes.extend(v.notes)
    return GateVerdict(
        passed=all(v.passed for v in verdicts),
        checks=all_checks,
        failures=all_failures,
        blocked=all_blocked,
        notes=all_notes,
    )
