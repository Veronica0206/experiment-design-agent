#!/bin/sh
# Run the same reviewed R installation/library surface used by the MCP runtime.
set -eu

rscript_bin=
if [ "${EXPDESIGN_RSCRIPT+x}" = x ]; then
  case "$EXPDESIGN_RSCRIPT" in
    /*) ;;
    *)
      printf '%s\n' 'EXPDESIGN_RSCRIPT must be an absolute path.' >&2
      exit 1
      ;;
  esac
  if [ -f "$EXPDESIGN_RSCRIPT" ] && [ -x "$EXPDESIGN_RSCRIPT" ]; then
    rscript_bin=$EXPDESIGN_RSCRIPT
  fi
else
  for candidate in \
    /Library/Frameworks/R.framework/Resources/bin/Rscript \
    /opt/homebrew/bin/Rscript \
    /usr/local/bin/Rscript \
    /usr/bin/Rscript \
    /opt/local/bin/Rscript
  do
    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
      rscript_bin=$candidate
      break
    fi
  done
fi

if [ -z "$rscript_bin" ]; then
  printf '%s\n' 'No approved absolute Rscript executable is available.' >&2
  exit 1
fi

PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:/opt/local/bin:/Library/Frameworks/R.framework/Resources/bin
export PATH
unset R_ENVIRON R_ENVIRON_USER R_PROFILE R_PROFILE_USER R_HOME R_USER
# Match the MCP child policy: remove every native-loader-prefixed variable,
# including platform-specific additions such as LD_AUDIT or DYLD_PRINT_*.
loader_names=$(
  /usr/bin/env | /usr/bin/sed -n \
    -e 's/^\([Ll][Dd]_[A-Za-z0-9_]*\)=.*/\1/p' \
    -e 's/^\([Dd][Yy][Ll][Dd]_[A-Za-z0-9_]*\)=.*/\1/p'
)
for loader_name in $loader_names; do
  unset "$loader_name"
done
unset loader_name loader_names
R_LIBS=
R_LIBS_USER=
R_LIBS_SITE=
R_DEFAULT_PACKAGES=datasets,utils,grDevices,graphics,stats,methods
export R_LIBS R_LIBS_USER R_LIBS_SITE R_DEFAULT_PACKAGES

if [ "${1:-}" = "--version" ]; then
  exec "$rscript_bin" --version
fi
exec "$rscript_bin" --vanilla "$@"
