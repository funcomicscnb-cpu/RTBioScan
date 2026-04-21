import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "persist_read_ids_ever.pl"


def test_persist_read_ids_ever_unions_round_and_existing_ids(tmp_path: Path) -> None:
    round_ids = tmp_path / "round_ids.list"
    ever_ids = tmp_path / "ever_ids.list"
    stats = tmp_path / "stats.tsv"

    round_ids.write_text("readB\nreadA|COI|hac\nreadB\n", encoding="utf-8")
    ever_ids.write_text("readC\nreadA\n", encoding="utf-8")

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(round_ids), str(ever_ids), str(stats)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert ever_ids.read_text(encoding="utf-8").splitlines() == ["readA", "readB", "readC"]
    assert stats.read_text(encoding="utf-8").splitlines() == [
        "round_read_ids_count\t3",
        "protected_read_ids_ever_count\t3",
    ]


def test_persist_read_ids_ever_creates_output_from_empty_state(tmp_path: Path) -> None:
    round_ids = tmp_path / "round_ids.list"
    ever_ids = tmp_path / "ever_ids.list"

    round_ids.write_text("readZ\n", encoding="utf-8")

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(round_ids), str(ever_ids)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert ever_ids.read_text(encoding="utf-8").splitlines() == ["readZ"]
