consensus_cache_backfill_missing_count() {
	local state_root="$1"
	local local_root="$2"
	local state_dir=""
	local sample_name=""
	local missing=0
	[ -d "$state_root" ] || {
		printf '0\n'
		return 0
	}
	for state_dir in "$state_root"/*; do
		[ -d "$state_dir" ] || continue
		sample_name=$(basename "$state_dir")
		if [ ! -d "$local_root/$sample_name" ]; then
			missing=$(( missing + 1 ))
		fi
	done
	printf '%s\n' "$missing"
}

consensus_cache_backfill_missing_file_count() {
	local state_root="$1"
	local local_root="$2"
	local state_dir=""
	local sample_name=""
	local files=0
	local sample_files=0
	[ -d "$state_root" ] || {
		printf '0\n'
		return 0
	}
	for state_dir in "$state_root"/*; do
		[ -d "$state_dir" ] || continue
		sample_name=$(basename "$state_dir")
		if [ ! -d "$local_root/$sample_name" ]; then
			sample_files=$(find "$state_dir" -type f | wc -l | tr -d ' ')
			[ -z "$sample_files" ] && sample_files=0
			files=$(( files + sample_files ))
		fi
	done
	printf '%s\n' "$files"
}

restore_missing_consensus_cache_dirs() {
	local state_root="$1"
	local local_root="$2"
	local sync_script="$3"
	local state_dir=""
	local sample_name=""
	[ -d "$state_root" ] || return 0
	if [ ! -f "$sync_script" ]; then
		echo "ERROR: consensus cache sync script not found at $sync_script" 1>&2
		return 1
	fi
	mkdir -p "$local_root"
	for state_dir in "$state_root"/*; do
		[ -d "$state_dir" ] || continue
		sample_name=$(basename "$state_dir")
		if [ ! -d "$local_root/$sample_name" ]; then
			if ! bash "$sync_script" "$state_dir" "$local_root/$sample_name"; then
				echo "ERROR: failed to restore untouched consensus cache dir sample=$sample_name before persist" 1>&2
				return 1
			fi
		fi
	done
}
