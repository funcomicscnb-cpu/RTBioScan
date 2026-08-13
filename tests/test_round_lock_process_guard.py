"""Focused contract tests for the state-writer round-lock shell guard."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATION_HELPER = REPO_ROOT / "bin" / "round_lock_generation.pl"
PROCESS_GUARD = REPO_ROOT / "bin" / "round_lock_process_guard.sh"


def _token(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _env(**extra: str) -> dict[str, str]:
    return {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "LC_CTYPE": "C",
        **extra,
    }


def _helper(
    action: str,
    state: Path,
    *,
    token: str | None = None,
    pin_token: str | None = None,
    scope: str = "full_round",
    owner_pid: int | None = None,
    role: str | None = None,
    stale_seconds: int = 30,
) -> subprocess.CompletedProcess[str]:
    command = [
        "perl",
        str(GENERATION_HELPER),
        action,
        "--state-dir",
        str(state),
        "--round-barcode",
        "run_1",
        "--scope",
        scope,
        "--wait-seconds",
        "1",
    ]
    if token is not None:
        command.extend(["--token", token])
    if pin_token is not None:
        command.extend(["--pin-token", pin_token])
    if owner_pid is not None:
        command.extend(["--owner-pid", str(owner_pid)])
    if role is not None:
        command.extend(["--role", role])
    if action == "acquire":
        command.extend(["--stale-seconds", str(stale_seconds)])
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=_env(),
    )


def _acquire(
    state: Path, *, scope: str = "full_round", stale_seconds: int = 30
) -> tuple[str, str]:
    generation_token = _token(f"generation:{state}:{scope}:{time.time_ns()}")
    pin_token = _token(f"acquisition-pin:{state}:{scope}:{time.time_ns()}")
    assert generation_token != pin_token
    result = _helper(
        "acquire",
        state,
        token=generation_token,
        pin_token=pin_token,
        scope=scope,
        owner_pid=os.getpid(),
        stale_seconds=stale_seconds,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == (
        f"generation_token={generation_token}\npin_token={pin_token}\n"
    )
    return generation_token, pin_token


def _handoff(state: Path, token: str, pin_token: str) -> None:
    result = _helper("handoff", state, token=token, pin_token=pin_token)
    assert result.returncode == 0, result.stderr


def _guard_env(
    state: Path,
    token: str,
    scope: str,
    body_marker: Path,
    *,
    helper: Path = GENERATION_HELPER,
    pin_file: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    return _env(
        RTBIOSCAN_TEST_PROCESS_GUARD=str(PROCESS_GUARD),
        RTBIOSCAN_TEST_BODY_MARKER=str(body_marker),
        RTBIOSCAN_ROUND_LOCK_STATE_DIR=str(state),
        RTBIOSCAN_ROUND_LOCK_ROUND_BARCODE="run_1",
        RTBIOSCAN_ROUND_LOCK_SCOPE=scope,
        RTBIOSCAN_ROUND_LOCK_GENERATION_TOKEN=token,
        RTBIOSCAN_ROUND_LOCK_HELPER=str(helper),
        RTBIOSCAN_ROUND_LOCK_PIN_TOKEN_FILE=str(
            pin_file or body_marker.with_suffix(".pin-token")
        ),
        **(extra or {}),
    )


def _run_guard(
    state: Path,
    token: str,
    scope: str,
    role: str,
    body_marker: Path,
    *,
    helper: Path = GENERATION_HELPER,
    unpin: bool = True,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    script = r'''
source "$RTBIOSCAN_TEST_PROCESS_GUARD"
rtbioscan_round_lock_pin "$RTBIOSCAN_TEST_ROLE"
printf 'completed=%s\npin=%s\n' \
    "$RTBIOSCAN_ROUND_LOCK_ALREADY_COMPLETED" \
    "$RTBIOSCAN_ROUND_LOCK_PIN_TOKEN" > "$RTBIOSCAN_TEST_BODY_MARKER"
if [ "$RTBIOSCAN_TEST_UNPIN" -eq 1 ]; then
    rtbioscan_round_lock_unpin
fi
'''
    return subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=_guard_env(
            state,
            token,
            scope,
            body_marker,
            helper=helper,
            extra={
                "RTBIOSCAN_TEST_ROLE": role,
                "RTBIOSCAN_TEST_UNPIN": "1" if unpin else "0",
                **(extra_env or {}),
            },
        ),
    )


def _age_lock(state: Path) -> None:
    lock_dir = state / ".round_inflight.lockdir"
    old = time.time() - 10
    for path in [lock_dir, *lock_dir.rglob("*")]:
        os.utime(path, (old, old), follow_symlinks=False)


def test_guard_is_bash_compatible_and_token_file_is_durable_and_fail_closed(
    tmp_path: Path,
) -> None:
    syntax = subprocess.run(
        ["bash", "-n", str(PROCESS_GUARD)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    token_file = tmp_path / "retained.token"
    hooks_cleared = tmp_path / "hooks-cleared"
    script = r'''
source "$RTBIOSCAN_TEST_PROCESS_GUARD"
[ -z "${RTBIOSCAN_ROUND_LOCK_FAILPOINT+x}" ]
[ -z "${RTBIOSCAN_ROUND_LOCK_TEST_ARBITRARY_EXTRA+x}" ]
printf 'hooks-cleared\n' > "$RTBIOSCAN_TEST_HOOKS_CLEARED"
rtbioscan_round_lock_prepare_token_file "$RTBIOSCAN_TEST_TOKEN_FILE" test-label
rtbioscan_round_lock_prepare_token_file "$RTBIOSCAN_TEST_TOKEN_FILE" ignored-on-replay
'''
    prepared = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=_env(
            RTBIOSCAN_TEST_PROCESS_GUARD=str(PROCESS_GUARD),
            RTBIOSCAN_TEST_TOKEN_FILE=str(token_file),
            RTBIOSCAN_TEST_HOOKS_CLEARED=str(hooks_cleared),
            RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-acquire-install",
            RTBIOSCAN_ROUND_LOCK_TEST_ARBITRARY_EXTRA="must-not-leak",
        ),
    )
    assert prepared.returncode == 0, prepared.stderr
    assert hooks_cleared.read_text(encoding="ascii") == "hooks-cleared\n"
    first, second = prepared.stdout.splitlines()
    assert first == second
    assert first == token_file.read_text(encoding="ascii").removesuffix("\n")
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert list(tmp_path.glob("retained.token.tmp-*")) == []

    malformed = tmp_path / "malformed.token"
    malformed.write_text("not-a-token\n", encoding="ascii")
    rejected = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=_env(
            RTBIOSCAN_TEST_PROCESS_GUARD=str(PROCESS_GUARD),
            RTBIOSCAN_TEST_TOKEN_FILE=str(malformed),
            RTBIOSCAN_TEST_HOOKS_CLEARED=str(hooks_cleared),
        ),
    )
    assert rejected.returncode != 0
    assert "malformed token file" in rejected.stderr

    symlink = tmp_path / "symlink.token"
    symlink.symlink_to(token_file)
    rejected_symlink = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=_env(
            RTBIOSCAN_TEST_PROCESS_GUARD=str(PROCESS_GUARD),
            RTBIOSCAN_TEST_TOKEN_FILE=str(symlink),
            RTBIOSCAN_TEST_HOOKS_CLEARED=str(hooks_cleared),
        ),
    )
    assert rejected_symlink.returncode != 0
    assert "not a regular non-symlink file" in rejected_symlink.stderr


def test_pin_response_loss_replays_exact_retained_request(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    _handoff(state, token, acquisition_pin)

    counter = tmp_path / "pin-attempts"
    shim = tmp_path / "response-loss-helper.pl"
    shim.write_text(
        r'''#!/usr/bin/env perl
use strict;
use warnings;
use Fcntl qw(:DEFAULT :flock);
my $real = $ENV{RTBIOSCAN_TEST_REAL_HELPER};
my $counter = $ENV{RTBIOSCAN_TEST_PIN_COUNTER};
if (($ARGV[0] // '') eq 'pin') {
    sysopen(my $fh, $counter, O_RDWR | O_CREAT, 0600) or die "$!\n";
    flock($fh, LOCK_EX) or die "$!\n";
    local $/;
    my $current = <$fh> // '0';
    my $attempt = int($current) + 1;
    seek($fh, 0, 0) or die "$!\n";
    truncate($fh, 0) or die "$!\n";
    print {$fh} "$attempt\n" or die "$!\n";
    close($fh) or die "$!\n";
    if ($attempt == 1) {
        open(STDOUT, '>', '/dev/null') or die "$!\n";
        my $status = system('perl', $real, @ARGV);
        exit($status == 0 ? 75 : ($status >> 8));
    }
}
exec('perl', $real, @ARGV) or die "$!\n";
''',
        encoding="utf-8",
    )

    body = tmp_path / "body"
    guarded = _run_guard(
        state,
        token,
        "full_round",
        "test_state_writer",
        body,
        helper=shim,
        extra_env={
            "RTBIOSCAN_TEST_REAL_HELPER": str(GENERATION_HELPER),
            "RTBIOSCAN_TEST_PIN_COUNTER": str(counter),
        },
    )
    assert guarded.returncode == 0, guarded.stderr
    assert counter.read_text(encoding="ascii") == "2\n"
    fields = dict(line.split("=", 1) for line in body.read_text().splitlines())
    retained = body.with_suffix(".pin-token").read_text(encoding="ascii").strip()
    assert fields == {"completed": "0", "pin": retained}
    assert list((state / ".round_inflight.lockdir" / "pins").glob("ready.*.tsv")) == []


def test_guard_runs_under_live_generation_and_blocks_displaced_generation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, acquisition_pin_a = _acquire(state, stale_seconds=1)
    _handoff(state, token_a, acquisition_pin_a)

    positive_body = tmp_path / "positive-body"
    positive = _run_guard(
        state, token_a, "full_round", "test_state_writer", positive_body
    )
    assert positive.returncode == 0, positive.stderr
    assert positive_body.is_file()

    _age_lock(state)
    token_b, _pin_b = _acquire(state, stale_seconds=1)
    assert token_b != token_a

    stale_body = tmp_path / "stale-body"
    stale = _run_guard(
        state, token_a, "full_round", "test_state_writer", stale_body
    )
    assert stale.returncode != 0
    assert "generation was displaced" in stale.stderr
    assert not stale_body.exists()


def test_dorado_release_and_finish_are_distinct_resume_states(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state, scope="dorado_only")
    released = _helper(
        "early-release",
        state,
        token=token,
        pin_token=acquisition_pin,
        scope="dorado_only",
    )
    assert released.returncode == 0, released.stderr

    writer_body = tmp_path / "writer-body"
    writer = _run_guard(
        state, token, "dorado_only", "test_state_writer", writer_body
    )
    assert writer.returncode == 0, writer.stderr
    assert writer_body.read_text() == "completed=0\npin=\n"

    backup_body = tmp_path / "backup-before-finish"
    backup = _run_guard(
        state, token, "dorado_only", "backup_update_and_clean", backup_body
    )
    assert backup.returncode == 0, backup.stderr
    assert backup_body.read_text() == "completed=0\npin=\n"

    finished = _helper("finish", state, token=token, scope="dorado_only")
    assert finished.returncode == 0, finished.stderr

    resumed_body = tmp_path / "backup-after-finish"
    resumed = _run_guard(
        state, token, "dorado_only", "backup_update_and_clean", resumed_body
    )
    assert resumed.returncode == 0, resumed.stderr
    assert resumed_body.read_text() == "completed=1\npin=\n"

    late_body = tmp_path / "late-writer"
    late = _run_guard(
        state, token, "dorado_only", "test_state_writer", late_body
    )
    assert late.returncode != 0
    assert "generation is already finished" in late.stderr
    assert not late_body.exists()


def test_backup_resumes_full_round_terminal_release_before_body(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    _handoff(state, token, acquisition_pin)
    backup_pin = _token(f"backup-pin:{state}")
    pinned = _helper(
        "pin",
        state,
        token=token,
        pin_token=backup_pin,
        owner_pid=os.getpid(),
        role="backup_update_and_clean",
    )
    assert pinned.returncode == 0, pinned.stderr
    assert pinned.stdout == f"{backup_pin}\n"
    finish_receipt = state / f".round_lock_finish.{token}.tsv"
    marker = state / f".round_lock_handoff.{token}.tsv"
    interrupted = subprocess.run(
        [
            "perl", str(GENERATION_HELPER), "finish",
            "--state-dir", str(state),
            "--round-barcode", "run_1",
            "--scope", "full_round",
            "--token", token,
            "--pin-token", backup_pin,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT=(
                "after-finish-install-before-marker-remove"
            )
        ),
    )
    assert interrupted.returncode != 0
    assert "injected failure after finish install" in interrupted.stderr
    assert finish_receipt.is_file()
    assert marker.is_file()

    body = tmp_path / "backup-body"
    resumed = _run_guard(
        state, token, "full_round", "backup_update_and_clean", body
    )
    assert resumed.returncode == 0, resumed.stderr
    assert body.read_text() == "completed=1\npin=\n"
    assert finish_receipt.is_file()
    assert not marker.exists()
    verified = _helper("verify-finish", state, token=token)
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout == "full_round_released\n"


def test_malformed_release_never_reaches_dorado_body(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state, scope="dorado_only")
    released = _helper(
        "early-release",
        state,
        token=token,
        pin_token=acquisition_pin,
        scope="dorado_only",
    )
    assert released.returncode == 0, released.stderr
    release_receipt = state / f".round_lock_release.{token}.tsv"
    release_receipt.write_text("not-an-authenticated-receipt\n", encoding="utf-8")

    body = tmp_path / "body"
    rejected = _run_guard(
        state, token, "dorado_only", "test_state_writer", body
    )
    assert rejected.returncode != 0
    assert "malformed record" in rejected.stderr
    assert not body.exists()


def test_guard_preserves_existing_exit_trap_and_failed_task_pin(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    _handoff(state, token, acquisition_pin)
    trap_marker = tmp_path / "trap-ran"
    body = tmp_path / "body"
    script = r'''
trap 'printf "trap-ran\n" > "$RTBIOSCAN_TEST_TRAP_MARKER"' EXIT
before=$(trap -p EXIT)
source "$RTBIOSCAN_TEST_PROCESS_GUARD"
after=$(trap -p EXIT)
[ "$before" = "$after" ]
rtbioscan_round_lock_pin test_state_writer
printf 'body-ran\n' > "$RTBIOSCAN_TEST_BODY_MARKER"
exit 23
'''
    failed = subprocess.run(
        ["/bin/bash", "-euo", "pipefail", "-c", script],
        capture_output=True,
        text=True,
        check=False,
        env=_guard_env(
            state,
            token,
            "full_round",
            body,
            extra={"RTBIOSCAN_TEST_TRAP_MARKER": str(trap_marker)},
        ),
    )
    assert failed.returncode == 23, failed.stderr
    assert body.read_text() == "body-ran\n"
    assert trap_marker.read_text() == "trap-ran\n"
    ready = list((state / ".round_inflight.lockdir" / "pins").glob("ready.*.tsv"))
    assert len(ready) == 1


def test_pin_stdout_is_validated_independently_before_body(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    _handoff(state, token, acquisition_pin)
    calls = tmp_path / "calls"
    shim = tmp_path / "wrong-output-helper.pl"
    shim.write_text(
        r'''#!/usr/bin/env perl
use strict;
use warnings;
open(my $fh, '>>', $ENV{RTBIOSCAN_TEST_PIN_COUNTER}) or die "$!\n";
print {$fh} "called\n" or die "$!\n";
close($fh) or die "$!\n";
print "0000000000000000000000000000000000000000000000000000000000000000\n";
''',
        encoding="utf-8",
    )
    body = tmp_path / "body"
    rejected = _run_guard(
        state,
        token,
        "full_round",
        "test_state_writer",
        body,
        helper=shim,
        extra_env={"RTBIOSCAN_TEST_PIN_COUNTER": str(calls)},
    )
    assert rejected.returncode != 0
    assert calls.read_text() == "called\n"
    assert "pin response did not match retained token" in rejected.stderr
    assert not body.exists()
