import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "active_prune_candidates.pl"
MODE_HELPER = REPO_ROOT / "bin" / "otu_blast_effective_mode.sh"


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _run(tmp_path: Path, *, round_index_value: str, skip_rounds: str):
    otu_members_round = tmp_path / "otu_members_round.tsv"
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    size_streak_ids = tmp_path / "otu_size_streak_ids_last.txt"
    round_index = tmp_path / "round_index.tsv"

    out_size_streak = tmp_path / "active_prune_candidates_size_streak.list"
    out_size_candidates = tmp_path / "active_prune_candidates_size_candidates.list"
    out_all = tmp_path / "active_prune_candidates_all.list"
    out_counts = tmp_path / "active_prune_candidates_counts.tsv"

    _write_text(
        otu_members_round,
        (
            "otu_id\tread_id\n"
            "OTU_1\treadA\n"
            "OTU_1\treadB\n"
            "OTU_2\treadC\n"
        ),
    )
    _write_text(
        otu_sizes_round,
        (
            "otu_id\tsize\n"
            "OTU_1\t2\n"
            "OTU_2\t3\n"
            "OTU_3\t1\n"
        ),
    )
    _write_text(size_streak_ids, "readA\nreadZ\n")
    _write_text(round_index, f"round_001\t{round_index_value}\n")

    result = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--otu-members-round",
            str(otu_members_round),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--size-streak-ids",
            str(size_streak_ids),
            "--otu-blast-min-members",
            "3",
            "--otu-blast-filter-mode",
            "enforce",
            "--otu-blast-filter-skip-rounds",
            skip_rounds,
            "--round-index-file",
            str(round_index),
            "--round-barcode",
            "round_001",
            "--effective-mode-helper",
            str(MODE_HELPER),
            "--out-size-streak",
            str(out_size_streak),
            "--out-size-candidates",
            str(out_size_candidates),
            "--out-all",
            str(out_all),
            "--out-counts",
            str(out_counts),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return result, out_size_streak, out_size_candidates, out_all, out_counts


def _read_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line]


def _read_kv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _read_lines(path):
        if "\t" not in line:
            continue
        key, value = line.split("\t", 1)
        out[key] = value
    return out


def test_active_prune_candidates_within_skip_window_emits_size_streak_and_candidates(
    tmp_path: Path,
) -> None:
    result, out_size_streak, out_size_candidates, out_all, out_counts = _run(
        tmp_path, round_index_value="1", skip_rounds="3"
    )
    assert result.returncode == 0, result.stderr

    assert _read_lines(out_size_streak) == ["readA"]
    assert _read_lines(out_size_candidates) == ["readA", "readB"]
    assert _read_lines(out_all) == ["readA", "readB"]

    counts = _read_kv(out_counts)
    assert set(counts) >= {
        "active_total",
        "active_scope",
        "active_scope_reason",
        "size_streak_input_status",
        "size_streak_active",
        "size_streak_candidates",
        "union",
        "size_streak_possible",
        "size_streak_applied",
        "size_streak_disabled",
        "effective_mode",
        "effective_reason",
        "round_index",
        "size_streak_round_candidates",
        "size_streak_eligible_candidates",
    }
    assert counts["active_total"] == "3"
    assert counts["size_streak_active"] == "1"
    assert counts["size_streak_candidates"] == "2"
    assert counts["union"] == "2"
    assert counts["active_scope"] == "round"
    assert counts["active_scope_reason"] == "round_local_ok"
    assert counts["size_streak_input_status"] == "ok"
    assert counts["size_streak_possible"] == "1"
    assert counts["size_streak_applied"] == "0"
    assert counts["size_streak_disabled"] == "0"
    assert counts["effective_mode"] == "off"
    assert counts["effective_reason"] == "within_skip_window"
    assert counts["round_index"] == "1"
    assert counts["size_streak_round_candidates"] == "2"
    assert counts["size_streak_eligible_candidates"] == "2"


def test_active_prune_candidates_after_skip_window_disables_size_candidates(tmp_path: Path) -> None:
    result, out_size_streak, out_size_candidates, out_all, out_counts = _run(
        tmp_path, round_index_value="5", skip_rounds="3"
    )
    assert result.returncode == 0, result.stderr

    assert _read_lines(out_size_streak) == ["readA"]
    assert _read_lines(out_size_candidates) == []
    assert _read_lines(out_all) == ["readA"]

    counts = _read_kv(out_counts)
    assert counts["size_streak_active"] == "1"
    assert counts["size_streak_candidates"] == "0"
    assert counts["union"] == "1"
    assert counts["active_scope"] == "round"
    assert counts["active_scope_reason"] == "round_local_ok"
    assert counts["size_streak_input_status"] == "ok"
    assert counts["size_streak_possible"] == "1"
    assert counts["size_streak_applied"] == "1"
    assert counts["size_streak_disabled"] == "1"
    assert counts["effective_mode"] == "enforce"
    assert counts["effective_reason"] == "after_skip_window"


def test_active_prune_candidates_missing_round_index_keeps_size_streak_only(tmp_path: Path) -> None:
    result, out_size_streak, out_size_candidates, out_all, out_counts = _run(
        tmp_path, round_index_value="2", skip_rounds="3"
    )
    assert result.returncode == 0, result.stderr
    (tmp_path / "round_index.tsv").write_text("other_round\t2\n", encoding="utf-8")

    rerun = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--otu-members-round",
            str(tmp_path / "otu_members_round.tsv"),
            "--otu-sizes-round",
            str(tmp_path / "otu_sizes_round.tsv"),
            "--size-streak-ids",
            str(tmp_path / "otu_size_streak_ids_last.txt"),
            "--otu-blast-min-members",
            "3",
            "--otu-blast-filter-mode",
            "enforce",
            "--otu-blast-filter-skip-rounds",
            "3",
            "--round-index-file",
            str(tmp_path / "round_index.tsv"),
            "--round-barcode",
            "round_001",
            "--effective-mode-helper",
            str(MODE_HELPER),
            "--out-size-streak",
            str(out_size_streak),
            "--out-size-candidates",
            str(out_size_candidates),
            "--out-all",
            str(out_all),
            "--out-counts",
            str(out_counts),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 0, rerun.stderr
    assert "missing/invalid round index" in rerun.stderr
    assert _read_lines(out_size_streak) == ["readA"]
    assert _read_lines(out_size_candidates) == []
    assert _read_lines(out_all) == ["readA"]
    counts = _read_kv(out_counts)
    assert counts["round_index"] == "NA"
    assert counts["size_streak_disabled"] == "1"
    assert counts["active_scope"] == "round"
    assert counts["active_scope_reason"] == "round_local_ok"
    assert counts["size_streak_input_status"] == "ok"
    assert counts["size_streak_possible"] == "1"
    assert counts["size_streak_applied"] == "0"
    assert counts["effective_reason"] == "missing_round_index"


def test_active_prune_candidates_skip_all_without_round_index_still_computes_size_candidates(
    tmp_path: Path,
) -> None:
    result, out_size_streak, out_size_candidates, out_all, out_counts = _run(
        tmp_path, round_index_value="2", skip_rounds="all"
    )
    assert result.returncode == 0, result.stderr
    (tmp_path / "round_index.tsv").write_text("", encoding="utf-8")

    rerun = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--otu-members-round",
            str(tmp_path / "otu_members_round.tsv"),
            "--otu-sizes-round",
            str(tmp_path / "otu_sizes_round.tsv"),
            "--size-streak-ids",
            str(tmp_path / "otu_size_streak_ids_last.txt"),
            "--otu-blast-min-members",
            "3",
            "--otu-blast-filter-mode",
            "enforce",
            "--otu-blast-filter-skip-rounds",
            "all",
            "--round-index-file",
            str(tmp_path / "round_index.tsv"),
            "--round-barcode",
            "round_001",
            "--effective-mode-helper",
            str(MODE_HELPER),
            "--out-size-streak",
            str(out_size_streak),
            "--out-size-candidates",
            str(out_size_candidates),
            "--out-all",
            str(out_all),
            "--out-counts",
            str(out_counts),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 0, rerun.stderr
    assert _read_lines(out_size_streak) == ["readA"]
    assert _read_lines(out_size_candidates) == ["readA", "readB"]
    assert _read_lines(out_all) == ["readA", "readB"]
    counts = _read_kv(out_counts)
    assert counts["round_index"] == "NA"
    assert counts["effective_mode"] == "off"
    assert counts["effective_reason"] == "skip_all_rounds_no_index"
    assert counts["size_streak_disabled"] == "0"
    assert counts["active_scope"] == "round"
    assert counts["active_scope_reason"] == "round_local_ok"
    assert counts["size_streak_input_status"] == "ok"
    assert counts["size_streak_possible"] == "1"
    assert counts["size_streak_applied"] == "0"


def test_active_prune_candidates_round_members_empty_keeps_round_scope(
    tmp_path: Path,
) -> None:
    result, out_size_streak, out_size_candidates, out_all, out_counts = _run(
        tmp_path, round_index_value="1", skip_rounds="3"
    )
    assert result.returncode == 0, result.stderr
    (tmp_path / "otu_members_round.tsv").write_text("otu_id\tread_id\n", encoding="utf-8")
    (tmp_path / "otu_sizes_round.tsv").write_text("otu_id\tsize\n", encoding="utf-8")

    rerun = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--otu-members-round",
            str(tmp_path / "otu_members_round.tsv"),
            "--otu-sizes-round",
            str(tmp_path / "otu_sizes_round.tsv"),
            "--size-streak-ids",
            str(tmp_path / "otu_size_streak_ids_last.txt"),
            "--otu-blast-min-members",
            "3",
            "--otu-blast-filter-mode",
            "enforce",
            "--otu-blast-filter-skip-rounds",
            "3",
            "--round-index-file",
            str(tmp_path / "round_index.tsv"),
            "--round-barcode",
            "round_001",
            "--effective-mode-helper",
            str(MODE_HELPER),
            "--out-size-streak",
            str(out_size_streak),
            "--out-size-candidates",
            str(out_size_candidates),
            "--out-all",
            str(out_all),
            "--out-counts",
            str(out_counts),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 0, rerun.stderr
    counts = _read_kv(out_counts)
    assert counts["active_scope"] == "round"
    assert counts["active_scope_reason"] == "round_local_empty"
    assert counts["size_streak_input_status"] == "empty"
    assert counts["size_streak_possible"] == "0"
    assert counts["size_streak_applied"] == "0"


def test_active_prune_candidates_missing_round_members_reports_unavailable(
    tmp_path: Path,
) -> None:
    result, out_size_streak, out_size_candidates, out_all, out_counts = _run(
        tmp_path, round_index_value="1", skip_rounds="3"
    )
    assert result.returncode == 0, result.stderr
    (tmp_path / "otu_members_round.tsv").unlink()

    rerun = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--otu-members-round",
            str(tmp_path / "otu_members_round.tsv"),
            "--otu-sizes-round",
            str(tmp_path / "otu_sizes_round.tsv"),
            "--size-streak-ids",
            str(tmp_path / "otu_size_streak_ids_last.txt"),
            "--otu-blast-min-members",
            "3",
            "--otu-blast-filter-mode",
            "enforce",
            "--otu-blast-filter-skip-rounds",
            "3",
            "--round-index-file",
            str(tmp_path / "round_index.tsv"),
            "--round-barcode",
            "round_001",
            "--effective-mode-helper",
            str(MODE_HELPER),
            "--out-size-streak",
            str(out_size_streak),
            "--out-size-candidates",
            str(out_size_candidates),
            "--out-all",
            str(out_all),
            "--out-counts",
            str(out_counts),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 0, rerun.stderr
    counts = _read_kv(out_counts)
    assert counts["active_scope"] == "round"
    assert counts["active_scope_reason"] == "round_local_unavailable"
    assert counts["size_streak_input_status"] == "ok"
    assert counts["size_streak_possible"] == "1"
    assert counts["size_streak_applied"] == "0"
    assert counts["active_total"] == "0"
    assert counts["size_streak_active"] == "0"
    assert counts["size_streak_candidates"] == "0"
    assert counts["union"] == "0"


def test_active_prune_candidates_missing_round_sizes_reports_input_status(tmp_path: Path) -> None:
    result, out_size_streak, out_size_candidates, out_all, out_counts = _run(
        tmp_path, round_index_value="1", skip_rounds="3"
    )
    assert result.returncode == 0, result.stderr
    (tmp_path / "otu_sizes_round.tsv").unlink()

    rerun = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--otu-members-round",
            str(tmp_path / "otu_members_round.tsv"),
            "--otu-sizes-round",
            str(tmp_path / "otu_sizes_round.tsv"),
            "--size-streak-ids",
            str(tmp_path / "otu_size_streak_ids_last.txt"),
            "--otu-blast-min-members",
            "3",
            "--otu-blast-filter-mode",
            "enforce",
            "--otu-blast-filter-skip-rounds",
            "3",
            "--round-index-file",
            str(tmp_path / "round_index.tsv"),
            "--round-barcode",
            "round_001",
            "--effective-mode-helper",
            str(MODE_HELPER),
            "--out-size-streak",
            str(out_size_streak),
            "--out-size-candidates",
            str(out_size_candidates),
            "--out-all",
            str(out_all),
            "--out-counts",
            str(out_counts),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rerun.returncode == 0, rerun.stderr
    counts = _read_kv(out_counts)
    assert counts["active_scope"] == "round"
    assert counts["active_scope_reason"] == "round_local_ok"
    assert counts["size_streak_input_status"] == "missing"
    assert counts["size_streak_possible"] == "0"
    assert counts["size_streak_applied"] == "0"
