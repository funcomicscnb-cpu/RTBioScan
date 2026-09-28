"""Real-wrapper contract tests for the inherited Dorado state lock."""
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "bin" / "with_dorado_lock.sh"
HELPER = ROOT / "bin" / "lib" / "fd_lock.pl"


@pytest.fixture
def fake_dorado(tmp_path):
    script = tmp_path / "fake_dorado.py"
    script.write_text(
        "import os, pathlib, sys, time\n"
        "mode, marker, done = sys.argv[1:4]\n"
        "pathlib.Path(marker).write_text(str(os.getpid()))\n"
        "sys.stdout.buffer.write(b'@HD\\tVN:1.6\\nread\\tACGT\\n')\n"
        "sys.stdout.flush()\n"
        "if mode == 'sleep': time.sleep(3)\n"
        "pathlib.Path(done).write_text('done')\n"
        "if mode != 'sleep': sys.exit(int(mode))\n"
    )
    return script


def command(tmp_path, fake_dorado, wait="2", mode="0"):
    state = tmp_path / "_state"
    state.mkdir(exist_ok=True)
    target = state / ".dorado.lock"
    marker = tmp_path / "child.pid"
    done = tmp_path / "done"
    argv = [str(WRAPPER), str(target), wait, "test", "--",
            sys.executable, str(fake_dorado), mode, str(marker), str(done)]
    return argv, target, marker, done


def until(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


@pytest.mark.parametrize("status", [0, 1, 2, 126, 127, 134, 137, 139, 143, 255])
def test_exact_child_status_and_output(tmp_path, fake_dorado, status):
    argv, target, marker, done = command(tmp_path, fake_dorado, mode=str(status))
    result = subprocess.run(argv, capture_output=True)
    assert result.returncode == status
    assert result.stdout == b"@HD\tVN:1.6\nread\tACGT\n"
    assert marker.exists() and done.exists()
    until(lambda: not Path(str(target) + ".lockdir").exists())
    assert Path(str(target) + ".flock").is_file()
    assert b"token=" in Path(str(target) + ".flock").read_bytes()


@pytest.mark.parametrize("wait", ["1", "2", "300", "0", "000300"])
def test_valid_waits_run_dorado(tmp_path, fake_dorado, wait):
    argv, _, marker, _ = command(tmp_path, fake_dorado, wait=wait)
    result = subprocess.run(argv, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert marker.exists()


def test_existing_lock_wait_environment_is_preserved(tmp_path):
    target = tmp_path / "_state" / ".dorado.lock"
    target.parent.mkdir()
    env = os.environ.copy()
    env["LOCK_WAIT"] = "caller-value"
    result = subprocess.run(
        [str(WRAPPER), str(target), "0", "environment", "--",
         sys.executable, "-c", "import os; print(os.environ['LOCK_WAIT'])"],
        capture_output=True, env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"caller-value\n"


@pytest.mark.parametrize("wait", ["-1", "1.5", "", "abc", "86401", "999999999999999999999"])
def test_invalid_wait_fails_before_dorado(tmp_path, fake_dorado, wait):
    argv, _, marker, _ = command(tmp_path, fake_dorado, wait=wait)
    result = subprocess.run(argv, capture_output=True)
    assert result.returncode == 2
    assert not marker.exists()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_dead_wrapper_cannot_release_live_child(tmp_path, fake_dorado, signum):
    argv, target, marker, done = command(tmp_path, fake_dorado, wait="2", mode="sleep")
    with (tmp_path / "out").open("wb") as out, (tmp_path / "err").open("wb") as err:
        wrapper = subprocess.Popen(argv, stdout=out, stderr=err)
        try:
            until(marker.exists)
            child_pid = int(marker.read_text())
            held = subprocess.check_output(["lsof", "-a", "-p", str(child_pid)], text=True)
            assert ".rtbioscan_state_reset.flock" in held
            assert ".dorado.lock.flock" in held
            os.kill(wrapper.pid, signum)
            if signum == signal.SIGKILL:
                assert wrapper.wait(timeout=2) == -signal.SIGKILL
            assert not done.exists()
            contender = subprocess.run([str(WRAPPER), str(target), "1", "contender", "--", "true"], capture_output=True)
            assert contender.returncode != 0
            assert not done.exists()
            until(done.exists, 5)
            if signum == signal.SIGTERM:
                assert wrapper.wait(timeout=2) == 143
            next_run = subprocess.run([str(WRAPPER), str(target), "2", "next", "--", "true"], capture_output=True)
            assert next_run.returncode == 0, next_run.stderr
        finally:
            if wrapper.poll() is None:
                wrapper.kill()
                wrapper.wait()


@pytest.mark.parametrize("record,fragment", [
    (None, b"LEGACY_LOCK_UNOWNED"),
    ("pid=1\nhost={host}\n", b"timed out"),
    ("pid=99999999\nhost=foreign.invalid\n", b"timed out"),
    ("broken\n", b"corrupt compatibility fence"),
])
def test_ambiguous_legacy_fence_is_preserved(tmp_path, fake_dorado, record, fragment):
    argv, target, marker, _ = command(tmp_path, fake_dorado, wait="1")
    fence = Path(str(target) + ".lockdir")
    fence.mkdir()
    if record is not None:
        (fence / "meta.env").write_text(record.format(host=socket.gethostname()))
    result = subprocess.run(argv, capture_output=True)
    assert result.returncode != 0
    assert fragment in result.stderr
    assert fence.exists() and not marker.exists()
    if record is None:
        expected = f"perl '{HELPER}' adopt-legacy '{fence}' --confirm".encode()
        assert expected in result.stderr


def test_confirmed_dead_local_legacy_owner_is_reclaimed(tmp_path, fake_dorado):
    argv, target, marker, _ = command(tmp_path, fake_dorado, wait="1")
    fence = Path(str(target) + ".lockdir")
    fence.mkdir()
    (fence / "meta.env").write_text(f"pid=99999999\nhost={socket.gethostname()}\n")
    result = subprocess.run(argv, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert marker.exists()
    until(lambda: not fence.exists())
    assert "reclaim-legacy complete" in (target.parent / ".lock_migration.log").read_text()


def test_explicit_ownerless_adoption(tmp_path, fake_dorado):
    argv, target, marker, _ = command(tmp_path, fake_dorado, wait="1")
    fence = Path(str(target) + ".lockdir")
    fence.mkdir()
    refused = subprocess.run(argv, capture_output=True)
    assert refused.returncode != 0 and fence.is_dir() and not marker.exists()
    adopted = subprocess.run(["perl", str(HELPER), "adopt-legacy", str(fence), "--confirm"], capture_output=True)
    assert adopted.returncode == 0, adopted.stderr
    assert not fence.exists()
    result = subprocess.run(argv, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert marker.exists()
