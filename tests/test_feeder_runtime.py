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
        "import os, time\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"INSPECT_COUNTS = {json.dumps(inspect_counts)}\n"
        f"VIEW_ROWS = {json.dumps(view_rows)}\n"
        + _POD5_TWO_OPEN_CONTRACT
        + "args = sys.argv[1:]\n"
        "if not args:\n"
        "    raise SystemExit(99)\n"
        "def key_for(path):\n"
        "    return Path(path).name\n"
        "if args[:2] == ['inspect', 'summary']:\n"
        "    key = key_for(args[2])\n"
        "    if key not in INSPECT_COUNTS:\n"
        "        count = inspect_filtered(Path(args[2]))\n"
        "    else:\n"
        "        count = int(INSPECT_COUNTS[key])\n"
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
        "        write_filtered(Path(output_path), Path(ids_path))\n"
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
        f"dummy-slice\t{source_fp}\tsample.pod5\t1\t2\t{pod5_root.parent / 'PriorRun'}\t1\n",
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
        # File removal precedes its log/ledger records. Let startup finish before
        # SIGTERM, especially now that it also reclaims publication temporaries.
        wait_for((meta_dir / "round_commit_ledger.tsv").exists, timeout=5)
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


# Synthetic, non-biological payload. Like pod5 0.3.10, the writer closes the
# signal stream and then reopens the SAME pathname in append mode for the footer.
_POD5_TWO_OPEN_CONTRACT = r'''
def event(phase, path):
    control = os.environ.get('S1F_CONTROL')
    if not control:
        return
    control = Path(control)
    with (control / 'events').open('a') as stream:
        stream.write(json.dumps([phase, str(path)]) + '\n')
    if os.environ.get('S1F_PAUSE') == phase:
        (control / phase).write_text(str(path))
        deadline = time.monotonic() + 30
        while not (control / ('release_' + phase)).exists():
            if time.monotonic() > deadline:
                raise SystemExit(97)
            time.sleep(.01)

def write_filtered(path, ids_path):
    event('output', path)
    if path.suffix != '.pod5':
        raise SystemExit('writer requires a .pod5 suffix')
    ids = ids_path.read_text().splitlines()
    if os.environ.get('S1F_MODE') == 'mismatch':
        ids = ids[:1]
    if os.environ.get('S1F_MODE') == 'zero':
        ids = []
    signals = {rid: [idx, -idx, 17] for idx, rid in enumerate(ids, 1)}
    with path.open('w') as stream:
        stream.write(json.dumps({'signals': signals}) + '\n')
        stream.flush()
        event('writing', path)
    event('signal_closed', path)
    if os.environ.get('S1F_MODE') == 'filter_fail':
        raise SystemExit(2)
    with path.open('a') as stream:
        stream.write(json.dumps({'ids': ids, 'footer': 'complete'}) + '\n')
    event('closed', path)

def inspect_filtered(path):
    event('inspect', path)
    mode = os.environ.get('S1F_MODE')
    if mode == 'validation_fail':
        # Valid-looking stdout must not override unsuccessful command status.
        print('Found 1 batches, 2 reads')
        raise SystemExit(2)
    if mode == 'malformed':
        print('Found 1 batches, unknown reads')
        raise SystemExit(0)
    try:
        signal_part, footer = map(json.loads, path.read_text().splitlines())
        assert footer['footer'] == 'complete'
        assert set(footer['ids']) == set(signal_part['signals'])
    except (ValueError, KeyError, AssertionError):
        raise SystemExit(2)
    event('validated', path)
    return len(footer['ids'])
'''


_S1F_BASE = 'fce55e8af19051e6f1f90fa2260fe0821db3e35f'


def _s1f_setup(root: Path, *, mode: str = '', pause: str = '') -> dict[str, str]:
    root.mkdir(parents=True, exist_ok=True)
    (root / 'input').mkdir()
    (root / 'control').mkdir()
    source = root / 'input' / 'sample.pod5'
    source.write_bytes(b'synthetic-source')
    os.utime(source, (1700000000, 1700000000))
    env = make_pod5_stub_env(root, inspect_counts={'sample.pod5': 2},
                             view_rows={'sample.pod5': ['r1\tfile1', 'r2\tfile1']})
    env.update(S1F_CONTROL=str(root / 'control'), S1F_MODE=mode, S1F_PAUSE=pause)
    # Freeze only epoch timestamps so equal inputs give byte-identical records.
    date = root / 'fake-bin' / 'date'
    date.write_text('#!/bin/bash\nif [ "$*" = "+%s" ]; then echo 1700000000; else exec /bin/date "$@"; fi\n')
    date.chmod(0o755)
    mv = root / 'fake-bin' / 'mv'
    mv.write_text(
        '#!/usr/bin/env python3\nimport os, sys, time, json, subprocess\nfrom pathlib import Path\n'
        + _POD5_TWO_OPEN_CONTRACT + r'''
args = sys.argv[1:]
source, dest = map(Path, args[-2:])
publication = source.parent.name == 'ori_round_pod5' and dest.parent == source.parent
if publication:
    event('before_rename', source)
    if os.environ.get('S1F_MODE') == 'rename_fail':
        raise SystemExit(2)
status = subprocess.call(['/bin/mv'] + args)
if publication and status == 0:
    event('after_rename', dest)
raise SystemExit(status)
''')
    mv.chmod(0o755)
    return env


def _s1f_paths(root: Path) -> tuple[Path, Path, Path]:
    base = root / 'results' / 'pod5' / 'S1F_Run'
    return base / 'ori_round_pod5', base / 'reads_rt_round_pod5', base / 'metadata'


def _s1f_start(root: Path, env: dict[str, str], script: Path | None = None):
    script = script or Path(os.environ.get('S1F_FEEDER', str(FEEDER)))
    log = (root / 'feeder-output').open('a')
    try:
        return subprocess.Popen(
            ['/bin/bash', str(script), '--run_id', 'S1F_Run', '--input_folder', str(root / 'input'),
             '--sleep_time', '1', '--num_reads', '2', '--targets', 'COI|ITS2'],
            cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    finally:
        log.close()


def _s1f_kill(proc) -> None:
    # Kill the whole isolated process group, including a writer/rename at a gate.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def _s1f_gate(root: Path, phase: str) -> Path:
    marker = root / 'control' / phase
    wait_for(marker.exists, timeout=15)
    path = Path(marker.read_text())
    return path if path.is_absolute() else root / path


def _s1f_no_commit(root: Path) -> None:
    _, _, meta = _s1f_paths(root)
    assert not list(meta.glob('*_slice.tsv')), 'sidecar advanced before publication'
    assert not list(meta.glob('progress_reads_*.count')), 'progress advanced before publication'
    assert not list(meta.glob('*_read_info_rpt.txt')), 'metadata advanced before publication'
    ledger = meta / 'round_commit_ledger.tsv'
    assert not ledger.exists() or not ledger.read_text().strip(), 'ledger advanced before publication'


def _s1f_payload(path: Path, expected_ids=('r1', 'r2')) -> None:
    # Independent oracle: both serialized sections and exact signal vectors,
    # rather than trusting the fake's summary/count alone.
    signals, footer = [json.loads(line) for line in path.read_text().splitlines()]
    assert footer == {'ids': list(expected_ids), 'footer': 'complete'}
    assert signals == {'signals': {rid: [idx, -idx, 17] for idx, rid in enumerate(expected_ids, 1)}}


def _s1f_committed(root: Path, env: dict[str, str]) -> Path:
    spool, ready, meta = _s1f_paths(root)
    wait_for(lambda: len(list(ready.glob('S1F_Run_*.pod5'))) == 1, timeout=15)
    final = next(ready.glob('S1F_Run_*.pod5'))
    assert not list(spool.iterdir()), 'publication left a temporary or duplicate spool file'
    assert (meta / 'progress_reads_sample.pod5.count').read_text() == '2\n'
    ledger = (meta / 'round_commit_ledger.tsv').read_text().splitlines()
    assert len(ledger) == 1 and ledger[0].endswith('\t' + final.stem)
    result = subprocess.run(['pod5', 'inspect', 'summary', str(final)], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    count = len(json.loads(final.read_text().splitlines()[1])['ids'])
    assert result.stdout.rstrip().endswith(f', {count} reads')
    return final


def _s1f_function(script: Path, name: str) -> str:
    return name + '() {' + script.read_text().split(name + '() {', 1)[1].split('\n}\n', 1)[0] + '\n}\n'


def test_s1f_atomic_visibility_and_order(tmp_path: Path) -> None:
    env = _s1f_setup(tmp_path, pause='writing')
    proc = _s1f_start(tmp_path, env)
    spool, ready, _ = _s1f_paths(tmp_path)
    try:
        partial = _s1f_gate(tmp_path, 'writing')
        assert partial.name == '.S1F_Run_0.partial.pod5', 'writer output must be hidden and POD5-suffixed'
        assert partial.parent == spool
        assert list(spool.iterdir()) == [partial]
        assert not (spool / 'S1F_Run_0.pod5').exists(), 'incomplete final became visible'
        _s1f_no_commit(tmp_path)
        # Execute the existing feeder scanner/publisher and the pipeline's shell
        # glob discovery while the writer is paused; neither may select partial.
        code = ''.join(_s1f_function(FEEDER, name) for name in
                       ('count_pod5_files', 'pod5_mtime_epoch', 'publish_pending_round'))
        code += '\noutput_folder=$1; output_rt=$2; count_pod5_files "$1"; publish_pending_round\n'
        code += 'for f in "$1"/*.pod5; do [ ! -f "$f" ] || printf "VISIBLE:%s\\n" "$f"; done\n'
        result = subprocess.run(['/bin/bash', '-c', code, 'scan', str(spool), str(ready)], capture_output=True, text=True)
        assert result.stdout.strip() == '0', 'ready discovery selected incomplete output'
        assert not list(ready.iterdir())
        (tmp_path / 'control' / 'release_writing').touch()
        final = _s1f_committed(tmp_path, env)
        _s1f_payload(final)
        events = [json.loads(line)[0] for line in (tmp_path / 'control' / 'events').read_text().splitlines()]
        assert events.index('closed') < events.index('validated') < events.index('before_rename') < events.index('after_rename')
    finally:
        _s1f_kill(proc)


@pytest.mark.parametrize('mode', ['filter_fail', 'validation_fail', 'malformed', 'zero', 'rename_fail'])
def test_s1f_failures_remain_retryable(tmp_path: Path, mode: str) -> None:
    env = _s1f_setup(tmp_path, mode=mode)
    proc = _s1f_start(tmp_path, env)
    spool, ready, _ = _s1f_paths(tmp_path)
    try:
        wait_for(lambda: 'round emission aborted.' in (tmp_path / 'feeder-output').read_text()
                 or list(spool.glob('S1F_Run_*.pod5')) or list(ready.glob('*.pod5')), timeout=15)
        assert not list(spool.glob('S1F_Run_*.pod5')), 'failed output was published'
        assert not list(ready.glob('*.pod5')), 'failed output reached ready queue'
        _s1f_no_commit(tmp_path)
        wait_for(lambda: not list(spool.glob('.*.partial.pod5')))
    finally:
        _s1f_kill(proc)
    env['S1F_MODE'] = ''
    proc = _s1f_start(tmp_path, env)
    try:
        _s1f_payload(_s1f_committed(tmp_path, env))
    finally:
        _s1f_kill(proc)


@pytest.mark.parametrize('phase', ['writing', 'before_rename', 'after_rename'])
def test_s1f_interruption_reconciles(tmp_path: Path, phase: str) -> None:
    env = _s1f_setup(tmp_path, pause=phase)
    proc = _s1f_start(tmp_path, env)
    spool, ready, meta = _s1f_paths(tmp_path)
    try:
        paused = _s1f_gate(tmp_path, phase)
        _s1f_no_commit(tmp_path)
        if phase == 'after_rename':
            assert paused == spool / 'S1F_Run_0.pod5'
            _s1f_payload(paused)
        else:
            assert not list(spool.glob('S1F_Run_*.pod5')), 'final visible before validated rename'
        assert not list(ready.iterdir())
    finally:
        _s1f_kill(proc)
    env['S1F_PAUSE'] = ''
    proc = _s1f_start(tmp_path, env)
    try:
        final = _s1f_committed(tmp_path, env)
        _s1f_payload(final)
        assert final.stem == 'S1F_Run_1', 'existing round reservation semantics changed'
        assert not paused.exists()
        if phase == 'after_rename':
            assert 'startup_orphan_cleanup' in (meta / 'S1F_Run_feeder.log').read_text()
        # One slice remains consumed across a further restart; no duplicate ledger.
    finally:
        _s1f_kill(proc)
    (tmp_path / 'feeder-output').write_text('')
    proc = _s1f_start(tmp_path, env)
    try:
        wait_for(lambda: 'Buffered unread reads=0' in (tmp_path / 'feeder-output').read_text(), timeout=15)
        assert _s1f_committed(tmp_path, env) == final
    finally:
        _s1f_kill(proc)


def test_s1f_count_mismatch_warns_once(tmp_path: Path) -> None:
    env = _s1f_setup(tmp_path, mode='mismatch')
    proc = _s1f_start(tmp_path, env)
    try:
        final = _s1f_committed(tmp_path, env)
        _s1f_payload(final, ('r1',))
        warning = 'WARNING: round S1F_Run_0 authoritative read count (1) differs from selected ID count (2); publishing validated output.'
        assert (tmp_path / 'feeder-output').read_text().count(warning) == 1
        # The pre-existing ledger is a selected-slice contract; validation must
        # not change its read_count or its progress ranges to the output count.
        _, _, meta = _s1f_paths(tmp_path)
        sidecar = (meta / 'S1F_Run_0_slice.tsv').read_text()
        assert int(sidecar.split('# read_count=', 1)[1].splitlines()[0]) == 2
    finally:
        _s1f_kill(proc)


def test_s1f_startup_sweep_boundaries(tmp_path: Path) -> None:
    spool = tmp_path / 'spool'
    ready = tmp_path / 'ready'
    meta = tmp_path / 'metadata'
    for directory in (spool, ready, meta):
        directory.mkdir()
    stale = spool / '.S1F_Run_4.partial.pod5'
    keep = [spool / 'S1F_Run_3.pod5', spool / '.unrelated',
            spool / '.S1F_Run_other_4.partial.pod5', spool / '.S1F_Run_4x.partial.pod5',
            spool / '.S1F_Run_.partial.pod5', spool / '.S1F_Run_4.partial.pod5.extra',
            ready / '.S1F_Run_4.partial.pod5', tmp_path / '.S1F_Run_4.partial.pod5']
    for path in [stale, *keep]:
        path.write_text('keep')
    symlink = spool / '.S1F_Run_5.partial.pod5'
    symlink.symlink_to(keep[-1])
    # Preserve a completed round under the existing orphan-reconciliation rules.
    (meta / 'S1F_Run_3_slice.tsv').write_text('# committed\n')
    script = Path(os.environ.get('S1F_FEEDER', str(FEEDER)))
    code = _s1f_function(script, 'startup_cleanup_orphan_rounds')
    code += '\nslice_sidecar_path_for_round() { printf "%s/%s_slice.tsv" "$metadata" "$1"; }\n'
    code += 'feeder_log_event() { :; }\nrun_id=S1F_Run; output_folder=$1; output_rt=$2; metadata=$3\nstartup_cleanup_orphan_rounds\n'
    result = subprocess.run(['/bin/bash', '-c', code, 'cleanup', str(spool), str(ready), str(meta)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert all(path.read_text() == 'keep' for path in keep)
    assert symlink.is_symlink()


def _s1f_baseline(tmp_path: Path) -> Path:
    script = tmp_path / 'baseline.sh'
    script.write_bytes(subprocess.check_output(['git', 'show', f'{_S1F_BASE}:bin/Metadata_pod5_processing.sh'], cwd=REPO_ROOT))
    return script


def test_s1f_success_matches_immutable_base(tmp_path: Path) -> None:
    baseline = _s1f_baseline(tmp_path)
    records = []
    for label, script in [('before', baseline), ('after', None)]:
        root = tmp_path / label
        env = _s1f_setup(root)
        proc = _s1f_start(root, env, script)
        try:
            final = _s1f_committed(root, env)
            assert final.name == 'S1F_Run_0.pod5'
            _s1f_payload(final)
            _, _, meta = _s1f_paths(root)
            names = ['S1F_Run_0_slice.tsv', 'progress_reads_sample.pod5.count',
                     'S1F_Run_0_read_info_rpt.txt', 'round_commit_ledger.tsv',
                     'S1F_Run_reads_time_rpt.txt', 'next_round_id.count']
            records.append({name: (meta / name).read_bytes() for name in names})
        finally:
            _s1f_kill(proc)
    assert records[0] == records[1], 'successful commit records changed'


def test_s1f_baseline_race_splits_writer_and_advances_progress(tmp_path: Path) -> None:
    baseline = _s1f_baseline(tmp_path)
    root = tmp_path / 'race'
    env = _s1f_setup(root, pause='writing')
    proc = _s1f_start(root, env, baseline)
    try:
        visible = _s1f_gate(root, 'writing')
        assert visible.name == 'S1F_Run_0.pod5'
        assert len(visible.read_text().splitlines()) == 1
        # Concurrent promotion between the two opens leaves permanent fragments.
        moved = root / 'consumer.pod5'
        visible.rename(moved)
        (root / 'control' / 'release_writing').touch()
        _, ready, meta = _s1f_paths(root)
        wait_for(lambda: (meta / 'progress_reads_sample.pod5.count').exists(), timeout=15)
        wait_for(lambda: (ready / 'S1F_Run_0.pod5').exists())
        assert (meta / 'progress_reads_sample.pod5.count').read_text() == '2\n'
        for fragment in [moved, ready / 'S1F_Run_0.pod5']:
            result = subprocess.run(['pod5', 'inspect', 'summary', str(fragment)], env=env, capture_output=True)
            assert result.returncode != 0, 'split fragment unexpectedly validates'
    finally:
        _s1f_kill(proc)


def test_s1f_writer_requires_pod5_suffix(tmp_path: Path) -> None:
    env = _s1f_setup(tmp_path, pause='output')
    proc = _s1f_start(tmp_path, env)
    try:
        output = _s1f_gate(tmp_path, 'output')
        assert output.suffix == '.pod5', 'writer output lost required .pod5 suffix'
        (tmp_path / 'control' / 'release_output').touch()
        _s1f_payload(_s1f_committed(tmp_path, env))
    finally:
        _s1f_kill(proc)


def test_s1f_does_not_replace_existing_final(tmp_path: Path) -> None:
    env = _s1f_setup(tmp_path, pause='writing')
    proc = _s1f_start(tmp_path, env)
    spool, _, _ = _s1f_paths(tmp_path)
    try:
        _s1f_gate(tmp_path, 'writing')
        final = spool / 'S1F_Run_0.pod5'
        final.write_bytes(b'existing final must survive')
        (tmp_path / 'control' / 'release_writing').touch()
        wait_for(lambda: 'failed to publish pod5' in (tmp_path / 'feeder-output').read_text(), timeout=15)
        assert final.read_bytes() == b'existing final must survive'
        _s1f_no_commit(tmp_path)
        wait_for(lambda: not list(spool.glob('.*.partial.pod5')))
    finally:
        _s1f_kill(proc)
