# Report Schema (`round_report.json`)

> **Docs:** [Index](README.md) · [Pipeline Overview](pipeline.md) · [Concepts](concepts.md) · [Installation](installation.md) · [Usage](usage.md) · [Output](output.md) · **Report Schema**

Each round writes one JSON object with `schema_version`.

Current schema version: `1.6`.

This page is the machine-readable JSON contract. For biological and report terminology, see [Concepts](concepts.md). For user-facing report interpretation and output locations, see [Output](output.md).

## Required top-level keys
[back to Top](#report-schema-round-reportjson)

- `schema_version` (string)
- `run_id` (string)
- `barcode` (string)
- `round_barcode` (string)
- `timestamp_utc` (string, ISO-8601 UTC)
- `warnings` (array of strings)

## Optional top-level keys
[back to Top](#report-schema-round-reportjson)

- `state_id` (string or null)
- `figures` (array of figure objects; run-level figures attached to the round payload)

## Metric namespaces
[back to Top](#report-schema-round-reportjson)

- `reads`
  - `total` (integer or null)
  - `on_target` (integer or null)
  - `hac` (integer or null)
  - `sup` (integer or null)
- `otu`
  - `canonical`
    - `active` (integer or null; active OTU count used for public reporting)
    - `consolidated` (integer or null)
    - `frozen_not_consolidated` (integer or null)
    - `active_not_frozen` (integer or null; OTU Fate universe excluding consolidated and frozen-not-consolidated lock-state OTUs)
    - `informative_dynamic` (integer or null; active-not-frozen OTUs not in prune/pruned buckets)
  - `pruned`
    - `prune_candidates` (integer or null; OTUs retained under grace/skip windows)
    - `size_streak` (integer or null; OTUs pruned by the unified size-streak rule when applied; otherwise size-streak OTUs are counted under `prune_candidates`)
    - `blast_unassigned` (integer or null; OTUs seen in BLAST output but without assignment)

OTU Fate categories are computed within the `active_not_frozen` OTU universe and are
mutually exclusive. Precedence is: `blast_unassigned` (or `prune_candidates` when BLAST
grace is active) → `size_streak` → `prune_candidates` (size-streak candidates under grace)
→ `informative_dynamic`. OTUs that would be pruned but are
still within a grace/skip window are counted under `prune_candidates`.
  - `diagnostic`
    - `blast_rows_total` (integer or null)
    - `blast_otu_total` (integer or null)
    - `lock_active_not_frozen` (integer or null; diagnostic active-not-frozen count)
    - `size_streak_possible` (boolean; size inputs present and `otu_blast_min_members >= 2`)
    - `size_streak_applied` (boolean; size-streak pruning applied this round)
    - `size_streak_grace_active` (boolean; size-grace window active)
    - `size_streak_grace_source` (string; currently `otu_blast_filter_skip_rounds`)
    - `fate_universe_source` (string or null)
    - `fate_universe_reason` (string or null)
    - `fate_universe_sizes_status` (string or null; `ok|empty|missing|unreadable|invalid`)
    - `fate_universe_otu_def_status` (string or null)
    - `fate_conservation_ok` (boolean or null)
    - `fate_conservation_delta` (integer or null; only set when conservation mismatch is detected)
  - `prune_candidates_round`
    - `active_scope` (string or null; currently `round`)
    - `active_scope_reason` (string or null; `round_local_ok|round_local_empty|round_local_unavailable`)
    - `active_total` (integer or null)
    - `size_streak_input_status` (string or null; `ok|empty|missing|unreadable|invalid`)
    - `size_streak_possible` (integer or boolean or null)
    - `size_streak_applied` (integer or boolean or null)
    - `size_streak_active` (integer or null)
    - `size_streak_candidates` (integer or null)
    - `size_streak_round_candidates` (integer or null)
    - `size_streak_eligible_candidates` (integer or null)
    - `size_streak_disabled` (integer or boolean or null)
    - `effective_mode` (string or null; `off|observe|enforce`)
    - `effective_reason` (string or null)
    - `round_index` (integer or null)
    - `union` (integer or null)
    - `blast_unassigned_candidates` (integer or null)
    - `blast_unassigned_status` (string or null)
    - `consensus_unassigned_candidates` (integer or null)
    - `otu_unassigned_streak_candidates` (integer or null)
- `blast`
  - `filtered_reads` (integer or null)
  - `filtered_otus` (integer or null)
  - `mode` (string or null; `off|observe|enforce`)
  - `missing_policy` (string or null; `keep|drop`)
- `consensus`
  - `emitted` (integer or null)
  - `consolidated` (integer or null)
- `read_fate`
  - `demux_total_reads` (integer or null; unique normalized demux read IDs)
  - `no_adapter_reads` (integer or null; unique normalized demux read IDs resolved to `no_adapter`)
  - `demux_enabled` (boolean or null)
  - `blast_seen_reads` (integer or null; unique normalized BLAST read IDs seen in `blast_otu`)
  - `blast_assigned_reads` (integer or null; unique normalized BLAST read IDs with merged assignment state `assigned`)
  - `blast_unassigned_reads` (integer or null; unique normalized BLAST read IDs with merged assignment state `unassigned`)
  - `blast_seen_reads_unbucketed` (integer or null; merged BLAST reads whose final bucket state is `unresolved` or `conflict`)
  - `marker_split_status` (`ok|invalid`)
  - `data_reason_codes` (array of strings)
  - `chart_reason_codes` (array of strings)
  - `marker_split_invalid_read_count` (integer; deduped read-level invalid count, or `0` for globally invalid rounds with no bad reads)
  - `marker_split_warning_counts` (fixed object)
  - `marker_split_fatal_counts` (fixed object)
  - `demux_total_reads_coi` (integer or null)
  - `demux_total_reads_its2` (integer or null)
  - `blast_seen_reads_coi` (integer or null)
  - `blast_seen_reads_its2` (integer or null)
  - `blast_assigned_reads_coi` (integer or null)
  - `blast_assigned_reads_its2` (integer or null)
  - `blast_unassigned_reads_coi` (integer or null)
  - `blast_unassigned_reads_its2` (integer or null)
  - `chart_blast_assigned_coi` (integer or null)
  - `chart_blast_assigned_its2` (integer or null)
  - `chart_blast_unassigned_coi` (integer or null)
  - `chart_blast_unassigned_its2` (integer or null)
  - `chart_blast_skipped_coi` (integer or null)
  - `chart_blast_skipped_its2` (integer or null)
  - `chart_on_target_not_demultiplexed` (integer or null)
  - `chart_off_target` (integer or null)

Read-fate charts and TSV exports must be rendered directly from the stored `chart_*` fields in this exact order:
1. `BLAST-assigned COI`
2. `BLAST-assigned ITS2`
3. `BLAST-unassigned COI`
4. `BLAST-unassigned ITS2`
5. `BLAST skipped COI`
6. `BLAST skipped ITS2`
7. `On-target not demultiplexed`
8. `Off-target`

This page documents the stable public JSON contract. Internal helper TSVs and intermediate diagnostics used to generate these fields are intentionally not described here.

### `consensus.emitted_by_marker_taxon`
[back to Top](#report-schema-round-reportjson)

Per-round consensus emitted breakdown by marker and assignment status.

Fields (integers or `null` if unavailable):
- `coi_assigned`
- `its2_assigned`
- `coi_unassigned`
- `its2_unassigned`
- `other_assigned`
- `other_unassigned`

### `otu.active_by_marker_taxon`
[back to Top](#report-schema-round-reportjson)

Per-round **informative OTU** breakdown by marker and assignment status.
Informative OTUs are the union of:
- consolidated OTUs
- frozen (not consolidated) OTUs
- informative_dynamic OTUs

Fields (integers or `null` if unavailable):
- `coi_assigned`
- `its2_assigned`
- `coi_unassigned`
- `its2_unassigned`
- `other_assigned`
- `other_unassigned`

### `otu.assignments_by_level`
[back to Top](#report-schema-round-reportjson)

Per-round OTU taxonomic assignment summary for the latest round in run reports.
Structured as `{species, genus, family}` arrays with rows **aggregated by taxon + sample + marker**.
Rows are sorted by `reads_total` (desc), then `otu_count` (desc), then taxon.
Each level is capped to the top 200 rows.

Each row includes:
- `taxon` (string; taxon name for the level)
- `sample` (string; may be empty when demux is off)
- `marker` (string; COI/ITS2/Other)
- `otu_count` (integer)
- `frozen_otu_count` (integer)
- `frozen_otu_reads_total` (integer or null; reads assigned to frozen OTUs in this row)
- `reads_total` (integer or null)
- `perc_id_min` / `perc_id_max` (number or null)
- `aln_length_min` / `aln_length_max` (number or null)
- `species_interest` (boolean; only present on species-level rows when a species-of-interest list is provided)

Additional field:
- `species_interest_enabled` (boolean; true when a species-of-interest list was provided)

### `consensus.assignments_by_level`
[back to Top](#report-schema-round-reportjson)

Per-round consensus taxonomic assignment summary for the latest round in run reports.
Structured as `{species, genus, family}` arrays with rows **aggregated by taxon + sample + marker**.
Rows are sorted by `reads_total` (desc), then `consensus_count` (desc), then taxon.
Each level is capped to the top 200 rows.

Each row includes:
- `taxon` (string; taxon name for the level)
- `sample` (string; may be empty when demux is off)
- `marker` (string; COI/ITS2/Other)
- `consensus_count` (integer)
- `consolidated_consensus_count` (integer)
- `consolidated_consensus_reads_total` (integer or null; reads assigned to consolidated consensus in this row)
- `reads_total` (integer or null)
- `perc_id_min` / `perc_id_max` (number or null)
- `aln_length_min` / `aln_length_max` (number or null)
- `species_interest` (boolean; only present on species-level rows when a species-of-interest list is provided)

Additional field:
- `species_interest_enabled` (boolean; true when a species-of-interest list is provided)

## Sample Metrics (`sample_metrics`)
[back to Top](#report-schema-round-reportjson)

Optional object keyed by deterministic label-based `sample_id` (trimmed label + short hash, case-sensitive, emitted by collector). Sample labels are normalized so `no_adapter` and `no_adapter_*` collapse to `no_adapter` across FASTQ headers, reporting TSVs, and reports.

- `sample_metrics.<sample_id>.sample_id` (string; deterministic key)
- `sample_metrics.<sample_id>.label` (string; display label)
- `sample_metrics.<sample_id>.reads_demux` (integer or null)
- `sample_metrics.<sample_id>.reads_demux_coi` (integer or null)
- `sample_metrics.<sample_id>.reads_demux_its2` (integer or null)
- `sample_metrics.<sample_id>.reads_blast_assigned` (integer or null)
- `sample_metrics.<sample_id>.otu_active` (integer or null)
- `sample_metrics.<sample_id>.consensus_emitted` (integer or null)
- `sample_metrics.<sample_id>.replicates` (object keyed by original replicate label, optional)
  - `label` (string)
  - `reads_demux` (integer or null)
  - `reads_blast_assigned` (integer or null)
  - `otu_active` (integer or null)
  - `consensus_emitted` (integer or null)
- `sample_metrics.<sample_id>.figures` (array; sample-scoped figure objects, optional)
  - `id` (string)
  - `title` (string)
  - `description` (string or null)
  - `path` (string)
  - `pdf_path` (string or null)
  - `exists` (boolean)
  - `pdf_exists` (boolean)
  - `section` (string)
  - `order` (integer)

## Figure objects
[back to Top](#report-schema-round-reportjson)

Each entry in `figures[]` may include:
- `id` (string)
- `title` (string)
- `description` (string or null)
- `path` (string)
- `pdf_path` (string or null)
- `exists` (boolean)
- `pdf_exists` (boolean)
- `section` (string; e.g. `Global Overview`, `Read QC / FAST`, `Demultiplexing`, `OTU Definition`, `Consensus`, `Other`)
- `order` (integer; used for sorting within section)

## Run Report (`run_report.json`)
[back to Top](#report-schema-round-reportjson)

Each round directory also contains `run_report.json`, a run-level aggregate derived from `report_history.jsonl`. During `--serve` startup it may be seeded before the first completed round with `rounds_count: 0` and `status_label: "Fresh"`.

Required keys:
- `schema_version` (string)
- `run_id` (string)
- `rounds_count` (integer)
- `last_updated_utc` (string, ISO-8601 UTC)

Common optional keys:
- `barcode` (string)
- `state_id` (string)
- `identity_mode` (string; `collapse|track`)
- `outdir` (string)
- `started_utc` (string, ISO-8601 UTC)
- `last_round_barcode` (string)
- `last_round_timestamp_utc` (string, ISO-8601 UTC)
- `report_rel_path` (string)
- `report_url` (string)
- `report_views` (array; emitted in track mode when alternate run views are available)
- `last_round_status` (string; usually `ok|failed`)
- `last_round_failure_reason` (string; present when `last_round_status = failed`)
- `status` (string; currently `running`)
- `status_label` (string; e.g. `Fresh|Aging|Stale`)
- `status_color` (string; renderer hint)
- `status_cadence_seconds` (number or null)
- `status_age_seconds` (number or null)
- `run_summary_source_round` (string; round used for the current summary cards)
- `run_summary` (object with latest-round `reads`, `otu`, `consensus`, `read_fate` snapshots)

When `report_views` is present, each entry includes:
- `view_id` (string; `sample|replicate|track_detail`)
- `label` (string; current writer uses `Run Info`, `Primer Comparison`, `Replicate Comparison`)
- `report_rel_path` (string)
- `report_url` (string)
- `is_primary` (boolean)

## History key (resume-safe)
[back to Top](#report-schema-round-reportjson)

`run_id + barcode + round_barcode`

This key is used to replace an existing row for the same round instead of duplicating it.

## Renderer ordering
[back to Top](#report-schema-round-reportjson)

The report renderer sorts rounds by:
1. `timestamp_utc` ascending (missing/invalid timestamps last)
2. `round_barcode`
3. `barcode`

## Runs index (`runs_index.jsonl`)
[back to Top](#report-schema-round-reportjson)

Each line is a JSON object describing one run. These entries drive the Runs table in the dashboard.

Required keys:
- `run_id` (string)
- `last_updated_utc` (string, ISO-8601 UTC)
- `rounds_count` (integer)

Optional keys:
- `schema_version` (string; current writer uses `1.6`)
- `barcode` (string)
- `state_id` (string)
- `identity_mode` (string; `collapse|track`)
- `outdir` (string)
- `report_rel_path` (string; e.g. `runs/<run_id>/report.html`)
- `report_url` (string; defaults to `report_rel_path`)
- `report_views` (array; same structure as `run_report.json`, present in track mode)
- `started_utc` (string, ISO-8601 UTC) — timestamp of the first processed data in the run
- `last_round_barcode` (string)
- `last_round_timestamp_utc` (string, ISO-8601 UTC)
- `last_round_status` (string; usually `ok|failed`)
- `last_round_failure_reason` (string; present when `last_round_status = failed`)
- `status` (string; current writer uses `running`)
- `status_label` (string; `Fresh|Aging|Stale` when cadence is available)
- `status_color` (string; `green|orange|red`)
- `status_age_seconds` (integer or null; seconds since last update)
- `status_cadence_seconds` (integer or null; expected cadence between updates)
- `run_summary_source_round` (string; round_barcode used to compute `run_summary`)
- `run_summary` (object; latest round snapshot for index charts)
  - `reads`
    - `total` (integer or null)
    - `on_target` (integer or null)
  - `read_fate`
    - `demux_total_reads` (integer or null)
    - `no_adapter_reads` (integer or null)
    - `demux_enabled` (always null at run level)
    - `blast_seen_reads` (integer or null)
    - `blast_seen_reads_unbucketed` (integer or null)
    - `blast_assigned_reads` (integer or null)
    - `blast_unassigned_reads` (integer or null)
    - `marker_split_status` (`ok|invalid`)
    - `data_reason_codes` (array of strings)
    - `chart_reason_codes` (array of strings)
    - `marker_split_invalid_read_count` (integer; round-summed on invalid runs)
    - `marker_split_warning_counts` (fixed object)
    - `marker_split_fatal_counts` (fixed object)
    - marker-aware raw fields and `chart_*` fields with the same names as the per-round object
  - `otu`
    - `active_by_marker_taxon` (object; same fields as per-round `otu.active_by_marker_taxon`)
  - `consensus`
    - `emitted_by_marker_taxon` (object; same fields as per-round `consensus.emitted_by_marker_taxon`)

Renderer ordering for runs:
1. `last_updated_utc` descending (missing/invalid timestamps last)
2. `run_id`
