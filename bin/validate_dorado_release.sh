#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Usage:
  bin/validate_dorado_release.sh \
    --manifest FILE \
    --release-dir DIR \
    [--qualification-pod5 FILE --device DEVICE --report FILE]

Without --qualification-pod5, verifies installed bytes, platform, Dorado
version, model files, and the command-line surface used by RTBioScan.

With --qualification-pod5, also runs FAST, HAC, and SUP basecalling using the
production arguments, validates SAM/summary compatibility, and records a
qualification report. --device and --report are then required. Qualification
never changes RTBioScan defaults or state.
EOF
}

die() {
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

manifest=""
release_dir=""
pod5=""
device=""
report=""

while [ "$#" -gt 0 ]; do
	case "$1" in
		--manifest)
			[ "$#" -ge 2 ] || die "--manifest requires a value"
			manifest="$2"
			shift 2
			;;
		--release-dir)
			[ "$#" -ge 2 ] || die "--release-dir requires a value"
			release_dir="$2"
			shift 2
			;;
		--qualification-pod5)
			[ "$#" -ge 2 ] || die "--qualification-pod5 requires a value"
			pod5="$2"
			shift 2
			;;
		--device)
			[ "$#" -ge 2 ] || die "--device requires a value"
			device="$2"
			shift 2
			;;
		--report)
			[ "$#" -ge 2 ] || die "--report requires a value"
			report="$2"
			shift 2
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			die "unknown argument: $1"
			;;
	esac
done

[ -n "$manifest" ] || die "--manifest is required"
[ -f "$manifest" ] || die "Dorado release manifest not found: $manifest"
[ -n "$release_dir" ] || die "--release-dir is required"
[ -d "$release_dir" ] || die "Dorado release directory not found: $release_dir"

script_dir="$(cd "$(dirname "$0")" && pwd -P)"
perl "${script_dir}/install_dorado_release.pl" \
	--manifest "$manifest" \
	--destination "$release_dir" >/dev/null

manifest_value() {
	local key="$1"
	sed -n "s/^# ${key}=//p" "$manifest" | sed -n '1p'
}

release_id="$(manifest_value release_id)"
expected_platform="$(manifest_value platform)"
expected_version="$(manifest_value expected_version)"
[ -n "$release_id" ] || die "release manifest has no release_id metadata"
[ -n "$expected_platform" ] || die "release manifest has no platform metadata"
[ -n "$expected_version" ] || die "release manifest has no expected_version metadata"

case "$expected_platform" in
	osx-arm64)
		[ "$(uname -s)" = "Darwin" ] && [ "$(uname -m)" = "arm64" ] \
			|| die "Dorado release platform mismatch: expected osx-arm64, found $(uname -s)-$(uname -m)"
		;;
	linux-x64)
		[ "$(uname -s)" = "Linux" ] && [ "$(uname -m)" = "x86_64" ] \
			|| die "Dorado release platform mismatch: expected linux-x64, found $(uname -s)-$(uname -m)"
		;;
	*)
		die "unsupported Dorado release platform in manifest: $expected_platform"
		;;
esac

dorado="${release_dir}/bin/dorado"
fast_model="${release_dir}/models/dna_r10.4.1_e8.2_400bps_fast@v5.0.0"
hac_model="${release_dir}/models/dna_r10.4.1_e8.2_400bps_hac@v5.0.0"
sup_model="${release_dir}/models/dna_r10.4.1_e8.2_400bps_sup@v4.3.0"

[ -x "$dorado" ] || die "installed Dorado binary is not executable: $dorado"
for model in "$fast_model" "$hac_model" "$sup_model"; do
	[ -r "${model}/config.toml" ] || die "installed Dorado model is incomplete: $model"
done

actual_version="$("$dorado" --version 2>&1 | awk 'NR == 1 { print $1; exit }')"
[ "$actual_version" = "$expected_version" ] \
	|| die "Dorado version mismatch: expected=$expected_version actual=${actual_version:-unknown}"

basecaller_help="$("$dorado" basecaller --help 2>&1)" \
	|| die "Dorado basecaller help failed"
for required_option in \
	"--device" "--read-ids" "--min-qscore" "--batchsize" \
	"--chunksize" "--overlap" "--emit-sam"; do
	printf '%s\n' "$basecaller_help" | grep -F -- "$required_option" >/dev/null \
		|| die "Dorado basecaller does not advertise required option: $required_option"
done
"$dorado" summary --help >/dev/null 2>&1 \
	|| die "Dorado summary help failed"

if [ -z "$pod5" ]; then
	[ -z "$device" ] || die "--device requires --qualification-pod5"
	[ -z "$report" ] || die "--report requires --qualification-pod5"
	printf 'OK: Dorado release static compatibility passed (%s, %s)\n' \
		"$release_id" "$actual_version"
	exit 0
fi

[ -f "$pod5" ] || die "qualification POD5 not found: $pod5"
[ -r "$pod5" ] || die "qualification POD5 is not readable: $pod5"
[ -n "$device" ] || die "--device is required with --qualification-pod5"
[ -n "$report" ] || die "--report is required with --qualification-pod5"
for tool in samtools awk grep sed shasum; do
	command -v "$tool" >/dev/null 2>&1 \
		|| die "qualification tool not found in PATH: $tool"
done

qualification_tmp="$(mktemp -d "${TMPDIR:-/tmp}/rtbioscan-dorado-qualification.XXXXXX")" \
	|| die "cannot create Dorado qualification directory"
report_tmp=""
cleanup() {
	rm -rf "$qualification_tmp"
	[ -z "$report_tmp" ] || rm -f "$report_tmp"
}
trap cleanup EXIT HUP INT TERM

expected_summary_header='input_filename	batch_id	parent_read_id	read_id	run_id	channel	mux	minknow_events	start_time	duration	passes_filtering	template_start	num_events_template	template_duration	sequence_length_template	mean_qscore_template	pore_type	experiment_id	sample_id	end_reason'

validate_sam_and_summary() {
	local stage="$1"
	local sam="$2"
	local summary="${qualification_tmp}/${stage}.summary.tsv"
	local count
	samtools view -h "$sam" >/dev/null \
		|| die "Dorado ${stage} output is not valid SAM"
	count="$(samtools view -c "$sam")"
	[ "$count" -gt 0 ] || die "Dorado ${stage} output contains no reads"
	"$dorado" summary "$sam" > "$summary" \
		|| die "Dorado summary rejected ${stage} SAM output"
	[ "$(sed -n '1p' "$summary")" = "$expected_summary_header" ] \
		|| die "Dorado ${stage} summary header is incompatible with RTBioScan"
	[ "$(awk 'END { print NR + 0 }' "$summary")" -gt 1 ] \
		|| die "Dorado ${stage} summary contains no read rows"
}

run_basecaller() {
	local stage="$1"
	local model="$2"
	local overlap="$3"
	local chunksize="$4"
	local batchsize="$5"
	local min_qscore="$6"
	local read_list="$7"
	local sam="${qualification_tmp}/${stage}.sam"
	local log="${qualification_tmp}/${stage}.log"

	if [ -n "$read_list" ]; then
		"$dorado" basecaller -x "$device" --emit-sam \
			-o "$overlap" -c "$chunksize" -b "$batchsize" \
			--min-qscore "$min_qscore" -l "$read_list" \
			"$model" "$pod5" > "$sam" 2> "$log" \
			|| die "Dorado ${stage} basecalling failed on requested device '$device'"
	else
		"$dorado" basecaller -x "$device" --emit-sam \
			-o "$overlap" -c "$chunksize" -b "$batchsize" \
			--min-qscore "$min_qscore" \
			"$model" "$pod5" > "$sam" 2> "$log" \
			|| die "Dorado ${stage} basecalling failed on requested device '$device'"
	fi
	if grep -Eiq 'fall(ing)? back.*(cpu|device)' "$log"; then
		die "Dorado ${stage} reported a device fallback; requested '$device'"
	fi
	validate_sam_and_summary "$stage" "$sam"
}

run_basecaller fast "$fast_model" 100 1000 5040 0 ""
samtools view "${qualification_tmp}/fast.sam" \
	| awk 'NR == 1 { print $1; exit }' \
	> "${qualification_tmp}/selected_read_ids.list"
[ -s "${qualification_tmp}/selected_read_ids.list" ] \
	|| die "cannot select a read ID from FAST Dorado output"

run_basecaller hac "$hac_model" 500 2000 3024 10 \
	"${qualification_tmp}/selected_read_ids.list"
run_basecaller sup "$sup_model" 1000 5000 720 15 \
	"${qualification_tmp}/selected_read_ids.list"

report_parent="$(dirname "$report")"
[ -d "$report_parent" ] || mkdir -p "$report_parent"
[ ! -e "$report" ] || die "qualification report already exists: $report"
report_tmp="${report}.tmp.$$"
{
	printf 'field\tvalue\n'
	printf 'release_id\t%s\n' "$release_id"
	printf 'platform\t%s\n' "$expected_platform"
	printf 'dorado_version\t%s\n' "$actual_version"
	printf 'device\t%s\n' "$device"
	printf 'pod5_sha256\t%s\n' "$(shasum -a 256 "$pod5" | awk '{print $1}')"
	printf 'fast_reads\t%s\n' "$(samtools view -c "${qualification_tmp}/fast.sam")"
	printf 'hac_reads\t%s\n' "$(samtools view -c "${qualification_tmp}/hac.sam")"
	printf 'sup_reads\t%s\n' "$(samtools view -c "${qualification_tmp}/sup.sam")"
	printf 'status\tqualified\n'
} > "$report_tmp"
mv "$report_tmp" "$report"
report_tmp=""
chmod 0444 "$report" \
	|| die "cannot make qualification report read-only: $report"

printf 'OK: Dorado live compatibility passed (%s, device=%s)\n' \
	"$release_id" "$device"
printf 'Report: %s\n' "$report"
