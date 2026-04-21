import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RENDER = REPO_ROOT / "bin" / "report_render.py"
CHECK = REPO_ROOT / "bin" / "report_publication_check.py"
TEMPLATE = REPO_ROOT / "assets" / "report" / "template.html"
RUN_TEMPLATE = REPO_ROOT / "assets" / "report" / "run_template.html"
CSS = REPO_ROOT / "assets" / "report" / "report.css"
JS = REPO_ROOT / "assets" / "report" / "report.js"


def _render(history: Path, out_html: Path, out_state: Path, run_id: str, extra_args=None) -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(RENDER),
        "--history",
        str(history),
        "--template",
        str(RUN_TEMPLATE if run_id else TEMPLATE),
        "--css",
        str(CSS),
        "--js",
        str(JS),
        "--out",
        str(out_html),
        "--state-out",
        str(out_state),
    ]
    if run_id:
        cmd.extend(["--run-id-filter", run_id])
    if extra_args:
        cmd.extend(extra_args)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _check(history: Path, out_html: Path, out_state: Path, run_id: str, expected_view: str = "") -> subprocess.CompletedProcess[str]:
    cmd = [
        sys.executable,
        str(CHECK),
        "--report-html",
        str(out_html),
        "--report-state",
        str(out_state),
        "--history",
        str(history),
        "--run-id",
        run_id,
    ]
    if expected_view:
        cmd.extend(["--expected-report-view", expected_view])
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _write_history(path: Path, rows) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_publication_check_accepts_consistent_run_report(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {"run_id": "runA", "barcode": "RTBioScan", "round_barcode": "runA_0", "timestamp_utc": "2026-03-06T00:01:00Z"},
            {"run_id": "runB", "barcode": "RTBioScan", "round_barcode": "runB_0", "timestamp_utc": "2026-03-06T00:02:00Z"},
            {"run_id": "runA", "barcode": "RTBioScan", "round_barcode": "runA_1", "timestamp_utc": "2026-03-06T00:03:00Z"},
        ],
    )
    out_html = tmp_path / "runs" / "runA" / "report.html"
    out_state = tmp_path / "runs" / "runA" / "report_state.json"
    out_html.parent.mkdir(parents=True)

    rc = _render(history, out_html, out_state, "runA")
    assert rc.returncode == 0, rc.stderr

    check = _check(history, out_html, out_state, "runA")
    assert check.returncode == 0, check.stderr


def test_publication_check_rejects_revision_mismatch_between_html_and_state(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [{"run_id": "runA", "barcode": "RTBioScan", "round_barcode": "runA_0", "timestamp_utc": "2026-03-06T00:01:00Z"}],
    )
    out_html = tmp_path / "runs" / "runA" / "report.html"
    out_state = tmp_path / "runs" / "runA" / "report_state.json"
    out_html.parent.mkdir(parents=True)

    rc = _render(history, out_html, out_state, "runA")
    assert rc.returncode == 0, rc.stderr

    state = json.loads(out_state.read_text(encoding="utf-8"))
    state["report_revision"] = "0" * 64
    out_state.write_text(json.dumps(state) + "\n", encoding="utf-8")

    check = _check(history, out_html, out_state, "runA")
    assert check.returncode != 0
    assert "report_revision_mismatch" in check.stderr


def test_publication_check_rejects_stale_run_html_when_history_advances(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {"run_id": "runA", "barcode": "RTBioScan", "round_barcode": "runA_0", "timestamp_utc": "2026-03-06T00:01:00Z"},
        {"run_id": "runB", "barcode": "RTBioScan", "round_barcode": "runB_0", "timestamp_utc": "2026-03-06T00:02:00Z"},
    ]
    _write_history(history, rows)
    out_html = tmp_path / "runs" / "runA" / "report.html"
    out_state = tmp_path / "runs" / "runA" / "report_state.json"
    out_html.parent.mkdir(parents=True)

    rc = _render(history, out_html, out_state, "runA")
    assert rc.returncode == 0, rc.stderr

    rows.append({"run_id": "runA", "barcode": "RTBioScan", "round_barcode": "runA_1", "timestamp_utc": "2026-03-06T00:03:00Z"})
    _write_history(history, rows)

    check = _check(history, out_html, out_state, "runA")
    assert check.returncode != 0
    assert "report_revision_history_mismatch" in check.stderr


def test_publication_check_rejects_unexpected_report_view(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "run_id": "runA",
                "barcode": "RTBioScan",
                "round_barcode": "runA_0",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "identity_mode": "track",
            }
        ],
    )
    out_html = tmp_path / "runs" / "runA" / "report_replicates.html"
    out_state = tmp_path / "runs" / "runA" / "report_replicates_state.json"
    out_html.parent.mkdir(parents=True)

    rc = _render(
        history,
        out_html,
        out_state,
        "runA",
        extra_args=["--group-view", "replicate"],
    )
    assert rc.returncode == 0, rc.stderr

    check = _check(history, out_html, out_state, "runA", expected_view="sample")
    assert check.returncode != 0
    assert "unexpected_payload_report_view:replicate" in check.stderr


def test_publication_check_accepts_consistent_replicate_view(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "run_id": "runA",
                "barcode": "RTBioScan",
                "round_barcode": "runA_0",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "identity_mode": "track",
            }
        ],
    )
    out_html = tmp_path / "runs" / "runA" / "report_replicates.html"
    out_state = tmp_path / "runs" / "runA" / "report_replicates_state.json"
    out_html.parent.mkdir(parents=True)

    rc = _render(
        history,
        out_html,
        out_state,
        "runA",
        extra_args=["--group-view", "replicate"],
    )
    assert rc.returncode == 0, rc.stderr

    check = _check(history, out_html, out_state, "runA", expected_view="replicate")
    assert check.returncode == 0, check.stderr


def test_publication_check_accepts_consistent_track_detail_view(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "run_id": "runA",
                "barcode": "RTBioScan",
                "round_barcode": "runA_0",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "identity_mode": "track",
                "track_unit_metrics": {
                    "sample_A_1_COI": {
                        "track_unit_id": "sample_A_1_COI",
                        "track_sample_label": "sample_A",
                        "track_replicate_id": "sample_A_1",
                        "track_replicate_number": 1,
                        "track_replicate_label": "sample_A_1",
                        "track_primer_label": "COI",
                        "reads_demux": 4,
                    }
                },
            }
        ],
    )
    out_html = tmp_path / "runs" / "runA" / "report_replicates_primers.html"
    out_state = tmp_path / "runs" / "runA" / "report_replicates_primers_state.json"
    out_html.parent.mkdir(parents=True)

    rc = _render(
        history,
        out_html,
        out_state,
        "runA",
        extra_args=["--group-view", "track_detail"],
    )
    assert rc.returncode == 0, rc.stderr

    check = _check(history, out_html, out_state, "runA", expected_view="track_detail")
    assert check.returncode == 0, check.stderr


def test_publication_check_rejects_unexpected_track_detail_view(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            {
                "run_id": "runA",
                "barcode": "RTBioScan",
                "round_barcode": "runA_0",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "identity_mode": "track",
                "track_unit_metrics": {
                    "sample_A_1_COI": {
                        "track_unit_id": "sample_A_1_COI",
                        "track_sample_label": "sample_A",
                        "track_replicate_id": "sample_A_1",
                        "track_replicate_number": 1,
                        "track_replicate_label": "sample_A_1",
                        "track_primer_label": "COI",
                        "reads_demux": 4,
                    }
                },
            }
        ],
    )
    out_html = tmp_path / "runs" / "runA" / "report_replicates_primers.html"
    out_state = tmp_path / "runs" / "runA" / "report_replicates_primers_state.json"
    out_html.parent.mkdir(parents=True)

    rc = _render(
        history,
        out_html,
        out_state,
        "runA",
        extra_args=["--group-view", "track_detail"],
    )
    assert rc.returncode == 0, rc.stderr

    check = _check(history, out_html, out_state, "runA", expected_view="sample")
    assert check.returncode != 0
    assert "unexpected_payload_report_view:track_detail" in check.stderr
