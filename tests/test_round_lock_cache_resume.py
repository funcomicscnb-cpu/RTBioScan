import csv
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
NEXTFLOW = REPO_ROOT / "nextflow"
EXPECTED_NEXTFLOW_VERSION = "22.10.8"


WORKFLOW = r'''nextflow.enable.dsl=1

params.generation = 'gen1'

def topLevelGeneration = params.generation.toString()

Channel.value(params.generation.toString()).set { generation_ch }

process UNUSED_VAL_INPUT {
    input:
    val generation_for_hash from generation_ch

    output:
    file 'unused_val.txt' into unused_val_out

    script:
    """
    current=\$(cat '__PROBE_ROOT__/current-generation')
    printf 'unused_val:%s\\n' "\$current" >> '__PROBE_ROOT__/executed.log'
    printf 'constant\\n' > unused_val.txt
    """
}

process TOP_LEVEL_DEF_INTERPOLATION {
    output:
    file 'top_level.txt' into top_level_out

    script:
    """
    printf 'top_level:${topLevelGeneration}\\n' >> '__PROBE_ROOT__/executed.log'
    printf '${topLevelGeneration}\\n' > top_level.txt
    """
}

process DIRECT_PARAMS_INTERPOLATION {
    output:
    file 'direct_params.txt' into direct_params_out

    script:
    """
    printf 'direct_params:${params.generation}\\n' >> '__PROBE_ROOT__/executed.log'
    printf '${params.generation}\\n' > direct_params.txt
    """
}

process TASK_LOCAL_PARAMS_INTERPOLATION {
    output:
    file 'task_local_params.txt' into task_local_params_out

    script:
    def taskLocalGeneration = params.generation.toString()
    """
    printf 'task_local_params:${taskLocalGeneration}\\n' >> '__PROBE_ROOT__/executed.log'
    printf '${taskLocalGeneration}\\n' > task_local_params.txt
    """
}

process RUNTIME_ONLY_GUARD {
    output:
    file 'runtime_guard.txt' into runtime_guard_out

    script:
    """
    current=\$(cat '__PROBE_ROOT__/current-generation')
    expected=\$(cat '__PROBE_ROOT__/expected-generation')
    printf 'runtime_guard:%s\\n' "\$current" >> '__PROBE_ROOT__/executed.log'
    if [ "\$current" != "\$expected" ]; then
        echo "RUNTIME_GUARD_FIRED current=\$current expected=\$expected" >&2
        exit 97
    fi
    printf '%s\\n' "\$current" > runtime_guard.txt
    """
}
'''


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _require_repo_nextflow() -> None:
    # Positive control: prove the availability predicate recognizes an
    # executable before trusting a negative result for the repo launcher.
    assert _is_executable_file(Path(sys.executable))
    if not _is_executable_file(NEXTFLOW):
        pytest.skip(f"repository Nextflow launcher is unavailable: {NEXTFLOW}")


def _nextflow_env() -> dict[str, str]:
    env = os.environ.copy()
    env["NXF_VER"] = EXPECTED_NEXTFLOW_VERSION
    env["NXF_ANSI_LOG"] = "false"
    return env


def _run_nextflow(
    launch_dir: Path,
    *,
    log_name: str,
    trace_name: str,
    generation: str,
    run_name: str | None = None,
    resume_name: str | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        str(NEXTFLOW),
        "-log",
        log_name,
        "run",
        "main.nf",
        "-ansi-log",
        "false",
        "-with-trace",
        trace_name,
        "--generation",
        generation,
    ]
    if run_name is not None:
        command.extend(["-name", run_name])
    if resume_name is not None:
        command.extend(["-resume", resume_name])
    return subprocess.run(
        command,
        cwd=launch_dir,
        env=_nextflow_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _trace_by_process(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        assert reader.fieldnames is not None
        assert {"name", "status", "hash"}.issubset(reader.fieldnames)
        rows = list(reader)

    result = {row["name"]: row for row in rows}
    assert len(result) == len(rows), rows
    return result


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_dsl1_generation_values_and_runtime_guards_have_distinct_cache_semantics(
    tmp_path: Path,
) -> None:
    _require_repo_nextflow()

    probe_root = str(tmp_path).replace("'", "'\\''")
    (tmp_path / "main.nf").write_text(
        WORKFLOW.replace("__PROBE_ROOT__", probe_root), encoding="utf-8"
    )
    (tmp_path / "nextflow.config").write_text(
        "process.shell = ['/bin/bash', '-euo', 'pipefail']\n", encoding="utf-8"
    )
    (tmp_path / "current-generation").write_text("gen1\n", encoding="utf-8")
    (tmp_path / "expected-generation").write_text("gen1\n", encoding="utf-8")
    (tmp_path / "executed.log").write_text("", encoding="utf-8")

    first = _run_nextflow(
        tmp_path,
        log_name="run-1.nextflow.log",
        trace_name="trace-1.tsv",
        generation="gen1",
        run_name="cache-probe",
    )
    first_output = _combined_output(first)
    assert first.returncode == 0, first_output
    # Positive control: prove this actually ran the pinned launcher/version.
    assert f"version {EXPECTED_NEXTFLOW_VERSION}" in first_output

    expected_processes = {
        "UNUSED_VAL_INPUT",
        "TOP_LEVEL_DEF_INTERPOLATION",
        "DIRECT_PARAMS_INTERPOLATION",
        "TASK_LOCAL_PARAMS_INTERPOLATION",
        "RUNTIME_ONLY_GUARD",
    }
    first_trace = _trace_by_process(tmp_path / "trace-1.tsv")
    assert set(first_trace) == expected_processes
    assert {row["status"] for row in first_trace.values()} == {"COMPLETED"}

    first_executions = Counter(
        (tmp_path / "executed.log").read_text(encoding="utf-8").splitlines()
    )
    assert first_executions == Counter(
        {
            "unused_val:gen1": 1,
            "top_level:gen1": 1,
            "direct_params:gen1": 1,
            "task_local_params:gen1": 1,
            "runtime_guard:gen1": 1,
        }
    )

    # Keep the guard's expected value at gen1. If its shell executes for gen2,
    # it fails; a cache hit skips that runtime-only check entirely.
    (tmp_path / "current-generation").write_text("gen2\n", encoding="utf-8")
    resumed = _run_nextflow(
        tmp_path,
        log_name="run-2.nextflow.log",
        trace_name="trace-2.tsv",
        generation="gen2",
        resume_name="cache-probe",
    )
    resumed_output = _combined_output(resumed)
    assert resumed.returncode == 0, resumed_output
    assert "RUNTIME_GUARD_FIRED" not in resumed_output

    resumed_trace = _trace_by_process(tmp_path / "trace-2.tsv")
    assert set(resumed_trace) == expected_processes
    assert {name: row["status"] for name, row in resumed_trace.items()} == {
        "UNUSED_VAL_INPUT": "COMPLETED",
        "TOP_LEVEL_DEF_INTERPOLATION": "CACHED",
        "DIRECT_PARAMS_INTERPOLATION": "COMPLETED",
        "TASK_LOCAL_PARAMS_INTERPOLATION": "COMPLETED",
        "RUNTIME_ONLY_GUARD": "CACHED",
    }

    for name in ("TOP_LEVEL_DEF_INTERPOLATION", "RUNTIME_ONLY_GUARD"):
        assert resumed_trace[name]["hash"] == first_trace[name]["hash"]
    for name in (
        "UNUSED_VAL_INPUT",
        "DIRECT_PARAMS_INTERPOLATION",
        "TASK_LOCAL_PARAMS_INTERPOLATION",
    ):
        assert resumed_trace[name]["hash"] != first_trace[name]["hash"]

    resumed_executions = Counter(
        (tmp_path / "executed.log").read_text(encoding="utf-8").splitlines()
    )
    assert resumed_executions == first_executions + Counter(
        {
            "unused_val:gen2": 1,
            "direct_params:gen2": 1,
            "task_local_params:gen2": 1,
        }
    )

    # Positive control for the cached runtime guard: in a fresh session, the
    # same mismatch must reach the guarded shell and fail with its unique text.
    guard_proof = _run_nextflow(
        tmp_path,
        log_name="guard-proof.nextflow.log",
        trace_name="trace-guard-proof.tsv",
        generation="gen2",
        run_name="guard-proof",
    )
    guard_output = _combined_output(guard_proof)
    assert guard_proof.returncode != 0
    assert "RUNTIME_GUARD_FIRED current=gen2 expected=gen1" in guard_output
