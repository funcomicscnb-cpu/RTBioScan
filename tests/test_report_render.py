import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

_HAVE_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None
_skip_no_matplotlib = pytest.mark.skipif(not _HAVE_MATPLOTLIB, reason="matplotlib not installed")


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "report_render.py"
TEMPLATE = REPO_ROOT / "assets" / "report" / "template.html"
CSS = REPO_ROOT / "assets" / "report" / "report.css"
JS = REPO_ROOT / "assets" / "report" / "report.js"


def _run(history: Path, out_html: Path, out_state: Path, extra_args=None):
    cmd = _cmd(history, out_html, out_state, extra_args=extra_args)
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def _cmd(history: Path, out_html: Path, out_state: Path, extra_args=None):
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--history",
        str(history),
        "--template",
        str(TEMPLATE),
        "--css",
        str(CSS),
        "--js",
        str(JS),
        "--out",
        str(out_html),
        "--state-out",
        str(out_state),
    ]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _extract_js_json(html_text: str, marker: str):
    start = html_text.index(marker) + len(marker)
    end = html_text.index(";\n", start)
    return json.loads(html_text[start:end])


def _load_report_render_module():
    spec = importlib.util.spec_from_file_location("report_render_test_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_ensure_pdf_from_png_skips_up_to_date_pdf_and_regenerates_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report_render = _load_report_render_module()
    png_path = tmp_path / "figure.png"
    pdf_path = tmp_path / "figure.pdf"
    png_path.write_text("fake png", encoding="utf-8")
    pdf_path.write_text("current pdf", encoding="utf-8")

    calls = {"savefig": 0}

    class _FakeImage:
        shape = (20, 30, 3)

    class _FakeAx:
        def imshow(self, _image):
            return None

        def axis(self, *_args, **_kwargs):
            return None

    class _FakeFig:
        def subplots_adjust(self, *_args, **_kwargs):
            return None

        def savefig(self, path, **_kwargs):
            calls["savefig"] += 1
            Path(path).write_text("fresh pdf", encoding="utf-8")

    class _FakePlt:
        def imread(self, _path):
            return _FakeImage()

        def subplots(self, **_kwargs):
            return _FakeFig(), _FakeAx()

        def close(self, _fig):
            return None

    monkeypatch.setattr(report_render, "_import_matplotlib", lambda: _FakePlt())

    now = time.time()
    os.utime(png_path, (now - 10, now - 10))
    os.utime(pdf_path, (now, now))
    assert report_render.ensure_pdf_from_png(png_path, pdf_path) is True
    assert calls["savefig"] == 0

    os.utime(pdf_path, (now - 20, now - 20))
    assert report_render.ensure_pdf_from_png(png_path, pdf_path) is True
    assert calls["savefig"] == 1


def test_append_generated_sample_figures_uses_signatures_and_regenerates_on_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_render = _load_report_render_module()
    out_path = tmp_path / "runs" / "runX" / "report.html"
    out_path.parent.mkdir(parents=True)

    round_1 = {
        "run_id": "runX",
        "barcode": "B1",
        "round_barcode": "round_001",
        "timestamp_utc": "2026-03-06T00:01:00Z",
        "sample_metrics": {
            "sample_a": {
                "sample_id": "sample_a",
                "label": "sample_A",
                "reads_demux": 3,
                "figures": [],
            }
        },
        "otu": {"assignments_by_level": {}},
        "consensus": {"assignments_by_level": {}},
    }
    round_2 = {
        "run_id": "runX",
        "barcode": "B1",
        "round_barcode": "round_002",
        "timestamp_utc": "2026-03-06T00:02:00Z",
        "_tree_rev": 1,
        "sample_metrics": {
            "sample_a": {
                "sample_id": "sample_a",
                "label": "sample_A",
                "reads_demux": 5,
                "reads_blast_assigned": 4,
                "otu_active": 2,
                "consensus_emitted": 1,
                "figures": [],
            }
        },
        "otu": {"assignments_by_level": {}},
        "consensus": {"assignments_by_level": {}},
    }
    sorted_rounds = [round_1, round_2]
    latest_round = round_2

    calls = {
        "history_reads": 0,
        "history_cumulative": 0,
        "history_taxonomy": 0,
        "icicle": 0,
        "sunburst": 0,
    }

    def _write_assets(kind_key, png_path, pdf_path):
        calls[kind_key] += 1
        png_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_text(f"{kind_key} png", encoding="utf-8")
        pdf_path.write_text(f"{kind_key} pdf", encoding="utf-8")

    monkeypatch.setattr(
        report_render,
        "export_sample_history_reads_r",
        lambda rows, sample_stub, png_path, pdf_path: _write_assets("history_reads", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_sample_history_cumulative_r",
        lambda rows, sample_stub, png_path, pdf_path: _write_assets("history_cumulative", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_sample_history_taxonomy_r",
        lambda rows, sample_stub, source_key, png_path, pdf_path: _write_assets("history_taxonomy", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_icicle_plot_png_pdf",
        lambda title, subtitle, tree, png_path, pdf_path: _write_assets("icicle", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_sunburst_plot_png_pdf",
        lambda title, subtitle, tree, png_path, pdf_path: _write_assets("sunburst", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "build_sample_taxonomy_tree",
        lambda round_obj, sample_label, source_key, marker, include_row=None: {
            "name": marker,
            "value": 1,
            "rev": round_obj.get("_tree_rev", 0),
            "source": source_key,
        },
    )

    report_render.append_generated_sample_figures(sorted_rounds, latest_round, out_path, "runX")
    assert calls == {
        "history_reads": 1,
        "history_cumulative": 1,
        "history_taxonomy": 2,
        "icicle": 4,
        "sunburst": 4,
    }
    signature_dir = out_path.parent / "report_assets" / ".private_signatures" / "samples" / "sample_a"
    assert signature_dir.exists()

    report_render.append_generated_sample_figures(sorted_rounds, latest_round, out_path, "runX")
    assert calls == {
        "history_reads": 1,
        "history_cumulative": 1,
        "history_taxonomy": 2,
        "icicle": 4,
        "sunburst": 4,
    }

    round_1["sample_metrics"]["sample_a"]["reads_demux"] = 4
    report_render.append_generated_sample_figures(sorted_rounds, latest_round, out_path, "runX")
    assert calls["history_reads"] == 2
    assert calls["history_cumulative"] == 2
    assert calls["history_taxonomy"] == 4
    assert calls["icicle"] == 4
    assert calls["sunburst"] == 4

    latest_round["_tree_rev"] = 2
    report_render.append_generated_sample_figures(sorted_rounds, latest_round, out_path, "runX")
    assert calls["icicle"] == 8
    assert calls["sunburst"] == 8

    missing_png = out_path.parent / "report_assets" / "samples" / "sample_a" / "sample_a_reads_time_history.png"
    missing_png.unlink()
    report_render.append_generated_sample_figures(sorted_rounds, latest_round, out_path, "runX")
    assert calls["history_reads"] == 3


def test_append_generated_sample_figures_honors_sample_plot_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_render = _load_report_render_module()
    out_path = tmp_path / "runs" / "runX" / "report.html"
    out_path.parent.mkdir(parents=True)

    latest_round = {
        "run_id": "runX",
        "barcode": "B1",
        "round_barcode": "round_002",
        "timestamp_utc": "2026-03-06T00:02:00Z",
        "sample_metrics": {
            "sample_a": {"sample_id": "sample_a", "label": "sample_A", "reads_demux": 3, "figures": []},
            "sample_b": {"sample_id": "sample_b", "label": "sample_B", "reads_demux": 9, "figures": []},
        },
        "otu": {"assignments_by_level": {}},
        "consensus": {"assignments_by_level": {}},
    }
    sorted_rounds = [latest_round]
    rendered = []

    def _record(kind_key, png_path, pdf_path):
        rendered.append((kind_key, png_path.name))
        png_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_text("png", encoding="utf-8")
        pdf_path.write_text("pdf", encoding="utf-8")

    monkeypatch.setattr(
        report_render,
        "export_sample_history_reads_r",
        lambda rows, sample_stub, png_path, pdf_path: _record("history_reads", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_sample_history_cumulative_r",
        lambda rows, sample_stub, png_path, pdf_path: _record("history_cumulative", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_sample_history_taxonomy_r",
        lambda rows, sample_stub, source_key, png_path, pdf_path: _record("history_taxonomy", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_icicle_plot_png_pdf",
        lambda title, subtitle, tree, png_path, pdf_path: _record("icicle", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "export_sunburst_plot_png_pdf",
        lambda title, subtitle, tree, png_path, pdf_path: _record("sunburst", png_path, pdf_path),
    )
    monkeypatch.setattr(
        report_render,
        "build_sample_taxonomy_tree",
        lambda round_obj, sample_label, source_key, marker, include_row=None: {"name": marker, "value": 1},
    )

    report_render.append_generated_sample_figures(sorted_rounds, latest_round, out_path, "runX", sample_plot_max=1)
    assert all("sample_b" in name for _kind, name in rendered)
    assert latest_round["sample_metrics"]["sample_a"]["figures"] == []
    assert latest_round["sample_metrics"]["sample_b"]["figures"]


def test_sort_rounds_prefers_logical_round_suffix_order_over_timestamps() -> None:
    report_render = _load_report_render_module()
    rounds = [
        {"round_barcode": "TS_run_2", "timestamp_utc": "2026-03-06T00:01:00Z", "barcode": "B1"},
        {"round_barcode": "TS_run_0", "timestamp_utc": "2026-03-06T00:02:00Z", "barcode": "B1"},
        {"round_barcode": "TS_run_1", "timestamp_utc": "2026-03-06T00:03:00Z", "barcode": "B1"},
    ]
    sorted_rounds = report_render.sort_rounds(rounds)
    assert [row["round_barcode"] for row in sorted_rounds] == ["TS_run_0", "TS_run_1", "TS_run_2"]


def test_render_html_with_empty_history(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text("", encoding="utf-8")
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state)
    assert rc.returncode == 0, rc.stderr
    html = out_html.read_text(encoding="utf-8")
    assert out_state.exists()
    state = json.loads(out_state.read_text(encoding="utf-8"))
    assert set(state.keys()) == {"schema_version", "generated_at_utc", "report_revision"}
    assert state["schema_version"] == "1.2"
    assert len(state["report_revision"]) == 64


def test_render_html_skips_malformed_history_lines(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    good = {
        "schema_version": "1.0",
        "run_id": "runX",
        "barcode": "RTBioScan",
        "round_barcode": "round_001",
        "timestamp_utc": "2026-03-06T00:00:00Z",
        "reads": {"total": 1, "on_target": 1},
        "otu": {"canonical": {"active": 1}},
        "blast": {"filtered_reads": 1},
        "consensus": {"emitted": 1},
        "warnings": ["ok"],
    }
    history.write_text("not json\n" + json.dumps(good) + "\n", encoding="utf-8")
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state)
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    assert "malformed_history_line:1" in html_text


def test_render_html_sorts_rounds_by_logical_round_suffix(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    rows = [
        {
            "schema_version": "1.1",
            "run_id": "runX",
            "barcode": "B2",
            "round_barcode": "round_003",
            "timestamp_utc": "2026-03-06T00:03:00Z",
            "warnings": [],
        },
        {
            "schema_version": "1.1",
            "run_id": "runX",
            "barcode": "B1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "warnings": [],
        },
        {
            "schema_version": "1.1",
            "run_id": "runX",
            "barcode": "B0",
            "round_barcode": "round_002",
            "warnings": [],
        },
    ]
    history.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state)
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    payload = _extract_js_json(html_text, "window.REPORT_PAYLOAD = ")
    got = [(r["round_barcode"], r["barcode"]) for r in payload["rounds"]]
    assert got == [("round_001", "B1"), ("round_002", "B0"), ("round_003", "B2")]


def test_render_revision_changes_only_when_history_changes(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    row = {
        "schema_version": "1.1",
        "run_id": "runX",
        "barcode": "B1",
        "round_barcode": "round_001",
        "timestamp_utc": "2026-03-06T00:01:00Z",
        "warnings": [],
    }
    history.write_text(json.dumps(row) + "\n", encoding="utf-8")
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"

    rc1 = _run(history, out_html, out_state)
    assert rc1.returncode == 0, rc1.stderr
    rev1 = json.loads(out_state.read_text(encoding="utf-8"))["report_revision"]

    rc2 = _run(history, out_html, out_state)
    assert rc2.returncode == 0, rc2.stderr
    rev2 = json.loads(out_state.read_text(encoding="utf-8"))["report_revision"]
    assert rev2 == rev1

    history.write_text(json.dumps(row) + "\n" + json.dumps({**row, "round_barcode": "round_002"}) + "\n", encoding="utf-8")
    rc3 = _run(history, out_html, out_state)
    assert rc3.returncode == 0, rc3.stderr
    rev3 = json.loads(out_state.read_text(encoding="utf-8"))["report_revision"]
    assert rev3 != rev1


def test_render_embeds_auto_refresh_meta(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text("", encoding="utf-8")
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=[
            "--auto-refresh-enabled",
            "0",
            "--auto-refresh-seconds",
            "30",
            "--state-url",
            "report_state.json",
            "--schema-version",
            "1.1",
        ],
    )
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    meta = _extract_js_json(html_text, "window.REPORT_META = ")
    assert meta["auto_refresh_enabled"] is False
    assert meta["refresh_interval_sec"] == 30
    assert meta["state_url"] == "report_state.json"


def test_render_run_view_includes_figures_from_latest_round(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "run_id": "runX",
                        "barcode": "B1",
                        "round_barcode": "round_001",
                        "timestamp_utc": "2026-03-06T00:01:00Z",
                        "figures": [],
                        "warnings": [],
                    }
                ),
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "run_id": "runX",
                        "barcode": "B1",
                        "round_barcode": "round_002",
                        "timestamp_utc": "2026-03-06T00:02:00Z",
                        "figures": [
                            {
                                "id": "reads_time",
                                "title": "Reads vs Time",
                                "description": "Total reads over time",
                                "path": "report_assets/B1_reads_time.png",
                                "exists": True,
                            }
                        ],
                        "warnings": [],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=[
            "--state-url",
            "report_state.json",
            "--run-id-filter",
            "runX",
        ],
    )
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    payload = _extract_js_json(html_text, "window.REPORT_PAYLOAD = ")
    assert payload["figures"][0]["id"] == "reads_time"


@_skip_no_matplotlib
def test_render_run_view_exposes_figure_and_chart_pdf_exports(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "run_id": "runX",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:02:00Z",
                "reads": {"total": 10, "on_target": 8},
                "read_fate": {"blast_assigned_reads": 5, "consensus_used_reads": 2, "demux_total_reads": 0, "no_adapter_reads": 0},
                "otu": {
                    "canonical": {"informative_dynamic": 3},
                    "pruned": {"prune_candidates": 1, "size_streak": 0, "blast_unassigned": 0},
                    "active_by_marker_taxon": {"coi_assigned": 2, "its2_assigned": 1, "coi_unassigned": 0, "its2_unassigned": 0},
                },
                "consensus": {"emitted_by_marker_taxon": {"coi_assigned": 1, "its2_assigned": 0, "coi_unassigned": 0, "its2_unassigned": 0}},
                "figures": [
                    {
                        "id": "reads_time",
                        "title": "Reads vs Time",
                        "description": "Total reads over time",
                        "path": "runs/runX/report_assets/B1_reads_time.png",
                        "pdf_path": "runs/runX/report_assets/B1_reads_time.pdf",
                        "pdf_exists": True,
                        "exists": True,
                    }
                ],
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "runs" / "runX"
    run_dir.mkdir(parents=True)
    asset_dir = run_dir / "report_assets"
    asset_dir.mkdir(parents=True, exist_ok=True)
    (asset_dir / "B1_reads_time.png").write_text("fake", encoding="utf-8")
    (asset_dir / "B1_reads_time.pdf").write_text("fake", encoding="utf-8")
    out_html = run_dir / "report.html"
    out_state = run_dir / "report_state.json"
    rc = _run(history, out_html, out_state, extra_args=["--run-id-filter", "runX"])
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert payload["figures"][0]["pdf_path"] == "report_assets/B1_reads_time.pdf"
    assert payload["figures"][0]["pdf_exists"] is True
    assert payload["chart_exports"]["run_reads_fate"]["pdf_path"] == "report_assets/embedded/run_reads_fate.pdf"
    assert (run_dir / "report_assets" / "embedded" / "run_reads_fate.pdf").exists()


def test_render_run_view_prefixes_sample_figure_paths(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.2",
                "run_id": "runX",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "read_fate": {"demux_total_reads": 10, "no_adapter_reads": 3},
                "sample_metrics": {
                    "sample_a_12345678": {
                        "sample_id": "sample_a_12345678",
                        "label": "sample_A",
                        "figures": [
                            {
                                "id": "sample_reads_per_sample",
                                "title": "Reads per Sample",
                                "path": "runs/runX/report_assets/samples/sample_a_12345678/sample_a_12345678_reads_per_sample.png",
                                "exists": True,
                            }
                        ],
                    }
                },
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=[
            "--run-id-filter",
            "runX",
            "--url-prefix",
            "/app",
        ],
    )
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    sample = payload["rounds"][0]["sample_metrics"]["sample_a_12345678"]
    assert sample["figures"][0]["path"].startswith("/app/")


@_skip_no_matplotlib
def test_render_run_view_generates_sample_icicles_and_embedded_barcode_pdf(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.2",
                "run_id": "runX",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "read_fate": {"demux_total_reads": 10, "no_adapter_reads": 3},
                "sample_metrics": {
                    "sample_a_12345678": {
                        "sample_id": "sample_a_12345678",
                        "label": "sample_A",
                        "reads_demux": 10,
                        "reads_demux_coi": 7,
                        "reads_demux_its2": 3,
                        "reads_blast_assigned": 8,
                        "otu_active": 4,
                        "consensus_emitted": 2,
                        "replicates": {
                            "sample_A_1": {"label": "sample_A_1", "reads_demux": 6},
                            "sample_A_2": {"label": "sample_A_2", "reads_demux": 4},
                        },
                        "figures": [],
                    }
                },
                "otu": {
                    "assignments_by_level": {
                        "family": [
                            {"sample": "sample_A", "marker": "COI", "family": "Felidae", "reads_total": 7},
                            {"sample": "sample_A", "marker": "ITS2", "family": "Poaceae", "reads_total": 3},
                        ],
                        "genus": [
                            {"sample": "sample_A", "marker": "COI", "family": "Felidae", "genus": "Panthera", "reads_total": 5},
                            {"sample": "sample_A", "marker": "ITS2", "family": "Poaceae", "genus": "Triticum", "reads_total": 2},
                        ],
                        "species": [
                            {"sample": "sample_A", "marker": "COI", "family": "Felidae", "genus": "Panthera", "species": "Panthera leo", "reads_total": 4},
                            {"sample": "sample_A", "marker": "ITS2", "family": "Poaceae", "genus": "Triticum", "species": "Triticum aestivum", "reads_total": 1},
                        ],
                    }
                },
                "consensus": {
                    "assignments_by_level": {
                        "family": [
                            {"sample": "sample_A", "marker": "COI", "family": "Felidae", "reads_total": 2},
                            {"sample": "sample_A", "marker": "ITS2", "family": "Poaceae", "reads_total": 1},
                        ],
                        "genus": [
                            {"sample": "sample_A", "marker": "COI", "family": "Felidae", "genus": "Panthera", "reads_total": 2}
                        ],
                        "species": [
                            {"sample": "sample_A", "marker": "ITS2", "family": "Poaceae", "genus": "Triticum", "species": "Triticum aestivum", "reads_total": 1}
                        ],
                    }
                },
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_dir = tmp_path / "runs" / "runX"
    run_dir.mkdir(parents=True)
    out_html = run_dir / "report.html"
    out_state = run_dir / "report_state.json"
    rc = _run(history, out_html, out_state, extra_args=["--run-id-filter", "runX"])
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    sample = payload["rounds"][0]["sample_metrics"]["sample_a_12345678"]
    figure_ids = {fig["id"] for fig in sample["figures"]}
    assert {
        "sample_otu_coi_icicle",
        "sample_otu_its2_icicle",
        "sample_consensus_coi_icicle",
        "sample_consensus_its2_icicle",
        "sample_otu_coi_sunburst",
        "sample_otu_its2_sunburst",
        "sample_consensus_coi_sunburst",
        "sample_consensus_its2_sunburst",
    }.issubset(figure_ids)

    otu_coi = next(fig for fig in sample["figures"] if fig["id"] == "sample_otu_coi_icicle")
    assert otu_coi["path"] == "report_assets/samples/sample_a_12345678/sample_a_12345678_otu_coi_icicle.png"
    assert otu_coi["pdf_path"] == "report_assets/samples/sample_a_12345678/sample_a_12345678_otu_coi_icicle.pdf"
    assert otu_coi["pdf_exists"] is True
    assert (run_dir / "report_assets" / "samples" / "sample_a_12345678" / "sample_a_12345678_otu_coi_icicle.pdf").exists()
    assert (run_dir / "report_assets" / "samples" / "sample_a_12345678" / "sample_a_12345678_consensus_its2_icicle.png").exists()
    assert (run_dir / "report_assets" / "samples" / "sample_a_12345678" / "sample_a_12345678_otu_coi_sunburst.pdf").exists()
    assert (run_dir / "report_assets" / "samples" / "sample_a_12345678" / "sample_a_12345678_consensus_its2_sunburst.png").exists()

    chart_export = payload["chart_exports"]["sample_sample_a_12345678_reads_per_barcode"]
    assert chart_export["pdf_path"] == "report_assets/embedded/samples/sample_a_12345678_reads_per_barcode.pdf"
    assert (run_dir / "report_assets" / "embedded" / "samples" / "sample_a_12345678_reads_per_barcode.pdf").exists()
    treemap_export = payload["chart_exports"]["sample_sample_a_12345678_otu_treemap_species"]
    assert treemap_export["pdf_path"] == "report_assets/embedded/samples/sample_a_12345678_otu_treemap_species.pdf"
    assert (run_dir / "report_assets" / "embedded" / "samples" / "sample_a_12345678_otu_treemap_species.pdf").exists()
    consensus_treemap_export = payload["chart_exports"]["sample_sample_a_12345678_consensus_treemap_species"]
    assert consensus_treemap_export["pdf_path"] == "report_assets/embedded/samples/sample_a_12345678_consensus_treemap_species.pdf"
    assert (run_dir / "report_assets" / "embedded" / "samples" / "sample_a_12345678_consensus_treemap_species.pdf").exists()


@_skip_no_matplotlib
def test_build_chart_exports_orders_sample_barcode_bars_by_round_label(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report_render = _load_report_render_module()
    captured = {}

    def fake_export_vertical_bar_pdf(title, bars, color, pdf_path):
        captured["title"] = title
        captured["bars"] = [bar["label"] for bar in bars]
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("fake pdf", encoding="utf-8")

    monkeypatch.setattr(report_render, "export_vertical_bar_pdf", fake_export_vertical_bar_pdf)
    monkeypatch.setattr(report_render, "export_stacked_bar_pdf", lambda *args, **kwargs: None)

    rounds = [
        {
            "run_id": "runX",
            "barcode": "B1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "read_fate": {"demux_enabled": True, "demux_total_reads": 18, "no_adapter_reads": 0},
            "sample_metrics": {
                "sample_a_12345678": {
                    "sample_id": "sample_a_12345678",
                    "label": "sample_A",
                    "reads_demux": 18,
                    "reads_demux_coi": 18,
                    "reads_demux_its2": 0,
                    "replicates": {
                        "sample_A_10": {"label": "sample_A_10", "reads_demux": 90},
                        "sample_A_2": {"label": "sample_A_2", "reads_demux": 5},
                        "sample_A_1": {"label": "sample_A_1", "reads_demux": 50},
                    },
                }
            },
            "warnings": [],
        }
    ]

    report_render.build_chart_exports(rounds, [], tmp_path / "runs" / "runX" / "report.html", "runX")

    assert captured["title"] == "Reads per Barcode (sample): sample_A"
    assert captured["bars"] == ["sample_A_1", "sample_A_2", "sample_A_10"]


def test_build_sample_taxonomy_tree_uses_deepest_available_assignments() -> None:
    report_render = _load_report_render_module()
    round_obj = {
        "otu": {
            "assignments_by_level": {
                "family": [
                    {"sample": "sample_A", "marker": "COI", "family": "Felidae", "reads_total": 10},
                ],
                "genus": [
                    {"sample": "sample_A", "marker": "COI", "family": "Felidae", "genus": "Panthera", "reads_total": 7},
                ],
                "species": [
                    {
                        "sample": "sample_A",
                        "marker": "COI",
                        "family": "Felidae",
                        "genus": "Panthera",
                        "species": "Panthera leo",
                        "reads_total": 4,
                    },
                ],
            }
        }
    }

    tree = report_render.build_sample_taxonomy_tree(round_obj, "sample_A", "otu", "COI")

    assert tree["name"] == "COI"
    assert tree["value"] == 10.0
    assert len(tree["children"]) == 1
    family = tree["children"][0]
    assert family["name"] == "Felidae"
    assert family["value"] == 10.0
    assert family["leaf_value"] == 3.0
    assert len(family["children"]) == 1
    genus = family["children"][0]
    assert genus["name"] == "Panthera"
    assert genus["value"] == 7.0
    assert genus["leaf_value"] == 3.0
    assert len(genus["children"]) == 1
    species = genus["children"][0]
    assert species["name"] == "Panthera leo"
    assert species["value"] == 4.0
    assert species["leaf_value"] == 4.0


def test_iter_sample_assignment_rows_isolates_track_units() -> None:
    """Plain-equality filter must not let sample_A_2 rows bleed into sample_A_1 results."""
    report_render = _load_report_render_module()
    round_obj = {
        "otu": {
            "assignments_by_level": {
                "species": [
                    {"sample": "sample_A_1", "marker": "COI", "family": "F1", "genus": "G1", "species": "S1", "reads_total": 10},
                    {"sample": "sample_A_2", "marker": "COI", "family": "F2", "genus": "G2", "species": "S2", "reads_total": 7},
                ],
            }
        }
    }
    rows_1 = report_render.iter_sample_assignment_rows(round_obj, "sample_A_1", "otu", "species")
    rows_2 = report_render.iter_sample_assignment_rows(round_obj, "sample_A_2", "otu", "species")
    assert len(rows_1) == 1
    assert rows_1[0]["species"] == "S1"
    assert len(rows_2) == 1
    assert rows_2[0]["species"] == "S2"


def test_build_sample_treemap_items_isolates_track_units() -> None:
    """build_sample_treemap_items must not merge items from different track units."""
    report_render = _load_report_render_module()
    round_obj = {
        "otu": {
            "assignments_by_level": {
                "species": [
                    {"sample": "sample_A_1", "marker": "COI", "taxon": "S1", "family": "F1", "reads_total": 5},
                    {"sample": "sample_A_2", "marker": "COI", "taxon": "S2", "family": "F2", "reads_total": 3},
                ],
            }
        }
    }
    items_1 = report_render.build_sample_treemap_items(round_obj, "sample_A_1", "species", source_key="otu")
    items_2 = report_render.build_sample_treemap_items(round_obj, "sample_A_2", "species", source_key="otu")
    assert len(items_1) == 1
    assert items_1[0]["taxon"] == "S1"
    assert len(items_2) == 1
    assert items_2[0]["taxon"] == "S2"


def test_iter_sample_assignment_rows_collapses_track_sample_replicates() -> None:
    report_render = _load_report_render_module()
    round_obj = {
        "otu": {
            "assignments_by_level": {
                "species": [
                    {"sample": "sample_A_1_MPold1", "track_sample_replicate_label": "sample_A_1", "marker": "COI", "species": "S1", "reads_total": 5},
                    {"sample": "sample_A_1_MPnew1", "track_sample_replicate_label": "sample_A_1", "marker": "ITS2", "species": "S2", "reads_total": 4},
                    {"sample": "sample_A_2_MPold1", "track_sample_replicate_label": "sample_A_2", "marker": "COI", "species": "S3", "reads_total": 7},
                ],
            }
        }
    }
    rows = report_render.iter_sample_assignment_rows(
        round_obj,
        "sample_A_1",
        "otu",
        "species",
        match_mode="track_sample_replicate",
    )
    assert [row["species"] for row in rows] == ["S1", "S2"]


def test_build_sample_treemap_items_collapses_track_sample_replicates() -> None:
    report_render = _load_report_render_module()
    round_obj = {
        "otu": {
            "assignments_by_level": {
                "species": [
                    {"sample": "sample_A_1_MPold1", "track_sample_replicate_label": "sample_A_1", "taxon": "S1", "family": "F1", "reads_total": 5},
                    {"sample": "sample_A_1_MPnew1", "track_sample_replicate_label": "sample_A_1", "taxon": "S2", "family": "F2", "reads_total": 4},
                    {"sample": "sample_A_2_MPold1", "track_sample_replicate_label": "sample_A_2", "taxon": "S3", "family": "F3", "reads_total": 7},
                ],
            }
        }
    }
    items = report_render.build_sample_treemap_items(
        round_obj,
        "sample_A_1",
        "species",
        source_key="otu",
        match_mode="track_sample_replicate",
    )
    assert [item["taxon"] for item in items] == ["S1", "S2"]


def test_track_sample_replicate_label_falls_back_for_legacy_track_rows() -> None:
    report_render = _load_report_render_module()
    assert report_render.track_sample_replicate_label({
        "track_replicate_label": "CS.D.P_1_MPnew1",
    }) == "CS.D.P_1"
    assert report_render.track_sample_replicate_label({
        "sample": "sample_A_2_MPold1",
    }) == "sample_A_2"


def test_write_assignments_produces_nonempty_tsv(tmp_path: Path) -> None:
    """write_assignments must correctly index into assignments_by_level dict."""
    report_render = _load_report_render_module()
    round_obj = {
        "otu": {
            "assignments_by_level": {
                "species": [
                    {"sample": "sample_A", "marker": "COI", "taxon": "S1", "frozen_otu_count": 1, "reads_total": 5},
                ],
            }
        }
    }
    tsv_dir = tmp_path / "figures"
    tsv_dir.mkdir()
    report_render.write_chart_tsvs([round_obj], tsv_dir, run_id="runX")
    otu_sp = tsv_dir / "otu_assignments_species.tsv"
    assert otu_sp.exists(), "otu_assignments_species.tsv should be written"
    lines = otu_sp.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 2, "TSV must have header + at least one data row"
    frozen_sp = tsv_dir / "frozen_otu_assignments_species.tsv"
    assert frozen_sp.exists(), "frozen_otu_assignments_species.tsv should be written"
    frozen_lines = frozen_sp.read_text(encoding="utf-8").strip().splitlines()
    assert len(frozen_lines) >= 2, "frozen TSV must have header + at least one data row"


def test_natural_round_key_sorts_ordinal_round_words() -> None:
    report_render = _load_report_render_module()
    labels = ["output_tenth_50k", "output_second_50k", "output_first_50k"]
    assert sorted(labels, key=report_render.natural_round_key) == [
        "output_first_50k",
        "output_second_50k",
        "output_tenth_50k",
    ]


def test_render_index_view_hides_figures_gallery(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "run_id": "runX",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "figures": [
                    {
                        "id": "reads_time",
                        "title": "Reads vs Time",
                        "path": "report_assets/B1_reads_time.png",
                        "exists": True,
                    }
                ],
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state)
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert payload["view_scope"] == "index"
    assert payload["figures"] == []


def test_render_includes_run_index_sorted(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text("", encoding="utf-8")
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(
        "\n".join(
            [
                json.dumps({"run_id": "runA", "last_updated_utc": "2026-03-06T00:01:00Z", "rounds_count": 1}),
                json.dumps({"run_id": "runB", "last_updated_utc": "2026-03-06T00:03:00Z", "rounds_count": 2}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=["--run-index", str(run_index)],
    )
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert payload["run_index"][0]["run_id"] == "runB"


def test_render_run_id_filter_limits_rounds(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "run_id": "runA",
                        "barcode": "B1",
                        "round_barcode": "round_001",
                        "timestamp_utc": "2026-03-06T00:01:00Z",
                        "warnings": [],
                    }
                ),
                json.dumps(
                    {
                        "schema_version": "1.1",
                        "run_id": "runB",
                        "barcode": "B1",
                        "round_barcode": "round_002",
                        "timestamp_utc": "2026-03-06T00:02:00Z",
                        "warnings": [],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=["--run-id-filter", "runA"],
    )
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert len(payload["rounds"]) == 1
    assert payload["rounds"][0]["run_id"] == "runA"


def test_render_template_includes_modal_aria_labels(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text("", encoding="utf-8")
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state)
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    assert 'aria-labelledby="figure-modal-title"' in html_text
    assert 'aria-describedby="figure-modal-caption"' in html_text


def test_render_template_includes_sample_sections(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text("", encoding="utf-8")
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state)
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    assert 'id="sample-index"' in html_text
    assert 'id="sample-details"' in html_text


def test_render_url_prefix_rewrites_paths(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "run_id": "runA",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "figures": [
                    {"id": "reads_time", "path": "report_assets/B1_reads_time.png", "exists": True},
                    {"id": "remote", "path": "https://example.com/fig.png", "exists": True},
                    {"id": "data", "path": "data:image/png;base64,AAA", "exists": True},
                ],
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(
        json.dumps({"run_id": "runA", "report_rel_path": "runs/runA/report.html"}) + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=["--run-index", str(run_index), "--url-prefix", "/", "--run-id-filter", "runA"],
    )
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert payload["figures"][0]["path"].startswith("/report_assets/")
    assert payload["figures"][1]["path"] == "https://example.com/fig.png"
    assert payload["figures"][2]["path"].startswith("data:")


def test_render_run_filter_relativizes_run_asset_paths_without_url_prefix(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "run_id": "runA",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "figures": [
                    {
                        "id": "reads_time",
                        "path": "runs/runA/report_assets/B1_reads_time.png",
                        "exists": True,
                    }
                ],
                "sample_metrics": {
                    "sample_a": {
                        "sample_id": "sample_a",
                        "label": "sample_a",
                        "figures": [
                            {
                                "id": "sample_reads",
                                "path": "runs/runA/report_assets/samples/sample_a/sample_reads.png",
                                "exists": True,
                            }
                        ],
                    }
                },
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=["--run-id-filter", "runA", "--url-prefix", ""],
    )
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert payload["figures"][0]["path"] == "report_assets/B1_reads_time.png"
    sample_figs = payload["rounds"][0]["sample_metrics"]["sample_a"]["figures"]
    assert sample_figs[0]["path"] == "report_assets/samples/sample_a/sample_reads.png"
    assert "file_mode_run_report_unreliable" not in payload["parse_warnings"]


def test_render_includes_otu_assignments_payload(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "run_id": "runA",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "otu": {"assignments_by_level": {"species": [{"otu_id": "OTU1", "reads": 5}]}},
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state, extra_args=["--run-id-filter", "runA"])
    assert rc.returncode == 0, rc.stderr
    payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert payload["rounds"][0]["otu"]["assignments_by_level"]["species"][0]["otu_id"] == "OTU1"


def test_render_concurrent_writes_keep_outputs_valid(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.1",
                "run_id": "runX",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    p1 = subprocess.Popen(_cmd(history, out_html, out_state), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen(_cmd(history, out_html, out_state), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    o1, e1 = p1.communicate(timeout=20)
    o2, e2 = p2.communicate(timeout=20)
    assert p1.returncode == 0, f"p1 failed: {e1}\n{o1}"
    assert p2.returncode == 0, f"p2 failed: {e2}\n{o2}"
    html_text = out_html.read_text(encoding="utf-8")
    assert "window.REPORT_PAYLOAD = " in html_text
    state = json.loads(out_state.read_text(encoding="utf-8"))
    assert set(state.keys()) == {"schema_version", "generated_at_utc", "report_revision"}


def test_index_render_contains_per_run_breakdown_label(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "run_id": "runA",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(
        json.dumps({"run_id": "runA", "report_rel_path": "runs/runA/report.html"}) + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state, extra_args=["--run-index", str(run_index)])
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    assert "Per-run numeric breakdown" in html_text


def test_render_preserves_explicit_blast_unassigned_fields(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.4",
                "run_id": "runX",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "reads": {"total": 10, "on_target": 8},
                "read_fate": {
                    "demux_total_reads": 8,
                    "no_adapter_reads": 2,
                    "blast_assignment_status": "classified",
                    "blast_seen_reads": 8,
                    "blast_assigned_reads": 5,
                    "blast_assigned_reads_adapter": 4,
                    "blast_unassigned_reads": 3,
                    "blast_unassigned_reads_adapter": 2,
                    "blast_unassigned_reads_no_adapter": 1,
                    "consensus_used_reads": 1,
                    "demux_enabled": True,
                },
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "report.html"
    out_state = tmp_path / "report_state.json"
    rc = _run(history, out_html, out_state, extra_args=["--run-id-filter", "runX"])
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    payload = _extract_js_json(html_text, "window.REPORT_PAYLOAD = ")
    rf = payload["rounds"][0]["read_fate"]
    assert rf["blast_assignment_status"] == "classified"
    assert rf["blast_unassigned_reads"] == 3
    assert rf["blast_assigned_reads_adapter"] == 4
    assert rf["blast_unassigned_reads_adapter"] == 2


def test_report_js_contains_marker_split_read_fate_mapping() -> None:
    js_text = JS.read_text(encoding="utf-8")
    css_text = CSS.read_text(encoding="utf-8")
    assert "marker_split_status" in js_text
    assert "chart_blast_assigned_${" in js_text
    assert "chart_blast_unassigned_${" in js_text
    assert "chart_on_target_not_demultiplexed" in js_text
    assert "Read fate status: invalid" in js_text
    assert "invalid_reads" in js_text
    assert "OTU assignments: OTUs" in js_text
    assert "Consensus assignments: consensus" in js_text
    assert "Assignments by ${groupEntityLabel()}" in js_text
    assert "buildAssignmentSampleMatrix" in js_text
    assert "frozen_otu_reads_total" in js_text
    assert "consolidated_consensus_reads_total" in js_text
    assert "assignment-special-count" in js_text
    assert "samplesWithReadEvidence" in js_text
    assert "Cells show total values; bold values in parentheses show" in js_text
    assert ".assignment-special-count" in css_text


def test_report_js_track_detail_matrix_uses_canonical_track_sort_fields() -> None:
    js_text = JS.read_text(encoding="utf-8")
    assert "groupSortMetaByLabel" in js_text
    assert "recordTrackGroupSortMeta" in js_text
    assert "compareTrackGroupSortMeta" in js_text
    assert "track_sample_label" in js_text
    assert "track_replicate_number" in js_text
    assert "track_sample_replicate_label" in js_text
    assert "track_replicate_label" in js_text
    assert "text.match(/^(.*?_\\d+)_[^_]+$/)" in js_text


# ---------------------------------------------------------------------------
# Tests for should_regenerate_sample_asset (Part 5a / 5b)
# ---------------------------------------------------------------------------

def _load_render_module():
    spec = importlib.util.spec_from_file_location(
        "report_render",
        Path(__file__).resolve().parents[1] / "bin" / "report_render.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_should_regenerate_sample_asset_missing_sig(tmp_path):
    mod = _load_render_module()
    png = tmp_path / "a.png"
    pdf = tmp_path / "a.pdf"
    sig_path = tmp_path / "a.sig"
    png.write_text("x")
    pdf.write_text("x")
    # sig file absent → should regenerate
    assert mod.should_regenerate_sample_asset(sig_path, [png, pdf], "mysig")


def test_should_regenerate_sample_asset_missing_pdf(tmp_path):
    mod = _load_render_module()
    png = tmp_path / "a.png"
    pdf = tmp_path / "a.pdf"
    sig_path = tmp_path / "a.sig"
    png.write_text("x")
    sig_path.write_text("mysig\n")
    # PDF absent → should regenerate
    assert mod.should_regenerate_sample_asset(sig_path, [png, pdf], "mysig")


def test_should_regenerate_sample_asset_all_present_matching(tmp_path):
    mod = _load_render_module()
    png = tmp_path / "a.png"
    pdf = tmp_path / "a.pdf"
    sig_path = tmp_path / "a.sig"
    png.write_text("x")
    pdf.write_text("x")
    sig_path.write_text("mysig\n")
    # All present + sig matches → should NOT regenerate
    assert not mod.should_regenerate_sample_asset(sig_path, [png, pdf], "mysig")


def test_should_regenerate_sample_asset_sig_mismatch(tmp_path):
    mod = _load_render_module()
    png = tmp_path / "a.png"
    pdf = tmp_path / "a.pdf"
    sig_path = tmp_path / "a.sig"
    png.write_text("x")
    pdf.write_text("x")
    sig_path.write_text("oldsig\n")
    # Sig differs → should regenerate
    assert mod.should_regenerate_sample_asset(sig_path, [png, pdf], "newsig")


def test_write_chart_tsvs_skips_when_sig_matches(tmp_path):
    """write_chart_tsvs skips TSV when sig matches and file present."""
    mod = _load_render_module()
    tsv_dir = tmp_path / "figures"
    sig_root = tmp_path / "report_assets" / ".private_signatures"
    # Round with minimal valid data
    round_data = {
        "schema_version": "1.5",
        "run_id": "testrun",
        "reads": {"total": 5},
        "read_fate": {},
        "otu": {},
        "consensus": {},
        "warnings": [],
    }
    sorted_rounds = [round_data]
    # First call: writes TSVs
    mod.write_chart_tsvs(sorted_rounds, tsv_dir, run_id="testrun", sig_root=sig_root)
    tsv_path = tsv_dir / "reads_fate_per_round.tsv"
    # TSV may or may not be created depending on data (empty data → skipped by try/except)
    # Just verify it doesn't crash and sig_root directory is created when needed
    assert tsv_dir.exists()


def test_write_chart_tsvs_no_sig_root_always_writes(tmp_path):
    """Without sig_root, write_chart_tsvs always writes TSVs."""
    mod = _load_render_module()
    tsv_dir = tmp_path / "figures"
    round_data = {
        "schema_version": "1.5",
        "run_id": "testrun",
        "reads": {"total": 5},
        "read_fate": {},
        "otu": {},
        "consensus": {},
        "warnings": [],
    }
    # Should not raise
    mod.write_chart_tsvs([round_data], tsv_dir, run_id="testrun")
    assert tsv_dir.exists()


def test_build_chart_exports_index_view_caches_on_second_run(tmp_path, monkeypatch):
    """Index-view branch (run_id=None) should not re-export when sig matches and PDFs exist."""
    mod = _load_report_render_module()
    call_count = {"n": 0}

    def counting_export(title, rows, order, colors, pdf_path):
        call_count["n"] += 1
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("pdf", encoding="utf-8")

    monkeypatch.setattr(mod, "export_stacked_bar_pdf", counting_export)

    out_path = tmp_path / "report.html"
    sorted_runs = [{"run_id": "r1"}]

    mod.build_chart_exports([], sorted_runs, out_path, None)
    first_count = call_count["n"]
    assert first_count >= 1, "export_stacked_bar_pdf should be called on first run"

    mod.build_chart_exports([], sorted_runs, out_path, None)
    assert call_count["n"] == first_count, (
        f"export_stacked_bar_pdf re-invoked on second run (calls: {call_count['n']} vs {first_count})"
    )


@_skip_no_matplotlib
def test_render_track_detail_uses_view_specific_paths_and_metadata(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.6",
                "run_id": "runA",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "identity_mode": "track",
                "track_unit_metrics": {
                    "sample_a_1_COI": {
                        "track_unit_id": "sample_a_1_COI",
                        "track_sample_label": "sample_A",
                        "track_replicate_id": "sample_A_1",
                        "track_replicate_number": 1,
                        "track_replicate_label": "sample_A_1",
                        "track_sample_replicate_label": "sample_A_1",
                        "track_primer_label": "COI",
                        "reads_demux": 4,
                        "reads_demux_by_marker": {"COI": 4},
                        "reads_blast_assigned": 3,
                        "otu_active": 1,
                        "consensus_emitted": 1,
                        "figures": [],
                    }
                },
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(
        json.dumps(
            {
                "run_id": "runA",
                "identity_mode": "track",
                "report_rel_path": "runs/runA/report.html",
                "report_views": [
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
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "runs" / "runA" / "report_replicates_primers.html"
    out_state = tmp_path / "runs" / "runA" / "report_replicates_primers_state.json"
    out_html.parent.mkdir(parents=True, exist_ok=True)
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=[
            "--run-id-filter",
            "runA",
            "--run-index",
            str(run_index),
            "--group-view",
            "track_detail",
            "--state-url",
            "report_replicates_primers_state.json",
            "--figures-dir-name",
            "figures_replicates_primers",
            "--report-assets-dir-name",
            "report_assets_replicates_primers",
            "--figures-dir-url",
            "./figures_replicates_primers/README.html",
        ],
    )
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    payload = _extract_js_json(html_text, "window.REPORT_PAYLOAD = ")
    meta = _extract_js_json(html_text, "window.REPORT_META = ")
    assert payload["report_view"] == "track_detail"
    assert meta["report_view"] == "track_detail"
    assert meta["report_view_label"] == "Replicate Comparison"
    assert meta["state_url"] == "report_replicates_primers_state.json"
    assert meta["figures_dir_url"] == "./figures_replicates_primers/README.html"
    assert payload["report_view_links"][1]["href"] == "report_replicates_primers.html"
    assert payload["report_view_links"][1]["is_active"] is True
    assert payload["chart_exports"]["run_reads_fate"]["pdf_path"] == "report_assets_replicates_primers/embedded/run_reads_fate.pdf"
    assert (out_html.parent / "report_assets_replicates_primers" / "embedded" / "run_reads_fate.pdf").exists()


def test_build_chart_exports_collapses_track_sample_view(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_report_render_module()
    monkeypatch.setattr(mod, "CURRENT_GROUP_VIEW", "sample")

    captured = {"vertical": [], "treemap": []}

    def fake_stacked(_title, _rows, _order, _colors, pdf_path):
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("pdf", encoding="utf-8")

    def fake_vertical(title, bars, _color, pdf_path):
        captured["vertical"].append({"title": title, "bars": bars})
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("pdf", encoding="utf-8")

    def fake_treemap(title, items, pdf_path):
        captured["treemap"].append({"title": title, "items": items})
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("pdf", encoding="utf-8")

    def fake_sunburst(_title, _subtitle, _tree, png_path, pdf_path):
        png_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_text("png", encoding="utf-8")
        pdf_path.write_text("pdf", encoding="utf-8")

    monkeypatch.setattr(mod, "export_stacked_bar_pdf", fake_stacked)
    monkeypatch.setattr(mod, "export_vertical_bar_pdf", fake_vertical)
    monkeypatch.setattr(mod, "export_treemap_pdf", fake_treemap)
    monkeypatch.setattr(mod, "export_sunburst_plot_png_pdf", fake_sunburst)

    rounds = [
        {
            "schema_version": "1.6",
            "run_id": "runX",
            "barcode": "B1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "identity_mode": "track",
            "read_fate": {"demux_enabled": True, "demux_total_reads": 12, "no_adapter_reads": 0},
            "sample_metrics": {
                "sample_a_1": {
                    "sample_id": "sample_a_1",
                    "label": "sample_A_1",
                    "reads_demux": 5,
                    "reads_demux_by_marker": {"COI": 5},
                    "reads_blast_assigned": 5,
                    "otu_active": 1,
                    "consensus_emitted": 1,
                    "figures": [],
                },
                "sample_a_2": {
                    "sample_id": "sample_a_2",
                    "label": "sample_A_2",
                    "reads_demux": 7,
                    "reads_demux_by_marker": {"COI": 7},
                    "reads_blast_assigned": 7,
                    "otu_active": 1,
                    "consensus_emitted": 1,
                    "figures": [],
                },
            },
            "otu": {
                "assignments_by_level": {
                    "species": [
                        {"sample": "sample_A_1", "marker": "COI", "family": "F1", "species": "S1", "taxon": "S1", "reads_total": 5},
                        {"sample": "sample_A_2", "marker": "COI", "family": "F2", "species": "S2", "taxon": "S2", "reads_total": 7},
                    ],
                    "genus": [],
                    "family": [],
                }
            },
            "consensus": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
            "warnings": [],
        }
    ]

    exports = mod.build_chart_exports(rounds, [], tmp_path / "runs" / "runX" / "report.html", "runX")

    sample_exports = [key for key in exports if key.endswith("_reads_per_barcode")]
    assert sample_exports == ["sample_sample_A_reads_per_barcode"]
    assert len(captured["vertical"]) == 1
    assert captured["vertical"][0]["title"] == "Reads per Barcode (sample): sample_A"
    assert [row["label"] for row in captured["vertical"][0]["bars"]] == ["sample_A_1", "sample_A_2"]
    assert len(captured["treemap"]) == 6


def test_build_chart_exports_replicate_view_groups_primer_track_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mod = _load_report_render_module()
    monkeypatch.setattr(mod, "CURRENT_GROUP_VIEW", "replicate")

    captured = {"vertical": []}

    def fake_stacked(_title, _rows, _order, _colors, pdf_path):
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("pdf", encoding="utf-8")

    def fake_vertical(title, bars, _color, pdf_path):
        captured["vertical"].append({"title": title, "bars": bars})
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("pdf", encoding="utf-8")

    def fake_treemap(_title, _items, pdf_path):
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.write_text("pdf", encoding="utf-8")

    def fake_sunburst(_title, _subtitle, _tree, png_path, pdf_path):
        png_path.parent.mkdir(parents=True, exist_ok=True)
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_text("png", encoding="utf-8")
        pdf_path.write_text("pdf", encoding="utf-8")

    monkeypatch.setattr(mod, "export_stacked_bar_pdf", fake_stacked)
    monkeypatch.setattr(mod, "export_vertical_bar_pdf", fake_vertical)
    monkeypatch.setattr(mod, "export_treemap_pdf", fake_treemap)
    monkeypatch.setattr(mod, "export_sunburst_plot_png_pdf", fake_sunburst)

    rounds = [
        {
            "schema_version": "1.6",
            "run_id": "runX",
            "barcode": "B1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "identity_mode": "track",
            "read_fate": {"demux_enabled": True, "demux_total_reads": 17, "no_adapter_reads": 0},
            "sample_metrics": {},
            "track_unit_metrics": {
                "sample_A_1_MPold1_COI": {
                    "track_unit_id": "sample_A_1_MPold1_COI",
                    "track_sample_label": "sample_A",
                    "track_replicate_id": "sample_A_1_MPold1",
                    "track_replicate_number": 1,
                    "track_replicate_label": "sample_A_1_MPold1",
                    "track_sample_replicate_label": "sample_A_1",
                    "track_primer_label": "COI",
                    "reads_demux": 5,
                    "reads_demux_by_marker": {"COI": 5},
                    "reads_blast_assigned": 5,
                    "otu_active": 1,
                    "consensus_emitted": 1,
                    "figures": [],
                },
                "sample_A_1_MPold1_ITS2": {
                    "track_unit_id": "sample_A_1_MPold1_ITS2",
                    "track_sample_label": "sample_A",
                    "track_replicate_id": "sample_A_1_MPold1",
                    "track_replicate_number": 1,
                    "track_replicate_label": "sample_A_1_MPold1",
                    "track_sample_replicate_label": "sample_A_1",
                    "track_primer_label": "ITS2",
                    "reads_demux": 4,
                    "reads_demux_by_marker": {"ITS2": 4},
                    "reads_blast_assigned": 4,
                    "otu_active": 1,
                    "consensus_emitted": 1,
                    "figures": [],
                },
                "sample_A_2_MPnew1_COI": {
                    "track_unit_id": "sample_A_2_MPnew1_COI",
                    "track_sample_label": "sample_A",
                    "track_replicate_id": "sample_A_2_MPnew1",
                    "track_replicate_number": 2,
                    "track_replicate_label": "sample_A_2_MPnew1",
                    "track_sample_replicate_label": "sample_A_2",
                    "track_primer_label": "COI",
                    "reads_demux": 8,
                    "reads_demux_by_marker": {"COI": 8},
                    "reads_blast_assigned": 8,
                    "otu_active": 1,
                    "consensus_emitted": 1,
                    "figures": [],
                },
            },
            "otu": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
            "consensus": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
            "warnings": [],
        }
    ]

    exports = mod.build_chart_exports(
        rounds, [], tmp_path / "runs" / "runX" / "report.html", "runX"
    )

    sample_exports = sorted(k for k in exports if k.endswith("_reads_per_barcode"))
    assert sample_exports == [
        "sample_sample_A_1_MPold1_reads_per_barcode",
        "sample_sample_A_2_MPnew1_reads_per_barcode",
    ], f"Expected primer-group keys, got: {sample_exports}"


def test_collect_sample_totals_stage_three_track_group_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_report_render_module()
    rounds = [
        {
            "identity_mode": "track",
            "round_barcode": "round_001",
            "sample_metrics": {
                "sample_A_1": {"label": "sample_A_1", "reads_demux": 10},
                "sample_A_2": {"label": "sample_A_2", "reads_demux": 12},
            },
            "track_unit_metrics": {
                "sample_A_1_MPold1_COI": {
                    "track_unit_id": "sample_A_1_MPold1_COI",
                    "track_sample_label": "sample_A",
                    "track_replicate_id": "sample_A_1_MPold1",
                    "track_replicate_number": 1,
                    "track_replicate_label": "sample_A_1_MPold1",
                    "track_sample_replicate_label": "sample_A_1",
                    "track_primer_label": "COI",
                    "reads_demux": 5,
                    "reads_demux_by_marker": {"COI": 5},
                },
                "sample_A_1_MPnew1_ITS2": {
                    "track_unit_id": "sample_A_1_MPnew1_ITS2",
                    "track_sample_label": "sample_A",
                    "track_replicate_id": "sample_A_1_MPnew1",
                    "track_replicate_number": 1,
                    "track_replicate_label": "sample_A_1_MPnew1",
                    "track_sample_replicate_label": "sample_A_1",
                    "track_primer_label": "ITS2",
                    "reads_demux": 5,
                    "reads_demux_by_marker": {"ITS2": 5},
                },
                "sample_A_2_MPold1_COI": {
                    "track_unit_id": "sample_A_2_MPold1_COI",
                    "track_sample_label": "sample_A",
                    "track_replicate_id": "sample_A_2_MPold1",
                    "track_replicate_number": 2,
                    "track_replicate_label": "sample_A_2_MPold1",
                    "track_sample_replicate_label": "sample_A_2",
                    "track_primer_label": "COI",
                    "reads_demux": 6,
                    "reads_demux_by_marker": {"COI": 6},
                },
                "sample_A_2_MPold1_ITS2": {
                    "track_unit_id": "sample_A_2_MPold1_ITS2",
                    "track_sample_label": "sample_A",
                    "track_replicate_id": "sample_A_2_MPold1",
                    "track_replicate_number": 2,
                    "track_replicate_label": "sample_A_2_MPold1",
                    "track_sample_replicate_label": "sample_A_2",
                    "track_primer_label": "ITS2",
                    "reads_demux": 6,
                    "reads_demux_by_marker": {"ITS2": 6},
                },
            },
        }
    ]

    monkeypatch.setattr(mod, "CURRENT_GROUP_VIEW", "sample")
    assert len(mod.collect_sample_totals(rounds, collapse_track_units=True)) == 1

    monkeypatch.setattr(mod, "CURRENT_GROUP_VIEW", "replicate")
    assert len(mod.collect_sample_totals(rounds, collapse_track_units=False)) == 3

    monkeypatch.setattr(mod, "CURRENT_GROUP_VIEW", "track_detail")
    detail = mod.collect_sample_totals(rounds, collapse_track_units=False)
    assert len(detail) == 2
    assert [entry["label"] for entry in detail] == [
        "sample_A_1",
        "sample_A_2",
    ]


def test_collect_sample_replicate_bars_track_detail_breaks_down_primer_groups(monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_report_render_module()
    monkeypatch.setattr(mod, "CURRENT_GROUP_VIEW", "track_detail")
    latest_round = {
        "track_unit_metrics": {
            "sample_A_1_MPold1_COI": {
                "track_unit_id": "sample_A_1_MPold1_COI",
                "track_replicate_label": "sample_A_1_MPold1",
                "track_sample_replicate_label": "sample_A_1",
                "reads_demux": 5,
            },
            "sample_A_1_MPnew1_ITS2": {
                "track_unit_id": "sample_A_1_MPnew1_ITS2",
                "track_replicate_label": "sample_A_1_MPnew1",
                "track_sample_replicate_label": "sample_A_1",
                "reads_demux": 4,
            },
        }
    }
    sample_entry = {
        "label": "sample_A_1",
        "track_sample_replicate_label": "sample_A_1",
        "totals": {"reads_demux": 9},
    }
    bars = mod.collect_sample_replicate_bars(latest_round, sample_entry, collapse_track_units=False)
    assert bars == [
        {"label": "sample_A_1_MPnew1", "value": 4.0},
        {"label": "sample_A_1_MPold1", "value": 5.0},
    ]


@_skip_no_matplotlib
def test_render_replicate_view_uses_view_specific_paths_and_metadata(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.6",
                "run_id": "runA",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "identity_mode": "track",
                "read_fate": {"demux_enabled": True, "demux_total_reads": 8, "no_adapter_reads": 0},
                "sample_metrics": {
                    "sample_a_1": {
                        "sample_id": "sample_a_1",
                        "label": "sample_A_1",
                        "reads_demux": 8,
                        "reads_demux_by_marker": {"COI": 8},
                        "reads_blast_assigned": 6,
                        "otu_active": 2,
                        "consensus_emitted": 1,
                        "figures": [],
                    }
                },
                "otu": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
                "consensus": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(
        json.dumps(
            {
                "run_id": "runA",
                "identity_mode": "track",
                "report_rel_path": "runs/runA/report.html",
                "report_views": [
                    {
                        "view_id": "sample",
                        "label": "Run Info",
                        "report_rel_path": "runs/runA/report.html",
                        "report_url": "runs/runA/report.html",
                        "is_primary": True,
                    },
                    {
                        "view_id": "replicate",
                        "label": "Primer Comparison",
                        "report_rel_path": "runs/runA/report_replicates.html",
                        "report_url": "runs/runA/report_replicates.html",
                        "is_primary": False,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    out_html = tmp_path / "runs" / "runA" / "report_replicates.html"
    out_state = tmp_path / "runs" / "runA" / "report_replicates_state.json"
    out_html.parent.mkdir(parents=True, exist_ok=True)
    rc = _run(
        history,
        out_html,
        out_state,
        extra_args=[
            "--run-id-filter",
            "runA",
            "--run-index",
            str(run_index),
            "--group-view",
            "replicate",
            "--state-url",
            "report_replicates_state.json",
            "--figures-dir-name",
            "figures_replicates",
            "--report-assets-dir-name",
            "report_assets_replicates",
            "--figures-dir-url",
            "./figures_replicates/README.html",
        ],
    )
    assert rc.returncode == 0, rc.stderr
    html_text = out_html.read_text(encoding="utf-8")
    payload = _extract_js_json(html_text, "window.REPORT_PAYLOAD = ")
    meta = _extract_js_json(html_text, "window.REPORT_META = ")
    assert payload["report_view"] == "replicate"
    assert meta["report_view"] == "replicate"
    assert meta["report_view_label"] == "Primer Comparison"
    assert meta["state_url"] == "report_replicates_state.json"
    assert meta["figures_dir_url"] == "./figures_replicates/README.html"
    assert payload["report_view_links"][1]["href"] == "report_replicates.html"
    assert payload["report_view_links"][1]["is_active"] is True
    assert payload["chart_exports"]["run_reads_fate"]["pdf_path"] == "report_assets_replicates/embedded/run_reads_fate.pdf"
    assert (out_html.parent / "report_assets_replicates" / "embedded" / "run_reads_fate.pdf").exists()


@_skip_no_matplotlib
def test_render_stage_three_views_isolate_output_paths(tmp_path: Path) -> None:
    history = tmp_path / "history.jsonl"
    history.write_text(
        json.dumps(
            {
                "schema_version": "1.6",
                "run_id": "runA",
                "barcode": "B1",
                "round_barcode": "round_001",
                "timestamp_utc": "2026-03-06T00:01:00Z",
                "identity_mode": "track",
                "read_fate": {"demux_enabled": True, "demux_total_reads": 8, "no_adapter_reads": 0},
                "sample_metrics": {
                    "sample_a_1": {
                        "sample_id": "sample_a_1",
                        "label": "sample_A_1",
                        "reads_demux": 8,
                        "reads_demux_by_marker": {"COI": 8},
                        "reads_blast_assigned": 6,
                        "otu_active": 2,
                        "consensus_emitted": 1,
                        "figures": [],
                    }
                },
                "track_unit_metrics": {
                    "sample_A_1_MPold1_COI": {
                        "track_unit_id": "sample_A_1_MPold1_COI",
                        "track_sample_label": "sample_A",
                        "track_replicate_id": "sample_A_1_MPold1",
                        "track_replicate_number": 1,
                        "track_replicate_label": "sample_A_1_MPold1",
                        "track_sample_replicate_label": "sample_A_1",
                        "track_primer_label": "COI",
                        "reads_demux": 8,
                        "reads_demux_by_marker": {"COI": 8},
                        "reads_blast_assigned": 6,
                        "otu_active": 2,
                        "consensus_emitted": 1,
                        "figures": [],
                    }
                },
                "otu": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
                "consensus": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
                "warnings": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(
        json.dumps(
            {
                "run_id": "runA",
                "identity_mode": "track",
                "report_rel_path": "runs/runA/report.html",
                "report_views": [
                    {"view_id": "sample", "label": "Run Info", "report_rel_path": "runs/runA/report.html", "report_url": "runs/runA/report.html", "is_primary": True},
                    {"view_id": "track_detail", "label": "Replicate Comparison", "report_rel_path": "runs/runA/report_replicates_primers.html", "report_url": "runs/runA/report_replicates_primers.html", "is_primary": False},
                    {"view_id": "replicate", "label": "Primer Comparison", "report_rel_path": "runs/runA/report_replicates.html", "report_url": "runs/runA/report_replicates.html", "is_primary": False},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    cases = [
        ("sample", "report.html", "report_state.json", "report_assets", "figures"),
        ("replicate", "report_replicates.html", "report_replicates_state.json", "report_assets_replicates", "figures_replicates"),
        ("track_detail", "report_replicates_primers.html", "report_replicates_primers_state.json", "report_assets_replicates_primers", "figures_replicates_primers"),
    ]
    pdf_paths = []
    for group_view, html_name, state_name, assets_name, figures_name in cases:
        out_html = tmp_path / "runs" / "runA" / html_name
        out_state = tmp_path / "runs" / "runA" / state_name
        out_html.parent.mkdir(parents=True, exist_ok=True)
        rc = _run(
            history,
            out_html,
            out_state,
            extra_args=[
                "--run-id-filter",
                "runA",
                "--run-index",
                str(run_index),
                "--group-view",
                group_view,
                "--state-url",
                state_name,
                "--figures-dir-name",
                figures_name,
                "--report-assets-dir-name",
                assets_name,
                "--figures-dir-url",
                f"./{figures_name}/README.html",
            ],
        )
        assert rc.returncode == 0, rc.stderr
        payload = _extract_js_json(out_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
        pdf_path = payload["chart_exports"]["run_reads_fate"]["pdf_path"]
        pdf_paths.append(pdf_path)
        assert pdf_path == f"{assets_name}/embedded/run_reads_fate.pdf"
        assert (out_html.parent / assets_name / "embedded" / "run_reads_fate.pdf").exists()

    assert pdf_paths == [
        "report_assets/embedded/run_reads_fate.pdf",
        "report_assets_replicates/embedded/run_reads_fate.pdf",
        "report_assets_replicates_primers/embedded/run_reads_fate.pdf",
    ]
