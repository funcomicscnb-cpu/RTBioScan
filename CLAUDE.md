# RTBioScan — Claude Code Notes

## Linux Setup Requirements

### Conda environment
Create the `rtbioscan` conda environment before running the pipeline or tests:
```bash
conda env create -f environment.yml
conda activate rtbioscan
```
`environment.yml` pins `r-base =4.3` specifically — R 4.4+ breaks Bioconductor
package compatibility (DECIPHER, Biostrings, muscle).

### Nextflow symlink
Tests in `tests/test_strict_round_join_runtime.py` resolve Nextflow as
`REPO_ROOT/nextflow`. On Linux, Nextflow is installed at `/home/pol/bin/nextflow`
(not bundled in the repo). A symlink must exist at the repo root:
```bash
ln -sf /home/pol/bin/nextflow nextflow
```
This symlink is in `.gitignore` and must be re-created after a fresh clone.

### Locale — LC_NUMERIC must be C (or en_US)
The system locale `es_ES.UTF-8` uses commas as decimal separators. This breaks:
- `awk printf "%.6f"` output: `0,333333` instead of `0.333333`
- `bash $EPOCHREALTIME`: `1234567890,123456` instead of `1234567890.123456`
- `awk` float comparisons: `0,333333 > 0.2` evaluates false

**For tests**: `conftest.py` at the repo root sets `LC_NUMERIC=C` and
`LANG=en_US.UTF-8` automatically — all test subprocesses inherit this.

**For running the pipeline directly**: set in your shell before activating the
conda env, or add to `~/.bashrc`:
```bash
export LC_NUMERIC=C
export LANG=en_US.UTF-8
```

Affected pipeline scripts (do not need modification if the env locale is correct):
- `bin/otu_refine_blastreport_parallel.sh` — `now_ms()` / `EPOCHREALTIME`
- `bin/otu_blast_filter_decide.sh` — awk float comparison
- `bin/consensus_assign_depth.sh` — awk pident comparison
- `bin/Consensus_simple.sh` — `top2_ratio` float comparison

### Known Linux-specific script bug (pending fix)
`bin/lib/lock_utils.sh` uses `${#array[@]-0}` (lines 26 and 76) which is
invalid bash syntax on Linux bash 4+/5 ("bad substitution"). This causes 7 test
failures in `tests/test_lock_utils.py`. Fix: replace `${#array[@]-0}` with
`${#array[@]}` — the array is always initialised by `__lock_utils_ensure_arrays`
before that point. **This requires modifying `bin/lib/lock_utils.sh`.**

## Running Tests
```bash
conda activate rtbioscan
# pytest inherits locale from conftest.py automatically
python -m pytest tests/ -q
```

Expected result on Linux (after above setup): 887 passed, 7 failed (lock_utils
bash syntax — pending script fix), 58 skipped, 4 pre-existing logic failures.
