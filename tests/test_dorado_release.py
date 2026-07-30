from __future__ import annotations

import hashlib
import os
import platform
import signal
import stat
import subprocess
import tarfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "bin" / "install_dorado_release.pl"
VALIDATOR = REPO_ROOT / "bin" / "validate_dorado_release.sh"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _platform_name() -> str:
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return "osx-arm64"
    if platform.system() == "Linux" and platform.machine() == "x86_64":
        return "linux-x64"
    raise RuntimeError("test fixture requires a supported Dorado platform")


def _build_fixture(tmp_path: Path) -> dict[str, Path]:
    archive_root = tmp_path / "archive-root" / "dorado-0.7.0-test"
    binary = archive_root / "bin" / "dorado"
    binary.parent.mkdir(parents=True)
    binary.write_text(
        """#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
  echo "0.7.0+fixture"
  exit 0
fi
if [ "${1:-}" = "basecaller" ] && [ "${2:-}" = "--help" ]; then
  echo "--device --read-ids --min-qscore --batchsize --chunksize --overlap --emit-sam"
  exit 0
fi
if [ "${1:-}" = "summary" ] && [ "${2:-}" = "--help" ]; then
  exit 0
fi
if [ "${1:-}" = "summary" ]; then
  printf 'filename\tread_id\trun_id\tsequence_length_template\tmean_qscore_template\n'
  printf 'fixture.pod5\tfixture-read\tfixture-run\t4\t40\n'
  exit 0
fi
if [ "${1:-}" = "basecaller" ]; then
  if [ -n "${DORADO_FIXTURE_STARTED_FILE:-}" ]; then
    printf 'started\n' > "${DORADO_FIXTURE_STARTED_FILE}"
  fi
  if [ -n "${DORADO_FIXTURE_SLEEP:-}" ]; then
    sleep "${DORADO_FIXTURE_SLEEP}"
  fi
  if [ "${DORADO_FIXTURE_SUCCESS:-0}" = "1" ]; then
    printf '@HD\tVN:1.6\n'
    printf 'fixture-read\t4\t*\t0\t0\t*\t*\t0\t0\tACGT\tIIII\n'
    exit 0
  fi
  echo "fixture Metal model-load failure" >&2
  exit 42
fi
exit 2
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)

    archive = tmp_path / "dorado-0.7.0-test.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        handle.add(archive_root, arcname=archive_root.name)

    model_source = tmp_path / "models"
    model_names = [
        "dna_r10.4.1_e8.2_400bps_fast@v5.0.0",
        "dna_r10.4.1_e8.2_400bps_hac@v5.0.0",
        "dna_r10.4.1_e8.2_400bps_sup@v4.3.0",
    ]
    for model_name in model_names:
        config = model_source / model_name / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(f'model = "{model_name}"\n', encoding="utf-8")

    rows = [
        "# release_id=dorado-0.7.0-test",
        f"# platform={_platform_name()}",
        "# expected_version=0.7.0+fixture",
        "# source_url=https://example.invalid/dorado-0.7.0-test.tar.gz",
        "kind\tartifact\tsha256\tbytes\tmode\trole",
        (
            f"archive\t{archive.name}\t{_sha256(archive)}\t{archive.stat().st_size}"
            "\t-\tfixture archive"
        ),
        (
            f"runtime\tbin/dorado\t{_sha256(binary)}\t{binary.stat().st_size}"
            "\t0555\tfixture executable"
        ),
    ]
    for model_name in model_names:
        config = model_source / model_name / "config.toml"
        rows.append(
            f"model\tmodels/{model_name}/config.toml\t{_sha256(config)}"
            f"\t{config.stat().st_size}\t0444\tfixture model"
        )

    manifest = tmp_path / "manifest.tsv"
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return {
        "archive": archive,
        "manifest": manifest,
        "model_source": model_source,
        "destination": tmp_path / "releases" / "dorado-0.7.0-test",
    }


def _install(fixture: dict[str, Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "perl",
            str(INSTALLER),
            "--archive",
            str(fixture["archive"]),
            "--model-source-dir",
            str(fixture["model_source"]),
            "--manifest",
            str(fixture["manifest"]),
            "--destination",
            str(fixture["destination"]),
        ],
        capture_output=True,
        text=True,
    )


def test_dorado_release_installs_side_by_side_and_reverifies(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    first = _install(fixture)
    assert first.returncode == 0, first.stderr
    assert Path(first.stdout.strip()) == fixture["destination"]
    assert (fixture["destination"] / "bin" / "dorado").is_file()
    assert (fixture["destination"] / "release_manifest.tsv").is_file()

    second = subprocess.run(
        [
            "perl",
            str(INSTALLER),
            "--manifest",
            str(fixture["manifest"]),
            "--destination",
            str(fixture["destination"]),
        ],
        capture_output=True,
        text=True,
    )
    assert second.returncode == 0, second.stderr
    assert Path(second.stdout.strip()) == fixture["destination"]


def test_dorado_release_rejects_archive_or_model_drift(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["archive"].write_bytes(fixture["archive"].read_bytes() + b"tamper")
    bad_archive = _install(fixture)
    assert bad_archive.returncode != 0
    assert "archive size mismatch" in bad_archive.stderr

    fixture = _build_fixture(tmp_path / "model-drift")
    config = (
        fixture["model_source"]
        / "dna_r10.4.1_e8.2_400bps_fast@v5.0.0"
        / "config.toml"
    )
    config.write_text("changed\n", encoding="utf-8")
    bad_model = _install(fixture)
    assert bad_model.returncode != 0
    assert "Dorado source artifact" in bad_model.stderr


def test_static_validator_checks_version_platform_and_command_surface(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    installed = _install(fixture)
    assert installed.returncode == 0, installed.stderr

    result = subprocess.run(
        [
            str(VALIDATOR),
            "--manifest",
            str(fixture["manifest"]),
            "--release-dir",
            str(fixture["destination"]),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "static compatibility passed" in result.stdout


def test_live_validator_rejects_unattested_pod5_before_basecalling(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    installed = _install(fixture)
    assert installed.returncode == 0, installed.stderr

    pod5 = tmp_path / "qualification.pod5"
    pod5.write_bytes(b"not the attested fixture")
    qualification_manifest = tmp_path / "qualification.tsv"
    qualification_manifest.write_text(
        "\t".join(
            [
                "artifact",
                "sha256",
                "bytes",
                "source_url",
                "source_revision",
                "chemistry",
                "role",
            ]
        )
        + "\n"
        + "\t".join(
            [
                pod5.name,
                "0" * 64,
                str(pod5.stat().st_size),
                "https://example.invalid/qualification.pod5",
                "fixture-revision",
                "R10.4.1_E8.2_400bps_5kHz",
                "test fixture",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            str(VALIDATOR),
            "--manifest",
            str(fixture["manifest"]),
            "--release-dir",
            str(fixture["destination"]),
            "--qualification-pod5",
            str(pod5),
            "--qualification-manifest",
            str(qualification_manifest),
            "--device",
            "cpu",
            "--report",
            str(tmp_path / "report.tsv"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "qualification POD5 checksum mismatch" in result.stderr


def test_live_validator_preserves_read_only_failure_evidence(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    installed = _install(fixture)
    assert installed.returncode == 0, installed.stderr

    pod5 = tmp_path / "qualification.pod5"
    pod5.write_bytes(b"attested qualification fixture")
    qualification_manifest = tmp_path / "qualification.tsv"
    qualification_manifest.write_text(
        "\t".join(
            [
                "artifact",
                "sha256",
                "bytes",
                "source_url",
                "source_revision",
                "chemistry",
                "role",
            ]
        )
        + "\n"
        + "\t".join(
            [
                pod5.name,
                _sha256(pod5),
                str(pod5.stat().st_size),
                "https://example.invalid/qualification.pod5",
                "fixture-revision",
                "R10.4.1_E8.2_400bps_5kHz",
                "test fixture",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "qualification-report.tsv"

    result = subprocess.run(
        [
            str(VALIDATOR),
            "--manifest",
            str(fixture["manifest"]),
            "--release-dir",
            str(fixture["destination"]),
            "--qualification-pod5",
            str(pod5),
            "--qualification-manifest",
            str(qualification_manifest),
            "--device",
            "metal",
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Dorado fast basecalling failed on requested device 'metal'" in result.stderr
    evidence = Path(f"{report}.evidence")
    assert report.is_file(), result.stderr
    assert evidence.is_dir(), result.stderr

    report_values = dict(
        line.split("\t", 1)
        for line in report.read_text(encoding="utf-8").splitlines()[1:]
    )
    assert report_values["status"] == "failed"
    assert report_values["failed_stage"] == "fast"
    assert report_values["stage_exit_status"] == "42"
    assert report_values["validator_exit_status"] == "1"
    assert report_values["evidence_directory"] == str(evidence)

    assert "fixture Metal model-load failure" in (
        evidence / "fast.log"
    ).read_text(encoding="utf-8")
    commands = (evidence / "commands.tsv").read_text(encoding="utf-8")
    assert "\tbasecaller\tmetal\t1\t100\t1000\t5040\t0\t" in commands
    assert (evidence / "environment.tsv").is_file()
    assert (evidence / "dorado-version.txt").is_file()
    assert (evidence / "resource-limits.txt").is_file()
    assert (evidence / "runtime-environment.tsv").is_file()
    checksum_result = subprocess.run(
        ["shasum", "-a", "256", "-c", "checksums.sha256"],
        cwd=evidence,
        capture_output=True,
        text=True,
    )
    assert checksum_result.returncode == 0, checksum_result.stderr
    assert report.stat().st_ino == (evidence / "failure-report.tsv").stat().st_ino
    assert not Path(f"{report}.lockdir").exists()
    assert stat.S_IMODE(report.stat().st_mode) == 0o444
    assert stat.S_IMODE(evidence.stat().st_mode) == 0o555
    for artifact in evidence.iterdir():
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o444


def test_live_validator_serializes_attempts_for_the_same_report(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    installed = _install(fixture)
    assert installed.returncode == 0, installed.stderr

    pod5 = tmp_path / "qualification.pod5"
    pod5.write_bytes(b"attested qualification fixture")
    qualification_manifest = tmp_path / "qualification.tsv"
    qualification_manifest.write_text(
        "artifact\tsha256\tbytes\tsource_url\tsource_revision\tchemistry\trole\n"
        f"{pod5.name}\t{_sha256(pod5)}\t{pod5.stat().st_size}"
        "\thttps://example.invalid/qualification.pod5\tfixture-revision"
        "\tR10.4.1_E8.2_400bps_5kHz\ttest fixture\n",
        encoding="utf-8",
    )
    report = tmp_path / "qualification-report.tsv"
    started = tmp_path / "basecaller-started"
    command = [
        str(VALIDATOR),
        "--manifest",
        str(fixture["manifest"]),
        "--release-dir",
        str(fixture["destination"]),
        "--qualification-pod5",
        str(pod5),
        "--qualification-manifest",
        str(qualification_manifest),
        "--device",
        "metal",
        "--report",
        str(report),
    ]
    first = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            **os.environ,
            "DORADO_FIXTURE_STARTED_FILE": str(started),
            "DORADO_FIXTURE_SLEEP": "2",
        },
    )
    for _ in range(100):
        if started.exists():
            break
        time.sleep(0.05)
    assert started.exists()

    second = subprocess.run(command, capture_output=True, text=True)
    assert second.returncode != 0
    assert "locked by another attempt" in second.stderr

    first_stdout, first_stderr = first.communicate(timeout=10)
    assert first.returncode != 0, first_stdout
    assert "fixture Metal model-load failure" in (
        Path(f"{report}.evidence") / "fast.log"
    ).read_text(encoding="utf-8")
    assert "Failure report:" in first_stderr
    assert not Path(f"{report}.lockdir").exists()


def test_term_preserves_failure_evidence_and_releases_lock(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    installed = _install(fixture)
    assert installed.returncode == 0, installed.stderr

    pod5 = tmp_path / "qualification.pod5"
    pod5.write_bytes(b"attested qualification fixture")
    qualification_manifest = tmp_path / "qualification.tsv"
    qualification_manifest.write_text(
        "artifact\tsha256\tbytes\tsource_url\tsource_revision\tchemistry\trole\n"
        f"{pod5.name}\t{_sha256(pod5)}\t{pod5.stat().st_size}"
        "\thttps://example.invalid/qualification.pod5\tfixture-revision"
        "\tR10.4.1_E8.2_400bps_5kHz\ttest fixture\n",
        encoding="utf-8",
    )
    report = tmp_path / "qualification-report.tsv"
    started = tmp_path / "basecaller-started"
    process = subprocess.Popen(
        [
            str(VALIDATOR),
            "--manifest",
            str(fixture["manifest"]),
            "--release-dir",
            str(fixture["destination"]),
            "--qualification-pod5",
            str(pod5),
            "--qualification-manifest",
            str(qualification_manifest),
            "--device",
            "metal",
            "--report",
            str(report),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env={
            **os.environ,
            "DORADO_FIXTURE_STARTED_FILE": str(started),
            "DORADO_FIXTURE_SLEEP": "30",
        },
    )
    for _ in range(100):
        if started.exists():
            break
        time.sleep(0.05)
    assert started.exists()
    os.killpg(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143, (stdout, stderr)
    values = dict(
        line.split("\t", 1)
        for line in report.read_text(encoding="utf-8").splitlines()[1:]
    )
    assert values["status"] == "failed"
    assert values["failure_reason"] == "qualification interrupted by TERM"
    assert values["validator_exit_status"] == "143"
    assert (Path(f"{report}.evidence") / "fast.log").is_file()
    assert not Path(f"{report}.lockdir").exists()


def test_live_validator_preserves_production_calls_and_separate_format_probes() -> None:
    validator = VALIDATOR.read_text(encoding="utf-8")
    assert 'run_basecaller fast "$fast_model" 100 1000 5040 0 "" 1' in validator
    assert (
        'run_basecaller hac "$hac_model" 500 2000 3024 10 \\\n'
        '\t"${qualification_tmp}/selected_read_ids.list" 0'
    ) in validator
    assert (
        'run_basecaller sup "$sup_model" 1000 5000 720 15 \\\n'
        '\t"${qualification_tmp}/selected_read_ids.list" 0'
    ) in validator
    assert "hac_format_probe" in validator
    assert "sup_format_probe" in validator
    assert "--qualification-manifest is required with --qualification-pod5" in validator


def test_live_validator_reports_accelerator_and_diagnostic_success(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    installed = _install(fixture)
    assert installed.returncode == 0, installed.stderr

    pod5 = tmp_path / "qualification.pod5"
    pod5.write_bytes(b"attested qualification fixture")
    qualification_manifest = tmp_path / "qualification.tsv"
    qualification_manifest.write_text(
        "artifact\tsha256\tbytes\tsource_url\tsource_revision\tchemistry\trole\n"
        f"{pod5.name}\t{_sha256(pod5)}\t{pod5.stat().st_size}"
        "\thttps://example.invalid/qualification.pod5\tfixture-revision"
        "\tR10.4.1_E8.2_400bps_5kHz\ttest fixture\n",
        encoding="utf-8",
    )

    cases = {
        "metal": (
            "accelerator",
            "accelerator_candidate",
            "accelerator_compatibility_passed",
        ),
        "cpu": ("non_accelerator", "diagnostic_only", "compatibility_only"),
    }
    for device, expected in cases.items():
        report = tmp_path / f"{device}-qualification-report.tsv"
        result = subprocess.run(
            [
                str(VALIDATOR),
                "--manifest",
                str(fixture["manifest"]),
                "--release-dir",
                str(fixture["destination"]),
                "--qualification-pod5",
                str(pod5),
                "--qualification-manifest",
                str(qualification_manifest),
                "--device",
                device,
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
            env={**os.environ, "DORADO_FIXTURE_SUCCESS": "1"},
        )

        assert result.returncode == 0, result.stderr
        values = dict(
            line.split("\t", 1)
            for line in report.read_text(encoding="utf-8").splitlines()[1:]
        )
        assert (
            values["device_class"],
            values["qualification_scope"],
            values["status"],
        ) == expected
        assert values["fast_reads"] == "1"
        assert values["hac_format_probe_reads"] == "1"
        assert values["sup_format_probe_reads"] == "1"
        assert stat.S_IMODE(report.stat().st_mode) == 0o444
        assert not Path(f"{report}.evidence").exists()
        assert not Path(f"{report}.lockdir").exists()
        if device == "cpu":
            assert "diagnostic-only" in result.stdout
        else:
            assert "diagnostic-only" not in result.stdout


def test_only_accelerator_devices_can_produce_production_qualification() -> None:
    validator = VALIDATOR.read_text(encoding="utf-8")
    assert "metal|cuda|cuda:*)" in validator
    assert 'qualification_scope="accelerator_candidate"' in validator
    assert 'report_status="accelerator_compatibility_passed"' in validator
    assert 'qualification_scope="diagnostic_only"' in validator
    assert 'report_status="compatibility_only"' in validator
    assert "cannot qualify a production RTBioScan release" in validator


def test_dorado_candidate_installation_does_not_change_pipeline_defaults() -> None:
    config = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    assert 'dorado_bin = "bin/dorado/bin/dorado"' in config
    assert 'fast_model = "bin/dorado/bin/dna_r10.4.1_e8.2_400bps_fast@v5.0.0"' in config
    assert 'hac_model = "bin/dorado/bin/dna_r10.4.1_e8.2_400bps_hac@v5.0.0"' in config
    assert 'sup_model = "bin/dorado/bin/dna_r10.4.1_e8.2_400bps_sup@v4.3.0"' in config


def test_public_release_never_bundles_arbitrary_local_dorado_bytes() -> None:
    release_script = (REPO_ROOT / "bin" / "prepare_public_release.sh").read_text(
        encoding="utf-8"
    )
    assert "INCLUDE_DORADO" not in release_script
    assert "Never copy arbitrary local bytes into a public release" in release_script
