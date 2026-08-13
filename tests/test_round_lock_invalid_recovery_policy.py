"""Fail-closed policy for invalid round-lock snapshots.

Invalid canonical and recovery snapshots are evidence for an operator, not
authority for automatic recovery.  Every contender must therefore leave the
whole namespace untouched until an explicitly confirmed, identity-bound
operator action quarantines the invalid canonical entry.  Valid token-bound
transitions remain automatically recoverable as the positive control.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import stat
import struct
import subprocess
import time
from pathlib import Path

import pytest

from tests.round_lock_test_utils import (
    GENERATION_SCHEMA,
    MARKER_SCHEMA,
    OPERATOR_EVENT_SCHEMA,
    PIN_SCHEMA,
    RELEASE_SCHEMA,
    TRANSITION_SCHEMA,
    MalformedRecord,
    RecordSchema,
    assert_exact_error_line,
    parse_acquire_output,
    perl_test_env,
    read_nofollow_bytes,
    read_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
RUNBOOK = REPO_ROOT / "docs" / "internal" / "round_lock_operator_recovery.md"
LOCK_NAME = ".round_inflight.lockdir"
EVENTS_NAME = ".round_lock_events"


def _perl_sub_source(name: str) -> str:
    """Return one complete top-level Perl sub span, up to its successor."""
    source = SCRIPT.read_text(encoding="utf-8")
    starts = list(
        re.finditer(
            rf"(?m)^sub {re.escape(name)} \{{(?:\n|$)",
            source,
        )
    )
    assert len(starts) == 1, f"expected one top-level Perl sub {name!r}"
    start = starts[0].start()
    successor = re.search(
        r"(?m)^sub [A-Za-z_][A-Za-z0-9_]* \{(?:\n|$)",
        source[starts[0].end() :],
    )
    end = len(source) if successor is None else starts[0].end() + successor.start()
    return source[start:end]


def test_operator_pending_enumeration_adopts_its_directory_first() -> None:
    body = _perl_sub_source("operator_pending_tokens")
    path_assignment = body.index("my $path = operator_pending_dir($state_dir);")
    directory_sync = body.index("sync_directory($path);")
    directory_open = body.index("opendir(my $dh, $path)")
    name_enumeration = body.index("readdir($dh)")
    assert path_assignment < directory_sync < directory_open < name_enumeration


def test_operator_manifest_adopts_each_directory_before_enumeration() -> None:
    body = _perl_sub_source("operator_directory_manifest_add")
    directory_sync = body.index("sync_directory($directory);")
    directory_open = body.index("opendir(my $dh, $directory)")
    name_enumeration = body.index("readdir($dh)")
    recursive_descent = body.index(
        "operator_directory_manifest_add($digest, $path, $relative)"
    )
    assert directory_sync < directory_open < name_enumeration < recursive_descent


def _token(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def test_operator_recovery_runbook_pins_the_required_safety_contract() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    for required in (
        "Stop every RTBioScan, Nextflow, scheduler, and helper process",
        "operator-quarantine-invalid",
        "--expected-lock-dev",
        "--expected-lock-ino",
        "--operation-token",
        "--source-name",
        "--operator-label",
        "--reason",
        "--confirm-invalid-snapshot",
        "operator-quarantine-unrecoverable",
        "--expected-generation-token",
        "--confirm-stopped-world",
        "--confirm-abandon-generation",
        "zero-second lease",
        "dead PID",
        "recursive no-follow digest",
        "External-finalization eligibility is deliberately exact",
        "directory-valued `round_inflight.txt`",
        "before any writer resumes",
        "Silent activation over an existing state directory is not acceptable",
        "schema-1 pending operation",
        "There is no silent dual-parser migration",
        "Tokenless",
        "cannot recover a lost response",
        "Production launchers must clear every",
        ".round_lock_operator_pending",
        "same operation token, source name, device, inode",
        "run the exact command once more",
        "It does not write a release receipt",
        "or a revocation",
        "authenticate who created",
        "every runtime writer",
    ):
        assert required in text


def _write_record(
    path: Path,
    values: dict[str, str],
    *,
    schema: RecordSchema,
) -> dict[str, str]:
    assert set(values) == set(schema.fields), (values, schema)
    body = b"".join(
        key.encode("utf-8") + b"\t" + values[key].encode("utf-8") + b"\n"
        for key in schema.fields
    )
    path.write_bytes(
        body + b"record_sha256\t" + hashlib.sha256(body).hexdigest().encode() + b"\n"
    )
    parsed = read_record(path, schema=schema)
    assert parsed == values
    return parsed


def _new_state(path: Path) -> Path:
    path.mkdir()
    (path / EVENTS_NAME).mkdir(mode=0o700)
    return path


def _semantic_invalid_generation(directory: Path, token: str) -> dict[str, str]:
    entry = os.lstat(directory)
    values = {
        "schema": "1",
        "token": token,
        "round_barcode": "run_invalid",
        "scope": "dorado_only",
        "pid": str(os.getpid()),
        "host": "fixture.invalid",
        "process_start": "unavailable",
        "started_epoch": "1",
        "effective_ttl_seconds": "1",
        "lock_dev": str(entry.st_dev),
        # The record parses and has the exact generation field set, but it is
        # not a snapshot of this directory.
        "lock_ino": str(entry.st_ino + 1),
    }
    parsed = _write_record(
        directory / "generation.tsv", values, schema=GENERATION_SCHEMA
    )
    assert parsed["lock_dev"] == str(entry.st_dev)
    assert parsed["lock_ino"] != str(entry.st_ino)
    return parsed


def _parse_invalid_generation(directory: Path) -> None:
    generation = directory / "generation.tsv"
    generation.write_bytes(
        b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n"
    )
    with pytest.raises(MalformedRecord, match="mismatch"):
        read_record(generation, schema=GENERATION_SCHEMA)


def _transition(
    directory: Path,
    *,
    action: str,
    operation_token: str,
    owner_token: str,
    round_barcode: str,
    scope: str,
    reason: str,
    allowed_pin_token: str,
) -> dict[str, str]:
    entry = os.lstat(directory)
    return _write_record(
        directory / "transition.tsv",
        {
            "schema": "1",
            "action": action,
            "operation_token": operation_token,
            "owner_token": owner_token,
            "round_barcode": round_barcode,
            "scope": scope,
            "reason": reason,
            "effective_ttl_seconds": "1",
            "lock_dev": str(entry.st_dev),
            "lock_ino": str(entry.st_ino),
            "allowed_pin_token": allowed_pin_token,
            "started_epoch": "1",
        },
        schema=TRANSITION_SCHEMA,
    )


def _backdate_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        os.utime(path, (1, 1), follow_symlinks=False)
    os.utime(root, (1, 1), follow_symlinks=False)


def _invalid_state(tmp_path: Path, case: str) -> Path:
    state = _new_state(tmp_path / "state")
    owner_token = _token(f"{case}:owner")
    operation_token = _token(f"{case}:operation")

    if case == "invalid-reclaim-quarantine":
        target = state / f"{LOCK_NAME}.reclaim-{operation_token}"
        target.mkdir(mode=0o700)
        _transition(
            target,
            action="reclaim",
            operation_token=operation_token,
            owner_token="legacy",
            round_barcode="legacy",
            scope="legacy",
            reason="legacy snapshot requires operator review",
            allowed_pin_token="none",
        )
    elif case == "invalid-release-quarantine":
        target = state / f"{LOCK_NAME}.release-{operation_token}"
        target.mkdir(mode=0o700)
        _semantic_invalid_generation(target, owner_token)
        _transition(
            target,
            action="release",
            operation_token=operation_token,
            owner_token=owner_token,
            round_barcode="run_invalid",
            scope="dorado_only",
            reason="dorado_only_early",
            allowed_pin_token=_token(f"{case}:pin"),
        )
    else:
        target = state / LOCK_NAME
        target.mkdir(mode=0o700)
        if case == "generation-absent-canonical":
            with pytest.raises(FileNotFoundError):
                os.lstat(target / "generation.tsv")
        elif case == "parse-invalid-canonical":
            _parse_invalid_generation(target)
        elif case in {
            "semantic-invalid-canonical",
            "invalid-canonical-with-legacy-transition",
        }:
            _semantic_invalid_generation(target, owner_token)
        else:  # pragma: no cover - the parametrization is the exhaustive list
            raise AssertionError(f"unknown invalid-snapshot fixture: {case}")

        if case == "invalid-canonical-with-legacy-transition":
            _transition(
                target,
                action="reclaim",
                operation_token=operation_token,
                owner_token="legacy",
                round_barcode="legacy",
                scope="legacy",
                reason="legacy snapshot requires operator review",
                allowed_pin_token="none",
            )

    _backdate_tree(target)
    return state


def _namespace(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture every name, type, identity, link count, mode, and file byte."""
    entries = [root, *sorted(root.rglob("*"), key=lambda path: path.as_posix())]
    captured: list[tuple[object, ...]] = []
    for path in entries:
        entry = os.lstat(path)
        relative = "." if path == root else path.relative_to(root).as_posix()
        if stat.S_ISREG(entry.st_mode):
            kind = "file"
            payload: object = read_nofollow_bytes(path)
        elif stat.S_ISDIR(entry.st_mode):
            kind = "directory"
            payload = None
        elif stat.S_ISLNK(entry.st_mode):
            kind = "symlink"
            payload = os.readlink(path)
        else:
            kind = "other"
            payload = None
        captured.append(
            (
                relative,
                kind,
                entry.st_mode,
                entry.st_uid,
                entry.st_gid,
                entry.st_dev,
                entry.st_ino,
                entry.st_nlink,
                entry.st_size,
                entry.st_mtime_ns,
                payload,
            )
        )
    return tuple(captured)


def _acquire_command(
    state: Path,
    *,
    round_barcode: str,
    scope: str = "full_round",
    stale_seconds: int = 1,
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
        str(os.getpid()),
        "--stale-seconds",
        str(stale_seconds),
        "--wait-seconds",
        "1",
    ]


def _run_acquire(
    state: Path,
    *,
    round_barcode: str,
    scope: str = "full_round",
    stale_seconds: int = 1,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        _acquire_command(
            state,
            round_barcode=round_barcode,
            scope=scope,
            stale_seconds=stale_seconds,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )


@pytest.mark.parametrize(
    "case",
    [
        "generation-absent-canonical",
        "parse-invalid-canonical",
        "semantic-invalid-canonical",
        "invalid-canonical-with-legacy-transition",
        "invalid-reclaim-quarantine",
        "invalid-release-quarantine",
    ],
)
def test_invalid_snapshots_require_operator_recovery_and_are_non_mutating(
    tmp_path: Path,
    case: str,
) -> None:
    """Neither the first nor a later contender may consume invalid evidence."""
    state = _invalid_state(tmp_path, case)
    before = _namespace(state)
    results: list[subprocess.CompletedProcess[bytes]] = []
    after: list[tuple[tuple[object, ...], ...]] = []

    # Run both attempts before asserting so a failure still proves that the
    # second contender was exercised, rather than stopping at the first one.
    for index in range(2):
        results.append(_run_acquire(state, round_barcode=f"contender_{index}"))
        after.append(_namespace(state))

    for result in results:
        assert result.returncode != 0, result.stdout
        assert result.stdout == b"", result.stdout
    assert after == [before, before]


def test_valid_token_bound_transition_still_recovers_automatically(
    tmp_path: Path,
) -> None:
    """Fail-closed invalid recovery must not disable authenticated replay."""
    state = _new_state(tmp_path / "state")
    acquired = _run_acquire(
        state,
        round_barcode="run_old",
        scope="dorado_only",
        stale_seconds=30,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)
    lock_dir = state / LOCK_NAME
    generation = read_record(lock_dir / "generation.tsv", schema=GENERATION_SCHEMA)
    pin = read_record(
        lock_dir / "pins" / f"ready.{pin_token}.tsv", schema=PIN_SCHEMA
    )
    assert pin["token"] == generation_token

    interrupted = subprocess.run(
        [
            "perl",
            str(SCRIPT),
            "early-release",
            "--state-dir",
            str(state),
            "--round-barcode",
            "run_old",
            "--scope",
            "dorado_only",
            "--token",
            generation_token,
            "--pin-token",
            pin_token,
            "--stale-seconds",
            "30",
            "--wait-seconds",
            "1",
        ],
        capture_output=True,
        check=False,
        env=perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-transition-install"),
        timeout=5,
    )
    assert interrupted.returncode != 0
    assert_exact_error_line(
        interrupted.stderr,
        "ERROR: injected failure after transition install",
    )
    marker = read_record(
        state / f".round_lock_handoff.{generation_token}.tsv",
        schema=MARKER_SCHEMA,
    )
    transition = read_record(lock_dir / "transition.tsv", schema=TRANSITION_SCHEMA)
    assert marker["token"] == generation_token
    assert transition["owner_token"] == generation_token
    assert transition["allowed_pin_token"] == pin_token
    assert transition["lock_dev"] == generation["lock_dev"]
    assert transition["lock_ino"] == generation["lock_ino"]

    recovered = _run_acquire(
        state,
        round_barcode="run_replacement",
        scope="full_round",
        stale_seconds=30,
    )
    assert recovered.returncode == 0, recovered.stderr
    replacement_token, _replacement_pin = parse_acquire_output(recovered.stdout)
    replacement = read_record(lock_dir / "generation.tsv", schema=GENERATION_SCHEMA)
    receipt = read_record(
        state / f".round_lock_release.{generation_token}.tsv",
        schema=RELEASE_SCHEMA,
    )
    assert replacement["token"] == replacement_token
    assert replacement_token != generation_token
    assert receipt["operation_token"] == transition["operation_token"]
    assert list(state.glob(f"{LOCK_NAME}.release-*")) == []


def _operator_command(
    state: Path,
    *,
    expected_dev: int,
    expected_ino: int,
    omit: str | None = None,
    operation_token: str | None = None,
    source_name: str | None = None,
) -> list[str]:
    operation_token = operation_token or _token("operator recovery")
    values: list[tuple[str, str | None]] = [
        ("expected-lock-dev", str(expected_dev)),
        ("expected-lock-ino", str(expected_ino)),
        ("operation-token", operation_token),
        ("operator-label", "pytest operator"),
        ("reason", "invalid snapshot reviewed for recovery"),
        ("confirm-invalid-snapshot", None),
    ]
    if source_name is not None:
        values.append(("source-name", source_name))
    command = [
        "perl",
        str(SCRIPT),
        "operator-quarantine-invalid",
        "--state-dir",
        str(state),
    ]
    for name, value in values:
        if name == omit:
            continue
        command.append(f"--{name}")
        if value is not None:
            command.append(value)
    return command


def _wait_for_pause(
    process: subprocess.Popen[bytes], ready: Path, failpoint: str,
    *, timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            entry = os.lstat(ready)
        except FileNotFoundError:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"operator exited before {failpoint}: rc={process.returncode}, "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            if time.monotonic() >= deadline:
                process.kill()
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"operator did not reach {failpoint}: "
                    f"stdout={stdout!r}, stderr={stderr!r}"
                )
            time.sleep(0.01)
            continue
        assert stat.S_ISREG(entry.st_mode) and not stat.S_ISLNK(entry.st_mode)
        assert read_nofollow_bytes(ready) == f"{failpoint}\n".encode("ascii")
        assert process.poll() is None
        return


def _assert_absent(path: Path) -> None:
    """Require ENOENT without following a dangling final-component symlink."""
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return
    raise AssertionError(f"expected {path} absent, found mode {entry.st_mode:o}")


def _operator_entry_fingerprint(path: Path) -> str:
    """Reimplement the operator evidence metadata digest independently."""
    entry = os.lstat(path)
    assert stat.S_ISREG(entry.st_mode) and not stat.S_ISLNK(entry.st_mode)
    metadata = "\0".join(
        (
            "regular",
            str(entry.st_dev),
            str(entry.st_ino),
            str(entry.st_mode),
            str(entry.st_nlink),
            str(entry.st_uid),
            str(entry.st_gid),
            str(entry.st_rdev),
            str(entry.st_size),
            str(entry.st_mtime_ns // 1_000_000_000),
            str(entry.st_ctime_ns // 1_000_000_000),
            "",
        )
    ).encode("ascii")
    return hashlib.sha256(metadata).hexdigest()


def _assert_operator_event(
    path: Path,
    *,
    expected_fields: dict[str, str],
    phase: str,
    outcome: str,
    event_epoch_floor: int,
    event_epoch_ceiling: int,
) -> dict[str, str]:
    """Validate one audit event against independently captured expectations."""
    expected = {**expected_fields, "phase": phase, "outcome": outcome}
    assert set(expected) == set(OPERATOR_EVENT_SCHEMA.fields) - {"event_epoch"}
    event = read_record(path, schema=OPERATOR_EVENT_SCHEMA)
    for field, value in expected.items():
        assert event[field] == value, field
    assert event["event_epoch"].isascii()
    assert event["event_epoch"].isdigit()
    assert event_epoch_floor <= int(event["event_epoch"]) <= event_epoch_ceiling
    return event


def _assert_operator_crash_boundary(
    state: Path,
    *,
    source: Path,
    destination: Path,
    operation_token: str,
    source_manifest: tuple[tuple[object, ...], ...],
    source_visible: bool,
    destination_visible: bool,
    pending_visible: bool,
    complete_visible: bool,
    expected_event_fields: dict[str, str],
    event_epoch_floor: int,
    event_epoch_ceiling: int,
) -> tuple[Path, Path]:
    """Pin the exact visible namespace at one operator crash boundary."""
    audit = state / ".round_lock_operator_events"
    pending_dir = state / ".round_lock_operator_pending"
    intent_path = audit / f"{operation_token}.intent.tsv"
    complete_path = audit / f"{operation_token}.complete.tsv"
    pending_path = pending_dir / f"{operation_token}.tsv"

    assert source_visible != destination_visible
    expected_top = {EVENTS_NAME, ".round_lock_operator_events"}
    if source_visible:
        expected_top.add(source.name)
    if destination_visible:
        expected_top.add(destination.name)
    if pending_visible:
        expected_top.add(".round_lock_operator_pending")
    assert {path.name for path in state.iterdir()} == expected_top
    expected_audit = {f"{operation_token}.intent.tsv"}
    if complete_visible:
        expected_audit.add(f"{operation_token}.complete.tsv")
    assert {path.name for path in audit.iterdir()} == expected_audit

    holder = source if source_visible else destination
    absent_holder = destination if source_visible else source
    assert _namespace(holder) == source_manifest
    _assert_absent(absent_holder)

    intent = _assert_operator_event(
        intent_path,
        expected_fields=expected_event_fields,
        phase="intent",
        outcome="prepared",
        event_epoch_floor=event_epoch_floor,
        event_epoch_ceiling=event_epoch_ceiling,
    )
    holder_entry = os.lstat(holder)
    assert intent["lock_dev"] == str(holder_entry.st_dev)
    assert intent["lock_ino"] == str(holder_entry.st_ino)

    intent_entry = os.lstat(intent_path)
    assert stat.S_ISREG(intent_entry.st_mode)
    assert intent_entry.st_nlink == (2 if pending_visible else 1)
    if pending_visible:
        assert sorted(path.name for path in pending_dir.iterdir()) == [
            f"{operation_token}.tsv"
        ]
        pending_entry = os.lstat(pending_path)
        assert stat.S_ISREG(pending_entry.st_mode)
        assert (pending_entry.st_dev, pending_entry.st_ino) == (
            intent_entry.st_dev,
            intent_entry.st_ino,
        )
        _assert_operator_event(
            pending_path,
            expected_fields=expected_event_fields,
            phase="intent",
            outcome="prepared",
            event_epoch_floor=event_epoch_floor,
            event_epoch_ceiling=event_epoch_ceiling,
        )
    else:
        _assert_absent(pending_dir)

    if complete_visible:
        complete = _assert_operator_event(
            complete_path,
            expected_fields=expected_event_fields,
            phase="complete",
            outcome="quarantined",
            event_epoch_floor=event_epoch_floor,
            event_epoch_ceiling=event_epoch_ceiling,
        )
        complete_entry = os.lstat(complete_path)
        assert stat.S_ISREG(complete_entry.st_mode)
        assert complete_entry.st_nlink == 1
        assert int(complete["event_epoch"]) >= int(intent["event_epoch"])
    else:
        _assert_absent(complete_path)
    return intent_path, complete_path


def test_operator_quarantine_requires_confirmation_and_expected_identity(
    tmp_path: Path,
) -> None:
    """Pin only the minimal explicit operator-remedy interface and outcome."""
    state = _invalid_state(tmp_path, "semantic-invalid-canonical")
    lock_dir = state / LOCK_NAME
    lock_entry = os.lstat(lock_dir)
    before = _namespace(state)
    required = [
        "expected-lock-dev",
        "expected-lock-ino",
        "operation-token",
        "operator-label",
        "reason",
        "confirm-invalid-snapshot",
    ]

    for omitted in required:
        rejected = subprocess.run(
            _operator_command(
                state,
                expected_dev=lock_entry.st_dev,
                expected_ino=lock_entry.st_ino,
                omit=omitted,
            ),
            capture_output=True,
            check=False,
            env=perl_test_env(),
            timeout=5,
        )
        assert rejected.returncode != 0, (omitted, rejected.stdout)
        assert _namespace(state) == before, omitted

    displaced = subprocess.run(
        _operator_command(
            state,
            expected_dev=lock_entry.st_dev,
            expected_ino=lock_entry.st_ino + 1,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert displaced.returncode != 0, displaced.stdout
    assert _namespace(state) == before

    quarantined = subprocess.run(
        _operator_command(
            state,
            expected_dev=lock_entry.st_dev,
            expected_ino=lock_entry.st_ino,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert quarantined.returncode == 0, quarantined.stderr
    assert not os.path.lexists(lock_dir)

    replacement = _run_acquire(
        state,
        round_barcode="run_after_operator_recovery",
        stale_seconds=30,
    )
    assert replacement.returncode == 0, replacement.stderr
    replacement_token, _replacement_pin = parse_acquire_output(replacement.stdout)
    assert read_record(
        lock_dir / "generation.tsv", schema=GENERATION_SCHEMA
    )["token"] == replacement_token


def test_operator_quarantine_refuses_a_valid_generation(tmp_path: Path) -> None:
    """Confirmation of invalidity cannot authorize moving a valid holder."""
    state = _new_state(tmp_path / "state")
    acquired = _run_acquire(
        state,
        round_barcode="live_generation",
        scope="full_round",
        stale_seconds=30,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, _pin_token = parse_acquire_output(acquired.stdout)
    lock_dir = state / LOCK_NAME
    lock_entry = os.lstat(lock_dir)
    assert read_record(
        lock_dir / "generation.tsv", schema=GENERATION_SCHEMA
    )["token"] == generation_token
    before = _namespace(state)

    rejected = subprocess.run(
        _operator_command(
            state,
            expected_dev=lock_entry.st_dev,
            expected_ino=lock_entry.st_ino,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert rejected.returncode != 0, rejected.stdout
    assert _namespace(state) == before


def test_operator_quarantine_writes_bound_audit_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    state = _invalid_state(tmp_path, "parse-invalid-canonical")
    lock_dir = state / LOCK_NAME
    lock_entry = os.lstat(lock_dir)
    command = _operator_command(
        state, expected_dev=lock_entry.st_dev, expected_ino=lock_entry.st_ino,
    )
    first = subprocess.run(
        command, capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert first.returncode == 0, first.stderr
    operator_token = _token("operator recovery")
    destination = state / f"{LOCK_NAME}.operator-{operator_token}"
    destination_entry = os.lstat(destination)
    assert (destination_entry.st_dev, destination_entry.st_ino) == (
        lock_entry.st_dev, lock_entry.st_ino,
    )
    assert read_nofollow_bytes(destination / "generation.tsv") == (
        b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n"
    )
    audit = state / ".round_lock_operator_events"
    intent = read_record(
        audit / f"{operator_token}.intent.tsv", schema=OPERATOR_EVENT_SCHEMA,
    )
    complete = read_record(
        audit / f"{operator_token}.complete.tsv", schema=OPERATOR_EVENT_SCHEMA,
    )
    assert intent["phase"] == "intent"
    assert intent["outcome"] == "prepared"
    assert complete["phase"] == "complete"
    assert complete["outcome"] == "quarantined"
    assert complete["schema"] == "2"
    assert complete["operation_kind"] == "invalid_snapshot"
    assert complete["expected_generation_token"] == "none"
    assert complete["recovery_basis"] == "generation-invalid"
    assert complete["event_epoch"].isdigit()
    assert int(complete["event_epoch"]) >= int(intent["event_epoch"])
    for field in (
        "operation_token", "operation_kind", "expected_generation_token",
        "recovery_basis", "operator_label", "reason", "source_name",
        "destination_name", "lock_dev", "lock_ino", "generation_status",
        "generation_entry_kind", "generation_sha256",
        "generation_entry_fingerprint", "pins_status", "pins_entry_kind",
        "pins_sha256", "pins_entry_fingerprint", "transition_status",
        "transition_entry_kind", "transition_sha256",
        "transition_entry_fingerprint",
    ):
        assert complete[field] == intent[field], field
    assert complete["generation_status"] == "parse-invalid"
    assert complete["generation_entry_kind"] == "regular"
    assert len(complete["generation_entry_fingerprint"]) == 64
    malformed_bytes = read_nofollow_bytes(destination / "generation.tsv")
    assert complete["generation_sha256"] == hashlib.sha256(
        malformed_bytes
    ).hexdigest()
    assert complete["transition_status"] == "absent"
    assert complete["transition_entry_kind"] == "absent"
    assert complete["transition_sha256"] == "none"
    assert complete["transition_entry_fingerprint"] == "none"
    assert complete["pins_status"] == "missing"
    assert complete["pins_entry_kind"] == "absent"
    assert complete["pins_sha256"] == "none"
    assert complete["pins_entry_fingerprint"] == "none"
    assert sorted(path.name for path in audit.iterdir()) == [
        f"{operator_token}.complete.tsv",
        f"{operator_token}.intent.tsv",
    ]
    assert not list(state.glob(".round_lock_release.*.tsv"))
    assert not list(state.glob(".round_lock_revocation.*.tsv"))
    before_replay = _namespace(state)
    replay = subprocess.run(
        command, capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert replay.returncode == 0, replay.stderr
    assert _namespace(state) == before_replay

    # Historical audit is outside the hot pending namespace. Reusing the
    # canonical pathname for a new valid generation must not make the
    # completed operation block later helper commands.
    acquired = _run_acquire(
        state, round_barcode="post_operator_runtime", stale_seconds=30,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)
    guard = subprocess.run(
        [
            "perl", str(SCRIPT), "guard-pin", "--state-dir", str(state),
            "--round-barcode", "post_operator_runtime", "--scope", "full_round",
            "--token", generation_token, "--pin-token", pin_token,
            "--wait-seconds", "1",
        ],
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert guard.returncode == 0, guard.stderr
    inflight = subprocess.run(
        [
            "perl", str(SCRIPT), "inflight", "--state-dir", str(state),
            "--round-barcode", "post_operator_runtime", "--scope", "full_round",
            "--token", generation_token, "--pin-token", pin_token,
            "--read-file", "/reads/post-operator.pod5", "--wait-seconds", "1",
        ],
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert inflight.returncode == 0, inflight.stderr


def test_operator_quarantine_never_certifies_an_unintended_existing_destination(
    tmp_path: Path,
) -> None:
    state = _new_state(tmp_path / "state")
    operator_token = _token("operator recovery")
    destination = state / f"{LOCK_NAME}.operator-{operator_token}"
    destination.mkdir(mode=0o700)
    _parse_invalid_generation(destination)
    destination_entry = os.lstat(destination)
    before = _namespace(state)
    rejected = subprocess.run(
        _operator_command(
            state,
            expected_dev=destination_entry.st_dev,
            expected_ino=destination_entry.st_ino,
        ),
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert rejected.returncode != 0, rejected.stdout
    assert _namespace(state) == before


def test_operator_quarantine_rejects_unrelated_options_and_unknown_command(
    tmp_path: Path,
) -> None:
    state = _invalid_state(tmp_path, "generation-absent-canonical")
    lock_entry = os.lstat(state / LOCK_NAME)
    base = _operator_command(
        state, expected_dev=lock_entry.st_dev, expected_ino=lock_entry.st_ino,
    )
    before = _namespace(state)
    for option in (
        ("--token", "a" * 64),
        ("-token", "a" * 64),
        ("+token", "a" * 64),
        ("--scope", "full_round"),
        ("--best-effort",),
    ):
        rejected = subprocess.run(
            [*base, *option], capture_output=True, check=False,
            env=perl_test_env(), timeout=5,
        )
        assert rejected.returncode != 0, option
        assert _namespace(state) == before

    unknown = subprocess.run(
        [
            "perl", str(SCRIPT), "operator-quarantine-invalid-unknown",
            "--state-dir", str(state),
        ],
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )
    assert unknown.returncode != 0
    assert unknown.stdout == b""
    assert _namespace(state) == before


def test_operator_quarantine_refuses_an_absent_source_without_mutation(
    tmp_path: Path,
) -> None:
    state = _new_state(tmp_path / "state")
    before = _namespace(state)
    rejected = subprocess.run(
        _operator_command(state, expected_dev=1, expected_ino=1),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert rejected.returncode != 0
    assert _namespace(state) == before


def test_operator_quarantine_rejects_a_symlinked_audit_namespace(
    tmp_path: Path,
) -> None:
    state = _invalid_state(tmp_path, "generation-absent-canonical")
    outside = tmp_path / "outside-audit"
    outside.mkdir()
    (state / ".round_lock_operator_events").symlink_to(outside)
    lock_entry = os.lstat(state / LOCK_NAME)
    before = _namespace(state)
    rejected = subprocess.run(
        _operator_command(
            state, expected_dev=lock_entry.st_dev,
            expected_ino=lock_entry.st_ino,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert rejected.returncode != 0
    assert _namespace(state) == before
    assert list(outside.iterdir()) == []


def test_operator_quarantine_rejects_an_unexpected_pending_entry_without_mutation(
    tmp_path: Path,
) -> None:
    state = _invalid_state(tmp_path, "generation-absent-canonical")
    pending = state / ".round_lock_operator_pending"
    pending.mkdir(mode=0o700)
    (pending / "unexpected").write_bytes(b"not a pending intent\n")
    lock_entry = os.lstat(state / LOCK_NAME)
    before = _namespace(state)
    rejected = subprocess.run(
        _operator_command(
            state, expected_dev=lock_entry.st_dev,
            expected_ino=lock_entry.st_ino,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert rejected.returncode != 0
    assert _namespace(state) == before


@pytest.mark.parametrize("entry_kind", ["dangling-symlink", "directory"])
def test_operator_quarantine_preserves_nonregular_generation_evidence(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    state = _new_state(tmp_path / "state")
    lock_dir = state / LOCK_NAME
    lock_dir.mkdir(mode=0o700)
    generation = lock_dir / "generation.tsv"
    if entry_kind == "dangling-symlink":
        generation.symlink_to(tmp_path / "missing-generation-target")
    else:
        generation.mkdir(mode=0o700)
    before_kind = os.lstat(generation).st_mode
    lock_entry = os.lstat(lock_dir)
    blocked = _run_acquire(state, round_barcode="blocked")
    assert blocked.returncode != 0

    command = _operator_command(
        state, expected_dev=lock_entry.st_dev, expected_ino=lock_entry.st_ino,
    )
    moved = subprocess.run(
        command, capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert moved.returncode == 0, moved.stderr
    operator_token = _token("operator recovery")
    destination = state / f"{LOCK_NAME}.operator-{operator_token}"
    preserved = destination / "generation.tsv"
    assert os.lstat(preserved).st_mode == before_kind
    if entry_kind == "dangling-symlink":
        assert os.readlink(preserved) == str(tmp_path / "missing-generation-target")
    event = read_record(
        state / ".round_lock_operator_events" / f"{operator_token}.complete.tsv",
        schema=OPERATOR_EVENT_SCHEMA,
    )
    assert event["generation_status"] == "parse-invalid"
    assert event["generation_sha256"] == (
        hashlib.sha256(b"").hexdigest()
        if entry_kind == "directory"
        else "none"
    )
    assert event["generation_entry_kind"] == (
        "symlink" if entry_kind == "dangling-symlink" else "directory"
    )
    assert len(event["generation_entry_fingerprint"]) == 64
    replacement = _run_acquire(state, round_barcode="after_operator")
    assert replacement.returncode == 0, replacement.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_operator_quarantine_preserves_unreadable_regular_evidence(
    tmp_path: Path,
) -> None:
    state = _new_state(tmp_path / "state")
    lock_dir = state / LOCK_NAME
    lock_dir.mkdir(mode=0o700)
    generation = lock_dir / "generation.tsv"
    generation.write_bytes(b"unreadable evidence\n")
    generation.chmod(0)
    lock_entry = os.lstat(lock_dir)
    moved = subprocess.run(
        _operator_command(
            state, expected_dev=lock_entry.st_dev,
            expected_ino=lock_entry.st_ino,
        ),
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert moved.returncode == 0, moved.stderr
    operator_token = _token("operator recovery")
    destination = state / f"{LOCK_NAME}.operator-{operator_token}"
    preserved = destination / "generation.tsv"
    assert stat.S_IMODE(os.lstat(preserved).st_mode) == 0
    event = read_record(
        state / ".round_lock_operator_events" / f"{operator_token}.complete.tsv",
        schema=OPERATOR_EVENT_SCHEMA,
    )
    assert event["generation_status"] == "parse-invalid"
    assert event["generation_entry_kind"] == "regular-unreadable"
    assert event["generation_sha256"] == "none"
    assert len(event["generation_entry_fingerprint"]) == 64


@pytest.mark.parametrize(
    "case",
    [
        "generation-absent-canonical",
        "invalid-reclaim-quarantine",
        "invalid-release-quarantine",
    ],
)
def test_operator_quarantine_handles_every_advertised_invalid_source_class(
    tmp_path: Path,
    case: str,
) -> None:
    state = _invalid_state(tmp_path, case)
    if case == "generation-absent-canonical":
        source = state / LOCK_NAME
        source_name = None
    else:
        matches = list(state.glob(f"{LOCK_NAME}.*-*"))
        assert len(matches) == 1
        source = matches[0]
        source_name = source.name
    source_entry = os.lstat(source)
    source_manifest = _namespace(source)
    operation_token = _token(f"operator source {case}")
    command = _operator_command(
        state,
        expected_dev=source_entry.st_dev,
        expected_ino=source_entry.st_ino,
        operation_token=operation_token,
        source_name=source_name,
    )
    completed = subprocess.run(
        command, capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert completed.returncode == 0, completed.stderr
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    assert _namespace(destination) == source_manifest
    assert not os.path.lexists(source)
    intent = read_record(
        state / ".round_lock_operator_events" / f"{operation_token}.intent.tsv",
        schema=OPERATOR_EVENT_SCHEMA,
    )
    complete = read_record(
        state / ".round_lock_operator_events" / f"{operation_token}.complete.tsv",
        schema=OPERATOR_EVENT_SCHEMA,
    )
    assert intent["source_name"] == (source_name or LOCK_NAME)
    assert complete["operation_token"] == operation_token
    assert not list(state.glob(".round_lock_release.*.tsv"))
    assert not list(state.glob(".round_lock_revocation.*.tsv"))
    replacement = _run_acquire(
        state, round_barcode=f"after_{case}", stale_seconds=30,
    )
    assert replacement.returncode == 0, replacement.stderr


@pytest.mark.parametrize(
    (
        "failpoint,source_visible,destination_visible,pending_visible,"
        "complete_visible"
    ),
    [
        ("after-operator-intent-link-before-sync", True, False, False, False),
        ("after-operator-intent", True, False, True, False),
        ("after-operator-rename-before-sync", False, True, True, False),
        ("after-operator-sync-before-complete", False, True, True, False),
        ("after-operator-complete-link-before-sync", False, True, True, True),
    ],
)
def test_operator_quarantine_replays_every_crash_boundary(
    tmp_path: Path,
    failpoint: str,
    source_visible: bool,
    destination_visible: bool,
    pending_visible: bool,
    complete_visible: bool,
) -> None:
    state = _invalid_state(tmp_path, "semantic-invalid-canonical")
    source = state / LOCK_NAME
    source_entry = os.lstat(source)
    source_manifest = _namespace(source)
    operation_token = _token(f"operator crash {failpoint}")
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    generation_path = source / "generation.tsv"
    generation = read_record(generation_path, schema=GENERATION_SCHEMA)
    assert generation["lock_dev"] == str(source_entry.st_dev)
    assert generation["lock_ino"] != str(source_entry.st_ino)
    generation_bytes = read_nofollow_bytes(generation_path)
    generation_fingerprint = _operator_entry_fingerprint(generation_path)
    tree_digest = hashlib.sha256()
    for value in (
        "generation.tsv",
        "regular",
        hashlib.sha256(generation_bytes).hexdigest(),
        generation_fingerprint,
    ):
        encoded = value.encode("ascii")
        tree_digest.update(struct.pack(">I", len(encoded)))
        tree_digest.update(encoded)
    _assert_absent(source / "transition.tsv")
    expected_event_fields = {
        "schema": "2",
        "operation_token": operation_token,
        "operation_kind": "invalid_snapshot",
        "expected_generation_token": "none",
        "recovery_basis": "generation-invalid",
        "operator_label": "pytest operator",
        "reason": "invalid snapshot reviewed for recovery",
        "source_name": source.name,
        "destination_name": destination.name,
        "lock_dev": str(source_entry.st_dev),
        "lock_ino": str(source_entry.st_ino),
        "tree_sha256": tree_digest.hexdigest(),
        "generation_status": "semantic-invalid",
        "generation_entry_kind": "regular",
        "generation_sha256": hashlib.sha256(generation_bytes).hexdigest(),
        "generation_entry_fingerprint": generation_fingerprint,
        "pins_status": "missing",
        "pins_entry_kind": "absent",
        "pins_sha256": "none",
        "pins_entry_fingerprint": "none",
        "transition_status": "absent",
        "transition_entry_kind": "absent",
        "transition_sha256": "none",
        "transition_entry_fingerprint": "none",
        "marker_status": "not-applicable",
        "marker_entry_kind": "absent",
        "marker_sha256": "none",
        "marker_entry_fingerprint": "none",
        "release_authority_status": "not-applicable",
        "finalization_status": "not-applicable",
        "finalization_sha256": "none",
    }
    command = _operator_command(
        state,
        expected_dev=source_entry.st_dev,
        expected_ino=source_entry.st_ino,
        operation_token=operation_token,
    )
    event_epoch_floor = int(time.time())
    ready = tmp_path / f"{failpoint}.ready"
    release = tmp_path / f"{failpoint}.release"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint,
            RTBIOSCAN_ROUND_LOCK_TEST_READY=str(ready),
            RTBIOSCAN_ROUND_LOCK_TEST_RELEASE=str(release),
        ),
    )
    _wait_for_pause(process, ready, failpoint)
    process.send_signal(signal.SIGKILL)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL, (stdout, stderr)

    intent_path, complete_path = _assert_operator_crash_boundary(
        state,
        source=source,
        destination=destination,
        operation_token=operation_token,
        source_manifest=source_manifest,
        source_visible=source_visible,
        destination_visible=destination_visible,
        pending_visible=pending_visible,
        complete_visible=complete_visible,
        expected_event_fields=expected_event_fields,
        event_epoch_floor=event_epoch_floor,
        event_epoch_ceiling=int(time.time()),
    )

    if not complete_visible:
        before_runtime = _namespace(state)
        blocked = _run_acquire(
            state, round_barcode=f"blocked_{failpoint}", stale_seconds=30,
        )
        assert blocked.returncode != 0
        assert blocked.stdout == b""
        if failpoint == "after-operator-intent-link-before-sync":
            expected_error = (
                "ERROR: round lock has semantic-invalid generation state and "
                f"requires operator quarantine: {source}"
            )
        else:
            expected_error = (
                f"ERROR: incomplete operator quarantine {operation_token}; "
                "rerun operator-quarantine-invalid with the original arguments"
            )
        assert_exact_error_line(blocked.stderr, expected_error)
        assert _namespace(state) == before_runtime

    replacement_token: str | None = None
    if complete_visible:
        reconciled = _run_acquire(
            state, round_barcode=f"reconciled_{failpoint}", stale_seconds=30,
        )
        assert reconciled.returncode == 0, reconciled.stderr
        replacement_token, _replacement_pin = parse_acquire_output(
            reconciled.stdout
        )

    replay = subprocess.run(
        command, capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert replay.returncode == 0, replay.stderr
    replay_epoch_ceiling = int(time.time())
    destination_entry = os.lstat(destination)
    assert (destination_entry.st_dev, destination_entry.st_ino) == (
        source_entry.st_dev, source_entry.st_ino,
    )
    assert _namespace(destination) == source_manifest
    if complete_visible:
        assert read_record(
            source / "generation.tsv", schema=GENERATION_SCHEMA,
        )["token"] == replacement_token
    else:
        assert not os.path.lexists(source)
    assert not list((state / ".round_lock_operator_pending").glob("*.tsv"))
    intent = _assert_operator_event(
        intent_path,
        expected_fields=expected_event_fields,
        phase="intent",
        outcome="prepared",
        event_epoch_floor=event_epoch_floor,
        event_epoch_ceiling=replay_epoch_ceiling,
    )
    complete = _assert_operator_event(
        complete_path,
        expected_fields=expected_event_fields,
        phase="complete",
        outcome="quarantined",
        event_epoch_floor=event_epoch_floor,
        event_epoch_ceiling=replay_epoch_ceiling,
    )
    assert int(complete["event_epoch"]) >= int(intent["event_epoch"])
    if not complete_visible:
        replacement = _run_acquire(
            state, round_barcode=f"after_{failpoint}", stale_seconds=30,
        )
        assert replacement.returncode == 0, replacement.stderr


def test_unicode_decimal_generation_field_is_semantically_invalid(
    tmp_path: Path,
) -> None:
    state = _new_state(tmp_path / "state")
    lock_dir = state / LOCK_NAME
    lock_dir.mkdir(mode=0o700)
    entry = os.lstat(lock_dir)
    values = {
        "schema": "1",
        "token": _token("unicode numeric generation"),
        "round_barcode": "unicode",
        "scope": "full_round",
        "pid": str(os.getpid()),
        "host": "fixture.invalid",
        "process_start": "unavailable",
        "started_epoch": "١٢٣",
        "effective_ttl_seconds": "1",
        "lock_dev": str(entry.st_dev),
        "lock_ino": str(entry.st_ino),
    }
    _write_record(lock_dir / "generation.tsv", values, schema=GENERATION_SCHEMA)
    before = _namespace(state)
    for index in range(2):
        blocked = _run_acquire(state, round_barcode=f"unicode_{index}")
        assert blocked.returncode != 0
        assert _namespace(state) == before


def test_runtime_command_surfaces_corruption_as_operator_recovery_state(
    tmp_path: Path,
) -> None:
    state = _invalid_state(tmp_path, "parse-invalid-canonical")
    lock_dir = state / LOCK_NAME
    before = _namespace(state)
    result = subprocess.run(
        [
            "perl", str(SCRIPT), "guard-pin", "--state-dir", str(state),
            "--round-barcode", "run_invalid", "--scope", "dorado_only",
            "--token", _token("runtime malformed token"),
            "--pin-token", _token("runtime malformed pin"),
            "--wait-seconds", "1",
        ],
        capture_output=True, check=False, env=perl_test_env(), timeout=5,
    )
    assert result.returncode != 0
    assert result.stdout == b""
    assert_exact_error_line(
        result.stderr,
        "ERROR: round lock has parse-invalid generation state and requires "
        f"operator quarantine: {lock_dir}",
    )
    assert _namespace(state) == before
