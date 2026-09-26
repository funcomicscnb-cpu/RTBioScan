import subprocess
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "detect_consensus_sample_mode.sh"


def _run(tmp_path: Path, blast_rows: str, default_samples: str = "", env: dict[str, str] | None = None):
    blast = tmp_path / "blast_report_annotated.txt"
    samples_out = tmp_path / "samples.txt"
    blast.write_text(blast_rows, encoding="utf-8")
    cmd = ["bash", str(SCRIPT), str(blast), str(samples_out)]
    if default_samples:
        default = tmp_path / "input_samples.txt"
        default.write_text(default_samples, encoding="utf-8")
        cmd.append(str(default))
    run_env = None
    if env:
        run_env = {**os.environ, **env}
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=run_env)
    return result, samples_out


def test_invalid_invocation_remains_nonzero_with_usage_on_stderr(tmp_path: Path) -> None:
    result = subprocess.run(["bash", str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert result.stdout == ""
    assert (
        result.stderr.strip()
        == f"usage: {SCRIPT} <blast_report_annotated.txt> <samples_out> [default_samples]"
    )


def test_detects_barcoded_samples_from_actual_adapter_names(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=barcode_1|adapter=sample_A_1|OTUB_1-COI\tNA\n"
        "read2|COI|hac2sup|barcode=barcode_2|adapter=sample_B_2|OTUB_2-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tbarcoded" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["sample_A", "sample_B"]


def test_collapse_identity_map_resolves_units_exactly(tmp_path: Path) -> None:
    identity = tmp_path / "replicate_identity.tsv"
    identity.write_text(
        "sample_id\tmarker_id\tsuffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\n"
        "Plot_3_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\n"
        "Plot_4_North\tCOI\tmarker\tCOI\tPlot_4_North_COI\n",
        encoding="utf-8",
    )
    rows = (
        "tag1|COI|hac2sup|adapter=Plot_3_North_COI|OTUB_1-COI\tNA\n"
        "tag2|COI|hac2sup|adapter=Plot_4_North_COI|OTUB_1-COI\tNA\n"
    )
    env = {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}
    result, samples_out = _run(tmp_path, rows, env=env)
    assert result.returncode == 0, result.stderr
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["Plot_3_North", "Plot_4_North"]


def test_collapse_present_map_refuses_missing_unit_before_samples_publication(tmp_path: Path) -> None:
    identity = tmp_path / "replicate_identity.tsv"
    identity.write_text(
        "sample_id\tmarker_id\tsuffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\n"
        "Plot_3_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\n",
        encoding="utf-8",
    )
    result, samples_out = _run(
        tmp_path,
        "tag1|COI|hac2sup|adapter=Plot_4_North_COI|OTUB_1-COI\tNA\n",
        env={"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)},
    )
    assert result.returncode != 0
    assert "no entry" in result.stderr
    assert not samples_out.exists()


def test_collapse_configured_identity_path_cannot_silently_become_legacy(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "tag1|COI|hac2sup|adapter=sample_A_COI|OTUB_1-COI\tNA\n",
        env={"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(tmp_path / "missing.tsv")},
    )
    assert result.returncode != 0
    assert "missing, empty, or unreadable" in result.stderr
    assert not samples_out.exists()


def test_defaults_to_no_adapter_when_only_no_adapter_reads_are_present(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tno_adapter_only" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["no_adapter"]


def test_mixed_case_no_adapter_rows_are_treated_as_no_adapter(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=no_adapter_1|adapter=NO_ADAPTER_1|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tno_adapter_only" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["no_adapter"]


def test_falls_back_to_default_samples_when_current_round_has_no_barcoded_rows(tmp_path: Path) -> None:
    result, samples_out = _run(tmp_path, "", "sample_A_1\nsample_B_2\n")

    assert result.returncode == 0, result.stderr
    assert "mode\tbarcoded_fallback" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["sample_A", "sample_B"]


def test_mapped_defaults_use_exact_sample_ids_and_reach_consensus_partition(tmp_path: Path) -> None:
    identity = tmp_path / "replicate_identity.tsv"
    identity.write_text(
        "sample_id\tmarker_id\tsuffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\n"
        "Plot_3_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\n"
        "Plot_4_North\tCOI\tmarker\tCOI\tPlot_4_North_COI\n",
        encoding="utf-8",
    )
    roster = (
        "Plot_4_North P4N_1 1 A1 P1 runA >A1_P1\n"
        "Plot_3_North P3N_1 1 A2 P1 runA >A2_P1\n"
        "Plot_3_North P3N_2 2 A3 P1 runA >A3_P1\n"
    )
    env = {
        "RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
        "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity),
        "RTBIOSCAN_TARGET_TOKENS": "COI",
        "RTBIOSCAN_TARGET_TAXA": "Metazoa",
        "CONSENSUS_TAXONOMY_MODE": "allow_unassigned",
    }
    result, samples_out = _run(
        tmp_path, "read_id\totu_id\totu_kingdom\tbarcode_by_homology\n", roster, env
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[:2] == [
        "mode\tbarcoded_fallback", "source\tdefault_samples"
    ]
    assert samples_out.read_text(encoding="utf-8") == "Plot_3_North\nPlot_4_North\n"

    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(
        ">cached|COI|hac|barcode=COI|adapter=Plot_3_North_COI\nACGT\n",
        encoding="utf-8",
    )
    (tmp_path / "read_qscore.tsv").write_text("read_id\tqscore\n", encoding="utf-8")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("seqkit", "seqtk", "vsearch", "Rscript"):
        stub = stubs / name
        stub.write_text("#!/bin/sh\nexit 93\n", encoding="utf-8")
        stub.chmod(0o755)
    consensus = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "bin" / "Consensus_simple.sh"),
         str(REPO_ROOT / "bin"), "98", "1", "10", "0", "0", "", "representative", "4"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
        env={**os.environ, **env, "PATH": f"{stubs}:{os.environ['PATH']}"},
    )
    assert consensus.returncode == 0, consensus.stderr
    assert "refusing fallback" not in consensus.stderr
    partitions = tmp_path / "Consensus" / "_sample_partitions"
    assert {path.name for path in partitions.glob("*.blast.tsv")} == {
        "Plot_3_North.blast.tsv", "Plot_4_North.blast.tsv"
    }
    assert all(not path.read_bytes() for path in partitions.glob("*.blast.tsv"))


def test_mapped_defaults_refuse_malformed_unsafe_and_unmapped_rows_before_output(tmp_path: Path) -> None:
    identity = tmp_path / "replicate_identity.tsv"
    identity.write_text(
        "sample_id\tmarker_id\tsuffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\n"
        "Plot_3_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\n",
        encoding="utf-8",
    )
    env = {"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
           "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}
    valid = "Plot_3_North P3N_1 1 A1 P1 runA >A1_P1\n"
    for index, bad in enumerate((
        "Plot_3_North P3N_1 1 A1 P1 runA\n",
        "Plot_3_North P3N_1 1 A1 P1 runA >A1_P1 extra\n",
        "\n",
        "Plot_3_North P3N_1 1 A1 P1 runA bad/path\n",
        "Plot_3_North\tP3N_1 1 A1 P1 runA P3N_1\n",
        "Plot_4_North P4N_1 1 A1 P1 runA >A1_P1\n",
    )):
        case = tmp_path / f"case_{index}"
        case.mkdir()
        sentinel = case / "samples.txt"
        sentinel.write_text("preexisting\n", encoding="utf-8")
        result, samples_out = _run(case, "read_id\totu_id\n", valid + bad, env)
        assert result.returncode != 0, (index, bad, result.stderr)
        assert samples_out.read_text(encoding="utf-8") == "preexisting\n"
        assert not list(case.glob("samples.txt.*.tmp.*"))


def test_observed_no_adapter_takes_priority_over_default_samples(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\tNA\n",
        "sample_A_1\nsample_B_2\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tno_adapter_only" in result.stdout
    assert "source\tobserved_no_adapter" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["no_adapter"]


def test_ignores_adapter_tokens_outside_read_id_column(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\tcomment adapter=sample_A_1\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tno_adapter_only" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["no_adapter"]


def test_skips_empty_normalized_sample_values(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "",
        "_1\nsample_A_2\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tbarcoded_fallback" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["sample_A"]


def test_trims_whitespace_around_default_samples(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "",
        "  sample_A_1  \n\t sample_B_2\t\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "mode\tbarcoded_fallback",
        "source\tdefault_samples",
        "observed_no_adapter\t0",
    ]
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["sample_A", "sample_B"]


def test_mid_field_replicates_collapse_to_single_base_sample(tmp_path: Path) -> None:
    """AD1_AF_AB_1_MPnew_COI2, _2_, _3_ must all collapse to AD1_AF_AB_MPnew."""
    result, samples_out = _run(
        tmp_path,
        "r1|COI|sup|barcode=b|adapter=AD1_AF_AB_1_MPnew_COI2|OTUB_1-COI\tNA\n"
        "r2|COI|sup|barcode=b|adapter=AD1_AF_AB_2_MPnew_COI2|OTUB_1-COI\tNA\n"
        "r3|COI|sup|barcode=b|adapter=AD1_AF_AB_3_MPnew_COI2|OTUB_1-COI\tNA\n"
        "r4|ITS2|sup|barcode=b|adapter=AD1_AF_AB_1_MPnew_ITS2|OTUB_2-ITS2\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tbarcoded" in result.stdout
    lines = samples_out.read_text(encoding="utf-8").splitlines()
    assert lines == ["AD1_AF_AB_MPnew"]


def test_barcoded_plus_no_adapter_includes_no_adapter_in_samples(tmp_path: Path) -> None:
    """When both barcoded and no_adapter reads are present, no_adapter must appear in samples.txt."""
    result, samples_out = _run(
        tmp_path,
        "r1|COI|sup|barcode=b1|adapter=sample_A_1|OTUB_1-COI\tNA\n"
        "r2|ITS2|hac|barcode=|adapter=no_adapter|OTUB_2-ITS2\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tbarcoded" in result.stdout
    assert "observed_no_adapter\t1" in result.stdout
    lines = samples_out.read_text(encoding="utf-8").splitlines()
    assert "sample_A" in lines
    assert "no_adapter" in lines


def test_stdout_lines_are_exact_and_ordered(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "r1|COI|sup|barcode=b1|adapter=sample_A_1|OTUB_1-COI\tNA\n"
        "r2|ITS2|hac|barcode=|adapter=no_adapter|OTUB_2-ITS2\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "mode\tbarcoded",
        "source\tobserved_adapters",
        "observed_no_adapter\t1",
    ]
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["sample_A", "no_adapter"]


def test_ignores_observed_adapter_values_that_normalize_to_empty(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=barcode_1|adapter=_1|OTUB_1-COI\tNA\n",
    )

    assert result.returncode == 0, result.stderr
    assert "mode\tno_adapter_only" in result.stdout
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["no_adapter"]


def test_ignores_missing_or_malformed_adapter_tokens(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=barcode_1|OTUB_1-COI\tNA\n"
        "read2|COI|hac2sup|barcode=barcode_2|adapter=|OTUB_2-COI\tNA\n",
        "sample_B_2\n",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "mode\tbarcoded_fallback",
        "source\tdefault_samples",
        "observed_no_adapter\t0",
    ]
    assert samples_out.read_text(encoding="utf-8").splitlines() == ["sample_B"]


def test_track_mode_preserves_exact_track_units_and_appends_observed_no_adapter(tmp_path: Path) -> None:
    result, samples_out = _run(
        tmp_path,
        "read1|COI|hac2sup|barcode=COI|adapter=sample_A_1_MPold1_COI|OTUB_1-COI\tNA\n"
        "read2|ITS2|hac2sup|barcode=ITS2|adapter=no_adapter_1|OTUB_2-ITS2\tNA\n",
        "sample_A_1_MPold1_COI\nsample_A_1_MPold1_ITS2\n",
        env={"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "track"},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "mode\tbarcoded",
        "source\tdefault_samples",
        "observed_no_adapter\t1",
    ]
    assert samples_out.read_text(encoding="utf-8").splitlines() == [
        "sample_A_1_MPold1_COI",
        "sample_A_1_MPold1_ITS2",
        "no_adapter",
    ]


def test_mapped_defaults_accept_producer_permitted_compatibility_delimiters(tmp_path: Path) -> None:
    identity = tmp_path / "identity.tsv"
    identity.write_text(
        "sample_id\tmarker_id\tsuffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\n"
        "Plot_3_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\n"
        "Plot_4_North\tCOI\tmarker\tCOI\tPlot_4_North_COI\n", encoding="utf-8")
    env = {"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
           "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}
    roster = ("Plot_4_North P4N_1 rep/2 W|2 P/2 run|A >W|2_P/2\n"
              "Plot_3_North P3N_1 rep|1 W/1 P|1 run/A >W/1_P|1\n")
    result, output = _run(tmp_path, "read_id\totu_id\n", roster, env)
    assert result.returncode == 0, result.stderr
    assert output.read_text() == "Plot_3_North\nPlot_4_North\n"

    for index, bad in enumerate((
        "Plot_3_North P|3 1 A1 P1 runA >A1_P1\n",
        "Plot_3_North P/3 1 A1 P1 runA >A1_P1\n",
        "Plot_3_North no_adapter 1 A1 P1 runA >A1_P1\n",
        "Plot_3_North no_adapter_1 1 A1 P1 runA >A1_P1\n",
        "Plot_3_North P3 1 A_1 P1 runA >A_1_P1\n",
        "Plot_3_North P3 1 A1 P1 runA >A1_P2\n",
        "Plot_3_North P3 1 A1 P1 runA\n",
        "Plot_3_North P3 1 A1 P1 runA >A1_P1 extra\n",
        "Plot_3_North P3 1 A1 P1 runA >A1_P1\r\n",
        "Plot_3_North\tP3 1 A1 P1 runA >A1_P1\n",
        "Plot_3_North/Bad P3 1 A1 P1 runA >A1_P1\n",
    )):
        case = tmp_path / f"invalid_{index}"
        case.mkdir()
        sentinel = case / "samples.txt"
        sentinel.write_bytes(b"existing\n")
        result, output = _run(case, "read_id\totu_id\n", bad, env)
        assert result.returncode != 0, (index, result.stderr)
        assert output.read_bytes() == b"existing\n"
