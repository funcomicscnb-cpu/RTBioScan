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

normalized_wait="$WAIT_SECONDS"
while [ "${normalized_wait#0}" != "$normalized_wait" ]; do
  normalized_wait="${normalized_wait#0}"
done
[ -n "$normalized_wait" ] || normalized_wait=0
if [ "${#normalized_wait}" -gt 5 ] || [ "$normalized_wait" -gt 86400 ]; then
  echo "ERROR: wait_seconds exceeds the supported maximum of 86400, got '$WAIT_SECONDS'" 1>&2
  exit 2
fi

mkdir -p "$(dirname "$LOCK_TARGET")"
source "$(dirname "${BASH_SOURCE[0]}")/lib/lock_utils.sh"
init_lock_helpers
[ "$normalized_wait" -ne 0 ] || normalized_wait=86400
trap 'exit 143' TERM

if ! LOCK_WAIT="$normalized_wait" acquire_lock "$LOCK_TARGET"; then
  echo "ERROR: failed acquiring Dorado lock: $LOCK_TARGET label=$LABEL" 1>&2
  exit 1
fi

echo "INFO: dorado_lock acquired label=$LABEL at $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)" 1>&2
rc=0
"$@" || rc=$?
[ "$rc" -eq 0 ] || exit "$rc"
echo "INFO: dorado_lock released label=$LABEL at $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)" 1>&2
