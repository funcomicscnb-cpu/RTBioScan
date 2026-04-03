restore_sup_fastq() {
	local lock_path="$1"
	local state_dir="$2"
	local barcode="$3"
	local local_output_path="$4"
	local base_dir="$5"
	local pruned_barrier="${state_dir}/${barcode}_pruned_barrier.list"
	local state_sup_gz="${state_dir}/blastreport_sup_annotated_pre.fastq.gz"
	local state_sup_fastq="${state_dir}/blastreport_sup_annotated_pre.fastq"
	local found_state=0

	if acquire_lock "$lock_path"; then
		if [ -f "$state_sup_gz" ]; then
			gzip -dc "$state_sup_gz" > "$local_output_path" || true
			found_state=1
		elif [ -f "$state_sup_fastq" ]; then
			cp "$state_sup_fastq" "$local_output_path" || true
			found_state=1
		fi
		if [ "$found_state" -eq 1 ] && [ -s "$local_output_path" ] && [ -s "$pruned_barrier" ]; then
			if ! perl "$base_dir/bin/fastq_filter_ids.pl" \
				"$local_output_path" "$pruned_barrier" \
				"$local_output_path.filtered" \
				"${state_dir}/${barcode}_sup_barrier_filter_stats.tsv"; then
				release_lock "$lock_path"
				exit 1
			fi
			mv "$local_output_path.filtered" "$local_output_path"
			if [ -f "$state_sup_gz" ]; then
				gzip -c "$local_output_path" > "${state_sup_gz}.tmp" && mv "${state_sup_gz}.tmp" "$state_sup_gz"
				cp "$local_output_path" "$state_sup_fastq" 2>/dev/null || true
			else
				cp "$local_output_path" "$state_sup_fastq" 2>/dev/null || true
			fi
		fi
		release_lock "$lock_path"
	else
		exit 1
	fi

	if [ "$found_state" -eq 1 ]; then
		return 0
	fi

	return 1
}
