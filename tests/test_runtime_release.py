from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "blast": "2.15.0",
    "last": "1542",
    "seqkit": "2.6.1",
    "taxonkit": "0.14.2",
    "cutadapt": "4.6",
}


def _locked_versions(path: Path, platform: str) -> dict[str, str]:
    packages: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("- name: "):
            if current is not None:
                packages.append(current)
            current = {"name": line[len("- name: ") :].strip("'")}
        elif current is not None and line.startswith("  version: "):
            current["version"] = line[len("  version: ") :].strip("'")
        elif current is not None and line.startswith("  platform: "):
            current["platform"] = line[len("  platform: ") :].strip("'")
        elif current is not None and line.startswith("  url: "):
            current["url"] = line[len("  url: ") :]
    if current is not None:
        packages.append(current)

    assert packages
    assert {package["platform"] for package in packages} == {platform}
    for package in packages:
        url = package["url"]
        assert f"/{platform}/" in url or "/noarch/" in url
    return {
        package["name"]: str(package["version"])
        for package in packages
        if package["name"] in EXPECTED
    }


def test_environment_pins_match_the_existing_last_index_and_host_baseline() -> None:
    environment = (REPO_ROOT / "environment.yml").read_text(encoding="utf-8")
    for package, version in EXPECTED.items():
        assert f"- {package} ={version}" in environment

    project = (REPO_ROOT / "db" / "targets_All_tagged_nr95.prj").read_text(
        encoding="utf-8"
    )
    project_version = next(
        line.split("=", 1)[1]
        for line in project.splitlines()
        if line.startswith("version=")
    )
    assert project_version == EXPECTED["last"]


def test_platform_locks_are_isolated_and_preserve_load_bearing_versions() -> None:
    assert (
        _locked_versions(REPO_ROOT / "conda-lock-linux-64.yml", "linux-64")
        == EXPECTED
    )
    assert (
        _locked_versions(REPO_ROOT / "conda-lock-osx-64.yml", "osx-64")
        == EXPECTED
    )


def test_release_bundles_environment_locks_and_runtime_validator() -> None:
    release = (REPO_ROOT / "bin" / "prepare_public_release.sh").read_text(
        encoding="utf-8"
    )
    for artifact in [
        "environment.yml",
        "conda-lock-linux-64.yml",
        "conda-lock-osx-64.yml",
        "conf/runtime_validation/fast_routing_endosymbionts.fa",
        "conf/runtime_validation/fast_routing_endosymbionts.expected.tsv",
    ]:
        assert artifact in release

    validator = (REPO_ROOT / "bin" / "validate_runtime.sh").read_text(
        encoding="utf-8"
    )
    assert 'index_version="$(awk' in validator
    assert "fast_routing_endosymbionts.expected.tsv" in validator
    assert "-task megablast -dust no" in validator
    assert "-perc_identity 92 -evalue 11 -max_hsps 50 -max_target_seqs 1" in validator
    assert "-word_size 50 -qcov_hsp_perc 50 -mt_mode 2" in validator
    assert '$3 "\\t" $4 "\\t" $12' not in validator
    for taxid in ["9606", "3702", "562", "1386", "2157"]:
        assert taxid in validator
