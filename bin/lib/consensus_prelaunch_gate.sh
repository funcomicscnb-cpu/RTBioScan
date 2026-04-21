#!/usr/bin/env bash

consensus_prelaunch_has_reads() {
	local reads_path="${1:-}"
	[ -n "$reads_path" ] && [ -s "$reads_path" ]
}

consensus_prelaunch_has_cache() {
	local cache_root="${1:-}"
	if [ -d "$cache_root" ]; then
		local cache_state_files=( "$cache_root"/*/*.consensus.fasta )
		[ -f "${cache_state_files[0]:-}" ]
	else
		return 1
	fi
}

consensus_prelaunch_should_run() {
	local reads_path="${1:-}"
	local cache_root="${2:-}"
	if consensus_prelaunch_has_reads "$reads_path" || consensus_prelaunch_has_cache "$cache_root"; then
		printf '1\n'
	else
		printf '0\n'
	fi
}
