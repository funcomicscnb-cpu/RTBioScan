from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "lib" / "retry.sh"


def _write_attempt_script(path: Path, *, permanent: bool) -> None:
    failure = (
        'echo "Unknown argument: --bad-option" >&2\nexit 2'
        if permanent
        else 'echo "PytorchStreamReader simulated initialization failure" >&2\nexit 42'
    )
    path.write_text(
        f"""#!/usr/bin/env bash
set -eu
counter_file="$1"
count=0
if [ -f "$counter_file" ]; then
  count=$(cat "$counter_file")
fi
count=$((count + 1))
printf '%s\\n' "$count" > "$counter_file"
if [ "$count" -eq 1 ]; then
  {failure}
fi
printf '@HD\\tVN:1.6\\n'
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def test_retry_recovers_from_transient_initialization_failure(tmp_path: Path) -> None:
    command = tmp_path / "transient-command"
    counter = tmp_path / "counter"
    output = tmp_path / "out.sam"
    _write_attempt_script(command, permanent=False)

    cmd = f"""
set -euo pipefail
source "{SCRIPT}"
unset DORADO_LOCK_PATH
export DORADO_RETRY_ATTEMPTS=3
export DORADO_RETRY_SLEEP_SECONDS=0
dorado_basecall_retry "Metal initialization" "{output}" "{command}" "{counter}"
"""
    result = subprocess.run(
        ["bash", "-lc", cmd],
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert counter.read_text(encoding="utf-8") == "2\n"
    assert output.read_text(encoding="utf-8") == "@HD\tVN:1.6\n"
    assert "simulated initialization failure" in result.stderr
    assert "attempt 2/3" in result.stderr


def test_retry_stops_immediately_on_clear_cli_error(tmp_path: Path) -> None:
    command = tmp_path / "permanent-command"
    counter = tmp_path / "counter"
    output = tmp_path / "out.sam"
    _write_attempt_script(command, permanent=True)

    cmd = f"""
set -euo pipefail
source "{SCRIPT}"
unset DORADO_LOCK_PATH
export DORADO_RETRY_ATTEMPTS=3
export DORADO_RETRY_SLEEP_SECONDS=60
dorado_basecall_retry "bad invocation" "{output}" "{command}" "{counter}"
"""
    result = subprocess.run(
        ["bash", "-lc", cmd],
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    assert counter.read_text(encoding="utf-8") == "1\n"
    assert not output.exists()
    assert "non-retryable CLI/configuration error" in result.stderr
    assert "attempt 2/3" not in result.stderr


@pytest.mark.parametrize("exit_status", [126, 127, 132])
def test_retry_stops_immediately_on_permanent_exit_status(
    tmp_path: Path, exit_status: int
) -> None:
    command = tmp_path / "permanent-status-command"
    counter = tmp_path / "counter"
    output = tmp_path / "out.sam"
    command.write_text(
        f"""#!/usr/bin/env bash
set -eu
counter_file="$1"
count=0
if [ -f "$counter_file" ]; then
  count=$(cat "$counter_file")
fi
count=$((count + 1))
printf '%s\\n' "$count" > "$counter_file"
echo "simulated permanent process failure" >&2
exit {exit_status}
""",
        encoding="utf-8",
    )
    command.chmod(0o755)

    cmd = f"""
set -euo pipefail
source "{SCRIPT}"
unset DORADO_LOCK_PATH
export DORADO_RETRY_ATTEMPTS=3
export DORADO_RETRY_SLEEP_SECONDS=60
dorado_basecall_retry "permanent status" "{output}" "{command}" "{counter}"
"""
    result = subprocess.run(
        ["bash", "-lc", cmd],
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
        timeout=5,
    )

    assert result.returncode == 1
    assert counter.read_text(encoding="utf-8") == "1\n"
    assert not output.exists()
    assert f"non-retryable status {exit_status}" in result.stderr
    assert "attempt 2/3" not in result.stderr


def test_realtime_retry_default_is_ten_seconds() -> None:
    config = (REPO_ROOT / "nextflow.config").read_text(encoding="utf-8")
    usage = (REPO_ROOT / "docs" / "usage.md").read_text(encoding="utf-8")
    assert "dorado_retry_sleep_seconds = 10" in config
    assert "Default: `10`." in usage


def test_retry_reclaims_dead_lock_when_hostname_differs_only_by_suffix(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "hostname").write_text("#!/bin/sh\necho testhost.local\n", encoding="utf-8")
    os.chmod(fake_bin / "hostname", 0o755)

    lock_path = tmp_path / "dorado_lock"
    lockdir = Path(f"{lock_path}.lockdir")
    lockdir.mkdir()
    (lockdir / "meta.env").write_text(
        "pid=999999\nhost=testhost.home\nstarted_epoch=1\ndesc=FAST basecalling\nout=out.sam\n",
        encoding="utf-8",
    )

    cmd = f"""
set -euo pipefail
source "{SCRIPT}"
export PATH="{fake_bin}:$PATH"
export DORADO_LOCK_PATH="{lock_path}"
export DORADO_LOCK_WAIT_SECONDS=2
export DORADO_LOCK_TTL_SECONDS=999999
dorado_basecall_retry "test" "{tmp_path / 'out.sam'}" bash -lc 'printf "ok\\n"'
"""
    result = subprocess.run(
        ["bash", "-lc", cmd],
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "reclaiming stale dorado lock (dead pid=999999 host=testhost.home)" in result.stderr.lower()
    assert (tmp_path / "out.sam").read_text(encoding="utf-8") == "ok\n"
