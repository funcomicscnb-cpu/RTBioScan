"""Tests for bin/report_sample_read_counts_plots.sh --sig-dir caching."""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "report_sample_read_counts_plots.sh"
PLOT_SIG = REPO_ROOT / "bin" / "plot_sig.sh"


def _make_summary(path: Path, sample_label: str = "SampleA", read_count: int = 100) -> None:
    path.write_text(
        "read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tsample_name\n"
        f"{read_count}\tCOI\thac\t{sample_label}\tplatform\tsampling\tsubsample\t1\t{sample_label}\n",
        encoding="utf-8",
    )


def _make_mock_r(script_path: Path, log_path: Path) -> None:
    """Create a mock Rscript that touches expected output files and logs invocations."""
    script_path.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # Mock Rscript: $1=script, $2=summary, $3=sample_label, $4=output_prefix
        echo "invoked" >> {log_path}
        prefix="$4"
        touch "${{prefix}}_reads_per_barcode.png"
        touch "${{prefix}}_reads_per_barcode.pdf"
        touch "${{prefix}}_reads_per_sample.png"
        touch "${{prefix}}_reads_per_sample.pdf"
        touch "${{prefix}}_reads_per_sample_log.png"
        touch "${{prefix}}_reads_per_sample_log.pdf"
        """),
        encoding="utf-8",
    )
    script_path.chmod(0o755)


def _run(args, env=None):
    return subprocess.run(
        ["bash", str(SCRIPT)] + args,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture()
def env_with_mock_rscript(tmp_path):
    """Return env dict with Rscript overridden by a mock that creates output files."""
    mock_dir = tmp_path / "_mock_bin"
    mock_dir.mkdir()
    log = tmp_path / "rscript_invocations.log"
    mock_r = mock_dir / "Rscript"
    _make_mock_r(mock_r, log)
    env = {**os.environ, "PATH": f"{mock_dir}:{os.environ['PATH']}"}
    return env, log


def test_first_run_creates_artifacts_and_sig(tmp_path, env_with_mock_rscript):
    env, log = env_with_mock_rscript
    summary = tmp_path / "summary.tsv"
    _make_summary(summary)
    out_dir = tmp_path / "samples"
    sig_dir = tmp_path / "sigs"

    rc = _run([
        "--summary", str(summary),
        "--out-dir", str(out_dir),
        "--max", "10",
        "--sig-dir", str(sig_dir),
    ], env=env)
    assert rc.returncode == 0, rc.stderr

    # R was invoked once
    assert log.exists()
    assert log.read_text().strip().count("invoked") == 1

    # At least one sig file written
    sig_files = list(sig_dir.glob("*.sig"))
    assert sig_files, "Expected sig file(s) in sig_dir"


def test_second_run_unchanged_skips_r(tmp_path, env_with_mock_rscript):
    env, log = env_with_mock_rscript
    summary = tmp_path / "summary.tsv"
    _make_summary(summary)
    out_dir = tmp_path / "samples"
    sig_dir = tmp_path / "sigs"

    args = [
        "--summary", str(summary),
        "--out-dir", str(out_dir),
        "--max", "10",
        "--sig-dir", str(sig_dir),
    ]

    rc1 = _run(args, env=env)
    assert rc1.returncode == 0, rc1.stderr
    invocations_after_first = log.read_text().strip().count("invoked")
    assert invocations_after_first == 1

    # Second run with same inputs: R should be skipped
    rc2 = _run(args, env=env)
    assert rc2.returncode == 0, rc2.stderr
    assert log.read_text().strip().count("invoked") == invocations_after_first, \
        "R was re-invoked on second run despite unchanged inputs"


def test_changed_summary_triggers_r(tmp_path, env_with_mock_rscript):
    env, log = env_with_mock_rscript
    summary = tmp_path / "summary.tsv"
    _make_summary(summary, read_count=100)
    out_dir = tmp_path / "samples"
    sig_dir = tmp_path / "sigs"

    args = [
        "--summary", str(summary),
        "--out-dir", str(out_dir),
        "--max", "10",
        "--sig-dir", str(sig_dir),
    ]

    rc1 = _run(args, env=env)
    assert rc1.returncode == 0, rc1.stderr
    inv1 = log.read_text().strip().count("invoked")

    # Change the summary data
    _make_summary(summary, read_count=200)

    rc2 = _run(args, env=env)
    assert rc2.returncode == 0, rc2.stderr
    inv2 = log.read_text().strip().count("invoked")
    assert inv2 > inv1, "R was not re-invoked after summary changed"


def test_missing_pdf_triggers_r(tmp_path, env_with_mock_rscript):
    env, log = env_with_mock_rscript
    summary = tmp_path / "summary.tsv"
    _make_summary(summary)
    out_dir = tmp_path / "samples"
    sig_dir = tmp_path / "sigs"

    args = [
        "--summary", str(summary),
        "--out-dir", str(out_dir),
        "--max", "10",
        "--sig-dir", str(sig_dir),
    ]

    rc1 = _run(args, env=env)
    assert rc1.returncode == 0, rc1.stderr
    inv1 = log.read_text().strip().count("invoked")

    # Delete one of the PDF artifacts
    pdfs = list(out_dir.rglob("*.pdf"))
    if pdfs:
        pdfs[0].unlink()
        rc2 = _run(args, env=env)
        assert rc2.returncode == 0, rc2.stderr
        inv2 = log.read_text().strip().count("invoked")
        assert inv2 > inv1, "R was not re-invoked after PDF deleted"


def test_no_sig_dir_always_invokes_r(tmp_path, env_with_mock_rscript):
    env, log = env_with_mock_rscript
    summary = tmp_path / "summary.tsv"
    _make_summary(summary)
    out_dir = tmp_path / "samples"

    args = [
        "--summary", str(summary),
        "--out-dir", str(out_dir),
        "--max", "10",
    ]

    rc1 = _run(args, env=env)
    assert rc1.returncode == 0, rc1.stderr
    inv1 = log.read_text().strip().count("invoked") if log.exists() else 0
    assert inv1 >= 1

    # Second run with no --sig-dir: R should always be invoked
    rc2 = _run(args, env=env)
    assert rc2.returncode == 0, rc2.stderr
    inv2 = log.read_text().strip().count("invoked")
    assert inv2 > inv1, "R was not re-invoked on second run without --sig-dir"


def test_normalized_sample_name_groups_marker_split_rows(tmp_path, env_with_mock_rscript):
    env, log = env_with_mock_rscript
    summary = tmp_path / "summary.tsv"
    summary.write_text(
        "\n".join([
            "read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tsample_name",
            "100\tCOI\thac\tYT.C.spiked_COI\tplatform\tsampling\tsubsample\t1\tYT.C.spiked",
            "50\tITS2\thac\tYT.C.spiked_ITS2\tplatform\tsampling\tsubsample\t1\tYT.C.spiked",
        ]) + "\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "samples"
    sig_dir = tmp_path / "sigs"

    rc = _run([
        "--summary", str(summary),
        "--out-dir", str(out_dir),
        "--max", "10",
        "--sig-dir", str(sig_dir),
    ], env=env)
    assert rc.returncode == 0, rc.stderr
    assert log.read_text().strip().count("invoked") == 1
    sample_dirs = [p for p in out_dir.iterdir() if p.is_dir()]
    assert len(sample_dirs) == 1


def test_legacy_summary_without_sample_name_fails_explicitly(tmp_path, env_with_mock_rscript):
    env, _log = env_with_mock_rscript
    summary = tmp_path / "summary.tsv"
    summary.write_text(
        "read_count\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n"
        "100\tCOI\thac\tSampleA\tplatform\tsampling\tsubsample\t1\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "samples"

    rc = _run([
        "--summary", str(summary),
        "--out-dir", str(out_dir),
        "--max", "10",
    ], env=env)
    assert rc.returncode != 0
    assert "missing required sample_name column" in rc.stderr
    assert "regenerate the summary with the corrected schema" in rc.stderr
