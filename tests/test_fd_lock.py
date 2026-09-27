"""Behavioral checks for the Stage A inherited-descriptor lock protocol.

All state, mutating probes, and simulated hosts live under pytest tmp_path.
"""
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import time
import fcntl

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SHELL = ROOT / "bin/lib/lock_utils.sh"
HELPER = ROOT / "bin/lib/fd_lock.pl"
A0_HANDLER = ROOT / "bin/restart_handler.sh"
BASH = "/bin/bash"
ID_A = "macos:IOPlatformUUID:11111111-1111-4111-8111-111111111111"
ID_B = "macos:IOPlatformUUID:22222222-2222-4222-8222-222222222222"


def run_lock(target, body=":", wait="3", source=SHELL, timeout=12):
    script = 'set -euo pipefail; source "$1"; LOCK_WAIT="$3"; init_lock_helpers; acquire_lock "$2"; ' + body
    return subprocess.run([BASH, "-c", script, "_", str(source), str(target), str(wait)],
                          capture_output=True, text=True, timeout=timeout)


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def assert_drained(target):
    assert wait_for(lambda: not pathlib.Path(str(target) + ".lockdir").exists()), target


def a0_env(outdir, operation="a", wait="0"):
    return dict(os.environ, MODE="reset", OUTDIR=str(outdir), LOCK_WAIT=wait,
                RUN_NAME="stage-a-integration", STATE_ID="SID", FORCE="1",
                OPERATION_ID=operation * 64)


def run_a0(outdir, operation="a", wait="0", env=None):
    return subprocess.run([BASH, str(A0_HANDLER)],
                          env=env or a0_env(outdir, operation, wait),
                          capture_output=True, text=True, timeout=12)


def a0_state(outdir):
    state = outdir / "temp/ongoing/state/SID/_state"
    state.mkdir(parents=True, exist_ok=True)
    return state


def scratch_source(tmp_path, host=None, identity=None, edits=()):
    lib = tmp_path / "scratch_lib"
    lib.mkdir(parents=True, exist_ok=True)
    shell = lib / "lock_utils.sh"
    helper = lib / "fd_lock.pl"
    shutil.copyfile(SHELL, shell)
    text = HELPER.read_text()
    if host is not None:
        text = text.replace("use Sys::Hostname qw(hostname);", f"sub hostname {{ return '{host}'; }}")
    if identity is not None:
        assert re.fullmatch(r"(?:macos:IOPlatformUUID:[0-9a-f-]+|linux:machine-id:[0-9a-f]+)", identity)
        text = text.replace("sub machine_identity {", f"sub machine_identity {{ return '{identity}';", 1)
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new, 1)
    helper.write_text(text)
    helper.chmod(0o755)
    return shell, helper


def provider_source(tmp_path, platform, output="", status=0, signaled=False, missing=False, host="diagnostic-host"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    edits = [("my $platform = $^O;", f"my $platform = '{platform}';")]
    if platform == "darwin":
        provider = tmp_path / "ioreg-provider"
        data = tmp_path / "provider-output"
        data.write_text(output)
        if not missing:
            provider.write_text("#!/bin/sh\n" +
                                ("kill -TERM $$\n" if signaled else f"cat {shlex.quote(str(data))}\nexit {status}\n"))
            provider.chmod(0o755)
        edits.append(("'/usr/sbin/ioreg'", f"'{provider}'"))
    elif platform == "linux":
        provider = tmp_path / "machine-id"
        if not missing:
            provider.write_text(output)
        edits.append(("'/etc/machine-id'", f"'{provider}'"))
    return scratch_source(tmp_path, host=host, edits=edits)


def ioreg_output(*values):
    properties = "".join(f'      "IOPlatformUUID" = "{value}"\n' for value in values)
    return "+-o mock  <class IOPlatformExpertDevice, id 0x1>\n    {\n" + properties + "    }\n"


def test_normal_two_namespaces_unicode_path_and_descriptor_lifetime(tmp_path):
    a = tmp_path / "é space.lock"
    b = tmp_path / "other.lock"
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=2; init_lock_helpers
acquire_lock "$2"; acquire_lock "$3"
printf protected > "$4"
release_lock "$3"; release_lock "$2"
'''
    result = subprocess.run([BASH, "-c", script, "_", str(SHELL), str(a), str(b), str(tmp_path / "value")],
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "value").read_bytes() == b"protected"
    for target in (a, b):
        assert pathlib.Path(str(target) + ".flock").is_file()
        assert_drained(target)
    assert (tmp_path / ".rtbioscan_lock_host_v1").read_text().startswith("rtbioscan-lock-host-v2\tmacos\tIOPlatformUUID\t")


def test_live_holder_timeout_then_release(tmp_path):
    target = tmp_path / "contended.lock"
    marker = tmp_path / "ready"
    owner = subprocess.Popen([BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=2; init_lock_helpers; acquire_lock "$2"; : > "$3"; sleep 1; release_lock "$2"',
                              "_", str(SHELL), str(target), str(marker)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    try:
        assert wait_for(marker.exists)
        failed = run_lock(target, wait="0.2")
        assert failed.returncode == 1
        assert f"Failed to acquire lock on {target}" in failed.stderr
        assert "timed out" in failed.stderr and "live holder:" in failed.stderr
        owner.wait(timeout=4)
        passed = run_lock(target)
        assert passed.returncode == 0, passed.stderr
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_eight_contenders_eighty_entries(tmp_path):
    target = tmp_path / "many.lock"
    inside = tmp_path / "inside"
    violation = tmp_path / "violation"
    completed = tmp_path / "completed"
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=12; init_lock_helpers
for ((i=0; i<10; i++)); do
  acquire_lock "$2"
  if ! mkdir "$3" 2>/dev/null; then printf overlap >> "$4"; fi
  sleep 0.01
  rmdir "$3"
  printf x >> "$5"
  release_lock "$2"
done
'''
    procs = [subprocess.Popen([BASH, "-c", script, "_", str(SHELL), str(target), str(inside), str(violation), str(completed)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True) for _ in range(8)]
    try:
        results = [p.communicate(timeout=35) for p in procs]
        assert [p.returncode for p in procs] == [0] * 8, results
        assert not violation.exists()
        assert completed.read_bytes() == b"x" * 80
        assert_drained(target)
    finally:
        for p in procs:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=3)


@pytest.mark.parametrize("status", [1, 127])
def test_exit_status_closes_owner(tmp_path, status):
    target = tmp_path / "exit.lock"
    script = 'set -euo pipefail; source "$1"; LOCK_WAIT=2; init_lock_helpers; acquire_lock "$2"; exit "$3"'
    result = subprocess.run([BASH, "-c", script, "_", str(SHELL), str(target), str(status)], timeout=8)
    assert result.returncode == status
    assert run_lock(target, wait="1").returncode == 0
    assert_drained(target)


def _live_child_case(tmp_path, mode, source=SHELL):
    target = tmp_path / "child.lock"
    writes = tmp_path / "writes"
    childpid = tmp_path / "childpid"
    ready = tmp_path / "ready"
    owner_script = '''set -euo pipefail
source "$1"; LOCK_WAIT=4; init_lock_helpers; acquire_lock "$2"
/bin/bash -c 'for i in 1 2 3 4; do /usr/bin/python3 -c "import time; print(time.time())" >> "$1"; sleep 0.17; done' _ "$3" &
echo $! > "$4"
: > "$5"
wait
release_lock "$2"
'''
    owner = subprocess.Popen([BASH, "-c", owner_script, "_", str(source), str(target), str(writes), str(childpid), str(ready)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert wait_for(ready.exists, 5)
        if mode == "kill-shell":
            os.kill(owner.pid, signal.SIGKILL)
        elif mode == "release-early":
            os.kill(owner.pid, signal.SIGTERM)
        elif mode == "kill-tree":
            os.killpg(owner.pid, signal.SIGKILL)
        owner.wait(timeout=4)
        if mode == "kill-tree":
            contender = run_lock(target, body='printf ok > "$2.success"', wait="3")
            assert contender.returncode == 0, contender.stderr
            assert_drained(target)
            return
        contender = run_lock(target, body='/usr/bin/python3 -c "import time; print(time.time())" > "$2.acquired"', wait="4")
        assert contender.returncode == 0, contender.stderr
        times = [float(x) for x in writes.read_text().splitlines()]
        acquired = float(pathlib.Path(str(target) + ".acquired").read_text())
        assert len(times) == 4 and acquired > max(times)
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_shell_sigkill_live_child_and_final_close(tmp_path):
    _live_child_case(tmp_path, "kill-shell")


def test_whole_tree_sigkill_stale_fence_adopted(tmp_path):
    _live_child_case(tmp_path, "kill-tree")


def test_normal_release_with_live_child_keeps_fence(tmp_path):
    target = tmp_path / "released.lock"
    writes = tmp_path / "writes"
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=4; init_lock_helpers; acquire_lock "$2"
/bin/bash -c 'sleep 0.3; printf child >> "$1"' _ "$3" &
release_lock "$2"
'''
    owner = subprocess.run([BASH, "-c", script, "_", str(SHELL), str(target), str(writes)], timeout=8)
    assert owner.returncode == 0
    assert pathlib.Path(str(target) + ".lockdir").exists()
    assert run_lock(target, wait="3").returncode == 0
    assert writes.read_text() == "child"
    assert_drained(target)


def test_legacy_empty_and_explicit_audited_adoption(tmp_path):
    target = tmp_path / "legacy.lock"
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    failed = run_lock(target, wait="0.2")
    assert failed.returncode == 1
    assert "LEGACY_LOCK_UNOWNED" in failed.stderr
    assert "adopt-legacy" in failed.stderr and "--confirm" in failed.stderr
    assert fence.is_dir()
    adopted = subprocess.run(["perl", str(HELPER), "adopt-legacy", str(fence), "--confirm"], capture_output=True, text=True, timeout=8)
    assert adopted.returncode == 0, adopted.stderr
    assert not fence.exists()
    assert "adopt-legacy" in (tmp_path / ".lock_migration.log").read_text()
    assert run_lock(target).returncode == 0
    assert_drained(target)


def test_corrupt_fence_and_owner_metadata(tmp_path):
    target = tmp_path / "corrupt.lock"
    lockfile = pathlib.Path(str(target) + ".flock")
    lockfile.write_bytes(b"\0" * 1024)
    assert run_lock(target).returncode == 0
    assert_drained(target)
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    (fence / "v2owner").write_text("broken\n")
    failed = run_lock(target, wait="0")
    assert failed.returncode == 1 and "corrupt compatibility fence" in failed.stderr
    assert (fence / "v2owner").read_text() == "broken\n"


def test_legacy_live_dead_and_host_mismatch(tmp_path):
    target = tmp_path / "recorded.lock"
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    import socket
    (fence / "meta.env").write_text(f"pid={os.getpid()}\nhost={socket.gethostname()}\n")
    assert run_lock(target, wait="0.2").returncode == 1
    assert fence.exists()
    (fence / "meta.env").write_text(f"pid=99999999\nhost={socket.gethostname()}\n")
    assert run_lock(target).returncode == 0
    assert_drained(target)
    audit = (tmp_path / ".lock_migration.log").read_text()
    assert "reclaim-legacy intent" in audit and "reclaim-legacy complete" in audit
    fence.mkdir()
    (fence / "meta.env").write_text("pid=99999999\nhost=another-host\n")
    assert run_lock(target, wait="0.2").returncode == 1
    assert fence.exists()


def test_binding_initialization_mismatch_and_rebind(tmp_path):
    a, helper_a = scratch_source(tmp_path / "A", host="host-a", identity=ID_A)
    b, helper_b = scratch_source(tmp_path / "B", host="host-b", identity=ID_B)
    target = tmp_path / "bound.lock"
    assert run_lock(target, source=a).returncode == 0
    assert_drained(target)
    binding = tmp_path / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    denied = run_lock(target, source=b)
    assert denied.returncode == 1 and "HOST_BINDING_MISMATCH" in denied.stderr
    assert binding.read_bytes() == original
    old, new = ID_A, ID_B
    wrong = subprocess.run(["perl", str(helper_b), "rebind-host", str(tmp_path), "00", new, "--confirm"], capture_output=True, timeout=8)
    assert wrong.returncode != 0
    changed = subprocess.run(["perl", str(helper_b), "rebind-host", str(tmp_path), old, new, "--confirm"], capture_output=True, timeout=8)
    assert changed.returncode == 0, changed.stderr
    audit = (tmp_path / ".lock_migration.log").read_text()
    assert "rebind-host complete" in audit and f"old={old} new={new}" in audit
    assert run_lock(target, source=b).returncode == 0
    assert_drained(target)
    assert run_lock(target, source=a).returncode == 1


def test_malformed_and_unsupported_binding_refused(tmp_path):
    target = tmp_path / "bound.lock"
    binding = tmp_path / ".rtbioscan_lock_host_v1"
    for text in ("", "partial", "rtbioscan-lock-host-v3\tabcd\n", "rtbioscan-lock-host-v2\tabcd\n"):
        binding.write_text(text)
        assert run_lock(target).returncode == 1
        assert binding.read_text() == text


def test_stable_identity_is_sole_binding_key_and_hostname_is_diagnostic(tmp_path):
    a, _ = scratch_source(tmp_path / "A", host="initial-host", identity=ID_A)
    renamed, _ = scratch_source(tmp_path / "renamed", host="changed-host", identity=ID_A)
    different_same_host, _ = scratch_source(tmp_path / "different-same", host="initial-host", identity=ID_B)
    different_new_host, _ = scratch_source(tmp_path / "different-new", host="changed-host", identity=ID_B)
    target = tmp_path / "é space.lock"
    output = tmp_path / "scientific-output"
    body = 'printf result > "$3"'
    def acquire(source):
        script = 'set -euo pipefail; source "$1"; LOCK_WAIT=2; init_lock_helpers; acquire_lock "$2"; ' + body + '; release_lock "$2"'
        return subprocess.run([BASH, "-c", script, "_", str(source), str(target), str(output)],
                              capture_output=True, text=True, timeout=12)
    first = acquire(a)
    assert first.returncode == 0, first.stderr
    binding = tmp_path / ".rtbioscan_lock_host_v1"
    old_binding = binding.read_bytes()
    inode = pathlib.Path(str(target) + ".flock").stat().st_ino
    old_owner = pathlib.Path(str(target) + ".flock").read_text()
    renamed_result = acquire(renamed)
    assert renamed_result.returncode == 0, renamed_result.stderr
    assert binding.read_bytes() == old_binding
    assert pathlib.Path(str(target) + ".flock").stat().st_ino == inode
    assert "v=2 host=changed-host" in pathlib.Path(str(target) + ".flock").read_text()
    assert "v=2 host=initial-host" in old_owner
    assert output.read_bytes() == b"result"
    assert_drained(target)
    for source in (different_same_host, different_new_host):
        output.unlink(missing_ok=True)
        refused = acquire(source)
        assert refused.returncode == 1 and "HOST_BINDING_MISMATCH" in refused.stderr
        assert not output.exists() and binding.read_bytes() == old_binding


def test_controlled_macos_provider_ignores_hostname_change_and_refuses_id_change(tmp_path):
    uuid_a, uuid_b = ID_A.split(":")[-1], ID_B.split(":")[-1]
    a, _ = provider_source(tmp_path / "A", "darwin", ioreg_output(uuid_a.upper()), host="original-host")
    renamed, _ = provider_source(tmp_path / "renamed", "darwin", ioreg_output(uuid_a), host="renamed-host")
    changed, changed_helper = provider_source(tmp_path / "changed", "darwin", ioreg_output(uuid_b), host="original-host")
    target = tmp_path / "é spaced state.lock"
    assert run_lock(target, source=a).returncode == 0
    binding = tmp_path / ".rtbioscan_lock_host_v1"
    record = binding.read_bytes()
    assert uuid_a.encode() in record
    assert run_lock(target, source=renamed).returncode == 0
    assert binding.read_bytes() == record
    preflight = subprocess.run(["perl", str(changed_helper), "preflight", str(target)],
                               capture_output=True, text=True, timeout=8)
    assert preflight.returncode == 74 and "HOST_BINDING_MISMATCH" in preflight.stderr
    refused = run_lock(target, source=changed)
    assert refused.returncode == 1 and "HOST_BINDING_MISMATCH" in refused.stderr
    assert binding.read_bytes() == record


@pytest.mark.parametrize("output,status,signaled,missing,fragment", [
    (ioreg_output(), 0, False, False, "missing or ambiguous IOPlatformUUID"),
    ("", 0, False, False, "unexpected IOPlatformUUID provider output"),
    (ioreg_output(ID_A.split(":")[-1]), 5, False, False, "provider failed"),
    (ioreg_output(ID_A.split(":")[-1]), 0, True, False, "provider failed"),
    ("", 0, False, True, "cannot run IOPlatformUUID provider"),
    (ioreg_output(ID_A.split(":")[-1], ID_B.split(":")[-1]), 0, False, False, "ambiguous IOPlatformUUID"),
    (ioreg_output("not-a-uuid"), 0, False, False, "malformed IOPlatformUUID"),
    (ioreg_output(ID_A.split(":")[-1]).replace("    }", "    unexpected\n    }"), 0, False, False, "unexpected IOPlatformUUID provider output"),
])
def test_macos_provider_fails_closed(tmp_path, output, status, signaled, missing, fragment):
    source, _ = provider_source(tmp_path / "provider", "darwin", output, status, signaled, missing)
    refused = run_lock(tmp_path / "state.lock", source=source)
    assert refused.returncode == 1 and fragment in refused.stderr
    assert not (tmp_path / ".rtbioscan_lock_host_v1").exists()


@pytest.mark.parametrize("output", ["", "0" * 32 + "\n", "x" * 32 + "\n", "a" + "x" * 31 + "\n", "a" * 32 + "\n" + "b" * 32 + "\n"])
def test_linux_machine_id_fails_closed(tmp_path, output):
    source, _ = provider_source(tmp_path / "provider", "linux", output)
    refused = run_lock(tmp_path / "state.lock", source=source)
    assert refused.returncode == 1 and "machine-id" in refused.stderr
    assert not (tmp_path / ".rtbioscan_lock_host_v1").exists()


def test_linux_machine_id_canonicalizes_case_and_unsupported_platform_refuses(tmp_path):
    source, _ = provider_source(tmp_path / "linux", "linux", "ABCDEF0123456789ABCDEF0123456789\n")
    target = tmp_path / "linux-state.lock"
    assert run_lock(target, source=source).returncode == 0
    assert "linux\tmachine-id\tabcdef0123456789abcdef0123456789" in (tmp_path / ".rtbioscan_lock_host_v1").read_text()
    other, _ = provider_source(tmp_path / "other", "unsupported-platform", host="same-host")
    refused = run_lock(tmp_path / "other-state.lock", source=other)
    assert refused.returncode == 1 and "unsupported platform" in refused.stderr


def test_hostname_candidate_record_requires_explicit_rebind(tmp_path):
    source, helper = scratch_source(tmp_path / "source", host="same-host", identity=ID_A)
    binding = tmp_path / ".rtbioscan_lock_host_v1"
    old = "hostname-v1:" + b"same-host".hex()
    original = ("rtbioscan-lock-host-v1\t" + b"same-host".hex() + "\n").encode()
    binding.write_bytes(original)
    denied = run_lock(tmp_path / "state.lock", source=source)
    assert denied.returncode == 1 and "HOST_BINDING_LEGACY" in denied.stderr
    assert f"rebind-host '{tmp_path}' '{old}' '{ID_A}' --confirm" in denied.stderr
    without_confirmation = subprocess.run(["perl", str(helper), "rebind-host", str(tmp_path), old, ID_A], capture_output=True)
    assert without_confirmation.returncode != 0 and binding.read_bytes() == original
    confirmed = subprocess.run(["perl", str(helper), "rebind-host", str(tmp_path), old, ID_A, "--confirm"], capture_output=True)
    assert confirmed.returncode == 0, confirmed.stderr
    assert ID_A.split(":")[-1] in binding.read_text()
    audit = (tmp_path / ".lock_migration.log").read_text()
    assert f"old={old} new={ID_A}" in audit


def test_failed_rebind_keeps_binding_byte_exact(tmp_path):
    a, _ = scratch_source(tmp_path / "A", identity=ID_A)
    _, helper_b = scratch_source(tmp_path / "B", identity=ID_B)
    target = tmp_path / "state.lock"
    assert run_lock(target, source=a).returncode == 0
    assert_drained(target)
    binding = tmp_path / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    failed = subprocess.run(["perl", str(helper_b), "rebind-host", str(tmp_path), ID_A, ID_B, "--confirm"],
                            capture_output=True, timeout=8)
    assert failed.returncode != 0 and b"compatibility fence exists" in failed.stderr
    assert binding.read_bytes() == original


def test_reaper_start_failure_removes_only_own_fence(tmp_path):
    source, _ = scratch_source(tmp_path, edits=(("pipe(my $read_ack, my $write_ack) or return 0;", "pipe(my $read_ack, my $write_ack) or return 0; return 0;"),))
    target = tmp_path / "fail.lock"
    failed = run_lock(target, source=source)
    assert failed.returncode == 1 and "cannot start compatibility-fence drain reaper" in failed.stderr
    assert not pathlib.Path(str(target) + ".lockdir").exists()
    assert run_lock(target).returncode == 0
    assert_drained(target)


def test_probe_detects_simulated_false_success_and_skip_mutant(tmp_path):
    target = tmp_path / "probe.lock"
    bad_fs = (("my $got = flock($b, LOCK_EX|LOCK_NB);", "my $got = 1;"),)
    source, _ = scratch_source(tmp_path / "broken", edits=bad_fs)
    failed = run_lock(target, source=source)
    assert failed.returncode == 1 and "flock conformance probe failed" in failed.stderr
    skipped, _ = scratch_source(tmp_path / "skipped", edits=bad_fs + (("probe_conformance($dir, $guard);", "close $guard; # probe skipped"),))
    assert run_lock(target, source=skipped).returncode == 0
    assert_drained(target)


def test_legacy_live_holder_and_preupgrade_contender(tmp_path):
    target = tmp_path / "mixed.lock"
    fence = pathlib.Path(str(target) + ".lockdir")
    marker = tmp_path / "legacy_inside"
    old = '''set -euo pipefail
while ! mkdir "$1.lockdir" 2>/dev/null; do sleep 0.02; done
: > "$2"
sleep 0.4
rmdir "$1.lockdir"
'''
    legacy = subprocess.Popen([BASH, "-c", old, "_", str(target), str(marker)], start_new_session=True)
    try:
        assert wait_for(marker.exists)
        start = time.monotonic()
        result = run_lock(target)
        elapsed = time.monotonic() - start
        assert result.returncode == 0, result.stderr
        assert elapsed >= 0.25
        legacy.wait(timeout=3)
        assert_drained(target)
    finally:
        if legacy.poll() is None:
            os.killpg(legacy.pid, signal.SIGKILL)
            legacy.wait(timeout=3)
    ready = tmp_path / "new_inside"
    owner = subprocess.Popen([BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"; : > "$3"; sleep 0.4; release_lock "$2"',
                              "_", str(SHELL), str(target), str(ready)], start_new_session=True)
    try:
        assert wait_for(ready.exists)
        blocked = subprocess.run([BASH, "-c", 'mkdir "$1.lockdir" 2>/dev/null', "_", str(target)], timeout=2)
        assert blocked.returncode != 0
        owner.wait(timeout=3)
        assert_drained(target)
        passed = subprocess.run([BASH, "-c", 'mkdir "$1.lockdir" && rmdir "$1.lockdir"', "_", str(target)], timeout=2)
        assert passed.returncode == 0
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_sigterm_shell_and_child_only_sigkill(tmp_path):
    target = tmp_path / "signals.lock"
    ready = tmp_path / "ready"
    owner = subprocess.Popen([BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"; : > "$3"; exec sleep 10',
                              "_", str(SHELL), str(target), str(ready)], start_new_session=True)
    try:
        assert wait_for(ready.exists)
        os.kill(owner.pid, signal.SIGTERM)
        owner.wait(timeout=3)
        assert run_lock(target).returncode == 0
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)
    ready.unlink()
    childpid = tmp_path / "childpid"
    owner_script = '''set -euo pipefail
source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"
sleep 10 & echo $! > "$3"
: > "$4"
wait || true
release_lock "$2"
'''
    owner = subprocess.Popen([BASH, "-c", owner_script, "_", str(SHELL), str(target), str(childpid), str(ready)], start_new_session=True)
    try:
        assert wait_for(ready.exists)
        os.kill(int(childpid.read_text()), signal.SIGKILL)
        owner.wait(timeout=3)
        assert run_lock(target).returncode == 0
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_legacy_permission_denied_and_process_start_mismatch(tmp_path):
    target = tmp_path / "legacy.lock"
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    import socket
    (fence / "meta.env").write_text(f"pid=1\nhost={socket.gethostname()}\nstarted_epoch=1\n")
    failed = run_lock(target, wait="0.2")
    assert failed.returncode == 1 and fence.exists()
    (fence / "meta.env").write_text(f"pid={os.getpid()}\nhost={socket.gethostname()}\nstarted_epoch=1\n")
    failed = run_lock(target, wait="0.2")
    assert failed.returncode == 1 and fence.exists()


def test_stale_diagnostic_record_ignores_pid_and_host(tmp_path):
    target = tmp_path / "stale.lock"
    lockfile = pathlib.Path(str(target) + ".flock")
    lockfile.write_text(f"v=2 host=elsewhere pid={os.getpid()} started=1 token=0000000000000000\n")
    assert run_lock(target).returncode == 0
    assert b"host=elsewhere" not in lockfile.read_bytes()
    assert_drained(target)


def test_reaper_death_stale_token_and_two_adopters(tmp_path):
    source, _ = scratch_source(tmp_path / "dead_reaper", edits=((
        'write_all($ack, "READY\\n");\n    close $ack;\n    flock($independent, LOCK_EX)',
        'write_all($ack, "READY\\n");\n    close $ack;\n    _exit(0);\n    flock($independent, LOCK_EX)'),))
    target = tmp_path / "adopters.lock"
    assert run_lock(target, source=source).returncode == 0
    fence = pathlib.Path(str(target) + ".lockdir")
    assert fence.exists() and "rtbioscan-fence-v2" in (fence / "v2owner").read_text()
    inside = tmp_path / "inside"
    violation = tmp_path / "violation"
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=5; init_lock_helpers; acquire_lock "$2"
if ! mkdir "$3" 2>/dev/null; then : > "$4"; fi
sleep 0.15
rmdir "$3"
release_lock "$2"
'''
    ps = [subprocess.Popen([BASH, "-c", script, "_", str(SHELL), str(target), str(inside), str(violation)],
                           stderr=subprocess.PIPE, start_new_session=True) for _ in range(2)]
    try:
        results = [p.communicate(timeout=8) for p in ps]
        assert [p.returncode for p in ps] == [0, 0], results
        assert not violation.exists()
        assert_drained(target)
    finally:
        for p in ps:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=3)


def test_late_reaper_never_removes_new_token(tmp_path):
    slow, _ = scratch_source(tmp_path / "slow", edits=((
        'flock($independent, LOCK_EX) or _exit(1);',
        'sleep 0.7; flock($independent, LOCK_EX) or _exit(1);'),))
    target = tmp_path / "late.lock"
    started = time.monotonic()
    assert run_lock(target, source=slow).returncode == 0
    assert time.monotonic() - started < 0.5, "reaper retained a task output pipe"
    fence = pathlib.Path(str(target) + ".lockdir")
    old_token = (fence / "v2owner").read_text()
    ready = tmp_path / "ready"
    owner = subprocess.Popen([BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"; : > "$3"; sleep 0.9; release_lock "$2"',
                              "_", str(SHELL), str(target), str(ready)], start_new_session=True)
    try:
        assert wait_for(ready.exists)
        new_token = (fence / "v2owner").read_text()
        assert new_token != old_token
        time.sleep(0.75)
        assert fence.exists() and (fence / "v2owner").read_text() == new_token
        owner.wait(timeout=3)
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_two_simulated_first_hosts_only_one_wins(tmp_path):
    a, _ = scratch_source(tmp_path / "A", host="host-a", identity=ID_A)
    b, _ = scratch_source(tmp_path / "B", host="host-b", identity=ID_B)
    target = tmp_path / "first.lock"
    script = 'set -euo pipefail; source "$1"; LOCK_WAIT=2; init_lock_helpers; acquire_lock "$2"; sleep 0.1; release_lock "$2"'
    ps = [subprocess.Popen([BASH, "-c", script, "_", str(src), str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                           start_new_session=True) for src in (a, b)]
    try:
        output = [p.communicate(timeout=8) for p in ps]
        assert sorted(p.returncode for p in ps) == [0, 1], output
        assert (tmp_path / ".rtbioscan_lock_host_v1").read_text() in (
            "rtbioscan-lock-host-v2\tmacos\tIOPlatformUUID\t" + ID_A.split(":")[-1] + "\t" + b"host-a".hex() + "\n",
            "rtbioscan-lock-host-v2\tmacos\tIOPlatformUUID\t" + ID_B.split(":")[-1] + "\t" + b"host-b".hex() + "\n")
        assert_drained(target)
    finally:
        for p in ps:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=3)


def test_rebind_refuses_live_lock_and_any_fence_and_serializes(tmp_path):
    a, _ = scratch_source(tmp_path / "A", host="host-a", identity=ID_A)
    b, helper_b = scratch_source(tmp_path / "B", host="host-b", identity=ID_B)
    target = tmp_path / "rebind.lock"
    old, new = ID_A, ID_B
    ready = tmp_path / "ready"
    owner = subprocess.Popen([BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"; : > "$3"; sleep 0.6; release_lock "$2"',
                              "_", str(a), str(target), str(ready)], start_new_session=True)
    command = ["perl", str(helper_b), "rebind-host", str(tmp_path), old, new, "--confirm"]
    try:
        assert wait_for(ready.exists)
        held = subprocess.run(command, capture_output=True, timeout=8)
        assert held.returncode != 0 and b"kernel lock active" in held.stderr
        owner.wait(timeout=3)
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    blocked = subprocess.run(command, capture_output=True, timeout=8)
    assert blocked.returncode != 0 and b"compatibility fence exists" in blocked.stderr
    fence.rmdir()
    ps = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True) for _ in range(2)]
    try:
        output = [p.communicate(timeout=8) for p in ps]
        assert sorted(p.returncode for p in ps) == [0, 74], output
    finally:
        for p in ps:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=3)


def test_host_check_omission_mutant_and_inode_probe_mutant(tmp_path):
    a, _ = scratch_source(tmp_path / "A", host="host-a", identity=ID_A)
    b, _ = scratch_source(tmp_path / "B", host="host-b", identity=ID_B)
    target = tmp_path / "host.lock"
    assert run_lock(target, source=a).returncode == 0
    assert_drained(target)
    assert run_lock(target, source=b).returncode == 1
    skipped, _ = scratch_source(tmp_path / "skip", host="host-b", identity=ID_B, edits=(
        ("ensure_binding($dir, $current);", "# binding check skipped"),
        ("ensure_binding($dir, $current);", "# binding check skipped"),
        ('read_binding("$dir/$BINDING", $current) eq $current', '1'),
        ('read_binding("$dir/$BINDING", $current) eq $current', '1'),
    ))
    assert run_lock(target, source=skipped).returncode == 0
    assert_drained(target)
    altered, _ = scratch_source(tmp_path / "altered", host="host-a", identity=ID_A, edits=((
        'my @initial = stat($a);',
        'my @initial = stat($a); $initial[1]++;'),))
    denied = run_lock(target, source=altered)
    assert denied.returncode == 1 and "flock conformance probe failed" in denied.stderr
    ignored, _ = scratch_source(tmp_path / "ignored", host="host-a", identity=ID_A, edits=((
        'my @initial = stat($a);',
        'my @initial = stat($a); $initial[1]++;'), (
        'my $stable = @end && S_ISREG($end[2]) && $initial[0] == $end[0] && $initial[1] == $end[1];',
        'my $stable = 1;')))
    assert run_lock(target, source=ignored).returncode == 0
    assert_drained(target)


def test_fd_pool_exhaustion_is_bounded(tmp_path):
    target = tmp_path / "fdpool.lock"
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=0; init_lock_helpers
for fd in 3 4 5 6 7 8 9; do eval "exec $fd</dev/null"; done
if acquire_lock "$2"; then exit 91; fi
'''
    result = subprocess.run([BASH, "-c", script, "_", str(SHELL), str(target)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0 and "no free reset barrier descriptor" in result.stderr
    assert not pathlib.Path(str(target) + ".lockdir").exists()


def test_consensus_prune_contends_with_lock_utils(tmp_path):
    target = tmp_path / "qced.lock"
    fasta = tmp_path / "reads.fasta"
    fasta.write_text(">read1\nACGT\n")
    ids = tmp_path / "ids.list"
    ids.write_text("read1\n")
    recovery = tmp_path / "recovery.list"
    recovery.write_text("")
    apply = tmp_path / "apply.pl"
    apply.write_text('''use strict; use warnings;
my ($fasta,$ids,$out,$stats)=@ARGV;
open my $in,'<',$fasta or die $!;
open my $fh,'>',$out or die $!;
while (<$in>) { print $fh $_ }
close $fh;
open my $sf,'>',$stats or die $!;
print $sf "pruned_count\\t0\\n";
close $sf;
''')
    command = [BASH, str(ROOT / "bin/consensus_prune_apply.sh"),
               "--prune-ids", str(ids), "--recovery-ids", str(recovery), "--fasta", str(fasta),
               "--fasta-tmp", str(tmp_path / "reads.tmp"), "--apply-stats", str(tmp_path / "apply.stats"),
               "--prune-stats", str(tmp_path / "prune.stats"), "--round-cp", str(tmp_path / "round.fasta"),
               "--lock-dir", str(target), "--apply-script", str(apply), "--lock-wait", "0.2"]
    ready = tmp_path / "ready"
    holder = subprocess.Popen([BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"; : > "$3"; sleep 0.8; release_lock "$2"',
                               "_", str(SHELL), str(target), str(ready)], start_new_session=True)
    try:
        assert wait_for(ready.exists)
        blocked = subprocess.run(command, capture_output=True, text=True, timeout=8)
        assert blocked.returncode == 1 and not (tmp_path / "apply.stats").exists()
        holder.wait(timeout=4)
        command[-1] = "3"
        passed = subprocess.run(command, capture_output=True, text=True, timeout=8)
        assert passed.returncode == 0, passed.stderr
        assert (tmp_path / "round.fasta").read_bytes() == fasta.read_bytes()
        assert_drained(target)
    finally:
        if holder.poll() is None:
            os.killpg(holder.pid, signal.SIGKILL)
            holder.wait(timeout=3)


def test_child_that_closes_fds_does_not_extend_lock(tmp_path):
    target = tmp_path / "closed-child.lock"
    ready = tmp_path / "ready"
    childpid = tmp_path / "childpid"
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"
/usr/bin/python3 -c 'import subprocess,sys; p=subprocess.Popen(["sleep","0.8"],close_fds=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,stdin=subprocess.DEVNULL); open(sys.argv[1],"w").write(str(p.pid))' "$3"
: > "$4"
'''
    start = time.monotonic()
    result = subprocess.run([BASH, "-c", script, "_", str(SHELL), str(target), str(childpid), str(ready)], timeout=8)
    assert result.returncode == 0 and ready.exists()
    acquired = run_lock(target, wait="1")
    assert acquired.returncode == 0, acquired.stderr
    assert time.monotonic() - start < 0.8
    assert_drained(target)
    # The non-writing child has a fixed 0.8 s lifetime; allow it to finish.
    time.sleep(0.85)


def test_explicit_adoption_raced_by_new_contender(tmp_path):
    target = tmp_path / "adopt-race.lock"
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    adopt = subprocess.Popen(["perl", str(HELPER), "adopt-legacy", str(fence), "--confirm"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    contender = subprocess.Popen([BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"; : > "$3"; release_lock "$2"',
                                  "_", str(SHELL), str(target), str(tmp_path / "entered")],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        aout = adopt.communicate(timeout=8)
        cout = contender.communicate(timeout=8)
        assert adopt.returncode == 0, aout
        assert contender.returncode in (0, 1), cout
        if contender.returncode == 1:
            assert b"LEGACY_LOCK_UNOWNED" in cout[1] or b"timed out" in cout[1]
            assert run_lock(target).returncode == 0
        assert (tmp_path / "entered").exists() or contender.returncode == 1
        assert_drained(target)
    finally:
        for p in (adopt, contender):
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=3)


def test_preupgrade_mkdir_blocked_until_orphan_writer_finishes(tmp_path):
    target = tmp_path / "mixed-child.lock"
    ready = tmp_path / "ready"
    writes = tmp_path / "writes"
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=3; init_lock_helpers; acquire_lock "$2"
/bin/bash -c 'sleep 0.4; printf done > "$1"' _ "$3" &
: > "$4"
wait
'''
    owner = subprocess.Popen([BASH, "-c", script, "_", str(SHELL), str(target), str(writes), str(ready)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert wait_for(ready.exists)
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=3)
        old = subprocess.run([BASH, "-c", 'mkdir "$1.lockdir" 2>/dev/null', "_", str(target)], timeout=2)
        assert old.returncode != 0
        assert wait_for(writes.exists, 3)
        assert_drained(target)
        later = subprocess.run([BASH, "-c", 'mkdir "$1.lockdir" && rmdir "$1.lockdir"', "_", str(target)], timeout=2)
        assert later.returncode == 0
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_six_state_descriptors_plus_shared_barrier_then_seventh_fails(tmp_path):
    targets = [tmp_path / f"lock{i}" for i in range(7)]
    script = '''set -euo pipefail
source "$1"; LOCK_WAIT=0; init_lock_helpers
for i in 2 3 4 5 6 7; do acquire_lock "${!i}"; done
if acquire_lock "$8"; then exit 91; fi
for i in 2 3 4 5 6 7; do release_lock "${!i}"; done
'''
    result = subprocess.run([BASH, "-c", script, "_", str(SHELL)] + [str(t) for t in targets],
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert "no free lock descriptor" in result.stderr
    for target in targets[:6]:
        assert_drained(target)
    assert pathlib.Path(str(targets[6]) + ".flock").exists()


def test_unreadable_or_nonregular_binding_refused(tmp_path):
    target = tmp_path / "unsafe.lock"
    binding = tmp_path / ".rtbioscan_lock_host_v1"
    binding.mkdir()
    failed = run_lock(target)
    assert failed.returncode == 1 and "host binding" in failed.stderr
    assert binding.is_dir()


def test_real_a0_writer_first_refuses_then_preserves_controls(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    first = run_a0(outdir)
    assert first.returncode == 0, first.stderr
    target = state / ".qced_reads.lock"
    barrier = state / ".rtbioscan_state_reset.flock"
    inode = (barrier.stat().st_ino, pathlib.Path(str(target) + ".flock").stat().st_ino)
    ready = tmp_path / "writer-ready"
    owner = subprocess.Popen(
        [BASH, "-c", 'set -euo pipefail; source "$1"; init_lock_helpers; '
         'acquire_lock "$2"; : > "$3"; sleep 1; release_lock "$2"',
         "_", str(SHELL), str(target), str(ready)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert wait_for(ready.exists)
        fence = pathlib.Path(str(target) + ".lockdir")
        assert fence.is_dir()
        refused = run_a0(outdir, operation="b")
        assert refused.returncode != 0 and "exclusive reset barrier" in refused.stderr
        assert fence.is_dir()
        assert owner.wait(timeout=4) == 0
        assert_drained(target)
        lock_file = pathlib.Path(str(target) + ".flock")
        record = lock_file.read_bytes()
        assert record.startswith(b"v=2 ")
        binding = state / ".rtbioscan_lock_host_v1"
        host_record = binding.read_bytes()
        audit = state / ".lock_migration.log"
        audit.write_bytes(b"preserve audit\n")
        completed = run_a0(outdir, operation="b")
        assert completed.returncode == 0, completed.stderr
        assert (barrier.stat().st_ino, lock_file.stat().st_ino) == inode
        assert lock_file.read_bytes() == record
        assert binding.read_bytes() == host_record
        assert audit.read_bytes() == b"preserve audit\n"
        assert run_lock(target).returncode == 0
        assert_drained(target)
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_real_a0_reset_first_blocks_state_lock_and_fence(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    assert run_a0(outdir).returncode == 0
    target = state / ".qced_reads.lock"
    lock_file = pathlib.Path(str(target) + ".flock")
    inode = (state / ".rtbioscan_state_reset.flock").stat().st_ino, lock_file.stat().st_ino
    (state / "scientific.txt").write_bytes(b"wipe me")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    entered = tmp_path / "reset-entered"
    shim = shim_dir / "rm"
    shim.write_text('#!/bin/sh\n'
                    'if [ ! -e "$RTB_A0_ENTERED" ]; then\n'
                    '  : > "$RTB_A0_ENTERED"\n'
                    '  sleep 1\n'
                    'fi\n'
                    'exec /bin/rm "$@"\n')
    shim.chmod(0o755)
    env = a0_env(outdir, operation="b", wait="2")
    env["PATH"] = str(shim_dir) + os.pathsep + env["PATH"]
    env["RTB_A0_ENTERED"] = str(entered)
    reset = subprocess.Popen([BASH, str(A0_HANDLER)], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    writer_done = tmp_path / "writer-entered"
    writer = None
    try:
        assert wait_for(entered.exists, seconds=5)
        writer = subprocess.Popen(
            [BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; '
             'init_lock_helpers; acquire_lock "$2"; : > "$3"; release_lock "$2"',
             "_", str(SHELL), str(target), str(writer_done)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.2)
        assert not writer_done.exists()
        assert not pathlib.Path(str(target) + ".lockdir").exists()
        _, reset_err = reset.communicate(timeout=10)
        assert reset.returncode == 0, reset_err
        _, writer_err = writer.communicate(timeout=6)
        assert writer.returncode == 0, writer_err
        assert writer_done.exists()
        assert_drained(target)
        assert ((state / ".rtbioscan_state_reset.flock").stat().st_ino,
                lock_file.stat().st_ino) == inode
    finally:
        for proc in (writer, reset):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=3)


def test_real_a0_dead_shell_live_child_retains_barrier(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    assert run_a0(outdir).returncode == 0
    target = state / ".qced_reads.lock"
    ready = tmp_path / "child-ready"
    done = tmp_path / "child-done"
    script = ('set -euo pipefail; source "$1"; init_lock_helpers; acquire_lock "$2"; '
              '(sleep 1; : > "$4") & child=$!; printf "%s" "$child" > "$3"; wait "$child"')
    owner = subprocess.Popen([BASH, "-c", script, "_", str(SHELL), str(target),
                              str(ready), str(done)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    child_pid = None
    try:
        assert wait_for(ready.exists)
        child_pid = int(ready.read_text())
        os.kill(owner.pid, signal.SIGKILL)
        owner.wait(timeout=3)
        assert not done.exists()
        refused = run_a0(outdir, operation="b")
        assert refused.returncode != 0 and "exclusive reset barrier" in refused.stderr
        contender = run_lock(target, wait="0")
        assert contender.returncode == 1
        assert pathlib.Path(str(target) + ".lockdir").is_dir()
        assert wait_for(done.exists, seconds=4)
        assert_drained(target)
        completed = run_a0(outdir, operation="b")
        assert completed.returncode == 0, completed.stderr
        assert run_lock(target).returncode == 0
        assert_drained(target)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=3)
        if child_pid is not None and not done.exists():
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_real_a0_waits_for_slow_drain_reaper(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    assert run_a0(outdir).returncode == 0
    target = state / ".qced_reads.lock"
    slow, _ = scratch_source(tmp_path / "slow", edits=((
        'flock($independent, LOCK_EX) or _exit(1);',
        'sleep 0.8; flock($independent, LOCK_EX) or _exit(1);'),))
    assert run_lock(target, source=slow).returncode == 0
    assert pathlib.Path(str(target) + ".lockdir").is_dir()
    refused = run_a0(outdir, operation="b")
    assert refused.returncode != 0 and "exclusive reset barrier" in refused.stderr
    assert_drained(target)
    completed = run_a0(outdir, operation="b")
    assert completed.returncode == 0, completed.stderr


def test_real_a0_reset_during_owner_record_publication(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    assert run_a0(outdir).returncode == 0
    target = state / ".qced_reads.lock"
    marker = tmp_path / "publishing"
    replacement = ('open(my $pause, ">", ' + repr(str(marker)) + ') or die $!; '
                   'close $pause; sleep 0.8; truncate($owner, 0) or fail')
    paused, _ = scratch_source(tmp_path / "paused", edits=((
        'truncate($owner, 0) or fail', replacement),))
    owner = subprocess.Popen(
        [BASH, "-c", 'set -euo pipefail; source "$1"; init_lock_helpers; '
         'acquire_lock "$2"; release_lock "$2"',
         "_", str(paused), str(target)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        assert wait_for(marker.exists)
        assert pathlib.Path(str(target) + ".lockdir").is_dir()
        refused = run_a0(outdir, operation="b")
        assert refused.returncode != 0 and "exclusive reset barrier" in refused.stderr
        assert owner.wait(timeout=4) == 0
        assert_drained(target)
        assert run_a0(outdir, operation="b").returncode == 0
    finally:
        if owner.poll() is None:
            os.killpg(owner.pid, signal.SIGKILL)
            owner.wait(timeout=3)


def test_stable_lock_and_barrier_symlinks_fail_closed(tmp_path):
    for kind in ("state", "barrier"):
        root = tmp_path / kind
        root.mkdir()
        target = root / ".qced_reads.lock"
        elsewhere = root / "other"
        elsewhere.write_bytes(b"unchanged")
        path = (pathlib.Path(str(target) + ".flock") if kind == "state"
                else root / ".rtbioscan_state_reset.flock")
        path.symlink_to(elsewhere)
        denied = run_lock(target, wait="0")
        assert denied.returncode == 1
        assert path.is_symlink() and elsewhere.read_bytes() == b"unchanged"
        assert not pathlib.Path(str(target) + ".lockdir").exists()


def test_group_writable_control_modes_do_not_define_lock_authority(tmp_path):
    target = tmp_path / ".qced_reads.lock"
    assert run_lock(target).returncode == 0
    assert_drained(target)
    for path in (tmp_path / ".rtbioscan_state_reset.flock",
                 tmp_path / ".rtbioscan_lock_host_v1.guard",
                 tmp_path / ".rtbioscan_lock_host_v1",
                 pathlib.Path(str(target) + ".flock")):
        path.chmod(0o664)
    assert run_lock(target).returncode == 0
    assert_drained(target)


def test_probe_detects_real_guard_inode_replacement(tmp_path):
    target = tmp_path / "replaced.lock"
    marker = tmp_path / "probe-paused"
    injected = ('my $first = <$b_read>; '
                'open(my $pause, ">", ' + repr(str(marker)) + ') or die $!; '
                'close $pause; sleep 0.5;')
    source, _ = scratch_source(tmp_path / "injected", edits=((
        'my $first = <$b_read>;', injected),))
    script = 'set -euo pipefail; source "$1"; init_lock_helpers; acquire_lock "$2"'
    proc = subprocess.Popen([BASH, "-c", script, "_", str(source), str(target)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        assert wait_for(marker.exists)
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"")
        os.replace(replacement, tmp_path / ".rtbioscan_lock_host_v1.guard")
        _, stderr = proc.communicate(timeout=8)
        assert proc.returncode == 1 and b"flock conformance probe failed" in stderr
        assert not pathlib.Path(str(target) + ".lockdir").exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)


def test_stage_a_callers_use_a0_declared_lock_names():
    stage_a = {
        ".blastreport.lock", ".blastreport_sup.lock", ".qced_reads.lock",
        ".otu_size_streak.lock", ".sup_basecall_cache.lock", ".done_pod5.lock",
    }
    a0 = A0_HANDLER.read_text()
    inventory = a0[a0.index("my @names = ('.rtbioscan_state_reset.flock',"):]
    inventory = inventory[:inventory.index(");")]
    declared = set(re.findall(r"'(\.[a-z0-9_.]+\.flock)'", inventory))
    assert {name + ".flock" for name in stage_a} <= declared
    main = (ROOT / "main.nf").read_text()
    assert all("/" + name in main for name in stage_a)
    assert '--lock-dir       "${ongoingStateDir}/_state/.qced_reads.lock"' in main
    assert 'lock_file="${target}.flock"' in SHELL.read_text()


def test_shared_barrier_precedes_wait_on_per_state_lock(tmp_path):
    outdir = tmp_path / "out"
    outdir.mkdir()
    state = a0_state(outdir)
    assert run_a0(outdir).returncode == 0
    target = state / ".qced_reads.lock"
    state_lock = os.open(str(target) + ".flock", os.O_RDWR)
    fcntl.flock(state_lock, fcntl.LOCK_EX)
    barrier = state / ".rtbioscan_state_reset.flock"
    observer = os.open(barrier, os.O_RDONLY)
    entered = tmp_path / "writer-entered"
    writer = subprocess.Popen(
        [BASH, "-c", 'set -euo pipefail; source "$1"; LOCK_WAIT=3; '
         'init_lock_helpers; acquire_lock "$2"; : > "$3"; release_lock "$2"',
         "_", str(SHELL), str(target), str(entered)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        def barrier_is_shared():
            try:
                fcntl.flock(observer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(observer, fcntl.LOCK_UN)
            return False

        assert wait_for(barrier_is_shared)
        assert not entered.exists()
        refused = run_a0(outdir, operation="b")
        assert refused.returncode != 0 and "exclusive reset barrier" in refused.stderr
    finally:
        os.close(observer)
        os.close(state_lock)
        if writer.poll() is None:
            try:
                writer.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(writer.pid, signal.SIGKILL)
                writer.communicate(timeout=3)
    assert writer.returncode == 0
    assert entered.exists()
    assert_drained(target)


def test_token_scoped_cleanup_preserves_newer_fence(tmp_path):
    fence = tmp_path / ".qced_reads.lock.lockdir"
    fence.mkdir()
    record = "rtbioscan-fence-v2\t" + "a" * 16 + "\n"
    (fence / "v2owner").write_text(record)
    dispatch = "if ($command eq 'preflight') { preflight_command(@ARGV); }"
    hook = (
        "if ($command eq 'test-token-cleanup') { "
        "my ($dir, $expected) = @ARGV; "
        "exit remove_fence_if_token($dir, $expected) ? 1 : 0; }\n"
        "elsif ($command eq 'preflight') { preflight_command(@ARGV); }"
    )
    _, helper = scratch_source(tmp_path / "hook", edits=((dispatch, hook),))
    late = subprocess.run(["perl", str(helper), "test-token-cleanup",
                           str(fence), "b" * 16], capture_output=True, timeout=5)
    assert late.returncode == 0
    assert (fence / "v2owner").read_text() == record
    matching = subprocess.run(["perl", str(helper), "test-token-cleanup",
                               str(fence), "a" * 16], capture_output=True, timeout=5)
    assert matching.returncode == 1
    assert not fence.exists()


def test_state_root_substitution_after_probe_is_refused(tmp_path):
    root = tmp_path / "state"
    root.mkdir()
    target = root / ".qced_reads.lock"
    source, _ = scratch_source(tmp_path / "source")
    marker = tmp_path / "preflight-complete"
    shell_text = source.read_text()
    needle = "barrier_fd=$__lock_utils_free_fd"
    assert needle in shell_text
    source.write_text(shell_text.replace(
        needle, ': > "$RTB_ROOT_SWAP_MARKER"; sleep 0.5\n\t\t' + needle, 1))
    env = dict(os.environ, RTB_ROOT_SWAP_MARKER=str(marker))
    script = 'set -euo pipefail; source "$1"; init_lock_helpers; acquire_lock "$2"'
    proc = subprocess.Popen([BASH, "-c", script, "_", str(source), str(target)],
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    try:
        assert wait_for(marker.exists)
        old = tmp_path / "old-state"
        root.rename(old)
        root.mkdir()
        for name in (".rtbioscan_lock_host_v1", ".rtbioscan_lock_host_v1.guard",
                     ".rtbioscan_state_reset.flock"):
            shutil.copy2(old / name, root / name)
        _, stderr = proc.communicate(timeout=6)
        assert proc.returncode == 1 and b"state root changed" in stderr
        assert not pathlib.Path(str(target) + ".lockdir").exists()
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)
