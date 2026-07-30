#!/usr/bin/env python3
"""Replay the deterministic legacy taxonomy fixture after basecalling.

The replay intentionally separates FAST/LAST routing from a forced marker-lane
BLAST.  The latter models a read that has already entered a marker path and
therefore exposes downstream reference/taxonomy defects even when this exact
clean sequence is safely rejected by the current FAST decoy set.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


OUTPUT_FIELDS = [
    "query_id",
    "mode",
    "marker",
    "variant",
    "sequence_evidence",
    "fast_subject",
    "fast_label",
    "fast_pident",
    "fast_aln_length",
    "best_target_subject",
    "best_target_score",
    "best_offtarget_subject",
    "best_offtarget_score",
    "target_minus_offtarget_margin",
    "blast_subject",
    "blast_taxid",
    "header_kingdom",
    "resolved_superkingdom",
    "resolved_kingdom",
    "legacy_guard",
    "target_filter_expectation",
]


def die(message: str) -> "None":
    raise SystemExit(f"ERROR: {message}")


def read_fasta(path: Path) -> tuple[list[str], dict[str, str], dict[str, str]]:
    order: list[str] = []
    sequences: dict[str, list[str]] = {}
    headers: dict[str, str] = {}
    current = ""
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.rstrip("\r\n")
            if line.startswith(">"):
                full_header = line[1:]
                current = full_header.split()[0]
                if not current or current in sequences:
                    die(f"duplicate or empty FASTA identifier in {path}: {current!r}")
                order.append(current)
                sequences[current] = []
                headers[current] = full_header
            elif line:
                if not current:
                    die(f"sequence before first FASTA header in {path}")
                sequences[current].append(line.strip().upper())
    return order, {key: "".join(value) for key, value in sequences.items()}, headers


def read_cases(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {
            "query_id",
            "mode",
            "marker",
            "variant",
            "sequence_evidence",
            "source_accession",
            "legacy_defect",
            "target_filter_expectation",
        }
        if reader.fieldnames is None or set(reader.fieldnames) != required:
            die(f"unexpected classification-case schema in {path}")
        rows = list(reader)
    if not rows:
        die(f"classification-case table is empty: {path}")
    return rows


def validate_fixture(
    fixture: Path, cases_path: Path, synthetic_cases_path: Path
) -> tuple[list[dict[str, str]], dict[str, str]]:
    fasta_order, sequences, _ = read_fasta(fixture)
    cases = read_cases(cases_path)
    case_ids = [row["query_id"] for row in cases]
    fasta_case_ids = [entry.split("|", 1)[0] for entry in fasta_order]
    if case_ids != fasta_case_ids:
        die("classification_cases.tsv order/identifiers do not match the FASTA")
    if len(case_ids) != len(set(case_ids)):
        die("duplicate query_id in classification_cases.tsv")

    for entry, row in zip(fasta_order, cases):
        marker_parts = entry.split("|")
        if len(marker_parts) < 2 or marker_parts[1] != row["marker"]:
            die(f"marker mismatch for {row['query_id']}")
        if row["marker"] not in {"COI", "ITS2"}:
            die(f"unsupported marker for {row['query_id']}: {row['marker']}")
        if row["variant"] not in {"clean", "hac", "fast"}:
            die(f"unsupported variant for {row['query_id']}: {row['variant']}")
        sequence = sequences[entry]
        if not sequence or re.search(r"[^ACGTN]", sequence):
            die(f"invalid nucleotide sequence for {row['query_id']}")
        if (
            row["mode"] == "B2_UNSUPPORTED"
            and row["sequence_evidence"] != "off_target_supported"
        ):
            die("the unsupported B2 candidate must remain an off-target filter case")

    with synthetic_cases_path.open(newline="", encoding="utf-8") as handle:
        synthetic_rows = list(csv.DictReader(handle, delimiter="\t"))
    expected_pairs = {
        ("COI", "-557"),
        ("ITS2", "-557"),
        ("COI", "-561"),
        ("ITS2", "-561"),
    }
    observed_pairs = {
        (row.get("marker", ""), row.get("synthetic_taxid", ""))
        for row in synthetic_rows
    }
    if observed_pairs != expected_pairs:
        die("synthetic lineage fixture must cover -557 and -561 for both markers")
    return cases, sequences


def run_checked(
    command: list[str],
    *,
    input_text: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        command,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        die(f"command failed ({' '.join(command)}): {detail}")
    return completed.stdout


def first_last_hits(
    last_index: Path, fixture: Path, marker_by_query: dict[str, str]
) -> dict[str, tuple[str, str, str, str, str, str, str, str, str]]:
    output = run_checked(
        ["lastal", str(last_index), str(fixture), "-f", "BlastTab", "-P", "1"]
    )
    hits: dict[str, tuple[str, str, str, str]] = {}
    best_target: dict[str, tuple[float, str]] = {}
    best_offtarget: dict[str, tuple[float, str]] = {}
    for line in output.splitlines():
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        query_id = fields[0].split("|", 1)[0]
        if len(fields) < 12 or query_id not in marker_by_query:
            continue
        subject = fields[1]
        subject_parts = subject.split("|")
        label = "|".join(subject_parts[:2]) if len(subject_parts) >= 2 else subject
        if query_id not in hits:
            hits[query_id] = (
                subject,
                label,
                fields[2],
                fields[3],
            )
        score = float(fields[11])
        marker = marker_by_query[query_id]
        is_target = (marker == "COI" and label == "COI|Metazoa") or (
            marker == "ITS2" and label == "ITS2|Viridiplantae"
        )
        scores = best_target if is_target else best_offtarget
        if query_id not in scores or score > scores[query_id][0]:
            scores[query_id] = (score, subject)

    result: dict[str, tuple[str, str, str, str, str, str, str, str, str]] = {}
    for query_id, first in hits.items():
        target_score, target_subject = best_target.get(query_id, (None, ""))
        offtarget_score, offtarget_subject = best_offtarget.get(
            query_id, (None, "")
        )
        margin = (
            target_score - offtarget_score
            if target_score is not None and offtarget_score is not None
            else None
        )
        result[query_id] = first + (
            target_subject,
            f"{target_score:g}" if target_score is not None else "",
            offtarget_subject,
            f"{offtarget_score:g}" if offtarget_score is not None else "",
            f"{margin:g}" if margin is not None else "",
        )
    return result


def write_marker_query(
    output_path: Path,
    marker: str,
    fasta_order: list[str],
    sequences: dict[str, str],
) -> None:
    with output_path.open("w", encoding="utf-8") as handle:
        for entry in fasta_order:
            parts = entry.split("|")
            if len(parts) >= 2 and parts[1] == marker:
                handle.write(f">{entry}\n{sequences[entry]}\n")


def marker_blast_hits(
    fixture: Path,
    sequences: dict[str, str],
    marker_dbs: dict[str, Path],
    workdir: Path,
) -> dict[str, str]:
    fasta_order, _, _ = read_fasta(fixture)
    hits: dict[str, str] = {}
    for marker, database in marker_dbs.items():
        query_path = workdir / f"{marker}.fa"
        write_marker_query(query_path, marker, fasta_order, sequences)
        output = run_checked(
            [
                "blastn",
                "-task",
                "megablast",
                "-dust",
                "no",
                "-query",
                str(query_path),
                "-db",
                str(database),
                "-num_threads",
                "1",
                "-outfmt",
                "6 qseqid sseqid",
                "-perc_identity",
                "92",
                "-evalue",
                "11",
                "-max_hsps",
                "50",
                "-max_target_seqs",
                "1",
                "-word_size",
                "50",
                "-qcov_hsp_perc",
                "50",
                "-mt_mode",
                "2",
            ]
        )
        for line in output.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2:
                hits.setdefault(fields[0].split("|", 1)[0], fields[1])
    return hits


def selected_header_map(path: Path, wanted: set[str]) -> dict[str, str]:
    """Read only requested headers from a large reference FASTA.

    The legacy COI FASTA contains one known duplicate identifier.  Last-write
    behavior here mirrors the legacy lookup while avoiding loading ~800k
    reference sequences into memory.
    """
    headers: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            if not raw.startswith(">"):
                continue
            full_header = raw[1:].rstrip("\r\n")
            identifier = full_header.split()[0]
            if identifier in wanted:
                headers[identifier] = full_header
    return headers


def kingdom_from_header(header: str) -> str:
    match = re.search(r"(?:^|[ ;])k__([^; ]*)", header)
    return match.group(1) if match else ""


def global_synthetic_kingdoms(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            fields = raw.rstrip("\r\n").split("\t", 1)
            if len(fields) != 2:
                continue
            match = re.match(r"k__([^;]*)", fields[1])
            result[fields[0]] = match.group(1) if match else ""
    return result


def ncbi_lineages(taxids: set[str], taxonomy_dir: Path) -> dict[str, tuple[str, str]]:
    if not taxids:
        return {}
    ordered = sorted(taxids, key=int)
    env = dict(os.environ)
    env["TAXONKIT_DB"] = str(taxonomy_dir)
    lineage = run_checked(
        ["taxonkit", "lineage"], input_text="\n".join(ordered) + "\n", env=env
    )
    reformatted = run_checked(
        ["taxonkit", "reformat", "-f", "k__{k};K__{K}"],
        input_text=lineage,
        env=env,
    )
    result: dict[str, tuple[str, str]] = {}
    for line in reformatted.splitlines():
        fields = line.split("\t")
        if not fields:
            continue
        match = re.search(r"k__([^;]*);K__(.*)$", fields[-1])
        if match:
            result[fields[0]] = (match.group(1), match.group(2))
    return result


def write_tsv(path: Path | None, rows: list[dict[str, str]]) -> str:
    lines = ["\t".join(OUTPUT_FIELDS)]
    lines.extend("\t".join(row[field] for field in OUTPUT_FIELDS) for row in rows)
    text = "\n".join(lines) + "\n"
    if path is not None:
        path.write_text(text, encoding="utf-8")
    return text


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture",
        type=Path,
        default=repo_root / "conf/taxonomy_regression/classification_fixture.fa",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        default=repo_root / "conf/taxonomy_regression/classification_cases.tsv",
    )
    parser.add_argument(
        "--synthetic-cases",
        type=Path,
        default=repo_root / "conf/taxonomy_regression/synthetic_lineage_cases.tsv",
    )
    parser.add_argument("--taxonomy-data-dir", type=Path)
    parser.add_argument(
        "--last-index", type=Path, default=repo_root / "db/targets_All_tagged_nr95"
    )
    parser.add_argument(
        "--coi-db",
        type=Path,
        default=repo_root / "db/COInr98_2024Jun_RioNegro_Brazil",
    )
    parser.add_argument(
        "--its2-db",
        type=Path,
        default=repo_root / "db/ITS2nr98_2024Jun_RioNegro_Brazil",
    )
    parser.add_argument(
        "--coi-fasta",
        type=Path,
        default=repo_root / "db/COInr98_2024Jun_RioNegro_Brazil.fasta",
    )
    parser.add_argument(
        "--its2-fasta",
        type=Path,
        default=repo_root / "db/ITS2nr98_2024Jun_RioNegro_Brazil.fasta",
    )
    parser.add_argument(
        "--lineage-map",
        type=Path,
        default=repo_root / "db/DBnr_2024Jun_id2lineage.txt",
    )
    parser.add_argument(
        "--expected",
        type=Path,
        default=repo_root / "conf/taxonomy_regression/legacy_expected.tsv",
    )
    parser.add_argument("--write-observed", type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    cases, sequences = validate_fixture(
        args.fixture, args.cases, args.synthetic_cases
    )
    if args.validate_only:
        print("OK: deterministic taxonomy classification fixture is internally valid")
        return 0

    if args.taxonomy_data_dir is None:
        die("--taxonomy-data-dir is required unless --validate-only is used")
    required_paths = [
        args.fixture,
        args.cases,
        args.synthetic_cases,
        args.taxonomy_data_dir,
        Path(f"{args.last_index}.prj"),
        args.coi_fasta,
        args.its2_fasta,
        args.lineage_map,
    ]
    for path in required_paths:
        if not path.exists():
            die(f"required fixture input not found: {path}")
    for tool in ("lastal", "blastn", "taxonkit"):
        if shutil.which(tool) is None:
            die(f"required runtime tool not found: {tool}")

    fasta_order, _, _ = read_fasta(args.fixture)
    with tempfile.TemporaryDirectory(prefix="rtbioscan-taxonomy-fixture.") as temp:
        workdir = Path(temp)
        marker_by_query = {row["query_id"]: row["marker"] for row in cases}
        last_hits = first_last_hits(args.last_index, args.fixture, marker_by_query)
        blast_hits = marker_blast_hits(
            args.fixture,
            sequences,
            {"COI": args.coi_db, "ITS2": args.its2_db},
            workdir,
        )

    subjects_by_marker: dict[str, set[str]] = {"COI": set(), "ITS2": set()}
    marker_by_query = {row["query_id"]: row["marker"] for row in cases}
    for query_id, subject in blast_hits.items():
        subjects_by_marker[marker_by_query[query_id]].add(subject)
    coi_headers = selected_header_map(args.coi_fasta, subjects_by_marker["COI"])
    its2_headers = selected_header_map(args.its2_fasta, subjects_by_marker["ITS2"])
    header_maps = {"COI": coi_headers, "ITS2": its2_headers}
    synthetic_kingdoms = global_synthetic_kingdoms(args.lineage_map)
    taxids: set[str] = set()
    for subject in blast_hits.values():
        match = re.search(r"\|kraken:taxid\|(-?\d+)", subject)
        if match and not match.group(1).startswith("-"):
            taxids.add(match.group(1))
    lineages = ncbi_lineages(taxids, args.taxonomy_data_dir)

    expected_marker_kingdom = {"COI": "Metazoa", "ITS2": "Viridiplantae"}
    observed_rows: list[dict[str, str]] = []
    for row in cases:
        query_id = row["query_id"]
        marker = row["marker"]
        (
            fast_subject,
            fast_label,
            fast_pident,
            fast_aln_length,
            best_target_subject,
            best_target_score,
            best_offtarget_subject,
            best_offtarget_score,
            target_minus_offtarget_margin,
        ) = last_hits.get(
            query_id, ("", "Unassigned", "", "", "", "", "", "", "")
        )
        blast_subject = blast_hits.get(query_id, "")
        taxid_match = re.search(r"\|kraken:taxid\|(-?\d+)", blast_subject)
        taxid = taxid_match.group(1) if taxid_match else ""
        header = header_maps[marker].get(blast_subject, "")
        header_kingdom = kingdom_from_header(header)
        if taxid.startswith("-"):
            superkingdom = "Synthetic"
            kingdom = synthetic_kingdoms.get(taxid, "")
        else:
            superkingdom, kingdom = lineages.get(taxid, ("", ""))
        if not blast_subject:
            legacy_guard = "not_evaluated"
        elif not kingdom:
            legacy_guard = "keep_empty"
        elif kingdom == expected_marker_kingdom[marker]:
            legacy_guard = "keep"
        else:
            legacy_guard = "reject"
        observed_rows.append(
            {
                "query_id": query_id,
                "mode": row["mode"],
                "marker": marker,
                "variant": row["variant"],
                "sequence_evidence": row["sequence_evidence"],
                "fast_subject": fast_subject,
                "fast_label": fast_label,
                "fast_pident": fast_pident,
                "fast_aln_length": fast_aln_length,
                "best_target_subject": best_target_subject,
                "best_target_score": best_target_score,
                "best_offtarget_subject": best_offtarget_subject,
                "best_offtarget_score": best_offtarget_score,
                "target_minus_offtarget_margin": target_minus_offtarget_margin,
                "blast_subject": blast_subject,
                "blast_taxid": taxid,
                "header_kingdom": header_kingdom,
                "resolved_superkingdom": superkingdom,
                "resolved_kingdom": kingdom,
                "legacy_guard": legacy_guard,
                "target_filter_expectation": row["target_filter_expectation"],
            }
        )

    observed_text = write_tsv(args.write_observed, observed_rows)
    if args.write_observed is not None:
        print(f"OK: wrote observed taxonomy fixture to {args.write_observed}")
        return 0
    if not args.expected.is_file():
        die(f"expected legacy result not found: {args.expected}")
    expected_text = args.expected.read_text(encoding="utf-8")
    if observed_text != expected_text:
        sys.stderr.write("ERROR: deterministic taxonomy classification replay diverged\n")
        import difflib

        sys.stderr.writelines(
            difflib.unified_diff(
                expected_text.splitlines(keepends=True),
                observed_text.splitlines(keepends=True),
                fromfile=str(args.expected),
                tofile="observed",
            )
        )
        return 1
    print("OK: deterministic legacy taxonomy classification replay matched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
