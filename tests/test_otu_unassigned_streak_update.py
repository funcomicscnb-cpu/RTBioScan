"""Tests for bin/otu_unassigned_streak_update.pl

Covers: streak accumulation, reset on assignment, size filter, OTU key identity,
multi-member OTUs, empty evidence (all unassigned), and sorted output.

After the hash-map fix the script uses stable_key = marker|md5hash for state
so tests that verify cross-round accumulation must supply a hash map and use
the stable key format in prev_state / next_state assertions.
"""
import re
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "otu_unassigned_streak_update.pl"

# Fake 32-char hex hashes for test use
FAKE_HASH_A = "aabbccddaabbccddaabbccddaabbccdd"
FAKE_HASH_B = "11223344112233441122334411223344"
FAKE_HASH_C = "deadbeefdeadbeefdeadbeefdeadbeef"


def _otu_marker(otu: str) -> str:
    """Mirror the Perl otu_marker() logic."""
    m = re.match(r"^OTUB_[^-]+-(.+)$", otu)
    return m.group(1).upper() if m else "NA"


def _stable_key(otu: str, hash_hex: str) -> str:
    """Mirror the Perl stable_key() logic."""
    if not hash_hex:
        return ""
    return f"{_otu_marker(otu)}|{hash_hex.lower()}"


def _w(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _run(
    tmp_path: Path,
    *,
    sizes: str,
    members: str,
    evidence: str,
    prev_state: str,
    min_rounds: int = 3,
    min_size: int = 1,
    max_size: int = 50,
    with_next_state: bool = False,
    hash_map: "dict[str, str] | None" = None,
) -> "tuple[list[str], dict[str, str], dict[str, int]]":
    tmp_path.mkdir(parents=True, exist_ok=True)
    sizes_path   = _w(tmp_path / "sizes.tsv", sizes)
    members_path = _w(tmp_path / "members.tsv", members)
    evid_path    = _w(tmp_path / "evidence.tsv", evidence)
    prev_path    = _w(tmp_path / "prev_state.tsv", prev_state)
    prune_path   = tmp_path / "prune_ids.list"
    stats_path   = tmp_path / "stats.tsv"
    next_state_path = tmp_path / "next_state.tsv"

    cmd = [
        "perl", str(SCRIPT),
        str(sizes_path), str(members_path),
        str(evid_path), str(prev_path),
        str(min_rounds), str(min_size), str(max_size),
        str(prune_path), str(stats_path),
    ]
    if with_next_state:
        cmd.append(str(next_state_path))
    else:
        cmd.append("")   # placeholder so hash_map can be 11th arg

    if hash_map is not None:
        hm_path = tmp_path / "hash_map.tsv"
        hm_path.write_text(
            "".join(f"{uuid}\t{h}\n" for uuid, h in hash_map.items()),
            encoding="utf-8",
        )
        cmd.append(str(hm_path))

    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

    prune_ids = prune_path.read_text().splitlines()

    stats: "dict[str, str]" = {}
    for line in stats_path.read_text().splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            stats[parts[0]] = parts[1]

    next_state: "dict[str, int]" = {}
    if with_next_state and next_state_path.exists():
        for line in next_state_path.read_text().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[1].isdigit():
                next_state[parts[0]] = int(parts[1])

    return prune_ids, stats, next_state


_EVIDENCE_HDR = "read_id\totu_taxid\n"


def _evidence_unassigned(otu_key: str, read_id: str = "") -> str:
    if not read_id:
        read_id = f"uuid-{otu_key}"
    return _EVIDENCE_HDR + f"{read_id}|{otu_key}\tNA\n"


def _evidence_assigned(otu_key: str, taxid: str = "9606", read_id: str = "") -> str:
    if not read_id:
        read_id = f"uuid-{otu_key}"
    return _EVIDENCE_HDR + f"{read_id}|{otu_key}\t{taxid}\n"


# ---------------------------------------------------------------------------
# Basic streak accumulation
# ---------------------------------------------------------------------------

def test_streak_accumulates_across_rounds(tmp_path: Path) -> None:
    otu = "OTUB_1-COI-bc01"
    sizes = f"{otu}\t2\n"
    members = f"{otu}\tuuid1\n{otu}\tuuid2\n"
    evidence = _evidence_unassigned(otu, "uuid1")
    # uuid1 is the representative (in hash map); uuid2 is a plain member
    hm = {"uuid1": FAKE_HASH_A}
    sk = _stable_key(otu, FAKE_HASH_A)

    # Round 1 → streak 1 (below threshold=3)
    prune1, stats1, ns1 = _run(tmp_path / "r1", sizes=sizes, members=members,
                                evidence=evidence, prev_state="",
                                min_rounds=3, min_size=1, max_size=50,
                                with_next_state=True, hash_map=hm)
    assert prune1 == []
    assert ns1[sk] == 1

    # Round 2 → streak 2 (still below)
    prune2, _, ns2 = _run(tmp_path / "r2", sizes=sizes, members=members,
                           evidence=evidence, prev_state=f"{sk}\t1\n",
                           min_rounds=3, min_size=1, max_size=50,
                           with_next_state=True, hash_map=hm)
    assert prune2 == []
    assert ns2[sk] == 2

    # Round 3 → streak 3 (hits threshold)
    prune3, stats3, _ = _run(tmp_path / "r3", sizes=sizes, members=members,
                              evidence=evidence, prev_state=f"{sk}\t2\n",
                              min_rounds=3, min_size=1, max_size=50,
                              hash_map=hm)
    assert sorted(prune3) == ["uuid1", "uuid2"]
    assert stats3["otus_prune_candidate"] == "1"


def test_streak_resets_on_assignment(tmp_path: Path) -> None:
    otu = "OTUB_2-COI-bc01"
    sizes = f"{otu}\t2\n"
    members = f"{otu}\tuuid-a\n"
    # Previously at streak=2, now assigned
    evidence = _evidence_assigned(otu)
    sk = _stable_key(otu, FAKE_HASH_A)
    prune, stats, ns = _run(tmp_path, sizes=sizes, members=members,
                             evidence=evidence, prev_state=f"{sk}\t2\n",
                             min_rounds=3, with_next_state=True,
                             hash_map={"uuid-a": FAKE_HASH_A})
    assert prune == []
    assert sk not in ns
    assert stats["otus_assigned"] == "1"


# ---------------------------------------------------------------------------
# Size filter
# ---------------------------------------------------------------------------

def test_otu_below_min_size_excluded(tmp_path: Path) -> None:
    otu = "OTUB_3-COI-bc01"
    sizes = f"{otu}\t1\n"
    members = f"{otu}\tuuid-b\n"
    evidence = _evidence_unassigned(otu)
    # min_size=2 excludes size-1 OTU — hash map not needed (excluded before hash lookup)
    prune, stats, ns = _run(tmp_path, sizes=sizes, members=members,
                             evidence=evidence, prev_state="",
                             min_size=2, max_size=50, min_rounds=1,
                             with_next_state=True)
    assert prune == []
    assert stats["otus_in_size_range"] == "0"


def test_otu_above_max_size_excluded(tmp_path: Path) -> None:
    otu = "OTUB_4-COI-bc01"
    sizes = f"{otu}\t51\n"
    members = f"{otu}\tuuid-c\n"
    evidence = _evidence_unassigned(otu)
    prune, stats, _ = _run(tmp_path, sizes=sizes, members=members,
                            evidence=evidence, prev_state="",
                            min_size=1, max_size=50, min_rounds=1)
    assert prune == []
    assert stats["otus_in_size_range"] == "0"


def test_otu_at_boundary_included(tmp_path: Path) -> None:
    otu = "OTUB_5-COI-bc01"
    sizes = f"{otu}\t5\n"
    members = f"{otu}\tuuid-d\n"
    evidence = _evidence_unassigned(otu)
    sk = _stable_key(otu, FAKE_HASH_A)
    prune, stats, _ = _run(tmp_path, sizes=sizes, members=members,
                            evidence=evidence, prev_state=f"{sk}\t2\n",
                            min_size=5, max_size=5, min_rounds=3,
                            hash_map={"uuid-d": FAKE_HASH_A})
    assert "uuid-d" in prune
    assert stats["otus_prune_candidate"] == "1"


# ---------------------------------------------------------------------------
# Multi-member OTU: all members emitted
# ---------------------------------------------------------------------------

def test_multi_member_all_uuids_emitted(tmp_path: Path) -> None:
    otu = "OTUB_6-ITS2-bc02"
    sizes = f"{otu}\t3\n"
    members = f"{otu}\tm1\n{otu}\tm2\n{otu}\tm3\n"
    evidence = _evidence_unassigned(otu, "m1")
    sk = _stable_key(otu, FAKE_HASH_A)
    prune, _, _ = _run(tmp_path, sizes=sizes, members=members,
                        evidence=evidence, prev_state=f"{sk}\t2\n",
                        min_rounds=3, hash_map={"m1": FAKE_HASH_A})
    assert sorted(prune) == ["m1", "m2", "m3"]


# ---------------------------------------------------------------------------
# Empty/missing evidence → OTU considered unassigned
# ---------------------------------------------------------------------------

def test_empty_evidence_otu_counted_unassigned(tmp_path: Path) -> None:
    otu = "OTUB_7-COI-bc01"
    sizes = f"{otu}\t2\n"
    members = f"{otu}\tuuid-x\n"
    sk = _stable_key(otu, FAKE_HASH_A)
    prune, stats, ns = _run(tmp_path, sizes=sizes, members=members,
                             evidence="", prev_state=f"{sk}\t2\n",
                             min_rounds=3, with_next_state=True,
                             hash_map={"uuid-x": FAKE_HASH_A})
    assert "uuid-x" in prune
    assert stats["otus_assigned"] == "0"
    assert ns[sk] == 3


# ---------------------------------------------------------------------------
# Mixed scenario: one OTU assigned, one not
# ---------------------------------------------------------------------------

def test_mixed_one_assigned_one_unassigned(tmp_path: Path) -> None:
    otu_a = "OTUB_8-COI-bc01"
    otu_b = "OTUB_9-COI-bc01"
    sizes = f"{otu_a}\t2\n{otu_b}\t2\n"
    members = f"{otu_a}\tread_a\n{otu_b}\tread_b\n"
    evidence = (
        _EVIDENCE_HDR
        + f"read_a|{otu_a}\t9606\n"    # assigned
        + f"read_b|{otu_b}\tNA\n"      # unassigned
    )
    sk_b = _stable_key(otu_b, FAKE_HASH_B)
    prune, stats, ns = _run(tmp_path, sizes=sizes, members=members,
                             evidence=evidence, prev_state=f"{sk_b}\t2\n",
                             min_rounds=3, with_next_state=True,
                             hash_map={"read_a": FAKE_HASH_A, "read_b": FAKE_HASH_B})
    assert "read_a" not in prune
    assert "read_b" in prune
    assert _stable_key(otu_a, FAKE_HASH_A) not in ns
    assert stats["otus_assigned"] == "1"
    assert stats["otus_prune_candidate"] == "1"


# ---------------------------------------------------------------------------
# Sorted output
# ---------------------------------------------------------------------------

def test_prune_ids_are_sorted(tmp_path: Path) -> None:
    otu = "OTUB_10-COI-bc01"
    sizes = f"{otu}\t3\n"
    members = f"{otu}\tzzz\n{otu}\taaa\n{otu}\tmmm\n"
    evidence = _evidence_unassigned(otu, "zzz")
    sk = _stable_key(otu, FAKE_HASH_A)
    prune, _, _ = _run(tmp_path, sizes=sizes, members=members,
                        evidence=evidence, prev_state=f"{sk}\t2\n",
                        min_rounds=3, hash_map={"zzz": FAKE_HASH_A})
    assert prune == sorted(prune)


# ---------------------------------------------------------------------------
# Next-state file not written when not requested
# ---------------------------------------------------------------------------

def test_no_next_state_file_without_arg(tmp_path: Path) -> None:
    otu = "OTUB_11-COI-bc01"
    sizes = f"{otu}\t2\n"
    members = f"{otu}\tuuid-ns\n"
    evidence = _evidence_unassigned(otu)
    _run(tmp_path, sizes=sizes, members=members, evidence=evidence,
         prev_state="", min_rounds=1, with_next_state=False)
    assert not (tmp_path / "next_state.tsv").exists()


# ---------------------------------------------------------------------------
# Legacy state entries (OTU-key format) are silently skipped
# ---------------------------------------------------------------------------

def test_legacy_state_skipped_does_not_prune(tmp_path: Path) -> None:
    """Old OTUB_N-COI state entries must be skipped, not cause a prune."""
    otu = "OTUB_12-COI-bc01"
    sizes = f"{otu}\t2\n"
    members = f"{otu}\tuuid-leg\n"
    evidence = _evidence_unassigned(otu, "uuid-leg")
    sk = _stable_key(otu, FAKE_HASH_A)
    # prev_state in old format → should be skipped → streak starts fresh at 1
    prune, stats, ns = _run(tmp_path, sizes=sizes, members=members,
                             evidence=evidence,
                             prev_state=f"{otu}\t10\n",   # legacy key
                             min_rounds=3, with_next_state=True,
                             hash_map={"uuid-leg": FAKE_HASH_A})
    assert prune == []                   # streak=1, below threshold
    assert ns[sk] == 1                   # fresh start
    assert stats["otus_prune_candidate"] == "0"


# ---------------------------------------------------------------------------
# No hash map provided → OTU skipped (cannot establish stable identity)
# ---------------------------------------------------------------------------

def test_no_hash_map_otus_skipped(tmp_path: Path) -> None:
    """Without a hash map, no stable key can be derived → nothing tracked."""
    otu = "OTUB_13-COI-bc01"
    sizes = f"{otu}\t2\n"
    members = f"{otu}\tuuid-nohm\n"
    evidence = _evidence_unassigned(otu, "uuid-nohm")
    prune, stats, ns = _run(tmp_path, sizes=sizes, members=members,
                             evidence=evidence, prev_state="",
                             min_rounds=1, with_next_state=True)
    assert prune == []
    assert ns == {}
    assert stats["otus_streak_tracked"] == "0"
    assert stats["hash_map_loaded"] == "0"
