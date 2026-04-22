# RTBioScan: Concepts

> **Docs:** [Index](README.md) · [Pipeline Overview](pipeline.md) · **Concepts** · [Installation](installation.md) · [Usage](usage.md) · [Output](output.md) · [Report Schema](report_schema.md)

This page is the canonical glossary for the public documentation. Other pages introduce terms only briefly and link back here for the full meaning.

## Workflow and State
[back to Top](#rtbioscan-concepts)

- **Run** — one RTBioScan analysis session for a set of input data and configuration. A run may be monitored live or processed as a completed batch.
- **Round** — one incremental processing cycle within a run. In real-time mode, rounds let RTBioScan update reports as new POD5 chunks arrive.
- **State** — cumulative files that let later rounds build on earlier ones. State also supports `-resume` and restart/restore workflows.
- **Rolling state** — the latest working state used while the run is active.
- **Stable snapshot** — a completed-round snapshot intended for browsing, reuse, or restoration.

## Samples, Barcodes, Primers, and Markers
[back to Top](#rtbioscan-concepts)

- **Sample** — the biological or experimental unit inferred from metadata and demultiplexing inputs.
- **Barcode** — the sequencing or demultiplexing label used to route reads to samples or tracked groups.
- **Primer** — the marker-specific primer signal used to identify target amplicons.
- **Replicate** — a repeated observation of the same sample when the metadata define replicate structure.
- **Tracked group** — a report grouping used in track mode, such as sample, primer, or sample+replicate.
- **Marker** — the target genetic marker used for taxonomic identification. Common report groups are `COI`, `ITS2`, and `Other`.

## Reads and Read Fate
[back to Top](#rtbioscan-concepts)

- **Read** — an individual sequence read produced from nanopore signal after basecalling.
- **On-target read** — a read matching the configured marker and broad target-taxon scope closely enough to continue through the pipeline.
- **Off-target read** — a read excluded because it does not fit the configured target filters.
- **Demultiplexed read** — a read assigned to a sample, primer, barcode, or tracked group.
- **BLAST-assigned read** — a read with a taxonomic database match that passes the configured assignment criteria.
- **BLAST-unassigned read** — a read that reached the BLAST stage but did not receive a usable taxonomic assignment.
- **BLAST skipped** — a read category used in read-fate plots when a read is not submitted to BLAST for a marker-specific reason.
- **On-target not demultiplexed** — an on-target read that did not receive a usable sample or tracked-group assignment.
- **Read-fate chart** — a report chart showing how reads are distributed across `BLAST-assigned`, `BLAST-unassigned`, `BLAST skipped`, `On-target not demultiplexed`, and `Off-target` categories.

## OTUs and Consensus
[back to Top](#rtbioscan-concepts)

- **OTU** — operational taxonomic unit: a sequence-read cluster used as an intermediate biological grouping before or alongside final consensus reporting. An OTU is not automatically a species.
- **Active OTU** — an OTU still participating in the current active clustering pool.
- **Frozen OTU** — a stable OTU carried forward across rounds so the pipeline can avoid repeatedly reprocessing resolved evidence.
- **Informative OTU** — the user-facing OTU set counted in reports after excluding internal bookkeeping categories.
- **Consolidated OTU** — an OTU with enough stable evidence to support cached consensus behavior.
- **Consensus sequence** — a representative sequence emitted for an OTU after consensus generation.
- **Consolidated consensus** — a stable consensus sequence carried forward as a representative result.
- **`reads-N`** — read-support value shown in consensus FASTA headers. Its exact meaning is controlled by `--consensus_reads_mode`.

## Taxonomy and Reporting Aids
[back to Top](#rtbioscan-concepts)

- **Taxonomic assignment** — the sequence-similarity interpretation produced from configured marker-specific barcoding databases and taxonomy resources.
- **Taxonomic level** — the rank used for display, usually species, genus, or family.
- **Assignment depth** — the deepest rank justified by configured identity thresholds, such as family, genus, or species.
- **Species of interest** — optional project-specific taxa highlighted in reports when species tables are supplied.
- **Local-interest genus list** — optional genus lists, often derived from observational resources, used for project-specific report highlighting.
- **Observational resources** — external resources such as GBIF or iNaturalist-derived lists. They can support filtering or highlighting in reports but do not replace sequence-based assignment.

## Report Sections
[back to Top](#rtbioscan-concepts)

- **Run Info** — the default per-run report view.
- **Primer Comparison** — a track-mode report view for primer-level comparison.
- **Replicate Comparison** — a track-mode report view for sample+replicate comparison.
- **Global Overview** — run-wide cards, read evolution, and latest-round result summaries.
- **Sample Details** — sample, primer, or replicate-level summaries depending on the report view.
- **Rounds Info** — per-round metrics and progression through time.
- **Additional Info** — figure gallery and exported visual assets.
