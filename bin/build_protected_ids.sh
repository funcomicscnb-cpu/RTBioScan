#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 3 ]; then
  echo "usage: build_protected_ids.sh <protected_ever> <protected_round> <out> [stats_out]" 1>&2
  exit 2
fi

protected_ever="$1"
protected_round="$2"
out="$3"
stats_out="${4:-}"

tmp="${out}.tmp.$$"
: > "$tmp"
if [ -s "$protected_ever" ]; then
  cat "$protected_ever" >> "$tmp"
fi
if [ -s "$protected_round" ]; then
  cat "$protected_round" >> "$tmp"
fi

if [ -s "$tmp" ]; then
  LC_ALL=C sort -u "$tmp" > "$out"
else
  : > "$out"
fi
rm -f "$tmp"

if [ -n "$stats_out" ]; then
  ever_count=0
  round_count=0
  total_count=0
  if [ -s "$protected_ever" ]; then
    ever_count=$(wc -l < "$protected_ever" | tr -d ' ')
  fi
  if [ -s "$protected_round" ]; then
    round_count=$(wc -l < "$protected_round" | tr -d ' ')
  fi
  if [ -s "$out" ]; then
    total_count=$(wc -l < "$out" | tr -d ' ')
  fi
  printf 'protected_read_ids_ever_count\t%s\n' "$ever_count" > "$stats_out"
  printf 'protected_read_ids_round_count\t%s\n' "$round_count" >> "$stats_out"
  printf 'protected_ids_total\t%s\n' "$total_count" >> "$stats_out"
fi
