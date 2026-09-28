import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/report_run_index_update.sh"


def invoke(index, mode, run_id="alpha", *, wait="3", env=None, script=SCRIPT):
    lock = index.parent.parent / ".runs_index.lock"
    if mode == "update":
        report = index.parent / f"{run_id}.json"
        report.write_text(json.dumps({"run_id": run_id}) + "\n")
        args = [str(report), str(index), str(lock)]
    else:
        args = [mode, run_id, str(index), str(lock)]
    return subprocess.run(["bash", str(script), *args], capture_output=True,
                          env={**os.environ, "LOCK_WAIT": wait, **(env or {})})


def rows(index):
    return [json.loads(line) for line in index.read_text().splitlines() if line]


def setup_index(tmp_path):
    out = tmp_path / "out"
    report = out / "report_html"
    report.mkdir(parents=True)
    index = report / "runs_index.jsonl"
    index.write_text('{"run_id":"alpha"}\n{"run_id":"beta"}\n')
    return index


def test_clean_filter_temp_is_adjacent_to_index():
    source = SCRIPT.read_text()
    clean = source.split('if ($mode eq "clean") {', 1)[1].split('} else {', 1)[0]
    assert 'dirname($index)' in clean
    assert '-d $index_dir' in clean
    assert 'tempfile("$index.tmp.XXXXXX", UNLINK => 0)' in clean
    assert 'File::Spec->tmpdir()' not in clean


def test_clean_filter_cross_device_and_failed_publish(tmp_path):
    candidates = [os.environ.get("RTB_CROSS_DEVICE_TEST_DIR"), "/dev/shm", "/tmp"]
    other = None
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            if path.is_dir() and path.stat().st_dev != tmp_path.stat().st_dev:
                other = tempfile.TemporaryDirectory(prefix="rtb-clean-cross-device-", dir=path)
                break
        except OSError:
            continue
    if other is None:
        pytest.skip("No second writable filesystem with a different device ID")
    with other:
        index = setup_index(Path(other.name) / "space ñ" / "-leading")
        assert index.parent.stat().st_dev != tmp_path.stat().st_dev
        original = index.read_bytes()
        stable = index.parent.parent / ".runs_index.lock.flock"
        env = {"TMPDIR": str(tmp_path)}

        source = SCRIPT.read_text()
        old_clean = '($out, $tmp) = tempfile("tmp.XXXXXX", DIR => File::Spec->tmpdir(), UNLINK => 0);'
        revised_clean = ('my $index_dir = dirname($index);\n'
                         '    -d $index_dir or fail("run index directory missing: $index_dir");\n'
                         '    ($out, $tmp) = tempfile("$index.tmp.XXXXXX", UNLINK => 0);')
        assert source.count(revised_clean) == 1
        prior_source = source.replace('-MFile::Basename=dirname', '-MFile::Spec').replace(
            revised_clean, old_clean)
        prior_script = tmp_path / "prior_clean_filter.sh"
        prior_script.write_text(prior_source)
        prior = invoke(index, "--clean-filter", env=env, script=prior_script)
        assert prior.returncode == 2 and b"Cross-device link" in prior.stderr
        assert index.read_bytes() == original

        inode = stable.stat().st_ino
        current = invoke(index, "--clean-filter", env=env)
        assert current.returncode == 0, current.stderr
        assert index.read_bytes() == b'{"run_id":"beta"}\n'
        assert stable.stat().st_ino == inode
        assert not list(index.parent.glob("runs_index.jsonl.tmp.*"))

        index.write_bytes(original)
        publish = 'rename($tmp, $index) or die "replace run index: $!";'
        assert source.count(publish) == 1
        failed_script = tmp_path / "forced_publish_failure.sh"
        failed_script.write_text(source.replace(publish, 'die "forced publish failure\\n";'))
        unrelated = index.parent / "runs_index.jsonl.tmp.other"
        unrelated.write_bytes(b"another invocation")
        failed = invoke(index, "--clean-filter", env=env, script=failed_script)
        assert failed.returncode == 2 and b"forced publish failure" in failed.stderr
        assert index.read_bytes() == original
        assert stable.stat().st_ino == inode
        assert unrelated.read_bytes() == b"another invocation"
        assert list(index.parent.glob("runs_index.jsonl.tmp.*")) == [unrelated]
        unrelated.unlink()
        assert not list(index.parent.glob("runs_index.jsonl.tmp.*"))


def test_all_modes_share_permanent_stable_inode(tmp_path):
    index = setup_index(tmp_path)
    stable = index.parent.parent / ".runs_index.lock.flock"
    fence = index.parent.parent / ".runs_index.lock.lockdir"
    for mode, rid in (("update", "gamma"), ("--prune-seeded", "alpha"),
                      ("--clean-filter", "beta")):
        p = invoke(index, mode, rid)
        assert p.returncode == 0, p.stderr
        assert stable.is_file() and stable.stat().st_size == 0
        assert stable.stat().st_nlink == 1
        assert not fence.exists()
        if mode == "update":
            inode = stable.stat().st_ino
        assert stable.stat().st_ino == inode
    assert [r["run_id"] for r in rows(index)] == ["gamma"]


@pytest.mark.parametrize("mode", ["update", "--prune-seeded", "--clean-filter"])
def test_unsafe_stable_paths_refused_before_mutation(tmp_path, mode):
    for kind in ("symlink", "directory", "hardlink"):
        root = tmp_path / kind
        index = setup_index(root)
        stable = index.parent.parent / ".runs_index.lock.flock"
        if kind == "symlink":
            stable.symlink_to(index)
        elif kind == "directory":
            stable.mkdir()
        else:
            stable.write_bytes(b"")
            os.link(stable, root / "second_link")
        before = index.read_bytes()
        p = invoke(index, mode)
        assert p.returncode != 0
        assert index.read_bytes() == before


def test_legacy_directory_wait_and_recovery_command(tmp_path):
    index = setup_index(tmp_path / "space ' ñ")
    lockdir = index.parent.parent / ".runs_index.lock.lockdir"
    lockdir.mkdir()
    before = index.read_bytes()
    p = invoke(index, "--clean-filter", wait="1")
    assert p.returncode == 2 and index.read_bytes() == before
    assert lockdir.is_dir()
    quoted = "'" + str(lockdir).replace("'", "'\"'\"'") + "'"
    assert ("rmdir " + quoted).encode() in p.stderr
    lockdir.rmdir()
    assert invoke(index, "--clean-filter").returncode == 0


def test_leftover_regular_fence_reclaimed(tmp_path):
    index = setup_index(tmp_path)
    fence = index.parent.parent / ".runs_index.lock.lockdir"
    fence.write_bytes(b"")
    assert invoke(index, "--prune-seeded").returncode == 0
    assert not fence.exists()


@pytest.mark.parametrize("count", [2, 8])
def test_mixed_writers_preserve_all_updates(tmp_path, count):
    index = setup_index(tmp_path)
    commands = []
    for i in range(count):
        mode = "update" if i % 2 == 0 else ("--prune-seeded" if i % 4 == 1 else "--clean-filter")
        rid = f"new{i}" if mode == "update" else "alpha"
        lock = index.parent.parent / ".runs_index.lock"
        if mode == "update":
            report = index.parent / f"{rid}.json"
            report.write_text(json.dumps({"run_id": rid}) + "\n")
            args = [str(report), str(index), str(lock)]
        else:
            args = [mode, rid, str(index), str(lock)]
        commands.append(args)
    procs = [subprocess.Popen(["bash", str(SCRIPT), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env={**os.environ, "LOCK_WAIT": "60"})
             for args in commands]
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=65)
        assert proc.returncode == 0, (stdout, stderr)
    actual = [r["run_id"] for r in rows(index)]
    assert "alpha" not in actual
    assert "beta" in actual
    assert {f"new{i}" for i in range(0, count, 2)}.issubset(actual)
    assert len(actual) == 1 + count // 2


def test_surviving_filter_child_keeps_kernel_exclusion(tmp_path):
    index = setup_index(tmp_path)
    shimdir = tmp_path / "shim"
    shimdir.mkdir()
    marker = tmp_path / "child.pid"
    shim = shimdir / "python3"
    shim.write_text('#!/bin/sh\necho "$PPID" > "$INDEX_PARENT_MARKER"\nsleep 3\nexec "' +
                    os.environ.get("INDEX_REAL_PYTHON", "/usr/bin/python3") + '" "$@"\n')
    shim.chmod(0o755)
    env = {**os.environ, "PATH": str(shimdir) + os.pathsep + os.environ["PATH"],
           "INDEX_PARENT_MARKER": str(marker), "LOCK_WAIT": "10"}
    lock = index.parent.parent / ".runs_index.lock"
    proc = subprocess.Popen(["bash", str(SCRIPT), "--prune-seeded", "alpha", str(index), str(lock)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert marker.exists()
    stable = index.parent.parent / ".runs_index.lock.flock"
    fence = index.parent.parent / ".runs_index.lock.lockdir"
    assert stable.is_file() and fence.is_file()
    with stable.open("rb") as stream:
        with pytest.raises(BlockingIOError):
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.kill(int(marker.read_text()), 9)
    proc.wait(timeout=5)
    blocked = invoke(index, "update", "gamma", wait="1")
    assert blocked.returncode == 2
    assert [r["run_id"] for r in rows(index)] == ["alpha", "beta"]
    time.sleep(3)
    assert invoke(index, "update", "gamma").returncode == 0


def test_no_direct_launcher_index_rewrite_remains():
    source = (ROOT / "RTBioScan.sh").read_text()
    prune = source.split("prune_seeded_run_status_if_stale() {", 1)[1].split("\n}", 1)[0]
    clean = source.split('_idx="$_outdir/report_html/runs_index.jsonl"', 1)[1].split(
        "# Regenerate the top-level report.html", 1)[0]
    assert "--prune-seeded" in prune and "--clean-filter" in clean
    assert 'mv "$_tmp" "$_run_index"' not in prune
    assert 'mv "$_tmp" "$_idx"' not in clean


def test_modes_wait_on_exact_same_kernel_inode(tmp_path):
    index = setup_index(tmp_path)
    stable = index.parent.parent / ".runs_index.lock.flock"
    stable.touch()
    fence = index.parent.parent / ".runs_index.lock.lockdir"
    before = index.read_bytes()
    with stable.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        for mode in ("update", "--prune-seeded", "--clean-filter"):
            p = invoke(index, mode, "gamma", wait="1")
            assert p.returncode == 2, (mode, p.stderr)
            assert index.read_bytes() == before
            assert not fence.exists()  # Fence must follow kernel acquisition.
        fcntl.flock(stream, fcntl.LOCK_UN)
    assert invoke(index, "update", "gamma").returncode == 0


@pytest.mark.parametrize("args", [
    [], ["--prune-seeded"], ["--clean-filter", "x"], ["--other", "x", "y", "z"],
    ["--prune-seeded", "", "x", "y"], ["--clean-filter", "x", "y", "z", "extra"],
])
def test_invalid_modes_do_not_fall_through(args):
    p = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True)
    assert p.returncode == 2


def test_stable_path_swap_while_held_blocks_mutation(tmp_path):
    index = setup_index(tmp_path)
    before = index.read_bytes()
    shimdir = tmp_path / "shim"
    shimdir.mkdir()
    marker = tmp_path / "child.ready"
    shim = shimdir / "python3"
    shim.write_text('#!/bin/sh\ntouch "$INDEX_CHILD_MARKER"\nsleep 1\nexec /usr/bin/python3 "$@"\n')
    shim.chmod(0o755)
    env = {**os.environ, "PATH": str(shimdir) + os.pathsep + os.environ["PATH"],
           "INDEX_CHILD_MARKER": str(marker)}
    lock = index.parent.parent / ".runs_index.lock"
    p = subprocess.Popen(["bash", str(SCRIPT), "--prune-seeded", "alpha", str(index), str(lock)],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert marker.exists()
    stable = index.parent.parent / ".runs_index.lock.flock"
    saved = index.parent.parent / "saved.stable"
    os.replace(stable, saved)
    stable.touch()
    _, stderr = p.communicate(timeout=5)
    assert p.returncode == 2, stderr
    assert index.read_bytes() == before


def test_two_simultaneous_maintenance_writers_preserve_both_removals(tmp_path):
    index = setup_index(tmp_path)
    lock = index.parent.parent / ".runs_index.lock"
    commands = [
        ["--prune-seeded", "alpha", str(index), str(lock)],
        ["--clean-filter", "beta", str(index), str(lock)],
    ]
    procs = [subprocess.Popen(["bash", str(SCRIPT), *args], stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE) for args in commands]
    for proc in procs:
        _, stderr = proc.communicate(timeout=5)
        assert proc.returncode == 0, stderr
    assert rows(index) == []
