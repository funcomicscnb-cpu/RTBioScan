import importlib.util
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "extract_frozen_tax_time.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("extract_frozen_tax_time", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_extract_frozen_counts_uses_frozen_reads_not_row_total_reads() -> None:
    mod = _load_module()
    assignments_by_level = {
        "species": [
            {
                "taxon": "Sorghum",
                "family": "Poaceae",
                "genus": "Sorghum",
                "marker": "ITS2",
                "frozen_otu_count": 1,
                "reads_total": 14,
                "otu_reads_sample_total": 14,
                "frozen_otu_reads_sample_total": 8,
            },
        ],
        "genus": [
            {
                "taxon": "Sorghum",
                "family": "Poaceae",
                "marker": "ITS2",
                "frozen_otu_count": 1,
                "reads_total": 14,
                "frozen_otu_reads_sample_total": 8,
            },
        ],
        "family": [
            {
                "taxon": "Poaceae",
                "marker": "ITS2",
                "frozen_otu_count": 1,
                "reads_total": 14,
                "frozen_otu_reads_sample_total": 8,
            },
        ],
    }

    _species, _genus, _family, reads_by_rank_marker = mod.extract_frozen_counts(
        assignments_by_level,
        schema_version="2.0",
    )

    assert reads_by_rank_marker[("species", "ITS2")] == 8
    assert reads_by_rank_marker[("genus", "ITS2")] == 8
    assert reads_by_rank_marker[("family", "ITS2")] == 8


def test_extract_frozen_counts_uses_unsuffixed_frozen_reads_for_schema_2_only() -> None:
    mod = _load_module()
    assignments_by_level = {
        "species": [
            {
                "taxon": "Sorghum",
                "marker": "ITS2",
                "frozen_otu_count": 1,
                "reads_total": 14,
                "frozen_otu_reads_total": 8,
            },
        ],
    }

    _species, _genus, _family, reads_by_rank_marker = mod.extract_frozen_counts(
        assignments_by_level,
        schema_version="2.0",
    )

    assert reads_by_rank_marker[("species", "ITS2")] == 8


def test_extract_frozen_counts_does_not_use_ambiguous_unsuffixed_legacy_reads() -> None:
    mod = _load_module()
    assignments_by_level = {
        "species": [
            {
                "taxon": "Sorghum",
                "marker": "ITS2",
                "frozen_otu_count": 1,
                "reads_total": 14,
                "frozen_otu_reads_total": 313,
            },
        ],
    }

    _species, _genus, _family, reads_by_rank_marker = mod.extract_frozen_counts(
        assignments_by_level,
        schema_version="1.7",
    )

    assert reads_by_rank_marker.get(("species", "ITS2"), 0) == 0
