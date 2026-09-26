"""Independent exact-map oracle for the coupled collapse identity boundary."""

import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
HEADER = "sample_id\tmarker_id\tsuffix_resolution_mode\tunit_suffix_current\tunit_id_collapse\n"


def run(command, *, env=None, cwd=None):
    return subprocess.run(command, capture_output=True, text=True, check=False,
                          env={**os.environ, **(env or {})}, cwd=cwd)


def dna(index):
    digits = []
    for _ in range(12):
        digits.append("ACGT"[index % 4])
        index //= 4
    return "".join(digits)


def make_case(path, units, *, reverse_map=False, reverse_blast=False):
    path.mkdir(parents=True, exist_ok=True)
    mapping = [f"{sample}\t{marker}\tmarker\t{marker}\t{sample}_{marker}\n"
               for sample, marker, _, _ in units]
    if reverse_map:
        mapping.reverse()
    identity = path / "replicate_identity.tsv"
    identity.write_text(HEADER + "".join(mapping), encoding="utf-8")
    blast_rows = []
    demult_rows = []
    roster = []
    seen_samples = set()
    serial = 0
    for sample, marker, count, otu in units:
        if sample not in seen_samples:
            roster.append(f"{sample}\t{sample}_r1\n")
            seen_samples.add(sample)
        unit = f"{sample}_{marker}"
        for _ in range(count):
            serial += 1
            read_id = f"tag{serial:03d}|{marker}|hac2sup|barcode=barcode_1|adapter={unit}|{otu}-{marker}"
            blast_rows.append(f"{read_id}\t{otu}-{marker}\tMetazoa\t{marker}\t{dna(serial)}\n")
            demult_rows.append(f"{read_id}\t{marker}\thac\t{unit}\n")
    if reverse_blast:
        blast_rows.reverse()
    blast = path / "blast.tsv"
    blast.write_text("read_id\totu_id\totu_kingdom\tbarcode_by_homology\tsequence\n"
                     + "".join(blast_rows), encoding="utf-8")
    demult = path / "demult.tsv"
    demult.write_text("read_id\tbarcode_by_homology\tbasecalling_model\tsample\n"
                      + "".join(demult_rows), encoding="utf-8")
    roster_path = path / "roster.txt"
    roster_path.write_text("".join(roster), encoding="utf-8")
    return identity, blast, demult, roster_path


def oracle(identity, blast):
    """Use only identity rows and tagged BLAST records, never production helpers."""
    rows = identity.read_text(encoding="utf-8").splitlines()
    columns = rows[0].split("\t")
    key = {name: columns.index(name) for name in ("sample_id", "marker_id", "unit_id_collapse")}
    mapping = {}
    for line in rows[1:]:
        fields = line.split("\t")
        mapping[fields[key["unit_id_collapse"]]] = (fields[key["sample_id"]], fields[key["marker_id"]])
    expected = defaultdict(list)
    for line in blast.read_text(encoding="utf-8").splitlines()[1:]:
        read_id, otu, _, marker, sequence = line.split("\t")
        unit = re.search(r"(?:^|\|)adapter=([^|]+)", read_id).group(1)
        sample, mapped_marker = mapping[unit]
        assert marker == mapped_marker
        assert sequence == dna(int(read_id[3:6]))
        expected[sample].append((read_id, otu, marker, sequence))
    return {sample: sorted(rows) for sample, rows in expected.items()}


def exercise(path, units, *, reverse_map=False, reverse_blast=False, seed="0", locale="C",
             duplicate_map_row=False):
    identity, blast, demult, roster = make_case(
        path, units, reverse_map=reverse_map, reverse_blast=reverse_blast)
    if duplicate_map_row:
        lines = identity.read_text(encoding="utf-8").splitlines()
        identity.write_text("\n".join(lines + [lines[1]]) + "\n", encoding="utf-8")
    expected = oracle(identity, blast)
    env = {"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
           "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity),
           "RTBIOSCAN_TARGET_TOKENS": "COI|ITS2", "PERL_HASH_SEED": seed,
           "PERL_PERTURB_KEYS": "0", "LC_ALL": locale}
    samples = path / "consensus_samples.txt"
    detect = run(["bash", str(BIN / "detect_consensus_sample_mode.sh"),
                  str(blast), str(samples), str(roster)], env=env)
    assert detect.returncode == 0, detect.stderr
    assert set(samples.read_text(encoding="utf-8").splitlines()) == set(expected)
    parts_dir = path / "parts"
    part = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                str(blast), str(samples), str(parts_dir)], env=env)
    assert part.returncode == 0, part.stderr
    actual = {}
    for file in parts_dir.glob("*.blast.tsv"):
        rows = [tuple(line.split("\t")[i] for i in (0, 1, 3, 4))
                for line in file.read_text(encoding="utf-8").splitlines()]
        actual[file.name.removesuffix(".blast.tsv")] = sorted(rows)
    assert actual == expected
    assert {row[0] for rows in actual.values() for row in rows} == {
        row[0] for rows in expected.values() for row in rows}
    # These are the exact rows and OTU/marker keys entering the consensus sample loop.
    assert {sample: {(row[1], row[2]) for row in rows} for sample, rows in actual.items()} == {
        sample: {(row[1], row[2]) for row in rows} for sample, rows in expected.items()}

    blast_otu = path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        + "".join(
            f"{read_id}\t{marker}\thac\t{re.search(r'adapter=([^|]+)', read_id).group(1)}\thit\t123\t100\t99\t{otu}\tF1\tG1\tS1\n"
            for rows in expected.values() for read_id, otu, marker, _ in rows
        ), encoding="utf-8")
    report_path = path / "report.json"
    report = run(["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
                  "--barcode", "RTBioScan", "--round-barcode", "round_1",
                  "--out", str(report_path), "--sample-roster", str(roster),
                  "--demult", str(demult), "--blast-otu", str(blast_otu),
                  "--replicate-identity", str(identity)], env=env)
    assert report.returncode == 0, report.stderr
    metrics = {entry["label"]: entry["reads_demux"] for entry in
               json.loads(report_path.read_text(encoding="utf-8"))["sample_metrics"].values()}
    assert metrics == {sample: len(rows) for sample, rows in expected.items()}
    blast_metrics = {entry["label"]: entry["reads_blast_assigned"] for entry in
                     json.loads(report_path.read_text(encoding="utf-8"))["sample_metrics"].values()}
    assert blast_metrics == {sample: len(rows) for sample, rows in expected.items()}

    # Exercise the production provenance parser with headers and read lists made from
    # the actual partition membership. A phantom or merged partition changes its sample.
    consensus_dir = path / "Consensus"
    for sample, rows in actual.items():
        sample_dir = consensus_dir / sample
        (sample_dir / "OriginalReads").mkdir(parents=True)
        by_key = defaultdict(list)
        for read_id, otu, marker, _ in rows:
            by_key[(otu, marker)].append(read_id)
        fasta = []
        for (otu, marker), members in sorted(by_key.items()):
            cons_token = otu.removesuffix("-" + marker)
            fasta.append(f">{sample}|{cons_token}|{marker}|RTBioScan|reads-{len(members)}\n{dna(len(members))}\n")
            (sample_dir / "OriginalReads" / f"{cons_token}_reads.list").write_text(
                "".join(f"{rid}\n" for rid in members), encoding="utf-8")
        (sample_dir / f"{sample}_Merged_Consensus.fasta").write_text("".join(fasta), encoding="utf-8")
    provenance = path / "provenance.tsv"
    prov = run(["perl", str(BIN / "emit_consensus_round_provenance.pl"),
                "--consensus-dir", str(consensus_dir), "--round-barcode", "round_1",
                "--out", str(provenance)], env=env)
    assert prov.returncode == 0, prov.stderr
    provenance_rows = [line.split("\t") for line in provenance.read_text(encoding="utf-8").splitlines()[1:]]
    expected_provenance = []
    for sample, rows in expected.items():
        by_key = defaultdict(list)
        for read_id, otu, marker, _ in rows:
            by_key[(otu, marker)].append(read_id)
        for (otu, marker), members in by_key.items():
            token = otu.removesuffix("-" + marker)
            expected_provenance.append(["round_1", sample, "NA", f"{token}_{sample}", str(len(members))])
    assert sorted(provenance_rows) == sorted(expected_provenance)
    consensus_blast = path / "blast_consensus.tsv"
    consensus_blast.write_text(
        "consensus_id\tsample\tbarcode_by_homology\tnumber_of_reads\n" +
        "".join(f"{consensus_id}\t{sample}\t{next(row[2] for row in expected[sample] if row[1].removesuffix('-' + row[2]) + '_' + sample == consensus_id)}\t{count}\n"
                for _, sample, _, consensus_id, count in expected_provenance),
        encoding="utf-8",
    )
    report_with_consensus = path / "report_with_consensus.json"
    reported = run(["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
                    "--barcode", "RTBioScan", "--round-barcode", "round_1",
                    "--out", str(report_with_consensus), "--sample-roster", str(roster),
                    "--demult", str(demult), "--blast-otu", str(blast_otu),
                    "--blast-consensus", str(consensus_blast),
                    "--consensus-round-provenance", str(provenance),
                    "--replicate-identity", str(identity)], env=env)
    assert reported.returncode == 0, reported.stderr
    report_metrics = {entry["label"]: entry for entry in
                      json.loads(report_with_consensus.read_text(encoding="utf-8"))["sample_metrics"].values()}
    assert set(report_metrics) == set(expected)
    for sample in expected:
        assert report_metrics[sample]["consensus_emitted"] == sum(
            row[1] == sample for row in expected_provenance)
    return expected, actual, provenance.read_bytes()


@pytest.mark.parametrize("units", [
    [("Plot_3_North", "COI", 5, "OTUB_1"), ("Plot_4_North", "COI", 3, "OTUB_1"),
     ("Plot_3_North", "ITS2", 4, "OTUB_7"), ("Plot_4_North", "ITS2", 2, "OTUB_7")],
    [("W_eDNA_1", "COI", 5, "OTUB_1"), ("W_eDNA_2", "COI", 3, "OTUB_1")],
    [("Site", "COI", 3, "OTUB_1"), ("Site_10", "COI", 5, "OTUB_1")],
    [("Soil", "COI", 5, "OTUB_1"), ("Soil_COI", "COI", 3, "OTUB_1")],
    [("A_1_B_2_C", "COI", 5, "OTUB_1"), ("A_9_B_8_C", "COI", 3, "OTUB_1")],
    [("Site_A", "COI", 5, "OTUB_1"), ("Site_B", "COI", 3, "OTUB_1")],
])
def test_coupled_exact_identity_scenarios(tmp_path, units):
    expected, actual, _ = exercise(tmp_path, units)
    assert expected == actual
    assert set(actual) == {unit[0] for unit in units}


def test_order_seed_and_locale_do_not_change_scientific_identity(tmp_path):
    units = [("Plot_3_North", "COI", 5, "OTUB_1"), ("Plot_4_North", "COI", 3, "OTUB_1")]
    locales = subprocess.check_output(["locale", "-a"], text=True).splitlines()
    utf8 = next(name for name in locales if "UTF-8" in name.upper() or "UTF8" in name.upper())
    snapshots = []
    for reverse in (False, True):
        for seed in ("0", "17"):
            for locale in ("C", utf8):
                case = tmp_path / f"{reverse}_{seed}_{locale}"
                snapshots.append(exercise(case, units, reverse_map=reverse,
                                          reverse_blast=reverse, seed=seed, locale=locale)[0])
    assert all(snapshot == snapshots[0] for snapshot in snapshots)


def test_mapped_unicode_identities_remain_exact(tmp_path):
    expected, actual, _ = exercise(
        tmp_path, [("Río", "COI", 2, "OTUB_1"), ("MuestraÑ", "COI", 3, "OTUB_1")])
    assert expected == actual
    assert set(actual) == {"Río", "MuestraÑ"}


def test_absent_map_keeps_legacy_embedded_numeric_label(tmp_path):
    blast = tmp_path / "blast.tsv"
    read_id = "tag001|COI|hac2sup|adapter=CS.D.P_2_MPold1|OTUB_1-COI"
    blast.write_text(f"read_id\totu_id\n{read_id}\tOTUB_1-COI\n", encoding="utf-8")
    samples = tmp_path / "samples.txt"
    env = {"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
           "RTBIOSCAN_REPLICATE_IDENTITY_TSV": ""}
    detect = run(["bash", str(BIN / "detect_consensus_sample_mode.sh"),
                  str(blast), str(samples)], env=env)
    assert detect.returncode == 0, detect.stderr
    assert samples.read_text(encoding="utf-8") == "CS.D.P_MPold1\n"
    parts = tmp_path / "parts"
    partition = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                     str(blast), str(samples), str(parts)], env=env)
    assert partition.returncode == 0, partition.stderr
    assert (parts / "CS.D.P_MPold1.blast.tsv").read_text(encoding="utf-8") == f"{read_id}\tOTUB_1-COI\n"


def test_identical_collapse_unit_duplicate_is_consistent(tmp_path):
    units = [("Site_A", "COI", 2, "OTUB_1")]
    # The exact identity fields are identical, as for multiple replicates of a sample.
    expected, actual, _ = exercise(tmp_path, units, duplicate_map_row=True)
    assert expected == actual


def test_fallback_suffix_uses_exact_map_even_when_longer_sample_is_prefix(tmp_path):
    identity, blast, demult, roster = make_case(
        tmp_path, [("A", "COI", 2, "OTUB_1"), ("A_B", "COI", 3, "OTUB_1")])
    identity.write_text(identity.read_text(encoding="utf-8").replace(
        "A\tCOI\tmarker\tCOI\tA_COI", "A\tCOI\tfallback\tB_C\tA_B_C"), encoding="utf-8")
    for path in (blast, demult):
        path.write_text(path.read_text(encoding="utf-8").replace(
            "adapter=A_COI", "adapter=A_B_C").replace("\tA_COI\n", "\tA_B_C\n"),
            encoding="utf-8")
    expected = oracle(identity, blast)
    assert {sample: len(rows) for sample, rows in expected.items()} == {"A": 2, "A_B": 3}
    env = {"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
           "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}
    samples = tmp_path / "samples.txt"
    assert run(["bash", str(BIN / "detect_consensus_sample_mode.sh"), str(blast),
                str(samples), str(roster)], env=env).returncode == 0
    assert set(samples.read_text(encoding="utf-8").splitlines()) == {"A", "A_B"}
    parts = tmp_path / "parts"
    result = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                  str(blast), str(samples), str(parts)], env=env)
    assert result.returncode == 0, result.stderr
    assert {sample: {line.split("\t")[0] for line in
                     (parts / f"{sample}.blast.tsv").read_text(encoding="utf-8").splitlines()}
            for sample in expected} == {sample: {row[0] for row in rows}
                                   for sample, rows in expected.items()}
    out = tmp_path / "report.json"
    result = run(["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
                  "--barcode", "RTBioScan", "--round-barcode", "round_1",
                  "--out", str(out), "--sample-roster", str(roster), "--demult", str(demult),
                  "--replicate-identity", str(identity)], env=env)
    assert result.returncode == 0, result.stderr
    labels = {entry["label"]: entry["reads_demux"] for entry in
              json.loads(out.read_text(encoding="utf-8"))["sample_metrics"].values()}
    assert labels == {"A": 2, "A_B": 3}
    marker_counts = {entry["label"]: entry["reads_demux_by_marker"] for entry in
                     json.loads(out.read_text(encoding="utf-8"))["sample_metrics"].values()}
    assert marker_counts == {"A": {"COI": 2}, "A_B": {"COI": 3}}


@pytest.mark.parametrize("rows", [
    "Plot_3_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\n",  # missing observed unit
    "Plot_3_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\nPlot_4_North\tCOI\tmarker\tCOI\tPlot_3_North_COI\n",
    "Plot_3_North\tCOI\tmarker\tCOI\n",
    "\tCOI\tmarker\tCOI\t_COI\n",
    "Plot_3_North\tCOI\tmarker\tCOI\t\n",
    "Bad/Name\tCOI\tmarker\tCOI\tBad/Name_COI\n",
    "Plot_3_North\tITS2\tmarker\tCOI\tPlot_3_North_COI\n",
])
def test_present_map_refuses_invalid_or_missing_identity_before_report(tmp_path, rows):
    units = [("Plot_3_North", "COI", 1, "OTUB_1"),
             ("Plot_4_North", "COI", 1, "OTUB_1")]
    identity, blast, demult, roster = make_case(tmp_path, units)
    identity.write_text(HEADER + rows, encoding="utf-8")
    env = {"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
           "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}
    samples = tmp_path / "samples.txt"
    detect = run(["bash", str(BIN / "detect_consensus_sample_mode.sh"),
                  str(blast), str(samples), str(roster)], env=env)
    assert detect.returncode != 0
    samples.write_text("Plot_3_North\nPlot_4_North\n", encoding="utf-8")
    parts_dir = tmp_path / "parts"
    partition = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                     str(blast), str(samples), str(parts_dir)], env=env)
    assert partition.returncode != 0
    assert not list(parts_dir.glob("*.blast.tsv"))
    out = tmp_path / "report.json"
    report = run(["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
                  "--barcode", "RTBioScan", "--round-barcode", "round_1",
                  "--out", str(out), "--sample-roster", str(roster), "--demult", str(demult),
                  "--replicate-identity", str(identity)], env=env)
    assert report.returncode != 0
    assert not out.exists()


def test_marker_suffix_corruption_refuses_before_detector_output(tmp_path):
    identity, blast, _, roster = make_case(
        tmp_path, [("Plot_3_North", "COI", 1, "OTUB_1")])
    identity.write_text(
        HEADER + "Plot_3_North\tITS2\tmarker\tCOI\tPlot_3_North_COI\n",
        encoding="utf-8")
    samples = tmp_path / "samples.txt"
    samples.write_text("preexisting\n", encoding="utf-8")
    result = run(["bash", str(BIN / "detect_consensus_sample_mode.sh"),
                  str(blast), str(samples), str(roster)],
                 env={"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
                      "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)})
    assert result.returncode != 0
    assert "marker/suffix mismatch" in result.stderr
    assert samples.read_text(encoding="utf-8") == "preexisting\n"


def test_present_map_excludes_blast_row_marker_mismatch(tmp_path):
    identity, blast, _, _ = make_case(tmp_path, [("Site_A", "COI", 1, "OTUB_1")])
    blast.write_text(blast.read_text(encoding="utf-8").replace(
        "\tMetazoa\tCOI\t", "\tMetazoa\tITS2\t"), encoding="utf-8")
    samples = tmp_path / "samples.txt"
    samples.write_text("Site_A\n", encoding="utf-8")
    parts = tmp_path / "parts"
    result = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                  str(blast), str(samples), str(parts)],
                 env={"RTBIOSCAN_EFFECTIVE_IDENTITY_MODE": "collapse",
                      "RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)})
    assert result.returncode == 0, result.stderr
    assert "marker/unit mismatch" in result.stderr
    assert (parts / "Site_A.blast.tsv").read_text(encoding="utf-8") == ""


def test_present_map_excludes_demult_marker_mismatch(tmp_path):
    identity, _, demult, roster = make_case(tmp_path, [("Site_A", "COI", 1, "OTUB_1")])
    demult.write_text(demult.read_text(encoding="utf-8").replace(
        "\tCOI\thac\t", "\tITS2\thac\t"), encoding="utf-8")
    out = tmp_path / "report.json"
    result = run(["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
                  "--barcode", "RTBioScan", "--round-barcode", "round_1",
                  "--out", str(out), "--sample-roster", str(roster), "--demult", str(demult),
                  "--replicate-identity", str(identity)])
    assert result.returncode == 0, result.stderr
    assert "marker/unit mismatch" in result.stderr
    metrics = json.loads(out.read_text(encoding="utf-8"))["sample_metrics"]
    assert all(entry["reads_demux"] is None for entry in metrics.values())


def test_present_map_rejects_phantom_consensus_provenance_before_report(tmp_path):
    identity, _, demult, roster = make_case(tmp_path, [("Plot_3_North", "COI", 1, "OTUB_1")])
    provenance = tmp_path / "provenance.tsv"
    provenance.write_text(
        "round_barcode\tsample\totu_key\tconsensus_id\treads_used_round\n"
        "round_1\tPlot_North\tOTUB_1-COI\tOTUB_1_Plot_North\t1\n",
        encoding="utf-8")
    out = tmp_path / "report.json"
    result = run(["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
                  "--barcode", "RTBioScan", "--round-barcode", "round_1",
                  "--out", str(out), "--sample-roster", str(roster), "--demult", str(demult),
                  "--consensus-round-provenance", str(provenance),
                  "--replicate-identity", str(identity)])
    assert result.returncode != 0
    assert "absent from identity map" in result.stderr
    assert not out.exists()


def test_consensus_consumes_exact_partition_files_and_refuses_mapped_fallback():
    source = (BIN / "Consensus_simple.sh").read_text(encoding="utf-8")
    assert 'sample_blast="$sample_partition_dir/${sample}.blast.tsv"' in source
    assert 'if [ "$sample_partition_ok" -eq 1 ] && [ -f "$sample_partition_dir/${sample}.blast.tsv" ]; then' in source
    assert 'if [ "$identity_mode" = "track" ] || [ -n "${RTBIOSCAN_REPLICATE_IDENTITY_TSV:-}" ]; then' in source
    assert 'ERROR: partition_blast_rows_by_adapter_class.sh failed with collapse identity map; refusing fallback' in source


def test_valid_but_conflicting_unit_rows_are_rejected(tmp_path):
    identity = tmp_path / "replicate_identity.tsv"
    identity.write_text(
        HEADER + "A_B\tCOI\tmarker\tCOI\tA_B_COI\n"
        "A\tCOI\tfallback\tB_COI\tA_B_COI\n", encoding="utf-8")
    result = run(["perl", "-I", str(BIN), "-e",
                  'require "collapse_identity_map.pl"; CollapseIdentityMap::load($ARGV[0]);',
                  str(identity)])
    assert result.returncode != 0
    assert "conflicting rows" in result.stderr


def producer_case(path, reads, *, reverse_map=False, reverse_blast=False,
                  seed="0", locale="C"):
    """Use the unchanged demux and OTU producers; reads are (id, marker, unit)."""
    path.mkdir(parents=True, exist_ok=True)
    mapping = [(sample, marker) for sample in ("Plot_3_North", "Plot_4_North")
               for marker in ("COI", "ITS2")]
    if reverse_map:
        mapping.reverse()
    identity = path / "replicate_identity.tsv"
    identity.write_text(HEADER + "".join(
        f"{sample}\t{marker}\tmarker\t{marker}\t{sample}_{marker}\n"
        for sample, marker in mapping), encoding="utf-8")
    roster = path / "roster.tsv"
    roster.write_text("Plot_3_North\tP3r1\nPlot_4_North\tP4r1\n", encoding="utf-8")
    tagged = [(rid, f"{rid}|{marker}|hac|barcode={marker}|adapter={unit}", marker, unit)
              for rid, marker, unit in reads]
    fastq = path / "reads.fastq"
    fastq.write_text("".join(f"@{tag}\nACGT\n+\n####\n" for _, tag, _, _ in tagged),
                     encoding="utf-8")
    cluster = path / "merged.clstr"
    cluster.write_text("".join(
        f">Cluster {cluster_id}\n" + "".join(
            f"{i}\t4nt, >{tag}... {'*' if i == 0 else 'at +/99.0%'}\n"
            for i, (_, tag, _, _) in enumerate(r for r in tagged if r[2] == marker))
        for cluster_id, marker in enumerate(("COI", "ITS2"))), encoding="utf-8")
    env = {"RTBIOSCAN_DEMUX_IDENTITY_CONTEXT": "full_collapse",
           "RTBIOSCAN_TARGET_TOKENS": "COI|ITS2", "PERL_HASH_SEED": seed,
           "PERL_PERTURB_KEYS": "0", "LC_ALL": locale}
    for script, args in (
        ("reporting_demultiplexing.pl", [fastq, path / "results", path / "round", "RTBioScan"]),
        ("reporting_otu_definition.pl", [cluster, path / "RTBioScan_demult_rpt.txt",
                                          "round_1", "RTBioScan", "COI", "ITS2"]),
    ):
        result = run(["perl", str(BIN / script), *map(str, args)], env=env, cwd=path)
        # The producers publish relative output names in their working directory.
        if result.returncode != 0:
            raise AssertionError(result.stderr)
    blast_rows = [f"{tag}\tOTUB_{0 if marker == 'COI' else 1}-{marker}\tMetazoa\t{marker}\tACGT\n"
                  for _, tag, marker, _ in tagged]
    if reverse_blast:
        blast_rows.reverse()
    blast = path / "blast.tsv"
    blast.write_text("read_id\totu_id\totu_kingdom\tbarcode_by_homology\tsequence\n"
                     + "".join(blast_rows), encoding="utf-8")
    blast_otu = path / "blast_otu.tsv"
    blast_otu.write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_family\totu_genus\totu_species\n"
        + "".join(f"{rid}\t{marker}\thac\t{unit}\thit\t123\t100\t99\tOTUB_{0 if marker == 'COI' else 1}-{marker}\tF1\tG1\tS1\n"
                  for rid, _, marker, unit in tagged), encoding="utf-8")
    return identity, roster, blast, blast_otu, path / "RTBioScan_demult_rpt.txt", path / "RTBioScan_otu_def_rpt.txt", env


def run_producer_report(path, identity, roster, blast_otu, demult, otu_def, env, *, mapped=True,
                        cumulative=None, sizes=None):
    out = path / "report.json"
    args = ["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
            "--barcode", "RTBioScan", "--round-barcode", "round_1",
            "--out", str(out), "--sample-roster", str(roster), "--demult", str(demult),
            "--otu-def", str(otu_def), "--blast-otu", str(blast_otu),
            "--timestamp-utc", "2026-09-26T00:00:00Z"]
    if cumulative is not None:
        args += ["--blast-otu-cumulative", str(cumulative)]
    if sizes is not None:
        args += ["--otu-sizes-round", str(sizes)]
    if mapped:
        args += ["--replicate-identity", str(identity)]
    result = run(args, env=env)
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8")), result


def test_real_producer_otu_identity_value_maps_exactly(tmp_path):
    reads = [(f"r{i:03d}", marker, f"{sample}_{marker}")
             for sample, marker, count in (("Plot_3_North", "COI", 3),
                                           ("Plot_4_North", "COI", 2),
                                           ("Plot_3_North", "ITS2", 4),
                                           ("Plot_4_North", "ITS2", 1))
             for i in range(count)]
    # Distinct read IDs are required across sample and marker partitions.
    reads = [(f"r{i:03d}", marker, unit) for i, (_, marker, unit) in enumerate(reads)]
    identity, roster, _, blast_otu, demult, otu_def, env = producer_case(tmp_path, reads)
    header = otu_def.read_text(encoding="utf-8").splitlines()[0].split("\t")
    assert "identity_value" in header
    produced_rows = [dict(zip(header, line.split("\t"))) for line in
                     otu_def.read_text(encoding="utf-8").splitlines()[1:]]
    assert len(produced_rows) == 10
    assert {row["identity_value"] for row in produced_rows} == {
        f"{sample}_{marker}" for sample in ("Plot_3_North", "Plot_4_North")
        for marker in ("COI", "ITS2")}
    assert all(row["sample"] == row["identity_value"] for row in produced_rows)
    (tmp_path / "report.json").write_text("preexisting destination\n", encoding="utf-8")
    data, _ = run_producer_report(tmp_path, identity, roster, blast_otu, demult, otu_def, env)
    members = {(row["sample"], row["marker"]): row["otu_reads_sample_total"]
               for row in data["otu"]["assignments_by_level"]["species"]}
    assert members == {("Plot_3_North", "COI"): 3, ("Plot_4_North", "COI"): 2,
                       ("Plot_3_North", "ITS2"): 4, ("Plot_4_North", "ITS2"): 1}
    assert {entry["label"]: entry["reads_demux"] for entry in data["sample_metrics"].values()} == {
        "Plot_3_North": 7, "Plot_4_North": 3}


def test_mapped_marker_conflicts_are_excluded_by_partition_and_reporting(tmp_path):
    valid = [("v1", "COI", "Plot_3_North_COI"),
             ("v2", "COI", "Plot_3_North_COI"),
             ("v3", "ITS2", "Plot_4_North_ITS2")]
    wrong = [("x1", "COI", "Plot_4_North_ITS2"),
             ("x2", "ITS2", "Plot_3_North_COI")]
    identity, roster, blast, blast_otu, demult, otu_def, env = producer_case(tmp_path, valid + wrong)
    samples = tmp_path / "samples.txt"
    detect = run(["bash", str(BIN / "detect_consensus_sample_mode.sh"), str(blast),
                  str(samples), str(roster)], env=env | {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)})
    assert detect.returncode == 0, detect.stderr
    assert set(samples.read_text(encoding="utf-8").splitlines()) == {"Plot_3_North", "Plot_4_North"}
    parts = tmp_path / "parts"
    partition = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                     str(blast), str(samples), str(parts)],
                    env=env | {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)})
    assert partition.returncode == 0, partition.stderr
    assert partition.stderr.count("marker/unit mismatch") == 1
    partition_ids = {sample: {line.split("\t")[0].split("|")[0] for line in
                             (parts / f"{sample}.blast.tsv").read_text(encoding="utf-8").splitlines()}
                     for sample in ("Plot_3_North", "Plot_4_North")}
    assert partition_ids == {"Plot_3_North": {"v1", "v2"}, "Plot_4_North": {"v3"}}
    consensus = tmp_path / "Consensus"
    for sample, marker, otu, ids in (("Plot_3_North", "COI", "OTUB_0", {"v1", "v2"}),
                                      ("Plot_4_North", "ITS2", "OTUB_1", {"v3"})):
        sample_dir = consensus / sample
        (sample_dir / "OriginalReads").mkdir(parents=True)
        (sample_dir / "OriginalReads" / f"{otu}_reads.list").write_text(
            "".join(f"{rid}\n" for rid in sorted(partition_ids[sample])), encoding="utf-8")
        (sample_dir / f"{sample}_Merged_Consensus.fasta").write_text(
            f">{sample}|{otu}|{marker}|RTBioScan|reads-{len(ids)}\nACGT\n",
            encoding="utf-8")
    provenance = tmp_path / "provenance.tsv"
    emitted = run(["perl", str(BIN / "emit_consensus_round_provenance.pl"),
                   "--consensus-dir", str(consensus), "--round-barcode", "round_1",
                   "--out", str(provenance)])
    assert emitted.returncode == 0, emitted.stderr
    provenance_rows = [line.split("\t") for line in provenance.read_text(encoding="utf-8").splitlines()[1:]]
    assert {(row[1], row[4]) for row in provenance_rows} == {
        ("Plot_3_North", "2"), ("Plot_4_North", "1")}
    data, result = run_producer_report(tmp_path, identity, roster, blast_otu, demult, otu_def, env)
    assert result.stderr.count("marker/unit mismatch") == 1
    assert {entry["label"]: entry["reads_demux"] for entry in data["sample_metrics"].values()} == {
        "Plot_3_North": 2, "Plot_4_North": 1}
    assert {(row["sample"], row["marker"]): row["otu_reads_sample_total"]
            for row in data["otu"]["assignments_by_level"]["species"]} == {
                ("Plot_3_North", "COI"): 2, ("Plot_4_North", "ITS2"): 1}
    assert {entry["label"]: entry["reads_blast_assigned"] for entry in
            data["sample_metrics"].values()} == {"Plot_3_North": 2, "Plot_4_North": 1}
    assert {key: data["read_fate"][key] for key in
            ("demux_total_reads", "blast_seen_reads", "blast_assigned_reads")} == {
                "demux_total_reads": 3, "blast_seen_reads": 3,
                "blast_assigned_reads": 3}


def test_all_mapped_rows_mismatching_keep_valid_empty_round(tmp_path):
    wrong = [("x1", "COI", "Plot_4_North_ITS2"),
             ("x2", "ITS2", "Plot_3_North_COI")]
    identity, roster, blast, blast_otu, demult, otu_def, env = producer_case(tmp_path, wrong)
    samples = tmp_path / "samples.txt"
    assert run(["bash", str(BIN / "detect_consensus_sample_mode.sh"), str(blast),
                str(samples), str(roster)],
               env=env | {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}).returncode == 0
    parts = tmp_path / "parts"
    result = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                  str(blast), str(samples), str(parts)],
                 env=env | {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)})
    assert result.returncode == 0, result.stderr
    assert all(not p.read_text(encoding="utf-8") for p in parts.glob("*.blast.tsv"))
    data, result = run_producer_report(tmp_path, identity, roster, blast_otu, demult, otu_def, env)
    assert result.stderr.count("marker/unit mismatch") == 1
    assert not data["otu"]["assignments_by_level"]["species"]
    assert all(entry["reads_demux"] is None for entry in data["sample_metrics"].values())
    assert {entry["label"] for entry in data["sample_metrics"].values()} == {
        "Plot_3_North", "Plot_4_North"}


def test_one_mismatch_among_many_valid_rows_keeps_round(tmp_path):
    reads = [(f"v{i}", "COI", "Plot_3_North_COI") for i in range(5)]
    reads += [("x1", "COI", "Plot_4_North_ITS2")]
    identity, roster, blast, blast_otu, demult, otu_def, env = producer_case(tmp_path, reads)
    samples = tmp_path / "samples.txt"
    helper_env = env | {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}
    assert run(["bash", str(BIN / "detect_consensus_sample_mode.sh"),
                str(blast), str(samples), str(roster)], env=helper_env).returncode == 0
    parts = tmp_path / "parts"
    partition = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                     str(blast), str(samples), str(parts)], env=helper_env)
    assert partition.returncode == 0, partition.stderr
    assert "excluded 1 row(s)" in partition.stderr
    assert {line.split("\t")[0].split("|")[0] for line in
            (parts / "Plot_3_North.blast.tsv").read_text(encoding="utf-8").splitlines()} == {
                f"v{i}" for i in range(5)}
    assert (parts / "Plot_4_North.blast.tsv").read_text(encoding="utf-8") == ""
    data, report = run_producer_report(tmp_path, identity, roster, blast_otu, demult, otu_def, env)
    assert "excluded 1 read(s)" in report.stderr
    assert {entry["label"]: entry["reads_demux"] for entry in data["sample_metrics"].values()} == {
        "Plot_3_North": 5, "Plot_4_North": None}
    assert {(row["sample"], row["marker"], row["otu_reads_sample_total"])
            for row in data["otu"]["assignments_by_level"]["species"]} == {
                ("Plot_3_North", "COI", 5)}


def test_cumulative_exclusion_and_global_totals_match_physical_removal(tmp_path):
    valid = [("v1", "COI", "Plot_3_North_COI"),
             ("v2", "COI", "Plot_4_North_COI"),
             ("v4", "COI", "Plot_3_North_COI_2"),
             ("old", "COI", "Plot_3_North_COI_2"),
             ("v3", "ITS2", "Plot_4_North_ITS2")]
    mismatches = [("x1", "COI", "Plot_4_North_ITS2"),
                  ("x2", "ITS2", "Plot_3_North_COI")]

    def prepare(path, reads, cumulative_only_mismatch=False):
        identity, roster, _, blast_otu, demult, otu_def, env = producer_case(path, reads)
        with identity.open("a", encoding="utf-8") as stream:
            stream.write("Plot_3_North\tCOI\tfallback\tCOI_2\tPlot_3_North_COI_2\n")
        otu_def.write_text("".join(
            line.replace("OTUB_0-COI", "OTUB_2-COI") if line.startswith("old\t") else line
            for line in otu_def.read_text(encoding="utf-8").splitlines(keepends=True)),
            encoding="utf-8")
        header, *rows = blast_otu.read_text(encoding="utf-8").splitlines(keepends=True)
        rows = [row.replace("OTUB_0-COI", "OTUB_2-COI") if row.startswith("old\t") else row
                for row in rows]
        cumulative = path / "blast_otu_cumulative.tsv"
        cumulative_rows = list(rows)
        if cumulative_only_mismatch:
            cumulative_rows.append(
                "xearly\tCOI\thac\tPlot_4_North_ITS2\thit\t123\t100\t99"
                "\tOTUB_0-COI\tF1\tG1\tS1\n")
        cumulative.write_text(header + "".join(cumulative_rows), encoding="utf-8")
        blast_otu.write_text(header + "".join(row for row in rows
                                                if not row.startswith("old\t")), encoding="utf-8")
        sizes = path / "otu_sizes_round.tsv"
        sizes.write_text("otu_id\tsize\nOTUB_0-COI\t4\nOTUB_2-COI\t1\nOTUB_1-ITS2\t2\n"
                         if mismatches and len(reads) > len(valid)
                         else "otu_id\tsize\nOTUB_0-COI\t3\nOTUB_2-COI\t1\nOTUB_1-ITS2\t1\n",
                         encoding="utf-8")
        return identity, roster, blast_otu, demult, otu_def, env, cumulative, sizes

    candidate_path = tmp_path / "candidate"
    case = prepare(candidate_path, valid + mismatches, cumulative_only_mismatch=True)
    identity, roster, blast_otu, demult, otu_def, env, cumulative, sizes = case
    cumulative_bytes = cumulative.read_bytes()
    candidate, result = run_producer_report(
        candidate_path, identity, roster, blast_otu, demult, otu_def, env,
        cumulative=cumulative, sizes=sizes)
    assert result.stderr.count("marker/unit mismatch") == 1
    assert "excluded 3 read(s)" in result.stderr
    assert cumulative.read_bytes() == cumulative_bytes

    oracle_path = tmp_path / "physically_removed"
    oracle_case = prepare(oracle_path, valid)
    oracle, _ = run_producer_report(oracle_path, *oracle_case[:6],
                                    cumulative=oracle_case[6], sizes=oracle_case[7])
    for section in ("otu", "sample_metrics", "read_fate"):
        assert candidate[section] == oracle[section]

    rows = candidate["otu"]["assignments_by_level"]["species"]
    assert {(row["sample"], row["marker"], row["otu_reads_sample_total"],
             row["otu_reads_global_total"]) for row in rows} == {
                 ("Plot_3_North", "COI", 3, 4),
                 ("Plot_4_North", "COI", 1, 3),
                 ("Plot_4_North", "ITS2", 1, 1),
             }
    coi_rows = [row for row in rows if row["marker"] == "COI"]
    assert sum(row["otu_reads_sample_total"] for row in coi_rows) == 4
    assert {row["sample"]: row["otu_reads_global_total"] for row in coi_rows} == {
        "Plot_3_North": 4, "Plot_4_North": 3}
    assert next(row for row in coi_rows if row["sample"] == "Plot_3_North")["otu_count"] == 2
    p3 = next(row for row in coi_rows if row["sample"] == "Plot_3_North")
    assert {rep["label"]: rep["count"] for rep in p3["replicate_reads"]} == {
        "Plot_3_North_COI": 1, "rep_2": 1}
    assert not any("x1" in str(row) or "x2" in str(row) or "xearly" in str(row)
                   for row in rows)


def test_global_totals_without_mismatch_or_map_keep_existing_sizes(tmp_path):
    reads = [("v1", "COI", "Plot_3_North_COI"),
             ("v2", "COI", "Plot_4_North_COI")]
    identity, roster, _, blast_otu, demult, otu_def, env = producer_case(tmp_path, reads)
    sizes = tmp_path / "sizes.tsv"
    sizes.write_text("otu_id\tsize\nOTUB_0-COI\t7\n", encoding="utf-8")
    mapped, _ = run_producer_report(tmp_path, identity, roster, blast_otu, demult,
                                     otu_def, env, sizes=sizes)
    assert {row["otu_reads_global_total"] for row in
            mapped["otu"]["assignments_by_level"]["species"]} == {7}
    absent, _ = run_producer_report(tmp_path, identity, roster, blast_otu, demult,
                                     otu_def, env, mapped=False, sizes=sizes)
    assert {row["otu_reads_global_total"] for row in
            absent["otu"]["assignments_by_level"]["species"]} == {7}


def test_mismatch_order_seed_and_locale_are_scientifically_invariant(tmp_path):
    reads = [("v1", "COI", "Plot_3_North_COI"),
             ("v2", "ITS2", "Plot_4_North_ITS2"),
             ("x1", "COI", "Plot_4_North_ITS2"),
             ("x2", "ITS2", "Plot_3_North_COI")]
    locales = subprocess.check_output(["locale", "-a"], text=True).splitlines()
    utf8 = next(name for name in locales if "UTF-8" in name.upper() or "UTF8" in name.upper())
    snapshots = []
    for reverse in (False, True):
        for seed in ("0", "17"):
            for locale in ("C", utf8):
                path = tmp_path / f"{reverse}_{seed}_{locale}"
                identity, roster, blast, blast_otu, demult, otu_def, env = producer_case(
                    path, reads, reverse_map=reverse, reverse_blast=reverse,
                    seed=seed, locale=locale)
                samples = path / "samples.txt"
                helper_env = env | {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": str(identity)}
                assert run(["bash", str(BIN / "detect_consensus_sample_mode.sh"),
                            str(blast), str(samples), str(roster)], env=helper_env).returncode == 0
                parts = path / "parts"
                partition = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                                 str(blast), str(samples), str(parts)], env=helper_env)
                assert partition.returncode == 0, partition.stderr
                assert partition.stderr.count("marker/unit mismatch") == 1
                data, report = run_producer_report(
                    path, identity, roster, blast_otu, demult, otu_def, env)
                assert report.stderr.count("marker/unit mismatch") == 1
                partitions = {p.name: sorted(line.split("\t")[0].split("|")[0]
                                             for line in p.read_text(encoding="utf-8").splitlines())
                              for p in parts.glob("*.blast.tsv")}
                metrics = {v["label"]: (v["reads_demux"], v["reads_blast_assigned"])
                           for v in data["sample_metrics"].values()}
                members = sorted((r["sample"], r["marker"], r["otu_reads_sample_total"])
                                 for r in data["otu"]["assignments_by_level"]["species"])
                partition_warning = next(line for line in partition.stderr.splitlines()
                                         if "marker/unit mismatch" in line)
                report_warning = next(line for line in report.stderr.splitlines()
                                      if "marker/unit mismatch" in line)
                snapshots.append((partitions, metrics, members,
                                  partition_warning, report_warning))
    assert all(snapshot == snapshots[0] for snapshot in snapshots)
    assert snapshots[0][0] == {"Plot_3_North.blast.tsv": ["v1"],
                                "Plot_4_North.blast.tsv": ["v2"]}


def test_marker_conflict_has_no_new_legacy_drop_or_warning(tmp_path):
    reads = [("v1", "COI", "Plot_3_North_COI"),
             ("x1", "COI", "Plot_4_North_ITS2")]
    identity, roster, blast, blast_otu, demult, otu_def, env = producer_case(tmp_path, reads)
    legacy_env = env | {"RTBIOSCAN_REPLICATE_IDENTITY_TSV": ""}
    samples = tmp_path / "legacy_samples.txt"
    assert run(["bash", str(BIN / "detect_consensus_sample_mode.sh"),
                str(blast), str(samples), str(roster)], env=legacy_env).returncode == 0
    parts = tmp_path / "legacy_parts"
    partition = run(["bash", str(BIN / "partition_blast_rows_by_adapter_class.sh"),
                     str(blast), str(samples), str(parts)], env=legacy_env)
    assert partition.returncode == 0, partition.stderr
    assert "marker/unit mismatch" not in partition.stderr
    assert sum(len(p.read_text(encoding="utf-8").splitlines()) for p in
               parts.glob("*.blast.tsv")) == 2
    data, report = run_producer_report(
        tmp_path, identity, roster, blast_otu, demult, otu_def, legacy_env, mapped=False)
    assert "marker/unit mismatch" not in report.stderr
    assert sum(entry["reads_demux"] or 0 for entry in data["sample_metrics"].values()) == 2


@pytest.mark.parametrize("seed,locale,reverse", [("0", "C", False),
                                                   ("4242", "en_US.UTF-8", True)])
def test_r4d_round_and_cumulative_sidecars_match_physical_removal(tmp_path, seed, locale, reverse):
    """The oracle copies production-shaped artifacts and removes only x1 rows."""
    import hashlib
    import importlib.util
    import shutil
    spec = importlib.util.spec_from_file_location(
        "r4_fixture", ROOT / "tests" / "test_r4_reporting_contract.py")
    r4 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r4)

    for round_has_x, cumulative_has_x, better in ((True, False, False),
                                                   (False, True, False),
                                                   (True, True, False),
                                                   (True, True, True)):
        case = tmp_path / f"r{int(round_has_x)}c{int(cumulative_has_x)}b{int(better)}"
        current = case / "candidate"
        current.mkdir(parents=True)
        identity = current / "identity.tsv"
        identity.write_text(HEADER + "".join(
            f"{sample}\t{marker}\tmarker\t{marker}\t{sample}_{marker}\n"
            for sample in ("Plot_3_North", "Plot_4_North")
            for marker in ("COI", "ITS2")), encoding="utf-8")
        valid_coi = [r4.member("v1", adapter="Plot_3_North_COI"),
                     r4.member("v2", adapter="Plot_4_North_COI")]
        excluded = r4.member("x1", adapter="Plot_4_North_ITS2")
        valid_its = [r4.member("v3", adapter="Plot_4_North_ITS2",
                               taxid="-1156", ranks=r4.PLANT)]
        old = [r4.member("old", adapter="Plot_3_North_COI", taxid="555")]
        def make_model(has_x, historical):
            current_ranks = r4.METAZOA[:5] + ["NA", "NA"] if better and not historical else r4.METAZOA
            model = [r4.otu(0, ranks=current_ranks,
                            members=valid_coi + ([excluded] if has_x else [])),
                     r4.otu(1, marker="ITS2", taxid="-1156", ranks=r4.PLANT,
                            members=valid_its)]
            if historical:
                model.append(r4.otu(2, taxid="555", members=old))
            return model
        for folder, has_x, historical in ((current / "round", round_has_x, False),
                                          (current / "cumulative", cumulative_has_x, True)):
            r4.write_inputs(folder, make_model(has_x, historical), reverse=reverse)
            r4.run_build(folder)
            r4.write_otu_def(folder, make_model(has_x, historical))
        oracle = case / "oracle"
        shutil.copytree(current, oracle)
        for folder in (oracle / "round", oracle / "cumulative"):
            public = folder / "RTBioScan_blast_otu_pretax_rpt.txt"
            public.write_text("".join(line for line in public.read_text().splitlines(keepends=True)
                                      if not line.startswith("x1|")), encoding="utf-8")
            sidecar = folder / "RTBioScan_blast_otu_reporting_v1.tsv"
            lines = sidecar.read_text().splitlines(keepends=True)
            header = lines[:2]
            columns = header[1].rstrip("\n").split("\t")[1:]
            member_col = columns.index("member_count")
            rows = [line.split("\t") for line in lines[2:-1]
                    if not line.startswith("x1|COI\t")]
            counts = {}
            for row in rows:
                counts[row[columns.index("display_otu_key")]] = (
                    counts.get(row[columns.index("display_otu_key")], 0) + 1)
            for row in rows:
                row[member_col] = str(counts[row[columns.index("display_otu_key")]])
            body = "".join("\t".join(row) for row in rows)
            sidecar.write_text("".join(header) + body +
                               f"#END\t{len(rows)}\t{hashlib.sha256(body.encode()).hexdigest()}\n",
                               encoding="utf-8")
            otu_def = folder / "otu_def.tsv"
            otu_def.write_text("".join(line for line in otu_def.read_text().splitlines(keepends=True)
                                       if not line.startswith("x1\t")), encoding="utf-8")
            sizes = folder / "otu_sizes_round.tsv"
            sizes.write_text("otu_id\tsize\n" + "".join(
                f"{otu}\t{count}\n" for otu, count in sorted(counts.items())), encoding="utf-8")
        def report(root):
            rr, cc = root / "round", root / "cumulative"
            args = ["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA",
                    "--barcode", "RTBioScan", "--round-barcode", "round_1",
                    "--timestamp-utc", "2026-09-26T00:00:00Z", "--out", str(root / "out.json"),
                    "--replicate-identity", str(root / "identity.tsv"),
                    "--blast-otu", str(rr / "RTBioScan_blast_otu_pretax_rpt.txt"),
                    "--blast-otu-reporting", str(rr / "RTBioScan_blast_otu_reporting_v1.tsv"),
                    "--otu-def", str(rr / "otu_def.tsv"),
                    "--otu-sizes-round", str(rr / "otu_sizes_round.tsv"),
                    "--otu-lock-summary", str(rr / "lock.tsv"),
                    "--blast-otu-cumulative", str(cc / "RTBioScan_blast_otu_pretax_rpt.txt"),
                    "--blast-otu-reporting-cumulative", str(cc / "RTBioScan_blast_otu_reporting_v1.tsv")]
            result = run(args, env={"RTBIOSCAN_TARGET_TOKENS": "COI|ITS2",
                                    "RTBIOSCAN_TARGET_TAXA": "Metazoa|Viridiplantae",
                                    "PERL_HASH_SEED": seed, "LC_ALL": locale})
            assert result.returncode == 0, result.stderr
            return json.loads((root / "out.json").read_text())
        before = [(p, p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns)
                  for p in current.rglob("*reporting_v1.tsv")]
        actual, expected = report(current), report(oracle)
        assert all((p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) ==
                   (data, inode, mtime) for p, data, inode, mtime in before)
        for section in ("taxonomy_assignment", "otu", "sample_metrics", "read_fate"):
            assert actual[section] == expected[section]
        assert all(row["sample"] != "Plot_4_North_ITS2" or row["marker"] != "COI"
                   for side in ("round", "cumulative")
                   for row in actual["taxonomy_assignment"][side]["by_sample_marker"])
        assert actual["taxonomy_assignment"]["cumulative"]["canonical_member_count"] >= 4
        if better:
            assert actual["taxonomy_assignment"]["cumulative"]["canonical_assigned_count"]["species"] > (
                actual["taxonomy_assignment"]["round"]["canonical_assigned_count"]["species"])
        else:
            assert all(actual["taxonomy_assignment"]["reconciliation"][rank]["consistent"]
                       for rank in ("family", "genus", "species"))


def test_frozen_membership_excludes_only_cross_marker_read(tmp_path):
    import shutil
    reads = [("v1", "COI", "Plot_3_North_COI"),
             ("v2", "COI", "Plot_4_North_COI"),
             ("x1", "COI", "Plot_4_North_ITS2"),
             ("v3", "ITS2", "Plot_4_North_ITS2")]
    current = tmp_path / "candidate"
    identity, roster, _, blast_otu, demult, otu_def, env = producer_case(current, reads)
    (current / "lock.tsv").write_text("otu_key\teffective_consolidated\tis_frozen\n"
                                       "OTUB_0-COI\t0\t1\nOTUB_1-ITS2\t0\t0\n")
    (current / "sizes.tsv").write_text("otu_id\tsize\nOTUB_0-COI\t3\nOTUB_1-ITS2\t1\n")
    (current / "RTBioScan_otu_hash_map.tsv").write_text("v1\th1\n")
    state = current / "_state"
    state.mkdir()
    (state / "otu_frozen_meta.tsv").write_text("FROZEN_h1\tv1\th1\n")
    members = state / "otu_frozen_members.tsv"
    members.write_text("".join(f"FROZEN_h1\t{rid}|{mk}|hac|barcode={mk}|adapter={unit}\t0\n"
                               for rid, mk, unit in reads[:3]))
    shutil.copyfile(demult, current / "read_fate_demult.tsv")
    shutil.copyfile(blast_otu, current / "read_fate_blast.tsv")
    (current / "blast_unassigned.list").write_text("x1\n")
    oracle = tmp_path / "oracle"
    shutil.copytree(current, oracle)
    for name in ("RTBioScan_demult_rpt.txt", "RTBioScan_otu_def_rpt.txt", "blast_otu.tsv",
                 "read_fate_demult.tsv", "read_fate_blast.tsv"):
        path = oracle / name
        path.write_text("".join(line for line in path.read_text().splitlines(keepends=True)
                                if not line.startswith("x1\t")))
    path = oracle / "_state" / "otu_frozen_members.tsv"
    path.write_text("".join(line for line in path.read_text().splitlines(keepends=True)
                            if not line.startswith("FROZEN_h1\tx1|")))
    (oracle / "sizes.tsv").write_text("otu_id\tsize\nOTUB_0-COI\t2\nOTUB_1-ITS2\t1\n")
    (oracle / "blast_unassigned.list").write_text("")
    def report(path):
        args = ["perl", str(BIN / "report_round_json.pl"), "--run-id", "runA", "--barcode", "RTBioScan",
                "--round-barcode", "round_1", "--timestamp-utc", "2026-09-26T00:00:00Z",
                "--out", str(path / "out.json"), "--replicate-identity", str(path / "replicate_identity.tsv"),
                "--sample-roster", str(path / "roster.tsv"), "--demult", str(path / "RTBioScan_demult_rpt.txt"),
                "--otu-def", str(path / "RTBioScan_otu_def_rpt.txt"), "--blast-otu", str(path / "blast_otu.tsv"),
                "--otu-lock-summary", str(path / "lock.tsv"), "--otu-sizes-round", str(path / "sizes.tsv"),
                "--read-fate-demult", str(path / "read_fate_demult.tsv"),
                "--read-fate-blast", str(path / "read_fate_blast.tsv"),
                "--blast-unassigned-ids", str(path / "blast_unassigned.list")]
        result = run(args, env=env)
        assert result.returncode == 0, result.stderr
        return json.loads((path / "out.json").read_text())
    source = members.read_bytes()
    actual, expected = report(current), report(oracle)
    assert members.read_bytes() == source
    for section in ("taxonomy_assignment", "otu", "sample_metrics", "read_fate"):
        assert actual[section] == expected[section]
    rows = actual["otu"]["assignments_by_level"]["species"]
    assert {(row["sample"], row["marker"], row["otu_reads_sample_total"])
            for row in rows} == {("Plot_3_North", "COI", 1), ("Plot_4_North", "COI", 1),
                                ("Plot_4_North", "ITS2", 1)}
