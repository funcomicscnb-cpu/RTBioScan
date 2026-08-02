# Taxid production, resolution, persistence, and reporting inventory

This inventory describes the current code paths. It is diagnostic documentation, not a proposed implementation.

## Configuration

The default marker pairs are:

| Marker | Target taxon string | Marker database |
|---|---|---|
| COI | Metazoa | `db/COInr98_2024Jun_RioNegro_Brazil` |
| ITS2 | Viridiplantae | `db/ITS2nr98_2024Jun_RioNegro_Brazil` |

The target-taxon interface is string-based (`targets` and `target_taxa`). There is currently no configured target-clade taxid.

## Path inventory

| Stage | Producer or consumer | Taxonomic decision | Persistent/output effect | Principal Phase 1 observation |
|---|---|---|---|---|
| FAST routing | `main.nf:848` | LAST output is reduced to the first emitted row per query by `awk '!seen[$1]++'`. | Writes `*_qced_reads_kingdom.txt`. | No score, coverage, or competing-taxon margin is applied by the pipeline. |
| Target admission | `main.nf:859-875` | Rows containing `MARKER|TARGET_TAXON` are selected. | Writes `*_reads_target.list`. | Database labels determine admission. |
| HAC annotation | `main.nf:1010-1120`, `fastq_add_annotations2ids.pl` | HAC basecalling is restricted to admitted read IDs and reuses their marker annotation. | Marker is embedded in HAC read headers. | FAST routing is not rerun after HAC basecalling. |
| OTU/representative BLAST | `bin/blast_otu_pretax.sh:47-164` | Each marker is searched only against its marker-specific database with 92% family identity by default, 50% coverage, and `-max_target_seqs 1`. | Raw and cached best-hit rows. | No bacterial competitor can correct an animal-routed read. |
| Subject taxid extraction | `bin/blast_otu_pretax.sh:145-149` | `accession|kraken:taxid|N` is reduced to numeric `N` before taxonomy-depth processing. | Numeric hit taxid enters the OTU path. | Positive NCBI taxids are not replaced by the synthetic lineage map. |
| Identity-depth taxid | `bin/get_blast_taxdepth.pl` | TaxonKit supplies rank taxids; identity thresholds select species, genus, family, order, or `NA`. | Updates marker `memtaxN.txt` in `_state`. | TaxonKit uses its implicit default data directory. |
| OTU taxid selection | `bin/lib/RTBioScan/OTURefineBlastreport.pm:14-235` | The most frequent valid member taxid is selected for the cluster, with lexical tie-breaking. | Produces OTU/member taxid pairs. | Marker consistency is not considered when choosing the cluster taxid. |
| OTU lineage resolution | `bin/lib/RTBioScan/OTURefineBlastreport.pm:275-370` | Synthetic IDs use `DBnr_2024Jun_id2lineage.txt`; positive numeric IDs fall through to TaxonKit; unresolved IDs receive `K__Unassigned`. | Produces annotated OTU lineage rows. | TaxonKit `{K}` is blank for bacterial taxids. Synthetic overrides are loaded into one last-write-wins hash despite 1,066 cross-target ID collisions. |
| OTU report join | `bin/reporting_blast_otu.pl:59-132` | Per-read `hit_id/taxid` come from raw subject IDs; `otu_taxid` and lineage come from the refined OTU report. | Writes `*_blast_otu_pretax_rpt.txt`. | A row deliberately contains two different taxid provenances. |
| Assigned read state | `bin/blast_assigned_read_ids.pl`, `main.nf:2988-3010` | A positive numeric taxid, assigned kingdom, or assigned lineage marks a read assigned. | Appends to `*_assigned_read_ids_ever.list` and protected-read state. | No target-clade consistency check precedes persistence. Negative synthetic IDs rely on lineage rather than `is_numeric_taxid`. |
| Assigned OTU state | `bin/blast_assigned_otu_keys.pl`, `main.nf:3013-3058` | Positive numeric taxid or sufficient assigned lineage marks an OTU assigned. | Persists `*_assigned_otu_keys_ever.list`, expands protected members, and applies grace state. | A bacterial taxid is sticky once considered assigned. |
| Unassigned/pruning state | `bin/blast_unassigned_read_ids.pl`, `main.nf:3545-3580` | Uses the same numeric-taxid/lineage assignment predicates. | Controls live unassigned lists and pruning eligibility. | Target inconsistency is not part of assigned/unassigned status. |
| Consensus BLAST | `main.nf:4240-4420` | Consensus sequences are split by existing marker token and searched against the same marker-only database with `-max_target_seqs 1`. | Consensus BLAST cache and taxonomy TSV. | The prior marker route remains authoritative. |
| Consensus lineage | `main.nf:4328-4420` | Numeric subject taxids are resolved with TaxonKit `{K};{p};...`; failures become Unassigned. | `consensus_blast_report_full.txt` and a persistent lineage cache. | Cache metadata keys the BLAST taxdb directory, not the effective TaxonKit dump. |
| Consensus report join | `bin/reporting_blast_consensus.pl` | Output taxid is parsed from the BLAST subject; lineage columns come from the consensus taxonomy TSV. | Writes `*_blast_consensus_tax_rpt.txt`. | Invalid subject formats are dropped; marker consistency is not enforced here. |
| Consensus assigned state | `bin/consensus_assigned_otu_keys.pl`, `bin/consensus_recovered_reads.pl`, `main.nf:4470-4555` | A nonempty configured rank marks consensus/OTU/read membership assigned. | Persists OTU keys and protects contributing reads. | Consistency is again evaluated after persistence, in reporting. |
| Cumulative OTU report | `main.nf:3845-3872`, `bin/append_reports.pl` | Round rows are appended into `_state/*_blast_otu_pretax_rpt.txt`. | Cross-round report state. | Existing rows have no database, taxonomy, or classifier version. |
| Legacy report aggregation | `bin/append_reports.pl:173-240` and call sites | `marker_matches_target_taxon` compares formatted kingdom strings and rejects blank kingdom only at guarded call sites. | TSV/treemap and general aggregates. | Some aggregates are not guarded, so report surfaces can disagree. |
| JSON/HTML report eligibility | `bin/report_round_json.pl:421-427`, call sites at 2336, 2628, 3041 | `is_kingdom_consistent` compares kingdom strings but returns keep for blank kingdom. | OTU, read-assignment, and consensus JSON sections; HTML consumes JSON. | Bacterial taxids with blank `{K}` pass the marker guard. |
| Render fallback | `bin/report_render.py:785-820` | Reads current or accumulated BLAST reports and kingdom columns. | Rendered report tables. | Rendering inherits upstream eligibility and state provenance. |

## State and cache inventory

| State artifact | Producer | Invalidated by current code | Missing compatibility dimension |
|---|---|---|---|
| `otu_blast_cache_TARGET.tsv/.meta` | `blast_otu_pretax.sh` | Marker DB signature, `db/taxdb` signature, thresholds, target. | Effective TaxonKit dump; classifier policy. |
| `memtaxN.txt` | `get_blast_taxdepth.pl` | Created once per state directory; subsequently updated. | Source taxonomy checksum and resolver version. |
| `blastreport.txt` | BLAST process | Rolling merge by query ID. | Reference/taxonomy/classifier version. |
| `*_assigned_read_ids_ever.list` | assignment state update | Append/persist semantics. | Reason, taxid provenance, target consistency, version. |
| `*_assigned_otu_keys_ever.list` | OTU/consensus assignment | Append/persist semantics. | Reason, taxid provenance, target consistency, version. |
| `*_protected_read_ids_ever.list` | protected-state refresh | Derived from ever-assigned/grace state. | Compatibility with a changed classifier. |
| `consensus_blast_cache_TARGET.tsv/.meta` | consensus BLAST | Marker DB and `db/taxdb` signature plus thresholds. | Effective TaxonKit dump; classifier policy. |
| `*_consensus_taxonkit_lineage_cache.tsv/.meta` | consensus taxonomy | `db/taxdb` directory signature. | Actual `~/.taxonkit` checksum. |
| `*_blast_otu_pretax_rpt.txt` in `_state` | reporting process | Rows appended across rounds. | Reference, taxonomy, classifier, and scoring versions. |
| accumulated HQ FASTA and OTU membership | stateful core | Round/grace/pruning policies. | Reclassification requirement when taxonomy eligibility changes. |

## Required Phase 2 boundaries identified by this inventory

1. A database manifest alone is insufficient: TaxonKit taxonomy and classifier policy require separate version identifiers.
2. Target eligibility must run before ever-assigned/protected state is updated, not only during reporting.
3. The authoritative resolver must support positive NCBI taxids and target-namespaced synthetic IDs; the current negative-ID namespace cannot be used globally without repairing 1,066 collisions.
4. OTU, per-read, consensus, fallback, and accumulated-report paths must consume the same eligibility decision.
5. Existing legacy state must remain legacy-only unless explicitly reclassified.
