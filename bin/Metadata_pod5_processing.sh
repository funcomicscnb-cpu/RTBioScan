#!/bin/bash

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
	echo "	metadata will be used to retrieve the demultiplexing fastas for the pipeline to work, if not defined will search in: $HOME/Tumbira_Final/Metadata/Pipeline_Information.tsv"
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
			token_count = split("sequence_mismatch|sample_id_mismatch|track_id_mismatch|replicate_number_mismatch|marker_id_mismatch|unit_suffix_mismatch|unit_id_collapse_mismatch|demult_id_metadata_mismatch", token_order, /\|/)
			out = ""
			for (i = 1; i <= token_count; i++) {
				token = token_order[i]
				if (detail_tokens[key, token]) out = out (out == "" ? "" : "|") token
			}
			return out
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

			if (mismatch) {
				status[unit_key] = "conflicting_duplicate_present"
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
		exit 1
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
		exit 1
	fi

	track_demult_lines=$(wc -l < "$track_demult_path")
	if [ "$identity_rows" -gt 0 ] && { [ "$track_demult_lines" -le 0 ] || [ $(( track_demult_lines % 2 )) -ne 0 ]; }; then
		echo "ERROR: track_demult.fasta must contain a positive even number of lines when replicate_identity.tsv has data." >&2
		exit 1
	fi
	track_demult_record_count=$(grep -c '^>' "$track_demult_path" || true)
	track_active_count=$(wc -l < "$track_active_units_path")
	track_identity_rows=$(( $(wc -l < "$track_identity_path") - 1 ))

	if [ "$track_demult_record_count" -ne "$track_active_count" ]; then
		echo "ERROR: track_demult.fasta records ($track_demult_record_count) do not match track_active_units.txt lines ($track_active_count)." >&2
		exit 1
	fi
	if [ "$track_demult_record_count" -ne "$track_identity_rows" ]; then
		echo "ERROR: track_demult.fasta records ($track_demult_record_count) do not match track_identity.tsv rows ($track_identity_rows)." >&2
		exit 1
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
		exit 1
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
		exit 1
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
		exit 1
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
		exit 1
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
		exit 1
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
		exit 1
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
		exit 1
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
	local track_demult_records="${metadata_stage_dir}/track_demult_records.tsv"
	local track_identity_indexed="${metadata_stage_dir}/track_identity_indexed.tsv"
	local track_identity_first="${metadata_stage_dir}/track_identity_first.tsv"
	local track_roster_identity_lines="${metadata_stage_dir}/track_roster_identity_lines.tsv"

	emit_track_demult_records_index "$staged_output_fasta" "$track_demult_records"
	emit_track_identity_index "$staged_replicate_identity" "$track_identity_indexed"
	emit_track_roster_identity_lines "$track_identity_indexed" "$track_roster_identity_lines"
	emit_track_identity_views "$track_demult_records" "$track_identity_indexed" "$track_identity_first" "$staged_track_identity"
	: > "$staged_track_demult"
	: > "$staged_track_active_units"
	emit_track_demult_view "$track_demult_records" "$track_identity_first" "$staged_track_demult"
	emit_track_active_units_view "$track_identity_first" "$staged_track_active_units"
	emit_track_roster_view "$staged_replicate_roster" "$track_roster_identity_lines" "$staged_track_roster"
	validate_track_artifacts "$staged_replicate_identity" "$track_demult_records" "$track_identity_first" "$staged_track_demult" "$staged_track_active_units" "$staged_track_identity" "$staged_track_roster"
	emit_track_view_warnings "track_identity" "unit_id_track" "$staged_track_identity"
	emit_track_view_warnings "track_roster" "track_id" "$staged_track_roster"
}

# Resolve one metadata row into one or more demultiplexing FASTA records.
# Usage: resolve_metadata_row <sample_name> <line_no> <replicate_id> <replicate_number> <well> <plate> <demult_id> <general_fasta> <primers_fasta> <targets> <metadata_path> <records_out> <sidecar_out> <meta_out>
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
		-v meta_out="$meta_out" '
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
		#######This needs to be changed to the actual file
		metadata="$HOME/Tumbira_Final/Metadata/Pipeline_Information.tsv"		
	fi
	if [ ! -e "$metadata" ];then
		echo
		echo "Error: metadata file $metadata doesn't exist."
		echo
		usage
	fi
	if [ -z "$general_fasta" ];then
		#####Needs to be updated too
		general_fasta="$HOME/Tumbira_Final/Metadata/demult_general_with_Tucan.fasta"
	fi
	if [ ! -e "$general_fasta" ];then
		echo
		echo "Error: general fasta file $general_fasta doesn't exist."
		usage
	fi
	if [ -z "$primers_fasta" ];then
		#####Needs to be updated too
		primers_fasta="$HOME/Tumbira_Final/Metadata/demult_primers.fasta"
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
				warn_seen_file="${metadata_stage_dir}/.demult_warn_seen"
				: > "$row_info_file"
				: > "$replicate_roster_rows_file"
				: > "$samples_compat_rows_file"
				: > "$warn_seen_file"
				: > "$staged_output_fasta"
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
				: > "$tmp_meta"
				: > "$tmp_sidecar"
				if ! resolve_metadata_row "$sample_name" "$line_no" "$replicate_id" "$replicate" "$well" "$plate" "$demult_id" "$general_fasta" "$primers_fasta" "$targets" "$metadata" "$tmp_records" "$tmp_sidecar" "$tmp_meta" 2> "$tmp_warn"; then
					if [ -s "$tmp_warn" ]; then
						cat "$tmp_warn" >&2
					fi
					rm -f "$tmp_records" "$tmp_sidecar" "$tmp_warn" "$tmp_meta"
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
					rm -f "$tmp_records" "$tmp_sidecar"
					exit 1
				fi
				if [ "$sidecar_count" -ne "$record_count" ]; then
					echo "ERROR: replicate_identity.tsv rows ($sidecar_count) do not match demult.fasta records ($record_count) for Sample_ID '$sample_name'" >&2
					rm -f "$tmp_records" "$tmp_sidecar"
					exit 1
				fi
				cat "$tmp_records" >> "$staged_output_fasta"
				cat "$tmp_sidecar" >> "$staged_replicate_identity"
				rm -f "$tmp_records" "$tmp_sidecar"
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
			if ! derive_track_artifacts "$staged_output_fasta" "$staged_replicate_identity" "$staged_replicate_roster" "$staged_track_demult" "$staged_track_roster" "$staged_track_active_units" "$staged_track_identity"; then
				exit 1
			fi

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
		if ! cp "$primers_fasta" "${SAMPLE_INFO_DIR}/primers.fasta"; then
			echo "ERROR: Failed to copy primers fasta into $SAMPLE_INFO_DIR" >&2
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
		if ! mv "$staged_track_demult" "${SAMPLE_INFO_DIR}/track_demult.fasta"; then
			echo "ERROR: Failed to write track_demult.fasta into $SAMPLE_INFO_DIR" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
		if ! mv "$staged_track_roster" "${SAMPLE_INFO_DIR}/track_roster.tsv"; then
			echo "ERROR: Failed to write track_roster.tsv into $SAMPLE_INFO_DIR" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
		if ! mv "$staged_track_active_units" "${SAMPLE_INFO_DIR}/track_active_units.txt"; then
			echo "ERROR: Failed to write track_active_units.txt into $SAMPLE_INFO_DIR" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
		fi
		if ! mv "$staged_track_identity" "${SAMPLE_INFO_DIR}/track_identity.tsv"; then
			echo "ERROR: Failed to write track_identity.tsv into $SAMPLE_INFO_DIR" >&2
			rollback_metadata_commit "$SAMPLE_INFO_DIR" "$run_id" "$general_copy_name"
			exit 1
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



#round=0
#Add loop to find the actual round
#counter=0
reads_time="${metadata}/${run_id}_reads_time_rpt.txt"
if [ ! -s "$reads_time" ]; then
	echo -e "run_id\ttime\tdata\treads\tfile" > $reads_time
fi


for ((;;));do
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
	if [ -e "${POD5_BASE}/tmp_file_pod5" ];then rm "${POD5_BASE}/tmp_file_pod5";fi
	if [ -e "${POD5_BASE}/la" ];then rm "${POD5_BASE}/la";fi

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
			fi

			if [ ! -f "$per_file_view" ]; then
				pod5 view "$output_full_pod5/$file" --no-header --output "${POD5_BASE}/tmp_file_pod5"
				if [ ! $? -eq 0 ]; then
					rm -f "${POD5_BASE}/tmp_file_pod5"
					echo
					echo "WARNING: pod5 view failed for '${file}' — file is truncated or corrupted."
					mv -f "$output_full_pod5/$file" "$output_skipped_pod5/" 2>/dev/null || true
					touch "${metadata}/skipped_${file}.flag"
					echo "WARNING: Moved '${file}' to ${output_skipped_pod5}/ and flagged to prevent re-import."
					echo "WARNING: Continuing round with remaining valid pod5 files."
					echo
					continue
				fi
				cp "${POD5_BASE}/tmp_file_pod5" "$per_file_view"
				cat "${POD5_BASE}/tmp_file_pod5" >> "${metadata}/full_pod5_view.txt"

				time=$(stat -f %SB -t %s "$output_full_pod5/$file")
				last=$(tail -n1 $reads_time| awk '{print $4}')
				if [ "$last" == "file" ];then last=0;fi
				pod5_reads=$(wc -l < "${POD5_BASE}/tmp_file_pod5")
				cumulative=$(( last + pod5_reads ))
				echo -e "${run_id}\t${time}\tsequencing\t${cumulative}\t${file}" >> $reads_time
				rm -f "${POD5_BASE}/tmp_file_pod5"
			fi
		done <<< "$full_files"

		if [ $breaking -eq 1 ]; then
			breaking=0
		else
			archived_files=$(list_visible_pod5_files "$output_full_pod5")
			if [ -n "$archived_files" ]; then
				tmp_read_info="${POD5_BASE}/tmp_read_info_rpt.txt"
				tmp_round_ids="${POD5_BASE}/la"
				tmp_progress="${POD5_BASE}/tmp_round_progress.tsv"
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
					printf '%s\t%s\n' "$file" "$end_line" >> "$tmp_progress"
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
						out_id=$(ls ${metadata}/${run_id}_*_read_info_rpt.txt 2>/dev/null | wc -l | tr -d ' ')
						round_output="${output_folder}/${run_id}_${out_id}.pod5"
						if ! pod5 filter "${round_inputs[@]}" --ids "$tmp_round_ids" --output "$round_output"; then
							echo "pod5 filter didn't work properly"
							rm -f "$round_output"
						else
							printf '%s\n' "$round_header" > "${metadata}/${run_id}_${out_id}_read_info_rpt.txt"
							cat "$tmp_read_info" >> "${metadata}/${run_id}_${out_id}_read_info_rpt.txt"
							while IFS=$'\t' read -r progress_file_name progress_reads; do
								[ -n "$progress_file_name" ] || continue
								printf '%s\n' "$progress_reads" > "$(progress_reads_file_for "$progress_file_name")"
							done < "$tmp_progress"
							echo "Created a new pod5 in ${round_output}"
							ready_count=$(count_pod5_files "${output_rt}")
							if [ "$ready_count" -eq 0 ]; then
								mv "$round_output" "${output_rt}/"
							fi
							publish_pending_round
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
