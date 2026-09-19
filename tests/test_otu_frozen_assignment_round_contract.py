"""C1: real CD-HIT contracts and executed OTU_definition shell sections.

No biological pipeline is launched. Groovy substitutions are fixed fixture values;
all assignment, pooling, clustering, freezing, parsing and merging use repository
code and installed CD-HIT. The independent oracle uses manufactured substitution
families and explicit relation sets, never the repository parsing expressions.
"""
import hashlib
import os
from pathlib import Path
import random
import re
import shlex
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "main.nf").read_text()
BASE = "4769f3b67f2cb7bc7a5c7d2909415ace99afa9b3"
BC = "bc"
MEMBERS = "bc_otu_active_members.tsv"
MERGED = "bc_qced_reads_nr.fasta.clstr"


def run(args, cwd=None, env=None):
    return subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                          text=True, capture_output=True)


def perl(name, *args):
    p = run(["perl", ROOT / "bin" / name, *args])
    assert p.returncode == 0, p.stderr
    return p


def fasta(path, rows):
    path.write_text("".join(f">{name}\n{seq}\n" for name, seq in rows))


def sequences(path):
    result = {}
    for record in path.read_text().split(">"):
        if record:
            lines = record.splitlines()
            assert lines[0] not in result, "duplicate FASTA ID"
            result[lines[0]] = "".join(lines[1:])
    return result


def rows(path):
    return [line.split("\t") for line in path.read_text().splitlines() if line]


def merged_relations(path):
    """Token-based independent synthetic-cluster reader, including multiplicity."""
    groups = []
    for line in path.read_text().splitlines():
        if line.startswith(">Cluster "):
            groups.append([])
        else:
            fields = line.split()
            assert fields[1] == "0nt,", "synthetic schema changed"
            ident = fields[2].removeprefix(">").removesuffix("...")
            groups[-1].append((ident, fields[-1] == "*"))
    return groups


def assert_complete(path, expected):
    actual = [ident for group in merged_relations(path) for ident, _ in group]
    assert set(actual) == set(expected), "merged membership completeness"
    assert len(actual) == len(set(actual)), "merged membership multiplicity"
    assert all(sum(rep for _, rep in group) == 1 for group in merged_relations(path)), "representative identity"


def assert_groups(path, expected):
    actual = {}
    for group in merged_relations(path):
        representatives = [ident for ident, is_rep in group if is_rep]
        assert len(representatives) == 1, "one representative per merged cluster"
        rep = representatives[0]
        assert rep not in actual, "one merged cluster per representative"
        actual[rep] = {ident for ident, _ in group}
    assert actual == expected, "merged representative/member relation"


def ident(name):
    return name + "|COI|sup|barcode=bc|adapter=bc"


def dna(seed, length=320):
    r = random.Random(seed)
    return "".join(r.choice("ACGT") for _ in range(length))


def variant(seq, pos):
    return seq[:pos] + ("A" if seq[pos] != "A" else "C") + seq[pos + 1:]


def fixture_reads():
    refs = [("FROZEN_" + hashlib.md5(dna(i).encode()).hexdigest() + "|" + ident(f"rep{i}"), dna(i)) for i in (41, 71)]
    queries = [(ident(f"a{i}"), variant(refs[0][1], 11 + i * 29)) for i in range(3)]
    queries += [(ident("b"), variant(refs[1][1], 53)), (ident("unassigned"), dna(901))]
    return refs, queries


def oracle(refs, queries, threshold=.97):
    # Fixtures contain substitutions and optional terminal extensions, with no
    # indels; families are separated by >60% mismatches and positive hits >99%.
    answer = {}
    for query, seq in queries:
        hits = []
        for ref, refseq in refs:
            n = min(len(seq), len(refseq))
            fraction = sum(a == b for a, b in zip(seq[:n], refseq[:n])) / n
            if fraction >= threshold:
                hits.append(ref.split("|")[0])
        assert len(hits) <= 1, "fixture must not depend on ambiguous best-hit ordering"
        if hits:
            answer[query] = hits[0]
    return answer


def render(text, values):
    text = re.sub(r"(?<!\\)\$\{([^}]+)\}", lambda m: str(values[m[1]]), text)
    return text.replace(r"\$", "$").replace(r'\"', '"').replace(r"\\", "\\")


def values(root, round_no=1, freeze=99, minimum=1):
    return {"baseDir": ROOT, "barcode": BC, "round_barcode": f"bc_{round_no}",
            "ongoingStateDir": root, "cdHitIdentity": ".97",
            "params.otu_incremental_min_new": minimum,
            "params.otu_pool_decision_include_hash": "true",
            "params.otu_commit_dropped_hashes": "false",
            "params.otu_frozen_min_rounds": freeze, "params.otu_frozen_min_reads": 1,
            "params.otu_frozen_growth_window": 2, "params.otu_frozen_drop_ratio": .5,
            "params.otu_frozen_min_frac": 0,
            "params.otu_hashmap_mixed_policy": "error", "params.otu_allow_unsafe_recovery": "false"}


def shell(work, script, env=None):
    work.mkdir(parents=True, exist_ok=True)
    path = work / "rendered.sh"
    path.write_text("set -euo pipefail\n" + script)
    syntax = run(["/bin/bash", "-n", path])
    assert syntax.returncode == 0, syntax.stderr
    result = run(["/bin/bash", path], cwd=work, env=env)
    (work / "stdout.log").write_text(result.stdout)
    (work / "stderr.log").write_text(result.stderr)
    return result


def assignment(work, refs, queries, site="live", source=SOURCE):
    work.mkdir(parents=True)
    fasta(work / "refs.fa", refs)
    fasta(work / "queries.fa", queries)
    env_vars = {"FROZEN_REPS": work / "refs.fa", "NEW_FASTA": work / "queries.fa",
                "PRUNED_ARCHIVE": work / "queries.fa", "THREADS": 1,
                "OTU_PRUNED_RECOVERY_ID": .97, "OTU_ID_MODE": "strict",
                "FROZEN_DB_ONLY_POLICY": "auto", "FROZEN_ENABLED": 1,
                "FROZEN_MEMBERS": work / "cumulative.tsv",
                "FROZEN_MEMBERS_SEEN": work / "seen.tsv"}
    (work / "cumulative.tsv").touch()
    prefix = "\n".join(k + "=" + shlex.quote(str(v)) for k, v in env_vars.items()) + "\n"
    if site == "live":
        a = source.index('\t\t\tFROZEN_2D_OK=0')
        b = source.index('\n\t\t\t\tif [ -s ${barcode}_new_unassigned.fasta ]', a)
        text = source[a:b]
        member_path = work / "bc_frozen_members_new.tsv"
        unassigned_path = work / "bc_new_unassigned.fasta"
    else:
        # Execute the actual archive command and parser invocation (the enclosing
        # recovery lock, accumulated-HQ append and barrier transactions are unchanged).
        command = next(line.strip()[3:].removesuffix("; then") for line in source.splitlines()
                       if 'if cd-hit-est-2d ' in line and 'archive_vs_frozen' in line)
        parser = next(line.strip()[5:].removesuffix("; then") for line in source.splitlines()
                      if 'if ! ${baseDir}/bin/otu_frozen_members_from_clstr.pl' in line)
        prefix += 'ARCHIVE_CLSTR=bc_archive_vs_frozen.clstr\nARCHIVE_RECOVERY_MEMBERS_RAW=members.tsv\nARCHIVE_RECOVERY_UNASSIGNED=unassigned.list\n'
        extraction = next(line.strip() for line in source.splitlines()
                          if "awk '/^>/" in line and 'archive_vs_frozen >' in line)
        text = command + "\n" + parser + "\n" + extraction + "\n"
        member_path = work / "members.tsv"
        unassigned_path = work / "bc_archive_vs_frozen"
    result = shell(work, prefix + render(text, values(work)))
    assert result.returncode == 0, result.stderr
    if site == "archive":
        assert set((work / "unassigned.list").read_text().splitlines()) == set(sequences(unassigned_path))
    return rows(member_path), sequences(unassigned_path)


def check_assignment(work, source=SOURCE, site="live", reverse=False, ref_only=False, longer=False):
    refs, queries = fixture_reads()
    if ref_only:
        queries = queries[:3] + queries[-1:]
    if longer:
        queries.append((ident("longer"), refs[0][1] + "ACGT"))
    if reverse:
        refs, queries = list(reversed(refs)), list(reversed(queries))
    assigned, unassigned = assignment(work, refs, queries, site, source)
    expected = oracle(refs, queries)
    assert {row[1]: row[0] for row in assigned} == expected, "query-to-frozen mapping"
    assert len(assigned) == len(expected), "assignment multiplicity"
    assert all(row[2] == "0" for row in assigned), "query/reference representative roles"
    assert set(unassigned) == {q for q, _ in queries} - set(expected), "unassigned conservation"
    assert not (set(unassigned) & set(expected)), "disjoint frozen and active assignment"
    return assigned


@pytest.mark.parametrize("site", ["live", "archive"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("ref_only,longer", [(False, False), (True, False), (True, True)])
def test_real_tool_contract(tmp_path, site, reverse, ref_only, longer):
    check_assignment(tmp_path / "tool", site=site, reverse=reverse, ref_only=ref_only, longer=longer)


def test_old_direction_negative_control(tmp_path):
    refs, queries = fixture_reads()
    expected = oracle(refs, queries)
    for reverse in (False, True):
        work = tmp_path / str(reverse)
        work.mkdir()
        fasta(work / "refs", list(reversed(refs)) if reverse else refs)
        fasta(work / "queries", list(reversed(queries)) if reverse else queries)
        cp = run(["cd-hit-est-2d", "-i", work / "queries", "-i2", work / "refs", "-c", ".97", "-d", 0, "-T", 1, "-o", work / "old"])
        assert cp.returncode == 0
        perl("otu_frozen_members_from_clstr.pl", work / "old.clstr", work / "members", work / "unassigned", "strict", "auto")
        mapped = {r[1]: r[0] for r in rows(work / "members")}
        assert len(mapped) == 2 < len(expected) == 4
        assert set(mapped) < set(expected)
        assert len(set(expected) & set((work / "unassigned").read_text().splitlines())) == 2


class Round:
    def __init__(self, root, source=SOURCE, freeze=99):
        self.root, self.source, self.freeze = root, source, freeze
        self.state = root / "_state"
        self.state.mkdir(parents=True)
        for name in ("otu_active_pool.fasta", "otu_frozen_meta.tsv", "otu_frozen_reps.fasta",
                     "otu_frozen_history.tsv", "otu_frozen_members.tsv", "otu_frozen_members_seen.tsv", "otu_seen_hashes.tsv"):
            (self.state / name).touch()
        self.n = 0

    @property
    def active(self):
        return self.state / MEMBERS

    @property
    def pool(self):
        return self.state / "otu_active_pool.fasta"

    def seed_frozen(self, refs):
        fasta(self.state / "otu_frozen_reps.fasta", refs)
        (self.state / "otu_frozen_members.tsv").write_text("".join(
            f"{ref.split('|')[0]}\t{ref.split('|', 1)[1]}\t1\n" for ref, _ in refs))
        (self.state / "otu_frozen_meta.tsv").write_text("".join(
            f"{ref.split('|')[0]}\t{ref.split('|', 1)[1]}\t{hashlib.md5(seq.encode()).hexdigest()}\n" for ref, seq in refs))

    def execute(self, incoming=(), env=None, minimum=1):
        self.n += 1
        self.work = self.root / f"task{self.n}"
        self.work.mkdir()
        fasta(self.work / "bc_qced_reads_hq_accumulated.fasta", incoming)
        v = {"STATE_DIR": self.state, "FROZEN_META": self.state / "otu_frozen_meta.tsv",
             "FROZEN_REPS": self.state / "otu_frozen_reps.fasta",
             "FROZEN_HIST": self.state / "otu_frozen_history.tsv",
             "FROZEN_MEMBERS": self.state / "otu_frozen_members.tsv",
             "FROZEN_MEMBERS_SEEN": self.state / "otu_frozen_members_seen.tsv",
             "ACTIVE_POOL": self.pool, "ACTIVE_MEMBERS_STATE": self.active,
             "SEEN_HASHES": self.state / "otu_seen_hashes.tsv", "THREADS": 1,
             "FROZEN_ENABLED": 1, "OTU_ID_MODE": "strict", "FROZEN_DB_ONLY_POLICY": "auto",
             "ROUND_ID": f"bc_{self.n}"}
        prelude = "\n".join(k + "=" + shlex.quote(str(x)) for k, x in v.items()) + "\n"
        a = self.source.index('\t\tHASH_MAP="${barcode}_otu_hash_map.tsv"', self.source.index('process OTU_definition'))
        b = self.source.index('\t\trtbioscan_round_lock_unpin', a)
        script = render(self.source[a:b], values(self.root, self.n, self.freeze, minimum))
        self.result = shell(self.work, prelude + script, env)
        return self.result

    def good(self, incoming=(), **kwargs):
        p = self.execute(incoming, **kwargs)
        assert p.returncode == 0, p.stderr
        assert (self.work / MERGED).is_file(), "declared merged output exists"
        return p


def check_first(root, source=SOURCE):
    r = Round(root, source)
    reads = [(ident("active1"), dna(301)), (ident("active2"), variant(dna(301), 91))]
    r.good(reads)
    assert r.active.is_file(), "active membership persisted"
    active = rows(r.active)
    assert {x[1] for x in active} == {i for i, _ in reads}, "active state completeness"
    assert [x[:3] for x in active] == rows(r.work / "bc_active_members.tsv"), "representative flags persisted"
    assert all(x[3] == hashlib.sha256(r.pool.read_bytes()).hexdigest() for x in active), "pool fingerprint"
    assert_complete(r.work / MERGED, [i for i, _ in reads])
    return r, reads


def test_first_round(tmp_path):
    check_first(tmp_path / "round")


def check_skips(root, source=SOURCE):
    r, active = check_first(root, source)
    refs, queries = fixture_reads()
    r.seed_frozen(refs)
    expected = {i for i, _ in active} | {ref.split("|", 1)[1] for ref, _ in refs}
    before = r.active.read_bytes()
    history = (r.state / "otu_frozen_history.tsv").read_bytes()
    groups = {active[0][0]: {i for i, _ in active}}
    groups.update({ref.split("|", 1)[1]: {ref.split("|", 1)[1]} for ref, _ in refs})
    # Include a true no-input round, then three rounds of frozen-only growth.
    for n in range(4):
        batch = [] if n == 0 else [(ident(f"round{n}a{k}"), variant(refs[0][1], 15+n*10+k)) for k in range(3)] + [(ident(f"round{n}b"), variant(refs[1][1], 33+n))]
        expected.update(i for i, _ in batch)
        mapping = oracle(refs, batch)
        for ref, _ in refs:
            groups[ref.split("|", 1)[1]].update(i for i, cid in mapping.items() if cid == ref.split("|")[0])
        (r.state / "qced_reads_nr.fasta.clstr").write_text(">Cluster 9\n0\t0nt, >STALE... *\n")
        r.good(batch)
        assert r.active.read_bytes() == before, "skipped round preserves active state"
        assert not (r.work / "bc_active_nr.fasta").exists(), "unchanged pool skips clustering"
        assert_complete(r.work / MERGED, expected)
        assert_groups(r.work / MERGED, groups)
        assert (r.state / "otu_frozen_history.tsv").read_bytes() == history, "skipped rounds preserve history"
    return r


def test_consecutive_skipped_rounds(tmp_path):
    check_skips(tmp_path / "round")


def check_bootstrap(root, source=SOURCE):
    r = Round(root, source)
    read = (ident("legacy_active"), dna(503))
    fasta(r.pool, [read])
    (r.state / "qced_reads_nr.fasta.clstr").write_text(">Cluster 8\n0\t0nt, >STALE... *\n")
    history = (r.state / "otu_frozen_history.tsv").read_bytes()
    pool_before = r.pool.read_bytes()
    p = r.good()
    assert r.active.is_file(), "bootstrap persists active state"
    assert p.stderr.count("bootstrapping active membership") == 1, "one bootstrap diagnostic"
    assert (r.work / "bc_active_nr.fasta").is_file(), "bootstrap forced clustering"
    assert_complete(r.work / MERGED, [read[0]])
    assert r.pool.read_bytes() == pool_before
    assert (r.state / "otu_frozen_history.tsv").read_bytes() == history
    state_before = r.active.read_bytes()
    p = r.good()
    assert "bootstrapping active membership" not in p.stderr
    assert r.active.read_bytes() == state_before
    assert not (r.work / "bc_active_nr.fasta").exists()
    assert_complete(r.work / MERGED, [read[0]])


def test_upgrade_bootstrap_and_retry(tmp_path):
    check_bootstrap(tmp_path / "round")


def check_empty(root, source=SOURCE, frozen=True):
    r = Round(root, source)
    refs, _ = fixture_reads()
    if frozen:
        r.seed_frozen(refs)
    r.good()
    assert r.active.exists() and r.active.read_bytes() == b"", "valid empty active state"
    assert_complete(r.work / MERGED, [ref.split("|", 1)[1] for ref, _ in refs] if frozen else [])
    before = r.active.stat().st_ino
    r.good()
    assert r.active.stat().st_ino == before, "empty state valid on retry"
    assert_complete(r.work / MERGED, [ref.split("|", 1)[1] for ref, _ in refs] if frozen else [])


@pytest.mark.parametrize("frozen", [False, True])
def test_empty_active_pool(tmp_path, frozen):
    check_empty(tmp_path / "round", frozen=frozen)


def check_changed(root, source=SOURCE):
    r, old = check_first(root, source)
    # Simulate an interruption after the durable pool update, before publication.
    new = (ident("changed"), dna(831))
    fasta(r.pool, old + [new])
    r.good()
    assert_complete(r.work / MERGED, [x[0] for x in old + [new]])
    assert {x[1] for x in rows(r.active)} == {x[0] for x in old + [new]}, "changed pool reparsed"
    # Newly staged small batches must also not be hidden by the minimum-new skip.
    extra = (ident("below_threshold"), dna(855))
    r.good([extra], minimum=100)
    assert_complete(r.work / MERGED, [x[0] for x in old + [new, extra]])


def test_changed_pool_retry_and_small_batch(tmp_path):
    check_changed(tmp_path / "round")


def failure_env(root, boundary):
    """Fault only the publication commands; scientific tools remain real."""
    root.mkdir()
    definitions = {
        "mktemp": ('case "$1" in *otu_active_members.tsv.tmp.*) exit 71;; esac\n' if boundary == "create" else ""),
        "awk": ('case "${2:-}" in pool_hash=*) printf "PARTIAL\\n"; exit 72;; esac\n' if boundary == "write" else ""),
        "mv": "",
    }
    if boundary in ("rename", "before_rename", "after_rename"):
        action = {"rename": "exit 73",
                  "before_rename": 'kill -TERM "$PPID"; exit 74',
                  "after_rename": '/bin/mv "$@"; kill -TERM "$PPID"; exit 75'}[boundary]
        definitions["mv"] = f'case "$*" in *otu_active_members.tsv*) {action};; esac\n'
    for name, injected in definitions.items():
        wrapper = root / name
        wrapper.write_text('#!/bin/bash\n' + injected + 'exec ' + shlex.quote(shutil.which(name)) + ' "$@"\n')
        wrapper.chmod(0o755)
    return dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"])


def check_failure(root, boundary, source=SOURCE):
    # Start from a known valid candidate state, then execute the disposable mutant
    # (if any) at the replacement boundary. No mutant touches the checkout.
    r, initial = check_first(root / "run")
    r.source = source
    old = r.active.read_bytes()
    old_inode = r.active.stat().st_ino
    extra = (ident("after_failure"), dna(444))
    env = failure_env(root / "fault-bin", boundary)
    p = r.execute([extra], env=env)
    assert p.returncode != 0, "failed publication must fail the task"
    if boundary != "after_rename":
        assert r.active.read_bytes() == old, "old state preserved before rename"
        assert r.active.stat().st_ino == old_inode, "final state not replaced before rename"
    else:
        assert {x[1] for x in rows(r.active)} == {x[0] for x in initial + [extra]}, "complete state after rename"
        assert r.active.stat().st_ino != old_inode, "publication atomically replaces inode"
    # A stale temporary must never serve as authority.
    (r.state / (MEMBERS + ".tmp.stale")).write_text("POISON\n")
    r.source = SOURCE
    r.good([extra])
    assert_complete(r.work / MERGED, [x[0] for x in initial + [extra]])
    assert {x[1] for x in rows(r.active)} == {x[0] for x in initial + [extra]}, "retry convergence"
    assert (r.state / (MEMBERS + ".tmp.stale")).read_text() == "POISON\n"


@pytest.mark.parametrize("boundary", ["create", "write", "rename", "before_rename", "after_rename"])
def test_publication_failure_and_interruption(tmp_path, boundary):
    check_failure(tmp_path, boundary)


def test_successful_replacement_is_atomic(tmp_path):
    r, initial = check_first(tmp_path / "run")
    before = r.active.stat().st_ino
    # An open reader of the old inode must retain a complete old generation.
    with r.active.open("rb") as reader:
        old = reader.read()
        extra = (ident("replacement"), dna(474))
        r.good([extra])
        reader.seek(0)
        assert reader.read() == old
    assert r.active.stat().st_ino != before
    assert_complete(r.work / MERGED, [i for i, _ in initial + [extra]])


def test_no_publication_after_clustering_or_parsing_failure(tmp_path):
    for tool in ("cd-hit-est", "otu_parse_clstr.pl"):
        r, initial = check_first(tmp_path / tool)
        old = r.active.read_bytes()
        extra = (ident("failure"), dna(141))
        if tool == "cd-hit-est":
            fake_bin = tmp_path / "fail-bin"
            fake_bin.mkdir()
            fake = fake_bin / tool
            fake.write_text("#!/bin/bash\nexit 81\n")
            fake.chmod(0o755)
            env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
        else:
            # Substitute the actual parser executable with a failing subprocess;
            # this is fault injection, not a scientific implementation substitute.
            r.source = SOURCE.replace('${baseDir}/bin/otu_parse_clstr.pl ${barcode}_active_nr.fasta.clstr', '/usr/bin/false ${barcode}_active_nr.fasta.clstr')
            env = None
        assert r.execute([extra], env=env).returncode != 0
        assert r.active.read_bytes() == old
        r.source = SOURCE
        r.good([extra])
        assert_complete(r.work / MERGED, [i for i, _ in initial + [extra]])


def check_simulator(root, reverse=False, source=SOURCE):
    """Bounded sim2 reconstruction: two taxa grow, freeze, then grow frozen-only.

    Shorter variants keep a unique longest representative independent of order.
    The F-02 promotion round is audited separately and is not a correctness oracle.
    """
    r = Round(root, source, freeze=3)
    expected = set()
    originals = [(ident("taxonA_rep"), dna(1021)), (ident("taxonB_rep"), dna(2021))]
    promotion_observation = None
    sentinel = (ident("active_control"), dna(3301))
    active_state = None
    for n in range(1, 8):
        batch = originals.copy() if n == 1 else []
        for taxon, (name, seq) in enumerate(originals):
            for k in range(3):
                batch.append((ident(f"taxon{taxon}_r{n}_{k}"), variant(seq[:-2], n*17+k)))
        if n == 4:
            batch.append(sentinel)
        if reverse:
            batch.reverse()
        expected.update(i for i, _ in batch)
        r.good(batch)
        meta = rows(r.state / "otu_frozen_meta.tsv")
        if n < 3:
            assert not meta
            assert_complete(r.work / MERGED, expected)
        elif n == 3:
            assert len(meta) == 2, "exactly two taxa freeze"
            # Record rather than approve duplication; no assertion relies on it.
            groups = merged_relations(r.work / MERGED)
            flattened = [i for g in groups for i, _ in g]
            promotion_observation = {"round": n, "rows": len(flattened),
                                     "unique_members": len(set(flattened)),
                                     "duplicate_rows": len(flattened)-len(set(flattened))}
            (root / "f02-observed.txt").write_text(str(promotion_observation) + "\n")
        else:
            assert len(meta) == 2, "no extra frozen OTUs"
            assert set(sequences(r.pool)) == {sentinel[0]}, "no shadow active clusters"
            if n == 4:
                active_state = r.active.read_bytes()
            else:
                assert r.active.read_bytes() == active_state, "simulator preserves active membership through skips"
                assert not (r.work / "bc_active_nr.fasta").exists()
            frozen = rows(r.state / "otu_frozen_members.tsv")
            assert {x[1] for x in frozen} == expected - {sentinel[0]}, "cumulative frozen conservation"
            assert len(frozen) == len(expected) - 1, "frozen multiplicity"
            assert_complete(r.work / MERGED, expected)
            # Independent expected taxon assignment, tied to actual rep sequence hash.
            refs = [(row[0], seq) for row, (_, seq) in zip(sorted(meta, key=lambda x: x[1]), originals)]
            correct = oracle(refs, batch)
            actual = {x[1]: x[0] for x in frozen if x[1] in {i for i, _ in batch}}
            assert actual == correct, "simulator frozen mapping"
    return {tuple(x[:3]) for x in rows(r.state / "otu_frozen_members.tsv")}


def test_sim2_forward_reverse(tmp_path):
    forward = check_simulator(tmp_path / "forward")
    reverse = check_simulator(tmp_path / "reverse", reverse=True)
    assert forward == reverse, "sim2 invariant to input ordering"


def baseline_selector(tmp_path):
    selector = tmp_path / "bin"
    selector.mkdir()
    seqkit = selector / "seqkit"
    seqkit.write_text('''#!/usr/bin/env python3
import sys
from pathlib import Path
a=sys.argv
ids=set(Path(a[a.index('-l')+1]).read_text().splitlines())
for record in Path(a[a.index('-r')+1]).read_text().split('>'):
    if record and record.splitlines()[0] in ids:
        sys.stdout.write('>'+record)
''')
    seqkit.chmod(0o755)
    env = dict(os.environ, PATH=str(selector) + os.pathsep + os.environ["PATH"])
    return env


def test_normal_path_baseline_byte_equivalence(tmp_path):
    baseline = run(["git", "show", BASE + ":main.nf"], ROOT)
    assert baseline.returncode == 0
    # Reproduce unaffected scientific cases; old-direction no-match extraction
    # needs seqkit. An exact-ID FASTA selector below covers only this mechanical
    # baseline extraction, not assignment or clustering, which use real CD-HIT.
    env = baseline_selector(tmp_path)
    refs, _ = fixture_reads()
    for with_frozen in (False, True):
        old = Round(tmp_path / f"old-{with_frozen}", baseline.stdout)
        new = Round(tmp_path / f"new-{with_frozen}")
        if with_frozen:
            old.seed_frozen(refs)
            new.seed_frozen(refs)
        batch = [(ident("active"), dna(888)), (ident("active2"), variant(dna(888), 15))]
        for r in (old, new):
            r.good(batch, env=env)
        for name in (MERGED, "bc_active_members.tsv", "bc_active_counts.tsv", "bc_active_nr.fasta"):
            assert (old.work / name).read_bytes() == (new.work / name).read_bytes(), name
        for name in ("otu_active_pool.fasta", "otu_frozen_members.tsv", "otu_frozen_history.tsv", "otu_frozen_meta.tsv", "otu_seen_hashes.tsv"):
            assert (old.state / name).read_bytes() == (new.state / name).read_bytes(), name


def mutant(number):
    """Construct mutants in memory; rendered mutant scripts live under tmp_path."""
    s = SOURCE
    merge = '${baseDir}/bin/otu_merge_clstr.pl "\\$FROZEN_MEMBERS" "\\$ACTIVE_MEMBERS_STATE" ${barcode}_qced_reads_nr.fasta.clstr'
    publish = 'mv -f "\\$ACTIVE_MEMBERS_TMP" "\\$ACTIVE_MEMBERS_STATE"'
    def change(old, new):
        nonlocal s
        assert s.count(old) == 1, "mutant construction must be unambiguous"
        s = s.replace(old, new)
    if number in (1, 2):
        query = "NEW_FASTA" if number == 1 else "PRUNED_ARCHIVE"
        change(f'-i "\\$FROZEN_REPS" -i2 "\\${query}"', f'-i "\\${query}" -i2 "\\$FROZEN_REPS"')
    elif number == 3:
        change(merge, merge + '\nif [ "\\$SKIP_CLUSTER" -eq 1 ]; then cp "\\${STATE_DIR}/qced_reads_nr.fasta.clstr" ${barcode}_qced_reads_nr.fasta.clstr; fi')
    elif number == 4:
        change(publish, 'rm -f "\\$ACTIVE_MEMBERS_TMP"')
    elif number == 5:
        change(merge, 'if [ "\\$SKIP_CLUSTER" -eq 0 ]; then\n' + merge + '\nelse : > ${barcode}_qced_reads_nr.fasta.clstr; fi')
    elif number == 6:
        change('echo "ERROR: cannot publish active membership state" 1>&2\n\t\t\t\texit 1', 'echo "ERROR: cannot publish active membership state" 1>&2\n\t\t\t\t:')
    elif number == 7:
        change('> "\\$ACTIVE_MEMBERS_TMP" \\\n', '> "\\$ACTIVE_MEMBERS_STATE" \\\n')
        change(publish, 'true')
    elif number == 8:
        change(merge, 'if [ "\\$SKIP_CLUSTER" -eq 1 ]; then : > "\\$ACTIVE_MEMBERS_STATE"; fi\n' + merge)
    elif number == 9:
        change('[ "\\$ACTIVE_MEMBERS_POOL_HASH" != "\\$ACTIVE_POOL_HASH" ]', 'false')
    elif number == 10:
        change(merge, 'if [ ! -f "\\${FROZEN_MEMBERS}.stale" ]; then cp "\\$FROZEN_MEMBERS" "\\${FROZEN_MEMBERS}.stale"; fi\n' + merge.replace('"\\$FROZEN_MEMBERS"', '"\\${FROZEN_MEMBERS}.stale"'))
    elif number in (11, 12):
        condition = 'ACTIVE_MEMBERS_STATE' if number == 11 else 'FROZEN_MEMBERS'
        change(merge, f'if [ -s "\\${condition}" ]; then\n' + merge + '\nelse : > ${barcode}_qced_reads_nr.fasta.clstr; fi')
    elif number == 13:
        change('if [ ! -f "\\$ACTIVE_MEMBERS_STATE" ]; then', 'if [ ! -f "\\$ACTIVE_MEMBERS_STATE" ]; then\nif [ -f "\\${STATE_DIR}/qced_reads_nr.fasta.clstr" ]; then cp "\\${STATE_DIR}/qced_reads_nr.fasta.clstr" ${barcode}_qced_reads_nr.fasta.clstr; exit 0; fi')
    elif number == 14:
        line = '${baseDir}/bin/otu_frozen_members_from_clstr.pl "\\$FROZEN_CLSTR" ${barcode}_frozen_members_new.tsv ${barcode}_new_unassigned.list "\\$OTU_ID_MODE" "warn_skip"'
        change(line, line + '\nawk \'BEGIN{OFS="\\t"} {\\$3=1; print}\' ${barcode}_frozen_members_new.tsv > roles.tmp\nmv roles.tmp ${barcode}_frozen_members_new.tsv')
    elif number == 15:
        line = 'cp ${barcode}_new_vs_frozen ${barcode}_new_unassigned.fasta'
        change(line, line + '\nawk -v cid="\\$(awk \'NR==1{print \\$1}\' ${barcode}_frozen_members_new.tsv)" \'/^>/{print cid "\\t" substr(\\$0,2) "\\t0"}\' ${barcode}_new_unassigned.fasta >> ${barcode}_frozen_members_new.tsv')
    elif number == 16:
        change('cp ${barcode}_new_vs_frozen ${barcode}_new_unassigned.fasta', 'cp "\\$NEW_FASTA" ${barcode}_new_unassigned.fasta')
    else:
        raise ValueError(number)
    assert s != SOURCE
    return s


MUTATIONS = [
    (1, "live", "query-to-frozen mapping"),
    (2, "archive", "query-to-frozen mapping"),
    (3, "skips", "merged membership completeness"),
    (4, "first", "active membership persisted"),
    (5, "skips", "merged membership completeness"),
    (6, "failure", "failed publication must fail the task"),
    (7, "failure", "old state preserved before rename"),
    (8, "skips", "skipped round preserves active state"),
    (9, "changed", "merged membership completeness"),
    (10, "skips", "merged membership completeness"),
    (11, "empty", "merged membership completeness"),
    (12, "first", "merged membership completeness"),
    (13, "bootstrap", "bootstrap persists active state"),
    (14, "live", "query/reference representative roles"),
    (15, "live", "query-to-frozen mapping"),
    (16, "live", "unassigned conservation"),
]


@pytest.mark.parametrize("number,scenario,assertion", MUTATIONS, ids=[f"M{x[0]}" for x in MUTATIONS])
def test_semantic_mutation_kills(tmp_path, number, scenario, assertion):
    source = mutant(number)
    (tmp_path / "mutant.nf").write_text(source)
    # Only the named semantic assertion counts. Construction, shell syntax,
    # missing tools and unrelated exceptions do not satisfy the expected kill.
    with pytest.raises(AssertionError, match=assertion) as failure:
        if scenario in ("live", "archive"):
            check_assignment(tmp_path / "assignment", source, site=scenario)
        elif scenario == "first":
            check_first(tmp_path / "round", source)
        elif scenario == "skips":
            check_skips(tmp_path / "round", source)
        elif scenario == "failure":
            check_failure(tmp_path, "write", source)
        elif scenario == "changed":
            check_changed(tmp_path / "round", source)
        elif scenario == "empty":
            check_empty(tmp_path / "round", source)
        elif scenario == "bootstrap":
            check_bootstrap(tmp_path / "round", source)
    (tmp_path / "semantic-kill.txt").write_text(f"M{number}: {failure.value}\n")


@pytest.mark.parametrize("reverse", [False, True])
def test_sim2_old_direction_exposes_shadow_clusters(tmp_path, reverse):
    baseline = run(["git", "show", BASE + ":main.nf"], ROOT)
    assert baseline.returncode == 0
    env = baseline_selector(tmp_path)
    r = Round(tmp_path / "old", baseline.stdout, freeze=3)
    originals = [(ident("taxonA_rep"), dna(1021)), (ident("taxonB_rep"), dna(2021))]
    observed = []
    for n in range(1, 8):
        batch = originals.copy() if n == 1 else []
        for taxon, (_, seq) in enumerate(originals):
            batch.extend((ident(f"taxon{taxon}_r{n}_{k}"), variant(seq[:-2], n*17+k)) for k in range(3))
        if reverse:
            batch.reverse()
        r.good(batch, env=env)
        meta = rows(r.state / "otu_frozen_meta.tsv")
        observed.append((n, len(meta), len(sequences(r.pool))))
        if n == 4:
            assert len(sequences(r.pool)) == 6, "old direction leaves qualifying reads in shadow active clusters"
            # Here shorter queries also expose the old db1>=db2 length gate.
            assert len(oracle([(m[0], seq) for m, (_, seq) in zip(sorted(meta, key=lambda x: x[1]), originals)], batch)) == 6
        if n == 6:
            assert len(meta) == 4, "old direction freezes duplicate taxon OTUs"
    (tmp_path / "old-sim2-rounds.tsv").write_text("round\tfrozen_otus\tactive_reads\n" + "".join("\t".join(map(str, x)) + "\n" for x in observed))


@pytest.mark.parametrize("boundary", ["create", "write", "rename", "before_rename", "after_rename"])
def test_first_publication_failure_has_no_partial_final_state(tmp_path, boundary):
    r = Round(tmp_path / "run")
    batch = [(ident("first"), dna(910))]
    env = failure_env(tmp_path / "fault-bin", boundary)
    assert r.execute(batch, env=env).returncode != 0
    if boundary != "after_rename":
        assert not r.active.exists(), "failed first publication leaves no final state"
    else:
        assert {x[1] for x in rows(r.active)} == {batch[0][0]}
        assert not (r.work / MERGED).exists(), "interrupted before merged generation"
    r.good(batch)
    assert_complete(r.work / MERGED, [batch[0][0]])


# F-C1-01: compare against the exact reviewed candidate, not immutable HEAD.
def frozen_hash_block(source=SOURCE):
    a = source.index('\t\t\t\t\t# Frozen assignments are also decided hashes')
    b = source.index('\t\t\t\t\tif [ "${params.otu_commit_dropped_hashes}"', a)
    return source[a:b]


def reviewed_candidate():
    # This negative control is R3-C1 before F-C1-01, not the evolving candidate.
    historical = subprocess.check_output([
        "git", "-C", str(ROOT), "show",
        "842881dfa1e99e9dc8ac20658e88d1da7976c4a6:main.nf",
    ])
    assert hashlib.sha256(historical).hexdigest() == "a4fb19020bc244d3ddd55caee7ec1e6517c0aaa0e4038475b065169e572afefc", "immutable R3-C1 source hash"
    source = historical.decode("utf-8")
    start = '\t\t\t\t\t# Frozen assignments are also decided hashes'
    end = '\t\t\t\t\tif [ "${params.otu_commit_dropped_hashes}"'
    assert source.count(start) == source.count(end) == 1, "historical F-C1-01 anchors must be unique"
    assert source.index(start) < source.index(end), "historical F-C1-01 anchor order"
    block = frozen_hash_block(source)
    assert len(block.splitlines()) == 6, "historical F-C1-01 block must contain six lines"
    source = source.replace(block, "", 1)
    assert hashlib.sha256(source.encode()).hexdigest() == "88c6df06975c2b3aac9773d9e9457b6b8826e9363de10ad1f05477ace3e77de3"
    return source


def seqhash(sequence):
    return hashlib.md5(sequence.upper().encode()).hexdigest()


class RollingRound(Round):
    """Append to an actual persistent HQ FASTA; stage its full contents each round."""
    def __init__(self, root, source=SOURCE, freeze=99, reverse=False):
        super().__init__(root, source, freeze)
        self.hq = root / "rolling_hq.fasta"
        self.hq.touch()
        self.reverse = reverse

    def execute(self, incoming=(), **kwargs):
        with self.hq.open("a") as out:
            for name, sequence in incoming:
                out.write(f">{name}\n{sequence}\n")
        accumulated = list(sequences(self.hq).items())
        if self.reverse:
            accumulated.reverse()
        result = super().execute(accumulated, **kwargs)
        # The production section removes its staged HQ file, so preserve the
        # actual rolling input separately for audit and size assertions.
        shutil.copyfile(self.hq, self.work / "rolling_input.fasta")
        return result


def rolling_batches():
    refs, _ = fixture_reads()
    return refs, [[(ident(f"rolling.r{n}.a"), variant(refs[0][1], 40+n)),
                   (ident(f"rolling.r{n}.b"), variant(refs[1][1], 70+n)),
                   (ident(f"rolling.r{n}.active"), dna(8400+n))] for n in range(1, 4)]


def rolling_observation(r):
    return {
        "new": {seqhash(s) for s in sequences(r.work / "bc_otu_new_unique.fasta").values()},
        "new_ids": set(sequences(r.work / "bc_otu_new_unique.fasta")),
        "commit": (r.work / "bc_otu_new_hashes_to_commit.tsv").read_text().splitlines(),
        "seen": (r.state / "otu_seen_hashes.tsv").read_text().splitlines(),
        "frozen": (r.state / "otu_frozen_members.tsv").read_bytes(),
        "merged": (r.work / MERGED).read_bytes(),
        "pool": set(sequences(r.pool)),
        "accumulated": len(sequences(r.work / "rolling_input.fasta")),
    }


def assert_rolling_hashes(observation, batch, decided):
    hashes = {seqhash(s) for _, s in batch}
    assert observation["new"] == hashes - decided, "NEW_FASTA contains only newly undecided hashes"
    assert observation["new_ids"] == {name for name, _ in batch}, "processed frozen reads do not re-enter NEW_FASTA"
    assert observation["commit"] == sorted(hashes), "all mixed-round candidate hashes committed once in order"
    assert observation["seen"] == sorted(decided | hashes), "seen-hash growth includes frozen assignments"
    frozen_ids = {name for name, _ in batch[:2]}
    assert not (frozen_ids & observation["pool"]), "frozen reads excluded from active pool"


def rolling_run(root, source=SOURCE, reverse=False):
    refs, batches = rolling_batches()
    r = RollingRound(root, source, reverse=reverse)
    r.seed_frozen(refs)
    observations = []
    for batch in batches:
        r.good(batch)
        observations.append(rolling_observation(r))
    return r, batches, observations


@pytest.mark.parametrize("reverse", [False, True])
def test_frozen_hash_rolling_accumulation_and_reviewed_equivalence(tmp_path, reverse):
    r, batches, corrected = rolling_run(tmp_path / "corrected", reverse=reverse)
    _, _, reviewed = rolling_run(tmp_path / "reviewed", reviewed_candidate(), reverse)
    decided = set()
    for n, (batch, new, old) in enumerate(zip(batches, corrected, reviewed), 1):
        assert new["accumulated"] == old["accumulated"] == 3*n
        assert_rolling_hashes(new, batch, decided)
        assert new["frozen"] == old["frozen"], "frozen membership byte equivalence"
        assert new["merged"] == old["merged"], "merged cluster byte equivalence"
        decided.update(seqhash(s) for _, s in batch)
        assert len(new["seen"]) == 3*n
    # A truly no-new-read round after mixed rounds remains truly empty.
    before = (r.state / "otu_frozen_members.tsv").read_bytes()
    r.good()
    assert not sequences(r.work / "bc_otu_new_unique.fasta")
    assert (r.state / "otu_frozen_members.tsv").read_bytes() == before
    assert not (r.work / "bc_active_nr.fasta").exists()
    assert set((r.state / "otu_seen_hashes.tsv").read_text().splitlines()) == decided


def test_frozen_hash_reviewed_negative_control_and_order_invariance(tmp_path):
    _, batches, old = rolling_run(tmp_path / "reviewed", reviewed_candidate())
    assert [len(x["new"]) for x in old] == [3, 5, 7]
    assert [len(x["seen"]) for x in old] == [1, 2, 3]
    with pytest.raises(AssertionError, match="all mixed-round candidate hashes committed"):
        assert_rolling_hashes(old[0], batches[0], set())
    _, _, forward = rolling_run(tmp_path / "forward")
    _, _, reverse = rolling_run(tmp_path / "reverse", reverse=True)
    for a, b in zip(forward, reverse):
        for key in ("new", "new_ids", "commit", "seen", "pool"):
            assert a[key] == b[key], "rolling input-order invariance"
    (tmp_path / "growth.tsv").write_text(
        "round\taccumulated\treviewed_new\tcorrected_new\treviewed_seen\tcorrected_seen\n" +
        "".join(f"{n}\t{a['accumulated']}\t{len(a['new'])}\t{len(b['new'])}\t{len(a['seen'])}\t{len(b['seen'])}\n"
                for n, (a, b) in enumerate(zip(old, forward), 1)))


def test_frozen_hash_later_promotion_cannot_reassign_processed_read(tmp_path):
    refs, batches = rolling_batches()
    refseq = refs[0][1]
    query = refseq
    later = refseq
    for pos in range(12):
        later = variant(later, 10 + 21*pos)
        if pos < 6:
            query = variant(query, 10 + 21*pos)
    # The future representative is unmatched to the old ref, but overlaps the
    # already assigned query. Its greater length gives it precedence in real 2d.
    batches[0][0] = (ident("overlap.query"), query)
    batches[0][2] = (ident("later.rep"), later + "ACGT")
    results = {}
    for name, source in (("reviewed", reviewed_candidate()), ("corrected", SOURCE)):
        r = RollingRound(tmp_path / name, source, freeze=3)
        r.seed_frozen(refs)
        for batch in batches:
            r.good(batch)
        assert len(rows(r.state / "otu_frozen_meta.tsv")) == 3, "later representative really promoted"
        before = {row[0] for row in rows(r.state / "otu_frozen_members.tsv") if row[1] == ident("overlap.query")}
        assert before == {refs[0][0].split("|")[0]}
        r.good()
        after = {row[0] for row in rows(r.state / "otu_frozen_members.tsv") if row[1] == ident("overlap.query")}
        results[name] = after
    assert results["corrected"] == {refs[0][0].split("|")[0]}, "processed query cannot acquire a second frozen membership"
    assert len(results["reviewed"]) == 2, "negative control exhibits second frozen membership"


def frozen_hash_boundary(work, block=None, dropped=False, missing_map=False):
    """Exercise exact-ID/candidate intersection with deliberately extraneous rows."""
    work.mkdir(parents=True)
    hashes = {name: seqhash(dna(seed)) for name, seed in
              (("kept", 1), ("one", 2), ("two", 3), ("excluded", 4), ("history", 5), ("dropped", 6))}
    (work / "current.tsv").write_text("".join(f"{name}\t{h}\n" for name, h in hashes.items()))
    # Wrong hash map has full annotated IDs, as the accumulated-HQ map does.
    (work / "wrong.tsv").write_text("".join(f"{ident(name)}\t{h}\n" for name, h in hashes.items()))
    (work / "candidates").write_text("\n".join(hashes[x] for x in ("kept", "one", "two", "dropped")) + "\n")
    (work / "commit").write_text(hashes["kept"] + "\n")
    (work / "dropped").write_text(hashes["dropped"] + "\n")
    (work / "history.tsv").write_text(f"FROZEN_historical\t{ident('history')}\t{hashes['history']}\n")
    (work / "bc_frozen_members_new.tsv").write_text("".join(
        f"FROZEN_ref\t{ident(name)}\t0\n" for name in ("two", "one", "two", "excluded", "one.extra")))
    block = frozen_hash_block() if block is None else block
    if missing_map:
        (work / "current.tsv").unlink()
    prelude = '\n'.join(f'{name}={shlex.quote(str(work / path))}' for name, path in
                        (("NEW_BASE_HASH", "current.tsv"), ("HASH_MAP", "wrong.tsv"),
                         ("NEW_HASHES_CAND", "candidates"), ("NEW_HASHES_TO_COMMIT", "commit"),
                         ("NEW_HASHES_DROPPED", "dropped"), ("FROZEN_META", "history.tsv"))) + '\n'
    a = SOURCE.index('\t\t\t\t\tif [ "${params.otu_commit_dropped_hashes}"')
    b = SOURCE.index('\t\t\t\t\tcand_rows=', a)
    v = values(work)
    v['params.otu_commit_dropped_hashes'] = "true" if dropped else "false"
    result = shell(work, prelude + render(block + SOURCE[a:b], v))
    if missing_map:
        assert result.returncode != 0, "hash lookup failure must fail closed"
        return
    assert result.returncode == 0, result.stderr
    expected = sorted(hashes[x] for x in ("kept", "one", "two") + (("dropped",) if dropped else ()))
    assert (work / "commit").read_text().splitlines() == expected, "exact candidate hash set, sorted and unique"


@pytest.mark.parametrize("dropped", [False, True])
def test_frozen_hash_exact_intersection_and_dropped_policy(tmp_path, dropped):
    frozen_hash_boundary(tmp_path / "boundary", dropped=dropped)


def test_frozen_hash_lookup_failure_is_fatal(tmp_path):
    frozen_hash_boundary(tmp_path / "boundary", missing_map=True)


def frozen_hash_mutant(number):
    block = frozen_hash_block()
    replacement = block
    if number == 1:
        replacement = ""
    elif number == 2:
        replacement = block.replace('sub(/[|].*/,"",id); ', '')
    elif number == 3:
        replacement = block.replace('id=\\$2;', 'id=\\$1;')
    elif number == 4:
        replacement = block.replace('"\\$NEW_BASE_HASH"', '"\\$HASH_MAP"')
    elif number == 5:
        replacement = block.replace("| awk 'FILENAME==ARGV[1]{cand[\\$1]=1; next} (\\$1 in cand){print \\$1}' \"\\$NEW_HASHES_CAND\" -", '| cat')
    elif number == 6:
        replacement = block.replace('LC_ALL=C sort', 'awk -F \'\\t\' \'{print \\$3}\' "\\$FROZEN_META" >> "\\$NEW_HASHES_TO_COMMIT"\n\t\t\t\t\t\tLC_ALL=C sort')
    elif number == 7:
        replacement = '\n'.join(line for line in block.splitlines() if 'LC_ALL=C sort' not in line) + '\n'
    elif number == 8:
        s = SOURCE.replace(block, "", 1)
        anchor = '\n\t# -- §7: State persistence and cleanup --'
        return s.replace(anchor, '\n' + block + anchor, 1), block
    elif number == 9:
        replacement = block.replace('if [ -s ${barcode}_frozen_members_new.tsv ]; then', 'if [ -s ${barcode}_frozen_members_new.tsv ] && [ ! -s ${barcode}_new_unassigned.fasta ]; then')
    else:
        raise ValueError(number)
    assert replacement != block
    return SOURCE.replace(block, replacement, 1), replacement


@pytest.mark.parametrize("number", range(1, 10), ids=lambda n: f"FH{n}")
def test_frozen_hash_semantic_mutation_kills(tmp_path, number):
    source, block = frozen_hash_mutant(number)
    (tmp_path / "mutant.nf").write_text(source)
    expected = ("exact candidate hash set, sorted and unique" if number in (5, 6, 7) else
                "seen-hash growth includes frozen assignments" if number == 8 else
                "all mixed-round candidate hashes committed")
    with pytest.raises(AssertionError, match=expected) as failure:
        if number in (5, 6, 7):
            frozen_hash_boundary(tmp_path / "boundary", block)
        else:
            _, batches, observations = rolling_run(tmp_path / "rolling", source)
            assert_rolling_hashes(observations[0], batches[0], set())
    (tmp_path / "semantic-kill.txt").write_text(f"FH{number}: {failure.value}\n")


def test_frozen_hash_cached_nextflow_22108_execution(tmp_path):
    capsule = Path.home() / ".nextflow/capsule/apps/nextflow-all_22.10.8"
    if not (capsule / "nextflow-22.10.8.jar").is_file():
        pytest.skip("cached Nextflow 22.10.8 is unavailable; network/install forbidden")
    fixture = tmp_path / "inputs"
    frozen_hash_boundary(fixture)
    (fixture / "commit").write_text(seqhash(dna(1)) + "\n")
    a = SOURCE.index('\t\tif [ "\\$NEW_HASHES_COMMIT_OK" -eq 1 ]', SOURCE.index('# Regenerate every round:'))
    b = SOURCE.index('\n\t# -- §7:', a)
    script = '''nextflow.enable.dsl=1
def barcode = 'bc'
process HASH_DECISION {
    publishDir 'results', mode: 'copy'
    output:
    file 'commit' into committed
    file 'seen' into seen
    script:
    """
    set -euo pipefail
    ''' + f'cp "{fixture}"/* .\n' + r'''
    NEW_BASE_HASH=current.tsv
    NEW_HASHES_CAND=candidates
    NEW_HASHES_TO_COMMIT=commit
    NEW_HASHES_COMMIT_OK=1
    SEEN_HASHES=seen
    : > "\$SEEN_HASHES"
''' + frozen_hash_block() + SOURCE[a:b] + '\n    """\n}\n'
    (tmp_path / "probe.nf").write_text(script)
    (tmp_path / "nextflow.config").write_text("process.shell = ['/bin/bash', '-euo', 'pipefail']\n")
    packages = ('java.lang', 'java.io', 'java.nio', 'java.net', 'java.util',
                'java.util.concurrent.locks', 'java.util.concurrent.atomic', 'java.nio.file.spi',
                'sun.nio.ch', 'sun.nio.fs', 'sun.net.www.protocol.http', 'sun.net.www.protocol.https',
                'sun.net.www.protocol.ftp', 'sun.net.www.protocol.file', 'jdk.internal.misc', 'java.util.regex')
    java17 = Path('/Library/Java/JavaVirtualMachines/microsoft-17.jdk/Contents/Home/bin/java')
    java = str(java17) if java17.is_file() else shutil.which('java')
    command = [java, *[f'--add-opens=java.base/{p}=ALL-UNNAMED' for p in packages],
               '-cp', str(capsule / '*'), 'nextflow.cli.Launcher']
    env = dict(os.environ, NXF_OFFLINE='true', NXF_VER='22.10.8', NXF_DISABLE_CHECK_LATEST='true',
               NXF_ANSI_LOG='false', NXF_HOME=str(tmp_path / 'nxf-home'), NXF_TEMP=str(tmp_path / 'nxf-temp'))
    (tmp_path / 'nxf-temp').mkdir()
    version = run([*command, '-version'], tmp_path, env)
    assert version.returncode == 0 and '22.10.8' in version.stdout, version.stderr
    result = run([*command, '-log', str(tmp_path / 'nextflow.log'), 'run', 'probe.nf',
                  '-work-dir', str(tmp_path / 'work')], tmp_path, env)
    (tmp_path / 'execution.log').write_text(version.stdout + result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    expected = ''.join(h + '\n' for h in sorted(seqhash(dna(n)) for n in (1, 2, 3)))
    assert (tmp_path / 'results/commit').read_text() == expected, 'Nextflow committed candidate hashes'
    assert (tmp_path / 'results/seen').read_text() == expected, 'Nextflow persisted seen hashes'
