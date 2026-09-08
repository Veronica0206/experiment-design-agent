#!/bin/sh
# Launch the MCP runtime without consulting an ambient, user-controlled PATH.
case "$0" in
  */*) launcher_path=$0 ;;
  *) launcher_path=./$0 ;;
esac
launcher_parent=${launcher_path%/*}
server_dir=$(CDPATH= cd -P -- "$launcher_parent" 2>/dev/null && pwd -P) || {
  printf '%s\n' 'Experiment-design MCP directory could not be resolved.' >&2
  exit 1
}

node_bin=
if [ "${EXPDESIGN_NODE+x}" = x ]; then
  case "$EXPDESIGN_NODE" in
    /*) ;;
    *)
      printf '%s\n' 'EXPDESIGN_NODE must be an absolute path.' >&2
      exit 1
      ;;
  esac
  if [ -f "$EXPDESIGN_NODE" ] && [ -x "$EXPDESIGN_NODE" ]; then
    node_bin=$EXPDESIGN_NODE
  fi
else
  for candidate in \
    /opt/homebrew/bin/node \
    /usr/local/bin/node \
    /usr/bin/node \
    /opt/local/bin/node
  do
    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
      node_bin=$candidate
      break
    fi
  done
fi

if [ -z "$node_bin" ]; then
  printf '%s\n' 'No approved absolute Node.js executable is available for the MCP server.' >&2
  exit 1
fi

# Validate every caller-provided value that is intentionally admitted across
# the boundary. Everything else -- provider/cloud credentials, proxies, npm
# configuration, native-loader controls, Node preloads, Python/R startup
# controls, and unrelated EXPDESIGN flags -- is omitted by the env -i exec.
fail_invalid() {
  printf '%s\n' "$1" >&2
  exit 1
}

validate_absolute_executable() {
  override_name=$1
  override_value=$2
  case "$override_value" in
    /*) ;;
    *) fail_invalid "$override_name must be an absolute path." ;;
  esac
  if [ ! -f "$override_value" ] || [ ! -x "$override_value" ]; then
    fail_invalid "$override_name does not identify an executable file."
  fi
}

validate_absolute_path() {
  setting_name=$1
  setting_value=$2
  case "$setting_value" in
    /*) ;;
    *) fail_invalid "$setting_name must be an absolute path." ;;
  esac
}

validate_absolute_path_list() {
  setting_name=$1
  remaining=$2
  [ -n "$remaining" ] || fail_invalid "$setting_name must contain absolute paths."
  while :; do
    case "$remaining" in
      *:*) item=${remaining%%:*}; remaining=${remaining#*:}; more=yes ;;
      *) item=$remaining; more=no ;;
    esac
    case "$item" in
      /*) ;;
      *) fail_invalid "$setting_name must contain only absolute paths." ;;
    esac
    [ "$more" = yes ] || break
  done
}

validate_positive_integer() {
  setting_name=$1
  setting_value=$2
  case "$setting_value" in
    ''|*[!0-9]*) fail_invalid "$setting_name must be a positive integer." ;;
  esac
  [ "$setting_value" -gt 0 ] 2>/dev/null ||
    fail_invalid "$setting_name must be a positive integer."
}

if [ "${EXPDESIGN_RSCRIPT+x}" = x ]; then
  validate_absolute_executable EXPDESIGN_RSCRIPT "$EXPDESIGN_RSCRIPT"
fi
if [ "${EXPDESIGN_PYTHON+x}" = x ]; then
  validate_absolute_executable EXPDESIGN_PYTHON "$EXPDESIGN_PYTHON"
fi
if [ "${EXPDESIGN_ALLOWED_READ_ROOTS+x}" = x ]; then
  validate_absolute_path_list EXPDESIGN_ALLOWED_READ_ROOTS "$EXPDESIGN_ALLOWED_READ_ROOTS"
fi
if [ "${EXPDESIGN_RUNS_DIR+x}" = x ]; then
  validate_absolute_path EXPDESIGN_RUNS_DIR "$EXPDESIGN_RUNS_DIR"
fi
if [ "${EXPDESIGN_RUNTIME_REGISTRY_DIR+x}" = x ]; then
  validate_absolute_path EXPDESIGN_RUNTIME_REGISTRY_DIR "$EXPDESIGN_RUNTIME_REGISTRY_DIR"
fi
for setting_name in \
  EXPDESIGN_ARTIFACT_LOCK_WAIT_MS \
  EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS \
  EXPDESIGN_ARTIFACT_RETENTION_DAYS \
  EXPDESIGN_ARTIFACT_MAX_DIRS
do
  # POSIX sh has no indirect expansion. Select each reviewed setting without
  # eval so an ambient value can never become shell syntax.
  case "$setting_name" in
    EXPDESIGN_ARTIFACT_LOCK_WAIT_MS)
      if [ "${EXPDESIGN_ARTIFACT_LOCK_WAIT_MS+x}" = x ]; then
        validate_positive_integer "$setting_name" "$EXPDESIGN_ARTIFACT_LOCK_WAIT_MS"
      fi
      ;;
    EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS)
      if [ "${EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS+x}" = x ]; then
        validate_positive_integer "$setting_name" "$EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS"
      fi
      ;;
    EXPDESIGN_ARTIFACT_RETENTION_DAYS)
      if [ "${EXPDESIGN_ARTIFACT_RETENTION_DAYS+x}" = x ]; then
        validate_positive_integer "$setting_name" "$EXPDESIGN_ARTIFACT_RETENTION_DAYS"
      fi
      ;;
    EXPDESIGN_ARTIFACT_MAX_DIRS)
      if [ "${EXPDESIGN_ARTIFACT_MAX_DIRS+x}" = x ]; then
        validate_positive_integer "$setting_name" "$EXPDESIGN_ARTIFACT_MAX_DIRS"
      fi
      ;;
  esac
done

env_bin=/usr/bin/env
if [ ! -f "$env_bin" ] || [ ! -x "$env_bin" ]; then
  fail_invalid 'The approved absolute env executable is unavailable.'
fi

approved_path=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:/Library/Frameworks/R.framework/Resources/bin
controlled_home=/
if [ -d /var/empty ]; then
  controlled_home=/var/empty
fi

set -- "$env_bin" -i \
  "PATH=$approved_path" \
  "HOME=$controlled_home" \
  "TMPDIR=/tmp" \
  "TMP=/tmp" \
  "TEMP=/tmp" \
  "LANG=C" \
  "LC_ALL=C" \
  "TZ=UTC" \
  "R_LIBS=" \
  "R_LIBS_USER=" \
  "R_LIBS_SITE=" \
  "R_DEFAULT_PACKAGES=datasets,utils,grDevices,graphics,stats,methods"

if [ "${EXPDESIGN_NODE+x}" = x ]; then
  set -- "$@" "EXPDESIGN_NODE=$EXPDESIGN_NODE"
fi
if [ "${EXPDESIGN_RSCRIPT+x}" = x ]; then
  set -- "$@" "EXPDESIGN_RSCRIPT=$EXPDESIGN_RSCRIPT"
fi
if [ "${EXPDESIGN_PYTHON+x}" = x ]; then
  set -- "$@" "EXPDESIGN_PYTHON=$EXPDESIGN_PYTHON"
fi
if [ "${EXPDESIGN_ALLOWED_READ_ROOTS+x}" = x ]; then
  set -- "$@" "EXPDESIGN_ALLOWED_READ_ROOTS=$EXPDESIGN_ALLOWED_READ_ROOTS"
fi
if [ "${EXPDESIGN_RUNS_DIR+x}" = x ]; then
  set -- "$@" "EXPDESIGN_RUNS_DIR=$EXPDESIGN_RUNS_DIR"
fi
if [ "${EXPDESIGN_ARTIFACT_LOCK_WAIT_MS+x}" = x ]; then
  set -- "$@" "EXPDESIGN_ARTIFACT_LOCK_WAIT_MS=$EXPDESIGN_ARTIFACT_LOCK_WAIT_MS"
fi
if [ "${EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS+x}" = x ]; then
  set -- "$@" "EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS=$EXPDESIGN_ARTIFACT_MAX_LEASE_SECONDS"
fi
if [ "${EXPDESIGN_ARTIFACT_RETENTION_DAYS+x}" = x ]; then
  set -- "$@" "EXPDESIGN_ARTIFACT_RETENTION_DAYS=$EXPDESIGN_ARTIFACT_RETENTION_DAYS"
fi
if [ "${EXPDESIGN_ARTIFACT_MAX_DIRS+x}" = x ]; then
  set -- "$@" "EXPDESIGN_ARTIFACT_MAX_DIRS=$EXPDESIGN_ARTIFACT_MAX_DIRS"
fi
if [ "${EXPDESIGN_RUNTIME_REGISTRY_DIR+x}" = x ]; then
  set -- "$@" "EXPDESIGN_RUNTIME_REGISTRY_DIR=$EXPDESIGN_RUNTIME_REGISTRY_DIR"
fi
if [ "${EXPDESIGN_RUNTIME_REGISTRY_TOKEN+x}" = x ]; then
  set -- "$@" "EXPDESIGN_RUNTIME_REGISTRY_TOKEN=$EXPDESIGN_RUNTIME_REGISTRY_TOKEN"
fi
if [ "${EXPDESIGN_RUNTIME_REGISTRY_DEV+x}" = x ]; then
  set -- "$@" "EXPDESIGN_RUNTIME_REGISTRY_DEV=$EXPDESIGN_RUNTIME_REGISTRY_DEV"
fi
if [ "${EXPDESIGN_RUNTIME_REGISTRY_INO+x}" = x ]; then
  set -- "$@" "EXPDESIGN_RUNTIME_REGISTRY_INO=$EXPDESIGN_RUNTIME_REGISTRY_INO"
fi

exec "$@" "$node_bin" "$server_dir/dist/index.js"
