import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "blast_assigned_read_ids.pl"


def _run(inp: Path, out: Path) -> None:
    subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_assigned_ids_with_header_taxid_or_kingdom(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report.tsv"
    out = tmp_path / "assigned.list"
    inp.write_text(
        "read_id\totu_id\totu_taxid\totu_kingdom\n"
        "r1|COI|sup|adapter=s1|OTUB_1-COI\tOTUB_1-COI\t9606\tMetazoa\n"
        "r2|COI|sup|adapter=s1|OTUB_2-COI\tOTUB_2-COI\tNA\tMetazoa\n"
        "r3|COI|sup|adapter=s1|OTUB_3-COI\tOTUB_3-COI\tNA\tUnassigned\n",
        encoding="utf-8",
    )
    _run(inp, out)
    got = [ln.strip() for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert got == ["r1", "r2"]


def test_assigned_ids_without_header_uses_fallback_columns(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report.tsv"
    out = tmp_path / "assigned.list"
    inp.write_text(
        "u1|COI|sup|adapter=s1|OTUB_1-COI\tOTUB_1-COI\tMetazoa\tCOI\n"
        "u2|COI|sup|adapter=s1|OTUB_2-COI\tOTUB_2-COI\tUnassigned\tCOI\n",
        encoding="utf-8",
    )
    _run(inp, out)
    got = [ln.strip() for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert got == ["u1"]


def test_assigned_ids_with_seq_id_tax_id_lineage_header(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report.tsv"
    out = tmp_path / "assigned.list"
    inp.write_text(
        "#seq_id\ttax_id\tlineage\n"
        "a1|COI|sup|adapter=s1|OTUB_1-COI\tNA\tK__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned\n"
        "a2|COI|sup|adapter=s1|OTUB_2-COI\t9606\tK__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned\n"
        "a3|COI|sup|adapter=s1|OTUB_3-COI\tNA\tK__Metazoa;p__Arthropoda;c__Insecta;o__Diptera;f__Muscidae;g__Musca;s__domestica\n",
        encoding="utf-8",
    )
    _run(inp, out)
    got = [ln.strip() for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert got == ["a2", "a3"]


def test_assigned_ids_with_raw_blast_semicolon_rows(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report.txt"
    out = tmp_path / "assigned.list"
    inp.write_text(
        "r1|COI|sup|barcode=|adapter=s1;9606;1e-20;400;99.0\n"
        "r2|COI|sup|barcode=|adapter=s1;NA;1e-20;400;99.0\n"
        "r3|ITS2|hac|barcode=|adapter=s2;4056;1e-10;300;95.0\n",
        encoding="utf-8",
    )
    _run(inp, out)
    got = [ln.strip() for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert got == ["r1", "r3"]
