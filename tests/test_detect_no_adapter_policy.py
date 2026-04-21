import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "detect_no_adapter_policy.sh"
PERL_HELPER = REPO_ROOT / "bin" / "detect_no_adapter_policy.pl"


def _run(tmp_path: Path, rows: str) -> subprocess.CompletedProcess[str]:
    blast = tmp_path / "blast_report_annotated_otu_full.txt"
    blast.write_text(rows, encoding="utf-8")
    return subprocess.run(
        ["bash", str(SCRIPT), str(blast)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_input_path_fails(tmp_path: Path) -> None:
    missing = tmp_path / "missing.tsv"
    result = subprocess.run(
        ["bash", str(SCRIPT), str(missing)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ERROR: blast report not found:" in result.stderr


def test_directory_input_fails(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "ERROR: blast report is not a regular file:" in result.stderr


def test_empty_existing_file_emits_zero_policy(tmp_path: Path) -> None:
    empty = tmp_path / "blast_report_annotated_otu_full.txt"
    empty.write_text("", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(SCRIPT), str(empty)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "observed_no_adapter\t0" in result.stdout
    assert "observed_non_no_adapter\t0" in result.stdout
    assert "separate_no_adapter\t0" in result.stdout


def test_no_adapter_only_rows_do_not_enable_split(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "observed_no_adapter\t1" in result.stdout
    assert "observed_non_no_adapter\t0" in result.stdout
    assert "separate_no_adapter\t0" in result.stdout


def test_barcoded_rows_only_do_not_enable_split(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=barcode_1|adapter=sample_A_2|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "observed_no_adapter\t0\n"
        "observed_non_no_adapter\t1\n"
        "separate_no_adapter\t0\n"
    )


def test_mixed_case_no_adapter_is_treated_as_no_adapter(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=no_adapter_1|adapter=NO_ADAPTER_1|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "observed_no_adapter\t1" in result.stdout
    assert "observed_non_no_adapter\t0" in result.stdout
    assert "separate_no_adapter\t0" in result.stdout


def test_malformed_empty_adapter_is_ignored(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=barcode_1|adapter=|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "observed_no_adapter\t0" in result.stdout
    assert "observed_non_no_adapter\t0" in result.stdout
    assert "separate_no_adapter\t0" in result.stdout


def test_non_numeric_no_adapter_suffix_is_not_treated_as_no_adapter(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=barcode_1|adapter=no_adapter_1a|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "observed_no_adapter\t0" in result.stdout
    assert "observed_non_no_adapter\t1" in result.stdout
    assert "separate_no_adapter\t0" in result.stdout


def test_no_adapter_like_barcoded_value_preserves_normalization_order(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=barcode_1|adapter=no_adapter_1_2|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "observed_no_adapter\t0\n"
        "observed_non_no_adapter\t1\n"
        "separate_no_adapter\t0\n"
    )


def test_mixed_valid_rows_enable_split(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\tNA\n"
        "read2|COI|sup|barcode=barcode_1|adapter=sample_A_2|OTUB_2-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "observed_no_adapter\t1" in result.stdout
    assert "observed_non_no_adapter\t1" in result.stdout
    assert "separate_no_adapter\t1" in result.stdout


def test_missing_adapter_token_is_ignored(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "read1|COI|sup|barcode=barcode_1|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "observed_no_adapter\t0\n"
        "observed_non_no_adapter\t0\n"
        "separate_no_adapter\t0\n"
    )


def test_large_fixture_short_circuits_after_mixed_observation(tmp_path: Path) -> None:
    rows = (
        "read1|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\tNA\n"
        "read2|COI|sup|barcode=barcode_1|adapter=sample_A_2|OTUB_2-COI\tNA\n"
        + ("readX|COI|sup|barcode=barcode_1|adapter=sample_B_4|OTUB_3-COI\tNA\n" * 200000)
    )
    blast = tmp_path / "blast_report_annotated_otu_full.txt"
    blast.write_text(rows, encoding="utf-8")

    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(SCRIPT), str(blast)],
        capture_output=True,
        text=True,
        check=False,
        timeout=2.0,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 2.0
    assert result.stdout == (
        "observed_no_adapter\t1\n"
        "observed_non_no_adapter\t1\n"
        "separate_no_adapter\t1\n"
    )


def test_script_uses_single_perl_helper_without_line_by_line_shell_scan() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'perl "$script_dir/detect_no_adapter_policy.pl" "$blast_report"' in text
    assert 'while IFS= read -r line; do' not in text
    assert 'source "$script_dir/lib/adapter_utils.sh"' not in text
    assert PERL_HELPER.exists()
