"""Real-script checks for the two R10-D2 ggrepel layers."""

import csv
import hashlib
import math
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(os.environ.get("RTB_PLOT_SCRIPT_ROOT", Path(__file__).resolve().parents[1]))
R = Path(
    "/Users/DavidJuan/Downloads/RTBioScan_working/.rtbioscan-envs/"
    "r4-smoke-bfc32567/bin/Rscript"
)
SCRIPTS = ("Time_reads.R", "CircleTree_assignments.R")
CIRCLE_CASES = (("retained", 2), ("adversarial", 30))
DATE = re.compile(rb"/(CreationDate|ModDate)\s*\(D:[^)]*\)")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def masked_pdf(data):
    assert DATE.search(data), "PDF date metadata was not found"
    return DATE.sub(lambda m: b"/" + m.group(1) + b" (D:MASKED)", data)


def assert_repeatable(outputs):
    assert len({digest(png) for png, _ in outputs}) == 1
    assert len({digest(masked_pdf(pdf)) for _, pdf in outputs}) == 1


def check_runtime():
    if not R.is_file():
        pytest.skip(f"pinned R10 Rscript unavailable: {R}")
    probe = subprocess.run(
        [str(R), "-e", 'for (p in c("ggplot2", "ggrepel", "dplyr", "withr")) '
         'if (!requireNamespace(p, quietly = TRUE)) stop(p); '
         'cat(as.character(packageVersion("ggrepel")))'],
        capture_output=True, text=True, timeout=20,
    )
    if probe.returncode:
        pytest.skip("pinned R10 plotting dependency unavailable: " + probe.stderr[-300:])
    assert probe.stdout.strip() == "0.9.6", probe.stdout


@pytest.fixture(scope="module", autouse=True)
def runtime():
    check_runtime()


def time_fixture(path, series=("sequencing", "processed", "fast", "hac", "sup"),
                 coincident=False, degenerate=False):
    rows = [("run_id", "time", "data", "reads")]
    for t in range(1 if degenerate else 3):
        for j, name in enumerate(series):
            count = 0 if degenerate else (10 if coincident else 10 + j + t)
            rows.append((f"r{t}", str(t * 3600), name, str(count)))
    with path.open("w", newline="") as handle:
        csv.writer(handle, delimiter="\t", lineterminator="\n").writerows(rows)
    return rows[1:]


def circle_fixture(path, n=30):
    rows = [("path", "weight", "sample")]
    for j in range(n):
        rows.append((f"Root;Group;Long_label_taxon_{j:02d}_overlap_test", str(100 - j), "Synthetic"))
    with path.open("w", newline="") as handle:
        csv.writer(handle, delimiter="\t", lineterminator="\n").writerows(rows)
    return rows[1:]


def run_script(script, inp, workdir, driver=False, script_root=ROOT):
    workdir.mkdir(parents=True, exist_ok=True)
    output = workdir / (inp.name.replace("_reads_time_rpt.txt", "_reads_time.png")
                        if script == "Time_reads.R" else "circle.png")
    args = [str(R), str(script_root / "bin" / script), str(inp)]
    if script == "CircleTree_assignments.R":
        args += [str(output), "Synthetic circle"]
    if driver:
        driver_path = workdir / "driver.R"
        driver_path.write_text(DRIVER)
        args = [str(R), str(driver_path), str(script_root / "bin" / script),
                str(inp), str(output), str(workdir / "observed.tsv")]
    env = {"PATH": str(R.parent) + ":/usr/bin:/bin", "HOME": str(workdir),
           "TMPDIR": str(workdir), "LANG": "en_US.UTF-8"}
    proc = subprocess.run(args, cwd=workdir, env=env, capture_output=True,
                          text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[-1000:]
    pdf = output.with_suffix(".pdf")
    assert output.is_file() and pdf.is_file()
    assert set(p.name for p in workdir.glob("*.png")) == {output.name}
    assert set(p.name for p in workdir.glob("*.pdf")) == {pdf.name}
    return output.read_bytes(), pdf.read_bytes()


DRIVER = r'''
argv <- base::commandArgs(trailingOnly = TRUE)
script <- argv[1]; input <- argv[2]; output <- argv[3]; observed <- argv[4]
commandArgs <- function(trailingOnly = TRUE) {
  if (trailingOnly) {
    if (grepl("CircleTree", script)) return(c(input, output, "Synthetic circle"))
    return(c(input))
  }
  c(paste0("--file=", script))
}
set.seed(20260929L)
before <- .Random.seed
expected <- runif(1)
.Random.seed <- before
source(script, local = globalenv())
stopifnot(identical(before, .Random.seed), identical(expected, runif(1)))
if (grepl("CircleTree", script)) {
  stopifnot(nrow(label_df) > 0)
  data <- data.frame(name = label_df$name, x = label_df$x,
                     y = label_df$y, weight = label_df$leaf_weight)
  built <- ggplot2::ggplot_build(p)$data[[3]]
  stopifnot(nrow(built) == nrow(data), identical(as.character(built$label), as.character(data$name)))
} else {
  data <- data.frame(name = as.character(data_cumulative$data),
                     x = data_cumulative$hours, y = data_cumulative$reads,
                     label = ifelse(is.na(data_cumulative$label), "", data_cumulative$label))
  built <- ggplot2::ggplot_build(p)$data
  stopifnot(nrow(built[[1]]) == nrow(data),
            sum(!is.na(built[[2]]$label) & nzchar(built[[2]]$label)) == length(unique(data$name)))
}
write.table(data, observed, sep = "\t", row.names = FALSE, quote = FALSE)
'''


def assert_time_oracle(rows, observed):
    zero = min(int(r[1]) for r in rows)
    totals = {}
    expected = []
    for _, t, name, count in sorted(rows, key=lambda r: int(r[1])):
        totals[name] = totals.get(name, 0) + int(count)
        expected.append((name, (int(t) - zero) / 3600, totals[name]))
    actual = [(r["name"], float(r["x"]), float(r["y"])) for r in observed]
    assert sorted(actual) == sorted(expected)
    for name in totals:
        labels = [r["label"] for r in observed if r["name"] == name and r["label"]]
        assert labels == [name]


def assert_circle_oracle(rows, observed):
    assert len(rows) == 30 and len(observed) == 20, "adversarial fixture was weakened"
    expected = {r[0].split(";")[-1]: int(r[1]) for r in rows[:20]}
    assert {r["name"]: int(float(r["weight"])) for r in observed} == expected
    for row in observed:
        idx = int(row["name"].split("_")[3])
        angle = 2 * math.pi * idx / 30
        assert math.isclose(float(row["x"]), 2 * math.cos(angle), abs_tol=1e-6)
        assert math.isclose(float(row["y"]), 2 * math.sin(angle), abs_tol=1e-6)


def test_literal_layer_seed_and_comparator():
    assert ("adversarial", 30) in CIRCLE_CASES
    for name in SCRIPTS:
        source = (ROOT / "bin" / name).read_text()
        assert "set.seed(" not in source
        calls = re.findall(r"(?:ggrepel::)?geom_text_repel\s*\((.*?)\n\s*\)", source, re.S)
        if name == "Time_reads.R":
            calls = re.findall(r"geom_text_repel\s*\(([^\n]*)\)", source)
        assert len(calls) == 1
        assert re.search(r"\bseed\s*=\s*1L\b", calls[0])
    png = b"\x89PNG" + b"a" * 100
    changed = png[:-1] + b"b"
    assert len(png) == len(changed) and digest(png) != digest(changed)
    pdf = b"/CreationDate (D:20260929120000) /ModDate (D:20260929120000) /Type /Page"
    assert masked_pdf(pdf) == masked_pdf(pdf.replace(b"120000", b"130000"))
    assert masked_pdf(pdf) != masked_pdf(pdf.replace(b"/Type /Page", b"/Type /Poge"))
    with pytest.raises(AssertionError):
        assert_repeatable([(png, pdf), (changed, pdf)])
    with pytest.raises(AssertionError):
        assert_repeatable([(png, pdf), (png, pdf.replace(b"/Type /Page", b"/Type /Poge"))])


@pytest.mark.parametrize("case,series,coincident,degenerate", [
    ("retained", ("sequencing", "processed", "fast", "hac", "sup"), False, False),
    ("history", ("hac", "sup"), False, False),
    ("overlap", ("sequencing", "processed", "fast", "hac", "sup"), True, False),
    ("single", ("sequencing",), False, False),
    ("degenerate", ("sequencing",), False, True),
])
def test_time_fresh_processes(tmp_path, case, series, coincident, degenerate):
    inp = tmp_path / "Sample_reads_time_rpt.txt"
    rows = time_fixture(inp, series, coincident, degenerate)
    count = 5 if case in ("retained", "history", "overlap") else 3
    outputs = [run_script("Time_reads.R", inp, tmp_path / f"run{i}") for i in range(count)]
    assert_repeatable(outputs)
    run_script("Time_reads.R", inp, tmp_path / "oracle", driver=True)
    with (tmp_path / "oracle" / "observed.tsv").open() as handle:
        assert_time_oracle(rows, list(csv.DictReader(handle, delimiter="\t")))


def test_changed_input_changes_plot(tmp_path):
    first = tmp_path / "first" / "Sample_reads_time_rpt.txt"
    first.parent.mkdir()
    rows = time_fixture(first)
    second = tmp_path / "second" / "Sample_reads_time_rpt.txt"
    second.parent.mkdir()
    time_fixture(second)
    data = second.read_text().replace("r1\t3600\tfast\t13", "r1\t3600\tfast\t113")
    assert data != second.read_text()
    second.write_text(data)
    a, _ = run_script("Time_reads.R", first, tmp_path / "a")
    b, _ = run_script("Time_reads.R", second, tmp_path / "b")
    assert digest(a) != digest(b)


@pytest.mark.parametrize("case,n", CIRCLE_CASES)
def test_circle_fresh_processes(tmp_path, case, n):
    inp = tmp_path / "circle.tsv"
    rows = circle_fixture(inp, n)
    count = 5 if n == 30 else 3
    outputs = [run_script("CircleTree_assignments.R", inp, tmp_path / f"run{i}") for i in range(count)]
    assert_repeatable(outputs)
    if n == 30:
        baseline = tmp_path / "unseeded" / "bin"
        baseline.mkdir(parents=True)
        shutil.copyfile(ROOT / "bin" / "plot_style.R", baseline / "plot_style.R")
        source = (ROOT / "bin" / "CircleTree_assignments.R").read_text()
        assert source.count("seed = 1L") == 1
        (baseline / "CircleTree_assignments.R").write_text(source.replace(",\n      seed = 1L", ""))
        unseeded = [run_script("CircleTree_assignments.R", inp, tmp_path / f"unseeded{i}",
                               script_root=baseline.parent) for i in range(3)]
        assert len({digest(png) for png, _ in unseeded}) > 1, "adversarial case did not reveal baseline variation"
        run_script("CircleTree_assignments.R", inp, tmp_path / "oracle", driver=True)
        with (tmp_path / "oracle" / "observed.tsv").open() as handle:
            assert_circle_oracle(rows, list(csv.DictReader(handle, delimiter="\t")))
