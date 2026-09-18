"""S3b launcher/session contracts. Run with an external pytest --basetemp.

Set RTB_RUN_REAL_NXF=1 to exercise the already cached 22.10.8 runtime offline.
No biological workflow is launched. All wrapper copies and runtime writes are
below tmp_path; the cached runtime is read-only.
"""
import base64
import csv
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "910e3b00da82fbd32cf1a26e03d7299ad59eb3f8"
UUID_X = '11111111-1111-4111-8111-111111111111'
UUID_TARGET = '22222222-2222-4222-8222-222222222222'
UUID_DECOY = '33333333-3333-4333-8333-333333333333'


def _history(root, rows=None):
    rows = rows if rows is not None else [('X', UUID_X), ('TARGET', UUID_TARGET),
                                         ('target with spaces', UUID_TARGET), ('DECOY', UUID_DECOY)]
    (root / '.nextflow').mkdir(exist_ok=True)
    (root / '.nextflow/history').write_text(''.join(
        f'2026-01-01 00:00:00\t1s\t{name}\tOK\thash\t{uuid}\tnextflow run main.nf\n'
        for name, uuid in rows))


def _baseline():
    return subprocess.check_output(
        ["git", "show", f"{BASELINE}:RTBioScan.sh"], cwd=ROOT, text=True
    )


CAPTURE = r'''
import base64, json, os, pathlib, sys
root = pathlib.Path.cwd()
args = sys.argv[1:]
with open(os.environ['CAPTURE'], 'a') as out:
    out.write(json.dumps(args) + '\n')
if 'config' in args:
    print("params.targets = 'COI|ITS2'")
    sys.exit(0)
files = {str(p.relative_to(root / 'results')): base64.b64encode(p.read_bytes()).decode()
         for p in (root / 'results').rglob('*')
         if p.is_file() and (p.suffix in ('.json', '.jsonl', '.html') or p.name == 'sentinel')}
pathlib.Path(os.environ['SNAPSHOT']).write_text(json.dumps(files))
sys.exit(int(os.environ.get('RUN_EXIT', '0')))
'''


def _wrapper(tmp_path, history=False):
    assert ROOT not in tmp_path.resolve().parents, "use an external --basetemp"
    root = tmp_path / 'wrapper'
    (root / 'bin' / 'lib').mkdir(parents=True)
    for name in ('stale_lock_utils.sh',):
        shutil.copy2(ROOT / 'bin' / 'lib' / name, root / 'bin' / 'lib' / name)
    for name in ('report_run_json.pl', 'report_run_index_update.sh', 'report_render.py'):
        shutil.copy2(ROOT / 'bin' / name, root / 'bin' / name)
    shutil.copytree(ROOT / 'assets', root / 'assets')
    (root / 'bin' / 'Metadata_pod5_processing.sh').write_text(
        '#!/bin/bash\necho started > "$FEEDER_CAPTURE"\nexec sleep 120\n')
    # No socket: record wait-mode arguments, then simulate a server startup failure.
    (root / 'bin' / 'serve_report.sh').write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$SERVER_CAPTURE"\nexit 1\n')
    shim = tmp_path / 'shim'
    shim.mkdir()
    (shim / 'nextflow').write_text(f'#!{sys.executable}\n' + CAPTURE)
    (shim / 'nextflow').chmod(0o755)
    # Fix the seed timestamp so byte comparisons do not mask report differences.
    (shim / 'date').write_text('#!/bin/bash\nprintf "2026-01-01T00:00:00Z\\n"\n')
    (shim / 'date').chmod(0o755)
    (shim / 'python3').write_text(f'#!{sys.executable}\n' + r'''import datetime, os, runpy, sys
if len(sys.argv) > 1 and sys.argv[1].endswith('/report_render.py'):
    class FrozenDatetime(datetime.datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 1, 1)
    datetime.datetime = FrozenDatetime
    sys.argv = sys.argv[1:]
    runpy.run_path(sys.argv[0], run_name='__main__')
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
''')
    (shim / 'python3').chmod(0o755)
    caller = tmp_path / 'caller'
    caller.mkdir()
    _history(caller, [('X', UUID_DECOY), ('TARGET', UUID_DECOY)])
    if history:
        _history(root)
    return root, {**os.environ, 'CALLER': str(caller),
                  'PERL_HASH_SEED': '0', 'PERL_PERTURB_KEYS': '0',
                  'FEEDER_CAPTURE': str(tmp_path / 'feeder-started'), 'PATH': f'{shim}:{os.environ["PATH"]}',
                  'HOME': str(tmp_path / 'home'), 'TMPDIR': str(tmp_path),
                  'PYTHONDONTWRITEBYTECODE': '1', 'CAPTURE': str(tmp_path / 'argv.jsonl'),
                  'SNAPSHOT': str(tmp_path / 'snapshot.json'),
                  'SERVER_CAPTURE': str(tmp_path / 'server-args.txt')}


def _launch(root, env, args, *, source=None, exit_code=0, failure=None):
    shutil.rmtree(root / 'results', ignore_errors=True)
    sentinel = root / 'results/ongoing/single_exp/sentinel'
    sentinel.parent.mkdir(parents=True)
    sentinel.write_text('preserve cached-round workspace\n')
    (root / 'RTBioScan.sh').write_text(source if source is not None else (ROOT / 'RTBioScan.sh').read_text())
    Path(env['CAPTURE']).unlink(missing_ok=True)
    Path(env['SNAPSHOT']).unlink(missing_ok=True)
    result = subprocess.run(['/bin/bash', str(root / 'RTBioScan.sh'), *args],
                            cwd=env['CALLER'], env={**env, 'RUN_EXIT': str(exit_code)},
                            capture_output=True, text=True, timeout=30)
    if failure is not None:
        assert result.returncode != 0, result.stdout + result.stderr
        assert failure in result.stderr
        for key in ('CAPTURE', 'SNAPSHOT', 'SERVER_CAPTURE', 'FEEDER_CAPTURE'):
            assert not Path(env[key]).exists(), f'{key}: startup occurred after invalid resume'
        assert sentinel.exists()
        assert not (root / 'results/report_html').exists()
        return result
    assert result.returncode == exit_code, result.stdout + result.stderr
    calls = [json.loads(line) for line in Path(env['CAPTURE']).read_text().splitlines()]
    snapshot = json.loads(Path(env['SNAPSHOT']).read_text())
    return calls, snapshot


def _paths(run='X'):
    return ['--run_id', run, '--reads', f'results/pod5/{run}/reads_rt_round_pod5/*pod5',
            '--ori_dir', f'results/pod5/{run}/ori_round_pod5/',
            '--indexes', f'results/sample_info/{run}/demult.fasta',
            '--primer_indexes', f'results/sample_info/{run}/primers.fasta']


# Explicit independent expected arrays: these are not generated by a parser.
CASES = [
    ('new', [], ['-name', 'X']),
    ('bare', ['-resume'], ['-resume', UUID_X, '--state_id', 'X']),
    ('target', ['-resume', 'TARGET'], ['-resume', UUID_TARGET, '--state_id', 'X']),
    ('uuid', ['-resume', UUID_TARGET], ['-resume', UUID_TARGET, '--state_id', 'X']),
    ('last', ['-resume', 'last'], ['-resume', 'last', '--state_id', 'X']),
    ('name_bare', ['-name', 'NEW', '-resume'], ['-name', 'NEW', '-resume', '--state_id', 'X']),
    ('name_target', ['-name', 'NEW', '-resume', 'TARGET'], ['-name', 'NEW', '-resume', UUID_TARGET, '--state_id', 'X']),
    ('state', ['-resume', '--state_id', 'STATE'], ['-resume', UUID_X, '--state_id', 'STATE']),
    ('attached_state', ['-resume', '--state_id=STATE'], ['-resume', UUID_X, '--state_id=STATE']),
    ('name_after', ['-resume', '-name', 'NEW'], ['-resume', '-name', 'NEW', '--state_id', 'X']),
    ('spaces', ['--outdir', 'results with spaces', '-resume', 'target with spaces', '--state_id', 'state with spaces'],
     ['--outdir', 'results with spaces', '-resume', UUID_TARGET, '--state_id', 'state with spaces']),
    ('legacy_double', ['--resume'], ['--resume', '-name', 'X']),
    ('legacy_attached_double', ['--resume=TARGET'], ['--resume=TARGET', '-name', 'X']),
    ('attached_name', ['-name=NEW', '-resume'], ['-name=NEW', '-resume', '--state_id', 'X']),
]


@pytest.mark.parametrize('label,args,expected', CASES, ids=[c[0] for c in CASES])
def test_wrapper_argv(tmp_path, label, args, expected):
    root, env = _wrapper(tmp_path, history='-resume' in args)
    calls, snapshot = _launch(root, env, ['--run_id', 'X', *args])
    assert calls == [['run', 'main.nf', *expected, *_paths()]]
    resumed = label not in ('new', 'legacy_double', 'legacy_attached_double')
    assert ('ongoing/single_exp/sentinel' in snapshot) == resumed


@pytest.mark.parametrize('args', [[], ['-resume'], ['-resume', 'TARGET'],
                                   ['-name', 'NEW', '-resume', '--state_id=STATE'],
                                   ['--reads', 'reads with spaces/*pod5', '--resume=TARGET']])
def test_no_run_id_forwarding(tmp_path, args):
    root, env = _wrapper(tmp_path)
    assert _launch(root, env, args)[0] == [['run', 'main.nf', *args]]
    assert _launch(root, env, args, source=_baseline())[0] == [['run', 'main.nf', *args]]


@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('override', [False, True])
def test_config_feeder_watch_paths(tmp_path, resume, override):
    root, env = _wrapper(tmp_path, history=resume)
    config = ['-C', 'base config', '-c', 'extra config', '-config-ignore-includes']
    pipeline = ['-profile', 'test,voucher', '--outdir', 'output with spaces']
    overrides = ['--watch', 'false', '--reads', 'my reads/*pod5', '--ori_dir', 'my originals/',
                 '--indexes', 'my demult.fasta', '--primer_indexes', 'my primers.fasta'] if override else []
    tail = ['--run_id', 'X'] if override else [*_paths(), '--watch', 'true']
    args = ['--run_id', 'X', '--feeder', '--skip_pod5', *config, *pipeline, *overrides]
    if resume:
        args += ['-resume']
    identity = ['-resume', UUID_X, '--state_id', 'X'] if resume else ['-name', 'X']
    calls, _ = _launch(root, env, args)
    assert calls == [[*config, 'config', '-flat', '-profile', 'test,voucher', 'main.nf'],
                     [*config, 'run', 'main.nf', *pipeline, *overrides, *identity, *tail]]
    if not resume:
        assert _launch(root, env, args, source=_baseline())[0] == calls


@pytest.mark.parametrize('extra', [[], ['-name', 'NEW'], ['--state_id=STATE'],
                                    ['--delete_input_pod5', '--delete_from_ori_dir'],
                                    ['--primers_fasta', 'primer file.fasta']])
def test_nonresume_byte_equivalence(tmp_path, extra):
    root, env = _wrapper(tmp_path)
    args = ['--run_id', 'X', *extra]
    candidate = _launch(root, env, args)[0]
    baseline = _launch(root, env, args, source=_baseline())[0]
    assert candidate == baseline
    assert b'\0'.join(a.encode() for a in candidate[0]) == b'\0'.join(a.encode() for a in baseline[0])


@pytest.mark.parametrize('extra,seed_name', [(['-resume'], None),
    (['-resume', 'TARGET'], None), (['-resume', UUID_TARGET], None),
    (['-name', 'NEW', '-resume'], 'NEW'), ([], 'X')])
@pytest.mark.parametrize('exit_code', [0, 17])
def test_initial_seed(tmp_path, extra, seed_name, exit_code):
    root, env = _wrapper(tmp_path, history='-resume' in extra)
    args = ['--run_id', 'X', '--serve', '--serve-port', '8123', *extra]
    _, snapshot = _launch(root, env, args, exit_code=exit_code)
    report_files = {k: v for k, v in snapshot.items() if k.startswith('report_html/')}
    assert '--wait-for-report' in Path(env['SERVER_CAPTURE']).read_text().splitlines()
    if seed_name is None:
        assert report_files == {}, 'implicit-name resume must not guess any report identity'
        assert not (root / 'results/report_html/runs/X').exists()
        index = root / 'results/report_html/runs_index.jsonl'
        assert not index.exists() or index.read_bytes() == b''
    else:
        report = json.loads(base64.b64decode(report_files[f'report_html/runs/{seed_name}/run_report.json']))
        rows = [json.loads(line) for line in base64.b64decode(report_files['report_html/runs_index.jsonl']).splitlines()]
        assert report['run_id'] == seed_name
        assert report['state_id'] == 'X'
        assert [row['run_id'] for row in rows] == [seed_name]
        if not extra:
            _, baseline = _launch(root, env, args, source=_baseline(), exit_code=exit_code)
            assert _report_contract(baseline) == _report_contract(snapshot)



def _report_contract(snapshot):
    # Timestamps are frozen in date/renderer stubs. Parse complete JSON records
    # to ignore only JSON object key order; compare the complete HTML bytes.
    result = {}
    for name, encoded in snapshot.items():
        if not name.startswith('report_html/'):
            continue
        payload = base64.b64decode(encoded)
        if name.endswith('.json'):
            result[name] = json.loads(payload)
        elif name.endswith('.jsonl'):
            result[name] = [json.loads(line) for line in payload.splitlines()]
        else:
            result[name] = payload
    return result


@pytest.mark.parametrize('rows,diagnostic', [
    ([], 'no session UUID'),
    ([('PREFIX_X', UUID_X), ('X_suffix', UUID_X)], 'no session UUID'),
    ([('X', UUID_X), ('X', UUID_TARGET)], 'ambiguous resume name'),
    ([('X', '')], 'no session UUID'),
    ([('X', 'not-a-uuid')], 'malformed session UUID'),
    ([('X', UUID_X[:-1])], 'malformed session UUID'),
    ([('X', UUID_X + '0')], 'malformed session UUID'),
    ([('X', UUID_X), ('X', 'broken')], 'malformed session UUID'),
])
def test_invalid_history_fails_before_startup(tmp_path, rows, diagnostic):
    root, env = _wrapper(tmp_path)
    _history(root, rows)
    _launch(root, env, ['--run_id', 'X', '--feeder', '--skip_pod5', '--serve', '-resume'], failure=diagnostic)


def test_missing_history_fails_before_startup(tmp_path):
    root, env = _wrapper(tmp_path)
    _launch(root, env, ['--run_id', 'X', '--serve', '-resume'], failure='cannot read')


@pytest.mark.parametrize('target', ['MISSING', 'PREFIX', UUID_X[:-1]])
def test_unresolved_explicit_target_fails(tmp_path, target):
    root, env = _wrapper(tmp_path, history=True)
    _launch(root, env, ['--run_id', 'X', '-name', 'NEW', '-resume', target], failure='no session UUID')


def test_repeated_identical_and_empty_history_rows(tmp_path):
    root, env = _wrapper(tmp_path)
    _history(root, [('X', ''), ('X', UUID_X), ('X', UUID_X), ('DECOY', UUID_DECOY)])
    calls, _ = _launch(root, env, ['--run_id', 'X', '-resume'])
    assert calls == [['run', 'main.nf', '-resume', UUID_X, '--state_id', 'X', *_paths()]]


def test_attached_resume_rejected_before_startup(tmp_path):
    root, env = _wrapper(tmp_path, history=True)
    _launch(root, env, ['--run_id', 'X', '--feeder', '--skip_pod5', '--serve', '-resume=TARGET'],
            failure='use the separate-token form: -resume TARGET')


@pytest.mark.parametrize('arg', ['--resume', '--resume=TARGET'])
def test_double_dash_does_not_skip_name_preflight(tmp_path, arg):
    root, env = _wrapper(tmp_path, history=True)
    _launch(root, env, ['--run_id', 'X', arg], failure="run name 'X' already exists")


@pytest.mark.parametrize('extra', [['-name', 'NEW', '-resume'], ['-resume', 'last'], ['-resume', UUID_TARGET]])
def test_explicit_latest_or_uuid_needs_no_history(tmp_path, extra):
    root, env = _wrapper(tmp_path)
    calls, snapshot = _launch(root, env, ['--run_id', 'X', *extra])
    assert calls == [['run', 'main.nf', *extra, '--state_id', 'X', *_paths()]]
    assert 'ongoing/single_exp/sentinel' in snapshot

def _runtime():
    capsule = Path(os.environ.get('RTB_NXF_CACHE', Path.home() / '.nextflow')) / 'capsule/apps/nextflow-all_22.10.8'
    if not (capsule / 'nextflow-22.10.8.jar').is_file():
        pytest.fail(f'cached 22.10.8 runtime required: {capsule}')
    packages = ('java.lang', 'java.io', 'java.nio', 'java.net', 'java.util',
                'java.util.concurrent.locks', 'java.util.concurrent.atomic', 'java.nio.file.spi',
                'sun.nio.ch', 'sun.nio.fs', 'sun.net.www.protocol.http', 'sun.net.www.protocol.https',
                'sun.net.www.protocol.ftp', 'sun.net.www.protocol.file', 'jdk.internal.misc', 'java.util.regex')
    return ['java', *[f'--add-opens=java.base/{p}=ALL-UNNAMED' for p in packages],
            '-cp', str(capsule / '*'), 'nextflow.cli.Launcher']


@pytest.mark.skipif(os.environ.get('RTB_RUN_REAL_NXF') != '1', reason='opt-in cached offline Nextflow contract')
def test_real_nextflow_resume_contract(tmp_path):
    root, env = _wrapper(tmp_path)
    (tmp_path / 'shim/date').unlink()  # Real task timing needs the real date command.
    command = _runtime()
    env.update({'NXF_OFFLINE': 'true', 'NXF_VER': '22.10.8', 'NXF_ANSI_LOG': 'false',
                'NXF_DISABLE_CHECK_LATEST': 'true', 'NXF_HOME': str(tmp_path / 'nxf-home'),
                'NXF_TEMP': str(tmp_path / 'nxf-temp'), 'RTB_EXEC_LOG': str(root / 'executed.log')})
    (tmp_path / 'nxf-temp').mkdir()
    # Exercise the entire production wrapper and its resolver with the real CLI.
    (root / 'RTBioScan.sh').write_text((ROOT / 'RTBioScan.sh').read_text())
    (tmp_path / 'shim/nextflow').write_text(
        f'#!{sys.executable}\nimport os, sys\nos.execvp({command[0]!r}, {command!r} + sys.argv[1:])\n')
    (root / 'main.nf').write_text(r'''nextflow.enable.dsl=1
params.state_id = 'default'
process PROBE {
    output:
    file 'result.txt' into result
    script:
    """
    echo executed >> "\$RTB_EXEC_LOG"
    echo stable > result.txt
    """
}
workflow.onComplete {
    new File("${workflow.launchDir}/identity.json").text = groovy.json.JsonOutput.toJson([
        session: workflow.sessionId.toString(), name: workflow.runName,
        state: params.state_id, resumed: workflow.resume])
}
''')
    (root / 'nextflow.config').write_text("trace.fields = 'task_id,name,status,hash,workdir'\n")
    evidence = {}

    def run(label, args, *, wrapper=False, success=True):
        if wrapper:
            argv = ['/bin/bash', str(root / 'RTBioScan.sh'), *args]
            cwd = env['CALLER']
        else:
            argv = [*command, *args]
            cwd = root
        result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=90)
        observation = {'argv': argv, 'exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}
        evidence[label] = observation
        if result.returncode == 0 and (root / f'{label}.tsv').exists():
            observation['identity'] = json.loads((root / 'identity.json').read_text())
            with (root / f'{label}.tsv').open() as handle:
                observation['tasks'] = list(csv.DictReader(handle, delimiter='\t'))
            observation['history'] = (root / '.nextflow/history').read_text()
        (tmp_path / 'evidence.json').write_text(json.dumps(evidence, indent=2))
        if success:
            assert result.returncode == 0, observation
        return observation

    def direct(label, *args, success=True):
        return run(label, ['run', 'main.nf', '-with-trace', f'{label}.tsv', *args], success=success)

    def wrapper(label, *args):
        return run(label, ['--run_id', 'X', '-with-trace', f'{label}.tsv', *args], wrapper=True)

    version = run('version', ['-version'])
    assert '22.10.8' in version['stdout']
    original = direct('initial', '-name', 'X', '--state_id', 'X')
    decoy = direct('decoy', '-name', 'DECOY', '--state_id', 'DECOY')
    assert original['identity']['name'] == 'X' and not original['identity']['resumed']
    assert original['tasks'][0]['status'] == 'COMPLETED'
    assert decoy['identity']['session'] != original['identity']['session']
    assert decoy['tasks'][0]['workdir'] != original['tasks'][0]['workdir']
    old = direct('old_shape', '-name', 'X', '-resume', success=False)
    assert old['exit'] != 0 and 'already used' in old['stdout'] + old['stderr']
    unsafe = direct('unsafe_positional_name', '-resume', 'X')
    assert unsafe['identity']['session'] == decoy['identity']['session']
    assert unsafe['tasks'][0]['hash'] == decoy['tasks'][0]['hash']
    _history(Path(env['CALLER']), [('X', decoy['identity']['session'])])
    targeted = wrapper('targeted', '-resume')
    explicit = wrapper('explicit', '-name', 'NEW', '-resume', 'X', '--state_id', 'OVERRIDE')
    for observed, state in [(targeted, 'X'), (explicit, 'OVERRIDE')]:
        identity = observed['identity']
        assert identity['session'] == original['identity']['session'] and identity['resumed']
        assert identity['state'] == state
        assert identity['name'] not in ('X', 'DECOY', unsafe['identity']['name'])
        task = observed['tasks'][0]
        assert task['status'] == 'CACHED'
        assert task['hash'] == original['tasks'][0]['hash'] != decoy['tasks'][0]['hash']
        assert task['workdir'] == original['tasks'][0]['workdir'] != decoy['tasks'][0]['workdir']
    assert explicit['identity']['name'] == 'NEW'
    assert (root / 'executed.log').read_text().splitlines() == ['executed', 'executed']
    latest = direct('latest', '-name', 'LATER_DECOY')
    bare = wrapper('bare_explicit', '-name', 'LATEST_NEW', '-resume')
    assert bare['identity']['name'] == 'LATEST_NEW'
    assert bare['identity']['session'] == latest['identity']['session'] != original['identity']['session']
    assert bare['tasks'][0]['status'] == 'CACHED'
    attached = direct('attached', '-resume=X', success=False)
    assert attached['exit'] != 0 and 'Unknown option' in attached['stderr']
    for label, arg in [('double_resume', '--resume'), ('double_attached', '--resume=X')]:
        observed = direct(label, arg)
        assert not observed['identity']['resumed']
        assert observed['identity']['session'] not in (original['identity']['session'], latest['identity']['session'])
        assert observed['tasks'][0]['status'] == 'COMPLETED'
