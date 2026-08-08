#!/usr/bin/env bash
# Publish the current state of this suite to the standalone private GitHub repo.
#
# Why a sync script: this folder lives inside the larger 00_AIWorkingFolder git
# repo (which has no remote and holds unrelated private material), so it cannot
# simply have its own remote without entangling the two. Instead we mirror the
# content into a separate clone and push from there.
#
# Usage:
#   tools/sync-to-github.sh                 # sync + show what changed, then confirm
#   tools/sync-to-github.sh -m "message"    # use a specific commit message
#   tools/sync-to-github.sh --dry-run       # show what would change, push nothing
set -euo pipefail

REPO="${EXPDESIGN_REPO:-Veronica0206/experiment-design-agent}"
CLONE="${EXPDESIGN_CLONE:-$HOME/.cache/experiment-design-agent}"
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PUBLISH_SRC="${EXPDESIGN_PUBLISH_SRC:-$SRC}"

MSG=""
DRY=0
while [ $# -gt 0 ]; do
  case "$1" in
    -m) MSG="${2:-}"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

# The working source contains proprietary plaintext skills and is never a
# publishable tree. A release must point EXPDESIGN_PUBLISH_SRC at a separately
# prepared, authorized distribution tree (for example encrypted artifacts plus
# non-proprietary runtime files) and provide the separately reviewed manifest
# hash as an operator pin in
# EXPDESIGN_PUBLISH_MANIFEST_SHA256. This guard runs before authentication/network.
python3 "$SRC/tools/check_publish_source.py" "$PUBLISH_SRC"

command -v gh >/dev/null || { echo "ERROR: gh CLI not installed"; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "ERROR: gh not authenticated (run: gh auth login)"; exit 1; }

# Never publish a broken suite.
echo "== running the test matrix before publishing =="
make -C "$SRC" release-check

if [ ! -d "$CLONE/.git" ]; then
  echo "== cloning $REPO -> $CLONE =="
  mkdir -p "$(dirname "$CLONE")"
  gh repo clone "$REPO" "$CLONE" -- -q
else
  echo "== updating existing clone =="
  git -C "$CLONE" fetch -q origin
  git -C "$CLONE" checkout -q main
  git -C "$CLONE" reset -q --hard origin/main
fi

echo "== mirroring content =="
# --delete so files removed locally are removed upstream. Excludes mirror
# .gitignore; .git/ is protected so the clone's history survives.
rsync -a --delete \
  --exclude='.git/' \
  --exclude='node_modules/' \
  --exclude='dist/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  --exclude='*.tsbuildinfo' \
  --exclude='.DS_Store' \
  --exclude='agent-harness/runs/' \
  --exclude='.env' --exclude='.env.*' \
  "$PUBLISH_SRC/" "$CLONE/"

# Re-check the exact bytes that are about to be staged. This catches rsync
# exclusions, stale clone files, symlinks, and any post-approval mutation.
python3 "$SRC/tools/check_publish_source.py" "$CLONE"

cd "$CLONE"
if git diff --quiet && git diff --cached --quiet && [ -z "$(git status --porcelain)" ]; then
  echo "== no changes to publish =="
  exit 0
fi

git add -A
python3 "$SRC/tools/check_publish_source.py" --staged "$CLONE"
echo "== changes to publish =="
git diff --cached --stat

# Refuse to publish anything that looks like a credential. Scan the staged blob
# bytes directly: textual diffs contain no content for binary files.
set +e
python3 "$SRC/tools/check_diff_credentials.py" --staged "$CLONE"
scan_status=$?
set -e
if [ "$scan_status" -eq 1 ]; then
  echo "ABORT: possible credential in the diff — inspect before publishing." >&2
  exit 1
fi
if [ "$scan_status" -ne 0 ]; then
  echo "ABORT: credential scanner failed; nothing will be published." >&2
  exit 1
fi

if [ "$DRY" -eq 1 ]; then
  echo "== dry run: nothing committed or pushed =="
  exit 0
fi

git commit -q -m "${MSG:-Sync from working tree}"
git push -q origin main
echo "== pushed to https://github.com/$REPO =="
