from __future__ import annotations

import gzip
import hashlib
import os
import shutil
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
REPORT_LIVE_STAGE = REPO_ROOT / "bin" / "report_live_stage.sh"
REPORT_LIVE_PUBLISH = REPO_ROOT / "bin" / "report_live_publish.sh"
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


def _make_structured_current_root(current: Path) -> Path:
    witness = current / "tables" / "structured-layout-witness.tsv"
    witness.parent.mkdir(parents=True, exist_ok=True)
    witness.write_bytes(b"real structured layout witness\n")
    return witness


def _build_published_current_state(tmp_path: Path, outdir: Path) -> dict[str, Path]:
    inputs = tmp_path / "published-state-inputs"
    stage_root = inputs / "stage"
    round_dir = inputs / "round"
    state_dir = inputs / "state"
    consensus_dir = inputs / "consensus"
    report_asset = stage_root / "report_assets" / "overview.png"
    round_table = round_dir / "round-table.tsv"
    round_sequence = round_dir / "round-sequence.fasta"
    state_table = state_dir / "barcode01_read_info_rpt.txt"
    consensus = consensus_dir / "sample-a" / "otu.consensus.fasta"
    for path, content in (
        (report_asset, b"fixture-png-bytes\n"),
        (round_table, b"sample\treads\nsample-a\t4\n"),
        (round_sequence, b">round-sequence\nACGT\n"),
        (state_table, b"sample-a\t4\n"),
        (consensus, b">otu-a\nACGTACGT\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    staged = subprocess.run(
        [
            "/bin/bash",
            str(REPORT_LIVE_STAGE),
            "--stage-root",
            str(stage_root),
            "--round-dir",
            str(round_dir),
            "--state-dir",
            str(state_dir),
            "--consensus-dir",
            str(consensus_dir),
            "--barcode",
            "barcode01",
            "--run-id",
            "restart-restore-test",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert staged.returncode == 0, staged.stderr

    current = outdir / "current" / "state" / "state-a"
    current.mkdir(parents=True)
    for name in ("tables", "plots", "sequences"):
        shutil.copytree(stage_root / name, current / name, symlinks=True)

    published = subprocess.run(
        [
            "/bin/bash",
            str(REPORT_LIVE_PUBLISH),
            "--stage-root",
            str(stage_root),
            "--state-root",
            str(current),
            "--run-asset-root",
            str(outdir / "assets"),
            "--round-barcode",
            "round-001",
            "--lock-path",
            str(tmp_path / "live-publish"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert published.returncode == 0, published.stderr

    payloads = list((current / ".live_round_payloads").iterdir())
    assert len(payloads) == 1
    payload = payloads[0]
    live_round = current / "live_round"
    payload_asset = payload / "report_assets" / "overview.png"
    assert live_round.is_symlink()
    assert os.readlink(live_round) == f".live_round_payloads/{payload.name}"
    assert payload_asset.is_symlink()
    assert os.readlink(payload_asset) == "../plots/png/overview.png"
    return {
        "current": current,
        "payload": payload,
        "live_round": live_round,
        "payload_asset": payload_asset,
    }


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
    assert [item.name for item in paths["ongoing"].iterdir()] == ["_state"]
    assert {item.name for item in paths["state"].iterdir()} == set(CONTROL)
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
    fake_cp = fake_bin / "perl"
    real_perl = shutil.which("perl")
    assert real_perl is not None
    cp_log = tmp_path / "cp-exit-73-invocation.txt"
    fake_bin.mkdir()
    fake_cp.write_text(
        f"""#!/bin/bash
case "$2" in overlay-copy|compat-copy) ;; *) exec "{real_perl}" "$@" ;; esac
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
    fake_cp = fake_bin / "perl"
    real_perl = shutil.which("perl")
    assert real_perl is not None
    cp_log = tmp_path / "consensus-cp-exit-73-invocation.txt"
    fake_bin.mkdir()
    fake_cp.write_text(
        f"""#!/bin/bash
case "$2" in overlay-copy|compat-copy) ;; *) exec "{real_perl}" "$@" ;; esac
printf 'FAKE_CONSENSUS_CP_EXIT_73\\n%s\\n' "$*" > "{cp_log}"
exit 73
""",
        encoding="utf-8",
    )
    fake_cp.chmod(0o755)

    result = _run("restore", outdir, operation_id=OPERATION_A, path_prefix=fake_bin)

    assert cp_log.is_file(), "positive control: handler did not reach consensus cp"
    assert "sequences/Consensus" in cp_log.read_text(encoding="utf-8")
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
    outdir.mkdir()
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

    assert result.returncode != 0
    assert "unsafe presentation alias" in result.stderr.lower(), result.stderr
    assert legacy_table.read_text(encoding="utf-8") == "round\n"
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert presentation_link.is_symlink()
    assert presentation_link.readlink() == outside
    assert not paths["sentinel"].exists()
    assert not paths["ongoing"].exists()


def test_restore_accepts_published_live_round_without_copying_presentation_artifacts(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    published = _build_published_current_state(tmp_path, outdir)
    stale = paths["state"] / "stale.tsv"
    stale.parent.mkdir(parents=True)
    stale.write_text("stale\n", encoding="utf-8")

    result = _run("restore", outdir)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (paths["state"] / "round-table.tsv").read_bytes() == (
        b"sample\treads\nsample-a\t4\n"
    )
    assert (paths["state"] / "png" / "overview.png").read_bytes() == (
        b"fixture-png-bytes\n"
    )
    restored_consensus = paths["state"] / "Consensus" / "sample-a" / "otu.consensus.fasta"
    assert restored_consensus.read_bytes() == b">otu-a\nACGTACGT\n"
    assert (
        paths["ongoing"] / "Consensus" / "sample-a" / "otu.consensus.fasta"
    ).read_bytes() == restored_consensus.read_bytes()
    assert published["live_round"].is_symlink()
    assert published["payload_asset"].is_symlink()
    assert not os.path.lexists(paths["ongoing"] / "live_round")
    assert not os.path.lexists(paths["ongoing"] / ".live_round_payloads")
    assert not os.path.lexists(paths["state"] / "live_round")
    assert not os.path.lexists(paths["state"] / ".live_round_payloads")
    assert not any(path.name == "report_assets" for path in paths["ongoing"].rglob("*"))
    assert not any(path.is_symlink() for path in paths["ongoing"].rglob("*"))
    assert not any(
        path.name in {"live_round", ".live_round_payloads", "report_assets"}
        for path in (paths["state"] / "Consensus").rglob("*")
    )
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record("restore")


@pytest.mark.parametrize(
    ("removed", "expected_path", "expected_bytes"),
    [
        ("plots", ("round-table.tsv",), b"sample\treads\nsample-a\t4\n"),
        ("tables", ("png", "overview.png"), b"fixture-png-bytes\n"),
    ],
)
def test_restore_accepts_published_live_round_with_one_structured_directory(
    tmp_path: Path,
    removed: str,
    expected_path: tuple[str, ...],
    expected_bytes: bytes,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    published = _build_published_current_state(tmp_path, outdir)
    shutil.rmtree(published["current"] / removed)
    stale = paths["state"] / "stale.tsv"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"stale\n")

    result = _run("restore", outdir)

    assert result.returncode == 0, result.stderr
    assert not stale.exists()
    assert (paths["state"].joinpath(*expected_path)).read_bytes() == expected_bytes
    restored_consensus = paths["state"] / "Consensus" / "sample-a" / "otu.consensus.fasta"
    assert restored_consensus.read_bytes() == b">otu-a\nACGTACGT\n"
    assert (
        paths["ongoing"] / "Consensus" / "sample-a" / "otu.consensus.fasta"
    ).read_bytes() == restored_consensus.read_bytes()
    assert published["live_round"].is_symlink()
    assert published["payload_asset"].is_symlink()
    assert not os.path.lexists(paths["ongoing"] / "live_round")
    assert not os.path.lexists(paths["ongoing"] / ".live_round_payloads")
    assert not os.path.lexists(paths["state"] / "live_round")
    assert not os.path.lexists(paths["state"] / ".live_round_payloads")
    assert not any(path.is_symlink() for path in paths["ongoing"].rglob("*"))
    assert paths["sentinel"].read_text(encoding="utf-8") == _applied_record("restore")


def test_restore_refuses_published_presentation_symlink_without_structured_layout(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    published = _build_published_current_state(tmp_path, outdir)
    shutil.rmtree(published["current"] / "tables")
    shutil.rmtree(published["current"] / "plots")
    outside = tmp_path / "outside-presentation-target"
    outside.write_bytes(b"outside target must survive\n")
    published["payload_asset"].unlink()
    published["payload_asset"].symlink_to(outside)
    live = paths["state"] / "must-survive.tsv"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"must survive refused restore\n")
    live_before_bytes = live.read_bytes()
    live_before_sha256 = hashlib.sha256(live_before_bytes).hexdigest()
    live_before_stat = os.lstat(live)
    payload_link_before = os.lstat(published["payload_asset"])
    outside_before = _identity(outside)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert str(published["payload_asset"]) in result.stderr
    live_after_stat = os.lstat(live)
    assert live.read_bytes() == live_before_bytes
    assert hashlib.sha256(live.read_bytes()).hexdigest() == live_before_sha256
    assert (
        live_after_stat.st_dev,
        live_after_stat.st_ino,
        live_after_stat.st_mode,
    ) == (
        live_before_stat.st_dev,
        live_before_stat.st_ino,
        live_before_stat.st_mode,
    )
    payload_link_after = os.lstat(published["payload_asset"])
    assert (
        payload_link_after.st_dev,
        payload_link_after.st_ino,
        payload_link_after.st_mode,
    ) == (
        payload_link_before.st_dev,
        payload_link_before.st_ino,
        payload_link_before.st_mode,
    )
    assert _identity(outside) == outside_before
    assert not paths["sentinel"].exists()
    assert not os.path.lexists(paths["ongoing"] / "live_round")
    assert not os.path.lexists(paths["ongoing"] / ".live_round_payloads")
    assert not os.path.lexists(paths["state"] / "live_round")
    assert not os.path.lexists(paths["state"] / ".live_round_payloads")


@pytest.mark.parametrize("name", ["live_round", ".live_round_payloads"])
def test_restore_refuses_direct_legacy_presentation_symlink_before_mutation(
    tmp_path: Path,
    name: str,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    outside = tmp_path / f"outside-{name}"
    unsafe = paths["legacy"] / name
    table = paths["legacy"] / "tables" / "safe.tsv"
    live.parent.mkdir(parents=True)
    unsafe.parent.mkdir(parents=True)
    table.parent.mkdir(parents=True)
    live.write_bytes(b"must survive refused restore\n")
    outside.write_bytes(b"outside target must survive\n")
    table.write_bytes(b"safe structured content\n")
    unsafe.symlink_to(outside)
    live_before = _identity(live)
    outside_before = _identity(outside)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert str(unsafe) in result.stderr
    assert _identity(live) == live_before
    assert _identity(outside) == outside_before
    assert not paths["sentinel"].exists()


def test_restore_refuses_unrelated_direct_current_symlink_before_mutation(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    outside = tmp_path / "outside.tsv"
    unsafe = paths["current"] / "unrelated-link"
    live.parent.mkdir(parents=True)
    unsafe.parent.mkdir(parents=True)
    _make_structured_current_root(paths["current"])
    live.write_bytes(b"must survive refused restore\n")
    outside.write_bytes(b"outside target\n")
    unsafe.symlink_to(outside)
    live_before = _identity(live)
    unsafe_before = os.lstat(unsafe)
    outside_before = _identity(outside)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert str(unsafe) in result.stderr
    assert _identity(live) == live_before
    unsafe_after = os.lstat(unsafe)
    assert (unsafe_after.st_dev, unsafe_after.st_ino, unsafe_after.st_mode) == (
        unsafe_before.st_dev,
        unsafe_before.st_ino,
        unsafe_before.st_mode,
    )
    assert _identity(outside) == outside_before
    assert not paths["sentinel"].exists()


def test_restore_refuses_nested_sequence_symlink_before_mutation(tmp_path: Path) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    outside = tmp_path / "outside.tsv"
    unsafe = paths["current"] / "sequences" / "sample-a" / "unsafe.tsv"
    live.parent.mkdir(parents=True)
    unsafe.parent.mkdir(parents=True)
    _make_structured_current_root(paths["current"])
    live.write_bytes(b"must survive refused restore\n")
    outside.write_bytes(b"outside target\n")
    unsafe.symlink_to(outside)
    live_before = _identity(live)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert str(unsafe) in result.stderr
    assert _identity(live) == live_before
    assert not paths["sentinel"].exists()


def test_restore_does_not_exempt_nested_symlink_named_live_round(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    live = paths["state"] / "must-survive.tsv"
    outside = tmp_path / "outside-live-round"
    unsafe = paths["current"] / "sequences" / "nested" / "live_round"
    live.parent.mkdir(parents=True)
    unsafe.parent.mkdir(parents=True)
    _make_structured_current_root(paths["current"])
    live.write_bytes(b"must survive refused restore\n")
    outside.write_bytes(b"outside target\n")
    unsafe.symlink_to(outside)
    live_before = _identity(live)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert str(unsafe) in result.stderr
    assert _identity(live) == live_before
    assert not paths["sentinel"].exists()


def test_restore_structured_published_state_refuses_nested_live_round_before_mutation(
    tmp_path: Path,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    published = _build_published_current_state(tmp_path, outdir)
    unsafe = published["current"] / "sequences" / "Consensus" / "live_round"
    outside = tmp_path / "outside-live-round"
    live = paths["state"] / "must-survive.tsv"
    outside.write_bytes(b"outside target must survive\n")
    unsafe.symlink_to(outside)
    live.parent.mkdir(parents=True)
    live.write_bytes(b"must survive refused restore\n")
    live_before_bytes = live.read_bytes()
    live_before_sha256 = hashlib.sha256(live_before_bytes).hexdigest()
    live_before_stat = os.lstat(live)
    outside_before = _identity(outside)

    result = _run("restore", outdir)

    assert result.returncode != 0, result.stderr
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert str(unsafe) in result.stderr
    live_after_stat = os.lstat(live)
    assert live.read_bytes() == live_before_bytes
    assert hashlib.sha256(live.read_bytes()).hexdigest() == live_before_sha256
    assert (
        live_after_stat.st_dev,
        live_after_stat.st_ino,
        live_after_stat.st_mode,
    ) == (
        live_before_stat.st_dev,
        live_before_stat.st_ino,
        live_before_stat.st_mode,
    )
    assert _identity(outside) == outside_before
    assert not paths["sentinel"].exists()
    assert not os.path.lexists(f"{paths['sentinel']}.lockdir")
    assert not os.path.lexists(f"{paths['sentinel']}.tmp.{OPERATION_A}")
    assert not os.path.lexists(paths["ongoing"] / "Consensus" / "live_round")
    assert not os.path.lexists(paths["state"] / "round-table.tsv")
    assert not os.path.lexists(paths["state"] / "png" / "overview.png")
    assert not os.path.lexists(paths["ongoing"] / "live_round")
    assert not os.path.lexists(paths["ongoing"] / ".live_round_payloads")


@pytest.mark.parametrize("entry_name", ["tables", "plots"])
@pytest.mark.parametrize(
    "shape",
    ["symlink_to_directory", "regular_file", "dangling_symlink"],
)
def test_restore_refuses_non_directory_structured_boundary_before_mutation(
    tmp_path: Path,
    entry_name: str,
    shape: str,
) -> None:
    outdir = tmp_path / "results"
    paths = _paths(outdir)
    published = _build_published_current_state(tmp_path, outdir)
    other_entry = "plots" if entry_name == "tables" else "tables"
    shutil.rmtree(published["current"] / other_entry)
    tested_entry = published["current"] / entry_name
    shutil.rmtree(tested_entry)
    external_witness = None
    missing_target = None
    if shape == "symlink_to_directory":
        external_target = tmp_path / f"outside-{entry_name}"
        external_witness = external_target / "must-survive.tsv"
        external_witness.parent.mkdir()
        external_witness.write_bytes(b"outside target must survive\n")
        tested_entry.symlink_to(external_target, target_is_directory=True)
        expected_unsafe = tested_entry
    elif shape == "regular_file":
        tested_entry.write_bytes(b"not a structured directory\n")
        expected_unsafe = published["payload_asset"]
    else:
        missing_target = tmp_path / f"missing-{entry_name}"
        tested_entry.symlink_to(missing_target, target_is_directory=True)
        expected_unsafe = published["payload_asset"]
    live = paths["state"] / "must-survive.tsv"
    live.parent.mkdir(parents=True)
    live.write_bytes(b"must survive refused restore\n")
    live_before = _identity(live)
    external_before = (
        _identity(external_witness) if external_witness is not None else None
    )

    result = _run("restore", outdir)

    assert result.returncode != 0, (entry_name, shape, result.stderr)
    assert "unsafe symlink in restore snapshot state" in result.stderr
    assert str(expected_unsafe) in result.stderr
    assert _identity(live) == live_before
    if external_witness is not None:
        assert _identity(external_witness) == external_before
    if missing_target is not None:
        assert not os.path.lexists(missing_target)
        assert os.path.lexists(tested_entry)
        assert tested_entry.is_symlink()
    assert not paths["sentinel"].exists()
    assert not os.path.lexists(paths["ongoing"] / "live_round")
    assert not os.path.lexists(paths["ongoing"] / ".live_round_payloads")
    assert not os.path.lexists(paths["state"] / "round-table.tsv")
    assert not os.path.lexists(paths["state"] / "png" / "overview.png")
    assert not any(path.is_symlink() for path in paths["ongoing"].rglob("*"))


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
    _make_structured_current_root(paths["current"])
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
# Stage A0 reset barrier and stable lock inode regressions (all state is scratch).

import fcntl
import pathlib
import signal
import stat
import time


CONTROL = (
    ".rtbioscan_state_reset.flock",
    ".dorado.lock.flock",
    ".blastreport.lock.flock",
    ".blastreport_sup.lock.flock",
    ".qced_reads.lock.flock",
    ".otu_size_streak.lock.flock",
    ".sup_basecall_cache.lock.flock",
    ".done_pod5.lock.flock",
    ".report_history.lock.flock",
)


def state_root(outdir):
    state = outdir / "temp/ongoing/state/SID/_state"
    state.mkdir(parents=True, exist_ok=True)
    return state


def env_for(outdir, *, wait="2", mode="reset"):
    return dict(
        os.environ,
        MODE=mode,
        OUTDIR=str(outdir),
        LOCK_WAIT=wait,
        RUN_NAME="stage-a0",
        STATE_ID="SID",
        FORCE="1",
        OPERATION_ID="a" * 64,
    )


def run_reset(outdir, *, wait="2", mode="reset", handler=HANDLER):
    return subprocess.run(
        ["/bin/bash", str(handler)],
        env=env_for(outdir, wait=wait, mode=mode),
        capture_output=True,
        text=True,
        timeout=12,
    )


def inventory(root):
    found = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = pathlib.Path(base) / name
            s = path.lstat()
            found[str(path.relative_to(root))] = (
                stat.S_IFMT(s.st_mode),
                stat.S_IMODE(s.st_mode),
                s.st_uid,
                s.st_ino,
                s.st_mtime_ns,
                os.readlink(path) if path.is_symlink() else
                path.read_bytes() if path.is_file() else None,
            )
    return found


def assert_refusal_delta(before, after):
    assert not (before.keys() - after.keys())
    added = after.keys() - before.keys()
    assert added <= set(CONTROL)
    for path in before:
        old, new = before[path], after[path]
        if path == "." or (old[0] == stat.S_IFDIR and any(
            pathlib.PurePath(name).parent == pathlib.PurePath(path) for name in added
        )):
            assert old[:4] == new[:4]
            assert old[5] == new[5]
        else:
            assert old == new, path
    for path in added:
        kind, mode, owner, _, _, data = after[path]
        assert kind == stat.S_IFREG and mode == 0o600 and owner == os.getuid()
        assert data == b""


def assert_root_refusal_delta(before, after):
    chain = ("temp/ongoing", "temp/ongoing/state",
             "temp/ongoing/state/SID", "temp/ongoing/state/SID/_state")
    control_paths = {f"{chain[-1]}/{name}" for name in CONTROL}
    added = after.keys() - before.keys()
    assert not (before.keys() - after.keys())
    assert added <= set(chain) | control_paths
    for path in before:
        old, new = before[path], after[path]
        if old[0] == stat.S_IFDIR and any(
            pathlib.PurePath(name).parent == pathlib.PurePath(path) for name in added
        ):
            assert old[:4] == new[:4] and old[5] == new[5], path
        else:
            assert old == new, path
    for path in added:
        kind, mode, owner, _, _, data = after[path]
        assert owner == os.getuid()
        if path in chain:
            assert kind == stat.S_IFDIR and mode == 0o700 and data is None
        else:
            assert kind == stat.S_IFREG and mode == 0o600 and data == b""


def make_lock(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_empty_state_and_success_preserve_all_control_inodes(tmp_path):
    state = state_root(tmp_path)
    (state / "scientific.txt").write_bytes(b"original")
    assert run_reset(tmp_path).returncode == 0
    assert not (state / "scientific.txt").exists()
    first = {name: inventory(state)[name] for name in CONTROL}
    assert set(first) == set(CONTROL)
    assert all(value[0] == stat.S_IFREG and value[1] == 0o600 and value[5] == b""
               for value in first.values())
    assert run_reset(tmp_path).returncode == 0
    assert {name: inventory(state)[name] for name in CONTROL} == first


def test_reset_initializes_absent_state_root(tmp_path):
    assert run_reset(tmp_path).returncode == 0
    state = tmp_path / "temp/ongoing/state/SID/_state"
    assert state.is_dir()
    assert {item.name for item in state.iterdir()} == set(CONTROL)


@pytest.mark.parametrize("existing_depth", range(5))
def test_refusal_may_complete_only_missing_control_ancestors(tmp_path, existing_depth):
    temp = tmp_path / "temp"
    temp.mkdir()
    legacy_fence = temp / "current/state/SID/.legacy.lockdir"
    legacy_fence.mkdir(parents=True)
    chain = [temp / "ongoing", temp / "ongoing/state",
             temp / "ongoing/state/SID", temp / "ongoing/state/SID/_state"]
    for item in chain[:existing_depth]:
        item.mkdir()
    before = inventory(tmp_path)
    first = run_reset(tmp_path)
    assert first.returncode != 0 and "lock fence" in first.stderr
    after = inventory(tmp_path)
    assert_root_refusal_delta(before, after)
    assert not (temp / ".restart_applied.SID").exists()
    second = run_reset(tmp_path)
    assert second.returncode != 0
    assert inventory(tmp_path) == after
    assert set(item.name for item in chain[-1].iterdir()) == set(CONTROL)


@pytest.mark.parametrize("unsafe", ["symlink", "file"])
def test_unsafe_partial_ancestor_refuses_without_deeper_creation(tmp_path, unsafe):
    temp = tmp_path / "temp"
    temp.mkdir()
    ongoing = temp / "ongoing"
    if unsafe == "symlink":
        target = tmp_path / "outside"
        target.mkdir()
        ongoing.symlink_to(target, target_is_directory=True)
    elif unsafe == "file":
        ongoing.write_bytes(b"unsafe")
    before = inventory(tmp_path)
    result = run_reset(tmp_path)
    assert result.returncode != 0
    assert inventory(tmp_path) == before
    assert not (temp / ".restart_applied.SID").exists()


@pytest.mark.parametrize("active", CONTROL[1:])
def test_active_lock_refuses_without_applying_and_second_refusal_is_identical(tmp_path, active):
    state = state_root(tmp_path)
    scientific = state / "scientific.txt"
    scientific.write_bytes(b"untouched")
    fd = make_lock(state / active)
    try:
        before = inventory(state)
        first = run_reset(tmp_path)
        after = inventory(state)
        assert first.returncode != 0 and "active governed reset lock" in first.stderr
        assert_refusal_delta(before, after)
        assert after.keys() - before.keys() == set(CONTROL[:CONTROL.index(active)]) - before.keys()
        assert not (tmp_path / "temp/.restart_applied.SID").exists()
        parent_mtime = state.stat().st_mtime_ns
        second = run_reset(tmp_path)
        assert second.returncode != 0
        assert inventory(state) == after
        assert state.stat().st_mtime_ns == parent_mtime
    finally:
        os.close(fd)
    assert run_reset(tmp_path).returncode == 0
    for name in after.keys() & set(CONTROL):
        assert (state / name).stat().st_ino == after[name][3]


@pytest.mark.parametrize("bad", ["barrier-symlink", "barrier-directory", "lock-symlink", "fence-ownerless", "fence-token", "fence-tokenized", "fence-legacy-owner", "unknown-lock"])
def test_unsafe_control_or_fence_refuses_before_applying(tmp_path, bad):
    state = state_root(tmp_path)
    sentinel = state / "scientific.txt"
    sentinel.write_bytes(b"untouched")
    if bad == "barrier-symlink":
        (state / CONTROL[0]).symlink_to(sentinel)
    elif bad == "barrier-directory":
        (state / CONTROL[0]).mkdir()
    elif bad == "lock-symlink":
        (state / CONTROL[1]).symlink_to(sentinel)
    elif bad == "unknown-lock":
        fd = os.open(state / ".unknown.lock.flock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    else:
        fence = state / (".qced_reads.lock.lockdir.reclaim-" + TOKEN
                         if bad == "fence-tokenized" else ".qced_reads.lock.lockdir")
        fence.mkdir()
        if bad == "fence-token":
            (fence / "v2owner").write_text("rtbioscan-fence-v2\t0123456789abcdef\n")
        elif bad == "fence-legacy-owner":
            (fence / "meta.env").write_text("pid=1\nhost=host\n")
    before = inventory(state)
    result = run_reset(tmp_path)
    assert result.returncode != 0
    assert_refusal_delta(before, inventory(state))
    assert not (tmp_path / "temp/.restart_applied.SID").exists()


@pytest.mark.parametrize("partial_count", [1, 2])
def test_partial_control_set_is_completed_without_replacement(tmp_path, partial_count):
    state = state_root(tmp_path)
    for name in CONTROL[:partial_count]:
        fd = os.open(state / name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    before = {name: (state / name).stat().st_ino for name in CONTROL[:partial_count]}
    assert run_reset(tmp_path).returncode == 0
    assert {name: (state / name).stat().st_ino for name in CONTROL[:partial_count]} == before
    assert all((state / name).exists() for name in CONTROL)


def test_host_record_and_inactive_stable_files_survive_success(tmp_path):
    state = state_root(tmp_path)
    host = state / ".rtbioscan_lock_host_v1"
    host.write_bytes(b"rtbioscan-lock-host-v1\t686f7374\n")
    before_host = inventory(state)[host.name]
    before_lock = {}
    for name in CONTROL[1:]:
        fd = os.open(state / name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        before_lock[name] = inventory(state)[name]
    assert run_reset(tmp_path).returncode == 0
    after = inventory(state)
    assert after[host.name] == before_host
    assert {name: after[name] for name in before_lock} == before_lock


def test_several_preexisting_stable_locks_with_late_active_holder(tmp_path):
    state = state_root(tmp_path)
    for name in CONTROL:
        fd = os.open(state / name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
    (state / "scientific.txt").write_bytes(b"unchanged")
    active = os.open(state / CONTROL[-1], os.O_RDONLY)
    fcntl.flock(active, fcntl.LOCK_EX)
    try:
        before = inventory(state)
        parent_mtime = state.stat().st_mtime_ns
        result = run_reset(tmp_path)
        assert result.returncode != 0 and "active governed reset lock" in result.stderr
        assert inventory(state) == before
        assert state.stat().st_mtime_ns == parent_mtime
        assert not (tmp_path / "temp/.restart_applied.SID").exists()
    finally:
        os.close(active)
    assert run_reset(tmp_path).returncode == 0
    assert all((state / name).stat().st_ino == before[name][3] for name in CONTROL)


def test_shared_holder_blocks_reset_then_same_inode_is_used(tmp_path):
    state = state_root(tmp_path)
    assert run_reset(tmp_path).returncode == 0
    barrier = state / CONTROL[0]
    fd = os.open(barrier, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_SH)
    before = inventory(state)
    try:
        result = run_reset(tmp_path, wait="0")
        assert result.returncode != 0 and "exclusive reset barrier" in result.stderr
        assert inventory(state) == before
    finally:
        os.close(fd)
    assert run_reset(tmp_path).returncode == 0
    assert barrier.stat().st_ino == before[barrier.name][3]


def test_eight_simultaneous_initializers_converge(tmp_path):
    state = state_root(tmp_path)
    env = env_for(tmp_path, wait="8")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    entry_dir = tmp_path / "in-reset"
    overlap = tmp_path / "overlap"
    shim = shim_dir / "rm"
    shim.write_text("#!/bin/sh\n"
                    'if ! mkdir "$RTB_RESET_ENTRY_DIR" 2>/dev/null; then\n'
                    '  : > "$RTB_RESET_OVERLAP"\n'
                    'else\n'
                    '  sleep 0.05\n'
                    '  rmdir "$RTB_RESET_ENTRY_DIR"\n'
                    'fi\n'
                    'exec /bin/rm "$@"\n')
    shim.chmod(0o755)
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    env["RTB_RESET_ENTRY_DIR"] = str(entry_dir)
    env["RTB_RESET_OVERLAP"] = str(overlap)
    jobs = [subprocess.Popen(["/bin/bash", str(HANDLER)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(8)]
    for job in jobs:
        _, err = job.communicate(timeout=15)
        assert job.returncode == 0, err
    assert not overlap.exists(), "two resets entered destructive work together"
    first = inventory(state)
    assert set(CONTROL) <= first.keys()
    assert run_reset(tmp_path).returncode == 0
    assert all(inventory(state)[name] == first[name] for name in CONTROL)


def test_space_and_non_ascii_state_path(tmp_path):
    outdir = tmp_path / "espaço and spaces"
    state = state_root(outdir)
    fd = make_lock(state / CONTROL[4])
    try:
        assert run_reset(outdir).returncode != 0
    finally:
        os.close(fd)
    assert run_reset(outdir).returncode == 0


@pytest.mark.parametrize("bad", ["directory", "symlink", "hardlink"])
def test_malformed_partial_control_file_is_never_replaced(tmp_path, bad):
    state = state_root(tmp_path)
    barrier = state / CONTROL[0]
    fd = os.open(barrier, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    target = state / CONTROL[1]
    if bad == "directory":
        target.mkdir()
    elif bad == "symlink":
        target.symlink_to(barrier)
    elif bad == "hardlink":
        other = state / "hardlink-target"
        other.write_bytes(b"informational")
        os.link(other, target)
    before = inventory(state)
    result = run_reset(tmp_path)
    assert result.returncode != 0
    assert_refusal_delta(before, inventory(state))
    assert barrier.stat().st_ino == before[barrier.name][3]
    assert not (tmp_path / "temp/.restart_applied.SID").exists()


def test_reset_exclusive_blocks_shared_writer_and_transition_holder(tmp_path):
    state = state_root(tmp_path)
    (state / "scientific.txt").write_bytes(b"wipe me")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    marker = tmp_path / "reset-entered"
    shim = shim_dir / "rm"
    shim.write_text("#!/bin/sh\n"
                    'if [ ! -e "$RTB_RESET_TEST_MARKER" ]; then\n'
                    '  : > "$RTB_RESET_TEST_MARKER"\n'
                    '  sleep 1\n'
                    'fi\n'
                    'exec /bin/rm "$@"\n')
    shim.chmod(0o755)
    env = env_for(tmp_path)
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    env["RTB_RESET_TEST_MARKER"] = str(marker)
    reset = subprocess.Popen(["/bin/bash", str(HANDLER)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), reset.poll()
        inode = (state / CONTROL[4]).stat().st_ino
        shared_done = tmp_path / "shared-entered"
        transition_done = tmp_path / "transition-entered"
        shared_acquire = ("" if os.environ.get("RTB_A0_MUTANT_WRITER_NO_SHARED") == "1"
                          else "fcntl.flock(f, fcntl.LOCK_SH); ")
        shared_script = (
            "import fcntl, pathlib, sys; "
            "f=open(sys.argv[1], 'rb'); " + shared_acquire +
            "pathlib.Path(sys.argv[2]).write_text('entered')"
        )
        transition_script = (
            "import fcntl, pathlib, sys; "
            "f=open(sys.argv[1], 'rb'); fcntl.flock(f, fcntl.LOCK_EX); "
            "pathlib.Path(sys.argv[2]).write_text('entered')"
        )
        shared = subprocess.Popen(["python3", "-c", shared_script,
                                   str(state / CONTROL[0]), str(shared_done)])
        transition = subprocess.Popen(["python3", "-c", transition_script,
                                       str(state / CONTROL[4]), str(transition_done)])
        try:
            time.sleep(0.2)
            assert not shared_done.exists() and not transition_done.exists()
            _, err = reset.communicate(timeout=10)
            assert reset.returncode == 0, err
            assert shared.wait(timeout=5) == 0
            assert transition.wait(timeout=5) == 0
            assert shared_done.exists() and transition_done.exists()
            assert (state / CONTROL[4]).stat().st_ino == inode
            assert not (state / "scientific.txt").exists()
        finally:
            for proc in (shared, transition):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=3)
    finally:
        if reset.poll() is None:
            reset.kill()
            reset.wait(timeout=3)


def test_child_inherits_transition_lock_after_parent_exits(tmp_path):
    state = state_root(tmp_path)
    path = state / CONTROL[4]
    ready = tmp_path / "child-pid"
    script = (
        "import fcntl, os, pathlib, sys, time; "
        "fd=os.open(sys.argv[1], os.O_RDWR|os.O_CREAT|os.O_EXCL, 0o600); "
        "fcntl.flock(fd, fcntl.LOCK_EX); pid=os.fork(); "
        "time.sleep(8) if pid == 0 else pathlib.Path(sys.argv[2]).write_text(str(pid))"
    )
    parent = subprocess.Popen(["python3", "-c", script, str(path), str(ready)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child_pid = int(ready.read_text())
        assert parent.wait(timeout=3) == 0
        before = inventory(state)
        refused = run_reset(tmp_path)
        assert refused.returncode != 0 and "active governed reset lock" in refused.stderr
        assert_refusal_delta(before, inventory(state))
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=3)


@pytest.mark.parametrize("signal_number", [15, 9])
def test_task_shell_signal_keeps_child_transition_lock(tmp_path, signal_number):
    state = state_root(tmp_path)
    target = state / CONTROL[4]
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    child_pid_file = tmp_path / "child-pid"
    ready = tmp_path / "ready"
    script = r'''set -e
exec 9< "$1"
perl -MFcntl=:flock -e 'open(my $f, "<&9") or die; flock($f, LOCK_EX) or die'
sleep 8 &
printf '%s\n' "$!" > "$2"
: > "$3"
wait
'''
    shell = subprocess.Popen(["/bin/bash", "-c", script, "_", str(target),
                              str(child_pid_file), str(ready)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    child_pid = None
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child_pid = int(child_pid_file.read_text())
        os.kill(shell.pid, signal_number)
        shell.wait(timeout=3)
        before = inventory(state)
        refused = run_reset(tmp_path)
        assert refused.returncode != 0 and "active governed reset lock" in refused.stderr
        assert_refusal_delta(before, inventory(state))
    finally:
        if child_pid is not None:
            try:
                os.kill(child_pid, 9)
            except ProcessLookupError:
                pass
        if shell.poll() is None:
            shell.kill()
            shell.wait(timeout=3)
    assert run_reset(tmp_path).returncode == 0


@pytest.mark.parametrize("late", ["active", "symlink", "barrier-replaced"])
def test_control_path_created_or_replaced_while_reset_waits(tmp_path, late):
    state = state_root(tmp_path)
    scientific = state / "scientific.txt"
    scientific.write_bytes(b"must survive")
    barrier = state / CONTROL[0]
    fd = os.open(barrier, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    reset = subprocess.Popen(["/bin/bash", str(HANDLER)], env=env_for(tmp_path),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    late_fd = None
    try:
        barrier_ready = state / CONTROL[0]
        deadline = time.monotonic() + 5
        while not barrier_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert barrier_ready.exists()
        time.sleep(0.1)
        if late == "active":
            late_fd = make_lock(state / CONTROL[4])
        elif late == "symlink":
            (state / CONTROL[1]).symlink_to(scientific)
        else:
            barrier.rename(state / "old-barrier")
            new_fd = os.open(barrier, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(new_fd)
        os.close(fd)
        fd = None
        _, err = reset.communicate(timeout=8)
        assert reset.returncode != 0, err
        assert scientific.read_bytes() == b"must survive"
        assert not (tmp_path / "temp/.restart_applied.SID").exists()
        if late == "active":
            assert b"active governed reset lock" in err
        elif late == "symlink":
            assert b"unsafe reset control" in err
        else:
            assert b"reset control inode changed" in err
    finally:
        if fd is not None:
            os.close(fd)
        if late_fd is not None:
            os.close(late_fd)
        if reset.poll() is None:
            reset.kill()
            reset.wait(timeout=3)


def test_unwritable_state_root_refuses_before_applying(tmp_path):
    state = state_root(tmp_path)
    scientific = state / "scientific.txt"
    scientific.write_bytes(b"untouched")
    state.chmod(0o500)
    try:
        result = run_reset(tmp_path)
        assert result.returncode != 0
        assert not (tmp_path / "temp/.restart_applied.SID").exists()
        assert scientific.read_bytes() == b"untouched"
    finally:
        state.chmod(0o700)


def test_barrier_symlink_to_valid_file_is_refused(tmp_path):
    state = state_root(tmp_path)
    target = state / "valid-target"
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    barrier = state / CONTROL[0]
    barrier.symlink_to(target)
    before = inventory(state)
    result = run_reset(tmp_path)
    assert result.returncode != 0
    assert_refusal_delta(before, inventory(state))
    assert not (tmp_path / "temp/.restart_applied.SID").exists()


def test_whole_holder_process_group_sigkill_releases_stable_inode(tmp_path):
    state = state_root(tmp_path)
    target = state / CONTROL[4]
    fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    ready = tmp_path / "ready"
    script = r'''set -e
exec 9< "$1"
perl -MFcntl=:flock -e 'open(my $f, "<&9") or die; flock($f, LOCK_EX) or die'
sleep 8 &
: > "$2"
wait
'''
    shell = subprocess.Popen(["/bin/bash", "-c", script, "_", str(target), str(ready)],
                             start_new_session=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        inode = target.stat().st_ino
        assert run_reset(tmp_path).returncode != 0
        os.killpg(shell.pid, signal.SIGKILL)
        shell.wait(timeout=3)
        assert run_reset(tmp_path).returncode == 0
        assert target.stat().st_ino == inode
    finally:
        if shell.poll() is None:
            os.killpg(shell.pid, signal.SIGKILL)
            shell.wait(timeout=3)


# P2 correction: advisory bytes and effective filesystem modes are preserved.
STAGE_A_RECORD = (
    b"v=2 host=stage-a0.local pid=93530 ppid=93530 started=1790457301 "
    b"token=9ba2210f7001e3dc label=.qced_reads.lock boot=-\n"
)


@pytest.mark.parametrize("record", [
    b"", b"owner=previous-holder\n", STAGE_A_RECORD,
    b"arbitrary informational bytes\x00" * 8192,
])
def test_nonempty_stable_lock_record_survives_success(tmp_path, record):
    state = state_root(tmp_path)
    target = state / CONTROL[4]
    target.write_bytes(record)
    target.chmod(0o666)
    before = inventory(state)[target.name]
    assert run_reset(tmp_path).returncode == 0
    assert inventory(state)[target.name] == before


def test_actual_paused_stage_a_owner_record_survives_success(tmp_path):
    path = os.environ.get("RTBIOSCAN_PAUSED_STAGE_A_RECORD")
    record = pathlib.Path(path).read_bytes() if path else STAGE_A_RECORD
    assert record.startswith(b"v=2 host=") and b" label=.qced_reads.lock boot=-\n" in record
    assert 100 <= len(record) <= 200
    state = state_root(tmp_path)
    target = state / CONTROL[4]
    target.write_bytes(record)
    before = inventory(state)[target.name]
    assert run_reset(tmp_path).returncode == 0
    assert inventory(state)[target.name] == before


def test_all_seven_nonempty_lock_records_survive_success_and_refusal(tmp_path):
    state = state_root(tmp_path)
    for index, name in enumerate(CONTROL[1:]):
        target = state / name
        target.write_bytes((b"owner record %d\n" % index) * (index + 1))
        target.chmod(0o664)
    before = {name: inventory(state)[name] for name in CONTROL[1:]}
    active = os.open(state / CONTROL[-1], os.O_RDONLY)
    fcntl.flock(active, fcntl.LOCK_EX)
    try:
        refused = run_reset(tmp_path)
        assert refused.returncode != 0 and "active governed reset lock" in refused.stderr
        assert {name: inventory(state)[name] for name in before} == before
        assert not (tmp_path / "temp/.restart_applied.SID").exists()
    finally:
        os.close(active)
    assert run_reset(tmp_path).returncode == 0
    assert {name: inventory(state)[name] for name in before} == before


def test_preexisting_group_writable_directories_and_control_modes_survive(tmp_path):
    state = state_root(tmp_path)
    for directory in (tmp_path, tmp_path / "temp", tmp_path / "temp/ongoing", state):
        directory.chmod(0o775)
    for name in CONTROL:
        target = state / name
        target.write_bytes(b"" if name == CONTROL[0] else b"record\n")
        target.chmod(0o666)
    before_dirs = {str(path): (path.stat().st_ino, stat.S_IMODE(path.stat().st_mode))
                   for path in (tmp_path, tmp_path / "temp", tmp_path / "temp/ongoing", state)}
    before_files = {name: inventory(state)[name] for name in CONTROL}
    assert run_reset(tmp_path).returncode == 0
    assert {str(path): (path.stat().st_ino, stat.S_IMODE(path.stat().st_mode))
            for path in (tmp_path, tmp_path / "temp", tmp_path / "temp/ongoing", state)} == before_dirs
    assert {name: inventory(state)[name] for name in CONTROL} == before_files


def test_missing_output_root_is_explicitly_refused_without_creation(tmp_path):
    missing = tmp_path / "missing-output"
    result = run_reset(missing)
    assert result.returncode != 0
    assert "output root does not exist; reset will not create it" in result.stderr
    assert not missing.exists()


def test_empty_output_root_is_explicitly_refused_for_reset(tmp_path):
    env = env_for(tmp_path)
    env["OUTDIR"] = ""
    result = subprocess.run(["/bin/bash", str(HANDLER)], env=env,
                            capture_output=True, text=True, timeout=12)
    assert result.returncode != 0
    assert "output root does not exist; reset will not create it" in result.stderr
    assert not (tmp_path / "temp").exists()


@pytest.mark.parametrize("fences", [
    (".report_history.lock.lockdir",),
    (".qced_reads.lock.lockdir",),
    (".qced_reads.lock.lockdir.reclaim-" + TOKEN,),
    (".report_history.lock.lockdir", ".qced_reads.lock.lockdir"),
])
def test_existing_compatibility_fences_refuse_and_preserve(tmp_path, fences):
    state = state_root(tmp_path)
    for name in fences:
        fence = state / name
        fence.mkdir()
        (fence / "owner").write_bytes(b"existing owner\n")
    before = inventory(state)
    result = run_reset(tmp_path)
    assert result.returncode != 0
    assert any(str(state / name) in result.stderr for name in fences)
    assert_refusal_delta(before, inventory(state))
    assert not (tmp_path / "temp/.restart_applied.SID").exists()


def test_migration_audit_survives_success_and_refusal(tmp_path):
    state = state_root(tmp_path)
    audit = state / ".lock_migration.log"
    audit.write_bytes(b"adoption audit\nrebind intent\n")
    audit.chmod(0o640)
    unrelated = state / ".lookalike.log"
    unrelated.write_bytes(b"delete me")
    before = inventory(state)[audit.name]
    fence = state / ".qced_reads.lock.lockdir"
    fence.mkdir()
    refused = run_reset(tmp_path)
    assert refused.returncode != 0
    assert inventory(state)[audit.name] == before
    fence.rmdir()
    assert run_reset(tmp_path).returncode == 0
    assert inventory(state)[audit.name] == before
    assert not unrelated.exists()


def test_fixed_future_lock_inventory_matches_all_current_names():
    source = HANDLER.read_text()
    for name in CONTROL[1:]:
        assert source.count(name) == 3, name
    assert "Any future A2 or Stage B stable kernel-lock name" in source


@pytest.mark.parametrize("relative", [False, True])
def test_symlinked_output_root_resolves_once_and_preserves_user_link(tmp_path, relative):
    real = tmp_path / "réal output with spaces"
    real.mkdir()
    link = tmp_path / "résults link"
    destination = os.path.relpath(real, link.parent) if relative else str(real)
    link.symlink_to(destination, target_is_directory=True)
    original = (link.lstat().st_ino, os.readlink(link))
    state = state_root(real)
    scientific = state / "scientific.txt"
    scientific.write_bytes(b"remove on target")
    assert run_reset(link).returncode == 0
    assert (link.lstat().st_ino, os.readlink(link)) == original
    assert not scientific.exists()
    assert (real / "temp/.restart_applied.SID").exists()
    assert {name for name in CONTROL} <= {item.name for item in state.iterdir()}


def test_symlinked_output_root_retargeted_before_barrier_refuses(tmp_path):
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    link = tmp_path / "results"
    link.symlink_to(original, target_is_directory=True)
    state = state_root(original)
    scientific = state / "scientific.txt"
    scientific.write_bytes(b"keep")
    barrier = state / CONTROL[0]
    barrier.write_bytes(b"")
    holder = os.open(barrier, os.O_RDONLY)
    fcntl.flock(holder, fcntl.LOCK_SH)
    reset = subprocess.Popen(["/bin/bash", str(HANDLER)], env=env_for(link, wait="4"),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(0.2)
        assert reset.poll() is None
        link.unlink()
        link.symlink_to(replacement, target_is_directory=True)
        os.close(holder)
        holder = None
        _, err = reset.communicate(timeout=7)
        assert reset.returncode != 0 and b"output root identity changed" in err
        assert scientific.read_bytes() == b"keep"
        assert not (original / "temp/.restart_applied.SID").exists()
        assert not (replacement / "temp").exists()
    finally:
        if holder is not None:
            os.close(holder)
        if reset.poll() is None:
            reset.kill()
            reset.wait(timeout=3)


@pytest.mark.parametrize("change", ["retarget", "replace-target", "suffix-symlink"])
def test_output_root_identity_rechecked_immediately_before_applying(tmp_path, change):
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    original.mkdir()
    replacement.mkdir()
    link = tmp_path / "results"
    link.symlink_to(original, target_is_directory=True)
    state = state_root(original)
    scientific = state / "scientific.txt"
    scientific.write_bytes(b"keep")
    marker = tmp_path / "at-final-check"
    release = tmp_path / "release-final-check"
    source = HANDLER.read_text()
    anchor = 'if [ "$MODE" = "reset" ]; then\n    verify_reset_root || exit 1\nfi\nwrite_restart_sentinel applying'
    assert source.count(anchor) == 1
    injected = (
        'if [ "$MODE" = "reset" ]; then\n'
        '    : > "$RTB_TEST_FINAL_MARKER"\n'
        '    while [ ! -e "$RTB_TEST_FINAL_RELEASE" ]; do sleep 0.02; done\n'
        '    verify_reset_root || exit 1\n'
        'fi\nwrite_restart_sentinel applying'
    )
    handler = tmp_path / "instrumented-restart.sh"
    handler.write_text(source.replace(anchor, injected, 1))
    env = env_for(link, wait="4")
    env["RTB_TEST_FINAL_MARKER"] = str(marker)
    env["RTB_TEST_FINAL_RELEASE"] = str(release)
    reset = subprocess.Popen(["/bin/bash", str(handler)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    moved = tmp_path / "moved-original"
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), reset.poll()
        if change == "retarget":
            link.unlink()
            link.symlink_to(replacement, target_is_directory=True)
        elif change == "replace-target":
            original.rename(moved)
            original.mkdir()
        else:
            moved_state = tmp_path / "moved-state"
            state.rename(moved_state)
            state.symlink_to(moved_state, target_is_directory=True)
        release.write_bytes(b"")
        _, err = reset.communicate(timeout=7)
        expected = (b"reset suffix inode changed" if change == "suffix-symlink"
                    else b"output root identity changed")
        assert reset.returncode != 0 and expected in err
        preserved_scientific = (moved / "temp/ongoing/state/SID/_state/scientific.txt"
                                if change == "replace-target" else scientific)
        assert preserved_scientific.read_bytes() == b"keep"
        assert not (moved / "temp/.restart_applied.SID").exists()
        assert not (original / "temp/.restart_applied.SID").exists()
        assert not (replacement / "temp/.restart_applied.SID").exists()
    finally:
        release.write_bytes(b"")
        if reset.poll() is None:
            reset.kill()
            reset.wait(timeout=3)


def test_symlinked_output_root_does_not_allow_nested_suffix_symlink(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "results"
    link.symlink_to(real, target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    temp = real / "temp"
    temp.mkdir()
    (temp / "ongoing").symlink_to(outside, target_is_directory=True)
    before = inventory(real)
    result = run_reset(link)
    assert result.returncode != 0
    assert inventory(real) == before
    assert not (outside / "state").exists()


@pytest.mark.parametrize("synthetic", ["noncurrent-uid", "nonposix-mode"])
def test_reset_accepts_synthetic_external_drive_metadata(tmp_path, synthetic):
    # Model a filesystem reporting synthesized ownership or mode through
    # Perl lstat. The real scratch files stay on the host filesystem.
    outdir = tmp_path / "external-drive-model"
    outdir.mkdir()
    module_dir = tmp_path / "perl-fixture"
    module_dir.mkdir()
    (module_dir / "FakeMetadata.pm").write_text(r'''package FakeMetadata;
use strict;
use warnings;
BEGIN {
    *CORE::GLOBAL::lstat = sub {
        my @s = CORE::lstat($_[0]);
        if (@s && index($_[0], $ENV{RTB_FAKE_ROOT}) == 0) {
            $s[4] = $< + 1 if $ENV{RTB_FAKE_UID};
            if ($ENV{RTB_FAKE_MODE}) {
                $s[2] = ($s[2] & 0170000) |
                    (($s[2] & 0170000) == 0040000 ? 0775 : 0666);
            }
        }
        return @s;
    };
}
1;
''')
    env = env_for(outdir)
    env["PERL5LIB"] = str(module_dir)
    env["PERL5OPT"] = "-MFakeMetadata"
    env["RTB_FAKE_ROOT"] = str(outdir)
    env["RTB_FAKE_UID"] = "1" if synthetic == "noncurrent-uid" else ""
    env["RTB_FAKE_MODE"] = "1" if synthetic == "nonposix-mode" else ""
    first = subprocess.run(["/bin/bash", str(HANDLER)], env=env,
                           capture_output=True, text=True, timeout=12)
    assert first.returncode == 0, first.stderr
    state = outdir / "temp/ongoing/state/SID/_state"
    before = {name: inventory(state)[name] for name in CONTROL}
    second = subprocess.run(["/bin/bash", str(HANDLER)], env=env,
                            capture_output=True, text=True, timeout=12)
    assert second.returncode == 0, second.stderr
    assert {name: inventory(state)[name] for name in CONTROL} == before


def test_lock_replacement_after_validation_refuses_before_applying(tmp_path):
    state = state_root(tmp_path)
    target = state / CONTROL[4]
    target.write_bytes(b"record before replacement\n")
    scientific = state / "scientific.txt"
    scientific.write_bytes(b"keep")
    source = HANDLER.read_text()
    anchor = '    sysopen($fh, $path, O_RDONLY) or die "ERROR: cannot open reset control $path: $!\\n"'
    assert source.count(anchor) == 1
    injected = (
        '    if ($path =~ /[.]qced_reads[.]lock[.]flock$/) {\n'
        '        rename($path, "$path.old") or die;\n'
        '        sysopen(my $replacement, $path, O_RDWR | O_CREAT | O_EXCL, 0600) or die;\n'
        '        close $replacement;\n'
        '    }\n' + anchor
    )
    handler = tmp_path / "replace-between-checks.sh"
    handler.write_text(source.replace(anchor, injected, 1))
    result = run_reset(tmp_path, handler=handler)
    assert result.returncode != 0 and "reset control inode changed" in result.stderr
    assert scientific.read_bytes() == b"keep"
    assert not (tmp_path / "temp/.restart_applied.SID").exists()
