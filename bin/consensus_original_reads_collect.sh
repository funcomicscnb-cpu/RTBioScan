#!/bin/bash

set -euo pipefail

usage() {
	cat <<'EOF'
Usage: consensus_original_reads_collect.sh --consensus-dir <dir> --round-dir <dir> --keep <0|1> --provenance <file>

Collects per-consensus OriginalReads lists into a round-scoped location when keep=1.
When keep=1, removes OriginalReads from the consensus directory after successful copy.
EOF
}

consensus_dir=""
round_dir=""
provenance=""
keep_raw=""

while [[ $# -gt 0 ]]; do
	case "$1" in
		--consensus-dir)
			consensus_dir="$2"
			shift 2
			;;
		--round-dir)
			round_dir="$2"
			shift 2
			;;
		--provenance)
			provenance="$2"
			shift 2
			;;
		--keep)
			keep_raw="$2"
			shift 2
			;;
		--help|-h)
			usage
			exit 0
			;;
		*)
			echo "ERROR: Unknown argument '$1'" 1>&2
			usage 1>&2
			exit 2
			;;
	esac
done

if [[ -z "$consensus_dir" || -z "$round_dir" || -z "$provenance" ]]; then
	echo "ERROR: --consensus-dir, --round-dir, and --provenance are required" 1>&2
	usage 1>&2
	exit 2
fi
if [[ ! -d "$consensus_dir" ]]; then
	exit 0
fi

keep_flag=0
if [[ -n "$keep_raw" ]]; then
	keep_norm="$(printf '%s' "$keep_raw" | tr '[:upper:]' '[:lower:]')"
	case "$keep_norm" in
		1|true|yes|y|on) keep_flag=1 ;;
		0|false|no|n|off) keep_flag=0 ;;
		*) keep_flag=0 ;;
	esac
fi

if [[ "$keep_flag" -eq 1 ]]; then
	if [[ ! -s "$provenance" ]]; then
		echo "WARN: consensus_original_reads_collect:skip_empty_provenance:${provenance}" 1>&2
		exit 0
	fi
	dest_root="${round_dir}/Consensus"
	manifest="${dest_root}/consensus_original_reads_manifest.tsv"
	manifest_tmp="${manifest}.tmp"
	mkdir -p "$dest_root"
	printf 'consensus_id\treads_list_path\tcompressed\tread_count\n' > "$manifest_tmp"
	header_read=0
	idx_cons=-1
	idx_sample=-1
	found=0
	while IFS= read -r line; do
		line="${line%$'\r'}"
		if [[ "$header_read" -eq 0 ]]; then
			header_read=1
			IFS=$'\t' read -r -a cols <<< "$line"
			for i in "${!cols[@]}"; do
				case "${cols[$i]}" in
					consensus_id) idx_cons=$i ;;
					sample) idx_sample=$i ;;
				esac
			done
			if [[ "$idx_cons" -lt 0 || "$idx_sample" -lt 0 ]]; then
				echo "WARN: consensus_original_reads_collect:invalid_provenance_header:${provenance}" 1>&2
				rm -f "$manifest_tmp"
				exit 0
			fi
			continue
		fi
		[[ -z "$line" ]] && continue
		IFS=$'\t' read -r -a fields <<< "$line"
		if [[ "$idx_cons" -ge "${#fields[@]}" || "$idx_sample" -ge "${#fields[@]}" ]]; then
			continue
		fi
		cons_id="${fields[$idx_cons]}"
		sample="${fields[$idx_sample]}"
		[[ -z "$cons_id" || -z "$sample" ]] && continue
		base="$cons_id"
		if [[ "$base" == *"_${sample}" ]]; then
			base="${base%_${sample}}"
		fi
		list_src="${consensus_dir}/${sample}/OriginalReads/${base}_reads.list"
		if [[ -s "$list_src" ]]; then
			dest_dir="${dest_root}/${sample}/OriginalReads"
			mkdir -p "$dest_dir"
			dest="${dest_dir}/${base}_reads.list"
			cp "$list_src" "$dest"
			read_count=$(awk 'END{print NR+0}' "$dest")
			printf '%s\t%s\t0\t%s\n' "$cons_id" "$dest" "$read_count" >> "$manifest_tmp"
			found=$((found + 1))
		fi
	done < "$provenance"

	if [[ "$found" -eq 0 ]]; then
		echo "WARN: consensus_original_reads_collect:skip_no_matching_lists:${provenance}" 1>&2
		rm -f "$manifest_tmp"
		exit 0
	fi
	mv "$manifest_tmp" "$manifest"

	while IFS= read -r -d '' orig_dir; do
		rm -rf "$orig_dir"
	done < <(find "$consensus_dir" -type d -name "OriginalReads" -print0 2>/dev/null || true)
fi
