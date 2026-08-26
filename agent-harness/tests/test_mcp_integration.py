#!/usr/bin/env python3
"""End-to-end MCP startup, schema, path, and persistence smoke tests."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", "..", "hooks"))

from mcp_client import (  # noqa: E402
    MCPClient,
    MCPToolError,
    PUBLIC_TOOL_ERROR_MESSAGES,
)
from final_report import public_arguments_view  # noqa: E402
from gates import combined_gate, design_checks_for  # noqa: E402
from gates import check_reproducibility  # noqa: E402
from verification import content_hash, public_arguments_hash  # noqa: E402
import verification_policy  # noqa: E402


passed = failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"TEST {name} : PASS")
        passed += 1
    else:
        print(f"TEST {name} : FAIL {detail}")
        failed += 1


def expect_tool_error(name, fn):
    try:
        fn()
    except MCPToolError:
        check(name, True)
    else:
        check(name, False, "call unexpectedly succeeded")


def expect_invalid_request(name, fn):
    try:
        fn()
    except MCPToolError as exc:
        expected = (
            "Tool request failed [invalid_request]: "
            + PUBLIC_TOOL_ERROR_MESSAGES["invalid_request"]
        )
        check(name, str(exc) == expected, str(exc))
    else:
        check(name, False, "call unexpectedly succeeded")


def private_resource_paths(result):
    paths = []
    handles = result.get("_private_provenance", {}).get("artifact_handles", {})
    for resource in result.get("_private_resources") or []:
        if not isinstance(resource, dict):
            continue
        handle = resource.get("_meta", {}).get(
            "experiment-design/private-artifact", {}).get("handle")
        path = handles.get(handle) if isinstance(handles, dict) else None
        if isinstance(path, str):
            paths.append(Path(path))
    return paths


def artifact_directory(result):
    paths = private_resource_paths(result)
    return paths[0].parent if paths else None


def live_hook_allows(tool_name, arguments, result, regression_result):
    server = "mcp__experiment-design__"
    model_result = {
        key: value for key, value in result.items()
        if key not in {"_private_provenance", "_private_resources"}
    }
    lines = [
        {"message": {"id": "analysis", "role": "assistant", "content": [{
            "type": "tool_use", "id": "analysis-call",
            "name": server + tool_name, "input": arguments,
        }]}},
        {"message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "analysis-call",
            "content": [{"type": "text", "text": json.dumps(model_result)}],
        }]}},
        {"message": {"id": "tests", "role": "assistant", "content": [{
            "type": "tool_use", "id": "tests-call",
            "name": server + "run_tests", "input": {},
        }]}},
        {"message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": "tests-call",
            "content": [{"type": "text", "text": json.dumps(regression_result)}],
        }]}},
    ]
    data = {
        "hook_event_name": "Stop",
        "agent_type": "experiment-designer",
        "_expdesign_agent_scope": "experiment-designer",
        "last_assistant_message": result["_verification"]["report"],
    }
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr):
            verification_policy.enforce(
                data, line_source=lambda _data: [json.dumps(line) for line in lines],
            )
    except SystemExit as exc:
        return int(exc.code or 0) == 0, stderr.getvalue()
    return True, stderr.getvalue()


output_dir = None
with MCPClient() as client:
    tools = client.list_tools()
    check("server_lists_all_tools", len(tools) == 11, len(tools))
    master_tool = next(tool for tool in tools if tool.get("name") == "master_simulate")
    master_schema_fields = (
        master_tool.get("inputSchema", {}).get("properties", {})
        .get("config", {}).get("properties", {})
    )
    check("master_effect_threshold_is_absent_from_public_schema",
          "effect_threshold" not in master_schema_fields, master_schema_fields)
    legacy_master_arguments = {"config": {
        "master_design_type": "platform", "endpoint_type": "binary",
        "n_subgroups": 2, "null_params": 0.2, "alt_params": [0.4, 0.4],
        "n_periods": 2, "n_per_period": 10,
        "arms_schedule": {"enter": [1, 1], "leave": [2, 2]},
        "ncc_method": "none", "futility_threshold": 0.05,
        "effect_threshold": 0.99,
    }}
    public_master_arguments = public_arguments_view(
        "master_simulate", legacy_master_arguments,
    )
    check("master_effect_threshold_is_absent_from_public_argument_projection",
          "effect_threshold" not in public_master_arguments.get("config", {})
          and public_master_arguments.get("config", {}).get("futility_threshold") == 0.05,
          public_master_arguments)

    tests = client.call_tool("run_tests", {})
    check("run_tests_fixed_public_status", tests.get("all_ok") is True
          and set(tests) == {"all_ok", "suite_count", "passed_suite_count",
                             "failed_suite_count", "checks"}
          and tests.get("suite_count") == 5
          and tests.get("passed_suite_count") == 5
          and tests.get("failed_suite_count") == 0
          and tests.get("checks") == {
              "declared_all_ok": True,
              "complete_suite_set": True,
              "suite_records_valid": True,
              "expected_check_count": True,
          }
          and "output" not in str(tests).lower()
          and "vera-" not in str(tests), tests)

    validated = client.call_tool("validate_config", {
        "endpoint_type": "binary", "study_type": "poc",
        "design": "single_arm", "null_param": 0.2, "alt_param": 0.4,
        "alphas": 0.1, "powers": 0.8,
    })
    check("validate_config_scalar_grids_are_normalized_in_strict_public_dto",
          set(validated) == {"valid", "endpoint_type", "study_type", "design",
                             "go_target", "alphas", "powers", "resolved_config",
                             "simulation_defaults", "configuration_report"}
          and validated.get("valid") is True
          and validated.get("alphas") == [0.1]
          and validated.get("powers") == [0.8]
          and validated.get("resolved_config", {}).get("sidedness") == "one_sided"
          and validated.get("resolved_config", {}).get("estimand") == "response_probability"
          and validated.get("resolved_config", {}).get("prior_params") == {"a": 0.5, "b": 0.5}
          and validated.get("simulation_defaults") == {"seed": 42, "b_oc": 5000}
          and validated.get("configuration_report", "").startswith(
              "CONFIGURATION_VALIDATED {"), validated)

    try:
        client.call_tool("sample_size", {
            "endpoint_type": "binary", "study_type": "poc",
            "design": "single_arm", "null_param": 0.4, "alt_param": 0.2,
        })
        safe_error = False
    except MCPToolError as exc:
        safe_error = str(exc) == (
            "Tool request failed [invalid_request]: "
            + PUBLIC_TOOL_ERROR_MESSAGES["invalid_request"]
        )
    check("invalid_design_returns_error_without_result_payload", safe_error)

    expect_tool_error(
        "unknown_top_level_key_rejected",
        lambda: client.call_tool("validate_config", {
            "endpoint_type": "binary", "study_type": "poc",
            "design": "single_arm", "null_param": 0.2, "alt_param": 0.4,
            "powres": 0.99,
        }),
    )
    expect_tool_error(
        "resource_limit_rejected",
        lambda: client.call_tool("randomize", {"n": 10001}),
    )
    expect_tool_error(
        "fractional_seed_rejected",
        lambda: client.call_tool("randomize", {"n": 10, "seed": 1.5}),
    )
    expect_tool_error(
        "zero_block_size_rejected",
        lambda: client.call_tool("randomize", {
            "n": 10, "method": "block", "block_size": 0,
        }),
    )
    expect_tool_error(
        "noninteger_factor_levels_rejected",
        lambda: client.call_tool("factorial_design", {
            "n_factors": 2, "levels": 2.5,
        }),
    )
    expect_invalid_request(
        "custom_factorial_generator_count_rejected_before_r",
        lambda: client.call_tool("factorial_design", {
            "n_factors": 4, "fraction": 1, "generators": [],
        }),
    )
    expect_invalid_request(
        "custom_factorial_generator_must_use_basic_factors",
        lambda: client.call_tool("factorial_design", {
            "n_factors": 4, "fraction": 1, "generators": [[1, 4]],
        }),
    )
    expect_invalid_request(
        "custom_factorial_generator_requires_fraction",
        lambda: client.call_tool("factorial_design", {
            "n_factors": 4, "generators": [[1, 2]],
        }),
    )
    expect_tool_error(
        "oversized_factorial_rejected",
        lambda: client.call_tool("factorial_design", {
            "n_factors": 5, "levels": 10,
        }),
    )
    expect_tool_error(
        "nonpositive_ccd_alpha_rejected",
        lambda: client.call_tool("rsm_design", {
            "n_factors": 2, "design": "ccd", "alpha": 0,
        }),
    )
    expect_tool_error(
        "invalid_source_ci_level_rejected",
        lambda: client.call_tool("meta_analyze", {
            "endpoint_type": "time_to_event",
            "studies": [
                {"hr": 0.8, "ci_lower": 0.6, "ci_upper": 0.95,
                 "source_ci_level": 1},
                {"hr": 0.9, "ci_lower": 0.7, "ci_upper": 1.1},
            ],
        }),
    )
    expect_tool_error(
        "invalid_go_target_rejected",
        lambda: client.call_tool("validate_config", {
            "endpoint_type": "binary", "study_type": "poc",
            "design": "single_arm", "null_param": 0.2, "alt_param": 0.4,
            "go_target": 2,
        }),
    )
    expect_tool_error(
        "inverted_decision_thresholds_rejected",
        lambda: client.call_tool("validate_config", {
            "endpoint_type": "binary", "study_type": "poc",
            "design": "single_arm", "null_param": 0.2, "alt_param": 0.4,
            "consider_threshold": 0.95, "go_threshold": 0.9,
        }),
    )
    expect_tool_error(
        "coupled_oc_workload_rejected",
        lambda: client.call_tool("simulate_design", {
            "config": {"endpoint_type": "binary", "study_type": "poc",
                       "design": "single_arm", "null_param": 0.2, "alt_param": 0.4},
            "n_oc": 100000, "B_oc": 5000,
        }),
    )
    expect_tool_error(
        "external_read_path_rejected",
        lambda: client.call_tool("indirect_compare", {
            "method": "maic", "ipd_file": "/etc/hosts",
            "targets_file": "/etc/hosts", "treatment_arm": "A",
        }),
    )
    expect_tool_error(
        "whitespace_padded_path_rejected_without_normalization",
        lambda: client.call_tool("indirect_compare", {
            "method": "maic", "ipd_file": " /etc/hosts",
            "targets_file": "/etc/hosts", "treatment_arm": "A",
        }),
    )
    expect_tool_error(
        "controlled_zero_baseline_incidence_rejected",
        lambda: client.call_tool("simulate_design", {
            "config": {"endpoint_type": "incidence_rate", "study_type": "poc",
                       "design": "controlled", "null_param": 0, "alt_param": 1,
                       "exposure_time": 1},
            "n_oc": {"n_trt": 2, "n_ctrl": 2}, "B_oc": 1,
        }),
    )
    expect_tool_error(
        "master_zero_rate_log_effect_rejected",
        lambda: client.call_tool("master_simulate", {"config": {
            "master_design_type": "platform", "endpoint_type": "incidence_rate",
            "n_subgroups": 2, "null_params": 0, "alt_params": [0, 0],
            "exposure_time": 1, "n_periods": 2, "n_per_period": 2,
            "arms_schedule": {"enter": [1, 1], "leave": [2, 2]}, "n_sims": 1,
        }}),
    )
    expect_tool_error(
        "nested_ppos_failure_is_top_level_tool_error",
        lambda: client.call_tool("simulate_design", {
            "config": {"endpoint_type": "continuous", "study_type": "confirmatory",
                       "design": "single_arm", "null_param": 0, "alt_param": 1,
                       "sd": 1, "p2_data": {"x_bar": 1e308, "s2": 1, "n": 10},
                       "p3_n": 20},
            "n_oc": 2, "B_oc": 1,
        }),
    )
    expect_tool_error(
        "freeform_ppos_fields_rejected_at_schema",
        lambda: client.call_tool("simulate_design", {
            "config": {"endpoint_type": "binary", "study_type": "confirmatory",
                       "design": "single_arm", "null_param": 0.2, "alt_param": 0.4,
                       "p2_data": {"x": 2, "n": 10, "custom_note": "MRN-4242"},
                       "p3_n": 20},
            "n_oc": 2, "B_oc": 1,
        }),
    )

    master_args = {
        "verification_id": "integration-master",
        "config": {
            "master_design_type": "basket", "endpoint_type": "binary",
            "n_subgroups": 2, "null_params": 0.2,
            "alt_params": [0.4, 0.4], "n_per_subgroup": 10,
            "n_sims": 1, "seed": 42,
        },
    }
    master = client.call_tool("master_simulate", master_args)
    output_dir = artifact_directory(master)
    check("default_output_persists", bool(output_dir) and Path(output_dir).is_dir(), master)
    envelope_id = master.get("_verification", {}).get("identity", {}).get("analysis_id")
    check("runtime_verification_id_replaces_model_hint",
          isinstance(envelope_id, str) and envelope_id.startswith("analysis-")
          and envelope_id != "integration-master", master.get("_verification"))
    check("provenance_is_identity_bound",
          master.get("_verification", {}).get("identity", {}).get("provenance_hash")
          == content_hash(master.get("_provenance", {})), master.get("_verification"))
    check("master_identity_binds_public_caller_domain",
          master.get("_verification", {}).get("identity", {}).get("public_args_hash")
          == public_arguments_hash("master_simulate", master_args)
          and "args_hash" not in master.get("_verification", {}).get("identity", {}),
          master.get("_verification"))
    server_checks = master.get("_verification", {}).get("checks", {})
    check("master_result_contract_passes",
          server_checks.get("artifact_integrity") is True
          and all(value is not False for value in server_checks.values()), server_checks)
    public_provenance = master.get("_provenance", {})
    artifact_hashes = master.get("_private_provenance", {}).get("artifact_hashes", {})
    check("public_provenance_is_non_oracular",
          public_provenance.get("artifact_count") == len(artifact_hashes)
          and public_provenance.get("config_bound") is True
          and not ({"config_hash", "input_hashes", "artifact_hashes"}
                   & set(public_provenance)), public_provenance)
    check("private_provenance_excludes_lifecycle_control_files",
          bool(artifact_hashes)
          and not any(name.startswith(".expdesign-") for name in artifact_hashes),
          artifact_hashes)
    check("server_envelope_attests_regression_health",
          server_checks.get("regression_suite") is True,
          server_checks)
    hook_ok, hook_detail = live_hook_allows("master_simulate", master_args, master, tests)
    check("live_master_result_passes_stop_hook", hook_ok, hook_detail)

    # The dispatcher serializes with auto_unbox=TRUE, so a length-1 R vector
    # reaches the gate as a scalar. Those shapes appear only in one-word,
    # one-stage, and one-method results, so exercise them against the real
    # serializer rather than a hand-built payload.
    half_fraction_args = {"n_factors": 5, "fraction": 1}
    half_fraction = client.call_tool("factorial_design", half_fraction_args)
    check("live_half_fraction_defining_relation_is_presentable",
          half_fraction.get("_verification", {}).get("presentable") is True,
          half_fraction.get("_verification", {}).get("failures"))
    hook_ok, hook_detail = live_hook_allows(
        "factorial_design", half_fraction_args, half_fraction, tests,
    )
    check("live_half_fraction_passes_stop_hook", hook_ok, hook_detail)

    custom_fraction_args = {
        "n_factors": 4, "fraction": 1, "generators": [[1, 2]],
    }
    custom_fraction = client.call_tool("factorial_design", custom_fraction_args)
    check("live_custom_fraction_reports_bound_generator_metadata",
          custom_fraction.get("generators") == [{
              "generated_factor_index": 4,
              "source_factor_indices": [1, 2],
          }]
          and custom_fraction.get("defining_relation") == [[1, 2, 4]]
          and custom_fraction.get("alias_structure", {}).get("scope")
          == "main_and_two_factor"
          and custom_fraction.get("_verification", {}).get("presentable") is True,
          custom_fraction.get("_verification", {}).get("failures"))
    hook_ok, hook_detail = live_hook_allows(
        "factorial_design", custom_fraction_args, custom_fraction, tests,
    )
    check("live_custom_fraction_passes_stop_hook", hook_ok, hook_detail)

    replicated_fraction_args = {
        "n_factors": 5, "fraction": 2, "replicates": 2,
        "center_points": 1,
    }
    replicated_fraction = client.call_tool(
        "factorial_design", replicated_fraction_args,
    )
    check("live_fractional_replicates_are_honored_and_presentable",
          replicated_fraction.get("replicates") == 2
          and replicated_fraction.get("n_runs") == 17
          and replicated_fraction.get("_verification", {}).get("presentable") is True,
          replicated_fraction.get("_verification", {}).get("failures"))
    hook_ok, hook_detail = live_hook_allows(
        "factorial_design", replicated_fraction_args, replicated_fraction, tests,
    )
    check("live_replicated_fraction_passes_stop_hook", hook_ok, hook_detail)

    single_stage_args = {"config": {
        "master_design_type": "umbrella", "endpoint_type": "continuous",
        "n_subgroups": 2, "n_arms": 2, "n_stages": 1, "n_per_arm_stage": 10,
        "sd": 1.0, "null_params": 0.0, "alt_params": [0.5, 0.5],
        "n_sims": 1, "seed": 42,
    }}
    single_stage = client.call_tool("master_simulate", single_stage_args)
    check("live_single_stage_umbrella_boundary_is_presentable",
          single_stage.get("_verification", {}).get("presentable") is True,
          single_stage.get("_verification", {}).get("failures"))
    hook_ok, hook_detail = live_hook_allows(
        "master_simulate", single_stage_args, single_stage, tests,
    )
    check("live_single_stage_umbrella_passes_stop_hook", hook_ok, hook_detail)

    single_method_args = {"config": {
        "master_design_type": "platform", "endpoint_type": "binary",
        "n_subgroups": 2, "null_params": 0.2, "alt_params": [0.4, 0.4],
        "n_periods": 2, "n_per_period": 10,
        "arms_schedule": {"enter": [1, 1], "leave": [2, 2]},
        "n_sims": 1, "seed": 42,
    }}
    single_method = client.call_tool("master_simulate", single_method_args)
    check("live_single_analysis_method_platform_is_presentable",
          single_method.get("_verification", {}).get("presentable") is True,
          single_method.get("_verification", {}).get("failures"))
    hook_ok, hook_detail = live_hook_allows(
        "master_simulate", single_method_args, single_method, tests,
    )
    check("live_single_analysis_method_platform_passes_stop_hook",
          hook_ok, hook_detail)

    random_args = {"n": 8, "seed": 42}
    random_one = client.call_tool("randomize", random_args)
    random_two = client.call_tool("randomize", random_args)
    check("live_stochastic_replay_ignores_runtime_metadata",
          check_reproducibility(random_one, random_two).passed,
          check_reproducibility(random_one, random_two).failures)
    random_paths = private_resource_paths(random_one)
    check("randomization_is_private_artifact_not_model_payload",
          bool(random_paths) and random_paths[0].is_file()
          and "assignment" not in random_one
          and "assignment" in random_paths[0].read_text(encoding="utf-8"),
          random_one)
    random_resources = random_one.get("_private_resources") or []
    check("private_artifacts_are_user_audience_only",
          bool(random_resources)
          and all(isinstance(resource, dict)
                  and resource.get("annotations", {}).get("audience") == ["user"]
                  for resource in random_resources),
          random_resources)
    random_private = random_one.get("_private_provenance", {})
    random_identity = random_one.get("_verification", {}).get("identity", {}).get(
        "analysis_id")
    check("private_artifact_metadata_is_identity_and_digest_bound",
          bool(random_resources)
          and random_private.get("analysis_id") == random_identity
          and all(
              resource.get("_meta", {}).get(
                  "experiment-design/private-artifact", {}).get("analysis_id")
              == random_identity
              and resource.get("_meta", {}).get(
                  "experiment-design/private-artifact", {}).get("sha256")
              == random_private.get("artifact_hashes", {}).get(resource.get("name"))
              and resource.get("_meta", {}).get(
                  "experiment-design/private-artifact", {}).get("handle")
              in random_private.get("artifact_handles", {})
              and str(resource.get("uri", "")).startswith("expdesign-artifact://")
              and not str(resource.get("uri", "")).startswith("file:")
              and random_private.get("artifact_handles", {}).get(
                  resource.get("_meta", {}).get(
                      "experiment-design/private-artifact", {}).get("handle"), ""
              ) not in str(resource.get("uri", ""))
              for resource in random_resources
          ), (random_resources, random_private))

    expect_tool_error(
        "managed_output_artifact_cannot_be_reused_as_analysis_input",
        lambda: client.call_tool("indirect_compare", {
            "method": "maic", "ipd_file": str(random_paths[0]),
            "targets_file": str(random_paths[0]), "treatment_arm": "A",
        }),
    )

    controlled_config = {
        "endpoint_type": "binary", "study_type": "poc", "design": "controlled",
        "null_param": 0.2, "alt_param": 0.5, "go_target": 0.35,
    }
    derived_margin = client.call_tool("simulate_design", {
        "config": controlled_config, "n_oc": {"n_trt": 30, "n_ctrl": 30},
        "B_oc": 20, "seed": 4,
    })
    zero_margin = client.call_tool("simulate_design", {
        "config": controlled_config, "n_oc": {"n_trt": 30, "n_ctrl": 30},
        "B_oc": 20, "seed": 4, "delta": 0,
    })
    derived_row = next(row for row in derived_margin["oc"] if row["true_param"] == 0.5)
    zero_row = next(row for row in zero_margin["oc"] if row["true_param"] == 0.5)
    check("mcp_omitted_delta_uses_go_target_margin",
          zero_row["p_go"] > derived_row["p_go"] + 0.2,
          (derived_row, zero_row))

if os.name == "posix":
    with tempfile.TemporaryDirectory(prefix="expdesign-master-contract-") as directory:
        contract_root = Path(directory)
        r_marker = contract_root / "unexpected-r-execution.txt"
        r_shim = contract_root / "forbidden-rscript.sh"
        r_shim.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' executed > {shlex.quote(str(r_marker))}\n"
            "exit 99\n",
            encoding="utf-8",
        )
        r_shim.chmod(0o700)
        old_rscript = os.environ.get("EXPDESIGN_RSCRIPT")
        os.environ["EXPDESIGN_RSCRIPT"] = str(r_shim)
        platform_base = {
            "master_design_type": "platform", "endpoint_type": "binary",
            "n_subgroups": 2, "null_params": 0.2, "alt_params": [0.4, 0.4],
            "n_periods": 2, "n_per_period": 10,
            "arms_schedule": {"enter": [1, 1], "leave": [2, 2]},
            "n_sims": 1,
        }
        try:
            with MCPClient() as contract_client:
                for required_field in ("n_periods", "n_per_period", "arms_schedule"):
                    incomplete_platform = dict(platform_base)
                    incomplete_platform.pop(required_field)
                    expect_invalid_request(
                        f"platform_requires_{required_field}_before_r",
                        lambda config=incomplete_platform: contract_client.call_tool(
                            "master_simulate", {"config": config},
                        ),
                    )
                expect_invalid_request(
                    "platform_schedule_length_fails_before_r",
                    lambda: contract_client.call_tool("master_simulate", {"config": {
                        **platform_base,
                        "arms_schedule": {"enter": [1, 1, 1], "leave": [2, 2, 2]},
                    }}),
                )
                expect_invalid_request(
                    "platform_schedule_range_fails_before_r",
                    lambda: contract_client.call_tool("master_simulate", {"config": {
                        **platform_base,
                        "arms_schedule": {"enter": [0, 2], "leave": [2, 3]},
                    }}),
                )
                expect_tool_error(
                    "platform_schedule_noninteger_fails_at_schema_before_r",
                    lambda: contract_client.call_tool("master_simulate", {"config": {
                        **platform_base,
                        "arms_schedule": {"enter": [1, 1.5], "leave": [2, 2]},
                    }}),
                )
                nonplatform_base = {
                    "master_design_type": "basket", "endpoint_type": "binary",
                    "n_subgroups": 2, "null_params": 0.2,
                    "alt_params": [0.4, 0.4], "n_per_subgroup": 10,
                    "n_sims": 1,
                }
                platform_only_examples = {
                    "n_periods": 2,
                    "n_per_period": 10,
                    "arms_schedule": {"enter": [1, 1], "leave": [2, 2]},
                    "interim_frequency": 1,
                    "futility_threshold": 0.05,
                }
                for field, value in platform_only_examples.items():
                    expect_invalid_request(
                        f"nonplatform_rejects_{field}_before_r",
                        lambda key=field, item=value: contract_client.call_tool(
                            "master_simulate",
                            {"config": {**nonplatform_base, key: item}},
                        ),
                    )
                expect_invalid_request(
                    "platform_interim_endpoint_combination_fails_before_r",
                    lambda: contract_client.call_tool("master_simulate", {"config": {
                        **platform_base, "endpoint_type": "tte", "null_params": 1,
                        "alt_params": [0.8, 0.8], "ncc_method": "none",
                        "interim_frequency": 1,
                    }}),
                )
                expect_invalid_request(
                    "platform_interim_ncc_combination_fails_before_r",
                    lambda: contract_client.call_tool("master_simulate", {"config": {
                        **platform_base, "futility_threshold": 0.05,
                    }}),
                )
                expect_tool_error(
                    "legacy_effect_threshold_fails_at_schema_before_r",
                    lambda: contract_client.call_tool("master_simulate", {"config": {
                        **platform_base, "effect_threshold": 0.99,
                    }}),
                )
                expect_invalid_request(
                    "simple_randomization_rejects_nonequal_ratio_before_r",
                    lambda: contract_client.call_tool("randomize", {
                        "n": 12, "ratio": [1, 2], "seed": 42,
                    }),
                )
            check("invalid_master_and_randomize_contract_requests_never_reach_r",
                  not r_marker.exists(), r_marker)
        finally:
            if old_rscript is None:
                os.environ.pop("EXPDESIGN_RSCRIPT", None)
            else:
                os.environ["EXPDESIGN_RSCRIPT"] = old_rscript
else:
    check("invalid_master_and_randomize_contract_requests_never_reach_r",
          True, "POSIX-only shim")

if output_dir:
    shutil.rmtree(output_dir, ignore_errors=True)

suite = Path(__file__).resolve().parents[2]
integrity_script = r'''
import {
  currentRRuntimeSnapshot, FingerprintPromiseCache, REGRESSION_SKILLS,
  TRACKED_R_BASE_PACKAGES, TRACKED_R_PACKAGES, mutableEngineFiles,
} from "./mcp-server/dist/integrity.js";
import {readBoundedFile} from "./mcp-server/dist/bounded-read.js";
import {publishVerifiedArtifacts} from "./mcp-server/dist/artifact-publication.js";
import {createHash} from "node:crypto";
import {appendFile, mkdtemp, open, rm, writeFile} from "node:fs/promises";
import {realpathSync, statSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
const root = process.cwd();
const files = mutableEngineFiles(root);
const cache = new FingerprintPromiseCache();
let loads = 0;
let releaseLoad;
const gate = new Promise((resolve) => { releaseLoad = resolve; });
const loader = async () => { loads += 1; await gate; return {ok: true}; };
const first = cache.get("same-fingerprint", loader);
const second = cache.get("same-fingerprint", loader);
releaseLoad();
const [a, b] = await Promise.all([first, second]);
const orderingCache = new FingerprintPromiseCache();
let releaseOld;
const oldGate = new Promise((resolve) => { releaseOld = resolve; });
const oldLoad = orderingCache.get("old", async () => {
  await oldGate;
  return {generation: "old"};
});
const newest = await orderingCache.get("new", async () => ({generation: "new"}));
releaseOld();
await oldLoad;
let newerReloads = 0;
const stillNewest = await orderingCache.get("new", async () => {
  newerReloads += 1;
  return {generation: "unexpected-reload"};
});
const testsBound = REGRESSION_SKILLS.every((skill) =>
  files.includes(`${root}/${skill}/scripts/tests/run_tests.R`))
  && files.includes(`${root}/agent-harness/artifact_download.py`);
const runtimeOne = await currentRRuntimeSnapshot();
const runtimeTwo = await currentRRuntimeSnapshot();
const runtimeBound = runtimeOne.fingerprint === runtimeTwo.fingerprint
  && /^[0-9a-f]{64}$/.test(runtimeOne.fingerprint)
  && typeof runtimeOne.version === "string"
  && [...TRACKED_R_PACKAGES, ...TRACKED_R_BASE_PACKAGES].every((name) =>
    Object.prototype.hasOwnProperty.call(runtimeOne.packageVersions, name));
const boundedRoot = await mkdtemp(join(tmpdir(), "expdesign-bounded-read-"));
const boundedPath = join(boundedRoot, "growing.csv");
await writeFile(boundedPath, Buffer.alloc(64));
const boundedHandle = await open(boundedPath, "r");
await appendFile(boundedPath, Buffer.from([1]));
let concurrentAppendRejected = false;
try {
  await readBoundedFile(boundedHandle, 64, "test input");
} catch {
  concurrentAppendRejected = true;
} finally {
  await boundedHandle.close();
  await rm(boundedRoot, {recursive: true, force: true});
}
const publicationRoot = await mkdtemp(join(tmpdir(), "expdesign-publication-"));
const publicationPath = join(publicationRoot, "verified.csv");
const publicationBytes = Buffer.from("unit,arm\n1,A\n");
const publicationDigest = createHash("sha256").update(publicationBytes).digest("hex");
await writeFile(publicationPath, publicationBytes);
const published = publishVerifiedArtifacts(
  [publicationPath], publicationRoot, {"verified.csv": publicationDigest});
const publishedReadOnly = (statSync(publicationPath).mode & 0o777) === 0o400;
const mutatedPath = join(publicationRoot, "mutated.csv");
await writeFile(mutatedPath, Buffer.from("changed\n"));
let mutationRejected = false;
try {
  publishVerifiedArtifacts(
    [mutatedPath], publicationRoot, {"mutated.csv": publicationDigest});
} catch {
  mutationRejected = true;
}
const artifactPublicationBound = published.length === 1
  && published[0] === realpathSync(publicationPath)
  && publishedReadOnly && mutationRejected;
await rm(publicationRoot, {recursive: true, force: true});
console.log(JSON.stringify({
  loads, shared: a === b, testsBound,
  staleCannotOverwrite: newest === stillNewest && newerReloads === 0,
  runtimeBound, concurrentAppendRejected, artifactPublicationBound,
}));
'''
integrity = subprocess.run(
    ["node", "--input-type=module", "-e", integrity_script], cwd=suite,
    capture_output=True, text=True,
)
integrity_result = json.loads(integrity.stdout) if integrity.returncode == 0 else {}
check("runtime_cache_and_bounded_io_guards_are_active",
      integrity_result == {
          "loads": 1, "shared": True, "testsBound": True,
          "staleCannotOverwrite": True, "runtimeBound": True,
          "concurrentAppendRejected": True,
          "artifactPublicationBound": True,
      },
      (integrity_result, integrity.stderr))

if hasattr(os, "mkfifo"):
    with tempfile.TemporaryDirectory() as safe_read_directory:
        safe_root = Path(safe_read_directory)
        regular_input = safe_root / "regular.csv"
        fifo_input = safe_root / "blocked.fifo"
        regular_input.write_text("a,b\n1,2\n", encoding="utf-8")
        os.mkfifo(fifo_input)
        safe_read_script = f'''
          import {{readAllowedFile}} from "./mcp-server/dist/index.js";
          const regular = await readAllowedFile({json.dumps(str(regular_input))});
          let fifoRejected = false;
          try {{ await readAllowedFile({json.dumps(str(fifo_input))}); }}
          catch {{ fifoRejected = true; }}
          console.log(JSON.stringify({{regular: regular.length > 0, fifoRejected}}));
        '''
        safe_read = subprocess.run(
            ["node", "--input-type=module", "-e", safe_read_script], cwd=suite,
            capture_output=True, text=True, timeout=10,
            env=dict(
                os.environ,
                EXPDESIGN_ALLOWED_READ_ROOTS=str(safe_root),
                EXPDESIGN_RUNS_DIR=str(safe_root / "runs"),
            ),
        )
        safe_read_result = (
            json.loads(safe_read.stdout) if safe_read.returncode == 0 else {}
        )
        check("input_authorization_rejects_fifo_without_blocking",
              safe_read_result == {"regular": True, "fifoRejected": True},
              (safe_read_result, safe_read.stderr))

index_source = (suite / "mcp-server" / "src" / "index.ts").read_text(encoding="utf-8")
preverify_calls = index_source.count("await preverify(")
postverify_guards = index_source.count(
    'assertEngineUnchanged(executionFingerprint, "the verifier was running", signal);'
)
check("every_gated_path_rechecks_fingerprint_after_verification",
      preverify_calls >= 1 and postverify_guards == preverify_calls,
      (preverify_calls, postverify_guards))

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    ipd = root / "ipd.csv"
    targets = root / "targets.csv"
    ipd.write_text(
        "participant_id,arm,response,age_scaled\n" +
        "\n".join([
            "P1,TRT,1,-1", "P2,TRT,1,0", "P3,TRT,0,1", "P4,TRT,1,0",
            "P5,CTRL,0,-1", "P6,CTRL,1,0", "P7,CTRL,0,1", "P8,CTRL,0,0",
        ]) + "\n", encoding="utf-8",
    )
    targets.write_text("covariate,target_mean\nage_scaled,0\n", encoding="utf-8")
    old_reads = os.environ.get("EXPDESIGN_ALLOWED_READ_ROOTS")
    old_runs = os.environ.get("EXPDESIGN_RUNS_DIR")
    os.environ["EXPDESIGN_ALLOWED_READ_ROOTS"] = os.pathsep.join([
        str(Path(__file__).resolve().parents[2]), str(root),
    ])
    os.environ["EXPDESIGN_RUNS_DIR"] = str(root / "runs")
    try:
        oversized_ipd = root / "oversized.csv"
        with oversized_ipd.open("wb") as handle:
            handle.truncate(50 * 1024 * 1024 + 1)
        with MCPClient() as client:
            maic = client.call_tool("indirect_compare", {
                "method": "maic", "ipd_file": str(ipd),
                "targets_file": str(targets), "treatment_arm": "TRT",
                "comparator_arm": "CTRL", "covariates": ["age_scaled"],
            })
            symlinked = root / "symlinked-ipd.csv"
            symlinked.symlink_to("/etc/hosts")
            expect_tool_error(
                "symlinked_read_path_rejected",
                lambda: client.call_tool("indirect_compare", {
                    "method": "maic", "ipd_file": str(symlinked),
                    "targets_file": str(targets), "treatment_arm": "TRT",
                }),
            )
            expect_tool_error(
                "oversized_maic_input_rejected_before_read",
                lambda: client.call_tool("indirect_compare", {
                    "method": "maic", "ipd_file": str(oversized_ipd),
                    "targets_file": str(targets), "treatment_arm": "TRT",
                }),
            )
        encoded = str(maic)
        check("maic_model_payload_is_aggregate_only",
              "weights" not in maic and "participant_id" not in encoded
              and isinstance(maic.get("weight_summary"), dict), maic)
        original_maic_args = {
            "method": "maic", "ipd_file": str(ipd),
            "targets_file": str(targets), "treatment_arm": "TRT",
            "comparator_arm": "CTRL", "covariates": ["age_scaled"],
        }
        check("maic_identity_binds_only_public_caller_domain",
              maic.get("_verification", {}).get("identity", {}).get("public_args_hash")
              == public_arguments_hash("indirect_compare", original_maic_args)
              and "args_hash" not in maic.get("_verification", {}).get("identity", {}),
              maic.get("_verification"))
        import hashlib
        expected_input_hashes = {
                  "ipd_file": hashlib.sha256(ipd.read_bytes()).hexdigest(),
                  "targets_file": hashlib.sha256(targets.read_bytes()).hexdigest(),
              }
        check("maic_private_provenance_hashes_analyzed_input_bytes",
              maic.get("_private_provenance", {}).get("input_hashes")
              == expected_input_hashes, maic.get("_private_provenance"))
        check("maic_input_hashes_never_enter_model_visible_provenance",
              maic.get("_provenance", {}).get("input_file_count") == 2
              and not ({"config_hash", "input_hashes", "artifact_hashes"}
                       & set(maic.get("_provenance", {})))
              and all(digest not in json.dumps(maic.get("_provenance", {}))
                      for digest in expected_input_hashes.values()),
              maic.get("_provenance"))
    finally:
        if old_reads is None:
            os.environ.pop("EXPDESIGN_ALLOWED_READ_ROOTS", None)
        else:
            os.environ["EXPDESIGN_ALLOWED_READ_ROOTS"] = old_reads
        if old_runs is None:
            os.environ.pop("EXPDESIGN_RUNS_DIR", None)
        else:
            os.environ["EXPDESIGN_RUNS_DIR"] = old_runs

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory) / "runs"
    old_runs = os.environ.get("EXPDESIGN_RUNS_DIR")
    old_max = os.environ.get("EXPDESIGN_ARTIFACT_MAX_DIRS")
    os.environ["EXPDESIGN_RUNS_DIR"] = str(root)
    os.environ["EXPDESIGN_ARTIFACT_MAX_DIRS"] = "1"
    config = {
        "master_design_type": "basket", "endpoint_type": "binary",
        "n_subgroups": 2, "null_params": 0.2, "alt_params": [0.4, 0.4],
        "n_per_subgroup": 10, "n_sims": 1, "seed": 42,
    }
    try:
        unmarked = root / "master-user-data"
        unmarked.mkdir(parents=True)
        (unmarked / "keep.txt").write_text("user-owned", encoding="utf-8")
        forged = root / "forged-marker-user-data"
        forged.mkdir(parents=True)
        (forged / ".expdesign-managed.json").write_text(
            "not a valid managed marker", encoding="utf-8",
        )
        (forged / "keep.txt").write_text("user-owned", encoding="utf-8")
        with MCPClient() as client:
            first = client.call_tool("master_simulate", {"config": config})
            first_dir = artifact_directory(first)
            assert first_dir is not None
            forged_lease = first_dir / f".expdesign-active-{os.getpid()}-forged.json"
            forged_lease.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
            second = client.call_tool("master_simulate", {"config": config})
        second_dir = artifact_directory(second)
        check("artifact_retention_includes_new_directory",
              bool(second_dir and second_dir.is_dir())
              and bool(first_dir and not first_dir.exists()), list(root.iterdir()))
        check("forged_current_pid_lease_cannot_defeat_retention",
              not forged_lease.exists(), list(root.iterdir()))
        check("artifact_retention_preserves_unmarked_prefix_directory",
              (unmarked / "keep.txt").read_text(encoding="utf-8") == "user-owned",
              list(root.iterdir()))
        check("artifact_retention_rejects_forged_marker",
              (forged / "keep.txt").read_text(encoding="utf-8") == "user-owned",
              list(root.iterdir()))

        before_failed = {path.name for path in root.iterdir() if path.is_dir()}
        invalid_master = {
            "master_design_type": "umbrella", "endpoint_type": "continuous",
            "n_subgroups": 2, "null_params": 0,
            "alt_params": [0.5, 0.2], "umbrella_method": "mams",
            "n_stages": 2, "n_per_arm_stage": 5, "n_sims": 1, "seed": 9,
        }
        with MCPClient() as client:
            expect_tool_error(
                "failed_master_execution_is_reported",
                lambda: client.call_tool("master_simulate", {"config": invalid_master}),
            )
        after_failed = {path.name for path in root.iterdir() if path.is_dir()}
        check("failed_master_artifact_is_aborted",
              after_failed == before_failed, (before_failed, after_failed))

        shared = root / "shared-output"
        with MCPClient() as client, ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(client.call_tool, "master_simulate",
                            {"config": config, "output_dir": str(shared)})
                for _ in range(2)
            ]
            outcomes = []
            for future in futures:
                try:
                    outcomes.append(("ok", future.result()))
                except MCPToolError:
                    outcomes.append(("error", None))
        check("caller_output_directory_claim_is_atomic",
              sorted(kind for kind, _value in outcomes) == ["error", "ok"], outcomes)

        script_a = (
            'import {createManagedArtifactDir,releaseManagedArtifactDir} from '
            '"./mcp-server/dist/artifacts.js"; import {access} from "node:fs/promises"; '
            'const p=await createManagedArtifactDir("master-"); console.log(p); '
            'await new Promise(r=>setTimeout(r,2000)); let ok=true; '
            'try{await access(p)}catch{ok=false}; console.log(ok); '
            'await releaseManagedArtifactDir(p);'
        )
        script_b = (
            'import {createManagedArtifactDir,releaseManagedArtifactDir} from '
            '"./mcp-server/dist/artifacts.js"; '
            'const p=await createManagedArtifactDir("master-"); '
            'await releaseManagedArtifactDir(p);'
        )
        proc_a = subprocess.Popen(
            ["node", "--input-type=module", "-e", script_a], cwd=suite,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert proc_a.stdout is not None
        active_path = proc_a.stdout.readline().strip()
        proc_b = subprocess.run(
            ["node", "--input-type=module", "-e", script_b], cwd=suite,
            capture_output=True, text=True,
        )
        remaining_out, remaining_err = proc_a.communicate(timeout=10)
        check("cross_process_active_artifact_is_retained",
              bool(active_path) and proc_b.returncode == 0 and proc_a.returncode == 0
              and remaining_out.strip() == "true",
              (active_path, proc_b.stderr, remaining_out, remaining_err))

        lock = root / ".expdesign-lifecycle.lock"
        owner = lock / "owner.json"
        lock.mkdir()
        old_seconds = time.time() - 60
        owner.write_text(json.dumps({
            "token": "live-owner-token", "pid": os.getpid(),
            "created_at": old_seconds * 1000,
        }), encoding="utf-8")
        os.utime(owner, (old_seconds, old_seconds))
        os.utime(lock, (old_seconds, old_seconds))
        lock_env = dict(
            os.environ,
            EXPDESIGN_ARTIFACT_LOCK_WAIT_MS="150",
        )
        live_contender = subprocess.run(
            ["node", "--input-type=module", "-e", script_b], cwd=suite,
            capture_output=True, text=True, env=lock_env, timeout=5,
        )
        live_owner = json.loads(owner.read_text(encoding="utf-8")) if owner.exists() else {}
        check("old_lock_is_never_auto_stolen_from_live_owner",
              live_contender.returncode != 0 and lock.is_dir()
              and live_owner.get("token") == "live-owner-token",
              (live_contender.returncode, live_contender.stderr, live_owner))
        shutil.rmtree(lock)

        lock.mkdir()
        owner.write_text(json.dumps({
            "token": "dead-owner-token", "pid": 2147483647,
            "created_at": old_seconds * 1000,
        }), encoding="utf-8")
        os.utime(owner, (old_seconds, old_seconds))
        os.utime(lock, (old_seconds, old_seconds))
        dead_contender = subprocess.run(
            ["node", "--input-type=module", "-e", script_b], cwd=suite,
            capture_output=True, text=True, env=lock_env, timeout=5,
        )
        dead_owner = json.loads(owner.read_text(encoding="utf-8")) if owner.exists() else {}
        check("orphaned_lock_requires_explicit_safe_cleanup",
              dead_contender.returncode != 0 and lock.is_dir()
              and dead_owner.get("token") == "dead-owner-token"
              and "remove" in dead_contender.stderr,
              (dead_contender.returncode, dead_contender.stderr, dead_owner))
        shutil.rmtree(lock)

        handoff_script = (
            'import {createManagedArtifactDir,releaseManagedArtifactDir} from '
            '"./mcp-server/dist/artifacts.js"; import {access} from "node:fs/promises"; '
            'const a=await createManagedArtifactDir("master-"); '
            'const b=await createManagedArtifactDir("master-"); '
            'await releaseManagedArtifactDir(b); let ok=true; '
            'try{await access(b)}catch{ok=false}; console.log(JSON.stringify({ok,b})); '
            'await releaseManagedArtifactDir(a);'
        )
        handoff = subprocess.run(
            ["node", "--input-type=module", "-e", handoff_script], cwd=suite,
            capture_output=True, text=True,
        )
        handoff_result = json.loads(handoff.stdout) if handoff.returncode == 0 else {}
        check("just_completed_artifact_survives_release_handoff",
              handoff_result.get("ok") is True, (handoff_result, handoff.stderr))

        abort_script = (
            'import {abortManagedArtifactDir,createManagedArtifactDir} from '
            '"./mcp-server/dist/artifacts.js"; import {access} from "node:fs/promises"; '
            'const p=await createManagedArtifactDir("master-"); '
            'await abortManagedArtifactDir(p); let exists=true; '
            'try{await access(p)}catch{exists=false}; console.log(JSON.stringify({exists}));'
        )
        aborted = subprocess.run(
            ["node", "--input-type=module", "-e", abort_script], cwd=suite,
            capture_output=True, text=True,
        )
        aborted_result = json.loads(aborted.stdout) if aborted.returncode == 0 else {}
        check("abort_removes_unverified_artifact",
              aborted_result.get("exists") is False, (aborted_result, aborted.stderr))

        custom_script = (
            'import {createManagedArtifactDir,releaseManagedArtifactDir} from '
            '"./mcp-server/dist/artifacts.js"; import {readdir} from "node:fs/promises"; '
            f'const root={json.dumps(str(root))}; '
            'const a=await createManagedArtifactDir("master-",root+"/custom-one"); '
            'await releaseManagedArtifactDir(a); '
            'const b=await createManagedArtifactDir("master-",root+"/custom-two"); '
            'await releaseManagedArtifactDir(b); '
            'console.log(JSON.stringify((await readdir(root,{withFileTypes:true}))'
            '.filter(x=>x.isDirectory()&&!x.name.startsWith(".")).map(x=>x.name).sort()));'
        )
        custom = subprocess.run(
            ["node", "--input-type=module", "-e", custom_script], cwd=suite,
            capture_output=True, text=True,
        )
        custom_dirs = json.loads(custom.stdout) if custom.returncode == 0 else []
        check("caller_named_artifacts_obey_retention",
              custom_dirs == ["custom-two", "forged-marker-user-data", "master-user-data"],
              (custom_dirs, custom.stderr))
    finally:
        if old_runs is None:
            os.environ.pop("EXPDESIGN_RUNS_DIR", None)
        else:
            os.environ["EXPDESIGN_RUNS_DIR"] = old_runs
        if old_max is None:
            os.environ.pop("EXPDESIGN_ARTIFACT_MAX_DIRS", None)
        else:
            os.environ["EXPDESIGN_ARTIFACT_MAX_DIRS"] = old_max

print(f"\n--- Results: {passed} passed, {failed} failed ---")
sys.exit(1 if failed else 0)
