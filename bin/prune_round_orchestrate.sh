#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat 1>&2 <<'USAGE'
Usage:
  prune_round_orchestrate.sh \
    --protected-read-ids-ever <file> \
    --protected-read-ids-round <file> \
    --protected-ids <file> \
    [--protected-stats <file>] \
    --candidate <name> <file> [...repeatable] \
    --c1-ids <file> \
    --round-prune-ids <file> \
    --round-prune-stats <file> \
    --prune-cumulative-pool-all <0|1> \
    --blast-unassigned-status <value> \
    [--merge-error-context <message>]
USAGE
  exit 2
}

protected_read_ids_ever=""
protected_read_ids_round=""
protected_ids=""
protected_stats=""
cand_names=()
cand_files=()
c1_ids=""
round_prune_ids=""
round_prune_stats=""
prune_cumulative_pool_all=""
blast_unassigned_status=""
merge_error_context="failed to merge round prune ID lists"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --protected-read-ids-ever)
      [ "$#" -ge 2 ] || usage
      protected_read_ids_ever="$2"
      shift 2
      ;;
    --protected-read-ids-round)
      [ "$#" -ge 2 ] || usage
      protected_read_ids_round="$2"
      shift 2
      ;;
    --protected-ids)
      [ "$#" -ge 2 ] || usage
      protected_ids="$2"
      shift 2
      ;;
    --protected-stats)
      [ "$#" -ge 2 ] || usage
      protected_stats="$2"
      shift 2
      ;;
    --candidate)
      [ "$#" -ge 3 ] || usage
      cand_names+=("$2")
      cand_files+=("$3")
      shift 3
      ;;
    --c1-ids)
      [ "$#" -ge 2 ] || usage
      c1_ids="$2"
      shift 2
      ;;
    --round-prune-ids)
      [ "$#" -ge 2 ] || usage
      round_prune_ids="$2"
      shift 2
      ;;
    --round-prune-stats)
      [ "$#" -ge 2 ] || usage
      round_prune_stats="$2"
      shift 2
      ;;
    --prune-cumulative-pool-all)
      [ "$#" -ge 2 ] || usage
      prune_cumulative_pool_all="$2"
      shift 2
      ;;
    --blast-unassigned-status)
      [ "$#" -ge 2 ] || usage
      blast_unassigned_status="$2"
      shift 2
      ;;
    --merge-error-context)
      [ "$#" -ge 2 ] || usage
      merge_error_context="$2"
      shift 2
      ;;
    *)
      usage
      ;;
  esac
done

for req in protected_read_ids_ever protected_read_ids_round protected_ids c1_ids round_prune_ids round_prune_stats prune_cumulative_pool_all blast_unassigned_status; do
  [ -n "${!req}" ] || usage
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
build_protected_script="$script_dir/build_protected_ids.sh"
merge_prune_script="$script_dir/merge_prune_ids.sh"
cand_tmp_files=()
protected_ids_sorted=""

cleanup_merge_lists() {
  for f in "${cand_tmp_files[@]+"${cand_tmp_files[@]}"}"; do
    rm -f "$f"
  done
  rm -f "$protected_ids_sorted"
}

subtract_protected_copy() {
  local source="$1"
  local protected_sorted="$2"
  local out="$3"
  local source_sorted

  if [ ! -s "$source" ]; then
    : > "$out"
    return 0
  fi
  if [ ! -s "$protected_sorted" ]; then
    cp "$source" "$out"
    return 0
  fi

  source_sorted="${out}.source.sorted"
  LC_ALL=C sort -u "$source" > "$source_sorted"
  comm -23 "$source_sorted" "$protected_sorted" > "$out"
  rm -f "$source_sorted"
}

: > "$protected_ids"
if [ -n "$protected_stats" ]; then
  : > "$protected_stats"
fi

if ! bash "$build_protected_script" \
  "$protected_read_ids_ever" \
  "$protected_read_ids_round" \
  "$protected_ids" \
  ${protected_stats:+"$protected_stats"}; then
  echo "ERROR: build_protected_ids.sh failed; aborting prune orchestration to preserve protected-read coverage" 1>&2
  exit 1
fi

case "$prune_cumulative_pool_all" in
  0|1)
    ;;
  *)
    echo "ERROR: invalid --prune-cumulative-pool-all value '$prune_cumulative_pool_all'" 1>&2
    exit 2
    ;;
esac

if [ "$prune_cumulative_pool_all" = "1" ]; then
  # Non-C1 candidates get protected-ID subtraction into temporary files; the
  # caller's source lists remain unchanged. C1 is passed through as-is since
  # it is the authoritative archive/drop set for consolidated reads.
  trap cleanup_merge_lists EXIT
  protected_ids_sorted="$(mktemp "${TMPDIR:-/tmp}/prune_protected.XXXXXX")"
  if [ -s "$protected_ids" ]; then
    LC_ALL=C sort -u "$protected_ids" > "$protected_ids_sorted"
  else
    : > "$protected_ids_sorted"
  fi

  merge_args=()
  merge_args+=(--source c1 "$c1_ids")

  local_idx=0
  while [ "$local_idx" -lt "${#cand_names[@]}" ]; do
    local_name="${cand_names[$local_idx]}"
    local_file="${cand_files[$local_idx]}"
    local_tmp="$(mktemp "${TMPDIR:-/tmp}/prune_${local_name}.XXXXXX")"
    cand_tmp_files+=("$local_tmp")
    subtract_protected_copy "$local_file" "$protected_ids_sorted" "$local_tmp"
    merge_args+=(--source "$local_name" "$local_tmp")
    local_idx=$(( local_idx + 1 ))
  done

  if ! bash "$merge_prune_script" \
    --out-list "$round_prune_ids" \
    --out-stats "$round_prune_stats" \
    "${merge_args[@]}"; then
    echo "ERROR: $merge_error_context" 1>&2
    exit 1
  fi
else
  : > "$round_prune_ids"
  printf 'disabled\t1\nreason\tprune_cumulative_pool_all_off\n' > "$round_prune_stats"
fi

if [ -n "$protected_stats" ] && [ -s "$protected_stats" ]; then
  cat "$protected_stats" >> "$round_prune_stats"
fi
printf 'blast_unassigned_status\t%s\n' "$blast_unassigned_status" >> "$round_prune_stats"
cleanup_merge_lists
trap - EXIT
