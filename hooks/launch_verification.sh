#!/bin/sh
# Normalize every launcher/runtime failure to Claude Code's blocking hook code.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || {
  printf '%s' 'INTERNAL_ERROR. Verification hook directory could not be resolved.' >&2
  exit 2
}

if ! command -v node >/dev/null 2>&1; then
  printf '%s' 'INTERNAL_ERROR. Node.js is unavailable for verification.' >&2
  exit 2
fi

node "$script_dir/launch_verification.mjs" "$@"
status=$?
if [ "$status" -eq 0 ]; then
  exit 0
fi
exit 2
