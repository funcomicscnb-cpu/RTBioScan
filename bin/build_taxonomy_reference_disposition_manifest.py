#!/usr/bin/env python3
"""Build a deterministic, policy-explicit reference disposition manifest.

This offline database-curation tool joins an audit, independently confirmed
references, and lower-tier adjudications. It validates every selected sequence
against a pinned shipped source FASTA snapshot and does not modify that FASTA,
an index, or pipeline behavior.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import tempfile
from collections import Counter
from pathlib import Path


OUTPUT_FIELDS = [
    "reference_id",
    "reference_sequence_sha256",
    "stored_taxid",
    "header_kingdom",
    "header_lineage",
    "source_taxid_record_count",
    "post_policy_taxid_record_count",
    "source_exact_sequence_record_count",
    "post_policy_exact_sequence_record_count",
    "source_tier",
    "source_status",
    "evidence_class",
    "evidence_source",
    "supporting_reference_id",
    "supporting_reference_role",
    "release_action",
    "decision_basis",
    "policy_id",
    "notes",
]

CONFIRMED_CLASS = "confirmed_reference_sequence_contamination"
CONFLICT_CLASS = "cross_family_sequence_label_conflict_candidate"
UNRESOLVED_CLASS = "unresolved_insufficient_local_discriminator"


def die(message: str) -> "None":
    raise SystemExit(f"ERROR: {message}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_tsv(path: Path, required: set[str], label: str) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows or not required.issubset(rows[0]):
        die(f"unexpected or empty {label}: {path}")
    return rows


def require_unique_ids(rows: list[dict[str, str]], label: str) -> None:
    ids = [row["reference_id"] for row in rows]
    if any(not reference_id for reference_id in ids):
        die(f"{label} contains an empty reference ID")
    duplicates = sorted(
        reference_id for reference_id, count in Counter(ids).items() if count > 1
    )
    if duplicates:
        die(f"{label} reference IDs are not unique: {duplicates}")


def read_audit(path: Path) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    rows = read_tsv(
        path,
        {
            "source_accession",
            "reference_id",
            "stored_taxid",
            "header_kingdom",
            "header_lineage",
            "reference_sequence_sha256",
            "discovery_tier",
        },
        "candidate audit",
    )
    require_unique_ids(rows, "candidate audit")
    for row in rows:
        if not re.fullmatch(r"-?[0-9]+", row["stored_taxid"]):
            die(f"invalid audit taxid for {row['reference_id']}")
        if not re.fullmatch(r"[0-9a-f]{64}", row["reference_sequence_sha256"]):
            die(f"invalid audit sequence checksum for {row['reference_id']}")
    return rows, {row["reference_id"]: row for row in rows}


def read_confirmed_references(path: Path) -> list[dict[str, str]]:
    rows = read_tsv(
        path,
        {
            "source_accession",
            "reference_id",
            "stored_taxid",
            "evidence_status",
            "corrected_outcome",
        },
        "confirmed-reference manifest",
    )
    require_unique_ids(rows, "confirmed-reference manifest")
    nonconfirmed = sorted(
        row["reference_id"] for row in rows if row["evidence_status"] != "confirmed"
    )
    if nonconfirmed:
        die(f"confirmed-reference manifest contains non-confirmed rows: {nonconfirmed}")
    return rows


def read_lower_adjudications(path: Path) -> list[dict[str, str]]:
    rows = read_tsv(
        path,
        {
            "reference_id",
            "reference_sequence_sha256",
            "stored_taxid",
            "cross_family_reference_id",
            "cross_family_reference_role",
            "disposition",
        },
        "lower-tier adjudication",
    )
    require_unique_ids(rows, "lower-tier adjudication")
    allowed = {CONFLICT_CLASS, UNRESOLVED_CLASS}
    unexpected = sorted(
        row["reference_id"] for row in rows if row["disposition"] not in allowed
    )
    if unexpected:
        die(f"lower-tier adjudication contains unknown dispositions: {unexpected}")
    for row in rows:
        if not re.fullmatch(r"[0-9a-f]{64}", row["reference_sequence_sha256"]):
            die(f"invalid lower-tier sequence checksum for {row['reference_id']}")
        if row["disposition"] == CONFLICT_CLASS:
            if not row["cross_family_reference_id"] or row[
                "cross_family_reference_role"
            ] not in {"review_candidate", "confirmed_anchor"}:
                die(f"conflict candidate lacks declared support: {row['reference_id']}")
        elif row["cross_family_reference_id"] or row["cross_family_reference_role"]:
            die(f"unresolved row unexpectedly declares conflict support: {row['reference_id']}")
    return rows


def read_release_policy(
    path: Path,
) -> tuple[str, dict[tuple[str, str], dict[str, str]]]:
    rows = read_tsv(
        path,
        {
            "policy_id",
            "source_tier",
            "source_status",
            "evidence_class",
            "release_action",
            "decision_basis",
        },
        "release policy",
    )
    policy_ids = {row["policy_id"] for row in rows if row["policy_id"]}
    if len(policy_ids) != 1:
        die("release policy must declare exactly one non-empty policy ID")
    policy_id = next(iter(policy_ids))
    if any(row["policy_id"] != policy_id for row in rows):
        die("release policy contains an empty or inconsistent policy ID")
    mappings: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["source_tier"], row["source_status"])
        if not all(key) or not row["evidence_class"] or not row["decision_basis"]:
            die("release policy contains an empty mapping field")
        if key in mappings:
            die(f"release policy mapping is not unique: {key}")
        if row["release_action"] not in {"quarantine", "retain"}:
            die(f"release policy has invalid action for {key}")
        mappings[key] = row
    return policy_id, mappings


def verify_provenance_bindings(
    path: Path,
    expected_schema: str,
    artifacts: list[Path],
) -> None:
    rows = read_tsv(path, {"field", "artifact", "value"}, "evidence provenance")
    schemas = [row["value"] for row in rows if row["field"] == "schema"]
    if schemas != [expected_schema]:
        die(f"unexpected evidence provenance schema: {path}")
    declared_hashes = [
        (row["artifact"], row["value"])
        for row in rows
        if row["field"] == "sha256"
    ]
    for artifact in artifacts:
        digest = sha256_file(artifact)
        matching = [
            value
            for declared_artifact, value in declared_hashes
            if Path(declared_artifact).name == artifact.name
        ]
        if matching != [digest]:
            die(f"evidence provenance does not bind {artifact}: {path}")


def verify_upstream_provenance(args: argparse.Namespace) -> None:
    verify_provenance_bindings(
        args.audit_provenance,
        "chain_a_similarity_audit_v1",
        [args.audit, args.reference_source_fasta],
    )
    verify_provenance_bindings(
        args.lower_tier_provenance,
        "chain_a_local_cluster_adjudication_v2",
        [
            args.audit,
            args.confirmed_reference_manifest,
            args.lower_tier_adjudication,
            args.reference_source_fasta,
        ],
    )


def validate_support_references(
    confirmed_rows: list[dict[str, str]],
    lower_rows: list[dict[str, str]],
) -> None:
    confirmed_ids = {row["reference_id"] for row in confirmed_rows}
    lower_by_id = {row["reference_id"]: row for row in lower_rows}
    for row in lower_rows:
        if row["disposition"] != CONFLICT_CLASS:
            continue
        reference_id = row["reference_id"]
        supporting_id = row["cross_family_reference_id"]
        supporting_role = row["cross_family_reference_role"]
        if supporting_id == reference_id:
            die(f"conflict candidate cannot support itself: {reference_id}")
        if supporting_role == "review_candidate":
            supporting_row = lower_by_id.get(supporting_id)
            if supporting_row is None:
                die(
                    "review-candidate support is not in the lower-tier set: "
                    f"{reference_id} -> {supporting_id}"
                )
            if supporting_row["disposition"] != CONFLICT_CLASS:
                die(
                    "review-candidate support is not a conflict candidate: "
                    f"{reference_id} -> {supporting_id}"
                )
        if supporting_role == "confirmed_anchor" and supporting_id not in confirmed_ids:
            die(
                "confirmed-anchor support is not in the confirmed set: "
                f"{reference_id} -> {supporting_id}"
            )


def validate_partition(
    audit_rows: list[dict[str, str]],
    audit_by_id: dict[str, dict[str, str]],
    confirmed_rows: list[dict[str, str]],
    lower_rows: list[dict[str, str]],
) -> None:
    confirmed_ids = {row["reference_id"] for row in confirmed_rows}
    lower_ids = {row["reference_id"] for row in lower_rows}
    overlap = sorted(confirmed_ids.intersection(lower_ids))
    if overlap:
        die(f"confirmed and lower-tier reference sets overlap: {overlap}")
    validate_support_references(confirmed_rows, lower_rows)
    selected_ids = confirmed_ids.union(lower_ids)
    audit_ids = set(audit_by_id)
    if selected_ids != audit_ids:
        missing = sorted(audit_ids - selected_ids)
        extra = sorted(selected_ids - audit_ids)
        die(f"disposition inputs do not partition the audit (missing={missing}, extra={extra})")

    for row in confirmed_rows:
        audit = audit_by_id[row["reference_id"]]
        if audit["discovery_tier"] != "priority":
            die(f"confirmed reference is not priority-tier: {row['reference_id']}")
        if row["stored_taxid"] != audit["stored_taxid"]:
            die(f"confirmed-reference/audit taxid mismatch: {row['reference_id']}")
    for row in lower_rows:
        audit = audit_by_id[row["reference_id"]]
        if audit["discovery_tier"] != "review":
            die(f"lower adjudication is not review-tier: {row['reference_id']}")
        if row["stored_taxid"] != audit["stored_taxid"]:
            die(f"lower-tier/audit taxid mismatch: {row['reference_id']}")
        if row["reference_sequence_sha256"] != audit["reference_sequence_sha256"]:
            die(f"lower-tier/audit sequence mismatch: {row['reference_id']}")
    if len(audit_rows) != len(selected_ids):
        die("candidate audit row count does not match its unique reference partition")


def iter_fasta(path: Path):
    header = None
    sequence: list[str] = []
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if line.startswith(">"):
                if header is not None:
                    yield header, "".join(sequence).upper()
                header = line[1:]
                sequence = []
            elif line:
                if header is None:
                    die(f"sequence before first header in {path}")
                sequence.append(line.strip())
    if header is not None:
        yield header, "".join(sequence).upper()


def verify_source_fasta(
    path: Path, audit_by_id: dict[str, dict[str, str]]
) -> tuple[dict[str, int], Counter[str], Counter[str]]:
    found: set[str] = set()
    source_ids: set[str] = set()
    duplicate_source_ids: set[str] = set()
    selected_taxids = {row["stored_taxid"] for row in audit_by_id.values()}
    selected_hashes = {
        row["reference_sequence_sha256"] for row in audit_by_id.values()
    }
    selected_taxid_counts: Counter[str] = Counter()
    selected_hash_counts: Counter[str] = Counter()
    record_count = 0
    empty_record_count = 0
    missing_taxid_count = 0
    for header, sequence in iter_fasta(path):
        record_count += 1
        reference_id = header.split()[0].split("|", 1)[0]
        if reference_id in source_ids:
            duplicate_source_ids.add(reference_id)
        else:
            source_ids.add(reference_id)
        if not sequence:
            empty_record_count += 1
        taxid_match = re.search(r"\|kraken:taxid\|(-?\d+)", header)
        source_taxid = taxid_match.group(1) if taxid_match else ""
        if not source_taxid:
            missing_taxid_count += 1
        elif source_taxid in selected_taxids:
            selected_taxid_counts[source_taxid] += 1
        digest = sha256_bytes(sequence.encode("ascii"))
        if digest in selected_hashes:
            selected_hash_counts[digest] += 1
        if reference_id not in audit_by_id:
            continue
        if reference_id in found:
            die(f"duplicate selected reference in source FASTA: {reference_id}")
        audit = audit_by_id[reference_id]
        if source_taxid != audit["stored_taxid"]:
            die(f"source/audit taxid mismatch for {reference_id}")
        if digest != audit["reference_sequence_sha256"]:
            die(f"source/audit sequence mismatch for {reference_id}")
        found.add(reference_id)
    missing = sorted(set(audit_by_id) - found)
    if missing:
        die(f"audited references missing from source FASTA: {missing}")
    return (
        {
            "records": record_count,
            "unique_reference_ids": len(source_ids),
            "duplicate_reference_ids": len(duplicate_source_ids),
            "empty_records": empty_record_count,
            "missing_taxid_records": missing_taxid_count,
        },
        selected_taxid_counts,
        selected_hash_counts,
    )


def build_dispositions(
    audit_by_id: dict[str, dict[str, str]],
    confirmed_rows: list[dict[str, str]],
    lower_rows: list[dict[str, str]],
    policy_id: str,
    policy_mappings: dict[tuple[str, str], dict[str, str]],
) -> list[dict[str, str]]:
    dispositions: list[dict[str, str]] = []
    used_policy_keys: set[tuple[str, str]] = set()
    for source in confirmed_rows:
        audit = audit_by_id[source["reference_id"]]
        policy_key = ("priority", source["evidence_status"])
        if policy_key not in policy_mappings:
            die(f"release policy does not map source state: {policy_key}")
        policy = policy_mappings[policy_key]
        if policy["evidence_class"] != CONFIRMED_CLASS:
            die("release policy changes confirmed-reference evidence class")
        used_policy_keys.add(policy_key)
        dispositions.append(
            {
                "reference_id": source["reference_id"],
                "reference_sequence_sha256": audit["reference_sequence_sha256"],
                "stored_taxid": audit["stored_taxid"],
                "header_kingdom": audit["header_kingdom"],
                "header_lineage": audit["header_lineage"],
                "source_tier": "priority",
                "source_status": source["evidence_status"],
                "evidence_class": policy["evidence_class"],
                "evidence_source": "confirmed_reference_manifest",
                "supporting_reference_id": audit["source_accession"],
                "supporting_reference_role": "confirmed_bacterial_control",
                "release_action": policy["release_action"],
                "decision_basis": policy["decision_basis"],
                "policy_id": policy_id,
                "notes": (
                    "Independent priority adjudication confirms reference-sequence "
                    "contamination without asserting specimen misidentification"
                ),
            }
        )
    for source in lower_rows:
        audit = audit_by_id[source["reference_id"]]
        policy_key = ("review", source["disposition"])
        if policy_key not in policy_mappings:
            die(f"release policy does not map source state: {policy_key}")
        policy = policy_mappings[policy_key]
        if policy["evidence_class"] != source["disposition"]:
            die(f"release policy changes lower-tier evidence class: {policy_key}")
        if source["disposition"] == UNRESOLVED_CLASS and policy[
            "release_action"
        ] != "retain":
            die("release policy must retain unresolved references")
        used_policy_keys.add(policy_key)
        if source["disposition"] == CONFLICT_CLASS:
            supporting_id = source["cross_family_reference_id"]
            supporting_role = source["cross_family_reference_role"]
            notes = (
                "Cross-family sequence/label conflict; release action is an explicit "
                "policy choice and does not assert bacterial origin or which endpoint is wrong"
            )
        else:
            supporting_id = ""
            supporting_role = ""
            notes = (
                "Insufficient local discriminator; retained without assigning a clean or "
                "contaminated biological conclusion"
            )
        dispositions.append(
            {
                "reference_id": source["reference_id"],
                "reference_sequence_sha256": audit["reference_sequence_sha256"],
                "stored_taxid": audit["stored_taxid"],
                "header_kingdom": audit["header_kingdom"],
                "header_lineage": audit["header_lineage"],
                "source_tier": "review",
                "source_status": source["disposition"],
                "evidence_class": policy["evidence_class"],
                "evidence_source": "lower_tier_adjudication",
                "supporting_reference_id": supporting_id,
                "supporting_reference_role": supporting_role,
                "release_action": policy["release_action"],
                "decision_basis": policy["decision_basis"],
                "policy_id": policy_id,
                "notes": notes,
            }
        )
    unused_policy_keys = sorted(set(policy_mappings) - used_policy_keys)
    if unused_policy_keys:
        die(f"release policy contains unused mappings: {unused_policy_keys}")
    return sorted(dispositions, key=lambda row: row["reference_id"])


def add_release_impact(
    dispositions: list[dict[str, str]],
    source_taxid_counts: Counter[str],
    source_hash_counts: Counter[str],
) -> None:
    quarantined_taxids = Counter(
        row["stored_taxid"]
        for row in dispositions
        if row["release_action"] == "quarantine"
    )
    quarantined_hashes = Counter(
        row["reference_sequence_sha256"]
        for row in dispositions
        if row["release_action"] == "quarantine"
    )
    for row in dispositions:
        taxid = row["stored_taxid"]
        sequence_hash = row["reference_sequence_sha256"]
        source_taxid_count = source_taxid_counts[taxid]
        source_hash_count = source_hash_counts[sequence_hash]
        if source_taxid_count < quarantined_taxids[taxid]:
            die(f"quarantine count exceeds source taxid count: {taxid}")
        if source_hash_count < quarantined_hashes[sequence_hash]:
            die(f"quarantine count exceeds source sequence count: {sequence_hash}")
        row["source_taxid_record_count"] = str(source_taxid_count)
        row["post_policy_taxid_record_count"] = str(
            source_taxid_count - quarantined_taxids[taxid]
        )
        row["source_exact_sequence_record_count"] = str(source_hash_count)
        row["post_policy_exact_sequence_record_count"] = str(
            source_hash_count - quarantined_hashes[sequence_hash]
        )


def tsv_text(fields: list[str], rows: list[dict[str, str]]) -> str:
    lines = ["\t".join(fields)]
    lines.extend("\t".join(row[field] for field in fields) for row in rows)
    return "\n".join(lines) + "\n"


def provenance_text(
    args: argparse.Namespace,
    dispositions: list[dict[str, str]],
    manifest_text: str,
    quarantine_text: str,
    retained_text: str,
    source_stats: dict[str, int],
    policy_id: str,
) -> str:
    evidence_counts = Counter(row["evidence_class"] for row in dispositions)
    action_counts = Counter(row["release_action"] for row in dispositions)
    quarantine_rows = [
        row for row in dispositions if row["release_action"] == "quarantine"
    ]
    depleted_taxids = {
        row["stored_taxid"]
        for row in quarantine_rows
        if row["post_policy_taxid_record_count"] == "0"
    }
    quarantine_hashes = {
        row["reference_sequence_sha256"] for row in quarantine_rows
    }
    depleted_hashes = {
        row["reference_sequence_sha256"]
        for row in quarantine_rows
        if row["post_policy_exact_sequence_record_count"] == "0"
    }
    rows = [
        ("schema", "", "taxonomy_reference_disposition_v1"),
        ("scope", "database", "downstream_coi_only"),
        ("scope", "records", "declared_chain_a_queue_only"),
        ("scope", "nonlisted_source_records", "outside_queue_not_adjudicated"),
        ("scope", "source_snapshot", "shipped_full_fasta_observation_only"),
        ("scope", "canonical_base_snapshot", "undecided"),
        ("scope", "artifact_set_commit_marker", "provenance_output_written_last"),
        ("policy", "policy_id", policy_id),
        ("count", "source_fasta_records", str(source_stats["records"])),
        (
            "count",
            "source_fasta_unique_reference_ids",
            str(source_stats["unique_reference_ids"]),
        ),
        (
            "count",
            "source_fasta_duplicate_reference_ids",
            str(source_stats["duplicate_reference_ids"]),
        ),
        ("count", "source_fasta_empty_records", str(source_stats["empty_records"])),
        (
            "count",
            "source_fasta_missing_taxid_records",
            str(source_stats["missing_taxid_records"]),
        ),
        ("count", "all_disposition_records", str(len(dispositions))),
        ("count", "quarantine_records", str(action_counts["quarantine"])),
        ("count", "retain_records", str(action_counts["retain"])),
        ("count", CONFIRMED_CLASS, str(evidence_counts[CONFIRMED_CLASS])),
        ("count", CONFLICT_CLASS, str(evidence_counts[CONFLICT_CLASS])),
        ("count", UNRESOLVED_CLASS, str(evidence_counts[UNRESOLVED_CLASS])),
        (
            "count",
            "quarantine_taxids_losing_all_source_records",
            str(len(depleted_taxids)),
        ),
        ("count", "quarantine_unique_sequence_hashes", str(len(quarantine_hashes))),
        (
            "count",
            "quarantine_sequence_hashes_losing_all_source_records",
            str(len(depleted_hashes)),
        ),
        ("sha256", "builder_script", sha256_file(Path(__file__))),
        ("sha256", "candidate_audit", sha256_file(args.audit)),
        ("sha256", "candidate_audit_provenance", sha256_file(args.audit_provenance)),
        (
            "sha256",
            "confirmed_reference_manifest",
            sha256_file(args.confirmed_reference_manifest),
        ),
        (
            "sha256",
            "lower_tier_adjudication",
            sha256_file(args.lower_tier_adjudication),
        ),
        (
            "sha256",
            "lower_tier_adjudication_provenance",
            sha256_file(args.lower_tier_provenance),
        ),
        ("sha256", "release_policy", sha256_file(args.release_policy)),
        ("sha256", "reference_source_fasta", sha256_file(args.reference_source_fasta)),
        ("sha256", "disposition_manifest", sha256_bytes(manifest_text.encode("utf-8"))),
        ("sha256", "quarantine_projection", sha256_bytes(quarantine_text.encode("utf-8"))),
        ("sha256", "retained_projection", sha256_bytes(retained_text.encode("utf-8"))),
    ]
    return "field\tartifact\tvalue\n" + "".join(
        f"{field}\t{artifact}\t{value}\n" for field, artifact, value in rows
    )


def write_output_set(outputs: list[tuple[Path, str]]) -> None:
    for path, _ in outputs:
        if path.exists() and not path.is_file():
            die(f"output target is not a regular file: {path}")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            die(f"cannot prepare output directory for {path}: {error}")

    staged: list[tuple[str, Path]] = []
    try:
        for path, content in outputs:
            fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            staged.append((temporary, path))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        for temporary, path in staged:
            os.replace(temporary, path)
    except OSError as error:
        die(f"cannot write output set: {error}")
    finally:
        for temporary, _ in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--audit-provenance", type=Path, required=True)
    parser.add_argument("--confirmed-reference-manifest", type=Path, required=True)
    parser.add_argument("--lower-tier-adjudication", type=Path, required=True)
    parser.add_argument("--lower-tier-provenance", type=Path, required=True)
    parser.add_argument("--release-policy", type=Path, required=True)
    parser.add_argument("--reference-source-fasta", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path, required=True)
    parser.add_argument("--quarantine-output", type=Path, required=True)
    parser.add_argument("--retained-output", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    args = parser.parse_args()

    for path in (
        args.audit,
        args.audit_provenance,
        args.confirmed_reference_manifest,
        args.lower_tier_adjudication,
        args.lower_tier_provenance,
        args.release_policy,
        args.reference_source_fasta,
    ):
        if not path.is_file():
            die(f"required input is missing: {path}")

    input_paths = {
        path.resolve()
        for path in (
            args.audit,
            args.audit_provenance,
            args.confirmed_reference_manifest,
            args.lower_tier_adjudication,
            args.lower_tier_provenance,
            args.release_policy,
            args.reference_source_fasta,
        )
    }
    output_paths = [
        args.manifest_output.resolve(),
        args.quarantine_output.resolve(),
        args.retained_output.resolve(),
        args.provenance_output.resolve(),
    ]
    if len(output_paths) != len(set(output_paths)):
        die("output paths must be distinct")
    if input_paths.intersection(output_paths):
        die("an output path overlaps a required input")

    verify_upstream_provenance(args)

    audit_rows, audit_by_id = read_audit(args.audit)
    confirmed_rows = read_confirmed_references(args.confirmed_reference_manifest)
    lower_rows = read_lower_adjudications(args.lower_tier_adjudication)
    policy_id, policy_mappings = read_release_policy(args.release_policy)
    validate_partition(audit_rows, audit_by_id, confirmed_rows, lower_rows)
    source_stats, source_taxid_counts, source_hash_counts = verify_source_fasta(
        args.reference_source_fasta, audit_by_id
    )
    dispositions = build_dispositions(
        audit_by_id,
        confirmed_rows,
        lower_rows,
        policy_id,
        policy_mappings,
    )
    add_release_impact(dispositions, source_taxid_counts, source_hash_counts)
    manifest_text = tsv_text(OUTPUT_FIELDS, dispositions)
    quarantine_text = tsv_text(
        OUTPUT_FIELDS,
        [row for row in dispositions if row["release_action"] == "quarantine"],
    )
    retained_text = tsv_text(
        OUTPUT_FIELDS,
        [row for row in dispositions if row["release_action"] == "retain"],
    )
    provenance = provenance_text(
        args,
        dispositions,
        manifest_text,
        quarantine_text,
        retained_text,
        source_stats,
        policy_id,
    )
    write_output_set(
        [
            (args.manifest_output, manifest_text),
            (args.quarantine_output, quarantine_text),
            (args.retained_output, retained_text),
            (args.provenance_output, provenance),
        ]
    )
    action_counts = Counter(row["release_action"] for row in dispositions)
    print(
        f"OK: wrote {len(dispositions)} reference dispositions "
        f"({action_counts['quarantine']} quarantine, {action_counts['retain']} retain)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
