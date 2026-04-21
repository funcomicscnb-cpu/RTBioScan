"""Integration test for rolling pool BLAST-hit retention.

This mirrors the pipeline step that appends all BLAST-hit reads into the
rolling qced_reads_hq_accumulated.fasta each round.
"""
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "focus_hq_tax_fasta.pl"


def test_blast_hit_reads_appended_to_rolling_pool(tmp_path):
    # Rolling pool contains only a SUP read
    rolling_pool = tmp_path / "qced_reads_hq_accumulated.fasta"
    rolling_pool.write_text(
        ">readA|COI|sup|barcode=bc1|adapter=no_adapter\nACGT\n",
        encoding="utf-8",
    )

    # Full HQ FASTA includes a non-SUP read that has a BLAST hit
    fasta_hq_qced = tmp_path / "fasta_hq_qced.fasta"
    fasta_hq_qced.write_text(
        ">readA|COI|sup|barcode=bc1|adapter=no_adapter\nACGT\n"
        ">readB|COI|fast|barcode=bc1|adapter=no_adapter\nTGCA\n",
        encoding="utf-8",
    )

    # BLAST report includes readB (non-SUP); focus_hq_tax_fasta.pl matches by read ID
    blast_report = tmp_path / "blastreport_join.txt"
    blast_report.write_text(
        "readB|COI|fast|barcode=bc1|adapter=no_adapter\n",
        encoding="utf-8",
    )

    tmp_focus = tmp_path / "tmp_focus_hit.fasta"
    with tmp_focus.open("w", encoding="utf-8") as out_fh:
        result = subprocess.run(
            ["perl", str(SCRIPT), str(blast_report), str(fasta_hq_qced)],
            stdout=out_fh,
            stderr=subprocess.PIPE,
            text=True,
        )
    assert result.returncode == 0, result.stderr

    # Append BLAST-hit reads into the rolling pool (mirrors pipeline behavior)
    if tmp_focus.stat().st_size > 0:
        rolling_pool.write_text(
            rolling_pool.read_text(encoding="utf-8") + tmp_focus.read_text(encoding="utf-8"),
            encoding="utf-8",
        )

    text = rolling_pool.read_text(encoding="utf-8")
    assert ">readB|" in text
