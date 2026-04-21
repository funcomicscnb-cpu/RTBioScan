#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --state-dir DIR --round-barcode ID [--lock-wait SEC] [--no-lock]" >&2
}

state_dir=""
round_barcode=""
lock_wait="300"
use_lock=1

while [ "$#" -gt 0 ]; do
  case "$1" in
    --state-dir)
      state_dir="${2:-}"
      shift 2
      ;;
    --round-barcode)
      round_barcode="${2:-}"
      shift 2
      ;;
    --lock-wait)
      lock_wait="${2:-}"
      shift 2
      ;;
    --no-lock)
      use_lock=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -z "$state_dir" ] || [ -z "$round_barcode" ]; then
  usage
  exit 2
fi

if ! [[ "$lock_wait" =~ ^[0-9]+$ ]]; then
  echo "Invalid --lock-wait '$lock_wait' (expected integer >= 0)" >&2
  exit 2
fi

if [[ "$round_barcode" == *$'\t'* ]] || [[ "$round_barcode" == *$'\n'* ]] || [[ "$round_barcode" == *$'\r'* ]]; then
  echo "Invalid --round-barcode '$round_barcode' (tab/newline not allowed)" >&2
  exit 2
fi

mkdir -p "$state_dir"
index_file="$state_dir/round_index.tsv"
lock_dir="$state_dir/.round_index.lock.lockdir"
lock_acquired=0

release_lock() {
  if [ "$lock_acquired" -eq 1 ]; then
    rmdir "$lock_dir" 2>/dev/null || true
    lock_acquired=0
  fi
}
trap release_lock EXIT

if [ "$use_lock" -eq 1 ]; then
  waited=0
  while ! mkdir "$lock_dir" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if [ "$lock_wait" -gt 0 ] && [ "$waited" -ge "$lock_wait" ]; then
      echo "Failed to acquire round index lock: $lock_dir" >&2
      exit 3
    fi
  done
  lock_acquired=1
fi

existing_index=""
if [ -s "$index_file" ]; then
  existing_index="$(awk -F'\t' -v rb="$round_barcode" '$1==rb{print $2; exit}' "$index_file")"
fi

if [ -n "$existing_index" ]; then
  if ! [[ "$existing_index" =~ ^[0-9]+$ ]] || [ "$existing_index" -lt 1 ]; then
    echo "Malformed round index '$existing_index' for '$round_barcode' in $index_file" >&2
    exit 4
  fi
  printf '%s\n' "$existing_index"
  exit 0
fi

max_index=0
if [ -s "$index_file" ]; then
  max_index="$(awk -F'\t' '($2 ~ /^[0-9]+$/ && $2>m){m=$2} END{print m+0}' "$index_file")"
fi
if ! [[ "$max_index" =~ ^[0-9]+$ ]]; then
  echo "Malformed max index in $index_file" >&2
  exit 4
fi
new_index=$((max_index + 1))

tmp_file="$(mktemp "${index_file}.tmp.XXXXXX")"
if [ -s "$index_file" ]; then
  cat "$index_file" > "$tmp_file"
fi
printf '%s\t%s\n' "$round_barcode" "$new_index" >> "$tmp_file"
mv "$tmp_file" "$index_file"

printf '%s\n' "$new_index"
