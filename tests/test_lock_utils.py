import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "lib" / "lock_utils.sh"


def test_init_lock_helpers_handles_empty_exit_hook_list_under_nounset(tmp_path: Path) -> None:
    lock_target = tmp_path / "guard_empty"
    script = f"""
set -euo pipefail
source "{SCRIPT}"
LOCK_WAIT=1
init_lock_helpers
acquire_lock "{lock_target}"
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not Path(f"{lock_target}.lockdir").exists()


def test_init_lock_helpers_preserves_existing_exit_trap_and_releases_locks(tmp_path: Path) -> None:
    lock_target = tmp_path / "guard"
    marker = tmp_path / "caller_exit.txt"
    script = f"""
set -euo pipefail
source "{SCRIPT}"
trap 'printf caller > "{marker}"' EXIT
LOCK_WAIT=1
init_lock_helpers
acquire_lock "{lock_target}"
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "caller"
    assert not Path(f"{lock_target}.lockdir").exists()


def test_init_lock_helpers_handles_existing_exit_trap_with_single_quotes(tmp_path: Path) -> None:
    lock_target = tmp_path / "guard_quoted"
    marker = tmp_path / "caller_exit_quoted.txt"
    script = f"""
set -euo pipefail
source "{SCRIPT}"
MARKER="{marker}"
trap 'printf %s '\''x'\'' > "$MARKER"' EXIT
LOCK_WAIT=1
init_lock_helpers
acquire_lock "{lock_target}"
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "x"
    assert not Path(f"{lock_target}.lockdir").exists()


def test_init_lock_helpers_runs_cleanup_even_if_existing_exit_trap_fails(tmp_path: Path) -> None:
    lock_target = tmp_path / "guard_fail"
    marker = tmp_path / "caller_exit_fail.txt"
    script = f"""
set -euo pipefail
source "{SCRIPT}"
trap 'printf fail > "{marker}"; false' EXIT
LOCK_WAIT=1
init_lock_helpers
acquire_lock "{lock_target}"
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "fail"
    assert not Path(f"{lock_target}.lockdir").exists()
    assert "WARN: EXIT hook 1 failed with status 1" in result.stderr


def test_init_lock_helpers_runs_cleanup_even_if_existing_exit_trap_exits(tmp_path: Path) -> None:
    lock_target = tmp_path / "guard_exit"
    marker = tmp_path / "caller_exit_exit.txt"
    script = f"""
set -euo pipefail
source "{SCRIPT}"
trap 'printf exit > "{marker}"; exit 0' EXIT
LOCK_WAIT=1
init_lock_helpers
acquire_lock "{lock_target}"
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="utf-8") == "exit"
    assert not Path(f"{lock_target}.lockdir").exists()


def test_init_lock_helpers_preserves_original_exit_status_for_existing_trap(tmp_path: Path) -> None:
    lock_target = tmp_path / "guard_status"
    marker = tmp_path / "caller_exit_status.txt"
    script = f"""
set -euo pipefail
source "{SCRIPT}"
trap 'printf %s $? > "{marker}"' EXIT
LOCK_WAIT=1
init_lock_helpers
acquire_lock "{lock_target}"
exit 7
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 7
    assert marker.read_text(encoding="utf-8") == "7"
    assert not Path(f"{lock_target}.lockdir").exists()


def test_init_lock_helpers_preserves_original_exit_status_when_existing_trap_fails(tmp_path: Path) -> None:
    lock_target = tmp_path / "guard_status_fail"
    marker = tmp_path / "caller_exit_status_fail.txt"
    script = f"""
set -euo pipefail
source "{SCRIPT}"
trap 'printf %s $? > "{marker}"; false' EXIT
LOCK_WAIT=1
init_lock_helpers
acquire_lock "{lock_target}"
exit 7
"""
    result = subprocess.run(
        ["bash", "-lc", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 7
    assert marker.read_text(encoding="utf-8") == "7"
    assert not Path(f"{lock_target}.lockdir").exists()
    assert "WARN: EXIT hook 1 failed with status 1" in result.stderr
