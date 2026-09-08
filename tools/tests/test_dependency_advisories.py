#!/usr/bin/env python3
"""Offline contract tests: fixtures only, never connect to an advisory service."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import audit_dependency_advisories as audit

PASS = FAIL = 0
HASH = "a" * 64
PYTHON_LOCK = (f'public-one==1.2.3 \\\n    --hash=sha256:{HASH}\n'
               f'watchdog==6.0.0 ; platform_system != "Darwin" \\\n    --hash=sha256:{HASH}\n').encode()
R_LOCK = json.dumps({"R": {"Version": "4.5.3"}, "Packages": {
    "publicR": {"Package": "publicR", "Version": "1.2-3", "Source": "Repository",
                "Repository": "CRAN", "Maintainer": "PRIVATE_MARKER", "Description": "PRIVATE_MARKER"},
}}).encode()
PIN = ("PyPI", "public-one", "1.2.3")
OTHER_PIN = ("CRAN", "publicR", "1.2-3")


def test(name, function):
    global PASS, FAIL
    try:
        function()
        PASS += 1
        print(f"TEST {name} : PASS")
    except Exception as exc:
        FAIL += 1
        print(f"TEST {name} : FAIL {exc}")


def require(condition):
    if not condition:
        raise AssertionError("condition was false")


def rejects(function):
    try:
        function()
    except audit.AuditError:
        return
    raise AssertionError("incomplete audit was accepted")


def vuln(name="GHSA-aaaa-bbbb-cccc"):
    return {"id": name, "modified": "2026-09-08T01:02:03Z"}


def marker_inventory():
    with patch("validate_python_environment.platform.system", return_value="Darwin"):
        pins = audit.python_pins(PYTHON_LOCK)
    require(("PyPI", "watchdog", "6.0.0") in pins)
    require(len(pins) == 2)


test("advisory_all_platform_pins_are_queried", marker_inventory)
test("advisory_r_metadata_is_projected_to_pins", lambda: require(audit.cran_pins(R_LOCK) == [OTHER_PIN]))
test("advisory_rejects_unhashed_python_lock", lambda: rejects(lambda: audit.python_pins(b"x==1.0\n")))
test("advisory_rejects_duplicate_python_pin", lambda: rejects(lambda: audit.python_pins(PYTHON_LOCK + PYTHON_LOCK)))
test("advisory_rejects_python_direct_url", lambda: rejects(lambda: audit.python_pins(PYTHON_LOCK.replace(b"1.2.3",b"https://private.example"))))
test("advisory_rejects_r_non_cran_source", lambda: rejects(lambda: audit.cran_pins(R_LOCK.replace(b'"Repository": "CRAN"',b'"Repository": "PRIVATE_MARKER"'))))
test("advisory_rejects_r_name_mismatch", lambda: rejects(lambda: audit.cran_pins(R_LOCK.replace(b'"Package": "publicR"',b'"Package": "different"'))))
test("advisory_rejects_duplicate_json_keys", lambda: rejects(lambda: audit.decode_json(b'{"results": [], "results": []}')))
test("advisory_rejects_nonfinite_json", lambda: rejects(lambda: audit.decode_json(b'{"results": NaN}')))
test("advisory_rejects_empty_inventory", lambda: rejects(lambda: audit.audit([],lambda p,t: {})))


def clean_projection():
    calls = []
    def transport(payload, timeout):
        calls.append(payload)
        require(0 < timeout <= audit.REQUEST_TIMEOUT)
        return {"results": [{} for _ in payload["queries"]]}
    pins = audit.python_pins(PYTHON_LOCK) + audit.cran_pins(R_LOCK)
    require(audit.audit(pins, transport) == {})
    raw = json.dumps(calls)
    require("PRIVATE_MARKER" not in raw and HASH not in raw and "platform_system" not in raw)
    for query in calls[0]["queries"]:
        require(set(query) == {"package", "version"})
        require(set(query["package"]) == {"ecosystem", "name"})


test("advisory_sends_only_public_package_coordinates", clean_projection)


def pagination():
    calls = []
    def transport(payload, timeout):
        calls.append(payload)
        if len(calls) == 1:
            # Inventory sorts CRAN before PyPI, only PyPI continues.
            return {"results": [{}, {"vulns": [vuln()], "next_page_token": "page-two"}]}
        require(payload == {"queries": [{"package": {"ecosystem": "PyPI", "name": "public-one"},
                                        "version": "1.2.3", "page_token": "page-two"}]})
        return {"results": [{"vulns": [vuln("PYSEC-2026-1")]}]}
    require(audit.audit([PIN,OTHER_PIN],transport) == {PIN: ["GHSA-aaaa-bbbb-cccc","PYSEC-2026-1"]})
    require(len(calls) == 2)


test("advisory_pagination_preserves_exact_pin_identity", pagination)
def batched_inventory():
    pins = [("PyPI", f"public-{i}", "1.0") for i in range(201)]
    sizes = []
    def transport(payload, timeout):
        sizes.append(len(payload["queries"]))
        return {"results": [{} for _ in payload["queries"]]}
    require(audit.audit(pins, transport) == {} and sizes == [100,100,1])
test("advisory_batches_do_not_omit_pins", batched_inventory)
test("advisory_pagination_without_initial_hits_is_not_clean", lambda: rejects(lambda: audit.audit([PIN],lambda p,t: {"results":[{"next_page_token":"repeat"}]})))
with patch.object(audit,"MAX_PAGES",2):
    count = [0]
    def forever(payload, timeout):
        count[0] += 1
        return {"results":[{"next_page_token":f"page-{count[0]}"}]}
    test("advisory_pagination_limit_is_incomplete",lambda: rejects(lambda: audit.audit([PIN],forever)))

for label, response in {
    "missing_results": {},
    "wrong_cardinality": {"results":[]},
    "query_error": {"results":[{"error":"PRIVATE_MARKER"}]},
    "null_vulns": {"results":[{"vulns":None}]},
    "bad_id": {"results":[{"vulns":[vuln("GHSA-x\nPRIVATE_MARKER")]}]},
    "missing_modified": {"results":[{"vulns":[{"id":"GHSA-good"}]}]},
    "full_metadata": {"results":[{"vulns":[{**vuln(),"details":"PRIVATE_MARKER"}]}]},
    "null_token": {"results":[{"next_page_token":None}]},
    "long_token": {"results":[{"next_page_token":"x"*4097}]},
    "duplicate_ids": {"results":[{"vulns":[vuln(),vuln()]}]},
}.items():
    test(f"advisory_rejects_{label}",lambda response=response: rejects(lambda: audit.audit([PIN],lambda p,t:response)))


class FakeResponse:
    status = 200
    def __init__(self, body=b'{"results":[{}]}', headers=None):
        self.body = body
        self.headers = {"Content-Type":"application/json", "Content-Length":str(len(body))}
        self.headers.update(headers or {})
    def getheader(self,name,default=None):
        return self.headers.get(name,default)
    def read1(self,count):
        result,self.body = self.body[:count],self.body[count:]
        return result


class FakeConnection:
    response = None
    requests = []
    closed = False
    sock = None
    def __init__(self, host, **kwargs):
        require(host == "api.osv.dev")
        require(kwargs["timeout"] > 0 and kwargs["context"] is not None)
    def request(self,method,path,body,headers):
        self.requests.append((method,path,body,headers))
    def getresponse(self):
        return self.response
    def close(self):
        FakeConnection.closed = True


def fake_http(response, expect_failure=False):
    FakeConnection.response = response
    FakeConnection.requests = []
    FakeConnection.closed = False
    with patch.object(audit.http.client,"HTTPSConnection",FakeConnection):
        run = lambda: audit.post_batch({"queries":[{"package":{"ecosystem":"PyPI","name":"public-one"},"version":"1.2.3"}]},20)
        if expect_failure:
            rejects(run)
        else:
            require(run() == {"results":[{}]})
    method,path,body,headers = FakeConnection.requests[0]
    require(method == "POST" and path == "/v1/querybatch")
    require(set(headers) == {"Content-Type","Accept","User-Agent"})
    require(FakeConnection.closed)


test("advisory_fixed_https_endpoint_has_no_credentials",lambda:fake_http(FakeResponse()))
redirect = FakeResponse(); redirect.status = 302
test("advisory_redirect_is_rejected",lambda:fake_http(redirect,True))
test("advisory_wrong_content_type_is_rejected",lambda:fake_http(FakeResponse(headers={"Content-Type":"text/html"}),True))
test("advisory_truncated_body_is_rejected",lambda:fake_http(FakeResponse(headers={"Content-Length":"9999"}),True))
test("advisory_oversized_body_is_rejected",lambda:fake_http(FakeResponse(headers={"Content-Length":str(audit.MAX_RESPONSE_BYTES+1)}),True))
test("advisory_oversized_unframed_body_is_rejected",lambda:fake_http(FakeResponse(b"x"*(audit.MAX_RESPONSE_BYTES+1),headers={"Content-Length":None}),True))
test("advisory_malformed_response_is_rejected",lambda:fake_http(FakeResponse(b'PRIVATE_MARKER'),True))
test("advisory_outbound_extra_metadata_is_rejected",lambda:rejects(lambda:audit.post_batch({"queries":[{"package":{"ecosystem":"PyPI","name":"public-one"},"version":"1.2.3","metadata":"PRIVATE_MARKER"}]},20)))


def exit_codes():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        py = root/"python.lock"; py.write_bytes(PYTHON_LOCK)
        r = root/"r.lock"; r.write_bytes(R_LOCK)
        args = ["--python-lock",str(py),"--r-lock",str(r)]
        for result,expected in (({},0),({PIN:["GHSA-a"]},1)):
            output = io.StringIO()
            with patch.object(audit,"audit",return_value=result), contextlib.redirect_stdout(output):
                require(audit.main(args) == expected)
            require("PRIVATE_MARKER" not in output.getvalue() and str(root) not in output.getvalue())
        output = io.StringIO(); errors = io.StringIO()
        with patch.object(audit,"audit",side_effect=audit.AuditError("OSV transport failed; audit is incomplete")), contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
            require(audit.main(args) == 2)
        require(not output.getvalue() and "incomplete" in errors.getvalue())
        link = root/"linked.lock"; link.symlink_to(py)
        rejects(lambda:audit.read_lock(link))


test("advisory_exit_codes_and_no_false_clean",exit_codes)


def transport_error():
    with patch.object(audit.http.client,"HTTPSConnection",side_effect=OSError("PRIVATE_MARKER")):
        # Construction is inside the transport error boundary as well.
        rejects(lambda:audit.post_batch({"queries":[{"package":{"ecosystem":"PyPI","name":"public-one"},"version":"1.2.3"}]},20))


test("advisory_transport_failure_is_sanitized",transport_error)
def total_deadline():
    with patch.object(audit.time,"monotonic",side_effect=[0,0,181]):
        rejects(lambda:audit.audit([PIN],lambda p,t:{"results":[{}]}))
test("advisory_late_clean_response_is_incomplete",total_deadline)
print(f"Dependency advisory tests: {PASS} passed; {FAIL} failed")
sys.exit(1 if FAIL else 0)
