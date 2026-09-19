"""R3-C2: promotion is an NR relation transfer, not a second membership.

Truth fixtures contain distinct sequences with an unambiguous longest representative.
The Python oracle treats an OTU as that representative and counts relations separately
from sets. Tests execute extracted production shell, real repository helpers and
CD-HIT; the boundary fixtures isolate publication faults without mocking biology.
All generated files, including disposable mutants, belong under pytest's tmp_path.
"""
import ast
from collections import Counter, defaultdict
import hashlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("_rtb_c1_contract", ROOT / "tests/test_otu_frozen_assignment_round_contract.py")
c1 = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = c1
_spec.loader.exec_module(c1)
SOURCE = Path(os.environ.get("RTB_C2_SOURCE", ROOT / "main.nf")).read_text()
BASE = "842881dfa1e99e9dc8ac20658e88d1da7976c4a6"
BASE_SHA = "a4fb19020bc244d3ddd55caee7ec1e6517c0aaa0e4038475b065169e572afefc"
REVIEWED_SHA = "88c6df06975c2b3aac9773d9e9457b6b8826e9363de10ad1f05477ace3e77de3"


def baseline():
    data = subprocess.check_output(["git", "-C", str(ROOT), "show", BASE + ":main.nf"])
    assert hashlib.sha256(data).hexdigest() == BASE_SHA
    return data.decode()


def tsv(path):
    return [line.split("\t") for line in path.read_text().splitlines()]


def table_relations(rows):
    groups = defaultdict(list)
    for row in rows:
        assert len(row) in (3, 4), "membership schema"
        assert row[2] in ("0", "1"), "representative flag domain"
        groups[row[0]].append((row[1], row[2] == "1"))
    return group_relations(list(groups.values()))


def group_relations(groups):
    result = []
    for members in groups:
        reps = [member for member, flag in members if flag]
        assert len(reps) == 1, "exactly one representative per retained cluster"
        result.extend((reps[0], member) for member, _ in members)
    return result


def clstr_groups(path):
    """Independent token parser; no production regular expression is reused."""
    groups = []
    for line in path.read_text().splitlines():
        if line.startswith(">Cluster "):
            assert line == f">Cluster {len(groups)}", "merged cluster ordering"
            groups.append([])
        else:
            index, length, name, *flag = line.split()
            assert int(index) == len(groups[-1]) and length == "0nt,"
            assert name.startswith(">") and name.endswith("...")
            assert flag in ([], ["*"]), "merged representative schema"
            groups[-1].append((name[1:-3], bool(flag)))
    return groups


def expected_relations(families):
    return {(family[0][0], member) for family in families for member, _ in family}


def assert_unique(relations):
    counts = Counter(relations)
    duplicates = sum(n - 1 for n in counts.values())
    assert duplicates == 0, f"canonical relation uniqueness: {duplicates} duplicate relations"
    assert len({member for _, member in relations}) == len(relations), "one canonical OTU per NR member"


def exact_merged(frozen, active):
    """Documented synthetic .clstr schema, independently serialized in Python."""
    groups = defaultdict(list)
    for prefix, rows in (("F_", frozen), ("A_", active)):
        for cid, member, flag, *_ in rows:
            groups[prefix + cid].append((member, flag))
    lines = []
    for number, key in enumerate(sorted(groups)):
        lines.append(f">Cluster {number}\n")
        for index, (member, flag) in enumerate(groups[key]):
            lines.append(f"{index}\t0nt, >{member}... {'*' if flag == '1' else ''}\n")
    return "".join(lines).encode()


def assert_round(r, families, promoted_members=()):
    frozen = tsv(r.state / "otu_frozen_members.tsv")
    active = tsv(r.active)
    fr, ar = table_relations(frozen), table_relations(active)
    merged = group_relations(clstr_groups(r.work / c1.MERGED))
    assert_unique(merged)
    truth = expected_relations(families)
    assert set(merged) == truth, "canonical conservation and no invented membership"
    frozen_ids = {member: "FROZEN_" + c1.seqhash(family[0][1]) for family in families for member, _ in family}
    assert all(cid == frozen_ids[member] for cid, member, *_ in frozen), "exact frozen OTU identity from representative sequence"
    assert Counter(merged) == Counter(fr + ar), "merged equals frozen plus retained active"
    assert not (set(fr) & set(ar)), "frozen/active canonical disjointness"
    assert not ({m for _, m in fr} & {m for _, m in ar}), "frozen/active member disjointness"
    assert set(promoted_members) <= {m for _, m in fr}, "every promoted member transferred to frozen"
    assert not (set(promoted_members) & {m for _, m in ar}), "persisted active exclusion"
    assert set(c1.sequences(r.pool)) == {m for _, m in ar}, "active pool and membership agree"
    fingerprint = hashlib.sha256(r.pool.read_bytes()).hexdigest()
    assert all(row[3] == fingerprint for row in active), "post-promotion pool fingerprint"
    assert (r.work / c1.MERGED).read_bytes() == exact_merged(frozen, active), "exact every-round merged output"
    return {"rows": len(merged), "unique": len(set(merged)), "duplicates": len(merged)-len(set(merged)),
            "frozen": len(fr), "active": len(ar)}


def family(name, seed, count=20):
    seq = c1.dna(seed, 420)
    # Distinct NR sequences; the uniquely longest representative is input-order invariant.
    return [(c1.ident(name + ".rep"), seq)] + [
        (c1.ident(f"{name}.{n}"), c1.variant(seq[:400], 7 + n * 13)) for n in range(1, count)]


def promotion_run(root, source=SOURCE, reverse=False, mixed=False, multiple=False):
    families = [family("promote", 1701)]
    if multiple:
        families.append(family("promote2", 1801))
    retained = family("retained", 1901, 3) if mixed else []
    r = c1.Round(root, source, freeze=2)
    first = [item for f in families for item in f[:10]]
    second = [item for f in families for item in f[10:]] + retained
    order = lambda items: list(reversed(items)) if reverse else items
    r.good(order(first))
    assert_round(r, [f[:10] for f in families])
    r.good(order(second))
    return r, families + ([retained] if retained else []), {m for f in families for m, _ in f}


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("mixed,multiple", [(False, False), (True, False), (True, True)])
def test_corrected_promotion_and_every_round(tmp_path, reverse, mixed, multiple):
    r, families, promoted = promotion_run(tmp_path / "run", reverse=reverse, mixed=mixed, multiple=multiple)
    observation = assert_round(r, families, promoted)
    assert observation["rows"] == (40 if multiple else 20) + (3 if mixed else 0)
    raw = tsv(r.work / "bc_active_members.tsv")
    ids = (r.work / "bc_promoted_clusters.tsv").read_text().splitlines()
    assert ids and all(x.startswith("CLUST_") and x[6:].isdigit() for x in ids)
    assert set(ids) <= {row[0] for row in raw}, "promotion IDs are active TSV column 1"
    assert len(ids) == (2 if multiple else 1)
    retained = [row for row in raw if row[0] not in ids]
    assert [row[:3] for row in tsv(r.active)] == retained, "retained active order and flags"
    before = r.active.read_bytes()
    frozen_before = (r.state / "otu_frozen_members.tsv").read_bytes()
    (r.state / "qced_reads_nr.fasta.clstr").write_text(">Cluster 0\n0\t0nt, >POISON... *\n")
    r.good()
    assert not (r.work / "bc_otu_new_unique.fasta").read_bytes(), "no-addition NEW_FASTA is empty"
    assert r.active.read_bytes() == before, "skipped round retains active bytes"
    assert (r.state / "otu_frozen_members.tsv").read_bytes() == frozen_before
    assert not (r.work / "bc_active_nr.fasta").exists(), "skip avoids reclustering"
    assert_round(r, families, promoted)
    later = family("later", 1991, 2)
    r.freeze = 99
    r.good(list(reversed(later)) if reverse else later)
    assert_round(r, families + [later], promoted)
    (tmp_path / "promotion-counts.json").write_text(json.dumps(observation, sort_keys=True))


def test_immutable_c1_40_rows_20_unique_negative_control(tmp_path):
    r, families, promoted = promotion_run(tmp_path / "baseline", baseline())
    relations = group_relations(clstr_groups(r.work / c1.MERGED))
    assert len(relations) == 40 and len(set(relations)) == 20
    assert sum(n-1 for n in Counter(relations).values()) == 20
    with pytest.raises(AssertionError, match="canonical relation uniqueness: 20 duplicate relations"):
        assert_round(r, families, promoted)


@pytest.mark.parametrize("reverse", [False, True])
def test_no_promotion_byte_equivalence(tmp_path, reverse):
    data = family("control", 2201, 5) + family("other", 2202, 3)
    if reverse:
        data.reverse()
    runs = []
    for name, source in (("candidate", SOURCE), ("baseline", baseline())):
        r = c1.Round(tmp_path / name, source, freeze=99)
        r.good(data)
        runs.append((r.active.read_bytes(), (r.work / c1.MERGED).read_bytes(), r.pool.read_bytes()))
    assert runs[0] == runs[1], "non-promotion bytes equivalent to immutable R3-C1"


@pytest.mark.parametrize("state", ["frozen_only", "active_only", "empty"])
def test_membership_boundaries(tmp_path, state):
    r = c1.Round(tmp_path / "run", SOURCE)
    f = family("existing", 2301, 1)
    if state == "frozen_only":
        name, seq = f[0]
        r.seed_frozen([("FROZEN_" + c1.seqhash(seq) + "|" + name, seq)])
    r.good(f if state == "active_only" else [])
    assert_round(r, [] if state == "empty" else [f])
    r.good()
    assert_round(r, [] if state == "empty" else [f])


def publication(source=SOURCE):
    a = source.index('\t\t\t# Columns: active cluster ID, annotated read ID, representative flag, pool SHA-256.')
    b = source.index('\n\t\tfi\n\n\t\t# Regenerate every round:', a)
    c = source.index('\t\t# Regenerate every round:', b)
    d = source.index('\t\tif [ "\\$NEW_HASHES_COMMIT_OK"', c)
    return source[a:b] + "\n" + source[c:d]


def boundary_fixture(root, promoted=("CLUST_1",), reverse=False):
    root.mkdir(parents=True)
    state = root / "state"
    state.mkdir()
    # CLUST_1_extra is deliberately an opaque retained-cluster adversary.
    raw = [[cid, c1.ident(cid + "." + label), str(flag)]
           for cid in ("CLUST_1", "CLUST_10", "CLUST_1_extra")
           for label, flag in (("rep", 1), ("member", 0), ("distinct", 0))]
    if reverse:
        raw.reverse()
    text = "".join("\t".join(row) + "\n" for row in raw)
    (root / "bc_active_members.tsv").write_text(text)
    (root / "bc_promoted_clusters.tsv").write_text("".join(cid+"\n" for cid in promoted))
    # Independent transfer oracle supplies the state immediately after snapshot/pruning.
    frozen = [["FROZEN_" + row[0][6:], *row[1:]] for row in raw if row[0] in promoted]
    frozen_path = state / "otu_frozen_members.tsv"
    frozen_path.write_text("".join("\t".join(row)+"\n" for row in frozen))
    pool = state / "otu_active_pool.fasta"
    c1.fasta(pool, [(row[1], c1.dna(i+2500)) for i, row in enumerate(raw) if row[0] not in promoted])
    active = state / c1.MEMBERS
    active.write_text("".join("\t".join(row)+"\tprevious-fingerprint\n" for row in raw))
    (state / "qced_reads_nr.fasta.clstr").write_text(">Cluster 0\n0\t0nt, >POISON... *\n")
    return SimpleNamespace(root=root, state=state, active=active, pool=pool, raw=raw, frozen=frozen,
                           promoted=set(promoted), frozen_bytes=frozen_path.read_bytes())


def run_boundary(b, source=SOURCE, env=None):
    values = {"STATE_DIR": b.state, "FROZEN_MEMBERS": b.state / "otu_frozen_members.tsv",
              "ACTIVE_MEMBERS_STATE": b.active, "ACTIVE_POOL": b.pool}
    prelude = "\n".join(k+"="+shlex.quote(str(v)) for k, v in values.items()) + "\n"
    return c1.shell(b.root, prelude + c1.render(publication(source), c1.values(b.root)), env)


def check_boundary(b):
    frozen, active = tsv(b.state / "otu_frozen_members.tsv"), tsv(b.active)
    merged = group_relations(clstr_groups(b.root / c1.MERGED))
    assert_unique(merged)
    truth = table_relations(b.raw)
    assert set(merged) == set(truth), "canonical conservation and no invented membership"
    assert (b.state / "otu_frozen_members.tsv").read_bytes() == b.frozen_bytes, "frozen members preserved"
    kept = [row for row in b.raw if row[0] not in b.promoted]
    assert {tuple(row[:3]) for row in active} == {tuple(row) for row in kept}, "persisted active transfer and distinct relations"
    assert [row[:3] for row in active] == kept, "retained active deterministic order"
    assert not b.promoted.intersection(row[0] for row in active), "promoted active clusters excluded"
    assert not set(table_relations(frozen)).intersection(table_relations(active)), "frozen/active canonical disjointness"
    digest = hashlib.sha256(b.pool.read_bytes()).hexdigest()
    assert b.active.read_bytes() == "".join("\t".join(row)+"\t"+digest+"\n" for row in kept).encode(), "retained bytes and pool fingerprint"
    assert (b.root / c1.MERGED).read_bytes() == exact_merged(frozen, active), "exact merged relation output"


@pytest.mark.parametrize("promoted", [(), ("CLUST_1",), ("CLUST_1", "CLUST_1"), ("CLUST_1", "CLUST_10")])
@pytest.mark.parametrize("reverse", [False, True])
def test_exact_identifiers_order_and_repeat_records(tmp_path, promoted, reverse):
    b = boundary_fixture(tmp_path / "boundary", promoted, reverse)
    p = run_boundary(b)
    assert p.returncode == 0, p.stderr
    check_boundary(b)


@pytest.mark.parametrize("record", ["CLUST_9\n", "CLUST_1_extra\n", "CLUST_1\tmember\n", "CLUST_1 \n", "\n", "FROZEN_1\n", "CLUST_1\nCLUST_9\n", "CLUST_01\n"])
def test_bad_promotion_preserves_authority(tmp_path, record):
    check_bad_promotion(tmp_path, record)


def check_bad_promotion(root, record="CLUST_9\n", source=SOURCE):
    b = boundary_fixture(root / "boundary")
    (b.root / "bc_promoted_clusters.tsv").write_text(record)
    before, inode = b.active.read_bytes(), b.active.stat().st_ino
    p = run_boundary(b, source)
    assert p.returncode != 0, "unreconciled promotion must fail closed"
    assert b.active.read_bytes() == before and b.active.stat().st_ino == inode, "invalid filter preserves old authority"
    assert not (b.root / c1.MERGED).exists(), "failed filter cannot merge"
    return b


@pytest.mark.parametrize("damage", ["missing_promotion", "missing_member", "missing_rep", "two_reps", "bad_flag", "duplicate_relation"])
def test_unreconciled_membership_fails_closed(tmp_path, damage):
    b = boundary_fixture(tmp_path / "boundary")
    path = b.root / "bc_active_members.tsv"
    if damage == "missing_promotion":
        (b.root / "bc_promoted_clusters.tsv").unlink()
    elif damage == "missing_member":
        path.unlink()
    else:
        rows = [row[:] for row in b.raw]
        if damage == "missing_rep": rows[0][2] = "0"
        if damage == "two_reps": rows[1][2] = "1"
        if damage == "bad_flag": rows[1][2] = "2"
        if damage == "duplicate_relation": rows.append(rows[4])
        path.write_text("".join("\t".join(row)+"\n" for row in rows))
    before = b.active.read_bytes()
    p = run_boundary(b)
    assert p.returncode != 0, "malformed membership must fail closed"
    assert b.active.read_bytes() == before, "malformed membership preserves authoritative state"


def check_publication_failure(root, boundary, source=SOURCE):
    b = boundary_fixture(root / "fault")
    before, inode = b.active.read_bytes(), b.active.stat().st_ino
    env = c1.failure_env(root / "fault-bin", boundary)
    p = run_boundary(b, source, env)
    assert p.returncode != 0, "failed publication must fail the task"
    if boundary != "after_rename":
        assert b.active.read_bytes() == before, "old state preserved before rename"
        assert b.active.stat().st_ino == inode, "no premature authoritative replacement"
    else:
        expected = [row for row in b.raw if row[0] not in b.promoted]
        assert [row[:3] for row in tsv(b.active)] == expected, "complete transfer after rename"
        assert b.active.stat().st_ino != inode, "atomic replacement inode"
    # Retry the bounded transaction from the same completed frozen snapshot.
    p = run_boundary(b)
    assert p.returncode == 0, p.stderr
    check_boundary(b)
    clean = boundary_fixture(root / "clean")
    p = run_boundary(clean)
    assert p.returncode == 0, p.stderr
    assert b.active.read_bytes() == clean.active.read_bytes(), "publication retry convergence"
    assert (b.root / c1.MERGED).read_bytes() == (clean.root / c1.MERGED).read_bytes(), "merged retry convergence"


@pytest.mark.parametrize("boundary", ["create", "write", "rename", "before_rename", "after_rename"])
def test_atomic_promotion_publication_and_retry(tmp_path, boundary):
    check_publication_failure(tmp_path, boundary)


@pytest.mark.parametrize("boundary", ["create", "write", "rename", "before_rename", "after_rename"])
def test_real_promotion_failure_then_round_retry(tmp_path, boundary):
    f = family("retry", 2601)
    r = c1.Round(tmp_path / "fault", SOURCE, freeze=2)
    r.good(f[:10])
    old = r.active.read_bytes()
    p = r.execute(f[10:], env=c1.failure_env(tmp_path / "fault-bin", boundary))
    assert p.returncode != 0, "promotion failure is fatal"
    if boundary != "after_rename":
        assert r.active.read_bytes() == old, "old promotion state survives failure"
    # Re-present the same accumulated NR input; real seen-hash/assignment code decides it.
    r.good(f)
    assert_round(r, [f], [name for name, _ in f])
    clean = c1.Round(tmp_path / "clean", SOURCE, freeze=2)
    clean.good(f[:10])
    clean.good(f[10:])
    clean.good(f)
    for name in (c1.MEMBERS, "otu_frozen_members.tsv", "otu_seen_hashes.tsv", "otu_active_pool.fasta"):
        assert (r.state / name).read_bytes() == (clean.state / name).read_bytes(), "real round retry converges: " + name
    assert (r.work / c1.MERGED).read_bytes() == (clean.work / c1.MERGED).read_bytes()


def test_historical_helper_controls(tmp_path, monkeypatch):
    original = c1.reviewed_candidate()
    assert hashlib.sha256(original.encode()).hexdigest() == REVIEWED_SHA
    assert original != SOURCE, "historical helper cannot return current production source"
    current_copy = tmp_path / "main.nf"
    current_copy.write_text(SOURCE + "\n// comment-only working source\n")
    monkeypatch.setattr(c1, "SOURCE", current_copy.read_text())
    assert c1.reviewed_candidate() == original != c1.SOURCE
    # Every test body and all code outside the authorized helper stay byte-identical.
    path = ROOT / "tests/test_otu_frozen_assignment_round_contract.py"
    previous = subprocess.check_output(["git", "-C", str(ROOT), "show", BASE+":"+str(path.relative_to(ROOT))]).decode()
    def without_helper(text):
        node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == "reviewed_candidate")
        lines = text.splitlines(keepends=True)
        return "".join(lines[:node.lineno-1]+lines[node.end_lineno:])
    assert without_helper(previous) == without_helper(path.read_text()), "existing assertions and test bodies byte-identical"


@pytest.mark.parametrize("damage", ["metadata", "commit", "hash", "missing_anchor", "ambiguous_anchor"])
def test_historical_helper_fails_loudly(tmp_path, damage):
    body = inspect.getsource(c1.reviewed_candidate)
    namespace = dict(c1.__dict__)
    historical = baseline().encode()
    payload = None
    expected = AssertionError
    if damage == "metadata":
        namespace["ROOT"] = tmp_path
        expected = subprocess.CalledProcessError
    elif damage == "commit":
        body = body.replace(BASE, "0"*40)
        expected = subprocess.CalledProcessError
    elif damage == "hash":
        payload = historical + b"\n"
    else:
        anchor = b"\t\t\t\t\t# Frozen assignments are also decided hashes"
        payload = historical.replace(anchor, b"# missing", 1) if damage == "missing_anchor" else historical + anchor + b"\n"
        # Independently exercise the anchor guard after satisfying its separate hash gate.
        body = body.replace(BASE_SHA, hashlib.sha256(payload).hexdigest())
    exec(body, namespace)
    with pytest.raises(expected) as error:
        if payload is None:
            namespace["reviewed_candidate"]()
        else:
            with patch.object(c1.subprocess, "check_output", return_value=payload):
                namespace["reviewed_candidate"]()
    if damage == "hash": assert "immutable R3-C1 source hash" in str(error.value)
    if "anchor" in damage: assert "anchors must be unique" in str(error.value)


def test_fc101_semantic_controls_survive_helper_correction(tmp_path):
    # Positive current-source oracle, historical negative control, and current-source revert.
    for label, source, valid in (("current", SOURCE, True), ("historical", c1.reviewed_candidate(), False),
                                 ("current_revert", SOURCE.replace(c1.frozen_hash_block(SOURCE), "", 1), False)):
        _, batches, observed = c1.rolling_run(tmp_path / label, source)
        if valid:
            c1.assert_rolling_hashes(observed[0], batches[0], set())
        else:
            with pytest.raises(AssertionError, match="all mixed-round candidate hashes committed"):
                c1.assert_rolling_hashes(observed[0], batches[0], set())


MERGE = '${baseDir}/bin/otu_merge_clstr.pl "\\$FROZEN_MEMBERS" "\\$ACTIVE_MEMBERS_STATE" ${barcode}_qced_reads_nr.fasta.clstr'
FILTER = 'if (\\$1 in p) {found[\\$1]++; reps[\\$1]+=\\$3; next}'
PRINT = 'print \\$0, pool_hash'
END = 'END {for (id in p) if (!found[id] || reps[id] != 1) exit 1}'
RENAME = 'mv -f "\\$ACTIVE_MEMBERS_TMP" "\\$ACTIVE_MEMBERS_STATE"'


def membership_mutant(number):
    source = SOURCE
    changes = []
    def change(old, new):
        nonlocal source
        assert source.count(old) == 1, "unambiguous mutant construction"
        source = source.replace(old, new, 1)
        changes.append({"old": old, "new": new})
    if number == 1:
        a = source.index('\t\t\tif ! awk -v pool_hash=')
        b = source.index(' > "\\$ACTIVE_MEMBERS_TMP"', a)
        change(source[a:b], '\t\t\tif ! awk -v pool_hash="\\$ACTIVE_POOL_HASH" \'BEGIN{OFS="\\t"} {print \\$0, pool_hash}\' ${barcode}_active_members.tsv')
    elif number == 2:
        change(FILTER, FILTER.replace('if (\\$1 in p)', 'if (\\$2 in p)'))
    elif number == 3:
        change(FILTER, 'drop=0; for (id in p) if (index(\\$1,id)==1) drop=1; ' + FILTER.replace('(\\$1 in p)', '(drop)'))
    elif number == 4:
        change(PRINT, 'next')
    elif number == 5:
        change(FILTER, FILTER.replace('; next', ''))
        change(MERGE, ': > "\\$FROZEN_MEMBERS"\n' + MERGE)
    elif number == 6:
        change(PRINT, 'if (!kept[\\$1]++) ' + PRINT)
    elif number == 7:
        change(FILTER, FILTER.replace('; next', '; if (found[\\$1] > 1) next'))
    elif number == 8:
        change('|| ! ' + RENAME, '|| ! { LC_ALL=C sort -r "\\$ACTIVE_MEMBERS_TMP" -o "\\$ACTIVE_MEMBERS_TMP" && ' + RENAME + '; }')
    elif number == 9:
        change(PRINT, '\\$3=0; ' + PRINT)
    elif number == 10:
        change(END, 'END {}')
    elif number == 11:
        change('> "\\$ACTIVE_MEMBERS_TMP" \\\n', '> "\\$ACTIVE_MEMBERS_STATE" \\\n')
        change(RENAME, 'true')
    elif number == 12:
        change('if ! awk -v pool_hash=', 'if ! { awk -v pool_hash=')
        change('> "\\$ACTIVE_MEMBERS_TMP" \\\n', '> "\\$ACTIVE_MEMBERS_TMP" || true; } \\\n')
    elif number == 13:
        change('|| ! ' + RENAME, '|| ! { ' + RENAME + ' || true; }')
    elif number == 14:
        change(MERGE, 'cp "\\${STATE_DIR}/qced_reads_nr.fasta.clstr" ${barcode}_qced_reads_nr.fasta.clstr')
    elif number == 15:
        change(MERGE, 'cp "\\$ACTIVE_MEMBERS_STATE" filtered.tsv\n' +
               'awk -v pool_hash="\\$ACTIVE_POOL_HASH" \'BEGIN{OFS="\\t"} {print \\$0, pool_hash}\' ${barcode}_active_members.tsv > "\\$ACTIVE_MEMBERS_STATE"\n' +
               MERGE.replace('"\\$ACTIVE_MEMBERS_STATE"', 'filtered.tsv'))
    elif number == 16:
        change(MERGE, MERGE.replace('"\\$ACTIVE_MEMBERS_STATE"', '${barcode}_active_members.tsv'))
    elif number == 17:
        change(c1.frozen_hash_block(SOURCE), '')
    elif number == 18:
        change('p[id]=1', 'if (!(id in p)) np++; p[id]=1')
        change(FILTER, FILTER.replace('; next', '; if (np == 1) next'))
    else:
        raise ValueError(number)
    assert source != SOURCE
    return source, changes


MUTATIONS = [
    (1, "boundary", "canonical relation uniqueness", False, "omit promotion filtering"),
    (2, "boundary", "valid promotion publication succeeds", False, "filter member field instead of cluster field"),
    (3, "boundary", "canonical conservation", False, "prefix/substring cluster matching"),
    (4, "boundary", "canonical conservation", False, "remove retained active rows"),
    (5, "boundary", "frozen members preserved", False, "clear frozen membership and retain promoted active rows"),
    (6, "boundary", "canonical conservation", False, "collapse distinct retained relations"),
    (7, "boundary", "canonical relation uniqueness", False, "retain one promoted representative relation"),
    (8, "boundary", "retained active deterministic order", False, "reverse-sort retained active rows"),
    (9, "representative", "representative identity", True, "clear retained representative flags"),
    (10, "unknown", "unreconciled promotion must fail closed", False, "tolerate unknown promoted IDs"),
    (11, "write", "old state preserved before rename", True, "write authoritative state before filtering completes"),
    (12, "write", "failed publication must fail the task", True, "ignore temporary write failure"),
    (13, "rename", "failed publication must fail the task", True, "ignore rename failure"),
    (14, "stale", "merged membership completeness", True, "read previous merged .clstr as authority"),
    (15, "boundary", "persisted active transfer", False, "filter merge only while persisting stale active state"),
    (16, "boundary", "canonical relation uniqueness", False, "persist filtered state but merge stale local rows"),
    (17, "hash", "all mixed-round candidate hashes committed", True, "undo six-line F-C1-01 insertion"),
    (18, "multi", "canonical relation uniqueness", False, "skip filtering with multiple promotions"),
]


@pytest.mark.parametrize("number,scenario,assertion,preexisting,description", MUTATIONS,
                         ids=[f"PM{x[0]}" for x in MUTATIONS])
def test_semantic_mutation_kills(tmp_path, number, scenario, assertion, preexisting, description):
    source, changes = membership_mutant(number)
    (tmp_path / "mutant.nf").write_text(source)
    with pytest.raises(AssertionError, match=assertion) as killed:
        if scenario in ("write", "rename"):
            c1.check_failure(tmp_path, scenario, source)
        elif scenario == "unknown":
            check_bad_promotion(tmp_path, source=source)
        elif scenario == "hash":
            _, batches, observed = c1.rolling_run(tmp_path / "rolling", source)
            c1.assert_rolling_hashes(observed[0], batches[0], set())
        else:
            b = boundary_fixture(tmp_path / "boundary", ("CLUST_1", "CLUST_10") if scenario == "multi" else ("CLUST_1",))
            p = run_boundary(b, source)
            assert "syntax error" not in p.stderr.lower(), p.stderr
            assert p.returncode == 0, "valid promotion publication succeeds: " + p.stderr
            if scenario in ("representative", "stale"):
                c1.assert_complete(b.root / c1.MERGED, [row[1] for row in b.raw])
            check_boundary(b)
    (tmp_path / "semantic-kill.json").write_text(json.dumps({
        "id": f"PM{number}", "description": description, "constructed": True,
        "exact_changes": changes, "assertion": str(killed.value),
        "invariant_asserted_before_R3_C2": preexisting,
    }, indent=2) + "\n")


def test_corrected_c1_positive_test_kills_current_fc101_revert(tmp_path):
    # Run the original protected positive test body against a disposable mutant.
    # Only its current-source input is substituted; its historical helper and every
    # assertion remain the corrected module's literal code.
    mutant = SOURCE.replace(c1.frozen_hash_block(SOURCE), "", 1)
    production = tmp_path / "mutant.nf"
    production.write_text(mutant)
    module = (ROOT / "tests/test_otu_frozen_assignment_round_contract.py").read_text()
    module = module.replace('ROOT = Path(__file__).resolve().parents[1]', f'ROOT = Path({str(ROOT)!r})', 1)
    module = module.replace('SOURCE = (ROOT / "main.nf").read_text()', f'SOURCE = Path({str(production)!r}).read_text()', 1)
    (tmp_path / "test_protected.py").write_text(module)
    result = c1.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                     str(tmp_path / "test_protected.py") + "::test_frozen_hash_rolling_accumulation_and_reviewed_equivalence[False]",
                     "--basetemp=" + str(tmp_path / "pytest")], cwd=tmp_path,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
    (tmp_path / "semantic-failure.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 1, "protected scientific test must fail on current F-C1-01 revert"
    assert "all mixed-round candidate hashes committed once in order" in result.stdout
    assert "1 failed" in result.stdout and "ERROR collecting" not in result.stdout


def test_reverse_input_canonical_sets(tmp_path):
    observations = []
    for reverse in (False, True):
        r, families, promoted = promotion_run(tmp_path / str(reverse), reverse=reverse, mixed=True, multiple=True)
        assert_round(r, families, promoted)
        observations.append(set(group_relations(clstr_groups(r.work / c1.MERGED))))
    assert observations[0] == observations[1], "reversed eligible input preserves canonical relation sets"


def test_cached_nextflow_22108_real_bounded_otu_definition(tmp_path):
    capsule = Path.home() / ".nextflow/capsule/apps/nextflow-all_22.10.8"
    if not (capsule / "nextflow-22.10.8.jar").is_file():
        pytest.skip("cached Nextflow 22.10.8 unavailable; installation forbidden")
    r = c1.Round(tmp_path / "state-root", SOURCE, freeze=2)
    f = family("nextflow", 2801)
    r.good(f[:10])
    incoming = tmp_path / "incoming.fasta"
    c1.fasta(incoming, f[10:])
    variables = {"STATE_DIR": r.state, "FROZEN_META": r.state / "otu_frozen_meta.tsv",
                 "FROZEN_REPS": r.state / "otu_frozen_reps.fasta", "FROZEN_HIST": r.state / "otu_frozen_history.tsv",
                 "FROZEN_MEMBERS": r.state / "otu_frozen_members.tsv", "FROZEN_MEMBERS_SEEN": r.state / "otu_frozen_members_seen.tsv",
                 "ACTIVE_POOL": r.pool, "ACTIVE_MEMBERS_STATE": r.active, "SEEN_HASHES": r.state / "otu_seen_hashes.tsv",
                 "THREADS": 1, "FROZEN_ENABLED": 1, "OTU_ID_MODE": "strict", "FROZEN_DB_ONLY_POLICY": "auto", "ROUND_ID": "bc_2"}
    prelude = "\n".join(k+"="+shlex.quote(str(v)) for k,v in variables.items()) + "\n"
    a = SOURCE.index('\t\tHASH_MAP="${barcode}_otu_hash_map.tsv"', SOURCE.index('process OTU_definition'))
    b = SOURCE.index('\t\trtbioscan_round_lock_unpin', a)
    # Preserve the real GString escaping; only bind declared Groovy fixture values.
    body = c1.re.sub(r"(?<!\\)\$\{([^}]+)\}", lambda m: str(c1.values(r.root, 2, 2)[m[1]]), SOURCE[a:b])
    probe = '''nextflow.enable.dsl=1
process OTU_DEFINITION_BOUNDED {
    publishDir 'results', mode: 'copy'
    output:
    file 'bc_qced_reads_nr.fasta.clstr' into merged
    script:
    """
    set -euo pipefail
''' + prelude + f'cp "{incoming}" bc_qced_reads_hq_accumulated.fasta\n' + body + '\n    """\n}\n'
    (tmp_path / "probe.nf").write_text(probe)
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
    version = c1.run([*command, '-version'], tmp_path, env)
    assert version.returncode == 0 and '22.10.8' in version.stdout, version.stderr
    result = c1.run([*command, '-log', str(tmp_path / 'nextflow.log'), 'run', 'probe.nf',
                     '-work-dir', str(tmp_path / 'work')], tmp_path, env)
    (tmp_path / 'execution.log').write_text(version.stdout + result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    commands = list((tmp_path / 'work').glob('*/*/.command.sh'))
    assert len(commands) == 1, "one bounded real Nextflow task"
    assert c1.run(['/bin/bash', '-n', commands[0]]).returncode == 0, "Bash 3.2 rendered task syntax"
    r.work = tmp_path / "results"
    assert_round(r, [f], [name for name, _ in f])
