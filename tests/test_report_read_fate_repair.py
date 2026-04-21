import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "report_read_fate_repair.py"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


DEMULT_HEADER = (
    "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t"
    "sampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n"
)
BLAST_HEADER = (
    "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\t"
    "aln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\t"
    "otu_order\totu_family\totu_genus\totu_species\n"
)


def write_repair_round(state_dir: Path, round_num: int, read_ids: list[str]) -> Path:
    round_dir = state_dir / f"output_round_{round_num}"
    round_json = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": state_dir.name,
        "round_barcode": f"output_round_{round_num}",
    }
    write_text(round_dir / "round_report.json", json.dumps(round_json) + "\n")
    demult_rows = "".join(
        f"{rid}\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n"
        for rid in read_ids
    )
    blast_rows = "".join(
        f"{rid}\tCOI\thac\tsample_A_1\thit_{rid}\t123\t100\t99\tOTUB_{idx}-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS\n"
        for idx, rid in enumerate(read_ids, start=1)
    )
    write_text(round_dir / "RTBioScan_demult_rpt.txt", DEMULT_HEADER + demult_rows)
    write_text(round_dir / "RTBioScan_blast_otu_pretax_rpt.txt", BLAST_HEADER + blast_rows)
    return round_dir


def test_report_read_fate_repair_rebuilds_state_sidecars(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    round1 = state_dir / "output_round_1"
    round2 = state_dir / "output_round_2"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round1.mkdir(parents=True, exist_ok=True)
    round2.mkdir(parents=True, exist_ok=True)

    round_json_base = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "warnings": [
            "missing_or_empty:/tmp/RTBioScan_otu_lock_summary.tsv",
            "otu_fate_universe_empty:strict_round",
            "missing_or_empty:/tmp/otu_members_blastdiag_stats.tsv",
            "missing_or_empty:/tmp/RTBioScan_otu_size_streak.tsv",
            "size_streak_inputs_missing:RTBioScan_otu_sizes_round.tsv",
            "missing_or_empty_data_rows:/tmp/consensus_round_provenance.tsv",
            "keep_me:still_relevant",
        ],
    }
    write_text(
        round1 / "round_report.json",
        json.dumps({**round_json_base, "round_barcode": "output_round_1"}) + "\n",
    )
    write_text(
        round2 / "round_report.json",
        json.dumps({**round_json_base, "round_barcode": "output_round_2"}) + "\n",
    )

    demult_header = (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t"
        "sampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n"
    )
    blast_header = (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\t"
        "aln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\t"
        "otu_order\totu_family\totu_genus\totu_species\n"
    )

    write_text(
        round1 / "RTBioScan_demult_rpt.txt",
        demult_header + "r1\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n",
    )
    write_text(
        round2 / "RTBioScan_demult_rpt.txt",
        demult_header
        + "r1\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n"
        + "r2\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n",
    )
    write_text(
        round1 / "RTBioScan_blast_otu_pretax_rpt.txt",
        blast_header + "r1\tCOI\thac\tsample_A_1\thit1\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS\n",
    )
    write_text(
        round2 / "RTBioScan_blast_otu_pretax_rpt.txt",
        blast_header
        + "r1\tCOI\thac\tsample_A_1\thit1\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS\n"
        + "r2\tCOI\thac\tsample_A_1\thit2\t456\t100\t98\tOTUB_2-COI\t222\tMetazoa\tP\tC\tO\tF\tG\tS2\n",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
            "--skip-render",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    round1_demux = (round1 / "RTBioScan_read_fate_demult_first_seen.tsv").read_text(encoding="utf-8").splitlines()
    round2_demux = (round2 / "RTBioScan_read_fate_demult_first_seen.tsv").read_text(encoding="utf-8").splitlines()
    assert len([line for line in round1_demux if line.strip()]) == 2
    assert len([line for line in round2_demux if line.strip()]) == 2
    assert round2_demux[1].startswith("r2\t")

    demux_seen = (state_state_dir / "RTBioScan_read_fate_demux_seen.tsv").read_text(encoding="utf-8").splitlines()
    blast_seen = (state_state_dir / "RTBioScan_read_fate_blast_seen.tsv").read_text(encoding="utf-8").splitlines()
    demux_cache = (state_state_dir / "RTBioScan_demux_annotation_cache.tsv").read_text(encoding="utf-8").splitlines()
    assert demux_seen == ["r1", "r2"]
    assert blast_seen == ["r1", "r2"]
    assert len([line for line in demux_cache if line.strip()]) == 3
    assert demux_cache[1].startswith("r1\t")
    assert demux_cache[2].startswith("r2\t")

    history_lines = [line for line in (state_state_dir / "report_history.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(history_lines) == 2
    round2_obj = json.loads(history_lines[1])
    assert round2_obj["read_fate"]["demux_total_reads"] == 1
    assert round2_obj["read_fate"]["blast_seen_reads"] == 1
    assert round2_obj["warnings"] == ["keep_me:still_relevant"]


def test_report_read_fate_repair_filters_rounds_without_feeder_sidecar(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round5 = write_repair_round(state_dir, 5, ["r1"])
    round6 = write_repair_round(state_dir, 6, ["r1", "r2"])
    feeder_meta = tmp_path / "results" / "pod5" / "runA" / "metadata"
    write_text(feeder_meta / "runA_6_slice.tsv", "# slice_fingerprint=abc\n")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
            "--valid-rounds-from-feeder-metadata",
            str(feeder_meta),
            "--skip-render",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    assert not (round5 / "round_report.json").exists()
    assert (round5 / "round_report.json.orphan").exists()
    assert (round6 / "round_report.json").exists()
    history_lines = [
        json.loads(line)
        for line in (state_state_dir / "report_history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["round_barcode"] for entry in history_lines] == ["output_round_6"]
    demux_first_seen = (round6 / "RTBioScan_read_fate_demult_first_seen.tsv").read_text(encoding="utf-8")
    assert "\nr1\t" in demux_first_seen
    assert "\nr2\t" in demux_first_seen
    assert (state_state_dir / "RTBioScan_read_fate_demux_seen.tsv").read_text(encoding="utf-8").splitlines() == [
        "r1",
        "r2",
    ]
    assert (state_state_dir / "RTBioScan_read_fate_blast_seen.tsv").read_text(encoding="utf-8").splitlines() == [
        "r1",
        "r2",
    ]


def test_report_read_fate_repair_feeder_filter_composes_with_live_bound(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    write_repair_round(state_dir, 4, ["r4"])
    round5 = write_repair_round(state_dir, 5, ["r5"])
    write_repair_round(state_dir, 6, ["r6"])
    write_repair_round(state_dir, 7, ["r7"])
    feeder_meta = tmp_path / "results" / "pod5" / "runA" / "metadata"
    write_text(feeder_meta / "runA_4_slice.tsv", "# slice_fingerprint=abc\n")
    write_text(feeder_meta / "runA_6_slice.tsv", "# slice_fingerprint=def\n")
    write_text(feeder_meta / "runA_7_slice.tsv", "# slice_fingerprint=ghi\n")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
            "--valid-rounds-from-feeder-metadata",
            str(feeder_meta),
            "--live",
            "--current-round-barcode",
            "output_round_6",
            "--skip-render",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    history_lines = [
        json.loads(line)
        for line in (state_state_dir / "report_history.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [entry["round_barcode"] for entry in history_lines] == ["output_round_4", "output_round_6"]
    assert (round5 / "round_report.json.orphan").exists()


def test_report_read_fate_repair_feeder_filter_fails_before_renaming_when_no_valid_sidecars(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round5 = write_repair_round(state_dir, 5, ["r1"])
    feeder_meta = tmp_path / "results" / "pod5" / "runA" / "metadata"
    feeder_meta.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
            "--valid-rounds-from-feeder-metadata",
            str(feeder_meta),
            "--skip-render",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "no *_slice.tsv files" in result.stderr
    assert (round5 / "round_report.json").exists()
    assert not (round5 / "round_report.json.orphan").exists()


def test_report_read_fate_repair_feeder_filter_fails_before_renaming_when_metadata_dir_missing(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round5 = write_repair_round(state_dir, 5, ["r1"])
    missing_meta = tmp_path / "results" / "pod5" / "runA" / "metadata_missing"

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
            "--valid-rounds-from-feeder-metadata",
            str(missing_meta),
            "--skip-render",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "feeder metadata dir not found" in result.stderr
    assert (round5 / "round_report.json").exists()
    assert not (round5 / "round_report.json.orphan").exists()


def test_report_read_fate_repair_ignores_old_reads_not_introduced_this_round(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    round1 = state_dir / "output_round_1"
    round2 = state_dir / "output_round_2"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round1.mkdir(parents=True, exist_ok=True)
    round2.mkdir(parents=True, exist_ok=True)

    round_json_base = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "warnings": [],
    }
    write_text(
        round1 / "round_report.json",
        json.dumps({**round_json_base, "round_barcode": "output_round_1"}) + "\n",
    )
    write_text(
        round2 / "round_report.json",
        json.dumps({**round_json_base, "round_barcode": "output_round_2"}) + "\n",
    )

    read_info_header = (
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\t"
        "hac_mean_qscore\tsup_length\tsup_mean_qscore\n"
    )
    demult_header = (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t"
        "sampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n"
    )
    blast_header = (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\t"
        "aln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\t"
        "otu_order\totu_family\totu_genus\totu_species\n"
    )

    write_text(
        round1 / "RTBioScan_read_info_rpt.txt",
        read_info_header + "r1\tr1.pod5\trunA\tbc\t100\t10\t100\t12\tNA\tNA\n",
    )
    write_text(
        round2 / "RTBioScan_read_info_rpt.txt",
        read_info_header + "r2\tr2.pod5\trunA\tbc\t101\t10\t101\t12\tNA\tNA\n",
    )
    write_text(round1 / "RTBioScan_demult_rpt.txt", demult_header)
    write_text(
        round2 / "RTBioScan_demult_rpt.txt",
        demult_header
        + "r1\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n"
        + "r2\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n",
    )
    write_text(round1 / "RTBioScan_blast_otu_pretax_rpt.txt", blast_header)
    write_text(
        round2 / "RTBioScan_blast_otu_pretax_rpt.txt",
        blast_header
        + "r1\tCOI\thac\tsample_A_1\thit1\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS\n"
        + "r2\tCOI\thac\tsample_A_1\thit2\tNA\t100\t98\tOTUB_2-COI\tNA\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\n",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--state-dir",
            str(state_dir),
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
            "--skip-render",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    round1_demux = (round1 / "RTBioScan_read_fate_demult_first_seen.tsv").read_text(encoding="utf-8").splitlines()
    round1_blast = (round1 / "RTBioScan_read_fate_blast_first_seen.tsv").read_text(encoding="utf-8").splitlines()
    round2_demux = (round2 / "RTBioScan_read_fate_demult_first_seen.tsv").read_text(encoding="utf-8").splitlines()
    round2_blast = (round2 / "RTBioScan_read_fate_blast_first_seen.tsv").read_text(encoding="utf-8").splitlines()
    assert len([line for line in round1_demux if line.strip()]) == 1
    assert len([line for line in round1_blast if line.strip()]) == 1
    assert len([line for line in round2_demux if line.strip()]) == 2
    assert len([line for line in round2_blast if line.strip()]) == 2
    assert round2_demux[1].startswith("r2\t")
    assert round2_blast[1].startswith("r2\t")
    assert all(not line.startswith("r1\t") for line in round2_demux[1:])
    assert all(not line.startswith("r1\t") for line in round2_blast[1:])

    demux_seen = (state_state_dir / "RTBioScan_read_fate_demux_seen.tsv").read_text(encoding="utf-8").splitlines()
    blast_seen = (state_state_dir / "RTBioScan_read_fate_blast_seen.tsv").read_text(encoding="utf-8").splitlines()
    assert demux_seen == ["r1", "r2"]
    assert blast_seen == ["r1", "r2"]


def test_report_read_fate_repair_check_live_order_requests_normalization_for_out_of_order_history(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    round0 = state_dir / "round_0"
    round1 = state_dir / "round_1"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round0.mkdir(parents=True, exist_ok=True)
    round1.mkdir(parents=True, exist_ok=True)

    round0_obj = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "round_barcode": "round_0",
        "warnings": [],
    }
    round1_obj = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "round_barcode": "round_1",
        "warnings": [],
    }
    write_text(round0 / "round_report.json", json.dumps(round0_obj) + "\n")
    write_text(round1 / "round_report.json", json.dumps(round1_obj) + "\n")
    write_text(
        state_state_dir / "report_history.jsonl",
        json.dumps(round1_obj) + "\n" + json.dumps(round0_obj) + "\n",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--check-live-order",
            "--state-dir",
            str(state_dir),
            "--current-round-barcode",
            "round_1",
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "normalize"


def test_report_read_fate_repair_check_live_order_requests_normalization_when_history_missing_but_prior_rounds_exist(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    round0 = state_dir / "round_0"
    round1 = state_dir / "round_1"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round0.mkdir(parents=True, exist_ok=True)
    round1.mkdir(parents=True, exist_ok=True)

    round0_obj = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "round_barcode": "round_0",
        "warnings": [],
    }
    round1_obj = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "round_barcode": "round_1",
        "warnings": [],
    }
    write_text(round0 / "round_report.json", json.dumps(round0_obj) + "\n")
    write_text(round1 / "round_report.json", json.dumps(round1_obj) + "\n")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--check-live-order",
            "--state-dir",
            str(state_dir),
            "--current-round-barcode",
            "round_1",
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "normalize"


def test_report_read_fate_repair_check_live_order_ignores_future_rounds(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    round1 = state_dir / "round_1"
    round2 = state_dir / "round_2"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round1.mkdir(parents=True, exist_ok=True)
    round2.mkdir(parents=True, exist_ok=True)

    base = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "warnings": [],
    }
    round1_obj = {**base, "round_barcode": "round_1"}
    round2_obj = {**base, "round_barcode": "round_2"}
    write_text(round1 / "round_report.json", json.dumps(round1_obj) + "\n")
    write_text(round2 / "round_report.json", json.dumps(round2_obj) + "\n")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--check-live-order",
            "--state-dir",
            str(state_dir),
            "--current-round-barcode",
            "round_1",
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "append"


def test_report_read_fate_repair_live_bounds_future_rounds(tmp_path: Path) -> None:
    state_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / "state1"
    state_state_dir = state_dir / "_state"
    round1 = state_dir / "round_1"
    round2 = state_dir / "round_2"
    state_state_dir.mkdir(parents=True, exist_ok=True)
    round1.mkdir(parents=True, exist_ok=True)
    round2.mkdir(parents=True, exist_ok=True)

    base = {
        "schema_version": "1.6",
        "run_id": "runA",
        "barcode": "RTBioScan",
        "state_id": "state1",
        "warnings": [],
    }
    write_text(round1 / "round_report.json", json.dumps({**base, "round_barcode": "round_1"}) + "\n")
    write_text(round2 / "round_report.json", json.dumps({**base, "round_barcode": "round_2"}) + "\n")

    demult_header = (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\t"
        "sampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n"
    )
    blast_header = (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\t"
        "aln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\t"
        "otu_order\totu_family\totu_genus\totu_species\n"
    )
    for round_dir, read_id in ((round1, "r1"), (round2, "r2")):
        write_text(
            round_dir / "RTBioScan_demult_rpt.txt",
            demult_header + f"{read_id}\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n",
        )
        write_text(
            round_dir / "RTBioScan_blast_otu_pretax_rpt.txt",
            blast_header + f"{read_id}\tCOI\thac\tsample_A_1\thit1\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS\n",
        )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            "--live",
            "--skip-render",
            "--state-dir",
            str(state_dir),
            "--current-round-barcode",
            "round_1",
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    history_lines = [line for line in (state_state_dir / "report_history.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(history_lines) == 1
    assert json.loads(history_lines[0])["round_barcode"] == "round_1"
