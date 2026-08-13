from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _consensus_block() -> str:
    text = (REPO_ROOT / "main.nf").read_text(encoding="utf-8")
    return text.split("process consensus {", 1)[1].split("process _reporting_consensus_tax {", 1)[0]


def test_consensus_blast_worker_failfast_is_wired() -> None:
    consensus_block = _consensus_block()
    assert 'if ! blastn -query ${barcode}_preblast_new\\${_idx}.fasta' in consensus_block
    assert 'wait "\\$_pid" || true' not in consensus_block
    assert "worker_failures_blast" in consensus_block
    assert "METRIC: worker_failures_blast=" in consensus_block


def test_consensus_target_split_is_header_driven_and_failfast() -> None:
    """A1: target split uses header tokens directly and fails fast on awk errors."""
    consensus_block = _consensus_block()
    assert "_split_rc=0" in consensus_block
    assert 'awk -v target="|\\${_t}|" ' in consensus_block
    assert 'BEGIN { RS=">"; ORS="" }' in consensus_block
    assert 'sub(/[[:space:]].*/, "", header)' in consensus_block
    assert 'if (index(header, target) > 0)' in consensus_block
    assert '"\\$_split_rc" -ne 0' in consensus_block
    assert "ERROR: consensus target split failed" in consensus_block
    assert "No consensus sequences matched this target" in consensus_block
    assert 'seqkit grep -p' not in consensus_block


def test_blast_worker_first_failure_kills_remaining() -> None:
    """A1b: on first BLAST worker failure, remaining workers are killed before exit."""
    consensus_block = _consensus_block()
    # kill loop present in wait section
    assert 'kill "\\$_k" 2>/dev/null || true' in consensus_block
    # immediate exit after first failure (not a counter-only approach)
    assert "exit 1" in consensus_block


def test_taxonkit_cache_metadata_invalidation_wired() -> None:
    """A6: taxdb signature sidecar and invalidation logic is present."""
    consensus_block = _consensus_block()
    assert "TAXONKIT_CACHE_META=" in consensus_block
    assert "_TAXDB_SIG=" in consensus_block
    assert "_CACHED_TAXDB_SIG=" in consensus_block
    assert "taxdb signature changed" in consensus_block
    # meta persisted after cache update
    assert "_TAXDB_SIG" in consensus_block.split("TAXONKIT_CACHE_META=")[1]


def test_blast_cache_key_uses_variables() -> None:
    """C1: BLAST cache key references _WORD_SIZE/_QCOV, not hardcoded values."""
    consensus_block = _consensus_block()
    assert "_WORD_SIZE=50" in consensus_block
    assert "_QCOV=50" in consensus_block
    # Nextflow escapes $ as \\$, so the cache key has \\${_WORD_SIZE}
    assert 'word=\\${_WORD_SIZE}' in consensus_block
    # No raw hardcoded value in the cache key line
    cache_key_line = [l for l in consensus_block.splitlines() if "_KEY=" in l and "word=" in l]
    assert cache_key_line, "cache key line not found"
    assert "word=50|" not in cache_key_line[0]


def test_consensus_cleanup_preserves_declared_join_output() -> None:
    consensus_block = _consensus_block()
    assert 'file("${barcode}_preblastreport_join.txt"), file("consensus_blast_report_full.txt"), file("consensus_round_provenance.tsv"), val(round_generation_token), val(round_lock_scope) into report_consensus' in consensus_block
    cleanup_block = consensus_block.split("# B1: clean up taxonomy and BLAST temp files from work directory", 1)[1]
    assert '${barcode}_preblastreport*.txt' not in cleanup_block
    assert '${barcode}_preblastreport[0-9]*.txt' in cleanup_block


def test_taxonomy_cache_joins_are_empty_safe() -> None:
    consensus_block = _consensus_block()
    assert 'BEGIN{FS=OFS="\\t"; first=ARGV[1]}' in consensus_block
    assert 'BEGIN{first=ARGV[1]}' in consensus_block
    assert "FILENAME==first { c[\\$1]=1; next }" in consensus_block
    assert "!(\\$1 in c) { print \\$1 }" in consensus_block
    assert "FILENAME==first { lin[\\$1]=\\$2; next }" in consensus_block
    third_join_block = consensus_block.split("fallback_lineage=", 1)[1].split("> tmp_tax_cols.tsv", 1)[0]
    assert "awk -v fb=" in third_join_block
    assert 'BEGIN{FS=OFS="\\t"; first=ARGV[1]}' in third_join_block
    assert "FILENAME==first { lin[\\$1]=\\$2; next }" in third_join_block
    assert "' tmp_tax.tsv tmp_idx.tsv \\" in third_join_block or "' tmp_tax.tsv tmp_idx.tsv" in third_join_block
    assert "FILENAME==cachedf" not in consensus_block
    assert "FILENAME==uniquef" not in consensus_block
    assert "FILENAME==mapf" not in consensus_block
    assert "FILENAME==taxf" not in consensus_block
    assert "FILENAME==idxf" not in consensus_block
    assert "NR==FNR { c[\\$1]=1; next }" not in consensus_block
    assert '-v cachedf=' not in consensus_block
    assert '-v uniquef=' not in consensus_block
    assert '-v mapf=' not in consensus_block
    assert '-v taxf=' not in consensus_block
    assert '-v idxf=' not in consensus_block


def test_consensus_taxid_filters_use_portable_numeric_match() -> None:
    consensus_block = _consensus_block()
    assert '\\$2 != "" && \\$2 !~ /[^0-9]/ {print \\$0}' in consensus_block
    assert '\\$1 != "" && \\$1 !~ /[^0-9]/ && \\$3 != "" { print \\$1, \\$3 }' in consensus_block
    assert '/^[0-9]+\\$/' not in consensus_block
