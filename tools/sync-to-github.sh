#!/bin/sh
# Publish an authorized distribution to the pinned public portfolio repository.
set -eu

EXPECTED_OWNER=Veronica0206
EXPECTED_REPO=Veronica0206/experiment-design-agent
EXPECTED_CLONE_URL=https://github.com/Veronica0206/experiment-design-agent.git
EXPECTED_BRANCH=main
PUBLIC_AUTHOR_NAME='VERA Public Release'
PUBLIC_AUTHOR_EMAIL=Veronica0206@users.noreply.github.com
PUBLIC_COMMIT_MESSAGE='Sync reviewed public portfolio distribution'
SAFE_PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:/opt/local/bin

abort() {
  printf 'ABORT: %s\n' "$1" >&2
  exit 1
}

case "$0" in
  /*) script_path=$0 ;;
  */*) script_path=$(/bin/pwd -P)/$0 ;;
  *) abort 'publication script must be invoked with a path' ;;
esac
script_parent=${script_path%/*}
SRC=$(CDPATH= cd -P -- "$script_parent/.." 2>/dev/null && /bin/pwd -P) || \
  abort 'could not resolve the suite root'

usage() {
  printf '%s\n' \
    'Usage: tools/sync-to-github.sh --dry-run' \
    '       tools/sync-to-github.sh --publish-reviewed' >&2
  exit 2
}
[ "$#" -eq 1 ] || usage
case "$1" in
  --dry-run) DRY=1 ;;
  --publish-reviewed) DRY=0 ;;
  *) usage ;;
esac

PYTHON_RUN=$SRC/tools/run-publication-python.sh
PUBLISH_GUARD=$SRC/tools/check_publish_source.py
PUBLIC_GUARD=$SRC/tools/validate_public_distribution.py
CREDENTIAL_GUARD=$SRC/tools/check_diff_credentials.py
GIT_BIN=/usr/bin/git
MAKE_BIN=/usr/bin/make
RSYNC_BIN=/usr/bin/rsync
MKTEMP_BIN=/usr/bin/mktemp
RM_BIN=/bin/rm
MKDIR_BIN=/bin/mkdir

for required in \
  "$PYTHON_RUN" "$PUBLISH_GUARD" "$PUBLIC_GUARD" "$CREDENTIAL_GUARD" \
  "$GIT_BIN" "$MAKE_BIN" "$RSYNC_BIN" "$MKTEMP_BIN" "$RM_BIN" "$MKDIR_BIN"
do
  [ -f "$required" ] && [ -x "$required" ] || \
    abort "required reviewed executable is unavailable: $required"
done

OWNER_HOME=$("$PYTHON_RUN" "$PUBLIC_GUARD" --owner-home) || \
  abort 'could not derive the operating-system account home'
[ "${HOME-}" = "$OWNER_HOME" ] || \
  abort 'HOME differs from the canonical operating-system account home'

GH_BIN=
for candidate in /opt/homebrew/bin/gh /usr/local/bin/gh /usr/bin/gh /opt/local/bin/gh
do
  if [ -f "$candidate" ] && [ -x "$candidate" ]; then
    GH_BIN=$candidate
    break
  fi
done
[ -n "$GH_BIN" ] || abort 'no approved absolute GitHub CLI is available'

# Reusable destination/clone overrides are forbidden. This publisher has one
# immutable public destination and creates a fresh clone for each invocation.
if [ "${EXPDESIGN_REPO+x}" = x ] || [ "${EXPDESIGN_CLONE+x}" = x ]; then
  abort 'repository and clone overrides are forbidden for public publication'
fi

# Git redirection, object substitution, credential, tracing, and config
# injection variables must be rejected, not merely hidden. GIT_PAGER is benign
# and is ignored by every sanitized Git child below.
git_redirect_names=$(
  /usr/bin/env | /usr/bin/sed -n \
    -e 's/^\(GIT_CONFIG_[A-Za-z0-9_]*\)=.*/\1/p' \
    -e 's/^\(GIT_DIR\|GIT_WORK_TREE\|GIT_INDEX_FILE\|GIT_OBJECT_DIRECTORY\|GIT_ALTERNATE_OBJECT_DIRECTORIES\|GIT_COMMON_DIR\|GIT_NAMESPACE\|GIT_REPLACE_REF_BASE\|GIT_SHALLOW_FILE\|GIT_SSH\|GIT_SSH_COMMAND\|GIT_PROXY_COMMAND\|GIT_ASKPASS\|GIT_TERMINAL_PROMPT\|GIT_EXEC_PATH\|GIT_TEMPLATE_DIR\|GIT_CEILING_DIRECTORIES\|GIT_DISCOVERY_ACROSS_FILESYSTEM\)=.*/\1/p' \
    -e 's/^\(GIT_TRACE[A-Za-z0-9_]*\)=.*/\1/p'
)
[ -z "$git_redirect_names" ] || \
  abort "Git environment redirection is forbidden: $git_redirect_names"
if [ "${GH_HOST+x}" = x ] || [ "${GH_REPO+x}" = x ] || \
   [ "${GH_CONFIG_DIR+x}" = x ] || [ "${GITHUB_HOST+x}" = x ]; then
  abort 'GitHub host, repository, and config-directory overrides are forbidden'
fi

PUBLISH_SRC=${EXPDESIGN_PUBLISH_SRC-}
[ -n "$PUBLISH_SRC" ] || \
  abort 'EXPDESIGN_PUBLISH_SRC must name the separately authorized public tree'
case "$PUBLISH_SRC" in
  /*) ;;
  *) abort 'EXPDESIGN_PUBLISH_SRC must be an absolute path' ;;
esac
[ -d "$PUBLISH_SRC" ] && [ ! -L "$PUBLISH_SRC" ] || \
  abort 'the authorized public tree must be a real directory'
[ -n "${EXPDESIGN_PUBLISH_MANIFEST_SHA256-}" ] || \
  abort 'the separately reviewed publish-manifest SHA-256 pin is required'

ambient_gh_token=${GH_TOKEN-}
ambient_github_token=${GITHUB_TOKEN-}
publication_token=
GH_ISOLATED_CONFIG=
run_gh() {
  /usr/bin/env -i HOME=/var/empty PATH="$SAFE_PATH" LANG=C LC_ALL=C \
    GH_CONFIG_DIR="$GH_ISOLATED_CONFIG" GH_TOKEN="$publication_token" \
    "$GH_BIN" "$@"
}

run_git() {
  /usr/bin/env -i HOME=/var/empty PATH="$SAFE_PATH" LANG=C LC_ALL=C \
    GH_CONFIG_DIR="$GH_ISOLATED_CONFIG" GH_TOKEN="$publication_token" \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 "$GIT_BIN" \
    -c core.hooksPath=/dev/null -c credential.helper= \
    -c "credential.https://github.com.helper=!$GH_BIN auth git-credential" "$@"
}

run_git_commit() {
  /usr/bin/env -i HOME="$OWNER_HOME" PATH="$SAFE_PATH" LANG=C LC_ALL=C \
    GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null GIT_TERMINAL_PROMPT=0 \
    GIT_AUTHOR_NAME="$PUBLIC_AUTHOR_NAME" GIT_AUTHOR_EMAIL="$PUBLIC_AUTHOR_EMAIL" \
    GIT_COMMITTER_NAME="$PUBLIC_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$PUBLIC_AUTHOR_EMAIL" \
    "$GIT_BIN" -c core.hooksPath=/dev/null "$@"
}

validate_tree() {
  mode=$1
  tree=$2
  object_id=${3-}
  if [ "$mode" = working ]; then
    "$PYTHON_RUN" "$PUBLISH_GUARD" "$tree"
    "$PYTHON_RUN" "$PUBLIC_GUARD" "$tree"
  elif [ "$mode" = staged ]; then
    "$PYTHON_RUN" "$PUBLISH_GUARD" --staged "$tree"
    "$PYTHON_RUN" "$PUBLIC_GUARD" --staged "$tree"
  else
    "$PYTHON_RUN" "$PUBLISH_GUARD" --tree-ish "$object_id" "$tree"
    "$PYTHON_RUN" "$PUBLIC_GUARD" --tree-ish "$object_id" "$tree"
  fi
}

TEMP_ROOT=
cleanup() {
  case "$TEMP_ROOT" in
    /tmp/experiment-design-publication.*)
      [ ! -e "$TEMP_ROOT" ] || "$RM_BIN" -rf -- "$TEMP_ROOT"
      ;;
  esac
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

# Validate before authentication or any network access.
validate_tree working "$PUBLISH_SRC"

umask 077
TEMP_ROOT=$($MKTEMP_BIN -d /tmp/experiment-design-publication.XXXXXX) || \
  abort 'could not create the ephemeral publication directory'
CLONE=$TEMP_ROOT/clone
GH_ISOLATED_CONFIG=$TEMP_ROOT/gh-config
"$MKDIR_BIN" -m 700 "$GH_ISOLATED_CONFIG"
if [ -n "$ambient_gh_token" ]; then
  publication_token=$ambient_gh_token
elif [ -n "$ambient_github_token" ]; then
  publication_token=$ambient_github_token
else
  publication_token=$(
    /usr/bin/env -i HOME="$OWNER_HOME" PATH="$SAFE_PATH" LANG=C LC_ALL=C \
      "$GH_BIN" auth token --hostname github.com
  ) || abort 'could not extract the github.com authentication token'
fi
[ -n "$publication_token" ] || abort 'github.com authentication token is empty'

fetch_destination_metadata() {
  metadata_dir=$1
  "$MKDIR_BIN" -m 700 "$metadata_dir"
  run_gh api --hostname github.com user >"$metadata_dir/user.json"
  run_gh api --hostname github.com "repos/$EXPECTED_REPO" >"$metadata_dir/repository.json"
  run_gh api --hostname github.com \
    "repos/$EXPECTED_REPO/branches/$EXPECTED_BRANCH" >"$metadata_dir/branch.json"
  "$PYTHON_RUN" "$PUBLIC_GUARD" --github-metadata "$metadata_dir"
}

printf '%s\n' '== verifying the immutable public destination =='
remote_sha=$(fetch_destination_metadata "$TEMP_ROOT/preclone")

printf '%s\n' '== running the private full-source release gate =='
/usr/bin/env -i HOME="$OWNER_HOME" PATH="$SAFE_PATH" LANG=C LC_ALL=C \
  "$MAKE_BIN" -C "$SRC" release-check

# Tests are not allowed to mutate the separately reviewed public tree.
validate_tree working "$PUBLISH_SRC"

printf '%s\n' '== creating a fresh temporary clone =='
run_git clone --quiet --no-local --origin origin --branch "$EXPECTED_BRANCH" \
  --single-branch "$EXPECTED_CLONE_URL" "$CLONE"
"$PYTHON_RUN" "$PUBLIC_GUARD" --repository-state "$CLONE" \
  --expected-head "$remote_sha" --expected-origin-head "$remote_sha"

printf '%s\n' '== mirroring authorized public content =='
/usr/bin/env -i HOME=/var/empty PATH=/usr/bin:/bin LANG=C LC_ALL=C \
  "$RSYNC_BIN" -a --delete \
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

validate_tree working "$CLONE"

if run_git -C "$CLONE" diff --quiet && \
   run_git -C "$CLONE" diff --cached --quiet && \
   [ -z "$(run_git -C "$CLONE" status --porcelain --untracked-files=all)" ]; then
  printf '%s\n' '== no changes to publish =='
  exit 0
fi

run_git -C "$CLONE" add -A
validate_tree staged "$CLONE"
printf '%s\n' '== changes to publish =='
run_git -C "$CLONE" diff --cached --stat

set +e
"$PYTHON_RUN" "$CREDENTIAL_GUARD" --staged "$CLONE"
scan_status=$?
set -e
if [ "$scan_status" -eq 1 ]; then
  abort 'possible credential in the staged public tree'
fi
[ "$scan_status" -eq 0 ] || abort 'credential scanner failed closed'

if [ "$DRY" -eq 1 ]; then
  printf '%s\n' '== dry run: temporary clone removed; nothing committed or pushed =='
  exit 0
fi

run_git_commit -C "$CLONE" commit --quiet --message "$PUBLIC_COMMIT_MESSAGE"
commit_sha=$(run_git -C "$CLONE" rev-parse HEAD)
validate_tree committed "$CLONE" "$commit_sha"
"$PYTHON_RUN" "$PUBLIC_GUARD" --repository-state "$CLONE" \
  --expected-head "$commit_sha" --expected-origin-head "$remote_sha" \
  --require-public-commit-metadata

# Re-fetch all identity fields immediately before pushing and refuse to race a
# changed main branch. The exact-SHA lease below supplies the server-side CAS.
prepush_sha=$(fetch_destination_metadata "$TEMP_ROOT/prepush")
[ "$prepush_sha" = "$remote_sha" ] || \
  abort 'public main changed after cloning; restart from a fresh review'

run_git -C "$CLONE" push --porcelain \
  --force-with-lease="refs/heads/$EXPECTED_BRANCH:$remote_sha" origin \
  "$commit_sha:refs/heads/$EXPECTED_BRANCH"

published_sha=$(fetch_destination_metadata "$TEMP_ROOT/postpush")
[ "$published_sha" = "$commit_sha" ] || \
  abort 'GitHub did not report the exact validated commit after push'
printf '== pushed exact commit %s to %s ==\n' "$commit_sha" "$EXPECTED_CLONE_URL"
