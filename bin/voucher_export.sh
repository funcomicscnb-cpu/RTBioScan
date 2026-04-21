#!/bin/bash
# voucher_export.sh — Extract dominant consensus sequences from RTBioScan results
# for reference database submission (BOLD, GenBank, etc.).
#
# The voucher workflow targets single known-individual specimens sequenced with the
# barcoding or voucher profile. The pipeline produces a high-quality consensus
# sequence per target marker; this script extracts the dominant OTU per marker,
# reformats the FASTA header for database submission, and optionally attaches a
# BLAST taxonomy suggestion (secondary — the most common use case is a new species
# for which no database match exists).
#
# Usage:
#   bash bin/voucher_export.sh [OPTIONS]
#
# Options:
#   --results DIR   Path to the results/ directory (default: ./results)
#   --out DIR       Output directory (default: ./voucher_output)
#   --sample NAME   Only export this sample (optional; exports all if omitted)
#   --marker MARKER Only export this marker, e.g. COI or ITS2 (optional)
#   -h, --help      Show this help
#
# Output files:
#   OUTPUT_DIR/voucher_sequences.fasta  — one record per sample+marker (dominant OTU)
#   OUTPUT_DIR/voucher_summary.tsv      — sample, marker, reads, otu_key, blast_suggestion
#
# FASTA header format:
#   >SAMPLE|MARKER|reads-N[|BLAST:Genus_species]

set -euo pipefail

RESULTS_DIR="./results"
OUTPUT_DIR="./voucher_output"
FILTER_SAMPLE=""
FILTER_MARKER=""

# --------------------------------------------------------------------------
usage() {
    cat >&2 <<'USAGE'
Usage: bash bin/voucher_export.sh [OPTIONS]

Extract dominant consensus sequences from RTBioScan results for reference
database submission (BOLD, GenBank, etc.).

Options:
  --results DIR   Path to the results/ directory (default: ./results)
  --out DIR       Output directory (default: ./voucher_output)
  --sample NAME   Only export this sample (optional)
  --marker MARKER Only export this marker, e.g. COI or ITS2 (optional)
  -h, --help      Show this help

Run the pipeline with -profile voucher (or barcoding) first, then run this
script to collect and reformat the consensus sequences for database submission.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --results) RESULTS_DIR="$2"; shift 2 ;;
        --out)     OUTPUT_DIR="$2"; shift 2 ;;
        --sample)  FILTER_SAMPLE="$2"; shift 2 ;;
        --marker)  FILTER_MARKER="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'Unknown option: %s\n' "$1" >&2; usage; exit 1 ;;
    esac
done

# --------------------------------------------------------------------------
# Locate merged consensus FASTAs.
# Prefer results/current/ (consistent end-of-round snapshot) over
# results/temp/ongoing/ (may reflect a mid-round state).
# --------------------------------------------------------------------------
SEARCH_BASE=""
for base in "$RESULTS_DIR/current" "$RESULTS_DIR/temp/ongoing"; do
    [ -d "$base" ] || continue
    count=$(find "$base" -name "*_Merged_Consensus.fasta" -type f 2>/dev/null \
            | wc -l | tr -d ' ')
    if [ "${count:-0}" -gt 0 ]; then
        SEARCH_BASE="$base"
        break
    fi
done

if [ -z "$SEARCH_BASE" ]; then
    printf 'ERROR: No *_Merged_Consensus.fasta files found under %s\n' \
        "$RESULTS_DIR" >&2
    printf '  Run the pipeline with -profile voucher (or barcoding) first.\n' >&2
    exit 1
fi

# --------------------------------------------------------------------------
# Locate the consensus taxonomy report (best-effort).
# Schema: long_seq_id<TAB>consensus_taxid<TAB>kingdom<TAB>phylum<TAB>class
#         <TAB>order<TAB>family<TAB>genus<TAB>species
# --------------------------------------------------------------------------
TAX_FILE=""
for tax_base in "$RESULTS_DIR/temp/ongoing" "$RESULTS_DIR/ongoing" "$RESULTS_DIR/current"; do
    [ -d "$tax_base" ] || continue
    candidate=$(find "$tax_base" -name "blast_report_cons_full.txt" -type f \
                2>/dev/null | sort -r | head -1 || true)
    if [ -n "$candidate" ] && [ -s "$candidate" ]; then
        TAX_FILE="$candidate"
        break
    fi
done

if [ -n "$TAX_FILE" ]; then
    printf 'INFO: Using taxonomy file: %s\n' "$TAX_FILE" >&2
else
    printf 'INFO: No taxonomy file found; blast_suggestion column will be empty.\n' >&2
fi

# --------------------------------------------------------------------------
# Create output directory and initialise output files.
# --------------------------------------------------------------------------
mkdir -p "$OUTPUT_DIR"
OUT_FASTA="$OUTPUT_DIR/voucher_sequences.fasta"
OUT_TSV="$OUTPUT_DIR/voucher_summary.tsv"
: > "$OUT_FASTA"
printf 'sample\tmarker\treads\totu_key\tblast_suggestion\n' > "$OUT_TSV"

# --------------------------------------------------------------------------
# Process each sample's merged consensus FASTA.
# For each record we extract:
#   reads    — number of reads supporting this consensus (reads-N field)
#   marker   — target gene marker (marker=VALUE field)
#   otu_key  — OTU identifier (OTU=VALUE field)
# Then for each marker we keep only the record with the highest read count.
# --------------------------------------------------------------------------
processed=0

while IFS= read -r fasta_path; do
    [ -f "$fasta_path" ] || continue

    fasta_base=$(basename "$fasta_path")
    sample="${fasta_base%_Merged_Consensus.fasta}"

    # Apply sample filter.
    if [ -n "$FILTER_SAMPLE" ] && [ "$sample" != "$FILTER_SAMPLE" ]; then
        continue
    fi

    printf 'INFO: Processing sample: %s\n' "$sample" >&2

    # awk parses the FASTA, tracks the highest-read record per marker, and
    # appends selected sequences + summary rows to the output files.
    awk \
        -v sample="$sample" \
        -v filter_marker="$FILTER_MARKER" \
        -v out_fasta="$OUT_FASTA" \
        -v out_tsv="$OUT_TSV" \
        -v tax_file="$TAX_FILE" \
    '
    BEGIN {
        # Pre-load taxonomy lookup: header → "Genus_species" (or "Genus_sp.")
        if (tax_file != "") {
            while ((getline tline < tax_file) > 0) {
                n = split(tline, tf, "\t")
                if (tf[1] == "long_seq_id") continue
                genus   = (n >= 8) ? tf[8] : ""
                species = (n >= 9) ? tf[9] : ""
                if (genus != "" && genus != "Unassigned") {
                    if (species != "" && species != "Unassigned" && species != genus) {
                        tax[tf[1]] = genus "_" species
                    } else {
                        tax[tf[1]] = genus "_sp."
                    }
                }
            }
            close(tax_file)
        }
    }

    /^>/ {
        # Flush the previous record before starting a new one.
        if (header != "") {
            _track(header, reads, marker, otu, seq)
        }
        header = substr($0, 2)
        seq    = ""
        reads  = 0
        marker = "unknown"
        otu    = ""

        # reads-N
        tmp = header
        if (match(tmp, /reads-[0-9]+/)) reads = substr(tmp, RSTART+6, RLENGTH-6)+0

        # marker=VALUE
        if (match(header, /marker=[^|]+/)) marker = substr(header, RSTART+7, RLENGTH-7)

        # OTU=VALUE
        if (match(header, /OTU=[^|;]+/)) otu = substr(header, RSTART+4, RLENGTH-4)

        next
    }

    # Accumulate sequence lines (handles multi-line FASTA).
    { seq = seq $0 }

    END {
        if (header != "") _track(header, reads, marker, otu, seq)
        _emit()
    }

    # Track the record with the highest read count per marker.
    function _track(h, r, m, o, s) {
        if (filter_marker != "" && m != filter_marker) return
        if (!(m in best_reads) || r+0 > best_reads[m]+0) {
            best_reads[m]  = r
            best_header[m] = h
            best_seq[m]    = s
            best_otu[m]    = o
        }
    }

    # Emit output for every winning record.
    function _emit(    m, clean_hdr, suggestion, out_hdr) {
        for (m in best_header) {
            clean_hdr  = sample "|" m "|reads-" best_reads[m]
            suggestion = ""

            # Taxonomy lookup: try full original header, then clean header.
            if (best_header[m] in tax) {
                suggestion = tax[best_header[m]]
            } else if (clean_hdr in tax) {
                suggestion = tax[clean_hdr]
            }

            out_hdr = ">" clean_hdr
            if (suggestion != "") out_hdr = out_hdr "|BLAST:" suggestion

            print out_hdr >> out_fasta
            print best_seq[m]  >> out_fasta
            printf "%s\t%s\t%s\t%s\t%s\n", \
                sample, m, best_reads[m], best_otu[m], suggestion >> out_tsv
        }
    }
    ' "$fasta_path"

    processed=$((processed + 1))

done < <(find "$SEARCH_BASE" -name "*_Merged_Consensus.fasta" -type f 2>/dev/null | sort)

# --------------------------------------------------------------------------
if [ "$processed" -eq 0 ]; then
    printf 'WARN: No samples exported (check --results path and --sample/--marker filters).\n' >&2
    exit 1
fi

seq_count=$(grep -c '^>' "$OUT_FASTA" 2>/dev/null || echo 0)
printf '\nDone. Exported %d sample(s), %d sequence(s).\n' "$processed" "$seq_count" >&2
printf '  FASTA: %s\n' "$OUT_FASTA" >&2
printf '  TSV:   %s\n' "$OUT_TSV" >&2
