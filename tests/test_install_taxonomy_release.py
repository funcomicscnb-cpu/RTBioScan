import hashlib
import stat
import subprocess
import tarfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "install_taxonomy_release.pl"
ARTIFACTS = ["nodes.dmp", "names.dmp", "merged.dmp", "delnodes.dmp"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build_release_fixture(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    source = tmp_path / "source"
    source.mkdir()
    contents = {
        name: f"fixture taxonomy {name}\n".encode()
        for name in ARTIFACTS
    }
    for name, content in contents.items():
        (source / name).write_bytes(content)

    archive = tmp_path / "taxdump.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ARTIFACTS:
            tar.add(source / name, arcname=name)

    manifest = tmp_path / "taxonomy_release.tsv"
    rows = [
        "kind\tartifact\tsha256\tbytes\trole",
        f"archive\ttaxdump.tar.gz\t{_sha256(archive)}\t{archive.stat().st_size}\tfixture archive",
    ]
    for name in ARTIFACTS:
        path = source / name
        rows.append(
            f"data\t{name}\t{_sha256(path)}\t{path.stat().st_size}\tfixture {name}"
        )
    manifest.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return archive, manifest, contents


def _run_installer(
    archive: Path,
    manifest: Path,
    destination: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "--archive",
            str(archive),
            "--manifest",
            str(manifest),
            "--destination",
            str(destination),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_installs_verified_release_atomically_and_is_idempotent(tmp_path: Path) -> None:
    archive, manifest, contents = _build_release_fixture(tmp_path)
    destination = tmp_path / "releases" / "fixture-release"

    first = _run_installer(archive, manifest, destination)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == str(destination)
    assert stat.S_IMODE(destination.stat().st_mode) == 0o555
    for name, expected in contents.items():
        artifact = destination / name
        assert artifact.read_bytes() == expected
        assert stat.S_IMODE(artifact.stat().st_mode) == 0o444

    second = _run_installer(archive, manifest, destination)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout


def test_rejects_archive_that_does_not_match_release_manifest(tmp_path: Path) -> None:
    archive, manifest, _ = _build_release_fixture(tmp_path)
    archive.write_bytes(archive.read_bytes() + b"tampered")

    result = _run_installer(archive, manifest, tmp_path / "release")
    assert result.returncode != 0
    assert "taxonomy archive size mismatch" in result.stderr
    assert not (tmp_path / "release").exists()


def test_does_not_overwrite_mismatched_existing_release(tmp_path: Path) -> None:
    archive, manifest, _ = _build_release_fixture(tmp_path)
    destination = tmp_path / "release"
    destination.mkdir()
    for name in ARTIFACTS:
        (destination / name).write_text("wrong\n", encoding="utf-8")

    result = _run_installer(archive, manifest, destination)
    assert result.returncode != 0
    assert "taxonomy data artifact size mismatch" in result.stderr
    assert (destination / "nodes.dmp").read_text(encoding="utf-8") == "wrong\n"
