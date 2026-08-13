import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN = REPO_ROOT / "main.nf"


# This is an independent inventory of every process in main.nf. FAST owns
# acquisition; the next twelve processes can read or write round-owned state.
# HAC basecalling is stateless with respect to that state, and async rendering
# consumes an immutable request after the round boundary.
ACQUISITION_PROCESS = "fast_on_target_detection"
GENERATION_BOUND_PROCESSES = {
    "_reporting_fast_on_target": "reporting_fast_on_target",
    "_reporting_hac_basecalling": "reporting_hac_basecalling",
    "demultiplexing_hq_reads": "demultiplexing_hq_reads",
    "_reporting_hq_demultiplexing": "reporting_hq_demultiplexing",
    "OTU_definition": "OTU_definition",
    "_reporting_OTU_definition": "reporting_OTU_definition",
    "blast_OTU_pretax": "blast_OTU_pretax",
    "_reporting_blast_pretax": "reporting_blast_pretax",
    "consensus": "consensus",
    "_reporting_consensus_tax": "reporting_consensus_tax",
    "getting_run_summary": "getting_run_summary",
    "backup_update_and_clean": "backup_update_and_clean",
}
INTENTIONAL_EXCLUSIONS = {"hac_basecalling", "async_report_render"}
PRE_BACKUP_WRITERS = tuple(
    process
    for process in GENERATION_BOUND_PROCESSES
    if process not in {"getting_run_summary", "backup_update_and_clean"}
)


def _process_blocks(text: str) -> dict[str, str]:
    matches = list(re.finditer(r"(?m)^process ([A-Za-z_][A-Za-z0-9_]*) \{", text))
    blocks: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        name = match.group(1)
        assert name not in blocks, name
        blocks[name] = text[match.start() : end]
    return blocks


def _compact(text: str) -> str:
    return " ".join(text.split())


def test_process_inventory_and_generation_fence_boundaries_are_explicit() -> None:
    text = MAIN.read_text(encoding="utf-8")
    blocks = _process_blocks(text)
    expected = (
        {ACQUISITION_PROCESS}
        | set(GENERATION_BOUND_PROCESSES)
        | INTENTIONAL_EXCLUSIONS
    )
    assert set(blocks) == expected
    assert len(GENERATION_BOUND_PROCESSES) == 12
    assert len(expected) == 15

    fast = blocks[ACQUISITION_PROCESS]
    assert 'source "${baseDir}/bin/round_lock_process_guard.sh"' in fast
    assert "rtbioscan_round_lock_pin " not in fast

    for process, role in GENERATION_BOUND_PROCESSES.items():
        block = blocks[process]
        input_section = block.split("output:", 1)[0]
        assert "val(round_generation_token)" in input_section, process
        assert "val(round_lock_scope)" in input_section, process
        assert (
            'export RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE='
            f'".rtbioscan-round-lock-pin.{role}.\\$\\$"'
        ) in block, process
        source = 'source "${baseDir}/bin/round_lock_process_guard.sh"'
        pin = f"rtbioscan_round_lock_pin {role}"
        assert block.count(source) == 1, process
        assert block.count(pin) == 1, process
        assert block.index(source) < block.index(pin), process

        if process == "backup_update_and_clean":
            assert "cache false" in block.split("input:", 1)[0]
            assert "rtbioscan_round_lock_unpin" not in block
        else:
            assert block.count("rtbioscan_round_lock_unpin") == 1, process

    for process in INTENTIONAL_EXCLUSIONS:
        block = blocks[process]
        assert "round_generation_token" not in block, process
        assert "RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE" not in block, process
        assert "round_lock_process_guard.sh" not in block, process
        assert "rtbioscan_round_lock_pin" not in block, process

    assert text.count('source "${baseDir}/bin/round_lock_process_guard.sh"') == 13
    assert text.count("rtbioscan_round_lock_pin ") == 12
    assert text.count("rtbioscan_round_lock_unpin") == 11


def test_generation_context_follows_success_and_failed_round_routes() -> None:
    text = MAIN.read_text(encoding="utf-8")
    required_routes = (
        "tuple env(barcode), env(round_barcode), val(read_path), "
        "env(round_generation_token), env(round_lock_scope) into failed_round_source_ch",
        'env(round_generation_token), env(round_lock_scope) into on_target_report',
        'env(round_generation_token), env(round_lock_scope) into round_generation_ch',
        "tuple val(barcode), val(round_barcode), val(round_generation_token), "
        "val(round_lock_scope) into fast_control",
        "hac_reads_with_generation = ChannelUtils.strictRoundJoin("
        "barcode_annotate, round_generation_ch, 'hac_reads_with_generation')",
        "hq_reads_report_with_fast_control = ChannelUtils.strictRoundJoin("
        "hq_reads_report, fast_control)",
        'file("${barcode}_hac_sup_annotated_clean.fasta"), '
        "val(round_generation_token), val(round_lock_scope) into otu_analysis",
        'file("${barcode}_hac_sup_annotated_clean.fastq"), '
        "val(round_generation_token), val(round_lock_scope) into annotate_reads_report",
        "file(fasta_hq_qced), file(\"${barcode}_qced_reads_nr.fasta.clstr\"), "
        "val(round_generation_token), val(round_lock_scope) into fastq_qced_blast",
        "file(fasta_hq_qced), val(round_generation_token), "
        "val(round_lock_scope) into fastq_qced_consensus",
        'file("${barcode}_qced_reads_nr.fasta.clstr"), '
        "val(round_generation_token), val(round_lock_scope) into report_otu",
        'file("${barcode}_blast_filter_stats.tsv"), '
        "val(round_generation_token), val(round_lock_scope) into report_blast",
        "file(blast_filter_stats), val(round_generation_token), "
        "val(round_lock_scope) into blst_rpt_summary",
        'file("consensus_round_provenance.tsv"), '
        "val(round_generation_token), val(round_lock_scope) into report_consensus",
        "tuple val(barcode), val(round_barcode), val(round_generation_token), "
        "val(round_lock_scope) into complete_round_ch",
        "tuple val(barcode), val(round_barcode), val(round_generation_token), "
        "val(round_lock_scope), val(read_path) from complete_round_with_path",
    )
    compact = _compact(text)
    for route in required_routes:
        assert _compact(route) in compact, route

    # Every failed-round fan-out consumes the authenticated generation context,
    # and exactly the blast-summary leg carries it into the final keyed join.
    failed_start = text.index("failed_round_ch = failed_round_source_ch")
    failed_end = text.index(
        "// ============================================================\n"
        "// STAGE B",
        failed_start,
    )
    failed = text[failed_start:failed_end]
    closure = (
        ".map { barcode, round_barcode, round_failed_file, "
        "round_generation_token, round_lock_scope ->"
    )
    assert _compact(failed).count(_compact(closure)) == 9
    authenticated_placeholder = (
        "[barcode, round_barcode, failedRoundPlaceholderAssets.blastOtuPretaxRpt, "
        "failedRoundPlaceholderAssets.readInfoRpt, "
        "failedRoundPlaceholderAssets.blastOtuNoadapterRpt, "
        "failedRoundPlaceholderAssets.blastFilterStats, "
        "round_generation_token, round_lock_scope]"
    )
    assert _compact(authenticated_placeholder) in _compact(failed)
    fanout = failed[failed.index("failed_blst_rpt_summary =") :]
    assert _compact(fanout).count("round_generation_token, round_lock_scope]") == 1


def test_summary_and_backup_wait_for_every_pre_backup_writer_on_failed_rounds() -> None:
    text = MAIN.read_text(encoding="utf-8")
    blocks = _process_blocks(text)

    # Positive control: this is the shortcut whose placeholder rows can reach
    # getting_run_summary before the normal generation-bound branches finish.
    assert "failed_round_ch = failed_round_source_ch" in text
    assert ".mix(fallbackCh)" in text

    done_channels = []
    for process in PRE_BACKUP_WRITERS:
        role = GENERATION_BOUND_PROCESSES[process]
        channel = f"round_lock_done_{role}_ch"
        done_channels.append(channel)
        assert (
            "tuple val(barcode), val(round_barcode) into " + channel
            in blocks[process]
        ), process

    drain = "round_lock_writers_drained_ch = ChannelUtils.strictRoundJoinAll(["
    assert drain in text
    drain_start = text.index(drain)
    drain_end = text.index("]", drain_start)
    drain_body = text[drain_start:drain_end]
    for channel in done_channels:
        assert drain_body.count(channel) == 1, channel
    assert "'round_lock_writers_drained'" in text[drain_start:drain_end + 80]

    gated_join_start = text.index(
        "getting_run_summary_inputs_drained = ChannelUtils.strictRoundJoin("
    )
    gated_join_end = text.index("\n)", gated_join_start) + 2
    gated_join = text[gated_join_start:gated_join_end]
    assert "getting_run_summary_inputs" in gated_join
    assert "round_lock_writers_drained_ch" in gated_join
    assert "'getting_run_summary_inputs_drained'" in gated_join
    assert (
        "getting_run_summary_with_path = ChannelUtils.strictRoundJoin("
        "getting_run_summary_inputs_drained, get_summary_ch)"
        in _compact(text)
    )
    backup_input = (
        "tuple val(barcode), val(round_barcode), val(round_generation_token), "
        "val(round_lock_scope), val(read_path) from "
        "complete_round_with_path"
    )
    assert _compact(backup_input) in _compact(blocks["backup_update_and_clean"])


def test_fast_acquire_retains_and_replays_exact_explicit_tokens() -> None:
    text = MAIN.read_text(encoding="utf-8")
    fast = _process_blocks(text)[ACQUISITION_PROCESS]
    header = fast.split("input:", 1)[0]
    assert "cache false" in header

    generation_file = (
        "ROUND_LOCK_GENERATION_TOKEN_FILE="
        '".rtbioscan-round-lock-acquire-generation.\\$\\$"'
    )
    acquisition_file = (
        "ROUND_LOCK_ACQUISITION_PIN_FILE="
        '".rtbioscan-round-lock-acquire-pin.\\$\\$"'
    )
    assert generation_file in fast
    assert acquisition_file in fast
    assert fast.count("rtbioscan_round_lock_prepare_token_file") == 4
    assert r'"acquire-generation:\$round_barcode:\$ROUND_LOCK_SCOPE:\$\$"' in fast
    assert r'"acquire-pin:\$round_barcode:\$ROUND_LOCK_SCOPE:\$\$"' in fast
    assert (
        r'if [ "\$round_generation_token_retained" = '
        r'"\$round_acquisition_pin_retained" ]; then'
    ) in fast

    acquire_start = fast.index("round_lock_acquire_once() {")
    acquire_end = fast.index("\n\t\t\t\t\t}", acquire_start) + 7
    acquire = fast[acquire_start:acquire_end]
    assert r'perl "\$ROUND_LOCK_HELPER" "\$round_lock_acquire_command"' in acquire
    assert r'--token "\$round_generation_token_retained"' in acquire
    assert r'--pin-token "\$round_acquisition_pin_retained"' in acquire
    assert r'--owner-pid "\$\$"' in acquire
    assert r'--wait-seconds "\$ROUND_LOCK_WAIT_SECONDS"' in acquire
    assert r'--stale-seconds "\$ROUND_LOCK_STALE_SECONDS"' in acquire
    assert fast.index(generation_file) < acquire_start
    assert fast.index(acquisition_file) < acquire_start

    first_attempt = r'ROUND_LOCK_ACQUIRE_OUTPUT="\$(round_lock_acquire_once acquire)"'
    replay_attempt = r'ROUND_LOCK_ACQUIRE_OUTPUT="\$(round_lock_acquire_once replay-acquire)"'
    assert fast.count(first_attempt) == 1
    assert fast.count(replay_attempt) == 1
    first_index = fast.index(first_attempt)
    replay_index = fast.index(replay_attempt)
    assert first_index < replay_index
    assert "|| \\" in fast[first_index:replay_index]

    assert "unexpected round-lock acquisition field" in fast
    assert "duplicate round-lock generation token" in fast
    assert "duplicate round-lock acquisition pin" in fast
    assert r'if [ "\$round_lock_field_count" -ne 2 ]; then' in fast
    assert r'if [ "\${#round_generation_token}" -ne 64 ] || ' in fast
    assert (
        r'if [ "\$round_generation_token" != '
        r'"\$round_generation_token_retained" ] ||'
    ) in fast
    assert (
        r'[ "\$round_acquisition_pin" != '
        r'"\$round_acquisition_pin_retained" ] ||'
    ) in fast
    assert "round-lock helper response does not match retained acquire tokens" in fast


def test_fast_cleanup_state_machine_has_one_failure_only_handoff_cancel() -> None:
    text = MAIN.read_text(encoding="utf-8")
    fast = _process_blocks(text)[ACQUISITION_PROCESS]

    acquisition_flag = fast.index("round_lock_acquired=0")
    default_abort = fast.index("round_lock_cleanup=abort")
    trap_definition = fast.index("round_lock_cleanup_on_exit() {")
    trap_install = fast.index("trap round_lock_cleanup_on_exit EXIT")
    acquire = fast.index("round_lock_acquire_once() {")
    assert acquisition_flag < default_abort < trap_definition < trap_install < acquire

    cleanup = fast[trap_definition:trap_install]
    for state in ("abort)", "cancel-dorado-handoff)", "none)"):
        assert cleanup.count(state) == 1, state
    assert r'perl "\$ROUND_LOCK_HELPER" abort' in cleanup
    assert r'perl "\$ROUND_LOCK_HELPER" cancel-handoff' in cleanup
    assert "--best-effort" in cleanup
    assert r'if [ "\$round_lock_acquired" -eq 1 ]; then' in cleanup
    assert fast.count(r'perl "\$ROUND_LOCK_HELPER" cancel-handoff') == 1

    authenticated = fast.index("round_lock_acquired=1")
    response_bound = fast.index(
        "round-lock helper response does not match retained acquire tokens"
    )
    first_state_mutation = fast.index("READ_FILE_ABS=")
    assert response_bound < authenticated < first_state_mutation

    early_release = fast.index(r'perl "\$ROUND_LOCK_HELPER" early-release')
    cancel_state = fast.index("round_lock_cleanup=cancel-dorado-handoff")
    handoff = fast.index(r'perl "\$ROUND_LOCK_HELPER" handoff')
    no_cleanup = fast.index("round_lock_cleanup=none", handoff)
    assert cancel_state < early_release < handoff < no_cleanup
    assert fast.count("round_lock_cleanup=abort") == 1
    assert fast.count("round_lock_cleanup=cancel-dorado-handoff") == 1
    assert fast.count("round_lock_cleanup=none") == 1


def test_fast_and_backup_have_no_owner_blind_round_namespace_mutation() -> None:
    text = MAIN.read_text(encoding="utf-8")
    blocks = _process_blocks(text)
    relevant = blocks[ACQUISITION_PROCESS] + blocks["backup_update_and_clean"]

    for legacy in (
        "ROUND_LOCKDIR",
        "ROUND_LOCK_HANDOFF_FILE",
        "LOCK_META",
        "remove_round_lock_if_stale",
        "release_round_lock_now",
        "release_round_lock()",
        "stale_lock_maybe_reclaim",
        r'.round_lock_handoff.\$round_barcode',
        '.round_lock_handoff.${round_barcode}',
    ):
        assert legacy not in relevant, legacy
    raw_round_namespace = re.compile(
        r"\.round_(?:inflight|lock_(?:handoff|release|finish|revocation|events|"
        r"operator_events|operator_pending|archives))"
    )
    assert raw_round_namespace.search(relevant) is None

    owner_blind_mutation = re.compile(
        r"(?m)^\s*(?:rm|rmdir|mv)\b[^\n]*"
        r"(?:\.round_inflight|\.round_lock_(?:handoff|release|finish|revocation|"
        r"events|operator|archives))"
    )
    assert owner_blind_mutation.search(relevant) is None

    # This assertion is deliberately scoped to the round-owner spans. The
    # async report lock has a separate, unchanged stale-reclaim protocol.
    assert "stale_lock_maybe_reclaim" in blocks["async_report_render"]


def test_backup_completion_skip_and_finish_are_generation_authenticated() -> None:
    text = MAIN.read_text(encoding="utf-8")
    backup = _process_blocks(text)["backup_update_and_clean"]

    assert "cache false" in backup.split("input:", 1)[0]
    assert "rtbioscan_round_lock_pin backup_update_and_clean" in backup
    assert "rtbioscan_round_lock_unpin" not in backup
    assert "RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED=" not in backup

    skip = r'if [ "\${RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED:-0}" -ne 1 ]; then'
    section_2 = "-- §2: Rolling state publish"
    section_6 = "-- §6: State-tables snapshot + round-lock release"
    finish = r'perl "\$RTBIOSCAN_ROUND_LOCK_HELPER" finish'
    assert backup.count(skip) == 1
    assert backup.index(skip) < backup.index(section_2) < backup.index(section_6)
    assert backup.index(section_6) < backup.index(finish)
    skip_tail = backup[
        backup.index('if (( \\${#state_tables[@]} )); then') : backup.index(
            "# ---- Release round lock"
        )
    ]
    assert _compact(skip_tail).endswith("fi fi")

    # Active full-round completion is authorized by the final writer pin.
    finish_positions = [match.start() for match in re.finditer(re.escape(finish), backup)]
    assert len(finish_positions) == 3
    active_finish = backup[finish_positions[1] : finish_positions[2]]
    for argument in (
        r'--state-dir "\$RTBIOSCAN_ROUND_LOCK_STATE_DIR"',
        r'--round-barcode "\$RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE"',
        r'--scope "\$RTBIOSCAN_ROUND_LOCK_SCOPE"',
        r'--token "\$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN"',
        r'--pin-token "\$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN"',
    ):
        assert argument in active_finish
    dorado_finish = backup[finish_positions[2] :]
    assert r'--token "\$RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN"' in dorado_finish
    assert r'--pin-token "\$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN"' not in dorado_finish

    # An authenticated final receipt makes resumed backup idempotent. Dorado
    # still runs backup on an early-release receipt and only then records finish.
    assert (
        r'if [ "\$RTBIOSCAN_ROUND_LOCK_SCOPE" = "full_round" ]; then'
    ) in backup
    assert (
        r'if [ "\${RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED:-0}" -eq 1 ]; then'
    ) in backup
    assert "authenticated completed round lacks done_pod5.txt state" in backup


def test_generation_token_changes_cache_keys_without_replacing_restart_token() -> None:
    text = MAIN.read_text(encoding="utf-8")
    assert (
        'SUP_CACHE_RESTART_TOKEN="${restartTokenForCache ?: workflow.runName}"'
        in text
    )
    assert 'SUP_CACHE_RESTART_TOKEN="${round_generation_token}' not in text
    assert text.count("val(round_generation_token)") == 22
