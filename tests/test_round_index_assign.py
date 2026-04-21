import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_index_assign.sh"


def _run(state_dir: Path, round_barcode: str, *extra: str):
    return subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--round-barcode",
            round_barcode,
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_round_index_assign_is_idempotent_for_same_round(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    first = _run(state_dir, "round_a", "--no-lock")
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "1"

    second = _run(state_dir, "round_a", "--no-lock")
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "1"

    index_file = state_dir / "round_index.tsv"
    lines = [ln for ln in index_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines == ["round_a\t1"]


def test_round_index_assign_increments_for_new_rounds(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    r1 = _run(state_dir, "round_001", "--no-lock")
    r2 = _run(state_dir, "round_002", "--no-lock")
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert r1.stdout.strip() == "1"
    assert r2.stdout.strip() == "2"


def test_round_index_assign_with_internal_lock(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    result = _run(state_dir, "round_lock_test", "--lock-wait", "5")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_round_index_assign_rejects_invalid_lock_wait(tmp_path: Path) -> None:
    state_dir = tmp_path / "_state"
    result = _run(state_dir, "round_bad", "--lock-wait", "bad")
    assert result.returncode != 0
    assert "Invalid --lock-wait" in result.stderr
