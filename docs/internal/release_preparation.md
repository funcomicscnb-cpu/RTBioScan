# RTBioScan: Release Preparation

> **Internal Docs:** [Index](README.md) · [Public Release Manifest](public_release_manifest.md)

> **Audience:** Maintainers and developers. This page is not intended for the final end-user documentation set.

This page documents the maintainer-side helper script:

```bash
bin/prepare_public_release.sh
```

The script is used **before** publishing a release. It assembles a clean release directory from the working repository, following the rules in [public_release_manifest.md](public_release_manifest.md).

This script is **not intended to be included inside the final public release bundle**. The documentation is kept under `docs/internal/` so maintainers can still reproduce the packaging process later.

## Purpose

The script creates a release directory that is ready to compress and share, while excluding:

- outdated snapshots such as `*.pre_*`
- generated run state such as `results/`, `work/`, `.nextflow/`, and logs
- Python caches and `.DS_Store` files
- internal local-only files that should not be published

It also adds profile-specific databases and support files when requested.

## Basic usage

From the repository root:

```bash
bash bin/prepare_public_release.sh --force
```

Default output:

```text
release/RTBioScan_public
```

After the directory is assembled, compress it with your preferred tool, for example:

```bash
tar -C release -czf RTBioScan_public.tar.gz RTBioScan_public
```

## Common examples

Core runtime release only:

```bash
bash bin/prepare_public_release.sh \
  --outdir release/RTBioScan_public \
  --force
```

Release that also supports bundled `voucher` and `xprize` profiles:

```bash
bash bin/prepare_public_release.sh \
  --outdir release/RTBioScan_public \
  --profiles voucher,xprize \
  --force
```

Release including tests and the `test` profile assets:

```bash
bash bin/prepare_public_release.sh \
  --outdir release/RTBioScan_public_testable \
  --profiles test \
  --with-tests \
  --force
```

Smaller release without bundled Dorado or local Nextflow:

```bash
bash bin/prepare_public_release.sh \
  --outdir release/RTBioScan_public_nobundles \
  --profiles voucher \
  --no-dorado \
  --no-nextflow \
  --force
```

## Options

`--outdir DIR`

- Output directory for the assembled release tree.
- Default: `release/RTBioScan_public` under the repository root.

`--profiles LIST`

- Comma-separated list of bundled profiles to support explicitly.
- Supported values: `barcoding`, `voucher`, `xprize`, `test`.
- This controls which extra databases and config files are copied beyond the default runtime set.

`--with-tests`

- Includes `tests/`, `pytest.ini`, and `Makefile`.
- Useful for internal review bundles, not usually needed for public end-user releases.

`--no-nextflow`

- Skips bundling the local `./nextflow` binary, even if it exists in the repository.

`--no-dorado`

- Skips bundling `bin/dorado/`.
- Useful when Dorado binaries and models are distributed separately or are too large for the intended release artifact.

`--strict`

- Treats missing manifest-required files, directories, or database prefixes as fatal errors.
- Without this flag, the script emits warnings and still builds the release directory.

`--force`

- Removes an existing output directory before rebuilding it.

`-h`, `--help`

- Prints the script help message.

## What the script copies

The script always copies:

- core runtime files such as `main.nf`, `nextflow.config`, and `RTBioScan.sh`
- shipped config files for bundled profiles
- the active `bin/` scripts
- `bin/lib/`
- `bin/report_placeholders/failed_round/`
- core docs and report assets
- database files required by `nextflow.config`

It conditionally copies:

- `conf/test.config` when `--profiles test` is requested
- `tests/`, `pytest.ini`, and `Makefile` when `--with-tests` is used
- `bin/dorado/` unless `--no-dorado` is used
- the local `nextflow` binary unless `--no-nextflow` is used
- profile-specific database files described in [public_release_manifest.md](public_release_manifest.md)

## What the script excludes

The script excludes:

- `bin/*.pre_*`
- `main.pre_refactor.nf`
- `conf/.xprize.config.swp`
- `bin/__pycache__/`, `tests/__pycache__/`, and `*.pyc`
- `.DS_Store`
- generated state such as `results/`, `work/`, `.nextflow/`, `.nextflow.log*`, and `Consensus/`

For bundled Dorado, it keeps only the model paths currently required by `nextflow.config`:

- `dna_r10.4.1_e8.2_400bps_fast@v5.0.0`
- `dna_r10.4.1_e8.2_400bps_hac@v5.0.0`
- `dna_r10.4.1_e8.2_400bps_sup@v4.3.0`

## Profile handling

Profile-specific additions are based on the rules documented in [public_release_manifest.md](public_release_manifest.md).

Examples:

- `--profiles barcoding` adds the corrected ITS2 memtax table for that profile.
- `--profiles voucher` adds the voucher-specific memtax requirements and assumes the final runtime environment will also provide the required demultiplex FASTAs.
- `--profiles xprize` adds the XPrize species-basics and local-genus helper files.
- `--profiles test` adds `conf/test.config` plus the test BLAST database prefixes.

If `--profiles` is omitted, the script builds a core runtime release only.

## Warnings and failure behavior

By default, missing required items produce warnings and the script continues.

Use `--strict` when you want packaging to stop immediately if:

- a manifest-required file is missing
- a required directory is missing
- a required database prefix has no matching files

This is useful for CI or release-candidate validation.

## Known caveats

The script reflects the current repository state, including current gaps.

The release includes `environment.yml` plus separate `linux-64` and `osx-64`
locks. A clean dependency solve is necessary but does not qualify the runtime:
the locked environment must also pass the committed behavioral acceptance
checks before the `conda` profile is enabled or advertised.

Another common case is missing database prefixes referenced by config files. In non-strict mode this becomes a warning; in strict mode it is a hard error.

## Recommended maintainer workflow

1. Review [public_release_manifest.md](public_release_manifest.md) and confirm which profiles the release should support.
2. Run `bin/prepare_public_release.sh` with the matching `--profiles` set.
3. Inspect the assembled output directory.
4. Compress the assembled directory into the archive you intend to publish.
5. Keep this documentation page in the repository even if the helper script itself is excluded from the published bundle.
