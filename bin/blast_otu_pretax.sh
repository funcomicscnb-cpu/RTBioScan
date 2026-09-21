#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob
export LC_ALL=C

if [ "$#" -ne 16 ]; then
	echo "Usage: blast_otu_pretax.sh BARCODE BLAST_INPUT_FASTA STATE_DIR THREADS BASE_DIR DB_DIR TAXDB_DIR ROUND_BC_DIR TARGETS BLAST_DBS ID_FAMILY ID_GENUS ID_SPEC MEMTAX BLAST_EVALUE BLAST_MAX_HSPS" >&2
	exit 1
fi

BARCODE="$1"
BLAST_INPUT_FASTA="$2"
STATE_DIR="$3"
THREADS="$4"
BASE_DIR="$5"
DB_DIR="$6"
TAXDB_DIR="$7"
ROUND_BC_DIR="$8"
TARGETS_RAW="$9"
BLAST_DBS_RAW="${10}"
ID_FAMILY_RAW="${11}"
ID_GENUS_RAW="${12}"
ID_SPEC_RAW="${13}"
MEMTAX_RAW="${14}"
BLAST_EVALUE="${15}"
BLAST_MAX_HSPS="${16}"

export BLASTDB="$TAXDB_DIR"

source "${BASE_DIR}/bin/lib/db_sig_utils.sh"

IFS='|' read -ra TARGETS <<< "$TARGETS_RAW"
IFS='|' read -ra BLAST_DBS <<< "$BLAST_DBS_RAW"
IFS='|' read -ra ID_FAMILY <<< "$ID_FAMILY_RAW"
IFS='|' read -ra ID_GENUS <<< "$ID_GENUS_RAW"
IFS='|' read -ra ID_SPEC <<< "$ID_SPEC_RAW"
IFS='|' read -ra MEMTAX <<< "$MEMTAX_RAW"

WORD_SIZE=50
QCOV=50

mkdir -p "$STATE_DIR" "$ROUND_BC_DIR"
WORK_DIR="$(pwd)"

if [[ "$THREADS" == *[!0-9]* ]] || [ "$THREADS" -lt 1 ]; then
	THREADS=1
fi

run_target_worker() {
	local idx0="$1"
	local worker_threads="$2"
	local target="${TARGETS[$idx0]}"
	local db_path="${DB_DIR}${BLAST_DBS[$idx0]}"
	local memtax_src="${MEMTAX[$idx0]:-}"
	local id_fam="${ID_FAMILY[$idx0]}"
	local id_gen="${ID_GENUS[$idx0]}"
	local id_spec="${ID_SPEC[$idx0]}"
	local idx=$(( idx0 + 1 ))
	local memtax_file="${STATE_DIR}/memtax${idx}.txt"
	local target_fasta="${BARCODE}_${target}.fasta"
	local preblast_cached="${BARCODE}_preblast_cached${idx}.txt"
	local preblast_new_fasta="${BARCODE}_preblast_new${idx}.fasta"
	local preblast_hash_new="${BARCODE}_preblast_hash_new${idx}.tsv"
	local preblast_new_txt="${BARCODE}_preblast_new${idx}.txt"
	local preblast_report="${BARCODE}_preblastreport${idx}.txt"
	local blast_report="${BARCODE}_blastreport${idx}.txt"
	local blast_report_adj="${BARCODE}_blastreport${idx}_adj.txt"
	local cache_path="${STATE_DIR}/otu_blast_cache_${target}.tsv"
	local cache_meta_path="${STATE_DIR}/otu_blast_cache_${target}.meta"
	local cache_key
	local target_tmp_dir
	local taxdepth_status

	: > "$preblast_cached"
	: > "$preblast_new_fasta"
	: > "$preblast_hash_new"
	: > "$preblast_new_txt"
	: > "$preblast_report"
	: > "$blast_report"
	: > "$blast_report_adj"
	rm -f "${blast_report_adj}.state" "${blast_report_adj}.target" "${blast_report_adj}."*.pending

	if ! seqkit grep -r -p "\\|${target}\\|" "$BLAST_INPUT_FASTA" > "$target_fasta"; then
		echo "ERROR: failed to extract BLAST input reads for target $target" >&2
		exit 1
	fi
	if [ ! -s "$target_fasta" ]; then
		return 0
	fi

	if [ -n "$memtax_src" ] && [ "$memtax_src" != "null" ]; then
		case "$memtax_src" in /*) ;; *) memtax_src="${BASE_DIR}/${memtax_src}" ;; esac
	fi
	# Content signatures bind both caches to the actual pinned taxonomy/reference.
	# All persistent replacement is deferred until BLAST and taxonomy validate.
	local cache_tool="${BASE_DIR}/bin/cache_blast_by_hash.pl"
	local signatures
	local ref_sig
	local tax_sig
	local subject_count
	signatures="$("$cache_tool" --signature "$db_path" "${TAXONKIT_DB:-}" \
		"idfam=$id_fam" "idgen=$id_gen" "idspec=$id_spec" "evalue=$BLAST_EVALUE" \
		"maxhsps=$BLAST_MAX_HSPS" "word=$WORD_SIZE" "qcov=$QCOV" "target=$target" "seed=$memtax_src")"
	IFS=$'\t' read -r cache_key ref_sig tax_sig <<< "$signatures"
	# A limit smaller than the database can conceal genuine equal-best subjects.
	subject_count="$(blastdbcmd -db "$db_path" -info | perl -ne \
		'if (/([\d,]+) sequences;/) { $n=$1; $n=~s/,//g; print "$n\n"; $seen++ } END { die "Invalid BLAST database sequence count\n" unless $seen==1 && $n>0 && $n<=2147483647 }')"
	target_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/rtbioscan_blast_taxdepth_${idx}.XXXXXX")"
	# The worker is already a subshell; this trap cannot affect another target.
	trap 'rm -rf "$target_tmp_dir"' EXIT
	local pending="${target_tmp_dir}/r4"
	if (
		cd "$target_tmp_dir"
		"$cache_tool" --worker --depth-helper "${BASE_DIR}/bin/get_blast_taxdepth.pl" "$WORK_DIR/$target_fasta" "$cache_path" "$cache_key" "$pending" \
			"$memtax_file" "$id_spec" "$id_gen" "$memtax_src" "$target" \
			blastn -db "$db_path" -num_threads "$worker_threads" -task megablast -dust no \
			-outfmt "6 qseqid sseqid staxids evalue length pident bitscore qstart qend sstart send qlen" \
			-perc_identity "$id_fam" -evalue "$BLAST_EVALUE" -max_hsps "$BLAST_MAX_HSPS" \
			-max_target_seqs "$subject_count" -word_size "$WORD_SIZE" -qcov_hsp_perc "$QCOV" -mt_mode 2
	); then
		:
	else
		taxdepth_status=$?
		echo "ERROR: BLAST/taxonomy generation failed for target $target (exit $taxdepth_status)" >&2
		exit 1
	fi
	cp "${pending}.fasta" "$preblast_new_fasta"
	cp "${pending}.raw" "$preblast_new_txt"
	cp "${pending}.preblast" "$preblast_report"
	cp "${pending}.report" "$blast_report_adj"
	cp "${pending}.manifest" "${blast_report_adj}.manifest"
	# Stage all targets first. A failure in any worker must publish no cache.
	cp "${pending}.memtax" "${blast_report_adj}.memtax.pending"
	cp "${pending}.cache" "${blast_report_adj}.cache.pending"
	cp "${pending}.evidence" "${blast_report_adj}.evidence.pending"
	cp "${pending}.all-evidence" "${blast_report_adj}.all-evidence.pending"
	cp "${pending}.all-report" "${blast_report_adj}.state"
	printf '%s\n' "$target" > "${blast_report_adj}.target"
	rm -rf "$target_tmp_dir"
	trap - EXIT
	if [ -s "$preblast_report" ]; then
		echo "Blast analysis for ${target} is successful" 1>&2
	fi

}

active_indices=()
for idx0 in "${!TARGETS[@]}"; do
	idx=$(( idx0 + 1 ))
	: > "${BARCODE}_preblastreport${idx}.txt"
	: > "${BARCODE}_blastreport${idx}_adj.txt"
	rm -f "${BARCODE}_blastreport${idx}_adj.txt.state" "${BARCODE}_blastreport${idx}_adj.txt.target"
	target="${TARGETS[$idx0]}"
	if [ -n "$target" ] && [ "$target" != "null" ]; then
		if grep -qF "|${target}|" "$BLAST_INPUT_FASTA" 2>/dev/null; then
			active_indices+=( "$idx0" )
		fi
	fi
done

active_count="${#active_indices[@]}"
if [ "$active_count" -gt 0 ]; then
	concurrency="$THREADS"
	if [ "$active_count" -lt "$concurrency" ]; then
		concurrency="$active_count"
	fi
	if [ "$concurrency" -lt 1 ]; then
		concurrency=1
	fi

	offset=0
	while [ "$offset" -lt "$active_count" ]; do
		remaining=$(( active_count - offset ))
		batch_size="$concurrency"
		if [ "$remaining" -lt "$batch_size" ]; then
			batch_size="$remaining"
		fi
		base_threads=$(( THREADS / batch_size ))
		remainder=$(( THREADS % batch_size ))
		pids=()
		for (( slot=0; slot<batch_size; slot++ )); do
			worker_threads="$base_threads"
			if [ "$slot" -lt "$remainder" ]; then
				worker_threads=$(( worker_threads + 1 ))
			fi
			if [ "$worker_threads" -lt 1 ]; then
				worker_threads=1
			fi
			run_target_worker "${active_indices[$(( offset + slot ))]}" "$worker_threads" &
			pids+=( "$!" )
		done
		batch_failed=0
		for pid in "${pids[@]}"; do
			if ! wait "$pid"; then
				batch_failed=1
			fi
		done
		if [ "$batch_failed" -ne 0 ]; then
			exit 1
		fi
		offset=$(( offset + batch_size ))
	done
fi

: > "${BARCODE}_preblastreport_join.txt"
: > "${BARCODE}_blastreport_join.txt"
: > "${BARCODE}_blastreport_state_r4.txt"
: > "${BARCODE}_blastreport_targets_r4.txt"
for idx0 in "${!TARGETS[@]}"; do
	idx=$(( idx0 + 1 ))
	preblast_report="${BARCODE}_preblastreport${idx}.txt"
	blast_report_adj="${BARCODE}_blastreport${idx}_adj.txt"
	if [ -f "$preblast_report" ]; then
		cat "$preblast_report" >> "${BARCODE}_preblastreport_join.txt"
	fi
	if [ -f "${blast_report_adj}.state" ]; then
		target="${TARGETS[$idx0]}"
		cache_tool="${BASE_DIR}/bin/cache_blast_by_hash.pl"
		"$cache_tool" --publish-ready "${blast_report_adj}.memtax.pending" "${STATE_DIR}/memtax${idx}.txt" "${blast_report_adj}.manifest" memtax
		"$cache_tool" --publish-ready "${blast_report_adj}.cache.pending" "${STATE_DIR}/otu_blast_cache_${target}.tsv" "${blast_report_adj}.manifest" cache
		"$cache_tool" --publish-ready "${blast_report_adj}.evidence.pending" "${ROUND_BC_DIR}/${BARCODE}_blast_evidence_${target}.tsv" "${blast_report_adj}.manifest" evidence
		"$cache_tool" --publish-ready "${blast_report_adj}.all-evidence.pending" "${STATE_DIR}/otu_blast_evidence_${target}.tsv" "${blast_report_adj}.manifest" all-evidence
		cp "${STATE_DIR}/memtax${idx}.txt" "${ROUND_BC_DIR}/${BARCODE}_memtax${idx}.txt"
		cat "${blast_report_adj}.state" >> "${BARCODE}_blastreport_state_r4.txt"
		cat "${blast_report_adj}.target" >> "${BARCODE}_blastreport_targets_r4.txt"
	fi
	if [ -f "$blast_report_adj" ]; then
		cat "$blast_report_adj" >> "${BARCODE}_blastreport_join.txt"
	fi
done

cp "${BARCODE}_blastreport_join.txt" "${BARCODE}_blastreport_round.txt"
