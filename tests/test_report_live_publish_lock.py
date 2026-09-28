"""Stage A2-1 publication lock integration; all mutable state is scratch."""
from __future__ import annotations

import hashlib
import os
import pathlib
import signal
import socket
import subprocess
import time

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PUBLISH = ROOT / 'bin/report_live_publish.sh'
RESET = ROOT / 'bin/restart_handler.sh'


def test_reset_declarations_and_cutover_control_inventory():
    from tests.test_restart_round_lock_cutover import CONTROL
    name = '.report_live_publish.lock.flock'
    assert name in CONTROL
    assert RESET.read_text().count(name) == 3


def make_fixture(tmp_path, name='one'):
    base = tmp_path / name
    state = base / 'out/temp/ongoing/state/SID/_state'
    run = base / 'run'
    stage = base / 'stage'
    for path in (state, run, stage / 'report_assets', stage / 'tables', stage / 'sequences'):
        path.mkdir(parents=True)
    for relative, data in (
        ('README.html', b'<html>fixed oracle</html>\n'),
        ('report_assets/a.txt', b'asset\x00bytes\n'),
        ('tables/t.tsv', b'x\ty\n1\t2\n'),
        ('sequences/s.fa', b'>s\nACGT\n'),
    ):
        (stage / relative).write_bytes(data)
    target = state / '.report_live_publish.lock'
    cmd = ['/bin/bash', str(PUBLISH), '--stage-root', str(stage),
           '--state-root', str(state), '--run-asset-root', str(run),
           '--round-barcode', 'R1', '--lock-path', str(target)]
    return base, state, stage, run, target, cmd


def wait_for(predicate, seconds=7):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(.03)
    return predicate()


def env(wait='1', shim=None, **extra):
    result = dict(os.environ, LOCK_WAIT=wait, **extra)
    if shim is not None:
        result['PATH'] = str(shim) + os.pathsep + result['PATH']
    return result


def slow_copy(tmp_path):
    shim = tmp_path / 'shim'
    shim.mkdir()
    cp = shim / 'cp'
    cp.write_text('#!/bin/bash\nprintf entered > "$CP_MARKER"\nsleep 3\nexec /bin/cp "$@"\n')
    cp.chmod(0o755)
    return shim


def reset(base, wait='0'):
    reset_env = env(wait, MODE='reset', OUTDIR=str(base / 'out'), RUN_NAME='a2-test',
                    STATE_ID='SID', FORCE='1', OPERATION_ID='1' * 64)
    return subprocess.run(['/bin/bash', str(RESET)], env=reset_env,
                          capture_output=True, text=True, timeout=20)


def test_success_preserves_bytes_and_stable_owner(tmp_path):
    base, state, stage, run, target, cmd = make_fixture(tmp_path)
    result = subprocess.run(cmd, env=env(), capture_output=True, timeout=20)
    assert (result.returncode, result.stdout, result.stderr) == (0, b'', b'')
    payload = (state / 'live_round').resolve(strict=True)
    assert (run / 'live_round').resolve(strict=True) == payload / 'report_assets'
    expected = {'README.html': b'<html>fixed oracle</html>\n',
                'report_assets/a.txt': b'asset\x00bytes\n',
                'tables/t.tsv': b'x\ty\n1\t2\n', 'sequences/s.fa': b'>s\nACGT\n'}
    assert {str(p.relative_to(payload)): p.read_bytes() for p in payload.rglob('*') if p.is_file()} == expected
    lock = pathlib.Path(str(target) + '.flock')
    before = (lock.stat().st_ino, lock.read_bytes())
    assert before[1].startswith(b'v=2 ') and b'label=.report_live_publish.lock ' in before[1]
    assert not pathlib.Path(str(target) + '.lockdir').exists()
    assert reset(base).returncode == 0
    assert (lock.stat().st_ino, lock.read_bytes()) == before


def test_live_holder_excludes_contender_and_reset(tmp_path):
    base, state, stage, run, target, cmd = make_fixture(tmp_path)
    shim = slow_copy(tmp_path)
    marker = tmp_path / 'copy-entered'
    first = subprocess.Popen(cmd, env=env(shim=shim, CP_MARKER=str(marker)),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert wait_for(marker.exists)
        inode = pathlib.Path(str(target) + '.flock').stat().st_ino
        blocked = subprocess.run(cmd, env=env(), capture_output=True, timeout=10)
        assert blocked.returncode == 2 and b'live holder' in blocked.stderr
        assert reset(base).returncode != 0
        assert pathlib.Path(str(target) + '.flock').stat().st_ino == inode
        assert first.communicate(timeout=10)[1] == b'' and first.returncode == 0
    finally:
        if first.poll() is None: first.kill(); first.communicate()


def test_reset_first_blocks_publisher_then_preserves_inode(tmp_path):
    base, state, stage, run, target, cmd = make_fixture(tmp_path)
    assert reset(base).returncode == 0
    lock = pathlib.Path(str(target) + '.flock')
    inode = lock.stat().st_ino
    (state / 'scientific.txt').write_bytes(b'old')
    shim = tmp_path / 'reset-shim'
    shim.mkdir()
    marker = tmp_path / 'reset-entered'
    rm = shim / 'rm'
    rm.write_text('#!/bin/sh\nif [ ! -e "$RESET_MARKER" ]; then\n'
                  '  : > "$RESET_MARKER"\n  sleep 2\nfi\nexec /bin/rm "$@"\n')
    rm.chmod(0o755)
    reset_env = env('5', shim=shim, MODE='reset', OUTDIR=str(base / 'out'),
                    RUN_NAME='a2-test', STATE_ID='SID', FORCE='1', OPERATION_ID='2' * 64,
                    RESET_MARKER=str(marker))
    holder = subprocess.Popen(['/bin/bash', str(RESET)], env=reset_env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    writer = None
    try:
        assert wait_for(marker.exists)
        writer = subprocess.Popen(cmd, env=env('5'), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        time.sleep(.25)
        assert writer.poll() is None
        assert not (state / 'live_round').exists()
        assert holder.communicate(timeout=15)[1] == b'' and holder.returncode == 0
        assert writer.communicate(timeout=15)[1] == b'' and writer.returncode == 0
        assert lock.stat().st_ino == inode
    finally:
        if holder.poll() is None: holder.kill(); holder.communicate()
        if writer is not None and writer.poll() is None: writer.kill(); writer.communicate()


def test_sigterm_stops_publication_and_retry(tmp_path):
    base, state, stage, run, target, cmd = make_fixture(tmp_path)
    shim = slow_copy(tmp_path)
    marker = tmp_path / 'copy-entered'
    first = subprocess.Popen(cmd, env=env(shim=shim, CP_MARKER=str(marker)),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert wait_for(marker.exists)
        first.send_signal(signal.SIGTERM)
        assert first.communicate(timeout=10)[1] == b''
        assert first.returncode == 143 and stage.exists()
        assert not (state / 'live_round').exists()
        assert wait_for(lambda: not pathlib.Path(str(target) + '.lockdir').exists())
        retry = subprocess.run(cmd, env=env(), capture_output=True, timeout=15)
        assert retry.returncode == 0, retry.stderr
    finally:
        if first.poll() is None: first.kill(); first.communicate()


@pytest.mark.parametrize('whole_group', [False, True])
def test_sigkill_recovers_and_child_retains_lock(tmp_path, whole_group):
    base, state, stage, run, target, cmd = make_fixture(tmp_path)
    shim = slow_copy(tmp_path)
    marker = tmp_path / 'copy-entered'
    first = subprocess.Popen(cmd, env=env(shim=shim, CP_MARKER=str(marker)),
                             start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert wait_for(marker.exists)
        inode = pathlib.Path(str(target) + '.flock').stat().st_ino
        if whole_group: os.killpg(first.pid, signal.SIGKILL)
        else: first.kill()
        assert first.wait(timeout=3) == -signal.SIGKILL
        if not whole_group:
            blocked = subprocess.run(cmd, env=env(), capture_output=True, timeout=10)
            assert blocked.returncode == 2 and b'live holder' in blocked.stderr
            assert wait_for(lambda: not pathlib.Path(str(target) + '.lockdir').exists(), 8)
        first.communicate(timeout=10)
        retry = subprocess.run(cmd, env=env('5'), capture_output=True, timeout=15)
        assert retry.returncode == 0, retry.stderr
        assert pathlib.Path(str(target) + '.flock').stat().st_ino == inode
    finally:
        if first.poll() is None: os.killpg(first.pid, signal.SIGKILL); first.communicate()


@pytest.mark.parametrize('kind,expected', [
    ('dead', b'reclaiming confirmed-dead legacy lock'),
    ('live', b'timed out after'),
    ('foreign', b'timed out after'),
    ('ownerless', b'LEGACY_LOCK_UNOWNED'),
    ('corrupt', b'corrupt compatibility fence'),
])
def test_legacy_compatibility_policy(tmp_path, kind, expected):
    base, state, stage, run, target, cmd = make_fixture(tmp_path)
    fence = pathlib.Path(str(target) + '.lockdir')
    fence.mkdir()
    if kind in ('dead', 'live', 'foreign'):
        pid = 99999999 if kind == 'dead' else os.getpid()
        host = 'foreign-host' if kind == 'foreign' else socket.gethostname()
        (fence / 'meta.env').write_text(f'pid={pid}\nhost={host}\n')
    elif kind == 'corrupt':
        (fence / 'unexpected').write_bytes(b'x')
    result = subprocess.run(cmd, env=env('0'), capture_output=True, timeout=15)
    assert expected in result.stderr
    assert result.returncode == (0 if kind == 'dead' else 2)
    if kind == 'dead':
        assert b'reclaim-legacy complete' in (state / '.lock_migration.log').read_bytes()
    else:
        assert fence.exists() and stage.exists()
