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

import hashlib
import re
from pathlib import Path

# bin/round_lock_generation.pl:29
TOKEN_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# bin/round_lock_generation.pl:397. The helper validates the field *set*, not
# the order: checksummed_content writes in this order, but parse_record only
# checks required-present and count. Callers asserting order are checking the
# writer's format, which is a separate claim from the record being valid.
TRANSITION_ORDER = (
    "schema", "action", "operation_token", "owner_token", "round_barcode",
    "scope", "reason", "effective_ttl_seconds", "lock_dev", "lock_ino",
    "allowed_pin_token", "started_epoch",
)

CHECKSUM_KEY = b"record_sha256"


class MalformedRecord(AssertionError):
    """A record the helper's parse_record would reject."""


def assert_token(token: str) -> None:
    """Require a 64-character lowercase hex token."""
    assert TOKEN_RE.match(token), f"not a 64-hex token: {token!r}"


def read_record(path: Path, *, required: tuple[str, ...] | None = None) -> dict[str, str]:
    """Parse and fully validate a checksummed helper record.

    ``required`` asserts the exact field set, as the Perl parser always does.
    It stays optional here only because existing call sites predate it.
    """
    raw = path.read_bytes()
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
    checksum_text = checksum.decode("utf-8", errors="replace")
    if not TOKEN_RE.match(checksum_text):
        raise MalformedRecord(f"checksum is not 64-hex: {checksum_text!r} in {path}")
    actual = hashlib.sha256(body).hexdigest()
    if actual != checksum_text:
        raise MalformedRecord(
            f"checksum mismatch in {path}: recorded {checksum_text}, computed {actual}"
        )

    decoded = {key.decode("utf-8"): value.decode("utf-8") for key, value in values.items()}
    if required is not None and set(decoded) != set(required):
        raise MalformedRecord(
            f"unexpected field set in {path}: {sorted(decoded)} != {sorted(required)}"
        )
    return decoded


def parse_acquire_output(stdout: str) -> tuple[str, str]:
    """Parse ``acquire`` stdout, rejecting duplicate or unexpected fields."""
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        assert separator == "=", stdout
        assert key not in fields, stdout
        fields[key] = value
    assert set(fields) == {"generation_token", "pin_token"}, stdout
    generation_token = fields["generation_token"]
    pin_token = fields["pin_token"]
    assert_token(generation_token)
    assert_token(pin_token)
    assert generation_token != pin_token
    return generation_token, pin_token
