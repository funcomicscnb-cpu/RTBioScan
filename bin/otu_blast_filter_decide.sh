#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 4 ] || [ "$#" -gt 5 ]; then
  echo "usage: $0 filter_stats.tsv filter_mode enforce_missing_max_frac enforce_no_clusters_policy [force_use_filtered]" >&2
  exit 2
fi

stats_tsv="$1"
filter_mode="$2"
missing_max_frac="$3"
no_clusters_policy="$4"
force_use_filtered_raw="${5:-0}"

case "${force_use_filtered_raw}" in
  1|true|TRUE|yes|YES) force_use_filtered=1 ;;
  0|false|FALSE|no|NO|'') force_use_filtered=0 ;;
  *)
    echo "Invalid force_use_filtered '${force_use_filtered_raw}' (expected true/false/1/0)" >&2
    exit 2
    ;;
esac

case "$filter_mode" in
  off|observe|enforce) ;;
  *)
    echo "Invalid filter_mode '$filter_mode' (expected off|observe|enforce)" >&2
    exit 2
    ;;
esac

case "$no_clusters_policy" in
  fail|fallback_unfiltered|allow_empty) ;;
  *)
    echo "Invalid no_clusters_policy '$no_clusters_policy' (expected fail|fallback_unfiltered|allow_empty)" >&2
    exit 2
    ;;
esac

if ! [[ "$missing_max_frac" =~ ^[0-9]*([.][0-9]+)?$ ]]; then
  echo "Invalid enforce_missing_max_frac '$missing_max_frac' (expected decimal in [0,1])" >&2
  exit 2
fi
if ! awk -v v="$missing_max_frac" 'BEGIN{exit !(v>=0 && v<=1)}'; then
  echo "Invalid enforce_missing_max_frac '$missing_max_frac' (expected decimal in [0,1])" >&2
  exit 2
fi

if [ ! -s "$stats_tsv" ]; then
  echo "Missing or empty stats file: $stats_tsv" >&2
  exit 3
fi

stats_value() {
  local key="$1"
  awk -F'\t' -v k="$key" '$1==k{print $2; exit}' "$stats_tsv"
}

require_key() {
  local key="$1"
  local val
  val="$(stats_value "$key")"
  if [ -z "$val" ]; then
    echo "Missing required stats key '$key' in $stats_tsv" >&2
    exit 3
  fi
  printf '%s' "$val"
}

total_reads="$(require_key total_reads)"
reads_missing="$(require_key reads_missing_from_clstr)"
clstr_records="$(require_key clstr_records)"
total_otus="$(require_key total_otus)"
ambiguous_hash_cluster="$(require_key ambiguous_hash_cluster)"
missing_policy="$(require_key missing_policy)"
clstr_has_clusters="$(stats_value clstr_has_clusters)"
if [ -z "$clstr_has_clusters" ]; then
  clstr_has_clusters="NA"
fi

for pair in \
  "total_reads:$total_reads" \
  "reads_missing_from_clstr:$reads_missing" \
  "clstr_records:$clstr_records" \
  "total_otus:$total_otus" \
  "ambiguous_hash_cluster:$ambiguous_hash_cluster"
do
  key="${pair%%:*}"
  val="${pair#*:}"
  if ! [[ "$val" =~ ^[0-9]+$ ]]; then
    echo "Malformed stats value for '$key': '$val' (expected integer)" >&2
    exit 3
  fi
done

if [ "$clstr_has_clusters" != "NA" ] && ! [[ "$clstr_has_clusters" =~ ^[0-9]+$ ]]; then
  echo "Malformed stats value for 'clstr_has_clusters': '$clstr_has_clusters' (expected integer or missing)" >&2
  exit 3
fi

if [ "$missing_policy" != "keep" ] && [ "$missing_policy" != "drop" ]; then
  echo "Malformed stats value for 'missing_policy': '$missing_policy' (expected keep|drop)" >&2
  exit 3
fi

missing_frac="0"
if [ "$total_reads" -gt 0 ]; then
  missing_frac="$(awk -v m="$reads_missing" -v t="$total_reads" 'BEGIN{printf "%.6f", m/t}')"
fi

emit_decision() {
  local decision="$1"
  local reason="$2"
  printf "decision\t%s\n" "$decision"
  printf "reason\t%s\n" "$reason"
  printf "missing_frac\t%s\n" "$missing_frac"
  printf "total_reads\t%s\n" "$total_reads"
  printf "reads_missing_from_clstr\t%s\n" "$reads_missing"
  printf "clstr_has_clusters\t%s\n" "$clstr_has_clusters"
  printf "clstr_records\t%s\n" "$clstr_records"
  printf "total_otus\t%s\n" "$total_otus"
  printf "ambiguous_hash_cluster\t%s\n" "$ambiguous_hash_cluster"
  printf "missing_policy\t%s\n" "$missing_policy"
}

if [ "$filter_mode" = "off" ]; then
  emit_decision "use_unfiltered" "mode_off"
  exit 0
fi
if [ "$filter_mode" = "observe" ]; then
  emit_decision "use_unfiltered" "mode_observe"
  exit 0
fi

# enforce mode
if [ "$force_use_filtered" -eq 1 ]; then
  emit_decision "use_filtered" "force_use_filtered"
  exit 0
fi
if [ "$ambiguous_hash_cluster" -gt 0 ]; then
  echo "WARN: ambiguous hash-to-cluster assignments detected: $ambiguous_hash_cluster; falling back to unfiltered BLAST input" >&2
  emit_decision "use_unfiltered" "ambiguous_hash_fallback_unfiltered"
  exit 0
fi

if [ "$clstr_records" -eq 0 ]; then
  case "$no_clusters_policy" in
    fail)
      echo "no OTU cluster member records available (clstr_records=0)" >&2
      exit 5
      ;;
    fallback_unfiltered)
      emit_decision "use_unfiltered" "no_clusters_fallback_unfiltered"
      exit 0
      ;;
    allow_empty)
      emit_decision "use_filtered" "no_clusters_allow_empty"
      exit 0
      ;;
  esac
fi

if [ "$total_otus" -eq 0 ]; then
  case "$no_clusters_policy" in
    fail)
      echo "no assignable OTUs after clustering/map reconciliation (total_otus=0)" >&2
      exit 5
      ;;
    fallback_unfiltered)
      emit_decision "use_unfiltered" "no_assignable_otus_fallback_unfiltered"
      exit 0
      ;;
    allow_empty)
      emit_decision "use_filtered" "no_assignable_otus_allow_empty"
      exit 0
      ;;
  esac
fi

if awk -v frac="$missing_frac" -v thr="$missing_max_frac" 'BEGIN{exit !(frac>thr)}'; then
  echo "missing fraction $missing_frac exceeds threshold $missing_max_frac" >&2
  exit 6
fi

if [ "$reads_missing" -gt 0 ]; then
  emit_decision "use_filtered" "threshold_pass_with_missing"
else
  emit_decision "use_filtered" "threshold_pass_no_missing"
fi
exit 0
