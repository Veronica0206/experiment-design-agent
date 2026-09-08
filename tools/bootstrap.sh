#!/usr/bin/env bash
# Setup for an authorized complete checkout: build the gitignored
# mcp-server/dist/ that .mcp.json points at, and verify the pinned Python, R,
# and Node runtimes. The public portfolio distribution intentionally omits the
# other statistical engines; its single-endpoint profile runs `make public-check`.
set -euo pipefail
cd "$(dirname "$0")/.."

MIN_CLAUDE_CODE_VERSION="2.1.233"
HARNESS_ONLY=false
CLAUDE_VERSION_CHECK_ONLY=false
CLAUDE_LIVE_CHECK_ONLY=false
PYTHON_LOCK_CHECK_ONLY=false
case "${1:-}" in
  "") ;;
  --harness-only) HARNESS_ONLY=true ;;
  --check-claude-version) CLAUDE_VERSION_CHECK_ONLY=true ;;
  --check-claude-live) CLAUDE_LIVE_CHECK_ONLY=true ;;
  --check-python-lock) PYTHON_LOCK_CHECK_ONLY=true ;;
  *) echo "Usage: tools/bootstrap.sh [--harness-only|--check-claude-version|--check-claude-live|--check-python-lock]"; exit 2 ;;
esac

echo "== toolchain =="
select_reviewed_node() {
  if [[ -n "${EXPDESIGN_NODE:-}" ]]; then
    if [[ "$EXPDESIGN_NODE" != /* ]]; then
      echo "ERROR: EXPDESIGN_NODE must be an absolute path"
      exit 1
    fi
    REVIEWED_NODE="$EXPDESIGN_NODE"
  else
    REVIEWED_NODE=""
    for candidate in \
      /opt/homebrew/bin/node \
      /usr/local/bin/node \
      /usr/bin/node \
      /opt/local/bin/node
    do
      if [[ -f "$candidate" && -x "$candidate" ]]; then
        REVIEWED_NODE="$candidate"
        break
      fi
    done
  fi
  if [[ ! -f "$REVIEWED_NODE" || ! -x "$REVIEWED_NODE" ]]; then
    echo "ERROR: no approved absolute Node.js executable is available"
    exit 1
  fi
}

select_harness_python() {
  if [[ -n "${EXPDESIGN_PYTHON:-}" ]]; then
    if [[ "$EXPDESIGN_PYTHON" != /* ]]; then
      echo "ERROR: EXPDESIGN_PYTHON must be an absolute path"
      exit 1
    fi
    HARNESS_PYTHON="$EXPDESIGN_PYTHON"
  elif [[ -x "agent-harness/.venv/bin/python" ]]; then
    HARNESS_PYTHON="$PWD/agent-harness/.venv/bin/python"
  else
    HARNESS_PYTHON=""
    for candidate in \
      /opt/homebrew/bin/python3 \
      /usr/local/bin/python3 \
      /usr/bin/python3 \
      /opt/local/bin/python3
    do
      if [[ -f "$candidate" && -x "$candidate" ]]; then
        HARNESS_PYTHON="$candidate"
        break
      fi
    done
    if [[ -z "$HARNESS_PYTHON" ]]; then
      echo "ERROR: Python 3.10+ is required for the harness"
      exit 1
    fi
  fi
  if [[ ! -x "$HARNESS_PYTHON" ]]; then
    echo "ERROR: harness Python is not executable: $HARNESS_PYTHON"
    exit 1
  fi

  if ! "$HARNESS_PYTHON" -E -s -S -B -I -c '
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
'; then
    echo "ERROR: Python 3.10+ is required for the harness"
    exit 1
  fi
  echo "python:  $($HARNESS_PYTHON -E -s -S -B -I --version 2>&1) ($HARNESS_PYTHON)"
}

check_harness_dependencies() {
  print_harness_setup_recipe() {
    echo "Create a clean locked harness environment with:"
    printf '  %q -E -s -S -B -I -m venv agent-harness/.venv\n' \
      "$HARNESS_PYTHON"
    echo "  agent-harness/.venv/bin/python -E -s -B -I -m pip install --no-compile --require-hashes -r agent-harness/requirements.lock"
    echo "Then rerun tools/bootstrap.sh. Set EXPDESIGN_PYTHON only to use another reviewed environment."
  }

  # A locked wheel may ship RECORD-declared bytecode. Normalize only the
  # selected venv package roots under the same pre-site interpreter flags that
  # protect the reviewed runner, then enforce the complete environment policy.
  if ! "$HARNESS_PYTHON" -E -s -S -B -I \
    tools/sanitize_python_environment.py
  then
    echo "ERROR: the pinned Python harness environment could not be sanitized safely."
    print_harness_setup_recipe
    exit 1
  fi
  if ! EXPDESIGN_PYTHON="$HARNESS_PYTHON" \
    tools/run-reviewed-python.sh --validate-only
  then
    echo "ERROR: the pinned Python harness environment is not ready."
    print_harness_setup_recipe
    exit 1
  fi
  echo "Python harness dependencies OK"
}

select_reviewed_claude() {
  if [[ -n "${EXPDESIGN_CLAUDE:-}" ]]; then
    if [[ "$EXPDESIGN_CLAUDE" != /* ]]; then
      echo "ERROR: EXPDESIGN_CLAUDE must be an absolute path"
      exit 1
    fi
    REVIEWED_CLAUDE="$EXPDESIGN_CLAUDE"
  else
    REVIEWED_CLAUDE=""
    for candidate in \
      "${HOME:-}/.local/bin/claude" \
      /opt/homebrew/bin/claude \
      /usr/local/bin/claude \
      /usr/bin/claude \
      /opt/local/bin/claude
    do
      if [[ "$candidate" == /* && -f "$candidate" && -x "$candidate" ]]; then
        REVIEWED_CLAUDE="$candidate"
        break
      fi
    done
  fi
  if [[ ! -f "$REVIEWED_CLAUDE" || ! -x "$REVIEWED_CLAUDE" ]]; then
    echo "ERROR: no approved absolute Claude Code executable is available"
    exit 1
  fi
}

check_claude_version() {
  select_reviewed_claude
  if ! claude_raw="$("$REVIEWED_CLAUDE" --version 2>&1)"; then
    echo "ERROR: Claude Code version probe failed"
    exit 1
  fi
  claude_version="$("$REVIEWED_NODE" -e '
const match = process.argv[1].match(/^([0-9]+\.[0-9]+\.[0-9]+) \(Claude Code\)$/);
if (match) process.stdout.write(match[1]);
' "$claude_raw")"
  if [[ -z "$claude_version" ]]; then
    echo "ERROR: unrecognized Claude Code product/version output (found: $claude_raw)"
    exit 1
  fi
  if ! "$REVIEWED_NODE" -e '
const [actual, minimum] = process.argv.slice(1);
const parts = (value) => value.split(".").map(Number);
const [a, b] = [parts(actual), parts(minimum)];
for (let i = 0; i < 3; i += 1) {
  if (a[i] > b[i]) process.exit(0);
  if (a[i] < b[i]) process.exit(1);
}
' "$claude_version" "$MIN_CLAUDE_CODE_VERSION"; then
    echo "ERROR: Claude Code >= $MIN_CLAUDE_CODE_VERSION is required (found: $claude_raw)"
    exit 1
  fi
  echo "claude:  $claude_raw ($REVIEWED_CLAUDE)"
  # Check the reviewed startup configuration, without authenticating or
  # dispatching a paid model request. The capture hook checks the effective
  # environment in the running Claude process as well.
  "$REVIEWED_NODE" -e '
const fs = require("node:fs");
const settings = JSON.parse(fs.readFileSync(".claude/settings.json", "utf8"));
if (settings.env?.CLAUDE_CODE_FORK_SUBAGENT !== "0"
    || (process.env.CLAUDE_CODE_FORK_SUBAGENT !== undefined
        && process.env.CLAUDE_CODE_FORK_SUBAGENT !== "0")) {
  console.error("ERROR: native coordinator requires CLAUDE_CODE_FORK_SUBAGENT=0 in project settings and no conflicting environment override");
  process.exit(1);
}
const disabled = (value) => ["1", "true", "yes", "on"].includes(String(value ?? "").trim().toLowerCase());
if (disabled(settings.env?.CLAUDE_CODE_DISABLE_BACKGROUND_TASKS)
    || disabled(process.env.CLAUDE_CODE_DISABLE_BACKGROUND_TASKS)) {
  console.error("ERROR: native coordinator explicit foreground contract is incompatible with CLAUDE_CODE_DISABLE_BACKGROUND_TASKS; unset it or set it false before starting Claude");
  process.exit(1);
}
'
  echo "Claude foreground startup configuration check passed"
}

check_claude_live() {
  check_claude_version
  if ! "$REVIEWED_CLAUDE" auth status >/dev/null 2>&1; then
    echo "ERROR: Claude Code is installed but has no authenticated live session"
    exit 1
  fi
  echo "Claude Code live availability check passed (installed and authenticated)"
}

if [[ "$PYTHON_LOCK_CHECK_ONLY" == true ]]; then
  select_harness_python
  check_harness_dependencies
  echo "Python requirements.lock check passed"
  exit 0
fi

select_reviewed_node
echo "node:    $($REVIEWED_NODE --version) ($REVIEWED_NODE)"
"$REVIEWED_NODE" -e 'if (Number(process.versions.node.split(".")[0]) < 20) process.exit(1)' || {
  echo "ERROR: Node.js >= 20 is required"
  exit 1
}

if [[ "$CLAUDE_VERSION_CHECK_ONLY" == true ]]; then
  check_claude_version
  echo "Claude Code version check passed (authentication was not required)"
  exit 0
fi

if [[ "$CLAUDE_LIVE_CHECK_ONLY" == true ]]; then
  check_claude_live
  exit 0
fi

select_harness_python
check_harness_dependencies
command -v npm >/dev/null     || { echo "ERROR: npm not on PATH"; exit 1; }
echo "Rscript: $(tools/run-reviewed-r.sh --version 2>&1 | head -1)"
if [[ "$HARNESS_ONLY" == false ]]; then
  check_claude_version
else
  echo "claude:  skipped (--harness-only)"
fi

RUNTIME_PROFILE=$(tools/run-reviewed-r.sh -e 'source("mcp-server/r-wrapper/runtime-profile.R"); cat(load_runtime_profile(".")$name)')
echo "== R packages ($RUNTIME_PROFILE) =="
tools/run-reviewed-r.sh - <<'RS'
source("mcp-server/r-wrapper/runtime-profile.R")
profile <- load_runtime_profile(".")
req <- setdiff(profile$r_packages, c("Exact", "MAMS"))
opt <- intersect(profile$r_packages, c("Exact", "MAMS"))
missing <- req[!vapply(req, requireNamespace, TRUE, quietly = TRUE)]
if (length(missing)) {
  stop("required R package(s) missing: ", paste(missing, collapse = ", "),
       ' — run install.packages("', paste(missing, collapse = '","'), '")')
}
for (p in opt) {
  if (!requireNamespace(p, quietly = TRUE)) {
    note <- switch(p,
      Exact = "Barnard sensitivity analysis is unavailable; the documented fallback is used",
      MAMS = "umbrella MAMS uses its explicitly disclosed approximation boundary",
      "optional functionality is unavailable")
    cat("optional package not installed:", p, "-", note, "\n")
  }
}
cat("R packages OK\n")
RS
if [[ "$RUNTIME_PROFILE" == complete ]]; then
  EXPDESIGN_PYTHON="$HARNESS_PYTHON" \
    tools/run-reviewed-python.sh tools/validate_r_environment.py
else
  echo "Public edition: installed R/package bytes are recorded and checked by runtime provenance."
  echo "The complete installation's platform-specific R release attestation is a separate gate."
fi

echo "== MCP server build =="
(cd mcp-server && npm ci --no-audit --no-fund && npm run build)
test -f mcp-server/dist/index.js || { echo "ERROR: dist/index.js missing after build"; exit 1; }

echo "== test matrix =="
if [[ "$RUNTIME_PROFILE" == single-endpoint ]]; then
  make PYTHON="$HARNESS_PYTHON" public-check
else
  make PYTHON="$HARNESS_PYTHON" test
fi

echo "Bootstrap complete. Open this folder in Claude Code (uses .mcp.json), or run:"
echo "  EXPDESIGN_PYTHON=$HARNESS_PYTHON tools/run-reviewed-python.sh -m streamlit run agent-harness/streamlit_app.py --server.headless true --server.address 127.0.0.1 --server.port 8501"
