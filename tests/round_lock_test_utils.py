"""Independent oracle for round-lock helper wire formats.

These parsers deliberately re-implement the helper's record contract in
Python rather than calling the helper to validate its own output. A test that
asserted through the helper's validator would agree with the helper by
construction and could not detect the two of them drifting together.

``read_record`` mirrors ``parse_record`` in bin/round_lock_generation.pl
(lines 368-392), which rejects, in this order:

* a line that does not split into exactly two tab-separated fields --
  ``split(/\\t/, $line, -1)`` with a third field is fatal, so an embedded tab
  is rejected rather than folded into the payload;
* a repeated non-checksum key;
* a repeated ``record_sha256`` line;
* a checksum that is absent or not 64 lowercase hex;
* a checksum that does not match sha256 over the reconstructed body;
* a field set that differs from the expected one.

Two details matter for faithfulness. The body is hashed as raw bytes, so this
module splits on b"\\n" and never uses ``str.splitlines()``: the Perl side
``chomp``s only "\\n", leaving a stray "\\r" inside the payload where it breaks
the hash, while ``splitlines()`` would strip it and validate a record the
helper rejects. And ``record_sha256`` is never stored in the value map on the
Perl side, so a duplicate checksum needs its own guard -- a duplicate-key check
alone cannot see it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

# bin/round_lock_generation.pl:29
TOKEN_RE = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RecordSchema:
    """The exact non-checksum field set for one helper record type."""

    helper_order_name: str
    fields: tuple[str, ...]


# bin/round_lock_generation.pl:395-402. The helper validates each field *set*,
# not its order: checksummed_content writes in this order, but parse_record only
# checks required-present and count. Keeping named schemas prevents a caller
# from accidentally omitting that required-set check or applying one record's
# shape to another.
GENERATION_SCHEMA = RecordSchema(
    "GENERATION_ORDER",
    (
        "schema", "token", "round_barcode", "scope", "pid", "host",
        "process_start", "started_epoch", "effective_ttl_seconds",
        "lock_dev", "lock_ino",
    ),
)
PIN_SCHEMA = RecordSchema(
    "PIN_ORDER",
    (
        "schema", "token", "pin_token", "round_barcode", "scope", "role",
        "pid", "host", "process_start", "created_epoch", "lock_dev",
        "lock_ino",
    ),
)
TRANSITION_SCHEMA = RecordSchema(
    "TRANSITION_ORDER",
    (
        "schema", "action", "operation_token", "owner_token",
        "round_barcode", "scope", "reason", "effective_ttl_seconds",
        "lock_dev", "lock_ino", "allowed_pin_token", "started_epoch",
    ),
)
MARKER_SCHEMA = RecordSchema(
    "MARKER_ORDER",
    ("schema", "token", "round_barcode", "scope", "outcome", "created_epoch"),
)
RELEASE_SCHEMA = RecordSchema(
    "RELEASE_ORDER",
    (
        "schema", "token", "round_barcode", "scope", "outcome", "reason",
        "effective_ttl_seconds", "release_transition_epoch", "lock_dev",
        "lock_ino", "operation_token",
    ),
)
REVOCATION_SCHEMA = RecordSchema(
    "REVOCATION_ORDER",
    (
        "schema", "token", "round_barcode", "scope", "outcome", "reason",
        "effective_ttl_seconds", "reclaim_transition_epoch", "lock_dev",
        "lock_ino", "operation_token",
    ),
)
EVENT_SCHEMA = RecordSchema(
    "EVENT_ORDER",
    (
        "schema", "event_id", "generation_token", "round_barcode", "scope",
        "event", "outcome", "effective_ttl_seconds", "event_epoch",
        "lock_dev", "lock_ino",
    ),
)
INFLIGHT_SCHEMA = RecordSchema(
    "INFLIGHT_ORDER",
    (
        "round_barcode", "started_utc", "read_file", "generation_token",
        "scope", "lock_dev", "lock_ino",
    ),
)
OPERATOR_EVENT_SCHEMA = RecordSchema(
    "OPERATOR_EVENT_ORDER",
    (
        "schema", "operation_token", "operation_kind",
        "expected_generation_token", "recovery_basis", "phase",
        "operator_label", "reason",
        "source_name", "destination_name", "lock_dev", "lock_ino",
        "tree_sha256",
        "generation_status", "generation_entry_kind", "generation_sha256",
        "generation_entry_fingerprint", "pins_status", "pins_entry_kind",
        "pins_sha256", "pins_entry_fingerprint", "transition_status",
        "transition_entry_kind", "transition_sha256",
        "transition_entry_fingerprint", "marker_status",
        "marker_entry_kind", "marker_sha256", "marker_entry_fingerprint",
        "release_authority_status", "finalization_status",
        "finalization_sha256", "event_epoch", "outcome",
    ),
)

HELPER_RECORD_SCHEMAS = (
    GENERATION_SCHEMA,
    PIN_SCHEMA,
    TRANSITION_SCHEMA,
    MARKER_SCHEMA,
    RELEASE_SCHEMA,
    REVOCATION_SCHEMA,
    EVENT_SCHEMA,
    INFLIGHT_SCHEMA,
    OPERATOR_EVENT_SCHEMA,
)

CHECKSUM_KEY = b"record_sha256"


class MalformedRecord(AssertionError):
    """A record the helper's parse_record would reject."""


class UnsafeRecordPath(AssertionError):
    """A record path that cannot be read without following or racing a link."""


def assert_token(token: str) -> None:
    """Require a 64-character lowercase hex token."""
    assert TOKEN_RE.match(token), f"not a 64-hex token: {token!r}"


def read_nofollow_bytes(path: Path) -> bytes:
    """Read one stable regular-file inode without following the final path.

    ``Path.read_bytes`` follows symlinks. That would let a fixture validate a
    target that the helper's O_NOFOLLOW reader correctly refuses, so mirror the
    helper's lstat/open/fstat identity checks before consuming raw bytes.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise UnsafeRecordPath("os.O_NOFOLLOW is unavailable or inert")

    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise UnsafeRecordPath(f"record is not a regular non-symlink file: {path}")

    try:
        fd = os.open(path, os.O_RDONLY | nofollow)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise UnsafeRecordPath(f"record path is a symlink: {path}") from error
        raise

    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise UnsafeRecordPath(f"open record is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise UnsafeRecordPath(f"record was replaced while being opened: {path}")
        chunks: list[bytes] = []
        while True:
            try:
                chunk = os.read(fd, 64 * 1024)
            except InterruptedError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _decode_values(values: dict[bytes, bytes], path: Path) -> dict[str, str]:
    # Perl keeps record fields as octets. ``surrogateescape`` preserves every
    # byte losslessly while still giving the tests strings for ordinary ASCII
    # records; strict UTF-8 decoding would reject records the helper accepts.
    return {
        key.decode("utf-8", errors="surrogateescape"):
        value.decode("utf-8", errors="surrogateescape")
        for key, value in values.items()
    }


def read_record(path: Path, *, schema: RecordSchema) -> dict[str, str]:
    """Parse and fully validate a checksummed helper record.

    ``schema`` is mandatory because the Perl parser always receives one exact
    required field set. A checksum-valid but wrong-shaped record is not valid.
    """
    if not isinstance(schema, RecordSchema):
        raise TypeError("schema must be a RecordSchema")
    raw = read_nofollow_bytes(path)
    lines = raw.split(b"\n")
    # A trailing newline yields a final empty element; Perl's <$fh> produces no
    # such line. A record that does not end in a newline is still parsed, as
    # chomp on the last line is simply a no-op there.
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        raise MalformedRecord(f"empty record: {path}")

    values: dict[bytes, bytes] = {}
    body = b""
    checksum: bytes | None = None
    for line in lines:
        fields = line.split(b"\t")
        if len(fields) != 2:
            raise MalformedRecord(
                f"line does not split into exactly two fields: {line!r} in {path}"
            )
        key, payload = fields
        if key in values:
            raise MalformedRecord(f"duplicate key {key!r} in {path}")
        if key == CHECKSUM_KEY:
            if checksum is not None:
                raise MalformedRecord(f"duplicate {CHECKSUM_KEY!r} in {path}")
            checksum = payload
            continue
        values[key] = payload
        body += key + b"\t" + payload + b"\n"

    if checksum is None:
        raise MalformedRecord(f"missing {CHECKSUM_KEY!r} in {path}")
    checksum_text = checksum.decode("ascii", errors="replace")
    if not TOKEN_RE.match(checksum_text):
        raise MalformedRecord(f"checksum is not 64-hex: {checksum_text!r} in {path}")
    actual = hashlib.sha256(body).hexdigest()
    if actual != checksum_text:
        raise MalformedRecord(
            f"checksum mismatch in {path}: recorded {checksum_text}, computed {actual}"
        )

    decoded = _decode_values(values, path)
    if set(decoded) != set(schema.fields):
        raise MalformedRecord(
            f"unexpected field set in {path}: "
            f"{sorted(decoded)} != {sorted(schema.fields)}"
        )
    return decoded


def read_compat_inflight(path: Path) -> dict[str, str]:
    """Validate the legacy ``key=value`` inflight diagnostic as raw bytes."""
    raw = read_nofollow_bytes(path)
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    if not lines:
        raise MalformedRecord(f"empty compatibility record: {path}")

    values: dict[bytes, bytes] = {}
    body = b""
    checksum: bytes | None = None
    checksum_index: int | None = None
    for index, line in enumerate(lines):
        key, separator, payload = line.partition(b"=")
        if separator != b"=":
            raise MalformedRecord(f"malformed compatibility line {line!r} in {path}")
        if key in values:
            raise MalformedRecord(f"duplicate key {key!r} in {path}")
        if key == CHECKSUM_KEY:
            if checksum is not None:
                raise MalformedRecord(f"duplicate {CHECKSUM_KEY!r} in {path}")
            checksum = payload
            checksum_index = index
            continue
        values[key] = payload
        body += line + b"\n"

    if checksum is None:
        raise MalformedRecord(f"missing {CHECKSUM_KEY!r} in {path}")
    if checksum_index != len(lines) - 1:
        raise MalformedRecord(f"checksum is not the final line in {path}")
    checksum_text = checksum.decode("ascii", errors="replace")
    if not TOKEN_RE.match(checksum_text):
        raise MalformedRecord(f"checksum is not 64-hex: {checksum_text!r} in {path}")
    actual = hashlib.sha256(body).hexdigest()
    if actual != checksum_text:
        raise MalformedRecord(
            f"checksum mismatch in {path}: recorded {checksum_text}, computed {actual}"
        )

    decoded = _decode_values(values, path)
    if tuple(decoded) != INFLIGHT_SCHEMA.fields:
        raise MalformedRecord(
            f"unexpected compatibility field order in {path}: "
            f"{tuple(decoded)} != {INFLIGHT_SCHEMA.fields}"
        )
    return decoded


def parse_acquire_output(stdout: bytes) -> tuple[str, str]:
    """Parse ``acquire`` stdout, rejecting duplicate or unexpected fields."""
    assert stdout.endswith(b"\n"), stdout
    lines = stdout.split(b"\n")
    lines.pop()
    fields: dict[bytes, bytes] = {}
    for line in lines:
        key, separator, value = line.partition(b"=")
        assert separator == b"=", stdout
        assert key not in fields, stdout
        fields[key] = value
    assert set(fields) == {b"generation_token", b"pin_token"}, stdout
    generation_token = fields[b"generation_token"].decode("ascii")
    pin_token = fields[b"pin_token"].decode("ascii")
    assert_token(generation_token)
    assert_token(pin_token)
    assert generation_token != pin_token
    return generation_token, pin_token


def assert_exact_error_line(stderr: bytes, expected: str) -> None:
    """Require exactly one matching ``ERROR:`` line, tolerating runtime warnings.

    Perl can emit locale warnings before program diagnostics on macOS. Those
    warnings must not make the gate environment-dependent, while CRLF, trailing
    whitespace, a confusable failpoint, or an additional program error must all
    remain detectable.
    """
    assert stderr.endswith(b"\n"), stderr
    lines = stderr.split(b"\n")[:-1]
    error_lines = [line for line in lines if line.startswith(b"ERROR:")]
    assert error_lines == [expected.encode("utf-8")], stderr


def perl_test_env(**overrides: str) -> dict[str, str]:
    """Return a deterministic Perl environment for byte-exact diagnostics."""
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env.pop("LC_CTYPE", None)
    env.update(overrides)
    return env
