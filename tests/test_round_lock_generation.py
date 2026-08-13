from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.round_lock_test_utils import (
    EVENT_SCHEMA,
    FINISH_SCHEMA,
    GENERATION_SCHEMA,
    INFLIGHT_SCHEMA,
    MARKER_SCHEMA,
    PIN_SCHEMA,
    RELEASE_SCHEMA,
    REVOCATION_SCHEMA,
    TRANSITION_SCHEMA,
    RecordSchema,
    assert_exact_error_line as _assert_exact_error_line,
    assert_token as _assert_token,
    parse_acquire_output as _parse_acquire_output,
    perl_test_env as _perl_test_env,
    read_compat_inflight as _read_compat_inflight,
    read_nofollow_bytes as _read_nofollow_bytes,
    read_record as _read_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
FAILPOINT_ERRORS = {
    "after-transition-install": "ERROR: injected failure after transition install",
    "after-quarantine-rename": "ERROR: injected failure after quarantine rename",
    "before-compat-publish": (
        "ERROR: injected failure before compatibility inflight publish"
    ),
}


def _command(
    action: str,
    state: Path,
    *,
    round_barcode: str = "run_1",
    scope: str = "full_round",
    token: str | None = None,
    pin_token: str | None = None,
    role: str | None = None,
    owner_pid: int | None = None,
    stale_seconds: int = 30,
    wait_seconds: int = 1,
    read_file: str | None = None,
    best_effort: bool = False,
) -> list[str]:
    cmd = [
        "perl",
        str(SCRIPT),
        action,
        "--state-dir",
        str(state),
        "--round-barcode",
        round_barcode,
        "--scope",
        scope,
        "--stale-seconds",
        str(stale_seconds),
        "--wait-seconds",
        str(wait_seconds),
    ]
    if token is not None:
        cmd.extend(["--token", token])
    if pin_token is not None:
        cmd.extend(["--pin-token", pin_token])
    if role is not None:
        cmd.extend(["--role", role])
    if owner_pid is not None:
        cmd.extend(["--owner-pid", str(owner_pid)])
    if read_file is not None:
        cmd.extend(["--read-file", read_file])
    if best_effort:
        cmd.append("--best-effort")
    return cmd


def _run(
    action: str,
    state: Path,
    **kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(action, state, **kwargs),
        capture_output=True,
        text=True,
        check=False,
    )


def _acquire(state: Path, **kwargs: object) -> tuple[str, str]:
    kwargs.setdefault("owner_pid", os.getpid())
    result = subprocess.run(
        _command("acquire", state, **kwargs),
        capture_output=True,
        check=False,
        env=_perl_test_env(),
    )
    assert result.returncode == 0, result.stderr
    return _parse_acquire_output(result.stdout)


def _pin(state: Path, token: str, *, role: str, **kwargs: object) -> str:
    kwargs.setdefault("owner_pid", os.getpid())
    result = subprocess.run(
        _command("pin", state, token=token, role=role, **kwargs),
        capture_output=True,
        check=False,
        env=_perl_test_env(),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith(b"\n"), result.stdout
    raw_token = result.stdout[:-1]
    assert b"\n" not in raw_token, result.stdout
    pin_token = raw_token.decode("ascii")
    _assert_token(pin_token)
    return pin_token


def _rewrite_record(
    path: Path,
    values: dict[str, str],
    *,
    schema: RecordSchema,
) -> None:
    assert set(values) == set(schema.fields), (values, schema)
    body = b"".join(
        key.encode("utf-8") + b"\t" + values[key].encode("utf-8") + b"\n"
        for key in schema.fields
    )
    path.write_bytes(
        body + b"record_sha256\t" + hashlib.sha256(body).hexdigest().encode() + b"\n"
    )


def _generation(state: Path) -> dict[str, str]:
    return _read_record(
        state / ".round_inflight.lockdir" / "generation.tsv",
        schema=GENERATION_SCHEMA,
    )


def _marker(state: Path, token: str) -> Path:
    return state / f".round_lock_handoff.{token}.tsv"


def _release_receipt(state: Path, token: str) -> Path:
    return state / f".round_lock_release.{token}.tsv"


def _finish_receipt(state: Path, token: str) -> Path:
    return state / f".round_lock_finish.{token}.tsv"


def _revocation(state: Path, token: str) -> Path:
    return state / f".round_lock_revocation.{token}.tsv"


def _backdate_lock(state: Path) -> None:
    lock_dir = state / ".round_inflight.lockdir"
    for path in [lock_dir, *lock_dir.iterdir()]:
        os.utime(path, (1, 1), follow_symlinks=False)


def _event_records(state: Path) -> list[dict[str, str]]:
    return [
        _read_record(path, schema=EVENT_SCHEMA)
        for path in sorted((state / ".round_lock_events").glob("*.tsv"))
    ]


def test_full_round_handoff_pins_release_and_exit_are_generation_bound(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, acquisition_pin_a = _acquire(state, stale_seconds=37)
    generation_a = _generation(state)
    lock_stat = (state / ".round_inflight.lockdir").stat()
    assert generation_a["token"] == token_a
    assert generation_a["round_barcode"] == "run_1"
    assert generation_a["scope"] == "full_round"
    assert generation_a["effective_ttl_seconds"] == "37"
    assert generation_a["lock_dev"] == str(lock_stat.st_dev)
    assert generation_a["lock_ino"] == str(lock_stat.st_ino)
    acquisition_pin_record = _read_record(
        state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{acquisition_pin_a}.tsv",
        schema=PIN_SCHEMA,
    )
    assert acquisition_pin_record["token"] == token_a
    assert acquisition_pin_record["pin_token"] == acquisition_pin_a
    assert acquisition_pin_record["role"] == "fast_acquisition"

    inflight = _run(
        "inflight",
        state,
        token=token_a,
        pin_token=acquisition_pin_a,
        read_file="/reads/run_1.pod5",
    )
    assert inflight.returncode == 0, inflight.stderr
    diagnostic = _read_compat_inflight(state / "round_inflight.txt")
    assert diagnostic["round_barcode"] == "run_1"
    assert diagnostic["read_file"] == "/reads/run_1.pod5"
    assert diagnostic["generation_token"] == token_a

    handoff = _run(
        "handoff", state, token=token_a, pin_token=acquisition_pin_a
    )
    assert handoff.returncode == 0, handoff.stderr
    assert _read_record(
        _marker(state, token_a), schema=MARKER_SCHEMA
    )["round_barcode"] == "run_1"
    assert not (
        state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{acquisition_pin_a}.tsv"
    ).exists()

    finisher_pin = _pin(state, token_a, role="backup_update_and_clean")
    worker_pin = _pin(state, token_a, role="state_writer")
    wrong_handoff_pin = _run(
        "handoff", state, token=token_a, pin_token=worker_pin
    )
    assert wrong_handoff_pin.returncode != 0
    assert "handoff pin role mismatch" in wrong_handoff_pin.stderr
    wrong_finish_pin = _run(
        "finish", state, token=token_a, pin_token=worker_pin
    )
    assert wrong_finish_pin.returncode != 0
    assert "process pin role mismatch" in wrong_finish_pin.stderr
    finish_without_pin = _run("finish", state, token=token_a)
    assert finish_without_pin.returncode != 0
    assert "missing pin-token" in finish_without_pin.stderr
    assert _run(
        "guard-pin", state, token=token_a, pin_token=finisher_pin
    ).returncode == 0
    assert _run(
        "guard-pin", state, token=token_a, pin_token=worker_pin
    ).returncode == 0

    unpin = _run("unpin", state, token=token_a, pin_token=worker_pin)
    assert unpin.returncode == 0, unpin.stderr
    missing_pin = _run(
        "guard-pin", state, token=token_a, pin_token=worker_pin
    )
    assert missing_pin.returncode != 0
    assert "process pin is absent" in missing_pin.stderr
    rejected_finish = _run(
        "finish", state, token=token_a, pin_token=worker_pin
    )
    assert rejected_finish.returncode != 0
    assert "process pin is absent" in rejected_finish.stderr
    assert (state / ".round_inflight.lockdir").is_dir()

    finish = _run("finish", state, token=token_a, pin_token=finisher_pin)
    assert finish.returncode == 0, finish.stderr
    assert not (state / ".round_inflight.lockdir").exists()
    assert not (state / "round_inflight.txt").exists()
    assert not (state / f".round_inflight.{token_a}.tsv").exists()
    assert not _marker(state, token_a).exists()
    assert list(state.glob(".round_inflight.lockdir.release-*")) == []
    receipt = _read_record(_release_receipt(state, token_a), schema=RELEASE_SCHEMA)
    assert receipt["reason"] == "full_round_released"
    assert receipt["effective_ttl_seconds"] == "37"
    finished = _read_record(_finish_receipt(state, token_a), schema=FINISH_SCHEMA)
    assert finished["outcome"] == "finished"
    assert finished["disposition"] == "round_complete"
    assert finished["release_reason"] == "full_round_released"
    assert finished["release_operation_token"] == receipt["operation_token"]
    verified = _run("verify-release", state, token=token_a)
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.strip() == "full_round_released"
    verified_finish = _run("verify-finish", state, token=token_a)
    assert verified_finish.returncode == 0, verified_finish.stderr
    assert verified_finish.stdout == "full_round_released\n"

    token_b, _pin_b = _acquire(state, round_barcode="run_2")
    generation_b_path = state / ".round_inflight.lockdir" / "generation.tsv"
    generation_b_bytes = _read_nofollow_bytes(generation_b_path)
    late_exit = _run(
        "abort",
        state,
        token=token_a,
        pin_token=acquisition_pin_a,
        best_effort=True,
    )
    assert late_exit.returncode != 0
    assert "release reason conflicts" in late_exit.stderr
    assert _read_nofollow_bytes(generation_b_path) == generation_b_bytes
    assert _generation(state)["token"] == token_b

    events = _event_records(state)
    a_events = {event["event"]: event for event in events if event["generation_token"] == token_a}
    assert {"acquire", "handoff", "release"} <= set(a_events)
    assert a_events["release"]["outcome"] == "full_round_released"
    assert all(event["scope"] == "full_round" for event in a_events.values())
    assert all(event["effective_ttl_seconds"] == "37" for event in a_events.values())


def test_dorado_only_marker_without_lock_and_prefix_cleanup_are_safe(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_1, pin_1 = _acquire(
        state, round_barcode="run_1", scope="dorado_only"
    )
    release_1 = _run(
        "early-release",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
        pin_token=pin_1,
    )
    assert release_1.returncode == 0, release_1.stderr
    marker_1 = _marker(state, token_1)
    assert marker_1.is_file()
    marker_1_record = _read_record(marker_1, schema=MARKER_SCHEMA)
    assert marker_1_record["token"] == token_1
    assert marker_1_record["round_barcode"] == "run_1"
    assert marker_1_record["scope"] == "dorado_only"
    assert marker_1_record["outcome"] == "handoff"
    assert not (state / ".round_inflight.lockdir").exists()
    assert not (state / "round_inflight.txt").exists()
    assert not (state / f".round_inflight.{token_1}.tsv").exists()
    assert list(state.glob(".round_inflight.lockdir.release-*")) == []
    verified_1 = _run(
        "verify-release",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
    )
    assert verified_1.returncode == 0, verified_1.stderr
    assert verified_1.stdout.strip() == "dorado_only_early"
    pending_finish_1 = _run(
        "verify-finish",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
    )
    assert pending_finish_1.returncode != 0
    assert "missing authenticated finish receipt" in pending_finish_1.stderr

    token_10, pin_10 = _acquire(
        state, round_barcode="run_10", scope="dorado_only"
    )
    release_10 = _run(
        "early-release",
        state,
        round_barcode="run_10",
        scope="dorado_only",
        token=token_10,
        pin_token=pin_10,
    )
    assert release_10.returncode == 0, release_10.stderr
    marker_10 = _marker(state, token_10)
    marker_10_bytes = _read_nofollow_bytes(marker_10)

    finish_1 = _run(
        "finish",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
    )
    assert finish_1.returncode == 0, finish_1.stderr
    assert not marker_1.exists()
    assert _read_nofollow_bytes(marker_10) == marker_10_bytes
    verified_finish_1 = _run(
        "verify-finish",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
    )
    assert verified_finish_1.returncode == 0, verified_finish_1.stderr
    assert verified_finish_1.stdout == "dorado_only_early\n"
    finish_receipt_1 = _read_record(
        _finish_receipt(state, token_1), schema=FINISH_SCHEMA
    )
    assert finish_receipt_1["outcome"] == "finished"
    assert finish_receipt_1["disposition"] == "round_complete"
    assert finish_receipt_1["release_reason"] == "dorado_only_early"

    wrong_round = _run(
        "verify-finish",
        state,
        round_barcode="wrong",
        scope="dorado_only",
        token=token_1,
    )
    assert wrong_round.returncode != 0
    assert "release receipt does not match generation" in wrong_round.stderr


def test_dorado_only_failed_post_release_handoff_is_terminal_but_not_finished(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    released = _run(
        "early-release",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert released.returncode == 0, released.stderr
    marker = _marker(state, token)
    assert marker.is_file()

    cancelled = _run(
        "cancel-handoff",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert cancelled.returncode == 0, cancelled.stderr
    assert not marker.exists()
    terminal = _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA)
    assert terminal["outcome"] == "abandoned"
    assert terminal["disposition"] == "fast_post_release_failure"
    assert terminal["release_reason"] == "dorado_only_early"

    replay = _run(
        "cancel-handoff",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert replay.returncode == 0, replay.stderr
    assert _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA) == terminal
    verified = _run("verify-finish", state, scope="dorado_only", token=token)
    assert verified.returncode != 0
    assert "generation handoff was abandoned" in verified.stderr
    conflicting_finish = _run("finish", state, scope="dorado_only", token=token)
    assert conflicting_finish.returncode != 0
    assert "finish receipt does not match generation" in conflicting_finish.stderr


def test_cancel_handoff_rejects_a_pin_not_bound_to_the_archived_release(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    released = _run(
        "early-release",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert released.returncode == 0, released.stderr
    release = _read_record(_release_receipt(state, token), schema=RELEASE_SCHEMA)
    archive = (
        state
        / ".round_lock_archives"
        / f"release-{release['operation_token']}"
    )
    transition = _read_record(archive / "transition.tsv", schema=TRANSITION_SCHEMA)
    assert transition["allowed_pin_token"] == pin_token
    marker = _marker(state, token)
    marker_before = _read_nofollow_bytes(marker)
    wrong_pin = "f" * 64 if pin_token != "f" * 64 else "e" * 64

    rejected = _run(
        "cancel-handoff",
        state,
        scope="dorado_only",
        token=token,
        pin_token=wrong_pin,
    )

    assert rejected.returncode != 0, rejected.stdout
    assert "cancel-handoff pin does not authorize released generation" in rejected.stderr
    assert _read_nofollow_bytes(marker) == marker_before
    assert not _finish_receipt(state, token).exists()

    accepted = _run(
        "cancel-handoff",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert accepted.returncode == 0, accepted.stderr
    assert not marker.exists()
    terminal = _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA)
    assert terminal["outcome"] == "abandoned"
    assert terminal["disposition"] == "fast_post_release_failure"


def test_cancel_handoff_reconciles_first_early_release_and_replays(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    assert (state / ".round_inflight.lockdir").is_dir()
    assert not _release_receipt(state, token).exists()
    assert not _finish_receipt(state, token).exists()

    cancelled = _run(
        "cancel-handoff",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert cancelled.returncode == 0, cancelled.stderr
    release = _read_record(_release_receipt(state, token), schema=RELEASE_SCHEMA)
    terminal = _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA)
    assert release["reason"] == "dorado_only_early"
    assert terminal["outcome"] == "abandoned"
    assert terminal["disposition"] == "fast_post_release_failure"
    assert not _marker(state, token).exists()
    assert not (state / ".round_inflight.lockdir").exists()

    replay = _run(
        "cancel-handoff",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert replay.returncode == 0, replay.stderr
    assert _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA) == terminal


@pytest.mark.parametrize(
    "failpoint",
    [
        "after-transition-install",
        "after-quarantine-rename",
    ],
)
def test_cancel_handoff_reconciles_early_release_before_terminal_outcome(
    tmp_path: Path,
    failpoint: str,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    interrupted = subprocess.run(
        _command(
            "early-release",
            state,
            scope="dorado_only",
            token=token,
            pin_token=pin_token,
        ),
        capture_output=True,
        check=False,
        env=_perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint),
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])
    # A genuine helper transition/quarantine/recovery artifact proves this
    # invocation reached the selected durable release boundary.
    assert (
        (state / ".round_inflight.lockdir" / "transition.tsv").exists()
        or list(state.glob(".round_inflight.lockdir.release-*"))
        or _release_receipt(state, token).exists()
    )

    reconciled = _run(
        "cancel-handoff",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert reconciled.returncode == 0, reconciled.stderr
    terminal = _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA)
    assert terminal["outcome"] == "abandoned"
    assert terminal["disposition"] == "fast_post_release_failure"
    assert not _marker(state, token).exists()
    assert not (state / ".round_inflight.lockdir").exists()
    assert list(state.glob(".round_inflight.lockdir.release-*")) == []


@pytest.mark.parametrize("command", ["finish", "cancel-handoff"])
def test_dorado_finish_disposition_replays_after_intent_before_marker_cleanup(
    tmp_path: Path,
    command: str,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    released = _run(
        "early-release",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert released.returncode == 0, released.stderr
    marker = _marker(state, token)
    assert marker.is_file()

    interrupted = subprocess.run(
        _command(
            command,
            state,
            scope="dorado_only",
            token=token,
            pin_token=pin_token if command == "cancel-handoff" else None,
        ),
        capture_output=True,
        text=True,
        check=False,
        env=_perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT=(
                "after-finish-install-before-marker-remove"
            )
        ),
    )
    assert interrupted.returncode != 0
    assert "injected failure" in interrupted.stderr
    assert marker.is_file()
    disposition = _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA)
    assert disposition["outcome"] == (
        "finished" if command == "finish" else "abandoned"
    )

    verified = _run("verify-finish", state, scope="dorado_only", token=token)
    assert not marker.exists()
    if command == "finish":
        assert verified.returncode == 0, verified.stderr
        assert verified.stdout == "dorado_only_early\n"
    else:
        assert verified.returncode != 0
        assert "generation handoff was abandoned" in verified.stderr
    assert _read_record(_finish_receipt(state, token), schema=FINISH_SCHEMA) == disposition


@pytest.mark.parametrize("command", ["finish", "cancel-handoff"])
@pytest.mark.parametrize("marker_state", ["absent", "malformed"])
def test_first_dorado_finish_disposition_requires_exact_handoff_marker(
    tmp_path: Path,
    command: str,
    marker_state: str,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    released = _run(
        "early-release",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert released.returncode == 0, released.stderr
    marker = _marker(state, token)
    assert _read_record(marker, schema=MARKER_SCHEMA)["token"] == token

    if marker_state == "absent":
        marker.unlink()
        assert not marker.exists()
    else:
        marker.write_bytes(b"malformed-handoff-marker\n")
        marker_before = marker.read_bytes()

    rejected = _run(
        command,
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token if command == "cancel-handoff" else None,
    )
    assert rejected.returncode != 0, rejected.stdout
    if marker_state == "absent":
        assert "missing handoff marker" in rejected.stderr
        assert not marker.exists()
    else:
        assert "malformed record" in rejected.stderr
        assert marker.read_bytes() == marker_before
    assert not _finish_receipt(state, token).exists()


def test_marker_absence_is_synced_before_finish_replay_accepts_it() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    start = source.index("sub remove_exact_marker {")
    end = source.index("\n}\n", start)
    body = source[start:end]
    sync_before_lookup = body.index("sync_directory($arg{state_dir});")
    absence_lookup = body.index("path_occupied_nofollow($path, 'handoff marker')")
    unlink_marker = body.index("unlink($path)")
    sync_after_unlink = body.rindex("sync_directory($arg{state_dir});")
    assert sync_before_lookup < absence_lookup < unlink_marker < sync_after_unlink


def test_reclaimed_full_round_cannot_publish_release_or_remove_replacement(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, round_barcode="same", stale_seconds=1)
    assert _run(
        "handoff",
        state,
        round_barcode="same",
        token=token_a,
        pin_token=pin_a,
        stale_seconds=1,
    ).returncode == 0
    _backdate_lock(state)

    token_b, pin_b = _acquire(state, round_barcode="same", stale_seconds=1)
    assert token_b != token_a
    revocation_a = _read_record(_revocation(state, token_a), schema=REVOCATION_SCHEMA)
    assert revocation_a["token"] == token_a
    assert revocation_a["round_barcode"] == "same"
    assert revocation_a["scope"] == "full_round"
    assert revocation_a["outcome"] == "revoked"
    assert revocation_a["effective_ttl_seconds"] == "1"
    _assert_token(revocation_a["operation_token"])
    assert not _release_receipt(state, token_a).exists()
    assert _marker(state, token_a).is_file()
    assert not (state / f".round_inflight.{token_a}.tsv").exists()
    late_handoff = _run(
        "handoff",
        state,
        round_barcode="same",
        token=token_a,
        pin_token=pin_a,
    )
    assert late_handoff.returncode != 0
    assert "cannot hand off revoked generation" in late_handoff.stderr
    assert _run(
        "inflight",
        state,
        round_barcode="same",
        token=token_b,
        pin_token=pin_b,
        read_file="/reads/replacement.pod5",
    ).returncode == 0
    generation_b_path = state / ".round_inflight.lockdir" / "generation.tsv"
    inflight_b_path = state / "round_inflight.txt"
    generation_b = _read_nofollow_bytes(generation_b_path)
    inflight_b = _read_nofollow_bytes(inflight_b_path)

    stale_guard = _run(
        "guard-pin",
        state,
        round_barcode="same",
        token=token_a,
        pin_token=pin_a,
    )
    assert stale_guard.returncode != 0
    stale_pin = _run(
        "pin",
        state,
        round_barcode="same",
        token=token_a,
        owner_pid=os.getpid(),
        role="stale_writer",
    )
    assert stale_pin.returncode != 0
    stale_finish = _run(
        "finish",
        state,
        round_barcode="same",
        token=token_a,
        pin_token=pin_a,
    )
    assert stale_finish.returncode != 0
    late_inflight = _run(
        "inflight",
        state,
        round_barcode="same",
        token=token_a,
        pin_token=pin_a,
        read_file="/reads/stale-a.pod5",
    )
    assert late_inflight.returncode != 0
    assert _run(
        "abort",
        state,
        round_barcode="same",
        token=token_a,
        pin_token=pin_a,
        best_effort=True,
    ).returncode == 0
    assert _read_nofollow_bytes(generation_b_path) == generation_b
    assert _read_nofollow_bytes(inflight_b_path) == inflight_b
    assert _generation(state)["token"] == token_b
    revoked_release = _run(
        "verify-release",
        state,
        round_barcode="same",
        token=token_a,
    )
    assert revoked_release.returncode != 0
    assert "missing authenticated release receipt" in revoked_release.stderr


def test_existing_handoff_marker_rejects_a_displaced_generation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, acquisition_pin = _acquire(state)
    handoff = _run(
        "handoff", state, token=token_a, pin_token=acquisition_pin
    )
    assert handoff.returncode == 0, handoff.stderr

    replacement_token = hashlib.sha256(b"replacement-generation").hexdigest()
    generation_path = state / ".round_inflight.lockdir" / "generation.tsv"
    generation = _read_record(generation_path, schema=GENERATION_SCHEMA)
    generation["token"] = replacement_token
    _rewrite_record(generation_path, generation, schema=GENERATION_SCHEMA)
    assert not _revocation(state, token_a).exists()

    late_handoff = _run(
        "handoff", state, token=token_a, pin_token=acquisition_pin
    )
    assert late_handoff.returncode != 0
    assert "round lock generation was displaced" in late_handoff.stderr
    assert _generation(state)["token"] == replacement_token
    assert _marker(state, token_a).is_file()


@pytest.mark.parametrize("malformed", [False, True])
def test_legacy_or_malformed_lock_requires_explicit_operator_quarantine(
    tmp_path: Path,
    malformed: bool,
) -> None:
    state = tmp_path / "state"
    lock_dir = state / ".round_inflight.lockdir"
    lock_dir.mkdir(parents=True)
    if malformed:
        (lock_dir / "generation.tsv").write_text(
            "not-a-valid-record\n", encoding="utf-8"
        )
    legacy_marker = state / ".round_lock_handoff.run_1"
    legacy_marker.write_bytes(b"legacy-marker\n")
    _backdate_lock(state)

    before = {
        path.relative_to(state).as_posix(): (
            os.lstat(path).st_dev,
            os.lstat(path).st_ino,
            _read_nofollow_bytes(path) if path.is_file() else None,
        )
        for path in [state, *sorted(state.rglob("*"))]
    }
    blocked = subprocess.run(
        _command(
            "acquire", state, round_barcode="run_10", owner_pid=os.getpid(),
            stale_seconds=1,
        ),
        capture_output=True,
        check=False,
        env=_perl_test_env(),
    )
    assert blocked.returncode != 0
    expected_status = "parse-invalid" if malformed else "absent"
    _assert_exact_error_line(
        blocked.stderr,
        f"ERROR: round lock has {expected_status} generation state and "
        f"requires operator quarantine: {lock_dir}",
    )
    after = {
        path.relative_to(state).as_posix(): (
            os.lstat(path).st_dev,
            os.lstat(path).st_ino,
            _read_nofollow_bytes(path) if path.is_file() else None,
        )
        for path in [state, *sorted(state.rglob("*"))]
    }
    assert after == before
    assert _read_nofollow_bytes(legacy_marker) == b"legacy-marker\n"


def test_unknown_generation_host_is_unverifiable_and_never_reclaimed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, stale_seconds=1)
    generation_path = state / ".round_inflight.lockdir" / "generation.tsv"
    generation = _read_record(generation_path, schema=GENERATION_SCHEMA)
    generation["host"] = "unknown"
    _rewrite_record(generation_path, generation, schema=GENERATION_SCHEMA)
    before_generation = _read_nofollow_bytes(generation_path)
    ready_pin = (
        state / ".round_inflight.lockdir" / "pins" / f"ready.{pin_token}.tsv"
    )
    before_pin = _read_nofollow_bytes(ready_pin)

    contender = subprocess.run(
        _command(
            "acquire", state, round_barcode="unknown_host_contender",
            scope="full_round", owner_pid=os.getpid(), stale_seconds=1,
            wait_seconds=1,
        ),
        capture_output=True, check=False, env=_perl_test_env(), timeout=5,
    )
    assert contender.returncode != 0
    assert contender.stdout == b""
    _assert_exact_error_line(
        contender.stderr,
        "ERROR: round lock host identity is unverifiable; refusing automatic reclaim",
    )
    assert _read_nofollow_bytes(generation_path) == before_generation
    assert _read_nofollow_bytes(ready_pin) == before_pin
    assert _generation(state)["token"] == token
    assert not (state / ".round_inflight.lockdir" / "transition.tsv").exists()
    assert not list(state.glob(".round_lock_release.*.tsv"))
    assert not list(state.glob(".round_lock_revocation.*.tsv"))


@pytest.mark.parametrize(
    "failpoint", ["after-transition-install", "after-quarantine-rename"]
)
def test_killed_reclaimer_is_resumed_without_moving_replacement(
    tmp_path: Path,
    failpoint: str,
) -> None:
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, stale_seconds=1)
    assert _run(
        "handoff", state, token=token_a, pin_token=pin_a, stale_seconds=1
    ).returncode == 0
    _backdate_lock(state)

    env = _perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint)
    interrupted = subprocess.run(
        _command(
            "acquire",
            state,
            round_barcode="run_2",
            owner_pid=os.getpid(),
            stale_seconds=1,
            wait_seconds=1,
        ),
        capture_output=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])

    token_b, _pin_b = _acquire(
        state,
        round_barcode="run_3",
        stale_seconds=300,
        wait_seconds=1,
    )
    assert _generation(state)["token"] == token_b
    assert _revocation(state, token_a).is_file()
    reclaim_events = [
        event
        for event in _event_records(state)
        if event["generation_token"] == token_a and event["event"] == "reclaim"
    ]
    assert len(reclaim_events) == 1
    assert _marker(state, token_a).is_file()
    assert _run(
        "abort",
        state,
        token=token_a,
        pin_token=pin_a,
        best_effort=True,
    ).returncode == 0
    assert _generation(state)["token"] == token_b


def test_concurrent_reclaimers_install_one_replacement_generation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, _pin_a = _acquire(state, owner_pid=99999999, stale_seconds=300)
    contenders = [
        subprocess.Popen(
            _command(
                "acquire",
                state,
                round_barcode=f"contender_{index}",
                owner_pid=os.getpid(),
                stale_seconds=300,
                wait_seconds=1,
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for index in range(4)
    ]
    results = [process.communicate(timeout=5) for process in contenders]
    winners = [
        _parse_acquire_output(stdout)[0]
        for process, (stdout, _stderr) in zip(contenders, results)
        if process.returncode == 0
    ]
    assert len(winners) == 1
    assert _generation(state)["token"] == winners[0]
    assert _revocation(state, token_a).is_file()
    archives = list((state / ".round_lock_archives").glob("reclaim-*"))
    assert len(archives) == 1
    reclaim_events = [
        event
        for event in _event_records(state)
        if event["generation_token"] == token_a and event["event"] == "reclaim"
    ]
    assert len(reclaim_events) == 1


def _write_pause_module(root: Path) -> Path:
    module_dir = root / "perl-hook"
    module_dir.mkdir()
    (module_dir / "RTBioScanRoundLockPause.pm").write_text(
        r'''package RTBioScanRoundLockPause;
use strict;
use warnings;
use Time::HiRes qw(usleep);
BEGIN {
    no warnings 'redefine';
    *CORE::GLOBAL::rename = sub {
        my ($source, $destination) = @_;
        my $target = $ENV{RTBIOSCAN_TEST_RENAME_SOURCE} // '';
        if ($target ne '' && $source eq $target && index($destination, "$target.reclaim-") == 0) {
            open(my $fh, '>', $ENV{RTBIOSCAN_TEST_READY}) or die $!;
            print {$fh} "ready\n";
            close($fh);
            while (!-e $ENV{RTBIOSCAN_TEST_RELEASE}) { usleep(10_000); }
        }
        return CORE::rename($source, $destination);
    };
}
1;
''',
        encoding="utf-8",
    )
    return module_dir


def test_delayed_reclaimer_cannot_rename_new_generation(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, stale_seconds=1)
    assert _run(
        "handoff", state, token=token_a, pin_token=pin_a, stale_seconds=1
    ).returncode == 0
    _backdate_lock(state)

    module_dir = _write_pause_module(tmp_path)
    ready = tmp_path / "rename.ready"
    release = tmp_path / "rename.release"
    env = dict(os.environ)
    env["PERL5LIB"] = os.pathsep.join(
        part for part in [str(module_dir), env.get("PERL5LIB", "")] if part
    )
    env["PERL5OPT"] = " ".join(
        part for part in ["-MRTBioScanRoundLockPause", env.get("PERL5OPT", "")] if part
    )
    env["RTBIOSCAN_TEST_RENAME_SOURCE"] = str(state / ".round_inflight.lockdir")
    env["RTBIOSCAN_TEST_READY"] = str(ready)
    env["RTBIOSCAN_TEST_RELEASE"] = str(release)
    delayed = subprocess.Popen(
        _command(
            "acquire",
            state,
            round_barcode="run_2",
            owner_pid=os.getpid(),
            stale_seconds=1,
            wait_seconds=1,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    deadline = time.time() + 5
    while not ready.exists() and delayed.poll() is None and time.time() < deadline:
        time.sleep(0.01)
    assert ready.exists(), delayed.communicate(timeout=1)[1]

    contender = subprocess.Popen(
        _command(
            "acquire", state, round_barcode="run_3", owner_pid=os.getpid(),
            stale_seconds=300, wait_seconds=1,
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_perl_test_env(),
    )
    with pytest.raises(subprocess.TimeoutExpired):
        contender.communicate(timeout=0.25)
    release.write_text("continue\n", encoding="utf-8")
    delayed_stdout, delayed_stderr = delayed.communicate(timeout=5)
    assert delayed.returncode == 0, delayed_stderr
    token_b, _pin_b = _parse_acquire_output(delayed_stdout.encode("ascii"))
    contender_stdout, contender_stderr = contender.communicate(timeout=5)
    assert contender.returncode != 0, contender_stdout
    _assert_exact_error_line(
        contender_stderr,
        f"ERROR: timed out waiting for round lock "
        f"'{state / '.round_inflight.lockdir'}'",
    )
    assert _generation(state)["token"] == token_b
    assert len(list((state / ".round_lock_archives").glob("reclaim-*"))) == 1


def test_release_crash_recovers_authenticated_receipt_and_late_exit_is_safe(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, scope="dorado_only", stale_seconds=19)
    failpoint = "after-quarantine-rename"
    env = _perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint)
    interrupted = subprocess.run(
        _command(
            "early-release",
            state,
            scope="dorado_only",
            token=token_a,
            pin_token=pin_a,
            stale_seconds=19,
        ),
        capture_output=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])
    assert _marker(state, token_a).is_file()
    assert not _release_receipt(state, token_a).exists()

    token_b, _pin_b = _acquire(
        state, round_barcode="run_2", stale_seconds=30
    )
    receipt = _read_record(_release_receipt(state, token_a), schema=RELEASE_SCHEMA)
    assert receipt["reason"] == "dorado_only_early"
    assert receipt["effective_ttl_seconds"] == "19"
    assert list(state.glob(".round_inflight.lockdir.release-*")) == []
    generation_b_path = state / ".round_inflight.lockdir" / "generation.tsv"
    generation_b = _read_nofollow_bytes(generation_b_path)
    late_abort = _run(
        "abort",
        state,
        scope="dorado_only",
        token=token_a,
        pin_token=pin_a,
        best_effort=True,
    )
    assert late_abort.returncode != 0
    assert "release reason conflicts" in late_abort.stderr
    assert _read_nofollow_bytes(generation_b_path) == generation_b
    assert _generation(state)["token"] == token_b


@pytest.mark.parametrize(
    "failpoint", ["after-transition-install", "after-quarantine-rename"]
)
def test_full_release_crash_is_recovered_without_minting_a_new_pin(
    tmp_path: Path,
    failpoint: str,
) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    handoff = _run(
        "handoff", state, token=token, pin_token=acquisition_pin
    )
    assert handoff.returncode == 0, handoff.stderr
    finisher_pin = _pin(state, token, role="backup_update_and_clean")

    env = _perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint)
    interrupted = subprocess.run(
        _command("finish", state, token=token, pin_token=finisher_pin),
        capture_output=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])

    if failpoint == "after-transition-install":
        assert (state / ".round_inflight.lockdir" / "transition.tsv").is_file()
        recovered = _run("verify-release", state, token=token)
        assert recovered.returncode == 0, recovered.stderr
        assert recovered.stdout.strip() == "full_round_released"
    else:
        assert len(list(state.glob(".round_inflight.lockdir.release-*"))) == 1
        recovered = _run("finish", state, token=token)
        assert recovered.returncode == 0, recovered.stderr

    replay = _run("finish", state, token=token)
    assert replay.returncode == 0, replay.stderr
    assert _read_record(
        _release_receipt(state, token), schema=RELEASE_SCHEMA
    )["reason"] == "full_round_released"
    assert not _marker(state, token).exists()
    assert not (state / ".round_inflight.lockdir").exists()
    assert list(state.glob(".round_inflight.lockdir.release-*")) == []


def test_unpin_cannot_remove_the_pin_authorizing_a_pending_release(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    failpoint = "after-transition-install"
    interrupted = subprocess.run(
        _command(
            "early-release", state, token=token, pin_token=pin_token,
            scope="dorado_only", owner_pid=os.getpid(),
        ),
        capture_output=True, check=False,
        env=_perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint),
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])

    lock = state / ".round_inflight.lockdir"
    ready = lock / "pins" / f"ready.{pin_token}.tsv"
    transition = lock / "transition.tsv"
    ready_entry = os.lstat(ready)
    ready_bytes = _read_nofollow_bytes(ready)
    transition_entry = os.lstat(transition)
    transition_bytes = _read_nofollow_bytes(transition)

    rejected = _run(
        "unpin", state, token=token, pin_token=pin_token,
        scope="dorado_only", best_effort=True,
    )
    assert rejected.returncode != 0
    _assert_exact_error_line(
        rejected.stderr.encode(),
        "ERROR: cannot remove the process pin authorizing a pending release",
    )
    assert (os.lstat(ready).st_dev, os.lstat(ready).st_ino) == (
        ready_entry.st_dev, ready_entry.st_ino,
    )
    assert _read_nofollow_bytes(ready) == ready_bytes
    assert (os.lstat(transition).st_dev, os.lstat(transition).st_ino) == (
        transition_entry.st_dev, transition_entry.st_ino,
    )
    assert _read_nofollow_bytes(transition) == transition_bytes

    recovered = _run("verify-release", state, token=token, scope="dorado_only")
    assert recovered.returncode == 0, recovered.stderr
    assert recovered.stdout == "dorado_only_early\n"


def test_pending_release_still_allows_a_non_authorizing_worker_to_unpin(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    handoff = _run(
        "handoff", state, token=token, pin_token=acquisition_pin,
    )
    assert handoff.returncode == 0, handoff.stderr
    finisher_pin = _pin(state, token, role="backup_update_and_clean")
    worker_pin = _pin(state, token, role="state_writer")

    blocked = _run("finish", state, token=token, pin_token=finisher_pin)
    assert blocked.returncode != 0
    _assert_exact_error_line(
        blocked.stderr.encode(),
        "ERROR: release is blocked by another live generation pin",
    )
    transition = _read_record(
        state / ".round_inflight.lockdir" / "transition.tsv",
        schema=TRANSITION_SCHEMA,
    )
    assert transition["allowed_pin_token"] == finisher_pin

    removed = _run(
        "unpin", state, token=token, pin_token=worker_pin,
        best_effort=True,
    )
    assert removed.returncode == 0, removed.stderr
    assert not os.path.lexists(
        state / ".round_inflight.lockdir" / "pins"
        / f"ready.{worker_pin}.tsv"
    )

    finished = _run("finish", state, token=token, pin_token=finisher_pin)
    assert finished.returncode == 0, finished.stderr
    receipt = _read_record(_release_receipt(state, token), schema=RELEASE_SCHEMA)
    assert receipt["operation_token"] == transition["operation_token"]


def test_pending_release_recovery_revalidates_the_transition_pin_role(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    assert _run(
        "handoff", state, token=token, pin_token=acquisition_pin
    ).returncode == 0
    finisher_pin = _pin(state, token, role="backup_update_and_clean")
    failpoint = "after-transition-install"
    env = _perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint)
    interrupted = subprocess.run(
        _command("finish", state, token=token, pin_token=finisher_pin),
        capture_output=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])

    ready = (
        state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{finisher_pin}.tsv"
    )
    pin_record = _read_record(ready, schema=PIN_SCHEMA)
    pin_record["role"] = "state_writer"
    _rewrite_record(ready, pin_record, schema=PIN_SCHEMA)

    verified = _run("verify-release", state, token=token)
    assert verified.returncode != 0
    assert "pending release process pin role mismatch" in verified.stderr
    replacement = _run(
        "acquire",
        state,
        round_barcode="run_2",
        owner_pid=os.getpid(),
        wait_seconds=1,
    )
    assert replacement.returncode != 0
    assert "pending release process pin role mismatch" in replacement.stderr
    assert _generation(state)["token"] == token
    assert not _release_receipt(state, token).exists()


@pytest.mark.parametrize(
    "first_action,second_action,pending_reason,marker_present",
    [
        ("abort", "early-release", "pre_handoff_abort", False),
        ("early-release", "abort", "dorado_only_early", True),
    ],
)
def test_pending_release_reason_switch_is_rejected_before_mutation(
    tmp_path: Path,
    first_action: str,
    second_action: str,
    pending_reason: str,
    marker_present: bool,
) -> None:
    """A durable release intent must win before retry-specific side effects."""
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    interrupted = subprocess.run(
        _command(
            first_action,
            state,
            token=token,
            pin_token=pin_token,
            scope="dorado_only",
            owner_pid=os.getpid(),
        ),
        capture_output=True,
        check=False,
        env=_perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-transition-install"
        ),
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(
        interrupted.stderr,
        FAILPOINT_ERRORS["after-transition-install"],
    )

    lock = state / ".round_inflight.lockdir"
    transition_path = lock / "transition.tsv"
    transition = _read_record(transition_path, schema=TRANSITION_SCHEMA)
    assert transition["action"] == "release", transition
    assert transition["reason"] == pending_reason, transition
    assert transition["allowed_pin_token"] == pin_token, transition
    transition_entry = os.lstat(transition_path)
    transition_bytes = _read_nofollow_bytes(transition_path)
    marker = _marker(state, token)
    assert os.path.lexists(marker) is marker_present
    marker_bytes = _read_nofollow_bytes(marker) if marker_present else None
    ready_pin = lock / "pins" / f"ready.{pin_token}.tsv"
    ready_entry = os.lstat(ready_pin)
    ready_bytes = _read_nofollow_bytes(ready_pin)

    conflicting = subprocess.run(
        _command(
            second_action,
            state,
            token=token,
            pin_token=pin_token,
            scope="dorado_only",
            owner_pid=os.getpid(),
        ),
        capture_output=True,
        check=False,
        env=_perl_test_env(),
    )
    assert conflicting.returncode != 0, conflicting.stdout
    _assert_exact_error_line(
        conflicting.stderr,
        f"ERROR: release lost transition race for generation {token}",
    )
    assert (os.lstat(transition_path).st_dev, os.lstat(transition_path).st_ino) == (
        transition_entry.st_dev,
        transition_entry.st_ino,
    )
    assert _read_nofollow_bytes(transition_path) == transition_bytes
    assert os.path.lexists(marker) is marker_present
    if marker_present:
        assert _read_nofollow_bytes(marker) == marker_bytes
    assert (os.lstat(ready_pin).st_dev, os.lstat(ready_pin).st_ino) == (
        ready_entry.st_dev,
        ready_entry.st_ino,
    )
    assert _read_nofollow_bytes(ready_pin) == ready_bytes


def test_inflight_publish_retry_reuses_the_immutable_record_and_cleans_temp(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state)
    failpoint = "before-compat-publish"
    env = _perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint)
    interrupted = subprocess.run(
        _command(
            "inflight",
            state,
            token=token,
            pin_token=pin_token,
            read_file="/reads/run_1.pod5",
        ),
        capture_output=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])
    exact_path = state / f".round_inflight.{token}.tsv"
    exact_before = _read_record(exact_path, schema=INFLIGHT_SCHEMA)
    exact_before_bytes = _read_nofollow_bytes(exact_path)
    assert not (state / "round_inflight.txt").exists()
    assert list((state / ".round_inflight.lockdir").glob(".round_inflight.*.tmp")) == []

    time.sleep(1.1)
    retried = _run(
        "inflight",
        state,
        token=token,
        pin_token=pin_token,
        read_file="/reads/run_1.pod5",
    )
    assert retried.returncode == 0, retried.stderr
    exact_after = _read_record(exact_path, schema=INFLIGHT_SCHEMA)
    exact_after_bytes = _read_nofollow_bytes(exact_path)
    compat = _read_compat_inflight(state / "round_inflight.txt")
    assert exact_after == exact_before
    assert exact_after_bytes == exact_before_bytes
    assert compat["started_utc"] == exact_before["started_utc"]
    assert list((state / ".round_inflight.lockdir").glob(".round_inflight.*.tmp")) == []


def test_fast_release_paths_reject_a_different_pin_role(tmp_path: Path) -> None:
    dorado_state = tmp_path / "dorado-state"
    dorado_token, dorado_pin = _acquire(dorado_state, scope="dorado_only")
    dorado_ready = (
        dorado_state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{dorado_pin}.tsv"
    )
    dorado_record = _read_record(dorado_ready, schema=PIN_SCHEMA)
    dorado_record["role"] = "backup_update_and_clean"
    _rewrite_record(dorado_ready, dorado_record, schema=PIN_SCHEMA)
    early = _run(
        "early-release",
        dorado_state,
        scope="dorado_only",
        token=dorado_token,
        pin_token=dorado_pin,
    )
    assert early.returncode != 0
    assert "process pin role mismatch" in early.stderr
    assert (dorado_state / ".round_inflight.lockdir").is_dir()
    assert not _release_receipt(dorado_state, dorado_token).exists()

    abort_state = tmp_path / "abort-state"
    abort_token, abort_pin = _acquire(abort_state)
    abort_ready = (
        abort_state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{abort_pin}.tsv"
    )
    abort_record = _read_record(abort_ready, schema=PIN_SCHEMA)
    abort_record["role"] = "state_writer"
    _rewrite_record(abort_ready, abort_record, schema=PIN_SCHEMA)
    aborted = _run("abort", abort_state, token=abort_token, pin_token=abort_pin)
    assert aborted.returncode == 0, aborted.stderr
    assert (abort_state / ".round_inflight.lockdir").is_dir()
    assert not _release_receipt(abort_state, abort_token).exists()


def test_tampered_release_receipt_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    assert _run(
        "early-release",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    ).returncode == 0
    receipt = _release_receipt(state, token)
    receipt.write_bytes(
        _read_nofollow_bytes(receipt).replace(
            b"reason\tdorado_only_early", b"reason\tforged"
        )
    )
    verified = _run("verify-release", state, scope="dorado_only", token=token)
    assert verified.returncode != 0
    assert "malformed record" in verified.stderr


def test_early_release_validates_an_existing_exact_marker(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state, scope="dorado_only")
    _rewrite_record(
        _marker(state, token),
        {
            "schema": "1",
            "token": token,
            "round_barcode": "wrong_round",
            "scope": "dorado_only",
            "outcome": "handoff",
            "created_epoch": "1",
        },
        schema=MARKER_SCHEMA,
    )
    released = _run(
        "early-release",
        state,
        scope="dorado_only",
        token=token,
        pin_token=pin_token,
    )
    assert released.returncode != 0
    assert "handoff marker does not match" in released.stderr
    assert (state / ".round_inflight.lockdir").is_dir()
    assert not _release_receipt(state, token).exists()


def test_pre_handoff_abort_is_not_a_resumable_release_receipt(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state)
    aborted = _run("abort", state, token=token, pin_token=pin_token)
    assert aborted.returncode == 0, aborted.stderr
    assert _read_record(
        _release_receipt(state, token), schema=RELEASE_SCHEMA
    )["reason"] == "pre_handoff_abort"
    verified = _run("verify-release", state, token=token)
    assert verified.returncode != 0
    assert "pre-handoff abort is not a resumable release" in verified.stderr


def test_impossible_full_round_abandonment_cannot_remove_handoff_marker(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    handoff = _run("handoff", state, token=token, pin_token=acquisition_pin)
    assert handoff.returncode == 0, handoff.stderr
    finisher_pin = _pin(state, token, role="backup_update_and_clean")

    interrupted = subprocess.run(
        _command("finish", state, token=token, pin_token=finisher_pin),
        capture_output=True,
        check=False,
        env=_perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-quarantine-rename"
        ),
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(
        interrupted.stderr,
        FAILPOINT_ERRORS["after-quarantine-rename"],
    )
    recovered = _run("verify-release", state, token=token)
    assert recovered.returncode == 0, recovered.stderr
    release = _read_record(_release_receipt(state, token), schema=RELEASE_SCHEMA)
    marker = _marker(state, token)
    marker_before = _read_nofollow_bytes(marker)

    _rewrite_record(
        _finish_receipt(state, token),
        {
            "schema": "1",
            "token": token,
            "round_barcode": "run_1",
            "scope": "full_round",
            "outcome": "abandoned",
            "disposition": "fast_post_release_failure",
            "release_reason": release["reason"],
            "release_operation_token": release["operation_token"],
            "release_transition_epoch": release["release_transition_epoch"],
            "lock_dev": release["lock_dev"],
            "lock_ino": release["lock_ino"],
            "finished_epoch": "1",
        },
        schema=FINISH_SCHEMA,
    )

    rejected = _run("verify-finish", state, token=token)
    assert rejected.returncode != 0, rejected.stdout
    assert "finish receipt does not match generation" in rejected.stderr
    assert _read_nofollow_bytes(marker) == marker_before


def test_dead_same_host_pid_is_reclaimed_without_waiting_for_ttl(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token_a, _pin_a = _acquire(state, owner_pid=99999999, stale_seconds=300)
    token_b, _pin_b = _acquire(
        state, round_barcode="run_2", stale_seconds=300, wait_seconds=1
    )
    assert token_b != token_a
    assert _revocation(state, token_a).is_file()


def test_foreign_host_pin_remains_blocking_after_lease_age(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token_a, acquisition_pin = _acquire(state, stale_seconds=1)
    handoff = _run(
        "handoff",
        state,
        token=token_a,
        pin_token=acquisition_pin,
        stale_seconds=1,
    )
    assert handoff.returncode == 0, handoff.stderr
    worker_pin = _pin(state, token_a, role="state_writer")
    ready_path = (
        state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{worker_pin}.tsv"
    )
    pin_record = _read_record(ready_path, schema=PIN_SCHEMA)
    pin_record["host"] = "foreign.example.invalid"
    pin_record["created_epoch"] = "1"
    _rewrite_record(ready_path, pin_record, schema=PIN_SCHEMA)
    _backdate_lock(state)
    os.utime(_marker(state, token_a), (1, 1), follow_symlinks=False)
    blocked = _run(
        "acquire",
        state,
        round_barcode="run_2",
        owner_pid=os.getpid(),
        stale_seconds=1,
        wait_seconds=1,
    )
    assert blocked.returncode != 0
    assert "timed out" in blocked.stderr
    assert _generation(state)["token"] == token_a
    assert not _revocation(state, token_a).exists()


def test_same_host_live_pin_remains_blocking_after_lease_age(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token_a, acquisition_pin = _acquire(state, stale_seconds=1)
    handoff = _run(
        "handoff",
        state,
        token=token_a,
        pin_token=acquisition_pin,
        stale_seconds=1,
    )
    assert handoff.returncode == 0, handoff.stderr
    worker_pin = _pin(state, token_a, role="state_writer")
    ready_path = (
        state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{worker_pin}.tsv"
    )
    pin_record = _read_record(ready_path, schema=PIN_SCHEMA)
    pin_record["created_epoch"] = "1"
    _rewrite_record(ready_path, pin_record, schema=PIN_SCHEMA)
    _backdate_lock(state)
    os.utime(_marker(state, token_a), (1, 1), follow_symlinks=False)

    blocked = _run(
        "acquire",
        state,
        round_barcode="run_2",
        owner_pid=os.getpid(),
        stale_seconds=1,
        wait_seconds=1,
    )
    assert blocked.returncode != 0
    assert "timed out" in blocked.stderr
    assert _generation(state)["token"] == token_a
    assert not _revocation(state, token_a).exists()


def test_unverifiable_live_pid_is_not_reclaimed_after_ttl(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, stale_seconds=1)
    assert _run(
        "unpin", state, token=token_a, pin_token=pin_a
    ).returncode == 0
    generation_path = state / ".round_inflight.lockdir" / "generation.tsv"
    generation = _read_record(generation_path, schema=GENERATION_SCHEMA)
    generation["process_start"] = "unavailable"
    _rewrite_record(generation_path, generation, schema=GENERATION_SCHEMA)
    _backdate_lock(state)

    blocked = _run(
        "acquire",
        state,
        round_barcode="run_2",
        owner_pid=os.getpid(),
        stale_seconds=1,
        wait_seconds=1,
    )
    assert blocked.returncode != 0
    assert "timed out" in blocked.stderr
    assert _generation(state)["token"] == token_a
    assert not _revocation(state, token_a).exists()


def test_verified_live_pid_wins_over_age_and_reused_pid_is_reclaimed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, stale_seconds=1)
    assert _run(
        "unpin", state, token=token_a, pin_token=pin_a
    ).returncode == 0
    generation_path = state / ".round_inflight.lockdir" / "generation.tsv"
    generation = _read_record(generation_path, schema=GENERATION_SCHEMA)
    parts = generation["process_start"].split(":")
    if len(parts) != 3 or parts[0] != "proc":
        pytest.skip("boot-aware process-start identity is unavailable")
    _backdate_lock(state)
    blocked = _run(
        "acquire",
        state,
        round_barcode="run_2",
        owner_pid=os.getpid(),
        stale_seconds=1,
        wait_seconds=1,
    )
    assert blocked.returncode != 0
    assert "timed out" in blocked.stderr
    assert _generation(state)["token"] == token_a

    generation = _read_record(generation_path, schema=GENERATION_SCHEMA)
    generation["process_start"] = f"proc:{parts[1]}:{int(parts[2]) + 1}"
    _rewrite_record(generation_path, generation, schema=GENERATION_SCHEMA)
    token_b, _pin_b = _acquire(
        state, round_barcode="run_3", stale_seconds=300, wait_seconds=1
    )
    assert token_b != token_a
    assert _revocation(state, token_a).is_file()


def test_malformed_identity_and_path_tokens_fail_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state)
    generation_path = state / ".round_inflight.lockdir" / "generation.tsv"
    generation_path.write_text("schema\t1\nrecord_sha256\t0\n", encoding="utf-8")
    guarded = _run("guard-pin", state, token=token, pin_token=pin_token)
    assert guarded.returncode != 0
    assert "parse-invalid generation state" in guarded.stderr
    assert "requires operator quarantine" in guarded.stderr

    bad_token = _run(
        "guard-pin", state, token="../../replacement", pin_token=pin_token
    )
    assert bad_token.returncode != 0
    assert "invalid generation token" in bad_token.stderr


def test_script_has_no_handoff_or_inflight_glob_cleanup() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert ".round_lock_handoff.*" not in text
    assert "round_lock_handoff.$round_barcode" not in text
    assert "glob(" not in text
    assert "unlink($path)" in text


def _captured_transition(state: Path) -> dict[str, str]:
    """Read the whole durable transition record, before recovery consumes it."""
    record = _read_record(
        state / ".round_inflight.lockdir" / "transition.tsv",
        schema=TRANSITION_SCHEMA,
    )
    assert record["started_epoch"].isdigit(), record
    _assert_token(record["operation_token"])
    return record


def test_release_receipt_and_event_both_equal_the_captured_transition_start(
    tmp_path: Path,
) -> None:
    """Both timestamps are anchored to the transition record on disk.

    Comparing the receipt against the release event is not sufficient: both are
    supplied from ``$transition->{started_epoch}`` at their own call sites, so a
    change to that shared source moves them together and a receipt-vs-event
    assertion still passes. This captures ``transition.tsv`` while the release
    is interrupted, then recovers, then requires both values to equal what was
    durably recorded before either existed.

    The field has now been named ``completed_epoch``, ``released_epoch``, and
    ``release_transition_epoch``; only the last describes the value.
    """
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state, scope="dorado_only")

    failpoint = "after-transition-install"
    env = _perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint)
    interrupted = subprocess.run(
        _command(
            "early-release", state, token=token, pin_token=acquisition_pin,
            scope="dorado_only", owner_pid=os.getpid(),
        ),
        capture_output=True, check=False, env=env,
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])
    captured = _captured_transition(state)

    recovered = _run(
        "verify-release", state, token=token, scope="dorado_only",
    )
    assert recovered.returncode == 0, recovered.stderr

    release_events = [
        event
        for event in _event_records(state)
        if event["generation_token"] == token and event["event"] == "release"
    ]
    assert len(release_events) == 1, release_events
    receipt = _read_record(_release_receipt(state, token), schema=RELEASE_SCHEMA)

    # Every value is anchored to the captured transition record, never to a
    # sibling output. Receipt and event are produced from the same source, so
    # comparing them to each other would pass even if both drifted together.
    assert receipt["release_transition_epoch"] == captured["started_epoch"]
    assert release_events[0]["event_epoch"] == captured["started_epoch"]
    assert receipt["operation_token"] == captured["operation_token"]
    assert release_events[0]["event_id"] == captured["operation_token"]


def test_revocation_receipt_and_event_both_equal_the_captured_reclaim_transition(
    tmp_path: Path,
) -> None:
    """The reclaim counterpart of the release binding test.

    ``reclaim_transition_epoch`` was renamed from ``revoked_epoch`` without any
    test referencing it, which is how ``completed_epoch`` survived two
    misleading names. Both the revocation receipt and the reclaim event are
    anchored to the captured transition record rather than to each other.
    """
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, stale_seconds=1)
    assert _run(
        "handoff", state, token=token_a, pin_token=pin_a, stale_seconds=1
    ).returncode == 0
    _backdate_lock(state)

    failpoint = "after-transition-install"
    env = _perl_test_env(RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint)
    interrupted = subprocess.run(
        _command(
            "acquire", state, round_barcode="run_2",
            owner_pid=os.getpid(), stale_seconds=1, wait_seconds=1,
        ),
        capture_output=True, check=False, env=env,
    )
    assert interrupted.returncode != 0
    _assert_exact_error_line(interrupted.stderr, FAILPOINT_ERRORS[failpoint])
    captured = _captured_transition(state)
    assert captured["action"] == "reclaim", captured

    recovered = _acquire(state, round_barcode="run_3", stale_seconds=1)
    assert recovered[0] != token_a

    receipt = _read_record(_revocation(state, token_a), schema=REVOCATION_SCHEMA)
    reclaim_events = [
        event
        for event in _event_records(state)
        if event["generation_token"] == token_a and event["event"] == "reclaim"
    ]
    assert len(reclaim_events) == 1, reclaim_events

    assert receipt["reclaim_transition_epoch"] == captured["started_epoch"]
    assert reclaim_events[0]["event_epoch"] == captured["started_epoch"]
    assert receipt["operation_token"] == captured["operation_token"]
    assert reclaim_events[0]["event_id"] == captured["operation_token"]
