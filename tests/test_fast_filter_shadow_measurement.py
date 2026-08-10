import csv
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
AGGREGATOR = REPO_ROOT / "bin" / "summarize_fast_filter_shadow.py"
PRODUCER = REPO_ROOT / "bin" / "fast_filter_shadow.py"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "fast_filter_shadow_measurement"


def run_aggregator(input_path, output_prefix, *extra_args):
    return subprocess.run(
        [
            sys.executable,
            str(AGGREGATOR),
            "--input",
            str(input_path),
            "--output-prefix",
            str(output_prefix),
            *extra_args,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def output_paths(prefix):
    return [
        prefix.with_name(prefix.name + "_aggregate.tsv"),
        prefix.with_name(prefix.name + "_rounds.tsv"),
        prefix.with_name(prefix.name + "_inputs.tsv"),
        prefix.with_name(prefix.name + ".json"),
    ]


def test_aggregates_provenance_fixtures(tmp_path):
    prefix = tmp_path / "measurement"
    completed = run_aggregator(FIXTURES, prefix)
    assert completed.returncode == 0, completed.stderr

    report = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
    assert report["schema_version"] == "fast-filter-shadow-measurement-v1"
    assert (report["rounds"], report["reads"], report["bases"]) == (3, 14, 6181)
    expected_metrics = {
        "aligned": (13, 6137),
        "unaligned": (1, 44),
        "current_retained": (4, 1540),
        "current_excluded": (10, 4641),
        "target_only": (1, 229),
        "offtarget_only": (2, 1064),
        "target_leads": (2, 1307),
        "offtarget_leads": (7, 3533),
        "tie": (1, 4),
        "no_alignment": (1, 44),
    }
    assert {
        metric: (report["metrics"][metric]["reads"], report["metrics"][metric]["bases"])
        for metric in expected_metrics
    } == expected_metrics
    assert report["interpretation_boundary"].startswith("Routing-policy evidence only")

    inputs = {row["round_barcode"]: row for row in report["inputs"]}
    assert inputs["qualified-last1542-fixture"]["detail_sha256"] == (
        "ebe707b065d12045759719db9259f9976a8999a641ea7ca95b34aa93a24cd271"
    )
    assert inputs["rtbioscan-shadow-success-cpu"]["summary_sha256"] == (
        "8be8f5036b6752e28be484dacd3d94c8269f812998b4738e11d39951be4be230"
    )

    with output_paths(prefix)[0].open(newline="", encoding="utf-8") as handle:
        buckets = list(csv.DictReader(handle, delimiter="\t"))
    tie = next(row for row in buckets if row["competition_status"] == "tie")
    assert (tie["current_marker"], tie["reads"], tie["bases"]) == ("COI", "1", "4")
    assert (tie["margin_min"], tie["margin_max"], tie["margin_mean"]) == (
        "0",
        "0",
        "0",
    )


def test_synthetic_tie_golden_regenerates_byte_for_byte(tmp_path):
    shadow = tmp_path / "synthetic_tie_fast_filter_shadow.tsv"
    summary = tmp_path / "synthetic_tie_fast_filter_shadow_summary.tsv"
    completed = subprocess.run(
        [
            sys.executable,
            str(PRODUCER),
            "--fasta",
            str(FIXTURES / "synthetic_tie.fa"),
            "--legacy-out",
            str(tmp_path / "legacy.tsv"),
            "--shadow-out",
            str(shadow),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|Viridiplantae",
            "--round-barcode",
            "synthetic-tie",
        ],
        input=(FIXTURES / "synthetic_tie.blasttab").read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert shadow.read_bytes() == (
        FIXTURES / "synthetic_tie_fast_filter_shadow.tsv"
    ).read_bytes()
    assert summary.read_bytes() == (
        FIXTURES / "synthetic_tie_fast_filter_shadow_summary.tsv"
    ).read_bytes()


def test_nextflow_no_alignment_golden_regenerates_from_captured_fasta(tmp_path):
    shadow = tmp_path / "nextflow_no_alignment_fast_filter_shadow.tsv"
    summary = tmp_path / "nextflow_no_alignment_fast_filter_shadow_summary.tsv"
    completed = subprocess.run(
        [
            sys.executable,
            str(PRODUCER),
            "--fasta",
            str(FIXTURES / "nextflow_no_alignment.fa"),
            "--legacy-out",
            str(tmp_path / "legacy.tsv"),
            "--shadow-out",
            str(shadow),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|Viridiplantae",
            "--round-barcode",
            "rtbioscan-shadow-success-cpu",
        ],
        input="",
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert shadow.read_bytes() == (
        FIXTURES / "nextflow_no_alignment_fast_filter_shadow.tsv"
    ).read_bytes()
    assert summary.read_bytes() == (
        FIXTURES / "nextflow_no_alignment_fast_filter_shadow_summary.tsv"
    ).read_bytes()


def test_rejects_summary_mismatch_without_writing_outputs(tmp_path):
    detail = tmp_path / "bad_fast_filter_shadow.tsv"
    summary = tmp_path / "bad_fast_filter_shadow_summary.tsv"
    shutil.copyfile(FIXTURES / "last1542_aligned_fast_filter_shadow.tsv", detail)
    summary_text = (
        FIXTURES / "last1542_aligned_fast_filter_shadow_summary.tsv"
    ).read_text(encoding="utf-8")
    summary.write_text(
        summary_text.replace("\tall_input\t12\t6133", "\tall_input\t11\t6133"),
        encoding="utf-8",
    )

    prefix = tmp_path / "measurement"
    completed = run_aggregator(detail, prefix)
    assert completed.returncode != 0
    assert "summary mismatch for all_input" in completed.stderr
    assert not any(path.exists() for path in output_paths(prefix))


def test_rejects_duplicate_rounds_across_inputs(tmp_path):
    for name in ("one", "two"):
        shutil.copyfile(
            FIXTURES / "synthetic_tie_fast_filter_shadow.tsv",
            tmp_path / f"{name}_fast_filter_shadow.tsv",
        )
        shutil.copyfile(
            FIXTURES / "synthetic_tie_fast_filter_shadow_summary.tsv",
            tmp_path / f"{name}_fast_filter_shadow_summary.tsv",
        )

    prefix = tmp_path / "measurement"
    completed = run_aggregator(tmp_path, prefix)
    assert completed.returncode != 0
    assert "duplicate round_barcode across inputs: synthetic-tie" in completed.stderr
    assert not any(path.exists() for path in output_paths(prefix))


def test_accepts_producer_generated_zero_read_round(tmp_path):
    fasta = tmp_path / "empty.fa"
    detail = tmp_path / "empty_fast_filter_shadow.tsv"
    summary = tmp_path / "empty_fast_filter_shadow_summary.tsv"
    fasta.write_text("", encoding="utf-8")
    produced = subprocess.run(
        [
            sys.executable,
            str(PRODUCER),
            "--fasta",
            str(fasta),
            "--legacy-out",
            str(tmp_path / "legacy.tsv"),
            "--shadow-out",
            str(detail),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|Viridiplantae",
            "--round-barcode",
            "empty-round",
            "--empty",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert produced.returncode == 0, produced.stderr

    prefix = tmp_path / "measurement"
    completed = run_aggregator(detail, prefix)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
    assert (report["rounds"], report["reads"], report["bases"]) == (1, 0, 0)
    assert report["inputs"][0]["round_barcode"] == "empty-round"


def test_accepts_producer_float_margin_format(tmp_path):
    fasta = tmp_path / "fractional.fa"
    detail = tmp_path / "fractional_fast_filter_shadow.tsv"
    summary = tmp_path / "fractional_fast_filter_shadow_summary.tsv"
    fasta.write_text(">read1\nACGT\n", encoding="utf-8")
    produced = subprocess.run(
        [
            sys.executable,
            str(PRODUCER),
            "--fasta",
            str(fasta),
            "--legacy-out",
            str(tmp_path / "legacy.tsv"),
            "--shadow-out",
            str(detail),
            "--summary-out",
            str(summary),
            "--targets",
            "COI|ITS2",
            "--target-taxa",
            "Metazoa|Viridiplantae",
            "--round-barcode",
            "fractional-round",
        ],
        input=(
            "read1\tCOI|Metazoa|target\t99\t4\t0\t0\t1\t4\t1\t4\t1e-4\t123.456789\n"
            "read1\tCOI|Bacteria|offtarget\t98\t4\t0\t0\t1\t4\t1\t4\t1e-3\t100.000001\n"
        ),
        text=True,
        capture_output=True,
        check=False,
    )
    assert produced.returncode == 0, produced.stderr

    prefix = tmp_path / "measurement"
    completed = run_aggregator(detail, prefix)
    assert completed.returncode == 0, completed.stderr
    with output_paths(prefix)[0].open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle, delimiter="\t"))
    assert row["margin_mean"] == "23.4568"


def test_existing_outputs_require_explicit_force(tmp_path):
    prefix = tmp_path / "measurement"
    first = run_aggregator(
        FIXTURES / "nextflow_no_alignment_fast_filter_shadow.tsv", prefix
    )
    assert first.returncode == 0, first.stderr
    original = prefix.with_suffix(".json").read_bytes()

    second = run_aggregator(
        FIXTURES / "nextflow_no_alignment_fast_filter_shadow.tsv", prefix
    )
    assert second.returncode != 0
    assert "measurement output already exists (use --force)" in second.stderr
    assert prefix.with_suffix(".json").read_bytes() == original

    forced = run_aggregator(
        FIXTURES / "nextflow_no_alignment_fast_filter_shadow.tsv", prefix, "--force"
    )
    assert forced.returncode == 0, forced.stderr
    assert prefix.with_suffix(".json").read_bytes() == original
