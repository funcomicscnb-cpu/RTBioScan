#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: otu_lock_summary_report.sh <otu_lock_summary.tsv> [options]

Options:
  --round <round_id>   Filter to a single round_id
  --sample <sample>    Filter to a single sample
  --out <file>         Write report to file (default: stdout)

Notes:
  - Expects the header columns written by Consensus_simple.sh:
    round_id, sample, otu_key, n_cand, min_cand, min_cand_ok, is_frozen,
    lock_enabled, cluster_cons_count, cluster_cons_min, cluster_noncons_max,
    lock_ratio, ratio_threshold, lock_rule_pass, stable_count, min_stable_rounds,
    should_consolidate, effective_consolidated, reason, source
USAGE
}

if [ "$#" -lt 1 ]; then
  usage
  exit 1
fi

infile="$1"
shift
round_filter=""
sample_filter=""
out_file=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --round)
      round_filter="${2:-}"; shift 2 ;;
    --sample)
      sample_filter="${2:-}"; shift 2 ;;
    --out)
      out_file="${2:-}"; shift 2 ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "ERROR: unknown option '$1'" 1>&2
      usage
      exit 1 ;;
  esac
done

if [ ! -s "$infile" ]; then
  echo "ERROR: input file is missing or empty: $infile" 1>&2
  exit 1
fi

report="$(
  awk -v round_filter="$round_filter" -v sample_filter="$sample_filter" '
    BEGIN{FS=OFS="\t"}
    function idx(name,   i) {
      for (i=1; i<=NF; i++) if ($i == name) return i;
      return 0;
    }
    NR==1{
      i_round=idx("round_id");
      i_sample=idx("sample");
      i_otu=idx("otu_key");
      i_eff=idx("effective_consolidated");
      i_reason=idx("reason");
      if (i_sample==0 || i_otu==0 || i_eff==0 || i_reason==0) {
        print "ERROR: missing required columns in header" > "/dev/stderr";
        exit 2;
      }
      next;
    }
    {
      if (round_filter != "" && i_round > 0 && $i_round != round_filter) next;
      if (sample_filter != "" && $i_sample != sample_filter) next;
      total++;
      eff=$i_eff+0;
      if (eff == 1) cons++; else notcons++;
      reason=$i_reason; if (reason=="") reason="unknown";
      reasons[reason]++;
      if (eff == 0) blockers[reason]++;
      if (i_round > 0) {
        r=$i_round;
        round_total[r]++;
        if (eff == 1) round_cons[r]++;
      }
    }
    END{
      print "SUMMARY\t" total "\t" cons "\t" notcons;
      for (r in reasons) print "REASON\t" reasons[r] "\t" r;
      for (r in blockers) print "BLOCKER\t" blockers[r] "\t" r;
      for (r in round_total) print "ROUND\t" round_total[r] "\t" round_cons[r] "\t" r;
    }
  ' "$infile"
)"

{
  echo "OTU Lock Summary Report"
  echo "Input: $infile"
  if [ -n "$round_filter" ]; then echo "Filter round_id: $round_filter"; fi
  if [ -n "$sample_filter" ]; then echo "Filter sample: $sample_filter"; fi
  echo

  total=$(printf "%s\n" "$report" | awk -F'\t' '$1=="SUMMARY"{print $2}')
  cons=$(printf "%s\n" "$report" | awk -F'\t' '$1=="SUMMARY"{print $3}')
  notcons=$(printf "%s\n" "$report" | awk -F'\t' '$1=="SUMMARY"{print $4}')
  echo "Summary"
  echo "  Rows considered: ${total:-0}"
  echo "  Consolidated:    ${cons:-0}"
  echo "  Not consolidated:${notcons:-0}"
  echo

  echo "Reasons (all rows)"
  printf "%s\n" "$report" | awk -F'\t' '$1=="REASON"{printf "  %-24s %s\n", $3, $2}'
  echo

  echo "Blockers (not consolidated)"
  printf "%s\n" "$report" | awk -F'\t' '$1=="BLOCKER"{printf "  %-24s %s\n", $3, $2}'
  echo

  if printf "%s\n" "$report" | awk -F'\t' '$1=="ROUND"{exit 0} END{exit 1}'; then
    echo "Per-round totals"
    printf "%s\n" "$report" | awk -F'\t' '$1=="ROUND"{printf "  %-12s total=%s consolidated=%s\n", $4, $2, $3}'
    echo
  fi
} | { if [ -n "$out_file" ]; then cat > "$out_file"; else cat; fi; }
