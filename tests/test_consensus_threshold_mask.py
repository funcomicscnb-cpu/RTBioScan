from pathlib import Path
import subprocess


def _run_mask(tmp_path: Path, levels: str, tax_join: str) -> str:
    levels_path = tmp_path / "levels.tsv"
    tax_path = tmp_path / "tax_join.tsv"
    levels_path.write_text(levels, encoding="utf-8")
    tax_path.write_text(tax_join, encoding="utf-8")

    script = Path(__file__).resolve().parents[1] / "bin" / "consensus_threshold_mask.sh"
    result = subprocess.run(
        ["bash", str(script), str(levels_path), str(tax_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


# tax_join columns: idx, qseqid, sseqid, taxid, kingdom, phylum, class, order, family, genus, species
_TAX_JOIN = "\n".join([
    "1\tq1\ts1\t123\tK1\tP1\tC1\tO1\tfamily_a\tgenus_a\tspecies_a",
    "2\tq2\ts2\t456\tK2\tP2\tC2\tO2\tfamily_a\tgenus_a\tspecies_a",
    "3\tq3\ts3\t789\tK3\tP3\tC3\tO3\tfamily_b\tgenus_b\tspecies_b",
    "4\tq4\ts4\t999\tK4\tP4\tC4\tO4\tfamily_c\tgenus_c\tspecies_c",
    "5\tq5\ts5\t111\tK5\tP5\tC5\tO5\tfamily_d\tgenus_d\tspecies_d",
]) + "\n"


def _rows(out: str) -> dict[str, list[str]]:
    result = {}
    for line in out.strip().splitlines():
        cols = line.split("\t")
        result[cols[0]] = cols
    return result


def test_species_level_keeps_all_ranks(tmp_path: Path) -> None:
    levels = "q1\tspecies\n"
    rows = _rows(_run_mask(tmp_path, levels, _TAX_JOIN))
    assert rows["q1"][6:9] == ["family_a", "genus_a", "species_a"]


def test_genus_level_masks_species(tmp_path: Path) -> None:
    levels = "q2\tgenus\n"
    rows = _rows(_run_mask(tmp_path, levels, _TAX_JOIN))
    assert rows["q2"][6:9] == ["family_a", "genus_a", "Unassigned"]


def test_family_level_masks_genus_and_species(tmp_path: Path) -> None:
    levels = "q3\tfamily\n"
    rows = _rows(_run_mask(tmp_path, levels, _TAX_JOIN))
    assert rows["q3"][6:9] == ["family_b", "Unassigned", "Unassigned"]


def test_unassigned_level_masks_all_three_ranks(tmp_path: Path) -> None:
    levels = "q4\tunassigned\n"
    rows = _rows(_run_mask(tmp_path, levels, _TAX_JOIN))
    assert rows["q4"][6:9] == ["Unassigned", "Unassigned", "Unassigned"]


def test_missing_level_entry_masks_all_three_ranks(tmp_path: Path) -> None:
    """qseqid absent from levels.tsv => unknown => unassigned (no leak)."""
    levels = ""
    rows = _rows(_run_mask(tmp_path, levels, _TAX_JOIN))
    assert rows["q3"][6:9] == ["Unassigned", "Unassigned", "Unassigned"]


def test_unknown_level_string_masks_all_three_ranks(tmp_path: Path) -> None:
    """An unrecognised level token (e.g. a bare rank name) => unassigned."""
    levels = "q5\tgenus_d\n"  # old-style rank name, not a canonical level token
    rows = _rows(_run_mask(tmp_path, levels, _TAX_JOIN))
    assert rows["q5"][6:9] == ["Unassigned", "Unassigned", "Unassigned"]


def test_multiple_queries_mixed_levels(tmp_path: Path) -> None:
    levels = "q1\tspecies\nq2\tgenus\nq3\tfamily\nq4\tunassigned\n"
    rows = _rows(_run_mask(tmp_path, levels, _TAX_JOIN))
    assert rows["q1"][6:9] == ["family_a", "genus_a", "species_a"]
    assert rows["q2"][6:9] == ["family_a", "genus_a", "Unassigned"]
    assert rows["q3"][6:9] == ["family_b", "Unassigned", "Unassigned"]
    assert rows["q4"][6:9] == ["Unassigned", "Unassigned", "Unassigned"]
