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

	if [ -n "$memtax_src" ] && [ -f "${BASE_DIR}/${memtax_src}" ] && [ ! -f "$memtax_file" ]; then
		cp "${BASE_DIR}/${memtax_src}" "$memtax_file"
	fi

	if ! seqkit grep -r -p "\\|${target}\\|" "$BLAST_INPUT_FASTA" > "$target_fasta"; then
		echo "ERROR: failed to extract BLAST input reads for target $target" >&2
		exit 1
	fi

	if [ ! -s "$target_fasta" ]; then
		return 0
	fi

	cache_key="db=${db_path}|sig=$(db_sig "${db_path}")|taxdb=${TAXDB_DIR}|taxsig=$(dir_sig "${TAXDB_DIR}")|idfam=${id_fam}|evalue=${BLAST_EVALUE}|maxhsps=${BLAST_MAX_HSPS}|word=${WORD_SIZE}|qcov=${QCOV}|target=${target}"
	if [ ! -f "$cache_meta_path" ] || [ "$(cat "$cache_meta_path" 2>/dev/null)" != "$cache_key" ]; then
		: > "$cache_path"
		printf '%s\n' "$cache_key" > "$cache_meta_path"
	fi
	[ -f "$cache_path" ] || : > "$cache_path"

	"${BASE_DIR}/bin/cache_blast_by_hash.pl" \
		"$target_fasta" \
		"$cache_path" \
		"$preblast_cached" \
		"$preblast_new_fasta" \
		"$preblast_hash_new"

	if [ -s "$preblast_new_fasta" ]; then
		if blastn \
			-query "$preblast_new_fasta" \
			-db "$db_path" \
			-num_threads "$worker_threads" \
			-task megablast \
			-dust no \
			-outfmt "10 qseqid sseqid evalue length pident" \
			-perc_identity "$id_fam" \
			-evalue "$BLAST_EVALUE" \
			-max_hsps "$BLAST_MAX_HSPS" \
			-max_target_seqs 1 \
			-word_size "$WORD_SIZE" \
			-qcov_hsp_perc "$QCOV" \
			-mt_mode 2 > "$preblast_new_txt"; then
			:
		else
			echo "ERROR: blastn failed for target $target (exit $?)" >&2
			exit 1
		fi
	fi

	if [ -s "$preblast_hash_new" ] && [ -s "$preblast_new_txt" ]; then
		awk -F'\t' 'NR==FNR{h[$1]=$2; next} {split($0,a,","); if (a[1] in h) print h[a[1]] "\t" a[2] "\t" a[3] "\t" a[4] "\t" a[5];}' \
			"$preblast_hash_new" \
			"$preblast_new_txt" >> "$cache_path"
		awk -F'\t' '{line[$1]=$0} END{for (k in line) print line[k]}' "$cache_path" \
			| LC_ALL=C sort > "${cache_path}.tmp" \
			&& mv "${cache_path}.tmp" "$cache_path"
	fi

	if [ -s "$preblast_cached" ] || [ -s "$preblast_new_txt" ]; then
		cat "$preblast_cached" "$preblast_new_txt" > "$preblast_report"
	fi

	if [ -s "$preblast_report" ]; then
		sed 's/,/;/g' "$preblast_report" | sed -E 's/\;\S+\|\S+\|/\;/' > "$blast_report"
		target_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/rtbioscan_blast_taxdepth_${idx}.XXXXXX")"
		if (
			cd "$target_tmp_dir"
			"${BASE_DIR}/bin/get_blast_taxdepth.pl" "${WORK_DIR}/${blast_report}" "$id_spec" "$id_gen" "$memtax_file" > "${WORK_DIR}/${blast_report_adj}"
		); then
			:
		else
			taxdepth_status=$?
			rm -rf "$target_tmp_dir"
			echo "ERROR: get_blast_taxdepth.pl failed for target $target (exit $taxdepth_status)" >&2
			exit 1
		fi
		rm -rf "$target_tmp_dir"
		cp "$memtax_file" "${ROUND_BC_DIR}/${BARCODE}_memtax${idx}.txt" 2>/dev/null || true
		echo "Blast analysis for ${target} is successful" 1>&2
	fi
}

active_indices=()
for idx0 in "${!TARGETS[@]}"; do
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
for idx0 in "${!TARGETS[@]}"; do
	idx=$(( idx0 + 1 ))
	preblast_report="${BARCODE}_preblastreport${idx}.txt"
	blast_report_adj="${BARCODE}_blastreport${idx}_adj.txt"
	if [ -f "$preblast_report" ]; then
		cat "$preblast_report" >> "${BARCODE}_preblastreport_join.txt"
	fi
	if [ -f "$blast_report_adj" ]; then
		cat "$blast_report_adj" >> "${BARCODE}_blastreport_join.txt"
	fi
done

cp "${BARCODE}_blastreport_join.txt" "${BARCODE}_blastreport_round.txt"
