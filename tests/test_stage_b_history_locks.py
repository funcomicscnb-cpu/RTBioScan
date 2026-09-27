"""Stage B all-holder, reset, pending and snapshot integration probes."""
import json
import os
import pathlib
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time

import pytest
from tests.test_fd_lock import (BASH, SHELL, A0_HANDLER, run_lock, wait_for, assert_drained, a0_env, run_a0, a0_state)
from tests.test_report_history_state import ROOT, fixture, finalizer, locked_reconcile, H
from tests.test_r5_final_design_regressions import fresh_v4_fixture, run_actual_worker, authenticated_fixture, publication, REPO, AUTH

def test_real_a0_writer_first_refuses_then_preserves_controls(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    first = run_a0(outdir)
    assert first.returncode == 0, first.stderr
    target = state / ".report_history.lock"
    barrier = state / ".rtbioscan_state_reset.flock"
    inode = (barrier.stat().st_ino, pathlib.Path(str(target) + ".flock").stat().st_ino)
    ready = tmp_path / "writer-ready"
    owner = subprocess.Popen(
        [BASH, "-c", 'set -euo pipefail; source "$1"; init_lock_helpers; '
         'acquire_lock "$2"; : > "$3"; sleep 1; release_lock "$2"',
         "_", str(SHELL), str(target), str(ready)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert wait_for(ready.exists)
        fence = pathlib.Path(str(target) + ".lockdir")
        assert fence.is_dir()
        refused = run_a0(outdir, operation="b")
        assert refused.returncode != 0 and "exclusive reset barrier" in refused.stderr
        assert fence.is_dir()
        assert owner.wait(timeout=4) == 0
        assert_drained(target)
        lock_file = pathlib.Path(str(target) + ".flock")
        record = lock_file.read_bytes()
        assert record.startswith(b"v=2 ")
        binding = state / ".rtbioscan_lock_host_v1"
        host_record = binding.read_bytes()
        audit = state / ".lock_migration.log"
        audit.write_bytes(b"preserve audit\n")
        completed = run_a0(outdir, operation="b")
        assert completed.returncode == 0, completed.stderr
        assert (barrier.stat().st_ino, lock_file.stat().st_ino) == inode
        assert lock_file.read_bytes() == record
        assert binding.read_bytes() == host_record
        assert audit.read_bytes() == b"preserve audit\n"
        assert run_lock(target).returncode == 0
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_real_a0_reset_first_blocks_state_lock_and_fence(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    assert run_a0(outdir).returncode == 0
    target = state / ".report_history.lock"
    lock_file = pathlib.Path(str(target) + ".flock")
    inode = (state / ".rtbioscan_state_reset.flock").stat().st_ino, lock_file.stat().st_ino
    (state / "scientific.txt").write_bytes(b"wipe me")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    entered = tmp_path / "reset-entered"
    shim = shim_dir / "rm"
    shim.write_text('#!/bin/sh\n'
                    'if [ ! -e "$RTB_A0_ENTERED" ]; then\n'
                    '  : > "$RTB_A0_ENTERED"\n'
                    '  sleep 1\n'
                    'fi\n'
                    'exec /bin/rm "$@"\n')
    shim.chmod(0o755)
    env = a0_env(outdir, operation="b", wait="2")
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    env["RTB_A0_ENTERED"] = str(entered)
    reset = subprocess.Popen([BASH, str(A0_HANDLER)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    writer_done = tmp_path / "writer-entered"
    writer = None
    try:
        assert wait_for(entered.exists, seconds=5)
        writer = subprocess.Popen(
            [BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; '
             'init_lock_helpers; acquire_lock "$2"; : > "$3"; release_lock "$2"',
             "_", str(SHELL), str(target), str(writer_done)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.2)
        assert not writer_done.exists()
        assert not pathlib.Path(str(target) + ".lockdir").exists()
        _, reset_err = reset.communicate(timeout=10)
        assert reset.returncode == 0, reset_err
        _, writer_err = writer.communicate(timeout=6)
        assert writer.returncode == 0, writer_err
        assert writer_done.exists()
        assert_drained(target)
        assert ((state / ".rtbioscan_state_reset.flock").stat().st_ino,
                lock_file.stat().st_ino) == inode
    finally:
        for proc in (writer, reset):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)



def hold_history(state,ready):
    return subprocess.Popen([BASH,'-c','set -euo pipefail; source "$1"; init_lock_helpers; acquire_lock "$2"; : > "$3"; sleep 60',
                             '_',str(SHELL),str(state/'_state/.report_history.lock'),str(ready)],
                            start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def test_live_holder_bounded_finalizer_and_killed_holder_recovery(tmp_path):
    state,hist,rows=fixture(tmp_path);out=tmp_path/'out';ready=tmp_path/'ready'
    owner=hold_history(state,ready)
    try:
        assert wait_for(ready.exists)
        target=state/'_state/.report_history.lock.flock';inode=target.stat().st_ino;before=target.read_bytes()
        started=time.monotonic();result=finalizer(state,out)
        assert result.returncode==73,result.stderr
        assert time.monotonic()-started<5
        assert target.stat().st_ino==inode and target.read_bytes()==before
        assert (out/'report_html/runs/run/.report_history_pending').is_file()
    finally:
        os.killpg(owner.pid,signal.SIGKILL);owner.wait(timeout=3)
    result=finalizer(state,out);assert result.returncode==0,result.stderr
    assert target.stat().st_ino==inode
    assert hist.read_bytes()==b''.join(rows)
    assert_drained(state/'_state/.report_history.lock')


def test_old_bare_compatibility_fence_refuses_without_age_wait(tmp_path):
    state,hist,rows=fixture(tmp_path);fence=state/'_state/.report_history.lock.lockdir';fence.mkdir()
    started=time.monotonic();result=finalizer(state,tmp_path/'out')
    assert result.returncode==73 and b'adopt-legacy' in result.stderr
    assert time.monotonic()-started<5 and fence.exists() and not list(fence.iterdir())
    assert hist.read_bytes()==b''.join(rows)


def test_three_holders_use_one_stable_inode_and_future_rows_survive_worker(tmp_path):
    live,helper,token,pin,rd=fresh_v4_fixture(tmp_path)
    rows=[]
    for i in (1,2):
        d=live/f'runA_{i}';d.mkdir()
        raw=(json.dumps(dict(schema_version='1.6',state_id='s',barcode='b',run_id='runA',round_barcode=d.name), separators=(', ',': '))+'\n').encode()
        (d/'round_report.json').write_bytes(raw);rows.append(raw)
    (live/'_state/round_index.tsv').write_text('runA_0\t1\nrunA_1\t2\nrunA_2\t3\n')
    hist=live/'_state/report_history.jsonl';hist.write_bytes(hist.read_bytes()+b''.join(rows))
    control,out=run_actual_worker(tmp_path,live,helper,token,pin,'b','runA_0')
    assert hist.read_bytes().endswith(b''.join(rows))
    assert len(hist.read_bytes().split(b'\n'))==4
    target=live/'_state/.report_history.lock.flock';inode=target.stat().st_ino
    env={**os.environ,'RTB_HISTORY_CURRENT':'runA_0','RTB_HISTORY_STATE':str(live),'RTB_HISTORY_OUTDIR':str(out)}
    result=subprocess.run([BASH,str(ROOT/'bin/report_history_append.sh'),str(rd/'round_report.json'),str(hist),str(live/'_state/.report_history.lock')],env=env,capture_output=True)
    assert result.returncode==0,result.stderr
    command=[sys.executable,'-B',str(ROOT/'bin/report_history_state.py'),'finalize','--state-dir',str(live),'--current-round-barcode','runA_0','--run-id','runA','--barcode','b','--outdir',str(out)]
    result=subprocess.run(command,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert target.stat().st_ino==inode and hist.read_bytes().endswith(b''.join(rows))
    assert_drained(live/'_state/.report_history.lock')


def test_scientific_snapshot_seals_with_reporting_pending_and_restore_is_invalid(tmp_path,monkeypatch):
    # Inject only the advisory marker before the existing independently tested transaction.
    import tests.test_r5_final_design_regressions as R
    original=R.authenticated_fixture
    def pending_fixture(path):
        live,helper,token,pin,env=original(path)
        (live/'_state/.report_history_pending').write_text('advisory only\n')
        (live/'_state/report_history.jsonl').write_bytes(b'partial reporting')
        return live,helper,token,pin,env
    monkeypatch.setattr(R,'authenticated_fixture',pending_fixture)
    result,live,t1,t2,candidate,env=R.publication(tmp_path)
    assert result.returncode==0,result.stderr
    assert (t1/'state_authority/AUTHORITY').read_bytes()==(t2/'state_authority/AUTHORITY').read_bytes()
    # Structured snapshots lack retained report inputs; derived copies cannot supply authority.
    restored=tmp_path/'restored';restored.mkdir()
    import shutil
    shutil.copytree(t1/'state_authority',restored/'_state')
    assert H.History(restored,'runA_0').classify()=='invalid_authority'


def test_no_raw_history_holder_or_wait_inversion_remains():
    main=(ROOT/'main.nf').read_text();append=(ROOT/'bin/report_history_append.sh').read_text()
    worker=(ROOT/'bin/state_snapshot_authority.pl').read_text().split('RTB_WORKER_SHELL',2)[1]
    assert 'mkdir "${REPORT_HISTORY_LOCK}.lockdir"' not in worker
    assert 'stale_lock_maybe_reclaim' not in worker
    assert 'acquire_lock "$REPORT_HISTORY_LOCK" || exit 74' in worker
    assert 'mkdir "$LOCK_DIR"' not in append
    assert '[ "\\${HISTORY_WAIT_USED:-0}" -eq 0 ] || wait_limit=0' in main
    async_body=main.split('process async_report_render {',1)[1].split('workflow.onComplete',1)[0]
    assert async_body.index('release_lock "\\$REPORT_HISTORY_LOCK"') < async_body.index('render_lock_acquired=1',async_body.index('snapshot_revision='))
    assert async_body.rindex('release_lock_dir "\\$REPORT_ROOT_RENDER_LOCK"') < async_body.rindex('acquire_lock "\\$REPORT_HISTORY_LOCK"')


def section8_functions(state,out,current='r3'):
    text=(ROOT/'main.nf').read_text()
    source=text[text.index('\t\thistory_lock_acquired=0'):text.index('\t\t\treport_live_publish_rc=0')]
    for old,new in {'${params.lock_wait_seconds}':'2','${baseDir}':str(ROOT),'${ongoingStateDir}':str(state),
                    '${round_barcode}':current,'${run_name}':'run','${barcode}':'b','${outdirResolved}':str(out)}.items():
        source=source.replace(old,new)
    return source.replace('\\$', '$')


def out_of_order_section8(state, out, current, request, run='run', barcode='b'):
    """Execute the production §8 call, derived publication, recheck and request text."""
    text = (ROOT / 'main.nf').read_text()
    source = (text[text.index('\t\thistory_lock_acquired=0'):text.index('\t\t\treport_live_publish_rc=0')]
              + text[text.index('\t\t\tset +e\n\t\t\tpublish_report_history\n'):text.index('\t\t\twrite_report_metadata_files() {')]
              + text[text.index("\t\t\t\tprintf 'render=0\\nround_barcode=%s\\n'"):text.index('\t\tif [ -d \\$ROUND_TMP/ ]')])
    replacements = {'${params.lock_wait_seconds}': '2', '${baseDir}': str(ROOT), '${ongoingStateDir}': str(state),
                    '${round_barcode}': current, '${run_name}': run, '${barcode}': barcode,
                    '${outdirResolved}': str(out), '${stateId}': state.name}
    for old, new in replacements.items():
        source = source.replace(old, new)
    source = source.replace('\\$', '$')
    script = '''set -euo pipefail
source "$1/bin/lib/lock_utils.sh"
init_lock_helpers
STATE_TMP="$2/_state"
REPORT_HISTORY_LOCK="$STATE_TMP/.report_history.lock"
REPORT_HISTORY_JSONL="$STATE_TMP/report_history.jsonl"
ROUND_REPORT_JSON="$2/$3/round_report.json"
HISTORY_SNAPSHOT="$STATE_TMP/.report_history.snapshot.section8.$$.jsonl"
RUN_REPORT_DIR="$4/report_html/runs/$6"
RUN_REPORT_JSON="$RUN_REPORT_DIR/.gen.run_report.json"
RUN_REPORT_REL_PATH="runs/$6/report.html"
RUN_REPORT_PENDING="$RUN_REPORT_DIR/.report_render_pending"
RUN_HISTORY_PENDING="$RUN_REPORT_DIR/.report_history_pending"
RUN_INDEX_JSONL="$4/report_html/runs_index.jsonl"
RUN_INDEX_LOCK="$4/.runs_index.lock"
RENDER_REQUEST_FILE="$5"
HTML_REPORT_ENABLED=0
RTBIOSCAN_ROUND_LOCK_SCOPE=dorado_only
mkdir -p "$RUN_REPORT_DIR"
''' + source
    return subprocess.run([BASH, '-c', script, '_', str(ROOT), str(state), current, str(out), str(request), run],
                          capture_output=True, timeout=30, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})


def test_rendered_section8_waits_for_other_barcode_and_preserves_prior_outputs(tmp_path):
    state, hist, rows = fixture(tmp_path, 1)
    out = tmp_path / 'out'
    (state / '_state/run_started_utc.txt').write_text('2026-09-27T00:00:00Z\n')
    first = out_of_order_section8(state, out, 'r1', tmp_path / 'first.request')
    assert first.returncode == 0, first.stderr
    run_json = out / 'report_html/runs/run/run_report.json'
    index = out / 'report_html/runs_index.jsonl'
    old_json, old_index = run_json.read_bytes(), index.read_bytes()
    c = state / 'r2'; c.mkdir()
    c_row = rows[0].replace(b'"round_barcode": "r1"', b'"round_barcode": "r2"').replace(
        b'"barcode": "b"', b'"barcode": "c"')
    (c / 'round_report.json').write_bytes(c_row)
    (state / '_state/round_index.tsv').write_text('r1\t1\nr2\t2\n')
    request = tmp_path / 'second.request'
    result = out_of_order_section8(state, out, 'r1', request)
    assert result.returncode == 0, result.stderr
    assert request.read_text() == 'render=0\nround_barcode=r2\nrun_id=run\nbarcode=c\nfinalize=1\n'
    marker = out / 'report_html/runs/run/.report_history_pending'
    assert marker.exists() and run_json.read_bytes() == old_json and index.read_bytes() == old_index
    assert hist.read_bytes() == rows[0]
    command = [sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'), 'finalize',
               '--state-dir', str(state), '--current-round-barcode', 'r2', '--run-id', 'run',
               '--barcode', 'c', '--outdir', str(out), '--html', '1', '--lock-wait', '0']
    render_lock = state / '_state/.report_render.lock.lockdir'
    render_lock.mkdir()
    (render_lock / 'meta.env').write_text('pid=%s\nhost=%s\nstarted_epoch=%s\n' %
                                          (os.getpid(), subprocess.check_output(['hostname'], text=True).strip(), int(time.time())))
    try:
        blocked = subprocess.run(command, capture_output=True, timeout=20)
        assert blocked.returncode == 73 and b'Offline repair:' in blocked.stderr
        assert marker.exists() and run_json.read_bytes() == old_json and index.read_bytes() == old_index
    finally:
        (render_lock / 'meta.env').unlink()
        render_lock.rmdir()
    finished = subprocess.run(command, capture_output=True, timeout=40)
    assert finished.returncode == 0, finished.stderr
    assert hist.read_bytes() == rows[0] + c_row
    assert json.loads(run_json.read_text())['rounds_count'] == 2
    assert json.loads(run_json.read_text())['barcodes'] == ['b', 'c']
    assert json.loads(run_json.read_text())['barcode'] == 'c'
    entries = [json.loads(line) for line in index.read_bytes().split(b'\n') if line]
    assert len(entries) == 1 and entries[0]['rounds_count'] == 2
    assert '"barcode":"c"' in (out / 'report_html/runs/run/report.html').read_text()
    assert not marker.exists()
    healthy = out_of_order_section8(state, out, 'r1', tmp_path / 'healthy.request')
    assert healthy.returncode == 0, healthy.stderr
    assert (tmp_path / 'healthy.request').read_text() == 'render=0\nround_barcode=r1\n'
    assert hist.read_bytes() == rows[0] + c_row and not marker.exists()


@pytest.mark.parametrize('order', [('b', 'c'), ('c', 'b')])
def test_rendered_section8_complete_multibarcode_identity_ignores_invoker(tmp_path, order):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    second = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r2/round_report.json').write_bytes(second)
    hist.write_bytes(rows[0] + second)
    for member in order:
        current = 'r1' if member == 'b' else 'r2'
        request = tmp_path / f'{member}.request'
        result = out_of_order_section8(state, out, current, request, barcode=member)
        assert result.returncode == 0, result.stderr
        assert 'finalize=1' not in request.read_text()
        run = json.loads((out / 'report_html/runs/run/run_report.json').read_bytes())
        index = [json.loads(raw) for raw in (out / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if raw]
        assert len(index) == 1
        for obj in (run, index[0]):
            assert (obj['barcodes'], obj['barcode'], obj['last_round_barcode']) == (['b', 'c'], 'c', 'r2')
        assert not (out / 'report_html/runs/run/.report_history_pending').exists()


def test_out_of_order_dorado_section8_requests_terminal_and_rechecks_chain(tmp_path):
    state, hist, rows = fixture(tmp_path, 4)
    out = tmp_path / 'out'
    (state / '_state/run_started_utc.txt').write_text('2026-09-27T00:00:00Z\n')
    hist.write_bytes(rows[0] + rows[1])
    (state / 'r3/round_report.json').unlink()
    marker = out / 'report_html/runs/run/.report_history_pending'
    r4 = out_of_order_section8(state, out, 'r4', tmp_path / 'r4.request')
    assert r4.returncode == 0, r4.stderr
    assert marker.exists() and 'finalize=1' in (tmp_path / 'r4.request').read_text()
    assert finalizer(state, out, 'r4', html=0).returncode == 73
    assert marker.exists() and hist.read_bytes() == rows[0] + rows[1]
    (state / 'r3/round_report.json').write_bytes(rows[2])
    r3 = out_of_order_section8(state, out, 'r3', tmp_path / 'r3.request')
    assert r3.returncode == 0, r3.stderr
    assert (tmp_path / 'r3.request').read_text() == 'render=0\nround_barcode=r4\nfinalize=1\n'
    assert marker.exists() and hist.read_bytes() == b''.join(rows[:3])
    assert not (out / 'report_html/runs/run/run_report.json').exists()
    render_lock = state / '_state/.report_render.lock.lockdir'
    render_lock.mkdir()
    (render_lock / 'meta.env').write_text('pid=%s\nhost=%s\nstarted_epoch=%s\n' %
                                          (os.getpid(), subprocess.check_output(['hostname'], text=True).strip(), int(time.time())))
    try:
        failed = finalizer(state, out, 'r4')
        assert failed.returncode == 73 and marker.exists() and b'Offline repair:' in failed.stderr
        assert not (out / 'report_html/runs/run/run_report.json').exists()
    finally:
        (render_lock / 'meta.env').unlink()
        render_lock.rmdir()
    result = finalizer(state, out, 'r4')
    assert result.returncode == 0, result.stderr
    assert hist.read_bytes() == b''.join(rows)
    assert json.loads((out / 'report_html/runs/run/run_report.json').read_text())['rounds_count'] == 4
    index = [json.loads(row) for row in (out / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if row]
    assert len(index) == 1 and index[0]['rounds_count'] == 4
    assert '"round_barcode":"r4"' in (out / 'report_html/runs/run/report.html').read_text()
    assert not marker.exists()


def test_in_order_section8_does_not_request_finalizer(tmp_path):
    state, hist, rows = fixture(tmp_path, 3)
    out = tmp_path / 'out'
    (state / '_state/run_started_utc.txt').write_text('2026-09-27T00:00:00Z\n')
    result = out_of_order_section8(state, out, 'r3', tmp_path / 'r3.request')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'r3.request').read_text() == 'render=0\nround_barcode=r3\n'
    assert hist.read_bytes() == b''.join(rows)
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()


def test_rendered_section8_does_not_target_foreign_run(tmp_path):
    state, hist, rows = fixture(tmp_path, 3)
    out = tmp_path / 'out'
    foreign = rows[2].replace(b'"run_id": "run"', b'"run_id": "other"')
    (state / 'r3/round_report.json').write_bytes(foreign)
    hist.write_bytes(rows[0] + rows[1])
    (state / '_state/run_started_utc.txt').write_text('2026-09-27T00:00:00Z\n')
    request = tmp_path / 'r2.request'
    result = out_of_order_section8(state, out, 'r2', request)
    assert result.returncode == 0, result.stderr
    assert request.read_text() == 'render=0\nround_barcode=r2\n'
    assert hist.read_bytes() == rows[0] + rows[1]
    assert json.loads((out / 'report_html/runs/run/run_report.json').read_text())['rounds_count'] == 2
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()


@pytest.mark.parametrize('scope',['full_round','dorado_only'])
def test_section8_both_scopes_current_retry_and_gap(tmp_path,scope):
    state,hist,rows=fixture(tmp_path);out=tmp_path/'out'
    script='''set -euo pipefail
source "$1/bin/lib/lock_utils.sh"
init_lock_helpers
REPORT_HISTORY_LOCK="$2/_state/.report_history.lock"
REPORT_HISTORY_JSONL="$2/_state/report_history.jsonl"
ROUND_REPORT_JSON="$2/r3/round_report.json"
HISTORY_SNAPSHOT="$2/_state/test.snapshot"
RTBIOSCAN_ROUND_LOCK_SCOPE=$4
'''+section8_functions(state,out)+'''
rc=0
publish_report_history || rc=$?
printf '%s\\n' "$rc"
'''
    def run():
        return subprocess.run([BASH,'-c',script,'_',str(ROOT),str(state),str(out),scope],capture_output=True,timeout=10)
    result=run();assert result.returncode==0 and result.stdout==b'0\n',result.stderr
    assert hist.read_bytes()==b''.join(rows)
    hist.write_bytes(rows[0]+rows[2]);result=run()
    assert result.returncode==0,result.stderr
    if scope=='dorado_only':
        assert result.stdout.endswith(b'0\n') and hist.read_bytes()==b''.join(rows)
    else:
        # Full RF-PIN science remains the preflight normalizer; §8 requests pending finalization.
        assert result.stdout==b'4\n' and hist.read_bytes()==rows[0]+rows[2]


def test_section8_does_not_wait_again_after_worker_wait(tmp_path):
    state,hist,rows=fixture(tmp_path);ready=tmp_path/'ready';owner=hold_history(state,ready)
    try:
        assert wait_for(ready.exists)
        script='''source "$1/bin/lib/lock_utils.sh"
init_lock_helpers
REPORT_HISTORY_LOCK="$2/_state/.report_history.lock"
HISTORY_WAIT_USED=1
LOCK_WAIT=300
'''+section8_functions(state,tmp_path/'out')+'\nacquire_report_history_lock\n'
        start=time.monotonic()
        result=subprocess.run([BASH,'-c',script,'_',str(ROOT),str(state)],capture_output=True,timeout=3)
        assert result.returncode==2 and time.monotonic()-start<2
    finally:
        os.killpg(owner.pid,signal.SIGKILL);owner.wait(timeout=3)


def test_legacy_r4_adoption_happens_once_only_after_history_release(tmp_path,monkeypatch):
    import inspect
    import tests.test_r5_final_design_regressions as R
    import shutil
    base=tmp_path/'instrumented';shutil.copytree(ROOT/'bin',base/'bin');shutil.copytree(ROOT/'assets',base/'assets')
    log=tmp_path/'r4-locks'
    path=base/'bin/lib/RTBioScan/R4DCumulative.pm';source=path.read_text()
    anchor='sub acquire_lock {\n    my ($path) = @_;\n'
    probe="""    my $hpath=File::Basename::dirname($path)."/.report_history.lock.flock";
    if(-f $hpath){
        open my$hf,'<',$hpath or die $!;
        my$free=flock($hf,LOCK_EX|Fcntl::LOCK_NB());
        die "history leaf violation\\n" unless $free;
        close($hf);
        open my$log,'>>',LOG_PATH or die $!;print {$log} "adoption\\n";close($log);
    }
""".replace('LOG_PATH',repr(str(log)))
    assert source.count(anchor)==1;path.write_text(source.replace(anchor,anchor+probe))
    monkeypatch.setattr(R,'REPO',base);monkeypatch.setattr(R,'AUTH',base/'bin/state_snapshot_authority.pl');monkeypatch.setattr(R,'REPAIR',base/'bin/report_read_fate_repair.py')
    function=inspect.getsource(R.test_worker_verifies_reached_r4_and_resolved_run_status)
    function=function.replace("body=f'#RTB-R4D-CUMULATIVE\\t1", "body=f'#RTB-R4D-CUMULATIVE-BACKUP\\t1")
    namespace=R.__dict__.copy();exec(compile(function,'r4-leaf-probe','exec'),namespace)
    namespace['test_worker_verifies_reached_r4_and_resolved_run_status'](tmp_path/'case',monkeypatch,'committed')
    assert log.read_text()=='adoption\n'
    state=tmp_path/'case/s/_state'
    assert (state/'b_blast_otu_cumulative.commit').read_bytes().startswith(b'#RTB-R4D-CUMULATIVE\t1\n')


@pytest.mark.parametrize('cut',['after-release','history-revision','r4-change'])
def test_rfpin_unlocked_phase_refuses_interruption_or_revision_change(tmp_path,monkeypatch,cut):
    import shutil
    import tests.test_r5_final_design_regressions as R
    bindir=tmp_path/'fault/bin';shutil.copytree(ROOT/'bin',bindir)
    authority=bindir/'state_snapshot_authority.pl';source=authority.read_text()
    if cut=='after-release':
        anchor='    release_history(ctx)\n';extra="    die('injected after history release')\n"
    elif cut=='history-revision':
        anchor='        # The same primitive reacquires H only after every downstream holder exits.\n'
        extra="        with (state/'_state/report_history.jsonl').open('ab') as changed: changed.write(b'\\n')\n"
    else:
        anchor='        # The same primitive reacquires H only after every downstream holder exits.\n'
        extra="        (state/'_state/b_blast_otu_cumulative.commit').write_bytes(b'intervening R4 change')\n"
    assert source.count(anchor)==1;authority.write_text(source.replace(anchor,anchor+extra))
    monkeypatch.setattr(R,'AUTH',authority)
    if cut=='r4-change':
        with pytest.raises(AssertionError):R.test_worker_verifies_reached_r4_and_resolved_run_status(tmp_path/'case',monkeypatch,'committed')
        live=tmp_path/'case/s'
    else:
        live,helper,token,pin,rd=R.fresh_v4_fixture(tmp_path/'case')
        with pytest.raises(AssertionError):R.run_actual_worker(tmp_path/'case',live,helper,token,pin,'b','runA_0')
    assert (live/'_state/.read_fate_normalization_pending').exists()
    assert (tmp_path/'case/output/report_html/runs/runA/.report_history_pending').exists()
    assert not (tmp_path/'case/worker/success.receipt').exists()


def test_concurrent_late_repair_and_append_preserve_all_future_rows(tmp_path):
    state,hist,rows=fixture(tmp_path,5);hist.write_bytes(rows[0]+rows[3]+rows[4]);before=hist.read_bytes()
    ready=tmp_path/'held';owner=hold_history(state,ready);children=[]
    try:
        assert wait_for(ready.exists)
        inode=(state/'_state/.report_history.lock.flock').stat().st_ino
        for current in ('r3','r5'):
            children.append(subprocess.Popen([BASH,str(ROOT/'bin/report_history_append.sh'),str(state/current/'round_report.json'),
                str(hist),str(state/'_state/.report_history.lock')],env={**os.environ,'LOCK_WAIT':'5','PYTHONDONTWRITEBYTECODE':'1'},stdout=subprocess.PIPE,stderr=subprocess.PIPE))
        assert hist.read_bytes()==before
        os.killpg(owner.pid,signal.SIGKILL);owner.wait(timeout=3)
        for child in children:
            stdout,stderr=child.communicate(timeout=12)
            assert child.returncode==0,stderr
        assert hist.read_bytes()==b''.join(rows)
        assert (state/'_state/.report_history.lock.flock').stat().st_ino==inode
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.wait()
        if owner.poll() is None:os.killpg(owner.pid,signal.SIGKILL);owner.wait()


def test_rendered_section8_does_not_certify_stale_nonterminal(tmp_path):
    state, hist, rows = fixture(tmp_path, 2)
    out = tmp_path / 'out'
    (state / '_state/run_started_utc.txt').write_text('2026-09-27T00:00:00Z\n')
    healthy = out_of_order_section8(state, out, 'r2', tmp_path / 'healthy.request')
    assert healthy.returncode == 0, healthy.stderr
    assert (tmp_path / 'healthy.request').read_text() == 'render=0\nround_barcode=r2\n'
    public = out / 'report_html/runs/run/run_report.json'
    index = out / 'report_html/runs_index.jsonl'
    prior = public.read_bytes(), index.read_bytes()
    changed = rows[0].replace(b'"metric": 1', b'"metric": 9')
    (state / 'r1/round_report.json').write_bytes(changed)
    request = tmp_path / 'pending.request'
    result = out_of_order_section8(state, out, 'r2', request)
    assert result.returncode == 0, result.stderr  # science is nonfatal
    assert 'finalize=1' in request.read_text()
    assert (out / 'report_html/runs/run/.report_history_pending').exists()
    assert (public.read_bytes(), index.read_bytes()) == prior
    assert hist.read_bytes() == b''.join(rows)
    repaired = finalizer(state, out, 'r2', html=0)
    assert repaired.returncode == 0, repaired.stderr
    assert hist.read_bytes() == changed + rows[1]
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()
