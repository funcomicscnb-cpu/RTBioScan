#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <blast_report_annotated_otu_full.txt>" >&2
  exit 2
fi

blast_report="$1"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -e "$blast_report" ]; then
  echo "ERROR: blast report not found: $blast_report" >&2
  exit 1
fi

if [ ! -f "$blast_report" ]; then
  echo "ERROR: blast report is not a regular file: $blast_report" >&2
  exit 1
fi

if [ ! -r "$blast_report" ]; then
  echo "ERROR: blast report is not readable: $blast_report" >&2
  exit 1
fi

perl "$script_dir/detect_no_adapter_policy.pl" "$blast_report"
