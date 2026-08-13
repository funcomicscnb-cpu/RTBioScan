"""Replay durable acquire/pin requests whose stdout response was lost."""

from __future__ import annotations

import hashlib
import os
import signal
import stat
import subprocess
import time
from pathlib import Path

from tests.round_lock_test_utils import (
    EVENT_SCHEMA,
    GENERATION_SCHEMA,
    PIN_SCHEMA,
    assert_token,
    parse_acquire_output,
    perl_test_env,
    read_nofollow_bytes,
    read_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
LOCK_NAME = ".round_inflight.lockdir"


def _token(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _acquire_command(
    state: Path,
    *,
    generation_token: str | None = None,
    pin_token: str | None = None,
    round_barcode: str = "response_replay",
    scope: str = "full_round",
    owner_pid: int | None = None,
    stale_seconds: int = 300,
) -> list[str]:
    command = [
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
        str(stale_seconds),
        "--wait-seconds",
        "1",
    ]
    if generation_token is not None:
        command += ["--token", generation_token]
    if pin_token is not None:
        command += ["--pin-token", pin_token]
    return command


def _generation_command(
    action: str,
    state: Path,
    *,
    generation_token: str,
    pin_token: str | None = None,
    role: str | None = None,
    round_barcode: str = "response_replay",
    owner_pid: int | None = None,
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
        "full_round",
        "--token",
        generation_token,
        "--owner-pid",
        str(os.getpid() if owner_pid is None else owner_pid),
        "--stale-seconds",
        "300",
        "--wait-seconds",
        "1",
    ]
    if pin_token is not None:
        command += ["--pin-token", pin_token]
    if role is not None:
        command += ["--role", role]
    return command


def _run_with_broken_stdout(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """Start with no pipe reader, so a response write cannot succeed."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        process = subprocess.Popen(
            command,
            stdout=write_fd,
            stderr=subprocess.PIPE,
            env=perl_test_env(),
        )
    finally:
        os.close(write_fd)
    _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode != 0, stderr
    assert process.returncode != -signal.SIGPIPE, (process.returncode, stderr)
    assert b"ERROR: cannot write stdout:" in stderr, stderr
    return subprocess.CompletedProcess(command, process.returncode, b"", stderr)


def _ready_pin_paths(state: Path) -> list[Path]:
    return sorted((state / LOCK_NAME / "pins").glob("ready.*.tsv"))


def _tree_manifest(root: Path) -> tuple[tuple[object, ...], ...]:
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
                content,
            )
        )
    return tuple(manifest)


def _wait_for_file(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.is_file():
            return
        if process.poll() is not None:
            _stdout, stderr = process.communicate()
            raise AssertionError((process.returncode, stderr))
        time.sleep(0.01)
    process.kill()
    _stdout, stderr = process.communicate()
    raise AssertionError(("timed out waiting for failpoint", stderr))


def _sub_body(name: str) -> str:
    """Read one complete top-level Perl subroutine span."""
    source = SCRIPT.read_text(encoding="utf-8")
    marker = f"\nsub {name} {{\n"
    start = source.index(marker) + len(marker)
    end = source.index("\n}\n", start)
    return source[start:end]


def test_explicit_acquire_replay_adopts_event_namespace_before_reading() -> None:
    """Visible acquire evidence is authority only after its parent is durable."""
    body = _sub_body("validate_explicit_acquire_replay_event")
    validation = "if !@event_dir_st || -l _ || !-d _;"
    event_sync = "sync_directory($event_dir);"
    event_read = "my $event = record_for($path, \\@EVENT_ORDER);"

    assert body.count(event_sync) == 1, body
    assert body.index(validation) < body.index(event_sync) < body.index(event_read), (
        "explicit acquire replay did not adopt the acquire-event namespace "
        "before treating the visible event as durable"
    )


def test_explicit_acquire_tokens_replay_after_response_loss(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    generation_token = _token("response replay generation")
    pin_token = _token("response replay acquisition pin")
    command = _acquire_command(
        state,
        generation_token=generation_token,
        pin_token=pin_token,
    )

    failed_response = _run_with_broken_stdout(command)
    assert failed_response.returncode != 0
    lock = state / LOCK_NAME
    lock_entry = os.lstat(lock)
    generation_path = lock / "generation.tsv"
    generation = read_record(generation_path, schema=GENERATION_SCHEMA)
    ready_paths = _ready_pin_paths(state)
    assert len(ready_paths) == 1, ready_paths
    ready = read_record(ready_paths[0], schema=PIN_SCHEMA)
    events = [
        read_record(path, schema=EVENT_SCHEMA)
        for path in sorted((state / ".round_lock_events").glob("*.tsv"))
    ]
    assert len(events) == 1, events
    assert events[0]["event"] == "acquire", events[0]
    assert events[0]["outcome"] == "acquired", events[0]

    # These assertions are also the reachability control: the helper crossed
    # durable generation, ready-pin, and event publication before stdout failed.
    assert generation["token"] == generation_token, generation
    assert ready["pin_token"] == pin_token, ready
    assert ready_paths[0].name == f"ready.{pin_token}.tsv"
    assert generation["lock_dev"] == str(lock_entry.st_dev), generation
    assert generation["lock_ino"] == str(lock_entry.st_ino), generation
    assert events[0] == {
        "schema": "1",
        "event_id": pin_token,
        "generation_token": generation_token,
        "round_barcode": "response_replay",
        "scope": "full_round",
        "event": "acquire",
        "outcome": "acquired",
        "effective_ttl_seconds": "300",
        "event_epoch": generation["started_epoch"],
        "lock_dev": str(lock_entry.st_dev),
        "lock_ino": str(lock_entry.st_ino),
    }
    before = _tree_manifest(state)

    replay = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert replay.returncode == 0, replay.stderr
    assert parse_acquire_output(replay.stdout) == (generation_token, pin_token)
    assert _tree_manifest(state) == before


def test_explicit_acquire_does_not_repair_incomplete_generation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    generation_token = _token("incomplete acquire generation")
    pin_token = _token("incomplete acquire pin")
    ready = tmp_path / "generation-installed.ready"
    release = tmp_path / "generation-installed.release"
    env = perl_test_env()
    env.update(
        {
            "RTBIOSCAN_ROUND_LOCK_FAILPOINT": "after-generation-install",
            "RTBIOSCAN_ROUND_LOCK_TEST_READY": str(ready),
            "RTBIOSCAN_ROUND_LOCK_TEST_RELEASE": str(release),
            "RTBIOSCAN_ROUND_LOCK_TEST_TIMEOUT_SECONDS": "5",
        }
    )
    command = _acquire_command(
        state,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    _wait_for_file(ready, process)

    # Exact pause readiness plus independently parsed generation evidence is
    # the positive control that the interrupted process reached this boundary.
    generation = read_record(
        state / LOCK_NAME / "generation.tsv", schema=GENERATION_SCHEMA,
    )
    assert generation["token"] == generation_token, generation
    assert _ready_pin_paths(state) == []
    assert list((state / ".round_lock_events").glob("*.tsv")) == []
    process.kill()
    _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL, stderr

    before = _tree_manifest(state)
    replay = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert replay.returncode != 0, replay.stdout
    assert b"missing its durable process pin" in replay.stderr
    assert _tree_manifest(state) == before


def test_explicit_pin_token_replays_after_response_loss(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    acquired = subprocess.run(
        _acquire_command(state),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, acquisition_pin = parse_acquire_output(acquired.stdout)
    handoff = subprocess.run(
        _generation_command(
            "handoff",
            state,
            generation_token=generation_token,
            pin_token=acquisition_pin,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert handoff.returncode == 0, handoff.stderr

    pin_token = _token("response replay state writer")
    command = _generation_command(
        "pin",
        state,
        generation_token=generation_token,
        pin_token=pin_token,
        role="state_writer",
    )
    failed_response = _run_with_broken_stdout(command)
    assert failed_response.returncode != 0
    pins = state / LOCK_NAME / "pins"
    candidate_path = pins / f"candidate.{pin_token}.tsv"
    ready_path = pins / f"ready.{pin_token}.tsv"
    candidate_entry = os.lstat(candidate_path)
    ready_entry = os.lstat(ready_path)
    assert stat.S_ISREG(candidate_entry.st_mode), candidate_entry
    assert (candidate_entry.st_dev, candidate_entry.st_ino) == (
        ready_entry.st_dev,
        ready_entry.st_ino,
    )
    pin = read_record(ready_path, schema=PIN_SCHEMA)
    assert pin["token"] == generation_token, pin
    assert pin["pin_token"] == pin_token, pin
    assert pin["role"] == "state_writer", pin
    assert_token(pin["pin_token"])
    before = _tree_manifest(state)

    deadline = int(pin["created_epoch"]) + 1
    while int(time.time()) <= deadline:
        time.sleep(0.02)

    replay = subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout == f"{pin_token}\n".encode("ascii")
    assert _tree_manifest(state) == before
    assert (os.lstat(candidate_path).st_dev, os.lstat(candidate_path).st_ino) == (
        candidate_entry.st_dev,
        candidate_entry.st_ino,
    )
    assert (os.lstat(ready_path).st_dev, os.lstat(ready_path).st_ino) == (
        ready_entry.st_dev,
        ready_entry.st_ino,
    )


def test_explicit_pin_token_cannot_resurrect_an_unpinned_request(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    acquired = subprocess.run(
        _acquire_command(state), capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, acquisition_pin = parse_acquire_output(acquired.stdout)
    handoff = subprocess.run(
        _generation_command(
            "handoff", state, generation_token=generation_token,
            pin_token=acquisition_pin,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert handoff.returncode == 0, handoff.stderr

    pin_token = _token("ended explicit pin")
    pin_command = _generation_command(
        "pin", state, generation_token=generation_token,
        pin_token=pin_token, role="state_writer",
    )
    pinned = subprocess.run(
        pin_command, capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert pinned.returncode == 0, pinned.stderr
    candidate = state / LOCK_NAME / "pins" / f"candidate.{pin_token}.tsv"
    ready = state / LOCK_NAME / "pins" / f"ready.{pin_token}.tsv"
    assert os.path.samefile(candidate, ready)

    unpinned = subprocess.run(
        _generation_command(
            "unpin", state, generation_token=generation_token,
            pin_token=pin_token,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert unpinned.returncode == 0, unpinned.stderr
    assert candidate.is_file()
    assert not ready.exists()
    before = _tree_manifest(state)

    replay = subprocess.run(
        pin_command, capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert replay.returncode != 0, replay.stdout
    assert b"immutable pin candidate has no live ready pin" in replay.stderr
    assert _tree_manifest(state) == before


def test_implicit_duplicate_pin_requests_still_mint_distinct_tokens(
    tmp_path: Path,
) -> None:
    """Replay is selected by an exact caller token, never by role/PID scanning."""
    state = tmp_path / "state"
    state.mkdir()
    acquired = subprocess.run(
        _acquire_command(state), capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, acquisition_pin = parse_acquire_output(acquired.stdout)
    handoff = subprocess.run(
        _generation_command(
            "handoff", state, generation_token=generation_token,
            pin_token=acquisition_pin,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert handoff.returncode == 0, handoff.stderr
    command = _generation_command(
        "pin", state, generation_token=generation_token, role="state_writer",
    )
    first = subprocess.run(
        command, capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    second = subprocess.run(
        command, capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_token = first.stdout.strip().decode("ascii")
    second_token = second.stdout.strip().decode("ascii")
    assert_token(first_token)
    assert_token(second_token)
    assert first_token != second_token


def test_explicit_acquire_requires_one_distinct_token_pair_without_mutation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    generation_token = _token("paired acquire generation")
    pin_token = _token("paired acquire pin")
    before = _tree_manifest(state)
    commands = [
        _acquire_command(state, generation_token=generation_token),
        _acquire_command(state, pin_token=pin_token),
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=generation_token,
        ),
    ]
    for command in commands:
        rejected = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=perl_test_env(),
            timeout=5,
        )
        assert rejected.returncode != 0, rejected.stdout
        assert rejected.stdout == b""
        assert _tree_manifest(state) == before


def test_explicit_acquire_replay_rejects_changed_request_without_mutation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    generation_token = _token("stable acquire generation")
    pin_token = _token("stable acquire pin")
    original = _acquire_command(
        state,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    acquired = subprocess.run(
        original,
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    assert parse_acquire_output(acquired.stdout) == (generation_token, pin_token)
    before = _tree_manifest(state)

    changed_commands = [
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=pin_token,
            round_barcode="changed_round",
        ),
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=pin_token,
            scope="dorado_only",
        ),
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=pin_token,
            owner_pid=os.getpid() + 1,
        ),
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=pin_token,
            stale_seconds=301,
        ),
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=_token("different stable acquire pin"),
        ),
    ]
    for command in changed_commands:
        rejected = subprocess.run(
            command,
            capture_output=True,
            check=False,
            env=perl_test_env(),
            timeout=5,
        )
        assert rejected.returncode != 0, rejected.stdout
        assert b"explicit acquire" in rejected.stderr
        assert _tree_manifest(state) == before


def test_explicit_acquire_generation_token_is_single_use_across_pin_tokens(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    generation_token = _token("single-use acquire generation")
    first_pin = _token("single-use acquire first pin")
    acquired = subprocess.run(
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=first_pin,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    assert parse_acquire_output(acquired.stdout) == (generation_token, first_pin)
    event_path = (
        state / ".round_lock_events"
        / f"{generation_token}.acquire.{first_pin}.tsv"
    )
    event = read_record(event_path, schema=EVENT_SCHEMA)
    assert event["generation_token"] == generation_token, event

    # Model a completed stopped-world preservation: canonical ownership is no
    # longer installed, while the durable history remains outside the tree.
    preserved = state / f"{LOCK_NAME}.preserved"
    os.rename(state / LOCK_NAME, preserved)
    before = _tree_manifest(state)
    second_pin = _token("single-use acquire second pin")
    reused = subprocess.run(
        _acquire_command(
            state,
            generation_token=generation_token,
            pin_token=second_pin,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert reused.returncode != 0, reused.stdout
    assert b"generation token has durable acquire history" in reused.stderr
    assert _tree_manifest(state) == before


def test_explicit_pin_replay_rejects_changed_identity_without_mutation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    acquired = subprocess.run(
        _acquire_command(state), capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, acquisition_pin = parse_acquire_output(acquired.stdout)
    handoff = subprocess.run(
        _generation_command(
            "handoff", state, generation_token=generation_token,
            pin_token=acquisition_pin,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert handoff.returncode == 0, handoff.stderr
    pin_token = _token("stable explicit pin")
    original = _generation_command(
        "pin", state, generation_token=generation_token,
        pin_token=pin_token, role="state_writer",
    )
    pinned = subprocess.run(
        original, capture_output=True, check=False,
        env=perl_test_env(), timeout=5,
    )
    assert pinned.returncode == 0, pinned.stderr
    assert pinned.stdout == f"{pin_token}\n".encode("ascii")
    before = _tree_manifest(state)

    for command in (
        _generation_command(
            "pin", state, generation_token=generation_token,
            pin_token=pin_token, role="different_role",
        ),
        _generation_command(
            "pin", state, generation_token=generation_token,
            pin_token=pin_token, role="state_writer", owner_pid=os.getpid() + 1,
        ),
    ):
        rejected = subprocess.run(
            command, capture_output=True, check=False,
            env=perl_test_env(), timeout=5,
        )
        assert rejected.returncode != 0, rejected.stdout
        assert b"immutable pin candidate conflicts" in rejected.stderr
        assert _tree_manifest(state) == before
