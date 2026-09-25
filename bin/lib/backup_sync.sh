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
    local r4d_sources=()
    local src
    for src in "$@"; do
        # A governed cumulative table is decided by its source's authority, never
        # by its own presence: a publication may have just retracted the name.
        if r4d_cumulative_governed "$src"; then
            r4d_already_backed_up "$dest_dir" "$src" || r4d_sources+=( "$src" )
            continue
        fi
        [ -e "$src" ] || continue
        existing+=( "$src" )
    done

    if [ "${#r4d_sources[@]}" -gt 0 ]; then
        r4d_publish_backup "$dest_dir" "${r4d_sources[@]}" || return 1
    fi

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

# R4-I2: the cumulative BLAST OTU public tables (`<bc>_blast_otu_pretax_rpt.txt`,
# `<bc>_blast_otu_noadapter_rpt.txt`) of a directory under generation authority
# -- a pipeline `_state`, or a directory holding an R4-I2 record, member, lock
# or temp, such as an earlier backup destination -- are never copied one by
# one. The generation named by the source's sealed record (or its validated
# legacy snapshot) is replicated into the destination as one generation, its
# reporting sidecar included, with its own record
# (RTBioScan::R4DCumulative::publish_backup): the tables change together, a
# killed sync leaves the previous generation authoritative, and every later
# copy of the destination repeats this. Damage or ambiguity fails the sync.
# backup_update_and_clean names each source directory and barcode explicitly
# (r4d_backup_generation), so no glob or existence test decides whether a
# generation is backed up; a governed name handed to sync_changed_files is
# resolved through its source's authority even when it is momentarily absent.
# Other files, and these names in other directories, are synced exactly as
# before.
case "${BASH_SOURCE[0]}" in
    /*) BACKUP_SYNC_LIB_DIR="${BASH_SOURCE[0]%/*}" ;;
    */*) BACKUP_SYNC_LIB_DIR="$PWD/${BASH_SOURCE[0]%/*}" ;;
    *) BACKUP_SYNC_LIB_DIR="$PWD" ;;
esac

r4d_cumulative_governed() {
    local src="$1"
    local name="${src##*/}"
    local dir="${src%/*}"
    local bc m

    case "$name" in
        *_blast_otu_pretax_rpt.txt) bc="${name%_blast_otu_pretax_rpt.txt}" ;;
        *_blast_otu_noadapter_rpt.txt) bc="${name%_blast_otu_noadapter_rpt.txt}" ;;
        *) return 1 ;;
    esac
    [ -n "$bc" ] || return 1
    [ "$dir" != "$src" ] || dir="."
    if [ "${dir##*/}" = "_state" ]; then
        return 0
    fi
    for m in "$dir/${bc}_blast_otu_cumulative."* "$dir/.r4d-publish-${bc}-"* \
        "$dir/${bc}_blast_otu_reporting_v1.tsv.gen-"* "$dir/${bc}_blast_otu_pretax_rpt.txt.gen-"* \
        "$dir/${bc}_blast_otu_noadapter_rpt.txt.gen-"*; do
        if [ -e "$m" ] || [ -L "$m" ]; then
            return 0
        fi
    done
    return 1
}

r4d_publish_backup() {
    local dest_dir="$1"
    shift
    perl -e 'require $ARGV[0]; exit RTBioScan::R4DCumulative::backup_cli(@ARGV[1 .. $#ARGV]);' \
        "$BACKUP_SYNC_LIB_DIR/RTBioScan/R4DCumulative.pm" "$dest_dir" "$@"
}

# Generations published by r4d_backup_generation in this shell, as
# `|<dest>|<source dir>|<barcode>|`: the per-file sync of the same source into
# the same destination does not repeat them.
R4D_BACKED_UP="${R4D_BACKED_UP:-}"

# r4d_backup_generation <dest dir> <source dir> <barcode>: back up the
# source's cumulative generation of that barcode (resolved from its authority;
# nothing when it has never been published; a failure when it is damaged or
# not authentic).
r4d_backup_generation() {
    local dest_dir="$1"
    local src_dir="$2"
    local bc="$3"
    mkdir -p "$dest_dir"
    perl -e 'require $ARGV[0]; exit RTBioScan::R4DCumulative::backup_generation_cli(@ARGV[1 .. $#ARGV]);' \
        "$BACKUP_SYNC_LIB_DIR/RTBioScan/R4DCumulative.pm" "$dest_dir" "$src_dir" "$bc" || return 1
    R4D_BACKED_UP="${R4D_BACKED_UP}|${dest_dir}|${src_dir}|${bc}|"
}

r4d_already_backed_up() {
    local dest_dir="$1"
    local src="$2"
    local name="${src##*/}"
    local dir="${src%/*}"
    local bc
    case "$name" in
        *_blast_otu_pretax_rpt.txt) bc="${name%_blast_otu_pretax_rpt.txt}" ;;
        *_blast_otu_noadapter_rpt.txt) bc="${name%_blast_otu_noadapter_rpt.txt}" ;;
        *) return 1 ;;
    esac
    [ "$dir" != "$src" ] || dir="."
    case "$R4D_BACKED_UP" in
        *"|${dest_dir}|${dir}|${bc}|"*) return 0 ;;
    esac
    return 1
}


# Joint-v2 canonical Consensus state is transported only by the authority
# transaction. Other Consensus products keep their existing backup behavior.
sync_joint_consensus_compat() {
    local src_dir="$1" dest_dir="$2" child name
    [ -d "$src_dir" ] || return 0
    mkdir -p "$dest_dir"
    local dotglob_was_set=0
    shopt -q dotglob && dotglob_was_set=1
    shopt -s dotglob
    for child in "$src_dir"/*; do
        [ -e "$child" ] || [ -L "$child" ] || continue
        name="${child##*/}"
        case "$name" in
            .cache|consensus_ownership.tsv|consolidated_consensus_ids.txt) continue ;;
        esac
        perl "$BACKUP_SYNC_LIB_DIR/../state_snapshot_authority.pl" compat-copy \
            "$child" "$dest_dir/$name" 0 0 || return 1
    done
    [ "$dotglob_was_set" -eq 1 ] || shopt -u dotglob
}
