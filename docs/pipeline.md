# RTBioScan: Pipeline Overview

> **Docs:** [Index](README.md) · [Installation](installation.md) · [Usage](usage.md) · [Output](output.md) · [Report Schema](report_schema.md) · **Pipeline Overview**

## What RTBioScan does
[back to Top](#rtbioscan-pipeline-overview)

RTBioScan is a real-time ONT amplicon metabarcoding pipeline. It is designed to turn incoming POD5 data into:

- demultiplexed read summaries
- OTU-level taxonomic assignments
- consensus sequences
- live HTML reports that update as sequencing progresses

The pipeline is intended for situations where you need usable results during or soon after sequencing, not only after a long offline analysis.

## How a run progresses
[back to Top](#rtbioscan-pipeline-overview)

At a high level, each run follows the same user-visible flow:

1. Prepare dependencies, reference databases, and input metadata.
2. Start the run in real-time mode or batch mode.
3. RTBioScan processes data in repeated rounds as new reads become available.
4. Each round updates the report, summary tables, and consensus outputs.
5. The run can be resumed later if sequencing or processing is interrupted.

Public workflow figure:

![RTBioScan public workflow overview](assets/pipeline_overview_public.svg)

The core classification path is driven by marker-specific reference databases and taxonomy tables; profile-specific highlight lists are optional reporting aids, not core taxonomic inputs.

Simple workflow view:

```text
POD5 reads
  -> FAST pre-filter / target read selection
  -> HAC basecalling
  -> demultiplexing and HQ filtering
  -> OTU definition
  -> OTU taxonomic assignment
  -> consensus generation
  -> live reports and exported result files
```

## Real-time and batch use
[back to Top](#rtbioscan-pipeline-overview)

RTBioScan supports two main operating styles:

- **Real-time mode**: new POD5 files are discovered and processed during sequencing. This is the recommended mode when you want live progress and live reports.
- **Batch mode**: an existing set of POD5 files is processed as a completed dataset.

See [usage.md](usage.md) for launcher examples and the required inputs for each mode.

## Main inputs
[back to Top](#rtbioscan-pipeline-overview)

Most runs need:

- POD5 input data
- marker-specific reference databases
- metadata describing the run and samples
- barcode and/or primer FASTA files when demultiplexing is enabled

Installation details are in [installation.md](installation.md). Exact file formats are documented in [usage.md](usage.md#input-file-formats).

## Main outputs
[back to Top](#rtbioscan-pipeline-overview)

The outputs most users interact with are:

- the live HTML report
- per-round summary tables
- consensus FASTA files
- machine-readable JSON summaries

Output locations and file descriptions are documented in [output.md](output.md). The public JSON contract is described in [report_schema.md](report_schema.md).

## Profiles and common use cases
[back to Top](#rtbioscan-pipeline-overview)

The shipped profiles tune RTBioScan for different use cases:

- default and `barcoding`: multi-sample metabarcoding runs
- `voucher`: reference-sequence generation from known material
- `xprize`: runs that use additional reporting helper tables

Choose the profile in [usage.md](usage.md#-profile), then make sure the corresponding databases and optional support files are installed.

## What this pipeline is not for
[back to Top](#rtbioscan-pipeline-overview)

RTBioScan is not intended to replace slower, publication-focused downstream analysis. It is best used for:

- rapid biodiversity assessment
- run monitoring during sequencing
- early detection of taxa of interest
- producing practical consensus outputs from amplicon data

It is not intended for whole-genome assembly, unrestricted discovery without a reference database, or the final analytical pass of an unlimited-compute workflow.
