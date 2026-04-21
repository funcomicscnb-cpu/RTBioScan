#!/bin/bash
# consensus_prune_apply.sh — post-consensus prune with recovery subtraction.
#
# Subtracts reads contributing to BLAST-assigned consensus sequences (recovery
# IDs) from the per-round prune list, then atomically applies the result to
# the accumulated FASTA under an exclusive lock.
#
# Lock timeout exits non-zero (fatal), preserving the "apply or abort" policy.
#
# Usage:
#   consensus_prune_apply.sh [options]
#
# Required options:
#   --prune-ids      <file>  Merged prune IDs list from blast_OTU_pretax.
#   --recovery-ids   <file>  IDs of reads used by BLAST-assigned OTUs.
#   --fasta          <file>  Accumulated FASTA pruned in-place.
#   --fasta-tmp      <file>  Temp path for atomic FASTA swap.
#   --apply-stats    <file>  Per-run apply stats (written by reads_apply_prune_ids.pl).
#   --prune-stats    <file>  Cumulative stats file (key=value rows appended).
#   --round-cp       <file>  Round-scoped destination for updated FASTA.
#   --lock-dir       <path>  Lock base path; lock acquired via mkdir <path>.lockdir.
#   --apply-script   <path>  Path to reads_apply_prune_ids.pl.
#
# Optional options:
#   --apply-last     <file>  Persistent snapshot copy of apply-stats.
#   --final-last     <file>  Persistent snapshot of the post-recovery applied list.
#   --c1-prune-ids   <file>  Round-local C1 prune IDs list (excluded from barrier).
#   --pruned-barrier <file>  Persistent cumulative blocklist (union-updated each round).
#   --pruned-archive <file>  Persistent FASTA archive of applied non-C1 pruned reads.
#   --lock-wait      <N>     Max seconds to wait for lock (default: 60).

set -euo pipefail

usage() {
	sed -n '/^# Usage:/,/^[^#]/{s/^# \{0,1\}//;/^[^#]/d;p}' "$0"
}

update_pruned_barrier() {
	local barrier_file="$1"
	local final_prune_file="$2"
	local c1_prune_file="$3"
	local stats_file="$4"
	local barrier_source="$final_prune_file"
	local barrier_round_file="${barrier_file}.round"
	local barrier_new_file="${barrier_file}.new"
	local barrier_non_c1_file="${final_prune_file%.list}.non_c1.list"
	local n_barrier_round=0
	local n_barrier_cumulative=0

	if [[ -s "$c1_prune_file" && -s "$final_prune_file" ]]; then
		barrier_source="$barrier_non_c1_file"
		LC_ALL=C comm -23 \
			<(LC_ALL=C sort "$final_prune_file") \
			<(LC_ALL=C sort "$c1_prune_file") > "$barrier_source"
	fi

	if [[ -s "$barrier_source" ]]; then
		LC_ALL=C sort -u "$barrier_source" > "$barrier_round_file"
	else
		: > "$barrier_round_file"
	fi

	if [[ -s "$barrier_round_file" ]]; then
		if [[ -s "$barrier_file" ]]; then
			n_barrier_round=$(LC_ALL=C comm -23 \
				<(LC_ALL=C sort -u "$barrier_round_file") \
				<(LC_ALL=C sort -u "$barrier_file") | wc -l | tr -d ' ')
			LC_ALL=C sort -u "$barrier_file" "$barrier_round_file" > "$barrier_new_file"
		else
			n_barrier_round=$(wc -l < "$barrier_round_file" | tr -d ' ')
			cp "$barrier_round_file" "$barrier_new_file"
		fi
	elif [[ -s "$barrier_file" ]]; then
		cp "$barrier_file" "$barrier_new_file"
	else
		: > "$barrier_new_file"
	fi

	mv "$barrier_new_file" "$barrier_file"
	if [[ -s "$barrier_file" ]]; then
		n_barrier_cumulative=$(wc -l < "$barrier_file" | tr -d ' ')
	fi

	rm -f "$barrier_round_file"
	printf 'pruned_barrier_added_round\t%s\n' "$n_barrier_round" >> "$stats_file"
	printf 'pruned_barrier_cumulative\t%s\n' "$n_barrier_cumulative" >> "$stats_file"
}

prune_ids=''      recovery_ids=''     fasta=''        fasta_tmp=''
apply_stats=''    apply_last=''       final_last=''   prune_stats=''  round_cp=''
lock_dir=''       lock_wait=60        apply_script=''
c1_prune_ids='' pruned_barrier='' pruned_archive=''

while [[ $# -gt 0 ]]; do
	case "$1" in
		--prune-ids)    prune_ids="$2";    shift 2 ;;
		--recovery-ids) recovery_ids="$2"; shift 2 ;;
		--fasta)        fasta="$2";        shift 2 ;;
		--fasta-tmp)    fasta_tmp="$2";    shift 2 ;;
		--apply-stats)  apply_stats="$2";  shift 2 ;;
		--apply-last)   apply_last="$2";   shift 2 ;;
		--final-last)   final_last="$2";   shift 2 ;;
		--prune-stats)  prune_stats="$2";  shift 2 ;;
		--round-cp)     round_cp="$2";     shift 2 ;;
		--lock-dir)     lock_dir="$2";     shift 2 ;;
		--lock-wait)    lock_wait="$2";    shift 2 ;;
		--apply-script) apply_script="$2"; shift 2 ;;
		--c1-prune-ids)   c1_prune_ids="$2";   shift 2 ;;
		--pruned-barrier) pruned_barrier="$2"; shift 2 ;;
		--pruned-archive) pruned_archive="$2"; shift 2 ;;
		--help|-h) usage; exit 0 ;;
		*) echo "ERROR: unknown argument '$1'" >&2; usage >&2; exit 2 ;;
	esac
done

for _req in prune_ids recovery_ids fasta fasta_tmp apply_stats prune_stats round_cp lock_dir apply_script; do
	[[ -n "${!_req}" ]] || { echo "ERROR: --${_req//_/-} is required" >&2; exit 2; }
done

# Nothing to prune.
[[ -s "$prune_ids" ]] || exit 0

# --- Subtract recovery IDs from prune list ---
final_prune="${prune_ids%.list}.final.list"
if [[ -s "$recovery_ids" ]]; then
	LC_ALL=C comm -23 \
		<(LC_ALL=C sort "$prune_ids") \
		<(LC_ALL=C sort -u "$recovery_ids") > "$final_prune"
	n_recovered=$(LC_ALL=C comm -12 \
		<(LC_ALL=C sort "$prune_ids") \
		<(LC_ALL=C sort -u "$recovery_ids") | wc -l | tr -d ' ')
else
	cp "$prune_ids" "$final_prune"
	n_recovered=0
fi
printf 'recovered_intersection_count\t%s\n' "$n_recovered"               >> "$prune_stats"
printf 'final_prune_count\t%s\n'            "$(wc -l < "$final_prune" | tr -d ' ')" >> "$prune_stats"
[[ -z "$final_last" ]] || cp "$final_prune" "$final_last" 2>/dev/null || true

# Nothing left after recovery subtraction.
[[ -s "$final_prune" ]] || exit 0

# --- Acquire exclusive lock (fatal on timeout) ---
_lockdir="${lock_dir}.lockdir"
_lock_acquired=0
acquire() {
	local waited=0
	while ! mkdir "$_lockdir" 2>/dev/null; do
		sleep 1; waited=$((waited + 1))
		[[ "$waited" -lt "$lock_wait" ]] || {
			echo "ERROR: timed out acquiring QCED lock for post-consensus prune" >&2; exit 1
		}
	done
	_lock_acquired=1
}
release() { [[ "$_lock_acquired" -eq 0 ]] || rmdir "$_lockdir" 2>/dev/null || true; }
trap release EXIT

acquire

archive_pruned_reads() {
	local archive_file="$1"
	local source_ids="$2"
	local fasta_in="$3"
	local non_c1_ids="${source_ids%.list}.archive_non_c1.list"
	local archive_tmp="${archive_file}.tmp.$$"

	if [[ -s "$c1_prune_file" && -s "$source_ids" ]]; then
		LC_ALL=C comm -23 \
			<(LC_ALL=C sort -u "$source_ids") \
			<(LC_ALL=C sort -u "$c1_prune_file") > "$non_c1_ids"
	else
		cp "$source_ids" "$non_c1_ids"
	fi

	if [[ ! -s "$non_c1_ids" || ! -s "$fasta_in" ]]; then
		rm -f "$non_c1_ids"
		return 0
	fi

	mkdir -p "$(dirname "$archive_file")"
	if [[ -f "$archive_file" ]]; then
		cp "$archive_file" "$archive_tmp"
	else
		: > "$archive_tmp"
	fi

	awk 'NR==FNR { ids[$1]=1; next }
	     /^>/ { uuid=substr($0,2); sub(/[|].*/,"",uuid); keep=(uuid in ids); if(keep)print; next }
	     keep { print }' \
		"$non_c1_ids" "$fasta_in" >> "$archive_tmp"

	awk '/^>/ {
	         id=substr($0,2); sub(/[|].*/,"",id);
	         if (seen[id]++) { keep=0; next }
	         keep=1; print; next
	     }
	     keep { print }' "$archive_tmp" > "${archive_tmp}.dedup"
	mv "${archive_tmp}.dedup" "$archive_file"
	rm -f "$archive_tmp" "$non_c1_ids"
}

# --- Apply prune ---
if perl "$apply_script" "$fasta" "$final_prune" "$fasta_tmp" "$apply_stats"; then
	if [[ -n "$pruned_archive" ]]; then
		c1_prune_file="${c1_prune_ids:-}"
		archive_pruned_reads "$pruned_archive" "$final_prune" "$fasta"
	fi
	mv "$fasta_tmp" "$fasta" || { echo "ERROR: mv after prune apply failed" >&2; exit 1; }
	# O1: rebuild .fai index so consensus can use indexed random-access extraction after pruning.
	# Delete first: a failed rebuild must leave no stale index (which would cause silent wrong-offset reads).
	rm -f "${fasta}.fai"
	command -v samtools >/dev/null 2>&1 && samtools faidx "$fasta" 2>/dev/null || true
	sed 's/^/apply_/' "$apply_stats" >> "$prune_stats" || true
	[[ -z "$apply_last" ]] || cp "$apply_stats" "$apply_last" 2>/dev/null || true

	# The cumulative barrier excludes C1 archive/drop IDs because those are tracked
	# separately from the rolling-pool suppression list.
	if [[ -n "$pruned_barrier" ]]; then
		update_pruned_barrier "$pruned_barrier" "$final_prune" "$c1_prune_ids" "$prune_stats"
	fi
else
	echo "ERROR: post-consensus prune apply failed" >&2; exit 1
fi
cp "$fasta" "$round_cp" 2>/dev/null || true
