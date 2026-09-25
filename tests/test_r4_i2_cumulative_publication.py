"""R4-I2: interruption-safe publication of the cumulative BLAST OTU snapshot.

The three cumulative products in `_state` form one generation named by the
sealed commit record `<barcode>_blast_otu_cumulative.commit`, the only
authority; its members are immutable `<name>.gen-<generation>` files. The
public names are compatibility projections: every public name that changes is
retracted before any is projected, so the names present at any instant never
mix generations. Backup destinations hold a replica of the generation -- its
record kind, every member including the reporting sidecar, projections of the
two public tables -- published from the source's authority (never from the
presence of its public names), and a restore installs that generation into
`_state` with its authority (an older backup is first proven by retained
evidence, or the restore is refused). Without a record only a pristine `_state`
(no public name, member or governed residue), a complete, content-consistent
pre-I2 snapshot and an authentic pre-R4-D snapshot (sealed on first sight as a
legacy generation; written here by the immutable pre-R4-D code) are
legitimate; everything else fails closed, and the publisher refuses to write
over anything that is not authentic.

These tests drive the real publishers (bin/r4_reporting_contract.pl, and
bin/lib/backup_sync.sh -> RTBioScan::R4DCumulative::publish_backup) and every
reader of the snapshot:

* real process death: a perl process stops itself at a chosen filesystem
  mutation boundary (a boundary module loaded through PERL5OPT) and receives
  a real SIGTERM (+SIGCONT) or SIGKILL;
* synchronous faults injected into one primitive by a disposable wrapper;
* missing, damaged or ambiguous authority, lagging public names, legacy state
  written by the former sequential protocol, and concurrent publishers.

Readers: RTBioScan::R4DCumulative::resolve, report_round_json.pl (both
cumulative options, as wired by main.nf), report_run_json.pl, report_render.py,
and backup_update_and_clean sections 2 and 7 extracted from main.nf and run by
/bin/bash. Generation labels come from byte comparison against independently
published controls, never from the code under test.
"""
import errno
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_r4_reporting_contract import (
    BARCODE,
    ENV,
    HELPER,
    PLANT,
    REPORT_ROUND_JSON,
    core_model,
    member,
    otu,
    run_build,
    write_inputs,
    write_otu_def,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN = REPO_ROOT / "bin"
LIB = BIN / "lib"
RESOLVER = LIB / "RTBioScan" / "R4DCumulative.pm"
REPORT_RUN_JSON = BIN / "report_run_json.pl"
REPORT_RENDER = BIN / "report_render.py"
BACKUP_SYNC = LIB / "backup_sync.sh"
RESTART_HANDLER = BIN / "restart_handler.sh"
MAIN_NF = REPO_ROOT / "main.nf"
PRODUCTS = ("reporting", "public", "noadapter")
PAIR = ("public", "noadapter")
NAMES = {
    "reporting": f"{BARCODE}_blast_otu_reporting_v1.tsv",
    "public": f"{BARCODE}_blast_otu_pretax_rpt.txt",
    "noadapter": f"{BARCODE}_blast_otu_noadapter_rpt.txt",
}
RECORD = f"{BARCODE}_blast_otu_cumulative.commit"
LOCK = f"{BARCODE}_blast_otu_cumulative.lock"
PRETAX_HEADER = (b"read_id\tbarcode_by_homology\tbasecalling_model\tsample\thit_id\ttaxid\taln_length\tperc_id\totu_id\t"
                 b"otu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n")

# Loaded into every perl process of a driven command through PERL5OPT. Turns
# each filesystem mutation (File::Temp tempfile, link, rename, unlink, symlink)
# into a before and an after boundary; at I2_STOP_AT the process records
# I2_REACHED ("pid<TAB>n<TAB>event") and stops itself until the test delivers
# a real signal. I2_TRACE lists the boundaries of an uninterrupted run.
# I2_LINK_ERRNO makes link(2) fail with that errno (a filesystem without hard
# links, or an injected error); I2_LINK_MAX overrides pathconf(_PC_LINK_MAX);
# I2_SYMLINK_ERRNO makes symlink(2) fail.
INTERRUPT_MODULE = r"""
package I2TestInterrupt;
use strict;
use warnings;
our $N = 0;
my %E = map { $_ => $ENV{$_} // '' } qw(I2_STOP_AT I2_TRACE I2_REACHED I2_LINK_ERRNO I2_LINK_MAX I2_SYMLINK_ERRNO);
sub boundary {
    my ($what) = @_;
    local $!;  # the wrapped call's errno must reach the caller unchanged
    $N++;
    if ($E{I2_TRACE} ne '') { open my $t, '>>', $E{I2_TRACE} or die $!; print {$t} "$$\t$N\t$what\n"; close $t; }
    if ($E{I2_STOP_AT} ne '' && $N == $E{I2_STOP_AT}) {
        open my $m, '>', $E{I2_REACHED} or die $!; print {$m} "$$\t$N\t$what\n"; close $m; kill 'STOP', $$;
    }
}
BEGIN {
    *CORE::GLOBAL::rename = sub { my ($f, $t) = @_; boundary("pre rename $f => $t"); my $r = CORE::rename($f, $t); boundary("post rename $f => $t"); $r };
    *CORE::GLOBAL::link = sub {
        my ($f, $t) = @_;
        boundary("pre link $f => $t");
        my $r;
        if ($E{I2_LINK_ERRNO} ne '') { $! = $E{I2_LINK_ERRNO}; $r = 0; } else { $r = CORE::link($f, $t); }
        { local $!; boundary("post link $f => $t"); }
        $r;
    };
    *CORE::GLOBAL::symlink = sub {
        my ($f, $t) = @_;
        boundary("pre symlink $f => $t");
        my $r;
        if ($E{I2_SYMLINK_ERRNO} ne '') { $! = $E{I2_SYMLINK_ERRNO}; $r = 0; } else { $r = CORE::symlink($f, $t); }
        { local $!; boundary("post symlink $f => $t"); }
        $r;
    };
    *CORE::GLOBAL::unlink = sub { my @f = @_ ? @_ : ($_); boundary("pre unlink @f"); my $r = CORE::unlink(@f); boundary("post unlink @f"); $r };
}
use File::Temp ();
use POSIX ();
{
    no warnings 'redefine';
    my $orig = \&File::Temp::tempfile;
    *File::Temp::tempfile = sub { boundary('pre tempfile'); my @r = $orig->(@_); boundary("post tempfile $r[1]"); @r };
    if ($E{I2_LINK_MAX} ne '') {
        my $pc = \&POSIX::pathconf;
        *POSIX::pathconf = sub { $_[1] == POSIX::_PC_LINK_MAX() ? $E{I2_LINK_MAX} : $pc->(@_) };
    }
}
1;
"""

# Makes exactly one primitive fail synchronously (I2_FAULT, I2_FAULT_ARG).
FAULT_WRAPPER = r"""
use strict;
use warnings;
our ($FAULT, $ARG, %TEMP_FD, $SYNC_DIRS) = ('', '', (), 0);
BEGIN {
    *CORE::GLOBAL::rename = sub {
        my ($f, $t) = @_;
        if (($FAULT eq 'retract_rename' && $t =~ /\.bak\.[0-9]+\z/ && $t !~ /\.commit\.bak\./)
            || ($FAULT eq 'commit_rename' && $f =~ /\.r4d-publish-/ && $t =~ /_blast_otu_cumulative\.commit\z/)
            || ($FAULT =~ /\Aprojection_rename/ && $f =~ /\.r4d-publish-/ && $t =~ /\Q$ARG\E\z/)
            || ($FAULT eq 'projection_rename+restore' && $f =~ /\.bak\.[0-9]+\z/)) { $! = 5; return 0; }
        return CORE::rename($f, $t);
    };
    *CORE::GLOBAL::link = sub {
        my ($f, $t) = @_;
        if ($FAULT eq 'link_member' && $t =~ /\.gen-[0-9a-f]{64}\z/) { $! = 5; return 0; }
        return CORE::link($f, $t);
    };
    *CORE::GLOBAL::unlink = sub {
        my @f = @_ ? @_ : ($_);
        if ($FAULT eq 'cleanup_unlink' && grep { /\.bak\.[0-9]+\z/ } @f) { $! = 5; return 0; }
        return CORE::unlink(@f);
    };
    require Symbol;
    *CORE::GLOBAL::close = sub (;*) {
        my $fh = Symbol::qualify_to_ref(@_ ? $_[0] : select, scalar caller);
        my $fd = fileno($fh);
        if ($FAULT eq 'close' && defined($fd) && $TEMP_FD{$fd}) { delete $TEMP_FD{$fd}; CORE::close($fh); $! = 5; return 0; }
        return CORE::close($fh);
    };
}
($FAULT, $ARG) = ($ENV{I2_FAULT} // '', $ENV{I2_FAULT_ARG} // '');
use FindBin ();
use File::Basename ();
use IO::Handle ();
my $helper = shift @ARGV;
$FindBin::Bin = File::Basename::dirname($helper);
require $helper;
{
    no warnings 'redefine';
    my $orig = \&RTBioScan::R4D::tempfile;
    my $calls = 0;
    *RTBioScan::R4D::tempfile = sub {
        $calls++;
        die "injected tempfile failure at call $calls\n" if $FAULT eq 'tempfile' && $calls == $ARG;
        my @r = $orig->(@_);
        if ($FAULT eq 'write' && $calls == $ARG) { CORE::close($r[0]); open my $ro, '<', $r[1] or die $!; return ($ro, $r[1]); }
        $TEMP_FD{ fileno($r[0]) } = 1 if $FAULT eq 'close' && $calls == $ARG;
        return @r;
    };
    my $sync = \&IO::Handle::sync;
    *IO::Handle::sync = sub {
        my ($h) = @_;
        if ($FAULT eq 'sync_file' && -f $h) { $! = 5; return undef; }
        if ($FAULT eq 'sync_dir' && -d $h && ++$SYNC_DIRS == $ARG) { $! = 5; return undef; }
        return $sync->(@_);
    };
}
RTBioScan::R4D::main();
"""


# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def gens(tmp_path_factory):
    """Authentic sealed round generations and their expected snapshot bytes."""
    root = tmp_path_factory.mktemp("r4i2-generations")
    old, extra = core_model()
    old[0]["members"].append(member("na1", read_status="NO_HIT", adapter="no_adapter_1"))
    new = [dict(o, members=list(o["members"])) for o in old]
    new[0]["members"] = [m for m in new[0]["members"] if m["uuid"] != "m2"] + [
        member("m3", adapter="beta"), member("na2", adapter="no_adapter_1")]
    new.append(otu(4, marker="ITS2", taxid="-1156", ranks=PLANT, members=[member("v1", taxid="-1156", ranks=PLANT)]))
    third = [dict(o, members=list(o["members"])) for o in new]
    third.append(otu(3, status="NO_HIT", members=[member("n1", read_status="NO_HIT")]))
    out = {}
    for label, model in (("old", old), ("new", new), ("third", third)):
        d = write_inputs(root / label, model, extra)
        run_build(d)
        write_otu_def(d, model)
        out[label] = d
    # Expected snapshot bytes: each attempt published into an empty control.
    expected = {}
    for key, (label, na) in {"old": ("old", "1"), "new": ("new", "1"), "old-na0": ("old", "0"), "third": ("third", "1")}.items():
        control = root / f"control-{key}" / "_state"
        control.mkdir(parents=True)
        publish(control, out[label], na)
        expected[key] = {p: (control / NAMES[p]).read_bytes() for p in PRODUCTS}
    assert len({json.dumps({p: sha(b) for p, b in v.items()}) for v in expected.values()}) == 4
    refs = {key: round_json(root / f"control-{key}" / "_state", root / f"rj-{key}", out["new"]) for key in ("old", "new")}
    empty = root / "control-empty" / "_state"
    empty.mkdir(parents=True)
    refs["absent"] = round_json(empty, root / "rj-absent", out["new"])
    assert refs["old"]["cumulative"] != refs["new"]["cumulative"] != refs["absent"]["cumulative"]
    assert refs["old"]["pretax"] != refs["new"]["pretax"]
    return {"dirs": out, "expected": expected, "rj": refs, "root": root}


@pytest.fixture(scope="module")
def interrupt_env(tmp_path_factory):
    d = tmp_path_factory.mktemp("r4i2-interrupt")
    (d / "I2TestInterrupt.pm").write_text(INTERRUPT_MODULE, encoding="utf-8")
    return {"PERL5OPT": f"-I{d} -MI2TestInterrupt"}


@pytest.fixture(scope="module")
def render():
    spec = importlib.util.spec_from_file_location("report_render_r4i2", REPORT_RENDER)
    module = importlib.util.module_from_spec(spec)
    sys.dont_write_bytecode = True
    spec.loader.exec_module(module)
    return module


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def new_state(root: Path, name="s") -> Path:
    """A pipeline state directory: the authority governs `_state`."""
    state = root / name / "_state"
    state.mkdir(parents=True)
    return state


def publish(state: Path, gen_dir: Path, na="1", check=True, wrapper=None, env_extra=None, popen=False):
    args = ["--publish-cumulative", "--reporting", str(gen_dir / NAMES["reporting"]), "--state-dir", str(state),
            "--barcode", BARCODE, "--noadapter-enabled", na]
    cmd = ["perl", str(wrapper), str(HELPER), *args] if wrapper else ["perl", str(HELPER), *args]
    env = {**os.environ, **ENV, **(env_extra or {})}
    if popen:
        return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    if check:
        assert result.returncode == 0, result.stderr
    return result


def snapshot(state: Path):
    return {p.name: (sha(p.read_bytes()), p.lstat().st_ino, p.lstat().st_nlink, oct(p.lstat().st_mode & 0o7777))
            for p in sorted(state.iterdir())}


KEYS = ("old", "new", "old-na0")


def generation_of(texts, gens, keys=KEYS):
    """The reference generation holding all these product bytes ('a|b' when
    several hold them identically), 'none' for no product, or MIXED."""
    if not texts:
        return "none"
    expected = gens["expected"]
    common = set(keys)
    for p, data in texts.items():
        common &= {k for k in keys if expected[k][p] == data}
    if not common:
        return "MIXED:" + json.dumps({p: sorted(k for k in keys if expected[k][p] == d) for p, d in texts.items()},
                                     sort_keys=True)
    return "|".join(sorted(common))


def resolve(directory: Path, expect="state"):
    script = "require $ARGV[0]; exit RTBioScan::R4DCumulative::resolve_cli(@ARGV[1 .. $#ARGV]);"
    result = subprocess.run(["perl", "-e", script, str(RESOLVER), BARCODE, str(directory), expect],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)[0]


def resolved_generation(directory: Path, gens, expect="state", keys=KEYS):
    res = resolve(directory, expect)
    if not res["ok"]:
        return "FAIL_CLOSED"
    if res["mode"] == "absent":
        return "absent"
    return generation_of({p: Path(v).read_bytes() for p, v in res["paths"].items()}, gens, keys)


def round_json(state: Path, work: Path, new_dir: Path):
    work.mkdir(parents=True, exist_ok=True)
    out = work / "round.json"
    if out.exists():
        out.unlink()
    cmd = ["perl", str(REPORT_ROUND_JSON), "--run-id", "runA", "--barcode", BARCODE, "--round-barcode", "round_2",
           "--targets", "COI|ITS2", "--target-taxa", "Metazoa|Viridiplantae", "--timestamp-utc", "2026-09-23T00:00:00Z",
           "--out", str(out), "--blast-otu", str(new_dir / NAMES["public"]),
           "--blast-otu-reporting", str(new_dir / NAMES["reporting"]), "--otu-def", str(new_dir / "otu_def.tsv"),
           "--otu-sizes-round", str(new_dir / "otu_sizes_round.tsv"), "--otu-lock-summary", str(new_dir / "lock.tsv"),
           "--blast-id-family", "92", "--blast-id-genus", "95", "--blast-id-spec", "98",
           "--blast-otu-cumulative", str(state / NAMES["public"]),
           "--blast-otu-reporting-cumulative", str(state / NAMES["reporting"])]
    result = subprocess.run(cmd, cwd=work, env={**os.environ, **ENV}, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return {"rc": result.returncode, "stderr": result.stderr}
    data = json.loads(out.read_text(encoding="utf-8"))
    tax = data.get("taxonomy_assignment") or {}
    return {"rc": 0, "stderr": result.stderr,
            "cumulative": json.dumps([tax.get("cumulative"), tax.get("cumulative_equals_round")], sort_keys=True),
            "pretax": json.dumps((data.get("otu") or {}).get("active_by_marker_taxon"), sort_keys=True)}


def round_json_generation(obs, refs):
    if obs["rc"] != 0:
        return "FAIL_CLOSED"
    cum = [k for k, v in refs.items() if v["cumulative"] == obs["cumulative"]]
    pre = [k for k, v in refs.items() if v["pretax"] == obs["pretax"]]
    return cum[0] if len(cum) == 1 and cum[0] in pre else f"MIXED:{cum}/{pre}"


def main_nf_fragment(first_line, end_marker, main_nf=MAIN_NF):
    """A backup_update_and_clean fragment of main.nf as rendered bash."""
    lines = Path(main_nf).read_text(encoding="utf-8").split("\n")
    start = next(i for i, line in enumerate(lines) if line.strip() == first_line)
    end = next(i for i in range(start, len(lines)) if end_marker in lines[i])
    body = "\n".join(lines[start:end]).replace("\\\n", "")
    return body.replace("\\$", "$").replace("${barcode}", BARCODE)


def section2_script(state: Path, dest: Path):
    body = main_nf_fragment('rpts=( "\\$STATE_TMP"/*_rpt.txt )', "if (( \\${#pngs_all[@]} ))")
    return (f"set -euo pipefail\nshopt -s nullglob\nsource '{BACKUP_SYNC}'\nSTATE_TMP='{state}'\n"
            f"ONGOING_FINAL='{dest}'\nmkdir -p \"$ONGOING_FINAL\"\n{body}\n")


def section7_script(ongoing: Path, current: Path):
    body = main_nf_fragment('tables=( "\\$ONGOING_FINAL"/*.txt "\\$ONGOING_FINAL"/*.tsv "\\$ONGOING_FINAL"/*.csv \\',
                            "# ---- Create to_figures/")
    return (f"set -euo pipefail\nshopt -s nullglob\nsource '{BACKUP_SYNC}'\nONGOING_FINAL='{ongoing}'\n"
            f"CURRENT_ROOT='{current}'\nmkdir -p \"$CURRENT_ROOT/tables\"\n{body}\n")


def bash(script, env_extra=None):
    return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, check=False,
                          env={**os.environ, **ENV, **(env_extra or {})})


def pair_generation(directory: Path, gens, keys=KEYS):
    """The generation of the public pair a backup destination resolves to,
    'absent' when it holds none, or FAIL_CLOSED."""
    res = resolve(directory, "any")
    if not res["ok"]:
        return "FAIL_CLOSED"
    if res["mode"] == "ungoverned":
        texts = {p: (directory / NAMES[p]).read_bytes() for p in PAIR if (directory / NAMES[p]).exists()}
        if not texts:
            return "absent"
        return generation_of(texts, gens, keys) if len(texts) == 2 else "PARTIAL:" + generation_of(texts, gens, keys)
    return generation_of({p: Path(res["paths"][p]).read_bytes() for p in PAIR}, gens, keys)


def present_generation(directory: Path, gens, products=PRODUCTS, keys=KEYS):
    """The generation the public names that exist hold; they must never mix."""
    return generation_of({p: (directory / NAMES[p]).read_bytes() for p in products if (directory / NAMES[p]).exists()},
                         gens, keys)


def coherent(label):
    return not label.startswith(("MIXED", "PARTIAL"))


def clone_state(src: Path, dst: Path):
    """Copy a directory preserving its hard-link structure."""
    dst.mkdir(parents=True)
    first = {}
    for entry in sorted(src.iterdir()):
        if entry.is_symlink():
            os.symlink(os.readlink(entry), dst / entry.name)
            continue
        if entry.is_dir():
            shutil.copytree(entry, dst / entry.name, symlinks=True)
            continue
        key = (entry.lstat().st_dev, entry.lstat().st_ino)
        if key in first:
            os.link(first[key], dst / entry.name)
        else:
            shutil.copy2(entry, dst / entry.name)
            first[key] = dst / entry.name


def reader_generations(state: Path, work: Path, gens, render):
    """The generation every reader selects, or FAIL_CLOSED."""
    out = {"resolver": resolved_generation(state, gens)}
    out["round_json"] = round_json_generation(round_json(state, work / "rj", gens["dirs"]["new"]), gens["rj"])
    try:
        rr = render.resolve_r4d_cumulative([(state, "state")])[0]
        out["render"] = "absent" if rr["mode"] == "absent" else generation_of(
            {p: rr["paths"][p].read_bytes() for p in PAIR}, gens)
    except render.R4DCumulativeStateError:
        out["render"] = "FAIL_CLOSED"
    view = work / "state-view" / "_state"
    view.parent.mkdir(parents=True, exist_ok=True)
    clone_state(state, view)
    (view / f"{BARCODE}_reads_time_rpt.txt").write_text("time\treads\n", encoding="utf-8")  # a real _state always has one
    dest = work / "ongoing_final"
    bk = bash(section2_script(view, dest))
    if bk.returncode != 0:
        out["backup"] = "FAIL_CLOSED"
    else:
        out["backup"] = pair_generation(dest, gens)
    return out


def residue(state: Path):
    return sorted(n for n in os.listdir(state)
                  if n.startswith(".r4d-publish-") or ".bak." in n or n == LOCK)


def committed_names(state: Path):
    record = (state / RECORD).read_text(encoding="utf-8").splitlines()
    return record[1].split("\t")[1], record[2].split("\t")[1]


def trace_events(cmd_env, run):
    """Boundaries of an uninterrupted run: run(env) executes the driven command."""
    trace = cmd_env["_trace"]
    if trace.exists():
        trace.unlink()
    result = run({"I2_TRACE": str(trace)})
    assert result.returncode == 0, result.stderr
    return [line.split("\t", 2)[2] for line in trace.read_text().splitlines()]


def stop_and_signal(popen, reached: Path, sig):
    """popen(env) starts the driven command; deliver sig to the perl process stopped at the boundary."""
    if reached.exists():
        reached.unlink()
    proc = popen({"I2_REACHED": str(reached)})
    deadline = time.monotonic() + 60
    while not reached.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert reached.exists(), proc.communicate()
    pid, _, point = reached.read_text().rstrip("\n").split("\t", 2)
    deadline = time.monotonic() + 10
    while (subprocess.run(["ps", "-o", "stat=", "-p", pid], capture_output=True, text=True).stdout.strip()[:1] != "T"
           and time.monotonic() < deadline):
        time.sleep(0.005)
    os.kill(int(pid), sig)
    if sig == signal.SIGTERM:
        os.kill(int(pid), signal.SIGCONT)
    proc.communicate(timeout=60)
    return point


def publisher_boundaries(tmp_path, seed: Path, gen_dir: Path, interrupt_env, na="1", extra=None):
    probe = tmp_path / "probe" / "_state"
    clone_state(seed, probe)
    trace = tmp_path / "trace.tsv"
    return trace_events({"_trace": trace}, lambda e: publish(probe, gen_dir, na, check=False,
                                                            env_extra={**interrupt_env, **(extra or {}), **e}))


def kill_publisher(state: Path, gen_dir: Path, index: int, sig, work: Path, interrupt_env, na="1", extra=None):
    return stop_and_signal(lambda e: publish(state, gen_dir, na, popen=True,
                                             env_extra={**interrupt_env, **(extra or {}), "I2_STOP_AT": str(index), **e}),
                           work / "reached", sig)


def is_commit(event):
    return event.startswith("post rename") and event.endswith(RECORD)


# ---------------------------------------------------------------------------
def test_record_format_generation_layout_and_projection(tmp_path, gens):
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["old"])
    record = (state / RECORD).read_bytes()
    lines = record.decode().split("\n")
    assert lines[0] == "#RTB-R4D-CUMULATIVE\t1" and lines[2] == "previous\tNA" and lines[-1] == ""
    products = "".join(line + "\n" for line in lines[3:6])
    expected = gens["expected"]["old"]
    oracle = "".join(f"{p}\t{NAMES[p]}\t{len(expected[p])}\t{sha(expected[p])}\n" for p in PRODUCTS)
    assert products == oracle
    generation = sha(oracle.encode())
    assert lines[1] == f"generation\t{generation}"
    head = "\n".join(lines[:6]) + "\n"
    assert lines[6] == f"#END\t{sha(head.encode())}"
    assert sorted(os.listdir(state)) == sorted([RECORD] + [NAMES[p] for p in PRODUCTS]
                                               + [f"{NAMES[p]}.gen-{generation}" for p in PRODUCTS])
    for p in PRODUCTS:
        live, copy = state / NAMES[p], state / f"{NAMES[p]}.gen-{generation}"
        assert live.read_bytes() == expected[p]
        assert live.stat().st_ino == copy.stat().st_ino and live.stat().st_nlink == 2
        assert live.stat().st_mode & 0o777 == 0o600
    assert resolve(state)["mode"] == "committed"


def test_replay_is_a_strict_noop_and_retention_keeps_one_predecessor(tmp_path, gens):
    state = new_state(tmp_path)
    d = gens["dirs"]
    publish(state, d["old"])
    first = snapshot(state)
    publish(state, d["old"])
    assert snapshot(state) == first
    old_gen, _ = committed_names(state)
    publish(state, d["new"])
    new_gen, previous = committed_names(state)
    assert previous == old_gen != new_gen
    assert {n.rsplit("-", 1)[1] for n in os.listdir(state) if ".gen-" in n} == {old_gen, new_gen}
    replay = snapshot(state)
    publish(state, d["new"])
    assert snapshot(state) == replay
    # back to an earlier generation: its retained copies are reused, not rewritten
    old_inodes = {p: (state / f"{NAMES[p]}.gen-{old_gen}").stat().st_ino for p in PRODUCTS}
    publish(state, d["old"])
    assert committed_names(state) == (old_gen, new_gen)
    assert {p: (state / NAMES[p]).stat().st_ino for p in PRODUCTS} == old_inodes
    # a third generation releases the oldest; the unchanged products share one inode
    publish(state, d["old"], na="0")
    na0_gen, previous = committed_names(state)
    assert previous == old_gen
    assert {n.rsplit("-", 1)[1] for n in os.listdir(state) if ".gen-" in n} == {old_gen, na0_gen}
    assert (state / f"{NAMES['reporting']}.gen-{na0_gen}").stat().st_ino == old_inodes["reporting"]
    assert (state / NAMES["noadapter"]).read_bytes() == gens["expected"]["old-na0"]["noadapter"]
    assert residue(state) == []
    # storage stays bounded: at most two generations of members, whatever the history
    for label in ("new", "third", "old", "new", "third"):
        publish(state, d[label])
        assert len({n.rsplit("-", 1)[1] for n in os.listdir(state) if ".gen-" in n}) == 2
        assert len(os.listdir(state)) <= 1 + 3 + 6


# ---------------------------------------------------------------------------
# Missing-record classification (finding 1).
def _legacy(state: Path, gens, key="old", products=PRODUCTS):
    for p in products:
        (state / NAMES[p]).write_bytes(gens["expected"][key][p])


def _i2_state_without(state: Path, gens, remove):
    publish(state, gens["dirs"]["old"])
    for name in os.listdir(state):
        if remove(name):
            os.unlink(state / name)


# What the publisher does with each record-less `_state` (classify_publication):
# it supersedes a pristine state, a complete and consistent snapshot and the
# residue a publication of this barcode leaves; it refuses everything else
# before any write, leaving every byte, inode, mode and time as it was.
SUPERSEDED, REFUSED = "superseded", "refused"
MISSING_RECORD = {
    # name: (builder(state, gens), legitimate outcome of every reader, publisher)
    "pristine": (lambda s, g: None, "absent", SUPERSEDED),
    "complete_legacy": (lambda s, g: _legacy(s, g), "old", SUPERSEDED),
    "complete_legacy_noadapter_header_only": (lambda s, g: _legacy(s, g, "old-na0"), "old-na0", SUPERSEDED),
    "partial_reporting": (lambda s, g: _legacy(s, g, products=("reporting",)), "FAIL_CLOSED", REFUSED),
    "partial_public": (lambda s, g: _legacy(s, g, products=("public",)), "FAIL_CLOSED", REFUSED),
    "partial_noadapter": (lambda s, g: _legacy(s, g, products=("noadapter",)), "FAIL_CLOSED", REFUSED),
    "partial_reporting_public": (lambda s, g: _legacy(s, g, products=("reporting", "public")), "FAIL_CLOSED", REFUSED),
    "partial_reporting_noadapter": (lambda s, g: _legacy(s, g, products=("reporting", "noadapter")), "FAIL_CLOSED", REFUSED),
    "partial_public_noadapter": (lambda s, g: _legacy(s, g, products=("public", "noadapter")), "FAIL_CLOSED", REFUSED),
    "mixed_old_new_public_names": (lambda s, g: (_legacy(s, g), (s / NAMES["public"]).write_bytes(g["expected"]["new"]["public"])),
                                   "FAIL_CLOSED", REFUSED),
    "mixed_noadapter_generation": (lambda s, g: (_legacy(s, g), (s / NAMES["noadapter"]).write_bytes(g["expected"]["new"]["noadapter"])),
                                   "FAIL_CLOSED", REFUSED),
    "legacy_reporting_seal_broken": (lambda s, g: (_legacy(s, g), (s / NAMES["reporting"]).write_bytes(
        g["expected"]["old"]["reporting"].replace(b"#END\t", b"#END\t9", 1))), "FAIL_CLOSED", REFUSED),
    "legacy_foreign_schema": (lambda s, g: (_legacy(s, g), (s / NAMES["reporting"]).write_bytes(b"read_id\tx\n")),
                              "FAIL_CLOSED", REFUSED),
    # the public names of a deleted record are a consistent snapshot: superseded, never served
    "commit_deleted": (lambda s, g: _i2_state_without(s, g, lambda n: n == RECORD), "FAIL_CLOSED", SUPERSEDED),
    "commit_and_public_names_deleted": (lambda s, g: _i2_state_without(s, g, lambda n: ".gen-" not in n),
                                        "FAIL_CLOSED", SUPERSEDED),
    "abandoned_generation_members": (lambda s, g: _i2_state_without(s, g, lambda n: ".gen-" not in n or "pretax" in n),
                                     "FAIL_CLOSED", SUPERSEDED),
    "lock_residue": (lambda s, g: (_legacy(s, g), (s / LOCK).write_text("")), "FAIL_CLOSED", SUPERSEDED),
    "i2_temp_residue": (lambda s, g: (_legacy(s, g), (s / f".r4d-publish-{BARCODE}-zzzzzz").write_text("x")),
                        "FAIL_CLOSED", SUPERSEDED),
    "r4d_temp_residue": (lambda s, g: (_legacy(s, g), (s / ".r4d-publish-Ab12_z").write_text("x")), "FAIL_CLOSED", REFUSED),
    "sequential_backup_residue": (lambda s, g: (_legacy(s, g), (s / NAMES["public"]).rename(s / f"{NAMES['public']}.bak.4242")),
                                  "FAIL_CLOSED", REFUSED),
    "same_inode_backup_residue": (lambda s, g: (_legacy(s, g), os.link(s / NAMES["public"], s / f"{NAMES['public']}.bak.4242")),
                                  "FAIL_CLOSED", REFUSED),
    "record_backup_residue": (lambda s, g: (_legacy(s, g), (s / f"{RECORD}.bak.4242").write_text("x")), "FAIL_CLOSED", REFUSED),
    "malformed_generation_member": (lambda s, g: (_legacy(s, g), (s / f"{NAMES['public']}.gen-zz").write_text("x")),
                                    "FAIL_CLOSED", SUPERSEDED),
    "malformed_backup_name": (lambda s, g: (_legacy(s, g), (s / f"{NAMES['reporting']}.bak.x").write_text("x")),
                              "FAIL_CLOSED", REFUSED),
    "public_name_is_symlink": (lambda s, g: (_legacy(s, g), (s / NAMES["public"]).unlink(),
                                             (s / "elsewhere").write_bytes(g["expected"]["old"]["public"]),
                                             os.symlink("elsewhere", s / NAMES["public"])), "FAIL_CLOSED", REFUSED),
    "public_name_is_directory": (lambda s, g: (_legacy(s, g, products=("reporting", "noadapter")),
                                               (s / NAMES["public"]).mkdir()), "FAIL_CLOSED", REFUSED),
}


@pytest.mark.parametrize("case", list(MISSING_RECORD))
def test_missing_record_is_legitimate_only_when_pristine_or_complete_legacy(tmp_path, gens, render, case):
    build, outcome, publisher = MISSING_RECORD[case]
    state = new_state(tmp_path)
    build(state, gens)
    assert not (state / RECORD).exists()
    got = reader_generations(state, tmp_path / "i", gens, render)
    expect = dict.fromkeys(got, outcome)
    if outcome == "old-na0":
        expect["round_json"] = "old"  # the round JSON does not read the no-adapter table
    assert got == expect, (case, got, sorted(os.listdir(state)))
    if outcome == "FAIL_CLOSED":
        # the diagnostic says what the publisher will do with the state
        error = resolve(state)["error"]
        assert ("rerun the reporting task" in error) == (publisher == SUPERSEDED), error
        assert ("remediate it explicitly" in error) == (publisher == REFUSED) or "not a regular file" in error, error
    before = tree_snapshot(state.parent)
    result = publish(state, gens["dirs"]["new"], check=False)
    if publisher == REFUSED:
        # refused before any write: every byte, inode, mode and time is kept
        assert result.returncode != 0 and "publication refused" in result.stderr, result.stderr
        assert tree_snapshot(state.parent) == before
        return
    assert result.returncode == 0, result.stderr
    assert reader_generations(state, tmp_path / "r", gens, render) == dict.fromkeys(got, "new")
    assert residue(state) == []


def test_deleting_the_commit_record_never_restores_legacy_mode(tmp_path, gens, render):
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["old"])
    publish(state, gens["dirs"]["new"])
    (state / RECORD).unlink()
    # the public names hold a complete, consistent generation, yet no reader serves it
    assert present_generation(state, gens) == "new"
    got = reader_generations(state, tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, "FAIL_CLOSED"), got
    assert "deleted commit record" in resolve(state)["error"]


def test_directories_other_than_state_are_ordinary_until_they_hold_an_artifact(tmp_path, gens):
    """A round directory's tables (no `_state`, no R4-I2 artifact) are ordinary
    files for every reader; any artifact places the directory under the authority."""
    round_dir = tmp_path / "round_7"
    round_dir.mkdir()
    (round_dir / NAMES["public"]).write_bytes(gens["expected"]["new"]["public"])
    (round_dir / NAMES["reporting"]).write_bytes(gens["expected"]["new"]["reporting"])
    res = resolve(round_dir)
    assert res["ok"] and res["mode"] == "ungoverned"
    obs = round_json(round_dir, tmp_path / "rj", gens["dirs"]["new"])
    assert obs["rc"] == 0 and obs["pretax"] == gens["rj"]["new"]["pretax"]
    (round_dir / f"{NAMES['public']}.gen-{'a' * 64}").write_text("x")
    assert not resolve(round_dir)["ok"]
    assert round_json(round_dir, tmp_path / "rj2", gens["dirs"]["new"])["rc"] != 0


# ---------------------------------------------------------------------------
# Interrupted first publication, migration and publication (real signals).
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
def test_first_publication_interrupted_anywhere_never_reads_as_first_run(tmp_path, gens, render, interrupt_env, sig):
    empty = new_state(tmp_path, "empty")
    events = publisher_boundaries(tmp_path, empty, gens["dirs"]["new"], interrupt_env)
    commit = next(i for i, e in enumerate(events, 1) if is_commit(e))
    for index in [1, 2, commit - 1, commit] + [i for i in range(commit + 1, len(events) + 1, 3)]:
        state = new_state(tmp_path / f"b{index}")
        kill_publisher(state, gens["dirs"]["new"], index, sig, tmp_path / f"b{index}", interrupt_env)
        res = resolve(state)
        if index < commit:
            assert not res["ok"], (index, events[index - 1], res)
            assert round_json(state, tmp_path / f"b{index}" / "rj", gens["dirs"]["new"])["rc"] != 0
        else:
            assert res["ok"] and res["mode"] == "committed", (index, res)
            assert resolved_generation(state, gens) == "new"
        assert present_generation(state, gens) in ("none", "new"), (index, sorted(os.listdir(state)))
        publish(state, gens["dirs"]["new"])
        assert resolved_generation(state, gens) == "new" and residue(state) == []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
def test_migration_interrupted_at_every_boundary(tmp_path, gens, render, interrupt_env, sig):
    """A complete legacy snapshot is served until the migrating publication
    starts; interrupted before its commit it fails closed (never legacy), after
    it the new generation is authoritative; the retry converges."""
    seed = new_state(tmp_path, "seed")
    _legacy(seed, gens)
    assert resolved_generation(seed, gens) == "old"
    control = new_state(tmp_path, "control")
    _legacy(control, gens)
    publish(control, gens["dirs"]["new"])
    events = publisher_boundaries(tmp_path, seed, gens["dirs"]["new"], interrupt_env)
    commit = next(i for i, e in enumerate(events, 1) if is_commit(e))
    for index in range(1, len(events) + 1, 1 if sig == signal.SIGKILL else 2):
        work = tmp_path / f"b{index}"
        state = work / "s" / "_state"
        clone_state(seed, state)
        kill_publisher(state, gens["dirs"]["new"], index, sig, work, interrupt_env)
        got = resolved_generation(state, gens)
        assert got == ("FAIL_CLOSED" if index < commit else "new"), (index, events[index - 1], got, sorted(os.listdir(state)))
        assert coherent(present_generation(state, gens)), (index, sorted(os.listdir(state)))
        publish(state, gens["dirs"]["new"])
        assert {k: v[0] for k, v in snapshot(state).items()} == {k: v[0] for k, v in snapshot(control).items()}
        assert residue(state) == []


PUBLICATION_BOUNDARIES = {
    # name: (selector over the traced events (1-based index), generation authoritative afterwards)
    "before_any_write": (lambda ev: next(i for i, e in enumerate(ev, 1) if e == "pre tempfile"), "old"),
    "after_first_member": (lambda ev: next(i for i, e in enumerate(ev, 1) if e.startswith("post link") and ".gen-" in e), "old"),
    "after_last_member": (lambda ev: max(i for i, e in enumerate(ev, 1) if e.startswith("post link") and ".gen-" in e), "old"),
    "after_record_backup": (lambda ev: next(i for i, e in enumerate(ev, 1) if e.startswith("post rename") and ".commit.bak." in e), "old"),
    "immediately_before_commit": (lambda ev: next(i for i, e in enumerate(ev, 1) if e.startswith("pre rename") and e.endswith(RECORD)), "old"),
    "immediately_after_commit": (lambda ev: next(i for i, e in enumerate(ev, 1) if is_commit(e)), "new"),
    "after_first_retraction": (lambda ev: next(i for i, e in enumerate(ev, 1) if e.startswith("post rename") and ".bak." in e
                                               and ".commit.bak." not in e), "new"),
    "after_last_retraction": (lambda ev: max(i for i, e in enumerate(ev, 1) if e.startswith("post rename") and ".bak." in e
                                             and ".commit.bak." not in e), "new"),
    "after_first_projection": (lambda ev: next(i for i, e in enumerate(ev, 1) if e.startswith("post rename") and ".r4d-publish-" in e
                                               and e.rsplit(" => ", 1)[1].endswith(("_rpt.txt", "_v1.tsv"))), "new"),
    "during_cleanup": (lambda ev: next(i for i, e in enumerate(ev, 1) if e.startswith("post unlink") and ".bak." in e), "new"),
}


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
@pytest.mark.parametrize("boundary", list(PUBLICATION_BOUNDARIES))
def test_process_death_leaves_one_complete_generation(tmp_path, gens, render, interrupt_env, boundary, sig):
    d = gens["dirs"]
    seed = new_state(tmp_path, "seed")
    publish(seed, d["old"])
    control = tmp_path / "control" / "_state"
    clone_state(seed, control)
    publish(control, d["new"])
    events = publisher_boundaries(tmp_path, seed, d["new"], interrupt_env)
    select, authoritative = PUBLICATION_BOUNDARIES[boundary]
    state = tmp_path / "state" / "_state"
    clone_state(seed, state)
    point = kill_publisher(state, d["new"], select(events), sig, tmp_path, interrupt_env)
    present = present_generation(state, gens)
    got = reader_generations(state, tmp_path / "interrupted", gens, render)
    # the backup follows the record like every reader, retracted public names or not (F3)
    assert got == dict.fromkeys(got, authoritative), (point, got, sorted(os.listdir(state)))
    # the public names present never mix generations (finding 3)
    assert coherent(present), (point, present, sorted(os.listdir(state)))
    publish(state, d["new"])
    assert reader_generations(state, tmp_path / "retried", gens, render) == dict.fromkeys(got, "new")
    assert residue(state) == []
    retried = snapshot(state)
    assert {k: v[0] for k, v in retried.items()} == {k: v[0] for k, v in snapshot(control).items()}
    publish(state, d["new"])
    assert snapshot(state) == retried


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
def test_public_names_never_mix_at_any_post_commit_boundary(tmp_path, gens, interrupt_env, sig):
    """Finding 3: at every boundary from the commit to the end of the
    publication, the public names that exist hold one generation (old before
    retraction, new after projection, absent in between), with mode 0600."""
    d = gens["dirs"]
    seed = new_state(tmp_path, "seed")
    publish(seed, d["old"])
    events = publisher_boundaries(tmp_path, seed, d["new"], interrupt_env)
    commit = next(i for i, e in enumerate(events, 1) if is_commit(e))
    seen = set()
    for index in range(commit, len(events) + 1):
        work = tmp_path / f"b{index}"
        state = work / "s" / "_state"
        clone_state(seed, state)
        kill_publisher(state, d["new"], index, sig, work, interrupt_env)
        got = present_generation(state, gens)
        assert coherent(got), (index, events[index - 1], got)
        seen.add(got)
        for p in PRODUCTS:
            if (state / NAMES[p]).exists():
                assert (state / NAMES[p]).stat().st_mode & 0o777 == 0o600
        assert resolved_generation(state, gens) == "new"
    assert {"old", "new", "none"} <= seen


# ---------------------------------------------------------------------------
def _validly_sealed_with_public_name(state: Path, name: str):
    """Rewrite the record so that its public entry names `name`, with a correct
    generation id and seal, and plant every member that record then names (the
    public one at `state / name`, even outside the directory): only the entry
    name check can reject it."""
    lines = (state / RECORD).read_text().split("\n")
    generation = lines[1].split("\t")[1]
    entries = [line.split("\t") for line in lines[3:6]]
    entries[1][1] = name
    products = "".join("\t".join(e) + "\n" for e in entries)
    new_generation = sha(products.encode())
    head = f"#RTB-R4D-CUMULATIVE\t1\ngeneration\t{new_generation}\nprevious\tNA\n{products}"
    for (_, member_name, _, _), p in zip(entries, PRODUCTS):
        (state / f"{member_name}.gen-{new_generation}").write_bytes((state / f"{NAMES[p]}.gen-{generation}").read_bytes())
    (state / RECORD).write_text(head + f"#END\t{sha(head.encode())}\n")


def _damage(state: Path, kind: str, gens):
    generation, _ = committed_names(state)
    record = state / RECORD
    copy = state / f"{NAMES['public']}.gen-{generation}"
    if kind == "broken_seal":
        record.write_bytes(record.read_bytes().replace(b"previous\tNA", b"previous\t" + b"0" * 64))
    elif kind == "generation_mismatch":
        lines = record.read_text().split("\n")
        lines[1] = "generation\t" + "f" * 64
        head = "\n".join(lines[:6]) + "\n"
        record.write_text(head + f"#END\t{sha(head.encode())}\n")
    elif kind == "foreign_barcode_entry":
        _validly_sealed_with_public_name(state, "OTHER_blast_otu_pretax_rpt.txt")
    elif kind == "path_traversal_entry":
        _validly_sealed_with_public_name(state, "../escape")
    elif kind == "missing_copy":
        (state / NAMES["public"]).unlink()
        copy.unlink()
        (state / NAMES["public"]).write_bytes(gens["expected"]["new"]["public"])
    elif kind == "same_size_byte_flip":
        data = bytearray(copy.read_bytes())
        data[-2] = ord("X") if data[-2] != ord("X") else ord("Y")
        (state / NAMES["public"]).unlink()
        copy.unlink()
        copy.write_bytes(bytes(data))
    elif kind == "other_generation_bytes":
        (state / NAMES["public"]).unlink()
        copy.unlink()
        copy.write_bytes(gens["expected"]["old"]["public"])
    elif kind == "member_is_symlink":
        (state / NAMES["public"]).unlink()
        copy.rename(state / "elsewhere")
        os.symlink("elsewhere", copy)
    elif kind == "record_is_directory":
        record.unlink()
        record.mkdir()
    elif kind.startswith("backup_record_in_state"):  # as an earlier candidate's restore left it
        text = record.read_text().replace("#RTB-R4D-CUMULATIVE\t1", "#RTB-R4D-CUMULATIVE-BACKUP\t1")
        lines = text.split("\n")
        head = "\n".join(lines[:6]) + "\n"
        record.write_text(head + f"#END\t{sha(head.encode())}\n")
        if kind == "backup_record_in_state_without_sidecar":  # the bound sidecar bytes are nowhere in _state
            gen = lines[1].split("\t")[1]
            (state / f"{NAMES['reporting']}.gen-{gen}").unlink()
            (state / NAMES["reporting"]).unlink()


DAMAGE = ["broken_seal", "generation_mismatch", "foreign_barcode_entry", "path_traversal_entry", "missing_copy",
          "same_size_byte_flip", "other_generation_bytes", "member_is_symlink", "record_is_directory",
          "backup_record_in_state_without_sidecar"]


@pytest.mark.parametrize("kind", DAMAGE)
def test_damaged_authority_fails_closed_and_is_republished(tmp_path, gens, render, kind):
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["new"])
    _damage(state, kind, gens)
    got = reader_generations(state, tmp_path / "i", gens, render)
    # never a fallback to the public names, which still hold complete bytes
    assert got == dict.fromkeys(got, "FAIL_CLOSED"), got
    if kind == "record_is_directory":
        assert publish(state, gens["dirs"]["new"], check=False).returncode != 0
        assert residue(state) == []
        (state / RECORD).rmdir()
    publish(state, gens["dirs"]["new"])
    assert reader_generations(state, tmp_path / "r", gens, render) == dict.fromkeys(got, "new")
    assert residue(state) == []


def test_an_earlier_candidates_backup_record_in_state_is_completed_from_its_bound_sidecar(tmp_path, gens, render):
    """An earlier candidate's restore left its two-member backup record in `_state`; the
    sidecar bytes the record binds (size and SHA-256) are still there, as its member. The
    first reader completes the generation under the lock -- no byte is synthesized -- and
    every reader serves it; the state record replaces the backup record by one rename."""
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["new"])
    committed = (state / RECORD).read_text()
    _damage(state, "backup_record_in_state", gens)
    assert (state / RECORD).read_text().startswith("#RTB-R4D-CUMULATIVE-BACKUP\t1\n")
    got = reader_generations(state, tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, "new"), got
    assert (state / RECORD).read_text() == committed  # the same generation, sealed as a state record
    assert resolve(state)["kind"] == "state"
    publish(state, gens["dirs"]["new"])
    assert reader_generations(state, tmp_path / "r", gens, render) == dict.fromkeys(got, "new")


def test_readers_follow_the_record_when_public_names_lag(tmp_path, gens, render):
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["old"])
    old_gen, _ = committed_names(state)
    publish(state, gens["dirs"]["new"])
    # as after a kill right after the commit: the public names still name the old members
    for p in PRODUCTS:
        (state / NAMES[p]).unlink()
        os.link(state / f"{NAMES[p]}.gen-{old_gen}", state / NAMES[p])
    got = reader_generations(state, tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, "new"), got
    # as after a kill between retraction and projection: no public name at all
    for p in PRODUCTS:
        (state / NAMES[p]).unlink()
    got = reader_generations(state, tmp_path / "j", gens, render)
    # the backup resolves the record, not the names: never an empty backup (F3)
    assert got == dict.fromkeys(got, "new"), got
    publish(state, gens["dirs"]["new"])
    new_gen, _ = committed_names(state)
    assert all((state / NAMES[p]).stat().st_ino == (state / f"{NAMES[p]}.gen-{new_gen}").stat().st_ino for p in PRODUCTS)


FAULTS = [
    # (fault, arg, message, outcome)  outcome: restored | committed | succeeded
    ("tempfile", "1", "injected tempfile failure", "restored"),
    ("tempfile", "3", "injected tempfile failure", "restored"),
    ("write", "2", "R4-D: write", "restored"),
    ("close", "1", "R4-D: close", "restored"),
    ("sync_file", "", "R4-D: sync", "restored"),
    ("link_member", "", "R4-D: link", "restored"),
    ("sync_dir", "1", "sync directory", "restored"),
    ("commit_rename", "", "R4-D: commit", "restored"),
    ("sync_dir", "2", "sync directory", "restored"),
    ("retract_rename", "", "stage backup", "restored"),
    ("projection_rename", NAMES["public"], "R4-D: replace", "restored"),
    ("projection_rename+restore", NAMES["public"], "stays authoritative", "committed"),
    ("cleanup_unlink", "", "", "succeeded"),
]


@pytest.mark.parametrize("seed_kind", ["committed", "legacy"])
@pytest.mark.parametrize("fault,arg,message,outcome", FAULTS, ids=[f"{f}-{a}" for f, a, _, _ in FAULTS])
def test_synchronous_failure_controls(tmp_path, gens, render, seed_kind, fault, arg, message, outcome):
    wrapper = tmp_path / "fault.pl"
    wrapper.write_text(FAULT_WRAPPER, encoding="utf-8")
    state = new_state(tmp_path)
    if seed_kind == "committed":
        publish(state, gens["dirs"]["old"])
    else:
        _legacy(state, gens)
    before = snapshot(state)
    result = publish(state, gens["dirs"]["new"], check=False, wrapper=wrapper,
                     env_extra={"I2_FAULT": fault, "I2_FAULT_ARG": arg})
    if outcome == "succeeded":
        assert result.returncode == 0, result.stderr
        assert residue(state), "the injected cleanup failure leaves residue behind"
        expected = "new"
    else:
        assert result.returncode != 0 and message in result.stderr, result.stderr
        expected = "old" if outcome == "restored" else "new"
        if outcome == "restored":
            assert snapshot(state) == before, (fault, sorted(os.listdir(state)))
    present = present_generation(state, gens)
    assert coherent(present), sorted(os.listdir(state))
    got = reader_generations(state, tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, expected), got
    assert LOCK not in os.listdir(state)
    publish(state, gens["dirs"]["new"])
    assert reader_generations(state, tmp_path / "r", gens, render) == dict.fromkeys(got, "new")
    assert residue(state) == []


def test_concurrent_publishers_are_serialized(tmp_path, gens, interrupt_env):
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["old"])
    old_gen, _ = committed_names(state)
    reached = tmp_path / "reached"
    first = publish(state, gens["dirs"]["new"], popen=True,
                    env_extra={**interrupt_env, "I2_STOP_AT": "1", "I2_REACHED": str(reached)})  # holds the lock, stopped
    while not reached.exists():
        time.sleep(0.01)
    second = publish(state, gens["dirs"]["old"], na="0", popen=True)
    time.sleep(1.5)
    assert second.poll() is None, "the second publisher must wait for the lock"
    assert committed_names(state)[0] == old_gen
    os.kill(int(reached.read_text().split("\t")[0]), signal.SIGCONT)
    first.communicate(timeout=60)
    second.communicate(timeout=60)
    assert first.returncode == 0 and second.returncode == 0
    final, previous = committed_names(state)
    assert final != old_gen and previous not in (None, old_gen, final)
    assert resolve(state)["mode"] == "committed"
    assert (state / NAMES["noadapter"]).read_bytes() == gens["expected"]["old-na0"]["noadapter"]
    assert residue(state) == []


CURRENCY = r"""
use strict; use warnings;
my ($lib, $dir, $bc, $helper, @pubs) = @ARGV;
require "$lib/RTBioScan/R4DCumulative.pm";
my $r = RTBioScan::R4DCumulative::resolve($dir, $bc);
my @out = ($r->{mode}, RTBioScan::R4DCumulative::still_current($r));
for my $rep (@pubs) {
    system($^X, $helper, '--publish-cumulative', '--reporting', $rep, '--state-dir', $dir, '--barcode', $bc,
           '--noadapter-enabled', '1') == 0 or die "publish $rep";
    push @out, RTBioScan::R4DCumulative::still_current($r);
}
print join("\t", @out), "\n";
"""


def test_a_reader_survives_one_commit_and_fails_beyond_the_retention_window(tmp_path, gens, render):
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["old"])
    d = gens["dirs"]

    def probe(*reps):
        r = subprocess.run(["perl", "-e", CURRENCY, str(LIB), str(state), BARCODE, str(HELPER),
                            *[str(d[x] / NAMES["reporting"]) for x in reps]],
                           env={**os.environ, **ENV}, capture_output=True, text=True, check=True)
        return r.stdout.split()

    # resolved G(old); one further commit keeps its members, a second releases them
    assert probe("new", "third") == ["committed", "1", "1", "0"]
    # the renderer applies the same window
    res = render.resolve_r4d_cumulative([(state, "state")])[0]
    assert render.r4d_cumulative_still_current(res)
    publish(state, d["new"])
    assert render.r4d_cumulative_still_current(res)
    publish(state, d["old"])
    assert not render.r4d_cumulative_still_current(res)


def test_still_current_semantics(tmp_path, gens):
    state = new_state(tmp_path)
    script = ("use strict; use warnings; my ($lib, $dir, $bc, $action, @rest) = @ARGV;"
              "require \"$lib/RTBioScan/R4DCumulative.pm\"; my $r = RTBioScan::R4DCumulative::resolve($dir, $bc);"
              "if ($action eq 'publish') { system($^X, @rest) == 0 or die 'publish'; }"
              "elsif ($action eq 'unlink') { unlink $r->{paths}{public} or die 'unlink'; }"
              "elsif ($action eq 'touch') { utime(time, time + 7, $r->{paths}{public}) or die 'touch'; }"
              "elsif ($action eq 'appear') { open my $f, '>', \"$dir/$bc\\_blast_otu_pretax_rpt.txt\" or die; close $f; }"
              "print $r->{mode}, \"\\t\", RTBioScan::R4DCumulative::still_current($r), \"\\n\";")
    pub = [str(HELPER), "--publish-cumulative", "--reporting", str(gens["dirs"]["new"] / NAMES["reporting"]),
           "--state-dir", str(state), "--barcode", BARCODE, "--noadapter-enabled", "1"]

    def probe(action, *extra):
        r = subprocess.run(["perl", "-e", script, str(LIB), str(state), BARCODE, action, *extra],
                           env={**os.environ, **ENV}, capture_output=True, text=True, check=True)
        return r.stdout.strip()

    assert probe("none") == "absent\t1"
    assert probe("appear") == "absent\t0"            # a public name appeared while reading nothing
    (state / NAMES["public"]).unlink()
    assert probe("publish", *pub) == "absent\t0"     # a first commit invalidates an absent resolution
    assert probe("none") == "committed\t1"
    assert probe("unlink") == "committed\t0"         # a committed member vanishing invalidates it
    legacy = new_state(tmp_path, "legacy")
    _legacy(legacy, gens)
    state = legacy
    assert probe("none") == "legacy\t1"
    assert probe("touch") == "legacy\t0"             # a legacy public name changing invalidates it


def test_stale_residue_next_to_a_record_is_ignored_and_cleaned(tmp_path, gens, render):
    state = new_state(tmp_path)
    publish(state, gens["dirs"]["old"])
    (state / LOCK).write_text("")
    (state / f".r4d-publish-{BARCODE}-zzzzzz").write_text("x")
    (state / f"{NAMES['public']}.bak.99999").write_text("stale backup")
    (state / f"{NAMES['reporting']}.gen-{'e' * 64}").write_text("abandoned copy")
    (state / f"{NAMES['noadapter']}.gen-zz").write_text("malformed copy")
    assert reader_generations(state, tmp_path / "i", gens, render) == dict.fromkeys(
        ("resolver", "round_json", "render", "backup"), "old")
    publish(state, gens["dirs"]["new"])
    assert residue(state) == [] and not any(n.endswith(("e" * 64, ".gen-zz")) for n in os.listdir(state))


# ---------------------------------------------------------------------------
# report_run_json.pl and report_render.py
def _run_json_state(root: Path):
    state = root / "results" / "temp" / "ongoing" / "state" / "state1" / "_state"
    state.mkdir(parents=True)
    rows = [{"schema_version": "1.6", "run_id": "runA", "barcode": BARCODE, "state_id": "state1",
             "round_barcode": f"round_00{i}", "timestamp_utc": f"2026-03-06T00:0{i}:00Z",
             "markers": {"order": ["COI"], "target_taxa_by_marker": {"COI": "Metazoa"}}} for i in (1, 2)]
    (state / "report_history.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    (state / f"{BARCODE}_read_info_rpt.txt").write_text(
        "read_id\tfilename\trun_id\tbarcode\tfast_length\tfast_mean_qscore\thac_length\thac_mean_qscore\tsup_length\tsup_mean_qscore\n"
        + "".join(f"r{i}\tr{i}.pod5\trunA\tbc\t100\t10\t100\t12\tNA\tNA\n" for i in (1, 2, 3)), encoding="utf-8")
    (state / f"{BARCODE}_on_target_rpt.txt").write_text(
        "read_id\tqc_filter\ton_target_kingdom\n" + "".join(f"r{i}\tIN\tON_TARGET\n" for i in (1, 2, 3)), encoding="utf-8")
    (state / f"{BARCODE}_demux_annotation_cache.tsv").write_text(
        "read_id\tbarcode_by_homology\tbasecalling_model\tsample\tplatform\tsampling_method\tsubsample\treplicate\tidentity_scope\tidentity_value\n"
        + "".join(f"r{i}\tCOI\thac\tsample_A_1\tnanopore\tgrab\tsub1\t1\tsample\tsample_A_1\n" for i in (1, 2, 3)), encoding="utf-8")
    (state / f"{BARCODE}_blast_unassigned_current.list").write_text("", encoding="utf-8")
    return state


def _public_table(read_ids):
    return PRETAX_HEADER + "".join(f"{r}\tCOI\thac\tsample_A_1\thit\t123\t100\t99\tOTUB_1-COI\t111\tMetazoa\tP\tC\tO\tF\tG\tS1\n"
                                   for r in read_ids).encode()


def _commit_by_oracle(state: Path, texts):
    """Write a committed generation exactly as the protocol specifies (independent oracle)."""
    products = "".join(f"{p}\t{NAMES[p]}\t{len(texts[p])}\t{sha(texts[p])}\n" for p in PRODUCTS)
    generation = sha(products.encode())
    head = f"#RTB-R4D-CUMULATIVE\t1\ngeneration\t{generation}\nprevious\tNA\n{products}"
    for p in PRODUCTS:
        (state / f"{NAMES[p]}.gen-{generation}").write_bytes(texts[p])
    (state / RECORD).write_text(head + f"#END\t{sha(head.encode())}\n", encoding="utf-8")
    return generation


def _run_json(root: Path, state: Path):
    out = root / "run_report.json"
    if out.exists():
        out.unlink()
    cmd = ["perl", str(REPORT_RUN_JSON), "--history", str(state / "report_history.jsonl"), "--out", str(out),
           "--run-id", "runA", "--barcode", BARCODE, "--state-id", "state1", "--outdir", "results",
           "--report-rel-path", "runs/runA/report.html"]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return result, (json.loads(out.read_text(encoding="utf-8")) if result.returncode == 0 else None)


def test_report_run_json_reads_only_the_committed_generation(tmp_path):
    state = _run_json_state(tmp_path)
    # pristine: nothing published yet, the read-fate block is simply absent
    result, data = _run_json(tmp_path, state)
    assert result.returncode == 0 and "run_status_read_fate" not in data, result.stderr
    committed = _public_table(["r1", "r2", "r3"])
    _commit_by_oracle(state, {"reporting": b"reporting\n", "public": committed, "noadapter": b"noadapter\n"})
    (state / NAMES["public"]).write_bytes(_public_table(["r1"]))  # a lagging public name
    result, data = _run_json(tmp_path, state)
    assert result.returncode == 0, result.stderr
    assert data["run_status_read_fate"]["blast_seen_reads"] == 3
    # the record deleted: the members and the public name remain, and nothing is read
    (state / RECORD).unlink()
    result, _ = _run_json(tmp_path, state)
    assert result.returncode != 0 and "deleted commit record" in result.stderr
    # a lone public table (a partial snapshot) is never read as legacy
    for n in os.listdir(state):
        if ".gen-" in n:
            (state / n).unlink()
    result, _ = _run_json(tmp_path, state)
    assert result.returncode != 0 and "incomplete pre-R4-D cumulative snapshot" in result.stderr
    # an interrupted sequential legacy publication fails closed
    (state / NAMES["public"]).rename(state / f"{NAMES['public']}.bak.77")
    result, _ = _run_json(tmp_path, state)
    assert result.returncode != 0 and "governed residue is present" in result.stderr


# Loaded through PERL5OPT: when the process is the report_round_json.pl child
# of report_run_json.pl, the snapshot is republished twice before it reads.
REPUBLISH_DURING_CHILD = r"""
package I2TestRepublish;
if (($0 // '') =~ /report_round_json\.pl\z/ && !$ENV{I2_TEST_REPUBLISHED}) {
    local $ENV{I2_TEST_REPUBLISHED} = 1;
    for my $rep (split /,/, $ENV{I2_TEST_REPORTINGS}) {
        system($^X, $ENV{I2_TEST_HELPER}, '--publish-cumulative', '--reporting', $rep, '--state-dir', $ENV{I2_TEST_STATE},
               '--barcode', 'RTBioScan', '--noadapter-enabled', '1') == 0 or die "republish failed\n";
    }
}
1;
"""


@pytest.mark.parametrize("commits", [1, 2])
def test_report_run_json_checks_currency_after_its_child_read(tmp_path, gens, commits):
    """report_run_json.pl resolves the committed generation, its child reads the
    member; one commit meanwhile keeps that member (retention), two release it
    and the run report fails instead of publishing a read fate from nothing."""
    state = _run_json_state(tmp_path)
    publish(state, gens["dirs"]["old"])
    hook = tmp_path / "hook"
    hook.mkdir()
    (hook / "I2TestRepublish.pm").write_text(REPUBLISH_DURING_CHILD, encoding="utf-8")
    reps = [gens["dirs"][x] / NAMES["reporting"] for x in ("new", "third")[:commits]]
    out = tmp_path / "run_report.json"
    cmd = ["perl", str(REPORT_RUN_JSON), "--history", str(state / "report_history.jsonl"), "--out", str(out),
           "--run-id", "runA", "--barcode", BARCODE, "--state-id", "state1", "--outdir", "results",
           "--report-rel-path", "runs/runA/report.html"]
    env = {**os.environ, **ENV, "PERL5OPT": f"-I{hook} -MI2TestRepublish", "I2_TEST_HELPER": str(HELPER),
           "I2_TEST_STATE": str(state), "I2_TEST_REPORTINGS": ",".join(map(str, reps))}
    result = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    last = gens["expected"][("new", "third")[commits - 1]]
    products = "".join(f"{p}\t{NAMES[p]}\t{len(last[p])}\t{sha(last[p])}\n" for p in PRODUCTS)
    assert committed_names(state)[0] == sha(products.encode())  # the republications really happened
    if commits == 1:
        assert result.returncode == 0 and "republished" not in result.stderr, result.stderr
    else:
        assert result.returncode != 0 and "republished while it was being read" in result.stderr, result.stderr


def test_report_render_rows_use_the_committed_generations(tmp_path, gens, render):
    results = tmp_path / "results"
    ongoing = results / "temp" / "ongoing" / "state" / "S1"
    state = ongoing / "_state"
    state.mkdir(parents=True)
    publish(state, gens["dirs"]["old"])
    old_gen, _ = committed_names(state)
    publish(state, gens["dirs"]["new"])
    for p in PRODUCTS:  # public names lag behind the record
        (state / NAMES[p]).unlink()
        os.link(state / f"{NAMES[p]}.gen-{old_gen}", state / NAMES[p])
    consensus = ongoing / "Consensus"
    consensus.mkdir()
    (consensus / "alpha_ITS2_Merged_Consensus.fasta").write_text(">alpha|c1|ITS2|reads-3|OTU=OTUB_4-ITS2\nACGTACGT\n")
    out = results / "report_html" / "runs" / "runA" / "report.html"
    rows = render.collect_consensus_sequence_rows("runA", out, "S1")
    assert len(rows) == 1 and "Porella arborisvitae" in rows[0]["otu_assignment"], rows
    # the results snapshot is read through its backup generation too
    (state / f"{BARCODE}_reads_time_rpt.txt").write_text("time\treads\n", encoding="utf-8")
    L = {"ongoing": results / "ongoing" / "state" / "S1", "current": results / "current" / "state" / "S1"}
    assert bash(section2_script(state, L["ongoing"])).returncode == 0
    assert bash(section7_script(L["ongoing"], L["current"])).returncode == 0
    tables = L["current"] / "tables"
    res = render.resolve_r4d_cumulative([(tables, "any")])[0]
    assert res["mode"] == "committed" and res["kind"] == "state"  # a replica of the state generation
    (tables / NAMES["public"]).unlink()  # a lagging destination name is never needed
    assert len(render.collect_consensus_sequence_rows("runA", out, "S1")) == 1
    (tables / f"{NAMES['public']}.gen-{res['generation']}").write_bytes(b"damaged\n")
    with pytest.raises(render.R4DCumulativeStateError):
        render.collect_consensus_sequence_rows("runA", out, "S1")
    (state / f"{NAMES['public']}.gen-{committed_names(state)[0]}").write_bytes(b"damaged\n")
    with pytest.raises(render.R4DCumulativeStateError):
        render.resolve_r4d_cumulative([(state, "state")])


# ---------------------------------------------------------------------------
# Backup destinations (finding 2).
def results_layout(root: Path):
    L = {"state": root / "temp" / "ongoing" / "state" / "S1" / "_state", "ongoing": root / "ongoing" / "state" / "S1",
         "current": root / "current" / "state" / "S1"}
    L["state"].mkdir(parents=True, exist_ok=True)
    (L["state"] / f"{BARCODE}_reads_time_rpt.txt").write_text("time\treads\n", encoding="utf-8")
    return L


def backup_round(L, check=True, env_extra=None):
    r2 = bash(section2_script(L["state"], L["ongoing"]), env_extra)
    if check:
        assert r2.returncode == 0, r2.stderr
    r7 = bash(section7_script(L["ongoing"], L["current"]), env_extra)
    if check:
        assert r7.returncode == 0, r7.stderr
    return r2, r7


def test_backup_destinations_hold_one_complete_replica(tmp_path, gens):
    """A backup is a replica of the state generation: the same record kind and
    generation, every member -- the reporting sidecar included -- and projections
    of the two public tables only, so a restore needs nothing else."""
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    backup_round(L)
    state_gen, _ = committed_names(L["state"])
    for dest in (L["ongoing"], L["current"] / "tables"):
        assert (dest / RECORD).read_text() == (L["state"] / RECORD).read_text()  # first generation: previous NA
        for p in PRODUCTS:
            member = dest / f"{NAMES[p]}.gen-{state_gen}"
            assert member.read_bytes() == gens["expected"]["old"][p]
            assert member.stat().st_mode & 0o777 == 0o600
        for p in PAIR:
            assert (dest / NAMES[p]).stat().st_ino == (dest / f"{NAMES[p]}.gen-{state_gen}").stat().st_ino
        assert not (dest / NAMES["reporting"]).exists()  # a destination projects the two public tables only
        assert pair_generation(dest, gens) == "old"
        res = resolve(dest)
        assert res["ok"] and res["kind"] == "state" and set(res["paths"]) == set(PRODUCTS)
    # replay: nothing changes in either destination
    before = {d: snapshot(d) for d in (L["ongoing"], L["current"] / "tables")}
    backup_round(L)
    assert {d: snapshot(d) for d in before} == before
    # retention: the current and the previous backup generation, whatever the history
    for label in ("new", "third", "old", "new"):
        publish(L["state"], gens["dirs"][label])
        backup_round(L)
        for dest in (L["ongoing"], L["current"] / "tables"):
            gens_on_disk = {n.rsplit("-", 1)[1] for n in os.listdir(dest) if ".gen-" in n}
            assert len(gens_on_disk) == 2 and not residue(dest), sorted(os.listdir(dest))
            assert pair_generation(dest, gens, keys=("old", "new", "third")) == label


def test_backup_sources_legacy_restored_and_ambiguous(tmp_path, gens):
    # a complete legacy _state: the validated snapshot is backed up as one generation
    L = results_layout(tmp_path / "legacy")
    _legacy(L["state"], gens)
    backup_round(L)
    assert pair_generation(L["current"] / "tables", gens) == "old"
    # a partial legacy _state fails the backup; the destination keeps its pair
    before = snapshot(L["ongoing"])
    (L["state"] / NAMES["reporting"]).unlink()
    r2, _ = backup_round(L, check=False)
    assert r2.returncode != 0 and "no round ledger" in r2.stderr  # R4-D projections are no pre-R4-D snapshot
    assert snapshot(L["ongoing"]) == before
    # ... and the publisher refuses to write over it (F)
    state_before = tree_snapshot(L["state"])
    assert publish(L["state"], gens["dirs"]["new"], check=False).returncode != 0
    assert tree_snapshot(L["state"]) == state_before
    # a damaged committed member fails the backup; the destination keeps its pair
    (L["state"] / NAMES["reporting"]).write_bytes(gens["expected"]["old"]["reporting"])  # complete again
    publish(L["state"], gens["dirs"]["new"])
    gen, _ = committed_names(L["state"])
    (L["state"] / NAMES["public"]).unlink()
    (L["state"] / f"{NAMES['public']}.gen-{gen}").write_bytes(b"damaged\n")
    (L["state"] / NAMES["noadapter"]).touch()
    r2, _ = backup_round(L, check=False)
    assert r2.returncode != 0 and "committed generation" in r2.stderr
    assert snapshot(L["ongoing"]) == before


def test_ungoverned_tables_are_synced_exactly_as_before(tmp_path, gens):
    for mode in ("rsync", "no-rsync"):
        env = {"BACKUP_SYNC_DISABLE_RSYNC": "1"} if mode == "no-rsync" else {}
        round_dir = tmp_path / mode / "round_3"
        round_dir.mkdir(parents=True)
        (round_dir / NAMES["public"]).write_bytes(b"round copy\n")
        (round_dir / NAMES["noadapter"]).write_bytes(b"round noadapter\n")
        (round_dir / "other_rpt.txt").write_text("other\n")
        dest = tmp_path / mode / "tables"
        script = (f"set -euo pipefail\nsource '{BACKUP_SYNC}'\nsync_changed_files '{dest}' "
                  + " ".join(f"'{round_dir / n}'" for n in (NAMES["public"], "other_rpt.txt", NAMES["noadapter"])))
        result = bash(script, env)
        assert result.returncode == 0, result.stderr
        assert sorted(os.listdir(dest)) == sorted([NAMES["public"], NAMES["noadapter"], "other_rpt.txt"])
        assert (dest / NAMES["public"]).read_bytes() == b"round copy\n"


def test_backup_lagging_destination_names_are_reconciled(tmp_path, gens):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    backup_round(L)
    (L["ongoing"] / NAMES["public"]).unlink()
    (L["ongoing"] / NAMES["noadapter"]).unlink()  # replaced, never rewritten in place: it shares the member's inode
    (L["ongoing"] / NAMES["noadapter"]).write_bytes(b"stray\n")
    assert pair_generation(L["ongoing"], gens) == "old"  # the record, never the names
    backup_round(L)
    assert present_generation(L["ongoing"], gens, PAIR) == "old"


def _backup_boundaries(tmp_path, seed_root, interrupt_env):
    probe = tmp_path / "probe"
    clone_state(seed_root, probe)
    L = results_layout(probe)
    trace = tmp_path / "backup-trace.tsv"
    return trace_events({"_trace": trace},
                        lambda e: bash(section2_script(L["state"], L["ongoing"]), {**interrupt_env, **e}))


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
def test_backup_interrupted_anywhere_leaves_one_pair_downstream(tmp_path, gens, interrupt_env, sig):
    seed_root = tmp_path / "seed"
    L = results_layout(seed_root)
    publish(L["state"], gens["dirs"]["old"])
    backup_round(L)
    publish(L["state"], gens["dirs"]["new"])
    events = _backup_boundaries(tmp_path, seed_root, interrupt_env)
    commit = next(i for i, e in enumerate(events, 1) if is_commit(e))
    for index in range(1, len(events) + 1):
        root = tmp_path / f"b{index}"
        clone_state(seed_root, root)
        L = results_layout(root)
        stop_and_signal(lambda e: subprocess.Popen(["/bin/bash", "-c", section2_script(L["state"], L["ongoing"])],
                                                    env={**os.environ, **ENV, **interrupt_env, "I2_STOP_AT": str(index), **e},
                                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                                    start_new_session=True),
                        root / "reached", sig)
        dest = pair_generation(L["ongoing"], gens)
        assert dest == ("old" if index < commit else "new"), (index, events[index - 1], dest)
        assert coherent(present_generation(L["ongoing"], gens, PAIR)), (index, sorted(os.listdir(L["ongoing"])))
        # the real downstream table copy: always one complete pair
        r7 = bash(section7_script(L["ongoing"], L["current"]))
        assert r7.returncode == 0, r7.stderr
        assert pair_generation(L["current"] / "tables", gens) in ("old", "new"), index
        assert coherent(present_generation(L["current"] / "tables", gens, PAIR))
        # retry converges
        backup_round(L)
        assert pair_generation(L["ongoing"], gens) == pair_generation(L["current"] / "tables", gens) == "new"
        assert not residue(L["ongoing"]) and not residue(L["current"] / "tables")


def restart_restore(outdir: Path, op="c", force="0"):
    env = {**os.environ, **ENV, "MODE": "restore", "OUTDIR": str(outdir), "LOCK_WAIT": "5", "RUN_NAME": "runR",
           "STATE_ID": "S1", "FORCE": force, "OPERATION_ID": op * 64}
    return subprocess.run(["/bin/bash", str(RESTART_HANDLER)], env=env, capture_output=True, text=True, check=False)


def test_restore_installs_the_backup_generation_with_its_sidecar(tmp_path, gens, render):
    """The restore installs the snapshot's generation into `_state` with its
    authority and sidecar: every reader works before another round, a failed
    round leaves it usable, and the next publication supersedes it."""
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    backup_round(L)
    publish(L["state"], gens["dirs"]["new"])  # the live state moves on; the snapshot stays at "old"
    snapshot_record = (L["current"] / "tables" / RECORD).read_text()
    result = restart_restore(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (L["state"] / RECORD).read_text() == snapshot_record  # the same generation, previous NA
    gen, _ = committed_names(L["state"])
    for p in PRODUCTS:
        member = L["state"] / f"{NAMES[p]}.gen-{gen}"
        assert member.read_bytes() == gens["expected"]["old"][p]
        assert (L["state"] / NAMES[p]).stat().st_ino == member.stat().st_ino
    got = reader_generations(L["state"], tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, "old"), got
    # the cumulative R4-D metrics come from the restored sidecar: not null
    assert round_json(L["state"], tmp_path / "rj", gens["dirs"]["new"])["cumulative"] == gens["rj"]["old"]["cumulative"]
    # a failed round publishes nothing: the restored generation stays authoritative
    assert publish(L["state"], tmp_path / "missing-round", check=False).returncode != 0
    assert reader_generations(L["state"], tmp_path / "f", gens, render) == dict.fromkeys(got, "old")
    publish(L["state"], gens["dirs"]["new"])
    assert reader_generations(L["state"], tmp_path / "r", gens, render) == dict.fromkeys(got, "new")
    assert residue(L["state"]) == []


# ---------------------------------------------------------------------------
# Filesystems: hard-link support is detected from explicit errors only.
LINK_CASES = [
    # (errno, pathconf LINK_MAX override, outcome)
    (errno.ENOTSUP, "", "copies"),
    (errno.EOPNOTSUPP, "", "copies"),
    (errno.ENOSYS, "", "copies"),
    (errno.EPERM, "1", "copies"),
    (errno.EMLINK, "1", "copies"),
    (errno.EPERM, "", "fails"),
    (errno.EMLINK, "", "fails"),
    (errno.EXDEV, "", "fails"),
    (errno.EIO, "", "fails"),
    (errno.EIO, "1", "fails"),
    (errno.EACCES, "", "fails"),
]


@pytest.mark.parametrize("err,link_max,outcome", LINK_CASES, ids=[f"{errno.errorcode[e]}-max{m or 'fs'}" for e, m, _ in LINK_CASES])
def test_link_failures_select_byte_copies_only_when_hard_links_are_unsupported(tmp_path, gens, interrupt_env, err, link_max, outcome):
    env = {**interrupt_env, "I2_LINK_ERRNO": str(err), "I2_LINK_MAX": link_max}
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    before = snapshot(L["state"])
    result = publish(L["state"], gens["dirs"]["new"], check=False, env_extra=env)
    if outcome == "fails":
        assert result.returncode != 0 and "link" in result.stderr, result.stderr
        assert snapshot(L["state"]) == before and residue(L["state"]) == []
        publish(L["state"], gens["dirs"]["new"])
        backup_round(L)
        r2 = bash(section2_script(L["state"], L["ongoing"] / "again"), env)
        assert r2.returncode != 0 and "link" in r2.stderr, r2.stderr
        return
    assert result.returncode == 0, result.stderr
    gen, _ = committed_names(L["state"])
    for p in PRODUCTS:
        member = L["state"] / f"{NAMES[p]}.gen-{gen}"
        assert (L["state"] / NAMES[p]).read_bytes() == member.read_bytes() == gens["expected"]["new"][p]
        assert (L["state"] / NAMES[p]).stat().st_ino != member.stat().st_ino
    assert resolved_generation(L["state"], gens) == "new"
    replay = snapshot(L["state"])
    publish(L["state"], gens["dirs"]["new"], env_extra=env)
    assert snapshot(L["state"]) == replay
    backup_round(L, env_extra=env)
    assert pair_generation(L["current"] / "tables", gens) == "new"
    assert residue(L["state"]) == [] and residue(L["ongoing"]) == []


@pytest.mark.parametrize("boundary", ["immediately_before_commit", "immediately_after_commit", "after_first_retraction",
                                      "after_first_projection"])
def test_process_death_without_hard_links(tmp_path, gens, render, interrupt_env, boundary):
    no_links = {**interrupt_env, "I2_LINK_ERRNO": str(errno.ENOTSUP)}
    seed = new_state(tmp_path, "seed")
    publish(seed, gens["dirs"]["old"], env_extra=no_links)
    events = publisher_boundaries(tmp_path, seed, gens["dirs"]["new"], interrupt_env, extra=no_links)
    select, authoritative = PUBLICATION_BOUNDARIES[boundary]
    state = tmp_path / "state" / "_state"
    clone_state(seed, state)
    kill_publisher(state, gens["dirs"]["new"], select(events), signal.SIGKILL, tmp_path, interrupt_env, extra=no_links)
    assert resolved_generation(state, gens) == authoritative
    assert coherent(present_generation(state, gens))
    publish(state, gens["dirs"]["new"], env_extra=no_links)
    assert reader_generations(state, tmp_path / "r", gens, render) == dict.fromkeys(
        ("resolver", "round_json", "render", "backup"), "new")
    assert residue(state) == []


def test_symlinks_are_never_used(tmp_path, gens, interrupt_env):
    """No part of the protocol creates or follows a symbolic link: a filesystem
    without symlinks is unaffected, and a forced symlink failure changes nothing."""
    L = results_layout(tmp_path)
    trace = tmp_path / "trace.tsv"
    env = {**interrupt_env, "I2_TRACE": str(trace), "I2_SYMLINK_ERRNO": str(errno.EPERM)}
    publish(L["state"], gens["dirs"]["old"], env_extra=env)
    publish(L["state"], gens["dirs"]["new"], env_extra=env)
    backup_round(L, env_extra=env)
    events = [line.split("\t", 2)[2] for line in trace.read_text().splitlines()]
    assert events and not any(e.startswith(("pre symlink ", "post symlink ")) for e in events)
    for d in (L["state"], L["ongoing"], L["current"] / "tables"):
        assert not any(p.is_symlink() for p in d.iterdir())
    assert pair_generation(L["current"] / "tables", gens) == "new"


# ---------------------------------------------------------------------------
# A concurrent first publication lands while report_round_json.pl reads a legacy
# snapshot: its reporting input is read before, its public input after the commit.
CONCURRENT_READ_WRAPPER = r"""
use strict;
use warnings;
my ($script, $helper, $publish_reporting, $state, @args) = @ARGV;
use FindBin ();
use File::Basename ();
$FindBin::Bin = File::Basename::dirname($helper);
require $helper;
{
    no warnings 'redefine';
    my $orig = \&RTBioScan::R4D::read_reporting;
    my $seen = 0;
    *RTBioScan::R4D::read_reporting = sub {
        my @r = $orig->(@_);
        # the first read of the snapshot is the resolver validating it; the
        # second is report_round_json.pl reading it
        if ($_[0] =~ m{\A\Q$state\E/} && ++$seen == 2) {
            system('perl', $helper, '--publish-cumulative', '--reporting', $publish_reporting, '--state-dir', $state,
                   '--barcode', 'RTBioScan', '--noadapter-enabled', '1') == 0 or die "concurrent publish failed\n";
        }
        return @r;
    };
}
@ARGV = @args;
$0 = $script;
do $script;
die $@ if $@;
"""


def test_a_first_commit_during_a_legacy_read_fails_closed(tmp_path, gens, render, monkeypatch):
    state = new_state(tmp_path)
    _legacy(state, gens)
    wrapper = tmp_path / "concurrent.pl"
    wrapper.write_text(CONCURRENT_READ_WRAPPER, encoding="utf-8")
    new_dir = gens["dirs"]["new"]
    out = tmp_path / "round.json"
    args = ["--run-id", "runA", "--barcode", BARCODE, "--round-barcode", "round_2", "--targets", "COI|ITS2",
            "--target-taxa", "Metazoa|Viridiplantae", "--timestamp-utc", "2026-09-23T00:00:00Z", "--out", str(out),
            "--blast-otu", str(new_dir / NAMES["public"]), "--blast-otu-reporting", str(new_dir / NAMES["reporting"]),
            "--blast-otu-cumulative", str(state / NAMES["public"]),
            "--blast-otu-reporting-cumulative", str(state / NAMES["reporting"])]
    result = subprocess.run(["perl", str(wrapper), str(REPORT_ROUND_JSON), str(HELPER), str(new_dir / NAMES["reporting"]),
                             str(state), *args], env={**os.environ, **ENV}, capture_output=True, text=True, check=False)
    assert (state / RECORD).exists(), result.stderr  # the concurrent publication really committed
    assert result.returncode != 0 and "republished while it was being read" in result.stderr, result.stderr
    assert not out.exists()
    # report_render: a commit between resolution and reading is detected the same way
    state2 = tmp_path / "results" / "temp" / "ongoing" / "state" / "S1" / "_state"
    state2.mkdir(parents=True)
    _legacy(state2, gens)
    (state2.parent / "Consensus").mkdir()
    (state2.parent / "Consensus" / "x_Merged_Consensus.fasta").write_text(">alpha|c1|COI|reads-3|OTU=OTUB_0-COI\nACGT\n")
    resolve_fn = render.resolve_r4d_cumulative

    def resolve_then_publish(requests, *a, **k):
        res = resolve_fn(requests, *a, **k)
        publish(state2, gens["dirs"]["new"])
        return res

    monkeypatch.setattr(render, "resolve_r4d_cumulative", resolve_then_publish)
    with pytest.raises(render.R4DCumulativeStateError, match="republished while it was being read"):
        render.collect_consensus_sequence_rows("runA", tmp_path / "results" / "report_html" / "runs" / "runA" / "report.html", "S1")


def test_main_nf_backs_up_the_generation_by_name_before_the_per_file_sync():
    """F3: backup_update_and_clean names each cumulative source and barcode
    explicitly (section 2: `_state` -> results ongoing; section 7: results ongoing
    -> results current), before the per-file syncs it keeps unchanged, so no glob
    or existence test decides whether the generation is backed up."""
    s2 = main_nf_fragment('rpts=( "\\$STATE_TMP"/*_rpt.txt )', "if (( \\${#pngs_all[@]} ))")
    call2 = f'r4d_backup_generation "$ONGOING_FINAL" "$STATE_TMP" "{BARCODE}"'
    assert s2.count(call2) == 1 and s2.index(call2) < s2.index('sync_changed_files "$ONGOING_FINAL" "${rpts_plain[@]}"')
    s7 = main_nf_fragment('tables=( "\\$ONGOING_FINAL"/*.txt "\\$ONGOING_FINAL"/*.tsv "\\$ONGOING_FINAL"/*.csv \\',
                          "# ---- Create to_figures/")
    call7 = f'r4d_backup_generation "$CURRENT_ROOT/tables" "$ONGOING_FINAL" "{BARCODE}"'
    assert s7.count(call7) == 1 and s7.index(call7) < s7.index('sync_changed_files "$CURRENT_ROOT/tables" "${tables[@]}"')
    assert MAIN_NF.read_text(encoding="utf-8").count("r4d_backup_generation") == 2


# ---------------------------------------------------------------------------
# Pre-R4-D compatibility (owner adjudication). An existing outdir written by the
# pre-R4-D pipeline is produced here by that pipeline's own immutable code: the
# `_reporting_blast_pretax` script and the completed-round ledger fragment of
# the R4-C commit (identical to main 71c8de1 for both), taken verbatim from its
# main.nf in Git, rendered as Nextflow renders a Groovy GString (checked
# byte-for-byte against Nextflow's own .command.sh in the evidence) and run by
# /bin/bash round by round. Only the round-lock fencing guard it sources is
# replaced by no-op functions: the lock is orthogonal to the cumulative tables.
R4C_COMMIT = "111c16672f08f3a65467aa9c846df3d598aa8f02"
LEGACY_HEADER = "#RTB-PRE-R4D-LEGACY\t1"
LEGACY_BACKUP_HEADER = "#RTB-PRE-R4D-LEGACY-BACKUP\t1"
R4C_GUARD_STUB = "rtbioscan_round_lock_pin() { :; }\nrtbioscan_round_lock_unpin() { :; }\n"
GROOVY_ESCAPES = {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "\\": "\\", "$": "$", '"': '"', "'": "'"}


def render_groovy(src, env):
    """A Groovy GString as Nextflow renders a process script: backslash escapes,
    line continuations and ${...} interpolation of the given names only."""
    out, i = [], 0
    while i < len(src):
        c = src[i]
        if c == "\\" and i + 1 < len(src):
            if src[i + 1] != "\n":
                out.append(GROOVY_ESCAPES[src[i + 1]])
            i += 2
            continue
        if src.startswith("${", i):
            j = src.index("}", i)
            out.append(str(env[src[i + 2:j].strip()]))
            i = j + 1
            continue
        assert not (c == "$" and i + 1 < len(src) and (src[i + 1].isalpha() or src[i + 1] == "_")), src[i:i + 40]
        out.append(c)
        i += 1
    return "".join(out)


def r4c_script_block(main_nf, process):
    body = main_nf[main_nf.index(f"process {process} {{"):]
    first = body.index('"""') + 3
    return body[first:body.index('"""', first)]


def r4c_done_fragment(main_nf):
    backup = main_nf[main_nf.index("process backup_update_and_clean {"):]
    start = backup.index("\t\t\t# -- §3: done_pod5 append")
    return backup[start:backup.index("\t\tDELETE_INPUT_RAW=", start)]


@pytest.fixture(scope="module")
def r4c(tmp_path_factory):
    """The immutable pre-R4-D production code (bin/ and main.nf), from Git objects."""
    code = tmp_path_factory.mktemp("r4c-code")
    archive = subprocess.run(["git", "-C", str(REPO_ROOT), "archive", R4C_COMMIT, "bin", "main.nf"],
                             capture_output=True, check=False)
    assert archive.returncode == 0, f"the pre-R4-D commit {R4C_COMMIT} is required: {archive.stderr[-300:]!r}"
    subprocess.run(["tar", "-x", "-C", str(code)], input=archive.stdout, check=True)
    (code / "bin" / "round_lock_process_guard.sh").write_text(R4C_GUARD_STUB, encoding="utf-8")
    return code


def r4c_read(uuid, marker="COI", adapter="ALPHA_COI", otu_n=1):
    if marker == "COI":
        lineage, taxid = ["Metazoa", "Arthropoda", "Insecta", "Diptera", "Drosophilidae", "Drosophila",
                          "Drosophila melanogaster"], "7227"
    else:
        lineage, taxid = ["Viridiplantae", "Streptophyta", "Magnoliopsida", "Fabales", "Fabaceae", "Pisum",
                          "Pisum sativum"], "3888"
    return {"uuid": uuid, "marker": marker, "adapter": adapter, "otu": f"OTUB_{otu_n}-{marker}", "taxid": taxid,
            "lineage": lineage}


# COI, ITS2 and no-adapter reads over five rounds, one of them failed (the
# placeholders) and one empty.
PRE_R4D_ROUNDS = [
    ("FAX00001_pass_0", [r4c_read("a1"), r4c_read("a2", adapter="BETA_COI", otu_n=2),
                         r4c_read("a3", "ITS2", "TH500_ITS2", 3), r4c_read("a4", adapter="no_adapter_1")], False),
    ("FAX00001_pass_1", [r4c_read("b1"), r4c_read("b2", "ITS2", "TH500_ITS2", 3)], False),
    ("FAX00001_pass_2", [r4c_read("c1", otu_n=4)], True),
    ("FAX00001_pass_3", [], False),
    ("FAX00001_pass_4", [r4c_read("d1", adapter="BETA_COI", otu_n=2), r4c_read("d2", adapter="no_adapter_1", otu_n=5),
                         r4c_read("d3", adapter="no_adapter_1", otu_n=5)], False),
]


def run_r4c_round(code, state_root, rb, reads, work, failed=False):
    """One pre-R4-D round as that pipeline runs it: the round index
    (round_index_assign.sh) and the round directory's round_index.tsv, the
    reporting task, then its backup task's completed-round ledger."""
    main_nf = (code / "main.nf").read_text(encoding="utf-8")
    state, round_dir = state_root / "_state", state_root / rb
    for d in (state, round_dir, work):
        d.mkdir(parents=True, exist_ok=True)
    idx = subprocess.run(["/bin/bash", str(code / "bin" / "round_index_assign.sh"), "--state-dir", str(state),
                          "--round-barcode", rb, "--no-lock"], capture_output=True, text=True, check=True).stdout.strip()
    (round_dir / "round_index.tsv").write_text(f"round_barcode\tround_index\n{rb}\t{idx}\n")
    if failed:
        (round_dir / "ROUND_FAILED.txt").write_text("failed\n")
    head = "long_read_id\totu_taxid\totu_kingdom\totu_phylum\totu_class\totu_order\totu_family\totu_genus\totu_species\n"
    long_ids = [f"{r['uuid']}|{r['marker']}|sup|barcode=|adapter={r['adapter']}" for r in reads]
    rows = [f"{lid}|{r['otu']}\t{r['taxid']}\t" + "\t".join(r["lineage"]) + "\n" for lid, r in zip(long_ids, reads)]
    (work / "round_sup.tsv").write_text("read_id\tsequence_length_template\tmean_qscore_template\n"
                                        + "".join(f"{r['uuid']}\t{400 + len(r['uuid'])}\t12.5\n" for r in reads))
    (work / "blast_read.csv").write_text("".join(
        f"{lid},ACC{r['uuid'].upper()}|kraken:taxid|{r['taxid']},0.0,{400 + len(r['uuid'])},99.1\n"
        for lid, r in zip(long_ids, reads)))
    # the preferred report holds every read, the no-adapter report its no-adapter subset;
    # a failed round's upstream output is unusable (the reporting helper dies: placeholders)
    (work / "blast_report_otu.tsv").write_text(head + "".join(rows) + ("garbage-without-context\t1\n" if failed else ""))
    noad = [row for row, r in zip(rows, reads) if r["adapter"].startswith("no_adapter")]
    (work / "blast_report_noadapter.tsv").write_text(head + "".join(noad) if noad else "")
    (round_dir / f"{BARCODE}_read_info_rpt.txt").write_text("read_id\n" + "".join(f"{r['uuid']}\n" for r in reads))
    ph = code / "bin" / "report_placeholders" / "failed_round"
    env = {"barcode": BARCODE, "round_barcode": rb, "ongoingStateDir": str(state_root), "baseDir": str(code),
           "restartTokenForCache": "fixture", "round_lock_scope": "fixture", "round_generation_token": "fixture",
           "demuxIdentityContext": "full_collapse", "params.targets": "COI|ITS2",
           "failedRoundPlaceholderAssets.blastOtuPretaxRpt": str(ph / "blast_otu_pretax_rpt.txt"),
           "failedRoundPlaceholderAssets.blastOtuNoadapterRpt": str(ph / "blast_otu_noadapter_rpt.txt"),
           "failedRoundPlaceholderAssets.readInfoRpt": str(ph / "read_info_rpt.txt"),
           "round_sup_tsv": "round_sup.tsv", "blast_read": "blast_read.csv", "blast_report_otu": "blast_report_otu.tsv",
           "blast_report_noadapter": "blast_report_noadapter.tsv", "params.delete_input_pod5": "false"}
    script = "#!/bin/bash -ue\n" + render_groovy(r4c_script_block(main_nf, "_reporting_blast_pretax"), env).strip() + "\n"
    (work / ".command.sh").write_text(script)
    result = subprocess.run(["/bin/bash", "-ue", ".command.sh"], cwd=work, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr[-1500:]
    pod5 = work / f"{rb}.pod5"
    pod5.write_bytes(b"POD5 " + rb.encode())
    done = (f"set -uo pipefail\nsource '{code}/bin/lib/lock_utils.sh'\ninit_lock_helpers\nSTATE_TMP='{state}'\n"
            f"DONE_LOCK=\"$STATE_TMP/.done_pod5.lock\"\nLOCK_WAIT=30\nREAD_PATH='{pod5}'\n"
            + render_groovy(r4c_done_fragment(main_nf), env))
    result = subprocess.run(["/bin/bash", "-c", done], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr[-1500:]


@pytest.fixture(scope="module")
def pre_r4d(r4c, tmp_path_factory):
    """An authentic pre-R4-D state root: its `_state` and its round directories."""
    root = tmp_path_factory.mktemp("pre-r4d")
    state_root = root / "state" / "S1"
    for rb, reads, failed in PRE_R4D_ROUNDS:
        run_r4c_round(r4c, state_root, rb, reads, root / "work" / rb, failed=failed)
    return state_root


def clone_pre_r4d(pre_r4d, dest: Path) -> Path:
    """A private copy of the authentic state root (bytes, modes, times); its `_state`."""
    shutil.copytree(pre_r4d, dest, symlinks=True)
    return dest / "_state"


def tree_snapshot(root: Path):
    """Every entry under root: mode, inode, size, mtime and bytes (directories included)."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        data = sha(p.read_bytes()) if p.is_file() and not p.is_symlink() else None
        out[str(p.relative_to(root))] = (oct(st.st_mode), st.st_ino, st.st_size, st.st_mtime_ns, data)
    return out


def round_blocks(state_root: Path, product):
    """(round_barcode, round_index, rows) of every round of the ledger, in index order."""
    ledger = [line.split("\t") for line in (state_root / "_state" / "round_index.tsv").read_text().splitlines()]
    out = []
    for rb, idx in sorted(ledger, key=lambda x: int(x[1])):
        table = state_root / rb / NAMES[product]
        out.append((rb, idx, table.read_bytes()[len(PRETAX_HEADER):] if table.exists() else None))
    return out


def legacy_record_oracle(state_root: Path, rounds: int):
    """The sealed legacy record of an authentic snapshot covering `rounds` rounds (independent oracle)."""
    state = state_root / "_state"
    products = "".join(f"{p}\t{NAMES[p]}\t{len((state / NAMES[p]).read_bytes())}\t{sha((state / NAMES[p]).read_bytes())}\n"
                       for p in PAIR)
    generation = sha(products.encode())
    blocks = {p: round_blocks(state_root, p) for p in PAIR}
    provenance = "".join(
        f"{i + 1}\t{blocks['public'][i][0]}\t{blocks['public'][i][1]}\t"
        + "\t".join(sha(blocks[p][i][2]) if blocks[p][i][2] is not None else "-" for p in PAIR) + "\n"
        for i in range(rounds))
    head = (f"{LEGACY_HEADER}\ngeneration\t{generation}\nprevious\tNA\n{products}"
            f"provenance\t{rounds}\t{sha(provenance.encode())}\n")
    return head + f"#END\t{sha(head.encode())}\n", generation


def round_json_bytes(state: Path, work: Path, new_dir: Path):
    """report_round_json.pl as main.nf wires the cumulative options, with a fixed
    hash order so that outputs compare byte for byte."""
    work.mkdir(parents=True, exist_ok=True)
    out = work / "round.json"
    cmd = ["perl", str(REPORT_ROUND_JSON), "--run-id", "runA", "--barcode", BARCODE, "--round-barcode", "round_2",
           "--targets", "COI|ITS2", "--target-taxa", "Metazoa|Viridiplantae", "--timestamp-utc", "2026-09-23T00:00:00Z",
           "--out", str(out), "--blast-otu", str(new_dir / NAMES["public"]),
           "--blast-otu-reporting", str(new_dir / NAMES["reporting"]), "--otu-def", str(new_dir / "otu_def.tsv"),
           "--otu-sizes-round", str(new_dir / "otu_sizes_round.tsv"), "--otu-lock-summary", str(new_dir / "lock.tsv"),
           "--blast-id-family", "92", "--blast-id-genus", "95", "--blast-id-spec", "98",
           "--blast-otu-cumulative", str(state / NAMES["public"]),
           "--blast-otu-reporting-cumulative", str(state / NAMES["reporting"])]
    result = subprocess.run(cmd, cwd=work, env={**os.environ, **ENV, "PERL_HASH_SEED": "0", "PERL_PERTURB_KEYS": "0"},
                            capture_output=True, text=True, check=False)
    return result.returncode, (out.read_bytes() if result.returncode == 0 else None), result.stderr


def legacy_labels(directory: Path, legacy, new):
    """Per present public name: 'L' (legacy bytes), 'G' (the new generation's bytes) or '?'."""
    out = {}
    for p in PRODUCTS:
        path = directory / NAMES[p]
        if os.path.lexists(path):
            data = path.read_bytes()
            out[p] = "L" if legacy.get(p) == data else "G" if new[p] == data else "?"
    return out


def test_authentic_pre_r4d_state_is_written_by_the_immutable_pre_r4d_code(pre_r4d, r4c):
    state = pre_r4d / "_state"
    family = sorted(n for n in os.listdir(state) if n.startswith(f"{BARCODE}_blast_otu_") or n.startswith(".r4d-"))
    assert family == sorted([NAMES["public"], NAMES["noadapter"]])  # no sidecar, record, member or temp
    assert (state / "round_index.tsv").read_text().splitlines() == [f"{rb}\t{i}" for i, (rb, _, _) in enumerate(PRE_R4D_ROUNDS, 1)]
    assert [line.split("\t")[-1] for line in (state / "done_pod5.txt").read_text().splitlines()] == [
        f"{rb}.pod5" for rb, _, _ in PRE_R4D_ROUNDS]
    placeholder = (r4c / "bin" / "report_placeholders" / "failed_round" / "blast_otu_pretax_rpt.txt").read_bytes()
    assert placeholder == PRETAX_HEADER and (pre_r4d / "FAX00001_pass_2" / NAMES["public"]).read_bytes() == placeholder
    for p, rows in (("public", 9), ("noadapter", 3)):
        text = (state / NAMES[p]).read_bytes()
        # created from the first round's table, then every later round's rows appended
        assert text == PRETAX_HEADER + b"".join(b for _, _, b in round_blocks(pre_r4d, p))
        assert text.count(b"\n") == 1 + rows
    row = (state / NAMES["public"]).read_text().splitlines()[1].split("\t")
    assert row[0] == "a1|COI|sup|barcode=|adapter=ALPHA_COI|OTUB_1-COI" and row[1:4] == ["COI", "sup", "ALPHA_COI"]
    assert row[8] == "OTUB_1-COI" and row[16] == "Drosophila melanogaster" and len(row) == 17


def test_pre_r4d_snapshot_is_sealed_once_and_idempotently(pre_r4d, tmp_path):
    state = clone_pre_r4d(pre_r4d, tmp_path / "S1")
    root = state.parent
    before = {p: ((state / NAMES[p]).read_bytes(), (state / NAMES[p]).lstat()) for p in PAIR}
    rounds_before = {k: v for k, v in tree_snapshot(root).items() if not k.startswith("_state")}
    res = resolve(state)
    assert res["ok"] and res["mode"] == "committed" and res["kind"] == "legacy", res
    assert set(res["paths"]) == set(PAIR)  # no reporting sidecar: nothing fabricated
    text, generation = legacy_record_oracle(root, len(PRE_R4D_ROUNDS))
    assert (state / RECORD).read_text() == text and res["generation"] == generation
    for p in PAIR:
        data, st0 = before[p]
        st = (state / NAMES[p]).lstat()
        assert (state / NAMES[p]).read_bytes() == data  # the legacy bytes are unchanged
        assert (st.st_ino, st.st_mode, st.st_mtime_ns) == (st0.st_ino, st0.st_mode, st0.st_mtime_ns)
        assert (state / f"{NAMES[p]}.gen-{generation}").lstat().st_ino == st.st_ino and st.st_nlink == 2
    assert not os.path.lexists(state / NAMES["reporting"]) and residue(state) == []
    assert {k: v for k, v in tree_snapshot(root).items() if not k.startswith("_state")} == rounds_before
    sealed = tree_snapshot(root)
    for _ in range(2):  # recognition is idempotent
        assert resolve(state)["generation"] == generation
    assert tree_snapshot(root) == sealed


def test_readers_serve_the_sealed_legacy_tables_with_r4d_metrics_unavailable(pre_r4d, tmp_path, gens, render):
    state = clone_pre_r4d(pre_r4d, tmp_path / "S1")
    plain = tmp_path / "plain"  # the same tables as ordinary files: exactly what the pre-I2 code reads
    plain.mkdir()
    for p in PAIR:
        shutil.copy2(state / NAMES[p], plain / NAMES[p])
    rc_plain, plain_json, err_plain = round_json_bytes(plain, tmp_path / "rj-plain", gens["dirs"]["new"])
    rc, sealed_json, err = round_json_bytes(state, tmp_path / "rj-sealed", gens["dirs"]["new"])
    assert rc_plain == 0 and rc == 0, (err_plain, err)
    assert (state / RECORD).read_text().startswith(LEGACY_HEADER + "\n")  # the summary sealed it
    assert sealed_json == plain_json
    tax = json.loads(sealed_json)["taxonomy_assignment"]
    assert tax["round"] is not None and tax["cumulative"] is None and tax["cumulative_equals_round"] is None
    # report_run_json: the read fate of the legacy public table, exactly as of the same bytes committed
    legacy_public = (state / NAMES["public"]).read_bytes()
    runs = {}
    for label in ("legacy", "committed"):
        run_state = _run_json_state(tmp_path / label)
        if label == "legacy":  # the authentic `_state` and its round directories under the run's state root
            for entry in pre_r4d.iterdir():
                if entry.name == "_state":
                    for f in entry.iterdir():
                        shutil.copy2(f, run_state / f.name)
                else:
                    shutil.copytree(entry, run_state.parent / entry.name, symlinks=True)
        else:
            _commit_by_oracle(run_state, {"reporting": b"", "public": legacy_public, "noadapter": PRETAX_HEADER})
        result, data = _run_json(tmp_path / label, run_state)
        assert result.returncode == 0, result.stderr
        runs[label] = data["run_status_read_fate"]
    assert runs["legacy"] == runs["committed"], runs
    # report_render.py: consensus rows take their OTU assignment from the legacy tables
    results = tmp_path / "results"
    ongoing = results / "temp" / "ongoing" / "state" / "S1"
    shutil.copytree(pre_r4d, ongoing, symlinks=True)
    (ongoing / "Consensus").mkdir()
    (ongoing / "Consensus" / "alpha_COI_Merged_Consensus.fasta").write_text(">alpha|c1|COI|reads-3|OTU=OTUB_1-COI\nACGT\n")
    rows = render.collect_consensus_sequence_rows("runA", results / "report_html" / "runs" / "runA" / "report.html", "S1")
    assert len(rows) == 1 and "Drosophila melanogaster" in rows[0]["otu_assignment"], rows
    assert resolve(ongoing / "_state")["kind"] == "legacy"


def test_backup_and_restore_carry_the_sealed_legacy_generation(pre_r4d, tmp_path, gens, render):
    L = results_layout(tmp_path)
    shutil.rmtree(L["state"].parent)
    clone_pre_r4d(pre_r4d, L["state"].parent)
    (L["state"] / f"{BARCODE}_reads_time_rpt.txt").write_text("time\treads\n", encoding="utf-8")
    legacy = {p: (L["state"] / NAMES[p]).read_bytes() for p in PAIR}
    backup_round(L)
    generation = resolve(L["state"])["generation"]  # sealed when section 2 resolved its source
    sealed = (L["state"] / RECORD).read_text()
    for dest in (L["ongoing"], L["current"] / "tables"):
        # a replica of the sealed legacy generation: its record, provenance line included
        assert (dest / RECORD).read_text() == sealed and sealed.startswith(LEGACY_HEADER + "\n")
        res = resolve(dest, "any")
        assert res["kind"] == "legacy" and {p: Path(v).read_bytes() for p, v in res["paths"].items()} == legacy
        assert {p: (dest / NAMES[p]).read_bytes() for p in PAIR} == legacy
    before = {str(d): snapshot(d) for d in (L["ongoing"], L["current"] / "tables")}
    backup_round(L)  # replay: nothing changes
    assert {str(d): snapshot(d) for d in (L["ongoing"], L["current"] / "tables")} == before
    env = {**os.environ, **ENV, "MODE": "restore", "OUTDIR": str(tmp_path), "LOCK_WAIT": "5", "RUN_NAME": "runR",
           "STATE_ID": "S1", "FORCE": "0", "OPERATION_ID": "d" * 64}
    result = subprocess.run(["/bin/bash", str(RESTART_HANDLER)], env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    # the restore installs the sealed legacy generation itself: readers keep working
    assert (L["state"] / RECORD).read_text() == sealed
    res = resolve(L["state"])
    assert res["ok"] and res["kind"] == "legacy" and res["generation"] == generation, res
    assert {p: Path(v).read_bytes() for p, v in res["paths"].items()} == legacy
    rc, data, err = round_json_bytes(L["state"], tmp_path / "rj", gens["dirs"]["new"])
    assert rc == 0 and json.loads(data)["taxonomy_assignment"]["cumulative"] is None, err
    publish(L["state"], gens["dirs"]["new"])
    assert committed_names(L["state"])[1] == generation and resolved_generation(L["state"], gens) == "new"
    backup_round(L)
    assert pair_generation(L["ongoing"], gens) == pair_generation(L["current"] / "tables", gens) == "new"


UPGRADE_FAULTS = [
    # (fault, arg, message, sealed_by)  sealed_by: the publisher, or the next reader when the fault hits the sealing
    ("tempfile", "1", "injected tempfile failure", "publisher"),
    ("sync_dir", "3", "sync directory", "publisher"),
    ("retract_rename", "", "stage backup", "publisher"),
    ("projection_rename", NAMES["public"], "R4-D: replace", "publisher"),
    ("sync_dir", "4", "sync directory", "publisher"),
    # an I/O error while sealing fails the publication itself: the snapshot stays intact and unsealed
    ("link_member", "", "cumulative: link", "reader"),
    ("commit_rename", "", "commit legacy record", "reader"),
]


@pytest.mark.parametrize("fault,arg,message,sealed_by", UPGRADE_FAULTS, ids=[f"{f}-{a}" for f, a, _, _ in UPGRADE_FAULTS])
def test_a_failed_first_upgraded_publication_keeps_the_legacy_authority(pre_r4d, tmp_path, gens, render,
                                                                         fault, arg, message, sealed_by):
    wrapper = tmp_path / "fault.pl"
    wrapper.write_text(FAULT_WRAPPER, encoding="utf-8")
    control = clone_pre_r4d(pre_r4d, tmp_path / "control")
    publish(control, gens["dirs"]["new"])
    state = clone_pre_r4d(pre_r4d, tmp_path / "S1")
    legacy = {p: (state / NAMES[p]).read_bytes() for p in PAIR}
    text, generation = legacy_record_oracle(state.parent, len(PRE_R4D_ROUNDS))
    result = publish(state, gens["dirs"]["new"], check=False, wrapper=wrapper, env_extra={"I2_FAULT": fault, "I2_FAULT_ARG": arg})
    assert result.returncode != 0 and message in result.stderr, result.stderr
    assert (state / RECORD).exists() == (sealed_by == "publisher")
    assert legacy_labels(state, legacy, gens["expected"]["new"]) == {"public": "L", "noadapter": "L"}
    # the legacy generation stays (or, when the sealing itself failed, becomes) the authority
    res = resolve(state)
    assert res["ok"] and res["kind"] == "legacy" and res["generation"] == generation, res
    assert (state / RECORD).read_text() == text
    assert not any(".gen-" in n and generation not in n for n in os.listdir(state)) and residue(state) == []
    rc, data, err = round_json_bytes(state, tmp_path / "rj", gens["dirs"]["new"])
    assert rc == 0 and json.loads(data)["taxonomy_assignment"]["cumulative"] is None, err
    # -resume: the retry publishes the new generation over the sealed legacy one
    publish(state, gens["dirs"]["new"])
    assert (state / RECORD).read_bytes() == (control / RECORD).read_bytes()
    assert sorted(os.listdir(state)) == sorted(os.listdir(control))
    assert {p: (state / NAMES[p]).read_bytes() for p in PRODUCTS} == gens["expected"]["new"] and residue(state) == []


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
def test_first_upgraded_publication_interrupted_anywhere(pre_r4d, tmp_path, gens, interrupt_env, sig):
    control = clone_pre_r4d(pre_r4d, tmp_path / "control")
    publish(control, gens["dirs"]["new"])
    new_generation, legacy_generation = committed_names(control)
    probe = clone_pre_r4d(pre_r4d, tmp_path / "probe")
    events = trace_events({"_trace": tmp_path / "trace.tsv"},
                          lambda e: publish(probe, gens["dirs"]["new"], check=False, env_extra={**interrupt_env, **e}))
    commits = [i for i, e in enumerate(events, 1) if is_commit(e)]
    assert len(commits) == 2  # the legacy record, then the new generation
    legacy = {p: (control.parent / "_state" / f"{NAMES[p]}.gen-{legacy_generation}").read_bytes() for p in PAIR}
    seen = set()
    for index in range(1, len(events) + 1):
        state = clone_pre_r4d(pre_r4d, tmp_path / f"b{index}")
        (tmp_path / f"w{index}").mkdir()
        point = kill_publisher(state, gens["dirs"]["new"], index, sig, tmp_path / f"w{index}", interrupt_env)
        labels = legacy_labels(state, legacy, gens["expected"]["new"])
        assert len(set(labels.values())) <= 1 and "?" not in labels.values(), (index, point, labels)
        res = resolve(state)  # a reader after the death: never absent, never mixed, never fails closed
        want = new_generation if index >= commits[1] else legacy_generation
        assert res["ok"] and res["generation"] == want, (index, point, res)
        seen.add(res["kind"])
        rc, data, err = round_json_bytes(state, tmp_path / f"rj{index}", gens["dirs"]["new"])
        assert rc == 0, (index, point, err)
        assert (json.loads(data)["taxonomy_assignment"]["cumulative"] is None) == (want == legacy_generation)
        publish(state, gens["dirs"]["new"])  # the retry converges on the uninterrupted upgrade
        assert (state / RECORD).read_bytes() == (control / RECORD).read_bytes(), (index, point)
        assert sorted(os.listdir(state)) == sorted(os.listdir(control)) and residue(state) == []
        before = snapshot(state)
        publish(state, gens["dirs"]["new"])  # replay: a strict no-op
        assert snapshot(state) == before
    assert seen == {"legacy", "state"}


def test_successful_upgrade_replay_retention_and_commit_deletion(pre_r4d, tmp_path, gens, render):
    state = clone_pre_r4d(pre_r4d, tmp_path / "S1")
    legacy = {p: (state / NAMES[p]).read_bytes() for p in PAIR}
    legacy_generation = resolve(state)["generation"]
    publish(state, gens["dirs"]["new"])
    new_generation, previous = committed_names(state)
    assert previous == legacy_generation and resolved_generation(state, gens) == "new"
    assert {p: (state / NAMES[p]).read_bytes() for p in PRODUCTS} == gens["expected"]["new"]
    assert sorted(n for n in os.listdir(state) if ".gen-" in n) == sorted(
        [f"{NAMES[p]}.gen-{new_generation}" for p in PRODUCTS] + [f"{NAMES[p]}.gen-{legacy_generation}" for p in PAIR])
    assert {p: (state / f"{NAMES[p]}.gen-{legacy_generation}").read_bytes() for p in PAIR} == legacy
    before = snapshot(state)
    publish(state, gens["dirs"]["new"])  # replay
    assert snapshot(state) == before
    publish(state, gens["dirs"]["third"])  # one further commit releases the legacy generation
    assert not any(legacy_generation in n for n in os.listdir(state)) and residue(state) == []
    # deleting the commit record never restores legacy mode: the members remain
    (state / RECORD).unlink()
    snap = tree_snapshot(state.parent)
    res = resolve(state)
    assert not res["ok"] and "deleted commit record" in res["error"], res
    assert tree_snapshot(state.parent) == snap
    # nor does deleting the legacy record right after the sealing
    sealed = clone_pre_r4d(pre_r4d, tmp_path / "S2")
    resolve(sealed)
    (sealed / RECORD).unlink()
    snap = tree_snapshot(sealed.parent)
    res = resolve(sealed)
    assert not res["ok"] and "deleted commit record" in res["error"], res
    assert tree_snapshot(sealed.parent) == snap


def _append(path: Path, data: bytes):
    with open(path, "ab") as f:
        f.write(data)


def _rewrite_rows(state: Path, product, rows: bytes):
    (state / NAMES[product]).write_bytes(PRETAX_HEADER + rows)


FOREIGN_ROW = b"zz9|COI|sup|barcode=|adapter=OTHER_COI|OTUB_9-COI\tCOI\tsup\tOTHER_COI\tACCZZ9\t7227\t403\t99.1\tOTUB_9-COI\t7227\tMetazoa\tArthropoda\tInsecta\tDiptera\tDrosophilidae\tDrosophila\tDrosophila melanogaster\n"


def _block(root: Path, product, index):
    return round_blocks(root, product)[index][2]


PRE_R4D_REJECTIONS = {
    # name: (mutate(root, state), diagnostic fragment)
    "only_public_table": (lambda r, s: (s / NAMES["noadapter"]).unlink(), "incomplete pre-R4-D cumulative snapshot"),
    "only_noadapter_table": (lambda r, s: (s / NAMES["public"]).unlink(), "incomplete pre-R4-D cumulative snapshot"),
    "no_round_ledger": (lambda r, s: (s / "round_index.tsv").unlink(), "no round ledger"),
    "malformed_round_ledger": (lambda r, s: _append(s / "round_index.tsv", b"FAX00001_pass_9\tx\n"), "round ledger"),
    "round_directories_missing": (lambda r, s: [shutil.rmtree(r / rb) for rb, _, _ in PRE_R4D_ROUNDS],
                                  "provenance cannot be established"),
    "completed_round_without_its_table": (lambda r, s: (r / "FAX00001_pass_1" / NAMES["public"]).unlink(), "has no round table"),
    "authentic_pair_plus_partial_sidecar": (lambda r, s: (s / NAMES["reporting"]).write_bytes(b"#RTB-R4D-REPORTING\t1\t" + b"0" * 64 + b"\n"),
                                            "not a valid R4-D publication"),
    "stray_tmp_copy": (lambda r, s: shutil.copy2(s / NAMES["public"], s / f"{NAMES['public']}.tmp"), "governed residue is present"),
    "r4d_temp_residue": (lambda r, s: (s / ".r4d-publish-Ab12_z").write_text("x"), "governed residue is present"),
    "bak_residue": (lambda r, s: os.link(s / NAMES["public"], s / f"{NAMES['public']}.bak.4242"), "governed residue is present"),
    "member_without_a_sealing": (lambda r, s: os.link(s / NAMES["public"], s / f"{NAMES['public']}.gen-{'a' * 64}"),
                                 "governed residue is present"),
    "member_of_another_generation": (lambda r, s: ((s / LOCK).write_text(""), os.link(s / NAMES["public"], s / f"{NAMES['public']}.gen-{'a' * 64}")),
                                     "without valid authority"),
    "mixed_barcodes": (lambda r, s: shutil.copy2(s / NAMES["noadapter"], s / "OTHER_blast_otu_noadapter_rpt.txt"), "mixes barcodes"),
    "rows_from_other_state": (lambda r, s: _append(s / NAMES["public"], FOREIGN_ROW), "not the ordered concatenation"),
    "wrong_header": (lambda r, s: (s / NAMES["public"]).write_bytes((s / NAMES["public"]).read_bytes().replace(b"read_id\t", b"readid\t", 1)),
                     "wrong header"),
    "wrong_round_table_header": (lambda r, s: (r / "FAX00001_pass_1" / NAMES["noadapter"]).write_bytes(b"read_id\n"), "wrong header"),
    "appended_row": (lambda r, s: _append(s / NAMES["public"], (s / NAMES["public"]).read_bytes().splitlines(True)[-1]),
                     "not the ordered concatenation"),
    "truncated_row": (lambda r, s: (s / NAMES["public"]).write_bytes((s / NAMES["public"]).read_bytes()[:-9]), "truncated row"),
    "malformed_row": (lambda r, s: _append(s / NAMES["public"], b"a\tb\tc\n"), "malformed row"),
    "truncated_completed_round": (lambda r, s: [_rewrite_rows(s, p, b"".join(_block(r, p, i) for i in range(4))) for p in PAIR],
                                  "is missing from the cumulative tables"),
    "duplicated_round": (lambda r, s: _append(s / NAMES["public"], _block(r, "public", 1)), "not the ordered concatenation"),
    "reordered_rounds": (lambda r, s: _rewrite_rows(s, "public", _block(r, "public", 1) + _block(r, "public", 0)
                                                    + b"".join(_block(r, "public", i) for i in range(2, 5))),
                         "not the ordered concatenation"),
    "tables_of_different_rounds": (lambda r, s: _rewrite_rows(s, "noadapter", _block(r, "noadapter", 0)), "tables of different rounds"),
    "public_table_is_a_symlink": (lambda r, s: (shutil.copy2(s / NAMES["public"], s / "elsewhere"), (s / NAMES["public"]).unlink(),
                                                os.symlink("elsewhere", s / NAMES["public"])), "not a regular file"),
    # the ledger or the round tables that prove the snapshot, changed (F)
    "changed_ledger": (lambda r, s: (s / "round_index.tsv").write_text(
        (s / "round_index.tsv").read_text().replace("FAX00001_pass_1\t", "FAX00001_pass_7\t")), "not the ordered concatenation"),
    "truncated_ledger": (lambda r, s: (s / "round_index.tsv").write_text(
        "".join((s / "round_index.tsv").read_text().splitlines(True)[:2])), "not the ordered concatenation"),
    "extra_completed_round": (lambda r, s: (_append(s / "round_index.tsv", b"FAX00001_pass_5\t6\n"),
                                            _append(s / "done_pod5.txt", b"/x/FAX00001_pass_5.pod5\t1\t1\t1\tFAX00001_pass_5.pod5\n")),
                              "has no round table"),
    "round_table_hash_changed": (lambda r, s: (r / "FAX00001_pass_1" / NAMES["public"]).write_bytes(
        (r / "FAX00001_pass_1" / NAMES["public"]).read_bytes().replace(b"\t99.1\t", b"\t98.1\t", 1)),
                                 "not the ordered concatenation"),
}


@pytest.mark.parametrize("case", list(PRE_R4D_REJECTIONS))
def test_partial_mixed_or_unprovable_pre_r4d_state_fails_closed_and_is_never_touched(pre_r4d, tmp_path, gens, case):
    mutate, fragment = PRE_R4D_REJECTIONS[case]
    state = clone_pre_r4d(pre_r4d, tmp_path / "S1")
    mutate(state.parent, state)
    before = tree_snapshot(state.parent)
    res = resolve(state)
    assert not res["ok"] and fragment in res["error"] and "remediate it explicitly" in res["error"], res
    rc, _, err = round_json_bytes(state, tmp_path / "rj", gens["dirs"]["new"])
    assert rc != 0 and fragment in err
    assert tree_snapshot(state.parent) == before  # every byte, mode, inode and time preserved; nothing sealed
    # the publisher refuses it before any write: no new generation erases the evidence (F4)
    result = publish(state, gens["dirs"]["new"], check=False)
    assert result.returncode != 0 and "publication refused" in result.stderr, result.stderr
    assert tree_snapshot(state.parent) == before


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["TERM", "KILL"])
def test_an_interrupted_sealing_is_completed_by_the_next_reader(pre_r4d, tmp_path, interrupt_env, sig):
    script = "require $ARGV[0]; exit RTBioScan::R4DCumulative::resolve_cli(@ARGV[1 .. $#ARGV]);"

    def reader(state, env_extra, popen):
        cmd = ["perl", "-e", script, str(RESOLVER), BARCODE, str(state), "state"]
        env = {**os.environ, **interrupt_env, **env_extra}
        if popen:
            return subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    start_new_session=True)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)

    probe = clone_pre_r4d(pre_r4d, tmp_path / "probe")
    events = trace_events({"_trace": tmp_path / "trace.tsv"}, lambda e: reader(probe, e, False))
    text, generation = legacy_record_oracle(probe.parent, len(PRE_R4D_ROUNDS))
    assert (probe / RECORD).read_text() == text and len(events) >= 6
    commit_at = next(i for i, e in enumerate(events, 1) if is_commit(e))
    for index in range(1, len(events) + 1):
        state = clone_pre_r4d(pre_r4d, tmp_path / f"b{index}")
        point = stop_and_signal(lambda e: reader(state, {"I2_STOP_AT": str(index), **e}, True), tmp_path / f"r{index}", sig)
        res = resolve(state)
        assert res["ok"] and res["kind"] == "legacy" and res["generation"] == generation, (index, point, res)
        # a sealing killed after its commit leaves only its lock name (removed by the next publication)
        assert residue(state) in ([], [LOCK]) and (index >= commit_at or residue(state) == []), (index, point, residue(state))
        assert (state / RECORD).read_text() == text, (index, point)
        assert sorted(n for n in os.listdir(state) if ".gen-" in n) == sorted(f"{NAMES[p]}.gen-{generation}" for p in PAIR)


def test_pristine_complete_r4d_and_committed_states_are_never_sealed(tmp_path, gens):
    pristine = new_state(tmp_path, "p")
    assert resolve(pristine)["mode"] == "absent" and os.listdir(pristine) == []
    complete = new_state(tmp_path, "c")
    _legacy(complete, gens)
    res = resolve(complete)
    assert res["mode"] == "legacy" and not (complete / RECORD).exists() and residue(complete) == []
    publish(complete, gens["dirs"]["new"])  # migration of a complete R4-D snapshot
    assert resolved_generation(complete, gens) == "new" and committed_names(complete)[1] == "NA"
    committed = new_state(tmp_path, "i")
    publish(committed, gens["dirs"]["old"])
    record = (committed / RECORD).read_bytes()
    assert resolve(committed)["kind"] == "state" and (committed / RECORD).read_bytes() == record


# ---------------------------------------------------------------------------
# Restore and backup authority (second correction). A restore installs the
# snapshot's generation into `_state` with its authority; an older backup is
# proven from retained evidence and completed in place before the live state
# that proves it is wiped, or the restore is refused before it changes anything.
def r4c_backup_round(r4c, L):
    """backup_update_and_clean sections 2 and 7 as the pre-R4-D pipeline ran them
    (its main.nf and its backup_sync.sh, verbatim), and its done_pod5 copy to
    temp/current: plain copies of the two tables, no record."""
    sync = r4c / "bin" / "lib" / "backup_sync.sh"
    s2 = main_nf_fragment('rpts=( "\\$STATE_TMP"/*_rpt.txt )', "if (( \\${#pngs_all[@]} ))", r4c / "main.nf")
    s7 = main_nf_fragment('tables=( "\\$ONGOING_FINAL"/*.txt "\\$ONGOING_FINAL"/*.tsv "\\$ONGOING_FINAL"/*.csv \\',
                          "# ---- Create to_figures/", r4c / "main.nf")
    for pre, body in ((f"STATE_TMP='{L['state']}'\nONGOING_FINAL='{L['ongoing']}'\nmkdir -p \"$ONGOING_FINAL\"\n", s2),
                      (f"ONGOING_FINAL='{L['ongoing']}'\nCURRENT_ROOT='{L['current']}'\nmkdir -p \"$CURRENT_ROOT/tables\"\n", s7)):
        result = bash(f"set -euo pipefail\nshopt -s nullglob\nsource '{sync}'\n{pre}{body}\n")
        assert result.returncode == 0, result.stderr
    temp_current = L["state"].parents[3] / "current" / "state" / "S1"
    temp_current.mkdir(parents=True, exist_ok=True)
    shutil.copy2(L["state"] / "done_pod5.txt", temp_current / "done_pod5.txt")


def f01_seal_snapshot(L):
    """Wrap completed F01 fixtures in authenticated joint-v2, preserving R4 bytes."""
    import runpy
    import tempfile
    api=runpy.run_path(str(REPO_ROOT/'tests/test_r5_final_design_regressions.py'))
    roots=[L['state'].parents[3]/'current/state/S1',L['current']]
    with tempfile.TemporaryDirectory(prefix='r4-joint-fixture-',dir=L['state'].parents[5]) as scratch:
        work=Path(scratch);live=work/'S1';shutil.copytree(L['state'].parent,live)
        seed=api['joint_fixture'](work/'seed')/'state_authority'
        for suffix in api['PARSER_NAMES']+api['FATE_NAMES']:
            dest=live/'_state'/f'{BARCODE}_{suffix}'
            assert not dest.exists(),dest
            shutil.copy2(seed/f'b_{suffix}',dest)
        rb=(live/'_state/round_index.tsv').read_text().splitlines()[-1].split('\t')[0]
        token='a'*64;pin='b'*64;acq='c'*64;h=BIN/'round_lock_generation.pl';authority=BIN/'state_snapshot_authority.pl'
        common=['--state-dir',str(live/'_state'),'--round-barcode',rb,'--scope','full_round','--token',token]
        def command(*args,env=None):
            r=subprocess.run(list(map(str,args)),capture_output=True,text=True,env=env)
            assert r.returncode==0,r.stderr
        command('perl',h,'acquire',*common,'--pin-token',acq,'--owner-pid',os.getpid(),'--wait-seconds','1','--stale-seconds','30')
        command('perl',h,'handoff',*common,'--pin-token',acq)
        command('perl',h,'pin',*common,'--pin-token',pin,'--owner-pid',os.getpid(),'--role','backup_update_and_clean')
        env={**os.environ,'RTBIOSCAN_ROUND_LOCK_STATE_DIR':str(live/'_state'),'RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE':rb,'RTBIOSCAN_ROUND_LOCK_SCOPE':'full_round','RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN':token,'RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED':'0'}
        candidate=work/'candidate'
        command('perl',authority,'prepare',live,'S1',BARCODE,rb,token,pin,'sample','COI',candidate,*roots,env=env)
        region=work/'region.py'
        region.write_text("import subprocess,sys\na,c,l,t1,t2,h,pin,rb,token=sys.argv[1:]\nrun=lambda *a:subprocess.run(a,check=True)\nrun('perl',a,'capture',c,l,t1,pin)\nrun('perl',h,'finish','--state-dir',l+'/_state','--round-barcode',rb,'--scope','full_round','--token',token,'--pin-token',pin)\nrun('perl',a,'seal',c,t1,t2)\n")
        command('perl',authority,'transaction',candidate,*roots,live,pin,sys.executable,region,authority,candidate,live,*roots,h,pin,rb,token,env=env)


def outdir_view(root: Path):
    """The whole outdir except the handler's own lock-directory churn in temp/."""
    view = tree_snapshot(root)
    view.pop("temp", None)
    return view


def test_restore_proves_an_unsealed_pre_r4d_snapshot_before_the_wipe_and_installs_it(pre_r4d, r4c, tmp_path, gens, render):
    """F1: the pre-R4-D pipeline's own backup left plain copies of the two tables and
    no upgraded reader ever sealed the live state. The restore proves the copies
    against the live round ledger and round tables before it wipes them, completes
    the snapshot in place as the sealed legacy generation (the copies untouched)
    and installs it: every report works at once, a repeated restore needs no
    evidence, failed rounds keep it, and the next publication supersedes it."""
    L = results_layout(tmp_path)
    shutil.rmtree(L["state"].parent)
    clone_pre_r4d(pre_r4d, L["state"].parent)
    r4c_backup_round(r4c, L)
    f01_seal_snapshot(L)
    tables = L["current"] / "tables"
    copies = {p: (tables / NAMES[p]).read_bytes() for p in PAIR}
    assert not (tables / RECORD).exists() and not (L["state"] / RECORD).exists()
    text, generation = legacy_record_oracle(L["state"].parent, len(PRE_R4D_ROUNDS))
    result = restart_restore(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tables / RECORD).read_text() == text and {p: (tables / NAMES[p]).read_bytes() for p in PAIR} == copies
    assert (L["state"] / RECORD).read_text() == text
    assert not any((L["state"].parent / rb).exists() for rb, _, _ in PRE_R4D_ROUNDS)  # the wipe removed the evidence
    res = resolve(L["state"])
    assert res["ok"] and res["kind"] == "legacy" and res["generation"] == generation
    assert {p: Path(v).read_bytes() for p, v in res["paths"].items()} == copies
    rc, data, err = round_json_bytes(L["state"], tmp_path / "rj", gens["dirs"]["new"])
    assert rc == 0 and json.loads(data)["taxonomy_assignment"]["cumulative"] is None, err
    assert render.resolve_r4d_cumulative([(L["state"], "state")])[0]["mode"] == "committed"
    backup_round(L)
    assert (L["ongoing"] / RECORD).read_text() == text
    # a forced repeat of the restore needs no evidence: the snapshot is a complete generation
    result = restart_restore(tmp_path, op="e", force="1")
    assert result.returncode == 0, result.stderr
    assert (L["state"] / RECORD).read_text() == text
    for attempt in range(2):  # failed rounds publish nothing: the legacy generation stays served
        assert publish(L["state"], tmp_path / "missing-round", check=False).returncode != 0
        assert resolve(L["state"])["generation"] == generation
    publish(L["state"], gens["dirs"]["new"])
    assert committed_names(L["state"])[1] == generation and resolved_generation(L["state"], gens) == "new"


def test_r5_f01_pre_r4d_legacy_snapshot_without_completeness_record_is_refused_before_any_change(pre_r4d, r4c,
                                                                                                tmp_path):
    """R5-F01: the pre-R4-D pipeline's own snapshot records completed rounds
    (a nonempty done_pod5.txt) but no completeness record: the restore refuses it
    before any change, with the reset/replay diagnostic."""
    L = results_layout(tmp_path)
    shutil.rmtree(L["state"].parent)
    clone_pre_r4d(pre_r4d, L["state"].parent)
    r4c_backup_round(r4c, L)
    temp_current = tmp_path / "temp" / "current" / "state" / "S1"
    assert (temp_current / "done_pod5.txt").stat().st_size > 0
    assert not (temp_current / "state_authority").exists() and not (L["current"] / "state_authority").exists()
    before = outdir_view(tmp_path)
    result = restart_restore(tmp_path)
    assert result.returncode != 0, result.stdout
    assert "cannot be restored safely" in result.stderr and "restart_mode=reset" in result.stderr, result.stderr
    assert outdir_view(tmp_path) == before
    assert not (tmp_path / "temp" / ".restart_applied.S1").exists()


RESTORE_REFUSALS = {
    # damage(L) to an authentic pre-R4-D outdir whose snapshot holds plain copies
    "snapshot_row_appended": lambda L: _append(L["current"] / "tables" / NAMES["public"], FOREIGN_ROW),
    "snapshot_tables_of_different_rounds": lambda L: _rewrite_rows(L["current"] / "tables", "noadapter",
                                                                    _block(L["state"].parent, "noadapter", 0)),
    "live_ledger_gone": lambda L: (L["state"] / "round_index.tsv").unlink(),
    "live_round_directories_gone": lambda L: [shutil.rmtree(L["state"].parent / rb) for rb, _, _ in PRE_R4D_ROUNDS],
    "snapshot_has_one_table": lambda L: (L["current"] / "tables" / NAMES["noadapter"]).unlink(),
    # compressed tables (no pipeline version writes them): refused, never dropped
    "snapshot_tables_only_compressed": lambda L: _gzip_tables(L["current"] / "tables", keep_plain=False),
    "snapshot_stray_compressed_copy": lambda L: _gzip_tables(L["current"] / "tables", keep_plain=True),
}


def _gzip_tables(tables: Path, keep_plain: bool):
    import gzip
    for p in PAIR:
        src = tables / NAMES[p]
        with open(src, "rb") as f, gzip.open(f"{src}.gz", "wb") as g:
            g.write(f.read())
        if not keep_plain:
            src.unlink()


@pytest.mark.parametrize("case", list(RESTORE_REFUSALS))
def test_restore_refuses_a_snapshot_it_cannot_authenticate_before_any_change(pre_r4d, r4c, tmp_path, case):
    L = results_layout(tmp_path)
    shutil.rmtree(L["state"].parent)
    clone_pre_r4d(pre_r4d, L["state"].parent)
    r4c_backup_round(r4c, L)
    f01_seal_snapshot(L)
    RESTORE_REFUSALS[case](L)
    before = outdir_view(tmp_path)
    result = restart_restore(tmp_path)
    assert result.returncode != 0, result.stdout
    assert ("cannot be authenticated" in result.stderr or "incomplete backup" in result.stderr), result.stderr
    assert outdir_view(tmp_path) == before  # the live state and both snapshots: every byte, inode, mode and time
    assert not (tmp_path / "temp" / ".restart_applied.S1").exists()  # nothing was even recorded as applying


def _earlier_candidate_backup(dest: Path, state: Path):
    """The two-member backup an earlier candidate wrote of the committed generation
    of `state`: a `backup` record binding the three products, members and
    projections of the two public tables only."""
    dest.mkdir(parents=True, exist_ok=True)
    lines = (state / RECORD).read_text().split("\n")
    head = f"#RTB-R4D-CUMULATIVE-BACKUP\t1\n{lines[1]}\nprevious\tNA\n" + "".join(l + "\n" for l in lines[3:6])
    (dest / RECORD).write_text(head + f"#END\t{sha(head.encode())}\n")
    gen = lines[1].split("\t")[1]
    for p in PAIR:
        shutil.copy2(state / f"{NAMES[p]}.gen-{gen}", dest / f"{NAMES[p]}.gen-{gen}")
        os.link(dest / f"{NAMES[p]}.gen-{gen}", dest / NAMES[p])
    return gen


@pytest.mark.parametrize("sidecar", ["round_copy_in_temp_current", "live_state_member", "nowhere"])
def test_restore_completes_an_earlier_candidates_backup_from_its_bound_sidecar(tmp_path, gens, render, sidecar):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    tables = L["current"] / "tables"
    gen = _earlier_candidate_backup(tables, L["state"])
    if sidecar == "round_copy_in_temp_current":  # backup section 7 copied the round's sidecar there
        temp_tables = tmp_path / "temp" / "current" / "state" / "S1" / "tables"
        temp_tables.mkdir(parents=True)
        shutil.copy2(L["state"] / NAMES["reporting"], temp_tables / NAMES["reporting"])
    publish(L["state"], gens["dirs"]["new"])  # "old" stays the live state's previous generation
    if sidecar != "live_state_member":
        publish(L["state"], gens["dirs"]["third"])  # ... and is released from the live state
        assert not (L["state"] / f"{NAMES['reporting']}.gen-{gen}").exists()
    before = outdir_view(tmp_path)
    result = restart_restore(tmp_path)
    if sidecar == "nowhere":
        assert result.returncode != 0 and "no retained artifact holds" in result.stderr, result.stderr
        assert outdir_view(tmp_path) == before
        return
    assert result.returncode == 0, result.stderr
    # the snapshot was completed in place, then installed with its sidecar
    for d in (tables, L["state"]):
        assert (d / RECORD).read_text().startswith("#RTB-R4D-CUMULATIVE\t1\n")
        assert (d / f"{NAMES['reporting']}.gen-{gen}").read_bytes() == gens["expected"]["old"]["reporting"]
    got = reader_generations(L["state"], tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, "old"), got
    assert round_json(L["state"], tmp_path / "rj", gens["dirs"]["new"])["cumulative"] == gens["rj"]["old"]["cumulative"]


@pytest.mark.parametrize("evidence", ["round_copy", "destination_member", "none", "mixed_generations"])
def test_restore_proves_r4d_plain_copies_by_their_sidecar_projection(tmp_path, gens, render, evidence):
    """R4-D (and the first I2 candidate) backed up plain copies of the two public
    tables; a retained sealed sidecar whose projections are exactly those tables
    proves them, and they are installed as one complete generation."""
    L = results_layout(tmp_path)
    tables = L["current"] / "tables"
    tables.mkdir(parents=True)
    for p in PAIR:
        (tables / NAMES[p]).write_bytes(gens["expected"]["old"][p])
    if evidence == "mixed_generations":
        (tables / NAMES["public"]).write_bytes(gens["expected"]["new"]["public"])
    if evidence == "destination_member":  # a destination whose record was lost kept its sidecar member
        (tables / f"{NAMES['reporting']}.gen-{'b' * 64}").write_bytes(gens["expected"]["old"]["reporting"])
    elif evidence != "none":
        temp_tables = tmp_path / "temp" / "current" / "state" / "S1" / "tables"
        temp_tables.mkdir(parents=True)
        (temp_tables / NAMES["reporting"]).write_bytes(gens["expected"]["old"]["reporting"])
    before = outdir_view(tmp_path)
    result = restart_restore(tmp_path)
    if evidence in ("none", "mixed_generations"):
        assert result.returncode != 0 and "cannot be authenticated" in result.stderr, result.stderr
        assert outdir_view(tmp_path) == before
        return
    assert result.returncode == 0, result.stderr
    got = reader_generations(L["state"], tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, "old"), got
    assert resolve(L["state"])["kind"] == "state" and resolve(tables, "any")["kind"] == "state"


RESTORE_DAMAGE = {
    # damage(tables, gen) to a complete snapshot generation, diagnostic
    "corrupt_record": (lambda t, g: (t / RECORD).write_text("garbage\n"), "unreadable backup record"),
    "missing_member": (lambda t, g: (t / f"{NAMES['reporting']}.gen-{g}").unlink(), "is incomplete"),
    "damaged_member": (lambda t, g: ((t / NAMES["public"]).unlink(), (t / f"{NAMES['public']}.gen-{g}").unlink(),
                                     (t / f"{NAMES['public']}.gen-{g}").write_bytes(b"damaged\n")), "is incomplete"),
    "record_and_sidecar_member_lost": (lambda t, g: ((t / RECORD).unlink(), (t / f"{NAMES['reporting']}.gen-{g}").unlink()),
                                       "cannot be authenticated"),
    "members_without_record_or_tables": (lambda t, g: [(t / n).unlink() for n in os.listdir(t) if ".gen-" not in n],
                                         "incomplete backup"),
    "record_without_members": (lambda t, g: [(t / n).unlink() for n in os.listdir(t) if ".gen-" in n], "is incomplete"),
}


@pytest.mark.parametrize("case", list(RESTORE_DAMAGE))
def test_restore_refuses_a_damaged_or_partial_backup_generation(tmp_path, gens, case):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    backup_round(L)
    gen = committed_names(L["state"])[0]
    for label in ("new", "third"):  # the live state no longer retains the snapshot's generation
        publish(L["state"], gens["dirs"][label])
    tables = L["current"] / "tables"
    damage, fragment = RESTORE_DAMAGE[case]
    damage(tables, gen)
    before = outdir_view(tmp_path)
    result = restart_restore(tmp_path)
    assert result.returncode != 0 and fragment in result.stderr, result.stderr
    assert outdir_view(tmp_path) == before


def test_restore_of_a_snapshot_without_a_cumulative_generation_leaves_it_pristine(tmp_path, gens, render):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    temp_tables = tmp_path / "temp" / "current" / "state" / "S1" / "tables"
    temp_tables.mkdir(parents=True)
    for p in PRODUCTS:  # round copies (backup section 7) are never cumulative authority
        (temp_tables / NAMES[p]).write_bytes(gens["expected"]["old"][p])
    (L["current"] / "tables").mkdir(parents=True)
    (L["current"] / "tables" / "other_rpt.txt").write_text("kept\n")
    result = restart_restore(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (L["state"] / "other_rpt.txt").read_text() == "kept\n"
    assert not [n for n in os.listdir(L["state"]) if "_blast_otu_" in n]
    got = reader_generations(L["state"], tmp_path / "i", gens, render)
    assert got == dict.fromkeys(got, "absent"), got


APPLYING = f"schema=2\nstatus=applying\noperation_id={'b' * 64}\nrequested_mode=restore\nrun_name=runR\n"
APPLIED = f"schema=2\nstatus=applied\noperation_id={'b' * 64}\nmode=restore\nrun_name=runR\n"
RESTART_RECORDS = {
    # restart_handler.sh's record while it changes the state, or after it was interrupted there
    "applying": (APPLYING, "is in progress, or was interrupted"),
    "unreadable_directory": (None, "is not a regular file"),
    "malformed": ("schema=2\nstat", "is malformed"),
    # completed restarts (schema 2 and the historical two-line record): the state is served
    "applied": (APPLIED, None),
    "legacy_applied": ("mode=restore\nrun_name=runR\n", None),
}


@pytest.mark.parametrize("stale_lockdir", [False, True])
@pytest.mark.parametrize("case", sorted(RESTART_RECORDS))
def test_readers_and_publishers_fail_closed_while_a_restart_is_applied(tmp_path, gens, case, stale_lockdir):
    """Every `_state` reader and publisher fails closed, before any write, while the
    restart record says the state is being (or was partly) reset or restored, or cannot
    be read; a completed restart serves the state, even with the lock directory a
    killed handler left behind (it alone does not fence)."""
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    text, fragment = RESTART_RECORDS[case]
    sentinel = tmp_path / "temp" / ".restart_applied.S1"
    if text is None:
        sentinel.mkdir(parents=True)
    else:
        sentinel.write_text(text)
    if stale_lockdir:
        (tmp_path / "temp" / ".restart_applied.S1.lockdir").mkdir()
    before = tree_snapshot(L["state"])
    res = resolve(L["state"])
    result = publish(L["state"], gens["dirs"]["new"], check=False)
    r2 = bash(section2_script(L["state"], L["ongoing"]))
    if fragment is None:
        assert res["ok"] and result.returncode == 0 and r2.returncode == 0, (res, result.stderr, r2.stderr)
        assert resolved_generation(L["state"], gens) == "new"
        return
    assert not res["ok"] and fragment in res["error"], res
    assert result.returncode != 0 and fragment in result.stderr
    assert r2.returncode != 0 and fragment in r2.stderr
    assert tree_snapshot(L["state"]) == before
    sentinel.rmdir() if text is None else sentinel.write_text(APPLIED)
    assert resolved_generation(L["state"], gens) == "old"


def helper_backup_round(L):
    """sync_changed_files handed the governed public paths explicitly, with no
    main.nf invocation and no glob: the helper itself resolves them through their
    source's record before any existence test."""
    for src, dest in ((L["state"], L["ongoing"]), (L["ongoing"], L["current"] / "tables")):
        r = bash(f"set -euo pipefail\nsource '{BACKUP_SYNC}'\n"
                 f"sync_changed_files '{dest}' '{src / NAMES['public']}' '{src / NAMES['noadapter']}'\n")
        assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("via", ["main_nf_sections", "helper_explicit_paths"])
@pytest.mark.parametrize("names_present", [0, 1])
def test_backup_during_a_publication_retraction_backs_up_the_committed_generation(tmp_path, gens, interrupt_env, names_present,
                                                                                  via):
    """F3: while a publication has retracted the public tables (none or one of them
    present), backup sections 2 and 7 -- and sync_changed_files handed the public
    paths explicitly -- still back up the committed generation: never an empty or
    mixed backup."""
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    events = publisher_boundaries(tmp_path, L["state"], gens["dirs"]["new"], interrupt_env)
    retractions = [i for i, e in enumerate(events, 1) if e.startswith("post rename") and e.endswith(".bak." + e.rsplit(".", 1)[-1])
                   and "_rpt.txt" in e and ".commit" not in e]
    index = retractions[-1] if names_present == 0 else retractions[0]
    proc = publish(L["state"], gens["dirs"]["new"], popen=True,
                   env_extra={**interrupt_env, "I2_STOP_AT": str(index), "I2_REACHED": str(tmp_path / "reached")})
    deadline = time.monotonic() + 60
    while not (tmp_path / "reached").exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    pid = int((tmp_path / "reached").read_text().split("\t", 1)[0])
    while subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()[:1] != "T":
        time.sleep(0.005)
    try:
        assert len([p for p in PAIR if (L["state"] / NAMES[p]).exists()]) == names_present
        backup_round(L) if via == "main_nf_sections" else helper_backup_round(L)
        for dest in (L["ongoing"], L["current"] / "tables"):
            assert pair_generation(dest, gens) == "new"  # the record committed before the retraction
            assert set(resolve(dest, "any")["paths"]) == set(PRODUCTS)
    finally:
        os.kill(pid, signal.SIGCONT)
        proc.communicate(timeout=60)
    assert proc.returncode == 0


RESIDUE_SUFFIXES = [".tmp", ".tmp.4242", ".temp", ".journal", ".journal-1", ".bak", ".bak.77", ".part", ".partial",
                    ".new", ".old", ".orig", "~"]


@pytest.mark.parametrize("product", PRODUCTS)
@pytest.mark.parametrize("suffix", RESIDUE_SUFFIXES + [".gen-" + "a" * 64, "dot-rsync"])
def test_table_named_residue_is_never_pristine(tmp_path, gens, product, suffix):
    """G: governed residue -- a governed table name followed by a transaction
    suffix, or a dot-prefixed partial copy of it -- is never pristine absence;
    the publisher refuses every residue that no publication of the barcode leaves."""
    state = new_state(tmp_path)
    name = f".{NAMES[product]}.Ab12Cd" if suffix == "dot-rsync" else NAMES[product] + suffix
    (state / name).write_bytes(gens["expected"]["old"][product])
    res = resolve(state)
    assert not res["ok"] and "governed residue is present" in res["error"] and name in res["error"], res
    before = tree_snapshot(state)
    result = publish(state, gens["dirs"]["new"], check=False)
    if suffix.startswith(".gen-"):  # an interrupted first publication's member: the retry supersedes it
        assert result.returncode == 0, result.stderr
        assert resolved_generation(state, gens) == "new" and not (state / name).exists()
    else:
        assert result.returncode != 0 and "publication refused" in result.stderr, result.stderr
        assert tree_snapshot(state) == before


@pytest.mark.parametrize("name", ["other.tmp", "notes.journal", f"{BARCODE}_reads_time_rpt.txt.tmp",
                                  f"{BARCODE}_blast_otu_taxonomy_v1.tsv.tmp", f"X{NAMES['public']}.tmp", "tmp.journal"])
def test_unrelated_temp_files_never_block(tmp_path, gens, name):
    state = new_state(tmp_path)
    (state / name).write_text("unrelated\n")
    assert resolve(state)["mode"] == "absent"
    publish(state, gens["dirs"]["new"])
    assert resolved_generation(state, gens) == "new" and (state / name).read_text() == "unrelated\n"


def _earlier_legacy_backup(dest: Path, sealed_state: Path):
    """The legacy backup record an earlier candidate wrote of a sealed legacy
    generation (no provenance line), with its members and projections."""
    dest.mkdir(parents=True, exist_ok=True)
    lines = (sealed_state / RECORD).read_text().split("\n")
    head = f"{LEGACY_BACKUP_HEADER}\n{lines[1]}\nprevious\tNA\n" + "".join(l + "\n" for l in lines[3:5])
    (dest / RECORD).write_text(head + f"#END\t{sha(head.encode())}\n")
    gen = lines[1].split("\t")[1]
    for p in PAIR:
        shutil.copy2(sealed_state / f"{NAMES[p]}.gen-{gen}", dest / f"{NAMES[p]}.gen-{gen}")
        os.link(dest / f"{NAMES[p]}.gen-{gen}", dest / NAMES[p])
    return head + f"#END\t{sha(head.encode())}\n", gen


def test_an_earlier_candidates_legacy_backup_is_served_replicated_and_restored(pre_r4d, tmp_path, gens):
    """The corrected candidate's legacy backup (both tables, no provenance line) is a
    complete legacy generation: served in `_state` where its restore left it,
    replicated as it is, and installed by a restore."""
    probe = clone_pre_r4d(pre_r4d, tmp_path / "probe" / "S1")
    assert resolve(probe)["kind"] == "legacy"  # sealed
    L = results_layout(tmp_path / "out")
    text, gen = _earlier_legacy_backup(L["state"], probe)  # as that candidate's restore left `_state`
    res = resolve(L["state"])
    assert res["ok"] and res["kind"] == "legacy-backup" and res["generation"] == gen
    backup_round(L)
    for dest in (L["ongoing"], L["current"] / "tables"):
        assert (dest / RECORD).read_text() == text
    shutil.rmtree(L["state"])
    L["state"].mkdir()
    result = restart_restore(tmp_path / "out")
    assert result.returncode == 0, result.stderr
    assert (L["state"] / RECORD).read_text() == text and resolve(L["state"])["kind"] == "legacy-backup"
    publish(L["state"], gens["dirs"]["new"])
    assert committed_names(L["state"])[1] == gen and resolved_generation(L["state"], gens) == "new"


# A concurrent sealing (another reader, or the publisher) that commits while a reader
# validates the record-less snapshot, forced deterministically: the validation first
# lets another process seal the state. The record that appeared is the answer.
RACE_WRAPPER = r"""
use strict;
use warnings;
my ($module, $sub, @args) = @ARGV;
require $module;
{
    no warnings 'redefine';
    my $orig = \&RTBioScan::R4DCumulative::__SUB__;
    my $done = 0;
    *RTBioScan::R4DCumulative::__SUB__ = sub {
        if (!$done++) {
            system('perl', '-e', 'require $ARGV[0]; RTBioScan::R4DCumulative::resolve($ARGV[1], $ARGV[2]);',
                   $module, $ENV{RACE_STATE}, $ENV{RACE_BC}) == 0 or die "concurrent sealing failed\n";
        }
        return $orig->(@_);
    };
}
"""


def test_a_sealing_committed_during_a_readers_validation_is_served(pre_r4d, tmp_path):
    state = clone_pre_r4d(pre_r4d, tmp_path / "S1")
    wrapper = tmp_path / "race.pl"
    wrapper.write_text(RACE_WRAPPER.replace("__SUB__", "validate_pre_r4d")
                       + 'my $r = RTBioScan::R4DCumulative::resolve($args[0], $args[1]); print "$r->{mode} $r->{kind}\\n";\n')
    result = subprocess.run(["perl", str(wrapper), str(RESOLVER), "validate_pre_r4d", str(state), BARCODE], capture_output=True,
                            text=True, env={**os.environ, "RACE_STATE": str(state), "RACE_BC": BARCODE})
    assert result.returncode == 0 and result.stdout == "committed legacy\n", result.stderr


PUBLISHER_RACE_WRAPPER = r"""
use strict;
use warnings;
use File::Basename ();
my ($module, $helper, @args) = @ARGV;
require $module;
{
    no warnings 'redefine';
    my $orig = \&RTBioScan::R4DCumulative::classify_publication;
    my $done = 0;
    *RTBioScan::R4DCumulative::classify_publication = sub {
        if (!$done++) {
            system('perl', '-e', 'require $ARGV[0]; RTBioScan::R4DCumulative::resolve($ARGV[1], $ARGV[2]);',
                   $module, $ENV{RACE_STATE}, $ENV{RACE_BC}) == 0 or die "concurrent sealing failed\n";
        }
        return $orig->(@_);
    };
}
require FindBin;
$FindBin::Bin = File::Basename::dirname($helper);
require $helper;
@ARGV = @args;
RTBioScan::R4D::main();
"""


def test_a_sealing_committed_during_the_publishers_classification_does_not_refuse(pre_r4d, tmp_path, gens):
    state = clone_pre_r4d(pre_r4d, tmp_path / "S1")
    wrapper = tmp_path / "race.pl"
    wrapper.write_text(PUBLISHER_RACE_WRAPPER)
    args = ["--publish-cumulative", "--reporting", str(gens["dirs"]["new"] / NAMES["reporting"]), "--state-dir", str(state),
            "--barcode", BARCODE, "--noadapter-enabled", "1"]
    result = subprocess.run(["perl", str(wrapper), str(RESOLVER), str(HELPER), *args], capture_output=True, text=True,
                            env={**os.environ, **ENV, "RACE_STATE": str(state), "RACE_BC": BARCODE})
    assert result.returncode == 0, result.stderr
    # sealed by the concurrent reader, then superseded by this publication (its predecessor)
    assert resolved_generation(state, gens) == "new" and (state / RECORD).read_text().split("\n")[2] != "previous\tNA"

def restore_prepare(outdir: Path, L):
    script = "require $ARGV[0]; exit RTBioScan::R4DCumulative::restore_cli(@ARGV[1 .. $#ARGV]);"
    return subprocess.run(["perl", "-e", script, str(RESOLVER), "prepare", str(L["state"]),
                           str(L["current"]), str(outdir / "temp" / "current" / "state" / "S1")],
                          capture_output=True, text=True, check=False)


def test_restore_rejects_sidecar_only_before_wiping_live_generation(tmp_path, gens, render):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    tables = L["current"] / "tables"
    tables.mkdir(parents=True)
    (tables / NAMES["reporting"]).write_bytes(gens["expected"]["new"]["reporting"])
    (tables / "ordinary_rpt.txt").write_text("ordinary\n")
    run_fixture = _run_json_state(tmp_path / "run_fixture")
    for path in run_fixture.iterdir():
        shutil.copy2(path, L["state"] / path.name)
    before = tree_snapshot(L["state"])
    generation = committed_names(L["state"])[0]
    preflight = restore_prepare(tmp_path, L)
    assert preflight.returncode != 0 and "incomplete backup" in preflight.stderr, (preflight.stdout, preflight.stderr)
    assert tree_snapshot(L["state"]) == before
    result = restart_restore(tmp_path)
    assert result.returncode != 0 and "incomplete backup" in result.stderr, (result.stdout, result.stderr)
    assert tree_snapshot(L["state"]) == before
    assert committed_names(L["state"])[0] == generation
    assert resolve(L["state"])["generation"] == generation
    assert not (tmp_path / "temp" / ".restart_applied.S1").exists()
    assert reader_generations(L["state"], tmp_path / "readers", gens, render) == {
        "resolver": "old", "round_json": "old", "render": "old", "backup": "old"}
    run_out = tmp_path / "run_report.json"
    run_result = subprocess.run(["perl", str(REPORT_RUN_JSON), "--history", str(L["state"] / "report_history.jsonl"),
                                 "--out", str(run_out), "--run-id", "runA", "--barcode", BARCODE,
                                 "--state-id", "state1", "--outdir", "results",
                                 "--report-rel-path", "runs/runA/report.html"],
                                cwd=tmp_path, capture_output=True, text=True, check=False)
    assert run_result.returncode == 0, run_result.stderr
    assert "run_status_read_fate" in json.loads(run_out.read_text()), run_result.stderr
    assert residue(L["state"]) == []
    # Correcting the snapshot permits the same operation to be retried.
    for p in ("public", "noadapter"):
        (tables / NAMES[p]).write_bytes(gens["expected"]["new"][p])
    result = restart_restore(tmp_path, op="d")
    assert result.returncode == 0, result.stderr
    assert resolved_generation(L["state"], gens) == "new"


@pytest.mark.parametrize("mask", range(1, 8), ids=lambda mask: f"subset_{mask:03b}")
def test_restore_three_member_subset_requires_complete_authentication(tmp_path, gens, mask):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    publish(L["state"], gens["dirs"]["new"])
    publish(L["state"], gens["dirs"]["third"])
    tables = L["current"] / "tables"
    tables.mkdir(parents=True)
    for bit, p in enumerate(PRODUCTS):
        if mask & (1 << bit):
            (tables / NAMES[p]).write_bytes(gens["expected"]["old"][p])
    before = tree_snapshot(L["state"])
    result = restore_prepare(tmp_path, L)
    if mask == 7:  # the complete, sealed R4-D sidecar authenticates its projections
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0, (mask, result.stdout, result.stderr)
        restart = restart_restore(tmp_path)
        assert restart.returncode != 0, (mask, restart.stdout, restart.stderr)
        assert not (tmp_path / "temp" / ".restart_applied.S1").exists()
    assert tree_snapshot(L["state"]) == before


@pytest.mark.parametrize("name", [f"{RECORD}.bak.77", f"{NAMES['reporting']}.tmp",
                                  f"{NAMES['public']}.gen-{'a' * 64}", f".r4d-publish-{BARCODE}-Ab12Cd",
                                  ".r4d-publish-Ab12Cd"])
def test_restore_rejects_governed_residue_without_members(tmp_path, gens, name):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    tables = L["current"] / "tables"
    tables.mkdir(parents=True)
    (tables / name).write_text("partial\n")
    before = tree_snapshot(L["state"])
    result = restore_prepare(tmp_path, L)
    assert result.returncode != 0 and ("incomplete backup" in result.stderr or "governed transaction residue" in result.stderr), (name, result.stdout, result.stderr)
    restart = restart_restore(tmp_path)
    assert restart.returncode != 0, (name, restart.stdout, restart.stderr)
    assert not (tmp_path / "temp" / ".restart_applied.S1").exists()
    assert tree_snapshot(L["state"]) == before


def test_restore_rejects_three_members_with_unmatched_sidecar(tmp_path, gens):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["third"])
    tables = L["current"] / "tables"
    tables.mkdir(parents=True)
    for p in PRODUCTS:
        value = "new" if p == "reporting" else "old"
        (tables / NAMES[p]).write_bytes(gens["expected"][value][p])
    before = tree_snapshot(L["state"])
    result = restore_prepare(tmp_path, L)
    assert result.returncode != 0 and "does not authenticate" in result.stderr, result.stderr
    restart = restart_restore(tmp_path)
    assert restart.returncode != 0 and "does not authenticate" in restart.stderr, restart.stderr
    assert not (tmp_path / "temp" / ".restart_applied.S1").exists()
    assert tree_snapshot(L["state"]) == before


def test_restore_unrelated_report_only_is_pristine_without_done_ledger(tmp_path, gens):
    L = results_layout(tmp_path)
    publish(L["state"], gens["dirs"]["old"])
    tables = L["current"] / "tables"
    tables.mkdir(parents=True)
    (tables / "ordinary_rpt.txt").write_text("ordinary\n")
    before = tree_snapshot(L["state"])
    result = restore_prepare(tmp_path, L)
    assert result.returncode == 0 and "Odd number of elements" not in result.stderr, result.stderr
    assert tree_snapshot(L["state"]) == before
