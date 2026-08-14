#!/bin/sh
# Normalize every launcher/runtime failure to Claude Code's blocking hook code.
case "$0" in
  */*) launcher_path=$0 ;;
  *) launcher_path=./$0 ;;
esac
launcher_parent=${launcher_path%/*}
script_dir=$(CDPATH= cd -P -- "$launcher_parent" 2>/dev/null && pwd -P) || {
  printf '%s' 'INTERNAL_ERROR. Verification hook directory could not be resolved.' >&2
  exit 2
}

node_bin=
if [ "${EXPDESIGN_NODE+x}" = x ]; then
  case "$EXPDESIGN_NODE" in
    /*) ;;
    *)
      printf '%s' 'INTERNAL_ERROR. EXPDESIGN_NODE must be an absolute path.' >&2
      exit 2
      ;;
  esac
  if [ -f "$EXPDESIGN_NODE" ] && [ -x "$EXPDESIGN_NODE" ]; then
    node_bin=$EXPDESIGN_NODE
  fi
else
  # Never consult ambient PATH here. These are the reviewed system/package-
  # manager locations supported by the hook boundary.
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
  printf '%s' 'INTERNAL_ERROR. No approved absolute Node.js executable is available for verification.' >&2
  exit 2
fi

unset NODE_OPTIONS NODE_PATH
unset LD_PRELOAD LD_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH
unset DYLD_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH
"$node_bin" "$script_dir/launch_verification.mjs" "$@"
status=$?
if [ "$status" -eq 0 ]; then
  exit 0
fi
exit 2
