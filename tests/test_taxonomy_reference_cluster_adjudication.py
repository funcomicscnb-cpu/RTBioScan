import csv
import hashlib
import importlib.util
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "adjudicate_taxonomy_reference_clusters.py"
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"


def load_module():
    spec = importlib.util.spec_from_file_location("reference_cluster_adjudication", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def audit_row(reference_id, family, pident="88.000", coverage="95.000"):
    return {
        "query_id": "control",
        "source_accession": "control.1",
        "reference_id": reference_id,
        "stored_taxid": "1",
        "header_lineage": f"k__Metazoa;c__Insecta;o__Order;f__{family}",
        "reference_sequence_sha256": hashlib.sha256(reference_id.encode()).hexdigest(),
        "pident": pident,
        "alignment_length": "100",
        "shorter_sequence_coverage": coverage,
        "discovery_tier": "review",
        "review_status": "pending_adjudication",
    }


def test_cross_family_rule_is_name_independent_and_conservative():
    adjudicator = load_module()
    rows = [
        audit_row("opaque_a", "FamilyOne"),
        audit_row("opaque_b", "FamilyTwo"),
        audit_row("opaque_c", "FamilyOne"),
    ]
    by_id = {row["reference_id"]: row for row in rows}
    blast = "\n".join(
        [
            "opaque_a\t100\topaque_b\t100\t95.000\t95\t1e-30\t150",
            "opaque_b\t100\topaque_a\t100\t95.000\t95\t1e-30\t150",
            "opaque_a\t100\topaque_c\t100\t99.000\t99\t1e-40\t180",
            "opaque_c\t100\topaque_a\t100\t99.000\t99\t1e-40\t180",
        ]
    )

    edges = adjudicator.parse_edges(blast, by_id, 95.0, 80.0)
    result = {
        row["reference_id"]: row
        for row in adjudicator.build_adjudications(rows, edges)
    }

    assert result["opaque_a"]["disposition"].startswith("confirmed_")
    assert result["opaque_b"]["disposition"].startswith("confirmed_")
    assert result["opaque_c"]["disposition"] == (
        "unresolved_insufficient_local_discriminator"
    )


def test_committed_local_adjudication_has_declared_outcomes():
    path = FIXTURE_DIR / "chain_a_lower_tier_adjudication.tsv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    assert len(rows) == 39
    assert len({row["reference_id"] for row in rows}) == 39
    confirmed = [row for row in rows if row["disposition"].startswith("confirmed_")]
    unresolved = [row for row in rows if row["disposition"].startswith("unresolved_")]
    assert len(confirmed) == 23
    assert len(unresolved) == 16
    for row in confirmed:
        assert row["header_family"] != row["cross_family_reference_family"]
        assert float(row["cross_family_pident"]) >= 95
        assert float(row["cross_family_shorter_sequence_coverage"]) >= 80
        assert int(row["component_size"]) > 1
        assert int(row["component_family_count"]) > 1
    for row in unresolved:
        assert row["cross_family_reference_id"] == ""
        assert row["component_id"] == "singleton"

    component_sizes = {
        row["component_id"]: int(row["component_size"])
        for row in confirmed
    }
    assert Counter(component_sizes.values()) == Counter({2: 2, 3: 1, 16: 1})


def test_committed_local_adjudication_provenance_matches_bytes():
    output = FIXTURE_DIR / "chain_a_lower_tier_adjudication.tsv"
    provenance = FIXTURE_DIR / "chain_a_lower_tier_adjudication_provenance.tsv"
    with provenance.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    values = {(row["field"], row["artifact"]): row["value"] for row in rows}

    assert values[("parameter", "scope")] == "local_all_vs_all_only"
    assert values[("count", "all_review_records")] == "39"
    assert values[("count", "confirmed_records")] == "23"
    assert values[("count", "unresolved_records")] == "16"
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    assert values[("sha256", str(output.relative_to(REPO_ROOT)))] == digest


def test_local_adjudication_regenerates_exact_rows(tmp_path):
    blastn = shutil.which("blastn")
    reference_fasta = REPO_ROOT / "db" / "COInr98_2024Jun_RioNegro_Brazil.fasta"
    if blastn is None or not reference_fasta.is_file():
        pytest.skip("blastn and the COI source FASTA are required for local replay")
    output = tmp_path / "adjudication.tsv"
    provenance = tmp_path / "provenance.tsv"

    subprocess.run(
        [
            str(SCRIPT),
            "--audit",
            str(FIXTURE_DIR / "chain_a_similarity_audit.tsv"),
            "--reference-source-fasta",
            str(reference_fasta),
            "--output",
            str(output),
            "--provenance-output",
            str(provenance),
            "--blastn",
            blastn,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert output.read_bytes() == (
        FIXTURE_DIR / "chain_a_lower_tier_adjudication.tsv"
    ).read_bytes()
