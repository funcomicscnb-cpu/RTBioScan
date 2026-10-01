"""Portable byte hashing, signature identity, and existing plot boundaries."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "bin/report_sample_read_counts_plots.sh"
SALT = b"read-counts-plots-v4\n"


def _helper():
    source = SCRIPT.read_text()
    match = re.search(r"^_sha256_stdin\(\) \{\n.*?^\}", source, re.M | re.S)
    assert match, "local SHA-256 helper missing"
    return match.group()


@pytest.mark.parametrize("payload", [b"", b"abc\n", bytes(range(256)), "é水\n".encode()])
@pytest.mark.parametrize("unicode_env", ["", "SDA"])
def test_digest_bytes(payload, unicode_env):
    result = subprocess.run(
        ["/bin/bash", "-c", _helper() + "\n_sha256_stdin"], input=payload,
        capture_output=True, env={**os.environ, "PERL_UNICODE": unicode_env},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == hashlib.sha256(payload).hexdigest().encode() + b"\n"
    if Path("/sbin/sha256sum").exists():
        control = subprocess.run(["/sbin/sha256sum"], input=payload, capture_output=True, check=True)
        assert result.stdout.strip() == control.stdout.split()[0]


@pytest.mark.parametrize("name", ["space name", "é水", "'quotes\"", "-leading", "$(touch INJECTED); &*", "line\nbreak"])
def test_hash_file_paths(tmp_path, name):
    payload = bytes(range(256)) + b"\x00\r\n"
    (tmp_path / name).write_bytes(payload)
    result = subprocess.run(
        ["/bin/bash", "-c", _helper() + '\n_sha256_stdin < "$1"', "test", name],
        cwd=tmp_path, capture_output=True, env={**os.environ, "PERL_UNICODE": "SDA"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == hashlib.sha256(payload).hexdigest().encode() + b"\n"
    assert not (tmp_path / "INJECTED").exists()


@pytest.mark.parametrize("kind", ["missing", "unreadable", "read_error"])
def test_hash_read_failure(tmp_path, kind):
    path = tmp_path / kind
    if kind == "unreadable":
        path.write_bytes(b"secret")
        path.chmod(0)
        if os.geteuid() == 0:
            pytest.skip("root bypasses file permissions")
    elif kind == "read_error":
        path.mkdir()
    result = subprocess.run(
        ["/bin/bash", "-c", _helper() + '\n_sha256_stdin < "$1"', "test", str(path)],
        capture_output=True,
    )
    assert result.returncode != 0
    assert not re.fullmatch(rb"[0-9a-f]{64}\n", result.stdout)


@pytest.fixture
def harness(tmp_path):
    tools = tmp_path / "tools"
    tools.mkdir()
    # Whitelist rather than inherit a host PATH that can hide sha256sum absence.
    for command in ["bash", "dirname", "mkdir", "awk", "sort", "cat", "perl", "tr", "rm", "mv"]:
        executable = shutil.which(command)
        assert executable, command
        (tools / command).symlink_to(executable)
    mktemp = tools / "mktemp"
    mktemp.write_text('#!/bin/bash\nexec /usr/bin/mktemp "$TEST_TMP/hash.XXXXXX"\n')
    mktemp.chmod(0o755)
    rscript = tools / "Rscript"
    rscript.write_text(
        '#!/bin/bash\n'
        'echo invoked >> "$TEST_TMP/render.log"\n'
        'if [ "${FAIL_R:-0}" = 1 ]; then exit 42; fi\n'
        'for suffix in reads_per_barcode reads_per_sample reads_per_sample_log; do\n'
        '  for ext in png pdf; do printf figure > "${4}_${suffix}.${ext}"; done\n'
        'done\n'
    )
    rscript.chmod(0o755)
    env = {**os.environ, "PATH": str(tools), "TEST_TMP": str(tmp_path), "LC_ALL": "C"}
    env.pop("PERL_UNICODE", None)
    summary = tmp_path / "-summary é '$(touch INJECTED).tsv"
    summary.write_bytes(b"read_count\tbasecalling_model\tsample_name\n100\thac\tSampleA\n")
    out = tmp_path / "-figures é ';&"
    sig = tmp_path / "-signatures é ';&"
    args = ["/bin/bash", str(SCRIPT), "--summary", str(summary), "--out-dir", str(out), "--sig-dir", str(sig)]
    def run(**extra):
        return subprocess.run(args, capture_output=True, env={**env, **extra}, cwd=tmp_path)
    return run, tools, summary, out, sig, tmp_path


def test_script_signatures_and_cache(harness):
    run, tools, summary, out, sig, tmp = harness
    assert not (tools / "sha256sum").exists()
    result = run(PERL_UNICODE="SDA")
    assert result.returncode == 0, result.stderr
    assert len(list(out.rglob("*.png"))) == len(list(out.rglob("*.pdf"))) == 3
    assert all(p.stat().st_size for p in out.rglob("*") if p.is_file())
    assert (sig / "selection.sig").read_bytes() == hashlib.sha256(SALT + b"10\nSampleA\t100\n").hexdigest().encode() + b"\n"
    sample_sig = next(p for p in sig.glob("*.sig") if p.name != "selection.sig")
    assert sample_sig.read_bytes() == hashlib.sha256(summary.read_bytes() + SALT).hexdigest().encode() + b"\n"
    assert run().returncode == 0
    assert (tmp / "render.log").read_text().splitlines() == ["invoked"]
    summary.write_bytes(summary.read_bytes().replace(b"100", b"200"))
    assert run().returncode == 0
    assert len((tmp / "render.log").read_text().splitlines()) == 2
    next(out.rglob("*.pdf")).unlink()
    assert run().returncode == 0
    assert len((tmp / "render.log").read_text().splitlines()) == 3


def test_hash_failure_stops_before_sample_output(harness):
    run, tools, summary, out, sig, tmp = harness
    perl = tools / "perl"
    real_perl = os.readlink(perl)
    perl.unlink()
    perl.write_text('#!/bin/bash\ncase "$*" in *-MDigest::SHA*) exit 42;; esac\nexec "' + real_perl + '" "$@"\n')
    perl.chmod(0o755)
    assert run().returncode != 0
    assert not list(out.iterdir())
    assert not (tmp / "render.log").exists()
    assert not any(re.fullmatch(rb"[0-9a-f]{64}\n", p.read_bytes()) for p in sig.glob("*.sig"))


def test_missing_summary_preserves_success(harness):
    run, tools, summary, out, sig, tmp = harness
    summary.unlink()
    assert run().returncode == 0
    assert not out.exists() and not sig.exists()


def test_r_failure_preserves_warning_and_success(harness):
    run, tools, summary, out, sig, tmp = harness
    result = run(FAIL_R="1")
    assert result.returncode == 0
    assert b"WARN: sample Read_counts plotting failed for sample=SampleA sample_id=" in result.stderr
    assert [p.name for p in sig.glob("*.sig")] == ["selection.sig"]
    assert not list(out.rglob("*.png"))
