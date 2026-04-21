import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HASH_MAP = REPO_ROOT / "bin" / "otu_hash_map_from_fasta.pl"
FILTER = REPO_ROOT / "bin" / "otu_filter_reads_by_otu_size.pl"
DECIDE = REPO_ROOT / "bin" / "otu_blast_filter_decide.sh"


def _prepare_common(tmp_path: Path) -> tuple[Path, Path, Path]:
    fasta = tmp_path / "reads.fasta"
    clstr = tmp_path / "reads.clstr"
    hash_map = tmp_path / "reads.hash_map.tsv"
    hash_counts = tmp_path / "reads.hash_counts.tsv"
    fasta.write_text(
        ">r1|COI|sup|barcode=s1|adapter=s1\nAAAA\n"
        ">r2|COI|sup|barcode=s1|adapter=s1\nCCCC\n"
        ">r3|COI|sup|barcode=s1|adapter=s1\nGGGG\n",
        encoding="utf-8",
    )
    clstr.write_text(
        ">Cluster 0\n"
        "0\t4nt, >r1|COI|sup|barcode=s1|adapter=s1... *\n"
        "1\t4nt, >r2|COI|sup|barcode=s1|adapter=s1... at +/99.0%\n",
        encoding="utf-8",
    )
    subprocess.run(
        [
            "perl",
            str(HASH_MAP),
            str(fasta),
            str(hash_map),
            str(hash_counts),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return fasta, clstr, hash_map


def _run_filter_and_decide(
    tmp_path: Path,
    *,
    mode: str,
    max_frac: str,
    no_clusters_policy: str,
    clstr_text: str | None = None,
):
    fasta, clstr, hash_map = _prepare_common(tmp_path)
    if clstr_text is not None:
        clstr.write_text(clstr_text, encoding="utf-8")

    filtered = tmp_path / "filtered.fasta"
    stats = tmp_path / "stats.tsv"
    kept = tmp_path / "kept.tsv"
    missing_policy = "drop" if mode == "enforce" else "keep"
    subprocess.run(
        [
            "perl",
            str(FILTER),
            str(clstr),
            str(hash_map),
            str(fasta),
            "3",
            str(filtered),
            str(stats),
            str(kept),
            missing_policy,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    decide = subprocess.run(
        [str(DECIDE), str(stats), mode, max_frac, no_clusters_policy],
        check=False,
        capture_output=True,
        text=True,
    )
    return fasta, filtered, stats, decide


def _decision(stdout: str) -> str:
    for line in stdout.splitlines():
        if line.startswith("decision\t"):
            return line.split("\t", 1)[1]
    return ""


def test_observe_uses_unfiltered_input(tmp_path: Path) -> None:
    fasta, filtered, _stats, decide = _run_filter_and_decide(
        tmp_path, mode="observe", max_frac="0.1", no_clusters_policy="fallback_unfiltered"
    )
    assert decide.returncode == 0, decide.stderr
    assert _decision(decide.stdout) == "use_unfiltered"
    # In observe, runtime should keep original input regardless of filter result.
    assert fasta.read_text(encoding="utf-8")
    assert filtered.read_text(encoding="utf-8")


def test_enforce_threshold_pass_uses_filtered_input(tmp_path: Path) -> None:
    _fasta, filtered, _stats, decide = _run_filter_and_decide(
        tmp_path, mode="enforce", max_frac="0.5", no_clusters_policy="fallback_unfiltered"
    )
    assert decide.returncode == 0, decide.stderr
    assert _decision(decide.stdout) == "use_filtered"
    out = filtered.read_text(encoding="utf-8")
    assert ">r1|COI|sup|barcode=s1|adapter=s1" not in out
    assert ">r2|COI|sup|barcode=s1|adapter=s1" not in out
    assert ">r3|COI|sup|barcode=s1|adapter=s1" not in out


def test_enforce_threshold_fail_exits_nonzero(tmp_path: Path) -> None:
    _fasta, _filtered, _stats, decide = _run_filter_and_decide(
        tmp_path, mode="enforce", max_frac="0.2", no_clusters_policy="fallback_unfiltered"
    )
    assert decide.returncode != 0
    assert "missing fraction" in decide.stderr


def test_enforce_no_clusters_fallback_unfiltered(tmp_path: Path) -> None:
    _fasta, _filtered, _stats, decide = _run_filter_and_decide(
        tmp_path,
        mode="enforce",
        max_frac="0",
        no_clusters_policy="fallback_unfiltered",
        clstr_text="",
    )
    assert decide.returncode == 0, decide.stderr
    assert _decision(decide.stdout) == "use_unfiltered"


def test_enforce_header_only_clstr_fallback_unfiltered(tmp_path: Path) -> None:
    _fasta, _filtered, _stats, decide = _run_filter_and_decide(
        tmp_path,
        mode="enforce",
        max_frac="0",
        no_clusters_policy="fallback_unfiltered",
        clstr_text=">Cluster 0\n",
    )
    assert decide.returncode == 0, decide.stderr
    assert _decision(decide.stdout) == "use_unfiltered"
    assert "reason\tno_clusters_fallback_unfiltered" in decide.stdout


def test_enforce_no_assignable_otus_fallback_unfiltered(tmp_path: Path) -> None:
    _fasta, _filtered, _stats, decide = _run_filter_and_decide(
        tmp_path,
        mode="enforce",
        max_frac="0",
        no_clusters_policy="fallback_unfiltered",
        clstr_text=(
            ">Cluster 0\n"
            "0\t4nt, >x_missing_1|COI|sup|barcode=s1|adapter=s1... *\n"
            "1\t4nt, >x_missing_2|COI|sup|barcode=s1|adapter=s1... at +/99.0%\n"
        ),
    )
    assert decide.returncode == 0, decide.stderr
    assert _decision(decide.stdout) == "use_unfiltered"
    assert "reason\tno_assignable_otus_fallback_unfiltered" in decide.stdout


def test_enforce_no_clusters_fail_policy(tmp_path: Path) -> None:
    _fasta, _filtered, _stats, decide = _run_filter_and_decide(
        tmp_path,
        mode="enforce",
        max_frac="1",
        no_clusters_policy="fail",
        clstr_text="",
    )
    assert decide.returncode != 0
    assert "no OTU cluster member records available" in decide.stderr
