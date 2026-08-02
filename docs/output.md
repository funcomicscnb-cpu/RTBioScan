# RTBioScan: Output

> **Docs:** [Index](README.md) · [Pipeline Overview](pipeline.md) · [Concepts](concepts.md) · [Installation](installation.md) · [Usage](usage.md) · **Output** · [Report Schema](report_schema.md)

This page describes the output produced by the pipeline. The default output directory is `results/` (configurable with `--outdir`).

## Overview
[back to Top](#rtbioscan-output)

RTBioScan produces two main kinds of output:

- **HTML reports** — live, auto-refreshing visual reports updated after every round.
- **Per-round and cumulative result files** — TSV/FASTA/JSON files for downstream analysis, mainly under the run-specific results directories.

---

## HTML Reports
[back to Top](#rtbioscan-output)

HTML reports are written under `results/report_html/` and updated after each analysis round. The default report links are relative (`--html_report_url_prefix ""`), so the files can be browsed directly; serve them over HTTP when you want auto-refresh or when you explicitly configure root-relative URLs.

```
results/
  report_html/
    report.html                               ← cross-run index report
    report_state.json                         ← auto-refresh sidecar for the index
    runs_index.jsonl                          ← runs index (one entry per run)
    runs/
      <run_id>/
        report.html                           ← Run Info (or the only run report in collapse mode)
        report_state.json
        report_replicates.html                ← Primer Comparison (track mode only)
        report_replicates_state.json
        report_replicates_primers.html        ← Replicate Comparison (track mode only)
        report_replicates_primers_state.json
        report_assets/                        ← figures/assets for Run Info
        report_assets_replicates/             ← figures/assets for Primer Comparison
        report_assets_replicates_primers/     ← figures/assets for Replicate Comparison
        figures/                              ← TSV/chart exports for Run Info
        figures_replicates/                   ← TSV/chart exports for Primer Comparison
        figures_replicates_primers/           ← TSV/chart exports for Replicate Comparison
```

The index report (`report_html/report.html`) is cross-run and shows a runs table (latest-updated first) with summary metrics from each run's latest round.

Per-run report views are:

- **Run Info** (`report.html`) — the default per-run view. In track mode it groups tracked units back to the sample-level view.
- **Primer Comparison** (`report_replicates.html`) — available in track mode only. This regroups tracked metrics for primer-level comparison.
- **Replicate Comparison** (`report_replicates_primers.html`) — available in track mode only. This regroups tracked metrics at the sample+replicate view.

All per-run views share the same top-level page sections:

- **Global Overview** — summary cards plus run-evolution and latest-round result panels.
- **Sample Details** — the main grouped-entity section. The heading stays the same, but the contents are grouped by sample, primer, or sample+replicate depending on the current report view.
- **Rounds Info** — round-level charts and the rounds table.
- **Additional Info** — figure gallery and exported visual assets.

### How to read the reports
[back to Top](#rtbioscan-output)

The reports are intended to answer three external-user questions:

1. **How much usable sequence data has the run produced?** Read-count plots and cards show how many reads were generated, how many were on-target, how many were assigned to samples, and how many reached the taxonomic-assignment stage.
2. **Which biological groups were detected?** OTU and consensus plots summarize assigned taxa by marker and taxonomic level. They are designed for screening and monitoring, not as a substitute for expert taxonomic review.
3. **How stable are the results as the run progresses?** Round-evolution plots show how reads, OTUs, consensus sequences, and assignments accumulate or stabilize over time.

RTBioScan is a round-based pipeline. Each round processes the reads available at that point, updates the cumulative state, and refreshes the reports. Later rounds can carry forward evidence from earlier rounds, so the latest report is usually the best entry point for interpreting the run.

### Report terminology
[back to Top](#rtbioscan-output)

Report labels use the shared RTBioScan terminology for runs, rounds, state, samples, barcodes, primers, markers, reads, OTUs, consensus sequences, taxonomic levels, and read-fate categories. See [Concepts](concepts.md) for the canonical definitions.

### Main plots and tables
[back to Top](#rtbioscan-output)

**Global Overview** summarizes the whole run. The read-evolution plots show whether the run is still accumulating data and whether enough reads are reaching downstream analysis. OTU and consensus panels show how many taxonomic units or representative sequences are currently assigned or unassigned for each marker.

**Read-fate charts** describe where reads end up in the pipeline. The categories distinguish reads assigned by marker, reads that reached BLAST but remained unassigned, reads skipped before BLAST, reads that were on-target but not demultiplexed, and off-target reads. These charts are useful for diagnosing whether missing taxa are more likely caused by low input, demultiplexing, target filtering, or database assignment.

**Assignments by Sample** compares taxa across samples or tracked groups. Users can switch between OTU counts, OTU-supported reads, consensus counts, and consensus-supported reads, then choose species, genus, or family level. In this matrix, larger values indicate stronger evidence in that sample. Low-read OTU assignments are shown separately from supported assignments, because they may need more cautious interpretation.

**Taxonomic sunbursts, treemaps, and fan cladograms** provide visual summaries of taxonomic composition. Sunbursts show hierarchy from broader to narrower ranks. Treemaps emphasize abundant taxa. Fan cladograms show taxonomic structure with weights based on OTUs, reads, or consensus counts depending on the plot.

**Sample Details** provides the same concepts at sample, primer, or replicate level. Use this section to check whether a taxon is broadly present across the run or concentrated in one sample or tracked group.

**Rounds Info** lists each processed round and the metrics recorded at that point. Use it to see whether detections are persistent across rounds or only appear transiently.

**Additional Info** contains the exported figures and links to downloadable plot assets. These are useful for reports, presentations, and external review.

### Interpreting taxonomic results
[back to Top](#rtbioscan-output)

Taxonomic names in the reports come from the configured barcoding databases and taxonomy resources. Observational resources such as GBIF or iNaturalist-derived lists can support filtering, highlighting, or ecological context, but they do not replace the primary sequence-based assignment.

External users should interpret the reports as evidence summaries:

- A taxon supported by multiple reads, OTUs, consensus sequences, samples, or rounds is generally stronger evidence than a taxon seen once.
- Species-level labels depend on marker resolution and database coverage. Genus or family-level interpretation may be more appropriate when species-level matches are weak or ambiguous.
- Unassigned reads and OTUs are informative: they can indicate poor database coverage, low-quality sequence, off-target amplification, or taxa outside the configured reference scope.
- Round-based reports can change as more reads arrive. The latest completed round is the most current view, while earlier rounds explain how the result developed.

---

## Per-round outputs
[back to Top](#rtbioscan-output)

Each analysis round produces a directory under `results/temp/ongoing/state/<state_id>/`:

```
results/temp/ongoing/state/<state_id>/
  <round_barcode>/
    round_report.json                             ← machine-readable round metrics (schema v2.0)
    run_report.json                               ← run-level summary JSON derived from history after this round
    <barcode>_summary_demult_rpt.txt              ← demultiplexing read counts
    <barcode>_blast_otu_pretax_rpt.txt            ← BLAST OTU assignments (round)
    otu_members.tsv                               ← OTU membership (canonical, phase-A)
    otu_sizes.tsv                                 ← OTU sizes (canonical)
    Consensus/
      <sample>/
        *.consensus.fasta                         ← per-OTU consensus sequences
      consolidated_consensus_ids.txt              ← IDs of consolidated consensus sequences
```

`round_barcode` is a string of the form `<name>_<barcode>_<index>` identifying the round.

Additional audit and diagnostic files may also be present in the round directory. Those are mainly intended for troubleshooting and are not the primary public interface of the pipeline.

### `round_report.json`
[back to Top](#rtbioscan-output)

Machine-readable metrics for each round, consumed by the HTML report renderer. Schema version: `2.0`. Key namespaces: `reads`, `otu`, `blast`, `consensus`, `read_fate`, `sample_metrics`, and `figures`. See `report_schema.md` for the full field reference.

### `run_report.json`
[back to Top](#rtbioscan-output)

Run-level summary generated after `round_report.json` is appended into history. It is the source for:

- `results/report_html/runs_index.jsonl`
- the cross-run dashboard (`results/report_html/report.html`)
- the per-run dashboard header cards and latest-round summary state

Key top-level fields include `run_id`, `barcode`, `state_id`, `identity_mode`, `rounds_count`, `started_utc`, `last_updated_utc`, `last_round_barcode`, `report_rel_path`, `report_url`, `report_views`, `status`, `status_label`, `status_color`, and `run_summary`.

When `--serve` starts before the first completed round, `run_report.json` can be present with `rounds_count: 0` and `status_label: "Fresh"`.

### Consensus sequences
[back to Top](#rtbioscan-output)

Consensus FASTA headers follow the format:

```
>OTUB_<N>-<MARKER>-<sample> reads-<N> [<taxonomy>]
```

`reads-N` meaning is controlled by `--consensus_reads_mode`:
- `representative` (default): read count of the selected OTU representative.
- `cluster_total`: sum of reads across all merged cluster members.

---

## Cumulative state
[back to Top](#rtbioscan-output)

Cumulative runtime state is maintained automatically so that later rounds can build on earlier rounds and interrupted runs can be resumed.

For most users, these internal state directories are not the main interface of the pipeline. Prefer the HTML reports, per-round exports, consensus FASTA files, and the stable snapshots described below.

## Snapshot directories
[back to Top](#rtbioscan-output)

The pipeline keeps both rolling and stable snapshots:

```
results/
  ongoing/state/<state_id>/                      ← latest user-facing rolling tables, plots, and sequences
  current/state/<state_id>/                      ← stable snapshot after the last completed round
  temp/current/state/<state_id>/                 ← restore snapshot used internally by `--restart_mode restore`
```

For downstream browsing and reuse, `results/ongoing/state/<state_id>/` and `results/current/state/<state_id>/` are usually the most useful locations.

---

## Key output files
[back to Top](#rtbioscan-output)

### BLAST OTU report (`<barcode>_blast_otu_pretax_rpt.txt`)
[back to Top](#rtbioscan-output)

Tab-separated. Each row is a read assigned to an OTU with BLAST taxonomy. Key columns: `read_id`, `otu_id`, `taxid`, `kingdom`, `lineage`, `pident`, `aln_len`. Per-round copies are useful for auditing and comparing round-to-round changes.

### OTU membership (`otu_members.tsv`)
[back to Top](#rtbioscan-output)

Two-column TSV: `otu_key`, `read_id`. Canonical source for which reads belong to which OTU in each round. Derived from the OTU-definition report, independent of BLAST filtering.

### Demultiplexing summary (`<barcode>_summary_demult_rpt.txt`)
[back to Top](#rtbioscan-output)

Per-sample read counts from the cutadapt demultiplexing step.

### Consensus FASTA (`Consensus/<sample>/*.consensus.fasta`)
[back to Top](#rtbioscan-output)

One FASTA file per OTU with a BLAST-assigned consensus sequence. These are the final taxonomically-annotated representative sequences for each OTU.

---

## Resuming a run
[back to Top](#rtbioscan-output)

Use `-resume` with Nextflow to continue an interrupted run. The pipeline restores its saved runtime state and resumes from the last completed round. The report history deduplicates by `run_id + barcode + round_barcode`, so resumed rounds do not create duplicate report entries.

The `--restart_mode` parameter provides additional recovery options:
- `off` (default): normal run.
- `restore`: restore rolling state from `results/temp/current` into `results/temp/ongoing` once.
- `reset`: wipe `results/temp/current` and `results/temp/ongoing` once.
