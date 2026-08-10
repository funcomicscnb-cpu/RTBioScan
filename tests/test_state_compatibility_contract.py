import hashlib
import os
import signal
import socket
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "state_compatibility_contract.pl"

LAST_EXTENSIONS = [".prj", ".bck", ".des", ".sds", ".ssp", ".suf", ".tis"]
BLAST_V5_EXTENSIONS = [".ndb", ".nhr", ".nin", ".njs", ".not", ".nsq", ".ntf", ".nto"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_toolchain_fingerprint(
    path: Path,
    *,
    backend: str = "test",
    blast_version: str = "2.15.0",
) -> None:
    values = {
        "fingerprint_schema_version": "1",
        "runtime_backend": backend,
        "tool_blastn_version": blast_version,
    }
    canonical = "".join(f"{key}\t{values[key]}\n" for key in sorted(values))
    fingerprint_id = hashlib.sha256(canonical.encode()).hexdigest()
    path.write_text(
        canonical + f"fingerprint_id\t{fingerprint_id}\n",
        encoding="utf-8",
    )


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
    toolchain_fingerprint = tmp_path / "toolchain_fingerprint.tsv"

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
    _write_toolchain_fingerprint(toolchain_fingerprint)
    return {
        "root": root,
        "taxonomy": taxonomy,
        "taxonomy_manifest": taxonomy_manifest,
        "manifest": manifest,
        "state": state,
        "cache": cache,
        "toolchain_fingerprint": toolchain_fingerprint,
    }


def _contract_command(
    fixture: dict[str, Path],
    *,
    state: Path | None = None,
    policy: str = "strict",
    verification_mode: str = "cached",
    memtax: str = "db/memtax.tsv",
    lineage: str = "db/id2lineage.tsv",
    scoring_version: str = "legacy-first-single-hit-v1",
    profile: str = "test",
    contract_migration: str = "strict",
    lock_wait: int = 2,
    lock_stale_seconds: int = 300,
) -> list[str]:
    return [
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
        str(lock_wait),
        "--lock-stale-seconds",
        str(lock_stale_seconds),
        "--verification-cache-dir",
        str(fixture["cache"]),
        "--verification-mode",
        verification_mode,
        "--profile",
        profile,
        "--toolchain-fingerprint",
        str(fixture["toolchain_fingerprint"]),
        "--contract-migration",
        contract_migration,
    ]


def _run_contract(
    fixture: dict[str, Path],
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _contract_command(fixture, **kwargs),
        capture_output=True,
        text=True,
        check=False,
    )


def _write_rename_pause_module(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    module_dir = tmp_path / "perl-test-hook"
    module_dir.mkdir()
    (module_dir / "RTBioScanTestPauseRename.pm").write_text(
        r'''package RTBioScanTestPauseRename;
use strict;
use warnings;
use Time::HiRes qw(usleep);

sub pause_for_test {
    my $ready = $ENV{RTBIOSCAN_TEST_RENAME_READY};
    my $release = $ENV{RTBIOSCAN_TEST_RENAME_RELEASE};
    open(my $fh, '>', $ready) or die "cannot create rename READY marker: $!\n";
    print {$fh} "ready\n";
    close($fh) or die "cannot close rename READY marker: $!\n";
    while (!-e $release) {
        usleep(10_000);
    }
}

sub record_attempt_for_test {
    my ($env_name, $result, $error_number) = @_;
    my $path = $ENV{$env_name} // '';
    return if $path eq '';
    open(my $fh, '>', $path) or die "cannot create attempt marker: $!\n";
    print {$fh} "result\t", ($result ? 1 : 0), "\n";
    print {$fh} "errno\t$error_number\n";
    close($fh) or die "cannot close attempt marker: $!\n";
}

BEGIN {
    no warnings 'redefine';
    *CORE::GLOBAL::rename = sub {
        my ($source, $destination) = @_;
        my $target = $ENV{RTBIOSCAN_TEST_RENAME_TARGET} // '';
        my $source_target = $ENV{RTBIOSCAN_TEST_RENAME_SOURCE} // '';
        my $target_is_prefix =
            ($ENV{RTBIOSCAN_TEST_RENAME_TARGET_IS_PREFIX} // '') eq '1';
        my $mode = $ENV{RTBIOSCAN_TEST_RENAME_MODE} // '';
        my $source_matches =
            $source_target eq '' || $source eq $source_target;
        my $target_matches = $target_is_prefix
            ? index($destination, $target) == 0
            : $destination eq $target;
        if (!$source_matches || !$target_matches) {
            return CORE::rename($source, $destination);
        }
        RTBioScanTestPauseRename::pause_for_test() if $mode eq 'before';
        my $result = CORE::rename($source, $destination);
        my $error_number = $result ? 0 : 0 + $!;
        RTBioScanTestPauseRename::record_attempt_for_test(
            'RTBIOSCAN_TEST_RENAME_ATTEMPTED', $result, $error_number
        );
        RTBioScanTestPauseRename::pause_for_test()
            if $result && $mode eq 'after';
        $! = $error_number;
        return $result;
    };

    *CORE::GLOBAL::link = sub {
        my ($source, $destination) = @_;
        my $target = $ENV{RTBIOSCAN_TEST_LINK_TARGET} // '';
        my $mode = $ENV{RTBIOSCAN_TEST_LINK_MODE} // '';
        if ($target eq '' || $destination ne $target) {
            return CORE::link($source, $destination);
        }
        RTBioScanTestPauseRename::pause_for_test() if $mode eq 'before';
        my $result = CORE::link($source, $destination);
        my $error_number = $result ? 0 : 0 + $!;
        RTBioScanTestPauseRename::record_attempt_for_test(
            'RTBIOSCAN_TEST_LINK_ATTEMPTED', $result, $error_number
        );
        RTBioScanTestPauseRename::pause_for_test()
            if $result && $mode eq 'after';
        $! = $error_number;
        return $result;
    };
}

1;
''',
        encoding="utf-8",
    )
    return module_dir


def _start_paused_contract(
    fixture: dict[str, Path],
    *,
    state: Path,
    rename_target: Path | None = None,
    rename_mode: str = "",
    hook_root: Path,
    rename_source: Path | None = None,
    rename_target_is_prefix: bool = False,
    rename_attempted: Path | None = None,
    link_target: Path | None = None,
    link_mode: str = "",
    link_attempted: Path | None = None,
    **kwargs: object,
) -> tuple[subprocess.Popen[str], Path, Path]:
    module_dir = _write_rename_pause_module(hook_root)
    ready = hook_root / "rename.ready"
    release = hook_root / "rename.release"
    env = dict(os.environ)
    env["PERL5LIB"] = os.pathsep.join(
        part for part in [str(module_dir), env.get("PERL5LIB", "")] if part
    )
    env["PERL5OPT"] = " ".join(
        part
        for part in ["-MRTBioScanTestPauseRename", env.get("PERL5OPT", "")]
        if part
    )
    env["RTBIOSCAN_TEST_RENAME_TARGET"] = (
        "" if rename_target is None else str(rename_target)
    )
    env["RTBIOSCAN_TEST_RENAME_SOURCE"] = (
        "" if rename_source is None else str(rename_source)
    )
    env["RTBIOSCAN_TEST_RENAME_TARGET_IS_PREFIX"] = (
        "1" if rename_target_is_prefix else "0"
    )
    env["RTBIOSCAN_TEST_RENAME_ATTEMPTED"] = (
        "" if rename_attempted is None else str(rename_attempted)
    )
    env["RTBIOSCAN_TEST_LINK_TARGET"] = (
        "" if link_target is None else str(link_target)
    )
    env["RTBIOSCAN_TEST_LINK_MODE"] = link_mode
    env["RTBIOSCAN_TEST_LINK_ATTEMPTED"] = (
        "" if link_attempted is None else str(link_attempted)
    )
    env["RTBIOSCAN_TEST_RENAME_MODE"] = rename_mode
    env["RTBIOSCAN_TEST_RENAME_READY"] = str(ready)
    env["RTBIOSCAN_TEST_RENAME_RELEASE"] = str(release)
    process = subprocess.Popen(
        _contract_command(fixture, state=state, **kwargs),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return process, ready, release


def _wait_for_ready(process: subprocess.Popen[str], ready: Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready.is_file():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"paused contract exited early ({process.returncode})\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
        time.sleep(0.01)
    process.kill()
    stdout, stderr = process.communicate(timeout=5)
    raise AssertionError(
        f"timed out waiting for rename hook\nstdout:\n{stdout}\nstderr:\n{stderr}"
    )


def _lock_owner_files(lock_dir: Path) -> list[Path]:
    return sorted(lock_dir.glob(".owner-*.tsv"))


def _reclaim_quarantines(lock_dir: Path) -> list[Path]:
    prefix = f"{lock_dir.name}.reclaim-"
    quarantines = []
    for path in lock_dir.parent.glob(f"{prefix}*"):
        suffix = path.name[len(prefix) :]
        if (
            path.is_dir()
            and len(suffix) == 64
            and all(char in "0123456789abcdef" for char in suffix)
        ):
            quarantines.append(path)
    return sorted(quarantines)


def _linux_process_start_ticks(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.is_file():
        return None
    tail = stat_path.read_text(encoding="utf-8").rsplit(") ", 1)[-1].split()
    return tail[19] if len(tail) > 19 and tail[19].isdigit() else None


def _write_manual_lock_owner(
    lock_dir: Path,
    *,
    pid: int,
    host: str,
    process_start: str,
    started_epoch: int,
    kind: str = "state compatibility lock",
    token: str = "a" * 64,
) -> Path:
    lock_dir.mkdir(parents=True)
    owner = lock_dir / f".owner-{token}.tsv"
    owner.write_text(
        "schema\t1\n"
        f"token\t{token}\n"
        f"pid\t{pid}\n"
        f"host\t{host}\n"
        f"process_start\t{process_start}\n"
        f"started_epoch\t{started_epoch}\n"
        f"kind\t{kind}\n",
        encoding="utf-8",
    )
    return owner


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


def _contract_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("\t", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    )


def _downgrade_contract_to_schema_v1(path: Path) -> str:
    values = _contract_values(path)
    identity_keys = [
        "schema_version",
        "reference_manifest_sha256",
        "taxonomy_nodes_sha256",
        "taxonomy_names_sha256",
        "taxonomy_merged_sha256",
        "taxonomy_delnodes_sha256",
        "classifier_policy_version",
        "scoring_policy_version",
        "targets",
        "target_taxa",
        "blast_filter_db",
        "blast_db_specs",
        "blast_taxdb",
        "nonncbi_memtax",
        "nonncbi_id2lineage",
    ]
    legacy = {key: values[key] for key in identity_keys}
    legacy["schema_version"] = "1"
    canonical = "".join(f"{key}\t{legacy[key]}\n" for key in sorted(legacy))
    legacy_id = hashlib.sha256(canonical.encode()).hexdigest()
    metadata_keys = [
        "reference_manifest_path",
        "taxonomy_release_manifest_path",
        "taxonomy_release_manifest_sha256",
        "taxonomy_data_dir",
        "taxonomy_mode",
        "execution_profile",
        "legacy_adopted",
    ]
    output = {**legacy, "contract_id": legacy_id}
    output.update({key: values[key] for key in metadata_keys if key in values})
    path.write_text(
        "".join(f"{key}\t{output[key]}\n" for key in sorted(output)),
        encoding="utf-8",
    )
    return legacy_id


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


def test_runtime_toolchain_change_is_incompatible_with_existing_state(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr

    _write_toolchain_fingerprint(
        fixture["toolchain_fingerprint"],
        blast_version="2.16.0",
    )
    changed = _run_contract(fixture)
    assert changed.returncode != 0
    assert "toolchain_fingerprint_id" in changed.stderr


def test_corrupt_runtime_toolchain_fingerprint_is_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["toolchain_fingerprint"].write_text(
        "fingerprint_schema_version\t1\n"
        f"fingerprint_id\t{'0' * 64}\n",
        encoding="utf-8",
    )
    result = _run_contract(fixture)
    assert result.returncode != 0
    assert "runtime toolchain fingerprint checksum mismatch" in result.stderr


def test_schema_v1_state_requires_explicit_attested_migration(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    contract_path = fixture["state"] / "state_compatibility_manifest.tsv"
    legacy_id = _downgrade_contract_to_schema_v1(contract_path)

    strict = _run_contract(fixture)
    assert strict.returncode != 0
    assert "requires explicit toolchain migration" in strict.stderr
    assert "--state_contract_migration attest_v1" in strict.stderr

    migrated = _run_contract(
        fixture,
        contract_migration="attest_v1",
    )
    assert migrated.returncode == 0, migrated.stderr
    assert "cannot be cryptographically proven" in migrated.stderr
    values = _contract_values(contract_path)
    assert values["schema_version"] == "2"
    assert values["migrated_from_contract_id"] == legacy_id
    assert values["toolchain_migration_attested"] == "1"
    assert (
        values["toolchain_migration_limitation"]
        == "historical_schema_v1_runtime_not_cryptographically_provable"
    )


def test_schema_v1_migration_rejects_changed_legacy_identity(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    first = _run_contract(fixture)
    assert first.returncode == 0, first.stderr
    contract_path = fixture["state"] / "state_compatibility_manifest.tsv"
    _downgrade_contract_to_schema_v1(contract_path)

    changed = _run_contract(
        fixture,
        scoring_version="changed-scoring-v2",
        contract_migration="attest_v1",
    )
    assert changed.returncode != 0
    assert "schema-v1 rolling state is incompatible" in changed.stderr
    assert "scoring_policy_version" in changed.stderr


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


def test_state_lock_recovers_after_hard_kill(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    seeded = _run_contract(fixture, state=tmp_path / "seed-state")
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "state-hard-kill"
    contract = state / "state_compatibility_manifest.tsv"
    process, ready, _ = _start_paused_contract(
        fixture,
        state=state,
        rename_target=contract,
        rename_mode="after",
        hook_root=tmp_path / "state-hook",
    )
    try:
        _wait_for_ready(process, ready)
        lock_dir = state / ".state_compatibility.lockdir"
        owners = _lock_owner_files(lock_dir)
        assert contract.is_file()
        assert len(owners) == 1
        owner_text = owners[0].read_text(encoding="utf-8")
        assert f"pid\t{process.pid}\n" in owner_text
        assert "process_start\t" in owner_text
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert lock_dir.is_dir()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    recovered = _run_contract(fixture, state=state)
    assert recovered.returncode == 0, recovered.stderr
    assert "reclaiming stale state compatibility lock (dead pid=" in recovered.stderr
    assert recovered.stdout.strip() == _contract_values(contract)["contract_id"]
    assert not lock_dir.exists()
    quarantines = _reclaim_quarantines(lock_dir)
    assert len(quarantines) == 1
    assert (quarantines[0] / ".reclaim-generation.tsv").is_file()


def test_attestation_cache_lock_recovers_after_hard_kill(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    seeded = _run_contract(fixture, state=tmp_path / "seed-state")
    assert seeded.returncode == 0, seeded.stderr
    cache_file = _cache_file(fixture)
    cache_file.unlink()

    state = tmp_path / "cache-hard-kill-state"
    process, ready, _ = _start_paused_contract(
        fixture,
        state=state,
        rename_target=cache_file,
        rename_mode="before",
        hook_root=tmp_path / "cache-hook",
    )
    cache_lock = Path(f"{cache_file}.lockdir")
    try:
        _wait_for_ready(process, ready)
        owners = _lock_owner_files(cache_lock)
        assert not cache_file.exists()
        assert len(owners) == 1
        assert f"pid\t{process.pid}\n" in owners[0].read_text(encoding="utf-8")
        assert len(list(cache_lock.glob(".attestation-*.tmp"))) == 1
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert cache_lock.is_dir()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    recovered = _run_contract(fixture, state=state)
    assert recovered.returncode == 0, recovered.stderr
    assert "reclaiming stale attestation cache lock (dead pid=" in recovered.stderr
    assert stat.S_IMODE(cache_file.stat().st_mode) == 0o600
    assert _cache_verified_times(cache_file)
    assert cache_file.read_text(encoding="utf-8").splitlines()[-1].startswith(
        "cache_sha256\t"
    )
    assert not cache_lock.exists()
    quarantines = _reclaim_quarantines(cache_lock)
    assert len(quarantines) == 1
    assert (quarantines[0] / ".reclaim-generation.tsv").is_file()


def test_verifiably_live_same_host_state_lock_is_never_stolen_by_age(
    tmp_path: Path,
) -> None:
    if not Path(f"/proc/{os.getpid()}/stat").is_file() or not Path(
        "/proc/sys/kernel/random/boot_id"
    ).is_file():
        pytest.skip("verifiable process-start identity is unavailable on this host")
    fixture = _build_fixture(tmp_path)
    seeded = _run_contract(fixture, state=tmp_path / "seed-state")
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "state-live-owner"
    contract = state / "state_compatibility_manifest.tsv"
    process, ready, release = _start_paused_contract(
        fixture,
        state=state,
        rename_target=contract,
        rename_mode="after",
        hook_root=tmp_path / "live-owner-hook",
    )
    lock_dir = state / ".state_compatibility.lockdir"
    try:
        _wait_for_ready(process, ready)
        owner = _lock_owner_files(lock_dir)[0]
        owner_before = owner.read_bytes()
        os.utime(owner, (1, 1))
        os.utime(lock_dir, (1, 1))

        contender = _run_contract(
            fixture,
            state=state,
            lock_wait=1,
            lock_stale_seconds=1,
        )
        assert contender.returncode != 0
        assert "timed out waiting for lock" in contender.stderr
        assert owner.read_bytes() == owner_before
        assert process.poll() is None

        release.write_text("release\n", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 0, stderr
        assert len(stdout.strip()) == 64
        assert not lock_dir.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_reclaimed_owner_cannot_publish_or_remove_replacement_lock(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    seeded = _run_contract(fixture, state=tmp_path / "seed-state")
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "state-fenced-owner"
    contract = state / "state_compatibility_manifest.tsv"
    process, ready, release = _start_paused_contract(
        fixture,
        state=state,
        rename_target=contract,
        rename_mode="before",
        hook_root=tmp_path / "fenced-owner-hook",
    )
    lock_dir = state / ".state_compatibility.lockdir"
    replacement_process = None
    replacement_release = None
    try:
        _wait_for_ready(process, ready)
        owner = _lock_owner_files(lock_dir)[0]
        owner_lines = owner.read_text(encoding="utf-8").splitlines()
        owner.write_text(
            "\n".join(
                "host\tforeign-host.invalid"
                if line.startswith("host\t")
                else "started_epoch\t1"
                if line.startswith("started_epoch\t")
                else line
                for line in owner_lines
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(owner, (1, 1))
        os.utime(lock_dir, (1, 1))

        replacement_process, replacement_ready, replacement_release = (
            _start_paused_contract(
                fixture,
                state=state,
                rename_target=contract,
                rename_mode="after",
                hook_root=tmp_path / "replacement-owner-hook",
                lock_wait=2,
                lock_stale_seconds=1,
            )
        )
        _wait_for_ready(replacement_process, replacement_ready)
        replacement_owner = _lock_owner_files(lock_dir)[0]
        replacement_owner_bytes = replacement_owner.read_bytes()
        assert f"pid\t{replacement_process.pid}\n" in replacement_owner_bytes.decode(
            "utf-8"
        )
        replacement_contract = contract.read_bytes()
        assert replacement_process.poll() is None

        release.write_text("release\n", encoding="utf-8")
        _, stale_stderr = process.communicate(timeout=10)
        assert process.returncode != 0
        assert (
            "cannot install state contract" in stale_stderr
            or "ownership of state compatibility lock was lost" in stale_stderr
        )
        assert contract.read_bytes() == replacement_contract
        assert replacement_process.poll() is None
        assert lock_dir.is_dir()
        assert replacement_owner.is_file()
        assert replacement_owner.read_bytes() == replacement_owner_bytes

        replacement_release.write_text("release\n", encoding="utf-8")
        replacement_stdout, replacement_stderr = replacement_process.communicate(
            timeout=10
        )
        assert replacement_process.returncode == 0, replacement_stderr
        assert "foreign owner host=foreign-host.invalid" in replacement_stderr
        assert len(replacement_stdout.strip()) == 64
        assert contract.read_bytes() == replacement_contract
        assert not lock_dir.exists()
    finally:
        if process.poll() is None:
            release.write_text("release\n", encoding="utf-8")
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if replacement_process is not None and replacement_process.poll() is None:
            assert replacement_release is not None
            replacement_release.write_text("release\n", encoding="utf-8")
            try:
                replacement_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                replacement_process.kill()
                replacement_process.wait(timeout=5)


def test_concurrent_malformed_lock_reclaimers_preserve_replacement_lock(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    seeded = _run_contract(fixture, state=tmp_path / "seed-state")
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "state-concurrent-malformed-reclaim"
    lock_dir = state / ".state_compatibility.lockdir"
    lock_dir.mkdir(parents=True)
    malformed_owner = lock_dir / ".owner-malformed.tsv"
    malformed_owner.write_text("malformed\n", encoding="utf-8")
    os.utime(malformed_owner, (1, 1))
    os.utime(lock_dir, (1, 1))
    contract = state / "state_compatibility_manifest.tsv"
    stale_attempted = tmp_path / "stale-malformed-hook" / "rename.attempted"

    stale_process, stale_ready, stale_release = _start_paused_contract(
        fixture,
        state=state,
        rename_source=lock_dir,
        rename_target=Path(f"{lock_dir}.reclaim-"),
        rename_target_is_prefix=True,
        rename_mode="before",
        rename_attempted=stale_attempted,
        hook_root=tmp_path / "stale-malformed-hook",
        lock_wait=3,
        lock_stale_seconds=60,
    )
    replacement_process = None
    replacement_release = None
    try:
        _wait_for_ready(stale_process, stale_ready)
        generation = lock_dir / ".reclaim-generation.tsv"
        assert generation.is_file()
        original_inode = lock_dir.stat().st_ino

        replacement_process, replacement_ready, replacement_release = (
            _start_paused_contract(
                fixture,
                state=state,
                rename_target=contract,
                rename_mode="after",
                hook_root=tmp_path / "malformed-replacement-hook",
                lock_wait=3,
                lock_stale_seconds=60,
            )
        )
        _wait_for_ready(replacement_process, replacement_ready)
        quarantines = _reclaim_quarantines(lock_dir)
        assert len(quarantines) == 1
        assert quarantines[0].stat().st_ino == original_inode
        replacement_inode = lock_dir.stat().st_ino
        replacement_owner = _lock_owner_files(lock_dir)[0]
        replacement_owner_bytes = replacement_owner.read_bytes()
        replacement_contract = contract.read_bytes()

        stale_release.write_text("release\n", encoding="utf-8")
        _wait_for_ready(stale_process, stale_attempted)
        assert "result\t0\n" in stale_attempted.read_text(encoding="utf-8")
        assert stale_process.poll() is None
        assert replacement_process.poll() is None
        assert lock_dir.stat().st_ino == replacement_inode
        assert replacement_owner.read_bytes() == replacement_owner_bytes
        assert contract.read_bytes() == replacement_contract

        replacement_release.write_text("release\n", encoding="utf-8")
        replacement_stdout, replacement_stderr = replacement_process.communicate(
            timeout=10
        )
        assert replacement_process.returncode == 0, replacement_stderr
        stale_stdout, stale_stderr = stale_process.communicate(timeout=10)
        assert stale_process.returncode == 0, stale_stderr
        assert stale_stdout.strip() == replacement_stdout.strip()
        assert len(stale_stdout.strip()) == 64
        assert contract.read_bytes() == replacement_contract
        assert not lock_dir.exists()
        assert _reclaim_quarantines(lock_dir) == quarantines
    finally:
        if stale_process.poll() is None:
            stale_release.write_text("release\n", encoding="utf-8")
            try:
                stale_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stale_process.kill()
                stale_process.wait(timeout=5)
        if replacement_process is not None and replacement_process.poll() is None:
            assert replacement_release is not None
            replacement_release.write_text("release\n", encoding="utf-8")
            try:
                replacement_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                replacement_process.kill()
                replacement_process.wait(timeout=5)


def test_delayed_generation_link_cannot_poison_replacement_lock(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    seeded = _run_contract(fixture, state=tmp_path / "seed-state")
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "state-delayed-generation-link"
    lock_dir = state / ".state_compatibility.lockdir"
    lock_dir.mkdir(parents=True)
    os.utime(lock_dir, (1, 1))
    contract = state / "state_compatibility_manifest.tsv"
    generation = lock_dir / ".reclaim-generation.tsv"
    link_attempted = tmp_path / "delayed-link-hook" / "link.attempted"
    stale_process, stale_ready, stale_release = _start_paused_contract(
        fixture,
        state=state,
        link_target=generation,
        link_mode="before",
        link_attempted=link_attempted,
        hook_root=tmp_path / "delayed-link-hook",
        lock_wait=3,
        lock_stale_seconds=60,
    )
    replacement_process = None
    replacement_release = None
    try:
        _wait_for_ready(stale_process, stale_ready)
        assert not generation.exists()
        assert len(list(lock_dir.glob(".reclaim-generation-*.tmp"))) == 1
        for child in lock_dir.iterdir():
            os.utime(child, (1, 1))
        os.utime(lock_dir, (1, 1))
        original_inode = lock_dir.stat().st_ino

        replacement_process, replacement_ready, replacement_release = (
            _start_paused_contract(
                fixture,
                state=state,
                rename_target=contract,
                rename_mode="after",
                hook_root=tmp_path / "delayed-link-replacement-hook",
                lock_wait=3,
                lock_stale_seconds=60,
            )
        )
        _wait_for_ready(replacement_process, replacement_ready)
        quarantines = _reclaim_quarantines(lock_dir)
        assert len(quarantines) == 1
        assert quarantines[0].stat().st_ino == original_inode
        replacement_inode = lock_dir.stat().st_ino
        replacement_owner = _lock_owner_files(lock_dir)[0]
        replacement_owner_bytes = replacement_owner.read_bytes()
        replacement_contract = contract.read_bytes()
        assert not generation.exists()

        stale_release.write_text("release\n", encoding="utf-8")
        _wait_for_ready(stale_process, link_attempted)
        assert "result\t0\n" in link_attempted.read_text(encoding="utf-8")
        assert stale_process.poll() is None
        assert replacement_process.poll() is None
        assert lock_dir.stat().st_ino == replacement_inode
        assert replacement_owner.read_bytes() == replacement_owner_bytes
        assert contract.read_bytes() == replacement_contract
        assert not generation.exists()

        replacement_release.write_text("release\n", encoding="utf-8")
        replacement_stdout, replacement_stderr = replacement_process.communicate(
            timeout=10
        )
        assert replacement_process.returncode == 0, replacement_stderr
        stale_stdout, stale_stderr = stale_process.communicate(timeout=10)
        assert stale_process.returncode == 0, stale_stderr
        assert stale_stdout.strip() == replacement_stdout.strip()
        assert len(stale_stdout.strip()) == 64
        assert contract.read_bytes() == replacement_contract
        assert not lock_dir.exists()
        assert _reclaim_quarantines(lock_dir) == quarantines
    finally:
        if stale_process.poll() is None:
            stale_release.write_text("release\n", encoding="utf-8")
            try:
                stale_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stale_process.kill()
                stale_process.wait(timeout=5)
        if replacement_process is not None and replacement_process.poll() is None:
            assert replacement_release is not None
            replacement_release.write_text("release\n", encoding="utf-8")
            try:
                replacement_process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                replacement_process.kill()
                replacement_process.wait(timeout=5)


def test_reclaim_generation_resumes_immediately_after_hard_kill(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    seeded = _run_contract(fixture, state=tmp_path / "seed-state")
    assert seeded.returncode == 0, seeded.stderr

    state = tmp_path / "state-killed-reclaimer"
    lock_dir = state / ".state_compatibility.lockdir"
    lock_dir.mkdir(parents=True)
    os.utime(lock_dir, (1, 1))
    contract = state / "state_compatibility_manifest.tsv"
    process, ready, _ = _start_paused_contract(
        fixture,
        state=state,
        rename_source=lock_dir,
        rename_target=Path(f"{lock_dir}.reclaim-"),
        rename_target_is_prefix=True,
        rename_mode="before",
        hook_root=tmp_path / "killed-reclaimer-hook",
        lock_wait=2,
        lock_stale_seconds=1,
    )
    try:
        _wait_for_ready(process, ready)
        generation = lock_dir / ".reclaim-generation.tsv"
        assert generation.is_file()
        generation_bytes = generation.read_bytes()
        assert b"lock_dev\t" not in generation_bytes
        assert b"lock_ino\t" not in generation_bytes
        process.kill()
        assert process.wait(timeout=5) == -signal.SIGKILL
        assert lock_dir.is_dir()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    recovered = _run_contract(
        fixture,
        state=state,
        lock_wait=2,
        lock_stale_seconds=300,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert "recorded reclaim generation=" in recovered.stderr
    assert contract.is_file()
    assert not lock_dir.exists()
    quarantines = _reclaim_quarantines(lock_dir)
    assert len(quarantines) == 1
    assert (quarantines[0] / ".reclaim-generation.tsv").read_bytes() == (
        generation_bytes
    )


def test_reclaim_generation_flushes_before_sync_and_publication() -> None:
    script_text = SCRIPT.read_text(encoding="utf-8")
    start = script_text.index("sub install_reclaim_generation {")
    end = script_text.index("sub same_reclaim_generation {", start)
    install_block = script_text[start:end]
    assert install_block.index("$fh->flush()") < install_block.index("$fh->sync()")
    assert install_block.index("$fh->sync()") < install_block.index(
        "link($tmp, $claim_path)"
    )


def test_reused_same_host_pid_is_reclaimed_immediately(tmp_path: Path) -> None:
    if not Path(f"/proc/{os.getpid()}/stat").is_file():
        pytest.skip("portable PID start identity is unavailable on this host")
    fixture = _build_fixture(tmp_path)
    state = fixture["state"]
    lock_dir = state / ".state_compatibility.lockdir"
    local_host = socket.gethostname().strip().lower().rstrip(".") or "unknown"
    _write_manual_lock_owner(
        lock_dir,
        pid=os.getpid(),
        host=local_host,
        process_start="proc:0",
        started_epoch=int(time.time()),
    )

    result = _run_contract(fixture, lock_stale_seconds=300)
    assert result.returncode == 0, result.stderr
    assert "reclaiming stale state compatibility lock (reused pid=" in result.stderr
    assert not lock_dir.exists()


def test_legacy_process_start_without_boot_id_uses_lease(
    tmp_path: Path,
) -> None:
    ticks = _linux_process_start_ticks(os.getpid())
    if ticks is None:
        pytest.skip("Linux process-start ticks are unavailable on this host")
    fixture = _build_fixture(tmp_path)
    lock_dir = fixture["state"] / ".state_compatibility.lockdir"
    local_host = socket.gethostname().strip().lower().rstrip(".") or "unknown"
    owner = _write_manual_lock_owner(
        lock_dir,
        pid=os.getpid(),
        host=local_host,
        process_start=f"proc:{ticks}",
        started_epoch=1,
    )
    os.utime(owner, (1, 1))
    os.utime(lock_dir, (1, 1))

    result = _run_contract(fixture, lock_stale_seconds=1)
    assert result.returncode == 0, result.stderr
    assert "unverifiable process identity" in result.stderr
    assert "reused pid=" not in result.stderr


def test_unverifiable_same_host_live_pid_is_reclaimed_after_lease(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    lock_dir = fixture["state"] / ".state_compatibility.lockdir"
    local_host = socket.gethostname().strip().lower().rstrip(".") or "unknown"
    owner = _write_manual_lock_owner(
        lock_dir,
        pid=os.getpid(),
        host=local_host,
        process_start="unavailable",
        started_epoch=1,
    )
    os.utime(owner, (1, 1))
    os.utime(lock_dir, (1, 1))

    result = _run_contract(fixture, lock_stale_seconds=1)
    assert result.returncode == 0, result.stderr
    assert "same-host live pid=" in result.stderr
    assert "unverifiable process identity" in result.stderr
    assert not lock_dir.exists()


@pytest.mark.parametrize("stale_seconds,backdate", [(60, False), (0, True)])
def test_unverifiable_same_host_live_pid_is_protected_by_lease(
    tmp_path: Path,
    stale_seconds: int,
    backdate: bool,
) -> None:
    fixture = _build_fixture(tmp_path)
    lock_dir = fixture["state"] / ".state_compatibility.lockdir"
    local_host = socket.gethostname().strip().lower().rstrip(".") or "unknown"
    owner = _write_manual_lock_owner(
        lock_dir,
        pid=os.getpid(),
        host=local_host,
        process_start="unavailable",
        started_epoch=1 if backdate else int(time.time()),
    )
    if backdate:
        os.utime(owner, (1, 1))
        os.utime(lock_dir, (1, 1))

    result = _run_contract(
        fixture,
        lock_wait=1,
        lock_stale_seconds=stale_seconds,
    )
    assert result.returncode != 0
    assert "timed out waiting for lock" in result.stderr
    assert lock_dir.is_dir()


def test_expired_foreign_and_malformed_state_locks_are_reclaimed(
    tmp_path: Path,
) -> None:
    for lock_type in ["foreign", "malformed"]:
        fixture = _build_fixture(tmp_path / lock_type)
        lock_dir = fixture["state"] / ".state_compatibility.lockdir"
        if lock_type == "foreign":
            owner = _write_manual_lock_owner(
                lock_dir,
                pid=1,
                host="foreign-host.invalid",
                process_start="unavailable",
                started_epoch=1,
            )
            os.utime(owner, (1, 1))
        else:
            lock_dir.mkdir(parents=True)
            (lock_dir / ".owner-malformed.tsv").write_text(
                "malformed\n", encoding="utf-8"
            )
            os.utime(lock_dir / ".owner-malformed.tsv", (1, 1))
        os.utime(lock_dir, (1, 1))

        result = _run_contract(fixture, lock_stale_seconds=1)
        assert result.returncode == 0, result.stderr
        assert "reclaiming stale state compatibility lock" in result.stderr
        assert not lock_dir.exists()


def test_abandoned_reclaim_quarantine_is_inert_without_legacy_adoption(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["state"].mkdir(parents=True)
    quarantine = (
        fixture["state"]
        / f".state_compatibility.lockdir.reclaim-{'b' * 64}"
    )
    quarantine.mkdir()
    (quarantine / ".contract-orphan.tmp").write_text(
        "incomplete\n", encoding="utf-8"
    )

    result = _run_contract(fixture)
    assert result.returncode == 0, result.stderr
    assert quarantine.is_dir()
    values = _contract_values(
        fixture["state"] / "state_compatibility_manifest.tsv"
    )
    assert values["legacy_adopted"] == "0"


def test_fresh_foreign_and_malformed_state_locks_fail_closed(
    tmp_path: Path,
) -> None:
    for lock_type in ["foreign", "malformed"]:
        fixture = _build_fixture(tmp_path / lock_type)
        lock_dir = fixture["state"] / ".state_compatibility.lockdir"
        if lock_type == "foreign":
            _write_manual_lock_owner(
                lock_dir,
                pid=1,
                host="foreign-host.invalid",
                process_start="unavailable",
                started_epoch=int(time.time()),
            )
        else:
            lock_dir.mkdir(parents=True)

        result = _run_contract(
            fixture,
            lock_wait=1,
            lock_stale_seconds=60,
        )
        assert result.returncode != 0
        assert "timed out waiting for lock" in result.stderr
        assert lock_dir.is_dir()


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
    assert "conf/runtime_compatibility/toolchain_legacy_v1.tsv" in release_text
    assert "db/taxonomy/releases/ncbi-taxdump-2024-06-24" in release_text


def test_main_wires_schema_v2_runtime_fingerprint_before_state_contract() -> None:
    main_text = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")
    config_text = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    fingerprint_call = main_text.index("bin/runtime_toolchain_fingerprint.pl")
    contract_call = main_text.index("bin/state_compatibility_contract.pl")
    assert fingerprint_call < contract_call
    assert "'--toolchain-fingerprint', stateToolchainFingerprintFile.toString()" in main_text
    assert "'--contract-migration', stateContractMigration" in main_text
    assert "'--lock-stale-seconds', stateLockStaleSeconds" in main_text
    assert "'--dorado-bin', doradoBin" in main_text
    assert "'--dorado-device', params.dorado_device.toString()" in main_text
    assert '"fast=${doradoFastBasecallerArgs}"' in main_text
    assert "stateRuntimeBackend = System.getenv('CONDA_PREFIX')" in main_text
    assert "conda-lock-osx-64.yml" in main_text
    assert "conda-lock-linux-64.yml" in main_text
    assert (
        'state_toolchain_policy_manifest = '
        '"conf/runtime_compatibility/toolchain_legacy_v1.tsv"'
    ) in config_text
    assert 'state_contract_migration = "strict"' in config_text
    assert 'state_lock_stale_seconds = 300' in config_text
