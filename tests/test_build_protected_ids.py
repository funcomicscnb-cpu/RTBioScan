import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "build_protected_ids.sh"


def test_build_protected_ids_union(tmp_path: Path) -> None:
    assigned_ever = tmp_path / "assigned_ever.list"
    assigned_round = tmp_path / "assigned_round.list"
    out = tmp_path / "protected.list"
    stats = tmp_path / "stats.tsv"
    assigned_ever.write_text("r1\nr2\n", encoding="utf-8")
    assigned_round.write_text("r2\nr3\n", encoding="utf-8")
    result = subprocess.run(
        [str(SCRIPT), str(assigned_ever), str(assigned_round), str(out), str(stats)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    got = [ln.strip() for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert got == ["r1", "r2", "r3"]
    stats_map = dict(
        line.split("\t", 1) for line in stats.read_text(encoding="utf-8").splitlines() if "\t" in line
    )
    assert stats_map["protected_read_ids_ever_count"] == "2"
    assert stats_map["protected_read_ids_round_count"] == "2"
    assert stats_map["protected_ids_total"] == "3"
