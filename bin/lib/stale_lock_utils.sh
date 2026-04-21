stale_lock_maybe_reclaim() {
	local lock_dir="$1"
	local lock_meta="$2"
	local this_host="$3"
	local stale_lock_ttl_seconds="${4:-0}"
	local lock_label="${5:-stale lock}"
	local reclaim_callback="$6"
	local dump_meta_on_age="${7:-0}"
	local lock_pid=""
	local lock_host=""
	local lock_started=""
	local now=""
	local age=0

	case "$stale_lock_ttl_seconds" in
		''|*[!0-9]*)
			echo "ERROR: invalid stale_lock_ttl_seconds '$stale_lock_ttl_seconds'; expected integer >= 0" 1>&2
			return 2
			;;
	esac

	# Return codes:
	#   0  no reclaim performed
	#   2  invalid helper input
	#  10  stale lock was reclaimed successfully
	#  11  stale lock was detected but removal failed
	if [ -f "$lock_meta" ]; then
		lock_pid="$(awk -F= '/^pid=/{print $2; exit}' "$lock_meta" 2>/dev/null || true)"
		lock_host="$(awk -F= '/^host=/{print $2; exit}' "$lock_meta" 2>/dev/null || true)"
		lock_started="$(awk -F= '/^started_epoch=/{print $2; exit}' "$lock_meta" 2>/dev/null || true)"
	fi
	if [ -z "$lock_started" ]; then
		lock_started="$(stat -c %Y "$lock_dir" 2>/dev/null || stat -f %m "$lock_dir" 2>/dev/null || true)"
	fi
	now="$(date +%s 2>/dev/null || echo 0)"

	if [ -n "$lock_pid" ] && [[ "$lock_pid" =~ ^[0-9]+$ ]] && [ -n "$lock_host" ] && [ "$lock_host" = "$this_host" ]; then
		if ! kill -0 "$lock_pid" 2>/dev/null; then
			echo "WARN: reclaiming stale ${lock_label} (dead pid=$lock_pid host=$lock_host) at $lock_dir" 1>&2
			"$reclaim_callback"
			if [ ! -d "$lock_dir" ]; then
				return 10
			fi
			echo "WARN: failed to remove stale ${lock_label} at $lock_dir" 1>&2
			return 11
		fi
		# A same-host live owner always wins over age-based reclaim. This avoids
		# stealing a healthy long-running round lock just because its TTL is low.
		return 0
	fi

	if [ "$stale_lock_ttl_seconds" -gt 0 ] && [ -n "$lock_started" ] && [[ "$lock_started" =~ ^[0-9]+$ ]] && [ "$now" -gt 0 ]; then
		age=$(( now - lock_started ))
		if [ "$age" -ge "$stale_lock_ttl_seconds" ]; then
			echo "WARN: reclaiming stale ${lock_label} (age=${age}s ttl=${stale_lock_ttl_seconds}s) at $lock_dir" 1>&2
			if [ "$dump_meta_on_age" = "1" ] && [ -f "$lock_meta" ]; then
				echo "WARN: Lock metadata:" 1>&2
				cat "$lock_meta" 1>&2 || true
			fi
			"$reclaim_callback"
			if [ ! -d "$lock_dir" ]; then
				return 10
			fi
			echo "WARN: failed to remove stale ${lock_label} at $lock_dir" 1>&2
			return 11
		fi
	fi

	return 0
}
