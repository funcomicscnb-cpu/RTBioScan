import os
import json
import time
from pathlib import Path
import re
import shlex
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MAIN_NF = REPO_ROOT / "main.nf"
STALE_LOCK_UTILS = REPO_ROOT / "bin" / "lib" / "stale_lock_utils.sh"


def _render_async_shell(tmp_path: Path) -> tuple[str, dict[str, Path]]:
    text = MAIN_NF.read_text(encoding="utf-8")
    process_text = text.split("process async_report_render {", 1)[1].split(
        "workflow.onComplete", 1
    )[0]
    shell = process_text.split('"""', 2)[1]

    base_dir = tmp_path / "stub-base"
    outdir = tmp_path / "out"
    ongoing_state = outdir / "temp" / "ongoing" / "state" / "STATE"
    current_state = outdir / "current" / "state" / "STATE"
    request = tmp_path / "report_render.request"
    replacements = {
        "${baseDir}": str(base_dir),
        "${barcode}": "B1",
        "${htmlReportEnabled ? 1 : 0}": "1",
        "${currentResultsStateDir}": str(current_state),
        "${htmlReportAutoRefresh ? 1 : 0}": "0",
        "${htmlReportRefreshSecondsStr}": "15",
        "${htmlReportSamplePlotMaxStr}": "10",
        "${htmlReportUrlPrefix}": "",
        "${ongoingStateDir}": str(ongoing_state),
        "${params.lock_wait_seconds}": "2",
        "${outdirResolved}": str(outdir),
        "${render_request_file}": str(request),
        "${replicateModeCanonical}": "track",
        "${round_barcode}": "round-001",
        "${run_name}": "run-001",
        "${staleLockTtlMinutesStr}": "1",
        "${stateId}": "STATE",
    }
    for source, value in replacements.items():
        shell = shell.replace(source, value)
    assert not re.search(r"(?<!\\)\$\{[^}]+\}", shell)
    shell = shell.replace(r"\$", "$")

    return shell, {
        "base_dir": base_dir,
        "outdir": outdir,
        "ongoing_state": ongoing_state,
        "current_state": current_state,
        "request": request,
        "run_dir": outdir / "report_html" / "runs" / "run-001",
    }


def _stage_report_fixture(tmp_path: Path) -> tuple[str, dict[str, Path], Path]:
    shell, paths = _render_async_shell(tmp_path)
    # Stage real production helpers; only the existing renderer failure injector is wrapped.
    shutil.copytree(REPO_ROOT / 'bin', paths['base_dir'] / 'bin')
    shutil.copytree(REPO_ROOT / 'assets', paths['base_dir'] / 'assets')
    rebuild = paths["base_dir"] / "bin" / "report_rebuild.sh"
    rebuild.parent.mkdir(parents=True, exist_ok=True)
    rebuild.rename(rebuild.with_name('report_rebuild-real.sh'))
    rebuild.write_text(
        """#!/usr/bin/env bash
set -u
original=("$@")
view=root
while [ "$#" -gt 0 ]; do
    case "$1" in
        --group-view)
            view="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done
printf 'start:%s\n' "$view" >> "$REPORT_STUB_LOG"
case "$view" in
    sample) rc="$REPORT_SAMPLE_RC" ;;
    replicate) rc="$REPORT_REPLICATE_RC" ;;
    track_detail) rc="$REPORT_TRACK_DETAIL_RC" ;;
    *) rc=0 ;;
esac
printf 'done:%s\n' "$view" >> "$REPORT_STUB_LOG"
[ "$rc" -eq 0 ] || exit "$rc"
exec /bin/bash "$(dirname "$0")/report_rebuild-real.sh" "${original[@]}"

""",
        encoding="utf-8",
    )
    rebuild.chmod(0o755)

    state_tmp = paths["ongoing_state"] / "_state"
    state_tmp.mkdir(parents=True)
    row = json.dumps(dict(schema_version='2.1', state_id='STATE', run_id='run-001',
                          barcode='B1', round_barcode='round-001')) + '\n'
    (state_tmp / 'report_history.jsonl').write_text(row, encoding='utf-8')
    (state_tmp / 'round_index.tsv').write_text('round-001\t1\n')
    retained = paths['ongoing_state'] / 'round-001'
    retained.mkdir()
    (retained / 'round_report.json').write_text(row, encoding='utf-8')
    paths["request"].write_text(
        "render=1\nround_barcode=round-001\n", encoding="utf-8"
    )
    paths["run_dir"].mkdir(parents=True)
    (paths["run_dir"] / ".report_render_pending").write_text(
        "queued:round-001\n", encoding="utf-8"
    )
    figures = paths["run_dir"] / "figures"
    figures.mkdir()
    (figures / "probe.tsv").write_text("probe\n", encoding="utf-8")

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    script = work_dir / "async-report-render.sh"
    script.write_text(shell, encoding="utf-8")
    return shell, paths, script


@pytest.mark.parametrize("with_contention", [False, True])
def test_async_report_renderer_failure_is_nonfatal_and_keeps_pending(
    tmp_path: Path, with_contention: bool
) -> None:
    _, paths, script = _stage_report_fixture(tmp_path)
    state_tmp = paths["ongoing_state"] / "_state"
    render_lock = state_tmp / ".report_render.lock.lockdir"
    if with_contention:
        render_lock.mkdir()
        hostname = subprocess.run(
            ["hostname"], capture_output=True, text=True, check=True
        ).stdout.strip()
        (render_lock / "meta.env").write_text(
            f"pid=999999999\nhost={hostname}\nstarted_epoch=1\n",
            encoding="utf-8",
        )

    stub_log = tmp_path / "report-stub.log"
    env = {
        **os.environ,
        "REPORT_STUB_LOG": str(stub_log),
        "REPORT_SAMPLE_RC": "7",
        "REPORT_REPLICATE_RC": "0",
        "REPORT_TRACK_DETAIL_RC": "9",
    }
    result = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", str(script)],
        cwd=script.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert (
        "WARN: async report_rebuild.sh failed for round=round-001 "
        "sample_rc=7 replicate_rc=0 track_detail_rc=9"
    ) in result.stderr
    if with_contention:
        assert "WARN: reclaiming stale report lock" in result.stderr
    lines = stub_log.read_text(encoding="utf-8").splitlines()
    assert lines.count("start:sample") == 2  # one automatic pending-only retry
    assert lines.count("start:replicate") == lines.count("start:track_detail") == 1
    assert "REPORT_HISTORY_PENDING" in result.stderr
    assert (paths["run_dir"] / ".report_history_pending").is_file()
    assert set(lines) == {
        "start:sample",
        "done:sample",
        "start:replicate",
        "done:replicate",
        "start:track_detail",
        "done:track_detail",
    }
    assert (paths["run_dir"] / ".report_render_pending").is_file()
    assert (paths["run_dir"] / "figures" / "probe.tsv").is_file()
    assert not (
        paths["current_state"] / "tables" / "to_figures" / "embedded" / "probe.tsv"
    ).exists()
    assert not render_lock.exists()
    assert not (state_tmp / ".report_history.lock.lockdir").exists()
    assert not (paths["outdir"] / ".report_root_render.lock.lockdir").exists()


@pytest.mark.parametrize("errexit_enabled", [False, True])
def test_async_report_lock_contention_preserves_errexit_state(
    tmp_path: Path, errexit_enabled: bool
) -> None:
    shell, _ = _render_async_shell(tmp_path)
    helper_start = shell.index("remove_report_lock_if_stale() {")
    helper_end = shell.index("release_lock_dir() {")
    helper_functions = shell[helper_start:helper_end]

    lock_path = tmp_path / "option-state-lock"
    lock_dir = Path(f"{lock_path}.lockdir")
    lock_dir.mkdir()
    hostname = subprocess.run(
        ["hostname"], capture_output=True, text=True, check=True
    ).stdout.strip()
    (lock_dir / "meta.env").write_text(
        f"pid=999999999\nhost={hostname}\nstarted_epoch=1\n", encoding="utf-8"
    )
    harness = tmp_path / "option-state.sh"
    harness.write_text(
        "\n".join(
            (
                f"source {shlex.quote(str(STALE_LOCK_UTILS))}",
                'REPORT_LOCK_HOST="$(hostname 2>/dev/null || uname -n 2>/dev/null || echo unknown)"',
                "REPORT_LOCK_STALE_TTL_SECONDS=60",
                'REPORT_STALE_LOCK_DIR=""',
                helper_functions,
                'before_flags="$-"',
                f"if ! acquire_lock_dir_wait {shlex.quote(str(lock_path))} 2 test-lock; then exit 90; fi",
                'after_flags="$-"',
                f"rm -f {shlex.quote(str(lock_dir / 'meta.env'))}",
                f"rmdir {shlex.quote(str(lock_dir))}",
                'printf "%s\\n%s\\n" "$before_flags" "$after_flags"',
            )
        )
        + "\n",
        encoding="utf-8",
    )
    options = "-euo" if errexit_enabled else "-uo"
    result = subprocess.run(
        ["/bin/bash", options, "pipefail", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    before, after = result.stdout.splitlines()
    assert before == after
    assert ("e" in after) is errexit_enabled

@pytest.mark.parametrize('pending_first', [False, True])
def test_async_real_publication_rechecks_revision_and_retries_only_pending(tmp_path, pending_first):
    _, paths, script = _stage_report_fixture(tmp_path)
    wrapper=paths['base_dir']/'bin/report_rebuild.sh'
    source=wrapper.read_text()
    source=source.replace('exit "$rc"', 'exit "$rc"')
    source=source.replace('[ "$rc" -eq 0 ] || exit "$rc"', '''if [ "$view" = sample ] && [ "${REPORT_FAIL_ONCE:-0}" = 1 ] && [ ! -f "$REPORT_STUB_LOG.once" ]; then
    : > "$REPORT_STUB_LOG.once"
    exit 7
fi
[ "$rc" -eq 0 ] || exit "$rc"''')
    wrapper.write_text(source)
    log=tmp_path/'calls'
    env={**os.environ,'REPORT_STUB_LOG':str(log),'REPORT_SAMPLE_RC':'0','REPORT_REPLICATE_RC':'0',
         'REPORT_TRACK_DETAIL_RC':'0','REPORT_FAIL_ONCE':str(int(pending_first))}
    result=subprocess.run(['/bin/bash','-euo','pipefail',str(script)],cwd=script.parent,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    lines=log.read_text().splitlines()
    assert lines.count('start:sample')==1+int(pending_first)
    assert result.stdout.count('REPORT_HISTORY_COMPLETE')==int(pending_first)
    assert not (paths['run_dir']/'.report_history_pending').exists()
    assert not (paths['run_dir']/'.report_render_pending').exists()
    report=json.loads((paths['run_dir']/'report_state.json').read_text())
    import hashlib
    assert report['report_revision']==hashlib.sha256((paths['ongoing_state']/'_state/report_history.jsonl').read_bytes()).hexdigest()


def test_async_revision_change_never_clears_pending(tmp_path):
    _, paths, script=_stage_report_fixture(tmp_path)
    wrapper=paths['base_dir']/'bin/report_rebuild.sh'
    source=wrapper.read_text().replace('exec /bin/bash', '/bin/bash')
    source += '\nif [ "$view" = root ]; then printf "bad index\\n" >> "$REPORT_TEST_INDEX"; fi\n'
    wrapper.write_text(source)
    env={**os.environ,'REPORT_STUB_LOG':str(tmp_path/'calls'),'REPORT_SAMPLE_RC':'0','REPORT_REPLICATE_RC':'0',
         'REPORT_TRACK_DETAIL_RC':'0','REPORT_TEST_INDEX':str(paths['ongoing_state']/'_state/round_index.tsv')}
    result=subprocess.run(['/bin/bash','-euo','pipefail',str(script)],cwd=script.parent,env=env,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert 'REPORT_HISTORY_PENDING' in result.stderr
    assert 'REPORT_HISTORY_COMPLETE' not in result.stdout
    assert (paths['run_dir']/'.report_history_pending').is_file()


def test_async_healthy_current_round_finalizes_later_retained_round(tmp_path):
    _, paths, script = _stage_report_fixture(tmp_path)
    state = paths['ongoing_state']
    later = state / 'round-002'
    later.mkdir()
    row = json.dumps(dict(schema_version='2.1', state_id='STATE', run_id='run-001',
                          barcode='B1', round_barcode='round-002')) + '\n'
    (later / 'round_report.json').write_text(row)
    (state / '_state/round_index.tsv').write_text('round-001\t1\nround-002\t2\n')
    log = tmp_path / 'calls'
    env = {**os.environ, 'REPORT_STUB_LOG': str(log), 'REPORT_SAMPLE_RC': '0',
           'REPORT_REPLICATE_RC': '0', 'REPORT_TRACK_DETAIL_RC': '0'}
    result = subprocess.run(['/bin/bash', '-euo', 'pipefail', str(script)], cwd=script.parent,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('REPORT_HISTORY_COMPLETE') == 1
    # The run-wide gate prevents the stale first render; the finalizer renders once.
    assert log.read_text().splitlines().count('start:sample') == 1
    history = (state / '_state/report_history.jsonl').read_bytes()
    assert [json.loads(x)['round_barcode'] for x in history.split(b'\n') if x] == ['round-001', 'round-002']
    index = [json.loads(x) for x in (paths['outdir'] / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if x]
    assert len(index) == 1 and index[0]['rounds_count'] == 2
    assert not (paths['run_dir'] / '.report_history_pending').exists()


def test_async_foreign_retained_round_does_not_request_finalizer(tmp_path):
    _, paths, script = _stage_report_fixture(tmp_path)
    state = paths['ongoing_state']
    later = state / 'round-002'; later.mkdir()
    row = json.dumps(dict(schema_version='2.1', state_id='STATE', run_id='other-run',
                          barcode='B1', round_barcode='round-002')) + '\n'
    (later / 'round_report.json').write_text(row)
    (state / '_state/round_index.tsv').write_text('round-001\t1\nround-002\t2\n')
    log = tmp_path / 'calls'
    env = {**os.environ, 'REPORT_STUB_LOG': str(log), 'REPORT_SAMPLE_RC': '0',
           'REPORT_REPLICATE_RC': '0', 'REPORT_TRACK_DETAIL_RC': '0'}
    result = subprocess.run(['/bin/bash', '-euo', 'pipefail', str(script)], cwd=script.parent,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert 'REPORT_HISTORY_COMPLETE' not in result.stdout
    assert log.read_text().splitlines().count('start:sample') == 1
    history = (state / '_state/report_history.jsonl').read_bytes()
    assert [json.loads(x)['round_barcode'] for x in history.split(b'\n') if x] == ['round-001']
    index = [json.loads(x) for x in (paths['outdir'] / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if x]
    assert len(index) == 1 and index[0]['rounds_count'] == 1
    assert not (paths['run_dir'] / '.report_history_pending').exists()


def test_async_pending_finalizer_uses_other_barcode_identity(tmp_path):
    _, paths, script = _stage_report_fixture(tmp_path)
    state = paths['ongoing_state']
    later = state / 'round-002'; later.mkdir()
    row = json.dumps(dict(schema_version='2.1', state_id='STATE', run_id='run-001',
                          barcode='B2', round_barcode='round-002')) + '\n'
    (later / 'round_report.json').write_text(row)
    (state / '_state/round_index.tsv').write_text('round-001\t1\nround-002\t2\n')
    paths['request'].write_text('render=0\nround_barcode=round-002\nrun_id=run-001\nbarcode=B2\nfinalize=1\n')
    log = tmp_path / 'calls'
    env = {**os.environ, 'REPORT_STUB_LOG': str(log), 'REPORT_SAMPLE_RC': '0',
           'REPORT_REPLICATE_RC': '0', 'REPORT_TRACK_DETAIL_RC': '0'}
    result = subprocess.run(['/bin/bash', '-euo', 'pipefail', str(script)], cwd=script.parent,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count('REPORT_HISTORY_COMPLETE') == 1
    history = (state / '_state/report_history.jsonl').read_bytes()
    assert [json.loads(line)['barcode'] for line in history.split(b'\n') if line] == ['B1', 'B2']
    report = json.loads((paths['run_dir'] / 'run_report.json').read_text())
    assert report['rounds_count'] == 2
    index = [json.loads(line) for line in (paths['outdir'] / 'report_html/runs_index.jsonl').read_bytes().split(b'\n') if line]
    assert len(index) == 1 and index[0]['rounds_count'] == 2
    assert '"barcode":"B2"' in (paths['run_dir'] / 'report.html').read_text()
    assert not (paths['run_dir'] / '.report_history_pending').exists()


def test_async_two_pending_barcodes_repair_one_per_invocation(tmp_path):
    _, paths, script = _stage_report_fixture(tmp_path)
    state = paths['ongoing_state']
    for rb, number, barcode in [('round-002', 2, 'B2'), ('round-003', 3, 'B3')]:
        directory = state / rb; directory.mkdir()
        row = json.dumps(dict(schema_version='2.1', state_id='STATE', run_id='run-001',
                              barcode=barcode, round_barcode=rb)) + '\n'
        (directory / 'round_report.json').write_text(row)
    (state / '_state/round_index.tsv').write_text('round-001\t1\nround-002\t2\nround-003\t3\n')
    paths['request'].write_text('render=0\nround_barcode=round-002\nrun_id=run-001\nbarcode=B2\nfinalize=1\n')
    log = tmp_path / 'calls'
    env = {**os.environ, 'REPORT_STUB_LOG': str(log), 'REPORT_SAMPLE_RC': '0',
           'REPORT_REPLICATE_RC': '0', 'REPORT_TRACK_DETAIL_RC': '0'}
    first = subprocess.run(['/bin/bash', '-euo', 'pipefail', str(script)], cwd=script.parent,
                           env=env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0 and first.stdout.count('REPORT_HISTORY_COMPLETE') == 0
    assert 'Offline repair:' in first.stderr
    assert not (paths['run_dir'] / 'run_report.json').exists()
    assert (paths['run_dir'] / '.report_history_pending').exists()
    assert [json.loads(line)['barcode'] for line in
            (state / '_state/report_history.jsonl').read_bytes().split(b'\n') if line] == ['B1', 'B2']
    second = subprocess.run(['/bin/bash', '-euo', 'pipefail', str(script)], cwd=script.parent,
                            env=env, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0 and second.stdout.count('REPORT_HISTORY_COMPLETE') == 1
    assert [json.loads(line)['barcode'] for line in
            (state / '_state/report_history.jsonl').read_bytes().split(b'\n') if line] == ['B1', 'B2', 'B3']
    assert json.loads((paths['run_dir'] / 'run_report.json').read_text())['rounds_count'] == 3
    assert not (paths['run_dir'] / '.report_history_pending').exists()

@pytest.mark.parametrize('pending',[False,True])
def test_final_round_request_runs_one_finalizer_only_when_pending(tmp_path,pending):
    _,paths,script=_stage_report_fixture(tmp_path)
    paths['request'].write_text('render=0\nround_barcode=round-001\n'+('finalize=1\n' if pending else ''))
    history=paths['ongoing_state']/'_state/report_history.jsonl'
    original=history.read_bytes()
    if pending:history.write_bytes(b'')
    log=tmp_path/'calls'
    env={**os.environ,'REPORT_STUB_LOG':str(log),'REPORT_SAMPLE_RC':'0','REPORT_REPLICATE_RC':'0','REPORT_TRACK_DETAIL_RC':'0'}
    for retry in range(2):
        result=subprocess.run(['/bin/bash','-euo','pipefail',str(script)],cwd=script.parent,env=env,capture_output=True,text=True,timeout=20)
        assert result.returncode==0,result.stderr
        assert result.stdout.count('REPORT_HISTORY_COMPLETE')==int(pending)
        assert history.read_bytes()==original
        if pending:
            assert log.read_text().splitlines().count('start:sample')==retry+1
            assert not (paths['run_dir']/'.report_history_pending').exists()
            assert len((paths['outdir']/'report_html/runs_index.jsonl').read_bytes().split(b'\n'))==2
        else:assert not log.exists()
