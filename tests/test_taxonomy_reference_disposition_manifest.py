import csv
import hashlib
import importlib.util
import subprocess
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "build_taxonomy_reference_disposition_manifest.py"
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"
POLICY = FIXTURE_DIR / "chain_a_downstream_coi_release_policy_v1.tsv"
MANIFEST = FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1.tsv"
QUARANTINE = FIXTURE_DIR / "chain_a_downstream_coi_quarantine_v1.tsv"
RETAINED = FIXTURE_DIR / "chain_a_downstream_coi_retained_unresolved_v1.tsv"
PROVENANCE = FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1_provenance.tsv"


def load_module():
    spec = importlib.util.spec_from_file_location("reference_disposition", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_tsv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def opaque_inputs():
    audit = {}
    for reference_id, tier in (
        ("opaque_priority", "priority"),
        ("opaque_conflict", "review"),
        ("opaque_unresolved", "review"),
    ):
        audit[reference_id] = {
            "reference_id": reference_id,
            "reference_sequence_sha256": hashlib.sha256(reference_id.encode()).hexdigest(),
            "stored_taxid": "-7" if reference_id == "opaque_conflict" else "7",
            "header_kingdom": "Metazoa",
            "header_lineage": "k__Metazoa;f__Opaque",
            "source_accession": "control.1",
            "discovery_tier": tier,
        }
    confirmed = [
        {
            "reference_id": "opaque_priority",
            "evidence_status": "confirmed",
        }
    ]
    lower = [
        {
            "reference_id": "opaque_conflict",
            "disposition": "cross_family_sequence_label_conflict_candidate",
            "cross_family_reference_id": "opaque_priority",
            "cross_family_reference_role": "confirmed_anchor",
        },
        {
            "reference_id": "opaque_unresolved",
            "disposition": "unresolved_insufficient_local_discriminator",
            "cross_family_reference_id": "",
            "cross_family_reference_role": "",
        },
    ]
    return audit, confirmed, lower


def policy_mappings(conflict_action="quarantine", unresolved_action="retain"):
    policy_id = "opaque_policy_v1"
    rows = [
        {
            "policy_id": policy_id,
            "source_tier": "priority",
            "source_status": "confirmed",
            "evidence_class": "confirmed_reference_sequence_contamination",
            "release_action": "quarantine",
            "decision_basis": "confirmed_evidence",
        },
        {
            "policy_id": policy_id,
            "source_tier": "review",
            "source_status": "cross_family_sequence_label_conflict_candidate",
            "evidence_class": "cross_family_sequence_label_conflict_candidate",
            "release_action": conflict_action,
            "decision_basis": "explicit_conflict_policy",
        },
        {
            "policy_id": policy_id,
            "source_tier": "review",
            "source_status": "unresolved_insufficient_local_discriminator",
            "evidence_class": "unresolved_insufficient_local_discriminator",
            "release_action": unresolved_action,
            "decision_basis": "preserve_uncertainty",
        },
    ]
    return policy_id, {
        (row["source_tier"], row["source_status"]): row for row in rows
    }


def test_release_action_is_policy_driven_but_unresolved_must_be_retained():
    builder = load_module()
    audit, confirmed, lower = opaque_inputs()
    policy_id, mappings = policy_mappings(conflict_action="retain")

    rows = builder.build_dispositions(audit, confirmed, lower, policy_id, mappings)
    by_id = {row["reference_id"]: row for row in rows}

    assert by_id["opaque_priority"]["release_action"] == "quarantine"
    assert by_id["opaque_conflict"]["release_action"] == "retain"
    assert by_id["opaque_conflict"]["evidence_class"] == (
        "cross_family_sequence_label_conflict_candidate"
    )
    assert by_id["opaque_unresolved"]["release_action"] == "retain"
    assert by_id["opaque_conflict"]["stored_taxid"] == "-7"

    _, unsafe = policy_mappings(unresolved_action="quarantine")
    with pytest.raises(SystemExit, match="must retain unresolved"):
        builder.build_dispositions(audit, confirmed, lower, policy_id, unsafe)


def test_conflict_support_resolves_by_role_and_cannot_be_self_referential():
    builder = load_module()
    _, confirmed, lower = opaque_inputs()
    builder.validate_support_references(confirmed, lower)

    missing = [dict(row) for row in lower]
    missing[0]["cross_family_reference_id"] = "missing_review_peer"
    missing[0]["cross_family_reference_role"] = "review_candidate"
    with pytest.raises(SystemExit, match="not in the lower-tier set"):
        builder.validate_support_references(confirmed, missing)

    wrong_role = [dict(row) for row in lower]
    wrong_role[0]["cross_family_reference_role"] = "review_candidate"
    with pytest.raises(SystemExit, match="not in the lower-tier set"):
        builder.validate_support_references(confirmed, wrong_role)

    unresolved_support = [dict(row) for row in lower]
    unresolved_support[0]["cross_family_reference_id"] = "opaque_unresolved"
    unresolved_support[0]["cross_family_reference_role"] = "review_candidate"
    with pytest.raises(SystemExit, match="not a conflict candidate"):
        builder.validate_support_references(confirmed, unresolved_support)

    self_link = [dict(row) for row in lower]
    self_link[0]["cross_family_reference_id"] = "opaque_conflict"
    self_link[0]["cross_family_reference_role"] = "review_candidate"
    with pytest.raises(SystemExit, match="cannot support itself"):
        builder.validate_support_references(confirmed, self_link)


def test_upstream_provenance_rejects_changed_source_snapshot(tmp_path):
    builder = load_module()
    artifacts = {
        "audit": tmp_path / "audit.tsv",
        "confirmed": tmp_path / "confirmed.tsv",
        "lower": tmp_path / "lower.tsv",
        "source": tmp_path / "source.fasta",
    }
    for role, path in artifacts.items():
        path.write_text(f"{role}\n", encoding="utf-8")

    audit_provenance = tmp_path / "audit_provenance.tsv"
    lower_provenance = tmp_path / "lower_provenance.tsv"

    def write_provenance(path, schema, bound_roles):
        rows = ["field\tartifact\tvalue", f"schema\t\t{schema}"]
        for role in bound_roles:
            digest = hashlib.sha256(artifacts[role].read_bytes()).hexdigest()
            rows.append(f"sha256\t{artifacts[role].name}\t{digest}")
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    write_provenance(
        audit_provenance,
        "chain_a_similarity_audit_v1",
        ("audit", "source"),
    )
    write_provenance(
        lower_provenance,
        "chain_a_local_cluster_adjudication_v2",
        ("audit", "confirmed", "lower", "source"),
    )
    args = SimpleNamespace(
        audit=artifacts["audit"],
        audit_provenance=audit_provenance,
        confirmed_reference_manifest=artifacts["confirmed"],
        lower_tier_adjudication=artifacts["lower"],
        lower_tier_provenance=lower_provenance,
        reference_source_fasta=artifacts["source"],
    )
    builder.verify_upstream_provenance(args)

    artifacts["source"].write_text("changed source\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="does not bind"):
        builder.verify_upstream_provenance(args)

    write_provenance(
        audit_provenance,
        "chain_a_similarity_audit_v1",
        ("audit", "source"),
    )
    with pytest.raises(SystemExit, match="does not bind"):
        builder.verify_upstream_provenance(args)

    write_provenance(
        lower_provenance,
        "chain_a_local_cluster_adjudication_v2",
        ("audit", "confirmed", "lower", "source"),
    )
    audit_digest = hashlib.sha256(artifacts["audit"].read_bytes()).hexdigest()
    source_digest = hashlib.sha256(artifacts["source"].read_bytes()).hexdigest()
    audit_provenance.write_text(
        "field\tartifact\tvalue\n"
        "schema\t\tchain_a_similarity_audit_v1\n"
        f"sha256\t{artifacts['audit'].name}\t{source_digest}\n"
        f"sha256\t{artifacts['source'].name}\t{audit_digest}\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="does not bind"):
        builder.verify_upstream_provenance(args)


def test_output_preflight_rejects_invalid_target_without_partial_files(tmp_path):
    builder = load_module()
    manifest = tmp_path / "manifest.tsv"
    quarantine = tmp_path / "quarantine.tsv"
    retained = tmp_path / "retained.tsv"
    invalid_provenance = tmp_path / "provenance.tsv"
    manifest.write_text("existing manifest\n", encoding="utf-8")
    invalid_provenance.mkdir()

    with pytest.raises(SystemExit, match="not a regular file"):
        builder.write_output_set(
            [
                (manifest, "replacement manifest\n"),
                (quarantine, "quarantine\n"),
                (retained, "retained\n"),
                (invalid_provenance, "provenance\n"),
            ]
        )

    assert manifest.read_text(encoding="utf-8") == "existing manifest\n"
    assert not quarantine.exists()
    assert not retained.exists()


def test_committed_disposition_manifest_is_complete_and_partitioned():
    builder = load_module()
    manifest = read_tsv(MANIFEST)
    quarantine = read_tsv(QUARANTINE)
    retained = read_tsv(RETAINED)

    assert len(manifest) == 44
    assert len(quarantine) == 29
    assert len(retained) == 15
    ids = [row["reference_id"] for row in manifest]
    assert ids == sorted(ids)
    assert len(ids) == len(set(ids))
    audit = {
        row["reference_id"]: row
        for row in read_tsv(FIXTURE_DIR / "chain_a_similarity_audit.tsv")
    }
    assert set(ids) == set(audit)
    for row in manifest:
        assert row["reference_sequence_sha256"] == audit[row["reference_id"]][
            "reference_sequence_sha256"
        ]
        assert row["stored_taxid"] == audit[row["reference_id"]]["stored_taxid"]
    quarantine_ids = {row["reference_id"] for row in quarantine}
    retained_ids = {row["reference_id"] for row in retained}
    assert not quarantine_ids.intersection(retained_ids)
    assert quarantine_ids.union(retained_ids) == set(ids)
    assert QUARANTINE.read_text(encoding="utf-8") == builder.tsv_text(
        builder.OUTPUT_FIELDS,
        [row for row in manifest if row["release_action"] == "quarantine"],
    )
    assert RETAINED.read_text(encoding="utf-8") == builder.tsv_text(
        builder.OUTPUT_FIELDS,
        [row for row in manifest if row["release_action"] == "retain"],
    )

    evidence = Counter(row["evidence_class"] for row in manifest)
    assert evidence == Counter(
        {
            "confirmed_reference_sequence_contamination": 5,
            "cross_family_sequence_label_conflict_candidate": 24,
            "unresolved_insufficient_local_discriminator": 15,
        }
    )
    for row in manifest:
        if row["evidence_class"] == "confirmed_reference_sequence_contamination":
            assert row["release_action"] == "quarantine"
        elif row["evidence_class"] == "cross_family_sequence_label_conflict_candidate":
            assert row["release_action"] == "quarantine"
            assert "does not assert bacterial origin" in row["notes"]
        else:
            assert row["release_action"] == "retain"
            assert row["decision_basis"] == "preserve_uncertainty"

    depleted_taxids = {
        row["stored_taxid"]
        for row in quarantine
        if row["post_policy_taxid_record_count"] == "0"
    }
    assert depleted_taxids == {"1119366", "1717526", "650448"}
    assert len({row["reference_sequence_sha256"] for row in quarantine}) == 27
    assert all(
        row["post_policy_exact_sequence_record_count"] == "0"
        for row in quarantine
    )
    core = "".join(
        "\t".join(
            (
                row["reference_id"],
                row["reference_sequence_sha256"],
                row["stored_taxid"],
                row["evidence_class"],
                row["release_action"],
            )
        )
        + "\n"
        for row in manifest
    )
    assert hashlib.sha256(core.encode()).hexdigest() == (
        "a4a9041b7062d5d8c57401b54b10d043db8960bd89be56fc767e0dbf5616d0ad"
    )

    policy_id, mappings = builder.read_release_policy(POLICY)
    assert policy_id == "chain_a_downstream_coi_correctness_first_v1"
    assert mappings[("priority", "confirmed")]["release_action"] == "quarantine"
    assert mappings[
        ("review", "cross_family_sequence_label_conflict_candidate")
    ]["release_action"] == "quarantine"
    assert mappings[
        ("review", "unresolved_insufficient_local_discriminator")
    ]["release_action"] == "retain"


def test_committed_disposition_provenance_matches_inputs_and_outputs():
    rows = read_tsv(PROVENANCE)
    values = {(row["field"], row["artifact"]): row["value"] for row in rows}

    assert values[("schema", "")] == "taxonomy_reference_disposition_v1"
    assert values[("scope", "source_snapshot")] == (
        "shipped_full_fasta_observation_only"
    )
    assert values[("scope", "canonical_base_snapshot")] == "undecided"
    assert values[("scope", "artifact_set_commit_marker")] == (
        "provenance_output_written_last"
    )
    assert values[("policy", "policy_id")] == (
        "chain_a_downstream_coi_correctness_first_v1"
    )
    assert values[("count", "all_disposition_records")] == "44"
    assert values[("count", "quarantine_records")] == "29"
    assert values[("count", "retain_records")] == "15"
    assert values[("count", "source_fasta_records")] == "792926"
    assert values[("count", "source_fasta_unique_reference_ids")] == "792925"
    assert values[("count", "source_fasta_duplicate_reference_ids")] == "1"
    assert values[("count", "source_fasta_empty_records")] == "1"
    assert values[("count", "quarantine_taxids_losing_all_source_records")] == "3"
    assert values[("count", "quarantine_unique_sequence_hashes")] == "27"

    artifacts = {
        "builder_script": SCRIPT,
        "candidate_audit": FIXTURE_DIR / "chain_a_similarity_audit.tsv",
        "candidate_audit_provenance": FIXTURE_DIR
        / "chain_a_similarity_audit_provenance.tsv",
        "confirmed_reference_manifest": FIXTURE_DIR
        / "chain_a_reference_candidates.tsv",
        "lower_tier_adjudication": FIXTURE_DIR
        / "chain_a_lower_tier_adjudication.tsv",
        "lower_tier_adjudication_provenance": FIXTURE_DIR
        / "chain_a_lower_tier_adjudication_provenance.tsv",
        "release_policy": POLICY,
        "disposition_manifest": MANIFEST,
        "quarantine_projection": QUARANTINE,
        "retained_projection": RETAINED,
    }
    for role, path in artifacts.items():
        assert values[("sha256", role)] == hashlib.sha256(path.read_bytes()).hexdigest()


def test_disposition_manifest_regenerates_byte_exact(tmp_path):
    reference_fasta = REPO_ROOT / "db" / "COInr98_2024Jun_RioNegro_Brazil.fasta"
    if not reference_fasta.is_file():
        pytest.skip("the pinned shipped COI source FASTA snapshot is required for replay")
    outputs = {
        "--manifest-output": tmp_path / "manifest.tsv",
        "--quarantine-output": tmp_path / "quarantine.tsv",
        "--retained-output": tmp_path / "retained.tsv",
        "--provenance-output": tmp_path / "provenance.tsv",
    }
    command = [
        str(SCRIPT),
        "--audit",
        str(FIXTURE_DIR / "chain_a_similarity_audit.tsv"),
        "--audit-provenance",
        str(FIXTURE_DIR / "chain_a_similarity_audit_provenance.tsv"),
        "--confirmed-reference-manifest",
        str(FIXTURE_DIR / "chain_a_reference_candidates.tsv"),
        "--lower-tier-adjudication",
        str(FIXTURE_DIR / "chain_a_lower_tier_adjudication.tsv"),
        "--lower-tier-provenance",
        str(FIXTURE_DIR / "chain_a_lower_tier_adjudication_provenance.tsv"),
        "--release-policy",
        str(POLICY),
        "--reference-source-fasta",
        str(reference_fasta),
    ]
    for option, path in outputs.items():
        command.extend([option, str(path)])
    subprocess.run(command, check=True, capture_output=True, text=True)

    expected = {
        "--manifest-output": MANIFEST,
        "--quarantine-output": QUARANTINE,
        "--retained-output": RETAINED,
        "--provenance-output": PROVENANCE,
    }
    for option, path in outputs.items():
        assert path.read_bytes() == expected[option].read_bytes()

    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("BOLD_COI", "LR799917", "MG988837", "Wolbachia"):
        assert forbidden not in source
