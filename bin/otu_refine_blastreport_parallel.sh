#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    echo "Usage: otu_refine_blastreport_parallel.sh BLAST_REPORT CLSTR LINEAGE_FILE [THREADS]" >&2
    exit 1
fi

BLAST_REPORT="$1"
CLSTR_FILE="$2"
LINEAGE_FILE="$3"
THREADS="${4:-1}"

ROUND_ID="${OTU_REFINE_ROUND_ID:-unknown}"
PHASE_TIMINGS_FILE="${OTU_REFINE_PHASE_TIMINGS_FILE:-otu_refine_phase_timings.tsv}"
PHASE_TIMINGS_MS_FILE="${OTU_REFINE_PHASE_TIMINGS_MS_FILE:-otu_refine_phase_timings_ms.tsv}"
WORKLOAD_STATS_FILE="${OTU_REFINE_WORKLOAD_STATS_FILE:-otu_refine_workload_stats.tsv}"
DEBUG_DIR="${OTU_REFINE_DEBUG_DIR:-}"

if ! printf 'round_barcode\tphase\tseconds\n' > "$PHASE_TIMINGS_FILE" 2>/dev/null; then
    :
fi
if ! printf 'round_barcode\tphase\tseconds\tms\n' > "$PHASE_TIMINGS_MS_FILE" 2>/dev/null; then
    :
fi

now_ms() {
    local stamp="${EPOCHREALTIME:-}"
    if [ -n "$stamp" ]; then
        local sec="${stamp%.*}"
        local frac="${stamp#*.}"
        frac="${frac}000"
        printf '%s\n' "$(( 10#$sec * 1000 + 10#${frac:0:3} ))"
        return
    fi
    perl -MTime::HiRes=time -e 'print int((time()*1000)+0.5), "\n"'
}

append_phase() {
    local phase="$1"
    local start_ms="$2"
    local end_ms="$3"
    local elapsed_ms=0
    local seconds=0
    if [[ "$start_ms" =~ ^[0-9]+$ ]] && [[ "$end_ms" =~ ^[0-9]+$ ]] && [ "$end_ms" -ge "$start_ms" ]; then
        elapsed_ms=$(( end_ms - start_ms ))
        seconds=$(( elapsed_ms / 1000 ))
    fi
    {
        printf '%s\t%s\t%s\n' "$ROUND_ID" "$phase" "$seconds"
    } >> "$PHASE_TIMINGS_FILE" 2>/dev/null || true
    {
        printf '%s\t%s\t%s\t%s\n' "$ROUND_ID" "$phase" "$seconds" "$elapsed_ms"
    } >> "$PHASE_TIMINGS_MS_FILE" 2>/dev/null || true
}

write_workload_stats() {
    local cluster_count="$1"
    local cluster_records="$2"
    local blastreport_rows="$3"
    local cluster_taxids_rows="$4"
    local worker_count="$5"
    local shard_count="$6"
    local shard_scheduler_mode="$7"
    local target_records_per_shard="$8"
    local smallest_shard_records="$9"
    local median_shard_records="${10}"
    local largest_shard_records="${11}"
    local largest_shard_fraction="${12}"
    local max_single_cluster_records="${13}"
    local max_single_cluster_fraction="${14}"
    local merged_pairs_rows="${15}"
    local merged_pairs_bytes="${16}"
    local annotated_rows="${17}"
    local annotated_bytes="${18}"
    local annotated_file_bytes="${19}"
    if ! {
        printf 'key\tvalue\n'
        printf 'cluster_count\t%s\n' "$cluster_count"
        printf 'cluster_records\t%s\n' "$cluster_records"
        printf 'blastreport_rows\t%s\n' "$blastreport_rows"
        printf 'cluster_taxids_rows\t%s\n' "$cluster_taxids_rows"
        printf 'worker_count\t%s\n' "$worker_count"
        printf 'shard_count\t%s\n' "$shard_count"
        printf 'shard_scheduler_mode\t%s\n' "$shard_scheduler_mode"
        printf 'target_records_per_shard\t%s\n' "$target_records_per_shard"
        printf 'smallest_shard_records\t%s\n' "$smallest_shard_records"
        printf 'median_shard_records\t%s\n' "$median_shard_records"
        printf 'largest_shard_records\t%s\n' "$largest_shard_records"
        printf 'largest_shard_fraction\t%s\n' "$largest_shard_fraction"
        printf 'max_single_cluster_records\t%s\n' "$max_single_cluster_records"
        printf 'max_single_cluster_fraction\t%s\n' "$max_single_cluster_fraction"
        printf 'merged_pairs_rows\t%s\n' "$merged_pairs_rows"
        printf 'merged_pairs_bytes\t%s\n' "$merged_pairs_bytes"
        # annotated_rows counts only data rows; annotated_file_bytes includes the header.
        printf 'annotated_rows\t%s\n' "$annotated_rows"
        printf 'annotated_bytes\t%s\n' "$annotated_bytes"
        printf 'annotated_file_bytes\t%s\n' "$annotated_file_bytes"
    } > "$WORKLOAD_STATS_FILE" 2>/dev/null; then
        :
    fi
}

blastreport_rows=0
cluster_count=0
cluster_records=0
cluster_taxids_rows=0
worker_count=0
shard_count=0
target_records_per_shard=0
smallest_shard_records=0
median_shard_records=0
largest_shard_records=0
largest_shard_fraction=0
max_single_cluster_records=0
max_single_cluster_fraction=0
shard_scheduler_mode="equal_record_count"
merged_pairs_rows=0
merged_pairs_bytes=0
annotated_rows=0
annotated_bytes=0
annotated_file_bytes=0
_t_wrapper_total_start=$(now_ms)

if [ ! -s "$BLAST_REPORT" ] || [ ! -s "$CLSTR_FILE" ]; then
    write_workload_stats "$cluster_count" "$cluster_records" "$blastreport_rows" "$cluster_taxids_rows" "$worker_count" "$shard_count" "$shard_scheduler_mode" "$target_records_per_shard" "$smallest_shard_records" "$median_shard_records" "$largest_shard_records" "$largest_shard_fraction" "$max_single_cluster_records" "$max_single_cluster_fraction" "$merged_pairs_rows" "$merged_pairs_bytes" "$annotated_rows" "$annotated_bytes" "$annotated_file_bytes"
    exit 0
fi

if [ ! -e "$LINEAGE_FILE" ]; then
    write_workload_stats "$cluster_count" "$cluster_records" "$blastreport_rows" "$cluster_taxids_rows" "$worker_count" "$shard_count" "$shard_scheduler_mode" "$target_records_per_shard" "$smallest_shard_records" "$median_shard_records" "$largest_shard_records" "$largest_shard_fraction" "$max_single_cluster_records" "$max_single_cluster_fraction" "$merged_pairs_rows" "$merged_pairs_bytes" "$annotated_rows" "$annotated_bytes" "$annotated_file_bytes"
    printf '#seq_id\ttax_id\tlineage\n'
    exit 0
fi

if [[ "$THREADS" == *[!0-9]* ]] || [ "$THREADS" -lt 1 ]; then
    THREADS=1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

blastreport_rows="$(awk 'NF{c++} END{print c+0}' "$BLAST_REPORT")"
cluster_count="$(grep -c '^>Cluster ' "$CLSTR_FILE" || true)"
if [[ "$cluster_count" == *[!0-9]* ]] || [ "$cluster_count" -lt 1 ]; then
    write_workload_stats 0 0 "$blastreport_rows" 0 0 0 "$shard_scheduler_mode" 0 0 0 0 0 0 0 0 0 0 0 0
    exit 0
fi

worker_count="$THREADS"
if [ "$cluster_count" -lt "$worker_count" ]; then
    worker_count="$cluster_count"
fi
[ "$worker_count" -lt 1 ] && worker_count=1

tmp_root="$(mktemp -d "${TMPDIR:-/tmp}/rtbioscan_otu_refine.XXXXXX")"
cleanup() {
    rm -rf "$tmp_root"
}
trap cleanup EXIT

cluster_taxids="$tmp_root/cluster_taxids.tsv"
cluster_sizes="$tmp_root/cluster_sizes.tsv"
# Keep the taxid-choice pass single-source-of-truth and validate it before
# fan-out. This preserves serial behavior even though the member expansion is
# sharded afterward.
OTU_REFINE_PHASE_TIMINGS_MS_FILE="$PHASE_TIMINGS_MS_FILE" \
perl -MFindBin -I"${SCRIPT_DIR}/lib" -MRTBioScan::OTURefineBlastreport=write_cluster_taxids_with_phase_timings \
    -e 'write_cluster_taxids_with_phase_timings($ARGV[0], $ARGV[1], $ARGV[2], $ARGV[3], $ARGV[4], $ARGV[5])' \
    "$BLAST_REPORT" \
    "$CLSTR_FILE" \
    "$cluster_taxids" \
    "$cluster_sizes" \
    "$PHASE_TIMINGS_FILE" \
    "$ROUND_ID"

_t_cluster_validation_start=$(now_ms)
observed_clusters="$(grep -cve '^[[:space:]]*$' "$cluster_taxids" || true)"
observed_cluster_sizes="$(grep -cve '^[[:space:]]*$' "$cluster_sizes" || true)"
if [[ "$observed_clusters" == *[!0-9]* ]] || [ "$observed_clusters" -ne "$cluster_count" ] || \
   [[ "$observed_cluster_sizes" == *[!0-9]* ]] || [ "$observed_cluster_sizes" -ne "$cluster_count" ]; then
    echo "ERROR: cluster_taxids.tsv is incomplete (${observed_clusters}/${cluster_count})" >&2
    exit 1
fi
cluster_taxid_ids="$tmp_root/cluster_taxids.ids"
cluster_size_ids="$tmp_root/cluster_sizes.ids"
cut -f1 "$cluster_taxids" > "$cluster_taxid_ids"
cut -f1 "$cluster_sizes" > "$cluster_size_ids"
if ! cmp -s "$cluster_taxid_ids" "$cluster_size_ids"; then
    echo "ERROR: cluster_sizes.tsv does not match cluster_taxids.tsv cluster IDs/order" >&2
    exit 1
fi
_t_cluster_validation_end=$(now_ms)
append_phase "cluster_taxid_validation" "$_t_cluster_validation_start" "$_t_cluster_validation_end"
cluster_taxids_rows="$observed_clusters"

while IFS=$'\t' read -r cluster_id record_count; do
    [ -n "$cluster_id" ] || continue
    if [[ -z "$record_count" || "$record_count" == *[!0-9]* ]]; then
        echo "ERROR: invalid record count '$record_count' for cluster $cluster_id in cluster_sizes.tsv" >&2
        exit 1
    fi
    cluster_records=$(( cluster_records + record_count ))
    if [ "$record_count" -gt "$max_single_cluster_records" ]; then
        max_single_cluster_records="$record_count"
    fi
done < "$cluster_sizes"

if [ "$cluster_records" -gt 0 ]; then
    max_single_cluster_fraction="$(awk -v n="$max_single_cluster_records" -v d="$cluster_records" 'BEGIN{ if(d>0){ printf "%.6f", n/d } else { print "0" } }')"
fi

shard_manifest="$tmp_root/shard_manifest.tsv"
printf 'shard_index\tstart_cluster\tend_cluster\tcluster_count\trecord_count\tshard_path\n' > "$shard_manifest"
_t_shard_plan_start=$(now_ms)
target_records_per_shard=$(( (cluster_records + worker_count - 1) / worker_count ))
cluster_idx=0
current_shard=0
current_start=""
current_end=""
current_clusters=0
current_records=0
running_shard_records=0
while IFS=$'\t' read -r cluster_id record_count; do
    [ -n "$cluster_id" ] || continue
    cluster_idx=$(( cluster_idx + 1 ))
    _start_new=0
    if [ "$current_shard" -eq 0 ]; then
        _start_new=1
    elif [ "$current_clusters" -gt 0 ] && \
         [ "$current_shard" -lt "$worker_count" ] && \
         [ $(( running_shard_records + record_count )) -gt "$target_records_per_shard" ]; then
        _start_new=1
    fi
    if [ "$_start_new" -eq 1 ]; then
        if [ "$current_shard" -gt 0 ]; then
            shard_path="$tmp_root/shard_$(printf '%06d' "$current_shard").clstr"
            printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$current_shard" "$current_start" "$current_end" "$current_clusters" "$current_records" "$shard_path" >> "$shard_manifest"
        fi
        current_shard=$(( current_shard + 1 ))
        current_start="$cluster_id"
        current_end="$cluster_id"
        current_clusters=1
        current_records="$record_count"
        running_shard_records="$record_count"
        continue
    fi
    current_end="$cluster_id"
    current_clusters=$(( current_clusters + 1 ))
    current_records=$(( current_records + record_count ))
    running_shard_records=$(( running_shard_records + record_count ))
done < "$cluster_sizes"

if [ "$current_shard" -gt 0 ]; then
    shard_path="$tmp_root/shard_$(printf '%06d' "$current_shard").clstr"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$current_shard" "$current_start" "$current_end" "$current_clusters" "$current_records" "$shard_path" >> "$shard_manifest"
fi
_t_shard_plan_end=$(now_ms)
append_phase "shard_plan" "$_t_shard_plan_start" "$_t_shard_plan_end"

shards=()
shard_record_values=()
_t_shard_materialize_start=$(now_ms)
awk -F'\t' '
    NR==FNR {
        if (FNR == 1) next;
        idx++;
        start[idx] = $2 + 0;
        end[idx] = $3 + 0;
        path[idx] = $6;
        next;
    }
    /^>Cluster / {
        cluster_id = $0;
        sub(/^>Cluster /, "", cluster_id);
        cluster_id += 0;
        current_file = "";
        if (current_idx < 1) current_idx = 1;
        while (current_idx <= idx && cluster_id > end[current_idx]) {
            current_idx++;
        }
        if (current_idx <= idx && cluster_id >= start[current_idx] && cluster_id <= end[current_idx]) {
            current_file = path[current_idx];
        }
    }
    current_file != "" {
        print >> current_file;
    }
' "$shard_manifest" "$CLSTR_FILE"
_t_shard_materialize_end=$(now_ms)
append_phase "shard_materialize" "$_t_shard_materialize_start" "$_t_shard_materialize_end"

while IFS=$'\t' read -r manifest_idx manifest_start manifest_end manifest_clusters manifest_records manifest_path; do
    [ "$manifest_idx" = "shard_index" ] && continue
    shards+=( "$manifest_path" )
    shard_record_values+=( "$manifest_records" )
    shard_count=$(( shard_count + 1 ))
    if [ "$smallest_shard_records" -eq 0 ] || [ "$manifest_records" -lt "$smallest_shard_records" ]; then
        smallest_shard_records="$manifest_records"
    fi
    if [ "$manifest_records" -gt "$largest_shard_records" ]; then
        largest_shard_records="$manifest_records"
    fi
done < "$shard_manifest"

if [ "$shard_count" -lt 1 ]; then
    echo "ERROR: failed to generate OTU-refine shards" >&2
    exit 1
fi
if [ "$shard_count" -lt "$worker_count" ]; then
    worker_count="$shard_count"
fi
if [ "$cluster_records" -gt 0 ]; then
    largest_shard_fraction="$(awk -v n="$largest_shard_records" -v d="$cluster_records" 'BEGIN{ if(d>0){ printf "%.6f", n/d } else { print "0" } }')"
fi
if [ "${#shard_record_values[@]}" -gt 0 ]; then
    sorted_records=( $(printf '%s\n' "${shard_record_values[@]}" | LC_ALL=C sort -n) )
    sorted_count="${#sorted_records[@]}"
    if [ $(( sorted_count % 2 )) -eq 1 ]; then
        median_shard_records="${sorted_records[$(( sorted_count / 2 ))]}"
    else
        lower_idx=$(( sorted_count / 2 - 1 ))
        upper_idx=$(( sorted_count / 2 ))
        median_shard_records=$(( (sorted_records[lower_idx] + sorted_records[upper_idx]) / 2 ))
    fi
fi

if [ -n "$DEBUG_DIR" ]; then
    mkdir -p "$DEBUG_DIR" 2>/dev/null || true
    cp "$cluster_taxids" "$DEBUG_DIR/cluster_taxids.tsv" 2>/dev/null || true
    cp "$cluster_sizes" "$DEBUG_DIR/cluster_sizes.tsv" 2>/dev/null || true
    cp "$shard_manifest" "$DEBUG_DIR/shard_manifest.tsv" 2>/dev/null || true
fi

_t_expand_start=$(now_ms)
pids=()
pair_files=()
for shard_path in "${shards[@]}"; do
    shard_name="$(basename "$shard_path" .clstr)"
    pair_path="$tmp_root/${shard_name}.pairs.tsv"
    pair_files+=( "$pair_path" )
    (
        perl "${SCRIPT_DIR}/otu_refine_blastreport_worker.pl" \
            "$cluster_taxids" \
            "$shard_path" \
            > "$pair_path"
    ) &
    pids+=( "$!" )
done

worker_failed=0
for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
        worker_failed=1
    fi
done
if [ "$worker_failed" -ne 0 ]; then
    exit 1
fi
_t_expand_end=$(now_ms)
append_phase "shard_expand_workers" "$_t_expand_start" "$_t_expand_end"

_t_pair_merge_start=$(now_ms)
merged_pairs="$tmp_root/merged_pairs.tsv"
: > "$merged_pairs"
for pair_path in "${pair_files[@]}"; do
    cat "$pair_path" >> "$merged_pairs"
done
_t_pair_merge_end=$(now_ms)
append_phase "pair_merge" "$_t_pair_merge_start" "$_t_pair_merge_end"
merged_pairs_rows="$(awk 'NF{c++} END{print c+0}' "$merged_pairs")"
merged_pairs_bytes="$(wc -c < "$merged_pairs" | tr -d ' ')"

export OTU_REFINE_PHASE_TIMINGS_FILE="$PHASE_TIMINGS_FILE"
export OTU_REFINE_PHASE_TIMINGS_MS_FILE="$PHASE_TIMINGS_MS_FILE"
export OTU_REFINE_ROUND_ID="$ROUND_ID"
annotate_stats_file="$tmp_root/annotate_stats.tsv"
export OTU_REFINE_ANNOTATE_STATS_FILE="$annotate_stats_file"
perl "${SCRIPT_DIR}/otu_refine_blastreport_annotate.pl" "$merged_pairs" "$LINEAGE_FILE"
if [ -s "$annotate_stats_file" ]; then
    while IFS=$'\t' read -r stat_key stat_value; do
        [ "$stat_key" = "key" ] && continue
        case "$stat_key" in
            annotated_rows) annotated_rows="$stat_value" ;;
            annotated_bytes) annotated_bytes="$stat_value" ;;
            annotated_file_bytes) annotated_file_bytes="$stat_value" ;;
        esac
    done < "$annotate_stats_file"
fi
write_workload_stats "$cluster_count" "$cluster_records" "$blastreport_rows" "$cluster_taxids_rows" "$worker_count" "$shard_count" "$shard_scheduler_mode" "$target_records_per_shard" "$smallest_shard_records" "$median_shard_records" "$largest_shard_records" "$largest_shard_fraction" "$max_single_cluster_records" "$max_single_cluster_fraction" "$merged_pairs_rows" "$merged_pairs_bytes" "$annotated_rows" "$annotated_bytes" "$annotated_file_bytes"
_t_wrapper_total_end=$(now_ms)
append_phase "wrapper_total" "$_t_wrapper_total_start" "$_t_wrapper_total_end"
