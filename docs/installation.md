# RTBioScan: Installation

> **Docs:** [Index](README.md) · [Pipeline Overview](pipeline.md) · [Concepts](concepts.md) · **Installation** · [Usage](usage.md) · [Output](output.md) · [Report Schema](report_schema.md)

## Table of contents
[back to Top](#rtbioscan-installation)

* [Overview](#overview)
* [Requirements at a glance](#requirements-at-a-glance)
* [1. Nextflow](#1-nextflow)
* [2. System tools](#2-system-tools)
  * [macOS (Apple Silicon — manual host runtime)](#macos-apple-silicon--manual-host-runtime)
  * [Locked Conda runtime (Linux x86-64 and macOS under Rosetta)](#locked-conda-runtime-linux-x86-64-and-macos-under-rosetta)
* [3. R and Bioconductor packages](#3-r-and-bioconductor-packages)
* [4. Python](#4-python)
* [5. Perl modules](#5-perl-modules)
* [6. Dorado basecaller](#6-dorado-basecaller)
  * [Install the audited macOS ARM64 baseline](#install-the-audited-macos-arm64-baseline)
  * [Static and live compatibility checks](#static-and-live-compatibility-checks)
  * [Explicit candidate selection](#explicit-candidate-selection)
* [7. BLAST databases](#7-blast-databases)
* [8. Verify the installation](#8-verify-the-installation)

---

## Overview
[back to Top](#rtbioscan-installation)

RTBioScan is a Nextflow DSL1 pipeline for real-time ONT metabarcoding. It requires:

- **Nextflow** 22.x as the workflow engine
- A set of **bioinformatics command-line tools** (BLAST+, cd-hit, vsearch, cutadapt, seqtk, seqkit, samtools, LAST)
- The **`pod5` CLI** for the wrapper/feeder workflow (`RTBioScan.sh --feeder` / `--do_metadata`)
- **R** with Bioconductor packages for consensus generation and report visualisation
- **Python 3** for report rendering, the local report server, and Python-packaged CLIs
- **Dorado** (Oxford Nanopore basecaller), provisioned separately per platform
- **BLAST databases** distributed separately from the code

The first reproducible runtime candidate is the committed Conda lock for
`linux-64` and macOS `osx-64` under Rosetta. Install and activate the lock, then
run RTBioScan without `-profile conda`; that profile remains disabled because
Nextflow would re-solve `environment.yml` instead of consuming the committed
lock. Native Homebrew remains usable only when every required tool is
provisioned at the validated versions.

RTBioScan does not currently publish a validated Docker or Singularity image.
The former `hecrp/nanortax` setting belonged to a different pipeline and must
not be used for RTBioScan.

---

## Requirements at a glance
[back to Top](#rtbioscan-installation)

| Component | Version | Notes |
|---|---|---|
| Nextflow | `>=22.10.0, <23.0.0` | DSL1; version 23+ not compatible |
| Java | 11 or 17 | Required by Nextflow |
| bash | ≥ 3.2 | macOS default is fine |
| Perl | ≥ 5.20 | Standard modules only (see §5) |
| Python | ≥ 3.8 | Pipeline scripts use stdlib only; `cutadapt` / `pod5` install via `pip` or `conda` |
| R | ≥ 4.0 | + Bioconductor packages (see §3) |
| BLAST+ | `2.15.0` | Audited classification baseline |
| cd-hit | ≥ 4.8 | `cd-hit-est` must be in `PATH` |
| vsearch | ≥ 2.21 | `vsearch` must be in `PATH` |
| cutadapt | `4.6` | Required for barcode / primer demultiplexing |
| seqtk | ≥ 1.3 | `seqtk` must be in `PATH` |
| seqkit | `2.6.1` | `seqkit` must be in `PATH` |
| samtools | ≥ 1.16 | `samtools` must be in `PATH` |
| LAST | `1542` | Must equal `version=1542` in the shipped `.prj` index |
| pod5 | ≥ 0.2 | Required for `RTBioScan.sh --feeder` / `--do_metadata`; optional for direct pre-sliced POD5 runs |
| taxonkit | `0.14.2` | Required; preserves the audited `{k}`/`{K}` semantics |
| Dorado | `0.7.0+71cc7442` baseline | Optional, platform-specific release; upgrades require qualification |
| BLAST databases | — | Provided separately (see §7) |

---

## 1. Nextflow
[back to Top](#rtbioscan-installation)

RTBioScan requires Nextflow **22.x** (DSL1). Version 23 and later changed the DSL behaviour and are **not compatible**.

```bash
# Install the Nextflow 22.10.8 binary directly
curl -fsSL https://github.com/nextflow-io/nextflow/releases/download/v22.10.8/nextflow \
  -o ~/bin/nextflow
chmod +x ~/bin/nextflow

# Verify
nextflow -version   # must show "22.10.x"
```

Nextflow requires Java 11 or 17:

```bash
# macOS — install via Homebrew
brew install openjdk@17
# then follow the `brew info openjdk@17` symlink instructions

# Ubuntu / Debian
sudo apt-get install -y openjdk-17-jdk

# Verify
java -version
```

---

## 2. System tools
[back to Top](#rtbioscan-installation)

### macOS (Apple Silicon — manual host runtime)
[back to Top](#rtbioscan-installation)

Homebrew can supply most native arm64 tools, but the shipped LAST index requires
LAST 1542 exactly. A newer `brew install last` is not automatically compatible.
Use this path only if you can provision the audited versions and pass
`bin/validate_runtime.sh`.

Generic `brew install` commands are deliberately not presented as a
reproducible recipe: Homebrew tracks current releases and may install a LAST,
BLAST, or TaxonKit version that differs from this reference release. For a new
installation, prefer the locked Conda runtime below. Existing manually
provisioned hosts must supply all tools in the requirements table; install the
wrapper-side `pod5` CLI as described in [§4 Python](#4-python).

> **Note:** A dependency installation is not acceptance. The runtime validator
> fails if `lastal` does not equal the `.prj` builder version or if another
> load-bearing tool differs from the audited baseline.

> **Conda on Apple Silicon:** the committed macOS lock is `osx-64`, not native
> `osx-arm64`, because the validated historical LAST/R/Bioconductor combination
> is unavailable natively. Rosetta 2 is required.

### Locked Conda runtime (Linux x86-64 and macOS under Rosetta)
[back to Top](#rtbioscan-installation)

Install the committed lock that matches the runtime platform:

```bash
# Linux x86-64
conda-lock install --name rtbioscan conda-lock-linux-64.yml
conda activate rtbioscan

# macOS Apple Silicon, using the osx-64 lock under Rosetta
CONDA_SUBDIR=osx-64 conda-lock install \
  --force-platform osx-64 \
  --name rtbioscan \
  conda-lock-osx-64.yml
conda activate rtbioscan
```

Validate the installed runtime against the shipped LAST index and pinned
taxonomy before starting the pipeline:

```bash
bin/validate_runtime.sh \
  --taxonomy-data-dir db/taxonomy/releases/ncbi-taxdump-2024-06-24 \
  --last-index db/targets_All_tagged_nr95
```

The validator checks the exact tool versions, confirms that LAST 1542 reads the
1542 index, replays the committed FAST first-hit fixture, exercises BLAST,
SeqKit, and Cutadapt, and verifies the TaxonKit kingdom matrix. Launch the
pipeline from the activated environment without `-profile conda`.

Both committed locks pass this validator against the shipped index and pinned
taxonomy: `osx-64` under Rosetta and `linux-64` with Linux/amd64 binaries. This
qualifies the toolchain behavior; a minimal full Nextflow round remains a
separate release gate before enabling the `conda` profile.

`pod5` is a wrapper-side host dependency and is intentionally not part of the
cross-platform process lock. Install it separately when using
`RTBioScan.sh --feeder` or `--do_metadata`.

---

## 3. R and Bioconductor packages
[back to Top](#rtbioscan-installation)

R ≥ 4.0 is required. Install R itself before the packages:

```bash
# macOS
brew install r

# Ubuntu
sudo apt-get install -y r-base r-base-dev
# or use CRAN's signed repo for the latest version:
# https://cran.r-project.org/bin/linux/ubuntu/
```

Then open an R session and install the required packages:

```r
# CRAN packages
install.packages(c(
  "ggplot2",
  "dplyr",
  "tidyr",
  "ggrepel",
  "ape"
))

# Bioconductor packages (consensus generation — required for pipeline to run)
if (!requireNamespace("BiocManager", quietly = TRUE))
  install.packages("BiocManager")

BiocManager::install(c(
  "Biostrings",   # DNA sequence manipulation
  "DECIPHER",     # AlignSeqs / ConsensusSequence
  "muscle"        # Alignment back-end used by DECIPHER
))

# Phylogenetics (optional — needed for tree visualisation outputs)
install.packages("treemapify")
```

`ape` is already included above and is used for cladogram plots. `treemapify` is optional: if it is absent, treemap reports fall back to placeholder images instead of failing the whole pipeline.

Verify the critical packages load without errors:

```r
library(Biostrings)
library(DECIPHER)
library(parallel)
library(ggplot2)
```

---

## 4. Python
[back to Top](#rtbioscan-installation)

Python ≥ 3.8 is required for HTML report rendering (`bin/report_render.py`), the local report server, and wrapper-side helper scripts. The pipeline's own Python code uses only the standard library, but RTBioScan also relies on Python-packaged CLIs for `cutadapt` and `pod5`.

```bash
# macOS
brew install python

# Ubuntu
sudo apt-get install -y python3 python3-pip

# Install Python-packaged command-line tools
python3 -m pip install --user cutadapt pod5

# Verify
python3 --version   # must be ≥ 3.8
```

`cutadapt` is required for sample demultiplexing. `pod5` is required when you use `RTBioScan.sh --feeder` or `--do_metadata`, because those wrapper-side scripts call `pod5 inspect`, `pod5 view`, and `pod5 filter` on the host.

---

## 5. Perl modules
[back to Top](#rtbioscan-installation)

All Perl code uses only **core modules** shipped with Perl 5.20+:

`strict`, `warnings`, `Digest::MD5`, `Digest::SHA`, `Fcntl`, `File::Basename`, `File::Copy`, `File::Glob`, `File::Path`, `File::Spec`, `File::Temp`, `FindBin`, `Getopt::Long`, `JSON::PP`, `POSIX`, `Scalar::Util`, `Time::HiRes`, `Time::Local`, `Time::Piece`

No CPAN installation is required if your system Perl is 5.20 or newer.

```bash
# Verify the Perl version and that JSON::PP (included since 5.14) is present
perl -e 'use JSON::PP; print "OK\n"'
```

---

## 6. Dorado basecaller
[back to Top](#rtbioscan-installation)

Dorado is provisioned independently from the Conda runtime because its binary,
models, and hardware backend are platform-specific. RTBioScan never updates
Dorado automatically and public release assembly does not copy arbitrary local
Dorado bytes.

The audited compatibility baseline is `0.7.0+71cc7442`. Installations are
immutable and side-by-side under:

```text
runtime/dorado/releases/<release-id>/
```

Installing a candidate does not change `dorado_bin`, model parameters, pipeline
state, or the supported default. A candidate must pass static, live hardware,
format, and full-round qualification before it can be promoted. Production
qualification requires the intended accelerator: Metal on supported Apple
hardware or CUDA on supported Linux hardware. A CPU run is diagnostic-only and
can never qualify a Dorado release for RTBioScan's real-time production path.

### Optional Dorado 0.2.3 recovery candidate

Dorado `0.2.3+4ed609d` remains an opt-in legacy candidate; it is not the
default. On the audited Apple M1 Pro, all three native 5 kHz v4.2-alpha FAST,
HAC, and SUP models initialized Metal and basecalled the one-read hardware
fixture through RTBioScan's compatibility launcher from a normal Terminal
session. The release accepts a directory rather than a single POD5 path, does
not accept the newer `--emit-sam` option, and does not provide `summary`.
RTBioScan detects the supported basecaller options and handles the input and
summary differences explicitly while keeping the current default path
unchanged.

Download the three native models into a persistent candidate directory using
the installed 0.2.3 binary:

```bash
mkdir -p runtime/dorado/candidates/dorado-0.2.3-models

/opt/bin/dorado download \
  --model dna_r10.4.1_e8.2_5khz_400bps_fast@v4.2.alpha \
  --directory runtime/dorado/candidates/dorado-0.2.3-models
/opt/bin/dorado download \
  --model dna_r10.4.1_e8.2_5khz_400bps_hac@v4.2.alpha \
  --directory runtime/dorado/candidates/dorado-0.2.3-models
/opt/bin/dorado download \
  --model dna_r10.4.1_e8.2_5khz_400bps_sup@v4.2.alpha \
  --directory runtime/dorado/candidates/dorado-0.2.3-models
```

Select the candidate only in a fresh shadow state:

```bash
./RTBioScan.sh \
  --state_id DORADO_023_SHADOW \
  --outdir results_dorado_023_shadow \
  --state_toolchain_policy_manifest conf/runtime_compatibility/toolchain_dorado_0.2.3_v1.tsv \
  --dorado_bin /opt/bin/dorado \
  --dorado_input_mode directory \
  --dorado_summary_bin bin/dorado/bin/dorado \
  --fast_model runtime/dorado/candidates/dorado-0.2.3-models/dna_r10.4.1_e8.2_5khz_400bps_fast@v4.2.alpha \
  --hac_model runtime/dorado/candidates/dorado-0.2.3-models/dna_r10.4.1_e8.2_5khz_400bps_hac@v4.2.alpha \
  --sup_model runtime/dorado/candidates/dorado-0.2.3-models/dna_r10.4.1_e8.2_5khz_400bps_sup@v4.2.alpha \
  [normal run arguments]
```

The legacy policy requires the audited `0.2.3+4ed609d` basecaller and
`0.7.0+71cc7442` summary reader. Because this candidate does not yet have a
committed immutable release manifest, the runtime contract hashes the selected
binary, complete model directories, summary binary, compatibility helper,
device, and effective arguments and records the selection as unqualified.
Do not resume a 0.7.0 state with this configuration. Promotion still requires
representative marker-length FAST/HAC/SUP output, classification equivalence,
and sustained Metal throughput/latency evidence on the intended hardware.

### Install the audited macOS ARM64 baseline

Download the official archive without extracting it:

```bash
curl -L \
  -o /path/to/dorado-0.7.0-osx-arm64.zip \
  https://cdn.oxfordnanoportal.com/software/analysis/dorado-0.7.0-osx-arm64.zip
```

Prepare a model-source directory containing exactly the configured FAST, HAC,
and SUP model directories. An existing audited installation may be used as the
source; otherwise use the downloaded Dorado binary's `download` subcommand.
The installer verifies every model tensor and configuration byte against the
committed release manifest.

```bash
perl bin/install_dorado_release.pl \
  --archive /path/to/dorado-0.7.0-osx-arm64.zip \
  --model-source-dir /path/to/dorado-models \
  --manifest conf/runtime_compatibility/dorado_release_0.7.0_osx-arm64.tsv \
  --destination runtime/dorado/releases/dorado-0.7.0-osx-arm64
```

Re-running the installer verifies an exact existing release and never replaces
it. A different version or platform must use a different destination.

The Linux x86-64 baseline must be installed from its own checksummed platform
manifest once that manifest and live CUDA qualification are available. CPU
evidence may diagnose command and format compatibility but cannot qualify the
release. A macOS binary must never be copied into a Linux release.

### Static and live compatibility checks

Static verification checks installed bytes, platform, version, model
completeness, and every Dorado command-line option used by RTBioScan:

```bash
bin/validate_dorado_release.sh \
  --manifest conf/runtime_compatibility/dorado_release_0.7.0_osx-arm64.tsv \
  --release-dir runtime/dorado/releases/dorado-0.7.0-osx-arm64
```

Live qualification uses a checksummed official Dorado v0.7.0 R10.4.1 E8.2
400 bps POD5 fixture and the actual hardware backend:

```bash
curl -L \
  -o /path/to/dorado-v0.7.0-qualification.pod5 \
  https://raw.githubusercontent.com/nanoporetech/dorado/v0.7.0/tests/data/pod5/dna_r10.4.1_e8.2_400bps_5khz/dna_r10.4.1_e8.2_400bps_5khz-FLO_PRO114M-SQK_LSK114_XL-5000.pod5
```

The validator checks this file against the committed fixture manifest. It runs
FAST, HAC, and SUP with the production chunk, batch, overlap, quality, and
read-list arguments; validates SAM and `dorado summary` output; rejects
reported device fallback; and writes a read-only evidence report outside the
release directory. Because this fixture contains one short read, a
production-threshold HAC or SUP result may contain no reads. In that case the
validator performs a separate quality-zero format probe; it does not change
the production threshold or qualify biological classification behavior.

If live qualification fails after the fixture has been verified, the requested
report is written with `status=failed` and a read-only sibling directory named
`<report>.evidence` preserves the completed stage logs, effective command
arguments, checksums, and OS/hardware metadata. The primary bundle includes its
own `failure-report.tsv` and `checksums.sha256`; the requested report is a hard
link to that bundled report. A per-report lock prevents concurrent attempts
from publishing to the same path. Use a new report path for every attempt;
previous success and failure evidence is never overwritten.

Handled HUP, INT, and TERM interruptions preserve a failed attempt and release
its lock. SIGKILL, host failure, or power loss cannot be trapped and may leave
an incomplete `<report>.lockdir` or temporary bundle. Inspect those artifacts
and confirm that no validator process still owns the recorded PID before
removing a stale lock; never delete it merely to bypass an active attempt.

```bash
bin/validate_dorado_release.sh \
  --manifest conf/runtime_compatibility/dorado_release_0.7.0_osx-arm64.tsv \
  --release-dir runtime/dorado/releases/dorado-0.7.0-osx-arm64 \
  --qualification-pod5 /path/to/dorado-v0.7.0-qualification.pod5 \
  --qualification-manifest conf/runtime_compatibility/dorado_qualification_fixture_v0.7.0.tsv \
  --device metal \
  --report runtime/dorado/qualification/dorado-0.7.0-osx-arm64-metal.tsv
```

This is a hardware and interface qualification, not an end-to-end RTBioScan
classification fixture. Promotion still requires a representative full-round
shadow run with the candidate binary and models on the intended Metal or CUDA
device. Before the comparison, define the deployment's minimum reads/second
and bases/second, maximum p95 round latency, maximum total basecalling-time
ratio relative to the stable release, and required headroom over the observed
sequencer input rate. Measure FAST, HAC, and SUP on the same hardware, fixture,
models, and effective arguments for the candidate and stable release. Reject
the candidate if the accelerator fails, falls back to CPU, or misses any
predeclared real-time performance budget. A passing CPU run does not override
an accelerator failure.

Run candidates in a new output directory and with a new `state_id`. Never
resume or migrate the stable state into a candidate run.

### Explicit candidate selection

Installation and qualification still do not activate a release. Select it
explicitly only for a shadow run:

```bash
./RTBioScan.sh \
  --state_id DORADO_CANDIDATE_STATE \
  --outdir results_dorado_candidate \
  --state_dorado_release_manifest conf/runtime_compatibility/dorado_release_0.7.0_osx-arm64.tsv \
  --dorado_bin runtime/dorado/releases/dorado-0.7.0-osx-arm64/bin/dorado \
  --fast_model runtime/dorado/releases/dorado-0.7.0-osx-arm64/models/dna_r10.4.1_e8.2_400bps_fast@v5.0.0 \
  --hac_model runtime/dorado/releases/dorado-0.7.0-osx-arm64/models/dna_r10.4.1_e8.2_400bps_hac@v5.0.0 \
  --sup_model runtime/dorado/releases/dorado-0.7.0-osx-arm64/models/dna_r10.4.1_e8.2_400bps_sup@v4.3.0 \
  [normal run arguments]
```

Rollback means selecting the retained stable release and its matching,
untouched state. Installed releases are never overwritten or deleted by the
installer. Do not replace or remove the stable release until the candidate has
passed accelerator compatibility and representative performance qualification
and has completed an operational shadow period.

The optional `state_dorado_release_manifest` binds the complete qualified
release declaration into the schema-v2 state identity and verifies that the
selected binary and model configurations belong to it. Omitting the parameter
keeps an existing explicit Dorado installation usable and fingerprints all
selected model bytes directly, but records it as unqualified and does not
substitute for static/live release qualification.

---

## 7. BLAST databases
[back to Top](#rtbioscan-installation)

The BLAST databases are **not included in the repository** due to their size. They must be placed in the `db/` directory before running the pipeline.

For the conceptual role of marker-specific barcoding databases and taxonomy resources, see [Concepts](concepts.md#taxonomy-and-reporting-aids). For parameter behavior and examples, see [Database parameters](usage.md#database-parameters).

The pipeline expects, relative to the RTBioScan root:

| Parameter | Default path | Purpose |
|---|---|---|
| `blast_db_specs` | `db/COInr98_2024Jun_RioNegro_Brazil\|db/ITS2nr98_2024Jun_RioNegro_Brazil` | Per-marker taxonomy assignment (pipe-separated, same order as `--targets`) |
| `blast_filter_db` | `db/toDefault/targets_All_tagged_nr95` | Kingdom pre-filter (LAST) |
| `blast_taxdb` | `db/taxdb` | BLAST taxid lookup database |
| `state_taxonomy_data_dir` | `db/taxonomy/releases/ncbi-taxdump-2024-06-24` | Pinned TaxonKit lineage database |

Auxiliary files also expected in `db/`:

- `COInr_2024Jun_metazoa_memtax1.txt` — in-memory taxonomy for fast COI lookup
- `ITS2nr_2024Jun_viridiplantae_memtax2.txt` — in-memory taxonomy for fast ITS2 lookup
- `DBnr_2024Jun_id2lineage.txt` — sequence ID to lineage mapping

The prepared runtime release includes the pinned TaxonKit directory. For a
source checkout, install it from the matching June-2024 `taxdump.tar.gz`:

```bash
perl bin/install_taxonomy_release.pl \
  --archive /path/to/taxdump.tar.gz \
  --manifest conf/state_compatibility/taxonomy_release_ncbi_2024-06-24.tsv \
  --destination db/taxonomy/releases/ncbi-taxdump-2024-06-24
```

The installer verifies the archive and all four extracted files, installs the
release atomically, and refuses to overwrite a mismatched existing directory.
The pipeline does not fall back to `~/.taxonkit`.

**To use your own databases**, update the corresponding parameters in `nextflow.config` or pass them as command-line arguments:

```bash
nextflow run main.nf \
  --blast_db_specs "db/my_COI_database|db/my_ITS2_database" \
  ...
```

BLAST+ databases must be pre-formatted with `makeblastdb`. The kingdom pre-filter database uses LAST format (built with `lastdb`).

---

## 8. Verify the installation
[back to Top](#rtbioscan-installation)

Run the syntax check and optional unit tests to confirm the environment is correct:

```bash
cd /path/to/RTBioScan

# Check all shell scripts parse correctly
bash -n bin/Consensus_simple.sh && echo "Shell syntax OK"

# Run the full test suite (optional; requires pytest)
pip install pytest --user   # or: conda install pytest
python3 -m pytest tests/ -q
# Some tests are skipped automatically when optional developer tools such as shellcheck are absent.
```

A quick tool availability check:

```bash
for tool in blastn cd-hit-est vsearch cutadapt seqtk seqkit samtools lastal perl python3; do
  command -v "$tool" >/dev/null 2>&1 \
    && echo "OK  $tool ($(command -v $tool))" \
    || echo "MISSING  $tool"
done

# pod5 is required for RTBioScan.sh --feeder / --do_metadata
command -v pod5 >/dev/null 2>&1 \
  && echo "OK  pod5 ($(command -v pod5))" \
  || echo "MISSING  pod5"

# Dorado release (adjust platform manifest/release path when qualified)
bin/validate_dorado_release.sh \
  --manifest conf/runtime_compatibility/dorado_release_0.7.0_osx-arm64.tsv \
  --release-dir runtime/dorado/releases/dorado-0.7.0-osx-arm64

# R packages
Rscript -e 'library(Biostrings); library(DECIPHER); library(ggplot2); cat("R packages OK\n")'

# Nextflow version
nextflow -version
```

---

> **Unsupported container profiles:** `-profile docker` and
> `-profile singularity` are retained only as explanatory erroring stubs. They
> fail before any pipeline work starts. Do not resume rolling state previously
> produced with the inherited NanoRTax image; start a new state and reanalyse
> with a provisioned RTBioScan runtime.
