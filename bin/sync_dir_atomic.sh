#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
  echo "usage: $0 <src_dir> <dst_dir>" >&2
  exit 2
fi

src="$1"
dst="$2"

if [ ! -d "$src" ]; then
  echo "source directory does not exist: $src" >&2
  exit 1
fi

dst_parent="$(dirname "$dst")"
mkdir -p "$dst_parent"

tmp="${dst}.sync_tmp.$$"
bak="${dst}.sync_bak.$$"

cleanup() {
  rm -rf "$tmp" "$bak" 2>/dev/null || true
}
trap cleanup EXIT

rm -rf "$tmp"
mkdir -p "$tmp"

sync_with_fallback() {
  if command -v rsync >/dev/null 2>&1; then
    if rsync -a --delete "$src"/ "$tmp"/; then
      return 0
    fi
    echo "WARN: rsync failed for $src -> $dst, falling back to cp -Rp" >&2
    rm -rf "$tmp"
    mkdir -p "$tmp"
  fi
  cp -Rp "$src"/. "$tmp"/
}

sync_with_fallback

if [ -e "$dst" ]; then
  mv "$dst" "$bak"
fi
mv "$tmp" "$dst"

if [ -e "$bak" ]; then
  rm -rf "$bak" || true    # cleanup failure is non-fatal; EXIT trap handles it
fi

trap - EXIT
exit 0
