# RTBioScan: Installation

> **Docs:** [Index](README.md) · [**Installation**](installation.md) · [Usage](usage.md) · [Output](output.md) · [Report Schema](report_schema.md) · [Pipeline Overview](pipeline.md)

## Table of contents
[back to Top](#rtbioscan-installation)

* [Overview](#overview)
* [Requirements at a glance](#requirements-at-a-glance)
* [1. Nextflow](#1-nextflow)
* [2. System tools](#2-system-tools)
  * [macOS (Apple Silicon — Homebrew recommended)](#macos-apple-silicon--homebrew-recommended)
  * [Linux](#linux)
* [3. R and Bioconductor packages](#3-r-and-bioconductor-packages)
* [4. Python](#4-python)
* [5. Perl modules](#5-perl-modules)
* [6. Dorado basecaller](#6-dorado-basecaller)
  * [macOS Apple Silicon](#macos-apple-silicon)
  * [Linux x86-64 (CUDA GPU)](#linux-x86-64-cuda-gpu)
  * [Linux x86-64 (CPU only)](#linux-x86-64-cpu-only)
  * [Dorado basecalling models](#dorado-basecalling-models)
  * [Configuring the device](#configuring-the-device)
* [7. BLAST databases](#7-blast-databases)
* [8. Verify the installation](#8-verify-the-installation)
* [Alternative: Docker (no local dependencies)](#alternative-docker-no-local-dependencies)

---

## Overview
[back to Top](#rtbioscan-installation)

RTBioScan is a Nextflow DSL1 pipeline for real-time ONT metabarcoding. It requires:

- **Nextflow** 22.x as the workflow engine
- A set of **bioinformatics command-line tools** (BLAST+, cd-hit, vsearch, cutadapt, seqtk, seqkit, samtools, LAST)
- The **`pod5` CLI** for the wrapper/feeder workflow (`RTBioScan.sh --feeder` / `--do_metadata`)
- **R** with Bioconductor packages for consensus generation and report visualisation
- **Python 3** for report rendering, the local report server, and Python-packaged CLIs
- **Dorado** (Oxford Nanopore basecaller) bundled inside `bin/dorado/bin/`
- **BLAST databases** distributed separately from the code

**On macOS (Apple Silicon), Homebrew is the recommended package manager.** It installs native arm64 binaries, requires no environment activation, and covers all the CLI tools this pipeline needs. Conda is the better choice when deploying on Linux or sharing a reproducible environment across platforms. A Docker image (`hecrp/nanortax:latest`) is also available and requires only Docker and Nextflow.

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
| BLAST+ | ≥ 2.12 | `blastn` must be in `PATH` |
| cd-hit | ≥ 4.8 | `cd-hit-est` must be in `PATH` |
| vsearch | ≥ 2.21 | `vsearch` must be in `PATH` |
| cutadapt | ≥ 4.0 | Required for barcode / primer demultiplexing |
| seqtk | ≥ 1.3 | `seqtk` must be in `PATH` |
| seqkit | ≥ 2.4 | `seqkit` must be in `PATH` |
| samtools | ≥ 1.16 | `samtools` must be in `PATH` |
| LAST | ≥ 1400 | `lastal` must be in `PATH` |
| pod5 | ≥ 0.2 | Required for `RTBioScan.sh --feeder` / `--do_metadata`; optional for direct pre-sliced POD5 runs |
| taxonkit | any | Optional; used for full lineage retrieval |
| Dorado | 0.7.x (arm64 / x86-64) | Bundled in `bin/dorado/bin/dorado` |
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

### macOS (Apple Silicon — Homebrew recommended)
[back to Top](#rtbioscan-installation)

Homebrew installs native arm64 binaries with no environment activation step, which is simpler and faster than Conda on macOS. Install [Homebrew](https://brew.sh) first, then:

```bash
# All core tools in one command
brew install blast cd-hit vsearch seqtk seqkit samtools

# LAST aligner (brewsci/bio tap)
brew tap brewsci/bio
brew install last

# Optional helper
brew install taxonkit     # Full lineage retrieval
```

Install the Python-packaged CLIs (`cutadapt` and `pod5`) in [§4 Python](#4-python) after Python itself is available.

> **Note:** Homebrew installs to `/opt/homebrew/bin/` on Apple Silicon. This is added to your `PATH` automatically after `brew shellenv` runs from your shell profile (usually already done by the Homebrew installer).

> **Conda on macOS:** If you prefer Conda or need the same environment on both macOS and Linux, the Linux Conda instructions below work on macOS too. Use [Miniforge](https://github.com/conda-forge/miniforge) (arm64 native) rather than the standard Anaconda installer to avoid Rosetta issues.

### Linux
[back to Top](#rtbioscan-installation)

Conda is the most reproducible approach on Linux:

```bash
conda create -n rtbioscan -c bioconda -c conda-forge \
  blast=2.14 \
  cd-hit=4.8.1 \
  vsearch=2.22.1 \
  cutadapt=4.6 \
  seqtk=1.3 \
  seqkit=2.6.1 \
  samtools=1.18 \
  last=1454 \
  taxonkit=0.15 \
  pod5

conda activate rtbioscan
```

Or with `apt` on Debian/Ubuntu (versions in the repositories may be older):

```bash
sudo apt-get install -y \
  ncbi-blast+ \
  cd-hit \
  vsearch \
  cutadapt \
  seqtk \
  samtools

# seqkit, LAST, and pod5 are not typically available in usable repo versions; install via conda
# or use §4 to install pod5 with pip:
conda install -c bioconda seqkit last
```

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

Dorado is the Oxford Nanopore basecaller. The pipeline expects the binary at:

```
bin/dorado/bin/dorado
```

relative to the RTBioScan root directory. The basecalling models go in the same directory.

### macOS Apple Silicon
[back to Top](#rtbioscan-installation)

```bash
cd /path/to/RTBioScan

# Download the macOS arm64 build (adjust version tag as needed)
curl -L https://cdn.oxfordnanoportal.com/software/analysis/dorado-0.7.3-osx-arm64.tar.gz \
  | tar -xz

# Move to the expected location
mkdir -p bin/dorado
mv dorado-0.7.3-osx-arm64/* bin/dorado/

# Verify
bin/dorado/bin/dorado --version
```

### Linux x86-64 (CUDA GPU)
[back to Top](#rtbioscan-installation)

```bash
cd /path/to/RTBioScan

curl -L https://cdn.oxfordnanoportal.com/software/analysis/dorado-0.7.3-linux-x64.tar.gz \
  | tar -xz

mkdir -p bin/dorado
mv dorado-0.7.3-linux-x64/* bin/dorado/

bin/dorado/bin/dorado --version
```

### Linux x86-64 (CPU only)
[back to Top](#rtbioscan-installation)

The same binary supports CPU basecalling. Set `dorado_device = "cpu"` in `nextflow.config` (see below).

### Dorado basecalling models
[back to Top](#rtbioscan-installation)

The three models used by the pipeline are already bundled in `bin/dorado/bin/` when you follow the steps above:

| Model | Path in `bin/dorado/bin/` | Used for |
|---|---|---|
| FAST v5.0.0 | `dna_r10.4.1_e8.2_400bps_fast@v5.0.0` | First-pass basecalling |
| HAC v5.0.0 | `dna_r10.4.1_e8.2_400bps_hac@v5.0.0` | High-accuracy pass |
| SUP v4.3.0 | `dna_r10.4.1_e8.2_400bps_sup@v4.3.0` | Super-accuracy consensus |

If the models are not bundled in the Dorado tarball you downloaded, download them explicitly:

```bash
# Download models into the Dorado binary directory
bin/dorado/bin/dorado download --model dna_r10.4.1_e8.2_400bps_fast@v5.0.0 \
  --directory bin/dorado/bin/

bin/dorado/bin/dorado download --model dna_r10.4.1_e8.2_400bps_hac@v5.0.0 \
  --directory bin/dorado/bin/

bin/dorado/bin/dorado download --model dna_r10.4.1_e8.2_400bps_sup@v4.3.0 \
  --directory bin/dorado/bin/
```

The model paths in `nextflow.config` are already set to match this layout:

```groovy
fast_model = "bin/dorado/bin/dna_r10.4.1_e8.2_400bps_fast@v5.0.0"
hac_model  = "bin/dorado/bin/dna_r10.4.1_e8.2_400bps_hac@v5.0.0"
sup_model  = "bin/dorado/bin/dna_r10.4.1_e8.2_400bps_sup@v4.3.0"
```

### Configuring the device
[back to Top](#rtbioscan-installation)

Edit `nextflow.config` to set the hardware accelerator:

```groovy
// Apple Silicon GPU (default)
dorado_device = "metal"

// NVIDIA GPU (CUDA)
dorado_device = "cuda:0"

// CPU only (any platform, slower)
dorado_device = "cpu"
```

---

## 7. BLAST databases
[back to Top](#rtbioscan-installation)

The BLAST databases are **not included in the repository** due to their size. They must be placed in the `db/` directory before running the pipeline.

The pipeline expects, relative to the RTBioScan root:

| Parameter | Default path | Purpose |
|---|---|---|
| `blast_db_specs` | `db/COInr98_2024Jun_RioNegro_Brazil\|db/ITS2nr98_2024Jun_RioNegro_Brazil` | Per-marker taxonomy assignment (pipe-separated, same order as `--targets`) |
| `blast_filter_db` | `db/toDefault/targets_All_tagged_nr95` | Kingdom pre-filter (LAST) |
| `blast_taxdb` | `db/taxdb` | NCBI taxonomy (taxonkit) |

Auxiliary files also expected in `db/`:

- `COInr_2024Jun_metazoa_memtax1.txt` — in-memory taxonomy for fast COI lookup
- `ITS2nr_2024Jun_viridiplantae_memtax2.txt` — in-memory taxonomy for fast ITS2 lookup
- `DBnr_2024Jun_id2lineage.txt` — sequence ID to lineage mapping

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

# Dorado binary
[ -x bin/dorado/bin/dorado ] \
  && echo "OK  dorado $(bin/dorado/bin/dorado --version 2>&1 | head -1)" \
  || echo "MISSING  bin/dorado/bin/dorado"

# R packages
Rscript -e 'library(Biostrings); library(DECIPHER); library(ggplot2); cat("R packages OK\n")'

# Nextflow version
nextflow -version
```

---

## Alternative: Docker (no local dependencies)
[back to Top](#rtbioscan-installation)

If you prefer not to install tools natively for the Nextflow tasks themselves, Docker requires only **Docker Desktop** and **Nextflow**.

```bash
# Pull the container image
docker pull hecrp/nanortax:latest

# Run the pipeline with the docker profile
nextflow run main.nf \
  --reads "pod5/reads_rt_round_pod5/*pod5" \
  -profile docker

# Or with the voucher profile
nextflow run main.nf \
  --reads "pod5/reads_rt_round_pod5/*pod5" \
  -profile voucher,docker
```

The Docker image (`hecrp/nanortax:latest`) bundles the pipeline-side bioinformatics tools except Dorado and the BLAST databases. You still need to:

1. Place the Dorado binary and models in `bin/dorado/bin/` (see §6).
2. Place or symlink the BLAST databases in `db/` (see §7).

If you launch via `RTBioScan.sh --feeder` or `--do_metadata` while using `-profile docker`, install `pod5` on the host as well. Those wrapper-side scripts run outside the container. Likewise, `--serve` requires a host `python3`.

> **macOS note:** Docker on macOS runs inside a Linux VM. The `dorado_device = "metal"` option does not work inside Docker — set `dorado_device = "cpu"` when using Docker on a Mac.
