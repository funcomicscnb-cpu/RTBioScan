"""Focused behavioral checks for the shared Stage A fence recovery handoff."""

import os
import pathlib
import re
import signal
import socket
import subprocess
import fcntl

import pytest

from tests.test_fd_lock import (
    BASH, ID_A, ID_B, a0_state,
    assert_drained, run_a0, run_lock, scratch_source, wait_for,
)


NAMES = (
    ".dorado.lock", ".blastreport.lock", ".blastreport_sup.lock",
    ".qced_reads.lock", ".otu_size_streak.lock", ".sup_basecall_cache.lock",
    ".done_pod5.lock", ".report_history.lock", ".report_live_publish.lock",
)
TOKEN = "0123456789abcdef"


def fence_at(target, contents=None):
    fence = pathlib.Path(str(target) + ".lockdir")
    fence.mkdir()
    if contents is not None:
        (fence / "v2owner").write_text(contents)
    return fence


def v2_fence(target):
    return fence_at(target, f"rtbioscan-fence-v2\t{TOKEN}\n")


def recovery_command(stderr):
    match = re.search(r"^Recovery command \(run after related writers have stopped\): (.+)$", stderr, re.M)
    assert match, stderr
    return match.group(1)


@pytest.mark.parametrize("name", NAMES)
def test_reset_prints_executable_zero_wait_recovery_for_each_declared_fence(tmp_path, name):
    outdir = tmp_path / "--out ' \" ; touch INJECTION é"
    state = a0_state(outdir)
    payload = state / "payload.txt"
    payload.write_bytes(b"protected scientific bytes\n")
    target = state / name
    stable = pathlib.Path(str(target) + ".flock")
    for declared in NAMES:
        pathlib.Path(str(state / declared) + ".flock").touch()
    assert run_lock(target).returncode == 0
    assert_drained(target)
    inode = stable.stat().st_ino
    fence = v2_fence(target)
    old_fence = (fence / "v2owner").read_bytes()
    state_before = {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()}

    refused = run_a0(outdir)
    assert refused.returncode == 1
    assert payload.read_bytes() == b"protected scientific bytes\n"
    assert (fence / "v2owner").read_bytes() == old_fence
    assert {str(p.relative_to(state)): p.read_bytes() for p in state.rglob("*") if p.is_file()} == state_before
    assert stable.stat().st_ino == inode
    command = recovery_command(refused.stderr)
    assert command.startswith("bash -c 'source \"$1\"; init_lock_helpers; LOCK_WAIT=0 acquire_lock \"$2\"' fence-recovery ")
    assert "After successful recovery, rerun restart_mode=reset." in refused.stderr
    assert not command.endswith(".lockdir'")
    recovered = subprocess.run([BASH, "-c", command], cwd=tmp_path, capture_output=True, text=True, timeout=12)
    assert recovered.returncode == 0, recovered.stderr
    assert_drained(target)
    assert not (tmp_path / "INJECTION").exists()
    assert stable.stat().st_ino == inode
    reset = run_a0(outdir, operation="b")
    assert reset.returncode == 0, reset.stderr


@pytest.mark.parametrize("shape,recovers", [
    ("empty", False), ("dead", True), ("live", False),
    ("foreign", False), ("corrupt", False),
])
def test_printed_command_uses_existing_legacy_classification(tmp_path, shape, recovers):
    outdir = tmp_path / "legacy case"
    state = a0_state(outdir)
    target = state / ".qced_reads.lock"
    fence = fence_at(target)
    if shape == "dead":
        (fence / "meta.env").write_text(f"pid=99999999\nhost={socket.gethostname()}\n")
    elif shape == "live":
        (fence / "meta.env").write_text(f"pid={os.getpid()}\nhost={socket.gethostname()}\n")
    elif shape == "foreign":
        (fence / "meta.env").write_text("pid=99999999\nhost=foreign-host\n")
    elif shape == "corrupt":
        (fence / "v2owner").write_text("invalid\n")
    before = sorted((p.name, p.read_bytes()) for p in fence.iterdir())
    refused = run_a0(outdir)
    assert refused.returncode == 1 and fence.exists()
    result = subprocess.run([BASH, "-c", recovery_command(refused.stderr)],
                            capture_output=True, text=True, timeout=12)
    if recovers:
        assert result.returncode == 0, result.stderr
        assert_drained(target)
        assert run_a0(outdir, operation="b").returncode == 0
    else:
        assert result.returncode != 0
        assert sorted((p.name, p.read_bytes()) for p in fence.iterdir()) == before
        assert run_a0(outdir, operation="b").returncode == 1
        if shape == "empty":
            assert "LEGACY_LOCK_UNOWNED" in result.stderr
            adopt = re.search(r"run: (perl .+ --confirm)", result.stderr)
            assert adopt, result.stderr
            adopted = subprocess.run([BASH, "-c", adopt.group(1)], capture_output=True, text=True, timeout=12)
            assert adopted.returncode == 0, adopted.stderr
            assert run_a0(outdir, operation="c").returncode == 0


def test_permission_indeterminate_legacy_owner_stays_fenced(tmp_path):
    state = a0_state(tmp_path / "permission case")
    target = state / ".done_pod5.lock"
    fence = fence_at(target)
    record = f"pid=99999999\nhost={socket.gethostname()}\n"
    (fence / "meta.env").write_text(record)
    source, _ = scratch_source(
        tmp_path / "simulated-eperm",
        edits=(("!kill(0, $pid)", "do { $! = Errno::EPERM; 1 }"),),
    )
    denied = run_lock(target, wait="0", source=source)
    assert denied.returncode != 0
    assert "reclaiming" not in denied.stderr
    assert (fence / "meta.env").read_text() == record


def bound_state(tmp_path):
    source_a, _ = scratch_source(tmp_path / "A", identity=ID_A)
    source_b, helper_b = scratch_source(tmp_path / "B", identity=ID_B)
    state = a0_state(tmp_path / "output")
    target = state / ".report_history.lock"
    assert run_lock(target, source=source_a).returncode == 0
    assert_drained(target)
    return state, target, source_a, source_b, helper_b


def rebind(helper, state):
    return subprocess.run(["perl", str(helper), "rebind-host", str(state), ID_A, ID_B, "--confirm"],
                          capture_output=True, text=True, timeout=12)


def test_rebind_recovers_exact_v2_and_same_command_is_idempotent(tmp_path):
    state, target, _, source_b, helper_b = bound_state(tmp_path)
    fence = v2_fence(target)
    stable = pathlib.Path(str(target) + ".flock")
    inode = stable.stat().st_ino
    unrelated = state / "scientific.bin"
    unrelated.write_bytes(b"unchanged\0bytes")
    denied = run_lock(target, source=source_b, wait="0")
    assert denied.returncode == 1 and "HOST_BINDING_MISMATCH" in denied.stderr
    assert "old host is stopped or no longer writing" in denied.stderr
    assert "all prior pipeline/task writers are stopped" in denied.stderr
    assert "one host with coherent local kernel locks" in denied.stderr
    command = re.search(r"run: (perl .+ --confirm)", denied.stderr)
    assert command, denied.stderr
    first = subprocess.run([BASH, "-c", command.group(1)], capture_output=True, text=True, timeout=12)
    assert first.returncode == 0, first.stderr
    assert not fence.exists()
    assert stable.stat().st_ino == inode
    assert unrelated.read_bytes() == b"unchanged\0bytes"
    binding = (state / ".rtbioscan_lock_host_v1").read_bytes()
    audit = (state / ".lock_migration.log").read_text()
    assert "fence-recovery complete" in audit and "rebind-host complete" in audit
    second = subprocess.run([BASH, "-c", command.group(1)], capture_output=True, text=True, timeout=12)
    assert second.returncode == 0, second.stderr
    assert (state / ".rtbioscan_lock_host_v1").read_bytes() == binding
    assert (state / ".lock_migration.log").read_text() == audit


@pytest.mark.parametrize("shape", ["empty", "legacy", "dead", "foreign", "corrupt", "unexpected-file", "symlink", "hardlink", "missing"])
def test_rebind_refuses_non_v2_or_unsafe_stable_lock_without_publishing(tmp_path, shape):
    state, target, _, _, helper_b = bound_state(tmp_path)
    binding = state / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    fence = fence_at(target)
    stable = pathlib.Path(str(target) + ".flock")
    if shape == "legacy":
        (fence / "meta.env").write_text(f"pid={os.getpid()}\nhost={socket.gethostname()}\n")
    elif shape == "dead":
        (fence / "meta.env").write_text(f"pid=99999999\nhost={socket.gethostname()}\n")
    elif shape == "foreign":
        (fence / "meta.env").write_text("pid=99999999\nhost=foreign-host\n")
    elif shape == "corrupt":
        (fence / "v2owner").write_text("rtbioscan-fence-v2\tWRONG\n")
    elif shape == "unexpected-file":
        fence.rmdir()
        fence.write_bytes(b"not a directory\n")
    elif shape in ("symlink", "hardlink", "missing"):
        (fence / "v2owner").write_text(f"rtbioscan-fence-v2\t{TOKEN}\n")
        if shape == "symlink":
            saved = state / "saved-stable"
            stable.rename(saved)
            stable.symlink_to(saved)
        elif shape == "hardlink":
            os.link(stable, state / "second-link")
        else:
            stable.unlink()
    before = (fence.read_bytes() if fence.is_file() else
              sorted((p.name, p.read_bytes()) for p in fence.iterdir()))
    result = rebind(helper_b, state)
    assert result.returncode != 0
    assert binding.read_bytes() == original
    assert (fence.read_bytes() if fence.is_file() else
            sorted((p.name, p.read_bytes()) for p in fence.iterdir())) == before
    audit = state / ".lock_migration.log"
    assert not audit.exists() or "rebind-host complete" not in audit.read_text()


def test_rebind_token_change_at_final_recheck_keeps_old_binding(tmp_path):
    state, target, _, _, _ = bound_state(tmp_path)
    fence = v2_fence(target)
    binding = state / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    injection = "my ($name, $target, $token, $dev, $ino, $owner_dev, $owner_ino) = @$fence;"
    replacement = injection + "\n        open(my $changed, '>', \"$dir/$name/v2owner\") or die; print {$changed} \"rtbioscan-fence-v2\\tffffffffffffffff\\n\"; close $changed;"
    _, helper = scratch_source(tmp_path / "mutator", identity=ID_B, edits=((injection, replacement),))
    result = rebind(helper, state)
    assert result.returncode != 0
    assert binding.read_bytes() == original
    assert fence.exists() and "ffffffffffffffff" in (fence / "v2owner").read_text()
    audit = state / ".lock_migration.log"
    assert not audit.exists() or "rebind-host complete" not in audit.read_text()


def test_rebind_same_token_owner_replacement_at_final_recheck_refuses(tmp_path):
    state, target, _, _, _ = bound_state(tmp_path)
    fence = v2_fence(target)
    binding = state / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    injection = "my ($name, $target, $token, $dev, $ino, $owner_dev, $owner_ino) = @$fence;"
    replacement = injection + "\n        unlink(\"$dir/$name/v2owner\") or die; open(my $changed, '>', \"$dir/$name/v2owner\") or die; print {$changed} \"rtbioscan-fence-v2\\t$token\\n\"; close $changed;"
    _, helper = scratch_source(tmp_path / "mutator", identity=ID_B, edits=((injection, replacement),))
    result = rebind(helper, state)
    assert result.returncode != 0
    assert binding.read_bytes() == original
    assert fence.exists() and (fence / "v2owner").read_text() == f"rtbioscan-fence-v2\t{TOKEN}\n"


@pytest.mark.parametrize("dead_shell", [False, True])
def test_rebind_cannot_reclaim_live_writer_or_inherited_child(tmp_path, dead_shell):
    state, target, source_a, _, helper_b = bound_state(tmp_path)
    ready = tmp_path / "ready"
    child_pid_file = tmp_path / "child_pid"
    script = 'source "$1"; init_lock_helpers; LOCK_WAIT=2 acquire_lock "$2" || exit 9; sleep 30 & echo $! > "$4"; : > "$3"; wait'
    writer = subprocess.Popen([BASH, "-c", script, "_", str(source_a), str(target), str(ready), str(child_pid_file)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    child_pid = None
    try:
        assert wait_for(ready.exists, seconds=5)
        child_pid = int(child_pid_file.read_text())
        fence = pathlib.Path(str(target) + ".lockdir")
        original_fence = (fence / "v2owner").read_bytes()
        binding = state / ".rtbioscan_lock_host_v1"
        original_binding = binding.read_bytes()
        if dead_shell:
            writer.kill()
            writer.wait(timeout=3)
        denied = rebind(helper_b, state)
        assert denied.returncode != 0
        assert "kernel lock active" in denied.stderr
        assert (fence / "v2owner").read_bytes() == original_fence
        assert binding.read_bytes() == original_binding
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.wait(timeout=3)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert_drained(target)


def test_rebind_stable_inode_replaced_after_lock_acquisition_refuses(tmp_path):
    state, target, _, _, _ = bound_state(tmp_path)
    fence = v2_fence(target)
    binding = state / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    injection = "my ($name, $target, $token, $dev, $ino, $owner_dev, $owner_ino) = @$fence;"
    replacement = injection + "\n        unlink(\"$dir/$target.flock\") or die; open(my $new_lock, '>', \"$dir/$target.flock\") or die; close $new_lock;"
    _, helper = scratch_source(tmp_path / "mutator", identity=ID_B, edits=((injection, replacement),))
    result = rebind(helper, state)
    assert result.returncode != 0
    assert binding.read_bytes() == original
    assert fence.exists()


def test_rebind_requires_stable_exclusivity_even_when_barrier_is_free(tmp_path):
    state, target, _, _, helper_b = bound_state(tmp_path)
    fence = v2_fence(target)
    binding = state / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    with pathlib.Path(str(target) + ".flock").open("r+b") as stable:
        fcntl.flock(stable.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = rebind(helper_b, state)
        assert result.returncode != 0
        assert "kernel lock active" in result.stderr
        assert fence.exists()
        assert binding.read_bytes() == original


@pytest.mark.parametrize("cut", ["before-fence-removal", "after-fence-removal"])
def test_killed_rebind_preserves_old_binding_and_confirmed_retry_converges(tmp_path, cut):
    state, target, _, _, normal_helper = bound_state(tmp_path)
    fence = v2_fence(target)
    binding = state / ".rtbioscan_lock_host_v1"
    original = binding.read_bytes()
    if cut == "before-fence-removal":
        injection = "remove_fence_if_token($fence_path, $token, $dev, $ino, $owner_dev, $owner_ino)"
    else:
        injection = "if (defined $temp && !rename($temp, $path)) {"
    _, slow_helper = scratch_source(tmp_path / "slow", identity=ID_B,
                                    edits=((injection, "sleep 10; " + injection),))
    args = ["perl", str(slow_helper), "rebind-host", str(state), ID_A, ID_B, "--confirm"]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        audit = state / ".lock_migration.log"
        if cut == "before-fence-removal":
            assert wait_for(lambda: audit.exists() and "fence-recovery intent" in audit.read_text(), seconds=5)
            assert fence.exists()
        else:
            assert wait_for(lambda: not fence.exists(), seconds=5)
        process.kill()
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    assert binding.read_bytes() == original
    assert "rebind-host complete" not in audit.read_text()
    retried = rebind(normal_helper, state)
    assert retried.returncode == 0, retried.stderr
    assert not fence.exists()
    assert ID_B.split(":")[-1].encode() in binding.read_bytes()


def evidence_snapshot(path):
    st = path.stat()
    return (st.st_ino, st.st_mode & 0o7777, st.st_mtime_ns,
            (path / "evidence.txt").read_bytes())


@pytest.mark.parametrize("name", NAMES)
def test_rebind_exact_stage_a_allowlist_and_near_misses(tmp_path, name):
    state, _, _, _, helper_b = bound_state(tmp_path)
    target = state / name
    stable = pathlib.Path(str(target) + ".flock")
    stable.touch(exist_ok=True)
    stable_inode = stable.stat().st_ino
    fence = v2_fence(target)
    near_names = (
        name + ".lockdirx", name + ".lockdir.operator-" + "a" * 64,
        ".round_inflight.lockdir.operator-" + "a" * 64,
        ".round_inflight.lockdir.reclaim-" + "a" * 64,
        ".round_inflight.lockdir.release-" + "a" * 64,
        ".unrelated.lockdir.extra",
    )
    preserved = {}
    for near in near_names:
        path = state / near
        path.mkdir()
        (path / "evidence.txt").write_bytes(b"operator evidence\0")
        path.chmod(0o750)
        preserved[near] = evidence_snapshot(path)
    result = rebind(helper_b, state)
    assert result.returncode == 0, result.stderr
    assert not fence.exists()
    assert stable.stat().st_ino == stable_inode
    for near, before in preserved.items():
        assert evidence_snapshot(state / near) == before
    audit = (state / ".lock_migration.log").read_text()
    assert audit.count("rebind-host fence-recovery complete ") == 1
    assert f"namespace={name}" in audit


@pytest.mark.parametrize("name", (
    ".round_inflight.lockdir", ".report_render.lock.lockdir",
    ".Dorado.lock.lockdir", "unknown.lockdir",
    "x.dorado.lock.lockdir", ".dorado.lock.lockdirx.lockdir",
    ".doradox.lock.lockdir", ".dorado.lock.lockdir.lockdir",
))
def test_rebind_refuses_noneligible_exact_lockdir_suffix(tmp_path, name):
    state, target, _, _, helper_b = bound_state(tmp_path)
    entry = state / name
    entry.mkdir()
    (entry / "v2owner").write_text(f"rtbioscan-fence-v2\t{TOKEN}\n")
    (entry / "evidence.txt").write_bytes(b"preserve this evidence\0")
    entry.chmod(0o750)
    before = evidence_snapshot(entry)
    owner_before = (entry / "v2owner").read_bytes()
    binding = state / ".rtbioscan_lock_host_v1"
    binding_before = binding.read_bytes()
    audit = state / ".lock_migration.log"
    audit_before = audit.read_bytes() if audit.exists() else None
    stable = pathlib.Path(str(target) + ".flock")
    stable_inode = stable.stat().st_ino
    result = rebind(helper_b, state)
    assert result.returncode == 74
    assert f"cannot rebind while compatibility fence exists: {name}" in result.stderr
    assert evidence_snapshot(entry) == before
    assert (entry / "v2owner").read_bytes() == owner_before
    assert binding.read_bytes() == binding_before
    assert (audit.read_bytes() if audit.exists() else None) == audit_before
    assert stable.stat().st_ino == stable_inode


def test_valid_v2_near_miss_with_stable_lock_still_refuses(tmp_path):
    state, _, _, _, helper_b = bound_state(tmp_path)
    target = state / ".doradox.lock"
    stable = pathlib.Path(str(target) + ".flock")
    stable.touch()
    stable_inode = stable.stat().st_ino
    fence = v2_fence(target)
    before = (fence.stat().st_ino, fence.stat().st_mode, fence.stat().st_mtime_ns,
              (fence / "v2owner").read_bytes())
    binding = state / ".rtbioscan_lock_host_v1"
    binding_before = binding.read_bytes()
    audit = state / ".lock_migration.log"
    audit_before = audit.read_bytes() if audit.exists() else None
    denied = rebind(helper_b, state)
    assert denied.returncode == 74
    assert "cannot rebind while compatibility fence exists: .doradox.lock.lockdir" in denied.stderr
    assert fence.exists()
    assert (fence.stat().st_ino, fence.stat().st_mode, fence.stat().st_mtime_ns,
            (fence / "v2owner").read_bytes()) == before
    assert binding.read_bytes() == binding_before
    assert (audit.read_bytes() if audit.exists() else None) == audit_before
    assert stable.stat().st_ino == stable_inode


def test_case_insensitive_dorado_alias_blocks_rebind_and_acquisition(tmp_path):
    state, _, _, source_b, helper_b = bound_state(tmp_path)
    alias = state / ".Dorado.lock.lockdir"
    alias.mkdir()
    (alias / "v2owner").write_text(f"rtbioscan-fence-v2\t{TOKEN}\n")
    (state / ".Dorado.lock.flock").touch()
    if not (state / ".dorado.lock.lockdir").exists():
        pytest.skip("case-sensitive test filesystem")
    binding = state / ".rtbioscan_lock_host_v1"
    before = binding.read_bytes()
    denied = rebind(helper_b, state)
    assert denied.returncode == 74 and binding.read_bytes() == before
    ordinary = run_lock(state / ".dorado.lock", source=source_b, wait="0")
    assert ordinary.returncode != 0 and alias.exists()


def test_rebind_exact_fence_in_hostile_parent_path(tmp_path):
    hostile = tmp_path / (".round_inflight.lockdir.operator-" + "b" * 64)
    hostile.mkdir()
    state, target, _, _, helper_b = bound_state(hostile)
    fence = v2_fence(target)
    result = rebind(helper_b, state)
    assert result.returncode == 0, result.stderr
    assert not fence.exists()
    assert (state / ".lock_migration.log").read_text().count(
        "rebind-host fence-recovery complete ") == 1


def test_identical_retry_and_fictitious_old_do_not_audit_transition(tmp_path):
    state, target, _, _, helper_b = bound_state(tmp_path)
    assert rebind(helper_b, state).returncode == 0
    binding = state / ".rtbioscan_lock_host_v1"
    before_binding = binding.read_bytes()
    audit = state / ".lock_migration.log"
    first_audit = audit.read_text()
    assert first_audit.count("rebind-host complete ") == 1
    assert rebind(helper_b, state).returncode == 0
    assert audit.read_text() == first_audit
    fence = v2_fence(target)
    fictitious = "macos:IOPlatformUUID:33333333-3333-4333-8333-333333333333"
    result = subprocess.run(["perl", str(helper_b), "rebind-host", str(state),
                             fictitious, ID_B, "--confirm"],
                            capture_output=True, text=True, timeout=12)
    assert result.returncode == 0, result.stderr
    assert not fence.exists() and binding.read_bytes() == before_binding
    delta = audit.read_text()[len(first_audit):]
    assert "rebind-host intent " not in delta
    assert "rebind-host complete " not in delta
    assert delta.count("rebind-host fence-recovery complete ") == 1


@pytest.mark.parametrize("step,anchor,removed,published", [
    ("before-barrier", "my $barrier_path = stable_barrier($dir);\n    sysopen(my $barrier, $barrier_path, O_RDWR) or fail(74, 'cannot inspect reset barrier');", False, False),
    ("after-barrier", "my @held = ($barrier);", False, False),
    ("after-stable", "my @recover;", False, False),
    ("after-validation", "if ($bound eq $new && !@recover) {", False, False),
    ("after-removal", "fail(74, 'state root changed before host binding publication')", True, False),
    ("during-publication", "if (defined $temp && !rename($temp, $path)) {", True, False),
    ("after-publication", "if (defined $temp) {\n        write_all($log, 'rebind-host complete", True, True),
])
def test_rebind_interrupted_at_each_step_converges_without_touching_evidence(
        tmp_path, step, anchor, removed, published):
    state, target, _, _, normal_helper = bound_state(tmp_path)
    fence = v2_fence(target)
    archive = state / (".round_inflight.lockdir.operator-" + "a" * 64)
    archive.mkdir()
    (archive / "evidence.txt").write_bytes(b"keep this evidence")
    archive_before = evidence_snapshot(archive)
    stable = pathlib.Path(str(target) + ".flock")
    stable_inode = stable.stat().st_ino
    binding = state / ".rtbioscan_lock_host_v1"
    old_binding = binding.read_bytes()
    marker = tmp_path / (step + ".ready")
    hook = "open(my $cut_marker, '>', '" + str(marker) + "') or die; close $cut_marker; sleep 10; "
    _, cut_helper = scratch_source(tmp_path / step, identity=ID_B,
                                   edits=((anchor, hook + anchor),))
    args = ["perl", str(cut_helper), "rebind-host", str(state), ID_A, ID_B, "--confirm"]
    process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_for(marker.exists, seconds=5), step
        assert process.poll() is None
        process.kill()
        process.wait(timeout=3)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    audit = state / ".lock_migration.log"
    interrupted_audit = audit.read_text() if audit.exists() else ""
    assert "rebind-host complete " not in interrupted_audit
    assert fence.exists() == (not removed)
    assert (binding.read_bytes() != old_binding) == published
    assert evidence_snapshot(archive) == archive_before
    assert stable.stat().st_ino == stable_inode
    assert rebind(normal_helper, state).returncode == 0
    assert not fence.exists()
    assert ID_B.split(":")[-1].encode() in binding.read_bytes()
    assert evidence_snapshot(archive) == archive_before
    assert stable.stat().st_ino == stable_inode
    final_audit = audit.read_text()
    assert final_audit.count("rebind-host fence-recovery complete ") == 1
    assert final_audit.count("rebind-host complete ") == (0 if published else 1)
