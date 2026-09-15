from pathlib import Path
import os
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("RTBIOSCAN_VOUCHER_EXPORT_SCRIPT", ROOT / "bin" / "voucher_export.sh"))
SUMMARY_HEADER = "sample\tmarker\treads\totu_key\tblast_suggestion\n"
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


def run_export(results, out, *args, cwd=None):
    return subprocess.run(
        ["bash", str(SCRIPT), "--results", str(results), "--out", str(out), *args],
        text=True,
        capture_output=True,
        check=False,
        cwd=cwd,
    )


def read_outputs(out):
    fasta = (out / "voucher_sequences.fasta").read_text(encoding="utf-8")
    summary = (out / "voucher_summary.tsv").read_text(encoding="utf-8")
    return fasta, summary


def write_stale_outputs(out):
    out.mkdir(parents=True, exist_ok=True)
    (out / "voucher_sequences.fasta").write_text("stale fasta\n", encoding="utf-8")
    (out / "voucher_summary.tsv").write_text("stale summary\n", encoding="utf-8")


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
