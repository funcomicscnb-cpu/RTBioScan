"""R5-F01: a restore rebuilds the complete authoritative rolling state or refuses.

Rounds are driven through the real state writers (round_index_assign.sh,
consensus_prune_apply.sh with reads_apply_prune_ids.pl,
otu_unassigned_streak_update.pl); the snapshot is written by the §5 done_pod5
copy plus the §6 fragment extracted verbatim from main.nf, and restored by the
real restart_handler.sh. Equivalence is judged by an oracle written here
(inventory globs, SHA-256, FASTA IDs, done keys, round map, list IDs, streak
rows), not by the production record parser.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BIN = REPO / "bin"
MAIN = REPO / "main.nf"
HELPER = BIN / "state_snapshot_authority.pl"
HANDLER = BIN / "restart_handler.sh"
SYNC = BIN / "lib" / "backup_sync.sh"
SID = "S1"
POOL = "qced_reads_hq_accumulated.fasta"

# Independent oracle inventory (the owner-authorized authoritative state).
ORACLE_GLOBS = [
    "done_pod5.txt", POOL, "round_index.tsv",
    "*_assigned_read_ids_ever.list", "*_protected_read_ids_ever.list", "*_assigned_otu_keys_ever.list",
    "*_pruned_barrier.list", "*_pruned_archive.fasta", "*_otu_size_streak.tsv", "*_otu_unassigned_streak.tsv",
    "read_qscore_rolling.tsv", "otu_frozen_*.tsv", "otu_frozen_reps.fasta", "otu_frozen_reps.fasta.gz",
    "otu_active_pool.fasta", "otu_seen_hashes.tsv", "*consensus_consolidated_ids.txt", "otu_consolidated_keys.tsv",
    "*_seen_read_ids.tsv", "*_on_target_state.tsv", "state_compatibility_manifest.tsv",
]

PROFILES = {
    # barcode -> (marker, writes a protected ever-list)
    "default": {"RTBioScan": ("COI", True)},
    "broad_its2": {"barcode01": ("ITS2", True), "barcode02": ("ITS2", False)},
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(cmd, env=None, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, **(env or {})}, check=False)
    if check:
        assert r.returncode == 0, (cmd, r.stdout, r.stderr)
    return r


def section6() -> str:
    text = MAIN.read_text(encoding="utf-8")
    backup = text[text.index("process backup_update_and_clean {"):]
    start = backup.index('mkdir -p "\\$CURRENT_TEMP_ROOT/tables" "\\$CURRENT_ROOT/tables"')
    end = backup.index("# ---- Release round lock", start)
    frag = backup[start:end].rstrip()
    assert frag.endswith("fi")
    frag = frag[: frag.rfind("fi")]  # the enclosing ALREADY_COMPLETED guard
    assert "state_snapshot_authority.pl" in frag
    return frag.replace("\\$", "$").replace("${baseDir}", str(REPO))


def layout(root: Path) -> dict:
    out = root / "out"
    L = {
        "out": out,
        "ongoing": out / "temp" / "ongoing" / "state" / SID,
        "state": out / "temp" / "ongoing" / "state" / SID / "_state",
        "tcur": out / "temp" / "current" / "state" / SID,
        "cur": out / "current" / "state" / SID,
        "work": root / "work",
    }
    L["state"].mkdir(parents=True)
    L["work"].mkdir()
    return L


def append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def run_round(L, n: int, profile: str, prune: bool = False, min_rounds: int = 4) -> Path:
    """One completed round's authoritative-state effects, via the real writers."""
    st = L["state"]
    rb = f"FAX00001_pass_{n}"
    rdir = L["ongoing"] / rb
    rdir.mkdir()
    idx = run(["bash", str(BIN / "round_index_assign.sh"), "--state-dir", str(st), "--round-barcode", rb,
               "--no-lock"]).stdout.strip()
    (rdir / "round_index.tsv").write_text(f"{rb}\t{idx}\n")  # the round-local copy §7 publishes
    for bc, (marker, protected) in PROFILES[profile].items():
        reads = [f"{bc}-r{n}-{i}" for i in range(4)]
        append(st / POOL, "".join(f">{r}\nACGT{n}{i}\n" for i, r in enumerate(reads)))
        append(st / f"{bc}_assigned_read_ids_ever.list", f"{reads[0]}\n")
        if protected:
            append(st / f"{bc}_protected_read_ids_ever.list", f"{reads[1]}\n")
        append(st / f"{bc}_assigned_otu_keys_ever.list", f"{marker}|{n:032x}\n")
        append(st / f"{bc}_otu_size_streak.tsv", f"{marker}|{n:032x}\t{n}\n")
        append(st / f"{bc}_seen_read_ids.tsv", "".join(f"{r}\n" for r in reads))
        # an OTU that stays unassigned every round: its streak reaches the threshold at round min_rounds
        streak(L, bc, marker, n, rdir, min_rounds)
    append(st / "otu_frozen_members.tsv", f"OTUB_{n}\t{n}\n")
    append(st / "read_qscore_rolling.tsv", f"r{n}\tsup\t20\n")
    # derived scratch rebuilt every round (not authoritative)
    (st / "qced_reads_nr.fasta.clstr").write_text(f">Cluster {n}\n")
    if prune:
        ids = rdir / "prune_ids.list"
        victims = [f"{bc}-r{k}-2" for bc in PROFILES[profile] for k in range(1, n + 1)]
        ids.write_text("".join(f"{v}\n" for v in victims))
        (rdir / "recovery.list").write_text("")
        bc0 = next(iter(PROFILES[profile]))
        run(["bash", str(BIN / "consensus_prune_apply.sh"), "--prune-ids", str(ids), "--recovery-ids",
             str(rdir / "recovery.list"), "--fasta", str(st / POOL), "--fasta-tmp", str(st / f"{POOL}.prune.tmp"),
             "--apply-stats", str(rdir / "apply_stats.tsv"), "--prune-stats", str(rdir / "prune_stats.tsv"),
             "--round-cp", str(rdir / POOL), "--lock-dir", str(st / ".qced.lock"), "--lock-wait", "5",
             "--apply-script", str(BIN / "reads_apply_prune_ids.pl"),
             "--pruned-barrier", str(st / f"{bc0}_pruned_barrier.list"),
             "--pruned-archive", str(st / f"{bc0}_pruned_archive.fasta")])
        assert (rdir / POOL).is_file()
    append(st / "done_pod5.txt", f"/x/{rb}.pod5\t1\t1\t{n}\t{rb}.pod5\n")
    return rdir


def streak(L, bc, marker, n, rdir, min_rounds):
    st = L["state"]
    otu = f"OTUB_1-{marker}-{bc}"
    w = L["work"] / f"streak-{bc}-{n}"
    w.mkdir(exist_ok=True)
    (w / "sizes.tsv").write_text(f"{otu}\t2\n")
    (w / "members.tsv").write_text(f"{otu}\t{bc}-u1\n{otu}\t{bc}-u2\n")
    (w / "evidence.tsv").write_text(f"read_id\totu_taxid\n{bc}-u1|{otu}\tNA\n")
    (w / "hash.tsv").write_text(f"{bc}-u1\t{'ab' * 16}\n")
    state = st / f"{bc}_otu_unassigned_streak.tsv"
    prev = state if state.exists() else w / "empty.tsv"
    prev.touch()
    nxt = w / "next.tsv"
    run(["perl", str(BIN / "otu_unassigned_streak_update.pl"), str(w / "sizes.tsv"), str(w / "members.tsv"),
         str(w / "evidence.tsv"), str(prev), str(min_rounds), "1", "50", str(rdir / f"{bc}_streak_prune.list"),
         str(w / "stats.tsv"), str(nxt), str(w / "hash.tsv")])
    shutil.copyfile(nxt, state)


def backup(L, check=True):
    """§5 done_pod5 copy + §6 (verbatim) + the §7 per-round copies that collide."""
    script = (f"set -euo pipefail\nshopt -s nullglob\nsource '{SYNC}'\nSTATE_TMP='{L['state']}'\n"
              f"CURRENT_TEMP_ROOT='{L['tcur']}'\nCURRENT_ROOT='{L['cur']}'\n"
              f"mkdir -p \"$CURRENT_TEMP_ROOT\"\ncp \"$STATE_TMP/done_pod5.txt\" \"$CURRENT_TEMP_ROOT\"/\n"
              + section6())
    r = run(["/bin/bash", "-c", script], check=False)
    if check:
        assert r.returncode == 0 and "WARN: state snapshot authority" not in r.stderr, r.stderr
    rounds = sorted(p for p in L["ongoing"].iterdir() if p.name.startswith("FAX"))
    last = rounds[-1]
    seq = L["tcur"] / "sequences"
    (L["tcur"] / "tables").mkdir(parents=True, exist_ok=True)
    seq.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(last / "round_index.tsv", L["tcur"] / "tables" / "round_index.tsv")
    for rd in rounds:  # a round-prune copy stays in sequences/ until a later prune replaces it
        if (rd / POOL).exists():
            shutil.copyfile(rd / POOL, seq / POOL)
    return r


def restore(L, op="a", force="0"):
    env = {"MODE": "restore", "OUTDIR": str(L["out"]), "LOCK_WAIT": "5", "RUN_NAME": "runF01", "STATE_ID": SID,
           "FORCE": force, "OPERATION_ID": op * 64}
    return run(["/bin/bash", str(HANDLER)], env=env, check=False)


def lose_live_state(L):
    shutil.rmtree(L["out"] / "temp" / "ongoing")


# ---- independent oracle ----------------------------------------------------
def fasta_ids(data: bytes):
    return sorted(line[1:].split()[0] for line in data.decode().splitlines() if line.startswith(">"))


def oracle(state: Path) -> dict:
    out = {}
    for p in sorted(state.iterdir()):
        if p.is_file() and any(fnmatch.fnmatchcase(p.name, g) for g in ORACLE_GLOBS):
            data = p.read_bytes()
            view = {"sha": sha(data)}
            if p.name.endswith(".fasta"):
                view["ids"] = fasta_ids(data)
            elif p.name == "done_pod5.txt":
                view["done"] = [line.split("\t")[0] for line in data.decode().splitlines()]
            elif p.name == "round_index.tsv":
                view["map"] = dict(line.split("\t") for line in data.decode().splitlines())
            else:
                view["rows"] = data.decode().splitlines()
            out[p.name] = view
    return out


def tree(root: Path) -> dict:
    view = {}
    if not root.exists():
        return view
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        data = sha(p.read_bytes()) if p.is_file() and not p.is_symlink() else None
        view[str(p.relative_to(root))] = (oct(st.st_mode), st.st_ino, st.st_size, st.st_mtime_ns, data)
    return view


def outdir_view(L) -> dict:
    return {k: tree(L["out"] / k) for k in ("temp/ongoing", "temp/current", "current")}


def sentinel(L) -> Path:
    return L["out"] / "temp" / f".restart_applied.{SID}"


def build(tmp_path, profile="default", rounds=3, prune_round=None, min_rounds=4):
    L = layout(tmp_path)
    for n in range(1, rounds + 1):
        run_round(L, n, profile, prune=(n == prune_round), min_rounds=min_rounds)
        backup(L)
    return L


def assert_refused(L, result, before, message="cannot be restored safely"):
    assert result.returncode != 0, result.stderr
    assert message in result.stderr, result.stderr
    if message == "cannot be restored safely":
        assert "restart_mode=reset" in result.stderr
    assert outdir_view(L) == before
    assert not sentinel(L).exists()


# ---- 1/3/10: no-prune equivalence, both profiles -----------------------------
@pytest.mark.parametrize("profile", list(PROFILES))
def test_three_round_no_prune_restore_is_equivalent(tmp_path, profile):
    L = build(tmp_path, profile)
    control = oracle(L["state"])
    assert control["round_index.tsv"]["map"] == {f"FAX00001_pass_{n}": str(n) for n in (1, 2, 3)}
    assert len(control["done_pod5.txt"]["done"]) == 3
    # the collision sources exist and are stale
    assert (L["tcur"] / "tables" / "round_index.tsv").read_text() == "FAX00001_pass_3\t3\n"
    lose_live_state(L)
    r = restore(L)
    assert r.returncode == 0, r.stderr
    assert oracle(L["state"]) == control
    for bc, (_, protected) in PROFILES[profile].items():
        # optional marker files are neither fabricated nor lost
        assert (L["state"] / f"{bc}_protected_read_ids_ever.list").exists() == protected
    assert not (L["state"] / "qced_reads_nr.fasta.clstr").exists()  # derived, rebuilt by OTU_definition
    assert sentinel(L).read_text().startswith("schema=2\nstatus=applied\n")


# ---- 2/6: prune case, stale round-prune copy loses ---------------------------
@pytest.mark.parametrize("profile", list(PROFILES))
def test_three_round_prune_restore_keeps_later_reads_barrier_and_archive(tmp_path, profile):
    L = build(tmp_path, profile, prune_round=2)
    control = oracle(L["state"])
    bc0 = next(iter(PROFILES[profile]))
    assert control[f"{bc0}_pruned_barrier.list"]["rows"] and control[f"{bc0}_pruned_archive.fasta"]["ids"]
    stale = (L["tcur"] / "sequences" / POOL).read_bytes()
    assert fasta_ids(stale) != control[POOL]["ids"]  # the round-2 copy lacks round-3 reads
    lose_live_state(L)
    r = restore(L)
    assert r.returncode == 0, r.stderr
    assert oracle(L["state"]) == control
    assert any(i.endswith("-r3-0") for i in oracle(L["state"])[POOL]["ids"])


# ---- 3/4: streak state and the next pruning decision -------------------------
def test_streak_state_restores_and_next_prune_happens_on_the_same_round(tmp_path):
    ctl = build(tmp_path / "ctl", min_rounds=4)
    rst = build(tmp_path / "rst", min_rounds=4)
    lose_live_state(rst)
    assert restore(rst).returncode == 0
    name = "RTBioScan_otu_unassigned_streak.tsv"
    assert (rst["state"] / name).read_bytes() == (ctl["state"] / name).read_bytes()
    assert (rst["state"] / "RTBioScan_otu_size_streak.tsv").read_bytes() == \
        (ctl["state"] / "RTBioScan_otu_size_streak.tsv").read_bytes()
    decisions = []
    for L in (ctl, rst):
        rdir = run_round(L, 4, "default", min_rounds=4)
        decisions.append((rdir / "RTBioScan_streak_prune.list").read_text())
    assert decisions[0] == decisions[1] and decisions[0].strip(), decisions


# ---- 4: legacy snapshot ------------------------------------------------------
def test_legacy_snapshot_with_completed_rounds_is_refused_before_mutation(tmp_path):
    L = build(tmp_path)
    for root in (L["tcur"], L["cur"]):
        shutil.rmtree(root / "state_authority")
    assert (L["tcur"] / "done_pod5.txt").stat().st_size > 0
    before = outdir_view(L)
    assert_refused(L, restore(L), before)


# ---- 5: partial / malformed / altered snapshots ------------------------------
def _record(root):
    return root / "state_authority" / "AUTHORITY"


def _rewrite_record(root, fn):
    p = _record(root)
    p.write_bytes(fn(p.read_bytes()))


DAMAGE = {
    "missing_pool": lambda r: (r / "state_authority" / POOL).unlink(),
    "missing_ever_list": lambda r: (r / "state_authority" / "RTBioScan_assigned_read_ids_ever.list").unlink(),
    "altered_member_same_size": lambda r: (r / "state_authority" / "round_index.tsv").write_bytes(
        (r / "state_authority" / "round_index.tsv").read_bytes().replace(b"\t3", b"\t9")),
    "stale_member": lambda r: (r / "state_authority" / "done_pod5.txt").write_bytes(
        b"".join((r / "state_authority" / "done_pod5.txt").read_bytes().splitlines(True)[:2])),
    "truncated_record": lambda r: _rewrite_record(r, lambda b: b[: len(b) // 2]),
    "record_without_end": lambda r: _rewrite_record(r, lambda b: b"".join(b.splitlines(True)[:-1])),
    "wrong_digest": lambda r: _rewrite_record(r, lambda b: b[:-65] + b"0" * 64 + b"\n"),
    "wrong_count": lambda r: _rewrite_record(r, lambda b: b.replace(b"#END\t", b"#END\t1")),
    "traversal_record_line": lambda r: _rewrite_record(
        r, lambda b: b.replace(b"#END", b"member\t../done_pod5.txt\t1\t" + b"0" * 64 + b"\n#END")),
    "extra_file": lambda r: (r / "state_authority" / "otu_frozen_extra.tsv").write_text("x\n"),
    "record_symlink": lambda r: (_record(r).rename(r / "rec"), _record(r).symlink_to(r / "rec")),
    "empty_authority_dir": lambda r: [p.unlink() for p in (r / "state_authority").iterdir()],
}


@pytest.mark.parametrize("damage", list(DAMAGE))
@pytest.mark.parametrize("which", ["tcur", "cur"])
def test_partial_or_damaged_snapshot_is_refused_before_mutation(tmp_path, damage, which):
    L = build(tmp_path)
    DAMAGE[damage](L[which])
    before = outdir_view(L)
    # a symlinked record is already refused by the existing snapshot symlink scan
    message = "unsafe symlink in restore snapshot state" if damage == "record_symlink" else "cannot be restored safely"
    assert_refused(L, restore(L), before, message)


# ---- 7: interrupted backup, then retry ---------------------------------------
@pytest.mark.parametrize("victim", ["RTBioScan_assigned_otu_keys_ever.list", "round_index.tsv"])
def test_interrupted_backup_is_never_accepted_and_retry_converges(tmp_path, victim):
    ctl = build(tmp_path / "ctl")
    L = layout(tmp_path / "x")
    for n in (1, 2):
        run_round(L, n, "default")
        backup(L)
    run_round(L, 3, "default")
    # the backup fails at the first member it copies (the ever-list sorts first) or after
    # several members were already rewritten (round_index.tsv sorts last)
    (L["state"] / victim).chmod(0)
    try:
        r = backup(L, check=False)
    finally:
        (L["state"] / victim).chmod(0o644)
    assert "WARN: state snapshot authority not published" in r.stderr
    for root in (L["tcur"], L["cur"]):  # the round-2 authority was invalidated, nothing new accepted
        assert not _record(root).exists()
    before = outdir_view(L)
    assert_refused(L, restore(L), before)
    backup(L)  # the retry
    for root, croot in ((L["tcur"], ctl["tcur"]), (L["cur"], ctl["cur"])):
        got = {p.name: p.read_bytes() for p in (root / "state_authority").iterdir() if p.name != ".lock"}
        want = {p.name: p.read_bytes() for p in (croot / "state_authority").iterdir() if p.name != ".lock"}
        assert got == want
    control = oracle(L["state"])
    lose_live_state(L)
    assert restore(L).returncode == 0
    assert oracle(L["state"]) == control


# ---- 8: pristine --------------------------------------------------------------
def test_pristine_snapshot_keeps_its_existing_behavior(tmp_path):
    L = layout(tmp_path)
    (L["tcur"] / "tables").mkdir(parents=True)
    (L["tcur"] / "tables" / "unrelated-report.tsv").write_text("x\n")
    (L["tcur"] / "done_pod5.txt").write_text("")
    (L["cur"] / "plots").mkdir(parents=True)
    (L["cur"] / "plots" / "rolling-plot.tsv").write_text("p\n")
    r = restore(L)
    assert r.returncode == 0, r.stderr
    assert (L["state"] / "unrelated-report.tsv").read_text() == "x\n"
    assert (L["state"] / "rolling-plot.tsv").read_text() == "p\n"
    assert sentinel(L).read_text().startswith("schema=2\nstatus=applied\n")


# ---- 9: both roots are independent authorities --------------------------------
@pytest.mark.parametrize("lost", ["tcur", "cur"])
def test_each_root_alone_restores_the_complete_state(tmp_path, lost):
    L = build(tmp_path, prune_round=2)
    control = oracle(L["state"])
    shutil.rmtree(L[lost])
    lose_live_state(L)
    r = restore(L)
    assert r.returncode == 0, r.stderr
    assert oracle(L["state"]) == control


def test_a_damaged_root_is_not_filled_from_the_other(tmp_path):
    L = build(tmp_path)
    (L["cur"] / "state_authority" / POOL).unlink()
    before = outdir_view(L)
    assert_refused(L, restore(L), before)


def test_roots_holding_different_sealed_generations_are_refused(tmp_path):
    L = build(tmp_path, rounds=2)
    saved = tmp_path / "cur-round2"
    shutil.copytree(L["cur"] / "state_authority", saved)
    run_round(L, 3, "default")
    backup(L)
    shutil.rmtree(L["cur"] / "state_authority")
    shutil.copytree(saved, L["cur"] / "state_authority")
    before = outdir_view(L)
    assert_refused(L, restore(L), before)


@pytest.mark.parametrize("selected", ["tcur", "cur"])
def test_advanced_root_ledger_refuses_stale_authority_then_retry_converges(tmp_path, selected):
    ctl = build(tmp_path / "control", rounds=3)
    control = oracle(ctl["state"])
    L = build(tmp_path / "interrupted", rounds=2)
    old = independent_record_check(L[selected])
    assert len(old["done_pod5.txt"].splitlines()) == 2
    run_round(L, 3, "default")
    live = oracle(L["state"])
    assert live == control
    assert len(live["done_pod5.txt"]["done"]) == 3
    assert any(i.endswith("-r3-0") for i in live[POOL]["ids"])

    # The root ledger advances before the helper invalidates the round-2 record.
    # Keep only the selected root so either can independently supply restore.
    other = "cur" if selected == "tcur" else "tcur"
    shutil.rmtree(L[other])
    shutil.copyfile(L["state"] / "done_pod5.txt", L[selected] / "done_pod5.txt")
    assert independent_record_check(L[selected]) == old
    assert (L[selected] / "done_pod5.txt").read_bytes() != old["done_pod5.txt"]
    before = outdir_view(L)
    v = run(["perl", str(HELPER), "verify", str(L[selected])], check=False)
    r = restore(L)

    # These semantic checks precede the status checks so a comparison-bypass
    # mutant is killed when the real handler installs the incomplete round-2 state.
    after = oracle(L["state"])
    assert after == live, (
        "restore installed the old N-1 scientific state: "
        f"verify_rc={v.returncode} restore_rc={r.returncode} "
        f"completed_rounds={len(after['done_pod5.txt']['done'])} "
        f"accumulated_reads={len(after[POOL]['ids'])}; "
        f"expected_completed_rounds=3 expected_accumulated_reads={len(live[POOL]['ids'])}"
    )
    assert outdir_view(L) == before
    assert v.returncode == 2 and "stale completion-ledger mismatch" in v.stderr
    assert_refused(L, r, before)
    assert not list((L["out"] / "temp").glob(".restart*"))

    backup(L)
    for key in ("tcur", "cur"):
        sealed = independent_record_check(L[key])
        assert sealed == independent_record_check(ctl[key])
        assert len(sealed["done_pod5.txt"].splitlines()) == 3
        assert run(["perl", str(HELPER), "verify", str(L[key])]).returncode == 0
    lose_live_state(L)
    assert restore(L).returncode == 0
    restored = oracle(L["state"])
    assert restored == control
    for name in control:
        got = L["state"] / name
        want = ctl["state"] / name
        assert got.read_bytes() == want.read_bytes()
        assert got.stat().st_mode == want.stat().st_mode


def test_sealed_snapshot_without_root_ledger_keeps_restore_behavior(tmp_path):
    L = build(tmp_path)
    control = oracle(L["state"])
    (L["tcur"] / "done_pod5.txt").unlink()
    assert run(["perl", str(HELPER), "verify", str(L["tcur"])]).returncode == 0
    lose_live_state(L)
    assert restore(L).returncode == 0
    assert oracle(L["state"]) == control


# ---- 7b: process death at every publication boundary ------------------------
# The helper runs unchanged; the wrapper SIGKILLs it at its k-th rename().
CRASH_WRAPPER = r"""
BEGIN {
    my $n = 0;
    *CORE::GLOBAL::rename = sub {
        CORE::kill('KILL', $$) if ++$n == $ENV{F01_KILL_AT_RENAME};
        return CORE::rename($_[0], $_[1]);
    };
}
my $script = shift @ARGV;
do $script;
die $@ if $@;
"""


def independent_record_check(root: Path):
    """Parse the record without the production code: None when absent, else the
    member bytes it validly binds (raises AssertionError when it binds anything else)."""
    d = root / "state_authority"
    rec = d / "AUTHORITY"
    if not rec.exists():
        return None
    raw = rec.read_bytes()
    lines = raw.split(b"\n")
    assert lines[-1] == b"" and lines[0] == b"#RTB-STATE-AUTHORITY\t1"
    end = lines[-2].split(b"\t")
    body = b"".join(line + b"\n" for line in lines[:-2])
    assert end[0] == b"#END" and int(end[1]) == len(lines) - 3 and end[2].decode() == sha(body)
    members = {}
    for line in lines[1:-2]:
        tag, name, size, digest = line.decode().split("\t")
        assert tag == "member" and "/" not in name
        data = (d / name).read_bytes()
        assert len(data) == int(size) and sha(data) == digest, name
        members[name] = data
    assert {p.name for p in d.iterdir()} - {"AUTHORITY", ".lock"} == set(members)
    return members


def test_process_death_at_every_publication_boundary_never_publishes_an_incomplete_record(tmp_path):
    ctl = build(tmp_path / "ctl")
    want = independent_record_check(ctl["tcur"])
    assert want == independent_record_check(ctl["cur"]) and POOL in want
    L = layout(tmp_path / "x")
    for n in (1, 2):
        run_round(L, n, "default")
        backup(L)
    run_round(L, 3, "default")
    saved = tmp_path / "saved"
    for key in ("tcur", "cur"):
        shutil.copytree(L[key], saved / key, symlinks=True)
    k = 0
    while True:
        k += 1
        for key in ("tcur", "cur"):
            shutil.rmtree(L[key])
            shutil.copytree(saved / key, L[key], symlinks=True)
        r = run(["perl", "-e", CRASH_WRAPPER, str(HELPER), "publish", str(L["state"]), str(L["tcur"]), str(L["cur"])],
                env={"F01_KILL_AT_RENAME": str(k)}, check=False)
        crashed = r.returncode == -9
        got = [independent_record_check(L[key]) for key in ("tcur", "cur")]
        for g in got:  # a record exists only for the complete round-3 snapshot
            assert g is None or g == want, k
        if any(g is None for g in got):
            before = outdir_view(L)
            assert_refused(L, restore(L), before)
        # the next backup converges on the uninterrupted snapshot
        assert run(["perl", str(HELPER), "publish", str(L["state"]), str(L["tcur"]), str(L["cur"])]).returncode == 0
        assert [independent_record_check(L[key]) for key in ("tcur", "cur")] == [want, want]
        if not crashed:
            break
    assert k > 2 * len(want)  # every member rename and both record renames were crash points
