#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lib/stale_lock_utils.sh"

GLOBAL_LEDGER_LOCK_STALE_TTL_SECONDS=300
global_ledger_lock_owned=0

usage() {
  echo "Usage: $0 --slice-sidecar <path> --global-ledger-dir <path>" >&2
  exit 1
}

ensure_dir() {
  [ -d "$1" ] || mkdir -p "$1"
}

current_host_id() {
  if [ -n "${HOSTNAME:-}" ]; then
    printf '%s\n' "$HOSTNAME"
  elif command -v hostname >/dev/null 2>&1; then
    hostname 2>/dev/null || printf 'unknown\n'
  else
    printf 'unknown\n'
  fi
}

remove_global_ledger_lock_if_stale() {
  rm -f "$global_lock_meta" 2>/dev/null || true
  rmdir "$global_lock_dir" 2>/dev/null || rm -rf "$global_lock_dir" 2>/dev/null || true
}

release_global_ledger_lock() {
  if [ "${global_ledger_lock_owned:-0}" -eq 1 ]; then
    rm -f "$global_lock_meta" 2>/dev/null || true
    rmdir "$global_lock_dir" 2>/dev/null || rm -rf "$global_lock_dir" 2>/dev/null || true
    global_ledger_lock_owned=0
  fi
}

acquire_global_ledger_lock_best_effort() {
  local timeout_seconds="${1:-10}"
  local host started_epoch waited reclaim_status

  ensure_dir "$global_ledger_dir"
  waited=0
  host="$(current_host_id)"
  started_epoch="$(date +%s 2>/dev/null || echo 0)"

  while ! mkdir "$global_lock_dir" 2>/dev/null; do
    stale_lock_maybe_reclaim \
      "$global_lock_dir" \
      "$global_lock_meta" \
      "$host" \
      "$GLOBAL_LEDGER_LOCK_STALE_TTL_SECONDS" \
      "global feeder ledger lock" \
      remove_global_ledger_lock_if_stale \
      0
    reclaim_status=$?
    if [ "$reclaim_status" -eq 2 ] || [ "$reclaim_status" -eq 11 ]; then
      echo "WARNING: unable to recover global feeder ledger lock at $global_lock_dir" >&2
      return 1
    fi
    if mkdir "$global_lock_dir" 2>/dev/null; then
      break
    fi
    if [ "$waited" -ge "$timeout_seconds" ]; then
      echo "WARNING: global feeder ledger lock unavailable after ${timeout_seconds}s; completion not recorded globally" >&2
      return 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done

  if ! {
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$host"
    printf 'started_epoch=%s\n' "$started_epoch"
  } > "$global_lock_meta"; then
    release_global_ledger_lock
    echo "WARNING: failed to write global feeder ledger lock metadata at $global_lock_meta" >&2
    return 1
  fi
  global_ledger_lock_owned=1
  return 0
}

sidecar_path=""
global_ledger_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --slice-sidecar)
      shift
      sidecar_path="${1:-}"
      ;;
    --global-ledger-dir)
      shift
      global_ledger_dir="${1:-}"
      ;;
    *)
      usage
      ;;
  esac
  shift
done

[ -n "$sidecar_path" ] || usage
[ -n "$global_ledger_dir" ] || usage
[ -f "$sidecar_path" ] || exit 0

global_lock_dir="${global_ledger_dir}/.lockdir"
global_lock_meta="${global_lock_dir}/meta.env"
ledger_path="${global_ledger_dir}/slice_completion.tsv"
slice_fp="$(awk -F= '/^# slice_fingerprint=/{print $2; exit}' "$sidecar_path" 2>/dev/null || true)"
[ -n "$slice_fp" ] || exit 0
run_path="$(cd "$(dirname "$sidecar_path")/.." && pwd -P)"
completed_timestamp="$(date +%s 2>/dev/null || echo 0)"

trap 'release_global_ledger_lock' EXIT
if ! acquire_global_ledger_lock_best_effort 10; then
  exit 0
fi

ensure_dir "$global_ledger_dir"
while IFS= read -r line; do
  case "$line" in
    \#*|'')
      continue
      ;;
  esac
  IFS=$'\t' read -r source_fp file_basename start_line end_line <<EOF
$line
EOF
  if [ -z "$source_fp" ] || [ "$source_fp" = "-" ]; then
    echo "WARNING: skipping completion row with missing source fingerprint in ${sidecar_path}" >&2
    continue
  fi
  if [ -f "$ledger_path" ] && awk -F '\t' -v a="$slice_fp" -v b="$source_fp" -v c="$file_basename" -v d="$start_line" -v e="$end_line" -v f="$run_path" '($1==a && $2==b && $3==c && $4==d && $5==e && $6==f){found=1; exit} END{exit(found ? 0 : 1)}' "$ledger_path"; then
    continue
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$slice_fp" "$source_fp" "$file_basename" "$start_line" "$end_line" "$run_path" "$completed_timestamp" >> "$ledger_path"
done < "$sidecar_path"
