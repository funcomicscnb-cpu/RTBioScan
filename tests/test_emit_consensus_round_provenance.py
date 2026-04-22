import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "emit_consensus_round_provenance.pl"


def _run(args):
    return subprocess.run(
        ["perl", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_emit_consensus_round_provenance_counts_unique_reads(tmp_path: Path) -> None:
    cons_dir = tmp_path / "Consensus" / "no_adapter"
    orig_dir = cons_dir / "OriginalReads"
    orig_dir.mkdir(parents=True)

    merged = cons_dir / "no_adapter_Merged_Consensus.fasta"
    merged.write_text(
        ">no_adapter|Consensus0|COI|reads-4|OTU=OTUB_1-COI\nACGT\n"
        ">no_adapter|Consensus1|ITS2|reads-2|OTU=OTUB_2-ITS2\nTGCA\n",
        encoding="utf-8",
    )

    (orig_dir / "Consensus0_reads.list").write_text(
        "r1|COI|sup\n"
        "r1|COI|sup\n"
        "r2|COI|sup\n",
        encoding="utf-8",
    )
    (orig_dir / "Consensus1_reads.list").write_text("r3|ITS2|sup\n", encoding="utf-8")

    out = tmp_path / "consensus_round_provenance.tsv"
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ]
    )

    assert result.returncode == 0, result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round"
    assert "round_001\tno_adapter\tOTUB_1-COI\tConsensus0_no_adapter\t2" in lines[1:]
    assert "round_001\tno_adapter\tOTUB_2-ITS2\tConsensus1_no_adapter\t1" in lines[1:]


def test_emit_consensus_round_provenance_missing_dir_writes_header(tmp_path: Path) -> None:
    out = tmp_path / "consensus_round_provenance.tsv"
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "missing_consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ]
    )

    assert result.returncode == 0, result.stderr
    assert (
        out.read_text(encoding="utf-8")
        == "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
    )


def test_emit_consensus_round_provenance_warns_on_missing_reads_list(tmp_path: Path) -> None:
    cons_dir = tmp_path / "Consensus" / "s1"
    cons_dir.mkdir(parents=True)
    merged = cons_dir / "s1_Merged_Consensus.fasta"
    merged.write_text(
        ">s1|Consensus0|COI|OTU=OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    out = tmp_path / "consensus_round_provenance.tsv"
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "consensus_provenance_missing_reads_list:" in result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert "round_001\ts1\tOTUB_1-COI\tConsensus0_s1\tNA" in lines[1:]


def test_emit_consensus_round_provenance_uses_header_count_when_reads_list_missing(
    tmp_path: Path,
) -> None:
    cons_dir = tmp_path / "Consensus" / "s1"
    cons_dir.mkdir(parents=True)
    merged = cons_dir / "s1_Merged_Consensus.fasta"
    merged.write_text(
        ">s1|Consensus0|COI|reads-4|OTU=OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    out = tmp_path / "consensus_round_provenance.tsv"
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "consensus_provenance_missing_reads_list:" in result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert "round_001\ts1\tOTUB_1-COI\tConsensus0_s1\t4" in lines[1:]


def test_emit_consensus_round_provenance_normalizes_no_adapter_labels(tmp_path: Path) -> None:
    cons_dir = tmp_path / "Consensus" / "x"
    orig_dir = cons_dir / "OriginalReads"
    orig_dir.mkdir(parents=True)
    merged = cons_dir / "x_Merged_Consensus.fasta"
    merged.write_text(
        ">NO_ADAPTER_2|Consensus0|COI|reads-4|OTU=OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    (orig_dir / "Consensus0_reads.list").write_text("r1|COI|sup\n", encoding="utf-8")
    out = tmp_path / "consensus_round_provenance.tsv"
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert "round_001\tno_adapter\tOTUB_1-COI\tConsensus0_no_adapter\t1" in lines[1:]


def test_emit_consensus_round_provenance_keeps_non_numeric_no_adapter_suffix_as_barcoded(tmp_path: Path) -> None:
    cons_dir = tmp_path / "Consensus" / "x"
    orig_dir = cons_dir / "OriginalReads"
    orig_dir.mkdir(parents=True)
    merged = cons_dir / "x_Merged_Consensus.fasta"
    merged.write_text(
        ">no_adapter_1a|Consensus0|COI|reads-4|OTU=OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    (orig_dir / "Consensus0_reads.list").write_text("r1|COI|sup\n", encoding="utf-8")
    out = tmp_path / "consensus_round_provenance.tsv"
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert "round_001\tno_adapter_1a\tOTUB_1-COI\tConsensus0_no_adapter_1a\t1" in lines[1:]


def test_emit_consensus_round_provenance_warns_on_duplicate_key(tmp_path: Path) -> None:
    c1 = tmp_path / "Consensus" / "s1"
    c2 = tmp_path / "Consensus" / "s2"
    o1 = c1 / "OriginalReads"
    o2 = c2 / "OriginalReads"
    o1.mkdir(parents=True)
    o2.mkdir(parents=True)
    (c1 / "s1_Merged_Consensus.fasta").write_text(
        ">same|Consensus0|COI|reads-4|OTU=OTUB_1-COI\nACGT\n",
        encoding="utf-8",
    )
    (c2 / "s2_Merged_Consensus.fasta").write_text(
        ">same|Consensus0|COI|reads-4|OTU=OTUB_1-COI\nTGCA\n",
        encoding="utf-8",
    )
    (o1 / "Consensus0_reads.list").write_text("r1|COI|sup\n", encoding="utf-8")
    (o2 / "Consensus0_reads.list").write_text("r2|COI|sup\nr3|COI|sup\n", encoding="utf-8")
    out = tmp_path / "consensus_round_provenance.tsv"
    result = _run(
        [
            "--consensus-dir",
            str(tmp_path / "Consensus"),
            "--round-barcode",
            "round_001",
            "--out",
            str(out),
        ]
    )
    assert result.returncode == 0, result.stderr
    assert "consensus_provenance_duplicate_key:" in result.stderr
