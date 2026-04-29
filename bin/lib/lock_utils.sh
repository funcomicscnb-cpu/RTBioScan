lock_dir_path() {
	printf "%s.lockdir" "$1"
}

__lock_utils_exit_dispatch_installed=0
__lock_utils_exit_hooks=()

__lock_utils_ensure_arrays() {
	declare -p __lock_utils_exit_hooks >/dev/null 2>&1 || __lock_utils_exit_hooks=()
	declare -p acquired_locks >/dev/null 2>&1 || acquired_locks=()
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
	if [ "${#__lock_utils_exit_hooks[@]}" -gt 0 ]; then
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
	if [ "${#__lock_utils_exit_hooks[@]}" -gt 0 ]; then
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
	local dir new_locks=() l
	dir=$(lock_dir_path "$target")
	rmdir "$dir" 2>/dev/null || true
	[ "${#acquired_locks[@]}" -eq 0 ] && return
	for l in "${acquired_locks[@]}"; do
		[ "$l" = "$target" ] || new_locks+=( "$l" )
	done
	if [ "${#new_locks[@]}" -eq 0 ]; then
		acquired_locks=()
	else
		acquired_locks=( "${new_locks[@]}" )
	fi
}

cleanup_locks() {
	local l
	__lock_utils_ensure_arrays
	[ "${#acquired_locks[@]}" -eq 0 ] && return
	for l in "${acquired_locks[@]}"; do
		release_lock "$l"
	done
}

init_lock_helpers() {
	__lock_utils_ensure_arrays
	acquired_locks=()
	# Chain lock cleanup onto EXIT instead of replacing any existing handler.
	# Callers that register additional EXIT cleanup should prefer append_trap EXIT ...
	# so lock cleanup remains composed rather than overwritten. Preserved hooks run
	# in isolated subshells: they see the original $? but cannot share shell-state
	# mutations with later hooks.
	append_trap EXIT cleanup_locks
}

acquire_lock() {
	local target="$1"
	local waited=0
	local dir
	dir=$(lock_dir_path "$target")
	while ! mkdir "$dir" 2>/dev/null; do
		sleep 1
		waited=$((waited + 1))
		if [ "$waited" -ge "${LOCK_WAIT:-300}" ]; then
			echo "Failed to acquire lock on $target" 1>&2
			return 1
		fi
	done
	acquired_locks+=( "$target" )
	return 0
}
