from __future__ import annotations

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


def _paths(outdir: Path, state_id: str = "state-a") -> dict[str, Path]:
    return {
        "ongoing": outdir / "temp" / "ongoing" / "state" / state_id,
        "state": outdir / "temp" / "ongoing" / "state" / state_id / "_state",
        "legacy": outdir / "temp" / "current" / "state" / state_id,
        "current": outdir / "current" / "state" / state_id,
        "sentinel": outdir / "temp" / f".restart_applied.{state_id}",
    }


def _run(mode: str, outdir: Path, state_id: str = "state-a") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "LC_ALL": "C",
            "MODE": mode,
            "OUTDIR": str(outdir),
            "LOCK_WAIT": "2",
            "RUN_NAME": "restart-cutover-test",
            "STATE_ID": state_id,
            "FORCE": "1",
        }
    )
    return subprocess.run(
        ["/bin/bash", str(HANDLER)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
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
    assert paths["sentinel"].read_text(encoding="utf-8") == (
        "mode=reset\nrun_name=restart-cutover-test\n"
    )


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
    assert paths["sentinel"].read_text(encoding="utf-8") == (
        "mode=restore\nrun_name=restart-cutover-test\n"
    )


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
    assert paths["sentinel"].read_text(encoding="utf-8") == (
        "mode=restore\nrun_name=restart-cutover-test\n"
    )


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
