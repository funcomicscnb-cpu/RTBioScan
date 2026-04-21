import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "otu_size_streak_update.pl"


def _run(
    tmp_path: Path,
    sizes_text: str,
    members_text: str,
    hash_map_text: str,
    prev_text: str,
    min_rounds: int,
    min_members: int = 1,
    eligible_text: str | None = None,
    eligible_counts_text: str | None = None,
):
    sizes = tmp_path / "otu_sizes.tsv"
    members = tmp_path / "otu_members.tsv"
    hash_map = tmp_path / "otu_hash_map.tsv"
    prev = tmp_path / "prev_state.tsv"
    eligible = tmp_path / "eligible_size_streak.tsv"
    eligible_counts = tmp_path / "eligible_counts.tsv"
    out_ids = tmp_path / "prune_ids.txt"
    out_state = tmp_path / "next_state.tsv"
    out_stats = tmp_path / "stats.tsv"
    sizes.write_text(sizes_text, encoding="utf-8")
    members.write_text(members_text, encoding="utf-8")
    hash_map.write_text(hash_map_text, encoding="utf-8")
    prev.write_text(prev_text, encoding="utf-8")
    if eligible_text is not None:
        eligible.write_text(eligible_text, encoding="utf-8")
    if eligible_counts_text is not None:
        eligible_counts.write_text(eligible_counts_text, encoding="utf-8")
    result = subprocess.run(
        (
            [
                "perl",
                str(SCRIPT),
                str(sizes),
                str(members),
                str(hash_map),
                str(prev),
                str(min_rounds),
                str(out_ids),
                str(out_state),
                str(out_stats),
            ]
            + ([str(eligible)] if eligible_text is not None else [])
            + ([str(eligible_counts)] if eligible_counts_text is not None else [])
            + [str(min_members)]
        ),
        capture_output=True,
        text=True,
        check=False,
    )
    return result, out_ids, out_state, out_stats


def test_size_streak_increments_with_stable_marker_hash_key(tmp_path: Path) -> None:
    hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="OTUB_1-COI\t1\n",
        members_text="OTUB_1-COI\treadA\n",
        hash_map_text=f"readA|COI|sup|barcode=s1|adapter=s1\t{hash_a}\n",
        prev_text=f"COI|{hash_a}\tCOI\treadZ\tOTUB_999-COI\t2\n",
        min_rounds=3,
        min_members=1,
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8").strip().splitlines() == ["readA"]
    rows = out_state.read_text(encoding="utf-8").strip().splitlines()
    assert rows == [f"COI|{hash_a}\tCOI\treadA\tOTUB_1-COI\t3"]
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["otus_prune_candidate"] == "1"
    assert stats["reads_prune_candidate"] == "1"


def test_size_streak_state_resets_by_absence(tmp_path: Path) -> None:
    hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="",
        members_text="",
        hash_map_text="",
        prev_text=f"COI|{hash_a}\tCOI\treadA\tOTUB_1-COI\t2\n",
        min_rounds=3,
        min_members=1,
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8") == ""
    assert out_state.read_text(encoding="utf-8") == ""
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["otus_total"] == "0"
    assert stats["state_rows"] == "0"


def test_missing_hash_for_size_streak_is_counted_and_excluded(tmp_path: Path) -> None:
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="OTUB_3-COI\t1\n",
        members_text="OTUB_3-COI\treadX\n",
        hash_map_text="",
        prev_text="",
        min_rounds=2,
        min_members=1,
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8") == ""
    assert out_state.read_text(encoding="utf-8") == ""
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["otus_size_streak"] == "1"
    assert stats["otus_size_streak_missing_hash_rows"] == "1"
    assert stats["state_rows"] == "0"


def test_legacy_prev_state_key_is_ignored(tmp_path: Path) -> None:
    hash_b = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="OTUB_10-ITS2\t1\n",
        members_text="OTUB_10-ITS2\treadX\n",
        hash_map_text=f"readX|ITS2|sup|barcode=s1|adapter=s1\t{hash_b}\n",
        prev_text="ITS2|legacyRead\tITS2\tlegacyRead\tOTUB_10-ITS2\t2\n",
        min_rounds=3,
        min_members=1,
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8") == ""
    rows = out_state.read_text(encoding="utf-8").strip().splitlines()
    assert rows == [f"ITS2|{hash_b}\tITS2\treadX\tOTUB_10-ITS2\t1"]
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["prev_state_rows_loaded"] == "0"
    assert stats["prev_state_rows_legacy_skipped"] == "1"


def test_ambiguous_base_hash_is_excluded(tmp_path: Path) -> None:
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="OTUB_11-COI\t1\n",
        members_text="OTUB_11-COI\treadQ\n",
        hash_map_text=(
            "readQ|COI|sup|barcode=s1|adapter=s1\t11111111111111111111111111111111\n"
            "readQ|COI|hac|barcode=s1|adapter=s1\t22222222222222222222222222222222\n"
        ),
        prev_text="",
        min_rounds=2,
        min_members=1,
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8") == ""
    assert out_state.read_text(encoding="utf-8") == ""
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["hash_rows_ambiguous_base"] == "1"
    assert stats["otus_size_streak_missing_hash_rows"] == "1"


def test_stable_key_normalizes_marker_case_to_upper(tmp_path: Path) -> None:
    hash_c = "cccccccccccccccccccccccccccccccc"
    result, out_ids, out_state, _ = _run(
        tmp_path,
        sizes_text="OTUB_12-coi\t1\n",
        members_text="OTUB_12-coi\treadCase\n",
        hash_map_text=f"readCase|coi|sup|barcode=s1|adapter=s1\t{hash_c}\n",
        prev_text=f"COI|{hash_c}\tCOI\tlegacy\tOTUB_000-COI\t2\n",
        min_rounds=3,
        min_members=1,
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8").strip().splitlines() == ["readCase"]
    rows = out_state.read_text(encoding="utf-8").strip().splitlines()
    assert rows == [f"COI|{hash_c}\tCOI\treadCase\tOTUB_12-coi\t3"]


def test_size_streak_uses_eligible_size_streak(tmp_path: Path) -> None:
    hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="OTUB_1-COI\t1\n",
        members_text="OTUB_1-COI\treadA\n",
        hash_map_text=f"readA|COI|sup|barcode=s1|adapter=s1\t{hash_a}\n",
        prev_text="",
        min_rounds=2,
        min_members=1,
        eligible_text=(
            "sample\totu_key\tread_id\tround_barcode\n"
            "no_adapter\tOTUB_1-COI\treadA\toutput_first_50k\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8").strip() == ""
    rows = out_state.read_text(encoding="utf-8").strip().splitlines()
    assert rows == [f"COI|{hash_a}\tCOI\treadA\tOTUB_1-COI\t1"]
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["otus_size_streak"] == "1"
    assert stats["otus_prune_candidate"] == "0"


def test_size_streak_eligible_count_blocks_candidate(tmp_path: Path) -> None:
    hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="OTUB_1-COI\t1\n",
        members_text="OTUB_1-COI\treadA\n",
        hash_map_text=f"readA|COI|sup|barcode=s1|adapter=s1\t{hash_a}\n",
        prev_text="",
        min_rounds=2,
        min_members=1,
        eligible_text=(
            "sample\totu_key\tread_id\tround_barcode\n"
            "no_adapter\tOTUB_1-COI\treadA\toutput_first_50k\n"
        ),
        eligible_counts_text=(
            "sample\totu_key\teligible_pool_count\tround_barcode\n"
            "no_adapter\tOTUB_1-COI\t1\toutput_first_50k\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8").strip() == ""
    assert out_state.read_text(encoding="utf-8").strip() == f"COI|{hash_a}\tCOI\treadA\tOTUB_1-COI\t1"
    stats = dict(
        line.split("\t", 1)
        for line in out_stats.read_text(encoding="utf-8").strip().splitlines()
    )
    assert stats["otus_size_streak"] == "1"
    assert stats["otus_prune_candidate"] == "0"


def test_size_streak_eligible_count_skips_when_ge_two(tmp_path: Path) -> None:
    hash_a = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    result, out_ids, out_state, out_stats = _run(
        tmp_path,
        sizes_text="OTUB_1-COI\t1\n",
        members_text="OTUB_1-COI\treadA\n",
        hash_map_text=f"readA|COI|sup|barcode=s1|adapter=s1\t{hash_a}\n",
        prev_text="",
        min_rounds=2,
        min_members=1,
        eligible_text=(
            "sample\totu_key\tread_id\tround_barcode\n"
            "no_adapter\tOTUB_1-COI\treadA\toutput_first_50k\n"
        ),
        eligible_counts_text=(
            "sample\totu_key\teligible_pool_count\tround_barcode\n"
            "no_adapter\tOTUB_1-COI\t2\toutput_first_50k\n"
        ),
    )
    assert result.returncode == 0, result.stderr
    assert out_ids.read_text(encoding="utf-8").strip() == ""
    assert out_state.read_text(encoding="utf-8").strip() == ""
