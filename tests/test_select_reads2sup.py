import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "select_reads2sup.pl"


def test_blocked_keeps_sup_and_downgrades_hac(tmp_path):
    inp = tmp_path / "blast_annotated_otu.txt"
    inp.write_text(
        "#header\n"
        "read1|COI|sup|adapter=sample_1|OTUB_1-COI\n"
        "read2|COI|hac|adapter=sample_1|OTUB_1-COI\n",
        encoding="utf-8",
    )
    pident = tmp_path / "pident.tsv"
    pident.write_text("", encoding="utf-8")
    blocked = tmp_path / "blocked.tsv"
    blocked.write_text("sample_1\tOTUB_1-COI\n", encoding="utf-8")

    cmd = [
        "perl",
        str(SCRIPT),
        str(inp),
        "50",
        str(pident),
        str(blocked),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.splitlines()
    assert "read1|COI|sup|adapter=sample_1|OTUB_1-COI" in out
    assert "read2|COI|hac_fixed|adapter=sample_1|OTUB_1-COI" in out


def test_marker_specific_blocking(tmp_path):
    inp = tmp_path / "blast_annotated_otu.txt"
    inp.write_text(
        "#header\n"
        "read3|ITS2|hac|adapter=sample_1|OTUB_1-ITS2\n",
        encoding="utf-8",
    )
    pident = tmp_path / "pident.tsv"
    pident.write_text("read3\t99.0\n", encoding="utf-8")
    blocked = tmp_path / "blocked.tsv"
    blocked.write_text("sample_1\tOTUB_1-COI\n", encoding="utf-8")

    cmd = [
        "perl",
        str(SCRIPT),
        str(inp),
        "50",
        str(pident),
        str(blocked),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.splitlines()
    assert "read3|ITS2|hac2sup|adapter=sample_1|OTUB_1-ITS2" in out


def test_blocks_match_base_sample_key(tmp_path):
    inp = tmp_path / "blast_annotated_otu.txt"
    inp.write_text(
        "#header\n"
        "read4|COI|hac|adapter=sample_2|OTUB_2\n",
        encoding="utf-8",
    )
    blocked = tmp_path / "blocked.tsv"
    blocked.write_text("sample\tOTUB_2-COI\n", encoding="utf-8")
    pident = tmp_path / "pident.tsv"
    pident.write_text("", encoding="utf-8")
    cmd = [
        "perl",
        str(SCRIPT),
        str(inp),
        "50",
        str(pident),
        str(blocked),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.splitlines()
    assert "read4|COI|hac_fixed|adapter=sample_2|OTUB_2-COI" in out


def test_missing_marker_is_normalized_for_blocking(tmp_path):
    inp = tmp_path / "blast_annotated_otu.txt"
    inp.write_text(
        "#header\n"
        "read5|COI|hac|adapter=sample_1|OTUB_5\n",
        encoding="utf-8",
    )
    blocked = tmp_path / "blocked.tsv"
    blocked.write_text("sample_1\tOTUB_5-COI\n", encoding="utf-8")
    pident = tmp_path / "pident.tsv"
    pident.write_text("", encoding="utf-8")
    cmd = [
        "perl",
        str(SCRIPT),
        str(inp),
        "50",
        str(pident),
        str(blocked),
    ]
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.splitlines()
    assert "read5|COI|hac_fixed|adapter=sample_1|OTUB_5-COI" in out
