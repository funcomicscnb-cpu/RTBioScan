# Dorado 0.2.3 basecaller / 0.7.0 summary fixture

These deterministic fixtures capture the mixed-version compatibility boundary
used by RTBioScan's optional Dorado 0.2.3 recovery path.

- Basecaller: `0.2.3+4ed609d`
- Summary reader: `0.7.0+71cc7442`
- Device: Apple Metal
- Source POD5: the official Dorado v0.7.0 one-read 5 kHz qualification fixture
- Native basecalling models: v4.2-alpha FAST, HAC, and SUP
- Capture date: 2026-07-30

Absolute local paths in SAM `CL` fields were normalized. Read records, model
identities, sequence lengths, Q-score tags, and summary rows are preserved.
The fixture proves the reporting interface only; it is not biological or
throughput qualification evidence.
