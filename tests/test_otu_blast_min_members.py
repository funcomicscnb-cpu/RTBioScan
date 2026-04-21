import subprocess
import hashlib
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FILTER = REPO_ROOT / "bin" / "otu_filter_reads_by_otu_size.pl"


def _run_filter(
    tmp_path: Path,
    clstr_text: str,
    fasta_text: str,
    min_members: str,
    missing_policy: str | None = None,
):
    clstr = tmp_path / "in.clstr"
    fasta = tmp_path / "in.fasta"
    hash_map = tmp_path / "in.hash_map.tsv"
    out_fasta = tmp_path / "out.fasta"
    out_stats = tmp_path / "out.stats.tsv"
    out_kept = tmp_path / "out.kept_otus.tsv"
    clstr.write_text(clstr_text, encoding="utf-8")
    fasta.write_text(fasta_text, encoding="utf-8")
    _write_hash_map_from_fasta(fasta, hash_map)
    cmd = [
        "perl",
        str(FILTER),
        str(clstr),
        str(hash_map),
        str(fasta),
        min_members,
        str(out_fasta),
        str(out_stats),
        str(out_kept),
    ]
    if missing_policy is not None:
        cmd.append(missing_policy)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result, out_fasta, out_stats, out_kept


def _read_stats(path: Path) -> dict[str, str]:
    data = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        k, v = line.split("\t", 1)
        data[k] = v
    return data


def _write_hash_map_from_fasta(fasta: Path, out_map: Path) -> None:
    lines = fasta.read_text(encoding="utf-8").splitlines()
    cur_id = None
    seq_parts = []
    rows = []
    for line in lines:
        if line.startswith(">"):
            if cur_id is not None:
                h = hashlib.md5("".join(seq_parts).upper().encode("utf-8")).hexdigest()
                rows.append(f"{cur_id}\t{h}")
            cur_id = line[1:].split()[0]
            seq_parts = []
        else:
            seq_parts.append(line.strip())
    if cur_id is not None:
        h = hashlib.md5("".join(seq_parts).upper().encode("utf-8")).hexdigest()
        rows.append(f"{cur_id}\t{h}")
    out_map.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")


def test_filter_keeps_only_reads_from_otus_meeting_threshold(tmp_path: Path) -> None:
    clstr = (
        ">Cluster 0\n"
        "0\t100nt, >r1|COI|sup|barcode=s1|adapter=s1... *\n"
        "1\t100nt, >r2|COI|hac|barcode=s1|adapter=s1... at +/99.0%\n"
        ">Cluster 1\n"
        "0\t100nt, >r3|COI|sup|barcode=s1|adapter=s1... *\n"
        "1\t100nt, >r4|COI|hac|barcode=s1|adapter=s1... at +/99.0%\n"
        "2\t100nt, >r5|COI|fast|barcode=s1|adapter=s1... at +/99.0%\n"
    )
    fasta = (
        ">r1|COI|sup|barcode=s1|adapter=s1 desc one\nAAAA\n"
        ">r2|COI|hac|barcode=s1|adapter=s1 desc two\nCCCC\n"
        ">r3|COI|sup|barcode=s1|adapter=s1 desc three\nGGGG\n"
        ">r4|COI|hac|barcode=s1|adapter=s1 desc four\nTTTT\n"
        ">r5|COI|fast|barcode=s1|adapter=s1 desc five\nACAC\n"
        ">rX|COI|sup|barcode=s1|adapter=s1 desc missing\nCACA\n"
    )
    result, out_fasta, out_stats, out_kept = _run_filter(tmp_path, clstr, fasta, "3")
    assert result.returncode == 0, result.stderr
    out = out_fasta.read_text(encoding="utf-8")
    assert ">r1|COI|sup|barcode=s1|adapter=s1 desc one" not in out
    assert ">r2|COI|hac|barcode=s1|adapter=s1 desc two" not in out
    assert ">r3|COI|sup|barcode=s1|adapter=s1 desc three" in out
    assert ">r4|COI|hac|barcode=s1|adapter=s1 desc four" in out
    assert ">r5|COI|fast|barcode=s1|adapter=s1 desc five" in out
    assert ">rX|COI|sup|barcode=s1|adapter=s1 desc missing" in out
    kept_otus = out_kept.read_text(encoding="utf-8").splitlines()
    assert kept_otus == ["CLUST_1"]
    stats = _read_stats(out_stats)
    assert stats["min_members"] == "3"
    assert stats["total_otus"] == "2"
    assert stats["kept_otus"] == "1"
    assert stats["total_reads"] == "6"
    assert stats["kept_reads"] == "4"
    assert stats["reads_missing_from_clstr"] == "1"


def test_min_zero_is_passthrough(tmp_path: Path) -> None:
    clstr = (
        ">Cluster 0\n"
        "0\t100nt, >r1... *\n"
    )
    fasta = (
        ">r1 desc\nAAAA\n"
        ">r2 desc\nCCCC\n"
    )
    result, out_fasta, out_stats, out_kept = _run_filter(tmp_path, clstr, fasta, "0")
    assert result.returncode == 0, result.stderr
    assert out_fasta.read_text(encoding="utf-8") == fasta
    assert out_kept.read_text(encoding="utf-8").splitlines() == ["CLUST_0"]
    stats = _read_stats(out_stats)
    assert stats["min_members"] == "0"
    assert stats["total_reads"] == "2"
    assert stats["kept_reads"] == "2"
    assert stats["dropped_reads"] == "0"


def test_malformed_clstr_fails(tmp_path: Path) -> None:
    clstr = (
        "not a clstr line\n"
        ">Cluster 0\n"
    )
    fasta = ">r1\nAAAA\n"
    result, *_ = _run_filter(tmp_path, clstr, fasta, "3")
    assert result.returncode != 0
    assert "malformed .clstr line" in result.stderr


@pytest.mark.parametrize("bad_min", ["-1", "x", "2.5"])
def test_invalid_min_members_fails(tmp_path: Path, bad_min: str) -> None:
    clstr = ">Cluster 0\n0\t100nt, >r1... *\n"
    fasta = ">r1\nAAAA\n"
    result, *_ = _run_filter(tmp_path, clstr, fasta, bad_min)
    assert result.returncode != 0
    assert "invalid min_members" in result.stderr


def test_missing_policy_drop_excludes_unmapped_reads(tmp_path: Path) -> None:
    clstr = (
        ">Cluster 0\n"
        "0\t100nt, >r1... *\n"
    )
    fasta = (
        ">r1 desc\nAAAA\n"
        ">r2 desc\nCCCC\n"
    )
    result, out_fasta, out_stats, _ = _run_filter(tmp_path, clstr, fasta, "1", missing_policy="drop")
    assert result.returncode == 0, result.stderr
    out = out_fasta.read_text(encoding="utf-8")
    assert ">r1 desc" in out
    assert ">r2 desc" not in out
    stats = _read_stats(out_stats)
    assert stats["missing_policy"] == "drop"
    assert stats["reads_missing_from_clstr"] == "1"
    assert stats["kept_reads"] == "1"


def test_empty_clstr_is_allowed_and_reported(tmp_path: Path) -> None:
    clstr = ""
    fasta = (
        ">r1 desc\nAAAA\n"
        ">r2 desc\nCCCC\n"
    )
    result, out_fasta, out_stats, out_kept = _run_filter(tmp_path, clstr, fasta, "3", missing_policy="drop")
    assert result.returncode == 0, result.stderr
    assert out_fasta.read_text(encoding="utf-8") == ""
    assert out_kept.read_text(encoding="utf-8") == ""
    stats = _read_stats(out_stats)
    assert stats["clstr_has_clusters"] == "0"
    assert stats["total_otus"] == "0"
    assert stats["reads_missing_from_clstr"] == "2"
