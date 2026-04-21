import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "filter_track_marker_unit_fastq.sh"


def test_filter_track_marker_unit_fastq_keeps_only_target_matched_reads(tmp_path: Path) -> None:
    lookup = tmp_path / "adapter_marker_map.tsv"
    lookup.write_text(
        "sample_A_1_MPold1_COI\tCOI\n"
        "sample_A_1_MPold1_ITS2\tITS2\n",
        encoding="utf-8",
    )
    fastq = tmp_path / "reads.fastq"
    fastq.write_text(
        "@read1|sup|barcode=COI|adapter=sample_A_1_MPold1_COI\n"
        "ACGT\n"
        "+\n"
        "####\n"
        "@read2|sup|barcode=COI|adapter=sample_A_1_MPold1_ITS2\n"
        "TGCA\n"
        "+\n"
        "!!!!\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), str(lookup), "COI", str(fastq), "round_sup_COI"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dropped 1 track marker/unit mismatches" in result.stderr
    assert fastq.read_text(encoding="utf-8") == (
        "@read1|sup|barcode=COI|adapter=sample_A_1_MPold1_COI\n"
        "ACGT\n"
        "+\n"
        "####\n"
    )


def test_filter_track_marker_unit_fastq_drops_unknown_adapters(tmp_path: Path) -> None:
    lookup = tmp_path / "adapter_marker_map.tsv"
    lookup.write_text("sample_A_1_MPold1_COI\tCOI\n", encoding="utf-8")
    fastq = tmp_path / "reads.fastq"
    fastq.write_text(
        "@read1|hac|barcode=COI|adapter=sample_A_1_MPold1_UNKNOWN\n"
        "ACGT\n"
        "+\n"
        "####\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SCRIPT), str(lookup), "COI", str(fastq)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dropped 1 track marker/unit mismatches" in result.stderr
    assert "read1 adapter=sample_A_1_MPold1_UNKNOWN" in result.stderr
    assert fastq.read_text(encoding="utf-8") == ""
