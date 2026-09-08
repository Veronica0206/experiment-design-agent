#!/usr/bin/env python3
"""Deterministic tests for the compositional public response budget."""

from __future__ import annotations

import os
import sys


sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from server_verify import (  # noqa: E402
    MAX_CANONICAL_REPORT_BYTES,
    MAX_PUBLIC_RESULT_BYTES,
    MAX_VERIFIER_PRESENTABLE_BYTES,
    _presentable_payload_within_budget,
    _withhold_oversized_response,
)


passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {detail}")
        failed += 1


check(
    "response_budget_stages_are_strictly_nested",
    0 < MAX_PUBLIC_RESULT_BYTES
    < MAX_CANONICAL_REPORT_BYTES
    < MAX_VERIFIER_PRESENTABLE_BYTES,
)

small_result = {"value": 1}
small_report = "verified"
small_envelope = {"public_result": small_result, "report": small_report}
check(
    "small_presentable_payload_fits",
    _presentable_payload_within_budget(
        small_result, small_report, small_envelope,
    ),
)

check(
    "oversized_public_result_is_rejected_before_stdout",
    not _presentable_payload_within_budget(
        {"blob": "x" * (MAX_PUBLIC_RESULT_BYTES + 1)},
        small_report,
        small_envelope,
    ),
)
check(
    "oversized_canonical_report_is_rejected_before_stdout",
    not _presentable_payload_within_budget(
        small_result,
        "x" * (MAX_CANONICAL_REPORT_BYTES + 1),
        small_envelope,
    ),
)
check(
    "oversized_verifier_envelope_is_rejected_before_stdout",
    not _presentable_payload_within_budget(
        small_result,
        small_report,
        {"blob": "x" * (MAX_VERIFIER_PRESENTABLE_BYTES + 1)},
    ),
)

withheld = {
    "status": "PASS_PARTIAL",
    "presentable": True,
    "checks": {"scientific_validation": True},
    "failures": [],
    "blocked": ["manual"],
    "notes": ["note"],
    "report": "PRIVATE-REPORT-SENTINEL",
    "public_result": {"blob": "PRIVATE-RESULT-SENTINEL"},
    "public_result_hash": "a" * 64,
    "report_hash": "b" * 64,
}
_withhold_oversized_response(withheld)
check(
    "oversized_payload_becomes_fixed_value_free_failure",
    withheld == {
        "status": "RETRY_REQUIRED",
        "presentable": False,
        "checks": {
            "scientific_validation": True,
            "response_budget": False,
        },
        "failures": ["check_failed:response_budget"],
        "blocked": [],
        "notes": [],
    },
    str(withheld),
)

print(f"\n--- Results: {passed} passed, {failed} failed ---")
raise SystemExit(1 if failed else 0)
