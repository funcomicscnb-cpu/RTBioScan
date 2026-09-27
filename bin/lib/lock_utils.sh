lock_dir_path() {
	printf "%s.lockdir" "$1"
}

__lock_utils_source_path=${BASH_SOURCE[0]}
if [[ "$__lock_utils_source_path" = */* ]]; then
	__lock_utils_source_dir=${__lock_utils_source_path%/*}
else
	__lock_utils_source_dir=.
fi
__lock_utils_exit_dispatch_installed=0
__lock_utils_exit_hooks=()

__lock_utils_ensure_arrays() {
	declare -p __lock_utils_exit_hooks >/dev/null 2>&1 || __lock_utils_exit_hooks=()
	declare -p acquired_locks >/dev/null 2>&1 || acquired_locks=()
	declare -p acquired_lock_fds >/dev/null 2>&1 || acquired_lock_fds=()
	declare -p __lock_utils_barrier_roots >/dev/null 2>&1 || __lock_utils_barrier_roots=()
	declare -p __lock_utils_barrier_fds >/dev/null 2>&1 || __lock_utils_barrier_fds=()
	declare -p __lock_utils_barrier_ids >/dev/null 2>&1 || __lock_utils_barrier_ids=()
}

__lock_utils_pick_free_fd() {
	local candidate
	__lock_utils_free_fd=
	for candidate in 9 8 7 6 5 4 3; do
		if { : >&$candidate; } 2>/dev/null || { : <&$candidate; } 2>/dev/null; then
			continue
		fi
		__lock_utils_free_fd=$candidate
		return 0
	done
	return 1
}

__lock_utils_seed_status() {
	return "${1:-0}"
}

__lock_utils_run_exit_hooks() {
	local status="${1:-0}"
	local hook
	local hook_status=0
	local hook_index=0

	__lock_utils_ensure_arrays
	trap - EXIT
	set +e
	if [ "${#__lock_utils_exit_hooks[@]-0}" -gt 0 ]; then
		for hook in "${__lock_utils_exit_hooks[@]}"; do
			hook_index=$((hook_index + 1))
			(
				# Preserve native-looking $? for each preserved EXIT hook while keeping
				# hooks isolated from one another: shell-state mutations in one hook do
				# not persist to later hooks because each runs in its own subshell.
				__lock_utils_seed_status "$status"
				eval "$hook"
			)
			hook_status=$?
			if [ "$hook_status" -ne 0 ]; then
				printf 'WARN: EXIT hook %s failed with status %s\n' "$hook_index" "$hook_status" 1>&2
			fi
		done
	fi
	exit "$status"
}

__lock_utils_install_exit_dispatch() {
	local existing quoted_existing

	__lock_utils_ensure_arrays
	[ "${__lock_utils_exit_dispatch_installed}" -eq 0 ] || return 0
	existing=$(trap -p EXIT)
	if [ -n "$existing" ]; then
		quoted_existing=${existing#trap -- }
		quoted_existing=${quoted_existing% EXIT}
		eval "__lock_utils_existing_exit_cmd=${quoted_existing}"
		if [ -n "${__lock_utils_existing_exit_cmd:-}" ]; then
			__lock_utils_exit_hooks+=( "$__lock_utils_existing_exit_cmd" )
		fi
	fi
	trap '__lock_utils_run_exit_hooks "$?"' EXIT
	__lock_utils_exit_dispatch_installed=1
}

append_trap() {
	local sig="$1"
	shift
	local cmd="$*"
	local hook

	__lock_utils_ensure_arrays
	if [ "$sig" != "EXIT" ]; then
		echo "append_trap currently only supports EXIT" 1>&2
		return 1
	fi

	__lock_utils_install_exit_dispatch
	if [ "${#__lock_utils_exit_hooks[@]-0}" -gt 0 ]; then
		for hook in "${__lock_utils_exit_hooks[@]}"; do
			if [ "$hook" = "$cmd" ]; then
				return 0
			fi
		done
	fi
	__lock_utils_exit_hooks+=( "$cmd" )
}

release_lock() {
	local target="$1"
	local i fd root held held_root found=0 remaining=0
	local new_locks=() new_fds=() new_roots=() new_barriers=() new_ids=()
	__lock_utils_ensure_arrays
	for ((i=0; i<${#acquired_locks[@]}; i++)); do
		if [ "${acquired_locks[$i]}" = "$target" ]; then
			fd=${acquired_lock_fds[$i]}
			eval "exec $fd>&-"  # close only; the drain reaper owns fence removal
			found=1
		else
			new_locks+=( "${acquired_locks[$i]}" )
			new_fds+=( "${acquired_lock_fds[$i]}" )
		fi
	done
	if [ "${#new_locks[@]}" -eq 0 ]; then
		acquired_locks=()
		acquired_lock_fds=()
	else
		acquired_locks=( "${new_locks[@]}" )
		acquired_lock_fds=( "${new_fds[@]}" )
	fi
	[ "$found" -eq 1 ] || return 0
	root=${target%/*}
	[ "$root" != "$target" ] || root=.
	for held in ${acquired_locks[@]+"${acquired_locks[@]}"}; do
		held_root=${held%/*}
		[ "$held_root" != "$held" ] || held_root=.
		if [ "$held_root" = "$root" ]; then remaining=1; break; fi
	done
	if [ "$remaining" -eq 0 ]; then
		for ((i=0; i<${#__lock_utils_barrier_roots[@]}; i++)); do
			if [ "${__lock_utils_barrier_roots[$i]}" = "$root" ]; then
				fd=${__lock_utils_barrier_fds[$i]}
				eval "exec $fd<&-"  # descendants and the reaper retain their copies
			else
				new_roots+=( "${__lock_utils_barrier_roots[$i]}" )
				new_barriers+=( "${__lock_utils_barrier_fds[$i]}" )
				new_ids+=( "${__lock_utils_barrier_ids[$i]}" )
			fi
		done
		if [ "${#new_roots[@]}" -eq 0 ]; then
			__lock_utils_barrier_roots=()
			__lock_utils_barrier_fds=()
			__lock_utils_barrier_ids=()
		else
			__lock_utils_barrier_roots=( "${new_roots[@]}" )
			__lock_utils_barrier_fds=( "${new_barriers[@]}" )
			__lock_utils_barrier_ids=( "${new_ids[@]}" )
		fi
	fi
	return 0
}

cleanup_locks() {
	local l
	__lock_utils_ensure_arrays
	[ "${#acquired_locks[@]-0}" -eq 0 ] && return
	for l in "${acquired_locks[@]}"; do
		release_lock "$l"
	done
}

init_lock_helpers() {
	__lock_utils_ensure_arrays
	acquired_locks=()
	acquired_lock_fds=()
	__lock_utils_barrier_roots=()
	__lock_utils_barrier_fds=()
	__lock_utils_barrier_ids=()
	# Chain lock cleanup onto EXIT instead of replacing any existing handler.
	# Callers that register additional EXIT cleanup should prefer append_trap EXIT ...
	# so lock cleanup remains composed rather than overwritten. Preserved hooks run
	# in isolated subshells: they see the original $? but cannot share shell-state
	# mutations with later hooks.
	append_trap EXIT cleanup_locks
}

acquire_lock() {
	local target="$1"
	local fd barrier_fd= barrier_new=0 barrier_file lock_file old_umask helper root root_id= i
	__lock_utils_ensure_arrays
	lock_file="${target}.flock"
	root=${target%/*}
	[ "$root" != "$target" ] || root=.
	helper="${__lock_utils_source_dir}/fd_lock.pl"
	for ((i=0; i<${#__lock_utils_barrier_roots[@]}; i++)); do
		if [ "${__lock_utils_barrier_roots[$i]}" = "$root" ]; then
			barrier_fd=${__lock_utils_barrier_fds[$i]}
			root_id=${__lock_utils_barrier_ids[$i]}
			break
		fi
	done
	if [ -z "$barrier_fd" ]; then
		if ! root_id=$(perl "$helper" preflight "$target"); then
			printf 'Failed to acquire lock on %s\n' "$target" >&2
			return 1
		fi
		if ! __lock_utils_pick_free_fd; then
			printf 'no free reset barrier descriptor for %s\n' "$target" >&2
			printf 'Failed to acquire lock on %s\n' "$target" >&2
			return 1
		fi
		barrier_fd=$__lock_utils_free_fd
		barrier_file="$root/.rtbioscan_state_reset.flock"
		if ! eval "exec $barrier_fd<\"\$barrier_file\""; then
			printf 'Failed to acquire lock on %s\n' "$target" >&2
			return 1
		fi
		barrier_new=1
	fi
	if ! perl "$helper" barrier "$barrier_fd" "${LOCK_WAIT:-300}" "$target" "$root_id"; then
		[ "$barrier_new" -eq 0 ] || eval "exec $barrier_fd<&-"
		printf 'Failed to acquire lock on %s\n' "$target" >&2
		return 1
	fi
	if ! __lock_utils_pick_free_fd; then
		[ "$barrier_new" -eq 0 ] || eval "exec $barrier_fd<&-"
		printf 'no free lock descriptor for %s\n' "$target" >&2
		printf 'Failed to acquire lock on %s\n' "$target" >&2
		return 1
	fi
	fd=$__lock_utils_free_fd
	if [ -L "$lock_file" ] || [ ! -f "$lock_file" ]; then
		[ "$barrier_new" -eq 0 ] || eval "exec $barrier_fd<&-"
		printf 'unsafe stable lock file %s\n' "$lock_file" >&2
		printf 'Failed to acquire lock on %s\n' "$target" >&2
		return 1
	fi
	old_umask=$(umask)
	umask 077
	if eval "exec $fd<>\"\$lock_file\""; then
		umask "$old_umask"
	else
		umask "$old_umask"
		[ "$barrier_new" -eq 0 ] || eval "exec $barrier_fd<&-"
		printf 'Failed to acquire lock on %s\n' "$target" >&2
		return 1
	fi
	if perl "$helper" lock "$fd" "$barrier_fd" "${LOCK_WAIT:-300}" "$target" "$$" "$root_id"; then
		if [ "$barrier_new" -eq 1 ]; then
			__lock_utils_barrier_roots+=( "$root" )
			__lock_utils_barrier_fds+=( "$barrier_fd" )
			__lock_utils_barrier_ids+=( "$root_id" )
		fi
		acquired_locks+=( "$target" )
		acquired_lock_fds+=( "$fd" )
		return 0
	fi
	eval "exec $fd>&-"
	[ "$barrier_new" -eq 0 ] || eval "exec $barrier_fd<&-"
	printf 'Failed to acquire lock on %s\n' "$target" >&2
	return 1
}
