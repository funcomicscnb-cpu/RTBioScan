#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 3 ]; then
  echo "usage: $0 configured_mode skip_rounds round_index" >&2
  exit 2
fi

configured_mode_raw="$1"
skip_rounds_raw="$2"
round_index_raw="$3"

configured_mode="$(printf '%s' "$configured_mode_raw" | tr '[:upper:]' '[:lower:]')"
skip_rounds="$(printf '%s' "$skip_rounds_raw" | tr '[:upper:]' '[:lower:]')"

case "$configured_mode" in
  off|observe|enforce) ;;
  *)
    echo "Invalid configured_mode '$configured_mode_raw' (expected off|observe|enforce)" >&2
    exit 2
    ;;
esac

if ! [[ "$round_index_raw" =~ ^[0-9]+$ ]] || [ "$round_index_raw" -lt 1 ]; then
  echo "Invalid round_index '$round_index_raw' (expected integer >= 1)" >&2
  exit 2
fi
round_index="$round_index_raw"

if [ "$skip_rounds" = "0" ] || [ -z "$skip_rounds" ]; then
  skip_rounds="none"
fi

effective_mode="$configured_mode"
reason="no_skip_window"

if [ "$skip_rounds" = "all" ]; then
  effective_mode="off"
  reason="skip_all_rounds"
elif [ "$skip_rounds" = "none" ]; then
  effective_mode="$configured_mode"
  reason="skip_none"
elif [[ "$skip_rounds" =~ ^[0-9]+$ ]]; then
  if [ "$skip_rounds" -eq 0 ]; then
    effective_mode="$configured_mode"
    reason="skip_zero"
  elif [ "$round_index" -le "$skip_rounds" ]; then
    effective_mode="off"
    reason="within_skip_window"
  else
    effective_mode="$configured_mode"
    reason="after_skip_window"
  fi
else
  echo "Invalid skip_rounds '$skip_rounds_raw' (expected none|all|integer>=0)" >&2
  exit 2
fi

printf "effective_mode\t%s\n" "$effective_mode"
printf "reason\t%s\n" "$reason"
printf "configured_mode\t%s\n" "$configured_mode"
printf "skip_rounds\t%s\n" "$skip_rounds"
printf "round_index\t%s\n" "$round_index"
