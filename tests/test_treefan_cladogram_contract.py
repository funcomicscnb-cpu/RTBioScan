"""TreeFan's real R process contract; set TREEFAN_APE_RSCRIPT and TREEFAN_NO_APE_RSCRIPT for dynamic runs."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import time
import zlib

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("TREEFAN_SCRIPT", REPO / "bin/TreeFan_cladogram.R"))
MULTI = (
    "marker\tpath\tweight\n"
    "COI\tRoot;Animalia;Genus;Alpha one\t7\n"
    "COI\tRoot;Animalia;Genus;Beta two\t3\n"
)
SINGLE = "marker\tpath\tweight\nCOI\tRoot;Animalia;Genus;Alpha one\t7\n"
EMPTY = "marker\tpath\tweight\n"
NO_ASSIGNMENT = "marker\tpath\tweight\nCOI\tRoot;Animalia;Genus;Alpha one\t0\n"
MALFORMED = "marker\tpath\nCOI\tRoot;Animalia;Alpha\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def png_dimensions(data):
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def pdf_without_dates(data):
    assert data.startswith(b"%PDF-")
    return re.sub(rb"/(CreationDate|ModDate)\s*\(D:[^)]*\)", rb"/\1 (D:MASK)", data)


def pdf_text(data):
    chunks = []
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", data, re.S):
        try:
            stream = zlib.decompress(match.group(1))
        except zlib.error:
            continue
        for block in re.findall(rb"\[(.*?)\] TJ", stream, re.S):
            chunks.append(b"".join(re.findall(rb"\((.*?)\)", block)))
        chunks.extend(re.findall(rb"\((.*?)\) Tj", stream))
    return b" ".join(chunks)


def test_png_header_comparator_rejects_dimension_changes():
    header = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    assert png_dimensions(header + struct.pack(">II", 1, 2)) != png_dimensions(header + struct.pack(">II", 3, 4))


def independent_oracle(tsv):
    rows = [line.split("\t") for line in tsv.strip().splitlines()[1:]]
    branches = {}
    for _, path, raw_weight in rows:
        weight = float(raw_weight)
        if weight <= 0:
            continue
        parts = path.split(";")
        for i in range(1, len(parts) + 1):
            key = ";".join(parts[:i])
            branches[key] = branches.get(key, 0) + weight
    tips = {key: value for key, value in branches.items() if not any(other.startswith(key + ";") for other in branches)}
    internals = sorted(set(branches) - set(tips))
    labels = sorted(tips)
    indices = {name: i + 1 for i, name in enumerate(labels + internals)}
    edges = {(indices[name.rsplit(";", 1)[0]], indices[name]) for name in branches if ";" in name}
    return {"tips": tips, "internals": {k: branches[k] for k in internals}, "edges": edges}


def test_platform_independent_contract_layer():
    assert SCRIPT.is_file()
    assert SCRIPT.name == "TreeFan_cladogram.R"
    expected = independent_oracle(MULTI)
    assert sorted(expected["tips"].values()) == [3, 7]
    assert len(expected["tips"]) == 2
    assert len(expected["edges"]) == 4
    assert independent_oracle(SINGLE)["tips"]["Root;Animalia;Genus;Alpha one"] == 7


def _r_env():
    env = os.environ.copy()
    for name in ("R_LIBS", "R_LIBS_USER", "R_LIBS_SITE", "R_PROFILE_USER", "R_ENVIRON_USER"):
        env.pop(name, None)
    return env


def _runtime(kind):
    key = "TREEFAN_APE_RSCRIPT" if kind == "ape" else "TREEFAN_NO_APE_RSCRIPT"
    candidates = [os.environ.get(key)]
    prefix_key = "TREEFAN_APE_PREFIX" if kind == "ape" else "TREEFAN_NO_APE_PREFIX"
    if os.environ.get(prefix_key):
        candidates.append(str(Path(os.environ[prefix_key]) / "bin/Rscript"))
    candidates.append(shutil.which("Rscript"))
    for path in candidates:
        if not path or not Path(path).is_file():
            continue
        prefix = Path(path).resolve().parents[1]
        probe = subprocess.run([path, "--vanilla", "-e", "cat(as.character(getRversion()), '\\n', requireNamespace('ape', quietly=TRUE), '\\n', paste(.libPaths(), collapse=';'), '\\n')"], env=_r_env(), capture_output=True, text=True)
        if probe.returncode:
            continue
        lines = [x.strip() for x in probe.stdout.splitlines() if x.strip()]
        if len(lines) != 3 or lines[0] != "4.3.3" or lines[1] != ("TRUE" if kind == "ape" else "FALSE"):
            continue
        if lines[2] != str(prefix / "lib/R/library"):
            continue
        meta = prefix / "conda-meta"
        expected_count = 239 if kind == "ape" else 237
        if len(list(meta.glob("*.json"))) != expected_count:
            continue
        if kind == "ape":
            for name, version, md5 in (("r-ape", "5.8_1", "2d74281b34cd277fe96f70b949e86c6e"), ("r-digest", "0.6.37", "e5f5fe851f7abbbc85a9be4d1fadb319")):
                records = list(meta.glob(name + "-*.json"))
                if len(records) != 1:
                    break
                record = json.loads(records[0].read_text())
                if record["version"] != version or record["md5"] != md5:
                    break
            else:
                lib_probe = subprocess.run([path, "--vanilla", "-e", "cat(find.package('ape'), '\\n', as.character(packageVersion('ape')), '\\n', as.character(packageVersion('digest')), '\\n')"], env=_r_env(), capture_output=True, text=True)
                vals = [x.strip() for x in lib_probe.stdout.splitlines() if x.strip()]
                if lib_probe.returncode == 0 and vals == [str(prefix / "lib/R/library/ape"), "5.8.1", "0.6.37"]:
                    return path
        elif not list(meta.glob("r-ape-*.json")):
            return path
    pytest.skip(f"No verified {kind} R 4.3.3 runtime; set {key} to a genuine pinned environment")


def test_explicit_runtime_selection_cannot_skip():
    for kind, key in (("ape", "TREEFAN_APE_RSCRIPT"), ("noape", "TREEFAN_NO_APE_RSCRIPT")):
        if os.environ.get(key):
            try:
                selected = _runtime(kind)
            except pytest.skip.Exception as exc:
                pytest.fail(f"Explicit {kind} runtime was skipped: {exc}")
            assert selected == os.environ[key]


def run_real(runtime, tmp_path, data, output_name="tree.png", cwd=None, title="Title", subtitle="Subtitle"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "input.tsv"
    source.write_text(data, encoding="utf-8")
    out = tmp_path / output_name
    working = cwd or tmp_path
    result = subprocess.run([runtime, "--vanilla", str(SCRIPT), str(source), str(out), title, subtitle], cwd=working, env=_r_env(), capture_output=True, text=True)
    return result, out


def assert_no_temp_or_stray(directory):
    assert not list(directory.glob(".treefan-*"))
    assert not (directory / "Rplots.pdf").exists()


def _instrument(runtime, tmp_path, data, function):
    """Run the real parsed script, tracing its render boundary without changing its expressions."""
    source = tmp_path / "oracle_input.tsv"
    source.write_text(data, encoding="utf-8")
    out = tmp_path / "oracle.png"
    tips = tmp_path / "tips.tsv"
    edges = tmp_path / "edges.tsv"
    nodes = tmp_path / "nodes.tsv"
    one = tmp_path / "one.tsv"
    draw = tmp_path / "draw.tsv"
    wrapper = tmp_path / "oracle_wrapper.R"
    def rquote(value):
        return json.dumps(str(value))
    tracer = (
        "write.table(data.frame(label=tree$tip.label,weight=tip_weights), file=" + rquote(tips) + ",sep='\\t',row.names=FALSE,quote=FALSE);"
        "write.table(tree$edge,file=" + rquote(edges) + ",sep='\\t',row.names=FALSE,col.names=FALSE,quote=FALSE);"
        "write.table(data.frame(label=tree$node.label,weight=internal_weights),file=" + rquote(nodes) + ",sep='\\t',row.names=FALSE,quote=FALSE)"
        if function == "plot_tree" else
        "write.table(data.frame(label=ids,weight=weights),file=" + rquote(one) + ",sep='\\t',row.names=FALSE,quote=FALSE)"
        if function == "plot_single_lineage" else
        "write.table(data.frame(label=label,weight=weight),file=" + rquote(one) + ",sep='\\t',row.names=FALSE,quote=FALSE)"
    )
    wrapper.write_text(
        "script <- " + rquote(SCRIPT) + "\n"
        "argv <- c(" + ",".join(map(rquote, (source, out, "Title", "Subtitle"))) + ")\n"
        "commandArgs <- function(trailingOnly=FALSE) if (trailingOnly) argv else paste0('--file=',script)\n"
        "expressions <- parse(file=script)\n"
        "for (i in seq_len(length(expressions)-1L)) eval(expressions[[i]], envir=.GlobalEnv)\n"
        "trace(" + rquote(function) + ", tracer=quote({" + tracer + "}), where=.GlobalEnv, print=FALSE)\n"
        "points <- function(x,y,...) {cat(paste('point',x,y,sep='\\t'), '\\n', file=" + rquote(draw) + ", append=TRUE,sep=''); graphics::points(x,y,...)}\n"
        "text <- function(x,y,labels,...) {cat(paste('text',labels,sep='\\t'), '\\n', file=" + rquote(draw) + ", append=TRUE,sep=''); graphics::text(x,y,labels,...)}\n"
        "eval(expressions[[length(expressions)]], envir=.GlobalEnv)\n"
    )
    result = subprocess.run([runtime, "--vanilla", str(wrapper)], cwd=tmp_path, env=_r_env(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return tips, edges, nodes, one, draw


def test_missing_ape_fails_closed_and_preserves_existing_outputs(tmp_path):
    runtime = _runtime("noape")
    result, out = run_real(runtime, tmp_path / "first", MULTI)
    assert result.returncode != 0
    assert "ape" in result.stderr.lower() and "required" in result.stderr.lower()
    assert not out.exists() and not out.with_suffix(".pdf").exists()
    assert_no_temp_or_stray(out.parent)
    rerun = tmp_path / "rerun"
    rerun.mkdir()
    (rerun / "tree.png").write_bytes(b"old png")
    (rerun / "tree.pdf").write_bytes(b"old pdf")
    result, out = run_real(runtime, rerun, MULTI)
    assert result.returncode != 0
    assert out.read_bytes() == b"old png" and out.with_suffix(".pdf").read_bytes() == b"old pdf"
    assert_no_temp_or_stray(rerun)


@pytest.mark.parametrize("input_data", [EMPTY, "", NO_ASSIGNMENT, "marker\tpath\tweight\nCOI\tRoot\t5\n"])
def test_no_data_without_ape_is_explicit_and_deterministic(tmp_path, input_data):
    runtime = _runtime("noape")
    outputs = []
    for index in range(2):
        result, out = run_real(runtime, tmp_path / str(index), input_data)
        assert result.returncode == 0, result.stderr
        png = out.read_bytes()
        pdf = out.with_suffix(".pdf").read_bytes()
        assert png_dimensions(png) == (2100, 1350)
        assert b"No data available" in pdf_text(pdf)
        assert_no_temp_or_stray(out.parent)
        outputs.append((png, pdf_without_dates(pdf)))
    assert outputs[0] == outputs[1]
    if input_data == EMPTY:
        reference = tmp_path / "reference"
        reference.mkdir()
        reference_png = reference / "expected.png"
        expr = "source(" + json.dumps(str(SCRIPT.parent / "plot_style.R")) + "); save_plot_placeholder(" + json.dumps(str(reference_png)) + ",'Title','No data available')"
        generated = subprocess.run([runtime, "--vanilla", "-e", expr], cwd=reference, env=_r_env(), capture_output=True, text=True)
        assert generated.returncode == 0, generated.stderr
        assert outputs[0][0] == reference_png.read_bytes()
        assert outputs[0][1] == pdf_without_dates(reference_png.with_suffix(".pdf").read_bytes())


def test_multi_ape_scientific_oracle_and_raw_determinism(tmp_path):
    runtime = _runtime("ape")
    pngs = []
    for index in range(2):
        run_dir = tmp_path / str(index)
        result, out = run_real(runtime, run_dir, MULTI)
        assert result.returncode == 0, result.stderr
        assert "render failed" not in result.stderr.lower()
        png = out.read_bytes()
        assert png_dimensions(png) == (3432, 3432)
        assert not out.with_suffix(".pdf").exists()
        assert_no_temp_or_stray(run_dir)
        pngs.append(png)
        if index == 0:
            time.sleep(1.1)
    assert pngs[0] == pngs[1]
    tips, edges, nodes, _, draw = _instrument(runtime, tmp_path / "0", MULTI, "plot_tree")
    oracle = independent_oracle(MULTI)
    seen_tips = {}
    for line in tips.read_text().splitlines()[1:]:
        label, weight = line.split("\t")
        seen_tips[label] = float(weight)
    assert seen_tips == oracle["tips"]
    seen_edges = {tuple(map(int, line.split("\t"))) for line in edges.read_text().splitlines()}
    assert seen_edges == oracle["edges"]
    seen_nodes = [line.split("\t") for line in nodes.read_text().splitlines()[1:]]
    assert len(seen_nodes) == len(oracle["internals"])
    assert all(float(weight) == 10 for _, weight in seen_nodes)
    drawn = draw.read_text().splitlines()
    assert any(line.endswith("\tAlpha one") for line in drawn)
    assert any(line.endswith("\tBeta two") for line in drawn)


@pytest.mark.parametrize("label,weight", [("Alpha one", 7), (("Étoile, space! (very long taxon label) " * 3).strip(), 11)])
def test_single_taxon_is_real_labeled_weighted_and_deterministic(tmp_path, label, weight):
    runtime = _runtime("ape")
    data = f"marker\tpath\tweight\nCOI\tRoot;Animalia;Genus;{label}\t{weight}\n"
    pngs = []
    for index in range(2):
        run_dir = tmp_path / str(index)
        result, out = run_real(runtime, run_dir, data)
        assert result.returncode == 0, result.stderr
        assert "render failed" not in result.stderr.lower()
        png = out.read_bytes()
        assert png_dimensions(png) == (3432, 3432)
        assert not out.with_suffix(".pdf").exists()
        assert_no_temp_or_stray(run_dir)
        pngs.append(png)
        if index == 0:
            time.sleep(1.1)
    assert pngs[0] == pngs[1]
    _, _, _, one, draw = _instrument(runtime, tmp_path / "0", data, "plot_single_taxon")
    assert one.read_text().splitlines()[1].split("\t") == [label, str(weight)]
    assert draw.read_text().splitlines() == ["point\t0\t0", f"text\t{label} ({weight})"]
    no_data, placeholder = run_real(_runtime("noape"), tmp_path / "empty", EMPTY)
    assert no_data.returncode == 0
    assert pngs[0] != placeholder.read_bytes()


@pytest.mark.parametrize("input_data", [MALFORMED, "marker\tpath\tweight\nCOI\tRoot;A;B\tabc\n", "marker\tpath\tweight\nCOI\t\t3\n"])
def test_malformed_fails_without_placeholder(tmp_path, input_data):
    runtime = _runtime("ape")
    result, out = run_real(runtime, tmp_path, input_data)
    assert result.returncode != 0
    assert "error" in result.stderr.lower()
    assert not out.exists() and not out.with_suffix(".pdf").exists()
    assert_no_temp_or_stray(tmp_path)


def test_different_cwd_output_and_preexisting_default_pdf(tmp_path):
    runtime = _runtime("ape")
    work = tmp_path / "cwd"
    output = tmp_path / "nested" / "report"
    work.mkdir()
    output.mkdir(parents=True)
    sentinel = b"pre-existing unrelated Rplots.pdf\n"
    (work / "Rplots.pdf").write_bytes(sentinel)
    result, out = run_real(runtime, output, MULTI, output_name="custom name.png", cwd=work)
    assert result.returncode == 0, result.stderr
    assert png_dimensions(out.read_bytes()) == (3432, 3432)
    assert (work / "Rplots.pdf").read_bytes() == sentinel
    assert not (output / "Rplots.pdf").exists()
    assert not list(work.glob("Rplots-*.pdf"))


def test_pdf_comparator_masks_dates_only():
    a = b"%PDF-1.4\n/CreationDate (D:20260101) /ModDate (D:20260101) /Title (x)"
    b = b"%PDF-1.4\n/CreationDate (D:20261231) /ModDate (D:20261231) /Title (x)"
    c = b.replace(b"/Title (x)", b"/Title (y)")
    assert pdf_without_dates(a) == pdf_without_dates(b)
    assert pdf_without_dates(a) != pdf_without_dates(c)
    assert sha(b"one") != sha(b"two")
    no_data = b"%PDF-1.4\nstream\n" + zlib.compress(b"(No data available) Tj") + b"endstream"
    failed = b"%PDF-1.4\nstream\n" + zlib.compress(b"(Tree render failed) Tj") + b"endstream"
    assert b"No data available" in pdf_text(no_data)
    assert b"No data available" not in pdf_text(failed)


def test_unrelated_open_device_remains_open(tmp_path):
    runtime = _runtime("ape")
    source = tmp_path / "input.tsv"
    source.write_text(MULTI)
    out = tmp_path / "tree.png"
    caller_pdf = tmp_path / "caller.pdf"
    wrapper = tmp_path / "device_wrapper.R"
    wrapper.write_text(
        f"script <- {json.dumps(str(SCRIPT))}\n"
        f"argv <- c({json.dumps(str(source))},{json.dumps(str(out))},'Title','Subtitle')\n"
        "commandArgs <- function(trailingOnly=FALSE) if (trailingOnly) argv else paste0('--file=',script)\n"
        f"grDevices::pdf({json.dumps(str(caller_pdf))})\n"
        "caller <- grDevices::dev.cur()\n"
        "expressions <- parse(file=script)\n"
        "for (i in expressions) eval(i, envir=.GlobalEnv)\n"
        "if (!(caller %in% grDevices::dev.list()) || grDevices::dev.cur() != caller) quit(save='no',status=3)\n"
        "grDevices::dev.off(which=caller)\n"
    )
    result = subprocess.run([runtime, "--vanilla", str(wrapper)], cwd=tmp_path, env=_r_env(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert out.exists() and caller_pdf.exists()
    assert not (tmp_path / "Rplots.pdf").exists()


def test_failed_render_preserves_outputs_and_removes_partial_temp(tmp_path):
    runtime = _runtime("ape")
    source = tmp_path / "input.tsv"
    source.write_text(MULTI)
    out = tmp_path / "tree.png"
    out.write_bytes(b"previous png")
    out.with_suffix(".pdf").write_bytes(b"previous pdf")
    wrapper = tmp_path / "failure_wrapper.R"
    wrapper.write_text(
        f"script <- {json.dumps(str(SCRIPT))}\n"
        f"argv <- c({json.dumps(str(source))},{json.dumps(str(out))},'Title','Subtitle')\n"
        "commandArgs <- function(trailingOnly=FALSE) if (trailingOnly) argv else paste0('--file=',script)\n"
        "expressions <- parse(file=script)\n"
        "for (i in seq_len(length(expressions)-1L)) eval(expressions[[i]], envir=.GlobalEnv)\n"
        "trace('draw_filtered_labels',tracer=quote(stop('injected render failure')),where=.GlobalEnv,print=FALSE)\n"
        "eval(expressions[[length(expressions)]], envir=.GlobalEnv)\n"
    )
    result = subprocess.run([runtime, "--vanilla", str(wrapper)], cwd=tmp_path, env=_r_env(), capture_output=True, text=True)
    assert result.returncode != 0
    assert "injected render failure" in result.stderr
    assert out.read_bytes() == b"previous png"
    assert out.with_suffix(".pdf").read_bytes() == b"previous pdf"
    assert not list(tmp_path.glob(".treefan-*"))
    assert not (tmp_path / "Rplots.pdf").exists()


def test_fake_namespace_is_rejected(tmp_path):
    runtime = _runtime("ape")
    prefix = Path(runtime).resolve().parents[1]
    package = tmp_path / "ape"
    (package / "R").mkdir(parents=True)
    (package / "DESCRIPTION").write_text("Package: ape\nVersion: 5.8.1\nTitle: Stub Ape\nDescription: Controlled fake namespace for TreeFan contract.\nLicense: MIT\nAuthor: Test Author\nMaintainer: Test Author <test@example.invalid>\n")
    (package / "NAMESPACE").write_text("export(reorder.phylo,plot.phylo)\n")
    (package / "R" / "stub.R").write_text("reorder.phylo <- function(x, ...) x\nplot.phylo <- function(x, ...) { plot.new(); list(x.lim=c(-1,1),y.lim=c(-1,1)) }\n")
    library = tmp_path / "library"
    library.mkdir()
    prebuilt = os.environ.get("TREEFAN_PREBUILT_FAKE_APE")
    if prebuilt:
        shutil.copytree(Path(prebuilt) / "ape", library / "ape")
    else:
        install = subprocess.run([str(prefix / "bin/R"), "CMD", "INSTALL", "--library=" + str(library), str(package)], env=_r_env(), cwd=tmp_path, capture_output=True, text=True)
        assert install.returncode == 0, install.stderr
    env = _r_env()
    env["R_LIBS"] = str(library)
    check = subprocess.run([runtime, "--vanilla", "-e", "cat(find.package('ape'))"], env=env, capture_output=True, text=True)
    assert check.returncode == 0 and check.stdout.strip() == str(library / "ape")
    source = tmp_path / "input.tsv"
    out = tmp_path / "tree.png"
    for data in (SINGLE, MULTI):
        source.write_text(data)
        result = subprocess.run([runtime, "--vanilla", str(SCRIPT), str(source), str(out)], env=env, cwd=tmp_path, capture_output=True, text=True)
        assert result.returncode != 0
        assert "ape" in result.stderr.lower() and "required" in result.stderr.lower()
        assert not out.exists() and not out.with_suffix(".pdf").exists()
        assert not (tmp_path / "Rplots.pdf").exists()


def test_pdf_publication_failure_rolls_back_existing_pair(tmp_path):
    runtime = _runtime("noape")
    source = tmp_path / "input.tsv"
    source.write_text(EMPTY)
    out = tmp_path / "tree.png"
    out.write_bytes(b"old png")
    out.with_suffix(".pdf").write_bytes(b"old pdf")
    wrapper = tmp_path / "rename_failure_wrapper.R"
    wrapper.write_text(
        f"script <- {json.dumps(str(SCRIPT))}\n"
        f"argv <- c({json.dumps(str(source))},{json.dumps(str(out))},'Title','Subtitle')\n"
        "commandArgs <- function(trailingOnly=FALSE) if (trailingOnly) argv else paste0('--file=',script)\n"
        "expressions <- parse(file=script)\n"
        "for (i in seq_len(length(expressions)-1L)) eval(expressions[[i]], envir=.GlobalEnv)\n"
        "file.rename <- function(from,to) { if (grepl('\\\\.png$',from) && to == out_path) FALSE else base::file.rename(from,to) }\n"
        "eval(expressions[[length(expressions)]], envir=.GlobalEnv)\n"
    )
    result = subprocess.run([runtime, "--vanilla", str(wrapper)], cwd=tmp_path, env=_r_env(), capture_output=True, text=True)
    assert result.returncode != 0
    assert out.read_bytes() == b"old png"
    assert out.with_suffix(".pdf").read_bytes() == b"old pdf"
    assert not list(tmp_path.glob(".treefan-*"))
    assert not (tmp_path / "Rplots.pdf").exists()


# The lineage oracle uses only direct producer-format rows, never propagated tree weights.
def direct_assignment_oracle(tsv):
    import csv
    from decimal import Decimal
    totals = {}
    for row in csv.DictReader(tsv.splitlines(), delimiter="\t"):
        weight = Decimal(row["weight"])
        if weight > 0 and row["path"] != "Root":
            totals[row["path"]] = totals.get(row["path"], Decimal(0)) + weight
    ordered = sorted(totals, key=lambda path: len(path.split(";")))
    assert all(child.startswith(parent + ";") for parent, child in zip(ordered, ordered[1:]))
    return [(path, totals[path]) for path in ordered]


def assert_direct_lineage(runtime, directory, data):
    from decimal import Decimal
    directory.mkdir(parents=True, exist_ok=True)
    expected = direct_assignment_oracle(data)
    function = "plot_single_taxon" if len(expected) == 1 else "plot_single_lineage"
    _, _, _, one, draw = _instrument(runtime, directory, data, function)
    seen = [line.split("\t") for line in one.read_text().splitlines()[1:]]
    expected_ids = [(path.rsplit(";", 1)[-1] if len(expected) == 1 else path, weight) for path, weight in expected]
    assert [(label, Decimal(weight)) for label, weight in seen] == expected_ids
    drawn = draw.read_text().splitlines()
    labels = [line[len("text\t"):] for line in drawn if line.startswith("text\t")]
    assert labels == [f"{path.rsplit(';', 1)[-1]} ({format(weight, 'f')})" for path, weight in expected]
    points = [line.split("\t")[1:] for line in drawn if line.startswith("point\t")]
    assert len(points) == len(expected)
    ys = [float(point[1]) for point in points]
    assert all(a > b for a, b in zip(ys, ys[1:]))
    png = (directory / "oracle.png").read_bytes()
    assert png_dimensions(png) == (3432, 3432)
    assert_no_temp_or_stray(directory)
    return png


def producer_lineage(tmp_path, duplicate=False):
    """Real OTU producer: best hit per OTU, size once per OTU, sum distinct OTUs per path."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    header = "OTU_id\tbarcode_by_homology\tperc_id\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\tread_id\n"
    genus = "genus1\tCOI\t99\tMetazoa\tArthropoda\tInsecta\tDiptera\tCulicidae\tAedes\tNA\tr1\n"
    species = "species1\tCOI\t99\tMetazoa\tArthropoda\tInsecta\tDiptera\tCulicidae\tAedes\tAedes aegypti\tr2\n"
    # Repeated hits do not multiply OTU sizes; a distinct OTU contributes its own size.
    extra = genus + species + "genus2\tCOI\t99\tMetazoa\tArthropoda\tInsecta\tDiptera\tCulicidae\tAedes\tNA\tr3\n" if duplicate else ""
    blast = tmp_path / "blast.tsv"
    sizes = tmp_path / "sizes.tsv"
    output = tmp_path / "producer.tsv"
    blast.write_text(header + genus + species + extra)
    sizes.write_text("otu_id\tsize\ngenus1\t5\nspecies1\t3\n" + ("genus2\t2\n" if duplicate else ""))
    result = subprocess.run(["perl", str(REPO / "bin/assignments_circle_tree_prep.pl"), "--mode", "otu", "--blast", str(blast), "--otu-sizes-round", str(sizes), "--out", str(output)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = output.read_text()
    assert [(path.rsplit(";", 1)[-1], int(weight)) for path, weight in direct_assignment_oracle(data)] == [("Aedes", 7 if duplicate else 5), ("Aedes aegypti", 3)]
    return data


@pytest.mark.parametrize("duplicate", [False, True])
def test_real_producer_nested_direct_assignments(tmp_path, duplicate):
    runtime = _runtime("ape")
    data = producer_lineage(tmp_path / "producer", duplicate)
    plain, out = run_real(runtime, tmp_path / "plain", data)
    assert plain.returncode == 0, plain.stderr
    assert out.read_bytes() == assert_direct_lineage(runtime, tmp_path / "trace", data)


LINEAGE_CASES = [
    "COI\tRoot;Z parent\t5\nCOI\tRoot;Z parent;M middle\t3\nCOI\tRoot;Z parent;M middle;A child\t2\n",
    "COI\tRoot;Z parent\t9007199254740991\nCOI\tRoot;Z parent;A child\t2147483648\n",
    "COI\tRoot;Z parent\t1.25\nCOI\tRoot;Z parent;A child\t0.5\n",
    "COI\tRoot;Étoile, parent!\t5\nCOI\tRoot;Étoile, parent!;" + ("Á long (child), label! " * 8).strip() + "\t3\n",
    "COI\tRoot;Z parent\t0\nCOI\tRoot;Z parent;M middle\t7\nCOI\tRoot;Z parent;M middle;A zero child\t0\n",
    "COI\tRoot;Z parent\t5\nCOI\tRoot;Z parent;A child\t3\nCOI\tRoot;Z parent;A child;Zero descendant\t0\n",
    "COI\tRoot;Z parent\t2\nCOI\tRoot;Z parent\t3\nCOI\tRoot;Z parent;A child\t3\n",
]


@pytest.mark.parametrize("rows", LINEAGE_CASES)
def test_direct_lineage_counts_order_and_fresh_process_determinism(tmp_path, rows):
    runtime = _runtime("ape")
    data = "marker\tpath\tweight\n" + rows
    reversed_data = "marker\tpath\tweight\n" + "\n".join(reversed(rows.strip().splitlines())) + "\n"
    first = assert_direct_lineage(runtime, tmp_path / "first", data)
    assert first == assert_direct_lineage(runtime, tmp_path / "fresh", data)
    assert first == assert_direct_lineage(runtime, tmp_path / "reversed", reversed_data)
    plain, out = run_real(runtime, tmp_path / "plain", data)
    assert plain.returncode == 0, plain.stderr
    assert first == out.read_bytes()


def test_lineage_changed_direct_counts_change_output(tmp_path):
    runtime = _runtime("ape")
    data = "marker\tpath\tweight\n" + LINEAGE_CASES[0]
    original = assert_direct_lineage(runtime, tmp_path / "first", data)
    changed = assert_direct_lineage(runtime, tmp_path / "changed", data.replace("\t5\n", "\t6\n"))
    assert original != changed


@pytest.mark.parametrize("weight", ["NA", "NaN", "Inf", "-Inf", "1e999"])
def test_nonfinite_nested_input_fails_closed(tmp_path, weight):
    runtime = _runtime("ape")
    data = "marker\tpath\tweight\nCOI\tRoot;A\t5\nCOI\tRoot;A;B\t" + weight + "\n"
    result, out = run_real(runtime, tmp_path, data)
    assert result.returncode != 0 and "error" in result.stderr.lower()
    assert not out.exists() and not out.with_suffix(".pdf").exists()
    assert_no_temp_or_stray(tmp_path)


@pytest.mark.parametrize("stage", ["construct", "draw", "close", "publish"])
@pytest.mark.parametrize("preexisting", [False, True])
def test_lineage_failures_are_fatal_and_preserve_artifacts(tmp_path, stage, preexisting):
    runtime = _runtime("ape")
    source = tmp_path / "input.tsv"
    source.write_text("marker\tpath\tweight\n" + LINEAGE_CASES[0])
    out = tmp_path / "tree.png"
    pdf = out.with_suffix(".pdf")
    sentinel = tmp_path / "Rplots.pdf"
    sentinel.write_bytes(b"caller Rplots.pdf")
    before = sentinel.stat()
    if preexisting:
        out.write_bytes(b"old png")
        pdf.write_bytes(b"old pdf")
    injections = {
        "construct": "build_phylo <- function(...) stop('injected construct failure')",
        "draw": "text <- function(...) stop('injected draw failure')",
        "close": "trace('dev.off',exit=quote(stop('injected close failure')),where=asNamespace('grDevices'),print=FALSE)",
        "publish": "file.rename <- function(from,to) {if (to == out_path) FALSE else base::file.rename(from,to)}",
    }
    wrapper = tmp_path / "fail.R"
    wrapper.write_text(
        f"script <- {json.dumps(str(SCRIPT))}\n"
        f"argv <- c({json.dumps(str(source))},{json.dumps(str(out))},'Title','Subtitle')\n"
        "commandArgs <- function(trailingOnly=FALSE) if (trailingOnly) argv else paste0('--file=',script)\n"
        "expressions <- parse(file=script)\n"
        "for (i in seq_len(length(expressions)-1L)) eval(expressions[[i]], envir=.GlobalEnv)\n"
        + injections[stage] + "\n"
        "eval(expressions[[length(expressions)]], envir=.GlobalEnv)\n"
    )
    result = subprocess.run([runtime, "--vanilla", str(wrapper)], cwd=tmp_path, env=_r_env(), capture_output=True, text=True)
    assert result.returncode != 0 and "ERROR: TreeFan" in result.stderr
    if preexisting:
        assert out.read_bytes() == b"old png" and pdf.read_bytes() == b"old pdf"
    else:
        assert not out.exists() and not pdf.exists()
    assert not list(tmp_path.glob(".treefan-*"))
    after = sentinel.stat()
    assert sentinel.read_bytes() == b"caller Rplots.pdf"
    assert (before.st_ino, before.st_size, before.st_mtime_ns) == (after.st_ino, after.st_size, after.st_mtime_ns)
