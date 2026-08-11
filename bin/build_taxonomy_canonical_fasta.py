#!/usr/bin/env python3
"""Construct a canonical FASTA from a verified legacy BLAST OID stream.

This construction-only tool validates the frozen base policy and live legacy
index, applies dispositions by full record identity, and writes a new FASTA.
It never writes a BLAST index or changes runtime configuration/state.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable

import audit_taxonomy_reference_source_integrity as source_audit
import validate_taxonomy_reference_base_policy as base_policy


SCHEMA = "taxonomy_reference_canonical_fasta_v1"
EXCLUDED_FIELDS = [
    "legacy_oid",
    "reference_id",
    "stored_taxid",
    "reference_sequence_sha256",
    "sequence_length",
]
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class Python38Text(str):
    """Provide the one Python 3.9 string method used by the frozen validator."""

    def removeprefix(self, prefix: str) -> str:
        return self[len(prefix) :] if self.startswith(prefix) else self


def install_python38_policy_compatibility() -> None:
    """Adapt the immutable policy validator without changing its frozen bytes."""

    if sys.version_info >= (3, 9):
        return

    def read_tsv_compat(
        path: Path,
        expected_fields: list[str] | None = None,
    ) -> tuple[list[str], list[dict[str, str]]]:
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle, delimiter="\t")
                fields = reader.fieldnames or []
                raw_rows = list(reader)
        except (OSError, UnicodeError, csv.Error) as error:
            base_policy.die(f"cannot read TSV {path}: {error}")
        if not fields or len(fields) != len(set(fields)):
            base_policy.die(f"invalid or duplicate TSV columns: {path}")
        if expected_fields is not None and fields != expected_fields:
            base_policy.die(f"unexpected TSV schema: {path}")
        if any(
            None in row or any(value is None for value in row.values())
            for row in raw_rows
        ):
            base_policy.die(f"malformed TSV row: {path}")
        rows = [
            {key: Python38Text(value) for key, value in row.items()}
            for row in raw_rows
        ]
        return list(fields), rows

    base_policy.read_tsv = read_tsv_compat


install_python38_policy_compatibility()


def die(message: str) -> "None":
    raise SystemExit(f"ERROR: {message}")


@dataclass(frozen=True)
class ExcludedRecord:
    legacy_oid: int
    reference_id: str
    stored_taxid: str
    sequence_sha256: str
    sequence_length: int


@dataclass(frozen=True)
class ConstructionResult:
    base_records: int
    base_bases: int
    base_sha256: str
    output_records: int
    output_bases: int
    output_sha256: str
    disposition_actions: Counter[str]
    excluded: tuple[ExcludedRecord, ...]


def sha256_file(path: Path) -> str:
    return source_audit.sha256_file(path)


def read_dispositions(path: Path) -> dict[str, dict[str, str]]:
    fields, rows = base_policy.read_tsv(path)
    required = {
        "reference_id",
        "reference_sequence_sha256",
        "stored_taxid",
        "release_action",
    }
    if not rows or not required.issubset(fields):
        die(f"unexpected or empty disposition manifest: {path}")
    result: dict[str, dict[str, str]] = {}
    for row in rows:
        reference_id = row["reference_id"]
        if not reference_id or reference_id in result:
            die(f"invalid or duplicate disposition reference ID: {reference_id}")
        result[reference_id] = row
    return result


def construct_records(
    records: Iterable[source_audit.BlastRecord],
    dispositions: dict[str, dict[str, str]],
    output: BinaryIO,
) -> ConstructionResult:
    base_digest = hashlib.sha256()
    output_digest = hashlib.sha256()
    base_records = 0
    base_bases = 0
    output_records = 0
    output_bases = 0
    seen_dispositions: set[str] = set()
    actions: Counter[str] = Counter()
    excluded: list[ExcludedRecord] = []

    for record in records:
        if record.oid != base_records:
            die(f"legacy index OIDs are not contiguous at {record.oid}")
        canonical = source_audit.canonical_record_bytes(record.title, record.sequence)
        base_digest.update(canonical)
        base_records += 1
        base_bases += record.length

        reference_id = source_audit.reference_id(record.title)
        disposition = dispositions.get(reference_id)
        if disposition is not None:
            if reference_id in seen_dispositions:
                die(
                    "disposition reference occurs more than once in legacy index: "
                    f"{reference_id}"
                )
            observed_sha256 = source_audit.sha256_bytes(record.sequence.encode("utf-8"))
            if observed_sha256 != disposition["reference_sequence_sha256"]:
                die(f"disposition/index sequence mismatch: {reference_id}")
            observed_taxid = source_audit.header_taxid(record.title)
            if observed_taxid != disposition["stored_taxid"]:
                die(f"disposition/index header taxid mismatch: {reference_id}")
            action = disposition["release_action"]
            if action not in {"quarantine", "retain"}:
                die(f"invalid disposition action: {reference_id}")
            seen_dispositions.add(reference_id)
            actions[action] += 1
            if action == "quarantine":
                excluded.append(
                    ExcludedRecord(
                        legacy_oid=record.oid,
                        reference_id=reference_id,
                        stored_taxid=observed_taxid,
                        sequence_sha256=observed_sha256,
                        sequence_length=record.length,
                    )
                )
                continue

        output.write(canonical)
        output_digest.update(canonical)
        output_records += 1
        output_bases += record.length

    missing = sorted(set(dispositions) - seen_dispositions)
    if missing:
        die(f"disposition references are absent from the legacy index: {missing}")
    return ConstructionResult(
        base_records=base_records,
        base_bases=base_bases,
        base_sha256=base_digest.hexdigest(),
        output_records=output_records,
        output_bases=output_bases,
        output_sha256=output_digest.hexdigest(),
        disposition_actions=actions,
        excluded=tuple(excluded),
    )


def excluded_text(records: tuple[ExcludedRecord, ...]) -> str:
    lines = ["\t".join(EXCLUDED_FIELDS)]
    lines.extend(
        "\t".join(
            (
                str(record.legacy_oid),
                record.reference_id,
                record.stored_taxid,
                record.sequence_sha256,
                str(record.sequence_length),
            )
        )
        for record in records
    )
    return "\n".join(lines) + "\n"


def provenance_text(
    args: argparse.Namespace,
    policy_id: str,
    result: ConstructionResult,
    excluded_output: str,
    blast_version: str,
    components: list[tuple[str, Path, str]],
) -> str:
    rows = [
        ("schema", "", SCHEMA),
        ("release", "release_id", args.release_id),
        ("scope", "database", args.scope),
        ("scope", "stage", "construction_only"),
        ("scope", "source_mutation", "none"),
        ("scope", "index_mutation", "none"),
        ("scope", "runtime_activation", "none"),
        ("scope", "state_identity", "unproduced"),
        ("scope", "legacy_state_reuse", "forbidden"),
        ("scope", "artifact_set_commit_marker", "provenance_output_written_last"),
        ("policy", "base_policy_id", policy_id),
        ("input", "base_authority", "effective_legacy_blast_oid_stream"),
        ("input", "record_identity", "legacy_index_oid"),
        ("input", "record_order", "legacy_index_oid_ascending"),
        (
            "decision",
            "disposition_match_identity",
            "reference_id_stored_taxid_sequence_sha256",
        ),
        ("decision", "nonlisted_base_records", "retain_unmodified"),
        ("decision", "implicit_deduplication", "none"),
        (
            "decision",
            "canonical_serialization",
            "greater_than_title_lf_uppercase_sequence_lf",
        ),
        (
            "decision",
            "old_to_new_ordinal_rule",
            "retained_new_oid=legacy_oid-count(excluded_legacy_oid<legacy_oid)",
        ),
        ("artifact", "canonical_fasta_basename", args.output_fasta.name),
        ("artifact", "excluded_legacy_oids_basename", args.excluded_oids_output.name),
        ("tool", "blastdbcmd", blast_version),
        ("count", "base_records", str(result.base_records)),
        ("count", "base_bases", str(result.base_bases)),
        (
            "count",
            "disposition_records",
            str(sum(result.disposition_actions.values())),
        ),
        ("count", "disposition:quarantine", str(result.disposition_actions["quarantine"])),
        ("count", "disposition:retain", str(result.disposition_actions["retain"])),
        (
            "count",
            "nonlisted_base_records",
            str(result.base_records - sum(result.disposition_actions.values())),
        ),
        ("count", "excluded_legacy_oids", str(len(result.excluded))),
        ("count", "excluded_bases", str(result.base_bases - result.output_bases)),
        ("count", "canonical_records", str(result.output_records)),
        ("count", "canonical_bases", str(result.output_bases)),
        ("sha256", "builder_script", sha256_file(Path(__file__))),
        ("sha256", "base_policy_validator_script", sha256_file(Path(base_policy.__file__))),
        ("sha256", "source_index_auditor_script", sha256_file(Path(source_audit.__file__))),
        ("sha256", "base_policy", sha256_file(args.policy)),
        (
            "sha256",
            "source_integrity_anomalies",
            sha256_file(args.source_integrity_anomalies),
        ),
        (
            "sha256",
            "source_integrity_provenance",
            sha256_file(args.source_integrity_provenance),
        ),
        ("sha256", "disposition_manifest", sha256_file(args.disposition_manifest)),
        (
            "sha256",
            "disposition_provenance",
            sha256_file(args.disposition_provenance),
        ),
        ("sha256", "legacy_reference_manifest", sha256_file(args.reference_manifest)),
        ("sha256", "legacy_index_canonical_stream", result.base_sha256),
        ("sha256", "canonical_fasta", result.output_sha256),
        (
            "sha256",
            "excluded_legacy_oids",
            hashlib.sha256(excluded_output.encode("utf-8")).hexdigest(),
        ),
    ]
    for artifact, _, digest in sorted(components):
        rows.append(("sha256", f"legacy_blast_component:{artifact}", digest))
    return "field\tartifact\tvalue\n" + "".join(
        f"{field}\t{artifact}\t{value}\n" for field, artifact, value in rows
    )


def make_temporary(path: Path) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.fchmod(fd, 0o644)
    return fd, Path(temporary)


def write_staged_text(path: Path, content: str) -> Path:
    fd, temporary = make_temporary(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def reject_unsafe_outputs(
    args: argparse.Namespace,
    components: list[tuple[str, Path, str]],
) -> None:
    outputs = [args.output_fasta, args.excluded_oids_output, args.provenance_output]
    resolved_outputs = [path.resolve() for path in outputs]
    if len(set(resolved_outputs)) != len(outputs):
        die("output paths must be distinct")
    if args.output_fasta.suffix.lower() not in {".fa", ".fasta", ".fna"}:
        die("canonical FASTA output must use a FASTA filename suffix")
    database_directory = args.blast_database.resolve().parent
    if any(
        path == database_directory or database_directory in path.parents
        for path in resolved_outputs
    ):
        die("construction outputs must be outside the legacy database directory")
    for path in outputs:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            die(f"output target is not a regular non-symlink file: {path}")
    existing = [str(path) for path in outputs if path.exists()]
    if existing and not args.replace:
        die(f"output targets already exist (use --replace explicitly): {existing}")
    protected = {
        path.resolve()
        for path in (
            args.policy,
            args.source_integrity_anomalies,
            args.source_integrity_provenance,
            args.disposition_manifest,
            args.disposition_provenance,
            args.reference_manifest,
            Path(__file__),
            Path(base_policy.__file__),
            Path(source_audit.__file__),
        )
    }
    protected.update(path.resolve() for _, path, _ in components)
    if protected.intersection(resolved_outputs):
        die("an output path overlaps a protected input or executable")


def install_output_set(
    staged: list[tuple[Path, Path]],
    provenance_destination: Path,
    replace: bool,
) -> None:
    invalidated_provenance: Path | None = None
    if replace and provenance_destination.exists():
        fd, invalidated = tempfile.mkstemp(
            prefix=f".{provenance_destination.name}.invalid-",
            dir=provenance_destination.parent,
        )
        os.close(fd)
        invalidated_provenance = Path(invalidated)
        os.replace(provenance_destination, invalidated_provenance)
        fsync_directory(provenance_destination.parent)
    try:
        for temporary, destination in staged:
            if replace:
                os.replace(temporary, destination)
            else:
                os.link(temporary, destination)
                temporary.unlink()
            fsync_directory(destination.parent)
    finally:
        if invalidated_provenance is not None:
            invalidated_provenance.unlink(missing_ok=True)
            fsync_directory(provenance_destination.parent)


def build(args: argparse.Namespace) -> ConstructionResult:
    if not TOKEN_RE.fullmatch(args.release_id):
        die(f"invalid release ID: {args.release_id}")
    if not TOKEN_RE.fullmatch(args.scope):
        die(f"invalid database scope: {args.scope}")

    policy_id, expected_records, _, expected_quarantine = base_policy.validate_policy(
        args.policy,
        args.source_integrity_anomalies,
        args.source_integrity_provenance,
        args.disposition_manifest,
        args.disposition_provenance,
        args.reference_manifest,
    )
    policy = base_policy.read_key_values(args.policy, "base/repair policy")
    if policy[("scope", "database")] != args.scope:
        die("requested database scope does not match the frozen base policy")
    expected_bases = int(policy[("count", "base_bases")])
    expected_output_records = int(policy[("count", "projected_canonical_records")])
    expected_base_sha256 = policy[("sha256", "selected_base_canonical_stream")]
    expected_retain = int(policy[("count", "disposition:retain")])
    integrity = base_policy.read_key_values(
        args.source_integrity_provenance,
        "source-integrity provenance",
    )
    base_policy.expect_value(
        integrity,
        ("sha256", "auditor_script"),
        sha256_file(Path(source_audit.__file__)),
        "source-integrity provenance",
    )

    metadata, components = source_audit.verify_index_components(
        args.blast_database,
        args.reference_manifest,
        args.reference_root,
    )
    if metadata.get("number-of-sequences") != expected_records:
        die("live legacy index record count does not match the frozen base policy")
    if metadata.get("number-of-letters") != expected_bases:
        die("live legacy index base count does not match the frozen base policy")
    reject_unsafe_outputs(args, components)
    dispositions = read_dispositions(args.disposition_manifest)
    blast_version = source_audit.blastdbcmd_version(args.blastdbcmd)

    fasta_fd, fasta_temporary = make_temporary(args.output_fasta)
    staged: list[tuple[Path, Path]] = []
    try:
        with os.fdopen(fasta_fd, "wb") as output:
            result = construct_records(
                source_audit.run_blastdbcmd_records(
                    args.blastdbcmd,
                    args.blast_database,
                ),
                dispositions,
                output,
            )
            output.flush()
            os.fsync(output.fileno())

        if result.base_records != expected_records or result.base_bases != expected_bases:
            die("live legacy index size does not match the frozen base policy")
        if result.base_sha256 != expected_base_sha256:
            die("live legacy index stream does not match the frozen base policy")
        if result.disposition_actions["quarantine"] != expected_quarantine:
            die("quarantine count does not match the frozen base policy")
        if result.disposition_actions["retain"] != expected_retain:
            die("retained disposition count does not match the frozen base policy")
        if result.output_records != expected_output_records:
            die("canonical record count does not match the frozen projection")

        excluded_output = excluded_text(result.excluded)
        provenance_output = provenance_text(
            args,
            policy_id,
            result,
            excluded_output,
            blast_version,
            components,
        )
        staged.append((fasta_temporary, args.output_fasta))
        staged.append(
            (
                write_staged_text(args.excluded_oids_output, excluded_output),
                args.excluded_oids_output,
            )
        )
        staged.append(
            (
                write_staged_text(args.provenance_output, provenance_output),
                args.provenance_output,
            )
        )
        install_output_set(
            staged,
            args.provenance_output,
            args.replace,
        )
        return result
    finally:
        for temporary in [fasta_temporary, *(item[0] for item in staged)]:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--source-integrity-anomalies", type=Path, required=True)
    parser.add_argument("--source-integrity-provenance", type=Path, required=True)
    parser.add_argument("--disposition-manifest", type=Path, required=True)
    parser.add_argument("--disposition-provenance", type=Path, required=True)
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--blast-database", type=Path, required=True)
    parser.add_argument("--blastdbcmd", default="blastdbcmd")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output-fasta", type=Path, required=True)
    parser.add_argument("--excluded-oids-output", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "replace an existing artifact set after invalidating its provenance marker; "
            "without this flag, any existing output is rejected"
        ),
    )
    args = parser.parse_args()

    result = build(args)
    print(
        f"OK: constructed {result.output_records} records / {result.output_bases} bases; "
        f"excluded {len(result.excluded)} legacy OIDs; sha256={result.output_sha256}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
