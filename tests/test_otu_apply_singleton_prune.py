import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "otu_apply_size_streak_prune.pl"


def _run(tmp_path: Path, fasta_text: str, prune_ids_text: str):
    in_fasta = tmp_path / "in.fasta"
    prune_ids = tmp_path / "prune_ids.txt"
    out_fasta = tmp_path / "out.fasta"
    out_stats = tmp_path / "stats.tsv"
    in_fasta.write_text(fasta_text, encoding="utf-8")
    prune_ids.write_text(prune_ids_text, encoding="utf-8")
    res = subprocess.run(
        ["perl", str(SCRIPT), str(in_fasta), str(prune_ids), str(out_fasta), str(out_stats)],
        capture_output=True,
        text=True,
        check=False,
    )
    return res, out_fasta, out_stats


def _stats(path: Path) -> dict[str, str]:
    return dict(
        line.split("\t", 1)
        for line in path.read_text(encoding="utf-8").strip().splitlines()
        if line.strip()
    )


def test_pass_through_when_prune_list_empty(tmp_path: Path) -> None:
    fasta = ">r1|COI|sup\nAAAA\n>r2|ITS2|hac\nTTTT\n"
    res, out_fa, out_st = _run(tmp_path, fasta, "")
    assert res.returncode == 0, res.stderr
    assert out_fa.read_text(encoding="utf-8") == fasta
    st = _stats(out_st)
    assert st["reads_total"] == "2"
    assert st["reads_pruned"] == "0"
    assert st["prune_ids_total"] == "0"


def test_prunes_by_base_read_id(tmp_path: Path) -> None:
    fasta = ">r1|COI|sup\nAAAA\n>r2|ITS2|hac\nTTTT\n"
    res, out_fa, out_st = _run(tmp_path, fasta, "r2\n")
    assert res.returncode == 0, res.stderr
    assert out_fa.read_text(encoding="utf-8") == ">r1|COI|sup\nAAAA\n"
    st = _stats(out_st)
    assert st["reads_total"] == "2"
    assert st["reads_pruned"] == "1"
    assert st["reads_kept"] == "1"
    assert st["prune_ids_matched"] == "1"
    assert st["prune_ids_unmatched"] == "0"


def test_unmatched_ids_are_counted_not_fatal(tmp_path: Path) -> None:
    fasta = ">r1|COI|sup\nAAAA\n"
    res, out_fa, out_st = _run(tmp_path, fasta, "missing\n")
    assert res.returncode == 0, res.stderr
    assert out_fa.read_text(encoding="utf-8") == fasta
    st = _stats(out_st)
    assert st["prune_ids_total"] == "1"
    assert st["prune_ids_matched"] == "0"
    assert st["prune_ids_unmatched"] == "1"


def test_duplicate_prune_ids_deduplicated(tmp_path: Path) -> None:
    fasta = ">rA|COI|sup\nAAAA\n>rB|COI|sup\nCCCC\n"
    res, out_fa, out_st = _run(tmp_path, fasta, "rA\nrA\n")
    assert res.returncode == 0, res.stderr
    assert out_fa.read_text(encoding="utf-8") == ">rB|COI|sup\nCCCC\n"
    st = _stats(out_st)
    assert st["prune_ids_total"] == "1"
    assert st["reads_pruned"] == "1"
