#!/usr/bin/env python3
"""Claude Code PostToolBatch entrypoint for the synchronous ledger."""

from __future__ import annotations

import json
import sys

from verification_ledger import record_batch


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if data.get("_expdesign_agent_scope") not in {"experiment-designer", "design-verifier"}:
            raise ValueError("a governed agent scope is required")
        record_batch(data)
        return 0
    except Exception as exc:
        sys.stderr.write(f"INTERNAL_ERROR. Verification ledger update failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
