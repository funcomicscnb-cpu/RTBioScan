import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_SCRIPT = REPO_ROOT / "bin" / "reporting_blast_consensus.pl"


def write_text(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_inputs(tmp_path: Path):
    qseqid = (
        "sampleA|Consensus1|COI|reads-10|OTU=OTUB_1-COI|n=10|minQ=20|frozen=1|consolidated=1"
    )
    return build_inputs_with_qseqid(tmp_path, qseqid)


def build_inputs_with_qseqid(tmp_path: Path, qseqid: str, sseqid: str = "hit|kraken:taxid|1234"):
    blast_csv = tmp_path / "blast.csv"
    write_text(blast_csv, f"{qseqid},{sseqid},1e-20,500,99.5\n")
    consensus_tax = tmp_path / "consensus_tax.tsv"
    write_text(
        consensus_tax,
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
        f"{qseqid}\t1234\tMetazoa\tChordata\tAves\tAccipitriformes\tAccipitridae\tGenusA\tSpeciesA\n",
    )
    return qseqid, blast_csv, consensus_tax


def run_report(tmp_path: Path, consolidated_ids):
    qseqid, blast_csv, consensus_tax = build_inputs(tmp_path)
    cons_ids = tmp_path / "cons_ids.txt"
    write_text(cons_ids, consolidated_ids if consolidated_ids is not None else qseqid)
    cmd = [
        "perl",
        str(REPORT_SCRIPT),
        str(blast_csv),
        str(consensus_tax),
        "RTBioScan",
        str(cons_ids),
    ]
    subprocess.run(cmd, check=True, cwd=tmp_path)
    consolidated_out = tmp_path / "RTBioScan_blast_consensus_tax_consolidated_rpt.txt"
    return qseqid, consolidated_out


def row_by_header(lines):
    header = lines[0].split("\t")
    row = lines[1].split("\t")
    return dict(zip(header, row))


def test_consolidated_accepts_full_header(tmp_path):
    _, consolidated_out = run_report(tmp_path, consolidated_ids=None)
    data = consolidated_out.read_text(encoding="utf-8")
    assert "Consensus1_sampleA" in data


def test_consolidated_accepts_normalized_id(tmp_path):
    _, consolidated_out = run_report(tmp_path, consolidated_ids="Consensus1_sampleA")
    data = consolidated_out.read_text(encoding="utf-8")
    assert "Consensus1_sampleA" in data


def test_parser_handles_duplicate_marker_token(tmp_path):
    qseqid = "sampleA|Consensus1|COI|COI|reads-10|OTU=OTUB_1-COI"
    qseqid, blast_csv, consensus_tax = build_inputs_with_qseqid(tmp_path, qseqid)
    cons_ids = tmp_path / "cons_ids.txt"
    write_text(cons_ids, qseqid)
    cmd = [
        "perl",
        str(REPORT_SCRIPT),
        str(blast_csv),
        str(consensus_tax),
        "RTBioScan",
        str(cons_ids),
    ]
    subprocess.run(cmd, check=True, cwd=tmp_path)
    consolidated_out = tmp_path / "RTBioScan_blast_consensus_tax_consolidated_rpt.txt"
    data = consolidated_out.read_text(encoding="utf-8")
    assert "Consensus1_sampleA" in data


def test_marker_extracted_from_otu_tag(tmp_path):
    qseqid = "sampleA|Consensus1|rbcL|reads-10|OTU=OTUB_1-rbcL"
    qseqid, blast_csv, consensus_tax = build_inputs_with_qseqid(tmp_path, qseqid)
    cons_ids = tmp_path / "cons_ids.txt"
    write_text(cons_ids, qseqid)
    cmd = [
        "perl",
        str(REPORT_SCRIPT),
        str(blast_csv),
        str(consensus_tax),
        "RTBioScan",
        str(cons_ids),
    ]
    subprocess.run(cmd, check=True, cwd=tmp_path)
    consolidated_out = tmp_path / "RTBioScan_blast_consensus_tax_consolidated_rpt.txt"
    data = consolidated_out.read_text(encoding="utf-8").splitlines()
    assert len(data) > 1
    row = row_by_header(data)
    assert row["otu_key"] == "OTUB_1-rbcL"
    assert row["barcode_by_homology"] == "rbcL"


def test_numeric_sseqid_is_reported_not_dropped(tmp_path):
    qseqid = "sampleA|Consensus1|COI|reads-10|OTU=OTUB_1-COI"
    qseqid, blast_csv, consensus_tax = build_inputs_with_qseqid(tmp_path, qseqid, sseqid="1234")
    cons_ids = tmp_path / "cons_ids.txt"
    write_text(cons_ids, "Consensus1_sampleA")
    cmd = [
        "perl",
        str(REPORT_SCRIPT),
        str(blast_csv),
        str(consensus_tax),
        "RTBioScan",
        str(cons_ids),
    ]
    subprocess.run(cmd, check=True, cwd=tmp_path)
    report = tmp_path / "RTBioScan_blast_consensus_tax_rpt.txt"
    lines = report.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 1
    row = row_by_header(lines)
    assert row["consensus_id"] == "Consensus1_sampleA"
    assert row["taxid"] == "1234"
    assert row["blast_hit"] == "1234"
