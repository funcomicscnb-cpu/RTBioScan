# RTBioScan canonical COI reference database v1

## Origin

This release was derived from the RTBioScan downstream COI BLAST database
`COInr98_2024Jun_RioNegro_Brazil`. Its public-data foundation was created
following the COInr/mkCOInr approach described by:

Meglécz, E. (2023). COInr and mkCOInr: Building and customizing a
nonredundant barcoding reference database from BOLD and NCBI using a
semi-automated pipeline. *Molecular Ecology Resources*, 23(4), 933-945.
<https://doi.org/10.1111/1755-0998.13756>

COInr integrates COI records from NCBI and BOLD into a coherent taxonomic
system. Its source-level quality controls include selecting COI records;
rejecting sequences outside 100-2,000 nucleotides or with more than five
consecutive internal `N` bases; excluding unsuitable NCBI environmental,
metagenomic, intron-bearing, or non-COI records; requiring valid Latin taxon
names; assigning coherent taxIDs to BOLD lineages; and removing taxonomically
redundant substrings within taxa.

## Project-specific customization

The COInr-derived records were supplemented with barcode sequences generated
by the project. The combined collection was clustered with CD-HIT at 98%
nucleotide identity. Each representative was assigned to the last common
ancestor of the records in its cluster. This reduces database redundancy and
limits sensitivity to the stochastic selection of a representative sequence.

## Additional reliability review

The canonical release was generated from the exact deployed legacy BLAST OID
stream rather than by slicing its companion raw FASTA. The source components
and complete record stream were verified against frozen hashes before
construction.

Potentially unreliable entries were screened in two evidence tiers. Candidate
discovery required at least 85% nucleotide identity and 80% shorter-sequence
coverage against the frozen audit controls; priority evidence required at
least 97% identity and 90% coverage. The subsequent local review classified a
cross-family sequence/label conflict only at 95% identity and 80% coverage or
greater.

The final correctness-first review considered 44 specifically identified
records:

- 5 records with independently confirmed reference-sequence contamination
  were excluded;
- 24 records with cross-family sequence/label conflicts were conservatively
  excluded without asserting which endpoint was biologically incorrect; and
- 15 unresolved records were retained because the available evidence did not
  support a reliable exclusion.

All other source records were retained unchanged. This stage performed no
implicit deduplication, taxonomic relabeling, sequence repair, or reordering.
Every exclusion was applied only after the reference ID, stored taxID, and
sequence SHA-256 matched its frozen disposition record exactly.

## Result

- Canonical records: 791,404
- Canonical bases: 485,444,705
- Excluded source records: 29
- Canonical FASTA SHA-256:
  `d0b3aca535fbadcd0da8dfc7218c08e7b4151c9562ffe3fd37baf6cdcaef1775`
- Release identifier: `rtbioscan_coi_canonical_v1`

The release includes the compressed canonical FASTA, a fixed eight-component
BLAST database, construction and index provenance, and the excluded legacy-OID
table. The provenance files define the exact input identities, filtering
decisions, record counts, build tools, and artifact hashes.

## License

The database release is distributed under the
[Creative Commons Attribution-ShareAlike 4.0 International license](https://creativecommons.org/licenses/by-sa/4.0/).
Users must provide attribution and distribute adaptations under the same or a
compatible license.
