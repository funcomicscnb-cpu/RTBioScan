"""Crash-boundary policy for round-lock transition temporary records.

Transition publication uses a synced temporary inode and a hard link to the
canonical ``transition.tsv`` name.  These tests pin the authority boundary:

* a temporary name alone is inert, even when it contains a complete record;
* once ``transition.tsv`` exists, that installed record is the authority;
* recovery never glob-removes ``.transition-*.tmp`` records, because a name
  can belong to an installer whose liveness has not been established.

The pause handshake is intentionally positive.  Merely setting an unknown
failpoint used to be a no-op, which could make a crash test exercise an
uninterrupted command.  Each test requires the exact boundary name to be
durably published while the helper is still running before sending SIGKILL.
"""

from __future__ import annotations

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
    REVOCATION_SCHEMA,
    TRANSITION_SCHEMA,
    MalformedRecord,
    assert_token,
    parse_acquire_output,
    perl_test_env,
    read_nofollow_bytes,
    read_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
LOCK_NAME = ".round_inflight.lockdir"
DEAD_OWNER_PID = 99_999_999
OWNER_ROUND = "orphan_owner"
REPLACEMENT_ROUND = "orphan_replacement"


def _acquire_argv(
    state: Path,
    *,
    round_barcode: str,
    owner_pid: int,
    wait_seconds: int = 2,
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
        str(owner_pid),
        "--stale-seconds",
        "300",
        "--wait-seconds",
        str(wait_seconds),
    ]


def _acquire(
    state: Path,
    *,
    round_barcode: str,
    owner_pid: int,
) -> tuple[str, str]:
    result = subprocess.run(
        _acquire_argv(
            state,
            round_barcode=round_barcode,
            owner_pid=owner_pid,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
    )
    assert result.returncode == 0, result.stderr
    return parse_acquire_output(result.stdout)


def _dead_generation(state: Path) -> tuple[str, str]:
    """Install a valid generation whose local owner is deterministically dead."""
    try:
        os.kill(DEAD_OWNER_PID, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError(
            f"test owner PID unexpectedly names a live process: {DEAD_OWNER_PID}"
        )
    return _acquire(
        state,
        round_barcode=OWNER_ROUND,
        owner_pid=DEAD_OWNER_PID,
    )


def _path_kind(path: Path) -> str:
    mode = os.lstat(path).st_mode
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISLNK(mode):
        return "symlink"
    return f"mode:{mode:o}"


def _namespace(root: Path) -> dict[str, str]:
    """Return every namespace entry without following a single symlink."""
    found: dict[str, str] = {}

    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in sorted(entries, key=lambda item: item.name):
                path = Path(entry.path)
                relative = path.relative_to(root).as_posix()
                kind = _path_kind(path)
                found[relative] = kind
                if kind == "dir":
                    visit(path)

    visit(root)
    return found


def _generation_namespace(prefix: str, pin_token: str) -> dict[str, str]:
    return {
        prefix: "dir",
        f"{prefix}/generation.tsv": "file",
        f"{prefix}/pins": "dir",
        f"{prefix}/pins/candidate.{pin_token}.tsv": "file",
        f"{prefix}/pins/ready.{pin_token}.tsv": "file",
    }


def _validated_events(state: Path) -> list[tuple[str, dict[str, str]]]:
    events_dir = state / ".round_lock_events"
    paths = sorted(events_dir.iterdir())
    records: list[tuple[str, dict[str, str]]] = []
    for path in paths:
        record = read_record(path, schema=EVENT_SCHEMA)
        expected_name = ".".join(
            (record["generation_token"], record["event"], record["event_id"])
        ) + ".tsv"
        assert path.name == expected_name, (path.name, expected_name)
        records.append((f".round_lock_events/{path.name}", record))
    return records


def _assert_generation(
    path: Path,
    *,
    token: str,
    pin_token: str,
    round_barcode: str,
) -> None:
    directory = os.lstat(path)
    assert stat.S_ISDIR(directory.st_mode), directory
    generation = read_record(path / "generation.tsv", schema=GENERATION_SCHEMA)
    assert generation["token"] == token, generation
    assert generation["round_barcode"] == round_barcode, generation
    assert generation["scope"] == "full_round", generation
    assert generation["lock_dev"] == str(directory.st_dev), generation
    assert generation["lock_ino"] == str(directory.st_ino), generation

    candidate = path / "pins" / f"candidate.{pin_token}.tsv"
    ready = path / "pins" / f"ready.{pin_token}.tsv"
    candidate_stat = os.lstat(candidate)
    ready_stat = os.lstat(ready)
    assert stat.S_ISREG(candidate_stat.st_mode), candidate_stat
    assert stat.S_ISREG(ready_stat.st_mode), ready_stat
    assert (candidate_stat.st_dev, candidate_stat.st_ino) == (
        ready_stat.st_dev,
        ready_stat.st_ino,
    )
    assert read_nofollow_bytes(candidate) == read_nofollow_bytes(ready)
    pin = read_record(ready, schema=PIN_SCHEMA)
    assert pin["token"] == token, pin
    assert pin["pin_token"] == pin_token, pin
    assert pin["round_barcode"] == round_barcode, pin
    assert pin["scope"] == "full_round", pin
    assert pin["role"] == "fast_acquisition", pin
    assert pin["lock_dev"] == str(directory.st_dev), pin
    assert pin["lock_ino"] == str(directory.st_ino), pin


def _assert_active_namespace(
    state: Path,
    *,
    generation_token: str,
    pin_token: str,
    transition_names: set[str],
) -> None:
    events = _validated_events(state)
    assert len(events) == 1, events
    event_path, event = events[0]
    assert (
        event["generation_token"],
        event["round_barcode"],
        event["event"],
        event["outcome"],
    ) == (generation_token, OWNER_ROUND, "acquire", "acquired"), event

    expected = {".round_lock_events": "dir", event_path: "file"}
    expected.update(_generation_namespace(LOCK_NAME, pin_token))
    expected.update(
        {f"{LOCK_NAME}/{name}": "file" for name in transition_names}
    )
    assert _namespace(state) == expected


def _start_paused_reclaimer(
    state: Path,
    gate_dir: Path,
    failpoint: str,
) -> subprocess.Popen[bytes]:
    ready = gate_dir / f"{failpoint}.ready"
    release = gate_dir / f"{failpoint}.release"
    env = perl_test_env(
        RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint,
        RTBIOSCAN_ROUND_LOCK_TEST_READY=str(ready),
        RTBIOSCAN_ROUND_LOCK_TEST_RELEASE=str(release),
        RTBIOSCAN_ROUND_LOCK_TEST_TIMEOUT_SECONDS="30",
    )
    process = subprocess.Popen(
        _acquire_argv(
            state,
            round_barcode=REPLACEMENT_ROUND,
            owner_pid=os.getpid(),
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.lstat(ready)
        except FileNotFoundError:
            if process.poll() is not None:
                break
            time.sleep(0.01)
            continue
        break

    try:
        try:
            os.lstat(ready)
        except FileNotFoundError:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                f"failpoint {failpoint!r} did not publish readiness; "
                f"rc={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
            )
        assert read_nofollow_bytes(ready) == f"{failpoint}\n".encode("ascii")
        assert process.poll() is None, (
            failpoint,
            process.returncode,
            process.communicate(timeout=1),
        )
        with pytest.raises(FileNotFoundError):
            os.lstat(release)
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        raise
    return process


def _kill_at_gate(process: subprocess.Popen[bytes]) -> None:
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


def _assert_reclaim_transition(
    path: Path,
    *,
    owner_token: str,
    directory: Path,
) -> dict[str, str]:
    transition = read_record(path, schema=TRANSITION_SCHEMA)
    directory_stat = os.lstat(directory)
    assert transition["schema"] == "1", transition
    assert transition["action"] == "reclaim", transition
    assert_token(transition["operation_token"])
    assert transition["owner_token"] == owner_token, transition
    assert transition["round_barcode"] == OWNER_ROUND, transition
    assert transition["scope"] == "full_round", transition
    assert transition["allowed_pin_token"] == "none", transition
    assert transition["effective_ttl_seconds"] == "300", transition
    assert transition["lock_dev"] == str(directory_stat.st_dev), transition
    assert transition["lock_ino"] == str(directory_stat.st_ino), transition
    assert transition["started_epoch"].isdigit(), transition
    return transition


def _recover_and_assert_exact_namespace(
    state: Path,
    *,
    old_token: str,
    old_pin: str,
    old_directory_identity: tuple[int, int],
    orphan_name: str | None,
    orphan_bytes: bytes | None,
) -> tuple[Path, dict[str, str]]:
    replacement_token, replacement_pin = _acquire(
        state,
        round_barcode=REPLACEMENT_ROUND,
        owner_pid=os.getpid(),
    )

    revocation_path = state / f".round_lock_revocation.{old_token}.tsv"
    revocation = read_record(revocation_path, schema=REVOCATION_SCHEMA)
    assert revocation["token"] == old_token, revocation
    assert revocation["round_barcode"] == OWNER_ROUND, revocation
    assert revocation["scope"] == "full_round", revocation
    assert revocation["outcome"] == "revoked", revocation
    assert_token(revocation["operation_token"])

    archive = state / ".round_lock_archives" / (
        f"reclaim-{revocation['operation_token']}"
    )
    archive_stat = os.lstat(archive)
    assert stat.S_ISDIR(archive_stat.st_mode), archive_stat
    assert (archive_stat.st_dev, archive_stat.st_ino) == old_directory_identity
    transition = _assert_reclaim_transition(
        archive / "transition.tsv",
        owner_token=old_token,
        directory=archive,
    )
    assert transition["operation_token"] == revocation["operation_token"], (
        transition,
        revocation,
    )

    if orphan_name is not None:
        orphan = archive / orphan_name
        assert read_nofollow_bytes(orphan) == orphan_bytes

    _assert_generation(
        archive,
        token=old_token,
        pin_token=old_pin,
        round_barcode=OWNER_ROUND,
    )
    _assert_generation(
        state / LOCK_NAME,
        token=replacement_token,
        pin_token=replacement_pin,
        round_barcode=REPLACEMENT_ROUND,
    )

    events = _validated_events(state)
    assert len(events) == 3, events
    assert {
        (
            record["generation_token"],
            record["round_barcode"],
            record["event"],
            record["outcome"],
        )
        for _path, record in events
    } == {
        (old_token, OWNER_ROUND, "acquire", "acquired"),
        (old_token, OWNER_ROUND, "reclaim", "quarantined"),
        (replacement_token, REPLACEMENT_ROUND, "acquire", "acquired"),
    }

    expected = {
        ".round_lock_events": "dir",
        f".round_lock_revocation.{old_token}.tsv": "file",
    }
    expected.update({path: "file" for path, _record in events})
    expected.update(_generation_namespace(LOCK_NAME, replacement_pin))
    archive_relative = archive.relative_to(state).as_posix()
    expected[".round_lock_archives"] = "dir"
    expected.update(_generation_namespace(archive_relative, old_pin))
    expected[f"{archive_relative}/transition.tsv"] = "file"
    if orphan_name is not None:
        expected[f"{archive_relative}/{orphan_name}"] = "file"
    assert _namespace(state) == expected
    return archive, transition


def test_complete_temporary_record_is_inert_and_is_not_glob_cleaned(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    old_token, old_pin = _dead_generation(state)
    lock_dir = state / LOCK_NAME
    lock_stat = os.lstat(lock_dir)

    failpoint = "after-transition-temp-write-before-link"
    process = _start_paused_reclaimer(state, tmp_path, failpoint)
    temporary_names = {
        path.name
        for path in lock_dir.iterdir()
        if path.name.startswith(".transition-") and path.name.endswith(".tmp")
    }
    assert len(temporary_names) == 1, temporary_names
    temporary_name = temporary_names.pop()
    temporary = lock_dir / temporary_name
    temporary_stat = os.lstat(temporary)
    assert stat.S_ISREG(temporary_stat.st_mode), temporary_stat
    assert temporary_stat.st_nlink == 1, temporary_stat
    orphan_transition = _assert_reclaim_transition(
        temporary,
        owner_token=old_token,
        directory=lock_dir,
    )
    assert temporary_name == (
        f".transition-{orphan_transition['operation_token']}.tmp"
    )
    orphan_bytes = read_nofollow_bytes(temporary)
    _assert_active_namespace(
        state,
        generation_token=old_token,
        pin_token=old_pin,
        transition_names={temporary_name},
    )
    _kill_at_gate(process)

    archive, installed_transition = _recover_and_assert_exact_namespace(
        state,
        old_token=old_token,
        old_pin=old_pin,
        old_directory_identity=(lock_stat.st_dev, lock_stat.st_ino),
        orphan_name=temporary_name,
        orphan_bytes=orphan_bytes,
    )
    assert installed_transition["operation_token"] != orphan_transition[
        "operation_token"
    ]
    carried = os.lstat(archive / temporary_name)
    assert (carried.st_dev, carried.st_ino) == (
        temporary_stat.st_dev,
        temporary_stat.st_ino,
    )
    assert carried.st_nlink == 1, carried


def test_canonical_link_is_authority_while_its_temporary_name_is_preserved(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    old_token, old_pin = _dead_generation(state)
    lock_dir = state / LOCK_NAME
    lock_stat = os.lstat(lock_dir)

    failpoint = "after-transition-link-before-temp-unlink"
    process = _start_paused_reclaimer(state, tmp_path, failpoint)
    transition_path = lock_dir / "transition.tsv"
    transition = _assert_reclaim_transition(
        transition_path,
        owner_token=old_token,
        directory=lock_dir,
    )
    temporary_name = f".transition-{transition['operation_token']}.tmp"
    temporary = lock_dir / temporary_name
    transition_stat = os.lstat(transition_path)
    temporary_stat = os.lstat(temporary)
    assert (transition_stat.st_dev, transition_stat.st_ino) == (
        temporary_stat.st_dev,
        temporary_stat.st_ino,
    )
    assert transition_stat.st_nlink == 2, transition_stat
    assert temporary_stat.st_nlink == 2, temporary_stat
    transition_bytes = read_nofollow_bytes(transition_path)
    assert read_nofollow_bytes(temporary) == transition_bytes
    _assert_active_namespace(
        state,
        generation_token=old_token,
        pin_token=old_pin,
        transition_names={"transition.tsv", temporary_name},
    )
    _kill_at_gate(process)

    archive, installed_transition = _recover_and_assert_exact_namespace(
        state,
        old_token=old_token,
        old_pin=old_pin,
        old_directory_identity=(lock_stat.st_dev, lock_stat.st_ino),
        orphan_name=temporary_name,
        orphan_bytes=transition_bytes,
    )
    assert installed_transition == transition
    installed_stat = os.lstat(archive / "transition.tsv")
    carried_stat = os.lstat(archive / temporary_name)
    assert (installed_stat.st_dev, installed_stat.st_ino) == (
        transition_stat.st_dev,
        transition_stat.st_ino,
    )
    assert (carried_stat.st_dev, carried_stat.st_ino) == (
        transition_stat.st_dev,
        transition_stat.st_ino,
    )
    assert installed_stat.st_nlink == 2, installed_stat
    assert carried_stat.st_nlink == 2, carried_stat


def test_canonical_only_transition_remains_the_authority(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    old_token, old_pin = _dead_generation(state)
    lock_dir = state / LOCK_NAME
    lock_stat = os.lstat(lock_dir)

    failpoint = "after-transition-temp-unlink-before-sync"
    process = _start_paused_reclaimer(state, tmp_path, failpoint)
    transition_path = lock_dir / "transition.tsv"
    transition = _assert_reclaim_transition(
        transition_path,
        owner_token=old_token,
        directory=lock_dir,
    )
    transition_stat = os.lstat(transition_path)
    assert transition_stat.st_nlink == 1, transition_stat
    _assert_active_namespace(
        state,
        generation_token=old_token,
        pin_token=old_pin,
        transition_names={"transition.tsv"},
    )
    _kill_at_gate(process)

    archive, installed_transition = _recover_and_assert_exact_namespace(
        state,
        old_token=old_token,
        old_pin=old_pin,
        old_directory_identity=(lock_stat.st_dev, lock_stat.st_ino),
        orphan_name=None,
        orphan_bytes=None,
    )
    assert installed_transition == transition
    installed_stat = os.lstat(archive / "transition.tsv")
    assert (installed_stat.st_dev, installed_stat.st_ino) == (
        transition_stat.st_dev,
        transition_stat.st_ino,
    )
    assert installed_stat.st_nlink == 1, installed_stat


def test_killed_partial_temporary_record_is_inert_and_preserved(
    tmp_path: Path,
) -> None:
    """A real mid-write SIGKILL leaves inert bytes that recovery preserves."""
    state = tmp_path / "state"
    old_token, old_pin = _dead_generation(state)
    lock_dir = state / LOCK_NAME
    lock_stat = os.lstat(lock_dir)

    failpoint = "after-transition-temp-partial-write"
    process = _start_paused_reclaimer(state, tmp_path, failpoint)
    temporary_names = {
        path.name
        for path in lock_dir.iterdir()
        if path.name.startswith(".transition-") and path.name.endswith(".tmp")
    }
    assert len(temporary_names) == 1, temporary_names
    temporary_name = temporary_names.pop()
    orphan_operation = temporary_name.removeprefix(".transition-").removesuffix(
        ".tmp"
    )
    assert_token(orphan_operation)
    temporary = lock_dir / temporary_name
    temporary_stat = os.lstat(temporary)
    assert stat.S_ISREG(temporary_stat.st_mode), temporary_stat
    assert temporary_stat.st_nlink == 1, temporary_stat
    partial = read_nofollow_bytes(temporary)
    assert partial, partial
    with pytest.raises(MalformedRecord):
        read_record(temporary, schema=TRANSITION_SCHEMA)
    _assert_active_namespace(
        state,
        generation_token=old_token,
        pin_token=old_pin,
        transition_names={temporary_name},
    )
    _kill_at_gate(process)

    archive, installed_transition = _recover_and_assert_exact_namespace(
        state,
        old_token=old_token,
        old_pin=old_pin,
        old_directory_identity=(lock_stat.st_dev, lock_stat.st_ino),
        orphan_name=temporary_name,
        orphan_bytes=partial,
    )
    assert installed_transition["operation_token"] != orphan_operation
    carried = os.lstat(archive / temporary_name)
    assert (carried.st_dev, carried.st_ino) == (
        temporary_stat.st_dev,
        temporary_stat.st_ino,
    )
    assert carried.st_nlink == 1, carried
