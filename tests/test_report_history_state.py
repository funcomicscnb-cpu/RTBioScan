import builtins
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('report_history_state', ROOT / 'bin/report_history_state.py')
H = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(H)


def fixture(tmp_path, count=3):
    state = tmp_path / 'state'; (state / '_state').mkdir(parents=True)
    rows = []
    for i in range(1, count + 1):
        rd = state / f'r{i}'; rd.mkdir()
        obj = dict(schema_version='2.1', run_id='run', barcode='b', state_id='state', round_barcode=rd.name,
                   label='old Ã\u0085land L\u2028S P\u2029S', metric=i)
        raw = (json.dumps(obj, ensure_ascii=False) + '\n').encode()
        (rd / 'round_report.json').write_bytes(raw); rows.append(raw)
    (state / '_state/round_index.tsv').write_text(''.join(f'r{i}\t{i}\n' for i in range(1, count + 1)))
    hist = state / '_state/report_history.jsonl';hist.write_bytes(b''.join(rows))
    return state, hist, rows


def view(state, current='r3', full=False):
    v = H.History(state, current)
    return v.classify(full=full), v


@pytest.mark.parametrize('wire,expected', [('all','complete'),('empty','normalize'),('tail','append'),('middle','normalize'),('duplicate','normalize'),('conflict','invalid_authority'),('order','normalize'),('blank','complete'),('trailing','blocked_malformed'),('partial','blocked_malformed'),('malformed-middle','blocked_malformed')])
def test_classifier_cases(tmp_path, wire, expected):
    state, hist, rows = fixture(tmp_path)
    wires = {'all':b''.join(rows),'empty':b'','tail':b''.join(rows[:2]),'middle':rows[0]+rows[2], 'duplicate':b''.join(rows)+rows[1],
             'conflict':b''.join(rows)+rows[1].replace(b'"metric": 2', b'"metric": 9'),'order':b''.join(reversed(rows)),
             'blank':b''.join(rows)+b'\n','trailing':b''.join(rows)+b'{bad\n','partial':b''.join(rows).rstrip(b'\n'),
             'malformed-middle':rows[0]+b'\xffbad\n'+rows[2]}
    hist.write_bytes(wires[wire]); before=hist.read_bytes()
    assert view(state)[0] == expected
    assert hist.read_bytes() == before


def test_first_round_missing_and_marker_is_advisory(tmp_path):
    state, hist, rows=fixture(tmp_path,1);hist.unlink()
    assert view(state,'r1')[0]=='append'
    (state/'_state/.report_history_pending').write_text('complete')
    assert view(state,'r1')[0]=='append'
    hist.write_bytes(rows[0]);assert view(state,'r1')[0]=='complete'


@pytest.mark.parametrize('change',['index','report','identity','schema','future','future-missing'])
def test_invalid_authority(tmp_path,change):
    state,hist,rows=fixture(tmp_path)
    current='r3'
    if change=='index':(state/'_state/round_index.tsv').unlink()
    elif change=='report':(state/'r1/round_report.json').unlink()
    elif change=='identity':(state/'r3/round_report.json').write_bytes(rows[2].replace(b'"state"',b'"wrong"'))
    elif change=='schema':hist.write_bytes(b''.join(rows).replace(b'"2.1"',b'"99.0"'))
    elif change=='future':current='r1';hist.write_bytes(b''.join(rows).replace(b'"metric": 3',b'"metric": 9'))
    else:current='r1';(state/'r3/round_report.json').unlink()
    assert view(state,current)[0]=='invalid_authority'


@pytest.mark.parametrize('a,b,same', [('1','1.0',True),('1','1e0',True),('-0.0','0',True),('1e999','10e998',True),('9007199254740993','9007199254740992',False),('true','1',False),('"1"','1',False),('[1,2]','[2,1]',False),('{"a":1,"b":2}','{"b":2.0,"a":1e0}',True),('"é"','"é"',False)])
def test_exact_numbers_and_types(a,b,same):
    assert H.equivalent(H.loads(a),H.loads(b)) is same


@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity','{"a":1,"a":2}'])
def test_invalid_json_numbers_and_duplicate_keys(value):
    with pytest.raises(ValueError):H.loads(value)


def test_numeric_equal_history_does_not_rewrite(tmp_path):
    state,hist,rows=fixture(tmp_path)
    hist.write_bytes(b''.join(rows).replace(b'"metric": 3',b'"metric": 3e0'))
    assert view(state)[0]=='complete'


def test_healthy_report_open_count(tmp_path,monkeypatch):
    state,hist,rows=fixture(tmp_path,1000);opened=[]
    original=Path.open
    def counted(path,*args,**kwargs):
        if path.name=='round_report.json':opened.append(path)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',counted)
    status,v=view(state,'r1000');assert status=='complete';assert len(opened)==v.reports_opened==1
    opened.clear()
    assert H.terminal_round(state, state/'_state/round_index.tsv', 'r1000', 'run', 'b')=='r1000'
    assert len(opened)==1
    opened.clear();hist.write_bytes(b''.join(rows[:-1]))
    status,v=view(state,'r1000');assert status=='append';assert len(opened)==v.reports_opened==2
    assert not v.full_scan


def test_run_wide_target_uses_run_and_index_order(tmp_path):
    state, hist, rows = fixture(tmp_path, 1)
    specs = [('z-last', 2, 'run', 'c'), ('a-first', 3, 'other', 'b'),
             ('middle', 4, 'run', 'd'), ('foreign', 5, 'other', 'c')]
    for rb, _, run, barcode in reversed(specs):
        directory = state / rb; directory.mkdir()
        obj = dict(schema_version='2.1', state_id='state', run_id=run,
                   barcode=barcode, round_barcode=rb)
        (directory / 'round_report.json').write_text(json.dumps(obj) + '\n')
    (state / '_state/round_index.tsv').write_text('r1\t1\n' + ''.join(
        f'{rb}\t{number}\n' for rb, number, _, _ in specs))
    index = state / '_state/round_index.tsv'
    assert H.run_repair_target(state, index, 'r1', 'run', 'b') == ('run', 'c', 'z-last')
    terminals, pending = H.run_completion(state, index, 'r1', 'run', 'b')
    assert terminals == {'b': 'r1', 'c': 'z-last', 'd': 'middle'}
    assert [(entry[2], entry[3]) for entry in pending] == [('c', 'z-last'), ('d', 'middle')]
    assert H.run_repair_target(state, index, 'a-first', 'other', 'b') == ('other', 'b', 'a-first')
    assert hist.read_bytes() == rows[0]


def test_run_wide_inventory_streams_retained_reports(tmp_path, monkeypatch):
    state, _, _ = fixture(tmp_path, 1000)
    sizes = []
    original = H.History.report
    def observed(self, rb):
        result = original(self, rb)
        sizes.append(len(self.cache))
        return result
    monkeypatch.setattr(H.History, 'report', observed)
    terminals, _ = H.run_terminals(state, state / '_state/round_index.tsv', 'r1000', 'run', 'b')
    assert terminals == {'b': 'r1000'}
    # One extra cached lookup validates the invoking round before the ordered scan.
    assert len(sizes) == 1001 and max(sizes) <= 2


def test_option_a_roster_uses_run_and_authoritative_order(tmp_path):
    state = tmp_path / 'state'; (state / '_state').mkdir(parents=True)
    entries = [('zeta', 'runA', 'b'), ('foreign', 'runB', 'c'),
               ('alpha', 'runA', 'c'), ('middle', 'runA', 'b'), ('last', 'runB', 'b')]
    for rb, run, barcode in reversed(entries):
        directory = state / rb; directory.mkdir()
        (directory / 'round_report.json').write_text(json.dumps(dict(
            schema_version='2.1', state_id='state', run_id=run,
            barcode=barcode, round_barcode=rb)) + '\n')
    (state / '_state/round_index.tsv').write_text(''.join(
        f'{rb}\t{position}\n' for position, (rb, _, _) in enumerate(entries, 1)))
    (state / '_state/report_history.jsonl').write_bytes(b''.join(
        (state / rb / 'round_report.json').read_bytes() for rb, _, _ in entries))
    a = H.context_for_run(state, 'runA', require_complete=True)
    b = H.context_for_run(state, 'runB', require_complete=True)
    assert (a['barcodes'], a['barcode'], a['last_round_barcode']) == (['b', 'c'], 'b', 'middle')
    assert a['rounds_count'] == 3
    assert a['round_order_sha256'] == H.round_order_digest(
        [('zeta', 'b'), ('alpha', 'c'), ('middle', 'b')])
    assert (b['barcodes'], b['barcode'], b['last_round_barcode']) == (['c', 'b'], 'b', 'last')
    (state / 'middle/round_report.json').unlink()
    assert H.context_for_run(state, 'runA')['last_round_barcode'] == 'alpha'
    (state / '_state/round_index.tsv').write_text('zeta\t1\nforeign\t2\nalpha\t2\n')
    with pytest.raises(H.Refusal, match='duplicate_or_unsafe_round_index'):
        H.context_for_run(state, 'runA')


@pytest.mark.parametrize('invoker', ['b', 'c'])
def test_option_a_full_finalizer_is_invocation_independent(tmp_path, invoker):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    second = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r2/round_report.json').write_bytes(second)
    hist.write_bytes(rows[0] + second)
    current = 'r1' if invoker == 'b' else 'r2'
    result = finalizer(state, out, current) if invoker == 'b' else subprocess.run(
        [sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'), 'finalize',
         '--state-dir', str(state), '--current-round-barcode', current, '--outdir', str(out),
         '--run-id', 'run', '--barcode', 'c', '--html', '1', '--lock-wait', '0'],
        capture_output=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=30)
    assert result.returncode == 0, result.stderr
    run = json.loads((out / 'report_html/runs/run/run_report.json').read_bytes())
    index = [json.loads(raw) for raw in (out / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if raw]
    assert len(index) == 1
    for obj in (run, index[0]):
        assert (obj['run_id'], obj['barcodes'], obj['barcode'], obj['last_round_barcode'],
                obj['run_summary_source_round']) == ('run', ['b', 'c'], 'c', 'r2', 'r2')
    html = (out / 'report_html/runs/run/report.html').read_text()
    assert html.index('"round_barcode":"r1"') < html.index('"round_barcode":"r2"')
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()


def test_option_a_rebuild_uses_same_roster_and_preserves_prior_on_pending(tmp_path):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    second = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r2/round_report.json').write_bytes(second)
    hist.write_bytes(rows[0] + second)
    command = ['/bin/bash', str(ROOT / 'bin/report_rebuild.sh'), '--outdir', str(out),
               '--history', str(hist), '--state-id', 'state', '--run-id', 'run', '--skip-root-report']
    first = subprocess.run(command, capture_output=True, timeout=30)
    assert first.returncode == 0, first.stderr
    run = out / 'report_html/runs/run/run_report.json'
    index = out / 'report_html/runs_index.jsonl'
    html = out / 'report_html/runs/run/report.html'
    assert json.loads(run.read_bytes())['barcodes'] == ['b', 'c']
    before = tuple(path.read_bytes() for path in (run, index, html))
    hist.write_bytes(rows[0])
    pending = subprocess.run(command, capture_output=True, timeout=30)
    assert pending.returncode != 0 and b'run_incomplete' in pending.stderr
    assert tuple(path.read_bytes() for path in (run, index, html)) == before


def test_option_a_nonnumeric_finalizer_and_html_share_index_order(tmp_path):
    state = tmp_path / 'state'; (state / '_state').mkdir(parents=True)
    rows = []
    for rb, barcode in [('zeta', 'b'), ('alpha', 'c')]:
        directory = state / rb; directory.mkdir()
        raw = (json.dumps(dict(schema_version='2.1', state_id='state', run_id='run',
                               barcode=barcode, round_barcode=rb)) + '\n').encode()
        (directory / 'round_report.json').write_bytes(raw)
        rows.append(raw)
    (state / '_state/round_index.tsv').write_text('zeta\t1\nalpha\t2\n')
    (state / '_state/report_history.jsonl').write_bytes(b''.join(rows))
    out = tmp_path / 'out'
    command = [sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'), 'finalize',
               '--state-dir', str(state), '--current-round-barcode', 'zeta', '--run-id', 'run',
               '--barcode', 'b', '--outdir', str(out), '--lock-wait', '0']
    result = subprocess.run(command, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    run = json.loads((out / 'report_html/runs/run/run_report.json').read_bytes())
    assert (run['barcodes'], run['barcode'], run['last_round_barcode']) == (['b', 'c'], 'c', 'alpha')
    html = (out / 'report_html/runs/run/report.html').read_text()
    assert html.index('"round_barcode":"zeta"') < html.index('"round_barcode":"alpha"')
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()


def test_option_a_numeric_r2_r10_uses_index_terminal(tmp_path):
    state, hist, rows = fixture(tmp_path, 10)
    last = rows[-1].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r10/round_report.json').write_bytes(last)
    hist.write_bytes(b''.join(rows[:-1]) + last)
    context = H.context_for_run(state, 'run', require_complete=True)
    assert (context['barcodes'], context['barcode'], context['last_round_barcode'],
            context['rounds_count']) == (['b', 'c'], 'c', 'r10', 10)
    result = finalizer(state, tmp_path / 'out', 'r2', html=0)
    assert result.returncode == 0, result.stderr
    obj = json.loads((tmp_path / 'out/report_html/runs/run/run_report.json').read_bytes())
    assert (obj['barcodes'], obj['barcode'], obj['last_round_barcode']) == (['b', 'c'], 'c', 'r10')


def test_option_a_rfpin_captured_run_context_uses_terminal_identity(tmp_path):
    state, hist, rows = fixture(tmp_path, 2)
    second = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r2/round_report.json').write_bytes(second)
    hist.write_bytes(rows[0] + second)
    destination = tmp_path / 'private-run.json'
    command = ['perl', str(ROOT / 'bin/report_run_json.pl'), '--history', str(hist),
               '--out', str(destination), '--run-id', 'run', '--barcode', 'b', '--state-id', 'state']
    captured = H.capture_run_context(command, state, 'r1', destination)
    assert '--authority-context' in captured
    result = subprocess.run(captured, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    obj = json.loads(destination.read_bytes())
    assert (obj['schema_version'], obj['barcodes'], obj['barcode'], obj['last_round_barcode']) == (
        '2.0', ['b', 'c'], 'c', 'r2')
    hist.write_bytes(rows[0])
    second_destination = tmp_path / 'private-pending.json'
    pending_command = command.copy(); pending_command[pending_command.index(str(destination))] = str(second_destination)
    private = H.capture_run_context(pending_command, state, 'r1', second_destination)
    assert '--authority-context' not in private
    assert subprocess.run(private, capture_output=True, timeout=30).returncode == 0
    # Another barcode can finish after RF-PIN captures this private witness.
    # The old single-barcode source must never replace the now-complete run.
    hist.write_bytes(rows[0] + second)
    snapshot = tmp_path / 'snapshot.jsonl'; snapshot.write_bytes(hist.read_bytes())
    context = H.context_for_run(state, 'run', require_complete=True)
    context['report_revision'] = H.revision(H.History(state, 'r1', run_id='run', barcode='b'), context)
    Path(str(snapshot) + '.authority.json').write_text(json.dumps(context))
    out = tmp_path / 'out'; published = out / 'report_html/runs/run/run_report.json'
    published.parent.mkdir(parents=True); published.write_bytes(b'prior public bytes\n')
    from types import SimpleNamespace
    with pytest.raises(H.Refusal, match='source_run_authority_mismatch'):
        H.publish_derived(SimpleNamespace(snapshot=str(snapshot), source=str(second_destination),
                                          outdir=str(out), run_id='run', guard_revision=context['report_revision']))
    assert published.read_bytes() == b'prior public bytes\n'


@pytest.mark.parametrize('change', ['metric', 'same_size_mtime', 'barcode', 'identity',
                                    'index_order', 'index_position', 'insert', 'remove'])
def test_full_authority_revision_binds_nonterminal_reports(tmp_path, change):
    state, hist, rows = fixture(tmp_path, 2)
    baseline = H.revision(H.History(state, 'r2', run_id='run', barcode='b'))
    authority_before = H.context_for_run(state, 'run')['authority_revision']
    r1 = state / 'r1/round_report.json'
    if change in ('metric', 'same_size_mtime'):
        previous = r1.stat()
        raw = r1.read_bytes().replace(b'"metric": 1', b'"metric": 9')
        assert len(raw) == previous.st_size
        r1.write_bytes(raw)
        if change == 'same_size_mtime':
            os.utime(r1, ns=(previous.st_atime_ns, previous.st_mtime_ns))
            assert r1.stat().st_mtime_ns == previous.st_mtime_ns
    elif change == 'barcode':
        r1.write_bytes(rows[0].replace(b'"barcode": "b"', b'"barcode": "c"'))
    elif change == 'identity':
        r1.write_bytes(rows[0].replace(b'"round_barcode": "r1"', b'"round_barcode": "other"'))
    elif change == 'index_order':
        (state / '_state/round_index.tsv').write_text('r1\t2\nr2\t1\n')
    elif change == 'index_position':
        (state / '_state/round_index.tsv').write_text('r1\t9\nr2\t10\n')
    elif change == 'insert':
        rd = state / 'middle'; rd.mkdir()
        (rd / 'round_report.json').write_bytes(rows[0].replace(b'"round_barcode": "r1"', b'"round_barcode": "middle"'))
        (state / '_state/round_index.tsv').write_text('r1\t1\nmiddle\t2\nr2\t3\n')
    else:
        r1.unlink()
    try:
        changed = H.revision(H.History(state, 'r2', run_id='run', barcode='b'))
    except H.Refusal:
        assert change == 'identity'
    else:
        assert changed != baseline
        if change in ('index_order', 'index_position'):
            assert H.context_for_run(state, 'run')['authority_revision'] != authority_before


def test_full_authority_revision_excludes_foreign_report_and_is_order_stable(tmp_path):
    state, hist, rows = fixture(tmp_path, 2)
    foreign = state / 'foreign'; foreign.mkdir()
    raw = rows[0].replace(b'"round_barcode": "r1"', b'"round_barcode": "foreign"').replace(
        b'"run_id": "run"', b'"run_id": "other"')
    (foreign / 'round_report.json').write_bytes(raw)
    (state / '_state/round_index.tsv').write_text('r1\t1\nforeign\t2\nr2\t3\n')
    token = H.revision(H.History(state, 'r2', run_id='run', barcode='b'))
    (foreign / 'round_report.json').write_bytes(raw.replace(b'"metric": 1', b'"metric": 9'))
    assert H.revision(H.History(state, 'r2', run_id='run', barcode='b')) == token
    # Discovery order is irrelevant: a second state root has the same bytes,
    # with retained reports recreated in reverse order.
    import shutil
    other = tmp_path / 'other/state'
    shutil.copytree(state, other)
    copies = {p.parent.name: p.read_bytes() for p in other.glob('*/round_report.json')}
    for p in other.glob('*/round_report.json'):
        p.unlink()
    for name in reversed(list(copies)):
        (other / name / 'round_report.json').write_bytes(copies[name])
    assert H.revision(H.History(other, 'r2', run_id='run', barcode='b')) == token


def test_full_authority_framing_handles_unicode_and_delimiters(tmp_path):
    state = tmp_path / 'state'; (state / '_state').mkdir(parents=True)
    names = ['x:y\u0085', 'zeta\u2028x', 'alpha\u2029y']
    for name in reversed(names):
        rd = state / name; rd.mkdir()
        (rd / 'round_report.json').write_bytes((json.dumps(dict(schema_version='2.1',
            run_id='a:b', state_id='state', barcode='b', round_barcode=name), ensure_ascii=False) + '\n').encode())
    (state / '_state/round_index.tsv').write_bytes(''.join(
        f'{name}\t{i}\n' for i, name in enumerate(names, 1)).encode())
    (state / '_state/report_history.jsonl').write_bytes(b''.join(
        (state / name / 'round_report.json').read_bytes() for name in names))
    context = H.context_for_run(state, 'a:b', require_complete=True)
    assert context['last_round_barcode'] == names[-1]
    assert len(context['authority_revision']) == 64
    import hashlib
    left = hashlib.sha256(); right = hashlib.sha256()
    for item in ('a:b', 'c'):
        H.digest_frame(left, item)
    for item in ('a', 'b:c'):
        H.digest_frame(right, item)
    assert left.digest() != right.digest()


def test_full_authority_digest_reuses_each_opened_report(tmp_path, monkeypatch):
    state, _, _ = fixture(tmp_path, 10)
    opened = []
    original = Path.read_bytes
    def counted(path):
        if path.name == 'round_report.json':
            opened.append(path)
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', counted)
    terminals, _, context = H.run_terminals(state, state / '_state/round_index.tsv', 'r10',
                                              'run', 'b', include_context=True)
    view = H.History(state, 'r10', run_id='run', barcode='b')
    H.revision(view, context)
    assert terminals == {'b': 'r10'} and len(opened) == 10


def test_nonterminal_retained_change_keeps_pending_at_real_recheck(tmp_path):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    assert finalizer(state, out, 'r2').returncode == 0
    snapshot = tmp_path / 'snapshot.jsonl'; snapshot.write_bytes(hist.read_bytes())
    public = out / 'report_html/runs/run/run_report.json'
    Path(str(snapshot) + '.run.json').write_bytes(public.read_bytes())
    token = H.revision(H.History(state, 'r2', run_id='run', barcode='b'))
    r1 = state / 'r1/round_report.json'; previous = r1.stat()
    r1.write_bytes(rows[0].replace(b'"metric": 1', b'"metric": 9'))
    os.utime(r1, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    marker = out / 'report_html/runs/run/.report_history_pending'; marker.write_text('pending\n')
    command = ['/bin/bash', '-c', H.HISTORY_COMMAND, 'history-recheck', str(ROOT / 'bin'),
               str(state), '0', sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'),
               'recheck', '--state-dir', str(state), '--current-round-barcode', 'r2',
               '--outdir', str(out), '--run-id', 'run', '--barcode', 'b', '--snapshot', str(snapshot),
               '--revision', token, '--html', '1']
    assert subprocess.run(command, capture_output=True, timeout=30).returncode == 73
    assert marker.exists()


def call_private_guard(state, out, snapshot, source, current='r1', barcode='b'):
    code = '''import runpy,sys
api=runpy.run_path(sys.argv[1])
state,current,run,barcode,out,snapshot,source=sys.argv[2:]
command=[sys.executable,'-B',sys.argv[1],'publish-derived','--state-dir',state,
 '--current-round-barcode',current,'--outdir',out,'--run-id',run,'--barcode',barcode,
 '--snapshot',snapshot,'--source',source,'--html','1','--identity-mode','sample','--lock-wait','0']
try:
 api['publish_private_source'](state,current,run,barcode,out,snapshot,source,command)
except (OSError,ValueError) as error:
 print(str(error),file=sys.stderr);sys.exit(73)
'''
    return subprocess.run([sys.executable, '-B', '-c', code, str(ROOT / 'bin/report_history_state.py'),
        str(state), current, 'run', barcode, str(out), str(snapshot), str(source)],
        capture_output=True, text=True, timeout=30)


def test_private_source_history_leaf_precedes_artifact_lock(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(H, 'guard_private_source', lambda *args: events.append('history_guard') or {'report_revision': 'a' * 64})
    monkeypatch.setattr(H, 'artifact_command', lambda *args: events.append('artifact_lock'))
    H.publish_private_source(tmp_path, 'r1', 'run', 'b', tmp_path, tmp_path / 'snapshot',
                             tmp_path / 'private', ['publish-derived'])
    assert events == ['history_guard', 'artifact_lock']


@pytest.mark.parametrize('change', ['stale_roster', 'earlier_content', 'terminal',
                                    'same_terminal_order', 'missing_source_context',
                                    'source_terminal_tamper', 'unchanged'])
def test_rfpin_private_source_guard_precedes_public_replacement(tmp_path, change):
    count = 3 if change in ('same_terminal_order', 'earlier_content') else 2
    state, hist, rows = fixture(tmp_path, count)
    second = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r2/round_report.json').write_bytes(second)
    rows[1] = second
    if count == 3:
        # b,b,c gives a same-roster, same-terminal index-order change.
        (state / 'r2/round_report.json').write_bytes(rows[1].replace(b'"barcode": "c"', b'"barcode": "b"'))
        rows[1] = (state / 'r2/round_report.json').read_bytes()
        rows[2] = rows[2].replace(b'"barcode": "b"', b'"barcode": "c"')
        (state / 'r3/round_report.json').write_bytes(rows[2])
    if change == 'stale_roster':
        (state / 'r2/round_report.json').unlink()
        hist.write_bytes(rows[0])
    else:
        hist.write_bytes(b''.join(rows))
    source = tmp_path / 'private.json'
    command = ['perl', str(ROOT / 'bin/report_run_json.pl'), '--history', str(hist),
        '--out', str(source), '--run-id', 'run', '--barcode', 'b', '--state-id', 'state']
    captured = H.capture_run_context(command, state, 'r1', source)
    assert '--authority-context' in captured
    assert subprocess.run(captured, capture_output=True, timeout=30).returncode == 0
    snapshot = tmp_path / 'snapshot.jsonl'; snapshot.write_bytes(hist.read_bytes())
    context = H.context_for_run(state, 'run', require_complete=True)
    context['report_revision'] = H.revision(H.History(state, 'r1', run_id='run', barcode='b'), context)
    Path(str(snapshot) + '.authority.json').write_text(json.dumps(context))
    if change == 'stale_roster':
        (state / 'r2/round_report.json').write_bytes(second)
        hist.write_bytes(rows[0] + second)
    out = tmp_path / 'out'
    assert finalizer(state, out, 'r1').returncode == 0
    paths = [out / 'report_html/runs/run/run_report.json', out / 'report_html/runs_index.jsonl',
             out / 'report_html/runs/run/report.html']
    before = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths]
    if change == 'earlier_content':
        first = state / 'r1/round_report.json'; old = first.stat()
        first.write_bytes(rows[0].replace(b'"metric": 1', b'"metric": 9'))
        os.utime(first, ns=(old.st_atime_ns, old.st_mtime_ns))
    elif change == 'terminal':
        (state / '_state/round_index.tsv').write_text('r1\t2\nr2\t1\n')
    elif change == 'same_terminal_order':
        (state / '_state/round_index.tsv').write_text('r1\t2\nr2\t1\nr3\t3\n')
    elif change == 'missing_source_context':
        Path(str(source) + '.authority.json').unlink()
    elif change == 'source_terminal_tamper':
        private = Path(str(source) + '.authority.json')
        tampered = json.loads(private.read_text()); tampered['barcode'] = 'wrong'
        private.write_text(json.dumps(tampered))
    result = call_private_guard(state, out, snapshot, source,
        current='r3' if change == 'earlier_content' else 'r1',
        barcode='c' if change == 'earlier_content' else 'b')
    if change == 'unchanged':
        assert result.returncode == 0, result.stderr
        assert not (out / 'report_html/runs/run/.report_history_pending').exists()
    else:
        assert result.returncode == 73, result.stderr
        assert (out / 'report_html/runs/run/.report_history_pending').exists()
        assert [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths] == before
        assert json.loads(paths[0].read_bytes())['barcodes'] == (['b', 'c'] if count == 2 else ['b', 'c'])
        if change == 'stale_roster':
            assert json.loads(source.read_bytes())['barcodes'] == ['b']
            assert finalizer(state, out, 'r1').returncode == 0
            assert not (out / 'report_html/runs/run/.report_history_pending').exists()
            assert len(paths[1].read_bytes().splitlines()) == 1


def test_rfpin_source_guard_requires_revision_beyond_roster(tmp_path):
    state, hist, rows = fixture(tmp_path, 3); out = tmp_path / 'out'
    source = tmp_path / 'private.json'
    command = ['perl', str(ROOT / 'bin/report_run_json.pl'), '--history', str(hist),
        '--out', str(source), '--run-id', 'run', '--barcode', 'b', '--state-id', 'state']
    assert subprocess.run(H.capture_run_context(command, state, 'r3', source),
                          capture_output=True, timeout=30).returncode == 0
    assert finalizer(state, out, 'r3').returncode == 0
    snapshot = tmp_path / 'snapshot.jsonl'; snapshot.write_bytes(hist.read_bytes())
    context = H.context_for_run(state, 'run', require_complete=True)
    context['report_revision'] = H.revision(H.History(state, 'r3', run_id='run', barcode='b'), context)
    Path(str(snapshot) + '.authority.json').write_text(json.dumps(context))
    paths = [out / 'report_html/runs/run/run_report.json', out / 'report_html/runs_index.jsonl',
             out / 'report_html/runs/run/report.html']
    prior = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths]
    first = state / 'r1/round_report.json'; old = first.stat()
    first.write_bytes(rows[0].replace(b'"metric": 1', b'"metric": 9'))
    os.utime(first, ns=(old.st_atime_ns, old.st_mtime_ns))
    result = call_private_guard(state, out, snapshot, source, current='r3', barcode='b')
    assert result.returncode == 73, result.stderr
    assert (out / 'report_html/runs/run/.report_history_pending').exists()
    assert [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in paths] == prior


@pytest.mark.parametrize('cut', ['after_guard', 'after_json', 'after_index', 'after_html'])
def test_rfpin_post_guard_race_keeps_pending(tmp_path, monkeypatch, cut):
    from types import SimpleNamespace
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    second = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r2/round_report.json').write_bytes(second); hist.write_bytes(rows[0] + second)
    source = tmp_path / 'private.json'
    command = ['perl', str(ROOT / 'bin/report_run_json.pl'), '--history', str(hist),
        '--out', str(source), '--run-id', 'run', '--barcode', 'b', '--state-id', 'state']
    assert subprocess.run(H.capture_run_context(command, state, 'r1', source),
                          capture_output=True, timeout=30).returncode == 0
    assert finalizer(state, out, 'r1').returncode == 0
    snapshot = tmp_path / 'snapshot.jsonl'; snapshot.write_bytes(hist.read_bytes())
    context = H.context_for_run(state, 'run', require_complete=True)
    token = H.revision(H.History(state, 'r1', run_id='run', barcode='b'), context)
    context['report_revision'] = token
    Path(str(snapshot) + '.authority.json').write_text(json.dumps(context))
    assert call_private_guard(state, out, snapshot, source).returncode == 0
    marker = out / 'report_html/runs/run/.report_history_pending'; marker.write_text('pending\n')
    changed = False
    def mutate():
        nonlocal changed
        if not changed:
            rd = state / 'r1/round_report.json'
            rd.write_bytes(rows[0].replace(b'"metric": 1', b'"metric": 9'))
            changed = True
    if cut == 'after_guard':
        mutate()
    elif cut == 'after_json':
        original = H.atomic_bytes
        def after_json(path, data):
            original(path, data)
            if Path(path) == out / 'report_html/runs/run/run_report.json':
                mutate()
        monkeypatch.setattr(H, 'atomic_bytes', after_json)
    elif cut == 'after_index':
        original = subprocess.run
        def after_index(command, *args, **kwargs):
            result = original(command, *args, **kwargs)
            if isinstance(command, list) and any('report_run_index_update.sh' in str(part) for part in command):
                mutate()
            return result
        monkeypatch.setattr(subprocess, 'run', after_index)
    args = SimpleNamespace(state_dir=str(state), current_round_barcode='r1', outdir=str(out),
        run_id='run', barcode='b', snapshot=str(snapshot), source=str(source), html=1,
        identity_mode='sample', url_prefix='', auto_refresh='1', refresh_seconds='15',
        sample_plot_max='-1', lock_wait=0, guard_revision=token)
    H.publish_derived(args)
    monkeypatch.undo()
    if cut == 'after_html':
        mutate()
    assert changed
    recheck = ['/bin/bash', '-c', H.HISTORY_COMMAND, 'history-recheck', str(ROOT / 'bin'),
        str(state), '0', sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'),
        'recheck', '--state-dir', str(state), '--current-round-barcode', 'r1', '--outdir', str(out),
        '--run-id', 'run', '--barcode', 'b', '--snapshot', str(snapshot), '--revision', token,
        '--html', '1']
    assert subprocess.run(recheck, capture_output=True, timeout=30).returncode == 73
    assert marker.exists()


def test_run_wide_missing_foreign_report_is_not_terminal(tmp_path):
    state, hist, rows = fixture(tmp_path, 2)
    hist.write_bytes(rows[0])
    (state / 'r2/round_report.json').write_bytes(rows[1].replace(
        b'"run_id": "run"', b'"run_id": "other"'))
    (state / 'r2/round_report.json').unlink()
    index = state / '_state/round_index.tsv'
    assert H.run_repair_target(state, index, 'r1', 'run', 'b') == ('run', 'b', 'r1')
    assert hist.read_bytes() == rows[0]


def test_identical_round_name_in_separate_state_roots_isolated(tmp_path):
    left, _, _ = fixture(tmp_path / 'left', 1)
    right, history, rows = fixture(tmp_path / 'right', 1)
    foreign = rows[0].replace(b'"run_id": "run"', b'"run_id": "other"').replace(
        b'"barcode": "b"', b'"barcode": "c"')
    (right / 'r1/round_report.json').write_bytes(foreign)
    history.write_bytes(foreign)
    assert H.run_repair_target(left, left / '_state/round_index.tsv', 'r1', 'run', 'b') == ('run', 'b', 'r1')
    assert H.run_repair_target(right, right / '_state/round_index.tsv', 'r1', 'other', 'c') == ('other', 'c', 'r1')


def test_two_pending_barcodes_repair_one_per_invocation(tmp_path):
    state, hist, rows = fixture(tmp_path, 1)
    out = tmp_path / 'out'
    (state / '_state/run_started_utc.txt').write_text('2026-09-27T00:00:00Z\n')
    for rb, number, barcode in [('later', 2, 'c'), ('early', 3, 'd')]:
        directory = state / rb; directory.mkdir()
        obj = dict(schema_version='2.1', state_id='state', run_id='run',
                   barcode=barcode, round_barcode=rb)
        (directory / 'round_report.json').write_text(json.dumps(obj) + '\n')
    index = state / '_state/round_index.tsv'
    index.write_text('r1\t1\nlater\t2\nearly\t3\n')
    command = [sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'), 'finalize',
               '--state-dir', str(state), '--outdir', str(out), '--run-id', 'run', '--html', '0', '--lock-wait', '0']
    assert H.run_repair_target(state, index, 'r1', 'run', 'b') == ('run', 'c', 'later')
    first = subprocess.run(command + ['--current-round-barcode', 'later', '--barcode', 'c'], capture_output=True)
    assert first.returncode == 73 and b'Offline repair:' in first.stderr
    assert [json.loads(row)['barcode'] for row in hist.read_bytes().split(b'\n') if row] == ['b', 'c']
    assert not (out / 'report_html/runs/run/run_report.json').exists()
    assert (out / 'report_html/runs/run/.report_history_pending').exists()
    assert H.run_repair_target(state, index, 'r1', 'run', 'b') == ('run', 'd', 'early')
    # The original offline command advances to the next pending barcode on retry.
    second = subprocess.run(command + ['--current-round-barcode', 'later', '--barcode', 'c'], capture_output=True)
    assert second.returncode == 0, second.stderr
    assert [json.loads(row)['barcode'] for row in hist.read_bytes().split(b'\n') if row] == ['b', 'c', 'd']
    report = json.loads((out / 'report_html/runs/run/run_report.json').read_text())
    assert report['rounds_count'] == 3
    entries = [json.loads(row) for row in (out / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if row]
    assert len(entries) == 1 and entries[0]['rounds_count'] == 3
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()
    retry = subprocess.run(command + ['--current-round-barcode', 'early', '--barcode', 'd'], capture_output=True)
    assert retry.returncode == 0 and hist.read_bytes().count(b'"barcode": "d"') == 1


def test_recheck_cannot_clear_after_new_same_run_barcode(tmp_path, monkeypatch):
    from types import SimpleNamespace
    state, hist, rows = fixture(tmp_path, 1)
    out = tmp_path / 'out'
    assert finalizer(state, out, 'r1', html=0).returncode == 0
    report = out / 'report_html/runs/run/run_report.json'
    prior = report.read_bytes()
    snapshot = state / '_state/test.snapshot'
    snapshot.write_bytes(hist.read_bytes())
    Path(str(snapshot) + '.run.json').write_bytes(prior)
    view = H.History(state, 'r1', run_id='run', barcode='b')
    assert view.classify() == 'complete'
    token = H.revision(view)
    c = state / 'r2'; c.mkdir()
    c_row = rows[0].replace(b'"round_barcode": "r1"', b'"round_barcode": "r2"').replace(
        b'"barcode": "b"', b'"barcode": "c"')
    (c / 'round_report.json').write_bytes(c_row)
    (state / '_state/round_index.tsv').write_text('r1\t1\nr2\t2\n')
    args = SimpleNamespace(state_dir=str(state), current_round_barcode='r1', history=None,
                           round_index_file=None, run_id='run', barcode='b', outdir=str(out),
                           snapshot=str(snapshot), revision=token, lock_fd=9, html=0,
                           identity_mode='sample')
    monkeypatch.setattr(H, 'lock_identity', lambda *unused: None)
    assert H.recheck(args) is False
    assert (out / 'report_html/runs/run/.report_history_pending').exists()
    assert report.read_bytes() == prior


def locked_reconcile(state,current='r3',repeat=1):
    code='''source "$1/bin/lib/lock_utils.sh"
init_lock_helpers
LOCK_WAIT=2
acquire_lock "$2/_state/.report_history.lock" || exit 2
fd=${acquired_lock_fds[$((${#acquired_lock_fds[@]}-1))]}
for ((i=0; i<$4; i++)); do
python3 -B "$1/bin/report_history_state.py" rebuild-history --state-dir "$2" --current-round-barcode "$3" --lock-fd "$fd" || exit $?
done
release_lock "$2/_state/.report_history.lock"
'''
    return subprocess.run(['/bin/bash','-c',code,'test',str(ROOT),str(state),current,str(repeat)],capture_output=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},timeout=15)


def test_rebuild_preserves_two_future_rows_and_old_bytes(tmp_path):
    state,hist,rows=fixture(tmp_path,5);hist.write_bytes(rows[0]+rows[2]+rows[3]+rows[4])
    assert view(state)[0]=='normalize'
    result=locked_reconcile(state,repeat=2);assert result.returncode==0,result.stderr
    assert hist.read_bytes()==b''.join(rows)
    assert view(state,'r5')[0]=='complete'
    assert not list(hist.parent.glob('*.tmp.*'))


def test_quarantine_exact_bytes_audit_and_retry(tmp_path):
    state,hist,rows=fixture(tmp_path)
    bad=b'\xffbroken\n';partial=b'{"round_barcode":'
    source=rows[0]+bad+rows[2]+partial;hist.write_bytes(source)
    result=locked_reconcile(state,repeat=2);assert result.returncode==0,result.stderr
    assert hist.read_bytes()==b''.join(rows)
    q=hist.parent/'report_history.quarantine.jsonl';audit=hist.parent/'report_history.quarantine.log'
    assert q.read_bytes()==bad+partial
    records=[json.loads(x) for x in audit.read_bytes().split(b'\n') if x];assert len(records)==1
    assert records[0]['records'][0]['byte_offset']==len(rows[0]);assert records[0]['records'][0]['line']==2
    assert records[0]['source']==str(hist)
    # Interrupted after archive/audit but before active replacement: the same source does not duplicate.
    hist.write_bytes(source)
    result=locked_reconcile(state);assert result.returncode==0,result.stderr
    assert q.read_bytes()==bad+partial;assert len(audit.read_bytes().split(b'\n'))==2


@pytest.mark.parametrize('damage',['conflict','future','missing'])
def test_refusal_precedes_any_mutation(tmp_path,damage):
    state,hist,rows=fixture(tmp_path,5);hist.write_bytes(rows[0]+rows[3]+rows[4]+b'bad\n')
    if damage=='conflict':hist.write_bytes(hist.read_bytes()+rows[0].replace(b'"metric": 1',b'"metric": 8'))
    elif damage=='future':(state/'r5/round_report.json').write_bytes(rows[4].replace(b'"metric": 5',b'"metric": 8'))
    else:(state/'r2/round_report.json').unlink()
    before=hist.read_bytes();result=locked_reconcile(state)
    assert result.returncode!=0;assert hist.read_bytes()==before
    assert not list(hist.parent.glob('report_history.quarantine.*'))


def test_cli_and_no_lock_write_refusal(tmp_path):
    state,hist,_=fixture(tmp_path)
    base=[sys.executable,'-B',str(ROOT/'bin/report_history_state.py')]
    result=subprocess.run(base+['classify','--state-dir',str(state),'--current-round-barcode','r3'],capture_output=True)
    assert result.returncode==0 and result.stdout==b'complete\n'
    result=subprocess.run(base+['rebuild-history','--state-dir',str(state),'--current-round-barcode','r3'],capture_output=True)
    assert result.returncode==73
    assert subprocess.run(base+['bogus'],capture_output=True).returncode==64


def finalizer(state, outdir, current='r3', html=1):
    return subprocess.run([sys.executable,'-B',str(ROOT/'bin/report_history_state.py'),'finalize',
                           '--state-dir',str(state),'--current-round-barcode',current,'--outdir',str(outdir),
                           '--run-id','run','--barcode','b','--html',str(html),'--lock-wait','0'],
                          capture_output=True,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},timeout=30)


@pytest.mark.parametrize('layer',['history','run-json','index','html','marker-absent','marker-stale'])
def test_finalizer_repairs_every_layer(tmp_path,layer):
    state,hist,rows=fixture(tmp_path);out=tmp_path/'results'
    result=finalizer(state,out);assert result.returncode==0,result.stderr
    run=out/'report_html/runs/run';marker=run/'.report_history_pending'
    if layer=='history':hist.write_bytes(rows[0]+rows[2])
    elif layer=='run-json':(run/'run_report.json').write_text('{"run_id":"run"}\n')
    elif layer=='index':(out/'report_html/runs_index.jsonl').write_text('{"run_id":"run"}\n')
    elif layer=='html':(run/'report.html').write_text('stale')
    elif layer=='marker-stale':marker.write_text('complete')
    else:assert not marker.exists()
    result=finalizer(state,out);assert result.returncode==0,result.stderr
    assert hist.read_bytes()==b''.join(rows)
    assert json.loads((run/'run_report.json').read_text())['rounds_count']==3
    index=[json.loads(row) for row in (out/'report_html/runs_index.jsonl').read_bytes().split(b'\n') if row]
    assert len(index)==1
    assert not marker.exists()
    assert not list(hist.parent.glob('.report_history.snapshot.*'))
    assert not list(run.glob('*.tmp.*'))


def test_finalizer_invalid_authority_fails_closed(tmp_path):
    state,hist,rows=fixture(tmp_path);(state/'r2/round_report.json').unlink()
    before=hist.read_bytes();result=finalizer(state,tmp_path/'results')
    assert result.returncode!=0 and b'REPORT_HISTORY_PENDING' in result.stderr and b'Offline repair:' in result.stderr
    assert hist.read_bytes()==before


def test_revision_and_published_artifact_check(tmp_path):
    state,hist,rows=fixture(tmp_path);v=H.History(state,'r3');assert v.classify()=='complete'
    before=H.revision(v);(state/'_state/round_index.tsv').write_bytes((state/'_state/round_index.tsv').read_bytes()+b'r4\t4\n')
    assert H.revision(v)!=before


def test_terminal_uses_retained_authority_in_index_order(tmp_path):
    state, hist, rows = fixture(tmp_path, 4)
    index = state / '_state/round_index.tsv'
    (state / 'r4/round_report.json').unlink()
    assert H.terminal_round(state, index, 'r2') == 'r3'
    # The last indexed name is neither the lexical nor a numeric maximum.
    for old, new in [('r1', 'omega'), ('r2', 'zeta'), ('r3', 'alpha')]:
        (state / old).rename(state / new)
        report = state / new / 'round_report.json'
        report.write_bytes(report.read_bytes().replace(('"round_barcode": "' + old + '"').encode(),
                                                     ('"round_barcode": "' + new + '"').encode()))
    index.write_text('omega\t1\nzeta\t2\nalpha\t3\nr4\t4\n')
    assert H.terminal_round(state, index, 'omega') == 'alpha'
    assert H.terminal_round(state, index, 'r4', 'run', 'b') == 'alpha'
    index.write_text('omega\t1\nzeta\t2\nalpha\t2\nr4\t4\n')
    with pytest.raises(H.Refusal, match='duplicate_or_unsafe_round_index'):
        H.terminal_round(state, index, 'omega')


@pytest.mark.parametrize('foreign_run,foreign_barcode', [('other', 'b'), ('run', 'other')])
def test_terminal_is_scoped_to_run_and_barcode(tmp_path, foreign_run, foreign_barcode):
    state, hist, rows = fixture(tmp_path, 4)
    index = state / '_state/round_index.tsv'
    foreign = rows[2].replace(b'"run_id": "run"', ('"run_id": "' + foreign_run + '"').encode())
    foreign = foreign.replace(b'"barcode": "b"', ('"barcode": "' + foreign_barcode + '"').encode())
    (state / 'r3/round_report.json').write_bytes(foreign)
    (state / 'r4/round_report.json').unlink()
    assert H.terminal_round(state, index, 'r2', 'run', 'b') == 'r2'
    assert H.terminal_round(state, index, 'r4', 'run', 'b') == 'r2'
    with pytest.raises(H.Refusal, match='missing_reporting_identity'):
        H.terminal_round(state, index, 'r4')
    assert H.terminal_round(state, index, 'r3', foreign_run, foreign_barcode) == 'r3'
    with pytest.raises(H.Refusal, match='current_identity_mismatch'):
        H.terminal_round(state, index, 'r2', foreign_run, foreign_barcode)
    (state / 'r4/round_report.json').write_bytes(rows[3])
    assert H.terminal_round(state, index, 'r2', 'run', 'b') == 'r4'
    (state / 'r3/round_report.json').unlink()
    assert H.terminal_round(state, index, 'r2', 'run', 'b') == 'r4'
    (state / 'r4/round_report.json').unlink()
    assert H.terminal_round(state, index, 'r2', 'run', 'b') == 'r2'


def test_interleaved_terminal_uses_index_not_name_or_discovery_order(tmp_path):
    state = tmp_path / 'state'; (state / '_state').mkdir(parents=True)
    ordered = [('z-A', 'runA'), ('x-B', 'runB'), ('a-A', 'runA'), ('q-B', 'runB')]
    for rb, run in reversed(ordered):
        directory = state / rb; directory.mkdir()
        (directory / 'round_report.json').write_text(json.dumps(dict(schema_version='2.1', state_id='state',
            run_id=run, barcode='b', round_barcode=rb)) + '\n')
    index = state / '_state/round_index.tsv'
    index.write_text(''.join(f'{rb}\t{i}\n' for i, (rb, _) in enumerate(ordered, 1)))
    assert H.terminal_round(state, index, 'z-A', 'runA', 'b') == 'a-A'
    assert H.terminal_round(state, index, 'x-B', 'runB', 'b') == 'q-B'
    history = state / '_state/report_history.jsonl'
    history.write_bytes(b''.join((state / rb / 'round_report.json').read_bytes() for rb, _ in ordered))
    assert H.History(state, 'z-A', run_id='runA', barcode='b').classify() == 'complete'
    assert H.History(state, 'x-B', run_id='runB', barcode='b').classify() == 'complete'


def test_cross_run_finalizers_preserve_each_others_rows_and_outputs(tmp_path):
    state, hist, rows = fixture(tmp_path, 3); out = tmp_path / 'out'
    foreign = rows[2].replace(b'"run_id": "run"', b'"run_id": "other"')
    (state / 'r3/round_report.json').write_bytes(foreign)
    hist.write_bytes(rows[0] + rows[1])
    assert finalizer(state, out, 'r2', html=0).returncode == 0
    own = out / 'report_html/runs/run/run_report.json'; before = own.read_bytes()
    command = [sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'), 'finalize',
               '--state-dir', str(state), '--current-round-barcode', 'r3', '--run-id', 'other',
               '--barcode', 'b', '--outdir', str(out), '--lock-wait', '0', '--html', '0']
    other = subprocess.run(command, capture_output=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=30)
    assert other.returncode == 0, other.stderr
    assert hist.read_bytes() == rows[0] + rows[1] + foreign
    assert own.read_bytes() == before
    assert json.loads((out / 'report_html/runs/other/run_report.json').read_text())['rounds_count'] == 1
    assert finalizer(state, out, 'r2', html=0).returncode == 0
    assert hist.read_bytes() == rows[0] + rows[1] + foreign
    index = [json.loads(row) for row in (out / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if row]
    assert sorted(row['run_id'] for row in index) == ['other', 'run']


@pytest.mark.parametrize('foreign_run,foreign_barcode,run_rounds', [('other', 'b', 2), ('run', 'other', 3)])
def test_cross_run_rebuild_preserves_valid_foreign_future_bytes(tmp_path, foreign_run, foreign_barcode, run_rounds):
    state, hist, rows = fixture(tmp_path, 3); out = tmp_path / 'out'
    foreign = rows[2].replace(b'"run_id": "run"', ('"run_id": "' + foreign_run + '"').encode())
    foreign = foreign.replace(b'"barcode": "b"', ('"barcode": "' + foreign_barcode + '"').encode())
    (state / 'r3/round_report.json').write_bytes(foreign)
    old_foreign = foreign.replace(b'"metric": 3', b'"metric": 3.000e0')
    hist.write_bytes(rows[0] + old_foreign)
    result = finalizer(state, out, 'r2', html=0)
    assert result.returncode == 0, result.stderr
    assert hist.read_bytes() == rows[0] + rows[1] + old_foreign
    # The existing run JSON aggregates a run's barcodes; terminal choice still uses both identity fields.
    assert json.loads((out / 'report_html/runs/run/run_report.json').read_text())['rounds_count'] == run_rounds
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()


def test_finalizer_chooses_terminal_and_concurrent_retries_are_idempotent(tmp_path):
    state, hist, rows = fixture(tmp_path, 4)
    out = tmp_path / 'out'
    hist.write_bytes(b''.join(rows[:3]))
    command = [sys.executable, '-B', str(ROOT / 'bin/report_history_state.py'), 'finalize',
               '--state-dir', str(state), '--current-round-barcode', 'r3', '--run-id', 'run',
               '--barcode', 'b', '--outdir', str(out), '--lock-wait', '0', '--html', '0']
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    other = command.copy(); other[other.index('r3')] = 'r4'
    second = subprocess.Popen(other, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    first.communicate(timeout=30); second.communicate(timeout=30)
    assert finalizer(state, out, 'r3', html=0).returncode == 0
    assert hist.read_bytes() == b''.join(rows)
    assert json.loads((out / 'report_html/runs/run/run_report.json').read_text())['rounds_count'] == 4
    assert len([row for row in (out / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if row]) == 1
    assert not (out / 'report_html/runs/run/.report_history_pending').exists()


def test_malformed_future_rebuild_requires_all_mapped_authority(tmp_path):
    state,hist,rows=fixture(tmp_path)
    hist.write_bytes(rows[0]+b'broken future\n')
    result=locked_reconcile(state,'r1');assert result.returncode==0,result.stderr
    assert hist.read_bytes()==b''.join(rows)
    hist.write_bytes(rows[0]+b'broken future\n');(state/'r3/round_report.json').unlink()
    before=hist.read_bytes();result=locked_reconcile(state,'r1')
    assert result.returncode!=0 and hist.read_bytes()==before


@pytest.mark.parametrize('raw',[b'[]\n',b'1\n',b'{}\n'])
def test_non_records_are_malformed(tmp_path,raw):
    state,hist,rows=fixture(tmp_path);hist.write_bytes(rows[0]+raw+rows[2])
    assert view(state)[0]=='blocked_malformed'

@pytest.mark.parametrize('cut',['audit','archive','active'])
def test_interrupted_quarantine_and_active_publication_retry(tmp_path,monkeypatch,cut):
    state,hist,rows=fixture(tmp_path);bad=b'\xffbroken\n';source=rows[0]+bad+rows[2];hist.write_bytes(source)
    originals={p:p.read_bytes() for p in state.glob('*/round_report.json')}
    monkeypatch.setattr(H,'lock_identity',lambda *args:None)
    real=H.atomic_bytes
    target={'audit':'report_history.quarantine.log','archive':'report_history.quarantine.jsonl','active':'report_history.jsonl'}[cut]
    def interrupted(path,data):
        if Path(path).name==target:
            raise OSError('injected publication interruption')
        return real(path,data)
    monkeypatch.setattr(H,'atomic_bytes',interrupted)
    with pytest.raises(OSError):H.reconcile(state,'r3')
    assert hist.read_bytes()==source
    assert all(p.read_bytes()==raw for p,raw in originals.items())
    monkeypatch.setattr(H,'atomic_bytes',real)
    H.reconcile(state,'r3');H.reconcile(state,'r3')
    assert hist.read_bytes()==b''.join(rows)
    assert (hist.parent/'report_history.quarantine.jsonl').read_bytes()==bad
    assert len((hist.parent/'report_history.quarantine.log').read_bytes().split(b'\n'))==2


@pytest.mark.parametrize('artifact',['run-json','index','html','root-html','revision'])
def test_recheck_refuses_stale_artifacts_and_retains_pending(tmp_path,monkeypatch,artifact):
    from types import SimpleNamespace
    state,hist,rows=fixture(tmp_path);out=tmp_path/'out'
    result=finalizer(state,out);assert result.returncode==0,result.stderr
    snap=state/'_state/test.snapshot';snap.write_bytes(hist.read_bytes())
    run=out/'report_html/runs/run';Path(str(snap)+'.run.json').write_bytes((run/'run_report.json').read_bytes())
    v=H.History(state,'r3');assert v.classify()=='complete'
    args=SimpleNamespace(state_dir=str(state),current_round_barcode='r3',history=None,round_index_file=None,
        run_id='run',barcode='b',outdir=str(out),snapshot=str(snap),revision=H.revision(v),lock_fd=9,html=1,identity_mode='sample')
    monkeypatch.setattr(H,'lock_identity',lambda *args:None)
    marker=run/'.report_history_pending';marker.write_text('advisory')
    assert H.recheck(args) and not marker.exists()
    if artifact=='run-json':(run/'run_report.json').write_text('{"run_id":"run"}')
    elif artifact=='index':(out/'report_html/runs_index.jsonl').write_text('{"run_id":"run"}\n')
    elif artifact=='html':(run/'report.html').write_text('stale')
    elif artifact=='root-html':(out/'report_html/report.html').write_text('stale')
    else:args.revision='0'*64
    assert not H.recheck(args)
    assert marker.exists()


def test_numeric_large_integer_and_boolean_history_disagreement(tmp_path):
    state,hist,rows=fixture(tmp_path,1)
    report=state/'r1/round_report.json'
    for authority,derived in [(b'9007199254740993',b'9007199254740992'),(b'true',b'1')]:
        report.write_bytes(rows[0].replace(b'"metric": 1',b'"metric": '+authority))
        hist.write_bytes(rows[0].replace(b'"metric": 1',b'"metric": '+derived))
        assert view(state,'r1')[0]=='normalize'


def test_full_scan_preserves_valid_old_numeric_and_garbled_bytes(tmp_path):
    state,hist,rows=fixture(tmp_path)
    old=rows[0].replace(b'"metric": 1',b'"metric": 1.000e0')
    hist.write_bytes(old+rows[2])
    result=locked_reconcile(state);assert result.returncode==0,result.stderr
    assert hist.read_bytes()==old+rows[1]+rows[2]
    assert not (hist.parent/'report_history.quarantine.jsonl').exists()


def test_append_retry_does_not_duplicate_current_row(tmp_path):
    state,hist,rows=fixture(tmp_path);hist.write_bytes(b''.join(rows[:2]))
    result=locked_reconcile(state,repeat=2)
    assert result.returncode==0,result.stderr
    assert hist.read_bytes()==b''.join(rows)


def test_automatic_finalizer_passes_zero_wait_to_the_lock_protocol(tmp_path,monkeypatch):
    from types import SimpleNamespace
    state,hist,rows=fixture(tmp_path)
    args=SimpleNamespace(state_dir=str(state),current_round_barcode='r3',outdir=str(tmp_path/'out'),run_id='run',barcode='b',
        html=1,identity_mode='sample',lock_wait=0,url_prefix='',auto_refresh='0',refresh_seconds='15',sample_plot_max='-1')
    calls=[]
    def lock_probe(command,timeout,**kwargs):
        calls.append(command)
        assert command[6]=='0',command
        assert timeout<=10
        raise H.Refusal('live holder')
    monkeypatch.setattr(H,'bounded',lock_probe)
    assert H.finalize(args)==73
    assert len(calls)==1


def test_interrupted_run_publication_keeps_previous_json_and_recovers(tmp_path,monkeypatch):
    from types import SimpleNamespace
    state,hist,rows=fixture(tmp_path);out=tmp_path/'out'
    assert finalizer(state,out).returncode==0
    published=out/'report_html/runs/run/run_report.json';before=published.read_bytes()
    marker=published.parent/'.report_history_pending';marker.write_text('interrupted publication\n')
    snapshot=hist.parent/'.report_history.snapshot.test.jsonl';snapshot.write_bytes(hist.read_bytes())
    context=H.context_for_run(state,'run',require_complete=True)
    context['report_revision']=H.revision(H.History(state,'r3',run_id='run',barcode='b'),context)
    Path(str(snapshot)+'.authority.json').write_text(json.dumps(context))
    args=SimpleNamespace(state_dir=str(state),outdir=str(out),run_id='run',barcode='b',snapshot=str(snapshot),
        lock_wait=0,guard_revision=context['report_revision'])
    replace=H.os.replace
    def interrupted(source,destination):
        if Path(destination)==published:raise OSError('cut before run JSON rename')
        return replace(source,destination)
    monkeypatch.setattr(H.os,'replace',interrupted)
    with pytest.raises(OSError,match='cut before'):H.generate_run(args)
    assert published.read_bytes()==before and marker.exists()
    assert not list(published.parent.glob('.run_report.json.tmp.*'))
    monkeypatch.setattr(H.os,'replace',replace);snapshot.unlink()
    assert finalizer(state,out).returncode==0
    assert not marker.exists()


def test_finalizer_missing_state_reports_exact_configured_offline_command(tmp_path):
    import shlex
    out=tmp_path/'out';missing=tmp_path/'missing-state'
    command=[sys.executable,'-B',str(ROOT/'bin/report_history_state.py'),'finalize','--state-dir',str(missing),
        '--current-round-barcode','r3','--outdir',str(out),'--run-id','run','--barcode','b',
        '--html','0','--identity-mode','track','--url-prefix','/my reports','--auto-refresh','0',
        '--refresh-seconds','27','--sample-plot-max','9']
    result=subprocess.run(command,capture_output=True,text=True)
    assert result.returncode==73 and 'REPORT_HISTORY_PENDING' in result.stderr
    repair=shlex.split(next(line[len('Offline repair: '):] for line in result.stderr.split('\n') if line.startswith('Offline repair: ')))
    for name,value in [('--html','0'),('--identity-mode','track'),('--url-prefix','/my reports'),('--auto-refresh','0'),('--refresh-seconds','27'),('--sample-plot-max','9')]:
        assert repair[repair.index(name)+1]==value
    assert (out/'report_html/runs/run/.report_history_pending').exists()


@pytest.mark.parametrize('change', ['roster', 'earlier_content', 'order', 'missing_context', 'foreign_run'])
def test_stage_b_guarded_publication_refuses_stale_standalone_source(tmp_path, change):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    source = tmp_path / 'private.json'
    command = ['perl', str(ROOT / 'bin/report_run_json.pl'), '--history', str(hist),
        '--out', str(source), '--run-id', 'run', '--barcode', 'b', '--state-id', 'state']
    assert subprocess.run(H.capture_run_context(command, state, 'r1', source),
                          capture_output=True, timeout=30).returncode == 0
    if change == 'roster':
        other = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
        (state / 'r2/round_report.json').write_bytes(other)
        hist.write_bytes(rows[0] + other)
    elif change == 'earlier_content':
        changed = rows[0].replace(b'"metric": 1', b'"metric": 9')
        (state / 'r1/round_report.json').write_bytes(changed)
        hist.write_bytes(changed + rows[1])
    elif change == 'order':
        (state / '_state/round_index.tsv').write_text('r1\t2\nr2\t1\n')
    elif change == 'missing_context':
        Path(str(source) + '.authority.json').unlink()
    else:
        foreign = rows[0].replace(b'"run_id": "run"', b'"run_id": "other"')
        source.write_bytes(foreign)
    assert finalizer(state, out, 'r1', html=0).returncode == 0
    # Prior public artifacts are genuine and must not change on any refusal.
    public = out / 'report_html/runs/run/run_report.json'
    index = out / 'report_html/runs_index.jsonl'
    html = out / 'report_html/runs/run/report.html'
    for path, data in ((public, b'public-json\n'), (index, b'public-index\n'), (html, b'public-html\n')):
        path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(data)
    before = [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in (public, index, html)]
    result = call_stage_b_publish_run(state, out, source)
    assert result.returncode == 73, result.stderr
    assert [(p.read_bytes(), p.stat().st_ino, p.stat().st_mtime_ns) for p in (public, index, html)] == before
    assert (public.parent / '.report_history_pending').exists()


def test_stage_b_guarded_publication_accepts_current_source_once(tmp_path):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    source = tmp_path / 'private.json'
    command = ['perl', str(ROOT / 'bin/report_run_json.pl'), '--history', str(hist),
        '--out', str(source), '--run-id', 'run', '--barcode', 'b', '--state-id', 'state']
    assert subprocess.run(H.capture_run_context(command, state, 'r2', source),
                          capture_output=True, timeout=30).returncode == 0
    assert finalizer(state, out, 'r2', html=0).returncode == 0
    assert call_stage_b_publish_run(state, out, source, 'r2').returncode == 0
    public = out / 'report_html/runs/run/run_report.json'
    assert public.read_bytes() == source.read_bytes()
    assert call_stage_b_publish_run(state, out, source, 'r2').returncode == 0
    assert public.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('barcodes', ['one', 'two'])
@pytest.mark.parametrize('same_metadata', [False, True])
def test_stage_b_finalizer_reconciles_preexisting_stale_nonterminal(tmp_path, barcodes, same_metadata):
    state, hist, rows = fixture(tmp_path, 3); out = tmp_path / 'out'
    if barcodes == 'two':
        later = rows[2].replace(b'"barcode": "b"', b'"barcode": "c"')
        (state / 'r3/round_report.json').write_bytes(later)
        hist.write_bytes(rows[0] + rows[1] + later)
    else:
        later = rows[2]
    assert finalizer(state, out, 'r1').returncode == 0
    source = state / 'r1/round_report.json'; before = source.stat()
    newer = rows[0].replace(b'"metric": 1', b'"metric": 9')
    assert len(newer) == before.st_size
    source.write_bytes(newer)
    if same_metadata:
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert source.stat().st_mtime_ns == before.st_mtime_ns
    marker = out / 'report_html/runs/run/.report_history_pending'; marker.write_text('pending\n')
    result = finalizer(state, out, 'r1')
    assert result.returncode == 0, result.stderr
    assert hist.read_bytes() == newer + rows[1] + later
    assert not marker.exists()
    record = json.loads((out / 'report_html/runs/run/run_report.json').read_bytes())
    assert record['barcodes'] == (['b', 'c'] if barcodes == 'two' else ['b'])
    index = [json.loads(line) for line in (out / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if line]
    assert len([row for row in index if row['run_id'] == 'run']) == 1


@pytest.mark.parametrize('n', [1, 10, 100, 1000])
def test_stage_b_one_guard_evaluation_opens_each_report_once(tmp_path, monkeypatch, n):
    state, hist, rows = fixture(tmp_path, n)
    assert finalizer(state, tmp_path / 'out', f'r{n}', html=0).returncode == 0
    source = tmp_path / 'source.json'
    context = H.context_for_run(state, 'run', require_complete=True)
    context['report_revision'] = H.revision(H.History(state, f'r{n}', run_id='run', barcode='b'), context)
    source.write_text(json.dumps({**context, 'schema_version': '2.0'}))
    Path(str(source) + '.authority.json').write_text(json.dumps(context))
    code = '''import json,pathlib,runpy,sys
api=runpy.run_path(sys.argv[1]);opened=[];original=pathlib.Path.read_bytes
def count(path):
 if path.name=='round_report.json':opened.append(str(path))
 return original(path)
pathlib.Path.read_bytes=count
api['guard_private_source'](sys.argv[2],sys.argv[3],'run','b',sys.argv[4],None,sys.argv[5])
print(json.dumps(opened))
'''
    result = subprocess.run([sys.executable, '-B', '-c', code, str(ROOT/'bin/report_history_state.py'),
        str(state), f'r{n}', str(tmp_path/'out'), str(source)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    opened = json.loads(result.stdout)
    assert len(opened) == n
    assert len(set(opened)) == n


def test_stage_b_snapshot_detects_stale_nonterminal_before_publication(tmp_path):
    state, hist, rows = fixture(tmp_path, 3)
    (state / 'r1/round_report.json').write_bytes(rows[0].replace(b'"metric": 1', b'"metric": 9'))
    _, pending, _ = H.run_completion(state, state / '_state/round_index.tsv', 'r3', 'run', 'b',
                                     hist, include_context=True)
    assert any(item[-1] == 'stale_history_row' and item[3] == 'r1' for item in pending)
    assert hist.read_bytes() == b''.join(rows)


def call_stage_b_publish_run(state, out, source, current='r1'):
    code = '''import runpy,sys
api=runpy.run_path(sys.argv[1])
try:
 api['publish_run'](sys.argv[2],sys.argv[3],sys.argv[4],'run',sys.argv[5],'b',None)
except (OSError,ValueError) as error:
 print(str(error),file=sys.stderr);sys.exit(73)
'''
    return subprocess.run([sys.executable, '-B', '-c', code, str(ROOT/'bin/report_history_state.py'),
        str(source), str(state), str(out), current], capture_output=True, text=True, timeout=30)


def test_stage_b_finalizer_refuses_changed_future_barcode_nonterminal(tmp_path):
    state, hist, rows = fixture(tmp_path, 3); out = tmp_path / 'out'
    c2 = rows[1].replace(b'"barcode": "b"', b'"barcode": "c"')
    c3 = rows[2].replace(b'"barcode": "b"', b'"barcode": "c"')
    (state / 'r2/round_report.json').write_bytes(c2)
    (state / 'r3/round_report.json').write_bytes(c3)
    hist.write_bytes(rows[0] + c2 + c3)
    assert finalizer(state, out, 'r1').returncode == 0
    updated = c2.replace(b'"metric": 2', b'"metric": 8')
    (state / 'r2/round_report.json').write_bytes(updated)
    marker = out / 'report_html/runs/run/.report_history_pending'; marker.write_text('pending\n')
    result = finalizer(state, out, 'r1')
    assert result.returncode == 73 and b'unverifiable_future_row:r2' in result.stderr
    assert hist.read_bytes() == rows[0] + c2 + c3
    assert marker.exists()
    assert (out / 'report_html/runs/run/run_report.json').exists()


def test_stage_b_finalizer_refuses_stale_nonterminal_identity(tmp_path):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    assert finalizer(state, out, 'r2').returncode == 0
    public = out / 'report_html/runs/run/run_report.json'; before = public.read_bytes()
    (state / 'r1/round_report.json').write_bytes(rows[0].replace(b'"round_barcode": "r1"', b'"round_barcode": "wrong"'))
    result = finalizer(state, out, 'r2')
    assert result.returncode == 73
    assert public.read_bytes() == before and hist.read_bytes() == b''.join(rows)
    assert (out / 'report_html/runs/run/.report_history_pending').exists()


def test_stage_b_finalizer_quarantines_malformed_nonterminal(tmp_path):
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    assert finalizer(state, out, 'r2').returncode == 0
    corrupt = b'\xffpartial\n'
    hist.write_bytes(corrupt + rows[1])
    marker = out / 'report_html/runs/run/.report_history_pending'; marker.write_text('pending\n')
    result = finalizer(state, out, 'r2')
    assert result.returncode == 0, result.stderr
    assert hist.read_bytes() == b''.join(rows)
    assert (state / '_state/report_history.quarantine.jsonl').read_bytes() == corrupt
    assert not marker.exists()


def test_stage_b_finalizer_post_publication_revision_recheck(tmp_path):
    import shutil
    state, hist, rows = fixture(tmp_path, 2); out = tmp_path / 'out'
    root = tmp_path / 'production-copy'
    shutil.copytree(ROOT / 'bin', root / 'bin')
    shutil.copytree(ROOT / 'assets', root / 'assets')
    rebuild = root / 'bin/report_rebuild.sh'
    rebuild.rename(root / 'bin/report_rebuild-real.sh')
    rebuild.write_text("""#!/bin/bash
set -e
/bin/bash "$(dirname "$0")/report_rebuild-real.sh" "$@"
python3 -B - "$RTB_MUTATE_AFTER_RENDER" <<'PY_MUTATE'
from pathlib import Path
import sys
p=Path(sys.argv[1]); raw=p.read_bytes(); p.write_bytes(raw.replace(b'"metric": 1', b'"metric": 9'))
PY_MUTATE
""")
    rebuild.chmod(0o755)
    command = [sys.executable, '-B', str(root / 'bin/report_history_state.py'), 'finalize',
        '--state-dir', str(state), '--current-round-barcode', 'r2', '--run-id', 'run',
        '--barcode', 'b', '--outdir', str(out), '--html', '1', '--lock-wait', '0']
    result = subprocess.run(command, capture_output=True, timeout=60,
        env={**os.environ, 'RTB_MUTATE_AFTER_RENDER': str(state / 'r1/round_report.json')})
    assert result.returncode == 73, result.stderr
    assert b'REPORT_HISTORY_PENDING' in result.stderr
    assert (out / 'report_html/runs/run/.report_history_pending').exists()
    assert hist.read_bytes() == b''.join(rows)
    # A later invocation reconciles the changed retained report and converges.
    assert finalizer(state, out, 'r2').returncode == 0
    assert b'"metric": 9' in hist.read_bytes()
