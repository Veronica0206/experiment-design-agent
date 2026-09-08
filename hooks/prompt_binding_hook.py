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
            prior_count = capture_prompt(data)
            if prior_count:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": (
                        f"This unresolved request has {prior_count} prior user message(s). "
                        "For Agent.prompt send a JSON object with type clarified_user_request, "
                        "prior_user_messages containing those exact user messages in order, "
                        "and current_user_message containing the exact latest user message. "
                        "Include no assistant/tool text. The host checks every message and "
                        "normalizes the JSON envelope before execution."
                    ),
                }}))
        elif event == "PreToolUse":
            updated_input = authorize_agent_dispatch(data)
            if updated_input != data.get("tool_input"):
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": event, "updatedInput": updated_input,
                }}))
        else:
            raise ValueError("unsupported prompt-binding hook event")
    except Exception as exc:
        sys.stderr.write(f"INTERNAL_ERROR. Coordinator prompt binding failed: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
