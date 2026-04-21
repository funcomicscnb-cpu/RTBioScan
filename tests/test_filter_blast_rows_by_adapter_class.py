import subprocess
import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "filter_blast_rows_by_adapter_class.sh"
PARTITION_SCRIPT = REPO_ROOT / "bin" / "partition_blast_rows_by_adapter_class.sh"


def _write_report(tmp_path: Path, rows: str) -> Path:
    report = tmp_path / "blast_report.txt"
    report.write_text(rows, encoding="utf-8")
    return report


def _write_samples(tmp_path: Path, rows: str) -> Path:
    samples = tmp_path / "samples.txt"
    samples.write_text(rows, encoding="utf-8")
    return samples


def test_filters_no_adapter_rows_by_shared_contract(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\tNA\n"
        "r2|COI|sup|barcode=bc1|adapter=no_adapter_1a\tNA\n"
        "r3|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n",
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(report), "no_adapter"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "r1|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\tNA\n"


def test_filters_mixed_case_no_adapter_rows_by_shared_contract(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=no_adapter_1|adapter=NO_ADAPTER_1\tNA\n"
        "r2|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n",
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(report), "no_adapter"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "r1|COI|sup|barcode=no_adapter_1|adapter=NO_ADAPTER_1\tNA\n"


def test_filters_sample_rows_by_normalized_sample_name(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n"
        "r2|COI|sup|barcode=bc1|adapter=sample_A_9\tNA\n"
        "r3|COI|sup|barcode=bc1|adapter=sample_B_1\tNA\n"
        "r4|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\tNA\n",
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(report), "sample", "sample_A"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "r1|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n"
        "r2|COI|sup|barcode=bc1|adapter=sample_A_9\tNA\n"
    )


def test_filters_mid_field_replicate_rows_by_base_sample(tmp_path: Path) -> None:
    """All three replicates of AD1_AF_AB_*_MPnew_COI2 must match the base sample name."""
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=b|adapter=AD1_AF_AB_1_MPnew_COI2\tNA\n"
        "r2|COI|sup|barcode=b|adapter=AD1_AF_AB_2_MPnew_COI2\tNA\n"
        "r3|COI|sup|barcode=b|adapter=AD1_AF_AB_3_MPnew_COI2\tNA\n"
        "r4|ITS2|sup|barcode=b|adapter=AD1_AF_AB_1_MPnew_ITS2\tNA\n",
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(report), "sample", "AD1_AF_AB_MPnew"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        "r1|COI|sup|barcode=b|adapter=AD1_AF_AB_1_MPnew_COI2\tNA\n"
        "r2|COI|sup|barcode=b|adapter=AD1_AF_AB_2_MPnew_COI2\tNA\n"
        "r3|COI|sup|barcode=b|adapter=AD1_AF_AB_3_MPnew_COI2\tNA\n"
        "r4|ITS2|sup|barcode=b|adapter=AD1_AF_AB_1_MPnew_ITS2\tNA\n"
    )


def test_filters_no_adapter_sample_name_via_shared_matcher(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\tNA\n"
        "r2|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n",
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), str(report), "sample", "no_adapter"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "r1|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\tNA\n"


def test_partition_helper_matches_per_sample_filter_outputs(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n"
        "r2|COI|sup|barcode=bc1|adapter=sample_A_9\tNA\n"
        "r3|COI|sup|barcode=bc1|adapter=sample_B_1\tNA\n"
        "r4|COI|sup|barcode=no_adapter_1|adapter=no_adapter_1\tNA\n"
        "r5|ITS2|sup|barcode=b|adapter=AD1_AF_AB_2_MPnew_COI2\tNA\n",
    )
    samples = _write_samples(
        tmp_path,
        "sample_A\n"
        "sample_B\n"
        "no_adapter\n"
        "AD1_AF_AB_MPnew\n",
    )
    out_dir = tmp_path / "partitioned"
    result = subprocess.run(
        ["bash", str(PARTITION_SCRIPT), str(report), str(samples), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr

    for sample in ("sample_A", "sample_B", "no_adapter", "AD1_AF_AB_MPnew"):
        expected = subprocess.run(
            ["bash", str(SCRIPT), str(report), "sample", sample],
            capture_output=True,
            text=True,
            check=False,
        )
        assert expected.returncode == 0, expected.stderr
        actual_path = out_dir / f"{sample}.blast.tsv"
        assert actual_path.exists()
        assert actual_path.read_text(encoding="utf-8") == expected.stdout


def test_partition_helper_precreates_empty_sample_outputs(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n",
    )
    samples = _write_samples(
        tmp_path,
        "sample_A\n"
        "sample_B\n",
    )
    out_dir = tmp_path / "partitioned"
    result = subprocess.run(
        ["bash", str(PARTITION_SCRIPT), str(report), str(samples), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (out_dir / "sample_A.blast.tsv").read_text(encoding="utf-8") == (
        "r1|COI|sup|barcode=bc1|adapter=sample_A_2\tNA\n"
    )
    assert (out_dir / "sample_B.blast.tsv").read_text(encoding="utf-8") == ""


def test_track_partition_helper_rejects_marker_unit_mismatch(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "read_id\totu_id\totu_kingdom\tbarcode_by_homology\n"
        "r1|COI|sup|barcode=COI|adapter=sample_A_1_MPold1_ITS2|OTUB_1-COI\tOTUB_1\tMetazoa\tCOI\n",
    )
    samples = _write_samples(tmp_path, "sample_A_1_MPold1_ITS2\n")
    track_identity = tmp_path / "track_identity.tsv"
    track_identity.write_text(
        "sample_id\ttrack_id\treplicate_number\tmarker_id\tmatched_general_fasta_header\tmatched_general_fasta_record_index\t"
        "suffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\tunit_id_track\t"
        "demult_id_metadata\tlookup_key_primary\tlookup_key_fallback\tlookup_grammar_used\t"
        "metadata_line_no\ttrack_duplicate_status\ttrack_duplicate_detail\ttrack_duplicate_source_metadata_lines\n"
        "sample_A\tsample_A_1_MPold1\t1\tITS2\tA1\t1\tmarker\tITS2\tsample_A_ITS2\tsample_A_1_MPold1_ITS2\t"
        ">A1\tA1\t1_A1\tWELL_PLATE\t2\tunique\t\t\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "partitioned"
    result = subprocess.run(
        ["bash", str(PARTITION_SCRIPT), str(report), str(samples), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "track",
            "RTBIOSCAN_TRACK_ACTIVE_UNITS": str(samples),
            "RTBIOSCAN_TRACK_IDENTITY_TSV": str(track_identity),
        },
    )

    assert result.returncode != 0
    assert "marker/unit mismatch" in result.stderr


def test_track_partition_helper_rejects_unknown_adapter_in_identity_map(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "r1|COI|sup|barcode=COI|adapter=sample_A_1_MPold1_COI|OTUB_1-COI\tNA\n",
    )
    samples = _write_samples(tmp_path, "sample_A_1_MPold1_COI\n")
    track_active_units = tmp_path / "track_active_units.txt"
    track_active_units.write_text("sample_A_1_MPold1_COI\n", encoding="utf-8")
    track_identity = tmp_path / "track_identity.tsv"
    track_identity.write_text(
        "sample_id\ttrack_id\treplicate_number\tmarker_id\tmatched_general_fasta_header\tmatched_general_fasta_record_index\t"
        "suffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\tunit_id_track\t"
        "demult_id_metadata\tlookup_key_primary\tlookup_key_fallback\tlookup_grammar_used\t"
        "metadata_line_no\ttrack_duplicate_status\ttrack_duplicate_detail\ttrack_duplicate_source_metadata_lines\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "partitioned"
    result = subprocess.run(
        ["bash", str(PARTITION_SCRIPT), str(report), str(samples), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "track",
            "RTBIOSCAN_TRACK_ACTIVE_UNITS": str(track_active_units),
            "RTBIOSCAN_TRACK_IDENTITY_TSV": str(track_identity),
        },
    )

    assert result.returncode != 0
    assert "track_identity.tsv" in result.stderr


def test_track_partition_helper_ignores_placeholder_header_marker_column_and_uses_read_id(tmp_path: Path) -> None:
    report = _write_report(
        tmp_path,
        "read_id\totu_id\totu_kingdom\tbarcode_by_homology\n"
        "00126d53-45a3-45ec-8e47-e477d755f517|ITS2|hac2sup|barcode=|adapter=TH500_1_MPold1_ITS2|OTUB_287-ITS2\t"
        "4557\tViridiplantae\tStreptophyta\tMagnoliopsida\tPoales\tPoaceae\tSorghum\t\n",
    )
    samples = _write_samples(tmp_path, "TH500_1_MPold1_ITS2\n")
    track_identity = tmp_path / "track_identity.tsv"
    track_identity.write_text(
        "sample_id\ttrack_id\treplicate_number\tmarker_id\tmatched_general_fasta_header\tmatched_general_fasta_record_index\t"
        "suffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\tunit_id_track\t"
        "demult_id_metadata\tlookup_key_primary\tlookup_key_fallback\tlookup_grammar_used\t"
        "metadata_line_no\ttrack_duplicate_status\ttrack_duplicate_detail\ttrack_duplicate_source_metadata_lines\n"
        "TH500\tTH500_1_MPold1\t1\tITS2\tA1\t1\tmarker\tITS2\tTH500_ITS2\tTH500_1_MPold1_ITS2\t"
        ">A1\tA1\t1_A1\tWELL_PLATE\t2\tunique\t\t\n",
        encoding="utf-8",
    )
    out_dir = tmp_path / "partitioned"
    result = subprocess.run(
        ["bash", str(PARTITION_SCRIPT), str(report), str(samples), str(out_dir)],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "track",
            "RTBIOSCAN_TARGET_TOKENS": "COI|ITS2",
            "RTBIOSCAN_TRACK_ACTIVE_UNITS": str(samples),
            "RTBIOSCAN_TRACK_IDENTITY_TSV": str(track_identity),
        },
    )

    assert result.returncode == 0, result.stderr
    assert (out_dir / "TH500_1_MPold1_ITS2.blast.tsv").read_text(encoding="utf-8").splitlines() == [
        "00126d53-45a3-45ec-8e47-e477d755f517|ITS2|hac2sup|barcode=|adapter=TH500_1_MPold1_ITS2|OTUB_287-ITS2\t4557\tViridiplantae\tStreptophyta\tMagnoliopsida\tPoales\tPoaceae\tSorghum\t",
    ]
