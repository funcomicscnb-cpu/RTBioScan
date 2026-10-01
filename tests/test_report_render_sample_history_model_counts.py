"""F1 row-count oracles. No basecaller execution ledger is used as count truth."""
import copy
import csv
import datetime as dt
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("RTB_F1_RENDER_SCRIPT", ROOT / "bin/report_render.py"))
KINDS = {
    "reads_time_history": "history_reads",
    "reads_cumulative_history": "history_cumulative",
    "otu_tax_time_history": "history_otu_tax",
    "consensus_tax_time_history": "history_consensus_tax",
}


@pytest.fixture
def renderer():
    spec = importlib.util.spec_from_file_location("f1_renderer", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def table(root, rows, state="stateA", round_name="r1", barcode="poolA", compressed=False):
    path = root / "temp/ongoing/state" / state / round_name / f"{barcode}_demult_rpt.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "read_id\tsample\tbasecalling_model\n" + "".join(
        f"read{i}\t{sample}\t{model}\n" for i, (sample, model) in enumerate(rows)
    )
    if compressed:
        path = path.with_suffix(".txt.gz")
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            stream.write(text)
    else:
        path.write_text(text, encoding="utf-8")
    return path


def oracle(path, attributed_labels):
    """Independent TSV reader with fixture-owned identity attribution, not renderer parsing."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    return {model: sum(row["sample"] in attributed_labels and row["basecalling_model"].strip().lower() == model
                       for row in rows) for model in ("hac", "sup")}


def round_obj(name="r1", state="stateA", barcode="poolA", mode="collapse", index=1):
    return {"run_id": "runA", "state_id": state, "barcode": barcode, "round_barcode": name,
            "identity_mode": mode, "timestamp_utc": f"2026-01-01T00:{index:02d}:00Z",
            "sample_metrics": {"s1": {"sample_id": "s1", "label": "S1", "reads_demux": 2, "figures": []}},
            "otu": {"assignments_by_level": {}}, "consensus": {"assignments_by_level": {}}}


def out_path(root):
    return root / "report_html/runs/runA/report.html"


def calculate(mod, root, **kwargs):
    return mod.collect_round_sample_model_counts("runA", "S1", "r1", out_path(root),
                                                 state_id="stateA", barcode="poolA", **kwargs)


def counts(rows):
    return [(r["hac_reads_round"], r["sup_reads_round"], r["hac_reads_cumulative"], r["sup_reads_cumulative"])
            for r in rows]


def test_standard_layout_and_metadata_fallback(renderer, tmp_path):
    """A-F1-04: absent state/barcode metadata retains run_id/RTBioScan fallback."""
    p = table(tmp_path, [("S1_COI", "hac")] * 2, state="runA", barcode="RTBioScan")
    expected = oracle(p, {"S1_COI"})
    assert expected == {"hac": 2, "sup": 0}
    assert renderer.collect_round_sample_model_counts("runA", "S1", "r1", out_path(tmp_path)) == expected


def test_state_and_pool_identity(renderer, tmp_path):
    """A-F1-08/09: decoy run and standard-pool tables must not be selected."""
    p = table(tmp_path, [("S1_COI", "hac")] * 2)
    table(tmp_path, [("S1_COI", "sup")] * 5, state="runA")
    table(tmp_path, [("S1_COI", "sup")] * 7, barcode="RTBioScan")
    assert calculate(renderer, tmp_path) == oracle(p, {"S1_COI"}) == {"hac": 2, "sup": 0}


def test_multiround_cumulative_and_repeat(renderer, tmp_path):
    """A-F1-05/06/14/22: collapse identity, accumulation, and each round's pool."""
    p1 = table(tmp_path, [("S1_COI", "hac"), ("S1_2", "hac"), ("S1_3", "hac"),
                          ("S1_1", "sup"), ("S10_COI", "hac"), ("S1_COI", "fast")])
    p2 = table(tmp_path, [("S1_COI", "hac"), ("S1_2", "sup"), ("S1_3", "sup")],
               round_name="r2", barcode="poolB")
    table(tmp_path, [("S1_COI", "sup")] * 9, round_name="r2")
    rounds = [round_obj(), round_obj("r2", barcode="poolB", index=2)]
    expected = [oracle(p1, {"S1_COI", "S1_2", "S1_3", "S1_1"}),
                oracle(p2, {"S1_COI", "S1_2", "S1_3"})]
    assert expected == [{"hac": 3, "sup": 1}, {"hac": 1, "sup": 2}]
    rows = renderer.build_sample_history_rows(rounds, "s1", "S1", out_path(tmp_path), "runA")
    assert counts(rows) == [(3, 1, 3, 1), (1, 2, 4, 3)]
    assert renderer.build_sample_history_rows(rounds, "s1", "S1", out_path(tmp_path), "runA") == rows


def test_track_matching_is_exact(renderer, tmp_path):
    """A-F1-07: S10 and a different sample never enter S1's track count."""
    labels = {"S1_1_P_COI", "S1_2_Q_ITS2"}
    p = table(tmp_path, [("S1_1_P_COI", "hac"), ("S1_2_Q_ITS2", "sup"),
                         ("S10_1_P_COI", "hac"), ("S10_1_P_COI", "sup"), ("X_1_P_COI", "hac")])
    assert calculate(renderer, tmp_path, identity_mode="track") == oracle(p, labels) == {"hac": 1, "sup": 1}
    rows = renderer.build_sample_history_rows([round_obj(mode="track")], "s1", "S1", out_path(tmp_path), "runA")
    assert counts(rows) == [(1, 1, 1, 1)]
    assert calculate(renderer, tmp_path, identity_mode="collapse") == {"hac": 0, "sup": 0}


def test_nearest_report_html(renderer, tmp_path):
    """A-F1-10: the outer report_html is a deliberate nonzero decoy."""
    root = tmp_path / "report_html/archive/results"
    p = table(root, [("S1_COI", "hac")] * 2)
    table(tmp_path, [("S1_COI", "sup")] * 3)
    assert calculate(renderer, root) == oracle(p, {"S1_COI"}) == {"hac": 2, "sup": 0}


def test_legacy_synthetic_root(renderer, tmp_path):
    p = table(tmp_path, [("S1_COI", "hac")])
    synthetic = tmp_path / "runs/runA/report.html"
    assert renderer.collect_round_sample_model_counts("runA", "S1", "r1", synthetic,
                                                       state_id="stateA", barcode="poolA") == oracle(p, {"S1_COI"})


def test_gzip_fallback(renderer, tmp_path):
    """A-F1-11."""
    p = table(tmp_path, [("S1_COI", "hac"), ("S1_COI", "sup")], compressed=True)
    assert calculate(renderer, tmp_path) == oracle(p, {"S1_COI"}) == {"hac": 1, "sup": 1}


def test_zero_absent_missing_and_restored_rounds(renderer, tmp_path):
    """A-F1-03/12: no historical table means zero, even with nonzero summary metrics."""
    assert calculate(renderer, tmp_path) == {"hac": 0, "sup": 0}
    table(tmp_path, [("S10_COI", "hac"), ("S1_COI", "fast")])
    assert calculate(renderer, tmp_path) == {"hac": 0, "sup": 0}
    rounds = [round_obj(), round_obj("missing", index=2)]
    assert counts(renderer.build_sample_history_rows(rounds, "s1", "S1", out_path(tmp_path), "runA")) == [(0, 0, 0, 0)] * 2


@pytest.mark.parametrize("change", ["size", "mtime"])
def test_same_process_rewrite_invalidation(renderer, tmp_path, change):
    """A-F1-13/23: exercise size and nanosecond-mtime independently, without sleeps."""
    p = table(tmp_path, [("S1_COI", "hac")])
    rounds = [round_obj()]
    before = renderer.build_sample_history_rows(rounds, "s1", "S1", out_path(tmp_path), "runA")
    old = p.stat()
    if change == "size":
        table(tmp_path, [("S1_COI", "sup")] * 2)
        os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns))
        assert p.stat().st_size != old.st_size
        expected = [(0, 2, 0, 2)]
    else:
        # Same length content plus an explicit two-second mtime advance is coarse-FS safe.
        p.write_text(p.read_text().replace("hac", "sup"), encoding="utf-8")
        os.utime(p, ns=(old.st_atime_ns, old.st_mtime_ns + 2_000_000_000))
        assert p.stat().st_size == old.st_size and p.stat().st_mtime_ns != old.st_mtime_ns
        expected = [(0, 1, 0, 1)]
    after = renderer.build_sample_history_rows(rounds, "s1", "S1", out_path(tmp_path), "runA")
    assert counts(before) == [(1, 0, 1, 0)]
    assert {"hac": expected[0][0], "sup": expected[0][1]} == oracle(p, {"S1_COI"})
    assert counts(after) == expected
    assert renderer.build_sample_history_rows(rounds, "s1", "S1", out_path(tmp_path), "runA") == after


def test_parse_once_per_table_and_mode_and_return_copy(renderer, tmp_path, monkeypatch):
    p = table(tmp_path, [("S1_1_P_COI", "hac"), ("S10_1_P_COI", "sup")])
    original = open
    opened = []

    def counted(path, *args, **kwargs):
        if Path(path) == p:
            opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(renderer, "open", counted, raising=False)
    result = calculate(renderer, tmp_path, identity_mode="track")
    assert result == oracle(p, {"S1_1_P_COI"})
    result["hac"] = 99
    assert calculate(renderer, tmp_path, identity_mode="track")["hac"] == 1
    renderer.collect_round_sample_model_counts("runA", "S10", "r1", out_path(tmp_path),
                                               identity_mode="track", state_id="stateA", barcode="poolA")
    assert len(opened) == 1
    assert calculate(renderer, tmp_path, identity_mode="collapse") == {"hac": 0, "sup": 0}
    assert len(opened) == 2


@pytest.mark.parametrize("failure", ["open", "read", "parse"])
def test_partial_exceptions_are_not_memoized(renderer, tmp_path, monkeypatch, failure):
    p = table(tmp_path, [("S1_COI", "hac"), ("S1_COI", "sup")])
    wire = p.read_text()
    class BrokenStream(io.StringIO):
        def __next__(self):
            if self.tell() >= len(wire.splitlines(keepends=True)[0] + wire.splitlines(keepends=True)[1]):
                raise OSError("injected read failure")
            return super().__next__()

    def broken(*args, **kwargs):
        if failure == "open":
            raise OSError("injected open failure")
        return BrokenStream(wire)

    if failure == "parse":
        normal = renderer.normalize_sample_base

        def bad_label(value):
            if value == "bad_COI":
                raise ValueError("injected parse failure")
            return normal(value)

        # File identity stays fixed across repair of the parser, proving failures aren't cached.
        p.write_text(wire.replace("read1\tS1_COI", "read1\tbad_COI"), encoding="utf-8")
        monkeypatch.setattr(renderer, "normalize_sample_base", bad_label)
    else:
        monkeypatch.setattr(renderer, "open", broken, raising=False)
    expected_partial = {"hac": 0 if failure == "open" else 1, "sup": 0}
    assert calculate(renderer, tmp_path) == expected_partial
    monkeypatch.undo()
    assert calculate(renderer, tmp_path) == oracle(p, {"S1_COI"})


def test_missing_then_created_and_malformed_then_repaired(renderer, tmp_path):
    assert calculate(renderer, tmp_path) == {"hac": 0, "sup": 0}
    p = table(tmp_path, [("S1_COI", "hac")])
    p.write_text("sample\twrong_column\nS1_COI\thac\n", encoding="utf-8")
    assert calculate(renderer, tmp_path) == {"hac": 0, "sup": 0}
    table(tmp_path, [("S1_COI", "hac")] * 2)
    assert calculate(renderer, tmp_path) == oracle(p, {"S1_COI"}) == {"hac": 2, "sup": 0}
    with p.open("a") as stream:
        stream.write("\nshort\nread3\tS1_COI\tunknown\n")
    assert calculate(renderer, tmp_path) == {"hac": 2, "sup": 0}


def test_retained_s0_counts(renderer, tmp_path):
    """A-F1-01/02: opt-in verified archive copy, never rerun the S0 pipeline."""
    retained = os.environ.get("RTB_F1_RETAINED_OUT")
    if not retained:
        pytest.skip("set RTB_F1_RETAINED_OUT to the verified retained C1/out copy")
    root = Path(retained)
    state = root / "temp/ongoing/state/R10S0_CAL"
    rounds = [json.loads(line) for line in (state / "_state/report_history.jsonl").read_text().splitlines() if line]
    p = state / rounds[0]["round_barcode"] / "SampleA_demult_rpt.txt"
    expected = oracle(p, {"SampleA_COI"})
    assert expected == {"hac": 2, "sup": 0}
    dest = tmp_path / "temp/ongoing/state/R10S0_CAL" / rounds[0]["round_barcode"] / p.name
    dest.parent.mkdir(parents=True)
    shutil.copyfile(p, dest)
    sample_id, raw = next(iter(rounds[-1]["sample_metrics"].items()))
    rows = renderer.build_sample_history_rows(rounds, sample_id, raw["label"], out_path(tmp_path), "R10S0_CAL")
    assert counts(rows) == [(2, 0, 2, 0)]


def test_signatures_regenerate_four_once_and_preserve_membership(renderer, tmp_path, monkeypatch):
    """A-F1-14/15/16/18/19: same-process 0->2 correction, then stable renders."""
    rounds = [round_obj()]
    out = out_path(tmp_path)
    out.parent.mkdir(parents=True)
    untouched = {out.with_name("run_report.json"): b'{"run_id":"runA"}\n',
                 tmp_path / "report_html/runs_index.jsonl": b'{"run_id":"runA"}\n',
                 out.parent / ".report_render_pending": b"pending\n",
                 out.parent / ".report_history_pending": b"pending\n"}
    for p, data in untouched.items():
        p.write_bytes(data)
    calls = []

    def write(kind, rows, png, pdf):
        calls.append(kind)
        png.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(rows, sort_keys=True).encode()
        png.write_bytes(data)
        pdf.write_bytes(data)

    monkeypatch.setattr(renderer, "export_sample_history_reads_r", lambda rows, s, png, pdf: write("history_reads", rows, png, pdf))
    monkeypatch.setattr(renderer, "export_sample_history_cumulative_r", lambda rows, s, png, pdf: write("history_cumulative", rows, png, pdf))
    monkeypatch.setattr(renderer, "export_sample_history_taxonomy_r", lambda rows, s, source, png, pdf: write("history_" + source + "_tax", rows, png, pdf))
    # Isolate history signatures from unrelated taxonomy plotting.
    monkeypatch.setattr(renderer, "export_icicle_plot_png_pdf", lambda *a: write("taxonomy", {}, a[-2], a[-1]))
    monkeypatch.setattr(renderer, "export_sunburst_plot_png_pdf", lambda *a: write("taxonomy", {}, a[-2], a[-1]))

    history = tmp_path / "history.jsonl"
    history.write_text(json.dumps(rounds[0]) + "\n", encoding="utf-8")
    monkeypatch.setattr(renderer, "build_chart_exports", lambda *a, **k: {})

    class FrozenDatetime(dt.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 1, 2)

    monkeypatch.setattr(renderer, "dt", SimpleNamespace(datetime=FrozenDatetime, timezone=dt.timezone))
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--history", str(history),
                                    "--template", str(ROOT / "assets/report/template.html"),
                                    "--css", str(ROOT / "assets/report/report.css"),
                                    "--js", str(ROOT / "assets/report/report.js"),
                                    "--out", str(out), "--run-index", str(tmp_path / "report_html/runs_index.jsonl"),
                                    "--run-id-filter", "runA"])

    def render():
        renderer.main()
        text = out.read_text()
        marker = "window.REPORT_PAYLOAD = "
        start = text.index(marker) + len(marker)
        payload = json.loads(text[start:text.index(";\n", start)])
        return payload["rounds"][-1]["sample_metrics"]["s1"]["figures"]

    def sigs():
        return {p.name: p.read_bytes() for p in out.parent.rglob("*.sig")}

    before = render()
    old_sigs = sigs()
    old_html = out.read_bytes()
    old_state = out.with_name("report_state.json").read_bytes()
    calls.clear()
    assert render() == before and not calls and sigs() == old_sigs
    p = table(tmp_path, [("S1_COI", "hac")] * 2)
    assert oracle(p, {"S1_COI"}) == {"hac": 2, "sup": 0}
    after = render()
    new_sigs = sigs()
    assert sorted(calls) == sorted(KINDS.values())
    assert after == before
    assert out.read_bytes() == old_html
    assert out.with_name("report_state.json").read_bytes() == old_state
    changed = {n for n in old_sigs if old_sigs[n] != new_sigs[n]}
    assert changed == {f"s1_{suffix}.sig" for suffix in KINDS}
    rows = renderer.build_sample_history_rows(rounds, "s1", "S1", out, "runA")
    assert counts(rows) == [(2, 0, 2, 0)]
    for suffix, kind in KINDS.items():
        payload = {"version": "sample-assets-v1", "run_id": "runA", "sample_id": "s1",
                   "figure_kind": kind, "marker": None, "payload": rows}
        expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        assert new_sigs[f"s1_{suffix}.sig"].decode().strip() == expected
    calls.clear()
    assert render() == after and render() == after and not calls and sigs() == new_sigs
    assert all(p.read_bytes() == data for p, data in untouched.items())


@pytest.mark.skipif(shutil.which("Rscript") is None, reason="Rscript unavailable")
def test_nonzero_figures_deterministic_and_match_oracle(renderer, tmp_path):
    """A-F1-17: sequential real R; mask only two PDF timestamp metadata fields."""
    p = table(tmp_path, [("S1_COI", "hac")] * 2)
    rounds = [round_obj()]
    rows = renderer.build_sample_history_rows(rounds, "s1", "S1", out_path(tmp_path), "runA", run_start_epoch=1767225600)
    expected = oracle(p, {"S1_COI"})
    assert expected == {"hac": 2, "sup": 0}
    oracle_rows = copy.deepcopy(rows)
    oracle_rows[-1].update(hac_reads_round=2, sup_reads_round=0, hac_reads_cumulative=2, sup_reads_cumulative=0)
    assert rows == oracle_rows
    functions = [("reads", renderer.export_sample_history_reads_r),
                 ("cumulative", renderer.export_sample_history_cumulative_r),
                 ("otu", lambda r, s, png, pdf: renderer.export_sample_history_taxonomy_r(r, s, "otu", png, pdf)),
                 ("consensus", lambda r, s, png, pdf: renderer.export_sample_history_taxonomy_r(r, s, "consensus", png, pdf))]
    for kind, fn in functions:
        for name, data in [("candidate", rows), ("oracle", oracle_rows)]:
            fn(data, "s1", tmp_path / f"{kind}_{name}.png", tmp_path / f"{kind}_{name}.pdf")
        assert (tmp_path / f"{kind}_candidate.png").read_bytes() == (tmp_path / f"{kind}_oracle.png").read_bytes()
        pdfs = [(tmp_path / f"{kind}_{name}.pdf").read_bytes() for name in ("candidate", "oracle")]
        assert all(data.startswith(b"%PDF") for data in pdfs)
        normalized = [re.sub(rb"/(CreationDate|ModDate)\s*\([^)]*\)", rb"/\1(DATE)", data) for data in pdfs]
        assert normalized[0] == normalized[1]
    zero = copy.deepcopy(rows)
    zero[-1].update(hac_reads_round=0, hac_reads_cumulative=0)
    renderer.export_sample_history_reads_r(zero, "s1", tmp_path / "reads_zero.png", tmp_path / "reads_zero.pdf")
    assert (tmp_path / "reads_zero.png").read_bytes() != (tmp_path / "reads_candidate.png").read_bytes()


@pytest.mark.parametrize("nrounds,nrows", [(50, 1000), (300, 2000), (1000, 2000)])
def test_performance_count_conservation(renderer, tmp_path, nrounds, nrows):
    """A-F1-21: same dimensions as the audit; correctness without a flaky time threshold."""
    rounds = []
    for i in range(nrounds):
        name = f"r{i:04d}"
        table(tmp_path, [(f"S{j % 10}_COI", "hac" if j % 3 else "sup") for j in range(nrows)], round_name=name)
        rounds.append({**round_obj(name), "timestamp_utc": f"2026-01-01T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"})
    start = time.perf_counter()
    total = 0
    for sample in range(10):
        rows = renderer.build_sample_history_rows(rounds, f"s{sample}", f"S{sample}", out_path(tmp_path), "runA")
        total += rows[-1]["hac_reads_cumulative"] + rows[-1]["sup_reads_cumulative"]
    elapsed = time.perf_counter() - start
    assert total == nrounds * nrows
    print(json.dumps({"rounds": nrounds, "samples": 10, "rows_per_round_table": nrows,
                      "seconds_all_samples": elapsed, "counted_reads": total, "expected_reads": nrounds * nrows}))
