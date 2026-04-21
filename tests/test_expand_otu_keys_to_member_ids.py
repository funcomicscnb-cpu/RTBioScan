import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "expand_otu_keys_to_member_ids.pl"

_HASH = "abcdef1234567890abcdef1234567890"


def test_expand_otu_keys_to_member_ids_expands_current_round_members(tmp_path: Path) -> None:
    otu_keys = tmp_path / "protected_otu_keys_round.list"
    otu_keys.write_text("OTUB_1-COI\nOTUB_3-COI\n", encoding="utf-8")
    otu_members = tmp_path / "otu_members_round.tsv"
    otu_members.write_text(
        "otu_id\tread_id\n"
        "OTUB_1-COI\treadA|COI|sup|barcode=s1|adapter=s1\n"
        "OTUB_1-COI\treadB|COI|sup|barcode=s1|adapter=s1\n"
        "OTUB_2-COI\treadC|COI|sup|barcode=s1|adapter=s1\n"
        "OTUB_3-COI\treadD|COI|sup|barcode=s1|adapter=s1\n",
        encoding="utf-8",
    )
    out = tmp_path / "protected_read_ids_round.list"
    stats = tmp_path / "protected_otu_round_stats.tsv"

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(otu_keys), str(otu_members), str(out), str(stats)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["readA", "readB", "readD"]
    stats_map = dict(
        line.split("\t", 1) for line in stats.read_text(encoding="utf-8").splitlines() if "\t" in line
    )
    assert stats_map["protected_otu_keys_round_count"] == "2"
    assert stats_map["protected_otu_member_ids_round_count"] == "3"


def test_expand_otu_keys_matches_via_stable_key(tmp_path: Path) -> None:
    """OTU renumbered from round N to round N+1; stable key (marker|hash) still matches."""
    # Keys list contains a stable key — simulating what blast_assigned_otu_keys.pl now emits
    otu_keys = tmp_path / "protected_otu_keys_ever.list"
    otu_keys.write_text(f"COI|{_HASH}\n", encoding="utf-8")
    # Members file has a *different* OTUB_N (renumbered after re-clustering) but same representative
    otu_members = tmp_path / "otu_members_round.tsv"
    otu_members.write_text(
        "otu_id\tread_id\n"
        "OTUB_7-COI\treadA\n"
        "OTUB_7-COI\treadB\n"
        "OTUB_8-COI\treadC\n",
        encoding="utf-8",
    )
    # Hash map: readA is the representative of OTUB_7-COI (same sequence, different cluster number)
    hash_map = tmp_path / "otu_hash_map.tsv"
    hash_map.write_text(f"readA\t{_HASH}\n", encoding="utf-8")
    out = tmp_path / "protected_ids.list"
    stats = tmp_path / "stats.tsv"

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(otu_keys), str(otu_members), str(out), str(stats), str(hash_map)],
        capture_output=True, text=True, check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["readA", "readB"]
    stats_map = dict(
        line.split("\t", 1) for line in stats.read_text(encoding="utf-8").splitlines() if "\t" in line
    )
    assert stats_map["protected_otu_member_ids_round_count"] == "2"


def test_expand_otu_keys_direct_match_still_works_with_hash_map(tmp_path: Path) -> None:
    """OTUB_N exact key match still works when hash map is provided (backward compat)."""
    otu_keys = tmp_path / "keys.list"
    otu_keys.write_text("OTUB_1-COI\n", encoding="utf-8")
    otu_members = tmp_path / "members.tsv"
    otu_members.write_text(
        "otu_id\tread_id\n"
        "OTUB_1-COI\treadA\n"
        "OTUB_2-COI\treadB\n",
        encoding="utf-8",
    )
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(f"readA\t{_HASH}\n", encoding="utf-8")
    out = tmp_path / "out.list"

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(otu_keys), str(otu_members), str(out), "", str(hash_map)],
        capture_output=True, text=True, check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["readA"]


def test_expand_otu_keys_mixed_stable_and_otub_keys(tmp_path: Path) -> None:
    """Keys list contains a mix of stable keys and OTUB_N keys; both resolve correctly."""
    hash2 = "1234567890abcdef1234567890abcdef"
    otu_keys = tmp_path / "keys.list"
    otu_keys.write_text(f"COI|{_HASH}\nOTUB_9-COI\n", encoding="utf-8")
    otu_members = tmp_path / "members.tsv"
    otu_members.write_text(
        "otu_id\tread_id\n"
        "OTUB_7-COI\treadA\n"   # matched via stable key COI|_HASH
        "OTUB_7-COI\treadB\n"
        "OTUB_9-COI\treadC\n"   # matched via direct OTUB_9-COI key
        "OTUB_10-COI\treadD\n",  # not in keys
        encoding="utf-8",
    )
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(f"readA\t{_HASH}\nreadC\t{hash2}\n", encoding="utf-8")
    out = tmp_path / "out.list"

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(otu_keys), str(otu_members), str(out), "", str(hash_map)],
        capture_output=True, text=True, check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["readA", "readB", "readC"]
