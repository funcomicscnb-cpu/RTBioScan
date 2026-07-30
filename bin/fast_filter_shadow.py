#!/usr/bin/env python3
"""Record FAST pre-filter competition without changing the legacy decision."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from pathlib import Path


SHADOW_FIELDS = [
    "round_barcode",
    "read_id",
    "read_length",
    "current_subject",
    "current_label",
    "current_score",
    "current_retained",
    "best_target_subject",
    "best_target_label",
    "best_target_score",
    "best_target_pident",
    "best_target_aln_length",
    "best_offtarget_subject",
    "best_offtarget_label",
    "best_offtarget_score",
    "best_offtarget_pident",
    "best_offtarget_aln_length",
    "target_minus_offtarget_margin",
    "competition_status",
]


@dataclass
class Alignment:
    line: str
    subject: str
    label: str
    score: float
    score_text: str
    pident: str
    aln_length: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fasta", required=True, type=Path)
    parser.add_argument("--legacy-out", required=True, type=Path)
    parser.add_argument("--shadow-out", required=True, type=Path)
    parser.add_argument("--summary-out", required=True, type=Path)
    parser.add_argument("--targets", required=True)
    parser.add_argument("--target-taxa", required=True)
    parser.add_argument("--round-barcode", required=True)
    parser.add_argument(
        "--empty",
        action="store_true",
        help="write empty/no-alignment diagnostics without reading LAST output",
    )
    return parser.parse_args()


def marker_schema(targets_text: str, taxa_text: str) -> dict[str, set[str]]:
    targets = targets_text.split("|")
    taxa = taxa_text.split("|")
    if len(targets) != len(taxa):
        raise ValueError(
            f"targets/target-taxa length mismatch: {len(targets)} != {len(taxa)}"
        )
    schema: dict[str, set[str]] = {}
    for marker, taxon in zip(targets, taxa):
        marker = marker.strip()
        taxon = taxon.strip()
        if not marker:
            raise ValueError("targets contains an empty marker")
        schema.setdefault(marker, set()).add(taxon)
    return schema


def fasta_inventory(path: Path) -> tuple[list[str], dict[str, int]]:
    order: list[str] = []
    lengths: dict[str, int] = {}
    current: str | None = None
    with path.open(encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith(">"):
                current = line[1:].split(None, 1)[0]
                if not current:
                    raise ValueError(f"empty FASTA identifier in {path}")
                if current in lengths:
                    raise ValueError(f"duplicate FASTA identifier '{current}' in {path}")
                order.append(current)
                lengths[current] = 0
            elif current is None:
                raise ValueError(f"sequence before first FASTA header in {path}")
            else:
                lengths[current] += len(line)
    return order, lengths


def subject_label(subject: str) -> str:
    parts = subject.split("|")
    return "|".join(parts[:2]) if len(parts) >= 2 else subject


def label_is_target(label: str, schema: dict[str, set[str]]) -> bool:
    parts = label.split("|", 1)
    marker = parts[0]
    taxon = parts[1] if len(parts) == 2 else ""
    allowed_taxa = schema.get(marker)
    if not allowed_taxa:
        return False
    # Match the legacy fixed-string grep: an empty taxon accepts the marker,
    # while a configured taxon accepts that prefix after "<marker>|".
    return "" in allowed_taxa or any(taxon.startswith(value) for value in allowed_taxa)


def parse_last(
    handle,
    schema: dict[str, set[str]],
) -> tuple[
    list[str],
    dict[str, Alignment],
    dict[str, Alignment],
    dict[str, Alignment],
    list[str],
]:
    legacy_lines: list[str] = []
    legacy_seen: set[str] = set()
    first: dict[str, Alignment] = {}
    best_target: dict[str, Alignment] = {}
    best_offtarget: dict[str, Alignment] = {}
    diagnostic_errors: list[str] = []
    for raw_line in handle:
        line = raw_line.rstrip("\n")
        if line.startswith("#"):
            continue
        # Reproduce: grep -v '^#' | awk '!seen[$1]++'. This deliberately
        # preserves the first blank row even though it carries no diagnostics.
        awk_fields = line.split(None, 1)
        legacy_key = awk_fields[0] if awk_fields else ""
        if legacy_key not in legacy_seen:
            legacy_seen.add(legacy_key)
            legacy_lines.append(line)
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) < 12:
            diagnostic_errors.append(
                f"LAST BlastTab row has {len(fields)} fields; expected >=12"
            )
            continue
        query_id = fields[0]
        subject = fields[1]
        label = subject_label(subject)
        try:
            score = float(fields[11])
        except ValueError:
            diagnostic_errors.append(
                f"invalid LAST score '{fields[11]}' for {query_id}"
            )
            continue
        alignment = Alignment(
            line=line,
            subject=subject,
            label=label,
            score=score,
            score_text=fields[11],
            pident=fields[2],
            aln_length=fields[3],
        )
        first.setdefault(query_id, alignment)
        destination = best_target if label_is_target(label, schema) else best_offtarget
        if query_id not in destination or score > destination[query_id].score:
            destination[query_id] = alignment
    return legacy_lines, first, best_target, best_offtarget, diagnostic_errors


def competition_status(
    target: Alignment | None, offtarget: Alignment | None
) -> tuple[str, str]:
    if target is None and offtarget is None:
        return "no_alignment", ""
    if target is not None and offtarget is None:
        return "target_only", ""
    if target is None and offtarget is not None:
        return "offtarget_only", ""
    assert target is not None and offtarget is not None
    margin = target.score - offtarget.score
    if margin > 0:
        status = "target_leads"
    elif margin < 0:
        status = "offtarget_leads"
    else:
        status = "tie"
    return status, f"{margin:g}"


def alignment_fields(alignment: Alignment | None) -> list[str]:
    if alignment is None:
        return ["", "", "", "", ""]
    return [
        alignment.subject,
        alignment.label,
        alignment.score_text,
        alignment.pident,
        alignment.aln_length,
    ]


def main() -> int:
    args = parse_args()
    schema = marker_schema(args.targets, args.target_taxa)
    order, lengths = fasta_inventory(args.fasta)
    if args.empty:
        legacy_lines, first, best_target, best_offtarget, diagnostic_errors = (
            [],
            {},
            {},
            {},
            [],
        )
    else:
        (
            legacy_lines,
            first,
            best_target,
            best_offtarget,
            diagnostic_errors,
        ) = parse_last(sys.stdin, schema)

    unknown_queries = (set(first) | set(best_target) | set(best_offtarget)) - set(lengths)
    if unknown_queries:
        preview = ", ".join(sorted(unknown_queries)[:3])
        diagnostic_errors.append(
            f"LAST output contains queries absent from FASTA: {preview}"
        )

    with args.legacy_out.open("w", encoding="utf-8", newline="") as legacy_handle:
        for line in legacy_lines:
            legacy_handle.write(line + "\n")

    if diagnostic_errors:
        preview = "; ".join(diagnostic_errors[:3])
        print(
            "ERROR: FAST shadow diagnostics were not written; "
            f"legacy first-hit output was preserved: {preview}",
            file=sys.stderr,
        )
        return 2

    summary: dict[str, list[int]] = {}

    def count(metric: str, read_length: int) -> None:
        values = summary.setdefault(metric, [0, 0])
        values[0] += 1
        values[1] += read_length

    with args.shadow_out.open("w", encoding="utf-8", newline="") as shadow_handle:
        writer = csv.writer(shadow_handle, delimiter="\t", lineterminator="\n")
        writer.writerow(SHADOW_FIELDS)
        for query_id in order:
            read_length = lengths[query_id]
            current = first.get(query_id)
            target = best_target.get(query_id)
            offtarget = best_offtarget.get(query_id)
            retained = bool(current and label_is_target(current.label, schema))
            status, margin = competition_status(target, offtarget)
            count("all_input", read_length)
            count("aligned" if current else "unaligned", read_length)
            current_status = "current_retained" if retained else "current_excluded"
            count(current_status, read_length)
            count(status, read_length)
            count(f"{current_status}__{status}", read_length)
            writer.writerow(
                [
                    args.round_barcode,
                    query_id,
                    read_length,
                    current.subject if current else "",
                    current.label if current else "",
                    current.score_text if current else "",
                    1 if retained else 0,
                    *alignment_fields(target),
                    *alignment_fields(offtarget),
                    margin,
                    status,
                ]
            )

    metric_order = [
        "all_input",
        "aligned",
        "unaligned",
        "current_retained",
        "current_excluded",
        "target_only",
        "offtarget_only",
        "target_leads",
        "offtarget_leads",
        "tie",
        "no_alignment",
    ]
    metric_order.extend(
        f"{current_status}__{status}"
        for current_status in ("current_retained", "current_excluded")
        for status in (
            "target_only",
            "offtarget_only",
            "target_leads",
            "offtarget_leads",
            "tie",
            "no_alignment",
        )
    )
    with args.summary_out.open("w", encoding="utf-8", newline="") as summary_handle:
        writer = csv.writer(summary_handle, delimiter="\t", lineterminator="\n")
        writer.writerow(["round_barcode", "metric", "reads", "bases"])
        for metric in metric_order:
            reads, bases = summary.get(metric, [0, 0])
            writer.writerow([args.round_barcode, metric, reads, bases])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
