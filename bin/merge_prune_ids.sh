#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat 1>&2 <<'USAGE'
Usage:
  merge_prune_ids.sh --out-list <path> --out-stats <path> [--source <name> <list>]...

Merges per-category prune ID lists into one deduplicated base-read-id list.
Each source list may contain raw read IDs, full FASTA header IDs ("|"-decorated),
or tab-delimited rows (first column used); only the base ID before the first "|"
is extracted.
USAGE
	exit 2
}

out_list=""
out_stats=""
names=()
files=()

while [ "$#" -gt 0 ]; do
	case "$1" in
		--out-list)
			[ "$#" -ge 2 ] || usage
			out_list="$2"
			shift 2
			;;
		--out-stats)
			[ "$#" -ge 2 ] || usage
			out_stats="$2"
			shift 2
			;;
		--source)
			[ "$#" -ge 3 ] || usage
			names+=( "$2" )
			files+=( "$3" )
			shift 3
			;;
		*)
			usage
			;;
	esac
done

[ -n "$out_list" ] || usage
[ -n "$out_stats" ] || usage

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/merge_prune_ids.XXXXXX")"
cleanup() {
	rm -rf "$tmp_dir"
}
trap cleanup EXIT

union_tmp="$tmp_dir/union.tmp"
: > "$union_tmp"

stats_tmp="$tmp_dir/stats.tmp"
: > "$stats_tmp"

idx=0
while [ "$idx" -lt "${#names[@]}" ]; do
	name="${names[$idx]}"
	src="${files[$idx]}"
	src_norm="$tmp_dir/${name}.norm"
	if [ -s "$src" ]; then
		awk '
			{
				gsub(/\r/, "", $0);
				sub(/^[[:space:]]+/, "", $0);
				sub(/[[:space:]]+$/, "", $0);
				if ($0 == "") next;
				split($0, t, "\t"); $0 = t[1];
				split($0, a, "|");
				if (a[1] != "") print a[1];
			}
		' "$src" | LC_ALL=C sort -u > "$src_norm"
	else
		: > "$src_norm"
	fi
	cat "$src_norm" >> "$union_tmp"
	count="$(wc -l < "$src_norm" | tr -d ' ')"
	printf "%s_candidates\t%s\n" "$name" "${count:-0}" >> "$stats_tmp"
	idx=$((idx + 1))
done

LC_ALL=C sort -u "$union_tmp" > "$out_list"
total_candidates="$(wc -l < "$out_list" | tr -d ' ')"

{
	printf "total_candidates\t%s\n" "${total_candidates:-0}"
	cat "$stats_tmp"
} > "$out_stats"
