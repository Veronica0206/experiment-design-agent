#!/usr/bin/env python3
"""Query OSV for every exact Python and CRAN lock pin, without installing code.

Only ecosystem/name/version and OSV's continuation tokens are transmitted.
No lock metadata, paths, hashes, credentials, or dependency source are sent.
API contract: https://google.github.io/osv.dev/post-v1-querybatch/
Exit 0 means no matching OSV advisories, 1 findings, 2 incomplete/failed audit.
It does not establish that a package has no undisclosed vulnerabilities.
"""

from __future__ import annotations

import argparse
import http.client
import json
import re
import ssl
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate_python_environment import (  # noqa: E402
    EnvironmentValidationError, LOCK_PIN_RE, canonical_name, parse_hash_lock,
)

HOST = "api.osv.dev"
ENDPOINT = "/v1/querybatch"
MAX_LOCK_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_PACKAGES = 2000
BATCH_SIZE = 100
MAX_PAGES = 20
REQUEST_TIMEOUT = 20
TOTAL_TIMEOUT = 180
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
VERSION = re.compile(r"[0-9][A-Za-z0-9.!+_-]{0,127}\Z")
ADVISORY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:+-]{0,199}\Z")
TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z\Z")


class AuditError(ValueError):
    """An incomplete audit must never be reported as clean."""


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AuditError("duplicate JSON keys")
        value[key] = item
    return value


def decode_json(data: bytes) -> Any:
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(AuditError("invalid JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise AuditError("invalid JSON document") from exc


def read_lock(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOCK_BYTES:
            raise AuditError("lock must be a bounded regular file")
        with path.open("rb") as handle:
            data = handle.read(MAX_LOCK_BYTES + 1)
        if len(data) > MAX_LOCK_BYTES:
            raise AuditError("lock exceeds size limit")
        return data
    except OSError as exc:
        raise AuditError("lock file could not be read") from exc


def python_pins(data: bytes) -> list[tuple[str, str, str]]:
    try:
        source = data.decode("utf-8")
        # This validates every entry, including inactive platform pins. Its
        # returned host-specific subset is deliberately not the audit inventory.
        parse_hash_lock(source)
    except (UnicodeDecodeError, EnvironmentValidationError) as exc:
        raise AuditError("Python hash lock is invalid") from exc
    pins = []
    for line in source.splitlines():
        match = LOCK_PIN_RE.fullmatch(line)
        if match:
            pins.append(("PyPI", canonical_name(match.group(1)), match.group(2)))
    return validate_pins(pins)


def cran_pins(data: bytes) -> list[tuple[str, str, str]]:
    value = decode_json(data)
    if not isinstance(value, dict) or not isinstance(value.get("Packages"), dict) or not value["Packages"]:
        raise AuditError("R lock has no declared packages")
    pins = []
    for name, package in value["Packages"].items():
        if (not isinstance(package, dict) or package.get("Package") != name
                or package.get("Source") != "Repository" or package.get("Repository") != "CRAN"):
            raise AuditError("R lock includes an unsupported or mismatched package source")
        pins.append(("CRAN", name, package.get("Version")))
    return validate_pins(pins)


def validate_pins(pins: Sequence[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    if not pins or len(pins) > MAX_PACKAGES:
        raise AuditError("package inventory is empty or exceeds its limit")
    seen = set()
    for ecosystem, name, version in pins:
        if (ecosystem not in {"PyPI", "CRAN"} or not isinstance(name, str)
                or not NAME.fullmatch(name) or not isinstance(version, str)
                or not VERSION.fullmatch(version) or (ecosystem, name) in seen):
            raise AuditError("package inventory contains an invalid or duplicate exact pin")
        seen.add((ecosystem, name))
    return sorted(pins)


def post_batch(payload: dict[str, Any], timeout: float) -> Any:
    """Fixed HTTPS host, no proxy/auth/redirect handling, bounded response body."""
    if (not isinstance(payload, dict) or set(payload) != {"queries"}
            or not isinstance(payload["queries"], list)
            or not 1 <= len(payload["queries"]) <= BATCH_SIZE):
        raise AuditError("OSV request is outside the package-coordinate contract")
    for query in payload["queries"]:
        if (not isinstance(query, dict) or set(query) - {"package", "version", "page_token"}
                or not isinstance(query.get("package"), dict)
                or set(query["package"]) != {"ecosystem", "name"}):
            raise AuditError("OSV request is outside the package-coordinate contract")
        package = query["package"]
        validate_pins([(package["ecosystem"], package["name"], query.get("version"))])
        token = query.get("page_token")
        if "page_token" in query and (not isinstance(token, str) or not 1 <= len(token) <= 4096
                                      or not token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise AuditError("OSV request pagination token is invalid")
    body = json.dumps(payload, separators=(",", ":")).encode("ascii")
    if len(body) > MAX_RESPONSE_BYTES:
        raise AuditError("OSV request exceeds its size limit")
    deadline = time.monotonic() + timeout
    connection = None
    try:
        connection = http.client.HTTPSConnection(HOST, timeout=timeout,
                                                  context=ssl.create_default_context())
        connection.request("POST", ENDPOINT, body=body, headers={
            "Content-Type": "application/json", "Accept": "application/json",
            "User-Agent": "experiment-design-advisory-audit/1",
        })
        response = connection.getresponse()
        if response.status != 200:
            raise AuditError("OSV request did not return HTTP 200")
        if response.getheader("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise AuditError("OSV response is not JSON")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdigit() or int(length) > MAX_RESPONSE_BYTES):
            raise AuditError("OSV response exceeds its size limit")
        chunks = []
        size = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuditError("OSV request exceeded its time limit")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read1(min(65536, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise AuditError("OSV response exceeds its size limit")
            chunks.append(chunk)
        if length is not None and size != int(length):
            raise AuditError("OSV response body is incomplete")
        return decode_json(b"".join(chunks))
    except (OSError, http.client.HTTPException) as exc:
        # Transport diagnostics can include host/local configuration. Keep
        # them out of logs, as well as any server-supplied error response body.
        raise AuditError("OSV transport failed; audit is incomplete") from exc
    finally:
        if connection is not None:
            connection.close()


def response_results(value: Any, count: int) -> list[tuple[set[str], str | None]]:
    if (not isinstance(value, dict) or set(value) != {"results"}
            or not isinstance(value["results"], list) or len(value["results"]) != count):
        raise AuditError("OSV response cardinality or schema is invalid")
    parsed = []
    for result in value["results"]:
        if not isinstance(result, dict) or set(result) - {"vulns", "next_page_token"}:
            raise AuditError("OSV query result has an unexpected field")
        vulnerabilities = result.get("vulns", [])
        if not isinstance(vulnerabilities, list) or len(vulnerabilities) > 10000:
            raise AuditError("OSV advisory list is invalid")
        ids = set()
        for record in vulnerabilities:
            if (not isinstance(record, dict) or set(record) != {"id", "modified"}
                    or not isinstance(record["id"], str) or not ADVISORY_ID.fullmatch(record["id"])
                    or not isinstance(record["modified"], str) or not TIMESTAMP.fullmatch(record["modified"])
                    or record["id"] in ids):
                raise AuditError("OSV advisory record is invalid")
            ids.add(record["id"])
        token = result.get("next_page_token")
        if "next_page_token" in result and (not isinstance(token, str) or len(token) > 4096
                                  or not token.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in token)):
            raise AuditError("OSV pagination token is invalid")
        parsed.append((ids, token or None))
    return parsed


def audit(pins: Sequence[tuple[str, str, str]],
          transport: Callable[[dict[str, Any], float], Any] = post_batch) -> dict[tuple[str, str, str], list[str]]:
    inventory = validate_pins(pins)
    findings: dict[tuple[str, str, str], set[str]] = {pin: set() for pin in inventory}
    deadline = time.monotonic() + TOTAL_TIMEOUT
    for offset in range(0, len(inventory), BATCH_SIZE):
        pending = [(pin, None) for pin in inventory[offset:offset+BATCH_SIZE]]
        seen_tokens = {pin: set() for pin, _ in pending}
        for _ in range(MAX_PAGES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AuditError("OSV audit exceeded its time limit")
            queries = []
            for (ecosystem, name, version), token in pending:
                query = {"package": {"ecosystem": ecosystem, "name": name}, "version": version}
                if token:
                    query["page_token"] = token
                queries.append(query)
            values = response_results(transport({"queries": queries}, min(REQUEST_TIMEOUT, remaining)), len(pending))
            if time.monotonic() > deadline:
                raise AuditError("OSV audit exceeded its time limit")
            followup = []
            for (pin, _), (ids, token) in zip(pending, values):
                findings[pin].update(ids)
                if token:
                    if token in seen_tokens[pin]:
                        raise AuditError("OSV pagination did not advance")
                    seen_tokens[pin].add(token)
                    followup.append((pin, token))
            if not followup:
                break
            pending = followup
        else:
            raise AuditError("OSV pagination exceeded its limit")
    return {pin: sorted(ids) for pin, ids in findings.items() if ids}


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-lock", type=Path, default=root / "agent-harness/requirements.lock")
    parser.add_argument("--r-lock", type=Path, default=root / "renv.lock")
    args = parser.parse_args(argv)
    try:
        pins = python_pins(read_lock(args.python_lock)) + cran_pins(read_lock(args.r_lock))
        findings = audit(pins)
    except AuditError as exc:
        print(f"Dependency advisory audit incomplete: {exc}", file=sys.stderr)
        return 2
    if findings:
        for (ecosystem, name, version), ids in findings.items():
            print(f"{ecosystem} {name}=={version}: {', '.join(ids)}")
        print(f"Dependency advisory audit: {len(findings)} affected exact pins; {len(pins)} pins checked")
        return 1
    print(f"Dependency advisory audit: no matching OSV advisories for {len(pins)} exact pins (PyPI and CRAN)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
