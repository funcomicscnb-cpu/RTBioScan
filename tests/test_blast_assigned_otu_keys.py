import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "blast_assigned_otu_keys.pl"

# 32-char hex MD5 used across tests; readA is the representative of OTUB_1-COI
_HASH = "abcdef1234567890abcdef1234567890"


def test_blast_assigned_otu_keys_emits_unique_assigned_otus(tmp_path: Path) -> None:
    inp = tmp_path / "blast_report_annotated_otu_full.txt"
    out = tmp_path / "blast_assigned_otu_keys_round.list"
    inp.write_text(
        "read_id\totu_id\totu_taxid\tfamily\tgenus\tspecies\n"
        "readA|COI|sup|barcode=s1|adapter=s1|OTUB_1-COI\tOTUB_1-COI\t9606\tMetazoa\tChordata\tSpeciesA\n"
        "readB|COI|sup|barcode=s1|adapter=s1|OTUB_1-COI\tOTUB_1-COI\tNA\tUnassigned\tUnassigned\tUnassigned\n"
        "readC|COI|sup|barcode=s1|adapter=s1|OTUB_2-COI\tOTUB_2-COI\tNA\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["OTUB_1-COI"]


def test_blast_assigned_otu_keys_emits_stable_key_when_hash_map_provided(tmp_path: Path) -> None:
    """With a hash map, assigned OTU key becomes marker|md5hash instead of OTUB_N."""
    inp = tmp_path / "evidence.txt"
    inp.write_text(
        "read_id\totu_id\totu_taxid\tfamily\tgenus\tspecies\n"
        f"readA|COI|sup|OTUB_1-COI\tOTUB_1-COI\t9606\tCarabidae\tCarabus\tCarabus_violaceus\n"
        f"readB|COI|sup|OTUB_1-COI\tOTUB_1-COI\tNA\tUnassigned\tUnassigned\tUnassigned\n"
        f"readC|COI|sup|OTUB_2-COI\tOTUB_2-COI\tNA\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    # Hash map: readA is the representative of OTUB_1-COI
    hash_map = tmp_path / "otu_hash_map.tsv"
    hash_map.write_text(f"readA\t{_HASH}\n", encoding="utf-8")
    out = tmp_path / "assigned_otu_keys.list"

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out), str(hash_map)],
        capture_output=True, text=True, check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == [f"COI|{_HASH}"]


def test_blast_assigned_otu_keys_falls_back_to_otub_when_rep_not_in_hash_map(tmp_path: Path) -> None:
    """If representative UUID is absent from hash map, fall back to OTUB_N key."""
    inp = tmp_path / "evidence.txt"
    inp.write_text(
        "read_id\totu_id\totu_taxid\tfamily\tgenus\tspecies\n"
        "readX|COI|sup|OTUB_5-COI\tOTUB_5-COI\t9606\tCarabidae\tCarabus\tCarabus_violaceus\n",
        encoding="utf-8",
    )
    # Hash map does NOT contain readX
    hash_map = tmp_path / "otu_hash_map.tsv"
    hash_map.write_text("otherRead\tabcdef1234567890abcdef1234567891\n", encoding="utf-8")
    out = tmp_path / "assigned_otu_keys.list"

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out), str(hash_map)],
        capture_output=True, text=True, check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["OTUB_5-COI"]


def test_blast_assigned_otu_keys_no_hash_map_arg_still_works(tmp_path: Path) -> None:
    """When hash_map arg is omitted the script behaves identically to the old code."""
    inp = tmp_path / "evidence.txt"
    inp.write_text(
        "read_id\totu_id\totu_taxid\tfamily\tgenus\tspecies\n"
        "readA|COI|sup|OTUB_3-COI\tOTUB_3-COI\t1234\tCarabidae\tCarabus\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "assigned_otu_keys.list"

    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out)],
        capture_output=True, text=True, check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["OTUB_3-COI"]


# ---------------------------------------------------------------------------
# --min-level genus and species
# ---------------------------------------------------------------------------

def test_blast_assigned_genus_level_family_only_not_included(tmp_path: Path) -> None:
    """At genus level, OTU with only family assigned is NOT emitted."""
    inp = tmp_path / "evidence.txt"
    inp.write_text(
        "read_id\totu_id\tfamily\tgenus\tspecies\n"
        "readA|COI|sup|OTUB_1-COI\tOTUB_1-COI\tCarabidae\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.list"
    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out), "--min-level", "genus"],
        capture_output=True, text=True, check=False,
    )
    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == []


def test_blast_assigned_genus_level_genus_included(tmp_path: Path) -> None:
    """At genus level, OTU with genus assigned IS emitted."""
    inp = tmp_path / "evidence.txt"
    inp.write_text(
        "read_id\totu_id\tfamily\tgenus\tspecies\n"
        "readA|COI|sup|OTUB_1-COI\tOTUB_1-COI\tCarabidae\tCarabus\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.list"
    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out), "--min-level", "genus"],
        capture_output=True, text=True, check=False,
    )
    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["OTUB_1-COI"]


def test_blast_assigned_species_level_genus_only_not_included(tmp_path: Path) -> None:
    """At species level, OTU with only genus assigned is NOT emitted."""
    inp = tmp_path / "evidence.txt"
    inp.write_text(
        "read_id\totu_id\tfamily\tgenus\tspecies\n"
        "readA|COI|sup|OTUB_1-COI\tOTUB_1-COI\tCarabidae\tCarabus\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.list"
    cp = subprocess.run(
        ["perl", str(SCRIPT), str(inp), str(out), "--min-level", "species"],
        capture_output=True, text=True, check=False,
    )
    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == []
