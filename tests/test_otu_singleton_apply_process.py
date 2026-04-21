import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "otu_apply_size_streak_prune.pl"


def _simulate_enforce_apply(tmp_path: Path, fasta_text: str, prune_ids_text: str):
    state_fa = tmp_path / "qced_reads_hq_accumulated.fasta"
    prune_ids = tmp_path / "otu_size_streak_prune_ids_last.txt"
    out_stats = tmp_path / "otu_size_streak_prune_apply.tsv"
    state_fa.write_text(fasta_text, encoding="utf-8")
    prune_ids.write_text(prune_ids_text, encoding="utf-8")

    if not state_fa.is_file() or state_fa.stat().st_size == 0:
        return state_fa.read_text(encoding="utf-8"), None

    tmp_out = tmp_path / "qced_reads_hq_accumulated.size_streak_pruned.tmp"
    res = subprocess.run(
        ["perl", str(SCRIPT), str(state_fa), str(prune_ids), str(tmp_out), str(out_stats)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    tmp_out.replace(state_fa)
    return state_fa.read_text(encoding="utf-8"), out_stats


def test_enforce_apply_mutates_state_when_non_empty(tmp_path: Path) -> None:
    fasta = ">r1|COI|sup\nAAAA\n>r2|ITS2|hac\nTTTT\n"
    out_fa, stats = _simulate_enforce_apply(tmp_path, fasta, "r2\n")
    assert out_fa == ">r1|COI|sup\nAAAA\n"
    assert stats is not None
    assert stats.read_text(encoding="utf-8")


def test_enforce_apply_skips_when_state_empty(tmp_path: Path) -> None:
    out_fa, stats = _simulate_enforce_apply(tmp_path, "", "r1\n")
    assert out_fa == ""
    assert stats is None
