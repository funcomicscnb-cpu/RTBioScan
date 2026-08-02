#!/usr/bin/env bash
set -u

usage() {
	echo "Usage: dorado_basecaller_input_compat.sh <file|directory> <dorado-bin> basecaller [args ...] <model> <input>" >&2
	exit 2
}

[ "$#" -ge 4 ] || usage

input_mode="$1"
dorado_bin="$2"
shift 2

case "$input_mode" in
	file)
		exec "$dorado_bin" "$@"
		;;
	directory)
		;;
	*)
		echo "ERROR: unsupported Dorado input mode '$input_mode'" >&2
		exit 2
		;;
esac

[ "$1" = "basecaller" ] || {
	echo "ERROR: directory compatibility mode is only valid for Dorado basecaller" >&2
	exit 2
}

args=("$@")
last_index=$((${#args[@]} - 1))
input_path="${args[$last_index]}"

if [ -d "$input_path" ]; then
	exec "$dorado_bin" "${args[@]}"
fi
if [ ! -f "$input_path" ]; then
	echo "ERROR: Dorado input is not a readable file or directory: $input_path" >&2
	exit 2
fi

input_parent=$(cd "$(dirname "$input_path")" 2>/dev/null && pwd -P) || {
	echo "ERROR: cannot resolve Dorado input parent: $input_path" >&2
	exit 2
}
input_abs="${input_parent}/$(basename "$input_path")"
tmp_parent="${TMPDIR:-/tmp}"
stage_dir=$(mktemp -d "${tmp_parent%/}/rtbioscan-dorado-input.XXXXXX") || {
	echo "ERROR: cannot create temporary Dorado input directory" >&2
	exit 1
}
stage_name=$(basename "$input_abs")
staged_input="${stage_dir}/${stage_name}"

cleanup() {
	rm -f "$staged_input" 2>/dev/null || true
	rmdir "$stage_dir" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

ln -s "$input_abs" "$staged_input" || {
	echo "ERROR: cannot stage Dorado input file: $input_abs" >&2
	exit 1
}
args[$last_index]="$stage_dir"

"$dorado_bin" "${args[@]}"
exit_status=$?
exit "$exit_status"
