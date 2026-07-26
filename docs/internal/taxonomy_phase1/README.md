# Taxonomy remediation Phase 1

Captured: 2026-07-24  
Repository commit: `258b8f8259578bc4d52dfc66a12645d5302e243e`  
Branch: `fix/report-frozen-otu-sample-reads`  
Worktree: dirty before this audit; pre-existing changes were not modified.

## Scope

This directory contains the repository-only portion of Phase 1:

- an inventory of every identified taxid-producing, resolving, persisting, and reporting path;
- a checksummed snapshot of the reference, taxonomy, tool, and relevant source-code inputs;
- a lineage-clustered catalog of confirmed A, B1, and B2 cases and controls;
- a template for capturing and adjudicating reads from an affected run.

No pipeline behavior, database, process body, existing test, or user-owned modified file was changed.

## Files

- `taxid_path_inventory.md` — authoritative path and state inventory.
- `reference_manifest.tsv` — checksums, counts, dates, and tool observations.
- `fixture_catalog.tsv` — immutable sequence hashes and expected Phase 1 outcomes.
- `incident_capture_template.tsv` — fields required from a real affected run.

The catalog records sequence hashes rather than copying database sequences. The current sequences remain recoverable from the source artifacts and repository history while avoiding a second unmanaged reference copy.

## Confirmed Phase 1 findings

### 1. The COI FASTA and BLAST index are desynchronized

- FASTA records: 792,926.
- Indexed records: 791,433.
- FASTA-only titles: 1,493.
- Index-only titles: 0.
- The FASTA-only records are the contiguous tail at positions 791,434–792,926.

The installed COI BLAST database therefore represents the FASTA prefix, not the complete shipped FASTA.

### 2. ITS2 is synchronized

The ITS2 FASTA and BLAST database both contain 63,604 records, with zero title differences in either direction.

### 3. The FAST FASTA and LAST index counts agree

The FAST reference contains 665,527 records. The LAST project records 1,331,054 indexed sequences with `strand=2`, corresponding to 665,527 source records. Exact title equality could not be checked because `lastal` and `lastdb` are not installed in the current host environment.

| FAST label | Records |
|---|---:|
| COI / Archaea | 4,231 |
| COI / Bacteria | 26,351 |
| COI / Fungi | 298 |
| COI / Human | 54 |
| COI / Metazoa | 364,873 |
| COI / Protist | 4,231 |
| COI / Viridiplantae | 305 |
| ITS2 / Fungi | 110,593 |
| ITS2 / Metazoa | 21,268 |
| ITS2 / Protist | 7,432 |
| ITS2 / Viridiplantae | 82,884 |
| rbcL / Protist | 17,946 |
| rbcL / Viridiplantae | 25,061 |

The directory also contains a lone `targets_All_tagged_nr95.fa.ndb` file dated July 2024. It is not part of the February 2026 LAST index set and has no accompanying BLAST index components under that basename, so it is recorded as an orphan artifact rather than an active index.

### 4. Effective TaxonKit provenance is external and differs from the repository dump

The pipeline invokes TaxonKit without `--data-dir`. TaxonKit v0.14.2 therefore uses `/Users/DavidJuan/.taxonkit` on this machine. Those files are dated June 2024 and have different checksums from `db/taxonomy`, whose files are dated October 2025.

The OTU and consensus cache keys track `db/taxdb`, not the effective TaxonKit data directory. A TaxonKit taxonomy change can therefore alter lineage results without invalidating the corresponding cached or accumulated classification state.

### 5. Positive bacterial taxids become persistent assignments

The assigned-read and assigned-OTU state paths treat a positive numeric taxid or assigned lineage as assigned without checking marker-to-clade consistency. Such reads and OTUs are added to ever-assigned/protected state and can be retained in subsequent rounds before the report-level kingdom guard is evaluated.

### 6. Synthetic negative taxids require a separate resolver and namespace repair

`DBnr_2024Jun_id2lineage.txt` contains 42,368 negative-ID rows:

- 41,302 Metazoa;
- 1,066 Viridiplantae;
- zero positive NCBI taxids.

They are not 42,368 unique IDs. There are only 41,302 unique values: every one of the 1,066 Viridiplantae IDs collides with a Metazoa ID and maps to a conflicting target lineage. The current hash loader is last-write-wins, and the plant rows occur later, so they overwrite the corresponding animal mappings.

The COI FASTA currently contains 1,108 records using 814 of these colliding IDs. This is a separate, confirmed source of valid-animal lineage loss or wrong-target annotation.

An ancestry implementation that consults only the NCBI taxonomy dump would classify all synthetic IDs as unresolved. An implementation that consumes the current override file without repairing its namespace would preserve the existing cross-target collisions.

## Deferred work requiring run data

No run directory was supplied. The following Phase 1 acceptance item is therefore pending:

1. identify the complained-about reads;
2. populate `incident_capture_template.tsv`;
3. adjudicate each read as animal, bacterial/off-target, ambiguous, or unresolved;
4. classify the observed mechanism as A, B1, B2, or another path;
5. freeze the adjudicated reads as mandatory regression cases.

Existing repository `results/` directories were not assumed to be the reported incident and were not inspected as user evidence.
