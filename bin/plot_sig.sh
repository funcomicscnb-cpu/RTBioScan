#!/usr/bin/env bash
# plot_sig.sh — per-plot signature guard for getting_run_summary
#
# Usage:
#   plot_sig.sh check  <sig_file> <key> <out_file1> [<out_file2> ...]
#     Exit 0 → should run (key mismatch OR any output file missing)
#     Exit 1 → skip     (key matches AND all output files exist)
#     Artifact paths can be PNG, PDF, or any file type.
#
#   plot_sig.sh commit <sig_file> <key>
#     Atomically writes key to sig_file (PID-scoped tmp + rename)

set -euo pipefail

if [ "$#" -lt 3 ]; then
    echo "usage: $0 check|commit <sig_file> <key> [<out_file> ...]" >&2
    exit 2
fi

cmd="$1"
sig_file="$2"
key="$3"
shift 3

if [ "$cmd" = "commit" ]; then
    mkdir -p "$(dirname "$sig_file")"
    tmp="${sig_file}.tmp.$$"
    printf '%s\n' "$key" > "$tmp"
    mv "$tmp" "$sig_file"
    exit 0
fi

if [ "$cmd" = "check" ]; then
    # Must run if sig file is absent or stale.
    if [ ! -f "$sig_file" ] || [ "$(cat "$sig_file" 2>/dev/null)" != "$key" ]; then
        exit 0
    fi
    # Must run if any mandatory output PNG is missing.
    for out in "$@"; do
        [ -f "$out" ] || exit 0
    done
    exit 1   # all outputs exist and key matches → skip
fi

echo "unknown command: $cmd (expected check or commit)" >&2
exit 2
