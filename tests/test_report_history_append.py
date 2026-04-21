import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "report_history_append.sh"


def _run(round_json: Path, history: Path, lock: Path):
    return subprocess.run(
        ["bash", str(SCRIPT), str(round_json), str(history), str(lock)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj) + "\n", encoding="utf-8")


def test_history_append_dedupes_by_run_barcode_round(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    lock = tmp_path / ".history.lock"

    round_a1 = tmp_path / "a1.json"
    _write_json(
        round_a1,
        {
            "schema_version": "1.0",
            "run_id": "run1",
            "barcode": "RTBioScan",
            "round_barcode": "round_001",
            "reads": {"total": 10},
            "warnings": [],
        },
    )
    rc1 = _run(round_a1, history, lock)
    assert rc1.returncode == 0, rc1.stderr

    round_a2 = tmp_path / "a2.json"
    _write_json(
        round_a2,
        {
            "schema_version": "1.0",
            "run_id": "run1",
            "barcode": "RTBioScan",
            "round_barcode": "round_001",
            "reads": {"total": 25},
            "warnings": [],
        },
    )
    rc2 = _run(round_a2, history, lock)
    assert rc2.returncode == 0, rc2.stderr

    lines = [ln for ln in history.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["reads"]["total"] == 25


def test_history_append_keeps_other_rounds(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    lock = tmp_path / ".history.lock"

    for round_id in ("round_001", "round_002"):
        rp = tmp_path / f"{round_id}.json"
        _write_json(
            rp,
            {
                "schema_version": "1.0",
                "run_id": "run1",
                "barcode": "RTBioScan",
                "round_barcode": round_id,
                "warnings": [],
            },
        )
        rc = _run(rp, history, lock)
        assert rc.returncode == 0, rc.stderr

    lines = [ln for ln in history.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
