"""Stopped-world recovery for structurally unrecoverable valid generations.

The operator command exercised here is deliberately distinct from
``operator-quarantine-invalid``.  A valid generation remains authoritative;
only an exact, stopped-world acknowledgement may preserve it outside the
canonical namespace when subordinate structure makes normal recovery
impossible.  Record parsing stays independent of the Perl validator.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import stat
import struct
import subprocess
import time
from pathlib import Path

import pytest

from tests.round_lock_test_utils import (
    EVENT_SCHEMA,
    GENERATION_SCHEMA,
    INFLIGHT_SCHEMA,
    MARKER_SCHEMA,
    OPERATOR_EVENT_SCHEMA,
    PIN_SCHEMA,
    RELEASE_SCHEMA,
    REVOCATION_SCHEMA,
    TRANSITION_SCHEMA,
    MalformedRecord,
    assert_exact_error_line,
    parse_acquire_output,
    perl_test_env,
    read_compat_inflight,
    read_nofollow_bytes,
    read_record,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
LOCK_NAME = ".round_inflight.lockdir"


def _token(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _manifest(root: Path) -> tuple[tuple[object, ...], ...]:
    """Capture the complete namespace without following symlinks."""
    paths = [root, *sorted(root.rglob("*"), key=lambda path: path.as_posix())]
    captured: list[tuple[object, ...]] = []
    for path in paths:
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


def _entry_evidence(path: Path) -> tuple[str, str, str]:
    """Independently derive kind, content SHA, and metadata fingerprint."""
    entry = os.lstat(path)
    if stat.S_ISLNK(entry.st_mode):
        kind = "symlink"
        link_target = os.readlink(path)
    elif stat.S_ISREG(entry.st_mode):
        kind = "regular"
        link_target = ""
    elif stat.S_ISDIR(entry.st_mode):
        kind = "directory"
        link_target = ""
    elif stat.S_ISFIFO(entry.st_mode):
        kind = "fifo"
        link_target = ""
    else:
        kind = "other"
        link_target = ""
    metadata = "\0".join(
        (
            kind,
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
            link_target,
        )
    ).encode("utf-8")
    content_sha = (
        hashlib.sha256(read_nofollow_bytes(path)).hexdigest()
        if kind == "regular"
        else "none"
    )
    return kind, content_sha, hashlib.sha256(metadata).hexdigest()


def _operator_entry_evidence(path: Path) -> tuple[str, str, str]:
    """Mirror one operator-evidence tuple, including absence/directories."""
    try:
        kind, content_sha, fingerprint = _entry_evidence(path)
    except FileNotFoundError:
        return "absent", "none", "none"
    if kind == "directory":
        content_sha = _directory_manifest_sha(path)
    return kind, content_sha, fingerprint


def _labeled_evidence_sha256(
    items: list[tuple[str, Path]],
) -> tuple[str, dict[Path, tuple[str, str, str]]]:
    """Reimplement the path-labeled finalization-evidence digest."""
    digest = hashlib.sha256()
    domain = b"RTBioScan-round-lock-operator-finalization-v1"
    digest.update(struct.pack(">I", len(domain)))
    digest.update(domain)
    evidence_by_path: dict[Path, tuple[str, str, str]] = {}
    for relative, path in items:
        evidence = _operator_entry_evidence(path)
        evidence_by_path[path] = evidence
        for value in (relative, *evidence):
            encoded = value.encode("utf-8")
            digest.update(struct.pack(">I", len(encoded)))
            digest.update(encoded)
    return digest.hexdigest(), evidence_by_path


def _pins_manifest_sha(pins: Path) -> str:
    """Reimplement the helper's recursive, no-follow manifest."""
    digest = hashlib.sha256()

    def visit(directory: Path, prefix: str) -> None:
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            relative = f"{prefix}/{path.name}" if prefix else path.name
            kind, content_sha, fingerprint = _entry_evidence(path)
            for value in (relative, kind, content_sha, fingerprint):
                encoded = value.encode("utf-8")
                digest.update(struct.pack(">I", len(encoded)))
                digest.update(encoded)
            if kind == "directory":
                visit(path, relative)

    visit(pins, "")
    return digest.hexdigest()


def _directory_manifest_sha(directory: Path) -> str:
    return _pins_manifest_sha(directory)


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=perl_test_env(),
        timeout=5,
    )


def _run_env(
    command: list[str], **extra_env: str,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        env=perl_test_env(**extra_env),
        timeout=5,
    )


def _acquire(
    state: Path,
    *,
    round_barcode: str,
    stale_seconds: int = 30,
    owner_pid: int | None = None,
    scope: str = "full_round",
) -> subprocess.CompletedProcess[bytes]:
    owner_pid = owner_pid or os.getpid()
    return _run(
        [
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
            str(owner_pid),
            "--stale-seconds",
            str(stale_seconds),
            "--wait-seconds",
            "1",
        ]
    )


def _guard(
    state: Path,
    *,
    round_barcode: str,
    generation_token: str,
    pin_token: str,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [
            "perl",
            str(SCRIPT),
            "guard-pin",
            "--state-dir",
            str(state),
            "--round-barcode",
            round_barcode,
            "--scope",
            "full_round",
            "--token",
            generation_token,
            "--pin-token",
            pin_token,
            "--wait-seconds",
            "1",
        ]
    )


def _verify_release(
    state: Path,
    *,
    round_barcode: str,
    generation_token: str,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [
            "perl",
            str(SCRIPT),
            "verify-release",
            "--state-dir",
            str(state),
            "--round-barcode",
            round_barcode,
            "--scope",
            "full_round",
            "--token",
            generation_token,
            "--wait-seconds",
            "1",
        ]
    )


def _early_release(
    state: Path,
    *,
    round_barcode: str,
    generation_token: str,
    pin_token: str,
    failpoint: str | None = None,
) -> subprocess.CompletedProcess[bytes]:
    command = [
        "perl", str(SCRIPT), "early-release", "--state-dir", str(state),
        "--round-barcode", round_barcode, "--scope", "dorado_only",
        "--token", generation_token, "--pin-token", pin_token,
        "--stale-seconds", "30", "--wait-seconds", "1",
    ]
    return _run_env(
        command,
        **(
            {"RTBIOSCAN_ROUND_LOCK_FAILPOINT": failpoint}
            if failpoint is not None else {}
        ),
    )


def _inflight(
    state: Path,
    *,
    round_barcode: str,
    generation_token: str,
    pin_token: str,
    read_file: str,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [
            "perl", str(SCRIPT), "inflight", "--state-dir", str(state),
            "--round-barcode", round_barcode, "--scope", "full_round",
            "--token", generation_token, "--pin-token", pin_token,
            "--read-file", read_file, "--wait-seconds", "1",
        ]
    )


def _unpin(
    state: Path,
    *,
    round_barcode: str,
    generation_token: str,
    pin_token: str,
) -> subprocess.CompletedProcess[bytes]:
    return _run(
        [
            "perl", str(SCRIPT), "unpin", "--state-dir", str(state),
            "--round-barcode", round_barcode, "--scope", "full_round",
            "--token", generation_token, "--pin-token", pin_token,
            "--wait-seconds", "1",
        ]
    )


def _abort_after_quarantine(
    state: Path,
    *,
    round_barcode: str,
    generation_token: str,
    pin_token: str,
    failpoint: str,
) -> subprocess.CompletedProcess[bytes]:
    return _run_env(
        [
            "perl", str(SCRIPT), "abort", "--state-dir", str(state),
            "--round-barcode", round_barcode, "--scope", "full_round",
            "--token", generation_token, "--pin-token", pin_token,
            "--stale-seconds", "30", "--wait-seconds", "1",
        ],
        RTBIOSCAN_ROUND_LOCK_FAILPOINT=failpoint,
    )


def _helper_created_release_orphan(
    tmp_path: Path,
    *,
    label: str,
) -> tuple[Path, Path, str, str, str, dict[str, str]]:
    state = tmp_path / "state"
    round_barcode = f"round_{label}"
    acquired = _acquire(
        state, round_barcode=round_barcode, scope="dorado_only",
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)
    interrupted = _early_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
        failpoint="after-quarantine-rename",
    )
    assert interrupted.returncode != 0
    assert_exact_error_line(
        interrupted.stderr, "ERROR: injected failure after quarantine rename",
    )
    orphans = list(state.glob(f"{LOCK_NAME}.release-*"))
    assert len(orphans) == 1, orphans
    orphan = orphans[0]
    transition = read_record(
        orphan / "transition.tsv", schema=TRANSITION_SCHEMA,
    )
    assert orphan.name == f"{LOCK_NAME}.release-{transition['operation_token']}"
    return (
        state, orphan, round_barcode, generation_token, pin_token, transition,
    )


def _helper_created_reclaim_orphan(
    tmp_path: Path,
    *,
    label: str,
) -> tuple[Path, Path, str, str, dict[str, str]]:
    exited_owner = subprocess.Popen(["perl", "-e", "exit 0"])
    exited_owner.wait(timeout=5)
    state, _lock, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path,
        label=label,
        stale_seconds=0,
        owner_pid=exited_owner.pid,
    )
    unpinned = _unpin(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    assert unpinned.returncode == 0, unpinned.stderr
    interrupted = _run_env(
        [
            "perl", str(SCRIPT), "acquire", "--state-dir", str(state),
            "--round-barcode", f"reclaim_{label}", "--scope", "full_round",
            "--owner-pid", str(os.getpid()), "--stale-seconds", "0",
            "--wait-seconds", "1",
        ],
        RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-quarantine-rename",
    )
    assert interrupted.returncode != 0
    assert_exact_error_line(
        interrupted.stderr, "ERROR: injected failure after quarantine rename",
    )
    orphans = list(state.glob(f"{LOCK_NAME}.reclaim-*"))
    assert len(orphans) == 1, orphans
    orphan = orphans[0]
    transition = read_record(
        orphan / "transition.tsv", schema=TRANSITION_SCHEMA,
    )
    assert orphan.name == f"{LOCK_NAME}.reclaim-{transition['operation_token']}"
    return state, orphan, round_barcode, generation_token, transition


def _finalization_items(
    state: Path,
    *,
    generation_token: str,
    transition: dict[str, str],
    canonical: bool,
) -> list[tuple[str, Path]]:
    action = transition["action"]
    operation_token = transition["operation_token"]
    items: list[tuple[str, Path]] = []
    if canonical:
        inflight_name = f".round_inflight.{generation_token}.tsv"
        inflight_path = state / inflight_name
        items.append((inflight_name, inflight_path))
        if os.path.lexists(inflight_path):
            items.append(("round_inflight.txt", state / "round_inflight.txt"))
    if action == "release":
        receipt_name = f".round_lock_release.{generation_token}.tsv"
        opposite_name = f".round_lock_revocation.{generation_token}.tsv"
    else:
        receipt_name = f".round_lock_revocation.{generation_token}.tsv"
        opposite_name = f".round_lock_release.{generation_token}.tsv"
    items.extend(
        (
            (receipt_name, state / receipt_name),
            (opposite_name, state / opposite_name),
            (
                f".round_lock_events/{generation_token}.{action}."
                f"{operation_token}.tsv",
                state / ".round_lock_events"
                / f"{generation_token}.{action}.{operation_token}.tsv",
            ),
            (
                f".round_lock_archives/{action}-{operation_token}",
                state / ".round_lock_archives" / f"{action}-{operation_token}",
            ),
        )
    )
    return items


def _assert_finalization_event_fields(
    state: Path,
    *,
    operation_token: str,
    status: str,
    sha256: str,
) -> None:
    audit = state / ".round_lock_operator_events"
    for phase in ("intent", "complete"):
        event = read_record(
            audit / f"{operation_token}.{phase}.tsv",
            schema=OPERATOR_EVENT_SCHEMA,
        )
        assert event["recovery_basis"] == "finalization-invalid"
        assert event["finalization_status"] == status
        assert event["finalization_sha256"] == sha256


def _assert_finalization_operator_success(
    state: Path,
    *,
    source: Path,
    generation_token: str,
    transition: dict[str, str],
    status: str,
    operation_label: str,
    finalization_items: list[tuple[str, Path]],
    marker_status: str | None = None,
    release_authority_status: str | None = None,
    expected_pins_status: str | None = None,
) -> tuple[str, str]:
    source_entry = os.lstat(source)
    source_manifest = _manifest(source)
    operation_token = _token(operation_label)
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
        source_name=None if source.name == LOCK_NAME else source.name,
    )
    finalization_sha, external_evidence = _labeled_evidence_sha256(
        finalization_items,
    )
    external_evidence = {
        path: evidence
        for path, evidence in external_evidence.items()
        if evidence[0] != "absent"
    }
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b""
    _assert_finalization_event_fields(
        state,
        operation_token=operation_token,
        status=status,
        sha256=finalization_sha,
    )
    return _assert_completed_quarantine(
        state,
        source=source,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="finalization-invalid",
        command=command,
        source_name=source.name,
        marker_status=(
            marker_status
            if marker_status is not None
            else ("valid" if transition["action"] == "release" else "absent")
        ),
        release_authority_status=(
            release_authority_status
            if release_authority_status is not None
            else (
                "valid"
                if transition["action"] == "release"
                else "not-applicable"
            )
        ),
        finalization_status=status,
        finalization_sha256=finalization_sha,
        expected_pins_status=expected_pins_status,
        transition_status=f"valid-{transition['action']}",
        preserved_external_evidence=external_evidence,
    )


def _acquired_state(
    tmp_path: Path,
    *,
    label: str,
    stale_seconds: int = 30,
    owner_pid: int | None = None,
) -> tuple[Path, Path, str, str, str]:
    state = tmp_path / "state"
    round_barcode = f"round_{label}"
    acquired = _acquire(
        state,
        round_barcode=round_barcode,
        stale_seconds=stale_seconds,
        owner_pid=owner_pid,
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)
    lock_dir = state / LOCK_NAME
    entry = os.lstat(lock_dir)
    generation = read_record(
        lock_dir / "generation.tsv", schema=GENERATION_SCHEMA
    )
    assert generation["token"] == generation_token
    assert generation["round_barcode"] == round_barcode
    assert generation["lock_dev"] == str(entry.st_dev)
    assert generation["lock_ino"] == str(entry.st_ino)

    # Positive control: these exact runtime arguments reach a healthy pin
    # before a fixture introduces subordinate structural corruption.
    healthy = _guard(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    assert healthy.returncode == 0, healthy.stderr
    return state, lock_dir, round_barcode, generation_token, pin_token


def _remove_exact_pin_namespace(lock_dir: Path, pin_token: str) -> None:
    pins = lock_dir / "pins"
    (pins / f"ready.{pin_token}.tsv").unlink()
    (pins / f"candidate.{pin_token}.tsv").unlink()
    pins.rmdir()
    assert not os.path.lexists(pins)


def _install_malformed_transition(lock_dir: Path) -> None:
    path = lock_dir / "transition.tsv"
    path.write_bytes(b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n")
    with pytest.raises(MalformedRecord, match="mismatch"):
        read_record(path, schema=TRANSITION_SCHEMA)


def _write_record(
    path: Path,
    values: dict[str, str],
    *,
    fields: tuple[str, ...],
) -> None:
    assert set(values) == set(fields)
    body = b"".join(
        key.encode("ascii") + b"\t" + values[key].encode("utf-8") + b"\n"
        for key in fields
    )
    path.write_bytes(
        body
        + b"record_sha256\t"
        + hashlib.sha256(body).hexdigest().encode("ascii")
        + b"\n"
    )


def _install_semantic_invalid_transition(
    lock_dir: Path,
    *,
    round_barcode: str,
    generation_token: str,
    pin_token: str,
) -> None:
    entry = os.lstat(lock_dir)
    _write_record(
        lock_dir / "transition.tsv",
        {
            "schema": "1",
            "action": "release",
            "operation_token": _token("semantic invalid transition operation"),
            "owner_token": _token("wrong transition owner"),
            "round_barcode": round_barcode,
            "scope": "full_round",
            "reason": "full_round_released",
            "effective_ttl_seconds": "30",
            "lock_dev": str(entry.st_dev),
            "lock_ino": str(entry.st_ino),
            "allowed_pin_token": pin_token,
            "started_epoch": "1",
        },
        fields=TRANSITION_SCHEMA.fields,
    )
    parsed = read_record(lock_dir / "transition.tsv", schema=TRANSITION_SCHEMA)
    assert parsed["owner_token"] != generation_token


def _unrecoverable_command(
    state: Path,
    *,
    lock_dev: int,
    lock_ino: int,
    generation_token: str,
    operation_token: str,
    omit: frozenset[str] = frozenset(),
    overrides: dict[str, str] | None = None,
    source_name: str | None = None,
) -> list[str]:
    values = {
        "expected-lock-dev": str(lock_dev),
        "expected-lock-ino": str(lock_ino),
        "expected-generation-token": generation_token,
        "operation-token": operation_token,
        "operator-label": "pytest stopped-world operator",
        "reason": "valid generation has unrecoverable subordinate state",
        "confirm-abandon-generation": generation_token,
    }
    values.update(overrides or {})
    if source_name is not None:
        values["source-name"] = source_name
    command = [
        "perl",
        str(SCRIPT),
        "operator-quarantine-unrecoverable",
        "--state-dir",
        str(state),
        "--wait-seconds",
        "1",
    ]
    for name, value in values.items():
        if name not in omit:
            command.extend((f"--{name}", value))
    if "confirm-stopped-world" not in omit:
        command.append("--confirm-stopped-world")
    return command


def _invalid_operator_command(
    state: Path,
    *,
    lock_dev: int,
    lock_ino: int,
    operation_token: str,
) -> list[str]:
    return [
        "perl",
        str(SCRIPT),
        "operator-quarantine-invalid",
        "--state-dir",
        str(state),
        "--expected-lock-dev",
        str(lock_dev),
        "--expected-lock-ino",
        str(lock_ino),
        "--operation-token",
        operation_token,
        "--operator-label",
        "pytest invalid-snapshot operator",
        "--reason",
        "attempted invalid-snapshot recovery",
        "--confirm-invalid-snapshot",
        "--wait-seconds",
        "1",
    ]


def _assert_runtime_corruption(
    state: Path,
    *,
    round_barcode: str,
    generation_token: str,
    pin_token: str,
) -> None:
    before = _manifest(state)
    failed = _guard(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    assert failed.returncode != 0, failed.stdout
    assert failed.stdout == b""
    assert _manifest(state) == before


def _assert_completed_quarantine(
    state: Path,
    *,
    source: Path,
    source_entry: os.stat_result,
    source_manifest: tuple[tuple[object, ...], ...],
    generation_token: str,
    operation_token: str,
    recovery_basis: str,
    command: list[str],
    source_name: str = LOCK_NAME,
    marker_status: str = "absent",
    release_authority_status: str = "not-applicable",
    finalization_status: str = "not-applicable",
    finalization_sha256: str = "none",
    expected_pins_status: str | None = None,
    transition_status: str | None = None,
    preserved_external_evidence: dict[Path, tuple[str, str, str]] | None = None,
) -> tuple[str, str]:
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    assert not os.path.lexists(source)
    destination_entry = os.lstat(destination)
    assert (destination_entry.st_dev, destination_entry.st_ino) == (
        source_entry.st_dev,
        source_entry.st_ino,
    )
    assert _manifest(destination) == source_manifest
    preserved_generation = read_record(
        destination / "generation.tsv", schema=GENERATION_SCHEMA
    )
    assert preserved_generation["token"] == generation_token

    audit = state / ".round_lock_operator_events"
    intent = read_record(
        audit / f"{operation_token}.intent.tsv", schema=OPERATOR_EVENT_SCHEMA
    )
    complete = read_record(
        audit / f"{operation_token}.complete.tsv", schema=OPERATOR_EVENT_SCHEMA
    )
    generation_kind, generation_sha, generation_fingerprint = _entry_evidence(
        destination / "generation.tsv"
    )
    transition_path = destination / "transition.tsv"
    try:
        transition_kind, transition_sha, transition_fingerprint = (
            _entry_evidence(transition_path)
        )
    except FileNotFoundError:
        transition_kind, transition_sha, transition_fingerprint = (
            "absent", "none", "none"
        )
    for event in (intent, complete):
        assert event["schema"] == "2"
        assert event["operation_token"] == operation_token
        assert event["source_name"] == source_name
        assert event["destination_name"] == destination.name
        assert event["lock_dev"] == str(source_entry.st_dev)
        assert event["lock_ino"] == str(source_entry.st_ino)
        assert event["tree_sha256"] == _directory_manifest_sha(destination)
        assert event["generation_status"] == "valid"
        assert event["generation_entry_kind"] == generation_kind == "regular"
        assert event["generation_sha256"] == generation_sha
        assert event["generation_entry_fingerprint"] == generation_fingerprint
        assert event["operation_kind"] == "abandon_unrecoverable_generation"
        assert event["expected_generation_token"] == generation_token
        assert event["recovery_basis"] == recovery_basis
        assert event["marker_status"] == marker_status
        if marker_status == "absent":
            assert event["marker_entry_kind"] == "absent"
            assert event["marker_sha256"] == "none"
            assert event["marker_entry_fingerprint"] == "none"
        else:
            marker = state / f".round_lock_handoff.{generation_token}.tsv"
            marker_kind, marker_sha, marker_fingerprint = _entry_evidence(marker)
            assert event["marker_entry_kind"] == marker_kind
            assert event["marker_sha256"] == marker_sha
            assert event["marker_entry_fingerprint"] == marker_fingerprint
        assert event["release_authority_status"] == release_authority_status
        assert event["finalization_status"] == finalization_status
        assert event["finalization_sha256"] == finalization_sha256
        if recovery_basis == "pins-missing":
            assert event["pins_status"] == "missing"
            assert event["pins_entry_kind"] == "absent"
            assert event["pins_sha256"] == "none"
            assert event["pins_entry_fingerprint"] == "none"
            assert event["transition_status"] == "absent"
        elif recovery_basis == "pins-unsafe":
            pins = destination / "pins"
            pins_kind, pins_sha, pins_fingerprint = _entry_evidence(pins)
            assert event["pins_status"] == "unsafe"
            assert event["pins_entry_kind"] == pins_kind
            assert event["pins_sha256"] == pins_sha
            assert event["pins_entry_fingerprint"] == pins_fingerprint
            assert event["transition_status"] == "absent"
        else:
            if expected_pins_status == "missing":
                assert event["pins_status"] == "missing"
                assert event["pins_entry_kind"] == "absent"
                assert event["pins_sha256"] == "none"
                assert event["pins_entry_fingerprint"] == "none"
            else:
                pins = destination / "pins"
                pins_kind, _pins_entry_sha, pins_fingerprint = _entry_evidence(pins)
                derived_pins_status = (
                    "record-invalid"
                    if recovery_basis == "pins-record-invalid"
                    else "healthy"
                )
                assert event["pins_status"] == (
                    expected_pins_status or derived_pins_status
                )
                assert event["pins_entry_kind"] == pins_kind == "directory"
                assert event["pins_sha256"] == _pins_manifest_sha(pins)
                assert event["pins_entry_fingerprint"] == pins_fingerprint
            if transition_status is None:
                transition_status = {
                    "pins-record-invalid": "absent",
                    "transition-parse-invalid": "parse-invalid",
                    "transition-semantic-invalid": "semantic-invalid",
                    "transition-orphan-invalid": "absent",
                    "marker-invalid": "absent",
                    "release-authority-invalid": "valid-release",
                }[recovery_basis]
            assert event["transition_status"] == transition_status
        assert event["transition_entry_kind"] == transition_kind
        assert event["transition_sha256"] == transition_sha
        assert event["transition_entry_fingerprint"] == transition_fingerprint
    assert intent["phase"] == "intent"
    assert intent["outcome"] == "prepared"
    assert complete["phase"] == "complete"
    assert complete["outcome"] == "quarantined"
    assert int(complete["event_epoch"]) >= int(intent["event_epoch"])
    generation_bytes = read_nofollow_bytes(destination / "generation.tsv")
    assert generation_sha == hashlib.sha256(generation_bytes).hexdigest()

    preserved_external_evidence = preserved_external_evidence or {}
    allowed_terminal_records = {
        path
        for path in preserved_external_evidence
        if path.parent == state
        and path.name.startswith(
            (".round_lock_release.", ".round_lock_revocation.")
        )
    }
    actual_terminal_records = {
        path
        for path in state.iterdir()
        if path.name.startswith(
            (".round_lock_release.", ".round_lock_revocation.")
        )
    }
    assert actual_terminal_records == allowed_terminal_records
    for path, expected_evidence in preserved_external_evidence.items():
        assert _operator_entry_evidence(path) == expected_evidence
    pending = state / ".round_lock_operator_pending"
    if pending.exists():
        assert list(pending.iterdir()) == []

    # The same fully bound operation is idempotent and does not rewrite its
    # preserved evidence or audit trail.
    before_replay = _manifest(state)
    replay = _run(command)
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout == b""
    assert _manifest(state) == before_replay
    for path, expected_evidence in preserved_external_evidence.items():
        assert _operator_entry_evidence(path) == expected_evidence

    replacement = _acquire(
        state,
        round_barcode=f"replacement_{recovery_basis}",
        stale_seconds=30,
    )
    assert replacement.returncode == 0, replacement.stderr
    replacement_token, _replacement_pin = parse_acquire_output(replacement.stdout)
    assert replacement_token != generation_token
    assert read_record(
        state / LOCK_NAME / "generation.tsv", schema=GENERATION_SCHEMA
    )["token"] == replacement_token
    assert _manifest(destination) == source_manifest
    for path, expected_evidence in preserved_external_evidence.items():
        assert _operator_entry_evidence(path) == expected_evidence
    return replacement_token, _replacement_pin


def test_stopped_world_quarantine_refuses_healthy_then_accepts_missing_pins(
    tmp_path: Path,
) -> None:
    exited_owner = subprocess.Popen(["perl", "-e", "exit 0"])
    exited_owner.wait(timeout=5)
    with pytest.raises(ProcessLookupError):
        os.kill(exited_owner.pid, 0)
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path,
        label="missing_pins",
        stale_seconds=0,
        owner_pid=exited_owner.pid,
    )
    source_entry = os.lstat(lock_dir)
    operation_token = _token("missing pins operator operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )

    # Negative half of the A/B: confirmations never authorize displacement of
    # a structurally healthy valid generation, even when its zero-second lease
    # and exited owner make ordinary liveness recovery immediately eligible.
    healthy_manifest = _manifest(state)
    refused = _run(command)
    assert refused.returncode != 0, refused.stdout
    assert refused.stdout == b""
    assert _manifest(state) == healthy_manifest

    _remove_exact_pin_namespace(lock_dir, pin_token)
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b""
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="pins-missing",
        command=command,
    )


def test_stopped_world_quarantine_ignores_inert_ready_like_names(
    tmp_path: Path,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label="inert_ready_name"
    )
    inert = lock_dir / "pins" / "ready.not-a-token.tsv"
    inert.write_bytes(b"operator evidence, not a ready pin\n")
    guarded = _guard(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    assert guarded.returncode == 0, guarded.stderr
    entry = os.lstat(lock_dir)
    command = _unrecoverable_command(
        state,
        lock_dev=entry.st_dev,
        lock_ino=entry.st_ino,
        generation_token=generation_token,
        operation_token=_token("inert ready name operator operation"),
    )
    before = _manifest(state)
    refused = _run(command)
    assert refused.returncode != 0, refused.stdout
    assert b"structurally recoverable generation" in refused.stderr
    assert _manifest(state) == before


def test_stopped_world_quarantine_accepts_parse_invalid_transition(
    tmp_path: Path,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label="malformed_transition"
    )
    _install_malformed_transition(lock_dir)
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token("malformed transition operator operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b""
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="transition-parse-invalid",
        command=command,
    )


def test_stopped_world_quarantine_accepts_semantic_invalid_transition(
    tmp_path: Path,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label="semantic_invalid_transition"
    )
    _install_semantic_invalid_transition(
        lock_dir,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    before_runtime = _manifest(state)
    failed = _verify_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
    )
    assert failed.returncode != 0, failed.stdout
    assert b"invalid release transition" in failed.stderr
    assert _manifest(state) == before_runtime

    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token("semantic invalid transition operator operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="transition-semantic-invalid",
        command=command,
    )


@pytest.mark.parametrize(
    ("corruption", "marker_status", "authority_status", "runtime_error"),
    [
        (
            "allowed-pin-missing", "valid", "pin-missing",
            b"pending release lost its authenticated process pin",
        ),
        (
            "marker-missing", "absent", "marker-missing",
            b"missing handoff marker",
        ),
        (
            "marker-invalid", "invalid", "marker-invalid",
            b"malformed record",
        ),
    ],
)
def test_stopped_world_quarantine_accepts_invalid_release_authority(
    tmp_path: Path,
    corruption: str,
    marker_status: str,
    authority_status: str,
    runtime_error: bytes,
) -> None:
    state = tmp_path / "state"
    round_barcode = f"release_authority_{corruption}"
    acquired = _acquire(
        state,
        round_barcode=round_barcode,
        scope="dorado_only",
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)
    interrupted = _early_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
        failpoint="after-transition-install",
    )
    assert interrupted.returncode != 0
    assert b"injected failure after transition install" in interrupted.stderr
    lock_dir = state / LOCK_NAME
    transition = read_record(lock_dir / "transition.tsv", schema=TRANSITION_SCHEMA)
    assert transition["action"] == "release"
    assert transition["allowed_pin_token"] == pin_token
    marker = state / f".round_lock_handoff.{generation_token}.tsv"
    assert marker.is_file()
    if corruption == "allowed-pin-missing":
        (lock_dir / "pins" / f"ready.{pin_token}.tsv").unlink()
    elif corruption == "marker-missing":
        marker.unlink()
    else:
        marker.write_bytes(b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n")

    before_runtime = _manifest(state)
    blocked = _acquire(state, round_barcode=f"blocked_{corruption}")
    assert blocked.returncode != 0, blocked.stdout
    assert runtime_error in blocked.stderr
    assert _manifest(state) == before_runtime

    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token(f"release authority operator {corruption}")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="release-authority-invalid",
        command=command,
        marker_status=marker_status,
        release_authority_status=authority_status,
    )


def test_dangling_pre_handoff_marker_fails_closed_then_can_be_preserved(
    tmp_path: Path,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = (
        _acquired_state(tmp_path, label="dangling_pre_handoff_marker")
    )
    interrupted = _run_env(
        [
            "perl", str(SCRIPT), "abort", "--state-dir", str(state),
            "--round-barcode", round_barcode, "--scope", "full_round",
            "--token", generation_token, "--pin-token", pin_token,
            "--stale-seconds", "30", "--wait-seconds", "1",
        ],
        RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-transition-install",
    )
    assert interrupted.returncode != 0
    assert b"injected failure after transition install" in interrupted.stderr
    transition = read_record(
        lock_dir / "transition.tsv", schema=TRANSITION_SCHEMA,
    )
    assert transition["action"] == "release"
    assert transition["reason"] == "pre_handoff_abort"
    marker = state / f".round_lock_handoff.{generation_token}.tsv"
    marker.symlink_to(tmp_path / "missing-marker-target")
    marker_entry = os.lstat(marker)
    assert stat.S_ISLNK(marker_entry.st_mode)
    assert not marker.exists()

    # Positive runtime control: this exact pending release is reached, and an
    # occupied-but-dangling marker pathname is a conflict rather than absence.
    before_runtime = _manifest(state)
    blocked = _verify_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
    )
    assert blocked.returncode != 0, blocked.stdout
    assert b"pre-handoff release conflicts" in blocked.stderr
    assert _manifest(state) == before_runtime

    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token("dangling pre-handoff marker operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="release-authority-invalid",
        command=command,
        marker_status="invalid",
        release_authority_status="marker-conflict",
        transition_status="valid-release",
    )


def test_invalid_standalone_marker_blocks_reclaim_then_can_be_preserved(
    tmp_path: Path,
) -> None:
    exited_owner = subprocess.Popen(["perl", "-e", "exit 0"])
    exited_owner.wait(timeout=5)
    state, lock_dir, _round_barcode, generation_token, _pin_token = (
        _acquired_state(
            tmp_path,
            label="invalid_standalone_marker",
            stale_seconds=0,
            owner_pid=exited_owner.pid,
        )
    )
    marker = state / f".round_lock_handoff.{generation_token}.tsv"
    marker.symlink_to(tmp_path / "missing-standalone-marker-target")
    marker_entry = os.lstat(marker)
    assert stat.S_ISLNK(marker_entry.st_mode)
    assert not marker.exists()

    before_runtime = _manifest(state)
    blocked = _acquire(state, round_barcode="blocked_by_invalid_marker")
    assert blocked.returncode != 0, blocked.stdout
    assert b"record is not a regular non-symlink file" in blocked.stderr
    assert _manifest(state) == before_runtime
    assert not list(state.glob(".round_lock_revocation.*.tsv"))

    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token("invalid standalone marker operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="marker-invalid",
        command=command,
        marker_status="invalid",
    )


def test_stopped_world_quarantine_accepts_valid_generation_orphan(
    tmp_path: Path,
) -> None:
    state, lock_dir, _round_barcode, generation_token, _pin_token = (
        _acquired_state(tmp_path, label="valid_generation_orphan")
    )
    orphan_operation = _token("missing reclaim transition")
    source_name = f"{LOCK_NAME}.reclaim-{orphan_operation}"
    orphan = state / source_name
    os.rename(lock_dir, orphan)
    source_entry = os.lstat(orphan)
    source_manifest = _manifest(orphan)

    before_runtime = _manifest(state)
    failed = _acquire(state, round_barcode="blocked_by_valid_orphan")
    assert failed.returncode != 0, failed.stdout
    assert b"quarantine lacks a valid transition" in failed.stderr
    assert _manifest(state) == before_runtime

    operation_token = _token("valid generation orphan operator operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
        source_name=source_name,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=orphan,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="transition-orphan-invalid",
        command=command,
        source_name=source_name,
    )


def test_orphan_mismatch_can_record_secondary_release_authority_failure(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    round_barcode = "orphan_with_secondary_release_failure"
    acquired = _acquire(
        state, round_barcode=round_barcode, scope="dorado_only",
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)
    interrupted = _early_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
        failpoint="after-transition-install",
    )
    assert interrupted.returncode != 0
    assert b"injected failure after transition install" in interrupted.stderr

    lock_dir = state / LOCK_NAME
    transition = read_record(
        lock_dir / "transition.tsv", schema=TRANSITION_SCHEMA,
    )
    assert transition["action"] == "release"
    source_name = f"{LOCK_NAME}.reclaim-{transition['operation_token']}"
    orphan = state / source_name
    os.rename(lock_dir, orphan)
    marker = state / f".round_lock_handoff.{generation_token}.tsv"
    marker.unlink()

    # The normal scanner reaches the orphan-name/action mismatch first.  The
    # missing marker is an independent secondary defect that audit evidence
    # must retain without making the event's primary basis unrepresentable.
    before_runtime = _manifest(state)
    blocked = _acquire(state, round_barcode="blocked_by_mismatched_orphan")
    assert blocked.returncode != 0, blocked.stdout
    assert b"quarantine name/action mismatch" in blocked.stderr
    assert _manifest(state) == before_runtime

    source_entry = os.lstat(orphan)
    source_manifest = _manifest(orphan)
    operation_token = _token("orphan secondary release authority operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
        source_name=source_name,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=orphan,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="transition-orphan-invalid",
        command=command,
        source_name=source_name,
        marker_status="absent",
        release_authority_status="marker-missing",
        transition_status="valid-release",
    )


def test_stopped_world_quarantine_refuses_recoverable_reclaim_orphan(
    tmp_path: Path,
) -> None:
    exited_owner = subprocess.Popen(["perl", "-e", "exit 0"])
    exited_owner.wait(timeout=5)
    state, _lock_dir, _round_barcode, _generation_token, _pin_token = (
        _acquired_state(
            tmp_path,
            label="recoverable_reclaim_orphan",
            owner_pid=exited_owner.pid,
        )
    )
    # Reach the exact helper-created orphan boundary, not a hand-built fixture.
    reclaim = _run_env(
        [
            "perl", str(SCRIPT), "acquire", "--state-dir", str(state),
            "--round-barcode", "reclaim_positive_control", "--scope",
            "full_round", "--owner-pid", str(os.getpid()),
            "--stale-seconds", "30", "--wait-seconds", "1",
        ],
        RTBIOSCAN_ROUND_LOCK_FAILPOINT="after-quarantine-rename",
    )
    assert reclaim.returncode != 0
    assert b"injected failure after quarantine rename" in reclaim.stderr
    orphans = list(state.glob(f"{LOCK_NAME}.reclaim-*"))
    assert len(orphans) == 1, orphans
    orphan = orphans[0]
    transition = read_record(orphan / "transition.tsv", schema=TRANSITION_SCHEMA)
    generation = read_record(orphan / "generation.tsv", schema=GENERATION_SCHEMA)
    source_entry = os.lstat(orphan)

    pins = orphan / "pins"
    for path in list(pins.iterdir()):
        path.unlink()
    pins.rmdir()
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation["token"],
        operation_token=_token("refuse recoverable reclaim orphan"),
        source_name=orphan.name,
    )
    before = _manifest(state)
    refused = _run(command)
    assert refused.returncode != 0, refused.stdout
    assert b"structurally recoverable generation" in refused.stderr
    assert _manifest(state) == before

    recovered = _acquire(state, round_barcode="reclaim_runtime_control")
    assert recovered.returncode == 0, recovered.stderr
    revocation = state / f".round_lock_revocation.{generation['token']}.tsv"
    assert revocation.is_file()
    assert not list(state.glob(f"{LOCK_NAME}.reclaim-*"))
    assert (state / ".round_lock_archives" / (
        f"reclaim-{transition['operation_token']}"
    )).is_dir()


def test_stopped_world_quarantine_refuses_recoverable_release_orphan(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    round_barcode = "recoverable_release_orphan"
    acquired = _acquire(
        state, round_barcode=round_barcode, scope="dorado_only",
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)
    interrupted = _early_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
        failpoint="after-quarantine-rename",
    )
    assert interrupted.returncode != 0
    assert b"injected failure after quarantine rename" in interrupted.stderr
    orphans = list(state.glob(f"{LOCK_NAME}.release-*"))
    assert len(orphans) == 1, orphans
    orphan = orphans[0]
    transition = read_record(orphan / "transition.tsv", schema=TRANSITION_SCHEMA)
    generation = read_record(orphan / "generation.tsv", schema=GENERATION_SCHEMA)
    assert transition["allowed_pin_token"] == pin_token

    # This exact-looking unrelated ready name is malformed. Terminal release
    # replay does not enumerate unrelated pins; only its captured authority pin
    # matters, so this evidence cannot authorize operator abandonment.
    unrelated = _token("unrelated malformed release orphan pin")
    (orphan / "pins" / f"ready.{unrelated}.tsv").write_bytes(
        b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n"
    )
    source_entry = os.lstat(orphan)
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=_token("refuse recoverable release orphan"),
        source_name=orphan.name,
    )
    before = _manifest(state)
    refused = _run(command)
    assert refused.returncode != 0, refused.stdout
    assert b"structurally recoverable generation" in refused.stderr
    assert _manifest(state) == before

    recovered = _acquire(state, round_barcode="release_runtime_control")
    assert recovered.returncode == 0, recovered.stderr
    assert (state / f".round_lock_release.{generation['token']}.tsv").is_file()
    assert not list(state.glob(f"{LOCK_NAME}.release-*"))
    assert (state / ".round_lock_archives" / (
        f"release-{transition['operation_token']}"
    )).is_dir()


def test_completed_orphan_quarantine_reconciles_through_runtime(
    tmp_path: Path,
) -> None:
    state, lock_dir, _round_barcode, generation_token, _pin_token = (
        _acquired_state(tmp_path, label="orphan_complete_reconciliation")
    )
    orphan_operation = _token("complete reconciliation missing transition")
    source_name = f"{LOCK_NAME}.release-{orphan_operation}"
    orphan = state / source_name
    os.rename(lock_dir, orphan)
    source_entry = os.lstat(orphan)
    source_manifest = _manifest(orphan)
    operation_token = _token("complete orphan reconciliation operation")
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
        source_name=source_name,
    )
    ready = tmp_path / "orphan-complete.ready"
    release = tmp_path / "orphan-complete.release"
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=perl_test_env(
            RTBIOSCAN_ROUND_LOCK_FAILPOINT=(
                "after-operator-complete-link-before-sync"
            ),
            RTBIOSCAN_ROUND_LOCK_TEST_READY=str(ready),
            RTBIOSCAN_ROUND_LOCK_TEST_RELEASE=str(release),
        ),
    )
    _wait_for_exact_pause(
        process, ready, failpoint="after-operator-complete-link-before-sync",
    )
    process.kill()
    _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL, stderr
    assert destination.is_dir()
    assert _manifest(destination) == source_manifest
    assert (state / ".round_lock_operator_pending" / (
        f"{operation_token}.tsv"
    )).is_file()

    replacement = _acquire(state, round_barcode="after_orphan_reconciliation")
    assert replacement.returncode == 0, replacement.stderr
    assert _manifest(destination) == source_manifest
    assert list((state / ".round_lock_operator_pending").iterdir()) == []


def test_stopped_world_quarantine_accepts_malformed_ready_pin(
    tmp_path: Path,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label="malformed_ready_pin"
    )
    ready = lock_dir / "pins" / f"ready.{pin_token}.tsv"
    ready.write_bytes(b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n")
    with pytest.raises(MalformedRecord, match="mismatch"):
        read_record(ready, schema=PIN_SCHEMA)
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token("malformed ready pin operator operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="pins-record-invalid",
        command=command,
    )


def test_operator_replay_rejects_preserved_tree_evidence_mutation(
    tmp_path: Path,
) -> None:
    state, lock_dir, _round_barcode, generation_token, pin_token = (
        _acquired_state(tmp_path, label="tree_evidence_mutation")
    )
    _remove_exact_pin_namespace(lock_dir, pin_token)
    evidence = lock_dir / ".transition-evidence.tmp"
    evidence.write_bytes(b"AAAA")
    entry = os.lstat(lock_dir)
    operation_token = _token("tree evidence mutation operation")
    command = _unrecoverable_command(
        state,
        lock_dev=entry.st_dev,
        lock_ino=entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    preserved = destination / evidence.name
    assert read_nofollow_bytes(preserved) == b"AAAA"

    preserved.write_bytes(b"BBBB")
    assert read_nofollow_bytes(preserved) == b"BBBB"
    before = _manifest(state)
    replay = _run(command)
    assert replay.returncode != 0, replay.stdout
    assert b"completed operator quarantine evidence changed" in replay.stderr
    assert _manifest(state) == before


def test_operator_tree_evidence_is_recursive_and_does_not_follow_symlinks(
    tmp_path: Path,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = (
        _acquired_state(tmp_path, label="recursive_tree_evidence")
    )
    _remove_exact_pin_namespace(lock_dir, pin_token)

    nested = lock_dir / ".operator-evidence" / "level-one" / "level-two"
    nested.mkdir(parents=True)
    nested_payload = nested / "payload.bin"
    nested_payload.write_bytes(b"nested-A\n")
    external = tmp_path / "external-target.bin"
    external.write_bytes(b"outside-A\n")
    external_link = nested / "external-link"
    external_link.symlink_to(external)

    # Positive control for eligibility: ordinary runtime recovery encounters
    # the missing pin namespace and fails closed before the operator command.
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )

    link_evidence = _entry_evidence(external_link)
    expected_tree_sha = _directory_manifest_sha(lock_dir)
    external_before = hashlib.sha256(external.read_bytes()).hexdigest()
    external.write_bytes(b"outside-B\n")
    external_after = hashlib.sha256(external.read_bytes()).hexdigest()
    assert external_after != external_before
    assert external.read_bytes()
    assert _entry_evidence(external_link) == link_evidence
    assert _directory_manifest_sha(lock_dir) == expected_tree_sha

    source_entry = os.lstat(lock_dir)
    operation_token = _token("recursive no-follow tree evidence operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == b""

    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    assert not os.path.lexists(lock_dir)
    destination_entry = os.lstat(destination)
    assert (destination_entry.st_dev, destination_entry.st_ino) == (
        source_entry.st_dev,
        source_entry.st_ino,
    )
    preserved_nested = (
        destination / ".operator-evidence" / "level-one" / "level-two"
    )
    preserved_payload = preserved_nested / "payload.bin"
    preserved_link = preserved_nested / "external-link"
    assert read_nofollow_bytes(preserved_payload) == b"nested-A\n"
    assert stat.S_ISLNK(os.lstat(preserved_link).st_mode)
    assert os.readlink(preserved_link) == str(external)
    assert _directory_manifest_sha(destination) == expected_tree_sha

    audit = state / ".round_lock_operator_events"
    intent = read_record(
        audit / f"{operation_token}.intent.tsv", schema=OPERATOR_EVENT_SCHEMA
    )
    complete = read_record(
        audit / f"{operation_token}.complete.tsv", schema=OPERATOR_EVENT_SCHEMA
    )
    assert intent["tree_sha256"] == expected_tree_sha
    assert complete["tree_sha256"] == expected_tree_sha
    assert intent["phase"] == "intent"
    assert intent["outcome"] == "prepared"
    assert complete["phase"] == "complete"
    assert complete["outcome"] == "quarantined"

    # Changing nonempty content reachable only through the symlink neither
    # changes the independently derived digest nor invalidates exact replay.
    external_before_replay = hashlib.sha256(external.read_bytes()).hexdigest()
    external.write_bytes(b"outside-C\n")
    external_after_replay = hashlib.sha256(external.read_bytes()).hexdigest()
    assert external_after_replay != external_before_replay
    assert external.read_bytes()
    assert _entry_evidence(preserved_link) == link_evidence
    assert _directory_manifest_sha(destination) == expected_tree_sha
    replay = _run(command)
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout == b""

    # Same-length nested mutation is independently visible and must make the
    # completed operation reject replay without changing any further state.
    preserved_payload.write_bytes(b"nested-B\n")
    assert read_nofollow_bytes(preserved_payload) == b"nested-B\n"
    assert _directory_manifest_sha(destination) != expected_tree_sha
    before_rejected_replay = _manifest(state)
    rejected = _run(command)
    assert rejected.returncode != 0, rejected.stdout
    assert b"completed operator quarantine evidence changed" in rejected.stderr
    assert _manifest(state) == before_rejected_replay


@pytest.mark.parametrize("entry_kind", ["regular", "symlink"])
def test_stopped_world_quarantine_accepts_unsafe_pin_namespace(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label=f"unsafe_pins_{entry_kind}"
    )
    _remove_exact_pin_namespace(lock_dir, pin_token)
    pins = lock_dir / "pins"
    if entry_kind == "regular":
        pins.write_bytes(b"not a pin directory\n")
    else:
        outside = tmp_path / "outside-pins"
        outside.mkdir()
        pins.symlink_to(outside, target_is_directory=True)
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token(f"unsafe {entry_kind} pins operator operation")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="pins-unsafe",
        command=command,
    )


@pytest.mark.parametrize(
    ("omit", "overrides"),
    [
        (frozenset({"expected-lock-dev"}), {}),
        (frozenset({"expected-lock-ino"}), {}),
        (frozenset({"expected-generation-token"}), {}),
        (frozenset({"confirm-stopped-world"}), {}),
        (frozenset({"confirm-abandon-generation"}), {}),
        (frozenset(), {"expected-lock-dev": "0"}),
        (frozenset(), {"expected-lock-ino": "0"}),
        (frozenset(), {"expected-generation-token": _token("wrong generation")}),
        (frozenset(), {"confirm-abandon-generation": _token("wrong abandon")}),
    ],
    ids=(
        "omit-dev",
        "omit-ino",
        "omit-generation",
        "omit-stopped-world",
        "omit-abandonment",
        "wrong-dev",
        "wrong-ino",
        "wrong-generation",
        "wrong-abandonment",
    ),
)
def test_stopped_world_quarantine_requires_exact_confirmations_and_identity(
    tmp_path: Path,
    omit: frozenset[str],
    overrides: dict[str, str],
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label="exact_confirmation"
    )
    _remove_exact_pin_namespace(lock_dir, pin_token)
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    entry = os.lstat(lock_dir)
    before = _manifest(state)
    operation_token = _token("exact confirmation operator operation")
    rejected = _run(
        _unrecoverable_command(
            state,
            lock_dev=entry.st_dev,
            lock_ino=entry.st_ino,
            generation_token=generation_token,
            operation_token=operation_token,
            omit=omit,
            overrides=overrides,
        )
    )
    assert rejected.returncode != 0, rejected.stdout
    assert rejected.stdout == b""
    assert _manifest(state) == before

    # Positive control for every parameterized rejection: the same fixture and
    # exact command without this mutation must reach the preservation path.
    accepted = _run(
        _unrecoverable_command(
            state,
            lock_dev=entry.st_dev,
            lock_ino=entry.st_ino,
            generation_token=generation_token,
            operation_token=operation_token,
        )
    )
    assert accepted.returncode == 0, accepted.stderr
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    assert (os.lstat(destination).st_dev, os.lstat(destination).st_ino) == (
        entry.st_dev,
        entry.st_ino,
    )


def test_invalid_snapshot_command_still_refuses_structurally_broken_valid_state(
    tmp_path: Path,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label="invalid_command_separation"
    )
    _remove_exact_pin_namespace(lock_dir, pin_token)
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    entry = os.lstat(lock_dir)
    before = _manifest(state)
    rejected = _run(
        _invalid_operator_command(
            state,
            lock_dev=entry.st_dev,
            lock_ino=entry.st_ino,
            operation_token=_token("invalid command separation"),
        )
    )
    assert rejected.returncode != 0, rejected.stdout
    assert rejected.stdout == b""
    assert _manifest(state) == before

    # The distinct command is reachable for the same structural state.
    accepted = _run(
        _unrecoverable_command(
            state,
            lock_dev=entry.st_dev,
            lock_ino=entry.st_ino,
            generation_token=generation_token,
            operation_token=_token("unrecoverable command separation"),
        )
    )
    assert accepted.returncode == 0, accepted.stderr


def _assert_absent(path: Path) -> None:
    """Require true ENOENT rather than hiding a dangling symlink."""
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return
    raise AssertionError(f"expected {path} absent, found mode {entry.st_mode:o}")


def _wait_for_exact_pause(
    process: subprocess.Popen[bytes],
    ready: Path,
    *,
    failpoint: str,
    timeout: float = 5.0,
) -> None:
    """Positive control that the child reached the requested helper span."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            entry = os.lstat(ready)
        except FileNotFoundError:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"operator exited before {failpoint}: "
                    f"rc={process.returncode}, stdout={stdout!r}, stderr={stderr!r}"
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
        assert stat.S_ISREG(entry.st_mode)
        assert not stat.S_ISLNK(entry.st_mode)
        assert read_nofollow_bytes(ready) == f"{failpoint}\n".encode("ascii")
        assert process.poll() is None
        return


def _assert_state_fence_is_held(state: Path) -> None:
    """Independently prove the paused operator owns the state flock."""
    descriptor = os.open(state, os.O_RDONLY)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)


def _assert_unrecoverable_operator_event(
    event: dict[str, str],
    *,
    operation_token: str,
    generation_token: str,
    source_entry: os.stat_result,
    destination_name: str,
    generation_sha256: str,
    generation_fingerprint: str,
    tree_sha256: str,
    phase: str,
    outcome: str,
    epoch_floor: int,
    epoch_ceiling: int,
) -> None:
    """Check every schema-2 value independently of the helper validator."""
    expected = {
        "schema": "2",
        "operation_token": operation_token,
        "operation_kind": "abandon_unrecoverable_generation",
        "expected_generation_token": generation_token,
        "recovery_basis": "pins-missing",
        "phase": phase,
        "operator_label": "pytest stopped-world operator",
        "reason": "valid generation has unrecoverable subordinate state",
        "source_name": LOCK_NAME,
        "destination_name": destination_name,
        "lock_dev": str(source_entry.st_dev),
        "lock_ino": str(source_entry.st_ino),
        "tree_sha256": tree_sha256,
        "generation_status": "valid",
        "generation_entry_kind": "regular",
        "generation_sha256": generation_sha256,
        "generation_entry_fingerprint": generation_fingerprint,
        "pins_status": "missing",
        "pins_entry_kind": "absent",
        "pins_sha256": "none",
        "pins_entry_fingerprint": "none",
        "transition_status": "absent",
        "transition_entry_kind": "absent",
        "transition_sha256": "none",
        "transition_entry_fingerprint": "none",
        "marker_status": "absent",
        "marker_entry_kind": "absent",
        "marker_sha256": "none",
        "marker_entry_fingerprint": "none",
        "release_authority_status": "not-applicable",
        "finalization_status": "not-applicable",
        "finalization_sha256": "none",
        "outcome": outcome,
    }
    assert set(event) == set(OPERATOR_EVENT_SCHEMA.fields)
    assert set(expected) == set(event) - {"event_epoch"}
    for field, value in expected.items():
        assert event[field] == value, field
    assert event["event_epoch"].isascii()
    assert event["event_epoch"].isdigit()
    assert epoch_floor <= int(event["event_epoch"]) <= epoch_ceiling


def _assert_unrecoverable_crash_boundary(
    state: Path,
    *,
    source: Path,
    destination: Path,
    source_entry: os.stat_result,
    source_manifest: tuple[tuple[object, ...], ...],
    operation_token: str,
    generation_token: str,
    generation_sha256: str,
    generation_fingerprint: str,
    source_visible: bool,
    destination_visible: bool,
    pending_visible: bool,
    complete_visible: bool,
    epoch_floor: int,
    epoch_ceiling: int,
    mutation_field: str,
) -> tuple[Path, Path]:
    """Assert one exact crash namespace and A/B its binding fields."""
    audit = state / ".round_lock_operator_events"
    pending_dir = state / ".round_lock_operator_pending"
    intent_path = audit / f"{operation_token}.intent.tsv"
    complete_path = audit / f"{operation_token}.complete.tsv"
    pending_path = pending_dir / f"{operation_token}.tsv"

    assert source_visible != destination_visible
    expected_top = {".round_lock_events", ".round_lock_operator_events"}
    expected_top.add(source.name if source_visible else destination.name)
    if pending_visible:
        expected_top.add(".round_lock_operator_pending")
    assert {path.name for path in state.iterdir()} == expected_top
    expected_audit = {intent_path.name}
    if complete_visible:
        expected_audit.add(complete_path.name)
    assert {path.name for path in audit.iterdir()} == expected_audit

    holder = source if source_visible else destination
    displaced = destination if source_visible else source
    holder_entry = os.lstat(holder)
    assert stat.S_ISDIR(holder_entry.st_mode)
    assert not stat.S_ISLNK(holder_entry.st_mode)
    assert (holder_entry.st_dev, holder_entry.st_ino) == (
        source_entry.st_dev,
        source_entry.st_ino,
    )
    assert _manifest(holder) == source_manifest
    tree_sha256 = _directory_manifest_sha(holder)
    _assert_absent(displaced)

    intent = read_record(intent_path, schema=OPERATOR_EVENT_SCHEMA)
    _assert_unrecoverable_operator_event(
        intent,
        operation_token=operation_token,
        generation_token=generation_token,
        source_entry=source_entry,
        destination_name=destination.name,
        generation_sha256=generation_sha256,
        generation_fingerprint=generation_fingerprint,
        tree_sha256=tree_sha256,
        phase="intent",
        outcome="prepared",
        epoch_floor=epoch_floor,
        epoch_ceiling=epoch_ceiling,
    )
    intent_entry = os.lstat(intent_path)
    assert stat.S_ISREG(intent_entry.st_mode)
    assert not stat.S_ISLNK(intent_entry.st_mode)
    assert intent_entry.st_nlink == (2 if pending_visible else 1)
    if pending_visible:
        assert {path.name for path in pending_dir.iterdir()} == {pending_path.name}
        pending_entry = os.lstat(pending_path)
        assert stat.S_ISREG(pending_entry.st_mode)
        assert not stat.S_ISLNK(pending_entry.st_mode)
        assert (pending_entry.st_dev, pending_entry.st_ino) == (
            intent_entry.st_dev,
            intent_entry.st_ino,
        )
        pending = read_record(pending_path, schema=OPERATOR_EVENT_SCHEMA)
        assert pending == intent
    else:
        _assert_absent(pending_dir)

    if complete_visible:
        complete = read_record(complete_path, schema=OPERATOR_EVENT_SCHEMA)
        _assert_unrecoverable_operator_event(
            complete,
            operation_token=operation_token,
            generation_token=generation_token,
            source_entry=source_entry,
            destination_name=destination.name,
            generation_sha256=generation_sha256,
            generation_fingerprint=generation_fingerprint,
            tree_sha256=tree_sha256,
            phase="complete",
            outcome="quarantined",
            epoch_floor=epoch_floor,
            epoch_ceiling=epoch_ceiling,
        )
        assert int(complete["event_epoch"]) >= int(intent["event_epoch"])
        complete_entry = os.lstat(complete_path)
        assert stat.S_ISREG(complete_entry.st_mode)
        assert not stat.S_ISLNK(complete_entry.st_mode)
        assert complete_entry.st_nlink == 1
    else:
        _assert_absent(complete_path)

    # A/B the two authority-bearing audit fields in an isolated in-memory copy.
    # The real record passed immediately above; this mutation must challenge the
    # exact same assertion used by every row in the crash matrix.
    mutated = dict(intent)
    mutation_value = (
        "invalid_snapshot"
        if mutation_field == "operation_kind"
        else "generation-invalid"
    )
    assert mutated[mutation_field] != mutation_value
    mutated[mutation_field] = mutation_value
    assert mutated[mutation_field] == mutation_value
    with pytest.raises(AssertionError, match=mutation_field):
        _assert_unrecoverable_operator_event(
            mutated,
            operation_token=operation_token,
            generation_token=generation_token,
            source_entry=source_entry,
            destination_name=destination.name,
            generation_sha256=generation_sha256,
            generation_fingerprint=generation_fingerprint,
            tree_sha256=tree_sha256,
            phase="intent",
            outcome="prepared",
            epoch_floor=epoch_floor,
            epoch_ceiling=epoch_ceiling,
        )

    assert not list(state.glob(".round_lock_release.*.tsv"))
    assert not list(state.glob(".round_lock_revocation.*.tsv"))
    return intent_path, complete_path


@pytest.mark.parametrize(
    (
        "failpoint,source_visible,destination_visible,pending_visible,"
        "complete_visible,mutation_field"
    ),
    [
        (
            "after-operator-intent-link-before-sync",
            True,
            False,
            False,
            False,
            "operation_kind",
        ),
        (
            "after-operator-intent",
            True,
            False,
            True,
            False,
            "recovery_basis",
        ),
        (
            "after-operator-rename-before-sync",
            False,
            True,
            True,
            False,
            "operation_kind",
        ),
        (
            "after-operator-sync-before-complete",
            False,
            True,
            True,
            False,
            "recovery_basis",
        ),
        (
            "after-operator-complete-link-before-sync",
            False,
            True,
            True,
            True,
            "operation_kind",
        ),
    ],
)
def test_stopped_world_quarantine_replays_every_operator_crash_boundary(
    tmp_path: Path,
    failpoint: str,
    source_visible: bool,
    destination_visible: bool,
    pending_visible: bool,
    complete_visible: bool,
    mutation_field: str,
) -> None:
    state, source, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label=f"crash_{failpoint}"
    )
    _remove_exact_pin_namespace(source, pin_token)
    _assert_runtime_corruption(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    source_entry = os.lstat(source)
    source_manifest = _manifest(source)
    generation_path = source / "generation.tsv"
    generation = read_record(generation_path, schema=GENERATION_SCHEMA)
    assert generation["token"] == generation_token
    assert generation["lock_dev"] == str(source_entry.st_dev)
    assert generation["lock_ino"] == str(source_entry.st_ino)
    generation_kind, generation_sha256, generation_fingerprint = _entry_evidence(
        generation_path
    )
    assert generation_kind == "regular"
    _assert_absent(source / "pins")
    _assert_absent(source / "transition.tsv")

    operation_token = _token(f"unrecoverable crash {failpoint}")
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    ready = tmp_path / f"{failpoint}.ready"
    release = tmp_path / f"{failpoint}.release"
    epoch_floor = int(time.time())
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
    _wait_for_exact_pause(process, ready, failpoint=failpoint)
    _assert_state_fence_is_held(state)
    process.send_signal(signal.SIGKILL)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL, (stdout, stderr)
    assert stdout == b""

    intent_path, complete_path = _assert_unrecoverable_crash_boundary(
        state,
        source=source,
        destination=destination,
        source_entry=source_entry,
        source_manifest=source_manifest,
        operation_token=operation_token,
        generation_token=generation_token,
        generation_sha256=generation_sha256,
        generation_fingerprint=generation_fingerprint,
        source_visible=source_visible,
        destination_visible=destination_visible,
        pending_visible=pending_visible,
        complete_visible=complete_visible,
        epoch_floor=epoch_floor,
        epoch_ceiling=int(time.time()),
        mutation_field=mutation_field,
    )

    if pending_visible and not complete_visible:
        # An incomplete stopped-world operation blocks every ordinary runtime
        # contender and directs replay through the distinct command.
        before_runtime = _manifest(state)
        blocked = _acquire(
            state,
            round_barcode=f"blocked_{failpoint}",
            stale_seconds=30,
        )
        assert blocked.returncode != 0, blocked.stdout
        assert blocked.stdout == b""
        assert blocked.stderr == (
            f"ERROR: incomplete operator quarantine {operation_token}; rerun "
            "operator-quarantine-unrecoverable with the original arguments\n"
        ).encode("ascii")
        assert _manifest(state) == before_runtime

    intent_before_replay = os.lstat(intent_path)
    intent_bytes_before_replay = read_nofollow_bytes(intent_path)
    complete_before_replay: tuple[os.stat_result, bytes] | None = None
    if complete_visible:
        complete_before_replay = (
            os.lstat(complete_path),
            read_nofollow_bytes(complete_path),
        )

    replay = _run(command)
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout == b""
    intent_after_replay = os.lstat(intent_path)
    assert (intent_after_replay.st_dev, intent_after_replay.st_ino) == (
        intent_before_replay.st_dev,
        intent_before_replay.st_ino,
    )
    assert read_nofollow_bytes(intent_path) == intent_bytes_before_replay
    if complete_before_replay is not None:
        complete_entry_before, complete_bytes_before = complete_before_replay
        complete_entry_after = os.lstat(complete_path)
        assert (complete_entry_after.st_dev, complete_entry_after.st_ino) == (
            complete_entry_before.st_dev,
            complete_entry_before.st_ino,
        )
        assert read_nofollow_bytes(complete_path) == complete_bytes_before

    assert (os.lstat(destination).st_dev, os.lstat(destination).st_ino) == (
        source_entry.st_dev,
        source_entry.st_ino,
    )
    assert _manifest(destination) == source_manifest
    assert not os.path.lexists(source)
    pending_dir = state / ".round_lock_operator_pending"
    assert pending_dir.is_dir()
    assert list(pending_dir.iterdir()) == []
    intent_entry = os.lstat(intent_path)
    complete_entry = os.lstat(complete_path)
    assert intent_entry.st_nlink == 1
    assert complete_entry.st_nlink == 1
    final_ceiling = int(time.time())
    final_intent = read_record(intent_path, schema=OPERATOR_EVENT_SCHEMA)
    final_complete = read_record(complete_path, schema=OPERATOR_EVENT_SCHEMA)
    _assert_unrecoverable_operator_event(
        final_intent,
        operation_token=operation_token,
        generation_token=generation_token,
        source_entry=source_entry,
        destination_name=destination.name,
        generation_sha256=generation_sha256,
        generation_fingerprint=generation_fingerprint,
        tree_sha256=_directory_manifest_sha(destination),
        phase="intent",
        outcome="prepared",
        epoch_floor=epoch_floor,
        epoch_ceiling=final_ceiling,
    )
    _assert_unrecoverable_operator_event(
        final_complete,
        operation_token=operation_token,
        generation_token=generation_token,
        source_entry=source_entry,
        destination_name=destination.name,
        generation_sha256=generation_sha256,
        generation_fingerprint=generation_fingerprint,
        tree_sha256=_directory_manifest_sha(destination),
        phase="complete",
        outcome="quarantined",
        epoch_floor=epoch_floor,
        epoch_ceiling=final_ceiling,
    )
    assert int(final_complete["event_epoch"]) >= int(final_intent["event_epoch"])
    assert not list(state.glob(".round_lock_release.*.tsv"))
    assert not list(state.glob(".round_lock_revocation.*.tsv"))

    # This performs a second exact replay with an unchanged full manifest, then
    # proves the canonical namespace can host a fresh generation while the
    # quarantined inode and evidence remain untouched.
    _assert_completed_quarantine(
        state,
        source=source,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="pins-missing",
        command=command,
    )


@pytest.mark.parametrize(
    ("corruption", "authority_status", "expected_pins_status"),
    [
        ("reason-scope-invalid", "reason-scope-invalid", None),
        ("pin-invalid", "pin-invalid", "record-invalid"),
        ("pin-role-invalid", "pin-role-invalid", None),
    ],
)
def test_stopped_world_quarantine_preserves_remaining_release_authority_failures(
    tmp_path: Path,
    corruption: str,
    authority_status: str,
    expected_pins_status: str | None,
) -> None:
    state = tmp_path / "state"
    round_barcode = f"release_authority_{corruption}"
    acquired = _acquire(
        state,
        round_barcode=round_barcode,
        scope="dorado_only",
    )
    assert acquired.returncode == 0, acquired.stderr
    generation_token, pin_token = parse_acquire_output(acquired.stdout)

    # Reach a real durable release transition before changing one authority
    # input.  The independent record reader anchors the exact transition, pin,
    # and marker identities rather than asking the helper to validate itself.
    interrupted = _early_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
        failpoint="after-transition-install",
    )
    assert interrupted.returncode != 0
    assert_exact_error_line(
        interrupted.stderr, "ERROR: injected failure after transition install",
    )
    lock_dir = state / LOCK_NAME
    transition_path = lock_dir / "transition.tsv"
    ready_pin_path = lock_dir / "pins" / f"ready.{pin_token}.tsv"
    marker_path = state / f".round_lock_handoff.{generation_token}.tsv"

    transition = read_record(transition_path, schema=TRANSITION_SCHEMA)
    assert transition["action"] == "release"
    assert transition["owner_token"] == generation_token
    assert transition["round_barcode"] == round_barcode
    assert transition["scope"] == "dorado_only"
    assert transition["reason"] == "dorado_only_early"
    assert transition["allowed_pin_token"] == pin_token
    pin = read_record(ready_pin_path, schema=PIN_SCHEMA)
    assert pin["token"] == generation_token
    assert pin["pin_token"] == pin_token
    assert pin["round_barcode"] == round_barcode
    assert pin["scope"] == "dorado_only"
    assert pin["role"] == "fast_acquisition"
    marker = read_record(marker_path, schema=MARKER_SCHEMA)
    assert marker["token"] == generation_token
    assert marker["round_barcode"] == round_barcode
    assert marker["scope"] == "dorado_only"
    assert marker["outcome"] == "handoff"

    if corruption == "reason-scope-invalid":
        _write_record(
            transition_path,
            {**transition, "reason": "full_round_released"},
            fields=TRANSITION_SCHEMA.fields,
        )
        changed_transition = read_record(
            transition_path, schema=TRANSITION_SCHEMA,
        )
        assert changed_transition["scope"] == "dorado_only"
        assert changed_transition["reason"] == "full_round_released"
        expected_runtime_error = (
            "ERROR: pending release reason does not match generation scope: "
            f"{generation_token}"
        )
    elif corruption == "pin-invalid":
        _write_record(
            ready_pin_path,
            {**pin, "token": _token("foreign release pin generation")},
            fields=PIN_SCHEMA.fields,
        )
        changed_pin = read_record(ready_pin_path, schema=PIN_SCHEMA)
        assert changed_pin["pin_token"] == pin_token
        assert changed_pin["token"] != generation_token
        expected_runtime_error = (
            "ERROR: process pin does not match the active generation"
        )
    else:
        _write_record(
            ready_pin_path,
            {**pin, "role": "backup_update_and_clean"},
            fields=PIN_SCHEMA.fields,
        )
        changed_pin = read_record(ready_pin_path, schema=PIN_SCHEMA)
        assert changed_pin["token"] == generation_token
        assert changed_pin["pin_token"] == pin_token
        assert changed_pin["role"] == "backup_update_and_clean"
        expected_runtime_error = (
            "ERROR: pending release process pin role mismatch"
        )

    # The unaffected authority inputs are independently re-read after the
    # mutation so a fixture bug cannot silently remove or replace them.
    changed_transition = read_record(
        transition_path, schema=TRANSITION_SCHEMA,
    )
    assert changed_transition["allowed_pin_token"] == pin_token
    changed_marker = read_record(marker_path, schema=MARKER_SCHEMA)
    assert changed_marker == marker
    transition_evidence = _entry_evidence(transition_path)
    pin_evidence = _entry_evidence(ready_pin_path)
    marker_evidence = _entry_evidence(marker_path)

    # Positive control: ordinary recovery reaches this exact pending release
    # and names the authority defect without changing any durable state.
    before_runtime = _manifest(state)
    blocked = _acquire(state, round_barcode=f"blocked_{corruption}")
    assert blocked.returncode != 0, blocked.stdout
    assert blocked.stdout == b""
    assert_exact_error_line(blocked.stderr, expected_runtime_error)
    assert _manifest(state) == before_runtime

    source_entry = os.lstat(lock_dir)
    source_manifest = _manifest(lock_dir)
    operation_token = _token(f"remaining release authority {corruption}")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
    )
    completed = _run(command)
    assert completed.returncode == 0, completed.stderr
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    assert _entry_evidence(destination / "transition.tsv") == transition_evidence
    assert (
        _entry_evidence(destination / "pins" / f"ready.{pin_token}.tsv")
        == pin_evidence
    )
    assert _entry_evidence(marker_path) == marker_evidence
    _assert_completed_quarantine(
        state,
        source=lock_dir,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="release-authority-invalid",
        command=command,
        marker_status="valid",
        release_authority_status=authority_status,
        expected_pins_status=expected_pins_status,
    )


@pytest.mark.parametrize("corruption", ["malformed", "conflicting"])
def test_finalization_quarantine_preserves_invalid_release_receipt(
    tmp_path: Path,
    corruption: str,
) -> None:
    (
        state, orphan, _round_barcode, generation_token, _pin_token, transition,
    ) = _helper_created_release_orphan(
        tmp_path, label=f"release_receipt_{corruption}",
    )
    receipt_path = state / f".round_lock_release.{generation_token}.tsv"
    if corruption == "malformed":
        receipt_path.write_bytes(
            b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n"
        )
        with pytest.raises(MalformedRecord, match="mismatch"):
            read_record(receipt_path, schema=RELEASE_SCHEMA)
    else:
        expected = {
            "schema": "1",
            "token": generation_token,
            "round_barcode": transition["round_barcode"],
            "scope": transition["scope"],
            "outcome": "released",
            "reason": transition["reason"],
            "effective_ttl_seconds": transition["effective_ttl_seconds"],
            "release_transition_epoch": transition["started_epoch"],
            "lock_dev": transition["lock_dev"],
            "lock_ino": transition["lock_ino"],
            "operation_token": _token("conflicting release receipt operation"),
        }
        _write_record(
            receipt_path, expected, fields=RELEASE_SCHEMA.fields,
        )
        receipt = read_record(receipt_path, schema=RELEASE_SCHEMA)
        assert receipt["operation_token"] != transition["operation_token"]

    external_before = _operator_entry_evidence(receipt_path)
    orphan_before_runtime = _manifest(orphan)
    blocked = _acquire(state, round_barcode=f"blocked_release_{corruption}")
    assert blocked.returncode != 0, blocked.stdout
    assert blocked.stdout == b""
    runtime_error = (
        b"malformed record" if corruption == "malformed"
        else b"immutable release receipt conflicts"
    )
    assert runtime_error in blocked.stderr
    assert _manifest(orphan) == orphan_before_runtime
    assert _operator_entry_evidence(receipt_path) == external_before

    items = _finalization_items(
        state,
        generation_token=generation_token,
        transition=transition,
        canonical=False,
    )
    _assert_finalization_operator_success(
        state,
        source=orphan,
        generation_token=generation_token,
        transition=transition,
        status="receipt-invalid",
        operation_label=f"invalid release receipt operator {corruption}",
        finalization_items=items,
    )


@pytest.mark.parametrize("corruption", ["malformed", "conflicting"])
def test_finalization_quarantine_preserves_invalid_reclaim_revocation(
    tmp_path: Path,
    corruption: str,
) -> None:
    state, orphan, _round_barcode, generation_token, transition = (
        _helper_created_reclaim_orphan(
            tmp_path, label=f"reclaim_revocation_{corruption}",
        )
    )
    revocation_path = state / f".round_lock_revocation.{generation_token}.tsv"
    if corruption == "malformed":
        revocation_path.write_bytes(
            b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n"
        )
        with pytest.raises(MalformedRecord, match="mismatch"):
            read_record(revocation_path, schema=REVOCATION_SCHEMA)
        # Terminal orphan recovery does not consult pins. Their absence must
        # remain secondary evidence, not displace the exact malformed outcome
        # that prevents finalization.
        pins = orphan / "pins"
        for path in pins.iterdir():
            path.unlink()
        pins.rmdir()
        assert not os.path.lexists(pins)
    else:
        expected = {
            "schema": "1",
            "token": generation_token,
            "round_barcode": transition["round_barcode"],
            "scope": transition["scope"],
            "outcome": "revoked",
            "reason": transition["reason"],
            "effective_ttl_seconds": transition["effective_ttl_seconds"],
            "reclaim_transition_epoch": transition["started_epoch"],
            "lock_dev": transition["lock_dev"],
            "lock_ino": transition["lock_ino"],
            "operation_token": _token("conflicting reclaim receipt operation"),
        }
        _write_record(
            revocation_path, expected, fields=REVOCATION_SCHEMA.fields,
        )
        revocation = read_record(revocation_path, schema=REVOCATION_SCHEMA)
        assert revocation["operation_token"] != transition["operation_token"]

    external_before = _operator_entry_evidence(revocation_path)
    orphan_before_runtime = _manifest(orphan)
    blocked = _acquire(state, round_barcode=f"blocked_reclaim_{corruption}")
    assert blocked.returncode != 0, blocked.stdout
    assert blocked.stdout == b""
    runtime_error = (
        b"malformed record" if corruption == "malformed"
        else b"immutable revocation receipt conflicts"
    )
    assert runtime_error in blocked.stderr
    assert _manifest(orphan) == orphan_before_runtime
    assert _operator_entry_evidence(revocation_path) == external_before

    items = _finalization_items(
        state,
        generation_token=generation_token,
        transition=transition,
        canonical=False,
    )
    _assert_finalization_operator_success(
        state,
        source=orphan,
        generation_token=generation_token,
        transition=transition,
        status="receipt-invalid",
        operation_label=f"invalid reclaim revocation operator {corruption}",
        finalization_items=items,
        expected_pins_status=("missing" if corruption == "malformed" else None),
    )


@pytest.mark.parametrize("action", ["release", "reclaim"])
def test_finalization_quarantine_preserves_conflicting_exact_transition_event(
    tmp_path: Path,
    action: str,
) -> None:
    if action == "release":
        state, orphan, _round, generation_token, _pin, transition = (
            _helper_created_release_orphan(tmp_path, label="release_event_conflict")
        )
    else:
        state, orphan, _round, generation_token, transition = (
            _helper_created_reclaim_orphan(tmp_path, label="reclaim_event_conflict")
        )
    event_path = (
        state / ".round_lock_events"
        / f"{generation_token}.{action}.{transition['operation_token']}.tsv"
    )
    event_values = {
        "schema": "1",
        "event_id": transition["operation_token"],
        "generation_token": generation_token,
        "round_barcode": transition["round_barcode"],
        "scope": transition["scope"],
        "event": action,
        "outcome": (
            transition["reason"] if action == "release" else "wrong-outcome"
        ),
        "effective_ttl_seconds": transition["effective_ttl_seconds"],
        "event_epoch": transition["started_epoch"],
        "lock_dev": transition["lock_dev"],
        "lock_ino": transition["lock_ino"],
    }
    if action == "release":
        event_values["outcome"] = "wrong-release-outcome"
    _write_record(event_path, event_values, fields=EVENT_SCHEMA.fields)
    event = read_record(event_path, schema=EVENT_SCHEMA)
    assert event["outcome"] != (
        transition["reason"] if action == "release" else "quarantined"
    )
    external_before = _operator_entry_evidence(event_path)

    orphan_before_runtime = _manifest(orphan)
    blocked = _acquire(state, round_barcode=f"blocked_{action}_event")
    assert blocked.returncode != 0, blocked.stdout
    assert b"immutable event conflicts" in blocked.stderr
    assert _manifest(orphan) == orphan_before_runtime
    assert _operator_entry_evidence(event_path) == external_before

    items = _finalization_items(
        state,
        generation_token=generation_token,
        transition=transition,
        canonical=False,
    )
    _assert_finalization_operator_success(
        state,
        source=orphan,
        generation_token=generation_token,
        transition=transition,
        status="event-invalid",
        operation_label=f"conflicting {action} event operator",
        finalization_items=items,
    )


@pytest.mark.parametrize("kind", ["generation", "compat"])
def test_canonical_finalization_quarantine_preserves_invalid_inflight(
    tmp_path: Path,
    kind: str,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = (
        _acquired_state(tmp_path, label=f"canonical_inflight_{kind}")
    )
    published = _inflight(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
        read_file=f"/reads/{kind}.pod5",
    )
    assert published.returncode == 0, published.stderr
    inflight_path = state / f".round_inflight.{generation_token}.tsv"
    compat_path = state / "round_inflight.txt"
    inflight = read_record(inflight_path, schema=INFLIGHT_SCHEMA)
    compat = read_compat_inflight(compat_path)
    assert inflight == compat

    interrupted = _abort_after_quarantine(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
        failpoint="after-transition-install",
    )
    assert interrupted.returncode != 0
    assert_exact_error_line(
        interrupted.stderr, "ERROR: injected failure after transition install",
    )
    transition = read_record(
        lock_dir / "transition.tsv", schema=TRANSITION_SCHEMA,
    )
    assert transition["action"] == "release"
    assert transition["reason"] == "pre_handoff_abort"

    corrupted_path = inflight_path if kind == "generation" else compat_path
    if kind == "generation":
        inflight_path.write_bytes(
            b"round_barcode\tbroken\nrecord_sha256\t" + b"0" * 64 + b"\n"
        )
        with pytest.raises(MalformedRecord, match="mismatch"):
            read_record(inflight_path, schema=INFLIGHT_SCHEMA)
        expected_status = "inflight-invalid"
        expected_runtime_error = b"malformed record"
    else:
        compat_path.write_bytes(b"malformed compatibility inflight\n")
        with pytest.raises(MalformedRecord):
            read_compat_inflight(compat_path)
        expected_status = "compat-invalid"
        expected_runtime_error = b"compatibility inflight diagnostic does not match"

    blocker_before = _operator_entry_evidence(corrupted_path)
    lock_before = _manifest(lock_dir)
    blocked = _verify_release(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
    )
    assert blocked.returncode != 0, blocked.stdout
    assert blocked.stdout == b""
    assert expected_runtime_error in blocked.stderr
    assert _manifest(lock_dir) == lock_before
    assert _operator_entry_evidence(corrupted_path) == blocker_before

    items = _finalization_items(
        state,
        generation_token=generation_token,
        transition=transition,
        canonical=True,
    )
    replacement_token, replacement_pin = _assert_finalization_operator_success(
        state,
        source=lock_dir,
        generation_token=generation_token,
        transition=transition,
        status=expected_status,
        operation_label=f"canonical {kind} inflight operator",
        finalization_items=items,
        marker_status="absent",
        release_authority_status="valid",
    )
    assert _operator_entry_evidence(corrupted_path) == blocker_before

    # A fresh generation must progress through the later inflight phase, not
    # merely acquire. A regular stale compatibility diagnostic is replaceable;
    # an old generation-bound name is disjoint from the replacement token.
    fresh_inflight = _inflight(
        state,
        round_barcode="replacement_finalization-invalid",
        generation_token=replacement_token,
        pin_token=replacement_pin,
        read_file=f"/reads/replacement-{kind}.pod5",
    )
    assert fresh_inflight.returncode == 0, fresh_inflight.stderr
    assert read_compat_inflight(compat_path)["generation_token"] == replacement_token
    if kind == "generation":
        assert _operator_entry_evidence(inflight_path) == blocker_before


@pytest.mark.parametrize("action", ["release", "reclaim"])
def test_finalization_quarantine_preserves_terminal_archive_collision(
    tmp_path: Path,
    action: str,
) -> None:
    if action == "release":
        state, orphan, _round, generation_token, _pin, transition = (
            _helper_created_release_orphan(tmp_path, label="release_archive_collision")
        )
    else:
        state, orphan, _round, generation_token, transition = (
            _helper_created_reclaim_orphan(tmp_path, label="reclaim_archive_collision")
        )
    archive = (
        state / ".round_lock_archives"
        / f"{action}-{transition['operation_token']}"
    )
    archive.mkdir(parents=True)
    (archive / "foreign-evidence.bin").write_bytes(b"occupied archive\n")
    archive_before = _operator_entry_evidence(archive)
    orphan_before = _manifest(orphan)

    blocked = _acquire(state, round_barcode=f"blocked_{action}_archive")
    assert blocked.returncode != 0, blocked.stdout
    assert b"fenced directory move destination already exists" in blocked.stderr
    assert _manifest(orphan) == orphan_before
    assert _operator_entry_evidence(archive) == archive_before

    items = _finalization_items(
        state,
        generation_token=generation_token,
        transition=transition,
        canonical=False,
    )
    _assert_finalization_operator_success(
        state,
        source=orphan,
        generation_token=generation_token,
        transition=transition,
        status="archive-collision",
        operation_label=f"{action} archive collision operator",
        finalization_items=items,
    )


@pytest.mark.parametrize("action", ["release", "reclaim"])
def test_finalization_quarantine_preserves_opposite_terminal_outcome(
    tmp_path: Path,
    action: str,
) -> None:
    if action == "release":
        state, orphan, _round, generation_token, _pin, transition = (
            _helper_created_release_orphan(tmp_path, label="release_opposite_outcome")
        )
        opposite_path = state / f".round_lock_revocation.{generation_token}.tsv"
        opposite = {
            "schema": "1",
            "token": generation_token,
            "round_barcode": transition["round_barcode"],
            "scope": transition["scope"],
            "outcome": "revoked",
            "reason": "planted opposite release outcome",
            "effective_ttl_seconds": transition["effective_ttl_seconds"],
            "reclaim_transition_epoch": transition["started_epoch"],
            "lock_dev": transition["lock_dev"],
            "lock_ino": transition["lock_ino"],
            "operation_token": _token("opposite release revocation"),
        }
        _write_record(
            opposite_path, opposite, fields=REVOCATION_SCHEMA.fields,
        )
        parsed_opposite = read_record(
            opposite_path, schema=REVOCATION_SCHEMA,
        )
        selected_path = state / f".round_lock_release.{generation_token}.tsv"
    else:
        state, orphan, _round, generation_token, transition = (
            _helper_created_reclaim_orphan(tmp_path, label="reclaim_opposite_outcome")
        )
        opposite_path = state / f".round_lock_release.{generation_token}.tsv"
        opposite = {
            "schema": "1",
            "token": generation_token,
            "round_barcode": transition["round_barcode"],
            "scope": transition["scope"],
            "outcome": "released",
            "reason": "full_round_released",
            "effective_ttl_seconds": transition["effective_ttl_seconds"],
            "release_transition_epoch": transition["started_epoch"],
            "lock_dev": transition["lock_dev"],
            "lock_ino": transition["lock_ino"],
            "operation_token": _token("opposite reclaim release"),
        }
        _write_record(opposite_path, opposite, fields=RELEASE_SCHEMA.fields)
        parsed_opposite = read_record(opposite_path, schema=RELEASE_SCHEMA)
        selected_path = state / f".round_lock_revocation.{generation_token}.tsv"
    assert parsed_opposite["token"] == generation_token
    assert parsed_opposite["operation_token"] != transition["operation_token"]
    assert not os.path.lexists(selected_path)
    opposite_before = _operator_entry_evidence(opposite_path)
    orphan_before = _manifest(orphan)

    blocked = _acquire(state, round_barcode=f"blocked_{action}_opposite")
    assert blocked.returncode != 0, blocked.stdout
    assert_exact_error_line(
        blocked.stderr,
        f"ERROR: pending {action} conflicts with an opposite terminal outcome "
        f"for generation {generation_token}",
    )
    assert _manifest(orphan) == orphan_before
    assert _operator_entry_evidence(opposite_path) == opposite_before
    assert not os.path.lexists(selected_path)
    selected_event = (
        state / ".round_lock_events"
        / f"{generation_token}.{action}.{transition['operation_token']}.tsv"
    )
    selected_archive = (
        state / ".round_lock_archives"
        / f"{action}-{transition['operation_token']}"
    )
    assert not os.path.lexists(selected_event)
    assert not os.path.lexists(selected_archive)

    items = _finalization_items(
        state,
        generation_token=generation_token,
        transition=transition,
        canonical=False,
    )
    _assert_finalization_operator_success(
        state,
        source=orphan,
        generation_token=generation_token,
        transition=transition,
        status="opposite-outcome-conflict",
        operation_label=f"{action} opposite outcome operator",
        finalization_items=items,
    )


@pytest.mark.parametrize(
    ("failpoint", "source_visible", "complete_visible"),
    [
        ("after-operator-intent-link-before-sync", True, False),
        ("after-operator-intent", True, False),
        ("after-operator-rename-before-sync", False, False),
        ("after-operator-sync-before-complete", False, False),
        ("after-operator-complete-link-before-sync", False, True),
    ],
)
def test_finalization_replays_external_evidence_at_every_operator_boundary(
    tmp_path: Path,
    failpoint: str,
    source_visible: bool,
    complete_visible: bool,
) -> None:
    state, orphan, _round, generation_token, _pin, transition = (
        _helper_created_release_orphan(
            tmp_path, label=f"finalization_crash_{failpoint}",
        )
    )
    receipt = state / f".round_lock_release.{generation_token}.tsv"
    receipt.write_bytes(b"malformed-release-receipt\n")
    receipt_evidence = _operator_entry_evidence(receipt)
    source_entry = os.lstat(orphan)
    source_manifest = _manifest(orphan)
    operation_token = _token(f"finalization crash {failpoint}")
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
        source_name=orphan.name,
    )
    items = _finalization_items(
        state,
        generation_token=generation_token,
        transition=transition,
        canonical=False,
    )
    finalization_sha, _external = _labeled_evidence_sha256(items)
    ready = tmp_path / f"finalization-{failpoint}.ready"
    release = tmp_path / f"finalization-{failpoint}.release"
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
    _wait_for_exact_pause(process, ready, failpoint=failpoint)
    _assert_state_fence_is_held(state)
    process.kill()
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL, (stdout, stderr)
    assert stdout == b""

    holder = orphan if source_visible else destination
    displaced = destination if source_visible else orphan
    assert _manifest(holder) == source_manifest
    assert not os.path.lexists(displaced)
    assert _operator_entry_evidence(receipt) == receipt_evidence
    intent_path = (
        state / ".round_lock_operator_events" / f"{operation_token}.intent.tsv"
    )
    intent = read_record(intent_path, schema=OPERATOR_EVENT_SCHEMA)
    assert intent["recovery_basis"] == "finalization-invalid"
    assert intent["finalization_status"] == "receipt-invalid"
    assert intent["finalization_sha256"] == finalization_sha
    complete_path = (
        state / ".round_lock_operator_events" / f"{operation_token}.complete.tsv"
    )
    assert os.path.lexists(complete_path) == complete_visible
    if complete_visible:
        complete = read_record(complete_path, schema=OPERATOR_EVENT_SCHEMA)
        assert complete["finalization_sha256"] == finalization_sha
        assert complete["finalization_status"] == "receipt-invalid"

    # Positive control: unchanged exact external evidence must allow identical
    # replay to finish without replacing its audit intent or blocker.
    intent_entry = os.lstat(intent_path)
    intent_bytes = read_nofollow_bytes(intent_path)
    replay = _run(command)
    assert replay.returncode == 0, replay.stderr
    assert replay.stdout == b""
    assert (os.lstat(intent_path).st_dev, os.lstat(intent_path).st_ino) == (
        intent_entry.st_dev,
        intent_entry.st_ino,
    )
    assert read_nofollow_bytes(intent_path) == intent_bytes
    assert _operator_entry_evidence(receipt) == receipt_evidence
    assert _manifest(destination) == source_manifest
    _assert_finalization_event_fields(
        state,
        operation_token=operation_token,
        status="receipt-invalid",
        sha256=finalization_sha,
    )

    _assert_completed_quarantine(
        state,
        source=orphan,
        source_entry=source_entry,
        source_manifest=source_manifest,
        generation_token=generation_token,
        operation_token=operation_token,
        recovery_basis="finalization-invalid",
        command=command,
        source_name=orphan.name,
        marker_status="valid",
        release_authority_status="valid",
        finalization_status="receipt-invalid",
        finalization_sha256=finalization_sha,
        transition_status="valid-release",
        preserved_external_evidence={receipt: receipt_evidence},
    )


@pytest.mark.parametrize(
    ("failpoint", "expected_error", "destination_visible"),
    [
        (
            "after-operator-intent",
            b"operator quarantine intent does not match this request",
            False,
        ),
        (
            "after-operator-complete-link-before-sync",
            b"completed operator quarantine evidence changed",
            True,
        ),
    ],
)
def test_finalization_intent_rejects_external_evidence_replacement(
    tmp_path: Path,
    failpoint: str,
    expected_error: bytes,
    destination_visible: bool,
) -> None:
    state, orphan, _round, generation_token, _pin, transition = (
        _helper_created_release_orphan(tmp_path, label="intent_external_binding")
    )
    receipt = state / f".round_lock_release.{generation_token}.tsv"
    receipt.write_bytes(b"AAAA")
    original_evidence = _operator_entry_evidence(receipt)
    source_entry = os.lstat(orphan)
    source_manifest = _manifest(orphan)
    operation_token = _token(f"finalization external binding {failpoint}")
    command = _unrecoverable_command(
        state,
        lock_dev=source_entry.st_dev,
        lock_ino=source_entry.st_ino,
        generation_token=generation_token,
        operation_token=operation_token,
        source_name=orphan.name,
    )
    ready = tmp_path / "finalization-intent.ready"
    release = tmp_path / "finalization-intent.release"
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
    _wait_for_exact_pause(process, ready, failpoint=failpoint)
    _assert_state_fence_is_held(state)
    process.kill()
    _stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == -signal.SIGKILL, stderr
    destination = state / f"{LOCK_NAME}.operator-{operation_token}"
    holder = destination if destination_visible else orphan
    assert _manifest(holder) == source_manifest
    pending = (
        state / ".round_lock_operator_pending" / f"{operation_token}.tsv"
    )
    intent = read_record(pending, schema=OPERATOR_EVENT_SCHEMA)
    assert intent["recovery_basis"] == "finalization-invalid"
    assert intent["finalization_status"] == "receipt-invalid"
    expected_sha, _evidence = _labeled_evidence_sha256(
        _finalization_items(
            state,
            generation_token=generation_token,
            transition=transition,
            canonical=False,
        )
    )
    assert intent["finalization_sha256"] == expected_sha

    # Replace, rather than rewrite, the external inode so both the same-length
    # bytes and metadata fingerprint challenge the aggregate binding.
    replacement = state / ".replacement-release-receipt"
    replacement.write_bytes(b"BBBB")
    os.replace(replacement, receipt)
    changed_evidence = _operator_entry_evidence(receipt)
    assert changed_evidence != original_evidence
    assert read_nofollow_bytes(receipt) == b"BBBB"
    before_replay = _manifest(state)
    replay = _run(command)
    assert replay.returncode != 0, replay.stdout
    assert replay.stdout == b""
    assert expected_error in replay.stderr
    assert _manifest(state) == before_replay
    assert _manifest(holder) == source_manifest
    assert _operator_entry_evidence(receipt) == changed_evidence
    assert pending.is_file()
    assert os.path.lexists(destination) == destination_visible


@pytest.mark.parametrize("unsafe_global", ["compat-directory", "event-symlink"])
def test_valid_abandonment_global_veto_precedes_local_eligibility(
    tmp_path: Path,
    unsafe_global: str,
) -> None:
    state, lock_dir, round_barcode, generation_token, pin_token = _acquired_state(
        tmp_path, label=f"global_veto_{unsafe_global}",
    )
    _remove_exact_pin_namespace(lock_dir, pin_token)
    if unsafe_global == "compat-directory":
        unsafe_path = state / "round_inflight.txt"
        unsafe_path.mkdir()
        expected_error = b"compatibility inflight diagnostic is a directory"
    else:
        event_dir = state / ".round_lock_events"
        for path in event_dir.iterdir():
            path.unlink()
        event_dir.rmdir()
        external = tmp_path / "external-events"
        external.mkdir()
        event_dir.symlink_to(external, target_is_directory=True)
        unsafe_path = event_dir
        expected_error = b"unsafe round-lock event directory"
    unsafe_before = _operator_entry_evidence(unsafe_path)
    lock_before = _manifest(lock_dir)

    # The local missing-pins condition is independently reachable by runtime.
    runtime_blocked = _guard(
        state,
        round_barcode=round_barcode,
        generation_token=generation_token,
        pin_token=pin_token,
    )
    assert runtime_blocked.returncode != 0, runtime_blocked.stdout
    assert _manifest(lock_dir) == lock_before

    entry = os.lstat(lock_dir)
    command = _unrecoverable_command(
        state,
        lock_dev=entry.st_dev,
        lock_ino=entry.st_ino,
        generation_token=generation_token,
        operation_token=_token(f"global veto {unsafe_global}"),
    )
    before_operator = _manifest(lock_dir)
    refused = _run(command)
    assert refused.returncode != 0, refused.stdout
    assert refused.stdout == b""
    assert expected_error in refused.stderr
    assert _manifest(lock_dir) == before_operator
    assert _operator_entry_evidence(unsafe_path) == unsafe_before
    assert not list(state.glob(f"{LOCK_NAME}.operator-*"))


def test_invalid_snapshot_operator_also_refuses_compatibility_directory(
    tmp_path: Path,
) -> None:
    state, lock_dir, _round_barcode, _generation_token, _pin_token = (
        _acquired_state(tmp_path, label="invalid_operator_global_veto")
    )
    generation = lock_dir / "generation.tsv"
    generation.write_bytes(
        b"schema\t1\nrecord_sha256\t" + b"0" * 64 + b"\n"
    )
    with pytest.raises(MalformedRecord, match="mismatch"):
        read_record(generation, schema=GENERATION_SCHEMA)
    compat = state / "round_inflight.txt"
    compat.mkdir()
    compat_before = _operator_entry_evidence(compat)
    lock_before = _manifest(lock_dir)
    entry = os.lstat(lock_dir)
    operation_token = _token("invalid operator compatibility directory veto")
    command = _invalid_operator_command(
        state,
        lock_dev=entry.st_dev,
        lock_ino=entry.st_ino,
        operation_token=operation_token,
    )
    refused = _run(command)
    assert refused.returncode != 0, refused.stdout
    assert refused.stdout == b""
    assert b"compatibility inflight diagnostic is a directory" in refused.stderr
    assert _manifest(lock_dir) == lock_before
    assert _operator_entry_evidence(compat) == compat_before
    assert not os.path.lexists(
        state / f"{LOCK_NAME}.operator-{operation_token}"
    )
