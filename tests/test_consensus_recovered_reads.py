"""Tests for bin/consensus_recovered_reads.pl

Tests the helper that extracts read IDs from OTUs that produced
BLAST-assigned consensus sequences (family or better).
"""
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_recovered_reads.pl"


def _run(blast_report_text, consensus_dir, out_path, min_level=None):
    """Write blast_report to a temp file and run the script."""
    blast_report = out_path.parent / "blast_report.txt"
    blast_report.write_text(blast_report_text, encoding="utf-8")
    cmd = [
        "perl", str(SCRIPT),
        "--blast-report", str(blast_report),
        "--consensus-dir", str(consensus_dir),
        "--out", str(out_path),
    ]
    if min_level is not None:
        cmd += ["--min-level", min_level]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result


def _make_consensus_dir(tmp_path, sample, cons_token, read_ids):
    """Create Consensus/{sample}/OriginalReads/{cons_token}_reads.list
    and a minimal Consensus/{sample}/{sample}_Merged_Consensus.fasta.
    Returns the consensus directory path.
    """
    cons_dir = tmp_path / "Consensus"
    sample_dir = cons_dir / sample
    orig_dir = sample_dir / "OriginalReads"
    orig_dir.mkdir(parents=True, exist_ok=True)

    # Reads list
    reads_list = orig_dir / f"{cons_token}_reads.list"
    reads_list.write_text("\n".join(read_ids) + "\n", encoding="utf-8")

    # Merged consensus FASTA (header = long_seq_id used in blast report)
    fasta = sample_dir / f"{sample}_Merged_Consensus.fasta"
    long_seq_id = f"{sample}|{cons_token}|OTU={cons_token}"
    fasta.write_text(f">{long_seq_id}\nACGT\n", encoding="utf-8")

    return cons_dir, long_seq_id


BLAST_HEADER = "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"


def test_assigned_family_reads_recovered(tmp_path):
    """Reads in a family-assigned OTU appear in output."""
    cons_dir, long_seq_id = _make_consensus_dir(
        tmp_path, "sample1", "OTU_1", ["read1", "read2|some|extra"]
    )
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t12345\tMetazoa\tArthropoda\tInsecta\tColeoptera\tCarabidae\tCarabus\tCarabus_violaceus\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out)
    assert result.returncode == 0, result.stderr
    lines = out.read_text().splitlines()
    assert "read1" in lines
    assert "read2" in lines   # base ID (before |)


def test_assigned_genus_reads_recovered(tmp_path):
    """Reads in a genus-assigned OTU (species=Unassigned) appear in output."""
    cons_dir, long_seq_id = _make_consensus_dir(
        tmp_path, "sample1", "OTU_2", ["readA"]
    )
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t12345\tMetazoa\tArthropoda\tInsecta\tColeoptera\tCarabidae\tCarabus\tUnassigned\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out)
    assert result.returncode == 0
    assert "readA" in out.read_text().splitlines()


def test_unassigned_family_reads_not_recovered(tmp_path):
    """Reads in a fully-unassigned OTU do NOT appear in output."""
    cons_dir, long_seq_id = _make_consensus_dir(
        tmp_path, "sample1", "OTU_3", ["readX"]
    )
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out)
    assert result.returncode == 0
    assert out.read_text().strip() == ""


def test_family_unassigned_reads_not_recovered(tmp_path):
    """At family level, family='Unassigned' means not recovered even if genus/species set."""
    cons_dir, long_seq_id = _make_consensus_dir(
        tmp_path, "sample1", "OTU_4", ["readY"]
    )
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t12345\tMetazoa\tArthropoda\tInsecta\tColeoptera\tUnassigned\tCarabus\tCarabus_violaceus\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out, min_level="family")
    assert result.returncode == 0
    assert out.read_text().strip() == ""


def test_missing_blast_report_empty_output(tmp_path):
    """Missing blast report → empty output, exit 0."""
    cons_dir = tmp_path / "Consensus"
    cons_dir.mkdir()
    out = tmp_path / "recovered.list"
    blast_missing = tmp_path / "nonexistent.txt"
    result = subprocess.run(
        [
            "perl", str(SCRIPT),
            "--blast-report", str(blast_missing),
            "--consensus-dir", str(cons_dir),
            "--out", str(out),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert out.exists()
    assert out.read_text().strip() == ""


def test_unreadable_blast_report_fails_with_clean_open_error(tmp_path):
    cons_dir = tmp_path / "Consensus"
    cons_dir.mkdir()
    out = tmp_path / "recovered.list"
    blast_report = tmp_path / "blast_report.txt"
    blast_report.write_text(BLAST_HEADER, encoding="utf-8")
    blast_report.chmod(0o000)
    try:
        result = subprocess.run(
            [
                "perl", str(SCRIPT),
                "--blast-report", str(blast_report),
                "--consensus-dir", str(cons_dir),
                "--out", str(out),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        blast_report.chmod(0o644)

    assert result.returncode != 0
    assert f"open {blast_report}" in result.stderr
    assert "closed filehandle" not in result.stderr


def test_missing_reads_list_skipped(tmp_path):
    """Missing _reads.list for one OTU → that OTU silently skipped, others recovered."""
    # OTU_good has a reads list; OTU_bad does not
    cons_dir = tmp_path / "Consensus"
    # OTU_good
    good_dir = cons_dir / "sample1"
    orig_good = good_dir / "OriginalReads"
    orig_good.mkdir(parents=True)
    (orig_good / "OTU_good_reads.list").write_text("goodread1\n", encoding="utf-8")
    long_seq_good = "sample1|OTU_good|OTU=OTU_good"
    (good_dir / "sample1_Merged_Consensus.fasta").write_text(
        f">{long_seq_good}\nACGT\n"
        f">sample1|OTU_bad|OTU=OTU_bad\nACGT\n",
        encoding="utf-8",
    )
    # No reads list for OTU_bad (reads dir exists but file missing)

    blast_report = (
        BLAST_HEADER
        + f"{long_seq_good}\t1\tMetazoa\tA\tB\tC\tCarabidae\tG\tS\n"
        + "sample1|OTU_bad|OTU=OTU_bad\t1\tMetazoa\tA\tB\tC\tCarabidae\tG\tS\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out)
    assert result.returncode == 0
    lines = out.read_text().splitlines()
    assert "goodread1" in lines
    # badread not present since reads list missing
    assert "badread" not in "\n".join(lines)


def test_deduplication(tmp_path):
    """Duplicate read IDs across multiple OTUs are deduplicated in output."""
    cons_dir = tmp_path / "Consensus"
    sample_dir = cons_dir / "sample1"
    orig_dir = sample_dir / "OriginalReads"
    orig_dir.mkdir(parents=True)

    for otu in ["OTU_A", "OTU_B"]:
        (orig_dir / f"{otu}_reads.list").write_text("shared_read\nunique_{}\n".format(otu), encoding="utf-8")
    long_a = "sample1|OTU_A|OTU=OTU_A"
    long_b = "sample1|OTU_B|OTU=OTU_B"
    (sample_dir / "sample1_Merged_Consensus.fasta").write_text(
        f">{long_a}\nACGT\n>{long_b}\nACGT\n", encoding="utf-8"
    )

    blast_report = (
        BLAST_HEADER
        + f"{long_a}\t1\tMetazoa\tA\tB\tC\tFamA\tG\tS\n"
        + f"{long_b}\t1\tMetazoa\tA\tB\tC\tFamB\tG\tS\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out)
    assert result.returncode == 0
    lines = out.read_text().splitlines()
    assert lines.count("shared_read") == 1, "Duplicate not deduplicated"


def test_empty_blast_report_empty_output(tmp_path):
    """Empty blast report (header only) → empty output."""
    cons_dir = tmp_path / "Consensus"
    cons_dir.mkdir()
    out = tmp_path / "recovered.list"
    blast_report = BLAST_HEADER
    result = _run(blast_report, cons_dir, out)
    assert result.returncode == 0
    assert out.read_text().strip() == ""


def test_missing_long_seq_id_header_fails(tmp_path):
    cons_dir, _ = _make_consensus_dir(
        tmp_path, "sample1", "OTU_1", ["read1"]
    )
    blast_report = (
        "wrong_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
        "sample1|OTU_1|OTU=OTU_1\t1\tMetazoa\tA\tB\tC\tFam\tG\tS\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out)
    assert result.returncode != 0
    assert "missing required column 'long_seq_id'" in result.stderr


def test_output_sorted(tmp_path):
    """Output IDs are sorted lexicographically."""
    cons_dir = tmp_path / "Consensus"
    sample_dir = cons_dir / "sample1"
    orig_dir = sample_dir / "OriginalReads"
    orig_dir.mkdir(parents=True)
    (orig_dir / "OTU_1_reads.list").write_text("zread\naread\nmread\n", encoding="utf-8")
    long_id = "sample1|OTU_1|OTU=OTU_1"
    (sample_dir / "sample1_Merged_Consensus.fasta").write_text(f">{long_id}\nACGT\n", encoding="utf-8")
    blast_report = BLAST_HEADER + f"{long_id}\t1\tMetazoa\tA\tB\tC\tFam\tG\tS\n"
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out)
    assert result.returncode == 0
    lines = out.read_text().splitlines()
    assert lines == sorted(lines)


# ---------------------------------------------------------------------------
# --min-level genus and species
# ---------------------------------------------------------------------------

def test_family_only_not_recovered_at_genus_level(tmp_path):
    """At genus level, family-only assignment is insufficient → reads not recovered."""
    cons_dir, long_seq_id = _make_consensus_dir(tmp_path, "sample1", "OTU_20", ["readU"])
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t12345\tMetazoa\tA\tB\tC\tCarabidae\tUnassigned\tUnassigned\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out, min_level="genus")
    assert result.returncode == 0
    assert out.read_text().strip() == ""


def test_genus_recovered_at_genus_level(tmp_path):
    """At genus level, genus non-empty → reads recovered."""
    cons_dir, long_seq_id = _make_consensus_dir(tmp_path, "sample1", "OTU_21", ["readV"])
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t12345\tMetazoa\tA\tB\tC\tCarabidae\tCarabus\tUnassigned\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out, min_level="genus")
    assert result.returncode == 0
    assert "readV" in out.read_text().splitlines()


def test_genus_only_not_recovered_at_species_level(tmp_path):
    """At species level, genus-only assignment is insufficient → reads not recovered."""
    cons_dir, long_seq_id = _make_consensus_dir(tmp_path, "sample1", "OTU_22", ["readW"])
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t12345\tMetazoa\tA\tB\tC\tCarabidae\tCarabus\tUnassigned\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out, min_level="species")
    assert result.returncode == 0
    assert out.read_text().strip() == ""


def test_species_recovered_at_species_level(tmp_path):
    """At species level, species non-empty → reads recovered."""
    cons_dir, long_seq_id = _make_consensus_dir(tmp_path, "sample1", "OTU_23", ["readX"])
    blast_report = (
        BLAST_HEADER
        + f"{long_seq_id}\t12345\tMetazoa\tA\tB\tC\tCarabidae\tCarabus\tCarabus_violaceus\n"
    )
    out = tmp_path / "recovered.list"
    result = _run(blast_report, cons_dir, out, min_level="species")
    assert result.returncode == 0
    assert "readX" in out.read_text().splitlines()
