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


def audit_row(
    reference_id,
    family,
    pident="88.000",
    coverage="95.000",
    discovery_tier="review",
):
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
        "discovery_tier": discovery_tier,
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
        for row in adjudicator.build_adjudications(rows, rows, set(), edges)
    }

    assert result["opaque_a"]["disposition"] == (
        "cross_family_sequence_label_conflict_candidate"
    )
    assert result["opaque_b"]["disposition"] == (
        "cross_family_sequence_label_conflict_candidate"
    )
    assert result["opaque_c"]["disposition"] == (
        "unresolved_insufficient_local_discriminator"
    )


def test_confirmed_anchor_can_support_review_row_without_becoming_output():
    adjudicator = load_module()
    review = audit_row("opaque_review", "FamilyOne")
    anchor = audit_row(
        "opaque_anchor",
        "FamilyTwo",
        pident="99.000",
        coverage="100.000",
        discovery_tier="priority",
    )
    comparison_rows = [review, anchor]
    by_id = {row["reference_id"]: row for row in comparison_rows}
    blast = "\n".join(
        [
            "opaque_review\t100\topaque_anchor\t100\t96.000\t95\t1e-30\t150",
            "opaque_anchor\t100\topaque_review\t100\t96.000\t95\t1e-30\t150",
        ]
    )

    edges = adjudicator.parse_edges(blast, by_id, 95.0, 80.0)
    result = adjudicator.build_adjudications(
        [review], comparison_rows, {"opaque_anchor"}, edges
    )

    assert [row["reference_id"] for row in result] == ["opaque_review"]
    assert result[0]["disposition"] == (
        "cross_family_sequence_label_conflict_candidate"
    )
    assert result[0]["cross_family_reference_id"] == "opaque_anchor"
    assert result[0]["cross_family_reference_role"] == "confirmed_anchor"


def test_anchor_manifest_rejects_nonconfirmed_rows(tmp_path):
    adjudicator = load_module()
    manifest = tmp_path / "anchors.tsv"
    manifest.write_text(
        "reference_id\tevidence_status\n"
        "confirmed_anchor\tconfirmed\n"
        "pending_candidate\tcandidate_pending_adjudication\n",
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="contains non-confirmed rows"):
        adjudicator.read_confirmed_anchor_ids(manifest)


def test_committed_local_adjudication_has_declared_outcomes():
    path = FIXTURE_DIR / "chain_a_lower_tier_adjudication.tsv"
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    assert len(rows) == 39
    assert len({row["reference_id"] for row in rows}) == 39
    conflicts = [
        row
        for row in rows
        if row["disposition"] == "cross_family_sequence_label_conflict_candidate"
    ]
    unresolved = [row for row in rows if row["disposition"].startswith("unresolved_")]
    assert len(conflicts) == 24
    assert len(unresolved) == 15
    for row in conflicts:
        assert row["header_family"] != row["cross_family_reference_family"]
        assert row["cross_family_reference_role"] in {
            "confirmed_anchor",
            "review_candidate",
        }
        assert float(row["cross_family_pident"]) >= 95
        assert float(row["cross_family_shorter_sequence_coverage"]) >= 80
        assert int(row["component_size"]) > 1
        assert int(row["component_family_count"]) > 1
    for row in unresolved:
        assert row["cross_family_reference_id"] == ""

    component_sizes = {
        row["component_id"]: int(row["component_size"])
        for row in conflicts
    }
    assert Counter(component_sizes.values()) == Counter({2: 2, 3: 2, 16: 1})
    assert Counter(row["cross_family_reference_role"] for row in conflicts) == Counter(
        {"review_candidate": 23, "confirmed_anchor": 1}
    )

    by_id = {row["reference_id"]: row for row in rows}
    cross_tier = by_id["BOLD_COI-5P_GMODL3842-22"]
    assert cross_tier["cross_family_reference_id"] == (
        "BOLD_COI-5P_GBMIN70259-17"
    )
    assert cross_tier["cross_family_reference_family"] == "Triozidae"
    assert cross_tier["cross_family_reference_role"] == "confirmed_anchor"
    assert cross_tier["cross_family_pident"] == "96.018"
    assert cross_tier["cross_family_alignment_length"] == "452"
    assert cross_tier["cross_family_shorter_sequence_coverage"] == "95.763"
    assert cross_tier["component_id"] == "cluster_8fef88fc9b07"
    assert cross_tier["component_size"] == "3"


def test_committed_local_adjudication_provenance_matches_bytes():
    output = FIXTURE_DIR / "chain_a_lower_tier_adjudication.tsv"
    provenance = FIXTURE_DIR / "chain_a_lower_tier_adjudication_provenance.tsv"
    with provenance.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    values = {(row["field"], row["artifact"]): row["value"] for row in rows}

    assert values[("schema", "")] == "chain_a_local_cluster_adjudication_v2"
    assert values[("parameter", "output_scope")] == "review_only"
    assert values[("parameter", "component_scope")] == (
        "review_plus_confirmed_anchors"
    )
    assert values[("count", "all_review_records")] == "39"
    assert values[("count", "confirmed_anchor_records")] == "5"
    assert values[("count", "all_comparison_records")] == "44"
    assert values[("count", "cross_family_conflict_candidates")] == "24"
    assert values[("count", "selected_review_candidate_neighbors")] == "23"
    assert values[("count", "selected_confirmed_anchor_neighbors")] == "1"
    assert values[("count", "unresolved_records")] == "15"
    anchor_manifest = FIXTURE_DIR / "chain_a_reference_candidates.tsv"
    anchor_digest = hashlib.sha256(anchor_manifest.read_bytes()).hexdigest()
    assert values[("sha256", str(anchor_manifest.relative_to(REPO_ROOT)))] == (
        anchor_digest
    )
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
            "--confirmed-anchor-manifest",
            str(FIXTURE_DIR / "chain_a_reference_candidates.tsv"),
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
