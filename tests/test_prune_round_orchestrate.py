import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "prune_round_orchestrate.sh"


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _stats_map(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def _run(
    tmp_path: Path,
    *,
    prune_cumulative: str,
    assigned_ever: str,
    assigned_round: str,
    c1_ids: str,
    candidates: list[tuple[str, str]],
    blast_unassigned_status: str = "skipped_scope_mismatch",
    with_protected_stats: bool = False,
    script: Path = SCRIPT,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, dict[str, Path], Path]:
    assigned_ever_path = _write(tmp_path / "assigned_ever.list", assigned_ever)
    assigned_round_path = _write(tmp_path / "assigned_round.list", assigned_round)
    c1_ids_path = _write(tmp_path / "c1.list", c1_ids)
    protected_ids_path = tmp_path / "protected.list"
    protected_stats_path = tmp_path / "protected_stats.tsv"
    round_prune_ids_path = tmp_path / "round_prune.list"
    round_prune_stats_path = tmp_path / "round_prune.tsv"

    cand_paths: dict[str, Path] = {}
    for name, content in candidates:
        p = _write(tmp_path / f"{name}.list", content)
        cand_paths[name] = p

    cmd = [
        "bash",
        str(script),
        "--protected-read-ids-ever",
        str(assigned_ever_path),
        "--protected-read-ids-round",
        str(assigned_round_path),
        "--protected-ids",
        str(protected_ids_path),
    ]
    if with_protected_stats:
        cmd.extend(["--protected-stats", str(protected_stats_path)])
    for name, _ in candidates:
        cmd.extend(["--candidate", name, str(cand_paths[name])])
    cmd.extend([
        "--c1-ids", str(c1_ids_path),
        "--round-prune-ids", str(round_prune_ids_path),
        "--round-prune-stats", str(round_prune_stats_path),
        "--prune-cumulative-pool-all", prune_cumulative,
        "--blast-unassigned-status", blast_unassigned_status,
    ])

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result, protected_ids_path, protected_stats_path, cand_paths, round_prune_stats_path


def test_orchestrate_filters_protected_ids_and_merges_sources(tmp_path: Path) -> None:
    result, protected_ids, protected_stats, cand_paths, round_stats = _run(
        tmp_path,
        prune_cumulative="1",
        assigned_ever="readA\n",
        assigned_round="readB\n",
        c1_ids="readC\n",
        candidates=[
            ("size_streak", "readA\nreadD\n"),
            ("consensus_unassigned", "readB\nreadE\n"),
        ],
        with_protected_stats=True,
    )

    assert result.returncode == 0, result.stderr
    assert protected_ids.read_text(encoding="utf-8").splitlines() == ["readA", "readB"]
    # Caller's source files must be unchanged
    assert cand_paths["size_streak"].read_text(encoding="utf-8").splitlines() == ["readA", "readD"]
    assert cand_paths["consensus_unassigned"].read_text(encoding="utf-8").splitlines() == ["readB", "readE"]
    assert (tmp_path / "round_prune.list").read_text(encoding="utf-8").splitlines() == ["readC", "readD", "readE"]

    stats = _stats_map(round_stats)
    assert stats["total_candidates"] == "3"
    assert stats["c1_candidates"] == "1"
    assert stats["size_streak_candidates"] == "1"
    assert stats["consensus_unassigned_candidates"] == "1"
    assert stats["protected_read_ids_ever_count"] == "1"
    assert stats["protected_read_ids_round_count"] == "1"
    assert stats["protected_ids_total"] == "2"
    assert stats["blast_unassigned_status"] == "skipped_scope_mismatch"
    assert protected_stats.read_text(encoding="utf-8").splitlines()[-1] == "protected_ids_total\t2"


def test_orchestrate_writes_disabled_stats_when_cumulative_prune_is_off(tmp_path: Path) -> None:
    result, protected_ids, protected_stats, cand_paths, round_stats = _run(
        tmp_path,
        prune_cumulative="0",
        assigned_ever="readA\n",
        assigned_round="readB\n",
        c1_ids="readC\n",
        candidates=[
            ("size_streak", "readA\nreadD\n"),
            ("consensus_unassigned", "readB\nreadE\n"),
        ],
        blast_unassigned_status="unknown",
        with_protected_stats=True,
    )

    assert result.returncode == 0, result.stderr
    assert protected_ids.read_text(encoding="utf-8").splitlines() == ["readA", "readB"]
    assert cand_paths["size_streak"].read_text(encoding="utf-8").splitlines() == ["readA", "readD"]
    assert cand_paths["consensus_unassigned"].read_text(encoding="utf-8").splitlines() == ["readB", "readE"]
    assert (tmp_path / "round_prune.list").read_text(encoding="utf-8") == ""

    stats = _stats_map(round_stats)
    assert stats["disabled"] == "1"
    assert stats["reason"] == "prune_cumulative_pool_all_off"
    assert stats["protected_read_ids_ever_count"] == "1"
    assert stats["protected_read_ids_round_count"] == "1"
    assert stats["protected_ids_total"] == "2"
    assert stats["blast_unassigned_status"] == "unknown"


def test_orchestrate_three_candidates(tmp_path: Path) -> None:
    """Verify N-source generality: 3 independent candidates each contribute distinct reads."""
    result, protected_ids, _, cand_paths, round_stats = _run(
        tmp_path,
        prune_cumulative="1",
        assigned_ever="protected1\n",
        assigned_round="protected2\n",
        c1_ids="c1read\n",
        candidates=[
            ("size_streak", "protected1\nss_read\n"),
            ("consensus_unassigned", "cu_read\n"),
            ("blast_unassigned", "protected2\nbu_read\n"),
        ],
        with_protected_stats=True,
    )

    assert result.returncode == 0, result.stderr
    prune_list = (tmp_path / "round_prune.list").read_text(encoding="utf-8").splitlines()
    assert "c1read" in prune_list
    assert "ss_read" in prune_list
    assert "cu_read" in prune_list
    assert "bu_read" in prune_list
    # Protected reads must not appear
    assert "protected1" not in prune_list
    assert "protected2" not in prune_list

    stats = _stats_map(round_stats)
    assert stats["total_candidates"] == "4"
    assert stats["c1_candidates"] == "1"
    assert stats["size_streak_candidates"] == "1"
    assert stats["consensus_unassigned_candidates"] == "1"
    assert stats["blast_unassigned_candidates"] == "1"


def test_orchestrate_fails_fast_when_protected_id_build_fails(tmp_path: Path) -> None:
    custom_bin = tmp_path / "custom_bin"
    custom_bin.mkdir(parents=True, exist_ok=True)
    helper_copy = custom_bin / "prune_round_orchestrate.sh"
    helper_copy.write_text(SCRIPT.read_text(encoding="utf-8"), encoding="utf-8")
    helper_copy.chmod(0o755)
    (custom_bin / "merge_prune_ids.sh").write_text(
        (REPO_ROOT / "bin" / "merge_prune_ids.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (custom_bin / "merge_prune_ids.sh").chmod(0o755)
    (custom_bin / "build_protected_ids.sh").write_text(
        "#!/usr/bin/env bash\n"
        "echo 'forced protected build failure' 1>&2\n"
        "exit 9\n",
        encoding="utf-8",
    )
    (custom_bin / "build_protected_ids.sh").chmod(0o755)

    result, protected_ids, protected_stats, cand_paths, round_stats_path = _run(
        tmp_path,
        prune_cumulative="1",
        assigned_ever="readA\n",
        assigned_round="readB\n",
        c1_ids="readC\n",
        candidates=[
            ("size_streak", "readA\nreadD\n"),
            ("consensus_unassigned", "readB\nreadE\n"),
        ],
        with_protected_stats=True,
        script=helper_copy,
    )

    assert result.returncode != 0
    assert (
        "ERROR: build_protected_ids.sh failed; aborting prune orchestration to preserve protected-read coverage"
        in result.stderr
    )
    assert protected_ids.read_text(encoding="utf-8") == ""
    assert protected_stats.read_text(encoding="utf-8") == ""
    assert cand_paths["size_streak"].read_text(encoding="utf-8").splitlines() == ["readA", "readD"]
    assert cand_paths["consensus_unassigned"].read_text(encoding="utf-8").splitlines() == ["readB", "readE"]
    assert not (tmp_path / "round_prune.list").exists()
    assert not round_stats_path.exists()
