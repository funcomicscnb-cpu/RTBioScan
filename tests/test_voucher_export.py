from pathlib import Path
import hashlib
import os
import shutil
import subprocess
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("RTBIOSCAN_VOUCHER_EXPORT_SCRIPT", ROOT / "bin" / "voucher_export.sh"))
SUMMARY_HEADER = "sample\tmarker\treads\totu_key\tblast_suggestion\n"
ADMISSION_HEADER = (
    "round_barcode\tsample\totu_key\tconsensus_id\t"
    "taxonomy_admission_status\tmerged_fasta_sha256"
)
TAX_COLUMNS = [
    "consensus_id",
    "otu_key",
    "barcode_by_homology",
    "basecalling_model",
    "number_of_reads",
    "sample",
    "taxid",
    "blast_hit",
    "aln_length",
    "perc_id",
    "consensus_kingdom",
    "consensus_phylum",
    "consensus_class",
    "consensus_order",
    "consensus_family",
    "consensus_genus",
    "consensus_species",
]


def make_state(tmp_path, state="state-one", run="run-one"):
    results = tmp_path / "results with spaces"
    state_dir = results / "current" / "state" / state
    consensus = state_dir / "sequences" / "Consensus"
    tables = state_dir / "tables"
    consensus.mkdir(parents=True)
    tables.mkdir()
    (state_dir / "README.html").write_text(
        f'<p class="brand">RTBioScan &#183; Run {run} &#183; State {state}</p>\n',
        encoding="utf-8",
    )
    return results, state_dir, consensus, tables


def write_identity(results, run, rows, filename="track_identity.tsv"):
    target = results / "sample_info" / filename if run is None else results / "sample_info" / run / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = ["sample_id\tmarker_id\tunit_id_collapse\tunit_id_track"]
    lines.extend("\t".join(row) for row in rows)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_fasta(consensus, unit, records, directory_name=None):
    unit_dir = consensus / (directory_name or unit)
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / f"{unit}_Merged_Consensus.fasta"
    text = "".join(f">{header}\n{sequence}\n" for header, sequence in records)
    path.write_text(text, encoding="utf-8")
    return path


def tax_row(consensus_id, otu, marker, unit, genus="", species=""):
    return [
        consensus_id,
        otu,
        marker,
        "consensus",
        "10",
        unit,
        "123",
        "hit",
        "500",
        "99.0",
        "Viridiplantae",
        "phylum",
        "class",
        "order",
        "family",
        genus,
        species,
    ]


def write_taxonomy(tables, rows, name="round_blast_consensus_tax_rpt.txt"):
    path = tables / name
    lines = ["\t".join(TAX_COLUMNS)]
    lines.extend("\t".join(row) for row in rows)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run_export(results, out, *args, cwd=None, script=None, env=None, timeout=None):
    return subprocess.run(
        ["bash", str(script or SCRIPT), "--results", str(results), "--out", str(out), *args],
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
        env=None if env is None else {**os.environ, **env},
        timeout=timeout,
    )


def read_outputs(out):
    fasta = (out / "voucher_sequences.fasta").read_text(encoding="utf-8")
    summary = (out / "voucher_summary.tsv").read_text(encoding="utf-8")
    return fasta, summary


def expected_taxon_suggestion(genus, species):
    """Independent token-based oracle for the advisory header field."""
    genus, species = genus.strip(), species.strip()
    if genus == "Unassigned":
        return ""
    if not genus:
        return "_".join(species.split()) if species != "Unassigned" else ""
    if not species or species in ("Unassigned", genus):
        return genus + "_sp."
    if species.startswith(genus) and len(species) > len(genus) and species[len(genus)].isspace():
        species = species[len(genus):].lstrip()
    return "_".join((genus + "_" + species).split())


def write_stale_outputs(out):
    out.mkdir(parents=True, exist_ok=True)
    (out / "voucher_sequences.fasta").write_text("stale fasta\n", encoding="utf-8")
    (out / "voucher_summary.tsv").write_text("stale summary\n", encoding="utf-8")


def write_round_index(state_dir, text="round_barcode\tround_index\nround-2\t2\n"):
    path = state_dir / "live_round" / "tables" / "round_index.tsv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def fasta_sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def admission_row(round_barcode, unit, otu, consensus_id, digest, status="unassigned"):
    return (round_barcode, unit, otu, consensus_id, status, digest)


def write_admission(consensus, rows=(), header=ADMISSION_HEADER, raw_lines=None):
    path = consensus / "consensus_taxonomy_admission.tsv"
    if raw_lines is None:
        lines = [header, *("\t".join(row) for row in rows)]
    else:
        lines = raw_lines
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_head_script(tmp_path):
    data = subprocess.check_output(
        ["git", "show", "HEAD:bin/voucher_export.sh"], cwd=ROOT
    )
    path = tmp_path / "immutable-head-voucher-export.sh"
    path.write_bytes(data)
    path.chmod(0o755)
    return path


def output_snapshot(out):
    if not out.exists():
        return ()
    snapshot = []
    for path in sorted(out.iterdir(), key=lambda item: item.name):
        if path.is_symlink():
            value = ("symlink", os.readlink(path))
        elif path.is_file():
            value = ("file", path.read_bytes())
        elif path.is_dir():
            value = ("directory", None)
        else:
            value = ("other", None)
        snapshot.append((path.name, value))
    return tuple(snapshot)


def admission_info(stderr):
    return [line for line in stderr.splitlines() if line.startswith("INFO: Admission provenance:")]


def admission_warnings(stderr):
    return [line for line in stderr.splitlines() if line.startswith("WARN: Admission provenance ignored:")]


def make_admission_fixture(tmp_path, candidates, taxonomy=()):
    results, state_dir, consensus, tables = make_state(tmp_path)
    identity_rows = []
    fasta_paths = {}
    for item in candidates:
        unit = item["unit"]
        identity_rows.append((item.get("sample", unit), item.get("marker", "ITS2"), unit, unit))
        fasta_paths[unit] = write_fasta(
            consensus,
            unit,
            [
                (
                    f"{unit}|{item['name']}|{item.get('marker', 'ITS2')}|reads-{item['reads']}|OTU={item['otu']}",
                    item["sequence"],
                )
            ],
        )
    write_identity(results, "run-one", identity_rows)
    write_taxonomy(tables, list(taxonomy))
    write_round_index(state_dir)
    return results, state_dir, consensus, tables, fasta_paths


@pytest.mark.parametrize("reversed_rows", [False, True])
@pytest.mark.parametrize("locale,hash_seed", [("C", "0"), ("en_US.UTF-8", "17")])
def test_advisory_taxon_suggestions_preserve_voucher_records(tmp_path, reversed_rows, locale, hash_seed):
    cases = [
        ("Quercus", "Quercus robur", "Quercus_robur"),
        ("Quercus", "robur", "Quercus_robur"),
        ("Gorilla", "Gorilla gorilla", "Gorilla_gorilla"),
        ("Quercus", "Quercus sp.", "Quercus_sp."),
        ("Quercus", "Quercus cf. robur", "Quercus_cf._robur"),
        ("Quercus", "Quercus aff. robur", "Quercus_aff._robur"),
        ("Quercus", "", "Quercus_sp."),
        ("", "Quercus robur", "Quercus_robur"),
        ("", "", ""),
        ("Unassigned", "Quercus robur", ""),
        ("Quercus", "Unassigned", "Quercus_sp."),
        ("Quercus", "Quercusilex", "Quercus_Quercusilex"),
        ("  Quercus  ", "  Quercus   cf.   robur  ", "Quercus_cf._robur"),
        ("  ", "  Quercus   robur  ", "Quercus_robur"),
    ]
    assert [expected_taxon_suggestion(g, s) for g, s, _ in cases] == [want for _, _, want in cases]
    results, _, consensus, tables = make_state(tmp_path)
    identity_rows, taxonomy_rows, expected = [], [], []
    for index, (genus, species, suggestion) in enumerate(cases):
        sample = f"sample-{index:02d}"
        marker = "COI" if index % 2 else "ITS2"
        unit = f"unit-{index:02d}_{marker}"
        otu = f"OTUB_{index + 1}-{marker}"
        sequence = ("ACGT" if marker == "ITS2" else "TGCA") + "ACGT"[index % 4] * (index + 1)
        consensus_id = f"Consensus1_{unit}"
        identity_rows.append((sample, marker, unit, unit))
        write_fasta(consensus, unit, [(f"{unit}|Consensus1|{marker}|reads-{index + 5}|OTU={otu}", sequence)])
        taxonomy_rows.append(tax_row(consensus_id, otu, marker, unit, genus, species))
        header = f">{sample}|{marker}|reads-{index + 5}"
        if suggestion:
            header += f"|BLAST:{suggestion}"
        expected.append((header + "\n" + sequence + "\n", f"{sample}\t{marker}\t{index + 5}\t{otu}\t{suggestion}\n"))
    write_identity(results, "run-one", identity_rows)
    write_taxonomy(tables, list(reversed(taxonomy_rows)) if reversed_rows else taxonomy_rows)
    out = tmp_path / "export"
    result = run_export(results, out, env={"LC_ALL": locale, "PYTHONHASHSEED": hash_seed})
    assert result.returncode == 0, result.stderr
    fasta, summary = read_outputs(out)
    assert fasta == "".join(row[0] for row in expected)
    assert summary == SUMMARY_HEADER + "".join(row[1] for row in expected)
    assert fasta.count("\n>") + fasta.startswith(">") == len(cases)
    assert len(summary.splitlines()) == len(cases) + 1


def test_advisory_suggestion_normalizes_tabs_with_exact_genus_token():
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("    function taxonomy_suggestion(")
    end = source.index("    function load_taxonomy(", start)
    awk_program = source[start:end] + 'BEGIN { print taxonomy_suggestion(genus, species) }'
    for genus, species in [
        (" \tQuercus  ", "\tQuercus\t  cf. \t robur \t"),
        ("Quercus", "Quercusilex\tminor"),
        ("", " \tQuercus\t robur\t"),
    ]:
        result = subprocess.run(
            ["awk", "-v", f"genus={genus}", "-v", f"species={species}", awk_program],
            text=True, capture_output=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == expected_taxon_suggestion(genus, species) + "\n"


def test_positional_markers_filter_taxonomy_and_explicit_marker_compatibility(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path)
    write_identity(
        results,
        "run-one",
        [
            ("sample alpha", "ITS2", "sample_alpha_ITS2", "unit A_ITS2"),
            ("sample alpha", "COI", "sample_alpha_COI", "unit A_COI"),
            ("sample beta", "ITS2", "sample_beta_ITS2", "unit B_ITS2"),
        ],
    )
    its2 = write_fasta(
        consensus,
        "unit A_ITS2",
        [("unit A_ITS2|Consensus1|ITS2|reads-12|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_fasta(
        consensus,
        "unit A_COI",
        [("unit A_COI|Consensus2|COI|reads-20|OTU=OTUB_2-COI", "CCCC")],
    )
    write_fasta(
        consensus,
        "unit B_ITS2",
        [("unit B_ITS2|Consensus3|marker=ITS2|reads-8|OTU=OTUB_3-ITS2", "GGGG")],
    )
    write_taxonomy(
        tables,
        [
            tax_row("Consensus1_unit A_ITS2", "OTUB_1-ITS2", "ITS2", "unit A_ITS2", "Quercus", "robur"),
            tax_row("Consensus2_unit A_COI", "OTUB_2-COI", "COI", "unit A_COI", "Bombus", "sp."),
        ],
    )

    duplicate = state_dir / "sequences" / "single_exp" / "Consensus" / "copy"
    duplicate.mkdir(parents=True)
    (duplicate / "unit A_ITS2_Merged_Consensus.fasta").write_text(
        ">unit A_ITS2|ConsensusDecoy|ITS2|reads-999|OTU=OTUB_999-ITS2\nTTTT\n",
        encoding="utf-8",
    )
    recursive_decoy = state_dir / "current" / "results" / "Consensus" / "copy"
    recursive_decoy.mkdir(parents=True)
    shutil.copyfile(its2, recursive_decoy / "unit A_ITS2_Merged_Consensus.fasta")
    stale_tax = results / "temp" / "ongoing" / "blast_report_cons_full.txt"
    stale_tax.parent.mkdir(parents=True)
    stale_tax.write_text("wrong\n", encoding="utf-8")

    out = tmp_path / "output with spaces"
    result = run_export(results, out, "--marker", "ITS2")
    assert result.returncode == 0, result.stderr
    fasta, summary = read_outputs(out)
    assert fasta.count(">") == 2
    assert ">sample alpha|ITS2|reads-12|BLAST:Quercus_robur\nAAAA\n" in fasta
    assert ">sample beta|ITS2|reads-8\nGGGG\n" in fasta
    assert "|COI|" not in fasta
    assert "sample alpha\tITS2\t12\tOTUB_1-ITS2\tQuercus_robur" in summary
    assert "sample beta\tITS2\t8\tOTUB_3-ITS2\t\n" in summary


def test_identity_collapse_highest_support_and_distinct_samples(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(
        results,
        "run-one",
        [
            ("alpha", "ITS2", "alpha_ITS2", "alpha_rep1_ITS2"),
            ("alpha", "ITS2", "alpha_ITS2", "alpha_rep2_ITS2"),
            ("beta", "ITS2", "beta_ITS2", "beta_rep1_ITS2"),
        ],
        filename="replicate_identity.tsv",
    )
    write_fasta(
        consensus,
        "alpha_rep1_ITS2",
        [("alpha_rep1_ITS2|Consensus1|ITS2|reads-5|OTU=OTUB_9-ITS2", "AAAA")],
    )
    write_fasta(
        consensus,
        "alpha_rep2_ITS2",
        [("alpha_rep2_ITS2|Consensus2|ITS2|reads-15|OTU=OTUB_2-ITS2", "CCCC")],
    )
    write_fasta(
        consensus,
        "beta_rep1_ITS2",
        [("beta_rep1_ITS2|Consensus3|ITS2|reads-7|OTU=OTUB_3-ITS2", "GGGG")],
    )
    write_taxonomy(tables, [])

    out = tmp_path / "out"
    result = run_export(results, out)
    assert result.returncode == 0, result.stderr
    fasta, summary = read_outputs(out)
    assert fasta.count(">") == 2
    assert ">alpha|ITS2|reads-15\nCCCC\n" in fasta
    assert ">beta|ITS2|reads-7\nGGGG\n" in fasta
    assert "reads-5" not in fasta
    assert summary.splitlines()[1:] == [
        "alpha\tITS2\t15\tOTUB_2-ITS2\t",
        "beta\tITS2\t7\tOTUB_3-ITS2\t",
    ]


def build_tie_fixture(root, first_otu, second_otu):
    results, _, consensus, tables = make_state(root)
    write_identity(
        results,
        "run-one",
        [
            ("alpha", "ITS2", "alpha_ITS2", "unit_first_ITS2"),
            ("alpha", "ITS2", "alpha_ITS2", "unit_second_ITS2"),
        ],
    )
    write_fasta(
        consensus,
        "unit_first_ITS2",
        [(f"unit_first_ITS2|Consensus9|ITS2|reads-10|OTU={first_otu}",
          "AAAA" if first_otu == "OTUB_1-ITS2" else "TTTT")],
        directory_name="a-first",
    )
    write_fasta(
        consensus,
        "unit_second_ITS2",
        [(f"unit_second_ITS2|Consensus1|ITS2|reads-10|OTU={second_otu}",
          "AAAA" if second_otu == "OTUB_1-ITS2" else "TTTT")],
        directory_name="z-second",
    )
    write_taxonomy(tables, [])
    return results


def test_read_tie_is_independent_of_input_order(tmp_path):
    first = build_tie_fixture(tmp_path / "fixture-one", "OTUB_9-ITS2", "OTUB_1-ITS2")
    second = build_tie_fixture(tmp_path / "fixture-two", "OTUB_1-ITS2", "OTUB_9-ITS2")
    out_one = tmp_path / "out-one"
    out_two = tmp_path / "out-two"
    result_one = run_export(first, out_one)
    result_two = run_export(second, out_two)
    assert result_one.returncode == result_two.returncode == 0
    assert read_outputs(out_one) == read_outputs(out_two)
    fasta, summary = read_outputs(out_one)
    assert "OTUB_1-ITS2" in summary
    assert "OTUB_9-ITS2" not in summary
    assert fasta.count(">") == 1


def test_conflicting_taxonomy_fails_without_replacing_outputs(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-10|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(
        tables,
        [
            tax_row("Consensus1_unit_ITS2", "OTUB_1-ITS2", "ITS2", "unit_ITS2", "Quercus", "robur"),
            tax_row("Consensus1_unit_ITS2", "OTUB_1-ITS2", "ITS2", "unit_ITS2", "Pinus", "sylvestris"),
        ],
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / "voucher_sequences.fasta").write_text("stale fasta\n", encoding="utf-8")
    (out / "voucher_summary.tsv").write_text("stale summary\n", encoding="utf-8")

    result = run_export(results, out)
    assert result.returncode != 0
    assert "conflicting taxonomy" in result.stderr
    assert (out / "voucher_sequences.fasta").read_text() == "stale fasta\n"
    assert (out / "voucher_summary.tsv").read_text() == "stale summary\n"


def test_populated_empty_and_populated_replacement_with_filters(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("sample alpha", "ITS2", "sample_alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-10|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    out = tmp_path / "out"

    populated = run_export(results, out)
    assert populated.returncode == 0
    assert read_outputs(out)[0].count(">") == 1

    empty_marker = run_export(results, out, "--marker", "COI")
    assert empty_marker.returncode == 0
    assert "No voucher sequences matched" in empty_marker.stderr
    assert read_outputs(out) == ("", SUMMARY_HEADER)

    repopulated = run_export(results, out, "--sample", "sample alpha", "--marker", "ITS2")
    assert repopulated.returncode == 0
    assert read_outputs(out)[0].count(">") == 1

    empty_sample = run_export(results, out, "--sample", "absent")
    assert empty_sample.returncode == 0
    assert read_outputs(out) == ("", SUMMARY_HEADER)


def test_no_state_and_multiple_states_fail_without_publication(tmp_path):
    no_state_results = tmp_path / "no-state-results"
    no_state_results.mkdir()
    no_state_out = tmp_path / "no-state-out"
    no_state = run_export(no_state_results, no_state_out)
    assert no_state.returncode != 0
    assert "No authoritative published state" in no_state.stderr
    assert not no_state_out.exists()

    results, _, _, _ = make_state(tmp_path / "multi", state="state-one")
    make_state(tmp_path / "multi", state="state-two")
    out = tmp_path / "multi-out"
    out.mkdir()
    (out / "voucher_sequences.fasta").write_text("old fasta\n")
    (out / "voucher_summary.tsv").write_text("old summary\n")
    multiple = run_export(results, out)
    assert multiple.returncode != 0
    assert "Multiple authoritative published states" in multiple.stderr
    assert (out / "voucher_sequences.fasta").read_text() == "old fasta\n"
    assert (out / "voucher_summary.tsv").read_text() == "old summary\n"


def test_state_selector_and_unique_nested_identity_fallback(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-2|OTU=OTUB_1-ITS2", "AA")],
    )
    write_taxonomy(tables, [])
    (state_dir / "README.html").unlink()

    unique = run_export(results, tmp_path / "out-unique", "--state", "state-one")
    assert unique.returncode == 0, unique.stderr
    assert "unique-layout fallback (nested data-run 'run-one')" in unique.stderr

    explicit = run_export(
        results,
        tmp_path / "out-explicit",
        "--state",
        "state-one",
        "--run-id",
        "run-one",
    )
    assert explicit.returncode == 0, explicit.stderr


def test_conflicting_identity_mapping_is_fatal_and_preserves_old_outputs(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(
        results,
        "run-one",
        [
            ("alpha", "ITS2", "shared_ITS2", "unit_ITS2"),
            ("beta", "ITS2", "beta_ITS2", "unit_ITS2"),
        ],
    )
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-10|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    out = tmp_path / "out"
    out.mkdir()
    (out / "voucher_sequences.fasta").write_text("old fasta\n")
    (out / "voucher_summary.tsv").write_text("old summary\n")

    result = run_export(results, out)
    assert result.returncode != 0
    assert "conflicting identity mapping" in result.stderr
    assert (out / "voucher_sequences.fasta").read_text() == "old fasta\n"
    assert (out / "voucher_summary.tsv").read_text() == "old summary\n"


def test_cli_help_and_missing_argument_contract():
    help_result = subprocess.run(
        ["bash", str(SCRIPT), "--help"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert help_result.returncode == 0
    assert help_result.stdout == ""
    assert "Usage: bash bin/voucher_export.sh" in help_result.stderr
    assert "Select exact biological input identity root results/sample_info/ID" in help_result.stderr
    assert "Nextflow -name sets workflow.runName (diagnostic execution metadata here)." in help_result.stderr
    assert "--run-id selects the biological input identity root." in help_result.stderr
    assert "when state metadata is absent" not in help_result.stderr

    missing_value = subprocess.run(
        ["bash", str(SCRIPT), "--marker"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert missing_value.returncode != 0
    assert missing_value.stdout == ""
    assert "Option --marker requires a value" in missing_value.stderr


def test_valid_state_with_zero_consensus_records_replaces_stale_outputs(tmp_path):
    results, _, _, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_taxonomy(tables, [])
    out = tmp_path / "out"
    out.mkdir()
    (out / "voucher_sequences.fasta").write_text("stale fasta\n")
    (out / "voucher_summary.tsv").write_text("stale summary\n")

    result = run_export(results, out)
    assert result.returncode == 0, result.stderr
    assert "No voucher sequences matched" in result.stderr
    assert read_outputs(out) == ("", SUMMARY_HEADER)


def test_arbitrary_positional_token_is_not_inferred_as_marker(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|NOT_A_MARKER|reads-10|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])

    result = run_export(results, tmp_path / "out")
    assert result.returncode != 0
    assert "no configured positional marker" in result.stderr


def test_explicit_data_run_overrides_colliding_readme_execution_name(tmp_path):
    results, _, consensus, tables = make_state(tmp_path, run="execution-name")
    write_identity(results, "data-run", [("selected sample", "ITS2", "selected_ITS2", "unit_ITS2")])
    write_identity(results, "execution-name", [("wrong sample", "ITS2", "wrong_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-10|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])

    result = run_export(results, tmp_path / "out", "--run-id", "data-run")
    assert result.returncode == 0, result.stderr
    assert "README Run 'execution-name' is workflow.runName" in result.stderr
    assert "explicit --run-id 'data-run' selects biological input metadata" in result.stderr
    fasta, summary = read_outputs(tmp_path / "out")
    assert ">selected sample|ITS2|reads-10" in fasta
    assert "wrong sample" not in fasta + summary


def test_sole_nested_identity_root_ignores_absent_or_different_readme_run(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path, run="different-execution")
    write_identity(results, "only-data-run", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-3|OTU=OTUB_1-ITS2", "AAA")],
    )
    write_taxonomy(tables, [])

    different = run_export(results, tmp_path / "out-different")
    assert different.returncode == 0, different.stderr
    assert "unique-layout fallback (nested data-run 'only-data-run')" in different.stderr
    state_dir.joinpath("README.html").unlink()
    absent = run_export(results, tmp_path / "out-absent")
    assert absent.returncode == 0, absent.stderr
    assert read_outputs(tmp_path / "out-different") == read_outputs(tmp_path / "out-absent")


def test_sole_flat_identity_root_is_supported(tmp_path):
    results, _, consensus, tables = make_state(tmp_path, run="execution-name")
    write_identity(results, None, [("flat sample", "ITS2", "flat_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert "unique-layout fallback (flat results/sample_info)" in result.stderr
    assert ">flat sample|ITS2|reads-4" in read_outputs(tmp_path / "out")[0]


def test_identity_root_ambiguity_ignores_readme_name_and_preserves_outputs(tmp_path):
    nested_root = tmp_path / "nested"
    results, _, consensus, tables = make_state(nested_root, run="run-one")
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_identity(results, "run-two", [("beta", "ITS2", "beta_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    nested_out = nested_root / "out"
    write_stale_outputs(nested_out)

    nested = run_export(results, nested_out)
    assert nested.returncode != 0
    assert "Multiple direct identity roots" in nested.stderr
    assert read_outputs(nested_out) == ("stale fasta\n", "stale summary\n")

    mixed_root = tmp_path / "mixed"
    results, _, consensus, tables = make_state(mixed_root, run="run-one")
    write_identity(results, None, [("flat", "ITS2", "flat_ITS2", "unit_ITS2")])
    write_identity(results, "run-one", [("nested", "ITS2", "nested_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    mixed_out = mixed_root / "out"
    write_stale_outputs(mixed_out)

    mixed = run_export(results, mixed_out)
    assert mixed.returncode != 0
    assert "Multiple direct identity roots" in mixed.stderr
    assert read_outputs(mixed_out) == ("stale fasta\n", "stale summary\n")


def test_no_eligible_identity_root_fails_without_replacing_outputs(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    out = tmp_path / "out"
    write_stale_outputs(out)

    result = run_export(results, out)
    assert result.returncode != 0
    assert "No direct identity root" in result.stderr
    assert read_outputs(out) == ("stale fasta\n", "stale summary\n")


def test_explicit_missing_root_malformed_preferred_track_and_readme_state_conflict_preserve_outputs(tmp_path):
    missing_root = tmp_path / "missing"
    results, _, consensus, tables = make_state(missing_root)
    write_identity(results, "real-run", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    missing_out = missing_root / "out"
    write_stale_outputs(missing_out)
    missing = run_export(results, missing_out, "--run-id", "absent-run")
    assert missing.returncode != 0
    assert "Explicit --run-id 'absent-run' has no identity directory" in missing.stderr
    assert read_outputs(missing_out) == ("stale fasta\n", "stale summary\n")
    (results / "sample_info" / "empty-run").mkdir()
    missing_sidecar = run_export(results, missing_out, "--run-id", "empty-run")
    assert missing_sidecar.returncode != 0
    assert "Explicit --run-id 'empty-run' has no track_identity.tsv or replicate_identity.tsv" in missing_sidecar.stderr
    assert read_outputs(missing_out) == ("stale fasta\n", "stale summary\n")

    malformed_root = tmp_path / "malformed"
    results, _, consensus, tables = make_state(malformed_root)
    track = write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    track.write_text("sample_id\tunit_id_track\nalpha\tunit_ITS2\n", encoding="utf-8")
    write_identity(
        results,
        "run-one",
        [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")],
        filename="replicate_identity.tsv",
    )
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    malformed_out = malformed_root / "out"
    write_stale_outputs(malformed_out)
    malformed = run_export(results, malformed_out)
    assert malformed.returncode != 0
    assert "track_identity.tsv: missing required named identity columns" in malformed.stderr
    assert "replicate_identity.tsv" not in malformed.stderr
    assert read_outputs(malformed_out) == ("stale fasta\n", "stale summary\n")

    state_root = tmp_path / "state-conflict"
    results, state_dir, consensus, tables = make_state(state_root)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    state_dir.joinpath("README.html").write_text(
        '<p class="brand">RTBioScan &#183; Run run-one &#183; State wrong-state</p>\n',
        encoding="utf-8",
    )
    state_out = state_root / "out"
    write_stale_outputs(state_out)
    conflict = run_export(results, state_out)
    assert conflict.returncode != 0
    assert "not directory 'state-one'" in conflict.stderr
    assert read_outputs(state_out) == ("stale fasta\n", "stale summary\n")


def test_taxonomy_is_confined_to_selected_state_and_not_temp(tmp_path):
    results, _, consensus, tables = make_state(tmp_path, state="state-one")
    _, _, state_two_consensus, state_two_tables = make_state(tmp_path, state="state-two")
    write_identity(
        results,
        "run-one",
        [
            ("alpha", "ITS2", "alpha_ITS2", "unit_ITS2"),
            ("positive", "ITS2", "positive_ITS2", "positive_ITS2"),
        ],
    )
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-10|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_fasta(
        consensus,
        "positive_ITS2",
        [("positive_ITS2|Positive|ITS2|reads-6|OTU=OTUB-POSITIVE", "CCCC")],
    )
    write_taxonomy(
        tables,
        [tax_row("Positive_positive_ITS2", "OTUB-POSITIVE", "ITS2", "positive_ITS2", "Exact", "match")],
    )
    write_fasta(
        state_two_consensus,
        "unit_ITS2",
        [("unit_ITS2|StateTwo|ITS2|reads-999|OTU=OTUB-STATE-TWO", "TTTT")],
    )
    decoy = tax_row("Consensus1_unit_ITS2", "OTUB_1-ITS2", "ITS2", "unit_ITS2", "Wrong", "state")
    write_taxonomy(state_two_tables, [decoy])
    temp_tables = results / "temp" / "ongoing" / "tables"
    temp_tables.mkdir(parents=True)
    write_taxonomy(temp_tables, [decoy], name="temp_blast_consensus_tax_rpt.txt")

    result = run_export(results, tmp_path / "out", "--state", "state-one")
    assert result.returncode == 0, result.stderr
    fasta, summary = read_outputs(tmp_path / "out")
    assert fasta.count("|BLAST:") == 1
    assert "|BLAST:Exact_match" in fasta
    assert "alpha\tITS2\t10\tOTUB_1-ITS2\t\n" in summary
    assert "Wrong_state" not in fasta + summary


def test_taxonomy_join_requires_sample_marker_otu_and_consensus_id(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(
        results,
        "run-one",
        [
            ("sample-a", "ITS2", "sample-a_ITS2", "unit-a_ITS2"),
            ("sample-b", "ITS2", "sample-b_ITS2", "unit-b_ITS2"),
        ],
    )
    records = [
        ("positive|Exact|ITS2|reads-10|OTU=OTU-P", "AAAA"),
        ("losing|Winner|ITS2|reads-10|OTU=OTU-L", "CCCC"),
        ("losing|Loser|ITS2|reads-5|OTU=OTU-L", "CCCA"),
        ("unit-a_ITS2|SampleDim|ITS2|reads-10|OTU=OTU-S", "GGGG"),
        ("plain-marker|MarkerDim|ITS2|reads-10|OTU=OTU-M", "TTTT"),
        ("plain-otu|OtuDim|ITS2|reads-10|OTU=OTU-O", "ACAC"),
        ("plain-consensus|ConsensusDim|ITS2|reads-10|OTU=OTU-C", "CACA"),
    ]
    write_fasta(consensus, "mixed", records)
    write_taxonomy(
        tables,
        [
            tax_row("Exact_positive", "OTU-P", "ITS2", "positive", "Exact", "match"),
            tax_row("Loser_losing", "OTU-L", "ITS2", "losing", "Wrong", "loser"),
            tax_row("SampleDim_unit-a_ITS2", "OTU-S", "ITS2", "unit-b_ITS2", "Wrong", "sample"),
            tax_row("MarkerDim_plain-marker", "OTU-M", "COI", "plain-marker", "Wrong", "marker"),
            tax_row("OtuDim_plain-otu", "OTHER-OTU", "ITS2", "plain-otu", "Wrong", "otu"),
            tax_row("Other_plain-consensus", "OTU-C", "ITS2", "plain-consensus", "Wrong", "consensus"),
        ],
    )

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    fasta, summary = read_outputs(tmp_path / "out")
    assert fasta.count("|BLAST:") == 1
    assert "|BLAST:Exact_match" in fasta
    for wrong in ["Wrong_loser", "Wrong_sample", "Wrong_marker", "Wrong_otu", "Wrong_consensus"]:
        assert wrong not in fasta + summary
    assert ">losing|ITS2|reads-10\nCCCC\n" in fasta


def test_marker_discordance_warns_and_skips_before_filters_and_selection(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(
        results,
        "run-one",
        [
            ("alpha", "ITS2", "alpha_ITS2", "good_ITS2"),
            ("beta", "ITS2", "beta_ITS2", "mismatch_ITS2"),
        ],
    )
    write_fasta(
        consensus,
        "good_ITS2",
        [("good_ITS2|Good|ITS2|reads-10|OTU=OTU-G", "AAAA")],
    )
    write_fasta(
        consensus,
        "mismatch_ITS2",
        [("mismatch_ITS2|Mismatch|COI|reads-99|OTU=OTU-X", "XXXX")],
    )
    write_taxonomy(tables, [])

    cases = [
        ((), {"alpha"}),
        (("--sample", "alpha"), {"alpha"}),
        (("--marker", "ITS2"), {"alpha"}),
        (("--sample", "beta", "--marker", "COI"), set()),
    ]
    for index, (args, expected_samples) in enumerate(cases):
        out = tmp_path / f"out-{index}"
        result = run_export(results, out, *args)
        assert result.returncode == 0, result.stderr
        fasta, summary = read_outputs(out)
        assert "XXXX" not in fasta
        assert "marker-discordant FASTA candidate" in result.stderr
        assert "unit=mismatch_ITS2 biological_sample=beta expected_identity_marker=ITS2 observed_consensus_marker=COI" in result.stderr
        assert {line.split("|", 1)[0][1:] for line in fasta.splitlines() if line.startswith(">")} == expected_samples

    beta_fasta, beta_summary = read_outputs(tmp_path / "out-3")
    assert (beta_fasta, beta_summary) == ("", SUMMARY_HEADER)


def test_mismatch_cannot_displace_compatible_candidate_and_taxonomy_mismatch_is_skipped(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(
        results,
        "run-one",
        [
            ("beta", "ITS2", "beta_ITS2", "mismatch_ITS2"),
            ("beta", "COI", "beta_COI", "compatible_COI"),
            ("gamma", "ITS2", "gamma_ITS2", "gamma_ITS2"),
            ("gamma", "COI", "gamma_COI", "gamma_tax_COI"),
        ],
    )
    write_fasta(
        consensus,
        "mismatch_ITS2",
        [("mismatch_ITS2|Mismatch|COI|reads-99|OTU=OTU-X", "XXXX")],
    )
    write_fasta(
        consensus,
        "compatible_COI",
        [("compatible_COI|Compatible|COI|reads-5|OTU=OTU-B", "BBBB")],
    )
    write_fasta(
        consensus,
        "gamma_ITS2",
        [("gamma_ITS2|Gamma|ITS2|reads-7|OTU=OTU-T", "GGGG")],
    )
    write_taxonomy(
        tables,
        [tax_row("Gamma_gamma_ITS2", "OTU-T", "ITS2", "gamma_tax_COI", "Wrong", "taxonomy")],
    )

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    fasta, summary = read_outputs(tmp_path / "out")
    assert ">beta|COI|reads-5\nBBBB\n" in fasta
    assert "XXXX" not in fasta
    assert ">gamma|ITS2|reads-7\nGGGG\n" in fasta
    assert "Wrong_taxonomy" not in fasta + summary
    assert "marker-discordant FASTA candidate" in result.stderr
    assert "marker-discordant taxonomy row" in result.stderr


def test_marker_mismatch_warning_order_is_deterministic(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(
        results,
        "run-one",
        [
            ("alpha", "ITS2", "alpha_ITS2", "good_ITS2"),
            ("beta", "ITS2", "beta1_ITS2", "bad-z_ITS2"),
            ("beta", "ITS2", "beta2_ITS2", "bad-a_ITS2"),
        ],
    )
    write_fasta(consensus, "good_ITS2", [("good_ITS2|Good|ITS2|reads-1|OTU=OTU-G", "A")])
    mismatch_path = write_fasta(
        consensus,
        "mixed",
        [
            ("bad-z_ITS2|BadZ|COI|reads-9|OTU=OTU-Z", "Z"),
            ("bad-a_ITS2|BadA|COI|reads-8|OTU=OTU-A", "C"),
        ],
    )
    write_taxonomy(tables, [])

    first = run_export(results, tmp_path / "out-first")
    mismatch_path.write_text(
        ">bad-a_ITS2|BadA|COI|reads-8|OTU=OTU-A\nC\n"
        ">bad-z_ITS2|BadZ|COI|reads-9|OTU=OTU-Z\nZ\n",
        encoding="utf-8",
    )
    second = run_export(results, tmp_path / "out-second")
    assert first.returncode == second.returncode == 0
    assert read_outputs(tmp_path / "out-first") == read_outputs(tmp_path / "out-second")
    first_warnings = [line for line in first.stderr.splitlines() if "marker-discordant" in line]
    second_warnings = [line for line in second.stderr.splitlines() if "marker-discordant" in line]
    assert first_warnings == second_warnings
    assert len(first_warnings) == 2


def test_assignment_shaped_relative_results_matches_dot_prefixed_control(tmp_path):
    results, _, consensus, tables = make_state(tmp_path / "run=1")
    write_identity(results, "run-one", [("sample alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-12|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(
        tables,
        [tax_row("Consensus1_unit_ITS2", "OTUB_1-ITS2", "ITS2", "unit_ITS2", "Exact", "match")],
    )
    out = tmp_path / "output with spaces = one"

    plain = run_export("run=1/results with spaces", out, cwd=tmp_path)
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout == ""
    populated_bytes = read_outputs(out)
    assert populated_bytes[0] == ">sample alpha|ITS2|reads-12|BLAST:Exact_match\nAAAA\n"

    controlled = run_export("./run=1/results with spaces", out, cwd=tmp_path)
    assert controlled.returncode == plain.returncode
    assert controlled.stdout == plain.stdout
    assert controlled.stderr == plain.stderr
    assert read_outputs(out) == populated_bytes

    plain_empty = run_export("run=1/results with spaces", out, "--marker", "COI", cwd=tmp_path)
    assert plain_empty.returncode == 0, plain_empty.stderr
    assert plain_empty.stdout == ""
    assert read_outputs(out) == ("", SUMMARY_HEADER)

    controlled_empty = run_export("./run=1/results with spaces", out, "--marker", "COI", cwd=tmp_path)
    assert controlled_empty.returncode == plain_empty.returncode
    assert controlled_empty.stdout == plain_empty.stdout
    assert controlled_empty.stderr == plain_empty.stderr
    assert read_outputs(out) == ("", SUMMARY_HEADER)


def make_dot_path_fixture(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-6|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    return results


def test_dot_results_path_has_clean_diagnostics_and_equivalent_outputs(tmp_path):
    results = make_dot_path_fixture(tmp_path)
    out = tmp_path / "out"
    controlled = run_export(results, out, cwd=results)
    assert controlled.returncode == 0, controlled.stderr
    controlled_outputs = read_outputs(out)

    relative = run_export(".", out, cwd=results)
    assert relative.returncode == controlled.returncode
    assert relative.stdout == controlled.stdout == ""
    assert read_outputs(out) == controlled_outputs
    assert "./sample_info" in relative.stderr
    assert "././" not in relative.stderr


def test_dotdot_results_path_has_clean_diagnostics_and_equivalent_outputs(tmp_path):
    results = make_dot_path_fixture(tmp_path)
    runner = results / "runner directory"
    runner.mkdir()
    out = tmp_path / "out"
    controlled = run_export(results, out, cwd=runner)
    assert controlled.returncode == 0, controlled.stderr
    controlled_outputs = read_outputs(out)

    relative = run_export("..", out, cwd=runner)
    assert relative.returncode == controlled.returncode
    assert relative.stdout == controlled.stdout == ""
    assert read_outputs(out) == controlled_outputs
    assert "../sample_info" in relative.stderr
    assert "./../" not in relative.stderr


def test_identity_discovery_is_direct_depth_not_recursive(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    identity = write_identity(
        results,
        "run-one",
        [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")],
    )
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|Consensus1|ITS2|reads-4|OTU=OTUB_1-ITS2", "AAAA")],
    )
    write_taxonomy(tables, [])
    out = tmp_path / "out"

    immediate = run_export(results, out)
    assert immediate.returncode == 0, immediate.stderr
    assert ">alpha|ITS2|reads-4" in read_outputs(out)[0]
    prior_outputs = read_outputs(out)

    archive = identity.parent / "archive"
    archive.mkdir()
    identity.replace(archive / "track_identity.tsv")
    deep_only = run_export(results, out)
    assert deep_only.returncode != 0
    assert deep_only.stdout == ""
    assert "No direct identity root" in deep_only.stderr
    assert read_outputs(out) == prior_outputs


def test_admission_absent_is_exact_head_control_and_touches_no_round_or_digest(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|C1|ITS2|reads-7|OTU=OTU-1", "AAAA")],
    )
    write_taxonomy(tables, [])
    round_path = state_dir / "live_round" / "tables" / "round_index.tsv"
    round_path.parent.mkdir(parents=True)
    os.mkfifo(round_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_perl = fake_bin / "perl"
    fake_perl.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
    fake_perl.chmod(0o755)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
    out = tmp_path / "out"

    candidate = run_export(results, out, env=env, timeout=5)
    candidate_outputs = read_outputs(out)
    candidate_paths = sorted(path.name for path in out.iterdir())
    head = run_export(results, out, script=make_head_script(tmp_path), env=env, timeout=5)

    assert (candidate.returncode, candidate.stdout, candidate.stderr) == (
        head.returncode,
        head.stdout,
        head.stderr,
    )
    assert candidate_outputs == read_outputs(out)
    assert candidate_paths == sorted(path.name for path in out.iterdir()) == [
        "voucher_sequences.fasta",
        "voucher_summary.tsv",
    ]
    assert "Admission provenance" not in candidate.stderr


def make_publication_fixture(tmp_path):
    results, _, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(
        consensus,
        "unit_ITS2",
        [("unit_ITS2|C1|ITS2|reads-7|OTU=OTU-1", "AAAA")],
    )
    write_taxonomy(tables, [])
    return results


def configure_prior_publication(out, prior, symlink_targets):
    if out.exists():
        shutil.rmtree(out)
    symlink_targets.mkdir(parents=True, exist_ok=True)
    fasta_target = symlink_targets / "prior-fasta"
    summary_target = symlink_targets / "prior-summary"
    fasta_target.write_text("stale fasta\n", encoding="utf-8")
    summary_target.write_text("stale summary\n", encoding="utf-8")
    if prior == "neither":
        return
    out.mkdir(parents=True)
    if prior in {"fasta", "both"}:
        (out / "voucher_sequences.fasta").write_text("stale fasta\n", encoding="utf-8")
    if prior in {"summary", "both"}:
        (out / "voucher_summary.tsv").write_text("stale summary\n", encoding="utf-8")
    if prior == "symlinks":
        (out / "voucher_sequences.fasta").symlink_to(fasta_target)
        (out / "voucher_summary.tsv").symlink_to(summary_target)


@pytest.mark.parametrize("prior", ["neither", "fasta", "summary", "both", "symlinks"])
def test_sidecar_absent_publication_matches_head_without_hard_links(tmp_path, prior):
    results = make_publication_fixture(tmp_path)
    out = tmp_path / "output = publication control"
    symlink_targets = tmp_path / "symlink targets"
    fake_bin = tmp_path / "no-hard-links"
    fake_bin.mkdir()
    ln_called = tmp_path / "ln-called"
    fake_ln = fake_bin / "ln"
    fake_ln.write_text(
        f"#!/bin/sh\nprintf called > '{ln_called}'\nexit 97\n",
        encoding="utf-8",
    )
    fake_ln.chmod(0o755)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    configure_prior_publication(out, prior, symlink_targets)
    candidate = run_export(results, out, env=env)
    candidate_snapshot = output_snapshot(out)
    candidate_targets = (
        (symlink_targets / "prior-fasta").read_bytes(),
        (symlink_targets / "prior-summary").read_bytes(),
    )

    configure_prior_publication(out, prior, symlink_targets)
    head = run_export(results, out, script=make_head_script(tmp_path), env=env)
    head_snapshot = output_snapshot(out)
    head_targets = (
        (symlink_targets / "prior-fasta").read_bytes(),
        (symlink_targets / "prior-summary").read_bytes(),
    )

    assert (candidate.returncode, candidate.stdout, candidate.stderr) == (
        head.returncode,
        head.stdout,
        head.stderr,
    )
    assert candidate_snapshot == head_snapshot
    assert candidate_targets == head_targets == (b"stale fasta\n", b"stale summary\n")
    assert [name for name, _value in candidate_snapshot] == [
        "voucher_sequences.fasta",
        "voucher_summary.tsv",
    ]
    assert not ln_called.exists()


@pytest.mark.parametrize("failed_output", ["fasta", "summary"])
def test_sidecar_absent_publication_failure_matches_head(tmp_path, failed_output):
    results = make_publication_fixture(tmp_path)
    out = tmp_path / "out"
    symlink_targets = tmp_path / "unused-targets"
    fake_bin = tmp_path / "fake-publication"
    fake_bin.mkdir()
    ln_called = tmp_path / "ln-called"
    fake_ln = fake_bin / "ln"
    fake_ln.write_text(
        f"#!/bin/sh\nprintf called > '{ln_called}'\nexit 97\n",
        encoding="utf-8",
    )
    fake_ln.chmod(0o755)
    fake_mv = fake_bin / "mv"
    failure_pattern = ".voucher_sequences.fasta." if failed_output == "fasta" else ".voucher_summary.tsv."
    fake_mv.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in -f) source=$2;; *) source=$1;; esac\n"
        f"case \"$source\" in *{failure_pattern}*) exit 94;; esac\n"
        "exec /bin/mv \"$@\"\n",
        encoding="utf-8",
    )
    fake_mv.chmod(0o755)
    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}

    configure_prior_publication(out, "both", symlink_targets)
    candidate = run_export(results, out, env=env)
    candidate_snapshot = output_snapshot(out)
    configure_prior_publication(out, "both", symlink_targets)
    head = run_export(results, out, script=make_head_script(tmp_path), env=env)
    head_snapshot = output_snapshot(out)

    assert (candidate.returncode, candidate.stdout, candidate.stderr) == (
        head.returncode,
        head.stdout,
        head.stderr,
    )
    assert candidate.returncode == 94
    assert candidate_snapshot == head_snapshot
    if failed_output == "fasta":
        assert candidate_snapshot == (
            ("voucher_sequences.fasta", ("file", b"stale fasta\n")),
            ("voucher_summary.tsv", ("file", b"stale summary\n")),
        )
    else:
        assert candidate_snapshot == (
            ("voucher_sequences.fasta", ("file", b">alpha|ITS2|reads-7\nAAAA\n")),
            ("voucher_summary.tsv", ("file", b"stale summary\n")),
        )
    assert not ln_called.exists()


def test_sidecar_absent_cross_device_symlink_targets_match_head_when_available(tmp_path):
    cross_device_parent = next(
        (
            Path(candidate)
            for candidate in ("/dev/shm", "/run/shm")
            if Path(candidate).is_dir()
            and os.access(candidate, os.W_OK)
            and os.stat(candidate).st_dev != os.stat(tmp_path).st_dev
        ),
        None,
    )
    if cross_device_parent is None:
        pytest.skip("no writable cross-device scratch filesystem available")

    results = make_publication_fixture(tmp_path)
    out = tmp_path / "out"
    cross_device_dir = Path(tempfile.mkdtemp(prefix="rtbioscan-voucher-", dir=cross_device_parent))
    try:
        configure_prior_publication(out, "symlinks", cross_device_dir)
        candidate = run_export(results, out)
        candidate_snapshot = output_snapshot(out)
        configure_prior_publication(out, "symlinks", cross_device_dir)
        head = run_export(results, out, script=make_head_script(tmp_path))

        assert (candidate.returncode, candidate.stdout, candidate.stderr) == (
            head.returncode,
            head.stdout,
            head.stderr,
        )
        assert candidate_snapshot == output_snapshot(out)
    finally:
        shutil.rmtree(cross_device_dir)


def assert_invalid_private_evidence(result, out, expected_outputs, reason):
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert read_outputs(out) == expected_outputs
    assert admission_info(result.stderr) == [
        "INFO: Admission provenance: invalid; classified=0 admitted_unassigned=0 unknown=1"
    ]
    warnings = admission_warnings(result.stderr)
    assert len(warnings) == 1
    assert f"reason={reason}" in warnings[0]
    assert "line=- unit=- otu_key=- consensus_id=-" in warnings[0]
    assert not list(out.glob(".voucher_*"))


@pytest.mark.parametrize("shape", ["directory", "dangling", "fifo"])
def test_invalid_sidecar_shapes_are_nonfatal_unknown_and_never_opened(tmp_path, shape):
    candidates = [
        {"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}
    ]
    results, _, consensus, _, _ = make_admission_fixture(tmp_path, candidates)
    sidecar = consensus / "consensus_taxonomy_admission.tsv"
    if shape == "directory":
        sidecar.mkdir()
    elif shape == "dangling":
        sidecar.symlink_to("missing-admission.tsv")
    else:
        os.mkfifo(sidecar)
    out = tmp_path / "out"

    head = run_export(results, out, script=make_head_script(tmp_path), timeout=5)
    expected_outputs = read_outputs(out)
    result = run_export(results, out, timeout=5)

    assert head.returncode == 0, head.stderr
    assert_invalid_private_evidence(result, out, expected_outputs, "sidecar_unavailable")


def test_unreadable_sidecar_is_nonfatal_unknown(tmp_path):
    candidates = [
        {"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}
    ]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    sidecar = write_admission(
        consensus,
        [admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"]))],
    )
    sidecar.chmod(0)
    if os.access(sidecar, os.R_OK):
        sidecar.chmod(0o600)
        pytest.skip("runtime privileges bypass unreadable-file permissions")
    out = tmp_path / "out"
    try:
        head = run_export(results, out, script=make_head_script(tmp_path))
        expected_outputs = read_outputs(out)
        result = run_export(results, out)
        assert head.returncode == 0, head.stderr
        assert_invalid_private_evidence(result, out, expected_outputs, "sidecar_unavailable")
    finally:
        sidecar.chmod(0o600)


def test_exact_header_only_admission_is_diagnostic_only(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit_ITS2")])
    write_fasta(consensus, "unit_ITS2", [("unit_ITS2|C1|ITS2|reads-7|OTU=OTU-1", "AAAA")])
    write_taxonomy(tables, [])
    write_round_index(state_dir)
    write_admission(consensus)
    out = tmp_path / "out"

    head = run_export(results, out, script=make_head_script(tmp_path))
    head_outputs = read_outputs(out)
    candidate = run_export(results, out)

    assert candidate.returncode == head.returncode == 0
    assert candidate.stdout == head.stdout == ""
    assert read_outputs(out) == head_outputs
    assert admission_info(candidate.stderr) == [
        "INFO: Admission provenance: header_only; classified=0 admitted_unassigned=0 unknown=1"
    ]
    assert admission_warnings(candidate.stderr) == []


def test_admission_states_are_counted_without_changing_reads_first_output(tmp_path):
    candidates = [
        {"unit": "unit-class", "sample": "alpha", "name": "Class", "otu": "OTU-C", "reads": 9, "sequence": "CCCC"},
        {"unit": "unit-admit", "sample": "alpha", "name": "Admit", "otu": "OTU-A", "reads": 15, "sequence": "AAAA"},
        {"unit": "unit-unknown", "sample": "alpha", "name": "Unknown", "otu": "OTU-U", "reads": 12, "sequence": "UUUU"},
    ]
    taxonomy = [tax_row("Class_unit-class", "OTU-C", "ITS2", "unit-class", "Exact", "species")]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates, taxonomy)
    write_admission(
        consensus,
        [admission_row("round-2", "unit-admit", "OTU-A", "Admit_unit-admit", fasta_sha256(fastas["unit-admit"]))],
    )
    out = tmp_path / "out"

    head = run_export(results, out, script=make_head_script(tmp_path))
    head_outputs = read_outputs(out)
    candidate = run_export(results, out)

    assert candidate.returncode == head.returncode == 0
    assert read_outputs(out) == head_outputs
    assert head_outputs == (
        ">alpha|ITS2|reads-15\nAAAA\n",
        SUMMARY_HEADER + "alpha\tITS2\t15\tOTU-A\t\n",
    )
    assert admission_info(candidate.stderr) == [
        "INFO: Admission provenance: loaded; classified=1 admitted_unassigned=1 unknown=1"
    ]
    assert admission_warnings(candidate.stderr) == []


@pytest.mark.parametrize(
    ("classified_reads", "admitted_reads", "expected_header", "expected_sequence"),
    [
        (9, 15, ">alpha|ITS2|reads-15", "AAAA"),
        (15, 9, ">alpha|ITS2|reads-15|BLAST:Exact_species", "CCCC"),
    ],
)
def test_classified_and_admitted_competition_remains_reads_first(
    tmp_path, classified_reads, admitted_reads, expected_header, expected_sequence
):
    candidates = [
        {"unit": "class-unit", "sample": "alpha", "name": "Class", "otu": "OTU-C", "reads": classified_reads, "sequence": "CCCC"},
        {"unit": "admit-unit", "sample": "alpha", "name": "Admit", "otu": "OTU-A", "reads": admitted_reads, "sequence": "AAAA"},
    ]
    taxonomy = [tax_row("Class_class-unit", "OTU-C", "ITS2", "class-unit", "Exact", "species")]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates, taxonomy)
    write_admission(
        consensus,
        [admission_row("round-2", "admit-unit", "OTU-A", "Admit_admit-unit", fasta_sha256(fastas["admit-unit"]))],
    )

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    fasta, _summary = read_outputs(tmp_path / "out")
    assert fasta == f"{expected_header}\n{expected_sequence}\n"
    assert "classified=1 admitted_unassigned=1 unknown=0" in result.stderr


@pytest.mark.parametrize(
    ("kind", "taxonomy", "with_row", "expected_info"),
    [
        ("admitted", (), True, "classified=0 admitted_unassigned=1 unknown=0"),
        ("legacy-unknown", (), False, "classified=0 admitted_unassigned=0 unknown=1"),
    ],
)
def test_single_admitted_or_legacy_unknown_candidate_exports_normally(
    tmp_path, kind, taxonomy, with_row, expected_info
):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 6, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates, taxonomy)
    rows = []
    if with_row:
        rows.append(admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"])))
    write_admission(consensus, rows)
    out = tmp_path / f"out-{kind}"

    head = run_export(results, out, script=make_head_script(tmp_path))
    head_outputs = read_outputs(out)
    result = run_export(results, out)
    assert result.returncode == head.returncode == 0
    assert read_outputs(out) == head_outputs == (
        ">alpha|ITS2|reads-6\nACGT\n",
        SUMMARY_HEADER + "alpha\tITS2\t6\tOTU-1\t\n",
    )
    assert expected_info in result.stderr


def test_unknown_higher_read_candidate_still_beats_classified(tmp_path):
    candidates = [
        {"unit": "class-unit", "sample": "alpha", "name": "Class", "otu": "OTU-C", "reads": 8, "sequence": "CCCC"},
        {"unit": "unknown-unit", "sample": "alpha", "name": "Unknown", "otu": "OTU-U", "reads": 20, "sequence": "UUUU"},
    ]
    taxonomy = [tax_row("Class_class-unit", "OTU-C", "ITS2", "class-unit", "Exact", "species")]
    results, _, consensus, _, _ = make_admission_fixture(tmp_path, candidates, taxonomy)
    write_admission(consensus)

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert read_outputs(tmp_path / "out")[0] == ">alpha|ITS2|reads-20\nUUUU\n"
    assert "classified=1 admitted_unassigned=0 unknown=1" in result.stderr


def test_classified_taxonomy_silently_precedes_valid_admission_row(tmp_path):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Both", "otu": "OTU-1", "reads": 10, "sequence": "ACGT"}]
    taxonomy = [tax_row("Both_unit-one", "OTU-1", "ITS2", "unit-one", "Exact", "species")]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates, taxonomy)
    write_admission(
        consensus,
        [admission_row("round-2", "unit-one", "OTU-1", "Both_unit-one", fasta_sha256(fastas["unit-one"]))],
    )

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert "classified=1 admitted_unassigned=0 unknown=0" in result.stderr
    assert admission_warnings(result.stderr) == []
    assert "|BLAST:Exact_species" in read_outputs(tmp_path / "out")[0]


@pytest.mark.parametrize(
    ("authority_case", "reason"),
    [
        ("missing", "round_authority_unavailable"),
        ("dangling", "round_authority_unavailable"),
        ("dangling-live-round", "round_authority_unavailable"),
        ("directory", "round_authority_unavailable"),
        ("unreadable", "round_authority_unavailable"),
        ("fifo", "round_authority_unavailable"),
        ("malformed", "round_authority_malformed"),
        ("multi-row", "round_authority_malformed"),
        ("wrong-header", "round_authority_malformed"),
    ],
)
def test_invalid_round_authority_is_unknown_nonfatal_and_output_compatible(
    tmp_path, authority_case, reason
):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, state_dir, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    round_path = state_dir / "live_round" / "tables" / "round_index.tsv"
    round_path.unlink()
    if authority_case == "dangling":
        round_path.symlink_to("missing-round-index.tsv")
    elif authority_case == "dangling-live-round":
        shutil.rmtree(state_dir / "live_round")
        (state_dir / "live_round").symlink_to("missing-live-round")
    elif authority_case == "directory":
        round_path.mkdir()
    elif authority_case == "unreadable":
        round_path.write_text("round_barcode\tround_index\nround-2\t2\n", encoding="utf-8")
        round_path.chmod(0)
        if os.access(round_path, os.R_OK):
            round_path.chmod(0o600)
            pytest.skip("runtime privileges bypass unreadable-file permissions")
    elif authority_case == "fifo":
        os.mkfifo(round_path)
    elif authority_case == "malformed":
        round_path.write_text("round_barcode\tround_index\n", encoding="utf-8")
    elif authority_case == "multi-row":
        round_path.write_text("round_barcode\tround_index\nround-1\t1\nround-2\t2\n", encoding="utf-8")
    elif authority_case == "wrong-header":
        round_path.write_text("round_index\tround_barcode\n2\tround-2\n", encoding="utf-8")
    write_admission(
        consensus,
        [admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"]))],
    )
    out = tmp_path / "out"

    try:
        head = run_export(results, out, script=make_head_script(tmp_path), timeout=5)
        head_outputs = read_outputs(out)
        result = run_export(results, out, timeout=5)
    finally:
        if authority_case == "unreadable":
            round_path.chmod(0o600)

    assert result.returncode == head.returncode == 0
    assert read_outputs(out) == head_outputs
    assert admission_info(result.stderr) == [
        "INFO: Admission provenance: invalid; classified=0 admitted_unassigned=0 unknown=1"
    ]
    warnings = admission_warnings(result.stderr)
    assert len(warnings) == 1
    assert f"reason={reason}" in warnings[0]
    assert "line=- unit=- otu_key=- consensus_id=-" in warnings[0]


@pytest.mark.parametrize("round_barcode", ["round-1", "round-3"])
def test_stale_or_future_round_evidence_warns_and_stays_unknown(tmp_path, round_barcode):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    write_admission(
        consensus,
        [admission_row(round_barcode, "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"]))],
    )

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert "loaded; classified=0 admitted_unassigned=0 unknown=1" in result.stderr
    assert len(admission_warnings(result.stderr)) == 1
    assert "reason=stale_round" in admission_warnings(result.stderr)[0]


def test_digest_mismatch_warns_and_stays_unknown_without_output_change(tmp_path):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, _ = make_admission_fixture(tmp_path, candidates)
    write_admission(consensus, [admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", "0" * 64)])
    out = tmp_path / "out"

    head = run_export(results, out, script=make_head_script(tmp_path))
    expected = read_outputs(out)
    result = run_export(results, out)
    assert result.returncode == 0, result.stderr
    assert read_outputs(out) == expected
    assert "admitted_unassigned=0 unknown=1" in result.stderr
    assert "reason=digest_mismatch" in admission_warnings(result.stderr)[0]


def test_cross_sample_digest_cannot_validate_another_source_unit(tmp_path):
    candidates = [
        {"unit": "unit-a", "sample": "alpha", "name": "A", "otu": "OTU-A", "reads": 4, "sequence": "AAAA"},
        {"unit": "unit-b", "sample": "beta", "name": "B", "otu": "OTU-B", "reads": 5, "sequence": "BBBB"},
    ]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    write_admission(
        consensus,
        [admission_row("round-2", "unit-b", "OTU-B", "B_unit-b", fasta_sha256(fastas["unit-a"]))],
    )

    result = run_export(results, tmp_path / "out", "--sample", "beta")
    assert result.returncode == 0, result.stderr
    assert "classified=0 admitted_unassigned=0 unknown=1" in result.stderr
    assert "reason=digest_mismatch" in admission_warnings(result.stderr)[0]
    assert read_outputs(tmp_path / "out")[0] == ">beta|ITS2|reads-5\nBBBB\n"


@pytest.mark.parametrize(
    ("case", "header", "row_builder", "status", "reason"),
    [
        ("extra-header", ADMISSION_HEADER + "\textra", None, "invalid", "invalid_header"),
        ("missing-header", "\t".join(ADMISSION_HEADER.split("\t")[:-1]), None, "invalid", "invalid_header"),
        ("reordered-header", "sample\tround_barcode\totu_key\tconsensus_id\ttaxonomy_admission_status\tmerged_fasta_sha256", None, "invalid", "invalid_header"),
        ("short-row", ADMISSION_HEADER, lambda digest: "round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned", "loaded", "malformed_row"),
        ("long-row", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t{digest}\textra", "loaded", "malformed_row"),
        ("uppercase-digest", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t{digest.upper()}", "loaded", "bad_digest"),
        ("mixed-case-digest", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t{digest[:32]}{digest[32:].upper()}", "loaded", "bad_digest"),
        ("short-digest", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t{digest[:-1]}", "loaded", "bad_digest"),
        ("long-digest", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t{digest}0", "loaded", "bad_digest"),
        ("leading-space-digest", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t {digest}", "loaded", "bad_digest"),
        ("trailing-space-digest", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t{digest} ", "loaded", "bad_digest"),
        ("empty-digest", ADMISSION_HEADER, lambda _digest: "round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t", "loaded", "bad_digest"),
        ("nonhex-digest", ADMISSION_HEADER, lambda digest: "round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t" + "g" * 64, "loaded", "bad_digest"),
        ("unknown-status", ADMISSION_HEADER, lambda digest: f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tclassified\t{digest}", "loaded", "unknown_status"),
    ],
)
def test_malformed_sidecar_schema_and_rows_are_unknown_with_stable_warning(
    tmp_path, case, header, row_builder, status, reason
):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    digest = fasta_sha256(fastas["unit-one"])
    raw_lines = [header]
    if row_builder is not None:
        raw_lines.append(row_builder(digest))
    else:
        raw_lines.append(f"round-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t{digest}")
    write_admission(consensus, raw_lines=raw_lines)

    result = run_export(results, tmp_path / f"out-{case}")
    assert result.returncode == 0, result.stderr
    assert f"Admission provenance: {status}; classified=0 admitted_unassigned=0 unknown=1" in result.stderr
    warnings = admission_warnings(result.stderr)
    assert any(f"reason={reason}" in warning for warning in warnings)


@pytest.mark.parametrize(
    "digest_value",
    [
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "a" * 32 + "A" * 32,
        "g" * 64,
        " " + "a" * 64,
        "a" * 64 + " ",
        "",
    ],
)
def test_normalized_digest_table_rejects_noncanonical_sha256(tmp_path, digest_value):
    candidates = [
        {"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}
    ]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    write_admission(
        consensus,
        [admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"]))],
    )
    out = tmp_path / "out"
    write_stale_outputs(out)
    fake_bin = tmp_path / "fake-perl"
    fake_bin.mkdir()
    fake_perl = fake_bin / "perl"
    fake_perl.write_text(
        "#!/bin/sh\n"
        "for argument do path=$argument; done\n"
        f"printf '%s\\t%s\\n' \"$path\" '{digest_value}'\n",
        encoding="utf-8",
    )
    fake_perl.chmod(0o755)

    result = run_export(results, out, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert result.returncode == 2
    assert result.stdout == ""
    assert "malformed digest table" in result.stderr
    assert read_outputs(out) == ("stale fasta\n", "stale summary\n")
    assert not list(out.glob(".voucher_*"))


def test_provenance_digest_validation_does_not_use_awk_intervals():
    source = SCRIPT.read_text(encoding="utf-8")
    provenance_digest_logic = source[
        source.index("function load_digests") : source.index("function parse_marker")
    ]
    assert "{64}" not in provenance_digest_logic
    assert "length(fields[2]) == 64" in provenance_digest_logic
    assert "fields[2] !~ /[^0-9a-f]/" in provenance_digest_logic
    assert "length(fields[6]) == 64" in provenance_digest_logic
    assert "fields[6] !~ /[^0-9a-f]/" in provenance_digest_logic


@pytest.mark.parametrize("conflicting", [False, True])
def test_duplicate_admission_keys_are_ambiguous_even_when_identical(tmp_path, conflicting):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    digest = fasta_sha256(fastas["unit-one"])
    row = admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", digest)
    second = admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", "0" * 64) if conflicting else row
    write_admission(consensus, [row, second])

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert "admitted_unassigned=0 unknown=1" in result.stderr
    reason = "conflicting_duplicate_key" if conflicting else "duplicate_key"
    duplicate_warnings = [warning for warning in admission_warnings(result.stderr) if f"reason={reason}" in warning]
    assert len(duplicate_warnings) == 2
    assert "line=2" in duplicate_warnings[0]
    assert "line=3" in duplicate_warnings[1]


def test_orphan_admission_warns_without_inventing_a_candidate(tmp_path):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    write_admission(
        consensus,
        [admission_row("round-2", "ghost-unit", "OTU-G", "Ghost_ghost-unit", fasta_sha256(fastas["unit-one"]))],
    )

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert "admitted_unassigned=0 unknown=1" in result.stderr
    warning = admission_warnings(result.stderr)[0]
    assert "reason=orphan_evidence" in warning
    assert "unit=ghost-unit otu_key=OTU-G consensus_id=Ghost_ghost-unit" in warning
    assert "ghost-unit" not in read_outputs(tmp_path / "out")[0]


def test_primers_only_provenance_joins_source_unit_not_collapsed_sample(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path)
    identity = results / "sample_info" / "run-one" / "track_identity.tsv"
    identity.parent.mkdir(parents=True)
    identity.write_text(
        "sample_id\ttrack_id\treplicate_number\tmarker_id\tmatched_general_fasta_header\t"
        "matched_general_fasta_record_index\tsuffix_resolution_mode\tunit_suffix_current\t"
        "unit_id_collapse\tunit_id_track\tdemult_id_metadata\tlookup_key_primary\t"
        "lookup_key_fallback\tlookup_grammar_used\tmetadata_line_no\ttrack_duplicate_status\t"
        "track_duplicate_detail\ttrack_duplicate_source_metadata_lines\n"
        "alpha\tsource\t1\tITS2\t>source\t1\tmarker\tITS2\talpha_ITS2\tsource-unit\t"
        ">source\tsource\t\tGENERAL_FASTA_HEADER\t2\tunique\t\t2\n",
        encoding="utf-8",
    )
    fasta = write_fasta(
        consensus,
        "source-unit",
        [("source-unit|Only|ITS2|reads-7|OTU=OTU-1", "ACGT")],
    )
    write_taxonomy(tables, [])
    write_round_index(state_dir)
    public_bytes = (
        ">alpha|ITS2|reads-7\nACGT\n",
        SUMMARY_HEADER + "alpha\tITS2\t7\tOTU-1\t\n",
    )

    write_admission(
        consensus,
        [admission_row("round-2", "source-unit", "OTU-1", "Only_source-unit", fasta_sha256(fasta))],
    )
    head = run_export(results, tmp_path / "head", script=make_head_script(tmp_path))
    source_join = run_export(results, tmp_path / "source-join")

    assert source_join.returncode == head.returncode == 0
    assert read_outputs(tmp_path / "head") == read_outputs(tmp_path / "source-join") == public_bytes
    assert admission_info(source_join.stderr) == [
        "INFO: Admission provenance: loaded; classified=0 admitted_unassigned=1 unknown=0"
    ]
    assert admission_warnings(source_join.stderr) == []
    assert "source-unit" not in public_bytes[0] + public_bytes[1]

    for collapsed_unit in ("alpha", "alpha_ITS2"):
        write_admission(
            consensus,
            [admission_row("round-2", collapsed_unit, "OTU-1", "Only_source-unit", fasta_sha256(fasta))],
        )
        collapsed_join = run_export(results, tmp_path / f"collapsed-{collapsed_unit}")
        assert collapsed_join.returncode == 0, collapsed_join.stderr
        assert read_outputs(tmp_path / f"collapsed-{collapsed_unit}") == public_bytes
        assert admission_info(collapsed_join.stderr) == [
            "INFO: Admission provenance: loaded; classified=0 admitted_unassigned=0 unknown=1"
        ]
        assert len(admission_warnings(collapsed_join.stderr)) == 1
        assert "reason=orphan_evidence" in admission_warnings(collapsed_join.stderr)[0]


def test_admission_decoys_outside_authoritative_consensus_root_are_ignored(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path, state="state-one")
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit-one")])
    write_fasta(consensus, "unit-one", [("unit-one|Only|ITS2|reads-7|OTU=OTU-1", "ACGT")])
    write_taxonomy(tables, [])
    decoy_text = ADMISSION_HEADER + "\nround-2\tunit-one\tOTU-1\tOnly_unit-one\tunassigned\t" + "0" * 64 + "\n"
    decoys = [
        state_dir / "sequences" / "single_exp" / "Consensus" / "consensus_taxonomy_admission.tsv",
        results / "temp" / "consensus_taxonomy_admission.tsv",
        state_dir / "round-2" / "consensus_taxonomy_admission.tsv",
        results / "current" / "_state" / "consensus_taxonomy_admission.tsv",
    ]
    _, _, other_consensus, _ = make_state(tmp_path, state="state-two")
    decoys.append(other_consensus / "consensus_taxonomy_admission.tsv")
    for decoy in decoys:
        decoy.parent.mkdir(parents=True, exist_ok=True)
        decoy.write_text(decoy_text, encoding="utf-8")
    out = tmp_path / "out"

    head = run_export(results, out, "--state", "state-one", script=make_head_script(tmp_path))
    expected = read_outputs(out)
    result = run_export(results, out, "--state", "state-one")

    assert (result.returncode, result.stdout, result.stderr) == (
        head.returncode,
        head.stdout,
        head.stderr,
    )
    assert read_outputs(out) == expected
    assert "Admission provenance" not in result.stderr


def test_admission_cannot_restore_marker_discordant_candidate(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path)
    write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "bad-unit")])
    fasta = write_fasta(
        consensus,
        "bad-unit",
        [("bad-unit|Bad|COI|reads-99|OTU=OTU-X", "XXXX")],
    )
    write_taxonomy(tables, [])
    write_round_index(state_dir)
    write_admission(
        consensus,
        [admission_row("round-2", "bad-unit", "OTU-X", "Bad_bad-unit", fasta_sha256(fasta))],
    )

    result = run_export(results, tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert read_outputs(tmp_path / "out") == ("", SUMMARY_HEADER)
    assert "marker-discordant FASTA candidate" in result.stderr
    assert admission_info(result.stderr) == [
        "INFO: Admission provenance: loaded; classified=0 admitted_unassigned=0 unknown=0"
    ]
    assert admission_warnings(result.stderr) == []


def test_valid_empty_with_admission_replaces_stale_outputs(tmp_path):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    write_admission(
        consensus,
        [admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"]))],
    )
    out = tmp_path / "out"
    write_stale_outputs(out)

    result = run_export(results, out, "--sample", "missing")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert read_outputs(out) == ("", SUMMARY_HEADER)
    assert "classified=0 admitted_unassigned=0 unknown=0" in result.stderr
    assert "No voucher sequences matched" in result.stderr
    assert admission_warnings(result.stderr) == []


@pytest.mark.parametrize("conflict", ["taxonomy", "identity"])
def test_existing_fatal_conflicts_with_admission_preserve_both_outputs(tmp_path, conflict):
    results, state_dir, consensus, tables = make_state(tmp_path)
    if conflict == "identity":
        write_identity(
            results,
            "run-one",
            [
                ("alpha", "ITS2", "same-unit", "same-unit"),
                ("beta", "ITS2", "same-unit", "other-unit"),
            ],
        )
        unit = "same-unit"
    else:
        write_identity(results, "run-one", [("alpha", "ITS2", "alpha_ITS2", "unit-one")])
        unit = "unit-one"
    fasta = write_fasta(consensus, unit, [(f"{unit}|Only|ITS2|reads-7|OTU=OTU-1", "ACGT")])
    taxonomy = []
    if conflict == "taxonomy":
        taxonomy = [
            tax_row(f"Only_{unit}", "OTU-1", "ITS2", unit, "First", "species"),
            tax_row(f"Only_{unit}", "OTU-1", "ITS2", unit, "Second", "species"),
        ]
    write_taxonomy(tables, taxonomy)
    write_round_index(state_dir)
    write_admission(
        consensus,
        [admission_row("round-2", unit, "OTU-1", f"Only_{unit}", fasta_sha256(fasta))],
    )
    out = tmp_path / "out"
    write_stale_outputs(out)

    result = run_export(results, out)
    assert result.returncode == 2
    assert result.stdout == ""
    assert read_outputs(out) == ("stale fasta\n", "stale summary\n")
    assert "conflicting taxonomy" in result.stderr or "conflicting identity mapping" in result.stderr
    assert not list(out.glob(".voucher_*"))


def _make_order_fixture(root, reverse=False):
    results, state_dir, consensus, tables = make_state(root)
    write_identity(
        results,
        "run-one",
        [
            ("alpha", "ITS2", "unit-a", "unit-a"),
            ("alpha", "ITS2", "unit-b", "unit-b"),
        ],
    )
    unit_a_records = [
        ("unit-a|A|ITS2|reads-10|OTU=OTU-A", "AAAA"),
        ("unit-a|C|ITS2|reads-10|OTU=OTU-C", "CCCC"),
    ]
    if reverse:
        unit_a_records.reverse()
    paths = {
        "unit-a": write_fasta(
            consensus,
            "unit-a",
            unit_a_records,
            directory_name="z-dir" if reverse else "a-dir",
        ),
        "unit-b": write_fasta(
            consensus,
            "unit-b",
            [("unit-b|B|ITS2|reads-10|OTU=OTU-B", "BBBB")],
            directory_name="a-dir" if reverse else "z-dir",
        ),
    }
    tax_rows = [
        tax_row("A_unit-a", "OTU-A", "ITS2", "unit-a", "Alpha", "one"),
        tax_row("B_unit-b", "OTU-B", "ITS2", "unit-b", "Beta", "two"),
        tax_row("C_unit-a", "OTU-C", "ITS2", "unit-a", "Gamma", "three"),
    ]
    write_taxonomy(tables, list(reversed(tax_rows)) if reverse else tax_rows)
    write_round_index(state_dir)
    rows = [
        admission_row("round-2", "unit-a", "OTU-A", "A_unit-a", fasta_sha256(paths["unit-a"])),
        admission_row("round-2", "unit-b", "OTU-B", "B_unit-b", fasta_sha256(paths["unit-b"])),
        admission_row("round-2", "unit-a", "OTU-C", "C_unit-a", fasta_sha256(paths["unit-a"])),
    ]
    write_admission(consensus, list(reversed(rows)) if reverse else rows)
    return results


def test_input_order_file_discovery_and_locale_do_not_change_outputs_or_diagnostics(tmp_path):
    first_results = _make_order_fixture(tmp_path / "first", reverse=False)
    second_results = _make_order_fixture(tmp_path / "second", reverse=True)
    locale_output = subprocess.run(["locale", "-a"], text=True, capture_output=True, check=False).stdout.splitlines()
    alternate_locale = next((value for value in locale_output if value not in {"C", "POSIX"}), "C")

    first = run_export(first_results, tmp_path / "out-first", env={"LC_ALL": "C"})
    second = run_export(second_results, tmp_path / "out-second", env={"LC_ALL": alternate_locale})
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout == ""
    assert read_outputs(tmp_path / "out-first") == read_outputs(tmp_path / "out-second") == (
        ">alpha|ITS2|reads-10|BLAST:Alpha_one\nAAAA\n",
        SUMMARY_HEADER + "alpha\tITS2\t10\tOTU-A\tAlpha_one\n",
    )
    assert admission_info(first.stderr) == admission_info(second.stderr) == [
        "INFO: Admission provenance: loaded; classified=3 admitted_unassigned=0 unknown=0"
    ]
    assert admission_warnings(first.stderr) == admission_warnings(second.stderr) == []


def test_admission_preserves_spaces_and_assignment_shaped_relative_results(tmp_path):
    results, state_dir, consensus, tables = make_state(tmp_path / "run=1")
    write_identity(results, "run-one", [("sample alpha", "ITS2", "alpha_ITS2", "unit one")])
    fasta = write_fasta(consensus, "unit one", [("unit one|Only|ITS2|reads-7|OTU=OTU-1", "ACGT")])
    write_taxonomy(tables, [])
    write_round_index(state_dir)
    write_admission(
        consensus,
        [admission_row("round-2", "unit one", "OTU-1", "Only_unit one", fasta_sha256(fasta))],
    )
    out = tmp_path / "output with spaces"

    plain = run_export("run=1/results with spaces", out, cwd=tmp_path)
    plain_outputs = read_outputs(out)
    controlled = run_export("./run=1/results with spaces", out, cwd=tmp_path)

    assert plain.returncode == controlled.returncode == 0
    assert plain.stdout == controlled.stdout == ""
    assert plain.stderr == controlled.stderr
    assert read_outputs(out) == plain_outputs == (
        ">sample alpha|ITS2|reads-7\nACGT\n",
        SUMMARY_HEADER + "sample alpha\tITS2\t7\tOTU-1\t\n",
    )
    assert "admitted_unassigned=1" in plain.stderr


@pytest.mark.parametrize("failure", ["digest", "normalization", "admission-mktemp"])
def test_private_provenance_tool_failures_preserve_outputs_and_clean_temporaries(tmp_path, failure):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    write_admission(
        consensus,
        [admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"]))],
    )
    out = tmp_path / "out"
    write_stale_outputs(out)
    fake_bin = tmp_path / f"fake-{failure}"
    fake_bin.mkdir()
    if failure == "digest":
        wrapper = fake_bin / "perl"
        wrapper.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
    elif failure == "normalization":
        wrapper = fake_bin / "awk"
        wrapper.write_text(
            "#!/bin/sh\ncase \"$*\" in *round_file=*) exit 92;; esac\nexec /usr/bin/awk \"$@\"\n",
            encoding="utf-8",
        )
    else:
        wrapper = fake_bin / "mktemp"
        wrapper.write_text(
            "#!/bin/sh\ncase \"$1\" in *.voucher_admission.*) exit 93;; esac\nexec /usr/bin/mktemp \"$@\"\n",
            encoding="utf-8",
        )
    wrapper.chmod(0o755)

    result = run_export(results, out, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert result.returncode == 2
    assert result.stdout == ""
    assert read_outputs(out) == ("stale fasta\n", "stale summary\n")
    assert not list(out.glob(".voucher_*"))


def test_invalid_evidence_temporary_write_failure_is_fatal_before_publication(tmp_path):
    candidates = [
        {"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}
    ]
    results, _, consensus, _, _ = make_admission_fixture(tmp_path, candidates)
    (consensus / "consensus_taxonomy_admission.tsv").mkdir()
    out = tmp_path / "out"
    write_stale_outputs(out)
    fake_bin = tmp_path / "fake-write"
    fake_bin.mkdir()
    fake_mktemp = fake_bin / "mktemp"
    fake_mktemp.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  *.voucher_admission.*) blocked=${1%XXXXXX}blocked; /bin/ln -s . \"$blocked\"; printf '%s\\n' \"$blocked\"; exit 0;;\n"
        "esac\n"
        "exec /usr/bin/mktemp \"$@\"\n",
        encoding="utf-8",
    )
    fake_mktemp.chmod(0o755)

    result = run_export(results, out, env={"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Could not normalize admission provenance" in result.stderr
    assert read_outputs(out) == ("stale fasta\n", "stale summary\n")
    assert not list(out.glob(".voucher_*"))


def test_second_publication_failure_with_admission_matches_head_partial_publication(tmp_path):
    candidates = [{"unit": "unit-one", "sample": "alpha", "name": "Only", "otu": "OTU-1", "reads": 7, "sequence": "ACGT"}]
    results, _, consensus, _, fastas = make_admission_fixture(tmp_path, candidates)
    write_admission(
        consensus,
        [admission_row("round-2", "unit-one", "OTU-1", "Only_unit-one", fasta_sha256(fastas["unit-one"]))],
    )
    out = tmp_path / "out"
    write_stale_outputs(out)
    fake_bin = tmp_path / "fake-mv"
    fake_bin.mkdir()
    wrapper = fake_bin / "mv"
    wrapper.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in -f) source=$2;; *) source=$1;; esac\n"
        "case \"$source\" in *.voucher_summary.tsv.*) exit 94;; esac\n"
        "exec /bin/mv \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    env = {"PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = run_export(results, out, env=env)
    candidate_snapshot = output_snapshot(out)
    write_stale_outputs(out)
    head = run_export(results, out, script=make_head_script(tmp_path), env=env)

    assert (result.returncode, result.stdout, result.stderr) == (
        head.returncode,
        head.stdout,
        head.stderr,
    )
    assert result.returncode == 94
    assert candidate_snapshot == output_snapshot(out) == (
        ("voucher_sequences.fasta", ("file", b">alpha|ITS2|reads-7\nACGT\n")),
        ("voucher_summary.tsv", ("file", b"stale summary\n")),
    )
