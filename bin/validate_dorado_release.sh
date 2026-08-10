#!/usr/bin/env bash
set -euo pipefail

usage() {
	cat <<'EOF'
Usage:
  bin/validate_dorado_release.sh \
    --manifest FILE \
    --release-dir DIR \
    [--qualification-pod5 FILE --qualification-manifest FILE \
     --device DEVICE --report FILE]

Without --qualification-pod5, verifies installed bytes, platform, Dorado
version, model files, and the command-line surface used by RTBioScan.

With --qualification-pod5, also verifies the fixture manifest, runs FAST, HAC,
and SUP basecalling using the production arguments, validates SAM/summary
compatibility, and records a qualification report. --qualification-manifest,
--device, and --report are then required. Metal and CUDA runs produce
accelerator-qualification evidence; CPU and other devices are diagnostic-only
and cannot qualify a release for production. Qualification never changes
RTBioScan defaults or state.
EOF
}

die() {
	failure_reason="$*"
	printf 'ERROR: %s\n' "$*" >&2
	exit 1
}

failure_reason=""
manifest=""
release_dir=""
pod5=""
qualification_manifest=""
device=""
report=""
qualification_succeeded=0
current_stage="setup"
current_stage_exit=""
lock_dir=""
lock_acquired=0
attempt_ready=0

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
		--qualification-manifest)
			[ "$#" -ge 2 ] || die "--qualification-manifest requires a value"
			qualification_manifest="$2"
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
	[ -z "$qualification_manifest" ] \
		|| die "--qualification-manifest requires --qualification-pod5"
	[ -z "$device" ] || die "--device requires --qualification-pod5"
	[ -z "$report" ] || die "--report requires --qualification-pod5"
	printf 'OK: Dorado release static compatibility passed (%s, %s)\n' \
		"$release_id" "$actual_version"
	exit 0
fi

[ -f "$pod5" ] || die "qualification POD5 not found: $pod5"
[ -r "$pod5" ] || die "qualification POD5 is not readable: $pod5"
[ -n "$qualification_manifest" ] \
	|| die "--qualification-manifest is required with --qualification-pod5"
[ -f "$qualification_manifest" ] \
	|| die "qualification fixture manifest not found: $qualification_manifest"
[ -n "$device" ] || die "--device is required with --qualification-pod5"
[ -n "$report" ] || die "--report is required with --qualification-pod5"
case "$device" in
	metal|cuda|cuda:*)
		device_class="accelerator"
		qualification_scope="accelerator_candidate"
		report_status="accelerator_compatibility_passed"
		;;
	*)
		device_class="non_accelerator"
		qualification_scope="diagnostic_only"
		report_status="compatibility_only"
		;;
esac
for tool in samtools awk grep sed shasum; do
	command -v "$tool" >/dev/null 2>&1 \
		|| die "qualification tool not found in PATH: $tool"
done

fixture_header="$(sed -n '1p' "$qualification_manifest")"
[ "$fixture_header" = 'artifact	sha256	bytes	source_url	source_revision	chemistry	role' ] \
	|| die "malformed Dorado qualification fixture manifest header"
fixture_row_count="$(awk 'NR > 1 && $0 !~ /^[[:space:]]*$/ { count++ } END { print count + 0 }' "$qualification_manifest")"
[ "$fixture_row_count" -eq 1 ] \
	|| die "Dorado qualification fixture manifest must contain exactly one data row"
fixture_row="$(awk 'NR > 1 && $0 !~ /^[[:space:]]*$/ { print; exit }' "$qualification_manifest")"
IFS=$'\t' read -r fixture_artifact fixture_sha fixture_bytes fixture_url fixture_revision fixture_chemistry fixture_role <<< "$fixture_row"
[ -n "$fixture_artifact" ] && [ -n "$fixture_sha" ] && [ -n "$fixture_bytes" ] \
	&& [ -n "$fixture_url" ] && [ -n "$fixture_revision" ] \
	&& [ -n "$fixture_chemistry" ] && [ -n "$fixture_role" ] \
	|| die "malformed Dorado qualification fixture manifest row"
case "$fixture_sha" in
	*[!0-9a-f]*|'') die "invalid qualification fixture SHA-256" ;;
esac
[ "${#fixture_sha}" -eq 64 ] || die "invalid qualification fixture SHA-256 length"
case "$fixture_bytes" in
	*[!0-9]*|'') die "invalid qualification fixture byte count" ;;
esac
actual_fixture_bytes="$(wc -c < "$pod5" | tr -d '[:space:]')"
[ "$actual_fixture_bytes" = "$fixture_bytes" ] \
	|| die "qualification POD5 size mismatch: expected=$fixture_bytes actual=$actual_fixture_bytes"
actual_fixture_sha="$(shasum -a 256 "$pod5" | awk '{print $1}')"
[ "$actual_fixture_sha" = "$fixture_sha" ] \
	|| die "qualification POD5 checksum mismatch: expected=$fixture_sha actual=$actual_fixture_sha"

report_parent="$(dirname "$report")"
[ -d "$report_parent" ] || mkdir -p "$report_parent"
evidence_dir="${report}.evidence"
lock_dir="${report}.lockdir"

qualification_tmp="$(mktemp -d "${TMPDIR:-/tmp}/rtbioscan-dorado-qualification.XXXXXX")" \
	|| die "cannot create Dorado qualification directory"
report_tmp=""
evidence_tmp=""
attempt_started_utc="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"

write_environment_evidence() {
	local destination="$1"
	{
		printf 'field\tvalue\n'
		printf 'started_utc\t%s\n' "$attempt_started_utc"
		printf 'completed_utc\t%s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
		printf 'release_id\t%s\n' "$release_id"
		printf 'release_manifest_sha256\t%s\n' \
			"$(shasum -a 256 "$manifest" | awk '{print $1}')"
		printf 'platform\t%s\n' "$expected_platform"
		printf 'dorado_version\t%s\n' "$actual_version"
		printf 'device\t%s\n' "$device"
		printf 'device_class\t%s\n' "$device_class"
		printf 'pod5_artifact\t%s\n' "$fixture_artifact"
		printf 'pod5_sha256\t%s\n' "$actual_fixture_sha"
		printf 'uname_system\t%s\n' "$(uname -s)"
		printf 'uname_machine\t%s\n' "$(uname -m)"
		printf 'uname_release\t%s\n' "$(uname -r)"
	} > "${destination}/environment.tsv" || return 1

	uname -a > "${destination}/uname.txt" 2>&1 || return 1
	if ! "$dorado" -vv > "${destination}/dorado-version.txt" 2>&1; then
		"$dorado" --version > "${destination}/dorado-version.txt" 2>&1 \
			|| printf 'Dorado version probe failed with exit status %s\n' "$?" \
				>> "${destination}/dorado-version.txt"
	fi
	(
		ulimit -a
	) > "${destination}/resource-limits.txt" 2>&1 \
		|| printf 'resource-limit probe failed with exit status %s\n' "$?" \
			>> "${destination}/resource-limits.txt"
	{
		printf 'field\tvalue\n'
		printf 'TMPDIR\t%s\n' "${TMPDIR:-}"
		printf 'DYLD_LIBRARY_PATH\t%s\n' "${DYLD_LIBRARY_PATH:-}"
		printf 'DYLD_FALLBACK_LIBRARY_PATH\t%s\n' \
			"${DYLD_FALLBACK_LIBRARY_PATH:-}"
		printf 'CUDA_VISIBLE_DEVICES\t%s\n' "${CUDA_VISIBLE_DEVICES:-}"
		printf 'CONDA_PREFIX\t%s\n' "${CONDA_PREFIX:-}"
	} > "${destination}/runtime-environment.tsv" || return 1
	if command -v sw_vers >/dev/null 2>&1; then
		sw_vers > "${destination}/sw_vers.txt" 2>&1 || return 1
	fi
	if command -v system_profiler >/dev/null 2>&1; then
		if ! system_profiler SPHardwareDataType SPDisplaysDataType -detailLevel mini \
			| grep -E '^[[:space:]]*(Model Name|Model Identifier|Chip|Total Number of Cores|Memory|Chipset Model|Type|Bus|VRAM|Metal Support|Displays|Resolution):' \
			> "${destination}/hardware.txt"; then
			printf 'filtered system_profiler probe failed\n' \
				>> "${destination}/hardware.txt"
		fi
	elif command -v nvidia-smi >/dev/null 2>&1; then
		nvidia-smi \
			--query-gpu=name,driver_version,memory.total \
			--format=csv,noheader \
			> "${destination}/hardware.txt" 2>&1 \
			|| printf 'nvidia-smi hardware probe failed with exit status %s\n' "$?" \
				>> "${destination}/hardware.txt"
	fi
	return 0
}

preserve_failure_evidence() {
	local exit_status="$1"
	local artifact=""
	local artifact_name=""
	local artifact_sha=""
	local log_file=""
	local copied_logs=0
	local safe_reason=""

	[ -n "$report" ] || return 0
	[ ! -e "$report" ] || return 1
	[ ! -e "$evidence_dir" ] || return 1

	evidence_tmp="${evidence_dir}.tmp.$$"
	[ ! -e "$evidence_tmp" ] || return 1
	mkdir -m 0700 "$evidence_tmp" || return 1

	if [ -f "${qualification_tmp}/commands.tsv" ]; then
		cp "${qualification_tmp}/commands.tsv" "${evidence_tmp}/commands.tsv" \
			|| return 1
	fi
	for log_file in "${qualification_tmp}"/*.log; do
		[ -f "$log_file" ] || continue
		cp "$log_file" "${evidence_tmp}/$(basename "$log_file")" || return 1
		copied_logs=$((copied_logs + 1))
	done
	printf '%s\n' "$copied_logs" > "${evidence_tmp}/preserved_log_count.txt" \
		|| return 1
	write_environment_evidence "$evidence_tmp" || return 1

	safe_reason="$(printf '%s' "${failure_reason:-qualification command failed}" \
		| tr '\t\r\n' '   ')"
	{
		printf 'field\tvalue\n'
		printf 'release_id\t%s\n' "$release_id"
		printf 'platform\t%s\n' "$expected_platform"
		printf 'dorado_version\t%s\n' "$actual_version"
		printf 'device\t%s\n' "$device"
		printf 'device_class\t%s\n' "$device_class"
		printf 'qualification_scope\t%s\n' "$qualification_scope"
		printf 'failed_stage\t%s\n' "$current_stage"
		printf 'stage_exit_status\t%s\n' "${current_stage_exit:-unknown}"
		printf 'validator_exit_status\t%s\n' "$exit_status"
		printf 'failure_reason\t%s\n' "$safe_reason"
		printf 'evidence_directory\t%s\n' "$evidence_dir"
		printf 'pod5_artifact\t%s\n' "$fixture_artifact"
		printf 'pod5_sha256\t%s\n' "$actual_fixture_sha"
		printf 'status\tfailed\n'
	} > "${evidence_tmp}/failure-report.tsv" || return 1

	: > "${evidence_tmp}/checksums.sha256" || return 1
	for artifact in "${evidence_tmp}"/*; do
		[ -f "$artifact" ] || continue
		[ "$(basename "$artifact")" != "checksums.sha256" ] || continue
		artifact_name="$(basename "$artifact")"
		artifact_sha="$(shasum -a 256 "$artifact" | awk '{print $1}')" \
			|| return 1
		printf '%s  %s\n' "$artifact_sha" "$artifact_name" \
			>> "${evidence_tmp}/checksums.sha256" || return 1
	done
	for artifact in "${evidence_tmp}"/*; do
		[ -f "$artifact" ] || continue
		chmod 0444 "$artifact" || return 1
	done
	mv "$evidence_tmp" "$evidence_dir" || return 1
	evidence_tmp=""
	chmod 0555 "$evidence_dir" || return 1
	ln "${evidence_dir}/failure-report.tsv" "$report" || return 1
	return 0
}

cleanup() {
	local exit_status=$?
	local preserve_status=0
	trap - EXIT HUP INT TERM
	if [ "$qualification_succeeded" -ne 1 ] && [ "$attempt_ready" -eq 1 ]; then
		set +e
		preserve_failure_evidence "$exit_status"
		preserve_status=$?
		set -e
		if [ "$preserve_status" -ne 0 ]; then
			printf 'WARN: failed to preserve Dorado qualification evidence for %s\n' \
				"$report" >&2
		else
			printf 'Evidence: %s\n' "$evidence_dir" >&2
			printf 'Failure report: %s\n' "$report" >&2
		fi
	fi
	rm -rf "$qualification_tmp"
	[ -z "$report_tmp" ] || rm -f "$report_tmp"
	if [ -n "$evidence_tmp" ]; then
		chmod -R u+w "$evidence_tmp" 2>/dev/null || true
		rm -rf "$evidence_tmp"
	fi
	if [ "$lock_acquired" -eq 1 ]; then
		rm -rf "$lock_dir"
	fi
	exit "$exit_status"
}

handle_signal() {
	local signal_name="$1"
	local signal_status="$2"
	failure_reason="qualification interrupted by ${signal_name}"
	current_stage_exit="$signal_status"
	exit "$signal_status"
}
trap cleanup EXIT
trap 'handle_signal HUP 129' HUP
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM

if ! mkdir -m 0700 "$lock_dir"; then
	die "qualification report is locked by another attempt: $lock_dir"
fi
lock_acquired=1
{
	printf 'pid\t%s\n' "$$"
	printf 'started_utc\t%s\n' "$attempt_started_utc"
} > "${lock_dir}/owner.tsv"
chmod 0400 "${lock_dir}/owner.tsv"
[ ! -e "$report" ] || die "qualification report already exists: $report"
[ ! -e "$evidence_dir" ] \
	|| die "qualification evidence directory already exists: $evidence_dir"
attempt_ready=1

printf 'stage\tdorado\tsubcommand\tdevice\temit_sam\toverlap\tchunksize\tbatchsize\tmin_qscore\tread_list\tmodel\tdata\n' \
	> "${qualification_tmp}/commands.tsv"

validate_sam_and_summary() {
	local stage="$1"
	local sam="$2"
	local require_reads="$3"
	local summary="${qualification_tmp}/${stage}.summary.tsv"
	local count
	samtools view -h "$sam" >/dev/null \
		|| die "Dorado ${stage} output is not valid SAM"
	count="$(samtools view -c "$sam")"
	if [ "$require_reads" -eq 1 ] && [ "$count" -eq 0 ]; then
		die "Dorado ${stage} output contains no reads"
	fi
	"$dorado" summary "$sam" > "$summary" \
		|| die "Dorado summary rejected ${stage} SAM output"
	awk -F '\t' '
		NR == 1 {
			seen_header = 1
			for (i = 1; i <= NF; i++) h[$i] = 1
			if (!(h["filename"] || h["input_filename"])) exit 1
			if (!h["read_id"] || !h["run_id"] || !h["sequence_length_template"] || !h["mean_qscore_template"]) exit 1
		}
		END {
			if (!seen_header) exit 1
		}
	' "$summary" \
		|| die "Dorado ${stage} summary lacks columns required by RTBioScan"
	if [ "$require_reads" -eq 1 ]; then
		[ "$(awk 'END { print NR + 0 }' "$summary")" -gt 1 ] \
			|| die "Dorado ${stage} summary contains no read rows"
	fi
}

run_basecaller() {
	local stage="$1"
	local model="$2"
	local overlap="$3"
	local chunksize="$4"
	local batchsize="$5"
	local min_qscore="$6"
	local read_list="$7"
	local require_reads="$8"
	local sam="${qualification_tmp}/${stage}.sam"
	local log="${qualification_tmp}/${stage}.log"

	current_stage="$stage"
	printf '%s\t%s\tbasecaller\t%s\t1\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
		"$stage" "$dorado" "$device" "$overlap" "$chunksize" "$batchsize" \
		"$min_qscore" "$read_list" "$model" "$pod5" \
		>> "${qualification_tmp}/commands.tsv"
	if [ -n "$read_list" ]; then
		if "$dorado" basecaller -x "$device" --emit-sam \
			-o "$overlap" -c "$chunksize" -b "$batchsize" \
			--min-qscore "$min_qscore" -l "$read_list" \
			"$model" "$pod5" > "$sam" 2> "$log" \
		; then
			current_stage_exit=0
		else
			current_stage_exit=$?
			die "Dorado ${stage} basecalling failed on requested device '$device'"
		fi
	else
		if "$dorado" basecaller -x "$device" --emit-sam \
			-o "$overlap" -c "$chunksize" -b "$batchsize" \
			--min-qscore "$min_qscore" \
			"$model" "$pod5" > "$sam" 2> "$log" \
		; then
			current_stage_exit=0
		else
			current_stage_exit=$?
			die "Dorado ${stage} basecalling failed on requested device '$device'"
		fi
	fi
	if grep -Eiq 'fall(ing)? back.*(cpu|device)' "$log"; then
		current_stage_exit=1
		die "Dorado ${stage} reported a device fallback; requested '$device'"
	fi
	validate_sam_and_summary "$stage" "$sam" "$require_reads"
}

run_basecaller fast "$fast_model" 100 1000 5040 0 "" 1
samtools view "${qualification_tmp}/fast.sam" \
	| awk 'NR == 1 { print $1; exit }' \
	> "${qualification_tmp}/selected_read_ids.list"
[ -s "${qualification_tmp}/selected_read_ids.list" ] \
	|| die "cannot select a read ID from FAST Dorado output"

run_basecaller hac "$hac_model" 500 2000 3024 10 \
	"${qualification_tmp}/selected_read_ids.list" 0
if [ "$(samtools view -c "${qualification_tmp}/hac.sam")" -eq 0 ]; then
	run_basecaller hac_format_probe "$hac_model" 500 2000 3024 0 \
		"${qualification_tmp}/selected_read_ids.list" 1
else
	cp "${qualification_tmp}/hac.sam" "${qualification_tmp}/hac_format_probe.sam"
fi

run_basecaller sup "$sup_model" 1000 5000 720 15 \
	"${qualification_tmp}/selected_read_ids.list" 0
if [ "$(samtools view -c "${qualification_tmp}/sup.sam")" -eq 0 ]; then
	run_basecaller sup_format_probe "$sup_model" 1000 5000 720 0 \
		"${qualification_tmp}/selected_read_ids.list" 1
else
	cp "${qualification_tmp}/sup.sam" "${qualification_tmp}/sup_format_probe.sam"
fi

report_tmp="${report}.tmp.$$"
{
	printf 'field\tvalue\n'
	printf 'release_id\t%s\n' "$release_id"
	printf 'platform\t%s\n' "$expected_platform"
	printf 'dorado_version\t%s\n' "$actual_version"
	printf 'device\t%s\n' "$device"
	printf 'device_class\t%s\n' "$device_class"
	printf 'qualification_scope\t%s\n' "$qualification_scope"
	printf 'pod5_artifact\t%s\n' "$fixture_artifact"
	printf 'pod5_sha256\t%s\n' "$actual_fixture_sha"
	printf 'pod5_source_revision\t%s\n' "$fixture_revision"
	printf 'pod5_chemistry\t%s\n' "$fixture_chemistry"
	printf 'fast_reads\t%s\n' "$(samtools view -c "${qualification_tmp}/fast.sam")"
	printf 'hac_reads_at_threshold\t%s\n' "$(samtools view -c "${qualification_tmp}/hac.sam")"
	printf 'hac_format_probe_reads\t%s\n' "$(samtools view -c "${qualification_tmp}/hac_format_probe.sam")"
	printf 'sup_reads_at_threshold\t%s\n' "$(samtools view -c "${qualification_tmp}/sup.sam")"
	printf 'sup_format_probe_reads\t%s\n' "$(samtools view -c "${qualification_tmp}/sup_format_probe.sam")"
	printf 'status\t%s\n' "$report_status"
} > "$report_tmp"
mv "$report_tmp" "$report"
report_tmp=""
chmod 0444 "$report" \
	|| die "cannot make qualification report read-only: $report"
qualification_succeeded=1

printf 'OK: Dorado live compatibility passed (%s, device=%s)\n' \
	"$release_id" "$device"
if [ "$device_class" != "accelerator" ]; then
	printf 'NOTICE: device=%s is diagnostic-only and cannot qualify a production RTBioScan release\n' \
		"$device"
fi
printf 'Report: %s\n' "$report"
