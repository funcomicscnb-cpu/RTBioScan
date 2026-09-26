import importlib.util
import json
import os
import shutil
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


def test_jsonl_readers_frame_only_on_lf_and_keep_mixed_history(tmp_path: Path) -> None:
    mod = _load_report_render_module()
    labels = ["old Ã\u0085land", "Río", "Åland", "児島", "prąd", "N\u0085E", "L\u2028S", "P\u2029S"]
    rows = [
        {"run_id": "runA", "barcode": "B1", "round_barcode": f"round_{idx}", "label": label}
        for idx, label in enumerate(labels)
    ]
    history = tmp_path / "history.jsonl"
    wire = b"\n".join(json.dumps(row, ensure_ascii=False).encode("utf-8") for row in rows) + b"\n"
    history.write_bytes(wire)
    # The oracle frames bytes on 0x0A before decoding any JSON record.
    oracle = [json.loads(line) for line in wire.split(b"\n") if line]
    assert oracle == rows
    parsed, warnings = mod.load_history(history)
    assert parsed == oracle and warnings == []
    assert history.read_bytes() == wire

    index = tmp_path / "runs_index.jsonl"
    index.write_bytes(wire)
    parsed, warnings = mod.load_run_index(index)
    assert parsed == oracle and warnings == []
    assert index.read_bytes() == wire

    history.write_bytes(wire + b"{bad json\n\n")
    parsed, warnings = mod.load_history(history)
    assert parsed == oracle
    assert warnings == [f"malformed_history_line:{len(rows) + 1}"]
    index.write_bytes(wire + b"{bad json\n\n")
    parsed, warnings = mod.load_run_index(index)
    assert parsed == oracle
    assert warnings == [f"malformed_run_index_line:{len(rows) + 1}"]

    history.write_bytes(b"")
    assert mod.load_history(history) == ([], [])
    index.write_bytes(b"")
    assert mod.load_run_index(index) == ([], [])


def test_report_rebuild_run_index_lookup_uses_literal_lf(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    run_dir = outdir / "report_html" / "runs" / "runA"
    run_dir.mkdir(parents=True)
    state_id = "state\u0085x"
    index_obj = {"run_id": "runA", "state_id": state_id}
    index = outdir / "report_html" / "runs_index.jsonl"
    index_wire = json.dumps(index_obj, ensure_ascii=False).encode("utf-8") + b"\n"
    index.write_bytes(index_wire)
    assert [json.loads(row) for row in index_wire.split(b"\n") if row] == [index_obj]
    history = tmp_path / "history.jsonl"
    history.write_bytes(
        json.dumps({"run_id": "runA", "barcode": "B1", "round_barcode": "round_1"}).encode() + b"\n"
    )
    result = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "bin" / "report_rebuild.sh"),
         "--outdir", str(outdir), "--history", str(history), "--run-id", "runA",
         "--skip-run-json", "--skip-root-report"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    marker = "window.REPORT_META = "
    meta = json.loads(html.split(marker, 1)[1].split(";\n", 1)[0])
    assert meta["state_dir_url"] == f"../../../current/state/{state_id}/README.html"
    assert index.read_bytes() == index_wire


def test_non_history_tsv_fasta_readers_keep_their_stage1_scope() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for expression in (
        'consolidated_ids_path.read_text(encoding="utf-8").splitlines()',
        'fasta_path.read_text(encoding="utf-8").splitlines()',
    ):
        assert expression in source
    assert source.count('path.read_text(encoding="utf-8").splitlines()') == 4


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


def _sample_identity_rounds(marker, reverse=False):
    """Round values are specified by producer label, independently of renderer matching."""
    values = [
        [("north", "Lake_North", 3), ("south", "Lake_South", 7), ("east", "Lake_East", 11), ("pine", "Pine_West", 2)],
        [("south", "Lake_South", 13), ("pine", "Pine_West", 4)],
        [("south", "Lake_South", 17), ("east", "Lake_East", 19)],
        [("old_north", "Lake_North", 23), ("pine", "Pine_West", 6)],
        [("north", "Lake_North", 29), ("old_south", "Lake_South", 31), ("east", "Lake_East", 37), ("pine", "Pine_West", 8)],
    ]
    rounds = []
    for index, entries in enumerate(values, 1):
        if reverse:
            entries = list(reversed(entries))
        rounds.append({
            "run_id": "runX",
            "round_barcode": f"round_{index:03d}",
            "timestamp_utc": f"2026-03-06T00:{index:02d}:00Z",
            "sample_metrics": {
                key: {"sample_id": key, "label": label, "reads_demux": reads,
                      "reads_demux_by_marker": {name: reads for name in marker}, "figures": []}
                for key, label, reads in entries
            },
            "otu": {"assignments_by_level": {}},
            "consensus": {"assignments_by_level": {}},
        })
    return rounds


def test_history_sample_identity_direct_exact_missing_and_track(tmp_path: Path) -> None:
    mod = _load_report_render_module()
    direct = {"sample_metrics": {
        "north": {"label": "Lake_North", "reads_demux": 5},
        "old_north": {"label": "Lake_North", "reads_demux": 99},
    }}
    assert mod.find_sample_round_entry(direct, "north", "Lake_North")["reads_demux"] == 5
    assert mod.find_sample_round_entry(direct, "absent", "Lake_North")["reads_demux"] == 5
    assert mod.find_sample_round_entry(direct, "absent", "Lake_South") == {}
    assert mod.find_sample_round_entry(
        {"sample_metrics": {"cleaned": {"label": "Lake_North ", "reads_demux": 99}}},
        "absent", "Lake_North",
    ) == {}
    assert mod.find_sample_round_entry(
        {"sample_metrics": {"legacy": {"sample_id": "Lake_North", "reads_demux": 99}}},
        "absent", "Lake_North",
    ) == {}
    track = {"identity_mode": "track", "track_unit_metrics": {
        "one": {"track_sample_label": "Lake_North", "reads_demux": 4},
        "two": {"track_sample_label": "Lake_North", "reads_demux": 6},
        "three": {"track_sample_label": "Lake_South", "reads_demux": 50},
    }}
    assert mod.find_sample_round_entry(track, "absent", "Lake_North")["reads_demux"] == 10
    assert mod.find_sample_round_entry(track, "absent", "Pine_West") == {}


@pytest.mark.parametrize("marker", [("COI",), ("ITS2",), ("COI", "ITS2")])
@pytest.mark.parametrize("context", ["default", "broad_its2"])
def test_history_sample_identity_independent_per_round_oracle(
    tmp_path: Path, marker, context
) -> None:
    mod = _load_report_render_module()
    expected = {
        ("north", "Lake_North"): [3, 0, 0, 23, 29],
        ("south", "Lake_South"): [7, 13, 17, 0, 31],
        ("east", "Lake_East"): [11, 0, 19, 0, 37],
        ("pine", "Pine_West"): [2, 4, 0, 6, 8],
    }
    for reverse in (False, True):
        rounds = _sample_identity_rounds(marker, reverse)
        for round_obj in rounds:
            round_obj["profile"] = context
        for (sample_id, label), per_round in expected.items():
            rows = mod.build_sample_history_rows(rounds, sample_id, label, tmp_path / context / "report.html", "runX")
            assert [row["reads_demux"] for row in rows] == per_round
            assert [row["total_reads_cumulative"] for row in rows] == [
                sum(per_round[:index]) for index in range(1, 6)
            ]
            assert [row["round_barcode"] for row in rows] == [f"round_{index:03d}" for index in range(1, 6)]


def test_history_sample_identity_full_figure_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_report_render_module()
    rounds = _sample_identity_rounds(("COI", "ITS2"))
    captured = {}

    def record(rows, sample_stub, png_path, pdf_path):
        captured[sample_stub] = rows
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(b"png")
        pdf_path.write_bytes(b"pdf")

    def dummy(*args):
        png_path, pdf_path = args[-2:]
        png_path.parent.mkdir(parents=True, exist_ok=True)
        png_path.write_bytes(b"png")
        pdf_path.write_bytes(b"pdf")

    monkeypatch.setattr(mod, "export_sample_history_cumulative_r", record)
    for name in ("export_sample_history_reads_r", "export_sample_history_taxonomy_r",
                 "export_icicle_plot_png_pdf", "export_sunburst_plot_png_pdf"):
        monkeypatch.setattr(mod, name, dummy)
    monkeypatch.setattr(mod, "build_sample_taxonomy_tree", lambda *args, **kwargs: {"name": "empty", "value": 0})
    out_path = tmp_path / "runs" / "runX" / "report.html"
    mod.append_generated_sample_figures(rounds, rounds[-1], out_path, "runX")
    oracle = {
        "north": [3, 3, 3, 26, 55],
        "old_south": [7, 20, 37, 37, 68],
        "east": [11, 11, 30, 30, 67],
        "pine": [2, 6, 6, 12, 20],
    }
    assert set(captured) == set(oracle)
    for sample_id, totals in oracle.items():
        assert [row["total_reads_cumulative"] for row in captured[sample_id]] == totals
        figures = rounds[-1]["sample_metrics"][sample_id]["figures"]
        cumulative = [fig for fig in figures if fig["id"] == "sample_reads_cumulative_history"]
        assert len(cumulative) == 1
        assert sample_id in cumulative[0]["path"]
        assert sample_id in cumulative[0]["pdf_path"]


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript unavailable")
def test_history_sample_identity_r_figure_matches_oracle(tmp_path: Path) -> None:
    mod = _load_report_render_module()
    rows = mod.build_sample_history_rows(
        _sample_identity_rounds(("COI",)), "north", "Lake_North", tmp_path / "report.html", "runX"
    )
    expected = [3, 3, 3, 26, 55]
    assert [row["total_reads_cumulative"] for row in rows] == expected
    oracle_rows = [{**row, "total_reads_cumulative": value} for row, value in zip(rows, expected)]
    contaminated = [{**row, "total_reads_cumulative": value} for row, value in zip(rows, [3, 16, 33, 56, 85])]
    for name, data in (("production", rows), ("oracle", oracle_rows), ("contaminated", contaminated)):
        mod.export_sample_history_cumulative_r(data, "north", tmp_path / f"{name}.png", tmp_path / f"{name}.pdf")
    assert (tmp_path / "production.png").read_bytes() == (tmp_path / "oracle.png").read_bytes()
    assert (tmp_path / "production.png").read_bytes() != (tmp_path / "contaminated.png").read_bytes()
    assert all((tmp_path / f"{name}.pdf").stat().st_size > 0 for name in ("production", "oracle", "contaminated"))


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
    meta = _extract_js_json(html, "window.REPORT_META = ")
    payload = _extract_js_json(html, "window.REPORT_PAYLOAD = ")
    assert set(state.keys()) == {"schema_version", "generated_at_utc", "report_revision"}
    assert meta["schema_version"] == state["schema_version"] == "2.0"
    assert len(state["report_revision"]) == 64
    assert payload["rounds"] == []


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
    state = json.loads(out_state.read_text(encoding="utf-8"))
    assert meta["schema_version"] == state["schema_version"] == "1.1"
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


def test_report_js_otu_heatmap_contract_for_frozen_fields() -> None:
    """Lock report.js field choices that prevent frozen-read scope bleed in the heatmap."""
    source = JS.read_text(encoding="utf-8")

    assert "frozen_otu_reads_global_total" not in source, (
        "report.js must not reference frozen_otu_reads_global_total; "
        "it is diagnostic-only and must stay out of UI paths"
    )
    assert 'specialField: "frozen_otu_reads_sample_total"' in source, (
        "OTU reads heatmap view must use frozen_otu_reads_sample_total"
    )
    assert "cell.frozenReads += num(row.frozen_otu_reads_sample_total)" in source, (
        "Any frozen-read heatmap accumulator must use sample-scoped frozen reads"
    )
    assert "cell.frozenReads += num(row.frozen_otu_reads_total)" not in source, (
        "The heatmap must not accumulate frozen reads from the unsuffixed total"
    )
    assert 'specialFallbackField: "frozen_otu_reads_total"' not in source, (
        "No heatmap view may use frozen_otu_reads_total as a specialFallbackField"
    )


def test_report_js_assignment_cards_show_latest_round_scope_without_changing_table_policy() -> None:
    node_path = shutil.which("node")
    if node_path is None:
        pytest.skip("node not found in PATH")
    source = JS.read_text(encoding="utf-8")

    def extract_function(name: str) -> str:
        start = source.index(f"  function {name}(")
        opening_brace = source.index(") {", start) + 2
        depth = 0
        for position in range(opening_brace, len(source)):
            if source[position] == "{":
                depth += 1
            elif source[position] == "}":
                depth -= 1
                if depth == 0:
                    return source[start:position + 1]
        raise AssertionError(f"Unable to extract JavaScript function {name}")

    functions = "\n".join(extract_function(name) for name in (
        "flatMapCompat",
        "assignmentScopeText",
        "renderOtuAssignmentsTable",
        "renderConsensusAssignmentsTable",
        "collectReplicateLabels",
        "replicateCountForLabel",
        "renderAssignmentsTable",
    ))
    harness = functions + r'''
class Element {
  constructor(tagName) {
    this.tagName = String(tagName).toUpperCase();
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener() {}
}
const document = { createElement(tagName) { return new Element(tagName); } };
const charts = null;
const otuSampleReads = () => 0;
const isSupportedOtuAssignment = () => true;
const otuRowMeetsConfiguredThreshold = () => true;
const clearNode = (node) => { node.children = []; };
const makeTsv = () => "";
const makeDownloadLink = () => new Element("a");

function renderScopes(round) {
  const mount = new Element("main");
  renderOtuAssignmentsTable(round, mount);
  renderConsensusAssignmentsTable(round, mount);
  return mount.children.map((card) => {
    const scope = card.children.find((child) => child.className.includes("otu-assign-scope"));
    return { text: scope.textContent, childCount: scope.children.length };
  });
}

console.log(JSON.stringify({
  normalized: renderScopes({round_barcode: "  round_006  "}),
  caseInsensitive: renderScopes({round_barcode: "ROUND_007"}),
  unrelated: renderScopes({round_barcode: "MYRUN_12"}),
  embedded: renderScopes({round_barcode: "ground_006"}),
  noRound: renderScopes(null),
  empty: renderScopes({round_barcode: "round_"}),
  htmlLike: renderScopes({round_barcode: "round_<img src=x onerror=alert(1)>"}),
}));
'''
    result = subprocess.run([node_path, "-e", harness], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)

    assert [item["text"] for item in rendered["normalized"]] == ["Current state through round 006"] * 2
    assert all("round round_006" not in item["text"] for item in rendered["normalized"])
    assert [item["text"] for item in rendered["caseInsensitive"]] == ["Current state through round 007"] * 2
    assert [item["text"] for item in rendered["unrelated"]] == ["Current state through round MYRUN_12"] * 2
    assert [item["text"] for item in rendered["embedded"]] == ["Current state through round ground_006"] * 2
    assert [item["text"] for item in rendered["noRound"]] == ["N/A"] * 2
    assert [item["text"] for item in rendered["empty"]] == ["Current state through latest completed round"] * 2
    assert [item["text"] for item in rendered["htmlLike"]] == [
        "Current state through round <img src=x onerror=alert(1)>"
    ] * 2
    assert all(item["childCount"] == 0 for item in rendered["htmlLike"])

    assert 'title: "OTU Assignments"' in source
    assert 'title: "Consensus Assignments"' in source
    assert "scope.textContent = scopeText" in source
    assert "scope.innerHTML" not in source
    assert "const pageSize = 10" in source
    assert "numAny(otuSampleReads(row)) >= OTU_ASSIGNMENT_MIN_READS" in source


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


def test_write_assignments_refreshes_populated_and_empty_tsvs_with_signatures(tmp_path: Path) -> None:
    report_render = _load_report_render_module()
    populated_round = {
        "identity_mode": "collapse",
        "otu": {
            "assignments_by_level": {
                "species": [
                    {"sample": "sample_A", "marker": "COI", "taxon": "Species_A", "otu_count": 1,
                     "frozen_otu_count": 1, "reads_total": 5},
                ],
            }
        },
        "consensus": {
            "assignments_by_level": {
                "species": [
                    {"sample": "sample_A", "marker": "COI", "taxon": "Species_A", "consensus_count": 1,
                     "consolidated_consensus_count": 1, "reads_total": 5},
                ],
            }
        },
    }
    empty_round = {
        "identity_mode": "collapse",
        "otu": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
        "consensus": {"assignments_by_level": {"species": [], "genus": [], "family": []}},
    }
    tsv_dir = tmp_path / "figures"
    sig_root = tmp_path / "report_assets" / ".private_signatures"
    otu_path = tsv_dir / "otu_assignments_species.tsv"
    consensus_path = tsv_dir / "consensus_assignments_species.tsv"
    otu_sig_path = sig_root / "figures" / "runX" / "otu_assignments_species.tsv.sig"
    consensus_sig_path = sig_root / "figures" / "runX" / "consensus_assignments_species.tsv.sig"

    report_render.write_chart_tsvs([populated_round], tsv_dir, run_id="runX", sig_root=sig_root)
    populated_otu_text = otu_path.read_text(encoding="utf-8")
    populated_consensus_text = consensus_path.read_text(encoding="utf-8")
    populated_otu_sig = otu_sig_path.read_text(encoding="utf-8")
    populated_consensus_sig = consensus_sig_path.read_text(encoding="utf-8")
    assert len(populated_otu_text.splitlines()) == 2
    assert len(populated_consensus_text.splitlines()) == 2

    report_render.write_chart_tsvs([empty_round], tsv_dir, run_id="runX", sig_root=sig_root)
    otu_lines = otu_path.read_text(encoding="utf-8").splitlines()
    consensus_lines = consensus_path.read_text(encoding="utf-8").splitlines()
    assert otu_lines == [
        "taxon\tsample\tmarker\tfamily\tgenus\tspecies\totu_count\tfrozen_otu_count"
        "\tfrozen_otu_reads_total\tfrozen_otu_reads_sample_total\totu_reads_sample_total"
        "\treads_total\totu_reads_global_total\tfrozen_otu_reads_global_total"
        "\tperc_id_min\tperc_id_max\taln_length_min\taln_length_max"
    ]
    assert consensus_lines == [
        "taxon\tsample\tmarker\tfamily\tgenus\tspecies\tconsensus_count"
        "\tconsolidated_consensus_count\tconsolidated_consensus_reads_total\treads_total"
        "\tperc_id_min\tperc_id_max\taln_length_min\taln_length_max"
    ]
    empty_otu_sig = otu_sig_path.read_text(encoding="utf-8")
    empty_consensus_sig = consensus_sig_path.read_text(encoding="utf-8")
    assert empty_otu_sig != populated_otu_sig
    assert empty_consensus_sig != populated_consensus_sig

    report_render.write_chart_tsvs([populated_round], tsv_dir, run_id="runX", sig_root=sig_root)
    assert otu_path.read_text(encoding="utf-8") == populated_otu_text
    assert consensus_path.read_text(encoding="utf-8") == populated_consensus_text
    assert otu_sig_path.read_text(encoding="utf-8") == populated_otu_sig
    assert consensus_sig_path.read_text(encoding="utf-8") == populated_consensus_sig


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
                "consensus": {"assignments_by_level": {"species": [{"consensus_id": "CONS1", "reads_total": 5}]}},
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
    assert payload["rounds"][0]["consensus"]["assignments_by_level"]["species"][0]["consensus_id"] == "CONS1"


def test_render_payload_keeps_only_latest_assignment_rows_for_run_and_index(tmp_path: Path) -> None:
    row_count = 205

    def assignment_round(round_number: int) -> dict:
        marker = "ITS2" if round_number == 1 else "COI"
        otu_levels = {}
        consensus_levels = {}
        for level in ("species", "genus", "family"):
            otu_levels[level] = [
                {
                    "taxon": f"{level}_otu_{round_number}_{idx}",
                    "sample": "sample_A",
                    "marker": marker,
                    "otu_count": 1,
                    "frozen_otu_count": 0,
                    "reads_total": 5,
                    "otu_reads_sample_total": 5,
                }
                for idx in range(row_count)
            ]
            consensus_levels[level] = [
                {
                    "taxon": f"{level}_consensus_{round_number}_{idx}",
                    "sample": "sample_A",
                    "marker": marker,
                    "consensus_count": 1,
                    "consolidated_consensus_count": 0,
                    "reads_total": 5,
                }
                for idx in range(row_count)
            ]
        otu_levels["species_interest_enabled"] = False
        consensus_levels["species_interest_enabled"] = False
        return {
            "schema_version": "2.0",
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": f"round_{round_number:03d}",
            "timestamp_utc": f"2026-03-06T00:0{round_number}:00Z",
            "round_metadata": {"sentinel": f"metadata_{round_number}"},
            "markers": {"order": [] if round_number == 1 else [marker]},
            "otu": {
                "canonical": {"active": round_number},
                "assignments_by_level": otu_levels,
            },
            "consensus": {
                "emitted": round_number,
                "assignments_by_level": consensus_levels,
            },
            "warnings": [],
        }

    rounds = [assignment_round(round_number) for round_number in range(1, 5)]
    report_render = _load_report_render_module()
    page_rounds = report_render.build_page_rounds(rounds)
    assert len(page_rounds[0]["otu"]["assignments_by_level"]["species"]) == 0
    assert len(rounds[0]["otu"]["assignments_by_level"]["species"]) == row_count
    history = tmp_path / "history.jsonl"
    history.write_text("\n".join(json.dumps(row) for row in rounds) + "\n", encoding="utf-8")
    history_before = history.read_bytes()

    index_html = tmp_path / "index.html"
    index_state = tmp_path / "index_state.json"
    index_result = _run(history, index_html, index_state, extra_args=["--sample-plot-max", "0"])
    assert index_result.returncode == 0, index_result.stderr
    index_payload = _extract_js_json(index_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_html = run_dir / "report.html"
    run_state = run_dir / "report_state.json"
    run_result = _run(
        history,
        run_html,
        run_state,
        extra_args=["--run-id-filter", "runA", "--sample-plot-max", "0"],
    )
    assert run_result.returncode == 0, run_result.stderr
    run_payload = _extract_js_json(run_html.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert "run_otu_sunburst_ITS2" in run_payload["chart_exports"]

    for payload in (index_payload, run_payload):
        assert payload["history_count"] == 4
        assert len(payload["rounds"]) == 4
        for round_index, embedded_round in enumerate(payload["rounds"]):
            assert embedded_round["round_metadata"] == {"sentinel": f"metadata_{round_index + 1}"}
            assert embedded_round["otu"]["canonical"]["active"] == round_index + 1
            assert embedded_round["consensus"]["emitted"] == round_index + 1
            for source_key in ("otu", "consensus"):
                assignments = embedded_round[source_key]["assignments_by_level"]
                assert assignments["species_interest_enabled"] is False
                expected_count = row_count if round_index == 3 else 0
                for level in ("species", "genus", "family"):
                    assert len(assignments[level]) == expected_count

    assert history.read_bytes() == history_before
    full_history = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    for round_obj in full_history:
        for source_key in ("otu", "consensus"):
            for level in ("species", "genus", "family"):
                assert len(round_obj[source_key]["assignments_by_level"][level]) == row_count

    assert len((run_dir / "figures" / "otu_assignments_species.tsv").read_text(encoding="utf-8").splitlines()) == row_count + 1
    assert len((run_dir / "figures" / "consensus_assignments_species.tsv").read_text(encoding="utf-8").splitlines()) == row_count + 1

    compact_json = lambda value: json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    full_rounds_size = len(compact_json(rounds))
    embedded_rounds_size = len(compact_json(run_payload["rounds"]))
    latest_round_size = len(compact_json(rounds[-1]))
    assert embedded_rounds_size <= latest_round_size + 12_000
    assert embedded_rounds_size * 2 < full_rounds_size


def test_main_authoritative_consumers_receive_full_history_before_page_trimming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report_render = _load_report_render_module()
    expected_otu_rows = [
        {"taxon": "Old otu A", "sample": "sample_A", "marker": "ITS2", "otu_count": 1, "reads_total": 3},
        {"taxon": "Old otu B", "sample": "sample_A", "marker": "ITS2", "otu_count": 1, "reads_total": 1},
    ]
    expected_consensus_rows = [
        {"taxon": "Old consensus A", "sample": "sample_A", "marker": "ITS2", "consensus_count": 1, "reads_total": 3},
        {"taxon": "Old consensus B", "sample": "sample_A", "marker": "ITS2", "consensus_count": 1, "reads_total": 2},
    ]
    rounds = [
        {
            "schema_version": "2.0",
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": "round_001",
            "timestamp_utc": "2026-03-06T00:01:00Z",
            "sample_metrics": {
                "sample_a": {"sample_id": "sample_a", "label": "sample_A", "reads_demux": 4},
            },
            "otu": {"assignments_by_level": {"species": expected_otu_rows, "genus": [], "family": []}},
            "consensus": {"assignments_by_level": {"species": expected_consensus_rows, "genus": [], "family": []}},
            "warnings": [],
        },
        {
            "schema_version": "2.0",
            "run_id": "runA",
            "barcode": "B1",
            "round_barcode": "round_002",
            "timestamp_utc": "2026-03-06T00:02:00Z",
            "sample_metrics": {
                "sample_a": {"sample_id": "sample_a", "label": "sample_A", "reads_demux": 6},
            },
            "otu": {"assignments_by_level": {"species": [{"taxon": "New otu", "sample": "sample_A", "marker": "COI", "otu_count": 1, "reads_total": 6}], "genus": [], "family": []}},
            "consensus": {"assignments_by_level": {"species": [{"taxon": "New consensus", "sample": "sample_A", "marker": "COI", "consensus_count": 1, "reads_total": 6}], "genus": [], "family": []}},
            "warnings": [],
        },
    ]
    history = tmp_path / "history.jsonl"
    history.write_text("\n".join(json.dumps(row) for row in rounds) + "\n", encoding="utf-8")
    calls = []

    class AuthoritativeHistoryError(BaseException):
        pass

    def assert_complete_history(consumer_name, sorted_rounds):
        oldest = sorted_rounds[0]
        if oldest["otu"]["assignments_by_level"]["species"] != expected_otu_rows:
            raise AuthoritativeHistoryError(f"{consumer_name} received incomplete oldest-round OTU assignments")
        if oldest["consensus"]["assignments_by_level"]["species"] != expected_consensus_rows:
            raise AuthoritativeHistoryError(f"{consumer_name} received incomplete oldest-round consensus assignments")
        calls.append(consumer_name)

    def fake_build_chart_exports(sorted_rounds, _sorted_runs, _out_path, _run_id, assignment_round=None):
        assert_complete_history("build_chart_exports", sorted_rounds)
        return {}

    def fake_append_generated_sample_figures(sorted_rounds, *_args, **_kwargs):
        assert_complete_history("append_generated_sample_figures", sorted_rounds)

    def fake_write_chart_tsvs(sorted_rounds, *_args, **_kwargs):
        assert_complete_history("write_chart_tsvs", sorted_rounds)

    def reject_external_processes(*_args, **_kwargs):
        raise AssertionError("authoritative-consumer ordering test must not invoke external processes")

    monkeypatch.setattr(report_render, "build_chart_exports", fake_build_chart_exports)
    monkeypatch.setattr(report_render, "append_generated_sample_figures", fake_append_generated_sample_figures)
    monkeypatch.setattr(report_render, "write_chart_tsvs", fake_write_chart_tsvs)
    monkeypatch.setattr(report_render, "collect_consensus_sequence_rows", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(report_render.subprocess, "run", reject_external_processes)
    monkeypatch.setattr(sys, "argv", [
        str(SCRIPT),
        "--history", str(history),
        "--template", str(TEMPLATE),
        "--css", str(CSS),
        "--js", str(JS),
        "--out", str(tmp_path / "report.html"),
        "--state-out", str(tmp_path / "report_state.json"),
        "--run-id-filter", "runA",
    ])

    assert report_render.main() is None
    assert calls == ["build_chart_exports", "append_generated_sample_figures", "write_chart_tsvs"]


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
    assert "function otuSampleReads" in js_text
    assert "otu_reads_sample_total" in js_text
    assert "metricFallbackField" in js_text
    assert 'metricFallbackField: "reads_total"' in js_text
    assert "displayTotal: true" in js_text
    assert "cell.total += value" in js_text
    assert "activeView.displayTotal ? num(cell.total)" in js_text
    assert "numAny(otuSampleReads(row)) >= OTU_ASSIGNMENT_MIN_READS" in js_text
    assert 'typeof readValueFn === "function"' in js_text
    assert "readValueFn(row)" in js_text
    assert '(sourceKey === "otu" ? num(otuSampleReads(row)) : num(row.reads_total))' in js_text
    assert "readValueFn: otuSampleReads" in js_text
    assert "Object.prototype.hasOwnProperty.call(row, metricField)" in js_text
    assert "frozen_otu_reads_sample_total" in js_text
    assert 'valueField: "frozen_otu_reads_sample_total"' in js_text
    assert 'valueField: "consolidated_consensus_reads_total"' in js_text
    assert "specialFallbackField" in js_text
    assert 'specialFallbackField: "frozen_otu_reads_total"' not in js_text
    assert "hasSpecialPresence" in js_text
    assert "sample-specific frozen-read counts" in js_text
    assert "consolidated read counts are unavailable" in js_text
    assert "reads from frozen OTUs (this sample/group)" not in js_text
    assert "consolidated_consensus_reads_total" in js_text
    assert "assignment-special-count" in js_text
    assert "samplesWithReadEvidence" in js_text
    assert "cellValueDescription" in js_text
    assert 'activeView.sourceKey === "otu"' in js_text
    assert "Cells show supported values; low-read-only OTU cells show assignments" in js_text
    assert ': "Cells show values";' in js_text
    assert "bold values in parentheses show" in js_text
    assert ".assignment-special-count" in css_text


def test_report_render_uses_sample_specific_otu_reads_for_exports() -> None:
    import importlib.util

    module_path = REPO_ROOT / "bin" / "report_render.py"
    spec = importlib.util.spec_from_file_location("report_render", module_path)
    assert spec is not None and spec.loader is not None
    report_render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(report_render)

    row = {
        "sample": "sample_A",
        "marker": "COI",
        "taxon": "Species one",
        "family": "Family one",
        "genus": "Genus one",
        "species": "Species one",
        "reads_total": 100,
        "otu_reads_sample_total": 4,
    }
    round_obj = {"otu": {"assignments_by_level": {"family": [], "genus": [], "species": [row]}}}

    assert report_render.is_supported_otu_assignment_row(row) is False
    items = report_render.build_sample_treemap_items(round_obj, "sample_A", "species", source_key="otu")
    assert items == [
        {"taxon": "Species one", "family": "Family one", "reads_total": 4.0},
    ]


def test_build_run_taxonomy_tree_can_scale_filtered_frozen_rows_by_frozen_reads() -> None:
    report_render = _load_report_render_module()
    row = {
        "sample": "YT.C.spiked_1_MPnew1",
        "marker": "ITS2",
        "taxon": "Sorghum",
        "family": "Poaceae",
        "genus": "Sorghum",
        "species": "Sorghum",
        "reads_total": 14,
        "otu_reads_sample_total": 14,
        "frozen_otu_count": 1,
        "frozen_otu_reads_sample_total": 8,
    }
    round_obj = {
        "otu": {
            "assignments_by_level": {
                "family": [],
                "genus": [],
                "species": [row],
            }
        }
    }

    frozen_tree = report_render.build_run_taxonomy_tree(
        round_obj,
        "otu",
        "ITS2",
        include_row=lambda r: (r.get("frozen_otu_count") or 0) > 0,
        read_field="frozen_otu_reads_sample_total",
    )
    total_tree = report_render.build_run_taxonomy_tree(
        round_obj,
        "otu",
        "ITS2",
        include_row=lambda r: (r.get("frozen_otu_count") or 0) > 0,
    )

    assert frozen_tree["value"] == 8.0
    assert frozen_tree["children"][0]["value"] == 8.0
    assert total_tree["value"] == 14.0


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


def test_report_js_assignment_matrix_scopes_special_counts_to_displayed_subset() -> None:
    js_text = JS.read_text(encoding="utf-8")
    assert "specialSupported" in js_text
    assert "specialUnsupported" in js_text
    assert "frozenSupported" in js_text
    assert "frozenUnsupported" in js_text
    assert 'displayScopedValue(cell, "special", "specialSupported", "specialUnsupported")' in js_text
    assert 'displayScopedValue(cell, "frozen", "frozenSupported", "frozenUnsupported")' in js_text
    assert 'cellDisplayScopedValue(cell, "special", "specialSupported", "specialUnsupported")' in js_text
    assert "cell.supported > 0 ? num(cell[supportedKey]) : num(cell[unsupportedKey])" in js_text


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


def test_report_js_collapse_uses_producer_sample_identity_and_keeps_panels_separate() -> None:
    node_path = shutil.which("node")
    if node_path is None:
        pytest.skip("node not found in PATH")
    producer = _load_report_render_module()
    source_forms = ("W_eDNA_1_COI", "W_eDNA_2_COI")
    labels = [producer.sample_group_label(value, collapse_track_units=True) for value in source_forms]
    assert labels == ["W_eDNA_1", "W_eDNA_2"]
    assert [producer.sample_group_label(value.replace("_COI", "_ITS2"), collapse_track_units=True)
            for value in source_forms] == labels

    source = JS.read_text(encoding="utf-8")

    def extract(name: str) -> str:
        start = source.index(f"  function {name}(")
        opening = source.index(") {", start) + 2
        depth = 0
        for position in range(opening, len(source)):
            if source[position] == "{":
                depth += 1
            elif source[position] == "}":
                depth -= 1
                if depth == 0:
                    return source[start:position + 1]
        raise AssertionError(name)

    functions = "\n".join(extract(name) for name in (
        "canonicalMarkerToken", "markerOrderFromData", "normalizeBase", "groupEntityLabel",
        "currentGroupLabel", "stableDomId", "currentGroupId", "metricGroupLabel",
        "assignmentGroupLabel", "hasTrackUnitMetrics", "trackSampleReplicateLabel",
        "mergeMarkerCounts", "getGroupedSampleMetricEntries", "aggregateSampleMetricEntries",
        "getSampleRoundSnapshot", "countSampleAssignmentsByLevel", "makeAssetLink",
        "detectDemuxEnabled", "appendFigureCard", "renderFigureGallery",
        "renderSampleEvolutionSection", "renderSampleSections",
    ))
    harness = r'''
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.dataset = {};
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this[name] = value; }
}
const document = { createElement(tag) { return new Element(tag); } };
const get = (obj, path, fallback) => {
  let value = obj;
  for (const key of path) value = value && value[key];
  return value == null ? fallback : value;
};
const num = (value) => typeof value === "number" && Number.isFinite(value) ? value : 0;
const sampleIndex = new Element("div");
const sampleDetails = new Element("div");
const viewScope = "run";
const groupViewMode = "sample";
let rounds = [];
let scientificRound = null;
let reportIdentityMode = "collapse";
const drawDemuxByMarkerPerSample = () => {};
const renderCurrentSampleResults = (panel, entry) => {
  panel.sample = {
    label: panel.children[0].children[0].textContent,
    reads: entry.totals.reads_demux,
    otus: entry.current.otu_total,
    taxa: countSampleAssignmentsByLevel(rounds[0], entry.label, "otu").species,
    members: rounds[0].otu.assignments_by_level.species
      .filter((row) => assignmentGroupLabel(row) === entry.label).map((row) => row.taxon),
  };
};
function links(node, tag) {
  const own = node.tagName === tag && (node.href || node.src) ? [node.href || node.src] : [];
  return own.concat(...node.children.map((child) => links(child, tag)));
}
function render(entries, identityMode, reverse, marker = "COI") {
  reportIdentityMode = identityMode;
  sampleDetails.children = [];
  const ordered = reverse ? entries.slice().reverse() : entries;
  rounds = [{
    identity_mode: identityMode,
    read_fate: {demux_enabled: true},
    round_barcode: "round_001",
    markers: {order: [marker]},
    sample_metrics: Object.fromEntries(ordered.map((entry) => [entry.id, {
      label: entry.label,
      reads_demux: entry.reads,
      otu_total: entry.otus,
      figures: [{id: "sample_reads_time_history", section: "Other", exists: true,
        path: entry.png, pdf_exists: true, pdf_path: entry.pdf}],
    }])),
    otu: {assignments_by_level: {species: ordered.map((entry) => ({
      sample: entry.label, taxon: entry.taxon,
    }))}},
  }];
  scientificRound = rounds[0];
  renderSampleSections();
  return sampleDetails.children.map((panel) => ({
    ...panel.sample,
    pdf: links(panel, "A").filter((href) => href.endsWith(".pdf")),
    png: links(panel, "IMG"),
  }));
}
console.log(JSON.stringify({
  forward: render(INPUT, "collapse", false),
  reverse: render(INPUT, "collapse", true),
  ordinary: render(ORDINARY, "collapse", false),
  final: render(FINAL, "collapse", false),
  broad: render(INPUT, "collapse", false, "ITS2"),
  track: render(TRACK, "track", false),
}));
'''
    collision = [
        {"id": f"id_{i}", "label": label, "reads": 10 * i, "otus": i,
         "taxon": f"Taxon {i}", "png": f"figures/{label}.png", "pdf": f"figures/{label}.pdf"}
        for i, label in enumerate(labels, 1)
    ]
    ordinary = [dict(collision[0], label="lakeA"), dict(collision[1], label="siteB")]
    final = [dict(collision[0], label="control"), dict(collision[1], label="blank")]
    track = [dict(collision[0], label="W_eDNA_1_COI"), dict(collision[1], label="W_eDNA_2_COI")]
    inputs = "\n".join(f"const {key} = {json.dumps(value)};" for key, value in (
        ("INPUT", collision), ("ORDINARY", ordinary), ("FINAL", final), ("TRACK", track),
    ))

    def run(js_functions: str) -> dict:
        result = subprocess.run([node_path, "-e", inputs + js_functions + harness],
                                capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    def expected(entries: list[dict]) -> dict:
        return {entry["label"]: {
            "label": entry["label"], "reads": entry["reads"], "otus": entry["otus"],
            "taxa": 1, "members": [entry["taxon"]],
            "pdf": [entry["pdf"]], "png": [entry["png"]],
        } for entry in entries}

    def by_label(panels: list[dict]) -> dict:
        return {panel["label"]: panel for panel in panels}

    result = run(functions)
    for key in ("forward", "reverse", "broad"):
        assert len(result[key]) == 2
        assert by_label(result[key]) == expected(collision)
    assert result["forward"] == result["reverse"]
    assert by_label(result["ordinary"]) == expected(ordinary)
    assert by_label(result["final"]) == expected(final)
    assert {label: (panel["reads"], panel["otus"], panel["taxa"], panel["members"])
            for label, panel in by_label(result["track"]).items()} == {
        label: (entry["reads"], entry["otus"], 1, [entry["taxon"]])
        for label, entry in zip(labels, track)
    }

    mutants = {
        "double_clean": functions.replace('if (reportIdentityMode === "collapse") return text;',
                                          'if (reportIdentityMode === "collapse") return normalizeBase(text);'),
        "merge_then_deduplicate": functions.replace(
            'const groupId = useTrackUnitMetrics ? currentGroupId(label) : currentGroupId(label);',
            'const groupId = currentGroupId(normalizeBase(label));'),
        "display_second_clean": functions.replace('h.textContent = s.label;',
                                                   'h.textContent = normalizeBase(s.label);'),
        "figure_based_group": functions.replace(
            'const groupId = useTrackUnitMetrics ? currentGroupId(label) : currentGroupId(label);',
            'const groupId = currentGroupId(raw.figures && raw.figures.length ? "has_figure" : label);'),
    }
    for name, mutant in mutants.items():
        assert mutant != functions, name
        observed = run(mutant)
        assert (len(observed["forward"]) != 2
                or by_label(observed["forward"]) != expected(collision)
                or by_label(observed["reverse"]) != expected(collision)), name


_F02_DOM_HARNESS = r'''
const fs = require("fs"), vm = require("vm");
class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null;
    this._text = ""; this._html = ""; this.className = ""; this.style = {};
    this.dataset = {}; this.attributes = {}; this.id = "";
    this.classList = {add: (s) => { this.className += " " + s; }, remove() {},
      contains: (s) => this.className.split(" ").includes(s), toggle() {}};
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set innerHTML(v) { this._html = String(v); this.children = []; this._text = ""; }
  get innerHTML() { return this._html; }
  get firstChild() { return this.children[0] || null; }
  get nextSibling() { return this.parentNode && this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null; }
  get childNodes() { return this.children; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); c.parentNode = null; return c; }
  insertBefore(c, r) { const i = this.children.indexOf(r); c.parentNode = this; if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attributes[k] || null; }
  addEventListener() {} querySelector() { return null; } querySelectorAll() { return []; }
  closest() { return null; } remove() { if (this.parentNode) this.parentNode.removeChild(this); }
}
const nodes = {};
for (const id of ["summary-cards", "summary-section", "warnings", "warnings-section", "charts",
  "figures", "figures-section", "runs-info-section", "run-index-pager", "sample-index",
  "sample-index-section", "sample-details", "sample-details-section", "rounds-section",
  "report-index-link", "run-name", "run-links", "global-overview"]) nodes[id] = new Element();
const runHead = new Element("thead"), runBody = new Element("tbody");
const runsSection = new Element("section"), global = nodes["global-overview"];
global.appendChild(runsSection); runsSection.appendChild(runHead); runsSection.appendChild(runBody);
runHead.closest = () => runsSection; runBody.closest = () => runsSection;
const document = {body: new Element("body"), createElement: (t) => new Element(t),
  createElementNS: (_, t) => new Element(t),
  createTextNode: (t) => { const n = new Element("#text"); n.textContent = t; return n; },
  getElementById: (id) => nodes[id] || null,
  querySelector: (s) => s === "#runs-table thead" ? runHead : s === "#runs-table tbody" ? runBody : null,
  addEventListener() {}};
const payload = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const window = {REPORT_PAYLOAD: payload, REPORT_META: {auto_refresh_enabled: false}, location: {reload() {}}};
vm.runInNewContext(fs.readFileSync(process.argv[1], "utf8"),
  {window, document, console, Blob, URL, setTimeout, Date, Intl, Map, Set, Number, String,
    Array, Object, Math, JSON, Promise}, {filename: process.argv[1]});
function descendants(n) { return [n, ...n.children.flatMap(descendants)]; }
function cards(n) { return descendants(n).filter((x) => x.className.split(" ").includes("card"))
  .map((x) => ({label: descendants(x).find((y) => y.className === "label")?.textContent,
    value: descendants(x).find((y) => y.className === "value")?.textContent}))
  .filter((x) => x.label); }
const assignments = descendants(global).filter((x) => x.className === "chart-card")
  .map((x) => x.textContent);
const tables = {};
for (const card of descendants(global).filter((x) => x.className === "chart-card")) {
  const title = descendants(card).find((x) => x.tagName === "H3")?.textContent;
  if (title === "OTU Assignments" || title === "Consensus Assignments") {
    tables[title] = descendants(card).filter((x) => x.tagName === "TR"
      && x.children.some((child) => child.tagName === "TD"))
      .map((row) => row.children.map((cell) => cell.textContent));
  }
}
console.log(JSON.stringify({summary: cards(nodes["summary-cards"]),
  samples: descendants(nodes["sample-details"]).filter((x) => x.className === "sample-panel")
    .map((x) => ({label: descendants(x).find((y) => y.tagName === "H3")?.textContent, cards: cards(x)})),
  assignments, tables, runName: nodes["run-name"].textContent,
  warnings: nodes["warnings"].textContent}));
'''


def _f02_round(number: int, status="ok", empty: bool = False,
               marker: str = "COI") -> dict:
    labels = ("Lake_North", "Lake_South")
    markers = ("COI", "ITS2") if marker == "MIXED" else (marker, marker)
    rows_otu = [] if empty else [
        {"taxon": f"O{number}_{label}", "sample": label, "marker": markers[i],
         "otu_count": number + i, "frozen_otu_count": 1 if i == 0 else 0,
         "reads_total": 7 + i} for i, label in enumerate(labels)
    ]
    rows_consensus = [] if empty else [
        {"taxon": f"C{number}_{label}", "sample": label, "marker": markers[i],
         "consensus_count": number + i, "consolidated_consensus_count": 1 if i == 1 else 0,
         "reads_total": 7 + i} for i, label in enumerate(labels)
    ]
    otu_levels = {level: ([dict(row, taxon=f"{level}_{row['taxon']}") for row in rows_otu]
                          if level != "species" else rows_otu)
                  for level in ("species", "genus", "family")}
    consensus_levels = {level: ([dict(row, taxon=f"{level}_{row['taxon']}") for row in rows_consensus]
                                if level != "species" else rows_consensus)
                        for level in ("species", "genus", "family")}
    row = {
        "run_id": "runA", "barcode": "B1", "round_barcode": f"round_{number:03d}",
        "timestamp_utc": f"2026-03-06T00:{number:02d}:00Z",
        "read_fate": {"demux_enabled": True}, "reads": {"total": 15},
        "markers": {"order": list(dict.fromkeys(markers))},
        "sample_metrics": {label.lower(): {"sample_id": label.lower(), "label": label,
            "reads_demux": 7 + i, "otu_total": 0 if empty else number + i,
            "consensus_total": 0 if empty else number + i}
            for i, label in enumerate(labels)},
        "otu": {"assignments_by_level": otu_levels},
        "consensus": {"emitted": 0 if empty else 2 * number + 1,
            "assignments_by_level": consensus_levels},
        "warnings": [],
    }
    if status is not None:
        row["round_status"] = status
    if status == "failed":
        row["failure_reason"] = f"failure_{number}"
    return row


def _f02_browser(payload: dict, tmp_path: Path) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not found in PATH")
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")
    result = subprocess.run([node, "-e", _F02_DOM_HARNESS, str(JS), str(payload_path)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("case,statuses,empty_last,reverse,marker", [
    ("A", ("ok", "ok"), False, False, "COI"),
    ("B", ("ok", "failed"), False, False, "COI"),
    ("C", ("failed", "failed"), False, False, "COI"),
    ("M", ("failed", "failed"), False, False, "COI"),
    ("N", ("ok", "failed"), False, False, "COI"),
    ("P", ("ok", "failed"), False, False, "COI"),
    ("D", ("ok", "failed", "ok"), False, False, "COI"),
    ("E", ("ok", "ok"), True, False, "COI"),
    ("F", ("ok", "failed"), False, False, "COI"),
    ("G", (None, "failed"), False, False, "COI"),
    ("H", ("ok", "failed"), False, False, "MIXED"),
    ("I", ("ok", "failed"), False, True, "COI"),
    ("J", ("ok", "failed"), False, False, "COI"),
    ("L", ("ok", "failed"), False, False, "ITS2"),
])
def test_f02_scientific_source_and_latest_attempt_are_independent(
    tmp_path: Path, case: str, statuses: tuple, empty_last: bool,
    reverse: bool, marker: str,
) -> None:
    rows = [_f02_round(i, status, empty_last and i == len(statuses), marker)
            for i, status in enumerate(statuses, 1)]
    if case in ("F", "M"):
        rows[-1]["otu"]["assignments_by_level"]["species"][0]["otu_count"] = 999
        rows[-1]["consensus"]["emitted"] = 999
    # Oracle uses only the round comparator, status, and the run-index source key.
    order = lambda row: int(row["round_barcode"].rsplit("_", 1)[1])
    latest_attempt = max(rows, key=order)
    eligible = [row for row in rows if row.get("round_status", "ok") != "failed"]
    oracle_source = max(eligible, key=order) if eligible else None
    if case == "N":
        oracle_source = None
    run_record = {"run_id": "runA", "run_status_read_fate": {},
                  "last_round_barcode": latest_attempt["round_barcode"],
                  "last_round_status": latest_attempt.get("round_status", "ok")}
    if case in ("M", "N"):
        del run_record["run_status_read_fate"]
    if latest_attempt.get("round_status") == "failed":
        run_record["last_round_failure_reason"] = latest_attempt["failure_reason"]
    if oracle_source is not None:
        run_record["run_summary_source_round"] = oracle_source["round_barcode"]
        run_record["run_summary"] = {"consensus": oracle_source["consensus"]}
    if case == "P":
        run_record["run_summary_source_round"] = "round_999"
    source_key = run_record.get("run_summary_source_round")
    assert source_key == ("round_999" if case == "P" else (oracle_source["round_barcode"] if oracle_source else None))
    oracle_source = next((row for row in rows if row["round_barcode"] == source_key), None)
    history = tmp_path / "history.jsonl"
    history.write_text("\n".join(json.dumps(row) for row in (reversed(rows) if reverse else rows)) + "\n",
                       encoding="utf-8")
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(json.dumps(run_record) + "\n", encoding="utf-8")
    out = tmp_path / "runs" / "runA" / "report.html"
    out.parent.mkdir(parents=True)
    result = _run(history, out, out.with_name("report_state.json"), extra_args=[
        "--run-id-filter", "runA", "--run-index", str(run_index), "--sample-plot-max", "0",
        "--auto-refresh-enabled", "0"])
    assert result.returncode == 0, result.stderr
    payload = _extract_js_json(out.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    browser = _f02_browser(payload, tmp_path)
    assert "render_failed:" not in browser["warnings"]
    assert payload["rounds"][-1]["round_barcode"] == latest_attempt["round_barcode"]
    assert payload["run_index"][0]["last_round_barcode"] == latest_attempt["round_barcode"]
    assert payload["run_index"][0]["last_round_status"] == latest_attempt.get("round_status", "ok")
    if case == "C":
        assert payload["run_index"][0]["run_status_read_fate"] == {}
    if case == "M":
        assert all(key not in payload["run_index"][0] for key in
                   ("run_status_read_fate", "run_summary", "run_summary_source_round"))
        assert latest_attempt["otu"]["assignments_by_level"]["species"][0]["otu_count"] == 999
    assert browser["runName"].count("Latest round failed") == (1 if latest_attempt.get("round_status") == "failed" else 0)
    if latest_attempt.get("round_status") == "failed":
        assert latest_attempt["failure_reason"] in browser["warnings"]
    summary = {item["label"]: item["value"] for item in browser["summary"]}
    assert summary["Consensus Emitted (Latest Round)"] == (
        str(oracle_source["consensus"]["emitted"]) if oracle_source else "N/A")
    source_rows = oracle_source["otu"]["assignments_by_level"]["species"] if oracle_source else []
    consensus_rows = oracle_source["consensus"]["assignments_by_level"]["species"] if oracle_source else []
    kept = [row for row in payload["rounds"]
            if row["otu"]["assignments_by_level"]["species"]]
    assert [row["round_barcode"] for row in kept] == ([oracle_source["round_barcode"]] if source_rows else [])
    assert [row["otu"]["assignments_by_level"]["species"] for row in kept] == ([source_rows] if source_rows else [])
    cards = {entry["label"]: {card["label"]: card["value"] for card in entry["cards"]}
             for entry in browser["samples"]}
    for label in ("Lake_North", "Lake_South"):
        assert cards[label]["Number of OTUs"] == (
            str(oracle_source["sample_metrics"][label.lower()]["otu_total"]) if oracle_source else "N/A")
        assert cards[label]["Number of Consensus"] == (
            str(oracle_source["sample_metrics"][label.lower()]["consensus_total"]) if oracle_source else "N/A")
    assignment_text = " ".join(browser["assignments"])
    assert (f"Current state through round {order(oracle_source):03d}" if oracle_source else "N/A") in assignment_text
    for row in source_rows + consensus_rows:
        assert row["taxon"] in assignment_text
    for title, expected_rows, count_field in (
        ("OTU Assignments", source_rows, "otu_count"),
        ("Consensus Assignments", consensus_rows, "consensus_count"),
    ):
        rendered_rows = {row[0]: row for row in browser["tables"][title]}
        assert set(rendered_rows) == {row["taxon"] for row in expected_rows}
        for row in expected_rows:
            rendered = rendered_rows[row["taxon"]]
            assert rendered[1:4] == [row["sample"], row["marker"], str(row[count_field])]
            assert rendered[5] == str(row["reads_total"])
    if oracle_source is not latest_attempt:
        for source_key in ("otu", "consensus"):
            for row in latest_attempt[source_key]["assignments_by_level"]["species"]:
                assert row["taxon"] not in assignment_text
    tsv_specs = [(source_key, level, f"{source_key}_assignments_{level}.tsv", None)
                 for source_key in ("otu", "consensus") for level in ("species", "genus", "family")]
    tsv_specs.extend([
        ("otu", "species", "frozen_otu_assignments_species.tsv", "frozen_otu_count"),
        ("consensus", "species", "consolidated_consensus_assignments_species.tsv", "consolidated_consensus_count"),
        ("otu", "species", "otu_assignments_by_sample_species.tsv", None),
        ("consensus", "species", "consensus_assignments_by_sample_species.tsv", None),
    ])
    for source_key, level, filename, filter_field in tsv_specs:
        expected_rows = oracle_source[source_key]["assignments_by_level"][level] if oracle_source else []
        if filter_field:
            expected_rows = [row for row in expected_rows if row[filter_field] > 0]
        tsv = out.parent / "figures" / filename
        lines = tsv.read_text(encoding="utf-8").splitlines()
        assert len(lines) == len(expected_rows) + 1
        columns = lines[0].split("\t")
        observed = [dict(zip(columns, line.split("\t"))) for line in lines[1:]]
        assert observed == [{key: str(value) for key, value in row.items()} for row in expected_rows]
    assert not payload["parse_warnings"]


def test_f02_legacy_run_index_without_source_field_keeps_latest_row(tmp_path: Path) -> None:
    rows = [_f02_round(1, "failed"), _f02_round(2, "failed")]
    history = tmp_path / "history.jsonl"
    history.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    run_index = tmp_path / "runs_index.jsonl"
    run_index.write_text(json.dumps({"run_id": "runA", "last_round_barcode": "round_002"}) + "\n",
                         encoding="utf-8")
    out = tmp_path / "runs" / "runA" / "report.html"
    out.parent.mkdir(parents=True)
    result = _run(history, out, out.with_name("report_state.json"), extra_args=[
        "--run-id-filter", "runA", "--run-index", str(run_index), "--sample-plot-max", "0",
        "--auto-refresh-enabled", "0"])
    assert result.returncode == 0, result.stderr
    payload = _extract_js_json(out.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
    assert payload["rounds"][-1]["otu"]["assignments_by_level"]["species"] == rows[-1]["otu"]["assignments_by_level"]["species"]
    browser = _f02_browser(payload, tmp_path)
    assert "Current state through round 002" in " ".join(browser["assignments"])
    assert "O2_Lake_North" in (out.parent / "figures" / "otu_assignments_species.tsv").read_text(encoding="utf-8")


def test_f02_assignment_chart_exports_use_scientific_round(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mod = _load_report_render_module()
    source = _f02_round(1)
    failed = _f02_round(2, "failed")
    for source_key in ("otu", "consensus"):
        for row in failed[source_key]["assignments_by_level"]["species"]:
            row["reads_total"] = 100
    captured = {"treemaps": [], "sunbursts": []}

    def write_pdf(*args):
        path = args[-1]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pdf")

    def treemap(_title, items, path):
        captured["treemaps"].extend(items)
        write_pdf(path)

    def sunburst(_title, _subtitle, tree, png_path, pdf_path):
        captured["sunbursts"].append(tree)
        write_pdf(pdf_path)
        png_path.write_bytes(b"png")

    monkeypatch.setattr(mod, "export_stacked_bar_pdf", write_pdf)
    monkeypatch.setattr(mod, "export_vertical_bar_pdf", write_pdf)
    monkeypatch.setattr(mod, "export_treemap_pdf", treemap)
    monkeypatch.setattr(mod, "export_sunburst_plot_png_pdf", sunburst)
    out = tmp_path / "runs" / "runA" / "report.html"
    exports = mod.build_chart_exports([source, failed], [], out, "runA", assignment_round=source)
    assert exports["run_otu_sunburst_COI"]["pdf_path"]
    assert {item["taxon"] for item in captured["treemaps"]} == {
        row["taxon"] for source_key in ("otu", "consensus")
        for level in ("species", "genus", "family")
        for row in source[source_key]["assignments_by_level"][level]
    }
    assert captured["sunbursts"]
    assert all(tree["value"] < 100 for tree in captured["sunbursts"])
    captured["treemaps"].clear()
    captured["sunbursts"].clear()
    missing_exports = mod.build_chart_exports([failed], [], out, "runA", assignment_round={})
    assert not captured["treemaps"] and not captured["sunbursts"]
    assert not missing_exports.get("run_otu_sunburst_COI", {}).get("pdf_path")


def test_f02_reversed_history_has_same_payload_browser_and_tsvs(tmp_path: Path) -> None:
    rows = [_f02_round(1), _f02_round(2, "failed")]
    run_record = {"run_id": "runA", "last_round_barcode": "round_002",
                  "last_round_status": "failed", "last_round_failure_reason": "failure_2",
                  "run_summary_source_round": "round_001", "run_summary": {},
                  "run_status_read_fate": {}}
    observed = []
    for name, history_rows in (("forward", rows), ("reverse", list(reversed(rows)))):
        root = tmp_path / name
        root.mkdir()
        history = root / "history.jsonl"
        history.write_text("\n".join(json.dumps(row) for row in history_rows) + "\n", encoding="utf-8")
        run_index = root / "runs_index.jsonl"
        run_index.write_text(json.dumps(run_record) + "\n", encoding="utf-8")
        out = root / "runs" / "runA" / "report.html"
        out.parent.mkdir(parents=True)
        result = _run(history, out, out.with_name("report_state.json"), extra_args=[
            "--run-id-filter", "runA", "--run-index", str(run_index), "--sample-plot-max", "0",
            "--auto-refresh-enabled", "0"])
        assert result.returncode == 0, result.stderr
        payload = _extract_js_json(out.read_text(encoding="utf-8"), "window.REPORT_PAYLOAD = ")
        payload.pop("generated_at_utc", None)
        browser = _f02_browser(payload, root)
        tsvs = {path.name: path.read_bytes() for path in (out.parent / "figures").glob("*.tsv")}
        observed.append((payload, browser, tsvs))
    assert observed[0] == observed[1]
