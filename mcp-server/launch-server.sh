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

# Child runtimes and any utilities they invoke inherit only administrator-owned
# installation locations. Explicit EXPDESIGN_RSCRIPT/EXPDESIGN_PYTHON values
# are still accepted by the TypeScript runtime, but must themselves be absolute.
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:/Library/Frameworks/R.framework/Resources/bin
export PATH
unset NODE_OPTIONS NODE_PATH
unset LD_PRELOAD LD_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH
unset DYLD_FRAMEWORK_PATH DYLD_FALLBACK_LIBRARY_PATH
unset R_ENVIRON R_ENVIRON_USER R_PROFILE R_PROFILE_USER R_HOME R_USER
R_LIBS=
R_LIBS_USER=
R_LIBS_SITE=
R_DEFAULT_PACKAGES=datasets,utils,grDevices,graphics,stats,methods
export R_LIBS R_LIBS_USER R_LIBS_SITE R_DEFAULT_PACKAGES

exec "$node_bin" "$server_dir/dist/index.js"
