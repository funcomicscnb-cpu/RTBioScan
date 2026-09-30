"""D4-F3 CLI contract: real helpers, manufactured authority, exact-base oracle.

All instrumentation and injected defects live in disposable scratch storage.
No preserved evidence or installed environment is required by these tests.
"""
import ast
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODE = Path(os.environ.get('RTB_COLLAPSE_CODE_ROOT', ROOT))
BASE_COMMIT = 'b4adf52866fab437fc52826a6011ec1569bf810c'
MARKERS = ('.report_history_pending', '.report_render_pending')

# The observer freezes only existing wall-clock fields and records real calls.
# It never replaces a lock, publisher, validator, or history implementation.
OBSERVER = r'''
import datetime, json, os, sys
real_datetime = datetime.datetime
class FixedDatetime(real_datetime):
    @classmethod
    def utcnow(cls): return cls(2026, 9, 30, 8, 0, 0)
datetime.datetime = FixedDatetime
busy = False
def audit(event, args):
    global busy
    if busy or event not in ('subprocess.Popen', 'os.remove', 'os.rename', 'open'): return
    busy = True
    try:
        out = os.environ.get('RTB_OBSERVE_OUT', '')
        directory = out + '/report_html/runs/run'
        markers = [os.path.exists(directory + '/' + n) for n in ('.report_history_pending', '.report_render_pending')]
        record = {'event': event, 'args': [str(a) for a in args[:2]], 'markers': markers}
        with open(os.environ['RTB_OBSERVE_LOG'], 'a') as stream: stream.write(json.dumps(record) + '\n')
        if event == 'subprocess.Popen' and os.environ.get('RTB_INJECT_FAIL') == 'publish':
            command = args[1]
            if isinstance(command, (list, tuple)) and 'publish-derived' in command:
                raise OSError('D4_F3_INJECTED_PUBLICATION_FAILURE')
    finally: busy = False
sys.addaudithook(audit)
'''


@pytest.fixture(scope='session')
def exact_base(tmp_path_factory):
    root = tmp_path_factory.mktemp('collapse-exact-base')
    for name in ('bin', 'assets'):
        shutil.copytree(ROOT / name, root / name)
    raw = subprocess.check_output(['git', '-C', str(ROOT), 'show', BASE_COMMIT + ':bin/report_history_state.py'])
    assert hashlib.sha256(raw).hexdigest() == '8f66ed8bf70e0a23f7a724488fadc2a814d31f7f46fb95e9f6de0861dffdc9cd'
    (root / 'bin/report_history_state.py').write_bytes(raw)
    return root


def fixture(root):
    state = root / 'state'; (state / '_state').mkdir(parents=True)
    rows = []
    for i in range(1, 4):
        directory = state / ('r' + str(i)); directory.mkdir()
        obj = dict(schema_version='2.1', state_id='state', run_id='run', barcode='b',
                   round_barcode=directory.name, metric=i, label='Åland 日本')
        raw = (json.dumps(obj, ensure_ascii=False) + '\n').encode()
        (directory / 'round_report.json').write_bytes(raw); rows.append(raw)
    (state / '_state/round_index.tsv').write_text('r1\t1\nr2\t2\nr3\t3\n')
    history = state / '_state/report_history.jsonl'; history.write_bytes(b''.join(rows))
    out = root / 'out'; mark(out)
    return state, out, history, b''.join(rows)


def mark(out):
    directory = out / 'report_html/runs/run'; directory.mkdir(parents=True, exist_ok=True)
    for name in MARKERS: (directory / name).write_text('original pending\n')


def pending(out):
    return tuple((out / 'report_html/runs/run' / name).exists() for name in MARKERS)


def environment(root, out, fail=''):
    tools = root / 'observer'; tools.mkdir(exist_ok=True)
    (tools / 'sitecustomize.py').write_text(OBSERVER)
    # Perl core-time override makes the existing volatile run clock reproducible.
    (tools / 'D4Clock.pm').write_text('package D4Clock; use strict; use warnings; BEGIN { *CORE::GLOBAL::gmtime = sub { CORE::gmtime(1790755200) }; } 1;\n')
    real_perl = shutil.which('perl')
    (tools / 'perl').write_text('#!/bin/bash\nprintf "%q " "$@" >> "$RTB_PERL_LOG"\nprintf "\\n" >> "$RTB_PERL_LOG"\nexec ' + shlex.quote(real_perl) + ' "$@"\n')
    (tools / 'perl').chmod(0o755)
    (tools / 'mktemp').write_text('#!/bin/bash\nif [ "$#" -eq 0 ]; then exec /usr/bin/mktemp "${TMPDIR:?}/tmp.XXXXXXXXXX"; fi\nexec /usr/bin/mktemp "$@"\n')
    (tools / 'mktemp').chmod(0o755)
    (root / 'tmp').mkdir(exist_ok=True)
    return {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(tools),
            'PATH': str(tools) + ':' + os.environ['PATH'], 'TMPDIR': str(root / 'tmp'),
            'PERL5OPT': '-MD4Clock', 'PERL5LIB': str(tools), 'PERL_HASH_SEED': '0', 'PERL_PERTURB_KEYS': '0', 'RTB_OBSERVE_LOG': str(root / 'events.jsonl'),
            'RTB_PERL_LOG': str(root / 'perl.log'), 'RTB_OBSERVE_OUT': str(out), 'RTB_INJECT_FAIL': fail,
            'MPLCONFIGDIR': str(root / 'mpl'), 'XDG_CACHE_HOME': str(root / 'cache')}


def command(code, state, out, mode='finalize', identity='collapse', extra=()):
    return [sys.executable, '-B', str(code / 'bin/report_history_state.py'), mode,
            '--state-dir', str(state), '--current-round-barcode', 'r3', '--run-id', 'run',
            '--barcode', 'b', '--outdir', str(out), '--lock-wait', '0', '--html', '1',
            '--identity-mode', identity, '--url-prefix', '', '--auto-refresh', '1',
            '--refresh-seconds', '15', '--sample-plot-max', '-1', *extra]


def invoke(root, code, state, out, mode='finalize', identity='collapse', extra=(), locked=False, fail=''):
    env = environment(root, out, fail)
    cmd = command(code, state, out, mode, identity, extra)
    if locked:
        history_command = runpy.run_path(str(code / 'bin/report_history_state.py'))['HISTORY_COMMAND']
        cmd = ['/bin/bash', '-c', history_command, 'collapse-probe', str(code / 'bin'), str(state), '0', *cmd]
    start = time.monotonic()
    result = subprocess.run(cmd, env=env, capture_output=True, timeout=45)
    log = os.environ.get('RTB_COLLAPSE_RESULT_LOG')
    if log:
        with open(log, 'a') as stream:
            stream.write(json.dumps(dict(command=cmd, rc=result.returncode, stdout=result.stdout.decode(),
                                         stderr=result.stderr.decode(), seconds=time.monotonic()-start,
                                         markers=pending(out))) + '\n')
    return result


def authoritative(out):
    return {str(p.relative_to(out)): p.read_bytes() for p in sorted(out.rglob('*'))
            if p.is_file() and not p.is_symlink() and not p.name.endswith('.flock')
            and p.name not in MARKERS}


def canonical(files, root):
    # Only the already-existing outdir field/link prefix varies across fixtures.
    values = (str(root).encode(), json.dumps(str(root), ensure_ascii=True)[1:-1].encode())
    return {name: data.replace(values[0], b'<SCRATCH>').replace(values[1], b'<SCRATCH>')
            for name, data in files.items()}


def oracle(state, out, expected, track=False):
    assert (state / '_state/report_history.jsonl').read_bytes() == expected
    rows = [json.loads(row) for row in expected.splitlines()]
    assert [row['round_barcode'] for row in rows] == ['r1', 'r2', 'r3']
    directory = out / 'report_html/runs/run'
    record = json.loads((directory / 'run_report.json').read_bytes())
    assert record['rounds_count'] == 3 and record['last_round_barcode'] == 'r3'
    index = [json.loads(row) for row in (out / 'report_html/runs_index.jsonl').read_bytes().splitlines()]
    assert len(index) == 1 and index[0]['run_id'] == 'run' and index[0]['rounds_count'] == 3
    for p in (directory / 'report.html', out / 'report_html/report.html'):
        assert b'window.REPORT_PAYLOAD' in p.read_bytes()
    assert (directory / 'report_replicates.html').exists() is track
    assert (directory / 'report_replicates_primers.html').exists() is track
    assert pending(out) == (False, False)
    leftovers = [p for root in (state, out) for p in root.rglob('*')
                 if '.tmp.' in p.name or '.snapshot.' in p.name or p.name.endswith('.lockdir')]
    assert leftovers == []
    for p in [*state.rglob('*.flock'), *out.rglob('*.flock')]:
        with p.open('rb') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def locks(root):
    result = []
    for line in (root / 'perl.log').read_text().splitlines():
        if 'fd_lock.pl lock ' not in line:
            continue
        args = shlex.split(line)
        if args and args[0].endswith('fd_lock.pl') and len(args) > 1 and args[1] == 'lock':
            result.append(args[5].replace(str(root), '<SCRATCH>'))
    return result


def lock_requests(root):
    targets = []
    for line in (root / 'events.jsonl').read_text().splitlines():
        event = json.loads(line)
        if event['event'] != 'subprocess.Popen':
            continue
        argv = ast.literal_eval(event['args'][1])
        if not isinstance(argv, (list, tuple)):
            continue
        if 'report-artifact' in argv:
            target = argv[argv.index('report-artifact') + 2]
        elif 'history-snapshot' in argv or 'history-recheck' in argv:
            label = 'history-snapshot' if 'history-snapshot' in argv else 'history-recheck'
            target = argv[argv.index(label) + 2] + '/_state/.report_history.lock'
        elif len(argv) > 6 and str(argv[1]).endswith('fd_lock.pl') and argv[2] == 'lock':
            target = argv[6]
        elif any(str(a).endswith('report_run_index_update.sh') for a in argv):
            pos = next(i for i, a in enumerate(argv) if str(a).endswith('report_run_index_update.sh'))
            target = argv[pos + 3]
        else:
            continue
        targets.append(str(target).replace(str(root), '<SCRATCH>'))
    return targets


@pytest.mark.parametrize('mode', ['finalize', 'recheck'])
def test_exact_base_parser_failure_before_body(tmp_path, exact_base, mode):
    state, out, hist, expected = fixture(tmp_path)
    r = invoke(tmp_path, exact_base, state, out, mode=mode, extra=['--snapshot', str(hist), '--revision', '0'*64, '--lock-fd', '999'])
    assert r.returncode == 64 and b"invalid choice: 'collapse'" in r.stderr
    assert b'Offline repair:' not in r.stderr and r.stdout == b''
    assert pending(out) == (True, True) and hist.read_bytes() == expected
    assert not (tmp_path / 'perl.log').exists()  # Parser never launches the lock helper.
    assert not list(state.rglob('*.flock'))


@pytest.mark.parametrize('identity', ['sample', 'track', 'collapse'])
def test_candidate_matches_exact_base_sample_or_track(tmp_path, exact_base, identity):
    reference = tmp_path / 'base'; candidate = tmp_path / 'candidate'
    state, out, _, expected = fixture(reference)
    r = invoke(reference, exact_base, state, out, identity='track' if identity == 'track' else 'sample')
    assert r.returncode == 0, r.stderr
    oracle(state, out, expected, identity == 'track')
    before = canonical(authoritative(out), reference)
    state, out, _, expected = fixture(candidate)
    r = invoke(candidate, CODE, state, out, identity=identity)
    assert r.returncode == 0, r.stderr
    oracle(state, out, expected, identity == 'track')
    assert canonical(authoritative(out), candidate) == before
    assert locks(candidate) == locks(reference)
    assert lock_requests(candidate) == lock_requests(reference)
    assert '<SCRATCH>/state/_state/.report_render.lock' in lock_requests(candidate)
    assert '<SCRATCH>/out/.report_root_render.lock' in lock_requests(candidate)
    assert '<SCRATCH>/out/.runs_index.lock' in lock_requests(candidate)
    assert locks(candidate) and locks(candidate)[0].endswith('/_state/.report_history.lock')


@pytest.mark.parametrize('identity', ['unknown', 'Collapse', '', 'samplex', 'trackx'])
@pytest.mark.parametrize('mode', ['finalize', 'recheck'])
def test_unknown_modes_remain_rejected(tmp_path, identity, mode):
    state, out, hist, expected = fixture(tmp_path)
    r = invoke(tmp_path, CODE, state, out, mode=mode, identity=identity,
               extra=['--snapshot', str(hist), '--revision', '0'*64])
    assert r.returncode == 64 and b'invalid choice' in r.stderr
    assert pending(out) == (True, True) and hist.read_bytes() == expected
    assert not (tmp_path / 'perl.log').exists()


@pytest.mark.parametrize('mode', ['finalize', 'recheck'])
def test_missing_arguments_and_help(tmp_path, mode):
    env = environment(tmp_path, tmp_path / 'out')
    cmd = [sys.executable, '-B', str(CODE / 'bin/report_history_state.py')]
    result = subprocess.run(cmd + [mode, '--identity-mode', 'collapse'], env=env, capture_output=True)
    assert result.returncode == 64 and b'required' in result.stderr
    result = subprocess.run(cmd + ['--help'], env=env, capture_output=True)
    assert result.returncode == 0 and b'--identity-mode {sample,collapse,track}' in result.stdout
    # Mode-specific requirements still reject before operational work.
    result = subprocess.run(cmd + [mode, '--state-dir', str(tmp_path), '--current-round-barcode', 'r3',
                                   '--identity-mode', 'collapse'], env=env, capture_output=True)
    assert result.returncode == 64 and b'is required for' in result.stderr


def test_recheck_success_failure_and_resume(tmp_path):
    state, out, hist, expected = fixture(tmp_path)
    r = invoke(tmp_path, CODE, state, out); assert r.returncode == 0, r.stderr
    snap = tmp_path / 'snapshot.jsonl'
    r = invoke(tmp_path, CODE, state, out, mode='snapshot', extra=['--snapshot', str(snap)], locked=True)
    assert r.returncode == 0, r.stderr
    token = r.stdout.decode().strip()
    Path(str(snap) + '.run.json').write_bytes((out / 'report_html/runs/run/run_report.json').read_bytes())
    before = authoritative(out)
    mark(out)
    r = invoke(tmp_path, CODE, state, out, mode='recheck', extra=['--snapshot', str(snap), '--revision', '0'*64], locked=True)
    assert r.returncode == 73 and pending(out) == (True, True)
    for damaged in (out / 'report_html/report.html', out / 'report_html/runs/run/run_report.json'):
        original = damaged.read_bytes()
        damaged.write_bytes(b'{}\n')
        r = invoke(tmp_path, CODE, state, out, mode='recheck',
                   extra=['--snapshot', str(snap), '--revision', token], locked=True)
        assert r.returncode == 73 and pending(out) == (True, True)
        damaged.write_bytes(original)
    r = invoke(tmp_path, CODE, state, out, mode='recheck', extra=['--snapshot', str(snap), '--revision', token], locked=True)
    assert r.returncode == 0 and pending(out) == (False, False), r.stderr
    assert authoritative(out) == before and hist.read_bytes() == expected
    # The existing cache-false render can re-execute this successful recheck.
    r = invoke(tmp_path, CODE, state, out, mode='recheck', extra=['--snapshot', str(snap), '--revision', token], locked=True)
    assert r.returncode == 0 and authoritative(out) == before


@pytest.mark.parametrize('failure', ['publish', 'authority', 'history-lock', 'render-lock'])
def test_failure_then_retry_idempotent(tmp_path, failure):
    state, out, hist, expected = fixture(tmp_path)
    if failure in ('history-lock', 'render-lock'):
        r = invoke(tmp_path, CODE, state, out); assert r.returncode == 0, r.stderr
        mark(out)
    blocker = None
    report = state / 'r2/round_report.json'; original = report.read_bytes()
    if failure == 'authority': report.unlink()
    render_lock = state / '_state/.report_render.lock.lockdir'
    if failure == 'history-lock':
        blocker = (state / '_state/.report_history.lock.flock').open('rb')
        fcntl.flock(blocker, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if failure == 'render-lock':
        render_lock.mkdir()
        (render_lock / 'meta.env').write_text(f'pid={os.getpid()}\nhost={os.uname().nodename}\nstarted_epoch={int(time.time())}\n')
    before = authoritative(out)
    try:
        r = invoke(tmp_path, CODE, state, out, fail='publish' if failure == 'publish' else '')
        assert r.returncode == 73 and pending(out) == (True, True), r.stderr
        assert b'Offline repair:' in r.stderr and hist.read_bytes() == expected
        assert authoritative(out) == before  # failed attempt did not publish partial authority
    finally:
        if blocker: blocker.close()
        if failure == 'render-lock': shutil.rmtree(render_lock)
        if failure == 'authority': report.write_bytes(original)
    r = invoke(tmp_path, CODE, state, out); assert r.returncode == 0, r.stderr
    oracle(state, out, expected)
    complete = authoritative(out)
    r = invoke(tmp_path, CODE, state, out); assert r.returncode == 0, r.stderr
    oracle(state, out, expected); assert authoritative(out) == complete
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    removals = [e for e in events if e['event'] == 'os.remove' and any(n in e['args'][0] for n in MARKERS)]
    assert len(removals) >= 4
    # The injected publication call sees both markers, before it fails.
    if failure == 'publish':
        publications = [e for e in events if e['event'] == 'subprocess.Popen' and 'publish-derived' in e['args'][1]]
        assert publications and publications[0]['markers'] == [True, True]


def test_marker_order_at_real_publication(tmp_path):
    state, out, _, expected = fixture(tmp_path)
    r = invoke(tmp_path, CODE, state, out); assert r.returncode == 0, r.stderr
    oracle(state, out, expected)
    events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text().splitlines()]
    publication = [i for i, e in enumerate(events) if e['event'] == 'subprocess.Popen' and
                   ('publish-derived' in e['args'][1] or 'report_rebuild.sh' in e['args'][1])]
    removals = [i for i, e in enumerate(events) if e['event'] == 'os.remove' and any(n in e['args'][0] for n in MARKERS)]
    assert publication and removals and min(removals) > max(publication)
    assert all(events[i]['markers'] == [True, True] for i in publication)


@pytest.mark.parametrize('name', ['with spaces', 'Å 日本', 'quote\'"', '-leading', 'shell;$()`&'])
def test_paths_preserve_argument_boundaries(tmp_path, exact_base, name):
    root = tmp_path / name
    state, out, _, expected = fixture(root)
    r = invoke(root, CODE, state, out); assert r.returncode == 0, r.stderr
    oracle(state, out, expected)
    before = authoritative(out)
    # Reuse the identical argument vector against base: also pins any existing
    # path encoding behavior without expanding this fix into Perl serialization.
    r = invoke(root, exact_base, state, out, identity='sample')
    assert r.returncode == 0, r.stderr
    oracle(state, out, expected)
    assert authoritative(out) == before


def test_production_vocabulary_and_both_argument_shapes():
    text = (ROOT / 'main.nf').read_text()
    canonicalizer = text.split('String getReplicateModeCanonical(', 1)[1].split('\n}', 1)[0]
    assert "replicateModeCanonical in ['collapse', 'track']" in canonicalizer
    finalizer = text.split('finalize_pending_report() {', 1)[1].split('\n            }', 1)[0]
    assert '--identity-mode "${replicateModeCanonical}"' in finalizer
    recheck = next(line for line in text.splitlines() if 'report_history_state.py" recheck' in line and '--identity-mode' in line)
    assert '--identity-mode "\\$REPORT_IDENTITY_MODE"' in recheck
    assert 'REPORT_IDENTITY_MODE="${replicateModeCanonical}"' in text
    render = text.split('process async_report_render', 1)[1].split('script:', 1)[0]
    assert re.search(r'cache\s+false', render)


@pytest.mark.parametrize('mode', ['finalize', 'recheck', 'snapshot', 'publish-derived'])
def test_alias_is_canonical_before_dispatch(monkeypatch, tmp_path, mode):
    api = runpy.run_path(str(CODE / 'bin/report_history_state.py'))
    main = api['main']; observed = []
    def operation(args):
        observed.append(args.identity_mode)
        return True if mode == 'recheck' else 0
    monkeypatch.setitem(main.__globals__, mode.replace('-', '_'), operation)
    argv = command(CODE, tmp_path, tmp_path, mode=mode,
                   extra=['--snapshot', str(tmp_path / 'snapshot'), '--revision', '0'*64])
    monkeypatch.setattr(sys, 'argv', argv[2:])
    assert main() == 0
    assert observed == ['sample']


@pytest.mark.parametrize('path', ['recheck', 'finalize'])
def test_actual_production_callsite_fragments(tmp_path, path):
    state, out, _, expected = fixture(tmp_path)
    text = (ROOT / 'main.nf').read_text()
    replacements = {
        '${baseDir}': str(CODE), '${ongoingStateDir}': str(state),
        '${outdirResolved}': str(out), '${round_barcode}': 'r3',
        '${run_name}': 'run', '${barcode}': 'b',
        '${replicateModeCanonical}': 'collapse', '${htmlReportEnabled ? 1 : 0}': '1',
        '${htmlReportUrlPrefix}': '', '${htmlReportAutoRefresh ? 1 : 0}': '1',
        '${htmlReportRefreshSecondsStr}': '15', '${htmlReportSamplePlotMaxStr}': '-1',
    }
    if path == 'finalize':
        fragment = 'finalize_pending_report() {' + text.split('finalize_pending_report() {', 1)[1].split('\n            }', 1)[0] + '\n}'
        shell = 'set -euo pipefail\nREQUEST_FINALIZE=1; REQUEST_ROUND=r3; REQUEST_RUN=run; REQUEST_BARCODE=b\n' + fragment + '\nfinalize_pending_report\n'
    else:
        r = invoke(tmp_path, CODE, state, out); assert r.returncode == 0, r.stderr
        snap = tmp_path / 'snapshot.jsonl'
        r = invoke(tmp_path, CODE, state, out, mode='snapshot', extra=['--snapshot', str(snap)], locked=True)
        assert r.returncode == 0, r.stderr
        token = r.stdout.decode().strip()
        Path(str(snap) + '.run.json').write_bytes((out / 'report_html/runs/run/run_report.json').read_bytes())
        mark(out)
        fragment = next(line for line in text.splitlines() if 'report_history_state.py" recheck' in line and '--identity-mode' in line)
        shell = ('set -euo pipefail\nsource ' + shlex.quote(str(CODE / 'bin/lib/lock_utils.sh')) + '\ninit_lock_helpers\n'
                 + 'LOCK_WAIT=0 acquire_lock ' + shlex.quote(str(state / '_state/.report_history.lock')) + '\n'
                 + 'export RTB_HISTORY_LOCK_OWNER=$$\nfinal_check_rc=0\nREPORT_IDENTITY_MODE=collapse\n'
                 + 'SNAPSHOT_PATH=' + shlex.quote(str(snap)) + '\nsnapshot_revision=' + shlex.quote(token) + '\n'
                 + fragment + '\nexit "$final_check_rc"\n')
    for key, value in replacements.items(): shell = shell.replace(key, value)
    shell = shell.replace(r'\$', '$')
    script = tmp_path / 'production-callsite.sh'; script.write_text(shell)
    result = subprocess.run(['/bin/bash', str(script)], env=environment(tmp_path, out), capture_output=True, timeout=45)
    assert result.returncode == 0, result.stderr
    oracle(state, out, expected)
