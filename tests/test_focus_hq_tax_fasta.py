import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "focus_hq_tax_fasta.pl"


def _run(tmp_path: Path, report_rows: str, fasta_rows: str):
    report = tmp_path / "blast.txt"
    fasta = tmp_path / "reads.fasta"
    report.write_text(report_rows, encoding="utf-8")
    fasta.write_text(fasta_rows, encoding="utf-8")
    return subprocess.run(
        ["perl", str(SCRIPT), str(report), str(fasta)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_preserves_wrapped_fasta_sequences(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "readA|COI|sup|barcode=bc1|adapter=no_adapter\n",
        ">readA|COI|sup|barcode=bc1|adapter=no_adapter\n"
        "ACGT\n"
        "TGCA\n"
        ">readB|COI|sup|barcode=bc1|adapter=no_adapter\n"
        "CCCC\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        ">readA|COI|sup|barcode=bc1|adapter=no_adapter",
        "ACGT",
        "TGCA",
    ]


def test_skips_non_matching_records(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "readZ|COI|sup|barcode=bc1|adapter=no_adapter\n",
        ">readA|COI|sup|barcode=bc1|adapter=no_adapter\nACGT\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
