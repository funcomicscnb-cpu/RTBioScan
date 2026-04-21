#!/usr/bin/env bash

backup_sync_can_use_rsync() {
    if [ "${BACKUP_SYNC_DISABLE_RSYNC:-0}" = "1" ]; then
        return 1
    fi
    command -v rsync >/dev/null 2>&1
}

sync_changed_files() {
    local dest_dir="$1"
    shift || true

    mkdir -p "$dest_dir"

    local existing=()
    local src
    for src in "$@"; do
        [ -e "$src" ] || continue
        existing+=( "$src" )
    done

    if [ "${#existing[@]}" -eq 0 ]; then
        return 0
    fi

    if backup_sync_can_use_rsync; then
        if rsync -a --checksum -- "${existing[@]}" "$dest_dir"/; then
            return 0
        fi
        echo "WARN: rsync failed for explicit file sync into $dest_dir; falling back to cp -f" >&2
    fi

    for src in "${existing[@]}"; do
        cp -f -- "$src" "$dest_dir"/
    done
}

sync_changed_tree() {
    local src_dir="$1"
    local dest_dir="$2"

    [ -d "$src_dir" ] || return 0
    mkdir -p "$dest_dir"

    if backup_sync_can_use_rsync; then
        if rsync -a --checksum -- "$src_dir"/ "$dest_dir"/; then
            return 0
        fi
        echo "WARN: rsync failed for tree sync $src_dir -> $dest_dir; falling back to cp -Rp" >&2
    fi

    cp -Rp -- "$src_dir"/. "$dest_dir"/
}

publish_gzip_atomic() {
    local src_file="$1"
    local dest_gz="$2"
    local dest_dir tmp uncompressed spill_root spill_tmp

    [ -e "$src_file" ] || return 0

    dest_dir="$(dirname "$dest_gz")"
    mkdir -p "$dest_dir"
    uncompressed="${dest_gz%.gz}"

    if [ -f "$dest_gz" ] && gzip -cd -- "$dest_gz" 2>/dev/null | cmp -s -- "$src_file" -; then
        if [ "$uncompressed" != "$dest_gz" ]; then
            rm -f -- "$uncompressed"
        fi
        return 0
    fi

    tmp="${dest_gz}.tmp.$$"

    if gzip -cn -- "$src_file" > "$tmp"; then
        mv -f -- "$tmp" "$dest_gz"
    else
        rm -f -- "$tmp"
        spill_root="${TMPDIR:-/tmp}"
        spill_tmp=""
        if spill_tmp="$(mktemp "$spill_root/rtbioscan_gzip.XXXXXX" 2>/dev/null)" \
            && gzip -cn -- "$src_file" > "$spill_tmp"; then
            echo "WARN: atomic gzip publish failed for $dest_gz; retrying via $spill_root" >&2
            if ! cat "$spill_tmp" > "$dest_gz"; then
                rm -f -- "$spill_tmp"
                return 1
            fi
            rm -f -- "$spill_tmp"
        else
            if [ -n "$spill_tmp" ]; then
                rm -f -- "$spill_tmp"
            fi
            return 1
        fi
    fi

    if [ "$uncompressed" != "$dest_gz" ]; then
        rm -f -- "$uncompressed"
    fi
}
