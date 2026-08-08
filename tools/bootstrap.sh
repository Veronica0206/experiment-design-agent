#!/usr/bin/env bash
# One-time setup for a fresh checkout: build the gitignored mcp-server/dist/
# that .mcp.json points at, and verify the R/Node toolchain.
set -euo pipefail
cd "$(dirname "$0")/.."

MIN_CLAUDE_CODE_VERSION="2.1.197"
HARNESS_ONLY=false
case "${1:-}" in
  "") ;;
  --harness-only) HARNESS_ONLY=true ;;
  *) echo "Usage: tools/bootstrap.sh [--harness-only]"; exit 2 ;;
esac

echo "== toolchain =="
command -v Rscript >/dev/null || { echo "ERROR: Rscript not on PATH"; exit 1; }
command -v node >/dev/null    || { echo "ERROR: node not on PATH"; exit 1; }
command -v npm >/dev/null     || { echo "ERROR: npm not on PATH"; exit 1; }
echo "Rscript: $(Rscript --version 2>&1 | head -1)"
echo "node:    $(node --version)"
node -e 'if (Number(process.versions.node.split(".")[0]) < 20) process.exit(1)' || {
  echo "ERROR: Node.js >= 20 is required"
  exit 1
}
if [[ "$HARNESS_ONLY" == false ]]; then
  command -v claude >/dev/null || { echo "ERROR: Claude Code not on PATH"; exit 1; }
  claude_raw="$(claude --version 2>&1 | head -1)"
  claude_version="$(node -e '
const match = process.argv[1].match(/(?:^|[^0-9])([0-9]+\.[0-9]+\.[0-9]+)(?:[^0-9]|$)/);
if (match) process.stdout.write(match[1]);
' "$claude_raw")"
  if [[ -z "$claude_version" ]] || ! node -e '
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
  echo "claude:  $claude_raw"
else
  echo "claude:  skipped (--harness-only)"
fi

echo "== R packages =="
Rscript --vanilla - <<'RS'
req <- c("jsonlite", "mvtnorm", "survival")
opt <- c("Exact", "MAMS")
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

echo "== MCP server build =="
(cd mcp-server && npm ci --no-audit --no-fund && npm run build)
test -f mcp-server/dist/index.js || { echo "ERROR: dist/index.js missing after build"; exit 1; }

echo "== test matrix =="
make test

echo "Bootstrap complete. Open this folder in Claude Code (uses .mcp.json), or run:"
echo "  streamlit run agent-harness/streamlit_app.py"
