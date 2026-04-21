import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_pruned_unassigned_merge.sh"


def _run(args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_merge_creates_all_list_from_empty_round(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    round_dir = tmp_path / "round_001"
    state_dir.mkdir()
    round_dir.mkdir()
    round_list = tmp_path / "round.list"
    round_list.write_text("", encoding="utf-8")

    result = _run(
        [
            "--round-list",
            str(round_list),
            "--state-dir",
            str(state_dir),
            "--barcode",
            "RTBioScan",
        ]
    )
    assert result.returncode == 0, result.stderr
    all_list = state_dir / "RTBioScan_pruned_unassigned_reads_all.list"
    assert all_list.exists()
    assert all_list.read_text(encoding="utf-8") == ""


def test_merge_unions_round_and_all(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    round_dir = tmp_path / "round_001"
    state_dir.mkdir()
    round_dir.mkdir()
    all_list = state_dir / "RTBioScan_pruned_unassigned_reads_all.list"
    all_list.write_text("r1\nr2\n", encoding="utf-8")
    round_list = tmp_path / "round.list"
    round_list.write_text("r2\nr3\n", encoding="utf-8")

    result = _run(
        [
            "--round-list",
            str(round_list),
            "--state-dir",
            str(state_dir),
            "--barcode",
            "RTBioScan",
        ]
    )
    assert result.returncode == 0, result.stderr
    merged = all_list.read_text(encoding="utf-8").splitlines()
    assert merged == ["r1", "r2", "r3"]
