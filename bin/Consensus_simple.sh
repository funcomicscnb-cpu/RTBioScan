#!/bin/bash

set -euo pipefail

is_uint() {
	[[ "${1:-}" =~ ^[0-9]+$ ]]
}

is_decimal() {
	[[ "${1:-}" =~ ^[0-9]+([.][0-9]+)?$ ]]
}

normalize_bool_01() {
	local value="${1:-}"
	value="${value#"${value%%[![:space:]]*}"}"
	value="${value%"${value##*[![:space:]]}"}"
	value="$(printf '%s' "$value" | tr '[:upper:]' '[:lower:]')"
	case "$value" in
		true) printf '1\n' ;;
		false) printf '0\n' ;;
		*) printf '%s\n' "$value" ;;
	esac
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
validator_helper="$script_dir/lib/validate_phase1_workload.sh"
if [ ! -f "$validator_helper" ] && [ -n "${1:-}" ]; then
	validator_helper="${1%/}/lib/validate_phase1_workload.sh"
fi
source "$validator_helper"
identity_mode="${RTBIOSCAN_EFFECTIVE_IDENTITY_MODE:-collapse}"
identity_mode="$(printf '%s' "$identity_mode" | tr '[:upper:]' '[:lower:]')"

# Minimum reads per OTU to attempt consensus (counted by FASTA headers)
min_reads="${3:-5}"
if ! is_uint "$min_reads"; then
	min_reads=5
fi
# Maximum reads per OTU to use for consensus
max_reads="${4:-50}"
if ! is_uint "$max_reads"; then
	max_reads=50
fi
min_qscore="${5:-15}"
if ! is_decimal "$min_qscore"; then
	min_qscore=15
fi
consolidated_min_qscore="${6:-20}"
if ! is_decimal "$consolidated_min_qscore"; then
	consolidated_min_qscore=20
fi
# Maximum allowed Ns in consensus sequences (exclusive)
max_N="${9:-4}"
if ! is_uint "$max_N"; then
	max_N=4
fi
# Rank-based consensus selection target (fixed to 10 reads)
selection_min=10
frozen_members="${7:-}"
samples_file="samples.txt"
#blast_report="blast_report_full.txt"
blast_report="blast_report_annotated.txt"
sup_reads="${CONSENSUS_SUP_READS:-qced_reads_hq_accumulated.fasta}"
qscore_map="read_qscore.tsv"
supreads_cons="$max_reads"
out_dir="Consensus"
cache_root="$out_dir/.cache"
cache_state_root="${CONSENSUS_CACHE_STATE_ROOT:-}"
consolidated_ids_global="$out_dir/consolidated_consensus_ids.txt"
consolidated_ids_prev="$out_dir/consolidated_consensus_ids.prev"
eligible_counts_file="$out_dir/eligible_pool_counts.tsv"
eligible_size_streak_file="$out_dir/eligible_pool_size_streak.tsv"
mkdir -p "$out_dir"
if [ ! -f "$eligible_counts_file" ]; then
	printf "sample\totu_key\teligible_pool_count\tround_barcode\n" > "$eligible_counts_file"
fi
if [ ! -f "$eligible_size_streak_file" ]; then
	printf "sample\totu_key\tread_id\tround_barcode\n" > "$eligible_size_streak_file"
fi
consolidated_ids_current="$out_dir/consolidated_consensus_ids.current"
consolidated_status="$out_dir/consolidated_ids_status.tsv"
consolidated_otu_keys_global="$out_dir/otu_consolidated_keys.tsv"
consolidated_otu_keys_prev="$out_dir/otu_consolidated_keys.prev.tsv"
consolidated_otu_keys_current="$out_dir/otu_consolidated_keys.current.tsv"
consolidated_otu_keys_drop="$out_dir/otu_consolidated_keys.drop.tsv"
consensus_map="$out_dir/consensus_otu_map.tsv"
lock_summary="$out_dir/otu_lock_summary.tsv"
round_id="${CONSENSUS_ROUND_ID:-NA}"
mkdir -p "$cache_root"
if [ -s "$consolidated_ids_global" ]; then
	cp "$consolidated_ids_global" "$consolidated_ids_prev"
else
	: > "$consolidated_ids_prev"
fi
: > "$consolidated_ids_current"
lock_enabled="${CONSENSUS_LOCK_ENABLED:-1}"
lock_ratio="${CONSENSUS_LOCK_RATIO:-0.1}"
lock_min_cons_reads="${CONSENSUS_LOCK_MIN_CONS_READS:-$selection_min}"
lock_min_stable_rounds="${CONSENSUS_LOCK_MIN_STABLE_ROUNDS:-1}"
lock_revalidate_every_rounds="${CONSENSUS_LOCK_REVALIDATE_EVERY_ROUNDS:-0}"
lock_prev_keys_src="${CONSENSUS_LOCK_KEYS_PREV:-}"
lock_reset_keys="${CONSENSUS_LOCK_RESET_KEYS:-}"
prune_frozen_policy="${CONSENSUS_PRUNE_FROZEN_POLICY:-always}"
prune_unassigned_clusters="${CONSENSUS_PRUNE_UNASSIGNED_CLUSTERS:-0}"
prune_unassigned_grace_rounds="${CONSENSUS_PRUNE_UNASSIGNED_GRACE_ROUNDS:-3}"
prune_unassigned_keep_top="${CONSENSUS_PRUNE_UNASSIGNED_KEEP_TOP:-5}"
prune_unassigned_drop_reads="${CONSENSUS_PRUNE_UNASSIGNED_DROP_READS:-0}"
assigned_ids_list="${CONSENSUS_ASSIGNED_IDS:-}"
round_index_file="${CONSENSUS_ROUND_INDEX_FILE:-}"
lock_enabled="$(normalize_bool_01 "$lock_enabled")"
if [ "$lock_enabled" != "0" ] && [ "$lock_enabled" != "1" ]; then
	lock_enabled=1
fi
if ! is_decimal "$lock_ratio"; then
	lock_ratio=0.1
fi
if ! is_uint "$lock_min_cons_reads"; then
	lock_min_cons_reads="$selection_min"
fi
if ! is_uint "$lock_min_stable_rounds"; then
	lock_min_stable_rounds=1
fi
if [ "$lock_min_stable_rounds" -lt 1 ]; then
	lock_min_stable_rounds=1
fi
if ! is_uint "$lock_revalidate_every_rounds"; then
	lock_revalidate_every_rounds=0
fi
if [ "$prune_frozen_policy" != "always" ] && [ "$prune_frozen_policy" != "until_consolidated" ] && [ "$prune_frozen_policy" != "never" ]; then
	prune_frozen_policy="always"
fi
prune_unassigned_clusters="$(normalize_bool_01 "$prune_unassigned_clusters")"
if [ "$prune_unassigned_clusters" != "0" ] && [ "$prune_unassigned_clusters" != "1" ]; then
	prune_unassigned_clusters=0
fi
prune_unassigned_drop_reads="$(normalize_bool_01 "$prune_unassigned_drop_reads")"
if [ "$prune_unassigned_drop_reads" != "0" ] && [ "$prune_unassigned_drop_reads" != "1" ]; then
	prune_unassigned_drop_reads=0
fi
if ! is_uint "$prune_unassigned_grace_rounds"; then
	prune_unassigned_grace_rounds=3
fi
if ! is_uint "$prune_unassigned_keep_top"; then
	prune_unassigned_keep_top=5
fi
if [ -n "$lock_prev_keys_src" ] && [ -s "$lock_prev_keys_src" ]; then
	cp "$lock_prev_keys_src" "$consolidated_otu_keys_prev"
else
	: > "$consolidated_otu_keys_prev"
fi
lock_reset_list="$out_dir/otu_consolidated_keys.reset.list"
if [ -n "$lock_reset_keys" ]; then
	printf "%s\n" "$lock_reset_keys" | tr ',;' ' ' | tr ' ' '\n' | awk 'NF' > "$lock_reset_list"
else
	: > "$lock_reset_list"
fi
if [ -s "$lock_reset_list" ] && [ -s "$consolidated_otu_keys_prev" ]; then
	awk 'BEGIN{FS=OFS="\t"}
		FNR==NR { r[$1]=1; next }
		{ key=(NF>=2?$2:$1); if (!(key in r)) print }
	' "$lock_reset_list" "$consolidated_otu_keys_prev" > "${consolidated_otu_keys_prev}.tmp" \
		&& mv "${consolidated_otu_keys_prev}.tmp" "$consolidated_otu_keys_prev"
fi
: > "$consolidated_otu_keys_current"
: > "$consolidated_otu_keys_drop"
emitted_consensus_count=0
merged_input_headers_total=0
zero_emit_policy="${CONSENSUS_ZERO_EMIT_POLICY:-warn}"
if [ "$zero_emit_policy" != "warn" ] && [ "$zero_emit_policy" != "fail" ]; then
	zero_emit_policy="warn"
fi
id_mismatch_policy="${CONSENSUS_ID_MISMATCH_POLICY:-warn}"
if [ "$id_mismatch_policy" != "warn" ] && [ "$id_mismatch_policy" != "fail" ]; then
	id_mismatch_policy="warn"
fi
cache_below_min_policy="${CONSENSUS_CACHE_BELOW_MIN_POLICY:-keep}"
if [ "$cache_below_min_policy" != "keep" ] && [ "$cache_below_min_policy" != "drop" ]; then
	cache_below_min_policy="keep"
fi
id_drop_global_suffix_mode="${CONSENSUS_ID_DROP_GLOBAL_SUFFIX_MODE:-strict}"
if [ "$id_drop_global_suffix_mode" != "strict" ] && [ "$id_drop_global_suffix_mode" != "heuristic" ]; then
	id_drop_global_suffix_mode="strict"
fi
id_mismatch_events=0
printf 'consensus_id\totu_key\tsample\tn_reads\tmin_qscore\tfrozen_flag\tconsolidated_flag\n' > "$consensus_map"
printf 'round_id\tsample\totu_key\tn_cand\tmin_cand\tmin_cand_ok\tis_frozen\tlock_enabled\tcluster_cons_count\tcluster_cons_min\tcluster_noncons_max\tlock_ratio\tratio_threshold\tlock_rule_pass\tstable_count\tmin_stable_rounds\tshould_consolidate\teffective_consolidated\treason\tsource\n' > "$lock_summary"
prune_stats_round="$out_dir/consensus_prune_unassigned_stats.tsv"
printf 'round_id\tsample\totu_key\tprune_requested\tprune_applied\treason\ttotal_clusters\tassigned_clusters\tunassigned_clusters\tkept_unassigned\tdropped_unassigned\tkeep_unassigned_top\n' > "$prune_stats_round"
pruned_unassigned_round="$out_dir/pruned_unassigned_reads_round.list"
if [ "$prune_unassigned_drop_reads" -eq 1 ]; then
	: > "$pruned_unassigned_round"
fi
shopt -s nullglob

# Detect whether cached consensus exists (used to allow cache-only rounds).
cache_has_files=0
cache_files=( "$cache_root"/*/*.consensus.fasta )
if [ "${#cache_files[@]}" -gt 0 ]; then
	cache_has_files=1
elif [ -n "$cache_state_root" ] && [ -d "$cache_state_root" ]; then
	cache_state_files=( "$cache_state_root"/*/*.consensus.fasta )
	[ -f "${cache_state_files[0]:-}" ] && cache_has_files=1
fi

# Optional debug logging for consensus recovery/consolidation diagnostics.
CONS_DEBUG="${CONSENSUS_DEBUG:-0}"
CONS_LOG="$out_dir/consensus_debug.log"
phase_timings_file="$out_dir/consensus_phase_timings.tsv"
phase_timings_raw_file="$out_dir/consensus_phase_timings_raw.tsv"
sample_phase_timings_file="$out_dir/consensus_sample_phase_timings.tsv"
rscript_stats_file="$out_dir/consensus_rscript_stats.tsv"
sample_totals_file="$out_dir/consensus_sample_totals.tsv"
cache_hydration_stats_file="$out_dir/cache_hydration_stats.tsv"
if ! printf 'round_id\tscope\tsample\tphase\tseconds\tms\n' > "$phase_timings_file" 2>/dev/null; then :; fi
if ! printf 'round_id\tscope\tsample\tphase\tseconds\tms\n' > "$phase_timings_raw_file" 2>/dev/null; then :; fi
if ! printf 'round_id\tscope\tsample\tphase\tseconds\tms\n' > "$sample_phase_timings_file" 2>/dev/null; then :; fi
if ! printf 'round_id\tsample\totu_inputs\tinput_headers\tinput_bases\toutput_consensus_records\toutput_consensus_bases\n' > "$rscript_stats_file" 2>/dev/null; then :; fi
if ! printf 'round_id\tsample\totu_inputs\tinput_headers\tinput_bases\toutput_consensus_records\toutput_consensus_bases\n' > "$sample_totals_file" 2>/dev/null; then :; fi
if ! printf 'round_id\tsample\tstate_cache_present\thydrated\trestored_files\tseconds\tms\n' > "$cache_hydration_stats_file" 2>/dev/null; then :; fi
cons_log() {
	if [ "$CONS_DEBUG" = "1" ]; then
		printf "[%s] %s\n" "$(date '+%Y-%m-%dT%H:%M:%S')" "$*" >> "$CONS_LOG"
	fi
}
timing_now() {
	if command -v perl >/dev/null 2>&1; then
		perl -MTime::HiRes=time -e 'print int(time()*1000), "\n"'
	else
		date +%s000
	fi
}
append_timing_row() {
	local file="$1"
	local scope="$2"
	local sample_name="$3"
	local phase="$4"
	local start_ts="$5"
	local end_ts="$6"
	local seconds=0
	local ms=0
	if is_uint "$start_ts" && is_uint "$end_ts" && [ "$end_ts" -ge "$start_ts" ]; then
		ms=$(( end_ts - start_ts ))
		seconds=$(( ms / 1000 ))
	fi
	{
		printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$round_id" "$scope" "$sample_name" "$phase" "$seconds" "$ms"
	} >> "$file" 2>/dev/null || true
}
append_duration_row() {
	local file="$1"
	local scope="$2"
	local sample_name="$3"
	local phase="$4"
	local duration_ms="$5"
	local seconds=0
	if ! is_uint "$duration_ms"; then
		duration_ms=0
	fi
	seconds=$(( duration_ms / 1000 ))
	{
		printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$round_id" "$scope" "$sample_name" "$phase" "$seconds" "$duration_ms"
	} >> "$file" 2>/dev/null || true
}
read_cache_consensus_meta() {
	local cache_cons="$1"
	if [ ! -s "$cache_cons" ]; then
		printf 'NA\tNA\t1\n'
		return 0
	fi
	awk '
		NR == 1 {
			h=$0
			sub(/^>/, "", h)
			n="NA"
			minq="NA"
			frozen="1"
			if (match(h, /\|n=[^|]+/)) {
				n=substr(h, RSTART+3, RLENGTH-3)
			}
			if (match(h, /\|minQ=[^|]+/)) {
				minq=substr(h, RSTART+6, RLENGTH-6)
			}
			if (match(h, /\|frozen=[01]/)) {
				frozen=substr(h, RSTART+8, 1)
			}
			print n "\t" minq "\t" frozen
			exit
		}
	' "$cache_cons"
}
read_selector_prune_stats() {
	local stats_file="$1"
	if [ ! -s "$stats_file" ]; then
		printf '0\t0\tNA\t0\t0\t0\t0\t0\t0\n'
		return 0
	fi
	awk -F'\t' '
		BEGIN {
			req=0; app=0; reason="NA"; total=0; assigned=0;
			unassigned=0; kept=0; dropped=0; keep_top=0
		}
		$1=="prune_requested" { req=$2; next }
		$1=="prune_applied" { app=$2; next }
		$1=="reason" { reason=$2; next }
		$1=="total_clusters" { total=$2; next }
		$1=="assigned_clusters" { assigned=$2; next }
		$1=="unassigned_clusters" { unassigned=$2; next }
		$1=="kept_unassigned" { kept=$2; next }
		$1=="dropped_unassigned" { dropped=$2; next }
		$1=="keep_unassigned_top" { keep_top=$2; next }
		END {
			print req "\t" app "\t" reason "\t" total "\t" assigned "\t" unassigned "\t" kept "\t" dropped "\t" keep_top
		}
	' "$stats_file"
}
cons_seq_hash() {
	local f="$1"
	if [ ! -s "$f" ]; then
		printf ""
		return 0
	fi
	awk '/^>/{next} {gsub(/\r/,""); printf "%s", toupper($0)} END{print ""}' "$f" \
		| perl -MDigest::MD5 -ne 'BEGIN{$d=Digest::MD5->new} $d->add($_); END{print $d->hexdigest}'
}
filter_consolidated_key_tsv_inplace() {
	local file="$1"
	local tmp
	if [ ! -s "$consolidated_otu_keys_drop" ] || [ ! -s "$file" ]; then
		return 0
	fi
	tmp="${file}.tmp"
	awk -v MODE="keys" -v DROP="$consolidated_otu_keys_drop" -v ID_GLOBAL_SUFFIX_MODE="$id_drop_global_suffix_mode" -f "$drop_filter_awk" "$consolidated_otu_keys_drop" "$file" > "$tmp" && mv "$tmp" "$file"
}
filter_consolidated_ids_by_dropped_keys() {
	local in_file="$1"
	local out_file="$2"
	local drop_file="$3"
	if [ ! -s "$in_file" ]; then
		: > "$out_file"
		return 0
	fi
	if [ ! -s "$drop_file" ]; then
		cp "$in_file" "$out_file"
		return 0
	fi
	awk -v MODE="ids" -v DROP="$drop_file" -v ID_GLOBAL_SUFFIX_MODE="$id_drop_global_suffix_mode" -f "$drop_filter_awk" "$drop_file" "$in_file" "$in_file" > "$out_file"
}

_detect_cpu_budget() {
	local _p
	_p=$(sysctl -n hw.perflevel0.physicalcpu 2>/dev/null || true)
	if [ -n "$_p" ] && [ "$_p" -gt 0 ] 2>/dev/null; then
		printf '%s\n' "$_p"
		return
	fi
	local _n
	_n=$(nproc 2>/dev/null || sysctl -n hw.logicalcpu 2>/dev/null || printf '4\n')
	printf '%d\n' $(( _n > 3 ? _n - 2 : 1 ))
}

cons_log "Consensus debug enabled. min_reads=$min_reads max_reads=$max_reads min_qscore=$min_qscore consolidated_min_qscore=$consolidated_min_qscore selection_min=$selection_min max_N=$max_N"
cons_log "Consolidated IDs: prev_exists=$( [ -s "$consolidated_ids_prev" ] && echo yes || echo no )"
cons_log "Policies: zero_emit_policy=$zero_emit_policy id_mismatch_policy=$id_mismatch_policy cache_below_min_policy=$cache_below_min_policy id_drop_global_suffix_mode=$id_drop_global_suffix_mode"

round_index=""
if [ -n "$round_index_file" ] && [ -s "$round_index_file" ] && [ -n "$round_id" ] && [ "$round_id" != "NA" ]; then
	round_index=$(awk -F'\t' -v rb="$round_id" '$1==rb{print $2; exit}' "$round_index_file" 2>/dev/null || true)
fi
if [ -n "$round_index" ] && ! [[ "$round_index" =~ ^[0-9]+$ ]]; then
	round_index=""
fi

prune_unassigned_global_skip=1
prune_unassigned_global_reason="feature_disabled"
if [ "$prune_unassigned_clusters" -eq 1 ]; then
	if [ -z "$round_index" ]; then
		prune_unassigned_global_skip=1
		prune_unassigned_global_reason="missing_round_index"
		echo "WARN: consensus unassigned-cluster prune disabled for round=$round_id (missing round index)" 1>&2
	elif [ "$round_index" -le "$prune_unassigned_grace_rounds" ]; then
		prune_unassigned_global_skip=1
		prune_unassigned_global_reason="within_grace_window"
	else
		prune_unassigned_global_skip=0
		prune_unassigned_global_reason="enabled"
	fi
fi
echo "INFO: consensus_unassigned_prune enabled=$prune_unassigned_clusters skip=$prune_unassigned_global_skip reason=$prune_unassigned_global_reason grace_rounds=$prune_unassigned_grace_rounds round_index=${round_index:-NA} keep_top=$prune_unassigned_keep_top assigned_ids_file=${assigned_ids_list:-NA}" 1>&2

# Normalize consensus read IDs to FASTA IDs:
# - strip whitespace suffix
# - strip trailing OTU token only when it is the last pipe-delimited field
normalize_header_id_stream() {
	awk '{
		id=$0;
		sub(/\r$/, "", id);
		sub(/[[:space:]].*$/, "", id);
		sub(/\|OTUB_[^|[:space:]]+$/, "", id);
		if (id != "") print id;
	}'
}

# Resolve requested IDs to actual headers present in sup_reads, tolerating
# model-token drift (e.g. hac2sup vs hac) while keeping target/tag contracts.
resolve_ids_to_supreads() {
	local in_ids="$1"
	local out_ids="$2"
	local tmp_map="${out_ids}.map.$$"
	resolve_ids_to_supreads_map "$in_ids" "$tmp_map"
	awk -F'\t' 'NF>=2 && !seen[$2]++ { print $2 }' "$tmp_map" > "$out_ids"
	rm -f "$tmp_map"
}

resolve_ids_to_supreads_map() {
	local in_ids="$1"
	local out_map="$2"
	awk -v IDX="$sup_index" '
		function mnorm(m) {
			if (m=="hac2sup" || m=="hac_fixed") return "hac";
			return m;
		}
		function mrank(m, n) {
			n=mnorm(m);
			return (n=="sup"?3:(n=="hac"?2:1));
		}
		function parse_hdr(h, a, n, i, f) {
			n=split(h, a, "|");
			delete P;
			P["uuid"]=(n>=1?a[1]:"");
			P["target"]=(n>=2?a[2]:"");
			P["model"]=(n>=3?a[3]:"");
			P["barcode"]=""; P["adapter"]="";
			for (i=1; i<=n; i++) {
				f=a[i];
				if (f ~ /^barcode=/) P["barcode"]=f;
				else if (f ~ /^adapter=/) P["adapter"]=f;
			}
		}
		function choose(key, hdr, rank) {
			if (key=="") return;
			if (!(key in K_HDR) || rank > K_RANK[key] || (rank == K_RANK[key] && hdr < K_HDR[key])) {
				K_HDR[key]=hdr;
				K_RANK[key]=rank;
			}
		}
		BEGIN{
			FS=OFS="\t";
			while ((getline line < IDX) > 0) {
				if (line == "") continue;
				hdr=line;
				parse_hdr(hdr, A);
				u=P["uuid"]; t=P["target"]; b=P["barcode"]; ad=P["adapter"]; r=mrank(P["model"]);
				EXACT[hdr]=hdr;
				choose(u "|" t "|" b "|" ad, hdr, r);
				choose(u "|" t "|" ad, hdr, r);
				choose(u "|" t, hdr, r);
				choose(u, hdr, r);
			}
			close(IDX);
		}
		{
			id=$0;
			sub(/\r$/, "", id);
			sub(/[[:space:]].*$/, "", id);
			sub(/\|OTUB_[^|[:space:]]+$/, "", id);
			if (id=="") next;
			h="";
			if (id in EXACT) {
				h=EXACT[id];
			} else {
				parse_hdr(id, A);
				u=P["uuid"]; t=P["target"]; b=P["barcode"]; ad=P["adapter"];
				k=u "|" t "|" b "|" ad;
				if (k in K_HDR) h=K_HDR[k];
				else {
					k=u "|" t "|" ad;
					if (k in K_HDR) h=K_HDR[k];
					else {
						k=u "|" t;
						if (k in K_HDR) h=K_HDR[k];
						else if (u in K_HDR) h=K_HDR[u];
					}
				}
			}
			if (h != "" && !seen_req[id]++) print id, h;
		}
	' "$in_ids" > "$out_map"
}

#This needs to be changed!
script_path=$1
Consensus_Rscript="$script_path/Consensus_simple.R"
drop_filter_awk="$script_path/consensus_drop_filter.awk"
adapter_row_filter_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/filter_blast_rows_by_adapter_class.sh"
partition_row_filter_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/partition_blast_rows_by_adapter_class.sh"
cache_sync_script="${CONSENSUS_CACHE_SYNC_SCRIPT:-${script_path%/}/sync_dir_atomic.sh}"
if [ ! -f "$drop_filter_awk" ]; then
	echo "ERROR: consensus drop filter not found at $drop_filter_awk" 1>&2
	exit 1
fi

# Consensus clustering identity (vsearch --id) is provided as a percent (e.g. 98).
# Default is 98 to match the historical hard-coded value (0.98).
consensus_id_pct="${2:-98}"
if ! is_decimal "$consensus_id_pct"; then
	consensus_id_pct=98
fi
consensus_id=$(awk -v p="$consensus_id_pct" 'BEGIN{printf "%.4f", p/100.0}')

frozen_ids_list=""
consensus_reads_mode="${8:-representative}"
if [ "$consensus_reads_mode" != "representative" ] && [ "$consensus_reads_mode" != "cluster_total" ]; then
	echo "ERROR: consensus_reads_mode must be 'representative' or 'cluster_total' (got '$consensus_reads_mode')" 1>&2
	exit 1
fi
# Quick dependency guards for clearer failures
if ! command -v seqkit >/dev/null 2>&1; then
	echo "Error: seqkit not found in PATH (required for consensus selection)" 1>&2
	exit 1
fi
if ! command -v seqtk >/dev/null 2>&1; then
	echo "Error: seqtk not found in PATH (required for consensus read extraction)" 1>&2
	exit 1
fi
if ! command -v vsearch >/dev/null 2>&1; then
	echo "Error: vsearch not found in PATH (required for consensus clustering)" 1>&2
	exit 1
fi
if ! command -v Rscript >/dev/null 2>&1; then
	echo "Error: Rscript not found in PATH (required for consensus)" 1>&2
	exit 1
fi
if ! Rscript -e 'suppressPackageStartupMessages(library(muscle))' >/dev/null 2>&1; then
	echo "Error: R package \"muscle\" is not available in this environment" 1>&2
	exit 1
fi
if [ ! -s "$sup_reads" ] && [ "$cache_has_files" -eq 0 ]; then
	echo "Error: $sup_reads is missing or empty and no cache found (required for consensus read extraction)" 1>&2
	exit 1
fi
if [ ! -s "$sup_reads" ] && [ "$cache_has_files" -eq 1 ]; then
	echo "INFO: cache-only mode (sup_reads empty; using cached consensus)" 1>&2
fi
_t_startup_state_start=$(timing_now)
sup_index="$out_dir/sup_reads_header_index.tsv"
awk '
	/^>/{
		h=substr($0,2);
		sub(/ .*/, "", h);
		if (h != "") print h;
	}
' "$sup_reads" > "$sup_index"
# O1: build .fai index once (same I/O cost as the sup_index scan above, amortised across all
# per-sample extractions below). Allows samtools faidx -r to seek directly to each candidate
# read instead of scanning the whole accumulated FASTA per sample.
if [ -s "$sup_reads" ] && command -v samtools >/dev/null 2>&1; then
	samtools faidx "$sup_reads" 2>/dev/null || true
fi
if [ -n "$frozen_members" ] && [ -f "$frozen_members" ] && [ -s "$frozen_members" ]; then
	frozen_ids_list="$out_dir/frozen_read_ids.list"
	cut -f2 "$frozen_members" | cut -d"|" -f1 | LC_ALL=C sort -u > "$frozen_ids_list"
	cons_log "FROZEN: loaded $(wc -l < "$frozen_ids_list" | tr -d ' ') frozen read IDs"
else
	echo "WARN: frozen_members source missing or empty (path=${frozen_members:-unset}); new consolidations disabled this round, cached consolidated consensuses still reusable" 1>&2
	cons_log "WARN: frozen_members source missing or empty; new consolidations disabled this round"
fi
if [ "$CONS_DEBUG" = "1" ]; then
	if [ -s "$frozen_ids_list" ]; then
		cons_log "Frozen IDs loaded: $(wc -l < "$frozen_ids_list" | tr -d ' ')"
	else
		cons_log "Frozen IDs list empty or missing"
	fi
	if [ ! -s "$qscore_map" ]; then
		cons_log "Qscore map missing or empty: $qscore_map"
	fi
fi
prune_frozen_ids=1
if [ "$prune_frozen_policy" = "until_consolidated" ] || [ "$prune_frozen_policy" = "never" ]; then
	prune_frozen_ids=0
fi

# Scope prefilter (intentional): keep Metazoa+COI and Viridiplantae+ITS2 rows.
# Always write an output file and preserve header when present.
prefilter_status="$out_dir/prefilter_status.tsv"
prefilter_input_rows=0
prefilter_output_rows=0
prefilter_metazoa_coi_rows=0
prefilter_viridiplantae_its2_rows=0
_t_startup_state_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "startup_validation_and_state" "$_t_startup_state_start" "$_t_startup_state_end"
_t_prefilter_partition_start=$(timing_now)
if [ -s "$blast_report" ]; then
	awk '
		BEGIN{
			OFS="\t";
			has_header=0;
			kingdom_col=3;
			marker_col=4;
		}
		NR==1 && $1=="read_id" {
			print;
			has_header=1;
			for (i=1; i<=NF; i++) {
				k=tolower($i);
				if (k=="otu_kingdom" || k=="kingdom") kingdom_col=i;
				else if (k=="barcode_by_homology" || k=="marker" || k=="otu_marker" || k=="target") marker_col=i;
			}
			next
		}
		{
			in_rows++;
			kingdom=(kingdom_col<=NF ? $kingdom_col : "");
			kl=tolower(kingdom);
			if (has_header) {
				ml=tolower(marker_col<=NF ? $marker_col : "");
			} else {
				# blast_report_annotated.txt format: col 1 is read_id|target|model|...|OTUB_N-target
				# The marker (COI/ITS2) is the 2nd pipe-delimited field of the read ID.
				n=split($1, rid_fields, "|");
				ml=tolower(n>=2 ? rid_fields[2] : "");
			}
			is_mc=(kl ~ /metazoa/ && ml ~ /coi/);
			is_vi=(kl ~ /viridiplantae/ && ml ~ /its2/);
			if (is_mc || is_vi) {
				print;
				out_rows++;
				if (is_mc) mc_rows++;
				if (is_vi) vi_rows++;
			}
		}
		END{
			print "prefilter_input_rows\t" (in_rows+0) > stat;
			print "prefilter_output_rows\t" (out_rows+0) > stat;
			print "prefilter_metazoa_coi_rows\t" (mc_rows+0) > stat;
			print "prefilter_viridiplantae_its2_rows\t" (vi_rows+0) > stat;
			if (!has_header) {
				# Keep downstream contracts stable even when input has no header.
				print "read_id\totu_id\totu_kingdom\tbarcode_by_homology" > out_hdr;
			}
		}
	' stat="$prefilter_status" out_hdr="tmp_clean_blast_report_full.txt" "$blast_report" > tmp_clean_blast_report_full.txt.data
	if head -n1 "$blast_report" | awk '$1=="read_id"{exit 0} {exit 1}'; then
		mv tmp_clean_blast_report_full.txt.data tmp_clean_blast_report_full.txt
	else
		cat tmp_clean_blast_report_full.txt tmp_clean_blast_report_full.txt.data > tmp_clean_blast_report_full.txt.tmp
		mv tmp_clean_blast_report_full.txt.tmp tmp_clean_blast_report_full.txt
		rm -f tmp_clean_blast_report_full.txt.data
	fi
else
	printf "read_id\totu_id\totu_kingdom\tbarcode_by_homology\n" > tmp_clean_blast_report_full.txt
	{
		printf "prefilter_input_rows\t0\n"
		printf "prefilter_output_rows\t0\n"
		printf "prefilter_metazoa_coi_rows\t0\n"
		printf "prefilter_viridiplantae_its2_rows\t0\n"
	} > "$prefilter_status"
fi

sample_partition_dir="$out_dir/_sample_partitions"
sample_partition_ok=0
if [ "$identity_mode" = "track" ] && [ ! -f "$partition_row_filter_script" ]; then
	echo "ERROR: partition_blast_rows_by_adapter_class.sh is required in track mode" 1>&2
	exit 1
fi
if [ -f "$partition_row_filter_script" ]; then
	mkdir -p "$sample_partition_dir"
	if bash "$partition_row_filter_script" tmp_clean_blast_report_full.txt "$samples_file" "$sample_partition_dir"; then
		sample_partition_ok=1
	else
		if [ "$identity_mode" = "track" ]; then
			echo "ERROR: partition_blast_rows_by_adapter_class.sh failed in track mode; refusing sample-collapsing fallback" 1>&2
			exit 1
		else
			echo "WARN: partition_blast_rows_by_adapter_class.sh failed; falling back to per-sample blast filtering" 1>&2
			sample_partition_ok=0
		fi
	fi
fi
_t_prefilter_partition_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "prefilter_partition" "$_t_prefilter_partition_start" "$_t_prefilter_partition_end"

# CPU budget and per-sample parallelism
_t_scheduler_setup_start=$(timing_now)
CPU_BUDGET="${CONSENSUS_CPU_BUDGET:-0}"
if ! [ "$CPU_BUDGET" -gt 0 ] 2>/dev/null; then
	CPU_BUDGET=$(_detect_cpu_budget)
fi
n_samples=$(wc -l < "$samples_file")
n_samples=$(( n_samples + 0 ))
_half=$(( CPU_BUDGET / 2 ))
[ "$_half" -lt 1 ] && _half=1
if   [ "$n_samples" -ge "$CPU_BUDGET" ] && [ "$CPU_BUDGET" -gt 1 ]; then
	MAX_JOBS=$CPU_BUDGET; RSCRIPT_WORKERS=1
elif [ "$n_samples" -ge "$_half" ] && [ "$_half" -gt 1 ]; then
	MAX_JOBS=$_half; RSCRIPT_WORKERS=2
elif [ "$n_samples" -ge 2 ]; then
	MAX_JOBS=$n_samples
	RSCRIPT_WORKERS=$(( CPU_BUDGET / n_samples ))
	[ "$RSCRIPT_WORKERS" -lt 1 ] && RSCRIPT_WORKERS=1
else
	MAX_JOBS=1; RSCRIPT_WORKERS=$CPU_BUDGET
fi
export RSCRIPT_WORKERS
# O3: honour explicit RSCRIPT_WORKERS override (params.consensus_workers > 0).
# Allows trading sample-level parallelism for within-sample parallelism:
#   e.g. consensus_workers=2 → MAX_JOBS=CPU/2, each sample's mclapply gets 2 cores.
_rw_override="${CONSENSUS_RSCRIPT_WORKERS:-0}"
if [ "$_rw_override" -gt 0 ] 2>/dev/null; then
	RSCRIPT_WORKERS="$_rw_override"
	MAX_JOBS=$(( CPU_BUDGET / RSCRIPT_WORKERS ))
	[ "$MAX_JOBS" -lt 1 ] && MAX_JOBS=1
	[ "$MAX_JOBS" -gt "$n_samples" ] && MAX_JOBS="$n_samples"
	export RSCRIPT_WORKERS
	cons_log "O3 override: RSCRIPT_WORKERS=$RSCRIPT_WORKERS MAX_JOBS=$MAX_JOBS"
fi
cons_log "CPU_BUDGET=$CPU_BUDGET n_samples=$n_samples MAX_JOBS=$MAX_JOBS RSCRIPT_WORKERS=$RSCRIPT_WORKERS"
_t_scheduler_setup_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "cpu_budget_and_scheduler_setup" "$_t_scheduler_setup_start" "$_t_scheduler_setup_end"
_sample_jobs=()
_t_sample_worker_dispatch_start=$(timing_now)

# Inline equivalent of bin/Best_consensus_addition.sh -- avoids subprocess fork per cluster.
# Usage: _best_consensus_addition <fasta_file> [target_header]
# Reads CONSENSUS_READS_MODE from environment (representative|cluster_total).
_best_consensus_addition() {
	local fasta_file="$1"
	local target_header="${2:-}"
	local _bca_out_dir
	_bca_out_dir=$(dirname "$fasta_file")
	local _bca_cons
	_bca_cons=$(basename "$fasta_file" | awk -F "_" '{print $NF}')
	[ -d "$_bca_out_dir/OriginalReads" ] || mkdir "$_bca_out_dir/OriginalReads"
	local _bca_orig="$_bca_out_dir/OriginalReads/${_bca_cons}_reads.list"
	local _bca_sup="$_bca_out_dir/OriginalReads/${_bca_cons}_reads_sup.fasta"
	local _bca_best_entry="" _bca_best_seq="" _bca_best_otu="" _bca_best_reads=0
	local _bca_max=0 _bca_total=0 _bca_cap_best=0
	target_header="${target_header#>}"
	target_header="${target_header%$'\r'}"
	local _bca_tfound=0 _bca_tent="" _bca_tseq="" _bca_totu="" _bca_treads=0 _bca_cap_t=0
	local _bca_line _bca_hraw _bca_reads _bca_otu
	while IFS= read -r _bca_line; do
		if [[ $_bca_line =~ ^\> ]]; then
			_bca_hraw="${_bca_line#>}"
			_bca_hraw="${_bca_hraw%$'\r'}"
			_bca_reads=$(printf '%s\n' "$_bca_line" | sed -E 's/.*reads-([0-9]+).*/\1/')
			if ! printf '%s\n' "$_bca_reads" | awk 'BEGIN{ok=1} /^[0-9]+$/{next} {ok=0} END{exit ok?0:1}'; then
				_bca_reads=0
			fi
			_bca_otu=""
			if [[ "$_bca_line" =~ \|OTU=([^|]+) ]]; then
				_bca_otu="${BASH_REMATCH[1]}"
			else
				_bca_otu=$(printf '%s\n' "$_bca_line" | tr '|' '\n' | awk '/^OTUB_/{print; exit}')
				[ -z "$_bca_otu" ] && _bca_otu=$(printf '%s\n' "$_bca_line" | cut -d'|' -f2)
			fi
			_bca_total=$((_bca_total + _bca_reads))
			if [ -n "$target_header" ] && [ "$_bca_hraw" = "$target_header" ]; then
				_bca_tfound=1; _bca_totu="$_bca_otu"; _bca_treads="$_bca_reads"
				if [[ "$_bca_line" =~ ^\>([^|]+)\|([^|]+)\|(.*)$ ]]; then
					_bca_tent=">${BASH_REMATCH[1]}|${_bca_cons}|${BASH_REMATCH[3]}"
				else
					_bca_tent=$(printf '%s\n' "$_bca_line" | sed "s/$_bca_otu/$_bca_cons/")
				fi
				_bca_tseq=""; _bca_cap_t=1
			else
				_bca_cap_t=0
			fi
			if (( _bca_reads > _bca_max )) || [[ -z "$_bca_best_entry" ]]; then
				_bca_max=$_bca_reads
				if [[ "$_bca_line" =~ ^\>([^|]+)\|([^|]+)\|(.*)$ ]]; then
					_bca_best_entry=">${BASH_REMATCH[1]}|${_bca_cons}|${BASH_REMATCH[3]}"
				else
					_bca_best_entry=$(printf '%s\n' "$_bca_line" | sed "s/$_bca_otu/$_bca_cons/")
				fi
				_bca_best_seq=""; _bca_cap_best=1; _bca_best_otu="$_bca_otu"; _bca_best_reads=$_bca_reads
			else
				_bca_cap_best=0
			fi
		else
			[[ $_bca_cap_best -eq 1 ]] && _bca_best_seq+="$_bca_line"
			[[ $_bca_cap_t    -eq 1 ]] && _bca_tseq+="$_bca_line"
		fi
	done < "$fasta_file"
	local _bca_sel_otu="$_bca_best_otu" _bca_sel_reads=$_bca_best_reads
	if [ -n "$target_header" ] && [ "$_bca_tfound" -eq 1 ]; then
		_bca_best_entry="$_bca_tent"; _bca_best_seq="$_bca_tseq"
		_bca_sel_otu="$_bca_totu"; _bca_sel_reads=$_bca_treads
	fi
	if [ -n "$_bca_sel_otu" ]; then
		local _bca_mode="${CONSENSUS_READS_MODE:-representative}"
		if [ "$_bca_mode" = "cluster_total" ]; then
			: > "$_bca_orig"
			local _bca_tr="${_bca_orig}.tmp" _bca_ts="${_bca_sup}.tmp"
			: > "$_bca_tr"; : > "$_bca_ts"
			local _bca_hdr
			while IFS= read -r _bca_line; do
				if [[ $_bca_line =~ ^\> ]]; then
					_bca_hdr="$_bca_line"; _bca_otu=""
					if [[ "$_bca_hdr" =~ \|OTU=([^|]+) ]]; then
						_bca_otu="${BASH_REMATCH[1]}"
					else
						_bca_otu=$(printf '%s\n' "$_bca_hdr" | tr '|' '\n' | awk '/^OTUB_/{print; exit}')
						[ -z "$_bca_otu" ] && _bca_otu=$(printf '%s\n' "$_bca_hdr" | cut -d'|' -f2)
					fi
					[ -n "$_bca_otu" ] && [ -f "$_bca_out_dir/${_bca_otu}_all_reads.list"  ] && cat "$_bca_out_dir/${_bca_otu}_all_reads.list"  >> "$_bca_tr"
					[ -n "$_bca_otu" ] && [ -f "$_bca_out_dir/${_bca_otu}_reads_sup.fasta" ] && cat "$_bca_out_dir/${_bca_otu}_reads_sup.fasta" >> "$_bca_ts"
				fi
			done < "$fasta_file"
			[ -s "$_bca_tr" ] && LC_ALL=C sort -u "$_bca_tr" > "$_bca_orig"
			[ -s "$_bca_ts" ] && awk 'BEGIN{RS=">"; ORS=""} NR>1 {h=$1; sub(/\n.*/, "", h); if(!seen[h]++){print ">"$0}}' "$_bca_ts" > "$_bca_sup"
			rm -f "$_bca_tr" "$_bca_ts"
		else
			[ -f "$_bca_out_dir/${_bca_sel_otu}_all_reads.list"  ] && cat "$_bca_out_dir/${_bca_sel_otu}_all_reads.list"  >> "$_bca_orig"
			[ -f "$_bca_out_dir/${_bca_sel_otu}_reads_sup.fasta" ] && cat "$_bca_out_dir/${_bca_sel_otu}_reads_sup.fasta" >  "$_bca_sup"
		fi
	fi
	local _bca_rmode="${CONSENSUS_READS_MODE:-representative}"
	local _bca_rval="$_bca_sel_reads"
	[ "$_bca_rmode" = "cluster_total" ] && _bca_rval=$_bca_total
	printf '%s\n%s\n' "$_bca_best_entry" "$_bca_best_seq" | sed -E "s/(reads-)[0-9]+/\1$_bca_rval/"
}

hydrate_sample_cache() {
	local sample_name="$1"
	local local_cache_dir="$2"
	local stats_file="$3"
	local state_sample_dir=""
	local state_cache_present=0
	local hydrated=0
	local restored_files=0
	local start_ts end_ts seconds=0 duration_ms=0
	start_ts=$(timing_now)
	mkdir -p "$local_cache_dir"
	if [ -n "$cache_state_root" ]; then
		state_sample_dir="$cache_state_root/$sample_name"
		if [ -d "$state_sample_dir" ]; then
			state_cache_present=1
			# Diagnostic scope only: this is the source cache file count, not an exact copied-file count.
			restored_files=$(find "$state_sample_dir" -type f | wc -l | tr -d ' ')
			[ -z "$restored_files" ] && restored_files=0
			if [ ! -f "$cache_sync_script" ]; then
				end_ts=$(timing_now)
				if is_uint "$start_ts" && is_uint "$end_ts" && [ "$end_ts" -ge "$start_ts" ]; then
					duration_ms=$(( end_ts - start_ts ))
					seconds=$(( duration_ms / 1000 ))
				fi
				printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$round_id" "$sample_name" "$state_cache_present" "$hydrated" "$restored_files" "$seconds" "$duration_ms" > "$stats_file" 2>/dev/null || true
				echo "ERROR: consensus cache sync script not found at $cache_sync_script for sample=$sample_name" 1>&2
				return 1
			fi
			if bash "$cache_sync_script" "$state_sample_dir" "$local_cache_dir"; then
				hydrated=1
			else
				end_ts=$(timing_now)
				if is_uint "$start_ts" && is_uint "$end_ts" && [ "$end_ts" -ge "$start_ts" ]; then
					duration_ms=$(( end_ts - start_ts ))
					seconds=$(( duration_ms / 1000 ))
				fi
				printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$round_id" "$sample_name" "$state_cache_present" "$hydrated" "$restored_files" "$seconds" "$duration_ms" > "$stats_file" 2>/dev/null || true
				echo "ERROR: failed to hydrate consensus cache for sample=$sample_name from $state_sample_dir" 1>&2
				return 1
			fi
		fi
	fi
	end_ts=$(timing_now)
	if [ "$state_cache_present" -eq 1 ] && is_uint "$start_ts" && is_uint "$end_ts" && [ "$end_ts" -ge "$start_ts" ]; then
		duration_ms=$(( end_ts - start_ts ))
		seconds=$(( duration_ms / 1000 ))
	else
		seconds=0
		duration_ms=0
	fi
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$round_id" "$sample_name" "$state_cache_present" "$hydrated" "$restored_files" "$seconds" "$duration_ms" > "$stats_file" 2>/dev/null || true
	return 0
}

#Consensus loop
while IFS= read -r sample; do
	(
	if [ ! -e "$out_dir/$sample" ]; then mkdir -p "$out_dir/$sample"; fi
	sample_timing_file="$out_dir/$sample/_timings.tmp"
	sample_cache_hydration_file="$out_dir/$sample/_cache_hydration.tmp"
	: > "$sample_timing_file"
	_t_sample_total_start=$(timing_now)
	consolidated_ids_current="$out_dir/$sample/_consolidated_ids.tmp"
	consolidated_otu_keys_current="$out_dir/$sample/_otu_keys_current.tmp"
	eligible_counts_file="$out_dir/$sample/_eligible_counts.tmp"
	eligible_size_streak_file="$out_dir/$sample/_eligible_size_streak.tmp"
	pruned_unassigned_round="$out_dir/$sample/_pruned_unassigned.tmp"
	prune_stats_round="$out_dir/$sample/_prune_stats.tmp"
	lock_summary="$out_dir/$sample/_lock_summary.tmp"
	consensus_map="$out_dir/$sample/_consensus_map.tmp"
	: > "$consolidated_ids_current"
	: > "$consolidated_otu_keys_current"
	: > "$eligible_counts_file"
	: > "$eligible_size_streak_file"
	: > "$pruned_unassigned_round"
	: > "$prune_stats_round"
	: > "$lock_summary"
	: > "$consensus_map"
	emitted_consensus_count=0
	merged_input_headers_total=0
	id_mismatch_events=0
	#echo "Processing sample $sample"
		sample_blast="$out_dir/$sample/${sample}_blast_report.txt"
		sample_parsed="$out_dir/$sample/${sample}_blast_report.parsed.tsv"
		otu_list="$out_dir/$sample/${sample}_otu_keys.list"
		sample_otu_dir="$out_dir/$sample/otu_rows"
		sample_otu_resolved_dir="$out_dir/$sample/otu_rows_resolved"
		sample_cache_dir="$cache_root/$sample"
	if ! hydrate_sample_cache "$sample" "$sample_cache_dir" "$sample_cache_hydration_file"; then
		exit 1
	fi
	lock_state_file="$sample_cache_dir/lock_state.tsv"
	lock_state_prev="$sample_cache_dir/lock_state.prev.tsv"
	lock_state_current="$sample_cache_dir/lock_state.current.tsv"
	if [ -s "$lock_state_file" ]; then
		cp "$lock_state_file" "$lock_state_prev"
	else
		: > "$lock_state_prev"
	fi
	if [ -s "$lock_reset_list" ] && [ -s "$lock_state_prev" ]; then
		awk 'BEGIN{FS=OFS="\t"}
			FNR==NR { r[$1]=1; next }
			NF>=1 { if (!($1 in r)) print }
		' "$lock_reset_list" "$lock_state_prev" > "${lock_state_prev}.tmp" \
			&& mv "${lock_state_prev}.tmp" "$lock_state_prev"
	fi
	: > "$lock_state_current"
	revalidate_counter_file="$sample_cache_dir/revalidate_counter.txt"
	revalidate_counter_prev=0
	if [ -s "$revalidate_counter_file" ]; then
		revalidate_counter_prev=$(awk 'BEGIN{v=0} /^[0-9]+$/{v=$1} END{print v+0}' "$revalidate_counter_file")
	fi
	revalidate_counter=$((revalidate_counter_prev + 1))
	printf "%s\n" "$revalidate_counter" > "$revalidate_counter_file"
	revalidate_due_sample=0
	if [ "$lock_revalidate_every_rounds" -gt 0 ]; then
		if [ $((revalidate_counter % lock_revalidate_every_rounds)) -eq 0 ]; then
			revalidate_due_sample=1
		fi
	fi
	revalidate_before="$out_dir/$sample/revalidate_before.tsv"
	: > "$revalidate_before"
	cached_list="$out_dir/$sample/cached_consensus.list"
	recompute_list="$out_dir/$sample/otu_recompute.tsv"
	consolidated_keys="$out_dir/$sample/consolidated_otu_keys.list"
	locked_keys_sample="$out_dir/$sample/locked_otu_keys.list"
	sample_meta="$out_dir/$sample/otu_meta.tsv"
	otu_runtime_plan="$out_dir/$sample/${sample}_otu_runtime_plan.tsv"
	carry_forward_state="$out_dir/$sample/${sample}_carry_forward_state.tsv"
		: > "$cached_list"
		: > "$recompute_list"
		: > "$consolidated_keys"
		: > "$sample_meta"
		: > "$otu_runtime_plan"
		: > "$carry_forward_state"
		fallback_empty_sample_blast_count=0
		worker_failures_selector=0
		if [ "$lock_enabled" -eq 1 ] && [ -s "$consolidated_otu_keys_prev" ]; then
			awk -v s="$sample" 'BEGIN{FS=OFS="\t"}
				NF==1 { print $1; next }
				NF>=2 && $1==s { print $2 }
			' "$consolidated_otu_keys_prev" | LC_ALL=C sort -u > "$locked_keys_sample"
		else
			: > "$locked_keys_sample"
		fi
		union_cand_ids="$out_dir/$sample/_union_cand_ids.list"
		cand_id_otu_map="$out_dir/$sample/_cand_id_otu_map.tsv"
		union_cand_reads="$out_dir/$sample/_union_cand_reads.fasta"
		processed_otu_keys="$out_dir/$sample/_processed_otu_keys.list"
	: > "$union_cand_ids"
	: > "$cand_id_otu_map"
	: > "$union_cand_reads"
	: > "$processed_otu_keys"
	_t_filter_parse_start=$(timing_now)
	# Keep the per-sample split helper isolated so a filter failure is visible
	# without aborting the whole consensus round.
	filter_stderr="$out_dir/$sample/_sample_blast_filter.stderr"
	sample_blast_cleanup=1
	if [ "$sample_partition_ok" -eq 1 ] && [ -f "$sample_partition_dir/${sample}.blast.tsv" ]; then
		sample_blast="$sample_partition_dir/${sample}.blast.tsv"
		sample_blast_cleanup=0
	else
		if [ "$identity_mode" = "track" ] && [ "$sample_partition_ok" -eq 1 ]; then
			echo "ERROR: track mode partitioning did not produce expected per-unit blast partition for sample=$sample" 1>&2
			exit 1
		fi
		if bash "$adapter_row_filter_script" \
			tmp_clean_blast_report_full.txt sample "$sample" > "$sample_blast" 2>"$filter_stderr"; then
			:
		else
			filter_status=$?
			filter_err="$(tr '\n' ' ' < "$filter_stderr" | sed -E 's/[[:space:]]+/ /g; s/^ //; s/ $//')"
			fallback_empty_sample_blast_count=$((fallback_empty_sample_blast_count + 1))
			if [ -n "$filter_err" ]; then
				echo "WARN: filter_blast_rows_by_adapter_class.sh failed for sample=$sample exit_code=$filter_status stderr=$filter_err; continuing with empty sample_blast" 1>&2
			else
				echo "WARN: filter_blast_rows_by_adapter_class.sh failed for sample=$sample exit_code=$filter_status; continuing with empty sample_blast" 1>&2
			fi
			echo "METRIC: fallback_empty_sample_blast_count=$fallback_empty_sample_blast_count sample=$sample" 1>&2
			: > "$sample_blast"
		fi
	fi
	rm -f "$filter_stderr"
	awk 'BEGIN{FS=OFS="\t"}
		$1=="read_id" { next }
		{
			rid=$1;
			if (rid=="") next;
			otu=""; bc=""; ad=""; model="";
			n=split(rid, t, "|");
			if (n>=3) model=t[3];
			for (i=1; i<=n; i++) {
				if (t[i] ~ /^OTUB_[^|[:space:]]+$/) otu=t[i];
				else if (t[i] ~ /^barcode=/) { bc=t[i]; sub(/^barcode=/, "", bc); }
				else if (t[i] ~ /^adapter=/) { ad=t[i]; sub(/^adapter=/, "", ad); }
			}
			# Fallback for older blast row formats where OTU token is not present in read_id.
			if (otu=="" && NF>=2 && $2 ~ /^OTUB_[^|[:space:]]+$/) otu=$2;
			if (otu=="") next;
			print rid, otu, bc, ad, model;
		}
	' "$sample_blast" > "$sample_parsed"
	sample_parsed_rows=$(awk 'NF{c++} END{print c+0}' "$sample_parsed")
	_t_filter_parse_end=$(timing_now)
	append_timing_row "$sample_timing_file" "sample" "$sample" "blast_filter_parse" "$_t_filter_parse_start" "$_t_filter_parse_end"
	if [ "$CONS_DEBUG" = "1" ]; then
		sb_lines=$(wc -l < "$sample_blast" | tr -d ' ')
		cons_log "Sample=$sample blast_rows=$sb_lines parsed_rows=$sample_parsed_rows"
	fi
	#echo "grep -E \"adapter=${sample}_[0-9]\" tmp_clean_blast_report_full.txt > $sample_blast"
		if [ "$sample_parsed_rows" -ne 0 ];then
			_t_otu_prepare_start=$(timing_now)
			echo "Generating consensus for sample $sample"
				mkdir -p "$sample_otu_dir" "$sample_otu_resolved_dir"
			awk -v dir="$sample_otu_dir" -v min="$min_reads" 'BEGIN{FS=OFS="\t"}
			function make_key(otu, bc, k) {
				k=otu;
				if (bc != "" && bc != "NA" && k !~ ("-" bc "$")) k=k "-" bc;
				return k;
			}
			{
				k=make_key($2, $3);
				c[k]++;
				print > (dir "/" k ".tsv");
			}
				END{
					for (k in c) if (c[k] >= min) print k;
				}
			' "$sample_parsed" | LC_ALL=C sort > "$otu_list"
			sample_requested_ids="$out_dir/$sample/_requested_ids.list"
			sample_requested_uuids="$out_dir/$sample/_requested_uuids.list"
			sample_resolved_map="$out_dir/$sample/_resolved_id_map.tsv"
			sample_qscore_map="$out_dir/$sample/_qscore_map.tsv"
			awk '{
				id=$1;
				sub(/\r$/, "", id);
				sub(/[[:space:]].*$/, "", id);
				sub(/\|OTUB_[^|[:space:]]+$/, "", id);
				if (id != "" && !seen[id]++) print id;
			}' "$sample_parsed" > "$sample_requested_ids"
			awk -F'|' 'NF>=1 && $1 != "" && !seen[$1]++ { print $1 }' "$sample_requested_ids" > "$sample_requested_uuids"
			resolve_ids_to_supreads_map "$sample_requested_ids" "$sample_resolved_map"
			if [ -s "$qscore_map" ]; then
				awk 'NR==FNR { want[$1]=1; next } ($1 in want)' "$sample_requested_uuids" "$qscore_map" > "$sample_qscore_map"
			else
				: > "$sample_qscore_map"
			fi
			awk -v mapf="$sample_resolved_map" -v dir="$sample_otu_resolved_dir" 'BEGIN{FS=OFS="\t"}
				function make_key(otu, bc, k) {
					k=otu;
					if (bc != "" && bc != "NA" && k !~ ("-" bc "$")) k=k "-" bc;
					return k;
				}
				FILENAME==mapf {
					if (NF>=2) m[$1]=$2;
					next
				}
				{
					req=$1;
					sub(/\r$/, "", req);
					sub(/[[:space:]].*$/, "", req);
					sub(/\|OTUB_[^|[:space:]]+$/, "", req);
					if (req=="" || !(req in m)) next;
					k=make_key($2, $3);
					print m[req], $1, $2, $3, $4, $5 > (dir "/" k ".tsv");
				}
			' "$sample_resolved_map" "$sample_parsed"
			awk -v lockf="$lock_state_prev" -v lockedf="$locked_keys_sample" -v otuf="$otu_list" 'BEGIN{FS=OFS="\t"}
				FILENAME==lockf {
					if (NF>=3) {
						st[$1]=($2 ~ /^[0-9]+$/ ? $2 : 0);
						ps[$1]=($3 ~ /^[0-9]+$/ ? $3 : 0);
					}
					next
				}
				FILENAME==lockedf {
					if (NF>=1 && $1!="") lk[$1]=1;
					next
				}
				FILENAME==otuf {
					if (NF<1 || $1=="") next;
					k=$1;
					print k, ((k in st)?st[k]:0), ((k in ps)?ps[k]:0), ((k in lk)?1:0);
				}
			' "$lock_state_prev" "$locked_keys_sample" "$otu_list" > "$otu_runtime_plan"
			awk -v lockf="$lock_state_prev" -v lockedf="$locked_keys_sample" 'BEGIN{FS=OFS="\t"}
				FILENAME==lockf {
					if (NF>=3) {
						st[$1]=($2 ~ /^[0-9]+$/ ? $2 : 0);
						ps[$1]=($3 ~ /^[0-9]+$/ ? $3 : 0);
					}
					next
				}
				FILENAME==lockedf && NF>=1 && $1!="" {
					k=$1;
					print k, ((k in st)?st[k]:0), ((k in ps)?ps[k]:0);
				}
			' "$lock_state_prev" "$locked_keys_sample" > "$carry_forward_state"
			MAX_SELECTOR_JOBS=$RSCRIPT_WORKERS
		_sel_pids=()
	# O1b: accumulation files for batch pool extraction (defer per-OTU seqtk to post-loop samtools call)
	_union_pool_ids="$out_dir/$sample/_union_pool_ids.list"
		_union_pool_map="$out_dir/$sample/_pool_id_otu_map.tsv"
		_union_pool_reads="$out_dir/$sample/_union_pool_reads.fasta"
		_phase1_workload="$out_dir/$sample/_phase1_workload.tsv"
		_selector_queue="$out_dir/$sample/_selector_queue.tsv"
		: > "$_union_pool_ids"
		: > "$_union_pool_map"
		: > "$_phase1_workload"
		: > "$_selector_queue"
			while IFS=$'\t' read -r otu_key prev_stable_count prev_lock_pass locked_this_otu; do
					[ -n "$otu_key" ] || continue
					raw_otu_rows="$sample_otu_dir/${otu_key}.tsv"
					otu_rows="$sample_otu_resolved_dir/${otu_key}.tsv"
					[ -f "$otu_rows" ] || : > "$otu_rows"
					prev_stable_count=${prev_stable_count:-0}
					prev_lock_pass=${prev_lock_pass:-0}
					locked_this_otu=${locked_this_otu:-0}
				printf "%s\n" "$otu_key" >> "$processed_otu_keys"
			#echo "Processing OTU $otu_key"
			eligible_list="$out_dir/$sample/${otu_key}_eligible.list"
				candidate_list="$out_dir/$sample/${otu_key}_candidate.list"
				cache_meta="$sample_cache_dir/${otu_key}.meta"
				cache_cons="$sample_cache_dir/${otu_key}.consensus.fasta"
				# Fallback: if barcode-specific cache is missing, try barcode-less cache name.
					if [ ! -s "$cache_cons" ]; then
						otu_key_nobc="${otu_key%-*}"
						if [ "$otu_key_nobc" != "$otu_key" ]; then
							cache_cons_alt="$sample_cache_dir/${otu_key_nobc}.consensus.fasta"
							if [ -s "$cache_cons_alt" ]; then
								cache_cons="$cache_cons_alt"
							fi
						fi
					fi
						stable_count=0
						lock_rule_pass=0
						revalidate_this_otu=0
						if [ "$locked_this_otu" -eq 1 ] && [ "$revalidate_due_sample" -eq 1 ] && [ -s "$cache_cons" ]; then
						revalidate_this_otu=1
						old_hash=$(cons_seq_hash "$cache_cons")
						if [ -n "$old_hash" ]; then
							printf "%s\t%s\n" "$otu_key" "$old_hash" >> "$revalidate_before"
						fi
						cons_log "OTU=$otu_key locked_cache_revalidation_triggered round_index=$revalidate_counter every=$lock_revalidate_every_rounds"
					fi
					if [ "$locked_this_otu" -eq 1 ] && [ "$revalidate_this_otu" -eq 0 ] && [ -s "$cache_cons" ]; then
						if [ "$prev_stable_count" -lt "$lock_min_stable_rounds" ]; then
							prev_stable_count="$lock_min_stable_rounds"
						fi
						printf "%s\t%s\t%s\n" "$otu_key" "$prev_stable_count" "1" >> "$lock_state_current"
						IFS=$'\t' read -r cache_n cache_minq cache_frozen < <(read_cache_consensus_meta "$cache_cons")
						printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$otu_key" "$sample" "$cache_n" "$cache_minq" "$cache_frozen" "1" >> "$sample_meta"
						echo "$otu_key" >> "$consolidated_keys"
						printf "%s\t%s\n" "$sample" "$otu_key" >> "$consolidated_otu_keys_current"
						echo "$cache_cons" >> "$cached_list"
						continue
					elif [ "$locked_this_otu" -eq 1 ]; then
						cons_log "OTU=$otu_key locked but cache missing; recomputing"
					fi
					# When the accumulated FASTA is empty (all reads C1-pruned), no new reads can
					# be extracted. Carry forward the cached consensus if it exists; skip this OTU.
					if ! [ -s "$sup_reads" ]; then
						printf "%s\t%s\t%s\n" "$otu_key" "$prev_stable_count" "$prev_lock_pass" >> "$lock_state_current"
						[ -s "$cache_cons" ] && echo "$cache_cons" >> "$cached_list"
						continue
					fi
					# Determine whether OTU is frozen early (used by prune gating and consolidation).
					is_frozen=0
					if [ -n "$frozen_ids_list" ] && [ -s "$otu_rows" ]; then
						if awk 'BEGIN{FS=OFS="\t"}
							FNR==NR { f[$1]=1; next }
							{
								u=$1
								if (index(u,"|")>0) { split(u,a,"|"); u=a[1] }
								if (u in f) { found=1; exit }
							}
							END { exit(found ? 0 : 1) }
						' "$frozen_ids_list" "$otu_rows"; then
							is_frozen=1
						fi
					fi
					if [ "$CONS_DEBUG" = "1" ]; then
						n_all_full=$(awk 'END{print NR+0}' "$otu_rows")
						cons_log "OTU=$otu_key is_frozen=$is_frozen all_full=$n_all_full frozen_ids_list=$( [ -s "$frozen_ids_list" ] && echo yes || echo no )"
					fi
					if [ "$prune_frozen_ids" -eq 1 ] && [ -s "$frozen_ids_list" ]; then
						raw_elig=$(awk 'BEGIN{FS=OFS="\t"}
							FNR==NR { f[$1]=1; next }
							function uuid(id, a) { split(id, a, "|"); return a[1] }
						($5=="sup" || $5=="hac2sup") {
							u=uuid($1);
							if (!(u in f)) c++;
						}
						END{print c+0}
					' "$frozen_ids_list" "$raw_otu_rows")
						awk 'BEGIN{FS=OFS="\t"}
							FNR==NR { f[$1]=1; next }
							function uuid(id, a) { split(id, a, "|"); return a[1] }
						($6=="sup" || $6=="hac2sup") {
							u=uuid($1);
							if (!(u in f)) print $1;
						}
					' "$frozen_ids_list" "$otu_rows" \
							| normalize_header_id_stream \
							| LC_ALL=C sort -u \
							> "$eligible_list"
					else
						raw_elig=$(awk 'BEGIN{FS=OFS="\t"}
							($5=="sup" || $5=="hac2sup") { c++ }
							END{print c+0}
						' "$raw_otu_rows")
						awk 'BEGIN{FS=OFS="\t"}
							($6=="sup" || $6=="hac2sup") { print $1 }
						' "$otu_rows" \
							| normalize_header_id_stream \
							| LC_ALL=C sort -u \
							> "$eligible_list"
				fi
				n_elig=$(wc -l < "$eligible_list" | tr -d ' ')
				if [ "$raw_elig" -gt 0 ] && [ "$n_elig" -lt "$raw_elig" ]; then
					id_mismatch_events=$((id_mismatch_events + 1))
					msg="Consensus eligible ID mismatch: OTU=$otu_key raw_eligible=$raw_elig resolved_eligible=$n_elig"
					if [ "$id_mismatch_policy" = "fail" ] && [ "$n_elig" -eq 0 ]; then
						echo "ERROR: $msg (none resolved in $sup_reads)" 1>&2
						exit 1
					else
						echo "WARN: $msg" 1>&2
					fi
					cons_log "$msg"
				fi
				[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key elig_sup_hac2sup=$n_elig"
				# Fallback: if no SUP/HAC2SUP reads are available, allow HAC/HAC_FIXED reads
				if [ "$n_elig" -lt "$min_reads" ]; then
					if [ "$prune_frozen_ids" -eq 1 ] && [ -s "$frozen_ids_list" ]; then
							raw_elig=$(awk 'BEGIN{FS=OFS="\t"}
								FNR==NR { f[$1]=1; next }
								function uuid(id, a) { split(id, a, "|"); return a[1] }
								($5=="sup" || $5=="hac2sup" || $5=="hac_fixed" || $5=="hac") {
									u=uuid($1);
									if (!(u in f)) c++;
								}
								END{print c+0}
							' "$frozen_ids_list" "$raw_otu_rows")
							awk 'BEGIN{FS=OFS="\t"}
								FNR==NR { f[$1]=1; next }
								function uuid(id, a) { split(id, a, "|"); return a[1] }
								($6=="sup" || $6=="hac2sup" || $6=="hac_fixed" || $6=="hac") {
									u=uuid($1);
									if (!(u in f)) print $1;
								}
							' "$frozen_ids_list" "$otu_rows" \
								| normalize_header_id_stream \
								| LC_ALL=C sort -u \
								> "$eligible_list"
						else
							raw_elig=$(awk 'BEGIN{FS=OFS="\t"}
								($5=="sup" || $5=="hac2sup" || $5=="hac_fixed" || $5=="hac") { c++ }
								END{print c+0}
							' "$raw_otu_rows")
							awk 'BEGIN{FS=OFS="\t"}
								($6=="sup" || $6=="hac2sup" || $6=="hac_fixed" || $6=="hac") { print $1 }
							' "$otu_rows" \
								| normalize_header_id_stream \
								| LC_ALL=C sort -u \
								> "$eligible_list"
					fi
					n_elig=$(wc -l < "$eligible_list" | tr -d ' ')
					if [ "$raw_elig" -gt 0 ] && [ "$n_elig" -lt "$raw_elig" ]; then
						id_mismatch_events=$((id_mismatch_events + 1))
						msg="Consensus fallback eligible ID mismatch: OTU=$otu_key raw_eligible=$raw_elig resolved_eligible=$n_elig"
						if [ "$id_mismatch_policy" = "fail" ] && [ "$n_elig" -eq 0 ]; then
							echo "ERROR: $msg (none resolved in $sup_reads)" 1>&2
							exit 1
						else
							echo "WARN: $msg" 1>&2
						fi
						cons_log "$msg"
					fi
				fi
				[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key elig_final=$n_elig"
			# Accumulate eligible reads across rounds (per OTU/sample), storing best model rank and qscore per read.
			pool_file="$sample_cache_dir/${otu_key}.pool.tsv"
			pool_new="$sample_cache_dir/${otu_key}.pool_new.tsv"
			pool_ids=""
			pool_ids_count=0
			pool_size_streak_id=""
			[ -f "$pool_file" ] || : > "$pool_file"
				if [ -s "$eligible_list" ] && [ -s "$sample_qscore_map" ]; then
					pool_file_tmp="${pool_file}.tmp"
					awk 'BEGIN{FS=OFS="\t"}
						NR==FNR{
							# qscore_map format: read_id \t model \t qscore
						if (NF>=3) { q[$1]=$3; m[$1]=$2; }
						else if (NF==2) { q[$1]=$2; m[$1]="fast"; }
						next
					}
					{
						hdr=$1; uuid=hdr;
						if (index(uuid,"|")>0){split(uuid,a,"|"); uuid=a[1];}
						if (uuid in q) {
							model=m[uuid];
							rank=(model=="sup"?3:(model=="hac"?2:1));
							print uuid, hdr, rank, q[uuid];
						}
				}' "$sample_qscore_map" "$eligible_list" > "$pool_new"
				awk 'BEGIN{FS=OFS="\t"}
					{
						if (NF>=4) { uuid=$1; hdr=$2; r=$3+0; q=$4+0; }
						else if (NF==3) { hdr=$1; r=$2+0; q=$3+0; uuid=hdr; if (index(uuid,"|")>0){split(uuid,a,"|"); uuid=a[1];} }
						else if (NF==2) { hdr=$1; r=1; q=$2+0; uuid=hdr; if (index(uuid,"|")>0){split(uuid,a,"|"); uuid=a[1];} }
						else { next }
							sub(/\|OTUB_[^|[:space:]]+$/, "", hdr);
							if (!(uuid in br) || r>br[uuid] || (r==br[uuid] && q>bq[uuid])) {
								br[uuid]=r; bq[uuid]=q; bh[uuid]=hdr;
							}
						}
					END{for(u in br) print u, bh[u], br[u], bq[u] }' "$pool_file" "$pool_new" \
						| LC_ALL=C sort -k4,4nr -k3,3nr -k2,2 > "$pool_file_tmp"
					mv "$pool_file_tmp" "$pool_file"
					rm -f "$pool_new"
				fi

			# Candidate selection from pooled reads using sequence-rank logic.
			sel_prefix="$out_dir/$sample/${otu_key}_selection"
			sel_meta="$sel_prefix.meta.tsv"
			sel_rank1="$sel_prefix.rank1.fasta"
				cand_reps="$sel_prefix.selected_reps.tsv"
				reps_all="$sel_prefix.reps_all.tsv"
				if [ -s "$pool_file" ]; then
					pool_ids="$sel_prefix.pool_ids.list"
					pool_reads="$sel_prefix.pool_reads.fasta"
					: > "$pool_reads"   # pre-create; split-back appends; deferred loop uses -s not -f
					pool_ids_raw=""
					# Drop frozen IDs from pool (UUID-based) so we don't try to extract pruned reads.
					if [ "$prune_frozen_ids" -eq 1 ] && [ -s "$frozen_ids_list" ]; then
						pool_ids_raw="$sel_prefix.pool_ids.raw.list"
						awk 'BEGIN{FS=OFS="\t"}
							{
								id="";
								if (NF>=2) id=$2;
								else if (NF>=1) id=$1;
								sub(/\|OTUB_[^|[:space:]]+$/, "", id);
								if (id != "") print id;
							}' "$pool_file" > "$pool_ids_raw"
						awk 'FNR==NR { f[$1]=1; next }
							{
								id=$1; uuid=id;
								if (index(uuid,"|")>0) { split(uuid,a,"|"); uuid=a[1]; }
								if (!(uuid in f)) print id;
							}' "$frozen_ids_list" "$pool_ids_raw" > "$pool_ids"
					else
						awk 'BEGIN{FS=OFS="\t"}
							{
								id="";
								if (NF>=2) id=$2;
								else if (NF>=1) id=$1;
								sub(/\|OTUB_[^|[:space:]]+$/, "", id);
								if (id != "") print id;
							}' "$pool_file" > "$pool_ids"
					fi
					# O1b: accumulate pool_ids for batch extraction; defer per-OTU seqtk -> one samtools call per sample.
					pool_ids_count=0
					pool_size_streak_id=""
					if [ -s "$pool_ids" ]; then
						pool_ids_count=$(awk 'NF{c++} END{print c+0}' "$pool_ids")
						if [ "$pool_ids_count" -eq 1 ]; then
							if read -r _pool_one < "$pool_ids"; then
								_pool_one="${_pool_one%%|*}"
								if [ -n "$_pool_one" ]; then
									pool_size_streak_id="$_pool_one"
								fi
							fi
						fi
						awk -v k="${otu_key}_selection" '{print $0 "\t" k}' "$pool_ids" >> "$_union_pool_map"
						cat "$pool_ids" >> "$_union_pool_ids"
					fi
				if [ -n "$eligible_counts_file" ]; then
					printf "%s\t%s\t%s\t%s\n" "$sample" "$otu_key" "$pool_ids_count" "${CONSENSUS_ROUND_ID:-NA}" >> "$eligible_counts_file"
				fi
				if [ -n "$eligible_size_streak_file" ] && [ "$pool_ids_count" -eq 1 ] && [ -n "$pool_size_streak_id" ]; then
					printf "%s\t%s\t%s\t%s\n" "$sample" "$otu_key" "$pool_size_streak_id" "${CONSENSUS_ROUND_ID:-NA}" >> "$eligible_size_streak_file"
				fi
						selector_skip_reason="none"
						if [ "$prune_unassigned_clusters" -eq 1 ]; then
							if [ "$prune_unassigned_global_skip" -eq 1 ]; then
								selector_skip_reason="$prune_unassigned_global_reason"
							elif [ "$locked_this_otu" -eq 1 ]; then
								selector_skip_reason="locked_otu"
							elif [ "$is_frozen" -eq 1 ]; then
								selector_skip_reason="frozen_otu"
						fi
						fi
						# Save phase-1 orchestration state; deferred selector launch and phase 2 both derive from this workload row.
						[ -n "$pool_size_streak_id" ] || pool_size_streak_id="NA"
						printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
							"$otu_key" "$prev_stable_count" "$prev_lock_pass" "$locked_this_otu" "$is_frozen" \
							"$selector_skip_reason" "$n_elig" "$pool_ids_count" "$pool_size_streak_id" "pool_select" "$sel_prefix" \
							>> "$_phase1_workload"
						continue

			else
				rank1_only=0
				sel_mode="fallback"
				min_cand_conf="NA"
				confirmed_seqs=0
				if [ ! -s "$qscore_map" ]; then
					sel_mode="no_qscore_fallback"
					head -n "$selection_min" "$eligible_list" > "$candidate_list"
					n_cand=$(wc -l < "$candidate_list" | tr -d ' ')
					min_cand="NA"
				else
					if [ "$min_qscore" -gt 0 ]; then
						: > "$candidate_list"
						: > "$cand_reps"
						n_cand=0
						min_cand="NA"
					else
						head -n "$selection_min" "$eligible_list" > "$candidate_list"
						n_cand=$(wc -l < "$candidate_list" | tr -d ' ')
						min_cand="NA"
					fi
				fi
				min_cand_use="$min_cand"
				# Fallback path: save phase-1 state and write sel_meta for phase 2
				printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
					"$otu_key" "$prev_stable_count" "$prev_lock_pass" "$locked_this_otu" "$is_frozen" \
					"fallback" "$n_elig" "0" "NA" "fallback" "$sel_prefix" \
					>> "$_phase1_workload"
				printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$n_cand" "${min_cand:-NA}" "$rank1_only" "$sel_mode" "${min_cand_conf:-NA}" "${confirmed_seqs:-0}" \
					> "$sel_meta"
				continue
			fi
			done < "$otu_runtime_plan"
			_t_otu_prepare_end=$(timing_now)
			append_timing_row "$sample_timing_file" "sample" "$sample" "otu_prepare_phase1" "$_t_otu_prepare_start" "$_t_otu_prepare_end"
			_t_selector_phase_start=$(timing_now)
		# O1b: batch pool extraction
		if [ -s "$_union_pool_ids" ]; then
			LC_ALL=C sort -u "$_union_pool_ids" -o "$_union_pool_ids"
			if [ -f "${sup_reads}.fai" ] && command -v samtools >/dev/null 2>&1; then
				samtools faidx -r "$_union_pool_ids" "$sup_reads" > "$_union_pool_reads" 2>/dev/null || \
					{ cons_log "WARN: O1b samtools faidx failed for pool extraction"; : > "$_union_pool_reads"; }
				# samtools exits 0 even when IDs are absent; fall back to seqtk if output is empty but IDs were non-empty
				_pool_id_count=$(wc -l < "$_union_pool_ids" 2>/dev/null || echo 0)
				_pool_extracted=$(grep -c '^>' "$_union_pool_reads" 2>/dev/null || true)
				_pool_extracted=${_pool_extracted:-0}
				if [ "$_pool_extracted" -eq 0 ] && [ "$_pool_id_count" -gt 0 ]; then
					cons_log "WARN: O1b samtools returned empty output for $_pool_id_count pool IDs; falling back to seqtk"
					if ! seqtk subseq "$sup_reads" "$_union_pool_ids" > "$_union_pool_reads"; then
						cons_log "WARN: O1b seqtk fallback also failed; pool reads empty"
						: > "$_union_pool_reads"
					fi
				fi
			else
				if ! seqtk subseq "$sup_reads" "$_union_pool_ids" > "$_union_pool_reads"; then
					id_mismatch_events=$((id_mismatch_events + 1))
					msg="Consensus seqtk extraction failed (pool batch): sup_reads=$sup_reads union_ids=$_union_pool_ids"
					echo "WARN: $msg" 1>&2
					cons_log "$msg"
					: > "$_union_pool_reads"
				fi
			fi
			if [ -s "$_union_pool_reads" ] && [ -s "$_union_pool_map" ]; then
				awk -v base="$out_dir/$sample" '
					NR==FNR { a[$1] = a[$1] (a[$1] ? "\t" : "") $2; next }
					function flush(    n, keys, i) {
						if (cur == "" || seq == "") return
						n = split(cur, keys, "\t")
						for (i = 1; i <= n; i++)
							print seq >> (base "/" keys[i] ".pool_reads.fasta")
					}
					/^>/ { flush(); cur = ""; h = substr($0, 2); sub(/ .*/, "", h); if (h in a) cur = a[h]; seq = $0; next }
					cur  { seq = seq "\n" $0 }
					END  { flush() }
				' "$_union_pool_map" "$_union_pool_reads"
			fi
		fi
		# O1b: derive selector launch input from phase-1 workload, then spawn deferred selectors.
		if [ -s "$_phase1_workload" ]; then
			validate_phase1_workload_file "$_phase1_workload"
			awk 'BEGIN{FS=OFS="\t"} NF>=11 && $10=="pool_select" { print $1, $11, $8, $6 }' "$_phase1_workload" > "$_selector_queue"
		fi
		# O1b: spawn deferred selectors now that pool_reads files are in place
		if [ -s "$_selector_queue" ]; then
			while IFS=$'\t' read -r _p1_otu_key _p1_sel_prefix _p1_pool_count _p1_selector_skip_reason; do
				[ -n "$_p1_sel_prefix" ] || continue
				_p1_pool_file="$sample_cache_dir/${_p1_otu_key}.pool.tsv"
				_p1_pool_reads="${_p1_sel_prefix}.pool_reads.fasta"
				_p1_sel_prune_stats="${_p1_sel_prefix}.prune_stats.tsv"
				_p1_sel_drop_ids=""
				[ -n "$_p1_selector_skip_reason" ] || _p1_selector_skip_reason="none"
				_p1_selector_cmd=(
					"$script_path/consensus_select_reads_by_rank.pl"
					"$_p1_pool_file"
					"$_p1_pool_reads"
					"$selection_min"
					"$min_qscore"
					"$_p1_sel_prefix"
					"--emit-prune-stats" "$_p1_sel_prune_stats"
				)
				if [ "$prune_unassigned_clusters" -eq 1 ]; then
					_p1_selector_cmd+=(
						"--prune-unassigned"
						"--keep-unassigned-top" "$prune_unassigned_keep_top"
						"--assigned-ids" "$assigned_ids_list"
					)
					if [ "$prune_unassigned_drop_reads" -eq 1 ]; then
						_p1_sel_drop_ids="${_p1_sel_prefix}.dropped_ids.list"
						_p1_selector_cmd+=( "--dropped-ids" "$_p1_sel_drop_ids" )
					fi
					if [ "$_p1_selector_skip_reason" != "none" ]; then
						_p1_selector_cmd+=( "--skip-prune" )
					fi
				fi
				# pool_reads.fasta was pre-created empty; only flag mismatch if pool had IDs but none extracted
				if [ ! -s "${_p1_sel_prefix}.pool_reads.fasta" ] && [ "${_p1_pool_count:-0}" -gt 0 ]; then
					if [ "$id_mismatch_policy" = "fail" ]; then
						echo "ERROR: Consensus ID mismatch: OTU=${_p1_otu_key} pool_reads=0 (expected ${_p1_pool_count})" >&2; exit 1
					else
						id_mismatch_events=$((id_mismatch_events + 1))
						cons_log "WARN: Consensus ID mismatch: OTU=${_p1_otu_key} pool_reads=0 (expected ${_p1_pool_count})"
					fi
					fi
					if [ "${#_sel_pids[@]}" -ge "$MAX_SELECTOR_JOBS" ]; then
						_wait_pid="${_sel_pids[0]}"
						_sel_rc=0
						wait "$_wait_pid" 2>/dev/null || _sel_rc=$?
						if [ "$_sel_rc" -ne 0 ]; then
							worker_failures_selector=$((worker_failures_selector + 1))
							echo "ERROR: selector worker failed sample=$sample otu=${_p1_otu_key:-unknown} pid=$_wait_pid exit=$_sel_rc" 1>&2
							echo "METRIC: worker_failures_selector=$worker_failures_selector sample=$sample" 1>&2
							exit 1
						fi
						_sel_pids=("${_sel_pids[@]:1}")
					fi
					"${_p1_selector_cmd[@]}" &
				_sel_pids+=($!)
			done < "$_selector_queue"
			fi
			# Wait for all background selectors to complete
			for _p in "${_sel_pids[@]+"${_sel_pids[@]}"}"; do
				_sel_rc=0
				wait "$_p" 2>/dev/null || _sel_rc=$?
				if [ "$_sel_rc" -ne 0 ]; then
					worker_failures_selector=$((worker_failures_selector + 1))
					echo "ERROR: selector worker failed sample=$sample pid=$_p exit=$_sel_rc" 1>&2
					echo "METRIC: worker_failures_selector=$worker_failures_selector sample=$sample" 1>&2
					exit 1
				fi
			done
			_sel_pids=()
			# Phase 2: process selector results and compute consolidation decisions from the phase-1 workload table.
			while IFS=$'\t' read -r otu_key prev_stable_count prev_lock_pass locked_this_otu is_frozen selector_skip_reason eligible_count pool_ids_count pool_size_streak_id phase1_mode sel_prefix; do
				[ -n "$otu_key" ] || continue
			# Reconstruct per-OTU paths (same formulas as phase 1)
			pool_file="$sample_cache_dir/${otu_key}.pool.tsv"
			sel_meta="$sel_prefix.meta.tsv"
			sel_rank1="$sel_prefix.rank1.fasta"
			cand_reps="$sel_prefix.selected_reps.tsv"
			reps_all="$sel_prefix.reps_all.tsv"
			sel_prune_stats="$sel_prefix.prune_stats.tsv"
			sel_drop_ids=""
			[ "$prune_unassigned_drop_reads" -eq 1 ] && sel_drop_ids="$sel_prefix.dropped_ids.list"
			eligible_list="$out_dir/$sample/${otu_key}_eligible.list"
			cache_meta="$sample_cache_dir/${otu_key}.meta"
			cache_cons="$sample_cache_dir/${otu_key}.consensus.fasta"
			# Barcode fallback (mirrors phase 1)
			if [ ! -s "$cache_cons" ]; then
				_p2_nobc="${otu_key%-*}"
				if [ "$_p2_nobc" != "$otu_key" ]; then
					_p2_cons_nobc="$sample_cache_dir/${_p2_nobc}.consensus.fasta"
					_p2_meta_nobc="$sample_cache_dir/${_p2_nobc}.meta"
					if [ -s "$_p2_cons_nobc" ]; then
						cache_cons="$_p2_cons_nobc"
						cache_meta="$_p2_meta_nobc"
					fi
				fi
			fi
			if [ "$phase1_mode" = "pool_select" ]; then
				candidate_list="$sel_prefix.selected_ids.list"
						if [ "$prune_unassigned_drop_reads" -eq 1 ] && [ -n "$sel_drop_ids" ] && [ -s "$sel_drop_ids" ]; then
							cat "$sel_drop_ids" >> "$pruned_unassigned_round"
						fi
						if [ -s "$sel_prune_stats" ]; then
							IFS=$'\t' read -r _p_req _p_app _p_reason _p_total _p_assigned _p_unassigned _p_kept_u _p_drop_u _p_keep_top < <(read_selector_prune_stats "$sel_prune_stats")
							if [ "$_p_reason" = "skip_prune_flag" ] && [ "$selector_skip_reason" != "none" ]; then
								_p_reason="$selector_skip_reason"
							fi
							printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
								"$round_id" "$sample" "$otu_key" \
								"${_p_req:-0}" "${_p_app:-0}" "${_p_reason:-NA}" \
								"${_p_total:-0}" "${_p_assigned:-0}" "${_p_unassigned:-0}" \
								"${_p_kept_u:-0}" "${_p_drop_u:-0}" "${_p_keep_top:-$prune_unassigned_keep_top}" \
								>> "$prune_stats_round"
						else
							printf "%s\t%s\t%s\t0\t0\tmissing_selector_stats\t0\t0\t0\t0\t0\t%s\n" \
								"$round_id" "$sample" "$otu_key" "$prune_unassigned_keep_top" >> "$prune_stats_round"
						fi
				if [ -f "$sel_meta" ]; then
					read -r n_cand min_cand rank1_only sel_mode min_cand_conf confirmed_seqs < "$sel_meta"
				else
					n_cand=0
					min_cand="NA"
					rank1_only=0
					sel_mode="none"
						min_cand_conf="NA"
						confirmed_seqs=0
					fi
					if [ "$n_cand" -eq 0 ] && [ -s "$eligible_list" ]; then
						head -n "$selection_min" "$eligible_list" > "$candidate_list"
						n_cand=$(wc -l < "$candidate_list" | tr -d ' ')
						min_cand="NA"
						min_cand_conf="NA"
						confirmed_seqs=0
						rank1_only=0
						sel_mode="pool_select_empty_fallback"
						printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$n_cand" "$min_cand" "$rank1_only" "$sel_mode" "$min_cand_conf" "$confirmed_seqs" > "$sel_meta"
						cons_log "OTU=$otu_key pool_select_empty_fallback n_cand=$n_cand qscore_map_nonempty=$( [ -s "$qscore_map" ] && echo 1 || echo 0 )"
					fi
					min_cand_use="$min_cand"
					if [ "${min_cand_conf:-NA}" != "NA" ] && [ "${confirmed_seqs:-0}" -gt 0 ]; then
						min_cand_use="$min_cand_conf"
					fi
					[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key pool_select n_cand=$n_cand min_cand=$min_cand min_cand_conf=${min_cand_conf:-NA} confirmed_seqs=${confirmed_seqs:-0} min_cand_use=$min_cand_use rank1_only=$rank1_only sel_mode=$sel_mode"
			else
				candidate_list="$out_dir/$sample/${otu_key}_candidate.list"
				# sel_meta was written by phase 1 fallback; read it
				if [ -f "$sel_meta" ]; then
					read -r n_cand min_cand rank1_only sel_mode min_cand_conf confirmed_seqs < "$sel_meta"
				else
					n_cand=0; min_cand="NA"; rank1_only=0; sel_mode="fallback"; min_cand_conf="NA"; confirmed_seqs=0
				fi
				min_cand_use="$min_cand"
			fi
				if [ "$n_cand" -lt "$min_reads" ]; then
					printf "%s\t%s\t%s\n" "$otu_key" "0" "0" >> "$lock_state_current"
					[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key skip_consensus n_cand<$min_reads (n_cand=$n_cand min_cand=${min_cand_use:-$min_cand})"
					# Not enough pooled reads: keep cache by default to preserve recoverability.
				if [ "$cache_below_min_policy" = "drop" ]; then
					rm -f "$cache_meta" "$cache_cons"
					cons_log "OTU=$otu_key cache_drop_below_min policy=drop"
				else
					if [ -s "$cache_cons" ] || [ -s "$cache_meta" ]; then
						cons_log "OTU=$otu_key cache_keep_below_min policy=keep cache_cons=$( [ -s "$cache_cons" ] && echo 1 || echo 0 ) cache_meta=$( [ -s "$cache_meta" ] && echo 1 || echo 0 )"
					fi
				fi
				rm -f "$eligible_list" "$candidate_list" "$out_dir/$sample/${otu_key}_reads_sup.fasta"
				continue
			fi

					# Consolidation condition: by default, selected reads >= selection_min and min rep qscore >= consolidated_min_qscore, and OTU is frozen.
				# With lock enabled, require optional consecutive stable rounds before consolidation.
				min_cand_ok=0
				should_consolidate=0
				cluster_rule_ok=0
				cluster_cons_min=""
				cluster_noncons_max=0
			cluster_cons_count=0
			if [ "$n_cand" -ge "$selection_min" ] && [ "${min_cand_use:-NA}" != "NA" ]; then
				if awk -v v="$min_cand_use" -v thr="$consolidated_min_qscore" 'BEGIN{exit !(v>=thr)}'; then
					min_cand_ok=1
				fi
			fi
			if [ "$lock_enabled" -eq 1 ] && [ -s "$reps_all" ]; then
				while IFS=$'\t' read -r _hash _rep _rq _rk _cnt _sel; do
					[ -n "$_cnt" ] || continue
					case "$_cnt" in
						*[!0-9]*) continue ;;
					esac
					rq_ok=0
					if [ -n "$_rq" ] && [ "$_rq" != "NA" ]; then
						if awk -v v="$_rq" -v thr="$consolidated_min_qscore" 'BEGIN{exit !(v>=thr)}'; then
							rq_ok=1
						fi
					fi
					if [ "$rq_ok" -eq 1 ] && [ "$_cnt" -ge "$lock_min_cons_reads" ]; then
						cluster_cons_count=$((cluster_cons_count + 1))
						if [ -z "$cluster_cons_min" ] || [ "$_cnt" -lt "$cluster_cons_min" ]; then
							cluster_cons_min="$_cnt"
						fi
					else
						if [ "$_cnt" -gt "$cluster_noncons_max" ]; then
							cluster_noncons_max="$_cnt"
						fi
					fi
				done < "$reps_all"
				if [ -n "$cluster_cons_min" ]; then
					thr=$(awk -v r="$lock_ratio" -v m="$cluster_cons_min" 'BEGIN{printf "%.6f", r*m}')
					if awk -v non="$cluster_noncons_max" -v t="$thr" 'BEGIN{exit !(non<=t)}'; then
						cluster_rule_ok=1
					fi
				fi
			fi
				if [ "$lock_enabled" -eq 1 ]; then
					if [ "$is_frozen" -eq 1 ] && [ "$cluster_rule_ok" -eq 1 ] && [ "$cluster_cons_count" -gt 0 ]; then
						lock_rule_pass=1
					else
						lock_rule_pass=0
					fi
					if [ "$lock_rule_pass" -eq 1 ]; then
						if [ "$prev_lock_pass" -eq 1 ]; then
							stable_count=$((prev_stable_count + 1))
						else
							stable_count=1
						fi
					else
						stable_count=0
					fi
					if [ "$lock_rule_pass" -eq 1 ] && [ "$stable_count" -ge "$lock_min_stable_rounds" ]; then
						should_consolidate=1
					fi
				else
					if [ "$min_cand_ok" -eq 1 ] && [ "$is_frozen" -eq 1 ]; then
						should_consolidate=1
					fi
					stable_count=0
					lock_rule_pass=0
				fi
				printf "%s\t%s\t%s\n" "$otu_key" "$stable_count" "$lock_rule_pass" >> "$lock_state_current"
				[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key consolidate_decision=$should_consolidate n_cand=$n_cand min_cand=${min_cand_use:-$min_cand} min_cand_ok=$min_cand_ok cluster_cons_count=$cluster_cons_count cluster_cons_min=${cluster_cons_min:-NA} cluster_noncons_max=$cluster_noncons_max cluster_rule_ok=$cluster_rule_ok lock_rule_pass=$lock_rule_pass stable_count=$stable_count min_stable_rounds=$lock_min_stable_rounds"

			# If a cached consensus is already marked consolidated, reuse it even if
			# the current round doesn't meet consolidation thresholds.
			cache_cons_consolidated=0
			if [ -s "$cache_cons" ] && grep -q 'consolidated=1' "$cache_cons" 2>/dev/null; then
				cache_cons_consolidated=1
			fi
			effective_consolidated=0
			if [ "$should_consolidate" -eq 1 ] || [ "$cache_cons_consolidated" -eq 1 ]; then
				effective_consolidated=1
			fi
			ratio_threshold="NA"
			if [ -n "$cluster_cons_min" ]; then
				ratio_threshold=$(awk -v r="$lock_ratio" -v m="$cluster_cons_min" 'BEGIN{printf "%.6f", r*m}')
			fi
			reason="ok"
			if [ "$effective_consolidated" -eq 1 ]; then
				reason="consolidated"
			elif [ "$is_frozen" -eq 0 ]; then
				reason="not_frozen"
			elif [ "$lock_enabled" -eq 1 ]; then
				if [ "$cluster_cons_count" -le 0 ]; then
					reason="no_candidate_clusters"
				elif [ "$cluster_rule_ok" -eq 0 ]; then
					reason="ratio_failed"
				elif [ "$stable_count" -lt "$lock_min_stable_rounds" ]; then
					reason="stable_rounds"
				else
					reason="lock_pending"
				fi
			else
				if [ "$min_cand_ok" -eq 0 ]; then
					reason="minQ_or_reads"
				else
					reason="pending"
				fi
			fi
			printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
				"$round_id" "$sample" "$otu_key" "$n_cand" "${min_cand_use:-$min_cand}" "$min_cand_ok" "$is_frozen" \
				"$lock_enabled" "$cluster_cons_count" "${cluster_cons_min:-NA}" "$cluster_noncons_max" "$lock_ratio" \
				"$ratio_threshold" "$lock_rule_pass" "$stable_count" "$lock_min_stable_rounds" "$should_consolidate" \
				"$effective_consolidated" "$reason" "computed" >> "$lock_summary"
			if [ "$effective_consolidated" -eq 1 ]; then
				echo "$otu_key" >> "$consolidated_keys"
				printf "%s\t%s\n" "$sample" "$otu_key" >> "$consolidated_otu_keys_current"
			fi
			printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$otu_key" "$sample" "$n_cand" "${min_cand_use:-$min_cand}" "$is_frozen" "$effective_consolidated" >> "$sample_meta"
			if [ "$cache_cons_consolidated" -eq 1 ] && [ "$should_consolidate" -eq 0 ]; then
				[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key consolidated_cache_reuse cache_cons=$cache_cons"
				echo "$cache_cons" >> "$cached_list"
				continue
			fi

			if [ "$should_consolidate" -eq 1 ] && [ -s "$cache_cons" ]; then
				[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key consolidated_uses_cache cache_cons=$cache_cons"
				# Consolidated OTU: keep cached consensus, no recompute.
				echo "$cache_cons" >> "$cached_list"
				continue
			fi

			cand_hash=$(perl -MDigest::MD5 -ne 'BEGIN{$/; $d=Digest::MD5->new} $d->add($_); END{print $d->hexdigest}' "$candidate_list")
			cached_count=""
			cached_hash=""
			if [ -f "$cache_meta" ]; then
				read -r cached_count cached_hash < "$cache_meta"
			fi
			if [ "$cached_count" = "$n_cand" ] && [ "$cached_hash" = "$cand_hash" ] && [ -s "$cache_cons" ]; then
				[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key cache_reuse count=$cached_count hash=$cached_hash"
				echo "$cache_cons" >> "$cached_list"
				continue
			fi
			if [ "$rank1_only" -eq 1 ] && [ -s "$sel_rank1" ]; then
				[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$otu_key rank1_only_consensus n_cand=$n_cand"
				otu_cons="$out_dir/$sample/${otu_key}_consensus.fasta"
				otu_name=$(echo "$otu_key" | tr '-' '|')
				rank1_seq=$(awk 'BEGIN{seq=""} /^>/{next} {gsub(/\r/,""); seq=seq $0} END{print seq}' "$sel_rank1" \
					| tr 'acgtn' 'ACGTN' \
					| sed -E 's/[^ATCGN]/N/g')
				n_count=$(printf "%s" "$rank1_seq" | awk '{n=gsub(/N/,""); print n}')
				if [ -n "$rank1_seq" ]; then
					if [ "$n_count" -lt "$max_N" ]; then
						printf ">%s|%s|reads-%s\n%s\n" "$sample" "$otu_name" "$n_cand" "$rank1_seq" > "$otu_cons"
						cp "$otu_cons" "$sample_cache_dir/${otu_key}.consensus.fasta"
						printf "%s\t%s\n" "$n_cand" "$cand_hash" > "$sample_cache_dir/${otu_key}.meta"
						echo "$sample_cache_dir/${otu_key}.consensus.fasta" >> "$cached_list"
					fi
				fi
				continue
			fi
			# Accumulate candidate IDs for batched seqtk extraction (PR5)
			candidate_reads_fasta="$out_dir/$sample/${otu_key}_reads_sup.fasta"
			: > "$candidate_reads_fasta"
				if [ -s "$candidate_list" ]; then
					cat "$candidate_list" >> "$union_cand_ids"
					awk -v k="$otu_key" '{print $0 "\t" k}' "$candidate_list" >> "$cand_id_otu_map"
				fi
				echo -e "${otu_key}\t${n_cand}\t${cand_hash}" >> "$recompute_list"
			done < "$_phase1_workload"
			# ── Batch seqtk #2: one extraction per sample (PR5) ─────────────────────────
			if [ -s "$union_cand_ids" ]; then
				sort -u "$union_cand_ids" -o "$union_cand_ids"
				if [ -f "${sup_reads}.fai" ] && command -v samtools >/dev/null 2>&1; then
					# O1: indexed random-access extraction — O(n_candidates) instead of O(n_total_reads).
					# samtools faidx exits 0 even when some IDs are absent (prints warnings to stderr).
					samtools faidx -r "$union_cand_ids" "$sup_reads" > "$union_cand_reads" || true
				else
					if ! seqtk subseq "$sup_reads" "$union_cand_ids" > "$union_cand_reads"; then
						id_mismatch_events=$((id_mismatch_events + 1))
						msg="Consensus batch seqtk extraction failed: sample=$sample union_ids=$union_cand_ids"
						if [ "$id_mismatch_policy" = "fail" ]; then
							echo "ERROR: $msg" 1>&2; exit 1
						else
							echo "WARN: $msg" 1>&2
						fi
						cons_log "$msg"
					fi
				fi
				# Split union FASTA back to per-OTU _reads_sup.fasta (exact ID match).
			# Buffers the full sequence to handle both single-line and wrapped FASTA.
				awk -v base="$out_dir/$sample" '
					NR==FNR { a[$1] = a[$1] (a[$1]?"\t":"") $2; next }
					function flush(   n, keys, i) {
						if (cur == "" || seq == "") return
						n = split(cur, keys, "\t")
						for (i = 1; i <= n; i++)
							print seq >> (base "/" keys[i] "_reads_sup.fasta")
					}
					/^>/ {
						flush()
						h = substr($0, 2); sub(/ .*/, "", h)
						cur = (h in a) ? a[h] : ""; seq = $0; next
					}
					{ seq = seq "\n" $0 }
					END { flush() }
				' "$cand_id_otu_map" "$union_cand_reads"
				# Per-OTU mismatch validation
				if [ -s "$recompute_list" ]; then
					awk -F'\t' '{c[$2]++} END{for(k in c) print k"\t"c[k]}' \
						"$cand_id_otu_map" > "$out_dir/$sample/_rotu_counts.tmp"
					awk -F'\t' 'NR==FNR{cnt[$1]=$2; next} {print $0"\t"(($1 in cnt)?cnt[$1]:0)}' \
						"$out_dir/$sample/_rotu_counts.tmp" "$recompute_list" \
						> "$out_dir/$sample/_recompute_with_counts.tmp"
					while IFS=$'\t' read -r _rotu _rn _rh _rids; do
						_rfasta="$out_dir/$sample/${_rotu}_reads_sup.fasta"
						if [ -f "$_rfasta" ]; then
							_rreads=$(awk '/^>/{c++} END{print c+0}' "$_rfasta")
						else
							_rreads=0
						fi
						if [ "$_rids" -gt 0 ] && [ "$_rreads" -eq 0 ]; then
							id_mismatch_events=$((id_mismatch_events + 1))
							msg="Consensus candidate ID mismatch: OTU=$_rotu candidate_ids=$_rids candidate_reads=0 (IDs not found in $sup_reads)"
							if [ "$id_mismatch_policy" = "fail" ]; then
								echo "ERROR: $msg" 1>&2; exit 1
							else
								echo "WARN: $msg" 1>&2
							fi
							cons_log "$msg"
						fi
					done < "$out_dir/$sample/_recompute_with_counts.tmp"
				fi
				fi
					# Carry forward locked OTUs missing from this round's OTU list so
					# consolidated consensuses do not disappear in sparse rounds.
					if [ "$lock_enabled" -eq 1 ] && [ -s "$carry_forward_state" ]; then
						while IFS=$'\t' read -r locked_key prev_stable_count prev_lock_pass; do
							[ -n "$locked_key" ] || continue
							if grep -Fxq "$locked_key" "$processed_otu_keys"; then
								continue
						fi
						cache_cons="$sample_cache_dir/${locked_key}.consensus.fasta"
						if [ ! -s "$cache_cons" ]; then
							locked_key_nobc="${locked_key%-*}"
							if [ "$locked_key_nobc" != "$locked_key" ]; then
								cache_cons_alt="$sample_cache_dir/${locked_key_nobc}.consensus.fasta"
								if [ -s "$cache_cons_alt" ]; then
									cache_cons="$cache_cons_alt"
								fi
							fi
						fi
							prev_stable_count=${prev_stable_count:-0}
							prev_lock_pass=${prev_lock_pass:-0}
							printf "%s\t%s\t%s\n" "$locked_key" "$prev_stable_count" "$prev_lock_pass" >> "$lock_state_current"
						if [ -s "$cache_cons" ]; then
							echo "$locked_key" >> "$consolidated_keys"
							printf "%s\t%s\n" "$sample" "$locked_key" >> "$consolidated_otu_keys_current"
							IFS=$'\t' read -r cache_n cache_minq cache_frozen < <(read_cache_consensus_meta "$cache_cons")
							printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$locked_key" "$sample" "$cache_n" "$cache_minq" "$cache_frozen" "1" >> "$sample_meta"
							printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
								"$round_id" "$sample" "$locked_key" "$cache_n" "$cache_minq" "NA" "$cache_frozen" \
								"$lock_enabled" "NA" "NA" "NA" "$lock_ratio" "NA" "$prev_lock_pass" "$prev_stable_count" \
								"$lock_min_stable_rounds" "0" "1" "carry_forward_cache" "carry_forward" >> "$lock_summary"
							echo "$cache_cons" >> "$cached_list"
							[ "$CONS_DEBUG" = "1" ] && cons_log "OTU=$locked_key locked_cache_carry_forward source=missing_from_otu_list"
						else
							echo "WARN: OTU=$locked_key locked but cache missing during carry-forward; metadata placeholder emitted" 1>&2
							printf "%s\t%s\t%s\t%s\t%s\t%s\n" "$locked_key" "$sample" "0" "NA" "1" "0" >> "$sample_meta"
							printf "%s\t%s\n" "$sample" "$locked_key" >> "$consolidated_otu_keys_drop"
							printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n" \
								"$round_id" "$sample" "$locked_key" "0" "NA" "NA" "1" "$lock_enabled" "NA" "NA" "NA" \
								"$lock_ratio" "NA" "$prev_lock_pass" "$prev_stable_count" "$lock_min_stable_rounds" "0" "0" \
								"carry_forward_cache_missing" "carry_forward" >> "$lock_summary"
						fi
						done < "$carry_forward_state"
					fi
				_t_selector_phase_end=$(timing_now)
				append_timing_row "$sample_timing_file" "sample" "$sample" "selector_phase" "$_t_selector_phase_start" "$_t_selector_phase_end"
				rm -f "$union_cand_ids" "$cand_id_otu_map" "$union_cand_reads"
				# ── End batch seqtk #2 ───────────────────────────────────────────────────────
					[ "$sample_blast_cleanup" -eq 1 ] && rm -f "$sample_blast"
					rm -f "$sample_parsed" "$otu_list" "$processed_otu_keys"
				else
			if [ "$CONS_DEBUG" = "1" ]; then
				cached_count=$(ls "$cache_root/$sample"/*.consensus.fasta 2>/dev/null | wc -l | tr -d ' ')
			cons_log "Sample=$sample no_current_reads cached_consensus_files=$cached_count (carrying cached consensus)"
		fi
			for cf in "$cache_root/$sample"/*.consensus.fasta; do
				[ -s "$cf" ] || continue
				echo "$cf" >> "$cached_list"
			done
				[ "$sample_blast_cleanup" -eq 1 ] && rm -f "$sample_blast"
				rm -f "$sample_parsed" "$otu_list" "$processed_otu_keys"
			fi
	if [ -d "$out_dir/$sample" ] && [ "$(ls -A "$out_dir/$sample" 2>/dev/null)" ] || [ -s "$cached_list" ]; then
			sample_consensus_fasta="$out_dir/$sample/${sample}_consensus.fasta"
			r_otu_inputs=0
			r_input_headers=0
			r_input_bases=0
			r_output_records=0
			r_output_bases=0
			_t_r_consensus_start=$(timing_now)
			if compgen -G "$out_dir/$sample/*_reads_sup.fasta" > /dev/null 2>&1; then
				_t_r_input_scan_start=$(timing_now)
				IFS=$'\t' read -r r_otu_inputs r_input_headers r_input_bases < <(
					awk '
						FNR==1 { files++ }
						/^>/ { seqs++; next }
						{ gsub(/\r/, ""); bases+=length($0) }
						END { print files+0 "\t" seqs+0 "\t" bases+0 }
					' "$out_dir/$sample"/*_reads_sup.fasta
				)
				_t_r_input_scan_end=$(timing_now)
				append_timing_row "$sample_timing_file" "sample" "$sample" "r_consensus_input_scan" "$_t_r_input_scan_start" "$_t_r_input_scan_end"
				_t_r_exec_start=$(timing_now)
				Rscript "$Consensus_Rscript" "$sample" "$min_reads" "$max_N" || { echo "ERROR: Rscript failed for sample $sample" 1>&2; exit 1; }
				_t_r_exec_end=$(timing_now)
				append_timing_row "$sample_timing_file" "sample" "$sample" "r_consensus_exec" "$_t_r_exec_start" "$_t_r_exec_end"
				if [ -s "$sample_consensus_fasta" ]; then
					IFS=$'\t' read -r r_output_records r_output_bases < <(
						awk '
							/^>/ { seqs++; next }
							{ gsub(/\r/, ""); bases+=length($0) }
							END { print seqs+0 "\t" bases+0 }
						' "$sample_consensus_fasta"
					)
				fi
			else
				: > "$sample_consensus_fasta"
				append_timing_row "$sample_timing_file" "sample" "$sample" "r_consensus_input_scan" "$_t_r_consensus_start" "$_t_r_consensus_start"
				append_timing_row "$sample_timing_file" "sample" "$sample" "r_consensus_exec" "$_t_r_consensus_start" "$_t_r_consensus_start"
			fi
			_t_r_consensus_end=$(timing_now)
				append_timing_row "$sample_timing_file" "sample" "$sample" "r_consensus" "$_t_r_consensus_start" "$_t_r_consensus_end"
					printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
						"$round_id" "$sample" "$r_otu_inputs" "$r_input_headers" "$r_input_bases" "$r_output_records" "$r_output_bases" \
						>> "$rscript_stats_file" 2>/dev/null || true
					printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
						"$round_id" "$sample" "$r_otu_inputs" "$r_input_headers" "$r_input_bases" "$r_output_records" "$r_output_bases" \
						>> "$sample_totals_file" 2>/dev/null || true

						# Update cache for recomputed OTUs
						if [ -s "$recompute_list" ]; then
							recompute_with_reval="$out_dir/$sample/recompute_with_reval.tsv"
							awk -v revalf="$revalidate_before" -v recomputef="$recompute_list" 'BEGIN{FS=OFS="\t"}
								FILENAME==revalf { h[$1]=$2; next }
								FILENAME==recomputef { print $1, $2, $3, (($1 in h) ? h[$1] : "") }
							' "$revalidate_before" "$recompute_list" > "$recompute_with_reval"
						while IFS=$'\t' read -r otu_key n_cand cand_hash old_reval_hash; do
							otu_cons="$out_dir/$sample/${otu_key}_consensus.fasta"
							if [ -s "$otu_cons" ]; then
								cp "$otu_cons" "$sample_cache_dir/${otu_key}.consensus.fasta"
								printf "%s\t%s\n" "$n_cand" "$cand_hash" > "$sample_cache_dir/${otu_key}.meta"
							if [ -n "$old_reval_hash" ]; then
								new_reval_hash=$(cons_seq_hash "$sample_cache_dir/${otu_key}.consensus.fasta")
								if [ -n "$new_reval_hash" ]; then
									if [ "$new_reval_hash" = "$old_reval_hash" ]; then
										cons_log "OTU=$otu_key lock_revalidation_result=unchanged hash=$new_reval_hash"
									else
										cons_log "OTU=$otu_key lock_revalidation_result=drifted old_hash=$old_reval_hash new_hash=$new_reval_hash"
									fi
								fi
							fi
						else
							rm -f "$sample_cache_dir/${otu_key}.consensus.fasta" "$sample_cache_dir/${otu_key}.meta"
							if [ -n "$old_reval_hash" ]; then
									cons_log "OTU=$otu_key lock_revalidation_result=missing_recompute_output old_hash=$old_reval_hash"
								fi
							fi
						done < "$recompute_with_reval"
						rm -f "$recompute_with_reval"
					fi

			_t_refine_emit_start=$(timing_now)
			# Combine cached and newly computed consensus sequences
			cached_fasta="$out_dir/$sample/${sample}_cached_consensus.fasta"
			: > "$cached_fasta"
			if [ -s "$cached_list" ]; then
					while IFS= read -r cf; do
						[ -s "$cf" ] || continue
						otu_key_from_file="$(basename "$cf" .consensus.fasta)"
						awk -v ok="$otu_key_from_file" 'BEGIN{RS=">"; ORS=""} NR>1{
							h=$1; sub(/\n.*/, "", h);
							body=$0; sub(/^[^\n]*\n/, "", body);
							if (h ~ /\|OTU=/ || h ~ /\|OTUB_/) { print ">"$0; next }
							otu=ok; marker="";
							dash=index(ok, "-");
							if (dash > 0) {
								otu=substr(ok, 1, dash-1);
								marker=substr(ok, dash+1);
							}
							reads="reads-0";
							if (match(h, /reads-[0-9]+/)) { reads=substr(h, RSTART, RLENGTH); }
							sample="";
							split(h, b, "|"); sample=b[1];
							if (sample=="") sample="unknown";
						newh=sample "|" otu;
						if (marker != "") { newh=newh "|" marker; }
						newh=newh "|" reads;
						print ">" newh "\n" body;
					}' "$cf" >> "$cached_fasta"
				done < "$cached_list"
			fi
				merged="$out_dir/$sample/${sample}_consensus_all.fasta"
				sample_consensus_fasta="$out_dir/$sample/${sample}_consensus.fasta"
				[ -f "$sample_consensus_fasta" ] || : > "$sample_consensus_fasta"
				cat "$cached_fasta" "$sample_consensus_fasta" > "$merged"
				awk 'BEGIN{RS=">"; ORS=""} NR>1 {h=$1; sub(/\n.*/, "", h); if(!seen[h]++){print ">"$0}}' "$merged" > "${merged}.tmp" && mv "${merged}.tmp" "$merged"
				merged_headers_this_sample=0
				if [ -s "$merged" ]; then
					merged_headers_this_sample=$(awk '/^>/{c++} END{print c+0}' "$merged")
				fi
				merged_input_headers_total=$((merged_input_headers_total + merged_headers_this_sample))
				# Annotate consensus headers with OTU metadata and build map
				if [ -s "$merged" ]; then
				annotated_tmp="${merged}.annotated"
				awk -v meta="$sample_meta" -v map="$consensus_map" -v sample="$sample" 'BEGIN{FS=OFS="\t";
					while((getline< meta)>0){
						if(NF>=6){ m[$1]=$0; }
					}
				}
				/^>/{
					h_raw=substr($0,2);
					n=split(h_raw,a,"|");
					otu_key="NA";
					tmp=h_raw;
					while (match(tmp, /\|OTU=[^|]+/)) {
						otu_key=substr(tmp, RSTART+5, RLENGTH-5);
						tmp=substr(tmp, RSTART+RLENGTH);
					}
					if (otu_key=="NA" && n>=3) {
						otu_key=a[2];
						if (a[3] != "" && otu_key !~ ("-" a[3] "$")) { otu_key=otu_key "-" a[3]; }
					}
					info=m[otu_key];
					n_reads="NA"; minq="NA"; frozen="0"; cons="0";
					if (info!="") {
						split(info,f,"\t");
						n_reads=f[3]; minq=f[4]; frozen=f[5]; cons=f[6];
					} else {
						tmp=h_raw;
						while (match(tmp, /\|n=[^|]+/)) { n_reads=substr(tmp, RSTART+3, RLENGTH-3); tmp=substr(tmp, RSTART+RLENGTH); }
						tmp=h_raw;
						while (match(tmp, /\|minQ=[^|]+/)) { minq=substr(tmp, RSTART+6, RLENGTH-6); tmp=substr(tmp, RSTART+RLENGTH); }
						tmp=h_raw;
						while (match(tmp, /\|frozen=[01]/)) { frozen=substr(tmp, RSTART+8, 1); tmp=substr(tmp, RSTART+RLENGTH); }
						tmp=h_raw;
						while (match(tmp, /\|consolidated=[01]/)) { cons=substr(tmp, RSTART+14, 1); tmp=substr(tmp, RSTART+RLENGTH); }
					}
					# Drop any existing metadata tags before appending a fresh block.
					h=h_raw;
					gsub(/\|OTU=[^|]*/, "", h);
					gsub(/\|n=[^|]*/, "", h);
					gsub(/\|minQ=[^|]*/, "", h);
					gsub(/\|frozen=[^|]*/, "", h);
					gsub(/\|consolidated=[^|]*/, "", h);
					newh=h "|OTU=" otu_key "|n=" n_reads "|minQ=" minq "|frozen=" frozen "|consolidated=" cons;
					print ">" newh;
					print newh "\t" otu_key "\t" sample "\t" n_reads "\t" minq "\t" frozen "\t" cons >> map;
					next;
				}
				{print}' "$merged" > "$annotated_tmp" && mv "$annotated_tmp" "$merged"
			fi
			# B2: Use file-based lookup instead of string grep to avoid O(n*m) per-cluster cost.
			# _cons_ids_prev_file points directly at consolidated_ids_prev for grep -Fxq.
			_cons_ids_prev_file=""
			if [ -s "$consolidated_ids_prev" ]; then
				_cons_ids_prev_file="$consolidated_ids_prev"
			fi

			if [ -s "$merged" ]; then
				vsearch --cluster_fast "$merged" --id "$consensus_id" --clusters "$out_dir/${sample}/${sample}_Consensus" --iddef 3 --threads $RSCRIPT_WORKERS
				if [ ! -e "$out_dir/$sample/OriginalReads" ]; then mkdir "$out_dir/$sample/OriginalReads"; fi
				: > "$out_dir/${sample}/${sample}_Merged_Consensus.fasta"
				cluster_files=( "$out_dir/${sample}/${sample}_Consensus"* )
				# Load consolidated keys once for all cluster files in this sample
				_consk_tab=""
				if [ -s "$consolidated_keys" ]; then
					_consk_tab=$(tr '\n' '\t' < "$consolidated_keys")
				fi
				for i in "${cluster_files[@]}"; do
					[ -f "$i" ] || continue
					# Determine whether this cluster corresponds to a consolidated OTU
					consolidated_hit=0
					best_entry=""
					otu_key=""
					best_cons=0
					best_fields=()
					while IFS= read -r line; do
						best_fields+=("$line")
					done < <(awk -v _consk_tab="$_consk_tab" '
						BEGIN{
							max_reads=-1; best_cons=-1; best_minq=-1e18; best_otu=""; best_h="";
							rec_n=0; any_cons=0;
							n=split(_consk_tab, _ck_a, "\t");
							for (_j=1; _j<=n; _j++) {
								gsub(/\r/, "", _ck_a[_j]);
								if (_ck_a[_j] ~ /\S/) consk[_ck_a[_j]]=1;
							}
						}
						/^>/{
							h=$0; sub(/^>/,"",h);
							reads=0;
							if (h ~ /reads-[0-9]+/) {
								tmp=h; sub(/.*reads-/,"",tmp); gsub(/[^0-9].*/,"",tmp); reads=tmp+0;
							}
							otu="";
							tmp=h;
							while (match(tmp, /\|OTU=[^|]+/)) {
								otu=substr(tmp, RSTART+5, RLENGTH-5);
								tmp=substr(tmp, RSTART+RLENGTH);
							}
							if (otu == "") {
								n=split(h, p, "|");
								if (n>=2) {
									otu=p[2];
									if (n>=3 && p[3] != "" && otu !~ ("-" p[3] "$")) otu=otu "-" p[3];
								}
							}
							cons=-1;
							tmp=h;
							while (match(tmp, /\|consolidated=[01]/)) {
								cons=substr(tmp, RSTART+14, 1)+0;
								tmp=substr(tmp, RSTART+RLENGTH);
							}
							if (cons < 0) {
								if (otu != "" && (otu in consk)) { cons=1; } else { cons=0; }
							}
							minq=-1e18;
							tmp=h;
							while (match(tmp, /\|minQ=[^|]+/)) {
								mq=substr(tmp, RSTART+6, RLENGTH-6);
								tmp=substr(tmp, RSTART+RLENGTH);
								if (mq != "NA" && mq ~ /^-?[0-9]+([.][0-9]+)?$/) minq=mq+0;
							}
							if (cons == 1) any_cons=1;
							rec_n++;
							rec_h[rec_n]=h; rec_otu[rec_n]=otu; rec_reads[rec_n]=reads; rec_cons[rec_n]=cons; rec_minq[rec_n]=minq;
						}
						END{
							best_set=0;
							if (any_cons) {
								for (i=1; i<=rec_n; i++) {
									if (rec_cons[i] == 1) {
										best_h=rec_h[i]; best_otu=rec_otu[i]; best_cons=rec_cons[i]; best_minq=rec_minq[i]; max_reads=rec_reads[i];
										best_set=1;
										break;
									}
								}
							}
							if (!best_set && rec_n >= 1) {
								best_h=rec_h[1]; best_otu=rec_otu[1]; best_cons=rec_cons[1]; best_minq=rec_minq[1]; max_reads=rec_reads[1];
								best_set=1;
							}
							for (i=1; i<=rec_n; i++) {
								if (any_cons && rec_cons[i] != 1) continue;
								reads=rec_reads[i]; cons=rec_cons[i]; minq=rec_minq[i]; otu=rec_otu[i]; h=rec_h[i];
								if (!best_set || reads > max_reads \
								 || (reads == max_reads && cons > best_cons) \
								 || (reads == max_reads && cons == best_cons && minq > best_minq) \
								 || (reads == max_reads && cons == best_cons && minq == best_minq && (best_otu == "" || otu < best_otu))) {
									max_reads=reads; best_cons=cons; best_minq=minq; best_otu=otu; best_h=h;
									best_set=1;
								}
							}
							print best_h;
							print best_otu;
							print best_cons;
						}' "$i")
						best_entry="${best_fields[0]:-}"
						otu_key="${best_fields[1]:-}"
						best_cons="${best_fields[2]:-0}"
						if [ -n "$best_entry" ]; then
							if [ -z "$otu_key" ]; then
								otu=$(echo "$best_entry" | cut -d"|" -f2)
								bc=$(echo "$best_entry" | cut -d"|" -f3)
								otu_key="${otu}"
								if [[ -n "$bc" && "$bc" != "NA" && "$otu_key" != *"-${bc}" ]]; then
									otu_key="${otu_key}-${bc}"
								fi
							fi
							if [ "$best_cons" = "1" ]; then
								consolidated_hit=1
							fi
						fi

						out_block=$(CONSENSUS_READS_MODE="$consensus_reads_mode" _best_consensus_addition "$i" "$best_entry")
						if [ -n "$out_block" ]; then
							emitted_consensus_count=$((emitted_consensus_count + 1))
							printf "%s\n" "$out_block" >> $out_dir/${sample}/${sample}_Merged_Consensus.fasta
							cons_header=$(printf "%s\n" "$out_block" | head -n1 | sed 's/^>//')
							if [ -n "$cons_header" ]; then
								consensus_id=$(printf "%s\n" "$cons_header" | awk -F'|' 'NF>=2{print $2 "_" $1}')
								prior_keep=0
								if [ -n "$_cons_ids_prev_file" ]; then
									if grep -Fxq "$cons_header" "$_cons_ids_prev_file"; then
										prior_keep=1
									elif [ -n "$consensus_id" ] && grep -Fxq "$consensus_id" "$_cons_ids_prev_file"; then
										prior_keep=1
									fi
								fi
								if [ "$consolidated_hit" -eq 1 ] || [ "$prior_keep" -eq 1 ]; then
									echo "$cons_header" >> "$consolidated_ids_current"
									if [ -n "$consensus_id" ]; then
										echo "$consensus_id" >> "$consolidated_ids_current"
									fi
								fi
							fi
						fi
						rm -f "$i"
				done
				fi
				rm -f $out_dir/$sample/${sample}_consensus.fasta "$cached_fasta" "$merged"
				rm -f $out_dir/$sample/*_all_reads.list $out_dir/$sample/*_all_reads_full.list $out_dir/$sample/*_all_read_ids.list
				rm -f $out_dir/$sample/*_reads_sup.fasta \
							$out_dir/$sample/*_candidate.list \
							$out_dir/$sample/*_eligible.list \
							$out_dir/$sample/*_eligible.raw.list \
						$out_dir/$sample/*_consensus.fasta \
					$out_dir/$sample/*_selection.* \
					$out_dir/$sample/*_selection_* \
						$out_dir/$sample/*_candidate_reps.tsv \
						$out_dir/$sample/*_pool_ids.list \
						$out_dir/$sample/*_pool_reads.fasta \
						"$out_dir/$sample/_phase1_workload.tsv" \
						"$out_dir/$sample/_selector_queue.tsv" \
						"$out_dir/$sample/_rotu_counts.tmp" \
						"$out_dir/$sample/_recompute_with_counts.tmp" \
						"$out_dir/$sample/_requested_ids.list" \
						"$out_dir/$sample/_requested_uuids.list" \
						"$out_dir/$sample/_resolved_id_map.tsv" \
						"$out_dir/$sample/_qscore_map.tsv"
						rm -f "$revalidate_before" "$otu_runtime_plan" "$carry_forward_state"
						if [ -d "$sample_otu_dir" ]; then
							rm -f "$sample_otu_dir"/*.tsv 2>/dev/null || true
							rmdir "$sample_otu_dir" 2>/dev/null || true
						fi
						if [ -d "$sample_otu_resolved_dir" ]; then
							rm -f "$sample_otu_resolved_dir"/*.tsv 2>/dev/null || true
							rmdir "$sample_otu_resolved_dir" 2>/dev/null || true
						fi
				_t_refine_emit_end=$(timing_now)
				append_timing_row "$sample_timing_file" "sample" "$sample" "merge_vsearch_refine" "$_t_refine_emit_start" "$_t_refine_emit_end"
				fi
		if [ -s "$lock_state_current" ]; then
			awk 'BEGIN{FS=OFS="\t"}
				NF>=3 {
					key=$1;
					if (!(key in last_order)) {
						order[++n]=key;
					}
					last_order[key]=n;
					row[key]=$1 OFS $2 OFS $3;
				}
				END{
					for (i=1; i<=n; i++) {
						k=order[i];
						if (k in row) print row[k];
					}
				}
			' "$lock_state_current" > "${lock_state_file}.tmp" && mv "${lock_state_file}.tmp" "$lock_state_file"
		elif [ -s "$lock_state_prev" ]; then
			cp "$lock_state_prev" "$lock_state_file"
		else
			: > "$lock_state_file"
		fi
		rm -f "$lock_state_prev" "$lock_state_current"
	printf "emitted\t%d\nmerged\t%d\nmismatches\t%d\nselector_failures\t%d\nfallback_empty_sample_blast_count\t%d\n" \
		"$emitted_consensus_count" "$merged_input_headers_total" "$id_mismatch_events" "$worker_failures_selector" "$fallback_empty_sample_blast_count" \
		> "$out_dir/$sample/_counters.tmp"
	_t_sample_total_end=$(timing_now)
	append_timing_row "$sample_timing_file" "sample" "$sample" "sample_total" "$_t_sample_total_start" "$_t_sample_total_end"
	# O5: mark sample dirty so the parent knows which samples produced new consensus this round
	[ "$emitted_consensus_count" -gt 0 ] && printf '%s\n' "$sample" > "$out_dir/.dirty_${sample}" || true
	) &
	_sample_jobs+=("$!")
	if [ "${#_sample_jobs[@]}" -ge "$MAX_JOBS" ]; then
		wait "${_sample_jobs[0]}" || { echo "ERROR: consensus worker failed" >&2; exit 1; }
		_sample_jobs=("${_sample_jobs[@]:1}")
	fi
done < "$samples_file"
_t_sample_worker_dispatch_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "sample_worker_dispatch" "$_t_sample_worker_dispatch_start" "$_t_sample_worker_dispatch_end"

_t_sample_workers_start="$_t_sample_worker_dispatch_end"
for _pid in "${_sample_jobs[@]+"${_sample_jobs[@]}"}"; do
	wait "$_pid" || { echo "ERROR: consensus worker failed" >&2; exit 1; }
done
_t_sample_workers_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "sample_worker_compute_elapsed" "$_t_sample_workers_start" "$_t_sample_workers_end"

# O5: collect per-sample dirty markers into a single list for the caller
: > "$out_dir/modified_samples.list"
for _df in "$out_dir"/.dirty_*; do
	[ -f "$_df" ] || continue
	cat "$_df" >> "$out_dir/modified_samples.list"
	rm -f "$_df"
done
for _tf in "$out_dir"/*/_timings.tmp; do
	[ -f "$_tf" ] || continue
	cat "$_tf" >> "$sample_phase_timings_file" 2>/dev/null || true
	rm -f "$_tf"
done
for _hf in "$out_dir"/*/_cache_hydration.tmp; do
	[ -f "$_hf" ] || continue
	cat "$_hf" >> "$cache_hydration_stats_file" 2>/dev/null || true
	rm -f "$_hf"
done

emitted_consensus_count=0
merged_input_headers_total=0
id_mismatch_events=0
worker_failures_selector=0
fallback_empty_sample_blast_count=0
_t_finalize_start=$(timing_now)
_t_counter_merge_start=$(timing_now)
for _ctmp in "$out_dir"/*/_counters.tmp; do
	[ -f "$_ctmp" ] || continue
	IFS=$'\t' read -r _e _m _i _s _f < <(
		awk 'BEGIN{e=m=i=s=f=0}
			$1=="emitted"                           {e=$2}
			$1=="merged"                            {m=$2}
			$1=="mismatches"                        {i=$2}
			$1=="selector_failures"                 {s=$2}
			$1=="fallback_empty_sample_blast_count" {f=$2}
			END{print e"\t"m"\t"i"\t"s"\t"f}' "$_ctmp"
	)
	emitted_consensus_count=$(( emitted_consensus_count + _e ))
	merged_input_headers_total=$(( merged_input_headers_total + _m ))
	id_mismatch_events=$(( id_mismatch_events + _i ))
	worker_failures_selector=$(( worker_failures_selector + _s ))
	fallback_empty_sample_blast_count=$(( fallback_empty_sample_blast_count + _f ))
done
_t_counter_merge_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "counter_and_tmp_merge" "$_t_counter_merge_start" "$_t_counter_merge_end"

_t_sample_output_merge_start=$(timing_now)
for _f in "$out_dir"/*/_consolidated_ids.tmp;     do [ -f "$_f" ] && cat "$_f" >> "$consolidated_ids_current";      done
for _f in "$out_dir"/*/_otu_keys_current.tmp;     do [ -f "$_f" ] && cat "$_f" >> "$consolidated_otu_keys_current"; done
for _f in "$out_dir"/*/_eligible_counts.tmp;      do [ -f "$_f" ] && cat "$_f" >> "$eligible_counts_file";          done
for _f in "$out_dir"/*/_eligible_size_streak.tmp; do [ -f "$_f" ] && cat "$_f" >> "$eligible_size_streak_file";     done
for _f in "$out_dir"/*/_pruned_unassigned.tmp;    do [ -f "$_f" ] && cat "$_f" >> "$pruned_unassigned_round";       done
for _f in "$out_dir"/*/_prune_stats.tmp;          do [ -f "$_f" ] && cat "$_f" >> "$prune_stats_round";             done
for _f in "$out_dir"/*/_lock_summary.tmp;         do [ -f "$_f" ] && cat "$_f" >> "$lock_summary";                 done
for _f in "$out_dir"/*/_consensus_map.tmp;        do [ -f "$_f" ] && cat "$_f" >> "$consensus_map";                done

if [ "$prune_unassigned_drop_reads" -eq 1 ] && [ -s "$pruned_unassigned_round" ]; then
	LC_ALL=C sort -u -o "$pruned_unassigned_round" "$pruned_unassigned_round"
fi
_t_sample_output_merge_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "sample_output_merge" "$_t_sample_output_merge_start" "$_t_sample_output_merge_end"

_t_consolidated_finalize_start=$(timing_now)
cons_ids_source=""
cons_ids_kept_previous=0
cons_ids_reason=""
if [ "$emitted_consensus_count" -gt 0 ]; then
	if [ -s "$consolidated_ids_current" ]; then
		LC_ALL=C sort -u -o "$consolidated_ids_current" "$consolidated_ids_current"
		cons_ids_source="$consolidated_ids_current"
		cons_ids_reason="emitted"
		cons_log "CONS_IDS: prepared current set (emitted_consensus_count=$emitted_consensus_count)"
	elif [ -s "$consolidated_ids_prev" ]; then
		cons_ids_source="$consolidated_ids_prev"
		cons_ids_kept_previous=1
		cons_ids_reason="emitted_keep_prev"
		cons_log "CONS_IDS: consensus emitted but no new consolidations; keeping previous IDs"
	else
		cons_ids_source="$consolidated_ids_current"
		cons_ids_reason="emitted_no_prev"
		cons_log "CONS_IDS: consensus emitted but no consolidated IDs exist yet"
	fi
elif [ -s "$consolidated_ids_prev" ]; then
	cons_ids_source="$consolidated_ids_prev"
	cons_ids_kept_previous=1
	cons_ids_reason="no_emission_keep_prev"
	cons_log "CONS_IDS: keeping previous (no consensus emitted)"
else
	: > "$consolidated_ids_global"
	cons_ids_reason="no_emission_no_prev"
	cons_log "CONS_IDS: no consensus emitted and no previous IDs"
fi
if [ -n "$cons_ids_source" ]; then
	filter_consolidated_ids_by_dropped_keys "$cons_ids_source" "$consolidated_ids_global" "$consolidated_otu_keys_drop"
	if [ -s "$consolidated_otu_keys_drop" ]; then
		cons_log "CONS_IDS: filtered dropped OTU keys from consolidated IDs"
	fi
fi
# Post-filter finalization: if filtering emptied the global, the provisional
# source-based status no longer describes the effective output state.
if [ -n "$cons_ids_source" ] && [ -s "$cons_ids_source" ] && [ ! -s "$consolidated_ids_global" ]; then
	cons_log "CONS_IDS: dropped-key filtering emptied previously retained IDs (was $cons_ids_reason)"
	cons_ids_kept_previous=0
	if [ "$emitted_consensus_count" -gt 0 ]; then
		cons_ids_reason="emitted_no_prev"
	else
		cons_ids_reason="no_emission_no_prev"
	fi
fi
if [ -s "$consolidated_otu_keys_prev" ] || [ -s "$consolidated_otu_keys_current" ]; then
	filter_consolidated_key_tsv_inplace "$consolidated_otu_keys_prev"
	filter_consolidated_key_tsv_inplace "$consolidated_otu_keys_current"
	cat "$consolidated_otu_keys_prev" "$consolidated_otu_keys_current" > "${consolidated_otu_keys_global}.tmp"
	LC_ALL=C sort -u -o "$consolidated_otu_keys_global" "${consolidated_otu_keys_global}.tmp"
	rm -f "${consolidated_otu_keys_global}.tmp"
else
	: > "$consolidated_otu_keys_global"
fi
{
	printf "emitted_consensus_count\t%s\n" "$emitted_consensus_count"
	printf "merged_input_headers_total\t%s\n" "$merged_input_headers_total"
	printf "zero_emit_policy\t%s\n" "$zero_emit_policy"
	printf "id_mismatch_policy\t%s\n" "$id_mismatch_policy"
	printf "cache_below_min_policy\t%s\n" "$cache_below_min_policy"
	printf "id_mismatch_events\t%s\n" "$id_mismatch_events"
	printf "worker_failures_selector\t%s\n" "$worker_failures_selector"
printf "fallback_empty_sample_blast_count\t%s\n" "$fallback_empty_sample_blast_count"
	printf "kept_previous_ids\t%s\n" "$cons_ids_kept_previous"
	printf "reason\t%s\n" "$cons_ids_reason"
	} > "$consolidated_status"
echo "METRIC: worker_failures_selector=$worker_failures_selector" 1>&2
echo "METRIC: fallback_empty_sample_blast_count=$fallback_empty_sample_blast_count" 1>&2
_t_consolidated_finalize_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "consolidated_ids_finalize" "$_t_consolidated_finalize_start" "$_t_consolidated_finalize_end"
_t_finalize_end=$(timing_now)
append_timing_row "$phase_timings_raw_file" "global" "-" "aggregate_finalize" "$_t_finalize_start" "$_t_finalize_end"
sample_input_partition_ms=$(awk -F'\t' 'NR>1 && $4=="prefilter_partition" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
sample_worker_dispatch_ms=$(awk -F'\t' 'NR>1 && $4=="sample_worker_dispatch" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
sample_worker_compute_elapsed_ms=$(awk -F'\t' 'NR>1 && $4=="sample_worker_compute_elapsed" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
aggregate_finalize_ms=$(awk -F'\t' 'NR>1 && $4=="aggregate_finalize" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
counter_and_tmp_merge_ms=$(awk -F'\t' 'NR>1 && $4=="counter_and_tmp_merge" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
sample_output_merge_ms=$(awk -F'\t' 'NR>1 && $4=="sample_output_merge" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
sample_worker_compute_sum_ms=$(awk -F'\t' 'NR>1 && $2=="sample" && $4=="sample_total" && $6 !~ /[^0-9]/ {sum+=$6} END{print sum+0}' "$sample_phase_timings_file")
cache_lookup_and_restore_ms=$(awk -F'\t' 'NR>1 && $7 !~ /[^0-9]/ {sum+=$7} END{print sum+0}' "$cache_hydration_stats_file")
consensus_generation_ms=$(awk -F'\t' 'NR>1 && $2=="sample" && $4=="r_consensus" && $6 !~ /[^0-9]/ {sum+=$6} END{print sum+0}' "$sample_phase_timings_file")
consensus_postprocess_ms=$(awk -F'\t' 'NR>1 && $2=="sample" && $4=="merge_vsearch_refine" && $6 !~ /[^0-9]/ {sum+=$6} END{print sum+0}' "$sample_phase_timings_file")
phase_timings_tmp="${phase_timings_file}.tmp"
if ! printf 'round_id\tscope\tsample\tphase\tseconds\tms\n' > "$phase_timings_tmp" 2>/dev/null; then
	phase_timings_tmp=""
fi
phase_timings_out="${phase_timings_tmp:-$phase_timings_file}"
if [ -n "$phase_timings_tmp" ]; then
	:
else
	if ! printf 'round_id\tscope\tsample\tphase\tseconds\tms\n' > "$phase_timings_out" 2>/dev/null; then
		phase_timings_out="$phase_timings_file"
	fi
fi
startup_validation_and_state_ms=$(awk -F'\t' 'NR>1 && $4=="startup_validation_and_state" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
cpu_budget_and_scheduler_setup_ms=$(awk -F'\t' 'NR>1 && $4=="cpu_budget_and_scheduler_setup" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
consolidated_ids_finalize_ms=$(awk -F'\t' 'NR>1 && $4=="consolidated_ids_finalize" && $6 !~ /[^0-9]/ {print $6; exit} END{if (NR<=1) print 0}' "$phase_timings_raw_file")
append_duration_row "$phase_timings_out" "global" "-" "startup_validation_and_state" "$startup_validation_and_state_ms"
append_duration_row "$phase_timings_out" "global" "-" "sample_input_partition" "$sample_input_partition_ms"
append_duration_row "$phase_timings_out" "global" "-" "cpu_budget_and_scheduler_setup" "$cpu_budget_and_scheduler_setup_ms"
append_duration_row "$phase_timings_out" "global" "-" "sample_worker_dispatch" "$sample_worker_dispatch_ms"
append_duration_row "$phase_timings_out" "global" "-" "sample_worker_compute_elapsed" "$sample_worker_compute_elapsed_ms"
append_duration_row "$phase_timings_out" "global" "-" "counter_and_tmp_merge" "$counter_and_tmp_merge_ms"
append_duration_row "$phase_timings_out" "global" "-" "sample_output_merge" "$sample_output_merge_ms"
append_duration_row "$phase_timings_out" "worker_sum" "-" "sample_worker_compute_sum" "$sample_worker_compute_sum_ms"
append_duration_row "$phase_timings_out" "worker_sum" "-" "cache_lookup_and_restore" "$cache_lookup_and_restore_ms"
append_duration_row "$phase_timings_out" "worker_sum" "-" "consensus_generation" "$consensus_generation_ms"
append_duration_row "$phase_timings_out" "worker_sum" "-" "consensus_postprocess" "$consensus_postprocess_ms"
append_duration_row "$phase_timings_out" "global" "-" "consolidated_ids_finalize" "$consolidated_ids_finalize_ms"
append_duration_row "$phase_timings_out" "global" "-" "aggregate_finalize" "$aggregate_finalize_ms"
if [ -n "$phase_timings_tmp" ] && [ -f "$phase_timings_tmp" ]; then
	mv "$phase_timings_tmp" "$phase_timings_file" 2>/dev/null || true
fi
if [ "$emitted_consensus_count" -eq 0 ] && [ "$merged_input_headers_total" -gt 0 ] && [ "$zero_emit_policy" = "fail" ]; then
	echo "ERROR: No consensus emitted despite merged inputs (merged_input_headers_total=$merged_input_headers_total)" 1>&2
	exit 1
fi
rm -f "$consolidated_ids_prev"
rm -f "$consolidated_ids_current"
rm -f "$consolidated_otu_keys_prev"
rm -f "$consolidated_otu_keys_current"
rm -f "$consolidated_otu_keys_drop"
rm -f "$lock_reset_list"
rm -f "$sup_index"

rm -f tmp_clean_blast_report_full.txt
