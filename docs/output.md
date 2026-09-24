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
    round_report.json                             ← machine-readable round metrics (schema v2.1)
    run_report.json                               ← run-level summary JSON derived from history after this round
    <barcode>_summary_demult_rpt.txt              ← demultiplexing read counts
    <barcode>_blast_otu_pretax_rpt.txt            ← BLAST OTU table: one row per canonical NR membership relation
    <barcode>_blast_otu_reporting_v1.tsv          ← sealed R4-D reporting sidecar (internal status/depth/evidence)
    otu_members.tsv                               ← OTU membership (canonical, phase-A)
    otu_sizes.tsv                                 ← OTU sizes (canonical)
    Consensus/
      <sample>/
        *.consensus.fasta                         ← per-OTU consensus sequences
      consolidated_consensus_ids.txt              ← IDs of consolidated consensus sequences
```

`round_barcode` is a string of the form `<name>_<barcode>_<index>` identifying the round.

Additional audit and diagnostic files may also be present in the round directory. Those are mainly intended for troubleshooting and are not the primary public interface of the pipeline.

### Split-child diagnostics

Dorado split children have their own SAM QNAMEs; the `pi:Z:` tag identifies the raw parent read. Raw POD5 contains the parent identity, so a child QNAME cannot be requested from it by exact ID. RTBioScan explicitly excludes FAST split-child target relations before HAC, without expanding to parents or siblings. Excluded children retain the existing `OFF_TARGET` value in the on-target report.

When exclusions occur, `results/temp/ongoing/state/<state_id>/<round_barcode>/<barcode>_split_children_excluded.list` records headerless tab-separated `child_read_id`, `parent_read_id`, and `marker` columns, in original target-relation order (including duplicates). This is troubleshooting provenance, not a stable public schema or taxonomic result. The file is absent when no targeted split children were excluded. If all targets are excluded, the existing no-target failed-round route applies.

HAC and newly generated SUP SAMs can independently contain split children. Their warnings count unique child QNAMEs and are diagnostic only: exact-ID membership remains unchanged, with no parent or sibling admission. SUP cache-only paths do not generate this warning.

### `round_report.json`
[back to Top](#rtbioscan-output)

Machine-readable metrics for each round, consumed by the HTML report renderer. Schema version: `2.1` (additive over `2.0`: the `taxonomy_assignment` namespace carries explicit canonical-membership denominators, and OTU/consensus assignment predicates use validated status and lineage instead of taxid positivity, so signed synthetic assignments count). Key namespaces: `reads`, `otu`, `blast`, `consensus`, `read_fate`, `taxonomy_assignment`, `sample_metrics`, and `figures`. See `report_schema.md` for the full field reference.

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

Tab-separated, 17 columns (`read_id`, `barcode_by_homology`, `basecalling_model`,
`sample`, `hit_id`, `taxid`, `aln_length`, `perc_id`, `otu_id`, `otu_taxid`,
`otu_kingdom` … `otu_species`). Since R4-D the table holds **exactly one row per
canonical NR sequence-to-OTU membership relation** of the complete current
membership (accumulated frozen plus active relations, the same population as
`otu_members_round.tsv`): every canonical member appears once, including members
without any BLAST hit, and raw observations that are not canonical members (exact
duplicates, direct-hit non-members) never appear. `read_id` is the member's
full header plus its `OTUB_N-<marker>` display token (the best available model
alias when a read was re-basecalled); `otu_id` stays the round-local display
identity; `otu_taxid` and `otu_kingdom` … `otu_species` are the validated R4-B OTU
assignment that every member inherits (`Unassigned` in all seven ranks when the
OTU status is not `ASSIGNED`/`AMBIGUOUS_TIE`; `NA` marks a rank gap inside a
usable partial lineage, e.g. a family-only assignment).

`hit_id`, `taxid`, `aln_length` and `perc_id` describe **that member's own sealed
direct evidence only** (R4-A all-evidence state): `hit_id` is the subject
accession when exactly one equal-best subject exists (`NA` for a deferred tie or
no hit), `taxid` is the member's validated direct read attribution (a signed
synthetic identity is valid; `NA` when no usable direct attribution exists), and
the alignment metrics come from the first equal-best candidate. A member without
a direct hit carries `NA` in all four fields while still inheriting its OTU
lineage; an inherited assignment never fabricates a read-level hit.

The no-adapter table `<barcode>_blast_otu_noadapter_rpt.txt` is the same
projection restricted to members whose sample is a no-adapter class, produced
only when the round's no-adapter split is active; otherwise it is header-only.
Per-round copies are useful for auditing and comparing round-to-round changes.
The persistent `_state/<barcode>_blast_otu_pretax_rpt.txt` is the current
cumulative snapshot described under [Cumulative BLAST OTU state](#cumulative-blast-otu-state-r4-d).

### OTU membership (`otu_members.tsv`)
[back to Top](#rtbioscan-output)

Two-column TSV: `otu_key`, `read_id`. Canonical source for the unique NR sequence relations belonging to each OTU in that round, derived from the OTU-definition report independently of BLAST filtering. Read IDs identify NR sequence members here; they are not a census of all raw observations. The schema and filenames are unchanged.

#### Membership and metric contract

The names below describe populations, not additional output columns. All are counts (no percentage denominator); their units, grouping, and time scope differ.

| Population | Unit and grouping | Time scope and exclusions |
| --- | --- | --- |
| `canonical_membership` | Unique NR sequence-to-OTU relations, identified by the retained NR IDs | Current round's canonical relation assembled from retained active and accumulated frozen state. Exact duplicate raw observations and repeated identical NR relations add no members; distinct NR sequences do. |
| `representative_count` | Starred original representative per pre-split NR cluster | Exactly one before marker splitting. A marker projection contains zero or one, according to whether that original representative belongs to the subgroup; never elect a replacement. |
| `promotion_sequence_round_evidence` | Sum of recorded active-NR distinct-sequence counts per representative-hash OTU | Distinct-sequence-round observations under existing history/eligibility rules. A sequence can contribute in several qualifying rounds. The temporal minimum and growth-window checks are separate requirements. |
| `blast_eligible_read_support` | Current eligible raw-read support per OTU after the existing hash join | Current BLAST-input round only. Multiple raw reads with a uniquely assigned hash contribute; ambiguous/unmapped hashes cannot contribute OTU support. The enforced drop policy excludes them from the filtered query. |
| `consensus_reads_N` | Existing support represented by `reads-N` for the emitted consensus | Mode-dependent: selected representative OTU's read count in `representative` mode; sum of counts across merged consensus-cluster members in `cluster_total` mode. Cached consensus retains its existing provenance and mode semantics. It is not canonical NR cardinality. |

For example, a pre-split cluster with a COI representative and an ITS2 member has two canonical members and one representative. Its COI and ITS2 projections each have one member, with representative counts `1` and `0`, respectively. Both trace to the same original pre-split cluster; no representative is invented. Separately, recorded promotion counts `3, 4` yield evidence `7`, so a threshold of `5` passes when the temporal and other checks also pass. See [promotion controls](usage.md#--otu_frozen_min_rounds----otu_frozen_min_reads).

**Zero and missingness:** zero is valid only for a successfully computed population, such as the ITS2 projection's representative count above or an empty set of eligible matches. An absent output, an unexecuted/failed stage, and unavailable evidence do not establish zero. An empty frozen-by-hash append file means no additional relation was emitted; it does not mean accumulated frozen membership is zero. Absent/empty frozen metadata preserves the existing no-append behavior and supplies no representative authority. Malformed or conflicting metadata/hash-map input is an error, not a successful zero: the helper rejects it before replacing its output, and the success-only caller does not append it to persistent membership. Consumers must retain computation status and diagnostics when interpreting empty tables or missing values; no new sentinel or schema field is introduced here.

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

The `--restart_mode` parameter provides bounded recovery options:
- `off` (default): normal run.
- `restore`: after genuine loss of the live ongoing tree, restore retained snapshots only if all existing authentication and completeness checks pass and no protected round-lock evidence blocks the operation.
- `reset`: wipe rolling state only at an eligible boundary with no protected round-lock evidence in any state or snapshot location the handler would mutate.

Normal completed rounds retain protected protocol history, so an in-place completed state is not eligible for reset or restore. `restart_force=true` may repeat an otherwise eligible operation but does not bypass safety checks. Stop related writers, preserve the original state, and ensure replay inputs remain available. Never delete, edit, or fabricate round-lock records to pass the scanner. Existing quarantine and cutover operations do not prepare completed history for reset or restore. There is no supported in-place preparation command for ordinary completed protected history; use a fresh namespace and reanalyse, or a separately reviewed recovery procedure.

### Internal BLAST marker-taxonomy sidecar

`<barcode>_blast_otu_taxonomy_v1.tsv` is an internal round artifact. Its header is
`#RTB-R4B-TAXONOMY`, version `2`, a generation SHA-256, and canonical JSON mapping each
accepted marker to its sealed R4-A evidence `signature`, `rows`, and `body_sha256`.
The JSON is `{}` when there are no markers. A named-column line precedes the records;
a count/body-SHA-256 footer and final newline seal the complete file. Publication validates the entire generation
before atomically replacing the destination. It is derived evidence, not independently
reusable taxonomy authority. A round with no cluster members retains existing empty
placeholder behavior.

Reporting rejects a version 1 R4-B sidecar because it has no R4-A evidence provenance;
it cannot infer a generation from current taxid sets. Nextflow does not generally hash
arbitrary `bin/` content, so a cached version 1 B sidecar followed by a rerun of D
fails closed. The full R4 chain is not yet integrated into local `main`; its eventual
integration changes the relevant process bodies and does not migrate a production
R4-B version 1 cache. No `main.nf` cache-token change is part of this correction.

Each row records the query/member, canonical OTU and marker, resolved taxid, explicit
seven-rank lineage, assignment status, actual depth, direct/LCA origin, canonical-member
and vote counts, winning support/tie count, stable representative key, and read-level
status/lineage/origin/rejection reason with original signed IDs. Status counts retain
minority and rejected evidence even when a valid direct plurality wins. Aliases cannot
multiply a canonical member's vote. Missing ranks and absent stable keys are `NA`.

The bounded statuses are `ASSIGNED`, `AMBIGUOUS_TIE`, `NO_HIT`,
`FILTERED_INELIGIBLE`, `REFERENCE_UNRESOLVED`, `REFERENCE_INCONSISTENT`, and
`COMPUTATION_FAILED`; unknown spellings are rejected. Numeric depth is -1 for unresolved
lineage and 0–6 for kingdom through species. An LCA without a unique supported taxid
retains its lineage with taxid `NA`. An ambiguous contribution to the winning identity
retains its LCA origin; ambiguity in a different minority identity does not taint a
direct winner. The existing public annotated-report columns and report denominators
are unchanged. Public denominator and report-policy changes remain deferred.

The existing OTU-refinement phase-timing and workload artifact names and columns
remain available. Strict marker refinement reports `single_pass` scheduling and
zero shard/merged-pair counters because it does not materialize those intermediates;
cluster, member and annotated-output counts describe the work actually performed.

### Consensus taxonomy authority (R4-C)

Consensus attribution uses an internal `consensus_taxonomy_<MARKER>_v1.tsv` in the
existing consensus state directory. A merged `consensus_taxonomy_v1.tsv` is used
inside the task; the reporting task reads the round copy
`<barcode>_consensus_taxonomy_v1.tsv`. These artifacts do not add columns to the
established 17-column public consensus report.

The sealed TSV has exactly 23 fields, in this order:
`long_seq_id`, `marker`, `sample`, `stable_otu_key`, `display_otu_key`,
`consensus_id`, `sequence_hash`, `query_length`, `resolved_taxid`, `kingdom`,
`phylum`, `class`, `order`, `family`, `genus`, `species`, `status`, `depth`,
`origin`, `candidate_count`, `candidates_json`, `reason`, `signature`.
Missing scalar values and ranks use `NA`; an empty candidate set is `[]`.
Rows sort by full query identifier, with one attribution per query. Duplicate
queries, ambiguous public identities and inconsistent sequence attributions are
rejected. Stable ownership is the existing marker/representative-hash key, or
`NA` when the marker projection has no representative ownership evidence.

Statuses are `ASSIGNED`, `AMBIGUOUS_TIE`, `NO_HIT`, `REFERENCE_UNRESOLVED`,
`REFERENCE_INCONSISTENT` and `COMPUTATION_FAILED`. Depth is -1 for no usable
lineage, otherwise 0–6 for kingdom through species. Origin is independently
recorded as `DIRECT`, `LCA` or `NONE`. Signed taxids are valid identities. Public
unusable taxonomy retains the existing `Unassigned` placeholder.

`candidates_json` retains every equal-best subject in subject order. Each tuple
contains sequence hash, subject, subject taxid, e-value, alignment length,
identity, bitscore, query start/end, subject start/end and query length. The
selected metrics use R4-A canonical decimal encoding. A single direct subject
can supply public `blast_hit`; multiple subjects use `NA`. The full-table
`consensus_taxid` and public `taxid` contain the resolved signed identity or
`NA`, including `NA` for a multi-taxid LCA.

The first line is `#RTB-R4C-TAXONOMY`, version `1`, and a SHA-256 scientific
signature, separated by tabs. The final `#END` line records the body-line count
(including the field-name header) and SHA-256 of the exact newline-terminated
body. Readers require the field count, ordering, coherent status/depth/origin,
canonical evidence, footer, checksum and generation identity before publication.
Each persistent file is published by a same-directory temporary file and rename.
The marker files and round projection are separate atomic files; a retry
validates and regenerates the complete current projection.

### Public OTU propagation and reporting denominators (R4-D)

The experimental unit for OTU abundance and every public taxon count is one
unique canonical NR sequence-to-OTU membership relation. It is not a raw
observation, a BLAST HSP, a direct-hit row, an eligible-support observation, a
consensus sequence or a round-local `OTUB` alias. The typed quantities are:

| Quantity | Unit and rule |
| --- | --- |
| `canonical_member_count` | Unique `(marker, stable OTU, canonical member)` relations of the current membership joined to the validated R4-B sidecar. Exact duplicate raw observations add nothing; a hit-less member stays in the denominator; a directly hit non-member is excluded. |
| `blast_eligible_read_support` | Raw-read support used only for BLAST eligibility (`otu_blast_min_members`). Never an abundance, a canonical size or a public denominator; the reporting sidecar records only the resulting per-OTU eligibility flag. |
| `canonical_direct_hit_count` | Canonical members with sealed direct hit evidence (diagnostic; `canonical_direct_attribution_count` counts those whose read-level attribution is usable). |
| `canonical_assigned_count[L]` | Members whose OTU status is `ASSIGNED` or `AMBIGUOUS_TIE` with actual resolved depth ≥ L (family = 4, genus = 5, species = 6). Members inherit the OTU assignment even without a direct hit; signed taxids qualify exactly like positive ones; a family-only assignment counts at family but not genus/species; an ambiguous tie counts only through its resolved depth. |
| `canonical_unassigned_count[L]` | `canonical_member_count − canonical_assigned_count[L]`, never derived from taxid sign, missing hits, placeholder text or kingdom spelling. |
| status counts | The bounded R4-B vocabulary (`ASSIGNED`, `AMBIGUOUS_TIE`, `NO_HIT`, `FILTERED_INELIGIBLE`, `REFERENCE_UNRESOLVED`, `REFERENCE_INCONSISTENT`, `COMPUTATION_FAILED`); they always sum to `canonical_member_count`, and read-level rejection reasons are counted separately. |
| taxon counts | At each rank one assigned member contributes to exactly one taxon (a rank gap is counted under `NA`); the per-taxon sum equals `canonical_assigned_count[rank]`; OTU counts (`otu_count`, `stable_otu_count`, `assigned_otu_count`) are separate fields that count each OTU once. |
| fractions | Stored with explicit numerator and denominator; `null` (never numeric zero) when the denominator is zero. |

`REFERENCE_UNRESOLVED` and `REFERENCE_INCONSISTENT` members remain in the
denominators, are excluded from every assigned numerator, appear in the status
and reason diagnostics, and are never converted into no-hit or assigned. At this
release R4-B reports members that never reached a sealed BLAST query (for
example size-ineligible members) as `REFERENCE_UNRESOLVED` with reason
`missing_sealed_query`; R4-D propagates that verbatim.

The rolling tables written by `append_reports.pl` (species/genus/family time
series, treemaps, marker/model breakdowns) count the same canonical-member rows;
`NA` and `Unassigned` rank values never form a taxon, and `min_reads_sample`
applies to canonical-member counts. Consensus `reads-N` counts keep their own
documented mode semantics and are not canonical membership counts.

### Internal BLAST OTU reporting sidecar (R4-D)

`<barcode>_blast_otu_reporting_v1.tsv` (task local, round copy in the round
directory, current snapshot in `_state/`) is the sealed artifact from which the
public tables and the `taxonomy_assignment` metrics are projected. Its first
line is `#RTB-R4D-REPORTING`, version `1` and a SHA-256 generation signature
binding the R4-B sidecar signature, the canonical-membership bytes, the sealed
per-marker evidence signatures, the eligibility list and the identity context
(no timestamps); a `#columns` line, the rows, and a `#END` line with the row
count and the SHA-256 of the exact body seal the file. Readers require the exact
column set, one row per canonical member, canonical row order (marker, display
OTU number, canonical member), coherent status/taxid/depth/origin for both the
OTU and read level, consistent projection fields inside one OTU, per-OTU
cardinality equal to `member_count`, and the footer before any use.

The 36 fields are: `canonical_member`, `uuid`, `marker`, `read_id`,
`barcode_by_homology`, `basecalling_model`, `sample`, `display_otu_key`,
`stable_otu_key` (`MARKER|md5` or `NA`), `otu_status`, `otu_taxid`, `otu_depth`,
`otu_origin`, the seven ranks, `member_count`, `blast_eligible` (`1`/`0`/`NA`),
`direct_hit`, `read_status`, `read_taxid`, `read_depth`, `read_origin`,
`read_reason`, `hit_id`, `hit_taxid`, `aln_length`, `perc_id`, `evalue`,
`bitscore`, `candidate_count` and `source_taxids`. Missing values are `NA`.
Duplicate canonical members are rejected; a conflicting relation (one member in
two projections, or membership and sidecar disagreeing) fails the round instead
of being resolved by row order. Publication is atomic (same-directory temporary
file and rename after validation).

### Cumulative BLAST OTU state (R4-D)

`_state/<barcode>_blast_otu_reporting_v1.tsv`,
`_state/<barcode>_blast_otu_pretax_rpt.txt` and
`_state/<barcode>_blast_otu_noadapter_rpt.txt` form the current cumulative
snapshot: the complete current canonical membership (accumulated frozen plus
active relations) with its current classification, keyed by biological identity
rather than `OTUB` number. Each round replaces all three files as one
transaction from the validated round sidecar; they are never appended. Hence a
retried or replayed round adds no duplicate, renumbering an `OTUB` creates no
second row, stale classifications are replaced, a relation removed from the
current membership disappears, a no-new or cache-only round republishes
byte-identical files, and a failed publication leaves the previous snapshot in
place. A legacy appended state file is tolerated as opaque input and replaced
only after the complete new snapshot validated. Failed rounds keep their
placeholder tables and leave the snapshot untouched. `report_round_json.pl`
reads the snapshot through `--blast-otu-cumulative` and
`--blast-otu-reporting-cumulative`; `--summary` and `--summary-otu` are
deprecated and ignored (a deterministic warning is printed when they are given).

Interruption safety (R4-I2). The three files form one generation, named by the
sealed commit record `_state/<barcode>_blast_otu_cumulative.commit` (header
`#RTB-R4D-CUMULATIVE`, version `1`, the generation id, its predecessor, the
name/size/SHA-256 of each product and a SHA-256 seal); the record is the only
authority. A publication writes immutable members `_state/<name>.gen-<generation>`
and makes them durable, replaces the record with one rename (the commit), then
retracts every public name that changes (renamed to `<name>.bak.<pid>`) before
it links any of them to its committed member. A process killed or terminated
before the commit leaves the previous generation authoritative, after it the
new one. Every production reader (`report_round_json.pl`, `report_run_json.pl`,
`report_render.py` and the backup table publication) resolves the record,
checks each member's size and SHA-256, opens only those members, and before it
writes output confirms they are still the same files; one predecessor
generation is kept, so a reader overtaken by one further commit still
completes, and one overtaken by two fails and is rerun. A synchronous failure
restores the previous names, bytes and record exactly. The next publication
(or the task retry) reconciles lagging public names and removes temporary
files (`.r4d-publish-<barcode>-*`), rollback files (`<name>.bak.*`) and members
of generations other than the committed one and its predecessor; one
publication per barcode runs at a time (`<barcode>_blast_otu_cumulative.lock`).

The public names are compatibility copies, never the authority: no portable
filesystem operation switches three names at once (exFAT has no hard links, and
symbolic links only where the operating system emulates them), so they change
one by one. Because every changed name is
retracted before any new one appears, the public names that exist at any
instant hold one generation; after an interruption between the commit and the
end of the projection some of them can be missing (a direct read fails) until
the task retry, but they never mix generations. They are hard links of the
committed members, with mode 0600. Byte copies replace hard links only when
link(2) reports that the filesystem does not support them (ENOTSUP, EOPNOTSUPP
or ENOSYS, or EPERM/EMLINK on a filesystem whose pathconf link limit is 1, such
as exFAT or FAT); any other link error (EIO, ENOSPC, EACCES, EXDEV, EPERM on a
filesystem with hard links) fails the publication with the previous generation
intact. Where link(2) reports EPERM without a link limit of 1, publication
fails closed. Symbolic links are never used.

Without a record, only three `_state` contents are legitimate: a pristine first
run (none of the three public names, no generation member, no record and no
governed residue), a complete pre-I2 R4-D snapshot, which is served only after
it is validated as one publication (a sealed reporting sidecar, the public
table projected from it, and its no-adapter projection or the header-only
table), and an authentic pre-R4-D snapshot, which is sealed on first sight
(below). Governed residue is every transaction file of the barcode's names:
generation members, `.r4d-publish-*` temporaries, the lock and other
`<barcode>_blast_otu_cumulative.*` files, and a governed table name followed by
a transaction suffix (`.tmp`, `.temp`, `.journal`, `.bak`, `.part`, `.partial`,
`.new`, `.old`, `.orig`, optionally followed by `.`, `-`, `_` or `~` and more,
or a trailing `~`) or a dot-prefixed partial copy of it (`.<name>.XXXXXX`);
other files, such as `other.tmp` or `<barcode>_reads_time_rpt.txt.tmp`, never
count. Every other record-less state fails closed, and each diagnostic says
what the reporting task will do with it. The reporting task republishes a
consistent snapshot and the residue its own interrupted first publication
leaves (members, temporaries, the lock). It refuses to write over anything
else before it writes anything (`publication refused`, a non-zero exit, every
byte, inode, mode and time kept), so that no new generation erases the evidence
of a state that is not authentic: a subset of the public names, inconsistent or
malformed tables, rollback or journal files, another publisher's temporaries.
Such a state needs explicit remediation (restore an authentic backup after genuine live-tree loss, or reset only at an eligible boundary without protected round-lock evidence). Deleting the record never restores legacy mode. While a restart
(`restart_mode=reset` or `restore`) of the state is being applied, or after it
was interrupted, its `_state` fails closed for every reader and publisher:
`bin/restart_handler.sh` records `status=applying` in
`<outdir>/temp/.restart_applied.<state_id>` before it first changes the state
and `status=applied` after its last change (rerunning an interrupted restart
completes it); an unreadable or malformed record also fails closed. Other
directories (a round directory, say) are subject to these rules only once they
hold an R4-I2 file; until then their tables are ordinary files, and the
publication rules above apply to the pipeline's `_state` only.

Pre-R4-D outdirs (upgrade). The pre-R4-D pipeline (up to R4-C; `main` writes
the same layout) creates `_state/<barcode>_blast_otu_pretax_rpt.txt` and
`_state/<barcode>_blast_otu_noadapter_rpt.txt` from its first reported round's
tables and appends every later round's rows, after copying each round's own
tables into its round directory `temp/ongoing/state/<state_id>/<round_barcode>/`;
`_state/round_index.tsv` orders the rounds and `_state/done_pod5.txt` lists the
completed ones. Such a `_state` has no reporting sidecar and no record. Its
tables are accepted only when they are exactly that: both tables present as
regular files with the exact pre-R4-D header and complete 17-field rows, no
other `<barcode>_blast_otu_*` entry and no other barcode's cumulative table,
and each table equal to the header followed by the rows of the round tables of
one leading run of the ledger's rounds (the same run for both tables, every
completed round included, each completed round's tables present). The first
reader or reporting task that meets it seals it once, under the publication
lock: hard links `<name>.gen-<L>` of the two tables (their bytes, modes and
times are never changed; verified byte copies without hard links) and a legacy
record `<barcode>_blast_otu_cumulative.commit` with header
`#RTB-PRE-R4D-LEGACY` version `1`, the generation id `L` (SHA-256 of the two
product lines), predecessor `NA`, the name/size/SHA-256 of both tables, the
number of rounds validated and the SHA-256 of their per-round digests, and a
SHA-256 seal, committed by one rename. Sealing is idempotent, and a sealing
killed part-way is completed by the next reader. From then on the snapshot is a
committed generation without a reporting sidecar: `report_round_json.pl` reads
the old public table for the historical values it always fed (per-sample OTU
totals, the sunburst, the canonical OTU taxonomy) and reports the cumulative
R4-D taxonomy-assignment metrics as unavailable (`null`, never zero; no sidecar
is ever fabricated); `report_run_json.pl` and `report_render.py` read the old
tables as before. Report-only rebuilds of such an outdir therefore work right
after the upgrade. If the first upgraded round fails, the sealed legacy
generation stays authoritative and `-resume` retries without any manual step;
no partial new generation is ever visible. (If the snapshot cannot even be
sealed, because of an I/O error, the reporting task fails and leaves it intact
for its readers and for the retry.) The first complete publication
replaces the legacy record by one rename (its record names the legacy
generation as predecessor) and keeps the legacy members for one further commit;
the next publication removes them. A `_state` that is not exactly this layout
(a single table, a table appended, truncated, duplicated, reordered or taken
from other rounds or other state, a wrong header, missing ledger or round
tables, residue, a sidecar next to the tables) is never sealed: every reader
fails closed with the reason and leaves every byte untouched, and the reporting
task refuses to publish over it (see above). Results copies of the tables
establish this authority only through a restore, which proves them against
the live round ledger and round tables before it wipes those (below).

Backup copies (R4-I2). `backup_update_and_clean` backs up the cumulative
generation of its barcode by name, never through what a glob of the public
names finds: section 2 from `_state` into `<outdir>/ongoing/state/<state_id>/`,
section 7 from there into `<outdir>/current/state/<state_id>/tables/`
(`r4d_backup_generation` in bin/lib/backup_sync.sh); a governed table handed to
`sync_changed_files` is likewise resolved through its source's record even
while its public name is retracted. The backup is a replica of the source's
generation: its record (the same kind and generation id; a sealed pre-R4-D
generation keeps its legacy record and provenance line), every member -- the
reporting sidecar included -- written from bytes verified against the source
record, the record replaced by one rename, then retraction and projection of
the two public tables (a destination projects no sidecar name). A backup killed
before its commit leaves the previous generation authoritative, after it the
new one; every later copy of a destination, and `report_render.py`, resolve its
record, so no reader combines the tables of two generations, and no backup is
empty or mixed. Each destination keeps its current and previous generation.
A source that has never been published is not backed up; a damaged or
unauthenticated one fails the backup.

`restart_mode=restore` restores the cumulative generation with its authority only after the protected round-lock namespace preflight permits restore. A normal completed in-place state retains protected history and is ineligible. Before it changes anything, `bin/restart_handler.sh` classifies the snapshot's
generation of every barcode it holds (the structured
`current/state/<state_id>/tables/`; the round copies in
`temp/current/state/<state_id>/tables/` are never cumulative authority): a
complete generation is accepted (its record's seal and every member's size and
SHA-256 verified, as for the live record); an older backup is accepted only when retained
evidence proves it -- an earlier candidate's backup record
(`#RTB-R4D-CUMULATIVE-BACKUP`, two members) by the sidecar bytes its record
binds (found by size and SHA-256 among the snapshot, the round copy in
temp/current, the live `_state` and the live round directories), plain R4-D
copies by a valid sealed sidecar whose projections they are exactly, and plain
pre-R4-D copies by the live round ledger and round tables with the snapshot's
completed-round ledger -- and anything else (a corrupt record, a missing or
damaged member, a single table, members without a record, tables no evidence
proves, compressed copies of the tables) refuses the restore with the reason,
before any byte changes. After
recording the restart as applying and before it wipes the live state, the
handler completes a proven older backup in place into a full generation (so
provenance is established while its evidence exists, and a repeated restore
needs none); the snapshot copy then skips every governed name, and the
generation is installed into `_state`: member copies verified against the
record, the record committed by one rename, then its public names. The
restored generation resolves at once, with its reporting sidecar for an R4-D
generation: every report works before another round, failed rounds leave it
authoritative, and the next complete publication supersedes it. A legacy
backup record written by an earlier candidate (`#RTB-PRE-R4D-LEGACY-BACKUP`)
is a complete legacy generation and is served and restored as such. Round-local
copies of these tables (round directories and
`temp/current/state/<state_id>/tables/`) are ordinary files and are copied
exactly as before.

### Retired write-only state (R4-D)

- `_state/blastreport.txt` was snapshot-copied, merged, sorted and republished
  every round although nothing read its content. Current BLAST results come only
  from the sealed R4-A cache/evidence state. The round copy
  `<round>/blastreport.txt` is kept. New runs write a small versioned marker
  `_state/blastreport_initialized_v1.txt` after each successful BLAST state
  publication; an existing legacy `_state/blastreport.txt` is accepted as
  evidence of prior initialization and is never parsed, rewritten or deleted.
- `_state/<barcode>_sup.tsv` and `_state/<barcode>_hac.tsv` had no consumer and
  grew without bound. They are no longer created or appended; the round-local
  `<round>/<barcode>_sup.tsv` and `<round>/<barcode>_hac.tsv` copies remain. Old
  files are tolerated on resume and left untouched.
