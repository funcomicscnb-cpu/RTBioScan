import subprocess
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_select_reads_by_rank.pl"


def test_reps_all_includes_unselected_clusters(tmp_path: Path) -> None:
    pool = tmp_path / "pool.tsv"
    reads = tmp_path / "reads.fasta"
    out_prefix = tmp_path / "sel"

    pool.write_text(
        "\n".join(
            [
                "read1\tread1|COI|sup|adapter=no_adapter_1\t3\t30",
                "read2\tread2|COI|sup|adapter=no_adapter_1\t3\t30",
                "read3\tread3|COI|sup|adapter=no_adapter_1\t3\t30",
                "read4\tread4|COI|sup|adapter=no_adapter_1\t3\t30",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    reads.write_text(
        "\n".join(
            [
                ">read1|COI|sup|adapter=no_adapter_1",
                "AAAA",
                ">read2|COI|sup|adapter=no_adapter_1",
                "AAAA",
                ">read3|COI|sup|adapter=no_adapter_1",
                "CCCC",
                ">read4|COI|sup|adapter=no_adapter_1",
                "CCCC",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(pool),
            str(reads),
            "2",  # min_reads: rank1_only will select AAAA cluster only
            "0",  # min_qscore
            str(out_prefix),
        ],
        check=True,
    )

    reps_all = tmp_path / "sel.reps_all.tsv"
    rows = [line.strip().split("\t") for line in reps_all.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    selected_flags = {row[5] for row in rows}
    assert selected_flags == {"0", "1"}


def test_selected_ids_and_selected_reps_preserve_current_ordering(tmp_path: Path) -> None:
    pool = tmp_path / "pool.tsv"
    reads = tmp_path / "reads.fasta"
    out_prefix = tmp_path / "sel"

    pool.write_text(
        "\n".join(
            [
                "read1\tread1|COI|sup|adapter=no_adapter_1\t3\t30",
                "read2\tread2|COI|sup|adapter=no_adapter_1\t3\t30",
                "read3\tread3|COI|sup|adapter=no_adapter_1\t3\t25",
                "read4\tread4|COI|sup|adapter=no_adapter_1\t3\t25",
                "read5\tread5|COI|sup|adapter=no_adapter_1\t3\t20",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    reads.write_text(
        "\n".join(
            [
                ">read1|COI|sup|adapter=no_adapter_1",
                "AAAA",
                ">read3|COI|sup|adapter=no_adapter_1",
                "CCCC",
                ">read2|COI|sup|adapter=no_adapter_1",
                "AAAA",
                ">read4|COI|sup|adapter=no_adapter_1",
                "CCCC",
                ">read5|COI|sup|adapter=no_adapter_1",
                "GGGG",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(pool),
            str(reads),
            "4",
            "0",
            str(out_prefix),
        ],
        check=True,
    )

    selected_ids = (tmp_path / "sel.selected_ids.list").read_text(encoding="utf-8").splitlines()
    assert selected_ids == [
        "read1|COI|sup|adapter=no_adapter_1",
        "read2|COI|sup|adapter=no_adapter_1",
        "read3|COI|sup|adapter=no_adapter_1",
        "read4|COI|sup|adapter=no_adapter_1",
    ]

    selected_reps = (tmp_path / "sel.selected_reps.tsv").read_text(encoding="utf-8").splitlines()
    assert len(selected_reps) == 2
    rep_cols = [row.split("\t") for row in selected_reps]
    assert [cols[1] for cols in rep_cols] == [
        "read1|COI|sup|adapter=no_adapter_1",
        "read3|COI|sup|adapter=no_adapter_1",
    ]
    assert [cols[2:] for cols in rep_cols] == [
        ["30", "1", "2"],
        ["25", "2", "2"],
    ]


def test_selected_ids_preserve_order_with_many_selected_clusters_and_invalid_tmpdir(tmp_path: Path) -> None:
    pool = tmp_path / "pool.tsv"
    reads = tmp_path / "reads.fasta"
    out_prefix = tmp_path / "sel_many"

    cluster_count = 40
    pool_rows: list[str] = []
    read_lines: list[str] = []
    expected_ids: list[str] = []

    def _seq_for(idx: int) -> str:
        digits = []
        value = idx
        for _ in range(8):
            digits.append("ACGT"[value % 4])
            value //= 4
        return "".join(digits)

    for idx in range(cluster_count):
        rank = idx + 1
        qscore = 200 - idx
        seq = _seq_for(idx)
        read_a = f"cluster{rank:02d}_a|COI|sup|adapter=no_adapter_1"
        read_b = f"cluster{rank:02d}_b|COI|sup|adapter=no_adapter_1"
        pool_rows.append(f"cluster{rank:02d}_a\t{read_a}\t3\t{qscore}")
        pool_rows.append(f"cluster{rank:02d}_b\t{read_b}\t3\t{qscore}")
        read_lines.extend([f">{read_a}", seq])
        expected_ids.extend([read_a, read_b])
    for idx in range(cluster_count):
        rank = idx + 1
        seq = _seq_for(idx)
        read_b = f"cluster{rank:02d}_b|COI|sup|adapter=no_adapter_1"
        read_lines.extend([f">{read_b}", seq])

    pool.write_text("\n".join(pool_rows) + "\n", encoding="utf-8")
    reads.write_text("\n".join(read_lines) + "\n", encoding="utf-8")

    env = os.environ.copy()
    env["TMPDIR"] = str(tmp_path / "missing_tmp")

    subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(pool),
            str(reads),
            str(cluster_count * 2),
            "0",
            str(out_prefix),
        ],
        check=True,
        env=env,
    )

    selected_ids = (tmp_path / "sel_many.selected_ids.list").read_text(encoding="utf-8").splitlines()
    assert selected_ids == expected_ids
