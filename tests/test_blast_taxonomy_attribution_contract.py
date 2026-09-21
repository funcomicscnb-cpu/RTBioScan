"""R4-A analytical fixtures; expected biology is independent of Perl selection.

The strongest evidence is manufactured explicitly. Serialization/checksum helpers
implement only the wire format, never the production selection or lineage rules.
"""
import hashlib
import itertools
import json
import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "bin/cache_blast_by_hash.pl"
DEPTH = ROOT / "bin/get_blast_taxdepth.pl"
SEQ = "ACGT" * 25
HASH = hashlib.md5(SEQ.encode()).hexdigest()
SIG = "a" * 64


def run(*args, cwd, env=None, ok=True):
    proc = subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=45)
    if ok:
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0, proc.stdout
    return proc


def seal(kind, rows, sig=SIG):
    body = "".join(row + "\n" for row in rows)
    return (f"#RTB-R4-{kind}\t1\t{sig}\n{body}#END\t{len(rows)}\t"
            f"{hashlib.sha256(body.encode()).hexdigest()}\n")


def records(path):
    return [line.split("\t") for line in path.read_text().splitlines()[1:-1]]


def hsp(subject="acc.alpha", tax="40", bits="200", ev="1e-30", length="100",
        pid="99", start="1", end="100", hash_=HASH, qlen="100"):
    return "\t".join([hash_, subject, tax, ev, length, pid, bits, start, end,
                       start, end, qlen])


def select(tmp_path, rows, *, old=None, sig=SIG, env=None, fa=None):
    tmp_path.mkdir(exist_ok=True)
    fasta = tmp_path / "reads.fa"
    fasta.write_text(fa or f">q|ITS2|opaque\n{SEQ}\n")
    cache = tmp_path / "cache"
    if old is not None:
        cache.write_text(old)
    prefix = tmp_path / "pending"
    run(CACHE, "--prepare", fasta, cache, sig, prefix, cwd=tmp_path, env=env)
    raw = tmp_path / "raw.tsv"
    raw.write_text("".join(row + "\n" for row in rows))
    run(CACHE, "--complete", fasta, str(prefix) + ".snapshot", sig, raw, prefix,
        cwd=tmp_path, env=env)
    return prefix


def depth(tmp_path, evidence, *, cache_text=None, sig=SIG, env=None):
    previous = tmp_path / "memtax"
    if cache_text is not None:
        previous.write_text(cache_text)
    return run(DEPTH, evidence, "98", "95", previous, sig, tmp_path / "next-memtax",
               cwd=tmp_path, env=env)


def tiny_taxdump(path):
    path.mkdir(exist_ok=True)
    nodes = [(1, 1, "no rank"), (10, 1, "order"), (20, 10, "family"),
             (30, 20, "genus"), (40, 30, "species"), (41, 40, "subspecies"),
             (42, 40, "no rank")]
    (path / "nodes.dmp").write_text("".join(
        "\t|\t".join(map(str, [i, parent, rank, "", 0, 0, 1, 0, 0, 0, 0, 0, ""])) + "\t|\n"
        for i, parent, rank in nodes))
    (path / "names.dmp").write_text("".join(
        f"{i}\t|\tTaxon{i}\t|\t\t|\tscientific name\t|\n" for i, _, _ in nodes))
    (path / "merged.dmp").write_text("50\t|\t41\t|\n")
    (path / "delnodes.dmp").write_text("60\t|\n")
    return dict(os.environ, TAXONKIT_DB=str(path))


@pytest.mark.parametrize("reverse", [False, True])
def test_best_hsp_and_unique_subject_analytical_oracle(tmp_path, reverse):
    # Strongest bitscore deliberately has neither best identity nor longest HSP.
    rows = [hsp(bits="400", pid="98", length="90", end="90"),
            hsp(bits="200", pid="100", start="2"),
            hsp("acc.other", "30", bits="300", length="150", pid="99")]
    p = select(tmp_path, rows[::-1] if reverse else rows)
    data = records(Path(str(p) + ".evidence"))
    assert len(data) == 1
    assert data[0][2:4] == ["acc.alpha", "40"]
    assert float(data[0][7]) == 400
    assert float(data[0][6]) == 98
    assert data[0][-1] == "UNIQUE"


@pytest.mark.parametrize("taxids", [("40", "40"), ("40", "30"), ("40", "-123")])
@pytest.mark.parametrize("seed", ["0", "1", "12345"])
def test_equal_best_lossless_in_both_orders(tmp_path, taxids, seed):
    rows = [hsp("z|opaque|acc", taxids[0]), hsp("a|opaque|acc.10", taxids[1])]
    output = []
    for index, rr in enumerate([rows, rows[::-1]]):
        p = select(tmp_path / str(index), rr, env=dict(os.environ, PERL_HASH_SEED=seed,
                                                     PERL_PERTURB_KEYS="2"))
        path = Path(str(p) + ".evidence")
        output.append(path.read_bytes())
        data = records(path)
        assert len(data) == 2
        assert [x[2] for x in data] == ["a|opaque|acc.10", "z|opaque|acc"]
        assert {x[3] for x in data} == set(taxids)
        assert {x[-1] for x in data} == {"DEFERRED_TIE"}
        memory = seal("MEMTAX", ["30\tNA\t30\t20\t10\tgenus",
                                 "40\t40\t30\t20\t10\tspecies"])
        result = depth(path.parent, path, cache_text=memory)
        assert result.stdout == "q|ITS2|opaque;NA;1e-30;100;99.0\n"
    assert output[0] == output[1]


def test_single_hit_duplicates_and_cache_only_resume(tmp_path):
    p = select(tmp_path, [hsp(), hsp()])
    first = Path(str(p) + ".cache").read_bytes()
    (tmp_path / "cache").write_bytes(first)
    evidence = Path(str(p) + ".evidence").read_bytes()
    p = select(tmp_path, [])
    assert Path(str(p) + ".fasta").read_bytes() == b""
    assert Path(str(p) + ".cache").read_bytes() == first
    assert Path(str(p) + ".evidence").read_bytes() == evidence


@pytest.mark.parametrize("bad", ["fields", "nan", "infinite", "negative_length", "unknown_query",
                                  "wrong_qlen", "conflicting_duplicate", "conflicting_taxid", "no_newline"])
def test_incomplete_or_conflicting_blast_is_rejected_before_publication(tmp_path, bad):
    fasta = tmp_path / "reads.fa"; fasta.write_text(f">q|ITS2\n{SEQ}\n")
    cache = tmp_path / "cache"; original = seal("CACHE", [])
    cache.write_text(original); prefix = tmp_path / "p"
    run(CACHE, "--prepare", fasta, cache, SIG, prefix, cwd=tmp_path)
    row = hsp()
    if bad == "fields": row = "\t".join(row.split("\t")[:5])
    elif bad == "nan": row = hsp(bits="NaN")
    elif bad == "infinite": row = hsp(ev="1e9999")
    elif bad == "negative_length": row = hsp(length="-1")
    elif bad == "unknown_query": row = hsp(hash_="f" * 32)
    elif bad == "wrong_qlen": row = hsp(qlen="99")
    elif bad == "conflicting_duplicate": row += "\n" + hsp(bits="100")
    elif bad == "conflicting_taxid": row += "\n" + hsp(tax="30", start="2")
    raw = tmp_path / "raw"; raw.write_text(row + ("" if bad == "no_newline" else "\n"))
    run(CACHE, "--complete", fasta, str(prefix) + ".snapshot", SIG, raw, prefix,
        cwd=tmp_path, ok=False)
    assert cache.read_text() == original
    assert not Path(str(prefix) + ".cache").exists()


@pytest.mark.parametrize("bad", ["truncated", "checksum", "version", "numeric", "mapping", "duplicate"])
def test_cache_validation_even_when_signature_is_stale(tmp_path, bad):
    row = "H\t" + hsp()
    rows = ["Q\t" + HASH, "M\tq|ITS2\t" + HASH, row]
    if bad == "numeric": rows[-1] = "H\t" + hsp(pid="NaN")
    elif bad == "mapping": rows[1] = "M\tq|ITS2\t" + "f" * 32
    elif bad == "duplicate": rows += ["H\t" + hsp(bits="20")]
    text = seal("CACHE", rows)
    if bad == "truncated": text = text[:-1]
    elif bad == "checksum": text = text.replace("acc.alpha", "acc.other")
    elif bad == "version": text = text.replace("CACHE\t1", "CACHE\t99")
    cache = tmp_path / "cache"; cache.write_text(text)
    fasta = tmp_path / "fa"; fasta.write_text(f">q|ITS2\n{SEQ}\n")
    run(CACHE, "--prepare", fasta, cache, "b" * 64, tmp_path / "p", cwd=tmp_path, ok=False)
    assert cache.read_text() == text


def test_legacy_rebuild_once_and_empty_blast_negative_cache(tmp_path):
    old = HASH + "\tacc.alpha\t1e-20\t100\t99\n"
    p = select(tmp_path, [], old=old)
    assert Path(str(p) + ".fasta").stat().st_size > 0
    new = Path(str(p) + ".cache").read_text()
    assert records(Path(str(p) + ".evidence"))[0][-1] == "NO_HIT"
    p = select(tmp_path, [], old=new)
    assert Path(str(p) + ".fasta").stat().st_size == 0
    assert Path(str(p) + ".cache").read_text() == new


def test_crlf_cache_and_fasta_without_final_newline(tmp_path):
    p = select(tmp_path, [hsp()], fa=f">q|ITS2\r\n{SEQ}")
    text = Path(str(p) + ".cache").read_text()
    p = select(tmp_path, [], old=text.replace("\n", "\r\n"), fa=f">q|ITS2\r\n{SEQ}")
    assert Path(str(p) + ".cache").read_text() == text
    assert Path(str(p) + ".fasta").stat().st_size == 0


def test_missing_snapshot_and_unreadable_cache_directory(tmp_path):
    fasta = tmp_path / "fa"; fasta.write_text(f">q|ITS2\n{SEQ}\n")
    cache = tmp_path / "cache"; cache.mkdir()
    run(CACHE, "--prepare", fasta, cache, SIG, tmp_path / "p", cwd=tmp_path, ok=False)
    raw = tmp_path / "raw"; raw.write_text("")
    run(CACHE, "--complete", fasta, tmp_path / "absent", SIG, raw, tmp_path / "p", cwd=tmp_path, ok=False)


def test_empty_and_signed_batches_do_not_start_resolver(tmp_path):
    # This executable is a tripwire, not a fake claiming taxonomy correctness.
    fake = tmp_path / "bin"; fake.mkdir()
    tripwire = fake / "taxonkit"
    tripwire.write_text("#!/bin/sh\ntouch resolver-started\nexit 91\n"); tripwire.chmod(0o755)
    env = tiny_taxdump(tmp_path / "tripwire-taxonomy")
    env["PATH"] = str(fake) + os.pathsep + os.environ["PATH"]
    for tax in [None, "0", "-123"]:
        case = tmp_path / str(tax); case.mkdir()
        p = select(case, [] if tax is None else [hsp(tax=tax)])
        result = depth(case, Path(str(p) + ".evidence"), env=env)
        expected = "" if tax is None else f"q|ITS2|opaque;{'NA'};1e-30;100;99.0\n"
        assert result.stdout == expected
        assert not (case / "resolver-started").exists()


@pytest.mark.parametrize("tax,expected", [("40", "40"), ("41", "40"), ("42", "40"),
    ("50", "40"), ("30", "30"), ("20", "20"), ("10", "10"), ("99999999", "NA"), ("60", "NA")])
def test_real_taxonkit_rank_identity_fresh_reload_restart(tmp_path, tax, expected):
    assert shutil.which("taxonkit"), "Real taxonkit required; do not substitute a fake"
    env = tiny_taxdump(tmp_path / "taxonomy")
    p = select(tmp_path, [hsp(tax=tax)])
    evidence = Path(str(p) + ".evidence")
    first = depth(tmp_path, evidence, env=env)
    memory = (tmp_path / "next-memtax").read_text()
    second = depth(tmp_path, evidence, env=env, cache_text=memory)
    assert first.stdout == second.stdout == f"q|ITS2|opaque;{expected};1e-30;100;99.0\n"
    assert (tmp_path / "next-memtax").read_text() == memory


def test_taxonomy_signatures_invalidate_reference_and_taxdump(tmp_path):
    env = tiny_taxdump(tmp_path / "tax")
    db = tmp_path / "reference"; Path(str(db) + ".nsq").write_bytes(b"reference-v1")
    def signature():
        return run(CACHE, "--signature", db, env["TAXONKIT_DB"], "thresholds=98/95/92", cwd=tmp_path).stdout.split("\t")[0]
    first = signature()
    stat = (tmp_path / "tax/nodes.dmp").stat()
    with (tmp_path / "tax/nodes.dmp").open("a") as f: f.write("\n")
    os.utime(tmp_path / "tax/nodes.dmp", ns=(stat.st_atime_ns, stat.st_mtime_ns))
    tax_changed = signature(); assert tax_changed != first
    Path(str(db) + ".nsq").write_bytes(b"reference-v2")
    ref_changed = signature(); assert ref_changed != tax_changed
    p = select(tmp_path, [hsp()], sig=first)
    old = Path(str(p) + ".cache").read_text()
    p = select(tmp_path, [hsp()], sig=ref_changed, old=old)
    assert Path(str(p) + ".fasta").stat().st_size > 0
    # Wrong resolved species in stale memory MUST be rebuilt from real taxonomy.
    result = depth(tmp_path, Path(str(p) + ".evidence"), sig=ref_changed, env=env,
                   cache_text=seal("MEMTAX", ["40\t999\t30\t20\t10\tspecies"], first))
    assert ";40;" in result.stdout


def test_authorized_state_merge_replaces_stale_active_target(tmp_path):
    old = tmp_path / "old"; new = tmp_path / "new"; targets = tmp_path / "targets"
    old.write_text("q|ITS2;222;1e-20;150;100\nz|COI;55;1e-10;100;99\n")
    new.write_text("q|ITS2;111;1e-100;400;98\n")
    targets.write_text("ITS2\n")
    result = run(CACHE, "--merge", old, new, targets, cwd=tmp_path)
    assert result.stdout == "q|ITS2;111;1e-100;400;98\nz|COI;55;1e-10;100;99\n"
    new.write_text("")
    assert run(CACHE, "--merge", old, new, targets, cwd=tmp_path).stdout == "z|COI;55;1e-10;100;99\n"


@pytest.mark.parametrize("rows,expected", [
    ([hsp(bits="200", ev="1e-20"), hsp("b", bits="200", ev="1e-40")], "b"),
    ([hsp(length="90", end="90"), hsp("b", length="100")], "b"),
    ([hsp(pid="98"), hsp("b", pid="99")], "b"),
])
def test_all_biological_tiebreakers(tmp_path, rows, expected):
    for i, rr in enumerate([rows, rows[::-1]]):
        p = select(tmp_path / str(i), rr)
        data = records(Path(str(p) + ".evidence"))
        assert len(data) == 1 and data[0][2] == expected


def test_foldback_chimera_selected_hsp_controls_depth(tmp_path):
    # Two alignments to the same subject: long 96% vs short 100% fold-back.
    # The selected 96% HSP must resolve at genus, both fresh and cached.
    rows = [hsp(bits="350", pid="96", length="90", end="90"),
            hsp(bits="120", pid="100", length="55", start="46", end="100")]
    memory = seal("MEMTAX", ["40\t40\t30\t20\t10\tspecies"])
    outputs = []
    for i, rr in enumerate([rows, rows[::-1]]):
        case = tmp_path / str(i); p = select(case, rr)
        evidence = Path(str(p) + ".evidence")
        fresh = depth(case, evidence, cache_text=memory).stdout
        old = Path(str(p) + ".cache").read_text()
        p = select(case, [], old=old)
        cached = depth(case, Path(str(p) + ".evidence"), cache_text=memory).stdout
        assert fresh == cached == "q|ITS2|opaque;30;1e-30;90;96.0\n"
        outputs.append(cached)
    assert outputs[0] == outputs[1]


def test_query_aliases_share_hash_and_conflicting_ids_fail(tmp_path):
    fa = f">q|ITS2\n{SEQ}\n>other|ITS2\n{SEQ}\n"
    p = select(tmp_path, [hsp()], fa=fa)
    assert Path(str(p) + ".fasta").read_text().count(">") == 1
    data = records(Path(str(p) + ".evidence"))
    assert {row[0] for row in data} == {"q|ITS2", "other|ITS2"}
    assert {row[1] for row in data} == {HASH}
    reverse = select(tmp_path / "reversed", [hsp()], fa=f">other|ITS2\n{SEQ}\n>q|ITS2\n{SEQ}\n")
    assert Path(str(reverse) + ".evidence").read_bytes() == Path(str(p) + ".evidence").read_bytes()
    (tmp_path / "bad.fa").write_text(f">q|ITS2\n{SEQ}\n>q|ITS2\nAAAA\n")
    run(CACHE, "--prepare", tmp_path / "bad.fa", tmp_path / "cache", SIG,
        tmp_path / "bad", cwd=tmp_path, ok=False)


def test_real_blast_ties_and_reversed_database(tmp_path):
    assert shutil.which("blastn") and shutil.which("makeblastdb")
    rng = random.Random(42001)
    seq = "".join(rng.choice("ACGT") for _ in range(400))
    hash_ = hashlib.md5(seq.encode()).hexdigest()
    query = tmp_path / "query.fa"; query.write_text(f">{hash_}\n{seq}\n")
    outputs = []
    for order, names in enumerate([("acc.alpha", "acc.beta"), ("acc.beta", "acc.alpha")]):
        fasta = tmp_path / f"db{order}.fa"
        fasta.write_text("".join(f">{name}\n{seq}\n" for name in names))
        mapping = tmp_path / f"map{order}"
        mapping.write_text("acc.alpha\t40\nacc.beta\t30\n")
        db = tmp_path / f"db{order}"
        run("makeblastdb", "-in", fasta, "-dbtype", "nucl", "-parse_seqids", "-taxid_map", mapping,
            "-out", db, cwd=tmp_path)
        args = ["blastn", "-query", query, "-db", db, "-task", "megablast", "-dust", "no",
                "-perc_identity", "92", "-evalue", "11", "-max_hsps", "50", "-word_size", "50",
                "-qcov_hsp_perc", "50", "-outfmt",
                "6 qseqid sseqid staxids evalue length pident bitscore qstart qend sstart send qlen"]
        raw = run(*args, "-max_target_seqs", "2", cwd=tmp_path).stdout.splitlines()
        assert len(raw) == 2
        p = select(tmp_path / str(order), raw, fa=f">q|ITS2\n{seq}\n")
        outputs.append(Path(str(p) + ".evidence").read_bytes())
        assert {r[-1] for r in records(Path(str(p) + ".evidence"))} == {"DEFERRED_TIE"}
    assert outputs[0] == outputs[1]


def helper_environment(tmp_path):
    # Reuse only fixture setup, never production selection logic or expectations.
    import runpy
    return runpy.run_path(str(ROOT / "tests/test_blast_otu_pretax_helper.py"))["_prepare_fake_environment"](tmp_path)


def run_shell(tmp_path, env, base, db, taxdb, *, ok=True, targets="COI"):
    return run("/bin/bash", ROOT / "bin/blast_otu_pretax.sh", "bc", tmp_path / "reads.fa",
               tmp_path / "state", "2", base, str(db) + "/", taxdb, tmp_path / "round",
               targets, "coi_db" if targets == "COI" else "coi_db|its_db", "92|92", "95|95", "98|98",
               "memtax1.txt|memtax2.txt", "11", "50", cwd=tmp_path, env=env, ok=ok)


def test_shell_partial_failure_preserves_all_targets_and_retry_converges(tmp_path):
    env, base, db, taxdb = helper_environment(tmp_path)
    reads = ">q|COI|opaque\nACGT\n>r|ITS2|opaque\nTGCA\n"
    (tmp_path / "reads.fa").write_text(reads)
    run_shell(tmp_path, env, base, db, taxdb, targets="COI|ITS2")
    before = {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()}
    fake = tmp_path / "fake-bin/blastn"; good = fake.read_text()
    # Force a content-signature invalidation, then fail after partial BLAST output.
    (db / "coi_db.nsq").write_text("new COI reference\n")
    (db / "its_db.nsq").write_text("new ITS2 reference\n")
    fake.write_text(good + "\nif args[args.index('-db') + 1].endswith('its_db'):\n    raise SystemExit(19)\n")
    run_shell(tmp_path, env, base, db, taxdb, targets="COI|ITS2", ok=False)
    assert {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()} == before
    fake.write_text(good)
    run_shell(tmp_path, env, base, db, taxdb, targets="COI|ITS2")
    after = {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()}
    # Successful cache-only resume: make any attempted BLAST query fatal.
    fake.write_text("#!/bin/sh\nexit 99\n")
    run_shell(tmp_path, env, base, db, taxdb, targets="COI|ITS2")
    assert {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()} == after


def test_publish_never_opens_live_destination_for_truncation(tmp_path):
    # Inject a write failure after a prefix: if publication writes the live file
    # before rename, this ordinary assertion detects loss of the previous bytes.
    injection = tmp_path / "inject"; injection.mkdir()
    source = tmp_path / "source"; source.write_text(seal("CACHE", []))
    destination = tmp_path / "destination"; destination.write_text("prior valid state\n")
    # A rename fault occurs after the pending temporary file has been written.
    (injection / "WriteFault.pm").write_text('''package WriteFault;
use strict; use warnings;
BEGIN { *CORE::GLOBAL::rename = sub { $! = 13; return 0 }; }
1;
''')
    env = dict(os.environ, PERL5OPT="-MWriteFault", PERL5LIB=str(injection))
    run(CACHE, "--publish", source, destination, cwd=tmp_path, env=env, ok=False)
    assert destination.read_text() == "prior valid state\n"
    assert not list(tmp_path.glob(".r4-publish-*"))
    run(CACHE, "--publish", source, destination, cwd=tmp_path)
    assert destination.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("pid,expected", [("98", "40"), ("97.999", "30"), ("95", "30"),
                                          ("94.999", "20"), ("92", "20")])
def test_exact_identity_tiers_preserve_selected_precision(tmp_path, pid, expected):
    p = select(tmp_path, [hsp(pid=pid)])
    memory = seal("MEMTAX", ["40\t40\t30\t20\t10\tspecies"])
    result = depth(tmp_path, Path(str(p) + ".evidence"), cache_text=memory)
    assert result.stdout.split(";")[1] == expected
    assert float(result.stdout.strip().split(";")[4]) == float(pid)


def test_taxonomy_input_permutation_and_multiple_rounds(tmp_path):
    env = tiny_taxdump(tmp_path / "taxonomy")
    fa = ""; rows = []
    for i, tax in enumerate(["40", "41", "42", "50", "30", "20", "10", "0", "-123", "99999999"]):
        seq = "ACGT" * 20 + "A" * (i + 1)
        hash_ = hashlib.md5(seq.encode()).hexdigest()
        fa += f">q{i}|ITS2|opaque\n{seq}\n"
        rows.append(hsp(f"acc.{i}", tax, hash_=hash_, qlen=str(len(seq)), end=str(len(seq))))
    first = select(tmp_path, rows, fa=fa)
    e = Path(str(first) + ".evidence")
    first_output = depth(tmp_path, e, env=env).stdout
    memory = (tmp_path / "next-memtax").read_text()
    p = select(tmp_path, rows[::-1], fa=fa)
    assert depth(tmp_path, Path(str(p) + ".evidence"), cache_text=memory, env=env).stdout == first_output
    assert (tmp_path / "next-memtax").read_text() == memory


def test_no_new_read_round_preserves_persistent_state(tmp_path):
    env, base, db, taxdb = helper_environment(tmp_path)
    (tmp_path / "reads.fa").write_text(">q|COI|opaque\nACGT\n")
    run_shell(tmp_path, env, base, db, taxdb)
    old = {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()}
    (tmp_path / "reads.fa").write_text("")
    run_shell(tmp_path, env, base, db, taxdb)
    assert {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()} == old
    assert (tmp_path / "bc_blastreport_round.txt").read_bytes() == b""
    assert (tmp_path / "bc_blastreport_targets_r4.txt").read_bytes() == b""


def test_unknown_schema_and_malformed_memtax_fail_closed(tmp_path):
    p = select(tmp_path, [hsp()])
    for bad in [seal("MEMTAX", ["40\t40\t30\t20"]),
                seal("MEMTAX", ["40\t40\t30\t20\t10\tspecies"]).replace("MEMTAX\t1", "MEMTAX\t9")]:
        previous = tmp_path / "memtax"; previous.write_text(bad)
        out = tmp_path / "pending-memory"; out.write_text("previous pending output\n")
        run(DEPTH, Path(str(p) + ".evidence"), "98", "95", previous, SIG, out, cwd=tmp_path, ok=False)
        assert previous.read_text() == bad
        assert out.read_text() == "previous pending output\n"


def test_legacy_reader_rejects_truncation_and_missing_input(tmp_path):
    fa = tmp_path / "fa"; fa.write_text(f">q|ITS2\n{SEQ}\n")
    cache = tmp_path / "old"
    outputs = [tmp_path / name for name in ["cached", "new.fa", "hashes"]]
    for p in outputs: p.write_text("preserve\n")
    for text in [HASH + "\tacc\t1e-20\t100\t99", HASH + "\tacc\tNaN\t100\t99\n"]:
        cache.write_text(text)
        run(CACHE, fa, cache, *outputs, cwd=tmp_path, ok=False)
        assert all(p.read_text() == "preserve\n" for p in outputs)
    cache.unlink()
    run(CACHE, fa, cache, *outputs, cwd=tmp_path, ok=False)


def test_pinned_local_taxdump_readonly(tmp_path):
    pinned = Path(os.environ.get("R4_PINNED_TAXDUMP", str(ROOT / "db/taxonomy/releases/ncbi-taxdump-2024-06-24")))
    assert pinned.is_dir(), "Pinned taxonomy unavailable; set R4_PINNED_TAXDUMP for this real-data check"
    # These expected identities are read independently from nodes.dmp.
    requested = {9606, 9605, 9604, 9443}
    found = {}
    with (pinned / "nodes.dmp").open() as source:
        for line in source:
            fields = [v.strip() for v in line.split("|")]
            if int(fields[0]) in requested:
                found[int(fields[0])] = (int(fields[1]), fields[2])
    assert found[9606] == (9605, "species")
    assert found[9605] == (207598, "genus")
    assert found[9604][1] == "family" and found[9443][1] == "order"
    env = dict(os.environ, TAXONKIT_DB=str(pinned))
    p = select(tmp_path, [hsp(tax="9606")])
    result = depth(tmp_path, Path(str(p) + ".evidence"), env=env)
    assert result.stdout == "q|ITS2|opaque;9606;1e-30;100;99.0\n"
    rows = records(tmp_path / "next-memtax")
    assert rows == [["9606", "9606", "9605", "9604", "9443", "species"]]


def test_blast_rounded_fifty_percent_coverage_compatibility(tmp_path):
    rng = random.Random(134)
    seq = "".join(rng.choice("ACGT") for _ in range(201))
    hash_ = hashlib.md5(seq.encode()).hexdigest()
    p = select(tmp_path, [hsp(hash_=hash_, qlen="201", end="100")],
               fa=f">q|ITS2|opaque\n{seq}\n")
    assert records(Path(str(p) + ".evidence"))[0][-1] == "UNIQUE"
    query = tmp_path / "query.fa"; query.write_text(f">q\n{seq}\n")
    subject = tmp_path / "subject.fa"; subject.write_text(f">subject\n{seq[:100]}\n")
    run("makeblastdb", "-in", subject, "-dbtype", "nucl", "-out", tmp_path / "db", cwd=tmp_path)
    result = run("blastn", "-query", query, "-db", tmp_path / "db", "-task", "megablast",
                 "-word_size", "50", "-dust", "no", "-qcov_hsp_perc", "50", "-outfmt",
                 "6 qseqid length qstart qend qlen qcovhsp", cwd=tmp_path)
    assert result.stdout == "q\t100\t1\t100\t201\t50\n"


def test_reference_signature_covers_numeric_volumes_and_alias_dependencies(tmp_path):
    env = tiny_taxdump(tmp_path / "tax")
    db = tmp_path / "volume db"
    Path(str(db) + ".njs").write_text("metadata\n")
    volume = Path(str(db) + ".00.nsq"); volume.write_text("volume one\n")
    def signature(prefix):
        return run(CACHE, "--signature", prefix, env["TAXONKIT_DB"], cwd=tmp_path).stdout
    original = signature(db)
    stat = volume.stat(); volume.write_text("volume two\n")
    os.utime(volume, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert signature(db) != original
    alias = tmp_path / "alias"
    Path(str(alias) + ".nal").write_text('DBLIST "volume db"\n')
    original = signature(alias)
    volume.write_text("volume six\n")
    assert signature(alias) != original


@pytest.mark.parametrize("taxids", [("40", "40"), ("40", "30"), ("40", "-123")])
def test_real_blast_reserved_taxid_annotations_preserve_signed_ties(tmp_path, taxids):
    # The installed production references use these explicit annotations rather
    # than native BLAST taxid maps. No accession component determines taxonomy.
    rng = random.Random(51)
    seq = "".join(rng.choice("ACGT") for _ in range(400))
    hash_ = hashlib.md5(seq.encode()).hexdigest()
    query = tmp_path / "q.fa"; query.write_text(f">{hash_}\n{seq}\n")
    outputs = []
    labels = [f"opaque|acc.1|kraken:taxid|{taxids[0]}", f"opaque|acc.10|kraken:taxid|{taxids[1]}"]
    for i, subjects in enumerate([labels, labels[::-1]]):
        ref = tmp_path / f"ref{i}.fa"
        ref.write_text("".join(f">{subject} ITS2 manufactured fixture\n{seq}\n" for subject in subjects))
        db = tmp_path / f"db{i}"
        run("makeblastdb", "-in", ref, "-dbtype", "nucl", "-out", db, cwd=tmp_path)
        raw = run("blastn", "-query", query, "-db", db, "-task", "megablast", "-dust", "no",
                  "-word_size", "50", "-qcov_hsp_perc", "50", "-max_target_seqs", "2",
                  "-outfmt", "6 qseqid sseqid staxids evalue length pident bitscore qstart qend sstart send qlen",
                  cwd=tmp_path).stdout.splitlines()
        assert len(raw) == 2
        assert {row.split("\t")[2] for row in raw} == {"0"}
        p = select(tmp_path / str(i), raw, fa=f">q|ITS2|opaque\n{seq}\n")
        path = Path(str(p) + ".evidence"); outputs.append(path.read_bytes())
        evidence = records(path)
        assert {row[2] for row in evidence} == set(labels)
        assert {row[3] for row in evidence} == set(taxids)
        assert {row[-1] for row in evidence} == {"DEFERRED_TIE"}
    assert outputs[0] == outputs[1]


def test_conflicting_reference_annotation_fails_closed(tmp_path):
    fa = tmp_path / "fa"; fa.write_text(f">q|ITS2|opaque\n{SEQ}\n")
    cache = tmp_path / "cache"; cache.write_text(seal("CACHE", []))
    p = tmp_path / "p"
    run(CACHE, "--prepare", fa, cache, SIG, p, cwd=tmp_path)
    raw = tmp_path / "raw"; raw.write_text(hsp("opaque|kraken:taxid|-123", tax="40") + "\n")
    run(CACHE, "--complete", fa, str(p) + ".snapshot", SIG, raw, p, cwd=tmp_path, ok=False)
    assert cache.read_text() == seal("CACHE", [])
    assert not Path(str(p) + ".cache").exists()


@pytest.mark.parametrize("outcome", ["success", "blast_failure", "malformed_output", "taxonomy_failure"])
def test_worker_scratch_cleanup_on_success_and_failure(tmp_path, outcome):
    env, base, db, taxdb = helper_environment(tmp_path)
    scratch = tmp_path / "temporary"; scratch.mkdir()
    env["TMPDIR"] = str(scratch)
    (tmp_path / "reads.fa").write_text(">q|COI|opaque\nACGT\n")
    if outcome in ("blast_failure", "malformed_output"):
        fake = tmp_path / "fake-bin/blastn"
        with fake.open("a") as f:
            f.write("\nraise SystemExit(19)\n" if outcome == "blast_failure" else "\nprint('truncated')\n")
    elif outcome == "taxonomy_failure":
        fake = tmp_path / "fake-bin/taxonkit"
        with fake.open("a") as f: f.write("\nraise SystemExit(19)\n")
    run_shell(tmp_path, env, base, db, taxdb, ok=outcome == "success")
    assert not list(scratch.iterdir())


def test_empty_blast_shell_result_is_cached_without_new_logging(tmp_path):
    env, base, db, taxdb = helper_environment(tmp_path)
    (tmp_path / "reads.fa").write_text(">q|COI|opaque\nACGT\n")
    fake = tmp_path / "fake-bin/blastn"; fake.write_text("#!/bin/sh\nexit 0\n")
    first = run_shell(tmp_path, env, base, db, taxdb)
    assert first.stdout == first.stderr == ""
    assert (tmp_path / "bc_blastreport_round.txt").read_bytes() == b""
    old = {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()}
    fake.write_text("#!/bin/sh\nexit 99\n")
    second = run_shell(tmp_path, env, base, db, taxdb)
    assert second.stdout == second.stderr == ""
    assert {p.name: p.read_bytes() for p in (tmp_path / "state").iterdir()} == old


# Configured scientific seeds are distinct from obsolete derived state.
SHIPPED_SEEDS = Path('/Users/DavidJuan/Downloads/RTBioScan_working/RTBioScan/db')
SEED_NAMES = {'COI': 'COInr_2024Jun_metazoa_memtax1.txt', 'ITS2': 'ITS2nr_2024Jun_viridiplantae_memtax2.txt'}


def configured_depth(case, tax, pid, seed, target, *, previous=None, env=None):
    case.mkdir(exist_ok=True)
    p = select(case, [hsp(tax=tax, pid=pid)], fa=f'>q|{target}|opaque\n{SEQ}\n')
    old = case / 'memtax'
    if previous is not None:
        old.write_text(previous)
    proc = run(DEPTH, Path(str(p) + '.evidence'), '98', '95', old, SIG,
               case / 'next-memtax', seed, target, cwd=case, env=env)
    return proc.stdout, (case / 'next-memtax').read_bytes()


@pytest.mark.parametrize('target,expected', [('COI', ['6407', '-122', '-123']), ('ITS2', ['80887', '94685', '-123'])])
def test_shipped_seed_depth_tiers_and_restart(tmp_path, target, expected):
    seed = SHIPPED_SEEDS / SEED_NAMES[target]; before = seed.read_bytes()
    env = tiny_taxdump(tmp_path / 'taxonomy')
    for pid, tax in zip(['93.5', '96', '99'], expected):
        case = tmp_path / pid
        report, memory = configured_depth(case, '-123', pid, seed, target, env=env)
        assert report.split(';')[1] == tax
        again, reloaded = configured_depth(case, '-123', pid, seed, target, previous=memory.decode(), env=env)
        assert again == report and reloaded == memory
    assert seed.read_bytes() == before


@pytest.mark.parametrize('tax,order', [('-34953', '7088'), ('-9045', '7147'), ('-18547', '7399')])
def test_exact_shipped_depth_equivalent_rank_pairs(tmp_path, tax, order):
    source = SHIPPED_SEEDS / SEED_NAMES['COI']
    rows = [s for s in source.read_text().splitlines() if s.split('\t')[0] == tax]
    assert len(rows) == 2 and {s.split('\t')[4] for s in rows} == {'family', 'subfamily'}
    outputs = []; seed = tmp_path / 'configured-seed'
    for index, permutation in enumerate([rows, rows[::-1], rows + rows[::-1]]):
        seed.write_text('\n'.join(permutation) + '\n')
        report, memory = configured_depth(tmp_path / str(index), tax, '99', seed, 'COI')
        assert report.split(';')[1] == tax
        assert memory.decode().splitlines()[1].split('\t') == [tax, 'NA', 'NA', tax, order, '["family","subfamily"]']
        outputs.append(memory)
    assert outputs[0] == outputs[1] == outputs[2]
    report, memory = configured_depth(tmp_path / 'full', tax, '99', source, 'COI')
    assert memory == outputs[0]


@pytest.mark.parametrize('bad', ['-1\t-2\t3\t4\tfamily\n-1\t-2\t5\t4\tsubfamily\n', '-1\t-2\t3\tfamily\n',
                                 '-1\tNaN\t3\t4\tspecies\n', '-1\t-2\t3\t4\tfamily\n-1\t-2\t3\t4\tspecies\n'])
def test_configured_seed_conflicts_and_malformed_rows_fail_closed(tmp_path, bad):
    seed = tmp_path / 'seed'; seed.write_text(bad)
    p = select(tmp_path, [hsp(tax='-1')])
    old = tmp_path / 'memtax'; old.write_text('legacy\n\n')
    pending = tmp_path / 'pending'; pending.write_text('untouched\n')
    run(DEPTH, Path(str(p) + '.evidence'), '98', '95', old, SIG, pending, seed, 'ITS2', cwd=tmp_path, ok=False)
    assert old.read_text() == 'legacy\n\n' and pending.read_text() == 'untouched\n'


def test_seed_marker_authority_and_missingness(tmp_path):
    p = select(tmp_path, [hsp(tax='-123')])
    run(DEPTH, Path(str(p) + '.evidence'), '98', '95', tmp_path / 'memtax', SIG, tmp_path / 'pending',
        SHIPPED_SEEDS / SEED_NAMES['COI'], 'ITS2', cwd=tmp_path, ok=False)
    env = tiny_taxdump(tmp_path / 'taxonomy')
    stale = seal('MEMTAX', ['-123\t-123\t-122\t6407\t6406\tspecies'])
    assert depth(tmp_path, Path(str(p) + '.evidence'), cache_text=stale, env=env).stdout.split(';')[1] == 'NA'
    for tax in ['0', '99999999', '-123']:
        case = tmp_path / tax; case.mkdir()
        q = select(case, [hsp(tax=tax)])
        assert depth(case, Path(str(q) + '.evidence'), env=env).stdout.split(';')[1] == 'NA'


def test_seed_signature_canonical_content_and_marker(tmp_path):
    env = tiny_taxdump(tmp_path / 'taxonomy')
    db = tmp_path / 'db'; Path(str(db) + '.nsq').write_bytes(b'reference')
    seed = tmp_path / 'seed'; rows = ['-1\t\t-1\t4\tfamily', '-1\t\t-1\t4\tsubfamily']
    def signature(target='COI'):
        return run(CACHE, '--signature', db, env['TAXONKIT_DB'], 'seed=' + str(seed), 'target=' + target, cwd=tmp_path).stdout.split('\t')[0]
    signatures = []
    for rr in [rows, rows[::-1], rows + rows]:
        seed.write_text('\n'.join(rr) + '\n'); signatures.append(signature())
    assert len(set(signatures)) == 1
    stamp = seed.stat(); seed.write_text(seed.read_text().replace('\t4\t', '\t5\t'))
    os.utime(seed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert seed.stat().st_size == stamp.st_size and signature() != signatures[0]
    assert signature('ITS2') != signature('COI')


@pytest.mark.parametrize('legacy', ['', '\n\n', 'not a versioned cache\n\n', '-123\t900\t901\t902\tspecies\n\n'])
def test_legacy_state_is_never_seed_authority(tmp_path, legacy):
    report, memory = configured_depth(tmp_path, '-123', '96', SHIPPED_SEEDS / SEED_NAMES['COI'], 'COI', previous=legacy)
    assert report.split(';')[1] == '-122'
    assert memory.startswith(b'#RTB-R4-MEMTAX\t2\t')
    assert (tmp_path / 'memtax').read_text() == legacy


@pytest.mark.parametrize('labels', ['["subfamily","family"]', '["family","family"]', '["species"]', '["family",4]', '[]'])
def test_malformed_versioned_rank_sets_fail_closed(tmp_path, labels):
    p = select(tmp_path, [hsp(tax='-1')])
    bad = seal('MEMTAX', ['-1\tNA\tNA\t-1\t4\t' + labels]).replace('MEMTAX\t1', 'MEMTAX\t2')
    old = tmp_path / 'memtax'; old.write_text(bad)
    run(DEPTH, Path(str(p) + '.evidence'), '98', '95', old, SIG, tmp_path / 'pending', cwd=tmp_path, ok=False)
    assert old.read_text() == bad


def test_head_seed_copy_migration_both_markers_and_interruption(tmp_path):
    env, base, db, taxdb = helper_environment(tmp_path)
    env.update(tiny_taxdump(tmp_path / 'real-taxonomy'))
    env['PATH'] = str(tmp_path / 'fake-bin') + os.pathsep + os.environ['PATH']
    (tmp_path / 'fake-bin/taxonkit').unlink()
    states = tmp_path / 'state'; states.mkdir()
    seeds = [SHIPPED_SEEDS / SEED_NAMES[t] for t in ['COI', 'ITS2']]
    for i, source in enumerate(seeds, 1):
        shutil.copyfile(source, states / f'memtax{i}.txt')  # Exact HEAD copy behavior.
        assert (states / f'memtax{i}.txt').read_bytes() == source.read_bytes()
    before = {p.name: p.read_bytes() for p in states.iterdir()}
    (tmp_path / 'reads.fa').write_text('>a|COI|x\nACGT\n>b|ITS2|x\nTGCA\n>c|ITS2|x\nAAAA\n')
    fake = tmp_path / 'fake-bin/blastn'
    fake.write_text('''#!/usr/bin/env python3
import sys, hashlib
from pathlib import Path
args=sys.argv[1:]
for line in Path(args[args.index('-query')+1]).read_text().splitlines():
 if not line.startswith('>'): continue
 h=line[1:]
 tax,pid=('-123','93.5') if h==hashlib.md5(b'ACGT').hexdigest() else ('-123','96') if h==hashlib.md5(b'TGCA').hexdigest() else ('40','99')
 print('\\t'.join([h,'subject.'+tax,tax,'1e-30','4',pid,'200','1','4','1','4','4']))
''')
    args = ['/bin/bash', ROOT / 'bin/blast_otu_pretax.sh', 'bc', tmp_path / 'reads.fa', states, '2', base,
            str(db) + '/', taxdb, tmp_path / 'round', 'COI|ITS2', 'coi_db|its_db', '92|92', '95|95', '98|98',
            '|'.join(map(str, seeds)), '11', '50']
    tripwire = tmp_path / 'fake-bin/taxonkit'
    tripwire.write_text('#!/bin/sh\nprintf "partial\\n"\nexit 19\n'); tripwire.chmod(0o755)
    run(*args, cwd=tmp_path, env=env, ok=False)
    assert {p.name: p.read_bytes() for p in states.iterdir()} == before
    tripwire.unlink()
    run(*args, cwd=tmp_path, env=env)
    report = (tmp_path / 'bc_blastreport_round.txt').read_text()
    assert [line.split(';')[1] for line in report.splitlines()] == ['6407', '94685', '40']
    after = {p.name: p.read_bytes() for p in states.iterdir()}
    assert all(after[f'memtax{i}.txt'].startswith(b'#RTB-R4-MEMTAX\t2\t') for i in [1, 2])
    fake.write_text('#!/bin/sh\nexit 91\n')
    tripwire.write_text('#!/bin/sh\nexit 92\n'); tripwire.chmod(0o755)
    run(*args, cwd=tmp_path, env=env)
    assert {p.name: p.read_bytes() for p in states.iterdir()} == after
    assert (tmp_path / 'bc_blastreport_round.txt').read_text() == report
    nodes = Path(env['TAXONKIT_DB']) / 'names.dmp'; stamp = nodes.stat()
    nodes.write_text(nodes.read_text().replace('Taxon40', 'Taxon41'))
    os.utime(nodes, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    run(*args, cwd=tmp_path, env=env, ok=False)
    assert {p.name: p.read_bytes() for p in states.iterdir()} == after


@pytest.mark.parametrize('field,left,right', [('bits', '200.0000000000000001', '200'), ('ev', '1e-400', '2e-400'),
    ('ev', '9.999999999999999e-30', '1e-29'), ('bits', '9007199254740993', '9007199254740992'),
    ('bits', '.00010001', '1e-4'), ('pid', '98.000000000000001', '98')])
def test_exact_decimal_ranking_oracle(tmp_path, field, left, right):
    from decimal import Decimal
    assert (Decimal(left) < Decimal(right)) if field == 'ev' else (Decimal(left) > Decimal(right))
    rows = [hsp('z.winner', **{field: left}), hsp('a.loser', **{field: right})]
    for i, rr in enumerate([rows, rows[::-1]]):
        p = select(tmp_path / str(i), rr)
        evidence = records(Path(str(p) + '.evidence'))
        assert len(evidence) == 1 and evidence[0][2] == 'z.winner'


@pytest.mark.parametrize('values', [('0', '0.0', '0e0'), ('1e-30', '.000000000000000000000000000001', '10e-31'),
                                   ('100', '1e2', '100.000'), ('1.2', '12e-1', '1.2000')])
def test_decimal_equivalent_spellings_retain_all_ties(tmp_path, values):
    p = select(tmp_path, [hsp('subject.' + str(i), ev=value) for i, value in enumerate(values)])
    evidence = records(Path(str(p) + '.evidence'))
    assert len(evidence) == len(values) and {r[-1] for r in evidence} == {'DEFERRED_TIE'}


def profiled_worker(case, *, old=None, raw=None):
    case.mkdir(exist_ok=True)
    fasta = case / 'reads.fa'; fasta.write_text(f'>q|ITS2|opaque\n{SEQ}\n')
    cache = case / 'cache'
    if old is not None:
        cache.write_bytes(old)
    rows = raw if raw is not None else [hsp('a', '-123'), hsp('z', '-124')]
    blast = case / 'blast'; blast.write_text('#!/bin/sh\ncat ' + str(case / 'raw') + '\n'); blast.chmod(0o755)
    (case / 'raw').write_text(''.join(s + '\n' for s in rows))
    loader = case / 'profile.pl'
    loader.write_text(r'''use strict; use warnings; use JSON::PP;
require $ARGV[0]; shift @ARGV;
require Math::BigFloat;
my %count=map { $_=>0 } qw(cache_read selections evidence_read);
{ no strict 'refs'; no warnings 'redefine';
  *Math::BigFloat::new=sub { die "per-comparison BigFloat is forbidden\n" };
  for my $name (keys %count) {
    my $original=\&{$name};
    *{$name}=sub { $count{$name}++; return $original->(@_); };
  }
}
worker(@ARGV);
print JSON::PP->new->canonical->encode(\%count);
''')
    p = case / 'p'
    proc = run('perl', loader, CACHE, fasta, cache, SIG, p, case / 'memtax', '98', '95', '', 'ITS2', blast, cwd=case)
    return p, json.loads(proc.stdout)


def test_worker_one_history_pass_and_cache_only_no_reselection(tmp_path):
    p, first = profiled_worker(tmp_path / 'first')
    assert first == {'cache_read': 1, 'selections': 1, 'evidence_read': 0}
    cache = Path(str(p) + '.cache').read_bytes()
    q, resumed = profiled_worker(tmp_path / 'resumed', old=cache)
    assert resumed == {'cache_read': 1, 'selections': 0, 'evidence_read': 0}
    assert Path(str(q) + '.cache').read_bytes() == cache
    assert Path(str(q) + '.evidence').read_bytes() == Path(str(p) + '.evidence').read_bytes()
    assert len(records(Path(str(q) + '.evidence'))) == 2
    assert Path(str(q) + '.report').read_text().split(';')[1] == 'NA'


def test_generated_manifest_rejects_interrupted_or_changed_publication(tmp_path):
    p, _ = profiled_worker(tmp_path / 'worker')
    source = Path(str(p) + '.cache'); source.write_bytes(source.read_bytes()[:-1])
    destination = tmp_path / 'live'; destination.write_text('prior valid state\n')
    run(CACHE, '--publish-ready', source, destination, Path(str(p) + '.manifest'), 'cache', cwd=tmp_path, ok=False)
    assert destination.read_text() == 'prior valid state\n'
    assert not list(tmp_path.glob('.r4-publish-*'))


@pytest.mark.parametrize('prefix', ['\n', 'legacy\n', '   '])
def test_displaced_new_memtax_marker_is_not_legacy(tmp_path, prefix):
    p = select(tmp_path, [hsp(tax='-123')])
    old = tmp_path / 'memtax'; bad = prefix + seal('MEMTAX', [])
    old.write_text(bad)
    run(DEPTH, Path(str(p) + '.evidence'), '98', '95', old, SIG, tmp_path / 'pending', cwd=tmp_path, ok=False)
    assert old.read_text() == bad


def test_seed_content_change_regenerates_depth_after_safe_retry(tmp_path):
    env, base, db, taxdb = helper_environment(tmp_path)
    seed = base / 'seed'; seed.write_text('-123\t-122\t6407\t6406\tspecies\n')
    (tmp_path / 'reads.fa').write_text('>q|COI|opaque\nACGT\n')
    fake = tmp_path / 'fake-bin/blastn'
    good = fake.read_text().replace('staxids="123"', 'staxids="-123"').replace('pident="99.0"', 'pident="96"')
    fake.write_text(good)
    args = ['/bin/bash', ROOT / 'bin/blast_otu_pretax.sh', 'bc', tmp_path / 'reads.fa', tmp_path / 'state',
            '1', base, str(db) + '/', taxdb, tmp_path / 'round', 'COI', 'coi_db', '92', '95', '98', str(seed), '11', '50']
    run(*args, cwd=tmp_path, env=env)
    assert (tmp_path / 'bc_blastreport_round.txt').read_text().split(';')[1] == '-122'
    before = {p.name: p.read_bytes() for p in (tmp_path / 'state').iterdir()}
    stamp = seed.stat(); seed.write_text(seed.read_text().replace('-122', '-121'))
    os.utime(seed, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert seed.stat().st_size == stamp.st_size
    fake.write_text('#!/bin/sh\nexit 91\n')
    run(*args, cwd=tmp_path, env=env, ok=False)
    assert {p.name: p.read_bytes() for p in (tmp_path / 'state').iterdir()} == before
    fake.write_text(good)
    run(*args, cwd=tmp_path, env=env)
    assert (tmp_path / 'bc_blastreport_round.txt').read_text().split(';')[1] == '-121'
    after = {p.name: p.read_bytes() for p in (tmp_path / 'state').iterdir()}
    fake.write_text('#!/bin/sh\nexit 91\n')
    run(*args, cwd=tmp_path, env=env)
    assert {p.name: p.read_bytes() for p in (tmp_path / 'state').iterdir()} == after


def test_real_blast_foldback_hsps_use_strongest_evidence(tmp_path):
    rng = random.Random(97131)
    seq = ''.join(rng.choice('ACGT') for _ in range(600))
    mutated = seq[:250] + ''.join({'A': 'C', 'C': 'G', 'G': 'T', 'T': 'A'}[x] for x in seq[250:274]) + seq[274:]
    hash_ = hashlib.md5(seq.encode()).hexdigest()
    (tmp_path / 'subject.fa').write_text('>foldback\n' + mutated + 'N' * 100 + seq[:350] + '\n')
    (tmp_path / 'query.fa').write_text('>' + hash_ + '\n' + seq + '\n')
    run('makeblastdb', '-in', tmp_path / 'subject.fa', '-dbtype', 'nucl', '-parse_seqids', '-taxid', '40', '-out', tmp_path / 'db', cwd=tmp_path)
    raw = run('blastn', '-query', tmp_path / 'query.fa', '-db', tmp_path / 'db', '-task', 'megablast', '-dust', 'no',
              '-word_size', '50', '-qcov_hsp_perc', '50', '-perc_identity', '92', '-evalue', '11', '-max_hsps', '50',
              '-max_target_seqs', '1', '-outfmt', '6 qseqid sseqid staxids evalue length pident bitscore qstart qend sstart send qlen', cwd=tmp_path).stdout.splitlines()
    fields = [s.split('\t') for s in raw]
    # The long imperfect manufactured copy is stronger than the perfect fragment.
    long = [r for r in fields if int(r[4]) >= 590]
    short = [r for r in fields if int(r[4]) <= 400 and float(r[5]) == 100]
    assert len(long) == 1 and short and 95 <= float(long[0][5]) < 98
    assert all(float(long[0][6]) > float(r[6]) for r in short)
    env = tiny_taxdump(tmp_path / 'taxonomy')
    for i, rows in enumerate([raw, raw[::-1]]):
        case = tmp_path / str(i)
        p = select(case, rows, fa=f'>q|ITS2|opaque\n{seq}\n')
        result = depth(case, Path(str(p) + '.evidence'), env=env)
        assert result.stdout.split(';')[1] == '30'
        assert float(result.stdout.split(';')[4]) == float(long[0][5])


def test_incremental_worker_preserves_history_and_selects_only_new_rows(tmp_path):
    first, _ = profiled_worker(tmp_path / 'first')
    case = tmp_path / 'changed'; case.mkdir()
    cache = case / 'cache'; cache.write_bytes(Path(str(first) + '.cache').read_bytes())
    seq = SEQ[:-1] + 'A'; hash_ = hashlib.md5(seq.encode()).hexdigest()
    fa = case / 'reads.fa'; fa.write_text(f'>r|ITS2|new\n{seq}\n')
    raw = [hsp('strong', '0', bits='400', pid='96', hash_=hash_),
           hsp('strong', '0', bits='200', pid='100', start='2', hash_=hash_),
           hsp('other', '0', bits='300', hash_=hash_)]
    (case / 'raw').write_text('\n'.join(raw) + '\n')
    blast = case / 'blast'; blast.write_text('#!/bin/sh\ncat ' + str(case / 'raw') + '\n'); blast.chmod(0o755)
    loader = case / 'profile.pl'
    loader.write_text(r'''use strict; use warnings;
require $ARGV[0]; shift @ARGV;
my $rows=0; my $original=\&selections;
{ no warnings 'redefine'; *selections=sub { $rows+=scalar @{$_[1]}; return $original->(@_); }; }
worker(@ARGV); print $rows;
''')
    p = case / 'p'
    result = run('perl', loader, CACHE, fa, cache, SIG, p, case / 'memtax', '98', '95', '', 'ITS2', blast, cwd=case)
    assert result.stdout == '3'
    old_rows = [r for r in records(Path(str(first) + '.cache')) if r[0] == 'H']
    new_rows = [r for r in records(Path(str(p) + '.cache')) if r[0] == 'H' and r[1] == HASH]
    assert old_rows == new_rows
    assert len(Path(str(p) + '.all-report').read_text().splitlines()) == 2
    assert Path(str(p) + '.report').read_text() == 'r|ITS2|new;NA;1e-30;100;96.0\n'


def test_process_interruption_preserves_legacy_state_and_retry(tmp_path):
    import signal
    import time
    env, base, db, taxdb = helper_environment(tmp_path)
    scratch = tmp_path / 'scratch'; scratch.mkdir(); env['TMPDIR'] = str(scratch)
    states = tmp_path / 'state'; states.mkdir()
    legacy = (SHIPPED_SEEDS / SEED_NAMES['COI']).read_bytes()
    (states / 'memtax1.txt').write_bytes(legacy)
    (tmp_path / 'reads.fa').write_text('>q|COI|opaque\nACGT\n')
    fake = tmp_path / 'fake-bin/blastn'; good = fake.read_text()
    ready = tmp_path / 'ready'
    fake.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nimport time\n'
                    + f'Path({str(ready)!r}).write_text("ready")\ntime.sleep(10)\n')
    args = ['/bin/bash', ROOT / 'bin/blast_otu_pretax.sh', 'bc', tmp_path / 'reads.fa', states, '1', base,
            str(db) + '/', taxdb, tmp_path / 'round', 'COI', 'coi_db', '92', '95', '98', 'memtax1.txt', '11', '50']
    process = subprocess.Popen(list(map(str, args)), cwd=tmp_path, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.exists()
        os.killpg(process.pid, signal.SIGTERM)
        process.communicate(timeout=5)
        assert process.returncode != 0
        assert {p.name: p.read_bytes() for p in states.iterdir()} == {'memtax1.txt': legacy}
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL); process.communicate(timeout=5)
    fake.write_text(good)
    run(*args, cwd=tmp_path, env=env)
    assert (states / 'memtax1.txt').read_bytes().startswith(b'#RTB-R4-MEMTAX\t2\t')


def test_worker_rejects_cross_marker_history(tmp_path):
    p, _ = profiled_worker(tmp_path / 'first')
    rows = records(Path(str(p) + '.cache'))
    for row in rows:
        if row[0] == 'M':
            row[1] = row[1].replace('|ITS2|', '|COI|')
    cache = tmp_path / 'cache'
    cache.write_text(seal('CACHE', ['\t'.join(r) for r in rows]).replace('CACHE\t1', 'CACHE\t2'))
    before = cache.read_bytes()
    fa = tmp_path / 'reads.fa'; fa.write_text(f'>q|ITS2|opaque\n{SEQ}\n')
    result = run(CACHE, '--worker', fa, cache, SIG, tmp_path / 'p', tmp_path / 'memory',
                 '98', '95', '', 'ITS2', '/usr/bin/false', cwd=tmp_path, ok=False)
    assert 'marker mismatch' in result.stderr
    assert cache.read_bytes() == before
