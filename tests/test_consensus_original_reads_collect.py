import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_original_reads_collect.sh"


def _run(args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_collects_original_reads_when_enabled(tmp_path: Path) -> None:
    cons_dir = tmp_path / "Consensus" / "sampleA" / "OriginalReads"
    cons_dir.mkdir(parents=True)
    (cons_dir / "Consensus0_reads.list").write_text("r1\nr2\n", encoding="utf-8")
    (cons_dir / "Consensus1_reads.list").write_text("r3\n", encoding="utf-8")

    round_dir = tmp_path / "round_001"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "round_001\tsampleA\tOTUB_1-COI\tConsensus0_sampleA\t2\n",
        encoding="utf-8",
    )
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-dir",
            str(round_dir),
            "--provenance",
            str(provenance),
            "--keep",
            "1",
        ]
    )

    assert result.returncode == 0, result.stderr
    dest_dir = round_dir / "Consensus" / "sampleA" / "OriginalReads"
    assert (dest_dir / "Consensus0_reads.list").exists()
    assert not (dest_dir / "Consensus1_reads.list").exists()
    manifest = round_dir / "Consensus" / "consensus_original_reads_manifest.tsv"
    content = manifest.read_text(encoding="utf-8").splitlines()
    assert content[0] == "consensus_id\treads_list_path\tcompressed\tread_count"
    assert any(line.endswith("\t0\t2") for line in content[1:])
    assert not (tmp_path / "Consensus" / "sampleA" / "OriginalReads").exists()


def test_removes_original_reads_when_disabled(tmp_path: Path) -> None:
    cons_dir = tmp_path / "Consensus" / "sampleA" / "OriginalReads"
    cons_dir.mkdir(parents=True)
    (cons_dir / "Consensus0_reads.list").write_text("r1\n", encoding="utf-8")

    round_dir = tmp_path / "round_001"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "round_001\tsampleA\tOTUB_1-COI\tConsensus0_sampleA\t1\n",
        encoding="utf-8",
    )
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-dir",
            str(round_dir),
            "--provenance",
            str(provenance),
            "--keep",
            "0",
        ]
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "Consensus" / "sampleA" / "OriginalReads").exists()
    assert not (round_dir / "Consensus" / "consensus_original_reads_manifest.tsv").exists()


def test_skip_when_provenance_empty(tmp_path: Path) -> None:
    cons_dir = tmp_path / "Consensus" / "sampleA" / "OriginalReads"
    cons_dir.mkdir(parents=True)
    (cons_dir / "Consensus0_reads.list").write_text("r1\n", encoding="utf-8")

    round_dir = tmp_path / "round_001"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    provenance.write_text("round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n", encoding="utf-8")
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-dir",
            str(round_dir),
            "--provenance",
            str(provenance),
            "--keep",
            "1",
        ]
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "Consensus" / "sampleA" / "OriginalReads").exists()
    assert not (round_dir / "Consensus" / "consensus_original_reads_manifest.tsv").exists()
