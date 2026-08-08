#!/usr/bin/env python3
"""Protocol-order tests for the stdio MCP client."""

from __future__ import annotations

import os
import json
import queue
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.realpath(__file__)), ".."))

from mcp_client import (CONTROL_TIMEOUT_S, PRIVATE_ARTIFACT_META_KEY,
                        PRIVATE_PROVENANCE_META_KEY, TOOL_TIMEOUT_S, MCPClient,
                        MCPToolError)  # noqa: E402


client = MCPClient()
client._proc = SimpleNamespace(poll=lambda: None, returncode=None)
client._waiters = {1: queue.Queue(maxsize=1), 2: queue.Queue(maxsize=1)}
client._waiters[2].put({"jsonrpc": "2.0", "id": 2, "result": {"value": "second"}})
client._waiters[1].put({"jsonrpc": "2.0", "id": 1, "result": {"value": "first"}})

first = client._recv(1)
second = client._recv(2)
ok = first == {"value": "first"} and second == {"value": "second"}
print(f"TEST out_of_order_responses_are_dispatched_by_id : {'PASS' if ok else 'FAIL'}")

restart_client = MCPClient()
restart_client._generation = 2
restart_client._closed = False
restart_waiter = queue.Queue(maxsize=1)
restart_client._waiters = {1: restart_waiter}
old_proc = SimpleNamespace(stdout=[])
restart_client._read_stdout(old_proc, 1)
restart_ok = not restart_client._closed and restart_waiter.empty()
print(f"TEST old_reader_cannot_poison_new_generation : {'PASS' if restart_ok else 'FAIL'}")

budget_ok = TOOL_TIMEOUT_S >= 390 and CONTROL_TIMEOUT_S < TOOL_TIMEOUT_S
print(f"TEST tool_timeout_covers_valid_server_budget : {'PASS' if budget_ok else 'FAIL'}")

timeout_client = MCPClient()
timeout_client._proc = SimpleNamespace(poll=lambda: None, returncode=None)
timeout_client._waiters = {9: queue.Queue(maxsize=1)}
sent = []
timeout_client._send = sent.append
try:
    timeout_client._recv(9, timeout_s=0.01)
except TimeoutError:
    timed_out = True
else:
    timed_out = False
cancel_ok = (
    timed_out
    and 9 not in timeout_client._waiters
    and sent == [{
        "jsonrpc": "2.0",
        "method": "notifications/cancelled",
        "params": {"requestId": 9, "reason": "client timeout"},
    }]
)
print(f"TEST timeout_cancels_request_and_cleans_waiter : {'PASS' if cancel_ok else 'FAIL'}")

late_client = MCPClient()
late_client._generation = 3
late_client._closed = False
late_waiter = queue.Queue(maxsize=2)
late_client._waiters = {10: late_waiter}
late_proc = SimpleNamespace(
    stdout=[
        '{"jsonrpc":"2.0","id":9,"result":{"value":"late"}}\n',
        '{"jsonrpc":"2.0","id":10,"result":{"value":"next"}}\n',
    ],
    poll=lambda: None,
    returncode=None,
)
late_client._proc = late_proc
late_client._read_stdout(late_proc, 3)
next_result = late_client._recv(10)
late_ok = next_result == {"value": "next"}
print(f"TEST late_cancelled_reply_cannot_contaminate_next_request : {'PASS' if late_ok else 'FAIL'}")

analysis_id = "analysis-private-meta"
input_digest = "1" * 64
artifact_digest = "2" * 64
artifact_handle = "12345678-1234-4123-8123-123456789abc"
artifact_path = "/managed/assignment.csv"
model_payload = {
    "method": "block",
    "_verification": {"identity": {"analysis_id": analysis_id}},
    "_provenance": {"config_bound": True, "input_file_count": 1,
                    "artifact_count": 1},
}
model_text = json.dumps(model_payload)
metadata_response = {
    "content": [
        {"type": "text", "text": model_text},
        {
            "type": "resource_link", "name": "assignment.csv",
            "uri": f"expdesign-artifact://{analysis_id}/{artifact_handle}",
            "annotations": {"audience": ["user"]},
            "_meta": {
                PRIVATE_ARTIFACT_META_KEY: {
                    "version": 2, "analysis_id": analysis_id,
                    "sha256": artifact_digest, "handle": artifact_handle,
                },
            },
        },
    ],
    "_meta": {
        PRIVATE_PROVENANCE_META_KEY: {
            "version": 2, "analysis_id": analysis_id,
            "config_hash": "3" * 64,
            "input_hashes": {"ipd_file": input_digest},
            "artifact_handles": {artifact_handle: artifact_path},
        },
    },
}
metadata_client = MCPClient()
metadata_client._call = lambda *_args, **_kwargs: metadata_response
metadata_result = metadata_client.call_tool("randomize", {})
metadata_ok = (
    input_digest not in model_text and artifact_digest not in model_text
    and metadata_result.get("_private_provenance", {}).get("input_hashes")
    == {"ipd_file": input_digest}
    and metadata_result.get("_private_provenance", {}).get("artifact_hashes")
    == {"assignment.csv": artifact_digest}
    and metadata_result.get("_private_provenance", {}).get("artifact_handles")
    == {artifact_handle: artifact_path}
    and metadata_result.get("_private_provenance", {}).get("analysis_id") == analysis_id
    and not metadata_response["content"][1]["uri"].startswith("file:")
    and artifact_path not in metadata_response["content"][1]["uri"]
)
print(f"TEST private_metadata_is_recovered_outside_model_text : {'PASS' if metadata_ok else 'FAIL'}")

hostile_client = MCPClient()
hostile_client._call = lambda *_args, **_kwargs: {
    "content": [{"type": "text", "text": json.dumps({
        "_private_provenance": {"artifact_hashes": {"assignment.csv": artifact_digest}},
    })}],
}
try:
    hostile_client.call_tool("randomize", {})
except MCPToolError:
    hostile_ok = True
else:
    hostile_ok = False
print(f"TEST model_visible_private_provenance_is_rejected : {'PASS' if hostile_ok else 'FAIL'}")

sys.exit(0 if all((ok, restart_ok, budget_ok, cancel_ok, late_ok,
                   metadata_ok, hostile_ok)) else 1)
