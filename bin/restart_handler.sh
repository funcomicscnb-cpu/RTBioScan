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
}

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

restore_from_root() {
    local root="$1"
    local scope="${2:-all}"   # all | tables_plots | sequences
    [ -d "$root" ] || return 1

    # Prefer the structured snapshot layout used by backup_update_and_clean.
    local restored=0
    local subs=()
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
            items=( "$src"/* )
            if (( ${#items[@]} )); then
                cp -R "${items[@]}" "$ONGOING_STATE/"
                restored=1
            fi
        fi
    done

    # Backward-compatible: if the root doesn't have the structured layout,
    # treat it as a flat snapshot and copy its top-level contents.
    if [ "$restored" -eq 0 ]; then
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

{
    printf 'mode=%s\n' "$MODE"
    printf 'run_name=%s\n' "$RUN_NAME"
} > "$SENTINEL"
