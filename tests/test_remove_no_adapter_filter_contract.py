import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FASTA_SCRIPT = REPO_ROOT / "bin" / "remove_no_adapter_fasta.pl"
SUP_FASTA_SCRIPT = REPO_ROOT / "bin" / "filter_sup_non_no_adapter_fasta.pl"


def test_remove_no_adapter_fasta_filters_matching_records() -> None:
    fasta = (
        ">r1|COI|sup|barcode=bc1|adapter=no_adapter\n"
        "ACGT\n"
        ">r2|COI|sup|barcode=bc1|adapter=sample_1\n"
        "TGCA\n"
    )
    result = subprocess.run(
        ["perl", str(FASTA_SCRIPT)],
        input=fasta,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ">r2|COI|sup|barcode=bc1|adapter=sample_1\nTGCA\n"


def test_remove_no_adapter_fasta_does_not_filter_unrelated_no_adapter_substrings() -> None:
    fasta = (
        ">r1|COI|sup|barcode=bc1|note=no_adapter_like|adapter=sample_1\n"
        "ACGT\n"
        ">r2|COI|sup|barcode=bc1|adapter=no_adapter_2\n"
        "TGCA\n"
    )
    result = subprocess.run(
        ["perl", str(FASTA_SCRIPT)],
        input=fasta,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ">r1|COI|sup|barcode=bc1|note=no_adapter_like|adapter=sample_1\nACGT\n"


def test_filter_sup_non_no_adapter_fasta_preserves_wrapped_sup_records() -> None:
    fasta = (
        ">r1|COI|sup|barcode=bc1|adapter=sample_1\n"
        "ACGT\n"
        "TGCA\n"
        ">r2|COI|sup|barcode=bc1|adapter=no_adapter_2\n"
        "GGGG\n"
        "CCCC\n"
        ">r3|COI|hac|barcode=bc1|adapter=sample_2\n"
        "TTTT\n"
    )
    result = subprocess.run(
        ["perl", str(SUP_FASTA_SCRIPT)],
        input=fasta,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ">r1|COI|sup|barcode=bc1|adapter=sample_1\nACGT\nTGCA\n"


def test_filter_sup_non_no_adapter_fasta_keeps_protected_no_adapter_sup_records(tmp_path: Path) -> None:
    fasta = (
        ">r1|ITS2|sup|barcode=bc1|adapter=no_adapter\n"
        "ACGT\n"
        ">r2|COI|sup|barcode=bc1|adapter=sample_1\n"
        "TGCA\n"
        ">r3|COI|sup|barcode=bc1|adapter=no_adapter\n"
        "GGGG\n"
    )
    keep = tmp_path / "keep_ids.list"
    keep.write_text("r1\n", encoding="utf-8")

    result = subprocess.run(
        ["perl", str(SUP_FASTA_SCRIPT), str(keep)],
        input=fasta,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        ">r1|ITS2|sup|barcode=bc1|adapter=no_adapter\n"
        "ACGT\n"
        ">r2|COI|sup|barcode=bc1|adapter=sample_1\n"
        "TGCA\n"
    )


def test_filter_sup_non_no_adapter_fasta_keeps_protected_non_sup_records(tmp_path: Path) -> None:
    fasta = (
        ">r1|ITS2|hac|barcode=bc1|adapter=no_adapter\n"
        "ACGT\n"
        ">r2|COI|sup|barcode=bc1|adapter=sample_1\n"
        "TGCA\n"
        ">r3|COI|hac_fixed|barcode=bc1|adapter=sample_2\n"
        "GGGG\n"
    )
    keep = tmp_path / "keep_ids.list"
    keep.write_text("r1\nr3\n", encoding="utf-8")

    result = subprocess.run(
        ["perl", str(SUP_FASTA_SCRIPT), str(keep)],
        input=fasta,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        ">r1|ITS2|hac|barcode=bc1|adapter=no_adapter\n"
        "ACGT\n"
        ">r2|COI|sup|barcode=bc1|adapter=sample_1\n"
        "TGCA\n"
        ">r3|COI|hac_fixed|barcode=bc1|adapter=sample_2\n"
        "GGGG\n"
    )
