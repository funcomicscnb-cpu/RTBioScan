import hashlib
import os
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "state_compatibility_contract.pl"

LAST_EXTENSIONS = [".prj", ".bck", ".des", ".sds", ".ssp", ".suf", ".tis"]
BLAST_V5_EXTENSIONS = [".ndb", ".nhr", ".nin", ".njs", ".not", ".nsq", ".ntf", ".nto"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _build_fixture(
    tmp_path: Path,
    *,
    include_memtax: bool = True,
    include_lineage: bool = True,
    omit_last_extension: str | None = None,
    omit_blast_extension: str | None = None,
    omit_taxdb_extension: str | None = None,
) -> dict[str, Path]:
    root = tmp_path / "reference"
    taxonomy = tmp_path / "taxonomy"
    manifest = tmp_path / "reference_manifest.tsv"
    taxonomy_manifest = tmp_path / "taxonomy_release.tsv"
    state = tmp_path / "state"
    cache = tmp_path / "cache"

    artifacts: list[tuple[Path, str]] = []
    for extension in LAST_EXTENSIONS:
        if extension == omit_last_extension:
            continue
        path = root / f"db/filter{extension}"
        _write_file(path, f"last {extension}\n")
        artifacts.append((path, "LAST index"))
    for extension in BLAST_V5_EXTENSIONS:
        if extension == omit_blast_extension:
            continue
        path = root / f"db/coi{extension}"
        _write_file(path, f"blast {extension}\n")
        artifacts.append((path, "COI BLAST index"))
    for extension in [".btd", ".bti"]:
        if extension == omit_taxdb_extension:
            continue
        path = root / f"db/taxdb/taxdb{extension}"
        _write_file(path, f"taxdb {extension}\n")
        artifacts.append((path, "BLAST taxonomy database"))
    if include_memtax:
        path = root / "db/memtax.tsv"
        _write_file(path, "-1\t1\t1\t1\tspecies\n")
        artifacts.append((path, "COI taxonomy memory seed"))
    if include_lineage:
        path = root / "db/id2lineage.tsv"
        _write_file(path, "-1\tK__Metazoa;p__Arthropoda\n")
        artifacts.append((path, "synthetic lineage overrides"))

    for name in ["nodes.dmp", "names.dmp", "merged.dmp", "delnodes.dmp"]:
        _write_file(taxonomy / name, f"{name}\n")

    taxonomy_rows = [
        "kind\tartifact\tsha256\tbytes\trole",
        f"archive\ttaxdump.tar.gz\t{'0' * 64}\t0\tfixture archive",
    ]
    for name in ["nodes.dmp", "names.dmp", "merged.dmp", "delnodes.dmp"]:
        path = taxonomy / name
        taxonomy_rows.append(
            f"data\t{name}\t{_sha256(path)}\t{path.stat().st_size}\tfixture {name}"
        )
    taxonomy_manifest.write_text(
        "\n".join(taxonomy_rows) + "\n",
        encoding="utf-8",
    )

    rows = ["artifact\tsha256\trole"]
    for path, role in artifacts:
        rows.append(f"{path.relative_to(root)}\t{_sha256(path)}\t{role}")
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return {
        "root": root,
        "taxonomy": taxonomy,
        "taxonomy_manifest": taxonomy_manifest,
        "manifest": manifest,
        "state": state,
        "cache": cache,
    }


def _run_contract(
    fixture: dict[str, Path],
    *,
    state: Path | None = None,
    policy: str = "strict",
    verification_mode: str = "cached",
    memtax: str = "db/memtax.tsv",
    lineage: str = "db/id2lineage.tsv",
    scoring_version: str = "legacy-first-single-hit-v1",
    profile: str = "test",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--state-dir",
            str(state or fixture["state"]),
            "--reference-manifest",
            str(fixture["manifest"]),
            "--reference-root",
            str(fixture["root"]),
            "--taxonomy-data-dir",
            str(fixture["taxonomy"]),
            "--taxonomy-release-manifest",
            str(fixture["taxonomy_manifest"]),
            "--classifier-policy-version",
            "legacy-rank-string-v1",
            "--scoring-policy-version",
            scoring_version,
            "--targets",
            "COI",
            "--target-taxa",
            "Metazoa",
            "--blast-filter-db",
            "db/filter",
            "--blast-db-specs",
            "db/coi",
            "--blast-taxdb",
            "db/taxdb",
            "--nonncbi-memtax",
            memtax,
            "--nonncbi-id2lineage",
            lineage,
            "--policy",
            policy,
            "--lock-wait",
            "2",
            "--verification-cache-dir",
            str(fixture["cache"]),
            "--verification-mode",
            verification_mode,
            "--profile",
            profile,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _cache_file(fixture: dict[str, Path]) -> Path:
    matches = list(fixture["cache"].glob("uid-*/attestation-*.tsv"))
    assert len(matches) == 1
    return matches[0]


def _cache_verified_times(cache_file: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in cache_file.read_text(encoding="utf-8").splitlines():
        fields = line.split("\t")
        if fields[0] == "entry":
            result[fields[2]] = fields[13]
    return result


def _poison_current_cache_entry(cache_file: Path, target: Path) -> None:
    lines = cache_file.read_text(encoding="utf-8").splitlines()
    body_lines = lines[:-1]
    target_stat = target.stat()
    found = False
    for index, line in enumerate(body_lines):
        fields = line.split("\t")
        if fields[0] != "entry" or fields[2] != str(target):
            continue
        fields[4] = "0" * 64
        fields[5:13] = [
            str(target_stat.st_dev),
            str(target_stat.st_ino),
            f"{stat.S_IMODE(target_stat.st_mode):04o}",
            str(target_stat.st_uid),
            str(target_stat.st_gid),
            str(target_stat.st_size),
            str(int(target_stat.st_mtime)),
            str(int(target_stat.st_ctime)),
        ]
        body_lines[index] = "\t".join(fields)
        found = True
        break
    assert found
    body = "\n".join(body_lines) + "\n"
    cache_file.write_text(
        body + f"cache_sha256\t{hashlib.sha256(body.encode()).hexdigest()}\n",
        encoding="utf-8",
    )
    cache_file.chmod(0o600)


def test_new_state_and_unchanged_restart_reuse_private_cache(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    assert len(first.stdout.strip()) == 64
    assert (fixture["state"] / "state_compatibility_manifest.tsv").is_file()

    cache_file = _cache_file(fixture)
    assert stat.S_IMODE(cache_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    cache_mtime = cache_file.stat().st_mtime_ns

    second = _run_contract(fixture)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
    assert cache_file.stat().st_mtime_ns == cache_mtime


def test_taxonomy_artifact_drift_is_rejected_before_cache_update(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    cache_file = _cache_file(fixture)
    before = _cache_verified_times(cache_file)

    changed = fixture["taxonomy"] / "names.dmp"
    changed.write_text("changed names\n", encoding="utf-8")
    second = _run_contract(fixture, state=tmp_path / "state-v2")
    assert second.returncode != 0
    assert "taxonomy artifact size mismatch" in second.stderr
    assert _cache_verified_times(cache_file) == before


def test_only_changed_taxonomy_stat_signature_is_rehashed(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    cache_file = _cache_file(fixture)
    before = _cache_verified_times(cache_file)

    changed = fixture["taxonomy"] / "names.dmp"
    changed.chmod(0o440)
    second = _run_contract(fixture)
    assert second.returncode == 0, second.stderr
    after = _cache_verified_times(cache_file)

    changed_key = str(changed)
    assert after[changed_key] != before[changed_key]
    assert {
        path: stamp for path, stamp in after.items() if path != changed_key
    } == {
        path: stamp for path, stamp in before.items() if path != changed_key
    }


def test_changed_taxonomy_release_requires_a_matching_manifest(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr

    changed = fixture["taxonomy"] / "names.dmp"
    changed.write_text("changed names\n", encoding="utf-8")
    rows = fixture["taxonomy_manifest"].read_text(encoding="utf-8").splitlines()
    fixture["taxonomy_manifest"].write_text(
        "\n".join(
            f"data\tnames.dmp\t{_sha256(changed)}\t{changed.stat().st_size}\tfixture names.dmp"
            if row.startswith("data\tnames.dmp\t")
            else row
            for row in rows
        )
        + "\n",
        encoding="utf-8",
    )

    second = _run_contract(fixture, state=tmp_path / "state-v2")
    assert second.returncode == 0, second.stderr
    assert second.stdout != first.stdout


def test_same_size_reference_edit_with_preserved_mtime_is_rejected(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    target = fixture["root"] / "db/coi.nsq"
    original = target.read_bytes()
    previous_mtime = target.stat().st_mtime_ns
    target.write_bytes(b"X" * len(original))
    os.utime(target, ns=(previous_mtime, previous_mtime))

    result = _run_contract(fixture, state=tmp_path / "state-tampered")
    assert result.returncode != 0
    assert "reference artifact checksum mismatch" in result.stderr


def test_full_mode_rejects_tampering_even_with_existing_cache(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    target = fixture["root"] / "db/filter.suf"
    target.write_text("tampered last index\n", encoding="utf-8")

    result = _run_contract(
        fixture,
        state=tmp_path / "state-full",
        verification_mode="full",
    )
    assert result.returncode != 0
    assert "reference artifact checksum mismatch" in result.stderr


def test_legacy_state_requires_explicit_adoption(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["state"].mkdir()
    _write_file(fixture["state"] / "read_qscore_rolling.tsv", "read\tqscore\n")

    strict = _run_contract(fixture)
    assert strict.returncode != 0
    assert "legacy rolling state exists" in strict.stderr

    adopted = _run_contract(fixture, policy="adopt_legacy")
    assert adopted.returncode == 0, adopted.stderr
    contract = (fixture["state"] / "state_compatibility_manifest.tsv").read_text(
        encoding="utf-8"
    )
    assert "legacy_adopted\t1\n" in contract


def test_macos_metadata_does_not_make_fresh_state_legacy(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["state"].mkdir()
    _write_file(fixture["state"] / ".DS_Store", "finder metadata")
    _write_file(fixture["state"] / "._state", "appledouble metadata")

    result = _run_contract(fixture)
    assert result.returncode == 0, result.stderr
    contract = (fixture["state"] / "state_compatibility_manifest.tsv").read_text(
        encoding="utf-8"
    )
    assert "legacy_adopted\t0\n" in contract


def test_unknown_dotfile_remains_fail_closed_material(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["state"].mkdir()
    _write_file(fixture["state"] / ".unexpected-state", "unknown")

    result = _run_contract(fixture)
    assert result.returncode != 0
    assert "legacy rolling state exists" in result.stderr


def test_empty_optional_taxonomy_maps_are_accepted(tmp_path: Path) -> None:
    fixture = _build_fixture(
        tmp_path,
        include_memtax=False,
        include_lineage=False,
    )
    result = _run_contract(fixture, memtax="", lineage="")
    assert result.returncode == 0, result.stderr
    contract = (fixture["state"] / "state_compatibility_manifest.tsv").read_text(
        encoding="utf-8"
    )
    assert "nonncbi_memtax\t\n" in contract
    assert "nonncbi_id2lineage\t\n" in contract


def test_incomplete_last_component_group_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, omit_last_extension=".suf")
    result = _run_contract(fixture)
    assert result.returncode != 0
    assert "incomplete FAST/LAST filter database" in result.stderr
    assert ".suf" in result.stderr


def test_incomplete_blast_component_group_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, omit_blast_extension=".nin")
    result = _run_contract(fixture)
    assert result.returncode != 0
    assert "incomplete marker BLAST database" in result.stderr
    assert ".nin" in result.stderr


def test_incomplete_taxdb_component_group_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path, omit_taxdb_extension=".bti")
    result = _run_contract(fixture)
    assert result.returncode != 0
    assert "incomplete BLAST taxonomy database" in result.stderr
    assert ".bti" in result.stderr


def test_contract_mismatch_remains_fail_closed(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    mismatch = _run_contract(fixture, scoring_version="changed-v2")
    assert mismatch.returncode != 0
    assert "incompatible rolling state" in mismatch.stderr
    assert "scoring_policy_version" in mismatch.stderr


def test_contract_bearing_container_state_cannot_be_resumed_on_host(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture, profile="docker")
    assert first.returncode == 0, first.stderr

    resumed = _run_contract(fixture, profile="test")
    assert resumed.returncode != 0
    assert "produced with unsupported docker/singularity execution profile" in resumed.stderr
    assert "cannot be resumed or migrated" in resumed.stderr


def test_legacy_state_warning_forbids_adopting_suspected_container_state(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["state"].mkdir(parents=True)
    (fixture["state"] / "legacy.tsv").write_text("legacy\n", encoding="utf-8")

    result = _run_contract(fixture)
    assert result.returncode != 0
    assert "Never adopt state known or suspected" in result.stderr
    assert "NanoRTax image" in result.stderr


def test_container_profiles_are_explanatory_stubs_and_fail_early() -> None:
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    main_text = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")

    assert "hecrp/nanortax" not in config_text
    assert "docker.enabled = true" not in config_text
    assert "singularity.enabled = true" not in config_text
    assert "params.unsupported_execution_profile = 'docker'" in config_text
    assert "params.unsupported_execution_profile = 'singularity'" in config_text
    assert "params.unsupported_execution_profile = 'conda'" in config_text
    assert "conda.enabled = true" not in config_text
    assert "Unsupported RTBioScan execution profile" in main_text
    assert "inherited hecrp/nanortax container" in main_text
    assert "Direct Nextflow Conda resolution would bypass" in main_text


def test_matching_taxonomy_bytes_migrate_legacy_provenance_without_new_identity(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    contract_path = fixture["state"] / "state_compatibility_manifest.tsv"
    lines = contract_path.read_text(encoding="utf-8").splitlines()
    legacy_lines = []
    for line in lines:
        if line.startswith("taxonomy_release_manifest_"):
            continue
        if line.startswith("taxonomy_data_dir\t"):
            legacy_lines.append("taxonomy_data_dir\t/legacy/home/.taxonkit")
        elif line.startswith("taxonomy_mode\t"):
            legacy_lines.append("taxonomy_mode\timplicit_default")
        else:
            legacy_lines.append(line)
    contract_path.write_text("\n".join(legacy_lines) + "\n", encoding="utf-8")

    migrated = _run_contract(fixture)
    assert migrated.returncode == 0, migrated.stderr
    assert migrated.stdout == first.stdout
    contract = contract_path.read_text(encoding="utf-8")
    assert f"taxonomy_data_dir\t{fixture['taxonomy']}\n" in contract
    assert "taxonomy_mode\tpinned_explicit\n" in contract
    assert "taxonomy_migrated_from_data_dir\t/legacy/home/.taxonkit\n" in contract
    assert "taxonomy_release_manifest_sha256\t" in contract


def test_untrusted_cache_file_is_reverified_and_replaced_privately(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    cache_file = _cache_file(fixture)
    before = _cache_verified_times(cache_file)
    cache_file.chmod(0o666)

    second = _run_contract(fixture)
    assert second.returncode == 0, second.stderr
    after = _cache_verified_times(cache_file)
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    assert all(after[path] != before[path] for path in before)


def test_group_writable_reference_disables_cache_reuse(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    cache_file = _cache_file(fixture)
    target = fixture["root"] / "db/coi.nsq"
    target.chmod(0o664)
    _poison_current_cache_entry(cache_file, target)

    result = _run_contract(fixture, state=tmp_path / "state-group-writable")
    assert result.returncode == 0, result.stderr
    assert "group/world writable; using full verification" in result.stderr


def test_symlinked_cache_file_is_treated_as_a_miss(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    cache_file = _cache_file(fixture)
    before = _cache_verified_times(cache_file)
    real_cache_file = cache_file.with_name(f"{cache_file.name}.real")
    cache_file.rename(real_cache_file)
    cache_file.symlink_to(real_cache_file)

    second = _run_contract(fixture)
    assert second.returncode == 0, second.stderr
    assert not cache_file.is_symlink()
    after = _cache_verified_times(cache_file)
    assert all(after[path] != before[path] for path in before)


def test_symlinked_private_cache_directory_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    cache_file = _cache_file(fixture)
    target = fixture["root"] / "db/coi.nsq"
    _poison_current_cache_entry(cache_file, target)
    private_dir = cache_file.parent
    real_private_dir = private_dir.with_name(f"{private_dir.name}.real")
    private_dir.rename(real_private_dir)
    private_dir.symlink_to(real_private_dir, target_is_directory=True)

    result = _run_contract(fixture, state=tmp_path / "state-symlink-cache-dir")
    assert result.returncode == 0, result.stderr
    assert "not a private user-owned 0700 directory" in result.stderr
    assert private_dir.is_symlink()


def test_corrupt_cache_is_treated_as_absent(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    cache_file = _cache_file(fixture)
    before = _cache_verified_times(cache_file)
    cache_file.write_text("corrupt\n", encoding="utf-8")
    cache_file.chmod(0o600)

    second = _run_contract(fixture)
    assert second.returncode == 0, second.stderr
    after = _cache_verified_times(cache_file)
    assert all(after[path] != before[path] for path in before)


def test_concurrent_cache_seed_is_atomic(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_run_contract, fixture, state=tmp_path / f"state-{index}")
            for index in range(2)
        ]
    results = [future.result() for future in futures]
    assert all(result.returncode == 0 for result in results), [
        result.stderr for result in results
    ]
    cache_file = _cache_file(fixture)
    assert _cache_verified_times(cache_file)
    assert cache_file.read_text(encoding="utf-8").splitlines()[-1].startswith(
        "cache_sha256\t"
    )


def test_duplicate_manifest_artifact_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    lines = fixture["manifest"].read_text(encoding="utf-8").splitlines()
    fixture["manifest"].write_text(
        "\n".join([*lines, lines[1]]) + "\n",
        encoding="utf-8",
    )
    result = _run_contract(fixture)
    assert result.returncode != 0
    assert "duplicate artifact in reference manifest" in result.stderr


def test_main_wires_one_pinned_taxonomy_release_into_both_taxonomy_processes() -> None:
    main_text = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    release_text = (REPO_ROOT / "bin" / "prepare_public_release.sh").read_text(
        encoding="utf-8"
    )
    assert 'state_taxonomy_data_dir = "db/taxonomy/releases/ncbi-taxdump-2024-06-24"' in config_text
    assert "state_taxonomy_release_manifest" in config_text
    assert "'--taxonomy-data-dir', stateTaxonomyDataDirResolved" in main_text
    assert "'--taxonomy-release-manifest', stateTaxonomyReleaseManifestResolved" in main_text
    assert main_text.count('export TAXONKIT_DB="${stateTaxonomyDataDirResolved}"') == 2

    blast_start = main_text.index("process blast_OTU_pretax {")
    consensus_start = main_text.index("process consensus {")
    report_start = main_text.index("process _reporting_blast_pretax {")
    blast_block = main_text[blast_start:report_start]
    consensus_block = main_text[consensus_start:]
    assert 'export TAXONKIT_DB="${stateTaxonomyDataDirResolved}"' in blast_block
    assert 'export TAXONKIT_DB="${stateTaxonomyDataDirResolved}"' in consensus_block
    assert "conf/state_compatibility/reference_manifest_legacy_v1.tsv" in release_text
    assert "conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv" in release_text
    assert "db/taxonomy/releases/ncbi-taxdump-2024-06-24" in release_text
