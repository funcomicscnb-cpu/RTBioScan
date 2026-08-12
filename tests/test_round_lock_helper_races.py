"""Concurrency regressions for the round-lock generation helper.

Two defects are covered here.

Events directory: ``ensure_real_directory`` performed ``lstat`` then ``mkdir``
and treated ``EEXIST`` as fatal, so two concurrent first acquisitions against a
fresh state directory could both observe ``ENOENT`` and one would then die.

Ready pins: ``blocking_pins`` enumerated pin records with ``readdir`` and then
read each one. A concurrent legitimate unpin between those steps made
``read_ready_pin`` return ``undef``, and ``pin_is_blocking`` read an undefined
host, compared it against this host, and classified the vanished pin as a
foreign-host pin -- blocking release indefinitely.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
EVENTS_DIR_NAME = ".round_lock_events"


def _acquire_argv(state: Path, round_barcode: str = "run_1") -> list[str]:
    return [
        "perl",
        str(SCRIPT),
        "acquire",
        "--state-dir",
        str(state),
        "--round-barcode",
        round_barcode,
        "--scope",
        "full_round",
        "--owner-pid",
        str(os.getpid()),
        "--stale-seconds",
        "30",
        "--wait-seconds",
        "5",
    ]


def _acquire(state: Path, round_barcode: str = "run_1") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _acquire_argv(state, round_barcode),
        capture_output=True,
        text=True,
        check=False,
    )


def test_concurrent_first_acquisition_never_fails_on_the_events_directory(
    tmp_path: Path,
) -> None:
    """Racing first acquisitions must not die creating the shared events dir.

    Exactly one contender may win the round lock; the rest must fail for lock
    contention alone, never because the events directory already existed.

    This is a real regression test: against the unfixed helper it failed on
    3 of 3 runs. It needs tightly-spaced process starts to hit the window --
    shell background jobs spawn too slowly to reproduce it, while
    ``subprocess.Popen`` in a loop does so consistently.
    """
    attempts = 0
    for index in range(8):
        state = tmp_path / f"state-{index}"
        state.mkdir()
        procs = [
            subprocess.Popen(
                _acquire_argv(state, f"run_{index}"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        for proc in procs:
            _, stderr = proc.communicate()
            attempts += 1
            assert "cannot create directory" not in stderr, stderr
            assert f"{EVENTS_DIR_NAME}: File exists" not in stderr, stderr
        assert (state / EVENTS_DIR_NAME).is_dir()
    assert attempts == 32


def test_events_directory_rejects_a_symlink(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    real = tmp_path / "elsewhere"
    real.mkdir()
    (state / EVENTS_DIR_NAME).symlink_to(real, target_is_directory=True)

    result = _acquire(state)

    assert result.returncode != 0
    assert "expected a real directory, not a symlink" in result.stderr


def test_events_directory_rejects_a_regular_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / EVENTS_DIR_NAME).write_text("not a directory\n", encoding="utf-8")

    result = _acquire(state)

    assert result.returncode != 0
    assert "expected a real directory, not a symlink" in result.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_non_eexist_directory_failures_are_still_reported(tmp_path: Path) -> None:
    """EEXIST tolerance must not swallow genuine creation failures."""
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o555)
    try:
        result = _acquire(state)
    finally:
        state.chmod(0o755)

    assert result.returncode != 0
    assert "cannot create directory" in result.stderr
    assert EVENTS_DIR_NAME in result.stderr


def test_abort_with_a_valid_pin_releases_the_lock(tmp_path: Path) -> None:
    """Release must actually release: rc 0, lock gone, next acquisition free.

    NOTE ON COVERAGE: this does *not* reproduce the ready-pin enumeration race
    that motivated the ``blocking_pins`` fix. A test that deletes the pin
    before calling ``abort`` cannot reach ``blocking_pins`` at all, because
    ``release_generation`` authenticates that same pin through ``guard_pin``
    first and ``abort`` is best-effort, so the call returns 0 having done
    nothing. Reproducing the real interleaving -- the record present at
    ``readdir`` and absent at the subsequent read -- needs fault injection the
    helper does not expose.

    The protection for that path is ``next if !defined($pin)`` in
    ``blocking_pins``: that is what handles a benign concurrent unpin. The
    ``die`` in ``pin_is_blocking`` is only a backstop against a future caller
    passing an undefined record; it never fires for the disappearance case,
    because ``blocking_pins`` skips first. The two are not interchangeable --
    removing the ``next`` in the belief that the die guard covers it would turn
    a benign unpin into a crash.

    What this test does cover is the release contract itself, which the earlier
    version of this test did not: it would have passed even with the lock left
    in place.
    """
    state = tmp_path / "state"
    state.mkdir()

    acquire = _acquire(state, "run_release")
    assert acquire.returncode == 0, acquire.stderr
    tokens = dict(
        line.split("=", 1)
        for line in acquire.stdout.strip().splitlines()
        if "=" in line
    )

    lock_dir = state / ".round_inflight.lockdir"
    assert lock_dir.is_dir(), f"expected round lock at {lock_dir}"

    result = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "abort",
            "--state-dir",
            str(state),
            "--round-barcode",
            "run_release",
            "--scope",
            "full_round",
            "--token",
            tokens["generation_token"],
            "--pin-token",
            tokens["pin_token"],
            "--owner-pid",
            str(os.getpid()),
            "--stale-seconds",
            "30",
            "--wait-seconds",
            "5",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Use of uninitialized value" not in result.stderr, result.stderr
    assert not lock_dir.exists(), "abort returned 0 but left the round lock in place"

    # The generation is genuinely released, not merely reported as released.
    again = _acquire(state, "run_release_2")
    assert again.returncode == 0, again.stderr


def _sub_body(name: str) -> str:
    """Return the source of a top-level Perl sub, brace-to-brace at column 0."""
    source = SCRIPT.read_text(encoding="utf-8")
    marker = f"\nsub {name} {{\n"
    start = source.index(marker) + len(marker)
    end = source.index("\n}\n", start)
    return source[start:end]


def test_both_directory_creation_outcomes_sync_the_parent() -> None:
    """Durability guard for ``ensure_real_directory``.

    Two outcomes create the directory as far as this caller is concerned: this
    process won the ``mkdir``, or it lost the race to a concurrent first
    acquisition. Both must fsync the parent, because observing the entry does
    not prove it is durable -- the winner may have died between its ``mkdir``
    and its own sync.

    This is asserted structurally. Power loss is not reproducible in-suite, and
    the concurrency test above cannot cover it: that test asserts only that no
    EEXIST error surfaced, which stays true if the loser-side sync is deleted.
    """
    body = _sub_body("ensure_real_directory")
    sync_call = "sync_directory(dirname($path));"

    assert body.count(sync_call) == 2, (
        "expected exactly two parent syncs in ensure_real_directory: one for "
        "the winning mkdir and one for the lost creation race"
    )

    # The pre-existing-directory path must NOT sync: it created nothing.
    before_mkdir, after_mkdir = body.split("if (mkdir($path, $mode)) {", 1)
    assert sync_call not in before_mkdir

    # The lost-race branch specifically must sync after revalidating the target.
    lost_race_branch = after_mkdir.split("lost_creation_race", 1)[1]
    assert sync_call in lost_race_branch, (
        "the EEXIST path returns without syncing the parent directory"
    )


def _helper(action: str, state: Path, **opts: str) -> subprocess.CompletedProcess[str]:
    argv = ["perl", str(SCRIPT), action, "--state-dir", str(state)]
    for key, value in opts.items():
        argv += [f"--{key.replace('_', '-')}", value]
    argv += ["--owner-pid", str(os.getpid()), "--stale-seconds", "30", "--wait-seconds", "5"]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _handed_off_generation(state: Path) -> tuple[str, str, str]:
    """Acquire, hand off, and return (token, finisher_pin, worker_pin)."""
    acquire = _acquire(state, "run_toctou")
    assert acquire.returncode == 0, acquire.stderr
    tokens = dict(
        line.split("=", 1)
        for line in acquire.stdout.strip().splitlines()
        if "=" in line
    )
    token = tokens["generation_token"]

    handoff = _helper(
        "handoff", state, token=token, round_barcode="run_toctou",
        scope="full_round", pin_token=tokens["pin_token"],
    )
    assert handoff.returncode == 0, handoff.stderr

    pins = {}
    for role in ("backup_update_and_clean", "state_writer"):
        result = _helper(
            "pin", state, token=token, round_barcode="run_toctou",
            scope="full_round", role=role,
        )
        assert result.returncode == 0, result.stderr
        pins[role] = result.stdout.strip()
    return token, pins["backup_update_and_clean"], pins["state_writer"]


def _finish(state: Path, token: str, pin: str, failpoint: str | None = None):
    argv = [
        "perl", str(SCRIPT), "finish", "--state-dir", str(state),
        "--token", token, "--round-barcode", "run_toctou",
        "--scope", "full_round", "--pin-token", pin,
        "--owner-pid", str(os.getpid()),
        "--stale-seconds", "30", "--wait-seconds", "5",
    ]
    env = dict(os.environ)
    if failpoint:
        env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = failpoint
    return subprocess.run(argv, capture_output=True, text=True, check=False, env=env)


def test_live_worker_pin_blocks_release(tmp_path: Path) -> None:
    """Control: a genuinely live worker pin must block release."""
    state = tmp_path / "state"
    state.mkdir()
    token, finisher, _worker = _handed_off_generation(state)

    result = _finish(state, token, finisher)

    assert result.returncode != 0
    assert "blocked by another live generation pin" in result.stderr


def test_worker_pin_removed_between_lstat_and_open_does_not_block_release(
    tmp_path: Path,
) -> None:
    """Deterministic TOCTOU: a concurrently unpinned worker must be skipped.

    The failpoint removes the worker's record between ``parse_record``'s
    ``lstat`` and its ``open``, which is the exact interleaving a legitimate
    concurrent unpin produces. Without the ENOENT-on-open handling this dies
    with ``cannot read '...': No such file or directory`` and release fails
    spuriously; verified against a build carrying the failpoint but not the
    fix.
    """
    state = tmp_path / "state"
    state.mkdir()
    token, finisher, worker = _handed_off_generation(state)

    result = _finish(
        state, token, finisher,
        failpoint=f"unlink-ready-pin-before-open:{worker}",
    )

    assert result.returncode == 0, result.stderr
    assert "cannot read" not in result.stderr
    assert "Use of uninitialized value" not in result.stderr
    assert "blocked by another live generation pin" not in result.stderr


def test_missing_authorization_pin_fails_closed(tmp_path: Path) -> None:
    """The same disappearance on the caller's own pin must fail closed.

    ``read_ready_pin`` reports absence; each caller decides what absence means.
    ``blocking_pins`` skips a vanished worker, while ``guard_pin`` treats a
    vanished authorization pin as lost authority. Both meanings are pinned so a
    future change to undef semantics cannot silently flip one of them.
    """
    state = tmp_path / "state"
    state.mkdir()
    token, finisher, _worker = _handed_off_generation(state)

    result = _finish(
        state, token, finisher,
        failpoint=f"unlink-ready-pin-before-open:{finisher}",
    )

    assert result.returncode != 0
    assert "process pin is absent" in result.stderr
