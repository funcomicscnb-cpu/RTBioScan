from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "dorado_mixed_0_2_3_0_7_0"
READ_ID = "c9d52049-ce91-491c-8b19-3ae940f9f020"
REQUIRED_SUMMARY_COLUMNS = {
    "filename",
    "read_id",
    "run_id",
    "sequence_length_template",
    "mean_qscore_template",
}


def _summary_row(stage: str) -> tuple[dict[str, int], list[str]]:
    lines = (FIXTURE / f"{stage}.summary.tsv").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(lines) == 2
    header = lines[0].split("\t")
    return {name: index for index, name in enumerate(header)}, lines[1].split("\t")


def _sam_read(stage: str) -> tuple[list[str], str]:
    lines = (FIXTURE / f"{stage}.sam").read_text(encoding="utf-8").splitlines()
    program = next(line for line in lines if line.startswith("@PG\t"))
    read = next(line for line in lines if not line.startswith("@"))
    return read.split("\t"), program


@pytest.mark.parametrize(
    ("stage", "expected_length", "expected_qscore"),
    [("fast", 180, 9), ("hac", 183, 12), ("sup", 182, 12)],
)
def test_captured_mixed_version_summary_matches_legacy_sam(
    stage: str, expected_length: int, expected_qscore: int
) -> None:
    header, summary = _summary_row(stage)
    sam, program = _sam_read(stage)

    assert REQUIRED_SUMMARY_COLUMNS <= set(header)
    assert "VN:0.2.3+4ed609d" in program
    assert sam[0] == summary[header["read_id"]] == READ_ID
    assert len(sam[9]) == int(summary[header["sequence_length_template"]])
    assert len(sam[9]) == expected_length
    qscore_tag = next(value for value in sam[11:] if value.startswith("qs:i:"))
    assert int(qscore_tag.removeprefix("qs:i:")) == expected_qscore
    assert int(summary[header["mean_qscore_template"]]) == expected_qscore


def _write_samtools_stub(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
[ "$1" = "fastq" ] || exit 2
printf '@fixture\\nACGT\\n+\\nIIII\\n'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_mixed_version_summaries_feed_actual_reporting_consumers(
    tmp_path: Path,
) -> None:
    for stage in ("fast", "hac", "sup"):
        shutil.copyfile(FIXTURE / f"{stage}.sam", tmp_path / f"{stage}.sam")
        shutil.copyfile(
            FIXTURE / f"{stage}.summary.tsv", tmp_path / f"{stage}.summary.tsv"
        )

    blast = tmp_path / "blast.tsv"
    blast.write_text(
        f"{READ_ID}\tCOI|Metazoa|fixture\t100\t180\n",
        encoding="utf-8",
    )
    targets = tmp_path / "targets.list"
    targets.write_text(f"{READ_ID}|COI|Metazoa|fixture\n", encoding="utf-8")
    (tmp_path / "blast_read.csv").write_text("", encoding="utf-8")
    (tmp_path / "blast_otu.tsv").write_text("", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_samtools_stub(fake_bin / "samtools")
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": "off",
        "RTBIOSCAN_TARGET_TOKENS": "COI|ITS2",
    }

    subprocess.run(
        [
            "perl",
            str(REPO_ROOT / "bin" / "reporting_getting_on_target.pl"),
            "fast.summary.tsv",
            "fast.sam",
            blast.name,
            targets.name,
            "1",
            "1000",
            "RTBioScan",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    shutil.copyfile(
        tmp_path / "RTBioScan_read_info_rpt.txt",
        tmp_path / "fast_read_info_rpt.txt",
    )

    subprocess.run(
        [
            "perl",
            str(REPO_ROOT / "bin" / "reporting_getting_hq.pl"),
            "hac.summary.tsv",
            "hac.sam",
            "fast_read_info_rpt.txt",
            "1",
            "1000",
            "0",
            "RTBioScan",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    shutil.copyfile(
        tmp_path / "RTBioScan_read_info_rpt.txt",
        tmp_path / "hac_read_info_rpt.txt",
    )

    subprocess.run(
        [
            "perl",
            str(REPO_ROOT / "bin" / "reporting_blast_otu.pl"),
            "sup.summary.tsv",
            "blast_read.csv",
            "blast_otu.tsv",
            "hac_read_info_rpt.txt",
            "RTBioScan",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )

    lines = (tmp_path / "RTBioScan_read_info_rpt.txt").read_text(
        encoding="utf-8"
    ).splitlines()
    assert lines == [
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore"
        "\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore",
        f"{READ_ID}\trtbioscan-dorado-qualification-5khz.pod5\ttest\tCOI"
        "\t180\t9\t183\t12\t182\t12",
    ]


def test_mixed_version_sup_summary_supports_cache_id_parser(tmp_path: Path) -> None:
    ids = tmp_path / "ids.list"
    command = r'''
set -euo pipefail
source "$1"
sup_summary_id_list "$2" "$3"
printf 'rows=%s\n' "$(sup_count_summary_rows "$2")"
'''
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "_",
            str(REPO_ROOT / "bin" / "blast_sup_path.sh"),
            str(FIXTURE / "sup.summary.tsv"),
            str(ids),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "rows=1\n"
    assert ids.read_text(encoding="utf-8") == f"{READ_ID}\n"
