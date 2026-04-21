#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat 1>&2 <<'USAGE'
Usage:
  consensus_assign_depth.sh --report <preblastreport.txt> \
    --family <thr> --genus <thr> --species <thr>

Resolves assignment depth for each BLAST hit using pident thresholds.

Input --report: comma-separated raw BLAST (qseqid,sseqid,evalue,length,pident).
Output (stdout): qseqid<TAB>level, one row per input hit.
Level is one of: species, genus, family, unassigned.

Missing, non-numeric, or below-family pident => unassigned (no leak path).
Callers are responsible for deduplication across multiple --report invocations.
USAGE
	exit 2
}

report=""
thr_family=""
thr_genus=""
thr_species=""

while [ "$#" -gt 0 ]; do
	case "$1" in
		--report)  [ "$#" -ge 2 ] || usage; report="$2";     shift 2 ;;
		--family)  [ "$#" -ge 2 ] || usage; thr_family="$2"; shift 2 ;;
		--genus)   [ "$#" -ge 2 ] || usage; thr_genus="$2";  shift 2 ;;
		--species) [ "$#" -ge 2 ] || usage; thr_species="$2"; shift 2 ;;
		*) usage ;;
	esac
done

[ -n "$report" ] && [ -n "$thr_family" ] && [ -n "$thr_genus" ] && [ -n "$thr_species" ] || usage

validate_threshold() {
	local name="$1" val="$2"
	case "$val" in
		''|*[!0-9.]*) echo "Error: $name must be numeric, got: '${val}'" >&2; exit 2 ;;
	esac
}
validate_threshold "--family"  "$thr_family"
validate_threshold "--genus"   "$thr_genus"
validate_threshold "--species" "$thr_species"

awk -F',' -v tf="$thr_family" -v tg="$thr_genus" -v ts="$thr_species" '
	{
		q = $1;
		pid = $5;
		if (q == "" || q == "qseqid") next;
		if (pid == "" || pid+0 != pid) { print q "\tunassigned"; next }
		pid += 0;
		if      (pid >= ts) print q "\tspecies";
		else if (pid >= tg) print q "\tgenus";
		else if (pid >= tf) print q "\tfamily";
		else                print q "\tunassigned";
	}
' "$report"
