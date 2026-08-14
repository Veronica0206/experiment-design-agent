#!/usr/bin/env python3
"""Claude Code entrypoint for coordinator prompt capture and dispatch binding."""

from __future__ import annotations

import json
import sys

from prompt_binding import authorize_agent_dispatch, capture_prompt


def main() -> int:
    try:
        data = json.load(sys.stdin)
        if not isinstance(data, dict):
            raise ValueError("hook input must be a JSON object")
        event = data.get("hook_event_name")
        if event == "UserPromptSubmit":
            capture_prompt(data)
        elif event == "PreToolUse":
            authorize_agent_dispatch(data)
        else:
            raise ValueError("unsupported prompt-binding hook event")
    except Exception as exc:
        sys.stderr.write(f"INTERNAL_ERROR. Coordinator prompt binding failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
