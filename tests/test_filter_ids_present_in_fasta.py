import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "filter_ids_present_in_fasta.pl"


def test_filter_ids_present_in_fasta_intersects_with_fasta_headers(tmp_path: Path) -> None:
    ids_in = tmp_path / "ids.list"
    fasta = tmp_path / "reads.fasta"
    out = tmp_path / "present.list"
    stats = tmp_path / "stats.tsv"

    ids_in.write_text("readB\nreadA|COI|sup\nreadC\n", encoding="utf-8")
    fasta.write_text(
        ">readA|COI|hac|barcode=bc|adapter=no_adapter\nACGT\n"
        ">readC|ITS2|sup|barcode=bc|adapter=no_adapter\nTGCA\n"
        ">readD|COI|hac|barcode=bc|adapter=no_adapter\nCCCC\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(ids_in), str(fasta), str(out), str(stats)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["readA", "readC"]
    assert stats.read_text(encoding="utf-8").splitlines() == [
        "candidate_ids_total\t3",
        "present_in_fasta_count\t2",
    ]


def test_filter_ids_present_in_fasta_writes_empty_when_no_candidates_present(tmp_path: Path) -> None:
    ids_in = tmp_path / "ids.list"
    fasta = tmp_path / "reads.fasta"
    out = tmp_path / "present.list"

    ids_in.write_text("readX\n", encoding="utf-8")
    fasta.write_text(">readY|COI|hac\nACGT\n", encoding="utf-8")

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(ids_in), str(fasta), str(out)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8") == ""
