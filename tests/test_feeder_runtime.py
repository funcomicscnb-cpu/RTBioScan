from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FEEDER = REPO_ROOT / "bin" / "Metadata_pod5_processing.sh"
WRAPPER = REPO_ROOT / "RTBioScan.sh"


def wait_for(predicate, timeout: float = 8.0, interval: float = 0.05) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("timed out waiting for condition")


def stop_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def wait_for_process_exit(proc: subprocess.Popen[str], timeout: float = 8.0) -> int:
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stop_process(proc)
        raise AssertionError("timed out waiting for process exit") from exc


def launch_feeder(
    cwd: Path,
    run_id: str,
    input_dir: Path,
    *,
    env: dict[str, str] | None = None,
    num_reads: str = "10",
) -> subprocess.Popen[str]:
    run_env = os.environ.copy()
    run_env["TERM"] = "dumb"
    if env:
        run_env.update(env)
    cmd = [
        "bash",
        str(FEEDER),
        "--run_id",
        run_id,
        "--input_folder",
        str(input_dir),
        "--sleep_time",
        "1",
        "--num_reads",
        num_reads,
        "--targets",
        "COI|ITS2",
    ]
    return subprocess.Popen(
        cmd,
        cwd=cwd,
        env=run_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def make_pod5_stub_env(tmp_path: Path, *, inspect_counts: dict[str, int], view_rows: dict[str, list[str]]) -> dict[str, str]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub_path = bin_dir / "pod5"
    stub_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"INSPECT_COUNTS = {json.dumps(inspect_counts)}\n"
        f"VIEW_ROWS = {json.dumps(view_rows)}\n"
        "args = sys.argv[1:]\n"
        "if not args:\n"
        "    raise SystemExit(99)\n"
        "def key_for(path):\n"
        "    return Path(path).name\n"
        "if args[:2] == ['inspect', 'summary']:\n"
        "    key = key_for(args[2])\n"
        "    if key not in INSPECT_COUNTS:\n"
        "        print(f'missing inspect count for {key}', file=sys.stderr)\n"
        "        raise SystemExit(2)\n"
        "    count = int(INSPECT_COUNTS[key])\n"
        "    batches = (count + 999) // 1000\n"
        "    if batches == 0:\n"
        "        batches = 1\n"
        "    for idx in range(max(batches - 1, 0)):\n"
        "        print(f'Batch {idx + 1}, 1000 reads')\n"
        "    last = count - (max(batches - 1, 0) * 1000)\n"
        "    print(f'Batch {batches}, {last} reads')\n"
        "    print(f'Found {batches} batches, {count} reads')\n"
        "    raise SystemExit(0)\n"
        "if args and args[0] == 'view':\n"
        "    input_path = None\n"
        "    output_path = None\n"
        "    no_header = False\n"
        "    idx = 1\n"
        "    while idx < len(args):\n"
        "        arg = args[idx]\n"
        "        if arg == '--no-header':\n"
        "            no_header = True\n"
        "            idx += 1\n"
        "        elif arg == '--output':\n"
        "            output_path = args[idx + 1]\n"
        "            idx += 2\n"
        "        elif arg.startswith('--'):\n"
        "            idx += 1\n"
        "        else:\n"
        "            input_path = arg\n"
        "            idx += 1\n"
        "    key = key_for(input_path or '')\n"
        "    rows = VIEW_ROWS.get(key)\n"
        "    if rows is None:\n"
        "        print(f'missing view rows for {key}', file=sys.stderr)\n"
        "        raise SystemExit(2)\n"
        "    lines = list(rows)\n"
        "    if not no_header:\n"
        "        lines.insert(0, 'read_id\\tfilename')\n"
        "    payload = '\\n'.join(lines) + ('\\n' if lines else '')\n"
        "    if output_path:\n"
        "        Path(output_path).write_text(payload, encoding='utf-8')\n"
        "    else:\n"
        "        sys.stdout.write(payload)\n"
        "    raise SystemExit(0)\n"
        "if args and args[0] == 'filter':\n"
        "    output_path = None\n"
        "    ids_path = None\n"
        "    idx = 1\n"
        "    while idx < len(args):\n"
        "        arg = args[idx]\n"
        "        if arg == '--ids':\n"
        "            ids_path = args[idx + 1]\n"
        "            idx += 2\n"
        "        elif arg == '--output':\n"
        "            output_path = args[idx + 1]\n"
        "            idx += 2\n"
        "        else:\n"
        "            idx += 1\n"
        "    count = 0\n"
        "    if ids_path:\n"
        "        count = sum(1 for line in Path(ids_path).read_text(encoding='utf-8').splitlines() if line.strip())\n"
        "    if output_path:\n"
        "        Path(output_path).write_text('filtered\\n', encoding='utf-8')\n"
        "    print(f'Found {count} read_ids from 1 inputs')\n"
        "    print(f'Calculated {count} transfers')\n"
        "    raise SystemExit(0)\n"
        "print('unsupported pod5 stub invocation', file=sys.stderr)\n"
        "raise SystemExit(98)\n",
        encoding="utf-8",
    )
    stub_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["TERM"] = "dumb"
    return env


def make_nextflow_stub_env(
    tmp_path: Path,
    *,
    run_mode: str = "sleep",
    run_exit: int = 0,
) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "fake-nextflow-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "nextflow-invocations.jsonl"
    stub_path = bin_dir / "nextflow"
    stub_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "log_path = Path(os.environ['RTBIOSCAN_NEXTFLOW_LOG'])\n"
        "log_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "with log_path.open('a', encoding='utf-8') as handle:\n"
        "    handle.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args and args[0] == 'run':\n"
        "    mode = os.environ.get('RTBIOSCAN_NEXTFLOW_RUN_MODE', 'sleep')\n"
        "    exit_code = int(os.environ.get('RTBIOSCAN_NEXTFLOW_RUN_EXIT', '0'))\n"
        "    signal_count = {'value': 0}\n"
        "    if mode in ('sleep', 'ignore_once'):\n"
        "        def _handle(signum, _frame):\n"
        "            signal_count['value'] += 1\n"
        "            if mode == 'ignore_once' and signal_count['value'] == 1:\n"
        "                return\n"
        "            raise SystemExit(128 + signum)\n"
        "        signal.signal(signal.SIGINT, _handle)\n"
        "        signal.signal(signal.SIGTERM, _handle)\n"
        "        signal.signal(signal.SIGHUP, _handle)\n"
        "        while True:\n"
        "            time.sleep(0.1)\n"
        "    raise SystemExit(exit_code)\n"
        "raise SystemExit(0)\n",
        encoding="utf-8",
    )
    stub_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["RTBIOSCAN_NEXTFLOW_LOG"] = str(log_path)
    env["RTBIOSCAN_NEXTFLOW_RUN_MODE"] = run_mode
    env["RTBIOSCAN_NEXTFLOW_RUN_EXIT"] = str(run_exit)
    env["TERM"] = "dumb"
    return env, log_path


def prepend_broken_pgrep_shim(env: dict[str, str], tmp_path: Path) -> dict[str, str]:
    shim_dir = tmp_path / "broken-pgrep-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "pgrep"
    shim_path.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'pgrep shim failure' >&2\n"
        "exit 2\n",
        encoding="utf-8",
    )
    shim_path.chmod(0o755)
    merged = env.copy()
    merged["PATH"] = f"{shim_dir}:{env['PATH']}"
    return merged


def read_nextflow_invocations(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_feeder_self_lock_blocks_second_instance(tmp_path: Path) -> None:
    run_id = f"LockRun_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    proc = launch_feeder(tmp_path, run_id, input_dir)
    lock_meta = tmp_path / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir" / "meta.env"
    try:
        wait_for(lock_meta.exists)
        second = subprocess.run(
            [
                "bash",
                str(FEEDER),
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "1",
                "--num_reads",
                "10",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**os.environ, "TERM": "dumb"},
            check=False,
        )
        assert second.returncode != 0
        assert "another feeder is already active" in (second.stdout + second.stderr)
        assert not (tmp_path / "results" / "pod5" / run_id / "metadata" / "next_round_id.count").exists()
    finally:
        stop_process(proc)


def test_feeder_sigterm_releases_lock_and_exits(tmp_path: Path) -> None:
    run_id = f"TermRun_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    proc = launch_feeder(tmp_path, run_id, input_dir)
    lock_dir = tmp_path / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"
    try:
        wait_for(lock_dir.exists)
        proc.send_signal(signal.SIGTERM)
        assert wait_for_process_exit(proc) == 143
        wait_for(lambda: not lock_dir.exists())
    finally:
        stop_process(proc)


def test_feeder_sigint_releases_lock_and_exits(tmp_path: Path) -> None:
    run_id = f"IntRun_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    proc = launch_feeder(tmp_path, run_id, input_dir)
    lock_dir = tmp_path / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"
    try:
        wait_for(lock_dir.exists)
        proc.send_signal(signal.SIGINT)
        assert wait_for_process_exit(proc) == 130
        wait_for(lambda: not lock_dir.exists())
    finally:
        stop_process(proc)


def test_feeder_reclaims_stale_lock_from_dead_pid(tmp_path: Path) -> None:
    run_id = f"ReclaimRun_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    lock_dir = tmp_path / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"
    lock_dir.mkdir(parents=True)
    (lock_dir / "meta.env").write_text("pid=999999\nhost=testhost\nstarted_epoch=1\n", encoding="utf-8")

    proc = launch_feeder(tmp_path, run_id, input_dir, env={"HOSTNAME": "testhost"})
    try:
        wait_for(lambda: lock_dir.exists() and "pid=999999" not in (lock_dir / "meta.env").read_text(encoding="utf-8"))
        assert proc.poll() is None
    finally:
        stop_process(proc)


def test_feeder_rebuilds_invalid_cache_before_progress(tmp_path: Path) -> None:
    run_id = f"CacheRebuild_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source_pod5 = input_dir / "sample.pod5"
    source_pod5.write_bytes(b"pod5-cache")

    env = make_pod5_stub_env(
        tmp_path,
        inspect_counts={"sample.pod5": 2},
        view_rows={"sample.pod5": ["r1\tfile1", "r2\tfile1"]},
    )

    metadata_dir = tmp_path / "results" / "pod5" / run_id / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    archived_dir = tmp_path / "results" / "pod5" / run_id / "full_pod5"
    archived_dir.mkdir(parents=True, exist_ok=True)
    archived_pod5 = archived_dir / "sample.pod5"
    archived_pod5.write_bytes(b"pod5-cache")

    source_size = archived_pod5.stat().st_size
    source_mtime = int(archived_pod5.stat().st_mtime)
    (metadata_dir / "fullview_sample.pod5.txt").write_text("r1\tfile1\n", encoding="utf-8")
    (metadata_dir / "fullview_sample.pod5.meta.env").write_text(
        f"source_size={source_size}\nsource_mtime={source_mtime}\nverified_reads=2\ncache_rows=2\n",
        encoding="utf-8",
    )

    proc = launch_feeder(tmp_path, run_id, input_dir, env=env)
    per_file_view = metadata_dir / "fullview_sample.pod5.txt"
    meta_path = metadata_dir / "fullview_sample.pod5.meta.env"
    try:
        wait_for(lambda: per_file_view.exists() and per_file_view.read_text(encoding="utf-8").splitlines() == ["r1\tfile1", "r2\tfile1"])
        meta_text = meta_path.read_text(encoding="utf-8")
        assert "verified_reads=2" in meta_text
        assert "cache_rows=2" in meta_text
        assert proc.poll() is None
    finally:
        stop_process(proc)


def test_feeder_applies_global_prefix_before_sidecar_repair(tmp_path: Path) -> None:
    run_id = f"PrefixSync_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()

    env = make_pod5_stub_env(
        tmp_path,
        inspect_counts={"sample.pod5": 2},
        view_rows={"sample.pod5": ["r1\tfile1", "r2\tfile1"]},
    )

    pod5_root = tmp_path / "results" / "pod5" / run_id
    metadata_dir = pod5_root / "metadata"
    archived_dir = pod5_root / "full_pod5"
    spool_dir = pod5_root / "ori_round_pod5"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    archived_dir.mkdir(parents=True, exist_ok=True)
    spool_dir.mkdir(parents=True, exist_ok=True)

    archived_pod5 = archived_dir / "sample.pod5"
    archived_pod5.write_bytes(b"pod5-prefix")
    source_size = archived_pod5.stat().st_size
    source_mtime = int(archived_pod5.stat().st_mtime)

    per_file_view = metadata_dir / "fullview_sample.pod5.txt"
    per_file_view.write_text("r1\tfile1\nr2\tfile1\n", encoding="utf-8")
    source_fp = hashlib.sha256(b"r1\nr2\n").hexdigest()
    (metadata_dir / "fullview_sample.pod5.meta.env").write_text(
        f"source_size={source_size}\nsource_mtime={source_mtime}\nverified_reads=2\ncache_rows=2\nsource_fp={source_fp}\n",
        encoding="utf-8",
    )

    round_id = f"{run_id}_1"
    (spool_dir / f"{round_id}.pod5").write_bytes(b"round")
    (metadata_dir / f"{round_id}_read_info_rpt.txt").write_text("read_id\tfilename\nr1\tfile1\nr2\tfile1\n", encoding="utf-8")
    (metadata_dir / f"{round_id}_slice.tsv").write_text(
        "# slice_fingerprint=dummy-slice\n# read_count=2\n# commit_timestamp=1\n"
        f"{source_fp}\tsample.pod5\t1\t2\n",
        encoding="utf-8",
    )
    progress_file = metadata_dir / "progress_reads_sample.pod5.count"
    progress_file.write_text("0\n", encoding="utf-8")

    global_dir = tmp_path / "results" / "temp" / "_global" / "feeder_dedup"
    global_dir.mkdir(parents=True, exist_ok=True)
    (global_dir / "slice_completion.tsv").write_text(
        f"dummy-slice\t{source_fp}\tsample.pod5\t1\t2\t{pod5_root}\t1\n",
        encoding="utf-8",
    )

    proc = launch_feeder(tmp_path, run_id, input_dir, env=env)
    try:
        wait_for(lambda: progress_file.exists() and progress_file.read_text(encoding="utf-8").strip() == "2")
        assert (metadata_dir / f"{round_id}_slice.tsv").exists()
        assert (metadata_dir / f"{round_id}_read_info_rpt.txt").exists()
        assert proc.poll() is None
    finally:
        stop_process(proc)


def test_startup_orphan_cleanup(tmp_path: Path) -> None:
    run_id = f"OrphanRun_{tmp_path.name}"
    pod5_base = tmp_path / "results" / "pod5" / run_id
    ori_dir = pod5_base / "ori_round_pod5"
    rt_dir = pod5_base / "reads_rt_round_pod5"
    done_dir = pod5_base / "done_round_pod5"
    meta_dir = pod5_base / "metadata"
    for directory in (ori_dir, rt_dir, done_dir, meta_dir):
        directory.mkdir(parents=True, exist_ok=True)

    orphan_ori = ori_dir / f"{run_id}_2.pod5"
    orphan_rt = rt_dir / f"{run_id}_2.pod5"
    orphan_ori.write_bytes(b"")
    orphan_rt.write_bytes(b"")
    orphan_meta = meta_dir / f"{run_id}_2_read_info_rpt.txt"
    orphan_meta.write_text("header\n", encoding="utf-8")

    metadata_only_orphan = meta_dir / f"{run_id}_5_read_info_rpt.txt"
    metadata_only_orphan.write_text("header\n", encoding="utf-8")

    valid_sidecar_meta = meta_dir / f"{run_id}_6_read_info_rpt.txt"
    valid_sidecar_meta.write_text("header\n", encoding="utf-8")
    valid_sidecar = meta_dir / f"{run_id}_6_slice.tsv"
    valid_sidecar.write_text(
        "# slice_fingerprint=def456\n"
        "# read_count=10\n"
        "# commit_timestamp=1000001\n"
        "fp_def\tFAK00001.pod5\t1\t100\n",
        encoding="utf-8",
    )

    done_only_meta = meta_dir / f"{run_id}_7_read_info_rpt.txt"
    done_only_meta.write_text("header\n", encoding="utf-8")
    (done_dir / f"{run_id}_7.pod5").write_bytes(b"done")

    completed_report_meta = meta_dir / f"{run_id}_8_read_info_rpt.txt"
    completed_report_meta.write_text("header\n", encoding="utf-8")
    completed_round_dir = tmp_path / "results" / "temp" / "ongoing" / "state" / run_id / f"{run_id}_8"
    completed_round_dir.mkdir(parents=True, exist_ok=True)
    (completed_round_dir / "round_report.json").write_text('{"round_barcode":"%s_8"}\n' % run_id, encoding="utf-8")

    source_file = "FAK00001.pod5"
    sidecar = meta_dir / f"{run_id}_3_slice.tsv"
    sidecar.write_text(
        "# slice_fingerprint=abc123\n"
        "# read_count=10\n"
        "# commit_timestamp=1000000\n"
        f"fp_abc\t{source_file}\t1\t100\n",
        encoding="utf-8",
    )
    progress = meta_dir / f"progress_reads_{source_file}.count"
    progress.write_text("100\n", encoding="utf-8")

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env = make_pod5_stub_env(tmp_path, inspect_counts={}, view_rows={})
    proc = launch_feeder(tmp_path, run_id, input_dir, env=env)
    lock_meta = meta_dir / ".feeder.lockdir" / "meta.env"
    try:
        wait_for(lock_meta.exists, timeout=10)
        wait_for(lambda: not orphan_ori.exists() and not orphan_rt.exists(), timeout=5)
        assert proc.poll() is None
    finally:
        stop_process(proc)

    assert not orphan_ori.exists()
    assert not orphan_rt.exists()
    assert not orphan_meta.exists()
    assert not metadata_only_orphan.exists()
    assert valid_sidecar_meta.exists()
    assert valid_sidecar.exists()
    assert done_only_meta.exists()
    assert completed_report_meta.exists()
    assert sidecar.exists()

    ledger = meta_dir / "round_commit_ledger.tsv"
    if ledger.exists():
        content = ledger.read_text(encoding="utf-8")
        assert f"\t{run_id}_3" in content
        assert f"\t{run_id}_2" not in content

    feeder_log = meta_dir / f"{run_id}_feeder.log"
    assert feeder_log.exists()
    feeder_log_text = feeder_log.read_text(encoding="utf-8")
    assert "startup_orphan_cleanup" in feeder_log_text
    assert "startup_orphan_metadata_cleanup" in feeder_log_text
    assert f"{run_id}_6" not in "\n".join(
        line for line in feeder_log_text.splitlines() if "startup_orphan_metadata_cleanup" in line
    )
    assert f"{run_id}_7" not in "\n".join(
        line for line in feeder_log_text.splitlines() if "startup_orphan_metadata_cleanup" in line
    )
    assert f"{run_id}_8" not in "\n".join(
        line for line in feeder_log_text.splitlines() if "startup_orphan_metadata_cleanup" in line
    )


def test_feeder_aborts_on_invalid_cache_after_progress(tmp_path: Path) -> None:
    run_id = f"CacheAbort_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source_pod5 = input_dir / "sample.pod5"
    source_pod5.write_bytes(b"pod5-cache")

    env = make_pod5_stub_env(
        tmp_path,
        inspect_counts={"sample.pod5": 2},
        view_rows={"sample.pod5": ["r1\tfile1", "r2\tfile1"]},
    )

    metadata_dir = tmp_path / "results" / "pod5" / run_id / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    archived_dir = tmp_path / "results" / "pod5" / run_id / "full_pod5"
    archived_dir.mkdir(parents=True, exist_ok=True)
    archived_pod5 = archived_dir / "sample.pod5"
    archived_pod5.write_bytes(b"pod5-cache")

    source_size = archived_pod5.stat().st_size
    source_mtime = int(archived_pod5.stat().st_mtime)
    (metadata_dir / "fullview_sample.pod5.txt").write_text("r1\tfile1\n", encoding="utf-8")
    (metadata_dir / "fullview_sample.pod5.meta.env").write_text(
        f"source_size={source_size}\nsource_mtime={source_mtime}\nverified_reads=2\ncache_rows=2\n",
        encoding="utf-8",
    )
    (metadata_dir / "progress_reads_sample.pod5.count").write_text("1\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(FEEDER),
            "--run_id",
            run_id,
            "--input_folder",
            str(input_dir),
            "--sleep_time",
            "1",
            "--num_reads",
            "10",
            "--targets",
            "COI|ITS2",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert "fullview cache integrity mismatch" in text
    assert not any((tmp_path / "results" / "pod5" / run_id / "ori_round_pod5").glob("*.pod5"))


def test_feeder_aborts_when_cache_missing_after_progress(tmp_path: Path) -> None:
    run_id = f"CacheMissing_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    source_pod5 = input_dir / "sample.pod5"
    source_pod5.write_bytes(b"pod5-cache")

    env = make_pod5_stub_env(
        tmp_path,
        inspect_counts={"sample.pod5": 2},
        view_rows={"sample.pod5": ["r1\tfile1", "r2\tfile1"]},
    )

    metadata_dir = tmp_path / "results" / "pod5" / run_id / "metadata"
    metadata_dir.mkdir(parents=True, exist_ok=True)
    archived_dir = tmp_path / "results" / "pod5" / run_id / "full_pod5"
    archived_dir.mkdir(parents=True, exist_ok=True)
    archived_pod5 = archived_dir / "sample.pod5"
    archived_pod5.write_bytes(b"pod5-cache")
    (metadata_dir / "progress_reads_sample.pod5.count").write_text("1\n", encoding="utf-8")

    result = subprocess.run(
        [
            "bash",
            str(FEEDER),
            "--run_id",
            run_id,
            "--input_folder",
            str(input_dir),
            "--sleep_time",
            "1",
            "--num_reads",
            "10",
            "--targets",
            "COI|ITS2",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    text = result.stdout + result.stderr
    assert result.returncode != 0
    assert "missing or incomplete fullview cache state" in text
    assert not any((tmp_path / "results" / "pod5" / run_id / "ori_round_pod5").glob("*.pod5"))


def test_rtbioscan_wrapper_refuses_duplicate_feeder_start_without_truncating_log(tmp_path: Path) -> None:
    run_id = f"WrapperRun_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    feeder_log.parent.mkdir(parents=True, exist_ok=True)
    feeder_log.write_text("sentinel-log\n", encoding="utf-8")

    proc = launch_feeder(REPO_ROOT, run_id, input_dir)
    lock_meta = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir" / "meta.env"
    try:
        wait_for(lock_meta.exists)
        result = subprocess.run(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={**os.environ, "TERM": "dumb"},
            check=False,
        )
        text = result.stdout + result.stderr
        assert result.returncode != 0
        assert "another feeder is already active" in text
        assert feeder_log.read_text(encoding="utf-8") == "sentinel-log\n"
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_log, encoding="utf-8")


def test_rtbioscan_wrapper_process_group_sigint_stops_feeder_and_removes_lock(tmp_path: Path) -> None:
    run_id = f"WrapperInt_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    wrapper_log = tmp_path / "wrapper-int.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "1",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for(lock_dir.exists)
        wait_for(lambda: any(args and args[0] == "run" for args in read_nextflow_invocations(nextflow_log)))
        os.killpg(proc.pid, signal.SIGINT)
        assert wait_for_process_exit(proc, timeout=10) == 130
        wait_for(lambda: not lock_dir.exists())
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_wrapper_sigterm_stops_feeder_and_removes_lock(tmp_path: Path) -> None:
    run_id = f"WrapperTerm_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    wrapper_log = tmp_path / "wrapper-term.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "1",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for(lock_dir.exists)
        wait_for(lambda: any(args and args[0] == "run" for args in read_nextflow_invocations(nextflow_log)))
        proc.terminate()
        assert wait_for_process_exit(proc, timeout=10) == 143
        wait_for(lambda: not lock_dir.exists())
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_wrapper_sigint_stops_long_sleep_feeder_promptly(tmp_path: Path) -> None:
    run_id = f"WrapperIntLong_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    wrapper_log = tmp_path / "wrapper-int-long.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "30",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for(lock_dir.exists)
        wait_for(lambda: any(args and args[0] == "run" for args in read_nextflow_invocations(nextflow_log)))
        os.killpg(proc.pid, signal.SIGINT)
        assert wait_for_process_exit(proc, timeout=10) == 130
        wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_wrapper_sigterm_stops_long_sleep_feeder_promptly(tmp_path: Path) -> None:
    run_id = f"WrapperTermLong_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    wrapper_log = tmp_path / "wrapper-term-long.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "30",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for(lock_dir.exists)
        wait_for(lambda: any(args and args[0] == "run" for args in read_nextflow_invocations(nextflow_log)))
        proc.terminate()
        assert wait_for_process_exit(proc, timeout=10) == 143
        wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_wrapper_sigint_stops_long_sleep_feeder_when_pgrep_fails(tmp_path: Path) -> None:
    run_id = f"WrapperIntPgrep_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    env = prepend_broken_pgrep_shim(env, tmp_path)
    wrapper_log = tmp_path / "wrapper-int-pgrep.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "30",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for(lock_dir.exists)
        wait_for(lambda: any(args and args[0] == "run" for args in read_nextflow_invocations(nextflow_log)))
        os.killpg(proc.pid, signal.SIGINT)
        assert wait_for_process_exit(proc, timeout=10) == 130
        wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


@pytest.mark.xfail(
    reason="Reliable second-signal pipeline escalation in the bash wrapper needs a policy decision on fallback escalation timing.",
    strict=False,
)
def test_rtbioscan_wrapper_second_sigint_escalates_stubborn_pipeline(tmp_path: Path) -> None:
    run_id = f"WrapperIntTwice_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="ignore_once")
    wrapper_log = tmp_path / "wrapper-int-twice.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "30",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for(lock_dir.exists)
        wait_for(lambda: any(args and args[0] == "run" for args in read_nextflow_invocations(nextflow_log)))
        proc.send_signal(signal.SIGINT)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        assert wait_for_process_exit(proc, timeout=10) == 130
        wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_wrapper_tracks_real_feeder_pid(tmp_path: Path) -> None:
    run_id = f"WrapperPid_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    wrapper_log = tmp_path / "wrapper-pid.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_meta = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir" / "meta.env"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(WRAPPER),
                "--feeder",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "1",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    try:
        wait_for(lock_meta.exists)
        wait_for(lambda: any(args and args[0] == "run" for args in read_nextflow_invocations(nextflow_log)))
        wait_for(lambda: "Feeder started (PID " in wrapper_log.read_text(encoding="utf-8"))
        wrapper_text = wrapper_log.read_text(encoding="utf-8")
        marker = "Feeder started (PID "
        wrapper_pid = wrapper_text.split(marker, 1)[1].split(")", 1)[0]
        lock_pid = next(
            line.split("=", 1)[1]
            for line in lock_meta.read_text(encoding="utf-8").splitlines()
            if line.startswith("pid=")
        )
        assert wrapper_pid == lock_pid
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            wait_for_process_exit(proc, timeout=10)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_pipeline_only_refuses_live_feeder_lock_before_nextflow_start(tmp_path: Path) -> None:
    run_id = f"PipeOnly_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    proc = launch_feeder(REPO_ROOT, run_id, input_dir)
    lock_meta = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir" / "meta.env"
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    try:
        wait_for(lock_meta.exists)
        result = subprocess.run(
            [
                "bash",
                str(WRAPPER),
                "--run_id",
                run_id,
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        text = result.stdout + result.stderr
        assert result.returncode != 0
        assert "another feeder is already active" in text
        assert read_nextflow_invocations(nextflow_log) == []
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)


def test_rtbioscan_do_metadata_refuses_live_feeder_lock(tmp_path: Path) -> None:
    run_id = f"DoMeta_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    proc = launch_feeder(REPO_ROOT, run_id, input_dir)
    lock_meta = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir" / "meta.env"
    env, nextflow_log = make_nextflow_stub_env(tmp_path, run_mode="sleep")
    fixtures = REPO_ROOT / "tests" / "fixtures" / "metadata"
    sample_info_dir = REPO_ROOT / "results" / "sample_info" / run_id
    try:
        wait_for(lock_meta.exists)
        result = subprocess.run(
            [
                "bash",
                str(WRAPPER),
                "--do_metadata",
                "--run_id",
                run_id,
                "--metadata",
                str(fixtures / "pipeline_info.tsv"),
                "--general_fasta",
                str(fixtures / "general.fasta"),
                "--primers_fasta",
                str(fixtures / "primers.fasta"),
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        text = result.stdout + result.stderr
        assert result.returncode != 0
        assert "another feeder is already active" in text
        assert read_nextflow_invocations(nextflow_log) == []
        assert not sample_info_dir.exists()
    finally:
        stop_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        shutil.rmtree(sample_info_dir, ignore_errors=True)
