import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "reads_apply_prune_ids.pl"


def _run(args):
    return subprocess.run(
        ["perl", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_reads_apply_prune_ids_removes_and_is_idempotent(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        "\n".join(
            [
                ">r1|COI|sup",
                "AAAA",
                ">r2|COI|sup",
                "CCCC",
                ">r3|COI|sup",
                "GGGG",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ids = tmp_path / "drop.list"
    ids.write_text("r2\nrX\n", encoding="utf-8")
    out1 = tmp_path / "out1.fasta"
    stats1 = tmp_path / "stats1.tsv"
    res = _run([str(fasta), str(ids), str(out1), str(stats1)])
    assert res.returncode == 0, res.stderr
    out_text = out1.read_text(encoding="utf-8")
    assert "r2|COI|sup" not in out_text
    assert "r1|COI|sup" in out_text
    assert "r3|COI|sup" in out_text
    # Idempotency: applying again yields same content
    out2 = tmp_path / "out2.fasta"
    stats2 = tmp_path / "stats2.tsv"
    res2 = _run([str(out1), str(ids), str(out2), str(stats2)])
    assert res2.returncode == 0, res2.stderr
    assert out2.read_text(encoding="utf-8") == out1.read_text(encoding="utf-8")
