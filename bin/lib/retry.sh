#!/usr/bin/env bash

# Retry Dorado basecaller invocations to mitigate transient backend failures.
#
# Usage:
#   dorado_basecall_retry "FAST basecalling" "out.sam" dorado basecaller ... args ...
#
# Notes:
# - Writes to a temporary file and only replaces the final output on success.
# - Sleeps 10s between retryable attempts by default (override with DORADO_RETRY_SLEEP_SECONDS).
# - Unmistakable Dorado CLI/usage errors fail immediately instead of consuming
#   the real-time retry budget.
# - Shell statuses 126 (not executable), 127 (not found), and 132 (SIGILL)
#   are non-retryable for the selected runtime.

dorado_basecall_retry() {
  local desc="$1"
  local out="$2"
  shift 2

  normalize_lock_host() {
    printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]' | sed 's/\..*$//'
  }

  dorado_retry_error_is_permanent() {
    local error_file="$1"
    [ -s "$error_file" ] || return 1
    LC_ALL=C grep -Eiq \
      '(^|[[:space:]])(unknown argument|unrecognized (argument|option)|usage:[[:space:]]+dorado)([[:space:]:]|$)' \
      "$error_file"
  }

  dorado_retry_status_is_permanent() {
    case "$1" in
      126|127|132)
        return 0
        ;;
      *)
        return 1
        ;;
    esac
  }

  local attempt rc
  local tmp="${out}.tmp"
  local err_tmp="${out}.err.tmp"
  local sleep_seconds="${DORADO_RETRY_SLEEP_SECONDS:-10}"
  local attempts="${DORADO_RETRY_ATTEMPTS:-3}"
  if ! echo "$attempts" | awk '/^[0-9]+$/{ok=1} END{exit ok?0:1}'; then
    attempts=3
  fi
  if [ "$attempts" -lt 1 ]; then
    attempts=1
  fi
  if ! echo "$sleep_seconds" | awk '/^[0-9]+$/{ok=1} END{exit ok?0:1}'; then
    sleep_seconds=10
  fi

  # Optional global lock to prevent multiple concurrent Dorado basecaller runs.
  # This is important on macOS/Metal where concurrent basecalling can overload the GPU backend.
  #
  # Set `DORADO_LOCK_PATH` to an absolute path shared by all tasks in the pipeline.
  # The lock is implemented as a directory `DORADO_LOCK_PATH.lockdir` so it works on macOS without `flock`.
  local lock_path="${DORADO_LOCK_PATH:-}"
  local lockdir=""
  local lock_meta=""
  local lock_wait="${DORADO_LOCK_WAIT_SECONDS:-21600}"   # 6 hours
  local lock_ttl="${DORADO_LOCK_TTL_SECONDS:-28800}"     # 8 hours

  if [ -n "$lock_path" ]; then
    lockdir="${lock_path}.lockdir"
    lock_meta="${lockdir}/meta.env"
    mkdir -p "$(dirname "$lockdir")" 2>/dev/null || true

    # Try to acquire; if the lock looks stale (crash), reclaim based on PID or age.
    local waited=0
    local host this_host this_host_norm host_norm now started pid
    this_host="$(hostname 2>/dev/null || uname -n 2>/dev/null || echo unknown)"
    this_host_norm="$(normalize_lock_host "$this_host")"
    # Inside containers, PIDs are namespaced, so PID-based stale detection is unreliable.
    # Prefer TTL-based reclaim there.
    local in_container=0
    if [ -f "/.dockerenv" ]; then
      in_container=1
    elif [ -r "/proc/1/cgroup" ] && grep -qaE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null; then
      in_container=1
    fi
    while ! mkdir "$lockdir" 2>/dev/null; do
      pid=""
      host=""
      started=""
      if [ -f "$lock_meta" ]; then
        pid="$(awk -F= '/^pid=/{print $2; exit}' "$lock_meta" 2>/dev/null || true)"
        host="$(awk -F= '/^host=/{print $2; exit}' "$lock_meta" 2>/dev/null || true)"
        started="$(awk -F= '/^started_epoch=/{print $2; exit}' "$lock_meta" 2>/dev/null || true)"
      fi

      now="$(date +%s 2>/dev/null || echo 0)"
      host_norm="$(normalize_lock_host "$host")"
      if [ "$in_container" -eq 0 ] && [ -n "$pid" ] && [ -n "$host" ] && \
         { [ "$host" = "$this_host" ] || { [ -n "$host_norm" ] && [ "$host_norm" = "$this_host_norm" ]; }; } && \
         echo "$pid" | awk '/^[0-9]+$/{exit 0} {exit 1}'; then
        if ! kill -0 "$pid" 2>/dev/null; then
          echo "WARN: Reclaiming stale Dorado lock (dead pid=$pid host=$host) at $lockdir" 1>&2
          rm -rf "$lockdir" 2>/dev/null || true
          continue
        fi
      fi
      if [ "$lock_ttl" -gt 0 ] && [ -n "$started" ] && echo "$started" | awk '/^[0-9]+$/{exit 0} {exit 1}' && [ "$now" -gt 0 ]; then
        if [ $(( now - started )) -ge "$lock_ttl" ]; then
          echo "WARN: Reclaiming stale Dorado lock (age=$(( now - started ))s ttl=${lock_ttl}s) at $lockdir" 1>&2
          rm -rf "$lockdir" 2>/dev/null || true
          continue
        fi
      fi

      sleep 5
      waited=$(( waited + 5 ))
      if [ $(( waited % 60 )) -eq 0 ]; then
        echo "INFO: Waiting for Dorado lock ($lockdir)" 1>&2
      fi
      if [ "$lock_wait" -gt 0 ] && [ "$waited" -ge "$lock_wait" ]; then
        echo "ERROR: Timed out waiting for Dorado lock ($lockdir)" 1>&2
        return 1
      fi
    done

    # Record lock owner metadata (best effort).
    {
      printf "pid=%s\n" "$$"
      printf "host=%s\n" "$this_host"
      printf "started_epoch=%s\n" "$(date +%s 2>/dev/null || echo 0)"
      printf "desc=%s\n" "$desc"
      printf "out=%s\n" "$out"
    } > "$lock_meta" 2>/dev/null || true

    # Ensure the lock is released when the function returns.
    # NOTE: the lockdir contains `meta.env`, so remove it first.
    trap 'rm -f "$lock_meta" 2>/dev/null || true; rmdir "$lockdir" 2>/dev/null || true' RETURN
  fi

  for (( attempt=1; attempt<=attempts; attempt++ )); do
    rm -f "$tmp"
    rm -f "$err_tmp"
    echo "INFO: Dorado ${desc} attempt ${attempt}/${attempts}" 1>&2

    set +e
    "$@" > "$tmp" 2> "$err_tmp"
    rc=$?
    set -e
    if [ -s "$err_tmp" ]; then
      cat "$err_tmp" 1>&2
    fi

    if [ "$rc" -eq 0 ] && [ -s "$tmp" ]; then
      mv "$tmp" "$out"
      rm -f "$err_tmp"
      return 0
    fi

    echo "WARN: Dorado ${desc} failed (attempt ${attempt}/${attempts})" 1>&2
    if dorado_retry_status_is_permanent "$rc"; then
      echo "ERROR: Dorado ${desc} exited with non-retryable status ${rc}; not retrying" 1>&2
      return 1
    fi
    if dorado_retry_error_is_permanent "$err_tmp"; then
      echo "ERROR: Dorado ${desc} reported a non-retryable CLI/configuration error; not retrying" 1>&2
      return 1
    fi
    if [ "$attempt" -lt "$attempts" ]; then
      sleep "$sleep_seconds"
    fi
  done

  echo "ERROR: Dorado ${desc} failed after ${attempts} attempts" 1>&2
  return 1
}
