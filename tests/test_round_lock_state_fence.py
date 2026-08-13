"""Deterministic state-fence regressions for the round-lock helper.

The pause protocol is deliberately a handshake, not a delay.  A helper at a
named semantic boundary creates ``RTBIOSCAN_ROUND_LOCK_TEST_READY`` with the
exact failpoint name and waits for ``RTBIOSCAN_ROUND_LOCK_TEST_RELEASE``.  A
test proceeds only after validating that readiness record.  An unknown or
ignored failpoint therefore fails the setup gate instead of exercising an
unrelated interleaving and reporting a false green.
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
    EVENT_SCHEMA,
    GENERATION_SCHEMA,
    PIN_SCHEMA,
    RELEASE_SCHEMA,
    REVOCATION_SCHEMA,
    TRANSITION_SCHEMA,
    assert_exact_error_line,
    parse_acquire_output,
    perl_test_env,
    read_nofollow_bytes,
    read_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
LOCK_NAME = ".round_inflight.lockdir"

INITIALIZATION_BOUNDARIES = (
    "after-lock-mkdir-before-stat",
    "after-lock-stat-before-pins",
    "after-pins-sync-before-generation",
    "after-generation-install",
)

RELEASE_RECOVERY_BOUNDARY = "before-quarantine-recovery-outcome"


def _acquire_command(
    state: Path,
    *,
    round_barcode: str,
    scope: str = "full_round",
    owner_pid: int | None = None,
    wait_seconds: int = 1,
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
        scope,
        "--owner-pid",
        str(os.getpid() if owner_pid is None else owner_pid),
        "--stale-seconds",
        "300",
        "--wait-seconds",
        str(wait_seconds),
    ]


def _generation_command(
    action: str,
    state: Path,
    *,
    token: str,
    round_barcode: str,
    scope: str,
    pin_token: str | None = None,
) -> list[str]:
    command = [
        "perl",
        str(SCRIPT),
        action,
        "--state-dir",
        str(state),
        "--round-barcode",
        round_barcode,
        "--scope",
        scope,
        "--token",
        token,
        "--owner-pid",
        str(os.getpid()),
        "--stale-seconds",
        "300",
        "--wait-seconds",
        "1",
    ]
    if pin_token is not None:
        command += ["--pin-token", pin_token]
    return command


def _wait_for_pause(
    process: subprocess.Popen[bytes],
    ready: Path,
    failpoint: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Wait for and validate one positively identified helper boundary."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            entry = os.lstat(ready)
        except FileNotFoundError:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"helper exited before reaching {failpoint!r}: "
                    f"rc={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
                )
            if time.monotonic() >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"helper did not publish readiness for {failpoint!r}: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
            continue

        assert stat.S_ISREG(entry.st_mode), (ready, entry.st_mode)
        assert not stat.S_ISLNK(entry.st_mode), (ready, entry.st_mode)
        assert read_nofollow_bytes(ready) == f"{failpoint}\n".encode("ascii")
        assert process.poll() is None, f"helper exited at pause {failpoint!r}"
        return


def _publish_release(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, b"release\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _tree_manifest(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture names, identities, types, and regular-file bytes under root."""
    manifest: list[tuple[object, ...]] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: str(item)):
        entry = os.lstat(path)
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISREG(entry.st_mode):
            kind = "file"
            content: bytes | None = read_nofollow_bytes(path)
        elif stat.S_ISDIR(entry.st_mode):
            kind = "directory"
            content = None
        elif stat.S_ISLNK(entry.st_mode):
            kind = "symlink"
            content = os.readlink(path).encode("utf-8", errors="surrogateescape")
        else:
            kind = "other"
            content = None
        manifest.append(
            (
                relative,
                kind,
                stat.S_IMODE(entry.st_mode),
                entry.st_dev,
                entry.st_ino,
                entry.st_nlink,
                entry.st_size,
                entry.st_mtime_ns,
                content,
            )
        )
    return tuple(manifest)


def _assert_absent(path: Path) -> None:
    """Require true ENOENT; ``Path.exists`` would hide a dangling symlink."""
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return
    raise AssertionError(f"expected {path} to be absent, found mode {entry.st_mode:o}")


def _assert_state_fence_is_held(state: Path) -> None:
    """Independently prove another process owns the state-directory flock."""
    descriptor = os.open(state, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def _assert_initialization_boundary(state: Path, failpoint: str) -> str | None:
    """Validate the exact durable namespace expected at an acquire pause."""
    lock = state / LOCK_NAME
    lock_entry = os.lstat(lock)
    assert stat.S_ISDIR(lock_entry.st_mode), lock_entry
    assert not stat.S_ISLNK(lock_entry.st_mode), lock_entry

    if failpoint in {
        "after-lock-mkdir-before-stat",
        "after-lock-stat-before-pins",
    }:
        assert list(lock.iterdir()) == []
        return None

    pins = lock / "pins"
    pins_entry = os.lstat(pins)
    assert stat.S_ISDIR(pins_entry.st_mode), pins_entry
    assert not stat.S_ISLNK(pins_entry.st_mode), pins_entry
    assert list(pins.iterdir()) == []

    if failpoint == "after-pins-sync-before-generation":
        assert sorted(path.name for path in lock.iterdir()) == ["pins"]
        return None

    assert failpoint == "after-generation-install"
    assert sorted(path.name for path in lock.iterdir()) == [
        "generation.tsv",
        "pins",
    ]
    generation = read_record(lock / "generation.tsv", schema=GENERATION_SCHEMA)
    assert generation["schema"] == "1", generation
    assert generation["round_barcode"] == "owner", generation
    assert generation["scope"] == "full_round", generation
    assert generation["effective_ttl_seconds"] == "300", generation
    assert generation["lock_dev"] == str(lock_entry.st_dev), generation
    assert generation["lock_ino"] == str(lock_entry.st_ino), generation
    return generation["token"]


def _kill_at_pause(process: subprocess.Popen[bytes]) -> None:
    """Kill a positively gated helper and require an unhandled SIGKILL."""
    assert process.poll() is None
    process.send_signal(signal.SIGKILL)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL, (
        process.returncode,
        stdout,
        stderr,
    )
    assert stdout == b"", stdout
    assert stderr == b"", stderr


@pytest.mark.parametrize("failpoint", INITIALIZATION_BOUNDARIES)
def test_sigkill_at_acquisition_initialization_boundary_fails_closed(
    tmp_path: Path,
    failpoint: str,
) -> None:
    """A killed initializer leaves either invalid state or one live owner.

    The first three boundaries have not installed a generation record, so the
    surviving canonical directory has unknown provenance and must fail closed.
    At the fourth boundary the generation record names this pytest process as
    its live owner; the lack of a ready pin does not make that generation
    reclaimable.  Two independent contenders pin persistence rather than an
    accidental one-shot obstruction.
    """
    state = tmp_path / "state"
    state.mkdir()
    ready = tmp_path / f"{failpoint}.kill.ready"
    release = tmp_path / f"{failpoint}.kill.release"
    owner = subprocess.Popen(
        _acquire_command(state, round_barcode="owner"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint,
            RTBIOSCAN_ROUND_LOCK_TEST_READY=str(ready),
            RTBIOSCAN_ROUND_LOCK_TEST_RELEASE=str(release),
            RTBIOSCAN_ROUND_LOCK_TEST_TIMEOUT_SECONDS="10",
        ),
    )
    try:
        _wait_for_pause(owner, ready, failpoint)
        installed_token = _assert_initialization_boundary(state, failpoint)
        _assert_state_fence_is_held(state)
        _kill_at_pause(owner)

        before = _tree_manifest(state)
        lock = state / LOCK_NAME
        lock_entry = os.lstat(lock)
        assert stat.S_ISDIR(lock_entry.st_mode), lock_entry
        assert not stat.S_ISLNK(lock_entry.st_mode), lock_entry

        if failpoint == "after-generation-install":
            assert installed_token is not None
            generation = read_record(
                lock / "generation.tsv", schema=GENERATION_SCHEMA
            )
            assert generation["token"] == installed_token, generation
            assert generation["pid"] == str(os.getpid()), generation
            assert generation["host"] != "", generation
            assert list((lock / "pins").iterdir()) == []
        else:
            assert installed_token is None
            _assert_absent(lock / "generation.tsv")

        for contender_round in ("contender-one", "contender-two"):
            contender = subprocess.run(
                _acquire_command(
                    state,
                    round_barcode=contender_round,
                    wait_seconds=1,
                ),
                capture_output=True,
                check=False,
                env=perl_test_env(),
                timeout=5,
            )
            assert contender.returncode != 0, contender.stdout
            assert contender.stdout == b"", contender.stdout
            if failpoint == "after-generation-install":
                expected = (
                    "ERROR: round lock host identity is unverifiable; "
                    "refusing automatic reclaim"
                    if generation["host"] == "unknown"
                    else f"ERROR: timed out waiting for round lock '{lock}'"
                )
                assert_exact_error_line(contender.stderr, expected)
            else:
                assert_exact_error_line(
                    contender.stderr,
                    "ERROR: round lock has absent generation state and requires "
                    f"operator quarantine: {lock}",
                )
            assert _tree_manifest(state) == before
            assert not list(state.glob(f"{LOCK_NAME}.failed-acquire-*"))
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.communicate(timeout=5)


@pytest.mark.parametrize("failpoint", INITIALIZATION_BOUNDARIES)
def test_state_fence_serializes_every_acquisition_initialization_boundary(
    tmp_path: Path,
    failpoint: str,
) -> None:
    """A contender cannot inspect or mutate a half-initialized generation."""
    state = tmp_path / "state"
    state.mkdir()
    ready = tmp_path / f"{failpoint}.ready"
    release = tmp_path / f"{failpoint}.release"
    owner_env = perl_test_env(
        RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint,
        RTBIOSCAN_ROUND_LOCK_TEST_READY=str(ready),
        RTBIOSCAN_ROUND_LOCK_TEST_RELEASE=str(release),
        RTBIOSCAN_ROUND_LOCK_TEST_TIMEOUT_SECONDS="10",
    )
    owner = subprocess.Popen(
        _acquire_command(state, round_barcode="owner"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=owner_env,
    )
    contender: subprocess.Popen[bytes] | None = None
    released = False
    try:
        _wait_for_pause(owner, ready, failpoint)
        paused_token = _assert_initialization_boundary(state, failpoint)
        _assert_state_fence_is_held(state)
        before_contender = _tree_manifest(state)

        contender = subprocess.Popen(
            _acquire_command(state, round_barcode="contender"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=perl_test_env(),
        )
        # Popen returns only after exec succeeds.  The independent nonblocking
        # flock assertion above proves why this still-running invocation cannot
        # enter the protected namespace; this short timeout only observes that
        # it has not exited through some pre-fence path.
        with pytest.raises(subprocess.TimeoutExpired):
            contender.communicate(timeout=0.25)
        assert _tree_manifest(state) == before_contender

        _publish_release(release)
        released = True
        owner_stdout, owner_stderr = owner.communicate(timeout=5)
        assert owner.returncode == 0, owner_stderr
        owner_token, owner_pin = parse_acquire_output(owner_stdout)
        if paused_token is not None:
            assert owner_token == paused_token

        lock = state / LOCK_NAME
        generation = read_record(lock / "generation.tsv", schema=GENERATION_SCHEMA)
        assert generation["token"] == owner_token, generation
        ready_pin = read_record(
            lock / "pins" / f"ready.{owner_pin}.tsv", schema=PIN_SCHEMA
        )
        assert ready_pin["token"] == owner_token, ready_pin
        assert ready_pin["pin_token"] == owner_pin, ready_pin

        after_owner = _tree_manifest(state)
        contender_stdout, contender_stderr = contender.communicate(timeout=5)
        assert contender.returncode != 0, contender_stdout
        expected = (
            f"ERROR: timed out waiting for round lock '{state / LOCK_NAME}'\n"
        ).encode("utf-8")
        assert contender_stderr == expected, contender_stderr
        assert _tree_manifest(state) == after_owner
        assert not list(state.glob(f"{LOCK_NAME}.failed-acquire-*"))
    finally:
        if not released and owner.poll() is None:
            try:
                _publish_release(release)
            except FileExistsError:
                pass
        if owner.poll() is None:
            try:
                owner.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.communicate()
        if contender is not None and contender.poll() is None:
            contender.kill()
            contender.communicate()


def test_state_fence_serializes_concurrent_interrupted_release_recovery(
    tmp_path: Path,
) -> None:
    """Only one verifier may publish and clean one quarantined release.

    The owner is paused after it has validated the quarantine and its release
    authority, but before it publishes the outcome.  This is the first point at
    which the recovery work is fully identified and no outcome has yet escaped.
    A second verifier must wait outside that namespace and then observe the
    first verifier's single idempotent result.
    """
    state = tmp_path / "state"
    state.mkdir()
    round_barcode = "release_owner"
    scope = "dorado_only"
    acquired = subprocess.run(
        _acquire_command(
            state,
            round_barcode=round_barcode,
            scope=scope,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
    )
    assert acquired.returncode == 0, acquired.stderr
    token, pin_token = parse_acquire_output(acquired.stdout)

    interrupted = subprocess.run(
        _generation_command(
            "early-release",
            state,
            token=token,
            pin_token=pin_token,
            round_barcode=round_barcode,
            scope=scope,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-quarantine-rename"
        ),
    )
    assert interrupted.returncode != 0, interrupted.stdout
    assert_exact_error_line(
        interrupted.stderr,
        "ERROR: injected failure after quarantine rename",
    )

    quarantines = sorted(state.glob(f"{LOCK_NAME}.release-*"))
    assert len(quarantines) == 1, quarantines
    quarantine = quarantines[0]
    quarantine_entry = os.lstat(quarantine)
    assert stat.S_ISDIR(quarantine_entry.st_mode), quarantine_entry
    transition = read_record(
        quarantine / "transition.tsv", schema=TRANSITION_SCHEMA
    )
    quarantine_manifest = _tree_manifest(quarantine)
    transition_bytes = read_nofollow_bytes(quarantine / "transition.tsv")
    expected_quarantine = (
        f"{LOCK_NAME}.release-{transition['operation_token']}"
    )
    assert quarantine.name == expected_quarantine, quarantine
    assert transition["action"] == "release", transition
    assert transition["owner_token"] == token, transition
    assert transition["allowed_pin_token"] == pin_token, transition
    assert transition["round_barcode"] == round_barcode, transition
    assert transition["scope"] == scope, transition
    assert transition["reason"] == "dorado_only_early", transition
    assert transition["lock_dev"] == str(quarantine_entry.st_dev), transition
    assert transition["lock_ino"] == str(quarantine_entry.st_ino), transition
    _assert_absent(state / LOCK_NAME)

    ready = tmp_path / "release-recovery.ready"
    release = tmp_path / "release-recovery.release"
    verifier_command = _generation_command(
        "verify-release",
        state,
        token=token,
        round_barcode=round_barcode,
        scope=scope,
    )
    owner = subprocess.Popen(
        verifier_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT=RELEASE_RECOVERY_BOUNDARY,
            RTBIOSCAN_ROUND_LOCK_TEST_READY=str(ready),
            RTBIOSCAN_ROUND_LOCK_TEST_RELEASE=str(release),
            RTBIOSCAN_ROUND_LOCK_TEST_TIMEOUT_SECONDS="10",
        ),
    )
    contender: subprocess.Popen[bytes] | None = None
    released = False
    try:
        _wait_for_pause(owner, ready, RELEASE_RECOVERY_BOUNDARY)
        _assert_state_fence_is_held(state)
        current_quarantine = os.lstat(quarantine)
        assert stat.S_ISDIR(current_quarantine.st_mode), current_quarantine
        assert (current_quarantine.st_dev, current_quarantine.st_ino) == (
            quarantine_entry.st_dev,
            quarantine_entry.st_ino,
        )
        _assert_absent(state / f".round_lock_release.{token}.tsv")
        before_contender = _tree_manifest(state)

        contender = subprocess.Popen(
            verifier_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=perl_test_env(),
        )
        with pytest.raises(subprocess.TimeoutExpired):
            contender.communicate(timeout=0.25)
        assert _tree_manifest(state) == before_contender

        _publish_release(release)
        released = True
        owner_stdout, owner_stderr = owner.communicate(timeout=5)
        assert owner.returncode == 0, owner_stderr
        assert owner_stdout == b"dorado_only_early\n"

        after_owner = _tree_manifest(state)
        contender_stdout, contender_stderr = contender.communicate(timeout=5)
        assert contender.returncode == 0, contender_stderr
        assert contender_stdout == b"dorado_only_early\n"
        assert _tree_manifest(state) == after_owner

        _assert_absent(state / LOCK_NAME)
        assert not list(state.glob(f"{LOCK_NAME}.release-*"))
        archive = state / ".round_lock_archives" / (
            f"release-{transition['operation_token']}"
        )
        archived_entry = os.lstat(archive)
        assert stat.S_ISDIR(archived_entry.st_mode), archived_entry
        assert (archived_entry.st_dev, archived_entry.st_ino) == (
            quarantine_entry.st_dev, quarantine_entry.st_ino,
        )
        assert read_nofollow_bytes(archive / "transition.tsv") == transition_bytes
        archived_manifest = _tree_manifest(archive)
        assert archived_manifest == quarantine_manifest
        archived_transition = read_record(
            archive / "transition.tsv", schema=TRANSITION_SCHEMA,
        )
        assert archived_transition == transition
        archived_pin = read_record(
            archive / "pins" / f"ready.{pin_token}.tsv", schema=PIN_SCHEMA,
        )
        assert archived_pin["token"] == token
        assert archived_pin["pin_token"] == pin_token
        receipts = sorted(state.glob(".round_lock_release.*.tsv"))
        assert receipts == [state / f".round_lock_release.{token}.tsv"]
        receipt = read_record(receipts[0], schema=RELEASE_SCHEMA)
        assert receipt["token"] == token, receipt
        assert receipt["round_barcode"] == round_barcode, receipt
        assert receipt["scope"] == scope, receipt
        assert receipt["reason"] == "dorado_only_early", receipt
        assert receipt["operation_token"] == transition["operation_token"], receipt
        assert receipt["lock_dev"] == transition["lock_dev"], receipt
        assert receipt["lock_ino"] == transition["lock_ino"], receipt

        events = [
            read_record(path, schema=EVENT_SCHEMA)
            for path in sorted((state / ".round_lock_events").glob("*.tsv"))
        ]
        release_events = [event for event in events if event["event"] == "release"]
        assert len(release_events) == 1, release_events
        assert release_events[0]["generation_token"] == token, release_events[0]
        assert release_events[0]["event_id"] == transition["operation_token"], (
            release_events[0]
        )
        assert release_events[0]["outcome"] == "dorado_only_early", (
            release_events[0]
        )
        later = subprocess.run(
            verifier_command, capture_output=True, check=False,
            env=perl_test_env(), timeout=5,
        )
        assert later.returncode == 0, later.stderr
        assert later.stdout == b"dorado_only_early\n"
        assert _tree_manifest(archive) == archived_manifest
        assert _tree_manifest(state) == after_owner
    finally:
        if not released and owner.poll() is None:
            try:
                _publish_release(release)
            except FileExistsError:
                pass
        if owner.poll() is None:
            try:
                owner.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                owner.kill()
                owner.communicate()
        if contender is not None and contender.poll() is None:
            contender.kill()
            contender.communicate()


@pytest.mark.parametrize(
    "failpoint",
    [
        "after-transition-receipt-before-event",
        "after-transition-outcome-before-archive",
        "after-terminal-archive-rename-before-sync",
    ],
)
def test_release_recovery_converges_at_every_terminal_boundary(
    tmp_path: Path,
    failpoint: str,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    round_barcode = "terminal_boundary"
    acquired = subprocess.run(
        _acquire_command(
            state, round_barcode=round_barcode, scope="dorado_only",
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    token, pin_token = parse_acquire_output(acquired.stdout)
    interrupted = subprocess.run(
        _generation_command(
            "early-release", state, token=token, pin_token=pin_token,
            round_barcode=round_barcode, scope="dorado_only",
        ),
        capture_output=True, check=False,
        env=perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-quarantine-rename"
        ),
        timeout=5,
    )
    assert interrupted.returncode != 0
    quarantines = list(state.glob(f"{LOCK_NAME}.release-*"))
    assert len(quarantines) == 1
    transition = read_record(
        quarantines[0] / "transition.tsv", schema=TRANSITION_SCHEMA,
    )
    original_manifest = _tree_manifest(quarantines[0])
    ready = tmp_path / f"{failpoint}.ready"
    release = tmp_path / f"{failpoint}.release"
    verifier_command = _generation_command(
        "verify-release", state, token=token,
        round_barcode=round_barcode, scope="dorado_only",
    )
    process = subprocess.Popen(
        verifier_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint,
            RTBIOSCAN_ROUND_LOCK_TEST_READY=str(ready),
            RTBIOSCAN_ROUND_LOCK_TEST_RELEASE=str(release),
        ),
    )
    _wait_for_pause(process, ready, failpoint)
    receipt_path = state / f".round_lock_release.{token}.tsv"
    archive = state / ".round_lock_archives" / (
        f"release-{transition['operation_token']}"
    )
    release_events_at_pause = [
        read_record(path, schema=EVENT_SCHEMA)
        for path in (state / ".round_lock_events").glob("*.tsv")
        if read_record(path, schema=EVENT_SCHEMA)["event"] == "release"
    ]
    assert os.path.lexists(receipt_path)
    if failpoint == "after-transition-receipt-before-event":
        assert os.path.lexists(quarantines[0])
        _assert_absent(archive)
        assert release_events_at_pause == []
    elif failpoint == "after-transition-outcome-before-archive":
        assert os.path.lexists(quarantines[0])
        _assert_absent(archive)
        assert len(release_events_at_pause) == 1
        assert release_events_at_pause[0]["event_id"] == transition["operation_token"]
    else:
        _assert_absent(quarantines[0])
        assert _tree_manifest(archive) == original_manifest
        assert len(release_events_at_pause) == 1
        assert release_events_at_pause[0]["event_id"] == transition["operation_token"]
    process.kill()
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode != 0, (stdout, stderr)

    recovered = subprocess.run(
        verifier_command, capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == b"dorado_only_early\n"
    assert not list(state.glob(f"{LOCK_NAME}.release-*"))
    assert _tree_manifest(archive) == original_manifest
    receipt = read_record(receipt_path, schema=RELEASE_SCHEMA)
    assert receipt["operation_token"] == transition["operation_token"]
    release_events = [
        read_record(path, schema=EVENT_SCHEMA)
        for path in (state / ".round_lock_events").glob("*.tsv")
        if read_record(path, schema=EVENT_SCHEMA)["event"] == "release"
    ]
    assert len(release_events) == 1
    assert release_events[0]["event_id"] == transition["operation_token"]
    stable = _tree_manifest(state)
    replayed = subprocess.run(
        verifier_command, capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert replayed.returncode == 0, replayed.stderr
    assert replayed.stdout == b"dorado_only_early\n"
    assert _tree_manifest(state) == stable
