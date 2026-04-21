import subprocess
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "Best_consensus_addition.sh"


def test_best_consensus_addition_uses_otu_tag(tmp_path):
    fasta = tmp_path / "sample_Consensus0"
    fasta.write_text(
        ">sample|OTUB_1|COI|reads-2|OTU=OTUB_1-COI\n"
        "ATGC\n"
        ">sample|OTUB_1|COI|reads-1|OTU=OTUB_1-COI\n"
        "ATGC\n",
        encoding="utf-8",
    )
    # provenance files expected by script
    (tmp_path / "OTUB_1-COI_all_reads.list").write_text("readA\n", encoding="utf-8")
    (tmp_path / "OTUB_1-COI_reads_sup.fasta").write_text(">readA\nATGC\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(fasta)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.stdout.strip()
    original_reads = tmp_path / "OriginalReads" / "Consensus0_reads.list"
    sup_reads = tmp_path / "OriginalReads" / "Consensus0_reads_sup.fasta"
    assert original_reads.exists()
    assert sup_reads.exists()


def test_best_consensus_addition_does_not_concatenate_tied_sequences(tmp_path):
    fasta = tmp_path / "sample_Consensus0"
    fasta.write_text(
        ">sample|OTUB_1|COI|reads-5|OTU=OTUB_1-COI\n"
        "AAAA\n"
        ">sample|OTUB_2|COI|reads-5|OTU=OTUB_2-COI\n"
        "TTTT\n",
        encoding="utf-8",
    )
    (tmp_path / "OTUB_1-COI_all_reads.list").write_text("readA\n", encoding="utf-8")
    (tmp_path / "OTUB_2-COI_all_reads.list").write_text("readB\n", encoding="utf-8")
    (tmp_path / "OTUB_1-COI_reads_sup.fasta").write_text(">readA\nAAAA\n", encoding="utf-8")
    (tmp_path / "OTUB_2-COI_reads_sup.fasta").write_text(">readB\nTTTT\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(fasta)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 2
    assert lines[1] in {"AAAA", "TTTT"}


def test_best_consensus_addition_respects_explicit_header(tmp_path):
    fasta = tmp_path / "sample_Consensus0"
    fasta.write_text(
        ">sample|OTUB_1|COI|reads-5|OTU=OTUB_1-COI\n"
        "AAAA\n"
        ">sample|OTUB_2|COI|reads-4|OTU=OTUB_2-COI\n"
        "TTTT\n",
        encoding="utf-8",
    )
    (tmp_path / "OTUB_1-COI_all_reads.list").write_text("readA\n", encoding="utf-8")
    (tmp_path / "OTUB_2-COI_all_reads.list").write_text("readB\n", encoding="utf-8")

    target_header = "sample|OTUB_2|COI|reads-4|OTU=OTUB_2-COI"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(fasta), target_header],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert len(lines) == 2
    assert lines[1] == "TTTT"


def test_best_consensus_addition_sets_reads_to_selected(tmp_path):
    fasta = tmp_path / "sample_Consensus0"
    fasta.write_text(
        ">sample|OTUB_1|COI|reads-5|OTU=OTUB_1-COI\n"
        "AAAA\n"
        ">sample|OTUB_2|COI|reads-3|OTU=OTUB_2-COI\n"
        "TTTT\n",
        encoding="utf-8",
    )
    (tmp_path / "OTUB_1-COI_all_reads.list").write_text("readA\n", encoding="utf-8")
    (tmp_path / "OTUB_2-COI_all_reads.list").write_text("readB\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(fasta)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    header = result.stdout.strip().splitlines()[0]
    assert "reads-5" in header


def test_best_consensus_addition_cluster_total_mode(tmp_path):
    fasta = tmp_path / "sample_Consensus0"
    fasta.write_text(
        ">sample|OTUB_1|COI|reads-5|OTU=OTUB_1-COI\n"
        "AAAA\n"
        ">sample|OTUB_2|COI|reads-3|OTU=OTUB_2-COI\n"
        "TTTT\n",
        encoding="utf-8",
    )
    (tmp_path / "OTUB_1-COI_all_reads.list").write_text("readA\n", encoding="utf-8")
    (tmp_path / "OTUB_2-COI_all_reads.list").write_text("readB\n", encoding="utf-8")

    env = {**os.environ, "CONSENSUS_READS_MODE": "cluster_total"}
    result = subprocess.run(
        ["bash", str(SCRIPT), str(fasta)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )
    header = result.stdout.strip().splitlines()[0]
    assert "reads-8" in header
    original_reads = tmp_path / "OriginalReads" / "Consensus0_reads.list"
    assert set(original_reads.read_text(encoding="utf-8").splitlines()) == {"readA", "readB"}


def test_best_consensus_addition_ignores_stale_prefix_files(tmp_path):
    fasta = tmp_path / "sample_Consensus0"
    fasta.write_text(
        ">sample|OTUB_1|COI|reads-2|OTU=OTUB_1-COI\n"
        "AAAA\n",
        encoding="utf-8",
    )
    (tmp_path / "OTUB_1-COI_all_reads.list").write_text("readA\n", encoding="utf-8")
    (tmp_path / "OTUB_1-COI-COI_all_reads.list").write_text("stale\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(fasta)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.stdout.strip()
    original_reads = tmp_path / "OriginalReads" / "Consensus0_reads.list"
    assert original_reads.read_text(encoding="utf-8").strip() == "readA"


def test_best_consensus_addition_rep_mode_only_selected(tmp_path):
    fasta = tmp_path / "sample_Consensus0"
    fasta.write_text(
        ">sample|OTUB_1|COI|reads-2|OTU=OTUB_1-COI\n"
        "AAAA\n"
        ">sample|OTUB_2|COI|reads-2|OTU=OTUB_2-COI\n"
        "TTTT\n",
        encoding="utf-8",
    )
    (tmp_path / "OTUB_1-COI_all_reads.list").write_text("readA\n", encoding="utf-8")
    (tmp_path / "OTUB_2-COI_all_reads.list").write_text("readB\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(SCRIPT), str(fasta)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert result.stdout.strip()
    original_reads = tmp_path / "OriginalReads" / "Consensus0_reads.list"
    lines = original_reads.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0] in {"readA", "readB"}
