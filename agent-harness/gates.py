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
from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from artifact_download import MAX_ARTIFACT_BYTES
from verification import VerificationStatus

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


# ── A/B test result sanity ─────────────────────────────────────────

def check_ab_test(result: dict) -> GateVerdict:
    """Sanity + completeness for the two-arm A/B sizing result (doe.R
    ab_test_size): positive arms, a non-zero MDE, a valid sidedness, and an
    achieved power that actually meets the target it solved for."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["ab_test returned no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=[f"tool returned error: {result['error']}"])

    def _pos(key: str) -> bool:
        v = result.get(key)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
        checks[f"{key}_positive"] = ok
        if not ok:
            failures.append(f"{key}={v}, expected positive")
        return ok

    _pos("n_total"); _pos("n_control"); _pos("n_treatment")

    mde = result.get("mde")
    checks["mde_nonzero"] = isinstance(mde, (int, float)) and mde != 0
    if not checks["mde_nonzero"]:
        failures.append(f"mde={mde}, expected a non-zero minimum detectable effect")

    sided = result.get("sided")
    checks["sided_valid"] = sided in (1, 2)
    if not checks["sided_valid"]:
        failures.append(f"sided={sided}, expected 1 or 2")

    target = result.get("target_power")
    achieved = result.get("achieved_power")
    blocked: list[str] = []
    if isinstance(target, (int, float)) and isinstance(achieved, (int, float)):
        # The design solves n to hit target; ceiling makes achieved >= target
        # (small tolerance). A large shortfall means the sizing is wrong.
        ok = achieved >= target - 0.02
        checks["power_meets_target"] = ok
        if not ok:
            failures.append(f"achieved_power={achieved} well below target={target}")
    else:
        # Never let a renamed/missing field make this check silently vacuous —
        # a clean PASS must mean the check actually ran.
        failures.append("target_power/achieved_power missing or non-numeric")
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked)


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
    oc: Any = None
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
        family = (config or {}).get("master_design_type")
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
            "basket_oc_table.csv": {"subgroup", "null_param", "alt_param", "reject_rate"},
            "basket_fwer.csv": {"scenario", "fwer"},
            "basket_subgroup_decisions.csv": {"subgroup", "decision"},
            "umbrella_power_table.csv": {"arm", "per_arm_power"},
            "platform_arm_results.csv": {"arm", "reject_rate", "mean_n"},
            "platform_oc_table.csv": {"metric", "value"},
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
    return GateVerdict(passed=not failures, checks=checks, failures=failures)


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


# ── Randomization plan invariants ──────────────────────────────────

def check_randomize(result: dict, args: dict) -> GateVerdict:
    """Structural invariants of the assignment plan: every unit assigned, arm
    labels consistent, counts sum to n, and the seed echoed for reproduction."""
    checks: dict[str, bool] = {}
    failures: list[str] = []
    blocked: list[str] = []
    if not isinstance(result, dict):
        return GateVerdict(passed=False, failures=["no parseable result"])
    if result.get("error"):
        return GateVerdict(passed=False, checks={"no_tool_error": False},
                           failures=[f"tool returned error: {result['error']}"])
    n = result.get("n")
    assignment = result.get("assignment")
    arms = result.get("arms")
    counts = result.get("counts")
    if not (_num(n) and isinstance(assignment, list)):
        return GateVerdict(passed=False, checks={"assignment_contract": False},
                           failures=["randomize output missing numeric n or assignment rows"])
    if not (isinstance(arms, list) and arms):
        failures.append("randomize output missing declared arms")
    if not isinstance(counts, dict):
        failures.append("randomize output missing arm counts")
    checks["all_units_assigned"] = len(assignment) == n
    if len(assignment) != n:
        failures.append(f"{len(assignment)} assignment rows for n={n}")
    if isinstance(counts, dict):
        total = sum(v for v in counts.values() if _num(v))
        checks["counts_sum_to_n"] = total == n
        if total != n:
            failures.append(f"arm counts sum to {total}, expected n={n}")
    if isinstance(arms, list) and arms:
        assigned_arms = {r.get("arm") for r in assignment if isinstance(r, dict)}
        ok = assigned_arms.issubset(set(arms))
        checks["arm_labels_consistent"] = ok
        if not ok:
            failures.append(f"assignment uses arm labels {assigned_arms - set(arms)} "
                            "not in the declared arms")
    unit_ids = [r.get("unit", r.get("unit_id"))
                for r in assignment if isinstance(r, dict)]
    unique_units = (len(unit_ids) == len(assignment)
                    and all(v is not None for v in unit_ids)
                    and len(set(unit_ids)) == len(unit_ids))
    checks["unit_ids_unique"] = unique_units
    if not unique_units:
        failures.append("assignment unit identifiers are missing or duplicated")
    checks["seed_echoed"] = _num(result.get("seed"))
    if not checks["seed_echoed"]:
        failures.append("seed not echoed — plan not independently reproducible")
    return GateVerdict(passed=not failures, checks=checks, failures=failures,
                       blocked=blocked)


# ── Factorial / RSM design invariants ──────────────────────────────

def _design_rows(design) -> list[dict]:
    return [r for r in design if isinstance(r, dict)] if isinstance(design, list) else []


def _factor_columns(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    return [k for k in rows[0]
            if k not in ("point_type", "run", "std_order") and _num(rows[0].get(k))]


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
    rows = _design_rows(result.get("design"))
    n_runs = result.get("n_runs")
    checks["design_present"] = bool(rows) and _num(n_runs)
    if not checks["design_present"]:
        failures.append("factorial design matrix or n_runs is missing")
    if _num(n_runs) and rows:
        checks["n_runs_matches_design"] = len(rows) == n_runs
        if len(rows) != n_runs:
            failures.append(f"n_runs={n_runs} but design has {len(rows)} rows")
    cols = _factor_columns(rows)
    if result.get("type") == "full_factorial" and rows and cols:
        levels = result.get("levels")
        if not (isinstance(levels, list) and len(levels) == len(cols)
                and all(_num(value) and int(value) == value and value >= 2 for value in levels)):
            failures.append("full factorial output has invalid level counts")
        else:
            centers = int((args or {}).get("center_points") or 0)
            replicates = int(result.get("replicates") or 1)
            base_rows = rows[:-centers] if centers else rows
            expected_base = math.prod(int(value) for value in levels) * replicates
            checks["full_factorial_run_count"] = len(base_rows) == expected_base
            if len(base_rows) != expected_base:
                failures.append(f"full factorial has {len(base_rows)} base rows; expected {expected_base}")
            distinct_ok = all(
                len({row.get(column) for row in base_rows}) == int(levels[index])
                for index, column in enumerate(cols)
            )
            checks["factor_level_counts_match"] = distinct_ok
            if not distinct_ok:
                failures.append("factor columns do not contain the declared number of levels")
    two_level = rows and cols and all(
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
    words = result.get("defining_relation")
    if _num(res) and isinstance(words, list) and words:
        min_word = min(len(str(w)) for w in words)
        checks["resolution_matches_relation"] = min_word == res
        if min_word != res:
            failures.append(f"resolution {res} != shortest defining word length "
                            f"{min_word}")
    if (int((args or {}).get("fraction") or 0) > 0 or
            result.get("type") == "fractional_factorial") and not (
            _num(res) and isinstance(words, list) and words):
        failures.append("fractional factorial output missing resolution/defining_relation")
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

    SINGLE SOURCE of the tool→check mapping, shared by the Python harness
    (harness._design_checks), the SubagentStop hook, and the Streamlit direct
    form — so no runtime silently applies weaker checks than another.
    Expensive checks (regression suite, same-seed reproducibility) are layered
    on top by the harness only."""
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
        verdicts.append(check_ab_test(result))
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
