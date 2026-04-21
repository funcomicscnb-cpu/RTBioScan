#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 4 ]; then
  echo "usage: $0 <lock_target> <wait_seconds> <label> -- <command...>" 1>&2
  exit 2
fi

LOCK_TARGET="$1"
WAIT_SECONDS="$2"
LABEL="$3"
shift 3

if [ "${1:-}" != "--" ]; then
  echo "usage: $0 <lock_target> <wait_seconds> <label> -- <command...>" 1>&2
  exit 2
fi
shift

if [ "$#" -lt 1 ]; then
  echo "ERROR: missing command after --" 1>&2
  exit 2
fi

if ! [[ "$WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  echo "ERROR: wait_seconds must be an integer >= 0, got '$WAIT_SECONDS'" 1>&2
  exit 2
fi

lockdir="${LOCK_TARGET}.lockdir"
mkdir -p "$(dirname "$LOCK_TARGET")"

waited=0
while ! mkdir "$lockdir" 2>/dev/null; do
  sleep 1
  waited=$((waited + 1))
  if [ "$WAIT_SECONDS" -gt 0 ] && [ "$waited" -ge "$WAIT_SECONDS" ]; then
    echo "ERROR: timed out acquiring Dorado lock: $LOCK_TARGET label=$LABEL" 1>&2
    exit 1
  fi
done

cleanup() {
  rmdir "$lockdir" 2>/dev/null || true
}
trap cleanup EXIT

echo "INFO: dorado_lock acquired label=$LABEL at $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)" 1>&2
"$@"
echo "INFO: dorado_lock released label=$LABEL at $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)" 1>&2
