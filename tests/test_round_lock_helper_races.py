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
import stat
import subprocess
from pathlib import Path

import pytest

from tests.round_lock_test_utils import (
    TRANSITION_ORDER,
    assert_token,
    parse_acquire_output,
    read_record,
)

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
    nothing.

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


def _release_receipts(state: Path) -> list[Path]:
    return sorted(state.glob(".round_lock_release.*.tsv"))


def _revocations(state: Path) -> list[Path]:
    return sorted(state.glob(".round_lock_revocation.*.tsv"))


def test_authorizing_pin_replaced_by_a_different_file_is_rejected(
    tmp_path: Path,
) -> None:
    """A substituted regular file must be refused by the identity check."""
    state = tmp_path / "state"
    state.mkdir()
    token, finisher, _worker = _handed_off_generation(state)
    lock_dir = state / ".round_inflight.lockdir"

    result = _finish(
        state, token, finisher,
        failpoint=f"replace-ready-pin-with-file:{finisher}",
    )

    assert result.returncode != 0
    assert "record was replaced while being opened" in result.stderr
    # The tamper must not have advanced release state in any way.
    assert lock_dir.is_dir(), "canonical lock was removed by a rejected read"
    assert _release_receipts(state) == []
    assert _revocations(state) == []
    # A competing acquisition must still be blocked by the surviving lock.
    contender = _acquire(state, "run_contender")
    assert contender.returncode != 0


def test_authorizing_pin_replaced_by_a_symlink_is_rejected(tmp_path: Path) -> None:
    """A symlink substituted for the record path must be refused.

    ``ready.<token>.tsv`` and ``candidate.<token>.tsv`` are hard links to one
    inode, so a symlink pointing at the candidate resolves to the very inode
    the earlier ``lstat`` observed. A device/inode comparison therefore cannot
    reject it; ``O_NOFOLLOW`` is what does, and it rejects at the read itself.

    NOTE: removing ``O_NOFOLLOW`` does not make this substitution succeed in
    the current ``finish`` flow -- a later ``parse_record`` call re-``lstat``s
    the path and rejects it with a different message. So this test does not
    demonstrate that the record would otherwise be *accepted*; it pins that
    rejection happens at the read, rather than depending on some later reader
    incidentally noticing.
    """
    state = tmp_path / "state"
    state.mkdir()
    token, finisher, _worker = _handed_off_generation(state)
    lock_dir = state / ".round_inflight.lockdir"

    result = _finish(
        state, token, finisher,
        failpoint=f"replace-ready-pin-with-symlink:{finisher}",
    )

    assert result.returncode != 0
    assert "record path is a symlink" in result.stderr
    assert lock_dir.is_dir(), "canonical lock was removed by a rejected read"
    assert _release_receipts(state) == []
    assert _revocations(state) == []
    contender = _acquire(state, "run_contender")
    assert contender.returncode != 0


def test_unmodified_control_release_still_succeeds(tmp_path: Path) -> None:
    """Control: the same flow without substitution releases cleanly.

    Without this, the rejection tests above could pass because the flow is
    broken rather than because tampering was detected.
    """
    state = tmp_path / "state"
    state.mkdir()
    token, finisher, worker = _handed_off_generation(state)

    unpin = _helper(
        "unpin", state, token=token, round_barcode="run_toctou",
        scope="full_round", pin_token=worker,
    )
    assert unpin.returncode == 0, unpin.stderr

    result = _finish(state, token, finisher)

    assert result.returncode == 0, result.stderr
    assert len(_release_receipts(state)) == 1
    assert not (state / ".round_inflight.lockdir").exists()


def _assert_absent(path: Path) -> None:
    """Require ``path`` to be absent, accepting only ENOENT.

    Path.exists() follows symlinks, so a dangling symlink at the canonical lock
    path reports absent while a later mkdir on it returns EEXIST -- the fixture
    would certify a clean slate for a state where acquisition contends.
    """
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return
    raise AssertionError(f"expected {path} to be absent, found mode {entry.st_mode:o}")


def _interrupted_release(state: Path) -> tuple[list[str], str, Path, dict[str, str]]:
    """Drive one release to the ``after-quarantine-rename`` failpoint.

    Shared by the concurrency regression and its setup gate so the two cannot
    drift apart -- the gate has to exercise the exact preparation the
    regression runs, not a copy of it. Every check below is a fail-closed
    assertion on the intermediate state: the gate's whole job is to separate
    "the known recovery defect" from "the setup silently stopped working", and
    a check that cannot fail does neither.

    Returns the CLI base arguments, the generation token, the single quarantine
    left behind, and its validated transition record.
    """
    state.mkdir()
    base = [
        "--state-dir", str(state), "--round-barcode", "r",
        "--scope", "dorado_only", "--owner-pid", str(os.getpid()),
        "--stale-seconds", "30", "--wait-seconds", "5",
    ]
    acquired = subprocess.run(
        ["perl", str(SCRIPT), "acquire", *base],
        capture_output=True, text=True, check=False,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)

    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = "after-quarantine-rename"
    injected = subprocess.run(
        ["perl", str(SCRIPT), "early-release", *base,
         "--token", generation_token, "--pin-token", pin_token],
        capture_output=True, text=True, check=False, env=env,
    )
    # Prove this exact interleaving was established. The helper has three
    # distinct "injected failure" messages -- after transition install (:998),
    # after quarantine rename (:1055), and before compatibility inflight
    # publish (:1385) -- which leave different intermediate states, so the
    # substring alone cannot tell them apart.
    assert injected.returncode != 0, injected.stdout
    assert injected.stderr.strip() == (
        "ERROR: injected failure after quarantine rename"
    ), injected.stderr

    # Assert on every entry in the namespace, then validate the sole one.
    # Filtering first and counting after cannot detect an unexpected entry: the
    # filter discards the evidence before the assertion sees it.
    candidates = sorted(state.glob(".round_inflight.lockdir.*"))
    assert len(candidates) == 1, candidates
    quarantine = candidates[0]
    # One lstat, not Path.is_dir(): is_dir() follows symlinks, and pairing it
    # with is_symlink() leaves a window between two checks. The helper holds
    # itself to this standard at bin/round_lock_generation.pl:963-969. No
    # FileNotFoundError guard either -- setup is single-process here, so a
    # vanished candidate is an anomaly, not a race to skip.
    entry = os.lstat(quarantine)
    assert stat.S_ISDIR(entry.st_mode), (quarantine, entry.st_mode)

    transition = read_record(
        quarantine / "transition.tsv", required=TRANSITION_ORDER
    )
    assert_token(transition["operation_token"])
    assert transition["schema"] == "1", transition
    assert transition["action"] == "release", transition
    assert transition["owner_token"] == generation_token, transition
    assert transition["allowed_pin_token"] == pin_token, transition
    assert transition["round_barcode"] == "r", transition
    assert transition["scope"] == "dorado_only", transition
    assert transition["reason"] == "dorado_only_early", transition
    assert transition["effective_ttl_seconds"] == "30", transition
    assert transition["started_epoch"].isdigit(), transition
    # Bind the record to the directory it describes. The quarantine is the
    # canonical lock renamed, and rename preserves the inode, so these must
    # still match the tree on disk.
    assert transition["lock_dev"] == str(entry.st_dev), (transition, entry.st_dev)
    assert transition["lock_ino"] == str(entry.st_ino), (transition, entry.st_ino)

    # Exact equality, not endswith. recover_quarantines scans an anchored
    # namespace, /\A\.round_inflight\.lockdir\.(?:reclaim|release)-[0-9a-f]{64}\z/,
    # and re-derives this same name at bin/round_lock_generation.pl:1083, so the
    # name is part of the recovery contract. A suffixed variant such as
    # ...release-<op>.cleanup-<op> satisfies endswith while falling outside that
    # anchor -- invisible to recovery, which is the orphaning defect withdrawn
    # in 371c1f3. This assertion is deliberately coupled to the current naming:
    # when the protocol legitimately renames the intermediate, it must fail here
    # and be updated consciously.
    expected_name = ".round_inflight.lockdir.release-{}".format(
        transition["operation_token"]
    )
    assert quarantine.name == expected_name, (quarantine.name, expected_name)
    _assert_absent(state / ".round_inflight.lockdir")
    return base, generation_token, quarantine, transition


def test_concurrent_release_recovery_is_single_winner(tmp_path: Path) -> None:
    """Many concurrent recoverers of one interrupted release must all succeed.

    KNOWN DEFECT: this test currently FAILS, and is deliberately left unmarked
    so the branch stays visibly red until the recovery protocol lands. It was
    previously ``xfail(strict=False)``, which exits 0 on both XFAIL and XPASS
    and so gated nothing; ``strict=True`` would not have helped either, since
    it only fails on XPASS and still accepts the known failure.

    Concurrent recovery of one interrupted release is not idempotent. Every
    read in ``recover_quarantines`` can observe the quarantine vanish under it,
    producing three failure classes: concurrent ``remove_tree``, ``pending
    release lost its authenticated process pin``, and ``quarantine lacks a
    valid transition``. Measured 13 failures in 96 concurrent recoveries; the
    rate is nondeterministic, so individual runs can pass outright.

    A cleanup-claim protocol was attempted and withdrawn in 371c1f3: claiming
    by rename moved the tree outside the recovery scan namespace, orphaning it
    on crash, and making claims enumerable to fix that made live claims
    stealable. The fix needs an owner-identity and liveness contract, not
    another rename.

    Uses ``Popen`` rather than shell jobs, which spawn too slowly to hit the
    window.
    """
    failures: list[str] = []
    total = 0
    for trial in range(6):
        base, generation_token, _quarantine, _transition = _interrupted_release(
            tmp_path / f"state-{trial}"
        )

        procs = [
            subprocess.Popen(
                ["perl", str(SCRIPT), "verify-release", *base,
                 "--token", generation_token],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(8)
        ]
        for proc in procs:
            _, stderr = proc.communicate()
            total += 1
            if proc.returncode != 0:
                failures.append(stderr.strip().splitlines()[0] if stderr.strip() else "?")

    assert total == 48
    assert not failures, f"{len(failures)}/{total} recoveries failed: {failures[:3]}"


def test_after_quarantine_rename_failpoint_establishes_the_interleaving(
    tmp_path: Path,
) -> None:
    """Gate the shared preparation the concurrency regression depends on.

    That regression is expected to fail on the known recovery defect for as
    long as the protocol is unfinished, so its own failure carries no
    information about whether the failpoint still works. This test runs the
    same ``_interrupted_release`` helper and nothing else, separating "red
    because of the known defect" from "red because the setup broke".
    """
    _interrupted_release(tmp_path / "state")
