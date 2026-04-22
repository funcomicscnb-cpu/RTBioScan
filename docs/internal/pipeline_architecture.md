# RTBioScan: Internal Pipeline Architecture

> **Internal Docs:** [Index](README.md) · **Architecture** · [Performance Backlog](performance_pr_backlog.md) · [Release Preparation](release_preparation.md)

> **Audience:** Maintainers and developers. This page is not intended for the public end-user documentation set.

## Purpose

This page keeps the implementation-oriented context that is useful when debugging, extending, or testing RTBioScan.

The public page [../pipeline.md](../pipeline.md) intentionally stays at workflow level. Detailed process naming, runtime-state layout, and maintenance concerns belong here.

## Core runtime model

RTBioScan is built around three invariants:

1. Processing is round-based. Each incoming chunk is handled as one round.
2. State is cumulative across rounds. A later round depends on the saved result of earlier rounds.
3. Resume and restart behavior depend on preserving that state layout and the atomic update order.

Any code change that alters round boundaries, state carry-forward, restore paths, output naming, or locking semantics is high risk.

## Internal stage map

At a maintainer level, the main execution path is:

```text
POD5 intake
  -> fast_on_target_detection
  -> hac_basecalling
  -> demultiplexing_hq_reads
  -> OTU_definition
  -> blast_OTU_pretax
  -> consensus
  -> getting_run_summary
  -> backup_update_and_clean
```

Supporting reporting helpers and wrapper-side feeder/server logic run around that main path.

## Runtime directories that matter for debugging

`results/pod5/<run_id>/`

- feeder intake, staged chunks, processed chunks, and feeder metadata

`results/sample_info/<run_id>/`

- run-filtered metadata and demultiplexing support files prepared for the run

`results/temp/ongoing/state/<state_id>/`

- active runtime state for the current round sequence
- `_state/` under this tree is the main carry-forward state between rounds

`results/temp/current/state/<state_id>/`

- restore snapshot used by restart/restore behavior

`results/ongoing/state/<state_id>/` and `results/current/state/<state_id>/`

- user-facing rolling and stable exports derived from the active state

## Debugging focus by symptom

If new rounds are not appearing:

- check feeder intake under `results/pod5/<run_id>/`
- check wrapper arguments and `run_id` alignment
- confirm POD5 staging and chunk promotion are advancing

If demultiplexing looks wrong:

- check the prepared metadata and FASTA files under `results/sample_info/<run_id>/`
- verify label conventions and sample roster outputs
- compare public input-format requirements in [../usage.md](../usage.md#input-file-formats)

If resume or restart behaves incorrectly:

- inspect `results/temp/ongoing/state/<state_id>/`
- inspect `results/temp/current/state/<state_id>/`
- confirm the round snapshot and cleanup order has not changed

If reports look inconsistent:

- compare `round_report.json`, `run_report.json`, and the exported figures
- distinguish public schema regressions from internal helper-artifact regressions

## Testing and extension guidance

When extending the pipeline:

- preserve round/state semantics first
- prefer small, stage-local changes
- validate resume behavior and output naming
- treat state-file additions as API-like changes for maintainers

When adding tests, prioritize:

- round transition integrity
- restore/resume correctness
- cumulative-state conservation
- report-schema stability for public outputs
- helper-artifact stability only when the change directly touches internal machinery

The active improvement queue is kept in [performance_pr_backlog.md](performance_pr_backlog.md).
