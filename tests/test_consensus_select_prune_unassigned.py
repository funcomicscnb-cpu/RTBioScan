import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_select_reads_by_rank.pl"


def _parse_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        k, v = ln.split("\t", 1)
        out[k] = v
    return out


def _write_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    pool = tmp_path / "pool.tsv"
    reads = tmp_path / "reads.fasta"
    assigned = tmp_path / "assigned.list"
    pool.write_text(
        "\n".join(
            [
                "read1\tread1|COI|sup|adapter=s1\t3\t30",
                "read1b\tread1b|COI|sup|adapter=s1\t3\t30",
                "read2\tread2|COI|sup|adapter=s1\t3\t30",
                "read2b\tread2b|COI|sup|adapter=s1\t3\t30",
                "read2c\tread2c|COI|sup|adapter=s1\t3\t30",
                "read3\tread3|COI|sup|adapter=s1\t3\t30",
                "read3b\tread3b|COI|sup|adapter=s1\t3\t30",
                "read4\tread4|COI|sup|adapter=s1\t3\t30",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    reads.write_text(
        "\n".join(
            [
                ">read1|COI|sup|adapter=s1",
                "AAAA",
                ">read1b|COI|sup|adapter=s1",
                "AAAA",
                ">read2|COI|sup|adapter=s1",
                "CCCC",
                ">read2b|COI|sup|adapter=s1",
                "CCCC",
                ">read2c|COI|sup|adapter=s1",
                "CCCC",
                ">read3|COI|sup|adapter=s1",
                "GGGG",
                ">read3b|COI|sup|adapter=s1",
                "GGGG",
                ">read4|COI|sup|adapter=s1",
                "TTTT",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assigned.write_text("read1\n", encoding="utf-8")
    return pool, reads, assigned


def test_prune_unassigned_keeps_assigned_plus_top_k(tmp_path: Path) -> None:
    pool, reads, assigned = _write_fixture(tmp_path)
    out_prefix = tmp_path / "sel"
    stats = tmp_path / "sel.prune.tsv"
    dropped = tmp_path / "sel.dropped.list"
    subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(pool),
            str(reads),
            "2",
            "0",
            str(out_prefix),
            "--prune-unassigned",
            "--assigned-ids",
            str(assigned),
            "--keep-unassigned-top",
            "1",
            "--emit-prune-stats",
            str(stats),
            "--dropped-ids",
            str(dropped),
        ],
        check=True,
    )
    kv = _parse_kv(stats)
    assert kv["prune_applied"] == "1"
    assert kv["reason"] == "applied"
    assert kv["total_clusters"] == "4"
    assert kv["assigned_clusters"] == "1"
    assert kv["unassigned_clusters"] == "3"
    assert kv["kept_unassigned"] == "1"
    assert kv["dropped_unassigned"] == "2"
    reps_all_rows = [ln for ln in (tmp_path / "sel.reps_all.tsv").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(reps_all_rows) == 2
    dropped_ids = {ln.strip() for ln in dropped.read_text(encoding="utf-8").splitlines() if ln.strip()}
    assert dropped_ids == {"read3", "read3b", "read4"}


def test_prune_unassigned_missing_assigned_list_disables_prune(tmp_path: Path) -> None:
    pool, reads, _assigned = _write_fixture(tmp_path)
    out_prefix = tmp_path / "sel"
    stats = tmp_path / "sel.prune.tsv"
    missing_assigned = tmp_path / "missing.list"
    subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(pool),
            str(reads),
            "2",
            "0",
            str(out_prefix),
            "--prune-unassigned",
            "--assigned-ids",
            str(missing_assigned),
            "--keep-unassigned-top",
            "1",
            "--emit-prune-stats",
            str(stats),
        ],
        check=True,
    )
    kv = _parse_kv(stats)
    assert kv["prune_applied"] == "0"
    assert kv["reason"] == "missing_assigned_ids"
    reps_all_rows = [ln for ln in (tmp_path / "sel.reps_all.tsv").read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(reps_all_rows) == 4


def test_prune_unassigned_all_clusters_pruned_keeps_dropped_ids(tmp_path: Path) -> None:
    pool, reads, _assigned = _write_fixture(tmp_path)
    assigned = tmp_path / "assigned_present.list"
    assigned.write_text("not_present\n", encoding="utf-8")
    out_prefix = tmp_path / "sel_all"
    stats = tmp_path / "sel_all.prune.tsv"
    dropped = tmp_path / "sel_all.dropped.list"
    subprocess.run(
        [
            "perl",
            str(SCRIPT),
            str(pool),
            str(reads),
            "2",
            "0",
            str(out_prefix),
            "--prune-unassigned",
            "--assigned-ids",
            str(assigned),
            "--keep-unassigned-top",
            "0",
            "--emit-prune-stats",
            str(stats),
            "--dropped-ids",
            str(dropped),
        ],
        check=True,
    )
    kv = _parse_kv(stats)
    assert kv["prune_applied"] == "1"
    assert kv["dropped_unassigned"] == "4"
    dropped_ids = [ln.strip() for ln in dropped.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert dropped_ids == ["read1", "read1b", "read2", "read2b", "read2c", "read3", "read3b", "read4"]
