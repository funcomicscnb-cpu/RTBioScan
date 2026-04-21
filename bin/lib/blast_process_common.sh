#!/usr/bin/env bash

persist_read_ids_ever_state() {
    local round_ids="$1"
    local ever_ids="$2"
    local stats_out="${3:-}"
    if [ -n "$stats_out" ]; then
        perl "${PIPELINE_BASEDIR}/bin/persist_read_ids_ever.pl" "$round_ids" "$ever_ids" "$stats_out"
    else
        perl "${PIPELINE_BASEDIR}/bin/persist_read_ids_ever.pl" "$round_ids" "$ever_ids"
    fi
}

filter_ids_present_in_fasta() {
    local ids_in="$1"
    local fasta_in="$2"
    local out_ids="$3"
    local stats_out="${4:-}"
    if [ -n "$stats_out" ]; then
        perl "${PIPELINE_BASEDIR}/bin/filter_ids_present_in_fasta.pl" "$ids_in" "$fasta_in" "$out_ids" "$stats_out"
    else
        perl "${PIPELINE_BASEDIR}/bin/filter_ids_present_in_fasta.pl" "$ids_in" "$fasta_in" "$out_ids"
    fi
}

replace_read_ids_state() {
    local round_ids="$1"
    local state_ids="$2"
    if [ -s "$round_ids" ]; then
        LC_ALL=C sort -u "$round_ids" > "${state_ids}.new"
        mv "${state_ids}.new" "$state_ids"
    else
        : > "$state_ids"
    fi
}

materialize_round_grace_ids() {
    local prev_ids="$1"
    local round_ids="$2"
    local out_ids="$3"
    : > "$out_ids"
    if [ -s "$prev_ids" ]; then
        cat "$prev_ids" >> "$out_ids"
    fi
    if [ -s "$round_ids" ]; then
        cat "$round_ids" >> "$out_ids"
    fi
    if [ -s "$out_ids" ]; then
        LC_ALL=C sort -u -o "$out_ids" "$out_ids"
    fi
}

refresh_protected_read_ids_ever() {
    local out_path="$1"
    local tmp="${out_path}.tmp.$$"
    : > "$tmp"
    for src in \
        "$ASSIGNED_READ_IDS_EVER_STATE" \
        "$ASSIGNED_OTU_MEMBER_IDS_GRACE_STATE" \
        "$CONSENSUS_ASSIGNED_MEMBER_IDS_GRACE_STATE"; do
        if [ -s "$src" ]; then
            cat "$src" >> "$tmp"
        fi
    done
    if [ -s "$tmp" ]; then
        LC_ALL=C sort -u "$tmp" > "${out_path}.new"
        mv "${out_path}.new" "$out_path"
    else
        : > "$out_path"
    fi
    rm -f "$tmp"
}

now_ms() {
    local stamp="${EPOCHREALTIME:-}"
    if [ -n "$stamp" ]; then
        local sec="${stamp%.*}"
        local frac="${stamp#*.}"
        frac="${frac}000"
        printf '%s\n' "$(( 10#$sec * 1000 + 10#${frac:0:3} ))"
        return
    fi
    perl -MTime::HiRes=time -e 'print int((time()*1000)+0.5), "\n"'
}

append_process_timing() {
    local phase="$1"
    local start_ts="$2"
    local end_ts="$3"
    local seconds=0
    if [ -n "$start_ts" ] && [ -n "$end_ts" ] && [[ "$start_ts" != *[!0-9]* ]] && [[ "$end_ts" != *[!0-9]* ]] && [ "$end_ts" -ge "$start_ts" ]; then
        seconds=$(( end_ts - start_ts ))
    fi
    printf '%s\t%s\t%s\n' "${round_barcode}" "$phase" "$seconds" >> blast_process_timings.tsv
}

append_otu_refine_breakdown() {
    local phase="$1"
    local start_ms="$2"
    local end_ms="$3"
    local elapsed_ms=0
    local seconds=0
    if [ -n "$start_ms" ] && [ -n "$end_ms" ] && [[ "$start_ms" != *[!0-9]* ]] && [[ "$end_ms" != *[!0-9]* ]] && [ "$end_ms" -ge "$start_ms" ]; then
        elapsed_ms=$(( end_ms - start_ms ))
        seconds=$(( elapsed_ms / 1000 ))
    fi
    {
        printf '%s\t%s\t%s\n' "${round_barcode}" "$phase" "$seconds"
    } >> "$OTU_REFINE_PROCESS_BREAKDOWN_FILE" 2>/dev/null || true
    {
        printf '%s\t%s\t%s\t%s\n' "${round_barcode}" "$phase" "$seconds" "$elapsed_ms"
    } >> "$OTU_REFINE_PROCESS_BREAKDOWN_MS_FILE" 2>/dev/null || true
}

append_sup_path_timing() {
    local phase="$1"
    local start_ms="$2"
    local end_ms="$3"
    local elapsed_ms=0
    local seconds=0
    if [ -n "$start_ms" ] && [ -n "$end_ms" ] && [[ "$start_ms" != *[!0-9]* ]] && [[ "$end_ms" != *[!0-9]* ]] && [ "$end_ms" -ge "$start_ms" ]; then
        elapsed_ms=$(( end_ms - start_ms ))
        seconds=$(( elapsed_ms / 1000 ))
    fi
    {
        printf '%s\t%s\t%s\t%s\n' "${round_barcode}" "$phase" "$seconds" "$elapsed_ms"
    } >> "$SUP_PATH_TIMINGS_MS_FILE" 2>/dev/null || true
}
