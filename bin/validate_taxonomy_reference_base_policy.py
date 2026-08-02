#!/usr/bin/env python3
"""Validate a policy-only taxonomy reference base selection.

The validator consumes committed audit and disposition artifacts. It never
reads or writes a FASTA or database and produces no artifact of its own.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import cast


POLICY_SCHEMA = "taxonomy_reference_base_repair_policy_v1"
INTEGRITY_SCHEMA = "taxonomy_source_index_integrity_v1"
DISPOSITION_SCHEMA = "taxonomy_reference_disposition_v1"
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SIGNED_INTEGER_RE = re.compile(r"-?[0-9]+")

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

FIXED_POLICY_VALUES = {
    ("schema", ""): POLICY_SCHEMA,
    ("scope", "records"): "whole_effective_legacy_index",
    ("scope", "stage"): "policy_only",
    ("scope", "source_mutation"): "none",
    ("scope", "index_mutation"): "none",
    ("scope", "runtime_activation"): "none",
    ("decision", "base_authority"): "effective_legacy_blast_oid_stream",
    ("decision", "base_membership"): "all_legacy_index_oids",
    ("decision", "record_identity"): "legacy_index_oid",
    ("decision", "record_order"): "legacy_index_oid_ascending",
    ("decision", "retained_relative_order"): "preserve_legacy_oid_relative_order",
    ("decision", "oid_serialization"): "omitted",
    ("decision", "future_oid_stability"): "not_guaranteed",
    ("decision", "title_content"): "preserve_complete_legacy_index_title",
    ("decision", "sequence_content"): "preserve_uppercase_legacy_index_sequence",
    ("decision", "implicit_deduplication"): "none",
    (
        "decision",
        "canonical_serialization",
    ): "greater_than_title_lf_uppercase_sequence_lf",
    ("decision", "raw_fasta_slice"): "forbidden",
    ("decision", "raw_fasta_role"): "integrity_verification_only",
    (
        "decision",
        "population:analysis_split_unindexed_records",
    ): "defer_not_admitted",
    (
        "decision",
        "population:line_start_unindexed_records",
    ): "defer_not_admitted",
    ("decision", "raw_source_repair"): "none",
    ("decision", "expanded_source_admission"): "none",
    (
        "decision",
        "disposition_match_identity",
    ): "reference_id_stored_taxid_sequence_sha256",
    (
        "decision",
        "reference_id_normalization",
    ): "first_whitespace_token_before_first_pipe",
    (
        "decision",
        "stored_taxid_extraction",
    ): "signed_kraken_taxid_token",
    (
        "decision",
        "sequence_sha256_content",
    ): "uppercase_ascii_sequence",
    ("decision", "canonical_encoding"): "utf8_lf",
    (
        "decision",
        "disposition:quarantine",
    ): "exclude_matching_base_record",
    ("decision", "disposition:retain"): "retain_matching_base_record",
    (
        "decision",
        "nonlisted_base_records",
    ): "retain_unmodified",
    (
        "decision",
        "post_filter_order",
    ): "preserve_relative_legacy_oid_order",
    (
        "decision",
        "disposition_application",
    ): "future_canonical_fasta_build",
    ("decision", "canonical_fasta_build"): "deferred",
    ("decision", "blast_index_build"): "deferred",
    ("decision", "projected_canonical_bases"): "not_computed",
    ("decision", "projected_canonical_stream_sha256"): "not_computed",
    (
        "decision",
        "state_identity",
    ): "new_reference_identity_required_before_activation",
    ("decision", "legacy_state_reuse"): "forbidden",
    ("decision", "corrected_benchmark"): "deferred",
}

COUNT_ARTIFACTS = {
    "base_records",
    "base_bases",
    "legacy_oid_first",
    "legacy_oid_last",
    "line_start_unindexed_records",
    "analysis_split_unindexed_records",
    "analysis_split_unindexed_bases",
    "embedded_header_candidates",
    "anomaly_rows",
    "disposition_records_in_base",
    "disposition:quarantine",
    "disposition:retain",
    "nonlisted_base_records",
    "projected_canonical_records",
}

SHA256_ARTIFACTS = {
    "validator_script",
    "source_integrity_anomalies",
    "source_integrity_provenance",
    "disposition_manifest",
    "disposition_provenance",
    "legacy_reference_manifest",
    "source_fasta",
    "selected_base_canonical_stream",
    "deferred_line_start_canonical_stream",
    "deferred_analysis_split_canonical_stream",
}


def die(message: str) -> "None":
    raise SystemExit(f"ERROR: {message}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_tsv(path: Path, expected_fields: list[str] | None = None) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        die(f"cannot read TSV {path}: {error}")
    if not fields or len(fields) != len(set(fields)):
        die(f"invalid or duplicate TSV columns: {path}")
    if expected_fields is not None and fields != expected_fields:
        die(f"unexpected TSV schema: {path}")
    if any(
        None in row or any(value is None for value in row.values())
        for row in rows
    ):
        die(f"malformed TSV row: {path}")
    return list(fields), cast(list[dict[str, str]], rows)


def read_key_values(path: Path, label: str) -> dict[tuple[str, str], str]:
    _, rows = read_tsv(path, ["field", "artifact", "value"])
    if not rows:
        die(f"empty {label}: {path}")
    result: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row["field"], row["artifact"])
        if not key[0] or not row["value"]:
            die(f"blank key or value in {label}: {path}")
        if key in result:
            die(f"duplicate {label} key: {key}")
        result[key] = row["value"]
    return result


def required_value(values: dict[tuple[str, str], str], key: tuple[str, str], label: str) -> str:
    if key not in values:
        die(f"missing {label} key: {key}")
    return values[key]


def expect_value(
    values: dict[tuple[str, str], str],
    key: tuple[str, str],
    expected: str,
    label: str,
) -> None:
    observed = required_value(values, key, label)
    if observed != expected:
        die(f"unexpected {label} value for {key}: {observed} != {expected}")


def nonnegative_integer(text: str, label: str) -> int:
    if not text.isdigit():
        die(f"invalid non-negative integer for {label}: {text}")
    return int(text)


def provenance_count(values: dict[tuple[str, str], str], artifact: str) -> int:
    return nonnegative_integer(
        required_value(values, ("count", artifact), "source-integrity provenance"),
        artifact,
    )


def validate_reference_manifest(path: Path) -> None:
    _, rows = read_tsv(path, ["artifact", "sha256", "role"])
    if not rows:
        die("legacy reference manifest is empty")
    artifacts: set[str] = set()
    for row in rows:
        artifact = row["artifact"]
        if not artifact or artifact in artifacts:
            die(f"invalid or duplicate legacy reference artifact: {artifact}")
        artifacts.add(artifact)
        if not SHA256_RE.fullmatch(row["sha256"]):
            die(f"invalid legacy reference checksum: {artifact}")
        if not row["role"]:
            die(f"blank legacy reference role: {artifact}")


def validate_integrity(
    anomalies_path: Path,
    provenance_path: Path,
    disposition_path: Path,
    disposition_provenance_path: Path,
    reference_manifest_path: Path,
) -> tuple[
    dict[tuple[str, str], str],
    dict[str, str],
    dict[str, int],
    dict[str, str],
    dict[str, str],
]:
    _, anomalies = read_tsv(anomalies_path, ANOMALY_FIELDS)
    provenance = read_key_values(provenance_path, "source-integrity provenance")
    expect_value(provenance, ("schema", ""), INTEGRITY_SCHEMA, "source-integrity provenance")
    expect_value(provenance, ("scope", "source_mutation"), "none", "source-integrity provenance")
    expect_value(provenance, ("scope", "release_policy"), "none", "source-integrity provenance")
    expect_value(
        provenance,
        ("normalization", "canonical_record"),
        "greater_than_title_lf_uppercase_sequence_lf",
        "source-integrity provenance",
    )

    direct_hashes = {
        "source_integrity_anomalies": sha256_file(anomalies_path),
        "source_integrity_provenance": sha256_file(provenance_path),
        "disposition_manifest": sha256_file(disposition_path),
        "disposition_provenance": sha256_file(disposition_provenance_path),
        "legacy_reference_manifest": sha256_file(reference_manifest_path),
    }
    bindings = {
        "source_integrity_anomalies": required_value(
            provenance, ("sha256", "anomaly_table"), "source-integrity provenance"
        ),
        "disposition_manifest": required_value(
            provenance, ("sha256", "disposition_manifest"), "source-integrity provenance"
        ),
        "disposition_provenance": required_value(
            provenance,
            ("sha256", "disposition_provenance"),
            "source-integrity provenance",
        ),
        "legacy_reference_manifest": required_value(
            provenance,
            ("sha256", "legacy_reference_manifest"),
            "source-integrity provenance",
        ),
    }
    for artifact, expected in bindings.items():
        if direct_hashes[artifact] != expected:
            die(f"source-integrity provenance has a stale {artifact} binding")

    index_stream = required_value(
        provenance,
        ("sha256", "legacy_index_canonical_stream"),
        "source-integrity provenance",
    )
    selected_stream = required_value(
        provenance,
        ("sha256", "analysis_split_selected_canonical_stream"),
        "source-integrity provenance",
    )
    if not SHA256_RE.fullmatch(index_stream) or selected_stream != index_stream:
        die("selected source stream does not exactly match the legacy index stream")

    counts = {
        name: provenance_count(provenance, name)
        for name in (
            "source_line_start_records",
            "analysis_split_logical_records",
            "analysis_split_total_bases",
            "embedded_header_candidates",
            "legacy_index_records",
            "legacy_index_bases",
            "analysis_split_selected_bases",
            "line_start_unindexed_records",
            "analysis_split_unindexed_records",
            "anomaly_rows",
            "disposition_records_in_legacy_index",
            "disposition:quarantine",
            "disposition:retain",
        )
    }
    if counts["legacy_index_records"] <= 0:
        die("legacy index must contain at least one record")
    if counts["analysis_split_selected_bases"] != counts["legacy_index_bases"]:
        die("selected source base count does not match the legacy index")
    if counts["analysis_split_logical_records"] != (
        counts["legacy_index_records"] + counts["analysis_split_unindexed_records"]
    ):
        die("analysis-split record arithmetic is inconsistent")
    if counts["source_line_start_records"] != (
        counts["legacy_index_records"] + counts["line_start_unindexed_records"]
    ):
        die("line-start record arithmetic is inconsistent")
    if counts["analysis_split_unindexed_records"] != (
        counts["line_start_unindexed_records"] + counts["embedded_header_candidates"]
    ):
        die("embedded-header/tail record arithmetic is inconsistent")
    if counts["analysis_split_total_bases"] < counts["legacy_index_bases"]:
        die("analysis-split source is shorter than the selected base")
    if counts["anomaly_rows"] != len(anomalies):
        die("source-integrity anomaly row count is inconsistent")

    class_counts: Counter[str] = Counter()
    class_categories: dict[str, set[str]] = defaultdict(set)
    seen_anomaly_ids: set[str] = set()
    for row in anomalies:
        anomaly_id = row["anomaly_id"]
        anomaly_class = row["anomaly_class"]
        if not anomaly_id or anomaly_id in seen_anomaly_ids or not anomaly_class:
            die("source-integrity anomaly IDs/classes must be non-empty and unique")
        seen_anomaly_ids.add(anomaly_id)
        logical_ordinal = nonnegative_integer(
            row["logical_record_ordinal"], f"{anomaly_id} logical record ordinal"
        )
        if logical_ordinal == 0:
            die(f"logical record ordinals are one-based: {anomaly_id}")
        legacy_oid = row["legacy_index_oid"]
        if logical_ordinal <= counts["legacy_index_records"]:
            if not legacy_oid:
                die(f"selected anomaly lacks a legacy index OID: {anomaly_id}")
            oid = nonnegative_integer(legacy_oid, f"{anomaly_id} legacy index OID")
            if oid != logical_ordinal - 1:
                die(f"selected anomaly OID/ordinal mismatch: {anomaly_id}")
            category = "selected"
        else:
            if legacy_oid:
                die(f"deferred anomaly unexpectedly has a legacy index OID: {anomaly_id}")
            category = "deferred"
        class_counts[anomaly_class] += 1
        class_categories[anomaly_class].add(category)

    anomaly_actions: dict[str, str] = {}
    for anomaly_class, categories in class_categories.items():
        if len(categories) != 1:
            die(f"anomaly class spans selected and deferred records: {anomaly_class}")
        expect_value(
            provenance,
            ("count", f"anomaly:{anomaly_class}"),
            str(class_counts[anomaly_class]),
            "source-integrity provenance",
        )
        category = next(iter(categories))
        anomaly_actions[anomaly_class] = (
            "select_index_representation" if category == "selected" else "defer_not_admitted"
        )
    provenance_anomaly_classes = {
        artifact.removeprefix("anomaly:")
        for field, artifact in provenance
        if field == "count" and artifact.startswith("anomaly:")
    }
    if provenance_anomaly_classes != set(class_counts):
        die("source-integrity anomaly class counts do not match the anomaly table")

    upstream = {
        "database_scope": required_value(
            provenance, ("scope", "database"), "source-integrity provenance"
        ),
        "source_fasta": required_value(
            provenance, ("sha256", "source_fasta"), "source-integrity provenance"
        ),
        "selected_base_canonical_stream": index_stream,
        "deferred_line_start_canonical_stream": required_value(
            provenance,
            ("sha256", "line_start_unindexed_canonical_stream"),
            "source-integrity provenance",
        ),
        "deferred_analysis_split_canonical_stream": required_value(
            provenance,
            ("sha256", "analysis_split_unindexed_canonical_stream"),
            "source-integrity provenance",
        ),
    }
    for artifact, digest in upstream.items():
        if artifact != "database_scope" and not SHA256_RE.fullmatch(digest):
            die(f"invalid upstream checksum: {artifact}")
    return provenance, direct_hashes, counts, anomaly_actions, upstream


def validate_dispositions(
    manifest_path: Path,
    provenance_path: Path,
    integrity: dict[tuple[str, str], str],
    database_scope: str,
) -> tuple[dict[str, int], dict[str, str]]:
    fields, rows = read_tsv(manifest_path)
    required_fields = {
        "reference_id",
        "reference_sequence_sha256",
        "stored_taxid",
        "release_action",
        "policy_id",
    }
    if not rows or not required_fields.issubset(fields):
        die("unexpected or empty disposition manifest")
    seen_ids: set[str] = set()
    seen_identities: set[tuple[str, str, str]] = set()
    policy_ids: set[str] = set()
    actions: Counter[str] = Counter()
    for row in rows:
        reference_id = row["reference_id"]
        sequence_sha = row["reference_sequence_sha256"]
        taxid = row["stored_taxid"]
        action = row["release_action"]
        if not reference_id or any(char.isspace() for char in reference_id) or "|" in reference_id:
            die(f"invalid normalized disposition reference ID: {reference_id}")
        if reference_id in seen_ids:
            die(f"duplicate disposition reference ID: {reference_id}")
        seen_ids.add(reference_id)
        if not SHA256_RE.fullmatch(sequence_sha) or not SIGNED_INTEGER_RE.fullmatch(taxid):
            die(f"invalid disposition identity: {reference_id}")
        identity = (reference_id, taxid, sequence_sha)
        if identity in seen_identities:
            die(f"duplicate disposition identity: {reference_id}")
        seen_identities.add(identity)
        if action not in {"quarantine", "retain"}:
            die(f"invalid disposition action: {reference_id}")
        if not row["policy_id"]:
            die(f"blank disposition policy ID: {reference_id}")
        policy_ids.add(row["policy_id"])
        actions[action] += 1
    if len(policy_ids) != 1:
        die("disposition manifest has multiple policy IDs")

    provenance = read_key_values(provenance_path, "disposition provenance")
    expect_value(provenance, ("schema", ""), DISPOSITION_SCHEMA, "disposition provenance")
    expect_value(provenance, ("scope", "database"), database_scope, "disposition provenance")
    expect_value(
        provenance,
        ("scope", "canonical_base_snapshot"),
        "undecided",
        "disposition provenance",
    )
    expect_value(
        provenance,
        ("sha256", "disposition_manifest"),
        sha256_file(manifest_path),
        "disposition provenance",
    )
    expect_value(
        provenance,
        ("sha256", "reference_source_fasta"),
        required_value(integrity, ("sha256", "source_fasta"), "source-integrity provenance"),
        "disposition provenance",
    )
    expect_value(
        provenance,
        ("policy", "policy_id"),
        next(iter(policy_ids)),
        "disposition provenance",
    )
    derived = {
        "records": len(rows),
        "quarantine": actions["quarantine"],
        "retain": actions["retain"],
    }
    for artifact, value in (
        ("all_disposition_records", derived["records"]),
        ("quarantine_records", derived["quarantine"]),
        ("retain_records", derived["retain"]),
    ):
        expect_value(
            provenance,
            ("count", artifact),
            str(value),
            "disposition provenance",
        )
    return derived, {
        "disposition_manifest": sha256_file(manifest_path),
        "disposition_provenance": sha256_file(provenance_path),
    }


def validate_policy(
    policy_path: Path,
    anomalies_path: Path,
    integrity_provenance_path: Path,
    disposition_path: Path,
    disposition_provenance_path: Path,
    reference_manifest_path: Path,
) -> tuple[str, int, int, int]:
    for path in (
        policy_path,
        anomalies_path,
        integrity_provenance_path,
        disposition_path,
        disposition_provenance_path,
        reference_manifest_path,
    ):
        if not path.is_file():
            die(f"required input is missing: {path}")
    validate_reference_manifest(reference_manifest_path)
    integrity, direct_hashes, counts, anomaly_actions, upstream = validate_integrity(
        anomalies_path,
        integrity_provenance_path,
        disposition_path,
        disposition_provenance_path,
        reference_manifest_path,
    )
    dispositions, disposition_hashes = validate_dispositions(
        disposition_path,
        disposition_provenance_path,
        integrity,
        upstream["database_scope"],
    )
    if counts["disposition_records_in_legacy_index"] != dispositions["records"]:
        die("not every disposition record is present once in the selected base")
    for action in ("quarantine", "retain"):
        if counts[f"disposition:{action}"] != dispositions[action]:
            die(f"source-integrity disposition count mismatch: {action}")

    policy = read_key_values(policy_path, "base/repair policy")
    policy_id = required_value(policy, ("policy", "policy_id"), "base/repair policy")
    if not TOKEN_RE.fullmatch(policy_id):
        die(f"invalid base/repair policy ID: {policy_id}")
    database_scope = required_value(policy, ("scope", "database"), "base/repair policy")
    if not TOKEN_RE.fullmatch(database_scope) or database_scope != upstream["database_scope"]:
        die("base/repair policy database scope does not match the audit")

    for key, expected in FIXED_POLICY_VALUES.items():
        expect_value(policy, key, expected, "base/repair policy")
    for anomaly_class, action in anomaly_actions.items():
        expect_value(
            policy,
            ("decision", f"anomaly:{anomaly_class}"),
            action,
            "base/repair policy",
        )

    base_records = counts["legacy_index_records"]
    base_bases = counts["legacy_index_bases"]
    tail_records = counts["analysis_split_unindexed_records"]
    tail_bases = counts["analysis_split_total_bases"] - base_bases
    nonlisted = base_records - dispositions["records"]
    projected = base_records - dispositions["quarantine"]
    if nonlisted < 0 or projected < 0:
        die("disposition counts exceed the selected base")
    expected_counts = {
        "base_records": base_records,
        "base_bases": base_bases,
        "legacy_oid_first": 0,
        "legacy_oid_last": base_records - 1,
        "line_start_unindexed_records": counts["line_start_unindexed_records"],
        "analysis_split_unindexed_records": tail_records,
        "analysis_split_unindexed_bases": tail_bases,
        "embedded_header_candidates": counts["embedded_header_candidates"],
        "anomaly_rows": counts["anomaly_rows"],
        "disposition_records_in_base": dispositions["records"],
        "disposition:quarantine": dispositions["quarantine"],
        "disposition:retain": dispositions["retain"],
        "nonlisted_base_records": nonlisted,
        "projected_canonical_records": projected,
    }
    for artifact, expected_count in expected_counts.items():
        expect_value(
            policy,
            ("count", artifact),
            str(expected_count),
            "base/repair policy",
        )

    expected_hashes = {
        "validator_script": sha256_file(Path(__file__)),
        **direct_hashes,
        **disposition_hashes,
        "source_fasta": upstream["source_fasta"],
        "selected_base_canonical_stream": upstream["selected_base_canonical_stream"],
        "deferred_line_start_canonical_stream": upstream[
            "deferred_line_start_canonical_stream"
        ],
        "deferred_analysis_split_canonical_stream": upstream[
            "deferred_analysis_split_canonical_stream"
        ],
    }
    for artifact, digest in expected_hashes.items():
        if not SHA256_RE.fullmatch(digest):
            die(f"invalid expected checksum: {artifact}")
        expect_value(policy, ("sha256", artifact), digest, "base/repair policy")

    anomaly_keys = {
        ("decision", f"anomaly:{name}")
        for name in anomaly_actions
    }
    expected_keys = (
        set(FIXED_POLICY_VALUES)
        | {("policy", "policy_id"), ("scope", "database")}
        | anomaly_keys
        | {("count", artifact) for artifact in COUNT_ARTIFACTS}
        | {("sha256", artifact) for artifact in SHA256_ARTIFACTS}
    )
    unexpected = sorted(set(policy) - expected_keys)
    missing = sorted(expected_keys - set(policy))
    if unexpected or missing:
        die(f"base/repair policy key set mismatch; missing={missing}, unexpected={unexpected}")
    return policy_id, base_records, tail_records, dispositions["quarantine"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--source-integrity-anomalies", type=Path, required=True)
    parser.add_argument("--source-integrity-provenance", type=Path, required=True)
    parser.add_argument("--disposition-manifest", type=Path, required=True)
    parser.add_argument("--disposition-provenance", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    args = parser.parse_args()
    policy_id, base_records, tail_records, quarantine = validate_policy(
        args.policy,
        args.source_integrity_anomalies,
        args.source_integrity_provenance,
        args.disposition_manifest,
        args.disposition_provenance,
        args.reference_manifest,
    )
    print(
        f"OK: validated {policy_id}: {base_records} selected records, "
        f"{tail_records} deferred records, {quarantine} future exclusions"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
