#!/usr/bin/env bash
set -euo pipefail
_SCRIPT_VERSION="read-counts-plots-v4"

summary_file=""
out_dir=""
max_samples="10"
sig_dir=""
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
read_counts_r="${script_dir}/Read_counts.R"
label_to_id_pl="${script_dir}/sample_label_id.pl"

usage() {
  cat 1>&2 <<'USAGE'
Usage: report_sample_read_counts_plots.sh --summary <summary_demult_rpt.tsv> --out-dir <samples_asset_dir> [--max <N>] [--sig-dir <sig_dir>]
USAGE
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --summary)
      summary_file="${2:-}"
      shift 2
      ;;
    --out-dir)
      out_dir="${2:-}"
      shift 2
      ;;
    --max)
      max_samples="${2:-}"
      shift 2
      ;;
    --sig-dir)
      sig_dir="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "Unknown argument: $1" 1>&2
      usage
      ;;
  esac
done

if [[ -z "$summary_file" || -z "$out_dir" ]]; then
  usage
fi
if ! [[ "$max_samples" =~ ^[0-9]+$ ]]; then
  echo "Invalid --max value: $max_samples" 1>&2
  exit 2
fi
if [[ "$max_samples" -eq 0 ]]; then
  exit 0
fi
if [[ ! -s "$summary_file" ]]; then
  exit 0
fi
if [[ ! -f "$read_counts_r" || ! -f "$label_to_id_pl" ]]; then
  echo "Missing helper scripts for sample plots" 1>&2
  exit 1
fi

mkdir -p "$out_dir"

tmp_top="$(mktemp)"
trap 'rm -f "$tmp_top"' EXIT

group_column="$(
  awk -F'\t' '
    NR==1 {
      for (i = 1; i <= NF; i++) idx[$i] = i;
      sample_idx = idx["sample_name"];
      track_idx = idx["track_sample_label"];
      if (!sample_idx) {
        print "ERROR: summary file " FILENAME " is missing required sample_name column; regenerate the summary with the corrected schema" > "/dev/stderr";
        exit 3;
      }
      next;
    }
    track_idx && $track_idx != "" { track_seen = 1 }
    END {
      if (track_idx && track_seen) print "track_sample_label";
      else print "sample_name";
    }
  ' "$summary_file"
)"
if [[ -z "$group_column" ]]; then
  echo "ERROR: failed to determine summary grouping column" 1>&2
  exit 1
fi

# Select top grouped samples by total HAC read_count across markers.
awk -F'\t' -v group_col="$group_column" '
  NR==1 {
    for (i = 1; i <= NF; i++) idx[$i] = i;
    ci = idx["read_count"];
    mi = idx["basecalling_model"];
    gi = idx[group_col];
    if (!ci || !mi || !gi) {
      print "ERROR: summary file " FILENAME " is missing required grouping column " group_col > "/dev/stderr";
      exit 3;
    }
    next;
  }
  {
    model = $mi;
    sample_label = $gi;
    read_count = $ci;
    if (model != "hac") next;
    if (sample_label == "" || sample_label ~ /^no_adapter/) next;
    if (read_count ~ /^[0-9]+$/) {
      sum[sample_label] += read_count;
    }
  }
  END {
    for (s in sum) printf "%s\t%d\n", s, sum[s];
  }
' "$summary_file" | LC_ALL=C sort -t$'\t' -k2,2nr -k1,1 | awk -F'\t' -v max="$max_samples" '
  NR <= max { print $1 "\t" $2 }
' > "$tmp_top"

# Write selection signature (bookkeeping only, not a regeneration gate).
if [ -n "$sig_dir" ]; then
  mkdir -p "$sig_dir"
  printf '%s\n%s\n%s\n' "$_SCRIPT_VERSION" "$max_samples" "$(cat "$tmp_top")" \
    | sha256sum | awk '{print $1}' > "$sig_dir/selection.sig"
fi

while IFS=$'\t' read -r sample_label _total_reads; do
  [[ -z "$sample_label" ]] && continue
  sample_id="$(perl "$label_to_id_pl" "$sample_label" 2>/dev/null | tr -d '\r\n')"
  if [[ -z "$sample_id" ]]; then
    echo "WARN: could not derive sample_id for sample label: $sample_label" 1>&2
    continue
  fi
  sample_out_dir="${out_dir}/${sample_id}"
  mkdir -p "$sample_out_dir"
  output_prefix="${sample_out_dir}/${sample_id}"

  if [ -n "$sig_dir" ]; then
    mkdir -p "$sig_dir"
    # Hash the rows for this normalized sample plus the version salt so any
    # logic change invalidates existing signatures.
    _sample_key="$(
      (awk -F'\t' -v lbl="$sample_label" -v group_col="$group_column" '
          NR==1 {
            for(i=1;i<=NF;i++) {
              idx[$i]=i;
            }
            print;
            next
          }
          {
            gi=idx[group_col];
            if (gi && $gi==lbl) print
          }
      ' "$summary_file"
      echo "$_SCRIPT_VERSION"
      ) | sha256sum | awk '{print $1}'
    )"

    _sample_sig_file="$sig_dir/${sample_id}.sig"
    _a="${sample_out_dir}/${sample_id}"
    if ! bash "${script_dir}/plot_sig.sh" check "$_sample_sig_file" "$_sample_key" \
        "${_a}_reads_per_barcode.png"     "${_a}_reads_per_barcode.pdf" \
        "${_a}_reads_per_sample.png"      "${_a}_reads_per_sample.pdf" \
        "${_a}_reads_per_sample_log.png"  "${_a}_reads_per_sample_log.pdf"; then
      echo "INFO: skipping sample $sample_label (unchanged)" 1>&2
      continue
    fi
  fi

  if ! Rscript "$read_counts_r" "$summary_file" "$sample_label" "$output_prefix"; then
    echo "WARN: sample Read_counts plotting failed for sample=${sample_label} sample_id=${sample_id}" 1>&2
  else
    if [ -n "$sig_dir" ]; then
      bash "${script_dir}/plot_sig.sh" commit "$_sample_sig_file" "$_sample_key"
    fi
  fi
done < "$tmp_top"
