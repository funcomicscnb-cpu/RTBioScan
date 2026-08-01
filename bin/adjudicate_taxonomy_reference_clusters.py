#!/usr/bin/env python3
"""Adjudicate reference candidates from local cross-family sequence evidence.

This is an offline database-curation tool. It compares review-tier references
with one another and with references declared confirmed by a separate manifest.
It never calls a remote service and does not modify a reference FASTA, BLAST
index, or pipeline behavior.
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
from collections import defaultdict
from pathlib import Path


OUTPUT_FIELDS = [
    "reference_id",
    "reference_sequence_sha256",
    "stored_taxid",
    "header_class",
    "header_order",
    "header_family",
    "bacterial_control_accession",
    "bacterial_pident",
    "bacterial_alignment_length",
    "bacterial_shorter_sequence_coverage",
    "cross_family_reference_id",
    "cross_family_reference_taxid",
    "cross_family_reference_class",
    "cross_family_reference_order",
    "cross_family_reference_family",
    "cross_family_reference_role",
    "cross_family_pident",
    "cross_family_alignment_length",
    "cross_family_shorter_sequence_coverage",
    "component_id",
    "component_size",
    "component_family_count",
    "disposition",
    "notes",
]


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


def read_audit_rows(
    path: Path,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    required = {
        "query_id",
        "source_accession",
        "reference_id",
        "stored_taxid",
        "header_lineage",
        "reference_sequence_sha256",
        "pident",
        "alignment_length",
        "shorter_sequence_coverage",
        "discovery_tier",
        "review_status",
    }
    if not rows or not required.issubset(rows[0]):
        die(f"unexpected or empty candidate audit: {path}")
    ids = [row["reference_id"] for row in rows]
    if len(ids) != len(set(ids)):
        die("candidate audit reference IDs are not unique")
    for row in rows:
        if not re.fullmatch(r"[0-9a-f]{64}", row["reference_sequence_sha256"]):
            die(f"invalid reference checksum for {row['reference_id']}")

    review_rows = [row for row in rows if row["discovery_tier"] == "review"]
    if not review_rows:
        die(f"candidate audit has no review-tier rows: {path}")
    for row in review_rows:
        if row["review_status"] != "pending_adjudication":
            die(f"review row is not pending: {row['reference_id']}")
        if float(row["pident"]) < 85 or float(row["shorter_sequence_coverage"]) < 80:
            die(f"review row falls outside the declared discovery protocol: {row['reference_id']}")
    return rows, review_rows


def read_confirmed_anchor_ids(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    required = {"reference_id", "evidence_status"}
    if not rows or not required.issubset(rows[0]):
        die(f"unexpected or empty confirmed-anchor manifest: {path}")
    anchor_ids = [row["reference_id"] for row in rows]
    if any(not reference_id for reference_id in anchor_ids):
        die("confirmed-anchor manifest contains an empty reference ID")
    if len(anchor_ids) != len(set(anchor_ids)):
        die("confirmed-anchor reference IDs are not unique")
    nonconfirmed = sorted(
        row["reference_id"] for row in rows if row["evidence_status"] != "confirmed"
    )
    if nonconfirmed:
        die(f"confirmed-anchor manifest contains non-confirmed rows: {nonconfirmed}")
    return anchor_ids


def extract_reference_sequences(
    source_fasta: Path, rows: list[dict[str, str]]
) -> dict[str, str]:
    wanted = {row["reference_id"]: row for row in rows}
    sequences: dict[str, str] = {}
    for header, sequence in iter_fasta(source_fasta):
        reference_id = header.split()[0].split("|", 1)[0]
        if reference_id not in wanted:
            continue
        if reference_id in sequences:
            die(f"duplicate source reference ID: {reference_id}")
        taxid_match = re.search(r"\|kraken:taxid\|(-?\d+)", header)
        source_taxid = taxid_match.group(1) if taxid_match else ""
        if source_taxid != wanted[reference_id]["stored_taxid"]:
            die(f"source/audit taxid mismatch for {reference_id}")
        digest = sha256_bytes(sequence.encode("ascii"))
        if digest != wanted[reference_id]["reference_sequence_sha256"]:
            die(f"source/audit sequence mismatch for {reference_id}")
        sequences[reference_id] = sequence
    missing = sorted(set(wanted) - set(sequences))
    if missing:
        die(f"comparison references missing from source FASTA: {missing}")
    return sequences


def write_query_fasta(path: Path, rows: list[dict[str, str]], sequences: dict[str, str]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            reference_id = row["reference_id"]
            handle.write(f">{reference_id}\n{sequences[reference_id]}\n")


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


def run_all_vs_all(query_fasta: Path, blastn: str) -> str:
    return run_checked(
        [
            blastn,
            "-task",
            "blastn",
            "-dust",
            "no",
            "-query",
            str(query_fasta),
            "-subject",
            str(query_fasta),
            "-num_threads",
            "1",
            "-evalue",
            "1e-20",
            "-word_size",
            "11",
            "-max_hsps",
            "1",
            "-max_target_seqs",
            "1000",
            "-outfmt",
            "6 qseqid qlen sseqid slen pident length evalue bitscore",
        ]
    )


def lineage_rank(lineage: str, prefix: str) -> str:
    for token in lineage.split(";"):
        if token.startswith(prefix):
            return token[len(prefix) :]
    return ""


def parse_edges(
    blast_output: str,
    rows_by_id: dict[str, dict[str, str]],
    min_pident: float,
    min_shorter_coverage: float,
) -> dict[tuple[str, str], dict[str, str]]:
    edges: dict[tuple[str, str], dict[str, str]] = {}
    for line in blast_output.splitlines():
        fields = line.split("\t")
        if len(fields) != 8:
            die(f"unexpected all-vs-all BLAST row with {len(fields)} fields")
        query_id, subject_id = fields[0], fields[2]
        if query_id not in rows_by_id or subject_id not in rows_by_id:
            die("all-vs-all BLAST returned an undeclared identifier")
        if query_id == subject_id:
            continue
        shorter_coverage = min(
            100.0,
            100.0 * int(fields[5]) / min(int(fields[1]), int(fields[3])),
        )
        pident = float(fields[4])
        if pident + 1e-9 < min_pident or shorter_coverage + 1e-9 < min_shorter_coverage:
            continue
        key = tuple(sorted((query_id, subject_id)))
        candidate = {
            "left": key[0],
            "right": key[1],
            "pident": f"{pident:.3f}",
            "alignment_length": fields[5],
            "shorter_sequence_coverage": f"{shorter_coverage:.3f}",
            "bitscore": fields[7],
        }
        if key not in edges or float(candidate["bitscore"]) > float(edges[key]["bitscore"]):
            edges[key] = candidate
    return edges


def connected_components(
    reference_ids: set[str], edges: dict[tuple[str, str], dict[str, str]]
) -> dict[str, tuple[str, ...]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    result: dict[str, tuple[str, ...]] = {}
    visited: set[str] = set()
    for start in sorted(reference_ids):
        if start in visited:
            continue
        stack = [start]
        component: list[str] = []
        visited.add(start)
        while stack:
            current = stack.pop()
            component.append(current)
            for neighbor in sorted(adjacency[current]):
                if neighbor not in visited:
                    visited.add(neighbor)
                    stack.append(neighbor)
        members = tuple(sorted(component))
        for member in members:
            result[member] = members
    return result


def build_adjudications(
    review_rows: list[dict[str, str]],
    comparison_rows: list[dict[str, str]],
    confirmed_anchor_ids: set[str],
    edges: dict[tuple[str, str], dict[str, str]],
) -> list[dict[str, str]]:
    rows_by_id = {row["reference_id"]: row for row in comparison_rows}
    components = connected_components(set(rows_by_id), edges)
    adjudications: list[dict[str, str]] = []
    for row in review_rows:
        reference_id = row["reference_id"]
        family = lineage_rank(row["header_lineage"], "f__")
        cross_family: list[tuple[dict[str, str], str]] = []
        for key, edge in edges.items():
            if reference_id not in key:
                continue
            neighbor = key[1] if key[0] == reference_id else key[0]
            neighbor_family = lineage_rank(rows_by_id[neighbor]["header_lineage"], "f__")
            if family and neighbor_family and family != neighbor_family:
                cross_family.append((edge, neighbor))
        cross_family.sort(
            key=lambda item: (
                -float(item[0]["pident"]),
                -float(item[0]["shorter_sequence_coverage"]),
                item[1],
            )
        )
        best_edge, neighbor = cross_family[0] if cross_family else ({}, "")
        component = components[reference_id]
        component_families = {
            lineage_rank(rows_by_id[member]["header_lineage"], "f__")
            for member in component
            if lineage_rank(rows_by_id[member]["header_lineage"], "f__")
        }
        component_id = (
            "singleton"
            if len(component) == 1
            else "cluster_" + sha256_bytes("\n".join(component).encode("utf-8"))[:12]
        )
        if neighbor:
            neighbor_row = rows_by_id[neighbor]
            disposition = "cross_family_sequence_label_conflict_candidate"
            notes = (
                "Meets the declared bacterial-control similarity screen and has a "
                ">=95% local sequence match carrying a different host-family assignment; "
                "this supports a sequence/label-conflict candidate, not proof of "
                "bacterial origin"
            )
        else:
            neighbor_row = {}
            disposition = "unresolved_insufficient_local_discriminator"
            notes = (
                "Meets the declared bacterial-control similarity screen but lacks a >=95% "
                "cross-family local match in the review-plus-confirmed-anchor scope; no "
                "biological origin is assigned"
            )
        adjudications.append(
            {
                "reference_id": reference_id,
                "reference_sequence_sha256": row["reference_sequence_sha256"],
                "stored_taxid": row["stored_taxid"],
                "header_class": lineage_rank(row["header_lineage"], "c__"),
                "header_order": lineage_rank(row["header_lineage"], "o__"),
                "header_family": family,
                "bacterial_control_accession": row["source_accession"],
                "bacterial_pident": row["pident"],
                "bacterial_alignment_length": row["alignment_length"],
                "bacterial_shorter_sequence_coverage": row[
                    "shorter_sequence_coverage"
                ],
                "cross_family_reference_id": neighbor,
                "cross_family_reference_taxid": neighbor_row.get("stored_taxid", ""),
                "cross_family_reference_class": lineage_rank(
                    neighbor_row.get("header_lineage", ""), "c__"
                ),
                "cross_family_reference_order": lineage_rank(
                    neighbor_row.get("header_lineage", ""), "o__"
                ),
                "cross_family_reference_family": lineage_rank(
                    neighbor_row.get("header_lineage", ""), "f__"
                ),
                "cross_family_reference_role": (
                    "confirmed_anchor"
                    if neighbor in confirmed_anchor_ids
                    else "review_candidate" if neighbor else ""
                ),
                "cross_family_pident": best_edge.get("pident", ""),
                "cross_family_alignment_length": best_edge.get(
                    "alignment_length", ""
                ),
                "cross_family_shorter_sequence_coverage": best_edge.get(
                    "shorter_sequence_coverage", ""
                ),
                "component_id": component_id,
                "component_size": str(len(component)),
                "component_family_count": str(len(component_families)),
                "disposition": disposition,
                "notes": notes,
            }
        )
    return adjudications


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
    adjudications: list[dict[str, str]],
    output_text: str,
    blast_version: str,
    confirmed_anchor_count: int,
) -> str:
    conflict_candidates = sum(
        row["disposition"] == "cross_family_sequence_label_conflict_candidate"
        for row in adjudications
    )
    review_neighbor_count = sum(
        row["cross_family_reference_role"] == "review_candidate"
        for row in adjudications
    )
    anchor_neighbor_count = sum(
        row["cross_family_reference_role"] == "confirmed_anchor"
        for row in adjudications
    )
    unresolved = len(adjudications) - conflict_candidates
    rows = [
        ("schema", "", "chain_a_local_cluster_adjudication_v2"),
        ("tool", "blastn", blast_version.splitlines()[0]),
        ("parameter", "output_scope", "review_only"),
        ("parameter", "component_scope", "review_plus_confirmed_anchors"),
        ("parameter", "task", "blastn"),
        ("parameter", "dust", "no"),
        ("parameter", "evalue", "1e-20"),
        ("parameter", "word_size", "11"),
        ("parameter", "max_hsps", "1"),
        ("parameter", "max_target_seqs", "1000"),
        ("parameter", "min_cross_family_pident", f"{args.min_cross_family_pident:g}"),
        (
            "parameter",
            "min_cross_family_shorter_sequence_coverage",
            f"{args.min_cross_family_shorter_coverage:g}",
        ),
        ("count", "all_review_records", str(len(adjudications))),
        ("count", "confirmed_anchor_records", str(confirmed_anchor_count)),
        (
            "count",
            "all_comparison_records",
            str(len(adjudications) + confirmed_anchor_count),
        ),
        ("count", "cross_family_conflict_candidates", str(conflict_candidates)),
        (
            "count",
            "selected_review_candidate_neighbors",
            str(review_neighbor_count),
        ),
        (
            "count",
            "selected_confirmed_anchor_neighbors",
            str(anchor_neighbor_count),
        ),
        ("count", "unresolved_records", str(unresolved)),
        ("sha256", str(args.audit), sha256_file(args.audit)),
        (
            "sha256",
            str(args.confirmed_anchor_manifest),
            sha256_file(args.confirmed_anchor_manifest),
        ),
        ("sha256", str(args.reference_source_fasta), sha256_file(args.reference_source_fasta)),
        ("sha256", str(args.output), sha256_bytes(output_text.encode("utf-8"))),
    ]
    return "field\tartifact\tvalue\n" + "".join(
        f"{field}\t{artifact}\t{value}\n" for field, artifact, value in rows
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--confirmed-anchor-manifest", type=Path, required=True)
    parser.add_argument("--reference-source-fasta", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance-output", type=Path, required=True)
    parser.add_argument("--blastn", default="blastn")
    parser.add_argument("--min-cross-family-pident", type=float, default=95.0)
    parser.add_argument("--min-cross-family-shorter-coverage", type=float, default=80.0)
    args = parser.parse_args()

    if not 0 < args.min_cross_family_pident <= 100:
        die("cross-family identity threshold must be in (0, 100]")
    if not 0 < args.min_cross_family_shorter_coverage <= 100:
        die("cross-family coverage threshold must be in (0, 100]")
    for path in (
        args.audit,
        args.confirmed_anchor_manifest,
        args.reference_source_fasta,
    ):
        if not path.is_file():
            die(f"required input is missing: {path}")
    if shutil.which(args.blastn) is None:
        die(f"BLAST executable is not available: {args.blastn}")

    audit_rows, review_rows = read_audit_rows(args.audit)
    audit_rows_by_id = {row["reference_id"]: row for row in audit_rows}
    confirmed_anchor_ids = read_confirmed_anchor_ids(args.confirmed_anchor_manifest)
    missing_anchors = sorted(set(confirmed_anchor_ids) - set(audit_rows_by_id))
    if missing_anchors:
        die(f"confirmed anchors missing from candidate audit: {missing_anchors}")
    review_ids = {row["reference_id"] for row in review_rows}
    overlapping_anchors = sorted(review_ids.intersection(confirmed_anchor_ids))
    if overlapping_anchors:
        die(f"confirmed anchors overlap review-tier rows: {overlapping_anchors}")
    anchor_rows = [audit_rows_by_id[reference_id] for reference_id in confirmed_anchor_ids]
    nonpriority_anchors = sorted(
        row["reference_id"]
        for row in anchor_rows
        if row["discovery_tier"] != "priority"
    )
    if nonpriority_anchors:
        die(f"confirmed anchors are not priority-tier audit rows: {nonpriority_anchors}")
    comparison_rows = review_rows + sorted(
        anchor_rows, key=lambda row: row["reference_id"]
    )
    sequences = extract_reference_sequences(args.reference_source_fasta, comparison_rows)
    with tempfile.TemporaryDirectory(prefix="rtbioscan-local-cluster-audit.") as temp:
        query_fasta = Path(temp) / "comparison-records.fa"
        write_query_fasta(query_fasta, comparison_rows, sequences)
        blast_output = run_all_vs_all(query_fasta, args.blastn)
    rows_by_id = {row["reference_id"]: row for row in comparison_rows}
    edges = parse_edges(
        blast_output,
        rows_by_id,
        args.min_cross_family_pident,
        args.min_cross_family_shorter_coverage,
    )
    adjudications = build_adjudications(
        review_rows,
        comparison_rows,
        set(confirmed_anchor_ids),
        edges,
    )
    output_text = tsv_text(OUTPUT_FIELDS, adjudications)
    provenance = provenance_text(
        args,
        adjudications,
        output_text,
        run_checked([args.blastn, "-version"]),
        len(anchor_rows),
    )
    write_atomic(args.output, output_text)
    write_atomic(args.provenance_output, provenance)
    conflict_candidates = sum(
        row["disposition"] == "cross_family_sequence_label_conflict_candidate"
        for row in adjudications
    )
    print(
        f"OK: wrote {len(adjudications)} local adjudications "
        f"({conflict_candidates} cross-family conflict candidates, "
        f"{len(adjudications) - conflict_candidates} unresolved)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
