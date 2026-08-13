set -euo pipefail
shopt -s nullglob

MODE="${MODE}"
OUTDIR="${OUTDIR}"
LOCK_WAIT="${LOCK_WAIT}"
RUN_NAME="${RUN_NAME}"
STATE_ID="${STATE_ID}"
FORCE="${FORCE}"

if [ -z "$OUTDIR" ]; then
    exit 0
fi

SENTINEL="${OUTDIR}/temp/.restart_applied.${STATE_ID}"
LOCKDIR="${SENTINEL}.lockdir"

mkdir -p "${OUTDIR}/temp"

waited=0
while ! mkdir "$LOCKDIR" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if (( waited >= LOCK_WAIT )); then
        echo "ERROR: Failed to acquire restart lock: $LOCKDIR" 1>&2
        exit 1
    fi
done
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# If restart has already been applied for this run name and mode, do nothing.
# This avoids repeated wiping/restoring when `-resume` causes multiple pipeline invocations.
if [ "$FORCE" != "1" ] && [ -f "$SENTINEL" ]; then
    prev_mode="$(awk -F= '/^mode=/{print $2; exit}' "$SENTINEL" 2>/dev/null || true)"
    # Guard is keyed by outdir+mode only.
    if [ "$prev_mode" = "$MODE" ]; then
        exit 0
    fi
fi

# Namespace rolling state by STATE_ID to avoid cross-run contamination.
ONGOING="${OUTDIR}/temp/ongoing/state/${STATE_ID}"
LEGACY_CURRENT="${OUTDIR}/temp/current/state/${STATE_ID}"
CURRENT_ROOT="${OUTDIR}/current/state/${STATE_ID}"
ONGOING_STATE="${ONGOING}/_state"

wipe_dir_contents() {
    local dir="$1"
    [ -d "$dir" ] || return 0
    local items=( "$dir"/* )
    if (( ${#items[@]} )); then
        rm -rf "${items[@]}"
    fi
    # Parser-state transaction artifacts are excluded explicitly; do not rely on hidden-dir glob omission.
    rm -rf "$dir/.parser_state_txn" 2>/dev/null || true
}

is_round_lock_namespace() {
    local name="$1"
    case "$name" in
        .round_inflight.*|round_inflight.txt|\
        .round_lock_handoff|.round_lock_handoff.*|\
        .round_lock_release|.round_lock_release.*|\
        .round_lock_finish|.round_lock_finish.*|\
        .round_lock_revocation|.round_lock_revocation.*|\
        .round_lock_events|.round_lock_events.*|\
        .round_lock_operator_events|.round_lock_operator_events.*|\
        .round_lock_operator_pending|.round_lock_operator_pending.*|\
        .round_lock_archives|.round_lock_archives.*)
            return 0
            ;;
    esac
    return 1
}

restart_refuse_path() {
    local path="$1"
    local reason="$2"
    printf "ERROR: restart_mode=%s refuses to mutate round-lock state: %s: %s\n" \
        "$MODE" "$reason" "$path" 1>&2
    return 1
}

# Reset and restore predate the fenced round-lock protocol.  They must never
# erase, copy, or follow that protocol's live state or durable evidence.  Scan
# with dotglob enabled only while collecting each directory's entries so that
# hidden protocol names are inspected without changing wipe/copy semantics.
scan_restart_tree() {
    local root="$1"
    local purpose="$2"
    local reject_symlinks="$3"
    local ignored_presentation_tree="${4:-}"
    local dotglob_was_set=0
    local entries=()
    local entry
    local name

    if [ -L "$root" ]; then
        restart_refuse_path "$root" "unsafe symlink at ${purpose} root"
        return 1
    fi
    [ -e "$root" ] || return 0
    [ -d "$root" ] || return 0

    if shopt -q dotglob; then
        dotglob_was_set=1
    else
        shopt -s dotglob
    fi
    entries=( "$root"/* )
    if [ "$dotglob_was_set" -eq 0 ]; then
        shopt -u dotglob
    fi

    for entry in "${entries[@]}"; do
        name="${entry##*/}"
        if is_round_lock_namespace "$name"; then
            restart_refuse_path "$entry" "protected round-lock namespace in ${purpose}"
            return 1
        fi
        if [ -L "$entry" ]; then
            if [ "$reject_symlinks" -eq 1 ]; then
                if [ -n "$ignored_presentation_tree" ]; then
                    case "$entry" in
                        "$ignored_presentation_tree"/*)
                            # backup_update_and_clean creates this derived link tree.
                            # Restore never copies it, so inspecting a target would
                            # add authority without restoring any rolling state.
                            continue
                            ;;
                    esac
                fi
                restart_refuse_path "$entry" "unsafe symlink in ${purpose}"
                return 1
            fi
            continue
        fi
        if [ -d "$entry" ]; then
            scan_restart_tree "$entry" "$purpose" "$reject_symlinks" \
                "$ignored_presentation_tree" || return 1
        fi
    done
}

# Complete every destructive preflight before the first wipe.  The live state
# tree may contain ordinary symlinks (rm removes the link itself), but restore
# snapshots may not: their copy paths could otherwise traverse or reproduce an
# entry whose contents were not inspected.  The `_state` pathname is also the
# helper's flock fence, so a symlink there is never a valid mutation target.
scan_restart_tree "$ONGOING" "live ongoing state" 0
if [ -L "$ONGOING_STATE" ]; then
    restart_refuse_path "$ONGOING_STATE" "round-lock state-fence path is a symlink"
fi

if [ "$MODE" = "reset" ]; then
    scan_restart_tree "$LEGACY_CURRENT" "reset snapshot state" 0
else
    scan_restart_tree "$LEGACY_CURRENT" "restore snapshot state" 1 \
        "$LEGACY_CURRENT/tables/to_figures"
    scan_restart_tree "$CURRENT_ROOT" "restore snapshot state" 1 \
        "$CURRENT_ROOT/tables/to_figures"
fi

if [ "$MODE" = "reset" ]; then
    wipe_dir_contents "$ONGOING"
    wipe_dir_contents "$LEGACY_CURRENT"
    {
        printf 'mode=%s\n' "$MODE"
        printf 'run_name=%s\n' "$RUN_NAME"
    } > "$SENTINEL"
    exit 0
fi

# MODE=restore
mkdir -p "$ONGOING"
wipe_dir_contents "$ONGOING"
mkdir -p "$ONGOING_STATE"
rm -rf "$ONGOING_STATE/.parser_state_txn" 2>/dev/null || true

restore_from_root() {
    local root="$1"
    local scope="${2:-all}"   # all | tables_plots | sequences
    [ -d "$root" ] || return 1

    # Prefer the structured snapshot layout used by backup_update_and_clean.
    local restored=0
    local structured_layout_seen=0
    local subs=()
    local items=()
    local copy_items=()
    local item
    if [ "$scope" = "tables_plots" ]; then
        subs=( tables plots )
    elif [ "$scope" = "sequences" ]; then
        subs=( sequences )
    else
        subs=( tables plots sequences )
    fi
    for sub in "${subs[@]}"; do
        local src="${root}/${sub}"
        if [ -d "$src" ]; then
            structured_layout_seen=1
            items=( "$src"/* )
            copy_items=()
            for item in "${items[@]}"; do
                # `tables/to_figures` contains derived absolute symlinks for
                # rendering.  The real table files are restored separately.
                if [ "$sub" = "tables" ] && [ "${item##*/}" = "to_figures" ]; then
                    continue
                fi
                copy_items+=( "$item" )
            done
            if (( ${#copy_items[@]} )); then
                cp -R "${copy_items[@]}" "$ONGOING_STATE/"
                restored=1
            fi
        fi
    done

    # Backward-compatible: if the root doesn't have the structured layout,
    # treat it as a flat snapshot and copy its top-level contents.
    if [ "$structured_layout_seen" -eq 0 ]; then
        items=( "$root"/* )
        if (( ${#items[@]} )); then
            cp -R "${items[@]}" "$ONGOING_STATE/"
            restored=1
        fi
    fi

    # Always try to restore done_pod5 tracking if present.
    if [ -f "${root}/done_pod5.txt" ]; then
        mkdir -p "${ONGOING_STATE}"
        cp -f "${root}/done_pod5.txt" "${ONGOING_STATE}/done_pod5.txt"
    fi

    rm -rf "${ONGOING_STATE}/.parser_state_txn" 2>/dev/null || true

    [ "$restored" -eq 1 ]
}

# Restore is a merge:
# - `temp/current` has uncompressed per-round + sequence snapshots (eg. qced_reads_hq_accumulated.fasta)
# - `current` has the rolling aggregate reports (often gzipped) required for cumulative plots
restored_any=0
if restore_from_root "$LEGACY_CURRENT" all; then
    restored_any=1
fi
if restore_from_root "$CURRENT_ROOT" tables_plots; then
    restored_any=1
fi
# If only current/state is available, also restore consensus sequences if present.
if [ -d "$CURRENT_ROOT/sequences/Consensus" ]; then
    mkdir -p "$ONGOING_STATE/Consensus"
    cp -R "$CURRENT_ROOT/sequences/Consensus/." "$ONGOING_STATE/Consensus/" 2>/dev/null || true
    restored_any=1
fi
if [ "$restored_any" -ne 1 ]; then
    echo "WARN: restart_mode=restore requested but no snapshot found at $LEGACY_CURRENT or $CURRENT_ROOT; skipping restore" 1>&2
    exit 0
fi

# If we restored gzipped rolling reports, make sure the expected uncompressed files exist.
# Most of the reporting scripts read/write `*_rpt.txt` directly under `temp/ongoing/`.
for gz in "$ONGOING_STATE"/*.txt.gz "$ONGOING_STATE"/*.tsv.gz "$ONGOING_STATE"/*.csv.gz "$ONGOING_STATE"/*_rpt.txt.gz; do
    [ -f "$gz" ] || continue
    out="${gz%.gz}"
    gunzip -c "$gz" > "$out" || true
done

# Restore consensus artifacts if present in sequence snapshots.
if [ -d "$ONGOING_STATE/single_exp/Consensus" ]; then
    mkdir -p "$ONGOING/Consensus"
    cp -R "$ONGOING_STATE/single_exp/Consensus/." "$ONGOING/Consensus/" 2>/dev/null || true
fi
if [ -d "$ONGOING_STATE/Consensus" ]; then
    mkdir -p "$ONGOING/Consensus"
    cp -R "$ONGOING_STATE/Consensus/." "$ONGOING/Consensus/" 2>/dev/null || true
fi
rm -rf "$ONGOING_STATE/.parser_state_txn" "$ONGOING/.parser_state_txn" 2>/dev/null || true

{
    printf 'mode=%s\n' "$MODE"
    printf 'run_name=%s\n' "$RUN_NAME"
} > "$SENTINEL"
