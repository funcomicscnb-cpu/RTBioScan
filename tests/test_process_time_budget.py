"""S6 FAST scheduler-time contract under the pinned Nextflow 22.10.8 runtime."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "nextflow.config"
BASE_CONFIG = ROOT / "conf" / "base.config"
MAIN_NF = ROOT / "main.nf"

FAST_SELECTOR = """  withName: fast_on_target_detection {
    time = { check_max(
      ((params.round_lock_wait_minutes as int) + (params.file_wait_minutes as int)).minutes + 4.h * task.attempt,
      'time'
    ) }
  }
"""


def expected_minutes(round_wait: int, file_wait: int, attempt: int, max_time: int) -> int:
    """Independent duration oracle; deliberately does not parse the Groovy expression."""
    wait_minutes = round_wait + file_wait
    work_minutes = 240 * attempt
    requested = wait_minutes + work_minutes
    return min(requested, max_time)


def _runtime() -> list[str]:
    cached_home = Path.home() / ".nextflow"
    capsule = cached_home / "capsule" / "apps" / "nextflow-all_22.10.8"
    jar = cached_home / "framework" / "22.10.8" / "nextflow-22.10.8-one.jar"
    java = shutil.which("java")
    if not java or not jar.is_file() or not (capsule / "nextflow-22.10.8.jar").is_file():
        pytest.skip(
            f"cached Nextflow 22.10.8 unavailable: java={java}, jar={jar}, capsule={capsule}"
        )
    packages = (
        "java.lang", "java.io", "java.nio", "java.net", "java.util",
        "java.util.concurrent.locks", "java.util.concurrent.atomic", "java.nio.file.spi",
        "sun.nio.ch", "sun.nio.fs", "sun.net.www.protocol.http",
        "sun.net.www.protocol.https", "sun.net.www.protocol.ftp",
        "sun.net.www.protocol.file", "jdk.internal.misc", "java.util.regex",
    )
    opens = [f"--add-opens=java.base/{package}=ALL-UNNAMED" for package in packages]
    return [java, *opens, "-cp", str(capsule / "*"), "nextflow.cli.Launcher"]


PROBE_SOURCE = r'''nextflow.enable.dsl=1
params.probe_out = params.probe_out ?: "${launchDir}/probe"
params.probe_retry = params.probe_retry ?: false

fast_input = Channel.value(1)
ordinary_input = Channel.value(1)
hac_input = Channel.value(1)
consensus_input = Channel.value(1)
backup_input = Channel.value(1)
async_input = Channel.value(1)
blast_input = Channel.value(1)

process fast_on_target_detection {
    input:
    val token from fast_input
    script:
    """
    if [ '${params.probe_retry}' = 'true' ] && [ '${task.attempt}' -eq 1 ]; then
        exit 143
    fi
    printf '%s\n' '${task.time.toMillis()}' > '${params.probe_out}/fast_on_target_detection.txt'
    """
}

process ordinary_probe {
    input:
    val token from ordinary_input
    script:
    """printf '%s\n' '${task.time.toMillis()}' > '${params.probe_out}/ordinary_probe.txt'"""
}

process hac_basecalling {
    label 'dorado'
    input:
    val token from hac_input
    script:
    """printf '%s\n' '${task.time.toMillis()}' > '${params.probe_out}/hac_basecalling.txt'"""
}

process consensus {
    label 'blast'
    input:
    val token from consensus_input
    script:
    """printf '%s\n' '${task.time.toMillis()}' > '${params.probe_out}/consensus.txt'"""
}

process backup_update_and_clean {
    input:
    val token from backup_input
    script:
    """printf '%s\n' '${task.time.toMillis()}' > '${params.probe_out}/backup_update_and_clean.txt'"""
}

process async_report_render {
    input:
    val token from async_input
    script:
    """printf '%s\n' '${task.time.toMillis()}' > '${params.probe_out}/async_report_render.txt'"""
}

process blast_OTU_pretax {
    label 'blast'
    time '20h'
    input:
    val token from blast_input
    script:
    """printf '%s\n' '${task.time.toMillis()}' > '${params.probe_out}/blast_OTU_pretax.txt'"""
}
'''

TIMEOUT_SOURCE = r'''nextflow.enable.dsl=1
timeout_input = Channel.value(1)
process timeout_probe {
    time '1 sec'
    input:
    val token from timeout_input
    script:
    """
    sleep 3
    printf '%s\n' '${task.attempt}' > timeout-attempt.txt
    """
}
'''

RETRY_SOURCE = r'''nextflow.enable.dsl=1
params.probe_out = params.probe_out ?: "${launchDir}/probe"
retry_input = Channel.value(1)
process retry_exit_probe {
    input:
    val token from retry_input
    script:
    """
    if [ '${task.attempt}' -eq 1 ]; then exit 143; fi
    printf '%s\n' '${task.attempt}' > '${params.probe_out}/retry-attempt.txt'
    """
}
'''


def _prepare_project(
    root: Path,
    *,
    config_text: str | None = None,
    base_text: str | None = None,
    source: str = PROBE_SOURCE,
) -> Path:
    project = root / "project"
    project.mkdir(parents=True)
    shutil.copytree(ROOT / "conf", project / "conf")
    shutil.copytree(ROOT / "bin" / "lib", project / "bin" / "lib")
    resolved_config = (
        config_text if config_text is not None else CONFIG.read_text(encoding="utf-8")
    )
    (project / "nextflow.config").write_text(
        resolved_config + "\ntrace.fields = 'task_id,name,status,exit,attempt'\n",
        encoding="utf-8",
    )
    if base_text is not None:
        (project / "conf" / "base.config").write_text(base_text, encoding="utf-8")
    (project / "main.nf").write_text(source, encoding="utf-8")
    return project


def _run(
    root: Path,
    *,
    args: list[str] | None = None,
    config_text: str | None = None,
    base_text: str | None = None,
    source: str = PROBE_SOURCE,
    expect_success: bool = True,
) -> tuple[subprocess.CompletedProcess[str], dict[str, int], Path]:
    project = _prepare_project(
        root, config_text=config_text, base_text=base_text, source=source
    )
    launch = root / "launch"
    probe = root / "probe"
    work = root / "work"
    home = root / "nxf-home"
    temp = root / "nxf-temp"
    for path in (launch, probe, work, home, temp):
        path.mkdir(parents=True, exist_ok=True)
    trace = root / "trace.tsv"
    command = [
        *_runtime(), "run", str(project / "main.nf"),
        "-work-dir", str(work), "-with-trace", str(trace),
        "--probe_out", str(probe), *(args or []),
    ]
    env = {
        **os.environ,
        "NXF_OFFLINE": "true",
        "NXF_VER": "22.10.8",
        "NXF_ANSI_LOG": "false",
        "NXF_DISABLE_CHECK_LATEST": "true",
        "NXF_HOME": str(home),
        "NXF_TEMP": str(temp),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(
        command, cwd=launch, env=env, text=True, capture_output=True, timeout=90
    )
    if expect_success:
        assert result.returncode == 0, result.stdout + result.stderr
    values = {
        path.stem: int(path.read_text(encoding="utf-8").strip()) // 60_000
        for path in probe.glob("*.txt")
        if path.name != "retry-attempt.txt"
    }
    return result, values, trace


def _selector_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    for match in re.finditer(r"(?m)^\s*withName:\s*([A-Za-z0-9_]+)\s*\{", text):
        depth = 1
        pos = match.end()
        while depth and pos < len(text):
            if text[pos] == "{":
                depth += 1
            elif text[pos] == "}":
                depth -= 1
            pos += 1
        assert depth == 0, f"unterminated selector for {match.group(1)}"
        blocks[match.group(1)] = text[match.start():pos]
    return blocks


def _trace_attempts(trace: Path, process_name: str) -> list[int]:
    if not trace.exists():
        return []
    with trace.open(encoding="utf-8") as handle:
        rows = csv.DictReader(handle, delimiter="\t")
        return [int(row["attempt"]) for row in rows if row["name"] == process_name]


def test_independent_duration_oracle_matrix() -> None:
    assert expected_minutes(360, 30, 1, 14_400) == 630
    assert expected_minutes(360, 30, 2, 14_400) == 870
    assert expected_minutes(7, 3, 1, 14_400) == 250
    assert expected_minutes(7, 3, 2, 14_400) == 490
    assert expected_minutes(0, 0, 1, 14_400) == 240
    assert expected_minutes(0, 0, 2, 14_400) == 480
    assert expected_minutes(360, 30, 1, 300) == 300
    assert expected_minutes(7, 3, 1, 240) == 240


def test_static_scope_and_retry_contract_are_unchanged() -> None:
    config = CONFIG.read_text(encoding="utf-8")
    base = BASE_CONFIG.read_text(encoding="utf-8")
    main = MAIN_NF.read_text(encoding="utf-8")
    head_config = subprocess.run(
        ["git", "show", "HEAD:nextflow.config"], cwd=ROOT, text=True,
        capture_output=True, check=True,
    ).stdout
    candidate_selectors = _selector_blocks(config)
    head_selectors = _selector_blocks(head_config)
    assert set(candidate_selectors) == set(head_selectors) | {"fast_on_target_detection"}
    assert candidate_selectors["blast_OTU_pretax"] == head_selectors["blast_OTU_pretax"]
    assert config.count("withName: fast_on_target_detection") == 1
    assert FAST_SELECTOR in config
    assert "time = { check_max( 4.h * task.attempt, 'time' ) }" in base
    assert "task.exitStatus in [143,137,104,134,139] ? 'retry' : 'finish'" in base
    assert "maxRetries = 1" in base
    blast = main.split("process blast_OTU_pretax {", 1)[1].split(
        "process _reporting_blast_pretax {", 1
    )[0]
    assert "time '20h'" in blast


@pytest.fixture(scope="module")
def resolved(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, int]]:
    root = tmp_path_factory.mktemp("s6-resolution")
    scenarios: dict[str, list[str]] = {
        "default": [],
        "cli": ["--round_lock_wait_minutes", "7", "--file_wait_minutes", "3"],
        "zero": ["--round_lock_wait_minutes", "0", "--file_wait_minutes", "0"],
        "zero_attempt2": [
            "--round_lock_wait_minutes", "0", "--file_wait_minutes", "0",
            "--probe_retry", "true",
        ],
        "low_cap": ["--max_time", "5h"],
        "custom_low_cap": [
            "--round_lock_wait_minutes", "7", "--file_wait_minutes", "3",
            "--max_time", "4h",
        ],
        "high_cap": ["--max_time", "300h"],
        "stale": ["--stale_lock_ttl_minutes", "999"],
        "lock_seconds": ["--lock_wait_seconds", "999"],
        "scope": ["--round_lock_scope", "dorado_only"],
        "profile_xprize": ["-profile", "xprize"],
        "profile_test": ["-profile", "test"],
        "profile_barcoding": ["-profile", "barcoding"],
        "profile_voucher": ["-profile", "voucher"],
        "profile_broad_its2": ["-profile", "broad_its2"],
        "default_attempt2": ["--probe_retry", "true"],
        "attempt2": [
            "--round_lock_wait_minutes", "7", "--file_wait_minutes", "3",
            "--probe_retry", "true",
        ],
    }
    values = {
        name: _run(root / name, args=args)[1] for name, args in scenarios.items()
    }
    params_file = root / "params.json"
    params_file.write_text(
        json.dumps({"round_lock_wait_minutes": 7, "file_wait_minutes": 3}),
        encoding="utf-8",
    )
    values["params_file"] = _run(
        root / "params-file", args=["-params-file", str(params_file)]
    )[1]
    late_config = root / "late.config"
    late_config.write_text(
        "params.round_lock_wait_minutes = 7\nparams.file_wait_minutes = 3\n",
        encoding="utf-8",
    )
    values["late_config"] = _run(
        root / "late-config", args=["-c", str(late_config)]
    )[1]
    return values


def test_default_and_process_isolation(resolved: dict[str, dict[str, int]]) -> None:
    default = resolved["default"]
    assert default["fast_on_target_detection"] == 630
    assert default["ordinary_probe"] == 240
    assert default["hac_basecalling"] == 240
    assert default["consensus"] == 240
    assert default["backup_update_and_clean"] == 240
    assert default["async_report_render"] == 240
    assert default["blast_OTU_pretax"] == 1_200


def test_overrides_caps_attempts_and_profiles(resolved: dict[str, dict[str, int]]) -> None:
    assert resolved["cli"]["fast_on_target_detection"] == 250
    assert resolved["params_file"]["fast_on_target_detection"] == 250
    assert resolved["late_config"]["fast_on_target_detection"] == 250
    assert resolved["zero"]["fast_on_target_detection"] == 240
    assert resolved["zero_attempt2"]["fast_on_target_detection"] == 480
    assert resolved["low_cap"]["fast_on_target_detection"] == 300
    assert resolved["custom_low_cap"]["fast_on_target_detection"] == 240
    assert resolved["high_cap"]["fast_on_target_detection"] == 630
    assert resolved["default_attempt2"]["fast_on_target_detection"] == 870
    assert resolved["attempt2"]["fast_on_target_detection"] == 490
    for name in (
        "profile_xprize", "profile_test", "profile_barcoding", "profile_voucher",
        "profile_broad_its2",
    ):
        assert resolved[name]["fast_on_target_detection"] == 630
    for name in ("stale", "lock_seconds", "scope"):
        assert resolved[name]["fast_on_target_detection"] == 630


def test_invalid_wait_uses_existing_task_configuration_failure(tmp_path: Path) -> None:
    result, _, _ = _run(
        tmp_path,
        args=["--round_lock_wait_minutes", "not-an-integer"],
        expect_success=False,
    )
    assert result.returncode != 0
    assert "fast_on_target_detection" in result.stdout + result.stderr


def test_timeout_is_terminal_and_retryable_exit_is_unchanged(tmp_path: Path) -> None:
    timeout_result, _, timeout_trace = _run(
        tmp_path / "timeout", source=TIMEOUT_SOURCE, expect_success=False
    )
    assert timeout_result.returncode != 0
    assert _trace_attempts(timeout_trace, "timeout_probe") == [1]

    retry_result, _, retry_trace = _run(tmp_path / "retry", source=RETRY_SOURCE)
    assert retry_result.returncode == 0
    assert _trace_attempts(retry_trace, "retry_exit_probe") == [1, 2]
    assert (tmp_path / "retry" / "probe" / "retry-attempt.txt").read_text(
        encoding="utf-8"
    ).strip() == "2"


def _replace_selector(config: str, replacement: str) -> str:
    assert config.count(FAST_SELECTOR) == 1
    return config.replace(FAST_SELECTOR, replacement)


def test_disposable_mutation_controls(tmp_path: Path) -> None:
    config = CONFIG.read_text(encoding="utf-8")
    base = BASE_CONFIG.read_text(encoding="utf-8")
    direct = lambda expression, name="fast_on_target_detection": (
        f"  withName: {name} {{\n    time = {{ {expression} }}\n  }}\n"
    )
    wait = "((params.round_lock_wait_minutes as int) + (params.file_wait_minutes as int)).minutes"
    cases = [
        ("M1 selector absent", _replace_selector(config, ""), base, PROBE_SOURCE, [], "fast_on_target_detection", 630),
        ("M2 wrong process", _replace_selector(config, direct("check_max(" + wait + " + 4.h * task.attempt, 'time')", "fast_helper")), base, PROBE_SOURCE, [], "fast_on_target_detection", 630),
        ("M3 stale TTL added", _replace_selector(config, direct("check_max(" + wait + " + (params.stale_lock_ttl_minutes as int).minutes + 4.h * task.attempt, 'time')")), base, PROBE_SOURCE, [], "fast_on_target_detection", 630),
        ("M4 waits treated as hours", _replace_selector(config, direct("check_max(((params.round_lock_wait_minutes as int) + (params.file_wait_minutes as int)).hours + 4.h * task.attempt, 'time')")), base, PROBE_SOURCE, [], "fast_on_target_detection", 630),
        ("M5 complete sum scaled", _replace_selector(config, direct("check_max((" + wait + " + 4.h) * task.attempt, 'time')")), base, PROBE_SOURCE, ["--round_lock_wait_minutes", "7", "--file_wait_minutes", "3", "--probe_retry", "true"], "fast_on_target_detection", 490),
        ("M6 attempt scaling removed", _replace_selector(config, direct("check_max(" + wait + " + 4.h, 'time')")), base, PROBE_SOURCE, ["--round_lock_wait_minutes", "7", "--file_wait_minutes", "3", "--probe_retry", "true"], "fast_on_target_detection", 490),
        ("M7 cap bypassed", _replace_selector(config, direct(wait + " + 4.h * task.attempt")), base, PROBE_SOURCE, ["--max_time", "5h"], "fast_on_target_detection", 300),
        ("M8 base-only cap", _replace_selector(config, direct(wait + " + check_max(4.h * task.attempt, 'time')")), base, PROBE_SOURCE, ["--max_time", "5h"], "fast_on_target_detection", 300),
        ("M9 generic base changed", config, base.replace("4.h * task.attempt, 'time'", "8.h * task.attempt, 'time'", 1), PROBE_SOURCE, [], "ordinary_probe", 240),
        ("M10 backup changed", _replace_selector(config, direct("check_max(" + wait + " + 4.h * task.attempt, 'time')", "backup_update_and_clean")), base, PROBE_SOURCE, [], "fast_on_target_detection", 630),
        ("M11 Dorado wait added", _replace_selector(config, direct("check_max(" + wait + " + 360.minutes + 4.h * task.attempt, 'time')")), base, PROBE_SOURCE, [], "fast_on_target_detection", 630),
        ("M12 scope branch", _replace_selector(config, direct("check_max(((params.round_lock_scope == 'full_round' ? params.round_lock_wait_minutes : 0) as int).minutes + (params.file_wait_minutes as int).minutes + 4.h * task.attempt, 'time')")), base, PROBE_SOURCE, ["--round_lock_scope", "dorado_only"], "fast_on_target_detection", 630),
        ("M14 direct max_time", _replace_selector(config, direct("params.max_time as nextflow.util.Duration")), base, PROBE_SOURCE, [], "fast_on_target_detection", 630),
        ("M15 blast time altered", config, base, PROBE_SOURCE.replace("time '20h'", "time '19h'"), [], "blast_OTU_pretax", 1_200),
    ]
    survivors = []
    for index, (name, mutant_config, mutant_base, source, args, process, expected) in enumerate(cases):
        _, values, _ = _run(
            tmp_path / f"mutant-{index}", args=args, config_text=mutant_config,
            base_text=mutant_base, source=source,
        )
        if values.get(process) == expected:
            survivors.append(name)

    retry_all = base.replace(
        "task.exitStatus in [143,137,104,134,139] ? 'retry' : 'finish'",
        "'retry'",
    )
    timeout_result, _, timeout_trace = _run(
        tmp_path / "mutant-timeout-retry", base_text=retry_all,
        source=TIMEOUT_SOURCE, expect_success=False,
    )
    assert timeout_result.returncode != 0
    if _trace_attempts(timeout_trace, "timeout_probe") == [1]:
        survivors.append("M13 timeouts retryable")
    assert not survivors, f"surviving mutants: {survivors}"
