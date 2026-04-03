#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCH_DIR="$(pwd -P)"
FEEDER="${SCRIPT_DIR}/bin/Metadata_pod5_processing.sh"
SERVER="${SCRIPT_DIR}/bin/serve_report.sh"

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
serve_port="8000"
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
  --serve-port <n>        Port for the HTTP server [default: 8000]
  --serve-host <addr>     Bind address [default: 127.0.0.1]
  --serve-open            Open report.html in the default browser on start
  --serve-open-all        Open all per-run reports in the browser on start
  --serve-open-last <n>   Open the last N per-run reports in the browser on start
  --serve-quiet           Suppress HTTP request logging
  --serve-dir <dir>       Directory to serve [default: same as --outdir, or results/]

Cleanup options:
  --clean-ref <dir>       Reference directory for cleanup discovery. With
                          --clean-all/--clean-temp-all it becomes the cleanup
                          root; with run-specific cleanup it is searched first.
  --clean <id>            Remove all artifacts for the given run ID:
                            results/runs/<id>/            results/current/state/<id>/
                            results/temp/*/state/<id>/    results/pod5/<id>/
                            results/sample_info/<id>/     work/
                          Also filters <id> from .nextflow/history and
                          results/runs_index.jsonl. Root discovery order:
                          --clean-ref, current working directory, pipeline dir.
  --clean-all             Remove ALL run artifacts: results/runs/,
                            results/current/, results/temp/, results/pod5/,
                            results/sample_info/, results/report*.*, work/,
                            .nextflow/, and .nextflow.log* files from the
                            current working directory by default.
  --clean-temp <id>       Remove only temporary artifacts for the given run ID:
                            results/temp/*/state/<state_id>/
                            results/ongoing/state/<state_id>/
                            work/ temp files tracked by Nextflow for <id>
                          Preserves reports, POD5 archives, sample info, and
                          Nextflow history/log metadata.
  --clean-temp-all        Remove only temporary artifacts for ALL runs:
                            results/temp/, results/ongoing/, and work/ temp
                            files tracked by Nextflow in the current working
                            directory by default.
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
    --run_id)        shift; run_id="$1" ;;
    --input_folder)  shift; input_folder="$1" ;;
    --num_reads)     shift; num_reads="$1" ;;
    --sleep_time)    shift; sleep_time="$1" ;;
    --metadata)      shift; metadata_file="$1" ;;
    --general_fasta) shift; general_fasta="$1" ;;
    --primers_fasta) shift; primers_fasta="$1" ;;
    --targets)       shift; targets="$1"; targets_explicit=1; nf_args+=("--targets" "$targets") ;;
    --do_metadata)          do_metadata=1 ;;
    --skip_pod5)            skip_pod5=1 ;;
    --delete-full-pod5)     delete_full_pod5=1 ;;
    --delete_input_pod5)    delete_input_pod5=1 ;;
    --delete_from_ori_dir)  delete_from_ori_dir=1 ;;
    --serve)            serve=1 ;;
    --serve-port)       shift; serve_port="$1" ;;
    --serve-host)       shift; serve_host="$1" ;;
    --serve-open)       serve_open=1 ;;
    --serve-open-all)   serve_open_all=1 ;;
    --serve-open-last)  shift; serve_open_last="$1" ;;
    --serve-quiet)      serve_quiet=1 ;;
    --serve-dir)        shift; serve_dir="$1" ;;
    --clean-ref)        shift; clean_ref="$1" ;;
    --clean)            shift; clean_run_id="$1" ;;
    --clean-all)        clean_all=1 ;;
    --clean-temp)       shift; clean_temp_run_id="$1" ;;
    --clean-temp-all)   clean_temp_all=1 ;;
    --dry-run)          dry_run=1 ;;
    -y|--yes)           yes=1 ;;
    -h|--help)          usage ;;
    *)                  nf_args+=("$1") ;;
  esac
  shift
done

if [[ -n "$clean_ref" && ! -d "$clean_ref" ]]; then
  echo "ERROR: --clean-ref directory does not exist: $clean_ref" >&2
  exit 1
fi
if [[ -n "$clean_ref" ]]; then
  clean_ref="$(cd "$clean_ref" && pwd -P)"
fi

cleanup_modes=0
[[ -n "$clean_run_id" ]] && cleanup_modes=$((cleanup_modes + 1))
[[ $clean_all -eq 1 ]] && cleanup_modes=$((cleanup_modes + 1))
[[ -n "$clean_temp_run_id" ]] && cleanup_modes=$((cleanup_modes + 1))
[[ $clean_temp_all -eq 1 ]] && cleanup_modes=$((cleanup_modes + 1))
if [[ $cleanup_modes -gt 1 ]]; then
  echo "ERROR: choose only one cleanup mode: --clean, --clean-all, --clean-temp, or --clean-temp-all" >&2
  exit 1
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

lookup_state_id_for_run() {
  local _root="$1"
  local _run_id="$2"
  local _run_index="${_root}/results/runs_index.jsonl"
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
      "${_root}/results/runs/${_run_id}" \
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
  _nextflow_clean_args=()
  _nextflow_preview_args=()

  if [[ ! -x "$_nextflow_bin" ]] && [[ -n "$clean_temp_run_id" || $clean_temp_all -eq 1 ]]; then
    echo "ERROR: nextflow executable not found at $_nextflow_bin; temp cleanup requires it for work/ cleanup" >&2
    exit 1
  fi

  if [[ $clean_all -eq 1 ]]; then
    echo "==> [RTBioScan] Clean-all: staging removal of all run artifacts under '${_target_root}' ..."
    for _p in \
        "$_outdir/runs" \
        "$_outdir/current" \
        "$_outdir/temp" \
        "$_outdir/pod5" \
        "$_outdir/sample_info" \
        "$_outdir/report.html" \
        "$_outdir/report_state.json" \
        "$_outdir/runs_index.jsonl" \
        "$_outdir/feeder.log" \
        "$_outdir/server.log" \
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
    _nextflow_clean_args=(-f -k)
    _nextflow_preview_args=(-n -k)
  else
    if [[ -n "$clean_run_id" ]]; then
      _state_id="$(lookup_state_id_for_run "$_target_root" "$clean_run_id")"
      echo "==> [RTBioScan] Clean run: staging removal of artifacts for run_id='${clean_run_id}' in '${_target_root}' (state_id='${_state_id}') ..."
      _filter_id="$clean_run_id"
      for _p in \
          "$_outdir/runs/${clean_run_id}" \
          "$_outdir/current/state/${_state_id}" \
          "$_outdir/temp/ongoing/state/${_state_id}" \
          "$_outdir/temp/current/state/${_state_id}" \
          "$_outdir/pod5/${clean_run_id}" \
          "$_outdir/sample_info/${clean_run_id}"; do
        [[ -e "$_p" ]] && _rm_targets+=("$_p")
      done
      for _f in "${_target_root}"/.nextflow.log*; do
        [[ -f "$_f" ]] && _rm_targets+=("$_f")
      done
      [[ -d "${_target_root}/work" ]] && _work_dir="${_target_root}/work"
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
    for _p in "${_rm_targets[@]}"; do echo "      rm -rf  $_p"; done
  fi
  if [[ -n "$_work_dir" ]]; then
    echo "      clear   ${_work_dir}/*  (directory kept)"
  fi
  if [[ ${#_nextflow_preview_args[@]} -gt 0 ]]; then
    echo "      nextflow ${_nextflow_preview_args[*]}"
  fi
  if [[ -n "$_filter_id" ]]; then
    [[ -f "${_target_root}/.nextflow/history" ]] && \
      echo "      filter  ${_target_root}/.nextflow/history  (remove '${_filter_id}' entries)"
    [[ -f "$_outdir/runs_index.jsonl" ]] && \
      echo "      filter  $_outdir/runs_index.jsonl  (remove '${_filter_id}' entries)"
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
    printf "Delete the above? [y/N] "
    read -r _resp
    [[ "$_resp" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
    echo
  fi

  if [[ ${#_nextflow_clean_args[@]} -gt 0 ]]; then
    if ! (
      cd "$_target_root"
      "$_nextflow_bin" clean "${_nextflow_clean_args[@]}"
    ); then
      echo "ERROR: Nextflow work/ temp cleanup failed. Stop active runs or clear the lock, then retry." >&2
      exit 1
    fi
    if [[ $clean_temp_all -eq 1 ]]; then
      echo "    Cleaned: work/ temp files for all Nextflow runs"
    elif [[ -n "$clean_temp_run_id" ]]; then
      echo "    Cleaned: work/ temp files for Nextflow run '${clean_temp_run_id}'"
    fi
  fi

  for _p in "${_rm_targets[@]}"; do
    rm -rf "$_p"
    echo "    Removed: $_p"
  done
  if [[ -n "$_work_dir" ]]; then
    find "$_work_dir" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
    echo "    Cleared: ${_work_dir}/*"
  fi

  if [[ -n "$_filter_id" ]]; then
    _hist="${_target_root}/.nextflow/history"
    if [[ -f "$_hist" ]]; then
      _tmp=$(mktemp)
      awk -F'\t' -v rid="${_filter_id}" '$3 != rid' "$_hist" > "$_tmp" && mv "$_tmp" "$_hist"
      echo "    Filtered: $_hist"
    fi
    _idx="$_outdir/runs_index.jsonl"
    if [[ -f "$_idx" ]]; then
      _tmp=$(mktemp)
      python3 - "${_filter_id}" "$_idx" <<'PYEOF' > "$_tmp" && mv "$_tmp" "$_idx"
import sys, json
rid, path = sys.argv[1], sys.argv[2]
for line in open(path):
    try:
        if json.loads(line).get('run_id') != rid:
            sys.stdout.write(line)
    except Exception:
        sys.stdout.write(line)
PYEOF
      echo "    Filtered: $_idx"
    fi
    # Regenerate the top-level report.html so the cleaned run's broken link is removed.
    _report_html="$_outdir/report.html"
    _report_state="$_outdir/report_state.json"
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
if [[ $feeder -eq 1 && $skip_pod5 -eq 0 && -n "$input_folder" && ! -e "$input_folder" ]]; then
  echo "ERROR: --input_folder does not exist: $input_folder" >&2
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
if [[ -n "$run_id" ]]; then
  name_present=0; reads_set=0; ori_dir_set=0; indexes_set=0; primer_indexes_set=0; watch_set=0
  for _a in ${nf_args[@]+"${nf_args[@]}"}; do
    case "$_a" in
      -name)            name_present=1 ;;
      --reads)          reads_set=1 ;;
      --ori_dir)        ori_dir_set=1 ;;
      --indexes)        indexes_set=1 ;;
      --primer_indexes) primer_indexes_set=1 ;;
      --watch)          watch_set=1 ;;
    esac
  done
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
[[ $delete_input_pod5   -eq 1 ]] && nf_args+=(--delete_input_pod5   true)
[[ $delete_from_ori_dir -eq 1 ]] && nf_args+=(--delete_from_ori_dir true)

# Ensure the realtime feeder tree exists before Nextflow validates --reads.
# This avoids a startup race where the feeder is still initializing.
if [[ $feeder -eq 1 && -n "$run_id" ]]; then
  mkdir -p \
    "results/pod5/$run_id/reads_rt_round_pod5" \
    "results/pod5/$run_id/ori_round_pod5" \
    "results/pod5/$run_id/full_pod5" \
    "results/pod5/$run_id/done_round_pod5" \
    "results/pod5/$run_id/metadata"
fi

# Derive serve-dir from --outdir in nextflow args if not set explicitly
if [[ $serve -eq 1 && -z "$serve_dir" ]]; then
  for (( i=0; i<${#nf_args[@]}; i++ )); do
    if [[ "${nf_args[$i]}" == "--outdir" && $((i+1)) -lt ${#nf_args[@]} ]]; then
      serve_dir="${nf_args[$((i+1))]}"
      break
    fi
  done
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
  bash "$FEEDER" "${meta_args[@]}"
  echo "==> [RTBioScan] Metadata setup complete."
  echo
fi

# ── cleanup handler (runs on exit, Ctrl-C, and SIGTERM) ──────────────────────
FEEDER_PID=""
SERVER_PID=""
cleanup() {
  if [[ -n "$FEEDER_PID" ]] && kill -0 "$FEEDER_PID" 2>/dev/null; then
    echo
    echo "==> [RTBioScan] Stopping feeder (PID $FEEDER_PID) ..."
    kill "$FEEDER_PID" 2>/dev/null || true
    wait "$FEEDER_PID" 2>/dev/null || true
    echo "==> [RTBioScan] Feeder stopped."
  fi
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "==> [RTBioScan] Stopping report server (PID $SERVER_PID) ..."
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    echo "==> [RTBioScan] Report server stopped."
  fi
}
trap cleanup EXIT

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
  bash "$FEEDER" "${feeder_args[@]}" >>"$feeder_log" 2>&1 &
  FEEDER_PID=$!
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
  mkdir -p "${SCRIPT_DIR}/${serve_dir}"
  server_args=(--dir "$serve_dir" --port "$serve_port" --host "$serve_host")
  [[ $serve_open     -eq 1 ]] && server_args+=(--open)
  [[ $serve_open_all -eq 1 ]] && server_args+=(--open-all)
  [[ -n "$serve_open_last" ]] && server_args+=(--open-last "$serve_open_last")
  [[ $serve_quiet    -eq 1 ]] && server_args+=(--quiet)

  server_log="${SCRIPT_DIR}/results/server.log"
  mkdir -p "${SCRIPT_DIR}/results"
  echo "==> [RTBioScan] Starting report server (http://${serve_host}:${serve_port}/report.html) ..."
  bash "$SERVER" "${server_args[@]}" >>"$server_log" 2>&1 &
  SERVER_PID=$!
  # Brief liveness check: give the server ~1 s to bind, then verify it's still up.
  sleep 1
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "WARNING: [RTBioScan] Report server failed to start (port ${serve_port} may already be in use)." >&2
    echo "         Use --serve-port <n> to choose a different port, or kill the process on port ${serve_port}." >&2
    echo "         Server log: $server_log" >&2
    SERVER_PID=""
  else
    echo "==> [RTBioScan] Report server started (PID $SERVER_PID)"
    echo "    Server log  → $server_log"
  fi
  echo
fi

# ── pin Nextflow version (DSL1 requires <23.0) ────────────────────────────────
# Override by setting NXF_VER in your environment before calling this script.
export NXF_VER="${NXF_VER:-22.10.8}"

# ── run the pipeline (foreground) ─────────────────────────────────────────────
build_normalized_nextflow_run_cmd
echo "==> [RTBioScan] Starting pipeline ..."
echo "    ${NORMALIZED_NEXTFLOW_RUN_CMD[*]}"
echo
cd "$SCRIPT_DIR"
"${NORMALIZED_NEXTFLOW_RUN_CMD[@]}"
