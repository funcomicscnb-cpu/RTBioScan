import subprocess
from pathlib import Path
import os


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "reporting_blast_otu.pl"
TEST_ENV = {
    "RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": "full_collapse",
    "RTBIOSCAN_TARGET_TOKENS": "COI|ITS2",
}


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run_script(
    tmp_path: Path,
    summary_text: str,
    blast_csv_text: str,
    blast_otu_text: str,
    pre_read_info_text: str = "read_id\n",
) -> Path:
    """Helper: write inputs, run reporting_blast_otu.pl, return the output report path."""
    summary = tmp_path / "summary.tsv"
    write_text(summary, summary_text)
    pre_read_info = tmp_path / "pre_read_info.tsv"
    write_text(pre_read_info, pre_read_info_text)
    blast_read = tmp_path / "blast.csv"
    write_text(blast_read, blast_csv_text)
    blast_otu = tmp_path / "blast_otu.tsv"
    write_text(blast_otu, blast_otu_text)
    cmd = [
        "perl",
        str(SCRIPT),
        str(summary),
        str(blast_read),
        str(blast_otu),
        str(pre_read_info),
        "RTBioScan",
    ]
    subprocess.run(cmd, check=True, cwd=tmp_path, env={**os.environ, **TEST_ENV})
    return tmp_path / "RTBioScan_blast_otu_pretax_rpt.txt"


def test_reporting_blast_otu_normalizes_no_adapter(tmp_path: Path) -> None:
    summary = tmp_path / "summary.tsv"
    write_text(
        summary,
        "read_id\tsequence_length_template\tmean_qscore_template\n"
        "read1\t100\t9.5\n",
    )
    pre_read_info = tmp_path / "pre_read_info.tsv"
    write_text(pre_read_info, "read_id\nread1\n")
    blast_read = tmp_path / "blast.csv"
    write_text(
        blast_read,
        "read1,foo,bar,baz,qux,hit1,zz,1234,zz,100,99.5\n",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    write_text(
        blast_otu,
        "long_read_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
        "read1|COI|hac|barcode=|adapter=no_adapter_1|OTUB_1-COI\t1234\tK\tP\tC\tO\tF\tG\tS\n",
    )
    cmd = [
        "perl",
        str(SCRIPT),
        str(summary),
        str(blast_read),
        str(blast_otu),
        str(pre_read_info),
        "RTBioScan",
    ]
    subprocess.run(cmd, check=True, cwd=tmp_path, env={**os.environ, **TEST_ENV})
    report = tmp_path / "RTBioScan_blast_otu_pretax_rpt.txt"
    data = report.read_text(encoding="utf-8").splitlines()
    assert len(data) == 2
    fields = data[1].split("\t")
    assert fields[1] == "COI"
    assert fields[3] == "no_adapter"


def test_reporting_blast_otu_no_adapter_pipe_marker_blast_columns(tmp_path: Path) -> None:
    """no_adapter|COI reads have an extra pipe in the adapter token; aln_length and
    perc_id must still be parsed correctly (not shifted to evalue/aln_length)."""
    OTU_HDR = (
        "long_read_id\totu_taxid\totu_kingdom\totu_phylum\totu_class"
        "\totu_order\totu_family\totu_genus\totu_species\n"
    )
    # Real format: annotated_read_id,sseqid|kraken:taxid|taxid,evalue,aln_length,pident
    # For no_adapter|COI the adapter token contains a pipe → extra field when naively
    # split by [\|,], causing index drift.
    blast_csv = (
        "uuid1|COI|sup|barcode=|adapter=no_adapter|COI,"
        "10416137|kraken:taxid|144444,0.0,403,97.767\n"
        "uuid2|COI|sup|barcode=|adapter=YT.C.spiked_COI,"
        "8397015|kraken:taxid|209941,9.92e-169,331,99.094\n"
    )
    blast_otu = (
        OTU_HDR
        + "uuid1|COI|sup|barcode=|adapter=no_adapter|COI|OTUB_1-COI\t1\tMetazoa\tP\tC\tO\tF\tG\tS\n"
        + "uuid2|COI|sup|barcode=|adapter=YT.C.spiked_COI|OTUB_2-COI\t2\tMetazoa\tP\tC\tO\tF\tG\tS\n"
    )
    summary = "read_id\tsequence_length_template\tmean_qscore_template\n"
    report = _run_script(tmp_path, summary, blast_csv, blast_otu)
    lines = report.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, f"Expected header + 2 data rows, got: {lines}"

    # Column indices in output: 0=read_id,1=barcode_by_homology,2=model,3=sample,
    #   4=hit_id,5=taxid,6=aln_length,7=perc_id,8=otu_id,...
    by_read = {}
    for ln in lines[1:]:
        fields = ln.split("\t")
        by_read[fields[0].split("|", 1)[0]] = fields

    # no_adapter|COI row: aln_length must be 403 (not 0.0), perc_id must be 97.767 (not 403)
    na = by_read["uuid1"]
    assert na[3] == "no_adapter", f"sample wrong: {na[3]!r}"
    assert na[6] == "403", f"aln_length wrong for no_adapter|COI: {na[6]!r}"
    assert na[7] == "97.767", f"perc_id wrong for no_adapter|COI: {na[7]!r}"

    # barcoded row: aln_length must be 331, perc_id must be 99.094
    bc = by_read["uuid2"]
    assert bc[1] == "COI", f"marker wrong for barcoded: {bc[1]!r}"
    assert bc[6] == "331", f"aln_length wrong for barcoded: {bc[6]!r}"
    assert bc[7] == "99.094", f"perc_id wrong for barcoded: {bc[7]!r}"


def test_reporting_blast_otu_prefers_target_token_over_sample_label(tmp_path: Path) -> None:
    summary = "read_id\tsequence_length_template\tmean_qscore_template\nread1\t100\t9.5\n"
    blast_csv = "read1,foo,bar,baz,qux,hit1,zz,1234,zz,100,99.5\n"
    blast_otu = (
        "long_read_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
        "read1|ITS2|hac|barcode=|adapter=TH500_ITS2|OTUB_1-ITS2\t1234\tK\tP\tC\tO\tF\tG\tS\n"
    )
    report = _run_script(tmp_path, summary, blast_csv, blast_otu, "read_id\nread1\n")
    data = report.read_text(encoding="utf-8").splitlines()
    assert len(data) == 2
    fields = data[1].split("\t")
    assert fields[1] == "ITS2"
    assert fields[3] == "TH500_ITS2"


def test_reporting_blast_otu_empty_barcode_without_target_falls_back_to_no_adapter(tmp_path: Path) -> None:
    summary = "read_id\tsequence_length_template\tmean_qscore_template\nread1\t100\t9.5\n"
    blast_csv = "read1,foo,bar,baz,qux,hit1,zz,1234,zz,100,99.5\n"
    blast_otu = (
        "long_read_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
        "read1|hac|barcode=|adapter=TH500_ITS2|OTUB_1-ITS2\t1234\tK\tP\tC\tO\tF\tG\tS\n"
    )
    report = _run_script(tmp_path, summary, blast_csv, blast_otu, "read_id\nread1\n")
    data = report.read_text(encoding="utf-8").splitlines()
    assert len(data) == 2
    fields = data[1].split("\t")
    assert fields[1] == "no_adapter_1"
    assert fields[3] == "TH500_ITS2"
