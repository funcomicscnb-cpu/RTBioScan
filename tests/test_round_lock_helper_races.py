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
    helper does not expose. Until that exists, the protection for that path is
    the ``die`` guard in ``pin_is_blocking``, which turns an undefined record
    from a silent foreign-host misclassification into a loud internal error.

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
