"""Unit tests for bin/lib/taxon_util.pl

Serves as the parity regression anchor: defines the shared contract for
is_unassigned_taxon, normalize_taxon, marker_from_token, and trim_text.
"""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = REPO_ROOT / "bin" / "lib" / "taxon_util.pl"


def _perl(expr):
    """Run a perl one-liner that requires taxon_util.pl and prints expr result."""
    code = (
        f'require "{MODULE}"; '
        f'my $r = do {{ {expr} }}; '
        f'print defined $r ? $r : "__undef__";'
    )
    result = subprocess.run(["perl", "-e", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout


# ---------------------------------------------------------------------------
# is_unassigned_taxon
# ---------------------------------------------------------------------------

def test_is_unassigned_empty_string():
    assert _perl('TaxonUtil::is_unassigned_taxon("")') == "1"

def test_is_unassigned_whitespace_only():
    assert _perl('TaxonUtil::is_unassigned_taxon("   ")') == "1"

def test_is_unassigned_na_upper():
    assert _perl('TaxonUtil::is_unassigned_taxon("NA")') == "1"

def test_is_unassigned_na_lower():
    assert _perl('TaxonUtil::is_unassigned_taxon("na")') == "1"

def test_is_unassigned_unassigned_mixed_case():
    assert _perl('TaxonUtil::is_unassigned_taxon("Unassigned")') == "1"

def test_is_unassigned_unassigned_upper():
    assert _perl('TaxonUtil::is_unassigned_taxon("UNASSIGNED")') == "1"

def test_is_unassigned_unassigned_lower():
    assert _perl('TaxonUtil::is_unassigned_taxon("unassigned")') == "1"

def test_is_unassigned_valid_family():
    assert _perl('TaxonUtil::is_unassigned_taxon("Carabidae")') == "0"

def test_is_unassigned_valid_species():
    assert _perl('TaxonUtil::is_unassigned_taxon("Homo_sapiens")') == "0"

def test_is_unassigned_undef():
    # undef → trim_text returns '' → unassigned
    assert _perl('TaxonUtil::is_unassigned_taxon(undef)') == "1"


# ---------------------------------------------------------------------------
# normalize_taxon
# ---------------------------------------------------------------------------

def test_normalize_taxon_assigned():
    assert _perl('TaxonUtil::normalize_taxon("  Carabidae  ")') == "Carabidae"

def test_normalize_taxon_unassigned_returns_undef():
    assert _perl('TaxonUtil::normalize_taxon("Unassigned")') == "__undef__"

def test_normalize_taxon_na_returns_undef():
    assert _perl('TaxonUtil::normalize_taxon("NA")') == "__undef__"

def test_normalize_taxon_empty_returns_undef():
    assert _perl('TaxonUtil::normalize_taxon("")') == "__undef__"


# ---------------------------------------------------------------------------
# marker_from_token
# ---------------------------------------------------------------------------

def test_marker_coi_suffix():
    assert _perl('TaxonUtil::marker_from_token("barcode01-COI")') == "COI"

def test_marker_coi_bare():
    assert _perl('TaxonUtil::marker_from_token("COI")') == "COI"

def test_marker_coi_case_insensitive():
    assert _perl('TaxonUtil::marker_from_token("barcode01-coi")') == "COI"

def test_marker_its2_suffix():
    assert _perl('TaxonUtil::marker_from_token("barcode01-ITS2")') == "ITS2"

def test_marker_its2_case_insensitive():
    assert _perl('TaxonUtil::marker_from_token("sample-its2")') == "ITS2"

def test_marker_other():
    assert _perl('TaxonUtil::marker_from_token("barcode01-16S")') == "OTHER"

def test_marker_empty_returns_undef():
    assert _perl('TaxonUtil::marker_from_token("")') == "__undef__"


# ---------------------------------------------------------------------------
# trim_text
# ---------------------------------------------------------------------------

def test_trim_leading_trailing():
    assert _perl('TaxonUtil::trim_text("  hello  ")') == "hello"

def test_trim_empty():
    assert _perl('TaxonUtil::trim_text("")') == ""

def test_trim_undef_returns_empty():
    assert _perl('TaxonUtil::trim_text(undef)') == ""

def test_trim_no_whitespace():
    assert _perl('TaxonUtil::trim_text("word")') == "word"


# ---------------------------------------------------------------------------
# is_numeric_taxid
# ---------------------------------------------------------------------------

def test_numeric_taxid_positive():
    assert _perl('TaxonUtil::is_numeric_taxid("12345")') == "1"

def test_numeric_taxid_zero():
    assert _perl('TaxonUtil::is_numeric_taxid("0")') == "1"

def test_numeric_taxid_empty():
    assert _perl('TaxonUtil::is_numeric_taxid("")') == "0"

def test_numeric_taxid_na_upper():
    assert _perl('TaxonUtil::is_numeric_taxid("NA")') == "0"

def test_numeric_taxid_na_lower():
    assert _perl('TaxonUtil::is_numeric_taxid("na")') == "0"

def test_numeric_taxid_float():
    assert _perl('TaxonUtil::is_numeric_taxid("1.5")') == "0"

def test_numeric_taxid_text():
    assert _perl('TaxonUtil::is_numeric_taxid("Homo_sapiens")') == "0"

def test_numeric_taxid_whitespace_digits():
    assert _perl('TaxonUtil::is_numeric_taxid("  42  ")') == "1"


# ---------------------------------------------------------------------------
# is_assigned_kingdom
# ---------------------------------------------------------------------------

def test_assigned_kingdom_valid():
    assert _perl('TaxonUtil::is_assigned_kingdom("Animalia")') == "1"

def test_assigned_kingdom_empty():
    assert _perl('TaxonUtil::is_assigned_kingdom("")') == "0"

def test_assigned_kingdom_na():
    assert _perl('TaxonUtil::is_assigned_kingdom("NA")') == "0"

def test_assigned_kingdom_unassigned_lower():
    assert _perl('TaxonUtil::is_assigned_kingdom("unassigned")') == "0"

def test_assigned_kingdom_unassigned_mixed():
    assert _perl('TaxonUtil::is_assigned_kingdom("Unassigned")') == "0"


# ---------------------------------------------------------------------------
# is_assigned_lineage
# ---------------------------------------------------------------------------

def test_assigned_lineage_all_unassigned():
    assert _perl('TaxonUtil::is_assigned_lineage("d__unassigned;p__unassigned;c__unassigned")') == "0"

def test_assigned_lineage_one_real():
    assert _perl('TaxonUtil::is_assigned_lineage("d__Eukaryota;p__unassigned")') == "1"

def test_assigned_lineage_empty():
    assert _perl('TaxonUtil::is_assigned_lineage("")') == "0"

def test_assigned_lineage_na():
    assert _perl('TaxonUtil::is_assigned_lineage("NA")') == "0"

def test_assigned_lineage_unassigned_bare():
    assert _perl('TaxonUtil::is_assigned_lineage("unassigned")') == "0"

def test_assigned_lineage_real():
    assert _perl('TaxonUtil::is_assigned_lineage("Animalia;Arthropoda;Insecta")') == "1"
