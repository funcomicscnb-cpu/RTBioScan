import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "blast_unassigned_read_ids.pl"


def test_blast_unassigned_read_ids_accepts_round_evidence_schema(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report_annotated_otu_evidence.txt"
    out = tmp_path / "unassigned.list"
    inp.write_text(
        "#seq_id\ttax_id\tlineage\n"
        "readA|COI|hac|barcode=s1|adapter=s1|OTUB_1-COI\t92558\tK__Metazoa;p__Arthropoda;c__Insecta;o__Diptera;f__Dolichopodidae;g__;s__\n"
        "readB|COI|hac|barcode=s1|adapter=s1|OTUB_2-COI\tNA\tK__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out), "--min-level", "genus"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["readB"]


def test_blast_unassigned_read_ids_lineage_only_row_is_not_emitted(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report_annotated_otu_evidence.txt"
    out = tmp_path / "unassigned.list"
    inp.write_text(
        "#seq_id\ttax_id\tlineage\n"
        "readA|ITS2|hac|barcode=s1|adapter=s1|OTUB_9-ITS2\tNA\tK__Viridiplantae;p__Tracheophyta;c__Magnoliopsida;o__Rosales;f__Rosaceae;g__Rosa;s__\n"
        "readB|ITS2|hac|barcode=s1|adapter=s1|OTUB_10-ITS2\tNA\tK__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out), "--min-level", "species"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["readB"]
