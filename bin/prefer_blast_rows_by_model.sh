#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "usage: $0 --input <path> --output <path> --policy <sup_hac2sup_preferred>" >&2
}

input=""
output=""
policy=""

while (($#)); do
  case "$1" in
    --input)
      input="${2:-}"
      shift 2
      ;;
    --output)
      output="${2:-}"
      shift 2
      ;;
    --policy)
      policy="${2:-}"
      shift 2
      ;;
    *)
      usage
      exit 1
      ;;
  esac
done

if [ -z "$input" ] || [ -z "$output" ] || [ -z "$policy" ]; then
  usage
  exit 1
fi

case "$policy" in
  sup_hac2sup_preferred)
    ;;
  *)
    echo "ERROR: unsupported policy '$policy'" >&2
    exit 1
    ;;
esac

if [ ! -f "$input" ]; then
  echo "ERROR: input file not found: $input" >&2
  exit 1
fi

extract_preferred_present() {
  awk '
    function model_from_first_field(line,    first, parts) {
      first = line
      sub(/\t.*$/, "", first)
      sub(/[;,].*$/, "", first)
      split(first, parts, "|")
      return parts[3]
    }
    /^[[:space:]]*$/ || /^#/ { next }
    {
      model = model_from_first_field($0)
      if (model == "sup" || model == "hac2sup") {
        found = 1
        exit
      }
    }
    END { exit(found ? 0 : 1) }
  ' "$1"
}

if extract_preferred_present "$input"; then
  awk '
    function model_from_first_field(line,    first, parts) {
      first = line
      sub(/\t.*$/, "", first)
      sub(/[;,].*$/, "", first)
      split(first, parts, "|")
      return parts[3]
    }
    /^[[:space:]]*$/ || /^#/ { print; next }
    {
      model = model_from_first_field($0)
      if (model == "sup" || model == "hac2sup") {
        print
      }
    }
  ' "$input" > "$output"
else
  cp "$input" "$output"
fi
