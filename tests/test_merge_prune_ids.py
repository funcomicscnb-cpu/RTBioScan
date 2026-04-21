import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "merge_prune_ids.sh"


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _read_lines(path):
        key, val = line.split("\t", 1)
        out[key] = val
    return out


def test_merge_prune_ids_unions_and_dedupes_base_ids(tmp_path: Path) -> None:
    c1 = tmp_path / "c1.list"
    size_streak = tmp_path / "size_streak.list"
    merged = tmp_path / "merged.list"
    stats = tmp_path / "stats.tsv"

    c1.write_text("readA|sup|x\nreadB\n", encoding="utf-8")
    size_streak.write_text("readB|hac|y\nreadC\nreadD\nreadA\n", encoding="utf-8")

    subprocess.run(
        [
            str(SCRIPT),
            "--out-list",
            str(merged),
            "--out-stats",
            str(stats),
            "--source",
            "c1",
            str(c1),
            "--source",
            "size_streak",
            str(size_streak),
        ],
        check=True,
    )

    assert _read_lines(merged) == ["readA", "readB", "readC", "readD"]
    st = _read_kv(stats)
    assert st["total_candidates"] == "4"
    assert st["c1_candidates"] == "2"
    assert st["size_streak_candidates"] == "4"


def test_merge_prune_ids_handles_empty_sources(tmp_path: Path) -> None:
    out_list = tmp_path / "round.list"
    out_stats = tmp_path / "round.tsv"
    empty = tmp_path / "empty.list"
    empty.write_text("", encoding="utf-8")

    subprocess.run(
        [
            str(SCRIPT),
            "--out-list",
            str(out_list),
            "--out-stats",
            str(out_stats),
            "--source",
            "c1",
            str(empty),
            "--source",
            "size_streak",
            str(tmp_path / "missing.list"),
        ],
        check=True,
    )

    assert _read_lines(out_list) == []
    st = _read_kv(out_stats)
    assert st["total_candidates"] == "0"
    assert st["c1_candidates"] == "0"
    assert st["size_streak_candidates"] == "0"
