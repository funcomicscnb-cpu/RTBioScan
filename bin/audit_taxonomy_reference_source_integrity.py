#!/usr/bin/env python3
"""Audit a source FASTA against the effective contents of a BLAST database.

The source is never rewritten. Non-leading FASTA header delimiters are split
only in an analysis stream so their effect can be compared with the indexed
records and reported explicitly.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


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

ALLOWED_SEQUENCE = frozenset("ACGTURYSWKMBDHVN-?")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
TAXID_RE = re.compile(r"(?:^|\|)kraken:taxid\|(-?[0-9]+)(?:\s|$)")


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


def reference_id(header: str) -> str:
    value = header.split(None, 1)[0] if header else ""
    if not value:
        die("FASTA record has an empty reference ID")
    return value.split("|", 1)[0]


def header_taxid(header: str) -> str:
    match = TAXID_RE.search(header)
    return match.group(1) if match else ""


def canonical_record_bytes(header: str, sequence: str) -> bytes:
    return f">{header}\n{sequence.upper()}\n".encode("utf-8")


def empty_anomaly(anomaly_class: str) -> dict[str, str]:
    row = {field: "" for field in ANOMALY_FIELDS}
    row["anomaly_class"] = anomaly_class
    return row


@dataclass(frozen=True)
class LogicalRecord:
    header: str
    sequence: str
    logical_ordinal: int
    physical_ordinal: int
    header_line: int
    header_byte_offset: int
    contains_embedded_boundary: bool = False


@dataclass(frozen=True)
class BlastRecord:
    oid: int
    title: str
    length: int
    sequence: str


class SourceScanner:
    def __init__(self, path: Path, indexed_record_count: int):
        self.path = path
        self.indexed_record_count = indexed_record_count
        self.physical_record_count = 0
        self.logical_record_count = 0
        self.logical_total_bases = 0
        self.logical_selected_bases = 0
        self.physical_selected_sequence_characters = 0
        self.physical_tail_records = 0
        self.logical_tail_records = 0
        self.logical_empty_records = 0
        self.embedded_header_candidates = 0
        self.line_count = 0
        self.physical_ids: dict[str, int] = {}
        self.logical_ids: dict[str, dict[str, object]] = {}
        self.anomalies: list[dict[str, str]] = []
        self.selected_digest = hashlib.sha256()
        self.logical_tail_digest = hashlib.sha256()
        self.physical_tail_digest = hashlib.sha256()
        self.index_comparisons: dict[int, BlastRecord] = {}
        self._physical_header = ""
        self._physical_header_line = 0
        self._physical_header_offset = 0
        self._physical_sequence_parts: list[str] = []
        self._physical_embedded: list[dict[str, object]] = []
        self._logical_header = ""
        self._logical_header_line = 0
        self._logical_header_offset = 0
        self._logical_sequence_parts: list[str] = []
        self._logical_origin: dict[str, object] | None = None

    def note_index_record(self, source: LogicalRecord, indexed: BlastRecord) -> None:
        if source.contains_embedded_boundary:
            self.index_comparisons[source.physical_ordinal] = indexed

    def _start_physical(self, header: str, line: int, offset: int) -> None:
        self.physical_record_count += 1
        self._physical_header = header
        self._physical_header_line = line
        self._physical_header_offset = offset
        self._physical_sequence_parts = []
        self._physical_embedded = []
        current_id = reference_id(header)
        self.physical_ids.setdefault(current_id, self.physical_record_count)

    def _start_logical(
        self,
        header: str,
        line: int,
        offset: int,
        origin: dict[str, object] | None = None,
    ) -> None:
        self._logical_header = header
        self._logical_header_line = line
        self._logical_header_offset = offset
        self._logical_sequence_parts = []
        self._logical_origin = origin

    def _finish_logical(self, embedded_boundary: bool = False) -> LogicalRecord:
        if not self._logical_header:
            die(f"empty logical FASTA header in {self.path}")
        self.logical_record_count += 1
        sequence = "".join(self._logical_sequence_parts).upper()
        current_id = reference_id(self._logical_header)
        sequence_sha = sha256_bytes(sequence.encode("utf-8"))
        invalid = "".join(sorted(set(sequence) - ALLOWED_SEQUENCE))
        if invalid:
            row = empty_anomaly("invalid_sequence_alphabet")
            row.update(
                {
                    "source_line": str(self._logical_header_line),
                    "source_byte_offset": str(self._logical_header_offset),
                    "physical_record_ordinal": str(self.physical_record_count),
                    "logical_record_ordinal": str(self.logical_record_count),
                    "reference_id": current_id,
                    "source_sequence_length": str(len(sequence)),
                    "source_sequence_sha256": sequence_sha,
                    "interpretation": f"Sequence contains non-IUPAC characters: {invalid}",
                }
            )
            self.anomalies.append(row)
        if not header_taxid(self._logical_header):
            row = empty_anomaly("missing_or_invalid_header_taxid")
            row.update(
                {
                    "source_line": str(self._logical_header_line),
                    "source_byte_offset": str(self._logical_header_offset),
                    "physical_record_ordinal": str(self.physical_record_count),
                    "logical_record_ordinal": str(self.logical_record_count),
                    "reference_id": current_id,
                    "source_sequence_length": str(len(sequence)),
                    "source_sequence_sha256": sequence_sha,
                    "interpretation": "Header lacks a signed kraken taxid token",
                }
            )
            self.anomalies.append(row)
        previous = self.logical_ids.get(current_id)
        if previous is not None:
            row = empty_anomaly("duplicate_reference_id")
            row.update(
                {
                    "source_line": str(self._logical_header_line),
                    "source_byte_offset": str(self._logical_header_offset),
                    "physical_record_ordinal": str(self.physical_record_count),
                    "logical_record_ordinal": str(self.logical_record_count),
                    "reference_id": current_id,
                    "related_logical_record_ordinal": str(previous["ordinal"]),
                    "related_reference_id": current_id,
                    "source_sequence_length": str(len(sequence)),
                    "comparison_sequence_length": str(previous["length"]),
                    "source_sequence_sha256": sequence_sha,
                    "comparison_sequence_sha256": str(previous["sha256"]),
                    "interpretation": "Reference ID repeats an earlier logical record",
                }
            )
            self.anomalies.append(row)
        else:
            self.logical_ids[current_id] = {
                "ordinal": self.logical_record_count,
                "length": len(sequence),
                "sha256": sequence_sha,
            }
        if not sequence:
            self.logical_empty_records += 1
            row = empty_anomaly("empty_sequence_record")
            row.update(
                {
                    "source_line": str(self._logical_header_line),
                    "source_byte_offset": str(self._logical_header_offset),
                    "physical_record_ordinal": str(self.physical_record_count),
                    "logical_record_ordinal": str(self.logical_record_count),
                    "reference_id": current_id,
                    "source_sequence_length": "0",
                    "source_sequence_sha256": sequence_sha,
                    "interpretation": "Logical FASTA record has no sequence characters",
                }
            )
            self.anomalies.append(row)
        canonical = canonical_record_bytes(self._logical_header, sequence)
        self.logical_total_bases += len(sequence)
        if self.logical_record_count <= self.indexed_record_count:
            self.logical_selected_bases += len(sequence)
            self.selected_digest.update(canonical)
        else:
            self.logical_tail_records += 1
            self.logical_tail_digest.update(canonical)
        if self._logical_origin is not None:
            origin = self._logical_origin
            row = empty_anomaly("embedded_header_candidate")
            row.update(
                {
                    "source_line": str(origin["source_line"]),
                    "source_byte_offset": str(origin["source_byte_offset"]),
                    "physical_record_ordinal": str(origin["physical_ordinal"]),
                    "logical_record_ordinal": str(self.logical_record_count),
                    "reference_id": current_id,
                    "related_logical_record_ordinal": str(origin["containing_logical_ordinal"]),
                    "related_reference_id": str(origin["containing_reference_id"]),
                    "source_sequence_length": str(len(sequence)),
                    "source_sequence_sha256": sequence_sha,
                    "interpretation": (
                        "Non-leading header delimiter is split for comparison only; "
                        "no source repair or release action is authorized"
                    ),
                }
            )
            self.anomalies.append(row)
            origin["recovered_logical_ordinal"] = self.logical_record_count
            origin["recovered_reference_id"] = current_id
        record = LogicalRecord(
            header=self._logical_header,
            sequence=sequence,
            logical_ordinal=self.logical_record_count,
            physical_ordinal=self.physical_record_count,
            header_line=self._logical_header_line,
            header_byte_offset=self._logical_header_offset,
            contains_embedded_boundary=embedded_boundary,
        )
        self._logical_header = ""
        self._logical_sequence_parts = []
        self._logical_origin = None
        return record

    def _finish_physical(self) -> None:
        if not self._physical_header:
            return
        sequence = "".join(self._physical_sequence_parts).upper()
        if self.physical_record_count <= self.indexed_record_count:
            self.physical_selected_sequence_characters += len(sequence)
        else:
            self.physical_tail_records += 1
            self.physical_tail_digest.update(
                canonical_record_bytes(self._physical_header, sequence)
            )
        for origin in self._physical_embedded:
            indexed = self.index_comparisons.get(self.physical_record_count)
            if indexed is None:
                continue
            if sequence == indexed.sequence and self._physical_header == indexed.title:
                continue
            row = empty_anomaly("line_oriented_index_sequence_mismatch")
            row.update(
                {
                    "source_line": str(origin["source_line"]),
                    "source_byte_offset": str(origin["source_byte_offset"]),
                    "physical_record_ordinal": str(self.physical_record_count),
                    "logical_record_ordinal": str(origin["containing_logical_ordinal"]),
                    "reference_id": str(origin["containing_reference_id"]),
                    "related_logical_record_ordinal": str(origin.get("recovered_logical_ordinal", "")),
                    "related_reference_id": str(origin.get("recovered_reference_id", "")),
                    "legacy_index_oid": str(indexed.oid),
                    "source_sequence_length": str(len(sequence)),
                    "comparison_sequence_length": str(indexed.length),
                    "source_sequence_sha256": sha256_bytes(sequence.encode("utf-8")),
                    "comparison_sequence_sha256": sha256_bytes(
                        indexed.sequence.encode("utf-8")
                    ),
                    "interpretation": (
                        "Line-start FASTA parsing merges an embedded header and sequence; "
                        "the analysis-split logical record matches the legacy index"
                    ),
                }
            )
            self.anomalies.append(row)
        self._physical_header = ""
        self._physical_sequence_parts = []
        self._physical_embedded = []

    def records(self) -> Iterator[LogicalRecord]:
        byte_offset = 0
        with self.path.open("rb") as handle:
            for line_number, raw in enumerate(handle, start=1):
                self.line_count = line_number
                content = raw.rstrip(b"\r\n")
                if content.startswith(b">"):
                    if self._logical_header:
                        yield self._finish_logical()
                    self._finish_physical()
                    try:
                        header = content[1:].decode("utf-8")
                    except UnicodeDecodeError:
                        die(f"non-UTF-8 FASTA header at line {line_number}")
                    self._start_physical(header, line_number, byte_offset)
                    self._start_logical(header, line_number, byte_offset)
                elif content:
                    if not self._physical_header or not self._logical_header:
                        die(f"sequence before first FASTA header at line {line_number}")
                    try:
                        text = content.decode("ascii")
                    except UnicodeDecodeError:
                        die(f"non-ASCII FASTA sequence at line {line_number}")
                    if text != text.strip():
                        die(
                            "leading or trailing FASTA sequence whitespace at line "
                            f"{line_number}"
                        )
                    self._physical_sequence_parts.append(text)
                    markers = [index for index, char in enumerate(text) if char == ">"]
                    if not markers:
                        self._logical_sequence_parts.append(text)
                    else:
                        if len(markers) != 1:
                            die(f"multiple non-leading header delimiters at line {line_number}")
                        marker = markers[0]
                        self._logical_sequence_parts.append(text[:marker])
                        containing_id = reference_id(self._logical_header)
                        containing_ordinal = self.logical_record_count + 1
                        origin: dict[str, object] = {
                            "source_line": line_number,
                            "source_byte_offset": byte_offset + marker,
                            "physical_ordinal": self.physical_record_count,
                            "containing_logical_ordinal": containing_ordinal,
                            "containing_reference_id": containing_id,
                        }
                        self._physical_embedded.append(origin)
                        self.embedded_header_candidates += 1
                        yield self._finish_logical(embedded_boundary=True)
                        recovered_header = text[marker + 1 :]
                        if not recovered_header:
                            die(f"empty embedded FASTA header at line {line_number}")
                        self._start_logical(
                            recovered_header,
                            line_number,
                            byte_offset + marker,
                            origin,
                        )
                byte_offset += len(raw)
        if self._logical_header:
            yield self._finish_logical()
        self._finish_physical()


def parse_blast_line(line: str, label: str) -> BlastRecord:
    fields = line.rstrip("\r\n").split("\t", 3)
    if len(fields) != 4:
        die(f"malformed blastdbcmd row in {label}")
    oid_text, title, length_text, sequence = fields
    if not oid_text.isdigit() or not length_text.isdigit():
        die(f"invalid blastdbcmd ordinal or length in {label}")
    normalized = sequence.upper()
    length = int(length_text)
    if len(normalized) != length:
        die(f"blastdbcmd length mismatch for OID {oid_text}")
    return BlastRecord(int(oid_text), title, length, normalized)


def run_blastdbcmd_records(executable: str, database: Path) -> Iterator[BlastRecord]:
    command = [
        executable,
        "-db",
        str(database),
        "-dbtype",
        "nucl",
        "-entry",
        "all",
        "-outfmt",
        "%o\t%t\t%l\t%s",
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
            if line.strip():
                yield parse_blast_line(line, str(database))
        return_code = process.wait()
        if return_code != 0:
            stderr_file.seek(0)
            stderr = stderr_file.read()
            die(f"blastdbcmd export failed ({return_code}): {stderr.strip()}")
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait()
        stderr_file.close()


def blastdbcmd_version(executable: str) -> str:
    environment = dict(os.environ)
    environment.update({"LC_ALL": "C", "LANG": "C"})
    try:
        completed = subprocess.run(
            [executable, "-version"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=environment,
        )
    except OSError as error:
        die(f"cannot run blastdbcmd: {error}")
    if completed.returncode != 0 or not completed.stdout.strip():
        die(f"blastdbcmd version probe failed: {completed.stderr.strip()}")
    return completed.stdout.splitlines()[0]


def compare_source_to_index(
    source: Path,
    indexed_records: Iterable[BlastRecord],
    expected_index_records: int,
    expected_index_bases: int,
    dispositions: dict[str, dict[str, str]],
) -> tuple[SourceScanner, str, int, int, Counter[str]]:
    scanner = SourceScanner(source, expected_index_records)
    source_records = scanner.records()
    index_digest = hashlib.sha256()
    index_count = 0
    index_bases = 0
    disposition_actions: Counter[str] = Counter()
    seen_dispositions: set[str] = set()
    for indexed in indexed_records:
        if indexed.oid != index_count:
            die(f"legacy index OIDs are not contiguous at {indexed.oid}")
        if index_count >= expected_index_records:
            die("legacy index export exceeds its metadata record count")
        try:
            observed = next(source_records)
        except StopIteration:
            die("analysis-split source is shorter than the legacy index")
        expected_ordinal = indexed.oid + 1
        if observed.logical_ordinal != expected_ordinal:
            die(f"source/index ordinal mismatch at OID {indexed.oid}")
        if observed.header != indexed.title:
            die(f"source/index title mismatch at OID {indexed.oid}")
        if header_taxid(observed.header) != header_taxid(indexed.title):
            die(f"source/index header taxid mismatch at OID {indexed.oid}")
        if observed.sequence != indexed.sequence:
            die(f"source/index sequence mismatch after analysis split at OID {indexed.oid}")
        indexed_id = reference_id(indexed.title)
        disposition = dispositions.get(indexed_id)
        if disposition is not None:
            if indexed_id in seen_dispositions:
                die(f"disposition reference occurs more than once in legacy index: {indexed_id}")
            indexed_sha = sha256_bytes(indexed.sequence.encode("utf-8"))
            if indexed_sha != disposition["reference_sequence_sha256"]:
                die(f"disposition/index sequence mismatch: {indexed_id}")
            if header_taxid(indexed.title) != disposition["stored_taxid"]:
                die(f"disposition/index header taxid mismatch: {indexed_id}")
            seen_dispositions.add(indexed_id)
            disposition_actions[disposition["release_action"]] += 1
        scanner.note_index_record(observed, indexed)
        index_digest.update(canonical_record_bytes(indexed.title, indexed.sequence))
        index_count += 1
        index_bases += indexed.length
    if index_count != expected_index_records:
        die(
            "legacy index export record count mismatch "
            f"({index_count} != {expected_index_records})"
        )
    if index_bases != expected_index_bases:
        die(
            "legacy index export base count mismatch "
            f"({index_bases} != {expected_index_bases})"
        )
    for _ in source_records:
        pass
    source_digest = scanner.selected_digest.hexdigest()
    index_digest_text = index_digest.hexdigest()
    if source_digest != index_digest_text:
        die("analysis-split source prefix digest does not match the legacy index")
    if scanner.logical_selected_bases != expected_index_bases:
        die("analysis-split source prefix base count does not match the legacy index")
    missing_dispositions = sorted(set(dispositions) - seen_dispositions)
    if missing_dispositions:
        die(f"disposition references are absent from the legacy index: {missing_dispositions}")
    return scanner, index_digest_text, index_count, index_bases, disposition_actions


def read_reference_manifest(path: Path, root: Path) -> dict[Path, dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != ["artifact", "sha256", "role"]:
            die(f"unexpected reference manifest schema: {path}")
        rows = list(reader)
    if not rows:
        die(f"empty reference manifest: {path}")
    result: dict[Path, dict[str, str]] = {}
    for row in rows:
        artifact = Path(row["artifact"])
        if artifact.is_absolute() or ".." in artifact.parts:
            die(f"unsafe artifact path in reference manifest: {artifact}")
        if not SHA256_RE.fullmatch(row["sha256"]):
            die(f"invalid reference manifest checksum: {artifact}")
        resolved = (root / artifact).resolve()
        if resolved in result:
            die(f"duplicate resolved artifact in reference manifest: {artifact}")
        result[resolved] = row
    return result


def verify_index_components(
    database: Path,
    reference_manifest: Path,
    reference_root: Path,
) -> tuple[dict[str, object], list[tuple[str, Path, str]]]:
    njs = Path(str(database) + ".njs")
    if not njs.is_file():
        die(f"BLAST metadata file is missing: {njs}")
    try:
        metadata = json.loads(njs.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        die(f"cannot parse BLAST metadata {njs}: {error}")
    files = metadata.get("files")
    if not isinstance(files, list) or not files or not all(isinstance(item, str) for item in files):
        die(f"BLAST metadata has no component file list: {njs}")
    manifest = read_reference_manifest(reference_manifest, reference_root)
    paths = [njs] + [njs.parent / item for item in files]
    verified: list[tuple[str, Path, str]] = []
    seen_components: set[Path] = set()
    for component in paths:
        if not component.is_file():
            die(f"BLAST component is missing: {component}")
        resolved = component.resolve()
        if resolved in seen_components:
            die(f"BLAST metadata repeats a component path: {component}")
        seen_components.add(resolved)
        row = manifest.get(resolved)
        if row is None:
            die(f"BLAST component is not pinned by the reference manifest: {component}")
        digest = sha256_file(component)
        if digest != row["sha256"]:
            die(f"BLAST component checksum mismatch: {component}")
        artifact_key = Path(row["artifact"]).as_posix()
        verified.append((artifact_key, component, digest))
    return metadata, verified


def read_dispositions(
    provenance_path: Path,
    manifest_path: Path,
    source_sha256: str,
) -> dict[str, dict[str, str]]:
    with provenance_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    if not rows or not {"field", "artifact", "value"}.issubset(rows[0]):
        die(f"unexpected disposition provenance schema: {provenance_path}")
    schemas = [row["value"] for row in rows if row["field"] == "schema"]
    if schemas != ["taxonomy_reference_disposition_v1"]:
        die(f"unexpected disposition provenance version: {provenance_path}")
    source_hashes = [
        row["value"]
        for row in rows
        if row["field"] == "sha256" and row["artifact"] == "reference_source_fasta"
    ]
    if source_hashes != [source_sha256]:
        die("disposition provenance does not bind the audited source FASTA")
    manifest_hashes = [
        row["value"]
        for row in rows
        if row["field"] == "sha256" and row["artifact"] == "disposition_manifest"
    ]
    if manifest_hashes != [sha256_file(manifest_path)]:
        die("disposition provenance does not bind the disposition manifest")
    with manifest_path.open(newline="", encoding="utf-8") as handle:
        disposition_rows = list(csv.DictReader(handle, delimiter="\t"))
    required = {
        "reference_id",
        "reference_sequence_sha256",
        "stored_taxid",
        "release_action",
    }
    if not disposition_rows or not required.issubset(disposition_rows[0]):
        die(f"unexpected disposition manifest schema: {manifest_path}")
    result: dict[str, dict[str, str]] = {}
    for row in disposition_rows:
        current_id = row["reference_id"]
        if not current_id or current_id in result:
            die(f"invalid or duplicate disposition reference ID: {current_id}")
        if not SHA256_RE.fullmatch(row["reference_sequence_sha256"]):
            die(f"invalid disposition sequence checksum: {current_id}")
        if not re.fullmatch(r"-?[0-9]+", row["stored_taxid"]):
            die(f"invalid disposition stored taxid: {current_id}")
        if row["release_action"] not in {"quarantine", "retain"}:
            die(f"invalid disposition release action: {current_id}")
        result[current_id] = row
    return result


def anomaly_text(anomalies: list[dict[str, str]]) -> str:
    ordered = sorted(
        anomalies,
        key=lambda row: (
            int(row["source_byte_offset"] or "0"),
            row["anomaly_class"],
            row["reference_id"],
        ),
    )
    for index, row in enumerate(ordered, start=1):
        row["anomaly_id"] = f"anomaly_{index:03d}"
    lines = ["\t".join(ANOMALY_FIELDS)]
    lines.extend("\t".join(row[field] for field in ANOMALY_FIELDS) for row in ordered)
    return "\n".join(lines) + "\n"


def provenance_text(
    args: argparse.Namespace,
    scanner: SourceScanner,
    anomaly_output_text: str,
    source_sha256: str,
    reference_manifest_sha256: str,
    disposition_provenance_sha256: str,
    disposition_manifest_sha256: str,
    blast_version: str,
    index_digest: str,
    index_count: int,
    index_bases: int,
    components: list[tuple[str, Path, str]],
    disposition_actions: Counter[str],
) -> str:
    anomaly_counts = Counter(row["anomaly_class"] for row in scanner.anomalies)
    rows = [
        ("schema", "", "taxonomy_source_index_integrity_v1"),
        ("scope", "database", args.scope),
        ("scope", "source_mutation", "none"),
        ("scope", "release_policy", "none"),
        ("scope", "artifact_set_commit_marker", "provenance_output_written_last"),
        (
            "normalization",
            "embedded_header_handling",
            "split_for_comparison_only",
        ),
        (
            "normalization",
            "canonical_record",
            "greater_than_title_lf_uppercase_sequence_lf",
        ),
        ("coordinate", "source_line", "one_based"),
        ("coordinate", "source_byte_offset", "zero_based"),
        ("tool", "blastdbcmd", blast_version),
        ("count", "source_lines", str(scanner.line_count)),
        (
            "count",
            "source_line_start_records",
            str(scanner.physical_record_count),
        ),
        (
            "count",
            "source_line_start_unique_reference_ids",
            str(len(scanner.physical_ids)),
        ),
        (
            "count",
            "analysis_split_logical_records",
            str(scanner.logical_record_count),
        ),
        (
            "count",
            "analysis_split_unique_reference_ids",
            str(len(scanner.logical_ids)),
        ),
        (
            "count",
            "analysis_split_total_bases",
            str(scanner.logical_total_bases),
        ),
        (
            "count",
            "analysis_split_empty_records",
            str(scanner.logical_empty_records),
        ),
        (
            "count",
            "embedded_header_candidates",
            str(scanner.embedded_header_candidates),
        ),
        ("count", "legacy_index_records", str(index_count)),
        ("count", "legacy_index_bases", str(index_bases)),
        (
            "count",
            "line_start_prefix_sequence_characters",
            str(scanner.physical_selected_sequence_characters),
        ),
        (
            "count",
            "analysis_split_selected_bases",
            str(scanner.logical_selected_bases),
        ),
        (
            "count",
            "line_start_unindexed_records",
            str(scanner.physical_tail_records),
        ),
        (
            "count",
            "analysis_split_unindexed_records",
            str(scanner.logical_tail_records),
        ),
        ("count", "anomaly_rows", str(len(scanner.anomalies))),
        (
            "count",
            "disposition_records_in_legacy_index",
            str(sum(disposition_actions.values())),
        ),
    ]
    for anomaly_class in sorted(anomaly_counts):
        rows.append(("count", f"anomaly:{anomaly_class}", str(anomaly_counts[anomaly_class])))
    for action in sorted(disposition_actions):
        rows.append(("count", f"disposition:{action}", str(disposition_actions[action])))
    rows.extend(
        [
            ("sha256", "auditor_script", sha256_file(Path(__file__))),
            ("sha256", "source_fasta", source_sha256),
            ("sha256", "legacy_reference_manifest", reference_manifest_sha256),
            (
                "sha256",
                "disposition_provenance",
                disposition_provenance_sha256,
            ),
            ("sha256", "disposition_manifest", disposition_manifest_sha256),
            (
                "sha256",
                "anomaly_table",
                sha256_bytes(anomaly_output_text.encode("utf-8")),
            ),
            (
                "sha256",
                "analysis_split_selected_canonical_stream",
                scanner.selected_digest.hexdigest(),
            ),
            ("sha256", "legacy_index_canonical_stream", index_digest),
            (
                "sha256",
                "line_start_unindexed_canonical_stream",
                scanner.physical_tail_digest.hexdigest(),
            ),
            (
                "sha256",
                "analysis_split_unindexed_canonical_stream",
                scanner.logical_tail_digest.hexdigest(),
            ),
        ]
    )
    for artifact_key, _, digest in sorted(components):
        rows.append(("sha256", f"legacy_blast_component:{artifact_key}", digest))
    return "field\tartifact\tvalue\n" + "".join(
        f"{field}\t{artifact}\t{value}\n" for field, artifact, value in rows
    )


def write_output_set(outputs: list[tuple[Path, str]]) -> None:
    for path, _ in outputs:
        if path.exists() and not path.is_file():
            die(f"output target is not a regular file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
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
    finally:
        for temporary, _ in staged:
            if os.path.exists(temporary):
                os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-fasta", type=Path, required=True)
    parser.add_argument("--blast-database", type=Path, required=True)
    parser.add_argument("--blastdbcmd", default="blastdbcmd")
    parser.add_argument("--reference-manifest", type=Path, required=True)
    parser.add_argument("--reference-root", type=Path, required=True)
    parser.add_argument("--disposition-provenance", type=Path, required=True)
    parser.add_argument("--disposition-manifest", type=Path, required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--anomaly-output", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    args = parser.parse_args()

    required = [
        args.source_fasta,
        args.reference_manifest,
        args.disposition_provenance,
        args.disposition_manifest,
    ]
    for path in required:
        if not path.is_file():
            die(f"required input is missing: {path}")
    if not args.scope.strip():
        die("scope must not be empty")
    output_paths = [args.anomaly_output.resolve(), args.provenance_output.resolve()]
    if len(set(output_paths)) != 2:
        die("output paths must be distinct")
    input_paths = {path.resolve() for path in required}
    if input_paths.intersection(output_paths):
        die("an output path overlaps a required input")
    protected_executables = {Path(__file__).resolve()}
    executable = shutil.which(args.blastdbcmd)
    if executable:
        protected_executables.add(Path(executable).resolve())
    if protected_executables.intersection(output_paths):
        die("an output path overlaps an audit executable")

    source_sha256 = sha256_file(args.source_fasta)
    dispositions = read_dispositions(
        args.disposition_provenance,
        args.disposition_manifest,
        source_sha256,
    )
    metadata, components = verify_index_components(
        args.blast_database,
        args.reference_manifest,
        args.reference_root,
    )
    component_paths = {path.resolve() for _, path, _ in components}
    if component_paths.intersection(output_paths):
        die("an output path overlaps a pinned BLAST component")
    index_count = metadata.get("number-of-sequences")
    index_bases = metadata.get("number-of-letters")
    if not isinstance(index_count, int) or index_count <= 0:
        die("BLAST metadata has an invalid sequence count")
    if not isinstance(index_bases, int) or index_bases < 0:
        die("BLAST metadata has an invalid base count")
    version = blastdbcmd_version(args.blastdbcmd)
    (
        scanner,
        index_digest,
        observed_count,
        observed_bases,
        disposition_actions,
    ) = compare_source_to_index(
        args.source_fasta,
        run_blastdbcmd_records(args.blastdbcmd, args.blast_database),
        index_count,
        index_bases,
        dispositions,
    )
    anomaly_output = anomaly_text(scanner.anomalies)
    provenance_output = provenance_text(
        args,
        scanner,
        anomaly_output,
        source_sha256,
        sha256_file(args.reference_manifest),
        sha256_file(args.disposition_provenance),
        sha256_file(args.disposition_manifest),
        version,
        index_digest,
        observed_count,
        observed_bases,
        components,
        disposition_actions,
    )
    write_output_set(
        [
            (args.anomaly_output, anomaly_output),
            (args.provenance_output, provenance_output),
        ]
    )
    print(
        "OK: audited "
        f"{observed_count} indexed records against {scanner.logical_record_count} "
        f"analysis-split source records; wrote {len(scanner.anomalies)} anomalies"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
