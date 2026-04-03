#!/bin/bash

# Initialize variables
num_reads="200000"
input_folder=""
run_id=""
sleep_time=300
skip_pod5=0
do_metadata=0
delete_full_pod5=0

# Function to display help message
usage() {
    echo "Usage: $0 --run_id <run_id>  --input_folder <input_folder> [--num_reads <reads_chunks>] [--sleep_time <sleep_time>] [--metadata <metadata>] [--general_fasta <general_fasta>] [--primers_fasta <primers_fasta>] [--skip_pod5] [--do_metadata] [--delete_full_pod5]"
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
		NR == 1 {
			$NF = trim_cr($NF)
			for (i = 1; i <= NF; i++) {
				if ($i == "Run") run_idx = i
			}
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
	awk -v run="$run_name" -v metadata_path="$metadata_path" -v row_info="$row_info_out" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function add_problem(msg) {
			if (problems != "") problems = problems "; "
			problems = problems msg
		}
		BEGIN {
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

			expected_demult = ">" well "_" plate
			if (demult_id != "" && demult_id != expected_demult) {
				add_problem("demult_id must equal " expected_demult)
			}

			if (problems != "") {
				print "ERROR: metadata file " metadata_path ", line " NR ": " problems > "/dev/stderr"
				exit 1
			}

				print $0
				printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n", NR, sample_id, pipeline_id, replicate, well, plate, demult_id, well "_" plate, replicate "_" well "_" plate >> row_info
				selected_count++
			}
		' "$metadata_path" > "$selected_out"
}

prompt_replicate_fallback_confirmation() {
	local fallback_file="$1"
	local count

	[ -s "$fallback_file" ] || return 0

	echo
	echo "WARNING: REPLICATE_WELL_PLATE fallback grammar was required for the following rows:"
	while IFS=$'\t' read -r line_no sample_name key1 key2; do
		printf '  metadata line %s: Sample_ID=%s Well_Plate=%s Replicate_Well_Plate=%s\n' \
			"$line_no" "$sample_name" "$key1" "$key2"
	done < "$fallback_file"
	echo

	if [ ! -t 0 ]; then
		echo "ERROR: REPLICATE_WELL_PLATE fallback requires interactive confirmation, but stdin is not a TTY." >&2
		return 1
	fi

	count=$(wc -l < "$fallback_file" | tr -d ' ')
	printf 'Continue metadata creation using REPLICATE_WELL_PLATE fallback for %s row(s)? [y/N] ' "$count"
	IFS= read -r reply
	case "$reply" in
		y|Y|yes|YES)
			return 0
			;;
		*)
			echo "ERROR: metadata creation aborted because REPLICATE_WELL_PLATE fallback was not confirmed." >&2
			return 1
			;;
	esac
}

# Resolve one metadata row into one or more demultiplexing FASTA records.
# Usage: resolve_metadata_row <sample_name> <line_no> <replicate_id> <replicate_number> <well> <plate> <demult_id> <general_fasta> <primers_fasta> <metadata_path> <records_out> <sidecar_out> <meta_out>
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
	local metadata_path="${10}"
	local records_out="${11}"
	local sidecar_out="${12}"
	local meta_out="${13}"

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
		-v metadata_path="$metadata_path" \
		-v records_out="$records_out" \
		-v sidecar_out="$sidecar_out" \
		-v meta_out="$meta_out" '
		function trim_cr(s) {
			sub(/\r$/, "", s)
			return s
		}
		function valid_token(token) {
			return (token != "" && token !~ /[[:space:]_]/)
		}
		function fallback_suffix(key, hdr, idx, suffix) {
			suffix = hdr
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
		function parse_primer_header(name, parts, n) {
			n = split(name, parts, "_")
			if (n != 2) return ""
			if (!valid_token(parts[1]) || !valid_token(parts[2])) return ""
			return parts[1] "\t" parts[2]
		}
		function classify_header(name, next_char) {
			if (name == key1) return "exact1"
			if (name == key2) return "exact2"
			return ""
		}
		function store_candidate(mode, header, seq, record_index, parts) {
			if (mode == "exact1") {
				key1_count++
				key1_header[key1_count] = header
				key1_seq[key1_count] = seq
				key1_record_index[key1_count] = record_index
				split(header, parts, "_")
				key1_plate[key1_count] = parts[2]
			} else if (mode == "exact2") {
				key2_count++
				key2_header[key2_count] = header
				key2_seq[key2_count] = seq
				key2_record_index[key2_count] = record_index
				split(header, parts, "_")
				key2_plate[key2_count] = parts[3]
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
		function resolve_candidates(count, headers, seqs, plates, record_indexes, active_key, active_label,    i, p, parsed, parts, left, right, marker, candidate_plate, hit_count, primer_info, primer_parts, output_suffix, suffix_mode, unit_id_collapse, unit_id_track) {
			for (i = 1; i <= count; i++) {
				candidate_plate = plates[i]
				if (candidate_plate != plate) {
					fail_row(active_label " candidate " headers[i] " has plate " candidate_plate " but metadata Plate is " plate)
				}
				parsed = parse_linked_parts(seqs[i], parts)
				split(parsed, parts, /\t/)
				left = parts[1]
				right = parts[2]
				marker = ""
				hit_count = 0
				for (p = 1; p <= primer_count; p++) {
					if (left ~ (primer_left[p] "$") && right ~ ("^" primer_right[p])) {
						primer_info = parse_primer_header(primer_name[p], primer_parts)
						if (primer_info == "") {
							fail_row("participating primer header " primer_name[p] " in " primers_fasta " is malformed; expected MARKER_PLATE")
						}
						split(primer_info, primer_parts, /\t/)
						if (primer_parts[2] != plate || primer_parts[2] != candidate_plate) {
							continue
						}
						hit_count++
						if (marker == "") {
							marker = primer_parts[1]
						} else if (marker != primer_parts[1]) {
							fail_row(active_label " candidate " headers[i] " matched multiple primer markers (" marker " and " primer_parts[1] ") in " primers_fasta)
						}
					}
				}
				if (hit_count == 0) {
					fail_row(active_label " candidate " headers[i] " has no matching primer entry in " primers_fasta)
				}
				resolved_marker[i] = marker
			}
				print active_label > meta_out
				for (i = 1; i <= count; i++) {
					output_suffix = resolved_marker[i]
					suffix_mode = "marker"
					if (output_suffix == "") {
						fail_row(active_label " candidate " headers[i] " did not resolve to a marker")
					}
					if (seen_marker[output_suffix]) {
						print "WARN: Barcode key " active_key " matched multiple sequences for marker " output_suffix " in " fasta "; using fallback suffix for duplicate header " headers[i] > "/dev/stderr"
						output_suffix = fallback_suffix(active_key, headers[i], i)
						while (seen_marker[output_suffix]) {
							output_suffix = fallback_suffix(active_key, headers[i], i "_" seen_marker[output_suffix])
						}
						suffix_mode = "fallback"
					}
					seen_marker[output_suffix]++
					unit_id_collapse = sample_name "_" output_suffix
					unit_id_track = replicate_id "_" output_suffix
					print ">" unit_id_collapse > records_out
					print seqs[i] > records_out
					printf "%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n",
						sample_name,
						replicate_id,
						replicate,
						resolved_marker[i],
						headers[i],
						record_indexes[i],
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
			primer_count = load_primers(primers_fasta)
			if (primer_count == 0) {
				print "ERROR: primers FASTA is empty or unreadable: " primers_fasta > "/dev/stderr"
				exit 1
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
					process_record(current_header, current_seq)

					if (key1_count > 0) {
						active_key = key1
						active_label = "WELL_PLATE"
						resolve_candidates(key1_count, key1_header, key1_seq, key1_plate, key1_record_index, active_key, active_label)
						exit 0
					}

					if (key2_count > 0) {
						active_key = key2
						active_label = "REPLICATE_WELL_PLATE"
						resolve_candidates(key2_count, key2_header, key2_seq, key2_plate, key2_record_index, active_key, active_label)
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
			staged_output_fasta="${metadata_stage_dir}/demult.fasta"
			staged_replicate_identity="${metadata_stage_dir}/replicate_identity.tsv"
			staged_samples="${metadata_stage_dir}/samples.txt"
			warn_seen_file="${metadata_stage_dir}/.demult_warn_seen"
			fallback_rows_file="${metadata_stage_dir}/.fallback_rows.tsv"
			: > "$row_info_file"
			: > "$warn_seen_file"
			: > "$fallback_rows_file"
			: > "$staged_output_fasta"
			printf '%s\n' "sample_id	replicate_id	replicate_number	marker_id	matched_general_fasta_header	matched_general_fasta_record_index	suffix_resolution_mode	unit_suffix_current	unit_id_collapse	unit_id_track	demult_id_metadata	lookup_key_primary	lookup_key_fallback	lookup_grammar_used	metadata_line_no" > "$staged_replicate_identity"

		if ! select_and_validate_metadata_rows "$metadata" "$run_id" "$staged_metadata" "$row_info_file"; then
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
		# Lookup resolves in order: WELL_PLATE, then REPLICATE_WELL_PLATE only if the first grammar has no exact candidates.
		# Each active candidate must resolve cleanly through primer-based marker assignment.
		expected_records=0
			while IFS=$'\t' read -r line_no sample_name replicate_id replicate well plate demult_id key1 key2; do
				tmp_records="${metadata_stage_dir}/.demult_match.tmp"
				tmp_sidecar="${metadata_stage_dir}/.demult_match.sidecar"
				tmp_warn="${metadata_stage_dir}/.demult_match.warn"
				tmp_meta="${metadata_stage_dir}/.demult_match.meta"
				: > "$tmp_meta"
				: > "$tmp_sidecar"
				if ! resolve_metadata_row "$sample_name" "$line_no" "$replicate_id" "$replicate" "$well" "$plate" "$demult_id" "$general_fasta" "$primers_fasta" "$metadata" "$tmp_records" "$tmp_sidecar" "$tmp_meta" 2> "$tmp_warn"; then
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
				if [ "$grammar_used" = "REPLICATE_WELL_PLATE" ]; then
					printf '%s\t%s\t%s\t%s\n' "$line_no" "$sample_name" "$key1" "$key2" >> "$fallback_rows_file"
				fi
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

		#List of samples in the run; preserve current implemented output semantics exactly.
		cat "$staged_metadata" | cut -f2  | sort | uniq > "$staged_samples"


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

			if ! prompt_replicate_fallback_confirmation "$fallback_rows_file"; then
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
			if ! mv "$staged_samples" "${SAMPLE_INFO_DIR}/samples.txt"; then
				echo "ERROR: Failed to write samples.txt into $SAMPLE_INFO_DIR" >&2
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
