# RTBioScan: Internal Performance Backlog (PR1-PR6)

> **Internal Docs:** [Index](internal/README.md) · [Architecture](internal/pipeline_architecture.md) · **Performance Backlog**

> **Audience:** Maintainers and developers. This page is not intended for the public end-user documentation set.

This backlog is designed to improve runtime while preserving current analysis logic and outputs.
Each PR is intentionally scoped for independent review, testing, and rollback.

> **Note on line numbers**: Line numbers in the Patch Targets sections are indicative. The codebase has evolved since these PRs were written. Before implementing any PR, search for the relevant code by its surrounding context rather than relying on the line numbers directly.

## Baseline For All PRs
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

Before starting each PR:

1. Run a baseline round (or replay a fixed test fixture) and keep:
   - wall time per process from `.nextflow.log`
   - output checksums for key files:
     - `_state/otu_frozen_members.tsv`
     - `_state/qced_reads_hq_accumulated.fasta`
     - `Consensus/consolidated_consensus_ids.txt`
     - `${barcode}_blast_otu_pretax_rpt.txt`
     - `consensus_blast_report_full.txt`
2. Run regression tests:
   - `pytest -q tests/test_consensus_recovery_integrity.py tests/test_otu_freezing_integrity.py tests/test_consensus_selection.py tests/test_otu_state_tools.py`

---

## PR1 - Skip Active Clustering When Active Pool Unchanged
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

### Goal
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

Avoid running `cd-hit-est` on unchanged `ACTIVE_POOL`.

### Patch Targets
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:1868)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:1925)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:1934)

### Exact Changes
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. In `OTU_definition`, compute pool fingerprint before and after merge:
   - `POOL_HASH_BEFORE=$(md5sum|md5 of ACTIVE_POOL or empty marker)`
   - `POOL_HASH_AFTER=...` after `otu_pool_merge_prefer_new.pl`.
2. Set `POOL_CHANGED=1` if hashes differ, else `0`.
3. Force `SKIP_CLUSTER=1` when `POOL_CHANGED=0`.
4. Reuse previous `${STATE_DIR}/qced_reads_nr.fasta.clstr` exactly as current no-new path does.
5. Log:
   - `INFO: active pool changed=0/1`
   - `INFO: skipping cd-hit-est due to unchanged active pool`

### Acceptance Tests
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. New unit/integration test in `tests/test_otu_freezing_integrity.py`:
   - unchanged active pool path skips clustering and reuses prior `.clstr`.
2. Existing tests remain green:
   - `pytest -q tests/test_otu_freezing_integrity.py`
3. Functional parity check:
   - same OTU cluster file content for unchanged-pool rounds.

### Expected Runtime Gain
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- OTU_definition in stable rounds: `20-45%`
- End-to-end steady-state: `8-25%`

### Rollback Criteria
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Any difference in OTU cluster content for unchanged-pool rounds.
- Any case where `.clstr` is missing after skip path.
- Rollback action: remove `POOL_CHANGED` gating and restore prior `SKIP_CLUSTER` logic.

---

## PR2 - Replace Repeated Full Frozen-Members Dedup With Incremental Seen-Index
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

### Goal
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

Remove repeated full-file dedup of `otu_frozen_members.tsv`.

### Patch Targets
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:1766)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:1856)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:1974)
- New helper: `bin/otu_members_append_unique.pl`

### Exact Changes
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Create `otu_members_append_unique.pl`:
   - Inputs: existing members file, append chunk, seen index path.
   - Appends only unseen `(frozen_id, read_id)` pairs.
   - Updates seen index atomically (`.tmp` + rename).
2. Replace each `cat ... >> FROZEN_MEMBERS` + full `awk '!seen...'` with helper call.
3. Initialize seen index in `_state` if missing.

### Acceptance Tests
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Add tests in `tests/test_otu_freezing_integrity.py`:
   - duplicate chunk rows do not duplicate final members.
   - crash-safe temp file handling does not corrupt index.
2. Existing strict/legacy snapshot tests pass.
3. Compare sorted pair-set against baseline on replayed rounds.

### Expected Runtime Gain
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- OTU_definition at larger state sizes: `5-15%`
- End-to-end: `2-8%`

### Rollback Criteria
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Any missing membership row vs baseline pair-set.
- Any index corruption causing false drops.
- Rollback action: restore original append+full-dedup blocks.

---

## PR3 - Incremental Consensus Cache Sync (Atomic)
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

### Goal
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

Reduce IO from full cache copy/move each consensus round.

### Patch Targets
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:3071)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:3287)

### Exact Changes
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Add helper function in process script:
   - `sync_dir_atomic SRC DST`
2. Prefer `rsync -a --delete` if available.
3. Fallback to `cp -R` into temp dir + atomic rename.
4. Keep behavior exactly same:
   - restore `.cache`
   - persist `Consensus` + consolidated IDs

### Acceptance Tests
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Add test in `tests/test_consensus_recovery_integrity.py`:
   - cached consensus survives round transitions.
2. Existing cached-only consolidated-ID tests pass.
3. Manual spot check:
   - same file count and checksums under `Consensus/.cache` pre/post.

### Expected Runtime Gain
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Consensus IO-heavy rounds: `10-30%`
- End-to-end: `3-10%`

### Rollback Criteria
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Missing or stale cache files after round.
- Any inconsistency in `consolidated_consensus_ids.txt`.
- Rollback action: revert to current `cp -r` + `rm -rf` + `mv`.

---

## PR4 - Plot Signature Guards In `getting_run_summary`
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

### Goal
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

Avoid rerunning unchanged R plots.

### Patch Targets
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:3483)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:3558)

### Exact Changes
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Add per-plot signature files in `_state/plot_sigs/`.
2. Signature input:
   - source TSV path + size + mtime (or content hash for smaller files).
3. For each R script:
   - if signature unchanged and output exists, skip and log.
   - else run script and update signature.

### Acceptance Tests
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. New script-level test in `tests/test_reporting_plots_integrity.py`:
   - unchanged input skips rerun.
   - changed input triggers rerun.
2. Existing report-generation tests remain green.

### Expected Runtime Gain
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- `getting_run_summary`: `20-60%`
- End-to-end: `5-20%`

### Rollback Criteria
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Missing expected plot after changed input.
- Stale plot when input changed.
- Rollback action: remove signature guards and always run plots.

---

## PR5 - Consensus Refactor: Single-Pass Parse + Batched `seqtk`
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

### Goal
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

Reduce repeated scans/extractions in `Consensus_simple.sh`.

### Patch Targets
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- [bin/Consensus_simple.sh](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/bin/Consensus_simple.sh:307)
- [bin/Consensus_simple.sh](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/bin/Consensus_simple.sh:486)
- [bin/Consensus_simple.sh](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/bin/Consensus_simple.sh:657)

### Exact Changes
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Parse blast rows once into normalized table:
   - `read_id`, `sample`, `otu_key`, `model`, `barcode`, `adapter`.
2. Build per-sample/per-OTU mappings from parsed table in one pass.
3. Build union ID list per sample and run one `seqtk subseq`.
4. Split extracted FASTA back per OTU using exact ID map.
5. Preserve current mismatch policy semantics and debug logs.

### Acceptance Tests
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Extend `tests/test_consensus_recovery_integrity.py`:
   - exact OTU routing with close prefixes (`OTUB_12` vs `OTUB_123`).
   - no-loss mapping in union extraction path.
2. Keep all existing consensus tests passing.
3. Output parity check:
   - consensus headers and sequences identical vs baseline fixture.

### Expected Runtime Gain
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Consensus stage: `20-40%` (many OTUs)
- End-to-end: `8-18%`

### Rollback Criteria
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Any OTU read mis-routing.
- Any consensus sequence/header divergence on fixed fixtures.
- Rollback action: restore per-OTU extraction path.

---

## PR6 - Parallelize Target1/Target2 Consensus BLAST Paths
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

### Goal
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

Reduce wall time in consensus tax assignment when both targets are enabled.

### Patch Targets
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:3107)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:3156)
- [main.nf](/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/main.nf:3206)

### Exact Changes
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Run target1 and target2 branches in parallel subshells.
2. Split threads deterministically:
   - `T1_THREADS=max(1, THREADS/2)`, `T2_THREADS=THREADS-T1_THREADS`.
3. After `wait`, merge reports with stable sort to avoid ordering drift.
4. Keep cache logic unchanged per target.

### Acceptance Tests
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. Add tests in `tests/test_consensus_target_parallelism.py`:
   - both targets produce same merged output as serial baseline (order-normalized).
   - works when only one target is configured.
2. Existing consensus report tests pass.

### Expected Runtime Gain
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Consensus BLAST segment: `15-35%` (both targets active)
- End-to-end: `5-15%`

### Rollback Criteria
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

- Nondeterministic merged report content after normalization.
- Any target output missing under dual-target runs.
- Rollback action: restore serial target execution.

---

## Release Order
[back to Top](#rtbioscan-performance-backlog-pr1-pr6)

1. PR1  
2. PR2  
3. PR3  
4. PR4  
5. PR5  
6. PR6

This order maximizes immediate wins while minimizing correctness risk.
