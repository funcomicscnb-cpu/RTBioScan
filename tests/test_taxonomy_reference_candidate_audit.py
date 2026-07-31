import csv
import hashlib
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "audit_taxonomy_reference_candidates.py"
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"


def load_module():
    spec = importlib.util.spec_from_file_location("reference_candidate_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_tsv(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)


def test_query_extraction_is_manifest_driven_and_checksum_verified(tmp_path):
    audit = load_module()
    sequence = "ACGTACGT"
    source = tmp_path / "source.fa"
    source.write_text(">bucket|duplicate_label accession.1 description\nACGTACGT\n")
    manifest_path = tmp_path / "queries.tsv"
    write_tsv(
        manifest_path,
        audit.QUERY_FIELDS,
        [
            {
                "query_id": "query_one",
                "source_accession": "accession.1",
                "source_sequence_sha256": hashlib.sha256(sequence.encode()).hexdigest(),
                "evidence_status": "confirmed_bacterial",
                "notes": "test",
            }
        ],
    )

    manifest = audit.read_query_manifest(manifest_path)
    assert audit.extract_queries(source, manifest) == {"query_one": sequence}


def test_query_extraction_does_not_match_accession_prefixes(tmp_path):
    audit = load_module()
    source = tmp_path / "source.fa"
    source.write_text(">record accession.10 description\nACGT\n")
    manifest_path = tmp_path / "queries.tsv"
    write_tsv(
        manifest_path,
        audit.QUERY_FIELDS,
        [
            {
                "query_id": "query_one",
                "source_accession": "accession.1",
                "source_sequence_sha256": hashlib.sha256(b"ACGT").hexdigest(),
                "evidence_status": "confirmed_bacterial",
                "notes": "test",
            }
        ],
    )

    manifest = audit.read_query_manifest(manifest_path)

    try:
        audit.extract_queries(source, manifest)
    except SystemExit as error:
        assert "missing from source FASTA" in str(error)
    else:
        raise AssertionError("accession prefix was incorrectly accepted")


def test_shorter_sequence_coverage_retains_partial_length_reference():
    audit = load_module()
    blast = (
        "rickettsia|source=OQ374915.1\t749\t"
        "BOLD_RECORD|kraken:taxid|123\t538\t89.827\t462\t"
        "1\t458\t462\t1\t1e-160\t590\t" + "A" * 462 + "\n"
    )

    hits = audit.parse_blast(blast, {"rickettsia"}, 80.0)

    assert hits[("rickettsia", "BOLD_RECORD")]["shorter_sequence_coverage"] == "85.874"


def test_priority_boundary_is_independent_of_taxon_names():
    audit = load_module()
    manifest = [
        {
            "query_id": "q",
            "source_accession": "source.1",
            "source_sequence_sha256": hashlib.sha256(b"A" * 100).hexdigest(),
            "evidence_status": "confirmed_bacterial",
            "notes": "",
        }
    ]
    hits = {
        ("q", "opaque_reference"): {
            "query_id": "q",
            "reference_id": "opaque_reference",
            "stored_taxid": "42",
            "query_length": "100",
            "reference_length": "100",
            "pident": "97.000",
            "alignment_length": "90",
            "shorter_sequence_coverage": "90.000",
            "evalue": "1e-30",
            "bitscore": "150",
            "subject_start": "1",
            "subject_end": "90",
            "subject_aligned_sequence": "A" * 90,
        }
    }
    records = {
        "opaque_reference": (
            "opaque_reference|kraken:taxid|42 COI k__Metazoa;p__X",
            "A" * 100,
        )
    }

    rows = audit.build_candidates(
        manifest, {"q": "A" * 100}, hits, records, 97.0, 90.0
    )

    assert rows[0]["discovery_tier"] == "priority"
    assert rows[0]["review_status"] == "pending_adjudication"


def test_index_alignment_must_match_source_record():
    audit = load_module()
    hit = {
        "reference_id": "opaque_reference",
        "stored_taxid": "42",
        "subject_start": "1",
        "subject_end": "4",
        "subject_aligned_sequence": "ACGT",
    }
    audit.verify_hit_source_record(
        hit,
        "opaque_reference|kraken:taxid|42 COI k__Metazoa;p__X",
        "ACGTAAAA",
    )

    hit["subject_aligned_sequence"] = "TGCA"
    try:
        audit.verify_hit_source_record(
            hit,
            "opaque_reference|kraken:taxid|42 COI k__Metazoa;p__X",
            "ACGTAAAA",
        )
    except SystemExit as error:
        assert "index/source aligned sequence mismatch" in str(error)
    else:
        raise AssertionError("index/source mismatch was incorrectly accepted")


def test_committed_protocol_snapshot_is_complete_for_declared_output():
    with (FIXTURE_DIR / "chain_a_similarity_audit.tsv").open(
        newline="", encoding="utf-8"
    ) as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))

    assert len(rows) == 44
    assert len({(row["query_id"], row["reference_id"]) for row in rows}) == 44
    assert sum(row["discovery_tier"] == "priority" for row in rows) == 5
    assert sum(row["discovery_tier"] == "review" for row in rows) == 39
    assert {row["review_status"] for row in rows} == {"pending_adjudication"}
    assert {
        row["reference_id"]
        for row in rows
        if row["discovery_tier"] == "priority"
    } == {
        "BOLD_COI-5P_GBCL13897-12",
        "BOLD_COI-5P_ISUP118-14",
        "BOLD_COI-5P_GBCL13905-12",
        "BOLD_COI-5P_GBMHH30183-19",
        "BOLD_COI-5P_GBMIN70259-17",
    }


def test_committed_provenance_pins_counts_and_candidate_bytes():
    candidate_path = FIXTURE_DIR / "chain_a_similarity_audit.tsv"
    provenance_path = FIXTURE_DIR / "chain_a_similarity_audit_provenance.tsv"
    with provenance_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle, delimiter="\t"))
    values = {(row["field"], row["artifact"]): row["value"] for row in rows}

    assert values[("count", "all_candidates")] == "44"
    assert values[("count", "priority_candidates")] == "5"
    assert values[("count", "review_candidates")] == "39"
    expected_digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
    assert values[("sha256", str(candidate_path.relative_to(REPO_ROOT)))] == expected_digest


def test_priority_adjudications_are_sequence_reproducible(tmp_path):
    blastn = shutil.which("blastn")
    if blastn is None:
        pytest.skip("blastn is required for priority-adjudication replay")
    audit = load_module()
    with (FIXTURE_DIR / "chain_a_priority_adjudication.tsv").open(
        newline="", encoding="utf-8"
    ) as handle:
        adjudications = list(csv.DictReader(handle, delimiter="\t"))
    with (FIXTURE_DIR / "chain_a_similarity_audit.tsv").open(
        newline="", encoding="utf-8"
    ) as handle:
        discovery = {
            row["reference_id"]: row for row in csv.DictReader(handle, delimiter="\t")
        }

    assert {row["reference_id"] for row in adjudications} == {
        "BOLD_COI-5P_ISUP118-14",
        "BOLD_COI-5P_GBMIN70259-17",
    }
    assert {row["disposition"] for row in adjudications} == {
        "confirmed_reference_sequence_contamination"
    }

    wanted = {
        row[field]
        for row in adjudications
        for field in ("reference_id", "same_species_control_id")
    }
    reference_records = {
        header.split()[0].split("|", 1)[0]: sequence
        for header, sequence in audit.iter_fasta(
            REPO_ROOT / "db" / "COInr98_2024Jun_RioNegro_Brazil.fasta"
        )
        if header.split()[0].split("|", 1)[0] in wanted
    }
    assert set(reference_records) == wanted

    for index, row in enumerate(adjudications):
        candidate = reference_records[row["reference_id"]]
        control = reference_records[row["same_species_control_id"]]
        assert hashlib.sha256(candidate.encode()).hexdigest() == row[
            "reference_sequence_sha256"
        ]
        assert hashlib.sha256(control.encode()).hexdigest() == row[
            "same_species_control_sequence_sha256"
        ]
        assert discovery[row["reference_id"]]["pident"] == row["bacterial_pident"]
        assert discovery[row["reference_id"]]["alignment_length"] == row[
            "bacterial_alignment_length"
        ]
        assert discovery[row["reference_id"]]["shorter_sequence_coverage"] == row[
            "bacterial_query_coverage"
        ]

        query = tmp_path / f"candidate-{index}.fa"
        subject = tmp_path / f"same-species-{index}.fa"
        query.write_text(f">candidate\n{candidate}\n", encoding="utf-8")
        subject.write_text(f">control\n{control}\n", encoding="utf-8")
        result = subprocess.run(
            [
                blastn,
                "-task",
                "blastn",
                "-dust",
                "no",
                "-query",
                str(query),
                "-subject",
                str(subject),
                "-outfmt",
                "6 pident length qcovs",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        observed = result.stdout.splitlines()[0].split("\t")
        assert observed == [
            row["same_species_pident"],
            row["same_species_alignment_length"],
            row["same_species_candidate_coverage"],
        ]
