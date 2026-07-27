import json
import hashlib
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "report_round_json.pl"


def _run(args):
    return subprocess.run(
        ["perl", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_lock_summary(
    path: Path,
    otu_key: str,
    *,
    extra_header: str = "",
    extra_row: str = "",
) -> None:
    header = "otu_key\teffective_consolidated\tis_frozen"
    row = f"{otu_key}\t0\t0"
    if extra_header:
        header = f"{header}\t{extra_header}"
    if extra_row:
        row = f"{row}\t{extra_row}"
    path.write_text(
        f"{header}\n{row}\n",
        encoding="utf-8",
    )


def _run_frozen_assignment_case(
    tmp_path: Path,
    *,
    blast_rows: str,
    otu_def_rows: str | None = None,
    otu_def_header: str = "read_id\tsample\tOTU_id\tOTU_role\n",
    frozen_member_rows: str | None = None,
    rep_hash_member_id: str | None = None,
    extra_args: list[str] | None = None,
    otu_size: int = 7,
    otu_sizes_rows: str | None = None,
    lock_summary_rows: str | None = None,
) -> dict:
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        f"{blast_rows}",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text(
        "otu_id\tsize\n" + (otu_sizes_rows if otu_sizes_rows is not None else f"OTUB_F-COI\t{otu_size}\n"),
        encoding="utf-8",
    )
    default_lock_row = "OTUB_F-COI\t0\t1\n" if frozen_member_rows is not None else "OTUB_F-COI\t0\t0\n"
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n" + (lock_summary_rows if lock_summary_rows is not None else default_lock_row),
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    args = [
        "--run-id", "runA",
        "--barcode", "RTBioScan",
        "--round-barcode", "output_round_1",
        "--out", str(out),
        "--blast-otu", str(blast_otu),
        "--otu-sizes-round", str(otu_sizes_round),
        "--otu-lock-summary", str(lock_summary),
    ]
    if otu_def_rows is not None:
        otu_def = tmp_path / "otu_def.tsv"
        otu_def.write_text(
            f"{otu_def_header}{otu_def_rows}",
            encoding="utf-8",
        )
        args.extend(["--otu-def", str(otu_def)])
    if frozen_member_rows is not None:
        state_dir = tmp_path / "_state"
        state_dir.mkdir(exist_ok=True)
        rep_hash_member_id = rep_hash_member_id or "rep1|COI|sup|barcode=|adapter=sample_A"
        (tmp_path / "RTBioScan_otu_hash_map.tsv").write_text(
            f"{rep_hash_member_id}\th1\n",
            encoding="utf-8",
        )
        (state_dir / "otu_frozen_meta.tsv").write_text(
            f"FROZEN_h1\t{rep_hash_member_id}\th1\n",
            encoding="utf-8",
        )
        (state_dir / "otu_frozen_members.tsv").write_text(
            frozen_member_rows,
            encoding="utf-8",
        )
    if extra_args:
        args.extend(extra_args)
    result = _run(args)
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def _otu_species_row_by_sample(data: dict, sample_label: str) -> dict:
    for row in data["otu"]["assignments_by_level"]["species"]:
        if row.get("sample") == sample_label:
            return row
    raise AssertionError(f"Missing OTU species row for sample {sample_label}: {data['otu']['assignments_by_level']['species']}")


def test_report_round_json_basic(tmp_path: Path) -> None:
    read_info = tmp_path / "read_info.tsv"
    read_info.write_text(
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        "r1\tf\tR\tb\t100\t10\t99\t12\tNA\tNA\n"
        "r2\tf\tR\tb\t110\t11\t108\t13\t107\t15\n",
        encoding="utf-8",
    )
    on_target = tmp_path / "on_target.tsv"
    on_target.write_text(
        "read_id\tqc_filter\ton_target_kingdom\n"
        "r1\tIN\tON_TARGET\n"
        "r2\tIN\tOFF_TARGET\n",
        encoding="utf-8",
    )
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text(
        "read_id\tOTU_id\nr1\tOTUB_1-COI\nr2\tOTUB_2-COI\nr3\tOTUB_2-COI\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\t"
        "taxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\t"
        "consensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "Consensus1_sample_A_1\tCOI\tconsensus\t10\tsample_A_1\t123\thit\t100\t99\tK\tP\tC\tO\tF\tG\tS\n"
        "Consensus2_sample_B_2\tITS2\tconsensus\t8\tsample_B_2\tNA\thit\t100\t99\tK\tP\tC\tO\tF\tG\tS\n",
        encoding="utf-8",
    )
    size_streak_stats = tmp_path / "size_streak.tsv"
    size_streak_stats.write_text("reads_prune_candidate\t3\notus_prune_candidate\t2\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_1-COI\t1\t0\n"
        "OTUB_2-COI\t0\t0\n",
        encoding="utf-8",
    )
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_1\t1\n", encoding="utf-8")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("k\tCOI\tr2\tOTUB_2-COI\t1\n", encoding="utf-8")
    blast_filter = tmp_path / "blast_filter.tsv"
    blast_filter.write_text("kept_reads\t11\nkept_otus\t5\ndropped_otus\t2\nmissing_policy\tdrop\n", encoding="utf-8")
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_1-COI\tF1\tG1\tS1\n"
        "r2\tCOI\thac\tsample_A_1\thit\t123\t100\t97\tOTUB_1-COI\tF1\tG1\tS1\n"
        "r3\tCOI\thac\tsample_B_2\thit\t234\t100\t98\tOTUB_2-COI\tF2\tG2\tS2\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text(
        "otu_id\tsize\n"
        "OTUB_1-COI\t5\n"
        "OTUB_2-COI\t3\n",
        encoding="utf-8",
    )
    blastdiag_stats = tmp_path / "blastdiag.tsv"
    blastdiag_stats.write_text("rows_total\t8\notu_total\t3\n", encoding="utf-8")
    consolidated = tmp_path / "cons_ids.txt"
    consolidated.write_text("id1\nid2\n", encoding="utf-8")
    blast_filter_dropped = tmp_path / "blast_filter_dropped.list"
    blast_filter_dropped.write_text("r1\n", encoding="utf-8")
    consensus_round_prov = tmp_path / "consensus_round_provenance.tsv"
    consensus_round_prov.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "output_round_1\tsample_A_1\tOTUB_1-COI\tConsensus1_sample_A_1\t4\n"
        "output_round_1\tsample_B_2\tOTUB_2-ITS2\tConsensus2_sample_B_2\t3\n",
        encoding="utf-8",
    )
    active_prune_counts = tmp_path / "active_prune_candidates_counts.tsv"
    active_prune_counts.write_text(
        "active_total\t3\n"
        "size_streak_active\t1\n"
        "size_streak_candidates\t2\n"
        "union\t2\n"
        "active_scope\tround\n"
        "active_scope_reason\tround_local_ok\n"
        "size_streak_input_status\tready\n"
        "size_streak_possible\t1\n"
        "size_streak_applied\t0\n"
        "size_streak_disabled\t0\n"
        "effective_mode\toff\n"
        "effective_reason\twithin_skip_window\n"
        "round_index\t1\n"
        "size_streak_round_candidates\t2\n"
        "size_streak_eligible_candidates\t2\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"

    result = _run(
        [
            "--run-id",
            "runA",
            "--state-id",
            "stateA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--read-info",
            str(read_info),
            "--on-target",
            str(on_target),
            "--otu-def",
            str(otu_def),
            "--blast-otu",
            str(blast_otu),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-consensus",
            str(blast_cons),
            "--consensus-round-provenance",
            str(consensus_round_prov),
            "--active-prune-counts",
            str(active_prune_counts),
            "--otu-size-streak-stats",
            str(size_streak_stats),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "2",
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-blast-filter-stats",
            str(blast_filter),
            "--blast-filter-dropped-ids",
            str(blast_filter_dropped),
            "--otu-blast-min-members",
            "4",
            "--otu-blast-filter-skip-rounds",
            "none",
            "--otu-blast-unassigned-grace-rounds",
            "0",
            "--round-index-file",
            str(round_index),
            "--otu-members-blastdiag-stats",
            str(blastdiag_stats),
            "--blast-filter-mode",
            "enforce",
            "--consensus-consolidated-ids",
            str(consolidated),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["run_id"] == "runA"
    assert data["schema_version"] == "2.0"
    assert data["barcode"] == "RTBioScan"
    assert data["round_barcode"] == "output_round_1"
    assert data["reads"]["total"] == 2
    assert data["reads"]["on_target"] == 1
    assert data["reads"]["hac"] == 2
    assert data["reads"]["sup"] == 1
    assert data["otu"]["canonical"]["active"] == 2

    assert data["otu"]["canonical"]["consolidated"] == 1
    assignments = data["otu"]["assignments_by_level"]
    assert "species" in assignments
    assert assignments["species"][0]["taxon"] == "S1"
    assert assignments["species"][0]["otu_count"] == 1
    assert assignments["species"][0]["reads_total"] == 2
    assert data["otu"]["canonical"]["frozen_not_consolidated"] == 0
    assert data["otu"]["canonical"]["active_not_frozen"] == 1
    assert data["otu"]["canonical"]["informative_dynamic"] == 0
    assert data["otu"]["pruned"]["prune_candidates"] == 0
    assert data["otu"]["pruned"]["size_streak"] == 1
    assert data["otu"]["pruned"]["blast_unassigned"] == 0
    assert data["blast"]["filtered_reads"] == 11
    assert data["blast"]["filtered_otus"] == 5
    assert data["blast"]["mode"] == "enforce"
    assert data["blast"]["missing_policy"] == "drop"
    otu_break = data["otu"]["active_by_marker_taxon"]
    assert otu_break["coi_assigned"] == 1
    assert otu_break["its2_assigned"] == 0
    assert otu_break["coi_unassigned"] == 0
    assert otu_break["its2_unassigned"] == 0
    assert data["consensus"]["emitted"] == 2
    assert data["consensus"]["consolidated"] == 2
    cons_break = data["consensus"]["emitted_by_marker_taxon"]
    assert cons_break["coi_assigned"] == 1
    assert cons_break["its2_assigned"] == 0
    assert cons_break["coi_unassigned"] == 0
    assert cons_break["its2_unassigned"] == 1
    read_fate = data["read_fate"]
    assert read_fate["marker_split_status"] == "invalid"
    assert "demux_stage_absent" in read_fate["data_reason_codes"]
    assert read_fate["blast_seen_reads"] == 3
    assert read_fate["blast_assigned_reads"] == 3
    assert read_fate["blast_unassigned_reads"] == 0
    assert read_fate["chart_blast_assigned_coi"] is None
    cons_assign = data["consensus"]["assignments_by_level"]
    assert "species" in cons_assign
    assert cons_assign["species"][0]["taxon"] == "S"
    assert cons_assign["species"][0]["consensus_count"] == 1
    prune = data["otu"]["prune_candidates_round"]
    assert prune["active_total"] == 3
    assert prune["size_streak_active"] == 1
    assert prune["size_streak_candidates"] == 2
    assert prune["union"] == 2
    assert prune["active_scope"] == "round"
    assert prune["active_scope_reason"] == "round_local_ok"
    assert prune["size_streak_input_status"] == "ready"
    assert prune["size_streak_possible"] == 1
    assert prune["size_streak_applied"] == 0
    assert prune["size_streak_disabled"] == 0
    assert prune["effective_mode"] == "off"
    assert prune["effective_reason"] == "within_skip_window"
    assert prune["round_index"] == 1
    assert prune["size_streak_round_candidates"] == 2
    assert prune["size_streak_eligible_candidates"] == 2


def test_report_round_json_accepts_lock_summary_with_top_two_columns(tmp_path: Path) -> None:
    read_info = tmp_path / "read_info.tsv"
    read_info.write_text(
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        "r1\tf\tR\tb\t100\t10\t99\t12\tNA\tNA\n",
        encoding="utf-8",
    )
    on_target = tmp_path / "on_target.tsv"
    on_target.write_text("read_id\tqc_filter\ton_target_kingdom\nr1\tIN\tON_TARGET\n", encoding="utf-8")
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_1-COI\n", encoding="utf-8")
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_1-COI\tF1\tG1\tS1\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_1-COI\t1\n", encoding="utf-8")
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\t"
        "taxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\t"
        "consensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n",
        encoding="utf-8",
    )
    consensus_round_prov = tmp_path / "consensus_round_provenance.tsv"
    consensus_round_prov.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n",
        encoding="utf-8",
    )
    size_streak_stats = tmp_path / "size_streak.tsv"
    size_streak_stats.write_text("reads_prune_candidate\t0\notus_prune_candidate\t0\n", encoding="utf-8")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("", encoding="utf-8")
    blast_filter = tmp_path / "blast_filter.tsv"
    blast_filter.write_text("kept_reads\t1\nkept_otus\t1\ndropped_otus\t0\nmissing_policy\tdrop\n", encoding="utf-8")
    blast_filter_dropped = tmp_path / "blast_filter_dropped.list"
    blast_filter_dropped.write_text("", encoding="utf-8")
    blastdiag_stats = tmp_path / "blastdiag.tsv"
    blastdiag_stats.write_text("rows_total\t1\notu_total\t1\n", encoding="utf-8")
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_1\t1\n", encoding="utf-8")
    active_prune_counts = tmp_path / "active_prune_counts.tsv"
    active_prune_counts.write_text(
        "active_total\t1\n"
        "size_streak_active\t0\n"
        "size_streak_candidates\t0\n"
        "union\t0\n"
        "active_scope\tround\n"
        "active_scope_reason\tround_local_ok\n"
        "size_streak_input_status\tready\n"
        "size_streak_possible\t1\n"
        "size_streak_applied\t0\n"
        "size_streak_disabled\t1\n"
        "effective_mode\toff\n"
        "effective_reason\twithin_skip_window\n"
        "round_index\t1\n"
        "size_streak_round_candidates\t0\n"
        "size_streak_eligible_candidates\t0\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(
        lock_summary,
        "OTUB_1-COI",
        extra_header="otu_sig_rule\ttop1_cluster_size\ttop1_cluster_qscore\ttop2_cluster_size\ttop2_cluster_qscore\ttop2_ratio\ttop2_delta_reads",
        extra_row="top_two_gap\t10\t30\t4\t30\t2.500000\t6",
    )
    consolidated = tmp_path / "cons_ids.txt"
    consolidated.write_text("", encoding="utf-8")
    out = tmp_path / "round_report.json"

    result = _run(
        [
            "--run-id",
            "runA",
            "--state-id",
            "stateA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--read-info",
            str(read_info),
            "--on-target",
            str(on_target),
            "--otu-def",
            str(otu_def),
            "--blast-otu",
            str(blast_otu),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-consensus",
            str(blast_cons),
            "--consensus-round-provenance",
            str(consensus_round_prov),
            "--active-prune-counts",
            str(active_prune_counts),
            "--otu-size-streak-stats",
            str(size_streak_stats),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "off",
            "--otu-size-streak-min-rounds",
            "2",
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-blast-filter-stats",
            str(blast_filter),
            "--blast-filter-dropped-ids",
            str(blast_filter_dropped),
            "--otu-blast-min-members",
            "1",
            "--otu-blast-filter-skip-rounds",
            "none",
            "--otu-blast-unassigned-grace-rounds",
            "0",
            "--round-index-file",
            str(round_index),
            "--otu-members-blastdiag-stats",
            str(blastdiag_stats),
            "--blast-filter-mode",
            "off",
            "--consensus-consolidated-ids",
            str(consolidated),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["canonical"]["active"] == 1
    assert data["otu"]["canonical"]["consolidated"] == 0


def test_report_round_json_honors_timestamp_override(tmp_path: Path) -> None:
    out = tmp_path / "round_report.json"
    timestamp_utc = "2026-04-10T12:34:56Z"

    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--timestamp-utc",
            timestamp_utc,
            "--out",
            str(out),
        ]
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["timestamp_utc"] == timestamp_utc


def test_report_round_json_marks_failed_round_when_failure_file_present(tmp_path: Path) -> None:
    failed = tmp_path / "ROUND_FAILED.txt"
    failed.write_text("No target reads for runA_3\n", encoding="utf-8")
    out = tmp_path / "round_report.json"

    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "runA_3",
            "--out",
            str(out),
            "--round-failed-file",
            str(failed),
        ]
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["round_status"] == "failed"
    assert data["failure_reason"] == "No target reads for runA_3"
    assert data["failure_stage"] == "fast_on_target_detection"


def test_report_round_json_on_target_fallback_legacy_rows(tmp_path: Path) -> None:
    on_target = tmp_path / "on_target.tsv"
    on_target.write_text("read_id\tqc_filter\nr1\tIN\nr2\tIN\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--on-target",
            str(on_target),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["reads"]["on_target"] == 2


def test_report_round_json_otu_assignments_respect_identity_thresholds(tmp_path: Path) -> None:
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTU_1-COI\tF1\tG1\tS1\n"
        "r2\tCOI\thac\tsample_A_1\thit\t123\t100\t97\tOTU_2-COI\tF2\tG2\tS2\n"
        "r3\tCOI\thac\tsample_A_1\thit\t123\t100\t94\tOTU_3-COI\tF3\tG3\tS3\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text(
        "otu_id\tsize\n"
        "OTU_1-COI\t5\n"
        "OTU_2-COI\t4\n"
        "OTU_3-COI\t3\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"

    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--blast-otu",
            str(blast_otu),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--targets",
            "COI",
            "--blast-id-family",
            "92",
            "--blast-id-genus",
            "95",
            "--blast-id-spec",
            "98",
        ]
    )
    assert result.returncode == 0, result.stderr

    data = json.loads(out.read_text(encoding="utf-8"))
    assignments = data["otu"]["assignments_by_level"]

    assert [row["taxon"] for row in assignments["species"]] == ["S1"]
    assert [row["taxon"] for row in assignments["genus"]] == ["G1", "G2"]
    assert [row["taxon"] for row in assignments["family"]] == ["F1", "F2", "F3"]
    assert all(row["perc_id_min"] >= 98 for row in assignments["species"])
    assert all(row["perc_id_min"] >= 95 for row in assignments["genus"])
    assert all(row["perc_id_min"] >= 92 for row in assignments["family"])


def test_report_round_json_tolerates_repeated_headers_blank_and_malformed_rows(tmp_path: Path) -> None:
    read_info = tmp_path / "read_info.tsv"
    read_info.write_text(
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        "\n"
        "r1\tf\tR\tb\t100\t10\t99\t12\tNA\tNA\n"
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        "broken\ttoo_few_columns\n"
        "r2\tf\tR\tb\t110\t11\t108\t13\t107\t15\n",
        encoding="utf-8",
    )
    on_target = tmp_path / "on_target.tsv"
    on_target.write_text(
        "read_id\tqc_filter\ton_target_kingdom\n"
        "r1\tIN\tON_TARGET\n"
        "\n"
        "read_id\tqc_filter\ton_target_kingdom\n"
        "r2\tIN\tOFF_TARGET\n"
        "r3\tIN\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_1-COI\tF1\tG1\tS1\n"
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r2\tCOI\thac\tsample_A_1\thit\t123\t100\t97\tOTUB_1-COI\tF1\tG1\tS1\n"
        "bad\trow\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text(
        "otu_id\tsize\n"
        "OTUB_1-COI\t2\n"
        "otu_id\tsize\n"
        "broken\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\t"
        "taxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\t"
        "consensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "Consensus1_sample_A_1\tCOI\tconsensus\t10\tsample_A_1\t123\thit\t100\t99\tK\tP\tC\tO\tF\tG\tS\n"
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\t"
        "taxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\t"
        "consensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "broken\trow\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--read-info",
            str(read_info),
            "--on-target",
            str(on_target),
            "--blast-otu",
            str(blast_otu),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-consensus",
            str(blast_cons),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["reads"]["total"] == 3
    assert data["reads"]["on_target"] == 1
    assert data["reads"]["hac"] == 2
    assert data["reads"]["sup"] == 1
    otu_rows = data["otu"]["assignments_by_level"]["species"]
    assert len(otu_rows) == 1
    assert otu_rows[0]["taxon"] == "S1"
    assert otu_rows[0]["otu_count"] == 1
    assert otu_rows[0]["reads_total"] == 2
    cons_rows = data["consensus"]["assignments_by_level"]["species"]
    assert len(cons_rows) == 1
    assert cons_rows[0]["taxon"] == "S"
    assert cons_rows[0]["consensus_count"] == 1


def test_report_round_json_emits_frozen_and_consolidated_read_totals(tmp_path: Path) -> None:
    read_info = tmp_path / "read_info.tsv"
    read_info.write_text(
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        "r1\tf\tR\tb\t100\t10\t99\t12\tNA\tNA\n",
        encoding="utf-8",
    )
    on_target = tmp_path / "on_target.tsv"
    on_target.write_text("read_id\tqc_filter\ton_target_kingdom\nr1\tIN\tON_TARGET\n", encoding="utf-8")
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        "r2\tCOI\thac\tsample_A\thit\t123\t100\t98\tOTUB_A-COI\tF1\tG1\tS1\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_F-COI\t5\nOTUB_A-COI\t3\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_F-COI\t0\t1\n"
        "OTUB_A-COI\t0\t0\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\t"
        "taxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\t"
        "consensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "Consensus1_sample_A\tCOI\tconsensus\t10\tsample_A\t123\thit\t100\t99\tK\tP\tC\tO\tF\tG\tS\n"
        "Consensus2_sample_A\tCOI\tconsensus\t4\tsample_A\t123\thit\t100\t98\tK\tP\tC\tO\tF\tG\tS\n",
        encoding="utf-8",
    )
    consolidated = tmp_path / "cons_ids.txt"
    consolidated.write_text("Consensus1_sample_A\n", encoding="utf-8")
    out = tmp_path / "round_report.json"

    result = _run(
        [
            "--run-id", "runA",
            "--barcode", "RTBioScan",
            "--round-barcode", "output_round_1",
            "--out", str(out),
            "--read-info", str(read_info),
            "--on-target", str(on_target),
            "--blast-otu", str(blast_otu),
            "--otu-sizes-round", str(otu_sizes_round),
            "--otu-lock-summary", str(lock_summary),
            "--blast-consensus", str(blast_cons),
            "--consensus-consolidated-ids", str(consolidated),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))

    otu_row = data["otu"]["assignments_by_level"]["species"][0]
    assert otu_row["taxon"] == "S1"
    assert otu_row["otu_count"] == 1
    assert otu_row["frozen_otu_count"] == 0
    assert otu_row["reads_total"] == 1
    assert otu_row["frozen_otu_reads_total"] is None
    assert "frozen_otu_reads_sample_total" not in otu_row

    cons_row = data["consensus"]["assignments_by_level"]["species"][0]
    assert cons_row["taxon"] == "S"
    assert cons_row["consensus_count"] == 2
    assert cons_row["consolidated_consensus_count"] == 1
    assert cons_row["reads_total"] == 14
    assert cons_row["consolidated_consensus_reads_total"] == 10


def test_otu_reads_sample_total_per_sample(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_B\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "a1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "a3\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "a4\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_B\tOTUB_F-COI\tMEMBER\n"
            "b2\tsample_B\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=6,
    )

    assert data["schema_version"] == "2.0"
    sample_a_row = _otu_species_row_by_sample(data, "sample_A")
    sample_b_row = _otu_species_row_by_sample(data, "sample_B")
    assert sample_a_row["otu_reads_sample_total"] == 4
    assert sample_b_row["otu_reads_sample_total"] == 2
    assert sample_a_row["reads_total"] == 4
    assert sample_b_row["reads_total"] == 2


def test_otu_reads_sample_total_sums_across_otus_in_same_taxon_row(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_A-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "f1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "f2\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "f3\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "a1\tsample_A\tOTUB_A-COI\tREPRESENTATIVE\n"
            "a2\tsample_A\tOTUB_A-COI\tMEMBER\n"
            "a3\tsample_A\tOTUB_A-COI\tMEMBER\n"
            "a4\tsample_A\tOTUB_A-COI\tMEMBER\n"
            "a5\tsample_A\tOTUB_A-COI\tMEMBER\n"
        ),
        otu_sizes_rows=(
            "OTUB_F-COI\t3\n"
            "OTUB_A-COI\t5\n"
        ),
        lock_summary_rows=(
            "OTUB_F-COI\t0\t1\n"
            "OTUB_A-COI\t0\t0\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\tf1|COI|sup|barcode=|adapter=sample_A\t0\n"
            "FROZEN_h1\tf2|COI|sup|barcode=|adapter=sample_A\t0\n"
            "FROZEN_h1\tf3|COI|sup|barcode=|adapter=sample_A\t0\n"
        ),
        rep_hash_member_id="f1|COI|sup|barcode=|adapter=sample_A",
    )

    row = _otu_species_row_by_sample(data, "sample_A")
    assert row["otu_count"] == 2
    assert row["otu_reads_sample_total"] == 8
    assert row["reads_total"] == 8
    assert row["frozen_otu_reads_total"] == 3


def test_otu_reads_sample_total_collapse_mode(tmp_path: Path) -> None:
    roster = tmp_path / "samples.txt"
    roster.write_text("sample_A\n", encoding="utf-8")
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_A_2\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "a1\tsample_A_1\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tsample_A_1\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_A_2\tOTUB_F-COI\tMEMBER\n"
            "b2\tsample_A_2\tOTUB_F-COI\tMEMBER\n"
            "b3\tsample_A_2\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=5,
        extra_args=["--identity-mode", "collapse", "--sample-roster", str(roster)],
    )

    row = _otu_species_row_by_sample(data, "sample_A")
    assert row["otu_reads_sample_total"] == 5
    assert row["reads_total"] == 5


def test_otu_reads_sample_total_track_mode(tmp_path: Path) -> None:
    roster = tmp_path / "track_roster.tsv"
    roster.write_text("track_id\nsample_A_1\nsample_A_2\n", encoding="utf-8")
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_A_2\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "a1\tsample_A_1\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tsample_A_1\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_A_2\tOTUB_F-COI\tMEMBER\n"
            "b2\tsample_A_2\tOTUB_F-COI\tMEMBER\n"
            "b3\tsample_A_2\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=5,
        extra_args=["--identity-mode", "track", "--sample-roster", str(roster)],
    )

    sample_a_row = _otu_species_row_by_sample(data, "sample_A_1")
    sample_b_row = _otu_species_row_by_sample(data, "sample_A_2")
    assert sample_a_row["otu_reads_sample_total"] == 2
    assert sample_b_row["otu_reads_sample_total"] == 3


def test_otu_reads_sample_total_track_mode_normalizes_identity_value_marker_suffix(tmp_path: Path) -> None:
    roster = tmp_path / "track_roster.tsv"
    roster.write_text("track_id\nsample_A_1\nsample_A_2\n", encoding="utf-8")
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A_1_COI\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_A_2_COI\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_header=(
            "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\t"
            "subsample\treplicate\tidentity_scope\tidentity_value\tOTU_id\tOTU_role\n"
        ),
        otu_def_rows=(
            "a1\tCOI\thac\tsample_A_1_COI\tunknown\tunknown\tunknown\tunknown\tunit\tsample_A_1_COI\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tCOI\thac\tsample_A_1_COI\tunknown\tunknown\tunknown\tunknown\tunit\tsample_A_1_COI\tOTUB_F-COI\tMEMBER\n"
            "b1\tCOI\thac\tsample_A_2_COI\tunknown\tunknown\tunknown\tunknown\tunit\tsample_A_2_COI\tOTUB_F-COI\tMEMBER\n"
            "b2\tCOI\thac\tsample_A_2_COI\tunknown\tunknown\tunknown\tunknown\tunit\tsample_A_2_COI\tOTUB_F-COI\tMEMBER\n"
            "b3\tCOI\thac\tsample_A_2_COI\tunknown\tunknown\tunknown\tunknown\tunit\tsample_A_2_COI\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=5,
        extra_args=["--identity-mode", "track", "--sample-roster", str(roster)],
    )

    sample_a_row = _otu_species_row_by_sample(data, "sample_A_1")
    sample_b_row = _otu_species_row_by_sample(data, "sample_A_2")
    assert sample_a_row["otu_reads_sample_total"] == 2
    assert sample_b_row["otu_reads_sample_total"] == 3
    assert all(row["sample"] not in {"sample_A_1_COI", "sample_A_2_COI"} for row in data["otu"]["assignments_by_level"]["species"])


def test_otu_reads_sample_total_track_mode_prefers_no_adapter_over_unknown_identity_value(tmp_path: Path) -> None:
    roster = tmp_path / "track_roster.tsv"
    roster.write_text("track_id\nsample_A_1\n", encoding="utf-8")
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tno_adapter\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_def_header=(
            "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\t"
            "subsample\treplicate\tidentity_scope\tidentity_value\tOTU_id\tOTU_role\n"
        ),
        otu_def_rows=(
            "a1\tCOI\thac\tno_adapter\tunknown\tunknown\tunknown\tunknown\tunit\tunknown\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tCOI\thac\tno_adapter\tunknown\tunknown\tunknown\tunknown\tunit\tunknown\tOTUB_F-COI\tMEMBER\n"
            "a3\tCOI\thac\tno_adapter\tunknown\tunknown\tunknown\tunknown\tunit\tunknown\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=3,
        extra_args=["--identity-mode", "track", "--sample-roster", str(roster)],
    )

    row = _otu_species_row_by_sample(data, "no_adapter")
    assert row["otu_reads_sample_total"] == 3
    assert all(r["sample"] != "unknown" for r in data["otu"]["assignments_by_level"]["species"])


def test_otu_reads_sample_total_track_mode_prefers_no_adapter_over_mixed_case_unknown_identity_value(tmp_path: Path) -> None:
    roster = tmp_path / "track_roster.tsv"
    roster.write_text("track_id\nsample_A_1\n", encoding="utf-8")
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tno_adapter\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_def_header=(
            "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\t"
            "subsample\treplicate\tidentity_scope\tidentity_value\tOTU_id\tOTU_role\n"
        ),
        otu_def_rows=(
            "a1\tCOI\thac\tno_adapter\tunknown\tunknown\tunknown\tunknown\tunit\tUnknown\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tCOI\thac\tno_adapter\tunknown\tunknown\tunknown\tunknown\tunit\tUNKNOWN\tOTUB_F-COI\tMEMBER\n"
            "a3\tCOI\thac\tno_adapter\tunknown\tunknown\tunknown\tunknown\tunit\tUnKnOwN\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=3,
        extra_args=["--identity-mode", "track", "--sample-roster", str(roster)],
    )

    row = _otu_species_row_by_sample(data, "no_adapter")
    assert row["otu_reads_sample_total"] == 3
    assert all((r["sample"] or "").lower() != "unknown" for r in data["otu"]["assignments_by_level"]["species"])


def test_otu_reads_sample_total_keeps_no_adapter_reads(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tno_adapter\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tno_adapter_1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "a1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tno_adapter\tOTUB_F-COI\tMEMBER\n"
            "b1\tno_adapter_1\tOTUB_F-COI\tMEMBER\n"
            "b2\tno_adapter_1\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=4,
    )

    row = _otu_species_row_by_sample(data, "no_adapter")
    assert row["otu_reads_sample_total"] == 4
    assert row["reads_total"] == 4


def test_otu_reads_sample_total_absent_without_otu_def(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_def_rows=None,
    )

    row = _otu_species_row_by_sample(data, "sample_A")
    assert row["otu_reads_sample_total"] == 1
    assert row["reads_total"] == 1
    assert row["otu_reads_global_total"] == 7


def test_otu_assignment_without_otu_def_keeps_sample_specific_blast_evidence(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thitA\t123\t90\t95\tOTUB_F-COI\tF1\tG1\t\n"
            "r2\tCOI\thac\tsample_B\thitB\t123\t110\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=None,
        otu_size=3,
    )

    species_rows = data["otu"]["assignments_by_level"]["species"]
    genus_rows = data["otu"]["assignments_by_level"]["genus"]
    assert len(species_rows) == 1
    assert species_rows[0]["sample"] == "sample_B"
    assert species_rows[0]["taxon"] == "S1"
    sample_a_genus = next(row for row in genus_rows if row["sample"] == "sample_A")
    assert sample_a_genus["taxon"] == "G1"
    assert sample_a_genus["perc_id_min"] == 95
    assert sample_a_genus["perc_id_max"] == 95


def test_otu_assignment_propagates_assigned_otu_to_member_samples(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_def_rows=(
            "a1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "a3\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "a4\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_B\tOTUB_F-COI\tMEMBER\n"
            "b2\tsample_B\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=6,
    )

    sample_a_row = _otu_species_row_by_sample(data, "sample_A")
    sample_b_row = _otu_species_row_by_sample(data, "sample_B")
    assert sample_a_row["taxon"] == "S1"
    assert sample_b_row["taxon"] == "S1"
    assert sample_a_row["otu_reads_sample_total"] == 4
    assert sample_b_row["otu_reads_sample_total"] == 2
    assert sample_a_row["reads_total"] == 4
    assert sample_b_row["reads_total"] == 2
    assert sample_a_row["perc_id_max"] == 99
    assert sample_b_row["perc_id_max"] == 99


def test_otu_assignment_canonicalization_promotes_existing_rows_to_species(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thitA\t123\t90\t95\tOTUB_F-COI\tF1\tG1\t\n"
            "r2\tCOI\thac\tsample_B\thitB\t123\t110\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "a1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_B\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=3,
    )

    sample_a_row = _otu_species_row_by_sample(data, "sample_A")
    sample_b_row = _otu_species_row_by_sample(data, "sample_B")
    assert sample_a_row["taxon"] == "S1"
    assert sample_b_row["taxon"] == "S1"
    assert sample_a_row["perc_id_min"] == 99
    assert sample_a_row["perc_id_max"] == 99
    assert sample_b_row["perc_id_min"] == 99
    assert sample_b_row["perc_id_max"] == 99


def test_otu_assignment_canonicalization_can_downgrade_species_rows(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thitA\t123\t100\t96\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_B\thitB\t123\t110\t99\tOTUB_F-COI\tF1\tG1\t\n"
        ),
        otu_def_rows=(
            "a1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_B\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_size=3,
    )

    assert data["otu"]["assignments_by_level"]["species"] == []
    genus_rows = data["otu"]["assignments_by_level"]["genus"]
    assert {row["sample"] for row in genus_rows} == {"sample_A", "sample_B"}
    assert all(row["taxon"] == "G1" for row in genus_rows)
    assert all(row["perc_id_min"] == 99 for row in genus_rows)
    assert all(row["perc_id_max"] == 99 for row in genus_rows)


def test_otu_assignment_reads_total_falls_back_to_total_members_when_size_missing(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_def_rows=(
            "a1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a2\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_B\tOTUB_F-COI\tMEMBER\n"
            "b2\tsample_B\tOTUB_F-COI\tMEMBER\n"
        ),
        otu_sizes_rows="otu_id\tsize\n",
    )

    sample_a_row = _otu_species_row_by_sample(data, "sample_A")
    sample_b_row = _otu_species_row_by_sample(data, "sample_B")
    assert sample_a_row["reads_total"] == 2
    assert sample_b_row["reads_total"] == 2
    assert sample_a_row["otu_reads_sample_total"] == 2
    assert sample_b_row["otu_reads_sample_total"] == 2


def test_frozen_otu_reads_sample_total_per_sample(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_B\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\ta1|COI|sup|barcode=|adapter=sample_A\t0\n"
            "FROZEN_h1\ta2|COI|sup|barcode=|adapter=sample_A\t0\n"
            "FROZEN_h1\tb1|COI|sup|barcode=|adapter=sample_B\t0\n"
            "FROZEN_h1\tb2|COI|sup|barcode=|adapter=sample_B\t0\n"
            "FROZEN_h1\tb3|COI|sup|barcode=|adapter=sample_B\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=no_adapter|COI",
    )

    sample_a_row = _otu_species_row_by_sample(data, "sample_A")
    sample_b_row = _otu_species_row_by_sample(data, "sample_B")
    assert sample_a_row["frozen_otu_reads_sample_total"] == 2
    assert sample_b_row["frozen_otu_reads_sample_total"] == 3
    assert sample_a_row["otu_reads_sample_total"] == 2
    assert sample_b_row["otu_reads_sample_total"] == 3
    assert sample_a_row["frozen_otu_reads_total"] == 2
    assert sample_b_row["frozen_otu_reads_total"] == 3
    assert sample_a_row["frozen_otu_reads_global_total"] == 7
    assert sample_b_row["frozen_otu_reads_global_total"] == 7


def test_frozen_otu_reads_sample_total_collapse_mode(tmp_path: Path) -> None:
    roster = tmp_path / "samples.txt"
    roster.write_text("sample_A\n", encoding="utf-8")
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_A_2\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\ta1|COI|sup|barcode=|adapter=sample_A_1\t0\n"
            "FROZEN_h1\ta2|COI|sup|barcode=|adapter=sample_A_1\t0\n"
            "FROZEN_h1\tb1|COI|sup|barcode=|adapter=sample_A_2\t0\n"
            "FROZEN_h1\tb2|COI|sup|barcode=|adapter=sample_A_2\t0\n"
            "FROZEN_h1\tb3|COI|sup|barcode=|adapter=sample_A_2\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=no_adapter|COI",
        extra_args=["--identity-mode", "collapse", "--sample-roster", str(roster)],
    )

    collapsed_row = _otu_species_row_by_sample(data, "sample_A")
    assert collapsed_row["frozen_otu_reads_sample_total"] == 5
    assert collapsed_row["frozen_otu_reads_total"] == 5
    assert collapsed_row["frozen_otu_reads_global_total"] == 7


def test_frozen_otu_reads_sample_total_track_mode(tmp_path: Path) -> None:
    roster = tmp_path / "track_roster.tsv"
    roster.write_text("track_id\nsample_A_1\nsample_A_2\n", encoding="utf-8")
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_A_2\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\ta1|COI|sup|barcode=|adapter=sample_A_1\t0\n"
            "FROZEN_h1\ta2|COI|sup|barcode=|adapter=sample_A_1\t0\n"
            "FROZEN_h1\tb1|COI|sup|barcode=|adapter=sample_A_2\t0\n"
            "FROZEN_h1\tb2|COI|sup|barcode=|adapter=sample_A_2\t0\n"
            "FROZEN_h1\tb3|COI|sup|barcode=|adapter=sample_A_2\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=no_adapter|COI",
        extra_args=["--identity-mode", "track", "--sample-roster", str(roster)],
    )

    sample_a_row = _otu_species_row_by_sample(data, "sample_A_1")
    sample_b_row = _otu_species_row_by_sample(data, "sample_A_2")
    assert sample_a_row["frozen_otu_reads_sample_total"] == 2
    assert sample_b_row["frozen_otu_reads_sample_total"] == 3


def test_frozen_otu_reads_sample_total_keeps_no_adapter_reads(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tno_adapter\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tno_adapter_1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\ta1|COI|sup|barcode=|adapter=no_adapter|COI\t0\n"
            "FROZEN_h1\ta2|COI|sup|barcode=|adapter=no_adapter|COI\t0\n"
            "FROZEN_h1\tb1|COI|sup|barcode=|adapter=no_adapter_1\t0\n"
            "FROZEN_h1\tb2|COI|sup|barcode=|adapter=no_adapter_1\t0\n"
            "FROZEN_h1\tb3|COI|sup|barcode=|adapter=no_adapter_1\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=no_adapter|COI",
    )

    row = _otu_species_row_by_sample(data, "no_adapter")
    assert row["frozen_otu_reads_sample_total"] == 5
    assert row["frozen_otu_reads_total"] == 5
    assert row["frozen_otu_reads_global_total"] == 7


def test_frozen_otu_rows_omitted_when_frozen_state_missing(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_B\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "rep1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a1\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_B\tOTUB_F-COI\tMEMBER\n"
            "b2\tsample_B\tOTUB_F-COI\tMEMBER\n"
        ),
        lock_summary_rows="OTUB_F-COI\t0\t1\n",
    )
    assert data["otu"]["assignments_by_level"]["species"] == []


def test_frozen_otu_rows_omitted_when_frozen_state_mapping_incomplete(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_B\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "rep1\tsample_A\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a1\tsample_A\tOTUB_F-COI\tMEMBER\n"
            "b1\tsample_B\tOTUB_F-COI\tMEMBER\n"
            "b2\tsample_B\tOTUB_F-COI\tMEMBER\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\tx1|COI|sup|barcode=|adapter=other_sample\t0\n"
            "FROZEN_h1\tx2|COI|sup|barcode=|adapter=other_sample\t0\n"
        ),
        rep_hash_member_id="different_rep|COI|sup|barcode=|adapter=other_sample",
    )

    assert data["otu"]["assignments_by_level"]["species"] == []


def test_frozen_otu_reads_sample_total_absent_when_state_lacks_sample(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tno_adapter\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_def_rows=(
            "rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
            "a1\tno_adapter\tOTUB_F-COI\tMEMBER\n"
            "a2\tno_adapter\tOTUB_F-COI\tMEMBER\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\tx1|COI|sup|barcode=|adapter=other_sample\t0\n"
            "FROZEN_h1\tx2|COI|sup|barcode=|adapter=other_sample\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=no_adapter|COI",
    )

    assert not any(row.get("sample") == "no_adapter" for row in data["otu"]["assignments_by_level"]["species"])
    row = _otu_species_row_by_sample(data, "other_sample")
    assert row["frozen_otu_count"] == 1
    assert row["frozen_otu_reads_total"] == 2
    assert row["frozen_otu_reads_sample_total"] == 2
    assert row["frozen_otu_reads_global_total"] == 7


def test_frozen_otu_reads_sample_total_uses_frozen_members_when_otu_def_sample_is_wrong(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tCS.D.P_2_MPold1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_size=303,
        otu_def_rows=(
            "rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
            "m1\tGAG1.spiked_1_MPold1\tOTUB_F-COI\tMEMBER\n"
            "m2\tGAG1.spiked_1_MPold1\tOTUB_F-COI\tMEMBER\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\tm1|COI|sup|barcode=|adapter=GAG1.spiked_1_MPold1_COI\t0\n"
            "FROZEN_h1\tm2|COI|sup|barcode=|adapter=GAG1.spiked_1_MPold1_COI\t0\n"
            "FROZEN_h1\tm3|COI|sup|barcode=|adapter=CS.D.P_2_MPold1_COI\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=GAG1.spiked_1_MPold1_COI",
    )

    row = _otu_species_row_by_sample(data, "CS.D.P_MPold1")
    assert row["frozen_otu_count"] == 1
    assert row["frozen_otu_reads_total"] == 1
    assert row["frozen_otu_reads_global_total"] == 303
    assert row["frozen_otu_reads_sample_total"] == 1
    assert row["otu_reads_sample_total"] == 1


def test_frozen_otu_reads_sample_total_absent_with_empty_otu_def(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows="r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n",
        otu_def_rows="",
        lock_summary_rows="OTUB_F-COI\t0\t1\n",
    )
    assert data["otu"]["assignments_by_level"]["species"] == []


def test_frozen_otu_four_track_units_scope_isolation(tmp_path: Path) -> None:
    """Regression: per-track frozen reads must be track-scoped, not the global OTU total."""
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "track_id\nsample_T1\nsample_T2\nsample_T3\nsample_T4\n",
        encoding="utf-8",
    )
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "f_r1\tCOI\thac\tsample_T1\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "f_r2\tCOI\thac\tsample_T2\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "f_r3\tCOI\thac\tsample_T3\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "f_r4\tCOI\thac\tsample_T4\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "nf_r1\tCOI\thac\tsample_T1\thit\t123\t100\t99\tOTUB_NF-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows=(
            "rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n"
            "rep2\tno_adapter\tOTUB_NF-COI\tREPRESENTATIVE\n"
            "nf_m1\tsample_T1\tOTUB_NF-COI\tMEMBER\n"
            "nf_m2\tsample_T1\tOTUB_NF-COI\tMEMBER\n"
            "nf_m3\tsample_T1\tOTUB_NF-COI\tMEMBER\n"
            "nf_m4\tsample_T1\tOTUB_NF-COI\tMEMBER\n"
            "nf_m5\tsample_T1\tOTUB_NF-COI\tMEMBER\n"
            "nf_m6\tsample_T1\tOTUB_NF-COI\tMEMBER\n"
        ),
        frozen_member_rows=(
            "FROZEN_h1\tm1a|COI|sup|barcode=|adapter=sample_T1\t0\n"
            "FROZEN_h1\tm1b|COI|sup|barcode=|adapter=sample_T1\t0\n"
            "FROZEN_h1\tm1c|COI|sup|barcode=|adapter=sample_T1\t0\n"
            "FROZEN_h1\tm1d|COI|sup|barcode=|adapter=sample_T1\t0\n"
            "FROZEN_h1\tm1e|COI|sup|barcode=|adapter=sample_T1\t0\n"
            "FROZEN_h1\tm1f|COI|sup|barcode=|adapter=sample_T1\t0\n"
            "FROZEN_h1\tm1g|COI|sup|barcode=|adapter=sample_T1\t0\n"
            "FROZEN_h1\tm2a|COI|sup|barcode=|adapter=sample_T2\t0\n"
            "FROZEN_h1\tm2b|COI|sup|barcode=|adapter=sample_T2\t0\n"
            "FROZEN_h1\tm2c|COI|sup|barcode=|adapter=sample_T2\t0\n"
            "FROZEN_h1\tm3a|COI|sup|barcode=|adapter=sample_T3\t0\n"
            "FROZEN_h1\tm3b|COI|sup|barcode=|adapter=sample_T3\t0\n"
            "FROZEN_h1\tm3c|COI|sup|barcode=|adapter=sample_T3\t0\n"
            "FROZEN_h1\tm3d|COI|sup|barcode=|adapter=sample_T3\t0\n"
            "FROZEN_h1\tm4a|COI|sup|barcode=|adapter=sample_T4\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=no_adapter|COI",
        otu_sizes_rows="OTUB_F-COI\t15\n",
        lock_summary_rows=(
            "OTUB_F-COI\t0\t1\n"
            "OTUB_NF-COI\t0\t0\n"
        ),
        extra_args=["--identity-mode", "track", "--sample-roster", str(roster)],
    )

    r1 = _otu_species_row_by_sample(data, "sample_T1")
    r2 = _otu_species_row_by_sample(data, "sample_T2")
    r3 = _otu_species_row_by_sample(data, "sample_T3")
    r4 = _otu_species_row_by_sample(data, "sample_T4")

    assert r1["reads_total"] == 13
    assert r1["frozen_otu_reads_total"] == 7
    assert r2["frozen_otu_reads_total"] == 3
    assert r3["frozen_otu_reads_total"] == 4
    assert r4["frozen_otu_reads_total"] == 1

    for row in (r1, r2, r3, r4):
        assert row["frozen_otu_reads_total"] != 15, (
            f"track unit {row['sample']} shows global total 15; scope bleed detected"
        )
        assert row["frozen_otu_reads_sample_total"] == row["frozen_otu_reads_total"]
        assert row["frozen_otu_reads_global_total"] == 15


def test_frozen_otu_reads_invariants_hold_for_all_rows(tmp_path: Path) -> None:
    data = _run_frozen_assignment_case(
        tmp_path,
        blast_rows=(
            "r1\tCOI\thac\tsample_A\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
            "r2\tCOI\thac\tsample_B\thit\t123\t100\t99\tOTUB_F-COI\tF1\tG1\tS1\n"
        ),
        otu_def_rows="rep1\tno_adapter\tOTUB_F-COI\tREPRESENTATIVE\n",
        frozen_member_rows=(
            "FROZEN_h1\ta1|COI|sup|barcode=|adapter=sample_A\t0\n"
            "FROZEN_h1\ta2|COI|sup|barcode=|adapter=sample_A\t0\n"
            "FROZEN_h1\tb1|COI|sup|barcode=|adapter=sample_B\t0\n"
            "FROZEN_h1\tb2|COI|sup|barcode=|adapter=sample_B\t0\n"
            "FROZEN_h1\tb3|COI|sup|barcode=|adapter=sample_B\t0\n"
        ),
        rep_hash_member_id="rep1|COI|sup|barcode=|adapter=no_adapter|COI",
    )

    for level in ("species", "genus", "family"):
        for row in data["otu"]["assignments_by_level"][level]:
            ctx = f"[{level}] sample={row.get('sample')} taxon={row.get('taxon')}"
            if row.get("frozen_otu_reads_total") is not None and row.get("reads_total") is not None:
                assert row["frozen_otu_reads_total"] <= row["reads_total"], (
                    f"{ctx}: frozen_otu_reads_total > reads_total"
                )
            if row.get("frozen_otu_reads_sample_total") is not None and row.get("otu_reads_sample_total") is not None:
                assert row["frozen_otu_reads_sample_total"] <= row["otu_reads_sample_total"], (
                    f"{ctx}: frozen_otu_reads_sample_total > otu_reads_sample_total"
                )
            if row.get("frozen_otu_count", 0) > 0:
                assert row.get("frozen_otu_reads_sample_total", 0) > 0, (
                    f"{ctx}: frozen_otu_count without positive frozen sample reads"
                )
            assert row.get("frozen_otu_count", 0) <= row.get("otu_count", 0), (
                f"{ctx}: frozen_otu_count > otu_count"
            )
    assert not any("invariant:" in warning for warning in data.get("warnings", []))


def test_fate_size_streak_enforce_missing_file(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_X-COI")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-size-streak",
            str(tmp_path / "missing_size_streak.tsv"),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "2",
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["size_streak"] == 0
    assert data["otu"]["pruned"]["prune_candidates"] == 0


def test_fate_size_streak_off_missing_sizes_round(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_X-COI")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(tmp_path / "missing_sizes.tsv"),
            "--otu-blast-min-members",
            "2",
            "--blast-filter-mode",
            "enforce",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["size_streak"] is None


def test_fate_blast_unassigned_grace_active(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_X-COI")
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample\t.\tNA\t100\t99\tOTUB_X-COI\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_1\t1\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--blast-otu",
            str(blast_otu),
            "--otu-blast-unassigned-grace-rounds",
            "5",
            "--round-index-file",
            str(round_index),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["blast_unassigned"] == 0
    assert data["otu"]["pruned"]["prune_candidates"] == 1


def test_fate_size_streak_enforce_counts(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_X-COI")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("k\tCOI\tr1\tOTUB_X-COI\t1\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "1",
            "--blast-filter-mode",
            "enforce",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["size_streak"] == 1


def test_fate_blast_unassigned_beats_size_streak(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_X-COI")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("k\tCOI\tr1\tOTUB_X-COI\t1\n", encoding="utf-8")
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample\t.\tNA\t100\t99\tOTUB_X-COI\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_1\t1\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--blast-otu",
            str(blast_otu),
            "--otu-blast-unassigned-grace-rounds",
            "0",
            "--round-index-file",
            str(round_index),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "1",
            "--blast-filter-mode",
            "enforce",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["blast_unassigned"] == 1
    assert data["otu"]["pruned"]["size_streak"] == 0


def test_fate_missing_round_index_grace_defaults_to_unassigned(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_X-COI")
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample\t.\tNA\t100\t99\tOTUB_X-COI\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--blast-otu",
            str(blast_otu),
            "--otu-blast-unassigned-grace-rounds",
            "5",
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["blast_unassigned"] == 1
    assert data["otu"]["pruned"]["prune_candidates"] == 0


def test_fate_not_seen_in_blast_uses_size_bucket(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text(
        "read_id\tOTU_id\n"
        "r1\tOTUB_A-COI\n"
        "r2\tOTUB_B-COI\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_A-COI\t0\t0\n"
        "OTUB_B-COI\t0\t0\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample\t.\t123\t100\t99\tOTUB_A-COI\tF\tG\tS\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text(
        "otu_id\tsize\n"
        "OTUB_A-COI\t5\n"
        "OTUB_B-COI\t1\n",
        encoding="utf-8",
    )
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("k\tCOI\tr2\tOTUB_B-COI\t1\n", encoding="utf-8")
    blast_filter_dropped = tmp_path / "blast_filter_dropped.list"
    blast_filter_dropped.write_text("r1\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--blast-otu",
            str(blast_otu),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "1",
            "--otu-blast-min-members",
            "2",
            "--blast-filter-dropped-ids",
            str(blast_filter_dropped),
            "--blast-filter-mode",
            "enforce",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["blast_unassigned"] == 0
    assert data["otu"]["pruned"]["size_streak"] == 1
    assert data["otu"]["canonical"]["informative_dynamic"] == 1


def test_fate_universe_prefers_sizes_round(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text(
        "read_id\tOTU_id\n"
        "r1\tOTUB_A-COI\n"
        "r2\tOTUB_B-COI\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_A-COI-no_adapter_1\t0\t0\n"
        "OTUB_B-COI-no_adapter_1\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_A-COI\t5\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["canonical"]["active"] == 1
    assert data["otu"]["canonical"]["active_not_frozen"] == 1
    assert data["otu"]["canonical"]["informative_dynamic"] == 1


def test_fate_universe_fallback_warns_to_otu_def(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_A-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_A-COI")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(tmp_path / "missing_sizes.tsv"),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["canonical"]["active"] == 1
    assert "otu_fate_universe_fallback:otu_def" in data["warnings"]
    diagnostic = data["otu"]["diagnostic"]
    assert diagnostic["fate_universe_source"] == "otu_def"
    assert diagnostic["fate_universe_reason"] == "fallback_to_otu_def"
    assert diagnostic["fate_universe_sizes_status"] == "missing"
    assert diagnostic["fate_universe_otu_def_status"] == "ok"


def test_fate_universe_header_only_sizes_falls_back_to_otu_def(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text(
        "read_id\tOTU_id\n"
        "r1\tOTUB_A-COI\n"
        "r2\tOTUB_B-COI\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_A-COI")
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["canonical"]["active"] == 2
    assert data["otu"]["canonical"]["active_not_frozen"] == 2
    assert "otu_fate_universe_fallback:otu_def" in data["warnings"]
    diagnostic = data["otu"]["diagnostic"]
    assert diagnostic["fate_universe_source"] == "otu_def"
    assert diagnostic["fate_universe_reason"] == "fallback_to_otu_def"
    assert diagnostic["fate_universe_sizes_status"] == "empty"
    assert diagnostic["fate_universe_otu_def_status"] == "ok"


def test_fate_universe_fallback_to_lock_summary_when_sizes_empty_and_otu_def_missing(tmp_path: Path) -> None:
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_A-COI-no_adapter_1")
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(tmp_path / "missing_otu_def.tsv"),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["canonical"]["active"] == 1
    assert data["otu"]["canonical"]["active_not_frozen"] == 1
    assert "otu_fate_universe_fallback:lock_summary" in data["warnings"]
    diagnostic = data["otu"]["diagnostic"]
    assert diagnostic["fate_universe_source"] == "lock_summary"
    assert diagnostic["fate_universe_reason"] == "fallback_to_lock"
    assert diagnostic["fate_universe_sizes_status"] == "empty"
    assert diagnostic["fate_universe_otu_def_status"] == "missing"


def test_fate_universe_strict_empty_round_no_lock_fallback(tmp_path: Path) -> None:
    lock_summary = tmp_path / "lock.tsv"
    _write_lock_summary(lock_summary, "OTUB_A-COI-no_adapter_1")
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\n", encoding="utf-8")
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    canonical = data["otu"]["canonical"]
    pruned = data["otu"]["pruned"]
    diagnostic = data["otu"]["diagnostic"]
    assert canonical["active"] == 0
    assert canonical["active_not_frozen"] == 0
    assert canonical["informative_dynamic"] == 0
    assert pruned["prune_candidates"] == 0
    assert pruned["size_streak"] == 0
    assert pruned["blast_unassigned"] == 0
    assert diagnostic["fate_universe_source"] == "none"
    assert diagnostic["fate_universe_reason"] == "strict_empty_round"
    assert diagnostic["fate_universe_sizes_status"] == "empty"
    assert diagnostic["fate_universe_otu_def_status"] == "empty"
    assert diagnostic["fate_conservation_ok"] is True
    assert diagnostic["fate_conservation_delta"] is None
    assert "otu_fate_universe_empty:strict_round" not in data["warnings"]
    assert "otu_fate_universe_fallback:lock_summary" not in data["warnings"]


def test_fate_lock_suffix_matches_base_otu_ids(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_X-COI-no_adapter_1\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_X-COI\t1\n", encoding="utf-8")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("k\tCOI\tr1\tOTUB_X-COI\t1\n", encoding="utf-8")
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample\t.\t123\t100\t99\tOTUB_X-COI\tF\tG\tS\n",
        encoding="utf-8",
    )
    blast_filter_dropped = tmp_path / "blast_filter_dropped.list"
    blast_filter_dropped.write_text("r1\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--blast-otu",
            str(blast_otu),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "1",
            "--otu-blast-min-members",
            "2",
            "--blast-filter-dropped-ids",
            str(blast_filter_dropped),
            "--blast-filter-mode",
            "enforce",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["size_streak"] == 1
    assert data["otu"]["pruned"]["blast_unassigned"] == 0
    assert data["otu"]["canonical"]["informative_dynamic"] == 0


def test_fate_lock_key_unrecognized_warning_emitted_once_per_file(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_A-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_A-XYZ-no_adapter_1\t0\t0\n"
        "OTUB_B-XYZ-no_adapter_2\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_A-COI\t1\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    warnings = [w for w in data["warnings"] if w.startswith("otu_lock_key_unrecognized_format:")]
    assert len(warnings) == 1


def test_size_streak_applies_after_skip_window(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_X-COI\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_X-COI\t1\n", encoding="utf-8")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("k\tCOI\tr1\tOTUB_X-COI\t3\n", encoding="utf-8")
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_4\t4\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_4",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "3",
            "--otu-blast-filter-skip-rounds",
            "3",
            "--round-index-file",
            str(round_index),
            "--blast-filter-mode",
            "enforce",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["size_streak"] == 1
    assert data["otu"]["pruned"]["prune_candidates"] == 0


def test_size_streak_moves_to_prune_candidates_when_not_applied(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\nr1\tOTUB_X-COI\n", encoding="utf-8")
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_X-COI\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_X-COI\t1\n", encoding="utf-8")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("k\tCOI\tr1\tOTUB_X-COI\t1\n", encoding="utf-8")
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_5\t2\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_5",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "1",
            "--blast-filter-mode",
            "enforce",
            "--otu-blast-filter-skip-rounds",
            "3",
            "--round-index-file",
            str(round_index),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["pruned"]["size_streak"] == 0
    assert data["otu"]["pruned"]["prune_candidates"] == 1


def test_fate_conservation_sums_to_active_not_frozen(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text(
        "read_id\tOTU_id\n"
        "r1\tOTUB_A-COI\n"
        "r2\tOTUB_B-COI\n"
        "r3\tOTUB_C-COI\n"
        "r4\tOTUB_D-COI\n"
        "r5\tOTUB_E-COI\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_A-COI\t0\t0\n"
        "OTUB_B-COI\t0\t0\n"
        "OTUB_C-COI\t0\t0\n"
        "OTUB_D-COI\t0\t0\n"
        "OTUB_E-COI\t0\t0\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample\t.\tNA\t100\t99\tOTUB_A-COI\tUnassigned\tUnassigned\tUnassigned\n"
        "r2\tCOI\thac\tsample\t.\t123\t100\t99\tOTUB_E-COI\tF\tG\tS\n",
        encoding="utf-8",
    )
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text(
        "k\tCOI\tr2\tOTUB_B-COI\t2\n"
        "k\tCOI\tr3\tOTUB_D-COI\t1\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text(
        "otu_id\tsize\n"
        "OTUB_A-COI\t5\n"
        "OTUB_B-COI\t5\n"
        "OTUB_C-COI\t1\n"
        "OTUB_D-COI\t1\n"
        "OTUB_E-COI\t5\n",
        encoding="utf-8",
    )
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_1\t1\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(lock_summary),
            "--blast-otu",
            str(blast_otu),
            "--otu-size-streak",
            str(size_streak_state),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "2",
            "--blast-filter-mode",
            "enforce",
            "--otu-blast-unassigned-grace-rounds",
            "0",
            "--round-index-file",
            str(round_index),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    pruned = data["otu"]["pruned"]
    canonical = data["otu"]["canonical"]
    total = (
        (pruned["blast_unassigned"] or 0)
        + (pruned["size_streak"] or 0)
        + (pruned["prune_candidates"] or 0)
        + (canonical["informative_dynamic"] or 0)
    )
    assert total == canonical["active_not_frozen"]
    diagnostic = data["otu"]["diagnostic"]
    assert diagnostic["fate_conservation_ok"] is True
    assert diagnostic["fate_conservation_delta"] is None

def test_report_round_json_emits_sample_metrics(tmp_path: Path) -> None:
    demult = tmp_path / "demult.tsv"
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\thac\tsample_A_1\n"
        "r2\tCOI\thac\tsample_A_1\n"
        "r3\tITS2\tsup\tsample_B_2\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\totu_id\n"
        "r1\tCOI\thac\tsample_A_1\tOTUB_1-COI\n"
        "r2\tCOI\thac\tsample_A_1\tOTUB_2-COI\n"
        "r3\tITS2\tsup\tsample_B_2\tOTUB_3-ITS2\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\n"
        "c1\tCOI\tconsensus\t10\tsample_A_1\n"
        "c2\tITS2\tconsensus\t6\tsample_B_2\n",
        encoding="utf-8",
    )
    consensus_round_prov = tmp_path / "consensus_round_provenance.tsv"
    consensus_round_prov.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "output_round_1\tsample_A_1\tOTUB_1-COI\tc1\t5\n"
        "output_round_1\tsample_B_2\tOTUB_2-ITS2\tc2\t4\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--demult",
            str(demult),
            "--blast-otu",
            str(blast_otu),
            "--blast-consensus",
            str(blast_cons),
            "--consensus-round-provenance",
            str(consensus_round_prov),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    sm = data["sample_metrics"]
    by_label = {v.get("label"): v for v in sm.values()}
    # Replicate suffixes are stripped: sample_A_1 → sample_A, sample_B_2 → sample_B
    assert "sample_A" in by_label
    assert "sample_B" in by_label
    assert by_label["sample_A"]["sample_id"].startswith("sample_a_")
    assert by_label["sample_A"]["reads_demux"] == 2
    assert by_label["sample_A"]["reads_blast_assigned"] is None
    assert by_label["sample_A"]["otu_active"] == 2
    assert by_label["sample_A"]["consensus_emitted"] == 1
    # Per-replicate data preserved in replicates sub-object
    reps_a = by_label["sample_A"].get("replicates", {})
    assert "sample_A_1" in reps_a
    assert reps_a["sample_A_1"]["reads_demux"] == 2
    assert reps_a["sample_A_1"]["otu_active"] == 2
    assert reps_a["sample_A_1"]["consensus_emitted"] == 1
    read_fate = data["read_fate"]
    assert read_fate["demux_total_reads"] == 3
    assert read_fate["no_adapter_reads"] == 0
    assert read_fate["marker_split_status"] == "invalid"
    assert read_fate["blast_seen_reads"] == 3
    assert read_fate["blast_assigned_reads"] is None
    assert read_fate["blast_unassigned_reads"] is None
    assert read_fate["blast_seen_reads_unbucketed"] == 0
    assert read_fate["demux_enabled"] is None
    assert "missing_required_global_total" in read_fate["data_reason_codes"]
    assert "missing_required_stage_total" in read_fate["data_reason_codes"]


def test_report_round_json_uses_passed_demult_input(tmp_path: Path) -> None:
    demult_round = tmp_path / "demult_round.tsv"
    demult_round.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\thac\tsample_A_1\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--demult",
            str(demult_round),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    by_label = {v.get("label"): v for v in data["sample_metrics"].values()}
    assert "sample_A" in by_label
    assert "sample_B" not in by_label
    assert by_label["sample_A"]["reads_demux"] == 1
    assert data["read_fate"]["demux_total_reads"] == 1


def test_report_round_json_read_fate_uses_first_seen_inputs_only(tmp_path: Path) -> None:
    read_info = tmp_path / "read_info.tsv"
    read_info.write_text(
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        "r_old\tf\tR\tb\t100\t10\t100\t12\tNA\tNA\n"
        "r_new\tf\tR\tb\t101\t10\t101\t12\tNA\tNA\n",
        encoding="utf-8",
    )
    on_target = tmp_path / "on_target.tsv"
    on_target.write_text(
        "read_id\tbarcode\ton_target_kingdom\n"
        "r_old\tb\tON_TARGET\n"
        "r_new\tb\tON_TARGET\n",
        encoding="utf-8",
    )
    demult = tmp_path / "demult.tsv"
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r_old\tCOI\thac\tsample_A_1\n"
        "r_new\tCOI\thac\tsample_A_1\n",
        encoding="utf-8",
    )
    read_fate_demult = tmp_path / "read_fate_demult.tsv"
    read_fate_demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r_new\tCOI\thac\tsample_A_1\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\ttaxid\totu_id\totu_family\totu_genus\totu_species\n"
        "r_old\tCOI\thac\tsample_A_1\t123\tOTUB_1-COI\tF\tG\tS\n"
        "r_new\tCOI\thac\tsample_A_1\tNA\tOTUB_2-COI\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    read_fate_blast = tmp_path / "read_fate_blast.tsv"
    read_fate_blast.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\ttaxid\totu_id\totu_family\totu_genus\totu_species\n"
        "r_new\tCOI\thac\tsample_A_1\tNA\tOTUB_2-COI\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_2",
            "--out",
            str(out),
            "--read-info",
            str(read_info),
            "--on-target",
            str(on_target),
            "--demult",
            str(demult),
            "--read-fate-demult",
            str(read_fate_demult),
            "--blast-otu",
            str(blast_otu),
            "--read-fate-blast",
            str(read_fate_blast),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    by_label = {v.get("label"): v for v in data["sample_metrics"].values()}
    assert by_label["sample_A"]["reads_demux"] == 2
    assert by_label["sample_A"]["reads_blast_assigned"] == 1
    read_fate = data["read_fate"]
    assert read_fate["marker_split_status"] == "ok"
    assert read_fate["demux_total_reads"] == 1
    assert read_fate["blast_seen_reads"] == 1
    assert read_fate["blast_assigned_reads"] == 0
    assert read_fate["blast_unassigned_reads"] == 1


def test_report_round_json_falls_back_when_first_seen_inputs_missing(tmp_path: Path) -> None:
    demult = tmp_path / "demult.tsv"
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\thac\tsample_A_1\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\ttaxid\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\t123\tOTUB_1-COI\tF\tG\tS\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--demult",
            str(demult),
            "--read-fate-demult",
            str(tmp_path / "missing_read_fate_demult.tsv"),
            "--blast-otu",
            str(blast_otu),
            "--read-fate-blast",
            str(tmp_path / "missing_read_fate_blast.tsv"),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    read_fate = data["read_fate"]
    assert read_fate["demux_total_reads"] == 1
    assert read_fate["blast_seen_reads"] == 1
    assert read_fate["blast_assigned_reads"] == 1


def test_report_round_json_seeds_sample_metrics_from_roster(tmp_path: Path) -> None:
    demult = tmp_path / "demult.tsv"
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\thac\tsample_A_1\n"
        "r2\tITS2\thac\tsample_B_1\n",
        encoding="utf-8",
    )
    roster = tmp_path / "samples.txt"
    roster.write_text(
        "sample_A\tsample_A_1\n"
        "sample_B\tsample_B_1\n"
        "sample_C\tsample_C_1\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--demult",
            str(demult),
            "--sample-roster",
            str(roster),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    by_label = {v.get("label"): v for v in data["sample_metrics"].values()}
    assert sorted(by_label) == ["sample_A", "sample_B", "sample_C"]
    assert by_label["sample_C"]["reads_demux"] is None
    assert by_label["sample_C"]["otu_active"] is None
    reps_c = by_label["sample_C"].get("replicates", {})
    assert "sample_C_1" in reps_c
    assert reps_c["sample_C_1"]["reads_demux"] is None


def test_report_round_json_missing_consensus_provenance_sets_null(tmp_path: Path) -> None:
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--consensus-round-provenance",
            str(tmp_path / "missing_consensus_round_provenance.tsv"),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "consensus_used_reads" not in data["read_fate"]
    assert any("missing_or_empty:" in w for w in data["warnings"])


def test_report_round_json_header_only_consensus_provenance_sets_null(tmp_path: Path) -> None:
    prov = tmp_path / "consensus_round_provenance.tsv"
    prov.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--consensus-round-provenance",
            str(prov),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "consensus_used_reads" not in data["read_fate"]
    assert not any("missing_or_empty_data_rows:" in w for w in data["warnings"])


def test_report_round_json_optional_early_round_inputs_do_not_emit_noise_warnings(tmp_path: Path) -> None:
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\n", encoding="utf-8")
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\tOTU_id\n", encoding="utf-8")
    otu_size_streak = tmp_path / "otu_size_streak.tsv"
    otu_size_streak.write_text("", encoding="utf-8")
    otu_members_blastdiag_stats = tmp_path / "otu_members_blastdiag_stats.tsv"
    otu_members_blastdiag_stats.write_text("", encoding="utf-8")
    consensus_round_provenance = tmp_path / "consensus_round_provenance.tsv"
    consensus_round_provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
            "--otu-lock-summary",
            str(tmp_path / "missing_otu_lock_summary.tsv"),
            "--otu-members-blastdiag-stats",
            str(otu_members_blastdiag_stats),
            "--otu-size-streak",
            str(otu_size_streak),
            "--otu-size-streak-mode",
            "enforce",
            "--otu-size-streak-min-rounds",
            "2",
            "--otu-sizes-round",
            str(otu_sizes_round),
            "--consensus-round-provenance",
            str(consensus_round_provenance),
            "--blast-filter-mode",
            "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    warnings = data["warnings"]
    assert not any("missing_otu_lock_summary.tsv" in w for w in warnings)
    assert not any("otu_members_blastdiag_stats.tsv" in w for w in warnings)
    assert not any("otu_size_streak.tsv" in w for w in warnings)
    assert not any("missing_or_empty_data_rows:" in w for w in warnings)
    assert "otu_fate_universe_empty:strict_round" not in warnings
    assert not any(w.startswith("size_streak_inputs_missing:") for w in warnings)
    diagnostic = data["otu"]["diagnostic"]
    assert diagnostic["fate_universe_reason"] == "strict_empty_round"
    assert diagnostic["fate_universe_sizes_status"] == "empty"
    assert diagnostic["fate_universe_otu_def_status"] == "empty"


def test_report_round_json_na_consensus_provenance_sets_null(tmp_path: Path) -> None:
    prov = tmp_path / "consensus_round_provenance.tsv"
    prov.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "output_round_1\tsample_A_1\tOTUB_1-COI\tc1\tNA\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--consensus-round-provenance",
            str(prov),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "consensus_used_reads" not in data["read_fate"]
    assert any("missing_numeric_reads_used_round:" in w for w in data["warnings"])


def test_report_round_json_sample_id_stable_for_colliding_normalized_labels(tmp_path: Path) -> None:
    demult_a = tmp_path / "demult_a.tsv"
    demult_b = tmp_path / "demult_b.tsv"
    demult_a.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\thac\tSample-1\n"
        "r2\tCOI\thac\tSample 1\n",
        encoding="utf-8",
    )
    demult_b.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r2\tCOI\thac\tSample 1\n"
        "r1\tCOI\thac\tSample-1\n",
        encoding="utf-8",
    )
    out_a = tmp_path / "round_a.json"
    out_b = tmp_path / "round_b.json"

    result_a = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "round_001",
            "--out",
            str(out_a),
            "--demult",
            str(demult_a),
        ]
    )
    result_b = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "round_002",
            "--out",
            str(out_b),
            "--demult",
            str(demult_b),
        ]
    )
    assert result_a.returncode == 0, result_a.stderr
    assert result_b.returncode == 0, result_b.stderr

    data_a = json.loads(out_a.read_text(encoding="utf-8"))
    data_b = json.loads(out_b.read_text(encoding="utf-8"))
    map_a = {v["label"]: v["sample_id"] for v in data_a["sample_metrics"].values()}
    map_b = {v["label"]: v["sample_id"] for v in data_b["sample_metrics"].values()}
    assert map_a == map_b
    assert map_a["Sample-1"] != map_a["Sample 1"]


def test_report_round_json_normalizes_no_adapter_samples(tmp_path: Path) -> None:
    demult = tmp_path / "demult.tsv"
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\thac\tno_adapter\n"
        "r2\tCOI\thac\tno_adapter_1\n"
        "r3\tCOI\thac\tNO_ADAPTER_2\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\totu_id\n"
        "r1\tCOI\thac\tno_adapter\tOTUB_1-COI\n"
        "r2\tCOI\thac\tno_adapter_1\tOTUB_1-COI\n"
        "r3\tCOI\thac\tNO_ADAPTER_2\tOTUB_2-COI\n",
        encoding="utf-8",
    )
    roster = tmp_path / "samples.txt"
    roster.write_text(
        "sample_A\tsample_A_1\n"
        "no_adapter\tno_adapter_1\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--demult",
            str(demult),
            "--blast-otu",
            str(blast_otu),
            "--sample-roster",
            str(roster),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    sm = data["sample_metrics"]
    by_label = {v["label"]: v for v in sm.values()}
    assert "no_adapter" in by_label
    assert "sample_A" in by_label
    entry = by_label["no_adapter"]
    assert entry["reads_demux"] == 3
    assert entry["reads_blast_assigned"] is None
    assert entry["otu_active"] == 2
    assert by_label["sample_A"]["reads_demux"] is None
    read_fate = data["read_fate"]
    assert read_fate["demux_total_reads"] == 3
    assert read_fate["no_adapter_reads"] == 3
    assert read_fate["marker_split_status"] == "invalid"
    assert read_fate["blast_seen_reads"] == 3
    assert read_fate["blast_assigned_reads"] is None
    assert read_fate["blast_unassigned_reads"] is None
    assert read_fate["blast_seen_reads_unbucketed"] == 0
    assert read_fate["demux_enabled"] is None


def test_report_round_json_no_adapter_pipe_marker_merged(tmp_path: Path) -> None:
    """no_adapter|COI and no_adapter|ITS2 in demult must collapse into the same
    sample_metrics entry as no_adapter reads from the BLAST report."""
    demult = tmp_path / "demult.tsv"
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\tsup\tno_adapter|COI\n"
        "r2\tCOI\tsup\tno_adapter|COI\n"
        "r3\tITS2\tsup\tno_adapter|ITS2\n",
        encoding="utf-8",
    )
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\totu_id\n"
        "r1\tCOI\tsup\tno_adapter\tOTUB_1-COI\n"
        "r2\tCOI\tsup\tno_adapter\tOTUB_1-COI\n"
        "r3\tITS2\tsup\tno_adapter\tOTUB_2-ITS2\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id", "runA",
            "--barcode", "RTBioScan",
            "--round-barcode", "output_round_1",
            "--out", str(out),
            "--demult", str(demult),
            "--blast-otu", str(blast_otu),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    sm = data["sample_metrics"]
    by_label = {v["label"]: v for v in sm.values()}
    # All three raw labels must collapse into a single 'no_adapter' entry
    assert list(by_label.keys()) == ["no_adapter"], f"Unexpected sample labels: {list(by_label.keys())}"
    entry = by_label["no_adapter"]
    assert entry["reads_demux"] == 3
    assert entry["reads_demux_coi"] == 2
    assert entry["reads_demux_its2"] == 1
    assert entry["otu_active"] == 2


def test_report_round_json_blast_read_fate_dedup_any_assigned_wins(tmp_path: Path) -> None:
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\ttaxid\totu_id\totu_family\totu_genus\totu_species\n"
        "r1|COI|hac|barcode=bc1|adapter=sample_A_1|OTUB_1-COI\tCOI\thac\tsample_A_1\tNA\tOTUB_1-COI\tUnassigned\tUnassigned\tUnassigned\n"
        "r1\tCOI\thac\tsample_A_1\t123\tOTUB_1-COI\tF\tG\tS\n"
        "r2\tCOI\thac\tsample_A_1\tNA\tOTUB_2-COI\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--blast-otu",
            str(blast_otu),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    read_fate = data["read_fate"]
    assert read_fate["marker_split_status"] == "invalid"
    assert read_fate["blast_seen_reads"] == 2
    assert read_fate["blast_assigned_reads"] == 1
    assert read_fate["blast_unassigned_reads"] == 1
    assert read_fate["blast_seen_reads_unbucketed"] == 0
    sm = data["sample_metrics"]
    entry = next(iter(sm.values()))
    assert entry["reads_blast_assigned"] == 1


def test_report_round_json_blast_read_fate_tracks_adapter_no_adapter_split(tmp_path: Path) -> None:
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\ttaxid\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\t123\tOTUB_1-COI\tF\tG\tS\n"
        "r2\tCOI\thac\tsample_A_1\tNA\tOTUB_2-COI\tUnassigned\tUnassigned\tUnassigned\n"
        "r3\tCOI\thac\tno_adapter\tNA\tOTUB_3-COI\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--blast-otu",
            str(blast_otu),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    read_fate = data["read_fate"]
    assert read_fate["marker_split_status"] == "invalid"
    assert read_fate["blast_seen_reads"] == 3
    assert read_fate["blast_assigned_reads"] == 1
    assert read_fate["blast_unassigned_reads"] == 2
    assert read_fate["blast_seen_reads_unbucketed"] == 0


def test_report_round_json_blast_read_fate_empty_samples_excluded_but_literal_unknown_bucketed(tmp_path: Path) -> None:
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\ttaxid\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\t\t123\tOTUB_1-COI\tF\tG\tS\n"
        "r2\tCOI\thac\tunknown\tNA\tOTUB_2-COI\tUnassigned\tUnassigned\tUnassigned\n"
        "r3\tCOI\thac\tno_adapter\tNA\tOTUB_3-COI\tUnassigned\tUnassigned\tUnassigned\n"
        "r4\tCOI\thac\tsample_A_1\t456\tOTUB_4-COI\tF\tG\tS\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--blast-otu",
            str(blast_otu),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    read_fate = data["read_fate"]
    assert read_fate["marker_split_status"] == "invalid"
    assert read_fate["blast_seen_reads"] == 4
    assert read_fate["blast_seen_reads_unbucketed"] == 2
    assert read_fate["blast_assigned_reads"] == 2
    assert read_fate["blast_unassigned_reads"] == 2


def test_report_round_json_attaches_sample_figures(tmp_path: Path) -> None:
    demult = tmp_path / "demult.tsv"
    demult.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
        "r1\tCOI\thac\tno_adapter_1\n",
        encoding="utf-8",
    )
    sample_id = f"no_adapter_{hashlib.sha1('no_adapter'.encode('utf-8')).hexdigest()[:8]}"
    sample_fig_list = tmp_path / "figures_sample.tsv"
    sample_fig_list.write_text(
        "# id\tfilename\ttitle\tdescription\tsection\torder\n"
        "sample_reads_per_sample\t{sample_id}_reads_per_sample.png\tReads per Sample\tDemo\tDemultiplexing\t10\n"
        "sample_reads_per_sample_log\t{sample_id}_reads_per_sample_log.png\tReads per Sample (log)\tDemo\tDemultiplexing\t20\n",
        encoding="utf-8",
    )
    sample_fig_dir = tmp_path / "report_assets" / "samples" / sample_id
    sample_fig_dir.mkdir(parents=True)
    (sample_fig_dir / f"{sample_id}_reads_per_sample.png").write_text("fake", encoding="utf-8")
    (sample_fig_dir / f"{sample_id}_reads_per_sample.pdf").write_text("fake", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--demult",
            str(demult),
            "--sample-fig-list",
            str(sample_fig_list),
            "--sample-fig-dir",
            str(tmp_path / "report_assets" / "samples"),
            "--sample-fig-url-prefix",
            "runs/runA/report_assets/samples",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    sm = data["sample_metrics"]
    assert len(sm) == 1
    entry = next(iter(sm.values()))
    figs = entry.get("figures", [])
    assert len(figs) == 2
    by_id = {f["id"]: f for f in figs}
    assert by_id["sample_reads_per_sample"]["exists"] is True
    assert by_id["sample_reads_per_sample_log"]["exists"] is False
    assert by_id["sample_reads_per_sample"]["path"] == f"runs/runA/report_assets/samples/{sample_id}/{sample_id}_reads_per_sample.png"
    assert by_id["sample_reads_per_sample"]["pdf_exists"] is True
    assert by_id["sample_reads_per_sample"]["pdf_path"] == f"runs/runA/report_assets/samples/{sample_id}/{sample_id}_reads_per_sample.pdf"


def test_report_round_json_missing_input_warns(tmp_path: Path) -> None:
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--read-info",
            str(tmp_path / "missing.tsv"),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["reads"]["total"] is None
    assert any(w.startswith("missing_or_empty:") for w in data["warnings"])


def test_report_round_json_otu_column_fallback_lowercase(tmp_path: Path) -> None:
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text("read_id\totu_id\nr1\tOTUB_1-COI\nr2\tOTUB_1-COI\nr3\tOTUB_2-COI\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--otu-def",
            str(otu_def),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["otu"]["canonical"]["active"] == 2


def test_report_round_json_includes_figures(tmp_path: Path) -> None:
    fig_list = tmp_path / "figures.tsv"
    fig_list.write_text(
        "reads_time\t{barcode}_reads_time.png\tReads vs Time\tTotal reads over time\tGlobal Overview\t10\n",
        encoding="utf-8",
    )
    fig_dir = tmp_path / "assets"
    fig_dir.mkdir()
    (fig_dir / "RTBioScan_reads_time.png").write_text("fake", encoding="utf-8")
    (fig_dir / "RTBioScan_reads_time.pdf").write_text("fake", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--fig-list",
            str(fig_list),
            "--fig-dir",
            str(fig_dir),
            "--fig-url-prefix",
            "report_assets",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert "figures" in data
    assert data["figures"][0]["exists"] is True
    assert data["figures"][0]["path"] == "report_assets/RTBioScan_reads_time.png"
    assert data["figures"][0]["pdf_exists"] is True
    assert data["figures"][0]["pdf_path"] == "report_assets/RTBioScan_reads_time.pdf"
    assert data["figures"][0]["section"] == "Global Overview"
    assert data["figures"][0]["order"] == 10


def test_report_round_json_trims_section_order(tmp_path: Path) -> None:
    fig_list = tmp_path / "figures.tsv"
    fig_list.write_text(
        "reads_time\t{barcode}_reads_time.png\tReads vs Time\tTotal reads over time\t Global Overview \t 10 \n",
        encoding="utf-8",
    )
    fig_dir = tmp_path / "report_assets"
    fig_dir.mkdir()
    (fig_dir / ".copied_manifest.tsv").write_text("RTBioScan_reads_time.png\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--fig-list",
            str(fig_list),
            "--fig-dir",
            str(fig_dir),
            "--fig-url-prefix",
            "report_assets",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["figures"][0]["section"] == "Global Overview"
    assert data["figures"][0]["order"] == 10


def test_report_round_json_uses_manifest_for_exists(tmp_path: Path) -> None:
    fig_list = tmp_path / "figures.tsv"
    fig_list.write_text(
        "reads_time\t{barcode}_reads_time.png\tReads vs Time\tTotal reads over time\n",
        encoding="utf-8",
    )
    fig_dir = tmp_path / "report_assets"
    fig_dir.mkdir()
    (fig_dir / ".copied_manifest.tsv").write_text("RTBioScan_reads_time.png\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--run-id",
            "runA",
            "--barcode",
            "RTBioScan",
            "--round-barcode",
            "output_round_1",
            "--out",
            str(out),
            "--fig-list",
            str(fig_list),
            "--fig-dir",
            str(fig_dir),
            "--fig-url-prefix",
            "report_assets",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["figures"][0]["exists"] is True


def test_assignments_sample_marker_suffix_stripped(tmp_path: Path) -> None:
    """Sample names in OTU and Consensus Assignments tables must not include the
    _COI / _ITS2 marker suffix that the blast reports may carry."""
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid"
        "\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tMySample_COI\thit\t123\t400\t98.0\tOTUB_1-COI\tF\tG\tSp\n"
        "r2\tITS2\thac\tMySample_ITS2\thit\t456\t350\t97.0\tOTUB_2-ITS2\tF\tG\tSp\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample"
        "\ttaxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum"
        "\tconsensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "Cons1\tCOI\tconsensus\t10\tMySample_COI\t123\thit\t400\t98.0\tK\tP\tC\tO\tF\tG\tSp\n"
        "Cons2\tITS2\tconsensus\t8\tMySample_ITS2\t456\thit\t350\t97.0\tK\tP\tC\tO\tF\tG\tSp\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_1-COI\t0\t0\n"
        "OTUB_2-ITS2\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes = tmp_path / "otu_sizes.tsv"
    otu_sizes.write_text("otu_id\tsize\nOTUB_1-COI\t5\nOTUB_2-ITS2\t4\n", encoding="utf-8")
    consolidated = tmp_path / "cons_ids.txt"
    consolidated.write_text("", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--barcode", "RTBioScan",
            "--round-barcode", "round_1",
            "--run-id", "testrun",
            "--out", str(out),
            "--blast-otu", str(blast_otu),
            "--otu-sizes-round", str(otu_sizes),
            "--blast-consensus", str(blast_cons),
            "--otu-lock-summary", str(lock_summary),
            "--consensus-consolidated-ids", str(consolidated),
            "--blast-filter-mode", "off",
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))

    # OTU Assignments: sample must be 'MySample' not 'MySample_COI' / 'MySample_ITS2'
    otu_rows = data["otu"]["assignments_by_level"]["species"]
    otu_samples = {r["sample"] for r in otu_rows}
    assert otu_samples == {"MySample"}, f"OTU assignment samples wrong: {otu_samples}"

    # Consensus Assignments: same expectation
    cons_rows = data["consensus"]["assignments_by_level"]["species"]
    cons_samples = {r["sample"] for r in cons_rows}
    assert cons_samples == {"MySample"}, f"Consensus assignment samples wrong: {cons_samples}"


def test_assignments_by_level_track_mode_isolates_track_units(tmp_path: Path) -> None:
    """In track mode, assignments_by_level must emit separate rows per track unit.
    Two replicates (sample_A_1, sample_A_2) with different species must NOT be
    merged into a single 'sample_A' row."""
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid"
        "\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t101\t400\t98.0\tOTUB_1-COI\tF\tG\tSpecies_alpha\n"
        "r2\tCOI\thac\tsample_A_2\thit\t102\t400\t98.0\tOTUB_2-COI\tF\tG\tSpecies_beta\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample"
        "\ttaxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum"
        "\tconsensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "Cons1\tCOI\tconsensus\t10\tsample_A_1\t101\thit\t400\t98.0\tK\tP\tC\tO\tF\tG\tSpecies_alpha\n"
        "Cons2\tCOI\tconsensus\t8\tsample_A_2\t102\thit\t400\t98.0\tK\tP\tC\tO\tF\tG\tSpecies_beta\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_1-COI\t0\t0\n"
        "OTUB_2-COI\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes = tmp_path / "otu_sizes.tsv"
    otu_sizes.write_text(
        "otu_id\tsize\nOTUB_1-COI\t5\nOTUB_2-COI\t4\n", encoding="utf-8"
    )
    consolidated = tmp_path / "cons_ids.txt"
    consolidated.write_text("", encoding="utf-8")
    roster = tmp_path / "roster.tsv"
    roster.write_text("track_id\n" "sample_A_1\n" "sample_A_2\n", encoding="utf-8")
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--barcode", "RTBioScan",
            "--round-barcode", "round_1",
            "--run-id", "testrun",
            "--out", str(out),
            "--blast-otu", str(blast_otu),
            "--otu-sizes-round", str(otu_sizes),
            "--blast-consensus", str(blast_cons),
            "--otu-lock-summary", str(lock_summary),
            "--consensus-consolidated-ids", str(consolidated),
            "--blast-filter-mode", "off",
            "--identity-mode", "track",
            "--sample-roster", str(roster),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))

    # OTU: two track units must appear as separate rows, not merged
    otu_rows = data["otu"]["assignments_by_level"]["species"]
    otu_by_sample = {r["sample"]: r["species"] for r in otu_rows if isinstance(r, dict)}
    assert "sample_A_1" in otu_by_sample, f"sample_A_1 missing from OTU rows: {otu_by_sample}"
    assert "sample_A_2" in otu_by_sample, f"sample_A_2 missing from OTU rows: {otu_by_sample}"
    assert otu_by_sample["sample_A_1"] == "Species_alpha", f"Wrong species for sample_A_1: {otu_by_sample}"
    assert otu_by_sample["sample_A_2"] == "Species_beta", f"Wrong species for sample_A_2: {otu_by_sample}"
    assert "sample_A" not in otu_by_sample, f"Collapsed key 'sample_A' must not appear in track mode: {otu_by_sample}"

    # Consensus: same isolation requirement
    cons_rows = data["consensus"]["assignments_by_level"]["species"]
    cons_by_sample = {r["sample"]: r["species"] for r in cons_rows if isinstance(r, dict)}
    assert "sample_A_1" in cons_by_sample, f"sample_A_1 missing from consensus rows: {cons_by_sample}"
    assert "sample_A_2" in cons_by_sample, f"sample_A_2 missing from consensus rows: {cons_by_sample}"
    assert cons_by_sample["sample_A_1"] == "Species_alpha", f"Wrong species for sample_A_1: {cons_by_sample}"
    assert cons_by_sample["sample_A_2"] == "Species_beta", f"Wrong species for sample_A_2: {cons_by_sample}"
    assert "sample_A" not in cons_by_sample, f"Collapsed key 'sample_A' must not appear in track mode: {cons_by_sample}"


def test_reads_total_counts_all_rows_including_malformed(tmp_path: Path) -> None:
    """reads.total must equal all non-blank data rows; reads.hac/sup count only valid-value rows."""
    read_info = tmp_path / "read_info.tsv"
    # 5 data rows:
    #   row1: hac_length=99  (valid hac), sup_length=NA (invalid sup)
    #   row2: hac_length=108 (valid hac), sup_length=107 (valid sup)
    #   row3: hac_length=NA  (invalid hac), sup_length=NA (invalid sup)
    #   row4: hac_length=    (empty = invalid hac), sup_length= (empty = invalid sup)
    #   row5: hac_length=NA  (NA = invalid hac), sup_length=NA (NA = invalid sup)
    #         but this row still counts toward reads.total
    read_info.write_text(
        "read_id\thac_length\tsup_length\n"
        "r1\t99\tNA\n"
        "r2\t108\t107\n"
        "r3\tNA\tNA\n"
        "r4\t\t\n"
        "r5\tNA\tNA\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run([
        "--barcode", "RTBioScan",
        "--round-barcode", "round_1",
        "--run-id", "testrun",
        "--out", str(out),
        "--read-info", str(read_info),
    ])
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    reads = data["reads"]
    assert reads["total"] == 5, f"reads.total expected 5, got {reads['total']}"
    assert reads["hac"] == 2, f"reads.hac expected 2, got {reads['hac']}"
    # row2 has valid sup_length; rows 1,3,4,5 do not
    assert reads["sup"] == 1, f"reads.sup expected 1, got {reads['sup']}"


# ---------------------------------------------------------------------------
# Identity mode tests
# ---------------------------------------------------------------------------

def _make_identity_scenario(tmp_path: Path, blast_samples: list[tuple[str, str]]):
    """Create minimum required files for identity-mode tests.

    Returns (base_args, out_path).  Caller appends roster / identity-mode args.
    blast_samples: list of (read_id, sample_label) for blast_otu rows.
    """
    blast_otu = tmp_path / "blast_otu.tsv"
    rows = "\n".join(
        f"{rid}\tCOI\thac\t{smp}\thit\t123\t100\t99\tOTUB_1-COI\tF1\tG1\tS1"
        for rid, smp in blast_samples
    )
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\t"
        "aln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n" + rows + "\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\nOTUB_1-COI\t0\t0\n",
        encoding="utf-8",
    )
    size_streak_stats = tmp_path / "size_streak_stats.tsv"
    size_streak_stats.write_text("reads_prune_candidate\t0\notus_prune_candidate\t0\n", encoding="utf-8")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("", encoding="utf-8")
    blast_filter = tmp_path / "blast_filter.tsv"
    blast_filter.write_text(
        "kept_reads\t1\nkept_otus\t1\ndropped_otus\t0\nmissing_policy\tdrop\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text("otu_id\tsize\nOTUB_1-COI\t2\n", encoding="utf-8")
    blastdiag = tmp_path / "blastdiag.tsv"
    blastdiag.write_text("rows_total\t1\notu_total\t1\n", encoding="utf-8")
    blast_filter_dropped = tmp_path / "blast_filter_dropped.list"
    blast_filter_dropped.write_text("", encoding="utf-8")
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_1\t1\n", encoding="utf-8")
    active_prune_counts = tmp_path / "active_prune_counts.tsv"
    active_prune_counts.write_text(
        "active_total\t1\nsize_streak_active\t0\nsize_streak_candidates\t0\nunion\t0\n"
        "active_scope\tround\nactive_scope_reason\tround_local_ok\n"
        "size_streak_input_status\tready\nsize_streak_possible\t0\nsize_streak_applied\t0\n"
        "size_streak_disabled\t0\neffective_mode\toff\neffective_reason\twithin_skip_window\n"
        "round_index\t1\nsize_streak_round_candidates\t0\nsize_streak_eligible_candidates\t0\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    base_args = [
        "--run-id", "testrun",
        "--barcode", "RTBioScan",
        "--round-barcode", "output_round_1",
        "--out", str(out),
        "--blast-otu", str(blast_otu),
        "--otu-sizes-round", str(otu_sizes_round),
        "--otu-lock-summary", str(lock_summary),
        "--otu-size-streak-stats", str(size_streak_stats),
        "--otu-size-streak", str(size_streak_state),
        "--otu-size-streak-mode", "off",
        "--otu-size-streak-min-rounds", "3",
        "--otu-blast-filter-stats", str(blast_filter),
        "--blast-filter-dropped-ids", str(blast_filter_dropped),
        "--blast-filter-mode", "enforce",
        "--otu-blast-min-members", "1",
        "--otu-blast-filter-skip-rounds", "none",
        "--otu-blast-unassigned-grace-rounds", "0",
        "--round-index-file", str(round_index),
        "--otu-members-blastdiag-stats", str(blastdiag),
        "--active-prune-counts", str(active_prune_counts),
    ]
    return base_args, out


def test_collapse_mode_unchanged(tmp_path: Path) -> None:
    """Collapse mode: multiple replicate labels aggregate to one base sample entry."""
    base_args, out = _make_identity_scenario(
        tmp_path, [("r1", "sample_A_1"), ("r2", "sample_A_2")]
    )
    roster = tmp_path / "samples.txt"
    roster.write_text("sample_A\n", encoding="utf-8")
    result = _run(base_args + [
        "--identity-mode", "collapse",
        "--sample-roster", str(roster),
    ])
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    metrics = data["sample_metrics"]
    assert len(metrics) == 1, f"expected 1 sample entry, got {len(metrics)}: {list(metrics)}"
    entry = next(iter(metrics.values()))
    assert entry["label"] == "sample_A"


def test_track_mode_separation(tmp_path: Path) -> None:
    """Track mode: each track unit gets its own entry; no collapse to base sample."""
    base_args, out = _make_identity_scenario(
        tmp_path, [("r1", "CS.D.P_1_MPnew1"), ("r2", "CS.D.P_2_MPnew1")]
    )
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "sample_id\ttrack_id\n"
        "CS.D.P\tCS.D.P_1_MPnew1\n"
        "CS.D.P\tCS.D.P_2_MPnew1\n",
        encoding="utf-8",
    )
    result = _run(base_args + [
        "--identity-mode", "track",
        "--sample-roster", str(roster),
    ])
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    metrics = data["sample_metrics"]
    assert len(metrics) == 2, f"expected 2 track entries, got {len(metrics)}: {list(metrics)}"
    labels = {e["label"] for e in metrics.values()}
    assert labels == {"CS.D.P_1_MPnew1", "CS.D.P_2_MPnew1"}, f"unexpected labels: {labels}"


def test_track_mode_marker_qualified_labels_match_track_roster(tmp_path: Path) -> None:
    """Track mode: terminal marker suffixes should resolve to the roster track unit."""
    base_args, out = _make_identity_scenario(
        tmp_path,
        [("r1", "CS.D.P_1_MPnew1_COI"), ("r2", "CS.D.P_1_MPnew1_ITS2")],
    )
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "sample_id\ttrack_id\n"
        "CS.D.P\tCS.D.P_1_MPnew1\n",
        encoding="utf-8",
    )
    result = _run(base_args + [
        "--identity-mode", "track",
        "--sample-roster", str(roster),
    ])
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    metrics = data["sample_metrics"]
    assert len(metrics) == 1, f"expected 1 track entry, got {len(metrics)}: {list(metrics)}"
    entry = next(iter(metrics.values()))
    assert entry["label"] == "CS.D.P_1_MPnew1"
    assert entry["reads_blast_assigned"] == 2
    assert entry["otu_active"] == 1


def test_track_mode_emits_track_unit_metrics_and_canonical_assignment_fields(tmp_path: Path) -> None:
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t101\t400\t98.0\tOTU_A-COI\tF\tG\tSpecies_alpha\n"
        "r2\tITS2\thac\tsample_A_1\thit\t102\t400\t97.5\tOTU_B-ITS2\tF\tG\tSpecies_beta\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\ttaxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\tconsensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "Cons1\tCOI\tconsensus\t10\tsample_A_1\t101\thit\t400\t98.0\tK\tP\tC\tO\tF\tG\tSpecies_alpha\n"
        "Cons2\tITS2\tconsensus\t8\tsample_A_1\t102\thit\t400\t97.5\tK\tP\tC\tO\tF\tG\tSpecies_beta\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTU_A-COI\t0\t0\n"
        "OTU_B-ITS2\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes = tmp_path / "otu_sizes.tsv"
    otu_sizes.write_text(
        "otu_id\tsize\nOTU_A-COI\t5\nOTU_B-ITS2\t4\n",
        encoding="utf-8",
    )
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "sample_id\ttrack_id\treplicate_number\n"
        "sample_A\tsample_A_1\t1\n",
        encoding="utf-8",
    )
    track_identity = tmp_path / "track_identity.tsv"
    track_identity.write_text(
        "sample_id\ttrack_id\treplicate_number\tmarker_id\tunit_id_track\n"
        "sample_A\tsample_A_1\t1\tCOI\tsample_A_1_COI\n"
        "sample_A\tsample_A_1\t1\tITS2\tsample_A_1_ITS2\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--barcode", "RTBioScan",
            "--round-barcode", "round_1",
            "--run-id", "testrun",
            "--out", str(out),
            "--blast-otu", str(blast_otu),
            "--otu-sizes-round", str(otu_sizes),
            "--blast-consensus", str(blast_cons),
            "--otu-lock-summary", str(lock_summary),
            "--blast-filter-mode", "off",
            "--identity-mode", "track",
            "--sample-roster", str(roster),
            "--track-identity", str(track_identity),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))

    sample_metrics = data["sample_metrics"]
    assert len(sample_metrics) == 1
    sample_entry = next(iter(sample_metrics.values()))
    assert sample_entry["label"] == "sample_A_1"

    tum = data["track_unit_metrics"]
    assert sorted(tum) == ["sample_A_1_COI", "sample_A_1_ITS2"]
    assert tum["sample_A_1_COI"]["track_sample_label"] == "sample_A"
    assert tum["sample_A_1_COI"]["track_replicate_id"] == "sample_A_1"
    assert tum["sample_A_1_COI"]["track_replicate_number"] == 1
    assert tum["sample_A_1_COI"]["track_replicate_label"] == "sample_A_1"
    assert tum["sample_A_1_COI"]["track_sample_replicate_label"] == "sample_A_1"
    assert tum["sample_A_1_COI"]["track_primer_label"] == "COI"
    assert tum["sample_A_1_COI"]["reads_demux"] is None
    assert tum["sample_A_1_COI"]["reads_blast_assigned"] == 1
    assert tum["sample_A_1_COI"]["otu_active"] == 1
    assert tum["sample_A_1_COI"]["consensus_emitted"] == 1
    assert tum["sample_A_1_ITS2"]["track_primer_label"] == "ITS2"

    otu_rows = data["otu"]["assignments_by_level"]["species"]
    otu_by_unit = {row["track_unit_id"]: row for row in otu_rows}
    assert otu_by_unit["sample_A_1_COI"]["sample"] == "sample_A_1"
    assert otu_by_unit["sample_A_1_COI"]["track_sample_label"] == "sample_A"
    assert otu_by_unit["sample_A_1_COI"]["track_replicate_number"] == 1
    assert otu_by_unit["sample_A_1_COI"]["track_sample_replicate_label"] == "sample_A_1"
    assert otu_by_unit["sample_A_1_COI"]["track_primer_label"] == "COI"
    assert otu_by_unit["sample_A_1_ITS2"]["track_primer_label"] == "ITS2"

    cons_rows = data["consensus"]["assignments_by_level"]["species"]
    cons_by_unit = {row["track_unit_id"]: row for row in cons_rows}
    assert cons_by_unit["sample_A_1_COI"]["track_sample_replicate_label"] == "sample_A_1"
    assert cons_by_unit["sample_A_1_COI"]["track_primer_label"] == "COI"
    assert cons_by_unit["sample_A_1_ITS2"]["track_primer_label"] == "ITS2"


def test_track_mode_accepts_sparse_track_identity_metrics(tmp_path: Path) -> None:
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t101\t400\t98.0\tOTU_A-COI\tF\tG\tSpecies_alpha\n",
        encoding="utf-8",
    )
    blast_cons = tmp_path / "blast_cons.tsv"
    blast_cons.write_text(
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\ttaxid\tblast_hit\taln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\tconsensus_class\tconsensus_order\tconsensus_family\tconsensus_genus\tconsensus_species\n"
        "Cons1\tCOI\tconsensus\t10\tsample_A_1\t101\thit\t400\t98.0\tK\tP\tC\tO\tF\tG\tSpecies_alpha\n",
        encoding="utf-8",
    )
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTU_A-COI\t0\t0\n",
        encoding="utf-8",
    )
    otu_sizes = tmp_path / "otu_sizes.tsv"
    otu_sizes.write_text("otu_id\tsize\nOTU_A-COI\t5\n", encoding="utf-8")
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "sample_id\ttrack_id\treplicate_number\n"
        "sample_A\tsample_A_1\t1\n",
        encoding="utf-8",
    )
    track_identity = tmp_path / "track_identity.tsv"
    track_identity.write_text(
        "sample_id\ttrack_id\treplicate_number\tmarker_id\tunit_id_track\n"
        "sample_A\tsample_A_1\t1\tCOI\tsample_A_1_COI\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--barcode", "RTBioScan",
            "--round-barcode", "round_1",
            "--run-id", "testrun",
            "--out", str(out),
            "--blast-otu", str(blast_otu),
            "--otu-sizes-round", str(otu_sizes),
            "--blast-consensus", str(blast_cons),
            "--otu-lock-summary", str(lock_summary),
            "--blast-filter-mode", "off",
            "--identity-mode", "track",
            "--sample-roster", str(roster),
            "--track-identity", str(track_identity),
        ]
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))

    tum = data["track_unit_metrics"]
    assert sorted(tum) == ["sample_A_1_COI"]
    assert "sample_A_1_ITS2" not in tum
    assert tum["sample_A_1_COI"]["track_primer_label"] == "COI"
    assert tum["sample_A_1_COI"]["reads_blast_assigned"] == 1
    assert tum["sample_A_1_COI"]["otu_active"] == 1
    assert tum["sample_A_1_COI"]["consensus_emitted"] == 1


def test_track_mode_duplicate_track_marker_mapping_fails(tmp_path: Path) -> None:
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "sample_id\ttrack_id\treplicate_number\n"
        "sample_A\tsample_A_1\t1\n",
        encoding="utf-8",
    )
    track_identity = tmp_path / "track_identity.tsv"
    track_identity.write_text(
        "sample_id\ttrack_id\treplicate_number\tmarker_id\tunit_id_track\n"
        "sample_A\tsample_A_1\t1\tCOI\tsample_A_1_COI\n"
        "sample_A\tsample_A_1\t1\tCOI\tsample_A_1_COI_alt\n",
        encoding="utf-8",
    )
    out = tmp_path / "round_report.json"
    result = _run(
        [
            "--barcode", "RTBioScan",
            "--round-barcode", "round_1",
            "--run-id", "testrun",
            "--out", str(out),
            "--identity-mode", "track",
            "--sample-roster", str(roster),
            "--track-identity", str(track_identity),
        ]
    )
    assert result.returncode != 0
    assert "maps track_id 'sample_A_1' and marker 'COI' to multiple unit_id_track values" in result.stderr


def test_track_mode_missing_roster(tmp_path: Path) -> None:
    """Track mode without --sample-roster must fail immediately."""
    base_args, out = _make_identity_scenario(tmp_path, [("r1", "CS.D.P_1_MPnew1")])
    result = _run(base_args + ["--identity-mode", "track"])
    assert result.returncode != 0
    assert "requires --sample-roster" in result.stderr


def test_track_mode_roster_mismatch(tmp_path: Path) -> None:
    """Track mode: observed identity not in roster must fail hard."""
    base_args, out = _make_identity_scenario(tmp_path, [("r1", "unknown_track_unit")])
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "sample_id\ttrack_id\nCS.D.P\tCS.D.P_1_MPnew1\n",
        encoding="utf-8",
    )
    result = _run(base_args + [
        "--identity-mode", "track",
        "--sample-roster", str(roster),
    ])
    assert result.returncode != 0
    assert "not in the track roster" in result.stderr


def test_track_mode_mixed_identity_guard(tmp_path: Path) -> None:
    """Track mode: mixing collapsed-style label with track labels must fail hard."""
    base_args, out = _make_identity_scenario(
        tmp_path, [("r1", "CS.D.P_1_MPnew1"), ("r2", "CS.D.P")]
    )
    roster = tmp_path / "track_roster.tsv"
    roster.write_text(
        "sample_id\ttrack_id\nCS.D.P\tCS.D.P_1_MPnew1\n",
        encoding="utf-8",
    )
    result = _run(base_args + [
        "--identity-mode", "track",
        "--sample-roster", str(roster),
    ])
    assert result.returncode != 0  # CS.D.P not in roster → guard fires


def test_track_mode_incompatible_roster_format(tmp_path: Path) -> None:
    """Track mode: roster with no 'track_id' column must fail hard."""
    base_args, out = _make_identity_scenario(tmp_path, [("r1", "CS.D.P_1_MPnew1")])
    roster = tmp_path / "bad_roster.tsv"
    roster.write_text(
        "sample_id\tother_col\nCS.D.P\t1\n",
        encoding="utf-8",
    )
    result = _run(base_args + [
        "--identity-mode", "track",
        "--sample-roster", str(roster),
    ])
    assert result.returncode != 0


def test_informative_barplot_uses_cumulative_blast(tmp_path: Path) -> None:
    """Regression: consolidated OTU absent from current-round blast but present
    in cumulative blast must count as assigned in otu.active_by_marker_taxon."""
    # Current-round blast: only OTUB_2-COI (OTUB_1-COI has dropped out,
    # simulating post-pruning behaviour).
    blast_otu = tmp_path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r3\tCOI\thac\tsample_B_2\thit\t234\t100\t98\tOTUB_2-COI\tF2\tG2\tS2\n",
        encoding="utf-8",
    )
    # Cumulative blast: both OTUs with valid taxids.
    blast_otu_cum = tmp_path / "blast_otu_cum.tsv"
    blast_otu_cum.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        "r1\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_1-COI\tF1\tG1\tS1\n"
        "r3\tCOI\thac\tsample_B_2\thit\t234\t100\t98\tOTUB_2-COI\tF2\tG2\tS2\n",
        encoding="utf-8",
    )
    # Lock: OTUB_1-COI is consolidated; OTUB_2-COI is active_not_frozen.
    lock_summary = tmp_path / "lock.tsv"
    lock_summary.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        "OTUB_1-COI\t1\t0\n"
        "OTUB_2-COI\t0\t0\n",
        encoding="utf-8",
    )
    otu_def = tmp_path / "otu_def.tsv"
    otu_def.write_text(
        "read_id\tOTU_id\nr1\tOTUB_1-COI\nr2\tOTUB_1-COI\nr3\tOTUB_2-COI\n",
        encoding="utf-8",
    )
    otu_sizes_round = tmp_path / "otu_sizes_round.tsv"
    otu_sizes_round.write_text(
        "otu_id\tsize\nOTUB_1-COI\t2\nOTUB_2-COI\t1\n",
        encoding="utf-8",
    )
    round_index = tmp_path / "round_index.tsv"
    round_index.write_text("round_barcode\tround_index\noutput_round_5\t5\n", encoding="utf-8")
    active_prune_counts = tmp_path / "active_prune_counts.tsv"
    active_prune_counts.write_text(
        "active_total\t1\nsize_streak_active\t0\nsize_streak_candidates\t0\n"
        "union\t0\nactive_scope\tround\nactive_scope_reason\tround_local_ok\n"
        "size_streak_input_status\tready\nsize_streak_possible\t1\n"
        "size_streak_applied\t1\nsize_streak_disabled\t0\n"
        "effective_mode\tenforce\neffective_reason\tapplied\n"
        "round_index\t5\nsize_streak_round_candidates\t0\nsize_streak_eligible_candidates\t0\n",
        encoding="utf-8",
    )
    size_streak_stats = tmp_path / "size_streak_stats.tsv"
    size_streak_stats.write_text("reads_prune_candidate\t0\notus_prune_candidate\t0\n", encoding="utf-8")
    size_streak_state = tmp_path / "otu_size_streak.tsv"
    size_streak_state.write_text("", encoding="utf-8")
    out = tmp_path / "round_report.json"

    result = _run([
        "--run-id", "runA",
        "--barcode", "RTBioScan",
        "--round-barcode", "output_round_5",
        "--out", str(out),
        "--otu-def", str(otu_def),
        "--blast-otu", str(blast_otu),
        "--blast-otu-cumulative", str(blast_otu_cum),
        "--otu-sizes-round", str(otu_sizes_round),
        "--otu-lock-summary", str(lock_summary),
        "--active-prune-counts", str(active_prune_counts),
        "--otu-size-streak-stats", str(size_streak_stats),
        "--otu-size-streak", str(size_streak_state),
        "--otu-size-streak-mode", "enforce",
        "--otu-size-streak-min-rounds", "2",
        "--otu-blast-filter-skip-rounds", "none",
        "--otu-blast-unassigned-grace-rounds", "0",
        "--otu-blast-min-members", "1",
        "--round-index-file", str(round_index),
    ])
    assert result.returncode == 0, result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    otu_break = data["otu"]["active_by_marker_taxon"]
    # Both consolidated (OTUB_1-COI) and active_not_frozen (OTUB_2-COI) must be
    # counted as assigned because both appear in the cumulative blast with valid
    # taxids. Before the fix, coi_assigned was 1 (current-round blast, missing
    # the consolidated OTU).
    assert otu_break["coi_assigned"] == 2, (
        f"Expected 2 COI assigned (1 consolidated + 1 active from cumulative blast), "
        f"got {otu_break['coi_assigned']}"
    )
    assert otu_break["coi_unassigned"] == 0
