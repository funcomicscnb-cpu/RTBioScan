import csv
import hashlib
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
NEXTFLOW = REPO_ROOT / "nextflow"
ROUND_LOCK_HELPER = REPO_ROOT / "bin" / "round_lock_generation.pl"
ROUND_LOCK_GUARD = REPO_ROOT / "bin" / "round_lock_process_guard.sh"
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


HANDOFF_RESUME_WORKFLOW = r'''nextflow.enable.dsl=1

Channel.value('round_1').set { reads_ch }
__FAST_CACHE_CHANNEL__

process FAST {
    input:
    val round_barcode from reads_ch
    val round_lock_fast_cache_token from fast_cache_key_ch

    output:
    tuple env(round_generation_token), env(round_lock_scope) into generation_ch

    script:
    """
    set -euo pipefail
    export LC_ALL=C
    ROUND_LOCK_STATE_DIR='${params.state}'
    ROUND_LOCK_HELPER='${params.helper}'
    ROUND_LOCK_SCOPE=full_round
    source '${params.guard}'
    generation_file=.probe-generation.\$\$
    pin_file=.probe-pin.\$\$
    generation=\$(rtbioscan_round_lock_prepare_token_file \
        "\$generation_file" "probe-generation:\$\$")
    pin=\$(rtbioscan_round_lock_prepare_token_file \
        "\$pin_file" "probe-pin:\$\$")
    printf 'FAST_ATTEMPT:%s\\n' "\$generation" >> '${params.audit_log}'
    perl "\$ROUND_LOCK_HELPER" acquire \
        --state-dir "\$ROUND_LOCK_STATE_DIR" \
        --round-barcode "${round_barcode}" \
        --scope "\$ROUND_LOCK_SCOPE" \
        --token "\$generation" \
        --pin-token "\$pin" \
        --owner-pid "\$\$" \
        --wait-seconds 1 \
        --stale-seconds 3600 >/dev/null
    perl "\$ROUND_LOCK_HELPER" inflight \
        --state-dir "\$ROUND_LOCK_STATE_DIR" \
        --round-barcode "${round_barcode}" \
        --scope "\$ROUND_LOCK_SCOPE" \
        --token "\$generation" \
        --pin-token "\$pin" \
        --read-file /probe/round_1.pod5
    perl "\$ROUND_LOCK_HELPER" handoff \
        --state-dir "\$ROUND_LOCK_STATE_DIR" \
        --round-barcode "${round_barcode}" \
        --scope "\$ROUND_LOCK_SCOPE" \
        --token "\$generation" \
        --pin-token "\$pin"
    round_generation_token="\$generation"
    round_lock_scope="\$ROUND_LOCK_SCOPE"
    printf 'FAST_HANDOFF:%s\\n' "\$generation" >> '${params.audit_log}'
    """
}

process CACHED_WRITER {
    input:
    tuple val(round_generation_token), val(round_lock_scope) from generation_ch

    output:
    tuple val(round_generation_token), val(round_lock_scope), \
        file('cached-writer.done') into retry_generation_ch

    script:
    """
    set -euo pipefail
    export RTBIOSCAN_ROUND_LOCK_STATE_DIR='${params.state}'
    export RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE=round_1
    export RTBIOSCAN_ROUND_LOCK_SCOPE='${round_lock_scope}'
    export RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN='${round_generation_token}'
    export RTBIOSCAN_ROUND_LOCK_HELPER='${params.helper}'
    export RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE=.cached-writer-pin.\$\$
    source '${params.guard}'
    rtbioscan_round_lock_pin cached_writer
    printf 'CACHED_WRITER_OK:%s\\n' '${round_generation_token}' >> '${params.audit_log}'
    printf 'cached\\n' > cached-writer.done
    rtbioscan_round_lock_unpin
    """
}

process WRITER {
    input:
    tuple val(round_generation_token), val(round_lock_scope), \
        file(cached_writer_done) from retry_generation_ch

    output:
    file 'writer.done'

    script:
    """
    set -euo pipefail
    if [ ! -f '${params.allow}' ]; then
        printf 'WRITER_FAIL:%s\\n' '${round_generation_token}' >> '${params.audit_log}'
        exit 97
    fi
    export RTBIOSCAN_ROUND_LOCK_STATE_DIR='${params.state}'
    export RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE=round_1
    export RTBIOSCAN_ROUND_LOCK_SCOPE='${round_lock_scope}'
    export RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN='${round_generation_token}'
    export RTBIOSCAN_ROUND_LOCK_HELPER='${params.helper}'
    export RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE=.writer-pin.\$\$
    source '${params.guard}'
    rtbioscan_round_lock_pin writer
    printf 'WRITER_OK:%s\\n' '${round_generation_token}' >> '${params.audit_log}'
    printf 'ok\\n' > writer.done
    rtbioscan_round_lock_unpin
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


def _run_handoff_resume_probe(
    launch_dir: Path,
    *,
    log_name: str,
    trace_name: str,
    run_name: str,
    resume: bool,
    restart_epoch: str = "epoch-a",
    helper: Path = ROUND_LOCK_HELPER,
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
    ]
    command.extend(["-resume" if resume else "-name", run_name])
    command.extend(
        [
            "--state",
            str(launch_dir / "state"),
            "--helper",
            str(helper),
            "--guard",
            str(ROUND_LOCK_GUARD),
            "--audit_log",
            str(launch_dir / "executed.log"),
            "--allow",
            str(launch_dir / "allow-writer"),
            "--restart_epoch",
            restart_epoch,
        ]
    )
    return subprocess.run(
        command,
        cwd=launch_dir,
        env=_nextflow_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _round_lock_runtime_contract(helper: Path, guard: Path) -> str:
    digest = hashlib.sha256()
    digest.update(b"RTBioScan round-lock runtime cache contract v1\n")
    for label, path in (
        ("generation-helper", helper),
        ("process-guard", guard),
    ):
        payload = path.read_bytes()
        digest.update(f"{label}\t{len(payload)}\n".encode())
        digest.update(payload)
        digest.update(b"\n")
    return digest.hexdigest()


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


def test_full_round_handoff_resume_reuses_cached_fast_generation(
    tmp_path: Path,
) -> None:
    _require_repo_nextflow()
    assert ROUND_LOCK_HELPER.is_file()
    assert ROUND_LOCK_GUARD.is_file()

    probe_dirs = {
        "cacheable": tmp_path / "cacheable-fast",
        "runname_key": tmp_path / "runname-key-fast",
        "uncached": tmp_path / "uncached-fast",
    }
    first_tokens: dict[str, str] = {}
    first_generation_records: dict[str, bytes] = {}
    first_generation_identities: dict[str, tuple[int, int, int]] = {}
    helpers: dict[str, Path] = {}
    cache_keys: dict[str, str] = {}

    for variant, launch_dir in probe_dirs.items():
        launch_dir.mkdir()
        (launch_dir / "state").mkdir()
        (launch_dir / "executed.log").write_text("", encoding="utf-8")
        helper = launch_dir / "round_lock_generation.pl"
        shutil.copy2(ROUND_LOCK_HELPER, helper)
        helpers[variant] = helper
        runtime_contract = _round_lock_runtime_contract(helper, ROUND_LOCK_GUARD)
        cache_keys[variant] = f"epoch-a|round-lock:{runtime_contract}"
        cache_channel = (
            "Channel.value(workflow.runName).set { fast_cache_key_ch }"
            if variant == "runname_key"
            else "Channel.value(params.restart_epoch.toString()).set { fast_cache_key_ch }"
        )
        workflow = HANDOFF_RESUME_WORKFLOW.replace(
            "__FAST_CACHE_CHANNEL__", cache_channel
        )
        assert "__FAST_CACHE_CHANNEL__" not in workflow
        if variant == "uncached":
            workflow = workflow.replace(
                "process FAST {", "process FAST {\n    cache false", 1
            )
            assert workflow.count("cache false") == 1
        else:
            assert "cache false" not in workflow
        (launch_dir / "main.nf").write_text(workflow, encoding="utf-8")
        (launch_dir / "nextflow.config").write_text(
            "process.shell = ['/bin/bash', '-euo', 'pipefail']\n",
            encoding="utf-8",
        )

        run_name = f"handoff-resume-{variant}"
        first = _run_handoff_resume_probe(
            launch_dir,
            log_name="run-1.nextflow.log",
            trace_name="trace-1.tsv",
            run_name=run_name,
            resume=False,
            restart_epoch=cache_keys[variant],
            helper=helper,
        )
        first_output = _combined_output(first)
        assert first.returncode != 0
        assert f"version {EXPECTED_NEXTFLOW_VERSION}" in first_output
        assert f"[{run_name}]" in first_output
        assert "terminated with an error exit status (97)" in first_output

        first_trace = _trace_by_process(launch_dir / "trace-1.tsv")
        assert {name: row["status"] for name, row in first_trace.items()} == {
            "FAST": "COMPLETED",
            "CACHED_WRITER": "COMPLETED",
            "WRITER": "FAILED",
        }
        first_audit = (
            (launch_dir / "executed.log").read_text(encoding="utf-8").splitlines()
        )
        assert len(first_audit) == 4
        assert first_audit[0].startswith("FAST_ATTEMPT:")
        assert first_audit[1].startswith("FAST_HANDOFF:")
        assert first_audit[2].startswith("CACHED_WRITER_OK:")
        assert first_audit[3].startswith("WRITER_FAIL:")
        first_token = first_audit[0].split(":", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{64}", first_token)
        assert first_audit == [
            f"FAST_ATTEMPT:{first_token}",
            f"FAST_HANDOFF:{first_token}",
            f"CACHED_WRITER_OK:{first_token}",
            f"WRITER_FAIL:{first_token}",
        ]
        generation_path = (
            launch_dir / "state" / ".round_inflight.lockdir" / "generation.tsv"
        )
        generation_bytes = generation_path.read_bytes()
        generation_record = generation_bytes.decode("utf-8")
        assert f"token\t{first_token}\n" in generation_record
        first_tokens[variant] = first_token
        first_generation_records[variant] = generation_bytes
        generation_stat = os.lstat(generation_path)
        first_generation_identities[variant] = (
            generation_stat.st_dev,
            generation_stat.st_ino,
            generation_stat.st_mode,
        )
        (launch_dir / "allow-writer").write_text("allow\n", encoding="utf-8")

    cacheable_dir = probe_dirs["cacheable"]
    cacheable_resumed = _run_handoff_resume_probe(
        cacheable_dir,
        log_name="run-2.nextflow.log",
        trace_name="trace-2.tsv",
        run_name="handoff-resume-cacheable",
        resume=True,
        restart_epoch=cache_keys["cacheable"],
        helper=helpers["cacheable"],
    )
    cacheable_output = _combined_output(cacheable_resumed)
    assert cacheable_resumed.returncode == 0, cacheable_output
    assert "[handoff-resume-cacheable]" not in cacheable_output
    cacheable_first_trace = _trace_by_process(cacheable_dir / "trace-1.tsv")
    cacheable_trace = _trace_by_process(cacheable_dir / "trace-2.tsv")
    assert {name: row["status"] for name, row in cacheable_trace.items()} == {
        "FAST": "CACHED",
        "CACHED_WRITER": "CACHED",
        "WRITER": "COMPLETED",
    }
    assert cacheable_trace["FAST"]["hash"] == cacheable_first_trace["FAST"]["hash"]
    assert (
        cacheable_trace["CACHED_WRITER"]["hash"]
        == cacheable_first_trace["CACHED_WRITER"]["hash"]
    )
    assert (cacheable_dir / "executed.log").read_text(
        encoding="utf-8"
    ).splitlines() == [
        f"FAST_ATTEMPT:{first_tokens['cacheable']}",
        f"FAST_HANDOFF:{first_tokens['cacheable']}",
        f"CACHED_WRITER_OK:{first_tokens['cacheable']}",
        f"WRITER_FAIL:{first_tokens['cacheable']}",
        f"WRITER_OK:{first_tokens['cacheable']}",
    ]

    # Positive control: Nextflow does not hash a helper referenced only by an
    # absolute command path.  Changing its bytes while retaining the old
    # declared contract must therefore leave all completed tasks cached.
    cacheable_helper = helpers["cacheable"]
    helper_before = cacheable_helper.read_bytes()
    cacheable_helper.write_bytes(
        helper_before + b"\n# cache-contract mutation positive control\n"
    )
    assert cacheable_helper.read_bytes() != helper_before
    assert (
        _round_lock_runtime_contract(cacheable_helper, ROUND_LOCK_GUARD)
        != cache_keys["cacheable"].split("round-lock:", 1)[1]
    )

    unchanged_contract = _run_handoff_resume_probe(
        cacheable_dir,
        log_name="run-3.nextflow.log",
        trace_name="trace-3.tsv",
        run_name="handoff-resume-cacheable",
        resume=True,
        restart_epoch=cache_keys["cacheable"],
        helper=cacheable_helper,
    )
    unchanged_contract_output = _combined_output(unchanged_contract)
    assert unchanged_contract.returncode == 0, unchanged_contract_output
    unchanged_contract_trace = _trace_by_process(cacheable_dir / "trace-3.tsv")
    assert {name: row["status"] for name, row in unchanged_contract_trace.items()} == {
        "FAST": "CACHED",
        "CACHED_WRITER": "CACHED",
        "WRITER": "CACHED",
    }
    assert (
        unchanged_contract_trace["FAST"]["hash"]
        == cacheable_first_trace["FAST"]["hash"]
    )
    assert (cacheable_dir / "executed.log").read_text(
        encoding="utf-8"
    ).splitlines() == [
        f"FAST_ATTEMPT:{first_tokens['cacheable']}",
        f"FAST_HANDOFF:{first_tokens['cacheable']}",
        f"CACHED_WRITER_OK:{first_tokens['cacheable']}",
        f"WRITER_FAIL:{first_tokens['cacheable']}",
        f"WRITER_OK:{first_tokens['cacheable']}",
    ]

    # Binding the independently recomputed helper/guard contract into the
    # declared value input makes the same byte change invalidate FAST.
    changed_runtime_contract = _run_handoff_resume_probe(
        cacheable_dir,
        log_name="run-4.nextflow.log",
        trace_name="trace-4.tsv",
        run_name="handoff-resume-cacheable",
        resume=True,
        restart_epoch=(
            "epoch-a|round-lock:"
            + _round_lock_runtime_contract(cacheable_helper, ROUND_LOCK_GUARD)
        ),
        helper=cacheable_helper,
    )
    changed_runtime_output = _combined_output(changed_runtime_contract)
    assert changed_runtime_contract.returncode != 0
    changed_runtime_timeout = (
        "ERROR: timed out waiting for round lock "
        f"'{cacheable_dir / 'state' / '.round_inflight.lockdir'}'"
    )
    assert changed_runtime_timeout in changed_runtime_output
    changed_runtime_trace = _trace_by_process(cacheable_dir / "trace-4.tsv")
    assert {name: row["status"] for name, row in changed_runtime_trace.items()} == {
        "FAST": "FAILED"
    }
    assert (
        changed_runtime_trace["FAST"]["hash"] != cacheable_first_trace["FAST"]["hash"]
    )
    changed_runtime_audit = (
        (cacheable_dir / "executed.log").read_text(encoding="utf-8").splitlines()
    )
    assert len(changed_runtime_audit) == 6
    changed_runtime_token = changed_runtime_audit[-1].split(":", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", changed_runtime_token)
    assert changed_runtime_token != first_tokens["cacheable"]
    assert changed_runtime_audit[-1] == f"FAST_ATTEMPT:{changed_runtime_token}"
    retained_after_runtime_change = (
        cacheable_dir / "state" / ".round_inflight.lockdir" / "generation.tsv"
    )
    assert (
        retained_after_runtime_change.read_bytes()
        == first_generation_records["cacheable"]
    )
    retained_runtime_stat = os.lstat(retained_after_runtime_change)
    assert (
        retained_runtime_stat.st_dev,
        retained_runtime_stat.st_ino,
        retained_runtime_stat.st_mode,
    ) == first_generation_identities["cacheable"]

    # Both the tempting workflow.runName key and the exact previous `cache
    # false` behavior rerun FAST on resume.  Each A/B mutation must reach the
    # real helper, mint a distinct request, time out behind the handed-off
    # generation, and preserve that canonical generation unchanged.
    for variant in ("runname_key", "uncached"):
        variant_dir = probe_dirs[variant]
        variant_resumed = _run_handoff_resume_probe(
            variant_dir,
            log_name="run-2.nextflow.log",
            trace_name="trace-2.tsv",
            run_name=f"handoff-resume-{variant}",
            resume=True,
            restart_epoch=cache_keys[variant],
            helper=helpers[variant],
        )
        variant_output = _combined_output(variant_resumed)
        assert variant_resumed.returncode != 0
        assert f"[handoff-resume-{variant}]" not in variant_output
        expected_timeout = (
            "ERROR: timed out waiting for round lock "
            f"'{variant_dir / 'state' / '.round_inflight.lockdir'}'"
        )
        assert expected_timeout in variant_output
        variant_trace = _trace_by_process(variant_dir / "trace-2.tsv")
        assert {name: row["status"] for name, row in variant_trace.items()} == {
            "FAST": "FAILED"
        }
        hash_prefix, hash_suffix = variant_trace["FAST"]["hash"].split("/", 1)
        failed_work_dirs = list(
            (variant_dir / "work" / hash_prefix).glob(f"{hash_suffix}*")
        )
        assert len(failed_work_dirs) == 1
        assert (failed_work_dirs[0] / ".command.err").read_text(
            encoding="utf-8"
        ) == f"{expected_timeout}\n"
        variant_audit = (
            (variant_dir / "executed.log").read_text(encoding="utf-8").splitlines()
        )
        assert len(variant_audit) == 5
        replacement_token = variant_audit[4].split(":", 1)[1]
        assert re.fullmatch(r"[0-9a-f]{64}", replacement_token)
        assert replacement_token != first_tokens[variant]
        assert variant_audit == [
            f"FAST_ATTEMPT:{first_tokens[variant]}",
            f"FAST_HANDOFF:{first_tokens[variant]}",
            f"CACHED_WRITER_OK:{first_tokens[variant]}",
            f"WRITER_FAIL:{first_tokens[variant]}",
            f"FAST_ATTEMPT:{replacement_token}",
        ]
        retained_generation = (
            variant_dir / "state" / ".round_inflight.lockdir" / "generation.tsv"
        )
        assert retained_generation.read_bytes() == first_generation_records[variant]
        retained_stat = os.lstat(retained_generation)
        assert (
            retained_stat.st_dev,
            retained_stat.st_ino,
            retained_stat.st_mode,
        ) == first_generation_identities[variant]

    # A persisted restart epoch is stable across the new workflow.runName that
    # Nextflow assigns on resume, but changing that epoch must invalidate FAST.
    changed_epoch = _run_handoff_resume_probe(
        cacheable_dir,
        log_name="run-5.nextflow.log",
        trace_name="trace-5.tsv",
        run_name="handoff-resume-cacheable",
        resume=True,
        restart_epoch=(
            "epoch-b|round-lock:"
            + _round_lock_runtime_contract(helpers["cacheable"], ROUND_LOCK_GUARD)
        ),
        helper=helpers["cacheable"],
    )
    changed_epoch_output = _combined_output(changed_epoch)
    assert changed_epoch.returncode != 0
    changed_expected_timeout = (
        "ERROR: timed out waiting for round lock "
        f"'{cacheable_dir / 'state' / '.round_inflight.lockdir'}'"
    )
    assert changed_expected_timeout in changed_epoch_output
    changed_trace = _trace_by_process(cacheable_dir / "trace-5.tsv")
    assert {name: row["status"] for name, row in changed_trace.items()} == {
        "FAST": "FAILED"
    }
    assert changed_trace["FAST"]["hash"] != cacheable_first_trace["FAST"]["hash"]
    changed_audit = (
        (cacheable_dir / "executed.log").read_text(encoding="utf-8").splitlines()
    )
    assert len(changed_audit) == 7
    changed_token = changed_audit[-1].split(":", 1)[1]
    assert re.fullmatch(r"[0-9a-f]{64}", changed_token)
    assert changed_token != first_tokens["cacheable"]
    assert changed_audit[-1] == f"FAST_ATTEMPT:{changed_token}"
    cacheable_generation = (
        cacheable_dir / "state" / ".round_inflight.lockdir" / "generation.tsv"
    )
    assert cacheable_generation.read_bytes() == first_generation_records["cacheable"]
    cacheable_generation_stat = os.lstat(cacheable_generation)
    assert (
        cacheable_generation_stat.st_dev,
        cacheable_generation_stat.st_ino,
        cacheable_generation_stat.st_mode,
    ) == first_generation_identities["cacheable"]
