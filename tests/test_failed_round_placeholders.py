import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER_DIR = REPO_ROOT / "bin" / "report_placeholders" / "failed_round"
SIDECAR_SCRIPT = REPO_ROOT / "bin" / "reporting_contract_sidecar.pl"

EXPECTED_SHARED_CONTENT = {
    "blast_otu_pretax_rpt.txt": (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\t"
        "otu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
    ),
    "blast_otu_noadapter_rpt.txt": (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\t"
        "otu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
    ),
    "read_info_rpt.txt": (
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\t"
        "sup_length\tsup_mean_qscore\n"
    ),
    "blast_filter_stats.tsv": "kept_reads\t0\nkept_otus\t0\nmissing_policy\tfailed_round_placeholder\n",
    "blast_consensus_tax_rpt.txt": (
        "consensus_id\tbarcode_by_homology\tbasecalling_model\tnumber_of_reads\tsample\ttaxid\tblast_hit\t"
        "aln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\tconsensus_class\tconsensus_order\t"
        "consensus_family\tconsensus_genus\tconsensus_species\n"
    ),
    "consensus_round_provenance.tsv": "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n",
    "on_target_rpt.txt": (
        "read_id\tqc_filter\tbarcode\tkingdom\tkingdom_perc_identity\tkingdom_aln_length\ton_target_kingdom\n"
    ),
    "blast_report_annotated.txt": "qseqid,sseqid,evalue,length,pident\n",
    "consensus_blast_report_full.txt": "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n",
    "otu_def_rpt.txt": (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\t"
        "replicate\tidentity_scope\tidentity_value\tOTU_id\tOTU_role\n"
    ),
    "otu_members_round.tsv": "otu_id\tread_id\n",
    "otu_sizes_round.tsv": "otu_id\tsize\n",
    "demult_rpt.txt": (
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\t"
        "replicate\tidentity_scope\tidentity_value\n"
    ),
}


def test_failed_round_placeholder_assets_exist_and_match_expected_contents() -> None:
    for relative_name, expected in EXPECTED_SHARED_CONTENT.items():
        path = PLACEHOLDER_DIR / relative_name
        assert path.is_file(), path
        assert path.read_text(encoding="utf-8") == expected


def test_failed_round_placeholder_sidecars_match_canonical_contracts(tmp_path: Path) -> None:
    report_map = {
        "demult_rpt": PLACEHOLDER_DIR / "demult_rpt.txt",
        "otu_def_rpt": PLACEHOLDER_DIR / "otu_def_rpt.txt",
    }
    for context in ("full_collapse", "primers_only", "full_track", "off"):
        for report_kind, report_path in report_map.items():
            generated = tmp_path / f"{context}.{report_kind}.contract.tsv"
            subprocess.run(
                [
                    "perl",
                    str(SIDECAR_SCRIPT),
                    report_kind,
                    context,
                    str(report_path),
                    str(generated),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            committed = PLACEHOLDER_DIR / context / f"{report_kind}.contract.tsv"
            assert committed.is_file(), committed
            assert committed.read_text(encoding="utf-8") == generated.read_text(encoding="utf-8")
