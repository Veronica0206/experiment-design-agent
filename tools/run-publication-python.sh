#!/bin/sh
# Run publication checks with a fixed interpreter and an approved absolute Git.
set -eu

case "$0" in
  /*) launcher_path=$0 ;;
  */*) launcher_path=$(/bin/pwd -P)/$0 ;;
  *)
    printf '%s\n' 'Publication Python launcher must be invoked with a path.' >&2
    exit 1
    ;;
esac
launcher_parent=${launcher_path%/*}
script_dir=$(CDPATH= cd -P -- "$launcher_parent" 2>/dev/null && /bin/pwd -P) || {
  printf '%s\n' 'Could not resolve the publication Python launcher directory.' >&2
  exit 1
}

[ "$#" -gt 0 ] || {
  printf '%s\n' 'Publication Python requires an approved guard script.' >&2
  exit 2
}
case "$1" in
  "$script_dir/check_publish_source.py"|\
  "$script_dir/validate_public_distribution.py"|\
  "$script_dir/check_diff_credentials.py") ;;
  *)
    printf '%s\n' 'Publication Python may run only the three fixed publication guards.' >&2
    exit 2
    ;;
esac
[ -f "$1" ] && [ ! -L "$1" ] || {
  printf '%s\n' 'Approved publication guard must be a regular, non-symlink file.' >&2
  exit 1
}

python_bin=
for candidate in \
  /usr/bin/python3 \
  /opt/homebrew/bin/python3 \
  /usr/local/bin/python3 \
  /opt/local/bin/python3
do
  if [ -f "$candidate" ] && [ -x "$candidate" ]; then
    python_bin=$candidate
    break
  fi
done
if [ -z "$python_bin" ]; then
  printf '%s\n' 'No approved absolute publication Python is available.' >&2
  exit 1
fi

git_bin=/usr/bin/git
if [ ! -f "$git_bin" ] || [ ! -x "$git_bin" ]; then
  printf '%s\n' 'The approved absolute publication Git is unavailable.' >&2
  exit 1
fi

manifest_pin=${EXPDESIGN_PUBLISH_MANIFEST_SHA256-}
exec /usr/bin/env -i \
  PATH=/usr/bin:/bin \
  HOME=/var/empty \
  LANG=C LC_ALL=C \
  EXPDESIGN_APPROVED_GIT="$git_bin" \
  EXPDESIGN_PUBLISH_MANIFEST_SHA256="$manifest_pin" \
  "$python_bin" -I -E -s -S -B "$@"
