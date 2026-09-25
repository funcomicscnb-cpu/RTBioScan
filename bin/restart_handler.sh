set -euo pipefail
shopt -s nullglob

MODE="${MODE}"
OUTDIR="${OUTDIR}"
LOCK_WAIT="${LOCK_WAIT}"
RUN_NAME="${RUN_NAME}"
STATE_ID="${STATE_ID}"
FORCE="${FORCE}"
OPERATION_ID="${OPERATION_ID:-}"

if [ -z "$OUTDIR" ]; then
    exit 0
fi

if [ -z "$STATE_ID" ] || [ "$STATE_ID" = "." ] || [ "$STATE_ID" = ".." ]; then
    printf 'ERROR: restart STATE_ID must be one safe pathname component\n' 1>&2
    exit 1
fi
case "$STATE_ID" in
    *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-]*)
        printf 'ERROR: restart STATE_ID must be one safe pathname component\n' 1>&2
        exit 1
        ;;
esac

SENTINEL="${OUTDIR}/temp/.restart_applied.${STATE_ID}"
LOCKDIR="${SENTINEL}.lockdir"
SENTINEL_TMP="${SENTINEL}.tmp.${OPERATION_ID}"
SENTINEL_TMP_OWNED=0
RESTART_HANDLER_SUCCESS=0

case "$MODE" in
    reset|restore)
        ;;
    *)
        printf 'ERROR: restart MODE must be reset or restore\n' 1>&2
        exit 1
        ;;
esac
if [ "${#OPERATION_ID}" -ne 64 ]; then
    printf 'ERROR: restart OPERATION_ID must be exactly 64 lowercase hexadecimal characters\n' 1>&2
    exit 1
fi
case "$OPERATION_ID" in
    *[!0-9a-f]*)
        printf 'ERROR: restart OPERATION_ID must be exactly 64 lowercase hexadecimal characters\n' 1>&2
        exit 1
        ;;
esac
if [ -z "$RUN_NAME" ]; then
    printf 'ERROR: restart RUN_NAME must be nonempty\n' 1>&2
    exit 1
fi
case "$RUN_NAME" in
    *$'\n'*|*$'\r'*)
        printf 'ERROR: restart RUN_NAME must occupy one record line\n' 1>&2
        exit 1
        ;;
esac

mkdir -p "${OUTDIR}/temp"

if [ "${RTB_JOINT_RESTART_OWNER:-}" != "$$" ]; then
waited=0
while ! mkdir "$LOCKDIR" 2>/dev/null; do
    sleep 1
    waited=$((waited + 1))
    if (( waited >= LOCK_WAIT )); then
        echo "ERROR: Failed to acquire restart lock: $LOCKDIR" 1>&2
        exit 1
    fi
done

fi

cleanup_restart_handler() {
    local status="$1"
    trap - EXIT
    set +e
    if [ "$status" -eq 0 ] && [ "$RESTART_HANDLER_SUCCESS" -ne 1 ]; then
        status=1
    fi
    if [ "$SENTINEL_TMP_OWNED" -eq 1 ]; then
        rm -f "$SENTINEL_TMP" 2>/dev/null || true
    fi
    rmdir "$LOCKDIR" 2>/dev/null || true
    exit "$status"
}
trap 'cleanup_restart_handler "$?"' EXIT

sentinel_take_line() {
    case "$SENTINEL_REST" in
        *$'\n'*)
            SENTINEL_LINE="${SENTINEL_REST%%$'\n'*}"
            SENTINEL_REST="${SENTINEL_REST#*$'\n'}"
            return 0
            ;;
    esac
    return 1
}

valid_record_operation_id() {
    local value="$1"
    [ "${#value}" -eq 64 ] || return 1
    case "$value" in
        *[!0-9a-f]*) return 1 ;;
    esac
    return 0
}

valid_record_run_name() {
    local value="$1"
    [ -n "$value" ] || return 1
    case "$value" in
        *$'\n'*|*$'\r'*) return 1 ;;
    esac
    return 0
}

# Parse the complete record rather than extracting one agreeable field.  This
# accepts the historical exact two-line applied sentinel and the schema-2
# applying/applied records, while rejecting partial, extended, or reordered
# records.
parse_restart_sentinel() {
    SENTINEL_REST="$1"
    SENTINEL_KIND=""
    SENTINEL_MODE=""
    SENTINEL_OPERATION_ID=""

    sentinel_take_line || return 1
    local first="$SENTINEL_LINE"
    if [ "$first" = "mode=reset" ] || [ "$first" = "mode=restore" ]; then
        SENTINEL_MODE="${first#mode=}"
        sentinel_take_line || return 1
        case "$SENTINEL_LINE" in
            run_name=?*)
                valid_record_run_name "${SENTINEL_LINE#run_name=}" || return 1
                ;;
            *) return 1 ;;
        esac
        [ -z "$SENTINEL_REST" ] || return 1
        SENTINEL_KIND="legacy-applied"
        return 0
    fi

    [ "$first" = "schema=2" ] || return 1
    sentinel_take_line || return 1
    local status_line="$SENTINEL_LINE"
    sentinel_take_line || return 1
    case "$SENTINEL_LINE" in
        operation_id=*) SENTINEL_OPERATION_ID="${SENTINEL_LINE#operation_id=}" ;;
        *) return 1 ;;
    esac
    valid_record_operation_id "$SENTINEL_OPERATION_ID" || return 1
    sentinel_take_line || return 1
    local mode_line="$SENTINEL_LINE"
    sentinel_take_line || return 1
    case "$SENTINEL_LINE" in
        run_name=?*)
            valid_record_run_name "${SENTINEL_LINE#run_name=}" || return 1
            ;;
        *) return 1 ;;
    esac
    [ -z "$SENTINEL_REST" ] || return 1

    if [ "$status_line" = "status=applying" ]; then
        case "$mode_line" in
            requested_mode=reset|requested_mode=restore)
                SENTINEL_MODE="${mode_line#requested_mode=}"
                SENTINEL_KIND="applying"
                return 0
                ;;
        esac
        return 1
    fi
    if [ "$status_line" = "status=applied" ]; then
        case "$mode_line" in
            mode=reset|mode=restore)
                SENTINEL_MODE="${mode_line#mode=}"
                SENTINEL_KIND="applied"
                return 0
                ;;
        esac
    fi
    return 1
}

write_restart_sentinel() {
    local status="$1"
    if [ -e "$SENTINEL_TMP" ] || [ -L "$SENTINEL_TMP" ]; then
        printf 'ERROR: restart transition temp path already exists: %s\n' \
            "$SENTINEL_TMP" 1>&2
        return 1
    fi

    SENTINEL_TMP_OWNED=1
    if ! (
        set -C
        umask 077
        if [ "$status" = "applying" ]; then
            {
                printf 'schema=2\n'
                printf 'status=applying\n'
                printf 'operation_id=%s\n' "$OPERATION_ID"
                printf 'requested_mode=%s\n' "$MODE"
                printf 'run_name=%s\n' "$RUN_NAME"
            } > "$SENTINEL_TMP"
        else
            {
                printf 'schema=2\n'
                printf 'status=applied\n'
                printf 'operation_id=%s\n' "$OPERATION_ID"
                printf 'mode=%s\n' "$MODE"
                printf 'run_name=%s\n' "$RUN_NAME"
            } > "$SENTINEL_TMP"
        fi
    ); then
        rm -f "$SENTINEL_TMP" 2>/dev/null || true
        SENTINEL_TMP_OWNED=0
        printf 'ERROR: failed to write restart transition temp: %s\n' \
            "$SENTINEL_TMP" 1>&2
        return 1
    fi
    if ! mv -f "$SENTINEL_TMP" "$SENTINEL"; then
        printf 'ERROR: failed to install restart transition record: %s\n' \
            "$SENTINEL" 1>&2
        return 1
    fi
    SENTINEL_TMP_OWNED=0
    if [ "$status" = "applied" ]; then
        RESTART_HANDLER_SUCCESS=1
    fi
}

# If restart has already been applied in the same mode, preserve its operation
# epoch on an ordinary resume.  An applying record is safe to recover by
# repeating the idempotent reset/restore transaction under a new operation ID.
if [ -L "$SENTINEL" ]; then
    printf 'ERROR: restart sentinel is a symlink: %s\n' "$SENTINEL" 1>&2
    exit 1
fi
if [ -e "$SENTINEL" ]; then
    if [ ! -f "$SENTINEL" ]; then
        printf 'ERROR: restart sentinel is not a regular file: %s\n' "$SENTINEL" 1>&2
        exit 1
    fi
    if ! SENTINEL_RAW="$({
        LC_ALL=C perl -MEncode=decode,FB_CROAK -e '
            local $/;
            my $bytes = <>;
            die "missing restart sentinel bytes\n" if !defined($bytes);
            die "NUL in restart sentinel\n" if index($bytes, "\0") >= 0;
            my $validation_copy = $bytes;
            decode("UTF-8", $validation_copy, FB_CROAK);
            print $bytes;
        ' "$SENTINEL" 2>/dev/null || exit 1
        printf x
    })"; then
        printf 'ERROR: failed to read restart sentinel: %s\n' "$SENTINEL" 1>&2
        exit 1
    fi
    SENTINEL_RAW="${SENTINEL_RAW%x}"
    if ! parse_restart_sentinel "$SENTINEL_RAW"; then
        printf 'ERROR: malformed restart sentinel: %s\n' "$SENTINEL" 1>&2
        exit 1
    fi
    if [ "$FORCE" != "1" ] && \
       { [ "$SENTINEL_KIND" = "applied" ] || [ "$SENTINEL_KIND" = "legacy-applied" ]; } && \
       [ "$SENTINEL_MODE" = "$MODE" ]; then
        RESTART_HANDLER_SUCCESS=1
        exit 0
    fi
fi

# Namespace rolling state by STATE_ID to avoid cross-run contamination.
ONGOING="${OUTDIR}/temp/ongoing/state/${STATE_ID}"
LEGACY_CURRENT="${OUTDIR}/temp/current/state/${STATE_ID}"
CURRENT_ROOT="${OUTDIR}/current/state/${STATE_ID}"
ONGOING_STATE="${ONGOING}/_state"

case "${BASH_SOURCE[0]}" in
    /*) RESTART_HANDLER_DIR="${BASH_SOURCE[0]%/*}" ;;
    */*) RESTART_HANDLER_DIR="$PWD/${BASH_SOURCE[0]%/*}" ;;
    *) RESTART_HANDLER_DIR="$PWD" ;;
esac

# The cumulative BLAST OTU generation is restored with its authority, never as
# bare public tables (RTBioScan::R4DCumulative::restore_cli): `prepare`
# authenticates the snapshot's generation read-only, `upgrade` completes a
# proven older backup in place while the live state that proves it still
# exists, `install` writes the generation, sidecar and record into `_state`.
r4d_restore() {
    perl -e 'require $ARGV[0]; exit RTBioScan::R4DCumulative::restore_cli(@ARGV[1 .. $#ARGV]);' \
        "$RESTART_HANDLER_DIR/lib/RTBioScan/R4DCumulative.pm" "$1" "$ONGOING_STATE" "$CURRENT_ROOT" "$LEGACY_CURRENT"
}

# Governed cumulative names of any barcode (tables, sidecar, members, record and
# residue): installed by `r4d_restore install`, never copied by the snapshot copy.
# The authoritative rolling state (accumulated reads, round map, ever-lists,
# prune barrier/archive, streak state, the §6 tables) and its completed-round
# ledger are restored only from a snapshot root's sealed completeness record
# (bin/state_snapshot_authority.pl): verified read-only before the first
# mutation, installed into `_state` after every compatibility copy.
state_authority() {
    perl "$RESTART_HANDLER_DIR/state_snapshot_authority.pl" "$@"
}

state_authority_refuse() {
    printf 'ERROR: restart_mode=restore refuses snapshot %s: %s. It records completed rounds but cannot prove that its accumulated reads, round map, ever-lists, prune and streak state are complete (a snapshot written before this check, or an interrupted backup). It cannot be restored safely: use restart_mode=reset and replay the input POD5 files, or restore from a snapshot a later completed backup has sealed. Reset is eligible only without protected round-lock evidence in affected state or snapshot locations; restore still requires every safety check. Preserve the original state and replay inputs.\n' \
        "$1" "$2" 1>&2
    exit 1
}

restore_skips_governed_name() {
    case "$1" in
        *_blast_otu_pretax_rpt.txt|*_blast_otu_noadapter_rpt.txt|*_blast_otu_reporting_v1.tsv|\
        *_blast_otu_pretax_rpt.txt.*|*_blast_otu_noadapter_rpt.txt.*|*_blast_otu_reporting_v1.tsv.*|\
        *_blast_otu_cumulative.*)
            return 0
            ;;
    esac
    state_authority governed "$1"
}

wipe_dir_contents() {
    local dir="$1"
    [ -d "$dir" ] || return 0
    local items=( "$dir"/* )
    if (( ${#items[@]} )); then
        rm -rf "${items[@]}"
    fi
    # Parser-state transaction artifacts are excluded explicitly; do not rely on hidden-dir glob omission.
    rm -rf "$dir/.parser_state_txn"
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
    printf "ERROR: restart_mode=%s refuses to mutate round-lock state: %s: %s. Stop related writers and preserve the original state; do not delete, edit, or fabricate round-lock records. For protected completed history, use a fresh namespace and reanalyse or a separately reviewed recovery procedure. restart_force does not bypass this check.\n" \
        "$MODE" "$reason" "$path" 1>&2
    return 1
}

# Reset and restore predate the fenced round-lock protocol.  They must never
# erase, copy, or follow that protocol's live state or durable evidence.
# Completed rounds retain protected history, so force cannot make them eligible.
# Scan with dotglob enabled only while collecting each directory's entries so that
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

    for entry in ${entries[@]+"${entries[@]}"}; do
        name="${entry##*/}"
        if [ "$MODE" = "restore" ] && [ "$root" = "$CURRENT_ROOT" ] &&
            { [ -d "$CURRENT_ROOT/tables" ] || [ -d "$CURRENT_ROOT/plots" ]; }; then
            case "$entry" in
                "$CURRENT_ROOT/live_round"|"$CURRENT_ROOT/.live_round_payloads")
                    # These direct current-state entries are presentation-only.
                    # Restore neither inspects nor copies their contents.
                    continue
                    ;;
            esac
        fi
        if is_round_lock_namespace "$name"; then
            restart_refuse_path "$entry" "protected round-lock namespace in ${purpose}"
            return 1
        fi
        if [ -L "$entry" ]; then
            if [ "$reject_symlinks" -eq 1 ]; then
                if [ "$MODE" = restore ] && [ "$purpose" = "restore snapshot state" ] &&
                    state_authority presentation-leaf "$CURRENT_ROOT" "$entry"; then
                    continue
                fi
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

# Keep the existing restart lock while the child retains the two existing
# snapshot flock descriptors through admission and installation. The child
# rechecks all paths and the sentinel; it does not create snapshot roots.
if [ "$MODE" = restore ] && [ "${RTB_JOINT_RESTART_OWNER:-}" != "$$" ]; then
    if state_authority restore-supervise "$STATE_ID" "$ONGOING" "$LEGACY_CURRENT" "$CURRENT_ROOT" \
        /bin/bash "$RESTART_HANDLER_DIR/restart_handler.sh"; then
        RESTART_HANDLER_SUCCESS=1
        exit 0
    else
        exit "$?"
    fi
fi

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
    scan_restart_tree "$LEGACY_CURRENT" "restore snapshot state" 1
    scan_restart_tree "$CURRENT_ROOT" "restore snapshot state" 1 "$CURRENT_ROOT/tables/to_figures"
    authority_choice="$(state_authority select "$STATE_ID" "$ONGOING" "$LEGACY_CURRENT" "$CURRENT_ROOT")"
    case "$authority_choice" in
        0) STATE_AUTHORITY_ROOT="" ;;
        1) STATE_AUTHORITY_ROOT="$LEGACY_CURRENT" ;;
        2) STATE_AUTHORITY_ROOT="$CURRENT_ROOT" ;;
        *) state_authority_refuse "$CURRENT_ROOT" "invalid joint source selection" ;;
    esac
    r4d_restore prepare
fi

# Publish the process-crash-visible operation epoch only after every read-only
# refusal check has passed, and before the first destructive state mutation.
write_restart_sentinel applying

if [ "$MODE" = "reset" ]; then
    wipe_dir_contents "$ONGOING"
    wipe_dir_contents "$LEGACY_CURRENT"
    write_restart_sentinel applied
    exit 0
fi

# MODE=restore
# Complete a proven older backup in place before the live state that proves it
# is wiped.
r4d_restore upgrade
mkdir -p "$ONGOING"
wipe_dir_contents "$ONGOING"
mkdir -p "$ONGOING_STATE"
rm -rf "$ONGOING_STATE/.parser_state_txn"

restore_from_root() {
    local root="$1"
    local scope="${2:-all}"   # all | tables_plots | sequences
    RESTORE_FROM_ROOT_RESULT="absent"
    [ -d "$root" ] || return 0

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
    for sub in ${subs[@]+"${subs[@]}"}; do
        local src="${root}/${sub}"
        if [ -d "$src" ]; then
            structured_layout_seen=1
            items=( "$src"/* )
            copy_items=()
            for item in ${items[@]+"${items[@]}"}; do
                # `tables/to_figures` contains derived absolute symlinks for
                # rendering.  The real table files are restored separately.
                if [ "$sub" = "tables" ] && [ "${item##*/}" = "to_figures" ]; then
                    continue
                fi
                if restore_skips_governed_name "${item##*/}"; then
                    continue
                fi
                copy_items+=( "$item" )
            done
            if (( ${#copy_items[@]} )); then
                for item in "${copy_items[@]}"; do
                    state_authority overlay-copy "$root" "$item" "$ONGOING_STATE/${item##*/}"
                done
                restored=1
            fi
        fi
    done

    # Backward-compatible: if the root doesn't have the structured layout,
    # treat it as a flat snapshot and copy its top-level contents.
    if [ "$structured_layout_seen" -eq 0 ]; then
        items=( "$root"/* )
        copy_items=()
        for item in ${items[@]+"${items[@]}"}; do
            if restore_skips_governed_name "${item##*/}"; then
                continue
            fi
            if [ "${item##*/}" = "state_authority" ]; then
                continue
            fi
            copy_items+=( "$item" )
        done
        if (( ${#copy_items[@]} )); then
            for item in "${copy_items[@]}"; do
                state_authority overlay-copy "$root" "$item" "$ONGOING_STATE/${item##*/}"
            done
            restored=1
        fi
    fi

    # Always try to restore done_pod5 tracking if present.
    if [ -z "$STATE_AUTHORITY_ROOT" ] && [ -f "${root}/done_pod5.txt" ]; then
        mkdir -p "${ONGOING_STATE}"
        cp -f "${root}/done_pod5.txt" "${ONGOING_STATE}/done_pod5.txt"
    fi

    rm -rf "${ONGOING_STATE}/.parser_state_txn"

    if [ "$restored" -eq 1 ]; then
        RESTORE_FROM_ROOT_RESULT="restored"
    fi
}

# Restore is a merge:
# - `temp/current` has uncompressed per-round + sequence snapshots (eg. qced_reads_hq_accumulated.fasta)
# - `current` has the rolling aggregate reports (often gzipped) required for cumulative plots
restored_any=0
restore_from_root "$LEGACY_CURRENT" all
if [ "$RESTORE_FROM_ROOT_RESULT" = "restored" ]; then
    restored_any=1
fi
restore_from_root "$CURRENT_ROOT" tables_plots
if [ "$RESTORE_FROM_ROOT_RESULT" = "restored" ]; then
    restored_any=1
fi
# If only current/state is available, also restore consensus sequences if present.
if [ -d "$CURRENT_ROOT/sequences/Consensus" ]; then
    mkdir -p "$ONGOING_STATE/Consensus"
    state_authority compat-copy "$CURRENT_ROOT/sequences/Consensus" "$ONGOING_STATE/Consensus" 0 0
    restored_any=1
fi
r4d_install_log="$(r4d_restore install)"
printf '%s\n' "$r4d_install_log"
case "$r4d_install_log" in
    *"restore install: installed 0 generation(s)") ;;
    *) restored_any=1 ;;
esac
if [ -n "$STATE_AUTHORITY_ROOT" ]; then
    restored_any=1
fi
if [ "$restored_any" -ne 1 ]; then
    write_restart_sentinel applied
    echo "WARN: restart_mode=restore requested but no snapshot found at $LEGACY_CURRENT or $CURRENT_ROOT; skipping restore" 1>&2
    exit 0
fi

# If we restored gzipped rolling reports, make sure the expected uncompressed files exist.
# Most of the reporting scripts read/write `*_rpt.txt` directly under `temp/ongoing/`.
for gz in "$ONGOING_STATE"/*.txt.gz "$ONGOING_STATE"/*.tsv.gz "$ONGOING_STATE"/*.csv.gz "$ONGOING_STATE"/*_rpt.txt.gz; do
    [ -f "$gz" ] || continue
    out="${gz%.gz}"
    if restore_skips_governed_name "${out##*/}"; then
        continue
    fi
    gunzip -c "$gz" > "$out"
done

# Restore consensus artifacts if present in sequence snapshots.
if [ -d "$ONGOING_STATE/single_exp/Consensus" ]; then
    mkdir -p "$ONGOING/Consensus"
    state_authority compat-copy "$ONGOING_STATE/single_exp/Consensus" "$ONGOING/Consensus" 0 0
fi
if [ -d "$ONGOING_STATE/Consensus" ]; then
    mkdir -p "$ONGOING/Consensus"
    state_authority compat-copy "$ONGOING_STATE/Consensus" "$ONGOING/Consensus" 0 0
fi
rm -rf "$ONGOING_STATE/.parser_state_txn" "$ONGOING/.parser_state_txn"

# Last, so no per-round or compatibility copy of a governed name
# (sequences/qced_reads_hq_accumulated.fasta, tables/round_index.tsv,
# done_pod5.txt) overrides the sealed state.
if [ -n "$STATE_AUTHORITY_ROOT" ]; then
    state_authority install "$STATE_AUTHORITY_ROOT" "$ONGOING_STATE"
fi

write_restart_sentinel applied
