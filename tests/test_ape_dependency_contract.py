"""Offline, platform-independent D3-1 ape dependency and lock integrity contract."""

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ("osx-64", "linux-64")
CONTENT_HASHES = {"osx-64": "abbeb498a38d10ddf4b1ce549e3a7a6c37a36ce1442be1e293bab33a017ca01c", "linux-64": "4845159b617fb811bbd0a49d67f618456838485b58862e564ce6d5fc16d2058e"}
BASE_ARTIFACT_DIGESTS = {'linux-64': 'c088c1eb9afead0d14690b695208b91c74ea094d2ce8bb83205dd9eb1ed74f20',
 'osx-64': 'b627076418892f69b2260914e59e026ea715b96e681f5766ed87ce7bc05adf0e'}

BASE_DEPENDENCY_DIGESTS = {'linux-64': '9b2315f7d91131345d1db55d76b5553fe3de90a7d2f8efecfd8ff9ba76ce6119',
 'osx-64': '9248aca679e69a7dcf3f1eb7eb1c7dc2406ae2d53fc0c05b05b82cd4f6a7096a'}

TRANSITIONS = {'linux-64': {'htslib': {'new': {'bzip2': '>=1.0.8,<2.0a0',
                                 'libcurl': '>=8.8.0,<9.0a0',
                                 'libdeflate': '>=1.20,<1.27.0a0',
                                 'libgcc': '>=12',
                                 'libzlib': '>=1.2.13,<2.0a0',
                                 'openssl': '>=3.3.2,<4.0a0',
                                 'xz': '>=5.2.6,<6.0a0'},
                         'old': {'bzip2': '>=1.0.8,<2.0a0',
                                 'libcurl': '>=8.8.0,<9.0a0',
                                 'libdeflate': '>=1.20,<1.26.0a0',
                                 'libgcc': '>=12',
                                 'libzlib': '>=1.2.13,<2.0a0',
                                 'openssl': '>=3.3.2,<4.0a0',
                                 'xz': '>=5.2.6,<6.0a0'},
                         'sha256': 'ef5a53fae052a14cbccc50dc2290c80791db3d8a8c5a01435c7aa12f8a53740c'},
              'perl-carp': {'new': {'__unix': '',
                                    'perl': '>=5.32.1,<6.0a0',
                                    'perl-exporter': '',
                                    'perl-extutils-makemaker': ''},
                            'old': {'perl': '>=5.32.1,<6.0a0',
                                    'perl-exporter': '',
                                    'perl-extutils-makemaker': ''},
                            'sha256': '1981e31113e1e77a2cdc13db657c636f047cd3be2a64d9a0bffac03c5427c1bd'},
              'perl-common-sense': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                                    'old': {'perl': '>=5.32.1,<6.0a0'},
                                    'sha256': '38ef218e9b9d55b9fbdce6b31cf81bcf6f1b16f21b8e7cb9279b41399522a320'},
              'perl-exporter': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                                'old': {'perl': '>=5.32.1,<6.0a0'},
                                'sha256': '42271d0b79043a10a89044acb5febea50046b745dd2fc37e02943bc3bc75bf8e'},
              'perl-exporter-tiny': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                                     'old': {'perl': '>=5.32.1,<6.0a0'},
                                     'sha256': 'abdf86828a12a389d0feb0d70501b267842557bae11820e266526aeb6ab2bebe'},
              'perl-extutils-makemaker': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                                          'old': {'perl': '>=5.32.1,<6.0a0'},
                                          'sha256': '1d3f342ca74cf2948c3edcfe0d3367b1db0fc64bb163393a2e025336dec3a40c'},
              'perl-parent': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                              'old': {'perl': '>=5.32.1,<6.0a0'},
                              'sha256': 'ec57d9e56ba86d840d5a9e65c665365fb6a290851cd13e6719628f3295eabf34'},
              'samtools': {'new': {'htslib': '>=1.21,<1.25.0a0',
                                   'libgcc': '>=12',
                                   'libzlib': '>=1.2.13,<2.0a0',
                                   'ncurses': '>=6.5,<7.0a0'},
                           'old': {'htslib': '>=1.21,<1.24.0a0',
                                   'libgcc': '>=12',
                                   'libzlib': '>=1.2.13,<2.0a0',
                                   'ncurses': '>=6.5,<7.0a0'},
                           'sha256': 'ff7c3abbb825f58b93b353abb9c78e07c8f92c8a893bea59dbdd6631cbaaf9de'}},
 'osx-64': {'htslib': {'new': {'bzip2': '>=1.0.8,<2.0a0',
                               'libcurl': '>=8.8.0,<9.0a0',
                               'libdeflate': '>=1.20,<1.27.0a0',
                               'libzlib': '>=1.2.13,<2.0a0',
                               'xz': '>=5.2.6,<6.0a0'},
                       'old': {'bzip2': '>=1.0.8,<2.0a0',
                               'libcurl': '>=8.8.0,<9.0a0',
                               'libdeflate': '>=1.20,<1.26.0a0',
                               'libzlib': '>=1.2.13,<2.0a0',
                               'xz': '>=5.2.6,<6.0a0'},
                       'sha256': '0c6191f2b130afa2bfe0dbdc3a0eb6871f12d0368c2f9c088a773a7b0663e7dd'},
            'perl-carp': {'new': {'__unix': '',
                                  'perl': '>=5.32.1,<6.0a0',
                                  'perl-exporter': '',
                                  'perl-extutils-makemaker': ''},
                          'old': {'perl': '>=5.32.1,<6.0a0',
                                  'perl-exporter': '',
                                  'perl-extutils-makemaker': ''},
                          'sha256': '1981e31113e1e77a2cdc13db657c636f047cd3be2a64d9a0bffac03c5427c1bd'},
            'perl-common-sense': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                                  'old': {'perl': '>=5.32.1,<6.0a0'},
                                  'sha256': '38ef218e9b9d55b9fbdce6b31cf81bcf6f1b16f21b8e7cb9279b41399522a320'},
            'perl-exporter': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                              'old': {'perl': '>=5.32.1,<6.0a0'},
                              'sha256': '42271d0b79043a10a89044acb5febea50046b745dd2fc37e02943bc3bc75bf8e'},
            'perl-exporter-tiny': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                                   'old': {'perl': '>=5.32.1,<6.0a0'},
                                   'sha256': 'abdf86828a12a389d0feb0d70501b267842557bae11820e266526aeb6ab2bebe'},
            'perl-extutils-makemaker': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                                        'old': {'perl': '>=5.32.1,<6.0a0'},
                                        'sha256': '1d3f342ca74cf2948c3edcfe0d3367b1db0fc64bb163393a2e025336dec3a40c'},
            'perl-parent': {'new': {'__unix': '', 'perl': '>=5.32.1,<6.0a0'},
                            'old': {'perl': '>=5.32.1,<6.0a0'},
                            'sha256': 'ec57d9e56ba86d840d5a9e65c665365fb6a290851cd13e6719628f3295eabf34'},
            'samtools': {'new': {'htslib': '>=1.21,<1.25.0a0',
                                 'libzlib': '>=1.2.13,<2.0a0',
                                 'ncurses': '>=6.5,<7.0a0'},
                         'old': {'htslib': '>=1.21,<1.24.0a0',
                                 'libzlib': '>=1.2.13,<2.0a0',
                                 'ncurses': '>=6.5,<7.0a0'},
                         'sha256': 'a246184b101f982400e46a67d0db84587d1baa0ae8d8aff2f663bbf6ebe287a5'}}}

NEW_RECORDS = {'linux-64': {'r-ape': {'category': 'main',
                        'dependencies': {'__glibc': '>=2.17,<3.0.a0',
                                         'libblas': '>=3.9.0,<4.0a0',
                                         'libgcc': '>=14',
                                         'liblapack': '>=3.9.0,<4.0a0',
                                         'libstdcxx': '>=14',
                                         'r-base': '>=4.3,<4.4.0a0',
                                         'r-digest': '',
                                         'r-lattice': '',
                                         'r-nlme': '',
                                         'r-rcpp': '>=0.12.0'},
                        'manager': 'conda',
                        'md5': 'b868c67df0a1069db56f984d025ea6c8',
                        'name': 'r-ape',
                        'optional': False,
                        'platform': 'linux-64',
                        'sha256': '1b5c831740e6ed4b6c579fda94e85230719acfedbb57ab92243930d126dec881',
                        'url': 'https://conda.anaconda.org/conda-forge/linux-64/r-ape-5.8_1-r43h3704496_1.conda',
                        'version': '5.8_1'},
              'r-digest': {'category': 'main',
                           'dependencies': {'__glibc': '>=2.17,<3.0.a0',
                                            'libgcc-ng': '>=12',
                                            'libstdcxx-ng': '>=12',
                                            'r-base': '>=4.3,<4.4.0a0'},
                           'manager': 'conda',
                           'md5': 'edcd201672d47f522666954cc7b25e0d',
                           'name': 'r-digest',
                           'optional': False,
                           'platform': 'linux-64',
                           'sha256': 'fa6b14abad4345d72adecf5b7e80948ead2d85a511bf6962014c758614f74868',
                           'url': 'https://conda.anaconda.org/conda-forge/linux-64/r-digest-0.6.37-r43h0d4f4ea_0.conda',
                           'version': '0.6.37'}},
 'osx-64': {'r-ape': {'category': 'main',
                      'dependencies': {'__osx': '>=10.13',
                                       'libblas': '>=3.9.0,<4.0a0',
                                       'libcxx': '>=19',
                                       'liblapack': '>=3.9.0,<4.0a0',
                                       'r-base': '>=4.3,<4.4.0a0',
                                       'r-digest': '',
                                       'r-lattice': '',
                                       'r-nlme': '',
                                       'r-rcpp': '>=0.12.0'},
                      'manager': 'conda',
                      'md5': '2d74281b34cd277fe96f70b949e86c6e',
                      'name': 'r-ape',
                      'optional': False,
                      'platform': 'osx-64',
                      'sha256': '066edd91ef65fa3dbd1ada5b79d85cc7d30a9ce61f4c7694d9f36f0f9d2eeeec',
                      'url': 'https://conda.anaconda.org/conda-forge/osx-64/r-ape-5.8_1-r43h1934be7_1.conda',
                      'version': '5.8_1'},
            'r-digest': {'category': 'main',
                         'dependencies': {'__osx': '>=10.13',
                                          'libcxx': '>=16',
                                          'r-base': '>=4.3,<4.4.0a0'},
                         'manager': 'conda',
                         'md5': 'e5f5fe851f7abbbc85a9be4d1fadb319',
                         'name': 'r-digest',
                         'optional': False,
                         'platform': 'osx-64',
                         'sha256': '48c04ba806a9bc541add3c958a2dc64f768ff364da832cc4634bdd0046c4144f',
                         'url': 'https://conda.anaconda.org/conda-forge/osx-64/r-digest-0.6.37-r43h25d921d_0.conda',
                         'version': '0.6.37'}}}

def _digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity(record):
    return {key: record[key] for key in ("name", "manager", "platform", "version", "url", "category", "optional")} | {
        "md5": record["hash"]["md5"], "sha256": record["hash"]["sha256"]
    }


def _addition(record):
    identity = _identity(record)
    identity["dependencies"] = record.get("dependencies") or {}
    return identity


def _version(value):
    # Conda's r-ape 5.8_1 package carries R ape 5.8.1.
    return tuple(int(part) for part in value.replace("_", ".").split("."))


def validate_contract(environment_text, installation_text, locks):
    """Assert the reviewed D3-1 declaration and both lock contracts from file content only."""
    environment = yaml.safe_load(environment_text)
    ape_specs = [item for item in environment["dependencies"] if isinstance(item, str) and item.startswith("r-ape")]
    assert ape_specs == ["r-ape >=5.7"]
    assert "`ape` is required" in installation_text
    assert "included in both locked Conda runtimes" in installation_text
    assert "library(ape)" in installation_text

    for platform in PLATFORMS:
        lock = yaml.safe_load(locks[platform])
        metadata = lock["metadata"]
        assert metadata["platforms"] == [platform]
        assert metadata["sources"] == ["environment.yml"]
        assert [channel["url"] for channel in metadata["channels"]] == ["conda-forge", "bioconda", "defaults"]
        assert metadata["content_hash"] == {platform: CONTENT_HASHES[platform]}

        records = lock["package"]
        assert len(records) == 239
        names = [record["name"] for record in records]
        assert len(names) == len(set(names))
        assert names.count("r-ape") == 1
        assert names.count("r-digest") == 1
        by_name = {record["name"]: record for record in records}
        for record in records:
            assert record["manager"] == "conda"
            assert record["platform"] == platform
            url = urlparse(record["url"])
            assert url.scheme == "https"
            assert f"/{platform}/" in url.path or "/noarch/" in url.path
            assert url.path.rsplit("/", 1)[-1].startswith(f"{record['name']}-{record['version']}-")
            assert len(record["hash"]["md5"]) == 32
            assert len(record["hash"]["sha256"]) == 64

        assert _version(by_name["r-ape"]["version"]) >= (5, 7)
        for name, expected in NEW_RECORDS[platform].items():
            assert _addition(by_name[name]) == expected

        existing = {name: record for name, record in by_name.items() if name not in NEW_RECORDS[platform]}
        assert len(existing) == 237
        assert _digest([_identity(existing[name]) for name in sorted(existing)]) == BASE_ARTIFACT_DIGESTS[platform]

        old_dependencies = {name: existing[name].get("dependencies") or {} for name in sorted(existing)}
        assert set(TRANSITIONS[platform]) == {
            "htslib", "samtools", "perl-carp", "perl-common-sense", "perl-exporter",
            "perl-exporter-tiny", "perl-extutils-makemaker", "perl-parent"
        }
        for name, transition in TRANSITIONS[platform].items():
            assert existing[name]["hash"]["sha256"] == transition["sha256"]
            assert old_dependencies[name] == transition["new"]
            old_dependencies[name] = transition["old"]
        assert _digest(old_dependencies) == BASE_DEPENDENCY_DIGESTS[platform]


def test_ape_dependency_contract():
    validate_contract(
        (REPO_ROOT / "environment.yml").read_text(encoding="utf-8"),
        (REPO_ROOT / "docs" / "installation.md").read_text(encoding="utf-8"),
        {platform: (REPO_ROOT / f"conda-lock-{platform}.yml").read_text(encoding="utf-8") for platform in PLATFORMS},
    )
