#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
  echo "usage: $0 <blast_report> <no_adapter|sample> [sample_name]" >&2
  exit 2
fi

blast_report="$1"
mode="$2"
sample_name="${3:-}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/adapter_utils.sh"

if [ ! -f "$blast_report" ]; then
  echo "ERROR: blast report is not a regular file: $blast_report" >&2
  exit 1
fi

if [ ! -r "$blast_report" ]; then
  echo "ERROR: blast report is not readable: $blast_report" >&2
  exit 1
fi

case "$mode" in
  no_adapter)
    ;;
  sample)
    if [ -z "$sample_name" ]; then
      echo "ERROR: sample mode requires a sample name" >&2
      exit 1
    fi
    ;;
  *)
    echo "ERROR: unsupported mode: $mode" >&2
    exit 1
    ;;
esac

while IFS= read -r line; do
  [ -n "$line" ] || continue
  read_id="${line%%$'\t'*}"
  adapter="$(extract_adapter_from_read_id_sh "$read_id" 2>/dev/null || true)"
  case "$mode" in
    no_adapter)
      if is_no_adapter_adapter_sh "$adapter"; then
        printf '%s\n' "$line"
      fi
      ;;
    sample)
      if adapter_matches_sample_sh "$sample_name" "$adapter"; then
        printf '%s\n' "$line"
      fi
      ;;
  esac
done < "$blast_report"
