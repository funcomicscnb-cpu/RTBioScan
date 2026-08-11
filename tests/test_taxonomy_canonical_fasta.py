import csv
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = REPO_ROOT / "bin"
sys.path.insert(0, str(BIN_DIR))
import build_taxonomy_canonical_fasta as builder  # noqa: E402


SCRIPT = BIN_DIR / "build_taxonomy_canonical_fasta.py"
FIXTURE_DIR = REPO_ROOT / "conf" / "taxonomy_regression"
POLICY = FIXTURE_DIR / "chain_a_downstream_coi_base_repair_policy_v1.tsv"
ANOMALIES = FIXTURE_DIR / "chain_a_downstream_coi_source_integrity_v1.tsv"
INTEGRITY_PROVENANCE = (
    FIXTURE_DIR / "chain_a_downstream_coi_source_integrity_v1_provenance.tsv"
)
DISPOSITIONS = FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1.tsv"
DISPOSITION_PROVENANCE = (
    FIXTURE_DIR / "chain_a_downstream_coi_disposition_v1_provenance.tsv"
)
REFERENCE_MANIFEST = (
    REPO_ROOT / "conf" / "state_compatibility" / "reference_manifest_legacy_v1.tsv"
)
CANONICAL_PROVENANCE = (
    FIXTURE_DIR / "chain_a_downstream_coi_canonical_fasta_v1_provenance.tsv"
)
EXCLUDED_OIDS = (
    FIXTURE_DIR / "chain_a_downstream_coi_canonical_fasta_v1_excluded_oids.tsv"
)
CANONICAL_BASENAME = (
    "COInr98_2024Jun_RioNegro_Brazil_chain_a_correctness_first_v1.fasta"
)
RELEASE_ID = "downstream_coi_chain_a_canonical_v1"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def record(oid: int, reference_id: str, taxid: int, sequence: str):
    title = f"{reference_id}|kraken:taxid|{taxid} opaque title"
    return builder.source_audit.BlastRecord(oid, title, len(sequence), sequence)


def disposition(reference_id: str, taxid: int, sequence: str, action: str):
    return {
        "reference_id": reference_id,
        "stored_taxid": str(taxid),
        "reference_sequence_sha256": sha256(sequence.encode("utf-8")),
        "release_action": action,
    }


def read_tsv(path: Path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def load_policy_test_helpers():
    path = REPO_ROOT / "tests" / "test_taxonomy_reference_base_policy.py"
    spec = importlib.util.spec_from_file_location("base_policy_test_helpers", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_tiny_cli_inputs(tmp_path: Path):
    helpers = load_policy_test_helpers()
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    inputs = helpers.make_inputs(input_root)

    database_dir = input_root / "db"
    database_dir.mkdir()
    database = database_dir / "legacy"
    component = database_dir / "legacy.nhr"
    component.write_bytes(b"opaque index component\n")
    metadata = database_dir / "legacy.njs"
    metadata.write_text(
        json.dumps(
            {
                "number-of-sequences": 2,
                "number-of-letters": 4,
                "files": [component.name],
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    inputs["reference_manifest"].write_text(
        "artifact\tsha256\trole\n"
        f"db/{metadata.name}\t{sha256(metadata.read_bytes())}\tmetadata\n"
        f"db/{component.name}\t{sha256(component.read_bytes())}\tindex\n",
        encoding="utf-8",
    )

    export = input_root / "blast_export.tsv"
    export.write_text(
        "0\topaque_a|kraken:taxid|1 alpha\t2\tAC\n"
        "1\topaque_b|kraken:taxid|-2 beta\t2\tGT\n",
        encoding="utf-8",
    )
    fake = input_root / "blastdbcmd"
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

    base_stream = b">opaque_a|kraken:taxid|1 alpha\nAC\n>opaque_b|kraken:taxid|-2 beta\nGT\n"
    helpers.replace_key(
        inputs["integrity_provenance"],
        ("sha256", "legacy_reference_manifest"),
        helpers.digest(inputs["reference_manifest"]),
    )
    for key in (
        "legacy_index_canonical_stream",
        "analysis_split_selected_canonical_stream",
    ):
        helpers.replace_key(
            inputs["integrity_provenance"],
            ("sha256", key),
            sha256(base_stream),
        )
    with inputs["integrity_provenance"].open("a", encoding="utf-8") as handle:
        handle.write(
            "sha256\tauditor_script\t"
            f"{sha256((BIN_DIR / 'audit_taxonomy_reference_source_integrity.py').read_bytes())}\n"
        )
    helpers.replace_key(
        inputs["policy"],
        ("sha256", "selected_base_canonical_stream"),
        sha256(base_stream),
    )
    helpers.refresh_direct_policy_hashes(inputs)
    inputs.update(
        {
            "database": database,
            "component": component,
            "export": export,
            "blastdbcmd": fake,
            "reference_root": input_root,
        }
    )
    return inputs


def tiny_command(inputs, output_dir: Path):
    return [
        str(SCRIPT),
        "--policy",
        str(inputs["policy"]),
        "--source-integrity-anomalies",
        str(inputs["anomalies"]),
        "--source-integrity-provenance",
        str(inputs["integrity_provenance"]),
        "--disposition-manifest",
        str(inputs["dispositions"]),
        "--disposition-provenance",
        str(inputs["disposition_provenance"]),
        "--reference-manifest",
        str(inputs["reference_manifest"]),
        "--reference-root",
        str(inputs["reference_root"]),
        "--blast-database",
        str(inputs["database"]),
        "--blastdbcmd",
        str(inputs["blastdbcmd"]),
        "--scope",
        "opaque_scope",
        "--release-id",
        "opaque_release_v1",
        "--output-fasta",
        str(output_dir / "opaque_release.fasta"),
        "--excluded-oids-output",
        str(output_dir / "excluded.tsv"),
        "--provenance-output",
        str(output_dir / "provenance.tsv"),
    ]


def test_constructor_filters_by_full_identity_without_deduplicating():
    records = [
        record(0, "opaque_a", 1, "AC"),
        record(1, "opaque_b", -2, "GT"),
        record(2, "opaque_c", 3, "GT"),
        record(3, "opaque_d", 4, "TT"),
    ]
    dispositions = {
        "opaque_a": disposition("opaque_a", 1, "AC", "quarantine"),
        "opaque_b": disposition("opaque_b", -2, "GT", "retain"),
        "opaque_d": disposition("opaque_d", 4, "TT", "quarantine"),
    }
    import io

    output = io.BytesIO()
    result = builder.construct_records(records, dispositions, output)
    assert output.getvalue() == (
        b">opaque_b|kraken:taxid|-2 opaque title\nGT\n"
        b">opaque_c|kraken:taxid|3 opaque title\nGT\n"
    )
    assert result.output_records == 2
    assert result.output_bases == 4
    assert result.disposition_actions == {"quarantine": 2, "retain": 1}
    assert [item.legacy_oid for item in result.excluded] == [0, 3]


@pytest.mark.parametrize("mismatch", ["taxid", "sequence"])
def test_constructor_rejects_partial_identity_matches(mismatch):
    pinned = disposition("opaque_a", 1, "AC", "quarantine")
    if mismatch == "taxid":
        records = [record(0, "opaque_a", 2, "AC")]
    else:
        records = [record(0, "opaque_a", 1, "AG")]
    import io

    with pytest.raises(SystemExit, match=f"{mismatch} mismatch"):
        builder.construct_records(records, {"opaque_a": pinned}, io.BytesIO())


def test_cli_is_deterministic_and_leaves_legacy_index_untouched(tmp_path):
    inputs = write_tiny_cli_inputs(tmp_path)
    index_before = {
        path: path.read_bytes()
        for path in (inputs["component"], Path(str(inputs["database"]) + ".njs"))
    }
    output_dir = tmp_path / "release"
    completed = subprocess.run(tiny_command(inputs, output_dir), capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert (output_dir / "opaque_release.fasta").read_bytes() == (
        b">opaque_b|kraken:taxid|-2 beta\nGT\n"
    )
    excluded = read_tsv(output_dir / "excluded.tsv")
    assert [(row["legacy_oid"], row["reference_id"]) for row in excluded] == [
        ("0", "opaque_a")
    ]
    values = {
        (row["field"], row["artifact"]): row["value"]
        for row in read_tsv(output_dir / "provenance.tsv")
    }
    assert values[("scope", "stage")] == "construction_only"
    assert values[("scope", "index_mutation")] == "none"
    assert values[("scope", "runtime_activation")] == "none"
    assert values[("count", "canonical_records")] == "1"
    assert values[("count", "canonical_bases")] == "2"

    first_outputs = {
        path.name: path.read_bytes() for path in output_dir.iterdir() if path.is_file()
    }
    refused = subprocess.run(tiny_command(inputs, output_dir), capture_output=True, text=True)
    assert refused.returncode != 0
    assert "output targets already exist" in refused.stderr
    repeated_command = tiny_command(inputs, output_dir) + ["--replace"]
    repeated = subprocess.run(repeated_command, capture_output=True, text=True)
    assert repeated.returncode == 0, repeated.stderr
    assert {
        path.name: path.read_bytes() for path in output_dir.iterdir() if path.is_file()
    } == first_outputs
    assert {
        path: path.read_bytes()
        for path in (inputs["component"], Path(str(inputs["database"]) + ".njs"))
    } == index_before


def test_cli_executes_under_supported_python38_when_available(tmp_path):
    python38 = shutil.which("python3.8")
    if not python38:
        pytest.skip("Python 3.8 is not installed")
    inputs = write_tiny_cli_inputs(tmp_path)
    command = tiny_command(inputs, tmp_path / "release")
    completed = subprocess.run(
        [python38, str(SCRIPT), *command[1:]],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_cli_rejects_live_index_tamper_without_outputs(tmp_path):
    inputs = write_tiny_cli_inputs(tmp_path)
    inputs["component"].write_bytes(b"same policy, changed live bytes\n")
    output_dir = tmp_path / "release"
    completed = subprocess.run(tiny_command(inputs, output_dir), capture_output=True, text=True)
    assert completed.returncode != 0
    assert "component checksum mismatch" in completed.stderr
    assert not output_dir.exists()


def test_cli_rejects_release_fasta_inside_legacy_database_directory(tmp_path):
    inputs = write_tiny_cli_inputs(tmp_path)
    command = tiny_command(inputs, tmp_path / "release")
    output_index = command.index("--output-fasta") + 1
    command[output_index] = str(inputs["database"].parent / "new_release.fasta")
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode != 0
    assert "outside the legacy database directory" in completed.stderr
    assert not Path(command[output_index]).exists()


def test_replacement_invalidates_old_provenance_before_install_failure(
    tmp_path, monkeypatch
):
    destinations = [
        tmp_path / "release.fasta",
        tmp_path / "excluded.tsv",
        tmp_path / "provenance.tsv",
    ]
    staged = []
    for index, destination in enumerate(destinations):
        destination.write_text(f"old-{index}\n", encoding="utf-8")
        temporary = tmp_path / f"staged-{index}"
        temporary.write_text(f"new-{index}\n", encoding="utf-8")
        staged.append((temporary, destination))

    real_replace = builder.os.replace

    def fail_on_excluded(source, destination):
        if Path(destination) == destinations[1]:
            raise OSError("synthetic installation failure")
        real_replace(source, destination)

    monkeypatch.setattr(builder.os, "replace", fail_on_excluded)
    with pytest.raises(OSError, match="synthetic installation failure"):
        builder.install_output_set(staged, destinations[2], replace=True)
    assert destinations[0].read_text(encoding="utf-8") == "new-0\n"
    assert destinations[1].read_text(encoding="utf-8") == "old-1\n"
    assert not destinations[2].exists()
    assert not list(tmp_path.glob(".provenance.tsv.invalid-*"))


def test_nonreplacement_install_never_overwrites_a_racing_target(tmp_path):
    destinations = [
        tmp_path / "release.fasta",
        tmp_path / "excluded.tsv",
        tmp_path / "provenance.tsv",
    ]
    staged = []
    for index, destination in enumerate(destinations):
        temporary = tmp_path / f"staged-{index}"
        temporary.write_text(f"new-{index}\n", encoding="utf-8")
        staged.append((temporary, destination))
    destinations[1].write_text("racing-writer\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        builder.install_output_set(staged, destinations[2], replace=False)
    assert destinations[0].read_text(encoding="utf-8") == "new-0\n"
    assert destinations[1].read_text(encoding="utf-8") == "racing-writer\n"
    assert not destinations[2].exists()


def test_committed_construction_metadata_is_frozen_and_generic():
    assert CANONICAL_PROVENANCE.is_file()
    assert EXCLUDED_OIDS.is_file()
    values = {
        (row["field"], row["artifact"]): row["value"]
        for row in read_tsv(CANONICAL_PROVENANCE)
    }
    assert values[("schema", "")] == "taxonomy_reference_canonical_fasta_v1"
    assert values[("release", "release_id")] == RELEASE_ID
    assert values[("artifact", "canonical_fasta_basename")] == CANONICAL_BASENAME
    assert values[("count", "base_records")] == "791433"
    assert values[("count", "disposition:quarantine")] == "29"
    assert values[("count", "disposition:retain")] == "15"
    assert values[("count", "canonical_records")] == "791404"
    assert values[("sha256", "builder_script")] == sha256(SCRIPT.read_bytes())
    assert values[("sha256", "excluded_legacy_oids")] == sha256(
        EXCLUDED_OIDS.read_bytes()
    )
    assert len(read_tsv(EXCLUDED_OIDS)) == 29

    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("COInr98", "791433", "791404", "BOLD_COI"):
        assert forbidden not in source


def test_real_canonical_fasta_replay_is_byte_exact(tmp_path):
    database = REPO_ROOT / "db" / "COInr98_2024Jun_RioNegro_Brazil"
    blastdbcmd = shutil.which("blastdbcmd")
    if not Path(str(database) + ".njs").is_file():
        pytest.skip("the pinned shipped downstream COI index is required")
    if not blastdbcmd:
        pytest.skip("blastdbcmd is required for real canonical FASTA replay")

    output_fasta = tmp_path / CANONICAL_BASENAME
    excluded = tmp_path / EXCLUDED_OIDS.name
    provenance = tmp_path / CANONICAL_PROVENANCE.name
    completed = subprocess.run(
        [
            str(SCRIPT),
            "--policy",
            str(POLICY),
            "--source-integrity-anomalies",
            str(ANOMALIES),
            "--source-integrity-provenance",
            str(INTEGRITY_PROVENANCE),
            "--disposition-manifest",
            str(DISPOSITIONS),
            "--disposition-provenance",
            str(DISPOSITION_PROVENANCE),
            "--reference-manifest",
            str(REFERENCE_MANIFEST),
            "--reference-root",
            str(REPO_ROOT),
            "--blast-database",
            str(database),
            "--blastdbcmd",
            blastdbcmd,
            "--scope",
            "downstream_coi_only",
            "--release-id",
            RELEASE_ID,
            "--output-fasta",
            str(output_fasta),
            "--excluded-oids-output",
            str(excluded),
            "--provenance-output",
            str(provenance),
        ],
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert excluded.read_bytes() == EXCLUDED_OIDS.read_bytes()
    assert provenance.read_bytes() == CANONICAL_PROVENANCE.read_bytes()
    values = {
        (row["field"], row["artifact"]): row["value"]
        for row in read_tsv(provenance)
    }
    assert sha256_file(output_fasta) == values[("sha256", "canonical_fasta")]
