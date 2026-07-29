from __future__ import annotations

import hashlib
import platform
import subprocess
import tarfile
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
