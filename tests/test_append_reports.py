import csv
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APPEND_REPORTS = REPO_ROOT / "bin" / "append_reports.pl"

DEMUX_SAMPLE = "sample_demo_alpha_1"
NOADAPTER_SAMPLE = "no_adapter_alpha_beta_1"
BARCODE = "RTBioScan"
ROUND_DIR = "round_001"
DEMUX_IDENTITY_CONTEXT = "full_collapse"

READ_INFO_HEADER = [
    "read_id",
    "filename",
    "run_id",
    "barcode",
    "fast_length",
    "fast_mean_qscore",
    "hac_length",
    "hac_mean_qscore",
    "sup_length",
    "sup_mean_qscore",
]

ON_TARGET_HEADER = ["read_id", "barcode", "on_target_kingdom"]

BLAST_OTU_HEADER = [
    "read_id",
    "barcode_by_homology",
    "basecalling_model",
    "sample",
    "hit_id",
    "taxid",
    "aln_length",
    "perc_id",
    "otu_id",
    "otu_taxid",
    "otu_kingdom",
    "otu_phylum",
    "otu_class",
    "otu_order",
    "otu_family",
    "otu_genus",
    "otu_species",
]

CONSENSUS_HEADER = [
    "read_id",
    "sample",
    "consensus_id",
    "number_of_reads",
    "consensus_kingdom",
    "consensus_class",
    "consensus_order",
    "consensus_family",
    "consensus_genus",
    "consensus_species",
]

CONSENSUS_RPT_HEADER = [
    "consensus_id",
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

DEMULT_REPORT_HEADER = [
    "read_id",
    "barcode_by_homology",
    "basecalling_model",
    "sample",
    "platform",
    "sampling_method",
    "subsample",
    "replicate",
    "identity_scope",
    "identity_value",
]

OTU_REPORT_HEADER = DEMULT_REPORT_HEADER + ["OTU_id", "OTU_role"]


def write_tsv(path: Path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=header,
            delimiter="\t",
            lineterminator="\n",
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_contract_sidecar(path: Path, report_kind: str, header, rows, context: str):
    header_line = "\t".join(header)
    row_count = len(rows)
    meta = {
        "contract_version": "1",
        "report_kind": report_kind,
        "context": context,
        "row_count": str(row_count),
        "empty_contract": "genuinely_empty" if row_count == 0 else "nonempty",
        "header_sha1": hashlib.sha1(header_line.encode("utf-8")).hexdigest(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        for key in (
            "contract_version",
            "report_kind",
            "context",
            "row_count",
            "empty_contract",
            "header_sha1",
        ):
            handle.write(f"{key}\t{meta[key]}\n")


def write_boundary_report_pair(path: Path, report_kind: str, header, rows, context: str):
    write_tsv(path, header, rows)
    write_contract_sidecar(path.with_suffix(".contract.tsv"), report_kind, header, rows, context)


def make_demult_rows(total_reads: int):
    return [
        {
            "read_id": "demux_boundary_row",
            "barcode_by_homology": str(total_reads),
            "basecalling_model": "hac",
            "sample": DEMUX_SAMPLE,
            "platform": "nanopore",
            "sampling_method": "grab",
            "subsample": "sub1",
            "replicate": "1",
            "identity_scope": "sample",
            "identity_value": DEMUX_SAMPLE,
        }
    ]


def make_otu_rows(blast_otu_rows):
    rows = []
    for row in blast_otu_rows:
        rows.append(
            {
                "read_id": row["read_id"],
                "barcode_by_homology": row["barcode_by_homology"],
                "basecalling_model": row["basecalling_model"],
                "sample": row["sample"],
                "platform": "nanopore",
                "sampling_method": "grab",
                "subsample": "sub1",
                "replicate": "1",
                "identity_scope": "sample",
                "identity_value": row["sample"],
                "OTU_id": row["otu_id"],
                "OTU_role": "member",
            }
        )
    return rows


def sequencing_template(path: Path):
    path.write_text(
        "run_id\ttime\tstage\treads\n"
        f"{BARCODE}\t0\tfast\t0\n",
        encoding="utf-8",
    )


def base_inputs(include_noadapter: bool):
    read_info_rows = [
        {
            "read_id": "demux_read",
            "filename": "round.pod5",
            "run_id": "run001",
            "barcode": DEMUX_SAMPLE,
            "fast_length": 100,
            "fast_mean_qscore": 10,
            "hac_length": 100,
            "hac_mean_qscore": 12,
            "sup_length": 100,
            "sup_mean_qscore": 15,
        }
    ]
    on_target_rows = [
        {"read_id": "demux_read", "barcode": DEMUX_SAMPLE, "on_target_kingdom": "ON_TARGET"}
    ]
    blast_otu_rows = [
        {
            "read_id": "demux_read",
            "barcode_by_homology": "bc1",
            "basecalling_model": "hac",
            "sample": DEMUX_SAMPLE,
            "hit_id": "hit1",
            "taxid": "111",
            "aln_length": 500,
            "perc_id": 99.1,
            "otu_id": "otu1",
            "otu_taxid": "201",
            "otu_kingdom": "Metazoa",
            "otu_phylum": "Chordata",
            "otu_class": "Aves",
            "otu_order": "Accipitriformes",
            "otu_family": "Accipitridae",
            "otu_genus": "GenusDemux",
            "otu_species": "SpeciesDemux",
        }
    ]
    consensus_rows = [
        {
            "read_id": "demux_read",
            "sample": DEMUX_SAMPLE,
            "consensus_id": "cons1",
            "number_of_reads": 12,
            "consensus_kingdom": "Metazoa",
            "consensus_class": "Aves",
            "consensus_order": "Accipitriformes",
            "consensus_family": "Accipitridae",
            "consensus_genus": "GenusDemux",
            "consensus_species": "SpeciesDemux",
        }
    ]
    # Add a second consensus cluster for the same taxon so consensus reporting can sum
    # read support across multiple clusters for the same assignment.
    consensus_rows.append(
        {
            "read_id": "demux_read_2",
            "sample": DEMUX_SAMPLE,
            "consensus_id": "cons1b",
            "number_of_reads": 3,
            "consensus_kingdom": "Metazoa",
            "consensus_class": "Aves",
            "consensus_order": "Accipitriformes",
            "consensus_family": "Accipitridae",
            "consensus_genus": "GenusDemux",
            "consensus_species": "SpeciesDemux",
        }
    )
    # Genus-only assignment (no species) should be kept as a genus representative.
    consensus_rows.append(
        {
            "read_id": "demux_read_3",
            "sample": DEMUX_SAMPLE,
            "consensus_id": "cons_genus_only",
            "number_of_reads": 4,
            "consensus_kingdom": "Metazoa",
            "consensus_class": "Aves",
            "consensus_order": "Accipitriformes",
            "consensus_family": "Accipitridae",
            "consensus_genus": "GenusOnly",
            "consensus_species": "",
        }
    )

    if include_noadapter:
        read_info_rows.append(
            {
                "read_id": "noadapter_read",
                "filename": "round.pod5",
                "run_id": "run001",
                "barcode": NOADAPTER_SAMPLE,
                "fast_length": 105,
                "fast_mean_qscore": 11,
                "hac_length": 105,
                "hac_mean_qscore": 13,
                "sup_length": 105,
                "sup_mean_qscore": 16,
            }
        )
        on_target_rows.append(
            {"read_id": "noadapter_read", "barcode": NOADAPTER_SAMPLE, "on_target_kingdom": "OFF_TARGET"}
        )
        blast_otu_rows.append(
            {
                "read_id": "noadapter_read",
                "barcode_by_homology": "bc2",
                "basecalling_model": "hac",
                "sample": NOADAPTER_SAMPLE,
                "hit_id": "hit2",
                "taxid": "222",
                "aln_length": 520,
                "perc_id": 97.4,
                "otu_id": "otu2",
                "otu_taxid": "202",
                "otu_kingdom": "Viridiplantae",
                "otu_phylum": "Magnoliophyta",
                "otu_class": "Liliopsida",
                "otu_order": "Poales",
                "otu_family": "Poaceae",
                "otu_genus": "GenusNoAdapter",
                "otu_species": "SpeciesNoAdapter",
            }
        )
        consensus_rows.append(
            {
                "read_id": "noadapter_read",
                "sample": NOADAPTER_SAMPLE,
                "consensus_id": "cons2",
                "number_of_reads": 7,
                "consensus_kingdom": "Viridiplantae",
                "consensus_class": "Liliopsida",
                "consensus_order": "Poales",
                "consensus_family": "Poaceae",
                "consensus_genus": "GenusNoAdapter",
                "consensus_species": "SpeciesNoAdapter",
            }
        )

    demult_rows = make_demult_rows(7)
    otu_rows = make_otu_rows(blast_otu_rows)

    return read_info_rows, on_target_rows, blast_otu_rows, consensus_rows, demult_rows, otu_rows


def run_append_reports(tmp_path, include_noadapter, include_consolidated=False, consolidated_empty=False):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ROUND_DIR).mkdir(parents=True, exist_ok=True)
    read_info_rows, on_target_rows, blast_otu_rows, consensus_rows, demult_rows, otu_rows = base_inputs(include_noadapter)

    write_tsv(tmp_path / f"{BARCODE}_read_info_rpt.txt", READ_INFO_HEADER, read_info_rows)
    write_tsv(tmp_path / f"{BARCODE}_on_target_rpt.txt", ON_TARGET_HEADER, on_target_rows)
    write_tsv(tmp_path / f"{BARCODE}_blast_otu_pretax_rpt.txt", BLAST_OTU_HEADER, blast_otu_rows)
    write_tsv(tmp_path / f"{BARCODE}_blast_consensus_tax_rpt.txt", CONSENSUS_HEADER, consensus_rows)
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_demult_rpt.txt",
        "demult_rpt",
        DEMULT_REPORT_HEADER,
        demult_rows,
        DEMUX_IDENTITY_CONTEXT,
    )
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_otu_def_rpt.txt",
        "otu_def_rpt",
        OTU_REPORT_HEADER,
        otu_rows,
        DEMUX_IDENTITY_CONTEXT,
    )

    if include_consolidated:
        consolidated_rows = [
            {
                "consensus_id": "cons1",
                "barcode_by_homology": "COI",
                "basecalling_model": "consensus",
                "number_of_reads": 12,
                "sample": DEMUX_SAMPLE,
                "taxid": "111",
                "blast_hit": "hit1",
                "aln_length": 500,
                "perc_id": 99.1,
                "consensus_kingdom": "Metazoa",
                "consensus_phylum": "Chordata",
                "consensus_class": "Aves",
                "consensus_order": "Accipitriformes",
                "consensus_family": "Accipitridae",
                "consensus_genus": "GenusDemux",
                "consensus_species": "SpeciesDemux",
            }
        ]
        write_tsv(
            temp_dir / f"{BARCODE}_blast_consensus_tax_consolidated_rpt.txt",
            CONSENSUS_RPT_HEADER,
            consolidated_rows,
        )
    elif consolidated_empty:
        (temp_dir / f"{BARCODE}_blast_consensus_tax_consolidated_rpt.txt").write_text(
            "\t".join(CONSENSUS_RPT_HEADER) + "\n",
            encoding="utf-8",
        )

    seq_file = tmp_path / "sequencing_template.tsv"
    sequencing_template(seq_file)
    pod5_path = tmp_path / f"{ROUND_DIR}.pod5"
    pod5_path.write_bytes(b"test")

    cmd = [
        "perl",
        str(APPEND_REPORTS),
        ROUND_DIR,
        str(temp_dir),
        str(pod5_path),
        BARCODE,
        str(seq_file),
    ]
    env = dict(os.environ)
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = DEMUX_IDENTITY_CONTEXT
    subprocess.run(cmd, check=True, cwd=tmp_path, env=env)
    return temp_dir


def load_cumulative_rows(temp_dir: Path):
    path = temp_dir / f"{BARCODE}_reads_cumulative_rpt.txt"
    with path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        return list(reader)


def load_cumulative_last(temp_dir: Path):
    rows = load_cumulative_rows(temp_dir)
    assert rows, "Expected cumulative rows"
    return rows[-1]


def load_seen_read_ids(temp_dir: Path) -> list[str]:
    path = temp_dir / f"{BARCODE}_seen_read_ids.tsv"
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_on_target_state(temp_dir: Path) -> dict[str, tuple[str, str]]:
    path = temp_dir / f"{BARCODE}_on_target_state.tsv"
    state = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rid, status, barcode = line.split("\t")
        state[rid] = (status, barcode)
    return state


def load_nonempty_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.parametrize("include_noadapter", [True, False])
def test_reads_cumulative_tracks_branch_counts(tmp_path, include_noadapter):
    temp_dir = run_append_reports(tmp_path, include_noadapter)
    rows = load_cumulative_rows(temp_dir)
    assert rows, "Expected at least one cumulative entry"
    entry = rows[-1]

    assert int(entry["total_reads"]) == (2 if include_noadapter else 1)
    assert int(entry["demultiplexed_reads"]) == 7
    assert int(entry["otu_genus_reads_demux"]) == 1
    assert int(entry["consensus_genus_reads_demux"]) == 19

    expected_otu_noadapter = 1 if include_noadapter else 0
    expected_cons_noadapter = 7 if include_noadapter else 0
    assert int(entry["otu_genus_reads_noadapter"]) == expected_otu_noadapter
    assert int(entry["consensus_genus_reads_noadapter"]) == expected_cons_noadapter
    assert int(entry["otu_genus_reads"]) == int(entry["otu_genus_reads_demux"]) + int(entry["otu_genus_reads_noadapter"])
    assert int(entry["consensus_genus_reads"]) == int(entry["consensus_genus_reads_demux"]) + int(
        entry["consensus_genus_reads_noadapter"]
    )

    # Cluster treemaps are separate from read-based treemaps and should reflect the number
    # of consensus clusters per assignment.
    genus_clusters = (temp_dir / f"{BARCODE}_consensus_tax_gns_metazoa_treemap_clusters_rpt.txt").read_text(
        encoding="utf-8"
    )
    assert "Aves\tGenusDemux\t2\n" in genus_clusters

    species_clusters = (temp_dir / f"{BARCODE}_consensus_tax_spc_metazoa_treemap_clusters_rpt.txt").read_text(
        encoding="utf-8"
    )
    assert "Aves\tSpeciesDemux\t2\n" in species_clusters

    rep = (temp_dir / f"{BARCODE}_blast_consensus_tax_representative_rpt.txt").read_text(encoding="utf-8")
    # Best species representative should be kept (12 reads), smaller cluster (3 reads) dropped.
    assert "\tsample_demo_alpha_1\tcons1\t12\t" in rep
    assert "\tsample_demo_alpha_1\tcons1b\t3\t" not in rep
    # Genus-only assignment should be present in the representative table.
    assert "\tsample_demo_alpha_1\tcons_genus_only\t4\t" in rep


def test_consolidated_reports_written_when_present(tmp_path):
    temp_dir = run_append_reports(tmp_path, include_noadapter=False, include_consolidated=True)
    consolidated_time = temp_dir / f"{BARCODE}_consensus_consolidated_tax_time_rpt.txt"
    consolidated_reads = temp_dir / f"{BARCODE}_consensus_consolidated_tax_reads_rpt.txt"
    assert consolidated_time.exists()
    assert consolidated_reads.exists()
    time_lines = consolidated_time.read_text(encoding="utf-8").strip().splitlines()
    reads_lines = consolidated_reads.read_text(encoding="utf-8").strip().splitlines()
    assert len(time_lines) > 1
    assert len(reads_lines) > 1
    assert any(line.endswith("\tspecies\t1") for line in time_lines)
    assert any(line.endswith("\tgenus\t1") for line in time_lines)
    assert any(line.endswith("\tfamily\t1") for line in time_lines)
    assert any(line.endswith("\tspecies\t1") for line in reads_lines)
    assert any(line.endswith("\tgenus\t1") for line in reads_lines)
    assert any(line.endswith("\tfamily\t1") for line in reads_lines)
    treemap = (temp_dir / f"{BARCODE}_consensus_consolidated_tax_gns_metazoa_treemap_rpt.txt").read_text(
        encoding="utf-8"
    )
    assert "Aves\tGenusDemux\t12\n" in treemap


def test_consolidated_reports_not_written_when_missing(tmp_path):
    temp_dir = run_append_reports(tmp_path, include_noadapter=False, include_consolidated=False)
    consolidated_time = temp_dir / f"{BARCODE}_consensus_consolidated_tax_time_rpt.txt"
    consolidated_reads = temp_dir / f"{BARCODE}_consensus_consolidated_tax_reads_rpt.txt"
    assert not consolidated_time.exists()
    assert not consolidated_reads.exists()


def test_consolidated_reports_not_written_when_empty(tmp_path):
    temp_dir = run_append_reports(tmp_path, include_noadapter=False, include_consolidated=False, consolidated_empty=True)
    consolidated_time = temp_dir / f"{BARCODE}_consensus_consolidated_tax_time_rpt.txt"
    consolidated_reads = temp_dir / f"{BARCODE}_consensus_consolidated_tax_reads_rpt.txt"
    assert consolidated_time.exists()
    assert consolidated_reads.exists()
    time_lines = consolidated_time.read_text(encoding="utf-8").strip().splitlines()
    reads_lines = consolidated_reads.read_text(encoding="utf-8").strip().splitlines()
    assert time_lines[0] == "run_id\ttime\ttaxon\tidentifications"
    assert reads_lines[0] == "run_id\treads\ttaxon\tidentifications"
    assert any(line.endswith("\tspecies\t0") for line in time_lines[1:])
    assert any(line.endswith("\tgenus\t0") for line in time_lines[1:])
    assert any(line.endswith("\tfamily\t0") for line in time_lines[1:])
    assert any(line.endswith("\tspecies\t0") for line in reads_lines[1:])
    assert any(line.endswith("\tgenus\t0") for line in reads_lines[1:])
    assert any(line.endswith("\tfamily\t0") for line in reads_lines[1:])


def test_standard_consensus_invariant_with_consolidated(tmp_path):
    temp_no_cons = run_append_reports(tmp_path / "no_cons", include_noadapter=False, include_consolidated=False)
    temp_cons = run_append_reports(tmp_path / "with_cons", include_noadapter=False, include_consolidated=True)
    no_cons = load_cumulative_last(temp_no_cons)
    with_cons = load_cumulative_last(temp_cons)
    assert no_cons["consensus_genus_reads_demux"] == with_cons["consensus_genus_reads_demux"]
    assert no_cons["consensus_genus_reads_noadapter"] == with_cons["consensus_genus_reads_noadapter"]
    assert no_cons["consensus_genus_reads"] == with_cons["consensus_genus_reads"]


def test_reads_cumulative_uses_unique_read_ids_across_rounds(tmp_path):
    temp_dir = run_append_reports(tmp_path, include_noadapter=False)
    # Re-run with the same round inputs to force duplicated rows in rolling temp reports.
    temp_dir = run_append_reports(tmp_path, include_noadapter=False)
    entry = load_cumulative_last(temp_dir)
    assert int(entry["total_reads"]) == 1
    assert int(entry["matched_reads"]) == 1
    assert load_seen_read_ids(temp_dir) == ["demux_read"]
    assert load_on_target_state(temp_dir)["demux_read"] == ("ON_TARGET", DEMUX_SAMPLE)


def test_append_reports_emits_first_seen_read_fate_snapshots_and_cache(tmp_path):
    temp_dir = run_append_reports(tmp_path, include_noadapter=False)
    round_demux = tmp_path / ROUND_DIR / f"{BARCODE}_read_fate_demult_first_seen.tsv"
    round_blast = tmp_path / ROUND_DIR / f"{BARCODE}_read_fate_blast_first_seen.tsv"
    demux_seen = temp_dir / f"{BARCODE}_read_fate_demux_seen.tsv"
    blast_seen = temp_dir / f"{BARCODE}_read_fate_blast_seen.tsv"
    demux_cache = temp_dir / f"{BARCODE}_demux_annotation_cache.tsv"

    demux_lines = load_nonempty_lines(round_demux)
    blast_lines = load_nonempty_lines(round_blast)
    assert len(demux_lines) == 1
    assert len(blast_lines) == 2
    assert blast_lines[1].startswith("demux_read\t")
    assert load_nonempty_lines(demux_seen) == ["demux_boundary_row"]
    assert load_nonempty_lines(blast_seen) == ["demux_read"]
    cache_lines = load_nonempty_lines(demux_cache)
    assert len(cache_lines) == 2
    assert cache_lines[1].startswith("demux_boundary_row\t")

    temp_dir = run_append_reports(tmp_path, include_noadapter=False)
    demux_lines = load_nonempty_lines(round_demux)
    blast_lines = load_nonempty_lines(round_blast)
    cache_lines = load_nonempty_lines(demux_cache)
    assert len(demux_lines) == 1
    assert len(blast_lines) == 1
    assert len(cache_lines) == 2


def test_append_reports_only_snapshots_reads_from_current_round_read_info(tmp_path):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "round_001").mkdir(parents=True, exist_ok=True)
    (tmp_path / "round_002").mkdir(parents=True, exist_ok=True)
    seq_file = tmp_path / "sequencing_template.tsv"
    sequencing_template(seq_file)
    env = dict(os.environ)
    env["RTBIOSCAN_DEMUX_IDENTITY_CONTEXT"] = DEMUX_IDENTITY_CONTEXT

    def run_round(round_dir: str, read_info_rows, on_target_rows, demult_rows, blast_rows) -> None:
        write_tsv(tmp_path / f"{BARCODE}_read_info_rpt.txt", READ_INFO_HEADER, read_info_rows)
        write_tsv(tmp_path / f"{BARCODE}_on_target_rpt.txt", ON_TARGET_HEADER, on_target_rows)
        write_tsv(tmp_path / f"{BARCODE}_blast_otu_pretax_rpt.txt", BLAST_OTU_HEADER, blast_rows)
        write_tsv(tmp_path / f"{BARCODE}_blast_consensus_tax_rpt.txt", CONSENSUS_HEADER, [])
        write_boundary_report_pair(
            tmp_path / f"{BARCODE}_demult_rpt.txt",
            "demult_rpt",
            DEMULT_REPORT_HEADER,
            demult_rows,
            DEMUX_IDENTITY_CONTEXT,
        )
        write_boundary_report_pair(
            tmp_path / f"{BARCODE}_otu_def_rpt.txt",
            "otu_def_rpt",
            OTU_REPORT_HEADER,
            make_otu_rows(blast_rows),
            DEMUX_IDENTITY_CONTEXT,
        )
        pod5_path = tmp_path / f"{round_dir}.pod5"
        pod5_path.write_bytes(b"test")
        subprocess.run(
            [
                "perl",
                str(APPEND_REPORTS),
                round_dir,
                str(temp_dir),
                str(pod5_path),
                BARCODE,
                str(seq_file),
            ],
            check=True,
            cwd=tmp_path,
            env=env,
        )

    run_round(
        "round_001",
        [
            {
                "read_id": "r_old",
                "filename": "round1.pod5",
                "run_id": "run001",
                "barcode": DEMUX_SAMPLE,
                "fast_length": 100,
                "fast_mean_qscore": 10,
                "hac_length": 100,
                "hac_mean_qscore": 12,
                "sup_length": 100,
                "sup_mean_qscore": 15,
            }
        ],
        [{"read_id": "r_old", "barcode": DEMUX_SAMPLE, "on_target_kingdom": "ON_TARGET"}],
        [],
        [],
    )

    run_round(
        "round_002",
        [
            {
                "read_id": "r_new",
                "filename": "round2.pod5",
                "run_id": "run001",
                "barcode": DEMUX_SAMPLE,
                "fast_length": 101,
                "fast_mean_qscore": 10,
                "hac_length": 101,
                "hac_mean_qscore": 12,
                "sup_length": 101,
                "sup_mean_qscore": 15,
            }
        ],
        [{"read_id": "r_new", "barcode": DEMUX_SAMPLE, "on_target_kingdom": "ON_TARGET"}],
        [
            {
                "read_id": "r_old",
                "barcode_by_homology": "COI",
                "basecalling_model": "hac",
                "sample": DEMUX_SAMPLE,
                "platform": "nanopore",
                "sampling_method": "grab",
                "subsample": "sub1",
                "replicate": "1",
                "identity_scope": "sample",
                "identity_value": DEMUX_SAMPLE,
            },
            {
                "read_id": "r_new",
                "barcode_by_homology": "COI",
                "basecalling_model": "hac",
                "sample": DEMUX_SAMPLE,
                "platform": "nanopore",
                "sampling_method": "grab",
                "subsample": "sub1",
                "replicate": "1",
                "identity_scope": "sample",
                "identity_value": DEMUX_SAMPLE,
            },
        ],
        [
            {
                "read_id": "r_old",
                "barcode_by_homology": "COI",
                "basecalling_model": "hac",
                "sample": DEMUX_SAMPLE,
                "hit_id": "hit_old",
                "taxid": "123",
                "aln_length": 500,
                "perc_id": 99.1,
                "otu_id": "OTUB_1-COI",
                "otu_taxid": "201",
                "otu_kingdom": "Metazoa",
                "otu_phylum": "Chordata",
                "otu_class": "Aves",
                "otu_order": "Accipitriformes",
                "otu_family": "Accipitridae",
                "otu_genus": "GenusOld",
                "otu_species": "SpeciesOld",
            },
            {
                "read_id": "r_new",
                "barcode_by_homology": "COI",
                "basecalling_model": "hac",
                "sample": DEMUX_SAMPLE,
                "hit_id": "hit_new",
                "taxid": "NA",
                "aln_length": 480,
                "perc_id": 97.0,
                "otu_id": "OTUB_2-COI",
                "otu_taxid": "NA",
                "otu_kingdom": "Unassigned",
                "otu_phylum": "Unassigned",
                "otu_class": "Unassigned",
                "otu_order": "Unassigned",
                "otu_family": "Unassigned",
                "otu_genus": "Unassigned",
                "otu_species": "Unassigned",
            },
        ],
    )

    round1_demux = load_nonempty_lines(tmp_path / "round_001" / f"{BARCODE}_read_fate_demult_first_seen.tsv")
    round1_blast = load_nonempty_lines(tmp_path / "round_001" / f"{BARCODE}_read_fate_blast_first_seen.tsv")
    round2_demux = load_nonempty_lines(tmp_path / "round_002" / f"{BARCODE}_read_fate_demult_first_seen.tsv")
    round2_blast = load_nonempty_lines(tmp_path / "round_002" / f"{BARCODE}_read_fate_blast_first_seen.tsv")

    assert len(round1_demux) == 1
    assert len(round1_blast) == 1
    assert len(round2_demux) == 2
    assert len(round2_blast) == 2
    assert round2_demux[1].startswith("r_new\t")
    assert round2_blast[1].startswith("r_new\t")
    assert not any(line.startswith("r_old\t") for line in round2_demux[1:])
    assert not any(line.startswith("r_old\t") for line in round2_blast[1:])


def test_matched_reads_cumulative_is_sticky_on_target(tmp_path):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ROUND_DIR).mkdir(parents=True, exist_ok=True)
    seq_file = tmp_path / "sequencing_template.tsv"
    sequencing_template(seq_file)
    pod5_path = tmp_path / f"{ROUND_DIR}.pod5"
    pod5_path.write_bytes(b"test")

    read_info_rows = [
        {
            "read_id": "sticky_read",
            "filename": "round.pod5",
            "run_id": "run001",
            "barcode": DEMUX_SAMPLE,
            "fast_length": 100,
            "fast_mean_qscore": 10,
            "hac_length": 100,
            "hac_mean_qscore": 12,
            "sup_length": 100,
            "sup_mean_qscore": 15,
        }
    ]
    blast_otu_rows = []
    consensus_rows = []
    demult_rows = make_demult_rows(1)
    otu_rows = []

    # Round 1: ON_TARGET
    write_tsv(tmp_path / f"{BARCODE}_read_info_rpt.txt", READ_INFO_HEADER, read_info_rows)
    write_tsv(
        tmp_path / f"{BARCODE}_on_target_rpt.txt",
        ON_TARGET_HEADER,
        [{"read_id": "sticky_read", "barcode": DEMUX_SAMPLE, "on_target_kingdom": "ON_TARGET"}],
    )
    write_tsv(tmp_path / f"{BARCODE}_blast_otu_pretax_rpt.txt", BLAST_OTU_HEADER, blast_otu_rows)
    write_tsv(tmp_path / f"{BARCODE}_blast_consensus_tax_rpt.txt", CONSENSUS_HEADER, consensus_rows)
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_demult_rpt.txt",
        "demult_rpt",
        DEMULT_REPORT_HEADER,
        demult_rows,
        DEMUX_IDENTITY_CONTEXT,
    )
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_otu_def_rpt.txt",
        "otu_def_rpt",
        OTU_REPORT_HEADER,
        otu_rows,
        DEMUX_IDENTITY_CONTEXT,
    )
    subprocess.run(
        ["perl", str(APPEND_REPORTS), ROUND_DIR, str(temp_dir), str(pod5_path), BARCODE, str(seq_file)],
        check=True,
        cwd=tmp_path,
        env={**os.environ, "RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": DEMUX_IDENTITY_CONTEXT},
    )

    # Round 2: same read appears as OFF_TARGET; cumulative matched should remain 1.
    write_tsv(tmp_path / f"{BARCODE}_read_info_rpt.txt", READ_INFO_HEADER, read_info_rows)
    write_tsv(
        tmp_path / f"{BARCODE}_on_target_rpt.txt",
        ON_TARGET_HEADER,
        [{"read_id": "sticky_read", "barcode": DEMUX_SAMPLE, "on_target_kingdom": "OFF_TARGET"}],
    )
    write_tsv(tmp_path / f"{BARCODE}_blast_otu_pretax_rpt.txt", BLAST_OTU_HEADER, blast_otu_rows)
    write_tsv(tmp_path / f"{BARCODE}_blast_consensus_tax_rpt.txt", CONSENSUS_HEADER, consensus_rows)
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_demult_rpt.txt",
        "demult_rpt",
        DEMULT_REPORT_HEADER,
        demult_rows,
        DEMUX_IDENTITY_CONTEXT,
    )
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_otu_def_rpt.txt",
        "otu_def_rpt",
        OTU_REPORT_HEADER,
        otu_rows,
        DEMUX_IDENTITY_CONTEXT,
    )
    subprocess.run(
        ["perl", str(APPEND_REPORTS), ROUND_DIR, str(temp_dir), str(pod5_path), BARCODE, str(seq_file)],
        check=True,
        cwd=tmp_path,
        env={**os.environ, "RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": DEMUX_IDENTITY_CONTEXT},
    )

    entry = load_cumulative_last(temp_dir)
    assert int(entry["matched_reads"]) == 1
    assert load_on_target_state(temp_dir)["sticky_read"] == ("ON_TARGET", DEMUX_SAMPLE)


def test_sidecars_rebuild_from_legacy_reports_with_tolerant_parser(tmp_path):
    temp_dir = tmp_path / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / ROUND_DIR).mkdir(parents=True, exist_ok=True)
    seq_file = tmp_path / "sequencing_template.tsv"
    sequencing_template(seq_file)
    pod5_path = tmp_path / f"{ROUND_DIR}.pod5"
    pod5_path.write_bytes(b"test")

    # Pre-populate rolling state with repeated headers, blank rows, CRLF rows, and malformed rows.
    (temp_dir / f"{BARCODE}_read_info_rpt.txt").write_text(
        "\r\n".join(
            [
                "\t".join(READ_INFO_HEADER),
                "read_a\tround.pod5\trun001\tbarcode_a\t100\t10\t100\t12\t100\t15",
                "\t".join(READ_INFO_HEADER),
                "",
                "malformed_only",
                "read_b\tround.pod5\trun001\tbarcode_b\t101\t10\t101\t12\t101\t15",
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )
    (temp_dir / f"{BARCODE}_on_target_rpt.txt").write_text(
        "\r\n".join(
            [
                "\t".join(ON_TARGET_HEADER),
                "read_a\tbarcode_a\tOFF_TARGET",
                "\t".join(ON_TARGET_HEADER),
                "read_a\t\tON_TARGET",
                "malformed_only",
                "read_b\tbarcode_b\tOFF_TARGET",
                "read_c\tbarcode_c\tON_TARGET",
            ]
        )
        + "\r\n",
        encoding="utf-8",
    )

    # Current round adds one new read and upgrades read_b to sticky ON_TARGET without restating barcode.
    write_tsv(
        tmp_path / f"{BARCODE}_read_info_rpt.txt",
        READ_INFO_HEADER,
        [
            {
                "read_id": "read_d",
                "filename": "round.pod5",
                "run_id": "run001",
                "barcode": "barcode_d",
                "fast_length": 102,
                "fast_mean_qscore": 10,
                "hac_length": 102,
                "hac_mean_qscore": 12,
                "sup_length": 102,
                "sup_mean_qscore": 15,
            }
        ],
    )
    write_tsv(
        tmp_path / f"{BARCODE}_on_target_rpt.txt",
        ON_TARGET_HEADER,
        [
            {"read_id": "read_b", "barcode": "", "on_target_kingdom": "ON_TARGET"},
            {"read_id": "read_d", "barcode": "barcode_d", "on_target_kingdom": "ON_TARGET"},
        ],
    )
    write_tsv(tmp_path / f"{BARCODE}_blast_otu_pretax_rpt.txt", BLAST_OTU_HEADER, [])
    write_tsv(tmp_path / f"{BARCODE}_blast_consensus_tax_rpt.txt", CONSENSUS_HEADER, [])
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_demult_rpt.txt",
        "demult_rpt",
        DEMULT_REPORT_HEADER,
        make_demult_rows(1),
        DEMUX_IDENTITY_CONTEXT,
    )
    write_boundary_report_pair(
        tmp_path / f"{BARCODE}_otu_def_rpt.txt",
        "otu_def_rpt",
        OTU_REPORT_HEADER,
        [],
        DEMUX_IDENTITY_CONTEXT,
    )

    subprocess.run(
        ["perl", str(APPEND_REPORTS), ROUND_DIR, str(temp_dir), str(pod5_path), BARCODE, str(seq_file)],
        check=True,
        cwd=tmp_path,
        env={**os.environ, "RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": DEMUX_IDENTITY_CONTEXT},
    )

    assert load_seen_read_ids(temp_dir) == ["read_a", "read_b", "read_d"]
    assert load_on_target_state(temp_dir) == {
        "read_a": ("ON_TARGET", "barcode_a"),
        "read_b": ("ON_TARGET", "barcode_b"),
        "read_c": ("ON_TARGET", "barcode_c"),
        "read_d": ("ON_TARGET", "barcode_d"),
    }
    entry = load_cumulative_last(temp_dir)
    assert int(entry["total_reads"]) == 3
    assert int(entry["matched_reads"]) == 3
