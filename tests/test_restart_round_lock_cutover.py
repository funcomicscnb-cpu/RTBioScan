from __future__ import annotations

import gzip
import os
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
HANDLER = Path(
    os.environ.get(
        "RTBIOSCAN_RESTART_HANDLER_UNDER_TEST",
        REPO_ROOT / "bin" / "restart_handler.sh",
    )
)
TOKEN = "a" * 64
OPERATION_A = "1" * 64
OPERATION_B = "2" * 64


def _paths(outdir: Path, state_id: str = "state-a") -> dict[str, Path]:
    return {
        "ongoing": outdir / "temp" / "ongoing" / "state" / state_id,
        "state": outdir / "temp" / "ongoing" / "state" / state_id / "_state",
        "legacy": outdir / "temp" / "current" / "state" / state_id,
        "current": outdir / "current" / "state" / state_id,
        "sentinel": outdir / "temp" / f".restart_applied.{state_id}",
    }


def _run(
    mode: str,
    outdir: Path,
    state_id: str = "state-a",
    *,
    force: str = "1",
    operation_id: str = OPERATION_A,
    run_name: str = "restart-cutover-test",
    path_prefix: Path | None = None,
    handler: Path = HANDLER,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "MODE": mode,
            "OUTDIR": str(outdir),
            "LOCK_WAIT": "2",
            "RUN_NAME": run_name,
            "STATE_ID": state_id,
            "FORCE": force,
            "OPERATION_ID": operation_id,
        }
    )
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run(
        ["/bin/bash", str(handler)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _applied_record(mode: str, operation_id: str = OPERATION_A) -> str:
    return (
        "schema=2\n"
        "status=applied\n"
        f"operation_id={operation_id}\n"
        f"mode={mode}\n"
        "run_name=restart-cutover-test\n"
    )


def _applying_record(mode: str, operation_id: str = OPERATION_A) -> str:
    return (
        "schema=2\n"
        "status=applying\n"
        f"operation_id={operation_id}\n"
        f"requested_mode={mode}\n"
        "run_name=restart-cutover-test\n"
    )


def _make_evidence(path: Path, kind: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "directory":
        path.mkdir()
        payload = path / "durable-evidence.tsv"
    else:
        payload = path
    payload.write_bytes(b"durable round-lock evidence\n")
    return payload


def _identity(path: Path) -> tuple[int, int, int, bytes]:
    st = os.lstat(path)
    return st.st_dev, st.st_ino, st.st_mode, path.read_bytes()


def test_normal_reset_still_wipes_both_mutable_state_roots(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "ordinary-live-state.tsv"
    saved = paths["legacy"] / "tables" / "ordinary-snapshot.tsv"
    live.parent.mkdir(parents=True)
    saved.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    saved.write_text("snapshot\n", encoding="utf-8")

    result = _run("reset", outdir)

    assert result.returncode == 0, result.stderr
    assert list(paths["ongoing"].iterdir()) == []
    assert list(paths["legacy"].iterdir()) == []
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record("reset")


def test_normal_restore_still_replaces_and_merges_rolling_state(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    stale = paths["state"] / "stale.tsv"
    legacy_table = paths["legacy"] / "tables" / "per-round.tsv"
    current_plot = paths["current"] / "plots" / "rolling-plot.tsv"
    consensus = (
        paths["current"]
        / "sequences"
        / "Consensus"
        / "sample-a"
        / "otu.consensus.fasta"
    )
    for path, content in (
        (stale, "stale\n"),
        (legacy_table, "round\n"),
        (current_plot, "plot\n"),
        (consensus, ">otu\nACGT\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    result = _run("restore", outdir)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (paths["state"] / "per-round.tsv").read_text(encoding="utf-8") == "round\n"
    assert (paths["state"] / "rolling-plot.tsv").read_text(encoding="utf-8") == "plot\n"
    assert (
        paths["state"] / "Consensus" / "sample-a" / "otu.consensus.fasta"
    ).read_text(encoding="utf-8") == ">otu\nACGT\n"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record("restore")


def test_same_mode_nonforce_preserves_applied_epoch_and_live_state(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-not-be-wiped.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].write_text(_applied_record("reset"), encoding="utf-8")
    sentinel_before = _identity(paths["sentinel"])
    live_before = _identity(live)

    result = _run(
        "reset", outdir, force="0", operation_id=OPERATION_B, run_name="resumed-run"
    )

    assert result.returncode == 0, result.stderr
    assert _identity(paths["sentinel"]) == sentinel_before
    assert _identity(live) == live_before


def test_same_mode_force_rotates_epoch_and_reapplies_reset(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-be-wiped.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].write_text(_applied_record("reset"), encoding="utf-8")

    result = _run("reset", outdir, force="1", operation_id=OPERATION_B)

    assert result.returncode == 0, result.stderr
    assert not live.exists(), "positive control: forced reset did not reapply the wipe"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "reset", OPERATION_B
    )


def test_mode_switch_rotates_epoch_without_force(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    stale = paths["state"] / "stale.tsv"
    snapshot = paths["legacy"] / "tables" / "restored.tsv"
    stale.parent.mkdir(parents=True)
    snapshot.parent.mkdir(parents=True)
    stale.write_text("stale\n", encoding="utf-8")
    snapshot.write_text("restored\n", encoding="utf-8")
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].write_text(_applied_record("reset"), encoding="utf-8")

    result = _run("restore", outdir, force="0", operation_id=OPERATION_B)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (paths["state"] / "restored.tsv").read_text(encoding="utf-8") == (
        "restored\n"
    )
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "restore", OPERATION_B
    )


def test_restore_without_snapshot_still_finalizes_applied_epoch(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "stale.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("stale\n", encoding="utf-8")

    result = _run("restore", outdir, operation_id=OPERATION_B)

    assert result.returncode == 0, result.stderr
    assert "requested but no snapshot found" in result.stderr
    assert not live.exists(), "positive control: restore did not reach its state wipe"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "restore", OPERATION_B
    )


def test_restore_with_existing_empty_roots_finalizes_applied_epoch(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    paths["legacy"].mkdir(parents=True)
    paths["current"].mkdir(parents=True)

    result = _run("restore", outdir, operation_id=OPERATION_B)

    assert result.returncode == 0, result.stderr
    assert "requested but no snapshot found" in result.stderr
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "restore", OPERATION_B
    )


def test_same_mode_nonforce_accepts_exact_legacy_sentinel_without_rewriting(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-not-be-wiped.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    legacy = "mode=reset\nrun_name=legacy-run\n"
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].write_text(legacy, encoding="utf-8")
    sentinel_before = _identity(paths["sentinel"])

    result = _run("reset", outdir, force="0", operation_id=OPERATION_B)

    assert result.returncode == 0, result.stderr
    assert _identity(paths["sentinel"]) == sentinel_before
    assert live.read_text(encoding="utf-8") == "live\n"


def test_valid_applying_record_is_recovered_by_reapplying_with_new_epoch(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "partial-operation-state.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("partial\n", encoding="utf-8")
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].write_text(_applying_record("reset"), encoding="utf-8")

    result = _run("reset", outdir, force="0", operation_id=OPERATION_B)

    assert result.returncode == 0, result.stderr
    assert (
        not live.exists()
    ), "positive control: applying recovery did not reapply reset"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "reset", OPERATION_B
    )


def test_applying_record_precedes_first_destructive_command_and_retry_recovers(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive-interruption.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_rm = fake_bin / "rm"
    rm_log = tmp_path / "rm-invocation.txt"
    fake_bin.mkdir()
    fake_rm.write_text(
        f"""#!/bin/bash
printf '%s\\n' "$*" > "{rm_log}"
exit 73
""",
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)
    assert fake_rm.is_file() and os.access(
        fake_rm, os.X_OK
    ), "positive control did not install the failing rm"

    interrupted = _run("reset", outdir, operation_id=OPERATION_A, path_prefix=fake_bin)

    assert interrupted.returncode == 73, interrupted.stderr
    assert rm_log.is_file(), "positive control: handler did not reach the failing rm"
    rm_invocation = rm_log.read_text(encoding="utf-8")
    assert "-rf" in rm_invocation and str(paths["state"]) in rm_invocation
    assert live.read_text(encoding="utf-8") == "live\n"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applying_record(
        "reset", OPERATION_A
    )
    assert not Path(f"{paths['sentinel']}.tmp.{OPERATION_A}").exists()

    recovered = _run("reset", outdir, force="0", operation_id=OPERATION_B)

    assert recovered.returncode == 0, recovered.stderr
    assert not live.exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "reset", OPERATION_B
    )


def test_restore_copy_failure_stays_applying_and_returns_nonzero(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    snapshot = paths["legacy"] / "tables" / "must-be-restored.tsv"
    destination = paths["state"] / snapshot.name
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("snapshot\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_cp = fake_bin / "cp"
    cp_log = tmp_path / "cp-exit-73-invocation.txt"
    fake_bin.mkdir()
    fake_cp.write_text(
        f"""#!/bin/bash
printf 'FAKE_CP_EXIT_73\\n%s\\n' "$*" > "{cp_log}"
exit 73
""",
        encoding="utf-8",
    )
    fake_cp.chmod(0o755)
    assert fake_cp.is_file() and os.access(
        fake_cp, os.X_OK
    ), "positive control did not install the failing cp"

    result = _run("restore", outdir, operation_id=OPERATION_A, path_prefix=fake_bin)

    assert cp_log.is_file(), "positive control: handler did not reach the failing cp"
    cp_invocation = cp_log.read_text(encoding="utf-8")
    assert cp_invocation.startswith("FAKE_CP_EXIT_73\n")
    assert str(snapshot) in cp_invocation
    assert result.returncode == 73, (result.stdout, result.stderr)
    assert snapshot.read_text(encoding="utf-8") == "snapshot\n"
    assert not destination.exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == _applying_record(
        "restore", OPERATION_A
    )

    recovered = _run("restore", outdir, force="0", operation_id=OPERATION_B)

    assert recovered.returncode == 0, recovered.stderr
    assert destination.read_text(encoding="utf-8") == "snapshot\n"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "restore", OPERATION_B
    )


def test_restore_consensus_copy_failure_stays_applying_and_retry_recovers(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    snapshot = (
        paths["current"]
        / "sequences"
        / "Consensus"
        / "sample-a"
        / "otu.consensus.fasta"
    )
    destination = paths["state"] / "Consensus" / "sample-a" / snapshot.name
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(">otu\nACGT\n", encoding="utf-8")
    (paths["current"] / "tables").mkdir()
    fake_bin = tmp_path / "fake-bin"
    fake_cp = fake_bin / "cp"
    cp_log = tmp_path / "consensus-cp-exit-73-invocation.txt"
    fake_bin.mkdir()
    fake_cp.write_text(
        f"""#!/bin/bash
printf 'FAKE_CONSENSUS_CP_EXIT_73\\n%s\\n' "$*" > "{cp_log}"
exit 73
""",
        encoding="utf-8",
    )
    fake_cp.chmod(0o755)

    result = _run("restore", outdir, operation_id=OPERATION_A, path_prefix=fake_bin)

    assert cp_log.is_file(), "positive control: handler did not reach consensus cp"
    assert "sequences/Consensus/." in cp_log.read_text(encoding="utf-8")
    assert result.returncode == 73, (result.stdout, result.stderr)
    assert not destination.exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == _applying_record(
        "restore", OPERATION_A
    )

    recovered = _run("restore", outdir, force="0", operation_id=OPERATION_B)

    assert recovered.returncode == 0, recovered.stderr
    assert destination.read_text(encoding="utf-8") == ">otu\nACGT\n"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "restore", OPERATION_B
    )


def test_restore_corrupt_gzip_stays_applying_and_retry_recovers(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    snapshot = paths["legacy"] / "tables" / "rolling_rpt.txt.gz"
    restored_gzip = paths["state"] / snapshot.name
    restored_text = restored_gzip.with_suffix("")
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(b"not a gzip stream\n")

    result = _run("restore", outdir, operation_id=OPERATION_A)

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert restored_gzip.read_bytes() == b"not a gzip stream\n"
    assert restored_text.exists(), "positive control: gunzip output was not opened"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applying_record(
        "restore", OPERATION_A
    )

    snapshot.write_bytes(gzip.compress(b"restored report\n"))
    recovered = _run("restore", outdir, force="0", operation_id=OPERATION_B)

    assert recovered.returncode == 0, recovered.stderr
    assert restored_text.read_text(encoding="utf-8") == "restored report\n"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "restore", OPERATION_B
    )


def test_reset_parser_transaction_cleanup_failure_stays_applying_and_recovers(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    parser_txn = paths["ongoing"] / ".parser_state_txn"
    marker = parser_txn / "partial.tsv"
    parser_txn.mkdir(parents=True)
    marker.write_text("partial\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_rm = fake_bin / "rm"
    rm_log = tmp_path / "parser-rm-exit-75-invocation.txt"
    fake_bin.mkdir()
    fake_rm.write_text(
        f"""#!/bin/bash
case "$*" in
    *'.parser_state_txn'*)
        printf 'FAKE_PARSER_RM_EXIT_75\\n%s\\n' "$*" > "{rm_log}"
        exit 75
        ;;
esac
exec /bin/rm "$@"
""",
        encoding="utf-8",
    )
    fake_rm.chmod(0o755)

    result = _run("reset", outdir, operation_id=OPERATION_A, path_prefix=fake_bin)

    assert rm_log.is_file(), "positive control: handler did not reach parser cleanup rm"
    assert str(parser_txn) in rm_log.read_text(encoding="utf-8")
    assert result.returncode == 75, (result.stdout, result.stderr)
    assert marker.read_text(encoding="utf-8") == "partial\n"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applying_record(
        "reset", OPERATION_A
    )

    recovered = _run("reset", outdir, force="0", operation_id=OPERATION_B)

    assert recovered.returncode == 0, recovered.stderr
    assert not parser_txn.exists()
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record(
        "reset", OPERATION_B
    )


def test_exit_cleanup_preserves_fatal_status_after_applying(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    mutated_handler = tmp_path / "restart-handler-with-fatal.sh"
    source = HANDLER.read_text(encoding="utf-8")
    needle = 'write_restart_sentinel applying\n\nif [ "$MODE" = "reset" ]; then'
    replacement = (
        "write_restart_sentinel applying\n"
        'printf "%s\\n" "$INTENTIONAL_FATAL_AFTER_APPLY"\n\n'
        'if [ "$MODE" = "reset" ]; then'
    )
    assert source.count(needle) == 1, "positive control: fatal mutation anchor drifted"
    mutated = source.replace(needle, replacement, 1)
    assert mutated != source and "$INTENTIONAL_FATAL_AFTER_APPLY" in mutated
    mutated_handler.write_text(mutated, encoding="utf-8")

    result = _run("reset", outdir, operation_id=OPERATION_A, handler=mutated_handler)

    assert result.returncode != 0, (result.stdout, result.stderr)
    assert "INTENTIONAL_FATAL_AFTER_APPLY" in result.stderr
    assert paths["sentinel"].read_text(encoding="utf-8") == _applying_record(
        "reset", OPERATION_A
    )
    assert not Path(f"{paths['sentinel']}.lockdir").exists()


@pytest.mark.parametrize(
    "record",
    [
        "schema=2\nstatus=applied\n",
        _applied_record("reset") + "unexpected=field\n",
        _applied_record("reset").replace("status=applied\n", ""),
        _applied_record("reset").replace("operation_id=", "operation_id=A"),
        _applying_record("reset").replace("requested_mode=", "mode="),
        "mode=reset\nrun_name=legacy-run",
        "mode=reset\nrun_name=legacy-run\r\n",
        _applied_record("reset").replace(
            "run_name=restart-cutover-test\n", "run_name=bad\r\n"
        ),
        _applying_record("reset").replace(
            "run_name=restart-cutover-test\n", "run_name=bad\r\n"
        ),
    ],
)
def test_malformed_sentinel_fails_closed_before_state_mutation(
    tmp_path: Path,
    record: str,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].write_text(record, encoding="utf-8")
    sentinel_before = _identity(paths["sentinel"])
    live_before = _identity(live)

    result = _run("reset", outdir, force="1", operation_id=OPERATION_B)

    assert result.returncode != 0, (record, result.stdout, result.stderr)
    assert "malformed restart sentinel" in result.stderr
    assert _identity(paths["sentinel"]) == sentinel_before
    assert _identity(live) == live_before


@pytest.mark.parametrize(
    "record",
    [
        b"mode=reset\nrun_name=bad\xff\n",
        b"mode=reset\nrun_name=bad\0evil\n",
    ],
)
def test_nontext_sentinel_fails_closed_before_state_mutation(
    tmp_path: Path,
    record: bytes,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].write_bytes(record)
    sentinel_before = _identity(paths["sentinel"])
    live_before = _identity(live)

    result = _run("reset", outdir, force="1", operation_id=OPERATION_B)

    assert result.returncode != 0, (record, result.stdout, result.stderr)
    assert "failed to read restart sentinel" in result.stderr
    assert _identity(paths["sentinel"]) == sentinel_before
    assert _identity(live) == live_before


def test_symlink_sentinel_fails_closed_without_reading_or_mutating_target(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    outside = tmp_path / "outside-sentinel"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    outside.write_text(_applied_record("reset"), encoding="utf-8")
    paths["sentinel"].parent.mkdir(parents=True, exist_ok=True)
    paths["sentinel"].symlink_to(outside)
    link_before = os.lstat(paths["sentinel"])
    outside_before = _identity(outside)
    live_before = _identity(live)

    result = _run("reset", outdir, force="1", operation_id=OPERATION_B)

    assert result.returncode != 0, result.stderr
    assert "restart sentinel is a symlink" in result.stderr
    link_after = os.lstat(paths["sentinel"])
    assert (link_after.st_dev, link_after.st_ino, link_after.st_mode) == (
        link_before.st_dev,
        link_before.st_ino,
        link_before.st_mode,
    )
    assert _identity(outside) == outside_before
    assert _identity(live) == live_before


@pytest.mark.parametrize("operation_id", ["", "a" * 63, "A" * 64, "g" * 64])
def test_invalid_operation_id_fails_before_state_mutation(
    tmp_path: Path,
    operation_id: str,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")
    live_before = _identity(live)

    result = _run("reset", outdir, operation_id=operation_id)

    assert result.returncode != 0, result.stderr
    assert "OPERATION_ID must be exactly 64 lowercase hexadecimal" in result.stderr
    assert _identity(live) == live_before
    assert not paths["sentinel"].exists()


def test_valid_state_id_path_component_is_accepted(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    state_id = "sample.alpha_1-2"
    paths = _paths(outdir, state_id)
    live = paths["state"] / "ordinary.tsv"
    live.parent.mkdir(parents=True)
    live.write_text("live\n", encoding="utf-8")

    result = _run("reset", outdir, state_id=state_id)

    assert result.returncode == 0, result.stderr
    assert not live.exists(), "positive control: valid STATE_ID did not reach reset"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record("reset")


@pytest.mark.parametrize("state_id", ["", ".", "..", "nested/state", "state id"])
def test_unsafe_state_id_fails_before_path_mutation(
    tmp_path: Path,
    state_id: str,
) -> None:
    outdir = tmp_path / "results"
    protected = (
        outdir / "temp" / "ongoing" / "state" / "protected-state" / "must-survive.tsv"
    )
    protected.parent.mkdir(parents=True)
    protected.write_text("must survive\n", encoding="utf-8")
    protected_before = _identity(protected)

    result = _run("reset", outdir, state_id=state_id)

    assert result.returncode != 0, (state_id, result.stdout, result.stderr)
    assert "STATE_ID must be one safe pathname component" in result.stderr
    assert _identity(protected) == protected_before
    assert list((outdir / "temp").glob(".restart_applied*")) == []


def test_restore_accepts_backup_generated_to_figures_symlinks_without_copying_them(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    stale = paths["state"] / "stale.tsv"
    table = paths["current"] / "tables" / "barcode_reads_time_rpt.txt"
    presentation_link = (
        paths["current"]
        / "tables"
        / "to_figures"
        / table.name
    )
    stale.parent.mkdir(parents=True)
    table.parent.mkdir(parents=True)
    presentation_link.parent.mkdir(parents=True)
    stale.write_text("stale\n", encoding="utf-8")
    table.write_text("round\n", encoding="utf-8")
    presentation_link.symlink_to(table)
    assert presentation_link.is_symlink(), "positive control did not create presentation link"

    result = _run("restore", outdir)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (paths["state"] / table.name).read_text(encoding="utf-8") == "round\n"
    assert not (paths["state"] / "to_figures").exists()
    assert presentation_link.is_symlink()
    assert presentation_link.read_text(encoding="utf-8") == "round\n"
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record("restore")


def test_restore_never_flat_copies_a_presentation_only_structured_snapshot(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    legacy_table = paths["legacy"] / "tables" / "per-round.tsv"
    outside = tmp_path / "outside.tsv"
    presentation_link = (
        paths["current"]
        / "tables"
        / "to_figures"
        / "barcode_reads_time_rpt.txt"
    )
    legacy_table.parent.mkdir(parents=True)
    presentation_link.parent.mkdir(parents=True)
    legacy_table.write_text("round\n", encoding="utf-8")
    outside.write_text("outside\n", encoding="utf-8")
    presentation_link.symlink_to(outside)
    assert presentation_link.is_symlink(), "positive control did not create presentation link"

    result = _run("restore", outdir)

    assert result.returncode == 0, result.stderr
    assert (paths["state"] / legacy_table.name).read_text(encoding="utf-8") == "round\n"
    assert not (paths["state"] / "tables").exists()
    assert not (paths["state"] / "to_figures").exists()
    assert outside.read_text(encoding="utf-8") == "outside\n"


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        (".round_inflight.lockdir", "directory"),
        (f".round_inflight.lockdir.reclaim-{TOKEN}", "directory"),
        (f".round_inflight.lockdir.release-{TOKEN}", "directory"),
        (f".round_inflight.lockdir.failed-acquire-{TOKEN}", "directory"),
        (f".round_inflight.lockdir.operator-{TOKEN}", "directory"),
        (".round_lock_handoff.legacy-round", "file"),
        (f".round_lock_handoff.{TOKEN}.tsv", "file"),
        (f".round_lock_release.{TOKEN}.tsv", "file"),
        (f".round_lock_finish.{TOKEN}.tsv", "file"),
        (f".round_lock_revocation.{TOKEN}.tsv", "file"),
        (f".round_inflight.{TOKEN}.tsv", "file"),
        ("round_inflight.txt", "file"),
        (".round_lock_events", "directory"),
        (".round_lock_operator_events", "directory"),
        (".round_lock_operator_pending", "directory"),
        (".round_lock_archives", "directory"),
    ],
)
def test_reset_refuses_every_reserved_live_namespace_without_mutation(
    tmp_path: Path,
    name: str,
    kind: str,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    protected = paths["state"] / name
    payload = _make_evidence(protected, kind)
    protected_before = os.lstat(protected)
    payload_before = _identity(payload)

    result = _run("reset", outdir)

    assert result.returncode != 0, (name, result.stdout, result.stderr)
    assert "protected round-lock namespace in live ongoing state" in result.stderr
    assert name in result.stderr
    protected_after = os.lstat(protected)
    assert (protected_after.st_dev, protected_after.st_ino, protected_after.st_mode) == (
        protected_before.st_dev,
        protected_before.st_ino,
        protected_before.st_mode,
    )
    assert _identity(payload) == payload_before
    assert not paths["sentinel"].exists()


def test_reset_refuses_protected_evidence_in_the_snapshot_it_would_wipe(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "ordinary-live-state.tsv"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"must survive refused reset\n")
    live_before = _identity(live)
    protected = paths["legacy"] / "tables" / f".round_lock_release.{TOKEN}.tsv"
    _make_evidence(protected, "file")
    protected_before = _identity(protected)

    result = _run("reset", outdir)

    assert result.returncode != 0, result.stderr
    assert "protected round-lock namespace in reset snapshot state" in result.stderr
    assert _identity(live) == live_before
    assert _identity(protected) == protected_before
    assert not paths["sentinel"].exists()


def test_restore_refuses_hidden_protected_snapshot_entry_before_wiping_live_state(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "ordinary-live-state.tsv"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"must survive refused restore\n")
    live_before = _identity(live)
    protected = (
        paths["current"]
        / "sequences"
        / "Consensus"
        / "sample-a"
        / f".round_lock_revocation.{TOKEN}.tsv"
    )
    _make_evidence(protected, "file")
    protected_before = _identity(protected)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "protected round-lock namespace in restore snapshot state" in result.stderr
    assert _identity(live) == live_before
    assert _identity(protected) == protected_before
    assert not (paths["state"] / "Consensus" / "sample-a" / protected.name).exists()
    assert not paths["sentinel"].exists()


def test_restore_refuses_snapshot_symlink_before_wiping_or_copying(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "ordinary-live-state.tsv"
    outside = tmp_path / "outside.tsv"
    unsafe = paths["legacy"] / "tables" / "unsafe.tsv"
    live.parent.mkdir(parents=True)
    unsafe.parent.mkdir(parents=True)
    live.write_bytes(b"must survive refused restore\n")
    outside.write_bytes(b"outside snapshot target\n")
    unsafe.symlink_to(outside)
    live_before = _identity(live)
    unsafe_before = os.lstat(unsafe)
    outside_before = _identity(outside)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert _identity(live) == live_before
    unsafe_after = os.lstat(unsafe)
    assert (unsafe_after.st_dev, unsafe_after.st_ino, unsafe_after.st_mode) == (
        unsafe_before.st_dev,
        unsafe_before.st_ino,
        unsafe_before.st_mode,
    )
    assert _identity(outside) == outside_before
    assert not (paths["state"] / unsafe.name).exists()
    assert not paths["sentinel"].exists()


def test_restart_refuses_symlink_at_the_state_fence_path(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    outside_state = tmp_path / "outside-state"
    outside_payload = outside_state / "ordinary.tsv"
    outside_state.mkdir()
    outside_payload.write_bytes(b"outside state must survive\n")
    paths["ongoing"].mkdir(parents=True)
    paths["state"].symlink_to(outside_state, target_is_directory=True)
    link_before = os.lstat(paths["state"])
    payload_before = _identity(outside_payload)

    result = _run("reset", outdir)

    assert result.returncode != 0, result.stderr
    assert "round-lock state-fence path is a symlink" in result.stderr
    link_after = os.lstat(paths["state"])
    assert (link_after.st_dev, link_after.st_ino, link_after.st_mode) == (
        link_before.st_dev,
        link_before.st_ino,
        link_before.st_mode,
    )
    assert _identity(outside_payload) == payload_before
    assert not paths["sentinel"].exists()
