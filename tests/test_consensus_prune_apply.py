"""Tests for bin/consensus_prune_apply.sh

Covers the runtime behaviour of the post-consensus prune helper:
subtraction logic, early-exit paths, stats output, optional snapshots,
apply-script failure, and round-copy persistence.
"""
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "consensus_prune_apply.sh"

_APPLY_STUB_OK = """\
#!/usr/bin/env perl
use strict;
my ($fasta, $ids, $tmp, $stats) = @ARGV;
open my $in, "<", $fasta or die "open $fasta: $!";
open my $out, ">", $tmp  or die "open $tmp: $!";
while (<$in>) { print $out $_ }
close $in; close $out;
open my $sf, ">", $stats or die "open $stats: $!";
print $sf "pruned_count\\t0\\n";
close $sf;
"""
_APPLY_STUB_FAIL = "#!/usr/bin/env perl\nexit 1;\n"


def _make_apply_stub(tmp_path, succeed=True):
    stub = tmp_path / "apply_stub.pl"
    stub.write_text(_APPLY_STUB_OK if succeed else _APPLY_STUB_FAIL, encoding="utf-8")
    return stub


def _setup(tmp_path):
    """Return a dict of default required paths; fasta is pre-populated."""
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(">read1\nACGT\n>read2\nACGT\n", encoding="utf-8")
    return dict(
        prune_ids    = tmp_path / "prune_ids.list",
        recovery_ids = tmp_path / "recovery_ids.list",
        fasta        = fasta,
        fasta_tmp    = tmp_path / "fasta.tmp",
        apply_stats  = tmp_path / "apply_stats.tsv",
        prune_stats  = tmp_path / "prune_stats.tsv",
        round_cp     = tmp_path / "round_reads.fasta",
        lock_dir     = tmp_path / "qced.lock",
        apply_script = _make_apply_stub(tmp_path),
    )


def _run(
    tmp_path,
    *,
    apply_last=None,
    final_last=None,
    c1_prune_ids=None,
    pruned_barrier=None,
    pruned_archive=None,
    lock_wait="5",
    **paths,
):
    cmd = [
        "bash", str(SCRIPT),
        "--prune-ids",    str(paths["prune_ids"]),
        "--recovery-ids", str(paths["recovery_ids"]),
        "--fasta",        str(paths["fasta"]),
        "--fasta-tmp",    str(paths["fasta_tmp"]),
        "--apply-stats",  str(paths["apply_stats"]),
        "--prune-stats",  str(paths["prune_stats"]),
        "--round-cp",     str(paths["round_cp"]),
        "--lock-dir",     str(paths["lock_dir"]),
        "--apply-script", str(paths["apply_script"]),
        "--lock-wait",    lock_wait,
    ]
    if apply_last is not None:
        cmd += ["--apply-last", str(apply_last)]
    if final_last is not None:
        cmd += ["--final-last", str(final_last)]
    if c1_prune_ids is not None:
        cmd += ["--c1-prune-ids", str(c1_prune_ids)]
    if pruned_barrier is not None:
        cmd += ["--pruned-barrier", str(pruned_barrier)]
    if pruned_archive is not None:
        cmd += ["--pruned-archive", str(pruned_archive)]
    return subprocess.run(cmd, capture_output=True, text=True)


def _stats(prune_stats_path):
    """Parse prune_stats into a key→value dict."""
    rows = {}
    for line in prune_stats_path.read_text().splitlines():
        k, _, v = line.partition("\t")
        rows[k] = v
    return rows


# ---------------------------------------------------------------------------
# Early-exit paths (no FASTA modification)
# ---------------------------------------------------------------------------

def test_missing_prune_ids_exits_silently(tmp_path):
    """No prune-ids file → exit 0, FASTA and stats unchanged."""
    d = _setup(tmp_path)
    d["recovery_ids"].write_text("", encoding="utf-8")
    original = d["fasta"].read_text()

    result = _run(tmp_path, **d)

    assert result.returncode == 0, result.stderr
    assert d["fasta"].read_text() == original
    assert not d["prune_stats"].exists()


def test_empty_prune_ids_exits_silently(tmp_path):
    """Empty prune-ids file → exit 0, FASTA unchanged."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")
    original = d["fasta"].read_text()

    result = _run(tmp_path, **d)

    assert result.returncode == 0, result.stderr
    assert d["fasta"].read_text() == original


def test_full_recovery_skips_apply(tmp_path):
    """All prune IDs in recovery → empty final list → exit 0, apply not called."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\nread2\n", encoding="utf-8")
    d["recovery_ids"].write_text("read1\nread2\n", encoding="utf-8")
    original = d["fasta"].read_text()
    final_last = tmp_path / "final_last.list"

    result = _run(tmp_path, final_last=final_last, **d)

    assert result.returncode == 0, result.stderr
    assert d["fasta"].read_text() == original
    assert not d["apply_stats"].exists()
    rows = _stats(d["prune_stats"])
    assert rows["recovered_intersection_count"] == "2"
    assert rows["final_prune_count"] == "0"


# ---------------------------------------------------------------------------
# Recovery subtraction
# ---------------------------------------------------------------------------

def test_no_recovery_applies_full_prune_list(tmp_path):
    """Empty recovery → full list applied; stats show recovered_intersection_count=0."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\nread2\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")

    result = _run(tmp_path, **d)

    assert result.returncode == 0, result.stderr
    rows = _stats(d["prune_stats"])
    assert rows["recovered_intersection_count"] == "0"
    assert rows["final_prune_count"] == "2"
    assert d["apply_stats"].exists()


def test_partial_recovery_subtraction(tmp_path):
    """Recovery IDs subtracted from prune list; intersection count and final content correct."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\nread2\nread3\n", encoding="utf-8")
    d["recovery_ids"].write_text("read2\n", encoding="utf-8")
    final_last = tmp_path / "final_last.list"

    result = _run(tmp_path, final_last=final_last, **d)

    assert result.returncode == 0, result.stderr
    rows = _stats(d["prune_stats"])
    assert rows["recovered_intersection_count"] == "1"
    assert rows["final_prune_count"] == "2"
    applied = set(final_last.read_text().splitlines())
    assert applied == {"read1", "read3"}


# ---------------------------------------------------------------------------
# Stats output
# ---------------------------------------------------------------------------

def test_both_stats_rows_written(tmp_path):
    """recovered_intersection_count and final_prune_count are always written."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("a\nb\n", encoding="utf-8")
    d["recovery_ids"].write_text("a\n", encoding="utf-8")

    _run(tmp_path, **d)

    rows = _stats(d["prune_stats"])
    assert "recovered_intersection_count" in rows
    assert "final_prune_count" in rows


# ---------------------------------------------------------------------------
# Optional snapshot args
# ---------------------------------------------------------------------------

def test_apply_last_written_on_success(tmp_path):
    """--apply-last is written after successful apply."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")
    apply_last = tmp_path / "apply_last.tsv"

    result = _run(tmp_path, apply_last=apply_last, **d)

    assert result.returncode == 0, result.stderr
    assert apply_last.exists()


def test_final_last_written_with_applied_ids(tmp_path):
    """--final-last receives the post-recovery final list content."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\nread2\nread3\n", encoding="utf-8")
    d["recovery_ids"].write_text("read2\n", encoding="utf-8")
    final_last = tmp_path / "final_last.list"

    result = _run(tmp_path, final_last=final_last, **d)

    assert result.returncode == 0, result.stderr
    assert set(final_last.read_text().splitlines()) == {"read1", "read3"}


def test_final_last_written_even_when_empty(tmp_path):
    """--final-last is written (empty file) even when all IDs are recovered."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\n", encoding="utf-8")
    d["recovery_ids"].write_text("read1\n", encoding="utf-8")
    final_last = tmp_path / "final_last.list"

    result = _run(tmp_path, final_last=final_last, **d)

    assert result.returncode == 0, result.stderr
    assert final_last.exists()
    assert final_last.read_text().strip() == ""


def test_pruned_barrier_excludes_c1_ids(tmp_path):
    """Barrier accumulates applied IDs excluding C1 entries."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("A\nB\nC\nD\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")
    c1_ids = tmp_path / "c1_ids.list"
    c1_ids.write_text("C\nD\n", encoding="utf-8")
    barrier = tmp_path / "pruned_barrier.list"

    result = _run(tmp_path, c1_prune_ids=c1_ids, pruned_barrier=barrier, **d)

    assert result.returncode == 0, result.stderr
    assert barrier.exists()
    assert set(barrier.read_text().splitlines()) == {"A", "B"}
    rows = _stats(d["prune_stats"])
    assert rows["pruned_barrier_added_round"] == "2"
    assert rows["pruned_barrier_cumulative"] == "2"


def test_pruned_barrier_unions_with_previous_rounds(tmp_path):
    """Barrier file keeps prior entries and adds only new round-local non-C1 IDs."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("A\nB\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")
    barrier = tmp_path / "pruned_barrier.list"
    barrier.write_text("B\nZ\n", encoding="utf-8")

    result = _run(tmp_path, pruned_barrier=barrier, **d)

    assert result.returncode == 0, result.stderr
    assert set(barrier.read_text().splitlines()) == {"A", "B", "Z"}
    rows = _stats(d["prune_stats"])
    assert rows["pruned_barrier_added_round"] == "1"
    assert rows["pruned_barrier_cumulative"] == "3"


def test_pruned_archive_captures_applied_non_c1_reads(tmp_path):
    """Archive stores only the applied non-C1 pruned read sequences."""
    d = _setup(tmp_path)
    d["fasta"].write_text(
        ">A|COI|sup\nACGT\n>B|COI|sup\nTGCA\n>C|COI|sup\nCCCC\n",
        encoding="utf-8",
    )
    d["prune_ids"].write_text("A\nB\nC\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")
    c1_ids = tmp_path / "c1_ids.list"
    c1_ids.write_text("C\n", encoding="utf-8")
    archive = tmp_path / "pruned_archive.fasta"

    result = _run(tmp_path, c1_prune_ids=c1_ids, pruned_archive=archive, **d)

    assert result.returncode == 0, result.stderr
    assert archive.exists()
    text = archive.read_text(encoding="utf-8")
    assert ">A|COI|sup" in text
    assert ">B|COI|sup" in text
    assert ">C|COI|sup" not in text


def test_pruned_archive_excludes_recovery_subtracted_ids(tmp_path):
    """Archive is derived from final_prune, so recovery-subtracted IDs are not archived."""
    d = _setup(tmp_path)
    d["fasta"].write_text(
        ">read1|COI|sup\nACGT\n>read2|COI|sup\nTGCA\n",
        encoding="utf-8",
    )
    d["prune_ids"].write_text("read1\nread2\n", encoding="utf-8")
    d["recovery_ids"].write_text("read2\n", encoding="utf-8")
    archive = tmp_path / "pruned_archive.fasta"

    result = _run(tmp_path, pruned_archive=archive, **d)

    assert result.returncode == 0, result.stderr
    text = archive.read_text(encoding="utf-8")
    assert ">read1|COI|sup" in text
    assert ">read2|COI|sup" not in text


# ---------------------------------------------------------------------------
# Apply-script integration
# ---------------------------------------------------------------------------

def test_apply_failure_is_fatal(tmp_path):
    """Failed apply script → non-zero exit."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")
    fail_stub = tmp_path / "apply_fail.pl"
    fail_stub.write_text(_APPLY_STUB_FAIL, encoding="utf-8")
    d["apply_script"] = fail_stub

    result = _run(tmp_path, **d)

    assert result.returncode != 0


def test_lock_not_stolen_on_timeout(tmp_path):
    """Lock held by another process is not removed when acquire times out."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")
    # Simulate another process holding the lock.
    lockdir = Path(str(d["lock_dir"]) + ".lockdir")
    lockdir.mkdir()

    result = _run(tmp_path, lock_wait="0", **d)

    assert result.returncode != 0   # timeout is fatal
    assert lockdir.exists()         # other process's lock was NOT removed


def test_lock_released_on_mv_failure(tmp_path):
    """mv failure after successful apply still releases the acquired lock."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")

    # Force mv("$fasta_tmp", "$fasta") to fail: destination directory is read-only.
    ro_dir = tmp_path / "ro_dest"
    ro_dir.mkdir()
    d["fasta"] = ro_dir / "reads.fasta"
    d["fasta"].write_text(">read1\nACGT\n", encoding="utf-8")
    d["fasta_tmp"] = tmp_path / "fasta.tmp"
    lockdir = Path(str(d["lock_dir"]) + ".lockdir")

    ro_dir.chmod(0o555)
    try:
        result = _run(tmp_path, **d)
    finally:
        # Ensure tmpdir cleanup is possible on all platforms.
        ro_dir.chmod(0o755)

    assert result.returncode != 0
    assert not lockdir.exists(), "lockdir leaked after mv failure"


def test_round_cp_written_after_apply(tmp_path):
    """round_cp is created (copy of FASTA) after a successful apply."""
    d = _setup(tmp_path)
    d["prune_ids"].write_text("read1\n", encoding="utf-8")
    d["recovery_ids"].write_text("", encoding="utf-8")

    result = _run(tmp_path, **d)

    assert result.returncode == 0, result.stderr
    assert d["round_cp"].exists()
