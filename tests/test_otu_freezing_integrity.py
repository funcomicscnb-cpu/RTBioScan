import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HASH_MAP_SCRIPT = REPO_ROOT / "bin" / "otu_hash_map_from_fasta.pl"
COUNTS_BY_HASH_SCRIPT = REPO_ROOT / "bin" / "otu_counts_by_hash.pl"
FROZEN_FROM_CLSTR_SCRIPT = REPO_ROOT / "bin" / "otu_frozen_members_from_clstr.pl"
SNAPSHOT_SCRIPT = REPO_ROOT / "bin" / "otu_snapshot_frozen_members.pl"
PARSE_CLSTR_SCRIPT = REPO_ROOT / "bin" / "otu_parse_clstr.pl"
CONSENSUS_SCRIPT = REPO_ROOT / "bin" / "Consensus_simple.sh"
POOL_MERGE_SCRIPT = REPO_ROOT / "bin" / "otu_pool_merge_prefer_new.pl"
MEMBERS_APPEND_UNIQUE_SCRIPT = REPO_ROOT / "bin" / "otu_members_append_unique.pl"

UNTIL_CONSOLIDATED_PRUNE_AWK = r'''
function trim(v) {
    gsub(/\r/, "", v);
    return v;
}
function is_unusable(v) {
    return (v=="" || v=="NA" || v=="barcode");
}
function normalize_key(v,   n, parts, j, outv, last) {
    outv=v;
    n=split(v, parts, "_");
    if (n>1) {
        last=parts[n];
        if (last != "" && last !~ /[^0-9]/) {
            outv=parts[1];
            for (j=2; j<n; j++) outv=outv "_" parts[j];
        }
    }
    return outv;
}
function extract_sample_key(id,   n, f, i, adapter, barcode, key) {
    n=split(id, f, "|");
    adapter=""; barcode="";
    for (i=1; i<=n; i++) {
        if (f[i] ~ /^adapter=/) adapter=substr(f[i], 9);
        else if (f[i] ~ /^barcode=/) barcode=substr(f[i], 9);
    }
    key=adapter;
    if (is_unusable(key)) key="";
    if (key == "" && !is_unusable(barcode)) key=barcode;
    return key;
}
function extract_otu(id,   n, f, i, otu) {
    n=split(id, f, "|");
    otu="";
    for (i=1; i<=n; i++) {
        if (f[i] ~ /^OTUB_/) otu=f[i];
        else if (f[i] ~ /^OTU=/) otu=substr(f[i], 5);
    }
    return otu;
}
BEGIN{
    FS=OFS="\t";
    mode=MIXED_MODE;
    if (mode=="") mode="sample_scoped_only";
    noadapter_hint=NOADAPTER_HINT+0;
    has_sample_scope=0;
    has_global=0;
    while ((getline line < CONS) > 0) {
        if (line=="") continue;
        n=split(line, a, FS);
        if (n>=2) {
            sample=trim(a[1]); otu=trim(a[2]);
            if (sample != "" && otu != "") {
                cons_sample[sample "\t" otu]=1;
                cons_sample_name[sample]=1;
                has_sample_scope=1;
            }
        } else if (n==1) {
            otu=trim(a[1]);
            if (otu != "") {
                cons_global[otu]=1;
                has_global=1;
            }
        }
    }
    close(CONS);
    if (has_sample_scope && has_global) {
        if (mode=="fail") {
            print "ERROR: Mixed consolidated key formats detected in " CONS " (sample-scoped + global)." > "/dev/stderr";
            exit 2;
        }
        if (mode=="warn_and_sample_scoped") {
            print "WARN: Mixed consolidated key formats detected in " CONS "; using sample-scoped keys only." > "/dev/stderr";
        }
    }
    known_count=0;
    if (SAMPLES != "") {
        while ((getline sline < SAMPLES) > 0) {
            sample_key=trim(sline);
            sub(/^[ \t]+/, "", sample_key);
            sub(/[ \t]+$/, "", sample_key);
            if (sample_key != "" && !(sample_key in known_sample)) {
                known_sample[sample_key]=1;
                known_count++;
            }
        }
        close(SAMPLES);
    }
    if (SAMPLES=="" && noadapter_hint==1 && ("no_adapter" in cons_sample_name) && !("no_adapter" in known_sample)) {
        known_sample["no_adapter"]=1;
        known_count++;
    }
    if (SAMPLES == "" && FASTA != "") {
        while ((getline hline < FASTA) > 0) {
            if (hline !~ /^>/) continue;
            id=substr(hline,2);
            sub(/ .*/, "", id);
            sample_key=extract_sample_key(id);
            if (sample_key != "" && !(sample_key in known_sample)) {
                known_sample[sample_key]=1;
                known_count++;
            }
        }
        close(FASTA);
    }
}
/^>/{
    id=substr($0,2);
    sub(/ .*/, "", id);
    otu=extract_otu(id);
    raw_sample_key=extract_sample_key(id);
    norm_sample_key=normalize_key(raw_sample_key);
    allow_norm=0;
    if (norm_sample_key != "" && norm_sample_key != raw_sample_key) {
        if (SAMPLES != "") {
            if (!(raw_sample_key in known_sample) && (norm_sample_key in known_sample)) allow_norm=1;
        } else if (norm_sample_key in cons_sample_name) {
            if (!(norm_sample_key=="no_adapter" && noadapter_hint!=1)) allow_norm=1;
        }
    }
    keep=1;
    if (otu != "") {
        if (has_sample_scope) {
            if (raw_sample_key != "" && ((raw_sample_key "\t" otu) in cons_sample)) keep=0;
            else if (allow_norm && ((norm_sample_key "\t" otu) in cons_sample)) keep=0;
        } else {
            if (otu in cons_global) keep=0;
        }
    }
    if (keep) print id;
}
'''


def run_perl(script: Path, *args: object) -> None:
    subprocess.run(["perl", str(script), *[str(a) for a in args]], check=True)


def run_until_consolidated_active_ids(
    fasta: Path,
    consolidated_keys: Path,
    *,
    samples_file: Path | None = None,
    mixed_mode: str = "sample_scoped_only",
    no_adapter_hint: int = 0,
) -> list[str]:
    samples_arg = str(samples_file) if samples_file is not None else ""
    cp = subprocess.run(
        [
            "awk",
            "-v",
            f"CONS={consolidated_keys}",
            "-v",
            f"FASTA={fasta}",
            "-v",
            f"SAMPLES={samples_arg}",
            "-v",
            f"MIXED_MODE={mixed_mode}",
            "-v",
            f"NOADAPTER_HINT={no_adapter_hint}",
            UNTIL_CONSOLIDATED_PRUNE_AWK,
            str(fasta),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]


def write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def read_fasta_records(path: Path) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    header = ""
    seq_chunks: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith(">"):
            if header:
                records.append((header, "".join(seq_chunks)))
            header = raw[1:].split()[0]
            seq_chunks = []
        else:
            seq_chunks.append(raw.strip())
    if header:
        records.append((header, "".join(seq_chunks)))
    return records


def test_hash_map_keeps_full_ids(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">readA|COI|sup|barcode=s1|adapter=s1\nACGTACGT\n"
        ">readB|COI|hac|barcode=s1|adapter=s1\nACGTACGT\n",
        encoding="utf-8",
    )
    out_map = tmp_path / "hash_map.tsv"
    out_counts = tmp_path / "hash_counts.tsv"
    run_perl(HASH_MAP_SCRIPT, fasta, out_map, out_counts)

    map_rows = [ln.strip().split("\t") for ln in out_map.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert map_rows[0][0] == "readA|COI|sup|barcode=s1|adapter=s1"
    assert map_rows[1][0] == "readB|COI|hac|barcode=s1|adapter=s1"
    assert all(len(r[0]) > 1 for r in map_rows)

    count_rows = [ln.strip().split("\t") for ln in out_counts.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(count_rows) == 1
    assert count_rows[0][1] == "2"


def test_counts_by_hash_matches_full_member_ids_against_base_hash_map(tmp_path: Path) -> None:
    members = tmp_path / "active_members.tsv"
    members.write_text(
        "CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n"
        "CLUST_0\treadB|COI|hac|barcode=s1|adapter=s1\t0\n",
        encoding="utf-8",
    )
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readB|COI|hac|barcode=s1|adapter=s1\th2\n",
        encoding="utf-8",
    )
    hash_counts = tmp_path / "hash_counts.tsv"
    hash_counts.write_text("h1\t3\nh2\t5\n", encoding="utf-8")
    out = tmp_path / "active_counts_instances.tsv"

    run_perl(COUNTS_BY_HASH_SCRIPT, members, hash_map, hash_counts, out)
    rows = [ln.strip().split("\t") for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows == [["CLUST_0", "readA|COI|sup|barcode=s1|adapter=s1", "8"]]


def test_frozen_members_from_clstr_canonicalizes_frozen_cluster_id(tmp_path: Path) -> None:
    clstr = tmp_path / "new_vs_frozen.clstr"
    clstr.write_text(
        ">Cluster 0\n"
        "0 100nt, >readQ|COI|sup|barcode=s1|adapter=s1... at +/99%\n"
        "1 100nt, >FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|readR|COI|sup|barcode=s1|adapter=s1... *\n"
        ">Cluster 1\n"
        "0 90nt, >plain_read|COI|sup... *\n",
        encoding="utf-8",
    )
    out_members = tmp_path / "frozen_members_new.tsv"
    out_unassigned = tmp_path / "new_unassigned.list"

    run_perl(FROZEN_FROM_CLSTR_SCRIPT, clstr, out_members, out_unassigned)

    members_rows = [ln.strip().split("\t") for ln in out_members.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert members_rows == [
        ["FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "readQ|COI|sup|barcode=s1|adapter=s1", "0"]
    ]
    assert all(not row[1].startswith("FROZEN_") for row in members_rows)
    assert "plain_read|COI|sup" in out_unassigned.read_text(encoding="utf-8")


def test_frozen_members_from_clstr_preserves_dotted_query_ids(tmp_path: Path) -> None:
    clstr = tmp_path / "new_vs_frozen.clstr"
    clstr.write_text(
        ">Cluster 0\n"
        "0 100nt, >FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|read.rep|COI|sup... *\n"
        "1 100nt, >read.1|COI|sup|barcode=s1|adapter=s1... at +/99%\n"
        ">Cluster 1\n"
        "0 90nt, >read.2|COI|sup... *\n",
        encoding="utf-8",
    )
    out_members = tmp_path / "frozen_members_new.tsv"
    out_unassigned = tmp_path / "new_unassigned.list"

    run_perl(FROZEN_FROM_CLSTR_SCRIPT, clstr, out_members, out_unassigned)

    members_rows = [ln.strip().split("\t") for ln in out_members.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert members_rows == [
        ["FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "read.1|COI|sup|barcode=s1|adapter=s1", "0"]
    ]
    assert "read.2|COI|sup" in out_unassigned.read_text(encoding="utf-8")


def test_parse_clstr_preserves_dotted_ids(tmp_path: Path) -> None:
    clstr = tmp_path / "reads.clstr"
    clstr.write_text(
        ">Cluster 0\n"
        "0 120nt, >read.1|COI|sup|barcode=s1|adapter=s1... *\n"
        "1 120nt, >read.2|COI|sup|barcode=s1|adapter=s1... at +/99%\n",
        encoding="utf-8",
    )
    members = tmp_path / "members.tsv"
    counts = tmp_path / "counts.tsv"

    run_perl(PARSE_CLSTR_SCRIPT, clstr, members, counts)

    member_rows = members.read_text(encoding="utf-8").splitlines()
    assert any("read.1|COI|sup|barcode=s1|adapter=s1" in row for row in member_rows)
    assert any("read.2|COI|sup|barcode=s1|adapter=s1" in row for row in member_rows)
    counts_rows = counts.read_text(encoding="utf-8").splitlines()
    assert any("read.1|COI|sup|barcode=s1|adapter=s1" in row for row in counts_rows)


def test_counts_by_hash_handles_dotted_ids_from_parse_clstr(tmp_path: Path) -> None:
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(
        ">read.1|COI|sup|barcode=s1|adapter=s1\nACGTACGT\n"
        ">read.2|COI|sup|barcode=s1|adapter=s1\nACGTACGA\n",
        encoding="utf-8",
    )
    hash_map = tmp_path / "hash_map.tsv"
    hash_counts = tmp_path / "hash_counts.tsv"
    run_perl(HASH_MAP_SCRIPT, fasta, hash_map, hash_counts)

    clstr = tmp_path / "reads.clstr"
    clstr.write_text(
        ">Cluster 0\n"
        "0 120nt, >read.1|COI|sup|barcode=s1|adapter=s1... *\n"
        "1 120nt, >read.2|COI|sup|barcode=s1|adapter=s1... at +/99%\n",
        encoding="utf-8",
    )
    members = tmp_path / "members.tsv"
    counts = tmp_path / "counts.tsv"
    run_perl(PARSE_CLSTR_SCRIPT, clstr, members, counts)

    out = tmp_path / "active_counts_instances.tsv"
    run_perl(COUNTS_BY_HASH_SCRIPT, members, hash_map, hash_counts, out, "strict")
    rows = [ln.strip().split("\t") for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows == [["CLUST_0", "read.1|COI|sup|barcode=s1|adapter=s1", "2"]]


def test_counts_by_hash_strict_rejects_mixed_member_id_styles(tmp_path: Path) -> None:
    members = tmp_path / "active_members.tsv"
    members.write_text(
        "CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n"
        "CLUST_0\treadA\t0\n",
        encoding="utf-8",
    )
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text("readA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_counts = tmp_path / "hash_counts.tsv"
    hash_counts.write_text("h1\t3\n", encoding="utf-8")
    out = tmp_path / "active_counts_instances.tsv"

    with pytest.raises(subprocess.CalledProcessError):
        run_perl(COUNTS_BY_HASH_SCRIPT, members, hash_map, hash_counts, out, "strict")


def test_counts_by_hash_legacy_fails_ambiguous_base_id(tmp_path: Path) -> None:
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA\t1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readA|COI|hac|barcode=s1|adapter=s1\th2\n",
        encoding="utf-8",
    )
    hash_counts = tmp_path / "hash_counts.tsv"
    hash_counts.write_text("h1\t3\nh2\t5\n", encoding="utf-8")
    out = tmp_path / "active_counts_instances.tsv"

    with pytest.raises(subprocess.CalledProcessError):
        run_perl(COUNTS_BY_HASH_SCRIPT, members, hash_map, hash_counts, out, "legacy")


def test_counts_by_hash_legacy_unique_base_id_uses_fallback_and_stats(tmp_path: Path) -> None:
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA\t1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text("readA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_counts = tmp_path / "hash_counts.tsv"
    hash_counts.write_text("h1\t3\n", encoding="utf-8")
    out = tmp_path / "active_counts_instances.tsv"
    stats = tmp_path / "counts_stats.tsv"

    run_perl(COUNTS_BY_HASH_SCRIPT, members, hash_map, hash_counts, out, "legacy", stats)

    rows = [ln.strip().split("\t") for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows == [["CLUST_0", "readA", "3"]]
    s = dict(
        ln.strip().split("\t", 1)
        for ln in stats.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    )
    assert s["mode"] == "legacy"
    assert s["fallback_hits"] == "1"


def test_counts_by_hash_strict_reports_unresolved_samples_and_writes_stats(tmp_path: Path) -> None:
    members = tmp_path / "active_members.tsv"
    members.write_text(
        "CLUST_0\treadMissing1|COI|sup|barcode=s1|adapter=s1\t1\n"
        "CLUST_0\treadMissing2|COI|sup|barcode=s1|adapter=s1\t0\n",
        encoding="utf-8",
    )
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text("readA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_counts = tmp_path / "hash_counts.tsv"
    hash_counts.write_text("h1\t3\n", encoding="utf-8")
    out = tmp_path / "active_counts_instances.tsv"
    stats = tmp_path / "counts_stats.tsv"

    cp = subprocess.run(
        ["perl", str(COUNTS_BY_HASH_SCRIPT), str(members), str(hash_map), str(hash_counts), str(out), "strict", str(stats)],
        capture_output=True,
        text=True,
    )
    assert cp.returncode != 0
    assert "unresolved member IDs" in cp.stderr
    assert "samples:" in cp.stderr
    assert "readMissing1|COI|sup|barcode=s1|adapter=s1" in cp.stderr
    assert stats.exists()
    s = dict(
        ln.strip().split("\t", 1)
        for ln in stats.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    )
    assert s["mode"] == "strict"
    assert s["misses"] == "2"
    assert not out.exists()


def test_snapshot_legacy_emits_no_pipe_ids_when_hash_map_has_only_no_pipe(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA\t1\n", encoding="utf-8")
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text("readA\th1\nreadB\th1\n", encoding="utf-8")
    out = tmp_path / "snapshot.tsv"

    run_perl(SNAPSHOT_SCRIPT, promoted, members, meta, hash_map, out, "legacy", "pipe_only", "false")
    rows = [ln.strip().split("\t") for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 2
    assert all(r[0] == "FROZEN_h1" for r in rows)
    assert {r[1] for r in rows} == {"readA", "readB"}


def test_snapshot_legacy_prefers_pipe_ids_when_hash_map_has_mixed_styles(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n", encoding="utf-8")
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readA\th1\n",
        encoding="utf-8",
    )
    out = tmp_path / "snapshot.tsv"

    run_perl(SNAPSHOT_SCRIPT, promoted, members, meta, hash_map, out, "legacy", "pipe_only", "false")
    rows = [ln.strip().split("\t") for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0][1] == "readA|COI|sup|barcode=s1|adapter=s1"


def test_snapshot_strict_rejects_mixed_hash_map_styles(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n", encoding="utf-8")
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readA\th1\n",
        encoding="utf-8",
    )
    out = tmp_path / "snapshot.tsv"

    with pytest.raises(subprocess.CalledProcessError):
        run_perl(SNAPSHOT_SCRIPT, promoted, members, meta, hash_map, out, "strict", "pipe_only", "false")


def test_snapshot_strict_unresolved_is_atomic_and_keeps_previous_output(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text(
        "CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n"
        "CLUST_0\treadMissing|COI|sup|barcode=s1|adapter=s1\t0\n",
        encoding="utf-8",
    )
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readB|COI|sup|barcode=s1|adapter=s1\th1\n",
        encoding="utf-8",
    )
    out = tmp_path / "snapshot.tsv"
    out.write_text("OLD\n", encoding="utf-8")

    cp = subprocess.run(
        [
            "perl",
            str(SNAPSHOT_SCRIPT),
            str(promoted),
            str(members),
            str(meta),
            str(hash_map),
            str(out),
            "strict",
            "pipe_only",
            "false",
        ],
        capture_output=True,
        text=True,
    )
    assert cp.returncode != 0
    assert "unresolved promoted members" in cp.stderr
    assert "samples:" in cp.stderr
    assert "readMissing|COI|sup|barcode=s1|adapter=s1" in cp.stderr
    assert out.read_text(encoding="utf-8") == "OLD\n"


def test_snapshot_legacy_mixed_policy_error_fails(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n", encoding="utf-8")
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readA\th1\n",
        encoding="utf-8",
    )
    out = tmp_path / "snapshot.tsv"
    with pytest.raises(subprocess.CalledProcessError):
        run_perl(SNAPSHOT_SCRIPT, promoted, members, meta, hash_map, out, "legacy", "error", "false")


def test_snapshot_legacy_mixed_policy_emit_all_requires_unsafe(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n", encoding="utf-8")
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readA\th1\n",
        encoding="utf-8",
    )
    out = tmp_path / "snapshot.tsv"
    with pytest.raises(subprocess.CalledProcessError):
        run_perl(SNAPSHOT_SCRIPT, promoted, members, meta, hash_map, out, "legacy", "emit_all", "false")


def test_snapshot_legacy_mixed_policy_emit_all_allows_both(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n", encoding="utf-8")
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readA\th1\n",
        encoding="utf-8",
    )
    out = tmp_path / "snapshot.tsv"
    run_perl(SNAPSHOT_SCRIPT, promoted, members, meta, hash_map, out, "legacy", "emit_all", "true")
    rows = [ln.strip().split("\t") for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert {r[1] for r in rows} == {"readA|COI|sup|barcode=s1|adapter=s1", "readA"}


def test_snapshot_legacy_emit_all_pipe_only_hash_map_emits_pipe_ids(tmp_path: Path) -> None:
    promoted = tmp_path / "promoted.tsv"
    promoted.write_text("CLUST_0\n", encoding="utf-8")
    members = tmp_path / "active_members.tsv"
    members.write_text("CLUST_0\treadA|COI|sup|barcode=s1|adapter=s1\t1\n", encoding="utf-8")
    meta = tmp_path / "meta.tsv"
    meta.write_text("FROZEN_h1\treadA|COI|sup|barcode=s1|adapter=s1\th1\n", encoding="utf-8")
    hash_map = tmp_path / "hash_map.tsv"
    hash_map.write_text(
        "readA|COI|sup|barcode=s1|adapter=s1\th1\n"
        "readB|COI|sup|barcode=s1|adapter=s1\th1\n",
        encoding="utf-8",
    )
    out = tmp_path / "snapshot.tsv"

    run_perl(SNAPSHOT_SCRIPT, promoted, members, meta, hash_map, out, "legacy", "emit_all", "false")
    rows = [ln.strip().split("\t") for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 2
    assert {r[1] for r in rows} == {
        "readA|COI|sup|barcode=s1|adapter=s1",
        "readB|COI|sup|barcode=s1|adapter=s1",
    }


def test_consensus_cached_only_filters_prior_consolidated_to_emitted_headers(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    write_executable(
        tools / "seqkit",
        "#!/bin/sh\nexit 0\n",
    )
    write_executable(
        tools / "Rscript",
        "#!/bin/sh\nexit 0\n",
    )
    write_executable(
        tools / "vsearch",
        "#!/bin/sh\n"
        "set -eu\n"
        "in=''\n"
        "out=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --cluster_fast) in=\"$2\"; shift 2 ;;\n"
        "    --clusters) out=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "cp \"$in\" \"${out}1\"\n",
    )

    (tmp_path / "samples.txt").write_text("s1\n", encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n",
        encoding="utf-8",
    )
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(">dummy\nACGT\n", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")

    cache_dir = tmp_path / "Consensus" / ".cache" / "s1"
    cache_dir.mkdir(parents=True)
    (cache_dir / "OTUB_1-COI.consensus.fasta").write_text(
        ">s1|Consensus7|COI|reads-10\nACGTACGT\n",
        encoding="utf-8",
    )
    (tmp_path / "Consensus" / "consolidated_consensus_ids.txt").write_text(
        "Consensus1_s1\nConsensus999_s1\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{tools}:{env['PATH']}"
    subprocess.run(
        [
            "bash",
            str(CONSENSUS_SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",
            "5",
            "50",
            "15",
            "20",
            "",
            "representative",
            "4",
        ],
        cwd=tmp_path,
        check=True,
        env=env,
    )

    consolidated = (tmp_path / "Consensus" / "consolidated_consensus_ids.txt").read_text(encoding="utf-8").splitlines()
    assert "Consensus1_s1" in consolidated
    assert "Consensus999_s1" not in consolidated


def test_pool_merge_prefers_higher_rank_and_new_source(tmp_path: Path) -> None:
    old_pool = tmp_path / "old.fasta"
    new_pool = tmp_path / "new.fasta"
    out_pool = tmp_path / "out.fasta"
    stats = tmp_path / "stats.tsv"
    old_pool.write_text(
        ">readA|COI|hac|barcode=s1|adapter=old\nAAAA\n"
        ">readB|COI|sup|barcode=s1|adapter=old\nCCCC\n",
        encoding="utf-8",
    )
    new_pool.write_text(
        ">readA|COI|sup|barcode=s1|adapter=new\nTTTT\n"
        ">readB|COI|sup|barcode=s1|adapter=new\nGGGG\n",
        encoding="utf-8",
    )

    run_perl(POOL_MERGE_SCRIPT, old_pool, new_pool, out_pool, stats)
    records = read_fasta_records(out_pool)
    rec_map = {h: s for h, s in records}
    assert rec_map["readA|COI|sup|barcode=s1|adapter=new"] == "TTTT"
    assert rec_map["readB|COI|sup|barcode=s1|adapter=new"] == "GGGG"
    stats_kv = dict(
        ln.strip().split("\t", 1)
        for ln in stats.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    )
    assert stats_kv["replaced_by_rank"] == "1"
    assert stats_kv["replaced_by_new"] == "1"


def test_pool_merge_writes_decisions_with_optional_hash(tmp_path: Path) -> None:
    old_pool = tmp_path / "old.fasta"
    new_pool = tmp_path / "new.fasta"
    out_pool = tmp_path / "out.fasta"
    stats = tmp_path / "stats.tsv"
    decisions = tmp_path / "decisions.tsv"
    old_pool.write_text(">readA|COI|hac\nAAAA\n", encoding="utf-8")
    new_pool.write_text(">readA|COI|sup\nTTTT\n", encoding="utf-8")

    run_perl(POOL_MERGE_SCRIPT, old_pool, new_pool, out_pool, stats, decisions, "true")
    rows = [ln.strip().split("\t") for ln in decisions.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    # base_id, selected_header, selected_source, selected_rank, selected_hash, best_new_header, best_new_rank, best_new_hash, decision
    assert len(rows[0]) == 9
    assert rows[0][0] == "readA"
    assert rows[0][2] == "new"
    assert rows[0][8] == "kept_new"
    assert rows[0][4] != ""
    assert rows[0][7] != ""


def test_pool_merge_decisions_show_no_pool_change_when_new_records_are_dropped(tmp_path: Path) -> None:
    old_pool = tmp_path / "old.fasta"
    new_pool = tmp_path / "new.fasta"
    out_pool = tmp_path / "out.fasta"
    stats = tmp_path / "stats.tsv"
    decisions = tmp_path / "decisions.tsv"
    old_pool.write_text(">readA|COI|sup|barcode=s1|adapter=old\nAAAA\n", encoding="utf-8")
    new_pool.write_text(">readA|COI|hac|barcode=s1|adapter=new\nTTTT\n", encoding="utf-8")

    run_perl(POOL_MERGE_SCRIPT, old_pool, new_pool, out_pool, stats, decisions, "false")
    rows = [ln.strip().split("\t") for ln in decisions.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1
    assert rows[0][8] == "dropped_new"
    kept_new_rows = sum(1 for row in rows if len(row) >= 9 and row[8] == "kept_new")
    assert kept_new_rows == 0


def test_pool_merge_normalizes_hac_aliases_and_lex_tiebreak(tmp_path: Path) -> None:
    old_pool = tmp_path / "old.fasta"
    new_pool = tmp_path / "new.fasta"
    out_pool = tmp_path / "out.fasta"
    old_pool.write_text(
        ">readX|COI|fast|barcode=s1|adapter=a\nAAAA\n"
        ">readY|COI|foo|z\nCCCC\n"
        ">readY|COI|foo|a\nGGGG\n",
        encoding="utf-8",
    )
    new_pool.write_text(
        ">readX|COI|hac_fixed|barcode=s1|adapter=b\nTTTT\n",
        encoding="utf-8",
    )
    run_perl(POOL_MERGE_SCRIPT, old_pool, new_pool, out_pool)
    rec_map = {h: s for h, s in read_fasta_records(out_pool)}
    assert rec_map["readX|COI|hac_fixed|barcode=s1|adapter=b"] == "TTTT"
    assert rec_map["readY|COI|foo|a"] == "GGGG"
    assert "readY|COI|foo|z" not in rec_map


def test_members_append_unique_deduplicates_by_pair(tmp_path: Path) -> None:
    members = tmp_path / "otu_frozen_members.tsv"
    chunk = tmp_path / "append.tsv"
    seen = tmp_path / "otu_frozen_members_seen.tsv"
    members.write_text("FROZEN_h1\treadA|COI|sup\t0\n", encoding="utf-8")
    chunk.write_text(
        "FROZEN_h1\treadA|COI|sup\t1\n"
        "FROZEN_h1\treadB|COI|sup\t0\n"
        "FROZEN_h1\treadB|COI|sup\t1\n",
        encoding="utf-8",
    )

    run_perl(MEMBERS_APPEND_UNIQUE_SCRIPT, members, chunk, seen)
    rows = [ln.strip().split("\t") for ln in members.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows == [
        ["FROZEN_h1", "readA|COI|sup", "0"],
        ["FROZEN_h1", "readB|COI|sup", "0"],
    ]
    seen_rows = [ln.strip() for ln in seen.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert set(seen_rows) == {"FROZEN_h1\treadA|COI|sup", "FROZEN_h1\treadB|COI|sup"}


def test_members_append_unique_rebuilds_seen_index_if_missing(tmp_path: Path) -> None:
    members = tmp_path / "otu_frozen_members.tsv"
    chunk = tmp_path / "append.tsv"
    seen = tmp_path / "otu_frozen_members_seen.tsv"
    members.write_text("FROZEN_h1\treadA|COI|sup\t0\n", encoding="utf-8")
    chunk.write_text(
        "FROZEN_h1\treadA|COI|sup\t1\n"
        "FROZEN_h1\treadB|COI|sup\t0\n",
        encoding="utf-8",
    )

    run_perl(MEMBERS_APPEND_UNIQUE_SCRIPT, members, chunk, seen)
    rows = [ln.strip().split("\t") for ln in members.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows == [
        ["FROZEN_h1", "readA|COI|sup", "0"],
        ["FROZEN_h1", "readB|COI|sup", "0"],
    ]
    assert seen.exists()
    seen_rows = [ln.strip() for ln in seen.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert set(seen_rows) == {"FROZEN_h1\treadA|COI|sup", "FROZEN_h1\treadB|COI|sup"}


def test_members_append_unique_rebuilds_corrupt_seen_index(tmp_path: Path) -> None:
    members = tmp_path / "otu_frozen_members.tsv"
    chunk = tmp_path / "append.tsv"
    seen = tmp_path / "otu_frozen_members_seen.tsv"
    members.write_text("FROZEN_h1\treadA|COI|sup\t0\n", encoding="utf-8")
    chunk.write_text("FROZEN_h1\treadA|COI|sup\t1\n", encoding="utf-8")
    seen.write_text("corrupt_line_without_tab\n", encoding="utf-8")

    run_perl(MEMBERS_APPEND_UNIQUE_SCRIPT, members, chunk, seen)
    rows = [ln.strip().split("\t") for ln in members.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert rows == [["FROZEN_h1", "readA|COI|sup", "0"]]
    assert seen.read_text(encoding="utf-8").strip() == "FROZEN_h1\treadA|COI|sup"


def test_until_consolidated_prune_is_sample_scoped_with_adapter_suffix_normalization(tmp_path: Path) -> None:
    fasta = tmp_path / "qced_reads_hq_accumulated.fasta"
    fasta.write_text(
        ">readA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\nACGT\n"
        ">readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI\nTGCA\n"
        ">readC|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_2-COI\nGGGG\n",
        encoding="utf-8",
    )
    consolidated = tmp_path / "otu_consolidated_keys.tsv"
    # Sample key has no "_1" suffix; prune code must normalize adapter=no_adapter_1 -> sample=no_adapter.
    consolidated.write_text("no_adapter\tOTUB_1-COI\n", encoding="utf-8")
    samples = tmp_path / "samples.txt"
    samples.write_text("no_adapter\nsampleB\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, consolidated, samples_file=samples)
    assert "readA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI" not in active_ids
    assert "readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI" in active_ids
    assert "readC|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_2-COI" in active_ids


def test_until_consolidated_prune_single_column_keys_are_global(tmp_path: Path) -> None:
    fasta = tmp_path / "qced_reads_hq_accumulated.fasta"
    fasta.write_text(
        ">readA|COI|hac|barcode=no_adapter_1|adapter=no_adapter_1|OTUB_1-COI\nACGT\n"
        ">readB|COI|hac|barcode=sampleB_1|adapter=sampleB_1|OTUB_1-COI\nTGCA\n",
        encoding="utf-8",
    )
    consolidated = tmp_path / "otu_consolidated_keys.tsv"
    consolidated.write_text("OTUB_1-COI\n", encoding="utf-8")

    active_ids = run_until_consolidated_active_ids(fasta, consolidated)
    assert active_ids == []


def test_consensus_zero_emission_keeps_previous_consolidated_ids(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    write_executable(tools / "seqkit", "#!/bin/sh\nexit 0\n")
    write_executable(tools / "Rscript", "#!/bin/sh\nexit 0\n")
    write_executable(tools / "vsearch", "#!/bin/sh\nexit 0\n")

    (tmp_path / "samples.txt").write_text("s1\n", encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n",
        encoding="utf-8",
    )
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(">dummy\nACGT\n", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")
    (tmp_path / "Consensus").mkdir()
    (tmp_path / "Consensus" / "consolidated_consensus_ids.txt").write_text(
        "Consensus1_s1\nConsensus2_s1\n",
        encoding="utf-8",
    )

    env = dict(os.environ)
    env["PATH"] = f"{tools}:{env['PATH']}"
    subprocess.run(
        [
            "bash",
            str(CONSENSUS_SCRIPT),
            str(REPO_ROOT / "bin"),
            "98",
            "5",
            "50",
            "15",
            "20",
            "",
            "representative",
            "4",
        ],
        cwd=tmp_path,
        check=True,
        env=env,
    )
    consolidated = (tmp_path / "Consensus" / "consolidated_consensus_ids.txt").read_text(encoding="utf-8").splitlines()
    assert set(consolidated) == {"Consensus1_s1", "Consensus2_s1"}


def test_consensus_zero_emission_with_inputs_fails_when_policy_fail(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    write_executable(tools / "seqkit", "#!/bin/sh\nexit 0\n")
    write_executable(tools / "Rscript", "#!/bin/sh\nexit 0\n")
    write_executable(
        tools / "vsearch",
        "#!/bin/sh\n"
        "set -eu\n"
        "out=''\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    --clusters) out=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        ": > \"${out}1\"\n",
    )

    (tmp_path / "samples.txt").write_text("s1\n", encoding="utf-8")
    (tmp_path / "blast_report_annotated.txt").write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n",
        encoding="utf-8",
    )
    (tmp_path / "qced_reads_hq_accumulated.fasta").write_text(">dummy\nACGT\n", encoding="utf-8")
    (tmp_path / "read_qscore.tsv").write_text("", encoding="utf-8")
    cache_dir = tmp_path / "Consensus" / ".cache" / "s1"
    cache_dir.mkdir(parents=True)
    (cache_dir / "OTUB_1-COI.consensus.fasta").write_text(">s1|Consensus7|COI|reads-10\nACGTACGT\n", encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{tools}:{env['PATH']}"
    env["CONSENSUS_ZERO_EMIT_POLICY"] = "fail"
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            [
                "bash",
                str(CONSENSUS_SCRIPT),
                str(REPO_ROOT / "bin"),
                "98",
                "5",
                "50",
                "15",
                "20",
                "",
                "representative",
                "4",
            ],
            cwd=tmp_path,
            check=True,
            env=env,
        )
    status = dict(
        ln.strip().split("\t", 1)
        for ln in (tmp_path / "Consensus" / "consolidated_ids_status.tsv").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    )
    assert status["merged_input_headers_total"] == "1"
    assert status["emitted_consensus_count"] == "0"


def test_frozen_members_db_only_cluster_strict_auto_fails(tmp_path: Path) -> None:
    clstr = tmp_path / "new_vs_frozen.clstr"
    clstr.write_text(
        ">Cluster 0\n"
        "0 100nt, >FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|read.rep|COI|sup... *\n",
        encoding="utf-8",
    )
    out_members = tmp_path / "frozen_members_new.tsv"
    out_unassigned = tmp_path / "new_unassigned.list"
    with pytest.raises(subprocess.CalledProcessError):
        run_perl(FROZEN_FROM_CLSTR_SCRIPT, clstr, out_members, out_unassigned, "strict", "auto")


def test_frozen_members_db_only_cluster_legacy_warn_skip(tmp_path: Path) -> None:
    clstr = tmp_path / "new_vs_frozen.clstr"
    clstr.write_text(
        ">Cluster 0\n"
        "0 100nt, >FROZEN_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa|read.rep|COI|sup... *\n",
        encoding="utf-8",
    )
    out_members = tmp_path / "frozen_members_new.tsv"
    out_unassigned = tmp_path / "new_unassigned.list"
    run_perl(FROZEN_FROM_CLSTR_SCRIPT, clstr, out_members, out_unassigned, "legacy", "warn_skip")
    assert out_members.read_text(encoding="utf-8") == ""
    assert out_unassigned.read_text(encoding="utf-8") == ""


def test_pool_merge_no_kept_new_rows_signals_pool_unchanged(tmp_path: Path) -> None:
    # Old pool holds a sup-rank read; new unassigned brings only a hac-rank read
    # for the same base ID → merge drops the new read → kept_new_rows == 0 → POOL_CHANGED=0.
    # This mirrors the exact pipeline check: count rows where decisions[col8]=="kept_new".
    old_pool = tmp_path / "active_pool.fasta"
    new_unassigned = tmp_path / "new_unassigned.fasta"
    out_pool = tmp_path / "merged_pool.fasta"
    stats = tmp_path / "pool_stats.tsv"
    decisions = tmp_path / "pool_decisions.tsv"

    old_pool.write_text(">readA|COI|sup|barcode=s1|adapter=s1\nAAAA\n", encoding="utf-8")
    new_unassigned.write_text(">readA|COI|hac|barcode=s1|adapter=s1\nTTTT\n", encoding="utf-8")

    run_perl(POOL_MERGE_SCRIPT, old_pool, new_unassigned, out_pool, stats, decisions, "false")

    rows = [
        ln.strip().split("\t")
        for ln in decisions.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(rows) >= 1
    kept_new_rows = sum(1 for row in rows if len(row) >= 9 and row[8] == "kept_new")
    # Pipeline logic: POOL_CHANGED=0 when kept_new_rows==0
    assert kept_new_rows == 0, (
        f"Expected zero kept_new decisions (pool unchanged), got {kept_new_rows}"
    )
