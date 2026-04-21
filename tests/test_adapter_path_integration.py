import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FASTQ_ANNOTATE = REPO_ROOT / "bin" / "fastq_add_annotations2ids.pl"
DETECT_POLICY = REPO_ROOT / "bin" / "detect_no_adapter_policy.sh"
FILTER_ROWS = REPO_ROOT / "bin" / "filter_blast_rows_by_adapter_class.sh"


def _write_fastq(path: Path, read_ids: list[str]) -> None:
    chunks = []
    for read_id in read_ids:
        chunks.append(f"@{read_id} runid=test\nACGT\n+\n####\n")
    path.write_text("".join(chunks), encoding="utf-8")


def _annotated_read_ids(fastq_output: str) -> list[str]:
    annotated = []
    for line in fastq_output.splitlines():
        if not line.startswith("@"):
            continue
        annotated.append(line[1:].split()[0])
    return annotated


def test_adapter_token_lifecycle_across_annotation_and_policy(tmp_path: Path) -> None:
    ids = tmp_path / "ids.txt"
    ids.write_text(
        "read_no_lower|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\n"
        "read_no_upper|COI|sup|barcode=no_adapter_1|adapter=NO_ADAPTER_1\n"
        "read_weird|COI|sup|barcode=bc1|adapter=no_adapter_1a\n"
        "read_empty|COI|sup|barcode=bc1|adapter=\n"
        "read_sample|COI|sup|barcode=bc1|adapter=sample_A_2\n",
        encoding="utf-8",
    )
    fastq = tmp_path / "reads.fastq"
    _write_fastq(
        fastq,
        [
            "read_no_lower",
            "read_no_upper",
            "read_weird",
            "read_empty",
            "read_sample",
        ],
    )

    annotate = subprocess.run(
        ["perl", str(FASTQ_ANNOTATE), str(ids), str(fastq)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert annotate.returncode == 0, annotate.stderr

    annotated_ids = _annotated_read_ids(annotate.stdout)
    assert len(annotated_ids) == 5
    assert any("adapter=no_adapter" in read_id for read_id in annotated_ids if read_id.startswith("read_no_lower|"))
    assert any("adapter=no_adapter" in read_id for read_id in annotated_ids if read_id.startswith("read_no_upper|"))
    assert any("adapter=no_adapter_1a" in read_id for read_id in annotated_ids if read_id.startswith("read_weird|"))
    assert any(read_id.endswith("adapter=") for read_id in annotated_ids if read_id.startswith("read_empty|"))
    assert any("adapter=sample_A" in read_id for read_id in annotated_ids if read_id.startswith("read_sample|"))

    blast_report = tmp_path / "blast_report_annotated_otu_full.txt"
    blast_report.write_text(
        "".join(f"{read_id}\tassigned\n" for read_id in annotated_ids),
        encoding="utf-8",
    )

    detect = subprocess.run(
        ["bash", str(DETECT_POLICY), str(blast_report)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert detect.returncode == 0, detect.stderr
    assert "observed_no_adapter\t1" in detect.stdout
    assert "observed_non_no_adapter\t1" in detect.stdout
    assert "separate_no_adapter\t1" in detect.stdout

    no_adapter_rows = subprocess.run(
        ["bash", str(FILTER_ROWS), str(blast_report), "no_adapter"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert no_adapter_rows.returncode == 0, no_adapter_rows.stderr
    assert "read_no_lower|COI|sup|barcode=no_adapter_1|adapter=no_adapter\tassigned\n" in no_adapter_rows.stdout
    assert "read_no_upper|COI|sup|barcode=no_adapter_1|adapter=no_adapter\tassigned\n" in no_adapter_rows.stdout
    assert "read_weird|" not in no_adapter_rows.stdout
    assert "read_empty|" not in no_adapter_rows.stdout
    assert "read_sample|" not in no_adapter_rows.stdout

    sample_rows = subprocess.run(
        ["bash", str(FILTER_ROWS), str(blast_report), "sample", "sample_A"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert sample_rows.returncode == 0, sample_rows.stderr
    assert sample_rows.stdout == "read_sample|COI|sup|barcode=bc1|adapter=sample_A\tassigned\n"
