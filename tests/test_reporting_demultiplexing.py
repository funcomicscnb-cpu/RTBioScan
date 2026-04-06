import subprocess
from pathlib import Path
import os


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "reporting_demultiplexing.pl"
TEST_ENV = {
    "RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": "full_collapse",
    "RTBIOSCAN_TARGET_TOKENS": "COI|ITS2",
}


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_reporting_demultiplexing_normalizes_no_adapter(tmp_path: Path) -> None:
    fastq = tmp_path / "reads.fastq"
    write_text(
        fastq,
        "@read1|COI|hac|barcode=|adapter=no_adapter_2\n"
        "ACGT\n"
        "+\n"
        "####\n",
    )
    results_dir = tmp_path / "results"
    round_dir = tmp_path / "round"
    cmd = ["perl", str(SCRIPT), str(fastq), str(results_dir), str(round_dir), "RTBioScan"]
    subprocess.run(cmd, check=True, cwd=tmp_path, env={**os.environ, **TEST_ENV})
    report = tmp_path / "RTBioScan_demult_rpt.txt"
    data = report.read_text(encoding="utf-8").splitlines()
    assert len(data) == 2
    fields = data[1].split("\t")
    assert fields[1] == "COI"
    assert fields[3] == "no_adapter"


def test_reporting_demultiplexing_prefers_target_token_over_sample_label(tmp_path: Path) -> None:
    fastq = tmp_path / "reads.fastq"
    write_text(
        fastq,
        "@read1|COI|hac|barcode=|adapter=GAG1.spiked_COI\n"
        "ACGT\n"
        "+\n"
        "####\n",
    )
    results_dir = tmp_path / "results"
    round_dir = tmp_path / "round"
    cmd = ["perl", str(SCRIPT), str(fastq), str(results_dir), str(round_dir), "RTBioScan"]
    subprocess.run(cmd, check=True, cwd=tmp_path, env={**os.environ, **TEST_ENV})
    report = tmp_path / "RTBioScan_demult_rpt.txt"
    data = report.read_text(encoding="utf-8").splitlines()
    assert len(data) == 2
    fields = data[1].split("\t")
    assert fields[1] == "COI"
    assert fields[3] == "GAG1.spiked_COI"


def test_reporting_demultiplexing_empty_barcode_without_target_falls_back_to_no_adapter(tmp_path: Path) -> None:
    fastq = tmp_path / "reads.fastq"
    write_text(
        fastq,
        "@read1|hac|barcode=|adapter=GAG1.spiked_COI\n"
        "ACGT\n"
        "+\n"
        "####\n",
    )
    results_dir = tmp_path / "results"
    round_dir = tmp_path / "round"
    cmd = ["perl", str(SCRIPT), str(fastq), str(results_dir), str(round_dir), "RTBioScan"]
    subprocess.run(cmd, check=True, cwd=tmp_path, env={**os.environ, **TEST_ENV})
    report = tmp_path / "RTBioScan_demult_rpt.txt"
    data = report.read_text(encoding="utf-8").splitlines()
    assert len(data) == 2
    fields = data[1].split("\t")
    assert fields[1] == "no_adapter_1"
    assert fields[3] == "GAG1.spiked_COI"
