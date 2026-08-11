#!/usr/bin/env python3
"""Build and verify a non-activated canonical downstream COI BLAST index.

The generated component hashes identify one fixed build; they are not a claim
that makeblastdb container bytes are reproducible. Reproducibility is instead
proved by exporting every indexed record in canonical form and matching the
frozen canonical FASTA digest.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import audit_taxonomy_reference_source_integrity as source_audit


SCHEMA = "taxonomy_reference_canonical_blastdb_v1"
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EXPECTED_POLICY_ROWS = {
    (
        "priority",
        "confirmed",
        "confirmed_reference_sequence_contamination",
        "quarantine",
        "confirmed_contamination_exclusion",
    ),
    (
        "review",
        "cross_family_sequence_label_conflict_candidate",
        "cross_family_sequence_label_conflict_candidate",
        "quarantine",
        "correctness_first_conservative_conflict_exclusion",
    ),
    (
        "review",
        "unresolved_insufficient_local_discriminator",
        "unresolved_insufficient_local_discriminator",
        "retain",
        "preserve_uncertainty",
    ),
}


def die(message: str) -> "None":
    raise SystemExit(f"ERROR: {message}")


def sha256_file(path: Path) -> str:
    return source_audit.sha256_file(path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_tsv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            fields = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeError, csv.Error) as error:
        die(f"cannot read TSV {path}: {error}")
    if not fields or len(fields) != len(set(fields)):
        die(f"invalid or duplicate TSV columns: {path}")
    if any(
        None in row or any(value is None for value in row.values()) for row in rows
    ):
        die(f"malformed TSV row: {path}")
    return fields, rows


def read_provenance(path: Path) -> dict[tuple[str, str], str]:
    fields, rows = read_tsv(path)
    if fields != ["field", "artifact", "value"] or not rows:
        die(f"unexpected or empty provenance schema: {path}")
    result: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row["field"], row["artifact"])
        if key in result:
            die(f"duplicate provenance key {key}: {path}")
        result[key] = row["value"]
    return result


def require_value(
    values: dict[tuple[str, str], str],
    key: tuple[str, str],
    expected: str,
    label: str,
) -> None:
    if values.get(key) != expected:
        die(f"{label} has unexpected {key[0]}/{key[1]}")


def require_file_sha(
    values: dict[tuple[str, str], str],
    artifact: str,
    path: Path,
    label: str,
) -> None:
    expected = values.get(("sha256", artifact), "")
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        die(f"{label} has invalid or missing SHA-256 for {artifact}")
    if not path.is_file() or path.is_symlink():
        die(f"required regular input is missing or a symlink: {path}")
    if sha256_file(path) != expected:
        die(f"{artifact} does not match {label}: {path}")


def parse_positive_int(value: str, label: str, allow_zero: bool = False) -> int:
    if not value.isdigit():
        die(f"invalid integer for {label}: {value}")
    parsed = int(value)
    if parsed < (0 if allow_zero else 1):
        die(f"invalid integer for {label}: {value}")
    return parsed


def path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class CandidateSummary:
    records: int
    bases: int
    sha256: str
    first_record: source_audit.BlastRecord


@dataclass(frozen=True)
class ExtendedBlastRecord:
    oid: int
    accession: str
    internal_taxids: str
    title: str
    length: int
    sequence: str


@dataclass(frozen=True)
class Expectations:
    release_id: str
    release_policy_id: str
    base_policy_id: str
    records: int
    bases: int
    canonical_sha256: str
    quarantine_records: int
    retain_records: int
    confirmed_records: int
    conflict_records: int
    unresolved_records: int
    excluded: dict[int, dict[str, str]]


@dataclass(frozen=True)
class BuildResult:
    output_dir: Path
    index_prefix: Path
    provenance: Path
    component_count: int
    component_set_sha256: str


def iter_canonical_fasta(path: Path) -> Iterator[source_audit.BlastRecord]:
    try:
        handle = path.open("rb")
    except OSError as error:
        die(f"cannot read canonical FASTA {path}: {error}")
    with handle:
        oid = 0
        while True:
            header_raw = handle.readline()
            if not header_raw:
                break
            if not header_raw.startswith(b">") or not header_raw.endswith(b"\n"):
                die(f"non-canonical FASTA header at OID {oid}: {path}")
            sequence_raw = handle.readline()
            if (
                not sequence_raw
                or sequence_raw.startswith(b">")
                or not sequence_raw.endswith(b"\n")
            ):
                die(f"non-canonical FASTA sequence at OID {oid}: {path}")
            if b"\r" in header_raw or b"\r" in sequence_raw:
                die(f"non-LF canonical FASTA encoding at OID {oid}: {path}")
            try:
                title = header_raw[1:-1].decode("utf-8")
                sequence = sequence_raw[:-1].decode("ascii")
            except UnicodeError as error:
                die(f"invalid FASTA encoding at OID {oid}: {error}")
            if not title or not sequence or sequence != sequence.upper():
                die(f"non-canonical FASTA record at OID {oid}: {path}")
            invalid = sorted(set(sequence) - source_audit.ALLOWED_SEQUENCE)
            if invalid:
                die(f"invalid FASTA sequence characters at OID {oid}: {invalid}")
            yield source_audit.BlastRecord(oid, title, len(sequence), sequence)
            oid += 1


def scan_canonical_fasta(path: Path) -> CandidateSummary:
    digest = hashlib.sha256()
    records = 0
    bases = 0
    first_record = None
    for record in iter_canonical_fasta(path):
        if first_record is None:
            first_record = record
        digest.update(source_audit.canonical_record_bytes(record.title, record.sequence))
        records += 1
        bases += record.length
    if first_record is None:
        die(f"canonical FASTA is empty: {path}")
    return CandidateSummary(records, bases, digest.hexdigest(), first_record)


def read_excluded(path: Path) -> dict[int, dict[str, str]]:
    fields, rows = read_tsv(path)
    expected_fields = [
        "legacy_oid",
        "reference_id",
        "stored_taxid",
        "reference_sequence_sha256",
        "sequence_length",
    ]
    if fields != expected_fields or not rows:
        die(f"unexpected or empty excluded-OID schema: {path}")
    result: dict[int, dict[str, str]] = {}
    previous = -1
    for row in rows:
        oid = parse_positive_int(row["legacy_oid"], "legacy_oid", allow_zero=True)
        if oid <= previous or oid in result:
            die(f"excluded legacy OIDs are not strictly increasing: {path}")
        if not re.fullmatch(r"-?[0-9]+", row["stored_taxid"]):
            die(f"invalid stored taxid for excluded OID {oid}")
        if not re.fullmatch(r"[0-9a-f]{64}", row["reference_sequence_sha256"]):
            die(f"invalid sequence SHA-256 for excluded OID {oid}")
        parse_positive_int(row["sequence_length"], f"excluded OID {oid} length")
        result[oid] = row
        previous = oid
    return result


def validate_policy_chain(args: argparse.Namespace) -> Expectations:
    construction = read_provenance(args.construction_provenance)
    disposition = read_provenance(args.disposition_provenance)
    base_policy = read_provenance(args.base_policy)

    require_value(
        construction,
        ("schema", ""),
        "taxonomy_reference_canonical_fasta_v1",
        "construction provenance",
    )
    require_value(
        construction,
        ("release", "release_id"),
        args.release_id,
        "construction provenance",
    )
    for key, expected in (
        (("scope", "stage"), "construction_only"),
        (("scope", "index_mutation"), "none"),
        (("scope", "runtime_activation"), "none"),
        (("scope", "state_identity"), "unproduced"),
        (("scope", "legacy_state_reuse"), "forbidden"),
    ):
        require_value(construction, key, expected, "construction provenance")
    if construction.get(("artifact", "canonical_fasta_basename")) != args.canonical_fasta.name:
        die("canonical FASTA basename does not match construction provenance")

    require_file_sha(construction, "base_policy", args.base_policy, "construction provenance")
    require_file_sha(
        construction,
        "disposition_manifest",
        args.disposition_manifest,
        "construction provenance",
    )
    require_file_sha(
        construction,
        "disposition_provenance",
        args.disposition_provenance,
        "construction provenance",
    )
    require_file_sha(
        construction,
        "excluded_legacy_oids",
        args.excluded_oids,
        "construction provenance",
    )

    release_policy_id = disposition.get(("policy", "policy_id"), "")
    base_policy_id = construction.get(("policy", "base_policy_id"), "")
    if not TOKEN_RE.fullmatch(release_policy_id) or not TOKEN_RE.fullmatch(base_policy_id):
        die("invalid selected release/base policy identity")
    require_value(base_policy, ("policy", "policy_id"), base_policy_id, "base policy")
    require_file_sha(
        disposition,
        "release_policy",
        args.release_policy,
        "disposition provenance",
    )
    require_file_sha(
        disposition,
        "disposition_manifest",
        args.disposition_manifest,
        "disposition provenance",
    )
    require_file_sha(
        disposition,
        "quarantine_projection",
        args.quarantine_projection,
        "disposition provenance",
    )
    require_file_sha(
        disposition,
        "retained_projection",
        args.retained_projection,
        "disposition provenance",
    )

    release_fields, release_rows = read_tsv(args.release_policy)
    if release_fields != [
        "policy_id",
        "source_tier",
        "source_status",
        "evidence_class",
        "release_action",
        "decision_basis",
    ]:
        die("unexpected release-policy schema")
    observed_policy_rows = {
        (
            row["source_tier"],
            row["source_status"],
            row["evidence_class"],
            row["release_action"],
            row["decision_basis"],
        )
        for row in release_rows
        if row["policy_id"] == release_policy_id
    }
    if len(release_rows) != 3 or observed_policy_rows != EXPECTED_POLICY_ROWS:
        die("selected correctness-first release policy is unexpected")

    _, manifest_rows = read_tsv(args.disposition_manifest)
    actions = Counter(row.get("release_action", "") for row in manifest_rows)
    evidence = Counter(row.get("evidence_class", "") for row in manifest_rows)
    expected_counts = {
        "quarantine": parse_positive_int(
            disposition.get(("count", "quarantine_records"), ""),
            "quarantine records",
        ),
        "retain": parse_positive_int(
            disposition.get(("count", "retain_records"), ""),
            "retain records",
        ),
        "confirmed": parse_positive_int(
            disposition.get(
                ("count", "confirmed_reference_sequence_contamination"), ""
            ),
            "confirmed records",
        ),
        "conflict": parse_positive_int(
            disposition.get(
                ("count", "cross_family_sequence_label_conflict_candidate"), ""
            ),
            "conflict records",
        ),
        "unresolved": parse_positive_int(
            disposition.get(
                ("count", "unresolved_insufficient_local_discriminator"), ""
            ),
            "unresolved records",
        ),
    }
    if actions != Counter(
        {"quarantine": expected_counts["quarantine"], "retain": expected_counts["retain"]}
    ):
        die("disposition action counts do not match selected policy provenance")
    if evidence != Counter(
        {
            "confirmed_reference_sequence_contamination": expected_counts["confirmed"],
            "cross_family_sequence_label_conflict_candidate": expected_counts["conflict"],
            "unresolved_insufficient_local_discriminator": expected_counts["unresolved"],
        }
    ):
        die("disposition evidence counts do not match selected policy provenance")

    records = parse_positive_int(
        construction.get(("count", "canonical_records"), ""),
        "canonical records",
    )
    bases = parse_positive_int(
        construction.get(("count", "canonical_bases"), ""),
        "canonical bases",
    )
    canonical_sha = construction.get(("sha256", "canonical_fasta"), "")
    if not re.fullmatch(r"[0-9a-f]{64}", canonical_sha):
        die("invalid canonical FASTA SHA-256 in construction provenance")
    excluded = read_excluded(args.excluded_oids)
    if len(excluded) != expected_counts["quarantine"]:
        die("excluded-OID count does not match selected policy")
    if construction.get(("count", "excluded_legacy_oids")) != str(len(excluded)):
        die("excluded-OID count does not match construction provenance")

    return Expectations(
        args.release_id,
        release_policy_id,
        base_policy_id,
        records,
        bases,
        canonical_sha,
        expected_counts["quarantine"],
        expected_counts["retain"],
        expected_counts["confirmed"],
        expected_counts["conflict"],
        expected_counts["unresolved"],
        excluded,
    )


def validate_legacy_components(args: argparse.Namespace) -> None:
    construction = read_provenance(args.construction_provenance)
    component_rows = {
        artifact[len("legacy_blast_component:") :]: value
        for (field, artifact), value in construction.items()
        if field == "sha256" and artifact.startswith("legacy_blast_component:")
    }
    if not component_rows:
        die("construction provenance has no legacy BLAST component identities")
    reference_root = args.reference_root.resolve()
    legacy_prefix = args.legacy_blast_database.resolve()
    observed_prefix_components: set[Path] = set()
    for relative, expected_sha in component_rows.items():
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            die(f"unsafe legacy BLAST component path: {relative}")
        component = reference_root / relative
        if not component.is_file() or component.is_symlink():
            die(f"legacy BLAST component missing or a symlink: {component}")
        if sha256_file(component) != expected_sha:
            die(f"legacy BLAST component checksum mismatch: {component}")
        if component.name.startswith(legacy_prefix.name + "."):
            observed_prefix_components.add(component.resolve())
    if len(observed_prefix_components) != len(component_rows):
        die("construction provenance component set does not match legacy database prefix")


def tool_path(command: str) -> str:
    resolved = shutil.which(command)
    if not resolved:
        die(f"required executable not found: {command}")
    return str(Path(resolved).resolve())


def tool_version(executable: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except OSError as error:
        die(f"cannot probe tool version for {executable}: {error}")
    if completed.returncode != 0:
        die(f"tool version probe failed for {executable}: {completed.stderr.strip()}")
    value = " ".join((completed.stdout or completed.stderr).split())
    if not value:
        die(f"tool version probe returned no output: {executable}")
    return value


def iter_extended_blast_records(
    blastdbcmd: str, database: Path
) -> Iterator[ExtendedBlastRecord]:
    command = [
        blastdbcmd,
        "-db",
        str(database),
        "-dbtype",
        "nucl",
        "-entry",
        "all",
        "-outfmt",
        "%o\t%a\t%T\t%t\t%l\t%s",
    ]
    environment = dict(os.environ)
    environment.update({"LC_ALL": "C", "LANG": "C"})
    stderr_file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            encoding="utf-8",
            env=environment,
        )
    except OSError as error:
        stderr_file.close()
        die(f"cannot run blastdbcmd: {error}")
    assert process.stdout is not None
    try:
        for line in process.stdout:
            if not line.strip():
                continue
            fields = line.rstrip("\r\n").split("\t", 5)
            if len(fields) != 6:
                die(f"malformed blastdbcmd verification row for {database}")
            oid_text, accession, internal_taxids, title, length_text, sequence = fields
            if not oid_text.isdigit() or not length_text.isdigit():
                die(f"invalid blastdbcmd OID/length for {database}")
            normalized = sequence.upper()
            length = int(length_text)
            if len(normalized) != length:
                die(f"blastdbcmd length mismatch for OID {oid_text}")
            yield ExtendedBlastRecord(
                int(oid_text),
                accession,
                internal_taxids,
                title,
                length,
                normalized,
            )
        return_code = process.wait()
        if return_code != 0:
            stderr_file.seek(0)
            die(
                f"blastdbcmd export failed ({return_code}): "
                f"{stderr_file.read().strip()}"
            )
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        stderr_file.close()


def verify_new_index(
    candidate_fasta: Path,
    index_prefix: Path,
    blastdbcmd: str,
    expected: Expectations,
) -> CandidateSummary:
    digest = hashlib.sha256()
    records = 0
    bases = 0
    first = None
    pairs = itertools.zip_longest(
        iter_canonical_fasta(candidate_fasta),
        iter_extended_blast_records(blastdbcmd, index_prefix),
    )
    for candidate, indexed in pairs:
        if candidate is None or indexed is None:
            die("candidate FASTA and rebuilt index record counts differ")
        if indexed.oid != candidate.oid:
            die(f"rebuilt index OID mismatch at candidate OID {candidate.oid}")
        if indexed.accession != f"BL_ORD_ID:{indexed.oid}":
            die(f"rebuilt index parsed a sequence identifier at OID {indexed.oid}")
        if indexed.internal_taxids != "0":
            die(f"rebuilt index populated an internal taxid at OID {indexed.oid}")
        if (
            indexed.title != candidate.title
            or indexed.length != candidate.length
            or indexed.sequence != candidate.sequence
        ):
            die(f"rebuilt index content mismatch at OID {indexed.oid}")
        if not source_audit.header_taxid(indexed.title):
            die(f"rebuilt index lost embedded stored taxid at OID {indexed.oid}")
        if first is None:
            first = candidate
        digest.update(source_audit.canonical_record_bytes(indexed.title, indexed.sequence))
        records += 1
        bases += indexed.length
    if first is None:
        die("rebuilt index is empty")
    summary = CandidateSummary(records, bases, digest.hexdigest(), first)
    if (
        summary.records != expected.records
        or summary.bases != expected.bases
        or summary.sha256 != expected.canonical_sha256
    ):
        die("rebuilt index canonical stream does not match frozen construction")
    return summary


def verify_legacy_oid_mapping(
    legacy_database: Path,
    new_database: Path,
    blastdbcmd: str,
    expected: Expectations,
) -> None:
    new_records = iter_extended_blast_records(blastdbcmd, new_database)
    excluded_seen: set[int] = set()
    excluded_before = 0
    retained = 0
    for legacy in source_audit.run_blastdbcmd_records(blastdbcmd, legacy_database):
        exclusion = expected.excluded.get(legacy.oid)
        if exclusion is not None:
            if source_audit.reference_id(legacy.title) != exclusion["reference_id"]:
                die(f"excluded legacy reference mismatch at OID {legacy.oid}")
            if source_audit.header_taxid(legacy.title) != exclusion["stored_taxid"]:
                die(f"excluded legacy taxid mismatch at OID {legacy.oid}")
            if str(legacy.length) != exclusion["sequence_length"]:
                die(f"excluded legacy length mismatch at OID {legacy.oid}")
            if sha256_bytes(legacy.sequence.encode("ascii")) != exclusion[
                "reference_sequence_sha256"
            ]:
                die(f"excluded legacy sequence mismatch at OID {legacy.oid}")
            excluded_seen.add(legacy.oid)
            excluded_before += 1
            continue
        try:
            rebuilt = next(new_records)
        except StopIteration:
            die("rebuilt index ended before the retained legacy stream")
        expected_oid = legacy.oid - excluded_before
        if rebuilt.oid != expected_oid:
            die(f"legacy-to-new OID remap mismatch at legacy OID {legacy.oid}")
        if (
            rebuilt.title != legacy.title
            or rebuilt.length != legacy.length
            or rebuilt.sequence != legacy.sequence
        ):
            die(f"retained legacy record mismatch at legacy OID {legacy.oid}")
        retained += 1
    try:
        extra = next(new_records)
        die(f"rebuilt index has an extra OID {extra.oid}")
    except StopIteration:
        pass
    if excluded_seen != set(expected.excluded):
        die("not every frozen excluded legacy OID was observed")
    if retained != expected.records:
        die("retained legacy record count does not match rebuilt index")


def verify_blast_sseqid(
    index_prefix: Path,
    blastn: str,
    first_record: source_audit.BlastRecord,
    staging_dir: Path,
) -> None:
    query = staging_dir / ".sseqid-smoke-query.fasta"
    query.write_bytes(f">query\n{first_record.sequence}\n".encode("ascii"))
    try:
        try:
            completed = subprocess.run(
                [
                    blastn,
                    "-query",
                    str(query),
                    "-db",
                    str(index_prefix),
                    "-task",
                    "megablast",
                    "-dust",
                    "no",
                    "-max_target_seqs",
                    "50",
                    "-outfmt",
                    "6 sseqid",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            )
        except OSError as error:
            die(f"cannot run BLAST sseqid smoke test: {error}")
    finally:
        query.unlink(missing_ok=True)
    if completed.returncode != 0:
        die(f"BLAST sseqid smoke failed: {completed.stderr.strip()}")
    expected_sseqid = first_record.title.split(None, 1)[0]
    observed = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    if expected_sseqid not in observed:
        die("BLAST sseqid does not preserve the embedded-taxid subject identifier")


def read_index_metadata(index_prefix: Path, expected: Expectations) -> dict[str, object]:
    metadata_path = Path(str(index_prefix) + ".njs")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        die(f"cannot read rebuilt index metadata {metadata_path}: {error}")
    if metadata.get("number-of-sequences") != expected.records:
        die("rebuilt .njs sequence count is incorrect")
    if metadata.get("number-of-letters") != expected.bases:
        die("rebuilt .njs letter count is incorrect")
    if metadata.get("db-version") != 5 or metadata.get("dbtype") != "Nucleotide":
        die("rebuilt .njs database type/version is incorrect")
    volumes = metadata.get("number-of-volumes")
    if not isinstance(volumes, int) or volumes < 1:
        die("rebuilt .njs volume count is invalid")
    return metadata


def enumerate_components(index_prefix: Path, metadata: dict[str, object]) -> list[Path]:
    components = sorted(index_prefix.parent.glob(index_prefix.name + ".*"))
    if not components:
        die("makeblastdb produced no index components")
    for component in components:
        if component.is_symlink() or not component.is_file():
            die(f"index component is not a regular file: {component}")
    metadata_names = metadata.get("files")
    if not isinstance(metadata_names, list) or not all(
        isinstance(name, str) for name in metadata_names
    ):
        die("rebuilt .njs component list is invalid")
    expected_names = set(metadata_names) | {index_prefix.name + ".njs"}
    observed_names = {path.name for path in components}
    if expected_names != observed_names:
        die("enumerated index component set does not match .njs metadata")
    return components


def component_fingerprint(components: list[Path]) -> tuple[str, list[tuple[Path, int, str]]]:
    rows = []
    digest = hashlib.sha256()
    for component in components:
        size = component.stat().st_size
        checksum = sha256_file(component)
        rows.append((component, size, checksum))
        digest.update(f"{component.name}\t{size}\t{checksum}\n".encode("utf-8"))
    return digest.hexdigest(), rows


def safe_tsv_value(value: object) -> str:
    text = str(value)
    if "\t" in text or "\n" in text or "\r" in text:
        die(f"unsafe provenance value: {text!r}")
    return text


def provenance_text(
    args: argparse.Namespace,
    expected: Expectations,
    metadata: dict[str, object],
    components: list[tuple[Path, int, str]],
    component_set_sha: str,
    command: list[str],
    versions: dict[str, str],
    built_utc: str,
) -> str:
    rows: list[tuple[str, str, object]] = [
        ("schema", "", SCHEMA),
        ("release", "release_id", expected.release_id),
        ("scope", "database", "downstream_coi_only"),
        ("scope", "stage", "index_candidate_only"),
        ("scope", "source_mutation", "none"),
        ("scope", "runtime_activation", "none"),
        ("scope", "state_identity", "unproduced"),
        ("scope", "legacy_state_reuse", "forbidden"),
        ("scope", "artifact_set_commit_marker", "provenance_written_last"),
        ("policy", "selection_source", "versioned_merged_release_policy"),
        ("policy", "release_policy_id", expected.release_policy_id),
        ("policy", "base_policy_id", expected.base_policy_id),
        ("artifact", "index_basename", args.index_basename),
        ("artifact", "component_hash_semantics", "fixed_build_tamper_detection"),
        ("verification", "content_hash_semantics", "reproducible_canonical_stream"),
        (
            "verification",
            "fixed_artifact_fingerprint_scope",
            "separately_supplied_release_artifact_only",
        ),
        (
            "verification",
            "clone_rebuild_expectation",
            "semantic_content_match_component_hash_match_not_expected",
        ),
        (
            "verification",
            "fixed_artifact_test_environment",
            "RTBIOSCAN_CANONICAL_BLASTDB_RELEASE",
        ),
        ("verification", "accession_contract", "BL_ORD_ID_colon_oid"),
        ("verification", "internal_taxid_contract", "zero"),
        ("verification", "stored_taxid_source", "signed_kraken_taxid_title_token"),
        ("verification", "legacy_oid_mapping", "full_stream_verified"),
        ("count", "canonical_records", expected.records),
        ("count", "canonical_bases", expected.bases),
        ("count", "disposition:quarantine", expected.quarantine_records),
        ("count", "disposition:retain", expected.retain_records),
        ("count", "evidence:confirmed_contamination", expected.confirmed_records),
        ("count", "evidence:cross_family_conflict", expected.conflict_records),
        ("count", "evidence:unresolved", expected.unresolved_records),
        ("count", "index_components", len(components)),
        ("count", "index_volumes", metadata["number-of-volumes"]),
        ("tool", "makeblastdb", versions["makeblastdb"]),
        ("tool", "blastdbcmd", versions["blastdbcmd"]),
        ("tool", "blastn", versions["blastn"]),
        ("command", "makeblastdb_argv_json", json.dumps(command, separators=(",", ":"))),
        ("build", "built_utc", built_utc),
        ("build", "njs_last_updated", metadata.get("last-updated", "")),
        ("sha256", "builder_script", sha256_file(Path(__file__).resolve())),
        ("sha256", "canonical_fasta", expected.canonical_sha256),
        ("sha256", "construction_provenance", sha256_file(args.construction_provenance)),
        ("sha256", "excluded_legacy_oids", sha256_file(args.excluded_oids)),
        ("sha256", "release_policy", sha256_file(args.release_policy)),
        ("sha256", "base_policy", sha256_file(args.base_policy)),
        ("sha256", "disposition_manifest", sha256_file(args.disposition_manifest)),
        ("sha256", "disposition_provenance", sha256_file(args.disposition_provenance)),
        ("sha256", "quarantine_projection", sha256_file(args.quarantine_projection)),
        ("sha256", "retained_projection", sha256_file(args.retained_projection)),
        ("sha256", "fixed_artifact_component_set", component_set_sha),
    ]
    for component, size, checksum in components:
        rows.append(("component_bytes", component.name, size))
        rows.append(("component_sha256", component.name, checksum))
    lines = ["field\tartifact\tvalue"]
    lines.extend(
        "\t".join((safe_tsv_value(field), safe_tsv_value(artifact), safe_tsv_value(value)))
        for field, artifact, value in rows
    )
    return "\n".join(lines) + "\n"


def write_synced(path: Path, data: str) -> None:
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError as error:
        die(f"cannot open directory for sync {path}: {error}")
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def validate_paths(args: argparse.Namespace) -> tuple[Path, Path]:
    if not TOKEN_RE.fullmatch(args.release_id):
        die(f"invalid release ID: {args.release_id}")
    if not TOKEN_RE.fullmatch(args.index_basename):
        die(f"invalid index basename: {args.index_basename}")
    regular_inputs = [
        args.canonical_fasta,
        args.construction_provenance,
        args.excluded_oids,
        args.release_policy,
        args.disposition_manifest,
        args.disposition_provenance,
        args.quarantine_projection,
        args.retained_projection,
        args.base_policy,
    ]
    for input_path in regular_inputs:
        if input_path.is_symlink() or not input_path.is_file():
            die(f"input must be an existing regular file: {input_path}")
    if args.output_dir.parent.is_symlink():
        die(f"output parent must not be a symlink: {args.output_dir.parent}")
    repository_root = args.repository_root.resolve()
    canonical = args.canonical_fasta.resolve()
    output_parent = args.output_dir.parent.resolve()
    output_dir = output_parent / args.output_dir.name
    if not output_parent.is_dir() or output_parent.is_symlink():
        die(f"output parent must be an existing real directory: {output_parent}")
    if output_dir.exists() or output_dir.is_symlink():
        die(f"output release directory already exists: {output_dir}")
    if path_is_within(canonical, repository_root):
        die("canonical FASTA must remain outside the repository")
    if path_is_within(output_dir, repository_root):
        die("index release directory must remain outside the repository")
    return canonical, output_dir


def build(args: argparse.Namespace) -> BuildResult:
    canonical_fasta, output_dir = validate_paths(args)
    expected = validate_policy_chain(args)
    validate_legacy_components(args)
    candidate_summary = scan_canonical_fasta(canonical_fasta)
    if (
        candidate_summary.records != expected.records
        or candidate_summary.bases != expected.bases
        or candidate_summary.sha256 != expected.canonical_sha256
    ):
        die("canonical FASTA does not match frozen construction provenance")

    makeblastdb = tool_path(args.makeblastdb)
    blastdbcmd = tool_path(args.blastdbcmd)
    blastn = tool_path(args.blastn)
    versions = {
        "makeblastdb": tool_version(makeblastdb),
        "blastdbcmd": tool_version(blastdbcmd),
        "blastn": tool_version(blastn),
    }
    built_utc = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.staging-", dir=str(output_dir.parent)
        )
    )
    index_prefix = staging / args.index_basename
    command = [
        makeblastdb,
        "-in",
        str(canonical_fasta),
        "-dbtype",
        "nucl",
        "-out",
        str(index_prefix),
        "-title",
        expected.release_id,
    ]
    reserved = False
    installed = False
    try:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                env={**os.environ, "LC_ALL": "C", "LANG": "C"},
            )
        except OSError as error:
            die(f"cannot run makeblastdb: {error}")
        if completed.returncode != 0:
            die(f"makeblastdb failed: {completed.stderr.strip()}")
        rebuilt = verify_new_index(canonical_fasta, index_prefix, blastdbcmd, expected)
        verify_legacy_oid_mapping(
            args.legacy_blast_database, index_prefix, blastdbcmd, expected
        )
        verify_blast_sseqid(index_prefix, blastn, rebuilt.first_record, staging)
        metadata = read_index_metadata(index_prefix, expected)
        component_paths = enumerate_components(index_prefix, metadata)
        component_set_sha, component_rows = component_fingerprint(component_paths)
        provenance_name = (
            "chain_a_downstream_coi_canonical_blastdb_v1_provenance.tsv"
        )
        provenance_path = staging / provenance_name
        write_synced(
            provenance_path,
            provenance_text(
                args,
                expected,
                metadata,
                component_rows,
                component_set_sha,
                command,
                versions,
                built_utc,
            ),
        )
        sync_directory(staging)

        os.mkdir(output_dir)
        reserved = True
        final_artifact = output_dir / "artifact"
        os.rename(staging, final_artifact)
        installed = True
        sync_directory(output_dir)
        sync_directory(output_dir.parent)
        return BuildResult(
            output_dir,
            final_artifact / args.index_basename,
            final_artifact / provenance_name,
            len(component_rows),
            component_set_sha,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if reserved and not installed:
            try:
                output_dir.rmdir()
            except OSError:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical-fasta", type=Path, required=True)
    parser.add_argument("--construction-provenance", type=Path, required=True)
    parser.add_argument("--excluded-oids", type=Path, required=True)
    parser.add_argument("--release-policy", type=Path, required=True)
    parser.add_argument("--disposition-manifest", type=Path, required=True)
    parser.add_argument("--disposition-provenance", type=Path, required=True)
    parser.add_argument("--quarantine-projection", type=Path, required=True)
    parser.add_argument("--retained-projection", type=Path, required=True)
    parser.add_argument("--base-policy", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--legacy-blast-database", type=Path, required=True)
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--index-basename", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--makeblastdb", default="makeblastdb")
    parser.add_argument("--blastdbcmd", default="blastdbcmd")
    parser.add_argument("--blastn", default="blastn")
    args = parser.parse_args()

    result = build(args)
    print(
        f"OK: built verified index {result.index_prefix}; "
        f"components={result.component_count}; "
        f"fixed_artifact_sha256={result.component_set_sha256}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
