"""Negative tests for the record oracle in tests/round_lock_test_utils.py.

The oracle exists to detect the helper drifting from its own wire format, so
it is only as good as the malformed records it refuses. Every rejection below
is a shape bin/round_lock_generation.pl's parse_record also refuses; the final
test drives a corrupted record through the helper itself to show the two
agree, rather than asserting the oracle is strict in isolation.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from tests.round_lock_test_utils import (
    GENERATION_SCHEMA,
    HELPER_RECORD_SCHEMAS,
    INFLIGHT_SCHEMA,
    MalformedRecord,
    RecordSchema,
    TRANSITION_SCHEMA,
    UnsafeRecordPath,
    assert_exact_error_line,
    assert_token,
    parse_acquire_output,
    perl_test_env,
    read_compat_inflight,
    read_record,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "round_lock_generation.pl"
SCHEMA_ONLY = RecordSchema("TEST_SCHEMA_ONLY", ("schema",))
SCHEMA_AND_ACTION = RecordSchema("TEST_SCHEMA_AND_ACTION", ("schema", "action"))


def _write(path: Path, body: bytes, checksum: bytes | None = None) -> Path:
    """Write a record, checksumming ``body`` unless a checksum is forced."""
    if checksum is None:
        checksum = hashlib.sha256(body).hexdigest().encode()
    path.write_bytes(body + b"record_sha256\t" + checksum + b"\n")
    return path


def test_accepts_a_well_formed_record(tmp_path: Path) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t1\naction\trelease\n")
    assert read_record(record, schema=SCHEMA_AND_ACTION) == {
        "schema": "1",
        "action": "release",
    }


def test_preserves_non_utf8_payload_octets(tmp_path: Path) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t\xff\n")
    parsed = read_record(record, schema=SCHEMA_ONLY)
    assert parsed["schema"].encode("utf-8", errors="surrogateescape") == b"\xff"


def test_rejects_a_duplicate_checksum(tmp_path: Path) -> None:
    body = b"schema\t1\n"
    good = hashlib.sha256(body).hexdigest().encode()
    record = tmp_path / "r.tsv"
    record.write_bytes(
        body + b"record_sha256\t" + b"0" * 64 + b"\nrecord_sha256\t" + good + b"\n"
    )
    # record_sha256 is never stored in the value map, so the duplicate-key
    # check cannot see this shape -- it needs its own guard.
    with pytest.raises(MalformedRecord, match="duplicate"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_an_extra_tab_field(tmp_path: Path) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t1\textra\n")
    with pytest.raises(MalformedRecord, match="exactly two fields"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_a_duplicate_key(tmp_path: Path) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t1\nschema\t2\n")
    with pytest.raises(MalformedRecord, match="duplicate key"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_a_carriage_return_body(tmp_path: Path) -> None:
    """A CRLF record must not validate by having the \\r normalized away.

    The helper chomps only "\\n", so the "\\r" stays in the payload and breaks
    its hash. str.splitlines() would strip it and accept a record the helper
    rejects, which is why the oracle splits on b"\\n" alone.
    """
    body_without_cr = b"schema\t1\n"
    record = tmp_path / "r.tsv"
    record.write_bytes(
        b"schema\t1\r\nrecord_sha256\t"
        + hashlib.sha256(body_without_cr).hexdigest().encode()
        + b"\n"
    )
    with pytest.raises(MalformedRecord, match="mismatch"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_a_carriage_return_in_the_checksum_line(tmp_path: Path) -> None:
    body = b"schema\t1\n"
    record = tmp_path / "r.tsv"
    record.write_bytes(
        body
        + b"record_sha256\t"
        + hashlib.sha256(body).hexdigest().encode()
        + b"\r\n"
    )
    with pytest.raises(MalformedRecord, match="64-hex"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_a_missing_checksum(tmp_path: Path) -> None:
    record = tmp_path / "r.tsv"
    record.write_bytes(b"schema\t1\n")
    with pytest.raises(MalformedRecord, match="missing"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_a_non_hex_checksum(tmp_path: Path) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t1\n", checksum=b"z" * 64)
    with pytest.raises(MalformedRecord, match="64-hex"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_a_checksum_mismatch(tmp_path: Path) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t1\n", checksum=b"0" * 64)
    with pytest.raises(MalformedRecord, match="mismatch"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_an_unexpected_field_set(tmp_path: Path) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t1\nstray\tvalue\n")
    with pytest.raises(MalformedRecord, match="unexpected field set"):
        read_record(record, schema=SCHEMA_ONLY)


def test_rejects_a_symlink_substituted_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _write(tmp_path / "r.tsv", b"schema\t1\n")
    target = _write(tmp_path / "target.tsv", b"schema\t1\n")
    real_open = os.open
    substituted = False

    def substitute_then_open(path: os.PathLike[str], flags: int) -> int:
        nonlocal substituted
        if not substituted and Path(path) == record:
            substituted = True
            record.unlink()
            record.symlink_to(target)
        return real_open(path, flags)

    monkeypatch.setattr(os, "open", substitute_then_open)
    with pytest.raises(UnsafeRecordPath, match="symlink"):
        read_record(record, schema=SCHEMA_ONLY)
    assert substituted, "the lstat/open substitution gate was not reached"


def test_assert_token_requires_lowercase_64_hex() -> None:
    assert_token("a" * 64)
    for bad in ("A" * 64, "a" * 63, "a" * 65, "g" * 64, ""):
        with pytest.raises(AssertionError):
            assert_token(bad)


def test_transition_order_matches_the_helper() -> None:
    """Pin every typed schema against the helper's own order declarations."""
    source = (SCRIPT).read_text(encoding="utf-8")
    for schema in HELPER_RECORD_SCHEMAS:
        line = next(
            one
            for one in source.splitlines()
            if one.startswith(f"my @{schema.helper_order_name}")
        )
        declared = tuple(line.split("qw(", 1)[1].split(")", 1)[0].split())
        assert declared == schema.fields, (schema.helper_order_name, declared, schema)


def test_acquire_output_and_error_lines_are_lf_exact() -> None:
    stdout = (
        b"generation_token=" + b"a" * 64 + b"\n"
        b"pin_token=" + b"b" * 64 + b"\n"
    )
    assert parse_acquire_output(stdout) == ("a" * 64, "b" * 64)
    for malformed in (stdout.replace(b"\n", b"\r\n"), stdout.rstrip(b"\n")):
        with pytest.raises(AssertionError):
            parse_acquire_output(malformed)

    assert_exact_error_line(b"ERROR: exact\n", "ERROR: exact")
    assert_exact_error_line(
        b"perl: warning: locale unavailable\nERROR: exact\n", "ERROR: exact"
    )
    for malformed in (
        b"ERROR: exact\r\n",
        b"ERROR: exact \n",
        b"ERROR: exact\nERROR: other\n",
    ):
        with pytest.raises(AssertionError):
            assert_exact_error_line(malformed, "ERROR: exact")


def test_compatibility_oracle_hashes_raw_bytes_and_requires_its_schema(
    tmp_path: Path,
) -> None:
    values = {
        "round_barcode": "r",
        "started_utc": "2026-08-13T00:00:00Z",
        "read_file": "/reads/a=b.pod5",
        "generation_token": "a" * 64,
        "scope": "dorado_only",
        "lock_dev": "1",
        "lock_ino": "2",
    }
    body = b"".join(
        key.encode() + b"=" + values[key].encode() + b"\n"
        for key in INFLIGHT_SCHEMA.fields
    )
    record = tmp_path / "round_inflight.txt"
    record.write_bytes(
        body + b"record_sha256=" + hashlib.sha256(body).hexdigest().encode() + b"\n"
    )
    assert read_compat_inflight(record) == values

    crlf = body.replace(b"\n", b"\r\n")
    record.write_bytes(
        crlf
        + b"record_sha256="
        + hashlib.sha256(body).hexdigest().encode()
        + b"\n"
    )
    with pytest.raises(MalformedRecord, match="mismatch"):
        read_compat_inflight(record)


def test_the_helper_rejects_what_the_oracle_rejects(tmp_path: Path) -> None:
    """Cross-check: a shape the oracle refuses is fatal to the helper too.

    Without this, the oracle could be arbitrarily strict rather than faithful.

    The helper rejects the record but reports it as a *lost* generation, not a
    malformed one: lock_snapshot captures parse_record's error into
    ``$snapshot->{malformed_error}`` at bin/round_lock_generation.pl:433, and
    that field is never read anywhere in the helper. Corruption and absence
    therefore reach the operator as the same message, though they call for
    different responses -- absence can be a benign cleanup race, corruption
    cannot. The assertion below pins today's behavior deliberately: when the
    diagnostic is surfaced, this test must fail and be updated consciously.
    """
    state = tmp_path / "state"
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

    generation = state / ".round_inflight.lockdir" / "generation.tsv"
    original = generation.read_bytes()
    read_record(
        generation, schema=GENERATION_SCHEMA
    )  # positive control: the helper's own record validates

    # Append a second field to the first line: rejected by the oracle above as
    # "exactly two fields", and by parse_record's @extra guard.
    corrupt = original.replace(b"schema\t1\n", b"schema\t1\textra\n", 1)
    assert corrupt != original
    os.chmod(generation, 0o600)
    generation.write_bytes(corrupt)

    with pytest.raises(MalformedRecord, match="exactly two fields"):
        read_record(generation, schema=GENERATION_SCHEMA)

    result = subprocess.run(
        ["perl", str(SCRIPT), "early-release", *base,
         "--token", generation_token, "--pin-token", pin_token],
        capture_output=True, check=False, env=perl_test_env(),
    )
    assert result.returncode != 0, result.stdout
    assert_exact_error_line(
        result.stderr,
        f"ERROR: round lock generation was lost: {generation_token}",
    )
