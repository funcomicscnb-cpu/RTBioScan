from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.round_lock_test_utils import assert_token as _assert_token
from tests.round_lock_test_utils import parse_acquire_output as _parse_acquire_output
from tests.round_lock_test_utils import read_record as _read_record


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"


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
    result = _run("acquire", state, **kwargs)
    assert result.returncode == 0, result.stderr
    return _parse_acquire_output(result.stdout)


def _pin(state: Path, token: str, *, role: str, **kwargs: object) -> str:
    kwargs.setdefault("owner_pid", os.getpid())
    result = _run("pin", state, token=token, role=role, **kwargs)
    assert result.returncode == 0, result.stderr
    pin_token = result.stdout.strip()
    _assert_token(pin_token)
    return pin_token


def _read_compat_inflight(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values = dict(line.split("=", 1) for line in lines)
    checksum = values.pop("record_sha256")
    body = "".join(f"{key}={value}\n" for key, value in values.items())
    assert checksum == hashlib.sha256(body.encode()).hexdigest()
    return values


def _rewrite_record(path: Path, values: dict[str, str]) -> None:
    body = "".join(f"{key}\t{value}\n" for key, value in values.items())
    path.write_text(
        body + f"record_sha256\t{hashlib.sha256(body.encode()).hexdigest()}\n",
        encoding="utf-8",
    )


def _generation(state: Path) -> dict[str, str]:
    return _read_record(state / ".round_inflight.lockdir" / "generation.tsv")


def _marker(state: Path, token: str) -> Path:
    return state / f".round_lock_handoff.{token}.tsv"


def _release_receipt(state: Path, token: str) -> Path:
    return state / f".round_lock_release.{token}.tsv"


def _revocation(state: Path, token: str) -> Path:
    return state / f".round_lock_revocation.{token}.tsv"


def _backdate_lock(state: Path) -> None:
    lock_dir = state / ".round_inflight.lockdir"
    for path in [lock_dir, *lock_dir.iterdir()]:
        os.utime(path, (1, 1), follow_symlinks=False)


def _event_records(state: Path) -> list[dict[str, str]]:
    return [
        _read_record(path)
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
        / f"ready.{acquisition_pin_a}.tsv"
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
    assert _read_record(_marker(state, token_a))["round_barcode"] == "run_1"
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
    receipt = _read_record(_release_receipt(state, token_a))
    assert receipt["reason"] == "full_round_released"
    assert receipt["effective_ttl_seconds"] == "37"
    verified = _run("verify-release", state, token=token_a)
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.strip() == "full_round_released"

    token_b, _pin_b = _acquire(state, round_barcode="run_2")
    generation_b_bytes = (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes()
    late_exit = _run(
        "abort",
        state,
        token=token_a,
        pin_token=acquisition_pin_a,
        best_effort=True,
    )
    assert late_exit.returncode != 0
    assert "release reason conflicts" in late_exit.stderr
    assert (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes() == generation_b_bytes
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
    marker_1_record = _read_record(marker_1)
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
    marker_10_bytes = marker_10.read_bytes()

    finish_1 = _run(
        "finish",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
    )
    assert finish_1.returncode == 0, finish_1.stderr
    assert not marker_1.exists()
    assert marker_10.read_bytes() == marker_10_bytes


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
    revocation_a = _read_record(_revocation(state, token_a))
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
    generation_b = (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes()
    inflight_b = (state / "round_inflight.txt").read_bytes()

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
    assert (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes() == generation_b
    assert (state / "round_inflight.txt").read_bytes() == inflight_b
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
    generation = _read_record(generation_path)
    generation["token"] = replacement_token
    _rewrite_record(generation_path, generation)
    assert not _revocation(state, token_a).exists()

    late_handoff = _run(
        "handoff", state, token=token_a, pin_token=acquisition_pin
    )
    assert late_handoff.returncode != 0
    assert "round lock generation was displaced" in late_handoff.stderr
    assert _generation(state)["token"] == replacement_token
    assert _marker(state, token_a).is_file()


@pytest.mark.parametrize("malformed", [False, True])
def test_legacy_or_malformed_lock_is_ttl_quarantined_without_marker_glob(
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

    token, _pin = _acquire(state, round_barcode="run_10", stale_seconds=1)
    assert _generation(state)["token"] == token
    assert legacy_marker.read_bytes() == b"legacy-marker\n"
    quarantines = list(state.glob(".round_inflight.lockdir.reclaim-*"))
    assert len(quarantines) == 1
    legacy_events = [
        event
        for event in _event_records(state)
        if event["generation_token"] == "legacy" and event["event"] == "reclaim"
    ]
    assert len(legacy_events) == 1
    assert legacy_events[0]["outcome"] == "quarantined"
    assert legacy_events[0]["effective_ttl_seconds"] == "1"


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

    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = failpoint
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
        text=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    assert "injected failure" in interrupted.stderr

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
            text=True,
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
    quarantines = list(state.glob(".round_inflight.lockdir.reclaim-*"))
    assert len(quarantines) == 1
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

    token_b, _pin_b = _acquire(
        state, round_barcode="run_3", stale_seconds=300, wait_seconds=1
    )
    generation_b = (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes()
    replacement_paths = [
        state / ".round_inflight.lockdir",
        *(state / ".round_inflight.lockdir").iterdir(),
    ]
    future = time.time() + 3600
    for path in replacement_paths:
        os.utime(path, (future, future), follow_symlinks=False)
    release.write_text("continue\n", encoding="utf-8")
    delayed_stdout, delayed_stderr = delayed.communicate(timeout=5)
    assert delayed.returncode != 0, delayed_stdout
    assert "timed out" in delayed_stderr
    assert (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes() == generation_b
    assert _generation(state)["token"] == token_b
    assert len(list(state.glob(".round_inflight.lockdir.reclaim-*"))) == 1


def test_release_crash_recovers_authenticated_receipt_and_late_exit_is_safe(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a, pin_a = _acquire(state, scope="dorado_only", stale_seconds=19)
    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = "after-quarantine-rename"
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
        text=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    assert _marker(state, token_a).is_file()
    assert not _release_receipt(state, token_a).exists()

    token_b, _pin_b = _acquire(
        state, round_barcode="run_2", stale_seconds=30
    )
    receipt = _read_record(_release_receipt(state, token_a))
    assert receipt["reason"] == "dorado_only_early"
    assert receipt["effective_ttl_seconds"] == "19"
    assert list(state.glob(".round_inflight.lockdir.release-*")) == []
    generation_b = (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes()
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
    assert (
        state / ".round_inflight.lockdir" / "generation.tsv"
    ).read_bytes() == generation_b
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

    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = failpoint
    interrupted = subprocess.run(
        _command("finish", state, token=token, pin_token=finisher_pin),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    assert "injected failure" in interrupted.stderr

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
    assert _read_record(_release_receipt(state, token))["reason"] == "full_round_released"
    assert not _marker(state, token).exists()
    assert not (state / ".round_inflight.lockdir").exists()
    assert list(state.glob(".round_inflight.lockdir.release-*")) == []


def test_pending_release_recovery_revalidates_the_transition_pin_role(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, acquisition_pin = _acquire(state)
    assert _run(
        "handoff", state, token=token, pin_token=acquisition_pin
    ).returncode == 0
    finisher_pin = _pin(state, token, role="backup_update_and_clean")
    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = "after-transition-install"
    interrupted = subprocess.run(
        _command("finish", state, token=token, pin_token=finisher_pin),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0

    ready = (
        state
        / ".round_inflight.lockdir"
        / "pins"
        / f"ready.{finisher_pin}.tsv"
    )
    pin_record = _read_record(ready)
    pin_record["role"] = "state_writer"
    _rewrite_record(ready, pin_record)

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


def test_inflight_publish_retry_reuses_the_immutable_record_and_cleans_temp(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token, pin_token = _acquire(state)
    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = "before-compat-publish"
    interrupted = subprocess.run(
        _command(
            "inflight",
            state,
            token=token,
            pin_token=pin_token,
            read_file="/reads/run_1.pod5",
        ),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    assert "injected failure before compatibility inflight publish" in interrupted.stderr
    exact_path = state / f".round_inflight.{token}.tsv"
    exact_before = _read_record(exact_path)
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
    exact_after = _read_record(exact_path)
    compat = _read_compat_inflight(state / "round_inflight.txt")
    assert exact_after == exact_before
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
    dorado_record = _read_record(dorado_ready)
    dorado_record["role"] = "backup_update_and_clean"
    _rewrite_record(dorado_ready, dorado_record)
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
    abort_record = _read_record(abort_ready)
    abort_record["role"] = "state_writer"
    _rewrite_record(abort_ready, abort_record)
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
    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace(
            "reason\tdorado_only_early", "reason\tforged"
        ),
        encoding="utf-8",
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
    assert _read_record(_release_receipt(state, token))["reason"] == "pre_handoff_abort"
    verified = _run("verify-release", state, token=token)
    assert verified.returncode != 0
    assert "pre-handoff abort is not a resumable release" in verified.stderr


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
    pin_record = _read_record(ready_path)
    pin_record["host"] = "foreign.example.invalid"
    pin_record["created_epoch"] = "1"
    _rewrite_record(ready_path, pin_record)
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
    pin_record = _read_record(ready_path)
    pin_record["created_epoch"] = "1"
    _rewrite_record(ready_path, pin_record)
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
    generation = _read_record(generation_path)
    generation["process_start"] = "unavailable"
    _rewrite_record(generation_path, generation)
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
    generation = _read_record(generation_path)
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

    generation = _read_record(generation_path)
    generation["process_start"] = f"proc:{parts[1]}:{int(parts[2]) + 1}"
    _rewrite_record(generation_path, generation)
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
    assert "malformed record" in guarded.stderr or "generation was lost" in guarded.stderr

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
    record = _read_record(state / ".round_inflight.lockdir" / "transition.tsv")
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

    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = "after-transition-install"
    interrupted = subprocess.run(
        _command(
            "early-release", state, token=token, pin_token=acquisition_pin,
            scope="dorado_only", owner_pid=os.getpid(),
        ),
        capture_output=True, text=True, check=False, env=env,
    )
    assert interrupted.returncode != 0
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
    receipt = _read_record(_release_receipt(state, token))

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

    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = "after-transition-install"
    interrupted = subprocess.run(
        _command(
            "acquire", state, round_barcode="run_2",
            owner_pid=os.getpid(), stale_seconds=1, wait_seconds=1,
        ),
        capture_output=True, text=True, check=False, env=env,
    )
    assert interrupted.returncode != 0
    captured = _captured_transition(state)
    assert captured["action"] == "reclaim", captured

    recovered = _acquire(state, round_barcode="run_3", stale_seconds=1)
    assert recovered[0] != token_a

    receipt = _read_record(_revocation(state, token_a))
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
