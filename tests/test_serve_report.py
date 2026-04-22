import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVE_REPORT = REPO_ROOT / "bin" / "serve_report.sh"
RTBIOSCAN = REPO_ROOT / "RTBioScan.sh"


def _wait_for(condition, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met before timeout")

def _terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _install_open_shim(tmp_path: Path) -> tuple[dict[str, str], Path]:
    shim_dir = tmp_path / "shim-bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "open_calls.log"
    open_shim = shim_dir / "open"
    open_shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "printf '%s\\n' \"$*\" >> \"$OPEN_SHIM_LOG\"\n",
        encoding="utf-8",
    )
    open_shim.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    env["OPEN_SHIM_LOG"] = str(log_path)
    return env, log_path


def _merge_env(base: dict[str, str], extra: dict[str, str]) -> dict[str, str]:
    merged = base.copy()
    for key, value in extra.items():
        if key == "PATH":
            extra_prefix = value.split(os.pathsep, 1)[0]
            merged["PATH"] = f"{extra_prefix}{os.pathsep}{merged['PATH']}"
        else:
            merged[key] = value
    return merged


def _install_python_shim(tmp_path: Path, *, mode: str) -> tuple[dict[str, str], Path]:
    shim_dir = tmp_path / "python-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    marker_path = tmp_path / "python_server.log"
    shim_path = shim_dir / "python3"
    shim_path.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "script=\"$(cat)\"\n"
        "mark_ready() {\n"
        "  if [ -n \"${PYTHON_SHIM_MARKER:-}\" ]; then\n"
        "    printf 'server_started\\n' >> \"$PYTHON_SHIM_MARKER\"\n"
        "  fi\n"
        "  if [ -n \"${RTBIOSCAN_READY_FILE:-}\" ]; then\n"
        "    mkdir -p \"$(dirname \"$RTBIOSCAN_READY_FILE\")\"\n"
        "    printf '127.0.0.1\\t0\\n' > \"$RTBIOSCAN_READY_FILE\"\n"
        "  fi\n"
        "}\n"
        "case \"$script\" in\n"
        "  *\"import http.server\"*)\n"
        "    case \"${PYTHON_SHIM_SERVER_MODE:-delegate}\" in\n"
        "      hold)\n"
        "        mark_ready\n"
        "        trap 'exit 0' TERM INT\n"
        "        while :; do\n"
        "          sleep 1\n"
        "        done\n"
        "        ;;\n"
        "      reject_bracketed_ipv6_bind)\n"
        "        case \"$script\" in\n"
        "          *'host = \"['*)\n"
        "            printf 'Traceback (most recent call last):\\n' >&2\n"
        "            printf 'socket.gaierror: [Errno 8] nodename nor servname provided, or not known\\n' >&2\n"
        "            exit 1\n"
        "            ;;\n"
        "          *)\n"
        "            mark_ready\n"
        "            trap 'exit 0' TERM INT\n"
        "            while :; do\n"
        "              sleep 1\n"
        "            done\n"
        "            ;;\n"
        "        esac\n"
        "        ;;\n"
        "      busy_8000)\n"
        "        case \"$script\" in\n"
        "          *\"port = int(\\\"8000\\\")\"*)\n"
        "            printf 'Traceback (most recent call last):\\n' >&2\n"
        "            printf 'OSError: [Errno 48] Address already in use\\n' >&2\n"
        "            exit 1\n"
        "            ;;\n"
        "          *)\n"
        "            mark_ready\n"
        "            trap 'exit 0' TERM INT\n"
        "            while :; do\n"
        "              sleep 1\n"
        "            done\n"
        "            ;;\n"
        "        esac\n"
        "        ;;\n"
        "      busy)\n"
        "        printf 'Traceback (most recent call last):\\n' >&2\n"
        "        printf 'OSError: [Errno 48] Address already in use\\n' >&2\n"
        "        exit 1\n"
        "        ;;\n"
        "      *)\n"
        "        printf '%s' \"$script\" | \"$PYTHON_REAL\" \"$@\"\n"
        "        ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *)\n"
        "    printf '%s' \"$script\" | \"$PYTHON_REAL\" \"$@\"\n"
        "    ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    env["PYTHON_REAL"] = sys.executable
    env["PYTHON_SHIM_SERVER_MODE"] = mode
    env["PYTHON_SHIM_MARKER"] = str(marker_path)
    return env, marker_path


def _install_nextflow_shim(tmp_path: Path, *, run_exit: int = 0, run_mode: str = "exit") -> dict[str, str]:
    shim_dir = tmp_path / "nextflow-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "nextflow"
    shim_path.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "\n"
        "subcmd = ''\n"
        "for arg in sys.argv[1:]:\n"
        "    if arg in ('config', 'run'):\n"
        "        subcmd = arg\n"
        "        break\n"
        "\n"
        "if subcmd == 'config':\n"
        "    sys.stdout.write(os.environ.get('NEXTFLOW_SHIM_CONFIG_STDOUT', \"params.targets = 'COI|ITS2'\\n\"))\n"
        "    raise SystemExit(0)\n"
        "\n"
        "if subcmd == 'run':\n"
        "    mode = os.environ.get('NEXTFLOW_SHIM_RUN_MODE', 'exit')\n"
        "    exit_code = int(os.environ.get('NEXTFLOW_SHIM_RUN_EXIT', '0'))\n"
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
        "\n"
        "raise SystemExit(98)\n",
        encoding="utf-8",
    )
    shim_path.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{shim_dir}:{env['PATH']}"
    env["NEXTFLOW_SHIM_RUN_EXIT"] = str(run_exit)
    env["NEXTFLOW_SHIM_RUN_MODE"] = run_mode
    return env


def test_serve_report_missing_report_fails_by_default(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", str(SERVE_REPORT), "--dir", str(tmp_path), "--port", "8000"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "ERROR: report not found:" in result.stderr


def test_serve_report_wait_for_report_opens_once_report_created(tmp_path: Path) -> None:
    env, open_log = _install_open_shim(tmp_path)
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    proc = subprocess.Popen(
        [
            "bash",
            str(SERVE_REPORT),
            "--dir",
            str(tmp_path),
            "--port",
            "8001",
            "--wait-for-report",
            "--open",
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for(lambda: proc.poll() is None and marker_path.exists())
        report = tmp_path / "report_html" / "report.html"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text("<html><body>ready</body></html>\n", encoding="utf-8")
        _wait_for(lambda: open_log.exists() and "http://127.0.0.1:8001/report_html/report.html" in open_log.read_text(encoding="utf-8"))
    finally:
        _terminate_process(proc)


def test_serve_report_wait_for_report_open_all_waits_for_delayed_run_report(tmp_path: Path) -> None:
    env, open_log = _install_open_shim(tmp_path)
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    proc = subprocess.Popen(
        [
            "bash",
            str(SERVE_REPORT),
            "--dir",
            str(tmp_path),
            "--port",
            "8004",
            "--wait-for-report",
            "--open-all",
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for(lambda: proc.poll() is None and marker_path.exists())
        (tmp_path / "report_html" / "report.html").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "report_html" / "report.html").write_text("<html>index</html>\n", encoding="utf-8")
        time.sleep(1.2)
        run_dir = tmp_path / "report_html" / "runs" / "run-a"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.html").write_text("<html>run</html>\n", encoding="utf-8")
        _wait_for(
            lambda: open_log.exists()
            and "http://127.0.0.1:8004/report_html/runs/run-a/report.html" in open_log.read_text(encoding="utf-8")
            and open_log.read_text(encoding="utf-8").splitlines()[-1] == "http://127.0.0.1:8004/report_html/report.html"
        )
    finally:
        _terminate_process(proc)


@pytest.mark.parametrize(
    ("flag_args", "expected_calls"),
    [
        (["--open-all"], 3),
        (["--open-last", "1"], 2),
    ],
)
def test_serve_report_open_modes_do_not_require_open(
    tmp_path: Path,
    flag_args: list[str],
    expected_calls: int,
) -> None:
    env, log_path = _install_open_shim(tmp_path)
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    (tmp_path / "report_html" / "report.html").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "report_html" / "report.html").write_text("<html>index</html>\n", encoding="utf-8")
    (tmp_path / "report_html" / "runs" / "run-a").mkdir(parents=True, exist_ok=True)
    (tmp_path / "report_html" / "runs" / "run-b").mkdir(parents=True, exist_ok=True)
    (tmp_path / "report_html" / "runs" / "run-a" / "report.html").write_text("<html>a</html>\n", encoding="utf-8")
    time.sleep(1.1)
    (tmp_path / "report_html" / "runs" / "run-b" / "report.html").write_text("<html>b</html>\n", encoding="utf-8")

    proc = subprocess.Popen(
        [
            "bash",
            str(SERVE_REPORT),
            "--dir",
            str(tmp_path),
            "--port",
            "8002",
            "--quiet",
            *flag_args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for(
            lambda: marker_path.exists()
            and log_path.exists()
            and len(log_path.read_text(encoding="utf-8").splitlines()) >= expected_calls
        )
        lines = log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == expected_calls
        assert lines[-1].endswith("http://127.0.0.1:8002/report_html/report.html")
    finally:
        _terminate_process(proc)


def test_serve_report_wait_open_helper_stops_after_process_exit(tmp_path: Path) -> None:
    env, log_path = _install_open_shim(tmp_path)
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    proc = subprocess.Popen(
        [
            "bash",
            str(SERVE_REPORT),
            "--dir",
            str(tmp_path),
            "--port",
            "8003",
            "--wait-for-report",
            "--open",
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        _wait_for(lambda: proc.poll() is None and marker_path.exists())
    finally:
        _terminate_process(proc)

    (tmp_path / "report_html").mkdir(parents=True, exist_ok=True)
    (tmp_path / "report_html" / "report.html").write_text("<html>late</html>\n", encoding="utf-8")
    time.sleep(2.0)
    if log_path.exists():
        assert log_path.read_text(encoding="utf-8").strip() == ""


def test_rtbioscan_reports_port_conflict_using_fresh_server_log(tmp_path: Path) -> None:
    env = _install_nextflow_shim(tmp_path, run_exit=0)
    py_env, _ = _install_python_shim(tmp_path, mode="busy")
    env = _merge_env(env, py_env)
    serve_dir = tmp_path / "served"
    serve_dir.mkdir(parents=True, exist_ok=True)
    port = 8123
    result = subprocess.run(
        [
            "bash",
            str(RTBIOSCAN),
            "--serve",
            "--serve-port",
            str(port),
            "--serve-dir",
            str(serve_dir),
            "-resume",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    assert f"port {port} is already in use" in result.stderr
    server_log = serve_dir / "server.log"
    assert server_log.exists()
    assert "Address already in use" in server_log.read_text(encoding="utf-8")


def test_rtbioscan_auto_picks_free_default_port_and_writes_url_files(tmp_path: Path) -> None:
    env = _install_nextflow_shim(tmp_path, run_exit=0)
    py_env, _ = _install_python_shim(tmp_path, mode="busy_8000")
    env = _merge_env(env, py_env)
    serve_dir = tmp_path / "served"
    serve_dir.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "bash",
            str(RTBIOSCAN),
            "--serve",
            "--serve-dir",
            str(serve_dir),
            "-resume",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0
    server_port = serve_dir / "server.port"
    server_url = serve_dir / "server.url"
    assert server_port.exists()
    assert server_url.exists()
    chosen_port = server_port.read_text(encoding="utf-8").strip()
    assert chosen_port == "8001"
    assert server_url.read_text(encoding="utf-8").strip() == f"http://127.0.0.1:{chosen_port}/report_html/report.html"
    assert f"http://127.0.0.1:{chosen_port}/report_html/report.html" in result.stdout


def test_rtbioscan_wrapper_sigint_stops_feeder_and_report_server_promptly(tmp_path: Path) -> None:
    run_id = f"ServeWrapperInt_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    serve_dir = tmp_path / "served"
    serve_dir.mkdir(parents=True, exist_ok=True)
    env = _install_nextflow_shim(tmp_path, run_mode="sleep")
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "wrapper-serve-int.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(RTBIOSCAN),
                "--feeder",
                "--serve",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "30",
                "--serve-dir",
                str(serve_dir),
                "--serve-port",
                "8006",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for(
            lambda: lock_dir.exists()
            and marker_path.exists()
            and "Report server started" in wrapper_log.read_text(encoding="utf-8")
        )
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 130
        _wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
        assert text.count("Stopping report server") == 1
        assert text.count("Feeder stopped.") == 1
        assert text.count("Report server stopped.") == 1
    finally:
        _terminate_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_wrapper_sigterm_stops_feeder_and_report_server_promptly(tmp_path: Path) -> None:
    run_id = f"ServeWrapperTerm_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    serve_dir = tmp_path / "served"
    serve_dir.mkdir(parents=True, exist_ok=True)
    env = _install_nextflow_shim(tmp_path, run_mode="sleep")
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "wrapper-serve-term.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(RTBIOSCAN),
                "--feeder",
                "--serve",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "30",
                "--serve-dir",
                str(serve_dir),
                "--serve-port",
                "8007",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for(
            lambda: lock_dir.exists()
            and marker_path.exists()
            and "Report server started" in wrapper_log.read_text(encoding="utf-8")
        )
        proc.terminate()
        assert proc.wait(timeout=10) == 143
        _wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
        assert text.count("Stopping report server") == 1
        assert text.count("Feeder stopped.") == 1
        assert text.count("Report server stopped.") == 1
    finally:
        _terminate_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_rtbioscan_wrapper_second_sigint_escalates_pipeline_with_report_server(tmp_path: Path) -> None:
    run_id = f"ServeWrapperIntTwice_{tmp_path.name}"
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    serve_dir = tmp_path / "served"
    serve_dir.mkdir(parents=True, exist_ok=True)
    env = _install_nextflow_shim(tmp_path, run_mode="ignore_once")
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "wrapper-serve-int-twice.log"
    feeder_log = REPO_ROOT / "results" / "feeder.log"
    original_feeder_log = feeder_log.read_text(encoding="utf-8") if feeder_log.exists() else None
    lock_dir = REPO_ROOT / "results" / "pod5" / run_id / "metadata" / ".feeder.lockdir"

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            [
                "bash",
                str(RTBIOSCAN),
                "--feeder",
                "--serve",
                "--run_id",
                run_id,
                "--input_folder",
                str(input_dir),
                "--sleep_time",
                "30",
                "--serve-dir",
                str(serve_dir),
                "--serve-port",
                "8008",
                "--targets",
                "COI|ITS2",
            ],
            cwd=tmp_path,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for(
            lambda: lock_dir.exists()
            and marker_path.exists()
            and "Report server started" in wrapper_log.read_text(encoding="utf-8")
        )
        proc.send_signal(signal.SIGINT)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 130
        _wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
        assert text.count("Stopping report server") == 1
    finally:
        _terminate_process(proc)
        shutil.rmtree(REPO_ROOT / "results" / "pod5" / run_id, ignore_errors=True)
        if original_feeder_log is None:
            feeder_log.unlink(missing_ok=True)
        else:
            feeder_log.write_text(original_feeder_log, encoding="utf-8")


def test_serve_report_normalizes_bracketed_ipv6_host_for_bind(tmp_path: Path) -> None:
    py_env, marker_path = _install_python_shim(tmp_path, mode="reject_bracketed_ipv6_bind")
    (tmp_path / "report_html").mkdir(parents=True, exist_ok=True)
    (tmp_path / "report_html" / "report.html").write_text("<html>index</html>\n", encoding="utf-8")
    proc = subprocess.Popen(
        [
            "bash",
            str(SERVE_REPORT),
            "--dir",
            str(tmp_path),
            "--port",
            "8005",
            "--host",
            "[::1]",
            "--quiet",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=py_env,
    )
    stderr = ""
    try:
        _wait_for(lambda: proc.poll() is None and marker_path.exists())
        _terminate_process(proc)
        _, stderr = proc.communicate()
        proc = None
        assert "Open: http://[::1]:8005/report_html/report.html" in stderr
    finally:
        if proc is not None:
            _terminate_process(proc)
