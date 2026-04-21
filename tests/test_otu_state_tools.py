import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO_ROOT / "bin" / "otu_state_preflight.pl"
NORMALIZE = REPO_ROOT / "bin" / "otu_state_normalize.pl"


def test_preflight_strict_rejects_mixed_member_ids(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "otu_frozen_members.tsv").write_text(
        "FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\treadA|COI|sup|barcode=s1|adapter=s1\t1\n"
        "FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\treadA\t0\n",
        encoding="utf-8",
    )
    (state / "otu_frozen_meta.tsv").write_text(
        "FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\treadA|COI|sup|barcode=s1|adapter=s1\th1\n",
        encoding="utf-8",
    )
    report = tmp_path / "preflight.tsv"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            ["perl", str(PREFLIGHT), str(state), "strict", str(report)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_preflight_legacy_allows_but_reports_issues(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "otu_frozen_members.tsv").write_text(
        "FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|legacy\treadA\t1\n",
        encoding="utf-8",
    )
    report = tmp_path / "preflight.tsv"
    subprocess.run(
        ["perl", str(PREFLIGHT), str(state), "legacy", str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    kv = dict(
        ln.strip().split("\t", 1)
        for ln in report.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    )
    assert kv["noncanonical_frozen_ids_members"] == "1"


def test_preflight_strict_rejects_malformed_active_pool_headers(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "otu_active_pool.fasta").write_text(
        ">\n"
        "ACGT\n",
        encoding="utf-8",
    )
    report = tmp_path / "preflight.tsv"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            ["perl", str(PREFLIGHT), str(state), "strict", str(report)],
            check=True,
            capture_output=True,
            text=True,
        )


def test_preflight_legacy_reports_malformed_active_pool_headers(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "otu_active_pool.fasta").write_text(
        ">\n"
        "ACGT\n",
        encoding="utf-8",
    )
    report = tmp_path / "preflight.tsv"
    subprocess.run(
        ["perl", str(PREFLIGHT), str(state), "legacy", str(report)],
        check=True,
        capture_output=True,
        text=True,
    )
    kv = dict(
        ln.strip().split("\t", 1)
        for ln in report.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    )
    assert kv["active_pool_malformed_ids"] == "1"


def test_normalize_members_canonicalizes_and_deduplicates(tmp_path: Path) -> None:
    members_in = tmp_path / "members_in.tsv"
    members_out = tmp_path / "members_out.tsv"
    members_in.write_text(
        "FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|old\treadA|COI|sup\t1\n"
        "FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\treadA|COI|sup\t0\n"
        "FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\treadB|COI|sup\t0\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["perl", str(NORMALIZE), str(members_in), str(members_out)],
        check=True,
        capture_output=True,
        text=True,
    )
    rows = [ln.strip().split("\t") for ln in members_out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows == [
        ["FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "readA|COI|sup", "1"],
        ["FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "readB|COI|sup", "0"],
    ]
