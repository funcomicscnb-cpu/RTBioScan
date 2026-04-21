#!/bin/bash

validate_phase1_workload_file() {
	local workload_file="${1:-}"
	[ -n "$workload_file" ] && [ -f "$workload_file" ] || {
		echo "ERROR: phase1 workload file missing: $workload_file" >&2
		return 1
	}
	awk '
		BEGIN { FS="\t" }
		function fail(msg) {
			printf "ERROR: malformed phase1 workload row line=%d %s\n", NR, msg > "/dev/stderr"
			exit 1
		}
		function is_uint(v) {
			return v ~ /^[0-9]+$/
		}
		NF == 0 { next }
		NF != 11 { fail("expected=11 got=" NF) }
		$1 == "" { fail("empty_otu_key") }
		$10 != "pool_select" && $10 != "fallback" { fail("invalid_phase1_mode=" $10) }
		$11 == "" { fail("empty_sel_prefix") }
		!is_uint($2) { fail("invalid_prev_stable_count=" $2) }
		!is_uint($3) { fail("invalid_prev_lock_pass=" $3) }
		!is_uint($4) { fail("invalid_locked_this_otu=" $4) }
		!is_uint($5) { fail("invalid_is_frozen=" $5) }
		!is_uint($7) { fail("invalid_eligible_count=" $7) }
		!is_uint($8) { fail("invalid_pool_ids_count=" $8) }
	' "$workload_file"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
	validate_phase1_workload_file "${1:-}"
	exit $?
fi
