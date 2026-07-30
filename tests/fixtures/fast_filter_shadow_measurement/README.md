# FAST shadow measurement fixtures

These fixtures validate measurement wiring and aggregation. They are not
representative production evidence and must not be used to choose routing
thresholds or claim biological accuracy.

Run the measurement harness over one or more detail TSVs or directories:

```bash
python3 bin/summarize_fast_filter_shadow.py \
  --input tests/fixtures/fast_filter_shadow_measurement \
  --output-prefix /tmp/fast-shadow-measurement
```

The harness verifies each paired producer summary before writing aggregate,
per-round, input-provenance, and JSON outputs. `checksums.sha256` pins the
committed fixture bytes.

## Provenance tiers

- `nextflow_no_alignment_*` is the captured output from the complete
  Nextflow 22.10.8 CPU shadow-success round documented in
  `docs/internal/taxonomy_phase2/README.md`. It proves the real process output
  schema and the one-read/no-alignment path.
- `last1542_aligned_*` was produced by LAST 1542 against the shipped
  `db/targets_All_tagged_nr95` index, using the committed deterministic
  taxonomy fixture. It exercises target/off-target-only and positive/negative
  competition margins, but it is still a designed qualification fixture.
- `synthetic_tie_*` was produced by the real `fast_filter_shadow.py` helper
  from the committed FASTA and crafted BlastTab stream. It exists only to
  cover the exact-tie path absent from the aligned qualification fixture.

## Regeneration

Aligned fixture (requires the qualified runtime and shipped ignored index):

```bash
lastal db/targets_All_tagged_nr95 \
  conf/taxonomy_regression/classification_fixture.fa \
  -f BlastTab -P 1 |
python3 bin/fast_filter_shadow.py \
  --fasta conf/taxonomy_regression/classification_fixture.fa \
  --legacy-out /tmp/last1542_legacy.tsv \
  --shadow-out /tmp/last1542_aligned_fast_filter_shadow.tsv \
  --summary-out /tmp/last1542_aligned_fast_filter_shadow_summary.tsv \
  --targets 'COI|ITS2' \
  --target-taxa 'Metazoa|Viridiplantae' \
  --round-barcode qualified-last1542-fixture
```

Synthetic tie fixture:

```bash
python3 bin/fast_filter_shadow.py \
  --fasta tests/fixtures/fast_filter_shadow_measurement/synthetic_tie.fa \
  --legacy-out /tmp/synthetic_tie_legacy.tsv \
  --shadow-out /tmp/synthetic_tie_fast_filter_shadow.tsv \
  --summary-out /tmp/synthetic_tie_fast_filter_shadow_summary.tsv \
  --targets 'COI|ITS2' \
  --target-taxa 'Metazoa|Viridiplantae' \
  --round-barcode synthetic-tie \
  < tests/fixtures/fast_filter_shadow_measurement/synthetic_tie.blasttab
```

The aligned input FASTA SHA-256 is
`d6975dc4f614fa1c04af1732e6ba665e7191bacfc981993c04426490d2192019`.
The shipped LAST `.prj` SHA-256 is
`01715bb5447edb62ddfe3218607e1d0b46c3594b5e9cfb67ea9b27e96ec12a31`
and records `version=1542`.
