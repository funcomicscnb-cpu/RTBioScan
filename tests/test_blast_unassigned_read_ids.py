"""Tests for bin/blast_unassigned_read_ids.pl

Verifies that the script emits exactly the read IDs whose OTU is NOT assigned.
Assignment is determined by family/genus/species taxonomy columns, with
configurable --min-level (default: family).
"""
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "blast_unassigned_read_ids.pl"


def _run(tsv_content: str, min_level: str | None = None) -> list[str]:
    with tempfile.TemporaryDirectory() as d:
        inp = Path(d) / "evidence.tsv"
        out = Path(d) / "unassigned.list"
        inp.write_text(tsv_content)
        cmd = ["perl", str(SCRIPT), str(inp), str(out)]
        if min_level is not None:
            cmd += ["--min-level", min_level]
        result = subprocess.run(cmd, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return out.read_text().splitlines()


def _header(*extra_cols):
    base = "read_id\tfamily\tgenus\tspecies"
    if extra_cols:
        return base + "\t" + "\t".join(extra_cols)
    return base


# ---------------------------------------------------------------------------
# Basic assignment cases (family level = default)
# ---------------------------------------------------------------------------

def test_empty_file_produces_empty_output():
    assert _run("") == []


def test_assigned_by_family_not_emitted():
    tsv = _header() + "\nread1\tCarabidae\tUnassigned\tUnassigned\n"
    assert _run(tsv, min_level="family") == []


def test_assigned_by_genus_not_emitted():
    """Genus non-empty → read protected at default (genus) level."""
    tsv = _header() + "\nread1\tUnassigned\tCarabus\tUnassigned\n"
    assert _run(tsv) == []


def test_unassigned_all_empty_emitted():
    tsv = _header() + "\nread1\t\t\t\n"
    assert _run(tsv) == ["read1"]


def test_unassigned_all_na_emitted():
    tsv = _header() + "\nread1\tNA\tNA\tNA\n"
    assert _run(tsv) == ["read1"]


def test_unassigned_all_unassigned_label_emitted():
    tsv = _header() + "\nread1\tUnassigned\tUnassigned\tUnassigned\n"
    assert _run(tsv) == ["read1"]


def test_mixed_assigned_and_unassigned():
    tsv = (
        _header()
        + "\nassigned_read\tCarabidae\tCarabus\tUnassigned"
        + "\nunassigned_read\t\t\t\n"
    )
    result = _run(tsv)
    assert "assigned_read" not in result
    assert "unassigned_read" in result


# ---------------------------------------------------------------------------
# --min-level genus
# ---------------------------------------------------------------------------

def test_min_level_genus_family_only_not_protected():
    """At genus level, family-only assignment is insufficient → emitted."""
    tsv = _header() + "\nread1\tCarabidae\tUnassigned\tUnassigned\n"
    assert _run(tsv, min_level="genus") == ["read1"]


def test_min_level_genus_genus_assigned_not_emitted():
    """At genus level, genus non-empty → assigned → not emitted."""
    tsv = _header() + "\nread1\tCarabidae\tCarabus\tUnassigned\n"
    assert _run(tsv, min_level="genus") == []


def test_min_level_genus_species_satisfies_genus_level():
    """At genus level, species non-empty satisfies genus-or-better → not emitted."""
    tsv = _header() + "\nread1\tUnassigned\tUnassigned\tCarabus_violaceus\n"
    assert _run(tsv, min_level="genus") == []


# ---------------------------------------------------------------------------
# --min-level species
# ---------------------------------------------------------------------------

def test_min_level_species_genus_only_not_protected():
    """At species level, genus-only assignment is insufficient → emitted."""
    tsv = _header() + "\nread1\tCarabidae\tCarabus\tUnassigned\n"
    assert _run(tsv, min_level="species") == ["read1"]


def test_min_level_species_species_assigned_not_emitted():
    """At species level, species non-empty → assigned → not emitted."""
    tsv = _header() + "\nread1\tCarabidae\tCarabus\tCarabus_violaceus\n"
    assert _run(tsv, min_level="species") == []


# ---------------------------------------------------------------------------
# First-assigned-wins: if any row for a read is assigned, it is not emitted
# ---------------------------------------------------------------------------

def test_assigned_row_wins_over_unassigned_row():
    # read1 appears twice: first row unassigned, second row assigned at genus level
    tsv = _header() + "\nread1\t\t\t\nread1\tCarabidae\tCarabus\tUnassigned\n"
    assert _run(tsv) == []


def test_all_rows_unassigned_read_still_emitted_once():
    tsv = _header() + "\nread1\tNA\tNA\tNA\nread1\t\t\t\n"
    result = _run(tsv)
    assert result == ["read1"]


# ---------------------------------------------------------------------------
# read_id pipe-splitting (UUID extraction)
# ---------------------------------------------------------------------------

def test_uuid_extracted_from_pipe_delimited_read_id():
    tsv = _header() + "\nuuid-abc|OTUB_5-COI-bc01\t\t\t\n"
    assert _run(tsv) == ["uuid-abc"]


# ---------------------------------------------------------------------------
# No-header fallback
# ---------------------------------------------------------------------------

def test_no_header_round_schema_still_detects_assigned_rows():
    """In no-header mode, the script falls back to read/taxid/lineage positions."""
    tsv = (
        "assigned_read\t92558\tK__Metazoa;p__Arthropoda;c__Insecta;o__Diptera;f__Dolichopodidae;g__;s__\n"
        "unassigned_read\tNA\tK__Unassigned;p__Unassigned;c__Unassigned;o__Unassigned;f__Unassigned;g__Unassigned;s__Unassigned\n"
    )
    assert _run(tsv) == ["unassigned_read"]


# ---------------------------------------------------------------------------
# Sorted output
# ---------------------------------------------------------------------------

def test_output_is_sorted():
    tsv = _header() + "\nzread\t\t\t\naread\t\t\t\nmread\t\t\t\n"
    assert _run(tsv) == ["aread", "mread", "zread"]
