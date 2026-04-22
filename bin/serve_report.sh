#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: serve_report.sh [--dir DIR] [--port PORT] [--host HOST] [--report FILE] [--wait-for-report] [--open] [--open-all|--open-last N] [--quiet|--verbose] [--ready-file FILE]

Serve the RTBioScan HTML report with a local HTTP server.

Defaults:
  --dir    results
  --port   8000
  --host   127.0.0.1
  --report report_html/report.html
  --wait-for-report  start serving before report_html/report.html exists
  --open-all  open all run reports under DIR/report_html/runs
  --open-last N  open last N run reports under DIR/report_html/runs
  --quiet  reduce request logging
USAGE
}

DIR="results"
PORT="8000"
HOST="127.0.0.1"
REPORT="report_html/report.html"
WAIT_FOR_REPORT=0
OPEN=0
QUIET=0
OPEN_ALL=0
OPEN_LAST=""
READY_FILE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)
      DIR="${2:-}"
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    --host)
      HOST="${2:-}"
      shift 2
      ;;
    --report)
      REPORT="${2:-}"
      shift 2
      ;;
    --wait-for-report)
      WAIT_FOR_REPORT=1
      shift
      ;;
    --open)
      OPEN=1
      shift
      ;;
    --open-all)
      OPEN_ALL=1
      shift
      ;;
    --open-last)
      OPEN_LAST="${2:-}"
      shift 2
      ;;
    --quiet)
      QUIET=1
      shift
      ;;
    --verbose)
      QUIET=0
      shift
      ;;
    --ready-file)
      READY_FILE="${2:-}"
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

if [ -z "$DIR" ] || [ -z "$PORT" ] || [ -z "$HOST" ] || [ -z "$REPORT" ]; then
  echo "ERROR: --dir, --port, --host, and --report must be set" 1>&2
  usage 1>&2
  exit 2
fi

if [ "$OPEN_ALL" -eq 1 ] && [ -n "$OPEN_LAST" ]; then
  echo "ERROR: use only one of --open-all or --open-last" 1>&2
  exit 2
fi

if [ -n "$OPEN_LAST" ] && ! [[ "$OPEN_LAST" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --open-last must be a positive integer" 1>&2
  exit 2
fi
if [ -n "$OPEN_LAST" ] && [ "$OPEN_LAST" -eq 0 ]; then
  echo "ERROR: --open-last must be >= 1" 1>&2
  exit 2
fi

if ! [[ "$PORT" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --port must be an integer between 1 and 65535" 1>&2
  exit 2
fi
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "ERROR: --port must be an integer between 1 and 65535" 1>&2
  exit 2
fi

REPORT_PATH="${DIR%/}/${REPORT}"
if [ "$WAIT_FOR_REPORT" -ne 1 ] && [ ! -f "$REPORT_PATH" ]; then
  echo "ERROR: report not found: $REPORT_PATH" 1>&2
  exit 2
fi
if [ -n "$READY_FILE" ]; then
  case "$READY_FILE" in
    /*) ;;
    *) READY_FILE="$(pwd -P)/$READY_FILE" ;;
  esac
  mkdir -p "$(dirname "$READY_FILE")"
  rm -f "$READY_FILE"
fi

BIND_HOST="$HOST"
case "$BIND_HOST" in
  \[*\])
    BIND_HOST="${BIND_HOST#\[}"
    BIND_HOST="${BIND_HOST%\]}"
    ;;
esac

BROWSER_HOST="$HOST"
case "$HOST" in
  ""|"0.0.0.0"|"::"|"[::]"|"*")
    BROWSER_HOST="127.0.0.1"
    ;;
esac
if [[ "$BROWSER_HOST" == *:* ]] && [[ "$BROWSER_HOST" != \[*\] ]]; then
  BROWSER_HOST="[$BROWSER_HOST]"
fi

PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  py_major="$(python -c 'import sys; print(sys.version_info[0])' 2>/dev/null || true)"
  if [ "$py_major" = "3" ]; then
    PY="python"
  else
    echo "ERROR: python is not Python 3; please install python3" 1>&2
    exit 2
  fi
else
  echo "ERROR: python3 not found in PATH" 1>&2
  exit 2
fi

URL="http://${BROWSER_HOST}:${PORT}/${REPORT}"
echo "Serving report from: ${REPORT_PATH}" 1>&2
echo "Open: ${URL}" 1>&2
if [ "$QUIET" -eq 0 ]; then
  echo "Note: reports built with root-absolute URLs (for example --url-prefix /) require HTTP serving; relative-path reports work with file://" 1>&2
fi
if [ "$WAIT_FOR_REPORT" -eq 1 ] && [ ! -f "$REPORT_PATH" ]; then
  echo "Waiting for report to appear: ${REPORT_PATH}" 1>&2
fi

collect_run_urls() {
  if [ "$OPEN_ALL" -ne 1 ] && [ -z "$OPEN_LAST" ]; then
    return 0
  fi
  if [ ! -d "${DIR%/}/report_html/runs" ]; then
    return 0
  fi
  "$PY" - <<PY
import os
import sys
from pathlib import Path

base = Path("${DIR}").resolve()
run_root = base / "report_html" / "runs"
items = []
if run_root.is_dir():
    for path in run_root.glob("*/report.html"):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0
        items.append((mtime, path))
items.sort(key=lambda x: x[0])
if "${OPEN_LAST}" != "":
    n = int("${OPEN_LAST}")
    if n > 0:
        items = items[-n:]
for _, path in items:
    try:
        rel = path.relative_to(base)
    except Exception:
        continue
    print(f"http://${BROWSER_HOST}:${PORT}/{rel.as_posix()}")
PY
}

OPEN_CMD=""
if [ "$OPEN" -eq 1 ] || [ "$OPEN_ALL" -eq 1 ] || [ -n "$OPEN_LAST" ]; then
  if command -v open >/dev/null 2>&1; then
    OPEN_CMD="open"
  elif command -v xdg-open >/dev/null 2>&1; then
    OPEN_CMD="xdg-open"
  else
    echo "WARN: could not auto-open browser (open/xdg-open not found)" 1>&2
  fi
fi

open_run_url() {
  local _url="$1"
  [ -n "$_url" ] || return 0
  if [ "$OPEN_CMD" = "open" ]; then
    open -g "$_url" >/dev/null 2>&1 || true
  else
    "$OPEN_CMD" "$_url" >/dev/null 2>&1 || true
  fi
}

open_index_url() {
  local _url="$1"
  [ -n "$_url" ] || return 0
  "$OPEN_CMD" "$_url" >/dev/null 2>&1 || true
}

OPEN_WAIT_PID=""
launch_open_helper() {
  [ -n "$OPEN_CMD" ] || return 0
  (
    warned_missing_runs=0
    opened_runs=0
    opened_index=0
    while :; do
      run_urls=""
      if [ "$OPEN_ALL" -eq 1 ] || [ -n "$OPEN_LAST" ]; then
        if [ "$opened_runs" -eq 0 ]; then
          run_urls="$(collect_run_urls)"
          if [ -n "$run_urls" ]; then
            while IFS= read -r u; do
              [ -n "$u" ] || continue
              open_run_url "$u"
            done <<EOF
$run_urls
EOF
            count="$(printf "%s\n" "$run_urls" | sed '/^$/d' | wc -l | tr -d ' ')"
            if [ "$count" -gt 30 ]; then
              echo "WARN: opening ${count} run reports (may be heavy)" 1>&2
            fi
            opened_runs=1
          else
            if [ "$WAIT_FOR_REPORT" -eq 1 ]; then
              :
            elif [ "$warned_missing_runs" -eq 0 ]; then
              echo "WARN: no run reports found under ${DIR%/}/runs" 1>&2
              warned_missing_runs=1
              opened_runs=1
            fi
          fi
        fi
      else
        opened_runs=1
      fi

      if [ "$opened_index" -eq 0 ] && [ "$opened_runs" -eq 1 ] && [ -f "$REPORT_PATH" ]; then
        open_index_url "$URL"
        opened_index=1
      fi

      if [ "$opened_runs" -eq 1 ] && [ "$opened_index" -eq 1 ]; then
        break
      fi
      sleep 1
    done
  ) &
  OPEN_WAIT_PID=$!
}

cleanup() {
  if [ -n "${OPEN_WAIT_PID:-}" ] && kill -0 "$OPEN_WAIT_PID" 2>/dev/null; then
    kill "$OPEN_WAIT_PID" 2>/dev/null || true
    wait "$OPEN_WAIT_PID" 2>/dev/null || true
  fi
  if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  [ -n "${READY_FILE:-}" ] && rm -f "$READY_FILE" 2>/dev/null || true
}
trap cleanup EXIT

cd "$DIR"
RTBIOSCAN_READY_FILE="$READY_FILE" "$PY" - <<PY &
import errno
import http.server
import os
import socket
import socketserver
import sys

host = "${BIND_HOST}"
port = int("${PORT}")
quiet = ${QUIET}
family = socket.AF_INET6 if ":" in host and host not in ("0.0.0.0", "") else socket.AF_INET

class ReportHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        if not quiet:
            super().log_message(format, *args)

    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except OSError as exc:
            if exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED):
                pass
            else:
                raise

class ReportServer(socketserver.TCPServer):
    address_family = family

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        if isinstance(exc, OSError) and exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ECONNABORTED):
            return
        super().handle_error(request, client_address)

with ReportServer((host, port), ReportHandler) as httpd:
    ready_file = os.environ.get("RTBIOSCAN_READY_FILE", "")
    if ready_file:
        with open(ready_file, "w", encoding="utf-8") as fh:
            fh.write(f"{host}\t{port}\n")
    httpd.serve_forever()
PY
SERVER_PID=$!

if [ -n "$OPEN_CMD" ]; then
  # Do not open browser tabs until the HTTP server has actually bound its port.
  # If binding fails, the wrapper may retry another port; opening here would
  # launch a stale refused-connection URL for the failed attempt.
  if [ -n "$READY_FILE" ]; then
    open_waited=0
    while [ ! -s "$READY_FILE" ] && kill -0 "$SERVER_PID" 2>/dev/null; do
      sleep 1
      open_waited=$((open_waited + 1))
      [ "$open_waited" -lt 5 ] || break
    done
    if [ ! -s "$READY_FILE" ]; then
      kill "$SERVER_PID" 2>/dev/null || true
      wait "$SERVER_PID" 2>/dev/null || true
      exit 1
    fi
  else
    sleep 1
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    wait "$SERVER_PID"
    exit "$?"
  fi
fi

launch_open_helper

set +e
wait "$SERVER_PID"
rc=$?
set -e
exit "$rc"
