#!/usr/bin/env python3
"""Discover reference-curation candidates from independently supported queries.

This is an offline database-release tool.  It is not imported or invoked by the
RTBioScan runtime pipeline, and it does not modify a source FASTA or BLAST index.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


QUERY_FIELDS = [
    "query_id",
    "source_accession",
    "source_sequence_sha256",
    "evidence_status",
    "notes",
]

CANDIDATE_FIELDS = [
    "query_id",
    "source_accession",
    "query_sequence_sha256",
    "reference_id",
    "stored_taxid",
    "header_kingdom",
    "header_lineage",
    "reference_sequence_sha256",
    "query_length",
    "reference_length",
    "pident",
    "alignment_length",
    "shorter_sequence_coverage",
    "evalue",
    "bitscore",
    "discovery_tier",
    "review_status",
]

BLAST_SUFFIXES = ["ndb", "nhr", "nin", "njs", "not", "nsq", "ntf", "nto"]


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


def read_query_manifest(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        if reader.fieldnames != QUERY_FIELDS:
            die(f"unexpected query-manifest schema in {path}")
        rows = list(reader)
    if not rows:
        die(f"query manifest is empty: {path}")
    for field in ("query_id", "source_accession"):
        values = [row[field] for row in rows]
        if any(not value for value in values) or len(values) != len(set(values)):
            die(f"query manifest requires unique non-empty {field} values")
    for row in rows:
        if not re.fullmatch(r"[0-9a-f]{64}", row["source_sequence_sha256"]):
            die(f"invalid query SHA-256 for {row['query_id']}")
        if row["evidence_status"] != "confirmed_bacterial":
            die(f"query {row['query_id']} is not independently confirmed bacterial")
    return rows


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


def extract_queries(
    source_fasta: Path, manifest: list[dict[str, str]]
) -> dict[str, str]:
    by_accession = {row["source_accession"]: row for row in manifest}
    accession_patterns = {
        accession: re.compile(
            rf"(?<![A-Za-z0-9.]){re.escape(accession)}(?![A-Za-z0-9.])"
        )
        for accession in by_accession
    }
    observed: dict[str, str] = {}
    for header, sequence in iter_fasta(source_fasta):
        matches = [
            accession
            for accession, pattern in accession_patterns.items()
            if pattern.search(header)
        ]
        if not matches:
            continue
        if len(matches) != 1:
            die(f"query header matches multiple accessions: {header}")
        accession = matches[0]
        if accession in observed:
            die(f"source accession occurs more than once: {accession}")
        if not sequence or re.search(r"[^ACGTN]", sequence):
            die(f"invalid nucleotide sequence for source accession {accession}")
        observed[accession] = sequence
    missing = sorted(set(by_accession) - set(observed))
    if missing:
        die(f"query accessions missing from source FASTA: {missing}")
    result: dict[str, str] = {}
    for row in manifest:
        sequence = observed[row["source_accession"]]
        digest = sha256_bytes(sequence.encode("ascii"))
        if digest != row["source_sequence_sha256"]:
            die(
                f"query sequence checksum changed for {row['query_id']}: "
                f"expected {row['source_sequence_sha256']}, observed {digest}"
            )
        result[row["query_id"]] = sequence
    return result


def write_query_fasta(path: Path, manifest: list[dict[str, str]], sequences: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in manifest:
            query_id = row["query_id"]
            handle.write(f">{query_id}|source={row['source_accession']}\n")
            handle.write(f"{sequences[query_id]}\n")


def run_checked(command: list[str]) -> str:
    completed = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        die(f"command failed ({' '.join(command)}): {detail}")
    return completed.stdout


def run_blast(
    query_fasta: Path,
    database: Path,
    blastn: str,
    min_pident: float,
    max_target_seqs: int,
) -> str:
    return run_checked(
        [
            blastn,
            "-task",
            "blastn",
            "-dust",
            "no",
            "-query",
            str(query_fasta),
            "-db",
            str(database),
            "-num_threads",
            "1",
            "-outfmt",
            "6 qseqid qlen sseqid slen pident length qstart qend sstart send evalue bitscore sseq",
            "-perc_identity",
            f"{min_pident:g}",
            "-evalue",
            "1e-20",
            "-max_hsps",
            "1",
            "-max_target_seqs",
            str(max_target_seqs),
            "-word_size",
            "11",
        ]
    )


def parse_blast(
    output: str,
    query_ids: set[str],
    min_shorter_coverage: float,
) -> dict[tuple[str, str], dict[str, str]]:
    hits: dict[tuple[str, str], dict[str, str]] = {}
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 13:
            die(f"unexpected BLAST row with {len(fields)} fields")
        query_id = fields[0].split("|", 1)[0]
        if query_id not in query_ids:
            die(f"unexpected BLAST query identifier: {query_id}")
        reference_id = fields[2].split("|", 1)[0]
        query_length = int(fields[1])
        reference_length = int(fields[3])
        alignment_length = int(fields[5])
        shorter_length = min(query_length, reference_length)
        coverage = min(100.0, 100.0 * alignment_length / shorter_length)
        if coverage + 1e-9 < min_shorter_coverage:
            continue
        taxid_match = re.search(r"\|kraken:taxid\|(-?\d+)", fields[2])
        if not taxid_match:
            die(f"candidate reference lacks a numeric taxid: {fields[2]}")
        key = (query_id, reference_id)
        candidate = {
            "query_id": query_id,
            "reference_id": reference_id,
            "stored_taxid": taxid_match.group(1),
            "query_length": str(query_length),
            "reference_length": str(reference_length),
            "pident": f"{float(fields[4]):.3f}",
            "alignment_length": str(alignment_length),
            "shorter_sequence_coverage": f"{coverage:.3f}",
            "evalue": fields[10],
            "bitscore": fields[11],
            "subject_start": fields[8],
            "subject_end": fields[9],
            "subject_aligned_sequence": fields[12].upper(),
        }
        if key not in hits or float(candidate["bitscore"]) > float(hits[key]["bitscore"]):
            hits[key] = candidate
    return hits


def read_reference_records(
    source_fasta: Path, wanted: set[str]
) -> dict[str, tuple[str, str]]:
    records: dict[str, tuple[str, str]] = {}
    for header, sequence in iter_fasta(source_fasta):
        reference_id = header.split()[0].split("|", 1)[0]
        if reference_id in wanted:
            if reference_id in records:
                die(f"candidate reference identifier is duplicated: {reference_id}")
            records[reference_id] = (header, sequence)
    missing = sorted(wanted - set(records))
    if missing:
        die(f"BLAST candidates missing from source FASTA: {missing}")
    return records


def header_metadata(header: str) -> tuple[str, str]:
    kingdom_match = re.search(r"(?:^|[ ;])k__([^; ]*)", header)
    kingdom = kingdom_match.group(1) if kingdom_match else ""
    lineage = header.split(" COI ", 1)[1] if " COI " in header else ""
    return kingdom, lineage


def reverse_complement(sequence: str) -> str:
    complements = str.maketrans(
        "ACGTRYSWKMBDHVN",
        "TGCAYRSWMKVHDBN",
    )
    return sequence.translate(complements)[::-1]


def verify_hit_source_record(hit: dict[str, str], header: str, sequence: str) -> None:
    taxid_match = re.search(r"\|kraken:taxid\|(-?\d+)", header)
    source_taxid = taxid_match.group(1) if taxid_match else ""
    if source_taxid != hit["stored_taxid"]:
        die(
            f"index/source taxid mismatch for {hit['reference_id']}: "
            f"index {hit['stored_taxid']}, source {source_taxid or 'missing'}"
        )
    subject_start = int(hit["subject_start"])
    subject_end = int(hit["subject_end"])
    low, high = sorted((subject_start, subject_end))
    if low < 1 or high > len(sequence):
        die(f"BLAST subject coordinates exceed source record for {hit['reference_id']}")
    source_segment = sequence[low - 1 : high]
    if subject_start > subject_end:
        source_segment = reverse_complement(source_segment)
    aligned_index_segment = hit["subject_aligned_sequence"].replace("-", "")
    if source_segment != aligned_index_segment:
        die(f"index/source aligned sequence mismatch for {hit['reference_id']}")


def build_candidates(
    manifest: list[dict[str, str]],
    query_sequences: dict[str, str],
    hits: dict[tuple[str, str], dict[str, str]],
    reference_records: dict[str, tuple[str, str]],
    priority_pident: float,
    priority_shorter_coverage: float,
) -> list[dict[str, str]]:
    manifest_by_id = {row["query_id"]: row for row in manifest}
    order = {row["query_id"]: index for index, row in enumerate(manifest)}
    candidates: list[dict[str, str]] = []
    for hit in hits.values():
        header, reference_sequence = reference_records[hit["reference_id"]]
        if len(reference_sequence) != int(hit["reference_length"]):
            die(f"reference length mismatch for {hit['reference_id']}")
        verify_hit_source_record(hit, header, reference_sequence)
        kingdom, lineage = header_metadata(header)
        is_priority = (
            float(hit["pident"]) >= priority_pident
            and float(hit["shorter_sequence_coverage"])
            >= priority_shorter_coverage
        )
        query_row = manifest_by_id[hit["query_id"]]
        candidates.append(
            {
                "query_id": hit["query_id"],
                "source_accession": query_row["source_accession"],
                "query_sequence_sha256": sha256_bytes(
                    query_sequences[hit["query_id"]].encode("ascii")
                ),
                "reference_id": hit["reference_id"],
                "stored_taxid": hit["stored_taxid"],
                "header_kingdom": kingdom,
                "header_lineage": lineage,
                "reference_sequence_sha256": sha256_bytes(
                    reference_sequence.encode("ascii")
                ),
                "query_length": hit["query_length"],
                "reference_length": hit["reference_length"],
                "pident": hit["pident"],
                "alignment_length": hit["alignment_length"],
                "shorter_sequence_coverage": hit["shorter_sequence_coverage"],
                "evalue": hit["evalue"],
                "bitscore": hit["bitscore"],
                "discovery_tier": "priority" if is_priority else "review",
                "review_status": "pending_adjudication",
            }
        )
    candidates.sort(
        key=lambda row: (
            order[row["query_id"]],
            0 if row["discovery_tier"] == "priority" else 1,
            -float(row["bitscore"]),
            row["reference_id"],
        )
    )
    return candidates


def tsv_text(fields: list[str], rows: list[dict[str, str]]) -> str:
    lines = ["\t".join(fields)]
    lines.extend("\t".join(row[field] for field in fields) for row in rows)
    return "\n".join(lines) + "\n"


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provenance_text(
    args: argparse.Namespace,
    candidates: list[dict[str, str]],
    candidate_text: str,
    blast_version: str,
) -> str:
    rows = [
        ("schema", "", "chain_a_similarity_audit_v1"),
        ("tool", "blastn", blast_version.splitlines()[0]),
        ("parameter", "task", "blastn"),
        ("parameter", "dust", "no"),
        ("parameter", "evalue", "1e-20"),
        ("parameter", "word_size", "11"),
        ("parameter", "max_hsps", "1"),
        ("parameter", "max_target_seqs", str(args.max_target_seqs)),
        ("parameter", "min_pident", f"{args.min_pident:g}"),
        (
            "parameter",
            "min_shorter_sequence_coverage",
            f"{args.min_shorter_coverage:g}",
        ),
        ("parameter", "priority_pident", f"{args.priority_pident:g}"),
        (
            "parameter",
            "priority_shorter_sequence_coverage",
            f"{args.priority_shorter_coverage:g}",
        ),
        ("count", "all_candidates", str(len(candidates))),
        (
            "count",
            "priority_candidates",
            str(sum(row["discovery_tier"] == "priority" for row in candidates)),
        ),
        (
            "count",
            "review_candidates",
            str(sum(row["discovery_tier"] == "review" for row in candidates)),
        ),
        ("sha256", str(args.query_manifest), sha256_file(args.query_manifest)),
        ("sha256", str(args.query_source_fasta), sha256_file(args.query_source_fasta)),
        (
            "sha256",
            str(args.reference_source_fasta),
            sha256_file(args.reference_source_fasta),
        ),
    ]
    for suffix in BLAST_SUFFIXES:
        component = Path(f"{args.database}.{suffix}")
        if not component.is_file():
            die(f"required BLAST component is missing: {component}")
        rows.append(("sha256", str(component), sha256_file(component)))
    rows.append(("sha256", str(args.output), sha256_bytes(candidate_text.encode("utf-8"))))
    return "field\tartifact\tvalue\n" + "".join(
        f"{field}\t{artifact}\t{value}\n" for field, artifact, value in rows
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query-manifest", type=Path, required=True)
    parser.add_argument("--query-source-fasta", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--reference-source-fasta", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--blastn", default="blastn")
    parser.add_argument("--min-pident", type=float, default=85.0)
    parser.add_argument("--min-shorter-coverage", type=float, default=80.0)
    parser.add_argument("--priority-pident", type=float, default=97.0)
    parser.add_argument("--priority-shorter-coverage", type=float, default=90.0)
    parser.add_argument("--max-target-seqs", type=int, default=1_000_000)
    args = parser.parse_args()

    if not 0 < args.min_pident <= args.priority_pident <= 100:
        die("identity thresholds must satisfy 0 < minimum <= priority <= 100")
    if not 0 < args.min_shorter_coverage <= args.priority_shorter_coverage <= 100:
        die("coverage thresholds must satisfy 0 < minimum <= priority <= 100")
    if args.max_target_seqs < 1:
        die("--max-target-seqs must be positive")
    for path in (
        args.query_manifest,
        args.query_source_fasta,
        args.reference_source_fasta,
    ):
        if not path.is_file():
            die(f"required input is missing: {path}")
    if shutil.which(args.blastn) is None:
        die(f"BLAST executable is not available: {args.blastn}")

    manifest = read_query_manifest(args.query_manifest)
    query_sequences = extract_queries(args.query_source_fasta, manifest)
    with tempfile.TemporaryDirectory(prefix="rtbioscan-reference-audit.") as temp:
        query_fasta = Path(temp) / "queries.fa"
        write_query_fasta(query_fasta, manifest, query_sequences)
        blast_output = run_blast(
            query_fasta,
            args.database,
            args.blastn,
            args.min_pident,
            args.max_target_seqs,
        )
    hits = parse_blast(
        blast_output,
        {row["query_id"] for row in manifest},
        args.min_shorter_coverage,
    )
    records = read_reference_records(
        args.reference_source_fasta,
        {reference_id for _, reference_id in hits},
    )
    candidates = build_candidates(
        manifest,
        query_sequences,
        hits,
        records,
        args.priority_pident,
        args.priority_shorter_coverage,
    )
    candidate_text = tsv_text(CANDIDATE_FIELDS, candidates)
    provenance = provenance_text(
        args,
        candidates,
        candidate_text,
        run_checked([args.blastn, "-version"]),
    )
    write_atomic(args.output, candidate_text)
    write_atomic(args.provenance_output, provenance)
    print(
        "OK: wrote "
        f"{len(candidates)} candidates "
        f"({sum(row['discovery_tier'] == 'priority' for row in candidates)} priority, "
        f"{sum(row['discovery_tier'] == 'review' for row in candidates)} review)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
