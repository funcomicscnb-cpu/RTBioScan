from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"


def _command(
    action: str,
    state: Path,
    *,
    round_barcode: str = "run_1",
    scope: str = "full_round",
    token: str | None = None,
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


def _acquire(state: Path, **kwargs: object) -> str:
    kwargs.setdefault("owner_pid", os.getpid())
    result = _run("acquire", state, **kwargs)
    assert result.returncode == 0, result.stderr
    token = result.stdout.strip()
    assert len(token) == 64
    assert set(token) <= set("0123456789abcdef")
    return token


def _read_record(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    body: list[str] = []
    checksum = ""
    for line in lines:
        key, value = line.split("\t", 1)
        if key == "record_sha256":
            checksum = value
        else:
            assert key not in values
            values[key] = value
            body.append(f"{key}\t{value}\n")
    assert checksum == hashlib.sha256("".join(body).encode()).hexdigest()
    return values


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


def _owner(state: Path) -> dict[str, str]:
    return _read_record(state / ".round_inflight.lockdir" / "owner.tsv")


def _marker(state: Path, token: str) -> Path:
    return state / f".round_lock_handoff.{token}.tsv"


def _completion(state: Path, token: str) -> Path:
    return state / f".round_lock_completion.{token}.tsv"


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


def test_full_round_handoff_adoption_release_and_exit_are_generation_bound(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a = _acquire(state, stale_seconds=37)
    owner_a = _owner(state)
    lock_stat = (state / ".round_inflight.lockdir").stat()
    assert owner_a["token"] == token_a
    assert owner_a["round_barcode"] == "run_1"
    assert owner_a["scope"] == "full_round"
    assert owner_a["effective_ttl_seconds"] == "37"
    assert owner_a["lock_dev"] == str(lock_stat.st_dev)
    assert owner_a["lock_ino"] == str(lock_stat.st_ino)

    inflight = _run(
        "inflight", state, token=token_a, read_file="/reads/run_1.pod5"
    )
    assert inflight.returncode == 0, inflight.stderr
    diagnostic = _read_compat_inflight(state / "round_inflight.txt")
    assert diagnostic["round_barcode"] == "run_1"
    assert diagnostic["read_file"] == "/reads/run_1.pod5"
    assert diagnostic["generation_token"] == token_a

    handoff = _run("handoff", state, token=token_a, owner_pid=os.getpid())
    assert handoff.returncode == 0, handoff.stderr
    assert _owner(state)["phase"] == "handoff"
    assert _read_record(_marker(state, token_a))["round_barcode"] == "run_1"

    adopt = _run("adopt", state, token=token_a, owner_pid=os.getpid())
    assert adopt.returncode == 0, adopt.stderr
    assert _owner(state)["phase"] == "active"
    assert _run("guard", state, token=token_a).returncode == 0

    finish = _run("finish", state, token=token_a)
    assert finish.returncode == 0, finish.stderr
    assert not (state / ".round_inflight.lockdir").exists()
    retained_diagnostic = _read_compat_inflight(state / "round_inflight.txt")
    assert retained_diagnostic["generation_token"] == token_a
    assert not _marker(state, token_a).exists()
    completion = _read_record(_completion(state, token_a))
    assert completion["reason"] == "full_round_complete"
    assert completion["effective_ttl_seconds"] == "37"
    verified = _run("verify-completion", state, token=token_a)
    assert verified.returncode == 0, verified.stderr
    assert verified.stdout.strip() == "full_round_complete"

    token_b = _acquire(state, round_barcode="run_2")
    owner_b_bytes = (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes()
    late_exit = _run("abort", state, token=token_a, best_effort=True)
    assert late_exit.returncode == 0, late_exit.stderr
    assert (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes() == owner_b_bytes
    assert _owner(state)["token"] == token_b

    events = _event_records(state)
    a_events = {event["event"]: event for event in events if event["generation_token"] == token_a}
    assert {"acquire", "handoff", "adopt", "release"} <= set(a_events)
    assert a_events["release"]["outcome"] == "full_round_complete"
    assert all(event["scope"] == "full_round" for event in a_events.values())
    assert all(event["effective_ttl_seconds"] == "37" for event in a_events.values())


def test_dorado_only_marker_without_lock_and_prefix_cleanup_are_safe(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_1 = _acquire(state, round_barcode="run_1", scope="dorado_only")
    release_1 = _run(
        "early-release",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
    )
    assert release_1.returncode == 0, release_1.stderr
    marker_1 = _marker(state, token_1)
    assert marker_1.is_file()
    assert not (state / ".round_inflight.lockdir").exists()
    assert _run(
        "guard",
        state,
        round_barcode="run_1",
        scope="dorado_only",
        token=token_1,
    ).returncode == 0

    token_10 = _acquire(state, round_barcode="run_10", scope="dorado_only")
    release_10 = _run(
        "early-release",
        state,
        round_barcode="run_10",
        scope="dorado_only",
        token=token_10,
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
    token_a = _acquire(state, round_barcode="same", stale_seconds=1)
    assert _run(
        "handoff",
        state,
        round_barcode="same",
        token=token_a,
        owner_pid=os.getpid(),
        stale_seconds=1,
    ).returncode == 0
    _backdate_lock(state)

    token_b = _acquire(state, round_barcode="same", stale_seconds=1)
    assert token_b != token_a
    assert _revocation(state, token_a).is_file()
    assert _marker(state, token_a).is_file()
    assert _run(
        "inflight",
        state,
        round_barcode="same",
        token=token_b,
        read_file="/reads/replacement.pod5",
    ).returncode == 0
    owner_b = (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes()
    inflight_b = (state / "round_inflight.txt").read_bytes()

    for action in ("guard", "adopt", "finish"):
        result = _run(
            action,
            state,
            round_barcode="same",
            token=token_a,
            owner_pid=os.getpid() if action == "adopt" else None,
        )
        assert result.returncode != 0, action
    late_inflight = _run(
        "inflight",
        state,
        round_barcode="same",
        token=token_a,
        read_file="/reads/stale-a.pod5",
    )
    assert late_inflight.returncode != 0
    assert _run(
        "abort", state, round_barcode="same", token=token_a, best_effort=True
    ).returncode == 0
    assert (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes() == owner_b
    assert (state / "round_inflight.txt").read_bytes() == inflight_b
    assert _owner(state)["token"] == token_b


@pytest.mark.parametrize("malformed", [False, True])
def test_legacy_or_malformed_lock_is_ttl_quarantined_without_marker_glob(
    tmp_path: Path,
    malformed: bool,
) -> None:
    state = tmp_path / "state"
    lock_dir = state / ".round_inflight.lockdir"
    lock_dir.mkdir(parents=True)
    if malformed:
        (lock_dir / "owner.tsv").write_text("not-a-valid-record\n", encoding="utf-8")
    legacy_marker = state / ".round_lock_handoff.run_1"
    legacy_marker.write_bytes(b"legacy-marker\n")
    _backdate_lock(state)

    token = _acquire(state, round_barcode="run_10", stale_seconds=1)
    assert _owner(state)["token"] == token
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
    token_a = _acquire(state, stale_seconds=1)
    assert _run(
        "handoff", state, token=token_a, owner_pid=os.getpid(), stale_seconds=1
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

    token_b = _acquire(
        state,
        round_barcode="run_3",
        stale_seconds=300,
        wait_seconds=1,
    )
    assert _owner(state)["token"] == token_b
    assert _revocation(state, token_a).is_file()
    reclaim_events = [
        event
        for event in _event_records(state)
        if event["generation_token"] == token_a and event["event"] == "reclaim"
    ]
    assert len(reclaim_events) == 1
    assert _marker(state, token_a).is_file()
    assert _run("abort", state, token=token_a, best_effort=True).returncode == 0
    assert _owner(state)["token"] == token_b


def test_concurrent_reclaimers_install_one_replacement_generation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a = _acquire(state, owner_pid=99999999, stale_seconds=300)
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
        stdout.strip()
        for process, (stdout, _stderr) in zip(contenders, results)
        if process.returncode == 0
    ]
    assert len(winners) == 1
    assert _owner(state)["token"] == winners[0]
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
    token_a = _acquire(state, stale_seconds=1)
    assert _run(
        "handoff", state, token=token_a, owner_pid=os.getpid(), stale_seconds=1
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

    token_b = _acquire(
        state, round_barcode="run_3", stale_seconds=300, wait_seconds=1
    )
    owner_b = (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes()
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
    assert (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes() == owner_b
    assert _owner(state)["token"] == token_b
    assert len(list(state.glob(".round_inflight.lockdir.reclaim-*"))) == 1


def test_release_crash_recovers_authenticated_receipt_and_late_exit_is_safe(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a = _acquire(state, scope="dorado_only", stale_seconds=19)
    env = dict(os.environ)
    env["RTBIOSCAN_ROUND_LOCK_FAILPOINT"] = "after-quarantine-rename"
    interrupted = subprocess.run(
        _command(
            "early-release",
            state,
            scope="dorado_only",
            token=token_a,
            stale_seconds=19,
        ),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert interrupted.returncode != 0
    assert _marker(state, token_a).is_file()
    assert not _completion(state, token_a).exists()

    token_b = _acquire(state, round_barcode="run_2", stale_seconds=30)
    receipt = _read_record(_completion(state, token_a))
    assert receipt["reason"] == "dorado_only_early"
    assert receipt["effective_ttl_seconds"] == "19"
    owner_b = (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes()
    assert _run(
        "abort",
        state,
        scope="dorado_only",
        token=token_a,
        best_effort=True,
    ).returncode == 0
    assert (state / ".round_inflight.lockdir" / "owner.tsv").read_bytes() == owner_b
    assert _owner(state)["token"] == token_b


def test_tampered_completion_receipt_fails_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token = _acquire(state, scope="dorado_only")
    assert _run("early-release", state, scope="dorado_only", token=token).returncode == 0
    receipt = _completion(state, token)
    receipt.write_text(
        receipt.read_text(encoding="utf-8").replace(
            "reason\tdorado_only_early", "reason\tforged"
        ),
        encoding="utf-8",
    )
    guarded = _run("guard", state, scope="dorado_only", token=token)
    assert guarded.returncode != 0
    assert "malformed record" in guarded.stderr


def test_dead_same_host_pid_is_reclaimed_without_waiting_for_ttl(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token_a = _acquire(state, owner_pid=99999999, stale_seconds=300)
    token_b = _acquire(
        state, round_barcode="run_2", stale_seconds=300, wait_seconds=1
    )
    assert token_b != token_a
    assert _revocation(state, token_a).is_file()


def test_unverifiable_live_pid_falls_back_to_lease(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token_a = _acquire(state, stale_seconds=1)
    owner_path = state / ".round_inflight.lockdir" / "owner.tsv"
    owner = _read_record(owner_path)
    owner["process_start"] = "unavailable"
    _rewrite_record(owner_path, owner)
    _backdate_lock(state)

    token_b = _acquire(
        state, round_barcode="run_2", stale_seconds=1, wait_seconds=1
    )
    assert token_b != token_a
    assert _revocation(state, token_a).is_file()


def test_verified_live_pid_wins_over_age_and_reused_pid_is_reclaimed(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    token_a = _acquire(state, stale_seconds=1)
    owner_path = state / ".round_inflight.lockdir" / "owner.tsv"
    owner = _read_record(owner_path)
    parts = owner["process_start"].split(":")
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
    assert _owner(state)["token"] == token_a

    owner = _read_record(owner_path)
    owner["process_start"] = f"proc:{parts[1]}:{int(parts[2]) + 1}"
    _rewrite_record(owner_path, owner)
    token_b = _acquire(
        state, round_barcode="run_3", stale_seconds=300, wait_seconds=1
    )
    assert token_b != token_a
    assert _revocation(state, token_a).is_file()


def test_malformed_identity_and_path_tokens_fail_closed(tmp_path: Path) -> None:
    state = tmp_path / "state"
    token = _acquire(state)
    owner_path = state / ".round_inflight.lockdir" / "owner.tsv"
    owner_path.write_text("schema\t1\nrecord_sha256\t0\n", encoding="utf-8")
    guarded = _run("guard", state, token=token)
    assert guarded.returncode != 0
    assert "malformed record" in guarded.stderr or "generation was lost" in guarded.stderr

    bad_token = _run("guard", state, token="../../replacement")
    assert bad_token.returncode != 0
    assert "invalid generation token" in bad_token.stderr


def test_script_has_no_handoff_or_inflight_glob_cleanup() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert ".round_lock_handoff.*" not in text
    assert "round_lock_handoff.$round_barcode" not in text
    assert "glob(" not in text
    assert "unlink($path)" in text
