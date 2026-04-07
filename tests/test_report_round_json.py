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


def _write_lock_summary(path: Path, otu_key: str) -> None:
    path.write_text(
        "otu_key\teffective_consolidated\tis_frozen\n"
        f"{otu_key}\t0\t0\n",
        encoding="utf-8",
    )


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
    assert data["schema_version"] == "1.6"
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
    assert assignments["species"][0]["reads_total"] == 5
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
    assert "otu_fate_universe_empty:strict_round" in data["warnings"]
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
    assert any("missing_or_empty_data_rows:" in w for w in data["warnings"])


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
    assert "track_id" in result.stderr
