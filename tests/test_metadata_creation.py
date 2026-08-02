"""Integration tests for the --do_metadata --skip_pod5 path in
bin/Metadata_pod5_processing.sh.

Tests cover the current sample-info contract:
- headered metadata rows are filtered by Run
- exact-token demux matching prefers WELL_PLATE over RAW_REPLICATE_WELL_PLATE
- marker resolution uses linked primer sequences and the normalized --targets allowlist
- demult.fasta remains sample-collapsed while replicate sidecars stay additive
- samples.txt is emitted as a canonical sorted-unique 7-field compatibility projection
- {run_id}_metadata.txt remains a filtered TSV copy of the selected metadata rows
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "Metadata_pod5_processing.sh"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "metadata"


def _run_metadata(
    tmp_path: Path,
    run_id: str = "TestRun",
    metadata_file: str = "pipeline_info.tsv",
    general_fasta: str | Path = "general_unique.fasta",
    primers_fasta: str = "primers.fasta",
    targets: str | None = "COI|ITS2",
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run metadata creation only."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    run_env = os.environ.copy()
    if env:
        run_env.update(env)
    cmd = [
        "bash",
        str(SCRIPT),
        "--run_id",
        run_id,
        "--do_metadata",
        "--skip_pod5",
        "--metadata",
        str(metadata_file if isinstance(metadata_file, Path) else FIXTURES / metadata_file),
        "--general_fasta",
        str(general_fasta if isinstance(general_fasta, Path) else FIXTURES / general_fasta),
        "--primers_fasta",
        str(primers_fasta if isinstance(primers_fasta, Path) else FIXTURES / primers_fasta),
    ]
    if targets is not None:
        cmd.extend(["--targets", targets])
    return subprocess.run(
        cmd,
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=run_env,
    )


def test_metadata_generation_happy_path_outputs(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path)
    assert result.returncode == 0, f"Script failed:\n{result.stdout}\n{result.stderr}"

    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    demult = sample_info / "demult.fasta"
    samples_file = sample_info / "samples.txt"
    metadata_file = sample_info / "TestRun_metadata.txt"
    roster_file = sample_info / "replicate_roster.tsv"
    identity_file = sample_info / "replicate_identity.tsv"

    assert demult.exists(), "demult.fasta not created"
    assert samples_file.exists(), "samples.txt not created"
    assert metadata_file.exists(), "TestRun_metadata.txt not created"
    assert roster_file.exists(), "replicate_roster.tsv not created"
    assert identity_file.exists(), "replicate_identity.tsv not created"

    demult_lines = demult.read_text(encoding="utf-8").splitlines()
    assert len(demult_lines) == 12
    assert [line for line in demult_lines if line.startswith(">")] == [
        ">SampleA_COI",
        ">SampleA_ITS2",
        ">SampleA_COI",
        ">SampleA_ITS2",
        ">SampleB_COI",
        ">SampleB_ITS2",
    ]

    assert samples_file.read_text(encoding="utf-8").splitlines() == [
        "SampleA SampleA_r1 1 A1 TestPlate TestRun >A1_TestPlate",
        "SampleA SampleA_r2 2 A2 TestPlate TestRun >A2_TestPlate",
        "SampleB SampleB_r1 1 B1 TestPlate TestRun >B1_TestPlate",
    ]
    assert metadata_file.read_text(encoding="utf-8").splitlines() == [
        "SampleA\tSampleA_r1\t1\tA1\tTestPlate\tTestRun\t>A1_TestPlate",
        "SampleA\tSampleA_r2\t2\tA2\tTestPlate\tTestRun\t>A2_TestPlate",
        "SampleB\tSampleB_r1\t1\tB1\tTestPlate\tTestRun\t>B1_TestPlate",
    ]

    assert roster_file.read_text(encoding="utf-8").splitlines() == [
        "sample_id\treplicate_id\treplicate_number\twell\tplate\trun_id\tdemult_id_metadata",
        "SampleA\tSampleA_r1\t1\tA1\tTestPlate\tTestRun\t>A1_TestPlate",
        "SampleA\tSampleA_r2\t2\tA2\tTestPlate\tTestRun\t>A2_TestPlate",
        "SampleB\tSampleB_r1\t1\tB1\tTestPlate\tTestRun\t>B1_TestPlate",
    ]

    identity_lines = identity_file.read_text(encoding="utf-8").splitlines()
    assert len(identity_lines) == 7
    assert identity_lines[0] == (
        "sample_id\treplicate_id\treplicate_number\tmarker_id\tmatched_general_fasta_header\t"
        "matched_general_fasta_record_index\tsuffix_resolution_mode\tunit_suffix_current\t"
        "unit_id_collapse\tunit_id_track\tdemult_id_metadata\tlookup_key_primary\t"
        "lookup_key_fallback\tlookup_grammar_used\tmetadata_line_no"
    )
    rows = [line.split("\t") for line in identity_lines[1:]]
    assert [row[3] for row in rows] == ["COI", "ITS2", "COI", "ITS2", "COI", "ITS2"]
    assert [row[7] for row in rows] == ["COI", "ITS2", "COI", "ITS2", "COI", "ITS2"]
    assert [row[12] for row in rows] == [
        "1_A1_TestPlate",
        "1_A1_TestPlate",
        "2_A2_TestPlate",
        "2_A2_TestPlate",
        "1_B1_TestPlate",
        "1_B1_TestPlate",
    ]
    assert [row[13] for row in rows] == [
        "WELL_PLATE",
        "WELL_PLATE",
        "RAW_REPLICATE_WELL_PLATE",
        "RAW_REPLICATE_WELL_PLATE",
        "WELL_PLATE",
        "WELL_PLATE",
    ]
    assert all(row[6] == "marker" for row in rows)
    assert rows[-1][8] == "SampleB_ITS2"
    assert rows[-1][9] == "SampleB_r1_ITS2"

    primers_file = sample_info / "primers.fasta"
    assert primers_file.exists(), "primers.fasta not created"
    primers_text = primers_file.read_text(encoding="utf-8")
    primers_headers = [l for l in primers_text.splitlines() if l.startswith(">")]
    assert primers_headers == [
        ">COI_BC.COIv1.BC9.1",
        ">COI_BC.COIv1.BC9.2",
        ">ITS2_BC.ITS2v1.BC9.1",
    ], f"primers.fasta has wrong entries: {primers_headers}"
    assert ">COI_BC.COIv1.BC9.3" not in primers_text
    assert ">ITS2_BC.ITS2v1.UNRELATED" not in primers_text


def test_track_metadata_rejects_fallback_suffix_resolution(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path, general_fasta="general.fasta")
    assert result.returncode != 0
    assert "fallback suffixes are not permitted for track identity artifacts" in result.stderr


def test_track_identity_exact_duplicate_rows_collapse_with_source_lines(tmp_path: Path) -> None:
    metadata = tmp_path / "pipeline_info_duplicate.tsv"
    metadata.write_text(
        "Sample_ID\tPipeline_ID\tReplicate\tWell\tPlate\tRun\tdemult_id\n"
        "SampleA\tSampleA_r1\t1\tA1\tTestPlate\tTestRun\t>A1_TestPlate\n"
        "SampleA\tSampleA_r1\t1\tA1\tTestPlate\tTestRun\t>A1_TestPlate\n",
        encoding="utf-8",
    )

    result = _run_metadata(tmp_path, metadata_file=metadata)
    assert result.returncode == 0, f"Script failed:\n{result.stdout}\n{result.stderr}"

    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    track_identity = sample_info / "track_identity.tsv"
    lines = track_identity.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    rows = [line.split("\t") for line in lines[1:]]
    assert [row[9] for row in rows] == ["SampleA_r1_COI", "SampleA_r1_ITS2"]
    assert all(row[15] == "exact_duplicate_collapsed" for row in rows)
    assert all(row[17] == "3" for row in rows)


def test_track_identity_conflicting_duplicate_unit_fails(tmp_path: Path) -> None:
    metadata = tmp_path / "pipeline_info_conflict.tsv"
    metadata.write_text(
        "Sample_ID\tPipeline_ID\tReplicate\tWell\tPlate\tRun\tdemult_id\n"
        "SampleA\tShared_r1\t1\tA1\tTestPlate\tTestRun\t>A1_TestPlate\n"
        "SampleB\tShared_r1\t1\tB1\tTestPlate\tTestRun\t>B1_TestPlate\n",
        encoding="utf-8",
    )

    result = _run_metadata(tmp_path, metadata_file=metadata)
    assert result.returncode != 0
    assert "track identity duplicate conflict for unit_id_track Shared_r1_COI" in result.stderr


def test_track_identity_accepts_heterogeneous_marker_composition(tmp_path: Path) -> None:
    general = tmp_path / "general_heterogeneous_markers.fasta"
    general.write_text(
        ">A1_TestPlate\n"
        "COI_LEFT...COI_RIGHT\n"
        ">A1_TestPlate\n"
        "ITS2_LEFT...ITS2_RIGHT\n"
        ">2_A2_TestPlate\n"
        "COI_LEFT...COI_RIGHT\n"
        ">2_A2_TestPlate\n"
        "ITS2_LEFT...ITS2_RIGHT\n"
        ">B1_TestPlate\n"
        "COI_LEFT...COI_RIGHT\n",
        encoding="utf-8",
    )

    result = _run_metadata(tmp_path, general_fasta=general)
    assert result.returncode == 0, f"Script failed:\n{result.stdout}\n{result.stderr}"

    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    track_identity = sample_info / "track_identity.tsv"
    track_active_units = sample_info / "track_active_units.txt"
    track_demult = sample_info / "track_demult.fasta"
    track_roster = sample_info / "track_roster.tsv"

    assert track_identity.exists(), "track_identity.tsv not created"
    identity_rows = track_identity.read_text(encoding="utf-8").splitlines()
    header = identity_rows[0].split("\t")
    track_col = header.index("track_id")
    marker_col = header.index("marker_id")
    observed_pairs = {
        (row.split("\t")[track_col], row.split("\t")[marker_col])
        for row in identity_rows[1:]
        if row.strip()
    }
    assert observed_pairs == {
        ("SampleA_r1", "COI"),
        ("SampleA_r1", "ITS2"),
        ("SampleA_r2", "COI"),
        ("SampleA_r2", "ITS2"),
        ("SampleB_r1", "COI"),
    }

    assert track_active_units.read_text(encoding="utf-8").splitlines() == [
        "SampleA_r1_COI",
        "SampleA_r1_ITS2",
        "SampleA_r2_COI",
        "SampleA_r2_ITS2",
        "SampleB_r1_COI",
    ]
    track_demult_headers = [
        line
        for line in track_demult.read_text(encoding="utf-8").splitlines()
        if line.startswith(">")
    ]
    assert ">SampleB_r1_COI" in track_demult_headers
    assert ">SampleB_r1_ITS2" not in track_demult_headers

    roster_lines = track_roster.read_text(encoding="utf-8").splitlines()
    assert len(roster_lines) == 4
    assert any(line.startswith("SampleB\tSampleB_r1\t") for line in roster_lines[1:])


def test_empty_output_fails_validation(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path, run_id="NonExistentRun")
    assert result.returncode != 0
    assert "run_id 'NonExistentRun' not found" in result.stdout + result.stderr


def test_missing_run_diagnostic_reads_only_run_column_under_tsv_parsing(tmp_path: Path) -> None:
    result = _run_metadata(
        tmp_path,
        run_id="MissingRun",
        metadata_file="pipeline_info_missingrun_spaces.tsv",
    )
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "run_id 'MissingRun' not found" in text
    assert "Available run IDs: OtherRun TestRun" in text


def test_partial_fasta_fails_validation(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path, general_fasta="general_partial.fasta")
    assert result.returncode != 0
    assert "no exact general_fasta key matched either" in result.stderr


def test_conflicting_marker_hits_fail(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path, primers_fasta="primers_conflict.fasta")
    assert result.returncode != 0
    assert "matched multiple primer markers" in result.stderr


def test_targets_allowlist_is_enforced(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path, targets="COI")
    assert result.returncode != 0
    assert "is not present in --targets COI" in result.stderr


def test_explicit_targets_with_surrounding_token_whitespace_fail_for_standalone_script(
    tmp_path: Path,
) -> None:
    result = _run_metadata(tmp_path, targets=" COI | ITS2 ")
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: invalid --targets value; targets must be pipe-separated tokens without whitespace"
    ) in text
    assert "no exact general_fasta key matched either" not in text
    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    assert not sample_info.exists()
    assert not (sample_info / "demult.fasta").exists()


def test_explicit_targets_with_tab_edge_whitespace_fail_for_standalone_script(
    tmp_path: Path,
) -> None:
    result = _run_metadata(tmp_path, targets="COI|\tITS2")
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: invalid --targets value; targets must be pipe-separated tokens without whitespace"
    ) in text
    assert "no exact general_fasta key matched either" not in text
    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    assert not sample_info.exists()
    assert not (sample_info / "demult.fasta").exists()


def test_standalone_metadata_warns_once_when_targets_are_implicit(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path, targets=None)
    assert result.returncode == 0, f"Script failed:\n{result.stdout}\n{result.stderr}"
    warning = "WARNING: --do_metadata is using the implicit default --targets 'COI|ITS2'."
    assert result.stdout.count(warning) == 1


def test_whitespace_in_canonical_samples_field_fails_validation(tmp_path: Path) -> None:
    result = _run_metadata(tmp_path, metadata_file="pipeline_info_whitespace.tsv")
    assert result.returncode != 0
    assert "Sample_ID must not contain whitespace" in result.stderr


def test_reordered_metadata_headers_preserve_canonical_samples_contract(tmp_path: Path) -> None:
    original = _run_metadata(tmp_path / "original")
    assert original.returncode == 0, f"Script failed:\n{original.stdout}\n{original.stderr}"
    reordered = _run_metadata(tmp_path / "reordered", metadata_file="pipeline_info_reordered.tsv")
    assert reordered.returncode == 0, f"Script failed:\n{reordered.stdout}\n{reordered.stderr}"

    expected_lines = [
        "SampleA SampleA_r1 1 A1 TestPlate TestRun >A1_TestPlate",
        "SampleA SampleA_r2 2 A2 TestPlate TestRun >A2_TestPlate",
        "SampleB SampleB_r1 1 B1 TestPlate TestRun >B1_TestPlate",
    ]
    original_lines = (
        tmp_path / "original" / "results" / "sample_info" / "TestRun" / "samples.txt"
    ).read_text(encoding="utf-8").splitlines()
    reordered_lines = (
        tmp_path / "reordered" / "results" / "sample_info" / "TestRun" / "samples.txt"
    ).read_text(encoding="utf-8").splitlines()
    original_metadata_lines = (
        tmp_path / "original" / "results" / "sample_info" / "TestRun" / "TestRun_metadata.txt"
    ).read_text(encoding="utf-8").splitlines()
    reordered_metadata_lines = (
        tmp_path / "reordered" / "results" / "sample_info" / "TestRun" / "TestRun_metadata.txt"
    ).read_text(encoding="utf-8").splitlines()
    assert original_lines == expected_lines
    assert reordered_lines == expected_lines
    assert reordered_lines == original_lines
    assert original_metadata_lines == [
        "SampleA\tSampleA_r1\t1\tA1\tTestPlate\tTestRun\t>A1_TestPlate",
        "SampleA\tSampleA_r2\t2\tA2\tTestPlate\tTestRun\t>A2_TestPlate",
        "SampleB\tSampleB_r1\t1\tB1\tTestPlate\tTestRun\t>B1_TestPlate",
    ]
    assert reordered_metadata_lines == [
        "TestRun\tTestPlate\t>A1_TestPlate\tSampleA_r1\tSampleA\t1\tA1\talpha",
        "TestRun\tTestPlate\t>A2_TestPlate\tSampleA_r2\tSampleA\t2\tA2\tbeta",
        "TestRun\tTestPlate\t>B1_TestPlate\tSampleB_r1\tSampleB\t1\tB1\tgamma",
    ]


def test_existing_nonempty_sample_info_dir_skips_generation(tmp_path: Path) -> None:
    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    sample_info.mkdir(parents=True)
    sentinel = sample_info / "sentinel.txt"
    sentinel.write_text("keep me\n", encoding="utf-8")

    result = _run_metadata(tmp_path)
    assert result.returncode == 0, f"Script failed:\n{result.stdout}\n{result.stderr}"
    assert sentinel.read_text(encoding="utf-8") == "keep me\n"
    assert not (sample_info / "replicate_roster.tsv").exists()
    assert "already exists and isn't empty, skipping metadata" in result.stdout


def test_existing_nonempty_sample_info_dir_does_not_bypass_explicit_target_validation(
    tmp_path: Path,
) -> None:
    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    sample_info.mkdir(parents=True)
    sentinel = sample_info / "sentinel.txt"
    sentinel.write_text("keep me\n", encoding="utf-8")

    result = _run_metadata(tmp_path, targets=" COI | ITS2 ")
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert (
        "ERROR: invalid --targets value; targets must be pipe-separated tokens without whitespace"
    ) in text
    assert "already exists and isn't empty, skipping metadata" not in text
    assert sentinel.read_text(encoding="utf-8") == "keep me\n"
    assert not (sample_info / "replicate_roster.tsv").exists()


def test_rollback_removes_committed_outputs_on_late_mv_failure(tmp_path: Path) -> None:
    shim_dir = tmp_path / "binshim"
    shim_dir.mkdir()
    shim = shim_dir / "mv"
    shim.write_text(
        "#!/bin/bash\n"
        "for last; do :; done\n"
        "if [ \"$last\" = \"results/sample_info/TestRun/samples.txt\" ]; then\n"
        "  exit 1\n"
        "fi\n"
        "exec /bin/mv \"$@\"\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)

    result = _run_metadata(tmp_path, env={"PATH": f"{shim_dir}:{os.environ['PATH']}"})
    assert result.returncode != 0

    sample_info = tmp_path / "results" / "sample_info" / "TestRun"
    committed = [
        sample_info / "TestRun_metadata.txt",
        sample_info / "demult.fasta",
        sample_info / "replicate_identity.tsv",
        sample_info / "replicate_roster.tsv",
        sample_info / "samples.txt",
        sample_info / "general.fasta",
        sample_info / "primers.fasta",
    ]
    assert not any(path.exists() for path in committed)
    if sample_info.exists():
        assert list(sample_info.iterdir()) == []


def test_feeder_ignores_hidden_sidecar_pod5_files() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "list_visible_pod5_files()" in text
    assert "find \"$dir\" -type f -name '*.pod5' ! -name '.*' ! -name '._*' -print0" in text
    assert "find \"$spool\" -mindepth 1 -maxdepth 1 -type f -name '*.pod5' ! -name '.*' ! -name '._*' -print0" in text
    assert "full_files=$(list_visible_pod5_files \"$input_folder\")" in text
    assert "while IFS= read -r full_file; do" in text


def test_feeder_does_not_emit_subthreshold_rounds_from_single_full_pod5() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "take_reads=\"$remaining_reads\"" in text
    assert "if [ \"$take_reads\" -gt \"$needed_reads\" ]; then" in text
    assert "take_reads=\"$needed_reads\"" in text


def test_feeder_accumulates_reads_across_multiple_full_pod5_files() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "progress_reads_file_for()" in text
    assert "consumed_reads_for_file()" in text
    assert "archived_files=$(list_visible_pod5_files \"$output_full_pod5\")" in text
    assert "needed_reads=$(( num_reads - collected_reads ))" in text
    assert "if [ \"$collected_reads\" -ge \"$num_reads\" ]; then" in text
    assert "pod5 filter \"${round_inputs[@]}\" --ids \"$tmp_round_ids\" --output \"$round_output\"" in text
    assert "Buffered unread reads=${collected_reads}; waiting until ${num_reads} reads are available before emitting next round" in text


def test_feeder_uses_run_scoped_temp_files_and_monotonic_round_ids() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "feeder_tmp_dir()" in text
    assert "round_id_counter_file()" in text
    assert "next_round_output_id()" in text
    assert 'tmp_file_pod5="$(feeder_tmp_dir)/tmp_file_pod5_${run_id}_$$.txt"' in text
    assert 'tmp_read_info="$(feeder_tmp_dir)/tmp_read_info_${run_id}_$$.tsv"' in text
    assert 'tmp_round_ids="$(feeder_tmp_dir)/tmp_round_ids_${run_id}_$$.txt"' in text
    assert 'tmp_progress="$(feeder_tmp_dir)/tmp_round_progress_${run_id}_$$.tsv"' in text
    assert 'id_count=$(wc -l < "$tmp_round_ids")' in text
    assert 'read_info_count=$(wc -l < "$tmp_read_info")' in text
    assert 'if [ ! -s "$tmp_round_ids" ]; then' in text
    assert 'elif [ "$id_count" -ne "$read_info_count" ]; then' in text
    assert 'out_id=$(next_round_output_id)' in text
    assert 'metadata_tmp="${metadata_output}.tmp.$$"' in text
    assert 'progress_update_ok=1' in text
    assert 'rm -f "$round_output" "$metadata_tmp" "$metadata_output"' in text


def test_round_id_floor_uses_committed_state_only() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    func_text = text.split("next_round_output_id() {", 1)[1].split("\n}\n", 1)[0]

    assert '"$output_folder"/"${run_id}"_*.pod5' not in func_text
    assert '"$output_rt"/"${run_id}"_*.pod5' not in func_text
    assert '"$done_round"/"${run_id}"_*.pod5' in func_text
    assert '"$metadata"/"${run_id}"_*_slice.tsv' in func_text
    assert '"$metadata"/"${run_id}"_*_read_info_rpt.txt' not in func_text
    assert 'suffix="${suffix%_slice.tsv}"' in func_text
    assert '"$metadata"/"${run_id}"_*_slice.tsv; do' in func_text


def test_global_feeder_dedup_ignores_same_run_path() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    completion_func = text.split("slice_fp_exists_in_completion_ledger() {", 1)[1].split("\n}\n", 1)[0]
    prefix_func = text.split("compute_source_prefix() {", 1)[1].split("\n}\n", 1)[0]
    orphan_func = text.split("startup_cleanup_orphan_rounds() {", 1)[1].split("\n}\n", 1)[0]

    assert 'current_run_path="${RUN_PATH_ABS:-}"' in completion_func
    assert '$1 == fp && (run_path == "" || $6 != run_path)' in completion_func
    assert 'local current_run_path="${3:-}"' in prefix_func
    assert '$2 == fp && (run_path == "" || $6 != run_path)' in prefix_func
    assert 'compute_source_prefix "$source_fp" "$ledger_path" "${RUN_PATH_ABS:-}"' in text
    assert 'round_report.json' in orphan_func


def test_feeder_recovers_readable_skipped_pod5_on_startup() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    func_text = text.split("startup_recover_valid_skipped_pod5() {", 1)[1].split("\n}\n", 1)[0]

    assert 'for skipped_path in "$output_skipped_pod5"/*.pod5; do' in func_text
    assert 'pod5_authoritative_read_count "$skipped_path"' in func_text
    assert 'dest="${output_full_pod5}/${file}"' in func_text
    assert 'mv -f "$skipped_path" "$dest"' in func_text
    assert 'rm -f "$flag_path"' in func_text
    assert 'feeder_log_event "skipped_recover" "$file" ""' in func_text
    assert "startup_recover_valid_skipped_pod5" in text


def test_usage_doc_describes_correct_sample_info_artifact_contracts() -> None:
    text = (REPO_ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "samples.txt             ← canonical 7-field compatibility projection" in text
    assert "{run_id}_metadata.txt   ← run-filtered metadata TSV rows" in text
