"""R3-C4 membership and denominator contract (manufactured truth, version 1).

Manufactured sequences and their independently computed uppercase MD5 hashes are
truth. Metadata, not lexical ordering of raw duplicate IDs, designates the frozen
representative. Contract failures are deliberately ordinary assertions: they must
not be hidden with skips, xfails, or a different denominator.
"""
from collections import Counter
import hashlib
import importlib.util
import json
import random
import shlex
import statistics
import sys
import time
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERRIDES = {}  # Disposable semantic mutants only; never edit repository scripts.
BASE = "72374217b03a761e84727a115853b3308873e79a"
REP = "z_authority|COI|sup|barcode=bc|adapter=bc"
SEQUENCES = {
    "z_authority": "ACGTACGTACGTACGT",
    "member_one": "ACGTACGTACGTACGA",
    "member_two": "ACGTACGTACGTACGC",
    "unrelated": "TTTTCCCCAAAAGGGG",
}


def sequence_hash(sequence):
    return hashlib.md5(sequence.upper().encode("ascii")).hexdigest()


def execute(script, *args, cwd=None):
    result = subprocess.run(
        ["perl", str(OVERRIDES.get(script, ROOT / "bin" / script)), *map(str, args)], cwd=cwd,
        env=dict(os.environ, RTBIOSCAN_DEMUX_IDENTITY_CONTEXT="off"),
        text=True, capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return result


def fasta(path, records):
    path.write_text("".join(f">{rid}\n{seq}\n" for rid, seq in records))


def rows(path):
    return [line.split("\t") for line in path.read_text().splitlines() if line]


def frozen_fixture(tmp_path, depth=5, reverse=False, representative_in_map=True):
    h = sequence_hash(SEQUENCES["z_authority"])
    fid = "FROZEN_" + h
    meta = tmp_path / "meta.tsv"
    meta.write_text(f"{fid}\t{REP}\t{h}\tbc_1\n")
    records = [(f"a_duplicate_{n}|COI", SEQUENCES["z_authority"].lower()) for n in range(depth)]
    records += [("z_authority|COI|hac", SEQUENCES["z_authority"])]
    if representative_in_map:
        records += [(REP, SEQUENCES["z_authority"])]
    records += [("unrelated|COI", SEQUENCES["unrelated"])]
    if reverse:
        records.reverse()
    raw = tmp_path / "raw.fasta"
    fasta(raw, records)
    hm, hc = tmp_path / "hashes.tsv", tmp_path / "hash_counts.tsv"
    execute("otu_hash_map_from_fasta.pl", raw, hm, hc)
    assert {rid: h for rid, h in rows(hm)} == {rid: sequence_hash(seq) for rid, seq in records}
    return fid, meta, hm, tmp_path / "append.tsv"


def assert_canonical(table, known_sequences, representative):
    """Independent sequence oracle; no helper selection algorithm is reused."""
    seen = set()
    reps = Counter()
    sizes = Counter()
    for cid, rid, flag in rows(table):
        base = rid.split("|", 1)[0]
        assert base in known_sequences, "canonical member must have a known sequence"
        pair = (cid, sequence_hash(known_sequences[base]))
        assert pair not in seen, "one canonical row per distinct sequence hash"
        seen.add(pair)
        sizes[cid] += 1
        if flag == "1":
            assert rid == representative, "authoritative representative identity"
            reps[cid] += 1
    assert all(reps[cid] == 1 for cid in sizes), "representative exactly once per OTU"
    assert sum(sizes.values()) == len(seen) == len(rows(table))
    return seen, sizes


@pytest.mark.parametrize("depth", [0, 1, 31])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("representative_in_map", [False, True])
def test_frozen_exact_duplicate_depth_and_authority(tmp_path, depth, reverse, representative_in_map):
    fid, meta, hm, out = frozen_fixture(tmp_path, depth, reverse, representative_in_map)
    execute("otu_add_frozen_members_by_hash.pl", meta, hm, out)
    assert rows(out) == [[fid, REP, "1"]], "only the metadata-designated representative is emitted"
    assert_canonical(out, SEQUENCES, REP)
    members, index = tmp_path / "members.tsv", tmp_path / "seen.tsv"
    members.write_text(f"{fid}\t{REP}\t1\n")
    members.write_text(members.read_text() + "".join(
        f"{fid}\t{name}|COI\t0\n" for name in ("member_one", "member_two")))
    before = members.read_bytes()
    execute("otu_members_append_unique.pl", members, out, index)
    execute("otu_members_append_unique.pl", members, out, index)
    assert members.read_bytes() == before, "later exact duplicates do not grow frozen state"
    relation, counts = assert_canonical(members, SEQUENCES, REP)
    assert counts == {fid: 3}
    assert relation == {(fid, sequence_hash(SEQUENCES[name])) for name in ("z_authority", "member_one", "member_two")}
    # Missing seen-index bootstrap must reconstruct existing representative identity.
    index.unlink()
    execute("otu_members_append_unique.pl", members, out, index)
    assert members.read_bytes() == before
    empty_members = tmp_path / "recovered.tsv"
    execute("otu_members_append_unique.pl", empty_members, out, tmp_path / "recovered_seen.tsv")
    assert rows(empty_members) == [[fid, REP, "1"]]


@pytest.mark.parametrize("case", ["missing", "empty", "duplicate", "truncated", "empty_rep", "bad_hash", "conflict", "base_conflict", "extra_field", "bad_rep", "empty_round"])
def test_frozen_metadata_never_guesses(tmp_path, case):
    fid, meta, hm, out = frozen_fixture(tmp_path)
    good = meta.read_text()
    if case == "missing":
        meta.unlink()
    elif case == "empty":
        meta.write_text("")
    elif case == "duplicate":
        meta.write_text(good * 2)
    elif case == "truncated":
        meta.write_text(fid + "\t" + REP + "\n")
    elif case == "empty_rep":
        meta.write_text(good.replace(REP, ""))
    elif case == "bad_hash":
        meta.write_text(good.replace(fid[7:], "invalid"))
    elif case == "conflict":
        meta.write_text(good + good.replace(REP, "other|COI"))
    elif case == "extra_field":
        meta.write_text(good.rstrip() + "\textra\n")
    elif case == "bad_rep":
        meta.write_text(good.replace(REP, "|COI"))
    elif case == "empty_round":
        meta.write_text(good.replace("bc_1", ""))
    else:
        other = sequence_hash(SEQUENCES["member_one"])
        meta.write_text(good + good.replace(fid[7:], other).replace(REP, "z_authority|ITS2"))
    out.write_text("prior-output\n")
    result = subprocess.run(["perl", str(ROOT / "bin/otu_add_frozen_members_by_hash.pl"),
                             str(meta), str(hm), str(out)], text=True, capture_output=True)
    if case in ("missing", "empty"):
        assert result.returncode == 0 and out.read_bytes() == b"", "existing absent-metadata behavior emits no relation"
    elif case == "duplicate":
        assert result.returncode == 0 and rows(out) == [[fid, REP, "1"]]
    else:
        assert result.returncode != 0
        assert "Malformed" in result.stderr or "Conflicting" in result.stderr
        assert out.read_text() == "prior-output\n", "metadata failure must precede output replacement"


def test_no_duplicate_frozen_hash_byte_equivalence(tmp_path):
    h = sequence_hash(SEQUENCES["z_authority"])
    meta, hm = tmp_path / "meta", tmp_path / "hash_map"
    meta.write_text(f"FROZEN_{h}\t{REP}\t{h}\n")
    hm.write_text(f"unrelated|COI\t{sequence_hash(SEQUENCES['unrelated'])}\n{REP}\t{h}\n")
    historical = subprocess.check_output(["git", "-C", str(ROOT), "show", BASE + ":bin/otu_add_frozen_members_by_hash.pl"])
    baseline = tmp_path / "baseline.pl"
    baseline.write_bytes(historical)
    old, new = tmp_path / "old.tsv", tmp_path / "new.tsv"
    subprocess.run(["perl", str(baseline), str(meta), str(hm), str(old)], check=True)
    execute("otu_add_frozen_members_by_hash.pl", meta, hm, new)
    assert new.read_bytes() == old.read_bytes()


def marker_report(tmp_path):
    cluster = tmp_path / "mixed.clstr"
    cluster.write_text(
        ">Cluster 0\n0\t16nt, >z_authority|COI... *\n"
        "1\t16nt, >member_one|ITS2... at +/99.0%\n")
    members, counts = tmp_path / "presplit.tsv", tmp_path / "counts.tsv"
    execute("otu_parse_clstr.pl", cluster, members, counts)
    assert rows(members) == [["CLUST_0", "z_authority|COI", "1"],
                             ["CLUST_0", "member_one|ITS2", "0"]]
    assert sum(int(row[2]) for row in rows(members)) == 1
    demux = tmp_path / "demux.tsv"
    demux.write_text("read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\n")
    execute("reporting_otu_definition.pl", cluster, demux, "bc_1", "bc", "COI", "ITS2", cwd=tmp_path)
    assert rows(tmp_path / "bc_otu_sizes_round.tsv") == [
        ["otu_id", "size"], ["OTUB_0-COI", "1"], ["OTUB_0-ITS2", "1"]]
    table = rows(tmp_path / "bc_otu_def_rpt.txt")
    return [dict(zip(table[0], row)) for row in table[1:]]


def assert_projection(report):
    expected = {("OTUB_0-COI", "z_authority", "REPRESENTATIVE"),
                ("OTUB_0-ITS2", "member_one", "MEMBER")}
    actual = [(r["OTU_id"], r["read_id"], r["OTU_role"]) for r in report]
    assert Counter(actual) == Counter(expected), "projected roles and membership conserve pre-split authority"
    reps = Counter(otu for otu, _, role in actual if role == "REPRESENTATIVE")
    assert (reps["OTUB_0-COI"], reps["OTUB_0-ITS2"]) == (1, 0)
    assert all(n <= 1 for n in reps.values())


def test_marker_projection_conserves_presplit_representative(tmp_path):
    assert_projection(marker_report(tmp_path))


def test_multiple_frozen_hashes_have_deterministic_bytes(tmp_path):
    truth = [(sequence_hash(seq), name + "|COI") for name, seq in SEQUENCES.items()]
    expected = "".join(f"FROZEN_{h}\t{rid}\t1\n" for h, rid in sorted(truth)).encode()
    meta, hm, out = (tmp_path / n for n in ("meta", "hashes", "append"))
    for seed in range(6):
        randomizer = random.Random(seed)
        metadata = [f"FROZEN_{h}\t{rid}\t{h}\n" for h, rid in truth]
        observations = [f"a_dup_{n}_{rid}\t{h}\n" for h, rid in truth for n in range(9)]
        observations += observations[::3]  # Exact repeated raw relations as well as different IDs.
        if seed == 1:
            metadata.reverse(); observations.reverse()
        elif seed > 1:
            randomizer.shuffle(metadata); randomizer.shuffle(observations)
        meta.write_text("".join(metadata)); hm.write_text("".join(observations))
        execute("otu_add_frozen_members_by_hash.pl", meta, hm, out)
        assert out.read_bytes() == expected, "sorted canonical bytes independent of duplicate order"


def promotion_case(tmp_path, threshold=5, min_rounds=2, depth=17):
    records = [(REP, SEQUENCES["z_authority"]),
               ("member_one|COI", SEQUENCES["member_one"]),
               ("member_two|COI", SEQUENCES["member_two"])]
    representative = tmp_path / "representative.fasta"
    fasta(representative, records[:1])
    meta, frozen, history, members, promoted = [tmp_path / name for name in
        ("meta.tsv", "frozen.fasta", "history.tsv", "frozen_members.tsv", "promoted.tsv")]
    observed = []
    for round_number in (1, 2):
        if round_number == 2:
            records.append(("member_three|COI", "ACGTACGTACGTACGG"))
        raw, active, hm, hc = [tmp_path / name for name in
                               ("raw.fasta", "active.fasta", "hash_map.tsv", "hash_counts.tsv")]
        # Real NR preprocessing, not a manufactured count supplied to promotion.
        duplicates = [(f"duplicate_{n}_{i}|COI", seq.lower()) for i, (_, seq) in enumerate(records)
                      for n in range(depth)]
        fasta(raw, records + duplicates)
        execute("otu_split_new_by_hash.pl", raw, tmp_path / "empty_seen", active, tmp_path / "new_hashes")
        execute("otu_hash_map_from_fasta.pl", active, hm, hc)
        assert sorted(int(row[1]) for row in rows(hc)) == [1] * len(records), "raw depth excluded before per-round count"
        relation = tmp_path / "active_members.tsv"
        relation.write_text("".join(f"CLUST_0\t{rid}\t{int(rid == REP)}\n" for rid, _ in records))
        counts = tmp_path / "counts.tsv"
        execute("otu_counts_by_hash.pl", relation, hm, hc, counts, "strict")
        assert rows(counts) == [["CLUST_0", REP, str(round_number + 2)]], "per-round NR cardinality"
        execute("otu_update_frozen.pl", counts, representative, meta, frozen, history,
                members, promoted, f"bc_{round_number}", min_rounds, threshold, 2, .5, 0)
        observed.append(promoted.read_text().splitlines())
    assert rows(history) == [[sequence_hash(SEQUENCES["z_authority"]), "3", "4"]]
    return observed, rows(members)


@pytest.mark.parametrize("threshold,min_rounds,expected", [
    (5, 2, [[], ["CLUST_0"]]), (7, 2, [[], ["CLUST_0"]]),
    (8, 2, [[], []]), (5, 3, [[], []]), (3, 1, [["CLUST_0"], []])])
@pytest.mark.parametrize("depth", [0, 17])
def test_promotion_sequence_round_evidence_matrix(tmp_path, threshold, min_rounds, expected, depth):
    observed, members = promotion_case(tmp_path, threshold, min_rounds, depth)
    assert observed == expected, "promotion uses evidence 3 + 4 = 7 and a separate temporal requirement"
    if any(expected):
        assert members == [["FROZEN_" + sequence_hash(SEQUENCES["z_authority"]), REP, "1"]]
    else:
        assert members == []


def test_distinct_nr_members_grow_but_exact_relations_do_not(tmp_path):
    fid, meta, hm, append = frozen_fixture(tmp_path)
    execute("otu_add_frozen_members_by_hash.pl", meta, hm, append)
    members, index = tmp_path / "members", tmp_path / "index"
    execute("otu_members_append_unique.pl", members, append, index)
    known = {"z_authority": SEQUENCES["z_authority"]}
    for n, name in enumerate(("member_one", "member_two"), 2):
        known[name] = SEQUENCES[name]
        clstr = tmp_path / "assigned.clstr"
        clstr.write_text(f">Cluster 0\n0\t16nt, >{fid}|{REP}... *\n"
                         f"1\t16nt, >{name}|COI... at +/99.0%\n")
        execute("otu_frozen_members_from_clstr.pl", clstr, append, tmp_path / "unassigned", "strict", "auto")
        append.write_bytes(append.read_bytes() * 3)
        execute("otu_members_append_unique.pl", members, append, index)
        execute("otu_members_append_unique.pl", members, append, index)
        _, sizes = assert_canonical(members, known, REP)
        assert sizes == {fid: n}


def blast_case(tmp_path, depth):
    tmp_path.mkdir(exist_ok=True)
    a, b = "representative|COI", "ambiguous|COI"
    cluster = tmp_path / "clstr"
    cluster.write_text(f">Cluster 0\n0\t16nt, >{a}... *\n"
                       f"1\t16nt, >{b}... at +/99.0%\n"
                       f">Cluster 1\n0\t16nt, >{b}... *\n")
    raw = tmp_path / "eligible.fasta"
    records = [(a, "AAAA"), (b, "CCCC"), ("unmapped|COI", "GGGG")]
    records += [(f"duplicate_{n}|COI", "AAAA") for n in range(depth - 1)]
    fasta(raw, records)
    hm, hc = tmp_path / "hm", tmp_path / "hc"
    execute("otu_hash_map_from_fasta.pl", raw, hm, hc)
    kept, stats, otus = (tmp_path / n for n in ("kept", "stats", "otus"))
    execute("otu_filter_reads_by_otu_size.pl", cluster, hm, raw, 3, kept, stats, otus, "drop")
    info = dict(rows(stats))
    assert info["ambiguous_hash_cluster"] == "1"
    assert info["reads_missing_from_clstr"] == "2"
    expected = {rid for rid, seq in records if seq == "AAAA"} if depth >= 3 else set()
    actual = {line[1:] for line in kept.read_text().splitlines() if line.startswith(">")}
    assert actual == expected, "BLAST eligibility counts current raw support, excluding ambiguous/unmapped hashes"
    assert int(info["kept_reads"]) == len(expected)
    assert otus.read_text().splitlines() == (["CLUST_0"] if depth >= 3 else [])


def test_blast_eligible_support_is_current_round_raw_depth(tmp_path):
    # The same canonical cluster (two sequences) alternates support 5 -> 1 -> 5.
    for n, depth in enumerate((5, 1, 5)):
        blast_case(tmp_path / str(n), depth)


def consensus_case(tmp_path, mode):
    f = tmp_path / "sample_Consensus0"
    fasta(f, [("sample|OTUB_0|COI|reads-5|OTU=OTUB_0-COI", "AAAA"),
              ("sample|OTUB_1|COI|reads-3|OTU=OTUB_1-COI", "CCCC")])
    for i, count in ((0, 5), (1, 3)):
        (tmp_path / f"OTUB_{i}-COI_all_reads.list").write_text("".join(f"read_{i}_{n}\n" for n in range(count)))
    script = OVERRIDES.get("Best_consensus_addition.sh", ROOT / "bin/Best_consensus_addition.sh")
    p = subprocess.run(["/bin/bash", str(script), str(f)], cwd=tmp_path,
                       env=dict(os.environ, CONSENSUS_READS_MODE=mode), capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    tokens = p.stdout.splitlines()[0].split("|")
    assert f"reads-{5 if mode == 'representative' else 8}" in tokens, "mode-specific consensus support, not two canonical sequences"
    assert p.stdout.splitlines()[1] == "AAAA"
    assert len((tmp_path / "OriginalReads/Consensus0_reads.list").read_text().splitlines()) == (5 if mode == "representative" else 8)


@pytest.mark.parametrize("mode", ["representative", "cluster_total"])
def test_consensus_reads_n_is_separate_and_mode_dependent(tmp_path, mode):
    consensus_case(tmp_path, mode)


@pytest.mark.parametrize("condition", ["zero", "unavailable", "malformed", "conflicting"])
def test_zero_unavailable_invalid_are_distinct_and_retry_preserves_state(tmp_path, condition):
    fid, meta, hm, out = frozen_fixture(tmp_path)
    good = hm.read_bytes()
    state, seen = tmp_path / "members", tmp_path / "seen"
    state.write_text(f"{fid}\t{REP}\t1\n")
    execute("otu_members_append_unique.pl", state, out, seen)
    before = (state.read_bytes(), seen.read_bytes())
    out.write_text("previous-output\n")
    if condition == "zero":
        hm.write_text("")
    elif condition == "unavailable":
        hm.unlink()
    elif condition == "malformed":
        hm.write_bytes(good + b"late_broken\tNA\n")
    else:
        hm.write_bytes(good + (rows(hm)[0][0] + "\t" + sequence_hash(SEQUENCES["member_one"]) + "\n").encode())
    helper = OVERRIDES.get("otu_add_frozen_members_by_hash.pl", ROOT / "bin/otu_add_frozen_members_by_hash.pl")
    # Execute the real success-only publication boundary used by set -e callers.
    cmd = "perl " + " ".join(shlex.quote(str(x)) for x in (helper, meta, hm, out))
    cmd += "\nperl " + " ".join(shlex.quote(str(x)) for x in (ROOT / "bin/otu_members_append_unique.pl", state, out, seen))
    result = subprocess.run(["/bin/bash", "-euc", cmd], capture_output=True, text=True)
    if condition == "zero":
        assert result.returncode == 0 and out.read_bytes() == b"", "computed zero has successful empty output"
    else:
        assert result.returncode != 0, "unavailable/invalid input must not become successful zero"
        assert out.read_bytes() == b"previous-output\n", "validate complete input before replacing output"
    assert (state.read_bytes(), seen.read_bytes()) == before
    hm.write_bytes(good)
    retry = subprocess.run(["/bin/bash", "-euc", cmd], capture_output=True, text=True)
    assert retry.returncode == 0, retry.stderr
    assert rows(out) == [[fid, REP, "1"]]
    assert (state.read_bytes(), seen.read_bytes()) == before, "retry cannot grow already persisted NR relation"


def load_round_harness():
    spec = importlib.util.spec_from_file_location("_r3c4_round", ROOT / "tests/test_otu_frozen_assignment_round_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_no_new_failed_round_and_resume_do_not_advance_history(tmp_path):
    c1 = load_round_harness()
    r = c1.Round(tmp_path / "run")
    seq = c1.dna(4101, 420)
    reads = [(c1.ident("authority"), seq), (c1.ident("one"), c1.variant(seq[:400], 7)),
             (c1.ident("two"), c1.variant(seq[:400], 31))]
    r.good(reads)
    history = r.state / "otu_frozen_history.tsv"
    assert rows(history) == [[sequence_hash(seq), "3"]]
    active = r.active.read_bytes()
    r.good([(c1.ident("raw_duplicate"), seq)])
    r.good()
    assert rows(history) == [[sequence_hash(seq), "3"]]
    assert r.active.read_bytes() == active
    faults = tmp_path / "faults"; faults.mkdir()
    executable = faults / "cd-hit-est"
    executable.write_text("#!/bin/bash\nexit 73\n"); executable.chmod(0o755)
    new = (c1.ident("three"), c1.variant(seq[:400], 83))
    failed = r.execute([new], env=dict(os.environ, PATH=str(faults) + os.pathsep + os.environ["PATH"]))
    assert failed.returncode != 0
    assert rows(history) == [[sequence_hash(seq), "3"]]
    assert r.active.read_bytes() == active
    # Failed clustering does not commit seen hashes; a successful retry counts once.
    r.good([new])
    assert rows(history) == [[sequence_hash(seq), "3", "4"]], "successful retry records one qualifying round"
    assert len(rows(r.active)) == 4
    r.good([(c1.ident("four"), c1.variant(seq[:400], 113))])
    assert rows(history) == [[sequence_hash(seq), "3", "4", "5"]]
    before = {p.name: p.read_bytes() for p in (r.active, r.pool, history)}
    r.good()
    assert {p.name: p.read_bytes() for p in (r.active, r.pool, history)} == before


def measure_scale(tmp_path, script=None):
    helper = script or ROOT / "bin/otu_add_frozen_members_by_hash.pl"
    timings = []
    for n in (4000, 32000):
        meta, hm, out = (tmp_path / f"{n}.{suffix}" for suffix in ("meta", "hm", "out"))
        # Every hash is different: array-based duplicate scans cannot short-circuit.
        truth = [(hashlib.md5(str(i).encode()).hexdigest(), f"rep{i}|COI") for i in range(n)]
        meta.write_text("".join(f"FROZEN_{h}\t{rid}\t{h}\n" for h, rid in truth))
        observations = [f"raw{i}|COI\t{h}\n" for i, (h, _) in enumerate(truth)]
        hm.write_text("".join(observations + observations[::-1]))
        expected = "".join(f"FROZEN_{h}\t{rid}\t1\n" for h, rid in sorted(truth)).encode()
        samples = []
        limit = 10 if not timings else min(10, max(.75, timings[0] * 20))
        for _ in range(3):
            start = time.perf_counter()
            try:
                p = subprocess.run(["perl", str(helper), str(meta), str(hm), str(out)], capture_output=True, timeout=limit)
            except subprocess.TimeoutExpired:
                raise AssertionError("runtime scaling exceeded subquadratic budget") from None
            samples.append(time.perf_counter() - start)
            assert p.returncode == 0, p.stderr
            assert out.read_bytes() == expected, "scale preserves exact deterministic bytes"
        timings.append(statistics.median(samples))
    assert timings[1] <= max(.75, timings[0] * 20), "eightfold input must not approach quadratic scaling"
    print("R3C4_PERFORMANCE " + json.dumps({"hashes": [4000, 32000], "rows": [8000, 64000],
          "median_seconds": timings, "ratio": timings[1] / timings[0]}))
    return timings


def test_adversarial_runtime_scaling(tmp_path):
    measure_scale(tmp_path)


def mutant_script(tmp_path, name, replacements):
    source = (ROOT / "bin" / name).read_text()
    for old, new in replacements:
        assert old in source, "mutant construction anchor"  # Never itself a kill.
        source = source.replace(old, new, 1)
    path = tmp_path / name
    path.write_text(source)
    check = subprocess.run((["/bin/bash", "-n"] if name.endswith(".sh") else ["perl", "-c"]) + [str(path)],
                           capture_output=True, text=True)
    assert check.returncode == 0, check.stderr  # Syntax failures never count.
    return path


MUTANTS = ["raw_ids", "lexical_rep", "one_rep_each_projection", "invent_projected_rep",
           "two_projected_reps", "current_cardinality", "raw_depth_promotion",
           "duplicate_nr_relation", "blast_is_membership", "consensus_is_membership",
           "unavailable_is_zero", "malformed_is_zero", "input_order", "quadratic"]


@pytest.mark.parametrize("mutant", MUTANTS)
def test_semantic_mutants_are_killed(tmp_path, monkeypatch, mutant):
    """Kills require a violated oracle over executed behavior, never an edit guard."""
    run = tmp_path / "run"; run.mkdir()
    if mutant in ("one_rep_each_projection", "invent_projected_rep", "two_projected_reps"):
        report = marker_report(run)
        assert_projection(report)
        if mutant == "one_rep_each_projection":
            # Mutant consumer rejects the real valid zero-representative projection.
            data = tmp_path / "projected.json"; data.write_text(json.dumps(report))
            code = ("import json,sys; from collections import Counter; r=json.load(open(sys.argv[1])); "
                    "c=Counter(x['OTU_id'] for x in r if x['OTU_role']=='REPRESENTATIVE'); "
                    "sys.exit(0 if all(c[x['OTU_id']]==1 for x in r) else 1)")
            p = subprocess.run([sys.executable, "-B", "-c", code, str(data)])
            with pytest.raises(AssertionError, match="valid projected"):
                assert p.returncode == 0, "valid projected OTU was incorrectly rejected"
        else:
            if mutant == "invent_projected_rep":
                report[1]["OTU_role"] = "REPRESENTATIVE"
            else:
                report.append(dict(report[0]))
            # Persist and reload the mutated product: multiplicity is not lost in a set.
            data = tmp_path / "mutated-report.json"; data.write_text(json.dumps(report))
            with pytest.raises(AssertionError, match="projected roles"):
                assert_projection(json.loads(data.read_text()))
        print("R3C4_KILLED " + mutant)
        return
    name = "otu_add_frozen_members_by_hash.pl"
    oracle = lambda: test_frozen_exact_duplicate_depth_and_authority(run, 5, False, False)
    message = "metadata-designated"
    match_line = "$matched{$hash} = 1 if exists $hash2frozen{$hash};"
    if mutant == "raw_ids":
        # Restore the real historical raw-observation behavior as the negative control.
        source = subprocess.check_output(["git", "--no-optional-locks", "-C", str(ROOT), "show", BASE + ":bin/" + name])
        path = tmp_path / name; path.write_bytes(source)
        syntax = subprocess.run(["perl", "-c", str(path)], capture_output=True)
        assert syntax.returncode == 0
    elif mutant == "lexical_rep":
        path = mutant_script(tmp_path, name, [
            ("my (%matched, %rid2hash);", "my (%matched, %rid2hash, %lex);"),
            (match_line, match_line + "\n  $lex{$hash} = $rid if !exists $lex{$hash} || $rid lt $lex{$hash};"),
            ('$hash2rep{$hash}, 1)', '$lex{$hash}, 1)')])
    elif mutant == "current_cardinality":
        name = "otu_update_frozen.pl"
        path = mutant_script(tmp_path, name, [("$reads += $_ for @$arr;", "$reads = $cluster{$cid}{n};")])
        oracle = lambda: test_promotion_sequence_round_evidence_matrix(run, 5, 2, [[], ["CLUST_0"]], 17)
        message = "promotion uses evidence"
    elif mutant == "raw_depth_promotion":
        name = "otu_split_new_by_hash.pl"
        path = mutant_script(tmp_path, name, [("$seen{$h} = 1;", "# mutant: retain raw duplicates")])
        oracle = lambda: promotion_case(run)
        message = "raw depth excluded"
    elif mutant == "duplicate_nr_relation":
        name = "otu_members_append_unique.pl"
        path = mutant_script(tmp_path, name, [("next if $seen{$k};", "# mutant: append duplicate NR relations")])
        oracle = lambda: test_distinct_nr_members_grow_but_exact_relations_do_not(run)
        message = "one canonical row"
    elif mutant == "blast_is_membership":
        name = "otu_filter_reads_by_otu_size.pl"
        path = mutant_script(tmp_path, name, [("$sum += ($hash_count{$hash} // 0);", "$sum += 1;")])
        oracle = lambda: blast_case(run, 5)
        message = "BLAST eligibility"
    elif mutant == "consensus_is_membership":
        name = "Best_consensus_addition.sh"
        path = mutant_script(tmp_path, name, [("# Print the result", "selected_reads=2\ntotal_reads=2\n# Print the result")])
        oracle = lambda: consensus_case(run, "cluster_total")
        message = "mode-specific consensus"
    elif mutant in ("unavailable_is_zero", "malformed_is_zero"):
        # Executable compatibility anti-pattern: turn helper failure into empty success.
        path = tmp_path / name
        path.write_text("use strict; use warnings;\nmy $rc = system('perl', "
                        + repr(str(ROOT / "bin" / name)) + ", @ARGV);\n"
                        "if ($rc != 0) { open my $O, '>', $ARGV[2] or die $!; close $O; }\nexit 0;\n")
        oracle = lambda: test_zero_unavailable_invalid_are_distinct_and_retry_preserves_state(
            run, "unavailable" if mutant == "unavailable_is_zero" else "malformed")
        message = "must not become successful zero"
    elif mutant == "input_order":
        path = mutant_script(tmp_path, name, [
            ("my (%matched, %rid2hash);", "my (%matched, %rid2hash); my @order;"),
            (match_line, "push @order, $hash if exists $hash2frozen{$hash} && !$matched{$hash}++;"),
            ("sort keys %matched", "@order")])
        oracle = lambda: test_multiple_frozen_hashes_have_deterministic_bytes(run)
        message = "sorted canonical bytes"
    else:
        path = mutant_script(tmp_path, name, [
            ("my (%matched, %rid2hash);", "my (%matched, %rid2hash); my @seen;"),
            (match_line, "if (exists $hash2frozen{$hash} && !grep { $_ eq $hash } @seen) {\n"
                         "    push @seen, $hash; $matched{$hash} = 1;\n  }")])
        oracle = lambda: measure_scale(run, path)
        message = "runtime scaling|quadratic scaling"
    monkeypatch.setitem(OVERRIDES, name, path)
    with pytest.raises(AssertionError, match=message):
        oracle()
    print("R3C4_KILLED " + mutant)
