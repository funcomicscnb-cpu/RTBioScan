import csv
import importlib.util
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"
VALIDATOR_PATH = REPO_ROOT / "bin" / "validate_taxonomy_classification_fixture.py"


def read_tsv(name):
    with (FIXTURE_DIR / name).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def load_validator_module():
    spec = importlib.util.spec_from_file_location(
        "taxonomy_fixture_validator", VALIDATOR_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_taxonomy_classification_fixture_is_internally_valid():
    completed = subprocess.run(
        [
            "python3",
            str(VALIDATOR_PATH),
            "--validate-only",
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "internally valid" in completed.stdout


def test_last_router_keeps_first_alignment_for_pipe_delimited_query(monkeypatch):
    validator = load_validator_module()
    output = (
        "query|COI|meta\tCOI|Metazoa|first\t99.0\t500\t0\t0\t1\t500\t1\t500\t1e-20\t100\n"
        "query|COI|meta\tCOI|Bacteria|later\t75.0\t60\t0\t0\t1\t60\t1\t60\t1e-5\t50\n"
    )
    monkeypatch.setattr(validator, "run_checked", lambda *args, **kwargs: output)

    hits = validator.first_last_hits(
        Path("index"), Path("fixture.fa"), {"query": "COI"}
    )

    assert hits["query"] == (
        "COI|Metazoa|first",
        "COI|Metazoa",
        "99.0",
        "500",
        "COI|Metazoa|first",
        "100",
        "COI|Bacteria|later",
        "50",
        "50",
    )


def test_legacy_expectation_captures_chain_a_b1_and_collision():
    rows = {row["query_id"]: row for row in read_tsv("legacy_expected.tsv")}

    chain_a = rows["chain_a_lr799917_hac"]
    assert chain_a["sequence_evidence"] == "confirmed_bacterial"
    assert chain_a["fast_label"] == "COI|Bacteria"
    assert chain_a["blast_taxid"] == "414942"
    assert chain_a["resolved_kingdom"] == "Metazoa"
    assert chain_a["legacy_guard"] == "keep"

    chain_b1 = rows["chain_b1_d11038_clean"]
    assert chain_b1["resolved_superkingdom"] == "Bacteria"
    assert chain_b1["resolved_kingdom"] == ""
    assert chain_b1["legacy_guard"] == "keep_empty"

    collision = rows["synthetic_collision_coi_neg557_clean"]
    assert collision["header_kingdom"] == "Metazoa"
    assert collision["resolved_kingdom"] == "Viridiplantae"
    assert collision["legacy_guard"] == "reject"
    assert collision["target_filter_expectation"] == "retain_metazoa"


def test_current_chain_a_fixtures_do_not_reproduce_fast_misrouting():
    rows = read_tsv("legacy_expected.tsv")
    chain_a_rows = [row for row in rows if row["mode"] == "A"]

    assert len(chain_a_rows) == 6
    assert {row["fast_label"] for row in chain_a_rows} == {"COI|Bacteria"}
    assert max(
        float(row["target_minus_offtarget_margin"]) for row in chain_a_rows
    ) <= -289
    assert {row["target_filter_expectation"] for row in chain_a_rows} == {
        "exclude_off_target"
    }


def test_chain_a_reference_candidates_capture_tie_without_overclaiming():
    rows = read_tsv("chain_a_reference_candidates.tsv")
    by_id = {row["reference_id"]: row for row in rows}

    assert set(by_id) == {
        "BOLD_COI-5P_GBCL13897-12",
        "BOLD_COI-5P_ISUP118-14",
        "BOLD_COI-5P_GBCL13905-12",
    }
    assert by_id["BOLD_COI-5P_GBCL13897-12"]["evidence_status"] == "confirmed"
    isup = by_id["BOLD_COI-5P_ISUP118-14"]
    assert isup["stored_taxid"] == "-2704"
    assert isup["header_kingdom"] == "Metazoa"
    assert isup["evidence_status"] == "candidate_pending_adjudication"
    assert isup["corrected_outcome"] == "adjudicate_before_release"
    assert isup["bitscore"] == by_id["BOLD_COI-5P_GBCL13897-12"]["bitscore"]
    assert isup["pident"] == by_id["BOLD_COI-5P_GBCL13897-12"]["pident"]
    assert isup["aln_length"] == by_id["BOLD_COI-5P_GBCL13897-12"]["aln_length"]

    lineage_map = REPO_ROOT / "db" / "DBnr_2024Jun_id2lineage.txt"
    neg2704 = [
        line.rstrip("\n").split("\t", 1)
        for line in lineage_map.read_text(encoding="utf-8").splitlines()
        if line.startswith("-2704\t")
    ]
    assert neg2704 == [
        [
            "-2704",
            "k__Metazoa;p__Arthropoda;c__Insecta;o__Coleoptera;"
            "f__Carabidae;g__Galerita;s__Galerita bicolor",
        ]
    ]


def test_b2_hypothesis_is_not_encoded_as_animal_truth():
    cases = {row["query_id"]: row for row in read_tsv("classification_cases.tsv")}
    legacy = {row["query_id"]: row for row in read_tsv("legacy_expected.tsv")}
    query_id = "b2_hypothesis_gbbac4514_clean"

    assert cases[query_id]["mode"] == "B2_UNSUPPORTED"
    assert cases[query_id]["sequence_evidence"] == "off_target_supported"
    assert legacy[query_id]["fast_label"] == "COI|Bacteria"
    assert legacy[query_id]["resolved_superkingdom"] == "Bacteria"
    assert legacy[query_id]["target_filter_expectation"] == "exclude_off_target"


def test_all_homonym_taxid_candidates_have_off_target_filter_support():
    rows = read_tsv("homonym_taxid_audit.tsv")
    assert len(rows) == 10
    assert {row["stored_taxid"] for row in rows} == {"1386", "265", "613", "1372"}
    assert {row["header_kingdom"] for row in rows} == {"Metazoa"}
    assert {row["best_bacterial_label"] for row in rows} == {"COI|Bacteria"}
    assert {row["target_filter_evidence"] for row in rows} == {
        "off_target_supported"
    }
    assert {row["evidence_tier"] for row in rows} == {"high", "moderate"}
    assert sum(row["evidence_tier"] == "high" for row in rows) == 5
    assert sum(row["evidence_tier"] == "moderate" for row in rows) == 5
    assert all(float(row["query_coverage"]) >= 97.0 for row in rows)
    assert all(float(row["bitscore"]) >= 625 for row in rows)


def test_human_is_an_explicit_offtarget_bucket_with_bacterial_contamination():
    cases = {row["query_id"]: row for row in read_tsv("classification_cases.tsv")}
    legacy = {row["query_id"]: row for row in read_tsv("legacy_expected.tsv")}
    audit = read_tsv("bidirectional_decoy_audit.tsv")[0]
    query_id = "control_metazoa_human_clean"

    assert cases[query_id]["mode"] == "CONTROL_HUMAN_OFFTARGET"
    assert cases[query_id]["target_filter_expectation"] == "exclude_off_target"
    assert legacy[query_id]["fast_label"] == "COI|Human"
    assert float(legacy[query_id]["fast_pident"]) > 99.0
    assert int(legacy[query_id]["fast_aln_length"]) == 532
    assert legacy[query_id]["best_target_subject"] == "COI|Metazoa|HG800486.1"
    assert legacy[query_id]["best_offtarget_subject"].startswith("COI|Human|")
    assert int(legacy[query_id]["target_minus_offtarget_margin"]) == -57
    assert legacy[query_id]["blast_taxid"] == "9606"
    assert audit["bacterial_competitor_label"] == "COI|Bacteria"
    assert float(audit["bacterial_competitor_pident"]) > 99.0
    assert int(audit["human_minus_bacterial_score_margin"]) == 27
    assert audit["operational_target_expectation"] == "exclude_off_target"


def test_synthetic_resolution_matrix_is_marker_scoped():
    rows = read_tsv("synthetic_lineage_cases.tsv")
    expected = {
        (row["marker"], row["synthetic_taxid"]): row["expected_marker_kingdom"]
        for row in rows
    }
    assert expected[("COI", "-557")] == "Metazoa"
    assert expected[("ITS2", "-557")] == "Viridiplantae"
    assert expected[("COI", "-561")] == "Metazoa"
    assert expected[("ITS2", "-561")] == "Viridiplantae"
