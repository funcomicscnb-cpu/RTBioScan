import os
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
        "${currentResultsStateDir}": str(current_state),
        "${htmlReportAutoRefresh ? 1 : 0}": "0",
        "${htmlReportRefreshSecondsStr}": "15",
        "${htmlReportSamplePlotMaxStr}": "10",
        "${htmlReportUrlPrefix}": "",
        "${ongoingStateDir}": str(ongoing_state),
        "${params.lock_wait_seconds}": "2",
        "${params.outdir}": str(outdir),
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
    helper_dir = paths["base_dir"] / "bin" / "lib"
    helper_dir.mkdir(parents=True)
    shutil.copy2(STALE_LOCK_UTILS, helper_dir / "stale_lock_utils.sh")

    rebuild = paths["base_dir"] / "bin" / "report_rebuild.sh"
    rebuild.parent.mkdir(parents=True, exist_ok=True)
    rebuild.write_text(
        """#!/usr/bin/env bash
set -u
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
exit "$rc"
""",
        encoding="utf-8",
    )
    rebuild.chmod(0o755)

    state_tmp = paths["ongoing_state"] / "_state"
    state_tmp.mkdir(parents=True)
    (state_tmp / "report_history.jsonl").write_text(
        '{"schema_version":"2.0","round_barcode":"round-001"}\n',
        encoding="utf-8",
    )
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
    assert set(stub_log.read_text(encoding="utf-8").splitlines()) == {
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
