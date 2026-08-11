# Deterministic target-filter and taxonomy benchmark

This benchmark starts after the initial FAST basecall. The FAST/LAST stage is a
compute-saving target filter: it tries to retain likely animal COI and plant
ITS2 reads for the more demanding Dorado models while dropping reads unlikely
to be targets. Labels such as `COI|Bacteria` and `COI|Fungi` are therefore
operational competitor buckets, not claims of exact biological origin.

The benchmark is deliberately independent of Dorado and POD5 so the same
sequence bytes can be replayed before and after a taxonomy/reference release.
Its primary outcomes are `retain_metazoa`, `retain_viridiplantae`, and
`exclude_off_target`. Exact origin is recorded only when independently
established; it is not required to justify filtering.

It has two observations for each query:

1. the actual first LAST alignment against the shipped FAST decoy index; and
2. a forced marker-lane BLAST using the production `megablast` settings.

The forced lane models the downstream state after FAST has routed a read to a
marker. It does not claim that every clean fixture query is misrouted. This
separation is necessary because routing and the contaminated marker database
are two links in Chain A.

## Cases

- **A:** bacterial LR799917/Wolbachia query sequences that hit separate,
  near-identical animal-database records carrying Metazoa host identities. The
  exact LR799917 accession is correctly labelled `COI|Bacteria` in the FAST
  database; it is not itself stored under a Metazoa identity downstream.
- **B1:** D11038, a bacterial sequence carrying bacterial taxid 1386. TaxonKit
  resolves its superkingdom as Bacteria and its kingdom as empty; the legacy
  JSON guard retains that empty kingdom.
- **B2_UNSUPPORTED:** the GBBAC4514 record preserves an important negative
  finding. Although its header says Metazoa and its taxid is the bacterial
  `Paracoccus` taxid 265, sequence competition strongly supports treating it as
  off-target. The same check was made for all ten records carrying taxids
  1386/265/613/1372 and is frozen in `homonym_taxid_audit.tsv`. The filter does
  not need to decide their exact origin, and they must not be used as evidence
  that an animal sequence was mapped to a bacterial homonym.
- **SYNTHETIC:** the marker collision at synthetic taxid -557 plus the
  Viridiplantae -561 control. `synthetic_lineage_cases.tsv` records both
  markers for both colliding IDs, including resolver-only combinations that do
  not currently have a marker-DB record.
- **CONTROL_HUMAN_OFFTARGET:** genuine human COI currently first-routes to the
  explicit `COI|Human` exclusion bucket. Its strong bacterial-bucket runner-up
  is a 99.41%, 96%-coverage human-like sequence stored in `COI|Bacteria`.
  This is a database-curation control, not a Metazoa-retention control.
- **CONTROL:** stable downstream Metazoa and Viridiplantae assignments.

The committed HAC-like variants have a fixed edit distance of about 1.7%; the
FAST-like variants about 5.1%. They contain deterministic substitutions and
homopolymer insertions/deletions. They are committed sequences, not random
simulation. The LR799917 and Wolbachia variants currently route to
`COI|Bacteria`; their forced marker-lane results show the downstream Chain-A
trap if a different read is misrouted, but they do not claim to reproduce the
complete two-link failure in this fixture. Under LAST 1542, their
target-minus-off-target score margins range from -289 to -435. Under the
shipped target policy, the human control has a configured-Metazoa minus
off-target-Human margin of -57 and is correctly excluded. The separate +27
value is only the pairwise Human-over-Bacteria difference; both labels are
off-target operational classes.

`legacy_expected.tsv` records the shipped behavior, including known defects.
Phase 3 must add a separately versioned corrected expectation; it must not
overwrite the legacy baseline.

`chain_a_reference_candidates.tsv` is the manually reviewed high-identity
subset. All five priority records are now confirmed reference-sequence
contamination, without claiming that their host specimens were misidentified.
`BOLD_COI-5P_GBMHH30183-19` is an exact 470-base match to independently
identified Wolbachia coxA while carrying the host taxid for *Telmapsylla
minuta*. `BOLD_COI-5P_ISUP118-14` is a full-query LR799917-class bacterial coxA
match; `BOLD_COI-5P_GBMIN70259-17` is a full-length 97.021% Wolbachia coxA
match. Independent same-species animal controls are strongly discordant with
those two records. `ISUP118-14`'s `-2704` identifier has one consistent Metazoa
mapping in the shipped global lineage file and is not evidence of the separate
cross-marker namespace collision represented by `-557` and `-561`.

The normal full validator re-derives the alignment columns with BLAST 2.15.0
using the committed LR799917 query and shipped COI index. Candidate enumeration
retains all tied hits and uses the production search settings except for a
raised target cap; `-max_target_seqs 1` hides the tied `ISUP118-14` record. A
high-identity cutoff is a review-queue criterion only, not an automatic
curation rule.

### Protocol-defined Chain-A similarity audit

`chain_a_audit_queries.tsv` declares three independently identified bacterial
coxA controls by accession and sequence checksum. The offline, database-release
tool `bin/audit_taxonomy_reference_candidates.py` extracts those sequences from
the shipped FAST source and searches the shipped downstream COI BLAST snapshot.
It is not called by `main.nf`, does not modify a reference or index, and contains
no sample/read identifiers, organism-name filters, or database-specific
dispositions. Database specificity resides in the manifest and resulting
curation artifacts.

The committed protocol uses BLAST 2.15.0 `blastn`, dust disabled, word size 11,
E-value 1e-20, one HSP per pair, a target cap of 1,000,000, minimum identity 85%,
and minimum coverage 80% of the shorter sequence. Priority review requires at
least 97% identity and 90% shorter-sequence coverage. Shorter-sequence coverage
retains a truncated reference that substantially covers the reference even
when it cannot cover the longer control. These thresholds define a finite
review protocol; they are not proof of biological exhaustiveness.

Against the checksummed shipped snapshot, the protocol produces 44
query/reference rows (five priority and 39 review); all are emitted as
`pending_adjudication`. For this snapshot the rows also correspond to 44 unique
reference IDs. The priority records are the three LR799917-associated records
already described above and the two Wolbachia-associated records. Lower-tier
similarity can include ordinary conserved animal COI, so neither tier is an
automatic contamination label. Only the separately reviewed
`chain_a_reference_candidates.tsv` records an adjudicated disposition.

`chain_a_priority_adjudication.tsv` freezes the evidence that resolved the two
formerly pending priority records. On 2026-07-31, NCBI GenBank identified
`LR799917.1` as bacterial coxA, `MG988837.1` as Wolbachia coxA, and `OM089800.1`
as mitochondrial *Bactericera albiventris* COI. The official BOLD API returned
`ISUP118-14` as a morphology-identified *Galerita bicolor* leg specimen and
returned the independent morphology-identified `BETN8083-20` control; their
sequence hashes match the shipped source FASTA. The API returned no current
document for `GBMIN70259-17`, which is recorded as missing external metadata
and is not itself used as evidence for the disposition.

The adjudication test independently verifies the source-record SHA-256 values,
cross-checks bacterial-control metrics against the generated audit, and
replays candidate-versus-same-species BLAST. `ISUP118-14` shares only an
8%-coverage local alignment with the independent *Galerita* COI control.
`GBMIN70259-17` is only 65.756% identical over 99% of its length to the
independently deposited mitochondrial *Bactericera* control. The combination
of full bacterial-control support and strong same-species mitochondrial
discordance establishes contaminated reference sequence while allowing the
host specimen metadata itself to remain valid.

### Local-only lower-tier adjudication

`chain_a_lower_tier_adjudication.tsv` evaluates all 39 review-tier records
without sending sequence data to an external service. The generic offline tool
`bin/adjudicate_taxonomy_reference_clusters.py` extracts the checksummed review
sequences plus only the five records explicitly marked `confirmed` in
`chain_a_reference_candidates.tsv`, then runs a local all-versus-all BLAST. The
confirmed records act as comparison anchors and are not emitted as lower-tier
output rows. The tool contains no record, organism, sample, or database-specific
dispositions.

The conservative conflict-candidate rule requires both:

1. the existing discovery alignment to an independently identified bacterial
   coxA control at at least 85% identity and 80% shorter-sequence coverage; and
2. a direct local alignment to another review record or confirmed anchor at at
   least 95% identity and 80% shorter-sequence coverage where the two records
   carry different, non-empty host-family assignments.

The bacterial-control threshold is a similarity screen, not proof of bacterial
origin. The second condition identifies a sequence/host-label conflict: the
assignments cannot safely be treated as mutually independent, correctly labelled
host COI references. It does not identify which endpoint is wrong, establish the
biological origin of either complete sequence, or challenge the physical
specimen IDs. Such rows are recorded as
`cross_family_sequence_label_conflict_candidate`; records lacking the direct
cross-family discriminator remain unresolved.

On the shipped snapshot, 24 review records satisfy both conditions and 15 remain
`unresolved_insufficient_local_discriminator`. Twenty-three are supported by a
review-record peer in four review-only components of sizes 16, 3, 2, and 2.
Examples include an Isopoda/Collembola pair at 99.380%, a
Coleoptera/Hymenoptera link at 97.833%, and identical sequences stored under
Chalcididae and Halictidae host assignments. The additional record,
`GMODL3842-22`, is supported by the confirmed `GBMIN70259-17` anchor at 96.018%
identity over 452 nt and 95.763% shorter-sequence coverage. That alignment covers
only 69.219% of the complete 653-nt review record, so it is not described as
proof that the whole record is bacterial. Components include anchors in their
IDs and sizes even though anchors are absent from the 39-row output. The 15
unresolved records must not be automatically removed or relabelled.

Regenerate the local adjudication and provenance with:

```bash
python3 bin/adjudicate_taxonomy_reference_clusters.py \
  --audit conf/taxonomy_regression/chain_a_similarity_audit.tsv \
  --confirmed-anchor-manifest \
    conf/taxonomy_regression/chain_a_reference_candidates.tsv \
  --reference-source-fasta db/COInr98_2024Jun_RioNegro_Brazil.fasta \
  --output conf/taxonomy_regression/chain_a_lower_tier_adjudication.tsv \
  --provenance-output \
    conf/taxonomy_regression/chain_a_lower_tier_adjudication_provenance.tsv
```

The artifact records `review_only` output and
`review_plus_confirmed_anchors` comparison scope, pins the anchor manifest, and
labels each supporting neighbor as `review_candidate` or `confirmed_anchor`.
Resolving the remaining 15 requires independently sourced sequence evidence or
explicit authorization for an external search; absence of that evidence is
preserved as uncertainty, not converted into a biological conclusion. Treating
all conflict candidates as quarantine inputs is a release-policy choice and must
not be reported as confirmation of bacterial origin.

### Downstream COI disposition manifest

`chain_a_downstream_coi_release_policy_v1.tsv` makes the correctness-first
release decision explicit rather than embedding it in pipeline or organism-
specific code. It maps the five confirmed priority contaminants and 24
cross-family conflict candidates to `quarantine`, while the 15 unresolved rows
map to `retain`. The policy preserves the three evidence classes; quarantine of
the conflict candidates is not reinterpreted as confirmation of bacterial
origin.

`bin/build_taxonomy_reference_disposition_manifest.py` validates that the five
priority and 39 lower-tier records are disjoint and exactly partition the audit,
then verifies every selected taxid and sequence SHA-256 against the pinned
shipped source FASTA snapshot. It emits one authoritative, reference-ID-sorted 44-row manifest
plus mechanically derived 29-row quarantine and 15-row retained-unresolved
projections. The retained projection is an unresolved subset, not a clean-
reference allowlist. Nonlisted FASTA records are outside this Chain-A queue and
are not silently adjudicated as clean.

All four outputs are fully staged before replacement. Each file is atomically
replaced, and the checksummed provenance output is written last as the artifact-
set commit marker; its absence or hash mismatch identifies an incomplete or
mixed set.

The manifest also records correctness-relevant release impact. The 29-record
quarantine represents 27 distinct sequence hashes; none has an exact retained
copy in the source snapshot. Three stored taxids (`1119366`, `1717526`, and
`650448`) would lose their only source record. These are disclosed consequences
of the conservative policy, not accuracy claims.

Regenerate all four disposition outputs with:

```bash
python3 bin/build_taxonomy_reference_disposition_manifest.py \
  --audit conf/taxonomy_regression/chain_a_similarity_audit.tsv \
  --audit-provenance \
    conf/taxonomy_regression/chain_a_similarity_audit_provenance.tsv \
  --confirmed-reference-manifest \
    conf/taxonomy_regression/chain_a_reference_candidates.tsv \
  --lower-tier-adjudication \
    conf/taxonomy_regression/chain_a_lower_tier_adjudication.tsv \
  --lower-tier-provenance \
    conf/taxonomy_regression/chain_a_lower_tier_adjudication_provenance.tsv \
  --release-policy \
    conf/taxonomy_regression/chain_a_downstream_coi_release_policy_v1.tsv \
  --reference-source-fasta db/COInr98_2024Jun_RioNegro_Brazil.fasta \
  --manifest-output \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1.tsv \
  --quarantine-output \
    conf/taxonomy_regression/chain_a_downstream_coi_quarantine_v1.tsv \
  --retained-output \
    conf/taxonomy_regression/chain_a_downstream_coi_retained_unresolved_v1.tsv \
  --provenance-output \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1_provenance.tsv
```

The disposition provenance's 792,926-record/792,925-unique-ID counts describe
the permissive line-start parser used by that frozen stage. They are not a
source-integrity claim. The strict audit below supersedes them for source-health
and base-selection decisions. All 44 disposition targets precede the discovered
boundary defect, so their evidence classes and release actions are unaffected.

### Downstream COI source/index integrity audit

`bin/audit_taxonomy_reference_source_integrity.py` performs a generic,
read-only stream comparison between the pinned source FASTA and the effective
legacy BLAST records. It verifies every BLAST component against the legacy
reference manifest, then compares OID order, complete titles (including signed
header taxids), lengths, sequences, and canonical stream digests. It reports
non-leading header delimiters without mutating the source or authorizing a
release policy.

The audit corrects the earlier "exact stale prefix" model. A `>` at one-based
source line 1,582,866 and zero-based byte offset 594,244,630 follows the first
200 nt of physical record 791,433 instead of starting a new line. The
repository's permissive line-start parser therefore merges the delimiter, an
embedded header candidate, and its 524-nt sequence into an apparent 857-
character sequence; the BLAST record contains only the initial 200 nt and
matches the analysis-split record. Splitting that delimiter for comparison only
produces 792,927 analysis-logical records and makes logical records 1–791,433
exactly match all 791,433 indexed records (`cf474aa6...` canonical stream
SHA-256).

Accordingly, there are 1,493 unindexed **line-start** records plus one embedded
record candidate, or 1,494 analysis-logical records after the index boundary.
The last two logical records share ID `WPB428`, and the second is empty. The
four-row anomaly table freezes the boundary, line-oriented mismatch, duplicate,
and empty record separately. These are observations only: no repair, tail
admission, canonical base, FASTA, or index is selected by this audit.

Both audit outputs are fully staged before replacement. Each file is atomically
replaced, and the checksummed provenance output is written last as the artifact-
set commit marker. Output paths that overlap an input, the audit executable, or
any pinned BLAST component are rejected before comparison.

Regenerate the audit and its checksummed provenance with:

```bash
python3 bin/audit_taxonomy_reference_source_integrity.py \
  --source-fasta db/COInr98_2024Jun_RioNegro_Brazil.fasta \
  --blast-database db/COInr98_2024Jun_RioNegro_Brazil \
  --blastdbcmd blastdbcmd \
  --reference-manifest \
    conf/state_compatibility/reference_manifest_legacy_v1.tsv \
  --reference-root . \
  --disposition-provenance \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1_provenance.tsv \
  --disposition-manifest \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1.tsv \
  --scope downstream_coi_only \
  --anomaly-output \
    conf/taxonomy_regression/chain_a_downstream_coi_source_integrity_v1.tsv \
  --provenance-output \
    conf/taxonomy_regression/chain_a_downstream_coi_source_integrity_v1_provenance.tsv
```

### Downstream COI base/repair policy

`chain_a_downstream_coi_base_repair_policy_v1.tsv` selects the effective legacy
BLAST OID stream as the behavior-preserving **corpus base**. It does not claim
that a future rebuilt database will preserve OIDs, tie behavior, or search
results. The selected base has 791,433 records, 485,462,968 bases, and canonical
stream SHA-256 `cf474aa6...`. Legacy OIDs define membership and order only; they
are not serialized, and only the retained records' relative legacy-OID order is
preserved after later exclusions.

The future serialization is declared as one UTF-8/LF record per two lines:
complete exported title followed by the uppercase exported index sequence. A
raw FASTA slice is forbidden. The 1,494-record/2,274,489-base analysis-logical
tail, including the embedded candidate and both duplicate/empty-record findings,
is outside the selected base and remains deferred. The boundary mismatch uses
the current indexed representation for this base. None of those decisions
repairs the shipped source, admits the tail, or assigns biological quality.

The existing dispositions are matched by `(reference_id, stored_taxid,
sequence_sha256)`, not ID or sequence alone. The policy fixes those components
as the first title token before its first pipe, the signed Kraken taxid token,
and SHA-256 of the uppercase ASCII sequence. The canonical builder
excludes the 29 quarantine records individually, retains the 15 unresolved
records without certifying them clean, retains all 791,389 nonlisted base
records, performs no implicit deduplication, and preserves relative legacy-OID
order. The frozen policy records the pre-construction projection of 791,404
records and deliberately remains unchanged as historical input to the completed
construction stage.

Validate the policy and all upstream bindings without a database or BLAST tool:

```bash
python3 bin/validate_taxonomy_reference_base_policy.py \
  --policy \
    conf/taxonomy_regression/chain_a_downstream_coi_base_repair_policy_v1.tsv \
  --source-integrity-anomalies \
    conf/taxonomy_regression/chain_a_downstream_coi_source_integrity_v1.tsv \
  --source-integrity-provenance \
    conf/taxonomy_regression/chain_a_downstream_coi_source_integrity_v1_provenance.tsv \
  --disposition-manifest \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1.tsv \
  --disposition-provenance \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1_provenance.tsv \
  --reference-manifest \
    conf/state_compatibility/reference_manifest_legacy_v1.tsv
```

The policy file remains policy-only and therefore retains its `deferred` and
`not_computed` values. Completion is recorded in the separate construction
provenance below; those historical policy fields must not be rewritten.

### Downstream COI canonical FASTA construction

`bin/build_taxonomy_canonical_fasta.py` consumes the verified legacy BLAST OID
stream, not a raw-FASTA slice. It verifies every live index component against
the legacy reference manifest, validates the frozen policy chain, and applies
all 44 dispositions by full identity. It emits no BLAST index and changes no
runtime configuration or state identity.

The frozen result is:

- release ID: `downstream_coi_chain_a_canonical_v1`;
- FASTA basename:
  `downstream_coi_chain_a_canonical_v1.fasta`;
- 791,404 records and 485,444,705 bases;
- 29 excluded legacy OIDs / 18,263 excluded bases and all 15 retained
  dispositions preserved;
- canonical FASTA SHA-256
  `d0b3aca535fbadcd0da8dfc7218c08e7b4151c9562ffe3fd37baf6cdcaef1775`.

The large release-candidate FASTA is deliberately kept outside version control. It
must be written to a dedicated release directory outside `db/`; never use the
production database directory as an output. The small committed
`chain_a_downstream_coi_canonical_fasta_v1_excluded_oids.tsv` and
`chain_a_downstream_coi_canonical_fasta_v1_provenance.tsv` freeze the excluded
coordinates, ordinal-remapping rule, counts, hashes, inputs, and exact output
basename.

Reproduce into a fresh external directory and compare the generated small
artifacts with the committed copies:

```bash
release_dir=/path/to/downstream-coi-chain-a-canonical-v1
mkdir -p "$release_dir"

python3 bin/build_taxonomy_canonical_fasta.py \
  --policy \
    conf/taxonomy_regression/chain_a_downstream_coi_base_repair_policy_v1.tsv \
  --source-integrity-anomalies \
    conf/taxonomy_regression/chain_a_downstream_coi_source_integrity_v1.tsv \
  --source-integrity-provenance \
    conf/taxonomy_regression/chain_a_downstream_coi_source_integrity_v1_provenance.tsv \
  --disposition-manifest \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1.tsv \
  --disposition-provenance \
    conf/taxonomy_regression/chain_a_downstream_coi_disposition_v1_provenance.tsv \
  --reference-manifest \
    conf/state_compatibility/reference_manifest_legacy_v1.tsv \
  --reference-root . \
  --blast-database db/COInr98_2024Jun_RioNegro_Brazil \
  --blastdbcmd blastdbcmd \
  --scope downstream_coi_only \
  --release-id downstream_coi_chain_a_canonical_v1 \
  --output-fasta \
    "$release_dir/downstream_coi_chain_a_canonical_v1.fasta" \
  --excluded-oids-output \
    "$release_dir/chain_a_downstream_coi_canonical_fasta_v1_excluded_oids.tsv" \
  --provenance-output \
    "$release_dir/chain_a_downstream_coi_canonical_fasta_v1_provenance.tsv"

cmp "$release_dir/chain_a_downstream_coi_canonical_fasta_v1_excluded_oids.tsv" \
  conf/taxonomy_regression/chain_a_downstream_coi_canonical_fasta_v1_excluded_oids.tsv
cmp "$release_dir/chain_a_downstream_coi_canonical_fasta_v1_provenance.tsv" \
  conf/taxonomy_regression/chain_a_downstream_coi_canonical_fasta_v1_provenance.tsv
```

Existing outputs are rejected by default. Intentional regeneration requires
`--replace`; replacement invalidates and durably records removal of the old
provenance marker before installing the FASTA and excluded-OID table, then
installs and syncs the new provenance last. Thus a process failure or a
filesystem crash honoring `fsync` ordering cannot leave an old provenance file
claiming that a partially replaced artifact set is valid.

Index construction, a new reference/state identity, corrected benchmark
expectations, runtime qualification, and activation remain subsequent,
separately reviewed stages.

Regenerate the discovery table and its checksummed provenance with:

```bash
python3 bin/audit_taxonomy_reference_candidates.py \
  --query-manifest conf/taxonomy_regression/chain_a_audit_queries.tsv \
  --query-source-fasta db/targets_All_tagged_nr95.fa \
  --database db/COInr98_2024Jun_RioNegro_Brazil \
  --reference-source-fasta db/COInr98_2024Jun_RioNegro_Brazil.fasta \
  --output conf/taxonomy_regression/chain_a_similarity_audit.tsv \
  --provenance-output conf/taxonomy_regression/chain_a_similarity_audit_provenance.tsv
```

The qualified environment must supply BLAST 2.15.0. Byte-identical output is
expected only when the script, manifest, source FASTAs, BLAST index components,
tool version, and declared parameters match the provenance artifact.

`homonym_taxid_audit.tsv` uses each complete reference sequence and the
best-scoring bacterial LAST 1542 alignment against the shipped
`targets_All_tagged_nr95` index. Five near-identical matches are high-confidence
off-target evidence. Five 79–90% matches span 98–100% of the query, have
E-values from 1.5e-181 to 4.2e-253, and point to named bacterial genomes or
annotated bacterial oxidase CDSs; these are moderate off-target evidence, not
mere bucket-label matches. This supports filtering and quarantine without
claiming the exact source organism.

`bidirectional_decoy_audit.tsv` records the converse database defect: an
animal/human-like sequence exists in the bacterial exclusion bucket. For the
committed human query the legitimate `COI|Human` alignment scores 827 and wins;
the SZWG bacterial-bucket competitor scores 800. Both are exclusion buckets
under the shipped `COI|Metazoa` target rule, so this does not demonstrate a
target false exclusion or a target-margin hazard. It does demonstrate a
sequence/label inconsistency that belongs in the bidirectional curation audit.

## Evidence status

The benchmark proves that the downstream animal database can turn an injected
endosymbiont query into a Metazoa host-taxid result. It also reproduces the B1
empty-kingdom leak, the synthetic -557 collision, and bidirectional decoy
contamination.

It does **not** currently reproduce the upstream Chain-A transition. All
committed LR799917 and Wolbachia clean/HAC/FAST variants first-route to
`COI|Bacteria` under LAST 1542. Therefore the downstream trap is proven, while
its reachability and incidence in real runs remain unmeasured.

The corrected filter must be evaluated on both sides: off-target exclusion
reduces HAC/SUP basecalling work, while false exclusion of configured Metazoa
or Viridiplantae controls defeats the biological objective. Ambiguous reads
should be measured separately rather than forced into a biological-origin
label.

Phase 3 shadow evaluation must therefore report:

- target retention/false-exclusion rates for Metazoa COI and Viridiplantae ITS2;
- off-target exclusion and ambiguous rates;
- reads and bases sent to each expensive Dorado model versus the legacy filter;
- estimated basecalling time saved and the added classifier runtime.

An off-target bucket is successful when it safely avoids unnecessary
basecalling; it does not need to identify Bacteria or Fungi to species, genus,
or even exact kingdom.

A margin policy must be tri-state and sensitivity-preserving:

- retain when target evidence wins by a calibrated target margin;
- exclude only when off-target evidence wins by a calibrated exclusion margin;
- otherwise mark ambiguous and retain by default for HAC/SUP. A secondary
  competitive check may exclude the read only when it supplies independently
  strong off-target evidence.

A symmetric “target margin not met ⇒ exclude” rule can create target false
exclusions and would defeat the purpose of the hard false-exclusion gate. This
fixture currently contains no configured-target near-tie suitable for choosing
a threshold. Raw LAST scores depend on alignment length, marker, and error
profile, so raw or normalized margins must be calibrated on representative
configured targets before enforcement.

A generic marker-length POD5 validates Dorado/workflow traversal but cannot by
itself estimate Chain-A incidence. That measurement needs representative
pre-filter FAST reads whose target/off-target status can be independently
checked. Shadow routing must retain the current first label plus the best score
per operational class and its margin, then compare those decisions with a
competitive downstream check and count the reads/bases each policy would send
to HAC/SUP.

These observations measure routing frequency, score-margin distributions, and
compute impact. They do not by themselves establish biological false-positive
or false-negative rates because the current and competitive classifiers may
share reference defects. Accuracy claims require independently adjudicated
controls or a reviewed subset classified against broader curated references.

The pipeline-side collector is opt-in:

```bash
nextflow run main.nf --fast_filter_shadow true ...
```

It writes `<barcode>_fast_filter_shadow.tsv` and
`<barcode>_fast_filter_shadow_summary.tsv` into each round directory. The
per-read file records the unchanged first-hit decision, best configured-target
and off-target LAST evidence, their score margin, and read length. The summary
records reads and bases by current decision, competition status, and their
joint candidate-disagreement buckets. These are routing-policy disagreements,
not biological false-positive/negative labels. No margin threshold is applied
and the files do not affect HAC/SUP selection. A diagnostic parse rejection
preserves the first hits already captured from the original LAST stream. A
tool or upstream LAST failure falls back to the unchanged legacy router and
omits the diagnostic files.

The collector success and forced tool-failure fallback paths were both
exercised in complete Nextflow 22.10.8 CPU rounds. The tiny qualification POD5
produced a 44-base no-alignment read, so those runs validate wiring and failure
isolation only; representative production reads are still required for
incidence and performance measurements.

## Run

Activate the qualified runtime, then run:

```bash
python3 bin/validate_taxonomy_classification_fixture.py \
  --taxonomy-data-dir db/taxonomy/releases/ncbi-taxdump-2024-06-24
```

For schema-only validation that does not invoke LAST, BLAST, or TaxonKit:

```bash
python3 bin/validate_taxonomy_classification_fixture.py --validate-only
```

## Coverage boundary

This replay directly exercises LAST, marker BLAST, taxid extraction, TaxonKit
superkingdom/kingdom resolution, the legacy empty-kingdom decision, and the
synthetic-map collision. It does not exercise Nextflow channels, rolling state,
OTU clustering, consensus generation, or report rendering. The real
marker-length POD5 round remains the workflow/hardware fixture; a genuine
Chain-A POD5 should be added if one becomes available.

The Python tests validate the committed snapshot and parser behavior without
requiring LAST in ordinary CI. Running
`validate_taxonomy_classification_fixture.py` without `--validate-only` in the
qualified runtime re-derives first hits, best per-class scores, and margins from
LAST 1542 before comparing them with the snapshot. LAST's subset seeds find
divergent target-class competitors for the Wolbachia variants that the BLAST
proxy used in independent review did not recover. BLAST-derived margins must
therefore not replace the production-LAST baseline.
