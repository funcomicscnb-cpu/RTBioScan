import csv
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "audit_taxonomy_reference_source_integrity.py"
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"
ANOMALIES = FIXTURE_DIR / "chain_a_downstream_coi_source_integrity_v1.tsv"
PROVENANCE = (
    FIXTURE_DIR / "chain_a_downstream_coi_source_integrity_v1_provenance.tsv"
)


def load_module():
    spec = importlib.util.spec_from_file_location("source_integrity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_tsv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def write_tiny_inputs(tmp_path, indexed_second_sequence="GT"):
    source = tmp_path / "source.fasta"
    source.write_text(
        ">opaque_a|kraken:taxid|1 alpha\n"
        "AC\n"
        ">opaque_b|kraken:taxid|-2 beta\n"
        "GT>opaque_hidden|kraken:taxid|3 hidden\n"
        "NN\n"
        ">opaque_duplicate|kraken:taxid|4 first\n"
        "AA\n"
        ">opaque_duplicate|kraken:taxid|5 second\n",
        encoding="utf-8",
    )
    database = tmp_path / "legacy"
    component = tmp_path / "legacy.nhr"
    component.write_bytes(b"opaque index component\n")
    metadata = {
        "number-of-sequences": 2,
        "number-of-letters": 4,
        "files": [component.name],
    }
    njs = tmp_path / "legacy.njs"
    njs.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    manifest = tmp_path / "reference_manifest.tsv"
    manifest.write_text(
        "artifact\tsha256\trole\n"
        f"{njs.name}\t{hashlib.sha256(njs.read_bytes()).hexdigest()}\tmetadata\n"
        f"{component.name}\t{hashlib.sha256(component.read_bytes()).hexdigest()}\tindex\n",
        encoding="utf-8",
    )
    disposition_manifest = tmp_path / "disposition_manifest.tsv"
    disposition_manifest.write_text(
        "reference_id\treference_sequence_sha256\tstored_taxid\trelease_action\n"
        "opaque_a\t"
        f"{hashlib.sha256(b'AC').hexdigest()}\t1\tquarantine\n"
        "opaque_b\t"
        f"{hashlib.sha256(b'GT').hexdigest()}\t-2\tretain\n",
        encoding="utf-8",
    )
    disposition_provenance = tmp_path / "disposition_provenance.tsv"
    disposition_provenance.write_text(
        "field\tartifact\tvalue\n"
        "schema\t\ttaxonomy_reference_disposition_v1\n"
        "sha256\treference_source_fasta\t"
        f"{hashlib.sha256(source.read_bytes()).hexdigest()}\n"
        "sha256\tdisposition_manifest\t"
        f"{hashlib.sha256(disposition_manifest.read_bytes()).hexdigest()}\n",
        encoding="utf-8",
    )
    export = tmp_path / "blast_export.tsv"
    export.write_text(
        "0\topaque_a|kraken:taxid|1 alpha\t2\tAC\n"
        f"1\topaque_b|kraken:taxid|-2 beta\t2\t{indexed_second_sequence}\n",
        encoding="utf-8",
    )
    fake = tmp_path / "blastdbcmd"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib\n"
        "import sys\n"
        "if '-version' in sys.argv:\n"
        "    print('blastdbcmd: synthetic')\n"
        "else:\n"
        f"    sys.stdout.write(pathlib.Path({str(export)!r}).read_text())\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return {
        "source": source,
        "database": database,
        "manifest": manifest,
        "disposition_manifest": disposition_manifest,
        "disposition_provenance": disposition_provenance,
        "blastdbcmd": fake,
    }


def run_audit(inputs, anomaly_output, provenance_output):
    return subprocess.run(
        [
            str(SCRIPT),
            "--source-fasta",
            str(inputs["source"]),
            "--blast-database",
            str(inputs["database"]),
            "--blastdbcmd",
            str(inputs["blastdbcmd"]),
            "--reference-manifest",
            str(inputs["manifest"]),
            "--reference-root",
            str(inputs["manifest"].parent),
            "--disposition-provenance",
            str(inputs["disposition_provenance"]),
            "--disposition-manifest",
            str(inputs["disposition_manifest"]),
            "--scope",
            "opaque_downstream_scope",
            "--anomaly-output",
            str(anomaly_output),
            "--provenance-output",
            str(provenance_output),
        ],
        capture_output=True,
        text=True,
    )


def test_auditor_reports_boundary_duplicate_and_empty_without_release_action(tmp_path):
    inputs = write_tiny_inputs(tmp_path)
    anomalies = tmp_path / "anomalies.tsv"
    provenance = tmp_path / "provenance.tsv"
    completed = run_audit(inputs, anomalies, provenance)
    assert completed.returncode == 0, completed.stderr

    rows = read_tsv(anomalies)
    assert [row["anomaly_class"] for row in rows] == [
        "embedded_header_candidate",
        "line_oriented_index_sequence_mismatch",
        "duplicate_reference_id",
        "empty_sequence_record",
    ]
    embedded = rows[0]
    assert embedded["reference_id"] == "opaque_hidden"
    assert embedded["source_sequence_length"] == "2"
    assert "comparison only" in embedded["interpretation"]
    assert "no source repair or release action" in embedded["interpretation"]
    mismatch = rows[1]
    assert mismatch["source_sequence_length"] == str(
        len("GT>opaque_hidden|kraken:taxid|3 hiddenNN")
    )
    assert mismatch["comparison_sequence_length"] == "2"

    values = {
        (row["field"], row["artifact"]): row["value"]
        for row in read_tsv(provenance)
    }
    assert values[("scope", "source_mutation")] == "none"
    assert values[("scope", "release_policy")] == "none"
    assert values[("scope", "artifact_set_commit_marker")] == (
        "provenance_output_written_last"
    )
    assert values[("coordinate", "source_line")] == "one_based"
    assert values[("coordinate", "source_byte_offset")] == "zero_based"
    assert values[("count", "source_line_start_records")] == "4"
    assert values[("count", "analysis_split_logical_records")] == "5"
    assert values[("count", "line_start_unindexed_records")] == "2"
    assert values[("count", "analysis_split_unindexed_records")] == "3"
    assert values[("count", "disposition_records_in_legacy_index")] == "2"
    assert values[("count", "disposition:quarantine")] == "1"
    assert values[("count", "disposition:retain")] == "1"
    assert values[("sha256", "analysis_split_selected_canonical_stream")] == values[
        ("sha256", "legacy_index_canonical_stream")
    ]

    first_anomalies = anomalies.read_bytes()
    first_provenance = provenance.read_bytes()
    repeated = run_audit(inputs, anomalies, provenance)
    assert repeated.returncode == 0, repeated.stderr
    assert anomalies.read_bytes() == first_anomalies
    assert provenance.read_bytes() == first_provenance


def test_auditor_rejects_unexplained_index_mismatch_without_outputs(tmp_path):
    inputs = write_tiny_inputs(tmp_path, indexed_second_sequence="GA")
    anomalies = tmp_path / "anomalies.tsv"
    provenance = tmp_path / "provenance.tsv"
    completed = run_audit(inputs, anomalies, provenance)
    assert completed.returncode != 0
    assert "sequence mismatch after analysis split" in completed.stderr
    assert not anomalies.exists()
    assert not provenance.exists()


def test_auditor_rejects_stale_source_and_index_bindings(tmp_path):
    stale_source_dir = tmp_path / "stale_source"
    stale_source_dir.mkdir()
    stale_source = write_tiny_inputs(stale_source_dir)
    stale_source["source"].write_text(
        stale_source["source"].read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )
    source_result = run_audit(
        stale_source,
        stale_source_dir / "anomalies.tsv",
        stale_source_dir / "provenance.tsv",
    )
    assert source_result.returncode != 0
    assert "does not bind the audited source FASTA" in source_result.stderr
    assert not (stale_source_dir / "anomalies.tsv").exists()
    assert not (stale_source_dir / "provenance.tsv").exists()

    stale_index_dir = tmp_path / "stale_index"
    stale_index_dir.mkdir()
    stale_index = write_tiny_inputs(stale_index_dir)
    (stale_index_dir / "legacy.nhr").write_bytes(b"changed component\n")
    index_result = run_audit(
        stale_index,
        stale_index_dir / "anomalies.tsv",
        stale_index_dir / "provenance.tsv",
    )
    assert index_result.returncode != 0
    assert "component checksum mismatch" in index_result.stderr
    assert not (stale_index_dir / "anomalies.tsv").exists()
    assert not (stale_index_dir / "provenance.tsv").exists()


def test_auditor_never_overwrites_a_pinned_index_component(tmp_path):
    inputs = write_tiny_inputs(tmp_path)
    component = tmp_path / "legacy.nhr"
    original = component.read_bytes()
    provenance = tmp_path / "provenance.tsv"
    completed = run_audit(inputs, component, provenance)
    assert completed.returncode != 0
    assert "overlaps a pinned BLAST component" in completed.stderr
    assert component.read_bytes() == original
    assert not provenance.exists()

    executable = Path(inputs["blastdbcmd"])
    executable_bytes = executable.read_bytes()
    executable_provenance = tmp_path / "executable-provenance.tsv"
    executable_result = run_audit(
        inputs,
        executable,
        executable_provenance,
    )
    assert executable_result.returncode != 0
    assert "overlaps an audit executable" in executable_result.stderr
    assert executable.read_bytes() == executable_bytes
    assert not executable_provenance.exists()

    auditor_bytes = SCRIPT.read_bytes()
    auditor_provenance = tmp_path / "auditor-provenance.tsv"
    auditor_result = run_audit(inputs, SCRIPT, auditor_provenance)
    assert auditor_result.returncode != 0
    assert "overlaps an audit executable" in auditor_result.stderr
    assert SCRIPT.read_bytes() == auditor_bytes
    assert not auditor_provenance.exists()


def test_auditor_accepts_multivolume_components_with_repeated_suffixes(tmp_path):
    inputs = write_tiny_inputs(tmp_path)
    second_component = tmp_path / "legacy.01.nhr"
    second_component.write_bytes(b"second opaque index component\n")
    metadata_path = tmp_path / "legacy.njs"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["files"].append(second_component.name)
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    manifest = inputs["manifest"]
    manifest.write_text(
        "artifact\tsha256\trole\n"
        f"{metadata_path.name}\t{hashlib.sha256(metadata_path.read_bytes()).hexdigest()}\tmetadata\n"
        "legacy.nhr\t"
        f"{hashlib.sha256((tmp_path / 'legacy.nhr').read_bytes()).hexdigest()}\tindex\n"
        f"{second_component.name}\t{hashlib.sha256(second_component.read_bytes()).hexdigest()}\tindex\n",
        encoding="utf-8",
    )
    anomalies = tmp_path / "anomalies.tsv"
    provenance = tmp_path / "provenance.tsv"
    completed = run_audit(inputs, anomalies, provenance)
    assert completed.returncode == 0, completed.stderr
    component_artifacts = {
        row["artifact"]
        for row in read_tsv(provenance)
        if row["field"] == "sha256"
        and row["artifact"].startswith("legacy_blast_component:")
    }
    assert component_artifacts == {
        "legacy_blast_component:legacy.njs",
        "legacy_blast_component:legacy.nhr",
        "legacy_blast_component:legacy.01.nhr",
    }


def test_auditor_rejects_repeated_component_paths(tmp_path):
    inputs = write_tiny_inputs(tmp_path)
    metadata_path = tmp_path / "legacy.njs"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["files"].append(metadata["files"][0])
    metadata_path.write_text(json.dumps(metadata, sort_keys=True) + "\n", encoding="utf-8")
    manifest = inputs["manifest"]
    component = tmp_path / "legacy.nhr"
    manifest.write_text(
        "artifact\tsha256\trole\n"
        f"{metadata_path.name}\t{hashlib.sha256(metadata_path.read_bytes()).hexdigest()}\tmetadata\n"
        f"{component.name}\t{hashlib.sha256(component.read_bytes()).hexdigest()}\tindex\n",
        encoding="utf-8",
    )
    anomalies = tmp_path / "anomalies.tsv"
    provenance = tmp_path / "provenance.tsv"
    completed = run_audit(inputs, anomalies, provenance)
    assert completed.returncode != 0
    assert "repeats a component path" in completed.stderr
    assert not anomalies.exists()
    assert not provenance.exists()


def test_auditor_rejects_disposition_taxid_mismatch(tmp_path):
    inputs = write_tiny_inputs(tmp_path)
    manifest = inputs["disposition_manifest"]
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("\t-2\tretain", "\t999\tretain"),
        encoding="utf-8",
    )
    provenance = inputs["disposition_provenance"]
    provenance.write_text(
        provenance.read_text(encoding="utf-8").replace(
            next(
                row["value"]
                for row in read_tsv(provenance)
                if row["field"] == "sha256" and row["artifact"] == "disposition_manifest"
            ),
            hashlib.sha256(manifest.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )
    anomalies = tmp_path / "anomalies.tsv"
    output_provenance = tmp_path / "provenance.tsv"
    completed = run_audit(inputs, anomalies, output_provenance)
    assert completed.returncode != 0
    assert "disposition/index header taxid mismatch" in completed.stderr
    assert not anomalies.exists()
    assert not output_provenance.exists()


def test_auditor_rejects_sequence_line_whitespace_without_outputs(tmp_path):
    inputs = write_tiny_inputs(tmp_path)
    source = inputs["source"]
    source.write_text(
        source.read_text(encoding="utf-8").replace("AC\n", " AC\n", 1),
        encoding="utf-8",
    )
    disposition_provenance = inputs["disposition_provenance"]
    disposition_provenance.write_text(
        disposition_provenance.read_text(encoding="utf-8").replace(
            next(
                row["value"]
                for row in read_tsv(disposition_provenance)
                if row["field"] == "sha256" and row["artifact"] == "reference_source_fasta"
            ),
            hashlib.sha256(source.read_bytes()).hexdigest(),
        ),
        encoding="utf-8",
    )
    anomalies = tmp_path / "anomalies.tsv"
    provenance = tmp_path / "provenance.tsv"
    completed = run_audit(inputs, anomalies, provenance)
    assert completed.returncode != 0
    assert "leading or trailing FASTA sequence whitespace" in completed.stderr
    assert not anomalies.exists()
    assert not provenance.exists()


def test_committed_source_integrity_artifacts_capture_exact_observations():
    assert hashlib.sha256(ANOMALIES.read_bytes()).hexdigest() == (
        "09207fbdba6d44d56bf2db5c9a562fca1f342ab3956a98e61c64a37ae6c208ab"
    )
    assert hashlib.sha256(PROVENANCE.read_bytes()).hexdigest() == (
        "a2aa34a16a8a6fe6e55dc9ad42ac9c02267640621ac7cef2eb42e5ca07334975"
    )
    anomalies = read_tsv(ANOMALIES)
    assert len(anomalies) == 4
    by_class = {row["anomaly_class"]: row for row in anomalies}
    embedded = by_class["embedded_header_candidate"]
    assert embedded["source_line"] == "1582866"
    assert embedded["source_byte_offset"] == "594244630"
    assert embedded["physical_record_ordinal"] == "791433"
    assert embedded["logical_record_ordinal"] == "791434"
    assert embedded["reference_id"] == "XPR26_16_H1"
    assert embedded["related_reference_id"] == "BOLD_COI-5P_GBORT1052-15"
    assert embedded["source_sequence_length"] == "524"
    mismatch = by_class["line_oriented_index_sequence_mismatch"]
    assert mismatch["legacy_index_oid"] == "791432"
    assert mismatch["reference_id"] == "BOLD_COI-5P_GBORT1052-15"
    assert mismatch["related_reference_id"] == "XPR26_16_H1"
    assert mismatch["source_sequence_length"] == "857"
    assert mismatch["comparison_sequence_length"] == "200"
    duplicate = by_class["duplicate_reference_id"]
    assert duplicate["logical_record_ordinal"] == "792927"
    assert duplicate["related_logical_record_ordinal"] == "792926"
    assert by_class["empty_sequence_record"]["source_sequence_length"] == "0"

    values = {
        (row["field"], row["artifact"]): row["value"]
        for row in read_tsv(PROVENANCE)
    }
    expected_counts = {
        "source_line_start_records": "792926",
        "source_line_start_unique_reference_ids": "792925",
        "analysis_split_logical_records": "792927",
        "analysis_split_unique_reference_ids": "792926",
        "analysis_split_total_bases": "487737457",
        "embedded_header_candidates": "1",
        "legacy_index_records": "791433",
        "legacy_index_bases": "485462968",
        "line_start_prefix_sequence_characters": "485463625",
        "analysis_split_selected_bases": "485462968",
        "line_start_unindexed_records": "1493",
        "analysis_split_unindexed_records": "1494",
        "anomaly_rows": "4",
        "disposition_records_in_legacy_index": "44",
        "disposition:quarantine": "29",
        "disposition:retain": "15",
    }
    for name, value in expected_counts.items():
        assert values[("count", name)] == value
    assert values[("sha256", "source_fasta")] == (
        "d0a1ba27438f16843fdffcb4ed30e6e89369ed310f338ec73c1fa058dffe6741"
    )
    assert values[("sha256", "legacy_reference_manifest")] == (
        "cf04ad3c0c3c0580bf9620ad9a26d4e77dddcf0ad8679b36b51b4c62753ad299"
    )
    assert values[("sha256", "disposition_provenance")] == (
        "637e0a12e465500c61c49dc8af04774ac1f8acfbb918917b4a0c27d2ee8b6a70"
    )
    assert values[("sha256", "disposition_manifest")] == (
        "ab75805caf97f7da3b352302ff331e8c85cdb66e878aa989d59d9e6173e8e748"
    )
    assert values[("sha256", "auditor_script")] == (
        "a24195dfc9b196be5acc66ba9f8547c17892ea7033af5f5079326e18dae3d0e8"
    )
    assert values[("sha256", "analysis_split_selected_canonical_stream")] == (
        "cf474aa6924929890719626723d346b95c1af867c63a8ac6ce85ec643ffffc47"
    )
    assert values[("sha256", "legacy_index_canonical_stream")] == values[
        ("sha256", "analysis_split_selected_canonical_stream")
    ]
    assert values[("sha256", "anomaly_table")] == (
        "09207fbdba6d44d56bf2db5c9a562fca1f342ab3956a98e61c64a37ae6c208ab"
    )
    expected_components = {
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.ndb": "6e5b8720162518bd92a5a273e9342072da448b86504ffc889393c2664aea698d",
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.nhr": "2507698a7a1d7f74c5d1aa0156916ee04d88b58eacc5a1104352a576aef5fe3d",
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.nin": "08ecd4c40708f3c4b4b233bf1b600c443db16224dd3f9ecb8dbf3cc6c66f7390",
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.njs": "9f3aabe98f5316f59a2ebdc97ff90fcdab8e34af00c03744cf09c557d2868e4c",
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.not": "8559af815c831ec8623ad2a6dce7b312f333b751d20e89269acc55c0b986efff",
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.nsq": "742290831c56f20acc7fd319a668bd7a33fecd54b2d6309c656b35454333c5c1",
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.ntf": "67982e4d7b1490b09cce06e7c29175c6bd011cd542cfacb1a1c310084a50821a",
        "legacy_blast_component:db/COInr98_2024Jun_RioNegro_Brazil.nto": "00b018440d5585c043b9570a6f2e53154f32618652ca40015068a3e21ee46537",
    }
    observed_components = {
        row["artifact"]: row["value"]
        for row in read_tsv(PROVENANCE)
        if row["field"] == "sha256"
        and row["artifact"].startswith("legacy_blast_component:")
    }
    assert observed_components == expected_components

    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "GBORT1052",
        "XPR26",
        "WPB428",
        "COInr98",
        "791433",
        "792926",
    ):
        assert forbidden not in source


def test_real_source_integrity_replay_is_byte_exact(tmp_path):
    source = REPO_ROOT / "db" / "COInr98_2024Jun_RioNegro_Brazil.fasta"
    database = REPO_ROOT / "db" / "COInr98_2024Jun_RioNegro_Brazil"
    blastdbcmd = shutil.which("blastdbcmd")
    if not source.is_file() or not Path(str(database) + ".njs").is_file():
        pytest.skip("the pinned shipped downstream COI source/index is required")
    if not blastdbcmd:
        pytest.skip("blastdbcmd is required for real source/index replay")
    anomaly_output = tmp_path / "anomalies.tsv"
    provenance_output = tmp_path / "provenance.tsv"
    completed = subprocess.run(
        [
            str(SCRIPT),
            "--source-fasta",
            str(source),
            "--blast-database",
            str(database),
            "--blastdbcmd",
            blastdbcmd,
            "--reference-manifest",
            str(REPO_ROOT / "conf/state_compatibility/reference_manifest_legacy_v1.tsv"),
            "--reference-root",
            str(REPO_ROOT),
            "--disposition-provenance",
            str(
                FIXTURE_DIR
                / "chain_a_downstream_coi_disposition_v1_provenance.tsv"
            ),
            "--disposition-manifest",
            str(
                FIXTURE_DIR
                / "chain_a_downstream_coi_disposition_v1.tsv"
            ),
            "--scope",
            "downstream_coi_only",
            "--anomaly-output",
            str(anomaly_output),
            "--provenance-output",
            str(provenance_output),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert anomaly_output.read_bytes() == ANOMALIES.read_bytes()
    assert provenance_output.read_bytes() == PROVENANCE.read_bytes()
