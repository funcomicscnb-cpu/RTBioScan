db_sig() {
	local base="$1"
	local f=""
	for ext in nsq nin nhr; do
		if [ -f "${base}.${ext}" ]; then
			f="${base}.${ext}"
			break
		fi
	done
	if [ -z "$f" ]; then
		echo "missing"
		return
	fi
	stat -c '%s:%Y' "$f" 2>/dev/null || stat -f '%z:%m' "$f" 2>/dev/null || echo "missing"
}

dir_sig() {
	local d="$1"
	if [ -d "$d" ]; then
		stat -c '%Y' "$d" 2>/dev/null || stat -f '%m' "$d" 2>/dev/null || echo "missing"
	else
		echo "missing"
	fi
}
