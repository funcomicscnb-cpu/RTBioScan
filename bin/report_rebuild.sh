#!/usr/bin/env bash
set -euo pipefail
shopt -s nullglob

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

usage() {
  cat <<'USAGE'
Usage: report_rebuild.sh [--outdir DIR] [--state-id ID] [--history PATH] [--run-id ID] [--all-runs] [--skip-root-report] [--group-view sample|replicate|track_detail] [--url-prefix PREFIX] [--auto-refresh 0|1] [--refresh-seconds N] [--sample-plot-max N]

Rebuild RTBioScan HTML reports from existing history files.

Defaults:
  --outdir           results
  --url-prefix       <empty> (relative paths)
  --auto-refresh     1
  --refresh-seconds  15
USAGE
}

OUTDIR="results"
STATE_ID=""
HISTORY_OVERRIDE=""
RUN_ID=""
ALL_RUNS=0
SKIP_ROOT_REPORT=0
URL_PREFIX=""
AUTO_REFRESH="1"
REFRESH_SECONDS="15"
SAMPLE_PLOT_MAX="-1"
GROUP_VIEW="sample"
RUN_HTML_OVERRIDE=""
RUN_STATE_OVERRIDE=""
FIGURES_DIR_NAME_OVERRIDE=""
REPORT_ASSETS_DIR_NAME_OVERRIDE=""
SKIP_RUN_JSON=0

RUN_ID_SEEN=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --outdir)
      OUTDIR="${2:-}"
      shift 2
      ;;
    --run-id)
      RUN_ID_SEEN=1
      RUN_ID="${2:-}"
      shift 2
      ;;
    --state-id)
      STATE_ID="${2:-}"
      shift 2
      ;;
    --history)
      HISTORY_OVERRIDE="${2:-}"
      shift 2
      ;;
    --all-runs)
      ALL_RUNS=1
      shift
      ;;
    --skip-root-report)
      SKIP_ROOT_REPORT=1
      shift
      ;;
    --group-view)
      GROUP_VIEW="${2:-}"
      shift 2
      ;;
    --run-html)
      RUN_HTML_OVERRIDE="${2:-}"
      shift 2
      ;;
    --run-state)
      RUN_STATE_OVERRIDE="${2:-}"
      shift 2
      ;;
    --figures-dir-name)
      FIGURES_DIR_NAME_OVERRIDE="${2:-}"
      shift 2
      ;;
    --report-assets-dir-name)
      REPORT_ASSETS_DIR_NAME_OVERRIDE="${2:-}"
      shift 2
      ;;
    --skip-run-json)
      SKIP_RUN_JSON=1
      shift
      ;;
    --url-prefix)
      URL_PREFIX="${2:-}"
      shift 2
      ;;
    --auto-refresh)
      AUTO_REFRESH="${2:-}"
      shift 2
      ;;
    --refresh-seconds)
      REFRESH_SECONDS="${2:-}"
      shift 2
      ;;
    --sample-plot-max)
      SAMPLE_PLOT_MAX="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" 1>&2
      usage 1>&2
      exit 2
      ;;
  esac
done

if [ -z "$OUTDIR" ]; then
  echo "ERROR: --outdir must be set" 1>&2
  exit 2
fi

case "$GROUP_VIEW" in
  sample|replicate|track_detail) ;;
  *)
    echo "ERROR: invalid --group-view '$GROUP_VIEW': must be sample, replicate, or track_detail" 1>&2
    exit 2
    ;;
esac

if [ "$ALL_RUNS" -eq 1 ] && [ -n "$RUN_ID" ]; then
  echo "ERROR: use only one of --run-id or --all-runs" 1>&2
  exit 2
fi
if [ "$RUN_ID_SEEN" -eq 1 ] && [ -z "$RUN_ID" ]; then
  echo "ERROR: --run-id requires a non-empty value" 1>&2
  exit 2
fi

HISTORY=""
if [ -n "$HISTORY_OVERRIDE" ]; then
  HISTORY="$HISTORY_OVERRIDE"
elif [ -n "$STATE_ID" ]; then
  HISTORY="${OUTDIR%/}/temp/ongoing/state/${STATE_ID}/_state/report_history.jsonl"
else
  candidates=( "${OUTDIR%/}/temp/ongoing/state"/*/_state/report_history.jsonl )
  if [ "${#candidates[@]}" -gt 0 ]; then
    best=""
    best_m=0
    for f in "${candidates[@]}"; do
      m=$(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f" 2>/dev/null || echo 0)
      if [ "$m" -gt "$best_m" ]; then
        best_m="$m"
        best="$f"
      fi
    done
    HISTORY="$best"
  fi
fi
RUN_INDEX="${OUTDIR%/}/report_html/runs_index.jsonl"
REPORT_HTML="${OUTDIR%/}/report_html/report.html"
REPORT_STATE="${OUTDIR%/}/report_html/report_state.json"
TEMPLATE="${REPO_ROOT}/assets/report/template.html"
RUN_TEMPLATE="${REPO_ROOT}/assets/report/run_template.html"
CSS="${REPO_ROOT}/assets/report/report.css"
JS="${REPO_ROOT}/assets/report/report.js"

if [ -z "$HISTORY" ] || [ ! -s "$HISTORY" ]; then
  echo "ERROR: history not found or empty: $HISTORY" 1>&2
  exit 2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found in PATH" 1>&2
  exit 2
fi

PYTHON="${PYTHON:-python3}"

lookup_state_id_for_run() {
  local run_id="$1"
  if [ -z "$run_id" ] || [ ! -s "$RUN_INDEX" ]; then
    printf '%s\n' "$run_id"
    return 0
  fi
  "$PYTHON" - "$RUN_INDEX" "$run_id" <<'PY'
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
PY
}

history_for_state() {
  local state_id="$1"
  if [ -n "$HISTORY_OVERRIDE" ] && [ -s "$HISTORY_OVERRIDE" ]; then
    printf '%s\n' "$HISTORY_OVERRIDE"
    return 0
  fi
  if [ -z "$state_id" ]; then
    return 1
  fi
  local candidates=(
    "${OUTDIR%/}/temp/ongoing/state/${state_id}/_state/report_history.jsonl"
    "${OUTDIR%/}/temp/current/state/${state_id}/_state/report_history.jsonl"
  )
  local path
  for path in "${candidates[@]}"; do
    if [ -s "$path" ]; then
      printf '%s\n' "$path"
      return 0
    fi
  done
  return 1
}

lookup_history_field() {
  local history_path="$1"
  local field_name="$2"
  if [ -z "$history_path" ] || [ ! -s "$history_path" ] || [ -z "$field_name" ]; then
    return 0
  fi
  "$PYTHON" - "$history_path" "$field_name" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
field_name = sys.argv[2]
value = ""
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except Exception:
        continue
    candidate = obj.get(field_name)
    if candidate not in (None, ""):
        value = str(candidate)
print(value)
PY
}

ensure_live_round_link() {
  local state_id="$1"
  local run_id="$2"
  local report_assets_dir_name="${3:-report_assets}"
  if [ -z "$state_id" ] || [ -z "$run_id" ]; then
    return 0
  fi
  local state_live="${OUTDIR%/}/current/state/${state_id}/live_round/report_assets"
  local run_assets="${OUTDIR%/}/report_html/runs/${run_id}/${report_assets_dir_name}"
  local run_live="${run_assets}/live_round"
  if [ ! -d "$state_live" ] && [ ! -L "$state_live" ]; then
    return 0
  fi
  mkdir -p "$run_assets"
  local tmp_link="${run_assets}/.live_round.next.$$"
  ln -s "$state_live" "$tmp_link"
  mv -f "$tmp_link" "$run_live"
}

render_root_report() {
  local render_args=(
    --history "$HISTORY"
    --template "$TEMPLATE"
    --css "$CSS"
    --js "$JS"
    --schema-version "2.0"
    --state-out "$REPORT_STATE"
    --auto-refresh-enabled "$AUTO_REFRESH"
    --auto-refresh-seconds "$REFRESH_SECONDS"
    --state-url "report_state.json"
    --url-prefix "$URL_PREFIX"
    --out "$REPORT_HTML"
  )
  if [ -s "$RUN_INDEX" ]; then
    render_args+=(--run-index "$RUN_INDEX")
  fi
  "$PYTHON" "${SCRIPT_DIR}/report_render.py" "${render_args[@]}"
}

rebuild_run_json() {
  local run_id="$1"
  local run_dir="${OUTDIR%/}/report_html/runs/${run_id}"
  local run_report_json="${run_dir}/run_report.json"
  local run_index_lock="${OUTDIR%/}/.runs_index.lock"
  local state_id=""
  local run_history=""
  local barcode=""
  local history_state_id=""
  local schema_version=""
  local run_started_utc_file=""
  local run_cmd=()

  mkdir -p "$run_dir"

  state_id="$(lookup_state_id_for_run "$run_id")"
  if ! run_history="$(history_for_state "$state_id")"; then
    echo "WARN: history not found for run ${run_id} (state_id=${state_id}); skipping run JSON rebuild" 1>&2
    return 0
  fi
  if [ -z "$STATE_ID" ] && [ -n "$RUN_ID" ]; then
    HISTORY="$run_history"
  fi

  barcode="$(lookup_history_field "$run_history" barcode)"
  history_state_id="$(lookup_history_field "$run_history" state_id)"
  schema_version="$(lookup_history_field "$run_history" schema_version)"
  if [ -n "$history_state_id" ]; then
    state_id="$history_state_id"
  fi
  if [ -z "$schema_version" ]; then
    schema_version="2.0"
  fi
  run_started_utc_file="$(dirname "$run_history")/run_started_utc.txt"

  run_cmd=(
    perl
    "${SCRIPT_DIR}/report_run_json.pl"
    --history "$run_history"
    --out "$run_report_json"
    --run-id "$run_id"
    --state-id "$state_id"
    --outdir "${OUTDIR%/}"
    --schema-version "$schema_version"
    --report-rel-path "runs/${run_id}/report.html"
  )
  if [ -n "$barcode" ]; then
    run_cmd+=(--barcode "$barcode")
  fi
  if [ -s "$run_started_utc_file" ]; then
    run_cmd+=(--run-started-utc-file "$run_started_utc_file")
  fi
  "${run_cmd[@]}"

  LOCK_WAIT="${LOCK_WAIT:-300}" bash "${SCRIPT_DIR}/report_run_index_update.sh" \
    "$run_report_json" \
    "$RUN_INDEX" \
    "$run_index_lock"
}

render_run_report() {
  local run_id="$1"
  local run_dir="${OUTDIR%/}/report_html/runs/${run_id}"
  local run_report_name="report.html"
  local run_state_name="report_state.json"
  local figures_dir_name="figures"
  local report_assets_dir_name="report_assets"
  local run_report=""
  local run_state=""
  local state_url=""
  local figures_dir_url=""
  local state_id=""
  local run_history=""
  case "$GROUP_VIEW" in
    sample)
      ;;
    replicate)
      run_report_name="report_replicates.html"
      run_state_name="report_replicates_state.json"
      figures_dir_name="figures_replicates"
      report_assets_dir_name="report_assets_replicates"
      ;;
    track_detail)
      run_report_name="report_replicates_primers.html"
      run_state_name="report_replicates_primers_state.json"
      figures_dir_name="figures_replicates_primers"
      report_assets_dir_name="report_assets_replicates_primers"
      ;;
  esac
  if [ -n "$RUN_HTML_OVERRIDE" ]; then
    run_report="$RUN_HTML_OVERRIDE"
  else
    run_report="${run_dir}/${run_report_name}"
  fi
  if [ -n "$RUN_STATE_OVERRIDE" ]; then
    run_state="$RUN_STATE_OVERRIDE"
  else
    run_state="${run_dir}/${run_state_name}"
  fi
  if [ -n "$FIGURES_DIR_NAME_OVERRIDE" ]; then
    figures_dir_name="$FIGURES_DIR_NAME_OVERRIDE"
  fi
  if [ -n "$REPORT_ASSETS_DIR_NAME_OVERRIDE" ]; then
    report_assets_dir_name="$REPORT_ASSETS_DIR_NAME_OVERRIDE"
  fi
  state_url="$(basename "$run_state")"
  figures_dir_url="./${figures_dir_name}/README.html"
  if [ -d "$run_dir" ]; then
    state_id="$(lookup_state_id_for_run "$run_id")"
    if ! run_history="$(history_for_state "$state_id")"; then
      echo "WARN: history not found for run ${run_id} (state_id=${state_id}); skipping" 1>&2
      return 0
    fi
    ensure_live_round_link "$state_id" "$run_id" "$report_assets_dir_name"
    $PYTHON "${SCRIPT_DIR}/report_render.py" \
      --history "$run_history" \
      --run-index "$RUN_INDEX" \
      --template "$RUN_TEMPLATE" \
      --css "$CSS" \
      --js "$JS" \
      --schema-version "2.0" \
      --state-out "$run_state" \
      --auto-refresh-enabled "$AUTO_REFRESH" \
      --auto-refresh-seconds "$REFRESH_SECONDS" \
      --group-view "$GROUP_VIEW" \
      --figures-dir-name "$figures_dir_name" \
      --report-assets-dir-name "$report_assets_dir_name" \
      --sample-plot-max "$SAMPLE_PLOT_MAX" \
      --state-url "$state_url" \
      --url-prefix "$URL_PREFIX" \
      --run-id-filter "$run_id" \
      --pod5-dir-url "../../../pod5/${run_id}/README.html" \
      --state-dir-url "../../../current/state/${state_id}/README.html" \
      --figures-dir-url "$figures_dir_url" \
      --sample-info-url "../../../sample_info/${run_id}/README.html" \
      --run-config-url "../../../config/${run_id}/README.html" \
      --out "$run_report"
  else
    echo "WARN: run dir not found: $run_dir" 1>&2
  fi
}

if [ -n "$RUN_ID" ]; then
  if [ "$SKIP_RUN_JSON" -ne 1 ]; then
    rebuild_run_json "$RUN_ID"
  fi
elif [ "$ALL_RUNS" -eq 1 ]; then
  if [ "$SKIP_RUN_JSON" -ne 1 ] && [ -d "${OUTDIR%/}/report_html/runs" ]; then
    while IFS= read -r dir; do
      [ -n "$dir" ] || continue
      rebuild_run_json "$(basename "$dir")"
    done < <(find "${OUTDIR%/}/report_html/runs" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
  elif [ "$SKIP_RUN_JSON" -ne 1 ]; then
    echo "WARN: runs directory not found: ${OUTDIR%/}/report_html/runs" 1>&2
  fi
fi

# Render run-specific reports first so their HTML exists before the index page links to them.
if [ -n "$RUN_ID" ]; then
  render_run_report "$RUN_ID"
elif [ "$ALL_RUNS" -eq 1 ]; then
  if [ -d "${OUTDIR%/}/report_html/runs" ]; then
    while IFS= read -r dir; do
      [ -n "$dir" ] || continue
      render_run_report "$(basename "$dir")"
    done < <(find "${OUTDIR%/}/report_html/runs" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort)
  else
    echo "WARN: runs directory not found: ${OUTDIR%/}/report_html/runs" 1>&2
  fi
fi

if [ "$SKIP_ROOT_REPORT" -ne 1 ]; then
  render_root_report
fi
