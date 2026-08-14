#!/bin/sh
# Launch application Python only through the pre-site validation boundary.
set -eu

case "$0" in
  */*) launcher_path=$0 ;;
  *) launcher_path=./$0 ;;
esac
launcher_parent=${launcher_path%/*}
script_dir=$(CDPATH= cd -P -- "$launcher_parent" 2>/dev/null && pwd -P) || {
  printf '%s\n' 'Could not resolve the reviewed Python launcher directory.' >&2
  exit 1
}
suite_root=${script_dir%/tools}

python_bin=
if [ "${EXPDESIGN_PYTHON+x}" = x ]; then
  case "$EXPDESIGN_PYTHON" in
    /*) ;;
    *)
      printf '%s\n' 'EXPDESIGN_PYTHON must be an absolute path.' >&2
      exit 1
      ;;
  esac
  if [ -f "$EXPDESIGN_PYTHON" ] && [ -x "$EXPDESIGN_PYTHON" ]; then
    python_bin=$EXPDESIGN_PYTHON
  fi
else
  for candidate in \
    "$suite_root/agent-harness/.venv/bin/python" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /usr/bin/python3 \
    /opt/local/bin/python3
  do
    if [ -f "$candidate" ] && [ -x "$candidate" ]; then
      python_bin=$candidate
      break
    fi
  done
fi

if [ -z "$python_bin" ]; then
  printf '%s\n' 'No approved absolute Python executable is available.' >&2
  exit 1
fi

# Python flags close user/site configuration; remove native-loader and Python
# variables as defense in depth before the interpreter itself starts.
PATH=/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:/opt/local/bin
export PATH
python_environment_names=$(
  /usr/bin/env | /usr/bin/sed -n \
    -e 's/^\([Pp][Yy][Tt][Hh][Oo][Nn][A-Za-z0-9_]*\)=.*/\1/p' \
    -e 's/^\([Ll][Dd]_[A-Za-z0-9_]*\)=.*/\1/p' \
    -e 's/^\([Dd][Yy][Ll][Dd]_[A-Za-z0-9_]*\)=.*/\1/p'
)
for environment_name in $python_environment_names; do
  unset "$environment_name"
done
unset environment_name python_environment_names

exec "$python_bin" -E -s -S -B -I \
  "$script_dir/reviewed_python_runner.py" "$@"
