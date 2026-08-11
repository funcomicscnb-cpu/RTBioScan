import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN_DIR))
import build_taxonomy_canonical_blastdb as builder  # noqa: E402


SCRIPT = BIN_DIR / "build_taxonomy_canonical_blastdb.py"
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"
INDEX_PROVENANCE = (
    FIXTURE_DIR / "rtbioscan_coi_canonical_v1_blastdb_provenance.tsv"
)
INPUT_ARTIFACTS = {
    "construction_provenance": (
        FIXTURE_DIR / "rtbioscan_coi_canonical_v1_fasta_provenance.tsv"
    ),
    "excluded_legacy_oids": (
        FIXTURE_DIR / "rtbioscan_coi_canonical_v1_excluded_oids.tsv"
    ),
    "release_policy": (
        FIXTURE_DIR / "chain_a_downstream_coi_release_policy_v1.tsv"
    ),
    "base_policy": (
        FIXTURE_DIR / "chain_a_downstream_coi_base_repair_policy_v1.tsv"
    ),
    "disposition_manifest": (
        FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1.tsv"
    ),
    "disposition_provenance": (
        FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1_provenance.tsv"
    ),
    "quarantine_projection": (
        FIXTURE_DIR / "chain_a_downstream_coi_quarantine_v1.tsv"
    ),
    "retained_projection": (
        FIXTURE_DIR / "chain_a_downstream_coi_retained_unresolved_v1.tsv"
    ),
}


def canonical_bytes(records):
    return b"".join(
        builder.source_audit.canonical_record_bytes(title, sequence)
        for title, sequence in records
    )


def write_fasta(path, records):
    path.write_bytes(canonical_bytes(records))


def make_index(tmp_path, name, records, parse_seqids=False):
    makeblastdb = shutil.which("makeblastdb")
    if not makeblastdb:
        pytest.skip("makeblastdb is required")
    fasta = tmp_path / f"{name}-input.fasta"
    prefix = tmp_path / name
    write_fasta(fasta, records)
    command = [
        makeblastdb,
        "-in",
        str(fasta),
        "-dbtype",
        "nucl",
        "-out",
        str(prefix),
        "-title",
        name,
    ]
    if parse_seqids:
        command.append("-parse_seqids")
    completed = subprocess.run(command, capture_output=True, text=True)
    return completed, fasta, prefix


def expectations(records, excluded=None):
    content = canonical_bytes(records)
    return builder.Expectations(
        release_id="synthetic_release",
        release_policy_id="synthetic_policy",
        base_policy_id="synthetic_base_policy",
        records=len(records),
        bases=sum(len(sequence) for _, sequence in records),
        canonical_sha256=hashlib.sha256(content).hexdigest(),
        quarantine_records=len(excluded or {}),
        retain_records=0,
        confirmed_records=0,
        conflict_records=0,
        unresolved_records=0,
        excluded=excluded or {},
    )


def blastdbcmd_path():
    executable = shutil.which("blastdbcmd")
    if not executable:
        pytest.skip("blastdbcmd is required")
    return executable


def read_provenance(path):
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    return rows, {
        (row["field"], row["artifact"]): row["value"] for row in rows
    }


def test_canonical_scanner_rejects_wrapped_sequence(tmp_path):
    fasta = tmp_path / "wrapped.fasta"
    fasta.write_text(">ref|kraken:taxid|1 title\nAC\nGT\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="header"):
        builder.scan_canonical_fasta(fasta)


def test_unparsed_index_preserves_full_stream_contract(tmp_path):
    records = [
        ("opaque_a|kraken:taxid|1 alpha", "ACGT" * 20),
        ("opaque_b|kraken:taxid|-2 beta", "TGCA" * 19),
    ]
    completed, fasta, prefix = make_index(tmp_path, "candidate", records)
    assert completed.returncode == 0, completed.stderr

    expected = expectations(records)
    summary = builder.verify_new_index(
        fasta, prefix, blastdbcmd_path(), expected
    )
    assert summary.records == 2
    assert summary.bases == 156
    assert summary.sha256 == expected.canonical_sha256

    metadata = builder.read_index_metadata(prefix, expected)
    components = builder.enumerate_components(prefix, metadata)
    component_set_sha, rows = builder.component_fingerprint(components)
    assert len(component_set_sha) == 64
    assert {path.name for path, _, _ in rows} == {
        path.name for path in components
    }


def test_parse_seqids_build_cannot_qualify(tmp_path):
    records = [("opaque_a|kraken:taxid|1 alpha", "ACGT" * 20)]
    completed, fasta, prefix = make_index(
        tmp_path, "parsed_candidate", records, parse_seqids=True
    )
    if completed.returncode != 0:
        assert "parse" in (completed.stdout + completed.stderr).lower()
        return

    with pytest.raises(SystemExit, match="parsed a sequence identifier"):
        builder.verify_new_index(
            fasta, prefix, blastdbcmd_path(), expectations(records)
        )


def test_full_legacy_to_new_oid_mapping_is_verified(tmp_path):
    legacy_records = [
        ("legacy_0|kraken:taxid|10 zero", "ACGT" * 20),
        ("legacy_1|kraken:taxid|11 excluded", "TGCA" * 19),
        ("legacy_2|kraken:taxid|-12 two", "GATTACA" * 12),
    ]
    retained_records = [legacy_records[0], legacy_records[2]]
    legacy_result, _, legacy_prefix = make_index(tmp_path, "legacy", legacy_records)
    new_result, _, new_prefix = make_index(tmp_path, "rebuilt", retained_records)
    assert legacy_result.returncode == 0, legacy_result.stderr
    assert new_result.returncode == 0, new_result.stderr

    excluded_sequence = legacy_records[1][1]
    excluded = {
        1: {
            "legacy_oid": "1",
            "reference_id": "legacy_1",
            "stored_taxid": "11",
            "reference_sequence_sha256": hashlib.sha256(
                excluded_sequence.encode("ascii")
            ).hexdigest(),
            "sequence_length": str(len(excluded_sequence)),
        }
    }
    builder.verify_legacy_oid_mapping(
        legacy_prefix,
        new_prefix,
        blastdbcmd_path(),
        expectations(retained_records, excluded),
    )


def test_sseqid_smoke_preserves_embedded_taxid_identifier(tmp_path):
    blastn = shutil.which("blastn")
    if not blastn:
        pytest.skip("blastn is required")
    records = [("opaque_a|kraken:taxid|-42 alpha", "ACGT" * 30)]
    completed, _, prefix = make_index(tmp_path, "smoke", records)
    assert completed.returncode == 0, completed.stderr
    first = builder.source_audit.BlastRecord(
        0, records[0][0], len(records[0][1]), records[0][1]
    )
    builder.verify_blast_sseqid(prefix, blastn, first, tmp_path)


def test_builder_has_no_parsed_id_or_taxid_map_build_options():
    source = SCRIPT.read_text(encoding="utf-8")
    assert '"-parse_seqids"' not in source
    assert '"-taxid_map"' not in source
    for forbidden in ("COInr98", "791404", "485444705", "BOLD_COI"):
        assert forbidden not in source


def test_committed_index_provenance_freezes_build_and_semantic_contracts():
    rows, values = read_provenance(INDEX_PROVENANCE)
    assert len(values) == len(rows)
    assert values[("schema", "")] == "taxonomy_reference_canonical_blastdb_v1"
    assert values[("release", "release_id")] == "rtbioscan_coi_canonical_v1"
    assert values[("scope", "stage")] == "index_candidate_only"
    assert values[("scope", "runtime_activation")] == "none"
    assert values[("scope", "state_identity")] == "unproduced"
    assert values[("artifact", "component_hash_semantics")] == (
        "fixed_build_tamper_detection"
    )
    assert values[("verification", "content_hash_semantics")] == (
        "reproducible_canonical_stream"
    )
    assert values[("verification", "fixed_artifact_fingerprint_scope")] == (
        "separately_supplied_release_artifact_only"
    )
    assert values[("verification", "clone_rebuild_expectation")] == (
        "semantic_content_match_component_hash_match_not_expected"
    )
    assert values[("verification", "fixed_artifact_test_environment")] == (
        "RTBIOSCAN_CANONICAL_BLASTDB_RELEASE"
    )
    assert values[("verification", "accession_contract")] == (
        "BL_ORD_ID_colon_oid"
    )
    assert values[("verification", "internal_taxid_contract")] == "zero"
    assert values[("verification", "legacy_oid_mapping")] == (
        "full_stream_verified"
    )
    assert values[("count", "canonical_records")] == "791404"
    assert values[("count", "canonical_bases")] == "485444705"
    assert values[("sha256", "canonical_fasta")] == (
        "d0b3aca535fbadcd0da8dfc7218c08e7b4151c9562ffe3fd37baf6cdcaef1775"
    )
    assert values[("sha256", "builder_script")] == builder.sha256_file(SCRIPT)
    for artifact, path in INPUT_ARTIFACTS.items():
        assert values[("sha256", artifact)] == builder.sha256_file(path)

    command = json.loads(values[("command", "makeblastdb_argv_json")])
    assert Path(command[0]).name == "makeblastdb"
    assert command[1::2] == ["-in", "-dbtype", "-out", "-title"]
    assert command[4] == "nucl"
    assert command[8] == "rtbioscan_coi_canonical_v1"
    assert "-parse_seqids" not in command
    assert "-taxid_map" not in command

    component_sizes = {
        row["artifact"]: int(row["value"])
        for row in rows
        if row["field"] == "component_bytes"
    }
    component_hashes = {
        row["artifact"]: row["value"]
        for row in rows
        if row["field"] == "component_sha256"
    }
    assert len(component_sizes) == 8
    assert component_sizes.keys() == component_hashes.keys()
    digest = hashlib.sha256()
    for name in sorted(component_hashes):
        digest.update(
            f"{name}\t{component_sizes[name]}\t{component_hashes[name]}\n".encode(
                "utf-8"
            )
        )
    assert digest.hexdigest() == values[("sha256", "fixed_artifact_component_set")]


def test_external_fixed_index_matches_committed_fingerprint_when_requested():
    release_value = os.environ.get("RTBIOSCAN_CANONICAL_BLASTDB_RELEASE")
    if not release_value:
        pytest.skip("set RTBIOSCAN_CANONICAL_BLASTDB_RELEASE to verify fixed index")
    artifact_dir = Path(release_value) / "artifact"
    rows, values = read_provenance(INDEX_PROVENANCE)
    sizes = {
        row["artifact"]: int(row["value"])
        for row in rows
        if row["field"] == "component_bytes"
    }
    hashes = {
        row["artifact"]: row["value"]
        for row in rows
        if row["field"] == "component_sha256"
    }
    for name in sorted(hashes):
        component = artifact_dir / name
        assert component.is_file() and not component.is_symlink()
        assert component.stat().st_size == sizes[name]
        assert builder.sha256_file(component) == hashes[name]
    metadata = json.loads(
        (artifact_dir / "rtbioscan_coi_canonical_v1.njs").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["number-of-sequences"] == int(
        values[("count", "canonical_records")]
    )
    assert metadata["number-of-letters"] == int(
        values[("count", "canonical_bases")]
    )
