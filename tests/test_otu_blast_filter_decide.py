import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DECIDE = REPO_ROOT / "bin" / "otu_blast_filter_decide.sh"


def _write_stats(path: Path, rows: list[tuple[str, str]]) -> None:
    path.write_text("".join(f"{k}\t{v}\n" for k, v in rows), encoding="utf-8")


def _run(stats: Path, mode: str, max_frac: str, no_clusters_policy: str):
    return subprocess.run(
        [str(DECIDE), str(stats), mode, max_frac, no_clusters_policy],
        capture_output=True,
        text=True,
        check=False,
    )


def _decision_map(stdout: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip() or "\t" not in line:
            continue
        k, v = line.split("\t", 1)
        out[k] = v
    return out


def test_decide_fails_on_missing_required_stats_key(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "1"),
            ("clstr_has_clusters", "1"),
            ("clstr_records", "2"),
            ("total_otus", "1"),
            ("ambiguous_hash_cluster", "0"),
            # missing missing_policy
        ],
    )
    result = _run(stats, "enforce", "0.1", "fallback_unfiltered")
    assert result.returncode != 0
    assert "Missing required stats key 'missing_policy'" in result.stderr


def test_decide_enforce_threshold_passes_to_filtered(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "1"),
            ("clstr_has_clusters", "1"),
            ("clstr_records", "2"),
            ("total_otus", "1"),
            ("ambiguous_hash_cluster", "0"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "0.2", "fallback_unfiltered")
    assert result.returncode == 0, result.stderr
    decision = _decision_map(result.stdout)
    assert decision["decision"] == "use_filtered"


def test_decide_accepts_missing_optional_clstr_has_clusters(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "1"),
            ("clstr_records", "2"),
            ("total_otus", "1"),
            ("ambiguous_hash_cluster", "0"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "0.2", "fallback_unfiltered")
    assert result.returncode == 0, result.stderr
    decision = _decision_map(result.stdout)
    assert decision["decision"] == "use_filtered"
    assert decision["clstr_has_clusters"] == "NA"


def test_decide_strict_equivalent_fails_when_missing_present(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "1"),
            ("clstr_has_clusters", "1"),
            ("clstr_records", "2"),
            ("total_otus", "1"),
            ("ambiguous_hash_cluster", "0"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "0", "fallback_unfiltered")
    assert result.returncode != 0
    assert "missing fraction" in result.stderr


def test_decide_no_clusters_fallback_unfiltered(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "10"),
            ("clstr_has_clusters", "0"),
            ("clstr_records", "0"),
            ("total_otus", "0"),
            ("ambiguous_hash_cluster", "0"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "0", "fallback_unfiltered")
    assert result.returncode == 0, result.stderr
    decision = _decision_map(result.stdout)
    assert decision["decision"] == "use_unfiltered"
    assert decision["reason"] == "no_clusters_fallback_unfiltered"


def test_decide_no_clusters_fail_policy(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "10"),
            ("clstr_has_clusters", "0"),
            ("clstr_records", "0"),
            ("total_otus", "0"),
            ("ambiguous_hash_cluster", "0"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "1", "fail")
    assert result.returncode != 0
    assert "no OTU cluster member records available" in result.stderr


def test_decide_enforce_fails_on_ambiguous_hash_cluster(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "0"),
            ("clstr_has_clusters", "1"),
            ("clstr_records", "2"),
            ("total_otus", "1"),
            ("ambiguous_hash_cluster", "2"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "1", "fallback_unfiltered")
    assert result.returncode == 0, result.stderr
    decision = _decision_map(result.stdout)
    assert decision["decision"] == "use_unfiltered"
    assert decision["reason"] == "ambiguous_hash_fallback_unfiltered"


def test_decide_header_only_clstr_uses_no_clusters_policy(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "10"),
            ("clstr_has_clusters", "1"),
            ("clstr_records", "0"),
            ("total_otus", "0"),
            ("ambiguous_hash_cluster", "0"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "0", "fallback_unfiltered")
    assert result.returncode == 0, result.stderr
    decision = _decision_map(result.stdout)
    assert decision["decision"] == "use_unfiltered"
    assert decision["reason"] == "no_clusters_fallback_unfiltered"


def test_decide_no_assignable_otus_uses_policy_branch(tmp_path: Path) -> None:
    stats = tmp_path / "stats.tsv"
    _write_stats(
        stats,
        [
            ("total_reads", "10"),
            ("reads_missing_from_clstr", "10"),
            ("clstr_has_clusters", "1"),
            ("clstr_records", "2"),
            ("total_otus", "0"),
            ("ambiguous_hash_cluster", "0"),
            ("missing_policy", "drop"),
        ],
    )
    result = _run(stats, "enforce", "0", "fallback_unfiltered")
    assert result.returncode == 0, result.stderr
    decision = _decision_map(result.stdout)
    assert decision["decision"] == "use_unfiltered"
    assert decision["reason"] == "no_assignable_otus_fallback_unfiltered"
