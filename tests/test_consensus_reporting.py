from pathlib import Path


def test_consolidated_report_header_matches_canonical(tmp_path: Path) -> None:
    canonical = (
        "consensus_id\tbarcode_by_homology\tbasecalling_model\t"
        "number_of_reads\tsample\ttaxid\tblast_hit\t"
        "aln_length\tperc_id\tconsensus_kingdom\tconsensus_phylum\t"
        "consensus_class\tconsensus_order\tconsensus_family\t"
        "consensus_genus\tconsensus_species\n"
    )
    consolidated = tmp_path / "RTBioScan_blast_consensus_tax_consolidated_rpt.txt"
    consolidated.write_text(canonical, encoding="utf-8")
    assert consolidated.read_text(encoding="utf-8") == canonical
