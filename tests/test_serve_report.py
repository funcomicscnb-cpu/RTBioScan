import json
import os
import secrets
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


def _read_nextflow_shim_identity(env: dict[str, str]) -> dict[str, object] | None:
    path = Path(env["NEXTFLOW_SHIM_IDENTITY_FILE"])
    try:
        identity = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return identity if isinstance(identity, dict) else None


def _wait_for_nextflow_shim_identity(
    env: dict[str, str], timeout: float = 10.0
) -> dict[str, object]:
    identity: dict[str, object] | None = None

    def _identity_ready() -> bool:
        nonlocal identity
        identity = _read_nextflow_shim_identity(env)
        observation = _read_nextflow_shim_observation(env)
        return (
            identity is not None
            and observation is not None
            and _nextflow_shim_identity_is_bound_to_env(identity, env)
            and _nextflow_shim_identity_agrees_with_observation(
                identity, observation
            )
            and _nextflow_shim_identity_matches(identity)
        )

    _wait_for(_identity_ready, timeout=timeout)
    assert identity is not None
    return identity


def _read_nextflow_shim_observation(
    env: dict[str, str],
) -> dict[str, object] | None:
    path = Path(env["NEXTFLOW_SHIM_OBSERVATION_FILE"])
    try:
        observation = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(observation, dict):
        return None
    pid = observation.get("pid")
    token = observation.get("token")
    shim_path = observation.get("shim_path")
    if (
        observation.get("schema") != 1
        or not isinstance(pid, int)
        or pid < 1
        or not isinstance(token, str)
        or not secrets.compare_digest(token, env["NEXTFLOW_SHIM_IDENTITY_TOKEN"])
        or not isinstance(shim_path, str)
        or shim_path != env["NEXTFLOW_SHIM_EXPECTED_PATH"]
    ):
        return None
    return observation


def _nextflow_shim_identity_is_bound_to_env(
    identity: dict[str, object],
    env: dict[str, str],
) -> bool:
    pid = identity.get("pid")
    token = identity.get("token")
    shim_path = identity.get("shim_path")
    request_path = identity.get("request_path")
    response_path = identity.get("response_path")
    if (
        not isinstance(pid, int)
        or pid < 1
        or not isinstance(token, str)
        or not secrets.compare_digest(token, env["NEXTFLOW_SHIM_IDENTITY_TOKEN"])
        or shim_path != env["NEXTFLOW_SHIM_EXPECTED_PATH"]
        or request_path != env["NEXTFLOW_SHIM_REQUEST_FILE"]
        or response_path != env["NEXTFLOW_SHIM_RESPONSE_FILE"]
    ):
        return False
    return True


def _nextflow_shim_identity_agrees_with_observation(
    identity: dict[str, object], observation: dict[str, object]
) -> bool:
    return identity.get("pid") == observation.get("pid")


def _nextflow_shim_observed_pid_state(observation: dict[str, object]) -> str:
    pid = observation.get("pid")
    if not isinstance(pid, int) or pid < 1:
        return "unknown"
    try:
        os.getpgid(pid)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "unknown"
    return "unknown"


def _wait_for_nextflow_shim_observed_pid_exit(
    observation: dict[str, object], timeout: float = 10.0
) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _nextflow_shim_observed_pid_state(observation) == "absent":
            return True
        time.sleep(0.1)
    return _nextflow_shim_observed_pid_state(observation) == "absent"


def _nextflow_shim_identity_state(identity: dict[str, object]) -> str:
    pid = identity.get("pid")
    pgid = identity.get("pgid")
    process_start = identity.get("process_start")
    shim_path = identity.get("shim_path")
    request_path = identity.get("request_path")
    response_path = identity.get("response_path")
    token = identity.get("token")
    if (
        identity.get("schema") != 1
        or not isinstance(pid, int)
        or pid < 1
        or not isinstance(pgid, int)
        or pgid < 1
        or not isinstance(process_start, str)
        or not process_start
        or not isinstance(shim_path, str)
        or not shim_path
        or not isinstance(request_path, str)
        or not request_path
        or not isinstance(response_path, str)
        or not response_path
        or not isinstance(token, str)
        or not token
    ):
        return "unknown"
    try:
        current_pgid = os.getpgid(pid)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "unknown"
    if current_pgid != pgid:
        return "unknown"
    response = _nextflow_shim_control(identity, "probe")
    expected = {
        "schema": 1,
        "pid": pid,
        "pgid": pgid,
        "process_start": process_start,
        "shim_path": shim_path,
        "token": token,
    }
    return "same" if response == expected else "unknown"


def _nextflow_shim_identity_matches(identity: dict[str, object]) -> bool:
    return _nextflow_shim_identity_state(identity) == "same"


def _nextflow_shim_control(
    identity: dict[str, object], action: str
) -> dict[str, object] | None:
    request_path_value = identity.get("request_path")
    response_path_value = identity.get("response_path")
    token = identity.get("token")
    if (
        not isinstance(request_path_value, str)
        or not isinstance(response_path_value, str)
        or not isinstance(token, str)
    ):
        return None
    request_path = Path(request_path_value)
    response_path = Path(response_path_value)
    nonce = secrets.token_hex(16)
    request = {"action": action, "nonce": nonce, "token": token}
    request_tmp = request_path.with_name(f"{request_path.name}.{nonce}.tmp")
    try:
        response_path.unlink(missing_ok=True)
        request_tmp.write_text(
            json.dumps(request, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(request_tmp, request_path)
    except OSError:
        return None
    deadline = time.time() + 1
    while time.time() < deadline:
        try:
            response = json.loads(response_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            time.sleep(0.05)
            continue
        if isinstance(response, dict) and response.pop("nonce", None) == nonce:
            return response
        time.sleep(0.05)
    return None


def _wait_for_nextflow_shim_exit(
    identity: dict[str, object], timeout: float = 10.0
) -> None:
    _wait_for(
        lambda: _nextflow_shim_identity_state(identity) == "absent",
        timeout=timeout,
    )


def _terminate_nextflow_shim(identity: dict[str, object]) -> bool:
    for _attempt in range(2):
        state = _nextflow_shim_identity_state(identity)
        if state == "absent":
            return True
        if state == "same":
            _nextflow_shim_control(identity, "terminate")
        deadline = time.time() + 3
        while time.time() < deadline:
            if _nextflow_shim_identity_state(identity) == "absent":
                return True
            time.sleep(0.1)
    return _nextflow_shim_identity_state(identity) == "absent"


def _cleanup_wrapper_and_nextflow_shim(
    proc: subprocess.Popen[str],
    env: dict[str, str],
    identity: dict[str, object] | None,
) -> None:
    primary_failure = sys.exc_info()[0] is not None
    cleanup_errors: list[str] = []
    try:
        _terminate_process(proc)
    except Exception as exc:
        cleanup_errors.append(f"wrapper cleanup failed: {exc!r}")
    try:
        observation = _read_nextflow_shim_observation(env)
        candidate = identity
        if candidate is None:
            candidate = _read_nextflow_shim_identity(env)
        candidate_state: str | None = None
        if candidate is not None and _nextflow_shim_identity_is_bound_to_env(
            candidate, env
        ):
            candidate_state = _nextflow_shim_identity_state(candidate)
        elif candidate is not None:
            candidate_state = "unbound"
        if candidate_state == "same":
            if not _terminate_nextflow_shim(candidate):
                cleanup_errors.append(
                    "identity-scoped Nextflow shim cleanup did not prove PID absence"
                )
            if (
                observation is not None
                and not _nextflow_shim_identity_agrees_with_observation(
                    candidate, observation
                )
                and not _wait_for_nextflow_shim_observed_pid_exit(observation)
            ):
                cleanup_errors.append(
                    "mismatched pre-readiness Nextflow shim observation did not "
                    "prove PID absence"
                )
        elif candidate_state == "absent":
            if observation is None:
                cleanup_errors.append(
                    "absent Nextflow shim identity is not current PID absence "
                    "evidence and no valid observation is available"
                )
            elif not _wait_for_nextflow_shim_observed_pid_exit(observation):
                cleanup_errors.append(
                    "pre-readiness Nextflow shim observation did not prove PID absence"
                )
        elif candidate_state == "unknown":
            if (
                observation is not None
                and _nextflow_shim_identity_agrees_with_observation(
                    candidate, observation
                )
            ):
                if not _wait_for_nextflow_shim_observed_pid_exit(observation):
                    cleanup_errors.append(
                        "pre-readiness Nextflow shim observation did not prove PID "
                        "absence"
                    )
            else:
                cleanup_errors.append(
                    "unverified Nextflow shim identity does not match a valid "
                    "observation; PID absence is unproven"
                )
        elif candidate_state == "unbound":
            cleanup_errors.append(
                "parseable Nextflow shim identity is not bound to the current "
                "environment; PID absence is unproven"
            )
        elif observation is None:
            cleanup_errors.append(
                "authenticated Nextflow shim identity and valid pre-readiness "
                "observation are unavailable; PID absence is unproven"
            )
        elif not _wait_for_nextflow_shim_observed_pid_exit(observation):
            cleanup_errors.append(
                "pre-readiness Nextflow shim observation did not prove PID absence"
            )
    except Exception as exc:
        cleanup_errors.append(f"Nextflow shim cleanup failed: {exc!r}")
    if not cleanup_errors:
        return
    message = "; ".join(cleanup_errors)
    if primary_failure:
        try:
            sys.stderr.write(f"NEXTFLOW SHIM CLEANUP ERROR: {message}\n")
            sys.stderr.flush()
        except Exception:
            pass
        return
    raise AssertionError(message)


def _read_json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _extract_report_meta(path: Path) -> dict[str, object]:
    html = path.read_text(encoding="utf-8")
    marker = "window.REPORT_META = "
    start = html.index(marker) + len(marker)
    end = html.index(";\n", start)
    return json.loads(html[start:end])


def _rtbioscan_serve_command(outdir: Path, run_id: str, port: int) -> list[str]:
    return [
        "bash",
        str(RTBIOSCAN),
        "--serve",
        "--serve-quiet",
        "--serve-port",
        str(port),
        "--serve-dir",
        str(outdir),
        "--run_id",
        run_id,
        "--targets",
        "COI|ITS2",
        "--outdir",
        str(outdir),
        "-resume",
    ]


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


def _install_nextflow_shim(
    tmp_path: Path,
    *,
    run_exit: int = 0,
    run_mode: str = "exit",
    indirect_python: bool = False,
    identity_gate: bool = False,
) -> dict[str, str]:
    shim_dir = tmp_path / "nextflow-shim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim_path = shim_dir / "nextflow"
    shim_path.write_text(
        ("#!/usr/bin/env python3\n" if indirect_python else f"#!{sys.executable}\n")
        + "import json\n"
        "import os\n"
        "import signal\n"
        "import sys\n"
        "import time\n"
        "\n"
        "def _record_identity(path, request_path, response_path, token, mode, process_start):\n"
        "    pid = os.getpid()\n"
        "    identity = {\n"
        "        'schema': 1,\n"
        "        'pid': pid,\n"
        "        'ppid': os.getppid(),\n"
        "        'pgid': os.getpgid(pid),\n"
        "        'process_start': process_start,\n"
        "        'shim_path': os.path.realpath(__file__),\n"
        "        'request_path': request_path,\n"
        "        'response_path': response_path,\n"
        "        'token': token,\n"
        "        'mode': mode,\n"
        "        'argv': sys.argv,\n"
        "        'cwd': os.getcwd(),\n"
        "        'handlers_ready': True,\n"
        "    }\n"
        "    tmp = f'{path}.{pid}.tmp'\n"
        "    with open(tmp, 'w', encoding='utf-8') as handle:\n"
        "        json.dump(identity, handle, sort_keys=True)\n"
        "        handle.write('\\n')\n"
        "    os.replace(tmp, path)\n"
        "    return identity\n"
        "\n"
        "def _record_observation(path, token):\n"
        "    pid = os.getpid()\n"
        "    observation = {\n"
        "        'schema': 1,\n"
        "        'pid': pid,\n"
        "        'shim_path': os.path.realpath(__file__),\n"
        "        'token': token,\n"
        "    }\n"
        "    tmp = f'{path}.{pid}.tmp'\n"
        "    with open(tmp, 'w', encoding='utf-8') as handle:\n"
        "        json.dump(observation, handle, sort_keys=True)\n"
        "        handle.write('\\n')\n"
        "    os.replace(tmp, path)\n"
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
        "    identity_path = os.environ.get('NEXTFLOW_SHIM_IDENTITY_FILE', '')\n"
        "    observation_path = os.environ.get('NEXTFLOW_SHIM_OBSERVATION_FILE', '')\n"
        "    request_path = os.environ.get('NEXTFLOW_SHIM_REQUEST_FILE', '')\n"
        "    response_path = os.environ.get('NEXTFLOW_SHIM_RESPONSE_FILE', '')\n"
        "    token = os.environ.get('NEXTFLOW_SHIM_IDENTITY_TOKEN', '')\n"
        "    token_arg = f'--rtbioscan-shim-token={token}'\n"
        "    if identity_path and token and token_arg not in sys.argv:\n"
        "        os.execv(sys.executable, [sys.executable, os.path.realpath(__file__), *sys.argv[1:], token_arg])\n"
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
        "    if observation_path and token:\n"
        "        _record_observation(observation_path, token)\n"
        "    identity_gate_path = os.environ.get('NEXTFLOW_SHIM_IDENTITY_GATE_FILE', '')\n"
        "    while identity_gate_path and not os.path.exists(identity_gate_path):\n"
        "        time.sleep(0.05)\n"
        "    identity = None\n"
        "    if identity_path and request_path and response_path and token:\n"
        "        process_start = f'shim:{time.time_ns()}:{token}'\n"
        "        identity = _record_identity(\n"
        "            identity_path, request_path, response_path, token, mode, process_start\n"
        "        )\n"
        "    if mode in ('sleep', 'ignore_once'):\n"
        "        try:\n"
        "            while True:\n"
        "                if identity is None:\n"
        "                    time.sleep(0.1)\n"
        "                    continue\n"
        "                try:\n"
        "                    with open(request_path, encoding='utf-8') as handle:\n"
        "                        request = json.load(handle)\n"
        "                except (FileNotFoundError, json.JSONDecodeError, OSError):\n"
        "                    time.sleep(0.05)\n"
        "                    continue\n"
        "                try:\n"
        "                    os.unlink(request_path)\n"
        "                except FileNotFoundError:\n"
        "                    pass\n"
        "                if request.get('token') != token:\n"
        "                    continue\n"
        "                response = {key: identity[key] for key in (\n"
        "                    'schema', 'pid', 'pgid', 'process_start', 'shim_path', 'token'\n"
        "                )}\n"
        "                response['nonce'] = request.get('nonce')\n"
        "                tmp = f'{response_path}.{os.getpid()}.tmp'\n"
        "                with open(tmp, 'w', encoding='utf-8') as handle:\n"
        "                    json.dump(response, handle, sort_keys=True)\n"
        "                    handle.write('\\n')\n"
        "                os.replace(tmp, response_path)\n"
        "                if request.get('action') == 'terminate':\n"
        "                    raise SystemExit(0)\n"
        "        finally:\n"
        "            for path in (request_path, response_path):\n"
        "                if path:\n"
        "                    try:\n"
        "                        os.unlink(path)\n"
        "                    except FileNotFoundError:\n"
        "                        pass\n"
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
    env["NEXTFLOW_SHIM_IDENTITY_FILE"] = str(tmp_path / "nextflow-shim.identity.json")
    env["NEXTFLOW_SHIM_OBSERVATION_FILE"] = str(
        tmp_path / "nextflow-shim.observation.json"
    )
    env["NEXTFLOW_SHIM_IDENTITY_TOKEN"] = secrets.token_hex(32)
    env["NEXTFLOW_SHIM_EXPECTED_PATH"] = str(shim_path.resolve())
    env["NEXTFLOW_SHIM_IDENTITY_GATE_FILE"] = (
        str(tmp_path / "nextflow-shim.identity.gate") if identity_gate else ""
    )
    env["NEXTFLOW_SHIM_REQUEST_FILE"] = str(tmp_path / "nextflow-shim.request.json")
    env["NEXTFLOW_SHIM_RESPONSE_FILE"] = str(tmp_path / "nextflow-shim.response.json")
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


def test_rtbioscan_fresh_seed_and_graceful_prune_keep_artifact_schema_current(tmp_path: Path) -> None:
    run_id = f"FreshSchema_{tmp_path.name}"
    outdir = tmp_path / "results"
    env = _install_nextflow_shim(tmp_path, run_mode="sleep")
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "fresh-schema.log"
    report_root = outdir / "report_html"
    run_json = report_root / "runs" / run_id / "run_report.json"
    run_index = report_root / "runs_index.jsonl"
    report_html = report_root / "report.html"
    report_state = report_root / "report_state.json"
    shim_identity: dict[str, object] | None = None

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            _rtbioscan_serve_command(outdir, run_id, 8010),
            cwd=tmp_path,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for(
            lambda: marker_path.exists()
            and run_json.exists()
            and run_index.exists()
            and report_html.exists()
            and report_state.exists()
        )
        shim_identity = _wait_for_nextflow_shim_identity(env)

        seeded = json.loads(run_json.read_text(encoding="utf-8"))
        indexed = [row for row in _read_json_lines(run_index) if row.get("run_id") == run_id]
        state = json.loads(report_state.read_text(encoding="utf-8"))
        meta = _extract_report_meta(report_html)
        assert seeded["status_label"] == "Fresh"
        assert seeded["status"] == "running"
        assert seeded["rounds_count"] == 0
        assert seeded["schema_version"] == "2.0"
        assert len(indexed) == 1
        assert indexed[0]["schema_version"] == "2.0"
        assert meta["schema_version"] == state["schema_version"] == "2.0"

        proc.terminate()
        assert proc.wait(timeout=10) == 143
        _wait_for_nextflow_shim_exit(shim_identity)
        assert not run_json.parent.exists()
        assert not [row for row in _read_json_lines(run_index) if row.get("run_id") == run_id]
        state = json.loads(report_state.read_text(encoding="utf-8"))
        meta = _extract_report_meta(report_html)
        assert meta["schema_version"] == state["schema_version"] == "2.0"
    finally:
        _cleanup_wrapper_and_nextflow_shim(proc, env, shim_identity)


def test_rtbioscan_hard_crash_restart_retains_fresh_schema_seed(tmp_path: Path) -> None:
    run_id = f"CrashSchema_{tmp_path.name}"
    outdir = tmp_path / "results"
    env = _install_nextflow_shim(tmp_path, run_mode="sleep")
    py_env, marker_path = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "crash-schema.log"
    report_root = outdir / "report_html"
    run_json = report_root / "runs" / run_id / "run_report.json"
    run_index = report_root / "runs_index.jsonl"
    shim_identity: dict[str, object] | None = None

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            _rtbioscan_serve_command(outdir, run_id, 8011),
            cwd=tmp_path,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for(lambda: marker_path.exists() and run_json.exists() and run_index.exists())
        shim_identity = _wait_for_nextflow_shim_identity(env)
        os.killpg(proc.pid, signal.SIGKILL)
        assert proc.wait(timeout=10) == -signal.SIGKILL
        _wait_for_nextflow_shim_exit(shim_identity)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
        _cleanup_wrapper_and_nextflow_shim(proc, env, shim_identity)

    seeded = json.loads(run_json.read_text(encoding="utf-8"))
    assert seeded["status_label"] == "Fresh"
    assert seeded["status"] == "running"
    assert seeded["schema_version"] == "2.0"

    restart_env = _install_nextflow_shim(tmp_path, run_exit=0)
    restart_py_env, _ = _install_python_shim(tmp_path, mode="hold")
    restart_env = _merge_env(restart_env, restart_py_env)
    restarted = subprocess.run(
        _rtbioscan_serve_command(outdir, run_id, 8011),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=restart_env,
        timeout=15,
    )

    assert restarted.returncode == 0, restarted.stdout + restarted.stderr
    retained = json.loads(run_json.read_text(encoding="utf-8"))
    indexed = [row for row in _read_json_lines(run_index) if row.get("run_id") == run_id]
    assert retained["status_label"] == "Fresh"
    assert retained["status"] == "running"
    assert retained["schema_version"] == "2.0"
    assert len(indexed) == 1
    assert indexed[0]["schema_version"] == "2.0"


@pytest.mark.parametrize(
    ("indirect_python", "wrapper_should_reap", "port"),
    [(True, False, 8012), (False, True, 8013)],
    ids=["legacy-indirect", "direct"],
)
def test_rtbioscan_wrapper_reaps_the_generated_nextflow_shim_ab(
    tmp_path: Path,
    indirect_python: bool,
    wrapper_should_reap: bool,
    port: int,
) -> None:
    run_id = f"ShimReap_{indirect_python}_{tmp_path.name}"
    outdir = tmp_path / "results"
    env = _install_nextflow_shim(
        tmp_path, run_mode="sleep", indirect_python=indirect_python
    )
    py_env, server_marker = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "wrapper-shim-reap.log"
    shim_identity: dict[str, object] | None = None

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            _rtbioscan_serve_command(outdir, run_id, port),
            cwd=tmp_path,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for(lambda: server_marker.exists())
        shim_identity = _wait_for_nextflow_shim_identity(env)
        assert (shim_identity["ppid"] == proc.pid) is wrapper_should_reap

        proc.terminate()
        assert proc.wait(timeout=10) == 143
        if wrapper_should_reap:
            _wait_for_nextflow_shim_exit(shim_identity)
        else:
            assert _nextflow_shim_identity_matches(shim_identity)
    finally:
        _cleanup_wrapper_and_nextflow_shim(proc, env, shim_identity)


def test_nextflow_shim_cleanup_fails_closed_without_pid_absence_evidence(
    tmp_path: Path,
) -> None:
    env = _install_nextflow_shim(tmp_path, run_mode="sleep")
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        text=True,
    )
    assert proc.wait(timeout=5) == 0

    with pytest.raises(AssertionError, match="PID absence is unproven"):
        _cleanup_wrapper_and_nextflow_shim(proc, env, None)


def test_nextflow_shim_cleanup_rejects_unbound_identity_over_absent_observation(
    tmp_path: Path,
) -> None:
    env = _install_nextflow_shim(tmp_path, run_mode="sleep")
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        text=True,
    )
    exited_pid = proc.pid
    assert proc.wait(timeout=5) == 0
    with pytest.raises(ProcessLookupError):
        os.getpgid(exited_pid)

    observation = {
        "schema": 1,
        "pid": exited_pid,
        "shim_path": env["NEXTFLOW_SHIM_EXPECTED_PATH"],
        "token": env["NEXTFLOW_SHIM_IDENTITY_TOKEN"],
    }
    Path(env["NEXTFLOW_SHIM_OBSERVATION_FILE"]).write_text(
        json.dumps(observation, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    unbound_identity = {
        "schema": 1,
        "pid": exited_pid,
        "pgid": os.getpgid(os.getpid()),
        "process_start": "unbound-exited-process",
        "shim_path": env["NEXTFLOW_SHIM_EXPECTED_PATH"],
        "request_path": env["NEXTFLOW_SHIM_REQUEST_FILE"],
        "response_path": env["NEXTFLOW_SHIM_RESPONSE_FILE"],
        "token": "not-the-current-environment-token",
    }
    Path(env["NEXTFLOW_SHIM_IDENTITY_FILE"]).write_text(
        json.dumps(unbound_identity, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        AssertionError,
        match="not bound to the current environment; PID absence is unproven",
    ):
        _cleanup_wrapper_and_nextflow_shim(proc, env, None)


def test_nextflow_shim_cleanup_rejects_stale_identity_over_live_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = _install_nextflow_shim(tmp_path, run_mode="sleep")
    stale_proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        text=True,
    )
    stale_pid = stale_proc.pid
    assert stale_proc.wait(timeout=5) == 0
    with pytest.raises(ProcessLookupError):
        os.getpgid(stale_pid)

    observation = {
        "schema": 1,
        "pid": os.getpid(),
        "shim_path": env["NEXTFLOW_SHIM_EXPECTED_PATH"],
        "token": env["NEXTFLOW_SHIM_IDENTITY_TOKEN"],
    }
    Path(env["NEXTFLOW_SHIM_OBSERVATION_FILE"]).write_text(
        json.dumps(observation, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    stale_identity = {
        "schema": 1,
        "pid": stale_pid,
        "pgid": os.getpgid(os.getpid()),
        "process_start": "stale-exited-process",
        "shim_path": env["NEXTFLOW_SHIM_EXPECTED_PATH"],
        "request_path": env["NEXTFLOW_SHIM_REQUEST_FILE"],
        "response_path": env["NEXTFLOW_SHIM_RESPONSE_FILE"],
        "token": env["NEXTFLOW_SHIM_IDENTITY_TOKEN"],
    }
    Path(env["NEXTFLOW_SHIM_IDENTITY_FILE"]).write_text(
        json.dumps(stale_identity, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    waited_observations: list[dict[str, object]] = []

    def _reject_live_observation(
        current: dict[str, object], timeout: float = 10.0
    ) -> bool:
        waited_observations.append(current)
        return False

    monkeypatch.setattr(
        sys.modules[__name__],
        "_wait_for_nextflow_shim_observed_pid_exit",
        _reject_live_observation,
    )

    with pytest.raises(
        AssertionError,
        match="pre-readiness Nextflow shim observation did not prove PID absence",
    ):
        _cleanup_wrapper_and_nextflow_shim(stale_proc, env, None)

    assert waited_observations == [observation]


def test_nextflow_shim_cleanup_reaps_live_identity_despite_stale_observation(
    tmp_path: Path,
) -> None:
    run_id = f"ShimStaleObservation_{tmp_path.name}"
    outdir = tmp_path / "results"
    env = _install_nextflow_shim(
        tmp_path,
        run_mode="sleep",
        indirect_python=True,
    )
    py_env, server_marker = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "wrapper-shim-stale-observation.log"
    retained_identity: dict[str, object] | None = None
    stale_observation: dict[str, object] | None = None

    with wrapper_log.open("w", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            _rtbioscan_serve_command(outdir, run_id, 8016),
            cwd=tmp_path,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            start_new_session=True,
        )
    try:
        _wait_for(lambda: server_marker.exists())
        retained_identity = _wait_for_nextflow_shim_identity(env)
        assert retained_identity["ppid"] != proc.pid

        exited_proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            text=True,
        )
        exited_pid = exited_proc.pid
        assert exited_proc.wait(timeout=5) == 0
        with pytest.raises(ProcessLookupError):
            os.getpgid(exited_pid)
        stale_observation = {
            "schema": 1,
            "pid": exited_pid,
            "shim_path": env["NEXTFLOW_SHIM_EXPECTED_PATH"],
            "token": env["NEXTFLOW_SHIM_IDENTITY_TOKEN"],
        }
        Path(env["NEXTFLOW_SHIM_OBSERVATION_FILE"]).write_text(
            json.dumps(stale_observation, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        _cleanup_wrapper_and_nextflow_shim(proc, env, None)

        assert _nextflow_shim_identity_state(retained_identity) == "absent"
        assert _nextflow_shim_observed_pid_state(stale_observation) == "absent"
    finally:
        _cleanup_wrapper_and_nextflow_shim(proc, env, retained_identity)


def test_nextflow_shim_cleanup_preserves_primary_failure_and_proves_exit(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = f"ShimPrimaryFailure_{tmp_path.name}"
    outdir = tmp_path / "results"
    env = _install_nextflow_shim(
        tmp_path,
        run_mode="sleep",
        indirect_python=True,
    )
    py_env, server_marker = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    wrapper_log = tmp_path / "wrapper-shim-primary-failure.log"
    retained_identity: dict[str, object] | None = None

    def _fail_after_identity_is_authenticated() -> None:
        nonlocal retained_identity
        with wrapper_log.open("w", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                _rtbioscan_serve_command(outdir, run_id, 8014),
                cwd=tmp_path,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
        try:
            _wait_for(lambda: server_marker.exists())
            retained_identity = _wait_for_nextflow_shim_identity(env)
            assert retained_identity["ppid"] != proc.pid
            raise AssertionError("induced primary failure sentinel")
        finally:
            _cleanup_wrapper_and_nextflow_shim(proc, env, retained_identity)

    with pytest.raises(AssertionError, match="induced primary failure sentinel"):
        _fail_after_identity_is_authenticated()

    assert retained_identity is not None
    assert _nextflow_shim_identity_state(retained_identity) == "absent"
    assert "NEXTFLOW SHIM CLEANUP ERROR" not in capsys.readouterr().err


def test_nextflow_shim_readiness_timeout_uses_observation_only_to_prove_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_id = f"ShimReadinessTimeout_{tmp_path.name}"
    outdir = tmp_path / "results"
    env = _install_nextflow_shim(
        tmp_path,
        run_mode="sleep",
        identity_gate=True,
    )
    py_env, _ = _install_python_shim(tmp_path, mode="hold")
    env = _merge_env(env, py_env)
    identity_path = Path(env["NEXTFLOW_SHIM_IDENTITY_FILE"])
    identity_path.write_text("{not-json\n", encoding="utf-8")
    wrapper_log = tmp_path / "wrapper-shim-readiness-timeout.log"
    retained_observation: dict[str, object] | None = None
    wrapper_pid: int | None = None
    direct_signal_targets: list[int] = []
    group_signal_targets: list[int] = []
    original_terminate = subprocess.Popen.terminate
    original_kill = subprocess.Popen.kill
    original_os_kill = os.kill
    original_os_killpg = os.killpg

    def _record_terminate(proc: subprocess.Popen[str]) -> None:
        direct_signal_targets.append(proc.pid)
        original_terminate(proc)

    def _record_kill(proc: subprocess.Popen[str]) -> None:
        direct_signal_targets.append(proc.pid)
        original_kill(proc)

    def _record_os_kill(pid: int, sig: int) -> None:
        direct_signal_targets.append(pid)
        original_os_kill(pid, sig)

    def _record_os_killpg(pgid: int, sig: int) -> None:
        group_signal_targets.append(pgid)
        original_os_killpg(pgid, sig)

    monkeypatch.setattr(subprocess.Popen, "terminate", _record_terminate)
    monkeypatch.setattr(subprocess.Popen, "kill", _record_kill)
    monkeypatch.setattr(os, "kill", _record_os_kill)
    monkeypatch.setattr(os, "killpg", _record_os_killpg)

    def _time_out_before_identity_is_ready() -> None:
        nonlocal retained_observation, wrapper_pid
        with wrapper_log.open("w", encoding="utf-8") as handle:
            proc = subprocess.Popen(
                _rtbioscan_serve_command(outdir, run_id, 8015),
                cwd=tmp_path,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
        wrapper_pid = proc.pid
        try:
            _wait_for(
                lambda: _read_nextflow_shim_observation(env) is not None,
            )
            retained_observation = _read_nextflow_shim_observation(env)
            assert retained_observation is not None
            observed_pid = retained_observation["pid"]
            assert isinstance(observed_pid, int)
            assert os.getpgid(observed_pid) > 0
            _wait_for_nextflow_shim_identity(env, timeout=0.5)
        finally:
            _cleanup_wrapper_and_nextflow_shim(proc, env, None)

    with pytest.raises(AssertionError, match="condition not met before timeout"):
        _time_out_before_identity_is_ready()

    assert _read_nextflow_shim_identity(env) is None
    assert retained_observation is not None
    assert _nextflow_shim_observed_pid_state(retained_observation) == "absent"
    assert not Path(env["NEXTFLOW_SHIM_REQUEST_FILE"]).exists()
    assert wrapper_pid is not None
    assert retained_observation["pid"] != wrapper_pid
    assert wrapper_pid in direct_signal_targets
    assert retained_observation["pid"] not in direct_signal_targets
    assert not group_signal_targets
    assert "NEXTFLOW SHIM CLEANUP ERROR" not in capsys.readouterr().err


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
    shim_identity: dict[str, object] | None = None

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
        shim_identity = _wait_for_nextflow_shim_identity(env)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 130
        _wait_for_nextflow_shim_exit(shim_identity)
        _wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
        assert text.count("Stopping report server") == 1
        assert text.count("Feeder stopped.") == 1
        assert text.count("Report server stopped.") == 1
    finally:
        _cleanup_wrapper_and_nextflow_shim(proc, env, shim_identity)
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
    shim_identity: dict[str, object] | None = None

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
        shim_identity = _wait_for_nextflow_shim_identity(env)
        proc.terminate()
        assert proc.wait(timeout=10) == 143
        _wait_for_nextflow_shim_exit(shim_identity)
        _wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
        assert text.count("Stopping report server") == 1
        assert text.count("Feeder stopped.") == 1
        assert text.count("Report server stopped.") == 1
    finally:
        _cleanup_wrapper_and_nextflow_shim(proc, env, shim_identity)
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
    shim_identity: dict[str, object] | None = None

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
        shim_identity = _wait_for_nextflow_shim_identity(env)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.5)
        proc.send_signal(signal.SIGINT)
        assert proc.wait(timeout=10) == 130
        _wait_for_nextflow_shim_exit(shim_identity)
        _wait_for(lambda: not lock_dir.exists())
        text = wrapper_log.read_text(encoding="utf-8")
        assert text.count("Stopping feeder") == 1
        assert text.count("Stopping report server") == 1
    finally:
        _cleanup_wrapper_and_nextflow_shim(proc, env, shim_identity)
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
