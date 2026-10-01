"""N-F2-01: input counts reconcile with all three decoded read-count figures.

Fixtures are the audited producer tables. Retained row indices and axis order are
explicit fixture expectations, independent of the R mode/grouping implementation.
Counts, fills and log heights are computed from those input rows in Python.
"""
import csv
import io
import math
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIGURES = ("reads_per_barcode", "reads_per_sample", "reads_per_sample_log")
TRACK = ("track_sample_label", "track_replicate_suffix", "track_replicate_number", "track_plate_label")

FIXTURES = {'c1_summary.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                   'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                   'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                   'track_plate_label\ttrack_replicate_suffix\n'
                   '2\tCOI\thac\tSampleA_COI\tSampleA\tCOI\tunknown\tunknown\t'
                   'SampleA\t\t\t\t\t\t\n',
 'dupes.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
              'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\ttrack_sample_label\t'
              'track_unit_label\ttrack_replicate_number\ttrack_plate_label\t'
              'track_replicate_suffix\n'
              '3\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\t\t\t\t\t\t\n'
              '3\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\t\t\t\t\t\t\n'
              '4\tITS2\thac\tSA_ITS2\tSA\tITS2\tu\tu\tSampleA\t\t\t\t\t\t\n',
 'header_only.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                    'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                    'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                    'track_plate_label\ttrack_replicate_suffix\n',
 'legacy_no_track.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                        'sampling_method\tsubsample\treplicate\tsample_name\n'
                        '9\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\n',
 'literalna.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                  'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                  'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                  'track_plate_label\ttrack_replicate_suffix\n'
                  '5\tCOI\thac\tNA_COI\tNA\tCOI\tu\tu\tSampleZ\ttrack\tNA\tNA_1_P1\t1\tP1\t1_P1\n',
 'mixed_rev.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                  'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                  'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                  'track_plate_label\ttrack_replicate_suffix\n'
                  '7\tCOI\thac\tSD_COI\tSD\tCOI\tu\tu\tSampleD\t\t\t\t\t\t\n'
                  '40\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\ttrack\tPlot1\tPlot1_1_P1\t1\tP1\t'
                  '1_P1\n',
 'mixed_track.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                    'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                    'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                    'track_plate_label\ttrack_replicate_suffix\n'
                    '40\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\ttrack\tPlot1\tPlot1_1_P1\t1\t'
                    'P1\t1_P1\n'
                    '7\tCOI\thac\tSD_COI\tSD\tCOI\tu\tu\tSampleD\t\t\t\t\t\t\n',
 'multi_perm.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                   'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                   'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                   'track_plate_label\ttrack_replicate_suffix\n'
                   '7\tCOI\thac\tSample-F.1_COI\tSample-F.1\tCOI\tunknown\tunknown\t'
                   'Sample-F.1\t\t\t\t\t\t\n'
                   'NA\tCOI\thac\tSampleE_COI\tSampleE\tCOI\tunknown\tunknown\t'
                   'SampleE\t\t\t\t\t\t\n'
                   '5\tITS2\thac\tSampleC_ITS2\tSampleC\tITS2\tunknown\tunknown\t'
                   'SampleC\t\t\t\t\t\t\n'
                   '5\tCOI\thac\tSampleD_COI\tSampleD\tCOI\tunknown\tunknown\tSampleD\t\t\t\t\t\t\n'
                   '999\tCOI\thac\tno_adapter_x_COI\tno_adapter_x\tCOI\tunknown\tunknown\t'
                   'no_adapter_x\t\t\t\t\t\t\n'
                   '50\tCOI\thac\tÉchantillon 水_COI\tÉchantillon 水\tCOI\tunknown\tunknown\t'
                   'Échantillon 水\t\t\t\t\t\t\n'
                   '150\tCOI\tsup\tSampleA_COI\tSampleA\tCOI\tunknown\tunknown\t'
                   'SampleA\t\t\t\t\t\t\n'
                   '30\tITS2\thac\tSampleA_ITS2\tSampleA\tITS2\tunknown\tunknown\t'
                   'SampleA\t\t\t\t\t\t\n'
                   '120\tCOI\thac\tSampleA_COI\tSampleA\tCOI\tunknown\tunknown\t'
                   'SampleA\t\t\t\t\t\t\n',
 'multi_summary.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                      'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                      'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                      'track_plate_label\ttrack_replicate_suffix\n'
                      '120\tCOI\thac\tSampleA_COI\tSampleA\tCOI\tunknown\tunknown\t'
                      'SampleA\t\t\t\t\t\t\n'
                      '30\tITS2\thac\tSampleA_ITS2\tSampleA\tITS2\tunknown\tunknown\t'
                      'SampleA\t\t\t\t\t\t\n'
                      '150\tCOI\tsup\tSampleA_COI\tSampleA\tCOI\tunknown\tunknown\t'
                      'SampleA\t\t\t\t\t\t\n'
                      '50\tCOI\thac\tÉchantillon 水_COI\tÉchantillon 水\tCOI\tunknown\tunknown\t'
                      'Échantillon 水\t\t\t\t\t\t\n'
                      '999\tCOI\thac\tno_adapter_x_COI\tno_adapter_x\tCOI\tunknown\tunknown\t'
                      'no_adapter_x\t\t\t\t\t\t\n'
                      '5\tCOI\thac\tSampleD_COI\tSampleD\tCOI\tunknown\tunknown\t'
                      'SampleD\t\t\t\t\t\t\n'
                      '5\tITS2\thac\tSampleC_ITS2\tSampleC\tITS2\tunknown\tunknown\t'
                      'SampleC\t\t\t\t\t\t\n'
                      'NA\tCOI\thac\tSampleE_COI\tSampleE\tCOI\tunknown\tunknown\t'
                      'SampleE\t\t\t\t\t\t\n'
                      '7\tCOI\thac\tSample-F.1_COI\tSample-F.1\tCOI\tunknown\tunknown\t'
                      'Sample-F.1\t\t\t\t\t\t\n',
 'track_summary.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                      'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                      'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                      'track_plate_label\ttrack_replicate_suffix\n'
                      '40\tCOI\thac\tSampleA_COI\tSampleA\tCOI\tunknown\tunknown\tSampleA\ttrack\t'
                      'Plot1\t\t\t\t\n'
                      '60\tCOI\thac\tSampleB_COI\tSampleB\tCOI\tunknown\tunknown\tSampleB\ttrack\t'
                      'Plot1\t\t\t\t\n'
                      '10\tITS2\thac\tSampleC_ITS2\tSampleC\tITS2\tunknown\tunknown\tSampleC\t'
                      'track\tPlot2\t\t\t\t\n',
 'weird_counts.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                     'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                     'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                     'track_plate_label\ttrack_replicate_suffix\n'
                     '2.5\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\t\t\t\t\t\t\n'
                     '2000000000\tITS2\thac\tSA_ITS2\tSA\tITS2\tu\tu\tSampleA\t\t\t\t\t\t\n',
 'wellformed_track.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                         'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                         'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                         'track_plate_label\ttrack_replicate_suffix\n'
                         '40\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\ttrack\tPlot1\tPlot1_1_P1\t'
                         '1\tP1\t1_P1\n'
                         '60\tCOI\thac\tSB_COI\tSB\tCOI\tu\tu\tSampleB\ttrack\tPlot1\tPlot1_2_P1\t'
                         '2\tP1\t2_P1\n'
                         '10\tITS2\thac\tSC_ITS2\tSC\tITS2\tu\tu\tSampleC\ttrack\tPlot2\t'
                         'Plot2_1_P2\t1\tP2\t1_P2\n',
 'whitespace.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                   'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                   'track_sample_label\ttrack_unit_label\ttrack_replicate_number\t'
                   'track_plate_label\ttrack_replicate_suffix\n'
                   '5\tCOI\thac\tW_COI\tW\tCOI\tu\tu\tSampleW\ttrack\t \t \t \t \t \n',
 'ws_mixed.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
                 'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\t'
                 'track_sample_label\ttrack_unit_label\ttrack_replicate_number\ttrack_plate_label\t'
                 'track_replicate_suffix\n'
                 '40\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\ttrack\tPlot1\tPlot1_1_P1\t1\tP1\t'
                 '1_P1\n'
                 '5\tCOI\thac\tSW_COI\tSW\tCOI\tu\tu\tSampleW\ttrack\t \t \t2\tP1\t2_P1\n',
 'zero.tsv': 'read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t'
             'sampling_method\tsubsample\treplicate\tsample_name\ttrack_mode\ttrack_sample_label\t'
             'track_unit_label\ttrack_replicate_number\ttrack_plate_label\ttrack_replicate_suffix\n'
             '0\tCOI\thac\tSA_COI\tSA\tCOI\tu\tu\tSampleA\t\t\t\t\t\t\n'}

# name, fixture, filter, retained input rows, ordered axis, x column
CASES = [('A_c1', 'c1_summary.tsv', None, (0,), ('unknown',), 'replicate'),
 ('A_multi', 'multi_summary.tsv', None, (0, 1, 3, 5, 6, 7, 8), ('unknown',), 'replicate'),
 ('A_multi_perm', 'multi_perm.tsv', None, (0, 1, 2, 3, 5, 7, 8), ('unknown',), 'replicate'),
 ('A_track', 'track_summary.tsv', None, (0, 1, 2), ('unknown',), 'replicate'),
 ('A_wellformed',
  'wellformed_track.tsv',
  None,
  (0, 1, 2),
  ('1_P1', '1_P2', '2_P1'),
  'track_replicate_suffix'),
 ('A_mixed', 'mixed_track.tsv', None, (0, 1), ('1_P1', ''), 'track_replicate_suffix'),
 ('A_mixed_rev', 'mixed_rev.tsv', None, (0, 1), ('1_P1', ''), 'track_replicate_suffix'),
 ('A_literalna', 'literalna.tsv', None, (0,), ('u',), 'replicate'),
 ('A_whitespace', 'whitespace.tsv', None, (0,), ('u',), 'replicate'),
 ('A_ws_mixed', 'ws_mixed.tsv', None, (0, 1), ('1_P1', '2_P1'), 'track_replicate_suffix'),
 ('A_dupes', 'dupes.tsv', None, (0, 1, 2), ('u',), 'replicate'),
 ('A_zero', 'zero.tsv', None, (0,), ('u',), 'replicate'),
 ('A_weird', 'weird_counts.tsv', None, (0, 1), ('u',), 'replicate'),
 ('A_legacy', 'legacy_no_track.tsv', None, (0,), ('u',), 'replicate'),
 ('A_header', 'header_only.tsv', None, (), (), 'replicate'),
 ('B_c1_SampleA', 'c1_summary.tsv', 'SampleA', (0,), ('unknown',), 'replicate'),
 ('B_multi_SampleA', 'multi_summary.tsv', 'SampleA', (0, 1), ('unknown',), 'replicate'),
 ('B_multi_unicode', 'multi_summary.tsv', 'Échantillon 水', (3,), ('unknown',), 'replicate'),
 ('B_multi_perm_SampleA', 'multi_perm.tsv', 'SampleA', (7, 8), ('unknown',), 'replicate'),
 ('B_track_Plot1', 'track_summary.tsv', 'Plot1', (0, 1), ('unknown',), 'replicate'),
 ('B_wellformed_Plot1',
  'wellformed_track.tsv',
  'Plot1',
  (0, 1),
  ('1_P1', '2_P1'),
  'track_replicate_suffix'),
 ('B_mixed_Plot1', 'mixed_track.tsv', 'Plot1', (0,), ('1_P1',), 'track_replicate_suffix'),
 ('B_mixed_SampleD', 'mixed_track.tsv', 'SampleD', (), (), 'replicate'),
 ('B_mixedrev_Plot1', 'mixed_rev.tsv', 'Plot1', (1,), ('1_P1',), 'track_replicate_suffix'),
 ('B_ws_mixed_space', 'ws_mixed.tsv', ' ', (1,), ('2_P1',), 'track_replicate_suffix'),
 ('B_dupes_SampleA', 'dupes.tsv', 'SampleA', (0, 1, 2), ('u',), 'replicate'),
 ('B_zero_SampleA', 'zero.tsv', 'SampleA', (0,), ('u',), 'replicate'),
 ('B_weird_SampleA', 'weird_counts.tsv', 'SampleA', (0, 1), ('u',), 'replicate'),
 ('B_literalna_SampleZ', 'literalna.tsv', 'SampleZ', (0,), ('u',), 'replicate'),
 ('B_whitespace_SampleW', 'whitespace.tsv', 'SampleW', (0,), ('u',), 'replicate'),
 ('B_c1_nomatch', 'c1_summary.tsv', 'Nope', (), (), 'replicate')]

SHIM = r"""
# NF2-01 audit instrumentation: real plot_style + ggplot_build capture (base R only)
source(Sys.getenv("NF201_REAL_PLOT_STYLE"))
.real_save_plot_journal <- save_plot_journal
save_plot_journal <- function(p, out_png, out_pdf = NULL) {
  g <- function(expr) tryCatch(expr, error = function(e) "ERR")
  b <- ggplot2::ggplot_build(p)
  d <- b$data[[1]]
  col <- function(nm) if (!is.null(d[[nm]])) paste(d[[nm]], collapse=",") else ""
  out <- c(
    paste0("out_png\t", out_png),
    paste0("layer_geom\t", class(p$layers[[1]]$geom)[1]),
    paste0("n_marks\t", nrow(d)),
    paste0("layers\t", paste(vapply(p$layers, function(l) class(l$geom)[1], ""), collapse="|")),
    paste0("label_x\t", p$labels$x),
    paste0("label_y\t", p$labels$y),
    paste0("bar_x\t", col("x")),
    paste0("axis_x_labels\t", g(paste(b$layout$panel_params[[1]]$x$get_labels(), collapse="|"))),
    paste0("bar_ymin\t", col("ymin")),
    paste0("bar_ymax\t", col("ymax")),
    paste0("bar_fill\t", col("fill")),
    paste0("text_label\t", col("label")),
    paste0("y_range\t", g(paste(b$layout$panel_params[[1]]$y.range, collapse=","))),
    paste0("rows_in_plot_data\t", g(nrow(p$data))),
    paste0("data_x_label\t", g(paste(as.character(p$data$x_label), collapse=","))),
    paste0("data_read_count\t", g(paste(p$data$read_count, collapse=","))),
    paste0("data_fill\t", g(paste(as.character(p$data$barcode_by_homology), collapse=",")))
  )
  writeLines(out, paste0(out_png, ".decode.tsv"))
  .real_save_plot_journal(p, out_png, out_pdf)
}
"""


def expected(case, text=None):
    _, fixture, _, indices, axes, x_column = case
    rows = list(csv.DictReader(io.StringIO(text or FIXTURES[fixture]), delimiter="\t"))
    selected = [rows[i] for i in indices]
    counts = [None if r["read_count"] == "NA" else float(r["read_count"]) for r in selected]
    # Only missing suffixes in the mixed fixtures normalize to empty labels.
    return {"axis": list(axes), "counts": counts,
            "x": [r[x_column] or "" for r in selected],
            "fills": [r["barcode_by_homology"] for r in selected]}


def number(value):
    return None if value in ("NA", "NaN") else float(value)


def assert_plot_data(want, figures):
    assert set(figures) == set(FIGURES)
    if not want["counts"]:
        for d in figures.values():
            assert d["layer_geom"] == "GeomText"
            assert d["text_label"] == "No sample read-count data"
            assert int(d["n_marks"]) == 1
        return
    for key in ("data_x_label", "data_read_count", "data_fill", "rows_in_plot_data"):
        assert len({d[key] for d in figures.values()}) == 1, key
    tuples = Counter(zip(want["x"], want["counts"], want["fills"]))
    for name, d in figures.items():
        assert d["layer_geom"] == "GeomCol"
        assert int(d["rows_in_plot_data"]) == len(want["counts"])
        assert d["axis_x_labels"].split("|") == want["axis"]
        assert d["label_x"] == "Replicate"
        assert d["label_y"] == ("Read count (log scale)" if name.endswith("_log") else "Read count")
        got = Counter(zip(d["data_x_label"].split(","),
                          map(number, d["data_read_count"].split(",")),
                          d["data_fill"].split(",")))
        assert got == tuples, (name, got, tuples)
        # Inspect actual rendered marks, not only p$data. This kills log-only M12.
        heights = []
        for low, high in zip(d["bar_ymin"].split(","), d["bar_ymax"].split(",")):
            low, high = number(low), number(high)
            if low is None or high is None:
                heights.append(None)
            elif math.isfinite(low) and math.isfinite(high):
                heights.append(high - low)
        if name.endswith("_log"):
            heights = [h for h in heights if h is not None]
            target = [abs(math.log10(c)) for c in want["counts"] if c is not None and c > 0]
        else:
            target = want["counts"]
            assert heights.count(None) == target.count(None)
            heights = [h for h in heights if h is not None]
            target = [h for h in target if h is not None]
        assert sorted(heights) == pytest.approx(sorted(target), rel=1e-10, abs=1e-7)


def test_normalization_static():
    """Always runs, including platforms without R and its plotting packages."""
    source = (ROOT / "bin/Read_counts.R").read_text(encoding="utf-8")
    start = source.index("has_track_cols <-")
    finish = source.index('group_col <- "sample_name"', start)
    block = source[start:finish]
    for column in TRACK:
        assert '"' + column + '"' in block
    assert 'if (has_track_cols) {' in block
    assert '.track_vals <- as.character(sample_info[[.track_col]])' in block
    assert '.track_vals[is.na(.track_vals)] <- ""' in block
    assert 'sample_info[[.track_col]] <- .track_vals' in block
    assert 'trimws' not in block
    assert len(CASES) == 31
    assert expected(CASES[15])["counts"] == [2.0]


@pytest.fixture(scope="module")
def rscript():
    executable = os.environ.get("RTBIOSCAN_RSCRIPT") or shutil.which("Rscript")
    if not executable or not Path(executable).is_file():
        pytest.skip("Rscript is unavailable; static normalization checks still run")
    probe = subprocess.run([executable, "--vanilla", "-e",
                            'quit(status=if (requireNamespace("ggplot2", quietly=TRUE) && requireNamespace("tidyr", quietly=TRUE)) 0 else 42)'],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode == 42:
        pytest.skip("Rscript lacks required ggplot2/tidyr plotting runtime")
    assert probe.returncode == 0, probe.stderr
    return executable


def render(tmp_path, executable, case, text=None, source=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "Read_counts.R").write_bytes(source or (ROOT / "bin/Read_counts.R").read_bytes())
    (bindir / "plot_style.R").write_text(SHIM, encoding="utf-8")
    fixture = tmp_path / case[1]
    fixture.write_text(text or FIXTURES[case[1]], encoding="utf-8")
    prefix = tmp_path / "figure"
    env = {**os.environ, "NF201_REAL_PLOT_STYLE": str(ROOT / "bin/plot_style.R"), "TMPDIR": str(tmp_path)}
    result = subprocess.run([executable, "--vanilla", str(bindir / "Read_counts.R"),
                             str(fixture), case[2] or "", str(prefix)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    figures = {}
    for name in FIGURES:
        png = tmp_path / ("figure_" + name + ".png")
        pdf = png.with_suffix(".pdf")
        assert png.read_bytes().startswith(b"\x89PNG")
        assert pdf.read_bytes().startswith(b"%PDF")
        figures[name] = dict(line.split("\t", 1) for line in
                             Path(str(png) + ".decode.tsv").read_text().splitlines())
    assert {p.name for p in tmp_path.iterdir()} == {
        "bin", case[1], *("figure_" + n + ext for n in FIGURES for ext in (".png", ".pdf", ".png.decode.tsv"))}
    return figures


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_read_counts_mode_selection(tmp_path, rscript, case):
    assert_plot_data(expected(case), render(tmp_path, rscript, case))


def test_changed_count_repeatability_and_shell_labels(tmp_path, rscript):
    case = CASES[15]
    first = render(tmp_path / "first", rscript, case)
    repeated = render(tmp_path / "repeat", rscript, case)
    assert_plot_data(expected(case), first)
    assert_plot_data(expected(case), repeated)
    for name in FIGURES:
        assert (tmp_path / "first" / ("figure_" + name + ".png")).read_bytes() == (
            tmp_path / "repeat" / ("figure_" + name + ".png")).read_bytes()
    label = "Sample-F.1;$(touch PWNED)&é水"
    text = FIXTURES[case[1]].replace("SampleA", label).replace("2\tCOI", "13\tCOI")
    altered_case = ("shell_labels", case[1], label, (0,), ("unknown",), "replicate")
    altered = render(tmp_path / "altered", rscript, altered_case, text=text)
    assert_plot_data(expected(altered_case, text), altered)
    assert altered[FIGURES[0]]["data_read_count"] != first[FIGURES[0]]["data_read_count"]
    assert not (tmp_path / "altered" / "PWNED").exists()


def test_distinct_sample_replicates_preserve_groups_under_permutation(tmp_path, rscript):
    """Multiple sample groups must keep separate replicate axes and zero groups."""
    header = FIXTURES["c1_summary.tsv"].splitlines()[0]
    records = []
    for count, marker, sample, replicate in (
        (2, "COI", "SampleA", "1"),
        (3, "COI", "SampleB", "1"),
        (7, "ITS2", "SampleA", "2"),
        (0, "COI", "SampleC", "3"),
        (999, "COI", "no_adapter_x", "4"),
    ):
        records.append("\t".join([str(count), marker, "hac", sample + "_" + marker,
                                  sample, marker, "u", replicate, sample] + [""] * 6))
    text = header + "\n" + "\n".join(records) + "\n"
    case = ("distinct_replicates", "c1_summary.tsv", None, (0, 1, 2, 3),
            ("1", "2", "3"), "replicate")
    first = render(tmp_path / "forward", rscript, case, text=text)
    assert_plot_data(expected(case, text), first)
    reversed_text = header + "\n" + "\n".join(reversed(records)) + "\n"
    reversed_case = (case[0], case[1], None, (1, 2, 3, 4), case[4], case[5])
    assert_plot_data(expected(reversed_case, reversed_text),
                     render(tmp_path / "reverse", rscript, reversed_case, text=reversed_text))
    filtered_case = (case[0], case[1], "SampleA", (0, 2), ("1", "2"), "replicate")
    assert_plot_data(expected(filtered_case, text),
                     render(tmp_path / "filtered", rscript, filtered_case, text=text))
