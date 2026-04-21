import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "fastq_add_annotations2ids.pl"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_fastq_add_annotations_normalizes_no_adapter(tmp_path: Path) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text(
        "read1|COI|hac|barcode=|adapter=no_adapter_1\n",
        encoding="utf-8",
    )
    fastq = tmp_path / "reads.fastq"
    fastq.write_text(
        "@read1\n"
        "ACGT\n"
        "+\n"
        "####\n",
        encoding="utf-8",
    )
    cmd = ["perl", str(SCRIPT), str(ids), str(fastq)]
    result = subprocess.run(cmd, check=True, cwd=tmp_path, capture_output=True, text=True)
    out = result.stdout
    assert "adapter=no_adapter\n" in out


def test_fastq_add_annotations_normalizes_mixed_case_no_adapter(tmp_path: Path) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text(
        "read1|COI|hac|barcode=|adapter=NO_ADAPTER_1\n",
        encoding="utf-8",
    )
    fastq = tmp_path / "reads.fastq"
    fastq.write_text(
        "@read1\n"
        "ACGT\n"
        "+\n"
        "####\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["perl", str(SCRIPT), str(ids), str(fastq)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert "adapter=no_adapter\n" in result.stdout


def test_fastq_add_annotations_keeps_non_numeric_no_adapter_suffix_as_barcoded(tmp_path: Path) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text(
        "read1|COI|hac|barcode=bc1|adapter=no_adapter_1a\n",
        encoding="utf-8",
    )
    fastq = tmp_path / "reads.fastq"
    fastq.write_text(
        "@read1\n"
        "ACGT\n"
        "+\n"
        "####\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["perl", str(SCRIPT), str(ids), str(fastq)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert "adapter=no_adapter_1a\n" in result.stdout


def test_fastq_add_annotations_normalizes_barcoded_suffix_to_base_sample(tmp_path: Path) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text(
        "read1|COI|hac|barcode=bc1|adapter=sample_A_2\n",
        encoding="utf-8",
    )
    fastq = tmp_path / "reads.fastq"
    fastq.write_text(
        "@read1\n"
        "ACGT\n"
        "+\n"
        "####\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["perl", str(SCRIPT), str(ids), str(fastq)],
        check=True,
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert "adapter=sample_A\n" in result.stdout
