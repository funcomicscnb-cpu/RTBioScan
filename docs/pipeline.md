# RTBioScan: Pipeline Overview

> **Docs:** [Index](README.md) · **Pipeline Overview** · [Concepts](concepts.md) · [Installation](installation.md) · [Usage](usage.md) · [Output](output.md) · [Report Schema](report_schema.md)

## Overall Aim
[back to Top](#rtbioscan-pipeline-overview)

RTBioScan is a real-time Oxford Nanopore Technologies (ONT) amplicon metabarcoding pipeline. Its purpose is to turn incoming POD5 sequencing data into interpretable biodiversity evidence while a sequencing run is still progressing, and to preserve enough [state](concepts.md#workflow-and-state) that the analysis can be resumed, audited, and compared across [rounds](concepts.md#workflow-and-state).

The pipeline philosophy is practical rather than purely archival: each round should produce useful information quickly, while later rounds refine the evidence as more reads become available. RTBioScan therefore reports both the biological interpretation and the processing context behind it: how many reads were generated, how many reached each stage, which samples or tracked groups they came from, which markers were detected, which taxa were assigned, and which consensus sequences were produced.

The intended final effect is a set of live reports and reusable result files that help scientific users answer:

1. **Is the sequencing run producing usable data?**
2. **Which taxa are supported by the current evidence?**
3. **Which samples, primers, or replicates contain that evidence?**
4. **Are detections stable across rounds, or still changing as data accumulate?**
5. **Which consensus sequences can be exported or reviewed downstream?**

RTBioScan is best suited for rapid biodiversity assessment, run monitoring, early detection of taxa of interest, and practical amplicon consensus generation. It is not intended to replace expert taxonomic review, whole-genome assembly, or a final publication-grade downstream analysis.

## Public Workflow
[back to Top](#rtbioscan-pipeline-overview)

Public workflow figure:

![RTBioScan public workflow overview](assets/pipeline_overview_public.svg)

At a high level, the pipeline connects five conceptual scopes:

```text
Input preparation
  -> read detection, basecalling, and marker filtering
  -> sample or tracked-group demultiplexing
  -> OTU definition and taxonomic assignment
  -> consensus generation, state carry-forward, and reports
```

The core classification path is driven by marker-specific barcoding databases and taxonomy tables, such as BOLD-derived, NCBI-derived, or project-specific references. Optional observational resources, such as GBIF or iNaturalist-derived lists, are reporting aids for filtering or highlighting taxa; they do not replace the sequence-based taxonomic assignment.

## Round-Based Design
[back to Top](#rtbioscan-pipeline-overview)

RTBioScan processes data in [rounds](concepts.md#workflow-and-state). This design is important for real-time sequencing because new data arrive continuously, but users still need stable intermediate reports.

Each round updates:

- read and demultiplexing summaries
- OTU and taxonomic-assignment tables
- consensus outputs
- cumulative state used by later rounds
- live HTML reports

The latest completed round is usually the best view of the run. Earlier rounds remain useful because they show how detections developed over time.

Several parameters control this behavior:

- `--run_mode realtime|batch` chooses live intake or one-time processing.
- `--reads` points Nextflow at the POD5 chunks to process.
- `--run_id` identifies the biological or operational run in wrapper-driven launches.
- `-name` identifies the Nextflow execution and report directory.
- `--state_id` controls the rolling-state namespace when state reuse must be explicit.
- `-resume`, `--restart_mode`, and `--restart_force` control recovery after interruption.
- `--outdir` selects where reports and outputs are written.

The full command-line reference is in [usage.md](usage.md).

## 1. Input Preparation
[back to Top](#rtbioscan-pipeline-overview)

The first conceptual step is to describe what is being sequenced and how reads should be interpreted. RTBioScan needs raw sequencing data, [marker](concepts.md#samples-barcodes-primers-and-markers) definitions, [sample](concepts.md#samples-barcodes-primers-and-markers) metadata, and taxonomic reference resources.

For real-time runs, `RTBioScan.sh --feeder` watches a MinKNOW output folder and slices incoming POD5 files into round-sized chunks. This lets the pipeline process data progressively instead of waiting for the full sequencing run to finish. For completed datasets, batch mode uses an existing POD5 glob and exits after all matching files are processed.

Common input parameters are:

- `--input_folder` — the MinKNOW POD5 source directory for feeder-driven real-time runs.
- `--num_reads` — the number of reads placed in each feeder-created POD5 chunk.
- `--sleep_time` — how often the feeder checks for new data.
- `--reads` — the POD5 chunk glob consumed by Nextflow.
- `--metadata` — the run metadata table used to create sample-specific inputs.
- `--general_fasta` — the master barcode FASTA used by metadata setup.
- `--primers_fasta` — the primer FASTA used for marker-aware demultiplexing.
- `--do_metadata` — creates the prepared `sample_info/<run_id>/` files before the run starts.

The prepared files are written under `results/sample_info/<run_id>/` and include `demult.fasta`, `primers.fasta`, `samples.txt`, and run-filtered metadata tables. These files define the sample names, barcode identities, primer identities, and replicate information that later appear in the reports.

## 2. Markers, Targets, and Reference Databases
[back to Top](#rtbioscan-pipeline-overview)

Amplicon metabarcoding identifies organisms through selected genetic [markers](concepts.md#samples-barcodes-primers-and-markers). In RTBioScan, marker names such as `COI` and `ITS2` are not just labels: they coordinate read filtering, demultiplexing, BLAST databases, identity thresholds, report categories, and output tables.

The main marker parameters are:

- `--targets` — pipe-separated marker names, for example `COI|ITS2`.
- `--target_taxa` — expected broad taxonomic groups for the fast on-target screen, for example `Metazoa|Viridiplantae`.
- `--min_read_lengths` and `--max_read_lengths` — marker-specific read-length ranges.
- `--blast_db_specs` — one BLAST database prefix per marker.
- `--blast_filter_db` — the LAST/BLAST-style pre-filter database used in fast target screening.
- `--blast_taxdb` — the NCBI taxdb resource used for lineage retrieval.
- `--nonncbi_memtax` and `--nonncbi_id2lineage_target` — optional taxonomy lookup tables for non-NCBI or project-specific database records.

These parameters are pipe-aligned: the first value belongs to the first target, the second value to the second target, and so on. For example, if `--targets "COI|ITS2"`, then the first `--blast_db_specs` entry is the COI database and the second is the ITS2 database.

Taxonomic assignment depth is controlled by percent-identity thresholds:

- `--blast_id_family`
- `--blast_id_genus`
- `--blast_id_spec`

These thresholds determine whether a match is reported at family, genus, or species level. This is why reports can show the same evidence at different taxonomic levels: species-level labels are more specific, but genus or family may be more appropriate when marker resolution or database coverage is limited.

## 3. Fast Read Screening and Basecalling
[back to Top](#rtbioscan-pipeline-overview)

Raw POD5 files contain nanopore signal, not directly interpretable sequence tables. RTBioScan first performs a fast screening path to decide which [reads](concepts.md#reads-and-read-fate) are likely to be useful for the configured marker and taxon targets. Reads that pass this stage are considered [on-target reads](concepts.md#reads-and-read-fate) in the reports.

This stage balances speed and biological relevance:

- FAST basecalling quickly estimates read sequence for target detection.
- Marker and taxon filters remove obvious off-target data early.
- HAC basecalling is then used for the main high-quality read pool.
- SUP basecalling can be used later for consensus-support reads.

Important parameters include:

- `--fast_model`, `--hac_model`, and `--sup_model` — Dorado models used for each basecalling purpose.
- `--dorado_device` and `--dorado_bin` — where and how Dorado runs.
- `--on_target_quality_score` — minimum qscore for the fast screening pass.
- `--min_quality_score` — minimum HAC qscore for reads entering the high-quality rolling pool.
- `--hq_quality_score` — qscore threshold for consensus-support reads.
- `--min_read_length`, `--max_read_length`, `--min_read_lengths`, and `--max_read_lengths` — global and marker-specific length filters.

For the read-fate terms introduced by this stage, see [Reads and Read Fate](concepts.md#reads-and-read-fate).

## 4. Demultiplexing: Samples, Primers, and Tracked Groups
[back to Top](#rtbioscan-pipeline-overview)

Demultiplexing connects reads to biological [samples](concepts.md#samples-barcodes-primers-and-markers) or experimental groups. This is the step that makes the later report sections interpretable by sample, [primer](concepts.md#samples-barcodes-primers-and-markers), [replicate](concepts.md#samples-barcodes-primers-and-markers), or sample+replicate grouping.

RTBioScan supports different demultiplexing modes:

- `--demultiplex_mode auto` enables full demultiplexing when both barcode and primer FASTAs are available.
- `--demultiplex_mode full` requires barcode plus primer demultiplexing.
- `--demultiplex_mode primers_only` uses primer trimming without barcode-derived sample labels.
- `--demultiplex_mode off` disables demultiplexing.

The main runtime files are:

- `--indexes` — prepared barcode/sample FASTA, usually `results/sample_info/<run_id>/demult.fasta`.
- `--primer_indexes` — prepared primer FASTA, usually `results/sample_info/<run_id>/primers.fasta`.

This stage feeds the **Sample Details**, **Primer Comparison**, and **Replicate Comparison** reports. It also determines the read-count plots labelled `Reads per Barcode`, `Reads per Sample`, and the sample-level cards in the HTML reports. For the shared sample/barcode/primer terminology, see [Samples, Barcodes, Primers, and Markers](concepts.md#samples-barcodes-primers-and-markers).

## 5. OTU Definition
[back to Top](#rtbioscan-pipeline-overview)

After reads have been filtered and assigned to samples or tracked groups, RTBioScan clusters related reads into [OTUs](concepts.md#otus-and-consensus). An OTU is a practical read cluster used to summarize sequence evidence before taxonomic assignment and consensus generation.

The main clustering parameter is:

- `--otu_id` — sequence identity threshold for OTU clustering. The default/barcoding value is diversity-oriented; the `voucher` profile raises it for single-specimen reference-sequence generation.

Because RTBioScan runs repeatedly over accumulating data, OTUs also have state behavior such as [active OTUs](concepts.md#otus-and-consensus), [frozen OTUs](concepts.md#otus-and-consensus), [consolidated OTUs](concepts.md#otus-and-consensus), and [informative OTUs](concepts.md#otus-and-consensus).

Selected parameters that control OTU state and reporting include:

- `--otu_frozen_enabled`
- `--otu_frozen_min_rounds`
- `--otu_frozen_min_reads`
- `--otu_frozen_growth_window`
- `--otu_frozen_drop_ratio`
- `--otu_prune_frozen_policy`
- `--otu_consolidation_lock`
- `--otu_consolidation_mode`
- `--otu_lock_min_consolidated_reads`
- `--otu_lock_min_stable_rounds`

The output reports use these concepts in OTU cards, OTU taxonomy plots, frozen-OTU plots, OTU assignment tables, and OTU fate categories.

## 6. Taxonomic Assignment
[back to Top](#rtbioscan-pipeline-overview)

Taxonomic assignment connects sequence evidence to biological names. RTBioScan compares OTU-associated reads and consensus sequences against the configured marker-specific reference databases, then reports assignments at the deepest justified rank: species, genus, family, or unassigned.

The central idea for non-expert readers is that a name in the report is a database-supported interpretation of sequence similarity. It is stronger when supported by multiple reads, OTUs, consensus sequences, samples, or rounds, and weaker when based on sparse or ambiguous evidence.

Key assignment parameters include:

- `--blast_db_specs` — marker-specific BLAST databases.
- `--blast_id_family`, `--blast_id_genus`, `--blast_id_spec` — identity thresholds for assignment depth.
- `--blast_evalue` and `--blast_max_hsps` — BLAST hit-retention settings.
- `--otu_blast_min_members` — minimum OTU size used for OTU BLAST filtering.
- `--otu_blast_filter_mode` — whether OTU BLAST filtering is disabled, observed, or enforced.
- `--otu_blast_unassigned_mode` — handling mode for OTUs that reach BLAST but remain unassigned.
- `--assign_protection_level` — minimum assignment depth that protects reads or OTUs from later pruning decisions.

For assignment terms such as [BLAST-assigned](concepts.md#reads-and-read-fate), [BLAST-unassigned](concepts.md#reads-and-read-fate), [taxonomic level](concepts.md#taxonomy-and-reporting-aids), and species of interest, see [Concepts](concepts.md).

Read-fate charts in [output.md](output.md#main-plots-and-tables) summarize this path with categories such as `BLAST-assigned COI`, `BLAST-unassigned ITS2`, `BLAST skipped COI`, `On-target not demultiplexed`, and `Off-target`.

## 7. Handling Unassigned and Low-Support Evidence
[back to Top](#rtbioscan-pipeline-overview)

Unassigned reads and OTUs are not necessarily errors. They can reflect low read depth, poor sequence quality, missing database coverage, off-target amplification, or taxa outside the configured reference scope. RTBioScan keeps these signals visible in reports but also provides controls to prevent unbounded growth of low-support clusters during long runs.

Important controls include:

- `--prune_unassigned_clusters` — enable consensus-time pruning of unassigned clusters.
- `--prune_unassigned_grace_rounds` — allow early rounds to accumulate evidence before pruning starts.
- `--prune_unassigned_keep_top` — retain the strongest unassigned clusters when pruning is enabled.
- `--prune_unassigned_drop_reads` — prevent dropped unassigned reads from re-entering future consensus.
- `--otu_size_streak_mode` and `--otu_size_streak_min_rounds` — manage repeated low-size OTU patterns across rounds.
- `--otu_unassigned_streak_mode` and `--otu_unassigned_streak_min_rounds` — manage repeated unassigned OTUs across rounds.

For monitoring runs focused on known taxa, pruning can keep reports and runtimes focused. For exploratory runs where unknown sequences are important, retaining unassigned clusters may be more appropriate, but it can increase runtime as the run progresses.

## 8. Consensus Generation
[back to Top](#rtbioscan-pipeline-overview)

Consensus generation turns supported OTU evidence into representative [consensus sequences](concepts.md#otus-and-consensus). These sequences are the main reusable sequence outputs of the pipeline and are often the files external users inspect, export, or compare downstream.

Main parameters include:

- `--consensus_id` — identity threshold used inside consensus generation.
- `--consensus_reads_mode` — whether `reads-N` means representative-read count or cluster-total read count.
- `--consensus_min_reads` and `--consensus_max_reads` — minimum and maximum reads used per OTU.
- `--consensus_min_qscore` and `--consensus_consolidated_min_qscore` — quality thresholds for consensus input reads.
- `--consensus_max_N` — maximum ambiguous bases allowed in a consensus sequence.
- `--consensus_zero_emit_policy` — behavior when a round emits no consensus sequences.

Consensus results appear in FASTA outputs, consensus assignment tables, consensus treemaps, consensus fan cladograms, and consolidated-consensus plots.

## 9. Reports and External Interpretation
[back to Top](#rtbioscan-pipeline-overview)

The HTML reports are the main interface for external users. They translate pipeline state into visual summaries and tables that can be reviewed during or after sequencing.

The main report sections are:

- **Global Overview** — run-wide cards, read evolution, and latest-round result summaries.
- **Sample Details** — sample, primer, or replicate-level summaries depending on the report view.
- **Rounds Info** — per-round metrics and progression through time.
- **Additional Info** — figure gallery and exported visual assets.

The main report parameters are:

- `--html_report_enabled` — enable incremental report rendering.
- `--html_report_auto_refresh` — browser-side refresh polling.
- `--html_report_refresh_seconds` — refresh interval.
- `--html_report_sample_plot_max` — maximum number of samples with sample-specific figures per round.
- `--html_report_url_prefix` — URL/link prefix for served reports.
- `--serve`, `--serve-open`, and related `RTBioScan.sh` options — serve and open live reports.

Optional project-specific reporting inputs include:

- `--metazoa_spc_basics`
- `--viridiplantae_spc_basics`
- `--local_metazoa_gns`
- `--local_viridiplantae_gns`

These files can highlight or filter taxa of interest in reports, but the primary taxonomic assignment still comes from sequence comparison against the configured barcoding databases.

See [output.md](output.md#how-to-read-the-reports) for a reader-oriented explanation of report concepts, plot categories, and interpretation.

## 10. Outputs and State
[back to Top](#rtbioscan-pipeline-overview)

RTBioScan produces both human-facing and machine-readable outputs:

- live HTML reports under `results/report_html/`
- per-round JSON summaries, including `round_report.json`
- run-level JSON summaries, including `run_report.json`
- OTU membership and assignment TSVs
- demultiplexing summary tables
- consensus FASTA files
- rolling and stable state snapshots

For most users, the important locations are:

- `results/report_html/` — browsable report interface.
- `results/current/state/<state_id>/` — stable snapshot after the latest completed round.
- `results/ongoing/state/<state_id>/` — latest rolling user-facing tables, plots, and sequences.
- `results/temp/ongoing/state/<state_id>/<round_barcode>/` — per-round machine-readable outputs and audit files.

The cumulative-state design is what lets RTBioScan resume interrupted runs and continue refining results across rounds. It also explains why reports distinguish latest-round values, run-level totals, frozen OTUs, consolidated consensus, and read-fate categories.

## Profiles and Common Use Cases
[back to Top](#rtbioscan-pipeline-overview)

Profiles bundle parameter choices for common contexts:

- `conda` is an explanatory disabled stub. Install and activate a committed
  platform lock, then run without an execution profile.
- `docker` and `singularity` are explanatory erroring stubs until RTBioScan
  publishes and validates its own container image.
- `test` points to the test database configuration.
- `barcoding` is intended for batch-mode diversity runs from pre-collected POD5 files.
- `voucher` is intended for reference-sequence generation from a known specimen.
- `xprize` enables deployment-specific databases and optional reporting helper tables.

The most important conceptual difference is between environmental metabarcoding and voucher generation:

| | Environmental / barcoding use | Voucher use |
|---|---|---|
| Input | Mixed biological material or environmental sample | Single known individual |
| Aim | Detect and monitor taxa supported by reference databases | Produce a high-quality reference sequence |
| OTU clustering | Diversity-oriented | Tighter, single-specimen-oriented |
| Unassigned evidence | Often controlled to keep monitoring focused | Often expected and retained |
| Main output | Reports, taxa, consensus files | Exportable consensus sequence |

Choose the profile first, then adjust parameters only where the scientific aim requires it. For exact command examples and defaults, see [usage.md](usage.md).
