# RTBioScan: Output

> **Docs:** [Index](README.md) · [Installation](installation.md) · [Usage](usage.md) · **Output** · [Report Schema](report_schema.md) · [Pipeline Overview](pipeline.md)

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

---

## Per-round outputs
[back to Top](#rtbioscan-output)

Each analysis round produces a directory under `results/temp/ongoing/state/<state_id>/`:

```
results/temp/ongoing/state/<state_id>/
  <round_barcode>/
    round_report.json                             ← machine-readable round metrics (schema v1.6)
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

Machine-readable metrics for each round, consumed by the HTML report renderer. Schema version: `1.6`. Key namespaces: `reads`, `otu`, `blast`, `consensus`, `read_fate`, `sample_metrics`, and `figures`. See `report_schema.md` for the full field reference.

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
