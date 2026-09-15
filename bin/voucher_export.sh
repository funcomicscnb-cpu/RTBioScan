#!/bin/bash
# voucher_export.sh — Extract dominant consensus sequences from one published
# RTBioScan current-state snapshot for reference database submission.

set -euo pipefail

LC_ALL=C
export LC_ALL

RESULTS_DIR="./results"
OUTPUT_DIR="./voucher_output"
FILTER_SAMPLE=""
FILTER_MARKER=""
STATE_SELECTOR=""
RUN_ID_SELECTOR=""

usage() {
    cat >&2 <<'USAGE'
Usage: bash bin/voucher_export.sh [OPTIONS]

Extract dominant consensus sequences from one authoritative published RTBioScan
current state for database submission (BOLD, GenBank, etc.).

Options:
  --results DIR   Path to the results/ directory (default: ./results)
  --out DIR       Output directory (default: ./voucher_output)
  --sample NAME   Only export this biological sample (optional)
  --marker MARKER Only export this marker, e.g. COI or ITS2 (optional)
  --state ID      Select results/current/state/ID (required if ambiguous)
  --run-id ID     Select exact biological input identity root results/sample_info/ID
  -h, --help      Show this help

Identity:
  Nextflow -name sets workflow.runName (diagnostic execution metadata here).
  --run-id selects the biological input identity root.

Outputs:
  OUTPUT_DIR/voucher_sequences.fasta
  OUTPUT_DIR/voucher_summary.tsv
USAGE
}

die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require_value() {
    [ "$#" -ge 2 ] && [ -n "$2" ] || die "Option $1 requires a value."
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --results) require_value "$@"; RESULTS_DIR="$2"; shift 2 ;;
        --out) require_value "$@"; OUTPUT_DIR="$2"; shift 2 ;;
        --sample) require_value "$@"; FILTER_SAMPLE="$2"; shift 2 ;;
        --marker) require_value "$@"; FILTER_MARKER="$2"; shift 2 ;;
        --state) require_value "$@"; STATE_SELECTOR="$2"; shift 2 ;;
        --run-id) require_value "$@"; RUN_ID_SELECTOR="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) printf 'ERROR: Unknown option: %s\n' "$1" >&2; usage; exit 1 ;;
    esac
done

# Prevent assignment-shaped relative paths (for example run=1/...) from being
# interpreted as variable assignments when they later appear as awk operands.
case "$RESULTS_DIR" in
    .|..|/*|./*|../*) ;;
    *) RESULTS_DIR="./$RESULTS_DIR" ;;
esac

case "$STATE_SELECTOR" in
    */*|.|..) die "--state must be one state ID, not a path." ;;
esac
case "$RUN_ID_SELECTOR" in
    */*|.|..) die "--run-id must be one run ID, not a path." ;;
esac

# Only results/current/state/<state_id> is authoritative. A state is eligible
# when both documented publication roots exist; temp/ongoing, round, and
# sequences/single_exp trees are never searched.
STATE_ROOT="$RESULTS_DIR/current/state"
STATE_DIR=""
shopt -s nullglob

if [ -n "$STATE_SELECTOR" ]; then
    candidate="$STATE_ROOT/$STATE_SELECTOR"
    if [ ! -d "$candidate/sequences/Consensus" ] || [ ! -d "$candidate/tables" ]; then
        die "Selected state '$STATE_SELECTOR' is missing sequences/Consensus or tables under $STATE_ROOT."
    fi
    STATE_DIR="$candidate"
else
    eligible_states=()
    if [ -d "$STATE_ROOT" ]; then
        for candidate in "$STATE_ROOT"/*; do
            [ -d "$candidate/sequences/Consensus" ] || continue
            [ -d "$candidate/tables" ] || continue
            eligible_states+=("$candidate")
        done
    fi
    if [ "${#eligible_states[@]}" -eq 0 ]; then
        die "No authoritative published state found under $STATE_ROOT."
    fi
    if [ "${#eligible_states[@]}" -gt 1 ]; then
        die "Multiple authoritative published states found under $STATE_ROOT; select one with --state ID."
    fi
    STATE_DIR="${eligible_states[0]}"
fi

STATE_ID=$(basename "$STATE_DIR")
CONSENSUS_DIR="$STATE_DIR/sequences/Consensus"
TABLES_DIR="$STATE_DIR/tables"

# main.nf renders workflow.runName (the execution name) and state ID into the
# published README. The execution name is diagnostic only: it is not authority
# for selecting biological input metadata under results/sample_info.
README_EXECUTION_NAME=""
META_STATE_ID=""
STATE_META=""
if [ -f "$STATE_DIR/README.html" ]; then
    STATE_META=$(awk '
        /RTBioScan &#183; Run / && / &#183; State / {
            line = $0
            sub(/^.*RTBioScan &#183; Run /, "", line)
            run = line
            sub(/ &#183; State .*/, "", run)
            state = line
            sub(/^.* &#183; State /, "", state)
            sub(/<.*/, "", state)
            print run "\t" state
            exit
        }
    ' "$STATE_DIR/README.html")
fi
if [ -n "$STATE_META" ]; then
    IFS=$'\t' read -r README_EXECUTION_NAME META_STATE_ID <<< "$STATE_META"
    [ "$META_STATE_ID" = "$STATE_ID" ] || die "Published state metadata identifies state '$META_STATE_ID', not directory '$STATE_ID'."
fi

SAMPLE_INFO_ROOT="$RESULTS_DIR/sample_info"
SAMPLE_INFO_DIR=""
IDENTITY_SELECTION=""
if [ -n "$RUN_ID_SELECTOR" ]; then
    SAMPLE_INFO_DIR="$SAMPLE_INFO_ROOT/$RUN_ID_SELECTOR"
    if [ ! -d "$SAMPLE_INFO_DIR" ]; then
        die "Explicit --run-id '$RUN_ID_SELECTOR' has no identity directory at $SAMPLE_INFO_DIR."
    fi
    if [ ! -f "$SAMPLE_INFO_DIR/track_identity.tsv" ] && [ ! -f "$SAMPLE_INFO_DIR/replicate_identity.tsv" ]; then
        die "Explicit --run-id '$RUN_ID_SELECTOR' has no track_identity.tsv or replicate_identity.tsv at $SAMPLE_INFO_DIR."
    fi
    IDENTITY_SELECTION="explicit --run-id '$RUN_ID_SELECTOR'"
    if [ -n "$README_EXECUTION_NAME" ] && [ "$README_EXECUTION_NAME" != "$RUN_ID_SELECTOR" ]; then
        printf "WARN: Published README Run '%s' is workflow.runName (execution metadata), while explicit --run-id '%s' selects biological input metadata from %s.\n" \
            "$README_EXECUTION_NAME" "$RUN_ID_SELECTOR" "$SAMPLE_INFO_DIR" >&2
    fi
else
    eligible_identity_roots=()
    eligible_identity_labels=()
    if [ -d "$SAMPLE_INFO_ROOT" ]; then
        if [ -f "$SAMPLE_INFO_ROOT/track_identity.tsv" ] || [ -f "$SAMPLE_INFO_ROOT/replicate_identity.tsv" ]; then
            eligible_identity_roots+=("$SAMPLE_INFO_ROOT")
            eligible_identity_labels+=("flat results/sample_info")
        fi
        for candidate in "$SAMPLE_INFO_ROOT"/*; do
            [ -d "$candidate" ] || continue
            if [ -f "$candidate/track_identity.tsv" ] || [ -f "$candidate/replicate_identity.tsv" ]; then
                eligible_identity_roots+=("$candidate")
                eligible_identity_labels+=("nested data-run '$(basename "$candidate")'")
            fi
        done
    fi
    if [ "${#eligible_identity_roots[@]}" -eq 0 ]; then
        die "No direct identity root containing track_identity.tsv or replicate_identity.tsv found under $SAMPLE_INFO_ROOT."
    fi
    if [ "${#eligible_identity_roots[@]}" -gt 1 ]; then
        die "Multiple direct identity roots found under $SAMPLE_INFO_ROOT; select one nested data run with --run-id ID."
    fi
    SAMPLE_INFO_DIR="${eligible_identity_roots[0]}"
    IDENTITY_SELECTION="unique-layout fallback (${eligible_identity_labels[0]})"
fi

if [ -f "$SAMPLE_INFO_DIR/track_identity.tsv" ]; then
    IDENTITY_FILE="$SAMPLE_INFO_DIR/track_identity.tsv"
elif [ -f "$SAMPLE_INFO_DIR/replicate_identity.tsv" ]; then
    IDENTITY_FILE="$SAMPLE_INFO_DIR/replicate_identity.tsv"
else
    die "No track_identity.tsv or replicate_identity.tsv found under selected identity root $SAMPLE_INFO_DIR."
fi

fasta_files=()
for candidate in "$CONSENSUS_DIR"/*/*_Merged_Consensus.fasta; do
    [ -f "$candidate" ] || continue
    fasta_files+=("$candidate")
done

tax_files=()
for candidate in "$TABLES_DIR"/*_blast_consensus_tax_rpt.txt; do
    [ -f "$candidate" ] || continue
    tax_files+=("$candidate")
done
tax_count=${#tax_files[@]}

# Bash 3.2 with nounset rejects an expansion of an empty array. Build one
# guaranteed-nonempty vector for awk while retaining taxonomy files first.
input_files=()
if [ "$tax_count" -gt 0 ]; then
    input_files+=("${tax_files[@]}")
fi
if [ "${#fasta_files[@]}" -gt 0 ]; then
    input_files+=("${fasta_files[@]}")
fi
input_files+=(/dev/null)

if [ -n "$README_EXECUTION_NAME" ]; then
    printf 'INFO: Using published state: %s (README execution name: %s)\n' "$STATE_ID" "$README_EXECUTION_NAME" >&2
else
    printf 'INFO: Using published state: %s (README execution name unavailable)\n' "$STATE_ID" >&2
fi
printf 'INFO: Identity selection: %s\n' "$IDENTITY_SELECTION" >&2
printf 'INFO: Using identity bridge: %s\n' "$IDENTITY_FILE" >&2
if [ "$tax_count" -eq 0 ]; then
    printf 'INFO: No *_blast_consensus_tax_rpt.txt file found in %s; BLAST suggestions will be empty.\n' "$TABLES_DIR" >&2
else
    printf 'INFO: Reading %d taxonomy table(s) from %s\n' "$tax_count" "$TABLES_DIR" >&2
fi

mkdir -p "$OUTPUT_DIR"
OUT_FASTA="$OUTPUT_DIR/voucher_sequences.fasta"
OUT_TSV="$OUTPUT_DIR/voucher_summary.tsv"
TMP_FASTA=""
TMP_TSV=""
TMP_STATS=""

cleanup() {
    [ -z "$TMP_FASTA" ] || rm -f "$TMP_FASTA"
    [ -z "$TMP_TSV" ] || rm -f "$TMP_TSV"
    [ -z "$TMP_STATS" ] || rm -f "$TMP_STATS"
}
trap cleanup EXIT HUP INT TERM

TMP_FASTA=$(mktemp "$OUTPUT_DIR/.voucher_sequences.fasta.XXXXXX")
TMP_TSV=$(mktemp "$OUTPUT_DIR/.voucher_summary.tsv.XXXXXX")
TMP_STATS=$(mktemp "$OUTPUT_DIR/.voucher_stats.XXXXXX")

# Taxonomy tables are the first tax_count ARGV entries. They and the identity
# bridge are loaded by named columns, leaving only authoritative FASTAs as input.
if awk \
    -v identity_file="$IDENTITY_FILE" \
    -v tax_count="$tax_count" \
    -v filter_sample="$FILTER_SAMPLE" \
    -v filter_marker="$FILTER_MARKER" \
    -v out_fasta="$TMP_FASTA" \
    -v out_tsv="$TMP_TSV" \
    -v stats_out="$TMP_STATS" \
    '
    function fail(message) {
        if (!failed) print "ERROR: " message > "/dev/stderr"
        failed = 1
        exit 2
    }
    function trim_cr(value) { sub(/\r$/, "", value); return value }
    function add_identity(alias, sample, marker, source_line) {
        if (alias == "") fail(identity_file ":" source_line ": empty identity alias")
        if ((alias in identity_sample) &&
            (identity_sample[alias] != sample || identity_marker[alias] != marker)) {
            fail(identity_file ":" source_line ": conflicting identity mapping for unit " alias)
        }
        identity_sample[alias] = sample
        identity_marker[alias] = marker
    }
    function biological_sample(unit) {
        return (unit in identity_sample) ? identity_sample[unit] : unit
    }
    function marker_is_compatible(record_type, context, unit, sample, observed, expected, message) {
        if (!(unit in identity_marker) || identity_marker[unit] == observed) return 1
        expected = identity_marker[unit]
        message = "WARN: Skipping marker-discordant " record_type ": source=" context \
            " unit=" unit " biological_sample=" sample \
            " expected_identity_marker=" expected " observed_consensus_marker=" observed
        marker_warning[++marker_warning_count] = message
        return 0
    }
    function emit_marker_warnings(    i, j, value) {
        for (i = 2; i <= marker_warning_count; i++) {
            value = marker_warning[i]
            j = i - 1
            while (j >= 1 && marker_warning[j] > value) {
                marker_warning[j + 1] = marker_warning[j]
                j--
            }
            marker_warning[j + 1] = value
        }
        for (i = 1; i <= marker_warning_count; i++) print marker_warning[i] > "/dev/stderr"
    }
    function parse_marker(header, context, fields, count, i, field, candidate, chosen) {
        count = split(header, fields, /\|/)
        chosen = ""
        for (i = 2; i <= count; i++) {
            field = fields[i]
            candidate = ""
            if (index(field, "marker=") == 1) {
                candidate = toupper(substr(field, 8))
                if (candidate == "" || candidate !~ /^[A-Z0-9_.-]+$/) {
                    fail(context ": malformed explicit marker tag in header " header)
                }
            } else if (toupper(field) in configured_marker) {
                candidate = toupper(field)
            }
            if (candidate != "") {
                if (chosen != "" && chosen != candidate) fail(context ": conflicting marker tokens in header " header)
                chosen = candidate
            }
        }
        if (chosen == "") fail(context ": no configured positional marker or marker= tag in header " header)
        return chosen
    }
    function parse_reads(header, context, fields, count, i, candidate, chosen, found) {
        count = split(header, fields, /\|/)
        chosen = 0
        found = 0
        for (i = 1; i <= count; i++) {
            if (fields[i] ~ /^reads-[0-9]+$/) {
                candidate = substr(fields[i], 7) + 0
                if (found && chosen != candidate) fail(context ": conflicting reads-N tokens in header " header)
                chosen = candidate
                found = 1
            }
        }
        if (!found) fail(context ": missing reads-N token in header " header)
        return chosen
    }
    function parse_otu(header, context, fields, count, i, candidate, chosen) {
        count = split(header, fields, /\|/)
        chosen = ""
        for (i = 1; i <= count; i++) {
            if (index(fields[i], "OTU=") == 1) {
                candidate = substr(fields[i], 5)
                sub(/;.*/, "", candidate)
                if (candidate == "") fail(context ": empty OTU= tag in header " header)
                if (chosen != "" && chosen != candidate) fail(context ": conflicting OTU= tags in header " header)
                chosen = candidate
            }
        }
        if (chosen == "") fail(context ": missing OTU= tag in header " header)
        return chosen
    }
    function parse_consensus_id(header, context, fields, count) {
        count = split(header, fields, /\|/)
        if (count < 2 || fields[2] == "") fail(context ": missing positional consensus identifier in header " header)
        return fields[2] "_" fields[1]
    }
    function is_better(key, reads, otu, header, sequence) {
        if (!(key in best_present)) return 1
        if (reads != best_reads[key]) return reads > best_reads[key]
        if (otu != best_otu[key]) return otu < best_otu[key]
        if (header != best_header[key]) return header < best_header[key]
        if (sequence != best_sequence[key]) return sequence < best_sequence[key]
        return 0
    }
    function flush_record(context, sample_unit, marker, reads, otu, consensus_id, sample, key) {
        if (record_header == "" || failed) return
        context = record_file
        sample_unit = record_header
        sub(/\|.*/, "", sample_unit)
        if (sample_unit == "") fail(context ": empty consensus unit in FASTA header")
        if (record_sequence == "") fail(context ": empty sequence for header " record_header)
        marker = parse_marker(record_header, context)
        reads = parse_reads(record_header, context)
        otu = parse_otu(record_header, context)
        consensus_id = parse_consensus_id(record_header, context)
        sample = biological_sample(sample_unit)
        if (index(sample, "\t") || index(sample, "|")) fail(context ": unsupported biological sample label " sample)
        if (!marker_is_compatible("FASTA candidate", context, sample_unit, sample, marker)) return
        if (filter_sample != "" && sample != filter_sample) return
        if (normalized_filter_marker != "" && marker != normalized_filter_marker) return
        key = sample SUBSEP marker
        if (is_better(key, reads, otu, record_header, record_sequence)) {
            best_present[key] = 1
            best_sample[key] = sample
            best_marker[key] = marker
            best_reads[key] = reads
            best_otu[key] = otu
            best_consensus_id[key] = consensus_id
            best_header[key] = record_header
            best_sequence[key] = record_sequence
        }
    }
    function load_identity(    line, line_no, count, fields, i, name, sample, marker, collapse_unit, track_unit) {
        line_no = 0
        while ((getline line < identity_file) > 0) {
            line_no++
            line = trim_cr(line)
            count = split(line, fields, "\t")
            if (line_no == 1) {
                for (i = 1; i <= count; i++) {
                    name = fields[i]
                    if (name in identity_column) fail(identity_file ": duplicate column " name)
                    identity_column[name] = i
                }
                if (!("sample_id" in identity_column) || !("marker_id" in identity_column) ||
                    !("unit_id_collapse" in identity_column) || !("unit_id_track" in identity_column)) {
                    fail(identity_file ": missing required named identity columns")
                }
                continue
            }
            if (line == "") continue
            sample = fields[identity_column["sample_id"]]
            marker = toupper(fields[identity_column["marker_id"]])
            collapse_unit = fields[identity_column["unit_id_collapse"]]
            track_unit = fields[identity_column["unit_id_track"]]
            if (sample == "" || marker == "" || collapse_unit == "" || track_unit == "") {
                fail(identity_file ":" line_no ": empty required identity value")
            }
            if (marker !~ /^[A-Z0-9_.-]+$/) fail(identity_file ":" line_no ": malformed marker_id " marker)
            if (index(sample, "\t") || index(sample, "|")) fail(identity_file ":" line_no ": unsupported sample_id " sample)
            configured_marker[marker] = 1
            add_identity(collapse_unit, sample, marker, line_no)
            add_identity(track_unit, sample, marker, line_no)
            identity_rows++
        }
        close(identity_file)
        if (line_no == 0) fail(identity_file ": empty identity file")
        if (identity_rows == 0) fail(identity_file ": identity file has no data rows")
    }
    function taxonomy_suggestion(genus, species) {
        if (genus == "" || genus == "Unassigned") return ""
        if (species != "" && species != "Unassigned" && species != genus) return genus "_" species
        return genus "_sp."
    }
    function load_taxonomy(file,    line, line_no, count, fields, i, name, consensus_id, otu, marker, unit, sample, genus, species, key, signature, suggestion) {
        for (name in taxonomy_column) delete taxonomy_column[name]
        line_no = 0
        while ((getline line < file) > 0) {
            line_no++
            line = trim_cr(line)
            count = split(line, fields, "\t")
            if (line_no == 1) {
                for (i = 1; i <= count; i++) {
                    name = fields[i]
                    if (name in taxonomy_column) fail(file ": duplicate column " name)
                    taxonomy_column[name] = i
                }
                if (!("consensus_id" in taxonomy_column) || !("otu_key" in taxonomy_column) ||
                    !("barcode_by_homology" in taxonomy_column) || !("sample" in taxonomy_column) ||
                    !("consensus_genus" in taxonomy_column) || !("consensus_species" in taxonomy_column)) {
                    fail(file ": missing required named taxonomy columns")
                }
                continue
            }
            if (line == "") continue
            consensus_id = fields[taxonomy_column["consensus_id"]]
            otu = fields[taxonomy_column["otu_key"]]
            marker = toupper(fields[taxonomy_column["barcode_by_homology"]])
            unit = fields[taxonomy_column["sample"]]
            genus = fields[taxonomy_column["consensus_genus"]]
            species = fields[taxonomy_column["consensus_species"]]
            if (consensus_id == "" || otu == "" || marker == "" || unit == "") fail(file ":" line_no ": empty taxonomy join field")
            if (marker !~ /^[A-Z0-9_.-]+$/) fail(file ":" line_no ": malformed taxonomy marker " marker)
            sample = biological_sample(unit)
            if (!marker_is_compatible("taxonomy row", file, unit, sample, marker)) continue
            key = sample SUBSEP marker SUBSEP otu SUBSEP consensus_id
            signature = genus SUBSEP species
            suggestion = taxonomy_suggestion(genus, species)
            if ((key in taxonomy_signature) && taxonomy_signature[key] != signature) {
                fail(file ":" line_no ": conflicting taxonomy for sample=" sample ", marker=" marker ", otu_key=" otu)
            }
            taxonomy_signature[key] = signature
            taxonomy_value[key] = suggestion
        }
        close(file)
        if (line_no == 0) fail(file ": empty taxonomy table")
    }
    function emit_results(    keys, key_count, key, i, j, tmp, tax_key, suggestion, out_header, sample_seen) {
        key_count = 0
        for (key in best_present) keys[++key_count] = key
        for (i = 2; i <= key_count; i++) {
            tmp = keys[i]
            j = i - 1
            while (j >= 1 && (best_sample[keys[j]] "\t" best_marker[keys[j]]) > (best_sample[tmp] "\t" best_marker[tmp])) {
                keys[j + 1] = keys[j]
                j--
            }
            keys[j + 1] = tmp
        }
        sample_count = 0
        for (i = 1; i <= key_count; i++) {
            key = keys[i]
            tax_key = best_sample[key] SUBSEP best_marker[key] SUBSEP best_otu[key] SUBSEP best_consensus_id[key]
            suggestion = (tax_key in taxonomy_value) ? taxonomy_value[tax_key] : ""
            out_header = ">" best_sample[key] "|" best_marker[key] "|reads-" best_reads[key]
            if (suggestion != "") out_header = out_header "|BLAST:" suggestion
            print out_header >> out_fasta
            print best_sequence[key] >> out_fasta
            print best_sample[key] "\t" best_marker[key] "\t" best_reads[key] "\t" best_otu[key] "\t" suggestion >> out_tsv
            if (!(best_sample[key] in sample_seen)) {
                sample_seen[best_sample[key]] = 1
                sample_count++
            }
        }
        print key_count "\t" sample_count > stats_out
        close(out_fasta)
        close(out_tsv)
        close(stats_out)
    }
    BEGIN {
        printf "%s", "" > out_fasta
        print "sample\tmarker\treads\totu_key\tblast_suggestion" > out_tsv
        close(out_fasta)
        close(out_tsv)
        configured_marker["COI"] = 1
        configured_marker["ITS2"] = 1
        normalized_filter_marker = toupper(filter_marker)
        if (normalized_filter_marker != "") configured_marker[normalized_filter_marker] = 1
        load_identity()
        for (tax_index = 1; tax_index <= tax_count; tax_index++) {
            load_taxonomy(ARGV[tax_index])
            delete ARGV[tax_index]
        }
    }
    FNR == 1 {
        flush_record()
        record_header = ""
        record_sequence = ""
        record_file = FILENAME
    }
    /^>/ {
        flush_record()
        record_header = trim_cr(substr($0, 2))
        record_sequence = ""
        record_file = FILENAME
        next
    }
    {
        line = trim_cr($0)
        if (line == "") next
        if (record_header == "") fail(FILENAME ": sequence data before first FASTA header")
        record_sequence = record_sequence line
    }
    END {
        if (!failed) flush_record()
        if (!failed) {
            emit_marker_warnings()
            emit_results()
        }
    }
    ' "${input_files[@]}"
then
    :
else
    parse_status=$?
    exit "$parse_status"
fi

IFS=$'\t' read -r seq_count sample_count < "$TMP_STATS"

# Both complete outputs are built before either public filename is replaced.
mv -f "$TMP_FASTA" "$OUT_FASTA"
TMP_FASTA=""
mv -f "$TMP_TSV" "$OUT_TSV"
TMP_TSV=""
rm -f "$TMP_STATS"
TMP_STATS=""

if [ "$seq_count" -eq 0 ]; then
    printf 'INFO: No voucher sequences matched the selected state and filters; wrote empty FASTA and header-only summary.\n' >&2
else
    printf 'Done. Exported %d biological sample(s), %d sequence(s).\n' "$sample_count" "$seq_count" >&2
fi
printf '  FASTA: %s\n' "$OUT_FASTA" >&2
printf '  TSV:   %s\n' "$OUT_TSV" >&2
