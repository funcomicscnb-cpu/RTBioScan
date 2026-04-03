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
    general_fasta: str = "general.fasta",
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
        str(FIXTURES / metadata_file),
        "--general_fasta",
        str(FIXTURES / general_fasta),
        "--primers_fasta",
        str(FIXTURES / primers_fasta),
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
        ">SampleB_ALT2",
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
    assert [row[3] for row in rows] == ["COI", "ITS2", "COI", "ITS2", "COI", "COI"]
    assert [row[7] for row in rows] == ["COI", "ITS2", "COI", "ITS2", "COI", "ALT2"]
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
    assert rows[-1][6] == "fallback"
    assert rows[-1][8] == "SampleB_ALT2"
    assert rows[-1][9] == "SampleB_r1_ALT2"


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


def test_usage_doc_describes_correct_sample_info_artifact_contracts() -> None:
    text = (REPO_ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "samples.txt             ← canonical 7-field compatibility projection" in text
    assert "{run_id}_metadata.txt   ← run-filtered metadata TSV rows" in text
