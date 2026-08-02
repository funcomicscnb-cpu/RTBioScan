import csv
import hashlib
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "validate_taxonomy_reference_base_policy.py"
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"
POLICY = FIXTURE_DIR / "chain_a_downstream_coi_base_repair_policy_v1.tsv"
ANOMALIES = FIXTURE_DIR / "chain_a_downstream_coi_source_integrity_v1.tsv"
INTEGRITY_PROVENANCE = (
    FIXTURE_DIR / "chain_a_downstream_coi_source_integrity_v1_provenance.tsv"
)
DISPOSITIONS = FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1.tsv"
DISPOSITION_PROVENANCE = (
    FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1_provenance.tsv"
)
REFERENCE_MANIFEST = REPO_ROOT / "conf" / "state_compatibility" / "reference_manifest_legacy_v1.tsv"

ANOMALY_FIELDS = [
    "anomaly_id",
    "anomaly_class",
    "source_line",
    "source_byte_offset",
    "physical_record_ordinal",
    "logical_record_ordinal",
    "reference_id",
    "related_logical_record_ordinal",
    "related_reference_id",
    "legacy_index_oid",
    "source_sequence_length",
    "comparison_sequence_length",
    "source_sequence_sha256",
    "comparison_sequence_sha256",
    "interpretation",
]

SAFE_POLICY_ROWS = [
    ("schema", "", "taxonomy_reference_base_repair_policy_v1"),
    ("policy", "policy_id", "opaque_effective_base_v1"),
    ("scope", "database", "opaque_scope"),
    ("scope", "records", "whole_effective_legacy_index"),
    ("scope", "stage", "policy_only"),
    ("scope", "source_mutation", "none"),
    ("scope", "index_mutation", "none"),
    ("scope", "runtime_activation", "none"),
    ("decision", "base_authority", "effective_legacy_blast_oid_stream"),
    ("decision", "base_membership", "all_legacy_index_oids"),
    ("decision", "record_identity", "legacy_index_oid"),
    ("decision", "record_order", "legacy_index_oid_ascending"),
    ("decision", "retained_relative_order", "preserve_legacy_oid_relative_order"),
    ("decision", "oid_serialization", "omitted"),
    ("decision", "future_oid_stability", "not_guaranteed"),
    ("decision", "title_content", "preserve_complete_legacy_index_title"),
    ("decision", "sequence_content", "preserve_uppercase_legacy_index_sequence"),
    ("decision", "implicit_deduplication", "none"),
    (
        "decision",
        "canonical_serialization",
        "greater_than_title_lf_uppercase_sequence_lf",
    ),
    ("decision", "raw_fasta_slice", "forbidden"),
    ("decision", "raw_fasta_role", "integrity_verification_only"),
    (
        "decision",
        "population:analysis_split_unindexed_records",
        "defer_not_admitted",
    ),
    (
        "decision",
        "population:line_start_unindexed_records",
        "defer_not_admitted",
    ),
    ("decision", "anomaly:opaque_selected_mismatch", "select_index_representation"),
    ("decision", "anomaly:opaque_deferred_candidate", "defer_not_admitted"),
    ("decision", "raw_source_repair", "none"),
    ("decision", "expanded_source_admission", "none"),
    (
        "decision",
        "disposition_match_identity",
        "reference_id_stored_taxid_sequence_sha256",
    ),
    (
        "decision",
        "reference_id_normalization",
        "first_whitespace_token_before_first_pipe",
    ),
    ("decision", "stored_taxid_extraction", "signed_kraken_taxid_token"),
    ("decision", "sequence_sha256_content", "uppercase_ascii_sequence"),
    ("decision", "canonical_encoding", "utf8_lf"),
    ("decision", "disposition:quarantine", "exclude_matching_base_record"),
    ("decision", "disposition:retain", "retain_matching_base_record"),
    ("decision", "nonlisted_base_records", "retain_unmodified"),
    ("decision", "post_filter_order", "preserve_relative_legacy_oid_order"),
    ("decision", "disposition_application", "future_canonical_fasta_build"),
    ("decision", "canonical_fasta_build", "deferred"),
    ("decision", "blast_index_build", "deferred"),
    ("decision", "projected_canonical_bases", "not_computed"),
    ("decision", "projected_canonical_stream_sha256", "not_computed"),
    (
        "decision",
        "state_identity",
        "new_reference_identity_required_before_activation",
    ),
    ("decision", "legacy_state_reuse", "forbidden"),
    ("decision", "corrected_benchmark", "deferred"),
]


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_key_values(path, rows):
    path.write_text(
        "field\tartifact\tvalue\n"
        + "".join(f"{field}\t{artifact}\t{value}\n" for field, artifact, value in rows),
        encoding="utf-8",
    )


def replace_key(path, key, value):
    rows = read_rows(path)
    found = 0
    output = []
    for row in rows:
        if (row["field"], row["artifact"]) == key:
            row["value"] = value
            found += 1
        output.append((row["field"], row["artifact"], row["value"]))
    assert found == 1
    write_key_values(path, output)


def remove_key(path, key):
    rows = read_rows(path)
    output = [
        (row["field"], row["artifact"], row["value"])
        for row in rows
        if (row["field"], row["artifact"]) != key
    ]
    assert len(output) == len(rows) - 1
    write_key_values(path, output)


def anomaly_row(anomaly_id, anomaly_class, logical_ordinal, legacy_oid=""):
    row = {field: "" for field in ANOMALY_FIELDS}
    row.update(
        {
            "anomaly_id": anomaly_id,
            "anomaly_class": anomaly_class,
            "source_line": logical_ordinal,
            "source_byte_offset": logical_ordinal,
            "physical_record_ordinal": "2",
            "logical_record_ordinal": logical_ordinal,
            "reference_id": f"opaque_{anomaly_id}",
            "legacy_index_oid": legacy_oid,
            "source_sequence_length": "2",
            "source_sequence_sha256": hashlib.sha256(b"NN").hexdigest(),
            "interpretation": "opaque observation",
        }
    )
    return row


def write_anomalies(path):
    rows = [
        anomaly_row("selected", "opaque_selected_mismatch", "2", "1"),
        anomaly_row("deferred", "opaque_deferred_candidate", "3"),
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ANOMALY_FIELDS, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def make_inputs(tmp_path):
    reference_manifest = tmp_path / "reference_manifest.tsv"
    reference_manifest.write_text(
        "artifact\tsha256\trole\n"
        f"opaque.ndb\t{'9' * 64}\topaque index\n",
        encoding="utf-8",
    )
    dispositions = tmp_path / "dispositions.tsv"
    sequence_a = hashlib.sha256(b"AC").hexdigest()
    sequence_b = hashlib.sha256(b"GT").hexdigest()
    dispositions.write_text(
        "reference_id\treference_sequence_sha256\tstored_taxid\trelease_action\tpolicy_id\n"
        f"opaque_a\t{sequence_a}\t1\tquarantine\topaque_disposition_v1\n"
        f"opaque_b\t{sequence_b}\t-2\tretain\topaque_disposition_v1\n",
        encoding="utf-8",
    )
    source_fasta_sha = "a" * 64
    disposition_provenance = tmp_path / "disposition_provenance.tsv"
    write_key_values(
        disposition_provenance,
        [
            ("schema", "", "taxonomy_reference_disposition_v1"),
            ("scope", "database", "opaque_scope"),
            ("scope", "canonical_base_snapshot", "undecided"),
            ("policy", "policy_id", "opaque_disposition_v1"),
            ("count", "all_disposition_records", "2"),
            ("count", "quarantine_records", "1"),
            ("count", "retain_records", "1"),
            ("sha256", "disposition_manifest", digest(dispositions)),
            ("sha256", "reference_source_fasta", source_fasta_sha),
        ],
    )
    anomalies = tmp_path / "anomalies.tsv"
    write_anomalies(anomalies)
    integrity_provenance = tmp_path / "integrity_provenance.tsv"
    selected_stream = "b" * 64
    deferred_line_stream = "c" * 64
    deferred_logical_stream = "d" * 64
    write_key_values(
        integrity_provenance,
        [
            ("schema", "", "taxonomy_source_index_integrity_v1"),
            ("scope", "database", "opaque_scope"),
            ("scope", "source_mutation", "none"),
            ("scope", "release_policy", "none"),
            (
                "normalization",
                "canonical_record",
                "greater_than_title_lf_uppercase_sequence_lf",
            ),
            ("count", "source_line_start_records", "2"),
            ("count", "analysis_split_logical_records", "3"),
            ("count", "analysis_split_total_bases", "6"),
            ("count", "embedded_header_candidates", "1"),
            ("count", "legacy_index_records", "2"),
            ("count", "legacy_index_bases", "4"),
            ("count", "analysis_split_selected_bases", "4"),
            ("count", "line_start_unindexed_records", "0"),
            ("count", "analysis_split_unindexed_records", "1"),
            ("count", "anomaly_rows", "2"),
            ("count", "anomaly:opaque_selected_mismatch", "1"),
            ("count", "anomaly:opaque_deferred_candidate", "1"),
            ("count", "disposition_records_in_legacy_index", "2"),
            ("count", "disposition:quarantine", "1"),
            ("count", "disposition:retain", "1"),
            ("sha256", "anomaly_table", digest(anomalies)),
            ("sha256", "disposition_manifest", digest(dispositions)),
            ("sha256", "disposition_provenance", digest(disposition_provenance)),
            ("sha256", "legacy_reference_manifest", digest(reference_manifest)),
            ("sha256", "source_fasta", source_fasta_sha),
            ("sha256", "legacy_index_canonical_stream", selected_stream),
            ("sha256", "analysis_split_selected_canonical_stream", selected_stream),
            ("sha256", "line_start_unindexed_canonical_stream", deferred_line_stream),
            ("sha256", "analysis_split_unindexed_canonical_stream", deferred_logical_stream),
        ],
    )
    policy = tmp_path / "policy.tsv"
    policy_rows = list(SAFE_POLICY_ROWS)
    policy_rows.extend(
        [
            ("count", "base_records", "2"),
            ("count", "base_bases", "4"),
            ("count", "legacy_oid_first", "0"),
            ("count", "legacy_oid_last", "1"),
            ("count", "line_start_unindexed_records", "0"),
            ("count", "analysis_split_unindexed_records", "1"),
            ("count", "analysis_split_unindexed_bases", "2"),
            ("count", "embedded_header_candidates", "1"),
            ("count", "anomaly_rows", "2"),
            ("count", "disposition_records_in_base", "2"),
            ("count", "disposition:quarantine", "1"),
            ("count", "disposition:retain", "1"),
            ("count", "nonlisted_base_records", "0"),
            ("count", "projected_canonical_records", "1"),
            ("sha256", "validator_script", digest(SCRIPT)),
            ("sha256", "source_integrity_anomalies", digest(anomalies)),
            ("sha256", "source_integrity_provenance", digest(integrity_provenance)),
            ("sha256", "disposition_manifest", digest(dispositions)),
            ("sha256", "disposition_provenance", digest(disposition_provenance)),
            ("sha256", "legacy_reference_manifest", digest(reference_manifest)),
            ("sha256", "source_fasta", source_fasta_sha),
            ("sha256", "selected_base_canonical_stream", selected_stream),
            ("sha256", "deferred_line_start_canonical_stream", deferred_line_stream),
            ("sha256", "deferred_analysis_split_canonical_stream", deferred_logical_stream),
        ]
    )
    write_key_values(policy, policy_rows)
    return {
        "policy": policy,
        "anomalies": anomalies,
        "integrity_provenance": integrity_provenance,
        "dispositions": dispositions,
        "disposition_provenance": disposition_provenance,
        "reference_manifest": reference_manifest,
    }


def command(inputs):
    return [
        str(SCRIPT),
        "--policy",
        str(inputs["policy"]),
        "--source-integrity-anomalies",
        str(inputs["anomalies"]),
        "--source-integrity-provenance",
        str(inputs["integrity_provenance"]),
        "--disposition-manifest",
        str(inputs["dispositions"]),
        "--disposition-provenance",
        str(inputs["disposition_provenance"]),
        "--reference-manifest",
        str(inputs["reference_manifest"]),
    ]


def run_validator(inputs):
    return subprocess.run(command(inputs), capture_output=True, text=True, check=False)


def refresh_direct_policy_hashes(inputs):
    for artifact, key in (
        ("anomalies", "source_integrity_anomalies"),
        ("integrity_provenance", "source_integrity_provenance"),
        ("dispositions", "disposition_manifest"),
        ("disposition_provenance", "disposition_provenance"),
        ("reference_manifest", "legacy_reference_manifest"),
    ):
        replace_key(inputs["policy"], ("sha256", key), digest(inputs[artifact]))


def rebind_anomaly_chain(inputs):
    replace_key(
        inputs["integrity_provenance"],
        ("sha256", "anomaly_table"),
        digest(inputs["anomalies"]),
    )
    refresh_direct_policy_hashes(inputs)


def rebind_disposition_chain(inputs):
    replace_key(
        inputs["disposition_provenance"],
        ("sha256", "disposition_manifest"),
        digest(inputs["dispositions"]),
    )
    replace_key(
        inputs["integrity_provenance"],
        ("sha256", "disposition_manifest"),
        digest(inputs["dispositions"]),
    )
    replace_key(
        inputs["integrity_provenance"],
        ("sha256", "disposition_provenance"),
        digest(inputs["disposition_provenance"]),
    )
    refresh_direct_policy_hashes(inputs)


def committed_command():
    return [
        str(SCRIPT),
        "--policy",
        str(POLICY),
        "--source-integrity-anomalies",
        str(ANOMALIES),
        "--source-integrity-provenance",
        str(INTEGRITY_PROVENANCE),
        "--disposition-manifest",
        str(DISPOSITIONS),
        "--disposition-provenance",
        str(DISPOSITION_PROVENANCE),
        "--reference-manifest",
        str(REFERENCE_MANIFEST),
    ]


def test_valid_opaque_policy_is_read_only(tmp_path):
    inputs = make_inputs(tmp_path)
    before = {path: path.read_bytes() for path in inputs.values()}
    completed = run_validator(inputs)
    assert completed.returncode == 0, completed.stderr
    assert "2 selected records, 1 deferred records, 1 future exclusions" in completed.stdout
    assert {path: path.read_bytes() for path in inputs.values()} == before


@pytest.mark.parametrize(
    ("key", "unsafe_value"),
    [
        (("scope", "source_mutation"), "rewrite_source"),
        (("scope", "index_mutation"), "rebuild_index"),
        (("scope", "runtime_activation"), "activate"),
        (("decision", "base_authority"), "raw_fasta_prefix"),
        (("decision", "record_identity"), "reference_id"),
        (("decision", "record_order"), "reference_id_sorted"),
        (("decision", "implicit_deduplication"), "sequence_hash"),
        (("decision", "raw_fasta_slice"), "allowed"),
        (("decision", "population:analysis_split_unindexed_records"), "admit"),
        (("decision", "raw_source_repair"), "insert_newline"),
        (("decision", "expanded_source_admission"), "all_records"),
        (("decision", "disposition_match_identity"), "reference_id"),
        (("decision", "reference_id_normalization"), "complete_title"),
        (("decision", "stored_taxid_extraction"), "unsigned_integer"),
        (("decision", "sequence_sha256_content"), "raw_sequence_bytes"),
        (("decision", "canonical_encoding"), "platform_default"),
        (("decision", "canonical_fasta_build"), "complete"),
        (("decision", "state_identity"), "reuse_legacy"),
        (("decision", "legacy_state_reuse"), "allowed"),
    ],
)
def test_unsafe_policy_decisions_fail_closed(tmp_path, key, unsafe_value):
    inputs = make_inputs(tmp_path)
    replace_key(inputs["policy"], key, unsafe_value)
    completed = run_validator(inputs)
    assert completed.returncode != 0
    assert "unexpected base/repair policy value" in completed.stderr


@pytest.mark.parametrize(
    "artifact",
    [
        "anomalies",
        "integrity_provenance",
        "dispositions",
        "disposition_provenance",
        "reference_manifest",
    ],
)
def test_stale_dependency_hashes_are_rejected(tmp_path, artifact):
    inputs = make_inputs(tmp_path)
    inputs[artifact].write_bytes(inputs[artifact].read_bytes() + b"\n")
    completed = run_validator(inputs)
    assert completed.returncode != 0


def test_policy_key_set_is_exact_and_unique(tmp_path):
    inputs = make_inputs(tmp_path)
    remove_key(inputs["policy"], ("decision", "raw_fasta_slice"))
    missing = run_validator(inputs)
    assert missing.returncode != 0
    assert "missing base/repair policy key" in missing.stderr

    inputs = make_inputs(tmp_path)
    rows = read_rows(inputs["policy"])
    rows.append({"field": "decision", "artifact": "opaque_extra", "value": "none"})
    write_key_values(
        inputs["policy"],
        [(row["field"], row["artifact"], row["value"]) for row in rows],
    )
    extra = run_validator(inputs)
    assert extra.returncode != 0
    assert "policy key set mismatch" in extra.stderr

    inputs = make_inputs(tmp_path)
    rows = read_rows(inputs["policy"])
    rows.append(dict(rows[0]))
    write_key_values(
        inputs["policy"],
        [(row["field"], row["artifact"], row["value"]) for row in rows],
    )
    duplicate = run_validator(inputs)
    assert duplicate.returncode != 0
    assert "duplicate base/repair policy key" in duplicate.stderr

    inputs = make_inputs(tmp_path)
    with inputs["policy"].open("a", encoding="utf-8") as handle:
        handle.write("decision\tincomplete\n")
    malformed = run_validator(inputs)
    assert malformed.returncode != 0
    assert "malformed TSV row" in malformed.stderr


def test_selected_and_index_streams_must_match(tmp_path):
    inputs = make_inputs(tmp_path)
    replace_key(
        inputs["integrity_provenance"],
        ("sha256", "analysis_split_selected_canonical_stream"),
        "e" * 64,
    )
    refresh_direct_policy_hashes(inputs)
    completed = run_validator(inputs)
    assert completed.returncode != 0
    assert "does not exactly match" in completed.stderr


def test_tail_arithmetic_and_anomaly_actions_are_derived(tmp_path):
    inputs = make_inputs(tmp_path)
    replace_key(
        inputs["integrity_provenance"],
        ("count", "analysis_split_unindexed_records"),
        "2",
    )
    refresh_direct_policy_hashes(inputs)
    broken_tail = run_validator(inputs)
    assert broken_tail.returncode != 0
    assert "record arithmetic" in broken_tail.stderr

    inputs = make_inputs(tmp_path)
    replace_key(
        inputs["policy"],
        ("decision", "anomaly:opaque_deferred_candidate"),
        "select_index_representation",
    )
    wrong_action = run_validator(inputs)
    assert wrong_action.returncode != 0
    assert "unexpected base/repair policy value" in wrong_action.stderr


@pytest.mark.parametrize(
    ("legacy_oid", "expected_error"),
    [
        ("", "selected anomaly lacks a legacy index OID"),
        ("0", "selected anomaly OID/ordinal mismatch"),
    ],
)
def test_selected_anomaly_requires_matching_index_coordinate(
    tmp_path, legacy_oid, expected_error
):
    inputs = make_inputs(tmp_path)
    rows = read_rows(inputs["anomalies"])
    rows[0]["legacy_index_oid"] = legacy_oid
    with inputs["anomalies"].open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=ANOMALY_FIELDS,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
    rebind_anomaly_chain(inputs)
    completed = run_validator(inputs)
    assert completed.returncode != 0
    assert expected_error in completed.stderr


def test_disposition_identity_and_counts_are_recomputed(tmp_path):
    inputs = make_inputs(tmp_path)
    text = inputs["dispositions"].read_text(encoding="utf-8")
    inputs["dispositions"].write_text(text.replace("opaque_b", "opaque_a"), encoding="utf-8")
    rebind_disposition_chain(inputs)
    completed = run_validator(inputs)
    assert completed.returncode != 0
    assert "duplicate disposition reference ID" in completed.stderr


def test_committed_policy_is_exact_and_validated_without_database():
    completed = subprocess.run(committed_command(), capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    values = {
        (row["field"], row["artifact"]): row["value"] for row in read_rows(POLICY)
    }
    assert values[("policy", "policy_id")] == "downstream_coi_legacy_effective_base_v1"
    assert values[("decision", "base_authority")] == "effective_legacy_blast_oid_stream"
    assert values[("decision", "record_identity")] == "legacy_index_oid"
    assert values[("decision", "implicit_deduplication")] == "none"
    assert values[("decision", "disposition_match_identity")] == (
        "reference_id_stored_taxid_sequence_sha256"
    )
    assert values[("decision", "reference_id_normalization")] == (
        "first_whitespace_token_before_first_pipe"
    )
    assert values[("decision", "stored_taxid_extraction")] == (
        "signed_kraken_taxid_token"
    )
    assert values[("decision", "sequence_sha256_content")] == (
        "uppercase_ascii_sequence"
    )
    assert values[("decision", "anomaly:embedded_header_candidate")] == (
        "defer_not_admitted"
    )
    assert values[("decision", "anomaly:line_oriented_index_sequence_mismatch")] == (
        "select_index_representation"
    )
    assert values[("count", "base_records")] == "791433"
    assert values[("count", "analysis_split_unindexed_records")] == "1494"
    assert values[("count", "projected_canonical_records")] == "791404"
    assert values[("decision", "projected_canonical_stream_sha256")] == "not_computed"
    assert values[("decision", "canonical_fasta_build")] == "deferred"
    assert values[("decision", "blast_index_build")] == "deferred"


def test_committed_policy_and_validator_hashes_are_frozen():
    assert digest(SCRIPT) == "98789a4d97654924e01c18ffe8eedfb7eaf1a238ebea08b377436a6226ef5076"
    assert digest(POLICY) == "3aace1e9da7805785db536321f6dc4b1d0d6e075fd432ebaabc40d389f5aa5a0"
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "BOLD_COI",
        "XPR26",
        "WPB428",
        "COInr98",
        "791433",
        "1494",
        "write_text",
        "write_bytes",
        "os.replace",
        "mkstemp",
        "--output",
    ):
        assert forbidden not in source
