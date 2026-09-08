"""Closed, repository-owned capability profiles for installed distributions.

The active selection is read from a regular local JSON file, never an environment
variable. Each catalog entry must match the reviewed capability boundary exactly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

PROFILE_PATH = Path(__file__).with_name("runtime-profiles.json")
_SKILLS = [
    "vera-experiment-designing", "vera-master-experiment-designing",
    "vera-doe-designing", "vera-indirect-comparing", "vera-meta-analyzing",
]
_TOOLS = [
    "validate_config", "sample_size", "simulate_design", "run_tests",
    "master_simulate", "ab_test", "factorial_design", "rsm_design",
    "randomize", "indirect_compare", "meta_analyze",
]
_COMPLETE = {
    "skills": _SKILLS,
    "domains": ["single_endpoint", "master_protocol", "doe", "randomization",
                "indirect_comparison", "meta_analysis"],
    "tools": _TOOLS,
    "r_packages": ["jsonlite", "survival", "Exact", "mvtnorm", "MAMS"],
    "expected_regression_pass_counts": dict(zip(_SKILLS, [31, 53, 14, 15, 14])),
}
_PROFILES = {
    "complete": _COMPLETE,
    "single-endpoint": {
        "skills": [_SKILLS[0]], "domains": ["single_endpoint"],
        "tools": _TOOLS[:4], "r_packages": ["jsonlite", "survival", "Exact"],
        "expected_regression_pass_counts": {_SKILLS[0]: 31},
    },
}


class RuntimeProfileError(ValueError):
    """The installed distribution has an invalid capability declaration."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RuntimeProfileError("runtime profile contains duplicate keys")
        value[key] = item
    return value


def validate_runtime_profiles(value: Any) -> dict[str, Any]:
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "active", "profiles"}
            or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or not isinstance(value.get("active"), str)
            or value["active"] not in _PROFILES):
        raise RuntimeProfileError("unsupported runtime profile declaration")
    catalog = value.get("profiles")
    # Canonical serialization also distinguishes bool from int and float from int.
    if json.dumps(catalog, sort_keys=True) != json.dumps(_PROFILES, sort_keys=True):
        raise RuntimeProfileError("runtime profiles differ from the reviewed capability catalog")
    name = value["active"]
    return {"name": name, **copy.deepcopy(_PROFILES[name])}


def load_runtime_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    try:
        if (path.parent.is_symlink() or not path.parent.is_dir()
                or path.is_symlink() or not path.is_file() or path.stat().st_size > 65536):
            raise RuntimeProfileError("runtime profile must be a regular, non-symlink file")
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeProfileError("runtime profile could not be loaded") from exc
    return validate_runtime_profiles(value)
