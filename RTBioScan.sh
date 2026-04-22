#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_DIR="$(pwd -P)"
FEEDER="${SCRIPT_DIR}/bin/Metadata_pod5_processing.sh"
SERVER="${SCRIPT_DIR}/bin/serve_report.sh"
source "${SCRIPT_DIR}/bin/lib/stale_lock_utils.sh"

# ── defaults ──────────────────────────────────────────────────────────────────
feeder=0
run_id=""
input_folder=""
num_reads=""
sleep_time=""
metadata_file=""
general_fasta=""
primers_fasta=""
targets=""
targets_explicit=0
resolved_targets=""
do_metadata=0
skip_pod5=0
delete_full_pod5=0
delete_input_pod5=0
delete_from_ori_dir=0

serve=0
view_only=0
serve_port="8000"
serve_port_explicit=0
serve_host="127.0.0.1"
serve_open=0
serve_open_all=0
serve_open_last=""
serve_quiet=0
serve_dir=""        # derived from --outdir if not set explicitly

clean_ref=""
clean_run_id=""
clean_all=0
clean_temp_run_id=""
clean_temp_all=0
dry_run=0
yes=0

nf_args=()
FEEDER_LOCK_STALE_TTL_SECONDS=21600
_feeder_preflight_lock_dir=""

current_host_id() {
  if [[ -n "${HOSTNAME:-}" ]]; then
    printf '%s\n' "$HOSTNAME"
  elif command -v hostname >/dev/null 2>&1; then
    hostname 2>/dev/null || printf 'unknown\n'
  else
    printf 'unknown\n'
  fi
}

feeder_lock_dir_for_run() {
  local run="$1"
  printf '%s/results/pod5/%s/metadata/.feeder.lockdir\n' "$SCRIPT_DIR" "$run"
}

feeder_lock_meta_for_run() {
  local run="$1"
  printf '%s/meta.env\n' "$(feeder_lock_dir_for_run "$run")"
}

remove_feeder_lock_if_stale() {
  local lock_dir="${_feeder_preflight_lock_dir:-}"
  [[ -n "$lock_dir" ]] || return 0
  rm -f "$lock_dir/meta.env" 2>/dev/null || true
  rmdir "$lock_dir" 2>/dev/null || rm -rf "$lock_dir" 2>/dev/null || true
}

preflight_run_name_or_die() {
  local _run="$1"
  local _a

  # Skip if the user is resuming — name reuse is intentional then.
  for _a in ${nf_args[@]+"${nf_args[@]}"}; do
    [[ "$_a" == "-resume" || "$_a" == "--resume" || "$_a" == -resume=* ]] && return 0
  done

  # Pipeline always runs from SCRIPT_DIR, so history lives there.
  if history_contains_run "$SCRIPT_DIR" "$_run"; then
    echo "ERROR: Nextflow run name '$_run' already exists in .nextflow/history." >&2
    echo "       To start fresh:  ./RTBioScan.sh --clean '$_run'" >&2
    echo "       To resume:       add -resume to your command." >&2
    exit 1
  fi
}

preflight_feeder_lock_or_die() {
  local run="$1"
  local lock_dir meta host reclaim_status
  lock_dir="$(feeder_lock_dir_for_run "$run")"
  meta="$(feeder_lock_meta_for_run "$run")"
  [[ -d "$lock_dir" ]] || return 0

  _feeder_preflight_lock_dir="$lock_dir"
  host="$(current_host_id)"
  set +e
  stale_lock_maybe_reclaim \
    "$lock_dir" \
    "$meta" \
    "$host" \
    "$FEEDER_LOCK_STALE_TTL_SECONDS" \
    "feeder lock" \
    remove_feeder_lock_if_stale \
    1
  reclaim_status=$?
  set -e

  if [[ "$reclaim_status" -eq 2 ]]; then
    echo "ERROR: stale_lock_maybe_reclaim rejected feeder lock parameters" >&2
    exit 1
  fi
  if [[ "$reclaim_status" -eq 11 ]]; then
    echo "ERROR: unable to reclaim stale feeder lock at $lock_dir" >&2
    exit 1
  fi
  if [[ -d "$lock_dir" ]]; then
    echo "ERROR: another feeder is already active for run '$run' (lock: $lock_dir)" >&2
    exit 1
  fi
}

list_run_feeder_pids() {
  local _run="${1:-}"
  local _self="${BASHPID:-$$}"
  [[ -n "$_run" ]] || return 0

  ps -ax -o pid= -o command= 2>/dev/null \
    | awk -v script="$FEEDER" -v run="$_run" -v self="$_self" '
        function has_script_field(    i) {
          for (i = 2; i <= NF; i++) {
            if ($i == script) return 1
          }
          return 0
        }
        function has_run_id_field(    i) {
          for (i = 2; i <= NF; i++) {
            if ($i == "--run_id" && i < NF && $(i + 1) == run) return 1
            if (index($i, "--run_id=") == 1 && substr($i, 10) == run) return 1
          }
          return 0
        }
        {
          pid=$1
          if (pid == self) next
          if (!has_script_field()) next
          if (has_run_id_field()) print pid
        }' || true
}

kill_run_feeders() {
  local _run="${1:-}"
  local _reason="${2:-same-run feeder}"
  local _exclude_pid="${3:-}"
  local _pids=""
  local _pid=""
  local _remaining=0
  local _live=0

  [[ -n "$_run" ]] || return 0
  _pids="$(list_run_feeder_pids "$_run" | awk -v exclude="$_exclude_pid" 'exclude == "" || $1 != exclude { print $1 }')"
  [[ -n "$_pids" ]] || return 0

  echo "==> [RTBioScan] Stopping ${_reason} for run '${_run}': $(printf '%s' "$_pids" | tr '\n' ' ')"
  while IFS= read -r _pid; do
    [[ -n "$_pid" ]] || continue
    kill -s TERM "$_pid" 2>/dev/null || true
  done <<< "$_pids"

  _remaining=2
  while [[ "$_remaining" -gt 0 ]]; do
    _live=0
    while IFS= read -r _pid; do
      [[ -n "$_pid" ]] || continue
      if kill -0 "$_pid" 2>/dev/null; then
        _live=1
        break
      fi
    done <<< "$_pids"
    [[ "$_live" -eq 0 ]] && break
    sleep 1
    _remaining=$((_remaining - 1))
  done

  _live=0
  while IFS= read -r _pid; do
    [[ -n "$_pid" ]] || continue
    if kill -0 "$_pid" 2>/dev/null; then
      _live=1
      kill -s KILL "$_pid" 2>/dev/null || true
    fi
  done <<< "$_pids"
  if [[ "$_live" -eq 1 ]]; then
    echo "==> [RTBioScan] Escalated ${_reason} shutdown to SIGKILL."
  fi
}

pid_is_live_non_zombie() {
  local _pid="${1:-}"
  local _stat=""

  [[ "$_pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$_pid" 2>/dev/null || return 1
  _stat="$(ps -o stat= -p "$_pid" 2>/dev/null | awk 'NR==1 { gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0); print; exit }')"
  if [[ -n "$_stat" && "$_stat" == Z* ]]; then
    return 1
  fi
  return 0
}

cleanup_feeder_lock_for_root_if_inactive() {
  local _root="${1:-}"
  local _run="${2:-}"
  local _lock_dir=""
  local _lock_meta=""
  local _lock_pid=""

  [[ -n "$_root" && -n "$_run" ]] || return 0

  _lock_dir="${_root}/results/pod5/${_run}/metadata/.feeder.lockdir"
  _lock_meta="${_lock_dir}/meta.env"
  [[ -d "$_lock_dir" ]] || return 0

  if [[ -f "$_lock_meta" ]]; then
    _lock_pid="$(awk -F= '/^pid=/{print $2; exit}' "$_lock_meta" 2>/dev/null || true)"
    if pid_is_live_non_zombie "$_lock_pid"; then
      return 0
    fi
  fi

  rm -f "$_lock_meta" 2>/dev/null || true
  rmdir "$_lock_dir" 2>/dev/null || rm -rf "$_lock_dir" 2>/dev/null || true
}

stop_clean_run_feeder_if_active_or_die() {
  local _root="${1:-}"
  local _run="${2:-}"
  local _pids=""
  local _lock_meta=""
  local _lock_pid=""
  local _remaining=0

  [[ -n "$_root" && -n "$_run" ]] || return 0

  _pids="$(list_run_feeder_pids "$_run")"
  if [[ -z "$_pids" ]] && ! feeder_lock_has_live_holder_for_root "$_root" "$_run"; then
    return 0
  fi

  echo "==> [RTBioScan] Checking for active feeder with run '${_run}' before --clean ..."
  if [[ -n "$_pids" ]]; then
    kill_run_feeders "$_run" "active feeder before --clean"
  fi

  if feeder_lock_has_live_holder_for_root "$_root" "$_run"; then
    _lock_meta="${_root}/results/pod5/${_run}/metadata/.feeder.lockdir/meta.env"
    _lock_pid="$(awk -F= '/^pid=/{print $2; exit}' "$_lock_meta" 2>/dev/null || true)"
    if [[ "$_lock_pid" =~ ^[0-9]+$ ]]; then
      kill -s TERM "$_lock_pid" 2>/dev/null || true
      _remaining=2
      while [[ "$_remaining" -gt 0 ]]; do
        if ! pid_is_live_non_zombie "$_lock_pid"; then
          break
        fi
        sleep 1
        _remaining=$((_remaining - 1))
      done
      if pid_is_live_non_zombie "$_lock_pid"; then
        kill -s KILL "$_lock_pid" 2>/dev/null || true
      fi
    fi
  fi

  cleanup_feeder_lock_for_root_if_inactive "$_root" "$_run"

  if feeder_lock_has_live_holder_for_root "$_root" "$_run"; then
    echo "ERROR: active feeder lock detected for run '${_run}'." >&2
    echo "       Failed to stop the feeder automatically; stop it manually, then retry --clean." >&2
    exit 1
  fi
}

preflight_orphan_feeders_for_run() {
  local _run="${1:-}"
  [[ -n "$_run" ]] || return 0
  kill_run_feeders "$_run" "orphan feeder"
}

# ── usage ─────────────────────────────────────────────────────────────────────
usage() {
  cat <<'EOF'
Usage: RTBioScan.sh [OPTIONS] [NEXTFLOW OPTIONS]

Feeder options:
  --feeder                Enable the POD5 feeder alongside the pipeline
  --run_id <id>           Run ID (required with --feeder or --do_metadata).
                          Also sets the Nextflow -name unless -name is passed
                          explicitly in the Nextflow args.
  --input_folder <dir>    MinKnow POD5 output folder (required with --feeder
                          unless --skip_pod5)
  --num_reads <n>         Reads per POD5 chunk [default: 200000]
  --sleep_time <s>        Feeder poll interval in seconds [default: 300]
  --metadata <file>       Metadata TSV file
  --general_fasta <file>  Master demultiplexing FASTA
  --primers_fasta <file>  Primers FASTA
  --targets <list>        Optional pipe-separated marker targets; overrides
                          config-derived params.targets and is forwarded to
                          metadata setup when present
  --do_metadata           Set up input_files/ before starting (runs synchronously,
                          can be used without --feeder)
  --skip_pod5             Skip POD5 splitting in feeder
  --delete-full-pod5      Delete each full_pod5/ file once all its reads have been consumed into rounds
  --delete_input_pod5     Delete processed POD5 from results/ immediately after each round (Nextflow param)
  --delete_from_ori_dir   Delete source POD5 from ori_dir after staging into the intake queue (Nextflow param)

Report server options (wraps bin/serve_report.sh):
  --serve                 Start the HTTP report server alongside the pipeline
  --view                  Start the report server for an existing outdir and wait (no pipeline)
  --serve-port <n>        Port for the HTTP server [default: 8000]
  --serve-host <addr>     Bind address [default: 127.0.0.1]
  --serve-open            Open report_html/report.html in the default browser on start
  --serve-open-all        Open all per-run reports in the browser on start
  --serve-open-last <n>   Open the last N per-run reports in the browser on start
  --serve-quiet           Suppress HTTP request logging
  --serve-dir <dir>       Directory to serve [default: same as --outdir, or results/]

Cleanup options:
  --clean-ref <dir>       Reference directory for cleanup discovery. With
                          --clean-all/--clean-temp-all it becomes the cleanup
                          root; with run-specific cleanup it is searched first.
	  --clean <id>            Remove all artifacts for the given run ID:
	                            results/report_html/runs/<id>/  results/config/<id>/
	                            results/runs/<id>/
	                            results/current/state/<id>/     results/temp/*/state/<id>/
	                            results/pod5/<id>/              results/sample_info/<id>/
                            results/ongoing/state/<id>      work/
                          Also filters <id> from .nextflow/history and
                          results/report_html/runs_index.jsonl. Root discovery order:
                          --clean-ref, current working directory, pipeline dir.
  --clean-all             Remove ALL run artifacts: results/report_html/,
                            results/config/, results/current/, results/temp/,
                            results/pod5/, results/sample_info/, work/,
                            results/ongoing/, .nextflow/, and .nextflow.log* files
                            from the current working directory by default.
                          --clean-temp <id>       Remove only temporary artifacts for the given run ID:
                            results/temp/*/state/<state_id>/
                            results/ongoing/state/<state_id>/
                            work/ temp files tracked by Nextflow for <id>
                          Preserves reports, POD5 archives, sample info, and
                          Nextflow history/log metadata; per-task .command*
                          stubs may remain in work/.
  --clean-temp-all        Remove only temporary artifacts for ALL runs:
                            results/temp/, results/ongoing/, and all work/
                            contents in the current working directory by
                            default.
  --dry-run               (with any clean option) Print what would be deleted.
  -y, --yes               Skip the delete confirmation prompt.

All remaining arguments are forwarded to: nextflow run main.nf
When --do_metadata / --feeder rely on config-derived params.targets, config-
affecting Nextflow options must use separate-token forms such as:
  -profile test   -c conf/file.config   -C conf/file.config

Examples:
  # Pipeline only (POD5s already present):
  ./RTBioScan.sh -profile test --reads "pod5/reads_rt_round_pod5/*pod5"

  # Resume with live report in browser:
  ./RTBioScan.sh --serve --serve-open -resume MyRun -profile test

  # Pipeline + feeder + live report:
  ./RTBioScan.sh --feeder --serve --serve-open \
      --run_id MyRun --input_folder /data/minknow/MyRun --targets "COI|ITS2" -profile test

  # First-time full run (metadata + feeder + pipeline + report):
  ./RTBioScan.sh --feeder --do_metadata --serve --serve-open \
      --run_id MyRun --input_folder /data/minknow/MyRun --targets "COI|ITS2" -profile test

  # Clean a specific run and start fresh:
  ./RTBioScan.sh --clean MyRun
  ./RTBioScan.sh --clean MyRun -y            # skip confirmation
  ./RTBioScan.sh --clean MyRun --dry-run     # preview only
  ./RTBioScan.sh --clean-ref /data/runA --clean MyRun

  # Wipe all run artifacts:
  ./RTBioScan.sh --clean-all

  # Drop only temporary state/work files for one run or all runs:
  ./RTBioScan.sh --clean-temp MyRun --dry-run
  ./RTBioScan.sh --clean-temp-all
EOF
  exit 1
}

targets_match_grammar() {
  [[ "$1" =~ ^[^[:space:]|]+(\|[^[:space:]|]+)*$ ]]
}

strip_matching_outer_quotes() {
  local _value="$1"
  local _len="${#_value}"
  local _first=""
  local _last=""
  if [[ "$_len" -ge 2 ]]; then
    _first="${_value:0:1}"
    _last="${_value:$((_len-1)):1}"
    if [[ "$_first" == "$_last" && ( "$_first" == "'" || "$_first" == '"' ) ]]; then
      printf '%s' "${_value:1:$((_len-2))}"
      return 0
    fi
  fi
  printf '%s' "$_value"
}

die_missing_config_selector_value() {
  local _opt="$1"
  echo "ERROR: config-affecting Nextflow option '$_opt' is missing its required value." >&2
  exit 1
}

die_attached_config_selector() {
  local _opt="$1"
  echo "ERROR: config-affecting Nextflow option '$_opt' must use the separate-token form in RTBioScan.sh." >&2
  exit 1
}

require_wrapper_option_value() {
  local _opt="$1"
  local _value="${2-}"
  if [[ -z "$_value" || "$_value" == -* ]]; then
    echo "ERROR: wrapper option '$_opt' is missing its required value." >&2
    exit 1
  fi
}

nextflow_arg_present() {
  local _opt="$1"
  local _arg=""
  for _arg in ${nf_args[@]+"${nf_args[@]}"}; do
    case "$_arg" in
      "$_opt"|"$_opt"=*)
        return 0
        ;;
    esac
  done
  return 1
}

nextflow_arg_value() {
  local _opt="$1"
  local _i=0
  local _arg=""
  local _value=""
  local _found=1
  for (( _i=0; _i<${#nf_args[@]}; _i++ )); do
    _arg="${nf_args[$_i]}"
    if [[ "$_arg" == "$_opt" ]]; then
      if (( _i + 1 < ${#nf_args[@]} )); then
        _value="${nf_args[$((_i+1))]}"
        _found=0
        continue
      fi
      continue
    fi
    case "$_arg" in
      "$_opt"=*)
        _value="${_arg#*=}"
        _found=0
        ;;
    esac
  done
  if [[ $_found -eq 0 ]]; then
    printf '%s\n' "$_value"
    return 0
  fi
  return 1
}

resolve_outdir_path() {
  local _dir="$1"
  if [[ "$_dir" == /* ]]; then
    printf '%s\n' "$_dir"
  else
    printf '%s/%s\n' "$SCRIPT_DIR" "$_dir"
  fi
}

sanitize_state_id() {
  perl -e '
    use strict;
    use warnings;
    my $value = shift // q{};
    $value =~ s/[^A-Za-z0-9_.-]+/_/g;
    print $value;
  ' "${1:-}"
}

effective_nextflow_run_name() {
  local _name=""
  _name="$(nextflow_arg_value "-name" || true)"
  if [[ -n "$_name" ]]; then
    printf '%s\n' "$_name"
  else
    printf '%s\n' "${run_id:-}"
  fi
}

seed_initial_run_status_if_needed() {
  local _outdir=""
  local _outdir_path=""
  local _run_name=""
  local _state_id_raw=""
  local _state_id=""
  local _run_started_utc=""
  local _history=""
  local _run_dir=""
  local _run_json=""
  local _run_index=""
  local _run_index_lock=""
  local _root_html=""
  local _root_state=""
  local _html_enabled=""
  local _html_enabled_norm=""
  local _auto_refresh=""
  local _refresh_seconds=""
  local _url_prefix=""
  local _barcode=""

  INITIAL_RUN_STATUS_SEEDED=0
  [[ -n "${run_id:-}" ]] || return 0
  [[ $cleanup_modes -eq 0 && $dry_run -eq 0 && $view_only -eq 0 ]] || return 0

  _run_name="$(effective_nextflow_run_name)"
  [[ -n "$_run_name" ]] || return 0

  _outdir="$(nextflow_arg_value "--outdir" || true)"
  _outdir="${_outdir:-results}"
  _outdir_path="$(resolve_outdir_path "$_outdir")"

  _state_id_raw="$(nextflow_arg_value "--state_id" || true)"
  _state_id_raw="${_state_id_raw:-$_run_name}"
  _state_id="$(sanitize_state_id "$_state_id_raw")"
  _state_id="${_state_id:-$_run_name}"

  _run_started_utc="${_outdir_path}/temp/ongoing/state/${_state_id}/_state/run_started_utc.txt"
  _history="${_outdir_path}/temp/ongoing/state/${_state_id}/_state/report_history.jsonl"
  _run_dir="${_outdir_path}/report_html/runs/${_run_name}"
  _run_json="${_run_dir}/run_report.json"
  _run_index="${_outdir_path}/report_html/runs_index.jsonl"
  _run_index_lock="${_outdir_path}/.runs_index.lock"
  _root_html="${_outdir_path}/report_html/report.html"
  _root_state="${_outdir_path}/report_html/report_state.json"

  if [[ -s "$_history" || -s "$_run_json" ]]; then
    return 0
  fi

  mkdir -p "$(dirname "$_run_started_utc")" "$_run_dir" "${_outdir_path}/report_html"
  ( set -C; date -u '+%Y-%m-%dT%H:%M:%SZ' > "$_run_started_utc" ) 2>/dev/null || true
  : > "$_history"

  _barcode="$(basename "$SCRIPT_DIR")"
  if ! perl "${SCRIPT_DIR}/bin/report_run_json.pl" \
      --history "$_history" \
      --out "$_run_json" \
      --run-id "$_run_name" \
      --barcode "$_barcode" \
      --state-id "$_state_id" \
      --outdir "$_outdir" \
      --schema-version "1.6" \
      --report-rel-path "runs/${_run_name}/report.html" \
      --run-started-utc-file "$_run_started_utc"; then
    echo "WARN: failed to seed initial run status entry for '${_run_name}'." >&2
    return 0
  fi
  INITIAL_RUN_STATUS_SEEDED=1

  if ! LOCK_WAIT="${LOCK_WAIT:-300}" bash "${SCRIPT_DIR}/bin/report_run_index_update.sh" \
      "$_run_json" "$_run_index" "$_run_index_lock"; then
    echo "WARN: failed to seed initial run index entry for '${_run_name}'." >&2
    return 0
  fi

  _html_enabled="$(nextflow_arg_value "--html_report_enabled" || true)"
  _html_enabled="${_html_enabled:-true}"
  _html_enabled_norm="$(printf '%s' "$_html_enabled" | tr '[:upper:]' '[:lower:]')"
  case "$_html_enabled_norm" in
    false|0)
      return 0
      ;;
  esac

  if ! command -v python3 >/dev/null 2>&1; then
    echo "WARN: python3 not found; skipping initial root report seed for '${_run_name}'." >&2
    return 0
  fi

  _auto_refresh="$(nextflow_arg_value "--html_report_auto_refresh" || true)"
  _auto_refresh="${_auto_refresh:-true}"
  _auto_refresh="$(printf '%s' "$_auto_refresh" | tr '[:upper:]' '[:lower:]')"
  case "$_auto_refresh" in
    true|1) _auto_refresh="1" ;;
    *) _auto_refresh="0" ;;
  esac
  _refresh_seconds="$(nextflow_arg_value "--html_report_refresh_seconds" || true)"
  _refresh_seconds="${_refresh_seconds:-15}"
  _url_prefix="$(nextflow_arg_value "--html_report_url_prefix" || true)"
  _url_prefix="${_url_prefix:-}"

  if ! python3 "${SCRIPT_DIR}/bin/report_render.py" \
      --history "$_history" \
      --template "${SCRIPT_DIR}/assets/report/template.html" \
      --css "${SCRIPT_DIR}/assets/report/report.css" \
      --js "${SCRIPT_DIR}/assets/report/report.js" \
      --schema-version "1.6" \
      --state-out "$_root_state" \
      --auto-refresh-enabled "$_auto_refresh" \
      --auto-refresh-seconds "$_refresh_seconds" \
      --state-url "report_state.json" \
      --url-prefix "$_url_prefix" \
      --run-index "$_run_index" \
      --out "$_root_html"; then
    echo "WARN: failed to seed initial root report for '${_run_name}'." >&2
  fi
}

prune_seeded_run_status_if_stale() {
  local _outdir=""
  local _outdir_path=""
  local _run_name=""
  local _state_id_raw=""
  local _state_id=""
  local _history=""
  local _run_dir=""
  local _run_json=""
  local _run_index=""
  local _root_html=""
  local _root_state=""

  [[ "${INITIAL_RUN_STATUS_SEEDED:-0}" -eq 1 ]] || return 0
  [[ -n "${run_id:-}" ]] || return 0
  [[ $cleanup_modes -eq 0 && $dry_run -eq 0 && $view_only -eq 0 ]] || return 0

  _run_name="$(effective_nextflow_run_name)"
  [[ -n "$_run_name" ]] || return 0

  _outdir="$(nextflow_arg_value "--outdir" || true)"
  _outdir="${_outdir:-results}"
  _outdir_path="$(resolve_outdir_path "$_outdir")"

  _state_id_raw="$(nextflow_arg_value "--state_id" || true)"
  _state_id_raw="${_state_id_raw:-$_run_name}"
  _state_id="$(sanitize_state_id "$_state_id_raw")"
  _state_id="${_state_id:-$_run_name}"

  _history="${_outdir_path}/temp/ongoing/state/${_state_id}/_state/report_history.jsonl"
  _run_dir="${_outdir_path}/report_html/runs/${_run_name}"
  _run_json="${_run_dir}/run_report.json"
  _run_index="${_outdir_path}/report_html/runs_index.jsonl"
  _root_html="${_outdir_path}/report_html/report.html"
  _root_state="${_outdir_path}/report_html/report_state.json"

  [[ -f "$_run_json" ]] || return 0
  if [[ -s "$_history" ]]; then
    return 0
  fi

  rm -f "$_run_json" 2>/dev/null || true
  rm -rf "$_run_dir" 2>/dev/null || true

  if [[ -f "$_run_index" ]] && command -v python3 >/dev/null 2>&1; then
    local _tmp=""
    _tmp="$(mktemp "${_run_index}.tmp.XXXXXX")"
    if python3 - "${_run_name}" "$_run_index" <<'PYEOF' > "$_tmp"; then
import sys, json
rid, path = sys.argv[1], sys.argv[2]
for line in open(path, encoding="utf-8"):
    try:
        if json.loads(line).get('run_id') != rid:
            sys.stdout.write(line)
    except Exception:
        sys.stdout.write(line)
PYEOF
      mv "$_tmp" "$_run_index"
    else
      rm -f "$_tmp"
    fi
  fi

  if [[ -f "$_root_html" && -f "${SCRIPT_DIR}/assets/report/template.html" ]] && command -v python3 >/dev/null 2>&1; then
    local _render_args=(
      --history /dev/null
      --template "${SCRIPT_DIR}/assets/report/template.html"
      --css "${SCRIPT_DIR}/assets/report/report.css"
      --js "${SCRIPT_DIR}/assets/report/report.js"
      --schema-version "1.6"
      --state-out "$_root_state"
      --state-url "report_state.json"
      --out "$_root_html"
    )
    [[ -f "$_run_index" ]] && _render_args+=(--run-index "$_run_index")
    python3 "${SCRIPT_DIR}/bin/report_render.py" "${_render_args[@]}" >/dev/null 2>&1 || true
  fi
}

serve_browser_host() {
  case "$1" in
    ""|"0.0.0.0"|"::"|"[::]"|"*")
      printf '%s' "127.0.0.1"
      ;;
    *)
      printf '%s' "$1"
      ;;
  esac
}

normalize_serve_bind_host() {
  local _host="$1"
  case "$_host" in
    \[*\])
      _host="${_host#\[}"
      _host="${_host%\]}"
      ;;
  esac
  printf '%s' "$_host"
}

pick_free_serve_port() {
  local _host="$1"
  local _start="$2"
  local _end="$3"
  local _py=""
  if command -v python3 >/dev/null 2>&1; then
    _py="python3"
  elif command -v python >/dev/null 2>&1; then
    local _py_major
    _py_major="$(python -c 'import sys; print(sys.version_info[0])' 2>/dev/null || true)"
    if [[ "$_py_major" == "3" ]]; then
      _py="python"
    fi
  fi
  [[ -n "$_py" ]] || return 1

  "$_py" - "$_host" "$_start" "$_end" <<'PY'
import socket
import sys

host = sys.argv[1]
start = int(sys.argv[2])
end = int(sys.argv[3])

if ":" in host and host not in ("0.0.0.0", ""):
    family = socket.AF_INET6
else:
    family = socket.AF_INET

for port in range(start, end + 1):
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        bind_host = host
        if family == socket.AF_INET and bind_host in ("", "*"):
            bind_host = "127.0.0.1"
        elif family == socket.AF_INET6 and bind_host in ("", "*"):
            bind_host = "::1"
        sock.bind((bind_host, port))
    except OSError:
        sock.close()
        continue
    else:
        sock.close()
        print(port)
        sys.exit(0)

sys.exit(1)
PY
}

resolve_launch_path() {
  local _path="$1"
  if [[ "$_path" == /* ]]; then
    printf '%s\n' "$_path"
  else
    printf '%s\n' "${LAUNCH_DIR}/${_path}"
  fi
}

have_controlling_tty() {
  if ( : < /dev/tty ) >/dev/null 2>&1 && ( : > /dev/tty ) >/dev/null 2>&1; then
    return 0
  fi
  [[ -t 0 && -t 1 ]]
}

# ── server registry helpers ───────────────────────────────────────────────────

_RTBIOSCAN_SERVER_INDEX="${HOME}/.rtbioscan/servers.index"

# Canonical absolute path (dir must already exist)
_canonical_serve_dir() {
  ( cd "$1" && pwd -P )
}

# Append to global index + write per-dir pid file
_register_server() {    # args: pid canonical_dir
  mkdir -p "$(dirname "$_RTBIOSCAN_SERVER_INDEX")" 2>/dev/null || true
  printf '%s\t%s\n' "$1" "$2" >> "$_RTBIOSCAN_SERVER_INDEX" 2>/dev/null || true
  printf '%s\n' "$1" > "$2/server.pid"
}

# Remove entry from global index + delete per-dir pid file
_unregister_server() {  # args: pid canonical_dir
  local _tmp
  if [[ -f "$_RTBIOSCAN_SERVER_INDEX" ]]; then
    _tmp="$(mktemp)"
    awk -v pid="$1" 'BEGIN{FS="\t"} $1 != pid' \
        "$_RTBIOSCAN_SERVER_INDEX" > "$_tmp" 2>/dev/null \
      && mv "$_tmp" "$_RTBIOSCAN_SERVER_INDEX" 2>/dev/null \
      || rm -f "$_tmp"
  fi
  rm -f "$2/server.pid" 2>/dev/null || true
}

# Pre-launch cleanup: auto-stop same-dir servers, prompt for other-dir servers
_cleanup_orphaned_servers() {  # arg: canonical new serve_dir
  local _new_dir="$1"
  [[ -f "$_RTBIOSCAN_SERVER_INDEX" ]] || return 0

  local _pid _dir _i _p
  local _same_pids=() _other_pids=() _other_dirs=() _keep_pids=() _keep_dirs=()

  while IFS=$'\t' read -r _pid _dir; do
    [[ -n "$_pid" && -n "$_dir" ]] || continue
    [[ "$_pid" =~ ^[0-9]+$ ]] || continue  # skip malformed entries
    if ! kill -0 "$_pid" 2>/dev/null; then
      rm -f "$_dir/server.pid" 2>/dev/null || true
      continue  # stale entry — drop silently
    fi
    if [[ "$_dir" == "$_new_dir" ]]; then
      _same_pids+=("$_pid")
    else
      _other_pids+=("$_pid")
      _other_dirs+=("$_dir")
      _keep_pids+=("$_pid")
      _keep_dirs+=("$_dir")
    fi
  done < "$_RTBIOSCAN_SERVER_INDEX"

  # Rewrite index: live other-dir entries only
  # (same-dir will be stopped below; new entry added after successful launch)
  {
    for _i in "${!_keep_pids[@]}"; do
      printf '%s\t%s\n' "${_keep_pids[$_i]}" "${_keep_dirs[$_i]}"
    done
  } > "$_RTBIOSCAN_SERVER_INDEX"

  # Auto-stop same-dir servers (no confirmation needed)
  for _p in ${_same_pids[@]+"${_same_pids[@]}"}; do
    echo "==> [RTBioScan] Stopping existing report server for this output directory (PID ${_p}) ..."
    terminate_tracked_tree "$_p" "report server" TERM 2
    rm -f "$_new_dir/server.pid" 2>/dev/null || true
  done

  [[ ${#_other_pids[@]} -eq 0 ]] && return 0

  # Prompt for other-dir servers
  echo "==> [RTBioScan] Report servers for other output directories are running:"
  for _i in "${!_other_pids[@]}"; do
    printf '    PID %s → %s\n' "${_other_pids[$_i]}" "${_other_dirs[$_i]}"
  done

  local _resp=""
  if have_controlling_tty; then
    printf 'Stop all of the above? [y/N] ' > /dev/tty
    IFS= read -r _resp < /dev/tty 2>/dev/null || true
    printf '\n' > /dev/tty
  else
    echo "==> [RTBioScan] No controlling terminal — leaving other-directory servers running."
    return 0
  fi

  case "$_resp" in
    [Yy]|[Yy][Ee][Ss])
      for _i in "${!_other_pids[@]}"; do
        _p="${_other_pids[$_i]}"; _dir="${_other_dirs[$_i]}"
        echo "==> [RTBioScan] Stopping report server (PID ${_p}) ..."
        terminate_tracked_tree "$_p" "report server" TERM 2
        _unregister_server "$_p" "$_dir"
      done
      ;;
    *)
      echo "==> [RTBioScan] Leaving other-directory servers running."
      ;;
  esac
}

prompt_cleanup_confirmation() {
  local _resp=""

  if ! have_controlling_tty; then
    echo "ERROR: interactive cleanup confirmation requires a controlling terminal; rerun with --yes for non-interactive cleanup." >&2
    return 1
  fi

  if ( : < /dev/tty ) >/dev/null 2>&1 && ( : > /dev/tty ) >/dev/null 2>&1; then
    stty sane < /dev/tty >/dev/null 2>&1 || true
    printf "Delete the above? [y/N] " > /dev/tty
    if ! IFS= read -r _resp < /dev/tty; then
      _resp=""
    fi
    printf '\n' > /dev/tty
  else
    stty sane >/dev/null 2>&1 || true
    printf "Delete the above? [y/N] "
    if ! IFS= read -r _resp; then
      _resp=""
    fi
    printf '\n'
  fi
  [[ "$_resp" =~ ^[Yy]$ ]]
}

confirm_report_server_shutdown() {
  local _server_pid="${1:-}"
  local _resp=""

  case "${REPORT_SERVER_STOP_ON_EXIT:-}" in
    1) return 0 ;;
    0) return 1 ;;
  esac

  if ! have_controlling_tty; then
    REPORT_SERVER_STOP_ON_EXIT=1
    return 0
  fi

  while :; do
    if ( : < /dev/tty ) >/dev/null 2>&1 && ( : > /dev/tty ) >/dev/null 2>&1; then
      stty sane < /dev/tty >/dev/null 2>&1 || true
      printf "Stop report server (PID %s)? [Y/n] " "$_server_pid" > /dev/tty
      if ! IFS= read -r _resp < /dev/tty; then
        _resp=""
      fi
      printf '\n' > /dev/tty
    else
      stty sane >/dev/null 2>&1 || true
      printf "Stop report server (PID %s)? [Y/n] " "$_server_pid"
      if ! IFS= read -r _resp; then
        _resp=""
      fi
      printf '\n'
    fi

    case "$_resp" in
      ""|[Yy]|[Yy][Ee][Ss])
        REPORT_SERVER_STOP_ON_EXIT=1
        return 0
        ;;
      [Nn]|[Nn][Oo])
        REPORT_SERVER_STOP_ON_EXIT=0
        return 1
        ;;
    esac
  done
}

validate_explicit_targets_or_die() {
  if [[ "$targets" =~ ^[[:space:]]*$ ]]; then
    echo "ERROR: explicit wrapper-level --targets must not be empty when using RTBioScan.sh." >&2
    exit 1
  fi
  if ! targets_match_grammar "$targets"; then
    echo "ERROR: explicit wrapper-level --targets contains invalid marker names; targets must be pipe-separated tokens without whitespace." >&2
    exit 1
  fi
}

declare -a NEXTFLOW_GLOBAL_ARGS=()
declare -a NEXTFLOW_CONFIG_CMD_ARGS=()
declare -a NEXTFLOW_RUN_ARGS=()
declare -a NORMALIZED_NEXTFLOW_RUN_CMD=()

partition_nextflow_args() {
  local _strict_config_resolution="$1"
  NEXTFLOW_GLOBAL_ARGS=()
  NEXTFLOW_CONFIG_CMD_ARGS=()
  NEXTFLOW_RUN_ARGS=()
  local _i _opt _value
  for (( _i=0; _i<${#nf_args[@]}; _i++ )); do
    _opt="${nf_args[$_i]}"
    case "$_opt" in
      -c|-config|-C|-profile)
        if (( _i + 1 >= ${#nf_args[@]} )); then
          die_missing_config_selector_value "$_opt"
        fi
        _value="${nf_args[$((_i+1))]}"
        if [[ "$_value" == -* ]]; then
          die_missing_config_selector_value "$_opt"
        fi
        if [[ "$_opt" == "-profile" ]]; then
          NEXTFLOW_CONFIG_CMD_ARGS+=("$_opt" "$_value")
          NEXTFLOW_RUN_ARGS+=("$_opt" "$_value")
        else
          NEXTFLOW_GLOBAL_ARGS+=("$_opt" "$_value")
        fi
        _i=$((_i+1))
        ;;
      -config-ignore-includes)
        NEXTFLOW_GLOBAL_ARGS+=("$_opt")
        ;;
      -profile=*)
        if [[ "$_strict_config_resolution" -eq 1 ]]; then
          die_attached_config_selector "-profile"
        fi
        NEXTFLOW_RUN_ARGS+=("$_opt")
        ;;
      -c=*)
        if [[ "$_strict_config_resolution" -eq 1 ]]; then
          die_attached_config_selector "-c"
        fi
        NEXTFLOW_RUN_ARGS+=("$_opt")
        ;;
      -config=*)
        if [[ "$_strict_config_resolution" -eq 1 ]]; then
          die_attached_config_selector "-config"
        fi
        NEXTFLOW_RUN_ARGS+=("$_opt")
        ;;
      -C=*)
        if [[ "$_strict_config_resolution" -eq 1 ]]; then
          die_attached_config_selector "-C"
        fi
        NEXTFLOW_RUN_ARGS+=("$_opt")
        ;;
      -config-ignore-includes=*)
        if [[ "$_strict_config_resolution" -eq 1 ]]; then
          die_attached_config_selector "-config-ignore-includes"
        fi
        NEXTFLOW_RUN_ARGS+=("$_opt")
        ;;
      *)
        NEXTFLOW_RUN_ARGS+=("$_opt")
        ;;
    esac
  done
}

build_normalized_nextflow_run_cmd() {
  NORMALIZED_NEXTFLOW_RUN_CMD=(nextflow)
  partition_nextflow_args 0
  if [[ ${#NEXTFLOW_GLOBAL_ARGS[@]} -gt 0 ]]; then
    NORMALIZED_NEXTFLOW_RUN_CMD+=("${NEXTFLOW_GLOBAL_ARGS[@]}")
  fi
  NORMALIZED_NEXTFLOW_RUN_CMD+=(run main.nf)
  if [[ ${#NEXTFLOW_RUN_ARGS[@]} -gt 0 ]]; then
    NORMALIZED_NEXTFLOW_RUN_CMD+=("${NEXTFLOW_RUN_ARGS[@]}")
  fi
}

resolve_targets_from_config_or_die() {
  local _config_output=""
  local _line=""
  local _rhs=""
  local _resolved=""
  local _config_cmd=(nextflow)

  partition_nextflow_args 1
  if [[ ${#NEXTFLOW_GLOBAL_ARGS[@]} -gt 0 ]]; then
    _config_cmd+=("${NEXTFLOW_GLOBAL_ARGS[@]}")
  fi
  _config_cmd+=(config -flat)
  if [[ ${#NEXTFLOW_CONFIG_CMD_ARGS[@]} -gt 0 ]]; then
    _config_cmd+=("${NEXTFLOW_CONFIG_CMD_ARGS[@]}")
  fi
  _config_cmd+=(main.nf)

  if ! _config_output="$(
    cd "$SCRIPT_DIR"
    "${_config_cmd[@]}" 2>&1
  )"; then
    echo "ERROR: failed to resolve params.targets via 'nextflow config -flat'; check profile/config arguments and config files." >&2
    exit 1
  fi

  while IFS= read -r _line; do
    case "$_line" in
      params.targets\ =\ *)
        _rhs="${_line#params.targets = }"
        _resolved="$(strip_matching_outer_quotes "$_rhs")"
        break
        ;;
    esac
  done <<< "$_config_output"

  if [[ -z "$_resolved" ]]; then
    echo "ERROR: unable to resolve params.targets from CLI --targets or the effective Nextflow config stack." >&2
    exit 1
  fi
  if ! targets_match_grammar "$_resolved"; then
    echo "ERROR: resolved params.targets from the effective Nextflow config stack is invalid; targets must be pipe-separated tokens without whitespace." >&2
    exit 1
  fi
  resolved_targets="$_resolved"
}

# ── parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --feeder)        feeder=1 ;;
    --run_id)        require_wrapper_option_value "$1" "${2-}"; shift; run_id="$1" ;;
    --input_folder)  require_wrapper_option_value "$1" "${2-}"; shift; input_folder="$1" ;;
    --num_reads)     require_wrapper_option_value "$1" "${2-}"; shift; num_reads="$1" ;;
    --sleep_time)    require_wrapper_option_value "$1" "${2-}"; shift; sleep_time="$1" ;;
    --metadata)      require_wrapper_option_value "$1" "${2-}"; shift; metadata_file="$1" ;;
    --general_fasta) require_wrapper_option_value "$1" "${2-}"; shift; general_fasta="$1" ;;
    --primers_fasta) require_wrapper_option_value "$1" "${2-}"; shift; primers_fasta="$1" ;;
    --targets)       require_wrapper_option_value "$1" "${2-}"; shift; targets="$1"; targets_explicit=1; nf_args+=("--targets" "$targets") ;;
    --do_metadata)          do_metadata=1 ;;
    --skip_pod5)            skip_pod5=1 ;;
    --delete-full-pod5)     delete_full_pod5=1 ;;
    --delete_input_pod5)    delete_input_pod5=1 ;;
    --delete_from_ori_dir)  delete_from_ori_dir=1 ;;
    --serve)            serve=1 ;;
    --view)             view_only=1; serve=1 ;;
    --serve-port)       require_wrapper_option_value "$1" "${2-}"; shift; serve_port="$1"; serve_port_explicit=1 ;;
    --serve-host)       require_wrapper_option_value "$1" "${2-}"; shift; serve_host="$1" ;;
    --serve-open)       serve_open=1 ;;
    --serve-open-all)   serve_open_all=1 ;;
    --serve-open-last)  require_wrapper_option_value "$1" "${2-}"; shift; serve_open_last="$1" ;;
    --serve-quiet)      serve_quiet=1 ;;
    --serve-dir)        require_wrapper_option_value "$1" "${2-}"; shift; serve_dir="$1" ;;
    --clean-ref)        require_wrapper_option_value "$1" "${2-}"; shift; clean_ref="$1" ;;
    --clean)            require_wrapper_option_value "$1" "${2-}"; shift; clean_run_id="$1" ;;
    --clean-all)        clean_all=1 ;;
    --clean-temp)       require_wrapper_option_value "$1" "${2-}"; shift; clean_temp_run_id="$1" ;;
    --clean-temp-all)   clean_temp_all=1 ;;
    --dry-run)          dry_run=1 ;;
    -y|--yes)           yes=1 ;;
    -h|--help)          usage ;;
    *)                  nf_args+=("$1") ;;
  esac
  shift
done

if [[ -n "$serve_port" ]] && ! [[ "$serve_port" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --serve-port must be a positive integer, got: $serve_port" >&2
  exit 1
fi
if [[ -n "$serve_open_last" ]] && ! [[ "$serve_open_last" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --serve-open-last must be a positive integer, got: $serve_open_last" >&2
  exit 1
fi

if [[ -n "$clean_ref" && ! -d "$clean_ref" ]]; then
  echo "ERROR: --clean-ref directory does not exist: $clean_ref" >&2
  exit 1
fi
if [[ -n "$clean_ref" ]]; then
  clean_ref="$(cd "$clean_ref" && pwd -P)"
fi

[[ -n "$input_folder" ]] && input_folder="$(resolve_launch_path "$input_folder")"
[[ -n "$metadata_file" ]] && metadata_file="$(resolve_launch_path "$metadata_file")"
[[ -n "$general_fasta" ]] && general_fasta="$(resolve_launch_path "$general_fasta")"
[[ -n "$primers_fasta" ]] && primers_fasta="$(resolve_launch_path "$primers_fasta")"

cleanup_modes=0
[[ -n "$clean_run_id" ]] && cleanup_modes=$((cleanup_modes + 1))
[[ $clean_all -eq 1 ]] && cleanup_modes=$((cleanup_modes + 1))
[[ -n "$clean_temp_run_id" ]] && cleanup_modes=$((cleanup_modes + 1))
[[ $clean_temp_all -eq 1 ]] && cleanup_modes=$((cleanup_modes + 1))
if [[ $cleanup_modes -gt 1 ]]; then
  echo "ERROR: choose only one cleanup mode: --clean, --clean-all, --clean-temp, or --clean-temp-all" >&2
  exit 1
fi

if [[ $view_only -eq 1 ]]; then
  if [[ $feeder -eq 1 || $do_metadata -eq 1 ]]; then
    echo "ERROR: --view cannot be combined with --feeder or --do_metadata." >&2
    exit 1
  fi
  if [[ ${#nf_args[@]} -gt 0 ]]; then
    echo "ERROR: --view cannot be combined with pipeline arguments (e.g. -profile, -resume, --run_id)." >&2
    exit 1
  fi
fi

declare -a CLEANUP_SEARCH_ROOTS=()

append_cleanup_search_root() {
  local _root="$1"
  local _resolved=""
  local _existing=""
  [[ -n "$_root" ]] || return 0
  _resolved="$(cd "$_root" 2>/dev/null && pwd -P)" || return 0
  if [[ ${#CLEANUP_SEARCH_ROOTS[@]} -gt 0 ]]; then
    for _existing in "${CLEANUP_SEARCH_ROOTS[@]}"; do
      [[ "$_existing" == "$_resolved" ]] && return 0
    done
  fi
  CLEANUP_SEARCH_ROOTS+=("$_resolved")
}

init_cleanup_search_roots() {
  CLEANUP_SEARCH_ROOTS=()
  if [[ -n "$clean_ref" ]]; then
    append_cleanup_search_root "$clean_ref"
  else
    append_cleanup_search_root "$LAUNCH_DIR"
    append_cleanup_search_root "$SCRIPT_DIR"
  fi
}

history_contains_run() {
  local _root="$1"
  local _run_id="$2"
  local _hist="${_root}/.nextflow/history"
  [[ -n "$_run_id" && -s "$_hist" ]] || return 1
  awk -F'\t' -v rid="${_run_id}" '$3 == rid { found=1; exit } END { exit(found ? 0 : 1) }' "$_hist"
}

collect_nextflow_cache_targets_for_run() {
  local _root="$1"
  local _run_id="$2"
  local _hist="${_root}/.nextflow/history"
  local _cache_root="${_root}/.nextflow/cache"
  local _session=""
  local _cache_dir=""

  [[ -n "$_run_id" && -s "$_hist" && -d "$_cache_root" ]] || return 0
  awk -F'\t' -v rid="${_run_id}" '$3 == rid && $6 != "" { print $6 }' "$_hist" \
    | while IFS= read -r _session; do
        case "$_session" in
          ''|*/*|*'..'*)
            continue
            ;;
        esac
        _cache_dir="${_cache_root}/${_session}"
        [[ -e "$_cache_dir" ]] && printf '%s\n' "$_cache_dir"
      done
}

nextflow_cache_locks_have_holders() {
  local _cache_dir=""
  local _lock_file=""
  local _saw_lock=0

  command -v lsof >/dev/null 2>&1 || return 2
  for _cache_dir in "$@"; do
    _lock_file="${_cache_dir}/db/LOCK"
    [[ -e "$_lock_file" ]] || continue
    _saw_lock=1
    if lsof "$_lock_file" >/dev/null 2>&1; then
      return 0
    fi
  done
  [[ $_saw_lock -eq 1 ]] || return 1
  return 1
}

feeder_lock_has_live_holder_for_root() {
  local _root="$1"
  local _run_id="$2"
  local _lock_meta="${_root}/results/pod5/${_run_id}/metadata/.feeder.lockdir/meta.env"
  local _lock_pid=""

  [[ -f "$_lock_meta" ]] || return 1
  _lock_pid="$(awk -F= '/^pid=/{print $2; exit}' "$_lock_meta" 2>/dev/null || true)"
  [[ "$_lock_pid" =~ ^[0-9]+$ ]] || return 1
  pid_is_live_non_zombie "$_lock_pid"
}

lookup_state_id_for_run() {
  local _root="$1"
  local _run_id="$2"
  local _run_index="${_root}/results/report_html/runs_index.jsonl"
  if [[ -z "$_run_id" || ! -s "$_run_index" ]]; then
    printf '%s\n' "$_run_id"
    return 0
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' "$_run_id"
    return 0
  fi
  python3 - "$_run_index" "$_run_id" <<'PYEOF'
import json
import sys
from pathlib import Path

run_index = Path(sys.argv[1])
run_id = sys.argv[2]
state_id = run_id
for raw in run_index.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    if obj.get("run_id") == run_id:
        state_id = obj.get("state_id") or run_id
        break
print(state_id)
PYEOF
}

root_has_run_artifacts() {
  local _root="$1"
  local _run_id="$2"
  local _state_id=""
  local _p=""
  _state_id="$(lookup_state_id_for_run "$_root" "$_run_id")"
	  for _p in \
      "${_root}/results/report_html/runs/${_run_id}" \
      "${_root}/results/runs/${_run_id}" \
      "${_root}/results/config/${_run_id}" \
      "${_root}/results/current/state/${_state_id}" \
	      "${_root}/results/temp/ongoing/state/${_state_id}" \
      "${_root}/results/temp/current/state/${_state_id}" \
      "${_root}/results/ongoing/state/${_state_id}" \
      "${_root}/results/pod5/${_run_id}" \
      "${_root}/results/sample_info/${_run_id}"; do
    [[ -e "$_p" ]] && return 0
  done
  return 1
}

resolve_cleanup_root_for_run() {
  local _run_id="$1"
  local _root=""
  for _root in "${CLEANUP_SEARCH_ROOTS[@]}"; do
    if history_contains_run "$_root" "$_run_id"; then
      printf '%s\n' "$_root"
      return 0
    fi
  done
  for _root in "${CLEANUP_SEARCH_ROOTS[@]}"; do
    if root_has_run_artifacts "$_root" "$_run_id"; then
      printf '%s\n' "$_root"
      return 0
    fi
  done
  return 1
}

# ── clean mode ────────────────────────────────────────────────────────────────
if [[ -n "$clean_run_id" || $clean_all -eq 1 || -n "$clean_temp_run_id" || $clean_temp_all -eq 1 ]]; then
  init_cleanup_search_roots
  _target_root=""
  if [[ $clean_all -eq 1 || $clean_temp_all -eq 1 ]]; then
    _target_root="${clean_ref:-$LAUNCH_DIR}"
  else
    _target_run_id="${clean_run_id:-$clean_temp_run_id}"
    if ! _target_root="$(resolve_cleanup_root_for_run "$_target_run_id")"; then
      echo "ERROR: unable to locate cleanup root for run '${_target_run_id}'." >&2
      echo "       Searched:" >&2
      for _root in "${CLEANUP_SEARCH_ROOTS[@]}"; do
        echo "         $_root" >&2
      done
      echo "       Use --clean-ref <dir> to point at the launch directory that contains the run artifacts." >&2
      exit 1
    fi
  fi

  _outdir="${_target_root}/results"
  _rm_targets=()
  _filter_id=""
  _state_id=""
  _work_dir=""   # work/ contents are cleared (dir kept); handled separately
  _nextflow_bin="${SCRIPT_DIR}/nextflow"
  if [[ ! -x "$_nextflow_bin" ]]; then
    _nextflow_bin="$(command -v nextflow 2>/dev/null || true)"
  fi
  _nextflow_clean_args=()
  _nextflow_preview_args=()
  _nextflow_cache_targets=()

  if [[ -z "$_nextflow_bin" || ! -x "$_nextflow_bin" ]] && [[ -n "$clean_run_id" || -n "$clean_temp_run_id" ]]; then
    echo "ERROR: nextflow executable not found at ${SCRIPT_DIR}/nextflow and not in PATH; run-specific cleanup requires it for work/ cleanup" >&2
    exit 1
  fi

  if [[ $clean_all -eq 1 ]]; then
    echo "==> [RTBioScan] Clean-all: staging removal of all run artifacts under '${_target_root}' ..."
    for _p in \
        "$_outdir/report_html" \
        "$_outdir/config" \
        "$_outdir/current" \
        "$_outdir/temp" \
        "$_outdir/pod5" \
        "$_outdir/sample_info" \
        "$_outdir/feeder.log" \
        "$_outdir/server.log" \
        "$_outdir/ongoing" \
        "${_target_root}/.nextflow"; do
      [[ -e "$_p" ]] && _rm_targets+=("$_p")
    done
    for _f in "${_target_root}"/.nextflow.log*; do
      [[ -f "$_f" ]] && _rm_targets+=("$_f")
    done
    [[ -d "${_target_root}/work" ]] && _work_dir="${_target_root}/work"
  elif [[ $clean_temp_all -eq 1 ]]; then
    echo "==> [RTBioScan] Clean-temp-all: staging removal of temporary artifacts for all runs under '${_target_root}' ..."
    for _p in \
        "$_outdir/temp" \
        "$_outdir/ongoing"; do
      [[ -e "$_p" ]] && _rm_targets+=("$_p")
    done
    [[ -d "${_target_root}/work" ]] && _work_dir="${_target_root}/work"
  else
    if [[ -n "$clean_run_id" ]]; then
      _state_id="$(lookup_state_id_for_run "$_target_root" "$clean_run_id")"
      echo "==> [RTBioScan] Clean run: staging removal of artifacts for run_id='${clean_run_id}' in '${_target_root}' (state_id='${_state_id}') ..."
      _filter_id="$clean_run_id"
      for _p in \
	          "$_outdir/report_html/runs/${clean_run_id}" \
	          "$_outdir/runs/${clean_run_id}" \
	          "$_outdir/config/${clean_run_id}" \
          "$_outdir/current/state/${_state_id}" \
          "$_outdir/temp/ongoing/state/${_state_id}" \
          "$_outdir/temp/current/state/${_state_id}" \
          "$_outdir/ongoing/state/${_state_id}" \
          "$_outdir/pod5/${clean_run_id}" \
          "$_outdir/sample_info/${clean_run_id}"; do
        [[ -e "$_p" ]] && _rm_targets+=("$_p")
      done
      while IFS= read -r _p; do
        [[ -n "$_p" ]] && _nextflow_cache_targets+=("$_p")
      done < <(collect_nextflow_cache_targets_for_run "$_target_root" "$clean_run_id")
      _nextflow_clean_args=(-f -k "$clean_run_id")
      _nextflow_preview_args=(-n -k "$clean_run_id")
    else
      _state_id="$(lookup_state_id_for_run "$_target_root" "$clean_temp_run_id")"
      echo "==> [RTBioScan] Clean-temp run: staging removal of temporary artifacts for run_id='${clean_temp_run_id}' in '${_target_root}' (state_id='${_state_id}') ..."
      for _p in \
          "$_outdir/temp/ongoing/state/${_state_id}" \
          "$_outdir/temp/current/state/${_state_id}" \
          "$_outdir/ongoing/state/${_state_id}"; do
        [[ -e "$_p" ]] && _rm_targets+=("$_p")
      done
      _nextflow_clean_args=(-f -k "$clean_temp_run_id")
      _nextflow_preview_args=(-n -k "$clean_temp_run_id")
    fi
  fi

  # Print plan
  if [[ ${#_rm_targets[@]} -gt 0 ]]; then
    echo "    Paths to delete:"
    for _p in ${_rm_targets[@]+"${_rm_targets[@]}"}; do echo "      rm -rf  $_p"; done
  fi
  if [[ -n "$_work_dir" ]]; then
    echo "      clear   ${_work_dir}/*  (directory kept)"
  fi
  if [[ ${#_nextflow_preview_args[@]} -gt 0 ]]; then
    echo "      nextflow ${_nextflow_preview_args[*]}"
  fi
  if [[ ${#_nextflow_cache_targets[@]} -gt 0 ]]; then
    for _p in ${_nextflow_cache_targets[@]+"${_nextflow_cache_targets[@]}"}; do
      echo "      rm -rf  $_p  (Nextflow cache/locks)"
    done
  fi
  if [[ -n "$_filter_id" ]]; then
    [[ -f "${_target_root}/.nextflow/history" ]] && \
      echo "      filter  ${_target_root}/.nextflow/history  (remove '${_filter_id}' entries)"
    [[ -f "$_outdir/report_html/runs_index.jsonl" ]] && \
      echo "      filter  $_outdir/report_html/runs_index.jsonl  (remove '${_filter_id}' entries)"
  fi
  echo

  if [[ ${#_rm_targets[@]} -eq 0 && -z "$_work_dir" && -z "$_filter_id" && ${#_nextflow_preview_args[@]} -eq 0 ]]; then
    echo "    Nothing to remove."
    exit 0
  fi

  if [[ ${#_nextflow_preview_args[@]} -gt 0 ]]; then
    echo "    Nextflow temp cleanup preview:"
    if ! (
      cd "$_target_root"
      "$_nextflow_bin" clean "${_nextflow_preview_args[@]}"
    ); then
      echo "WARN: failed to preview Nextflow work/ temp cleanup. The target run may still be active or locked." >&2
    fi
    echo
  fi

  if [[ $dry_run -eq 1 ]]; then
    echo "==> [RTBioScan] Dry run — nothing deleted."
    exit 0
  fi

  if [[ $yes -eq 0 ]]; then
    if ! prompt_cleanup_confirmation; then
      if have_controlling_tty; then
        echo "Aborted."
        exit 0
      fi
      exit 1
    fi
  fi

  if [[ -n "$clean_run_id" ]]; then
    stop_clean_run_feeder_if_active_or_die "$_target_root" "$clean_run_id"
  fi

  if [[ ${#_nextflow_clean_args[@]} -gt 0 ]]; then
    if ! (
      cd "$_target_root"
      "$_nextflow_bin" clean "${_nextflow_clean_args[@]}"
    ); then
      if [[ -n "$clean_run_id" && ${#_nextflow_cache_targets[@]} -gt 0 ]]; then
        set +e
        nextflow_cache_locks_have_holders "${_nextflow_cache_targets[@]}"
        _cache_lock_holder_status=$?
        set -e
        if [[ "$_cache_lock_holder_status" -eq 0 ]]; then
          echo "ERROR: Nextflow work/ temp cleanup failed and a cache LOCK is still held by a running process." >&2
          echo "       Stop the active run before using --clean, then retry." >&2
          exit 1
        fi
        if [[ "$_cache_lock_holder_status" -eq 2 ]]; then
          echo "ERROR: Nextflow work/ temp cleanup failed and lsof is unavailable to verify stale cache locks." >&2
          echo "       Install lsof or remove stale .nextflow/cache/<session>/db/LOCK only after confirming no run is active." >&2
          exit 1
        fi
        echo "WARN: Nextflow work/ temp cleanup failed; removing run cache/locks so the run name can be reused." >&2
        echo "WARN: Some work/ temp files may remain; use --clean-temp or --clean-all later if disk cleanup is needed." >&2
      else
        echo "ERROR: Nextflow work/ temp cleanup failed. Stop active runs or clear the lock, then retry." >&2
        exit 1
      fi
    fi
    if [[ $clean_temp_all -eq 1 ]]; then
      echo "    Cleaned: work/ temp files for all Nextflow runs"
    elif [[ -n "$clean_temp_run_id" ]]; then
      echo "    Cleaned: work/ temp files for Nextflow run '${clean_temp_run_id}'"
    fi
  fi

  for _p in ${_rm_targets[@]+"${_rm_targets[@]}"}; do
    rm -rf "$_p"
    echo "    Removed: $_p"
  done
  if [[ -n "$_work_dir" ]]; then
    find "$_work_dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    echo "    Cleared: ${_work_dir}/*"
  fi
  for _p in ${_nextflow_cache_targets[@]+"${_nextflow_cache_targets[@]}"}; do
    rm -rf "$_p"
    echo "    Removed: $_p"
  done

  if [[ -n "$_filter_id" ]]; then
    _hist="${_target_root}/.nextflow/history"
    if [[ -f "$_hist" ]]; then
      _tmp="$(mktemp)"
      if awk -F'\t' -v rid="${_filter_id}" '$3 != rid' "$_hist" > "$_tmp" && mv "$_tmp" "$_hist"; then
        echo "    Filtered: $_hist"
      else
        rm -f "$_tmp"
        echo "WARN: failed to filter $_hist after clean" >&2
      fi
    fi
    _idx="$_outdir/report_html/runs_index.jsonl"
    if [[ -f "$_idx" ]]; then
      if command -v python3 >/dev/null 2>&1; then
        _tmp="$(mktemp)"
        if python3 - "${_filter_id}" "$_idx" <<'PYEOF' > "$_tmp" && mv "$_tmp" "$_idx"; then
import sys, json
rid, path = sys.argv[1], sys.argv[2]
for line in open(path, encoding="utf-8"):
    try:
        if json.loads(line).get('run_id') != rid:
            sys.stdout.write(line)
    except Exception:
        sys.stdout.write(line)
PYEOF
          echo "    Filtered: $_idx"
        else
          rm -f "$_tmp"
          echo "WARN: failed to filter $_idx after clean" >&2
        fi
      else
        echo "WARN: python3 not found; leaving $_idx unchanged after clean" >&2
      fi
    fi
    # Regenerate the top-level report.html so the cleaned run's broken link is removed.
    _report_html="$_outdir/report_html/report.html"
    _report_state="$_outdir/report_html/report_state.json"
    _tmpl="${SCRIPT_DIR}/assets/report/template.html"
    if [[ -f "$_report_html" ]] && [[ -f "$_tmpl" ]] && command -v python3 >/dev/null 2>&1; then
      _render_args=(
        --history /dev/null
        --template "$_tmpl"
        --css "${SCRIPT_DIR}/assets/report/report.css"
        --js "${SCRIPT_DIR}/assets/report/report.js"
        --schema-version "1.4"
        --state-out "$_report_state"
        --state-url "report_state.json"
        --out "$_report_html"
      )
      [[ -f "$_idx" ]] && _render_args+=(--run-index "$_idx")
      if python3 "${SCRIPT_DIR}/bin/report_render.py" "${_render_args[@]}"; then
        echo "    Regenerated: $_report_html"
      else
        echo "WARN: failed to regenerate $_report_html after clean" >&2
      fi
    fi
  fi

  echo "==> [RTBioScan] Clean complete."
  exit 0
fi

# ── validate ──────────────────────────────────────────────────────────────────
if [[ $feeder -eq 1 || $do_metadata -eq 1 ]]; then
  if [[ -z "$run_id" ]]; then
    echo "ERROR: --run_id is required with --feeder / --do_metadata" >&2
    exit 1
  fi
fi
if [[ $feeder -eq 1 && $skip_pod5 -eq 0 && -z "$input_folder" ]]; then
  echo "ERROR: --feeder requires --input_folder (or --skip_pod5 to skip POD5 splitting)" >&2
  exit 1
fi
if [[ $feeder -eq 1 && $skip_pod5 -eq 0 && -n "$input_folder" && ! -d "$input_folder" ]]; then
  echo "ERROR: --input_folder directory does not exist: $input_folder" >&2
  exit 1
fi
if [[ $targets_explicit -eq 1 ]]; then
  validate_explicit_targets_or_die
  resolved_targets="$targets"
fi
if [[ $do_metadata -eq 1 || $feeder -eq 1 ]]; then
  if [[ $targets_explicit -eq 0 ]]; then
    resolve_targets_from_config_or_die
  fi
fi

# Auto-inject -name and pipeline path params derived from --run_id
if [[ -n "$run_id" && $view_only -eq 0 ]]; then
  name_present=0; reads_set=0; ori_dir_set=0; indexes_set=0; primer_indexes_set=0; watch_set=0
  nextflow_arg_present "-name" && name_present=1
  nextflow_arg_present "--reads" && reads_set=1
  nextflow_arg_present "--ori_dir" && ori_dir_set=1
  nextflow_arg_present "--indexes" && indexes_set=1
  nextflow_arg_present "--primer_indexes" && primer_indexes_set=1
  nextflow_arg_present "--watch" && watch_set=1
  [[ $name_present      -eq 0 ]] && nf_args+=(-name "$run_id")
  nf_args+=(--run_id "$run_id")
  [[ $reads_set          -eq 0 ]] && nf_args+=(--reads          "results/pod5/$run_id/reads_rt_round_pod5/*pod5")
  [[ $ori_dir_set        -eq 0 ]] && nf_args+=(--ori_dir        "results/pod5/$run_id/ori_round_pod5/")
  [[ $indexes_set        -eq 0 ]] && nf_args+=(--indexes        "results/sample_info/$run_id/demult.fasta")
  if [[ $primer_indexes_set -eq 0 ]]; then
    if [[ -n "$primers_fasta" ]]; then
      nf_args+=(--primer_indexes "$primers_fasta")
    else
      nf_args+=(--primer_indexes "results/sample_info/$run_id/primers.fasta")
    fi
  fi
  # Live feeder runs need Nextflow watching enabled even if a profile defaults to watch=false.
  # Respect an explicit user override, but otherwise force watch mode so the pipeline waits for
  # the first POD5 instead of exiting on an initially empty realtime glob.
  [[ $feeder -eq 1 && $watch_set -eq 0 ]] && nf_args+=(--watch true)
fi

# Inject boolean delete flags as Nextflow params.
if [[ $view_only -eq 0 ]]; then
  [[ $delete_input_pod5   -eq 1 ]] && nf_args+=(--delete_input_pod5   true)
  [[ $delete_from_ori_dir -eq 1 ]] && nf_args+=(--delete_from_ori_dir true)
fi

# Ensure the realtime feeder tree exists before Nextflow validates --reads.
# This avoids a startup race where the feeder is still initializing.
if [[ $feeder -eq 1 && -n "$run_id" ]]; then
  mkdir -p \
    "${SCRIPT_DIR}/results/pod5/$run_id/reads_rt_round_pod5" \
    "${SCRIPT_DIR}/results/pod5/$run_id/ori_round_pod5" \
    "${SCRIPT_DIR}/results/pod5/$run_id/full_pod5" \
    "${SCRIPT_DIR}/results/pod5/$run_id/done_round_pod5" \
    "${SCRIPT_DIR}/results/pod5/$run_id/metadata"
fi

if [[ -n "$run_id" && $cleanup_modes -eq 0 && $dry_run -eq 0 && $view_only -eq 0 ]]; then
  preflight_feeder_lock_or_die "$run_id"
  if [[ $feeder -eq 1 ]]; then
    preflight_orphan_feeders_for_run "$run_id"
  fi
  preflight_run_name_or_die "$(effective_nextflow_run_name)"
fi

# Derive serve-dir from --outdir in nextflow args if not set explicitly
if [[ $serve -eq 1 && -z "$serve_dir" ]]; then
  serve_dir="$(nextflow_arg_value "--outdir" || true)"
  serve_dir="${serve_dir:-results}"
fi

# ── metadata setup (synchronous — completes before pipeline starts) ───────────
if [[ $do_metadata -eq 1 ]]; then
  echo "==> [RTBioScan] Setting up results/sample_info/$run_id/ for run '$run_id' ..."
  meta_args=(--run_id "$run_id" --do_metadata --skip_pod5)
  [[ -n "$input_folder" ]]   && meta_args+=(--input_folder  "$input_folder")
  [[ -n "$num_reads" ]]      && meta_args+=(--num_reads     "$num_reads")
  [[ -n "$sleep_time" ]]     && meta_args+=(--sleep_time    "$sleep_time")
  [[ -n "$metadata_file" ]]  && meta_args+=(--metadata      "$metadata_file")
  [[ -n "$general_fasta" ]]  && meta_args+=(--general_fasta "$general_fasta")
  [[ -n "$primers_fasta" ]]  && meta_args+=(--primers_fasta "$primers_fasta")
  [[ -n "$resolved_targets" ]] && meta_args+=(--targets "$resolved_targets")
  (
    cd "$SCRIPT_DIR"
    # Wrapper metadata setup must remain compatible with duplicate marker fixtures
    # used by the launcher tests; the feeder still defaults to strict mode when run directly.
    RTBIOSCAN_TRACK_IDENTITY_STRICT="${RTBIOSCAN_TRACK_IDENTITY_STRICT:-0}" \
    RTBIOSCAN_TRACK_ARTIFACTS_OPTIONAL="${RTBIOSCAN_TRACK_ARTIFACTS_OPTIONAL:-1}" \
      bash "$FEEDER" "${meta_args[@]}"
  )
  echo "==> [RTBioScan] Metadata setup complete."
  echo
fi

# ── cleanup handlers (run on exit and wrapper signals) ───────────────────────
FEEDER_PID=""
NEXTFLOW_PID=""
SERVER_PID=""
serve_dir_canonical=""
CLEANUP_DONE=0
CLEANUP_IN_PROGRESS=0
PIPELINE_ESCALATED=0
SHUTDOWN_SIGNAL=""
SHUTDOWN_EXIT_CODE=""
LAUNCH_IN_PROGRESS=0
PENDING_SIGNAL_NAME=""
PENDING_SIGNAL_EXIT_CODE=""
REPORT_SERVER_STOP_ON_EXIT=""

process_is_alive() {
  local _pid="${1:-}"
  [[ -n "$_pid" ]] && kill -0 "$_pid" 2>/dev/null
}

process_is_zombie() {
  local _pid="${1:-}"
  local _stat=""
  [[ -n "$_pid" ]] || return 1

  _stat="$(ps -o stat= -p "$_pid" 2>/dev/null | awk 'NR==1 { gsub(/^[[:space:]]+|[[:space:]]+$/, "", $0); print; exit }')"
  [[ "$_stat" == Z* ]]
}

wait_for_tracked_pid_exit() {
  local _pid="${1:-}"
  [[ -n "$_pid" ]] || return 0

  while process_is_alive "$_pid"; do
    if process_is_zombie "$_pid"; then
      break
    fi
    sleep 1
  done
  wait "$_pid" 2>/dev/null || true
}

list_direct_child_pids() {
  local _pid="${1:-}"
  local _pgrep_output=""
  local _pgrep_status=0
  [[ "$_pid" =~ ^[0-9]+$ ]] || return 0

  if command -v pgrep >/dev/null 2>&1; then
    _pgrep_output="$(pgrep -P "$_pid" 2>/dev/null)"
    _pgrep_status=$?
    case "$_pgrep_status" in
      0)
        [[ -n "$_pgrep_output" ]] && printf '%s\n' "$_pgrep_output"
        return 0
        ;;
      1)
        return 0
        ;;
    esac
  fi

  ps -ax -o pid= -o ppid= 2>/dev/null \
    | awk -v target="$_pid" '$2 == target { gsub(/^[[:space:]]+|[[:space:]]+$/, "", $1); print $1 }'
}

list_descendant_pids_postorder() {
  local _pid="${1:-}"
  local _child
  [[ "$_pid" =~ ^[0-9]+$ ]] || return 0

  while IFS= read -r _child; do
    [[ -n "$_child" ]] || continue
    list_descendant_pids_postorder "$_child"
    printf '%s\n' "$_child"
  done < <(list_direct_child_pids "$_pid")
}

pid_list_has_live_processes() {
  local _pid_list="${1:-}"
  local _pid

  while IFS= read -r _pid; do
    [[ -n "$_pid" ]] || continue
    if process_is_alive "$_pid"; then
      return 0
    fi
  done <<< "$_pid_list"

  return 1
}

signal_pid_list() {
  local _signal="${1:-TERM}"
  local _pid_list="${2:-}"
  local _pid

  while IFS= read -r _pid; do
    [[ -n "$_pid" ]] || continue
    kill -s "$_signal" "$_pid" 2>/dev/null || true
  done <<< "$_pid_list"
}

signal_tracked_tree() {
  local _pid="${1:-}"
  local _signal="${2:-TERM}"
  local _descendants=""

  [[ -n "$_pid" ]] || return 0
  [[ "$_pid" =~ ^[0-9]+$ ]] || return 0

  _descendants="$(list_descendant_pids_postorder "$_pid")"
  signal_pid_list "$_signal" "$_descendants"
  kill -s "$_signal" "$_pid" 2>/dev/null || true
}

process_tree_is_alive() {
  local _pid="${1:-}"
  local _descendants="${2:-}"

  if process_is_alive "$_pid"; then
    return 0
  fi

  pid_list_has_live_processes "$_descendants"
}

terminate_tracked_tree() {
  local _pid="${1:-}"
  local _label="${2:-process}"
  local _signal="${3:-TERM}"
  local _grace_seconds="${4:-5}"
  local _descendants=""
  local _remaining_grace=0

  [[ -n "$_pid" ]] || return 0

  if process_is_alive "$_pid"; then
    _descendants="$(list_descendant_pids_postorder "$_pid")"
    signal_pid_list "$_signal" "$_descendants"
    kill -s "$_signal" "$_pid" 2>/dev/null || true

    _remaining_grace="$_grace_seconds"
    while [[ "$_remaining_grace" -gt 0 ]]; do
      if ! process_tree_is_alive "$_pid" "$_descendants"; then
        break
      fi
      sleep 1
      _remaining_grace=$((_remaining_grace - 1))
    done

    if process_tree_is_alive "$_pid" "$_descendants"; then
      echo "==> [RTBioScan] Escalating ${_label} shutdown to SIGKILL ..."
      _descendants="$(list_descendant_pids_postorder "$_pid")"
      signal_pid_list KILL "$_descendants"
      kill -s KILL "$_pid" 2>/dev/null || true
    fi
  fi

  wait "$_pid" 2>/dev/null || true
}

cleanup_feeder_lock_after_shutdown() {
  local _tracked_pid="${1:-}"
  local _lock_dir=""
  local _lock_meta=""
  local _lock_pid=""

  [[ -n "${run_id:-}" ]] || return 0

  _lock_dir="$(feeder_lock_dir_for_run "$run_id")"
  _lock_meta="$(feeder_lock_meta_for_run "$run_id")"
  [[ -d "$_lock_dir" ]] || return 0

  if [[ -f "$_lock_meta" ]]; then
    _lock_pid="$(awk -F= '/^pid=/{print $2; exit}' "$_lock_meta" 2>/dev/null || true)"
    if [[ -n "$_lock_pid" ]] && [[ -n "$_tracked_pid" ]] && [[ "$_lock_pid" != "$_tracked_pid" ]] && process_is_alive "$_lock_pid"; then
      return 0
    fi
  fi

  if [[ -n "$_tracked_pid" ]] && process_is_alive "$_tracked_pid"; then
    return 0
  fi

  rm -f "$_lock_meta" 2>/dev/null || true
  rmdir "$_lock_dir" 2>/dev/null || rm -rf "$_lock_dir" 2>/dev/null || true
}

escalate_pipeline_shutdown_if_needed() {
  local _signal="${1:-TERM}"
  local _nextflow_pid="${NEXTFLOW_PID:-}"

  if [[ "${PIPELINE_ESCALATED:-0}" -eq 1 ]]; then
    return 0
  fi
  if [[ -z "$_nextflow_pid" ]] || ! process_is_alive "$_nextflow_pid"; then
    return 0
  fi

  PIPELINE_ESCALATED=1
  terminate_tracked_tree "$_nextflow_pid" "pipeline" "$_signal" 2
  if ! process_is_alive "$_nextflow_pid"; then
    NEXTFLOW_PID=""
  fi
}

cleanup_children() {
  local _signal="${1:-}"
  local _nextflow_pid="${NEXTFLOW_PID:-}"
  local _feeder_pid="${FEEDER_PID:-}"
  local _server_pid="${SERVER_PID:-}"
  local _feeder_stop_logged=0
  local _server_stop_logged=0
  local _stop_server_on_exit=1

  if [[ "${CLEANUP_DONE:-0}" -eq 1 ]]; then
    return 0
  fi
  CLEANUP_DONE=1
  CLEANUP_IN_PROGRESS=1

  if [[ -n "$_nextflow_pid" ]] && process_is_alive "$_nextflow_pid"; then
    if [[ -n "$_signal" ]]; then
      kill -s "$_signal" "$_nextflow_pid" 2>/dev/null || true
    else
      kill "$_nextflow_pid" 2>/dev/null || true
    fi
  fi

  if [[ -n "$_feeder_pid" ]] && process_is_alive "$_feeder_pid"; then
    echo
    echo "==> [RTBioScan] Stopping feeder (PID $_feeder_pid) ..."
    signal_tracked_tree "$_feeder_pid" TERM
    _feeder_stop_logged=1
  fi

  if [[ -n "$_server_pid" ]] && process_is_alive "$_server_pid"; then
    if confirm_report_server_shutdown "$_server_pid"; then
      echo "==> [RTBioScan] Stopping report server (PID $_server_pid) ..."
      signal_tracked_tree "$_server_pid" TERM
      _server_stop_logged=1
    else
      _stop_server_on_exit=0
      echo "==> [RTBioScan] Leaving report server running (PID $_server_pid)."
    fi
  fi

  if [[ -n "$_nextflow_pid" ]]; then
    terminate_tracked_tree "$_nextflow_pid" "pipeline" "${_signal:-TERM}" 2
    NEXTFLOW_PID=""
  fi

  if [[ -n "$_feeder_pid" ]]; then
    terminate_tracked_tree "$_feeder_pid" "feeder" TERM 2
    kill_run_feeders "${run_id:-}" "same-run feeder"
    cleanup_feeder_lock_after_shutdown "$_feeder_pid"
    FEEDER_PID=""
    if [[ $_feeder_stop_logged -eq 1 ]]; then
      echo "==> [RTBioScan] Feeder stopped."
    fi
  elif [[ -n "${run_id:-}" ]]; then
    kill_run_feeders "$run_id" "same-run feeder"
  fi

  if [[ $_stop_server_on_exit -eq 1 && -n "$_server_pid" ]]; then
    terminate_tracked_tree "$_server_pid" "report server" TERM 2
    if [[ -n "${serve_dir_canonical:-}" ]]; then
      _unregister_server "$_server_pid" "$serve_dir_canonical"
    fi
    SERVER_PID=""
    if [[ $_server_stop_logged -eq 1 ]]; then
      echo "==> [RTBioScan] Report server stopped."
    fi
  elif [[ -n "$_server_pid" ]]; then
    SERVER_PID=""
  fi

  prune_seeded_run_status_if_stale
}

finalize_pending_signal_if_needed() {
  local _pending_signal="${PENDING_SIGNAL_NAME:-}"
  local _pending_exit_code="${PENDING_SIGNAL_EXIT_CODE:-}"
  if [[ -z "$_pending_signal" || -z "$_pending_exit_code" ]]; then
    return 0
  fi
  PENDING_SIGNAL_NAME=""
  PENDING_SIGNAL_EXIT_CODE=""
  handle_wrapper_signal "$_pending_signal" "$_pending_exit_code"
}

handle_wrapper_signal() {
  local _signal="$1"
  local _exit_code="$2"
  if [[ "${LAUNCH_IN_PROGRESS:-0}" -eq 1 ]]; then
    if [[ -z "${PENDING_SIGNAL_NAME:-}" ]]; then
      PENDING_SIGNAL_NAME="$_signal"
      PENDING_SIGNAL_EXIT_CODE="$_exit_code"
    fi
    return 0
  fi
  if [[ "${CLEANUP_IN_PROGRESS:-0}" -eq 1 ]]; then
    escalate_pipeline_shutdown_if_needed "$_signal"
    return 0
  fi
  SHUTDOWN_SIGNAL="$_signal"
  if [[ -z "${SHUTDOWN_EXIT_CODE:-}" ]]; then
    SHUTDOWN_EXIT_CODE="$_exit_code"
  fi
  cleanup_children "$_signal"
  if [[ -z "${NEXTFLOW_PID:-}" ]] || ! process_is_alive "${NEXTFLOW_PID:-}"; then
    exit "${SHUTDOWN_EXIT_CODE:-$_exit_code}"
  fi
  return 0
}

trap 'cleanup_children' EXIT
trap 'handle_wrapper_signal INT 130' INT
trap 'handle_wrapper_signal TERM 143' TERM
trap 'handle_wrapper_signal HUP 129' HUP

if [[ $serve -eq 1 && $cleanup_modes -eq 0 && $dry_run -eq 0 && $view_only -eq 0 ]]; then
  seed_initial_run_status_if_needed
fi

# ── start feeder in background ────────────────────────────────────────────────
if [[ $feeder -eq 1 ]]; then
  feeder_args=(--run_id "$run_id")
  [[ -n "$input_folder" ]]  && feeder_args+=(--input_folder  "$input_folder")
  [[ -n "$num_reads" ]]     && feeder_args+=(--num_reads     "$num_reads")
  [[ -n "$sleep_time" ]]    && feeder_args+=(--sleep_time    "$sleep_time")
  [[ -n "$metadata_file" ]] && feeder_args+=(--metadata      "$metadata_file")
  [[ -n "$general_fasta" ]] && feeder_args+=(--general_fasta "$general_fasta")
  [[ -n "$primers_fasta" ]] && feeder_args+=(--primers_fasta "$primers_fasta")
  [[ -n "$resolved_targets" ]] && feeder_args+=(--targets "$resolved_targets")
  [[ $skip_pod5 -eq 1 ]]          && feeder_args+=(--skip_pod5)
  [[ $delete_full_pod5 -eq 1 ]]  && feeder_args+=(--delete_full_pod5)

  feeder_log="${SCRIPT_DIR}/results/feeder.log"
  mkdir -p "${SCRIPT_DIR}/results"
  : >"$feeder_log"
  echo "==> [RTBioScan] Starting POD5 feeder (run_id='$run_id') ..."
  echo "    Feeder output → $feeder_log"
  LAUNCH_IN_PROGRESS=1
  (
    cd "$SCRIPT_DIR"
    exec bash "$FEEDER" "${feeder_args[@]}"
  ) >>"$feeder_log" 2>&1 &
  FEEDER_PID=$!
  LAUNCH_IN_PROGRESS=0
  finalize_pending_signal_if_needed
  sleep 1
  if ! kill -0 "$FEEDER_PID" 2>/dev/null; then
    echo "ERROR: [RTBioScan] POD5 feeder exited immediately." >&2
    echo "       Check --input_folder, metadata arguments, and feeder log." >&2
    echo "       Feeder log: $feeder_log" >&2
    wait "$FEEDER_PID" 2>/dev/null || true
    FEEDER_PID=""
    exit 1
  fi
  echo "==> [RTBioScan] Feeder started (PID $FEEDER_PID)"
  echo
fi

# ── start report server in background ────────────────────────────────────────
if [[ $serve -eq 1 ]]; then
  serve_port_limit=8099
  serve_bind_host="$(normalize_serve_bind_host "$serve_host")"
  if [[ "$serve_dir" == /* ]]; then
    serve_dir_path="$serve_dir"
  else
    serve_dir_path="${SCRIPT_DIR}/${serve_dir}"
  fi
  mkdir -p "$serve_dir_path"
  serve_dir_canonical="$(_canonical_serve_dir "$serve_dir_path")"
  _cleanup_orphaned_servers "$serve_dir_canonical"
  serve_host_display="$(serve_browser_host "$serve_host")"
  if [[ $serve_port_explicit -ne 1 ]]; then
    chosen_serve_port="$(pick_free_serve_port "$serve_bind_host" "$serve_port" "$serve_port_limit" 2>/dev/null || true)"
    if [[ -n "$chosen_serve_port" ]]; then
      serve_port="$chosen_serve_port"
    fi
  fi
  server_log="${serve_dir_path}/server.log"
  server_port_file="${serve_dir_path}/server.port"
  server_url_file="${serve_dir_path}/server.url"
  server_ready_file=""
  rm -f "$server_port_file" "$server_url_file" "$serve_dir_path/server.pid"
  SERVER_PID=""
  while :; do
    server_ready_file="$(mktemp "${serve_dir_path}/.server.ready.XXXXXX")"
    rm -f "$server_ready_file"
    server_url="http://${serve_host_display}:${serve_port}/report_html/report.html"
    server_args=(--dir "$serve_dir_path" --port "$serve_port" --host "$serve_bind_host" --wait-for-report --ready-file "$server_ready_file")
    [[ $serve_open     -eq 1 ]] && server_args+=(--open)
    [[ $serve_open_all -eq 1 ]] && server_args+=(--open-all)
    [[ -n "$serve_open_last" ]] && server_args+=(--open-last "$serve_open_last")
    [[ $serve_quiet    -eq 1 ]] && server_args+=(--quiet)

    : >"$server_log"
    echo "==> [RTBioScan] Starting report server (${server_url}) ..."
    LAUNCH_IN_PROGRESS=1
    bash "$SERVER" "${server_args[@]}" >>"$server_log" 2>&1 &
    SERVER_PID=$!
    LAUNCH_IN_PROGRESS=0
    finalize_pending_signal_if_needed
    # Wait for serve_report.sh to confirm that the Python HTTP server bound the port.
    server_ready=0
    server_wait_remaining=5
    while [[ "$server_wait_remaining" -gt 0 ]]; do
      if [[ -s "$server_ready_file" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
        server_ready=1
        break
      fi
      if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        break
      fi
      sleep 1
      server_wait_remaining=$((server_wait_remaining - 1))
    done
    if [[ "$server_ready" -eq 1 ]]; then
      break
    fi
    rm -f "$server_ready_file" 2>/dev/null || true
    if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
      terminate_tracked_tree "$SERVER_PID" "report server" TERM 2
    fi
    SERVER_PID=""
    if [[ $serve_port_explicit -ne 1 ]] && [[ -f "$server_log" ]] && grep -q "Address already in use" "$server_log"; then
      if [[ "$serve_port" =~ ^[0-9]+$ ]] && [[ "$serve_port" -lt "$serve_port_limit" ]]; then
        serve_port=$((serve_port + 1))
        continue
      fi
    fi
    break
  done

  if [[ -z "$SERVER_PID" ]]; then
    if [[ -f "$server_log" ]] && grep -q "Address already in use" "$server_log"; then
      echo "WARNING: [RTBioScan] Report server failed to start because port ${serve_port} is already in use." >&2
      echo "         Use --serve-port <n> to choose a different port, or stop the process already bound to ${serve_port}." >&2
    elif [[ -f "$server_log" ]] && grep -q "report not found:" "$server_log"; then
      echo "WARNING: [RTBioScan] Report server failed to start because no report was found under '${serve_dir_path}'." >&2
      echo "         Check --serve-dir/--outdir, or prebuild the report before using the standalone helper without wait mode." >&2
    else
      echo "WARNING: [RTBioScan] Report server failed to start." >&2
    fi
    echo "         Server log: $server_log" >&2
  else
    printf '%s\n' "$serve_port" >"$server_port_file"
    printf '%s\n' "$server_url" >"$server_url_file"
    _register_server "$SERVER_PID" "$serve_dir_canonical"
    echo "==> [RTBioScan] Report server started (PID $SERVER_PID)"
    echo "    Report URL  → $server_url"
    echo "    Server log  → $server_log"
    echo "    Server port → $server_port_file"
    echo "    Server URL  → $server_url_file"
  fi
  echo
fi

# ── pin Nextflow version (DSL1 requires <23.0) ────────────────────────────────
# Override by setting NXF_VER in your environment before calling this script.
export NXF_VER="${NXF_VER:-22.10.8}"

# Detect resume/restart so new runs can clear single_exp workspace
_is_resume=0
for _a in ${nf_args[@]+"${nf_args[@]}"}; do
  [[ "$_a" == "-resume" || "$_a" == "--resume" || "$_a" == -resume=* ]] && _is_resume=1 && break
done

# ── run the pipeline (tracked child) ──────────────────────────────────────────

# View-only mode: server already started above; just wait for it to exit.
if [[ $view_only -eq 1 ]]; then
  if [[ -n "${SERVER_PID:-}" ]]; then
    echo "==> [RTBioScan] View-only mode active — press Ctrl-C to exit."
    set +e
    while [[ -n "${SERVER_PID:-}" ]] && process_is_alive "${SERVER_PID:-}"; do
      sleep 1
    done
    set -e
    exit 0
  fi
  exit 1
fi

# Clear single_exp workspace for new (non-resumed) runs before pipeline starts
if [[ $_is_resume -eq 0 && $cleanup_modes -eq 0 && $dry_run -eq 0 && $view_only -eq 0 ]]; then
  for _p in \
      "${SCRIPT_DIR}/results/ongoing/single_exp"; do
    if [[ -d "$_p" ]]; then
      echo "==> [RTBioScan] New run: clearing ${_p} ..."
      rm -rf "$_p"
    fi
  done
fi

build_normalized_nextflow_run_cmd
echo "==> [RTBioScan] Starting pipeline ..."
echo "    ${NORMALIZED_NEXTFLOW_RUN_CMD[*]}"
echo

LAUNCH_IN_PROGRESS=1
(
  cd "$SCRIPT_DIR"
  exec "${NORMALIZED_NEXTFLOW_RUN_CMD[@]}"
) &
NEXTFLOW_PID=$!
LAUNCH_IN_PROGRESS=0
finalize_pending_signal_if_needed

set +e
while [[ -n "${NEXTFLOW_PID:-}" ]] && process_is_alive "$NEXTFLOW_PID"; do
  sleep 1
done
wait "$NEXTFLOW_PID" 2>/dev/null
nextflow_exit_code=$?
set -e
NEXTFLOW_PID=""
if [[ "${CLEANUP_IN_PROGRESS:-0}" -eq 1 ]]; then
  exit "${SHUTDOWN_EXIT_CODE:-$nextflow_exit_code}"
fi
exit "$nextflow_exit_code"
