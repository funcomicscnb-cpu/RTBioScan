#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DETECT_CONSENSUS_SAMPLE_MODE_PROG="$0" exec perl "$script_dir/detect_consensus_sample_mode.pl" "$@"
