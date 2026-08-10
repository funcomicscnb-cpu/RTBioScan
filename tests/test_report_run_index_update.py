import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "report_run_index_update.sh"


def test_run_index_update_replaces_existing(tmp_path: Path) -> None:
    run_json = tmp_path / "run.json"
    index = tmp_path / "runs_index.jsonl"
    lock = tmp_path / ".runs_index.lock"

    old = {
        "schema_version": "2.0",
        "run_id": "runA",
        "last_updated_utc": "2026-03-06T00:00:00Z",
        "rounds_count": 0,
        "status_label": "Fresh",
    }
    other = {
        "schema_version": "2.0",
        "run_id": "runB",
        "last_updated_utc": "2026-03-06T00:00:00Z",
        "rounds_count": 2,
    }
    index.write_text(json.dumps(old) + "\n" + json.dumps(other) + "\n", encoding="utf-8")

    new = {
        "schema_version": "2.0",
        "run_id": "runA",
        "last_updated_utc": "2026-03-06T00:10:00Z",
        "rounds_count": 1,
    }
    run_json.write_text(json.dumps(new) + "\n", encoding="utf-8")

    rc = subprocess.run(
        ["bash", str(SCRIPT), str(run_json), str(index), str(lock)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc.returncode == 0, rc.stderr
    lines = [json.loads(l) for l in index.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any(l["run_id"] == "runB" for l in lines)
    updated_rows = [l for l in lines if l["run_id"] == "runA"]
    assert len(updated_rows) == 1
    updated = updated_rows[0]
    assert updated["schema_version"] == "2.0"
    assert updated["rounds_count"] == 1
