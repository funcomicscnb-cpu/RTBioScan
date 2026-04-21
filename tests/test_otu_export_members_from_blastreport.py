import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "otu_export_members_from_blastreport.pl"


def _run(inp: Path, out_members: Path, out_sizes: Path, out_stats: Path):
    return subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(inp),
            str(out_members),
            str(out_sizes),
            str(out_stats),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_export_members_and_sizes_from_annotated_report(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report_annotated_otu.txt"
    out_members = tmp_path / "otu_members.tsv"
    out_sizes = tmp_path / "otu_sizes.tsv"
    out_stats = tmp_path / "otu_members_stats.tsv"
    inp.write_text(
        "#seq_id\ttax_id\tlineage\n"
        "readA|COI|sup|barcode=s1|adapter=s1|OTUB_1-COI\t9606\tMetazoa\n"
        "readA|COI|hac|barcode=s1|adapter=s1|OTUB_1-COI\t9606\tMetazoa\n"
        "readB|COI|sup|barcode=s1|adapter=s1\tOTUB_2-COI\tMetazoa\n"
        "readC|COI|sup|barcode=s2|adapter=s2|OTUB_2-COI\t9606\tMetazoa\n",
        encoding="utf-8",
    )

    result = _run(inp, out_members, out_sizes, out_stats)
    assert result.returncode == 0, result.stderr

    members = out_members.read_text(encoding="utf-8").strip().splitlines()
    assert members == [
        "OTUB_1-COI\treadA",
        "OTUB_2-COI\treadB",
        "OTUB_2-COI\treadC",
    ]

    sizes = out_sizes.read_text(encoding="utf-8").strip().splitlines()
    assert sizes == [
        "OTUB_1-COI\t1",
        "OTUB_2-COI\t2",
    ]

    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["rows_total"] == "4"
    assert stats["rows_kept_unique_pairs"] == "3"
    assert stats["rows_skipped_missing_otu_or_read"] == "0"
    assert stats["otu_total"] == "2"


def test_export_handles_empty_or_unparseable_rows(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report_annotated_otu.txt"
    out_members = tmp_path / "otu_members.tsv"
    out_sizes = tmp_path / "otu_sizes.tsv"
    out_stats = tmp_path / "otu_members_stats.tsv"
    inp.write_text(
        "readX|COI|sup|barcode=s1|adapter=s1\t9606\tMetazoa\n"
        "readY|COI|sup|barcode=s1|adapter=s1\tNA\tlineage\n",
        encoding="utf-8",
    )

    result = _run(inp, out_members, out_sizes, out_stats)
    assert result.returncode == 0, result.stderr
    assert out_members.read_text(encoding="utf-8") == ""
    assert out_sizes.read_text(encoding="utf-8") == ""
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["rows_total"] == "2"
    assert stats["rows_kept_unique_pairs"] == "0"
    assert stats["rows_skipped_missing_otu_or_read"] == "2"
    assert stats["otu_total"] == "0"


def test_export_parses_otu_definition_report_columns(tmp_path: Path) -> None:
    inp = tmp_path / "sample_otu_def_rpt.txt"
    out_members = tmp_path / "otu_members.tsv"
    out_sizes = tmp_path / "otu_sizes.tsv"
    out_stats = tmp_path / "otu_members_stats.tsv"
    inp.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tOTU_id\tOTU_role\n"
        "readA\tNA\thac\tsample1\tunknown\tunknown\tunknown\tunknown\tOTUB_1-COI\tREPRESENTATIVE\n"
        "readB\tNA\thac\tsample1\tunknown\tunknown\tunknown\tunknown\tOTUB_1-COI\tMEMBER\n"
        "readC\tNA\thac\tsample1\tunknown\tunknown\tunknown\tunknown\tOTUB_2-COI\tREPRESENTATIVE\n",
        encoding="utf-8",
    )

    result = _run(inp, out_members, out_sizes, out_stats)
    assert result.returncode == 0, result.stderr

    members = out_members.read_text(encoding="utf-8").strip().splitlines()
    assert members == [
        "OTUB_1-COI\treadA",
        "OTUB_1-COI\treadB",
        "OTUB_2-COI\treadC",
    ]

    sizes = out_sizes.read_text(encoding="utf-8").strip().splitlines()
    assert sizes == [
        "OTUB_1-COI\t2",
        "OTUB_2-COI\t1",
    ]

    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["rows_total"] == "3"
    assert stats["rows_kept_unique_pairs"] == "3"
    assert stats["rows_skipped_missing_otu_or_read"] == "0"
    assert stats["otu_total"] == "2"
