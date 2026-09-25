"""Offline DSL1 controls: execute extracted production path and finalization code.

All Nextflow launch/work/log files live below pytest's external --basetemp.
Biological process bodies are rendered as text only, never executed.
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEAD = "7b84fd5c6845e18b674d3e1894db233108740d7a"
PROCESSES = ("fast_on_target_detection", "_reporting_hq_demultiplexing",
             "getting_run_summary", "backup_update_and_clean", "async_report_render")
INTERPOLATION = re.compile(r"(?<!\\)\$\{([^{}]+)\}")
PATH_NAMES = {"outdirResolved", "params.outdir", "ongoingStateDir", "currentStateDir",
              "currentResultsStateDir", "ongoingResultsStateDir", "workflow.launchDir",
              "otuPruneSamplesFileValue", "params.ori_dir", "podBaseDir", "sampleInfoDir", "baseDir"}


def _runtime():
    home = Path(os.environ.get("NXF_HOME", Path.home() / ".nextflow"))
    jar = home / "framework/22.10.8/nextflow-22.10.8-one.jar"
    capsule = home / "capsule/apps/nextflow-all_22.10.8"
    java = shutil.which("java")
    if not java or not jar.is_file() or not (capsule / "nextflow-22.10.8.jar").is_file():
        pytest.skip(f"cached Nextflow 22.10.8 unavailable: java={java}, jar={jar}, capsule={capsule}")
    # Match the cached launcher's Java 17 module access (also required for
    # reliable Kryo cache serialization), without writing launcher caches at home.
    packages = ("java.lang", "java.io", "java.nio", "java.net", "java.util",
                "java.util.concurrent.locks", "java.util.concurrent.atomic", "java.nio.file.spi",
                "sun.nio.ch", "sun.nio.fs", "sun.net.www.protocol.http", "sun.net.www.protocol.https",
                "sun.net.www.protocol.ftp", "sun.net.www.protocol.file", "jdk.internal.misc", "java.util.regex")
    opens = [f"--add-opens=java.base/{package}=ALL-UNNAMED" for package in packages]
    return [java, *opens, "-cp", str(capsule / "*"), "nextflow.cli.Launcher"]


def _run(args, cwd, timeout=90):
    env = {**os.environ, "NXF_OFFLINE": "true", "NXF_VER": "22.10.8",
           "NXF_ANSI_LOG": "false", "NXF_DISABLE_CHECK_LATEST": "true",
           "NXF_TEMP": str(cwd.parent / "nxf-temp"), "NXF_HOME": str(cwd.parent / "nxf-home"), "PYTHONDONTWRITEBYTECODE": "1"}
    (cwd.parent / "nxf-temp").mkdir(exist_ok=True)
    result = subprocess.run([*_runtime(), *args], cwd=cwd, env=env,
                            text=True, capture_output=True, timeout=timeout)
    (cwd / "last-run.txt").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _shell(text, process):
    return text.split(f"process {process} {{", 1)[1].split('"""', 2)[1]


def _stub_nonpaths(shell):
    # Keep every real path expression. Other inputs have identical harmless
    # stand-ins in HEAD and candidate; these complete bodies are never executed.
    return INTERPOLATION.sub(lambda m: m[0] if m[1] in PATH_NAMES else "1", shell)


def _preamble(main):
    end = main.index("def failedRoundPlaceholderRoot")
    start = main.index("def outdirRaw =") if "def outdirRaw =" in main else main.index("def ongoingStateDir =")
    rolling = main[start:end]
    cache = re.search(r'(?m)^\s*stateVerificationCacheDir = "[^\n]+', main)[0]
    restart = re.search(r'(?m)^\s*env.OUTDIR = [^\n]+', main)[0]
    trim = re.search(r'(?m)^\s*def otuPruneSamplesFileValue = params.otu_prune_samples_file[^\n]+', main)[0]
    binding = main[main.index("def otuPruneSamplesFileRaw") if "def otuPruneSamplesFileRaw" in main else main.index("def otuPruneSamplesFileValue                 ="):main.index("def otuLockForcePruneMaxFastaMbStr           =")]
    run_id = re.search(r'(?m)^def _runId\s*=.*$', main)[0]
    sample_info = re.search(r'(?m)^def sampleInfoDir\s*=.*$', main)[0]
    # Preserve the existing validator's trim, then evaluate its top-level binding.
    return ("def stateId = 'STATE'\n" + rolling + "\ndef stateVerificationCacheDir\n" + cache
            + "\ndef env = [:]\n" + restart + "\ndef otuRecoveryCfg = {\n" + trim
            + "\n[otuPruneSamplesFileValue: otuPruneSamplesFileValue]\n}()\n" + binding
            + "\n" + run_id + "\n" + sample_info + "\n")


def _prepare(tmp_path, main, config, raw=None, prune="", delete=False, ready=False, launch_name="launch-dir"):
    # Deliberately separate projectDir/baseDir from launchDir.
    launch = tmp_path / launch_name
    project = tmp_path / "project"
    launch.mkdir(parents=True, exist_ok=True)
    project.mkdir(exist_ok=True)
    shutil.copytree(ROOT / "conf", project / "conf", dirs_exist_ok=True)
    (project / "bin/lib").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "bin/lib/retry.sh", project / "bin/lib/retry.sh")
    (project / "nextflow.config").write_text(config + '\nnew File("${launchDir}/artifacts/beforeScript.txt").text = rtbioscanBeforeScript\n')
    (launch / "artifacts").mkdir(exist_ok=True)
    for name in ("spool", "intake", "state", "current", "sample-info"):
        (launch / name).mkdir(exist_ok=True)
    (launch / "intake/processed.pod5").write_text("processed sentinel")
    (launch / "spool/future.pod5").write_text("feeder spool sentinel")
    if ready:
        (launch / "intake/ready.pod5").write_text("feeder ready sentinel")
        (launch / "intake/broken.pod5").symlink_to("absent.pod5")
    (launch / "samples.txt").write_text("LAUNCH_SAMPLE\n")
    (launch / "sample-info/samples.txt").write_text("DEFAULT_SAMPLE\n")
    source = 'nextflow.enable.dsl=1\n' + _preamble(main)
    serialization = main.split('def runConfigLines = params', 1)[1].split('script:', 1)[0]
    source += '\ndef runConfigLines = params' + serialization
    source += '\nnew File("${workflow.launchDir}/artifacts/raw-params.txt").text = runConfigLines\n'
    source += '''
    def podBaseDir = "${workflow.launchDir}/pod"
    new File("${workflow.launchDir}/artifacts/preamble.json").text = groovy.json.JsonOutput.toJson([
      launch: workflow.launchDir.toString(), base: baseDir.toString(), raw: params.outdir,
      ongoing: ongoingStateDir, current: currentStateDir, currentResults: currentResultsStateDir,
      ongoingResults: ongoingResultsStateDir, restart: env.OUTDIR, cache: stateVerificationCacheDir,
      prune: otuPruneSamplesFileValue])
    '''
    for process in PROCESSES:
        source += f'\nnew File("${{workflow.launchDir}}/artifacts/{process}.sh").text = """' + _stub_nonpaths(_shell(main, process)) + '"""\n'
    # Execute the actual prune lookup, including its existing empty/missing policy.
    prune_shell = main.split('C1_SAMPLES_FILE="${otuPruneSamplesFileValue}"', 1)[1].split('RTBIOSCAN_EFFECTIVE_IDENTITY_MODE=', 1)[0]
    # R3-C3 places stable-identity projections after the lookup; they need state files, so probe only the lookup.
    prune_shell = prune_shell.split('C1_STABLE_KEYS=', 1)[0]
    prune_shell = 'C1_SAMPLES_FILE="${otuPruneSamplesFileValue}"' + prune_shell
    prune_shell = prune_shell.replace('${sampleInfoDir}', '${workflow.launchDir}/sample-info').replace('${replicateModeCanonical}', 'collapse')
    backup = _shell(main, "backup_update_and_clean")
    disposal = backup[backup.index('# -- §3: done_pod5 append'):backup.index('# -- §6: State-tables snapshot')]
    if '            JOINT_READY=0' in disposal:
        # The disposal code ends before RF-PIN/candidate work. Exercise its
        # unchanged bytes, then the exact task-output copy from the fenced
        # region; this harness does not publish scientific snapshot roots.
        disposal = disposal.split('            JOINT_READY=0', 1)[0]
        region = backup.split('joint_snapshot_region() {', 1)[1]
        copy_line = next(line for line in region.splitlines() if line.strip() == r'cp \$STATE_TMP/done_pod5.txt done_pod5.txt')
        root_copy = next(line for line in region.splitlines() if line.strip() == r'cp \$STATE_TMP/done_pod5.txt "\$CURRENT_TEMP_ROOT"/')
        disposal += copy_line + '\n' + root_copy + '\n'
    disposal = disposal.replace('${params.delete_input_pod5}', 'true' if delete else 'false')
    disposal = disposal.replace('${baseDir}', '${workflow.launchDir}/stub-base').replace('${runMode}', 'realtime').replace('${params.watch}', 'true').replace('${params.delete_from_ori_dir}', 'false')
    output_decl = main.split('process backup_update_and_clean {', 1)[1].split('output:', 1)[1].splitlines()[1].strip()
    source += r'''
    process PATH_PROBE {
      output:
      file 'probe.txt'
      script:
      """
      printf '%s\\n' "\$DORADO_LOCK_PATH" > probe.txt
      printf '%s\\n' "\$PWD" >> probe.txt
      # A task-local file must never win over a launch-relative file.
      printf 'WORK_SAMPLE\\n' > samples.txt
    ''' + prune_shell + r'''
      if [ -n "\$C1_SAMPLES_FILE" ]; then cat "\$C1_SAMPLES_FILE" >> probe.txt; else echo MISSING >> probe.txt; fi
      cp probe.txt "${workflow.launchDir}/artifacts/probe.txt"
      """
    }
    process BACKUP_PROBE {
      beforeScript ''
      output:
    ''' + output_decl + r'''
      script:
      """
      set -euo pipefail
      STATE_TMP="${workflow.launchDir}/state"
      CURRENT_TEMP_ROOT="${workflow.launchDir}/current"
      READ_PATH="${workflow.launchDir}/intake/processed.pod5"
      FEEDER_SLICE_SIDECAR="${workflow.launchDir}/absent-sidecar"
      FEEDER_GLOBAL_LEDGER_DIR="${workflow.launchDir}/global"
      DONE_LOCK="\$STATE_TMP/done.lock"
      acquire_lock() { return 0; }
      release_lock() { return 0; }
    ''' + disposal + r'''
      printf 'reached-section-6\\n' > "${workflow.launchDir}/artifacts/finalized.txt"
      cp done_pod5.txt "${workflow.launchDir}/artifacts/done_pod5.txt"
      """
    }
    '''
    (project / "probe.nf").write_text(source)
    args = ["-C", str(project / "nextflow.config"), "run", str(project / "probe.nf"),
            "-ansi-log", "false", "-work-dir", str(launch / "work"),
            "--ori_dir", str(launch / "spool"), "--run_id", "1"]
    if prune:
        args += ["--otu_prune_samples_file", prune]
    if raw is not None:
        args += ["--outdir", raw]
    return launch, project, args


def _probe(tmp_path, main, config, **kwargs):
    launch, project, args = _prepare(tmp_path, main, config, **kwargs)
    _run(args, launch, timeout=45)
    artifacts = launch / "artifacts"
    pre = json.loads((artifacts / "preamble.json").read_text())
    lines = (artifacts / "probe.txt").read_text().splitlines()
    return launch, pre, lines


def _assert_paths(launch, pre, lines, raw):
    expected = raw if raw and raw.startswith('/') else f"{launch}/{raw}" if raw is not None else f"{launch}/results"
    assert pre["raw"] == (raw if raw is not None else expected)
    serialized = (launch / "artifacts/raw-params.txt").read_text()
    assert re.search(r'(?m)^  outdir + = "' + re.escape(raw if raw is not None else expected) + '"$', serialized)
    assert pre["restart"] == expected, "restart OUTDIR must resolve against launchDir"
    for key, suffix in {"ongoing": "temp/ongoing/state/STATE", "current": "temp/current/state/STATE",
                        "currentResults": "current/state/STATE", "ongoingResults": "ongoing/state/STATE",
                        "cache": "temp/_compatibility_cache"}.items():
        assert pre[key] == f"{expected}/{suffix}", key
    assert lines[0] == f"{expected}/temp/ongoing/_dorado_basecaller", "config must see CLI outdir before interpolation"
    assert lines[1].startswith(str(launch / "work") + '/'), "process must execute in a task workdir"
    if raw and not raw.startswith('/'):
        for process in PROCESSES:
            text = (launch / f"artifacts/{process}.sh").read_text()
            assert text.count(raw) == text.count(expected), f"{process}: unresolved task path"
    for process, suffix in [("fast_on_target_detection", "/ongoing/"),
                            ("_reporting_hq_demultiplexing", "/ongoing"),
                            ("backup_update_and_clean", "/report_html/runs/1"),
                            ("async_report_render", "/.report_root_render.lock")]:
        text = (launch / f"artifacts/{process}.sh").read_text()
        assert expected + suffix in text, process
        assert not re.search(r'(?<!/)\bwork/[^\n]*results_params_A', text)
    summary = (launch / "artifacts/getting_run_summary.sh").read_text()
    assert f"{launch}/results/sample_info/1/track_roster.tsv" in summary
    if expected != f"{launch}/results":
        assert f"{expected}/sample_info/" not in summary
    backup = (launch / "artifacts/backup_update_and_clean.sh").read_text()
    assert f'RUN_REPORT_DIR="{expected}/report_html/runs/1"' in backup
    async_render = (launch / "artifacts/async_report_render.sh").read_text()
    assert f'REPORT_ROOT_RENDER_LOCK="{expected}/.report_root_render.lock"' in async_render


@pytest.mark.parametrize("shape", ["default", "relative", "dotdot", "assignment", "symlink"])
def test_real_nextflow_launch_paths(tmp_path, shape):
    raw = {"default": None, "relative": "results_params_A", "dotdot": "a/../results/./",
           "assignment": "run=1/results"}.get(shape)
    if shape == "symlink":
        (tmp_path / "target").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "target", target_is_directory=True)
        raw = str(tmp_path / "link") + '/./results/'
    launch, pre, lines = _probe(tmp_path, (ROOT / "main.nf").read_text(),
                               (ROOT / "nextflow.config").read_text(), raw=raw, prune="samples.txt")
    _assert_paths(launch, pre, lines, raw)
    assert lines[2] == "LAUNCH_SAMPLE", "prune must not silently use the task-local file"
    assert pre["prune"] == f"{launch}/samples.txt"


@pytest.mark.parametrize("shape", ["absolute", "empty", "missing"])
def test_prune_lookup_policy(tmp_path, shape):
    absolute = str(tmp_path / 'absolute-samples.txt')
    Path(absolute).write_text('ABSOLUTE_SAMPLE\n')
    raw = {"absolute": '  ' + absolute + '  ', "empty": '', "missing": 'missing.txt'}[shape]
    launch, pre, lines = _probe(tmp_path, (ROOT / "main.nf").read_text(),
                               (ROOT / "nextflow.config").read_text(), prune=raw)
    assert pre['prune'] == {"absolute": absolute, "empty": '', "missing": f'{launch}/missing.txt'}[shape]
    assert lines[2] == {"absolute": "ABSOLUTE_SAMPLE", "empty": "DEFAULT_SAMPLE", "missing": "MISSING"}[shape]


@pytest.mark.parametrize("delete,ready", [(False, False), (True, False), (False, True), (True, True)])
def test_backup_finalizes_without_promoting(tmp_path, delete, ready):
    main = (ROOT / "main.nf").read_text()
    launch, _, _ = _probe(tmp_path, main, (ROOT / "nextflow.config").read_text(), delete=delete, ready=ready)
    assert (launch / 'artifacts/finalized.txt').read_text().strip() == 'reached-section-6'
    assert (launch / 'spool/future.pod5').is_file(), 'backup must leave feeder spool present'
    assert (launch / 'spool/future.pod5').read_text() == 'feeder spool sentinel', "backup must not promote feeder spool"
    assert not (launch / 'intake/future.pod5').exists()
    assert not (launch / 'intake/processed.pod5').exists()
    assert (launch / 'pod/done_round_pod5/processed.pod5').exists() is not delete
    if ready:
        assert (launch / 'intake/ready.pod5').read_text() == 'feeder ready sentinel'
        assert (launch / 'intake/broken.pod5').is_symlink()
    done = (launch / 'artifacts/done_pod5.txt').read_bytes()
    assert b'processed.pod5' in done
    assert (launch / 'current/done_pod5.txt').read_bytes() == done
    assert (launch / 'state/done_pod5.txt').read_bytes() == done
    assert 'file("done_pod5.txt")' in main.split('process backup_update_and_clean {')[1].split('script:')[0]
    backup = _shell(main, 'backup_update_and_clean')
    for forbidden in ('params.ori_dir', 'Staging helpers + realtime watch loop', 'ls -1tr',
                      'cp_new_pod5.err', 'sleep 10', 'pod5_key()', 'done_contains()', 'wait_minutes='):
        assert forbidden not in backup


def test_no_raw_path_consumers():
    main = (ROOT / "main.nf").read_text()
    lines = [line for line in main.splitlines() if not line.lstrip().startswith('//')]
    assert [line for line in lines if 'params.outdir' in line] == ['def outdirRaw = params.outdir?.toString()']
    assert not any('params.ori_dir' in line for line in lines)
    assert [line.strip() for line in lines if 'params.otu_prune_samples_file' in line] == [
        'def otuPruneSamplesFileValue = params.otu_prune_samples_file.toString().trim()']


def test_config_flat_offline(tmp_path):
    launch, project, _ = _prepare(tmp_path, (ROOT / 'main.nf').read_text(), (ROOT / 'nextflow.config').read_text())
    result = _run(['-C', str(project / 'nextflow.config'), 'config', '-flat'], launch)
    (launch / 'config-flat.txt').write_text(result.stdout)
    assert f'{launch}/results/temp/ongoing/_dorado_basecaller' in result.stdout


def _without_promoter(main):
    start = main.index("\t\t# Derive pod5_dir only")
    end = main.index('\t\t# -- §6: State-tables snapshot', start)
    return (main[:start] + main[end:]).replace('\twait_minutes=${params.file_wait_minutes ?: 30}\n', '')


def _without_fast_split_child_partition(artifact):
    lines = artifact.splitlines(keepends=True)
    start_marker = b'# FAST split-child partition start'
    end_marker = b'# FAST split-child partition end'
    starts = [index for index, line in enumerate(lines) if line.strip() == start_marker]
    ends = [index for index, line in enumerate(lines) if line.strip() == end_marker]
    assert len(starts) == 1, f'expected exactly one FAST split-child start marker, found {len(starts)}'
    assert len(ends) == 1, f'expected exactly one FAST split-child end marker, found {len(ends)}'
    assert starts[0] < ends[0], 'FAST split-child end marker must follow start marker'
    return b''.join(lines[:starts[0]] + lines[ends[0] + 1:])


@pytest.mark.parametrize('default', [False, True])
def test_head_candidate_generated_command_equivalence(tmp_path, default):
    head_main = subprocess.check_output(['git', 'show', f'{HEAD}:main.nf'], cwd=ROOT, text=True)
    head_config = subprocess.check_output(['git', 'show', f'{HEAD}:nextflow.config'], cwd=ROOT, text=True)
    # Isolate exactly the intentional promoter/dead-variable deletion in HEAD.
    head_main = _without_promoter(head_main)
    # R4-D: isolate the intentional round-report invocation changes (schema
    # 2.1, sealed reporting-sidecar inputs, retired --summary inputs).
    r4d_replacements = [
        ('\t\t\t--schema-version "2.0" \\\n\t\t\t--asset-snapshot-policy "latest_only" \\\n',
         '\t\t\t--schema-version "2.1" \\\n\t\t\t--asset-snapshot-policy "latest_only" \\\n', 1),
        ('\t\t\t--blast-otu "${blast_otu_pretax_rpt}" \\\n',
         '\t\t\t--blast-otu "${blast_otu_pretax_rpt}" \\\n'
         '\t\t\t--blast-otu-reporting "\\$ROUND_DIR/${barcode}_blast_otu_reporting_v1.tsv" \\\n'
         '\t\t\t--blast-otu-reporting-cumulative "${ongoingStateDir}/_state/${barcode}_blast_otu_reporting_v1.tsv" \\\n', 1),
        ('\t\t\t--summary "${summary}" \\\n\t\t\t--summary-otu "${summary_otu}" \\\n', '', 1),
        # R4-I2: backup_update_and_clean names the cumulative generation's source explicitly.
        ('\t\t\t\t\tpngs_all=( "\\$STATE_TMP"/*.png )\n',
         '\t\t\t\t\tpngs_all=( "\\$STATE_TMP"/*.png )\n'
         '\t\t\t\t\tr4d_backup_generation "\\$ONGOING_FINAL" "\\$STATE_TMP" "${barcode}"\n', 1),
        ('\t\t\t\t"\\$ONGOING_FINAL"/*_rpt.txt "\\$ONGOING_FINAL"/*_rpt.txt.gz )\n\t\t\tif (( \\${#tables[@]} )); then\n',
         '\t\t\t\t"\\$ONGOING_FINAL"/*_rpt.txt "\\$ONGOING_FINAL"/*_rpt.txt.gz )\n'
         '\t\t\tr4d_backup_generation "\\$CURRENT_ROOT/tables" "\\$ONGOING_FINAL" "${barcode}"\n'
         '\t\t\tif (( \\${#tables[@]} )); then\n', 1),
        # R5-F01: backup_update_and_clean seals the restore snapshot's completeness record.
        ('\t\t\t\t"\\$STATE_TMP"/state_compatibility_manifest.tsv )\n\t\t\tif (( \\${#state_tables[@]} )); then\n',
         '\t\t\t\t"\\$STATE_TMP"/state_compatibility_manifest.tsv )\n'
         '\t\t\t# R5-F01: seal a complete, integrity-bound copy of the authoritative state\n'
         '\t\t\t# (and done_pod5.txt) in each root; restore refuses a root without one.\n'
         '\t\t\tperl "${baseDir}/bin/state_snapshot_authority.pl" publish "\\$STATE_TMP" "\\$CURRENT_TEMP_ROOT" "\\$CURRENT_ROOT" \\\n'
         '\t\t\t\t|| echo "WARN: state snapshot authority not published; restore refuses this snapshot until a later backup seals it" >&2\n'
         '\t\t\tif (( \\${#state_tables[@]} )); then\n', 1),
    ]
    for old, new, count in r4d_replacements:
        assert head_main.count(old) == count, old
        head_main = head_main.replace(old, new)
    if not default:
        replacements = [
            ('${params.outdir}/sample_info/${run_name}/track_roster.tsv',
             '${sampleInfoDir}/track_roster.tsv', 1),
            ('${params.outdir}/sample_info/${run_name}/track_identity.tsv',
             '${sampleInfoDir}/track_identity.tsv', 1),
            ('${params.outdir}/sample_info/${run_name}/samples.txt',
             '${sampleInfoDir}/samples.txt', 2),
        ]
        for old, new, count in replacements:
            assert head_main.count(old) == count, old
            head_main = head_main.replace(old, new)
    # PROTECTED_TEST_MAP: exact approved joint-v2 backup command delta.
    # Its semantics are exercised by the worker/publication tests. Freeze the
    # literal bytes here so this allowance cannot absorb unrelated changes.
    candidate_main = (ROOT / 'main.nf').read_text()
    candidate_backup = _shell(candidate_main, 'backup_update_and_clean')
    import hashlib
    assert hashlib.sha256(candidate_backup.encode()).hexdigest() == '3d7f6afb30761bd16ce8b6155045e90adde5547bcaadd3ac33cbe08542080d3b'
    predecessor_backup = _shell(head_main, 'backup_update_and_clean')
    assert head_main.count(predecessor_backup) == 1
    head_main = head_main.replace(predecessor_backup, candidate_backup.replace('${outdirResolved}', '${params.outdir}'), 1)
    raw = None if default else str(tmp_path / 'absolute-link') + '/./results/'
    (tmp_path / 'target').mkdir()
    (tmp_path / 'absolute-link').symlink_to(tmp_path / 'target', target_is_directory=True)
    launch, head_pre, head_lines = _probe(tmp_path, head_main, head_config, raw=raw)
    head_before = (launch / "artifacts/beforeScript.txt").read_bytes()
    head_artifacts = {name: (launch / f'artifacts/{name}.sh').read_bytes() for name in PROCESSES}
    # Same launch/project locations make byte comparison meaningful.
    launch, pre, lines = _probe(tmp_path, (ROOT / 'main.nf').read_text(), (ROOT / 'nextflow.config').read_text(), raw=raw)
    assert pre == head_pre
    assert (launch / "artifacts/beforeScript.txt").read_bytes() == head_before
    assert lines[0] == head_lines[0]
    for name, expected in head_artifacts.items():
        actual = (launch / f'artifacts/{name}.sh').read_bytes()
        if name == 'fast_on_target_detection':
            actual = _without_fast_split_child_partition(actual)
        assert actual == expected, name


def test_dorado_lock_launch_directory_with_spaces(tmp_path):
    launch, project, args = _prepare(tmp_path, (ROOT / 'main.nf').read_text(),
                                     (ROOT / 'nextflow.config').read_text(),
                                     raw='run=1/results/../final/', launch_name='launch dir')
    script = project / 'probe.nf'
    script.write_text(script.read_text().split('    process BACKUP_PROBE {')[0])
    _run(args, launch)
    assert (launch / 'artifacts/probe.txt').read_text().splitlines()[0] == (
        f'{launch}/run=1/results/../final//temp/ongoing/_dorado_basecaller')


@pytest.mark.parametrize('literal', ['null', '""'])
def test_empty_outdir_keeps_existing_preamble_behavior(tmp_path, literal):
    main = (ROOT / 'main.nf').read_text()
    config = (ROOT / 'nextflow.config').read_text().replace('outdir = "$launchDir/results"', 'outdir = ' + literal, 1)
    launch, pre, _ = _probe(tmp_path, main, config)
    assert pre['ongoing'] is None
    assert pre['current'] is None
    assert pre['restart'] == ''
    assert pre['raw'] == (None if literal == 'null' else '')
