import csv
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("RTB_REPORT_RUN_JSON_SCRIPT", REPO_ROOT / "bin" / "report_run_json.pl"))


def _oracle_normalized_id(raw: str) -> str:
    return raw.strip().split()[0].split("|")[0] if raw.strip() and raw.strip().upper() != "NA" else ""


def _oracle_live_counts(state_dir: Path) -> tuple[int, int]:
    with (state_dir / "RTBioScan_read_info_rpt.txt").open(encoding="utf-8") as handle:
        total_ids = {_oracle_normalized_id(row["read_id"]) for row in csv.DictReader(handle, delimiter="\t")}
    with (state_dir / "RTBioScan_on_target_rpt.txt").open(encoding="utf-8") as handle:
        target_ids = {
            _oracle_normalized_id(row["read_id"])
            for row in csv.DictReader(handle, delimiter="\t")
            if row["on_target_kingdom"].upper() == "ON_TARGET"
        }
    return len(total_ids - {""}), len(target_ids - {""})


def _commit_cumulative_state(state_dir: Path, pretax: str) -> None:
    """R4-I2: a `_state` cumulative BLAST OTU snapshot is read only as a
    committed generation (sealed record + immutable members); a lone public
    table is an incomplete snapshot and fails closed."""
    names = {
        "reporting": "RTBioScan_blast_otu_reporting_v1.tsv",
        "public": "RTBioScan_blast_otu_pretax_rpt.txt",
        "noadapter": "RTBioScan_blast_otu_noadapter_rpt.txt",
    }
    texts = {"reporting": b"", "public": pretax.encode("utf-8"), "noadapter": pretax.split("\n", 1)[0].encode("utf-8") + b"\n"}
    lines = "".join(f"{p}\t{names[p]}\t{len(texts[p])}\t{hashlib.sha256(texts[p]).hexdigest()}\n" for p in names)
    generation = hashlib.sha256(lines.encode("utf-8")).hexdigest()
    head = f"#RTB-R4D-CUMULATIVE\t1\ngeneration\t{generation}\nprevious\tNA\n{lines}"
    for p, text in texts.items():
        (state_dir / f"{names[p]}.gen-{generation}").write_bytes(text)
    (state_dir / names["public"]).write_bytes(texts["public"])
    (state_dir / "RTBioScan_blast_otu_cumulative.commit").write_text(
        head + f"#END\t{hashlib.sha256(head.encode('utf-8')).hexdigest()}\n", encoding="utf-8")


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
    assert data["schema_version"] == "2.0"
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
    _commit_cumulative_state(
        state_dir,
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit1\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS1\n"
        "r2\tCOI\thac\tsample_A_1\thit2\t456\t100\t98\tOTUB_2-COI\t222\tMetazoa\tP\tC\tO\tF\tG\tS2\n",
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

    # Independent ID-set oracle: the live view is invariant to rolling replay rows.
    assert _oracle_live_counts(state_dir) == (4, 3)
    baseline = dict(data)
    baseline.pop("last_updated_utc", None)
    baseline.pop("status_age_seconds", None)
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    duplicate_free = json.loads(out.read_text(encoding="utf-8"))
    duplicate_free.pop("last_updated_utc", None)
    duplicate_free.pop("status_age_seconds", None)
    assert duplicate_free == baseline

    read_info = state_dir / "RTBioScan_read_info_rpt.txt"
    on_target = state_dir / "RTBioScan_on_target_rpt.txt"
    with read_info.open("a", encoding="utf-8") as handle:
        handle.write("r1\tr1.pod5\trunA\tbc\t100\t10\t100\t12\tNA\tNA\n")
    with on_target.open("a", encoding="utf-8") as handle:
        handle.write("r1\tIN\tON_TARGET\n")
    assert _oracle_live_counts(state_dir) == (4, 3)
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    exact_duplicate = json.loads(out.read_text(encoding="utf-8"))["run_status_read_fate"]
    assert exact_duplicate == read_fate

    with read_info.open("a", encoding="utf-8") as handle:
        handle.write("r1|variant\tr1.pod5\trunA\tbc\t100\t10\t100\t12\tNA\tNA\n")
        handle.write("r2 trailing_text\tr2.pod5\trunA\tbc\t101\t10\t101\t12\tNA\tNA\n")
    with on_target.open("a", encoding="utf-8") as handle:
        handle.write("r1|variant\tIN\tON_TARGET\n")
        handle.write("r1\tIN\tOFF_TARGET\n")
    expected_total, expected_target = _oracle_live_counts(state_dir)
    assert (expected_total, expected_target) == (4, 3)
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    normalized_duplicate = json.loads(out.read_text(encoding="utf-8"))["run_status_read_fate"]
    assert normalized_duplicate == read_fate
    assert sum(normalized_duplicate[key] for key in (
        "chart_blast_assigned_coi", "chart_blast_assigned_its2",
        "chart_blast_unassigned_coi", "chart_blast_unassigned_its2",
        "chart_blast_skipped_coi", "chart_blast_skipped_its2",
        "chart_on_target_not_demultiplexed", "chart_off_target",
    )) == expected_total

    ordinary_out = tmp_path / "ordinary_round.json"
    ordinary = subprocess.run(
        ["perl", str(REPO_ROOT / "bin" / "report_round_json.pl"),
         "--run-id", "runA", "--barcode", "RTBioScan", "--round-barcode", "round_002",
         "--out", str(ordinary_out), "--read-info", str(read_info), "--on-target", str(on_target)],
        capture_output=True, text=True,
    )
    assert ordinary.returncode == 0, ordinary.stderr
    ordinary_report = json.loads(ordinary_out.read_text(encoding="utf-8"))
    assert ordinary_report["reads"]["total"] == 7
    assert ordinary_report["reads"]["on_target"] == 5


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


@pytest.mark.parametrize(
    ("case", "statuses", "expected_source", "expected_attempt"),
    [
        ("ok_ok", ["ok", "ok"], "round_002", "round_002"),
        ("ok_failed", ["ok", "failed"], "round_001", "round_002"),
        ("failed_failed", ["failed", "failed"], None, "round_002"),
        ("ok_failed_ok", ["ok", "failed", "ok"], "round_003", "round_003"),
        ("ok_empty_ok", ["ok", "empty_ok"], "round_002", "round_002"),
        ("failed_legacy", ["failed", None], "round_002", "round_002"),
    ],
)
def test_r6_f03_summary_uses_latest_nonfailed_and_metadata_uses_latest_attempt(
    tmp_path: Path, case: str, statuses: list[str | None], expected_source: str | None, expected_attempt: str
) -> None:
    rows = []
    for index, status in enumerate(statuses, 1):
        empty = status in ("failed", "empty_ok")
        count = 0 if empty else 100 + index
        row = {
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": f"round_{index:03d}",
            "timestamp_utc": f"2026-03-06T00:{index:02d}:00Z",
            "reads": {"total": count, "on_target": count},
            "read_fate": {"blast_assigned_reads": count},
            "otu": {"active_by_marker_taxon": {"coi_assigned": count}},
            "consensus": {"emitted_by_marker_taxon": {"coi_assigned": count}},
        }
        if status is not None:
            row["round_status"] = "ok" if status == "empty_ok" else status
        if status == "failed":
            row["failure_reason"] = f"failed at round_{index:03d}"
        rows.append(row)

    # Independent selection: attempted and eligible rows are chosen separately.
    order = lambda row: int(row["round_barcode"].rsplit("_", 1)[1])
    latest_attempt = max(rows, key=order)
    eligible = [row for row in rows if row.get("round_status", "ok") != "failed"]
    latest_nonfailed = max(eligible, key=order) if eligible else None
    assert latest_attempt["round_barcode"] == expected_attempt, case
    assert (latest_nonfailed["round_barcode"] if latest_nonfailed else None) == expected_source, case

    history = tmp_path / "history.jsonl"
    history.write_text("\n".join(json.dumps(row) for row in reversed(rows)) + "\n", encoding="utf-8")
    out = tmp_path / "run_report.json"
    cmd = ["perl", str(SCRIPT), "--history", str(history), "--out", str(out),
           "--run-id", "runA", "--barcode", "B1", "--outdir", "results",
           "--report-rel-path", "runs/runA/report.html"]
    rc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert rc.returncode == 0, rc.stderr
    data = json.loads(out.read_text(encoding="utf-8"))

    assert data["last_round_barcode"] == latest_attempt["round_barcode"]
    assert data["last_round_timestamp_utc"] == latest_attempt["timestamp_utc"]
    assert data["last_round_status"] == latest_attempt.get("round_status", "ok")
    if latest_attempt.get("round_status") == "failed":
        assert data["last_round_failure_reason"] == latest_attempt["failure_reason"]
    else:
        assert "last_round_failure_reason" not in data
    if latest_nonfailed is None:
        assert "run_summary" not in data
        assert "run_summary_source_round" not in data
    else:
        assert data["run_summary_source_round"] == latest_nonfailed["round_barcode"]
        for field in ("reads", "read_fate", "otu", "consensus"):
            assert data["run_summary"][field] == latest_nonfailed[field]
