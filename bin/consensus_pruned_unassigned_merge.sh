#!/bin/bash

set -euo pipefail

usage() {
	cat <<'EOF'
Usage: consensus_pruned_unassigned_merge.sh --round-list <file> --state-dir <dir> --barcode <id>

Merges round-scoped pruned-unassigned read IDs into the cumulative state list.
EOF
}

round_list=""
state_dir=""
barcode=""

while [[ $# -gt 0 ]]; do
	case "$1" in
		--round-list)
			round_list="$2"
			shift 2
			;;
		--state-dir)
			state_dir="$2"
			shift 2
			;;
		--barcode)
			barcode="$2"
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

if [[ -z "$round_list" || -z "$state_dir" || -z "$barcode" ]]; then
	echo "ERROR: --round-list, --state-dir, and --barcode are required" 1>&2
	usage 1>&2
	exit 2
fi

mkdir -p "$state_dir"
all_list="${state_dir}/${barcode}_pruned_unassigned_reads_all.list"
merge_tmp="${all_list}.merge"

if [[ -f "$all_list" ]]; then
	cat "$round_list" "$all_list" > "$merge_tmp"
else
	cat "$round_list" > "$merge_tmp"
fi

awk 'NF' "$merge_tmp" | LC_ALL=C sort -u > "${all_list}.tmp" && mv "${all_list}.tmp" "$all_list"
rm -f "$merge_tmp"
