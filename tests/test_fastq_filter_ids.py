from pathlib import Path
import subprocess

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "fastq_filter_ids.pl"


def _run(tmp_path, fastq_text, ids_text):
    fastq = tmp_path / "in.fastq"
    ids = tmp_path / "ids.txt"
    out = tmp_path / "out.fastq"
    stats = tmp_path / "stats.tsv"
    fastq.write_text(fastq_text, encoding="utf-8")
    ids.write_text(ids_text, encoding="utf-8")
    result = subprocess.run(
        ["perl", str(SCRIPT), str(fastq), str(ids), str(out), str(stats)],
        capture_output=True,
        text=True,
    )
    return result, out, stats


def test_fastq_filter_removes_blocked_read(tmp_path):
    fastq_text = (
        "@read1|sup|barcode=x\nAAAA\n+\n!!!!\n"
        "@read2|sup|barcode=x\nCCCC\n+\n!!!!\n"
        "@read3|sup|barcode=x\nGGGG\n+\n!!!!\n"
    )
    ids_text = "read2\n"
    result, out, stats = _run(tmp_path, fastq_text, ids_text)
    assert result.returncode == 0, result.stderr
    out_text = out.read_text(encoding="utf-8")
    assert "@read2" not in out_text
    assert out_text.count("@") == 2
    stat_lines = {line.split("\t")[0]: line.split("\t")[1] for line in stats.read_text().splitlines()}
    assert stat_lines["reads_total"] == "3"
    assert stat_lines["reads_pruned"] == "1"
    assert stat_lines["reads_kept"] == "2"
