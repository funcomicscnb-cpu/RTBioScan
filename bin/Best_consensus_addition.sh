#!/bin/bash

# File containing the FASTA entries
fasta_file="$1"

#Output folder
out_dir=$(dirname $fasta_file)

# Variables to store the best entry and total reads
best_entry=""
best_sequence=""
best_otu=""
best_reads=0
max_reads=0
total_reads=0
capture_best=0
target_header="${2:-}"
target_header="${target_header#>}"
target_header="${target_header%$'\r'}"
target_found=0
target_entry=""
target_sequence=""
target_otu=""
target_reads=0
capture_target=0

Consensus=$(basename $fasta_file | awk -F "_" '{print $NF}')
if [ ! -e $out_dir/OriginalReads ]; then mkdir $out_dir/OriginalReads;fi
originalreads="$out_dir/OriginalReads/${Consensus}_reads.list"
supreads="$out_dir/OriginalReads/${Consensus}_reads_sup.fasta"

# Read the FASTA file and process each entry
while read -r line; do
    if [[ $line =~ ^\> ]]; then
        header_raw="${line#>}"
        header_raw="${header_raw%$'\r'}"
        # Extract the number of reads from the header line
        reads=$(echo "$line" | sed -E 's/.*reads-([0-9]+).*/\1/')
        if ! echo "$reads" | awk 'BEGIN{ok=1} /^[0-9]+$/{next} {ok=0} END{exit ok?0:1}'; then
            reads=0
        fi
	otu=""
	if [[ "$line" =~ \|OTU=([^|]+) ]]; then
		otu="${BASH_REMATCH[1]}"
	else
		otu=$(echo "$line" | tr '|' '\n' | awk '/^OTUB_/{print; exit}')
		if [ -z "$otu" ]; then
			otu=$(echo "$line" | cut -d"|" -f2)
		fi
	fi
        # Add to total reads
        total_reads=$((total_reads + reads))
	# If a target header is provided, capture that specific entry
	if [ -n "$target_header" ] && [ "$header_raw" = "$target_header" ]; then
		target_found=1
		target_otu="$otu"
		target_reads="$reads"
		if [[ "$line" =~ ^\>([^|]+)\|([^|]+)\|(.*)$ ]]; then
			target_entry=">${BASH_REMATCH[1]}|${Consensus}|${BASH_REMATCH[3]}"
		else
			target_entry=$(echo "$line" | sed "s/$otu/$Consensus/")
		fi
		target_sequence=""
		capture_target=1
	else
		capture_target=0
	fi
        # Check if this entry has more reads than the current max
        if (( reads > max_reads )) || [[ -z "$best_entry" ]]; then
            max_reads=$reads
            if [[ "$line" =~ ^\>([^|]+)\|([^|]+)\|(.*)$ ]]; then
                best_entry=">${BASH_REMATCH[1]}|${Consensus}|${BASH_REMATCH[3]}"
            else
                best_entry=$(echo "$line" | sed "s/$otu/$Consensus/")
            fi
            best_sequence=""
            capture_best=1
	    best_otu="$otu"
	    best_reads="$reads"
        else
            capture_best=0
        fi
    else
        # Append the sequence lines to the best entry
        if [[ $capture_best -eq 1 ]]; then
            best_sequence+="$line"
        fi
	if [[ $capture_target -eq 1 ]]; then
		target_sequence+="$line"
	fi
    fi
done < $fasta_file

selected_otu="$best_otu"
selected_reads="$best_reads"
if [ -n "$target_header" ] && [ "$target_found" -eq 1 ]; then
	best_entry="$target_entry"
	best_sequence="$target_sequence"
	selected_otu="$target_otu"
	selected_reads="$target_reads"
fi

if [ -n "$selected_otu" ]; then
	reads_mode="${CONSENSUS_READS_MODE:-representative}"
	if [ "$reads_mode" = "cluster_total" ]; then
		: > "$originalreads"
		tmp_reads="${originalreads}.tmp"
		: > "$tmp_reads"
		tmp_sup="${supreads}.tmp"
		: > "$tmp_sup"
		while read -r line; do
			if [[ $line =~ ^\> ]]; then
				hdr="$line"
				otu=""
				if [[ "$hdr" =~ \|OTU=([^|]+) ]]; then
					otu="${BASH_REMATCH[1]}"
				else
					otu=$(echo "$hdr" | tr '|' '\n' | awk '/^OTUB_/{print; exit}')
					if [ -z "$otu" ]; then
						otu=$(echo "$hdr" | cut -d"|" -f2)
					fi
				fi
				if [ -n "$otu" ] && [ -f "$out_dir/${otu}_all_reads.list" ]; then
					cat "$out_dir/${otu}_all_reads.list" >> "$tmp_reads"
				fi
				if [ -n "$otu" ] && [ -f "$out_dir/${otu}_reads_sup.fasta" ]; then
					cat "$out_dir/${otu}_reads_sup.fasta" >> "$tmp_sup"
				fi
			fi
		done < "$fasta_file"
		if [ -s "$tmp_reads" ]; then
			LC_ALL=C sort -u "$tmp_reads" > "$originalreads"
		fi
		if [ -s "$tmp_sup" ]; then
			awk 'BEGIN{RS=">"; ORS=""} NR>1 {h=$1; sub(/\n.*/, "", h); if(!seen[h]++){print ">"$0}}' "$tmp_sup" > "$supreads"
		fi
		rm -f "$tmp_reads" "$tmp_sup"
	else
		: > "$originalreads"
		if [ -f "$out_dir/${selected_otu}_all_reads.list" ]; then
			cat "$out_dir/${selected_otu}_all_reads.list" >> "$originalreads"
		fi
		if [ -f "$out_dir/${selected_otu}_reads_sup.fasta" ]; then
			cat "$out_dir/${selected_otu}_reads_sup.fasta" > "$supreads"
		fi
	fi
fi


# Print the result
reads_mode="${CONSENSUS_READS_MODE:-representative}"
reads_value="$selected_reads"
if [ "$reads_mode" = "cluster_total" ]; then
	reads_value="$total_reads"
fi
echo -e "$best_entry\n$best_sequence" |sed -E "s/(reads-)[0-9]+/\1$reads_value/"
