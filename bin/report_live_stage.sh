#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

stage_root=""
round_dir=""
state_dir=""
consensus_dir=""
barcode=""
run_id=""

usage() {
  cat 1>&2 <<'USAGE'
Usage: report_live_stage.sh --stage-root DIR --round-dir DIR --state-dir DIR --barcode ID [--consensus-dir DIR] [--run-id ID]
USAGE
  exit 2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --stage-root)
      stage_root="${2:-}"
      shift 2
      ;;
    --round-dir)
      round_dir="${2:-}"
      shift 2
      ;;
    --state-dir)
      state_dir="${2:-}"
      shift 2
      ;;
    --consensus-dir)
      consensus_dir="${2:-}"
      shift 2
      ;;
    --barcode)
      barcode="${2:-}"
      shift 2
      ;;
    --run-id)
      run_id="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      ;;
    *)
      echo "ERROR: unknown argument: $1" 1>&2
      usage
      ;;
  esac
done

if [ -z "$stage_root" ] || [ -z "$round_dir" ] || [ -z "$state_dir" ] || [ -z "$barcode" ]; then
  usage
fi

asset_dir="$stage_root/report_assets"
plots_png_dir="$stage_root/plots/png"
plots_pdf_dir="$stage_root/plots/pdf"
tables_dir="$stage_root/tables"
sequences_dir="$stage_root/sequences"

mkdir -p "$asset_dir" "$plots_png_dir" "$plots_pdf_dir" "$tables_dir" "$sequences_dir"

copy_if_exists() {
  local src="$1"
  local dst="$2"
  [ -e "$src" ] || return 0
  mkdir -p "$(dirname "$dst")"
  cp -Rp "$src" "$dst"
}

promote_top_level_assets() {
  local entry=""
  local base=""
  for entry in "$asset_dir"/*.png; do
    [ -f "$entry" ] || continue
    [ -L "$entry" ] && continue
    base="$(basename "$entry")"
    mv "$entry" "$plots_png_dir/$base"
    ln -sf "../plots/png/$base" "$entry"
  done
  for entry in "$asset_dir"/*.pdf; do
    [ -f "$entry" ] || continue
    [ -L "$entry" ] && continue
    base="$(basename "$entry")"
    mv "$entry" "$plots_pdf_dir/$base"
    ln -sf "../plots/pdf/$base" "$entry"
  done
}

promote_sample_assets() {
  local sample_root="$asset_dir/samples"
  local sample_dir=""
  local sample_id=""
  local entry=""
  local base=""
  [ -d "$sample_root" ] || return 0
  for sample_dir in "$sample_root"/*; do
    [ -d "$sample_dir" ] || continue
    sample_id="$(basename "$sample_dir")"
    mkdir -p "$plots_png_dir/samples/$sample_id" "$plots_pdf_dir/samples/$sample_id"
    for entry in "$sample_dir"/*.png; do
      [ -f "$entry" ] || continue
      [ -L "$entry" ] && continue
      base="$(basename "$entry")"
      mv "$entry" "$plots_png_dir/samples/$sample_id/$base"
      ln -sf "../../../plots/png/samples/$sample_id/$base" "$entry"
    done
    for entry in "$sample_dir"/*.pdf; do
      [ -f "$entry" ] || continue
      [ -L "$entry" ] && continue
      base="$(basename "$entry")"
      mv "$entry" "$plots_pdf_dir/samples/$sample_id/$base"
      ln -sf "../../../plots/pdf/samples/$sample_id/$base" "$entry"
    done
  done
}

# Round-local tables for browsing/debugging.
for entry in \
  "$round_dir"/*.txt "$round_dir"/*.tsv "$round_dir"/*.csv \
  "$round_dir"/*.txt.gz "$round_dir"/*.tsv.gz "$round_dir"/*.csv.gz \
  "$round_dir"/*_rpt.txt "$round_dir"/*_rpt.txt.gz; do
  [ -e "$entry" ] || continue
  copy_if_exists "$entry" "$tables_dir/$(basename "$entry")"
done

# Latest live report inputs needed by the renderer's consensus sequence table.
for entry in \
  "$state_dir"/RTBioScan_* \
  "$state_dir"/"${barcode}"_read_info_rpt.txt \
  "$state_dir"/"${barcode}"_on_target_rpt.txt \
  "$state_dir"/"${barcode}"_summary_demult_rpt.txt \
  "$state_dir"/"${barcode}"_otu_tax_time_rpt.txt \
  "$state_dir"/"${barcode}"_otu_frozen_tax_time_rpt.txt \
  "$state_dir"/"${barcode}"_consensus_tax_time_rpt.txt \
  "$state_dir"/"${barcode}"_consensus_consolidated_tax_time_rpt.txt \
  "$state_dir"/"${barcode}"_otu_tax_spc_*_treemap_rpt.txt \
  "$state_dir"/"${barcode}"_otu_tax_gns_*_treemap_rpt.txt \
  "$state_dir"/"${barcode}"_consensus_tax_spc_*_treemap_rpt.txt \
  "$state_dir"/"${barcode}"_consensus_tax_gns_*_treemap_rpt.txt \
  "$state_dir"/"${barcode}"_consensus_consolidated_tax_spc_*_treemap_rpt.txt \
  "$state_dir"/"${barcode}"_consensus_consolidated_tax_gns_*_treemap_rpt.txt; do
  [ -e "$entry" ] || continue
  copy_if_exists "$entry" "$tables_dir/$(basename "$entry")"
done

for entry in \
  "$round_dir"/*.fa "$round_dir"/*.fasta "$round_dir"/*.fq "$round_dir"/*.fastq \
  "$round_dir"/*.fa.gz "$round_dir"/*.fasta.gz "$round_dir"/*.fq.gz "$round_dir"/*.fastq.gz; do
  [ -e "$entry" ] || continue
  copy_if_exists "$entry" "$sequences_dir/$(basename "$entry")"
done

if [ -n "$consensus_dir" ] && [ -d "$consensus_dir" ]; then
  mkdir -p "$sequences_dir/Consensus"
  cp -Rp "$consensus_dir"/. "$sequences_dir/Consensus"/
fi

promote_top_level_assets
promote_sample_assets

cat > "$stage_root/README.html" <<EOF
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>RTBioScan Live Round</title>
</head>
<body>
  <h1>RTBioScan Live Round</h1>
  <p>Run: <strong>${run_id:-unknown}</strong></p>
  <p>This directory exposes the latest completed round media bundle only.</p>
</body>
</html>
EOF
