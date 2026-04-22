import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "report_run_json.pl"


def test_report_run_json_from_history(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {
            "schema_version": "1.1",
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "reads": {"total": 100, "on_target": 60},
            "read_fate": {"blast_assigned_reads": 20, "consensus_used_reads": 5},
            "otu": {"active_by_marker_taxon": {"coi_assigned": 2}},
            "consensus": {"emitted_by_marker_taxon": {"coi_assigned": 1}},
            "warnings": [],
        },
        {
            "schema_version": "1.1",
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": "round_002",
            "timestamp_utc": "2026-03-06T00:02:00Z",
            "reads": {"total": 200, "on_target": 120},
            "read_fate": {"blast_assigned_reads": 40, "consensus_used_reads": 10},
            "otu": {"active_by_marker_taxon": {"coi_assigned": 3}},
            "consensus": {"emitted_by_marker_taxon": {"coi_assigned": 2}},
            "warnings": [],
        },
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run_id"] == "runA"
    assert data["rounds_count"] == 2
    assert data["started_utc"] == "2026-03-06T00:01:00Z"
    assert data["last_round_barcode"] == "round_002"
    assert data["run_summary_source_round"] == "round_002"
    assert data["run_summary"]["reads"]["total"] == 200
    assert data["run_summary"]["read_fate"]["blast_assigned_reads"] == 40
    assert data["run_totals"]["reads"]["total"] == 300
    assert data["run_totals"]["read_fate"]["blast_assigned_reads"] == 60
    assert "run_status_read_fate" not in data
    assert "report_views" not in data          # collapse mode must not emit report_views
    assert data["identity_mode"] == "collapse"  # default identity_mode for non-track history


def test_report_run_json_missing_timestamps_uses_numeric_round(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {"schema_version": "1.1", "run_id": "runA", "barcode": "B1", "round_barcode": "round_2", "warnings": []},
        {"schema_version": "1.1", "run_id": "runA", "barcode": "B1", "round_barcode": "round_10", "warnings": []},
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["last_round_barcode"] == "round_10"


def test_report_run_json_empty_history_emits_zero_round_fresh_status(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text("", encoding="utf-8")
    started = tmp_path / "run_started_utc.txt"
    started.write_text("2026-03-06T00:00:00Z\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
        "--run-started-utc-file",
        str(started),
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["rounds_count"] == 0
    assert data["last_round_barcode"] == "0"
    assert data["started_utc"] == "2026-03-06T00:00:00Z"
    assert data["last_updated_utc"] == "2026-03-06T00:00:00Z"
    assert data["status_label"] == "Fresh"
    assert data["status_color"] == "green"
    assert data["report_rel_path"] == ""
    assert data["report_url"] == ""


def test_report_run_json_mixed_timestamps_prefers_logical_latest_round(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {"schema_version": "1.4", "run_id": "runA", "barcode": "B1", "round_barcode": "round_001", "timestamp_utc": "2026-03-06T00:01:00Z"},
        {"schema_version": "1.4", "run_id": "runA", "barcode": "B1", "round_barcode": "round_010"},
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["last_round_barcode"] == "round_010"
    assert data["run_summary_source_round"] == "round_010"


def test_report_run_json_timestamp_fallback_beats_unsuffixed_mixed_order(tmp_path: Path) -> None:
    history = tmp_path / "_state" / "report_history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"schema_version": "1.6", "run_id": "runA", "barcode": "B1", "round_barcode": "round_010"},
        {"schema_version": "1.6", "run_id": "runA", "barcode": "B1", "round_barcode": "final", "timestamp_utc": "2026-03-06T00:03:00Z"},
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["last_round_barcode"] == "final"


def test_report_run_json_emits_run_status_read_fate_from_cumulative_state(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1" / "_state"
    state_dir.mkdir(parents=True, exist_ok=True)
    history = state_dir / "report_history.jsonl"
    rows = [
        {
            "schema_version": "1.6",
            "run_id": "runA",
            "barcode": "RTBioScan",
            "state_id": "state1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "markers": {"order": ["COI"], "target_taxa_by_marker": {"COI": "Metazoa"}},
        },
        {
            "schema_version": "1.6",
            "run_id": "runA",
            "barcode": "RTBioScan",
            "state_id": "state1",
            "round_barcode": "round_002",
            "timestamp_utc": "2026-03-06T00:02:00Z",
            "markers": {"order": ["COI"], "target_taxa_by_marker": {"COI": "Metazoa"}},
        },
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (state_dir / "RTBioScan_read_info_rpt.txt").write_text(
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        "r1\tr1.pod5\trunA\tbc\t100\t10\t100\t12\tNA\tNA\n"
        "r2\tr2.pod5\trunA\tbc\t101\t10\t101\t12\tNA\tNA\n"
        "r3\tr3.pod5\trunA\tbc\t102\t10\t102\t12\tNA\tNA\n"
        "r4\tr4.pod5\trunA\tbc\t103\t10\t103\t12\tNA\tNA\n",
        encoding="utf-8",
    )
    (state_dir / "RTBioScan_on_target_rpt.txt").write_text(
        "read_id\tqc_filter\ton_target_kingdom\n"
        "r1\tIN\tON_TARGET\n"
        "r2\tIN\tON_TARGET\n"
        "r3\tIN\tON_TARGET\n"
        "r4\tIN\tOFF_TARGET\n",
        encoding="utf-8",
    )
    (state_dir / "RTBioScan_demux_annotation_cache.tsv").write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n"
        "r1\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n"
        "r2\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n",
        encoding="utf-8",
    )
    (state_dir / "RTBioScan_blast_otu_pretax_rpt.txt").write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit1\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS1\n"
        "r2\tCOI\thac\tsample_A_1\thit2\t456\t100\t98\tOTUB_2-COI\t222\tMetazoa\tP\tC\tO\tF\tG\tS2\n",
        encoding="utf-8",
    )
    (state_dir / "RTBioScan_blast_unassigned_current.list").write_text("r2\n", encoding="utf-8")

    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "RTBioScan",
        "--state-id",
        "state1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    read_fate = data["run_status_read_fate"]
    assert read_fate["marker_split_status"] == "ok"
    assert read_fate["demux_total_reads"] == 2
    assert read_fate["blast_seen_reads"] == 2
    assert read_fate["blast_assigned_reads"] == 1
    assert read_fate["blast_unassigned_reads"] == 1
    assert read_fate["chart_blast_assigned_coi"] == 1
    assert read_fate["chart_blast_unassigned_coi"] == 1
    assert read_fate["chart_blast_skipped_coi"] == 0
    assert read_fate["chart_on_target_not_demultiplexed"] == 1
    assert read_fate["chart_off_target"] == 1


def test_report_run_json_propagates_latest_failed_round_metadata(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {
            "schema_version": "1.6",
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "round_status": "ok",
            "reads": {"total": 100, "on_target": 60},
            "read_fate": {"blast_assigned_reads": 20},
            "otu": {"active_by_marker_taxon": {"coi_assigned": 2}},
            "consensus": {"emitted_by_marker_taxon": {"coi_assigned": 1}},
            "warnings": [],
        },
        {
            "schema_version": "1.6",
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": "round_002",
            "timestamp_utc": "2026-03-06T00:02:00Z",
            "round_status": "failed",
            "failure_reason": "No target reads for round_002",
            "reads": {"total": 0, "on_target": 0},
            "read_fate": {"blast_assigned_reads": 0},
            "otu": {"active_by_marker_taxon": {"coi_assigned": 0}},
            "consensus": {"emitted_by_marker_taxon": {"coi_assigned": 0}},
            "warnings": [],
        },
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["last_round_barcode"] == "round_002"
    assert data["last_round_status"] == "failed"
    assert data["last_round_failure_reason"] == "No target reads for round_002"


def test_single_round_with_run_started_utc_file_computes_cadence(tmp_path: Path) -> None:
    """run_started_utc.txt earlier than round ts → cadence = round_ts - run_start."""
    history = tmp_path / "history.jsonl"
    row = {
        "schema_version": "1.4",
        "run_id": "runA",
        "barcode": "B1",
        "round_barcode": "round_001",
        "timestamp_utc": "2026-01-01T00:15:00Z",  # round at T+15min
    }
    history.write_text(json.dumps(row) + "\n", encoding="utf-8")
    started_file = tmp_path / "run_started_utc.txt"
    started_file.write_text("2026-01-01T00:00:00Z\n", encoding="utf-8")  # run start at T+0
    out = tmp_path / "run_report.json"
    cmd = [
        "perl", str(SCRIPT),
        "--history", str(history),
        "--out", str(out),
        "--run-id", "runA",
        "--barcode", "B1",
        "--outdir", "results",
        "--report-rel-path", "runs/runA/report.html",
        "--run-started-utc-file", str(started_file),
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["started_utc"] == "2026-01-01T00:00:00Z"
    assert data["status_cadence_seconds"] == 900  # 15 min = 900 s (deterministic)
    assert "status_label" in data                 # label set (Fresh/Aging/Stale; depends on now)


def test_invalid_run_started_utc_file_content_is_ignored(tmp_path: Path) -> None:
    """Invalid content in run_started_utc.txt is silently ignored; no crash, no cadence."""
    history = tmp_path / "history.jsonl"
    row = {
        "schema_version": "1.4",
        "run_id": "runA",
        "barcode": "B1",
        "round_barcode": "round_001",
        "timestamp_utc": "2026-01-01T00:15:00Z",
    }
    history.write_text(json.dumps(row) + "\n", encoding="utf-8")
    started_file = tmp_path / "run_started_utc.txt"
    started_file.write_text("not-a-timestamp\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl", str(SCRIPT),
        "--history", str(history),
        "--out", str(out),
        "--run-id", "runA",
        "--barcode", "B1",
        "--outdir", "results",
        "--report-rel-path", "runs/runA/report.html",
        "--run-started-utc-file", str(started_file),
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    # Single round with invalid run-start → no cadence (start_epoch == last_epoch)
    assert "status_cadence_seconds" not in data


def test_report_run_json_tie_breaks_on_round_number(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {"schema_version": "1.4", "run_id": "runA", "barcode": "B1", "round_barcode": "round_002", "timestamp_utc": "2026-03-06T00:01:00Z"},
        {"schema_version": "1.4", "run_id": "runA", "barcode": "B1", "round_barcode": "round_010", "timestamp_utc": "2026-03-06T00:01:00Z"},
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["last_round_barcode"] == "round_010"


def test_report_run_json_track_mode_emits_stage_three_report_views(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    row = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "B1",
        "round_barcode": "round_001",
        "timestamp_utc": "2026-03-06T00:01:00Z",
        "identity_mode": "track",
        "warnings": [],
    }
    history.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = [
        "perl",
        str(SCRIPT),
        "--history",
        str(history),
        "--out",
        str(out),
        "--run-id",
        "runA",
        "--barcode",
        "B1",
        "--outdir",
        "results",
        "--report-rel-path",
        "runs/runA/report.html",
    ]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["identity_mode"] == "track"
    assert data["report_views"] == [
        {
            "view_id": "sample",
            "label": "Run Info",
            "report_rel_path": "runs/runA/report.html",
            "report_url": "runs/runA/report.html",
            "is_primary": True,
        },
        {
            "view_id": "track_detail",
            "label": "Replicate Comparison",
            "report_rel_path": "runs/runA/report_replicates_primers.html",
            "report_url": "runs/runA/report_replicates_primers.html",
            "is_primary": False,
        },
        {
            "view_id": "replicate",
            "label": "Primer Comparison",
            "report_rel_path": "runs/runA/report_replicates.html",
            "report_url": "runs/runA/report_replicates.html",
            "is_primary": False,
        },
    ]
