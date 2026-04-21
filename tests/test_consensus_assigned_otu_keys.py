import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_assigned_otu_keys.pl"


def test_consensus_assigned_otu_keys_maps_assigned_consensus_to_otu_keys(tmp_path: Path) -> None:
    blast_report = tmp_path / "consensus_blast_report_full.txt"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    out = tmp_path / "consensus_assigned_otu_keys_round.list"

    blast_report.write_text(
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
        "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\t1234\tMetazoa\tChordata\tAves\tOrderA\tFamilyA\tGenusA\tSpeciesA\n"
        "sampleA|Consensus2|OTU=OTUB_2-COI|best=1\tNA\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "round1\tsampleA\tOTUB_1-COI\tConsensus1_sampleA\t10\n"
        "round1\tsampleA\tOTUB_2-COI\tConsensus2_sampleA\t5\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--blast-report",
            str(blast_report),
            "--provenance",
            str(provenance),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["OTUB_1-COI"]


def test_consensus_assigned_otu_keys_does_not_collapse_distinct_no_adapter_like_barcodes(tmp_path: Path) -> None:
    blast_report = tmp_path / "consensus_blast_report_full.txt"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    out = tmp_path / "consensus_assigned_otu_keys_round.list"

    blast_report.write_text(
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
        "no_adapter_1a|Consensus0|OTU=OTUB_A|best=1\t1234\tMetazoa\tChordata\tAves\tOrderA\tFamilyA\tGenusA\tSpeciesA\n"
        "no_adapter_2b|Consensus0|OTU=OTUB_B|best=1\tNA\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "round1\tno_adapter_1a\tOTUB_A\tConsensus0_no_adapter_1a\t10\n"
        "round1\tno_adapter_2b\tOTUB_B\tConsensus0_no_adapter_2b\t5\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--blast-report",
            str(blast_report),
            "--provenance",
            str(provenance),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["OTUB_A"]


def test_consensus_assigned_otu_keys_requires_long_seq_id_header(tmp_path: Path) -> None:
    blast_report = tmp_path / "consensus_blast_report_full.txt"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    out = tmp_path / "consensus_assigned_otu_keys_round.list"

    blast_report.write_text(
        "wrong_id\tfamily\tgenus\tspecies\n"
        "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\tFamilyA\tGenusA\tSpeciesA\n",
        encoding="utf-8",
    )
    provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "round1\tsampleA\tOTUB_1-COI\tConsensus1_sampleA\t10\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--blast-report",
            str(blast_report),
            "--provenance",
            str(provenance),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode != 0
    assert "missing required column 'long_seq_id'" in cp.stderr


def test_consensus_assigned_otu_keys_requires_provenance_headers(tmp_path: Path) -> None:
    blast_report = tmp_path / "consensus_blast_report_full.txt"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    out = tmp_path / "consensus_assigned_otu_keys_round.list"

    blast_report.write_text(
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
        "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\t1234\tMetazoa\tChordata\tAves\tOrderA\tFamilyA\tGenusA\tSpeciesA\n",
        encoding="utf-8",
    )
    provenance.write_text(
        "round_barcode\tsample\twrong_otu_key\twrong_consensus_id\treads_used_round\n"
        "round1\tsampleA\tOTUB_1-COI\tConsensus1_sampleA\t10\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--blast-report",
            str(blast_report),
            "--provenance",
            str(provenance),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode != 0
    assert "missing required column 'consensus_id'" in cp.stderr


def test_consensus_assigned_otu_keys_validates_provenance_headers_even_without_assigned_consensus(tmp_path: Path) -> None:
    blast_report = tmp_path / "consensus_blast_report_full.txt"
    provenance = tmp_path / "consensus_round_provenance.tsv"
    out = tmp_path / "consensus_assigned_otu_keys_round.list"

    blast_report.write_text(
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
        "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\tNA\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\tUnassigned\n",
        encoding="utf-8",
    )
    provenance.write_text(
        "round_barcode\tsample\twrong_otu_key\twrong_consensus_id\treads_used_round\n"
        "round1\tsampleA\tOTUB_1-COI\tConsensus1_sampleA\t10\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--blast-report",
            str(blast_report),
            "--provenance",
            str(provenance),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode != 0
    assert "missing required column 'consensus_id'" in cp.stderr


def test_consensus_assigned_otu_keys_requires_existing_provenance_file(tmp_path: Path) -> None:
    blast_report = tmp_path / "consensus_blast_report_full.txt"
    out = tmp_path / "consensus_assigned_otu_keys_round.list"
    missing_provenance = tmp_path / "missing_consensus_round_provenance.tsv"

    blast_report.write_text(
        "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
        "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\t1234\tMetazoa\tChordata\tAves\tOrderA\tFamilyA\tGenusA\tSpeciesA\n",
        encoding="utf-8",
    )

    cp = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--blast-report",
            str(blast_report),
            "--provenance",
            str(missing_provenance),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert cp.returncode != 0
    assert f"open {missing_provenance}" in cp.stderr


# ---------------------------------------------------------------------------
# --min-level genus and species
# ---------------------------------------------------------------------------

def _run_with_level(tmp_path, blast_text, prov_text, level):
    blast_report = tmp_path / "br.txt"
    provenance = tmp_path / "prov.tsv"
    out = tmp_path / "out.list"
    blast_report.write_text(blast_text, encoding="utf-8")
    provenance.write_text(prov_text, encoding="utf-8")
    return subprocess.run(
        [
            "perl", str(SCRIPT),
            "--blast-report", str(blast_report),
            "--provenance", str(provenance),
            "--out", str(out),
            "--min-level", level,
        ],
        capture_output=True, text=True, check=False,
    ), out


_BLAST_HDR = "long_seq_id\tconsensus_taxid\tkingdom\tphylum\tclass\torder\tfamily\tgenus\tspecies\n"
_PROV_HDR = "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
_PROV_ROW = "round1\tsampleA\tOTUB_1-COI\tConsensus1_sampleA\t10\n"


def test_consensus_assigned_genus_level_family_only_not_included(tmp_path: Path) -> None:
    """At genus level, family-only assignment is not sufficient → OTU not included."""
    blast_text = (
        _BLAST_HDR
        + "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\t1234\tM\tA\tB\tC\tFamilyA\tUnassigned\tUnassigned\n"
    )
    cp, out = _run_with_level(tmp_path, blast_text, _PROV_HDR + _PROV_ROW, "genus")
    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == []


def test_consensus_assigned_genus_level_genus_included(tmp_path: Path) -> None:
    """At genus level, genus non-empty → OTU included."""
    blast_text = (
        _BLAST_HDR
        + "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\t1234\tM\tA\tB\tC\tFamilyA\tGenusA\tUnassigned\n"
    )
    cp, out = _run_with_level(tmp_path, blast_text, _PROV_HDR + _PROV_ROW, "genus")
    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == ["OTUB_1-COI"]


def test_consensus_assigned_species_level_genus_only_not_included(tmp_path: Path) -> None:
    """At species level, genus-only assignment is not sufficient → OTU not included."""
    blast_text = (
        _BLAST_HDR
        + "sampleA|Consensus1|OTU=OTUB_1-COI|best=1\t1234\tM\tA\tB\tC\tFamilyA\tGenusA\tUnassigned\n"
    )
    cp, out = _run_with_level(tmp_path, blast_text, _PROV_HDR + _PROV_ROW, "species")
    assert cp.returncode == 0, cp.stderr
    assert out.read_text(encoding="utf-8").splitlines() == []
