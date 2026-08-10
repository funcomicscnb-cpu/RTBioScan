#!/bin/bash

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "${SCRIPT_DIR}/lib/stale_lock_utils.sh"
FEEDER_LOCK_STALE_TTL_SECONDS=21600
feeder_lock_owned=0
GLOBAL_LEDGER_LOCK_STALE_TTL_SECONDS=300
global_ledger_lock_owned=0

# Initialize variables
num_reads="200000"
input_folder=""
run_id=""
sleep_time=300
targets="COI|ITS2"
targets_explicit=0
skip_pod5=0
do_metadata=0
delete_full_pod5=0

# Function to display help message
usage() {
    echo "Usage: $0 --run_id <run_id>  --input_folder <input_folder> [--num_reads <reads_chunks>] [--sleep_time <sleep_time>] [--metadata <metadata>] [--general_fasta <general_fasta>] [--primers_fasta <primers_fasta>] [--targets <marker1|marker2>] [--skip_pod5] [--do_metadata] [--delete_full_pod5]"
	echo "	run_id must match the name in the metadata file"
	echo "	input_folder is the folder where the pod5 will be retrieved from - Ideally the folder created from MinKnow as this will search for all the .pod5 in the folder recursively"
	echo "	--skip_pod5 to skip pod5 loop"
	echo "  By default metadata creation is skipped, run --do_metadata to do it"
	echo "	--metadata, --general_fasta, and --primers_fasta are required when --do_metadata is set"
    exit 1
}

# Parse command line arguments
while [[ "$1" != "" ]]; do
    case "$1" in
        --sleep_time )
            shift
            sleep_time="$1"
            ;;
        --num_reads )
            shift
            num_reads="$1"
            ;;
        --input_folder )
            shift
            input_folder="$1"
            ;;
        --run_id )
	    shift
            run_id="$1"
            ;;
		--metadata )
	    shift
            metadata="$1"
            ;;
		--general_fasta )
	    shift
            general_fasta="$1"
            ;;
		--primers_fasta )
	    shift
            primers_fasta="$1"
            ;;
		--targets )
	    shift
            targets="$1"
            targets_explicit=1
            ;;
		--skip_pod5 )
            skip_pod5=1
            ;;
		--do_metadata )
            do_metadata=1
            ;;
		--delete_full_pod5 )
            delete_full_pod5=1
            ;;
        -h | --help )
            usage
            ;;
        * )
            usage
            ;;
    esac
    shift
done

# Create directory if it does not already exist.
ensure_dir() { [ -d "$1" ] || mkdir -p "$1"; }

current_host_id() {
	if [ -n "${HOSTNAME:-}" ]; then
		printf '%s\n' "$HOSTNAME"
	elif command -v hostname >/dev/null 2>&1; then
		hostname 2>/dev/null || printf 'unknown\n'
	else
		printf 'unknown\n'
	fi
}

validate_explicit_targets() {
	local raw_targets="$1"
	if [[ ! "$raw_targets" =~ ^[^[:space:]|]+(\|[^[:space:]|]+)*$ ]]; then
		echo "ERROR: invalid --targets value; targets must be pipe-separated tokens without whitespace" >&2
		exit 1
	fi
}

metadata_stage_dir=""

cleanup_metadata_stage_dir() {
	if [ -n "$metadata_stage_dir" ] && [ -d "$metadata_stage_dir" ]; then
		rm -rf "$metadata_stage_dir"
	fi
}

rollback_metadata_commit() {
	local sample_info_dir="$1"
	local run_name="$2"
	local general_copy="$3"

	rm -f \
		"${sample_info_dir}/${run_name}_metadata.txt" \
		"${sample_info_dir}/demult.fasta" \
		"${sample_info_dir}/replicate_identity.tsv" \
		"${sample_info_dir}/replicate_roster.tsv" \
		"${sample_info_dir}/track_demult.fasta" \
		"${sample_info_dir}/track_roster.tsv" \
		"${sample_info_dir}/track_active_units.txt" \
		"${sample_info_dir}/track_identity.tsv" \
		"${sample_info_dir}/samples.txt" \
		"${sample_info_dir}/${general_copy}" \
		"${sample_info_dir}/primers.fasta"

	if [ -d "$sample_info_dir" ] && [ -z "$(ls -A "$sample_info_dir")" ]; then
		rmdir "$sample_info_dir" 2>/dev/null || true
	fi
}

list_available_runs() {
	local metadata_path="$1"
	awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		BEGIN {
			FS = "\t"
		}
		NR == 1 {
			$NF = trim_cr($NF)
			for (i = 1; i <= NF; i++) {
				header_idx[$i] = i
			}
			run_idx = header_idx["Run"]
			next
		}
		{
			$NF = trim_cr($NF)
			if (run_idx > 0 && run_idx <= NF && $(run_idx) != "") {
				seen[$(run_idx)] = 1
			}
		}
		END {
			for (run in seen) print run
		}
	' "$metadata_path" | sort -u | tr '\n' ' '
}

select_and_validate_metadata_rows() {
	local metadata_path="$1"
	local run_name="$2"
	local selected_out="$3"
	local row_info_out="$4"
	local replicate_roster_rows_out="$5"
	local samples_compat_rows_out="$6"
	awk -v run="$run_name" -v metadata_path="$metadata_path" -v row_info="$row_info_out" -v replicate_roster_rows="$replicate_roster_rows_out" -v samples_compat_rows="$samples_compat_rows_out" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function add_problem(msg) {
			if (problems != "") problems = problems "; "
			problems = problems msg
		}
		BEGIN {
			FS = "\t"
			OFS = " "
			mandatory_count = 7
			mandatory[1] = "Sample_ID"
			mandatory[2] = "Pipeline_ID"
			mandatory[3] = "Replicate"
			mandatory[4] = "Well"
			mandatory[5] = "Plate"
			mandatory[6] = "Run"
			mandatory[7] = "demult_id"
		}
		NR == 1 {
			raw_line = trim_cr($0)
			$NF = trim_cr($NF)
			for (i = 1; i <= NF; i++) {
				header_idx[$i] = i
			}
			for (i = 1; i <= mandatory_count; i++) {
				if (!(mandatory[i] in header_idx)) {
					missing[++missing_count] = mandatory[i]
				}
			}
			if (missing_count > 0) {
				msg = missing[1]
				for (i = 2; i <= missing_count; i++) msg = msg ", " missing[i]
				print "ERROR: metadata file " metadata_path " is missing mandatory header field(s): " msg > "/dev/stderr"
				exit 1
			}
			run_idx = header_idx["Run"]
			next
		}
		{
			raw_line = trim_cr($0)
			$NF = trim_cr($NF)
			if (run_idx > NF || $(run_idx) != run) next

			problems = ""
			for (i = 1; i <= mandatory_count; i++) {
				field = mandatory[i]
				idx = header_idx[field]
				value = (idx <= NF ? $(idx) : "")
				if (value == "") add_problem(field " is empty")
			}

				sample_id = $(header_idx["Sample_ID"])
				pipeline_id = $(header_idx["Pipeline_ID"])
				replicate = $(header_idx["Replicate"])
				well = $(header_idx["Well"])
				plate = $(header_idx["Plate"])
				demult_id = $(header_idx["demult_id"])

			if (replicate ~ /[[:space:]_]/) add_problem("Replicate must not contain whitespace or _")
			if (well ~ /[[:space:]_]/) add_problem("Well must not contain whitespace or _")
			if (plate ~ /[[:space:]_]/) add_problem("Plate must not contain whitespace or _")
			if (sample_id ~ /[[:space:]]/) add_problem("Sample_ID must not contain whitespace")
			if (pipeline_id ~ /[[:space:]]/) add_problem("Pipeline_ID must not contain whitespace")
			if ($(run_idx) ~ /[[:space:]]/) add_problem("Run must not contain whitespace")
			if (demult_id ~ /[[:space:]]/) add_problem("demult_id must not contain whitespace")

			expected_demult = ">" well "_" plate
			if (demult_id != "" && demult_id != expected_demult) {
				add_problem("demult_id must equal " expected_demult)
			}

			if (problems != "") {
				print "ERROR: metadata file " metadata_path ", line " NR ": " problems > "/dev/stderr"
				exit 1
			}

				print raw_line
				printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", NR, sample_id, pipeline_id, replicate, well, plate, demult_id, well "_" plate, replicate "_" well "_" plate >> row_info
				printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\n", sample_id, pipeline_id, replicate, well, plate, run, demult_id >> replicate_roster_rows
				printf "%s %s %s %s %s %s %s\n", sample_id, pipeline_id, replicate, well, plate, run, demult_id >> samples_compat_rows
				selected_count++
			}
	' "$metadata_path" > "$selected_out"
}

emit_track_demult_records_index() {
	local demult_fasta="$1"
	local out_path="$2"
	awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function flush_record() {
			if (header == "") return
			record_ordinal++
			printf "%s\t%s\t%s\n", record_ordinal, header, sequence
		}
		BEGIN {
			header = ""
			sequence = ""
		}
		/^>/ {
			flush_record()
			header = trim_cr(substr($0, 2))
			sequence = ""
			next
		}
		{
			sequence = sequence trim_cr($0)
		}
		END {
			flush_record()
		}
	' "$demult_fasta" > "$out_path"
}

emit_track_identity_index() {
	local replicate_identity_path="$1"
	local out_path="$2"
	awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function rename_header(name) {
			return (name == "replicate_id" ? "track_id" : name)
		}
		BEGIN {
			FS = "\t"
			OFS = "\t"
		}
		NR == 1 {
			printf "%s", "emitted_demult_ordinal"
			for (i = 1; i <= NF; i++) {
				printf "%s%s", OFS, rename_header(trim_cr($i))
			}
			printf "\n"
			next
		}
		{
			$NF = trim_cr($NF)
			row_ordinal++
			printf "%d", row_ordinal
			for (i = 1; i <= NF; i++) {
				printf "%s%s", OFS, $i
			}
			printf "\n"
		}
	' "$replicate_identity_path" > "$out_path"
}

emit_track_roster_identity_lines() {
	local indexed_identity_path="$1"
	local out_path="$2"
	awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		BEGIN {
			FS = "\t"
			OFS = "\t"
		}
		NR == 1 {
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			if (!("track_id" in header_idx) || !("metadata_line_no" in header_idx)) {
				print "ERROR: track identity helper is missing required columns track_id and metadata_line_no" > "/dev/stderr"
				exit 1
			}
			print "track_id", "metadata_line_no"
			next
		}
		{
			$NF = trim_cr($NF)
			key = $(header_idx["track_id"]) SUBSEP $(header_idx["metadata_line_no"])
			if (!(key in seen)) {
				seen[key] = 1
				print $(header_idx["track_id"]), $(header_idx["metadata_line_no"])
			}
		}
	' "$indexed_identity_path" > "$out_path"
}

emit_track_identity_views() {
	local demult_records_path="$1"
	local indexed_identity_path="$2"
	local first_out="$3"
	local final_out="$4"
	awk -v first_out="$first_out" -v final_out="$final_out" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function fatal(message) {
			print message > "/dev/stderr"
			exit 1
		}
		function add_source_line(key, line, seen_key) {
			if (line == "" || line == first_metadata_line[key]) return
			seen_key = key SUBSEP line
			if (!(seen_key in source_seen)) {
				source_seen[seen_key] = 1
				source_lines[key, ++source_count[key]] = line + 0
			}
		}
		function add_detail_token(key, token) {
			if (token != "") detail_tokens[key, token] = 1
		}
		function build_conflict_detail(key,    token_order, token_count, i, token, out) {
			token_count = split("sequence_mismatch|sample_id_mismatch|track_id_mismatch|replicate_number_mismatch|marker_id_mismatch|unit_suffix_mismatch|unit_id_collapse_mismatch|demult_id_metadata_mismatch|matched_general_fasta_header_mismatch|matched_general_fasta_record_index_mismatch|lookup_key_primary_mismatch|lookup_key_fallback_mismatch|lookup_grammar_used_mismatch", token_order, /\|/)
			out = ""
			for (i = 1; i <= token_count; i++) {
				token = token_order[i]
				if (detail_tokens[key, token]) out = out (out == "" ? "" : "|") token
			}
			return out
		}
		function build_source_list(key,    n, i, j, tmp, out, local_lines) {
			n = source_count[key]
			if (n == 0) return ""
			for (i = 1; i <= n; i++) local_lines[i] = source_lines[key, i]
			for (i = 1; i <= n; i++) {
				for (j = i + 1; j <= n; j++) {
					if ((local_lines[j] + 0) < (local_lines[i] + 0)) {
						tmp = local_lines[i]
						local_lines[i] = local_lines[j]
						local_lines[j] = tmp
					}
				}
			}
			out = ""
			for (i = 1; i <= n; i++) {
				out = out (i == 1 ? "" : ",") local_lines[i]
				delete local_lines[i]
			}
			return out
		}
		function build_detail(key,    token_order, token_count, i, token, out) {
			if (status[key] == "unique") return ""
			if (status[key] == "exact_duplicate_collapsed") return "later_rows_match_operational_fields"
			return build_conflict_detail(key)
		}
		FNR == NR {
			record_ordinal = $1 + 0
			demult_header[record_ordinal] = $2
			demult_sequence[record_ordinal] = $3
			if (record_ordinal > demult_record_count) demult_record_count = record_ordinal
			next
		}
		FNR == 1 {
			FS = "\t"
			OFS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			required_count = split("emitted_demult_ordinal|sample_id|track_id|replicate_number|marker_id|matched_general_fasta_header|matched_general_fasta_record_index|suffix_resolution_mode|unit_suffix_current|unit_id_collapse|unit_id_track|demult_id_metadata|lookup_key_primary|lookup_key_fallback|lookup_grammar_used|metadata_line_no", required_fields, /\|/)
			for (i = 1; i <= required_count; i++) {
				if (!(required_fields[i] in header_idx)) fatal("ERROR: track identity index is missing required column " required_fields[i])
			}
			print "emitted_demult_ordinal", "sample_id", "track_id", "replicate_number", "marker_id", "matched_general_fasta_header", "matched_general_fasta_record_index", "suffix_resolution_mode", "unit_suffix_current", "unit_id_collapse", "unit_id_track", "demult_id_metadata", "lookup_key_primary", "lookup_key_fallback", "lookup_grammar_used", "metadata_line_no" > first_out
			print "sample_id", "track_id", "replicate_number", "marker_id", "matched_general_fasta_header", "matched_general_fasta_record_index", "suffix_resolution_mode", "unit_suffix_current", "unit_id_collapse", "unit_id_track", "demult_id_metadata", "lookup_key_primary", "lookup_key_fallback", "lookup_grammar_used", "metadata_line_no", "track_duplicate_status", "track_duplicate_detail", "track_duplicate_source_metadata_lines" > final_out
			next
		}
		{
			$NF = trim_cr($NF)
			unit_key = $(header_idx["unit_id_track"])
			emitted_ordinal = $(header_idx["emitted_demult_ordinal"]) + 0
			metadata_line = $(header_idx["metadata_line_no"])
			current_sequence = demult_sequence[emitted_ordinal]
			current_collapse = $(header_idx["unit_id_collapse"])
			if (unit_key == "") fatal("ERROR: track identity index has an empty unit_id_track")
			if (emitted_ordinal < 1 || emitted_ordinal > demult_record_count) fatal("ERROR: emitted demult ordinal " emitted_ordinal " is out of bounds for unit_id_track " unit_key)
			if (!(emitted_ordinal in demult_header)) fatal("ERROR: missing demult.fasta record for emitted demult ordinal " emitted_ordinal)
			if (demult_header[emitted_ordinal] != current_collapse) fatal("ERROR: demult.fasta record " emitted_ordinal " header " demult_header[emitted_ordinal] " does not match replicate_identity unit_id_collapse " current_collapse)

			if (!(unit_key in seen_units)) {
				seen_units[unit_key] = 1
				order[++order_count] = unit_key
				status[unit_key] = "unique"
				first_metadata_line[unit_key] = metadata_line
				first_fields[unit_key, "emitted_demult_ordinal"] = emitted_ordinal
				first_fields[unit_key, "sample_id"] = $(header_idx["sample_id"])
				first_fields[unit_key, "track_id"] = $(header_idx["track_id"])
				first_fields[unit_key, "replicate_number"] = $(header_idx["replicate_number"])
				first_fields[unit_key, "marker_id"] = $(header_idx["marker_id"])
				first_fields[unit_key, "matched_general_fasta_header"] = $(header_idx["matched_general_fasta_header"])
				first_fields[unit_key, "matched_general_fasta_record_index"] = $(header_idx["matched_general_fasta_record_index"])
				first_fields[unit_key, "suffix_resolution_mode"] = $(header_idx["suffix_resolution_mode"])
				first_fields[unit_key, "unit_suffix_current"] = $(header_idx["unit_suffix_current"])
				first_fields[unit_key, "unit_id_collapse"] = current_collapse
				first_fields[unit_key, "unit_id_track"] = unit_key
				first_fields[unit_key, "demult_id_metadata"] = $(header_idx["demult_id_metadata"])
				first_fields[unit_key, "lookup_key_primary"] = $(header_idx["lookup_key_primary"])
				first_fields[unit_key, "lookup_key_fallback"] = $(header_idx["lookup_key_fallback"])
				first_fields[unit_key, "lookup_grammar_used"] = $(header_idx["lookup_grammar_used"])
				first_fields[unit_key, "metadata_line_no"] = metadata_line
				base_fields[unit_key, "sequence"] = current_sequence
				base_fields[unit_key, "sample_id"] = $(header_idx["sample_id"])
				base_fields[unit_key, "track_id"] = $(header_idx["track_id"])
				base_fields[unit_key, "replicate_number"] = $(header_idx["replicate_number"])
				base_fields[unit_key, "marker_id"] = $(header_idx["marker_id"])
				base_fields[unit_key, "unit_suffix_current"] = $(header_idx["unit_suffix_current"])
				base_fields[unit_key, "unit_id_collapse"] = current_collapse
				base_fields[unit_key, "demult_id_metadata"] = $(header_idx["demult_id_metadata"])
				base_fields[unit_key, "matched_general_fasta_header"] = $(header_idx["matched_general_fasta_header"])
				base_fields[unit_key, "matched_general_fasta_record_index"] = $(header_idx["matched_general_fasta_record_index"])
				base_fields[unit_key, "lookup_key_primary"] = $(header_idx["lookup_key_primary"])
				base_fields[unit_key, "lookup_key_fallback"] = $(header_idx["lookup_key_fallback"])
				base_fields[unit_key, "lookup_grammar_used"] = $(header_idx["lookup_grammar_used"])
				next
			}

			add_source_line(unit_key, metadata_line)
			mismatch = 0
			if (current_sequence != base_fields[unit_key, "sequence"]) {
				add_detail_token(unit_key, "sequence_mismatch")
				mismatch = 1
			}
			if ($(header_idx["sample_id"]) != base_fields[unit_key, "sample_id"]) {
				add_detail_token(unit_key, "sample_id_mismatch")
				mismatch = 1
			}
			if ($(header_idx["track_id"]) != base_fields[unit_key, "track_id"]) {
				add_detail_token(unit_key, "track_id_mismatch")
				mismatch = 1
			}
			if ($(header_idx["replicate_number"]) != base_fields[unit_key, "replicate_number"]) {
				add_detail_token(unit_key, "replicate_number_mismatch")
				mismatch = 1
			}
			if ($(header_idx["marker_id"]) != base_fields[unit_key, "marker_id"]) {
				add_detail_token(unit_key, "marker_id_mismatch")
				mismatch = 1
			}
			if ($(header_idx["unit_suffix_current"]) != base_fields[unit_key, "unit_suffix_current"]) {
				add_detail_token(unit_key, "unit_suffix_mismatch")
				mismatch = 1
			}
			if (current_collapse != base_fields[unit_key, "unit_id_collapse"]) {
				add_detail_token(unit_key, "unit_id_collapse_mismatch")
				mismatch = 1
			}
			if ($(header_idx["demult_id_metadata"]) != base_fields[unit_key, "demult_id_metadata"]) {
				add_detail_token(unit_key, "demult_id_metadata_mismatch")
				mismatch = 1
			}
			if ($(header_idx["matched_general_fasta_header"]) != base_fields[unit_key, "matched_general_fasta_header"]) {
				add_detail_token(unit_key, "matched_general_fasta_header_mismatch")
				mismatch = 1
			}
			if ($(header_idx["matched_general_fasta_record_index"]) != base_fields[unit_key, "matched_general_fasta_record_index"]) {
				add_detail_token(unit_key, "matched_general_fasta_record_index_mismatch")
				mismatch = 1
			}
			if ($(header_idx["lookup_key_primary"]) != base_fields[unit_key, "lookup_key_primary"]) {
				add_detail_token(unit_key, "lookup_key_primary_mismatch")
				mismatch = 1
			}
			if ($(header_idx["lookup_key_fallback"]) != base_fields[unit_key, "lookup_key_fallback"]) {
				add_detail_token(unit_key, "lookup_key_fallback_mismatch")
				mismatch = 1
			}
			if ($(header_idx["lookup_grammar_used"]) != base_fields[unit_key, "lookup_grammar_used"]) {
				add_detail_token(unit_key, "lookup_grammar_used_mismatch")
				mismatch = 1
			}

			if (mismatch) {
				fatal("ERROR: track identity duplicate conflict for unit_id_track " unit_key " detail=" build_conflict_detail(unit_key))
			} else if (status[unit_key] == "unique") {
				status[unit_key] = "exact_duplicate_collapsed"
			}
		}
		END {
			for (i = 1; i <= order_count; i++) {
				unit_key = order[i]
				detail = build_detail(unit_key)
				source_metadata_lines = build_source_list(unit_key)
				print first_fields[unit_key, "emitted_demult_ordinal"], first_fields[unit_key, "sample_id"], first_fields[unit_key, "track_id"], first_fields[unit_key, "replicate_number"], first_fields[unit_key, "marker_id"], first_fields[unit_key, "matched_general_fasta_header"], first_fields[unit_key, "matched_general_fasta_record_index"], first_fields[unit_key, "suffix_resolution_mode"], first_fields[unit_key, "unit_suffix_current"], first_fields[unit_key, "unit_id_collapse"], first_fields[unit_key, "unit_id_track"], first_fields[unit_key, "demult_id_metadata"], first_fields[unit_key, "lookup_key_primary"], first_fields[unit_key, "lookup_key_fallback"], first_fields[unit_key, "lookup_grammar_used"], first_fields[unit_key, "metadata_line_no"] > first_out
				print first_fields[unit_key, "sample_id"], first_fields[unit_key, "track_id"], first_fields[unit_key, "replicate_number"], first_fields[unit_key, "marker_id"], first_fields[unit_key, "matched_general_fasta_header"], first_fields[unit_key, "matched_general_fasta_record_index"], first_fields[unit_key, "suffix_resolution_mode"], first_fields[unit_key, "unit_suffix_current"], first_fields[unit_key, "unit_id_collapse"], first_fields[unit_key, "unit_id_track"], first_fields[unit_key, "demult_id_metadata"], first_fields[unit_key, "lookup_key_primary"], first_fields[unit_key, "lookup_key_fallback"], first_fields[unit_key, "lookup_grammar_used"], first_fields[unit_key, "metadata_line_no"], status[unit_key], detail, source_metadata_lines > final_out
			}
		}
	' "$demult_records_path" "$indexed_identity_path"
}

emit_track_demult_view() {
	local demult_records_path="$1"
	local first_identity_path="$2"
	local out_path="$3"
	awk -v out_path="$out_path" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function fatal(message) {
			print message > "/dev/stderr"
			exit 1
		}
		FNR == NR {
			record_ordinal = $1 + 0
			demult_sequence[record_ordinal] = $3
			next
		}
		FNR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			if (!("emitted_demult_ordinal" in header_idx) || !("unit_id_track" in header_idx)) {
				fatal("ERROR: track identity first helper is missing required columns")
			}
			next
		}
		{
			$NF = trim_cr($NF)
			emitted_ordinal = $(header_idx["emitted_demult_ordinal"]) + 0
			unit_key = $(header_idx["unit_id_track"])
			if (!(emitted_ordinal in demult_sequence)) fatal("ERROR: Missing demult sequence for emitted demult ordinal " emitted_ordinal)
			if (unit_key == "") fatal("ERROR: track identity first helper has an empty unit_id_track")
			printf ">%s\n%s\n", unit_key, demult_sequence[emitted_ordinal] >> out_path
		}
	' "$demult_records_path" "$first_identity_path"
}

emit_track_active_units_view() {
	local first_identity_path="$1"
	local out_path="$2"
	awk -v out_path="$out_path" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		NR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			if (!("unit_id_track" in header_idx)) {
				print "ERROR: track identity first helper is missing unit_id_track" > "/dev/stderr"
				exit 1
			}
			next
		}
		{
			$NF = trim_cr($NF)
			print $(header_idx["unit_id_track"]) >> out_path
		}
	' "$first_identity_path"
}

emit_track_roster_view() {
	local replicate_roster_path="$1"
	local roster_identity_lines_path="$2"
	local out_path="$3"
	awk -v out_path="$out_path" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function fatal(message) {
			print message > "/dev/stderr"
			exit 1
		}
		function add_source_line(key, line, seen_key) {
			if (line == "" || line == first_metadata_line[key]) return
			seen_key = key SUBSEP line
			if (!(seen_key in source_seen)) {
				source_seen[seen_key] = 1
				source_lines[key, ++source_count[key]] = line + 0
			}
		}
		function add_detail_token(key, token) {
			if (token != "") detail_tokens[key, token] = 1
		}
		function build_source_list(key,    n, i, j, tmp, out, local_lines) {
			n = source_count[key]
			if (n == 0) return ""
			for (i = 1; i <= n; i++) local_lines[i] = source_lines[key, i]
			for (i = 1; i <= n; i++) {
				for (j = i + 1; j <= n; j++) {
					if ((local_lines[j] + 0) < (local_lines[i] + 0)) {
						tmp = local_lines[i]
						local_lines[i] = local_lines[j]
						local_lines[j] = tmp
					}
				}
			}
			out = ""
			for (i = 1; i <= n; i++) {
				out = out (i == 1 ? "" : ",") local_lines[i]
				delete local_lines[i]
			}
			return out
		}
		function build_detail(key,    token_order, token_count, i, token, out) {
			if (status[key] == "unique") return ""
			if (status[key] == "exact_duplicate_collapsed") return "later_rows_match_operational_fields"
			token_count = split("sample_id_mismatch|replicate_number_mismatch|well_mismatch|plate_mismatch|run_id_mismatch|demult_id_metadata_mismatch", token_order, /\|/)
			out = ""
			for (i = 1; i <= token_count; i++) {
				token = token_order[i]
				if (detail_tokens[key, token]) out = out (out == "" ? "" : "|") token
			}
			return out
		}
		FNR == NR {
			if (FNR == 1) {
				FS = "\t"
				for (i = 1; i <= NF; i++) {
					header_idx_helper[trim_cr($i)] = i
				}
				if (!("track_id" in header_idx_helper) || !("metadata_line_no" in header_idx_helper)) {
					fatal("ERROR: track roster identity lines helper is missing required columns")
				}
				next
			}
			$NF = trim_cr($NF)
			track_key = $(header_idx_helper["track_id"])
			metadata_line = $(header_idx_helper["metadata_line_no"])
			if (!(track_key in first_metadata_line)) {
				first_metadata_line[track_key] = metadata_line
			} else {
				add_source_line(track_key, metadata_line)
			}
			next
		}
		FNR == 1 {
			FS = "\t"
			OFS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx_roster[trim_cr($i)] = i
			}
			required_count = split("sample_id|replicate_id|replicate_number|well|plate|run_id|demult_id_metadata", required_fields, /\|/)
			for (i = 1; i <= required_count; i++) {
				if (!(required_fields[i] in header_idx_roster)) fatal("ERROR: replicate_roster.tsv is missing required column " required_fields[i])
			}
			print "sample_id", "track_id", "replicate_number", "well", "plate", "run_id", "demult_id_metadata", "metadata_line_no_first", "track_duplicate_status", "track_duplicate_detail", "track_duplicate_source_metadata_lines" > out_path
			next
		}
		{
			$NF = trim_cr($NF)
			track_key = $(header_idx_roster["replicate_id"])
			if (track_key == "") fatal("ERROR: replicate_roster.tsv has an empty replicate_id")
			if (!(track_key in seen_tracks)) {
				seen_tracks[track_key] = 1
				order[++order_count] = track_key
				status[track_key] = "unique"
				first_fields[track_key, "sample_id"] = $(header_idx_roster["sample_id"])
				first_fields[track_key, "track_id"] = track_key
				first_fields[track_key, "replicate_number"] = $(header_idx_roster["replicate_number"])
				first_fields[track_key, "well"] = $(header_idx_roster["well"])
				first_fields[track_key, "plate"] = $(header_idx_roster["plate"])
				first_fields[track_key, "run_id"] = $(header_idx_roster["run_id"])
				first_fields[track_key, "demult_id_metadata"] = $(header_idx_roster["demult_id_metadata"])
				base_fields[track_key, "sample_id"] = $(header_idx_roster["sample_id"])
				base_fields[track_key, "replicate_number"] = $(header_idx_roster["replicate_number"])
				base_fields[track_key, "well"] = $(header_idx_roster["well"])
				base_fields[track_key, "plate"] = $(header_idx_roster["plate"])
				base_fields[track_key, "run_id"] = $(header_idx_roster["run_id"])
				base_fields[track_key, "demult_id_metadata"] = $(header_idx_roster["demult_id_metadata"])
				next
			}

			mismatch = 0
			if ($(header_idx_roster["sample_id"]) != base_fields[track_key, "sample_id"]) {
				add_detail_token(track_key, "sample_id_mismatch")
				mismatch = 1
			}
			if ($(header_idx_roster["replicate_number"]) != base_fields[track_key, "replicate_number"]) {
				add_detail_token(track_key, "replicate_number_mismatch")
				mismatch = 1
			}
			if ($(header_idx_roster["well"]) != base_fields[track_key, "well"]) {
				add_detail_token(track_key, "well_mismatch")
				mismatch = 1
			}
			if ($(header_idx_roster["plate"]) != base_fields[track_key, "plate"]) {
				add_detail_token(track_key, "plate_mismatch")
				mismatch = 1
			}
			if ($(header_idx_roster["run_id"]) != base_fields[track_key, "run_id"]) {
				add_detail_token(track_key, "run_id_mismatch")
				mismatch = 1
			}
			if ($(header_idx_roster["demult_id_metadata"]) != base_fields[track_key, "demult_id_metadata"]) {
				add_detail_token(track_key, "demult_id_metadata_mismatch")
				mismatch = 1
			}
			if (mismatch) {
				status[track_key] = "conflicting_duplicate_present"
			} else if (status[track_key] == "unique") {
				status[track_key] = "exact_duplicate_collapsed"
			}
		}
		END {
			for (i = 1; i <= order_count; i++) {
				track_key = order[i]
				if (!(track_key in first_metadata_line)) fatal("ERROR: Missing metadata line provenance for track_id " track_key)
				print first_fields[track_key, "sample_id"], first_fields[track_key, "track_id"], first_fields[track_key, "replicate_number"], first_fields[track_key, "well"], first_fields[track_key, "plate"], first_fields[track_key, "run_id"], first_fields[track_key, "demult_id_metadata"], first_metadata_line[track_key], status[track_key], build_detail(track_key), build_source_list(track_key) > out_path
			}
		}
	' "$roster_identity_lines_path" "$replicate_roster_path"
}

validate_track_artifacts() {
	local staged_replicate_identity="$1"
	local demult_records_path="$2"
	local first_identity_path="$3"
	local track_demult_path="$4"
	local track_active_units_path="$5"
	local track_identity_path="$6"
	local track_roster_path="$7"
	local configured_targets_raw="$8"
	local identity_rows
	local demult_record_count
	local track_demult_lines
	local track_demult_record_count
	local track_active_count
	local track_identity_rows

	identity_rows=$(( $(wc -l < "$staged_replicate_identity") - 1 ))
	demult_record_count=$(wc -l < "$demult_records_path")
	if [ "$identity_rows" -ne "$demult_record_count" ]; then
		echo "ERROR: replicate_identity.tsv data rows ($identity_rows) do not match demult.fasta records ($demult_record_count) before track artifact derivation." >&2
		return 1
	fi

	if ! awk -v max_ordinal="$demult_record_count" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		NR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			next
		}
		{
			$NF = trim_cr($NF)
			ordinal = $(header_idx["emitted_demult_ordinal"]) + 0
			if (ordinal < 1 || ordinal > max_ordinal) exit 1
			if (seen[ordinal]++) exit 1
		}
	' "$first_identity_path"; then
		echo "ERROR: track_identity_first.tsv has invalid emitted demult ordinals." >&2
		return 1
	fi

	track_demult_lines=$(wc -l < "$track_demult_path")
	if [ "$identity_rows" -gt 0 ] && { [ "$track_demult_lines" -le 0 ] || [ $(( track_demult_lines % 2 )) -ne 0 ]; }; then
		echo "ERROR: track_demult.fasta must contain a positive even number of lines when replicate_identity.tsv has data." >&2
		return 1
	fi
	track_demult_record_count=$(grep -c '^>' "$track_demult_path" || true)
	track_active_count=$(wc -l < "$track_active_units_path")
	track_identity_rows=$(( $(wc -l < "$track_identity_path") - 1 ))

	if [ "$track_demult_record_count" -ne "$track_active_count" ]; then
		echo "ERROR: track_demult.fasta records ($track_demult_record_count) do not match track_active_units.txt lines ($track_active_count)." >&2
		return 1
	fi
	if [ "$track_demult_record_count" -ne "$track_identity_rows" ]; then
		echo "ERROR: track_demult.fasta records ($track_demult_record_count) do not match track_identity.tsv rows ($track_identity_rows)." >&2
		return 1
	fi

	if ! awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		/^>/ {
			header = trim_cr(substr($0, 2))
			if (header == "") exit 1
			if (seen[header]++) exit 1
		}
	' "$track_demult_path"; then
		echo "ERROR: track_demult.fasta contains empty or duplicate headers." >&2
		return 1
	fi

	if ! awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		{
			value = trim_cr($0)
			if (value == "") exit 1
			if (seen[value]++) exit 1
		}
	' "$track_active_units_path"; then
		echo "ERROR: track_active_units.txt contains empty or duplicate values." >&2
		return 1
	fi

	if ! awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		NR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			next
		}
		{
			$NF = trim_cr($NF)
			value = $(header_idx["track_id"])
			if (value == "") exit 1
			if (seen[value]++) exit 1
		}
	' "$track_roster_path"; then
		echo "ERROR: track_roster.tsv contains empty or duplicate track_id values." >&2
		return 1
	fi

	if ! awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		NR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			next
		}
		{
			$NF = trim_cr($NF)
			value = $(header_idx["unit_id_track"])
			if (value == "") exit 1
			if (seen[value]++) exit 1
		}
	' "$track_identity_path"; then
		echo "ERROR: track_identity.tsv contains empty or duplicate unit_id_track values." >&2
		return 1
	fi

	if ! awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		FNR == NR {
			if ($0 ~ /^>/) headers[++header_count] = trim_cr(substr($0, 2))
			next
		}
		{
			values[++value_count] = trim_cr($0)
		}
		END {
			if (header_count != value_count) exit 1
			for (i = 1; i <= header_count; i++) {
				if (headers[i] != values[i]) exit 1
			}
			exit 0
		}
	' "$track_demult_path" "$track_active_units_path"; then
		echo "ERROR: track_active_units.txt does not match track_demult.fasta headers in order." >&2
		return 1
	fi

	if ! awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		FNR == NR {
			if ($0 ~ /^>/) headers[++header_count] = trim_cr(substr($0, 2))
			next
		}
		FNR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			next
		}
		{
			$NF = trim_cr($NF)
			values[++value_count] = $(header_idx["unit_id_track"])
		}
		END {
			if (header_count != value_count) exit 1
			for (i = 1; i <= header_count; i++) {
				if (headers[i] != values[i]) exit 1
			}
			exit 0
		}
	' "$track_demult_path" "$track_identity_path"; then
		echo "ERROR: track_identity.tsv unit_id_track values do not match track_demult.fasta headers in order." >&2
		return 1
	fi

	if ! awk '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		NR == FNR {
			if (FNR == 1) {
				FS = "\t"
				for (i = 1; i <= NF; i++) header_idx_identity[trim_cr($i)] = i
				next
			}
			$NF = trim_cr($NF)
			identity_tracks[$(header_idx_identity["track_id"])] = 1
			next
		}
		FNR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) header_idx_roster[trim_cr($i)] = i
			next
		}
		{
			$NF = trim_cr($NF)
			roster_tracks[$(header_idx_roster["track_id"])] = 1
		}
		END {
			for (track_id in identity_tracks) {
				if (!(track_id in roster_tracks)) exit 1
			}
			for (track_id in roster_tracks) {
				if (!(track_id in identity_tracks)) exit 1
			}
			exit 0
		}
	' "$track_identity_path" "$track_roster_path"; then
		echo "ERROR: track_identity.tsv and track_roster.tsv do not contain the same distinct track_id set." >&2
		return 1
	fi

	if ! awk -v targets_raw="$configured_targets_raw" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function fail(message) {
			print message > "/dev/stderr"
			exit 1
		}
		BEGIN {
			target_count = split(targets_raw, targets, /\|/)
			for (i = 1; i <= target_count; i++) {
				target = toupper(trim_cr(targets[i]))
				if (target == "") continue
				allowed[target] = 1
				allowed_target_count++
			}
			if (allowed_target_count == 0) {
				fail("ERROR: validate_track_artifacts requires at least one configured target marker")
			}
		}
		NR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			required_count = split("sample_id|track_id|marker_id|suffix_resolution_mode|unit_suffix_current|unit_id_collapse|unit_id_track", required_fields, /\|/)
			for (i = 1; i <= required_count; i++) {
				if (!(required_fields[i] in header_idx)) {
					fail("ERROR: track_identity.tsv is missing required semantic column " required_fields[i])
				}
			}
			next
		}
		{
			$NF = trim_cr($NF)
			sample_id = $(header_idx["sample_id"])
			track_id = $(header_idx["track_id"])
			marker_id = toupper($(header_idx["marker_id"]))
			suffix_mode = $(header_idx["suffix_resolution_mode"])
			unit_suffix = $(header_idx["unit_suffix_current"])
			unit_id_collapse = $(header_idx["unit_id_collapse"])
			unit_id_track = $(header_idx["unit_id_track"])
			if (suffix_mode != "marker") {
				fail("ERROR: track_identity.tsv contains non-marker suffix_resolution_mode for unit_id_track " unit_id_track)
			}
			if (unit_suffix != marker_id) {
				fail("ERROR: track_identity.tsv unit_suffix_current does not match marker_id for unit_id_track " unit_id_track)
			}
			if (unit_id_track != track_id "_" unit_suffix) {
				fail("ERROR: track_identity.tsv unit_id_track does not match track_id plus unit_suffix_current for unit_id_track " unit_id_track)
			}
			if (unit_id_collapse != sample_id "_" unit_suffix) {
				fail("ERROR: track_identity.tsv unit_id_collapse does not match sample_id plus unit_suffix_current for unit_id_track " unit_id_track)
			}
			if (!(marker_id in allowed)) {
				fail("ERROR: track_identity.tsv contains unexpected marker_id " marker_id " for track_id " track_id)
			}
			pair_key = track_id SUBSEP marker_id
			if (seen_pair[pair_key]++) {
				fail("ERROR: track_identity.tsv contains duplicate track_id/marker_id pair " track_id "/" marker_id)
			}
		}
	' "$track_identity_path"; then
		return 1
	fi
}

emit_track_view_warnings() {
	local scope="$1"
	local key_column="$2"
	local input_path="$3"
	awk -v scope="$scope" -v key_column="$key_column" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		NR == 1 {
			FS = "\t"
			for (i = 1; i <= NF; i++) {
				header_idx[trim_cr($i)] = i
			}
			next
		}
		{
			$NF = trim_cr($NF)
			status = $(header_idx["track_duplicate_status"])
			if (status == "unique") next
			detail = $(header_idx["track_duplicate_detail"])
			source_lines = $(header_idx["track_duplicate_source_metadata_lines"])
			if (detail == "") detail = "<none>"
			if (source_lines == "") source_lines = "<none>"
			printf "WARN: %s %s '\''%s'\'' final_status=%s detail=%s source_metadata_lines=%s\n", scope, key_column, $(header_idx[key_column]), status, detail, source_lines > "/dev/stderr"
		}
	' "$input_path"
}

derive_track_artifacts() {
	local staged_output_fasta="$1"
	local staged_replicate_identity="$2"
	local staged_replicate_roster="$3"
	local staged_track_demult="$4"
	local staged_track_roster="$5"
	local staged_track_active_units="$6"
	local staged_track_identity="$7"
	local configured_targets_raw="$8"
	local track_demult_records="${metadata_stage_dir}/track_demult_records.tsv"
	local track_identity_indexed="${metadata_stage_dir}/track_identity_indexed.tsv"
	local track_identity_first="${metadata_stage_dir}/track_identity_first.tsv"
	local track_roster_identity_lines="${metadata_stage_dir}/track_roster_identity_lines.tsv"

	if ! emit_track_demult_records_index "$staged_output_fasta" "$track_demult_records"; then
		return 1
	fi
	if ! emit_track_identity_index "$staged_replicate_identity" "$track_identity_indexed"; then
		return 1
	fi
	if ! emit_track_roster_identity_lines "$track_identity_indexed" "$track_roster_identity_lines"; then
		return 1
	fi
	if ! emit_track_identity_views "$track_demult_records" "$track_identity_indexed" "$track_identity_first" "$staged_track_identity"; then
		return 1
	fi
	: > "$staged_track_demult"
	: > "$staged_track_active_units"
	if ! emit_track_demult_view "$track_demult_records" "$track_identity_first" "$staged_track_demult"; then
		return 1
	fi
	if ! emit_track_active_units_view "$track_identity_first" "$staged_track_active_units"; then
		return 1
	fi
	if ! emit_track_roster_view "$staged_replicate_roster" "$track_roster_identity_lines" "$staged_track_roster"; then
		return 1
	fi
	if ! validate_track_artifacts "$staged_replicate_identity" "$track_demult_records" "$track_identity_first" "$staged_track_demult" "$staged_track_active_units" "$staged_track_identity" "$staged_track_roster" "$configured_targets_raw"; then
		return 1
	fi
	if ! emit_track_view_warnings "track_identity" "unit_id_track" "$staged_track_identity"; then
		return 1
	fi
	if ! emit_track_view_warnings "track_roster" "track_id" "$staged_track_roster"; then
		return 1
	fi
}

# Resolve one metadata row into one or more demultiplexing FASTA records.
# Usage: resolve_metadata_row <sample_name> <line_no> <replicate_id> <replicate_number> <well> <plate> <demult_id> <general_fasta> <primers_fasta> <targets> <metadata_path> <records_out> <sidecar_out> <meta_out> [track_identity_strict] [primers_used_out]
resolve_metadata_row() {
	local sample_name="$1"
	local line_no="$2"
	local replicate_id="$3"
	local replicate="$4"
	local well="$5"
	local plate="$6"
	local demult_id="$7"
	local fasta="$8"
	local primers_fasta="$9"
	local allowed_targets_raw="${10}"
	local metadata_path="${11}"
	local records_out="${12}"
	local sidecar_out="${13}"
	local meta_out="${14}"
	local track_identity_strict="${15:-0}"
	local primers_used_out="${16:-}"

	awk \
		-v sample_name="$sample_name" \
		-v line_no="$line_no" \
		-v replicate_id="$replicate_id" \
		-v replicate="$replicate" \
		-v well="$well" \
		-v plate="$plate" \
		-v demult_id="$demult_id" \
		-v fasta="$fasta" \
		-v primers_fasta="$primers_fasta" \
		-v allowed_targets_raw="$allowed_targets_raw" \
		-v metadata_path="$metadata_path" \
		-v records_out="$records_out" \
		-v sidecar_out="$sidecar_out" \
		-v meta_out="$meta_out" \
		-v track_identity_strict="$track_identity_strict" \
		-v primers_used_out="$primers_used_out" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function header_token(name, token) {
			token = trim_cr(name)
			sub(/[[:space:]].*$/, "", token)
			return token
		}
		function valid_token(token) {
			return (token != "" && token !~ /[[:space:]]/)
		}
		function fallback_suffix(key, hdr, idx, suffix) {
			suffix = header_token(hdr)
			if (substr(suffix, 1, length(key)) == key) {
				suffix = substr(suffix, length(key) + 1)
			}
			sub(/^[_| \t]+/, "", suffix)
			gsub(/[^A-Za-z0-9]+/, "_", suffix)
			gsub(/^_+|_+$/, "", suffix)
			if (suffix == "") suffix = "ALT" idx
			return toupper(suffix)
		}
		function parse_linked_parts(seq, parts, left, right, n) {
			n = split(seq, parts, /\.\.\./)
			if (n < 2) return ""
			left = parts[1]
			right = parts[2]
			sub(/;min_overlap=.*/, "", left)
			sub(/;min_overlap=.*/, "", right)
			return left "\t" right
		}
		function parse_primer_header(name, token, parts, n, family) {
			token = header_token(name)
			n = split(token, parts, "_")
			if (n < 2) return ""
			family = toupper(parts[1])
			if (!valid_token(family)) return ""
			return family
		}
		function fatal_exit(message, code) {
			fatal_error = 1
			fatal_code = code
			if (message != "") {
				print message > "/dev/stderr"
			}
			exit code
		}
		function load_allowed_targets(raw, parts, n, i, token) {
			raw = trim_cr(raw)
			if (raw !~ /^[^[:space:]|]+(\|[^[:space:]|]+)*$/) {
				fatal_exit("ERROR: invalid --targets value; targets must be pipe-separated tokens without whitespace", 1)
			}
			n = split(raw, parts, /\|/)
			for (i = 1; i <= n; i++) {
				token = toupper(parts[i])
				allowed_targets[token] = 1
				allowed_target_count++
			}
			return allowed_target_count
		}
		function classify_header(name, token) {
			token = header_token(name)
			if (token == key1) return "exact1"
			if (token == key2) return "exact2"
			return ""
		}
		function store_candidate(mode, header, seq, record_index) {
			if (mode == "exact1") {
				key1_count++
				key1_header[key1_count] = header
				key1_seq[key1_count] = seq
				key1_record_index[key1_count] = record_index
			} else if (mode == "exact2") {
				key2_count++
				key2_header[key2_count] = header
				key2_seq[key2_count] = seq
				key2_record_index[key2_count] = record_index
			}
		}
		function load_primers(path,    line, name, seq, parsed, parts, idx) {
			idx = 0
			name = ""
			seq = ""
			while ((getline line < path) > 0) {
				line = trim_cr(line)
				if (line ~ /^>/) {
					if (name != "") {
						idx++
						primer_name[idx] = name
						parsed = parse_linked_parts(seq, parts)
						split(parsed, parts, /\t/)
						primer_left[idx] = parts[1]
						primer_right[idx] = parts[2]
					}
					name = substr(line, 2)
					seq = ""
				} else {
					seq = seq line
				}
			}
			if (name != "") {
				idx++
				primer_name[idx] = name
				parsed = parse_linked_parts(seq, parts)
				split(parsed, parts, /\t/)
				primer_left[idx] = parts[1]
				primer_right[idx] = parts[2]
			}
			close(path)
			return idx
		}
		function process_record(header, seq, cls) {
			if (header == "") return
			fasta_record_index++
			cls = classify_header(header)
			if (cls == "exact1" || cls == "exact2") {
				store_candidate(cls, header, seq, fasta_record_index)
			}
		}
		function fail_row(message) {
			print "ERROR: metadata file " metadata_path ", line " line_no ", Sample_ID " sample_name ": " message > "/dev/stderr"
			exit 1
		}
		function resolve_candidates(mode, count, active_key, active_label,    i, p, parsed, parts, left, right, marker, candidate_token, field_count, marker_label, output_suffix, suffix_mode, unit_id_collapse, unit_id_track, current_header, current_seq, current_record_index) {
			for (i = 1; i <= count; i++) {
				if (mode == "exact1") {
					current_header = key1_header[i]
					current_seq = key1_seq[i]
					current_record_index = key1_record_index[i]
				} else {
					current_header = key2_header[i]
					current_seq = key2_seq[i]
					current_record_index = key2_record_index[i]
				}
				candidate_token = header_token(current_header)
				field_count = split(candidate_token, parts, "_")
				if (active_label == "WELL_PLATE") {
					if (field_count != 2 || parts[1] != well || parts[2] != plate) {
						fail_row(active_label " candidate " current_header " does not match metadata Well=" well " Plate=" plate)
					}
				} else if (active_label == "RAW_REPLICATE_WELL_PLATE") {
					if (field_count != 3 || parts[1] != replicate || parts[2] != well || parts[3] != plate) {
						fail_row(active_label " candidate " current_header " does not match metadata Replicate=" replicate " Well=" well " Plate=" plate)
					}
				}
				parsed = parse_linked_parts(current_seq, parts)
				split(parsed, parts, /\t/)
				left = parts[1]
				right = parts[2]
				marker = ""
				for (p = 1; p <= primer_count; p++) {
					if (left ~ (primer_left[p] "$") && right ~ ("^" primer_right[p])) {
						if (!used_primer[p] && primers_used_out != "") {
							print primer_name[p] > primers_used_out
							used_primer[p] = 1
						}
						marker_label = parse_primer_header(primer_name[p], primer_parts)
						if (marker_label == "") {
							fail_row("participating primer header " primer_name[p] " in " primers_fasta " is malformed; expected FAMILY_*")
						}
						if (marker == "") {
							marker = marker_label
						} else if (marker != marker_label) {
							fail_row(active_label " candidate " current_header " matched multiple primer markers (" marker " and " marker_label ") in " primers_fasta)
						}
					}
				}
				if (marker == "") {
					fail_row(active_label " candidate " current_header " has no matching primer entry in " primers_fasta)
				}
				if (!(marker in allowed_targets)) {
					fail_row(active_label " candidate " current_header " resolved marker " marker " which is not present in --targets " allowed_targets_raw)
				}
				resolved_marker[i] = marker
			}
				print active_label > meta_out
				for (i = 1; i <= count; i++) {
					if (mode == "exact1") {
						current_header = key1_header[i]
						current_seq = key1_seq[i]
						current_record_index = key1_record_index[i]
					} else {
						current_header = key2_header[i]
						current_seq = key2_seq[i]
						current_record_index = key2_record_index[i]
					}
					output_suffix = resolved_marker[i]
					suffix_mode = "marker"
					if (output_suffix == "") {
						fail_row(active_label " candidate " current_header " did not resolve to a marker")
					}
					if (seen_marker[output_suffix]) {
						if ((track_identity_strict + 0) != 0) {
							fail_row("Barcode key " active_key " matched multiple sequences for marker " output_suffix " in " fasta "; fallback suffixes are not permitted for track identity artifacts")
						}
						print "WARN: Barcode key " active_key " matched multiple sequences for marker " output_suffix " in " fasta "; using fallback suffix for duplicate header " current_header > "/dev/stderr"
						output_suffix = fallback_suffix(active_key, current_header, i)
						while (seen_marker[output_suffix]) {
							output_suffix = fallback_suffix(active_key, current_header, i "_" seen_marker[output_suffix])
						}
						suffix_mode = "fallback"
					}
					seen_marker[output_suffix]++
					unit_id_collapse = sample_name "_" output_suffix
					unit_id_track = replicate_id "_" output_suffix
					print ">" unit_id_collapse > records_out
					print current_seq > records_out
					printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
						sample_name,
						replicate_id,
						replicate,
						resolved_marker[i],
						current_header,
						current_record_index,
						suffix_mode,
						output_suffix,
						unit_id_collapse,
						unit_id_track,
						demult_id,
						key1,
						key2,
						active_label,
						line_no >> sidecar_out
				}
				return 0
			}
		BEGIN {
			key1 = well "_" plate
			key2 = replicate "_" well "_" plate
			allowed_target_count = load_allowed_targets(allowed_targets_raw)
			if (allowed_target_count == 0) {
				fatal_exit("ERROR: no valid targets were provided for metadata resolution", 1)
			}
			primer_count = load_primers(primers_fasta)
			if (primer_count == 0) {
				fatal_exit("ERROR: primers FASTA is empty or unreadable: " primers_fasta, 1)
			}
			current_header = ""
			current_seq = ""
		}
		/^>/ {
			process_record(current_header, current_seq)
			current_header = trim_cr(substr($0, 2))
			current_seq = ""
			next
		}
		{
			current_seq = current_seq trim_cr($0)
		}
				END {
					if (fatal_error) exit fatal_code
					process_record(current_header, current_seq)

					if (key1_count > 0) {
						active_key = key1
						active_label = "WELL_PLATE"
						resolve_candidates("exact1", key1_count, active_key, active_label)
						exit 0
					}

					if (key2_count > 0) {
						active_key = key2
						active_label = "RAW_REPLICATE_WELL_PLATE"
						resolve_candidates("exact2", key2_count, active_key, active_label)
						exit 0
					}

			fail_row("no exact general_fasta key matched either " key1 " or " key2 " in " fasta)
		}
	' "$fasta"
}

count_pod5_files() {
  local dir="$1"
  # Count .pod5 files robustly without relying on ls formatting
  local prev
  prev=$(shopt -p nullglob || true)
  shopt -s nullglob
  local files=("$dir"/*.pod5)
  local n=${#files[@]}
  # restore previous nullglob setting
  eval "$prev" 2>/dev/null || true
  echo "$n"
}

pod5_mtime_epoch() {
  local path="$1"
  if stat --version >/dev/null 2>&1; then
    stat -c %Y "$path"
  else
    stat -f %m "$path"
  fi
}

list_visible_pod5_files() {
  local dir="$1"
  find "$dir" -type f -name '*.pod5' ! -name '.*' ! -name '._*' -print0 2>/dev/null \
    | while IFS= read -r -d '' f; do
        printf '%s\t%s\n' "$(pod5_mtime_epoch "$f")" "$f"
      done \
    | sort -n \
    | cut -f2-
}

publish_pending_round() {
  local ready="${output_rt}"
  local spool="${output_folder}"

  # Trigger only when there are no files pending in ready queue
  local ready_count
  ready_count=$(count_pod5_files "$ready")
  if [ "$ready_count" -eq 0 ]; then
    # Find the OLDEST pending .pod5 file in the spool, cross-platform mtime
    local candidate
    candidate=$(
      find "$spool" -mindepth 1 -maxdepth 1 -type f -name '*.pod5' ! -name '.*' ! -name '._*' -print0 2>/dev/null \
      | while IFS= read -r -d '' f; do
        printf "%s\t%s\n" "$(pod5_mtime_epoch "$f")" "$f"
      done | sort -n | head -n1 | cut -f2- )

    if [ -n "$candidate" ]; then
      # Atomic move of exactly one file into the ready slot
      mv "$candidate" "$ready"/ || {
        echo "WARN: Failed to move pending round '$candidate' into $ready"
      }
    fi
  fi
}

progress_reads_file_for() {
  local file="$1"
  printf '%s/progress_reads_%s.count\n' "$metadata" "$file"
}

legacy_progress_file_for() {
  local file="$1"
  printf '%s/progress_%s.count\n' "$metadata" "$file"
}

consumed_reads_for_file() {
  local file="$1"
  local per_file_view="$2"
  local progress_reads_file
  local legacy_progress_file
  local total_reads
  local consumed

  progress_reads_file="$(progress_reads_file_for "$file")"
  legacy_progress_file="$(legacy_progress_file_for "$file")"
  total_reads=$(wc -l < "$per_file_view")

  if [ -f "$progress_reads_file" ]; then
    consumed=$(cat "$progress_reads_file")
  elif [ -f "$legacy_progress_file" ]; then
    consumed=$(( $(cat "$legacy_progress_file") * num_reads ))
  else
    consumed=0
  fi

  if [ "$consumed" -lt 0 ]; then
    consumed=0
  fi
  if [ "$consumed" -gt "$total_reads" ]; then
    consumed="$total_reads"
  fi
  echo "$consumed"
}

feeder_tmp_dir() {
  printf '%s/tmp\n' "$POD5_BASE"
}

round_id_counter_file() {
  printf '%s/next_round_id.count\n' "$metadata"
}

next_round_output_id() {
  local counter_file current max_id path filename suffix
  counter_file="$(round_id_counter_file)"
  max_id=-1

  for path in \
    "$done_round"/"${run_id}"_*.pod5 \
    "$metadata"/"${run_id}"_*_slice.tsv; do
    [ -e "$path" ] || continue
    filename=$(basename "$path")
    case "$filename" in
      "${run_id}"_*.pod5)
        suffix="${filename#${run_id}_}"
        suffix="${suffix%.pod5}"
        ;;
      "${run_id}"_*_slice.tsv)
        suffix="${filename#${run_id}_}"
        suffix="${suffix%_slice.tsv}"
        ;;
      *)
        continue
        ;;
    esac
    case "$suffix" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    if [ "$suffix" -gt "$max_id" ]; then
      max_id="$suffix"
    fi
  done

  current=$(( max_id + 1 ))
  if [ -f "$counter_file" ]; then
    current=$(cat "$counter_file" 2>/dev/null || printf '%s\n' "$current")
    case "$current" in
      ''|*[!0-9]*)
        current=$(( max_id + 1 ))
        ;;
    esac
  fi
  if [ "$current" -le "$max_id" ]; then
    current=$(( max_id + 1 ))
  fi

  printf '%s\n' "$(( current + 1 ))" > "$counter_file"
  printf '%s\n' "$current"
}

feeder_lock_dir() {
  printf '%s/.feeder.lockdir\n' "$metadata"
}

feeder_lock_meta_file() {
  printf '%s/meta.env\n' "$(feeder_lock_dir)"
}

remove_feeder_lock_if_stale() {
  local lock_dir tmp_lock
  lock_dir="$(feeder_lock_dir)"
  tmp_lock="${lock_dir}.stale.$$.$(date +%s 2>/dev/null || echo 0)"
  if mv "$lock_dir" "$tmp_lock" 2>/dev/null; then
    rm -rf "$tmp_lock" 2>/dev/null || true
  else
    rm -f "$lock_dir/meta.env" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || rm -rf "$lock_dir" 2>/dev/null || true
  fi
}

release_feeder_lock() {
	local lock_dir tmp_lock
	lock_dir="$(feeder_lock_dir)"
	if [ "${feeder_lock_owned:-0}" -eq 1 ]; then
    tmp_lock="${lock_dir}.release.$$.$(date +%s 2>/dev/null || echo 0)"
    if mv "$lock_dir" "$tmp_lock" 2>/dev/null; then
      rm -rf "$tmp_lock" 2>/dev/null || true
    else
      rm -f "$lock_dir/meta.env" 2>/dev/null || true
      rmdir "$lock_dir" 2>/dev/null || rm -rf "$lock_dir" 2>/dev/null || true
    fi
    feeder_lock_owned=0
	fi
}

exit_feeder_on_signal() {
	local exit_code="$1"
	release_feeder_lock
	exit "$exit_code"
}

acquire_feeder_lock_or_die() {
  local lock_dir lock_meta host reclaim_status started_epoch
  lock_dir="$(feeder_lock_dir)"
  lock_meta="$(feeder_lock_meta_file)"
  host="$(current_host_id)"
  started_epoch="$(date +%s 2>/dev/null || echo 0)"

  if ! mkdir "$lock_dir" 2>/dev/null; then
    stale_lock_maybe_reclaim \
      "$lock_dir" \
      "$lock_meta" \
      "$host" \
      "$FEEDER_LOCK_STALE_TTL_SECONDS" \
      "feeder lock" \
      remove_feeder_lock_if_stale \
      1
    reclaim_status=$?
    if [ "$reclaim_status" -eq 2 ]; then
      echo "ERROR: stale_lock_maybe_reclaim rejected feeder lock parameters" >&2
      exit 1
    fi
    if [ "$reclaim_status" -eq 11 ]; then
      echo "ERROR: unable to reclaim stale feeder lock at $lock_dir" >&2
      exit 1
    fi
    if ! mkdir "$lock_dir" 2>/dev/null; then
      echo "ERROR: another feeder is already active for run_id '$run_id' (lock: $lock_dir)" >&2
      exit 1
    fi
  fi

  if ! {
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$host"
    printf 'started_epoch=%s\n' "$started_epoch"
  } > "$lock_meta"; then
    release_feeder_lock
    echo "ERROR: failed to write feeder lock metadata at $lock_meta" >&2
    exit 1
  fi
  feeder_lock_owned=1
}

fullview_cache_meta_file_for() {
  local file="$1"
  printf '%s/fullview_%s.meta.env\n' "$metadata" "$file"
}

progress_committed_for_file() {
  local file="$1"
  local progress_reads_file
  local legacy_progress_file
  progress_reads_file="$(progress_reads_file_for "$file")"
  legacy_progress_file="$(legacy_progress_file_for "$file")"
  [ -f "$progress_reads_file" ] || [ -f "$legacy_progress_file" ]
}

source_size_bytes() {
  local path="$1"
  wc -c < "$path" | tr -d '[:space:]'
}

env_file_value() {
  local path="$1"
  local key="$2"
  [ -f "$path" ] || return 0
  awk -F= -v k="$key" '$1 == k { print substr($0, index($0, "=") + 1); exit }' "$path" 2>/dev/null
}

feeder_log_path() {
  printf '%s/%s_feeder.log\n' "$metadata" "$run_id"
}

duplicate_events_path() {
  printf '%s/duplicate_events.tsv\n' "$metadata"
}

local_round_commit_ledger_path() {
  printf '%s/round_commit_ledger.tsv\n' "$metadata"
}

validate_fail_env_for() {
  local file="$1"
  printf '%s/validate_fail_%s.env\n' "$metadata" "$file"
}

slice_sidecar_path_for_round() {
  local round_id="$1"
  printf '%s/%s_slice.tsv\n' "$metadata" "$round_id"
}

global_slice_completion_path() {
  printf '%s/slice_completion.tsv\n' "$GLOBAL_LEDGER_DIR"
}

global_ledger_lock_dir() {
  printf '%s/.lockdir\n' "$GLOBAL_LEDGER_DIR"
}

global_ledger_lock_meta_file() {
  printf '%s/meta.env\n' "$(global_ledger_lock_dir)"
}

feeder_log_event() {
  local action="$1"
  local subject="$2"
  local fingerprint="${3:-}"
  local fp_prefix ready_count spool_count now

  [ -n "${metadata:-}" ] || return 0
  ensure_dir "$metadata"
  fp_prefix="-"
  if [ -n "$fingerprint" ]; then
    fp_prefix=$(printf '%s' "$fingerprint" | cut -c1-8)
  fi
  ready_count=$(count_pod5_files "${output_rt:-.}" 2>/dev/null || echo 0)
  spool_count=$(count_pod5_files "${output_folder:-.}" 2>/dev/null || echo 0)
  now="$(date +%s 2>/dev/null || echo 0)"
  printf '%s\t%s\t%s\tR%s/S%s\t%s\n' "$now" "$subject" "$fp_prefix" "$ready_count" "$spool_count" "$action" >> "$(feeder_log_path)" 2>/dev/null || true
}

record_duplicate_event() {
  local kind="$1"
  local subject="$2"
  local fingerprint="${3:-}"
  local now
  now="$(date +%s 2>/dev/null || echo 0)"
  printf '%s\t%s\t%s\t%s\n' "$now" "$kind" "$subject" "$fingerprint" >> "$(duplicate_events_path)" 2>/dev/null || true
}

advance_progress_from_staged_ranges() {
  local progress_tsv="$1"
  local progress_file_name progress_start_line progress_reads progress_target current_progress
  [ -f "$progress_tsv" ] || return 0
  while IFS=$'\t' read -r progress_file_name progress_start_line progress_reads; do
    [ -n "$progress_file_name" ] || continue
    case "$progress_reads" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    progress_target="$(progress_reads_file_for "$progress_file_name")"
    if [ -f "$progress_target" ]; then
      current_progress="$(cat "$progress_target" 2>/dev/null || echo 0)"
    else
      current_progress=0
    fi
    case "$current_progress" in
      ''|*[!0-9]*)
        current_progress=0
        ;;
    esac
    if [ "$progress_reads" -gt "$current_progress" ]; then
      printf '%s\n' "$progress_reads" > "$progress_target" || return 1
    fi
  done < "$progress_tsv"
  return 0
}

_feeder_sha256() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 2>/dev/null | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 -r 2>/dev/null | awk '{print $1}'
  else
    return 1
  fi
}

compute_source_fp_from_view() {
  local per_file_view="$1"
  [ -f "$per_file_view" ] || return 1
  awk '{print $1}' "$per_file_view" | _feeder_sha256
}

write_validate_fail_env() {
  local fail_env="$1"
  local fail_count="$2"
  local fail_size="$3"
  local fail_mtime="$4"
  local tmp_env
  tmp_env="${fail_env}.tmp.$$"
  if ! {
    printf 'fail_count=%s\n' "$fail_count"
    printf 'fail_size=%s\n' "$fail_size"
    printf 'fail_mtime=%s\n' "$fail_mtime"
  } > "$tmp_env"; then
    rm -f "$tmp_env"
    return 1
  fi
  if ! mv "$tmp_env" "$fail_env"; then
    rm -f "$tmp_env"
    return 1
  fi
  return 0
}

load_validate_fail_count() {
  local fail_env="$1"
  local current_size="$2"
  local current_mtime="$3"
  local stored_size stored_mtime stored_count

  [ -f "$fail_env" ] || {
    printf '0\n'
    return 0
  }
  stored_size="$(env_file_value "$fail_env" 'fail_size')"
  stored_mtime="$(env_file_value "$fail_env" 'fail_mtime')"
  stored_count="$(env_file_value "$fail_env" 'fail_count')"
  case "$stored_count" in
    ''|*[!0-9]*)
      stored_count=0
      ;;
  esac
  if [ "$current_size" = "$stored_size" ] && [ "$current_mtime" = "$stored_mtime" ]; then
    printf '%s\n' "$stored_count"
  else
    printf '0\n'
  fi
}

remove_global_ledger_lock_if_stale() {
  local lock_dir
  lock_dir="$(global_ledger_lock_dir)"
  rm -f "$lock_dir/meta.env" 2>/dev/null || true
  rmdir "$lock_dir" 2>/dev/null || rm -rf "$lock_dir" 2>/dev/null || true
}

release_global_ledger_lock() {
  local lock_dir
  lock_dir="$(global_ledger_lock_dir)"
  if [ "${global_ledger_lock_owned:-0}" -eq 1 ]; then
    rm -f "$lock_dir/meta.env" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || rm -rf "$lock_dir" 2>/dev/null || true
    global_ledger_lock_owned=0
  fi
}

acquire_global_ledger_lock_best_effort() {
  local timeout_seconds="${1:-10}"
  local lock_dir lock_meta host started_epoch waited reclaim_status

  ensure_dir "$GLOBAL_LEDGER_DIR"
  lock_dir="$(global_ledger_lock_dir)"
  lock_meta="$(global_ledger_lock_meta_file)"
  host="$(current_host_id)"
  started_epoch="$(date +%s 2>/dev/null || echo 0)"
  waited=0

  while ! mkdir "$lock_dir" 2>/dev/null; do
    stale_lock_maybe_reclaim \
      "$lock_dir" \
      "$lock_meta" \
      "$host" \
      "$GLOBAL_LEDGER_LOCK_STALE_TTL_SECONDS" \
      "global feeder ledger lock" \
      remove_global_ledger_lock_if_stale \
      0
    reclaim_status=$?
    if [ "$reclaim_status" -eq 2 ] || [ "$reclaim_status" -eq 11 ]; then
      echo "WARNING: unable to recover global feeder ledger lock at $lock_dir" >&2
      return 1
    fi
    if mkdir "$lock_dir" 2>/dev/null; then
      break
    fi
    if [ "$waited" -ge "$timeout_seconds" ]; then
      echo "WARNING: global feeder ledger lock unavailable after ${timeout_seconds}s" >&2
      return 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
  done

  global_ledger_lock_owned=1
  if ! {
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$host"
    printf 'started_epoch=%s\n' "$started_epoch"
  } > "$lock_meta"; then
    release_global_ledger_lock
    echo "WARNING: failed to write global feeder ledger lock metadata at $lock_meta" >&2
    return 1
  fi
  return 0
}

write_fullview_cache_meta() {
  local meta_path="$1"
  local source_size="$2"
  local source_mtime="$3"
  local verified_reads="$4"
  local cache_rows="$5"
  local source_fp="$6"
  local tmp_meta
  tmp_meta="${meta_path}.tmp.$$"
  if ! {
    printf 'source_size=%s\n' "$source_size"
    printf 'source_mtime=%s\n' "$source_mtime"
    printf 'verified_reads=%s\n' "$verified_reads"
    printf 'cache_rows=%s\n' "$cache_rows"
    if [ -n "$source_fp" ]; then
      printf 'source_fp=%s\n' "$source_fp"
    fi
  } > "$tmp_meta"; then
    rm -f "$tmp_meta"
    return 1
  fi
  if ! mv "$tmp_meta" "$meta_path"; then
    rm -f "$tmp_meta"
    return 1
  fi
  return 0
}

ensure_source_fp_in_meta() {
  local file="$1"
  local per_file_view="$2"
  local meta_path="$3"
  local source_size source_mtime verified_reads cache_rows source_fp

  [ -f "$per_file_view" ] || return 1
  [ -f "$meta_path" ] || return 1
  source_fp="$(env_file_value "$meta_path" 'source_fp')"
  if [ -n "$source_fp" ]; then
    printf '%s\n' "$source_fp"
    return 0
  fi
  source_fp="$(compute_source_fp_from_view "$per_file_view")" || return 1
  source_size="$(env_file_value "$meta_path" 'source_size')"
  source_mtime="$(env_file_value "$meta_path" 'source_mtime')"
  verified_reads="$(env_file_value "$meta_path" 'verified_reads')"
  cache_rows="$(env_file_value "$meta_path" 'cache_rows')"
  if ! write_fullview_cache_meta "$meta_path" "$source_size" "$source_mtime" "$verified_reads" "$cache_rows" "$source_fp"; then
    return 1
  fi
  feeder_log_event "cache_backfill_fp" "$file" "$source_fp"
  printf '%s\n' "$source_fp"
}

compute_source_prefix() {
  local source_fp="$1"
  local tsv="$2"
  local current_run_path="${3:-}"
  [ -n "$source_fp" ] || {
    printf '0\n'
    return 0
  }
  [ -f "$tsv" ] || {
    printf '0\n'
    return 0
  }
  awk -F '\t' -v fp="$source_fp" -v run_path="$current_run_path" \
    '$2 == fp && (run_path == "" || $6 != run_path) { print $4 "\t" $5 }' "$tsv" \
    | sort -k1,1n \
    | awk 'BEGIN{p=0} {s=$1+0; e=$2+0; if (s<=p+1 && e>p) p=e} END{print p}'
}

slice_fp_exists_in_completion_ledger() {
  local slice_fp="$1"
  local ledger_path current_run_path
  ledger_path="$(global_slice_completion_path)"
  current_run_path="${RUN_PATH_ABS:-}"
  [ -n "$slice_fp" ] || return 1
  [ -f "$ledger_path" ] || return 1
  awk -F '\t' -v fp="$slice_fp" -v run_path="$current_run_path" '
    $1 == fp && (run_path == "" || $6 != run_path) { found = 1; exit }
    END { exit(found ? 0 : 1) }
  ' "$ledger_path"
}

slice_fp_exists_in_local_ledger() {
  local slice_fp="$1"
  local ledger_path
  ledger_path="$(local_round_commit_ledger_path)"
  [ -n "$slice_fp" ] || return 1
  [ -f "$ledger_path" ] || return 1
  awk -F '\t' -v fp="$slice_fp" '$1 == fp { found = 1; exit } END { exit(found ? 0 : 1) }' "$ledger_path"
}

append_local_round_commit_entry() {
  local slice_fp="$1"
  local round_id="$2"
  printf '%s\t%s\n' "$slice_fp" "$round_id" >> "$(local_round_commit_ledger_path)"
}

rebuild_local_round_commit_ledger() {
  local ledger_path tmp_ledger sidecar source_fp round_id
  ledger_path="$(local_round_commit_ledger_path)"
  tmp_ledger="${ledger_path}.tmp.$$"
  : > "$tmp_ledger"
  for sidecar in "$metadata"/"${run_id}"_*_slice.tsv; do
    [ -f "$sidecar" ] || continue
    source_fp="$(awk -F= '/^# slice_fingerprint=/{print $2; exit}' "$sidecar" 2>/dev/null || true)"
    [ -n "$source_fp" ] || continue
    round_id="$(basename "$sidecar")"
    round_id="${round_id%_slice.tsv}"
    printf '%s\t%s\n' "$source_fp" "$round_id" >> "$tmp_ledger"
  done
  mv "$tmp_ledger" "$ledger_path"
}

startup_validate_round_sidecars() {
  local repaired_any=0
  local sidecar round_id round_output_ori round_output_ready round_output_done metadata_output round_missing_progress
  local line source_fp file_name start_line end_line progress_target local_progress

  for sidecar in "$metadata"/"${run_id}"_*_slice.tsv; do
    [ -f "$sidecar" ] || continue
    round_id="$(basename "$sidecar")"
    round_id="${round_id%_slice.tsv}"
    round_output_ori="${output_folder}/${round_id}.pod5"
    round_output_ready="${output_rt}/${round_id}.pod5"
    round_output_done="${done_round}/${round_id}.pod5"
    metadata_output="${metadata}/${round_id}_read_info_rpt.txt"
    round_missing_progress=0

    while IFS= read -r line; do
      case "$line" in
        \#*|'')
          continue
          ;;
      esac
      IFS=$'\t' read -r source_fp file_name start_line end_line <<EOF
$line
EOF
      [ -n "$file_name" ] || continue
      progress_target="$(progress_reads_file_for "$file_name")"
      if [ -f "$progress_target" ]; then
        local_progress="$(cat "$progress_target" 2>/dev/null || echo 0)"
      else
        local_progress=0
      fi
      case "$local_progress" in
        ''|*[!0-9]*)
          local_progress=0
          ;;
      esac
      if [ "$local_progress" -lt "$end_line" ]; then
        round_missing_progress=1
        if [ -f "$round_output_ori" ] || [ -f "$round_output_ready" ]; then
          rm -f "$sidecar" "$round_output_ori" "$round_output_ready" "$metadata_output"
          feeder_log_event "startup_repair" "$round_id" ""
          echo "INFO: auto-repaired incomplete round commit for ${round_id}; round will be re-emitted." >&2
          repaired_any=1
        else
          feeder_log_event "startup_fail" "$round_id" ""
          echo "ERROR: incomplete progress commit for round ${round_id}: source ${file_name} expected through line ${end_line} but local progress is ${local_progress}. Manual repair required." >&2
          return 1
        fi
        break
      fi
    done < "$sidecar"

    if [ "$round_missing_progress" -ne 0 ]; then
      continue
    fi
  done

  rebuild_local_round_commit_ledger
  return 0
}

startup_cleanup_orphan_rounds() {
  local pod5_path round_id sidecar_path rpt_path rpt_round_id rpt_sidecar rpt_num

  for pod5_path in \
    "$output_folder"/"${run_id}"_*.pod5 \
    "$output_rt"/"${run_id}"_*.pod5; do
    [ -f "$pod5_path" ] || continue
    round_id="$(basename "$pod5_path")"
    round_id="${round_id%.pod5}"
    sidecar_path="$(slice_sidecar_path_for_round "$round_id")"
    [ -f "$sidecar_path" ] && continue
    rm -f \
      "${output_folder}/${round_id}.pod5" \
      "${output_rt}/${round_id}.pod5" \
      "${metadata}/${round_id}_read_info_rpt.txt"
    feeder_log_event "startup_orphan_cleanup" "$round_id" ""
    echo "INFO: removed orphan queued round ${round_id} (no sidecar); feeder will re-emit correct slice." >&2
  done

  for rpt_path in "$metadata"/"${run_id}"_*_read_info_rpt.txt; do
    [ -f "$rpt_path" ] || continue
    rpt_round_id="$(basename "$rpt_path")"
    rpt_round_id="${rpt_round_id%_read_info_rpt.txt}"
    rpt_num="${rpt_round_id#${run_id}_}"
    case "$rpt_num" in
      ''|*[!0-9]*)
        continue
        ;;
    esac
    rpt_sidecar="$(slice_sidecar_path_for_round "$rpt_round_id")"
    [ -f "$rpt_sidecar" ] && continue
    [ -f "${output_folder}/${rpt_round_id}.pod5" ] && continue
    [ -f "${output_rt}/${rpt_round_id}.pod5" ] && continue
    [ -f "${done_round}/${rpt_round_id}.pod5" ] && continue
    [ -f "${RESULTS_ROOT}/temp/ongoing/state/${run_id}/${rpt_round_id}/round_report.json" ] && continue
    rm -f "$rpt_path"
    feeder_log_event "startup_orphan_metadata_cleanup" "$rpt_round_id" ""
    echo "INFO: removed metadata-only orphan ${rpt_round_id} (no sidecar, no POD5)." >&2
  done

  return 0
}

startup_apply_global_completed_prefixes() {
  local archived_files archived_full file per_file_view meta_path source_fp local_progress global_prefix progress_target total_reads
  local ledger_path
  ledger_path="$(global_slice_completion_path)"

  if ! acquire_global_ledger_lock_best_effort 10; then
    return 0
  fi
  if [ ! -f "$ledger_path" ]; then
    release_global_ledger_lock
    return 0
  fi

  archived_files="$(list_visible_pod5_files "$output_full_pod5")"
  if [ -n "$archived_files" ]; then
    while IFS= read -r archived_full; do
      [ -n "$archived_full" ] || continue
      file="$(basename "$archived_full")"
      per_file_view="${metadata}/fullview_${file}.txt"
      meta_path="$(fullview_cache_meta_file_for "$file")"
      [ -f "$per_file_view" ] || continue
      [ -f "$meta_path" ] || continue
      source_fp="$(ensure_source_fp_in_meta "$file" "$per_file_view" "$meta_path" 2>/dev/null || true)"
      [ -n "$source_fp" ] || continue
      global_prefix="$(compute_source_prefix "$source_fp" "$ledger_path" "${RUN_PATH_ABS:-}")"
      case "$global_prefix" in
        ''|*[!0-9]*)
          global_prefix=0
          ;;
      esac
      progress_target="$(progress_reads_file_for "$file")"
      if [ -f "$progress_target" ]; then
        local_progress="$(cat "$progress_target" 2>/dev/null || echo 0)"
      else
        local_progress=0
      fi
      case "$local_progress" in
        ''|*[!0-9]*)
          local_progress=0
          ;;
      esac
      total_reads="$(wc -l < "$per_file_view" | tr -d '[:space:]')"
      if [ "$global_prefix" -gt "$total_reads" ]; then
        global_prefix="$total_reads"
      fi
      if [ "$global_prefix" -gt "$local_progress" ]; then
        printf '%s\n' "$global_prefix" > "$progress_target"
        feeder_log_event "startup_sync" "$file" "$source_fp"
        if [ "$global_prefix" -ge "$total_reads" ]; then
          record_duplicate_event "global_source_complete" "$file" "$source_fp"
        fi
      fi
    done <<< "$archived_files"
  fi
  release_global_ledger_lock
}

pod5_authoritative_read_count() {
  local pod5_path="$1"
  local inspect_output
  local count
  inspect_output="$(pod5 inspect summary "$pod5_path" 2>&1)" || return 1
  count="$(printf '%s\n' "$inspect_output" | sed -n 's/^Found .* batches, \([0-9][0-9]*\) reads$/\1/p' | tail -n 1)"
  case "$count" in
    ''|*[!0-9]*)
      return 1
      ;;
  esac
  printf '%s\n' "$count"
}

reads_time_has_file_entry() {
  local file="$1"
  [ -f "$reads_time" ] || return 1
  awk -F '\t' -v target="$file" 'NR > 1 && $5 == target { found = 1; exit } END { exit(found ? 0 : 1) }' "$reads_time"
}

record_reads_time_for_file() {
  local file="$1"
  local pod5_reads="$2"
  local time
  local last
  local cumulative
  if reads_time_has_file_entry "$file"; then
    return 0
  fi
  time=$(pod5_mtime_epoch "$output_full_pod5/$file")
  last=$(tail -n 1 "$reads_time" | awk '{print $4}')
  if [ "$last" = "file" ]; then
    last=0
  fi
  cumulative=$(( last + pod5_reads ))
  printf '%s\t%s\tsequencing\t%s\t%s\n' "$run_id" "$time" "$cumulative" "$file" >> "$reads_time"
}

mark_full_pod5_skipped() {
  local file="$1"
  local reason="$2"
  local archived_full="$3"
  echo
  echo "WARNING: ${reason}"
  mv -f "$archived_full" "$output_skipped_pod5/" 2>/dev/null || true
  touch "${metadata}/skipped_${file}.flag"
  echo "WARNING: Moved '${file}' to ${output_skipped_pod5}/ and flagged to prevent re-import."
  echo "WARNING: Continuing round with remaining valid pod5 files."
  echo
}

startup_recover_valid_skipped_pod5() {
  local skipped_path file flag_path dest read_count
  for skipped_path in "$output_skipped_pod5"/*.pod5; do
    [ -f "$skipped_path" ] || continue
    file="$(basename "$skipped_path")"
    flag_path="${metadata}/skipped_${file}.flag"
    [ -f "$flag_path" ] || continue
    if ! read_count="$(pod5_authoritative_read_count "$skipped_path" 2>/dev/null)"; then
      continue
    fi
    dest="${output_full_pod5}/${file}"
    if [ -e "$dest" ]; then
      echo "WARN: skipped POD5 ${file} is now readable (${read_count} reads) but ${dest} already exists; leaving skipped copy in place." >&2
      continue
    fi
    if mv -f "$skipped_path" "$dest"; then
      rm -f "$flag_path"
      feeder_log_event "skipped_recover" "$file" ""
      echo "INFO: recovered previously skipped POD5 ${file} (${read_count} reads)." >&2
    else
      echo "WARN: failed to recover readable skipped POD5 ${file}; leaving skipped flag in place." >&2
    fi
  done
}

refresh_full_pod5_view() {
  local out_path tmp_path archived_files archived_full file per_file_view
  out_path="${metadata}/full_pod5_view.txt"
  tmp_path="${out_path}.tmp.$$"
  : > "$tmp_path"
  archived_files="$(list_visible_pod5_files "$output_full_pod5")"
  if [ -n "$archived_files" ]; then
    while IFS= read -r archived_full; do
      [ -n "$archived_full" ] || continue
      file=$(basename "$archived_full")
      per_file_view="${metadata}/fullview_${file}.txt"
      [ -f "$per_file_view" ] || continue
      cat "$per_file_view" >> "$tmp_path" || {
        rm -f "$tmp_path"
        return 1
      }
    done <<< "$archived_files"
  fi
  mv "$tmp_path" "$out_path"
}

ensure_per_file_view_cache() {
  local archived_full="$1"
  local file="$2"
  local original_source="${3:-}"
  local per_file_view meta_path source_size source_mtime actual_rows authoritative_reads
  local meta_source_size meta_source_mtime meta_verified_reads meta_cache_rows
  local tmp_file_pod5 cache_tmp rebuild_attempted recopy_attempted inspect_ok fail_env fail_count source_fp

  per_file_view="${metadata}/fullview_${file}.txt"
  meta_path="$(fullview_cache_meta_file_for "$file")"
  fail_env="$(validate_fail_env_for "$file")"
  rebuild_attempted=0
  recopy_attempted=0

  while :; do
    if [ ! -e "$archived_full" ]; then
      if progress_committed_for_file "$file"; then
        echo "ERROR: missing archived full pod5 for '${file}' after progress was committed." >&2
        echo "ERROR: remove the corrupted run state before resuming. Archived=${archived_full}" >&2
        return 2
      fi
      feeder_log_event "validate_retry" "$file" ""
      return 1
    fi

    source_size="$(source_size_bytes "$archived_full")"
    source_mtime="$(pod5_mtime_epoch "$archived_full")"
    fail_count="$(load_validate_fail_count "$fail_env" "$source_size" "$source_mtime")"

    if [ -f "$per_file_view" ] && [ -f "$meta_path" ]; then
      meta_source_size="$(env_file_value "$meta_path" 'source_size')"
      meta_source_mtime="$(env_file_value "$meta_path" 'source_mtime')"
      meta_verified_reads="$(env_file_value "$meta_path" 'verified_reads')"
      meta_cache_rows="$(env_file_value "$meta_path" 'cache_rows')"
      actual_rows="$(wc -l < "$per_file_view" | tr -d '[:space:]')"
      if [ "$meta_source_size" = "$source_size" ] \
        && [ "$meta_source_mtime" = "$source_mtime" ] \
        && [ -n "$meta_verified_reads" ] \
        && [ "$meta_verified_reads" = "$meta_cache_rows" ] \
        && [ "$actual_rows" = "$meta_verified_reads" ]; then
        source_fp="$(ensure_source_fp_in_meta "$file" "$per_file_view" "$meta_path" 2>/dev/null || true)"
        rm -f "$fail_env"
        record_reads_time_for_file "$file" "$actual_rows"
        feeder_log_event "cache_hit" "$file" "$source_fp"
        return 0
      fi
    elif progress_committed_for_file "$file"; then
      echo "ERROR: missing or incomplete fullview cache state for '${file}' after progress was committed." >&2
      echo "ERROR: remove the corrupted run state before resuming. Cache=${per_file_view} Meta=${meta_path}" >&2
      return 2
    fi

    if [ -f "$per_file_view" ] || [ -f "$meta_path" ]; then
      if progress_committed_for_file "$file"; then
        echo "ERROR: detected fullview cache integrity mismatch for '${file}' after progress was committed." >&2
        echo "ERROR: remove the corrupted run state before resuming. Cache=${per_file_view} Meta=${meta_path}" >&2
        return 2
      fi
      rm -f "$per_file_view" "$meta_path"
    fi

    authoritative_reads="$(pod5_authoritative_read_count "$archived_full")"
    inspect_ok=$?
    if [ "$inspect_ok" -ne 0 ]; then
      if [ "$recopy_attempted" -eq 0 ] && [ -n "$original_source" ] && [ -r "$original_source" ]; then
        rm -f "$archived_full"
        if cp -p "$original_source" "$archived_full" && [ -s "$archived_full" ]; then
          recopy_attempted=1
          feeder_log_event "validate_retry" "$file" ""
          continue
        fi
        rm -f "$archived_full"
      fi
      fail_count=$(( fail_count + 1 ))
      write_validate_fail_env "$fail_env" "$fail_count" "$source_size" "$source_mtime" || true
      feeder_log_event "validate_fail_${fail_count}" "$file" ""
      if [ "$fail_count" -ge 2 ]; then
        rm -f "$fail_env"
        mark_full_pod5_skipped "$file" "pod5 inspect summary failed twice for '${file}' — file is truncated or corrupted." "$archived_full"
        feeder_log_event "corrupt_skip" "$file" ""
      fi
      return 1
    fi

    tmp_file_pod5="$(feeder_tmp_dir)/tmp_file_pod5_${run_id}_$$.txt"
    cache_tmp="${per_file_view}.tmp.$$"
    rm -f "$tmp_file_pod5" "$cache_tmp"
    pod5 view "$archived_full" --no-header --output "$tmp_file_pod5"
    if [ ! $? -eq 0 ]; then
      rm -f "$tmp_file_pod5" "$cache_tmp"
      if [ "$recopy_attempted" -eq 0 ] && [ -n "$original_source" ] && [ -r "$original_source" ]; then
        rm -f "$archived_full"
        if cp -p "$original_source" "$archived_full" && [ -s "$archived_full" ]; then
          recopy_attempted=1
          feeder_log_event "validate_retry" "$file" ""
          continue
        fi
        rm -f "$archived_full"
      fi
      fail_count=$(( fail_count + 1 ))
      write_validate_fail_env "$fail_env" "$fail_count" "$source_size" "$source_mtime" || true
      feeder_log_event "validate_fail_${fail_count}" "$file" ""
      if [ "$fail_count" -ge 2 ]; then
        rm -f "$fail_env"
        mark_full_pod5_skipped "$file" "pod5 view failed twice for '${file}' — file is truncated or corrupted." "$archived_full"
        feeder_log_event "corrupt_skip" "$file" ""
      fi
      return 1
    fi
    if [ ! -s "$tmp_file_pod5" ]; then
      rm -f "$tmp_file_pod5" "$cache_tmp"
      fail_count=$(( fail_count + 1 ))
      write_validate_fail_env "$fail_env" "$fail_count" "$source_size" "$source_mtime" || true
      feeder_log_event "validate_fail_${fail_count}" "$file" ""
      if [ "$fail_count" -ge 2 ]; then
        rm -f "$fail_env"
        mark_full_pod5_skipped "$file" "pod5 view produced no readable rows twice for '${file}'." "$archived_full"
        feeder_log_event "corrupt_skip" "$file" ""
      fi
      return 1
    fi

    actual_rows="$(wc -l < "$tmp_file_pod5" | tr -d '[:space:]')"
    if [ "$actual_rows" != "$authoritative_reads" ]; then
      rm -f "$tmp_file_pod5" "$cache_tmp"
      if [ "$rebuild_attempted" -eq 0 ]; then
        rebuild_attempted=1
        continue
      fi
      if [ "$recopy_attempted" -eq 0 ] && [ -n "$original_source" ] && [ -r "$original_source" ]; then
        rm -f "$archived_full"
        if cp -p "$original_source" "$archived_full" && [ -s "$archived_full" ]; then
          recopy_attempted=1
          rebuild_attempted=0
          feeder_log_event "validate_retry" "$file" ""
          continue
        fi
        rm -f "$archived_full"
      fi
      fail_count=$(( fail_count + 1 ))
      write_validate_fail_env "$fail_env" "$fail_count" "$source_size" "$source_mtime" || true
      feeder_log_event "validate_fail_${fail_count}" "$file" ""
      if [ "$fail_count" -ge 2 ]; then
        rm -f "$fail_env"
        mark_full_pod5_skipped "$file" "fullview cache validation failed twice for '${file}': pod5 inspect summary reports ${authoritative_reads} reads but pod5 view produced ${actual_rows} rows." "$archived_full"
        feeder_log_event "corrupt_skip" "$file" ""
      fi
      return 1
    fi

    if ! cp "$tmp_file_pod5" "$cache_tmp"; then
      rm -f "$tmp_file_pod5" "$cache_tmp"
      echo "WARNING: failed to stage per-file pod5 view for '${file}'; skipping cumulative update this cycle."
      return 1
    fi
    source_fp="$(compute_source_fp_from_view "$tmp_file_pod5" 2>/dev/null || true)"
    if ! write_fullview_cache_meta "$meta_path" "$source_size" "$source_mtime" "$authoritative_reads" "$actual_rows" "$source_fp"; then
      rm -f "$tmp_file_pod5" "$cache_tmp" "$per_file_view" "$meta_path"
      echo "WARNING: failed to finalize per-file pod5 view metadata for '${file}'; skipping cumulative update this cycle."
      return 1
    fi
    if ! mv "$cache_tmp" "$per_file_view"; then
      rm -f "$tmp_file_pod5" "$cache_tmp" "$per_file_view" "$meta_path"
      echo "WARNING: failed to finalize per-file pod5 view for '${file}'; skipping cumulative update this cycle."
      return 1
    fi
    if ! refresh_full_pod5_view; then
      echo "WARNING: failed to refresh ${metadata}/full_pod5_view.txt for '${file}'; continuing with validated per-file cache."
    fi
    rm -f "$fail_env"
    record_reads_time_for_file "$file" "$actual_rows"
    feeder_log_event "import" "$file" "$source_fp"
    rm -f "$tmp_file_pod5"
    return 0
  done
}

# Check if required variables are set
#if [ -z "$run_id" ] || [ -z "$input_folder" ]; then
if [ -z "$run_id" ]; then
    echo "Error: --run_id is required."
	echo
    usage
fi
if [ $skip_pod5 -eq 0 ]; then
	if [ -z "$input_folder" ]; then
		echo "Error: --input_folder is required."
		echo
		usage
	fi
	if [ ! -e "$input_folder" ];then
		echo "Error: Input folder does not exist, set a different one with --input_folder"
		echo
		usage
	fi
fi

# Print the values of the options
echo
echo "RunID: $run_id"
echo "Input folder: $input_folder"
echo "Num reads: $num_reads"
echo "Sleep time: $sleep_time"
echo "Targets: $targets"

if [ "$targets_explicit" -eq 1 ]; then
	validate_explicit_targets "$targets"
fi

if [ $do_metadata -eq 1 ] && [ "$targets_explicit" -eq 0 ]; then
	echo "WARNING: --do_metadata is using the implicit default --targets '$targets'."
	echo "         Pass --targets explicitly to keep metadata setup aligned with the run marker configuration."
fi


SAMPLE_INFO_DIR="results/sample_info/${run_id}"
POD5_BASE="results/pod5/${run_id}"

#Metadata creation
if [ $do_metadata -eq 1 ]; then
	#Checks on the variables existing
	if [ -z "$metadata" ];then
		echo "Error: --metadata is required when --do_metadata is set."
		usage
	fi
	if [ ! -e "$metadata" ];then
		echo
		echo "Error: metadata file $metadata doesn't exist."
		echo
		usage
	fi
	if [ -z "$general_fasta" ];then
		echo "Error: --general_fasta is required when --do_metadata is set."
		usage
	fi
	if [ ! -e "$general_fasta" ];then
		echo
		echo "Error: general fasta file $general_fasta doesn't exist."
		usage
	fi
	if [ -z "$primers_fasta" ];then
		echo "Error: --primers_fasta is required when --do_metadata is set."
		usage
	fi
	if [ ! -e "$primers_fasta" ];then
		echo
		echo "Error: primers fasta file $primers_fasta doesn't exist."
		usage
	fi
	output_fasta="${SAMPLE_INFO_DIR}/demult.fasta"

	echo
	echo "Starting metadata Creation"
	echo "Metadata file: $metadata"
	echo "General_fasta: $general_fasta"
	echo "Primers fasta: $primers_fasta"
	echo "$run_id primers: $output_fasta"
	echo

	#Input_files creation - Skip if already created
	sample_info_parent="results/sample_info"
	ensure_dir "$sample_info_parent"
	if [ -d "$SAMPLE_INFO_DIR" ] && [ -n "$( ls -A "$SAMPLE_INFO_DIR" )" ];then echo "Folder $SAMPLE_INFO_DIR already exists and isn't empty, skipping metadata";fi
	if [ ! -d "$SAMPLE_INFO_DIR" ] || [ -z "$( ls -A "$SAMPLE_INFO_DIR" )" ];then
		echo "Folder $SAMPLE_INFO_DIR is empty, creating metadata"
		echo

		metadata_stage_dir=$(mktemp -d "${sample_info_parent}/.${run_id}.metadata.XXXXXX") || {
			echo "ERROR: Failed to create staging directory under $sample_info_parent" >&2
			exit 1
		}
		trap 'cleanup_metadata_stage_dir; exit 1' INT TERM HUP
		trap 'cleanup_metadata_stage_dir' EXIT

				staged_metadata="${metadata_stage_dir}/${run_id}_metadata.txt"
				row_info_file="${metadata_stage_dir}/row_info.tsv"
				replicate_roster_rows_file="${metadata_stage_dir}/replicate_roster_rows.tsv"
				samples_compat_rows_file="${metadata_stage_dir}/samples_compat_rows.txt"
				staged_output_fasta="${metadata_stage_dir}/demult.fasta"
				staged_replicate_identity="${metadata_stage_dir}/replicate_identity.tsv"
				staged_replicate_roster="${metadata_stage_dir}/replicate_roster.tsv"
				staged_track_demult="${metadata_stage_dir}/track_demult.fasta"
				staged_track_roster="${metadata_stage_dir}/track_roster.tsv"
				staged_track_active_units="${metadata_stage_dir}/track_active_units.txt"
				staged_track_identity="${metadata_stage_dir}/track_identity.tsv"
				staged_samples="${metadata_stage_dir}/samples.txt"
				staged_primers_names="${metadata_stage_dir}/primers_used.txt"
				warn_seen_file="${metadata_stage_dir}/.demult_warn_seen"
				: > "$row_info_file"
				: > "$replicate_roster_rows_file"
				: > "$samples_compat_rows_file"
				: > "$warn_seen_file"
				metadata_track_identity_strict="${RTBIOSCAN_TRACK_IDENTITY_STRICT:-1}"
				track_artifacts_optional="${RTBIOSCAN_TRACK_ARTIFACTS_OPTIONAL:-0}"
				case "$metadata_track_identity_strict" in
					1|true|TRUE|yes|YES|on|ON) metadata_track_identity_strict=1 ;;
					0|false|FALSE|no|NO|off|OFF) metadata_track_identity_strict=0 ;;
					*) metadata_track_identity_strict=1 ;;
				esac
				case "$track_artifacts_optional" in
					1|true|TRUE|yes|YES|on|ON) track_artifacts_optional=1 ;;
					0|false|FALSE|no|NO|off|OFF) track_artifacts_optional=0 ;;
					*) track_artifacts_optional=0 ;;
				esac
				: > "$staged_output_fasta"
				: > "$staged_primers_names"
				printf '%s\n' "sample_id	replicate_id	replicate_number	marker_id	matched_general_fasta_header	matched_general_fasta_record_index	suffix_resolution_mode	unit_suffix_current	unit_id_collapse	unit_id_track	demult_id_metadata	lookup_key_primary	lookup_key_fallback	lookup_grammar_used	metadata_line_no" > "$staged_replicate_identity"

			if ! select_and_validate_metadata_rows "$metadata" "$run_id" "$staged_metadata" "$row_info_file" "$replicate_roster_rows_file" "$samples_compat_rows_file"; then
				exit 1
			fi

		# Verify run_id matched at least one row in the metadata file
		samples_number=$(wc -l < "$staged_metadata")
		if [ "$samples_number" -eq 0 ]; then
			available_runs=$(list_available_runs "$metadata")
			echo
			echo "ERROR: run_id '$run_id' not found in Run column of $metadata"
			echo "  Available run IDs: ${available_runs:-<none found>}"
			echo "  Check --run_id matches one of the above values."
			echo
			usage
		fi

		#Demultiplexing fasta
		# Sample_ID is the FASTA header base.
		# Lookup resolves in order: WELL_PLATE, then RAW_REPLICATE_WELL_PLATE only if the first grammar has no exact candidates.
		# Each active candidate must resolve cleanly through primer-based marker assignment.
		expected_records=0
			while IFS=$'\t' read -r line_no sample_name replicate_id replicate well plate demult_id key1 key2; do
				tmp_records="${metadata_stage_dir}/.demult_match.tmp"
				tmp_sidecar="${metadata_stage_dir}/.demult_match.sidecar"
				tmp_warn="${metadata_stage_dir}/.demult_match.warn"
				tmp_meta="${metadata_stage_dir}/.demult_match.meta"
				tmp_primers="${metadata_stage_dir}/.demult_match.primers"
				: > "$tmp_meta"
				: > "$tmp_sidecar"
				: > "$tmp_primers"
				if ! resolve_metadata_row "$sample_name" "$line_no" "$replicate_id" "$replicate" "$well" "$plate" "$demult_id" "$general_fasta" "$primers_fasta" "$targets" "$metadata" "$tmp_records" "$tmp_sidecar" "$tmp_meta" "$metadata_track_identity_strict" "$tmp_primers" 2> "$tmp_warn"; then
					if [ -s "$tmp_warn" ]; then
						cat "$tmp_warn" >&2
					fi
					rm -f "$tmp_records" "$tmp_sidecar" "$tmp_warn" "$tmp_meta" "$tmp_primers"
					exit 1
				fi
				if [ -s "$tmp_warn" ]; then
					while IFS= read -r warn_line; do
					[ -n "$warn_line" ] || continue
					if ! grep -F -x -q -- "$warn_line" "$warn_seen_file"; then
						printf '%s\n' "$warn_line" >> "$warn_seen_file"
						printf '%s\n' "$warn_line" >&2
					fi
				done < "$tmp_warn"
			fi
			grammar_used=$(tr -d '\r\n' < "$tmp_meta")
				rm -f "$tmp_warn" "$tmp_meta"
				record_count=$(grep -c '^>' "$tmp_records" || true)
				sidecar_count=$(wc -l < "$tmp_sidecar")
				if [ "$record_count" -le 0 ]; then
					echo "ERROR: Failed to emit demultiplexing FASTA records for Sample_ID '$sample_name' using keys '$key1' and '$key2'"
					rm -f "$tmp_records" "$tmp_sidecar" "$tmp_primers"
					exit 1
				fi
				if [ "$sidecar_count" -ne "$record_count" ]; then
					echo "ERROR: replicate_identity.tsv rows ($sidecar_count) do not match demult.fasta records ($record_count) for Sample_ID '$sample_name'" >&2
					rm -f "$tmp_records" "$tmp_sidecar" "$tmp_primers"
					exit 1
				fi
				cat "$tmp_records" >> "$staged_output_fasta"
				cat "$tmp_sidecar" >> "$staged_replicate_identity"
				cat "$tmp_primers" >> "$staged_primers_names"
				rm -f "$tmp_records" "$tmp_sidecar" "$tmp_primers"
				expected_records=$(( expected_records + record_count ))
			done < "$row_info_file"
			rm -f "$warn_seen_file"

			# List of samples in the run using the canonical compatibility projection.
			sort "$samples_compat_rows_file" | uniq > "$staged_samples"
			printf '%s\n' "sample_id	replicate_id	replicate_number	well	plate	run_id	demult_id_metadata" > "$staged_replicate_roster"
			cat "$replicate_roster_rows_file" >> "$staged_replicate_roster"


		#Check that demult.fasta has exactly the emitted record count (2 lines each)
		fasta_number=$(wc -l < "$staged_output_fasta")
		expected=$(( expected_records * 2 ))
			if [ ! "$fasta_number" -gt 0 ] || [ ! "$fasta_number" -eq "$expected" ]; then
				echo "ERROR: demult.fasta has $fasta_number lines; expected $expected ($expected_records records × 2 lines)."
				echo "       Check that selected metadata rows resolve unambiguously against $general_fasta"
				echo
				usage
			fi
			sidecar_rows=$(wc -l < "$staged_replicate_identity")
			sidecar_expected=$(( expected_records + 1 ))
				if [ ! "$sidecar_rows" -eq "$sidecar_expected" ]; then
					echo "ERROR: replicate_identity.tsv has $sidecar_rows lines; expected $sidecar_expected (header + $expected_records record rows)." >&2
					exit 1
				fi
				replicate_roster_rows=$(wc -l < "$replicate_roster_rows_file")
				if [ ! "$replicate_roster_rows" -eq "$samples_number" ]; then
					echo "ERROR: replicate_roster_rows.tsv has $replicate_roster_rows lines; expected $samples_number (one row per selected metadata row)." >&2
					exit 1
				fi
				replicate_roster_lines=$(wc -l < "$staged_replicate_roster")
				replicate_roster_expected=$(( samples_number + 1 ))
				if [ ! "$replicate_roster_lines" -eq "$replicate_roster_expected" ]; then
					echo "ERROR: replicate_roster.tsv has $replicate_roster_lines lines; expected $replicate_roster_expected (header + $samples_number roster rows)." >&2
					exit 1
				fi
			track_artifacts_error="${metadata_stage_dir}/track_artifacts.err"
			if [ "$track_artifacts_optional" -eq 1 ]; then
				if ! derive_track_artifacts "$staged_output_fasta" "$staged_replicate_identity" "$staged_replicate_roster" "$staged_track_demult" "$staged_track_roster" "$staged_track_active_units" "$staged_track_identity" "$targets" 2> "$track_artifacts_error"; then
					if [ -s "$track_artifacts_error" ]; then
						head -n 1 "$track_artifacts_error" | sed 's/^/WARN: optional track artifact derivation skipped: /' >&2
					else
						echo "WARN: optional track artifact derivation skipped." >&2
					fi
					rm -f "$staged_track_demult" "$staged_track_roster" "$staged_track_active_units" "$staged_track_identity"
				fi
			else
				if ! derive_track_artifacts "$staged_output_fasta" "$staged_replicate_identity" "$staged_replicate_roster" "$staged_track_demult" "$staged_track_roster" "$staged_track_active_units" "$staged_track_identity" "$targets"; then
					exit 1
				fi
			fi
			rm -f "$track_artifacts_error"

		general_copy_name=$(basename "$general_fasta")
		if [ ! -e "$SAMPLE_INFO_DIR" ]; then
			if ! mkdir -p "$SAMPLE_INFO_DIR"; then
				echo "ERROR: Failed to create output directory $SAMPLE_INFO_DIR" >&2
				rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
				exit 1
			fi
		fi
		if ! cp "$general_fasta" "$SAMPLE_INFO_DIR/"; then
			echo "ERROR: Failed to copy general fasta into $SAMPLE_INFO_DIR" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
		if ! awk \
				-v lf="$staged_primers_names" \
				'FILENAME==lf{sub(/\r$/,"");used[$0]=1;next} {sub(/\r$/,"")} /^>/{p=(substr($0,2) in used);if(p)print;next} p{print}' \
				"$staged_primers_names" "$primers_fasta" > "${SAMPLE_INFO_DIR}/primers.fasta"; then
			echo "ERROR: Failed to extract run-specific primers into $SAMPLE_INFO_DIR" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
		if [ "$(grep -c '^>' "${SAMPLE_INFO_DIR}/primers.fasta" || true)" -eq 0 ]; then
			echo "ERROR: No primer entries matched run metadata; primers.fasta is empty" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
		if ! mv "$staged_metadata" "${SAMPLE_INFO_DIR}/${run_id}_metadata.txt"; then
			echo "ERROR: Failed to write ${run_id}_metadata.txt into $SAMPLE_INFO_DIR" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
			if ! mv "$staged_output_fasta" "$output_fasta"; then
				echo "ERROR: Failed to write demult.fasta into $SAMPLE_INFO_DIR" >&2
				rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
				exit 1
			fi
				if ! mv "$staged_replicate_identity" "${SAMPLE_INFO_DIR}/replicate_identity.tsv"; then
					echo "ERROR: Failed to write replicate_identity.tsv into $SAMPLE_INFO_DIR" >&2
					rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
					exit 1
				fi
				if ! mv "$staged_replicate_roster" "${SAMPLE_INFO_DIR}/replicate_roster.tsv"; then
					echo "ERROR: Failed to write replicate_roster.tsv into $SAMPLE_INFO_DIR" >&2
					rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
					exit 1
				fi
				if ! mv "$staged_samples" "${SAMPLE_INFO_DIR}/samples.txt"; then
					echo "ERROR: Failed to write samples.txt into $SAMPLE_INFO_DIR" >&2
					rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
		if [ -e "$staged_track_demult" ]; then
			if ! mv "$staged_track_demult" "${SAMPLE_INFO_DIR}/track_demult.fasta"; then
				echo "ERROR: Failed to write track_demult.fasta into $SAMPLE_INFO_DIR" >&2
				rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
				exit 1
			fi
		fi
		if [ -e "$staged_track_roster" ]; then
			if ! mv "$staged_track_roster" "${SAMPLE_INFO_DIR}/track_roster.tsv"; then
				echo "ERROR: Failed to write track_roster.tsv into $SAMPLE_INFO_DIR" >&2
				rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
				exit 1
			fi
		fi
		if [ -e "$staged_track_active_units" ]; then
			if ! mv "$staged_track_active_units" "${SAMPLE_INFO_DIR}/track_active_units.txt"; then
				echo "ERROR: Failed to write track_active_units.txt into $SAMPLE_INFO_DIR" >&2
				rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
				exit 1
			fi
		fi
		if [ -e "$staged_track_identity" ]; then
			if ! mv "$staged_track_identity" "${SAMPLE_INFO_DIR}/track_identity.tsv"; then
				echo "ERROR: Failed to write track_identity.tsv into $SAMPLE_INFO_DIR" >&2
				rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
				exit 1
			fi
		fi
		echo "Demultiplexing fasta completed successfully ($expected_records records, $fasta_number lines)"
		echo
		rm -rf "$metadata_stage_dir"
		metadata_stage_dir=""
		trap - INT TERM HUP EXIT

	fi
else
	echo "Skipped metadata creation, use --do_metadata to run."
fi


if [ $skip_pod5 -eq 1 ];then
	echo "Pod5 creation skipped"
	exit 0
fi
echo
echo "Starting the pod5 creation"
echo


#Output will be in results/pod5/${run_id}/ and there a copy of the pod5 will be generated in full_pod5
output_full_pod5="${POD5_BASE}/full_pod5"
output_folder="${POD5_BASE}/ori_round_pod5"
output_rt="${POD5_BASE}/reads_rt_round_pod5"
done_round="${POD5_BASE}/done_round_pod5"
metadata="${POD5_BASE}/metadata"
output_skipped_pod5="${POD5_BASE}/skipped_pod5"
for _d in "$POD5_BASE" "$output_full_pod5" "$output_folder" "$output_rt" "$done_round" "$metadata" "$output_skipped_pod5"; do
    ensure_dir "$_d"
done
RESULTS_ROOT="$(cd "$(dirname "$(dirname "$POD5_BASE")")" && pwd -P)"
GLOBAL_LEDGER_DIR="${RESULTS_ROOT}/temp/_global/feeder_dedup"
RUN_PATH_ABS="$(cd "$POD5_BASE" && pwd -P)"
trap 'release_feeder_lock' EXIT
trap 'exit_feeder_on_signal 130' INT
trap 'exit_feeder_on_signal 143' TERM
trap 'exit_feeder_on_signal 129' HUP
acquire_feeder_lock_or_die



#round=0
#Add loop to find the actual round
#counter=0
reads_time="${metadata}/${run_id}_reads_time_rpt.txt"
if [ ! -s "$reads_time" ]; then
	echo -e "run_id\ttime\tdata\treads\tfile" > $reads_time
fi

startup_recover_valid_skipped_pod5
startup_cleanup_orphan_rounds
startup_apply_global_completed_prefixes
if ! startup_validate_round_sidecars; then
	exit 1
fi


for ((;;));do
	startup_recover_valid_skipped_pod5
	if [ ! -e $input_folder ];then 
		#To add here mail or message to warn something is not working:
		echo "Warning!!!!!!!!!"
		echo "$input_folder doesn't exist - Check the connection with MinIon still works or that the folder name hasn't changed!"
		echo "Warning!!!!!!!!!!"
		tput bel
		tput bel
		tput bel
		tput bel
                tput bel
                tput bel
                tput bel
                tput bel
                tput bel
                tput bel
                tput bel
                tput bel
                tput bel
		echo
		echo sleeping $sleep_time seconds
		sleep $sleep_time
		echo "starting over"
		echo
		continue
	fi

	# Global backpressure: if either queue has > 1 files, pause this iteration
	ready_count=$(count_pod5_files "${output_rt}")
	spool_count=$(count_pod5_files "${output_folder}")
	if [ "$ready_count" -gt 1 ] || [ "$spool_count" -gt 1 ]; then
		echo "Throttle: ready=${ready_count} spool=${spool_count}; letting downstream catch up"
		publish_pending_round
		echo sleeping $sleep_time seconds
		sleep $sleep_time
		echo "starting over"
		continue
	fi
	round=0
	counter=0
	breaking=0
	ensure_dir "$(feeder_tmp_dir)"
	rm -f "${POD5_BASE}/tmp_file_pod5" "${POD5_BASE}/la" "${POD5_BASE}/tmp_read_info_rpt.txt" "${POD5_BASE}/tmp_round_progress.tsv"

    # Global throttle: allow prefetching 1 in spool while 1 is in ready.
    # First, if ready is empty and spool has backlog, promote exactly one.
    ready_count=$(count_pod5_files "${output_rt}")
    spool_count=$(count_pod5_files "${output_folder}")
    if [ "$ready_count" -eq 0 ] && [ "$spool_count" -gt 0 ]; then
        echo "Promoting one .pod5 from ori_round_pod5 to reads_rt_round_pod5"
        publish_pending_round
        # Let downstream detect the new ready file before continuing
        sleep 2
        echo "starting over"
        continue
    fi

    # Wait only if:
    #  - ready > 1, or
    #  - spool > 1, or
    #  - both ready >= 1 AND spool >= 1 (one in each already)
    if [ "$ready_count" -gt 1 ] || [ "$spool_count" -gt 1 ] || { [ "$ready_count" -ge 1 ] && [ "$spool_count" -ge 1 ]; }; then
        echo "Throttle: ready=${ready_count} spool=${spool_count}; waiting before importing/splitting"
        publish_pending_round
        echo sleeping $sleep_time seconds
        sleep $sleep_time
        echo "starting over"
        continue
    fi
	full_files=$(list_visible_pod5_files "$input_folder")
	active_record="${metadata}/active_full.txt"
	rm -f "$active_record"
	if [ ! -z "$full_files" ]; then
		while IFS= read -r full_file; do
			[ -n "$full_file" ] || continue
			file=$(basename "$full_file")
			# Skip files permanently flagged as bad in a previous round.
			if [ -f "${metadata}/skipped_${file}.flag" ]; then
				continue
			fi
			# Skip files that were fully consumed and deleted.
			if [ -f "${metadata}/deleted_${file}.flag" ]; then
				continue
			fi
			if [ ! -r "$full_file" ]; then
				echo
				echo "WARNING: pod5 source file is not readable; skipping this file and continuing with remaining files."
				echo "  File: $full_file"
				echo "  (If this persists the file may be corrupted. Remove it from the source folder to stop this warning.)"
				echo
				continue
			fi
			per_file_view="${metadata}/fullview_${file}.txt"
			if [ ! -e "$output_full_pod5/$file" ]; then
				ready_count=$(count_pod5_files "${output_rt}")
				spool_count=$(count_pod5_files "${output_folder}")
				if [ "$ready_count" -gt 1 ] || [ "$spool_count" -gt 0 ]; then
					echo "Backpressure (pre-import): ready=${ready_count} spool=${spool_count}; delaying new full pod5 import"
					breaking=1
					break
				fi
				echo "Processing $full_file"
				echo
				cp -p "$full_file" "$output_full_pod5/$file"
				if [ ! $? -eq 0 ]; then
					rm -f "$output_full_pod5/$file"
					echo "WARNING: $full_file failed to copy (possibly still being written); skipping this file and continuing with remaining files."
					continue
				fi
				if [ ! -s "$output_full_pod5/$file" ]; then
					rm -f "$output_full_pod5/$file"
					echo "WARNING: $file archived as zero-byte; skipping this iteration."
					continue
				fi
			fi

			ensure_per_file_view_cache "$output_full_pod5/$file" "$file" "$full_file"
			cache_status=$?
			if [ "$cache_status" -eq 1 ]; then
				continue
			fi
			if [ "$cache_status" -eq 2 ]; then
				exit 1
			fi
		done <<< "$full_files"

		if [ $breaking -eq 1 ]; then
			breaking=0
		else
			archived_files=$(list_visible_pod5_files "$output_full_pod5")
			if [ -n "$archived_files" ]; then
				tmp_read_info="$(feeder_tmp_dir)/tmp_read_info_${run_id}_$$.tsv"
				tmp_round_ids="$(feeder_tmp_dir)/tmp_round_ids_${run_id}_$$.txt"
				tmp_progress="$(feeder_tmp_dir)/tmp_round_progress_${run_id}_$$.tsv"
				: > "$tmp_read_info"
				: > "$tmp_round_ids"
				: > "$tmp_progress"
				round_header=""
				collected_reads=0
				declare -a round_inputs=()

				while IFS= read -r archived_full; do
					[ -n "$archived_full" ] || continue
					file=$(basename "$archived_full")
					per_file_view="${metadata}/fullview_${file}.txt"
					[ -f "$per_file_view" ] || continue
					total_reads=$(wc -l < "$per_file_view")
					consumed_reads=$(consumed_reads_for_file "$file" "$per_file_view")
					if [ "$consumed_reads" -ge "$total_reads" ]; then
						if [ "$delete_full_pod5" -eq 1 ] && [ -f "$archived_full" ]; then
							rm -f "$archived_full" 2>/dev/null || true
							touch "${metadata}/deleted_${file}.flag"
							echo "INFO: deleted fully-consumed full_pod5: $file" >&2
						fi
						continue
					fi

					remaining_reads=$(( total_reads - consumed_reads ))
					needed_reads=$(( num_reads - collected_reads ))
					take_reads="$remaining_reads"
					if [ "$take_reads" -gt "$needed_reads" ]; then
						take_reads="$needed_reads"
					fi
					start_line=$(( consumed_reads + 1 ))
					end_line=$(( consumed_reads + take_reads ))

					sed -n "${start_line},${end_line}p" "$per_file_view" >> "$tmp_read_info"
					sed -n "${start_line},${end_line}p" "$per_file_view" | awk '{print $1}' >> "$tmp_round_ids"
					printf '%s\t%s\t%s\n' "$file" "$start_line" "$end_line" >> "$tmp_progress"
					round_inputs+=("$archived_full")
					if [ -z "$round_header" ]; then
						round_header=$(pod5 view "$archived_full" | head -1)
					fi
					collected_reads=$(( collected_reads + take_reads ))
					if [ "$collected_reads" -ge "$num_reads" ]; then
						break
					fi
				done <<< "$archived_files"

				if [ "$collected_reads" -ge "$num_reads" ]; then
					ready_count=$(count_pod5_files "${output_rt}")
					spool_count=$(count_pod5_files "${output_folder}")
					if [ "$ready_count" -gt 1 ] || [ "$spool_count" -gt 0 ]; then
						echo "Backpressure (pre-filter): ready=${ready_count} spool=${spool_count}; stopping round creation"
					else
						id_count=$(wc -l < "$tmp_round_ids")
						read_info_count=$(wc -l < "$tmp_read_info")
						round_inputs_missing=0
						for round_input in "${round_inputs[@]}"; do
							if [ ! -f "$round_input" ]; then
								echo "WARNING: round input disappeared before pod5 filter: $round_input"
								round_inputs_missing=1
							fi
						done
						if [ ! -s "$tmp_round_ids" ]; then
							echo "WARNING: skipping round creation because staged round IDs are missing or empty."
						elif [ "$id_count" -ne "$read_info_count" ]; then
							echo "WARNING: skipping round creation because staged read rows (${read_info_count}) do not match staged IDs (${id_count})."
						elif [ "${#round_inputs[@]}" -eq 0 ]; then
							echo "WARNING: skipping round creation because no source pod5 inputs were staged."
						elif [ "$round_inputs_missing" -ne 0 ]; then
							echo "WARNING: skipping round creation because one or more staged pod5 inputs disappeared."
						else
						slice_fp="$( _feeder_sha256 < "$tmp_round_ids" 2>/dev/null || true )"
						if [ -z "$slice_fp" ]; then
							echo "WARNING: failed to hash staged round IDs; round emission aborted."
						elif slice_fp_exists_in_local_ledger "$slice_fp"; then
							echo "WARNING: skipping duplicate local round slice"
							record_duplicate_event "local_slice_duplicate" "${run_id}_duplicate" "$slice_fp"
							feeder_log_event "local_dup_skip" "${run_id}_duplicate" "$slice_fp"
							if ! advance_progress_from_staged_ranges "$tmp_progress"; then
								echo "WARNING: failed to advance local progress for duplicate local round slice."
							fi
						else
						global_duplicate=0
						global_check_ok=0
						if acquire_global_ledger_lock_best_effort 10; then
							global_check_ok=1
							if slice_fp_exists_in_completion_ledger "$slice_fp"; then
								global_duplicate=1
							fi
							release_global_ledger_lock
						fi
						if [ "$global_check_ok" -ne 1 ]; then
							echo "WARNING: deferring round emission because the global feeder ledger lock is unavailable; no round ID was reserved."
							feeder_log_event "global_lock_defer" "${run_id}_duplicate" "$slice_fp"
						elif [ "$global_duplicate" -eq 1 ]; then
							echo "WARNING: skipping globally completed round slice"
							record_duplicate_event "global_slice_duplicate" "${run_id}_duplicate" "$slice_fp"
							feeder_log_event "global_dup_skip" "${run_id}_duplicate" "$slice_fp"
							if ! advance_progress_from_staged_ranges "$tmp_progress"; then
								echo "WARNING: failed to advance local progress for duplicate global round slice."
							fi
						else
						out_id=$(next_round_output_id)
						round_id="${run_id}_${out_id}"
						round_output="${output_folder}/${round_id}.pod5"
						rm -f "$round_output"
						if ! pod5 filter "${round_inputs[@]}" --ids "$tmp_round_ids" --output "$round_output"; then
							echo "WARNING: pod5 filter failed for round ${round_id}; round emission aborted."
							rm -f "$round_output"
						else
							metadata_output="${metadata}/${round_id}_read_info_rpt.txt"
							metadata_tmp="${metadata_output}.tmp.$$"
							sidecar_output="$(slice_sidecar_path_for_round "$round_id")"
							sidecar_tmp="${sidecar_output}.tmp.$$"
							rm -f "$metadata_tmp"
							if ! { printf '%s\n' "$round_header" > "$metadata_tmp" && cat "$tmp_read_info" >> "$metadata_tmp"; }; then
								echo "WARNING: failed to stage round metadata for ${round_id}; round emission aborted."
								rm -f "$round_output" "$metadata_tmp" "$sidecar_tmp" "$sidecar_output"
							else
								rm -f "$sidecar_tmp"
								{
									printf '# slice_fingerprint=%s\n' "$slice_fp"
									printf '# read_count=%s\n' "$id_count"
									printf '# commit_timestamp=%s\n' "$(date +%s 2>/dev/null || echo 0)"
									while IFS=$'\t' read -r progress_file_name progress_start_line progress_reads; do
										[ -n "$progress_file_name" ] || continue
										per_file_view="${metadata}/fullview_${progress_file_name}.txt"
										meta_path="$(fullview_cache_meta_file_for "$progress_file_name")"
										source_fp="$(ensure_source_fp_in_meta "$progress_file_name" "$per_file_view" "$meta_path" 2>/dev/null || true)"
										[ -n "$source_fp" ] || source_fp="-"
										printf '%s\t%s\t%s\t%s\n' "$source_fp" "$progress_file_name" "$progress_start_line" "$progress_reads"
									done < "$tmp_progress"
								} > "$sidecar_tmp"
								if ! mv "$sidecar_tmp" "$sidecar_output"; then
									echo "WARNING: failed to finalize slice sidecar for ${round_id}; round emission aborted."
									rm -f "$round_output" "$metadata_tmp" "$sidecar_tmp" "$sidecar_output"
								else
								progress_update_ok=1
								declare -a progress_targets=()
								declare -a progress_backups=()
								declare -a progress_existed=()
								while IFS=$'\t' read -r progress_file_name progress_start_line progress_reads; do
									[ -n "$progress_file_name" ] || continue
									progress_target="$(progress_reads_file_for "$progress_file_name")"
									progress_backup="${progress_target}.bak.$$"
									rm -f "$progress_backup"
									if [ -f "$progress_target" ]; then
										if ! cp "$progress_target" "$progress_backup"; then
											echo "WARNING: failed to back up progress file before round commit: $progress_target"
											progress_update_ok=0
											break
										fi
										progress_existed+=("1")
									else
										progress_existed+=("0")
									fi
									progress_targets+=("$progress_target")
									progress_backups+=("$progress_backup")
									if ! printf '%s\n' "$progress_reads" > "$progress_target"; then
										echo "WARNING: failed to update progress file during round commit: $progress_target"
										progress_update_ok=0
										break
									fi
								done < "$tmp_progress"

								if [ "$progress_update_ok" -eq 1 ] && ! mv "$metadata_tmp" "$metadata_output"; then
									echo "WARNING: failed to finalize round metadata for ${round_id}; round emission aborted."
									progress_update_ok=0
								fi

								if [ "$progress_update_ok" -ne 1 ]; then
									for progress_idx in "${!progress_targets[@]}"; do
										progress_target="${progress_targets[$progress_idx]}"
										progress_backup="${progress_backups[$progress_idx]}"
										progress_had_value="${progress_existed[$progress_idx]}"
										if [ "$progress_had_value" = "1" ] && [ -f "$progress_backup" ]; then
											cp "$progress_backup" "$progress_target" 2>/dev/null || true
										elif [ "$progress_had_value" = "0" ]; then
											rm -f "$progress_target"
										fi
									done
									rm -f "$round_output" "$metadata_tmp" "$metadata_output" "$sidecar_output" "$sidecar_tmp"
								else
									echo "Created a new pod5 in ${round_output}"
									append_local_round_commit_entry "$slice_fp" "$round_id" || true
									feeder_log_event "round_commit" "$round_id" "$slice_fp"
									publish_pending_round
								fi
								for progress_idx in "${!progress_backups[@]}"; do
									rm -f "${progress_backups[$progress_idx]}"
								done
								fi
							fi
						fi
							fi
							fi
						fi
						fi
					else
						echo "Buffered unread reads=${collected_reads}; waiting until ${num_reads} reads are available before emitting next round"
					fi

				rm -f "$tmp_read_info" "$tmp_round_ids" "$tmp_progress"
			fi
		fi
	else
		echo
		echo "WARNING!!!"
		echo "The folder $input_folder doesn't have any pod5s, if this message keeps appearing check the folder is the correct one."
		echo "WARNING!!!"
		echo
	fi
	echo sleeping $sleep_time seconds
	sleep $sleep_time
	echo "starting over"
done
