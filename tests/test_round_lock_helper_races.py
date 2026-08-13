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

import fcntl
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

from tests.round_lock_test_utils import (
    GENERATION_SCHEMA,
    EVENT_SCHEMA,
    MARKER_SCHEMA,
    PIN_SCHEMA,
    RELEASE_SCHEMA,
    TRANSITION_SCHEMA,
    assert_exact_error_line,
    assert_token,
    parse_acquire_output,
    perl_test_env,
    read_nofollow_bytes,
    read_record,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
EVENTS_DIR_NAME = ".round_lock_events"


def _acquire_argv(
    state: Path,
    round_barcode: str = "run_1",
    *,
    stale_seconds: int = 30,
) -> list[str]:
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
        str(stale_seconds),
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


def _acquire_raw(
    state: Path,
    round_barcode: str = "run_1",
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        _acquire_argv(state, round_barcode),
        capture_output=True,
        check=False,
        env=perl_test_env(),
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
        results = []
        for proc in procs:
            stdout, stderr = proc.communicate()
            results.append((proc.returncode, stdout, stderr))
            attempts += 1
            assert "cannot create directory" not in stderr, stderr
            assert f"{EVENTS_DIR_NAME}: File exists" not in stderr, stderr
        winners = [result for result in results if result[0] == 0]
        assert len(winners) == 1, results
        winner_token, _winner_pin = parse_acquire_output(
            winners[0][1].encode("ascii")
        )
        failures = [result for result in results if result[0] != 0]
        assert len(failures) == 3, results
        expected = {
            f"ERROR: timed out waiting for round lock '{state / '.round_inflight.lockdir'}'",
            "ERROR: timed out waiting for round-lock state fence",
        }
        for _rc, stdout, stderr in failures:
            assert stdout == ""
            error_lines = {
                line for line in stderr.splitlines() if line.startswith("ERROR:")
            }
            assert len(error_lines) == 1 and error_lines <= expected, failures
        assert (state / EVENTS_DIR_NAME).is_dir()
        generation = read_record(
            state / ".round_inflight.lockdir" / "generation.tsv",
            schema=GENERATION_SCHEMA,
        )
        assert generation["token"] == winner_token
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
    assert not os.path.lexists(state / ".round_inflight.lockdir")
    assert not list(state.glob(".round_inflight.lockdir.failed-acquire-*"))


def test_events_directory_rejects_a_regular_file(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / EVENTS_DIR_NAME).write_text("not a directory\n", encoding="utf-8")

    result = _acquire(state)

    assert result.returncode != 0
    assert "expected a real directory, not a symlink" in result.stderr
    assert not os.path.lexists(state / ".round_inflight.lockdir")
    assert not list(state.glob(".round_inflight.lockdir.failed-acquire-*"))


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

    acquire = _acquire_raw(state, "run_release")
    assert acquire.returncode == 0, acquire.stderr
    generation_token, pin_token = parse_acquire_output(acquire.stdout)

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
            generation_token,
            "--pin-token",
            pin_token,
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
    _assert_absent(lock_dir)

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


def test_state_fence_adopts_terminal_archive_before_source_removal() -> None:
    """A recovered rename must make its destination durable before its source."""
    body = _sub_body("acquire_state_fence")
    archive_sync = "sync_directory($archive_dir);"
    state_sync = "sync_directory($state_dir);"

    assert body.count(archive_sync) == 1, body
    assert body.count(state_sync) == 1, body
    assert body.index(archive_sync) < body.index(state_sync), (
        "terminal archive adoption synced the source parent before the "
        "destination parent"
    )


def test_blocking_pins_adopts_pin_directory_before_enumeration() -> None:
    """An empty ready-pin namespace is authority only after its parent fsync."""
    body = _sub_body("blocking_pins")
    validation = "if !@st || -l _ || !-d _;"
    pin_sync = "sync_directory($pin_dir);"
    enumeration = "opendir(my $dh, $pin_dir)"

    assert body.count(pin_sync) == 1, body
    assert body.index(validation) < body.index(pin_sync) < body.index(enumeration), (
        "blocking_pins did not validate and sync pins/ before enumerating it"
    )


def _write_unlink_pause_module(root: Path) -> Path:
    """Override one exact unlink and pause after the kernel removes its name."""
    module_dir = root / "perl-unlink-hook"
    module_dir.mkdir()
    (module_dir / "RTBioScanRoundLockUnlinkPause.pm").write_text(
        r'''package RTBioScanRoundLockUnlinkPause;
use strict;
use warnings;
use Fcntl qw(O_WRONLY O_CREAT O_EXCL);
use IO::Handle ();
use Time::HiRes qw(time usleep);
BEGIN {
    no warnings 'redefine';
    *CORE::GLOBAL::unlink = sub {
        my @paths = @_;
        my $target = $ENV{RTBIOSCAN_TEST_UNLINK_TARGET} // '';
        if (@paths == 1 && $target ne '' && $paths[0] eq $target) {
            my $removed = CORE::unlink(@paths);
            die "unlink pause failed to remove exact target: $target: $!\n"
                if $removed != 1;
            my $ready = $ENV{RTBIOSCAN_TEST_UNLINK_READY} // '';
            sysopen(my $ready_fh, $ready, O_WRONLY | O_CREAT | O_EXCL, 0600)
                or die "cannot publish unlink readiness '$ready': $!\n";
            print {$ready_fh} "unlinked\t$target\n"
                or die "cannot write unlink readiness '$ready': $!\n";
            $ready_fh->flush()
                or die "cannot flush unlink readiness '$ready': $!\n";
            $ready_fh->sync()
                or die "cannot sync unlink readiness '$ready': $!\n";
            close($ready_fh)
                or die "cannot close unlink readiness '$ready': $!\n";
            my $release = $ENV{RTBIOSCAN_TEST_UNLINK_RELEASE} // '';
            my $deadline = time() + 10;
            while (!-e $release) {
                die "timed out at exact unlink pause: $target\n"
                    if time() >= $deadline;
                usleep(10_000);
            }
            return $removed;
        }
        return CORE::unlink(@paths);
    };
}
1;
''',
        encoding="utf-8",
    )
    return module_dir


def _regular_file_manifest(root: Path) -> dict[str, bytes]:
    """Return every regular file and its bytes without following symlinks."""
    manifest: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        entry = os.lstat(path)
        if stat.S_ISREG(entry.st_mode):
            manifest[path.relative_to(root).as_posix()] = read_nofollow_bytes(path)
    return manifest


def _assert_state_fence_is_held(state: Path) -> None:
    descriptor = os.open(state, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def _wait_for_exact_unlink_pause(
    process: subprocess.Popen[bytes],
    ready: Path,
    target: Path,
) -> None:
    deadline = time.monotonic() + 5
    expected = f"unlinked\t{target}\n".encode("utf-8")
    while True:
        try:
            entry = os.lstat(ready)
        except FileNotFoundError:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    "unpin helper exited before the exact unlink boundary: "
                    f"rc={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            if time.monotonic() >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                pytest.fail(
                    "unpin helper did not publish the exact unlink boundary: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
            continue

        assert stat.S_ISREG(entry.st_mode), entry
        assert not stat.S_ISLNK(entry.st_mode), entry
        assert read_nofollow_bytes(ready) == expected
        assert process.poll() is None
        return


def test_reclaimer_adopts_interrupted_ready_pin_unlink(tmp_path: Path) -> None:
    """A killed unpin is adopted before its empty ready namespace authorizes.

    The external Perl hook calls the real unlink for one exact ready token and
    pauses before returning, so ``unpin_generation`` cannot reach its pins/
    fsync.  The subsequent reclaimer must fsync that visible namespace before
    treating it as free of ready pins; the structural test immediately above
    pins the otherwise power-loss-only ordering requirement.
    """
    state = tmp_path / "state"
    state.mkdir()
    acquire = subprocess.run(
        _acquire_argv(state, "unlink_owner", stale_seconds=1),
        capture_output=True,
        check=False,
        env=perl_test_env(),
    )
    assert acquire.returncode == 0, acquire.stderr
    generation_token, acquisition_pin = parse_acquire_output(acquire.stdout)

    handoff = _helper(
        "handoff",
        state,
        token=generation_token,
        round_barcode="unlink_owner",
        scope="full_round",
        pin_token=acquisition_pin,
    )
    assert handoff.returncode == 0, handoff.stderr

    target_pin = "a" * 64
    pin = _helper(
        "pin",
        state,
        token=generation_token,
        round_barcode="unlink_owner",
        scope="full_round",
        role="state_writer",
        pin_token=target_pin,
    )
    assert pin.returncode == 0, pin.stderr
    assert pin.stdout == f"{target_pin}\n"

    lock = state / ".round_inflight.lockdir"
    pins = lock / "pins"
    candidate = pins / f"candidate.{target_pin}.tsv"
    ready_pin = pins / f"ready.{target_pin}.tsv"
    candidate_before = os.lstat(candidate)
    ready_before = os.lstat(ready_pin)
    assert stat.S_ISREG(candidate_before.st_mode), candidate_before
    assert stat.S_ISREG(ready_before.st_mode), ready_before
    assert (candidate_before.st_dev, candidate_before.st_ino) == (
        ready_before.st_dev,
        ready_before.st_ino,
    )
    assert candidate_before.st_nlink == ready_before.st_nlink == 2
    pin_record = read_record(ready_pin, schema=PIN_SCHEMA)
    assert pin_record["token"] == generation_token, pin_record
    assert pin_record["pin_token"] == target_pin, pin_record
    assert pin_record["role"] == "state_writer", pin_record
    assert pin_record["pid"] == str(os.getpid()), pin_record
    os.kill(int(pin_record["pid"]), 0)
    candidate_bytes = read_nofollow_bytes(candidate)
    before_files = _regular_file_manifest(state)
    ready_relative = ready_pin.relative_to(state).as_posix()
    assert ready_relative in before_files
    _assert_absent(lock / "transition.tsv")
    assert _revocations(state) == []
    assert _release_receipts(state) == []

    module_dir = _write_unlink_pause_module(tmp_path)
    pause_ready = tmp_path / "unlink.ready"
    pause_release = tmp_path / "unlink.release"
    env = perl_test_env(
        RTBIOSCAN_TEST_UNLINK_TARGET=str(ready_pin),
        RTBIOSCAN_TEST_UNLINK_READY=str(pause_ready),
        RTBIOSCAN_TEST_UNLINK_RELEASE=str(pause_release),
    )
    env["PERL5LIB"] = os.pathsep.join(
        part for part in (str(module_dir), env.get("PERL5LIB", "")) if part
    )
    env["PERL5OPT"] = " ".join(
        part
        for part in ("-MRTBioScanRoundLockUnlinkPause", env.get("PERL5OPT", ""))
        if part
    )
    unpin = subprocess.Popen(
        [
            "perl",
            str(SCRIPT),
            "unpin",
            "--state-dir",
            str(state),
            "--round-barcode",
            "unlink_owner",
            "--scope",
            "full_round",
            "--token",
            generation_token,
            "--pin-token",
            target_pin,
            "--owner-pid",
            str(os.getpid()),
            "--stale-seconds",
            "1",
            "--wait-seconds",
            "5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        _wait_for_exact_unlink_pause(unpin, pause_ready, ready_pin)

        # Positive controls for the exact crash boundary: the target name is
        # truly gone, the wrapper and pin owner are live, and unpin still owns
        # the state fence because the post-unlink directory sync was not run.
        _assert_absent(ready_pin)
        candidate_paused = os.lstat(candidate)
        assert (candidate_paused.st_dev, candidate_paused.st_ino) == (
            candidate_before.st_dev,
            candidate_before.st_ino,
        )
        assert candidate_paused.st_nlink == 1
        assert read_nofollow_bytes(candidate) == candidate_bytes
        assert unpin.poll() is None
        os.kill(unpin.pid, 0)
        os.kill(int(pin_record["pid"]), 0)
        _assert_state_fence_is_held(state)

        expected_files = dict(before_files)
        del expected_files[ready_relative]
        assert _regular_file_manifest(state) == expected_files
        _assert_absent(lock / "transition.tsv")
        assert _revocations(state) == []
        assert _release_receipts(state) == []
        assert not (state / ".round_lock_archives").exists()

        unpin.send_signal(signal.SIGKILL)
        stdout, stderr = unpin.communicate(timeout=5)
        assert unpin.returncode == -signal.SIGKILL, (unpin.returncode, stderr)
        assert stdout == b"", stdout
        assert stderr == b"", stderr
    finally:
        if unpin.poll() is None:
            unpin.kill()
            unpin.communicate(timeout=5)

    newest_epoch = int(
        max(
            os.lstat(lock).st_mtime,
            os.lstat(lock / "generation.tsv").st_mtime,
            os.lstat(pins).st_mtime,
            os.lstat(state / f".round_lock_handoff.{generation_token}.tsv").st_mtime,
        )
    )
    deadline = time.monotonic() + 3
    while int(time.time()) - newest_epoch < 1:
        assert time.monotonic() < deadline
        time.sleep(0.01)

    contender = subprocess.run(
        _acquire_argv(state, "unlink_contender"),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=10,
    )
    assert contender.returncode == 0, contender.stderr
    contender_token, _contender_pin = parse_acquire_output(contender.stdout)
    assert contender_token != generation_token

    archives = sorted((state / ".round_lock_archives").glob("reclaim-*"))
    assert len(archives) == 1, archives
    archive = archives[0]
    transition = read_record(archive / "transition.tsv", schema=TRANSITION_SCHEMA)
    assert transition["action"] == "reclaim", transition
    assert transition["owner_token"] == generation_token, transition
    archived_candidate = archive / "pins" / candidate.name
    archived_candidate_entry = os.lstat(archived_candidate)
    assert (archived_candidate_entry.st_dev, archived_candidate_entry.st_ino) == (
        candidate_before.st_dev,
        candidate_before.st_ino,
    )
    assert archived_candidate_entry.st_nlink == 1
    assert read_nofollow_bytes(archived_candidate) == candidate_bytes
    _assert_absent(archive / "pins" / ready_pin.name)
    assert not list(state.glob(".round_inflight.lockdir.reclaim-*"))


def _helper(action: str, state: Path, **opts: str) -> subprocess.CompletedProcess[str]:
    argv = ["perl", str(SCRIPT), action, "--state-dir", str(state)]
    for key, value in opts.items():
        argv += [f"--{key.replace('_', '-')}", value]
    argv += ["--owner-pid", str(os.getpid()), "--stale-seconds", "30", "--wait-seconds", "5"]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _handed_off_generation(state: Path) -> tuple[str, str, str]:
    """Acquire, hand off, and return (token, finisher_pin, worker_pin)."""
    acquire = _acquire_raw(state, "run_toctou")
    assert acquire.returncode == 0, acquire.stderr
    token, acquisition_pin = parse_acquire_output(acquire.stdout)

    handoff = _helper(
        "handoff", state, token=token, round_barcode="run_toctou",
        scope="full_round", pin_token=acquisition_pin,
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
    _assert_absent(state / ".round_inflight.lockdir")


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
    assertion on the intermediate state: the gate distinguishes a recovery
    regression from setup that silently stopped reaching the intended boundary.

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
        capture_output=True, check=False, env=perl_test_env(),
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)

    env = perl_test_env(
        RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-quarantine-rename"
    )
    injected = subprocess.run(
        ["perl", str(SCRIPT), "early-release", *base,
         "--token", generation_token, "--pin-token", pin_token],
        capture_output=True, check=False, env=env,
    )
    # Prove this exact interleaving was established. The helper has three
    # distinct "injected failure" messages -- after transition install (:998),
    # after quarantine rename (:1055), and before compatibility inflight
    # publish (:1385) -- which leave different intermediate states, so the
    # substring alone cannot tell them apart.
    assert injected.returncode != 0, injected.stdout
    assert_exact_error_line(
        injected.stderr,
        "ERROR: injected failure after quarantine rename",
    )

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

    generation = read_record(
        quarantine / "generation.tsv", schema=GENERATION_SCHEMA
    )
    assert generation["schema"] == "1", generation
    assert generation["token"] == generation_token, generation
    assert generation["round_barcode"] == "r", generation
    assert generation["scope"] == "dorado_only", generation
    assert generation["effective_ttl_seconds"] == "30", generation
    assert generation["lock_dev"] == str(entry.st_dev), generation
    assert generation["lock_ino"] == str(entry.st_ino), generation

    pins_dir = quarantine / "pins"
    pin_entries = sorted(path.name for path in pins_dir.iterdir())
    expected_pin_entries = [
        f"candidate.{pin_token}.tsv",
        f"ready.{pin_token}.tsv",
    ]
    assert pin_entries == expected_pin_entries, pin_entries
    candidate_path = pins_dir / expected_pin_entries[0]
    ready_path = pins_dir / expected_pin_entries[1]
    candidate_stat = os.lstat(candidate_path)
    ready_stat = os.lstat(ready_path)
    assert stat.S_ISREG(candidate_stat.st_mode), candidate_stat
    assert stat.S_ISREG(ready_stat.st_mode), ready_stat
    assert (candidate_stat.st_dev, candidate_stat.st_ino) == (
        ready_stat.st_dev,
        ready_stat.st_ino,
    )
    assert read_nofollow_bytes(candidate_path) == read_nofollow_bytes(ready_path)
    pin = read_record(ready_path, schema=PIN_SCHEMA)
    assert pin["token"] == generation_token, pin
    assert pin["pin_token"] == pin_token, pin
    assert pin["role"] == "fast_acquisition", pin
    assert pin["lock_dev"] == str(entry.st_dev), pin
    assert pin["lock_ino"] == str(entry.st_ino), pin

    transition = read_record(
        quarantine / "transition.tsv", schema=TRANSITION_SCHEMA
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

    marker = read_record(
        state / f".round_lock_handoff.{generation_token}.tsv",
        schema=MARKER_SCHEMA,
    )
    assert marker["token"] == generation_token, marker
    assert marker["round_barcode"] == "r", marker
    assert marker["scope"] == "dorado_only", marker
    assert marker["outcome"] == "handoff", marker

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

    The helper-wide state-directory flock now serializes every cooperating
    recovery command through validation, outcome publication, and the atomic
    move to the terminal release archive. This test retains the high-contention
    ``Popen`` shape that reproduced the old three-way read/remove race and
    requires every waiter to observe the same idempotent receipt.
    """
    failures: list[str] = []
    total = 0
    for trial in range(6):
        base, generation_token, quarantine, transition = _interrupted_release(
            tmp_path / f"state-{trial}"
        )
        quarantine_manifest = {
            path.relative_to(quarantine).as_posix(): (
                os.lstat(path).st_dev, os.lstat(path).st_ino,
                read_nofollow_bytes(path) if stat.S_ISREG(os.lstat(path).st_mode)
                else None,
            )
            for path in [quarantine, *sorted(quarantine.rglob("*"))]
        }

        procs = [
            subprocess.Popen(
                ["perl", str(SCRIPT), "verify-release", *base,
                 "--token", generation_token],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(8)
        ]
        for proc in procs:
            stdout, stderr = proc.communicate()
            total += 1
            if proc.returncode != 0:
                failures.append(stderr.strip().splitlines()[0] if stderr.strip() else "?")
            else:
                assert stdout == "dorado_only_early\n", stdout

        state = quarantine.parent
        receipt = read_record(
            state / f".round_lock_release.{generation_token}.tsv",
            schema=RELEASE_SCHEMA,
        )
        assert receipt["operation_token"] == transition["operation_token"]
        archive = state / ".round_lock_archives" / (
            f"release-{transition['operation_token']}"
        )
        archive_manifest = {
            path.relative_to(archive).as_posix(): (
                os.lstat(path).st_dev, os.lstat(path).st_ino,
                read_nofollow_bytes(path) if stat.S_ISREG(os.lstat(path).st_mode)
                else None,
            )
            for path in [archive, *sorted(archive.rglob("*"))]
        }
        assert archive_manifest == quarantine_manifest
        events = [
            read_record(path, schema=EVENT_SCHEMA)
            for path in (state / ".round_lock_events").glob("*.tsv")
        ]
        releases = [event for event in events if event["event"] == "release"]
        assert len(releases) == 1
        assert releases[0]["event_id"] == transition["operation_token"]
        assert not list(state.glob(".round_inflight.lockdir.release-*"))

    assert total == 48
    assert not failures, f"{len(failures)}/{total} recoveries failed: {failures[:3]}"


def test_after_quarantine_rename_failpoint_establishes_the_interleaving(
    tmp_path: Path,
) -> None:
    """Gate the shared preparation the concurrency regression depends on.

    The concurrent regression and this setup gate exercise the same
    ``_interrupted_release`` helper. Keeping a separate gate makes a broken
    failpoint distinguishable from a regression in serialized recovery.
    """
    _interrupted_release(tmp_path / "state")
