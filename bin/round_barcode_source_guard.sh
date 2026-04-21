#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat 1>&2 <<'EOF'
Usage: round_barcode_source_guard.sh --state-dir DIR --round-barcode NAME --read-file PATH
EOF
}

state_dir=""
round_barcode=""
read_file=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        --state-dir)
            state_dir="${2:-}"
            shift 2
            ;;
        --round-barcode)
            round_barcode="${2:-}"
            shift 2
            ;;
        --read-file)
            read_file="${2:-}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "ERROR: unknown argument '$1'" 1>&2
            usage
            exit 1
            ;;
    esac
done

if [ -z "$state_dir" ] || [ -z "$round_barcode" ] || [ -z "$read_file" ]; then
    echo "ERROR: --state-dir, --round-barcode, and --read-file are required" 1>&2
    usage
    exit 1
fi

if [ ! -e "$read_file" ]; then
    echo "ERROR: read file does not exist: $read_file" 1>&2
    exit 1
fi

if [ ! -f "$read_file" ]; then
    echo "ERROR: read file is not a regular file: $read_file" 1>&2
    exit 1
fi

mkdir -p "$state_dir"

read_dir="$(cd "$(dirname "$read_file")" && pwd -P)"
read_abs="${read_dir}/$(basename "$read_file")"
map_file="${state_dir}/round_barcode_sources.tsv"
lock_dir="${state_dir}/.round_barcode_sources.lockdir"
lock_meta="${lock_dir}/meta.env"
lock_wait_seconds="${ROUND_BARCODE_GUARD_LOCK_WAIT:-30}"
stale_lock_ttl_seconds="${ROUND_BARCODE_GUARD_STALE_TTL:-300}"
this_host="${HOSTNAME:-$(hostname 2>/dev/null || uname -n 2>/dev/null || echo unknown)}"

case "$lock_wait_seconds" in
    ''|*[!0-9]*)
        echo "ERROR: invalid ROUND_BARCODE_GUARD_LOCK_WAIT '$lock_wait_seconds'; expected integer >= 0" 1>&2
        exit 1
        ;;
esac

case "$stale_lock_ttl_seconds" in
    ''|*[!0-9]*)
        echo "ERROR: invalid ROUND_BARCODE_GUARD_STALE_TTL '$stale_lock_ttl_seconds'; expected integer >= 0" 1>&2
        exit 1
        ;;
esac

case "$round_barcode" in
    *$'\t'*|*$'\n'*)
        echo "ERROR: round_barcode contains unsupported tab/newline characters: '$round_barcode'" 1>&2
        exit 1
        ;;
esac

case "$read_abs" in
    *$'\t'*|*$'\n'*)
        echo "ERROR: canonical read path contains unsupported tab/newline characters: '$read_abs'" 1>&2
        exit 1
        ;;
esac

guard_lock_held=0
cleanup_guard_lock() {
    if [ "$guard_lock_held" -eq 1 ]; then
        rm -f "$lock_meta" 2>/dev/null || true
        rmdir "$lock_dir" 2>/dev/null || true
        guard_lock_held=0
    fi
}
trap cleanup_guard_lock EXIT
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$script_dir/lib/stale_lock_utils.sh"

reclaim_guard_lock_if_stale() {
    rm -f "$lock_meta" 2>/dev/null || true
    rmdir "$lock_dir" 2>/dev/null || true
}

waited=0
while ! mkdir "$lock_dir" 2>/dev/null; do
    reclaimed=0
    set +e
    stale_lock_maybe_reclaim \
        "$lock_dir" \
        "$lock_meta" \
        "$this_host" \
        "$stale_lock_ttl_seconds" \
        "round barcode source guard lock" \
        reclaim_guard_lock_if_stale \
        0
    reclaim_status=$?
    set -e
    if [ "$reclaim_status" -eq 2 ]; then
        echo "ERROR: stale_lock_maybe_reclaim rejected round barcode source guard lock parameters" 1>&2
        exit 1
    fi
    if [ "$reclaim_status" -eq 10 ]; then
        reclaimed=1
    fi
    if [ "$reclaimed" -eq 1 ]; then
        continue
    fi
    if [ "$lock_wait_seconds" -eq 0 ] || [ "$waited" -ge "$lock_wait_seconds" ]; then
        echo "ERROR: timed out acquiring round barcode source guard lock at $lock_dir" 1>&2
        exit 1
    fi
    sleep 1
    waited=$(( waited + 1 ))
done
guard_lock_held=1
{
    printf 'pid=%s\n' "$$"
    printf 'host=%s\n' "$this_host"
    printf 'started_epoch=%s\n' "$(date +%s 2>/dev/null || echo 0)"
} > "$lock_meta"

if [ -f "$map_file" ]; then
    existing_entries="$(awk -F'\t' -v rb="$round_barcode" '
        $1 != rb { next }
        NF < 2 || $2 == "" { printf "ERR\t%s\t%s\n", NR, $0; next }
        { printf "OK\t%s\n", $2 }
    ' "$map_file")"
    if [ -n "$existing_entries" ]; then
        while IFS=$'\t' read -r entry_type entry_value entry_rest; do
            [ -n "$entry_type" ] || continue
            if [ "$entry_type" = "ERR" ]; then
                echo "ERROR: malformed round_barcode_sources.tsv entry for '$round_barcode' at line $entry_value: ${entry_rest:-<empty>}" 1>&2
                exit 1
            fi
            if [ "$entry_value" != "$read_abs" ]; then
                echo "ERROR: round_barcode collision for '$round_barcode': existing source '$entry_value' conflicts with '$read_abs'" 1>&2
                exit 1
            fi
        done <<< "$existing_entries"
        printf '%s\n' "$read_abs"
        exit 0
    fi
fi

printf '%s\t%s\n' "$round_barcode" "$read_abs" >> "$map_file"
printf '%s\n' "$read_abs"
