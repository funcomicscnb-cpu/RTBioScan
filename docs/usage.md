# RTBioScan: Usage

> **Docs:** [Index](README.md) · [Installation](installation.md) · **Usage** · [Output](output.md) · [Report Schema](report_schema.md) · [Pipeline Design](pipeline.md) · [Performance Backlog](performance_pr_backlog.md)

## Table of contents
[back to Top](#rtbioscan-usage)

* [Table of contents](#table-of-contents)
* [Introduction](#introduction)
* [Quick start with RTBioScan.sh](#quick-start-with-rtbioscansh)
  * [Common invocations](#common-invocations)
  * [RTBioScan.sh options reference](#rtbioscansh-options-reference)
* [Real-time workflow: setup and launch](#real-time-workflow-setup-and-launch)
  * [Directory structure](#directory-structure)
  * [Direct feeder invocation (advanced)](#direct-feeder-invocation-advanced)
  * [Batch mode (no feeder)](#batch-mode-no-feeder)
* [Voucher workflow: reference sequence generation](#voucher-workflow-reference-sequence-generation)
  * [What the voucher workflow does](#what-the-voucher-workflow-does)
  * [Running the voucher workflow](#running-the-voucher-workflow)
  * [Exporting sequences for database submission](#exporting-sequences-for-database-submission)
* [Running the pipeline](#running-the-pipeline)
* [Main arguments](#main-arguments)
  * [`-name`](#-name)
  * [`-profile`](#-profile)
  * [`--run_id`](#--run_id)
  * [`--reads`](#--reads)
  * [`--reads_rt`](#--reads_rt)
* [Database parameters](#database-parameters)
  * [`--blast_db_specs`](#--blast_db_specs)
  * [`--blast_filter_db`](#--blast_filter_db)
  * [`--blast_taxdb`](#--blast_taxdb)
  * [`--blast_evalue`](#--blast_evalue)
  * [`--blast_max_hsps`](#--blast_max_hsps)
  * [`--blast_id_family`](#--blast_id_family)
  * [`--blast_id_genus`](#--blast_id_genus)
  * [`--blast_id_spec`](#--blast_id_spec)
  * [`--nonncbi_memtax`](#--nonncbi_memtax)
  * [`--nonncbi_id2lineage_target`](#--nonncbi_id2lineage_target)
* [Other command line parameters](#other-command-line-parameters)
  * [General settings](#general-settings)
  * [Basecalling and read filtering](#basecalling-and-read-filtering)
  * [OTU definition (`OTU_definition`)](#otu-definition-otu_definition)
  * [BLAST (`blast_OTU_pretax`)](#blast-blast_otu_pretax)
  * [Consensus (`consensus`)](#consensus-consensus)
  * [Process concurrency](#process-concurrency)
  * [HTML report (`getting_run_summary`)](#html-report-getting_run_summary)
  * [OTU membership exports (`_reporting_OTU_definition`)](#otu-membership-exports-_reporting_otu_definition)


## Introduction
[back to Top](#rtbioscan-usage)

Nextflow handles job submissions on SLURM or other environments, and supervises running the jobs. Thus the Nextflow process must run until the pipeline is finished. We recommend that you put the process running in the background through `screen` / `tmux` or similar tool. Alternatively, you can run nextflow within a cluster job submitted in your job scheduler.

It is recommended to limit the Nextflow Java virtual machines memory. We recommend adding the following line to your environment (typically in `~/.bashrc` or `~./bash_profile`):

```bash
NXF_OPTS='-Xms1g -Xmx4g'
```

## Quick start with RTBioScan.sh
[back to Top](#rtbioscan-usage)

`RTBioScan.sh` (in the repository root) is the recommended way to launch the pipeline. It coordinates the POD5 feeder, the Nextflow pipeline, and the HTTP report server in a single command, with a shared lifecycle: all background processes are started before the pipeline and automatically stopped when the pipeline exits or when you press Ctrl-C.

### Common invocations
[back to Top](#rtbioscan-usage)

**Pipeline only** — POD5 chunks already present in `reads_rt_round_pod5/`:
```bash
./RTBioScan.sh --run_id MY_RUN -profile docker -resume
```

**Pipeline + live report in browser** — resume with auto-refreshing report:
```bash
./RTBioScan.sh --run_id MY_RUN --serve --serve-open -profile docker -resume
```

**First-time real-time run** — metadata setup + feeder + pipeline + report:
```bash
./RTBioScan.sh \
  --feeder --do_metadata \
  --run_id MY_RUN_ID \
  --input_folder /path/to/minknow/output \
  --metadata   /path/to/Pipeline_Information.tsv \
  --general_fasta /path/to/demult_general.fasta \
  --primers_fasta /path/to/demult_primers.fasta \
  --serve --serve-open \
  -profile docker
```

**Subsequent real-time rounds** — feeder + pipeline (metadata already set up):
```bash
./RTBioScan.sh \
  --feeder \
  --run_id MY_RUN_ID \
  --input_folder /path/to/minknow/output \
  --serve --serve-open \
  -profile docker -resume
```

**Batch mode** — no feeder, process all POD5s and exit:
```bash
./RTBioScan.sh \
  --serve --serve-open \
  -profile docker --run_mode batch \
  --reads "/path/to/pod5/chunks/*.pod5"
```

### RTBioScan.sh options reference
[back to Top](#rtbioscan-usage)

**Feeder options:**

| Option | Default | Description |
|---|---|---|
| `--feeder` | off | Enable the POD5 feeder alongside the pipeline. |
| `--run_id <id>` | required with `--feeder` / `--do_metadata`; optional otherwise | Run ID — used to filter the metadata TSV and name POD5 chunks. Automatically sets the Nextflow `-name` to the same value unless `-name` is passed explicitly. |
| `--input_folder <dir>` | required with `--feeder` (unless `--skip_pod5`) | MinKNOW output folder, searched recursively for `.pod5` files. |
| `--num_reads <n>` | `200000` | Reads per POD5 chunk. |
| `--sleep_time <s>` | `300` | Feeder poll interval in seconds. |
| `--metadata <file>` | `~/Tumbira_Final/Metadata/Pipeline_Information.tsv` | Metadata TSV. |
| `--general_fasta <file>` | `~/Tumbira_Final/Metadata/demult_general_with_Tucan.fasta` | Master demultiplexing FASTA. |
| `--primers_fasta <file>` | `~/Tumbira_Final/Metadata/demult_primers.fasta` | Primers FASTA. |
| `--do_metadata` | off | Set up `results/sample_info/{run_id}/` before starting. Runs synchronously (completes before the pipeline starts). Can be used without `--feeder`. |
| `--skip_pod5` | off | Skip the POD5 splitting loop in the feeder (metadata setup only). |
| `--delete-full-pod5` | off | Delete each file from `full_pod5/` as soon as all its reads have been sliced into round chunks. See [Disk management](#disk-management-pruning-pod5-files-during-a-run). |
| `--delete_input_pod5` | off | Delete the processed intake POD5 from `reads_rt_round_pod5/` immediately after each round completes, instead of archiving it to `done_round_pod5/`. Shorthand for passing `--delete_input_pod5 true` to Nextflow. See [Disk management](#disk-management-pruning-pod5-files-during-a-run). |
| `--delete_from_ori_dir` | off | Delete the source POD5 from `ori_round_pod5/` after it has been staged into the intake queue. Shorthand for passing `--delete_from_ori_dir true` to Nextflow. See [Disk management](#disk-management-pruning-pod5-files-during-a-run). |

**Report server options:**

| Option | Default | Description |
|---|---|---|
| `--serve` | off | Start the HTTP report server alongside the pipeline. |
| `--serve-port <n>` | `8000` | Port for the HTTP server. |
| `--serve-host <addr>` | `127.0.0.1` | Bind address. |
| `--serve-open` | off | Open `report.html` in the default browser on start. |
| `--serve-open-all` | off | Open all per-run reports in the browser on start. |
| `--serve-open-last <n>` | — | Open the last N per-run reports in the browser on start. |
| `--serve-quiet` | off | Suppress HTTP request logging. |
| `--serve-dir <dir>` | same as `--outdir`, or `results/` | Directory to serve. |

**Cleanup options:**

| Option | Default | Description |
|---|---|---|
| `--clean-ref <dir>` | off | Reference launch directory for cleanup discovery. `--clean-all` and `--clean-temp-all` operate on this directory when set; run-specific cleanup searches it first. |
| `--clean <run_id>` | off | Remove artifacts for one run from the launch directory that owns that run. Discovery order is `--clean-ref`, current working directory, then the pipeline directory, using `.nextflow/history` first and artifact presence second. |
| `--clean-all` | off | Remove all pipeline run artifacts under the current working directory by default, or under `--clean-ref <dir>` when provided. |
| `--clean-temp <run_id>` | off | Remove only temporary artifacts for one run from `results/temp/current/state`, `results/temp/ongoing/state`, `results/ongoing/state`, and that run's temp files in `work/` via `nextflow clean -k`, using the same root-discovery order as `--clean`. |
| `--clean-temp-all` | off | Remove only temporary artifacts for all runs from the current working directory by default, or from `--clean-ref <dir>` when provided, plus temp files in `work/` via `nextflow clean -k`. |
| `--dry-run` | off | With any cleanup option, print planned deletions without removing anything. |
| `-y`, `--yes` | off | Skip the interactive confirmation prompt during cleanup. |

All other arguments are forwarded verbatim to `nextflow run main.nf`.

---

## Real-time workflow: setup and launch
[back to Top](#rtbioscan-usage)

RTBioScan processes ONT reads as they arrive from the sequencer. The recommended launcher is `RTBioScan.sh` (see [Quick start](#quick-start-with-rtbioscansh) above), which manages the feeder and pipeline together. The sections below describe the underlying components for users who need direct control.

### Directory structure
[back to Top](#rtbioscan-usage)

The feeder creates the following directory structure under the pipeline launch directory, which the pipeline then uses:

```
results/
  pod5/{run_id}/
    ori_round_pod5/         ← spool: feeder deposits ready chunks here
    reads_rt_round_pod5/    ← intake: feeder promotes one chunk at a time here
    full_pod5/              ← staging copy of every full POD5 from MinKNOW
    done_round_pod5/        ← processed rounds (moved here by the pipeline)
    metadata/               ← per-file view tables and progress counters
  sample_info/{run_id}/
    demult.fasta            ← barcode sequences for sample demultiplexing
    primers.fasta           ← primer sequences for no-adapter reads
    samples.txt             ← canonical 7-field compatibility projection
    {run_id}_metadata.txt   ← run-filtered metadata TSV rows
    replicate_roster.tsv    ← replicate-level roster, when present
    replicate_identity.tsv  ← collapse/track identity bridge, when present
```

### Disk management: pruning POD5 files during a run

[back to Top](#rtbioscan-usage)

Long sequencing runs generate substantial POD5 data. By default the pipeline retains every file so that rounds can be inspected or replayed after the run. Three options let you trade safety for storage by removing files as soon as they are no longer needed.

#### How POD5 files accumulate

A single raw POD5 from MinKNOW passes through four directories before it is fully consumed:

```
MinKNOW output folder  (input_folder, never touched by RTBioScan)
       │  cp -p  (feeder: on first discovery)
       ▼
full_pod5/             ← staging copy; sliced repeatedly until all reads are consumed
       │  pod5 filter  (feeder: once per round, num_reads reads at a time)
       ▼
ori_round_pod5/        ← sized round chunk waiting to be promoted
       │  mv / ln -s   (feeder: when the intake queue is empty)
       ▼
reads_rt_round_pod5/   ← active intake for the Nextflow pipeline
       │  backup_update_and_clean  (pipeline: after the round is fully processed)
       ▼
done_round_pod5/       ← archive of processed rounds (default final destination)
```

Without any pruning option, all four directories accumulate files for the entire run. On a multi-day run with 200 000-read chunks, this can reach tens to hundreds of gigabytes depending on the sequencer yield.

#### The three pruning options

| Option (RTBioScan.sh) | Nextflow param | Removes | When |
| --- | --- | --- | --- |
| `--delete-full-pod5` | *(feeder-only, not a Nextflow param)* | `full_pod5/<file>.pod5` | Immediately after every read in that file has been sliced into round chunks |
| `--delete_input_pod5` | `--delete_input_pod5 true` | `reads_rt_round_pod5/<file>.pod5` (or the `done_round_pod5/` copy) | After `backup_update_and_clean` completes for that round |
| `--delete_from_ori_dir` | `--delete_from_ori_dir true` | `ori_round_pod5/<file>.pod5` | When the feeder moves it from `ori_round_pod5/` into `reads_rt_round_pod5/` |

#### Consequences and irreversibility

> **Warning — all three options are irreversible.** Files removed during the run cannot be recovered unless you retain the original MinKNOW output folder. Do not enable these options if you may need to replay a round or inspect the raw signal data later.

**`--delete-full-pod5`** removes the staging copy from `full_pod5/` once every read in that file has been assigned to a round chunk. This is the largest category of data: a full POD5 from MinKNOW is typically several gigabytes, and `full_pod5/` retains all of them permanently by default. After deletion, the feeder writes a `metadata/deleted_<file>.flag` sentinel so the file is never re-imported from `input_folder` in subsequent loop iterations. Consequence: the original read-level signal is no longer accessible on the pipeline host; FAST5/POD5 replay is impossible unless `input_folder` is on a separate volume that you control.

**`--delete_input_pod5`** removes each round chunk from `reads_rt_round_pod5/` immediately after `backup_update_and_clean` finishes. In the default case (symlink-based feeder), `reads_rt_round_pod5/` holds only symlinks, so only the symlink is removed — the actual file in `ori_round_pod5/` is untouched. In copy-based setups, the actual chunk file is deleted. Either way, `done_round_pod5/` is skipped entirely and the chunk is not archived. The `done_pod5.txt` tracking file is still updated, so the feeder's deduplication logic continues to work correctly. Consequence: per-round POD5 chunks cannot be inspected after the run; re-running a specific round from its chunk requires keeping `ori_round_pod5/` or `full_pod5/`.

**`--delete_from_ori_dir`** removes the round chunk from `ori_round_pod5/` at the moment the feeder moves it into `reads_rt_round_pod5/`. This option is primarily useful when the feeder uses `mv` (destructive staging, enabled automatically when `delete_from_ori_dir = true`), draining the spool directory aggressively. Consequence: `ori_round_pod5/` is empty except for the one chunk currently in-flight; no spool copy exists for the round that has just started.

#### Recommended combinations

| Goal | Options to enable |
|---|---|
| Maximum disk savings, long run | `--delete-full-pod5 --delete_input_pod5 --delete_from_ori_dir` |
| Keep per-round chunks for post-run review, save raw-signal space | `--delete-full-pod5` only |
| Keep raw signal, save processed-round space | `--delete_input_pod5 --delete_from_ori_dir` |
| Default: retain everything | *(no delete options)* |

> The default `--prune_round_sequences true` Nextflow param (which removes large FASTA/FASTQ/SAM files from the Nextflow work directory after each round) is separate from all three POD5 options and is independent of them. It is recommended for all long runs regardless of which POD5 pruning strategy you choose.

#### Example: maximum disk savings

```bash
./RTBioScan.sh \
  --feeder \
  --run_id MY_RUN \
  --input_folder /data/minknow/MY_RUN \
  --delete-full-pod5 \
  --delete_input_pod5 \
  --delete_from_ori_dir \
  -profile barcoding
```

---

### Direct feeder invocation (advanced)
[back to Top](#rtbioscan-usage)

`bin/Metadata_pod5_processing.sh` handles both input file preparation and the POD5 feeder loop. When using `RTBioScan.sh --feeder`, this script is called automatically with the options you pass. Direct invocation is only needed when you require separate terminal control or custom orchestration:

```bash
bash bin/Metadata_pod5_processing.sh \
  --run_id     MY_RUN_ID \
  --input_folder /path/to/minknow/output \
  --metadata   /path/to/Pipeline_Information.tsv \
  --general_fasta /path/to/demult_general.fasta \
  --primers_fasta /path/to/demult_primers.fasta \
  --do_metadata \
  --num_reads  200000 \
  --sleep_time 300
```

Key options:

| Option | Default | Description |
|---|---|---|
| `--run_id` | required | Identifier used to filter the metadata TSV (must match column 6) and to name output POD5 chunks. |
| `--input_folder` | required | MinKNOW output folder. Searched recursively for `.pod5` files. |
| `--metadata` | `~/Tumbira_Final/Metadata/Pipeline_Information.tsv` | TSV with columns: `sample_name`, `barcode_id`, …, `barcode_part1`, `barcode_part2`, `run_id`. Rows are filtered by `run_id`. |
| `--general_fasta` | `~/Tumbira_Final/Metadata/demult_general_with_Tucan.fasta` | Master FASTA containing all barcode sequences (one entry per barcode in the kit library). |
| `--primers_fasta` | `~/Tumbira_Final/Metadata/demult_primers.fasta` | FASTA of primer sequences for the no-adapter second-pass demultiplexing. Copied directly to `results/sample_info/{run_id}/primers.fasta`. |
| `--do_metadata` | off | Enable input file creation. Skipped by default; safe to re-run (skips if `results/sample_info/{run_id}/` is already non-empty). |
| `--num_reads` | `200000` | Reads per POD5 chunk. Tune to balance round length against latency. |
| `--sleep_time` | `300` | Seconds between feeder loop iterations. |
| `--skip_pod5` | off | Skip the feeder loop; only create input files. Useful for preparing `results/sample_info/{run_id}/` before sequencing starts or in batch mode. |

> The default `--metadata`, `--general_fasta`, and `--primers_fasta` paths point to the XPrize/Tumbira deployment layout. Always pass explicit paths when running in other environments.

**What `--do_metadata` does:**

1. Filters `--general_fasta` to the barcodes used in this run (by `run_id`) and renames FASTA headers from barcode IDs to sample names → `results/sample_info/{run_id}/demult.fasta`.
   If one barcode key maps to multiple marker-specific linked adapters in the master FASTA, RTBioScan classifies them by primer content and emits one record per marker with a suffix such as `_COI` or `_ITS2`.
2. Copies `--primers_fasta` → `results/sample_info/{run_id}/primers.fasta`.
3. Writes the canonical 7-field compatibility projection → `results/sample_info/{run_id}/samples.txt`.
4. Writes `results/sample_info/{run_id}/{run_id}_metadata.txt` as the run-filtered metadata TSV rows.
5. Writes `results/sample_info/{run_id}/replicate_roster.tsv`, when present, and `results/sample_info/{run_id}/replicate_identity.tsv`, when present.
6. Validates that the output FASTA line count matches the number of emitted demultiplexing records.

**Input file formats:**

`results/sample_info/{run_id}/demult.fasta` is passed to cutadapt with `-e 0.1` (1st-pass demultiplexing). Each header must be the exact sample name as it will appear throughout the run, or a marker-qualified variant when metadata disambiguation emits multiple marker-specific records for the same barcode key:

```
>sample_name_1
ACGTACGTACGT...
>sample_name_2_COI
ACGTACGTACGT...
>sample_name_2_ITS2
ACGTACGTACGT...
```

`results/sample_info/{run_id}/primers.fasta` is passed to cutadapt with `-e 0.3 --discard-untrimmed` in a second pass applied only to reads that did not match any barcode (`no_adapter` reads). Reads that match a primer are retained and annotated with the primer name; all others are discarded.

If `demultiplex_mode = 'auto'` (the default), demultiplexing is enabled only when both FASTA files exist and are non-empty. Set `--demultiplex_mode on` to force the full barcode+primer workflow, `--demultiplex_mode primers_only` to trim directly against `primers.fasta` without requiring `demult.fasta` or metadata-derived sample names, or `--demultiplex_mode off` to disable demultiplexing entirely.

### Batch mode (no feeder)
[back to Top](#rtbioscan-usage)

To run on a pre-existing set of POD5 chunks without the feeder, use `RTBioScan.sh` without `--feeder`:

```bash
./RTBioScan.sh \
  -profile docker \
  --run_mode batch \
  --reads "/path/to/pod5/chunks/*.pod5"
```

In batch mode (`--watch false` is implied), the pipeline processes all matching files and exits when done. To prepare `results/sample_info/{run_id}/` first (metadata setup only, no feeder loop):

```bash
./RTBioScan.sh \
  --do_metadata \
  --run_id MY_RUN_ID \
  --metadata /path/to/Pipeline_Information.tsv \
  --general_fasta /path/to/demult_general.fasta \
  --primers_fasta /path/to/demult_primers.fasta \
  --skip_pod5 \
  -profile docker --run_mode batch \
  --reads "/path/to/pod5/chunks/*.pod5"
```

## Voucher workflow: reference sequence generation
[back to Top](#rtbioscan-usage)

### What the voucher workflow does
[back to Top](#rtbioscan-usage)

The voucher workflow produces a high-quality consensus sequence from a **single known individual**, intended for submission to a reference database (BOLD, GenBank, etc.) so that the species can later be identified when environmental samples are interrogated by the main metabarcoding pipeline.

This is conceptually different from the environmental metabarcoding workflow:

| | Environmental (`main.nf` default) | Voucher (`-profile voucher`) |
|---|---|---|
| **Input** | Pool of individuals from an environmental sample | Single known individual |
| **Goal** | Monitor species diversity of identifiable taxa | Generate a reference sequence |
| **OTU clustering** | 97% identity (diversity-oriented) | 99% identity (single-organism purity) |
| **BLAST role** | Primary — drives species identification | Secondary / suggestive only |
| **New species?** | Unexpected — unassigned clusters are typically pruned | Expected — the common use case |
| **Unassigned clusters** | Retained by default; often pruned in performance-focused runs | Always kept (`prune_unassigned_clusters = false`) |
| **Output for** | HTML reports, species lists | Clean FASTA for database submission |

The environmental workflow is optimised for **monitoring biodiversity of taxa that can be assigned to a known species, genus, or family** in the reference database. Unassigned clusters — reads that do not match any known sequence at the configured identity thresholds — accumulate in every run and are typically removed by enabling `--prune_unassigned_clusters true`. This keeps per-round runtimes bounded and the report focused on the taxa of interest.

BLAST assignment in the voucher mode is provided as a suggestion only: the most common scenario is a specimen from a species not yet in the reference database, so the absence of a BLAST match is informative rather than problematic.

> **Keeping unassigned clusters in the environmental workflow**
>
> By default (`--prune_unassigned_clusters false`) all clusters are retained regardless of BLAST assignment. This is appropriate for exploratory or discovery runs where unknown sequences are of interest, but it means the OTU pool grows unboundedly, which can substantially increase consensus and BLAST runtimes as the run progresses — especially for long multi-day runs with many samples.
>
> To retain all unassigned clusters explicitly:
> ```bash
> ./RTBioScan.sh --feeder --run_id MY_RUN --input_folder /data/pod5 \
>   --prune_unassigned_clusters false \
>   -profile test
> ```
> If you enable pruning and also want to prevent pruned reads from re-entering future rounds, add `--prune_unassigned_drop_reads true`. See [`--prune_unassigned_clusters`](usage.md#--prune_unassigned_clusters) and related parameters for the full set of controls.

### Running the voucher workflow
[back to Top](#rtbioscan-usage)

Prepare the `results/sample_info/{run_id}/` directory with the barcodes and primers for your specimen library (see [Direct feeder invocation](#direct-feeder-invocation-advanced)). Then run in batch mode using the `voucher` profile:

```bash
./RTBioScan.sh \
  --do_metadata --run_id MY_VOUCHER_RUN \
  --metadata /path/to/Pipeline_Information.tsv \
  --general_fasta /path/to/demult_general.fasta \
  --primers_fasta /path/to/demult_primers.fasta \
  --skip_pod5 \
  -name MY_VOUCHER_RUN -profile voucher \
  --reads "results/pod5/MY_VOUCHER_RUN/reads_rt_round_pod5/*.pod5"
```

Key differences vs. the default profile (all set automatically by `conf/voucher.config`):

| Parameter | Voucher value | Default / barcoding | Rationale |
|---|---|---|---|
| `watch` | `false` | `true` | Batch mode only; no real-time feeder needed |
| `otu_id` | 99 | 97 | Tighter clustering for single-organism purity |
| `consensus_reads_mode` | `cluster_total` | `representative` | Use all reads to maximise consensus depth |
| `consensus_max_reads` | 200 | 15 | Allow deeper coverage per OTU |
| `min_reads_sample` | 1 | 3 | Keep results even with low read counts |
| `prune_unassigned_clusters` | `false` | `false` | Never prune — new species have no BLAST match |
| `hq_quality_score` | 20 | 15 | Higher Q-score threshold for reference-grade basecalls |

### Exporting sequences for database submission
[back to Top](#rtbioscan-usage)

After the pipeline completes, run `bin/voucher_export.sh` to extract the dominant consensus sequence per sample and target marker, and reformat the FASTA headers for database submission:

```bash
bash bin/voucher_export.sh \
  --results results/ \
  --out voucher_output/
```

Options:

| Option | Default | Description |
|---|---|---|
| `--results DIR` | `./results` | Pipeline results directory |
| `--out DIR` | `./voucher_output` | Output directory |
| `--sample NAME` | all | Export only this sample |
| `--marker NAME` | all | Export only this marker (e.g. `COI` or `ITS2`) |

Output files:

- `voucher_output/voucher_sequences.fasta` — one record per sample+marker, using the dominant OTU (highest read count). Header format: `>SAMPLE|MARKER|reads-N[|BLAST:Genus_species]`
- `voucher_output/voucher_summary.tsv` — tabular summary with columns: `sample`, `marker`, `reads`, `otu_key`, `blast_suggestion`

The BLAST taxonomy is attached as `|BLAST:Genus_species` when a match exists and is above threshold. When no match is found (new species), the field is omitted — absence of a BLAST suggestion is expected and does not indicate a pipeline failure.

> `main_barcoding.nf` — the earlier standalone pipeline for this use case — is retained under `extras/` and historical snapshots under `extras/versions/` for reference. The voucher profile supersedes it, using the same robust infrastructure as the main pipeline (state management, round locking, SUP consensus, HTML reports).

## Running the pipeline
[back to Top](#rtbioscan-usage)

The recommended way to launch the pipeline is via `RTBioScan.sh` (see [Quick start](#quick-start-with-rtbioscansh)). For a real-time run using the standard directory layout:

```bash
./RTBioScan.sh --feeder --serve --serve-open \
  --run_id MY_RUN_ID --input_folder /path/to/minknow/output \
  -profile docker -resume
```

Direct Nextflow invocation (when the feeder and server are managed separately):

```bash
NXF_VER=22.10.8 nextflow run main.nf -name 'my_run' -profile docker -resume
```

When you launch through `RTBioScan.sh` and provide `--run_id`, the wrapper auto-injects `--run_id`, `--reads`, `--ori_dir`, `--indexes`, and `--primer_indexes` if you did not pass them explicitly. Raw `nextflow run` does not do this.

The pipeline creates the following in the launch directory:

```
work/           # Nextflow working files (intermediate outputs per process)
results/        # Reports and analysis outputs (configurable via --outdir)
.nextflow_log   # Nextflow execution log
```

## Main arguments
[back to Top](#rtbioscan-usage)

### `-name`
[back to Top](#rtbioscan-usage)

A name for the run. Used to group results in the HTML report and to identify state directories. Appears as the `run_id` throughout outputs.

### `-profile`
[back to Top](#rtbioscan-usage)

Selects a configuration profile. Profiles set default parameters and control how software is executed. Multiple profiles can be combined: `-profile docker,xprize` (later profiles override earlier ones).

Available profiles:

| Profile | Notes | Description |
|---|---|---|
| `docker` | Linux/CUDA only | Runs all analysis tools inside `hecrp/nanortax:latest`. **Not suitable for macOS basecalling**: the default `dorado_device = "metal"` is incompatible with Docker containers (Metal GPU not accessible). For Linux, add `--dorado_device cuda:0`; for CPU-only testing use `--dorado_device cpu`. |
| `conda` | macOS + Linux | Uses the Conda environment in `environment.yml`. Dorado runs as a local binary so Metal GPU works on macOS. Requires the environment to be installed first. |
| `singularity` | Linux only | Singularity with auto-mounts. Same Metal incompatibility as Docker — only suitable for Linux/CUDA. |
| `debug` | — | Identical to the default profile, adds `$HOSTNAME` logging at the start of each process. |
| `xprize` | Requires XPrize databases | COI+ITS2 parameter presets for the XPrize/Tumbira deployment (`conf/xprize.config`). Requires XPrize-specific databases in `db/` and species-of-interest lists. |
| `test` | Requires `db/toDefault/` | Test configuration pointing to `db/toDefault/` databases (`conf/test.config`). Requires those databases to be present; no bundled test data. |
| `barcoding` | Batch mode | Batch-mode configuration (`conf/barcoding.config`, `watch = false`). Intended for diversity runs from pre-collected POD5 files. Three commented-out parameters (`target_barcode`, `intermediate_quality_score`, `barcoding_quality_score`) are reserved for future use and currently silently ignored. |
| `voucher` | Single specimen | Reference sequence generation from a single known individual (`conf/voucher.config`). Tighter clustering (99%), higher consensus depth, pruning disabled. Use with `bin/voucher_export.sh` to format outputs for BOLD/GenBank. |

If `-profile` is not specified, all tools must be installed and available on the `PATH`.

A good starting point for a custom configuration is to copy `conf/xprize.config` and edit the parameters for your run.

### `--run_id`
[back to Top](#rtbioscan-usage)

Optional run identifier used by wrapper-driven launches and some state/report helpers.

- Default: empty string.
- `RTBioScan.sh --run_id` forwards this automatically.
- When set, it is used to derive wrapper defaults such as `results/sample_info/<run_id>/`, `results/pod5/<run_id>/`, and the fallback samples roster used by `--otu_prune_samples_file`.
- This is distinct from Nextflow `-name`, although many wrapper launches set both to the same value.

### `--reads`
[back to Top](#rtbioscan-usage)

Glob pattern for the input POD5 files. Path must be enclosed in quotes.

```bash
--reads "$PWD/pod5/reads_rt_round_pod5/*pod5"
```

Defaults depend on how you launch the pipeline:

- Raw `nextflow run`: `$launchDir/results/pod5/reads_rt_round_pod5/*pod5`
- `RTBioScan.sh --run_id MY_RUN`: `results/pod5/MY_RUN/reads_rt_round_pod5/*pod5` (auto-injected unless overridden)

In real-time mode (`--run_mode realtime`, the default), the pipeline watches this directory for new files via Nextflow's file-watching mechanism — no extra flags needed. In batch mode (`--run_mode batch`), all matching files are processed and the pipeline exits.

> `--reads_rt` is a deprecated alias for `--reads` kept for backwards compatibility. Use `--reads` in all new configurations.

### `--reads_rt`
[back to Top](#rtbioscan-usage)

Deprecated realtime-only alias for `--reads`.

- Default: empty string.
- Only valid with `--run_mode realtime`.
- If both `--reads` and `--reads_rt` are provided, the pipeline uses `--reads_rt` and logs a warning.
- Kept only for backwards compatibility; prefer `--reads`.

## Database parameters
[back to Top](#rtbioscan-usage)

The following parameters define the BLAST databases used for taxonomic classification.

### `--blast_db_specs`
[back to Top](#rtbioscan-usage)

Pipe-separated paths to the per-marker BLAST databases, in the same order as `--targets`. No file extension (BLAST appends `.nhr`, `.nin`, etc.).

```
blast_db_specs = "db/COInr98_2024Jun_RioNegro_Brazil|db/ITS2nr98_2024Jun_RioNegro_Brazil"
```

To add a third marker, append an entry:
```
blast_db_specs = "db/COInr98_2024Jun|db/ITS2nr98_2024Jun|db/16Snr98_2024Jun"
```

### `--blast_filter_db`
[back to Top](#rtbioscan-usage)

Path to the pre-filter BLAST database used to screen reads before target-specific BLAST.

```
blast_filter_db = "db/toDefault/targets_All_tagged_nr95"
```

### `--blast_taxdb`
[back to Top](#rtbioscan-usage)

Path to the NCBI taxdb directory (contains `taxdb.btd` / `taxdb.bti`). Required for lineage retrieval via TaxonKit.

```
blast_taxdb = "db/taxdb"
```

### `--blast_evalue`
[back to Top](#rtbioscan-usage)

Expect value (E) for saving BLAST hits. Default: `11`.

### `--blast_max_hsps`
[back to Top](#rtbioscan-usage)

Maximum number of HSPs to keep per query–subject pair. Default: `50`. Set to `1` to keep only the best HSP.

### `--blast_id_family`
[back to Top](#rtbioscan-usage)

Pipe-separated minimum `pident` thresholds for family-level taxonomic assignment, in the same order as `--targets`.

- Default: `92|92`.
- Used both for OTU BLAST and consensus BLAST assignment depth.

### `--blast_id_genus`
[back to Top](#rtbioscan-usage)

Pipe-separated minimum `pident` thresholds for genus-level taxonomic assignment, in the same order as `--targets`.

- Default: `95|95`.

### `--blast_id_spec`
[back to Top](#rtbioscan-usage)

Pipe-separated minimum `pident` thresholds for species-level taxonomic assignment, in the same order as `--targets`.

- Default: `98|98`.

### `--nonncbi_memtax`
[back to Top](#rtbioscan-usage)

Pipe-separated paths to the per-marker memory-taxonomy tables used to adjust BLAST assignment depth, in the same order as `--targets`.

- Default: `db/COInr_2024Jun_metazoa_memtax1.txt|db/ITS2nr_2024Jun_metazoa_memtax1.txt`.
- Provide one entry per marker; leave an entry empty to skip the table for that marker.
- Paths are resolved relative to the pipeline root.

### `--nonncbi_id2lineage_target`
[back to Top](#rtbioscan-usage)

Optional ID-to-lineage mapping table used during reporting and taxonomy consolidation.

- Default: `db/DBnr_2024Jun_id2lineage.txt`.
- Path is resolved relative to the pipeline root.


## Other command line parameters
[back to Top](#rtbioscan-usage)

### General settings
[back to Top](#rtbioscan-usage)

#### `--outdir`
[back to Top](#rtbioscan-usage)

Output directory for all results. Default: `$launchDir/results`.

#### `--run_mode`
[back to Top](#rtbioscan-usage)

Execution mode for POD5 intake.

- Default: `realtime`.
- Allowed values: `realtime`, `batch`.
- `realtime` watches or polls an intake directory and maintains rolling state for successive rounds.
- `batch` consumes the matching POD5 files once and exits when they are finished.

#### `--watch`
[back to Top](#rtbioscan-usage)

Controls whether new files are watched for in realtime mode.

- Default: `true`.
- Allowed values: boolean (`true/false`, `1/0`, `yes/no`, `on/off`).
- Only applies to `--run_mode realtime`.
- `true`: keep watching the `--reads` glob for new POD5s.
- `false`: treat realtime input as a static set of files and exit after the initial matches finish.

#### `--ori_dir`
[back to Top](#rtbioscan-usage)

Origin POD5 spool directory used by feeder-style deployments and cleanup logic.

- Default: `$launchDir/results/pod5/ori_round_pod5/`.
- Mainly used by `backup_update_and_clean` when `--delete_from_ori_dir` is enabled.
- `RTBioScan.sh --run_id` auto-injects `results/pod5/<run_id>/ori_round_pod5/` unless you override it.

#### `--state_id`
[back to Top](#rtbioscan-usage)

Stable rolling-state namespace under `${outdir}/temp/{ongoing,current}/state/`.

- Default: current run name (`-name` or generated Nextflow run name).
- Use this when you want multiple Nextflow invocations to reuse the same rolling state even if the run name changes.

#### `--restart_mode`
[back to Top](#rtbioscan-usage)

One-shot rolling-state recovery action applied before the run starts.

- Default: `off`.
- Allowed values:
  - `off`: normal behavior.
  - `restore`: restore `results/temp/current/state/<state_id>` into `results/temp/ongoing/state/<state_id>`.
  - `reset`: wipe both `results/temp/current/state/<state_id>` and `results/temp/ongoing/state/<state_id>` before starting.

#### `--restart_force`
[back to Top](#rtbioscan-usage)

Force re-applying `--restart_mode` even if a sentinel file indicates it was already applied for this state.

- Default: `false`.
- Allowed values: boolean (`true/false`, `1/0`, `yes/no`, `on/off`).

#### `--restart`
[back to Top](#rtbioscan-usage)

Legacy numeric restart flag kept for backwards compatibility.

- Default: `0`.
- Only consulted when `--restart_mode off`.
- Any non-zero value is treated like a legacy request to restore rolling state.
- Prefer `--restart_mode restore` for current runs.

#### `-resume`
[back to Top](#rtbioscan-usage)

Resume a stopped or interrupted run. Nextflow restores completed processes from cache and re-runs only what is needed.

#### `--max_memory`
[back to Top](#rtbioscan-usage)

Upper limit for memory allocation per process. Default: `32.GB`. Individual process resource requests are capped at this value.

#### `--max_time`
[back to Top](#rtbioscan-usage)

Upper limit for execution time per process. Default: `240.h`.

#### `--max_cpus`
[back to Top](#rtbioscan-usage)

Upper limit for CPU allocation per process. Default: `16`.

#### `--delete_input_pod5`
[back to Top](#rtbioscan-usage)

Delete or detach the processed intake POD5 after a round completes.

- Default: `false`.
- If the intake file is a symlink, only the symlink is removed.
- The processed POD5 is still moved into `results/pod5/<run_id>/done_round_pod5/` before this cleanup logic.

#### `--delete_from_ori_dir`
[back to Top](#rtbioscan-usage)

Also remove the source POD5 from `--ori_dir` after processing.

- Default: `false`.
- Intended for tightly controlled feeder deployments where the origin spool should be drained aggressively.

#### `--make_round_tar`
[back to Top](#rtbioscan-usage)

Create a `tar.gz` archive of the round directory during `backup_update_and_clean`.

- Default: `false`.

#### `--prune_round_sequences`
[back to Top](#rtbioscan-usage)

Remove large per-round FASTA/FASTQ/SAM files after snapshotting them into the restore caches.

- Default: `true`.
- Recommended for long-running deployments to control disk usage.

#### `--file_wait_minutes`
[back to Top](#rtbioscan-usage)

Timeout for waiting on a newly discovered POD5 to become readable before failing the relevant round.

- Default: `30`.
- Used in both FAST on-target screening and HAC/SUP basecalling intake.

#### `--monochrome_logs`
[back to Top](#rtbioscan-usage)

Disable ANSI color codes in pipeline status logging.

- Default: `false`.
- Useful when writing logs to environments that do not render escape sequences cleanly.

#### `--hostnames`
[back to Top](#rtbioscan-usage)

Advanced advisory map used to warn when a run is launched on a hostname that usually expects a different `-profile`.

- Default: empty map.
- Intended to be set in config files rather than on the command line.
- Structure: profile name -> list of hostname substrings.
- This does not block execution; it only emits a recommendation in the startup log.

---

### Basecalling and read filtering
[back to Top](#rtbioscan-usage)

*Applies to `fast_on_target_detection` and HAC/SUP basecalling.*

#### `--demultiplex_mode`
[back to Top](#rtbioscan-usage)

Controls whether barcode/primer demultiplexing is enabled.

- Default: `auto`.
- Allowed values:
  - `auto`: enable full demultiplexing only when both `--indexes` and `--primer_indexes` exist and contain FASTA records.
  - `on`: require full barcode + primer demultiplexing.
  - `primers_only`: skip barcode FASTA and trim only against `--primer_indexes`.
  - `off`: disable demultiplexing entirely.

#### `--indexes`
[back to Top](#rtbioscan-usage)

FASTA used for barcode/sample demultiplexing.

- Default under raw `nextflow run`: `$launchDir/results/sample_info/demult.fasta`.
- Default under `RTBioScan.sh --run_id MY_RUN`: `results/sample_info/MY_RUN/demult.fasta` (auto-injected).

#### `--primer_indexes`
[back to Top](#rtbioscan-usage)

FASTA used for second-pass primer demultiplexing of `no_adapter` reads.

- Default under raw `nextflow run`: `$launchDir/results/sample_info/primers.fasta`.
- Default under `RTBioScan.sh --run_id MY_RUN`: `results/sample_info/MY_RUN/primers.fasta` (auto-injected).

#### `--targets`
[back to Top](#rtbioscan-usage)

Pipe-separated list of marker names processed in this run. All per-marker parameters (`--target_taxa`, `--min_read_lengths`, `--max_read_lengths`, `--blast_db_specs`, `--blast_id_family`, `--blast_id_genus`, `--blast_id_spec`, `--nonncbi_memtax`) must have the same number of `|`-separated entries, in the same order.

- Default: `COI|ITS2`.
- These strings are embedded in FASTQ/FASTA headers and must match the marker names expected by your databases and demultiplex setup.
- To add a third marker: `--targets "COI|ITS2|16S"` (and set all matching per-marker params with a third `|`-separated value).

#### `--target_taxa`
[back to Top](#rtbioscan-usage)

Pipe-separated list of taxon filters for the fast on-target detection pass, in the same order as `--targets`. A read is classified as on-target only if its fast BLAST hit matches both the marker and the taxon. Leave an entry empty to accept any taxon for that marker.

- Default: `Metazoa|Viridiplantae`.
- Example with a third marker that accepts any taxon: `--target_taxa "Metazoa|Viridiplantae|"`

#### `--on_target_quality_score`
[back to Top](#rtbioscan-usage)

Minimum FAST-basecall qscore used during the initial on-target detection pass.

- Default: `0`.

#### `--min_quality_score`
[back to Top](#rtbioscan-usage)

Minimum HAC basecall qscore required for reads to enter the HQ rolling pool.

- Default: `10`.

#### `--hq_quality_score`
[back to Top](#rtbioscan-usage)

Minimum qscore used when extracting the HAC reads that belong to BLAST-supported OTUs for consensus generation.

- Default: `15`.
- `voucher` and `barcoding` profiles raise this to `20`.

#### `--dorado_device`
[back to Top](#rtbioscan-usage)

Device string passed to Dorado.

- Default: `metal`.
- Examples: `metal`, `cpu`, `cuda:0`.
- On macOS, use a local/Conda execution mode so Dorado can access Metal directly.

#### `--dorado_bin`
[back to Top](#rtbioscan-usage)

Path to the Dorado executable.

- Default: `bin/dorado/bin/dorado`.
- Relative paths are resolved from the pipeline root.

#### `--fast_model`
[back to Top](#rtbioscan-usage)

Dorado model used for the initial FAST on-target screening pass.

- Default: `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_fast@v5.0.0`.

#### `--hac_model`
[back to Top](#rtbioscan-usage)

Dorado model used for the main HAC basecalling pass.

- Default: `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_hac@v5.0.0`.

#### `--sup_model`
[back to Top](#rtbioscan-usage)

Dorado model used for SUP consensus-support basecalling.

- Default: `bin/dorado/bin/dna_r10.4.1_e8.2_400bps_sup@v4.3.0`.

#### `--dorado_retry_attempts`
[back to Top](#rtbioscan-usage)

Retry count for Dorado basecalling wrapper failures.

- Default: `3`.

#### `--dorado_retry_sleep_seconds`
[back to Top](#rtbioscan-usage)

Sleep interval between Dorado retry attempts.

- Default: `300`.

#### `--align_threads`, `--blast_threads`, `--cluster_threads`
[back to Top](#rtbioscan-usage)

Thread budgets for the main CPU-bound tool classes.

- Defaults: `8`, `8`, `8`.
- These values drive both the Nextflow CPU reservation and the underlying tool flags to avoid oversubscription.

#### `--min_read_length/--max_read_length`
[back to Top](#rtbioscan-usage)

Global read length thresholds used outside per-marker contexts (e.g. the initial on-target screen before marker assignment). Reads outside `[min_read_length, max_read_length]` are discarded at that stage.

- Defaults: `286` / `532` bp (covering the union of all markers).
- Per-marker thresholds (`--min_read_lengths`/`--max_read_lengths`) take precedence for marker-specific filtering.

#### `--min_read_lengths/--max_read_lengths`
[back to Top](#rtbioscan-usage)

Pipe-separated per-marker read-length thresholds, in the same order as `--targets`. Applied after reads are labelled with the target marker.

- Defaults: `350|286` / `532|360` (COI: 350–532 bp; ITS2: 286–360 bp).
- Add a third value to cover a third marker: `--min_read_lengths "350|286|400"`.
- Applied after reads are labelled with the target marker.

---

### OTU definition (`OTU_definition`)
[back to Top](#rtbioscan-usage)

#### C1 read archiving
[back to Top](#rtbioscan-usage)

Reads belonging to OTUs that are stable/consolidated are archived from active calculations. This does not imply the reads are bad; it indicates the OTU is considered resolved.

Note: Internally these actions still use `prune` terminology in flags, filenames, and JSON keys for backward compatibility; user-facing text uses **archive**.

#### `--otu_id`
[back to Top](#rtbioscan-usage)

Sequence identity threshold passed to cd-hit-est for OTU clustering.

- Default: `97`.
- `voucher` profile raises this to `99`.

#### `--otu_frozen_enabled`
[back to Top](#rtbioscan-usage)

Enable the frozen-OTU subsystem that removes stable OTUs from active reclustering.

- Default: `true`.

#### `--otu_frozen_min_rounds` / `--otu_frozen_min_reads`
[back to Top](#rtbioscan-usage)

Minimum evidence required before an OTU can be promoted into the frozen set.

- Defaults: `3` rounds, `50` reads.

#### `--otu_frozen_growth_window` / `--otu_frozen_drop_ratio` / `--otu_frozen_min_frac`
[back to Top](#rtbioscan-usage)

Additional promotion controls for frozen OTUs.

- Defaults: `3`, `0.25`, `0.0`.
- Growth window and drop ratio govern stability over recent rounds; `min_frac` is an optional abundance floor.

#### `--otu_frozen_db_only_policy`
[back to Top](#rtbioscan-usage)

Controls what happens when recovering frozen-member mappings requires database-only information.

- Default: `auto`.
- Allowed values: `auto`, `fail`, `warn_skip`.

#### `--otu_prune_frozen_policy`
[back to Top](#rtbioscan-usage)

Controls archive consolidated OTU reads (C1) for rolling HQ reads against frozen/consolidated OTUs:

- `always`: archive reads present in frozen members.
- `until_consolidated` (default): archive reads only for consolidated OTUs.
- `never`: disable this archive.

When `until_consolidated` uses `otu_consolidated_keys.tsv`:

- One-column keys (`OTUB_*`) are treated as global archive keys.
- Two-column keys (`sample<TAB>OTU`) are treated as sample-scoped keys.
- If any sample-scoped rows exist, global one-column rows are ignored for archiving.
- Sample matching always tries the raw adapter/barcode key first.
- Normalized adapter matching (for example `no_adapter_1` -> `no_adapter`) is only applied when the raw key is not a known sample and the normalized key is known from `results/sample_info/<run_id>/samples.txt` (or the file passed via `--otu_prune_samples_file`).
- If the samples list is missing, generic `_N` alias normalization is disabled to avoid cross-sample archiving.
- In missing-samples mode, the only allowed alias normalization is `no_adapter_N` -> `no_adapter`, and only when `KEEP_NO_ADAPTER=1` and sample-scoped consolidated keys include `no_adapter`.
- Empty or whitespace-only samples files are treated as missing for C1 archiving.
- If all reads are consolidated in `until_consolidated` mode, the rolling `qced_reads_hq_accumulated.fasta` is truncated to empty for that round.

Consensus lock carry-forward guardrails:

- Locked OTUs missing from current-round reads are carried forward only if cached consensus exists.
- If cache is missing, the OTU is not re-added to consolidated key outputs for that round.
- In zero-emission rounds, previous `consolidated_consensus_ids.txt` entries are retained except OTUs explicitly dropped in that round (for example, locked OTUs with missing cache).
- Drop filtering now also applies to one-column global rows in `otu_consolidated_keys.tsv` when global drop keys are present.
- For headerless `consolidated_consensus_ids.txt`, drop filtering uses exact fallback IDs derived from dropped keys:
  - `<otu_key>`
  - `<otu_key>_<sample>`
  - `<otu_short>_<sample>` (for example `OTUB_2_no_adapter`)
- Headerless fallback applies only to ID-like lines (`OTUB_*` with optional `_sample` suffix); non-ID metadata/comment lines are not removed by fallback matching.
- Global headerless suffix fallback is `strict` by default (`CONSENSUS_ID_DROP_GLOBAL_SUFFIX_MODE=strict`): only exact global OTU keys are dropped.
- Optional `CONSENSUS_ID_DROP_GLOBAL_SUFFIX_MODE=heuristic` also drops `OTUB_short_*` headerless IDs for global drop rows (disabled by default to avoid false positives).
- Drop-filter AWK invocation enforces strict input order (`drop file` must be first input argument). Incorrect order exits with a clear error instead of silently no-oping.
- When carry-forward has no previous lock-state row, lock state is written as `stable_count=0`, `lock_pass=0`.
- Per-round lock evaluation summary is written to `Consensus/otu_lock_summary.tsv` and appended into `_state/otu_lock_summary_history.tsv` (includes round_id, OTU, lock-rule pass/fail, stability counts, and reason for not consolidating).
- A helper report script is available: `bin/otu_lock_summary_report.sh <otu_lock_summary.tsv> [--round R] [--sample S] [--out file]`.

#### `--otu_prune_samples_file`
[back to Top](#rtbioscan-usage)

Optional path to the samples list used by C1 `until_consolidated` archiving.

- Default: `${workflow.launchDir}/results/sample_info/<run_id>/samples.txt` when not provided and `--run_id` is set.
- If the file is missing, archiving falls back to header-derived keys and constrained normalization rules.

#### `--otu_consolidated_keys_mixed_policy`
[back to Top](#rtbioscan-usage)

Controls behavior when `otu_consolidated_keys.tsv` contains a mix of sample-scoped and global keys:

- `sample_scoped_only` (default): use sample-scoped keys and ignore global keys.
- `warn_and_sample_scoped`: same behavior, but emit a warning.
- `fail`: stop execution with an error on mixed key formats.

#### `--otu_lock_force_prune_max_fasta_mb`
[back to Top](#rtbioscan-usage)

Optional emergency size guard for the rolling HQ FASTA used before OTU definition.

- Default: `0` (disabled).
- When `> 0` and the rolling FASTA exceeds this size, the pipeline forces consolidated-key archiving for that round.

#### `--otu_force_prune_override`
[back to Top](#rtbioscan-usage)

Controls whether forced archiving can override `--otu_prune_frozen_policy=never`.

- Default: `false`.
- If `false`, forced archiving will not run when the archive policy is `never` (a warning is emitted).
- If `true`, forced archiving may override `never` when the size guard is triggered.
- Accepted boolean inputs (case-insensitive): `true/false`, `1/0`, `yes/no`, `on/off`.

#### `--prune_cumulative_pool_all`
[back to Top](#rtbioscan-usage)

Enable the unified cumulative-pool prune pass (default on).

- Default: `true`.
- When enabled, one canonical per-round prune list is built and applied once to `_state/qced_reads_hq_accumulated.fasta`. Reads that contributed to BLAST-assigned consensus sequences in the same round are excluded (recovery subtraction) before the list is applied.
- Current prune sources in the unified list:
  - C1 archive candidates (frozen/consolidated, policy-dependent),
  - size-streak candidates (when `--otu_size_streak_mode enforce`),
  - size-filter dropped reads (now driven by pooled eligible read counts when available),
  - consensus dropped-unassigned IDs from previous round state.
- Per-round audit outputs:
  - `ongoing/<round_barcode>/<barcode>_round_prune_ids.list` — merged candidates (pre-recovery)
  - `ongoing/<round_barcode>/<barcode>_round_prune_ids.final.list` — applied list (post-recovery)
  - `ongoing/<round_barcode>/<barcode>_round_prune_stats.tsv` — includes `recovered_intersection_count`, `final_prune_count`, `pruned_barrier_added_round`, `pruned_barrier_cumulative` (last-round barrier size)
  - `ongoing/<round_barcode>/<barcode>_round_prune_apply.tsv` — reads actually removed
- Last-seen state snapshots:
  - `_state/<barcode>_round_prune_ids_last.list` — last merged candidates (pre-recovery)
  - `_state/<barcode>_round_prune_ids_applied_last.list` — last applied list (post-recovery)
  - `_state/<barcode>_round_prune_stats_last.tsv`
- `_state/<barcode>_round_prune_apply_last.tsv`
- `_state/<barcode>_pruned_barrier.list` — last-round blocklist of applied prunes (non-C1 by default) used to filter rolling SUP input, OTU_definition input, and the active OTU pool
- `_state/<barcode>_active_pool_barrier_filter_stats.tsv` — active-pool filter stats for the last round (when barrier is present)
- `_state/<barcode>_eligible_pool_counts_last.tsv` — pooled eligible read counts per OTU (from consensus), used to drive size-based pruning when available

#### `--otu_incremental_min_new`
[back to Top](#rtbioscan-usage)

Minimum number of new reads required before the pipeline refreshes the active OTU pool incrementally.

- Default: `1`.

#### `--otu_hashmap_mixed_policy`
[back to Top](#rtbioscan-usage)

Behavior when OTU membership recovery encounters mixed hash-map formats.

- Default: `pipe_only`.

#### `--otu_allow_unsafe_recovery`
[back to Top](#rtbioscan-usage)

Allow fallback recovery paths when OTU member reconstruction is incomplete or potentially ambiguous.

- Default: `false`.

#### `--otu_strict_ids`
[back to Top](#rtbioscan-usage)

Use strict OTU identifier handling in rolling-state membership and frozen-member recovery.

- Default: `true`.

#### `--otu_commit_dropped_hashes`
[back to Top](#rtbioscan-usage)

Persist dropped hash decisions into the active-pool decision log.

- Default: `false`.

#### `--otu_pool_decision_include_hash`
[back to Top](#rtbioscan-usage)

Include sequence hashes in active-pool decision audit output.

- Default: `false`.

---

### BLAST (`blast_OTU_pretax`)
[back to Top](#rtbioscan-usage)

#### `--otu_blast_min_members`
[back to Top](#rtbioscan-usage)

Minimum OTU member count gate for read-level OTU BLAST prefiltering.

- Default: `3`.
- Allowed values: integer `>= 0`.
- `0` disables filtering (explicit opt-out).
- Member counts are evaluated from the current round BLAST-input read set.

#### `--otu_blast_filter_mode`
[back to Top](#rtbioscan-usage)

Rollout mode for OTU BLAST member-threshold filtering.

- Default: `enforce`.
- Allowed values:
  - `off`: disabled (current behavior).
  - `observe`: compute filter stats only; do not alter BLAST input.
- `enforce`: apply filtering to BLAST input (unmapped reads are excluded from filtered BLAST query).

#### `--otu_blast_force_use_filtered`
[back to Top](#rtbioscan-usage)

Force the OTU BLAST query to use the **filtered** FASTA (size-based) even when the
decision logic would otherwise fall back to unfiltered (for example, due to ambiguous
hash–cluster assignments). This bypasses the fallback and makes size-based removal
enforceable every round.

- Default: `true`.
- Allowed values: `true|false`.
- Requires: `--otu_blast_filter_mode enforce`.
- Use with `--otu_blast_filter_skip_rounds none` to avoid grace-window bypass.

#### `--otu_blast_filter_skip_rounds`
[back to Top](#rtbioscan-usage)

Grace window for OTU BLAST member-threshold filtering.

- Default: `3`.
- Allowed values:
  - `none`: no grace window.
  - `all`: skip filtering for all rounds (effective mode becomes `off`).
  - integer `N >= 0`: skip filtering for the first `N` assigned rounds, then use `--otu_blast_filter_mode`.
- `0` is treated as `none`.
- Round index is assigned when a round acquires the round lock (failed rounds still consume an index).
- Effective mode is logged per round as:
  - `configured_mode=<...> effective_mode=<...> skip_rounds=<...> round_index=<...>`.

#### `--otu_blast_unassigned_grace_rounds`
[back to Top](#rtbioscan-usage)

Grace window for BLAST-unassigned OTUs in OTU Fate reporting.

- Default: `0` (no grace window).
- Allowed values: integer `>= 0`.
- When `> 0`, OTUs seen in BLAST output but without assignment are counted under `prune_candidates`
  for the first `N` assigned rounds and move to `blast_unassigned` afterward.
- OTU Fate buckets are mutually exclusive within `active_not_frozen` OTUs; grace only
  changes which bucket the OTU is placed into.
- Size-streak OTUs (`otu_blast_min_members`) are counted under `size_streak` only when the
  size-streak filter was actually applied in the round; otherwise they fall under
  `prune_candidates` to reflect that they were not removed.
- OTU Fate universe is selected in this order: `otu_sizes_round.tsv` (round-local), then
  `otu_def`, then lock-summary active set (fallback only when upstream source is
  missing/unreadable/invalid). If both `otu_sizes_round` and `otu_def` are present but empty,
  strict round semantics apply and no lock fallback is used (`otu_fate_universe_empty:strict_round`).
  Fallback source is emitted as a round warning key: `otu_fate_universe_fallback:<source>`.
- Universe diagnostics in `round_report.json`:
  - `otu.diagnostic.fate_universe_source`: `otu_sizes_round|otu_def|lock_summary|none`
  - `otu.diagnostic.fate_universe_reason`: `primary|strict_empty_round|fallback_to_otu_def|fallback_to_lock`
  - `otu.diagnostic.fate_universe_sizes_status`: `ok|empty|missing|unreadable|invalid`
  - `otu.diagnostic.fate_universe_otu_def_status`: `ok|empty|missing|unreadable|invalid`

#### Active prune-candidate artifacts
[back to Top](#rtbioscan-usage)

At the end of each round, `getting_run_summary` emits round-scoped candidate files under:

- `ongoing/<round_barcode>/active_prune_candidates_size_streak.list`
- `ongoing/<round_barcode>/active_prune_candidates_size_candidates.list`
- `ongoing/<round_barcode>/active_prune_candidates_all.list`
- `ongoing/<round_barcode>/active_prune_candidates_counts.tsv`

Current semantics:

- Active scope uses round-local OTU artifacts emitted by `_reporting_OTU_definition`:
  - `${barcode}_otu_members_round.tsv` (`otu_id`, `read_id`)
  - `${barcode}_otu_sizes_round.tsv` (`otu_id`, `size`)
- If round-local members are present but empty, scope remains `round` (no fallback).
- If round-local members are missing/unreadable/invalid, scope remains `round` and outputs are empty with a warning (`active_scope_reason=round_local_unavailable`).
- Size-streak candidates are computed only when the effective OTU BLAST filter mode is in a skip window (`off` due to `within_skip_window` or `all`) and `otu_blast_min_members > 0`.
- If round index is missing:
  - `skip_rounds=all`: size-streak candidates are still computed, with `effective_reason=skip_all_rounds_no_index`.
  - otherwise: size-streak branch is disabled for that round.

`active_prune_candidates_counts.tsv` keys:

- `active_total`
- `active_scope` (`round`)
- `active_scope_reason` (`round_local_ok|round_local_empty|round_local_unavailable`)
- `size_streak_input_status` (`ok|empty|missing|unreadable|invalid`)
- `size_streak_active`
- `size_streak_candidates`
- `size_streak_possible` (`0|1`; inputs present and `otu_blast_min_members >= 2`)
- `size_streak_applied` (`0|1`; size-streak applied this round)
- `union`
- `size_streak_disabled` (`0|1`)
- `effective_mode`
- `effective_reason`
- `round_index` (or `NA`)

Round JSON mapping:
- `round_report.json` is enriched with `active_prune_candidates_counts.tsv` for downstream analytics (not shown in the UI).

Consensus emitted breakdown:
- `round_report.json` includes `consensus.emitted_by_marker_taxon` with per-round counts for:
  - `coi_assigned`, `its2_assigned`, `coi_unassigned`, `its2_unassigned`,
  - `other_assigned`, `other_unassigned` (present when other markers exist).

Informative OTU breakdown:
- `round_report.json` includes `otu.active_by_marker_taxon` with per-round **informative OTU** counts for:
  - `coi_assigned`, `its2_assigned`, `coi_unassigned`, `its2_unassigned`,
  - `other_assigned`, `other_unassigned` (present when other markers exist).
  Informative OTUs are the union of consolidated, frozen (not consolidated), and informative_dynamic OTUs.

#### `--otu_blast_enforce_missing_max_frac`
[back to Top](#rtbioscan-usage)

Maximum allowed fraction of reads lacking OTU mapping during `--otu_blast_filter_mode enforce`.

- Default: `0.1`.
- Allowed values: decimal in `[0,1]`.
- In `enforce` mode, the run fails only when:
  - `reads_missing_from_clstr / total_reads` exceeds this threshold.
- Set to `0` for strict behavior (fail on any missing mapped reads when `total_reads > 0`).
- If ambiguous hash-to-cluster assignments are detected, the filter falls back to unfiltered BLAST input for that round and logs a warning.

#### `--otu_blast_enforce_no_clusters_policy`
[back to Top](#rtbioscan-usage)

Controls behavior in `enforce` mode when filtering cannot produce usable OTU assignments in the current round.

- Default: `fallback_unfiltered`.
- Allowed values:
  - `fail`: stop the round with an error.
  - `fallback_unfiltered`: keep BLAST input unfiltered for that round.
  - `allow_empty`: proceed with filtered (possibly empty) BLAST input.
- Decision order in `enforce` mode:
  - `clstr_records == 0` (no cluster member rows): apply this policy.
  - `total_otus == 0` (clusters exist but no assignable OTUs after hash/cluster reconciliation): apply this policy.
  - otherwise, evaluate `reads_missing_from_clstr / total_reads` against `--otu_blast_enforce_missing_max_frac`.

#### `--otu_blast_unassigned_mode`
[back to Top](#rtbioscan-usage)

Rollout mode for BLAST-unassigned OTU handling.

- Default: `enforce`.
- Allowed values: `off`, `observe`, `enforce`.

#### `--otu_blast_unassigned_max_otu_size`
[back to Top](#rtbioscan-usage)

Maximum OTU size eligible for the BLAST-unassigned pruning path.

- Default: `50`.

#### `--otu_size_streak_mode`
[back to Top](#rtbioscan-usage)

Phase‑B size‑streak mode for canonical OTU membership snapshots.

- Default: `enforce`.
- Allowed values:
  - `off`: disabled.
  - `observe`: compute size‑streak state and emit candidate prune IDs/stats.
  - `enforce`: compute size‑streak state and feed previous‑round size‑streak IDs into the unified round prune list (see `--prune_cumulative_pool_all`).
- Size‑streak identity is tracked internally as `marker|sequence_hash` (not round‑local `OTUB_*` index), so streaks can continue across rounds despite OTU renumbering.
- Size‑streak rows are computed only for OTU rows whose read IDs can be resolved in the current round hash map (`_state/<barcode>_otu_hash_map.tsv`).
- When rows cannot be matched to a sequence hash, a warning is emitted:
  - `WARN: otu_size_streak_missing_hash_rows=<n> (size‑streak skipped for missing hash)`

#### `--otu_size_streak_min_rounds`
[back to Top](#rtbioscan-usage)

Minimum consecutive rounds required before a size‑streak OTU key is marked as a prune candidate.

- Default: `3`.
- Allowed values: integer `>= 1`.
- Candidate IDs are exported per round as `${barcode}_otu_size_streak_prune_ids.txt` and persisted under `ongoing/<round_barcode>/`.

#### `--otu_unassigned_streak_mode`
[back to Top](#rtbioscan-usage)

Rollout mode for repeated-round pruning of BLAST-unassigned OTUs.

- Default: `enforce`.
- Allowed values: `off`, `observe`, `enforce`.

#### `--otu_unassigned_streak_min_rounds`
[back to Top](#rtbioscan-usage)

Minimum consecutive unassigned rounds required before an OTU becomes an unassigned-streak prune candidate.

- Default: `3`.

#### `--otu_unassigned_streak_min_size` / `--otu_unassigned_streak_max_size`
[back to Top](#rtbioscan-usage)

Size bounds for OTUs eligible for unassigned-streak pruning.

- Defaults: `2` / `50`.

---

### Consensus (`consensus`)
[back to Top](#rtbioscan-usage)

#### `--consensus_id`
[back to Top](#rtbioscan-usage)

Sequence identity threshold used by the consensus-generation step.

- Default: `99`.

#### `--consensus_reads_mode`
[back to Top](#rtbioscan-usage)

Controls what `reads-N` represents in consensus headers and downstream reports:

- `representative` (default): `reads-N` equals the selected representative OTU's read count.
- `cluster_total`: `reads-N` equals the sum of all reads merged into the consensus cluster.

Per-round HTML report metrics do not rely on `reads-N` for read-fate attribution.
`read_fate.consensus_used_reads` is sourced from `consensus_round_provenance.tsv` (exact round-local consensus provenance).

#### `--consensus_min_reads` / `--consensus_max_reads`
[back to Top](#rtbioscan-usage)

Minimum and maximum read counts used per OTU during consensus generation.

- Defaults: `5` / `15`.
- `voucher` profile raises `--consensus_max_reads` to `200`.

#### `--consensus_min_qscore` / `--consensus_consolidated_min_qscore`
[back to Top](#rtbioscan-usage)

Minimum read qscore thresholds for consensus input selection.

- Defaults: `15` / `20`.
- The consolidated threshold applies to reads coming from consolidated/frozen OTUs.

#### `--consensus_max_N`
[back to Top](#rtbioscan-usage)

Maximum number of ambiguous `N` bases allowed in a consensus sequence before it is treated as low quality.

- Default: `4`.

#### `--consensus_keep_original_reads`
[back to Top](#rtbioscan-usage)

Keep per-consensus OriginalReads lists for each round (off by default).

- When enabled, `Consensus/<sample>/OriginalReads/*_reads.list` files are copied into the round results directory at `${outdir}/temp/ongoing/state/<state_id>/<round>/Consensus/<sample>/OriginalReads/`.
- A manifest is written alongside them at `${outdir}/temp/ongoing/state/<state_id>/<round>/Consensus/consensus_original_reads_manifest.tsv`.
- Manifest columns: `consensus_id`, `reads_list_path`, `compressed`, `read_count`.
- When disabled, OriginalReads lists are left in `Consensus` (baseline behavior).

Default: `false`.
`<state_id>` is the run state directory name created by Nextflow under `${outdir}/temp/ongoing/state/`.

#### `--consensus_cpu_budget`
[back to Top](#rtbioscan-usage)

CPU budget passed to the consensus helper for internal parallelism decisions.

- Default: `0`.
- `0` means auto-detect: P-cores on Apple Silicon, `nproc - 2` on other platforms.

#### `--consensus_zero_emit_policy`
[back to Top](#rtbioscan-usage)

Behavior when a round produces zero consensus sequences.

- Default: `warn`.
- Allowed values: `warn`, `fail`.

#### `--consensus_id_mismatch_policy`
[back to Top](#rtbioscan-usage)

Behavior when consensus ID expectations and recovered OTU state disagree.

- Default: `warn`.
- Allowed values: `warn`, `fail`.

#### `--consensus_cache_below_min_policy`
[back to Top](#rtbioscan-usage)

Behavior for cached consensus entries that fall below the current minimum-read threshold.

- Default: `keep`.
- Allowed values: `keep`, `drop`.

#### Unassigned cluster pruning
[back to Top](#rtbioscan-usage)

#### `--prune_unassigned_clusters`
[back to Top](#rtbioscan-usage)

Enable consensus-time sequence-cluster pruning that keeps:

- all clusters with at least one BLAST-assigned read UUID, plus
- top-K unassigned clusters (see `--prune_unassigned_keep_top`).

Default: `false`.

#### `--prune_unassigned_drop_reads`
[back to Top](#rtbioscan-usage)

Permanently remove reads from **dropped unassigned clusters** so they never enter future consensus.

- Only applies when `--prune_unassigned_clusters true` and pruning is actually applied.
- Dropped IDs are recorded per round:
  - `${outdir}/temp/ongoing/state/<state_id>/<round>/${barcode}_pruned_unassigned_reads.list`
- A cumulative audit is kept at:
  - `${outdir}/temp/ongoing/state/<state_id>/_state/${barcode}_pruned_unassigned_reads_all.list`
- On `-resume`, per-round lists may be regenerated; the cumulative list is de-duplicated.

Default: `false`.

#### `--prune_unassigned_grace_rounds`
[back to Top](#rtbioscan-usage)

Round-index grace window before unassigned-cluster pruning is allowed.

- If `round_index <= grace_rounds`, pruning is skipped.
- If round index is unavailable, pruning is skipped with a warning.

Default: `3`.

#### `--prune_unassigned_keep_top`
[back to Top](#rtbioscan-usage)

Maximum number of unassigned clusters to keep per OTU when pruning is active.

- Assigned clusters are always kept.
- Set `0` to keep only assigned clusters.

Default: `5`.

#### `--assign_protection_level`
[back to Top](#rtbioscan-usage)

Minimum taxonomic depth a BLAST or consensus assignment must reach before the reads in that OTU are **protected from pruning**.

| Value | A read/OTU is protected when… |
|-------|-------------------------------|
| `family` | `family` **or** `genus` **or** `species` column is non-empty / non-Unassigned |
| `genus` *(default)* | `genus` **or** `species` column is non-empty / non-Unassigned |
| `species` | `species` column is non-empty / non-Unassigned |

All levels use **"or better"** semantics: a species-level assignment satisfies `genus` and `family` thresholds as well.

This parameter is applied uniformly at four protection points in the pipeline:

1. **Pre-BLAST re-injection** — which historically assigned OTU keys are carried forward.
2. **Post-BLAST pruning candidates** — which reads are eligible for size-based removal.
3. **Consensus read recovery** — which reads are recovered after consensus BLAST.
4. **Consensus OTU key protection** — which OTU keys are written to the ever-assigned state.

**Practical guidance:**
- `genus` (default) — only OTUs resolved to genus or better are protected. Family-only hits (often ambiguous multi-species clusters) become pruning candidates in later rounds, keeping the OTU pool lean.
- `family` — restores the broadest protection; useful when the reference database lacks genus-level coverage for the taxa of interest.
- `species` — strictest mode; only confirmed species identifications are protected. Appropriate for high-confidence or voucher-oriented runs.

Default: `genus`.

#### OTU consolidation lock
[back to Top](#rtbioscan-usage)

The consolidation lock controls when an OTU can be treated as consolidated and reused from cache in later rounds.

#### `--otu_consolidation_lock`
[back to Top](#rtbioscan-usage)

Enable/disable lock-based consolidation behavior.

- Default: `true`

#### `--otu_lock_small_cluster_ratio`
[back to Top](#rtbioscan-usage)

Maximum allowed ratio between the largest non-consolidated cluster and the smallest consolidated cluster inside the OTU representative set.

- Default: `0.1`

#### `--otu_lock_min_consolidated_reads`
[back to Top](#rtbioscan-usage)

Minimum read count required for a representative cluster to be considered consolidated by the lock rule.

- Default: `10`

Clarification on "consolidated cluster" terminology:
The lock rule does not require a previously consolidated OTU. In each round, clusters that meet the consolidation thresholds (minimum reads and minimum Q score) are treated as **candidate consolidated clusters** for that decision. The ratio rule compares the largest non-candidate cluster to the smallest candidate cluster in the same round. If no clusters qualify as candidates, the lock rule cannot pass in that round.

#### `--otu_lock_min_stable_rounds`
[back to Top](#rtbioscan-usage)

Minimum number of consecutive rounds that must satisfy the lock rule before an OTU is consolidated.

- Default: `1` (preserves current behavior).
- Set to `2` or higher to reduce single-round false-positive locks.

#### `--otu_lock_revalidate_every_rounds`
[back to Top](#rtbioscan-usage)

Optional periodic revalidation interval for locked OTUs.

- Default: `0` (disabled).
- When set to `N > 0`, locked OTUs are forced through recompute path every Nth round to verify cache stability.

#### `--otu_lock_reset_keys`
[back to Top](#rtbioscan-usage)

Comma/semicolon/space separated OTU keys to remove from prior lock state before the current round.

- Example: `--otu_lock_reset_keys "OTUB_1-COI-no_adapter_1,OTUB_2-ITS2-sampleA"`
- Reset keys are excluded from previous consolidated key carry-over in the same round.

#### Consensus assignment depth contract
[back to Top](#rtbioscan-usage)

Assignment depth for consensus sequences is determined **solely by `pident` vs. the configured thresholds** — no fallback path can produce a deeper assignment than the thresholds allow.

- `--blast_id_family`: minimum `pident` for a family-level assignment (pipe-separated, one value per marker). Previously this threshold only filtered the BLAST query; it now also governs assignment depth.
- `--blast_id_genus`: minimum `pident` for a genus-level assignment (pipe-separated).
- `--blast_id_spec`: minimum `pident` for a species-level assignment (pipe-separated).

Rules applied per consensus hit:

| `pident` range | assignment level |
|---|---|
| `>= blast_id_spec` | `species` |
| `>= blast_id_genus` | `genus` |
| `>= blast_id_family` | `family` |
| below or missing | `unassigned` |

When a consensus sequence matches both targets, the deepest justified level wins (species > genus > family > unassigned).

Lineage columns (kingdom → species) are taxonomy metadata populated by TaxonKit and masked to the assigned depth. An unknown or missing assignment level always produces masked (`Unassigned`) output.

---

### Process concurrency
[back to Top](#rtbioscan-usage)

#### `--maxforks_fast`
[back to Top](#rtbioscan-usage)

Maximum concurrency for `fast_on_target_detection` process scheduling.

- Default: `1`.
- Allowed values: integer `>= 1`.
- Keep `1` for strict round-by-round behavior; set `>1` together with `--round_lock_scope dorado_only` to allow overlap after FAST Dorado completes.

#### `--maxforks_core_cpu`
[back to Top](#rtbioscan-usage)

Maximum concurrency for core CPU-heavy OTU/BLAST stages (`OTU_definition`, `blast_OTU_pretax`).

- Default: `1`.
- Allowed values: integer `>= 1`.

#### `--maxforks_consensus`
[back to Top](#rtbioscan-usage)

Maximum concurrency for `consensus` process scheduling.

- Default: `1`.
- Allowed values: integer `>= 1`.

#### `--maxforks_reporting`
[back to Top](#rtbioscan-usage)

Maximum concurrency for reporting/aggregation processes.

- Default: `2`.
- Allowed values: integer `>= 1`.

#### `--round_lock_scope`
[back to Top](#rtbioscan-usage)

Controls how long the per-round lock is held.

- Default: `full_round`.
- Allowed values:
  - `full_round`: keep current behavior (lock released at `backup_update_and_clean`).
  - `dorado_only`: release round lock immediately after FAST Dorado basecalling completes for the round.
- In `dorado_only`, release occurs before FAST post-basecalling CPU/reporting steps complete; this is intentional to allow overlap with later rounds.
- `dorado_only` only increases overlap when `--maxforks_fast > 1` (or after splitting FAST into separate Dorado/post-processing processes).
- Dorado execution remains serialized across all rounds via a dedicated `_state/.dorado.lock`.

#### `--lock_wait_seconds`
[back to Top](#rtbioscan-usage)

Lock wait timeout for short-lived filesystem locks used during reporting, restore, and cleanup steps.

- Default: `300`.

#### `--round_lock_wait_minutes`
[back to Top](#rtbioscan-usage)

Maximum time to wait for the per-state round lock before failing the run.

- Default: `360`.

#### `--stale_lock_ttl_minutes`
[back to Top](#rtbioscan-usage)

Age threshold for reclaiming a stale per-state round lock.

- Default: `360`.
- Set to `0` to disable TTL-based reclaim and rely only on same-host dead-PID detection when metadata is available.

---

### HTML report (`getting_run_summary`)
[back to Top](#rtbioscan-usage)

#### `--html_report_enabled`
[back to Top](#rtbioscan-usage)

Enable or disable incremental HTML report rendering (`${params.outdir}/report.html`) plus per-run report copies.

- Default: `true`.
- Allowed values: boolean (`true/false`, `1/0`, `yes/no`, `on/off`).
- Report update order per round is:
  1. collect round JSON (`ongoing/<round_barcode>/round_report.json`)
  2. append/dedupe history (`ongoing/_state/report_history.jsonl`)
  3. update run index (`${params.outdir}/runs_index.jsonl`)
  4. render HTML:
     - index: `${params.outdir}/report.html`
     - run report: `${params.outdir}/runs/<run_id>/report.html`
- History dedupe key is `run_id + barcode + round_barcode` (resume-safe).
- Missing source TSVs are recorded in `warnings[]`; report generation does not fail the round.
- Report schema version is `1.4`.
- `otu.canonical.active` is a unique OTU count (`OTU_id`/`otu_id`), not read rows.
- `blast.mode` reflects pipeline mode (`off|observe|enforce`); `blast.missing_policy` reports `keep|drop`.
- HTML rendering sorts rounds by `timestamp_utc` (missing timestamps last), then `round_barcode`, then `barcode`.
- Renderer also writes `${params.outdir}/report_state.json` (refresh sidecar) after `report.html`.
- Per-run report also writes `${params.outdir}/runs/<run_id>/report_state.json`.
- Sidecar fields are minimal: `schema_version`, `generated_at_utc`, `report_revision`.
- `report_revision` is a SHA256 hash of `ongoing/_state/report_history.jsonl` content.
- Report figures are copied into `${params.outdir}/runs/<run_id>/report_assets` from `${params.outdir}/ongoing/_state` using `assets/report/figures.tsv`.
- Sample figure definitions are read from `assets/report/figures_sample.tsv` and attached under each sample panel.
- Index report (`${params.outdir}/report.html`) is cross-run and does not render a figure gallery.
- Index charts are run-level summaries derived from the latest round (`run_summary` in `run_report.json`).
- Runs are displayed in pages of 10 (latest updated first).
- `run_summary_source_round` records the round_barcode used to compute each run summary.
- Figures are rendered in per-run reports only (`${params.outdir}/runs/<run_id>/report.html`).
- Missing figures in per-run reports are shown as placeholders (not interactive).
- Per-run reports include two main sections: `Global Overview` and `Sample Details`, with a `Sample Index` navigation block between them.
- Circle tree figures visualize lineage structure for OTU and consensus assignments. OTU circle trees are weighted by reads in OTUs; consensus circle trees are weighted by consensus count.
- Global section cards use run-level totals across all rounds in the run report history.
- Sample section cards use per-sample sums across rounds (not run-unique entity counts).
- Sample section figures are phase-A and currently generated from demultiplex `Read_counts` family only.
- Sample figure assets are written to `${params.outdir}/runs/<run_id>/report_assets/samples/<sample_id>/`.
- Sample labels are normalized early in the pipeline so `no_adapter` and `no_adapter_*` are treated as the same sample in FASTQ headers, reporting TSVs, and reports.
- Sample identity uses a deterministic, case-sensitive ID derived from the trimmed label.
- Reads fate chart is a stage-loss partition (later → earlier) and uses `read_fate.*` metrics; when demultiplexing is off, the sample section is hidden and the chart uses `reads.on_target` as the demux base.
  - `read_fate.blast_seen_reads` / `blast_assigned_reads` / `blast_unassigned_reads` are computed from unique normalized BLAST OTU `read_id` values (`read_id` prefix before `|`).
  - `blast_assigned_reads` counts reads with numeric `taxid > 0` or non-`Unassigned` family/genus/species; `blast_unassigned_reads` is the remainder.
  - In demux-on rounds, the chart uses adapter-scoped BLAST fields (`blast_assigned_reads_adapter`, `blast_unassigned_reads_adapter`) to avoid overlap with the `no_adapter` segment.
  - If `read_fate.blast_assignment_status` is missing, legacy payloads are treated as classified when explicit BLAST assigned/unassigned fields are present.
  - Empty/whitespace sample labels are excluded from adapter/no_adapter bucket splits; a literal non-empty sample label `unknown` is treated as a regular sample label (adapter bucket unless it matches `no_adapter`).
  - If adapter demuxed reads are not present in BLAST rows, the chart shows them as `Demuxed adapter not in BLAST` to preserve totals.
  - If adapter BLAST-seen split fields are missing (legacy payloads), or if unbucketed BLAST-seen reads are present, the chart emits a warning and cannot separate `Demuxed adapter not in BLAST` from adapter BLAST-unassigned.
- The Runs table is driven by `${params.outdir}/runs_index.jsonl` and links to per-run reports.
- Run status uses a time-based cadence: Fresh (≤1.5×), Aging (1.5–3×), Stale (>3×) relative to the last two updates (or start→first round).
- Per-run reports include an "OTU Assignments (Latest Round)" table with Species/Genus/Family tabs, sorted by supporting reads, paginated in 10-row pages (top 200 per level).
Note: run reports are easiest to browse over HTTP, but with the default empty `--html_report_url_prefix` they use relative links rather than root-absolute paths.

#### `--html_report_auto_refresh`
[back to Top](#rtbioscan-usage)

Enable browser-side auto-refresh polling for `report.html`.

- Default: `true`.
- Allowed values: boolean (`true/false`, `1/0`, `yes/no`, `on/off`).
- Polling compares `report_state.json.report_revision`; page reload occurs only when revision changes.
- If polling fails (common with `file://`), report remains usable and a passive warning is shown.

#### `--html_report_refresh_seconds`
[back to Top](#rtbioscan-usage)

Polling interval (seconds) for HTML auto-refresh.

- Default: `15`.
- Allowed values: integer `>= 1`.
- Recommended operational range: `15–30` seconds.

#### `--html_report_sample_plot_max`
[back to Top](#rtbioscan-usage)

Maximum number of samples to render sample-specific figures per round.

- Default: `10`.
- Allowed values: integer `>= 0`.
- `0` disables sample-specific figure generation.
- Selection is top-N samples by HAC `read_count` **per barcode group** from `${barcode}_summary_demult_rpt.txt` — each marker contributes its top-N independently.

#### Plot style contract
[back to Top](#rtbioscan-usage)

All report plots are styled through `bin/plot_style.R`.

- Canonical helpers: `theme_journal()`, `save_plot_journal()`, `save_plot_placeholder()`.
- Default export size: `7 x 4.5` inches, `dpi` 300.
- Font: `Helvetica` when available, otherwise the R graphics device default.
- Palette keys: `reads_series`, `reads_cumulative`.

If a plot needs a new semantic palette, add it to `plot_palettes` and fetch it via `plot_palette("<key>")`.

#### `--html_report_url_prefix`
[back to Top](#rtbioscan-usage)

URL prefix used to build report asset and run links.

- Default: empty string (`""`), which produces relative links.
- Set to `/` or another prefix only when you need root-relative links behind an HTTP server or reverse proxy.
- Relative-link reports work best for direct local browsing and for the default `serve_report.sh` usage.

#### `--min_reads_sample`
[back to Top](#rtbioscan-usage)

Minimum per-sample read count used by the run-summary/reporting helpers when deciding which samples to include in aggregate outputs.

- Default: `3`.
- `voucher` profile lowers this to `1`.

#### `--metazoa_spc_basics` / `--viridiplantae_spc_basics`
[back to Top](#rtbioscan-usage)

Optional species-of-interest tables used by reporting scripts to filter or annotate displayed taxa.

- Defaults: empty string (disabled).
- `xprize` profile points these to `db/metazoa_spec_basics.txt` and `db/viridiplantae_spec_basics.txt`.

#### `--local_metazoa_gns` / `--local_viridiplantae_gns`
[back to Top](#rtbioscan-usage)

Optional local genus lists used by reporting scripts for XPrize-style taxon highlighting.

- Defaults: empty string (disabled).
- `xprize` profile points these to the corresponding `db/GBIF_iNAturalist_*.txt` files.

#### Live report viewing
[back to Top](#rtbioscan-usage)

Auto-refresh works best when the report is served over HTTP (not `file://`).

The easiest way is to add `--serve --serve-open` to your `RTBioScan.sh` command — the server starts alongside the pipeline and stops when it exits. The report is served at:

```
http://127.0.0.1:8000/report.html
```

To open all per-run reports on start:
```bash
./RTBioScan.sh --serve --serve-open-all ...
```

To open the last N per-run reports on start:
```bash
./RTBioScan.sh --serve --serve-open-last 5 ...
```

To use a custom port or outdir:
```bash
./RTBioScan.sh --serve --serve-port 8001 --serve-open --outdir my_results ...
# --serve-dir is derived automatically from --outdir; pass --serve-dir explicitly to override
```

**Standalone server** (to view reports from a completed run without relaunching the pipeline):

```bash
bash bin/serve_report.sh --dir results --port 8000 --open --quiet
```

Makefile shortcuts:
```bash
make serve-report-open
make serve-report-open-all
make serve-report-open-last REPORT_OPEN_LAST=10
```

Notes:
- If `python3` is missing, the helper exits with a clear error.
- If the port is busy, choose a different one (e.g. `--port 8001`).
- Use `--verbose` if you want access logs printed.

Manual rebuild (optional):

```bash
make report
make report-run RUN_ID=your_run_id
make report-all
```

Notes:
- `report_rebuild.sh` auto-detects the most recent `report_history.jsonl` under `${outdir}/temp/ongoing/state/*/_state/`.
- Use `--state-id` to target a specific run state if needed.

---

### OTU membership exports (`_reporting_OTU_definition`)
[back to Top](#rtbioscan-usage)

#### Canonical OTU membership export (Phase A)
[back to Top](#rtbioscan-usage)

The canonical per-round OTU membership source is `${barcode}_otu_def_rpt.txt` generated in `_reporting_OTU_definition` (OTU-definition lineage, independent of BLAST hit/selection filtering).

- OTU key source: `OTU_id` column (`OTUB_<cluster>[-<marker>]`) from the OTU-definition report.
- Marker suffix is appended only when a whitelisted target token (from `--targets`, pipe-separated) found anywhere in the annotated read header is detected.
- Marker-token observability is emitted once per round in `_reporting_OTU_definition` logs:
  - `INFO: otu_marker_tokens seen=<n> allowed=<n> rejected=<n>`
- Read ID source: `read_id` column from the OTU-definition report.
- OTU keys in this Phase-A export are round-local cluster labels (per barcode/round) and are not yet a cross-round stable streak key.
- Singleton semantics from this export are therefore "single-sequence OTU member in the NR cluster report", not read-depth abundance.

Exports generated each round:

- `${params.outdir}/ongoing/<round_barcode>/otu_members.tsv` with columns: `otu_key<TAB>read_id`
- `${params.outdir}/ongoing/<round_barcode>/otu_sizes.tsv` with columns: `otu_key<TAB>member_count`
- `${params.outdir}/ongoing/<round_barcode>/otu_members_stats.tsv` (export counters)

Latest state snapshots:

- `${params.outdir}/ongoing/_state/<barcode>_otu_members.tsv`
- `${params.outdir}/ongoing/_state/<barcode>_otu_sizes.tsv`
- `${params.outdir}/ongoing/_state/<barcode>_otu_members_stats_last.tsv`

BLAST-derived diagnostic exports are kept separate (non-canonical):

- `${params.outdir}/ongoing/<round_barcode>/otu_members_blastdiag.tsv`
- `${params.outdir}/ongoing/<round_barcode>/otu_sizes_blastdiag.tsv`
- `${params.outdir}/ongoing/<round_barcode>/otu_members_blastdiag_stats.tsv`
